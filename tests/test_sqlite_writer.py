"""The shared per-process write connection must end the WAL teardown churn.

Every write used to open a connection, write and close it; the last close
tore the WAL down and concurrent openers got an immediate ``database is
locked`` (4,781 in 24 h on the dashboard). These tests pin the properties
that stop that: one connection per process per file, a WAL that survives
writes, retries on a genuine collision, and correctness under threads.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

import sqlite_writer
from sqlite_writer import run_write


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=0.2)
    con.execute("PRAGMA busy_timeout=200")
    con.execute("PRAGMA journal_mode=WAL")
    return con


@pytest.fixture
def db(tmp_path: Path):
    path = tmp_path / "t.db"
    with sqlite3.connect(str(path)) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("CREATE TABLE t (k TEXT PRIMARY KEY, v INTEGER)")
    yield path
    sqlite_writer.close_all()


def _seen(path: Path, con_ids: list[int]):
    def fn(con: sqlite3.Connection) -> None:
        con_ids.append(id(con))
        con.execute("INSERT OR REPLACE INTO t VALUES ('a', 1)")
    return fn


def test_one_connection_is_reused_within_a_process(db: Path):
    ids: list[int] = []
    for _ in range(5):
        run_write(db, _connect, _seen(db, ids))
    assert len(set(ids)) == 1


def test_wal_file_survives_writes(db: Path):
    """With open-write-close, the last close deleted ``-wal``; the persistent
    connection keeps it in place so no opener has to rebuild the wal-index."""
    run_write(db, _connect, lambda con: con.execute("INSERT OR REPLACE INTO t VALUES ('w', 1)"))
    assert (db.parent / "t.db-wal").exists()
    run_write(db, _connect, lambda con: con.execute("INSERT OR REPLACE INTO t VALUES ('w', 2)"))
    assert (db.parent / "t.db-wal").exists()


def test_transaction_commits_and_rolls_back(db: Path):
    run_write(db, _connect, lambda con: con.execute("INSERT INTO t VALUES ('c', 1)"))
    with pytest.raises(sqlite3.IntegrityError):
        def bad(con: sqlite3.Connection) -> None:
            con.execute("INSERT INTO t VALUES ('r', 1)")
            con.execute("INSERT INTO t VALUES ('c', 1)")  # PK collision -> rollback
        run_write(db, _connect, bad)
    with sqlite3.connect(str(db)) as con:
        assert [r[0] for r in con.execute("SELECT k FROM t ORDER BY k")] == ["c"]


def test_busy_collision_is_retried_not_surfaced(db: Path, monkeypatch):
    """Another process holds the write lock for longer than busy_timeout; the
    writer must back off and succeed instead of raising to the request."""
    monkeypatch.setattr(sqlite_writer, "BUSY_BACKOFF_S", (0.1, 0.2, 0.4))
    other = sqlite3.connect(str(db), timeout=0.05, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")           # hold the write lock ...
    other.execute("INSERT INTO t VALUES ('held', 1)")

    def release_later():
        time.sleep(0.35)                        # ... past our 0.2 s busy_timeout
        other.commit()
    threading.Thread(target=release_later, daemon=True).start()

    run_write(db, _connect, lambda con: con.execute("INSERT INTO t VALUES ('mine', 1)"))
    with sqlite3.connect(str(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_busy_that_never_clears_still_raises(db: Path, monkeypatch):
    monkeypatch.setattr(sqlite_writer, "BUSY_BACKOFF_S", (0.05,))
    other = sqlite3.connect(str(db), timeout=0.05, check_same_thread=False)
    other.execute("BEGIN IMMEDIATE")
    other.execute("INSERT INTO t VALUES ('held', 1)")
    try:
        with pytest.raises(sqlite3.OperationalError):
            run_write(db, _connect, lambda con: con.execute("INSERT INTO t VALUES ('mine', 1)"))
    finally:
        other.rollback()


def test_threads_serialise_on_the_shared_connection(db: Path):
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            for j in range(40):
                run_write(db, _connect,
                          lambda con, i=i, j=j: con.execute("INSERT OR REPLACE INTO t VALUES (?, ?)", (f"{i}-{j}", j)))
        except BaseException as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads: th.start()
    for th in threads: th.join()
    assert errors == []
    with sqlite3.connect(str(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 8 * 40


def test_closed_connection_is_reopened_once(db: Path):
    run_write(db, _connect, lambda con: con.execute("INSERT INTO t VALUES ('a', 1)"))
    sqlite_writer.close_all()                    # someone closed it underneath us
    run_write(db, _connect, lambda con: con.execute("INSERT INTO t VALUES ('b', 1)"))
    with sqlite3.connect(str(db)) as con:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone()[0] == 2


def test_integrity_error_does_not_drop_the_connection(db: Path):
    ids: list[int] = []
    def first(con):
        ids.append(id(con)); con.execute("INSERT INTO t VALUES ('dup', 1)")
    run_write(db, _connect, first)
    with pytest.raises(sqlite3.IntegrityError):
        def dup(con):
            ids.append(id(con)); con.execute("INSERT INTO t VALUES ('dup', 2)")
        run_write(db, _connect, dup)
    run_write(db, _connect, lambda con: ids.append(id(con)))
    assert len(set(ids)) == 1        # same connection throughout, no reopen
    assert len(ids) == 3             # the failing body ran exactly once
