#!/usr/bin/env python3
"""janitor_storage_watchdog.py — Disk lifecycle cron job for ROVIMEN stations.

Codename: atium

Runs every 10 minutes via cron. Each run does two things:
  1. Normal sweep: age-based retention for all tiers (videos, stacks, timelapse, RMS)
  2. Disk check: pressure-based escalation, chained until disk is under threshold

Handles single and dual-filesystem layouts:
  - Single fs: videos/stacks/timelapse and RMS data on the same device
  - Dual fs:   videocapture_path on external drive, home on root drive —
               each filesystem monitored and escalated independently

Cron entry (installed to /etc/cron.d/rovimen-janitor):
  */10 * * * * {user} nice -n 19 ionice -c 2 -n 7 {venv}/bin/python {scripts}/janitor_storage_watchdog.py -c {scripts}/config.json

Standalone:
  python janitor_storage_watchdog.py -c config.json
"""

from __future__ import annotations

import argparse
import fcntl
import gzip
import json
import logging
import logging.handlers
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import rovimen_lock
import flags_manager
import rovimen_config
import rovimen_night

logger = logging.getLogger('atium')


def _is_locked(mkv: Path, station_id: str, date_str: str, cfg: dict) -> bool:
    """Whether the janitor must treat a chunk as a detection: locked in
    state.json or by a legacy ``.locked`` sidecar, or in a night whose
    state.json exists but cannot be read. That night's locks are unknown,
    not absent, so every chunk in it is held like a detection (fail closed);
    only the last-resort pressure tiers, which take detections too, reach it.
    """
    if flags_manager.night_unreadable(station_id, date_str, cfg):
        return True
    return flags_manager.is_chunk_locked(mkv, station_id, date_str, cfg)

RETENTION_DAYS = 7

# Emitted at ERROR level for the one case in which the janitor destroys the
# only copy of a meteor: an un-uploaded, detection-locked chunk evicted under
# disk pressure. Fleet alerting keys on this exact token — do not reword it.
DATA_LOSS_MARKER = 'ROVIMEN-DATA-LOSS'

# Capture-side daemon logs are kept this long. Matches the 14 rotated days the
# janitor already keeps of its own janitor.log, so every log on a station has
# the same diagnostic window regardless of which daemon wrote it. Overridable
# with retention.log_days; 0 disables the pass entirely.
LOG_RETENTION_DAYS = 14

# Logs written under fixed names that their writers append to forever: the
# `>> ~/logs/<name>` crons toggle_rovimen.sh installs, and dawn_process's
# per-module FileHandlers. Nothing rotated them; on 2026-10-05 gmnro04
# (Vâlcele) held 165 MB of rovimen_dawn.log, 129 MB of encoder.log and 72 MB of
# stacker.log (707 MB in ~/logs). Past retention.log_rotate_mb (0 disables) each
# is gzipped beside itself and emptied in place, see _copytruncate_gz.
_APPEND_LOGS = (
    'rovimen_dawn.log', 'rovimen_janitor.log', 'rovimen_reboot.log',
    'rovimen_fixcam.log', 'updater.log',
    'stacker.log', 'encoder.log', 'timelapse.log',
)
LOG_ROTATE_MB = 20

# Capture-side log files the janitor may delete, as globs relative to log_base.
# This is an allowlist, not a filter: anything not matched here — video, state,
# calibration, an operator's scratch file parked in the log dir — is never a
# deletion candidate. Both the new per-night names and the legacy undated files
# they replace are covered, so the first sweep after an update reclaims the
# hundreds of megabytes the old append-forever logs left behind.
_LOG_PRUNE_GLOBS = (
    'color_capture/ffmpeg_*.log',   # per-camera ffmpeg stderr (dated + legacy)
    'color_capture/[0-9]*.log',     # colour capture's per-night daemon log
    'stacker*.log',                 # stacker stdout/stderr (dated + legacy)
    'janitor.log.[0-9]*',           # own rotated logs (see _ROTATED_JANITOR_RE)
) + tuple(f'{name}.[0-9]*.gz' for name in _APPEND_LOGS)  # rotated append logs

# On a color_tee station every colour chunk is a hardlink of an RMS raw segment
# under <rms_data_path>/VideoFiles, so unlinking the chunk alone frees nothing
# until RMS deletes its copy, which it only does at its own start and only as
# far as its next-night estimate needs. gmnro05 (Bistrița) on 2026-10-04: the
# pressure eviction deleted 232 chunks in one pass with free space unchanged at
# 69.9 GB, then went on to the only copies of 295 detections, freeing nothing
# either; 69 GB of raw segments whose chunks were long gone sat in VideoFiles.
# A raw segment whose chunk is gone is removed once it is this old, so a
# segment the tee watcher has not linked yet is never touched.
TEE_ORPHAN_MIN_AGE_H = 2

# Night products timelapse_build writes into the night dir beside the chunks.
# They follow timelapse_days and wait for their upload (the timelapse branch
# of _purge_station), so the flat-layout sweep, which deletes the dir's other
# files at the colour date cap, leaves them alone.
_NIGHT_PRODUCT_SUFFIXES = ('_timelapse.mp4', '_night_stack.webp')

# The janitor's own rotated logs are named janitor.log.YYYYMMDD — they end in
# the date, not in .log, so the suffix gate below needs this name-anchored
# exception. Kept deliberately exact: eight digits, nothing else.
_ROTATED_JANITOR_RE = re.compile(r'^janitor\.log\.\d{8}$')

# Rotated copies of the append logs: <name>.YYYYMMDD_HHMMSS.gz, or
# <name>.YYYYMMDD.gz from a hand rotation. Name-anchored the same way.
_ROTATED_APPEND_RE = re.compile(
    r'^(%s)\.\d{8}(?:_\d{6})?\.gz$' % '|'.join(re.escape(n) for n in _APPEND_LOGS))


def _copytruncate_gz(path: Path, dest: Path) -> int:
    """Gzip ``path`` into ``dest``, then empty ``path`` in place; return bytes rotated.

    In place rather than renamed, because the writers keep the file open: dawn
    runs for hours with append-mode handlers, and cron's ``>>`` is append mode
    too, so after the truncate they carry on at the new end. Bytes appended
    while compressing are written back after the truncate; only a write landing
    between that last read and the truncate is lost.
    """
    tmp = dest.with_name(dest.name + '.tmp')
    try:
        with path.open('rb') as src:
            size = os.fstat(src.fileno()).st_size
            with gzip.open(tmp, 'wb') as gz:
                remaining = size
                while remaining > 0:
                    block = src.read(min(1 << 20, remaining))
                    if not block:
                        break
                    gz.write(block)
                    remaining -= len(block)
            tail = src.read()
        os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    with path.open('r+b') as f:
        f.truncate(0)
    if tail:
        with path.open('ab') as f:
            f.write(tail)
    return size


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DATE_IN_NAME = re.compile(r'(?<!\d)(\d{8})(?!\d)')
_YEAR_DIR = re.compile(r'^\d{4}$')


def _dated_rms_dirs(subdir: Path) -> list[tuple[Path, str]]:
    """(dir, YYYYMMDD) for every dated directory under an RMS FramesFiles or
    TimeFiles dir. RMS nests them as ``<year>/<YYYYMMDD-doy>/``, so a 4-digit
    year dir is descended into rather than compared itself. A name that carries
    no 8-digit date is never returned, so it is never deleted.

    Comparing the top-level names directly (the pre-2026-09-23 code) compared
    "2026" with a date like "20260921": the year always sorted first, so every
    soft prune deleted the whole year, today's day frames included, and day
    timelapses never got built on stations that sit above 85% disk.
    """
    out: list[tuple[Path, str]] = []
    for entry in subdir.iterdir():
        if not entry.is_dir():
            continue
        if _YEAR_DIR.match(entry.name):
            for child in entry.iterdir():
                m = _DATE_IN_NAME.search(child.name)
                if child.is_dir() and m:
                    out.append((child, m.group(1)))
            continue
        m = _DATE_IN_NAME.search(entry.name)
        if m:
            out.append((entry, m.group(1)))
    return out


# The rolling release keeps this much more free space than the larger of:
# RMS's own next-night estimate, when RMS writes to the colour filesystem
# (below it, RMS's DeleteOldObservations deletes its VideoFiles, CapturedFiles
# and ArchivedFiles at capture start; gmnro05 (Bistrița) logged "Need 50.03 GB
# for next night" for a 13.0 h night on 2026-11-01); the free space at
# warn_pct, so the harsher pressure eviction never runs; and min_free_gb.
COLOR_KEEP_MARGIN_GB = 5.0
# Least date cap for non-detection chunks on a rolling station: color_days 1
# would delete the tail of the previous night at 00:00 UTC.
COLOR_MAX_DAYS = 2
_RMS_NEED_RE = re.compile(rb'Need ([0-9.]+) GB for next night')
# RMS logs searched for the estimate, newest first, and bytes read from each
# (the line comes minutes after the log opens).
_RMS_NEED_LOGS = 6
_RMS_NEED_READ_BYTES = 4 * 1024 * 1024


def _rms_next_night_need_gb(log_dir: Path) -> float | None:
    """RMS's latest "Need X GB for next night" in *log_dir*, in GB (RMS
    prints GiB), or None."""
    try:
        logs = sorted((f for f in log_dir.glob('log_*.log') if f.is_file()),
                      key=lambda f: f.name, reverse=True)
    except OSError:
        return None
    for f in logs[:_RMS_NEED_LOGS]:
        try:
            with open(f, 'rb') as fh:
                data = fh.read(_RMS_NEED_READ_BYTES)
        except OSError:
            continue
        found = _RMS_NEED_RE.findall(data)
        if found:
            try:
                return float(found[-1]) * 1024 ** 3 / 1e9
            except ValueError:
                continue
    return None
