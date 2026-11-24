"""Detections a signed-in user hid as false positives (birds, satellites, ...).

A hide is a property of ONE detection, ``(cam, ff_file, meteor_no)`` as in
``detection_db``, shared by every user. It lives in its own SQLite file, never
in the ``detections`` table: that table is re-ingested from what the stations
push and can be rebuilt from the Storage Box, and a hide must outlive both.

Two tables: ``hidden`` holds the current state (one row per hidden detection),
``history`` is append-only (every hide and unhide, by whom, when, from which
page, with the reason). Hiding never deletes, unlocks or deprioritises any
clip, stack or archive file; it only filters what the browsing lists show.

Readers never query per row. :func:`snapshot` returns an in-memory index of
the whole ``hidden`` table, rebuilt when the database file changes (one
``stat`` per call). The file stays in rollback-journal mode, so every commit
rewrites the main file and bumps its mtime, which is what the three gunicorn
workers watch; a write in this process also drops the snapshot at once.

Matching: a listed item carries either the detection key (``ff_file`` plus
``meteor_no``, at top level or in its ``rms`` dict) or only its meteor time.
:meth:`HiddenSnapshot.match` tries the key first, then ``(cam, time to the
second)``, the same join the Detection tab already uses between clips and
RMS detections.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_DEFAULT_PATH = "/opt/rovimen/hidden_detections.db"

# Reasons a user may give. Anything else is refused by the API.
REASONS = ("bird", "satellite", "plane", "insect", "other")

# Pages a hide may say it came from (free text would let a client write
# anything into the history).
PAGES = ("detections", "events", "video", "multi", "highlights", "grid", "archive",
         "overview", "other")

_BUSY_TIMEOUT_S = 10.0

_SCHEMA = """
CREATE TABLE IF NOT EXISTS hidden (
    cam        TEXT NOT NULL,
    ff_file    TEXT NOT NULL,
    meteor_no  INTEGER NOT NULL,
    date       TEXT,
    time_utc   TEXT,
    chunk_file TEXT,
    user       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    reason     TEXT,
    page       TEXT,
    PRIMARY KEY (cam, ff_file, meteor_no)
);
CREATE TABLE IF NOT EXISTS history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    cam        TEXT NOT NULL,
    ff_file    TEXT NOT NULL,
    meteor_no  INTEGER NOT NULL,
    action     TEXT NOT NULL CHECK (action IN ('hide', 'unhide')),
    user       TEXT NOT NULL,
    ts         TEXT NOT NULL,
    reason     TEXT,
    page       TEXT
);
CREATE INDEX IF NOT EXISTS idx_history_det ON history(cam, ff_file, meteor_no);
"""


def db_path() -> Path:
    """The store's path, read per call so tests can repoint it."""
    return Path(os.environ.get("ROVIMEN_HIDDEN_DETECTIONS_DB", _DEFAULT_PATH))


def second_key(t: Any) -> str | None:
    """A meteor time cut to the second, ``YYYY-MM-DDTHH:MM:SS``; None if absent.

    Mirrors ``utcSecondKey`` in dashboard-common.js: RMS and lock times use
    either separator and carry fractional seconds."""
    if not t:
        return None
    s = str(t).strip().replace(" ", "T")
    return s[:19] if len(s) >= 19 else None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=_BUSY_TIMEOUT_S, isolation_level=None)
    con.execute(f"PRAGMA busy_timeout={int(_BUSY_TIMEOUT_S * 1000)}")
    con.executescript(_SCHEMA)
    return con


# ── Writes ────────────────────────────────────────────────────────────────

def _state(row: sqlite3.Row | tuple | None) -> dict[str, Any] | None:
    if row is None:
        return None
    user, ts, reason = row
    return {"hidden_by": user, "hidden_at": ts, "hidden_reason": reason}


