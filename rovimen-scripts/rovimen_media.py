"""Shared media helpers for station scripts: ffprobe, Matroska headers,
ffmpeg flags, VAAPI detection and per-chunk locks.

Every ffprobe/ffmpeg call here takes a timeout: a hung USB capture disk (the
UAS hang on gmnro03 (Ciocârlia)) must stall one call, not a whole dawn run.

Stdlib only and Python 3.8 safe (see rovimen_config).
"""

from __future__ import annotations

import functools
import json
import logging
import os
import re
import subprocess
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - stations are Linux
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# ffprobe
# ---------------------------------------------------------------------------


def ffprobe_json(path: Path, *, timeout: float, entries: str = 'stream',
                 select: Optional[str] = 'v:0',
                 args: Sequence[str] = ()) -> Optional[Dict[str, Any]]:
    """``ffprobe -of json`` for *path*; None on any failure or timeout.

    *entries* is the ``-show_entries`` value (``stream=height``,
    ``format=duration``, ...). *args* are further ffprobe options
    (``-count_packets``, ``-skip_frame nokey``). *timeout* is required on
    purpose. A failure is logged at debug level; callers decide what to warn.
    """
    cmd = ['ffprobe', '-v', 'error']
    if select:
        cmd += ['-select_streams', select]
    cmd += list(args)
    cmd += ['-show_entries', entries, '-of', 'json', str(path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug('ffprobe %s failed: %s', path, exc)
        return None
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout or '{}')
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def first_stream(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The first stream of an ``ffprobe_json`` result, ``{}`` when there is none."""
    streams = (data or {}).get('streams') or [{}]
    first = streams[0]
    return first if isinstance(first, dict) else {}


def probe_video_height(path: Path, timeout: float = 10) -> Optional[int]:
    """Height of the first video stream from the container header, or None."""
    try:
        return int(first_stream(ffprobe_json(path, timeout=timeout,
                                             entries='stream=height'))['height'])
    except (KeyError, TypeError, ValueError):
        return None


def probe_duration(path: Path, timeout: float = 10) -> Optional[float]:
    """Container duration in seconds, or None."""
    data = ffprobe_json(path, timeout=timeout, entries='format=duration', select=None)
    try:
        return float((data or {})['format']['duration'])
    except (KeyError, TypeError, ValueError):
        return None


_COLOUR_TAG_OPTS = (
    ('color_space', '-colorspace'),
    ('color_primaries', '-color_primaries'),
    ('color_transfer', '-color_trc'),
    ('color_range', '-color_range'),
)


def colour_tag_args(path: Path, timeout: float = 20) -> List[str]:
    """ffmpeg output options repeating *path*'s colour tags (``[]`` on failure).

    Filters that stay in YUV (pad, drawtext) pass pixels through unconverted,
    so the tags must follow them or a player may decode with another matrix.
    """
    stream = first_stream(ffprobe_json(
        path, timeout=timeout,
        entries='stream=color_space,color_primaries,color_transfer,color_range'))
    args: List[str] = []
    for key, opt in _COLOUR_TAG_OPTS:
        val = stream.get(key)
        if val and val not in ('unknown', 'reserved', 'unspecified'):
            args += [opt, val]
    return args


# ---------------------------------------------------------------------------
# Matroska header reader (no subprocess)
# ---------------------------------------------------------------------------

_MKV_SEGMENT = 0x18538067
_MKV_TRACKS = 0x1654AE6B
_MKV_CLUSTER = 0x1F43B675
_MKV_TRACK_ENTRY = 0xAE
_MKV_TRACK_TYPE = 0x83
_MKV_VIDEO = 0xE0
_MKV_PIXEL_WIDTH = 0xB0
_MKV_PIXEL_HEIGHT = 0xBA
_MKV_PIXEL_CROPS = (0x54AA, 0x54BB, 0x54CC, 0x54DD)
# ffmpeg's muxer writes Tracks within the first kilobyte; anything that has not
# shown it by 64 KB is left to ffprobe.
_MKV_HEAD_BYTES = 64 * 1024


def _ebml_vint(buf: bytes, pos: int, keep_marker: bool) -> Tuple[int, int]:
    """Decode the EBML variable-length integer at *pos* into (value, length)."""
    first = buf[pos]
    length = 9 - first.bit_length()
    if first == 0 or pos + length > len(buf):
        raise ValueError('bad EBML integer')
    value = first if keep_marker else first & (0xFF >> length)
    for b in buf[pos + 1:pos + length]:
        value = (value << 8) | b
    return value, length


def _ebml_elements(buf: bytes, pos: int, end: int) -> Iterator[Tuple[int, int, Optional[int]]]:
    """Yield (id, data_start, data_end) for the EBML elements in ``buf[pos:end]``.

    An element of unknown size (a live-muxed Segment or Cluster) comes back
    with data_end None and ends the walk.
    """
    while pos < end:
        eid, id_len = _ebml_vint(buf, pos, keep_marker=True)
        size, size_len = _ebml_vint(buf, pos + id_len, keep_marker=False)
        start = pos + id_len + size_len
        if size == (1 << (7 * size_len)) - 1:
            yield eid, start, None
            return
        yield eid, start, start + size
        pos = start + size


def _ebml_children(buf: bytes, start: int, stop: int) -> Dict[int, Tuple[int, int]]:
    """Map each child ID of a small, fully-sized element to its (start, stop)."""
    children: Dict[int, Tuple[int, int]] = {}
    for eid, s, e in _ebml_elements(buf, start, stop):
        if e is None or e > stop:
            raise ValueError('child overruns its parent')
        children.setdefault(eid, (s, e))
    return children


def _ebml_uint(buf: bytes, span: Tuple[int, int]) -> int:
    return int.from_bytes(buf[span[0]:span[1]], 'big')


def mkv_frame_size(mkv: Path) -> Optional[Tuple[int, int]]:
    """(width, height) of the first video track from the Matroska header.

    None whenever the header cannot answer for certain (not Matroska, a
    cropped track, Tracks not within the first 64 KB), so the caller asks
    ffprobe instead.
    """
    try:
        with open(mkv, 'rb') as f:
            buf = f.read(_MKV_HEAD_BYTES)
        segment = next(((s, e) for eid, s, e in _ebml_elements(buf, 0, len(buf))
                        if eid == _MKV_SEGMENT), None)
        if segment is None:
            return None
        seg_end = len(buf) if segment[1] is None else min(segment[1], len(buf))
        for eid, start, stop in _ebml_elements(buf, segment[0], seg_end):
            if eid == _MKV_CLUSTER or stop is None:
                return None
            if eid != _MKV_TRACKS:
                continue
            for tid, ts, te in _ebml_elements(buf, start, min(stop, len(buf))):
                if tid != _MKV_TRACK_ENTRY or te is None:
                    continue
                entry = _ebml_children(buf, ts, te)
                if (_MKV_TRACK_TYPE not in entry or _MKV_VIDEO not in entry
                        or _ebml_uint(buf, entry[_MKV_TRACK_TYPE]) != 1):
                    continue
                video = _ebml_children(buf, *entry[_MKV_VIDEO])
                if any(_ebml_uint(buf, video[c]) for c in _MKV_PIXEL_CROPS if c in video):
                    return None
                if _MKV_PIXEL_WIDTH not in video or _MKV_PIXEL_HEIGHT not in video:
                    return None
                return (_ebml_uint(buf, video[_MKV_PIXEL_WIDTH]),
                        _ebml_uint(buf, video[_MKV_PIXEL_HEIGHT]))
            return None
    except (OSError, ValueError, IndexError):
        pass
    return None


# ---------------------------------------------------------------------------
# ffmpeg flags
# ---------------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def _ffmpeg_version() -> Optional[Tuple[int, int]]:
    """(major, minor) of the ffmpeg on PATH, None for an unparsable (git) build.

    Raises when the probe itself fails; lru_cache does not cache exceptions,
    so the next call probes again.
    """
    first = subprocess.run(['ffmpeg', '-version'], capture_output=True,
                           text=True, timeout=30).stdout.split('\n', 1)[0]
    m = re.match(r'ffmpeg version n?(\d+)\.(\d+)', first)
    return (int(m.group(1)), int(m.group(2))) if m else None


def passthrough_args() -> List[str]:
    """ffmpeg flag that emits exactly the coded frames, for this box's ffmpeg.

    ``-fps_mode`` only exists from ffmpeg 5.1; the Ubuntu 20.04 stations
    (gmnro05 (Bistrița), gmnro08 (Brașov)) ship 4.2, which rejects the whole
    command line, so 4.x gets ``-vsync passthrough``. An unparsable version
    (a git build) is treated as current. A failed probe gets ``-vsync``, which
    every ffmpeg from 4.2 to 8.0 accepts (deprecated from 5.1, still honoured):
    on 2026-10-30 a probe on gmnro05 under dawn load fell back to
    ``-fps_mode`` and the cached answer failed every stack of the run.
    """
    try:
        version = _ffmpeg_version()
    except (OSError, subprocess.SubprocessError):
        return ['-vsync', 'passthrough']
    if version is not None and version < (5, 1):
        return ['-vsync', 'passthrough']
    return ['-fps_mode', 'passthrough']


# Cameras that flag full-range data as limited need this before any other
# filter, identically in the encoder, the stacker and colour sampling.
FULL_RANGE_SCALE = 'scale=in_range=full:out_range=full'

# The colour cameras' capture frame. An encoded clip or a published stack adds
# its OSD bar below it, so anything taller carries a bar of the difference.
CAPTURE_WIDTH = 1280
CAPTURE_HEIGHT = 720


# ---------------------------------------------------------------------------
# Per-chunk locks
# ---------------------------------------------------------------------------


def lock_dir_name(kind: str) -> str:
    """The hidden per-night directory holding *kind* locks (``.stack_locks``).

    A sibling of the night's ``stacks/``, so locks travel with the data they
    guard and never match the ``*.webp`` globs the uploader and janitor walk.
    """
    return f'.{kind}_locks'


def _lock_is_current(fd: int, lock_path: Path) -> bool:
    """True when *fd* is still the file at *lock_path* (not unlinked by its last holder)."""
    try:
        st = os.stat(lock_path)
        held = os.fstat(fd)
    except OSError:
        return False
    return (st.st_dev, st.st_ino) == (held.st_dev, held.st_ino)


@contextmanager
def chunk_lock(mkv: Path, kind: str, timeout: float = 0.0) -> Iterator[bool]:
    """Yield True while this process owns *kind* work on *mkv*.

    An exclusive ``fcntl.flock`` on ``<night>/.<kind>_locks/<chunk>.lock``.
    A station stacks the same chunk from two directions (color_capture fires
    ``stacker.py --chunk`` as each segment closes, dawn's night pass sweeps
    what is not flagged yet); the second one stands down and yields False,
    leaving the chunk for the next pass, so unflagged work is only deferred.

    There is no stale lock to time out: the kernel drops an flock when the
    holder exits or its fd closes, so even a killed worker releases it.
    *timeout* only bounds how long a waiter blocks before giving up, and
    defaults to giving up at once.

    The holder unlinks the lock file before releasing it; left in place,
    every chunk left one behind (2,000-2,800 files per camera-night). A
    waiter that opened the file before the unlink would then win a lock on a
    file nobody else can see, so after taking the lock it checks that its
    file is still the one at the path, and starts over on the new one if
    not. An unusable lock directory yields True (unguarded) rather than
    dropping the work.
    """
    mkv = Path(mkv)
    lock_path = mkv.parent / lock_dir_name(kind) / f'{mkv.name}.lock'
    if fcntl is None:
        yield True
        return
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        logger.debug('%s lock unavailable for %s (%s); proceeding unguarded',
                     kind, mkv.name, exc)
        yield True
        return

    acquired = False
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.25)
                continue
            if _lock_is_current(fd, lock_path):
                acquired = True
                break
            # The holder we waited on removed this file as it released it.
            os.close(fd)
            fd = -1
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
            except OSError:
                break
        yield acquired
    finally:
        if acquired:
            # Unlink while still holding it, so a waiter sees the change.
            try:
                os.unlink(lock_path)
            except OSError:
                pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        if fd >= 0:
            os.close(fd)


def sweep_stale_locks(night_dir: Path, kind: str) -> int:
    """Remove the night's *kind* lock files nobody holds; return how many went.

    Lock files are removed on release, but a worker killed mid-chunk (such as
    color_capture's 120 s stacker budget) still leaves its file behind, and
    nights stacked before 2026-09-26 left one per chunk.
    """
    if fcntl is None:
        return 0
    lock_dir = Path(night_dir) / lock_dir_name(kind)
    removed = 0
    try:
        candidates = list(lock_dir.glob('*.lock'))
    except OSError:
        return 0
    for path in candidates:
        try:
            fd = os.open(str(path), os.O_RDWR)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue            # a worker holds it right now
            if _lock_is_current(fd, path):
                try:
                    os.unlink(path)
                    removed += 1
                except OSError:
                    pass
        finally:
            os.close(fd)
    return removed


# ---------------------------------------------------------------------------
# VAAPI detection
# ---------------------------------------------------------------------------

# libva picks iHD on every Intel GPU, but iHD only supports Gen9+; Haswell and
# Broadwell (gmnro05 (Bistrița)) need i965.
VAAPI_AUTO_FALLBACK_DRIVERS = ('i965',)
# The probe encodes at the capture size. A tiny test frame is rejected by
# drivers with a minimum encode size: the admin workstation's AMD GPU refuses 32x32 and 64x64
# ("width 128-4096") yet encodes 720p, and Intel iHD once refused 16x16
# (11473d59). The same source as ensure_vaapi.sh and hardware_assessment.sh,
# so the three cannot disagree about a GPU.
VAAPI_PROBE_SOURCE = 'testsrc=duration=0.08:size=1280x720:rate=25'

_vaapi_ok: Dict[Tuple[str, str], bool] = {}
_vaapi_device_cache: Optional[str] = None
_vaapi_driver_cache = ''
_vaapi_probe_lock = threading.Lock()


def vaapi_driver_candidates(cfg: Optional[Dict[str, Any]]) -> List[str]:
    """libva driver names to probe, in order ('' = libva's own choice).

    ``vaapi_driver``: 'auto' (default) tries libva's choice then the pre-Gen9
    Intel fallback; any other value is used alone.
    """
    setting = (cfg or {}).get('vaapi_driver', 'auto')
    token = setting.strip() if isinstance(setting, str) else ''
    if token and token.lower() != 'auto':
        return [token]
    return [''] + list(VAAPI_AUTO_FALLBACK_DRIVERS)


def check_vaapi(device: str, driver: str = '') -> bool:
    """Whether an h264_vaapi encode works on *device* with *driver* (cached)."""
    key = (device, driver)
    if key in _vaapi_ok:
        return _vaapi_ok[key]
    env = dict(os.environ)
    if driver:
        env['LIBVA_DRIVER_NAME'] = driver
    try:
        result = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error',
             '-vaapi_device', device,
             '-f', 'lavfi', '-i', VAAPI_PROBE_SOURCE,
             '-vf', 'format=nv12,hwupload',
             '-c:v', 'h264_vaapi', '-qp', '20',
             '-f', 'null', '-'],
            capture_output=True, timeout=10, env=env,
        )
        ok = result.returncode == 0
    except Exception:
        ok = False
    _vaapi_ok[key] = ok
    if not ok:
        logger.warning('VAAPI unavailable on %s (driver %s)', device, driver or 'default')
    return ok


def render_nodes() -> List[Path]:
    """DRM render nodes present on this host, in name order."""
    dri = Path('/dev/dri')
    return sorted(dri.glob('renderD*')) if dri.exists() else []


def find_vaapi_device(cfg: Optional[Dict[str, Any]] = None) -> Optional[str]:
    """The first working VAAPI render node, probed once per process.

    Also settles which libva driver works (see ``vaapi_env``). *cfg* is only
    read on the first call.
    """
    global _vaapi_device_cache, _vaapi_driver_cache
    if _vaapi_device_cache is not None:
        return _vaapi_device_cache or None
    with _vaapi_probe_lock:
        if _vaapi_device_cache is not None:
            return _vaapi_device_cache or None
        devices = render_nodes()
        for driver in vaapi_driver_candidates(cfg):
            for device in devices:
                if check_vaapi(str(device), driver):
                    _vaapi_driver_cache = driver
                    _vaapi_device_cache = str(device)
                    logger.info('VAAPI device: %s (driver %s)', device, driver or 'default')
                    return _vaapi_device_cache
        _vaapi_device_cache = ''
        logger.warning('No working VAAPI device found; using libx264 fallback')
        return None


def vaapi_env(base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Environment for an ffmpeg VAAPI subprocess, with the probed driver."""
    env = dict(os.environ if base is None else base)
    if _vaapi_driver_cache:
        env['LIBVA_DRIVER_NAME'] = _vaapi_driver_cache
    return env