# The rolling release stops when it has deleted this much without the free
# space rising by a quarter of it (a chunk whose bytes something else holds).
ROLLING_STALL_BYTES = 5 * 10**9


def _cutoff(days: int) -> str:
    """Return YYYYMMDD cutoff: dates strictly before this are eligible for deletion."""
    return (datetime.now(timezone.utc) - timedelta(days=days)).strftime('%Y%m%d')


# RMS's UploadManager logs every archive it sends to GMN. A verified upload
# ends in "File upload verified: files/<name>" (DEBUG, RMS's default level), a
# file the server already holds at the same size in "The file 'files/<name>'
# already exists on the server and is the same size. Skipping.", and at INFO
# "Starting upload of <local path> (...) to ..." is followed by "Upload
# successful!" or by one of the failure lines.
_RMS_VERIFIED_RE = re.compile(r'File upload verified: \S*?([^/\s]+\.tar\.bz2)')
_RMS_SKIPPED_RE = re.compile(r"The file '[^']*?([^/']+\.tar\.bz2)' already exists on the server")
_RMS_START_RE = re.compile(r'Starting upload of \S*?([^/\s]+\.tar\.bz2) ')
_RMS_FAILED = (b'Uploading failed!', b'File verification failed', b'Exception during SFTP upload')
_RMS_LOG_DATE_RE = re.compile(r'_(\d{8})_\d{6}_\d+\.log$')
# RMS uploads a night's archives the next morning and retries for a few days.
_RMS_UPLOAD_LOG_DAYS = 3


def _rms_log_uploads(log_file: Path) -> set[str]:
    """Names of the .tar.bz2 archives one RMS log shows as sent to GMN."""
    sent: set[str] = set()
    pending: str | None = None
    try:
        with open(log_file, 'rb') as fh:
            for raw in fh:
                if b'Upload' not in raw and b'upload' not in raw and b'already exists' not in raw:
                    continue
                line = raw.decode('utf-8', 'replace')
                if 'File upload verified' in line or 'already exists on the server' in line:
                    m = _RMS_VERIFIED_RE.search(line) or _RMS_SKIPPED_RE.search(line)
                    if m:
                        sent.add(m.group(1))
                elif 'Starting upload of' in line:
                    m = _RMS_START_RE.search(line)
                    pending = m.group(1) if m else None
                elif 'Upload successful!' in line:
                    if pending:
                        sent.add(pending)
                    pending = None
                elif any(f in raw for f in _RMS_FAILED):
                    pending = None
    except OSError:
        logger.warning('Could not read RMS log %s', log_file)
    return sent


def _rms_logs_for(log_dir: Path, date: str) -> list[Path]:
    """RMS logs started on *date* (YYYYMMDD) or up to _RMS_UPLOAD_LOG_DAYS after."""
    try:
        last = (datetime.strptime(date, '%Y%m%d')
                + timedelta(days=_RMS_UPLOAD_LOG_DAYS)).strftime('%Y%m%d')
    except ValueError:
        return []
    if not log_dir.is_dir():
        return []
    logs = []
    for f in log_dir.glob('*.log'):
        m = _RMS_LOG_DATE_RE.search(f.name)
        if m and date <= m.group(1) <= last:
            logs.append(f)
    return sorted(logs)


def _sent_to_gmn_proof(rms_data: Path, session: Path, date: str,
                       log_cache: dict[Path, set[str]]) -> str | None:
    """How we know an RMS session reached GMN, or None when we don't.

    Proof is the session's archive set, either still in ArchivedFiles or shown
    as uploaded in RMS's own logs (*log_cache* holds each log's parse across
    sessions). Current RMS sends an _imgdata and a _metadata archive; RMS
    before the 2026-10 split sent one _detected archive, which held every
    gmnmd01 (Cahul) session of 10-12 to 10-16 (84 GB a camera) as unsent.
    """
    archive_sets = (
        {f'{session.name}_imgdata.tar.bz2', f'{session.name}_metadata.tar.bz2'},
        {f'{session.name}_detected.tar.bz2'},
    )
    for archives in archive_sets:
        if all((rms_data / 'ArchivedFiles' / a).exists() for a in archives):
            return 'archives in ArchivedFiles'
    logged: set[str] = set()
    for lf in _rms_logs_for(rms_data / 'logs', date):
        if lf not in log_cache:
            log_cache[lf] = _rms_log_uploads(lf)
        logged |= log_cache[lf]
    if any(archives <= logged for archives in archive_sets):
        return 'RMS upload log'
    return None


# Regex for the colour-chunk / splice filename convention:
#   <CAM>_<YYYYMMDD>_<HHMMSS>_color.mkv            (regular chunk, start time)
#   <CAM>_<YYYYMMDD>_<HHMMSS>_<HHMMSS>_color.mkv   (boundary splice, start_end)
# Group 1 = date (UTC), group 2 = start HHMMSS. Age is measured from the start.
_CHUNK_TS_RE = re.compile(r'_(\d{8})_(\d{6})(?:_\d{6})?_color\.mkv$')


