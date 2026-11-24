#!/usr/bin/env python3
from __future__ import annotations
"""
Color Video Capture Daemon for RMS Meteor Stations

Captures color RTSP video alongside RMS's grayscale processing pipeline.
Uses inotify to detect FF file activity (night started/stopped) and
runs ffmpeg to write 20-second MKV chunks directly to videocapture_path.

Tee mode (color_tee=True in config.json): instead of spawning ffmpeg with
its own RTSP connection, links raw video segments from RMS's built-in
splitmuxsink (raw_video_save=True in the RMS .config). This halves the
number of RTSP connections per camera, which is required on stations with
10 Mbps ethernet where two concurrent streams exceed the link capacity.

compression_level 0: -c copy (raw, no annotation)
compression_level 1-4: hardware or software encode with rotation, color gains,
                   and OSD annotation bar using wall-clock per-frame timestamps.

Usage:
    python color_capture.py -c config.json
    python color_capture.py -c config.json --test-duration 300
"""

import errno
import os
import sys
import json
import queue as _queue
import shutil
import signal
import logging
import argparse
import threading
import subprocess
import math
import time
from collections import namedtuple
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Try inotify — required on Linux, skip gracefully for dev/testing
try:
    import inotify.adapters
    import inotify.constants
    INOTIFY_AVAILABLE = True
except ImportError:
    INOTIFY_AVAILABLE = False

# Only the events the tee watcher acts on. The library default subscribes to
# IN_ALL_EVENTS, and a splitmuxsink stream writing into a watched directory
# generates enough IN_MODIFY traffic to overflow the kernel event queue —
# which silently drops the IN_CLOSE_WRITE events the watcher actually needs.
_TEE_WATCH_MASK = (
    inotify.constants.IN_CLOSE_WRITE
    | inotify.constants.IN_CREATE
    | inotify.constants.IN_MOVED_TO
) if INOTIFY_AVAILABLE else 0

try:
    import overlay
    _OVERLAY_OK = True
except ImportError:
    _OVERLAY_OK = False

import camera_fps_guard
import flags_manager
import rovimen_config
import rovimen_night

_CHUNK_NAME_RE = rovimen_night.CHUNK_RE
_VIDEO_SEGMENT_RE = rovimen_night.VIDEO_SEGMENT_RE
# Segments stamped before NTP sync (an RTC-less boot or a mid-capture pipeline
# rebuild names them 1970...) carry garbage timestamps: they mis-map detections
# onto 1969/1970 night dirs and, once purged by the janitor, would be
# re-linked forever. Never tee them.
_TEE_MIN_SEGMENT_DATE = '20200101'

# ----------------------------------------------------------------------
# Capture health decision
#
# Kept as a module-level pure function: the restart policy is the part of
# the watchdog worth testing, and a live ffmpeg, a real night directory and
# a real clock are exactly what make it untestable in place.
#
# Liveness is judged by the segment FILENAME advancing and by the newest
# segment staying a sane size — never by its mtime. A camera emitting
# corrupted or frozen DTS timestamps keeps the mtime perfectly fresh while
# the stream is broken: the segment muxer never reaches its cut point, so
# ffmpeg appends to one file forever and touches it on every frame. RO000Y
# wrote such a mega-segment on 20260831 (839 MB, container claiming 20.0 s,
# ~13.4 min of payload) and kept doing it for 2 h 15 min without the
# watchdog ever firing. The mirror-image failure — DTS jumping backwards,
# cutting thousands of ~1 s segments — did not fire it either, because
# files kept appearing. Both are cleared by restarting ffmpeg, which
# resyncs the RTSP session.
# ----------------------------------------------------------------------

# No new segment filename for this long while ffmpeg runs means the muxer is
# not cutting: max(3x segment_duration, floor) so a long segment_duration
# still gets three chances and a short one is not judged in seconds.
CAPTURE_STALL_FLOOR_S = 90
# The newest segment may reach this multiple of the recent median before it
# counts as a mega-segment, ...
CAPTURE_SIZE_MEDIAN_FACTOR = 4
# ... measured over at most this many completed segments, ...
CAPTURE_SIZE_SAMPLE = 10
# ... and never below 2 x segment_duration at this bitrate, which is above
# anything in the fleet. The floor keeps a median skewed small by restart
# stubs from condemning a healthy segment.
CAPTURE_SIZE_FLOOR_BITRATE_BPS = 16000000
# More than this many segments inside one window of filename time is
# impossible at nominal cadence (a 20 s cadence yields 3-4): the muxer is
# cutting on garbage timestamps.
CAPTURE_STORM_WINDOW_S = 60
CAPTURE_STORM_MAX_SEGMENTS = 20
# Floor between two watchdog restarts, so a camera that is simply broken
# gets retried steadily instead of in a tight loop.
CAPTURE_RESTART_MIN_INTERVAL_S = 120

CaptureHealth = namedtuple('CaptureHealth', 'restart kind reason state')

# stacker_max_concurrent 'auto': two realtime stacker lanes on a station with
# at least this many colour cameras AND this many CPU cores, else one. Measured
# 2026-09-24: gmnro04 (4 cams, 6 cores) stacks ~460 chunks/h on one lane but
# captures ~720/h, so the backlog landed on dawn. Pi-class boxes (the HDD
# stations that motivated the serial default) fall below both thresholds.
STACKER_AUTO_MIN_CAMERAS = 4
STACKER_AUTO_MIN_CORES = 6


def watch_tree(inot, root: str, log: logging.Logger, onerror=None) -> int:
    """Add an inotify watch on ``root`` and every directory below it.

    Returns how many watches were added; a directory that cannot be watched
    is logged and skipped so one bad subtree does not stop the rest."""
    watched = 0
    for d, _dirs, _files in os.walk(root, onerror=onerror):
        try:
            inot.add_watch(d)
            watched += 1
        except Exception as e:
            log.warning('chunk-ready: add_watch failed for %s: %s', d, e)
    return watched


def resolve_stacker_workers(setting: object, n_cameras: int,
                            cpu_cores: int | None = None) -> int:
    """Number of realtime stacker lanes for a ``stacker_max_concurrent`` value.

    An explicit positive integer wins. 'auto' (the default, and anything that
    is not a positive integer) gives 2 lanes when the station has
    >= STACKER_AUTO_MIN_CAMERAS colour cameras and >= STACKER_AUTO_MIN_CORES
    CPU cores, else 1.
    """
    if isinstance(setting, bool):
        setting = 'auto'
    if isinstance(setting, (int, float)) and int(setting) >= 1:
        return int(setting)
    if isinstance(setting, str) and setting.strip().isdigit() and int(setting) >= 1:
        return int(setting)
    cores = cpu_cores if cpu_cores is not None else (os.cpu_count() or 1)
    if n_cameras >= STACKER_AUTO_MIN_CAMERAS and cores >= STACKER_AUTO_MIN_CORES:
        return 2
    return 1


def _median(values):
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def _storm_count(pairs, newest_name, floor_name):
    """Count segments cut within CAPTURE_STORM_WINDOW_S of the newest one.

    Measured in filename time, which is self-relative and so needs no clock
    at all. ``floor_name`` excludes segments written before the previous
    storm restart, so a recovered stream stops counting the wreckage it left
    behind instead of triggering restart after restart. ``pairs`` is sorted,
    and chunk names sort chronologically, so walking back from the newest
    stops at the edge of the window instead of scanning the whole night.
    """
    newest_dt = rovimen_night.chunk_start_utc(newest_name, stitched=False)
    if newest_dt is None:
        return 0
    count = 0
    for name, _size in reversed(pairs):
        if floor_name is not None and name <= floor_name:
            break
        seg_dt = rovimen_night.chunk_start_utc(name, stitched=False)
        if seg_dt is None:
            continue
        if (newest_dt - seg_dt).total_seconds() > CAPTURE_STORM_WINDOW_S:
            break
        count += 1
    return count


