"""Shared station configuration helpers.

One home for the config lookups that every station script used to copy:
where ``config.json`` lives, the colour-capture root, the log directory,
per-camera blocks, service switches, CPU cores, and safe writes of JSON files
(``config.json`` above all).

Stdlib only and Python 3.8 safe: station units run under the system python
(archive-upload, pusher, indexer) and under the RMS venvs (3.8.10 on gmnro05
(Bistrița) and gmnro08 (Brașov)), so this module must import on all of them.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

try:
    import fcntl
except ImportError:  # pragma: no cover - stations are Linux
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

# Keys that may name the colour-capture root, in priority order. Every station
# script used to spell this chain out by hand (18 copies); they all agreed.
CAPTURE_ROOT_KEYS = (
    'videocapture_path',
    'color_video_path',
    'reenc_path',
    'color_capture_path',
    'ssd_color_path',
)

CONFIG_LOCK_NAME = '.config.lock'
_DEFAULTS_FILE = Path(__file__).resolve().with_name('config_defaults.json')


def config_path() -> Path:
    """The station's ``config.json``.

    ``~/rovimen_scripts/config.json``; the pre-2026 ``~/meteor_detector``
    location is still honoured when only it exists.
    """
    home = Path.home()
    for candidate in (home / 'rovimen_scripts' / 'config.json',
                      home / 'meteor_detector' / 'config.json'):
        if candidate.exists():
            return candidate
    return home / 'rovimen_scripts' / 'config.json'


CONFIG_PATH = config_path()


def load_config(path: Optional[Path] = None) -> Dict[str, Any]:
    """Parse ``config.json`` (or *path*); ``{}`` when missing or unreadable."""
    target = Path(path) if path is not None else config_path()
    try:
        data = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        logger.warning('config %s unreadable (%s); using an empty config', target, exc)
        return {}
    return data if isinstance(data, dict) else {}


def configured_capture_root(cfg: Dict[str, Any]) -> Path:
    """The colour-capture root the config names, whether or not it is usable."""
    for key in CAPTURE_ROOT_KEYS:
        value = cfg.get(key)
        if value:
            return Path(value)
    return Path.home() / 'color_capture'


def capture_fallback_root(cfg: Dict[str, Any]) -> Path:
    """Where colour capture goes while the configured root is unusable:
    ``capture_fallback.path``, by default ``~/color_capture`` on the station's
    own disk."""
    block = cfg.get('capture_fallback') or {}
    value = block.get('path') if isinstance(block, dict) else None
    return Path(value) if value else Path.home() / 'color_capture'


# A create-and-delete probe that has not returned after this long counts as a
# dead disk. A hung probe thread is left behind (a syscall cannot be cancelled),
# and while one hangs the directory is reported unwritable without a new probe.
WRITE_PROBE_TIMEOUT_S = 10.0
_hung_since: Dict[str, float] = {}     # directory -> start of its oldest unfinished probe
_probes_lock = threading.Lock()

# How long a probe's answer stands. capture_root() runs for every
# flags_manager state lookup, so without this one upload scan created and
# deleted thousands of probe files on the capture disk, around the clock. A
# disk that dies (or comes back) is still noticed within this window.
WRITE_PROBE_CACHE_S = 30.0
_probe_results: Dict[str, Tuple[float, bool]] = {}   # directory -> (monotonic time, writable)


def _write_probe(directory: Path) -> bool:
    """Create and delete a hidden file in *directory*. Permission bits are
    not enough: on 2026-11-09 gmnro01 (Ghirdoveni)'s USB disk failed writes
    at 12:36 UTC and every create returned EIO, while the mount still read
    ``rw`` and os.access said writable, so colour capture stayed on it."""
    key = str(directory)
    now = time.monotonic()
    with _probes_lock:
        started = _hung_since.get(key)
        if started is not None and now - started >= WRITE_PROBE_TIMEOUT_S:
            return False
        _hung_since.setdefault(key, now)
    result: list = []

    def run() -> None:
        try:
            fd, name = tempfile.mkstemp(prefix='.rovimen-write-probe-', dir=key)
            os.close(fd)
            os.unlink(name)
            result.append(True)
        except OSError:
            result.append(False)
        finally:
            with _probes_lock:
                _hung_since.pop(key, None)

    worker = threading.Thread(target=run, name='capture-root-write-probe', daemon=True)
    worker.start()
    worker.join(WRITE_PROBE_TIMEOUT_S)
    return bool(result) and result[0]


def _writable_dir(path: Path) -> bool:
    """True if *path* is (or could be created as) a directory this user can
    write. A dead disk can answer EIO, which counts as not writable."""
    try:
        probe = path
        while not probe.exists():
            if probe.parent == probe:
                return False
            probe = probe.parent
        if not (probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)):
            return False
    except OSError:
        return False
    return _cached_write_probe(probe)


def _cached_write_probe(directory: Path) -> bool:
    """_write_probe's answer for *directory*, at most WRITE_PROBE_CACHE_S old."""
    key = str(directory)
    with _probes_lock:
        hit = _probe_results.get(key)
    if hit is not None and time.monotonic() - hit[0] < WRITE_PROBE_CACHE_S:
        return hit[1]
    ok = _write_probe(directory)
    with _probes_lock:
        _probe_results[key] = (time.monotonic(), ok)
    return ok


