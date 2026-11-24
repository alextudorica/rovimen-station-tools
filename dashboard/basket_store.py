"""Atomic JSON store for per-user download baskets.

A basket is a signed-in user's working set of clips to take home: each
entry references an archived clip (camera + night date + filename) plus an
optional stack filename hint. Checkout (routes/basket.py) streams one zip
with every clip, its stack image and a manifest.

Same persistence as ``hall_of_fame_store`` (``json_file_store``): one JSON
file written atomically (tmp + os.replace), guarded by a process-wide
threading.Lock plus an fcntl.flock sidecar for cross-process safety. The file maps
username -> list of entries; baskets are private to their owner at the
route layer (every call takes the session username, never a client-chosen
one).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from json_file_store import JsonFileStore

logger = logging.getLogger(__name__)

# Resolved at module import; override with ROVIMEN_BASKET_PATH (e.g. on dev:
# /opt/rovimen-dev/download_baskets.json — the dev sandbox can only write there).
_DEFAULT_PATH = Path("/opt/rovimen/download_baskets.json")
_PATH = Path(os.environ.get("ROVIMEN_BASKET_PATH", str(_DEFAULT_PATH)))
_LOCK_PATH = Path(f"/tmp/rovimen_basket_{_PATH.name}.lock")

_store = JsonFileStore(
    lambda: _PATH, lambda: _LOCK_PATH, empty=dict, corrupt_label="basket",
    shape_message="download_baskets.json wrong shape (%s) — starting empty",
    log=logger,
)


class DuplicateClipError(ValueError):
    """The exact clip (camera, date, filename) is already in this basket."""


class BasketFullError(ValueError):
    """The basket is at its item cap."""


def _read_unlocked() -> dict[str, list[dict]]:
    return {u: v for u, v in _store.read().items() if isinstance(v, list)}


def _clip_key(entry: dict) -> tuple:
    return (entry.get("camera"), entry.get("date"), entry.get("filename"))


def list_for(user: str) -> list[dict]:
    """This user's basket entries in insertion order."""
    with _store.locked():
        return list(_read_unlocked().get(user, []))


def find_clip(user: str, camera: str, date: str, filename: str) -> dict | None:
    """The entry holding this exact clip, or None. Backs the UI toggle."""
    key = (camera, date, filename)
    with _store.locked():
        for e in _read_unlocked().get(user, []):
            if _clip_key(e) == key:
                return e
    return None


def add(user: str, entry: dict, max_items: int) -> dict:
    """Append an entry to *user*'s basket.

    Raises DuplicateClipError on the same (camera, date, filename) triple —
    the route maps that to 409 so double-clicking the toggle can't create
    twins — and BasketFullError at the item cap.
    """
    if not entry.get("id"):
        raise ValueError("entry must have an 'id'")
    key = _clip_key(entry)
    with _store.locked():
        baskets = _read_unlocked()
        items = baskets.get(user, [])
        if len(items) >= max_items:
            raise BasketFullError(f"basket is full ({max_items} items)")
        if any(_clip_key(e) == key for e in items):
            raise DuplicateClipError("clip is already in the basket")
        items.append(entry)
        baskets[user] = items
        _store.write(baskets)
    return entry


def remove(user: str, entry_id: str) -> bool:
    """Remove one entry by id. Returns True if anything was removed."""
    with _store.locked():
        baskets = _read_unlocked()
        items = baskets.get(user, [])
        kept = [e for e in items if e.get("id") != entry_id]
        if len(kept) == len(items):
            return False
        baskets[user] = kept
        _store.write(baskets)
    return True


def clear(user: str) -> int:
    """Empty *user*'s basket. Returns the number of entries dropped."""
    with _store.locked():
        baskets = _read_unlocked()
        items = baskets.pop(user, [])
        if items:
            _store.write(baskets)
    return len(items)
