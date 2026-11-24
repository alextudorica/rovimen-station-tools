#!/usr/bin/env python3
"""archive_upload.py — Station-side archive uploader.

Codename: karbranth

Ships the station's archival files (locked meteor clips, chunk stacks, night products, FF thumbnails, state.json and the
RMS detection files) to the VPS over the HTTPS ingest blob endpoint, and
records each confirmed upload in the night's state.json so the janitor knows
which files are safe to delete.

Reads configuration from the "archive" block in config.json:
  enabled, upload_meteors, upload_stacks, upload_timelapses, upload_rms,
  interval_minutes, stagger_* (see stagger_hold_s)

A clip's faststart .mp4 copy is no longer made or sent here: the VPS derives it
from the uploaded .mkv at ingest (dashboard/faststart_sibling.py), so each
bitstream crosses the uplink once. ``publish_faststart_mp4`` is ignored.

Run by the archive-upload user unit as a daemon, by dawn_process one night at a
time (run_night) and as a backfill sweep (run_once), or standalone:
    python archive_upload.py -c config.json            # single pass
    python archive_upload.py -c config.json --daemon   # long-lived loop
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import signal
import socket
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path

from dataclasses import dataclass, field

import flags_manager
import rovimen_config
import rovimen_night
import encoder

# The blob transport and its retry policy live in ingest_blob; these names are
# re-exported for callers and tests that still reach them through here.
from ingest_blob import (
    BACKOFF_CAP_S as _BLOB_BACKOFF_CAP_S,
    MAX_ATTEMPTS as _BLOB_MAX_ATTEMPTS,
    NO_RESPONSE as _BLOB_NO_RESPONSE,
    PERMANENT_RETRY_AFTER_S as _BLOB_PERMANENT_RETRY_AFTER_S,
    RATE_LIMIT_MAX_WAITS as _BLOB_RATE_LIMIT_MAX_WAITS,
    RATE_LIMIT_WAIT_S as _BLOB_RATE_LIMIT_WAIT_S,
    SPOOL_FULL_PAUSE_S as _BLOB_SPOOL_FULL_PAUSE_S,
    BlobSender,
    post_blob,
    post_blob_status,
)

# Archival bytes now travel the HTTPS ingest channel (§9): the station POSTs each
# file to the VPS blob endpoint with its ingest key — the SAME channel + key the
# pusher uses — so the station holds NO SSH key. Reuse the pusher's target/key
# resolution so both agents point at the same VPS. Guarded so archive_upload
# still imports if the pusher module is unavailable (tests / partial installs).
try:
    from rovimen_pusher import (  # type: ignore
        resolve_base_url as _resolve_base_url,
        resolve_fallback_url as _resolve_fallback_url,
        resolve_host_key as _resolve_host_key,
        resolve_ingest_key as _resolve_ingest_key,
    )
except Exception:  # pragma: no cover - defensive
    _resolve_base_url = _resolve_host_key = _resolve_ingest_key = None  # type: ignore
    _resolve_fallback_url = None  # type: ignore

logger = logging.getLogger(__name__)

# Last-uploaded state.json signatures, persisted so a restart does not re-send
# every night on disk (see _SentSigs). Env-overridable for tests.
_SENT_SIGS_ENV = 'ROVIMEN_STATE_SIGS_PATH'
# Unsaved signatures that trigger an intermediate flush mid-cycle, so a crash
# or SIGTERM late in a long first pass costs at most this many re-uploads.
_SENT_SIGS_FLUSH_EVERY = 100


def _default_sent_sigs_path() -> Path:
    override = os.environ.get(_SENT_SIGS_ENV)
    if override:
        return Path(override)
    return Path.home() / 'rovimen_scripts' / 'upload_state' / 'state_json_sent.json'


class _SentSigs:
    """(size, mtime_ns) of the last successfully uploaded version of each
    night's state.json, keyed by the file's absolute path, persisted to disk.

    The map used to live only in the uploader's memory, so every process start
    -- the nightly daemon restart, every dawn_process one-shot, every manual
    run -- re-POSTed every state.json on disk: 15,450 POSTs (~5.8 GB, all
    written to the Storage Box) fleet-wide on 2026-09-23, for nights going back
    to May.

    Safety model: an entry is only ever written after a successful POST of a
    file whose stat() was taken BEFORE it was read, so a stored signature can
    only match a file whose content was uploaded. Concurrent processes (daemon
    + a one-shot) merge under an flock on save, and anything they get wrong can
    only leave an older signature behind, which costs one redundant re-upload
    -- never a skipped change. A corrupt or unreadable file is treated as
    empty (a one-off re-upload of everything, i.e. the old behaviour).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._sigs: dict[str, tuple[int, int]] = {}
        self._dirty: dict[str, tuple[int, int]] = {}
        self.reload()

    def _read_disk(self) -> dict[str, tuple[int, int]]:
        try:
            raw = json.loads(self._path.read_text())
            return {str(k): (int(v[0]), int(v[1])) for k, v in raw.get('sigs', {}).items()}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError, AttributeError, IndexError) as exc:
            logger.warning('state.json signature file %s unreadable (%s) -- treating as empty',
                           self._path, exc)
            return {}

    def reload(self) -> None:
        """Pick up signatures other processes saved; unsaved local ones win."""
        self._sigs = {**self._read_disk(), **self._dirty}

    def matches(self, path: Path, sig: tuple[int, int]) -> bool:
        return self._sigs.get(str(path)) == sig

    def record(self, path: Path, sig: tuple[int, int]) -> None:
        self._sigs[str(path)] = sig
        self._dirty[str(path)] = sig
        if len(self._dirty) >= _SENT_SIGS_FLUSH_EVERY:
            self.flush()

    def flush(self) -> None:
        """Merge unsaved signatures into the on-disk map (flock + atomic
        replace) and drop entries whose night directory is gone."""
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self._path.with_name(self._path.name + '.lock')
            with open(lock_path, 'w') as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                merged = {**self._read_disk(), **self._dirty}
                merged = {k: v for k, v in merged.items() if Path(k).parent.is_dir()}
                tmp = self._path.with_name(f'{self._path.name}.{os.getpid()}.tmp')
                tmp.write_text(json.dumps({'version': 1, 'sigs': merged}, separators=(',', ':')))
                os.replace(tmp, self._path)
        except OSError as exc:
            # Keep the dirty set: the next flush retries. Losing it only means
            # re-uploads after a restart, the pre-persistence behaviour.
            logger.warning('Could not save state.json signatures to %s: %s', self._path, exc)
            return
        self._sigs = merged
        self._dirty.clear()


# ---------------------------------------------------------------------------
# Per-night coordination between uploaders
# ---------------------------------------------------------------------------
# The archive-upload daemon and dawn (run_night, its backfill sweep) are
# separate processes that walk the same nights. The flags they share are only
# re-read between files, so two senders mid-POST on the same night both sent
# it: on gmnro01 (Ghirdoveni), night 20261023, 283 files (2.8 GB) went up
# twice. A pass now holds a camera-night's upload lock while it sends that
# night's files; whoever does not get it leaves the night to the holder.

# Env override for the directory holding the locks and mark journals (tests).
_UPLOAD_STATE_ENV = 'ROVIMEN_UPLOAD_STATE_DIR'

# How long dawn's one-night pass waits for a night another uploader holds
# before leaving it to that uploader. The daemon never waits.
_DAWN_LOCK_WAIT_S = 60.0
_LOCK_POLL_S = 2.0


def _upload_state_dir() -> Path:
    override = os.environ.get(_UPLOAD_STATE_ENV)
    if override:
        return Path(override)
    return Path.home() / 'rovimen_scripts' / 'upload_state'


# rovimen_watchdog.sh rewrites this file on every run (every 3 minutes) while
# the rescue Wi-Fi is up, and removes it when the rescue goes down. A phone
# hotspot must not carry a night's upload (gmnde00 (Johannismühle) sends about
# 19.5 GB a night), so the upload passes wait while it is fresh. An older file
# means the watchdog stopped running, and is ignored so uploads cannot stay
# blocked by a leftover.
_RESCUE_FLAG_MAX_AGE_S = 900


def _rescue_wifi_path() -> Path:
    return Path.home() / 'rovimen_scripts' / 'rescue_wifi_active'


def _on_rescue_wifi() -> bool:
    try:
        age = time.time() - _rescue_wifi_path().stat().st_mtime
    except OSError:
        return False
    if age > _RESCUE_FLAG_MAX_AGE_S:
        return False
    logger.info('Rescue Wi-Fi is up (%s): skipping this upload pass so the '
                'hotspot is not drained', _rescue_wifi_path())
    return True


