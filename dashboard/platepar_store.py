"""Persistent, process-wide platepar cache.

Platepars are fetched live from each station's /api/platepar endpoint by
rovimen_dashboard.py.  This module keeps a disk-backed copy keyed by camera
code so that coverage can be computed even for stations that are currently
offline — we use the last-known platepar.

Storage: ROVIMEN_PLATEPAR_STORE env var, default /opt/rovimen/platepar_store.json
Schema:  { "<CAM_CODE>": { "lat": …, "lon": …, "az_centre": …, … } }
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
from pathlib import Path

from atomic_write import write_atomic

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_store: dict[str, dict] = {}

STORE_PATH = Path(os.environ.get("ROVIMEN_PLATEPAR_STORE", "/opt/rovimen/platepar_store.json"))

_LOCK_PATH = STORE_PATH.with_suffix(".lock")
_disk_mtime_ns: int | None = None

_REQUIRED_FIELDS = {"lat", "lon", "az_centre", "alt_centre", "fov_h", "fov_v"}

_OPTIONAL_FIELDS = {
    "rotation_from_horiz", "F_scale", "X_res", "Y_res",
    "x_poly_fwd", "x_poly", "y_poly_fwd", "y_poly",
    "equal_aspect", "asymmetry_corr", "distortion_type",
    "RA_d", "dec_d", "pos_angle_ref",
}


def _stat_mtime() -> int | None:
    try:
        return STORE_PATH.stat().st_mtime_ns
    except OSError:
        return None


def _read_disk() -> dict[str, dict]:
    """The file as it is now, or {} when absent/unreadable. Never raises."""
    try:
        data = json.loads(STORE_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        logger.warning("platepar_store: failed to read %s: %s", STORE_PATH, exc)
        return {}


def load() -> None:
    """Load store from disk.  Called once at import time."""
    global _store, _disk_mtime_ns
    if not STORE_PATH.exists():
        return
    with _lock:
        _store = _read_disk()
        _disk_mtime_ns = _stat_mtime()
    logger.info("platepar_store: loaded %d cameras from %s: %s",
                len(_store), STORE_PATH, ", ".join(sorted(_store.keys())))


def _reload_if_changed_locked() -> None:
    """Adopt another worker's write. Caller holds ``_lock``.

    Each gunicorn worker keeps its own copy of this module, so a platepar
    refreshed by one worker was invisible to the others until they happened
    to refresh the same station -- coverage and network stats disagreed
    between requests. One stat per read keeps every worker on the file.
    """
    global _store, _disk_mtime_ns
    current = _stat_mtime()
    if current is None or current == _disk_mtime_ns:
        return
    _store = _read_disk()
    _disk_mtime_ns = current


def _entry(pp: dict) -> dict:
    entry = {k: pp[k] for k in _REQUIRED_FIELDS}
    for k in _OPTIONAL_FIELDS:
        if k in pp and pp[k] is not None:
            entry[k] = pp[k]
    return entry


def update(cam_code: str, data: dict) -> None:
    """Persist one camera's platepar.  Silently skips incomplete entries."""
    if not _REQUIRED_FIELDS.issubset(data):
        return
    with _lock:
        _merge_and_write_locked({cam_code: _entry(data)})


def update_host(host_payload: dict) -> None:
    """Persist all cameras from a full /api/platepar host response."""
    changed = False
    skipped = []
    fresh: dict[str, dict] = {}
    for cam_code, pp in host_payload.items():
        if isinstance(pp, dict) and _REQUIRED_FIELDS.issubset(pp):
            fresh[cam_code] = _entry(pp)
            changed = True
        else:
            skipped.append(cam_code)
    if changed:
        with _lock:
            _merge_and_write_locked(fresh)
    if skipped:
        logger.info("platepar_store: skipped %s (missing required fields or error)", ", ".join(skipped))
    if changed:
        logger.info("platepar_store: updated, now %d cameras total", len(_store))
        _notify_change()


_change_callbacks: list = []


def on_change(callback) -> None:
    _change_callbacks.append(callback)


def _notify_change() -> None:
    for cb in _change_callbacks:
        try:
            cb()
        except Exception:
            pass


def get_all() -> dict[str, dict]:
    with _lock:
        _reload_if_changed_locked()
        return dict(_store)


def _merge_and_write_locked(fresh: dict[str, dict]) -> None:
    """Merge ``fresh`` onto the FILE and write it back. Caller holds ``_lock``.

    Writing this worker's memory whole let the last writer erase every other
    worker's cameras (a stale worker refreshing station B wrote station A's
    old pointing back over the new one). Read-merge-write under the sidecar
    flock makes the file the union of all writers, and memory follows it.
    """
    global _store, _disk_mtime_ns
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_LOCK_PATH, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            merged = _read_disk()
            merged.update(fresh)
            _store = merged
            _write_locked()
            _disk_mtime_ns = _stat_mtime()
    except Exception as exc:
        logger.warning("platepar_store: merge/write failed: %s", exc)
        _store.update(fresh)


def _write_locked() -> None:
    try:
        STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        # Per-process temp: gunicorn runs multiple workers, so a shared ".tmp"
        # path races (two workers write+rename the same file -> intermittent
        # "[Errno 2] .tmp -> .json" and a possibly torn read). The rename is
        # atomic, so a unique temp per worker makes concurrent writes safe.
        write_atomic(STORE_PATH, json.dumps(_store, indent=2),
                     tmp=STORE_PATH.with_suffix(f".tmp.{os.getpid()}"))
    except Exception as exc:
        logger.warning("platepar_store: write failed: %s", exc)


load()
