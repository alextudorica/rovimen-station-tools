#!/usr/bin/env python3
"""station_collect.py — the station's status collectors, without Flask.

What /api/status, /api/vitals, /api/chunks, /api/nights, /api/timelapses,
/api/rms_cameras, /api/live_cameras, /api/latest_ff_maxpixel and
/api/detections-index report is computed here; station_api.py wraps these
functions in routes and re-exports their names. Keeping them Flask-free lets
the pusher read the same data in-process (rovimen_pusher.py) and lets tests
call them without an app.

The module is stdlib-only at import time so it also imports under the system
python3 the pusher runs on (no Flask, psutil, numpy or Pillow there; psutil
and Pillow are imported inside the functions that need them). It must import
on Python 3.8 (gmnro05, gmnro08), see tests/test_station_py38_imports.py.
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

import ff_thumbs
import rovimen_config
import rovimen_lock
import rovimen_rms
import rovimen_night

try:
    import flags_manager as _flags_manager
    _STATE_MANAGER_OK = True
except ImportError:
    _STATE_MANAGER_OK = False

logger = logging.getLogger(__name__)

BASE        = Path.home()
CONFIG_PATH = rovimen_config.config_path()  # ~/meteor_detector honoured when only it exists
CHUNK_RE    = rovimen_night.CHUNK_RE


def _bundle_status() -> dict:
    """Bundle-integrity telemetry for /api/status (surfaced in heartbeats).

    Reads the verifier's on-disk artefacts (docs/signed_bundles.md): the
    installed ``.version``, the verifier state file, the verify-mode file and
    the pinned public key. All paths mirror bundle_verify.py's env overrides
    so tests can redirect them. Never raises — a missing file simply yields
    nulls, which the dashboard watchdog treats as "not yet rolled out"."""
    import hashlib

    version_file = CONFIG_PATH.parent / '.version'
    version = None
    try:
        version = version_file.read_text().strip() or None
    except OSError:
        pass

    state: dict = {}
    state_path = Path(os.environ.get(
        'ROVIMEN_BUNDLE_STATE',
        str(BASE / 'rovimen_scripts' / '.bundle_state.json'),
    ))
    try:
        state = json.loads(state_path.read_text())
    except (OSError, ValueError):
        pass

    mode_path = Path(os.environ.get(
        'ROVIMEN_UPDATE_VERIFY_MODE_FILE', '/etc/rovimen/update_verify_mode'))
    verify_mode = None
    try:
        raw = mode_path.read_text().strip().lower()
        verify_mode = raw if raw in ('warn', 'enforce') else 'warn'
    except OSError:
        pass

    pin_path = Path(os.environ.get(
        'ROVIMEN_BUNDLE_PUBKEY', '/etc/rovimen/bundle_signing_pub.pem'))
    pin_fp = None
    try:
        pin_fp = hashlib.sha256(pin_path.read_bytes()).hexdigest()[:16]
    except OSError:
        pass

    return {
        'version':      version,
        'build':        state.get('build'),
        'channel':      state.get('channel'),
        'verify_mode':  verify_mode,
        'last_verify':  state.get('last_verify'),
        'verified_at':  state.get('verified_at'),
        'pin_fp':       pin_fp,
        # How the latest update attempt ended (updater.sh via
        # bundle_verify --record-update): status started/applied/ok/
        # unhealthy/failed, version, at, failed_services.
        'update':       state.get('update') if isinstance(state.get('update'), dict) else None,
    }


# The system-timezone resolver, re-exported for station_api and its tests.
_system_zoneinfo = rovimen_night._system_zoneinfo


def _chunk_utc_dt(date_str: str, time_str: str, cfg: dict | None = None) -> datetime:
    """Return a chunk-name timestamp (``YYYYMMDD``, ``HHMMSS``) as a naive UTC datetime.

    Single conversion point for every consumer of chunk filename timestamps:
    the /api/chunks listing, detection-offset computation and FF maxpixel
    matching all go through here. Names are UTC unless the station sets
    ``chunk_names_utc: false`` (pre-#851 local names); see
    ``rovimen_night.chunk_utc_dt``, which detection_lock and the indexer
    share. With no ``cfg`` the station's config.json decides."""
    return rovimen_night.chunk_utc_dt(date_str, time_str,
                                      load_config() if cfg is None else cfg)


def _detection_offset_s(chunk_dt: datetime, mt_str: str | None,
                        detection_time_str: str | None) -> float | None:
    """Seconds from chunk start to the detection, for clip trimming.

    All operands are naive UTC datetimes: ``chunk_dt`` comes from
    ``_chunk_utc_dt``; ``meteor_time``/``detection_time`` are written by
    detection_lock from FF timestamps (UTC). Full-date subtraction gives exact
    midnight rollover (chunk 23:59:50, meteor 00:00:05 next day → 15 s).

    Prefers ``meteor_time`` (ISO, sub-second precision) over ``detection_time``
    (``YYYYMMDD_HHMMSS``). A negative difference (detection spilled in from
    the previous chunk) clamps to 0 — the offset seeks into *this* chunk.
    Returns None when neither field is parseable."""
    det_dt: datetime | None = None
    if mt_str:
        det_dt = datetime.fromisoformat(mt_str)
        if det_dt.tzinfo is not None:
            det_dt = det_dt.astimezone(timezone.utc).replace(tzinfo=None)
    elif detection_time_str:
        parts = detection_time_str.split('_')
        if len(parts) == 2 and len(parts[0]) == 8 and len(parts[1]) == 6:
            det_dt = datetime.strptime(detection_time_str, '%Y%m%d_%H%M%S')
        elif len(parts[-1]) == 6:
            # Time-only legacy sidecar: anchor to the chunk's own UTC date,
            # then shift to the nearest day (chunk just before midnight).
            ts = parts[-1]
            det_dt = chunk_dt.replace(hour=int(ts[:2]), minute=int(ts[2:4]),
                                      second=int(ts[4:6]), microsecond=0)
            if (det_dt - chunk_dt).total_seconds() < -43200:
                det_dt += timedelta(days=1)
            elif (det_dt - chunk_dt).total_seconds() > 43200:
                det_dt -= timedelta(days=1)
    if det_dt is None:
        return None
    return max(0.0, (det_dt - chunk_dt).total_seconds())


# ---------------------------------------------------------------------------
# TTL cache
# ---------------------------------------------------------------------------

_cache      = {}
_cache_lock = threading.Lock()


_cache_inflight: dict[str, threading.Event] = {}


def cached(key: str, ttl: float, fn):
    with _cache_lock:
        entry = _cache.get(key)
        if entry and time.monotonic() - entry['ts'] < ttl:
            return entry['val']
        wait_evt = _cache_inflight.get(key)

    if wait_evt is not None:
        wait_evt.wait(timeout=ttl + 30)
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.monotonic() - entry['ts'] < ttl:
                return entry['val']

    evt = threading.Event()
    with _cache_lock:
        existing = _cache_inflight.get(key)
        if existing is not None:
            wait_evt = existing
        else:
            _cache_inflight[key] = evt
            wait_evt = None

    if wait_evt is not None:
        wait_evt.wait(timeout=ttl + 30)
        with _cache_lock:
            entry = _cache.get(key)
            if entry and time.monotonic() - entry['ts'] < ttl:
                return entry['val']
        return fn()

    # The event is set however fn ends: when it raised, waiters used to sit
    # out ttl + 30 s (930 s for the storage scan) before computing it again.
    try:
        val = fn()
        with _cache_lock:
            _cache[key] = {'val': val, 'ts': time.monotonic()}
    finally:
        with _cache_lock:
            _cache_inflight.pop(key, None)
        evt.set()
    return val


def cached_swr(key: str, ttl: float, fn):
    """Stale-while-revalidate variant of cached().

    Never blocks a caller once a value exists: on expiry the stale value is
    returned immediately and a single background thread refreshes the entry.
    Only the very first call (nothing cached yet) computes inline, via
    cached(). Use for routes whose fn can block for tens of seconds (e.g.
    /api/status du-walking a busy capture disk) but are polled by
    low-timeout callers such as the pusher status heartbeat — a blocked
    refresh must never turn into a missed heartbeat.
    """
    with _cache_lock:
        entry = _cache.get(key)
        if entry is not None and time.monotonic() - entry['ts'] < ttl:
            return entry['val']
        spawn = entry is not None and key not in _cache_inflight
        if spawn:
            _cache_inflight[key] = threading.Event()

    if entry is None:
        # Cold start: no stale value to serve — block like cached() does.
        return cached(key, ttl, fn)

    if spawn:
        def _refresh():
            try:
                val = fn()
                with _cache_lock:
                    _cache[key] = {'val': val, 'ts': time.monotonic()}
            except Exception:
                logger.exception('cached_swr: background refresh of %r failed', key)
            finally:
                with _cache_lock:
                    evt = _cache_inflight.pop(key, None)
                if evt is not None:
                    evt.set()

        threading.Thread(target=_refresh, name=f'swr-{key}', daemon=True).start()
    return entry['val']


# ---------------------------------------------------------------------------
# Config, paths, storage, night dates
# ---------------------------------------------------------------------------

def load_config() -> dict:
    # Cached for 5 s: most request handlers call load_config() one or more times
    # per request, and the dashboard polls every 60 s. Re-reading + parsing
    # config.json on every call burned syscalls + JSON-parse time for data that
    # only changes when the operator edits the file. 5 s is short enough that
    # config edits are picked up within the next poll, long enough to coalesce
    # all the load_config() calls within a single request.
    return cached('config', 5, lambda: json.loads(CONFIG_PATH.read_text()))


def capture_base() -> Path:
    return rovimen_config.capture_root(load_config())


def _rms_camera_dirs() -> list[tuple[str, Path, str | None]]:
    """Discover RMS camera directories and station codes.

    Returns list of (cam_label, cam_dir, station_code) tuples.
    Uses config.json stations dict if available (works for any layout),
    falls back to glob('cam*') for legacy gmn0002/gmn0003 layout.
    """
    cfg = load_config()
    stations = cfg.get('stations', {})
    results = []
    if stations:
        for sid, sinfo in stations.items():
            rms_path = sinfo.get('rms_data_path')
            if rms_path:
                cam_dir = Path(rms_path)
                if cam_dir.exists():
                    results.append((cam_dir.name, cam_dir, sid))
    if not results:
        # Fallback: scan ~/RMS_data/ for any subdirectory containing ArchivedFiles or CapturedFiles
        rms_base = BASE / 'RMS_data'
        if rms_base.exists():
            for d in sorted(rms_base.iterdir()):
                if d.is_dir() and ((d / 'ArchivedFiles').exists() or (d / 'CapturedFiles').exists()):
                    results.append((d.name, d, None))
    return results


def _du(path: Path) -> int:
    """Return total bytes for a directory tree via du -sbL (follows symlinks)."""
    try:
        r = subprocess.run(['du', '-sbL', str(path)], capture_output=True, text=True, timeout=30)
        return int(r.stdout.split()[0]) if r.returncode == 0 else 0
    except Exception:
        return 0


def _get_device(path: Path) -> str:
    """Return the block device backing path (follows symlinks)."""
    try:
        r = subprocess.run(['df', '--output=source', str(path)],
                           capture_output=True, text=True)
        lines = r.stdout.strip().splitlines()
        return lines[1].strip() if len(lines) >= 2 else ''
    except Exception:
        return ''


# The du walks below are by far the most expensive part of /api/status —
# tens of seconds on a busy capture disk (worst case: NTFS-3g/FUSE mounts
# under night-capture write load, e.g. gmn0004). The numbers they produce
# move slowly, so they get their own much longer TTL instead of being
# recomputed on every 60 s status refresh.
_STORAGE_TTL = 900


def _scan_storage() -> dict:
    """Scan capture + RMS trees with du. Slow — cache with _STORAGE_TTL."""
    storage: dict = {}
    folders = {
        'color_capture': capture_base(),
    }
    for name, path in folders.items():
        if not path.exists():
            storage[name] = None
            continue
        total = _du(path)
        oldest, newest, days = _date_range(path)
        storage[name] = {'bytes': total, 'oldest': oldest,
                         'newest': newest, 'days': days,
                         'device': _get_device(path)}

    # RMS CapturedFiles + ArchivedFiles
    rms_total = 0
    rms_dates: list[str] = []
    for _label, cam_dir, _sid in _rms_camera_dirs():
        for sub in ('CapturedFiles', 'ArchivedFiles'):
            sub_dir = cam_dir / sub
            if not sub_dir.exists():
                continue
            rms_total += _du(sub_dir)
            # A failing disk answers EIO here; a bare iterdir() made every
            # status refresh raise, and the last good status was served on.
            rms_dates += [p.name for p in _safe_iterdir(sub_dir)
                          if _safe_is_dir(p) and len(p.name) >= 8 and p.name[:8].isdigit()]
    rms_dates = sorted(set(rms_dates))
    storage['rms'] = {'bytes': rms_total,
                      'oldest': rms_dates[0]  if rms_dates else None,
                      'newest': rms_dates[-1] if rms_dates else None,
                      'days':   len(rms_dates),
                      'device': _get_device(BASE)}
    return storage


# Night naming and the morning rollover are rovimen_night's, shared with
# dawn_process, detection_lock and the indexer. _last_completed_night uses the
# 14:00 UTC rollover (past dawn's 13:15 deadline and the 13:45 reboot, before
# any station's sunset); the status routes used to roll over at noon, so
# between 12:00 and 14:00 they showed tonight's empty night while dawn was
# still working on the one that just ended.
_night_date = rovimen_night.night_of
_NIGHT_ROLLOVER_HOUR = rovimen_night.NIGHT_ROLLOVER_HOUR
_last_completed_night = rovimen_night.last_completed_night


def _safe_iterdir(d: Path) -> list[Path]:
    """Like list(d.iterdir()) but returns [] on I/O errors."""
    try:
        return list(d.iterdir())
    except OSError:
        logger.warning('I/O error listing %s — skipping', d)
        return []


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except OSError:
        return False


def _safe_is_file(p: Path) -> bool:
    try:
        return p.is_file()
    except OSError:
        return False


def _date_range(path: Path):
    """Return (oldest, newest, count) of YYYYMMDD subdirs anywhere under path.

    Tolerates per-subdir OSError (e.g. NTFS index corruption returning EIO on
    iterdir/listdir): the bad subtree is logged and skipped instead of taking
    the whole walker down.
    """
    dates: set[str] = set()

    def _onerror(exc: OSError) -> None:
        sys.stderr.write(f'_date_range: skipping unreadable {exc.filename}: {exc}\n')

    for root, dirs, _files in os.walk(path, onerror=_onerror):
        for part in Path(root).parts:
            if len(part) == 8 and part.isdigit():
                dates.add(part)
        for d in dirs:
            if len(d) == 8 and d.isdigit():
                dates.add(d)

    sdates = sorted(dates)
    return (sdates[0] if sdates else None,
            sdates[-1] if sdates else None,
            len(sdates))


# ---------------------------------------------------------------------------
# Segment health — colour-capture cadence watchdog
# ---------------------------------------------------------------------------
#
# When a camera stream corrupts, ffmpeg's segment muxer stops honouring
# -segment_time and cuts on almost every keyframe candidate: instead of one
# 20 s clip it writes thousands of ~1 s files (RO000Y once produced 1543 of
# them inside a single hour). The mirror-image failure is the stream stalling,
# which leaves multi-minute holes between segments. Neither shows up in the
# rest of /api/status — services stay active, the disk stays healthy — so both
# used to be discovered the morning after, with the night already lost.
#
# The signal is derived from the night directory listing alone: every segment
# carries its own start time in its filename, so nothing is opened, decoded or
# stat()ed. One listdir per camera per cache period.

SEGMENT_HEALTH_WINDOW_S = 3600   # trailing window the counters describe
SEGMENT_SHORT_MAX_S     = 3      # spacing <= this  => one "short" segment
SEGMENT_STALL_MIN_S     = 40     # spacing >  this  => one "stall" (coverage hole)
_SEGMENT_HEALTH_TTL     = 60     # a listdir is cheap, but not on a storm night


def _segment_name_ts(date_str: str, time_str: str) -> datetime:
    """Default converter: a colour segment name is already UTC (#851)."""
    return datetime(
        int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
        int(time_str[:2]), int(time_str[2:4]), int(time_str[4:6]),
    )


def segment_health(names, now: datetime,
                   window_s: int = SEGMENT_HEALTH_WINDOW_S,
                   short_max_s: int = SEGMENT_SHORT_MAX_S,
                   stall_min_s: int = SEGMENT_STALL_MIN_S,
                   to_utc=_segment_name_ts) -> dict:
    """Bucket colour-segment cadence over the trailing ``window_s`` seconds.

    Pure function of ``names`` (one night directory's filenames, any order,
    unrelated files tolerated) and ``now`` (naive UTC). ``to_utc`` converts a
    name's ``YYYYMMDD``/``HHMMSS`` fields to naive UTC — the caller injects
    ``_chunk_utc_dt`` so legacy locally-named artefacts land in the same clock
    as ``now`` instead of an hours-off window that silently reads as empty.

    Cadence is measured between consecutive segment *start* times, which is
    the segment's real length on disk: ffmpeg names each segment when it opens
    it, so the next name's timestamp is when this one was closed. A pair
    closer together than ``short_max_s`` counts one ``short`` segment (the
    clip-storm signature); a pair further apart than ``stall_min_s`` counts
    one ``stall``.
    """
    cutoff = now - timedelta(seconds=window_s)
    stamps = []
    for name in names:
        m = CHUNK_RE.match(name)
        if not m:
            continue
        try:
            ts = to_utc(m.group(2), m.group(3))
        except ValueError:
            continue
        # Clamp to the window at both ends: a timestamp in the future means a
        # mis-clocked or mis-named artefact, and counting it would inflate the
        # gap either side of it.
        if cutoff <= ts <= now:
            stamps.append(ts)
    stamps.sort()

    short = 0
    stalls = 0
    max_gap_s = None
    for prev, cur in zip(stamps, stamps[1:]):
        spacing = (cur - prev).total_seconds()
        if spacing <= short_max_s:
            short += 1
        if spacing > stall_min_s:
            stalls += 1
        if max_gap_s is None or spacing > max_gap_s:
            max_gap_s = spacing

    return {
        'window_min': int(round(window_s / 60.0)),
        'segments':   len(stamps),
        'short':      short,
        'stalls':     stalls,
        'max_gap_s':  None if max_gap_s is None else int(round(max_gap_s)),
        'newest':     stamps[-1].strftime('%Y-%m-%dT%H:%M:%SZ') if stamps else None,
    }


def _segment_health_night_dir(cap: Path, cam: str, night: str) -> Path | None:
    """Tonight's colour directory: per-camera layout first, then flat."""
    for d in (cap / cam / night, cap / night):
        if _safe_is_dir(d):
            return d
    return None


def _segment_health_for_camera(cap: Path, cam: str, night: str, now: datetime,
                               to_utc) -> dict:
    night_dir = _segment_health_night_dir(cap, cam, night)
    if night_dir is None:
        # Daytime, or capture has not opened tonight's directory yet. An idle
        # camera is not a fault: report zeros with night=None, never an error.
        health = segment_health([], now, to_utc=to_utc)
        health['night'] = None
        return health
    try:
        names = os.listdir(str(night_dir))
    except OSError:
        logger.warning('segment health: cannot list %s — skipping', night_dir)
        health = segment_health([], now, to_utc=to_utc)
        health['night'] = None
        return health
    health = segment_health(names, now, to_utc=to_utc)
    health['night'] = night
    return health


def _collect_segment_health() -> dict:
    """Per-camera segment health for the current night. Never raises."""
    try:
        cfg = load_config()
        cap = capture_base()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        night = _night_date(now.strftime('%Y%m%d'), now.strftime('%H%M%S'))

        def _to_utc(date_str: str, time_str: str) -> datetime:
            return _chunk_utc_dt(date_str, time_str, cfg)

        return {
            cam: _segment_health_for_camera(cap, cam, night, now, _to_utc)
            for cam in cfg.get('stations', {})
        }
    except Exception:
        logger.exception('segment health: collection failed')
        return {}


# ---------------------------------------------------------------------------
# Capture evidence (per RMS camera)
# ---------------------------------------------------------------------------
#
# Whether a camera records is read from what RMS wrote, not from its service
# state: RMS's unit stays "active" all day and while its camera is off the
# wire (RO0015 and RO001S wrote 0 FF files a night for weeks with rms-cam*
# active). RMS writes one FF file per 256 frames (10.24 s at 25 fps) while it
# captures, so the newest FF file's time, the count over the last half hour
# and the per-night counts carry the whole answer. The dashboard turns this
# into recording / degraded / stalled / idle (capture_state.py there).

CAPTURE_YIELD_WINDOW_S = 1800    # FF yield is judged over the last half hour
_CAPTURE_TTL           = 30
_CAPTURE_NIGHTS        = 2       # per-night FF counts for the newest nights


def summarise_capture(ff_names: list, now: datetime, fps_nominal: float,
                      session_start: datetime | None, rms_state: str | None,
                      guard: str | None) -> dict:
    """One camera's capture block from its FF file names (pure).

    ``ff_names`` are the FF names of its newest sessions (any order, unrelated
    names tolerated), ``now`` aware UTC. ``guard`` is camera_stall_guard's
    standing verdict for tonight ('stalled', 'degraded', 'lossy') or None.
    """
    period = 256.0 / fps_nominal if fps_nominal > 0 else 10.24
    window_start = now - timedelta(seconds=CAPTURE_YIELD_WINDOW_S)
    newest = None
    in_window = 0
    nights: dict[str, int] = {}
    for name in ff_names:
        t = rovimen_night.ff_file_start(name)
        if t is None:
            continue
        if t > now + timedelta(minutes=5):
            continue  # mis-clocked name: it would read as recording forever
        night = rovimen_night.current_night(t)  # the night this FF belongs to
        nights[night] = nights.get(night, 0) + 1
        if newest is None or t > newest:
            newest = t
        if t > window_start:
            in_window += 1
    # Expected over the part of the window RMS's session has run, so the first
    # minutes of a night are not read as a poor yield.
    covered = CAPTURE_YIELD_WINDOW_S
    if session_start is not None:
        covered = max(0.0, min(covered, (now - session_start).total_seconds()))
    keep = sorted(nights, reverse=True)[:_CAPTURE_NIGHTS]
    return {
        'newest_ff': newest.strftime('%Y-%m-%dT%H:%M:%SZ') if newest else None,
        'ff_window': in_window,
        'ff_window_expected': int(covered / period),
        'window_min': CAPTURE_YIELD_WINDOW_S // 60,
        'ff_period_s': round(period, 2),
        'rms_state': rms_state,
        'guard': guard,
        'nights': {n: nights[n] for n in keep},
    }


# Per-night FF counts outlive the files they count. Once a night is at GMN
# the janitor frees its CapturedFiles session (and RMS's own cleanup can take
# it sooner), so a count of the files still on disk read 0 by the evening:
# on 2026-11-12 the overview showed "none" for night 1111 of RO000T, RO000U,
# RO000V (Vâlcele), RO000B, RO001K (Ghirdoveni), all captured in full and
# uploaded. Each night keeps the largest count seen, in a small state file
# shared by station_api and the pusher, written at most every
# CAPTURE_COUNTS_SAVE_S (a restart loses at most that much of tonight's count;
# finished nights are long saved).
CAPTURE_COUNTS_PATH = Path.home() / 'rovimen_scripts' / 'capture_nights.json'
CAPTURE_COUNTS_SAVE_S = 600
CAPTURE_COUNTS_KEEP_DAYS = 7
_capture_counts: dict = {'data': None, 'saved_at': 0.0, 'dirty': False}
_capture_counts_lock = threading.Lock()


def _merge_counts(into: dict, cam: str, nights: dict) -> bool:
    """Max-merge *nights* into ``into[cam]``; True when anything grew."""
    have = into.setdefault(cam, {})
    grew = False
    for night, n in nights.items():
        if isinstance(n, int) and n > int(have.get(night) or 0):
            have[night] = n
            grew = True
    return grew


def _prune_counts(data: dict, now: datetime) -> None:
    cutoff = (now - timedelta(days=CAPTURE_COUNTS_KEEP_DAYS)).strftime('%Y%m%d')
    for cam in list(data):
        nights = data[cam] if isinstance(data[cam], dict) else {}
        data[cam] = {n: c for n, c in nights.items() if n >= cutoff}
        if not data[cam]:
            del data[cam]


def _read_counts(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def remember_night_counts(cam: str, nights: dict, now: datetime,
                          path: Path | None = None) -> dict:
    """*nights* (``{YYYYMMDD: FF count}`` from the files on disk) merged with
    the largest counts recorded before, newest _CAPTURE_NIGHTS nights."""
    path = path or CAPTURE_COUNTS_PATH
    with _capture_counts_lock:
        state = _capture_counts
        if state['data'] is None or state.get('path') != path:
            state.update(data=_read_counts(path), saved_at=0.0, dirty=False, path=path)
        data = state['data']
        if _merge_counts(data, cam, nights):
            state['dirty'] = True
        now_s = now.timestamp()
        if state['dirty'] and now_s - state['saved_at'] >= CAPTURE_COUNTS_SAVE_S:
            # The other process (station_api or the pusher) may have saved
            # counts this one never saw: merge them in before replacing.
            on_disk = _read_counts(path)
            for other_cam, other in on_disk.items():
                if isinstance(other, dict):
                    _merge_counts(data, other_cam, other)
            _prune_counts(data, now)
            try:
                rovimen_config.atomic_write_json(path, data, indent=None)
                state.update(saved_at=now_s, dirty=False)
            except OSError:
                logger.warning('capture: cannot save night counts to %s', path)
        merged = dict(data.get(cam) or {})
    keep = sorted(merged, reverse=True)[:_CAPTURE_NIGHTS]
    return {n: merged[n] for n in keep}


_RMS_STATE_RES = (
    # Continuous capture (the fleet default): CaptureModeSwitcher at -9 deg.
    (re.compile(r'switching to (day|night)time mode'), None),
    # Scheduled capture: StartCapture's own lines.
    (re.compile(r'Starting capture for'), 'capturing'),
    (re.compile(r'Ending capture'), 'ended'),
    (re.compile(r'to start recording for'), 'waiting'),
)
_rms_log_scan: dict = {}   # log path -> (bytes read, (state, since))
_RMS_LOG_STAMP = re.compile(r'^(\d{4})/(\d{2})/(\d{2}) (\d{2}):(\d{2}):(\d{2})')


def rms_state_from_lines(lines, state: tuple = (None, None)) -> tuple:
    """Newest RMS capture state in ``lines`` and when RMS logged it:
    ('night'|'day', ...) from a mode switch, or ('capturing'|'ended'|
    'waiting', ...) from scheduled capture; ``state`` if the lines hold none.
    The time is ``YYYY-MM-DDTHH:MM:SSZ`` from the log line, or None."""
    for line in lines:
        for pattern, value in _RMS_STATE_RES:
            m = pattern.search(line)
            if m:
                t = _RMS_LOG_STAMP.match(line)
                since = '{}-{}-{}T{}:{}:{}Z'.format(*t.groups()) if t else None
                state = (value or m.group(1), since)
                break
    return state


def _rms_log_state(log: Path) -> tuple:
    """RMS's newest capture state in its log, reading only bytes added since
    the last call. The mode switch sits near the start of a night's log, which
    grows to ~15 MB by dawn, so a tail read would lose it within the hour."""
    key = str(log)
    offset, state = _rms_log_scan.get(key, (0, (None, None)))
    try:
        size = log.stat().st_size
        if size < offset:
            offset, state = 0, (None, None)
        with open(log, 'rb') as f:
            f.seek(offset)
            data = f.read()
    except OSError:
        return state
    end = data.rfind(b'\n') + 1   # keep a partial last line for next time
    state = rms_state_from_lines(data[:end].decode(errors='replace').splitlines(), state)
    if len(_rms_log_scan) > 64:
        _rms_log_scan.clear()
    _rms_log_scan[key] = (offset + end, state)
    return state


def _collect_capture() -> dict:
    """{camera code: capture block} for every running RMS. Never raises.

    An RMS that is not running has no block; the dashboard reads a registry
    camera without one as "RMS not running" while the station reports."""
    try:
        import camera_fps_guard as fps
        import camera_stall_guard as stall
    except Exception:
        logger.exception('capture: guard modules unavailable')
        return {}
    now = datetime.now(timezone.utc)
    try:
        guard_state = json.loads(stall.STATE_PATH.read_text())
    except (OSError, ValueError):
        guard_state = {}
    tonight = fps.night_key(now)
    out: dict = {}
    try:
        processes = fps.rms_processes()
    except Exception:
        logger.exception('capture: cannot list RMS processes')
        return {}
    for proc in processes:
        try:
            cam = fps.parse_rms_config(proc.config)
            if cam is None or cam.code in out:
                continue
            names: list = []
            for d in stall._session_dirs(cam)[-3:]:
                try:
                    names.extend(os.listdir(d))
                except OSError:
                    continue
            log = fps.latest_log(cam)
            rms_state, rms_since = _rms_log_state(log) if log else (None, None)
            cs = guard_state.get(cam.code) or {}
            guard = cs.get('kind') if cs.get('night') == tonight and cs.get('bad_since') else None
            block = summarise_capture(
                names, now, cam.fps, stall.session_start(cam, now), rms_state, guard)
            block['nights'] = remember_night_counts(cam.code, block['nights'], now)
            block['rms_state_since'] = rms_since
            out[cam.code] = block
        except Exception:
            logger.exception('capture: %s failed', proc.config)
    return out


# ---------------------------------------------------------------------------
# Vitals (/api/vitals)
# ---------------------------------------------------------------------------

_io_prev: dict = {}  # ts, write_bytes
_io_lock = threading.Lock()
_proc_cpu_prev: dict[int, float] = {}  # pid -> cumulative cpu seconds
_proc_cpu_ts: float = 0.0
_proc_cpu_result: list = []
_proc_cpu_result_ts: float = 0.0
_proc_cpu_lock = threading.Lock()
_PROC_CACHE_TTL: float = 2.0   # recompute every 2 s, aligned with fast-poll


def _top_procs_by_cpu() -> list[dict]:
    """Return top-5 CPU consumers by name, using cumulative CPU time deltas.
    Has its own 5 s cache so it runs every fast-poll, not every 15 s vitals cache."""
    global _proc_cpu_ts, _proc_cpu_result, _proc_cpu_result_ts
    import psutil
    now = time.monotonic()

    with _proc_cpu_lock:
        if now - _proc_cpu_result_ts < _PROC_CACHE_TTL:
            return list(_proc_cpu_result)
        prev_snapshot = dict(_proc_cpu_prev)
        prev_ts = _proc_cpu_ts

    dt = now - prev_ts if prev_ts else 0.0

    curr: dict[int, tuple[str, float]] = {}
    for p in psutil.process_iter(['pid', 'name', 'cpu_times', 'status']):
        try:
            if p.info['status'] == 'zombie':
                continue
            t = p.info['cpu_times']
            curr[p.pid] = (p.info['name'] or '?', t.user + t.system)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    by_name: dict[str, float] = {}
    if dt > 0 and prev_snapshot:
        for pid, (name, total) in curr.items():
            prev_total = prev_snapshot.get(pid)
            if prev_total is not None:
                delta_pct = (total - prev_total) / dt * 100
                if delta_pct > 0:
                    by_name[name] = by_name.get(name, 0.0) + delta_pct

    top = sorted(by_name.items(), key=lambda x: x[1], reverse=True)[:5]
    result = [{'name': name, 'cores': round(pct / 100, 1)} for name, pct in top if pct > 0]

    with _proc_cpu_lock:
        _proc_cpu_prev.clear()
        _proc_cpu_prev.update({pid: total for pid, (_, total) in curr.items()})
        _proc_cpu_ts = now
        _proc_cpu_result = result
        _proc_cpu_result_ts = now
    return result


def _disk_write_mbps() -> float:
    """Return disk write MB/s averaged since the last call."""
    try:
        import psutil
        io = psutil.disk_io_counters()
        now = time.monotonic()
        with _io_lock:
            prev = _io_prev.copy()
            _io_prev['ts'] = now
            _io_prev['write'] = io.write_bytes
        if prev.get('ts') and (now - prev['ts']) < 120:
            dt = now - prev['ts']
            return round((io.write_bytes - prev['write']) / dt / 1e6, 2)
    except Exception:
        pass
    return 0.0


# The pusher polls /api/vitals every 30 s (rovimen_pusher.VITALS_INTERVAL);
# a TTL just under that gives each of its polls a fresh reading while any
# other caller in between shares it.
_VITALS_TTL = 25
_cpu_primed = False
_cpu_prime_lock = threading.Lock()


def _cpu_percent_per_core(psutil) -> list[float]:
    """Per-core CPU % since the previous call, without blocking.

    ``cpu_percent(interval=None)`` compares against the previous call, so
    each reading covers the whole poll interval (30 s for the pusher) instead
    of the one blocking second the old ``interval=1`` sampled, which held a
    request thread for that second on every refresh. Only the very first
    call has no baseline; it samples a short blocking window once."""
    global _cpu_primed
    with _cpu_prime_lock:
        if not _cpu_primed:
            _cpu_primed = True
            return psutil.cpu_percent(percpu=True, interval=0.5)
    return psutil.cpu_percent(percpu=True, interval=None)


def collect_vitals() -> dict:
    """CPU, RAM, temperature and disk-write rate (``top_procs`` excluded)."""
    result = {'online': True, 'last_updated': datetime.now(timezone.utc).isoformat()}
    try:
        import psutil
        per_core_pct = _cpu_percent_per_core(psutil)
        result['cpu_pct'] = round(sum(per_core_pct) / len(per_core_pct), 1) if per_core_pct else 0
        freqs = psutil.cpu_freq(percpu=True) or []
        result['cores'] = [
            {
                'load_pct': round(p, 1),
                'freq_ghz': round(freqs[i].current / 1000, 2) if i < len(freqs) else None,
            }
            for i, p in enumerate(per_core_pct)
        ]
        vm = psutil.virtual_memory()
        result['ram_pct'] = round(vm.percent, 1)
        result['ram_used_mb'] = vm.used // (1024 * 1024)
        result['ram_total_mb'] = vm.total // (1024 * 1024)
        try:
            sensors = psutil.sensors_temperatures() or {}  # one sysfs scan, not three
        except (AttributeError, OSError):
            sensors = {}
        for key in ('coretemp', 'k10temp', 'cpu_thermal'):
            temps = sensors.get(key, [])
            if temps:
                result['temp_c'] = round(temps[0].current, 1)
                break
        result['total_cores'] = len(per_core_pct)
    except ImportError:
        result['error'] = 'psutil not installed'
    result['disk_write_mbps'] = _disk_write_mbps()
    return result


# ---------------------------------------------------------------------------
# Service state probing (reported by /api/status)
# ---------------------------------------------------------------------------

SVC_ACTIVE = 'active'
# The work is running, but not under a systemd unit this API could resolve
# (legacy nohup/cron installs, or a unit named outside the families below).
# Reported as an OK-with-note so the dashboard stops calling a capturing
# camera dead, without pretending the unit is managing it.
SVC_UNMANAGED = 'active (unmanaged)'
SVC_FAILED = 'failed'
SVC_INACTIVE = 'inactive'

# ROVIMEN services reported on every station → the process each one runs, used
# as the `pgrep -f` pattern when systemd cannot answer for the unit.
_ROVIMEN_SERVICES: dict[str, str] = {
    'color-capture': 'color_capture.py',
    'rovimen-station-api': 'station_api.py',
    'detection-indexer': 'detection_indexer.py',
}

# A real RMS capture process, either `python -m RMS.StartCapture` or
# `python RMS/StartCapture.py`. Narrow enough not to count a shell pipeline
# that merely mentions StartCapture.
_CAPTURE_CMD_RE = re.compile(r'RMS[./]StartCapture')

# systemd unit-name families that implement an RMS capture instance. Station
# installs are not consistent: the bundle installer writes rms-cam{N}, the
# older hand-rolled per-user units are rms-capture / rms-capture-cam{N}, and
# upstream RMS documents gmn-capture. A camera must not be reported dead just
# because its unit uses another one of these names.
_RMS_UNIT_FAMILIES = ('rms-cam{n}', 'rms-capture-cam{n}', 'gmn-capture-cam{n}')
_RMS_UNIT_FAMILIES_FIRST = ('rms-capture', 'gmn-capture')

# systemd states ordered worst → best. A unit installed in both scopes (a
# system copy left behind by an installer switch plus the live copy under
# ~/.config/systemd/user) is only really down when neither scope runs it, so
# merging the two inventories keeps the best answer.
_ACTIVE_RANK = {'': 0, 'inactive': 1, 'deactivating': 2, 'failed': 3,
                'activating': 4, 'reloading': 5, 'active': 6}
_ENABLED_RANK = {'': 0, 'masked': 1, 'masked-runtime': 1, 'disabled': 2,
                 'indirect': 3, 'static': 4, 'generated': 5, 'alias': 5,
                 'transient': 5, 'enabled-runtime': 6, 'enabled': 7}
_UNIT_ACTIVE_OK = ('active', 'activating', 'reloading')
_UNIT_OFF_BY_CHOICE = ('disabled', 'masked', 'masked-runtime')


class _UnitInfo(NamedTuple):
    """Merged systemd view of one unit. Empty strings mean "systemd in no
    scope knows this unit"."""

    active: str = ''
    enabled: str = ''


def _systemctl_lines(user_scope: bool, args: list[str]) -> list[str]:
    """Rows of a `systemctl [--user] <args>` listing.

    Returns [] when the scope is unreachable instead of raising, so a station
    without a per-user systemd instance still reports on its system units.
    """
    env = None
    if user_scope:
        env = dict(os.environ)
        # The station API runs as a *system* unit (User=<station>) and so
        # inherits no XDG_RUNTIME_DIR. Without it `systemctl --user` cannot
        # reach the per-user bus and every user unit looks absent — which is
        # how a station whose whole pipeline lives in ~/.config/systemd/user
        # ends up reported dead while it is happily capturing.
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    cmd = ['systemctl'] + (['--user'] if user_scope else []) + args
    try:
        r = subprocess.run(cmd + ['--no-legend', '--no-pager'],
                           capture_output=True, text=True, timeout=20, env=env)
    except (OSError, subprocess.SubprocessError):
        return []
    if r.returncode != 0:
        return []
    return r.stdout.splitlines()


def _merge_unit_rows(inv: dict[str, _UnitInfo], rows: list[str],
                     column: int, field: str) -> None:
    """Fold one systemctl listing into the inventory, keeping the best state.

    `column` is the whitespace-separated field holding the state (2 for the
    ACTIVE column of `list-units`, 1 for the state column of
    `list-unit-files`); `field` names the _UnitInfo slot it fills.
    """
    ranks = _ACTIVE_RANK if field == 'active' else _ENABLED_RANK
    for row in rows:
        parts = row.split()
        if len(parts) <= column or not parts[0].endswith('.service'):
            continue
        unit = parts[0][:-len('.service')]
        state = parts[column]
        current = inv.get(unit, _UnitInfo())
        if ranks.get(state, 0) <= ranks.get(getattr(current, field), 0):
            continue
        inv[unit] = current._replace(**{field: state})


def _unit_inventory() -> dict[str, _UnitInfo]:
    """Unit name (without `.service`) → merged state, across both systemd
    scopes.

    Two listings per scope answer for every service at once, which is both
    cheaper than probing each unit and the only way to see units whose names
    we would otherwise have to guess.
    """
    inv: dict[str, _UnitInfo] = {}
    for user_scope in (False, True):
        _merge_unit_rows(
            inv, _systemctl_lines(user_scope, ['list-units', '--type=service',
                                               '--all', '--plain']),
            column=2, field='active')
        _merge_unit_rows(
            inv, _systemctl_lines(user_scope, ['list-unit-files',
                                               '--type=service']),
            column=1, field='enabled')
    return inv


def _service_unit_candidates(svc: str) -> list[str]:
    """Unit names that may implement a service key, best guess first."""
    m = re.match(r'rms-cam(\d+)$', svc)
    if not m:
        return [svc]
    n = m.group(1)
    names = [family.format(n=n) for family in _RMS_UNIT_FAMILIES]
    if n == '1':
        # The first instance is often the plain single-camera unit.
        names += list(_RMS_UNIT_FAMILIES_FIRST)
    return names


def _resolve_unit(svc: str, inv: dict[str, _UnitInfo]) -> _UnitInfo:
    """Merged systemd state of whichever candidate unit backs this service."""
    best = _UnitInfo()
    for unit in _service_unit_candidates(svc):
        info = inv.get(unit)
        if info is None:
            continue
        if _ACTIVE_RANK.get(info.active, 0) > _ACTIVE_RANK.get(best.active, 0):
            best = best._replace(active=info.active)
        if _ENABLED_RANK.get(info.enabled, 0) > _ENABLED_RANK.get(best.enabled, 0):
            best = best._replace(enabled=info.enabled)
    return best


def _proc_cwd(pid: str) -> str:
    """Working directory of a process, '' when it cannot be read."""
    try:
        return os.readlink(f'/proc/{pid}/cwd')
    except OSError:
        return ''


def _capture_marker(cmdline: str, cwd: str) -> str:
    """The RMS_cam{N} instance a StartCapture process belongs to.

    RMS takes its config from the working directory, so a per-camera instance
    is identified by its argv *or* by its cwd — `python -m RMS.StartCapture`
    started from ~/RMS_cam2 carries no RMS_cam2 token in argv at all, which is
    exactly the case the old argv-only match reported as dead.
    Returns '' for an instance that runs from a non-numbered layout
    (~/source/RMS, ~/source/Stations/<code>).
    """
    for text in (cmdline, cwd):
        m = re.search(r'RMS_cam(\d+)', text or '')
        if m:
            return f'RMS_cam{m.group(1)}'
    return ''


def _capture_process_markers() -> list[str]:
    """One marker per running RMS capture process (see _capture_marker)."""
    try:
        r = subprocess.run(['pgrep', '-af', 'StartCapture'],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    own_pid = str(os.getpid())
    markers: list[str] = []
    for line in r.stdout.splitlines():
        pid, _, cmdline = line.strip().partition(' ')
        if pid == own_pid or not _CAPTURE_CMD_RE.search(cmdline):
            continue
        markers.append(_capture_marker(cmdline, _proc_cwd(pid)))
    return markers


def _rms_service_running(svc: str, markers: list[str], numbered: bool) -> bool:
    """True if a capture process belongs to this RMS service key.

    `numbered` says whether the station also reports per-camera `rms-cam{N}`
    keys. When it does, the catch-all `rms-capture` key stands for the
    instance running from a non-numbered layout and must only match capture
    processes no numbered camera claims — otherwise a healthy camera would
    mask its dead sibling.
    """
    m = re.match(r'rms-cam(\d+)$', svc)
    if m:
        return f'RMS_cam{m.group(1)}' in markers
    if not numbered:
        return bool(markers)
    return '' in markers


def _service_state(unit: _UnitInfo, process_running: bool) -> str | None:
    """Resolve one service row.

    None means "don't report": the unit exists, is deliberately off, and
    nothing is running under it.
    """
    if unit.active in _UNIT_ACTIVE_OK:
        return SVC_ACTIVE
    if unit.enabled in _UNIT_OFF_BY_CHOICE and not process_running:
        return None
    if process_running:
        return SVC_UNMANAGED
    if unit.active == 'failed':
        return SVC_FAILED
    return SVC_INACTIVE


def _rms_service_names(base: Path, inv: dict[str, _UnitInfo],
                       markers: list[str]) -> list[str]:
    """Service keys for the station's RMS capture instances.

    Multi-instance stations keep one ~/RMS_cam{N} directory per camera and get
    one `rms-cam{N}` key each. An instance running from the classic
    ~/source/RMS layout has no such directory, so it is picked up from its
    unit file (or, failing that, from a running capture process) as
    `rms-capture` — otherwise a mixed-layout station never reports on that
    camera at all and its death goes unnoticed.
    """
    names: list[str] = []
    for cam_dir in sorted(p for p in base.glob('RMS_cam*') if p.is_dir()):
        m = re.match(r'RMS_cam(\d+)$', cam_dir.name)
        if m:
            names.append(f'rms-cam{m.group(1)}')
    if 'rms-cam1' in names:
        return names
    if any(unit in inv for unit in _RMS_UNIT_FAMILIES_FIRST) or markers:
        names.insert(0, 'rms-capture')
    return names


def _process_running(pattern: str) -> bool:
    """True if any process command line matches `pattern`."""
    try:
        r = subprocess.run(['pgrep', '-f', pattern],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0 and bool(r.stdout.strip())


def _collect_service_states(base: Path) -> dict[str, str]:
    """Service name → state for every service this station reports on."""
    inv = _unit_inventory()
    markers = _capture_process_markers()
    rms_svcs = _rms_service_names(base, inv, markers)
    numbered = any(svc.startswith('rms-cam') for svc in rms_svcs)
    all_svcs = rms_svcs + list(_ROVIMEN_SERVICES)

    def _probe(svc: str) -> str | None:
        if svc.startswith('rms-'):
            running = _rms_service_running(svc, markers, numbered)
        else:
            running = _process_running(_ROVIMEN_SERVICES.get(svc, svc))
        return _service_state(_resolve_unit(svc, inv), running)

    # The unit inventory answered for every unit up front; what remains is one
    # pgrep per ROVIMEN service. Fan those out across a small thread pool —
    # pure subprocess IO, so threads parallelize without contending on the GIL.
    with ThreadPoolExecutor(max_workers=6) as ex:
        states = list(ex.map(_probe, all_svcs))
    return {svc: state for svc, state in zip(all_svcs, states) if state is not None}


# RMS session directory names (groups: camera, date, time) and a session's
# final FTPdetectinfo files, from rovimen_night.
_SESSION_RE = rovimen_night.SESSION_DIR_RE
_FTP_LINE_RE = re.compile(r'FF_[A-Z0-9]+_(\d{8})_(\d{6})')
_final_ftpdetectinfos = rovimen_night.ftpdetect_files


def _rms_latest_night_summary(cam_dir: Path, station_code: str | None) -> dict:
    """Detections of the camera's most recent night that has an FTPdetectinfo.

    Aggregates every session of that night: RMS often splits one night into
    an evening and a post-midnight session filed under the next date (same
    grouping as detection_lock.process_night). Sessions are grouped by night
    from their names, and only the newest night's sessions are globbed,
    walking back only while a night has no FTPdetectinfo yet (tonight); the
    status refresh used to glob every session of every night kept on disk.
    An archived session also sits in CapturedFiles under the same name and
    counts once (rovimen_night.scan_rms_sessions: ArchivedFiles wins).
    """
    sessions, _listed = rovimen_night.scan_rms_sessions(cam_dir)
    by_night: dict[str, list[Path]] = {}
    for name in sorted(sessions):
        m = _SESSION_RE.match(name)
        by_night.setdefault(_night_date(m.group(2), m.group(3)), []).append(sessions[name])

    for night in sorted(by_night, reverse=True):
        files = [f for session in by_night[night] for f in _final_ftpdetectinfos(session)]
        if not files:
            continue
        total_count = 0
        entries: list[str] = []
        for ftpdet in files:
            try:
                for line in ftpdet.read_text(errors='ignore').splitlines():
                    m = _FTP_LINE_RE.match(line.strip())
                    if m:
                        total_count += 1
                        d, t = m.group(1), m.group(2)
                        entries.append(
                            f"{d[:4]}-{d[4:6]}-{d[6:]} {t[:2]}:{t[2:4]}:{t[4:]} UTC")
            except Exception:
                pass
        entries.sort()
        return {
            'station_code':  station_code,
            'total_count':   total_count,
            'last_entries':  entries[-10:],
            'ftp_file':      files[0].name,
            'night':         night,
            'session_count': len(files),
        }
    return {'station_code': station_code, 'total_count': 0, 'last_entries': []}


# ---------------------------------------------------------------------------
# Chunk listing (/api/chunks)
# ---------------------------------------------------------------------------

# Grace period beyond one segment before the newest clip is considered
# finalized — covers the last clip of a night once capture stops writing.
_INPROGRESS_GRACE_S = 10


def _segment_in_progress(name: str, newest_name: str, mtime: float,
                         now: float, seg_dur: int) -> bool:
    """True if ``name`` is the colour segment ffmpeg is still writing.

    During live capture ffmpeg writes each segment directly to its final
    filename, so the newest file in a night directory keeps growing until the
    next segment starts — serving it yields a truncated 1-7 s clip. A segment
    is in-progress only if it is the newest one *and* was modified within the
    last segment window; the final clip of a finished night ages out and is
    shown normally. After dawn processing state.json's ``ready`` flag makes
    this moot, but during the live night there is no state.json yet.
    """
    if not newest_name or name != newest_name:
        return False
    return (now - mtime) < (seg_dur + _INPROGRESS_GRACE_S)


def list_chunks(cam: str, date: str, *, locked_only: bool = False,
                lock_type_filter: str = '', from_s: str = '', to_s: str = '',
                cfg: dict | None = None) -> dict | list:
    """The /api/chunks payload for one camera-night.

    ``{"morning_done": bool, "chunks": [...]}``, or ``[]`` when the night
    directory does not exist. ``from_s``/``to_s`` are ``HHMM`` bounds on the
    chunk-name time; ``from_s > to_s`` is a window across midnight.

    The cheap filters (time window, ready flag, lock) run on names and
    state.json before anything is stat()ed, and the legacy ``.locked`` /
    ``.reencoded`` sidecars come from one directory listing, so a
    ``locked_only`` poll of a 2,000-chunk night stats the handful of locked
    chunks rather than every one (the pusher asks for three nights of every
    camera every 15 minutes).
    """
    cap_dir = capture_base() / cam / date
    if not cap_dir.exists():
        return []

    if cfg is None:
        cfg = load_config()

    # Load state.json for this night if available (primary source).
    # Falls back to .locked sidecars (legacy) when state.json is absent.
    night_state: dict = {}
    night_chunks_state: dict = {}
    if _STATE_MANAGER_OK:
        try:
            night_state = _flags_manager.load(cam, date, cfg)
            night_chunks_state = night_state.get('chunks', {})
        except Exception:
            pass

    # Flat layout (legacy) and videos/ subdirectory (new coppermind layout).
    mkv_files: list[Path] = []
    sidecars: set[str] = set()
    for d in (cap_dir, cap_dir / 'videos'):
        try:
            names = os.listdir(str(d))
        except OSError:
            continue
        for name in names:
            if name.endswith('_color.mkv'):
                mkv_files.append(d / name)
            elif name.endswith(('_color.mkv.locked', '_color.mkv.reencoded')):
                sidecars.add(str(d / name))

    def _get_lock_info_for(mkv: Path) -> tuple[str | None, str | None, str | None]:
        """Return (lock_type, detection_time, meteor_time) for a chunk.

        Reads from state.json if available, falls back to .locked sidecar.
        """
        cstate = night_chunks_state.get(mkv.name, {})
        lock = cstate.get('lock')
        if lock is not None:
            return lock.get('lock_type'), lock.get('detection_time'), lock.get('meteor_time')
        # Backward compat: .locked sidecar
        if f'{mkv}.locked' in sidecars:
            li = rovimen_lock.get_lock_info(mkv)
            if li is not None:
                return li.get('lock_type'), li.get('detection_time'), li.get('meteor_time')
        return None, None, None

    def _is_reencoded(mkv: Path) -> bool:
        cstate = night_chunks_state.get(mkv.name, {})
        if cstate.get('reencoded', False):
            return True
        return f'{mkv}.reencoded' in sidecars

    def _is_ready(mkv: Path) -> bool:
        cstate = night_chunks_state.get(mkv.name, {})
        if cstate.get('ready', False):
            return True
        # Legacy: if state.json absent, treat all MKVs in the dir as ready
        return not night_chunks_state  # no state.json → show everything

    chunks = []

    # Identify the segment ffmpeg is still writing so it is not served as a
    # truncated clip during the live night (see _segment_in_progress).
    seg_dur = int(cfg.get('segment_duration', 20) or 20)
    newest_name = max(
        (f.name for f in mkv_files if CHUNK_RE.match(f.name)), default='')
    now_ts = time.time()

    for mkv in sorted(mkv_files):
        m = CHUNK_RE.match(mkv.name)
        if not m:
            continue
        time_part = m.group(3)
        hhmm      = time_part[:4]

        if from_s and to_s and from_s > to_s:
            if hhmm < from_s and hhmm > to_s:
                continue
        else:
            if from_s and hhmm < from_s:
                continue
            if to_s and hhmm > to_s:
                continue

        # Only return chunks that are ready (state.json primary; all chunks if legacy)
        if night_chunks_state and not _is_ready(mkv):
            continue

        lock_stem = mkv.stem[:-6]  # strip _color suffix — used for stack filename below
        lock_type, detection_time_str, mt_str = _get_lock_info_for(mkv)
        locked = lock_type is not None
        if locked_only and not locked:
            continue
        if lock_type_filter and lock_type != lock_type_filter:
            continue

        # Never serve the segment ffmpeg is still writing (live capture).
        try:
            st = mkv.stat()
        except OSError:
            continue
        if _segment_in_progress(mkv.name, newest_name, st.st_mtime, now_ts, seg_dur):
            continue

        # The stack name is the same in the stacks/ subdirectory (current
        # layout) and the flat legacy layout; callers resolve the directory.
        stack_name = f'{lock_stem}_stack.webp'
        size_mb = round(st.st_size / (1024 * 1024), 1)

        chunk_dt = _chunk_utc_dt(m.group(2), time_part, cfg)
        det_offset_s = None
        if lock_type == 'detection':
            try:
                off = _detection_offset_s(chunk_dt, mt_str, detection_time_str)
                if off is not None:
                    det_offset_s = round(off, 2)
            except Exception:
                pass

        chunks.append({
            'filename':  mkv.name,
            'time':      chunk_dt.strftime('%H:%M:%S'),
            'stack':     stack_name,
            'size_mb':   size_mb,
            'locked':    locked,
            'lock_type': lock_type,
            'detection_offset_s': det_offset_s,
            'meteor_time': mt_str,
            'reencoded': _is_reencoded(mkv),
        })
    morning_done = bool(night_state.get('morning_done', False))
    return {"morning_done": morning_done, "chunks": chunks}


# ---------------------------------------------------------------------------
# RMS cameras (/api/rms_cameras, /api/live_cameras)
# ---------------------------------------------------------------------------

def _rms_config_paths() -> list[Path]:
    """Every RMS .config on the station, in discovery order.

    Three layouts:
      - ~/RMS_cam{1-8}/.config       (gmn0002/gmn0003 multi-cam style)
      - ~/source/Stations/*/.config  (gmn0005 style)
      - ~/source/RMS/.config         (single-cam source layout, e.g. gmnro16/gmnro18)

    The third layout is easy to miss: single-camera Pis run RMS straight from
    ~/source/RMS with the .config in that root, so without it /api/rms_cameras
    returns [] and the pusher never sends a live thumbnail for the station.
    """
    paths = [BASE / f'RMS_cam{i}' / '.config' for i in range(1, 9)]
    stations_dir = BASE / 'source' / 'Stations'
    if stations_dir.is_dir():
        paths += sorted(stations_dir.glob('*/.config'))
    paths.append(BASE / 'source' / 'RMS' / '.config')
    return [p for p in paths if p.exists()]


def _parse_rms_config(cfg_path: Path) -> dict | None:
    """``{'code', 'cam_ip', 'data_dir'}`` from one RMS .config, or None."""
    station_id = None
    camera_ip = None
    data_dir = None
    try:
        for line in cfg_path.read_text().splitlines():
            line = line.strip()
            if line.startswith('stationID'):
                station_id = line.split(':', 1)[-1].strip()
            elif line.startswith('camera_ip'):
                camera_ip = line.split(':', 1)[-1].strip()
            elif line.startswith('data_dir:'):
                data_dir = line.split(':', 1)[-1].strip()
            elif line.startswith('device') and camera_ip is None:
                # rtsp://192.168.42.11:554/..., or through the relay
                # rtsp://localhost:8554/cam1?camera=192.168.42.11
                camera_ip = rovimen_rms.camera_ip_from_device(line.split(':', 1)[-1])
    except Exception:
        return None
    if station_id:
        return {'code': station_id, 'cam_ip': camera_ip or '', 'data_dir': data_dir}
    return None


def _rms_config_entries() -> list[dict]:
    """One entry per RMS camera code (first config wins)."""
    result: list[dict] = []
    seen: set[str] = set()
    for cfg_path in _rms_config_paths():
        entry = _parse_rms_config(cfg_path)
        if entry and entry['code'] not in seen:
            seen.add(entry['code'])
            result.append(entry)
    return result


# ---------------------------------------------------------------------------
# Live tiles (/api/latest_ff_maxpixel)
# ---------------------------------------------------------------------------

def _ff_maxpixel_to_webp(ff_paths) -> 'io.BytesIO | None':
    """Combine FF maxpixels in memory and return a grayscale WebP as BytesIO. No disk write."""
    return ff_thumbs.ff_maxpixel_to_webp(ff_paths)


def _mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


@lru_cache(maxsize=16)
def _cam_sessions_newest_first(parent: str, parent_mtime_ns: int, cam: str) -> tuple[str, ...]:
    """<cam>'s RMS session dirs under ``parent``, newest first.

    Keyed by the parent's mtime, which changes whenever a session dir is
    created or removed, so the listing is only redone when it can differ
    instead of on every live-thumbnail poll. A change landing inside the same
    timestamp tick as a listing is caught on the next change (worst case: the
    next night's first session shows up one FF late).
    """
    return tuple(sorted(
        (str(d) for d in Path(parent).iterdir()
         if d.is_dir() and d.name.startswith(f'{cam}_')),
        reverse=True,
    ))


@lru_cache(maxsize=32)
def _newest_ff_in_session(session: str, session_mtime_ns: int, cam: str) -> str | None:
    """Lexically newest ``FF_<cam>_*.fits`` in a session dir (None if none).

    Keyed by the session dir's mtime: RMS adds an FF roughly every 10 s, so
    between additions a poll reuses the previous answer instead of re-globbing
    a directory that holds thousands of files by the end of the night.
    """
    ffs = [p.name for p in Path(session).glob(f'FF_{cam}_*.fits')]
    return str(Path(session) / max(ffs)) if ffs else None


def _find_latest_ff(cam: str) -> Path | None:
    """Return the newest FF FITS file for <cam> across all RMS sessions.

    Filenames embed a UTC timestamp (``FF_<cam>_YYYYMMDD_HHMMSS_*.fits``) so
    the lexically-largest name is also the newest. We sort sessions by name
    (also timestamp-prefixed) so we only have to scan the two most recent.
    """
    cfg = load_config()
    station_cfg = cfg.get('stations', {}).get(cam, {})
    # A camera RMS runs on while rovimen treats it as colour-only (``rms:
    # false``, no ``rms_data_path``; GH662S's rms-cam5 since 2026-09-25) has
    # its FF files where its own RMS .config says. rms_data_path is not added
    # to the config for it because color_capture, dawn_process and
    # detection_lock change behaviour on that key.
    rms_path = station_cfg.get('rms_data_path') or next(
        (e['data_dir'] for e in _rms_config_entries()
         if e['code'] == cam and e.get('data_dir')), None)
    if not rms_path:
        return None
    rms_dir = Path(rms_path).expanduser()
    # Newest sessions live in CapturedFiles; ArchivedFiles holds post-processed
    # copies that may lag by a day. Prefer CapturedFiles for "right now".
    for sub in ('CapturedFiles', 'ArchivedFiles'):
        parent = rms_dir / sub if (rms_dir / sub).exists() else rms_dir.parent / sub
        parent_mtime = _mtime_ns(parent)
        if parent_mtime is None:
            continue
        try:
            sessions = _cam_sessions_newest_first(str(parent), parent_mtime, cam)
        except OSError:
            continue
        # Two newest sessions is enough — older ones can't beat anything in
        # the newest by FF timestamp.
        for session in sessions[:2]:
            session_mtime = _mtime_ns(Path(session))
            if session_mtime is None:
                continue
            try:
                newest = _newest_ff_in_session(session, session_mtime, cam)
            except OSError:
                continue
            if newest:
                return Path(newest)
    return None


class _FFRenderFailed(Exception):
    """Raised inside the render cache so a failed render is never memoised."""


@lru_cache(maxsize=16)
def _latest_ff_webp_bytes(ff: str, mtime_ns: int, size: int) -> bytes:
    """Rendered WebP for one FF file version. The dashboard pusher polls every
    camera's live thumbnail every 30 s, but a new FF lands only every ~10 s
    and the pusher cadence means most polls see the same file; re-rendering it
    each time kept station_api near half a core on a Pi 4 (gmnro16). Keyed by
    (path, mtime_ns, size) so a rewritten file re-renders; 16 entries covers
    one current FF per camera with room to spare."""
    buf = _ff_maxpixel_to_webp([Path(ff)])
    if buf is None:
        raise _FFRenderFailed(ff)
    return buf.getvalue()


def _newest_colour_chunks(cam: str) -> list[Path]:
    """<cam>'s colour chunks of its newest night, newest first (at most 2).

    The newest may still be recording; a growing mkv still decodes from its
    start, and the one before it is the fallback.
    """
    root = capture_base() / cam
    try:
        nights = sorted((d for d in root.iterdir()
                         if d.is_dir() and re.fullmatch(r'\d{8}', d.name)), reverse=True)
    except OSError:
        return []
    for night in nights[:2]:
        chunks = sorted(night.glob(f'{cam}_*_color.mkv'), reverse=True)
        if chunks:
            return chunks[:2]
    return []


# A chunk still being recorded grows every second, but its first frame, all
# the tile shows, is fixed once written. Keying the render cache on mtime and
# size, as the FF tiles do, re-ran ffmpeg on every poll of a growing chunk.
# The inode identifies the file (an in-place re-encode is a new inode); the
# size stays in the key only while the file may not yet hold a whole frame.
_FRAME_STABLE_BYTES = 1_000_000


@lru_cache(maxsize=8)
def _colour_frame_webp_bytes(chunk: str, inode: int, size_key: int) -> bytes:
    """First frame of a colour chunk as a grayscale WebP (lossless, full size,
    the same form as the FF maxpixel tiles). Keyed by (path, inode, size_key),
    see _FRAME_STABLE_BYTES."""
    from PIL import Image

    r = subprocess.run(
        ['ffmpeg', '-v', 'error', '-i', chunk, '-frames:v', '1',
         '-vf', 'format=gray', '-f', 'image2pipe', '-vcodec', 'png', 'pipe:1'],
        capture_output=True, timeout=20)
    if r.returncode != 0 or not r.stdout:
        raise _FFRenderFailed(chunk)
    buf = io.BytesIO()
    Image.open(io.BytesIO(r.stdout)).convert('RGB').save(buf, format='webp', lossless=True)
    return buf.getvalue()


def _latest_colour_frame(cam: str) -> tuple[bytes, Path] | None:
    """Live tile for a colour camera without RMS: ``(webp, chunk)`` or None."""
    for chunk in _newest_colour_chunks(cam):
        try:
            st = chunk.stat()
            size_key = st.st_size if st.st_size < _FRAME_STABLE_BYTES else -1
            return _colour_frame_webp_bytes(str(chunk), st.st_ino, size_key), chunk
        except (OSError, subprocess.SubprocessError, _FFRenderFailed):
            continue
    return None


# ---------------------------------------------------------------------------
# Detection index (/api/detections-index)
# ---------------------------------------------------------------------------

_INDEX_DB = BASE / 'rovimen_scripts' / 'detections_index.db'
# Must match detection_indexer.COLS (and dashboard/detection_db.DETECTION_COLS
# minus the VPS-only ``source`` column) so the poller receives every field.
_INDEX_COLS = (
    'cam', 'date', 'ff_file', 'meteor_no',
    'time_utc', 'jd', 'solar_lon', 'shower',
    'mag_apparent', 'mag_absolute', 'duration_s',
    'ra_beg', 'dec_beg', 'ra_end', 'dec_end',
    'ra_radiant', 'dec_radiant', 'radiant_elev',
    'angular_velocity', 'num_segments', 'fps',
    'azim_beg', 'elev_beg', 'azim_end', 'elev_end',
    'chunk_file',
    'x_beg', 'y_beg', 'clip_offset_s',
)

_NIGHT_SKY_COLS = ('cam', 'night', 'n_ff', 'median_stars', 'cloud_fraction', 'updated')


def _read_night_sky(con, since: str) -> list[dict]:
    """Return per-night sky-quality rows for night >= since (or all).

    Tolerant of an older detections_index.db that predates the night_sky table
    (a station updated before the indexer shipped it): the missing-table
    OperationalError is swallowed and an empty list returned, so the endpoint
    stays backward-compatible.
    """
    cols = ', '.join(_NIGHT_SKY_COLS)
    try:
        if since:
            rows = con.execute(
                f'SELECT {cols} FROM night_sky WHERE night >= ? ORDER BY cam, night',
                (since,),
            ).fetchall()
        else:
            rows = con.execute(
                f'SELECT {cols} FROM night_sky ORDER BY cam, night',
            ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# /api/status
# ---------------------------------------------------------------------------

_JANITOR_STATE = BASE / 'logs' / 'janitor_state.json'
# A pressure record older than this belongs to a janitor that stopped running;
# the dashboard's own janitor-stale check covers that case.
_PRESSURE_MAX_AGE_S = 3600


def _janitor_pressure() -> dict | None:
    """The janitor's unrelieved disk-pressure episode, if one is ongoing.

    None when the janitor's last run relieved the disk, when its state is
    missing, or when that state is over an hour old.
    """
    try:
        state = json.loads(_JANITOR_STATE.read_text())
        pressure = state.get('pressure')
        if not isinstance(pressure, dict):
            return None
        last = datetime.fromisoformat(state['last_run'])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
    if (datetime.now(timezone.utc) - last).total_seconds() > _PRESSURE_MAX_AGE_S:
        return None
    return pressure


def collect_status() -> dict:
    """The /api/status payload. Slow on a busy capture disk (du walks);
    station_api serves it stale-while-revalidate."""
    result = {'online': True, 'last_updated': datetime.now(timezone.utc).isoformat()}

    # ── Services ──────────────────────────────────────────────────────
    result['services'] = _collect_service_states(BASE)

    # ── Storage ───────────────────────────────────────────────────────
    # du walks on their own long TTL — see _scan_storage().
    storage = cached('status:storage', _STORAGE_TTL, _scan_storage)
    result['storage'] = storage

    # ── Disk ──────────────────────────────────────────────────────────
    main_device = _get_device(BASE)
    r = subprocess.run(['df', '-BM', str(BASE)], capture_output=True, text=True)
    lines = r.stdout.strip().splitlines()
    if len(lines) >= 2:
        parts = lines[1].split()
        used_mb  = int(parts[2].rstrip('M'))
        total_mb = int(parts[1].rstrip('M'))
        # only count folders on the same device toward other_mb
        main_known_mb = sum(
            v['bytes'] // (1024 * 1024)
            for v in storage.values()
            if v and v.get('device') == main_device
        )
        result['disk'] = {
            'used_mb':  used_mb,
            'total_mb': total_mb,
            'pct':      int(parts[4].rstrip('%')),
            'other_mb': max(0, used_mb - main_known_mb),
            'device':   main_device,
        }

    # ── Extra disks (any mounted block device that isn't root/boot/virtual) ──
    skip_sources = {'tmpfs', 'efivarfs', 'devtmpfs', 'udev', 'overlay', 'none'}
    skip_mounts  = {'/', '/boot', '/boot/efi', '/sys/firmware/efi/efivars',
                    '/mnt/ramdisk'}
    extra_disks  = []
    r2 = subprocess.run(['df', '-BM', '--output=source,size,used,avail,pcent,target'],
                        capture_output=True, text=True)
    for line in r2.stdout.strip().splitlines()[1:]:
        # The mount point is the last column and may contain spaces: a desktop
        # auto-mounts a USB stick labelled "SANDISK USB" at /media/<user>/SANDISK USB,
        # which made /api/status fail on gmnro17 (Câmpenița) on 2026-11-23.
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        src, size, used_s, avail_s, pct_s, mount = parts
        if (src in skip_sources or src.startswith('/dev/loop')
                or mount in skip_mounts or mount.startswith('/boot')
                or mount.startswith('/sys') or mount.startswith('/proc')
                or mount.startswith('/dev/') or mount == '/'):
            continue
        try:
            extra_disks.append({
                'source':   src,
                'mount':    mount,
                'total_mb': int(size.rstrip('M')),
                'used_mb':  int(used_s.rstrip('M')),
                'avail_mb': int(avail_s.rstrip('M')),
                'pct':      int(pct_s.rstrip('%')),
            })
        except ValueError:
            pass
    result['extra_disks'] = extra_disks
    result['disk_pressure'] = _janitor_pressure()
    # Colour capture diverted to the fallback root: the colour disk is missing
    # or unwritable (rovimen_config.capture_root).
    _cfg = load_config()
    result['capture_fallback'] = (
        {'configured': str(rovimen_config.configured_capture_root(_cfg)),
         'active': str(rovimen_config.capture_root(_cfg))}
        if rovimen_config.capture_on_fallback(_cfg) else None
    )

    # ── RMS detections ────────────────────────────────────────────────
    result['rms'] = {
        cam_label: _rms_latest_night_summary(cam_dir, station_code)
        for cam_label, cam_dir, station_code in _rms_camera_dirs()
    }

    # ── Segment health (colour-capture cadence) ───────────────────────
    # Own TTL rather than riding the status SWR window: a stale-while-
    # revalidate read can hand out a value well past 60 s old, and a clip
    # storm has to surface on the first poll after it starts.
    result['segment_health'] = cached(
        'status:segment_health', _SEGMENT_HEALTH_TTL, _collect_segment_health)

    # ── Capture evidence (RMS FF files per camera) ────────────────────
    result['capture'] = cached('status:capture', _CAPTURE_TTL, _collect_capture)

    # ── Bundle integrity (signed-update telemetry) ────────────────────
    result['bundle'] = _bundle_status()
    return result


# ---------------------------------------------------------------------------
# Listings: nights, timelapses, cameras
# ---------------------------------------------------------------------------

def list_nights(cam: str, limit: int | None = None) -> list[str]:
    """Night directories (YYYYMMDD, newest first) that hold clips for cam.

    ``limit`` stops after that many: the names are sorted first, so a caller
    that wants the newest nights (the pusher) does not list the files of a
    year of old ones.
    """
    cap = capture_base() / cam
    if not cap.exists():
        return []

    def _has_mkvs(d: Path) -> bool:
        try:
            return any(f.suffix == '.mkv' for f in d.iterdir() if f.is_file())
        except OSError:
            return False

    names = sorted((d.name for d in _safe_iterdir(cap) if re.match(r'^\d{8}$', d.name)),
                   reverse=True)
    nights: list[str] = []
    for name in names:
        if limit is not None and len(nights) >= limit:
            break
        d = cap / name
        if _safe_is_dir(d) and _has_mkvs(d):
            nights.append(name)
    return nights


def list_timelapses() -> dict:
    """``{cam: [{date, filename, night_stack}, ...]}``, newest night first."""
    cap = capture_base()
    result: dict = {}
    if not cap.exists():
        return result
    for cam_dir in sorted(cap.iterdir()):
        if not cam_dir.is_dir():
            continue
        entries = []
        for date_dir in sorted(cam_dir.iterdir(), reverse=True):
            if not date_dir.is_dir() or not re.match(r'^\d{8}$', date_dir.name):
                continue
            for mp4 in sorted(date_dir.glob('*_timelapse.mp4'), reverse=True):
                stack = date_dir / mp4.name.replace('_timelapse.mp4', '_night_stack.webp')
                entries.append({
                    'date': date_dir.name,
                    'filename': mp4.name,
                    'night_stack': stack.name if stack.exists() else None,
                })
        if entries:
            result[cam_dir.name] = entries
    return result


def list_rms_cameras() -> list[dict]:
    """``[{code, cam_ip}]`` for every RMS instance (see _rms_config_paths)."""
    return [{'code': e['code'], 'cam_ip': e['cam_ip']} for e in _rms_config_entries()]


def list_live_cameras() -> list[dict]:
    """Every camera that can produce a live tile: the RMS cameras, plus the
    colour cameras RMS does not run on (GH662W on gmnro01 (Ghirdoveni)),
    whose tile is a frame from their newest colour chunk."""
    codes = [e['code'] for e in _rms_config_entries()]
    for sid, scfg in load_config().get('stations', {}).items():
        if isinstance(scfg, dict) and scfg.get('color_capture', True) and sid not in codes:
            codes.append(sid)
    return [{'code': c} for c in codes]


def read_detections_index(since: str = '') -> dict:
    """Indexed detections (and per-night sky rows) for dates >= ``since``.

    The /api/detections-index payload: ``ready`` is False when the index
    database does not exist yet or the query failed."""
    if not _INDEX_DB.exists():
        return {'detections': [], 'night_sky': [], 'ready': False}
    try:
        with closing(sqlite3.connect(str(_INDEX_DB), check_same_thread=False)) as con:
            con.execute('PRAGMA busy_timeout=15000')
            con.row_factory = sqlite3.Row
            # Columns the indexer has not migrated in yet read as None, so an
            # older index still feeds the pusher instead of failing the query.
            have = {r[1] for r in con.execute('PRAGMA table_info(detections)').fetchall()}
            cols = ', '.join(c if c in have else f'NULL AS {c}' for c in _INDEX_COLS)
            if since:
                rows = con.execute(
                    f'SELECT {cols} FROM detections WHERE date >= ? ORDER BY date, time_utc',
                    (since,),
                ).fetchall()
            else:
                rows = con.execute(
                    f'SELECT {cols} FROM detections ORDER BY date, time_utc',
                ).fetchall()
            sky_rows = _read_night_sky(con, since)
        return {
            'ready': True,
            'count': len(rows),
            'detections': [dict(r) for r in rows],
            # Per-night sky quality (cloud_fraction, star counts). Rides this
            # same feed so the VPS learns weather state per station without a
            # second endpoint. Absent on an older DB -> empty list.
            'night_sky': sky_rows,
        }
    except Exception as exc:
        logger.error('detections-index query failed: %s', exc)
        return {'detections': [], 'night_sky': [], 'ready': False, 'error': str(exc)}