_fallback_logged: set = set()


def capture_root(cfg: Dict[str, Any]) -> Path:
    """The colour-capture root: ``<root>/<camera>/<night>/*_color.mkv``.

    The configured root while it is writable; otherwise, unless
    ``capture_fallback.enabled`` is false, the fallback root on the station's
    own disk (capture_fallback_root). A colour disk is the fleet's most common
    hardware casualty: when gmnro01 (Ghirdoveni)'s USB disk vanished on
    2026-10-19, ``/mnt/data`` reverted to a root-owned stub and colour capture
    crash-looped for two nights. Checked on every call (the write probe's
    answer is reused for WRITE_PROBE_CACHE_S), so a long-running service
    follows a disk that dies or comes back; the janitor applies the
    fallback's tighter disk policy (capture_fallback.disk / .retention).
    """
    primary = configured_capture_root(cfg)
    if _writable_dir(primary):
        return primary
    block = cfg.get('capture_fallback') or {}
    if isinstance(block, dict) and block.get('enabled', True) is False:
        return primary
    fallback = capture_fallback_root(cfg)
    if fallback == primary or not _writable_dir(fallback):
        return primary
    if str(primary) not in _fallback_logged:
        _fallback_logged.add(str(primary))
        logger.warning('colour capture root %s is not writable; using the fallback %s',
                       primary, fallback)
    return fallback


def capture_on_fallback(cfg: Dict[str, Any]) -> bool:
    """True while colour capture is diverted to the fallback root."""
    return capture_root(cfg) != configured_capture_root(cfg)


def ffthumb_root(cfg: Dict[str, Any]) -> Path:
    """Where the detection indexer renders the BW FF thumbnails:
    ``<root>/<camera>/<night>/<stem>.webp``.

    Deliberately not under the colour-capture root: the thumbnails come from
    RMS's FF files and must exist whether or not colour video does. They used
    to sit in ``<capture root>/<cam>/<night>/ffthumbs/``, so when gmnro01
    (Ghirdoveni)'s colour disk vanished on 2026-10-19 every detection stayed
    "clip pending" with no image. ``ffthumb_path`` overrides the default.
    """
    value = cfg.get('ffthumb_path')
    if value:
        return Path(value)
    return Path.home() / 'rovimen_scripts' / 'ffthumbs'


def legacy_ffthumb_dir(cfg: Dict[str, Any], cam: str, night: str) -> Path:
    """The pre-2026-10-21 thumbnail directory, still read for nights rendered
    there (see ffthumb_root)."""
    return capture_root(cfg) / cam / night / 'ffthumbs'


def log_dir(cfg: Dict[str, Any]) -> Path:
    """Where station scripts write their logs.

    ``log_path`` is ``null`` in ``config_defaults.json`` and config_migrate
    copies missing keys as they are, so a present-but-null value must fall
    back like a missing one (``Path(None)`` raises TypeError).
    """
    value = cfg.get('log_path')
    return Path(value) if value else Path.home() / 'logs'


def station_cfg(cfg: Dict[str, Any], camera: str) -> Dict[str, Any]:
    """The ``stations.<camera>`` block, ``{}`` when absent or malformed."""
    stations = cfg.get('stations')
    block = stations.get(camera) if isinstance(stations, dict) else None
    return block if isinstance(block, dict) else {}


_COORDS_DM_RE = re.compile(
    r'(\d+)\s+(\d+(?:\.\d+)?)\s*([NS])\s+(\d+)\s+(\d+(?:\.\d+)?)\s*([EW])')
_COORDS_DEC_RE = re.compile(r'(\d+(?:\.\d+)?)\s*([NS])\s+(\d+(?:\.\d+)?)\s*([EW])')


def station_location(cfg: Dict[str, Any],
                     camera_cfg: Optional[Dict[str, Any]] = None) -> Optional[Tuple[float, float]]:
    """(latitude, longitude) in degrees, east positive, or None.

    Order: the camera's own ``latitude``/``longitude``, the station's
    top-level ``latitude``/``longitude`` (written by the installer), then the
    overlay's ``coords`` string ("45.30N  26.11E" or "45 00N  025 00E").
    The one implementation: colour calibration (encoder, stacker), colour
    capture's sun schedule, the detection indexer, the sky dome and the
    watchdog's reboot deferral all read the location here.
    """
    for src in (camera_cfg or {}, cfg or {}):
        lat, lon = src.get('latitude'), src.get('longitude')
        if lat is None or lon is None:
            continue
        try:
            return float(lat), float(lon)
        except (TypeError, ValueError):
            continue
    coords = str(((cfg or {}).get('overlay') or {}).get('coords') or '')
    m = _COORDS_DM_RE.search(coords)
    if m:
        lat = float(m.group(1)) + float(m.group(2)) / 60.0
        lon = float(m.group(4)) + float(m.group(5)) / 60.0
        return (-lat if m.group(3) == 'S' else lat), (-lon if m.group(6) == 'W' else lon)
    m = _COORDS_DEC_RE.search(coords)
    if m:
        lat, lon = float(m.group(1)), float(m.group(3))
        return (-lat if m.group(2) == 'S' else lat), (-lon if m.group(4) == 'W' else lon)
    return None


