"""Atomic JSON store for compilation manifests.

A compilation is a user-curated list of meteor clips that get fetched, ffmpeg-
concat'd, optionally re-encoded with an intro, and (in Phase 2) uploaded to
YouTube. Each manifest carries the cart contents, build progress, output path,
and YouTube metadata. The whole list lives in a single JSON file written
atomically (tmp + os.replace) through ``json_file_store``.

This module is intentionally tiny: no DB, no migrations, no schema validation.
The dashboard validates incoming payloads before calling `upsert`.

Concurrency: a process-wide threading.Lock guards same-process races; an
fcntl.flock on a sidecar file guards races against any future process that
might touch the file (e.g. a future cron-triggered scheduler running in a
separate Python interpreter).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from json_file_store import JsonFileStore

logger = logging.getLogger(__name__)

# Resolved at module import; the dashboard reads ROVIMEN_COMPILATIONS_PATH if
# the operator wants it elsewhere (e.g. on dev: /opt/rovimen-dev/compilations.json).
_DEFAULT_PATH = Path("/opt/rovimen/compilations.json")
_PATH = Path(os.environ.get("ROVIMEN_COMPILATIONS_PATH", str(_DEFAULT_PATH)))
_LOCK_PATH = Path(f"/tmp/rovimen_compilations_{_PATH.name}.lock")

_store = JsonFileStore(
    lambda: _PATH, lambda: _LOCK_PATH, empty=list, corrupt_label="compilations",
    shape_message="compilations.json wrong shape (%s) — starting from empty list",
    log=logger,
)


def load_all() -> list[dict]:
    """Return all manifests, newest first by created_at (best-effort sort)."""
    with _store.locked():
        items = _store.read()
    items.sort(key=lambda m: m.get("created_at", ""), reverse=True)
    return items


def get(manifest_id: str) -> dict | None:
    """Return one manifest or None."""
    return _store.find(lambda m: m.get("id") == manifest_id)


def upsert(manifest: dict) -> dict:
    """Insert or replace by `id`. Returns the stored manifest."""
    mid = manifest.get("id")
    if not mid:
        raise ValueError("manifest must have an 'id'")
    with _store.locked():
        items = _store.read()
        for i, existing in enumerate(items):
            if existing.get("id") == mid:
                items[i] = manifest
                break
        else:
            items.append(manifest)
        _store.write(items)
    return manifest


def patch(manifest_id: str, fields: dict) -> dict | None:
    """Shallow-merge fields into the stored manifest. Returns updated record
    or None if not found. Intended for partial updates from the build thread
    (progress, status) without clobbering concurrent user edits to other
    fields."""
    return _store.patch(manifest_id, fields)


def delete(manifest_id: str) -> bool:
    """Remove by id. Returns True if anything was removed."""
    return _store.delete(manifest_id)