# Moved to rovimen_night so stdlib-only callers (camera_fps_guard) share it.
_sun_altitude_deg = rovimen_night.sun_altitude_deg


def _sun_capture_action(
    sun_alt: float, night_alt: float, running: bool
) -> Optional[str]:
    """'start' / 'stop' / None for a sun-scheduled (``rms: false``) camera.

    One degree of hysteresis on the stop edge so jitter around the threshold
    cannot flap ffmpeg at dusk and dawn.
    """
    if sun_alt <= night_alt and not running:
        return 'start'
    if sun_alt > night_alt + 1.0 and running:
        return 'stop'
    return None


def chunk_opens_a_night(date_str: str, time_str: str) -> bool:
    """True for a chunk from the evening half of its night (started 12:00 UTC or later).

    Such a chunk means a new night's capture has begun, so any dawn_process
    still running is working on an older night. A morning chunk belongs to the
    night dawn is about to process and says nothing of the sort.
    """
    return rovimen_night.night_of(date_str, time_str) == date_str[:8]


# How long an RMS camera's recorder outlives its last FF file once RMS has
# ended its night (ColorCapture._rms_night_over). The full idle timeout still
# covers a mid-night RMS restart.
RMS_END_GRACE_S = 180


def evaluate_capture_health(segments, now, state=None, segment_duration=20):
    """Decide whether one camera's capture ffmpeg needs a restart.

    ``segments`` is the current night directory as ``(filename, size_bytes)``
    pairs, ``now`` a monotonic timestamp, and ``state`` the dict returned by
    the previous call (``None`` seeds it). Returns ``CaptureHealth(restart,
    kind, reason, state)``: the caller stores the returned state and acts on
    ``restart``. ``kind`` is ``stall``, ``oversize`` or ``storm``.
    """
    if not segment_duration or segment_duration <= 0:
        segment_duration = 20
    st = dict(state or {})
    st.setdefault('newest', None)
    st.setdefault('since', now)
    st.setdefault('last_restart', None)
    st.setdefault('storm_floor', None)

    pairs = sorted((name, size) for name, size in segments)
    if not pairs:
        # Daytime, or ffmpeg only just started: nothing written yet. Not a
        # stall — capture runs only while RMS produces FF files, and treating
        # an empty night directory as a fault would restart-loop all day.
        st['newest'] = None
        st['since'] = now
        return CaptureHealth(False, None, None, st)

    newest_name, newest_size = pairs[-1]
    if newest_name != st['newest']:
        st['newest'] = newest_name
        st['since'] = now

    stall_after = max(segment_duration * 3, CAPTURE_STALL_FLOOR_S)
    age = now - st['since']

    size_cap = int(2 * segment_duration * CAPTURE_SIZE_FLOOR_BITRATE_BPS / 8)
    completed = [size for _name, size in pairs[:-1]][-CAPTURE_SIZE_SAMPLE:]
    if completed:
        size_cap = max(size_cap,
                       int(CAPTURE_SIZE_MEDIAN_FACTOR * _median(completed)))

    # Nominal cadence fits window/segment_duration segments in the window, so
    # a station configured with an unusually short segment_duration must not
    # have its normal output called a storm.
    storm_max = max(CAPTURE_STORM_MAX_SEGMENTS,
                    int(4 * CAPTURE_STORM_WINDOW_S / segment_duration))
    storm = _storm_count(pairs, newest_name, st['storm_floor'])
    storm_hit = storm > storm_max

    kind = None
    reason = None
    if age > stall_after:
        kind = 'stall'
        reason = ('no new segment for %.0fs (threshold %ds) while ffmpeg runs '
                  '— %s is still being appended to'
                  % (age, stall_after, newest_name))
    elif newest_size > size_cap:
        kind = 'oversize'
        reason = ('%s has grown to %.0f MB, cap %.0f MB — the muxer is not '
                  'cutting segments'
                  % (newest_name, newest_size / 1048576.0,
                     size_cap / 1048576.0))
    elif storm_hit:
        kind = 'storm'
        reason = ('%d segments cut within %ds of %s — the stream timestamps '
                  'are corrupted'
                  % (storm, CAPTURE_STORM_WINDOW_S, newest_name))

    if kind is None:
        return CaptureHealth(False, None, None, st)

    last = st['last_restart']
    if last is not None and now - last < CAPTURE_RESTART_MIN_INTERVAL_S:
        # Rate-limited: report healthy so the caller neither restarts nor
        # logs the same reason every 30 s.
        return CaptureHealth(False, None, None, st)

    st['last_restart'] = now
    # Re-seed the filename timer from the restart, not from the doomed
    # segment: the replacement ffmpeg needs a full stall_after to prove
    # itself before the watchdog judges it again.
    st['newest'] = None
    st['since'] = now
    if storm_hit:
        st['storm_floor'] = newest_name
    return CaptureHealth(True, kind, reason, st)


