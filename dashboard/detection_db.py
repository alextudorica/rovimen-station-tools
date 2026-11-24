"""detection_db.py — VPS-side detection index (SQLite).

Single source of truth for the fleet-wide detection metadata index.
Written by index_poller.py (station pulls) and bootstrap_index.py
(storagebox historical scan). Read by dashboard routes (social report,
public API stats) instead of parsing SSHFS files at request time.

Identity is (cam, date, ff_file, meteor_no): the real FF filename plus the
1-based meteor number within that FF. Both producers derive this the same way
(see rms_parse.parse_session_detections), so the same physical detection lands
on one row regardless of source — no cross-source double counting.

Schema versioning: SCHEMA_VERSION is stored in PRAGMA user_version. open_db()
compares the on-disk version to the current one and applies ALTER TABLE
migrations for any missing columns before the caller runs any queries. This
ensures deploy-preserved DBs (excluded from rsync) survive schema additions.
"""

from __future__ import annotations

import logging
import os
import pwd
import re
import sqlite3
import time
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

import sqlite_writer

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("ROVIMEN_DETECTIONS_DB", "/opt/rovimen/detections.db"))

# Columns written for each detection, in a single canonical order shared by
# every INSERT path so producers cannot drift out of sync.
DETECTION_COLS = (
    "cam", "date", "ff_file", "meteor_no",
    "time_utc", "jd", "solar_lon", "shower",
    "mag_apparent", "mag_absolute", "duration_s",
    "ra_beg", "dec_beg", "ra_end", "dec_end",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "angular_velocity", "num_segments", "fps",
    "azim_beg", "elev_beg", "azim_end", "elev_end",
    "chunk_file",
    "x_beg", "y_beg", "clip_offset_s",
    "source",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    cam              TEXT NOT NULL,
    date             TEXT NOT NULL,
    ff_file          TEXT NOT NULL,
    meteor_no        INTEGER NOT NULL DEFAULT 1,
    time_utc         TEXT,
    jd               REAL,
    solar_lon        REAL,
    shower           TEXT,
    mag_apparent     REAL,
    mag_absolute     REAL,
    duration_s       REAL,
    ra_beg           REAL,
    dec_beg          REAL,
    ra_end           REAL,
    dec_end          REAL,
    ra_radiant       REAL,
    dec_radiant      REAL,
    radiant_elev     REAL,
    angular_velocity REAL,
    num_segments     INTEGER,
    fps              REAL,
    azim_beg         REAL,
    elev_beg         REAL,
    azim_end         REAL,
    elev_end         REAL,
    chunk_file       TEXT,
    x_beg            REAL,
    y_beg            REAL,
    clip_offset_s    REAL,
    source           TEXT DEFAULT 'station',
    PRIMARY KEY (cam, date, ff_file, meteor_no)
);

CREATE TABLE IF NOT EXISTS poll_state (
    host_key         TEXT PRIMARY KEY,
    last_polled      REAL,
    since_date       TEXT,
    error_count      INTEGER DEFAULT 0,
    -- Digest of the station index payload that produced the last merge (see
    -- index_poller._payload_digest). A poll whose payload hashes to this value
    -- carries nothing the DB does not already hold, so the poller skips the
    -- upsert entirely instead of re-writing identical rows under the single
    -- SQLite writer lock.
    index_hash       TEXT
);

-- Per-night sky quality, one row per (cam, night). Rides the same station
-- feed as detections (see latest_sky_by_cam): a station with ~0 detections on
-- a night whose cloud_fraction is high was clouded out, not broken. Kept in a
-- separate table (not a detections column) because it is a per-night summary,
-- not a per-FF-detection attribute.
CREATE TABLE IF NOT EXISTS night_sky (
    cam            TEXT NOT NULL,
    night          TEXT NOT NULL,
    n_ff           INTEGER,
    median_stars   REAL,
    cloud_fraction REAL,
    updated        TEXT,
    PRIMARY KEY (cam, night)
);

CREATE TABLE IF NOT EXISTS ingested_files (
    cam         TEXT NOT NULL,
    date        TEXT NOT NULL,
    filename    TEXT NOT NULL,
    file_mtime  REAL NOT NULL,
    row_count   INTEGER NOT NULL,
    ingested_at REAL NOT NULL,
    PRIMARY KEY (cam, date, filename)
);

