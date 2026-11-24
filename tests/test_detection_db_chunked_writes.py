"""Bulk upserts must commit in bounded chunks, not one night-long transaction.

The index poller merges a whole station's detections in one call; done as a
single transaction on a 113 MB database it held the write lock past the 10 s
busy_timeout and ingest writers on the other workers ran out of retries.
"""
from __future__ import annotations

from pathlib import Path

import detection_db


def _row(i: int) -> dict:
    return {"cam": "RO000A", "date": "20260905", "meteor_no": 1,
            "ff_file": f"FF_RO000A_20260905_20{i:04d}_000_0000000.fits",
            "time_utc": f"2026-09-05T20:{i % 60:02d}:00", "source": "station"}


def test_large_upsert_is_split_into_bounded_transactions(tmp_path: Path, monkeypatch):
    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    calls: list[int] = []
    real = detection_db._run_write

    def counting(path, fn):
        calls.append(1)
        return real(path, fn)
    monkeypatch.setattr(detection_db, "_run_write", counting)
    monkeypatch.setattr(detection_db, "_WRITE_CHUNK_ROWS", 100)

    n = detection_db.upsert_detections([_row(i) for i in range(1, 251)], path=db)

    assert n == 250
    assert len(calls) == 3                      # 100 + 100 + 50
    with detection_db._connect(db, read_only=True) as con:
        assert con.execute("SELECT COUNT(*) FROM detections").fetchone()[0] == 250


def test_caller_supplied_connection_is_not_chunked(tmp_path: Path, monkeypatch):
    """A caller that owns the transaction keeps it whole (bootstrap does this)."""
    db = tmp_path / "detections.db"
    con = detection_db.open_db(db)
    calls: list[int] = []
    monkeypatch.setattr(detection_db, "_run_write", lambda *a, **k: calls.append(1))
    with con:
        n = detection_db.upsert_detections([_row(i) for i in range(1, 1201)], path=db, con=con)
    assert n == 1200 and calls == []
    con.close()


def test_empty_and_small_batches_behave(tmp_path: Path):
    db = tmp_path / "detections.db"
    detection_db.open_db(db).close()
    assert detection_db.upsert_detections([], path=db) == 0
    assert detection_db.upsert_detections([_row(1)], path=db) == 1
