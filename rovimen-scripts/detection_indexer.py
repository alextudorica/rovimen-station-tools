#!/usr/bin/env python3
"""detection_indexer.py — Station-side detection metadata indexer.

Polls RMS session directories for new/changed FTPdetectinfo and radiants
files, parses them, and upserts results into a local SQLite index at
~/rovimen_scripts/detections_index.db.

The station API reads this DB to serve /api/detections-index, which the
VPS polls every few minutes instead of scanning the storagebox directly.

The parse+join (rovimen_rms.session_detections) mirrors dashboard/rms_parse.parse_session_detections so a
detection gets the SAME identity (real FF filename + meteor number) whether it
is indexed here or by the VPS bootstrap — that is what stops the two index
sources from double-counting. The two implementations are kept in lockstep by
tests/test_detection_index.py. (Station scripts ship separately from the
dashboard, so they cannot import dashboard modules.)

Run as: detection-indexer.service (systemd)
"""

from __future__ import annotations

import bisect
import json
import logging
import queue
import re
import signal
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ff_thumbs
import rovimen_config
import rovimen_night
import rovimen_rms

# The FF-thumbnail live push rides the same HTTPS ingest byte plane as
# archive_upload / rovimen_pusher. Guarded so the indexer still runs (indexing
# only, no push) on partial installs or in tests without these modules.
try:
    from ingest_blob import post_blob
except Exception:  # pragma: no cover - defensive
    post_blob = None  # type: ignore

try:
    import flags_manager
except Exception:  # pragma: no cover - defensive
    flags_manager = None  # type: ignore

try:
    from rovimen_pusher import (
        resolve_base_url as _resolve_base_url,
        resolve_host_key as _resolve_host_key,
        resolve_ingest_key as _resolve_ingest_key,
    )
except Exception:  # pragma: no cover - defensive
    _resolve_base_url = _resolve_host_key = _resolve_ingest_key = None  # type: ignore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BASE = Path.home()
CONFIG_PATH = rovimen_config.config_path()
DB_PATH = BASE / "rovimen_scripts" / "detections_index.db"

# Scan interval for the hot window (recent sessions that may still change)
HOT_POLL_INTERVAL = 60       # seconds
HOT_DAYS = 14                # days considered "hot"
_stop = False

# Canonical column order shared with the station API endpoint and (minus the
# VPS-only ``source`` column) with dashboard/detection_db.DETECTION_COLS.
COLS = (
    "cam", "date", "ff_file", "meteor_no",
    "time_utc", "jd", "solar_lon", "shower",
    "mag_apparent", "mag_absolute", "duration_s",
    "ra_beg", "dec_beg", "ra_end", "dec_end",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "angular_velocity", "num_segments", "fps",
    "azim_beg", "elev_beg", "azim_end", "elev_end",
    "chunk_file",
    # The meteor's first position (RMS pixels) and the measured time from the
    # first frame of chunk_file to the meteor's first frame (clip_timing).
    "x_beg", "y_beg", "clip_offset_s",
)


# ── Config ────────────────────────────────────────────────────────────────────

def _load_config() -> dict:
    return rovimen_config.load_config(CONFIG_PATH)


# ── RMS session discovery (mirrors station_api logic, no Flask dependency) ────

_ARC_RE = rovimen_night.SESSION_DIR_RE
_FF_RE = rovimen_night.FF_NAME_RE
_JOIN_TOLERANCE_S = rovimen_rms.JOIN_TOLERANCE_S

_night_date = rovimen_night.night_of


def _rms_camera_dirs(cfg: dict) -> list[tuple[str, Path]]:
    """Return (cam_code, rms_data_path) pairs from config."""
    results: list[tuple[str, Path]] = []
    for cam_code, info in cfg.get("stations", {}).items():
        rms_path = info.get("rms_data_path")
        if rms_path:
            p = Path(rms_path)
            if p.exists():
                results.append((cam_code, p))
    if not results:
        rms_base = BASE / "RMS_data"
        if rms_base.exists():
            for d in sorted(rms_base.iterdir()):
                if d.is_dir() and (
                    (d / "ArchivedFiles").exists() or (d / "CapturedFiles").exists()
                ):
                    results.append((d.name, d))
    return results


def _all_sessions(rms_dir: Path, since_date: str | None = None) -> list[tuple[str, str, Path]]:
    """Return (cam_code, night_date, session_path) for sessions in rms_dir."""
    sessions: list[tuple[str, str, Path]] = []
    for sub in ("ArchivedFiles", "CapturedFiles"):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        try:
            for d in sub_dir.iterdir():
                if not d.is_dir():
                    continue
                m = _ARC_RE.match(d.name)
                if not m:
                    continue
                cam = m.group(1)
                try:
                    night = _night_date(m.group(2), m.group(3))
                except (ValueError, IndexError):
                    continue
                if since_date and night < since_date:
                    continue
                sessions.append((cam, night, d))
        except OSError:
            pass
    return sessions


# ── Parsers + join (shared: rovimen_rms, mirrored by dashboard/rms_parse) ────

_ff_block_start = rovimen_rms.ff_block_start
_radiant_rows = rovimen_rms.radiant_rows
_ftp_rows = rovimen_rms.ftp_rows
_session_detections = rovimen_rms.session_detections
_merge_row = rovimen_rms.merge_row
_radiant_only_row = rovimen_rms.radiant_only_row


# ── SQLite schema ─────────────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS detections (
    cam             TEXT NOT NULL,
    date            TEXT NOT NULL,
    ff_file         TEXT NOT NULL,
    meteor_no       INTEGER NOT NULL DEFAULT 1,
    time_utc        TEXT,
    jd              REAL,
    solar_lon       REAL,
    shower          TEXT,
    mag_apparent    REAL,
    mag_absolute    REAL,
    duration_s      REAL,
    ra_beg          REAL,
    dec_beg         REAL,
    ra_end          REAL,
    dec_end         REAL,
    ra_radiant      REAL,
    dec_radiant     REAL,
    radiant_elev    REAL,
    angular_velocity REAL,
    num_segments    INTEGER,
    fps             REAL,
    azim_beg        REAL,
    elev_beg        REAL,
    azim_end        REAL,
    elev_end        REAL,
    chunk_file      TEXT,
    x_beg           REAL,
    y_beg           REAL,
    clip_offset_s   REAL,
    clip_offset_file TEXT,
    PRIMARY KEY (cam, date, ff_file, meteor_no)
);