class ColorCapture:
    """Manages color video capture for multiple RMS cameras."""

    # How often the tee reconcile sweep re-walks the RMS video tree.
    TEE_RECONCILE_INTERVAL_S = 60
    # A segment whose mtime moved more recently than this may still be
    # mid-write by splitmuxsink; leave it for the next sweep.
    TEE_RECONCILE_SETTLE_S = 30

    def __init__(self, config_path: str, test_duration: Optional[int] = None):
        with open(config_path) as f:
            self.cfg = json.load(f)

        self.capture_path = rovimen_config.capture_root(self.cfg)
        self.capture_path.mkdir(parents=True, exist_ok=True)

        self.segment_duration = self.cfg.get('segment_duration', 20)
        self.idle_timeout = self.cfg.get('ff_idle_timeout_minutes', 30) * 60
        self.min_disk_gb = self.cfg.get('min_disk_gb_free', 20)
        self.test_duration = test_duration

        self.color_tee = self.cfg.get('color_tee', False)

        # ffmpeg subprocesses keyed by station_id
        self._ffmpeg: dict[str, subprocess.Popen] = {}
        # Tee watcher threads keyed by station_id (used when color_tee=True)
        self._tee_watchers: dict[str, threading.Thread] = {}
        # Per-station stop events for tee watchers (set at dawn to stop the thread)
        self._tee_stop_events: dict[str, threading.Event] = {}
        # Timestamp of the previous linked tee segment, per station — used to
        # measure the real segment cadence RMS is producing.
        self._tee_last_ts: dict[str, datetime] = {}
        # Stations already warned about a tee cadence mismatch (warn once).
        self._tee_cadence_warned: set[str] = set()
        # Per-station set of segment filenames linked once already — the
        # reconcile sweep must not resurrect chunks the janitor purged.
        self._tee_ledger: dict[str, set[str]] = {}
        # Last time we saw an FF file per station
        self._last_ff: dict[str, float] = {}
        # Stations paused due to low disk — suppresses per-FF-file log spam
        self._disk_paused: set[str] = set()
        self._running = False
        self._config_path = config_path
        # Per-station watchdog state carried between health checks
        # (see evaluate_capture_health).
        self._capture_health: dict[str, dict] = {}
        self._ffmpeg_log_fh: dict[str, object] = {}

        # Stacker serialisation — at most `stacker_max_concurrent` stackers
        # run simultaneously so HDD I/O is not saturated by concurrent ffmpeg
        # rawvideo reads (root cause of the inotify watcher crash).
        # Default 'auto': 2 lanes on a 4+ camera, 6+ core station, else 1
        # (fully serial); an explicit integer wins. See resolve_stacker_workers.
        self._stacker_workers: int = resolve_stacker_workers(
            self.cfg.get('stacker_max_concurrent', 'auto'),
            sum(1 for scfg in (self.cfg.get('stations') or {}).values()
                if scfg.get('color_capture', True)),
        )
        self._stacker_queue: _queue.Queue = _queue.Queue()
        # Chunk filenames currently queued or being stacked — prevents duplicate
        # launches when the watcher sees repeated events for the same chunk.
        self._stacker_queued: set[str] = set()
        self._stacker_lock = threading.Lock()

        self._setup_logging()

    def _load_cfg(self) -> None:
        try:
            with open(self._config_path) as f:
                self.cfg = json.load(f)
            new_tee = self.cfg.get('color_tee', False)
            if new_tee != self.color_tee:
                logging.getLogger('color_capture').warning(
                    'color_tee changed in config (%s -> %s) — restart required to take effect',
                    self.color_tee, new_tee,
                )
        except Exception:
            logging.getLogger('color_capture').exception(
                'Config reload failed — keeping previous config'
            )

    def _log_dir(self, sub: Optional[str] = None) -> Path:
        """Resolve (and create) the station log directory, optionally a subdir.

        Three call sites resolved this independently — the daemon's own log,
        ffmpeg's stderr log and the stacker log — so the naming convention
        they share could drift apart. One resolver keeps them together.
        """
        d = rovimen_config.log_dir(self.cfg)
        if sub:
            d = d / sub
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _setup_logging(self):
        log_file = self._log_dir('color_capture') / f'{self._night_date()}.log'

        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s [%(levelname)s] %(message)s',
            handlers=[
                logging.StreamHandler(),
                logging.FileHandler(str(log_file)),
            ],
        )
        self.log = logging.getLogger('color_capture')

    # ------------------------------------------------------------------
    # Night date — boundary at noon UTC so one night = one folder
    # ------------------------------------------------------------------

    @staticmethod
    def _night_date() -> str:
        """Return the 'night date' as YYYYMMDD. Before noon UTC uses yesterday's date."""
        return rovimen_night.current_night()

    # ------------------------------------------------------------------
    # Disk space check
    # ------------------------------------------------------------------

    def _disk_ok(self) -> bool:
        try:
            usage = shutil.disk_usage(str(self.capture_path))
        except OSError as exc:
            self.log.warning('disk_usage failed (%s) — assuming OK to avoid killing capture', exc)
            return True
        free_gb = usage.free / (1024 ** 3)
        if free_gb < self.min_disk_gb:
            self.log.warning('Low disk: %.1f GB free (need %d GB)', free_gb, self.min_disk_gb)
            return False
        return True

    # ------------------------------------------------------------------
    # Tee mode — link RMS raw_video_save segments into color_capture
    # ------------------------------------------------------------------

    @staticmethod
    def _color_name_from_video(video_filename: str) -> str | None:
        """Convert RMS video segment name to color capture name.

        RMS names segments like RO000Z_20260622_234512_123456_video.mkv.
        Returns the color_capture equivalent: RO000Z_20260622_234512_color.mkv.
        """
        m = _VIDEO_SEGMENT_RE.match(video_filename)
        if not m:
            return None
        return f'{m.group(1)}_{m.group(2)}_{m.group(3)}_color.mkv'

    def _rms_video_path(self, station_id: str) -> Path:
        """Return the RMS raw video directory for a station.

        RMS writes splitmuxsink segments to ``<data_dir>/<video_dir>``, and its
        ``video_dir`` default is ``VideoFiles`` (RMS ConfigReader) — nested
        further as ``<year>/<YYYYMMDD-doy>/<...>``, which the watcher walks.
        Overridable for a station whose RMS .config sets a non-default
        ``video_dir``.
        """
        rms_data = Path(self.cfg['stations'][station_id]['rms_data_path'])
        return rms_data / self.cfg.get('rms_video_dir', 'VideoFiles')

    def _check_tee_cadence(self, station_id: str, date_str: str, time_str: str) -> None:
        """Warn once if RMS's real segment length disagrees with segment_duration.

        In tee mode the chunk length on disk is RMS's ``raw_video_duration``,
        but detection_lock and the station API map a detection time onto a
        chunk using ``segment_duration`` from config.json. When the two
        disagree, every detection near a chunk edge is attributed to the wrong
        clip — so surface it loudly instead of mis-mapping in silence.
        """
        seg_dt = rovimen_night.utc_stamp(date_str, time_str)
        if seg_dt is None:
            return
        prev = self._tee_last_ts.get(station_id)
        self._tee_last_ts[station_id] = seg_dt
        if prev is None or station_id in self._tee_cadence_warned:
            return
        gap = (seg_dt - prev).total_seconds()
        # Only judge back-to-back segments: a capture restart or a mid-night gap
        # leaves an arbitrary delta that says nothing about the cadence.
        if not 0 < gap < self.segment_duration * 3:
            return
        if abs(gap - self.segment_duration) < 1:
            return
        self._tee_cadence_warned.add(station_id)
        self.log.warning(
            'color_tee: %s segments arrive every %.0fs but segment_duration is %ds — '
            'set raw_video_duration=%d in the RMS .config so they agree, or detections '
            'near a chunk edge will map onto the wrong clip',
            station_id, gap, self.segment_duration, self.segment_duration)

    def _start_tee_watcher(self, station_id: str) -> None:
        """Start a tee watcher thread for one station (replaces ffmpeg in tee mode)."""
        if not INOTIFY_AVAILABLE:
            self.log.error('inotify not available — cannot start tee watcher for %s', station_id)
            return
        if station_id in self._tee_watchers:
            t = self._tee_watchers[station_id]
            if t.is_alive():
                return
        stop_evt = threading.Event()
        self._tee_stop_events[station_id] = stop_evt
        t = threading.Thread(
            target=self._tee_watcher, args=(station_id, stop_evt),
            name=f'tee-{station_id}', daemon=True,
        )
        t.start()
        self._tee_watchers[station_id] = t
        self.log.info('Tee watcher started for %s', station_id)

    def _stop_tee_watcher(self, station_id: str) -> None:
        """Signal a tee watcher thread to stop."""
        evt = self._tee_stop_events.pop(station_id, None)
        if evt:
            evt.set()
        self._tee_watchers.pop(station_id, None)
        self.log.info('Tee watcher stop requested for %s', station_id)

    @staticmethod
    def _night_date_from_filename(date_str: str, time_str: str) -> str:
        """Derive night date from segment filename timestamp fields.

        Uses the same noon-UTC boundary as _night_date(), but based on the
        timestamp embedded in the filename rather than the current wall-clock.
        This ensures segments processed from a backlog after a crash go to the
        correct date folder.
        """
        return rovimen_night.night_of(date_str, time_str)

    def _tee_link_segment(self, station_id: str, src: Path, *, quiet: bool = False) -> bool:
        """Hardlink one RMS raw segment into color_capture (copy on cross-device).

        Returns True when a new chunk was created. After linking, the
        destination is touched so it emits IN_CLOSE_WRITE and the chunk-ready
        watcher picks it up for flags_manager and stacking (hardlinks alone
        emit only IN_CREATE).
        """
        filename = src.name
        color_name = self._color_name_from_video(filename)
        if not color_name:
            return False

        m = _VIDEO_SEGMENT_RE.match(filename)
        if m.group(2) < _TEE_MIN_SEGMENT_DATE:
            return False

        if filename in self._tee_ledger_set(station_id):
            # Linked once before. If the chunk is gone now, the janitor purged
            # it deliberately — re-linking restarts the purge/relink fight.
            return False

        night = self._night_date_from_filename(m.group(2), m.group(3))

        if not self._disk_ok():
            self.log.warning('Tee %s: disk full, skipping %s', station_id, filename)
            return False

        dst = self.capture_path / station_id / night / color_name
        if dst.exists():
            self._tee_ledger_record(station_id, filename)
            if not quiet:
                self.log.debug('Tee %s: %s already exists, skipping', station_id, dst.name)
            return False
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            os.link(str(src), str(dst))
            open(dst, 'ab').close()
            self._tee_ledger_record(station_id, filename)
            self.log.info('Tee linked %s -> %s', filename, dst.name)
            return True
        except OSError as e:
            if e.errno == errno.EXDEV:
                try:
                    shutil.copy2(str(src), str(dst))
                    self._tee_ledger_record(station_id, filename)
                    self.log.info('Tee copied (cross-device) %s -> %s', filename, dst.name)
                    return True
                except Exception as ce:
                    self.log.warning('Tee copy failed %s: %s', filename, ce)
            else:
                self.log.warning('Tee link failed %s: %s', filename, e)
        return False

    # ------------------------------------------------------------------
    # Tee ledger — segments link at most once, ever
    #
    # The reconcile sweep cannot tell "inotify missed this segment" apart
    # from "the janitor purged this chunk on purpose" by looking at the
    # filesystem: both present as a raw segment without a chunk. The ledger
    # records every segment linked once, so a missing chunk afterwards means
    # a deliberate deletion. Persisted per station across service restarts.
    # ------------------------------------------------------------------

    def _tee_ledger_path(self, station_id: str) -> Path:
        return self.capture_path / station_id / '.tee_linked.log'

    def _tee_ledger_set(self, station_id: str) -> set[str]:
        if not hasattr(self, '_tee_ledger'):
            self._tee_ledger = {}
        return self._tee_ledger.setdefault(station_id, set())

    def _tee_ledger_load(self, station_id: str) -> None:
        """Load the persisted ledger (watcher start / service restart)."""
        try:
            with open(self._tee_ledger_path(station_id)) as f:
                entries = {line.strip() for line in f if line.strip()}
        except OSError:
            return
        self._tee_ledger_set(station_id).update(entries)

    def _tee_ledger_record(self, station_id: str, filename: str) -> None:
        ledger = self._tee_ledger_set(station_id)
        if filename in ledger:
            return
        ledger.add(filename)
        try:
            path = self._tee_ledger_path(station_id)
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a') as f:
                f.write(filename + '\n')
        except OSError as e:
            self.log.warning('tee %s: ledger append failed: %s', station_id, e)

    def _tee_ledger_prune(self, station_id: str, existing: set[str]) -> None:
        """Drop ledger entries whose raw segment RMS retention has deleted.

        Keeps the ledger bounded at roughly video_days_to_keep nights of
        segment names.
        """
        ledger = self._tee_ledger_set(station_id)
        stale = ledger - existing
        if not stale:
            return
        ledger.difference_update(stale)
        try:
            path = self._tee_ledger_path(station_id)
            tmp = path.with_name(path.name + '.tmp')
            tmp.write_text(''.join(name + '\n' for name in sorted(ledger)))
            tmp.replace(path)
        except OSError as e:
            self.log.warning('tee %s: ledger rewrite failed: %s', station_id, e)

    def _tee_reconcile(self, station_id: str, rms_video: Path, add_watch=None) -> int:
        """Walk the RMS video tree and link any closed segment inotify missed.

        The kernel drops events when its inotify queue overflows (gmnmd01 lost
        ~88% of a night's segments this way), so the event stream is only a
        latency optimisation — this sweep is the source of truth. When
        ``add_watch`` is given, every directory encountered is (re)registered,
        which also covers watch registrations lost to the same overflow.
        Segments whose mtime moved within TEE_RECONCILE_SETTLE_S may still be
        mid-write and are left for the next pass. Segments already in the tee
        ledger are skipped: linked once means handled, and a chunk missing
        afterwards was purged by the janitor on purpose.
        """
        linked = 0
        now = time.time()
        ledger = self._tee_ledger_set(station_id)
        seen: set[str] = set()
        for root, _dirs, files in os.walk(str(rms_video)):
            if add_watch is not None:
                add_watch(root)
            for filename in files:
                if not filename.endswith('_video.mkv'):
                    continue
                seen.add(filename)
                if filename in ledger:
                    continue
                src = Path(root) / filename
                try:
                    mtime = src.stat().st_mtime
                except OSError:
                    continue
                if now - mtime < self.TEE_RECONCILE_SETTLE_S:
                    continue
                if self._tee_link_segment(station_id, src, quiet=True):
                    linked += 1
        self._tee_ledger_prune(station_id, seen)
        if linked:
            self.log.warning('Tee reconcile %s: linked %d segment(s) missed by inotify',
                             station_id, linked)
        return linked

    def _tee_watcher(self, station_id: str, stop_event: threading.Event) -> None:
        """Watch RMS video directory and hardlink segments into color_capture.

        Runs as a daemon thread. When RMS writes a raw video segment via its
        GStreamer tee + splitmuxsink, this thread detects the closed file and
        creates a hardlink (or copy on cross-device) in the color_capture
        directory with the standard _color.mkv naming. inotify provides the
        low-latency path; the periodic reconcile sweep guarantees completeness
        when the kernel queue overflows or the thread was down.
        """
        rms_video = self._rms_video_path(station_id)
        backoff = 5
        self._tee_ledger_load(station_id)

        while self._running and not stop_event.is_set():
            if not rms_video.exists():
                self.log.info('Waiting for RMS video dir %s to appear', rms_video)
                if stop_event.wait(timeout=10):
                    return
                continue

            try:
                inot = inotify.adapters.Inotify()
                watched: set[str] = set()

                def _add_watch(root: str) -> None:
                    if root in watched:
                        return
                    try:
                        inot.add_watch(root, mask=_TEE_WATCH_MASK)
                        watched.add(root)
                    except Exception as e:
                        self.log.warning('tee %s: add_watch %s: %s', station_id, root, e)

                # The initial sweep registers all watches and links the backlog
                # from before this thread started (crash, reboot, mid-night
                # service restart).
                self._tee_reconcile(station_id, rms_video, add_watch=_add_watch)
                last_reconcile = time.monotonic()
                self.log.info('Tee watcher for %s watching %d dirs under %s',
                              station_id, len(watched), rms_video)
                backoff = 5

                # No timeout_s here: in this inotify package timeout_s makes
                # the generator TERMINATE after that many quiet seconds, which
                # sent this loop back through the Inotify rebuild + full
                # reconcile + rewatch every ~5 s all night (the 20260813
                # gmnmd01 regression that starved the RMS pipeline).
                # yield_nones already ticks ~1/s for the periodic reconcile.
                for event in inot.event_gen(yield_nones=True):
                    if not self._running or stop_event.is_set():
                        return

                    if time.monotonic() - last_reconcile >= self.TEE_RECONCILE_INTERVAL_S:
                        self._tee_reconcile(station_id, rms_video, add_watch=_add_watch)
                        last_reconcile = time.monotonic()

                    if event is None:
                        continue

                    _, type_names, path, filename = event

                    if 'IN_Q_OVERFLOW' in type_names:
                        self.log.warning('tee %s: inotify queue overflow — events lost, '
                                         'reconcile sweep will recover the segments', station_id)
                        continue

                    if 'IN_ISDIR' in type_names and ('IN_CREATE' in type_names or 'IN_MOVED_TO' in type_names):
                        _add_watch(os.path.join(path, filename))
                        continue

                    if 'IN_CLOSE_WRITE' not in type_names:
                        continue
                    if not filename.endswith('_video.mkv'):
                        continue

                    m = _VIDEO_SEGMENT_RE.match(filename)
                    if m and m.group(2) >= _TEE_MIN_SEGMENT_DATE:
                        self._check_tee_cadence(station_id, m.group(2), m.group(3))
                    self._tee_link_segment(station_id, Path(path) / filename)

            except Exception:
                self.log.exception('Tee watcher for %s crashed, restarting in %ds', station_id, backoff)
                if stop_event.wait(timeout=backoff):
                    return
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # Encoder selection — direct-to-disk with full annotation pipeline
    # ------------------------------------------------------------------

    def _encoder_args(self, station_id: str) -> list[str]:
        """Return ffmpeg video encoder args — always raw copy, no decode/encode at capture."""
        return ['-c:v', 'copy']

    # ------------------------------------------------------------------
    # ffmpeg management
    # ------------------------------------------------------------------

    def _start_ffmpeg(self, station_id: str):
        """Start ffmpeg segment recording for one camera.

        In tee mode (color_tee=True), starts a filesystem watcher instead of
        ffmpeg, linking RMS raw video segments into the color_capture directory.
        """
        if self.color_tee:
            self._start_tee_watcher(station_id)
            return

        if station_id in self._ffmpeg:
            if self._ffmpeg[station_id].poll() is None:
                return  # still running
            del self._ffmpeg[station_id]  # dead process — fall through to restart
        self._load_cfg()

        if not self._disk_ok():
            if station_id not in self._disk_paused:
                self.log.error('Disk full — pausing capture for %s', station_id)
                self._disk_paused.add(station_id)
            return

        station = self.cfg['stations'][station_id]
        # Prefer capture_rtsp when set: on bandwidth-starved sites (slow camera
        # links) the camera is fronted by a local RTSP relay so RMS and color
        # capture share a single upstream pull. camera_rtsp stays the real
        # camera URL (used by camera-control tooling); capture_rtsp points at
        # the loopback relay. Falls back to camera_rtsp when no relay is used.
        rtsp_url = station.get('capture_rtsp') or station['camera_rtsp']
        date_str = self._night_date()

        out_dir = self.capture_path / station_id / date_str
        out_dir.mkdir(parents=True, exist_ok=True)
        # ffmpeg's segment muxer expands this pattern with -strftime, which calls
        # localtime(). The chunk name is the only timestamp the rest of the
        # pipeline has: detection_lock, detection_indexer and the stacker all
        # parse it as UTC. On a station whose system timezone is not UTC the
        # names silently shift by the offset, so locks land on the chunk from
        # <offset> hours earlier and early-evening detections match no chunk at
        # all. TZ=UTC in the ffmpeg environment (below) pins the expansion to
        # UTC no matter how the host clock is configured.
        pattern = str(out_dir / f'{station_id}_%Y%m%d_%H%M%S_color.mkv')

        cmd = [
            'ffmpeg',
            '-hide_banner',
            '-loglevel', 'warning',
            '-rtsp_transport', 'tcp',
            '-i', rtsp_url,
            '-c:v', 'copy',
            '-an',
            '-f', 'segment',
            '-segment_time', str(self.segment_duration),
            '-reset_timestamps', '1',
            '-strftime', '1',
            '-segment_format', 'matroska',
            pattern,
        ]

        self.log.info('Starting ffmpeg for %s: %s', station_id, ' '.join(cmd[:10]) + ' ...')

        fh = None
        try:
            # Per-night, like the daemon's own log (see _setup_logging). The
            # name is resolved at capture start and capture starts once per
            # night on first FF activity, so each night gets its own file while
            # a watchdog restart mid-night reopens that same night's file in
            # append mode instead of losing the earlier stderr. The single
            # ffmpeg_<CAM>.log this replaces was opened once and appended
            # forever: stations were carrying 300 MB+, 1.9M-line files that ate
            # capture headroom (log_path shares a filesystem with the video data
            # on most stations) and buried live errors under months of old ones.
            ffmpeg_log = (
                self._log_dir('color_capture') /
                f'ffmpeg_{station_id}_{self._night_date()}.log'
            )
            fh = open(ffmpeg_log, 'a')
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=fh,
                env={**os.environ, 'TZ': 'UTC'},
            )
            self._ffmpeg_log_fh[station_id] = fh
            self._ffmpeg[station_id] = proc
            self._last_ff[station_id] = time.monotonic()
            self.log.info('ffmpeg started for %s (pid %d)', station_id, proc.pid)
        except FileNotFoundError:
            if fh:
                fh.close()
            self.log.error('ffmpeg not found — is it installed?')
        except Exception as e:
            if fh:
                fh.close()
            self.log.error('Failed to start ffmpeg for %s: %s', station_id, e)

    def _stop_ffmpeg(self, station_id: str):
        """Gracefully stop ffmpeg for one camera."""
        proc = self._ffmpeg.pop(station_id, None)
        if proc is None:
            return
        fh = getattr(self, '_ffmpeg_log_fh', {}).pop(station_id, None)
        if fh:
            try:
                fh.close()
            except Exception:
                pass
        self.log.info('Stopping ffmpeg for %s (pid %d)', station_id, proc.pid)
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.log.warning('ffmpeg for %s did not die after kill', station_id)

    def _stop_all_ffmpeg(self):
        for sid in list(self._ffmpeg):
            self._stop_ffmpeg(sid)
        for sid in list(self._tee_watchers):
            self._stop_tee_watcher(sid)

    def _night_segments(self, station_id: str) -> list:
        """Return tonight's chunks as ``(filename, size_bytes)`` pairs.

        Deliberately no mtime: a stream stuck on one mega-segment refreshes
        that mtime on every frame, so it is not evidence of anything (see
        evaluate_capture_health). Files that vanish between the listing and
        the stat (janitor, midnight sweep) are simply skipped.
        """
        out_dir = self.capture_path / station_id / self._night_date()
        found = []
        for path in out_dir.glob('*_color.mkv'):
            try:
                size = path.stat().st_size
            except OSError:
                continue
            found.append((path.name, size))
        return found

    def _check_ffmpeg_health(self):
        """Restart ffmpeg if it died, stopped cutting segments, or is writing
        a stream whose timestamps are corrupted.

        Skipped entirely in tee mode — tee watchers are passive and self-recovering.
        """
        if self.color_tee:
            return
        for sid in list(self._ffmpeg):
            proc = self._ffmpeg[sid]
            if proc.poll() is not None:
                self.log.warning('ffmpeg for %s exited (code %s), restarting', sid, proc.returncode)
                del self._ffmpeg[sid]
                self._start_ffmpeg(sid)
                continue

            health = evaluate_capture_health(
                self._night_segments(sid),
                time.monotonic(),
                self._capture_health.get(sid),
                segment_duration=self.segment_duration,
            )
            self._capture_health[sid] = health.state
            if not health.restart:
                continue
            # A stall is routine (a dropped RTSP session); oversize and storm
            # mean the camera is emitting corrupted timestamps and the video
            # already written this night is broken — say so at error level so
            # it surfaces in the nightly log review.
            log = self.log.warning if health.kind == 'stall' else self.log.error
            log('ffmpeg for %s unhealthy [%s]: %s — restarting capture',
                sid, health.kind, health.reason)
            self._stop_ffmpeg(sid)
            self._start_ffmpeg(sid)

    # ------------------------------------------------------------------
    # inotify-based FF file watching
    # ------------------------------------------------------------------

    def _color_stations(self) -> dict:
        """Stations that should get colour capture.

        A camera opts out with ``"color_capture": false`` in its station block.
        Used where the capture_path has no room for every camera's colour video
        (a night is ~25-30 GB per camera), so colour runs on a chosen subset
        while every other service keeps seeing the full station list.
        """
        return {
            sid: scfg for sid, scfg in self.cfg['stations'].items()
            if scfg.get('color_capture', True)
        }

    def _sun_scheduled_stations(self) -> dict:
        """Colour stations captured on a sun schedule instead of FF triggers.

        A camera with ``rms: false`` (the GH662x spectro units on gmnro01)
        produces no FF files, so the FF watcher can never start it — before
        this existed those cameras only recorded thanks to hand-placed
        station-side hacks that every bundle update clobbered (they captured
        zero chunks on 20260916). They record whenever the sun is below
        ``night_sun_alt`` (top-level config key, default -6 deg).
        """
        return {
            sid: scfg for sid, scfg in self._color_stations().items()
            if not scfg.get('rms', True)
        }

    def _schedule_sun_captures(self) -> None:
        """Open/close the capture window for sun-scheduled cameras."""
        stations = self._sun_scheduled_stations()
        if not stations:
            return
        night_alt = float(self.cfg.get('night_sun_alt', -6.0))
        now = time.monotonic()
        utc_now = datetime.now(timezone.utc)
        sun_alts: dict = {}
        for sid, scfg in stations.items():
            # Same fallback as the colour pipeline: the camera's own
            # latitude/longitude, the station's, then overlay.coords. A station
            # whose location lives only in overlay.coords used to get the moon
            # boost in colour while its sun-scheduled cameras never started.
            loc = rovimen_config.station_location(self.cfg, scfg)
            if loc is None:
                self.log.warning(
                    'sun-scheduled camera %s needs latitude/longitude or overlay.coords '
                    'in config', sid,
                )
                continue
            if loc not in sun_alts:
                sun_alts[loc] = _sun_altitude_deg(loc[0], loc[1], utc_now)
            sun_alt = sun_alts[loc]
            running = (
                sid in self._ffmpeg
                or (self.color_tee and sid in self._tee_watchers
                    and self._tee_watchers[sid].is_alive())
            )
            if running:
                # No FF files ever bump these cameras — keep the idle reaper
                # from killing a healthy sun-scheduled capture mid-night.
                self._last_ff[sid] = now
            action = _sun_capture_action(sun_alt, night_alt, running)
            if action == 'start' and sid not in self._disk_paused:
                self.log.info(
                    'Sun at %.1f deg (night below %.1f) — starting capture for %s',
                    sun_alt, night_alt, sid,
                )
                self._start_ffmpeg(sid)
                self._last_ff[sid] = now
            elif action == 'stop':
                self.log.info(
                    'Sun at %.1f deg — capture window closed for %s',
                    sun_alt, sid,
                )
                self._stop_ffmpeg(sid)

    def _get_station_for_ff(self, ff_filename: str) -> Optional[str]:
        """Extract station ID from FF filename like FF_RO000A_2026...fits."""
        parts = ff_filename.split('_')
        if len(parts) >= 2 and ff_filename.startswith('FF_'):
            candidate = parts[1]
            if candidate in self._color_stations():
                return candidate
        return None

    def _watch_ff_files(self):
        """Watch all station CapturedFiles dirs for FF activity via inotify."""
        if not INOTIFY_AVAILABLE:
            self.log.error('inotify not available — install with: pip install inotify')
            return

        notifier = inotify.adapters.Inotify()

        # Watch each station's CapturedFiles directory
        watched_paths: dict[str, str] = {}  # path -> station_id
        for station_id, station_cfg in self._color_stations().items():
            # Colour-only cameras (rms: false, e.g. the GH662x spectro units)
            # produce no FF files, carry no rms_data_path, and are captured off
            # the RTSP tee rather than triggered by RMS detections. Skip them
            # here instead of KeyError-ing the whole daemon down.
            rms_data_path = station_cfg.get('rms_data_path')
            if not rms_data_path:
                continue
            captured = Path(rms_data_path) / 'CapturedFiles'
            captured.mkdir(parents=True, exist_ok=True)
            notifier.add_watch(
                str(captured),
                mask=(inotify.constants.IN_CLOSE_WRITE
                      | inotify.constants.IN_CREATE
                      | inotify.constants.IN_MOVED_TO),
            )
            watched_paths[str(captured)] = station_id
            self.log.info('Watching %s for station %s', captured, station_id)

            for sub in captured.iterdir():
                if sub.is_dir():
                    notifier.add_watch(
                        str(sub),
                        mask=inotify.constants.IN_CLOSE_WRITE | inotify.constants.IN_MOVED_TO,
                    )
                    watched_paths[str(sub)] = station_id
                    self.log.info('Watching subdir %s', sub)

        self.log.info('inotify watching %d paths', len(watched_paths))

        last_health_check = time.monotonic()
        last_cfg_reload = time.monotonic()
        start_time = time.monotonic()

        while self._running:
            for event in notifier.event_gen(timeout_s=5, yield_nones=True):
                if not self._running:
                    break

                if self.test_duration and (time.monotonic() - start_time) > self.test_duration:
                    self.log.info('Test duration reached (%ds), stopping', self.test_duration)
                    self._running = False
                    break

                now = time.monotonic()

                if event is not None:
                    (_, type_names, path, filename) = event

                    if 'IN_CREATE' in type_names or 'IN_ISDIR' in type_names:
                        new_path = os.path.join(path, filename)
                        if os.path.isdir(new_path):
                            station_id = watched_paths.get(path)
                            if station_id:
                                notifier.add_watch(
                                    new_path,
                                    mask=(inotify.constants.IN_CLOSE_WRITE
                                          | inotify.constants.IN_MOVED_TO),
                                )
                                watched_paths[new_path] = station_id
                                self.log.info('Now watching new subdir: %s', new_path)

                    if filename and filename.startswith('FF_') and filename.endswith('.fits'):
                        if 'IN_CLOSE_WRITE' in type_names or 'IN_MOVED_TO' in type_names:
                            station_id = self._get_station_for_ff(filename)
                            if station_id is None:
                                station_id = watched_paths.get(path)
                            if station_id:
                                self.log.info('FF activity: %s (%s)', filename, station_id)
                                self._last_ff[station_id] = now
                                already_running = (
                                    station_id in self._ffmpeg
                                    or (self.color_tee and station_id in self._tee_watchers
                                        and self._tee_watchers[station_id].is_alive())
                                )
                                if not already_running and station_id not in self._disk_paused:
                                    self.log.info('Night started for %s — starting capture', station_id)
                                    self._start_ffmpeg(station_id)

                # Config reload (every 60s)
                if now - last_cfg_reload > 60:
                    last_cfg_reload = now
                    self._load_cfg()

                # Periodic checks (every 30s)
                if now - last_health_check > 30:
                    last_health_check = now
                    self._check_ffmpeg_health()
                    self._schedule_sun_captures()

                    # Resume any disk-paused stations if space has recovered
                    for sid in list(self._disk_paused):
                        if self._disk_ok():
                            self.log.info('Disk recovered — resuming capture for %s', sid)
                            self._disk_paused.discard(sid)
                            self._start_ffmpeg(sid)

                    self._reap_idle_captures(now)

    def _reap_idle_captures(self, now: float) -> None:
        """Stop the recorders of cameras that have written no FF file for a while.

        *now* is ``time.monotonic()``, the clock ``_last_ff`` is kept on.
        RMS_END_GRACE_S after the last FF file once RMS has ended its night,
        else after the full ``ff_idle_timeout_minutes``: that 30-min tail
        recorded past the 04:00 dawn run on every station, and the long
        timeout is only needed to ride out a mid-night RMS restart.
        """
        for sid in list(self._ffmpeg):
            why = self._idle_stop_reason(sid, now)
            if why:
                self.log.info('%s for %s — stopping capture', why, sid)
                self._stop_ffmpeg(sid)

        for sid in list(self._tee_watchers):
            why = self._idle_stop_reason(sid, now)
            if why:
                self.log.info('%s for %s — stopping tee watcher', why, sid)
                self._stop_tee_watcher(sid)

    def _idle_stop_reason(self, station_id: str, now: float) -> Optional[str]:
        idle = now - self._last_ff.get(station_id, 0)
        if idle > self.idle_timeout:
            return 'No FF activity in %d min (dawn?)' % (self.idle_timeout // 60)
        if idle > RMS_END_GRACE_S and self._rms_night_over(station_id):
            return 'RMS ended its night, no FF file in %d s' % idle
        return None

    def _rms_night_over(self, station_id: str) -> bool:
        """True once RMS has ended this camera's night.

        Either the sun is above ``night_sun_alt`` (+1 deg, as for the
        sun-scheduled cameras) at the camera, or RMS's own log says capture
        ended ("Ending capture", or idle until its next night). RMS ends its
        night with the sun near -9 deg, earlier than the sun test, so the log
        is the faster signal; the sun test covers an RMS whose log cannot be
        found. Mid-night neither holds, so an RMS restart still gets the full
        idle timeout. Never raises.
        """
        scfg = (self.cfg.get('stations') or {}).get(station_id) or {}
        loc = rovimen_config.station_location(self.cfg, scfg)
        if loc is not None:
            night_alt = float(self.cfg.get('night_sun_alt', -6.0))
            if _sun_altitude_deg(loc[0], loc[1], datetime.now(timezone.utc)) > night_alt + 1.0:
                return True
        try:
            for _pid, p in camera_fps_guard.rms_process_pids():
                cam = camera_fps_guard.parse_rms_config(p.config)
                if cam is None or cam.code != station_id:
                    continue
                log = camera_fps_guard.latest_log(cam)
                if log is None:
                    return False
                state = camera_fps_guard.rms_capture_state(camera_fps_guard._tail_lines(log))
                return state in ('ended', 'waiting')
        except OSError as e:
            self.log.debug('RMS state check for %s failed: %s', station_id, e)
        return False

    # ------------------------------------------------------------------
    # Stacker worker — serialises stacker launches to protect HDD I/O
    # ------------------------------------------------------------------

    def _stacker_worker(self) -> None:
        """Pull chunks from the stacker queue and run them one at a time.

        Running multiple stackers concurrently causes all of them to read
        large MKV files from the HDD simultaneously, which stalls the disk
        for 30-60 s, blocks the inotify event loop, and crashes the watcher —
        leaving chunks with ready=False and no real-time stacks.

        With _stacker_workers=1 all stackers are fully serial. Each one reads
        sequentially from the HDD and finishes in ~10-15 s. The 'auto' default
        only gives 2 lanes to 4+ camera, 6+ core stations (x86 with SSD/NVMe
        in this fleet); set stacker_max_concurrent=1 explicitly on an HDD box
        that nonetheless crosses those thresholds.
        """
        while self._running:
            try:
                station_id, chunk_path, cmd = self._stacker_queue.get(timeout=2)
            except _queue.Empty:
                continue
            try:
                # Reopened per stacker run, so the per-night name costs nothing
                # and gives the same bounded, greppable file the daemon and
                # ffmpeg logs now have (the flat stacker.log had grown to
                # 1.9M lines on some stations).
                stacker_log = self._log_dir() / f'stacker_{self._night_date()}.log'
                stacker_cfg = self.cfg.get('services', {}).get('stacker', {})
                stacker_cfg = stacker_cfg if isinstance(stacker_cfg, dict) else {}
                nice_val = int(stacker_cfg.get('nice', 19))
                # Priority from the argv, not a preexec_fn: this daemon is
                # multi-threaded, and code run in the forked child before exec
                # can deadlock it (a deadlocked child cost the 120 s timeout).
                # ionice (util-linux) is on every station: the dawn cron runs
                # under it.
                argv = ['ionice', '-c', '3', 'nice', '-n', str(nice_val), *cmd]

                with open(stacker_log, 'a') as lf:
                    proc = subprocess.Popen(argv, stdout=lf, stderr=lf)
                    try:
                        proc.wait(timeout=120)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                        self.log.warning('Stacker timed out for %s — killed', chunk_path.name)
                self.log.debug('Stacker finished for %s', chunk_path.name)
            except Exception as e:
                self.log.warning('Stacker failed for %s: %s', chunk_path.name, e)
            finally:
                with self._stacker_lock:
                    self._stacker_queued.discard(chunk_path.name)
                self._stacker_queue.task_done()

    # ------------------------------------------------------------------
    # Chunk ready watcher — replaces .ready sidecar with flags_manager
    # ------------------------------------------------------------------

    def _stop_stale_dawn(self) -> None:
        """Kill any stale dawn_process: a new night's capture has started, so
        morning processing is no longer safe to continue.

        Called only for a chunk that opens a night (chunk_opens_a_night). It
        used to run on every closed chunk, and the 30-min idle tail of RMS
        cameras' recordings after RMS ended its night closed chunks into the
        04:00 and 04:15 dawn runs, killing both within ~15 s on 17 stations
        every morning. Dawn now waits for colour capture to finish instead
        (dawn_process._colour_recording), and the 13:15 UTC deadline stops
        every automatic run long before the fleet's earliest sunset (14:19
        UTC), so what is left for this guard is a manual ``--date`` run, which
        has no deadline, running into the evening.
        """
        try:
            r = subprocess.run(['pgrep', '-f', 'dawn_process.py'],
                               capture_output=True, text=True)
            for pid in r.stdout.split():
                try:
                    os.kill(int(pid), signal.SIGTERM)
                    self.log.info('Sent SIGTERM to stale dawn_process pid %s', pid)
                except ProcessLookupError:
                    pass
        except Exception as e:
            self.log.debug('dawn_process check failed: %s', e)

    def _chunk_ready_watcher(self) -> None:
        """Watch capture_path for closed MKV segments; call flags_manager.mark_ready()
        and enqueue the chunk for real-time stacking.

        Uses a hand-rolled recursive watch (Inotify + os.walk) instead of
        InotifyTree so a single unreadable subdir (e.g. EIO from NTFS index
        corruption) only loses that subtree instead of crashing the whole
        watcher in a restart loop.
        """
        if not INOTIFY_AVAILABLE:
            self.log.warning('inotify not available — chunk-ready marking disabled')
            return
        backoff = 5
        while self._running:
            try:
                inot = inotify.adapters.Inotify()

                def _onwalkerr(exc: OSError) -> None:
                    self.log.warning(
                        'chunk-ready: skipping unreadable %s: %s',
                        getattr(exc, 'filename', '?'), exc,
                    )

                watched = watch_tree(inot, str(self.capture_path), self.log, _onwalkerr)
                self.log.info(
                    'Chunk-ready watcher watching: %s (%d dirs)',
                    self.capture_path, watched,
                )
                backoff = 5
                for event in inot.event_gen(yield_nones=True):
                    if not self._running:
                        return
                    if event is None:
                        continue
                    _, type_names, path, filename = event
                    # Auto-watch newly-created subdirectories so this loop
                    # behaves like InotifyTree did, including new YYYYMMDD
                    # date dirs created at midnight.
                    if 'IN_ISDIR' in type_names and 'IN_CREATE' in type_names:
                        # Walk the new directory, not just watch it: a new
                        # camera's ffmpeg creates <CAM>/<YYYYMMDD> in one go, so
                        # the date dir exists before this watch does and its
                        # IN_CREATE is never seen (RO22XC on gmnro22 went a
                        # whole evening unstacked, 2026-10-18).
                        new_dir = os.path.join(path, filename)
                        n = watch_tree(inot, new_dir, self.log, _onwalkerr)
                        self.log.debug(
                            'chunk-ready: added %d watch(es) under new dir %s', n, new_dir,
                        )
                        continue
                    if 'IN_CLOSE_WRITE' not in type_names:
                        continue
                    if not filename.endswith('_color.mkv'):
                        continue
                    m = _CHUNK_NAME_RE.match(filename)
                    if not m:
                        continue
                    station_id = m.group(1)
                    date_str, time_str = m.group(2), m.group(3)
                    # Night date: before noon UTC → previous day's folder
                    night_str = rovimen_night.night_of(date_str, time_str)
                    try:
                        flags_manager.mark_ready(station_id, night_str, filename, self.cfg)
                        self.log.debug('flags_manager.mark_ready: %s', filename)
                    except Exception as e:
                        self.log.warning('mark_ready failed for %s: %s', filename, e)
                        continue

                    if chunk_opens_a_night(date_str, time_str):
                        self._stop_stale_dawn()

                    # Enqueue for real-time stacking only if both stacker and realtime are enabled.
                    # If stacker is enabled but realtime is not, stacking happens in the morning sweep.
                    svc_stacker = self.cfg.get('services', {}).get('stacker', {})
                    stacker_cfg = svc_stacker if isinstance(svc_stacker, dict) else {}
                    if not stacker_cfg.get('enabled', True) or not stacker_cfg.get('realtime', True):
                        continue

                    # Deduplicated so a watcher restart cannot double-queue the same chunk.
                    mkv = Path(path) / filename
                    cmd = [sys.executable,
                           str(Path(__file__).parent / 'stacker.py'),
                           '--chunk', str(mkv),
                           '-c', self._config_path]
                    with self._stacker_lock:
                        if filename not in self._stacker_queued:
                            self._stacker_queued.add(filename)
                            self._stacker_queue.put((station_id, mkv, cmd))
                            self.log.debug(
                                'Queued stacker for %s (queue depth %d)',
                                filename, self._stacker_queue.qsize(),
                            )
            except Exception:
                self.log.exception('Chunk-ready watcher crashed — restarting in %ds', backoff)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)

    # ------------------------------------------------------------------
    # Main entry
    # ------------------------------------------------------------------

    # How often to re-resolve the capture root (rovimen_config.capture_root).
    ROOT_CHECK_S = 60

    def _capture_root_watch(self) -> None:
        """Stop the daemon when the capture root changes under it: the colour
        disk vanished (now on the fallback root) or came back. main() exits
        non-zero, so systemd (Restart=on-failure) starts it again on the right
        disk. Without this a daemon that started a moment before a slow disk
        mounted would write to the fallback all night."""
        while self._running:
            time.sleep(self.ROOT_CHECK_S)
            try:
                root = rovimen_config.capture_root(self.cfg)
            except Exception:
                continue
            if root != self.capture_path:
                self.log.warning('Capture root changed %s -> %s; restarting onto it',
                                 self.capture_path, root)
                self.root_changed = True
                self._running = False
                return

    def run(self):
        """Run the capture daemon."""
        self.log.info('Color capture daemon starting')
        colour = self._color_stations()
        self.log.info('Stations: %s', list(colour.keys()))
        skipped = [s for s in self.cfg['stations'] if s not in colour]
        if skipped:
            self.log.info('Colour disabled for: %s (color_capture=false)', skipped)
        self.log.info('Segment duration: %ds', self.segment_duration)
        self.log.info('Idle timeout: %d min, min disk: %d GB',
                      self.idle_timeout // 60, self.min_disk_gb)
        self.log.info('capture_path=%s  stacker_workers=%d',
                      self.capture_path, self._stacker_workers)
        # Chunk names are always written in UTC (see the ffmpeg env below), but a
        # non-UTC host clock still misleads anything reading the wall clock, and
        # it means chunks written before this fix carry the local offset.
        offset = -time.timezone if time.daylight == 0 else -time.altzone
        if offset != 0:
            self.log.warning(
                'host timezone is UTC%+.1fh, fleet standard is UTC — chunk names '
                'are pinned to UTC here, but any chunk written before this '
                'version is stamped in local time', offset / 3600.0)
        if self.color_tee:
            self.log.info('TEE MODE enabled — will link RMS raw video segments instead of spawning ffmpeg')
            for sid in colour:
                rms_vid = self._rms_video_path(sid)
                if not rms_vid.exists():
                    self.log.warning(
                        'color_tee: RMS video dir %s does not exist for %s — '
                        'ensure raw_video_save=True and raw_video_duration=20 in the RMS .config',
                        rms_vid, sid,
                    )

        self._running = True

        def _signal_handler(sig, frame):
            self.log.info('Signal %s received, shutting down', sig)
            self._running = False

        signal.signal(signal.SIGTERM, _signal_handler)
        signal.signal(signal.SIGINT, _signal_handler)

        for i in range(self._stacker_workers):
            threading.Thread(
                target=self._stacker_worker,
                name=f'stacker-worker-{i}',
                daemon=True,
            ).start()

        threading.Thread(target=self._chunk_ready_watcher, name='chunk-ready-watcher', daemon=True).start()
        threading.Thread(target=self._capture_root_watch, name='capture-root-watch', daemon=True).start()

        try:
            self._watch_ff_files()
        except Exception as e:
            self.log.error('Fatal error: %s', e, exc_info=True)
        finally:
            self._stop_all_ffmpeg()
            self.log.info('Color capture daemon stopped')


def main():
    parser = argparse.ArgumentParser(description='Color video capture daemon for RMS')
    parser.add_argument('-c', '--config', required=True, help='Path to config JSON')
    parser.add_argument('--test-duration', type=int, default=None,
                        help='Run for N seconds then exit (testing)')
    args = parser.parse_args()

    daemon = ColorCapture(args.config, test_duration=args.test_duration)
    daemon.run()
    if getattr(daemon, 'root_changed', False):
        sys.exit(75)   # non-zero: systemd restarts it on the new capture root


if __name__ == '__main__':
    main()
