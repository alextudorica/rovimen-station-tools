"""Atomic JSON store for hall-of-fame entries.

A hall-of-fame entry is an admin-picked "meteor of the week/month": a
reference to an archived clip (camera + night date + filename) plus a
caption, a period label and a display order. The curated list is rendered
prominently on the public landing (overview) page and exposed on the
versioned public API, so entries only ever reference cameras owned by
``public: true`` stations — the route layer enforces that at write time.

Same persistence as ``compilation_store`` (``json_file_store``): the whole
list lives in one JSON file written atomically (tmp + os.replace), guarded
by a process-wide threading.Lock plus an fcntl.flock sidecar for
cross-process safety.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from json_file_store import JsonFileStore

logger = logging.getLogger(__name__)

# Resolved at module import; override with ROVIMEN_HOF_PATH (e.g. on dev:
# /opt/rovimen-dev/hall_of_fame.json — the dev sandbox can only write there).
_DEFAULT_PATH = Path("/opt/rovimen/hall_of_fame.json")
_PATH = Path(os.environ.get("ROVIMEN_HOF_PATH", str(_DEFAULT_PATH)))
_LOCK_PATH = Path(f"/tmp/rovimen_hof_{_PATH.name}.lock")

_store = JsonFileStore(
    lambda: _PATH, lambda: _LOCK_PATH, empty=list, corrupt_label="hall-of-fame",
    shape_message="hall_of_fame.json wrong shape (%s) — starting from empty list",
    log=logger,
)


def _added_at_ts(entry: dict) -> float:
    from datetime import datetime
    raw = entry.get("added_at", "")
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return 0.0


def load_all() -> list[dict]:
    """Return all entries newest-first (most recently added at the front).

    The landing gallery is a rolling window over the front of this list, so
    freshly-featured clips appear first and the oldest picks roll off the
    visible set as new ones are added (they stay in the store for the manager
    and the public API). The ``order`` field is retained for backward
    compatibility but no longer drives display ordering.
    """
    with _store.locked():
        items = _store.read()
    items.sort(key=_added_at_ts, reverse=True)
    return items


def get(entry_id: str) -> dict | None:
    """Return one entry or None."""
    return _store.find(lambda e: e.get("id") == entry_id)


def add(entry: dict) -> dict:
    """Append a new entry. Raises ValueError on missing id or duplicate clip.

    Duplicate = same (camera, date, filename) triple; the route layer maps
    that to 409 so double-clicking the picker button can't create twins.
    """
    if not entry.get("id"):
        raise ValueError("entry must have an 'id'")
    key = (entry.get("camera"), entry.get("date"), entry.get("filename"))
    with _store.locked():
        items = _store.read()
        for existing in items:
            if (existing.get("camera"), existing.get("date"),
                    existing.get("filename")) == key:
                raise ValueError("clip is already in the hall of fame")
        items.append(entry)
        _store.write(items)
    return entry


def patch(entry_id: str, fields: dict) -> dict | None:
    """Shallow-merge fields into the stored entry. Returns the updated
    record or None if not found."""
    return _store.patch(entry_id, fields)


def delete(entry_id: str) -> bool:
    """Remove by id. Returns True if anything was removed."""
    return _store.delete(entry_id)


def find_by_clip(camera: str, date: str, filename: str) -> dict | None:
    """Return the entry featuring this exact clip, or None. Lets the
    highlights picker mark already-featured clips and toggle them off."""
    return _store.find(lambda e: (e.get("camera") == camera and e.get("date") == date
                                  and e.get("filename") == filename))
