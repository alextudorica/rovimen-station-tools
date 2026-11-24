"""Fail-safe read and ownership-preserving write for the YAML key stores.

Shared by ``station_keys.py`` (station ingest keys) and ``api_keys.py``
(public-API keys). Both files are read by the dashboard service user
(``rovimen`` on the VPS) and written by an operator CLI that usually runs as
root.

The rules come from the 2026-11-20 outage (21:29:02-21:38:31 UTC, 12,965
ingest POSTs answered 401, 14 stations marked offline): ``mint_station_key.py``
run as root replaced ``station_keys.yaml`` with a root-owned 0600 file, the
dashboard could no longer read it, and the loader turned the read error into
an empty key set.

1. :func:`read_yaml_strict` raises :class:`KeyFileUnreadable` (an OSError: the
   content may be intact) or :class:`KeyFileInvalid` (read, but not a valid
   key store) instead of returning "no keys". Writers refuse on both.
2. :class:`LastGood` serves the last good keys after an unreadable-file error
   for at most :data:`STALE_KEYS_WINDOW_S` from the first failed reload, then
   fails closed. An invalid file fails closed at once: the write tools cannot
   produce one, so it comes from a hand edit whose intent (possibly a
   revocation) is unknown.
3. :func:`write_preserving` writes a temp file in the same directory, fsyncs
   it, gives it the original owner and group and the original mode capped to
   ``(mode & 0o640) | 0o600`` (a new file takes the directory's owner and
   group, mode 0600), then renames it into place. It refuses when it cannot
   keep the owner.
"""

from __future__ import annotations

import logging
import os
import stat
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

import yaml

logger = logging.getLogger(__name__)

NEW_FILE_MODE = 0o600
# Never group-writable or world-readable after a write; owner always rw.
MODE_CAP = 0o640
ERROR_LOG_INTERVAL_S = 60.0
# How long last good keys are served while the file is unreadable, from the
# first failed reload. Bridges an operator mistake like the 9.5-minute outage
# of 2026-11-20; after it the store fails closed.
STALE_KEYS_WINDOW_S = 15 * 60

_log_lock = threading.Lock()
_last_logged: dict[str, float] = {}

K = TypeVar("K")


class KeyFileError(RuntimeError):
    """The key file exists but could not be read, parsed or written safely."""


class KeyFileUnreadable(KeyFileError):
    """The file exists but reading it failed (permissions, I/O)."""


class KeyFileInvalid(KeyFileError):
    """The file was read but is not valid YAML or not a valid key store."""


