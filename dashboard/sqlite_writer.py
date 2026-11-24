"""One long-lived SQLite write connection per (process, database), with retry.

Why this exists
---------------
Every write path used to open a fresh connection, write, and close it. With
three gunicorn workers each doing that for ~14 ingest POSTs a second, the
process holding the *last* open connection tore the WAL down on close
(checkpoint + delete ``-wal``) and the next opener had to rebuild the wal-index.
SQLite answers concurrent openers caught in that window with an immediate
``SQLITE_BUSY`` -- the busy handler is not consulted during WAL recovery -- so
``busy_timeout`` never got a chance: 4,781 ``database is locked`` failures in
24 h on the dashboard, every hour of the day, ~0.5 % of ingest POSTs.

Keeping one write connection open per worker removes the teardown (there is
always a connection holding the WAL open), serialises that worker's writes on
a lock the way SQLite would anyway, and retries the rare genuine collision
between workers instead of surfacing it as a 500 that the station retries.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
import time
from pathlib import Path
from typing import Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Back-off between attempts on "database is locked" / "database is busy".
# Total worst case ~1.75 s on top of the connection's own busy_timeout.
BUSY_BACKOFF_S: tuple[float, ...] = (0.05, 0.2, 0.5, 1.0)

_registry_lock = threading.Lock()
_conns: dict[tuple[int, str], sqlite3.Connection] = {}
_locks: dict[str, threading.Lock] = {}


def _lock_for(key: str) -> threading.Lock:
    with _registry_lock:
        lock = _locks.get(key)
        if lock is None:
            lock = _locks[key] = threading.Lock()
        return lock


def _is_dead_handle(exc: sqlite3.Error) -> bool:
    if isinstance(exc, sqlite3.ProgrammingError):
        return True  # "Cannot operate on a closed database."
    msg = str(exc).lower()
    return any(s in msg for s in ("not a database", "disk i/o", "malformed", "closed database"))


def _is_busy(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


def _get_connection(path: Path, connect: Callable[[Path], sqlite3.Connection]) -> sqlite3.Connection:
    # Keyed by pid so a connection inherited across a fork is never reused:
    # gunicorn forks workers after import, and SQLite connections must not
    # cross that boundary.
    key = (os.getpid(), str(path))
    con = _conns.get(key)
    if con is None:
        con = connect(path)
        _conns[key] = con
    return con


def _drop_connection(path: Path) -> None:
    key = (os.getpid(), str(path))
    con = _conns.pop(key, None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass


def run_write(
    path: Path,
    connect: Callable[[Path], sqlite3.Connection],
    fn: Callable[[sqlite3.Connection], T],
) -> T:
    """Run ``fn(con)`` inside one transaction on the shared write connection.

    ``connect`` opens a new connection with the caller's pragmas when this
    process has none yet. The transaction commits when ``fn`` returns and
    rolls back when it raises. A busy/locked error retries with back-off; a
    dead connection (closed underneath us, or the file replaced) is dropped
    and reopened once.
    """
    lock = _lock_for(str(path))
    with lock:
        reopened = False
        attempt = 0
        while True:
            con = _get_connection(path, connect)
            try:
                with con:
                    return fn(con)
            except sqlite3.OperationalError as exc:
                if _is_busy(exc) and attempt < len(BUSY_BACKOFF_S):
                    delay = BUSY_BACKOFF_S[attempt]
                    attempt += 1
                    logger.info("sqlite busy on %s, retry %d in %.2fs", path.name, attempt, delay)
                    time.sleep(delay)
                    continue
                raise
            except sqlite3.IntegrityError:
                raise  # the caller's data, not the connection -- never retried
            except (sqlite3.ProgrammingError, sqlite3.DatabaseError) as exc:
                # Only a dead handle justifies a reopen: the connection was
                # closed underneath us (ProgrammingError), or the file was
                # replaced/corrupted. Anything else is the caller's problem.
                if reopened or not _is_dead_handle(exc):
                    raise
                logger.warning("sqlite writer on %s unusable (%s) -- reopening", path.name, exc)
                _drop_connection(path)
                reopened = True


def close_all() -> None:
    """Close this process's cached write connections (tests, shutdown)."""
    with _registry_lock:
        keys = [k for k in _conns if k[0] == os.getpid()]
    for key in keys:
        con = _conns.pop(key, None)
        if con is not None:
            try:
                con.close()
            except Exception:
                pass
