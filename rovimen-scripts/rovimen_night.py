"""Shared night and file-name arithmetic for station scripts.

A "night" is named by the UTC date on which it starts: anything stamped
before 12:00 UTC belongs to the previous day's night. Colour chunks, RMS
session directories and FF files all carry UTC timestamps in their names
(color_capture pins TZ=UTC on the segment muxer since #851), so one set of
parsers serves every script.

Stdlib only and Python 3.8 safe (see rovimen_config).
"""

from __future__ import annotations

import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # Python < 3.9 (RMS venvs on Ubuntu 20.04)
    try:
        from backports.zoneinfo import ZoneInfo, ZoneInfoNotFoundError  # type: ignore[no-redef]
    except ImportError:
        ZoneInfo = None  # type: ignore[assignment,misc]
        ZoneInfoNotFoundError = Exception  # type: ignore[assignment,misc]

logger = logging.getLogger(__name__)

# Before this UTC hour a timestamp belongs to the previous day's night.
NIGHT_BOUNDARY_HOUR = 12
# Until this UTC hour the "last completed night" is yesterday's: past dawn's
# 13:15 deadline and the 13:45 reboot, before any station's sunset.
NIGHT_ROLLOVER_HOUR = 14

# RO000A_20260925_190512_color.mkv
CHUNK_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_color\.mkv$')
# Stitched boundary clip: RO000P_20260724_002833_002853_color.mkv (start, end).
STITCHED_CHUNK_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{6})_color\.mkv$')
# RMS raw video segment (raw_video_save / splitmuxsink, see BufferedCapture.moveSegment):
# RO000Z_20260622_234512_123456_video.mkv. On a color_tee station its colour chunk
# RO000Z_20260622_234512_color.mkv is a hardlink of it.
VIDEO_SEGMENT_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_\d{6}_video\.mkv$')
# RMS session directory: RO000A_20260925_170123_456789
SESSION_DIR_RE = re.compile(r'^([A-Z0-9]+)_(\d{8})_(\d{6})_')
# RMS FF file: FF_RO001E_20260801_190512_123_0123456.fits (camera, date, time, ms)
FF_NAME_RE = re.compile(r'^FF_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{3})_')
# RMS FF or FR file: FR_RO001E_20260801_190512_123_0123456.bin (kind, camera, date, time, ms)
RMS_FRAME_RE = re.compile(r'^(FF|FR)_([A-Z0-9]+)_(\d{8})_(\d{6})_(\d{3})_')
# A whole FF file name, any camera code, ``.fits`` required: what counts as a
# block RMS captured (camera_stall_guard, station_collect's capture summary).
FF_FILE_RE = re.compile(r'^FF_.+_(\d{8})_(\d{6})_\d{3}_\d+\.fits$')

# Seconds from a 20 s chunk's start to its middle.
CHUNK_MIDPOINT_S = 10


class ChunkName(NamedTuple):
    camera: str
    date: str          # YYYYMMDD of the chunk start (UTC)
    time: str          # HHMMSS of the chunk start (UTC)
    end_time: Optional[str]  # HHMMSS end for a stitched clip, else None

    @property
    def night(self) -> str:
        return night_of(self.date, self.time)


def parse_chunk_name(name: str) -> Optional[ChunkName]:
    """Split a regular or stitched colour-chunk file name, or None."""
    m = CHUNK_RE.match(name)
    if m:
        return ChunkName(m.group(1), m.group(2), m.group(3), None)
    m = STITCHED_CHUNK_RE.match(name)
    if m:
        return ChunkName(m.group(1), m.group(2), m.group(3), m.group(4))
    return None