CREATE TABLE IF NOT EXISTS indexed_sessions (
    session_path    TEXT PRIMARY KEY,
    fingerprint     REAL,
    row_count       INTEGER,
    indexed_at      REAL
);

-- Per-night sky quality derived from CALSTARS star counts (one row per
-- cam/night). Served on /api/detections-index so the VPS can tell a cloudy
-- night (few stars, ~0 detections) apart from a broken feed. calstars_mtime
-- gates the recompute: only rebuild the row when the newest CALSTARS changed.
CREATE TABLE IF NOT EXISTS night_sky (
    cam            TEXT NOT NULL,
    night          TEXT NOT NULL,
    n_ff           INTEGER,
    median_stars   REAL,
    cloud_fraction REAL,
    calstars_mtime REAL,
    updated        TEXT,
    PRIMARY KEY (cam, night)
);

CREATE INDEX IF NOT EXISTS idx_det_date    ON detections(date);
CREATE INDEX IF NOT EXISTS idx_det_cam     ON detections(cam);
CREATE INDEX IF NOT EXISTS idx_det_shower  ON detections(shower);
CREATE INDEX IF NOT EXISTS idx_det_mag     ON detections(mag_apparent);

-- Station-only bookkeeping (key/value): see _sync_rotation_offsets.
CREATE TABLE IF NOT EXISTS indexer_meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

# Bump when adding columns. _open_db() applies ALTER TABLE for any missing
# columns detected via PRAGMA table_info, keeping the DB backward-compatible.
_SCHEMA_VERSION = 3

# Version-keyed migrations: {version: [(table, col, col_def), ...]}
_MIGRATIONS: dict[int, list[tuple[str, str, str]]] = {
    # Version 1 is the baseline (covered by CREATE TABLE above).
    2: [("detections", "chunk_file", "TEXT")],
    # clip_offset_file is station-only: which clip clip_offset_s was measured
    # in, so a splice written later (detection_lock) is measured again.
    3: [("detections", "clip_offset_file", "TEXT")],
}

# Re-indexing a session (its files change all night) must keep the measured
# clip offset: an upsert that updates every parsed column but clip_offset_s
# (and the station-only clip_offset_file). A changed chunk_file still sends
# the row back to _clip_offset_pass, which compares it to clip_offset_file.
_KEY_COLS = ("cam", "date", "ff_file", "meteor_no")
_MEASURED_COLS = ("clip_offset_s",)
_INSERT_SQL = (
    f"INSERT INTO detections ({', '.join(COLS)}) "
    f"VALUES ({', '.join(':' + c for c in COLS)}) "
    f"ON CONFLICT({', '.join(_KEY_COLS)}) DO UPDATE SET "
    + ", ".join(f"{c}=excluded.{c}" for c in COLS
                if c not in _KEY_COLS and c not in _MEASURED_COLS)
)

_NIGHT_SKY_INSERT_SQL = (
    "INSERT OR REPLACE INTO night_sky "
    "(cam, night, n_ff, median_stars, cloud_fraction, calstars_mtime, updated) "
    "VALUES (:cam, :night, :n_ff, :median_stars, :cloud_fraction, "
    ":calstars_mtime, :updated)"
)


# ── Per-night sky quality (CALSTARS star counts) ──────────────────────────────
#
# RMS writes one CALSTARS block per FF frame listing the stars it extracted.
# On a clear night a wide-field FF holds tens of stars; when cloud rolls in the
# count collapses toward zero. So per night:
#   cloud_fraction = share of FF blocks whose star count is below a low
#                    threshold, threshold = max(8, ff_min_stars // 3).
# ff_min_stars is the RMS star-extraction floor (config key ``ff_min_stars``,
# RMS default 40 → threshold 13). Dividing by three keeps the bar well under a
# healthy frame's count so a merely hazy frame doesn't read as overcast, while
# the floor of 8 stops a tiny/short FOV from tripping it on a clear night.
#
# A high cloud_fraction on a night with ~0 detections is the weather-vs-fault
# discriminator: cloudy, not a dead station.

_CLOUD_STAR_FLOOR = 8
_FF_MIN_STARS_DEFAULT = 40  # RMS StarExtraction default


