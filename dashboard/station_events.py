"""Append-only per-station event history on the VPS.

What it records
---------------
* Events a station sends about itself (``rovimen_pusher.py`` POSTs them to
  ``/api/ingest/v1/<station>/events``): every new line of the station's
  ``~/rovimen_scripts/logs/uplink_events.log`` (uplink class changes, rescue
  Wi-Fi up/down) and one ``boot`` event per boot, saying whether the previous
  boot ended cleanly and how long the host was down.
* Presence the VPS observes itself: ``offline`` when a push station's
  heartbeats go stale (``station_client._demote_stale_push``) and ``online``
  at the first heartbeat after that, so an outage is on record even when the
  station sends nothing.

Storage
-------
One SQLite file, ``ROVIMEN_STATION_EVENTS_DB``, default
``<ROVIMEN_PUSHED_STATE_DIR>/station_events.db`` (``/opt/rovimen/pushed_state``
on the VPS; the deploy rsync's ``--exclude='pushed_state/'`` keeps it across
deploys). SQLite rather than JSONL because the two things it must do are
exactly what a table with a unique key does: drop a re-delivered event
(``UNIQUE(host_key, source, event_id)``, so a station may resend after an
outage) and flip presence once per transition across three gunicorn workers (a
conditional ``UPDATE ... WHERE state=?`` in one transaction). It is a separate
file from ``detections.db`` so its writes never queue behind the detection
writer.

Trust model
-----------
``source`` is set by the code path that writes a row ("station" for the ingest
route, "vps" for the presence functions), never read from a payload, and it is
part of the unique key, so a station's ids can never collide with, pre-empt or
suppress a VPS row. A station may only send the kinds in ``STATION_KINDS``;
``offline``/``online`` are VPS-only. The ingest route takes the host_key from
the authenticated station key, so a key writes only its own station's history.

A station also cannot evict or bury VPS rows: the row cap applies per
(station, source), station-sent rows are limited to
``STATION_EVENTS_PER_DAY`` new rows a day, and a station event time in the
future (or older than the retention window) is stored and sorted as the
receipt time, flagged ``clamped``. Every row keeps the server receipt time.

Bounded by age (``KEEP_DAYS``, 400 days, by receipt time) and by
``MAX_ROWS_PER_STATION`` rows per (station, source); pruned at most once an
hour per process.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import sqlite_writer

logger = logging.getLogger(__name__)

# Injectable clock (tests replace it; every "now" default reads it).
_clock = time.time

KEEP_DAYS = 400
# Row cap per (station, source): station-sent rows and VPS rows are capped
# separately, so however many events a station sends, its VPS rows stay.
MAX_ROWS_PER_STATION = 20_000
# New station-sent rows stored per station per UTC day. An honest station
# sends a few uplink lines and one boot a day; its whole backlog after an
# outage is at most the station-side queue (rovimen_pusher EVENTS_MAX_QUEUED,
# 500), so 2,000 is four backlogs a day.
STATION_EVENTS_PER_DAY = 2_000
# A station event time further than this past its receipt is clamped.
FUTURE_SKEW_S = 300
PRUNE_EVERY_S = 3600.0
# Largest stored detail, as JSON text. A station's log line is capped well
# under this on the station; the bound stops a broken agent filling the file.
MAX_DETAIL_CHARS = 2000

EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
KIND_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
# Kinds rovimen_pusher.py emits: BootTracker ("boot") and
# parse_uplink_log_line ("uplink", "rescue_wifi", "station_log" for any other
# uplink_events.log line). Anything else from a station is rejected.
STATION_KINDS = frozenset({"boot", "uplink", "rescue_wifi", "station_log"})
# Kinds only the VPS writes (note_heartbeat / mark_offline).
VPS_KINDS = frozenset({"online", "offline"})
SOURCES = ("station", "vps")
# Ids of rows the VPS writes (vps-offline-<ts>, vps-online-<ts>); refused from
# a station.
VPS_ID_PREFIX = "vps-"
_HOST_RE = re.compile(r"^[a-z0-9]{1,16}$")

# How long a worker trusts its own memory that a station is online before it
# asks the database again on a heartbeat. Must stay under the stale threshold
# (station_client.PUSH_STALE_SECS): a station this worker saw push less than
# half that long ago cannot have been demoted in between, because demotion
# measures from the newest heartbeat any worker received.
_PUSH_STALE_SECS = int(os.environ.get("ROVIMEN_PUSH_STALE_SECS", "300"))
_ONLINE_MEMO_S = max(10.0, _PUSH_STALE_SECS / 2.0)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    host_key    TEXT NOT NULL,
    event_id    TEXT NOT NULL,
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    source      TEXT NOT NULL,
    detail      TEXT,
    received_at REAL NOT NULL,
    sent_ts     TEXT,
    clamped     INTEGER NOT NULL DEFAULT 0,
    UNIQUE (host_key, source, event_id)
);
CREATE INDEX IF NOT EXISTS idx_events_host_ts ON events(host_key, ts);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_received ON events(received_at);

CREATE TABLE IF NOT EXISTS presence (
    host_key    TEXT PRIMARY KEY,
    state       TEXT NOT NULL,
    since       TEXT,
    updated_at  REAL
);
"""


