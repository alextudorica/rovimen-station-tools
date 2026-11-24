"""Tests for the detection SQLite index pipeline.

Covers the shared parse+join (rms_parse), the station indexer staying in
lockstep with it, the VPS DB aggregation/coverage/dedup semantics, schema
migration hardening, poller freshness gating, and an end-to-end bootstrap
over a synthetic archive.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import detection_db
import detection_indexer
import rms_parse


# ── Synthetic RMS session fixture ─────────────────────────────────────────────
#
# 5 FTP detections across 4 FF files; FF "203000" carries TWO meteors.
# 4 radiants entries (one per FF detection except the FTP-only "202000" one),
# whose begin times line up with FTP begin = FF_block_start + frame0/fps.

def _ftp_block(ff: str, meteor_no: int, frame0: float, frame1: float, mags: tuple[float, float]) -> str:
    hdr = f"{ff}\nRO000M {meteor_no:04d} 0002 0025.00 0 0 0 0 0 0"
    f0 = f"{frame0:09.4f} 100.0 100.0 10.000000 20.000000 130.000000 45.000000 100 {mags[0]:+.2f} 0 10 0"
    f1 = f"{frame1:09.4f} 110.0 110.0 11.000000 21.000000 131.000000 46.000000 100 {mags[1]:+.2f} 0 10 0"
    return f"{hdr}\n{f0}\n{f1}"


FF1 = "FF_RO000M_20260623_200000_500_0000000.fits"  # begin 20:00:01.500 (frame0=25)
FF2 = "FF_RO000M_20260623_201000_000_0000000.fits"  # begin 20:10:00.000, longest (3s)
FF3 = "FF_RO000M_20260623_202000_000_0000000.fits"  # FTP-only (no radiants)
FF4 = "FF_RO000M_20260623_203000_000_0000000.fits"  # two meteors


@pytest.fixture
def session_dir(tmp_path: Path) -> Path:
    d = tmp_path / "RO000M_20260623_180000_000000"
    d.mkdir()

    ftp = "Meteor Count = 000005\n-----\nCam# Meteor# #Segments fps\n-----\n"
    ftp += _ftp_block(FF1, 1, 25, 50, (-2.00, -1.50)) + "\n"     # 1.0 s, peak -2.00
    ftp += _ftp_block(FF2, 1, 0, 75, (-1.00, -0.50)) + "\n"      # 3.0 s
    ftp += _ftp_block(FF3, 1, 0, 25, (3.00, 3.50)) + "\n"        # FTP-only, peak 3.00
    ftp += _ftp_block(FF4, 1, 0, 25, (0.50, 0.80)) + "\n"        # begin 20:30:00.000
    ftp += _ftp_block(FF4, 2, 50, 75, (1.00, 1.20)) + "\n"       # begin 20:30:02.000
    (d / "FTPdetectinfo_RO000M_20260623_180000_000000.txt").write_text(ftp)

    rad_lines = [
        "# RMS single station association",
        "# Date And Time, Beg JD, La Sun, Shower, RAb, Db, RAe, De, RAr, Dr, t0, p0, bp, ep, AppMag, AbsMag, RadElev",
        "20260623 20:00:01.500000, 2461100.000000, 100.0, PER, 10.0, 20.0, 11.0, 21.0, None, None, 0,0,0,0, -2.00, None, 45.0",
        "20260623 20:10:00.000000, 2461100.100000, 100.1, SPO, 12.0, 22.0, 13.0, 23.0, None, None, 0,0,0,0, -1.00, None, 40.0",
        "20260623 20:30:00.000000, 2461100.200000, 100.2, GEM, 14.0, 24.0, 15.0, 25.0, None, None, 0,0,0,0,  0.50, None, 50.0",
        "20260623 20:30:02.000000, 2461100.300000, 100.3, GEM, 16.0, 26.0, 17.0, 27.0, None, None, 0,0,0,0,  1.00, None, 50.0",
    ]
    (d / "RO000M_20260623_180000_000000_radiants.txt").write_text("\n".join(rad_lines))
    return d


# ── Parse + join correctness ──────────────────────────────────────────────────

def test_join_keys_and_count(session_dir):
    rows = rms_parse.parse_session_detections(session_dir)
    # 5 FTP detections -> 5 rows (radiants count mismatch must NOT drop/merge).
    assert len(rows) == 5
    keys = {(r["ff_file"], r["meteor_no"]) for r in rows}
    assert keys == {(FF1, 1), (FF2, 1), (FF3, 1), (FF4, 1), (FF4, 2)}


def test_join_attaches_radiants_by_time_not_position(session_dir):
    rows = {(r["ff_file"], r["meteor_no"]): r for r in rms_parse.parse_session_detections(session_dir)}
    # FF1 begin 20:00:01.5 matches the -2.00/PER radiant.
    assert rows[(FF1, 1)]["shower"] == "PER"
    assert rows[(FF1, 1)]["mag_apparent"] == -2.00
    # FF4 holds two meteors 2 s apart; each gets its own radiant, not swapped.
    assert rows[(FF4, 1)]["mag_apparent"] == 0.50
    assert rows[(FF4, 2)]["mag_apparent"] == 1.00
    # Kinematics come from FTP regardless of the radiants match.
    assert rows[(FF1, 1)]["duration_s"] == pytest.approx(1.0)
    assert rows[(FF1, 1)]["azim_beg"] == pytest.approx(130.0)


def test_ftp_only_detection_kept(session_dir):
    rows = {(r["ff_file"], r["meteor_no"]): r for r in rms_parse.parse_session_detections(session_dir)}
    ftp_only = rows[(FF3, 1)]
    assert ftp_only["shower"] is None          # no radiants partner
    assert ftp_only["mag_apparent"] == 3.00     # falls back to FTP peak mag
    assert ftp_only["duration_s"] == pytest.approx(1.0)


def test_station_indexer_in_lockstep_with_rms_parse(session_dir):
    """The station indexer must emit byte-identical rows to rms_parse so both
    index sources produce the same identity for the same detection."""
    a = sorted(detection_indexer._session_detections(session_dir),
               key=lambda r: (r["ff_file"], r["meteor_no"]))
    b = sorted(rms_parse.parse_session_detections(session_dir),
               key=lambda r: (r["ff_file"], r["meteor_no"]))
    assert a == b


# ── VPS DB aggregation ────────────────────────────────────────────────────────

@pytest.fixture
def populated_db(tmp_path, session_dir):
    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    rows = rms_parse.parse_session_detections(session_dir)
    for r in rows:
        r["cam"], r["date"] = "RO000M", "20260623"
    detection_db.upsert_detections(rows, source="station", path=db)
    return db


def test_aggregate_stats_basic(populated_db):
    stats = detection_db.aggregate_stats(["20260623"], path=populated_db)
    assert stats["total"] == 5
    assert stats["per_day"] == {"20260623": 5}
    assert stats["per_cam"] == {"RO000M": 5}
    shower = dict(stats["per_shower"])
    assert shower == {"PER": 1, "SPO": 1, "GEM": 2}   # None excluded
    assert stats["brightest"]["mag_apparent"] == -2.00
    assert stats["longest"]["duration_s"] == pytest.approx(3.0)   # FF2


def test_empty_cam_filter_returns_zero_not_all(populated_db):
    """An empty cam_filter must mean 'no cameras', never silently 'all' — that
    would leak/count non-public stations on the public API."""
    stats = detection_db.aggregate_stats(["20260623"], cam_filter=set(), path=populated_db)
    assert stats["total"] == 0
    assert detection_db.aggregate_stats(["20260623"], cam_filter={"RO000M"}, path=populated_db)["total"] == 5
    assert detection_db.aggregate_stats(["20260623"], cam_filter={"NOPE"}, path=populated_db)["total"] == 0


def test_covers_dates(populated_db):
    # Seed a fresh poll record so the freshness gate does not block coverage checks.
    interval = 300
    _seed_poll_state(populated_db, time.time() - 10)

    # DB holds exactly 20260623.
    assert detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval) is True
    # One day past the newest indexed night = "tonight" in progress -> still OK
    # (the index legitimately has no rows for it yet).
    assert detection_db.covers_dates(["20260624"], path=populated_db, poll_interval=interval) is True
    # A range whose old end is covered and whose new end is "tonight" -> fast path.
    assert detection_db.covers_dates(["20260623", "20260624"], path=populated_db, poll_interval=interval) is True
    # Old end reaches before the indexed history -> fall back.
    assert detection_db.covers_dates(["20260620", "20260623", "20260624"], path=populated_db, poll_interval=interval) is False
    # >1 day past the newest indexed night -> poller is stale, fall back.
    assert detection_db.covers_dates(["20260625"], path=populated_db, poll_interval=interval) is False
    # Older than the indexed history -> fall back (mid-bootstrap safety).
    assert detection_db.covers_dates(["20260622"], path=populated_db, poll_interval=interval) is False
    assert detection_db.covers_dates([], path=populated_db, poll_interval=interval) is False


def test_no_double_count_across_sources(populated_db, session_dir):
    """Re-ingesting the same detections from the 'storagebox' source must not
    create duplicate rows — the whole point of the shared identity scheme."""
    rows = rms_parse.parse_session_detections(session_dir)
    for r in rows:
        r["cam"], r["date"] = "RO000M", "20260623"
    detection_db.upsert_detections(rows, source="storagebox", path=populated_db)
    assert detection_db.aggregate_stats(["20260623"], path=populated_db)["total"] == 5


def test_multiple_meteors_same_ff_are_distinct_rows(populated_db):
    rows = detection_db.query_detections(["20260623"], path=populated_db)
    ff4 = [r for r in rows if r["ff_file"] == FF4]
    assert len(ff4) == 2
    assert {r["meteor_no"] for r in ff4} == {1, 2}


# ── Schema migration hardening ────────────────────────────────────────────────

def test_open_db_adds_missing_column_without_data_loss(tmp_path):
    """Opening an old-schema DB with a column missing must add the column and
    preserve existing rows — no truncation, no failure."""
    db = tmp_path / "old.db"

    # Build a v0 DB manually: create the table without the 'source' column,
    # insert a row, then call open_db() which must heal the schema.
    with sqlite3.connect(str(db)) as con:
        # Subset of columns that existed before 'source' was introduced.
        con.execute("""
            CREATE TABLE detections (
                cam TEXT NOT NULL,
                date TEXT NOT NULL,
                ff_file TEXT NOT NULL,
                meteor_no INTEGER NOT NULL DEFAULT 1,
                time_utc TEXT,
                jd REAL,
                solar_lon REAL,
                shower TEXT,
                mag_apparent REAL,
                mag_absolute REAL,
                duration_s REAL,
                ra_beg REAL, dec_beg REAL, ra_end REAL, dec_end REAL,
                ra_radiant REAL, dec_radiant REAL, radiant_elev REAL,
                angular_velocity REAL, num_segments INTEGER, fps REAL,
                azim_beg REAL, elev_beg REAL, azim_end REAL, elev_end REAL,
                PRIMARY KEY (cam, date, ff_file, meteor_no)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS poll_state (
                host_key TEXT PRIMARY KEY, last_polled REAL,
                since_date TEXT, error_count INTEGER DEFAULT 0
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS ingested_files (
                cam TEXT NOT NULL, date TEXT NOT NULL, filename TEXT NOT NULL,
                file_mtime REAL NOT NULL, row_count INTEGER NOT NULL,
                ingested_at REAL NOT NULL,
                PRIMARY KEY (cam, date, filename)
            )
        """)
        # Insert a row using only the old columns.
        con.execute(
            "INSERT INTO detections (cam, date, ff_file, meteor_no, shower) "
            "VALUES (?, ?, ?, ?, ?)",
            ("RO000M", "20260623", "FF_RO000M_20260623_200000_500_0000000.fits", 1, "PER"),
        )
        # Leave user_version at 0 to simulate a pre-versioned DB.
        con.execute("PRAGMA user_version=0")

    # Now open via the current open_db — must not raise and must heal the schema.
    healed = detection_db.open_db(db)
    healed.close()

    with sqlite3.connect(str(db)) as con:
        cols = {row[1] for row in con.execute("PRAGMA table_info(detections)").fetchall()}
        assert "source" in cols, "'source' column missing after migration"

        # Existing data must survive.
        row = con.execute(
            "SELECT shower FROM detections WHERE cam='RO000M' AND date='20260623'"
        ).fetchone()
        assert row is not None, "pre-existing row was lost during migration"
        assert row[0] == "PER"

        # user_version bumped.
        ver = con.execute("PRAGMA user_version").fetchone()[0]
        assert ver == detection_db.SCHEMA_VERSION


def test_open_db_idempotent_on_current_schema(tmp_path):
    """Calling open_db twice on a current-schema DB must not raise."""
    db = tmp_path / "current.db"
    detection_db.open_db(db).close()
    detection_db.open_db(db).close()  # must be a no-op, not an error


# ── Poller freshness gate ─────────────────────────────────────────────────────

def _seed_poll_state(db: Path, last_polled: float) -> None:
    """Insert a poll_state row with the given last_polled timestamp."""
    with sqlite3.connect(str(db)) as con:
        con.execute(
            "INSERT OR REPLACE INTO poll_state (host_key, last_polled, since_date, error_count) "
            "VALUES (?, ?, ?, ?)",
            ("gmnro02", last_polled, "20260623", 0),
        )


def test_covers_dates_fails_when_poller_stale(populated_db):
    """covers_dates must return False when last_polled is beyond 2× the poll
    interval, even if date coverage is otherwise adequate."""
    interval = 300  # seconds
    stale_ts = time.time() - (interval * 2 + 60)   # just past the threshold
    _seed_poll_state(populated_db, stale_ts)

    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval)
    assert result is False, "covers_dates should fail when poller is stale"


def test_covers_dates_passes_when_poller_fresh(populated_db):
    """covers_dates must return True when last_polled is within 2× the poll
    interval and date coverage is adequate."""
    interval = 300
    fresh_ts = time.time() - (interval // 2)   # well within threshold
    _seed_poll_state(populated_db, fresh_ts)

    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=interval)
    assert result is True, "covers_dates should pass when poller is fresh and dates covered"


def test_covers_dates_fails_with_no_poll_state(populated_db):
    """covers_dates must return False when there is no poll_state record at all
    (bootstrap-only DB with no live poller running yet)."""
    # populated_db fixture has no poll_state rows by default.
    result = detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=300)
    assert result is False, "covers_dates should fail with no poll_state (bootstrap-only DB)"


def test_touch_poll_state_feeds_freshness_gate(populated_db):
    """A push-ingest touch must satisfy covers_dates' liveness gate on its own.

    Push stations are never polled — an accepted push is their only heartbeat.
    Without the touch the gate goes permanently stale once the fleet converges
    on push, silently disabling the index fast path (regression 2026-07-10)."""
    assert detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=300) is False
    detection_db.touch_poll_state("gmnro10", path=populated_db)
    assert detection_db.covers_dates(["20260623"], path=populated_db, poll_interval=300) is True
    # A never-polled push station carries no poller bookkeeping.
    state = detection_db.get_poll_state("gmnro10", path=populated_db)
    assert state["last_polled"] == pytest.approx(time.time(), abs=5)
    assert state["since_date"] is None


def test_touch_poll_state_preserves_poller_bookkeeping(populated_db):
    """Touching a station the poller also tracks must only refresh last_polled —
    since_date/error_count are the poller's resume state and must survive."""
    detection_db.set_poll_state("gmnro02", "20260601", error_count=3, path=populated_db)
    before = detection_db.get_poll_state("gmnro02", path=populated_db)
    detection_db.touch_poll_state("gmnro02", path=populated_db)
    after = detection_db.get_poll_state("gmnro02", path=populated_db)
    assert after["since_date"] == "20260601"
    assert after["error_count"] == 3
    assert after["last_polled"] >= before["last_polled"]


def test_touch_poll_state_coalesces_within_window(populated_db):
    """A second touch inside the throttle window is a no-op write: the stored
    last_polled is unchanged, so a fleet of gunicorn workers collapses onto
    one write per host per window instead of storming the single SQLite
    writer (the dominant "database is locked" source on prod)."""
    detection_db.touch_poll_state("gmnro10", path=populated_db)
    first = detection_db.get_poll_state("gmnro10", path=populated_db)["last_polled"]
    assert first is not None
    # Immediately touch again — well inside _POLL_TOUCH_MIN_INTERVAL_S.
    detection_db.touch_poll_state("gmnro10", path=populated_db)
    second = detection_db.get_poll_state("gmnro10", path=populated_db)["last_polled"]
    assert second == first, "second touch within the window must not rewrite"


def test_touch_poll_state_writes_again_after_window(populated_db, monkeypatch):
    """Once the window has elapsed (stored last_polled is stale) the touch
    writes again, keeping the liveness gate fed."""
    detection_db.touch_poll_state("gmnro10", path=populated_db)
    first = detection_db.get_poll_state("gmnro10", path=populated_db)["last_polled"]
    # Age the stored value past the window by shrinking the interval to 0 so the
    # freshness check always fails and the write fires.
    monkeypatch.setattr(detection_db, "_POLL_TOUCH_MIN_INTERVAL_S", 0.0)
    time.sleep(0.01)
    detection_db.touch_poll_state("gmnro10", path=populated_db)
    second = detection_db.get_poll_state("gmnro10", path=populated_db)["last_polled"]
    assert second > first, "a stale row must be refreshed"


# ── Poller: skip the merge when the station index has not changed ─────────────
#
# The rolling 14-day window is re-fetched every cycle by design, so a quiet
# daytime cycle re-delivers a window the DB already holds. Re-upserting it used
# to rewrite thousands of identical rows fleet-wide every five minutes while
# holding the single SQLite writer lock the ingest push path needs — measured as
# the dominant lock holder behind the 2026-09-01 ingest 500/504 storm.


class _FakeResponse:
    """Minimal stand-in for a requests.Response from /api/detections-index."""

    def __init__(self, payload: dict) -> None:
        import json
        self._payload = payload
        self.content = json.dumps(payload).encode()

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


def _index_payload(n: int = 2, shower: str = "PER") -> dict:
    return {
        "ready": True,
        "count": n,
        "detections": [
            {
                "cam": "RO000M", "date": "20260623",
                "ff_file": f"FF_RO000M_20260623_2000{i:02d}_000_0000000.fits",
                "meteor_no": 1, "time_utc": f"2026-06-23T20:00:{i:02d}Z",
                "shower": shower,
            }
            for i in range(n)
        ],
        "night_sky": [{"cam": "RO000M", "night": "20260623",
                       "n_ff": 100, "median_stars": 42.0, "cloud_fraction": 0.1}],
    }


@pytest.fixture
def poller(tmp_path, monkeypatch):
    """index_poller wired to a temp DB, with the station HTTP call stubbed.

    Yields a control object: set ``.payload`` to change what the station
    'serves', read ``.upserts`` for the number of detection upserts performed.
    """
    import index_poller

    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    monkeypatch.setattr(index_poller, "DB_PATH", db)
    index_poller._last_merge.clear()

    class _Control:
        payload = _index_payload()
        upserts = 0
        fetches = 0

    ctl = _Control()

    def _fake_get(url, params=None, timeout=None):
        ctl.fetches += 1
        return _FakeResponse(ctl.payload)

    real_upsert = detection_db.upsert_detections

    def _counting_upsert(rows, source="station", path=None, con=None):
        ctl.upserts += 1
        return real_upsert(rows, source=source, path=path, con=con)

    monkeypatch.setattr(index_poller.requests, "get", _fake_get)
    monkeypatch.setattr(index_poller.detection_db, "upsert_detections", _counting_upsert)

    ctl.db = db
    ctl.module = index_poller
    ctl.info = {"ip": "10.0.0.1", "port": 7779, "cameras": ["RO000M"], "label": "test"}
    yield ctl
    index_poller._last_merge.clear()


def _poll_and_record(ctl, host_key: str = "gmnro02"):
    """One full cycle: poll, then persist the outcome exactly as _run_cycle does."""
    result = ctl.module._poll_station(host_key, ctl.info)
    detection_db.set_poll_state(
        host_key, since_date="20260623", error_count=0,
        path=ctl.db, index_hash=result.digest,
    )
    return result


def test_first_poll_merges_and_stores_the_digest(poller):
    result = _poll_and_record(poller)
    assert result.rows == 2
    assert result.digest is not None
    stored = detection_db.get_poll_state("gmnro02", path=poller.db)["index_hash"]
    assert stored == result.digest


def test_unchanged_index_skips_the_upsert(poller):
    _poll_and_record(poller)
    assert poller.upserts == 1

    second = _poll_and_record(poller)
    assert second.rows == 0
    assert second.digest is None, "nothing merged, so the stored digest stands"
    assert poller.upserts == 1, "an unchanged window must not take the writer lock"
    assert poller.fetches == 2, "the window is still fetched — only the write is skipped"
    # The rows the first cycle merged are still there.
    assert detection_db.aggregate_stats(["20260623"], path=poller.db)["total"] == 2


def test_changed_index_merges_again(poller):
    _poll_and_record(poller)
    poller.payload = _index_payload(n=3)
    result = _poll_and_record(poller)
    assert result.rows == 3
    assert poller.upserts == 2
    assert detection_db.aggregate_stats(["20260623"], path=poller.db)["total"] == 3


def test_reprocessed_night_with_same_row_count_still_merges(poller):
    """The digest is over the response bytes, not the row count: a night
    re-reduced into a different shower classification must still propagate."""
    _poll_and_record(poller)
    poller.payload = _index_payload(shower="GEM")
    _poll_and_record(poller)
    assert poller.upserts == 2
    stats = detection_db.aggregate_stats(["20260623"], path=poller.db)
    assert dict(stats["per_shower"]) == {"GEM": 2}


def test_skipped_cycle_still_stamps_freshness(poller):
    """A skipped merge must keep feeding covers_dates' liveness gate — the
    station is being polled successfully, there is simply nothing new."""
    _poll_and_record(poller)
    stale = time.time() - 10_000
    with sqlite3.connect(str(poller.db)) as con:
        con.execute("UPDATE poll_state SET last_polled=?", (stale,))
    assert detection_db.covers_dates(["20260623"], path=poller.db, poll_interval=300) is False

    poller.module._run_cycle({"gmnro02": poller.info})

    assert poller.upserts == 1, "still a skip"
    assert detection_db.covers_dates(["20260623"], path=poller.db, poll_interval=300) is True


def test_forced_resync_ignores_a_matching_digest(poller, monkeypatch):
    """The digest skip is an optimisation, never the only path to correctness:
    every FORCE_MERGE_INTERVAL_S the whole window is written back regardless."""
    _poll_and_record(poller)
    monkeypatch.setattr(poller.module, "FORCE_MERGE_INTERVAL_S", 0.0)
    result = _poll_and_record(poller)
    assert result.rows == 2
    assert poller.upserts == 2


def test_restart_re_merges_even_with_a_stored_digest(poller):
    """_last_merge lives in-process, so the first cycle after a restart merges
    unconditionally — a redeploy is always a full resync."""
    _poll_and_record(poller)
    poller.module._last_merge.clear()   # simulate process restart
    result = _poll_and_record(poller)
    assert result.rows == 2
    assert poller.upserts == 2


def test_error_cycle_preserves_the_stored_digest(poller):
    """The poller's error branch rewrites poll_state without a digest; that must
    not blank the digest and force a needless full merge next cycle."""
    first = _poll_and_record(poller)
    detection_db.set_poll_state(
        "gmnro02", since_date="20260623", error_count=1, path=poller.db,
    )
    assert detection_db.get_poll_state("gmnro02", path=poller.db)["index_hash"] == first.digest


def test_index_not_ready_is_not_cached_as_a_digest(poller):
    """A station whose index is still building must not have its 'not ready'
    response remembered as merged state."""
    poller.payload = {"ready": False, "detections": [], "night_sky": []}
    result = poller.module._poll_station("gmnro02", poller.info)
    assert result.ready is False
    assert result.digest is None
    assert poller.upserts == 0


# ── Bootstrap end-to-end ──────────────────────────────────────────────────────

def test_bootstrap_scans_archive(tmp_path, session_dir, monkeypatch):
    import bootstrap_index
    archive = tmp_path / "archive"
    rms_dir = archive / "RO000M" / "20260623" / "rms"
    rms_dir.mkdir(parents=True)
    for f in session_dir.iterdir():
        (rms_dir / f.name).write_text(f.read_text())

    db = tmp_path / "boot.db"
    bootstrap_index.bootstrap(archive, db, days=0)

    stats = detection_db.aggregate_stats(["20260623"], path=db)
    assert stats["total"] == 5
    assert dict(stats["per_shower"]) == {"PER": 1, "SPO": 1, "GEM": 2}

    # Re-run is a no-op (fingerprint unchanged) and must not duplicate rows.
    bootstrap_index.bootstrap(archive, db, days=0)
    assert detection_db.aggregate_stats(["20260623"], path=db)["total"] == 5
