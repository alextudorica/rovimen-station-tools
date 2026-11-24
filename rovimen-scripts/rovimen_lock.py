#!/usr/bin/env python3
"""rovimen_lock.py — readers and cleanup for the legacy ``.locked`` sidecars.

Locks live in each night's state.json (flags_manager). Older chunks can still
carry a JSON sidecar written before that move:

Lock file: <mkv>.locked
Format: {"lock_type": "detection", "detection_time": "20260314_023422"}

Nothing writes sidecars any more; these functions read them and remove them.
"""

from __future__ import annotations

import json
from pathlib import Path


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + '.locked')


def unlock(path: Path) -> None:
    """Remove the .locked sidecar if present."""
    _lock_path(path).unlink(missing_ok=True)


def is_locked(path: Path) -> bool:
    return _lock_path(path).exists()


def chunk_is_locked(path: Path, chunk_state: dict) -> bool:
    """Locked per the chunk's state.json entry (a ``lock``), or by the legacy
    ``.locked`` sidecar."""
    return chunk_state.get('lock') is not None or is_locked(path)


def get_lock_info(path: Path) -> dict | None:
    """Return {'lock_type': ..., 'detection_time': ...} or None if not locked."""
    lp = _lock_path(path)
    if not lp.exists():
        return None
    try:
        data = json.loads(lp.read_text())
        return {
            'lock_type': data.get('lock_type'),
            'detection_time': data.get('detection_time'),
            'meteor_time': data.get('meteor_time'),
        }
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None


def get_lock_type(path: Path) -> str | None:
    """Return lock_type string or None if not locked.  Compat shim for station API."""
    info = get_lock_info(path)
    return info.get('lock_type') if info else None