def utc_stamp(date_str: str, time_str: str) -> Optional[datetime]:
    """A file name's ``YYYYMMDD`` + ``HHMMSS`` as aware UTC, or None if malformed."""
    try:
        return datetime.strptime(date_str + time_str, '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def chunk_start_utc(name: str, stitched: bool = True) -> Optional[datetime]:
    """Aware UTC start of a colour-chunk file name, or None.

    Reads the name as UTC, which every station's chunk names are since #851;
    ``chunk_utc_dt`` is the variant that honours ``chunk_names_utc: false``.
    ``stitched=False`` refuses stitched boundary clips, for callers that count
    capture segments.
    """
    parsed = parse_chunk_name(name)
    if parsed is None or (parsed.end_time is not None and not stitched):
        return None
    return utc_stamp(parsed.date, parsed.time)


def chunk_midpoint_utc(date_str: str, time_str: str) -> Optional[datetime]:
    """Aware UTC middle of a 20 s chunk from its name's date and time, or None.

    The moment the adaptive colour's moon and twilight lift is evaluated at,
    for clips (encoder) and chunk stacks (stacker) alike.
    """
    start = utc_stamp(date_str, time_str[:6])
    return None if start is None else start + timedelta(seconds=CHUNK_MIDPOINT_S)


def ff_file_start(name: str) -> Optional[datetime]:
    """Aware UTC start, to the second, of a whole FF ``.fits`` name (``FF_FILE_RE``), or None."""
    m = FF_FILE_RE.match(name)
    return utc_stamp(m.group(1), m.group(2)) if m else None


def rms_frame_time(name: str) -> Optional[datetime]:
    """Naive UTC start, with milliseconds, of an RMS ``FF_`` or ``FR_`` file name, or None."""
    m = RMS_FRAME_RE.match(name)
    start = utc_stamp(m.group(3), m.group(4)) if m else None
    if start is None:
        return None
    return start.replace(tzinfo=None) + timedelta(milliseconds=int(m.group(5)))


def night_of(date_str: str, time_str: str) -> str:
    """The night a ``YYYYMMDD`` + ``HHMMSS`` UTC timestamp belongs to."""
    if int(time_str[:2]) < NIGHT_BOUNDARY_HOUR:
        day = datetime(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
                       tzinfo=timezone.utc) - timedelta(days=1)
        return day.strftime('%Y%m%d')
    return date_str[:8]


def current_night(now: Optional[datetime] = None) -> str:
    """The night in progress (or just begun) at *now*: the capture folder."""
    now = now or datetime.now(timezone.utc)
    if now.hour < NIGHT_BOUNDARY_HOUR:
        now = now - timedelta(days=1)
    return now.strftime('%Y%m%d')


def last_completed_night(now: Optional[datetime] = None) -> str:
    """The most recent night whose processing day this is.

    dawn_process's rule: until 14:00 UTC the night that just ended is
    yesterday's. A noon boundary would send a 12:00-13:15 dawn run (inside
    the deadline window) to tonight's empty directory.
    """
    now = now or datetime.now(timezone.utc)
    if now.hour < NIGHT_ROLLOVER_HOUR:
        return (now - timedelta(days=1)).strftime('%Y%m%d')
    return now.strftime('%Y%m%d')


def _system_zoneinfo() -> Any:
    """The system timezone as a ZoneInfo, or None when undeterminable."""
    if ZoneInfo is None:
        return None
    candidates: List[str] = []
    tz_env = os.environ.get('TZ')
    if tz_env:
        candidates.append(tz_env.lstrip(':'))
    try:
        candidates.append(Path('/etc/timezone').read_text().strip())
    except OSError:
        pass
    try:
        link = os.path.realpath('/etc/localtime')
        if 'zoneinfo/' in link:
            candidates.append(link.split('zoneinfo/', 1)[1])
    except OSError:
        pass
    for name in candidates:
        if not name:
            continue
        try:
            return ZoneInfo(name)
        except (ZoneInfoNotFoundError, ValueError):
            continue
    return None


def chunk_utc_dt(date_str: str, time_str: str, cfg: Optional[Dict[str, Any]] = None) -> datetime:
    """A chunk-name timestamp as a naive UTC datetime.

    Chunk names are UTC (``chunk_names_utc``, default true, set on every
    station). A station still carrying pre-#851 local-named chunks sets it
    false; the name is then read in ``legacy_chunk_tz`` (IANA name) or the
    system timezone, resolved at the chunk's own moment so a DST change
    between recording and reading cannot skew it.
    """
    naive = datetime(
        int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]),
        int(time_str[:2]), int(time_str[2:4]), int(time_str[4:6]),
    )
    cfg = cfg or {}
    if cfg.get('chunk_names_utc', True):
        return naive
    tz = None
    tz_name = cfg.get('legacy_chunk_tz')
    if tz_name and ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
        except (ZoneInfoNotFoundError, ValueError):
            logger.warning('legacy_chunk_tz %r not resolvable, using system timezone', tz_name)
    if tz is None:
        tz = _system_zoneinfo()
    if tz is not None:
        return naive.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
    return naive.astimezone(timezone.utc).replace(tzinfo=None)


