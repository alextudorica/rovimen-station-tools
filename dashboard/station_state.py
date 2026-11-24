"""station_state.py — per-station push sequence guard and media pointers.

The reversed-HTTP push data plane (see ``docs/reversed_http_push_design.md``)
has stations POST their own status/vitals/detections/media pointers to the VPS
ingest API. Status and vitals live in the :class:`StationCache` the dashboard
reads (Redis-backed in production, so they survive a restart). This module
carries the per-station ``last_seq`` used for envelope idempotency (§2.3): a
body whose ``seq`` is ``<= last_seq`` for that station is a replay and is
dropped. It also stores the pushed media pointers.

Two tables, kept deliberately separate from ``detections`` (which the ingest
handler writes through :func:`detection_db.upsert_detections` unchanged):

    station_state(host_key PK, last_seq, last_seen_at, last_status, last_vitals)
    media_pointers(host_key, cam, date, kind, filename PK-tuple, ...)

Storage is the same SQLite file as the detection index by default so a single
WAL connection pool serves both, but the path is overridable for tests.

Reading the SQLite ``station_state`` table directly on the VPS is misleading and
has cost debugging time more than once: every row there is frozen at
2026-07-23T13:32Z, the moment the dashboard restarted after #819 moved the seq
guard to Redis. Production sets ``ROVIMEN_REDIS_URL``, so accept_seq /
advance_seq short-circuit to Redis and the table is never written again, by
design. Its ``last_status``/``last_vitals``/``last_seen_at`` columns are
leftovers of a last-value mirror that nothing read and that is no longer
written; the live values are the status/vitals hashes the overview reads
through StationCache.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import threading
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from cache_backend import RedisStationStateBackend

import sqlite_writer

logger = logging.getLogger(__name__)

# Every heartbeat/vitals POST used to make TWO writes to the single-writer
# SQLite DB: the seq/replay check-and-bump AND a last-value mirror UPDATE (since
# removed: nothing read it). Across the fleet that was ~50+ writes/min hitting
# one write lock, which thread-starves the worker and — when a memory stall
# freezes a lock-holder mid transaction past the 30 s busy_timeout — produces
# the "database is locked" storm (docs/reversed_http_push_design.md §9). When
# ``ROVIMEN_REDIS_URL`` is set the per-push seq guard is an atomic Redis
# check-and-bump instead (see accept_seq), so the Redis path touches the shared
# SQLite DB on ZERO of the hot status/vitals pushes. When it is unset the seq
# stays in SQLite, exactly as before.
#
# Media pointers stay in SQLite regardless: they are low-frequency (300s/900s
# cadence) and carry no single-writer contention worth moving.
_KIND_SEQ = "seq"

_seq_backend: RedisStationStateBackend | None = None
_seq_backend_ready = False
_seq_backend_lock = threading.Lock()


def _get_seq_backend() -> RedisStationStateBackend | None:
    """Return the shared Redis seq backend, or ``None`` for the SQLite path.

    Built once, lazily, from ``ROVIMEN_REDIS_URL``. ``None`` means "keep the
    historical SQLite seq path". Safe to call from many threads.
    """
    global _seq_backend, _seq_backend_ready
    if _seq_backend_ready:
        return _seq_backend
    with _seq_backend_lock:
        if not _seq_backend_ready:
            from cache_backend import make_seq_backend

            _seq_backend = make_seq_backend()
            _seq_backend_ready = True
    return _seq_backend


def reset_seq_backend_for_tests() -> None:
    """Drop the memoised seq backend so a test can re-select via env. Test-only."""
    global _seq_backend, _seq_backend_ready
    with _seq_backend_lock:
        _seq_backend = None
        _seq_backend_ready = False

# Defaults to the same DB as the detection index — the ingest handler already
# writes detections there, so co-locating station_state keeps everything the
# dashboard reads on one connection pool.
DB_PATH = Path(
    os.environ.get(
        "ROVIMEN_STATION_STATE_DB",
        os.environ.get("ROVIMEN_DETECTIONS_DB", "/opt/rovimen/detections.db"),
    )
)

# Aligned with detection_db._BUSY_TIMEOUT_MS: both modules share ONE write
# connection per process (sqlite_writer keys it on the database path), so the
# busy_timeout a worker ends up with is whichever module opened it first.
# 30 s here meant a contended write could hold an ingest request for
# 5 x 30 s + back-off, past the 120 s proxy budget; 10 s keeps the worst case
# inside it and lets the writer that holds the lock finish instead.
_BUSY_TIMEOUT_MS = 10_000
_POINTER_CHUNK_ROWS = 500

_SCHEMA = """
CREATE TABLE IF NOT EXISTS station_state (
    host_key      TEXT PRIMARY KEY,
    last_seq      INTEGER NOT NULL DEFAULT 0,
    last_seen_at  TEXT,
    last_status   TEXT,
    last_vitals   TEXT,
    updated_at    REAL
);