_defaults_cache: Optional[Dict[str, Any]] = None


def config_defaults() -> Dict[str, Any]:
    """The bundled ``config_defaults.json`` (``{}`` when not shipped)."""
    global _defaults_cache
    if _defaults_cache is None:
        try:
            loaded = json.loads(_DEFAULTS_FILE.read_text())
            _defaults_cache = loaded if isinstance(loaded, dict) else {}
        except (OSError, ValueError):
            _defaults_cache = {}
    return _defaults_cache


def meteor_clips_encoded(cfg: Dict[str, Any]) -> bool:
    """False where meteor clips are encoded on demand only.

    CPU-starved boxes (gmnro05 (Bistrița): 5 cameras on a 4-core Haswell) set
    ``services.reencode.meteor_clips: false``: dawn encodes none of their
    meteor clips, and a clip reaches the archive only through
    ``/api/process_clip`` (a Process click or the dashboard's clip requester).
    """
    services = cfg.get('services')
    re_cfg = services.get('reencode') if isinstance(services, dict) else None
    return not (isinstance(re_cfg, dict) and re_cfg.get('meteor_clips', True) is False)


def service_enabled(cfg: Dict[str, Any], service: str) -> bool:
    """Whether ``services.<service>.enabled`` is on.

    A missing switch falls back to the legacy ``active_services`` list when
    the config has one, else to ``config_defaults.json`` -- the value
    config_migrate would backfill -- so every script agrees on a config that
    has not been migrated yet.
    """
    services = cfg.get('services')
    svc = services.get(service) if isinstance(services, dict) else None
    if isinstance(svc, dict) and 'enabled' in svc:
        return bool(svc['enabled'])
    legacy = cfg.get('active_services')
    if isinstance(legacy, list):
        return service in legacy
    default = config_defaults().get('services', {}).get(service, {})
    return bool(default.get('enabled', False)) if isinstance(default, dict) else False


def cpu_cores(config_file: Optional[Path] = None) -> int:
    """CPU cores for parallelism decisions.

    ``hardware.json`` (written by hardware_assessment.sh next to
    ``config.json``) wins, because it is what the installer sized the station
    for; otherwise ``os.cpu_count()``.
    """
    base = Path(config_file).parent if config_file is not None else config_path().parent
    try:
        cores = int(json.loads((base / 'hardware.json').read_text()).get('cpu_cores', 0))
        if cores >= 1:
            return cores
    except (OSError, ValueError, TypeError, AttributeError):
        pass
    return os.cpu_count() or 1


def atomic_write_json(path: Path, data: Any, *, indent: Optional[int] = 2,
                      mode: Optional[int] = None) -> None:
    """Write *data* as JSON to *path* so readers never see a partial file.

    A private temp file in the same directory (``mkstemp``, so two writers
    never share one), fsync, then ``os.replace``. The old fixed
    ``config.json.tmp`` let two concurrent writers truncate each other's
    temp file and install a half-written config.
    """
    path = Path(path)
    fd, tmp_name = tempfile.mkstemp(prefix=f'.{path.name}.', suffix='.tmp', dir=str(path.parent))
    try:
        with os.fdopen(fd, 'w') as fh:
            json.dump(data, fh, indent=indent)
            fh.write('\n')
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            os.chmod(tmp_name, mode)
        elif path.exists():
            os.chmod(tmp_name, path.stat().st_mode & 0o7777)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


@contextmanager
def config_lock(path: Optional[Path] = None) -> Iterator[None]:
    """Exclusive lock serialising every writer of ``config.json``.

    The lock file is ``.config.lock`` beside ``config.json``. station_api's
    PATCH, config_migrate and update_config_coords all take it, so a
    read-modify-write can never lose another writer's update.
    """
    target = Path(path) if path is not None else config_path()
    lock_file = target.parent / CONFIG_LOCK_NAME
    if fcntl is None:
        yield
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def deep_merge(base: Dict[str, Any], update: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge *update* into *base* in place and return *base*."""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def update_config(patch: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    """Deep-merge *patch* into ``config.json`` under the config lock.

    Re-reads the file after taking the lock (never merges into a cached copy),
    writes atomically, and returns the merged config. Raises OSError/ValueError
    when the file cannot be read or written, leaving it untouched.
    """
    target = Path(path) if path is not None else config_path()
    with config_lock(target):
        current = json.loads(target.read_text()) if target.exists() else {}
        if not isinstance(current, dict):
            raise ValueError(f'{target} does not hold a JSON object')
        merged = deep_merge(copy.deepcopy(current), patch)
        atomic_write_json(target, merged)
    return merged
