"""On-disk store for built per-night detections payloads.

Building a past night's Detections payload lists every camera's night on the
Storage Box (meteors/, stacks/, timelapse/, ffthumbs/, rms/ and state.json).
Outside the archive index's hot window nothing is in memory, so a cold build
walks ~50 cameras over sshfs: 39 s serially, ~10 s at the mount's parallel
limit (measured 2026-09-26), against nginx's 30 s proxy timeout. Those nights
barely change, so the last build is kept here, shared by every gunicorn worker
and surviving restarts, and served at once while a stale copy is rebuilt in
the background (see routes/detections.py).

Files are gzip JSON, ``<key>.json.gz``, written atomically. A file whose
``schema`` differs from ``SCHEMA`` is ignored, so a payload-shape change only
needs the constant bumped.
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Bump when the detections payload changes shape, so old builds are rebuilt.
SCHEMA = 1

_KEY_RE = re.compile(r"^[0-9A-Za-z_-]{1,64}$")
# Newest files kept; a gzip'd fleet night is ~0.5 MB, so this bounds the
# store at a few hundred MB on the VPS root disk.
MAX_FILES = 400
# A rebuild claim older than this is from a worker that died mid-build.
CLAIM_STALE_S = 600.0


@dataclass(frozen=True)
class StoredPayload:
    payload: dict[str, Any]
    built_at: float      # epoch seconds
    fresh_until: float   # epoch seconds; after this the copy is served stale

    def is_fresh(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) < self.fresh_until


class NightPayloadStore:
    """Directory of built payloads. ``root=None`` disables it: every load
    misses and every save is dropped, which is what tests and single-process
    dev get unless they opt in."""

    def __init__(self, root: Path | None, max_files: int = MAX_FILES) -> None:
        self.root = Path(root) if root is not None else None
        self.max_files = max_files

    @property
    def enabled(self) -> bool:
        return self.root is not None

    def _path(self, key: str) -> Path | None:
        if self.root is None or not _KEY_RE.match(key):
            return None
        return self.root / f"{key}.json.gz"

    def load(self, key: str) -> StoredPayload | None:
        path = self._path(key)
        if path is None:
            return None
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                doc = json.load(fh)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, EOFError):
            logger.warning("unreadable stored payload %s; ignoring", path)
            return None
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA:
            return None
        payload = doc.get("payload")
        if not isinstance(payload, dict):
            return None
        try:
            return StoredPayload(
                payload=payload,
                built_at=float(doc["built_at"]),
                fresh_until=float(doc["fresh_until"]),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def save(self, key: str, payload: dict[str, Any], ttl_s: float,
             now: float | None = None) -> bool:
        """Write ``payload`` as fresh for ``ttl_s`` seconds. Fail-soft: a disk
        problem only costs the next cold build."""
        path = self._path(key)
        if path is None:
            return False
        built_at = time.time() if now is None else now
        doc = {
            "schema": SCHEMA,
            "built_at": built_at,
            "fresh_until": built_at + max(0.0, float(ttl_s)),
            "payload": payload,
        }
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=f".{key}.", suffix=".tmp", dir=path.parent)
            try:
                with os.fdopen(fd, "wb") as raw, gzip.GzipFile(fileobj=raw, mode="wb",
                                                               compresslevel=5) as gz:
                    gz.write(json.dumps(doc, separators=(",", ":")).encode("utf-8"))
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError as exc:
            logger.warning("could not store payload %s: %s", path, exc)
            return False
        self._trim()
        return True

    def try_claim(self, key: str, now: float | None = None) -> bool:
        """Claim the rebuild of ``key`` across processes (O_EXCL lock file),
        so sibling gunicorn workers holding the same stale copy do not all
        list the night at once. A claim older than CLAIM_STALE_S is taken
        over. Always True when the store is disabled."""
        path = self._path(key)
        if path is None:
            return self.root is None
        lock = path.with_name(f".{key}.claim")
        now = time.time() if now is None else now
        try:
            lock.parent.mkdir(parents=True, exist_ok=True)
            try:
                if now - lock.stat().st_mtime > CLAIM_STALE_S:
                    lock.unlink()
            except FileNotFoundError:
                pass
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            return True   # a disk problem must not stop the rebuild

    def release(self, key: str) -> None:
        path = self._path(key)
        if path is None:
            return
        try:
            path.with_name(f".{key}.claim").unlink()
        except OSError:
            pass

    def _trim(self) -> None:
        """Drop the oldest files beyond ``max_files``."""
        try:
            files = sorted(self.root.glob("*.json.gz"), key=lambda p: p.stat().st_mtime)
        except OSError:
            return
        for old in files[: max(0, len(files) - self.max_files)]:
            try:
                old.unlink()
            except OSError:
                pass