def read_yaml_strict(path: Path) -> dict[str, Any]:
    """Return the YAML mapping in ``path``; ``{}`` only when the file is absent.

    Raises :class:`KeyFileUnreadable` or :class:`KeyFileInvalid`, so a caller
    can tell "no keys" from "keys I cannot see".
    """
    try:
        text = path.read_text()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise KeyFileUnreadable(f"cannot read {path}: {exc}") from exc
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise KeyFileInvalid(f"cannot parse {path}: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise KeyFileInvalid(f"cannot parse {path}: top level is {type(raw).__name__}, not a mapping")
    return raw


def _log_rate_limited(key: str, msg: str, *args: object) -> None:
    now = time.monotonic()
    with _log_lock:
        last = _last_logged.get(key)
        if last is not None and now - last < ERROR_LOG_INTERVAL_S:
            return
        _last_logged[key] = now
    logger.error(msg, *args)


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


class LastGood(Generic[K]):
    """Last successfully loaded key set per file, with a bounded stale window.

    ``clock`` is injectable for tests (wall-clock seconds).
    """

    def __init__(self, store: str, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self.clock = clock
        self._lock = threading.Lock()
        self._keys: dict[str, dict[str, K]] = {}
        self._first_failure: dict[str, float] = {}
        self._state: dict[str, dict[str, Any]] = {}

    def loaded(self, path: Path, keys: dict[str, K]) -> None:
        """Record a successful reload; clears any open stale window."""
        with self._lock:
            self._keys[str(path)] = keys
            self._first_failure.pop(str(path), None)
            self._state.pop(str(path), None)

    def failed(self, path: Path, exc: KeyFileError) -> dict[str, K]:
        """Keys to serve after a failed reload: last good ones inside the
        window for an unreadable file, else none (fail closed)."""
        now = self.clock()
        p = str(path)
        cause = type(exc.__cause__ or exc).__name__
        with self._lock:
            kept = self._keys.get(p)
            first = self._first_failure.setdefault(p, now)
            until = first + STALE_KEYS_WINDOW_S
            if isinstance(exc, KeyFileUnreadable) and kept is not None and now < until:
                serving: dict[str, K] = kept
                verdict = "serving"
            else:
                serving = {}
                verdict = "closed"
                if isinstance(exc, KeyFileInvalid):
                    # A hand edit of unknown intent: never fall back to keys
                    # from before it, not even after a later read error.
                    self._keys.pop(p, None)
            self._state[p] = {
                "path": p,
                "error": cause,
                "since_utc": _hhmm(first),
                "serving_stale_keys": verdict == "serving",
                "stale_until_utc": _hhmm(until) if verdict == "serving" else None,
            }
        if verdict == "serving":
            _log_rate_limited(
                f"{self.store}:{p}:serving",
                "%s: cannot read %s (%s: %s) since %s; serving the %d last good keys until %s",
                self.store, p, cause, exc, _hhmm(first), len(serving), _hhmm(until),
            )
        else:
            reason = (
                "file is invalid" if isinstance(exc, KeyFileInvalid)
                else "no earlier keys in memory" if kept is None
                else f"stale window ended {_hhmm(until)}"
            )
            _log_rate_limited(
                f"{self.store}:{p}:closed",
                "%s: cannot load %s (%s: %s) since %s; failing closed, no keys served (%s)",
                self.store, p, cause, exc, _hhmm(first), reason,
            )
        return serving

    def status(self) -> list[dict[str, Any]]:
        """Open failure states, for the admin health view."""
        with self._lock:
            return [dict(v) for v in self._state.values()]

    def clear(self) -> None:
        with self._lock:
            self._keys.clear()
            self._first_failure.clear()
            self._state.clear()


def _target_attrs(path: Path) -> tuple[int, int, int]:
    """Owner, group and permission bits the written file must carry.

    The mode is the existing one capped to ``(mode & 0o640) | 0o600``: the
    owner (the dashboard user) can always read and write it, and it is never
    group-writable or world-readable afterwards.
    """
    try:
        st = path.stat()
    except FileNotFoundError:
        dst = path.parent.stat()
        return dst.st_uid, dst.st_gid, NEW_FILE_MODE
    mode = stat.S_IMODE(st.st_mode)
    if mode & stat.S_IROTH:
        logger.warning("%s was world-readable (mode %04o); writing it as %04o",
                       path, mode, (mode & MODE_CAP) | NEW_FILE_MODE)
    return st.st_uid, st.st_gid, (mode & MODE_CAP) | NEW_FILE_MODE


def write_preserving(path: Path, payload: str) -> None:
    """Atomically replace ``path`` with ``payload``, keeping owner and group.

    The file must stay readable for its owner (for a new file, the
    directory's owner): on the VPS that is the dashboard user. Raises
    :class:`KeyFileError`, leaving ``path`` untouched, when the temp file
    cannot be given that owner.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    uid, gid, mode = _target_attrs(path)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), mode)
            st = os.fstat(fh.fileno())
            if (st.st_uid, st.st_gid) != (uid, gid):
                try:
                    os.fchown(fh.fileno(), uid, gid)
                except OSError as exc:
                    raise KeyFileError(
                        f"refusing to write {path}: cannot give the new file owner "
                        f"{uid}:{gid} (running as uid {os.geteuid()}): {exc}. "
                        "Run the tool as root or as the file's owner."
                    ) from exc
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass
