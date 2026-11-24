"""Pushed per-night chunk listings (#1250).

The station pusher (``rovimen_pusher.push_chunk_listings``) POSTs each
camera's newest nights as a compact listing to
``/api/ingest/v1/<station>/chunks``; this module stores them next to
``station_state`` and expands them back into the exact chunk dicts the
station's ``/api/chunks/<cam>/<date>`` returns (``station_collect.list_chunks``).

That lets tonight's views (the videodb chunk grid, the all-footage grid, the
night pickers, tonight's locked detections) read VPS-local state instead of
fanning out to every station's port 7779 on each page load.

Compact wire format (one camera-night per POST)::

    {
      "cam": "RO000A", "date": "20261101", "morning_done": false,
      "chunks": [["20261101_183000", 12.3], ["20261101_183020", 12.1, "18:30:20"]],
      "locks": {"20261101_190020": ["detection", "2026-11-01 19:00:31.2", 11.2]},
      "reencoded": ["20261101_183000"]
    }

A chunk entry is ``[stamp, size_mb]`` with ``stamp`` the filename between
``<cam>_`` and ``_color.mkv``; a third element carries the chunk's UTC start
only when it differs from the name (legacy local-time chunk names).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import station_state

logger = logging.getLogger(__name__)

# Listings older than this are dropped on write; the archive answers for them.
RETENTION_DAYS = 10

_STAMP_RE = re.compile(r"^\d{8}_\d{6}$")
_DATE_RE = re.compile(r"^\d{8}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunk_listings (
    host_key      TEXT NOT NULL,
    cam           TEXT NOT NULL,
    date          TEXT NOT NULL,
    morning_done  INTEGER NOT NULL DEFAULT 0,
    listing       TEXT NOT NULL,
    received_at   REAL NOT NULL,
    PRIMARY KEY (cam, date)
);
CREATE INDEX IF NOT EXISTS idx_chunk_listings_host ON chunk_listings(host_key);
"""

_schema_ready: set[str] = set()


def _ensure_schema(path: Path) -> None:
    key = str(path)
    if key in _schema_ready:
        return

    def _ddl(con: sqlite3.Connection) -> None:
        con.executescript(_SCHEMA)

    station_state._run_write(path, _ddl)
    _schema_ready.add(key)


def store(host_key: str, listing: dict[str, Any], path: Path | None = None) -> None:
    """Replace the stored listing for ``(listing.cam, listing.date)``."""
    path = path or station_state.DB_PATH
    _ensure_schema(path)
    cam = listing["cam"]
    date = listing["date"]
    body = json.dumps(
        {
            "chunks": listing.get("chunks") or [],
            "locks": listing.get("locks") or {},
            "reencoded": listing.get("reencoded") or [],
        },
        separators=(",", ":"),
    )
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).strftime("%Y%m%d")

    def _write(con: sqlite3.Connection) -> None:
        con.execute(
            """INSERT INTO chunk_listings
               (host_key, cam, date, morning_done, listing, received_at)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(cam, date) DO UPDATE SET
                 host_key = excluded.host_key,
                 morning_done = excluded.morning_done,
                 listing = excluded.listing,
                 received_at = excluded.received_at""",
            (host_key, cam, date, int(bool(listing.get("morning_done"))), body, time.time()),
        )
        con.execute("DELETE FROM chunk_listings WHERE date < ?", (cutoff,))

    station_state._run_write(path, _write)


def _read_row(cam: str, date: str, path: Path) -> sqlite3.Row | None:
    if not path.exists():
        return None
    try:
        with closing(station_state._connect(path, read_only=True)) as con:
            con.row_factory = sqlite3.Row
            return con.execute(
                "SELECT * FROM chunk_listings WHERE cam = ? AND date = ?",
                (cam, date),
            ).fetchone()
    except sqlite3.Error:
        # Table not created yet (no station has pushed) or DB unreadable.
        return None


def _utc_time(stamp: str) -> str:
    t = stamp[9:15]
    return f"{t[:2]}:{t[2:4]}:{t[4:6]}"


def expand(cam: str, listing: dict[str, Any]) -> list[dict[str, Any]]:
    """Compact listing -> the station's ``/api/chunks`` chunk dicts."""
    locks = listing.get("locks") or {}
    reencoded = set(listing.get("reencoded") or [])
    out: list[dict[str, Any]] = []
    for entry in listing.get("chunks") or []:
        if not isinstance(entry, (list, tuple)) or not entry:
            continue
        stamp = entry[0]
        if not isinstance(stamp, str) or not _STAMP_RE.match(stamp):
            continue
        size_mb = entry[1] if len(entry) > 1 else None
        time_str = entry[2] if len(entry) > 2 and entry[2] else _utc_time(stamp)
        lock = locks.get(stamp)
        lock_type = meteor_time = det_off = None
        if isinstance(lock, (list, tuple)) and lock:
            lock_type = lock[0]
            meteor_time = lock[1] if len(lock) > 1 else None
            det_off = lock[2] if len(lock) > 2 else None
        out.append({
            "filename": f"{cam}_{stamp}_color.mkv",
            "time": time_str,
            "stack": f"{cam}_{stamp}_stack.webp",
            "size_mb": size_mb,
            "locked": lock_type is not None,
            "lock_type": lock_type,
            "detection_offset_s": det_off if lock_type == "detection" else None,
            "meteor_time": meteor_time,
            "reencoded": stamp in reencoded,
            "source": "pushed",
        })
    return out


