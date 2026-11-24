"""Re-pushing an unchanged row must not write to detections.db.

Stations re-send their whole 14-day detection window every 300 s and their
clip pointers every 900 s; ``INSERT OR REPLACE`` deleted and re-inserted every
one of those identical rows (plus every index entry), which was essentially
all of the steady-state write volume on the one SQLite file three gunicorn
workers and the index poller share. The upserts now carry a ``WHERE`` guard so
an identical row dirties nothing.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

import detection_db  # noqa: E402
import sqlite_writer  # noqa: E402
import station_state  # noqa: E402


def _row(i: int, **over) -> dict:
    base = {
        "cam": "RO000H", "date": "20260901", "ff_file": f"FF_{i:04d}.fits", "meteor_no": 1,
        "time_utc": "2026-09-01T22:00:00", "mag_apparent": -1.0, "shower": "PER",
        "chunk_file": "RO000H_20260901_220000_color.mkv",
    }
    base.update(over)
    return base


def _total_changes(path: Path) -> int:
    with sqlite3.connect(str(path)) as con:
        # data_version increments when ANOTHER connection commits a change to
        # the database, which is exactly "did the writer dirty anything".
        return con.execute("PRAGMA data_version").fetchone()[0]


def _reads_only_changes(path: Path):
    con = sqlite3.connect(str(path))
    con.execute("SELECT count(*) FROM detections").fetchone()
    return con


def test_identical_detection_repush_is_a_noop(tmp_path):
    db = tmp_path / "det.db"
    detection_db.open_db(db).close()
    rows = [_row(i) for i in range(5)]
    assert detection_db.upsert_detections(rows, path=db) == 5

    watcher = _reads_only_changes(db)
    before = watcher.execute("PRAGMA data_version").fetchone()[0]
    detection_db.upsert_detections(rows, path=db)
    after = watcher.execute("PRAGMA data_version").fetchone()[0]
    assert after == before, "an identical re-push must not commit any page"

    # A real change still lands.
    rows[2] = _row(2, mag_apparent=-3.5)
    detection_db.upsert_detections(rows, path=db)
    assert watcher.execute("PRAGMA data_version").fetchone()[0] != before
    got = watcher.execute(
        "SELECT mag_apparent FROM detections WHERE ff_file = ?", ("FF_0002.fits",)
    ).fetchone()[0]
    assert got == -3.5
    assert watcher.execute("SELECT count(*) FROM detections").fetchone()[0] == 5
    watcher.close()
    sqlite_writer.close_all()


def test_night_sky_repush_only_writes_when_a_metric_moved(tmp_path):
    db = tmp_path / "det.db"
    detection_db.open_db(db).close()
    rows = [{"cam": "RO000H", "night": "20260901", "n_ff": 300, "median_stars": 40.0,
             "cloud_fraction": 0.1, "updated": "2026-09-02T05:00:00"}]
    detection_db.upsert_night_sky(rows, path=db)
    watcher = _reads_only_changes(db)
    before = watcher.execute("PRAGMA data_version").fetchone()[0]

    # Same metrics, newer 'updated' stamp: nothing to write.
    rows[0]["updated"] = "2026-09-02T06:00:00"
    detection_db.upsert_night_sky(rows, path=db)
    assert watcher.execute("PRAGMA data_version").fetchone()[0] == before

    rows[0]["cloud_fraction"] = 0.8
    detection_db.upsert_night_sky(rows, path=db)
    assert watcher.execute("PRAGMA data_version").fetchone()[0] != before
    cf, upd = watcher.execute(
        "SELECT cloud_fraction, updated FROM night_sky WHERE cam='RO000H'"
    ).fetchone()
    assert cf == 0.8 and upd == "2026-09-02T06:00:00"
    watcher.close()
    sqlite_writer.close_all()


def test_media_pointer_repush_is_a_noop_and_large_batches_are_chunked(tmp_path, monkeypatch):
    db = tmp_path / "state.db"
    pointers = [
        {"cam": "RO000H", "date": "20260901", "kind": "meteors",
         "filename": f"RO000H_20260901_22{i:04d}_color.mkv", "locked": bool(i % 2)}
        for i in range(1200)
    ]
    assert station_state.upsert_media_pointers("gmnro03", pointers, path=db) == 1200
    watcher = sqlite3.connect(str(db))
    watcher.execute("SELECT count(*) FROM media_pointers").fetchone()
    before = watcher.execute("PRAGMA data_version").fetchone()[0]

    # Count transactions: each chunk is one run_write call.
    calls = []
    real = sqlite_writer.run_write

    def counting(path, connect, fn):
        calls.append(1)
        return real(path, connect, fn)

    monkeypatch.setattr(station_state.sqlite_writer, "run_write", counting)
    monkeypatch.setattr(station_state.time, "sleep", lambda s: None)
    station_state.upsert_media_pointers("gmnro03", pointers, path=db)
    assert len(calls) == 3, "1200 pointers -> three 500-row transactions"
    assert watcher.execute("PRAGMA data_version").fetchone()[0] == before

    pointers[7]["locked"] = not pointers[7]["locked"]
    station_state.upsert_media_pointers("gmnro03", pointers, path=db)
    assert watcher.execute("PRAGMA data_version").fetchone()[0] != before
    assert watcher.execute("SELECT count(*) FROM media_pointers").fetchone()[0] == 1200
    watcher.close()
    sqlite_writer.close_all()


def test_media_pointer_updated_at_marks_the_last_change_not_the_last_mention(tmp_path):
    db = tmp_path / "state.db"
    ptr = [{"cam": "RO000H", "date": "20260901", "kind": "meteors",
            "filename": "RO000H_20260901_220000_color.mkv", "locked": False}]
    station_state.upsert_media_pointers("gmnro03", ptr, path=db)
    con = sqlite3.connect(str(db))
    first = con.execute("SELECT updated_at FROM media_pointers").fetchone()[0]
    import time as _t
    _t.sleep(0.01)
    station_state.upsert_media_pointers("gmnro03", ptr, path=db)      # same pointer again
    assert con.execute("SELECT updated_at FROM media_pointers").fetchone()[0] == first
    ptr[0]["locked"] = True
    station_state.upsert_media_pointers("gmnro03", ptr, path=db)      # a real change
    assert con.execute("SELECT updated_at FROM media_pointers").fetchone()[0] > first
    con.close()
    sqlite_writer.close_all()