def scan_rms_sessions(rms_data: Path, camera: Optional[str] = None,
                      night: Optional[str] = None) -> Tuple[Dict[str, Path], bool]:
    """RMS session directories under *rms_data*, keyed by directory name.

    Reads ``CapturedFiles`` then ``ArchivedFiles``, so a session present in
    both resolves to the ArchivedFiles copy (it holds RMS's final, filtered
    FTPdetectinfo). Optionally filtered to one camera code and one night.
    Returns ``(sessions, listed_any)``; ``listed_any`` is False when neither
    tree could be listed (missing or unreadable disk), which callers treat
    differently from "listed, nothing there".
    """
    rms_data = Path(rms_data)
    sessions: Dict[str, Path] = {}
    listed_any = False
    for sub in ('CapturedFiles', 'ArchivedFiles'):
        try:
            entries = list((rms_data / sub).iterdir())
        except OSError:
            continue
        listed_any = True
        for entry in entries:
            m = SESSION_DIR_RE.match(entry.name)
            if not m:
                continue
            if camera is not None and m.group(1) != camera:
                continue
            if night is not None and night_of(m.group(2), m.group(3)) != night:
                continue
            try:
                if not entry.is_dir():
                    continue
            except OSError:
                continue
            sessions[entry.name] = entry
    return sessions, listed_any


def rms_sessions(rms_data: Path, camera: Optional[str] = None,
                 night: Optional[str] = None) -> List[Path]:
    """Session directories (ArchivedFiles wins), sorted by name."""
    sessions, _listed = scan_rms_sessions(rms_data, camera, night)
    return [sessions[name] for name in sorted(sessions)]


def ftpdetect_files(session: Path) -> List[Path]:
    """The session's real FTPdetectinfo files (no ``_unfiltered``/``_backup``)."""
    try:
        found = sorted(Path(session).glob('FTPdetectinfo_*.txt'))
    except OSError:
        return []
    return [f for f in found if '_unfiltered' not in f.name and '_backup' not in f.name]


# dawn_process holds an exclusive flock on this file for as long as it runs.
DAWN_LOCK_FILE = Path('/tmp/rovimen_dawn.lock')


def dawn_running(lock_file: Path = DAWN_LOCK_FILE, proc_locks: Path = Path('/proc/locks')) -> bool:
    """True while a dawn_process run holds its lock.

    Read from /proc/locks rather than by trying the lock: a probe that takes
    the flock, however briefly, makes a dawn cron tick that starts in that
    instant exit as "another instance is running" and wait 15 minutes.
    /proc/locks names a lock by the file's device (hex major:minor) and inode.
    """
    try:
        st = lock_file.stat()
        text = proc_locks.read_text()
    except OSError:
        return False
    key = '%02x:%02x:%d' % (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    return any(key in line.split() for line in text.splitlines())


def sun_altitude_deg(lat_deg: float, lon_deg: float, when_utc: datetime) -> float:
    """Geometric solar altitude in degrees (NOAA low-precision algorithm).

    *when_utc* must be timezone-aware. Within 0.02 deg of astropy's
    unrefracted altitude (tests/test_rovimen_shared_modules.py) and
    dependency-free. The one station implementation: colour capture's sun
    schedule, the fps and stall guards, the watchdog's reboot deferral, the
    out-of-band mode switch and the detection indexer all gate on it.
    color_calibration.sun_moon_state keeps its own sun term because the moon's
    phase angle needs the sun's ecliptic longitude; its altitude agrees with
    this one to 0.002 deg.
    """
    d = when_utc - datetime(2000, 1, 1, 12, tzinfo=timezone.utc)
    n = d.total_seconds() / 86400.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    mean_anom = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic_lon = math.radians(
        mean_lon + 1.915 * math.sin(mean_anom) + 0.020 * math.sin(2 * mean_anom)
    )
    obliquity = math.radians(23.439 - 0.0000004 * n)
    declination = math.asin(math.sin(obliquity) * math.sin(ecliptic_lon))
    right_asc = math.atan2(
        math.cos(obliquity) * math.sin(ecliptic_lon), math.cos(ecliptic_lon)
    )
    gmst_hours = (18.697374558 + 24.06570982441908 * n) % 24.0
    hour_angle = math.radians((gmst_hours * 15.0 + lon_deg) % 360.0) - right_asc
    lat = math.radians(lat_deg)
    alt = math.asin(
        math.sin(lat) * math.sin(declination)
        + math.cos(lat) * math.cos(declination) * math.cos(hour_angle)
    )
    return math.degrees(alt)