def _chunk_start_dt(name: str) -> datetime | None:
    """Parse the UTC start datetime encoded in a colour-chunk / splice filename.

    Returns None for names that don't match the convention (e.g. sidecars).
    Filenames are the source of truth for clip age — they carry the capture
    time and are immutable, unlike mtime which a copy/rsync can reset.
    """
    m = _CHUNK_TS_RE.search(name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S').replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


def _file_age_dt(f: Path) -> datetime | None:
    """Best-effort UTC timestamp for a file: filename start-time first, mtime
    as a fallback for anything without an encoded timestamp."""
    dt = _chunk_start_dt(f.name)
    if dt is not None:
        return dt
    try:
        return datetime.fromtimestamp(f.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return None


def _fs_stats(path: Path) -> tuple[int, float]:
    """Return (df_pct, avail_gb) for the filesystem containing *path*.

    df_pct matches what `df` reports for the NON-root pipeline user, unlike
    ``shutil.disk_usage(...).used * 100 // total``. ext4 reserves ~5% of blocks
    for root (f_bfree > f_bavail); that reserve is free-but-unusable by the
    ``gmn``/``rovimen`` capture user. The old metric divided by the reserve-
    inclusive total, so at a true ``df`` 100% (ENOSPC for the pipeline) it read
    only ~95% and the eviction thresholds never fired — the exact wedge that
    let gmnro16's 29 GB USB fill to 100% and crash dawn on ENOSPC.

    df computes the percentage against the user-usable size (used + available),
    which excludes the reserve, so it reaches 100% precisely when the pipeline
    can no longer write. avail_gb is the non-root free space (statvfs f_bavail),
    the concrete headroom before ENOSPC.
    """
    s = os.statvfs(path)
    frsize = s.f_frsize
    avail = s.f_bavail * frsize                 # non-root available (== df Avail)
    used = (s.f_blocks - s.f_bfree) * frsize    # used excluding the root reserve
    usable = used + avail                       # what df divides by
    pct = int(used * 100 // usable) if usable > 0 else 100
    return pct, avail / 1e9


def _disk_pct(path: Path) -> int:
    """Return disk usage percentage (0–100) for the filesystem containing path.

    Uses the df-equivalent metric (see :func:`_fs_stats`) so a full disk for the
    pipeline user actually reads as full, letting the pressure escalation fire
    before the capture pipeline hits ENOSPC.
    """
    return _fs_stats(path)[0]


def _existing_parent(p: Path) -> Path:
    """Return p itself if it exists, else the nearest existing ancestor."""
    while not p.exists():
        p = p.parent
    return p


def _safe_iterdir(d: Path) -> list[Path]:
    """Like list(d.iterdir()) but returns [] on I/O errors instead of crashing."""
    try:
        return list(d.iterdir())
    except OSError:
        logger.warning('I/O error listing %s — skipping directory', d)
        return []


def _is_night_dir(d: Path) -> bool:
    """True for a YYYYMMDD camera-night directory."""
    try:
        return d.name.isdigit() and len(d.name) == 8 and d.is_dir()
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Janitor
# ---------------------------------------------------------------------------

# Limits while colour capture is on the fallback root (rovimen_config.
# capture_root), overridable per station under capture_fallback.retention /
# .disk / .floor_gb. The whole current night is kept (protect_hours 16): on
# gmnro01 (Ghirdoveni) six colour cameras write ~220 GB a night onto a 468 GB
# SSD that RMS also needs, and with the previous night's RMS sessions freed at
# 00:00 a night peaks at ~83 %. Where a night does not fit, the floor
# (_evict_for_rms) gives up tonight's non-detection colour before RMS
# runs out of space.
FALLBACK_RETENTION = {'color_days': 1, 'uploaded_days': 1, 'locked_days': 3}
FALLBACK_DISK = {'warn_pct': 85, 'protect_hours': 16, 'min_free_gb': 25}
FALLBACK_FLOOR_GB = 15.0
# Free space kept for RMS on a disk it shares with colour: this much over its
# largest "Need X GB for next night" (see Janitor._evict_for_rms). A night
# writes up to ~2 GB per 10-min janitor run on a three-camera station, so 5 GB
# covers two runs.
RMS_FLOOR_MARGIN_GB = 5.0
# Tonight's chunks younger than this stay even below the RMS floor: the night
# stacker and RMS's live detection work a little behind capture.
RMS_FLOOR_MIN_AGE_H = 1.0


class Janitor:
    def __init__(self, config_path: str) -> None:
        self.config_path = config_path
        with open(config_path) as f:
            self.cfg = json.load(f)

        self.capture_path = rovimen_config.capture_root(self.cfg)
        self.log_base = rovimen_config.log_dir(self.cfg)

        ret = self.cfg.get('retention', {})
        disk = self.cfg.get('disk', {})
        # Colour diverted to the fallback root (the colour disk is missing):
        # that disk also holds RMS and is not sized for a 10-day colour
        # retention, so tighter limits apply (FALLBACK_*).
        self.on_fallback = rovimen_config.capture_on_fallback(self.cfg)
        if self.on_fallback:
            fb = self.cfg.get('capture_fallback') or {}
            ret = {**ret, **FALLBACK_RETENTION, **(fb.get('retention') or {})}
            disk = {**disk, **FALLBACK_DISK, **(fb.get('disk') or {})}
            logger.warning('Colour capture is on the fallback root %s: retention %s, disk %s',
                           self.capture_path, ret, disk)
        self.fallback_floor_gb = float(
            (self.cfg.get('capture_fallback') or {}).get('floor_gb', FALLBACK_FLOOR_GB))
        rms_floor = disk.get('rms_floor_gb')
        self.rms_floor_override = float(rms_floor) if rms_floor is not None else None
        self.color_retention    = ret.get('color_days',    self.cfg.get('color_retention_days', 2))
        self.locked_retention   = ret.get('locked_days',   self.cfg.get('locked_retention_days', 7))
        # Uploaded chunks stay locally for this many days ("7 days local iff
        # uploaded, forever on storagebox") — deleting them on sight raced the
        # faststart remux and left viewers with no local fallback. Under disk
        # pressure the ordered eviction still reclaims them.
        self.uploaded_retention = ret.get('uploaded_days',  self.cfg.get('uploaded_retention_days', 7))
        self.stack_retention    = ret.get('stacks_days',   self.cfg.get('stack_retention_days', RETENTION_DAYS))
        self.tl_retention       = ret.get('timelapse_days', self.cfg.get('timelapse_retention_days', RETENTION_DAYS))
        self.captured_retention = ret.get('captured_days', 1)
        # Rolling release of non-detection chunks (default on; retention
        # "rolling": false, or the older "color_after_morning": false, turns
        # it off): once a camera-night's morning sequence is done (and its
        # EON lock pass ran, see _night_released), its
        # non-detection chunks are deleted oldest first, one at a time, only
        # while free space is under the keep-free target (see
        # _rolling_release), so each lives as long as the disk allows and a
        # Process click can still fetch a fireball RMS did not lock.
        # color_max_days (default color_days, at least COLOR_MAX_DAYS) is the
        # date cap.
        self.rolling = bool(ret.get('rolling', ret.get('color_after_morning', True)))
        keep = ret.get('color_keep_free_gb')
        self.color_keep_free_gb = float(keep) if keep is not None else None
        self.color_max_days = int(ret.get(
            'color_max_days', max(int(self.color_retention), COLOR_MAX_DAYS)))
        self._keep_free_cache: float | None = None
        self._rms_need_cache: float | None | bool = False   # False: not read yet
        self.log_retention     = ret.get('log_days',      self.cfg.get('log_retention_days', LOG_RETENTION_DAYS))
        rotate_mb = ret.get('log_rotate_mb')
        self.log_rotate_bytes   = int(float(LOG_ROTATE_MB if rotate_mb is None else rotate_mb) * 1024 * 1024)
        self.archive_enabled    = self.cfg.get('archive', self.cfg.get('central_archive', {})).get('enabled', False)

        self.warn_pct         = disk.get('warn_pct',         self.cfg.get('disk_warn_pct', 85))
        # nuclear_pct / extreme_pct are only reported (janitor_state.json, the
        # dashboard's disk thresholds): no eviction tier keys on them since the
        # ordered eviction replaced the date-dir tiers.
        self.nuclear_pct      = disk.get('nuclear_pct',      self.cfg.get('disk_nuclear_pct', 90))
        self.extreme_pct      = disk.get('extreme_pct',      self.cfg.get('disk_extreme_pct', 95))
        self.home_warn_pct    = disk.get('home_warn_pct',    80)
        self.home_nuclear_pct = disk.get('home_nuclear_pct', 90)
        # Absolute free-bytes floor: eviction runs if avail drops below this many
        # GB even when the percentage hasn't crossed warn_pct. Belt-and-braces for
        # small disks where a few percent still leaves too little headroom before
        # ENOSPC. 0 disables (percentage-only). Also relieves down to this floor.
        self.min_free_gb      = float(disk.get('min_free_gb', self.cfg.get('min_disk_gb_free', 0)) or 0)
        # Hard-invariant window: never delete anything younger than this many
        # hours. Protects the entire current night (detections + non-detections).
        self.protect_hours    = float(disk.get('protect_hours', 24))

        # (filesystem, pct, free GB) when this run's escalation left a disk
        # over its limit; _write_state turns it into the 'pressure' record.
        self._unrelieved: tuple[str, int, float] | None = None

        self.station_ids: list[str] = list(self.cfg.get('stations', {}).keys())
        self.color_tee = bool(self.cfg.get('color_tee', False))
        self._tee_segments_cache: dict[str, dict[str, list[Path]]] = {}

        # Detect whether capture path and home are on the same filesystem.
        # Walk up to nearest existing parent in case capture_path hasn't been
        # created yet on first boot.
        capture_dev = os.stat(_existing_parent(self.capture_path)).st_dev
        home_dev    = os.stat(Path.home()).st_dev
        self.same_fs = (capture_dev == home_dev)
        # Whether any camera's RMS data shares the capture disk. Not the same
        # question as same_fs: gmnro09 (Sibiciu) had ~/color_capture and
        # ~/RMS_data both symlinked onto one USB disk, so same_fs was False and
        # capture-disk pressure never freed RMS sessions already at GMN; the
        # disk filled at 03:17 UTC on 2026-11-11 and phase 2 spent detection
        # clips instead.
        self.rms_on_capture_fs = any(
            os.stat(_existing_parent(Path(p))).st_dev == capture_dev
            for p in (
                (s or {}).get('rms_data_path')
                for s in self.cfg.get('stations', {}).values()
            )
            if p
        )

        self._setup_logging()

    def _setup_logging(self) -> None:
        self.log_base.mkdir(parents=True, exist_ok=True)
        log_file = self.log_base / 'janitor.log'

        logger.setLevel(logging.DEBUG)

        fh = logging.handlers.TimedRotatingFileHandler(
            log_file, when='midnight', backupCount=14, utc=True,
        )
        fh.suffix = '%Y%m%d'
        # getFilesToDelete() (Python >= 3.9) splits the base name at its LAST
        # dot, so for janitor.log the candidate suffix is 'log.YYYYMMDD', not
        # '.YYYYMMDD' — the old pattern matched nothing and backupCount was
        # inert (stations accumulated months of rotated logs). Accept both
        # shapes so older splitext behaviour stays covered too.
        fh.extMatch = re.compile(r'^(log\.)?\d{8}$')
        fh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(fh)

        sh = logging.StreamHandler()
        sh.setLevel(logging.INFO)
        sh.setFormatter(logging.Formatter(
            '%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S',
        ))
        logger.addHandler(sh)

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> None:
        # atium: disk lifecycle cron — sweep then check
        if not self.capture_path.exists():
            logger.warning(
                'capture path %s does not exist — external drive may be unmounted; skipping',
                self.capture_path,
            )
            return
        logger.debug(
            'atium run: capture=%s  same_fs=%s  rms_on_capture_fs=%s  cameras=%s',
            self.capture_path, self.same_fs, self.rms_on_capture_fs, self._camera_ids(),
        )
        # Snapshot disk usage before sweep so we can report bytes freed
        try:
            _before = shutil.disk_usage(self.capture_path).used
        except OSError:
            _before = 0

        # One parse of each night's state.json per run instead of one per
        # chunk per check (see flags_manager.scan_cache).
        with flags_manager.scan_cache():
            self._run_normal_sweep()
            try:
                self._rolling_release()
            except Exception:
                logger.exception('Error in rolling colour release')
            try:
                self._evict_for_rms()
            except Exception:
                logger.exception('Error in RMS floor eviction')
            self._run_disk_check()
            if not self.same_fs:
                self._run_home_check()

        try:
            _after = shutil.disk_usage(self.capture_path).used
            _deleted = max(0, _before - _after)
        except OSError:
            _deleted = 0
        self._write_state(_deleted)

    # ------------------------------------------------------------------
    # State file
    # ------------------------------------------------------------------

    def _write_state(self, deleted_bytes: int = 0) -> None:
        """Write a lightweight JSON state file for the station API to serve."""
        state: dict = {
            'last_run': datetime.now(timezone.utc).isoformat(),
            'deleted_gb': round(deleted_bytes / 1e9, 3),
            'filesystems': {},
        }

        def _fs_entry(path: Path, warn: int, nuclear: int, extreme: int | None) -> dict:
            usage = shutil.disk_usage(path)
            total_gb = usage.total / 1e9
            # Report the df-accurate percentage (reserve-corrected) so the
            # dashboard shows the same fullness that triggers eviction.
            pct, avail_gb = _fs_stats(path)
            gb_until_warn = round(max(0.0, (warn - pct) / 100 * (avail_gb + usage.used / 1e9)), 1) \
                if pct < warn else 0.0
            return {
                'pct':           pct,
                'total_gb':      round(total_gb, 1),
                'free_gb':       round(avail_gb, 1),
                'warn_pct':      warn,
                'nuclear_pct':   nuclear,
                'extreme_pct':   extreme,
                'gb_until_warn': gb_until_warn,
            }

        try:
            state['filesystems'][str(self.capture_path)] = _fs_entry(
                self.capture_path,
                self.warn_pct, self.nuclear_pct, self.extreme_pct,
            )
        except OSError:
            pass

        if not self.same_fs:
            try:
                state['filesystems'][str(Path.home())] = _fs_entry(
                    Path.home(),
                    self.home_warn_pct, self.home_nuclear_pct, None,
                )
            except OSError:
                pass

        state_path = self.log_base / 'janitor_state.json'
        state['pressure'] = self._pressure_record(state_path, state['last_run'])

        try:
            tmp = state_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(state, indent=2))
            os.replace(tmp, state_path)
        except Exception:
            pass

    def _pressure_record(self, state_path: Path, now_iso: str) -> dict | None:
        """The ongoing "escalation could not relieve the disk" episode, or None.

        From 2026-10-11 to 10-16 the janitor logged this ERROR about 120 times
        a day on gmnro14 (Chiribiș), five days running, and nobody saw it until
        the disk hit 100% and hung RMS. The record carries the episode's start
        across runs, so the station status and the dashboard can show how long
        the station has been out of room; a relieved run clears it.
        """
        if self._unrelieved is None:
            return None
        fs, pct, free_gb = self._unrelieved
        since, runs = now_iso, 1
        try:
            prev = json.loads(state_path.read_text()).get('pressure') or {}
            if prev.get('since'):
                since, runs = prev['since'], int(prev.get('runs', 0)) + 1
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return {'since': since, 'runs': runs, 'filesystem': fs,
                'pct': pct, 'free_gb': round(free_gb, 1)}

    # ------------------------------------------------------------------
    # Normal sweep — age-based retention
    # ------------------------------------------------------------------

    def _camera_ids(self) -> list[str]:
        """Configured cameras, then every other camera dir under the capture root.

        A renamed or removed camera leaves its nights under the old code
        (gmnro20: RO0020 became RO001M in September, RO20XB became RO001N on
        2026-10-06). Walking only config["stations"] hid those nights from
        retention and from pressure eviction, and about 96 GB of them filled
        gmnro20's disk at 00:59 on 2026-10-07. Any dir holding a YYYYMMDD
        night dir counts as a camera; the same retention rules apply to it.
        """
        ids = list(self.station_ids)
        for d in sorted(_safe_iterdir(self.capture_path)):
            if d.name in ids or d.name.startswith('.'):
                continue
            try:
                if not d.is_dir():
                    continue
            except OSError:
                continue
            if any(_is_night_dir(n) for n in _safe_iterdir(d)):
                ids.append(d.name)
        return ids

    def _run_normal_sweep(self) -> None:
        for sid in self._camera_ids():
            try:
                self._purge_station(sid)
            except Exception:
                logger.exception('[%s] Error in normal sweep', sid)
            if sid not in self.station_ids:
                try:
                    (self.capture_path / sid).rmdir()
                    logger.info('[%s] Removed empty dir of an unconfigured camera', sid)
                except OSError:
                    pass
        try:
            self._purge_tee_orphans()
        except Exception:
            logger.exception('Error in tee raw-segment sweep')
        try:
            self._purge_ffthumbs()
        except Exception:
            logger.exception('Error in FF thumbnail sweep')
        try:
            self._purge_captured_files()
        except Exception:
            logger.exception('Error in CapturedFiles sweep')
        try:
            self._rotate_append_logs()
        except Exception:
            logger.exception('Error in append-log rotation')
        try:
            self._purge_capture_logs()
        except Exception:
            logger.exception('Error in capture-log sweep')

    # ------------------------------------------------------------------
    # color_tee: chunks are hardlinks of RMS raw segments (TEE_ORPHAN_MIN_AGE_H)
    # ------------------------------------------------------------------

    def _rms_video_root(self, station_id: str) -> Path | None:
        rms_data = self.cfg.get('stations', {}).get(station_id, {}).get('rms_data_path')
        if not rms_data:
            return None
        return Path(rms_data) / self.cfg.get('rms_video_dir', 'VideoFiles')

    def _tee_segments(self, station_id: str) -> dict[str, list[Path]]:
        """RMS raw segments of a tee station by the colour chunk name they map to."""
        cached = self._tee_segments_cache.get(station_id)
        if cached is not None:
            return cached
        index: dict[str, list[Path]] = {}
        root = self._rms_video_root(station_id)
        if self.color_tee and root is not None and root.is_dir():
            for seg in root.rglob('*_video.mkv'):
                m = rovimen_night.VIDEO_SEGMENT_RE.match(seg.name)
                if m:
                    index.setdefault(f'{m.group(1)}_{m.group(2)}_{m.group(3)}_color.mkv', []).append(seg)
        self._tee_segments_cache[station_id] = index
        return index

    def _unlink_chunk(self, station_id: str, f: Path) -> bool:
        """Delete a colour chunk and, on a tee station, the RMS raw segment that
        is the same file, so the deletion frees the bytes it is counted for.

        Returns False, deleting nothing, when the chunk is already gone or an
        encoder holds it (encoder._chunk_encode_lock, an exclusive flock on the
        chunk file itself): the encoder renames its finished clip over the
        chunk's path, which brought a chunk deleted mid-encode back. The lock is
        held across the unlink, so an encoder waiting for it finds the chunk
        gone. A chunk that cannot be opened or locked is deleted unguarded, as
        before."""
        try:
            fd: int | None = os.open(f, os.O_RDONLY)
        except FileNotFoundError:
            return False
        except OSError:
            fd = None
        try:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    logger.info('[%s] Not deleting %s: an encoder is working on it',
                                station_id, f.name)
                    return False
                except OSError:
                    pass
            try:
                st = f.stat()
            except OSError:
                st = None
            f.unlink(missing_ok=True)
        finally:
            if fd is not None:
                os.close(fd)
        if st is None or st.st_nlink < 2 or not self.color_tee:
            return True
        for seg in self._tee_segments(station_id).get(f.name, ()):
            try:
                seg_st = seg.stat()
            except OSError:
                continue
            if (seg_st.st_dev, seg_st.st_ino) == (st.st_dev, st.st_ino):
                seg.unlink(missing_ok=True)
                logger.debug('[%s] Removed RMS raw twin %s of %s', station_id, seg.name, f.name)
        return True

    def _purge_tee_orphans(self) -> None:
        """Delete RMS raw segments whose colour chunk is already gone.

        Only segments the tee watcher recorded as linked (its .tee_linked.log)
        and that are no longer linked anywhere else (st_nlink 1) qualify: the
        janitor or an eviction removed their chunk, so RMS's name is the last
        one keeping the bytes. Segments on another filesystem than the colour
        tree were copied, not linked, and are left to RMS.
        """
        if not self.color_tee:
            return
        cutoff = datetime.now(timezone.utc) - timedelta(hours=TEE_ORPHAN_MIN_AGE_H)
        for sid in self.station_ids:
            root = self._rms_video_root(sid)
            cap_station = self.capture_path / sid
            if root is None or not root.is_dir() or not cap_station.is_dir():
                continue
            if os.stat(root).st_dev != os.stat(cap_station).st_dev:
                continue
            try:
                with open(cap_station / '.tee_linked.log') as fh:
                    linked = {line.strip() for line in fh if line.strip()}
            except OSError:
                continue
            removed = 0
            freed = 0
            for segs in self._tee_segments(sid).values():
                for seg in segs:
                    if seg.name not in linked:
                        continue
                    m = rovimen_night.VIDEO_SEGMENT_RE.match(seg.name)
                    try:
                        start = datetime.strptime(m.group(2) + m.group(3), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
                        seg_st = seg.stat()
                    except (OSError, ValueError, AttributeError):
                        continue
                    if seg_st.st_nlink > 1 or start >= cutoff:
                        continue
                    seg.unlink(missing_ok=True)
                    removed += 1
                    freed += seg_st.st_size
            if removed:
                logger.info('[%s] Removed %d RMS raw segment(s) whose colour chunk is gone (%.1f GB)',
                            sid, removed, freed / 1024 ** 3)

    def _purge_ffthumbs(self) -> None:
        """BW FF thumbnails live outside the capture root
        (rovimen_config.ffthumb_root) so they exist without colour video.
        Same retention as the stack thumbnails: the archive copy is the
        canonical one, the local render is only kept for a re-push."""
        root = rovimen_config.ffthumb_root(self.cfg)
        stack_cutoff = _cutoff(self.stack_retention)
        for cam_dir in _safe_iterdir(root):
            for night in _safe_iterdir(cam_dir):
                if not _is_night_dir(night) or night.name >= stack_cutoff:
                    continue
                for f in _safe_iterdir(night):
                    try:
                        f.unlink(missing_ok=True)
                    except OSError:
                        logger.warning('I/O error on ffthumb %s — skipping', f)
                try:
                    night.rmdir()
                except OSError:
                    pass

    def _purge_station(self, station_id: str) -> None:
        capture_cutoff  = _cutoff(self.color_max_days if self.rolling
                                  else self.color_retention)
        locked_cutoff   = _cutoff(self.locked_retention)
        uploaded_cutoff = _cutoff(self.uploaded_retention)
        tl_cutoff       = _cutoff(self.tl_retention)
        stack_cutoff    = _cutoff(self.stack_retention)

        cap_station = self.capture_path / station_id
        if not cap_station.exists():
            return

        for d in _safe_iterdir(cap_station):
            try:
                if not d.is_dir():
                    continue
            except OSError:
                logger.warning('[%s] I/O error checking %s — skipping', station_id, d)
                continue

            # videos/
            videos_dir = d / 'videos'
            if videos_dir.exists():
                for f in _safe_iterdir(videos_dir):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                        continue

                    if f.suffix == '.mkv':
                        if flags_manager.is_chunk_uploaded(station_id, d.name, f.name, self.cfg):
                            # Hold uploaded chunks for uploaded_days: deleting
                            # on sight raced the faststart remux (14 s loss on
                            # gmnde00) and broke "7 days local iff uploaded".
                            # Disk pressure reclaims them earlier (ordered eviction).
                            if d.name < uploaded_cutoff:
                                if self._unlink_chunk(station_id, f):
                                    rovimen_lock.unlock(f)
                                    logger.debug('[%s] Purged uploaded chunk: %s', station_id, f.name)
                            else:
                                logger.debug('[%s] Holding uploaded chunk %s — within uploaded_days',
                                             station_id, f.name)
                            continue

                        if _is_locked(f, station_id, d.name, self.cfg):
                            if d.name < locked_cutoff:
                                try:
                                    if self._unlink_chunk(station_id, f):
                                        rovimen_lock.unlock(f)
                                        logger.info('[%s] Purged expired locked chunk: %s', station_id, f.name)
                                except Exception:
                                    logger.exception('[%s] Failed to purge locked chunk %s', station_id, f.name)
                            elif self.archive_enabled and not flags_manager.is_chunk_uploaded(station_id, d.name, f.name, self.cfg):
                                logger.debug('[%s] Holding %s — locked, not yet uploaded', station_id, f.name)
                        elif d.name < capture_cutoff:
                            if self._unlink_chunk(station_id, f):
                                logger.debug('[%s] Purged %s', station_id, f.name)

                    elif d.name < capture_cutoff:
                        parts = f.stem.split('_')
                        if len(parts) >= 3:
                            mkv = videos_dir / f'{parts[0]}_{parts[1]}_{parts[2]}_color.mkv'
                            if mkv.exists() and _is_locked(mkv, station_id, d.name, self.cfg):
                                continue
                        f.unlink(missing_ok=True)
                        logger.debug('[%s] Purged sidecar %s', station_id, f.name)

                try:
                    videos_dir.rmdir()
                except OSError:
                    pass

            # stacks/thumbs/ — purge thumbnails first so stacks/ can rmdir cleanly
            thumbs_dir = d / 'stacks' / 'thumbs'
            if thumbs_dir.exists() and d.name < stack_cutoff:
                for f in _safe_iterdir(thumbs_dir):
                    try:
                        if f.is_file():
                            f.unlink(missing_ok=True)
                            logger.debug('[%s] Purged thumb %s', station_id, f.name)
                    except OSError:
                        logger.warning('[%s] I/O error on thumb %s — skipping', station_id, f)
                try:
                    thumbs_dir.rmdir()
                except OSError:
                    pass

            # ffthumbs/ — BW FF detection thumbnails pushed to the VPS by the
            # detection indexer. Same retention as thumbs: the archive copy is
            # the canonical one, the local render is only kept for re-push.
            ffthumbs_dir = d / 'ffthumbs'
            if ffthumbs_dir.exists() and d.name < stack_cutoff:
                for f in _safe_iterdir(ffthumbs_dir):
                    try:
                        if f.is_file():
                            f.unlink(missing_ok=True)
                            logger.debug('[%s] Purged ffthumb %s', station_id, f.name)
                    except OSError:
                        logger.warning('[%s] I/O error on ffthumb %s — skipping', station_id, f)
                try:
                    ffthumbs_dir.rmdir()
                except OSError:
                    pass

            # stacks/
            stacks_dir = d / 'stacks'
            if stacks_dir.exists() and d.name < stack_cutoff:
                for f in _safe_iterdir(stacks_dir):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on stack %s — skipping', station_id, f)
                        continue
                    parts = f.stem.split('_')
                    if len(parts) >= 3:
                        mkv = d / 'videos' / f'{parts[0]}_{parts[1]}_{parts[2]}_color.mkv'
                        if mkv.exists() and _is_locked(mkv, station_id, d.name, self.cfg):
                            continue
                    f.unlink(missing_ok=True)
                    logger.debug('[%s] Purged stack %s', station_id, f.name)
                try:
                    stacks_dir.rmdir()
                except OSError:
                    pass

            # timelapse + night stack — files directly in date dir
            if d.name < tl_cutoff:
                for f in _safe_iterdir(d):
                    try:
                        if not f.is_file():
                            continue
                    except OSError:
                        logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                        continue
                    if f.name.endswith('_timelapse.mp4'):
                        if self.archive_enabled and not flags_manager.is_timelapse_uploaded(station_id, d.name, self.cfg):
                            logger.debug('[%s] Timelapse %s retained — not yet uploaded', station_id, f.name)
                            continue
                        f.unlink(missing_ok=True)
                        logger.info('[%s] Purged timelapse: %s/%s', station_id, d.name, f.name)
                    elif f.name.endswith('_night_stack.webp'):
                        if self.archive_enabled and not flags_manager.is_night_stack_uploaded(station_id, d.name, self.cfg):
                            logger.debug('[%s] Night stack %s retained — not yet uploaded', station_id, f.name)
                            continue
                        f.unlink(missing_ok=True)
                        logger.info('[%s] Purged night stack: %s/%s', station_id, d.name, f.name)

            # Legacy flat-layout sweep
            for f in _safe_iterdir(d):
                try:
                    if not f.is_file():
                        continue
                except OSError:
                    logger.warning('[%s] I/O error on %s — skipping', station_id, f)
                    continue
                if f.suffix == '.mkv':
                    if flags_manager.is_chunk_uploaded(station_id, d.name, f.name, self.cfg):
                        # Same uploaded_days hold as the videos/ branch above.
                        if d.name < uploaded_cutoff:
                            if self._unlink_chunk(station_id, f):
                                rovimen_lock.unlock(f)
                                logger.debug('[%s] Purged uploaded legacy chunk: %s', station_id, f.name)
                        else:
                            logger.debug('[%s] Holding uploaded legacy chunk %s — within uploaded_days',
                                         station_id, f.name)
                    elif _is_locked(f, station_id, d.name, self.cfg):
                        if d.name < locked_cutoff:
                            try:
                                if self._unlink_chunk(station_id, f):
                                    rovimen_lock.unlock(f)
                                    logger.info('[%s] Purged legacy locked chunk: %s', station_id, f.name)
                            except Exception:
                                logger.exception('[%s] Failed to purge legacy chunk %s', station_id, f.name)
                    elif d.name < capture_cutoff:
                        if self._unlink_chunk(station_id, f):
                            logger.debug('[%s] Purged legacy chunk %s', station_id, f.name)
                elif (d.name < capture_cutoff and f.name != 'state.json'
                        and not f.name.endswith(_NIGHT_PRODUCT_SUFFIXES)):
                    f.unlink(missing_ok=True)
                    logger.debug('[%s] Purged legacy sidecar %s', station_id, f.name)

            try:
                d.rmdir()
                logger.info('[%s] Purged capture dir: %s', station_id, d)
            except OSError:
                pass

    def _keep_free_gb(self) -> float:
        """Free space the rolling release keeps on the colour filesystem:
        ``retention.color_keep_free_gb`` when set, else COLOR_KEEP_MARGIN_GB
        over the largest of RMS's next-night estimate (any camera whose RMS
        data shares the filesystem), the free space at warn_pct, and
        min_free_gb. Computed once per run."""
        if self.color_keep_free_gb is not None:
            return self.color_keep_free_gb
        if self._keep_free_cache is not None:
            return self._keep_free_cache
        floor = self.min_free_gb
        try:
            s = os.statvfs(self.capture_path)
            usable = ((s.f_blocks - s.f_bfree) + s.f_bavail) * s.f_frsize
            floor = max(floor, usable * (100 - self.warn_pct) / 100 / 1e9)
        except OSError:
            pass
        need = self._rms_need_gb()
        if need is not None:
            floor = max(floor, need)
        self._keep_free_cache = floor + COLOR_KEEP_MARGIN_GB
        return self._keep_free_cache

    def _rms_need_gb(self) -> float | None:
        """The largest "Need X GB for next night" RMS logged for a camera whose
        RMS data shares the colour filesystem, in GB, or None. Each RMS
        instance compares its own need with the free space when a night ends
        and starts, so the largest one is what the disk must hold. Computed
        once per run."""
        if self._rms_need_cache is not False:
            return self._rms_need_cache
        need: float | None = None
        try:
            capture_dev = os.stat(self.capture_path).st_dev
        except OSError:
            capture_dev = None
        for sinfo in (self.cfg.get('stations') or {}).values():
            rms_data = (sinfo or {}).get('rms_data_path')
            if not rms_data:
                continue
            try:
                if os.stat(rms_data).st_dev != capture_dev:
                    continue
            except OSError:
                continue
            cam_need = _rms_next_night_need_gb(Path(rms_data) / 'logs')
            if cam_need is not None:
                need = cam_need if need is None else max(need, cam_need)
        self._rms_need_cache = need
        return need

    def _rolling_release(self) -> None:
        """While free space is under the keep-free target (``_keep_free_gb``),
        delete non-detection chunks of camera-nights whose morning is done,
        oldest first across all cameras, one at a time.

        The previous night thus shrinks from its start as the next one is
        recorded, instead of going all at once: each chunk stays until the
        disk needs its room, about 24 h on gmnro05 (Bistrița) (a 13 h night
        is ~220 GB of its 457 GB). Locked chunks are left to locked_days and
        the current night (morning not done) is never touched, nor is a night
        whose EON lock pass has not run yet (see _night_released).
        """
        if not self.rolling:
            return
        try:
            _pct, avail_gb = _fs_stats(self.capture_path)
        except OSError:
            return
        target = self._keep_free_gb()
        if avail_gb >= target:
            return
        morning: dict[tuple[str, str], bool] = {}

        def done(sid: str, date_str: str) -> bool:
            if (sid, date_str) not in morning:
                morning[(sid, date_str)] = self._night_released(sid, date_str)
            return morning[(sid, date_str)]

        start_gb = avail_gb
        deleted = 0
        deleted_bytes = 0
        oldest = None
        for _dt, locked, sid, date_str, f in self._collect_chunks():
            if avail_gb >= target:
                break
            if locked or not done(sid, date_str):
                continue
            try:
                size = f.stat().st_size
            except OSError:
                continue
            if not self._unlink_chunk(sid, f):
                continue
            rovimen_lock.unlock(f)
            deleted += 1
            deleted_bytes += size
            oldest = oldest or f.name
            try:
                _pct, avail_gb = _fs_stats(self.capture_path)
            except OSError:
                break
            if deleted_bytes >= ROLLING_STALL_BYTES and (avail_gb - start_gb) * 1e9 < deleted_bytes / 4:
                logger.error('Rolling release: deleted %.1f GB of chunks but only %.1f GB came '
                             'free; stopping', deleted_bytes / 1e9, avail_gb - start_gb)
                break
        if deleted:
            logger.info('Rolling release: deleted %d non-detection chunk(s), %.1f GB, from %s; '
                        '%.1f GB free (keeping %.0f)', deleted, deleted_bytes / 1e9, oldest,
                        avail_gb, target)

    def _has_rms(self, sid: str) -> bool:
        """Whether RMS records this camera (detection_lock.process_night's
        test): not ``rms: false`` and an ``rms_data_path``. Colour-only and
        spectroscope cameras, and camera dirs no longer configured, have none."""
        scfg = (self.cfg.get('stations') or {}).get(sid) or {}
        return bool(scfg.get('rms', True)) and bool(scfg.get('rms_data_path'))

    def _night_released(self, sid: str, date_str: str) -> bool:
        """Whether the rolling release may take a camera-night's chunks.

        Its morning sequence must be done, and on a camera with RMS its EON
        lock pass must have run (``rms_complete``). Dawn marks morning_done
        also when it gave up waiting for RMS and skipped that pass; the
        detections RMS finishes later are locked by the next dawn's detection
        backfill, which sets rms_complete, and until then the night's chunks
        are not "non-detection". A night that never gets there is left to the
        RMS floor, the pressure eviction and the colour date cap.
        """
        if not flags_manager.is_morning_done(sid, date_str, self.cfg):
            return False
        if not self._has_rms(sid):
            return True
        return bool(flags_manager.peek(sid, date_str, self.cfg).get('rms_complete'))

    def _purge_captured_files(self) -> None:
        """Prune RMS CapturedFiles dirs older than captured_retention days
        that have been sent to GMN.

        Proof of sending is the session's archives (_imgdata + _metadata, or
        the older single _detected), either still in ArchivedFiles or shown as
        uploaded in RMS's own logs (see _sent_to_gmn_proof). RMS keeps the archives of only its newest night, so on
        gmnde00 (Johannismühle) the ArchivedFiles proof alone held every older
        session forever. A session with neither proof (never uploaded, or its
        logs already rotated away) stays held.
        """
        cutoff = _cutoff(self.captured_retention)
        log_cache: dict[Path, set[str]] = {}
        for sid, rms_data, session, date in self._captured_sessions():
            if date >= cutoff:
                continue
            proof = _sent_to_gmn_proof(rms_data, session, date, log_cache)
            if proof is None:
                logger.debug('[%s] Holding CapturedFiles %s — not yet sent to GMN',
                             sid, session.name)
                continue
            logger.info('[%s] Pruning CapturedFiles %s (sent to GMN: %s)',
                        sid, session.name, proof)
            shutil.rmtree(session, ignore_errors=True)

    def _captured_sessions(self) -> list[tuple[str, Path, Path, str]]:
        """(camera, rms_data, session dir, YYYYMMDD) for every CapturedFiles session, oldest first."""
        sessions: list[tuple[str, Path, Path, str]] = []
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            rms_data = Path(rms_data_path)
            for session in _safe_iterdir(rms_data / 'CapturedFiles'):
                parts = session.name.split('_')
                if len(parts) < 3 or not parts[1].isdigit() or len(parts[1]) != 8:
                    continue
                if not session.is_dir():
                    continue
                sessions.append((sid, rms_data, session, parts[1]))
        sessions.sort(key=lambda s: s[2].name.split('_', 1)[1])
        return sessions

    def _escalate_captured_sent(self, over_limit: Callable[[], bool]) -> int:
        """Pressure pass: free RMS CapturedFiles already sent to GMN, any age.

        The normal sweep holds the previous night's session until the
        captured_days date cutoff passes at 00:00 UTC, and the colour eviction
        never touches RMS data. On a single-filesystem station that session
        (~15 GB per camera on a long night) is the largest reclaimable item
        through the evening, so a long night filled the disk before midnight:
        gmnro02 (Vaslui) at 23:58 on 2026-09-24 and gmnro14 (Chiribiș) at
        22:08 on 2026-10-16, each time ENOSPC hanging every RMS camera for the
        rest of the night.

        A session is taken only with the same proof of sending the sweep
        uses, and only once dawn has finished its night (``morning_done``),
        because dawn reads the night's FTPdetectinfo and FF files. The night
        RMS is recording cannot have been sent, so it is never a candidate.
        Oldest first, stopping once *over_limit* clears. Returns the count.
        """
        log_cache: dict[Path, set[str]] = {}
        deleted = 0
        for sid, rms_data, session, date in self._captured_sessions():
            if not over_limit():
                break
            m = rovimen_night.SESSION_DIR_RE.match(session.name)
            night = rovimen_night.night_of(m.group(2), m.group(3)) if m else date
            if not flags_manager.is_morning_done(sid, night, self.cfg):
                continue
            proof = _sent_to_gmn_proof(rms_data, session, date, log_cache)
            if proof is None:
                continue
            logger.warning('[%s] Disk pressure: deleting CapturedFiles %s early (sent to GMN: %s)',
                           sid, session.name, proof)
            shutil.rmtree(session, ignore_errors=True)
            deleted += 1
        return deleted

    def _rotate_append_logs(self) -> None:
        """Rotate each :data:`_APPEND_LOGS` file once it passes log_rotate_mb.

        Only those exact names directly under log_base: the rotated copies are
        retired by :meth:`_purge_capture_logs` like any other old log.
        """
        if self.log_rotate_bytes <= 0 or not self.log_base.is_dir():
            return
        stamp = datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')
        for name in _APPEND_LOGS:
            path = self.log_base / name
            try:
                if not path.is_file() or path.stat().st_size <= self.log_rotate_bytes:
                    continue
                size = _copytruncate_gz(path, self.log_base / f'{name}.{stamp}.gz')
            except OSError:
                logger.warning('Could not rotate log %s — skipping', path, exc_info=True)
                continue
            logger.info('Rotated log %s (%.1f MB)', name, size / 1e6)

    def _newest_rotated_copies(self) -> set[Path]:
        """The newest rotated copy of each append log."""
        newest: dict[str, tuple[float, Path]] = {}
        for pattern in _LOG_PRUNE_GLOBS:
            for f in self.log_base.glob(pattern):
                m = _ROTATED_APPEND_RE.match(f.name)
                if not m or not f.is_file():
                    continue
                mtime = f.stat().st_mtime
                if m.group(1) not in newest or mtime > newest[m.group(1)][0]:
                    newest[m.group(1)] = (mtime, f)
        return {f for _, f in newest.values()}

    def _purge_capture_logs(self) -> None:
        """Prune capture-side daemon logs older than ``log_retention`` days.

        Colour capture appends ffmpeg's stderr and the stacker's output to
        files under log_path. Those used to be opened once and appended
        forever — stations were carrying 350 MB, 1.9M-line files. On 14 of 18
        stations log_path shares a filesystem with the capture data, so they
        ate the headroom that feeds the ENOSPC deadlock, and their months of
        stale errors twice sent an incident diagnosis down the wrong path.
        Capture now writes them per night; this pass retires the old nights
        and the legacy undated files with them.

        Deliberately narrow, because this is fleet-wide deletion: an explicit
        glob allowlist (:data:`_LOG_PRUNE_GLOBS`), a ``.log`` suffix or one of
        the name-anchored rotated-copy patterns, regular files only, no
        recursion of its own. It never touches video or data
        files — those stay the business of the retention passes that already
        manage them.
        """
        if self.log_retention <= 0 or not self.log_base.exists():
            return
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.log_retention)
        # The live append log restarts empty at each rotation, so its newest
        # rotated copy is kept whatever its age: it is the only history left.
        keep = self._newest_rotated_copies()
        freed = 0
        for pattern in _LOG_PRUNE_GLOBS:
            for f in sorted(self.log_base.glob(pattern)):
                try:
                    if not f.is_file() or f in keep:
                        continue
                    if (f.suffix != '.log' and not _ROTATED_JANITOR_RE.match(f.name)
                            and not _ROTATED_APPEND_RE.match(f.name)):
                        continue
                    st = f.stat()
                except OSError:
                    logger.warning('I/O error on log %s — skipping', f)
                    continue
                # Logs are append-only, so mtime is exactly "last written" —
                # the right age signal for the per-night names and the legacy
                # undated ones alike, and it never retires a file still in use.
                if datetime.fromtimestamp(st.st_mtime, tz=timezone.utc) >= cutoff:
                    continue
                f.unlink(missing_ok=True)
                freed += st.st_size
                logger.info('Purged capture log %s (%.1f MB)', f.name, st.st_size / 1e6)
        if freed:
            logger.info('Capture-log sweep freed %.1f MB (keeping %g days)',
                        freed / 1e6, self.log_retention)

    # ------------------------------------------------------------------
    # Disk check — capture filesystem
    # ------------------------------------------------------------------

    def _over_limit(self) -> bool:
        """True when the capture filesystem is at/over the pressure limit.

        Fires on EITHER the df-accurate percentage crossing warn_pct OR the
        non-root available space dropping below min_free_gb. The free-bytes
        floor lets small disks trigger before ENOSPC even if the percentage
        hasn't crossed, and both use the reserve-corrected metric so a disk
        that is full for the pipeline user always reads as full.
        """
        try:
            pct, avail_gb = _fs_stats(self.capture_path)
        except OSError:
            return False
        if self.min_free_gb > 0 and avail_gb < self.min_free_gb:
            return True
        return pct >= self.warn_pct

    def _run_disk_check(self) -> None:
        if not self.capture_path.exists():
            return
        try:
            pct, avail_gb = _fs_stats(self.capture_path)
        except OSError:
            logger.warning('Could not read disk usage for %s', self.capture_path)
            return

        if not self._over_limit():
            return

        logger.info(
            'Capture disk at %d%% (%.1f GB free) — starting escalation', pct, avail_gb,
        )

        # Ordered, age + lock aware eviction driven by the protect window and
        # the free-space target: never touches anything <=protect_hours old,
        # deletes non-detections older than that oldest-first, then anything
        # older than that oldest-first.
        self._ordered_eviction()

        pct = _disk_pct(self.capture_path)
        if self.rms_on_capture_fs and pct >= self.home_warn_pct:
            self._escalate_rms_soft()
            pct = _disk_pct(self.capture_path)

        if self.rms_on_capture_fs and pct >= self.home_nuclear_pct:
            self._escalate_rms_hard(
                lambda: _disk_pct(self.capture_path) >= self.home_nuclear_pct)
            pct = _disk_pct(self.capture_path)

        pct, avail_gb = _fs_stats(self.capture_path)
        if self._over_limit():
            logger.error(
                'Disk still at %d%% (%.1f GB free) after full escalation — '
                'nothing older than %gh left to delete',
                pct, avail_gb, self.protect_hours,
            )
            self._unrelieved = (str(self.capture_path), pct, avail_gb)
        else:
            logger.info('Escalation complete — disk now at %d%% (%.1f GB free)', pct, avail_gb)

    # ------------------------------------------------------------------
    # Disk check — home filesystem (only when separate from capture)
    # ------------------------------------------------------------------

    def _run_home_check(self) -> None:
        try:
            pct = _disk_pct(Path.home())
        except OSError:
            logger.warning('Could not read home disk usage')
            return

        if pct < self.home_warn_pct:
            return

        logger.info('Home disk at %d%% — starting RMS escalation', pct)

        self._escalate_captured_sent(lambda: _disk_pct(Path.home()) >= self.home_warn_pct)
        pct = _disk_pct(Path.home())

        if pct >= self.home_warn_pct:
            self._escalate_rms_soft()
            pct = _disk_pct(Path.home())

        if pct >= self.home_nuclear_pct:
            self._escalate_rms_hard(lambda: _disk_pct(Path.home()) >= self.home_nuclear_pct)
            pct = _disk_pct(Path.home())

        if pct >= self.home_warn_pct:
            logger.error('Home disk still at %d%% after RMS pruning', pct)
            try:
                self._unrelieved = (str(Path.home()), pct, shutil.disk_usage(Path.home()).free / 1e9)
            except OSError:
                pass
        else:
            logger.info('Home escalation complete — disk now at %d%%', pct)

    # ------------------------------------------------------------------
    # Escalation — capture filesystem
    # ------------------------------------------------------------------

    def _collect_date_dirs(self) -> list[tuple[str, str, Path]]:
        """Return (date, station_id, dir) for every YYYYMMDD night dir across
        all stations, sorted oldest first so escalation passes can walk the
        fleet's nights in age order."""
        all_date_dirs: list[tuple[str, str, Path]] = []
        for sid in self._camera_ids():
            cap_station = self.capture_path / sid
            if not cap_station.exists():
                continue
            for d in _safe_iterdir(cap_station):
                if _is_night_dir(d):
                    all_date_dirs.append((d.name, sid, d))
        all_date_dirs.sort(key=lambda x: x[0])
        return all_date_dirs

    # ------------------------------------------------------------------
    # Ordered eviction — the age + lock aware pressure pass
    # ------------------------------------------------------------------

    def _collect_chunks(self) -> list[tuple[datetime, bool, str, str, Path]]:
        """Enumerate every colour .mkv (chunk or splice) across all stations.

        Returns (start_dt, locked, station_id, date_str, path) tuples. Age comes
        from the filename start-time (UTC), which is immutable and correct even
        on RTC-less hosts where mtime can be wrong. Files whose name doesn't
        encode a timestamp fall back to mtime.
        """
        chunks: list[tuple[datetime, bool, str, str, Path]] = []
        for date_str, sid, date_dir in self._collect_date_dirs():
            videos_dir = date_dir / 'videos'
            mkv_dir = videos_dir if videos_dir.exists() else date_dir
            for f in _safe_iterdir(mkv_dir):
                try:
                    if not f.is_file() or f.suffix != '.mkv':
                        continue
                except OSError:
                    logger.warning('[%s] I/O error on %s — skipping', sid, f)
                    continue
                dt = _file_age_dt(f)
                if dt is None:
                    continue
                locked = _is_locked(f, sid, date_str, self.cfg)
                chunks.append((dt, locked, sid, date_str, f))
        chunks.sort(key=lambda c: c[0])  # oldest first
        return chunks

    def _ordered_eviction(self) -> None:
        """Age + lock aware pressure eviction (operator spec).

        Runs only when the disk is over the limit. The hard invariant is that
        nothing younger than ``protect_hours`` (default 24h) is ever deleted,
        so the entire current night — detections AND non-detections — is safe.

        Eviction order, each phase archived-then-oldest-first (see
        :meth:`_evict_chunks`) and stopping the instant the disk drops back
        under the limit / free-space target:

          Phase 1: NON-DETECTION (unlocked) chunks older than 24h — the
                   previous night's bulk. This is almost always enough.
          Phase 2: ANYTHING older than 24h (locked detections + stitched
                   splices included) — last resort, only reached when deleting
                   every eligible non-detection wasn't enough.

        Within a phase, chunks already uploaded to the storagebox are taken
        before chunks that exist only here, so pressure spends the copies the
        archive can still serve before it spends the irreplaceable ones.

        Both phases skip anything <=protect_hours old. A chunk deleted in a
        phase is gone; a second run simply re-enumerates what remains, so the
        pass is idempotent.
        """
        now = datetime.now(timezone.utc)
        protect_before = now - timedelta(hours=self.protect_hours)
        chunks = self._collect_chunks()
        # Only chunks strictly older than the protect window are ever eligible.
        eligible = [c for c in chunks if c[0] < protect_before]

        # Phase 1: unlocked (non-detection) bulk older than 24h, oldest first.
        phase1 = [c for c in eligible if not c[1]]
        deleted_p1 = self._evict_chunks(phase1, phase='non-detection >24h')

        # Between the phases: RMS sessions already at GMN, which on a
        # station whose RMS data shares the capture disk cost less than any
        # detection clip.
        if self.rms_on_capture_fs and self._over_limit():
            self._escalate_captured_sent(self._over_limit)

        # Phase 2: everything older than 24h, oldest first (locked + splices).
        deleted_p2 = 0
        if self._over_limit():
            # Re-enumerate: phase 1 already removed the unlocked survivors, and
            # locking state may have changed between passes.
            remaining = [c for c in self._collect_chunks() if c[0] < protect_before]
            deleted_p2 = self._evict_chunks(remaining, phase='any >24h')

        pct, avail_gb = _fs_stats(self.capture_path)
        logger.warning(
            'Ordered eviction: deleted %d non-detection + %d locked/other (>%gh old) '
            '— disk now at %d%% (%.1f GB free)',
            deleted_p1, deleted_p2, self.protect_hours, pct, avail_gb,
        )
        self._cleanup_empty_dirs()

    def _rms_floor_gb(self) -> float | None:
        """Free space colour must leave to RMS on a disk they share, in GB, or
        None when RMS records elsewhere.

        ``disk.rms_floor_gb`` when set; else RMS_FLOOR_MARGIN_GB over RMS's
        largest logged next-night need, or FALLBACK_FLOOR_GB before RMS has
        logged one. On the fallback root ``capture_fallback.floor_gb`` is the
        least it can be."""
        floor: float | None = None
        if self.rms_on_capture_fs:
            if self.rms_floor_override is not None:
                floor = self.rms_floor_override
            else:
                need = self._rms_need_gb()
                floor = FALLBACK_FLOOR_GB if need is None else need + RMS_FLOOR_MARGIN_GB
        if self.on_fallback:
            floor = max(floor or 0.0, self.fallback_floor_gb)
        return floor

    def _lock_tonights_detections(self) -> None:
        """Run the additive mid-night lock pass for every camera before
        tonight's chunks become eligible, so a meteor RMS has already found is
        locked rather than waiting for the hourly midnight-lock.timer."""
        try:
            import detection_lock
            night = detection_lock._tonight_date()
            for sid in self.cfg.get('stations', {}):
                try:
                    detection_lock.midnight_lock_pass(sid, night, self.cfg)
                except Exception:
                    logger.exception('[%s] Mid-night lock pass failed before the RMS floor eviction', sid)
        except Exception:
            logger.exception('Mid-night lock pass unavailable before the RMS floor eviction')

    def _evict_for_rms(self) -> int:
        """Keep ``_rms_floor_gb`` free on a disk colour shares with RMS, even
        if that costs tonight's colour.

        Every other pass protects the night being recorded (protect_hours), so
        on a disk that cannot hold a night of colour plus RMS nothing stopped
        it filling. RMS then lost the night: on gmnro22 (Ferigile), 232 GB with
        ~46 GB of colour per camera-night for three cameras, free space fell
        from 27 GB at 02:40 to 0.9 GB at 04:20 UTC every clear night from
        2026-11-09 to 11-12, and at the end of the night RMS either hit ENOSPC
        or, short of its 20 GB "Need ... for next night", deleted all of its
        own data including the unprocessed night (RO001T 11-09 and 11-11,
        RO001S 11-10 and 11-12, RO001R 11-12, never sent to GMN). RMS capture
        and the GMN upload outrank colour video, so below the floor:

          1. RMS sessions already sent to GMN (as in the pressure pass);
          2. non-detection chunks of any age, the previous nights' and then
             tonight's, archived copies and the oldest first, once the
             mid-night lock pass has locked what RMS found so far.
             Chunks younger than RMS_FLOOR_MIN_AGE_H stay (the night stacker
             and RMS's live detection have not reached them yet).

        Detections are never taken here; if only detections are left the run
        records a pressure episode, which station_watchdog alerts on. Returns
        the number of chunks deleted.
        """
        floor = self._rms_floor_gb()
        if floor is None:
            return 0

        def relieved() -> bool:
            try:
                return _fs_stats(self.capture_path)[1] >= floor
            except OSError:
                return True

        if relieved():
            return 0
        start_gb = _fs_stats(self.capture_path)[1]
        logger.warning('RMS floor: %.1f GB free on %s, under the %.1f GB RMS needs — '
                       'freeing space for RMS', start_gb, self.capture_path, floor)
        if self.rms_on_capture_fs:
            self._escalate_captured_sent(lambda: not relieved())
            if relieved():
                return 0
        self._lock_tonights_detections()
        young = datetime.now(timezone.utc) - timedelta(hours=RMS_FLOOR_MIN_AGE_H)
        unlocked = [c for c in self._collect_chunks() if not c[1] and c[0] < young]
        deleted = self._evict_chunks(unlocked, phase=f'RMS floor {floor:.1f} GB',
                                     relieved=relieved)
        try:
            avail_gb = _fs_stats(self.capture_path)[1]
        except OSError:
            avail_gb = float('nan')
        if deleted:
            logger.warning('RMS floor: deleted %d non-detection chunk(s), any age, '
                           '%.1f -> %.1f GB free (floor %.1f GB)',
                           deleted, start_gb, avail_gb, floor)
        if not relieved():
            logger.error('RMS floor: still %.1f GB free, under the %.1f GB RMS needs, with no '
                         'non-detection colour older than %gh left', avail_gb, floor,
                         RMS_FLOOR_MIN_AGE_H)
            self._unrelieved = (str(self.capture_path), _disk_pct(self.capture_path), avail_gb)
        self._cleanup_empty_dirs()
        return deleted

    def _evict_chunks(
        self, chunks: list[tuple[datetime, bool, str, str, Path]], phase: str,
        relieved: Callable[[], bool] | None = None,
    ) -> int:
        """Delete the given chunks safest-first, re-checking disk after each and
        stopping as soon as the disk is back under the limit. Returns the count
        deleted. Callers pass a list already filtered to the protect window.

        Order is ``(not uploaded, age)``: chunks the archive already holds go
        first, oldest-first, then chunks that exist nowhere but this disk, also
        oldest-first. Age alone decided before, so under sustained pressure the
        janitor could destroy the only copy of an un-uploaded detection while
        an older chunk that was safely on the storagebox survived it — the
        deletion freed the same bytes either way, it just picked the wrong
        copy. Upload state is read here rather than carried out of
        :meth:`_collect_chunks` so the enumeration stays a pure age + lock
        view, and it costs nothing when the disk is not over the limit.
        """
        deleted = 0
        if relieved is None:
            relieved = lambda: not self._over_limit()  # noqa: E731
        if relieved():
            return 0
        ordered = sorted(
            (
                (
                    flags_manager.is_chunk_uploaded(sid, date_str, f.name, self.cfg),
                    dt, locked, sid, date_str, f,
                )
                for dt, locked, sid, date_str, f in chunks
            ),
            key=lambda c: (not c[0], c[1]),
        )
        for uploaded, dt, locked, sid, date_str, f in ordered:
            if relieved():
                logger.info(
                    'Disk back under limit — stopping %s eviction after %d deletion(s)',
                    phase, deleted,
                )
                break
            if not self._unlink_chunk(sid, f):
                continue
            if locked:
                rovimen_lock.unlock(f)
            if locked and not uploaded:
                # The archive has no copy: this deletion loses the meteor.
                logger.error(
                    '%s [%s] Eviction (%s): deleting the ONLY copy of detection '
                    '%s/%s (start %s) — never uploaded, no archive copy exists',
                    DATA_LOSS_MARKER, sid, phase, date_str, f.name,
                    dt.strftime('%Y%m%d_%H%M%S'),
                )
            else:
                logger.warning(
                    'WARNING [%s] Eviction (%s): deleting %s/%s (start %s, %s, %s)',
                    sid, phase, date_str, f.name,
                    dt.strftime('%Y%m%d_%H%M%S'),
                    'DETECTION' if locked else 'non-detection',
                    'uploaded' if uploaded else 'not uploaded',
                )
            deleted += 1
        return deleted

    def _cleanup_empty_dirs(self) -> None:
        """Remove now-empty videos/ and date dirs left behind by eviction."""
        for _date_str, _sid, date_dir in self._collect_date_dirs():
            videos_dir = date_dir / 'videos'
            if videos_dir.exists():
                try:
                    videos_dir.rmdir()
                except OSError:
                    pass
            try:
                date_dir.rmdir()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # Escalation — home filesystem (RMS data)
    # ------------------------------------------------------------------

    def _escalate_rms_soft(self) -> None:
        """Soft RMS pruning — delete old FramesFiles and TimeFiles (>2 days).

        Safe to run at any time; does not affect RMS capture or processing.
        """
        cutoff = _cutoff(2)
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            rms_data = Path(rms_data_path)
            for subdir_name in ('FramesFiles', 'TimeFiles'):
                subdir = rms_data / subdir_name
                if not subdir.exists():
                    continue
                for entry, day in _dated_rms_dirs(subdir):
                    if day < cutoff:
                        logger.warning('[%s] RMS soft: deleting %s/%s', sid, subdir_name,
                                       entry.relative_to(subdir))
                        shutil.rmtree(entry, ignore_errors=True)

    def _escalate_rms_hard(self, over_limit: Callable[[], bool]) -> int:
        """Hard RMS pruning: CapturedFiles and ArchivedFiles session dirs
        beyond each camera's 3 newest, while *over_limit* holds.

        Sessions already sent to GMN (_sent_to_gmn_proof) go first, oldest
        first. Only if the disk is still over the limit after all of them do
        unsent sessions follow, oldest first, each logged as an error: a full
        disk hangs every RMS camera (ENOSPC), which costs more than one
        unsent night. Every deletion is re-checked against *over_limit*, so
        this never takes more than the old rule (every dir beyond the 3
        newest) and stops as soon as the disk is relieved. The old rule took
        all of them at once with no proof of sending, so a station whose GMN
        upload was failing lost its unsent raw FF sessions first. Returns the
        number of dirs deleted.
        """
        def still_over() -> bool:
            try:
                return over_limit()
            except OSError:
                return True   # cannot tell: keep relieving, as the old rule did

        log_cache: dict[Path, set[str]] = {}
        unsent: list[tuple[str, str, Path]] = []
        deleted = 0
        for sid, rms_data, subdir_name, d in self._rms_hard_candidates():
            parts = d.name.split('_')
            date = parts[1] if len(parts) > 1 and parts[1].isdigit() and len(parts[1]) == 8 else ''
            proof = _sent_to_gmn_proof(rms_data, d, date, log_cache)
            if proof is None:
                unsent.append((sid, subdir_name, d))
                continue
            if not still_over():
                return deleted
            logger.warning('[%s] RMS hard: deleting %s/%s (sent to GMN: %s)',
                           sid, subdir_name, d.name, proof)
            shutil.rmtree(d, ignore_errors=True)
            deleted += 1
        for sid, subdir_name, d in unsent:
            if not still_over():
                break
            logger.error('[%s] RMS hard: deleting %s/%s, NOT sent to GMN: the disk is still '
                         'over its limit with every sent session gone, and RMS must not run '
                         'out of space', sid, subdir_name, d.name)
            shutil.rmtree(d, ignore_errors=True)
            deleted += 1
        return deleted

    def _rms_hard_candidates(self) -> list[tuple[str, Path, str, Path]]:
        """(camera, rms_data, subdir name, session dir) for every CapturedFiles and
        ArchivedFiles dir beyond its camera's 3 newest in that tree, oldest
        session first.

        Grouped by camera (dir name up to the first '_') so that "keep 3
        newest" applies per camera, not across all cameras combined.
        """
        candidates: list[tuple[str, Path, str, Path]] = []
        seen: set[Path] = set()
        for sid, sinfo in self.cfg.get('stations', {}).items():
            rms_data_path = sinfo.get('rms_data_path')
            if not rms_data_path:
                continue
            rms_data = Path(rms_data_path)
            for subdir_name in ('CapturedFiles', 'ArchivedFiles'):
                subdir = rms_data / subdir_name
                if not subdir.exists():
                    continue
                by_camera: dict[str, list[Path]] = {}
                for d in subdir.iterdir():
                    if not d.is_dir():
                        continue
                    camera = d.name.split('_')[0]
                    by_camera.setdefault(camera, []).append(d)
                for cam_dirs in by_camera.values():
                    cam_dirs.sort(key=lambda d: d.name, reverse=True)
                    for old_dir in cam_dirs[3:]:
                        if old_dir not in seen:
                            seen.add(old_dir)
                            candidates.append((sid, rms_data, subdir_name, old_dir))
        candidates.sort(key=lambda c: c[3].name.split('_', 1)[-1])
        return candidates


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description='ROVIMEN disk lifecycle cron job — retention + pressure escalation'
    )
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    args = parser.parse_args()

    janitor = Janitor(args.config)
    janitor.run()


if __name__ == '__main__':
    main()