CREATE TABLE IF NOT EXISTS media_pointers (
    host_key      TEXT NOT NULL,
    cam           TEXT NOT NULL,
    date          TEXT NOT NULL,
    kind          TEXT NOT NULL,
    filename      TEXT NOT NULL,
    extra         TEXT,
    updated_at    REAL,
    PRIMARY KEY (cam, date, kind, filename)
);

CREATE INDEX IF NOT EXISTS idx_media_host ON media_pointers(host_key);
CREATE INDEX IF NOT EXISTS idx_media_date ON media_pointers(date);
"""


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(
        str(path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000
    )
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if not read_only:
        # journal_mode is persisted in the file; re-issuing the WAL pragma on
        # every connection takes a lock transition for nothing.
        mode = (con.execute("PRAGMA journal_mode").fetchone() or [""])[0]
        if str(mode).lower() != "wal":
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    return con


def _run_write(path: Path, fn):
    """One transaction on this process's shared write connection
    (see ``sqlite_writer``)."""
    return sqlite_writer.run_write(path, _connect, fn)


# ensure_schema() used to open, DDL and close a connection on EVERY write --
# a second connection churn per request on top of the write itself, and each
# close of the last connection tore the WAL down. The schema is idempotent,
# so once a process has applied it to a file, it is done.
_schema_ready: set[tuple[int, str]] = set()


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the station_state DB, ensuring the schema exists."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    con.executescript(_SCHEMA)
    con.commit()
    return con


def ensure_schema(path: Path = DB_PATH) -> None:
    """Create the tables if missing. Safe to call repeatedly; the DDL runs
    once per process per file."""
    key = (os.getpid(), str(path))
    if key in _schema_ready and path.exists():
        return
    _schema_ready.add(key)
    with closing(open_db(path)):
        pass


def _now() -> float:
    import time

    return time.time()


def get_last_seq(host_key: str, path: Path = DB_PATH) -> int:
    """Return the highest accepted ``seq`` for ``host_key`` (0 if unseen)."""
    backend = _get_seq_backend()
    if backend is not None:
        return backend.seq_get(host_key, _KIND_SEQ)
    ensure_schema(path)
    with closing(_connect(path, read_only=True)) as con:
        row = con.execute(
            "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
    return int(row[0]) if row and row[0] is not None else 0


def is_replay(host_key: str, seq: int, path: Path = DB_PATH) -> bool:
    """True if ``seq`` is a replay/duplicate for ``host_key`` (``<= last_seq``).

    A fresh station (no prior state) has ``last_seq == 0``; any positive seq is
    accepted. Envelope ordering is by ``seq``, never wall-clock (§7 clock skew).

    NOTE: this is a *read-only* probe. It races against a concurrent bump and is
    kept for callers/tests that only want to peek. Ingest handlers must instead
    use :func:`accept_seq`, whose check-and-bump is atomic.
    """
    return seq <= get_last_seq(host_key, path)


def accept_seq(host_key: str, seq: int, path: Path = DB_PATH) -> bool:
    """Atomically decide + record whether ``seq`` advances ``host_key``.

    Returns ``True`` iff ``seq`` is strictly greater than the last accepted seq
    (the push is accepted and the stored seq is advanced to ``seq``), ``False``
    when it is a replay/older seq (the caller must drop). This collapses the old
    ``is_replay`` check and the subsequent bump into ONE atomic operation so two
    concurrent pushes with the same seq can never both be accepted.

    * Redis path (``ROVIMEN_REDIS_URL`` set): a single atomic check-and-bump,
      no SQLite write at all — this is the whole point, removing the per-push
      seq write from the single-writer SQLite DB. A Redis error accepts the
      push (see ``RedisStationStateBackend.seq_bump``).
    * SQLite path (unset): the check-and-bump runs inside one transaction on a
      ``BEGIN IMMEDIATE`` write lock, which serialises concurrent writers, so it
      is atomic there too. Behaviour is identical to the historical
      ``is_replay`` + ``_bump_seq`` sequence.
    """
    backend = _get_seq_backend()
    if backend is not None:
        return backend.seq_bump(host_key, _KIND_SEQ, seq)
    ensure_schema(path)

    def _write(con: sqlite3.Connection) -> bool:
        # BEGIN IMMEDIATE takes the write lock up front so the read below
        # can't be undercut by a concurrent writer between check and bump.
        con.execute("BEGIN IMMEDIATE")
        row = con.execute(
            "SELECT last_seq FROM station_state WHERE host_key=?", (host_key,)
        ).fetchone()
        current = int(row[0]) if row and row[0] is not None else 0
        if seq <= current:
            return False
        _bump_seq(con, host_key, seq)
        return True

    return _run_write(path, _write)


def _bump_seq(con: sqlite3.Connection, host_key: str, seq: int) -> None:
    """Advance the SQLite ``last_seq`` to ``max(current, seq)`` inside an open
    txn, creating the row if missing.

    Only reached on the SQLite seq path (``ROVIMEN_REDIS_URL`` unset): accept_seq
    and advance_seq both short-circuit to Redis when the seq backend is active,
    so the shared SQLite DB is never written on the hot push path in that case.
    """
    con.execute(
        """
        INSERT INTO station_state (host_key, last_seq, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(host_key) DO UPDATE SET
            last_seq = MAX(station_state.last_seq, excluded.last_seq),
            updated_at = excluded.updated_at
        """,
        (host_key, seq, _now()),
    )


def advance_seq(host_key: str, seq: int, path: Path = DB_PATH) -> None:
    """Advance seq only (used by detections/media ingest, which carry their own
    durable stores — the detection PK and the media_pointers PK).

    On the Redis seq path this bumps the shared Redis seq; on the SQLite path it
    bumps ``last_seq`` exactly as before. Kept for callers that have already
    decided to accept and only need the monotonic advance (as opposed to the
    accept-or-drop decision, which is :func:`accept_seq`)."""
    backend = _get_seq_backend()
    if backend is not None:
        backend.seq_bump(host_key, _KIND_SEQ, seq)
        return
    ensure_schema(path)
    _run_write(path, lambda con: _bump_seq(con, host_key, seq))


def upsert_media_pointers(
    host_key: str,
    pointers: list[dict[str, Any]],
    path: Path = DB_PATH,
) -> int:
    """Upsert media pointers, deduped on ``(cam, date, kind, filename)`` (§2.3).

    Each pointer dict must carry ``cam``, ``date``, ``kind``, ``filename``;
    any other keys are preserved verbatim in an ``extra`` JSON blob so the
    dashboard can surface e.g. ``locked`` / ``meteor_time`` / ``night_stack``.
    Returns the number of rows written.
    """
    if not pointers:
        return 0
    ensure_schema(path)

    def _write(con: sqlite3.Connection, batch: list[dict[str, Any]]) -> int:
        n = 0
        for p in batch:
            cam = p.get("cam")
            date = p.get("date")
            kind = p.get("kind")
            filename = p.get("filename")
            if not (cam and date and kind and filename):
                continue
            extra = {
                k: v
                for k, v in p.items()
                if k not in ("cam", "date", "kind", "filename")
            }
            # No-op for an unchanged pointer: stations re-send the whole clip
            # window every 900 s, so most rows already match; INSERT OR
            # REPLACE rewrote them all (and their two indexes) regardless.
            # ``updated_at`` therefore means "last time this pointer CHANGED",
            # not "last time the station mentioned it" — same contract as
            # night_sky.updated. Nothing reads it as a liveness signal.
            con.execute(
                """INSERT INTO media_pointers
                   (host_key, cam, date, kind, filename, extra, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(cam, date, kind, filename) DO UPDATE SET
                     host_key = excluded.host_key,
                     extra = excluded.extra,
                     updated_at = excluded.updated_at
                   WHERE media_pointers.host_key IS NOT excluded.host_key
                      OR media_pointers.extra IS NOT excluded.extra""",
                (
                    host_key,
                    cam,
                    date,
                    kind,
                    filename,
                    json.dumps(extra, default=str) if extra else None,
                    _now(),
                ),
            )
            n += 1
        return n

    # Bounded transactions, like detection_db._run_write_chunked: one media
    # push can carry up to 3 x 5000 pointers, and a single transaction of that
    # size held the shared write lock past every other writer's busy_timeout.
    total = 0
    for i, start in enumerate(range(0, len(pointers), _POINTER_CHUNK_ROWS)):
        if i:
            time.sleep(0.02)
        chunk = pointers[start:start + _POINTER_CHUNK_ROWS]
        total += _run_write(path, lambda con, chunk=chunk: _write(con, chunk))
    return total


def get_media_pointers(
    host_key: str | None = None,
    date: str | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return media pointers, optionally filtered by host_key and/or date."""
    ensure_schema(path)
    clauses: list[str] = []
    params: list[Any] = []
    if host_key:
        clauses.append("host_key=?")
        params.append(host_key)
    if date:
        clauses.append("date=?")
        params.append(date)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT * FROM media_pointers{where} ORDER BY date DESC, cam", params
        ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        if d.get("extra"):
            try:
                d["extra"] = json.loads(d["extra"])
            except (ValueError, TypeError):
                d["extra"] = None
        out.append(d)
    return out