class _NightLock:
    """Non-blocking flock on ``<upload_state>/locks/<camera>_<night>.lock``.

    flock (not fcntl's POSIX locks) is per open file, so it also keeps apart
    two threads of one process (dawn uploads its cameras in parallel, and its
    detection retry and backfill sweep run on their own threads). The kernel
    drops the lock when its holder dies, so a crash cannot strand a night.
    The lock files stay (a few bytes per camera-night): removing one while
    another process opens it would let two holders in."""

    def __init__(self, camera: str, date: str, root: Path) -> None:
        self.path = root / 'locks' / f'{camera}_{date}.lock'
        self._fd: int | None = None

    def acquire(self) -> bool:
        """True if this process now holds the night. A lock file that cannot
        be created counts as held: uploading unlocked is the old behaviour,
        and better than stopping the station's uploads."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o644)
        except OSError as exc:
            logger.warning('Could not open upload lock %s (%s) — uploading unlocked',
                           self.path, exc)
            return True
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class _MarkJournal:
    """Append-only record of one camera-night's confirmed uploads that are
    not in its state.json yet: one JSON line ``[kind, name, value]`` per
    file, fsync'd before the next file is sent.

    A pass writes upload marks into state.json in batches (_MARK_FLUSH_EVERY)
    because one state.json save per file cost ~0.5 GB of writes on a busy
    night and 3-4 s per save on HDD stations such as gmnro09 (Sibiciu). The
    queued marks used to live only in memory, so the power loss at 06:10 on
    gmnro01 (Ghirdoveni), night 20261023, re-sent 113 files. The journal
    keeps the batching (an append of ~100 bytes per file) and makes the marks
    durable: the night's next holder folds a leftover journal into
    state.json before it sends anything (_Uploader._take_night).

    Only the night's lock holder writes or clears it."""

    def __init__(self, camera: str, date: str, root: Path) -> None:
        self.path = root / 'journal' / f'{camera}_{date}.jsonl'

    def append(self, mark: tuple[str, str, object]) -> None:
        line = json.dumps([mark[0], mark[1], mark[2]], separators=(',', ':')) + '\n'
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(str(self.path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode('utf-8'))
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            # The mark is still queued in memory; only its crash safety is lost.
            logger.warning('Could not journal upload mark in %s: %s', self.path, exc)

    def read(self) -> list[tuple[str, str, object]]:
        """The journaled marks. A torn or garbled line (power lost mid-write)
        is skipped: its file is simply sent again."""
        try:
            text = self.path.read_text(encoding='utf-8', errors='replace')
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning('Could not read upload journal %s: %s', self.path, exc)
            return []
        marks: list[tuple[str, str, object]] = []
        for raw in text.splitlines():
            try:
                kind, name, value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if isinstance(kind, str) and isinstance(name, str):
                marks.append((kind, name, value))
        return marks

    def clear(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning('Could not clear upload journal %s: %s', self.path, exc)


# Logged once per process when the ingest settings are missing (see
# _Uploader._ready_to_upload).
_INGEST_MISSING_WARNED = False


def _post_blob_status_via_module(url: str, key: str, data: bytes, timeout: int) -> int:
    """The sender's transport: ``post_blob_status`` looked up at call time, so
    replacing ``archive_upload.post_blob_status`` (tests) takes effect."""
    return post_blob_status(url, key, data, timeout)


_is_locked = flags_manager.is_chunk_locked


# ---------------------------------------------------------------------------
# RMS file helpers
# ---------------------------------------------------------------------------

# Every RMS check plot the dashboard knows how to label (routes/archive.py
# RMS_PLOT_LABELS plus its ``*_stack_*.jpg`` "Meteor stack" case). Keep the two
# in step: a suffix listed here but not there uploads as an unlabelled file; one
# there but not here never leaves the station.
_RMS_PLOT_GLOBS = (
    '*_DETECTED_thumbs.jpg',
    '*_CAPTURED_thumbs.jpg',
    '*_captured_stack.jpg',
    '*_stack_*.jpg',
    '*_calib_report_astrometry.jpg',
    '*_calib_report_photometry.png',
    '*_calibration_variation.png',
    '*_photometry_variation.png',
    '*_fieldsums.png',
    '*_fieldsums_noavg.png',
    '*_observing_periods.png',
    '*_ff_intervals.png',
    '*_radiants.png',
)


# ---------------------------------------------------------------------------
# Upload cycle
# ---------------------------------------------------------------------------

@dataclass
class _ScanResults:
    locked_mkvs: list[tuple[Path, str, str]] = field(default_factory=list)
    stacks: list[tuple[Path, str, str]] = field(default_factory=list)
    timelapses: list[tuple[Path, str, str]] = field(default_factory=list)
    state_jsons: list[tuple[Path, str, str]] = field(default_factory=list)
    ffthumbs: list[tuple[Path, str, str]] = field(default_factory=list)
    # The night's colour meteor stack (stacks/<CAM>_<night>_color_meteor_stack.webp):
    # a night product with its own marker, not a chunk stack.
    meteor_stacks: list[tuple[Path, str, str]] = field(default_factory=list)
    # starprint: derived per-clip detail variants (<stem>_color.details.mkv).
    # Shipped only after their base chunk is uploaded+locked; not tracked by a
    # flags_manager field — deleted locally after a confirmed upload instead.
    details_mkvs: list[tuple[Path, str, str]] = field(default_factory=list)

    def extend(self, other: '_ScanResults') -> None:
        self.locked_mkvs.extend(other.locked_mkvs)
        self.stacks.extend(other.stacks)
        self.timelapses.extend(other.timelapses)
        self.state_jsons.extend(other.state_jsons)
        self.ffthumbs.extend(other.ffthumbs)
        self.meteor_stacks.extend(other.meteor_stacks)
        self.details_mkvs.extend(other.details_mkvs)


# Queued upload marks written in one state.json save once this many are waiting
# (see _Uploader._queue_mark); also flushed per night before its state.json
# uploads and at the end of every pass.
_MARK_FLUSH_EVERY = 50

# Night products: (product key, filename suffix). The timelapse and night stack
# upload to timelapse/, the colour meteor stack to stacks/ (where the dashboard
# looks for it, fdp_products.FDP_PRODUCTS).
_PRODUCT_SUFFIXES = (
    ('color_meteor_stack', '_color_meteor_stack.webp'),
    ('night_stack', '_night_stack.webp'),
    ('timelapse', '_timelapse.mp4'),
)

# A night product younger than this is left for a later pass. The timelapse,
# night stack and meteor stack used to be written straight to their final
# names while the daemon could be uploading them, and a truncated upload got
# the night's flag and was never sent again. The producers now write
# atomically; the age gate also covers a producer that does not.
_PRODUCT_MIN_AGE_S = 120.0


def _product_of(name: str) -> str | None:
    """The night product ``name`` is, or None for per-chunk files."""
    for product, suffix in _PRODUCT_SUFFIXES:
        if name.endswith(suffix):
            return product
    return None


def _file_sig(path: Path) -> tuple[int, int] | None:
    """(size, mtime_ns) of ``path``, or None if it is gone."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_size, st.st_mtime_ns)


def _file_sha1(path: Path) -> str | None:
    """Hex SHA-1 of ``path``'s content, or None if it cannot be read."""
    h = hashlib.sha1()
    try:
        with open(path, 'rb') as fh:
            for block in iter(lambda: fh.read(1 << 20), b''):
                h.update(block)
    except OSError:
        return None
    return h.hexdigest()


# An RMS text file recorded under the pre-2026-10-21 size-only mark and
# modified within this window is sent once more (see _rms_version_pending).
_RMS_LEGACY_TEXT_RESEND_S = 7 * 86400


def _size_or_zero(path: Path) -> int:
    """``path``'s size, or 0 if it vanished (the janitor may delete an
    uploaded file between the scan and the log line)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Upload plan
# ---------------------------------------------------------------------------
# A pass is a plan (an ordered list of _Items, built from one scan) run by one
# executor, so the one-night pass (dawn) and the full scan (daemon, backfill)
# share every rule and differ only in which nights they scan.

_K_STACK = 'stack'                 # stacks/<chunk>_stack.webp
_K_METEOR_STACK = 'meteor_stack'   # stacks/<CAM>_<night>_color_meteor_stack.webp
_K_CLIP = 'clip'                   # meteors/<chunk>_color.mkv (+ its stack)
_K_DETAILS = 'details'             # meteors/<chunk>_color.details.mkv
_K_PRODUCT = 'product'             # timelapse/: timelapse and night stack
_K_FFTHUMB = 'ffthumb'             # ffthumbs/<FF>.webp
_K_STATE = 'state'                 # state.json at the night root
_K_RMS = 'rms'                     # rms/: FTPdetectinfo, radiants, plots (one item per night)

# The order a pass sends things in; within a kind, newest night first. Meteor
# clips (each with its stack) lead: they are what the janitor holds disk for
# and what the dashboard waits on, and dawn's Phase 3 is meant to get them out
# before the bulk phases. The per-chunk stacks used to go first, and a
# 1,900-stack night (489 MB on gmnro20 (Brăila)) kept clips at zero for an
# hour behind them (known failure mode 21). state.json follows the clips so the
# archive can place their detections; RMS and thumbnails are small.
_PHASE_ORDER = (_K_CLIP, _K_DETAILS, _K_STATE, _K_RMS, _K_FFTHUMB,
                _K_STACK, _K_METEOR_STACK, _K_PRODUCT)
_PHASE_RANK = {kind: i for i, kind in enumerate(_PHASE_ORDER)}


@dataclass(frozen=True)
class _Item:
    kind: str
    camera: str
    date: str
    path: Path

    def sort_key(self) -> tuple[int, int, str, str]:
        night = int(self.date) if self.date.isdigit() else 0
        return (_PHASE_RANK[self.kind], -night, self.camera, self.path.name)


# ---------------------------------------------------------------------------
# Dawn-burst stagger
# ---------------------------------------------------------------------------
# Every station's night ends within minutes of every other station's (the fleet
# spans ~2 degrees of longitude), RMS finishes at dawn, and ``cycle()`` ships
# whatever is pending as fast as the uplink allows. So the whole fleet starts
# dumping its night at the same moment. Measured on the VPS 2026-09-09: blob
# POSTs peaked at 2,518 per 5-minute bucket, arrivals ran ~375 MiB/min for over
# an hour, the ingest spool on the VPS boot disk peaked at 20 GB, and free space
# fell to 3.3 GB. The storagebox flush lane (3 concurrent ops) could not keep
# up, which stalled every dashboard read that touched the same mount.
#
# Fix: give each station a deterministic slot inside a stagger window and hold
# BULK backlogs until that slot. The total volume is unchanged — the point is
# to flatten the peak arrival rate so the VPS flusher stays ahead of it.
#
# The burst fires from dawn_process._run_upload_all() (cron ``*/15 4-12``),
# which uploads the finished night for every camera in parallel. So the gate is
# a per-station slot inside the dawn window: a station simply does not start
# its dawn upload until its own slot has arrived.
#
# Two properties drive the design:
#   * the slot is deterministic in the host key, so a restart never reshuffles a
#     station and the fleet-wide spread holds night after night;
#   * the slot is a pure function of the wall clock and the host key — no state
#     file, nothing to persist, and nothing a restart can reset. A crash-looping
#     uploader (Restart=always, RestartSec=60) still ships at its slot.
#
# A plain hash of the host key is NOT good enough for the first property, which
# is easy to get wrong: hashing gives each station an INDEPENDENT uniform draw,
# and 15 independent draws over an hour clump badly (measured on the real fleet
# keys: 7 of 15 stations still landed in the same 15-minute cron tick, which is
# most of the burst we are trying to break up). So the fraction below is a
# low-discrepancy additive recurrence over the station NUMBER — successive
# numbers land far apart by construction, which is exactly the even spread the
# stagger needs — offset per host-key PREFIX so the gmnro/gmnde/gmnmd families
# do not overlay each other. Keys with no trailing number fall back to a hash.
#
# Deliberately NOT keyed on backlog age: a night's files are written from ~21:00
# onward, so "age of the oldest pending file" is already many hours by dawn and
# would never hold anything back.
_STAGGER_WINDOW_MIN_DEFAULT = 60.0
_STAGGER_JITTER_MIN_DEFAULT = 5.0
# Must match the first hour of the dawn_process cron (``*/15 4-12 * * *``), in
# station-local time, because that cron is what it paces.
_STAGGER_WINDOW_START_HOUR_DEFAULT = 4


# Conjugate of the golden ratio. Successive multiples of it, taken modulo 1,
# are the classic low-discrepancy sequence: any prefix of it covers [0,1) about
# as evenly as that many points can.
_GOLDEN_CONJUGATE = 0.6180339887498949


def _hash_fraction(text: str) -> float:
    """``text`` hashed to a fraction in [0, 1).

    8 bytes of SHA-256 is ample spread and exactly representable in a float64.
    """
    digest = hashlib.sha256(text.encode('utf-8')).digest()
    return int.from_bytes(digest[:8], 'big') / float(1 << 64)


def _stagger_fraction(host_key: str) -> float:
    """This station's position in [0, 1) of the stagger window.

    Low-discrepancy in the station number (see the note above), so gmnro01..09
    spread across the whole window instead of clumping the way independent
    hashes do. The per-prefix hash keeps the gmnro / gmnde / gmnmd families from
    landing on top of each other.
    """
    match = re.search(r'(\d+)$', host_key)
    if match is None:
        # No trailing station number (an unusual host key): a plain hash is the
        # best available, and one odd station cannot clump with anything.
        return _hash_fraction(host_key)
    number = int(match.group(1))
    prefix_offset = _hash_fraction(host_key[:match.start()])
    return (prefix_offset + number * _GOLDEN_CONJUGATE) % 1.0


def _local_day(now: float | None = None) -> str:
    """Station-local date (YYYYMMDD) of ``now``: the jitter's daily key."""
    return time.strftime('%Y%m%d', time.localtime(time.time() if now is None else now))


def _stagger_offset_s(host_key: str, window_s: float, jitter_s: float = 0.0,
                      day: str | None = None) -> float:
    """This station's slot, in seconds into the stagger window.

    Deterministic in ``host_key`` (see the note above), plus up to ``jitter_s``
    that changes every day, so two stations that do land close together do
    not stay in lock-step every night. The jitter is a hash of the host key and
    the day, not a random draw: dawn asks once per camera, and independent
    draws put one station's cameras on both sides of its slot, so some uploaded
    early and some a pass later. ``window_s <= 0`` disables the stagger.
    """
    if window_s <= 0:
        return 0.0
    offset = _stagger_fraction(host_key) * window_s
    if jitter_s > 0:
        offset += _hash_fraction(f'{host_key}/{day or _local_day()}') * jitter_s
    return offset


def _stagger_window_start(now: float, start_hour: int) -> float:
    """Epoch of ``start_hour`` today, in STATION-LOCAL time.

    Local, not UTC, because it paces a local-time cron. The fleet runs UTC
    host clocks, so in practice the two agree.
    """
    lt = time.localtime(now)
    return time.mktime((
        lt.tm_year, lt.tm_mon, lt.tm_mday, start_hour, 0, 0,
        lt.tm_wday, lt.tm_yday, -1,
    ))


def stagger_hold_s(cfg: dict, host_key: str, now: float | None = None,
                   offset_s: float | None = None) -> float:
    """Seconds this station should still wait before starting its dawn upload.

    0.0 means "go now". Callers on a cron (``run_night``) should SKIP the pass
    and let the next cron run retry; a long-lived daemon can sleep it.

    Returns 0.0 outside the dawn window too, so a manual or evening run is
    never held back — the gate only paces the one moment when the whole fleet
    would otherwise fire at once.
    """
    arc = cfg.get('archive', {})
    window = float(arc.get('stagger_window_minutes', _STAGGER_WINDOW_MIN_DEFAULT)) * 60
    if window <= 0:
        return 0.0
    jitter = float(arc.get('stagger_jitter_minutes', _STAGGER_JITTER_MIN_DEFAULT)) * 60
    start_hour = int(arc.get('stagger_window_start_hour',
                             _STAGGER_WINDOW_START_HOUR_DEFAULT))
    now = time.time() if now is None else now
    if offset_s is None:
        offset_s = _stagger_offset_s(host_key, window, jitter, day=_local_day(now))
    window_start = _stagger_window_start(now, start_hour)
    if now < window_start:
        return 0.0
    return max(0.0, (window_start + offset_s) - now)


def stagger_id(cfg: dict) -> str:
    """The fleet-stable identity a station's slot is derived from.

    The ingest host key when resolvable; the hostname otherwise, so a station
    whose ingest identity is not configured yet still gets a slot instead of
    silently joining the unstaggered stampede.
    """
    if _resolve_host_key is not None:
        try:
            key = _resolve_host_key(cfg)
        except Exception:
            key = None
        if key:
            return str(key)
    return socket.gethostname()


class _Uploader:
    def __init__(self, cfg: dict) -> None:
        self._cfg = cfg
        arc = cfg.get('archive', {})
        self.enabled: bool = arc.get('enabled', False)
        self.upload_meteors: bool = arc.get('upload_meteors', True)
        self.upload_timelapses: bool = arc.get('upload_timelapses', True)
        # Default True: the per-chunk night stacks are the dashboard's primary
        # per-camera "plot" source. The historical False default silently drifted
        # stations off the plot pipeline whenever a config was reinstalled or
        # missed in the manual per-station patchwork — matching config_defaults.json
        # (which already ships True) removes that footgun.
        self.upload_stacks: bool = arc.get('upload_stacks', True)
        self.upload_rms: bool = arc.get('upload_rms', True)

        self.videocapture_path = rovimen_config.capture_root(cfg)
        self.stations = cfg.get('stations', {})
        # state.json changes only while a night is still being written, but the
        # cycle re-scans every night forever. Remember the last-uploaded (size,
        # mtime_ns) per night -- on disk, so a restart doesn't re-send them all.
        self._state_sigs = _SentSigs(_default_sent_sigs_path())

        # The current pass's abort check (dawn's deadline, the daemon's stop),
        # consulted before every file and inside every retry wait.
        self._abort: Callable[[], bool] | None = None
        # Upload marks not yet written, per night, while a pass batches them
        # (None outside a pass: marks are written at once). See _queue_mark.
        self._pending_marks: dict[tuple[str, str], list[tuple[str, str, object]]] | None = None
        self._pending_index: set[tuple[str, str, str, str]] = set()

        # The camera-night this pass holds (see _NightLock), its journal, and
        # how long a pass waits for a night another uploader holds.
        self._coord_root = _upload_state_dir()
        self._held: tuple[str, str] | None = None
        self._held_lock: _NightLock | None = None
        self._journals: dict[tuple[str, str], _MarkJournal] = {}
        self._lock_wait_s = 0.0

        # HTTPS-ingest byte-plane target (same VPS + key as the pusher).
        self.ingest_base_url: str | None = _resolve_base_url(cfg) if _resolve_base_url else None
        self.ingest_key: str | None = _resolve_ingest_key() if _resolve_ingest_key else None
        self.ingest_host_key: str | None = _resolve_host_key(cfg) if _resolve_host_key else None
        self.ingest_fallback_url: str | None = (
            _resolve_fallback_url(cfg) if _resolve_fallback_url else None)

        # Retry policy, quarantine, deferral, the spool-full pause and the
        # fallback route for this uploader's blob POSTs (see ingest_blob.BlobSender).
        self._sender = BlobSender(
            transport=_post_blob_status_via_module,
            fallback=(self.ingest_base_url or "", self.ingest_fallback_url or ""))

    def _ingest_ready(self) -> bool:
        return bool(self.ingest_base_url and self.ingest_key and self.ingest_host_key)

    def _ready_to_upload(self) -> bool:
        """Enabled and able to reach the ingest endpoint.

        Without the ingest settings every POST fails, so a pass skips even the
        scan. It used to walk every night and log one warning per file per
        cycle (the same flood as gmnro10 (Roit)'s 11k lines); now it logs once
        per process.
        """
        if not self.enabled:
            return False
        if self._ingest_ready():
            return True
        global _INGEST_MISSING_WARNED
        if not _INGEST_MISSING_WARNED:
            _INGEST_MISSING_WARNED = True
            logger.warning('archive upload: ingest not configured (base_url=%s key=%s '
                           'host_key=%s) — nothing can upload until it is; see '
                           '~/.config/rovimen/ingest.env',
                           bool(self.ingest_base_url), bool(self.ingest_key),
                           bool(self.ingest_host_key))
        return False

    def _blob_url(self, camera: str, date: str, relpath: str) -> str:
        base = (self.ingest_base_url or "").rstrip("/")
        return f"{base}/api/ingest/v1/{self.ingest_host_key}/blob/{camera}/{date}/{relpath}"

    @property
    def _quarantine(self) -> dict[str, tuple[float, str, int]]:
        """Blob URLs parked after a permanent rejection (see BlobSender)."""
        return self._sender.quarantined

    def _is_quarantined(self, url: str, size: int) -> bool:
        return self._sender.is_quarantined(url, size)

    def _post_one(self, local: Path, camera: str, date: str, subdir: str) -> bool:
        """Upload one file to the archive via the ingest blob endpoint. ``subdir``
        is '' for a night-root file (state.json).

        The retry policy is BlobSender's. An empty source is parked without a
        request: it can only ever come back 422 empty_blob, and the producing
        step (stacker, encoder, timelapse) has to regenerate it first."""
        if not self._ingest_ready():
            logger.debug("archive upload: ingest not configured; cannot upload %s", local.name)
            return False
        relpath = f"{subdir}/{local.name}" if subdir else local.name
        url = self._blob_url(camera, date, relpath)

        try:
            size = local.stat().st_size
        except OSError as exc:
            logger.warning("could not stat %s for upload: %s", local, exc)
            return False

        if size == 0:
            self._sender.quarantine(
                url, size,
                "source file is 0 bytes — the station must regenerate it "
                "(a stack/clip that failed to render) before it can be archived")
            return False

        if self._sender.blocked(url, size):
            return False

        try:
            data = local.read_bytes()
        except OSError as exc:
            logger.warning("could not read %s for upload: %s", local, exc)
            return False
        # Truncated under us between stat and read — same verdict as size == 0.
        if not data:
            self._sender.quarantine(url, len(data),
                                    "read back 0 bytes — file truncated while being uploaded")
            return False
        return self._sender.send(url, self.ingest_key or '', data, size, local.name,
                                 should_abort=self._abort)

    def _halted(self) -> bool:
        """True when the current pass must stop: aborted, or every POST is on
        hold (spool full, key rejected, breaker open)."""
        if self._abort is not None and self._abort():
            return True
        return self._sender.paused()

    # -- upload marks --------------------------------------------------------

    def _queue_mark(self, camera: str, date: str, mark: tuple[str, str, object]) -> None:
        """Record a confirmed upload: journaled and queued while a pass
        batches marks (see _MarkJournal), else written at once."""
        if self._pending_marks is None:
            self._write_marks(camera, date, [mark])
            return
        self._journal(camera, date).append(mark)
        self._pending_marks.setdefault((camera, date), []).append(mark)
        self._pending_index.add((camera, date, mark[0], mark[1]))
        if sum(len(m) for m in self._pending_marks.values()) >= _MARK_FLUSH_EVERY:
            self._flush_marks()

    def _marked_pending(self, camera: str, date: str, kind: str, name: str) -> bool:
        """True if this pass has a queued (unwritten) mark for the file."""
        return (camera, date, kind, name) in self._pending_index

    def _flush_marks(self, camera: str | None = None, date: str | None = None) -> None:
        """Write queued marks: one night's, or all of them."""
        if not self._pending_marks:
            return
        keys = [(camera, date)] if camera is not None else list(self._pending_marks)
        for key in keys:
            marks = self._pending_marks.pop(key, None)  # type: ignore[arg-type]
            if not marks:
                continue
            for kind, name, _value in marks:
                self._pending_index.discard((key[0], key[1], kind, name))  # type: ignore[arg-type]
            if self._write_marks(key[0], key[1], marks):  # type: ignore[arg-type]
                # Every journaled mark of the night was queued in this pass
                # (a leftover journal is folded when the night is taken).
                self._journal(key[0], key[1]).clear()  # type: ignore[arg-type]

    def _write_marks(self, camera: str, date: str, marks: list[tuple[str, str, object]]) -> bool:
        """Write marks into the night's state.json; True if they reached it."""
        try:
            return flags_manager.mark_uploaded_batch(camera, date, marks, self._cfg)
        except OSError as exc:
            # Corrupted or unwritable night directory — log and continue. The
            # files are already uploaded; worst case they re-upload next pass.
            logger.warning('Could not persist %d uploaded flag(s) for %s/%s: %s',
                           len(marks), camera, date, exc)
            return False

    def _journal(self, camera: str, date: str) -> _MarkJournal:
        key = (camera, date)
        journal = self._journals.get(key)
        if journal is None:
            journal = self._journals[key] = _MarkJournal(camera, date, self._coord_root)
        return journal

    # -- per-night lock ------------------------------------------------------

    def _take_night(self, camera: str, date: str) -> bool:
        """Hold this camera-night for the pass, releasing the one held before.

        Waits up to ``self._lock_wait_s`` (dawn) for another uploader to let
        go, then gives up: that uploader is sending the same files. A night
        taken with a leftover journal (its last holder crashed) has those
        marks folded into state.json first, so nothing it confirmed is sent
        again."""
        if self._held == (camera, date):
            return True
        self._release_night()
        lock = _NightLock(camera, date, self._coord_root)
        deadline = time.monotonic() + self._lock_wait_s
        while not lock.acquire():
            if time.monotonic() >= deadline or self._halted():
                return False
            time.sleep(_LOCK_POLL_S)
        self._held, self._held_lock = (camera, date), lock
        journal = self._journal(camera, date)
        leftover = journal.read()
        if leftover:
            logger.info('[%s/%s] Recovered %d upload mark(s) from an interrupted pass',
                        camera, date, len(leftover))
            if self._write_marks(camera, date, leftover):
                journal.clear()
        return True

    def _release_night(self) -> None:
        """Write the held night's queued marks, then let the night go, so the
        next holder sees every upload this pass made."""
        if self._held is None:
            return
        try:
            self._flush_marks(*self._held)
        finally:
            if self._held_lock is not None:
                self._held_lock.release()
            self._held, self._held_lock = None, None

    def _mark_ffthumb_uploaded(self, local: Path, camera: str, date: str) -> None:
        """Ffthumb names are plain ``<stem>.webp`` so the suffix dispatch in
        _mark_uploaded cannot classify them — mark them explicitly."""
        self._queue_mark(camera, date, (flags_manager.MARK_FFTHUMB, local.name, True))

    def _mark_uploaded(self, local: Path, camera: str, date: str,
                       sig: tuple[int, int] | None = None) -> None:
        """Record a confirmed upload. ``sig`` is the (size, mtime_ns) taken
        before the file was read; night products are marked with it, so a
        file rebuilt while it was uploading still counts as not uploaded."""
        name = local.name
        product = _product_of(name)
        if name.endswith('_color.mkv'):
            self._queue_mark(camera, date, (flags_manager.MARK_CHUNK, name, True))
        elif product is not None:
            # Before the _stack.webp branch: the night stack and the colour
            # meteor stack end with _stack.webp too.
            sig = sig or _file_sig(local)
            if sig is not None:
                self._queue_mark(camera, date, (flags_manager.MARK_PRODUCT, product, list(sig)))
        elif name.endswith('_stack.webp'):
            chunk_name = name.replace('_stack.webp', '_color.mkv')
            self._queue_mark(camera, date, (flags_manager.MARK_CHUNK_STACK, chunk_name, True))

    def _upload(self, local: Path, camera: str, date: str, subdir: str) -> bool:
        sig = _file_sig(local)  # before the read: see _mark_uploaded
        if self._post_one(local, camera, date, subdir):
            self._mark_uploaded(local, camera, date, sig=sig)
            logger.info('Uploaded %s → %s/%s/%s', local.name, camera, date, subdir)
            return True
        return False

    def _upload_details_variant(self, details: Path, camera: str, date: str) -> bool:
        """Ship one starprint ``*.details.mkv`` to meteors/, then delete the
        local ``.details.mkv`` (the VPS derives its ``.details.mp4``).

        The details clip is a DERIVED, re-creatable file (overlay_details
        re-renders it from the pristine base every dawn), so it needs no local
        retention, no janitor interaction and no uploaded flag: once the upload
        is confirmed, the local copy is removed. Crash-safe — the
        local file is deleted ONLY after a confirmed upload, so a failed upload
        just leaves it for the next cycle to retry. Idempotent — a re-rendered
        details clip re-uploads and re-deletes without touching the base chunk.
        """
        if not self._post_one(details, camera, date, 'meteors'):
            return False
        details.unlink(missing_ok=True)
        logger.info('Shipped details variant %s → %s/%s/meteors', details.name, camera, date)
        return True

    def _is_reencoded(self, mkv: Path, station_id: str, date_str: str) -> bool:
        return flags_manager.is_chunk_reencoded(mkv, station_id, date_str, self._cfg)

    def _scan_all(self) -> _ScanResults:
        """Walk the directory tree once and collect all uploadable files.

        One unreadable station or night directory (a failing USB disk answers
        EIO, a dead capture disk leaves a root-owned stub) costs only that
        directory: it is logged and skipped, and every other night, and the
        RMS files on their own disk, still upload."""
        results = _ScanResults()
        for station_id in self.stations:
            station_dir = self.videocapture_path / station_id
            try:
                if not station_dir.exists():
                    continue
                date_dirs = [d for d in station_dir.iterdir()
                             if len(d.name) == 8 and d.is_dir()]
            except OSError as exc:
                logger.warning('Could not list %s (%s) — skipping its nights this pass',
                               station_dir, exc)
                continue
            for date_dir in date_dirs:
                night = self._scan_night(station_id, date_dir)
                if night is not None:
                    results.extend(night)
        self._add_ffthumbs(results)

        # Process newest nights first. iterdir() yields nights in arbitrary
        # filesystem order, so a daemon with a large backlog (e.g. a dead
        # camera's months of un-reaped sessions) could spend an entire cycle on
        # ancient data and never reach the current night's stacks before the
        # nightly reboot restarts the scan — the dashboard then shows stale
        # plots indefinitely. Sorting by night date descending guarantees the
        # freshest data uploads first; the old tail just waits for a later pass.
        results.locked_mkvs.sort(key=lambda t: t[2], reverse=True)
        results.stacks.sort(key=lambda t: t[2], reverse=True)
        results.timelapses.sort(key=lambda t: t[2], reverse=True)
        results.state_jsons.sort(key=lambda t: t[2], reverse=True)
        results.ffthumbs.sort(key=lambda t: t[2], reverse=True)
        results.meteor_stacks.sort(key=lambda t: t[2], reverse=True)
        results.details_mkvs.sort(key=lambda t: t[2], reverse=True)
        return results

    def _add_ffthumbs(self, results: _ScanResults, station_id: str | None = None,
                      date_str: str | None = None) -> None:
        """Backfill sweep for the indexer's live BW push, over the thumbnail
        root (rovimen_config.ffthumb_root), which does not depend on the colour
        disk: any FF thumbnail the indexer rendered but could not POST (VPS
        unreachable, service restart) goes up here. Limited to one camera
        night when ``station_id``/``date_str`` are given (dawn's fast path).
        Thumbnails already found in a night's legacy ffthumbs/ dir are not
        added twice."""
        root = rovimen_config.ffthumb_root(self._cfg)
        queued = {(cam, date, p.name) for p, cam, date in results.ffthumbs}
        stations = [station_id] if station_id else list(self.stations)
        for cam in stations:
            try:
                cam_dir = root / cam
                if not cam_dir.is_dir():
                    continue
                nights = [cam_dir / date_str] if date_str else [
                    d for d in cam_dir.iterdir() if len(d.name) == 8 and d.is_dir()]
                for night_dir in nights:
                    if not night_dir.is_dir():
                        continue
                    uploaded = flags_manager.uploaded_ffthumbs(cam, night_dir.name, self._cfg)
                    for webp in night_dir.glob('*.webp'):
                        key = (cam, night_dir.name, webp.name)
                        if webp.name not in uploaded and key not in queued:
                            results.ffthumbs.append((webp, cam, night_dir.name))
                            queued.add(key)
            except OSError as exc:
                logger.warning('Could not scan FF thumbnails for %s (%s)', cam, exc)

    def _scan_night(self, station_id: str, date_dir: Path) -> _ScanResults | None:
        """Collect one night's uploadable files, or None if the night directory
        cannot be read (logged; the rest of the pass goes on)."""
        night = _ScanResults()
        try:
            date_str = date_dir.name
            sj = date_dir / 'state.json'
            if sj.exists():
                night.state_jsons.append((sj, station_id, date_str))
            for mkv in date_dir.glob('*_color.mkv'):
                if flags_manager.is_chunk_uploaded(station_id, date_str, mkv.name, self._cfg):
                    continue
                if not _is_locked(mkv, station_id, date_str, self._cfg):
                    continue
                if not self._is_reencoded(mkv, station_id, date_str):
                    continue
                night.locked_mkvs.append((mkv, station_id, date_str))
            # starprint details variants. The *_color.mkv glob above does
            # NOT match these ('.details.mkv' is a different suffix), so
            # there is no overlap. Ship one only when its BASE chunk is
            # already uploaded AND locked — we annotate real, already-shipped
            # detections, never speculative work.
            for f in date_dir.glob('*.details.mkv'):
                base_name = f.name.replace('.details.mkv', '.mkv')
                if not flags_manager.is_chunk_uploaded(
                        station_id, date_str, base_name, self._cfg):
                    continue
                base = date_dir / base_name
                if not _is_locked(base, station_id, date_str, self._cfg):
                    continue
                night.details_mkvs.append((f, station_id, date_str))
            stacks_dir = date_dir / 'stacks'
            if stacks_dir.exists():
                for webp in stacks_dir.glob('*_stack.webp'):
                    if _product_of(webp.name) == 'color_meteor_stack':
                        if self._product_pending(webp, station_id, date_str):
                            night.meteor_stacks.append((webp, station_id, date_str))
                        continue
                    chunk_name = webp.name.replace('_stack.webp', '_color.mkv')
                    if not flags_manager.is_chunk_stack_uploaded(station_id, date_str, chunk_name, self._cfg):
                        night.stacks.append((webp, station_id, date_str))
            for pattern in ('*_timelapse.mp4', '*_night_stack.webp'):
                for f in date_dir.glob(pattern):
                    if self._product_pending(f, station_id, date_str):
                        night.timelapses.append((f, station_id, date_str))
            # Backfill sweep for the indexer's live BW push: any rendered
            # FF thumbnail the indexer could not POST (VPS unreachable,
            # service restart) gets picked up here.
            ffthumb_dir = date_dir / 'ffthumbs'
            if ffthumb_dir.exists():
                uploaded = flags_manager.uploaded_ffthumbs(station_id, date_str, self._cfg)
                for webp in ffthumb_dir.glob('*.webp'):
                    if webp.name not in uploaded:
                        night.ffthumbs.append((webp, station_id, date_str))
        except OSError as exc:
            logger.warning('[%s/%s] Could not scan night directory (%s) — skipping it '
                           'this pass', station_id, date_dir.name, exc)
            return None
        return night

    def _product_pending(self, path: Path, station_id: str, date_str: str) -> bool:
        """True if this version of a night product still needs uploading and
        is old enough to be complete (see _PRODUCT_MIN_AGE_S)."""
        product = _product_of(path.name)
        sig = _file_sig(path)
        if product is None or sig is None:
            return False
        if flags_manager.is_product_uploaded(station_id, date_str, product,
                                             sig[0], sig[1], self._cfg):
            return False
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:
            return False
        if age < _PRODUCT_MIN_AGE_S:
            logger.debug('[%s/%s] %s is %.0fs old — leaving it for a later pass',
                         station_id, date_str, path.name, age)
            return False
        return True

    def _fmt_size(self, nbytes: int) -> str:
        if nbytes >= 1024 ** 3:
            return f'{nbytes / 1024 ** 3:.1f} GB'
        if nbytes >= 1024 ** 2:
            return f'{nbytes / 1024 ** 2:.0f} MB'
        return f'{nbytes / 1024:.0f} KB'

    def _find_rms_files(self, camera: str, date_str: str) -> list[Path]:
        """Return FTPdetectinfo + radiants + FF-intervals/calibration-variation
        plot files for a camera/night.

        RMS writes these into the CapturedFiles session dir first and mirrors
        them to ArchivedFiles later — scan both so the detection results reach
        the archive as soon as detection completes. The newest copy wins, the
        ArchivedFiles one (the final, filtered one) on a tie: a reprocess
        rewrites CapturedFiles minutes before it re-archives, and on
        2026-10-21 the stale ArchivedFiles copies of gmnro22 (Ferigile)'s
        recalibrated plots won that window, so the pass skipped them.

        The plot images ride along here so the dashboard resolves "latest
        night with a plot" from the archive alone (cache_store.py's
        _ArchiveIndex) instead of polling every station live on every page
        load. ``_RMS_PLOT_GLOBS`` mirrors the dashboard's ``RMS_PLOT_LABELS``
        catalogue: until it did, only the two calibration plots were ever
        mirrored and every other check plot (radiants, stacks, field sums,
        photometry, observing periods) vanished from the dashboard the day the
        plot surfaces went archive-first."""
        sinfo = self.stations.get(camera, {})
        rms_data_path = sinfo.get('rms_data_path')
        if not rms_data_path:
            return []
        by_name: dict[str, Path] = {}
        mtimes: dict[str, int] = {}

        def keep(f: Path) -> None:
            try:
                mtime_ns = f.stat().st_mtime_ns
            except OSError:
                return
            if f.name not in by_name or mtime_ns >= mtimes[f.name]:
                by_name[f.name] = f
                mtimes[f.name] = mtime_ns

        for sub in ('CapturedFiles', 'ArchivedFiles'):
            base = Path(rms_data_path) / sub
            if not base.exists():
                continue
            try:
                for session_dir in sorted(base.iterdir()):
                    if not session_dir.is_dir():
                        continue
                    m = rovimen_night.SESSION_DIR_RE.match(session_dir.name)
                    if not m:
                        continue
                    if rovimen_night.night_of(m.group(2), m.group(3)) != date_str:
                        continue
                    for f in session_dir.glob('*_radiants.txt'):
                        keep(f)
                    for f in session_dir.glob('FTPdetectinfo_*.txt'):
                        if '_unfiltered' not in f.name and '_backup' not in f.name:
                            keep(f)
                    for pattern in _RMS_PLOT_GLOBS:
                        for f in session_dir.glob(pattern):
                            keep(f)
            except OSError as exc:
                logger.warning('Could not scan RMS sessions for %s/%s: %s', camera, date_str, exc)
        return list(by_name.values())

    def _upload_rms_files(self, camera: str, date_str: str) -> int:
        """Upload FTPdetectinfo + radiants files to the archive rms/ subdir."""
        if not self.upload_rms:
            return 0
        files = self._find_rms_files(camera, date_str)
        if not files:
            return 0
        count = 0
        for f in files:
            if self._halted():
                break
            # Skip files whose current content already reached the archive.
            # Without a marker the daemon re-POSTs every night's files on every
            # cycle; with a size-only marker a same-size rewrite (a recalibrated
            # FTPdetectinfo) never re-uploads. See _rms_version_pending.
            pending, version = self._rms_version_pending(camera, date_str, f)
            if version is None:
                continue
            if not pending:
                self._queue_mark(camera, date_str, (flags_manager.MARK_RMS, f.name, version))
                continue
            if self._post_one(f, camera, date_str, 'rms'):
                self._queue_mark(camera, date_str, (flags_manager.MARK_RMS, f.name, version))
                logger.info('Uploaded RMS file %s (%s/%s/rms)', f.name, camera, date_str)
                count += 1
        return count

    def _rms_version_pending(self, camera: str, date_str: str,
                             f: Path) -> tuple[bool, list | None]:
        """Whether this version of an RMS file still has to reach the archive.

        Returns ``(pending, version)``. ``version`` is ``[size, mtime_ns,
        sha1]`` to record, or None when nothing is to be done (unreadable, or
        already recorded exactly). ``(False, version)`` means the content is
        already archived and only the record needs refreshing.

        Size and mtime are the cheap check; the SHA-1 is computed only when
        either differs from the record, so an unchanged backlog costs a stat
        per file. A same-content rewrite (a copy into ArchivedFiles with a new
        mtime) updates the record without a POST.

        A legacy record (a bare size, before 2026-10-21) cannot tell a same-size
        rewrite from the original. A plot rewrite changes the compressed size,
        so an equal-size image is taken as archived; equal-size text modified in
        the last _RMS_LEGACY_TEXT_RESEND_S is sent once more, which covers a
        recalibrated FTPdetectinfo without re-sending the whole backlog.
        """
        try:
            st = f.stat()
        except OSError:
            return False, None
        size, mtime_ns = st.st_size, st.st_mtime_ns
        mark = flags_manager.rms_upload_mark(camera, date_str, f.name, self._cfg)
        recorded = isinstance(mark, list) and len(mark) == 3 and mark[0] == size
        if recorded and mark[1] == mtime_ns:
            return False, None
        sha1 = _file_sha1(f)
        if sha1 is None:
            return False, None
        version = [size, mtime_ns, sha1]
        if recorded:
            return mark[2] != sha1, version
        if type(mark) is int and mark == size:
            recent = time.time() - st.st_mtime < _RMS_LEGACY_TEXT_RESEND_S
            return f.suffix == '.txt' and recent, version
        return True, version

    def _upload_state_json(self, station_id: str, date_str: str) -> None:
        """Upload state.json so the archive can reconstruct detection offsets."""
        state_path = self.videocapture_path / station_id / date_str / 'state.json'
        if not state_path.exists():
            return
        try:
            st = state_path.stat()
            sig = (st.st_size, st.st_mtime_ns)
        except OSError:
            sig = None
        # Skip re-POST when the state.json is unchanged since its last
        # successful upload (by any process -- see _SentSigs). Old, settled
        # nights don't change, so re-sending them is pure churn; a
        # still-writing night changes size/mtime and re-uploads as expected.
        if sig is not None and self._state_sigs.matches(state_path, sig):
            return
        if self._post_one(state_path, station_id, date_str, '') and sig is not None:
            self._state_sigs.record(state_path, sig)

    def _run_pass(self, body: Callable[[], int],
                  should_abort: Callable[[], bool] | None = None) -> int:
        """Run one upload pass with a per-pass state.json read cache and the
        persisted state.json signatures refreshed before and saved after
        (also when the pass is stopped or raises)."""
        self._state_sigs.reload()
        self._abort = should_abort
        try:
            with flags_manager.scan_cache():
                count = body()
            if self._sender.paused():
                logger.warning('Upload pass stopped early: %s', self._sender.pause_reason)
            elif should_abort is not None and should_abort():
                logger.info('Upload pass stopped early: asked to stop')
            return count
        finally:
            self._abort = None
            self._state_sigs.flush()

    def cycle_night(self, station_id: str, date_str: str,
                    should_abort: Callable[[], bool] | None = None,
                    stop_event: threading.Event | None = None) -> int:
        """Upload all pending data for one station/night (dawn's fast path).

        Scans exactly that night (and its RMS files) and runs the same plan as
        the full scan, so dawn gets meteor clips to the archive before the long
        bulk phases without walking the backlog.
        """
        if not self._ready_to_upload() or _on_rescue_wifi():
            return 0
        self._lock_wait_s = _DAWN_LOCK_WAIT_S
        try:
            return self._run_pass(lambda: self._cycle_night(station_id, date_str),
                                  _combine_abort(should_abort, stop_event))
        finally:
            self._lock_wait_s = 0.0

    def _cycle_night(self, station_id: str, date_str: str) -> int:
        scan = _ScanResults()
        date_dir = self.videocapture_path / station_id / date_str
        try:
            if date_dir.is_dir():
                night = self._scan_night(station_id, date_dir)
                if night is not None:
                    scan = night
        except OSError as exc:
            # A failing colour disk must not stop the RMS files, which live
            # on the RMS data disk, from reaching the archive.
            logger.warning('[%s/%s] Colour night directory unreadable (%s) — '
                           'uploading the RMS files only', station_id, date_str, exc)
        self._add_ffthumbs(scan, station_id, date_str)
        return self._execute(self._plan(scan, [(station_id, date_str)]))

    def _discover_rms_nights(self) -> set[tuple[str, str]]:
        """Discover (camera, night) pairs straight from the RMS data tree.

        The full scan learns which nights exist from scan.state_jsons, but
        state.json is a colour-capture artefact. A plots-only station never runs
        capture, so no state.json exists and no night is ever scanned — the RMS
        plots would never upload. Walk the CapturedFiles/ArchivedFiles session
        dirs instead and resolve each to its observation night, using the same
        regex and night rule as _find_rms_files."""
        nights: set[tuple[str, str]] = set()
        for camera, sinfo in self.stations.items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            for sub in ('CapturedFiles', 'ArchivedFiles'):
                base = Path(rms_data_path) / sub
                if not base.exists():
                    continue
                try:
                    for session_dir in base.iterdir():
                        if not session_dir.is_dir():
                            continue
                        m = rovimen_night.SESSION_DIR_RE.match(session_dir.name)
                        if not m:
                            continue
                        nights.add((camera, rovimen_night.night_of(m.group(2), m.group(3))))
                except OSError as exc:
                    logger.warning('Could not scan RMS sessions for %s: %s', camera, exc)
        return nights

    def cycle(self, stop_event: threading.Event | None = None,
              should_abort: Callable[[], bool] | None = None) -> int:
        """Full-scan pass: every night of every camera, newest night first."""
        if not self._ready_to_upload() or _on_rescue_wifi():
            return 0
        return self._run_pass(self._cycle, _combine_abort(should_abort, stop_event))

    def _cycle(self) -> int:
        scan = self._scan_all()

        # Drift guardrail: stacks are the dashboard's per-camera plot source. If
        # the flag is off yet stacks are piling up locally, the plots will be
        # silently missing — make that loud instead of invisible.
        if not self.upload_stacks and scan.stacks:
            nights = {(s, d) for _p, s, d in scan.stacks}
            logger.warning(
                'upload_stacks is DISABLED but %d stack(s) across %d night(s) are '
                'pending locally — dashboard plots for this station will be missing. '
                'Set archive.upload_stacks=true.', len(scan.stacks), len(nights))

        # RMS files upload independently of the colour pipeline. Night discovery
        # via scan.state_jsons only covers capture stations (state.json is a
        # colour-capture artefact); a plots-only station has none, so walk the
        # RMS tree directly. Union both sources so a capture station is
        # unaffected.
        rms_nights: set[tuple[str, str]] = set()
        if self.upload_rms:
            rms_nights = {(s, d) for _sj, s, d in scan.state_jsons}
            rms_nights |= self._discover_rms_nights()
        return self._execute(self._plan(scan, rms_nights))

    # -- plan and execute --------------------------------------------------

    def _plan(self, scan: _ScanResults, rms_nights: Iterable[tuple[str, str]]) -> list[_Item]:
        """Everything the scan found that this station uploads, in pass order."""
        items: list[_Item] = []

        def add(kind: str, found: list[tuple[Path, str, str]]) -> None:
            items.extend(_Item(kind, cam, date, path) for path, cam, date in found)

        if self.upload_stacks:
            add(_K_STACK, scan.stacks)
            add(_K_METEOR_STACK, scan.meteor_stacks)
        if self.upload_meteors:
            add(_K_CLIP, scan.locked_mkvs)
            add(_K_DETAILS, scan.details_mkvs)
            add(_K_FFTHUMB, scan.ffthumbs)
            add(_K_STATE, scan.state_jsons)
        if self.upload_timelapses:
            add(_K_PRODUCT, scan.timelapses)
        if self.upload_rms:
            # One item per night: its files are listed when the item runs, so a
            # pass stopped early never walks the RMS tree for nothing.
            items.extend(_Item(_K_RMS, cam, date, Path(cam, date)) for cam, date in rms_nights)
        items.sort(key=_Item.sort_key)
        return items

    def _execute(self, items: list[_Item]) -> int:
        """Send the plan in order. One item's failure is logged and skipped;
        the pass stops at the next item once it is halted (see _halted)."""
        count = 0
        done: dict[tuple[str, str], dict[str, int]] = {}
        busy: dict[tuple[str, str], int] = {}
        t0 = time.monotonic()
        self._pending_marks = {}
        try:
            for item in items:
                if self._halted():
                    break
                night = (item.camera, item.date)
                try:
                    # Before the pending re-check, so it sees what the
                    # night's previous holder recorded.
                    if not self._take_night(*night):
                        busy[night] = busy.get(night, 0) + 1
                        continue
                    if not self._still_pending(item):
                        continue
                    if item.kind == _K_STATE:
                        # The archive copy of state.json should carry the flags
                        # this pass has recorded so far.
                        self._flush_marks(item.camera, item.date)
                    n = self._send_item(item)
                except Exception:
                    logger.exception('[%s/%s] Uploading %s failed', item.camera, item.date,
                                     item.path.name)
                    continue
                if n:
                    count += n
                    tally = done.setdefault((item.camera, item.date), {})
                    tally[item.kind] = tally.get(item.kind, 0) + n
        finally:
            try:
                self._release_night()
            finally:
                self._flush_marks()
                self._pending_marks = None
        for (camera, date), n in sorted(busy.items()):
            logger.info('[%s/%s] Another uploader holds this night — left %d item(s) to it',
                        camera, date, n)
        for (camera, date), tally in sorted(done.items(), key=lambda kv: kv[0][1], reverse=True):
            summary = ', '.join(f'{n} {kind}' for kind, n in sorted(tally.items()))
            logger.info('[%s/%s] Uploaded %s', camera, date, summary)
        if count:
            logger.info('Upload pass: %d file(s) in %.0fs', count, time.monotonic() - t0)
        return count

    def _clip_stack_subdir(self) -> str:
        """Where a clip's stack goes when it is sent with the clip: stacks/
        (the plot timeline) when stacks upload, else meteors/ next to the clip.
        The dashboard finds a clip's stack in either (routes/archive.py)."""
        return 'stacks' if self.upload_stacks else 'meteors'

    def _still_pending(self, item: _Item) -> bool:
        """Re-check an item right before it is sent.

        The plan is a snapshot: dawn, the daemon and the station API can all be
        uploading the same night, and without this each re-sent what another
        had already shipped (daily duplicate uploads). The flags are re-read
        through the scan cache, so an upload by any process is seen.
        """
        cam, date, path = item.camera, item.date, item.path
        if item.kind in (_K_STATE, _K_RMS):
            return True   # both check their own markers file by file
        if not path.exists():
            return False
        if item.kind == _K_CLIP:
            return (not self._marked_pending(cam, date, flags_manager.MARK_CHUNK, path.name)
                    and not flags_manager.is_chunk_uploaded(cam, date, path.name, self._cfg)
                    and _is_locked(path, cam, date, self._cfg)
                    and self._is_reencoded(path, cam, date))
        if item.kind == _K_STACK:
            chunk = path.name.replace('_stack.webp', '_color.mkv')
            return (not self._marked_pending(cam, date, flags_manager.MARK_CHUNK_STACK, chunk)
                    and not flags_manager.is_chunk_stack_uploaded(cam, date, chunk, self._cfg))
        if item.kind in (_K_METEOR_STACK, _K_PRODUCT):
            product = _product_of(path.name) or ''
            return (not self._marked_pending(cam, date, flags_manager.MARK_PRODUCT, product)
                    and self._product_pending(path, cam, date))
        if item.kind == _K_FFTHUMB:
            return (not self._marked_pending(cam, date, flags_manager.MARK_FFTHUMB, path.name)
                    and not flags_manager.is_ffthumb_uploaded(cam, date, path.name, self._cfg))
        return True

    def _send_item(self, item: _Item) -> int:
        """Upload one plan item; return the number of files it counted."""
        cam, date, path = item.camera, item.date, item.path
        if item.kind == _K_STACK:
            if self._post_one(path, cam, date, 'stacks'):
                self._mark_uploaded(path, cam, date)
                return 1
            return 0
        if item.kind == _K_METEOR_STACK:
            return int(self._upload(path, cam, date, 'stacks'))
        if item.kind == _K_CLIP:
            if not self._upload(path, cam, date, 'meteors'):
                return 0
            n = 1
            stack = path.parent / 'stacks' / path.name.replace('_color.mkv', '_stack.webp')
            if (stack.exists()
                    and not self._marked_pending(cam, date, flags_manager.MARK_CHUNK_STACK, path.name)
                    and not flags_manager.is_chunk_stack_uploaded(cam, date, path.name, self._cfg)):
                if self._upload(stack, cam, date, self._clip_stack_subdir()):
                    n += 1
            return n
        if item.kind == _K_DETAILS:
            # No flags_manager marker: a still-present local .details.mkv IS
            # the "not yet shipped" state, so a crash mid-upload just retries.
            return int(self._upload_details_variant(path, cam, date))
        if item.kind == _K_PRODUCT:
            return int(self._upload(path, cam, date, 'timelapse'))
        if item.kind == _K_FFTHUMB:
            # Backfill for the indexer's live push: a rendered FF thumbnail it
            # could not POST (VPS unreachable, service restart) goes up here.
            if self._post_one(path, cam, date, 'ffthumbs'):
                self._mark_ffthumb_uploaded(path, cam, date)
                return 1
            return 0
        if item.kind == _K_STATE:
            # Unchanged state.json files are skipped (_state_sigs), so this is
            # cheap for already-synced nights.
            self._upload_state_json(cam, date)
            return 0
        if item.kind == _K_RMS:
            return self._upload_rms_files(cam, date)
        raise ValueError(f'unknown upload kind {item.kind!r}')


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _combine_abort(should_abort: Callable[[], bool] | None,
                   stop_event: threading.Event | None) -> Callable[[], bool] | None:
    """One abort check from either form callers hold: a callable (dawn's
    ``BulkDeadline``) or an event (the daemon's stop, ``deadline.expired``)."""
    if should_abort is None and stop_event is None:
        return None

    def _abort() -> bool:
        if stop_event is not None and stop_event.is_set():
            return True
        return bool(should_abort is not None and should_abort())

    return _abort


def run_night(station_id: str, date_str: str, cfg: dict,
              should_abort: Callable[[], bool] | None = None,
              stop_event: threading.Event | None = None) -> int:
    """Upload all pending data for a single station/date (Phase 4 fast path).

    Only touches the specified night — no backlog scanning.  Returns the number
    of files uploaded.

    Held back until this station's dawn slot (see stagger_hold_s): dawn_process
    calls this for every camera in parallel, and every station's dawn_process
    fires within minutes of every other's, which is precisely the burst that
    buried the VPS ingest spool on 2026-09-09. Returning 0 here is a SKIP, not a
    failure — the ``*/15 4-12`` cron retries, and the pass that lands at or
    after the slot does the upload. The stagger is keyed per HOST, so all of a
    station's cameras move together instead of interleaving.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        return 0
    hold = stagger_hold_s(cfg, stagger_id(cfg))
    if hold > 0:
        logger.info('[%s/%s] Dawn stagger: skipping this pass, slot opens in '
                    '%.0fs', station_id, date_str, hold)
        return 0
    count = uploader.cycle_night(station_id, date_str,
                                 should_abort=should_abort, stop_event=stop_event)
    if count:
        logger.info('[%s/%s] Uploaded %d file(s)', station_id, date_str, count)
    return count


def run_once(station_id: str, date_str: str, cfg: dict,
             should_abort: Callable[[], bool] | None = None,
             stop_event: threading.Event | None = None) -> int:
    """Full-scan upload pass (all stations, all nights).

    Called as a backfill sweep after Phase 5 when time permits; pass dawn's
    deadline as ``should_abort`` (or its event as ``stop_event``) so the sweep
    stops at the next file once time is up. Returns the number of files
    uploaded.
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        logger.info('[%s/%s] Archive upload disabled in config', station_id, date_str)
        return 0
    # Gated like run_night: this sweep normally lands well after the dawn
    # window, but a station that finishes its pipeline early would otherwise
    # walk straight past the stagger it just observed.
    hold = stagger_hold_s(cfg, stagger_id(cfg))
    if hold > 0:
        logger.info('[%s/%s] Dawn stagger: skipping the full-scan pass, slot '
                    'opens in %.0fs', station_id, date_str, hold)
        return 0
    logger.info('[%s/%s] Running full-scan upload pass', station_id, date_str)
    count = uploader.cycle(stop_event=stop_event, should_abort=should_abort)
    logger.info('[%s/%s] Upload pass complete: %d file(s)', station_id, date_str, count)
    return count


def upload_chunk(station_id: str, date_str: str, mkv: Path, cfg: dict,
                 force: bool = False) -> bool:
    """Upload a single locked MKV (+ its stack if present) to the archive.

    Called immediately after a manual lock is created so the clip is available
    in the archive without waiting for the next dawn process.
    Re-encodes the clip first (colour calibration + overlay) so the archive
    always receives presentation-quality video, matching dawn_process behaviour.
    A clip that did not re-encode (the encoder failed, or skipped a chunk that
    is still being written) is NOT uploaded: dawn re-encodes it in place later,
    and a clip marked uploaded is never sent again, so the archive would keep
    the raw capture forever. Dawn, or the daemon's next pass, uploads it once
    it is re-encoded.

    The send runs under the night's upload lock (_NightLock), taken without
    waiting, and re-checks the uploaded flag under it: a lock, a play-to-keep
    and a process_clip used to send a clip dawn or the daemon was sending or
    had sent. When another uploader holds the night, the clip is left to it or
    to the daemon's next pass (it is locked and re-encoded, which is what their
    scans pick up). ``force`` (process_clip ``?force=1``) sends a clip already
    flagged uploaded again.

    Returns True once the clip is in the archive or queued that way, False when
    it cannot go (upload disabled, not re-encoded, the send failed).
    """
    uploader = _Uploader(cfg)
    if not uploader._ready_to_upload():
        return False
    if not force and flags_manager.is_chunk_uploaded(station_id, date_str, mkv.name, cfg):
        logger.info('[%s/%s] %s is already in the archive', station_id, date_str, mkv.name)
        return True
    try:
        encoder.process_chunk(mkv, cfg)
    except Exception:
        logger.exception('[%s/%s] Re-encode failed for %s — not uploading the raw capture; '
                         'dawn retries it', station_id, date_str, mkv.name)
        return False
    if not uploader._is_reencoded(mkv, station_id, date_str):
        logger.warning('[%s/%s] %s is not re-encoded yet — leaving it for dawn to encode '
                       'and upload', station_id, date_str, mkv.name)
        return False
    if not uploader._take_night(station_id, date_str):
        logger.info('[%s/%s] Another uploader holds this night — %s is queued for it '
                    'or the next daemon pass', station_id, date_str, mkv.name)
        return True
    try:
        if not force and flags_manager.is_chunk_uploaded(station_id, date_str, mkv.name, cfg):
            logger.info('[%s/%s] %s was uploaded meanwhile', station_id, date_str, mkv.name)
            return True
        ok = uploader._upload(mkv, station_id, date_str, 'meteors')
        stack = mkv.parent / 'stacks' / mkv.name.replace('_color.mkv', '_stack.webp')
        if stack.exists() and not flags_manager.is_chunk_stack_uploaded(
                station_id, date_str, mkv.name, cfg):
            uploader._upload(stack, station_id, date_str, 'meteors')
        uploader._upload_state_json(station_id, date_str)
        uploader._state_sigs.flush()
    finally:
        uploader._release_night()
    return ok


def delete_chunk(station_id: str, date_str: str, filename: str, cfg: dict) -> bool:
    """Mark an unlocked chunk for removal from the archive.

    The station no longer holds an SSH path to the Storage Box, so it cannot
    delete a remote file directly. Retention/reaping of unlocked clips is handled
    VPS-side by the storagebox janitor. Here we clear the local uploaded flags and
    resync state.json so the dashboard reflects the unlock and a re-lock triggers
    a fresh upload. Returns True (the local intent is recorded).
    """
    uploader = _Uploader(cfg)
    if not uploader.enabled:
        return False
    logger.info('[%s/%s] %s unlocked — remote reap deferred to VPS retention',
                station_id, date_str, filename)
    flags_manager.unmark_chunk_uploaded(station_id, date_str, filename, cfg)
    uploader._upload_state_json(station_id, date_str)
    uploader._state_sigs.flush()
    return True


# ---------------------------------------------------------------------------
# Background loop (kept for backward compatibility)
# ---------------------------------------------------------------------------

class _ConfigWatch:
    """Notices when config.json is replaced or rewritten.

    The daemon used to read the config once at start, so a camera added
    later was never uploaded until the unit restarted: gmnro09 (Sibiciu)'s
    RO0013, added on 2026-10-24. config_patch.py replaces the file
    atomically, so a new (inode, mtime, size) means a complete new file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._sig = self._stat()

    def _stat(self) -> tuple[int, int, int] | None:
        try:
            st = self.path.stat()
        except OSError:
            return None
        return (st.st_ino, st.st_mtime_ns, st.st_size)

    def changed(self) -> dict | None:
        """The new config if the file changed since the last call and parses
        to a non-empty object, else None (an unreadable file keeps the old
        config, and is retried while it stays different)."""
        sig = self._stat()
        if sig is None or sig == self._sig:
            return None
        cfg = rovimen_config.load_config(self.path)
        if not cfg:
            return None
        self._sig = sig
        return cfg


def run_upload_loop(cfg: dict, stop_event: threading.Event,
                    config_path: Path | None = None) -> None:
    """Background upload loop. Runs until stop_event is set.

    With ``config_path``, the config is re-read before a cycle whenever that
    file changes (see _ConfigWatch), so new cameras and archive settings take
    effect without a restart.

    karbranth: periodic archive transfer loop.
    """
    watch = _ConfigWatch(config_path) if config_path is not None else None
    arc = cfg.get('archive', {})
    interval = int(arc.get('interval_minutes', 20)) * 60
    uploader = _Uploader(cfg)

    if not uploader.enabled:
        logger.info('Archive upload disabled in config')
        return

    # Dawn-burst stagger (see the block above stagger_hold_s). Set
    # archive.stagger_window_minutes to 0 to opt a station out — e.g. one being
    # commissioned, where waiting an hour for the first clips would just be
    # confusing. The slot is a pure function of the host key and the day, so
    # the daemon and dawn's run_night agree on it and a restart cannot move it.
    upload_id = stagger_id(cfg)
    stagger_window = float(arc.get('stagger_window_minutes',
                                   _STAGGER_WINDOW_MIN_DEFAULT)) * 60
    stagger_offset = _stagger_offset_s(
        upload_id, stagger_window,
        float(arc.get('stagger_jitter_minutes', _STAGGER_JITTER_MIN_DEFAULT)) * 60,
        day=_local_day(),
    )

    logger.info('Archive upload loop started: ingest=%s fallback=%s interval=%ds',
                uploader.ingest_base_url or 'not configured',
                uploader.ingest_fallback_url or 'none', interval)
    if stagger_window > 0:
        logger.info('Dawn stagger: id=%s slot=+%.0fs into a %.0fmin window',
                    upload_id, stagger_offset, stagger_window / 60)

    while not stop_event.is_set():
        try:
            fresh = watch.changed() if watch is not None else None
            if fresh is not None:
                cfg = fresh
                arc = cfg.get('archive', {})
                interval = int(arc.get('interval_minutes', 20)) * 60
                uploader = _Uploader(cfg)
                upload_id = stagger_id(cfg)
                logger.info('config.json changed — reloaded (cameras: %s)',
                            ', '.join(sorted(uploader.stations)) or 'none')
            hold = stagger_hold_s(cfg, upload_id)
            if hold > 0:
                # Inside the dawn window but before this station's slot. Sleep
                # it (interruptibly) rather than hammering the VPS alongside
                # every other station.
                logger.info('Dawn stagger: holding the upload cycle for %.0fs', hold)
                stop_event.wait(hold)
                continue
            # Follow the colour disk going away or coming back
            # (rovimen_config.capture_root's fallback).
            uploader.videocapture_path = rovimen_config.capture_root(cfg)
            count = uploader.cycle(stop_event)
            if count:
                logger.info('Upload cycle: %d file(s) uploaded', count)
        except Exception:
            logger.exception('Upload cycle failed')

        # Sleep in small increments so we can respond to stop quickly
        for _ in range(interval // 5):
            if stop_event.is_set():
                break
            stop_event.wait(5)

    logger.info('Archive upload loop stopped')


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description='ROVIMEN archive uploader')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--daemon', action='store_true',
                        help='Run as a long-lived daemon (loop with interval_minutes)')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(name)s: %(message)s')

    config_path = Path(args.config)
    with open(config_path) as f:
        cfg = json.load(f)

    if args.daemon:
        stop = threading.Event()

        def _shutdown(sig, _frame):
            logger.info('Signal %d — stopping', sig)
            stop.set()

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        run_upload_loop(cfg, stop, config_path=config_path)
    else:
        # Single-pass mode: upload everything and exit
        uploader = _Uploader(cfg)
        if not uploader.enabled:
            logger.info('Archive upload disabled in config')
            return
        count = uploader.cycle()
        logger.info('Single-pass upload complete: %d file(s)', count)


if __name__ == '__main__':
    main()