def db_path() -> Path:
    """Resolved per call so tests and the E2E fixture can repoint it."""
    explicit = os.environ.get("ROVIMEN_STATION_EVENTS_DB")
    if explicit:
        return Path(explicit)
    root = os.environ.get("ROVIMEN_PUSHED_STATE_DIR", "/opt/rovimen/pushed_state")
    return Path(root) / "station_events.db"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=10)
    con.execute("PRAGMA busy_timeout=10000")
    mode = (con.execute("PRAGMA journal_mode").fetchone() or [""])[0]
    if str(mode).lower() != "wal":
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    return con


def _resolve(path: Path | None) -> Path:
    return Path(path) if path is not None else db_path()


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_ts(raw: Any) -> str | None:
    """An ISO-8601 instant as ``YYYY-MM-DDTHH:MM:SSZ`` (UTC), else None."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _detail_text(detail: Any) -> str | None:
    if detail is None:
        return None
    text = json.dumps(detail, separators=(",", ":"), default=str)
    if len(text) > MAX_DETAIL_CHARS:
        text = json.dumps(str(detail)[:MAX_DETAIL_CHARS - 40] + "...")
    return text


# ── Pruning ──────────────────────────────────────────────────────────────

_last_prune: dict[str, float] = {}
_prune_lock = threading.Lock()


def _prune_due(path: Path, now: float) -> bool:
    with _prune_lock:
        key = f"{os.getpid()}:{path}"
        if now - _last_prune.get(key, 0.0) < PRUNE_EVERY_S:
            return False
        _last_prune[key] = now
        return True


def prune(*, path: Path | None = None, now: float | None = None) -> int:
    """Drop events received more than KEEP_DAYS ago, and beyond
    MAX_ROWS_PER_STATION per (station, source): station rows never push VPS
    rows out, whatever their number."""
    p = _resolve(path)
    now = _clock() if now is None else now
    cutoff = now - KEEP_DAYS * 86400

    def _write(con: sqlite3.Connection) -> int:
        removed = con.execute("DELETE FROM events WHERE received_at < ?", (cutoff,)).rowcount
        groups = con.execute(
            "SELECT host_key, source FROM events GROUP BY host_key, source HAVING COUNT(*) > ?",
            (MAX_ROWS_PER_STATION,)).fetchall()
        for host, source in groups:
            removed += con.execute(
                "DELETE FROM events WHERE host_key = ? AND source = ? AND id NOT IN ("
                "SELECT id FROM events WHERE host_key = ? AND source = ? "
                "ORDER BY id DESC LIMIT ?)",
                (host, source, host, source, MAX_ROWS_PER_STATION)).rowcount
        return removed

    return sqlite_writer.run_write(p, _connect, _write)


def _maybe_prune(path: Path, now: float) -> None:
    if not _prune_due(path, now):
        return
    try:
        removed = prune(path=path, now=now)
        if removed:
            logger.info("station_events: pruned %d old events", removed)
    except Exception:
        logger.exception("station_events: prune failed")


# ── Writes ───────────────────────────────────────────────────────────────


def check_station_event(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """``(event, None)`` for an acceptable station-sent event, else
    ``(None, reason)``.

    Checked per event: the ingest route rejects a bad event (and reports it)
    while storing the rest of the batch, so one malformed line can never block
    the station's queue behind it. Only the fields below are read; anything
    else in the event (a ``station`` or ``source`` key, say) is ignored.
    """
    if not isinstance(raw, dict):
        return None, "not_an_object"
    event_id = raw.get("id")
    kind = raw.get("kind")
    if not isinstance(event_id, str) or not EVENT_ID_RE.match(event_id):
        return None, "invalid_id"
    if event_id.lower().startswith(VPS_ID_PREFIX):
        # Second layer: the namespaces are already separate by source, but a
        # station never has a reason to send an id shaped like a VPS row's.
        return None, "id_reserved_for_vps"
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        return None, "invalid_kind"
    if kind in VPS_KINDS:
        return None, "kind_reserved_for_vps"
    if kind not in STATION_KINDS:
        return None, "unknown_kind"
    detail = raw.get("detail")
    if detail is not None and not isinstance(detail, (str, dict, list, int, float, bool)):
        return None, "invalid_detail"
    return {"id": event_id, "kind": kind, "ts": normalize_ts(raw.get("ts")),
            "detail": detail}, None


def validate_station_event(raw: Any) -> dict[str, Any] | None:
    """:func:`check_station_event` without the reason."""
    return check_station_event(raw)[0]


def clamp_station_ts(ts: str | None, received: float) -> tuple[str, bool]:
    """The time a station-sent event is stored and sorted under, and whether
    it was clamped to the receipt time.

    A time more than ``FUTURE_SKEW_S`` after receipt, or older than the
    retention window, becomes the receipt time: a station must not be able to
    float a row above newer VPS rows on the first page, or plant one the
    pruner would treat as ancient. An honest old time (an outage backlog
    delivered days later) is kept. No time at all is the receipt time,
    unmarked.
    """
    if not ts:
        return _iso(received), False
    when = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    if when > received + FUTURE_SKEW_S or when < received - KEEP_DAYS * 86400:
        return _iso(received), True
    return ts, False


def _utc_day_start(epoch: float) -> float:
    d = datetime.fromtimestamp(epoch, timezone.utc)
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def append_events(host_key: str, events: list[dict[str, Any]], *,
                  path: Path | None = None,
                  now: float | None = None) -> tuple[int, int, list[str]]:
    """Store station-sent events (already checked by
    :func:`check_station_event`); returns ``(inserted, duplicates,
    over_quota_ids)``.

    Rows are always written with source "station": there is no parameter to
    say otherwise, so nothing a station sends can land in the VPS's
    namespace. An event whose ``(host_key, "station", id)`` is already stored
    is a duplicate and left as it is, so a station that resends after an
    outage, or after losing the VPS's answer, never doubles a line. At most
    ``STATION_EVENTS_PER_DAY`` new rows per station per UTC day (by receipt
    time) are stored; the rest are refused and logged. Event times are
    clamped by :func:`clamp_station_ts`.
    """
    source = "station"
    if not events:
        return 0, 0, []
    p = _resolve(path)
    now = _clock() if now is None else now

    def _write(con: sqlite3.Connection) -> tuple[int, int, list[str]]:
        today = int(con.execute(
            "SELECT COUNT(*) FROM events WHERE host_key=? AND source='station' "
            "AND received_at >= ?", (host_key, _utc_day_start(now))).fetchone()[0])
        remaining = STATION_EVENTS_PER_DAY - today
        inserted = duplicates = 0
        over: list[str] = []
        for ev in events:
            exists = con.execute(
                "SELECT 1 FROM events WHERE host_key=? AND source=? AND event_id=?",
                (host_key, source, ev["id"])).fetchone()
            if exists:
                duplicates += 1
                continue
            if remaining <= 0:
                over.append(ev["id"])
                continue
            ts, clamped = clamp_station_ts(ev.get("ts"), now)
            cur = con.execute(
                "INSERT OR IGNORE INTO events "
                "(host_key, event_id, ts, kind, source, detail, received_at, sent_ts, clamped) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (host_key, ev["id"], ts, ev["kind"], source,
                 _detail_text(ev.get("detail")), now, ev.get("ts"), 1 if clamped else 0))
            if cur.rowcount == 1:
                inserted += 1
                remaining -= 1
            else:
                duplicates += 1  # the same batch carried the id twice
        return inserted, duplicates, over

    result = sqlite_writer.run_write(p, _connect, _write)
    if result[2]:
        logger.warning("station_events: %s hit the daily quota of %d station events; "
                       "refused %d", host_key, STATION_EVENTS_PER_DAY, len(result[2]))
    _maybe_prune(p, now)
    return result


# Per-process memory of "this station was online at <time>" (see _ONLINE_MEMO_S).
_online_memo: dict[str, float] = {}
# Per-process memory of the last-seen value an offline mark was settled for.
_offline_memo: dict[str, str] = {}
_memo_lock = threading.Lock()


def _seconds_between(since_iso: str | None, now: float) -> int | None:
    ts = normalize_ts(since_iso)
    if ts is None:
        return None
    then = datetime.strptime(ts, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    return max(0, int(now - then))


def note_heartbeat(host_key: str, *, path: Path | None = None,
                   now: float | None = None) -> bool:
    """Record a push station's heartbeat; returns True when it ended an outage.

    Writes an ``online`` event only on the offline -> online transition the
    VPS recorded. The first heartbeat ever seen for a station just creates
    its presence row. A heartbeat inside the memo window costs no database
    access at all.
    """
    now = _clock() if now is None else now
    with _memo_lock:
        seen = _online_memo.get(host_key)
        if seen is not None and now - seen < _ONLINE_MEMO_S:
            _online_memo[host_key] = now
            return False
    p = _resolve(path)
    now_iso = _iso(now)

    def _write(con: sqlite3.Connection) -> dict[str, Any] | None:
        row = con.execute("SELECT state, since FROM presence WHERE host_key=?",
                          (host_key,)).fetchone()
        if row is None:
            con.execute("INSERT OR IGNORE INTO presence (host_key, state, since, updated_at) "
                        "VALUES (?, 'online', ?, ?)", (host_key, now_iso, now))
            return None
        if row[0] != "offline":
            return None
        cur = con.execute("UPDATE presence SET state='online', since=?, updated_at=? "
                          "WHERE host_key=? AND state='offline'", (now_iso, now, host_key))
        if cur.rowcount != 1:
            return None  # another worker recorded this return first
        offline_since = row[1]
        detail = {"text": "heartbeats arriving again", "offline_since": offline_since,
                  "offline_s": _seconds_between(offline_since, now)}
        con.execute(
            "INSERT OR IGNORE INTO events "
            "(host_key, event_id, ts, kind, source, detail, received_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (host_key, f"vps-online-{offline_since or now_iso}", now_iso, "online", "vps",
             _detail_text(detail), now))
        return detail

    detail = sqlite_writer.run_write(p, _connect, _write)
    with _memo_lock:
        _online_memo[host_key] = now
        _offline_memo.pop(host_key, None)
    if detail is not None:
        logger.info("%s: push heartbeats back after %ss offline", host_key, detail.get("offline_s"))
    return detail is not None


def mark_offline(host_key: str, last_seen: str | None, *, path: Path | None = None,
                 now: float | None = None) -> bool:
    """Record that a push station's heartbeats went stale; True on the transition.

    ``last_seen`` is the server-stamped receipt time of the station's last
    heartbeat; it becomes the offline event's time and its id, so every worker
    that notices the same silence writes the same row at most once.
    """
    now = _clock() if now is None else now
    marker = last_seen or ""
    with _memo_lock:
        if _offline_memo.get(host_key) == marker:
            return False
    p = _resolve(path)
    since = normalize_ts(last_seen) or _iso(now)

    def _write(con: sqlite3.Connection) -> bool:
        row = con.execute("SELECT state FROM presence WHERE host_key=?", (host_key,)).fetchone()
        if row is not None and row[0] == "offline":
            return False
        if row is None:
            con.execute("INSERT OR IGNORE INTO presence (host_key, state, since, updated_at) "
                        "VALUES (?, 'offline', ?, ?)", (host_key, since, now))
        else:
            cur = con.execute("UPDATE presence SET state='offline', since=?, updated_at=? "
                              "WHERE host_key=? AND state='online'", (since, now, host_key))
            if cur.rowcount != 1:
                return False
        detail = {"text": "no heartbeat since last_seen", "last_seen": since,
                  "silent_s": _seconds_between(since, now)}
        con.execute(
            "INSERT OR IGNORE INTO events "
            "(host_key, event_id, ts, kind, source, detail, received_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (host_key, f"vps-offline-{since}", since, "offline", "vps",
             _detail_text(detail), now))
        return True

    changed = sqlite_writer.run_write(p, _connect, _write)
    with _memo_lock:
        _offline_memo[host_key] = marker
        _online_memo.pop(host_key, None)
    return changed


def reset_memos_for_tests() -> None:
    with _memo_lock:
        _online_memo.clear()
        _offline_memo.clear()
    with _prune_lock:
        _last_prune.clear()


# ── Reads ────────────────────────────────────────────────────────────────


def list_events(host_keys: list[str] | None = None, *, limit: int = 100,
                since: str | None = None, path: Path | None = None) -> list[dict[str, Any]]:
    """Events newest first. ``host_keys`` None means every station; an empty
    list means none. ``since`` is an ISO instant or date (inclusive)."""
    if host_keys is not None and not host_keys:
        return []
    p = _resolve(path)
    if not p.exists():
        return []
    limit = max(1, min(int(limit), 5000))
    clauses: list[str] = []
    params: list[Any] = []
    if host_keys is not None:
        clauses.append(f"host_key IN ({','.join('?' * len(host_keys))})")
        params.extend(host_keys)
    if since:
        clauses.append("ts >= ?")
        params.append(since)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=10)
    try:
        # ``ts`` is the clamped time (clamp_station_ts), so a station cannot
        # sort a row above newer VPS rows by dating it in the future.
        rows = con.execute(
            f"SELECT host_key, event_id, ts, kind, source, detail, received_at, sent_ts, clamped "
            f"FROM events {where} ORDER BY ts DESC, id DESC LIMIT ?",
            (*params, limit)).fetchall()
    except sqlite3.OperationalError:
        return []  # file exists but holds no schema yet
    finally:
        con.close()
    out = []
    for host, event_id, ts, kind, source, detail, received, sent_ts, clamped in rows:
        try:
            parsed = json.loads(detail) if detail else None
        except ValueError:
            parsed = detail
        out.append({
            "station": host, "event_id": event_id, "ts": ts, "kind": kind,
            "source": source, "detail": parsed, "received_at": _iso(received),
            "sent_ts": sent_ts, "ts_clamped": bool(clamped),
        })
    return out


def parse_since(raw: str | None) -> str | None:
    """``since`` query value as a comparable ISO string: a full instant, or a
    date (YYYY-MM-DD / YYYYMMDD) meaning its midnight UTC. None if invalid."""
    if not raw:
        return None
    text = raw.strip()
    if re.fullmatch(r"\d{8}", text):
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        text += "T00:00:00Z"
    return normalize_ts(text)


def valid_host(host_key: str) -> bool:
    return bool(_HOST_RE.match(host_key or ""))
