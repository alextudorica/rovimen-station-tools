"""SQLite hand-off between the fireball ingest route and the alert daemon.

The ingest route (``ingest_api``, inside gunicorn) appends the events station
watchers send (``rovimen-scripts/fireball_watch.py``); the alert daemon
(``fireball_alert.py``, its own systemd unit) reads rows past the last id it
has seen. Platepars a watcher sends are also kept per camera in their own
table, which the event pruning never touches, so the daemon starts with the
newest platepar of every camera.

Path: ``ROVIMEN_FIREBALL_DB``, default ``/opt/rovimen/cache/fireball_events.db``
(``cache/`` survives deploys: the rsync excludes it).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("ROVIMEN_FIREBALL_DB", "/opt/rovimen/cache/fireball_events.db"))
KEEP_S = 3 * 86400

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    received REAL NOT NULL,
    station TEXT NOT NULL,
    kind TEXT NOT NULL,
    cam TEXT,
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS events_received ON events(received);
CREATE TABLE IF NOT EXISTS platepars (
    cam TEXT PRIMARY KEY,
    station TEXT NOT NULL,
    mtime REAL NOT NULL,
    received REAL NOT NULL,
    body TEXT NOT NULL
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p), timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(_SCHEMA)
    return conn


def insert(station: str, events: list[dict[str, Any]], *, path: Path | None = None,
           now: float | None = None) -> int:
    """Append a batch; platepar events also replace that camera's stored platepar."""
    now = time.time() if now is None else now
    conn = connect(path)
    try:
        with conn:
            for ev in events:
                body = json.dumps(ev, separators=(",", ":"))
                kind = str(ev.get("kind"))
                cam = ev.get("cam")
                conn.execute("INSERT INTO events(received, station, kind, cam, body) VALUES (?,?,?,?,?)",
                             (now, station, kind, cam, body))
                if kind == "platepar" and cam:
                    conn.execute(
                        "INSERT INTO platepars(cam, station, mtime, received, body) VALUES (?,?,?,?,?) "
                        "ON CONFLICT(cam) DO UPDATE SET station=excluded.station, mtime=excluded.mtime, "
                        "received=excluded.received, body=excluded.body WHERE excluded.mtime >= platepars.mtime",
                        (cam, station, float(ev.get("mtime") or 0), now, body))
    finally:
        conn.close()
    return len(events)


def read_since(last_id: int, *, path: Path | None = None,
               limit: int = 5000) -> list[tuple[int, float, str, dict[str, Any]]]:
    conn = connect(path)
    try:
        rows = conn.execute("SELECT id, received, station, body FROM events WHERE id > ? ORDER BY id LIMIT ?",
                            (last_id, limit)).fetchall()
    finally:
        conn.close()
    out = []
    for rid, rec, st, body in rows:
        try:
            out.append((rid, rec, st, json.loads(body)))
        except ValueError:
            continue
    return out


def last_id(*, path: Path | None = None) -> int:
    conn = connect(path)
    try:
        row = conn.execute("SELECT MAX(id) FROM events").fetchone()
    finally:
        conn.close()
    return int(row[0] or 0)


def platepars(*, path: Path | None = None) -> dict[str, tuple[str, dict[str, Any]]]:
    """cam -> (station, platepar event) for every camera that sent one."""
    conn = connect(path)
    try:
        rows = conn.execute("SELECT cam, station, body FROM platepars").fetchall()
    finally:
        conn.close()
    out = {}
    for cam, st, body in rows:
        try:
            out[cam] = (st, json.loads(body))
        except ValueError:
            continue
    return out


def prune(*, path: Path | None = None, now: float | None = None) -> int:
    now = time.time() if now is None else now
    conn = connect(path)
    try:
        with conn:
            cur = conn.execute("DELETE FROM events WHERE received < ?", (now - KEEP_S,))
            return cur.rowcount
    finally:
        conn.close()