def _cloud_star_threshold(cfg: dict) -> int:
    """Low star-count threshold below which an FF frame counts as clouded."""
    try:
        ff_min = int(cfg.get("ff_min_stars", _FF_MIN_STARS_DEFAULT))
    except (TypeError, ValueError):
        ff_min = _FF_MIN_STARS_DEFAULT
    return max(_CLOUD_STAR_FLOOR, ff_min // 3)


_CALSTARS_FF_RE = re.compile(r"(FF_[A-Z0-9]+_\d{8}_\d{6}_\d{3}_\d+\.fits)")


def _calstars_star_counts(path: Path) -> list[int]:
    """Star count per FF block in a CALSTARS file.

    Prefer RMS's own reader (authoritative); fall back to a format-tolerant
    text parse (mirrors station_api._calstars_star_counts) so indexing works
    on hosts without the RMS venv and in tests. Returns [] on any read error.

    An empty result from the RMS reader is treated as a miss, not as an answer:
    the reader skips a fixed-length header and yields nothing at all on a
    readable CALSTARS whose header shape it does not recognise. Returning that
    [] would skip the night_sky row and never try the text parse below, quietly
    costing that station its weather-vs-fault signal.
    """
    try:
        from RMS.Formats import CALSTARS  # noqa: PLC0415 (optional, venv-only)
        calstars_list, _ = CALSTARS.readCALSTARS(str(path.parent), path.name)
        counts_rms = [len(stars) for _ff, stars in calstars_list]
        if counts_rms:
            return counts_rms
    except Exception:
        pass
    counts: dict[str, int] = {}
    current: str | None = None
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return []
    for raw in text.splitlines():
        line = raw.strip()
        m = _CALSTARS_FF_RE.search(line)
        if m:
            current = m.group(1)
            counts.setdefault(current, 0)
            continue
        if current and line:
            tokens = line.replace(",", " ").split()
            try:
                for tok in tokens:
                    float(tok)
            except ValueError:
                continue
            counts[current] += 1
    return list(counts.values())


def _median(values: list[int]) -> float:
    """Median of a non-empty list. Caller guarantees non-empty."""
    s = sorted(values)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return (s[mid - 1] + s[mid]) / 2.0


def _newest_calstars(session: Path) -> Path | None:
    """The most recently modified CALSTARS_*.txt in a session dir, or None."""
    newest: Path | None = None
    newest_mtime = -1.0
    for f in session.glob("CALSTARS_*.txt"):
        try:
            m = f.stat().st_mtime
        except OSError:
            continue
        if m > newest_mtime:
            newest, newest_mtime = f, m
    return newest


def _compute_night_sky(counts: list[int], threshold: int) -> dict:
    """Derive n_ff / median_stars / cloud_fraction from per-FF star counts."""
    n_ff = len(counts)
    below = sum(1 for c in counts if c < threshold)
    return {
        "n_ff": n_ff,
        "median_stars": round(_median(counts), 2) if counts else None,
        "cloud_fraction": round(below / n_ff, 4) if n_ff else None,
    }


def _index_night_sky(
    con: sqlite3.Connection,
    cam: str,
    night: str,
    session: Path,
    cfg: dict,
) -> None:
    """Compute + upsert the per-night sky row from the newest CALSTARS.

    Cheap and idempotent: skips the recompute when the newest CALSTARS mtime
    matches the stored one. No CALSTARS in the session (older/partial night) →
    silently skip, so it never blocks detection indexing.
    """
    calstars = _newest_calstars(session)
    if calstars is None:
        return
    try:
        mtime = calstars.stat().st_mtime
    except OSError:
        return
    row = con.execute(
        "SELECT calstars_mtime FROM night_sky WHERE cam=? AND night=?",
        (cam, night),
    ).fetchone()
    if row and row[0] == mtime:
        return  # unchanged since last compute
    counts = _calstars_star_counts(calstars)
    if not counts:
        return  # unreadable / empty CALSTARS — leave any prior row intact
    metrics = _compute_night_sky(counts, _cloud_star_threshold(cfg))
    con.execute(_NIGHT_SKY_INSERT_SQL, {
        "cam": cam, "night": night,
        "n_ff": metrics["n_ff"],
        "median_stars": metrics["median_stars"],
        "cloud_fraction": metrics["cloud_fraction"],
        "calstars_mtime": mtime,
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    })


def _apply_schema_migrations(con: sqlite3.Connection) -> None:
    """Heal any column-level drift and apply version-keyed migrations."""
    existing_cols = {
        row[1]
        for row in con.execute("PRAGMA table_info(detections)").fetchall()
    }
    missing = set(COLS) - existing_cols
    if missing:
        logger.info("detection_indexer: healing missing column(s): %s", sorted(missing))
        for col in sorted(missing):
            con.execute(f"ALTER TABLE detections ADD COLUMN {col} REAL")

    on_disk: int = con.execute("PRAGMA user_version").fetchone()[0]
    if on_disk < _SCHEMA_VERSION:
        for version in range(on_disk + 1, _SCHEMA_VERSION + 1):
            for table, col, col_def in _MIGRATIONS.get(version, []):
                existing = {
                    row[1]
                    for row in con.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if col not in existing:
                    logger.info(
                        "detection_indexer: migration v%d: ALTER TABLE %s ADD COLUMN %s %s",
                        version, table, col, col_def,
                    )
                    con.execute(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}")
        if on_disk < 3:
            # Re-index every session once: rows written before v3 carry
            # whole-second time_utc and no first-frame position.
            con.execute("DELETE FROM indexed_sessions")
        con.execute(f"PRAGMA user_version={_SCHEMA_VERSION}")
        con.commit()
        logger.info("detection_indexer: schema at version %d", _SCHEMA_VERSION)


def _open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=30)
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.executescript(_SCHEMA)
    _apply_schema_migrations(con)
    return con


# ── Chunk lookup ─────────────────────────────────────────────────────────────

_CHUNK_RE = rovimen_night.CHUNK_RE
# A boundary splice written by detection_lock._stitch_clips, e.g.
# RO000P_20260724_002833_002853_color.mkv — Tstart and Tend, not one start.
_STITCHED_CHUNK_RE = rovimen_night.STITCHED_CHUNK_RE
_CHUNK_DURATION_S = 20
# Sanity bound on a splice's Tstart..Tend so a malformed pair can't swallow a
# detection minutes away. Also the walk-back window in _find_chunk.
_STITCH_SPAN_MAX_S = 600


def _capture_base(cfg: dict) -> Path:
    return rovimen_config.capture_root(cfg)


def _chunk_dt(ds: str, ts: str, cfg: dict | None = None) -> datetime | None:
    """A YYYYMMDD + HHMMSS filename stamp as aware UTC, or None if malformed.

    Honours ``chunk_names_utc: false`` (pre-#851 local-named chunks) like
    station_api; on every current station names are UTC.
    """
    try:
        return rovimen_night.chunk_utc_dt(ds, ts, cfg).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _build_chunk_index(
    cam: str, night: str, cfg: dict,
) -> list[tuple[datetime, datetime, str]]:
    """Return sorted [(start_utc, end_utc, filename)] for cam/night.

    Covers both the raw 20 s capture chunks and the boundary splices
    detection_lock._stitch_clips writes into the same directory. A meteor near
    a chunk edge is locked and uploaded as the splice while the raw chunks stay
    unlocked, so only the splice name resolves against the archive later.
    """
    cap_dir = _capture_base(cfg) / cam / night
    if not cap_dir.exists():
        return []
    index: list[tuple[datetime, datetime, str]] = []
    for f in cap_dir.glob('*_color.mkv'):
        sm = _STITCHED_CHUNK_RE.match(f.name)
        if sm:
            start = _chunk_dt(sm.group(2), sm.group(3), cfg)
            end = _chunk_dt(sm.group(2), sm.group(4), cfg)
            if start is None or end is None:
                continue
            if end < start:  # the splice crosses midnight
                end += timedelta(days=1)
            if not 0 < (end - start).total_seconds() <= _STITCH_SPAN_MAX_S:
                continue
        else:
            m = _CHUNK_RE.match(f.name)
            if not m:
                continue
            start = _chunk_dt(m.group(2), m.group(3), cfg)
            if start is None:
                continue
            end = start + timedelta(seconds=_CHUNK_DURATION_S)
        index.append((start, end, f.name))
    index.sort(key=lambda x: x[0])
    return index


def _find_chunk(
    chunk_index: list[tuple[datetime, datetime, str]],
    time_utc: str,
) -> str | None:
    """Binary search: return the clip filename that contains time_utc, or None.

    A meteor near a chunk edge sits in both its raw chunk and the boundary
    splice, and it is the splice that gets locked and uploaded — prefer it, so
    chunk_file names a file the dashboard can actually find in the archive.
    """
    if not chunk_index or not time_utc:
        return None
    try:
        det_dt = datetime.fromisoformat(time_utc).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    lo, hi = 0, len(chunk_index) - 1
    idx = -1
    while lo <= hi:
        mid = (lo + hi) // 2
        if chunk_index[mid][0] <= det_dt:
            idx = mid
            lo = mid + 1
        else:
            hi = mid - 1
    # Entries sort by start, and a splice starts half a chunk before the raw
    # chunk it overlaps, so the covering splice can sit behind the newest entry
    # that starts before the detection. Walk back over the bounded window where
    # any span could still reach det_dt. Raw chunks don't overlap, so this can
    # only ever add a splice, never a different raw chunk.
    raw: str | None = None
    while idx >= 0:
        start, end, filename = chunk_index[idx]
        if (det_dt - start).total_seconds() > _STITCH_SPAN_MAX_S:
            break
        if det_dt < end:
            if _STITCHED_CHUNK_RE.match(filename):
                return filename
            if raw is None:
                raw = filename
        idx -= 1
    return raw


# ── FF thumbnails: live BW push ──────────────────────────────────────────────
#
# Colour clips + stacks only reach the archive after the dawn re-encode, so a
# detection can sit imageless on the dashboard for hours. Render a tiny BW
# WebP from the detection's FF maxpixel as soon as the row is indexed and POST
# it to the VPS ffthumbs blob subdir. The dashboard serves it until the colour
# stack lands. POSTs run on a worker thread so a slow/unreachable VPS never
# stalls the indexing loop; a failed POST is simply retried on the next poll
# (the uploaded flag is only set on success).

_FFTHUMB_MAX_WIDTH = 480
_FFTHUMB_QUALITY = 80

_ffthumb_queue: queue.Queue[tuple[Path, str, str]] = queue.Queue()
_ffthumb_queued: set[tuple[str, str, str]] = set()
_ffthumb_lock = threading.Lock()
# One-time numpy/venv capability check for this process. When the RMS venv is
# missing/broken, every render fails identically — without this gate the pass
# re-attempted the whole backlog every poll (gmnro05: 120 log msgs/s, 4.7M
# lines/day, journald retention collapsed to hours).
_ffthumb_env_ok: bool | None = None
# Renders that keep failing (corrupt FF, unreadable file) stop being retried
# after this many attempts instead of looping over the backlog forever.
_FFTHUMB_MAX_ATTEMPTS = 3
_ffthumb_failed: dict[tuple[str, str, str], int] = {}
_ingest_unconfigured_warned = False


def _is_file(path: Path) -> bool:
    """``path.is_file()``, False on an I/O error (a failing colour disk)."""
    try:
        return path.is_file()
    except OSError:
        return False


def _ffthumb_name(chunk_file: str | None, ff_file: str) -> str | None:
    """Thumbnail filename for a detection row.

    Prefer the chunk stem (``<cam>_<date>_<HHMMSS>[_<Tend>].webp``) so the
    dashboard can derive it from chunk_file exactly like a stack sibling; fall
    back to the FF's own cam/date/time. None for radiant-only rows (no FF frame
    exists to render).
    """
    if chunk_file and chunk_file.endswith("_color.mkv"):
        return chunk_file[: -len("_color.mkv")] + ".webp"
    m = _FF_RE.match(ff_file or "")
    if not m:
        return None
    return f"{m.group(1)}_{m.group(2)}_{m.group(3)}.webp"


def _locate_ff(cam: str, night: str, ff_file: str, cfg: dict) -> Path | None:
    """Find the FF FITS file for a detection row across RMS session dirs."""
    for _cam_code, rms_dir in _rms_camera_dirs(cfg):
        for s_cam, s_night, session in _all_sessions(rms_dir, since_date=night):
            if s_cam != cam or s_night != night:
                continue
            p = session / ff_file
            if p.is_file():
                return p
    return None


def _render_ffthumb(ff_path: Path, dest: Path) -> bool:
    """Render a BW WebP thumbnail from one FF maxpixel. Atomic write."""
    buf = ff_thumbs.ff_maxpixel_to_webp(
        [ff_path], max_width=_FFTHUMB_MAX_WIDTH, lossless=False,
        quality=_FFTHUMB_QUALITY,
    )
    if buf is None:
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    tmp.write_bytes(buf.getvalue())
    tmp.replace(dest)
    return True


def _post_ffthumb(path: Path, cam: str, night: str, cfg: dict | None = None) -> bool:
    """POST one rendered thumbnail to the VPS ffthumbs blob subdir.

    Marks the per-night uploaded flag on success so neither this loop nor the
    archive_upload backfill re-POSTs it. Returns False (no raise) on any
    failure — the next poll re-enqueues because the flag stays unset.
    """
    global _ingest_unconfigured_warned
    if post_blob is None:
        return False
    cfg = cfg if cfg is not None else _load_config()
    base_url = _resolve_base_url(cfg) if _resolve_base_url else None
    ingest_key = _resolve_ingest_key() if _resolve_ingest_key else None
    host_key = _resolve_host_key(cfg) if _resolve_host_key else None
    if not (base_url and ingest_key and host_key):
        if not _ingest_unconfigured_warned:
            _ingest_unconfigured_warned = True
            logger.warning(
                "ffthumb push: ingest not configured (base_url/key/host_key) — "
                "thumbnails stay local until archive_upload backfills them")
        return False
    url = (f"{base_url.rstrip('/')}/api/ingest/v1/{host_key}"
           f"/blob/{cam}/{night}/ffthumbs/{path.name}")
    try:
        data = path.read_bytes()
    except OSError as exc:
        logger.warning("ffthumb read failed for %s: %s", path, exc)
        return False
    if not post_blob(url, ingest_key, data):
        return False
    try:
        if flags_manager is not None:
            flags_manager.mark_ffthumb_uploaded(cam, night, path.name, cfg)
    except OSError as exc:
        logger.warning("could not persist ffthumb uploaded flag for %s: %s",
                       path.name, exc)
    logger.info("pushed ffthumb %s/%s/%s", cam, night, path.name)
    return True


def _ffthumb_worker() -> None:
    """Daemon thread: drain the upload queue. One item at a time, never raises."""
    while not _stop:
        try:
            path, cam, night = _ffthumb_queue.get(timeout=1.0)
        except queue.Empty:
            continue
        try:
            _post_ffthumb(path, cam, night)
        except Exception:
            logger.exception("ffthumb upload error for %s", path.name)
        finally:
            with _ffthumb_lock:
                _ffthumb_queued.discard((cam, night, path.name))
            _ffthumb_queue.task_done()


# ── Measured clip offsets ────────────────────────────────────────────────────
#
# Where each detection's meteor starts in its colour clip, measured from the
# video (clip_timing.measure_meteor_start): the clip-name time is off by the
# camera/relay latency, keyframe-aligned segment starts and whole-second
# names, 0.5-1.5 s and different per camera. The players seek to this.

_CLIP_OFFSET_BATCH = 30          # measurements per pass (each ~1-2 s of ffmpeg)
_CLIP_SETTLE_S = 60              # leave a clip alone this long after its last write


def _clip_start(name: str, cfg: dict) -> datetime | None:
    """The start time a clip's filename encodes (raw chunk or splice)."""
    m = _STITCHED_CHUNK_RE.match(name) or _CHUNK_RE.match(name)
    return _chunk_dt(m.group(2), m.group(3), cfg) if m else None


ROTATION_RESYNC_SUN_DEG = 5.0


def _sync_rotation_offsets(con: sqlite3.Connection, cfg: dict,
                           now: datetime | None = None) -> int:
    """Send a camera's measured clip offsets back to be measured again when its
    ``rotate`` setting is not the one they were measured with.

    clip_timing crops the meteor in the turned frame of a ``rotate: true``
    camera (#1425); offsets measured before that cropped the wrong patch of
    sky. ``indexer_meta.rotated_cams`` records the rotated cameras the stored
    offsets assume. No record means offsets older than #1425, so every rotated
    camera is measured again once; afterwards only a camera whose flag
    changes. Returns the number of rows sent back.

    Only while the sun is at least ROTATION_RESYNC_SUN_DEG up: rows sent back
    are measured 30 a minute whatever the hour (their nights are done), so a
    bundle that lands at the 16:45 UTC update must not start a backlog of
    decodes beside RMS capture on a Pi. Until then nothing is recorded and a
    later pass tries again."""
    rotated = sorted(cam for cam, sc in (cfg.get('stations') or {}).items()
                     if isinstance(sc, dict) and sc.get('rotate'))
    row = con.execute("SELECT value FROM indexer_meta WHERE key='rotated_cams'").fetchone()
    if row is not None:
        try:
            before = set(json.loads(row[0]))
        except ValueError:
            before = set()
        if before == set(rotated):
            return 0
        changed = sorted(before.symmetric_difference(rotated))
    else:
        changed = rotated
    loc = rovimen_config.station_location(cfg)
    if changed and loc is not None and rovimen_night.sun_altitude_deg(
            loc[0], loc[1], now or datetime.now(timezone.utc)) < ROTATION_RESYNC_SUN_DEG:
        return 0
    n = 0
    if changed:
        n = con.execute(
            "UPDATE detections SET clip_offset_file=NULL WHERE clip_offset_file IS NOT NULL "
            f"AND cam IN ({','.join('?' * len(changed))})", changed).rowcount
    con.execute("INSERT OR REPLACE INTO indexer_meta (key, value) VALUES ('rotated_cams', ?)",
                (json.dumps(rotated),))
    con.commit()
    if n:
        logger.info("clip offsets: %d row(s) of %s to measure again (rotate setting)",
                    n, ", ".join(changed))
    return n


def _clip_offset_pass(con: sqlite3.Connection, cfg: dict,
                      since_date: str | None = None) -> int:
    """Measure clip_offset_s for detections whose clip has not been measured
    (or has changed since). A meteor not visible in colour records the clip
    with a NULL offset, so it is not decoded again until the clip changes.

    Only nights whose dawn processing is done (``morning_done``): the clips
    are final by then (splices written, re-encoded in place), and the decode
    runs in daylight instead of beside RMS capture on a Pi. Until then the
    dashboard uses the camera's median correction (routes/detections.py)."""
    global _ffthumb_env_ok
    if _ffthumb_env_ok is None:
        _ffthumb_env_ok = ff_thumbs.ensure_rms_venv_on_path()
    if not _ffthumb_env_ok:
        return 0
    import clip_timing
    rows = con.execute(
        "SELECT cam, date, ff_file, meteor_no, time_utc, x_beg, y_beg, chunk_file "
        "FROM detections WHERE chunk_file IS NOT NULL AND x_beg IS NOT NULL "
        "AND (clip_offset_file IS NULL OR clip_offset_file != chunk_file) "
        "AND date >= ? ORDER BY time_utc DESC",
        (since_date or "00000000",),
    ).fetchall()
    base = _capture_base(cfg)
    done = 0
    morning_done: dict[tuple[str, str], bool] = {}
    for cam, night, ff_file, meteor_no, time_utc, x, y, chunk in rows:
        if done >= _CLIP_OFFSET_BATCH or _stop:
            break
        if flags_manager is not None:
            key = (cam, night)
            if key not in morning_done:
                try:
                    morning_done[key] = flags_manager.is_morning_done(cam, night, cfg)
                except Exception:
                    morning_done[key] = False
            if not morning_done[key]:
                continue
        clip = base / cam / night / chunk
        try:
            if time.time() - clip.stat().st_mtime < _CLIP_SETTLE_S:
                continue
        except OSError:
            continue
        start = _clip_start(chunk, cfg)
        try:
            det = datetime.fromisoformat(time_utc).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
        if start is None:
            continue
        expected = (det - start).total_seconds()
        rotate = bool(cfg.get('stations', {}).get(cam, {}).get('rotate', False))
        offset = clip_timing.measure_meteor_start(clip, x, y, expected, rotate=rotate)
        con.execute(
            "UPDATE detections SET clip_offset_s=?, clip_offset_file=? "
            "WHERE cam=? AND date=? AND ff_file=? AND meteor_no=?",
            (offset, chunk, cam, night, ff_file, meteor_no),
        )
        con.commit()
        done += 1
        logger.info("clip offset %s/%s %s: %s (clip name says %.2f s)",
                    cam, night, chunk, f"{offset:.2f} s" if offset is not None else "not visible",
                    expected)
    return done


def _ffthumb_pass(con: sqlite3.Connection, cfg: dict,
                  since_date: str | None = None) -> int:
    """Render missing thumbnails for indexed detections and enqueue un-uploaded
    ones for POSTing. Runs after every scan (and at startup as catch-up).
    Returns the number of thumbnails rendered this pass."""
    global _ffthumb_env_ok
    if _ffthumb_env_ok is None:
        _ffthumb_env_ok = ff_thumbs.ensure_rms_venv_on_path()
        if not _ffthumb_env_ok:
            logger.error(
                "ffthumb: no RMS venv with numpy found (looked at ~/RMS/venv, "
                "~/vRMS, ~/rms_venv) — FF thumbnails disabled until restart")
    if not _ffthumb_env_ok:
        return 0
    if since_date:
        rows = con.execute(
            "SELECT cam, date, ff_file, chunk_file FROM detections WHERE date >= ?",
            (since_date,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT cam, date, ff_file, chunk_file FROM detections").fetchall()

    # Group by night, dedup by thumb name (several detections can share a chunk).
    by_night: dict[tuple[str, str], dict[str, str]] = {}
    for cam, night, ff_file, chunk_file in rows:
        name = _ffthumb_name(chunk_file, ff_file)
        if name is None:
            continue
        by_night.setdefault((cam, night), {}).setdefault(name, ff_file)

    base = rovimen_config.ffthumb_root(cfg)
    rendered = 0
    # Newest nights first so tonight's detections get images before backlog.
    for (cam, night), names in sorted(by_night.items(), key=lambda kv: kv[0][1],
                                      reverse=True):
        thumb_dir = base / cam / night
        legacy_dir = rovimen_config.legacy_ffthumb_dir(cfg, cam, night)
        uploaded: set[str] = set()
        if flags_manager is not None:
            try:
                uploaded = flags_manager.uploaded_ffthumbs(cam, night, cfg)
            except Exception:
                pass
        for name, ff_file in names.items():
            # The archive copy is canonical: once uploaded, a thumbnail the
            # janitor deleted after stacks_days is not rendered again.
            if name in uploaded:
                continue
            dest = thumb_dir / name
            fail_key = (cam, night, name)
            if _ffthumb_failed.get(fail_key, 0) >= _FFTHUMB_MAX_ATTEMPTS:
                continue
            try:
                if not dest.exists() and _is_file(legacy_dir / name):
                    dest = legacy_dir / name
                if not dest.exists():
                    ff_path = _locate_ff(cam, night, ff_file, cfg)
                    if ff_path is None:
                        continue
                    if not _render_ffthumb(ff_path, dest):
                        _ffthumb_failed[fail_key] = _ffthumb_failed.get(fail_key, 0) + 1
                        continue
                    _ffthumb_failed.pop(fail_key, None)
                    rendered += 1
                    logger.info("rendered ffthumb %s/%s/%s", cam, night, name)
            except Exception as exc:
                _ffthumb_failed[fail_key] = _ffthumb_failed.get(fail_key, 0) + 1
                logger.warning("ffthumb render failed for %s/%s/%s (attempt %d/%d): %s",
                               cam, night, name, _ffthumb_failed[fail_key],
                               _FFTHUMB_MAX_ATTEMPTS, exc)
                continue
            _enqueue_ffthumb(dest, cam, night, uploaded)
    return rendered


# ── FF thumbnails for every chunk ────────────────────────────────────────────
#
# The all-footage grid shows a thumbnail per 20 s colour chunk. Its colour
# stack exists only where the night stacker runs and only once archive_upload
# has sent it (hours late, or at dawn, on stations that stack at dawn or not
# at all), but RMS writes an FF for every 10.24 s block whatever the colour
# pipeline does. So every finished chunk of the last two nights gets the BW
# maxpixel of the FF block at its middle, under the chunk stem the dashboard's
# ffthumbs tier already looks up (~1.5 KB each). The colour stack replaces it
# in the dashboard as soon as it lands.

_CHUNK_FFTHUMB_NIGHTS = 2            # tonight and last night
_CHUNK_FFTHUMB_MAX_PER_PASS = 200    # bounds a backlog (fresh install, outage)
_CHUNK_FFTHUMB_SETTLE_S = 20         # after a chunk ends, its middle FF is on disk
_CHUNK_FFTHUMB_GIVE_UP_S = 900       # no FF this long after the end: RMS had a gap
_FF_BLOCK_S = 256 / 25.0
_chunk_ffthumb_skip: set[tuple[str, str, str]] = set()


def _ff_index(cfg: dict, since_night: str) -> dict[str, list[tuple[datetime, Path]]]:
    """FF files of every camera from sessions since *since_night*, as
    {cam: [(block_start_utc, path)]} sorted by start."""
    index: dict[str, list[tuple[datetime, Path]]] = {}
    for _code, rms_dir in _rms_camera_dirs(cfg):
        for cam, _night, session in _all_sessions(rms_dir, since_date=since_night):
            try:
                names = [e.name for e in session.iterdir()]
            except OSError:
                continue
            for name in names:
                if not name.startswith("FF_") or not name.endswith(".fits"):
                    continue
                start = _ff_block_start(name)
                if start is not None:
                    index.setdefault(cam, []).append(
                        (start.replace(tzinfo=timezone.utc), session / name))
    for entries in index.values():
        entries.sort(key=lambda e: e[0])
    return index


def _ff_for_chunk(ffs: list[tuple[datetime, Path]], start: datetime,
                  end: datetime) -> Path | None:
    """The FF whose block covers the chunk's middle, else any FF block that
    overlaps the chunk, else None."""
    mid = start + (end - start) / 2
    starts = [s for s, _ in ffs]
    i = bisect.bisect_right(starts, mid) - 1
    if i >= 0 and (mid - starts[i]).total_seconds() < _FF_BLOCK_S:
        return ffs[i][1]
    for s, path in ffs[max(i, 0):]:
        if s >= end:
            break
        if s + timedelta(seconds=_FF_BLOCK_S) > start:
            return path
    return None


def _chunk_ffthumb_pass(cfg: dict, now: datetime | None = None) -> int:
    """Render a BW thumbnail for every finished colour chunk of the last
    _CHUNK_FFTHUMB_NIGHTS nights that has none, newest first, and queue the
    un-uploaded ones. Returns the number rendered this pass."""
    if not _ffthumb_env_ok:
        return 0
    now = now or datetime.now(timezone.utc)
    base = _capture_base(cfg)
    nights_by_cam: dict[str, list[str]] = {}
    for cam_dir in (base.iterdir() if base.is_dir() else []):
        if not cam_dir.is_dir():
            continue
        nights = sorted((d.name for d in cam_dir.iterdir()
                         if d.is_dir() and re.fullmatch(r"\d{8}", d.name)), reverse=True)
        if nights:
            nights_by_cam[cam_dir.name] = nights[:_CHUNK_FFTHUMB_NIGHTS]
    if not nights_by_cam:
        return 0
    oldest = min(n for nights in nights_by_cam.values() for n in nights)
    since = (datetime.strptime(oldest, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")
    ff_index = _ff_index(cfg, since)

    root = rovimen_config.ffthumb_root(cfg)
    rendered = 0
    todo: list[tuple[datetime, str, str, str, datetime]] = []
    for cam, nights in nights_by_cam.items():
        if cam not in ff_index:
            continue
        for night in nights:
            for start, end, name in _build_chunk_index(cam, night, cfg):
                if _STITCHED_CHUNK_RE.match(name):
                    continue
                if (now - end).total_seconds() < _CHUNK_FFTHUMB_SETTLE_S:
                    continue
                thumb = name[: -len("_color.mkv")] + ".webp"
                if (cam, night, thumb) in _chunk_ffthumb_skip:
                    continue
                todo.append((start, cam, night, thumb, end))

    uploaded_by_night: dict[tuple[str, str], set[str]] = {}
    todo.sort(key=lambda t: t[0], reverse=True)
    for start, cam, night, thumb, end in todo:
        key = (cam, night)
        if key not in uploaded_by_night:
            uploaded: set[str] = set()
            if flags_manager is not None:
                try:
                    uploaded = flags_manager.uploaded_ffthumbs(cam, night, cfg)
                except Exception:
                    pass
            uploaded_by_night[key] = uploaded
        uploaded = uploaded_by_night[key]
        dest = root / cam / night / thumb
        legacy = rovimen_config.legacy_ffthumb_dir(cfg, cam, night) / thumb
        if not dest.exists() and _is_file(legacy):
            dest = legacy
        if dest.exists():
            if thumb in uploaded:
                _chunk_ffthumb_skip.add((cam, night, thumb))
            else:
                _enqueue_ffthumb(dest, cam, night, uploaded)
            continue
        if rendered >= _CHUNK_FFTHUMB_MAX_PER_PASS:
            continue
        ff_path = _ff_for_chunk(ff_index[cam], start, end)
        if ff_path is None:
            if (now - end).total_seconds() > _CHUNK_FFTHUMB_GIVE_UP_S:
                _chunk_ffthumb_skip.add((cam, night, thumb))
            continue
        try:
            ok = _render_ffthumb(ff_path, dest)
        except Exception as exc:
            logger.warning("chunk ffthumb render failed for %s/%s/%s: %s",
                           cam, night, thumb, exc)
            ok = False
        if not ok:
            _chunk_ffthumb_skip.add((cam, night, thumb))
            continue
        rendered += 1
        _enqueue_ffthumb(dest, cam, night, uploaded)
    if rendered:
        logger.info("rendered %d chunk ffthumb(s)", rendered)
    return rendered


def _enqueue_ffthumb(dest: Path, cam: str, night: str, uploaded: set[str]) -> None:
    """Queue one rendered thumbnail for the upload worker, unless it is already
    uploaded or already waiting in the queue."""
    if dest.name in uploaded or post_blob is None:
        return
    key = (cam, night, dest.name)
    with _ffthumb_lock:
        if key in _ffthumb_queued:
            return
        _ffthumb_queued.add(key)
    _ffthumb_queue.put((dest, cam, night))


# ── Indexing logic ────────────────────────────────────────────────────────────

def _session_fingerprint(session: Path) -> float | None:
    """Largest mtime across all radiants + filtered FTPdetectinfo + CALSTARS.

    Any edit to any relevant file moves the fingerprint, so a re-index is
    triggered even when a session has several matching files. CALSTARS is
    included so a clouded-out night that produced star tables but no
    detections still triggers the sky-row compute — that is exactly the night
    the weather signal exists for.
    """
    latest: float | None = None
    try:
        for f in session.iterdir():
            n = f.name
            if n.endswith("_radiants.txt") or n.startswith("CALSTARS") or (
                n.startswith("FTPdetectinfo")
                and "_unfiltered" not in n and "_backup" not in n
            ):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if latest is None or m > latest:
                    latest = m
    except OSError:
        return None
    return latest


def _index_session(
    con: sqlite3.Connection,
    cam: str,
    night: str,
    session: Path,
    cfg: dict | None = None,
) -> int:
    fingerprint = _session_fingerprint(session)
    if fingerprint is None:
        return 0

    row = con.execute(
        "SELECT fingerprint FROM indexed_sessions WHERE session_path=?",
        (str(session),),
    ).fetchone()
    if row and row[0] == fingerprint:
        return 0  # nothing changed

    detections = _session_detections(session)
    if not detections:
        # A clouded-out night legitimately has ~0 detections but still produced
        # CALSTARS, so compute the sky row even here — that's exactly the night
        # the weather-vs-fault signal exists for. Still record the fingerprint
        # so we don't re-parse the (detection-)empty session next poll.
        with con:
            try:
                _index_night_sky(con, cam, night, session, cfg or {})
            except Exception as exc:  # never let sky compute break indexing
                logger.warning("night_sky compute failed for %s/%s: %s", cam, night, exc)
            con.execute(
                "INSERT OR REPLACE INTO indexed_sessions VALUES (?,?,?,?)",
                (str(session), fingerprint, 0, time.time()),
            )
        return 0

    chunk_idx = _build_chunk_index(cam, night, cfg or {})

    # No blanket delete: keying on the real FF filename + meteor number means
    # re-runs and overlapping Captured/Archived dirs INSERT OR REPLACE the same
    # rows, while genuinely distinct detections coexist.
    with con:
        for det in detections:
            payload = {c: det.get(c) for c in COLS}
            payload["cam"], payload["date"] = cam, night
            payload["chunk_file"] = _find_chunk(chunk_idx, det.get("time_utc") or "")
            con.execute(_INSERT_SQL, payload)
        try:
            _index_night_sky(con, cam, night, session, cfg or {})
        except Exception as exc:  # never let sky compute break indexing
            logger.warning("night_sky compute failed for %s/%s: %s", cam, night, exc)
        con.execute(
            "INSERT OR REPLACE INTO indexed_sessions VALUES (?,?,?,?)",
            (str(session), fingerprint, len(detections), time.time()),
        )
    return len(detections)


def _scan(con: sqlite3.Connection, cfg: dict, since_date: str | None = None) -> int:
    total = 0
    for cam, rms_dir in _rms_camera_dirs(cfg):
        for session_cam, night, session in _all_sessions(rms_dir, since_date):
            try:
                n = _index_session(con, session_cam or cam, night, session, cfg)
                if n:
                    logger.info("indexed %s/%s: %d rows", session_cam or cam, night, n)
                    total += n
            except Exception as exc:
                logger.warning("failed to index %s: %s", session, exc)
    return total


# ── Main loop ─────────────────────────────────────────────────────────────────

def _handle_signal(sig, frame):
    global _stop
    logger.info("received signal %s, stopping", sig)
    _stop = True


def main() -> None:
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info("detection_indexer starting, DB: %s", DB_PATH)
    con = _open_db()
    cfg = _load_config()

    # FF-thumbnail POSTs run off-thread so a slow VPS never stalls indexing.
    threading.Thread(target=_ffthumb_worker, name="ffthumb-upload",
                     daemon=True).start()

    # Startup: full scan — catches anything missed while service was down.
    logger.info("startup: full scan of all sessions")
    try:
        n = _scan(con, cfg, since_date=None)
        logger.info("startup scan complete: %d rows indexed", n)
    except Exception as exc:
        logger.error("startup scan failed: %s", exc)

    # Startup catch-up: render+push any missing thumbnails in the hot window
    # (covers detections indexed while the service was down).
    hot_since = (datetime.now(timezone.utc) - timedelta(days=HOT_DAYS)).strftime("%Y%m%d")
    try:
        _ffthumb_pass(con, cfg, since_date=hot_since)
    except Exception as exc:
        logger.error("startup ffthumb pass failed: %s", exc)

    while not _stop:
        start = time.monotonic()
        cfg = _load_config()
        since = (datetime.now(timezone.utc) - timedelta(days=HOT_DAYS)).strftime("%Y%m%d")
        try:
            _scan(con, cfg, since_date=since)
        except Exception as exc:
            logger.error("scan error: %s", exc)
        try:
            _ffthumb_pass(con, cfg, since_date=since)
        except Exception as exc:
            logger.error("ffthumb pass error: %s", exc)
        try:
            _chunk_ffthumb_pass(cfg)
        except Exception as exc:
            logger.error("chunk ffthumb pass error: %s", exc)
        try:
            _sync_rotation_offsets(con, cfg)
            _clip_offset_pass(con, cfg, since_date=since)
        except Exception as exc:
            logger.error("clip offset pass error: %s", exc)
        elapsed = time.monotonic() - start
        sleep_for = max(0, HOT_POLL_INTERVAL - elapsed)
        # Sleep in 1s increments so SIGTERM is handled promptly
        for _ in range(int(sleep_for)):
            if _stop:
                break
            time.sleep(1)

    con.close()
    logger.info("detection_indexer stopped")


if __name__ == "__main__":
    main()