def get(cam: str, date: str, path: Path | None = None) -> dict[str, Any] | None:
    """``{"morning_done", "chunks", "received_at", "host_key"}`` for one
    camera-night, or None when the station never pushed that night."""
    path = path or station_state.DB_PATH
    row = _read_row(cam, date, path)
    if row is None:
        return None
    try:
        listing = json.loads(row["listing"])
    except (TypeError, ValueError):
        return None
    return {
        "host_key": row["host_key"],
        "morning_done": bool(row["morning_done"]),
        "chunks": expand(cam, listing),
        "received_at": row["received_at"],
    }


def nights(cams: list[str] | set[str] | None = None, path: Path | None = None) -> list[str]:
    """Distinct pushed night dates (newest first), optionally for ``cams``."""
    path = path or station_state.DB_PATH
    if not path.exists():
        return []
    sql = "SELECT DISTINCT date FROM chunk_listings"
    params: list[Any] = []
    if cams is not None:
        cams = list(cams)
        if not cams:
            return []
        sql += f" WHERE cam IN ({','.join('?' * len(cams))})"
        params.extend(cams)
    try:
        with closing(station_state._connect(path, read_only=True)) as con:
            rows = con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    return sorted({r[0] for r in rows if r[0] and _DATE_RE.match(r[0])}, reverse=True)


def media_pointer_nights(cams: list[str] | set[str] | None = None,
                         path: Path | None = None) -> list[str]:
    """Distinct night dates the stations announced media pointers for
    (locked clips, timelapses, night stacks), newest first."""
    path = path or station_state.DB_PATH
    if not path.exists():
        return []
    sql = "SELECT DISTINCT date FROM media_pointers"
    params: list[Any] = []
    if cams is not None:
        cams = list(cams)
        if not cams:
            return []
        sql += f" WHERE cam IN ({','.join('?' * len(cams))})"
        params.extend(cams)
    try:
        with closing(station_state._connect(path, read_only=True)) as con:
            rows = con.execute(sql, params).fetchall()
    except sqlite3.Error:
        return []
    return sorted({r[0] for r in rows if r[0] and _DATE_RE.match(r[0])}, reverse=True)


def pushed_locks(date: str, path: Path | None = None) -> dict[str, list[dict[str, Any]]]:
    """Locked chunks for night ``date`` the stations announced, per camera:
    ``{cam: [{"filename", "meteor_time", "lock_type"}, ...]}``.

    Two pushed sources, deduped on filename: the chunk listings (this
    module) and the locked-clip pointers every pusher sends with its media
    push (``media_pointers`` kind ``clip``), which also covers stations still
    running a bundle without the chunk-listing push."""
    path = path or station_state.DB_PATH
    out: dict[str, dict[str, dict[str, Any]]] = {}
    if not path.exists():
        return {}
    try:
        with closing(station_state._connect(path, read_only=True)) as con:
            con.row_factory = sqlite3.Row
            try:
                rows = con.execute(
                    "SELECT cam, listing FROM chunk_listings WHERE date = ?", (date,)
                ).fetchall()
            except sqlite3.Error:
                rows = []
            for r in rows:
                try:
                    locks = (json.loads(r["listing"]) or {}).get("locks") or {}
                except (TypeError, ValueError):
                    continue
                for stamp, lock in locks.items():
                    if not isinstance(lock, (list, tuple)) or not lock:
                        continue
                    fname = f"{r['cam']}_{stamp}_color.mkv"
                    out.setdefault(r["cam"], {})[fname] = {
                        "filename": fname,
                        "lock_type": lock[0],
                        "meteor_time": lock[1] if len(lock) > 1 else None,
                    }
            try:
                ptrs = con.execute(
                    "SELECT cam, filename, extra FROM media_pointers "
                    "WHERE date = ? AND kind = 'clip'", (date,)
                ).fetchall()
            except sqlite3.Error:
                ptrs = []
            for p in ptrs:
                try:
                    extra = json.loads(p["extra"]) if p["extra"] else {}
                except (TypeError, ValueError):
                    extra = {}
                cam_locks = out.setdefault(p["cam"], {})
                if p["filename"] in cam_locks:
                    continue
                cam_locks[p["filename"]] = {
                    "filename": p["filename"],
                    "lock_type": extra.get("lock_type") or "detection",
                    "meteor_time": extra.get("meteor_time"),
                }
    except sqlite3.Error:
        return {}
    return {cam: list(v.values()) for cam, v in out.items()}