def hide(det: dict[str, Any], user: str, reason: str | None = None,
         page: str | None = None, path: Path | None = None) -> tuple[dict[str, Any], bool]:
    """Hide one detection row (as returned by ``detection_db``).

    Returns ``(state, changed)``. Hiding a detection that is already hidden
    changes nothing and writes no history row, so a double click or two users
    at once leave one hide, by whoever was first.
    """
    path = path or db_path()
    key = (str(det["cam"]).upper(), str(det["ff_file"]), int(det["meteor_no"]))
    ts = _now()
    with closing(_connect(path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO hidden (cam, ff_file, meteor_no, date, time_utc,"
                " chunk_file, user, ts, reason, page) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (*key, det.get("date"), det.get("time_utc"), det.get("chunk_file"),
                 user, ts, reason, page),
            )
            changed = cur.rowcount == 1
            if changed:
                con.execute(
                    "INSERT INTO history (cam, ff_file, meteor_no, action, user, ts,"
                    " reason, page) VALUES (?,?,?,'hide',?,?,?,?)",
                    (*key, user, ts, reason, page),
                )
            row = con.execute(
                "SELECT user, ts, reason FROM hidden WHERE cam=? AND ff_file=? AND meteor_no=?",
                key,
            ).fetchone()
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    _invalidate()
    return {"hidden": True, **(_state(row) or {})}, changed


def unhide(cam: str, ff_file: str, meteor_no: int, user: str,
           page: str | None = None, path: Path | None = None) -> tuple[dict[str, Any], bool]:
    """Unhide one detection. Returns ``(state, changed)``; a detection that is
    not hidden changes nothing and writes no history row."""
    path = path or db_path()
    key = (cam.upper(), ff_file, int(meteor_no))
    with closing(_connect(path)) as con:
        con.execute("BEGIN IMMEDIATE")
        try:
            cur = con.execute(
                "DELETE FROM hidden WHERE cam=? AND ff_file=? AND meteor_no=?", key)
            changed = cur.rowcount == 1
            if changed:
                con.execute(
                    "INSERT INTO history (cam, ff_file, meteor_no, action, user, ts,"
                    " reason, page) VALUES (?,?,?,'unhide',?,?,NULL,?)",
                    (*key, user, _now(), page),
                )
            con.execute("COMMIT")
        except BaseException:
            con.execute("ROLLBACK")
            raise
    _invalidate()
    return {"hidden": False, "hidden_by": None, "hidden_at": None, "hidden_reason": None}, changed


def history(cam: str, ff_file: str, meteor_no: int,
            path: Path | None = None) -> list[dict[str, Any]]:
    """Every hide and unhide of one detection, oldest first."""
    path = path or db_path()
    if not path.exists():
        return []
    with closing(_connect(path)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT action, user, ts, reason, page FROM history"
            " WHERE cam=? AND ff_file=? AND meteor_no=? ORDER BY id",
            (cam.upper(), ff_file, int(meteor_no)),
        ).fetchall()
    return [dict(r) for r in rows]


# ── Reads: one in-memory index per process ────────────────────────────────

@dataclass(frozen=True)
class HiddenSnapshot:
    """Every hidden detection, indexed by key and by (cam, second)."""

    by_key: dict[tuple[str, str, int], dict[str, Any]] = field(default_factory=dict)
    by_time: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    # Identity of the file content it was read from; part of the key of any
    # response cache that holds a filtered list.
    version: Any = None

    def __len__(self) -> int:
        return len(self.by_key)

    def __bool__(self) -> bool:
        return bool(self.by_key)

    def lookup(self, cam: str | None, ff_file: str | None = None,
               meteor_no: Any = None, time_utc: Any = None) -> dict[str, Any] | None:
        """The hide record of a detection, or None when it is not hidden."""
        if not cam or not self.by_key:
            return None
        cu = cam.upper()
        if ff_file and meteor_no is not None:
            try:
                rec = self.by_key.get((cu, ff_file, int(meteor_no)))
            except (TypeError, ValueError):
                rec = None
            if rec is not None:
                return rec
            # A key that names another meteor is authoritative: two meteors
            # of one FF file share its second.
            return None
        sk = second_key(time_utc)
        return self.by_time.get((cu, sk)) if sk else None

    def match(self, item: dict[str, Any], cam: str | None = None) -> dict[str, Any] | None:
        """The hide record of a listed item (a detection row, a clip card,
        a witness), read from its usual fields."""
        if not self.by_key or not isinstance(item, dict):
            return None
        rms = item.get("rms") if isinstance(item.get("rms"), dict) else {}
        return self.lookup(
            cam or item.get("cam") or item.get("camera"),
            item.get("ff_file") or rms.get("ff_file"),
            item.get("meteor_no") if item.get("meteor_no") is not None else rms.get("meteor_no"),
            item.get("meteor_time") or item.get("time_utc") or rms.get("time_utc"),
        )


_snap_lock = threading.Lock()
_snap: tuple[tuple[str, int, int, int] | None, HiddenSnapshot] = (None, HiddenSnapshot())


def _invalidate() -> None:
    global _snap
    with _snap_lock:
        _snap = (None, _snap[1])


def _file_sig(path: Path) -> tuple[str, int, int, int] | None:
    """Identity of the file's current content: its inode plus SQLite's file
    change counter (header bytes 24-27, bumped by every commit in
    rollback-journal mode). The counter is exact where an mtime is not: two
    commits from different workers inside one timestamp tick still differ."""
    try:
        with open(path, "rb") as fh:
            header = fh.read(28)
            ino = os.fstat(fh.fileno()).st_ino
    except OSError:
        return None
    if len(header) < 28:
        return None
    return (str(path), ino, int.from_bytes(header[24:28], "big"), len(header))


def _load(path: Path, version: Any = None) -> HiddenSnapshot:
    by_key: dict[tuple[str, str, int], dict[str, Any]] = {}
    by_time: dict[tuple[str, str], dict[str, Any]] = {}
    with closing(sqlite3.connect(f"file:{path}?mode=ro", uri=True,
                                 timeout=_BUSY_TIMEOUT_S)) as con:
        rows = con.execute(
            "SELECT cam, ff_file, meteor_no, time_utc, user, ts, reason FROM hidden"
        ).fetchall()
    for cam, ff_file, meteor_no, time_utc, user, ts, reason in rows:
        rec = {"hidden": True, "hidden_by": user, "hidden_at": ts, "hidden_reason": reason,
               "ff_file": ff_file, "meteor_no": meteor_no}
        by_key[(cam.upper(), ff_file, int(meteor_no))] = rec
        sk = second_key(time_utc)
        if sk:
            by_time[(cam.upper(), sk)] = rec
    return HiddenSnapshot(by_key=by_key, by_time=by_time, version=version)


def snapshot(path: Path | None = None) -> HiddenSnapshot:
    """The current hidden set. Cheap: one ``stat`` unless the file changed.

    A store that cannot be read yields the last good snapshot (or an empty
    one): a broken hide store must never take the detection lists down."""
    global _snap
    path = path or db_path()
    sig = _file_sig(path)
    cur_sig, cur = _snap
    if sig is not None and sig == cur_sig:
        return cur
    if sig is None:
        empty = HiddenSnapshot()
        with _snap_lock:
            _snap = (None, empty)
        return empty
    with _snap_lock:
        cur_sig, cur = _snap
        if sig == cur_sig:
            return cur
        try:
            fresh = _load(path, version=sig)
        except sqlite3.Error:
            logger.warning("hidden detections: cannot read %s", path, exc_info=True)
            return cur
        _snap = (sig, fresh)
        return fresh


# ── Applying a snapshot to listed items ───────────────────────────────────

def mark(item: dict[str, Any], rec: dict[str, Any]) -> dict[str, Any]:
    """A copy of ``item`` carrying the hide fields (never mutates a cached
    payload's dict)."""
    return {**item, "hidden": True, "hidden_by": rec.get("hidden_by"),
            "hidden_at": rec.get("hidden_at"), "hidden_reason": rec.get("hidden_reason")}


def apply(items: Iterable[dict[str, Any]], snap: HiddenSnapshot, show_hidden: bool,
          cam: str | None = None) -> list[dict[str, Any]]:
    """Leave hidden items out, or with ``show_hidden`` keep them marked.

    Items that are not hidden are passed through as the same objects, so an
    empty store costs one list copy."""
    items = list(items)
    if not snap:
        return items
    out: list[dict[str, Any]] = []
    for it in items:
        rec = snap.match(it, cam=cam)
        if rec is None:
            out.append(it)
        elif show_hidden:
            out.append(mark(it, rec))
    return out


def count(items: Iterable[dict[str, Any]], snap: HiddenSnapshot,
          cam: str | None = None) -> int:
    """How many of ``items`` are hidden."""
    if not snap:
        return 0
    return sum(1 for it in items if snap.match(it, cam=cam) is not None)


def apply_events(events: Iterable[dict[str, Any]], snap: HiddenSnapshot,
                 show_hidden: bool) -> list[dict[str, Any]]:
    """The event rule: an event whose witnesses are ALL hidden is hidden; an
    event with some hidden witnesses stays and lists only the others (its
    counts follow the listed witnesses). With ``show_hidden`` every event
    stays, its hidden witnesses marked, and a fully hidden event carries
    ``hidden: true``."""
    events = list(events)
    if not snap:
        return events
    out: list[dict[str, Any]] = []
    for ev in events:
        witnesses = ev.get("witnesses") or []
        recs = [snap.match(w) for w in witnesses]
        n_hidden = sum(1 for r in recs if r is not None)
        if not n_hidden:
            out.append(ev)
            continue
        all_hidden = n_hidden == len(witnesses)
        if show_hidden:
            marked = [w if r is None else mark(w, r) for w, r in zip(witnesses, recs)]
            out.append({**ev, "witnesses": marked, "hidden": all_hidden})
            continue
        if all_hidden:
            continue
        kept = [w for w, r in zip(witnesses, recs) if r is None]
        out.append({
            **ev,
            "witnesses": kept,
            "witness_count": len(kept),
            "station_count": len({w.get("host_key") for w in kept}),
            "hidden_witnesses": n_hidden,
        })
    return out


def apply_payload(payload: dict[str, Any], snap: HiddenSnapshot,
                  show_hidden: bool) -> dict[str, Any]:
    """A per-night detections payload (``compute_detections_payload``) with
    hidden detections left out, or marked with ``show_hidden``.

    Always returns a shallow copy carrying ``hidden_count`` (how many of the
    night's clips are hidden); the cached payload is shared by every request
    of the worker and is never mutated. Nothing is rebuilt when the store is
    empty."""
    if not snap:
        return {**payload, "hidden_count": 0}
    detections = payload.get("detections") or []
    by_station: dict[str, Any] = {}
    for hk, data in (payload.get("by_station") or {}).items():
        cams = data.get("cameras") if isinstance(data, dict) else None
        if not cams:
            by_station[hk] = data
            continue
        by_station[hk] = {**data, "cameras": {
            cam: apply(chunks or [], snap, show_hidden, cam=cam)
            for cam, chunks in cams.items()
        }}
    return {
        **payload,
        "by_station": by_station,
        "detections": apply(detections, snap, show_hidden),
        "events": apply_events(payload.get("events") or [], snap, show_hidden),
        "hidden_count": count(detections, snap),
    }


def wants_hidden(value: str | None, anonymous: bool) -> bool:
    """The ``show_hidden`` query flag; never honoured for anonymous callers."""
    return not anonymous and (value or "").lower() in ("1", "true", "yes")
