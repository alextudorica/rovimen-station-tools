"""One JSON document in one file: the persistence behind the small dashboard
stores (``compilation_store``, ``hall_of_fame_store``, ``basket_store``).

The whole document is read and rewritten on every change, written atomically
(``<name>.tmp`` then ``os.replace``, pretty-printed with ``indent=2``) under a
process-wide ``threading.Lock`` plus an ``fcntl.flock`` sidecar, so gunicorn
workers and any separate process serialise their read-modify-writes. A file
that is corrupt is copied aside to ``<stem>.corrupt.<unix-ts>`` and read as
empty; one of the wrong JSON type is read as empty too.

Each store module keeps its own ``_PATH`` / ``_LOCK_PATH`` globals (tests
point them at a temp dir) and its public functions; the store reads the
globals on every call through the getters it is given.
"""

from __future__ import annotations

import fcntl
import json
import logging
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from atomic_write import write_atomic


class JsonFileStore:
    """Locked read-modify-write access to one JSON document.

    ``empty`` is the document's type (``list`` or ``dict``) and builds the
    empty value. ``corrupt_label`` and ``shape_message`` keep each store's own
    log lines (``shape_message`` gets the wrong type's name).
    """

    def __init__(
        self,
        path: Callable[[], Path],
        lock_path: Callable[[], Path],
        *,
        empty: type[list] | type[dict],
        corrupt_label: str,
        shape_message: str,
        log: logging.Logger,
    ) -> None:
        self._path = path
        self._lock_path = lock_path
        self._empty = empty
        self._corrupt_label = corrupt_label
        self._shape_message = shape_message
        self._log = log
        self._thread_lock = threading.Lock()

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Combined thread-lock + file-lock for any read-modify-write."""
        with self._thread_lock:
            lock_path = self._lock_path()
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "w") as lf:
                fcntl.flock(lf, fcntl.LOCK_EX)
                yield

    def read(self) -> Any:
        """The document; call under :meth:`locked`."""
        path = self._path()
        if not path.exists():
            return self._empty()
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            self._log.error("Corrupt %s file %s: %s", self._corrupt_label, path, exc)
            backup = path.with_suffix(f".corrupt.{int(time.time())}")
            try:
                shutil.copy2(path, backup)
            except OSError:
                pass
            return self._empty()
        if not isinstance(data, self._empty):
            self._log.warning(self._shape_message, type(data).__name__)
            return self._empty()
        return data

    def write(self, data: Any) -> None:
        """Replace the document; call under :meth:`locked`."""
        path = self._path()
        write_atomic(path, json.dumps(data, indent=2),
                     tmp=path.with_name(path.name + ".tmp"), mkdir=True)

    # ── List-of-records stores (each record a dict with an ``id``) ──────

    def find(self, match: Callable[[dict], bool]) -> dict | None:
        """The first record ``match`` accepts, or None."""
        with self.locked():
            for record in self.read():
                if match(record):
                    return record
        return None

    def patch(self, record_id: str, fields: dict) -> dict | None:
        """Shallow-merge ``fields`` into the record with this id. Returns the
        updated record, or None if there is none."""
        with self.locked():
            items = self.read()
            for i, existing in enumerate(items):
                if existing.get("id") == record_id:
                    merged = {**existing, **fields}
                    items[i] = merged
                    self.write(items)
                    return merged
        return None

    def delete(self, record_id: str) -> bool:
        """Remove the record with this id. Returns True if anything was removed."""
        with self.locked():
            items = self.read()
            kept = [r for r in items if r.get("id") != record_id]
            if len(kept) == len(items):
                return False
            self.write(kept)
        return True