CREATE INDEX IF NOT EXISTS idx_det_date   ON detections(date);
-- query_detections() is always "WHERE date IN (...) ORDER BY date, time_utc".
-- idx_det_date alone finds the rows but leaves sqlite a full sort of the
-- night; the composite serves the ordering straight out of the index. open_db
-- re-runs this script on every open, so an existing DB picks it up on the next
-- dashboard start with no migration step.
CREATE INDEX IF NOT EXISTS idx_det_date_time ON detections(date, time_utc);
CREATE INDEX IF NOT EXISTS idx_det_cam    ON detections(cam);
CREATE INDEX IF NOT EXISTS idx_det_shower ON detections(shower);
CREATE INDEX IF NOT EXISTS idx_det_mag    ON detections(mag_apparent);
"""

# Bump this integer whenever a new column is added or a column type changes.
# open_db() reads PRAGMA user_version from an existing DB and applies
# ALTER TABLE migrations for every version gap before the caller runs any query.
SCHEMA_VERSION = 3

# Column definitions added in each schema revision, keyed by the version at
# which they were introduced. Entries here drive the ALTER TABLE migration loop.
# Format: {version_introduced: [(table, col, sql_type_and_default), ...]}
_MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    # Version 1 is the baseline — the CREATE TABLE above covers it.
    2: [("detections", "chunk_file", "TEXT")],
    3: [("poll_state", "index_hash", "TEXT")],
}

# SQLite allows a single writer; give concurrent connections time to wait for
# the lock instead of failing immediately with "database is locked".
#
# Must stay comfortably BELOW nginx's proxy_read_timeout on the ingest location
# (120 s, see the /api/ingest/ block on the VPS vhost). When the two were equal
# (both 30 s) every contended write had exactly two ways to end and no good one:
# exhaust the busy-wait and 500, or hold the request until nginx cut the
# connection and 504 — 347 failed station pushes in one night. With the app
# waiting at most 10 s inside a 120 s proxy window, a slow write degrades into a
# slow *success*: the writer that got the lock finishes and the waiter still has
# ~110 s of proxy budget left to answer in.
_BUSY_TIMEOUT_MS = 10_000

# touch_poll_state() only needs to keep MAX(last_polled) fresh for the
# covers_dates() liveness gate, which tolerates 2×poll_interval (≥600 s) of
# staleness. The ingest layer coalesces the call to one per station per ~60 s
# *per worker* (ingest_api._touch_freshness), but with multiple gunicorn
# workers that still multiplies into a per-push write storm on the single
# SQLite writer — measured as the dominant "database is locked" source on
# prod. This DB-level throttle collapses it *across* workers: skip the write
# when the row's stored last_polled is already newer than the window. A pure
# read on a miss (no lock), one write per host per window across the whole
# process group.
_POLL_TOUCH_MIN_INTERVAL_S = 45.0


def _run_write(path: Path, fn):
    """Run ``fn(con)`` in one transaction on this process's shared write
    connection (see ``sqlite_writer``). Replaces the open-write-close pattern
    that tore the WAL down on every request."""
    return sqlite_writer.run_write(path, _connect, fn)


# Rows per transaction for the bulk upserts. A whole station's night merged in
# one transaction held the write lock past the 10 s busy_timeout, so ingest
# writers on the other workers ran out of retries (4 failures in the first
# minutes after the shared-writer fix, all "retry 4"). At 500 rows a commit is
# a fraction of a second and other writers interleave between chunks; every
# chunk is still atomic, and INSERT OR REPLACE makes a partial merge harmless
# to redo.
_WRITE_CHUNK_ROWS = 500
_WRITE_CHUNK_GAP_S = 0.02


def _run_write_chunked(path: Path, rows: list, write_rows) -> int:
    """Write ``rows`` via ``write_rows(con, chunk)`` in ``_WRITE_CHUNK_ROWS``
    transactions. Returns the total count ``write_rows`` reported."""
    total = 0
    for i, start in enumerate(range(0, len(rows), _WRITE_CHUNK_ROWS)):
        if i:
            # Let a waiter in. SQLite's busy handler is a sleep-poll, not a
            # queue: a writer in another process that went to sleep for 50-100
            # ms almost never lands in the microsecond gap between one chunk's
            # COMMIT and the next chunk's first statement, so without this
            # pause a multi-chunk merge still held the lock end to end.
            time.sleep(_WRITE_CHUNK_GAP_S)
        chunk = rows[start:start + _WRITE_CHUNK_ROWS]
        total += _run_write(path, lambda con, chunk=chunk: write_rows(con, chunk))
    return total


def _connect(path: Path, *, read_only: bool = False) -> sqlite3.Connection:
    con = sqlite3.connect(str(path), check_same_thread=False, timeout=_BUSY_TIMEOUT_MS / 1000)
    con.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    if not read_only:
        # journal_mode is persisted on-disk, so once any connection has put the
        # DB into WAL every later connection already opens WAL. Re-issuing
        # `PRAGMA journal_mode=WAL` is not free: it takes a lock transition and
        # can force a checkpoint, and on the ~19-station ingest write path
        # (touch_poll_state / append_detections) that per-connection overhead
        # while holding the single writer lock was a real contributor to the
        # "database is locked" storm. Query the cheap read form first and only
        # set WAL when it isn't already active (fresh DB / first connection).
        # `synchronous` is per-connection (defaults to FULL) so it must be set
        # every time; NORMAL is cheap and never takes a lock.
        try:
            current = con.execute("PRAGMA journal_mode").fetchone()
            mode = (current[0] if current else "").lower()
        except sqlite3.Error:
            mode = ""
        if mode != "wal":
            con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    return con


def _apply_migrations(con: sqlite3.Connection) -> None:
    """Apply any ALTER TABLE migrations needed to reach SCHEMA_VERSION.

    Uses PRAGMA user_version as the on-disk version stamp. Also detects column
    drift via PRAGMA table_info so a DB with columns missing at the table level
    (rather than tracked via user_version) is healed without data loss.
    """
    on_disk: int = con.execute("PRAGMA user_version").fetchone()[0]

    # Detect and heal column-level drift against the current DETECTION_COLS,
    # regardless of user_version (guards DBs that predated version tracking).
    existing_cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(detections)").fetchall()
    }
    all_schema_cols = set(DETECTION_COLS)
    missing = all_schema_cols - existing_cols
    if missing:
        logger.info(
            "detection_db: healing %d missing column(s) on detections table: %s",
            len(missing), sorted(missing),
        )
        for col in sorted(missing):
            # Derive a safe default: TEXT columns default to NULL, numeric to NULL.
            if col == "source":
                col_type, default = "TEXT", "DEFAULT 'station'"
            elif col == "chunk_file":
                col_type, default = "TEXT", ""
            else:
                col_type, default = "REAL", ""
            con.execute(
                f"ALTER TABLE detections ADD COLUMN {col} {col_type} {default}".strip()
            )

    # Apply version-keyed migrations for any versions the DB has not yet seen.
    if on_disk < SCHEMA_VERSION:
        for version in range(on_disk + 1, SCHEMA_VERSION + 1):
            for table, col, col_def in _MIGRATIONS.get(version, []):
                existing = {
                    row[1]
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if col not in existing:
                    logger.info(
                        "detection_db: migration v%d: ALTER TABLE %s ADD COLUMN %s %s",
                        version, table, col, col_def,
                    )
                    con.execute(
                        f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"
                    )
        con.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        con.commit()
        logger.info("detection_db: schema migrated to version %d", SCHEMA_VERSION)


def _chown_db_to_rovimen(path: Path) -> None:
    """If running as root, transfer DB ownership to the rovimen service user.

    Covers the DB file itself and any WAL/SHM sidecars so the long-running
    poller (which runs as rovimen) can open it for writing after bootstrap.
    """
    try:
        pw = pwd.getpwnam("rovimen")
    except KeyError:
        return  # no rovimen user on this host (dev/test env)
    for suffix in ("", "-wal", "-shm"):
        p = path.with_name(path.name + suffix)
        if p.exists():
            try:
                os.chown(p, pw.pw_uid, pw.pw_gid)
                logger.debug("detection_db: chowned %s to rovimen", p)
            except OSError as exc:
                logger.warning("detection_db: could not chown %s: %s", p, exc)


def open_db(path: Path = DB_PATH) -> sqlite3.Connection:
    """Open (or create) the detection index DB, applying schema migrations.

    If called as root the DB is created root-owned and then immediately
    chowned to the rovimen user so the long-running poller can write to it.
    If there is no rovimen user (dev/test environment) ownership is left as-is.
    """
    if os.geteuid() == 0:
        logger.warning(
            "detection_db: open_db called as root — DB will be created then "
            "chowned to rovimen. Run the poller as rovimen to avoid this."
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(path)
    con.executescript(_SCHEMA)
    _apply_migrations(con)
    if os.geteuid() == 0:
        _chown_db_to_rovimen(path)
    return con


def is_ready(path: Path = DB_PATH) -> bool:
    """True if the DB exists and holds at least one detection row."""
    if not path.exists():
        return False
    try:
        with closing(_connect(path, read_only=True)) as con:
            return con.execute("SELECT 1 FROM detections LIMIT 1").fetchone() is not None
    except Exception:
        return False


def covers_dates(
    dates: list[str],
    path: Path = DB_PATH,
    poll_interval: int | None = None,
) -> bool:
    """True if the index can authoritatively answer for the requested range.

    Guards the dashboard fast path. Three conditions must all hold:

    (a) The indexed history reaches back far enough to cover the oldest
        requested date (mid-bootstrap safety: a partially-built index fails).
    (b) The newest indexed date is within one day of the newest requested date.
        The slack matters because the newest requested date is almost always
        "tonight", whose meteor night is still in progress, so the index
        legitimately has no rows for it yet.
    (c) At least one station's data feed is live within 2× the configured poll
        interval: either the index poller completed a poll, or the HTTP ingest
        surface accepted a push (both stamp ``poll_state.last_polled``; push
        stations are never polled, so ingest activity is their only heartbeat).
        A wedged feed whose ``MAX(date)`` still looks current because older
        nights happen to cover the range would otherwise pass (a) and (b)
        while silently serving stale zeros for completed nights.

    ``poll_interval`` overrides the module default (``POLL_INTERVAL`` from
    ``index_poller``). If ``None`` the environment variable
    ``ROVIMEN_INDEX_POLL_INTERVAL`` is read, falling back to 300 s.
    """
    import time as _time
    if not dates or not path.exists():
        return False

    effective_interval = poll_interval
    if effective_interval is None:
        try:
            effective_interval = int(os.environ.get("ROVIMEN_INDEX_POLL_INTERVAL", "300"))
        except (ValueError, TypeError):
            effective_interval = 300

    try:
        with closing(_connect(path, read_only=True)) as con:
            row = con.execute("SELECT MIN(date), MAX(date) FROM detections").fetchone()
            # Freshness: the most recent successful poll OR accepted push
            # (ingest_api touches last_polled too) across all stations.
            poll_row = con.execute(
                "SELECT MAX(last_polled) FROM poll_state WHERE last_polled IS NOT NULL"
            ).fetchone()
    except Exception:
        return False
    if not row or row[0] is None:
        return False

    db_lo, db_hi = row[0], row[1]
    req_lo, req_hi = min(dates), max(dates)
    try:
        req_hi_floor = (datetime.strptime(req_hi, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    except ValueError:
        req_hi_floor = req_hi

    if not (db_lo <= req_lo and db_hi >= req_hi_floor):
        return False

    # Freshness gate: if every feed (poller and push ingest) has been silent
    # for more than 2× the normal interval, the index may be stale even though
    # date coverage looks correct. Fall back to SSHFS rather than serve stale
    # zeros.
    last_polled: float | None = poll_row[0] if poll_row else None
    if last_polled is None:
        # No poll record at all — bootstrap-only DB, no freshness guarantee.
        return False
    staleness_threshold = effective_interval * 2
    if (_time.time() - last_polled) > staleness_threshold:
        logger.debug(
            "covers_dates: index stale — last_polled %.0fs ago, threshold %.0fs",
            _time.time() - last_polled,
            staleness_threshold,
        )
        return False

    return True


# How deep aggregate_stats looks for a pick that is not skipped.
_PICK_CANDIDATES = 500


def aggregate_stats(
    dates: list[str],
    cam_filter: set[str] | None = None,
    path: Path = DB_PATH,
    skip: Callable[[dict[str, Any]], bool] | None = None,
) -> dict[str, Any]:
    """Aggregate detection stats for a list of YYYYMMDD dates.

    Returns: total, per_day, per_cam, per_shower, brightest, longest.
    cam_filter semantics: None = all cameras; a set = only those cameras;
    an *empty* set = no cameras (returns zeroes — never "all").

    ``skip`` leaves rows out of the two picks (``brightest``, ``longest``),
    e.g. detections a user hid as false positives; the counts are
    statistics and include every row.
    """
    empty = {"total": 0, "per_day": {}, "per_cam": {}, "per_shower": [],
             "brightest": None, "longest": None}
    if not dates:
        return empty
    if cam_filter is not None and len(cam_filter) == 0:
        return empty

    placeholders = ",".join("?" * len(dates))
    cam_clause = ""
    cam_params: list[Any] = []
    if cam_filter:
        cam_clause = f" AND cam IN ({','.join('?' * len(cam_filter))})"
        cam_params = list(cam_filter)
    params = list(dates) + cam_params

    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        total = con.execute(
            f"SELECT COUNT(*) FROM detections WHERE date IN ({placeholders}){cam_clause}",
            params,
        ).fetchone()[0]

        per_day = {
            r["date"]: r["cnt"] for r in con.execute(
                f"""SELECT date, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} GROUP BY date""",
                params,
            ).fetchall()
        }

        per_cam = {
            r["cam"]: r["cnt"] for r in con.execute(
                f"""SELECT cam, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} GROUP BY cam""",
                params,
            ).fetchall()
        }

        per_shower = [
            (r["shower"], r["cnt"]) for r in con.execute(
                f"""SELECT shower, COUNT(*) AS cnt FROM detections
                    WHERE date IN ({placeholders}){cam_clause} AND shower IS NOT NULL
                    GROUP BY shower ORDER BY cnt DESC LIMIT 20""",
                params,
            ).fetchall()
        ]

        def _pick(rows: list[sqlite3.Row]) -> dict[str, Any] | None:
            for r in rows:
                row = dict(r)
                if skip is None or not skip(row):
                    return row
            return None

        # Without ``skip`` the first row is the pick; with it, the first of
        # the top _PICK_CANDIDATES that is not skipped.
        pick_limit = 1 if skip is None else _PICK_CANDIDATES
        brightest = _pick(con.execute(
            f"""SELECT cam, date, time_utc, shower, mag_apparent, mag_absolute, ff_file, meteor_no
                FROM detections
                WHERE date IN ({placeholders}){cam_clause}
                  AND (mag_apparent IS NOT NULL OR mag_absolute IS NOT NULL)
                ORDER BY COALESCE(mag_apparent, mag_absolute) ASC LIMIT {pick_limit}""",
            params,
        ).fetchall())

        longest = _pick(con.execute(
            f"""SELECT cam, date, time_utc, shower, duration_s, ff_file, meteor_no
                FROM detections
                WHERE date IN ({placeholders}){cam_clause} AND duration_s IS NOT NULL
                ORDER BY duration_s DESC LIMIT {pick_limit}""",
            params,
        ).fetchall())

    return {
        "total": total,
        "per_day": per_day,
        "per_cam": per_cam,
        "per_shower": per_shower,
        "brightest": brightest,
        "longest": longest,
    }


def query_detections(
    dates: list[str],
    cam_filter: set[str] | None = None,
    min_mag: float | None = None,
    shower: str | None = None,
    path: Path = DB_PATH,
) -> list[dict[str, Any]]:
    """Return filtered detection rows for the given dates."""
    if not dates:
        return []
    if cam_filter is not None and len(cam_filter) == 0:
        return []

    placeholders = ",".join("?" * len(dates))
    clauses = [f"date IN ({placeholders})"]
    params: list[Any] = list(dates)
    if cam_filter:
        clauses.append(f"cam IN ({','.join('?' * len(cam_filter))})")
        params.extend(cam_filter)
    if min_mag is not None:
        clauses.append("COALESCE(mag_apparent, mag_absolute) <= ?")
        params.append(min_mag)
    if shower:
        clauses.append("shower = ?")
        params.append(shower.upper())

    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            f"SELECT * FROM detections WHERE {' AND '.join(clauses)} ORDER BY date, time_utc",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def find_detection(
    cam: str,
    ff_file: str | None = None,
    meteor_no: int | None = None,
    time_utc: str | None = None,
    path: Path = DB_PATH,
) -> dict[str, Any] | None:
    """One detection row, by its key or else by its time to the second.

    By key: ``ff_file`` carries the capture date, and the night (``date``) is
    that date or the day before, so the lookup stays on the primary key.
    By time: the brightest row of that camera in that second (the row a clip
    card shows when an FF file holds several meteors). None when absent.
    """
    cam = (cam or "").upper()
    if not cam:
        return None
    with closing(_connect(path, read_only=True)) as con:
        con.row_factory = sqlite3.Row
        if ff_file and meteor_no is not None:
            dates: list[str] = []
            m = re.search(r"_(\d{8})_\d{6}", ff_file)
            if m:
                try:
                    d = datetime.strptime(m.group(1), "%Y%m%d")
                    dates = [m.group(1), (d - timedelta(days=1)).strftime("%Y%m%d")]
                except ValueError:
                    dates = []
            if dates:
                row = con.execute(
                    "SELECT * FROM detections WHERE cam = ? AND date IN (?, ?)"
                    " AND ff_file = ? AND meteor_no = ?",
                    (cam, *dates, ff_file, int(meteor_no)),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT * FROM detections WHERE cam = ? AND ff_file = ? AND meteor_no = ?",
                    (cam, ff_file, int(meteor_no)),
                ).fetchone()
            return dict(row) if row else None
        if time_utc:
            sec = str(time_utc).strip().replace(" ", "T")[:19]
            if len(sec) < 19:
                return None
            try:
                t = datetime.fromisoformat(sec)
            except ValueError:
                return None
            # The night starts at local noon or earlier; either date covers it.
            dates = [t.strftime("%Y%m%d"), (t - timedelta(days=1)).strftime("%Y%m%d")]
            rows = con.execute(
                "SELECT * FROM detections WHERE date IN (?, ?) AND cam = ?"
                " AND substr(replace(time_utc, ' ', 'T'), 1, 19) = ?"
                " ORDER BY COALESCE(mag_apparent, mag_absolute, 99) LIMIT 1",
                (*dates, cam, sec),
            ).fetchall()
            return dict(rows[0]) if rows else None
    return None


def chunk_meteor_times(cam: str, date: str, path: Path = DB_PATH) -> dict[str, str]:
    """Map ``chunk_file`` -> earliest ``time_utc`` for one camera-night.

    The station's detection push records which colour clip holds each meteor.
    Unlike the ``lock`` in the archived ``state.json`` — cleared by the dawn
    stale-lock sweep once the clip leaves the station's local disk — this
    mapping is written once and never swept, so it is the durable source for
    nights whose lock has since expired (see ``clip_detections``).

    Returns {} rather than raising when the DB is absent or unreadable: a
    missing pointer degrades the panel under a clip, it must not fail the
    night's chunk listing.
    """
    if not cam or not date:
        return {}
    try:
        with closing(_connect(path, read_only=True)) as con:
            rows = con.execute(
                "SELECT chunk_file, MIN(time_utc) FROM detections "
                "WHERE cam = ? AND date = ? AND chunk_file IS NOT NULL "
                "AND chunk_file <> '' AND time_utc IS NOT NULL "
                "GROUP BY chunk_file",
                (cam, date),
            ).fetchall()
    except (sqlite3.Error, OSError):
        return {}
    return {row[0]: row[1] for row in rows if row[0] and row[1]}


# Upsert that is a NO-OP for an unchanged row. Stations re-push their whole
# 14-day window every 300 s and the poller re-merges it every cycle, so nearly
# every row that arrives is already in the table byte-for-byte. ``INSERT OR
# REPLACE`` deleted and re-inserted each of them through the primary key and
# five secondary indexes anyway — all of the steady-state write volume, WAL
# growth and lock hold time on a database three workers plus the poller share.
# With the ``WHERE`` guard SQLite dirties no page for an identical row, so a
# 500-row chunk holds the write lock for the conflict lookups only.
_DETECTION_KEY_COLS = ("cam", "date", "ff_file", "meteor_no")
_DETECTION_UPDATE_COLS = tuple(c for c in DETECTION_COLS if c not in _DETECTION_KEY_COLS)
# ``source`` records which path wrote the row last (push, poller, storagebox);
# where two paths feed the same DB (the dev tier polls push stations too) it
# would flip on every cycle and defeat the guard, so it is written along with
# a real change but never counts as one.
_DETECTION_GUARD_COLS = tuple(c for c in _DETECTION_UPDATE_COLS if c != "source")
_INSERT_SQL = (
    f"INSERT INTO detections ({', '.join(DETECTION_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in DETECTION_COLS)}) "
    f"ON CONFLICT({', '.join(_DETECTION_KEY_COLS)}) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in _DETECTION_UPDATE_COLS)
    + " WHERE "
    + " OR ".join(f"detections.{c} IS NOT excluded.{c}" for c in _DETECTION_GUARD_COLS)
)


def upsert_detections(
    rows: list[dict[str, Any]],
    source: str = "station",
    path: Path = DB_PATH,
    con: sqlite3.Connection | None = None,
) -> int:
    """Bulk-upsert detection rows. Returns the number of rows processed
    (an unchanged row is a no-op write, see ``_INSERT_SQL``).

    Pass ``con`` to write on an existing connection/transaction (lets a caller
    keep all of a session's writes on one connection instead of opening a second
    writer and contending for the single-writer lock).
    """
    if not rows:
        return 0

    def _write(c: sqlite3.Connection, batch: list[dict[str, Any]] = rows) -> int:
        n = 0
        for r in batch:
            payload = {col: r.get(col) for col in DETECTION_COLS}
            payload["source"] = source
            if payload.get("meteor_no") is None:
                payload["meteor_no"] = 1
            c.execute(_INSERT_SQL, payload)
            n += 1
        return n

    if con is not None:
        return _write(con)
    return _run_write_chunked(path, rows, _write)


def latest_date_by_cam(path: Path = DB_PATH) -> dict[str, str]:
    """Return ``{cam: newest indexed night}`` for every camera in the index.

    Feeds the overview's per-station detection-staleness field. Served straight
    off the ``(cam, date, ...)`` primary key, so it is an index-only scan and
    never touches a row.
    """
    if not path.exists():
        return {}
    try:
        with closing(_connect(path, read_only=True)) as con:
            rows = con.execute(
                "SELECT cam, MAX(date) FROM detections GROUP BY cam"
            ).fetchall()
    except Exception:
        logger.warning("could not read detection-index freshness", exc_info=True)
        return {}
    return {str(cam): str(date) for cam, date in rows if cam and date}


_NIGHT_SKY_COLS = ("cam", "night", "n_ff", "median_stars", "cloud_fraction", "updated")
# Same no-op-on-unchanged shape as _INSERT_SQL. ``updated`` is stamped by the
# sender on every push, so it is written only when a metric actually moved —
# otherwise every re-push of an unchanged night would dirty the row.
_NIGHT_SKY_METRIC_COLS = ("n_ff", "median_stars", "cloud_fraction")
_NIGHT_SKY_INSERT_SQL = (
    f"INSERT INTO night_sky ({', '.join(_NIGHT_SKY_COLS)}) "
    f"VALUES ({', '.join(':' + c for c in _NIGHT_SKY_COLS)}) "
    "ON CONFLICT(cam, night) DO UPDATE SET "
    + ", ".join(f"{c} = excluded.{c}" for c in (*_NIGHT_SKY_METRIC_COLS, "updated"))
    + " WHERE "
    + " OR ".join(f"night_sky.{c} IS NOT excluded.{c}" for c in _NIGHT_SKY_METRIC_COLS)
)


def upsert_night_sky(
    rows: list[dict[str, Any]],
    path: Path = DB_PATH,
    con: sqlite3.Connection | None = None,
) -> int:
    """Bulk-upsert per-night sky-quality rows. Returns count written.

    Idempotent on (cam, night): the station recomputes the row when a night's
    CALSTARS changes, so re-pushing the same night just refreshes the metrics.
    Rows missing cam/night are skipped (a partial station payload can't create
    a keyless row). ``updated`` is stamped here if the row didn't carry one.
    """
    if not rows:
        return 0

    def _write(c: sqlite3.Connection, batch: list[dict[str, Any]] = rows) -> int:
        n = 0
        now = datetime.now(tz=None).isoformat(timespec="seconds")
        for r in batch:
            cam, night = r.get("cam"), r.get("night")
            if not cam or not night:
                continue
            payload = {col: r.get(col) for col in _NIGHT_SKY_COLS}
            payload["cam"], payload["night"] = cam, night
            if not payload.get("updated"):
                payload["updated"] = now
            c.execute(_NIGHT_SKY_INSERT_SQL, payload)
            n += 1
        return n

    if con is not None:
        return _write(con)
    return _run_write_chunked(path, rows, _write)


def latest_sky_by_cam(path: Path = DB_PATH) -> dict[str, dict[str, Any]]:
    """Return ``{cam: {night, n_ff, median_stars, cloud_fraction}}`` for the
    newest sky row per camera.

    The per-night sibling of :func:`latest_date_by_cam`: the overview joins the
    two so a stale/zero-detection night can be labelled overcast rather than
    faulty. Absent (older DB, station without the sky feed) → empty dict, and
    the overview simply emits ``weather=None``.
    """
    if not path.exists():
        return {}
    try:
        with closing(_connect(path, read_only=True)) as con:
            con.row_factory = sqlite3.Row
            # One row per cam: the night with the greatest date. GROUP BY cam
            # with MAX(night) and correlated columns via a window-free join on
            # the max — kept simple with a subquery on the (cam, night) PK.
            rows = con.execute(
                """SELECT ns.cam, ns.night, ns.n_ff, ns.median_stars, ns.cloud_fraction
                   FROM night_sky ns
                   JOIN (SELECT cam, MAX(night) AS mx FROM night_sky GROUP BY cam) m
                     ON ns.cam = m.cam AND ns.night = m.mx"""
            ).fetchall()
    except Exception:
        logger.warning("could not read night-sky freshness", exc_info=True)
        return {}
    return {
        str(r["cam"]): {
            "night": str(r["night"]),
            "n_ff": r["n_ff"],
            "median_stars": r["median_stars"],
            "cloud_fraction": r["cloud_fraction"],
        }
        for r in rows if r["cam"] and r["night"]
    }


def get_poll_state(host_key: str, path: Path = DB_PATH) -> dict[str, Any]:
    with closing(_connect(path, read_only=True)) as con:
        row = con.execute(
            "SELECT since_date, last_polled, error_count, index_hash "
            "FROM poll_state WHERE host_key=?",
            (host_key,),
        ).fetchone()
    if row:
        return {
            "since_date": row[0], "last_polled": row[1],
            "error_count": row[2], "index_hash": row[3],
        }
    return {
        "since_date": None, "last_polled": None,
        "error_count": 0, "index_hash": None,
    }


def set_poll_state(
    host_key: str,
    since_date: str,
    error_count: int = 0,
    path: Path = DB_PATH,
    index_hash: str | None = None,
) -> None:
    """Record the outcome of a poll cycle for one station.

    Column-scoped on purpose: the row carries fields written by *different*
    call sites (``last_polled`` is also stamped by the ingest push path via
    :func:`touch_poll_state`, ``index_hash`` only by a poll that actually
    merged). An ``INSERT OR REPLACE`` would silently blank every column the
    caller did not pass, so the upsert names exactly the columns it owns and
    leaves the rest of the row alone. ``index_hash=None`` therefore means
    "unchanged", not "clear it".
    """
    def _write(con: sqlite3.Connection) -> None:
        con.execute(
            """INSERT INTO poll_state (host_key, last_polled, since_date, error_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(host_key) DO UPDATE SET
                   last_polled = excluded.last_polled,
                   since_date  = excluded.since_date,
                   error_count = excluded.error_count""",
            (host_key, time.time(), since_date, error_count),
        )
        if index_hash is not None:
            con.execute(
                "UPDATE poll_state SET index_hash=? WHERE host_key=?",
                (index_hash, host_key),
            )
    _run_write(path, _write)


def touch_poll_state(host_key: str, path: Path = DB_PATH) -> None:
    """Refresh ``last_polled`` without touching the poller's bookkeeping.

    Called from the HTTP ingest path on accepted pushes: push stations are
    skipped by ``index_poller``, so without this the :func:`covers_dates`
    liveness gate would go permanently stale as the fleet converges on push —
    silently disabling the index fast path for every past-night query. An
    accepted push is the push-era equivalent of a successful poll.
    ``since_date``/``error_count`` belong to the poller and are preserved
    (NULL/0 for a never-polled push station).

    Coalesced across workers by ``_POLL_TOUCH_MIN_INTERVAL_S``: the row's
    stored ``last_polled`` is read first (a lock-free read) and the write is
    skipped when it is already fresh, so a fleet of workers collapses onto one
    write per host per window instead of a per-push storm on the single SQLite
    writer. The write itself is also kept lean by ``_connect`` no longer
    re-running the WAL PRAGMA on every write connection.
    """
    now = time.time()
    def _write(con: sqlite3.Connection) -> None:
        row = con.execute(
            "SELECT last_polled FROM poll_state WHERE host_key=?", (host_key,)
        ).fetchone()
        if row and row[0] is not None and (now - row[0]) < _POLL_TOUCH_MIN_INTERVAL_S:
            return  # another worker already refreshed this host within the window
        con.execute(
            """INSERT INTO poll_state (host_key, last_polled)
               VALUES (?, ?)
               ON CONFLICT(host_key) DO UPDATE SET last_polled=excluded.last_polled""",
            (host_key, now),
        )
    _run_write(path, _write)
