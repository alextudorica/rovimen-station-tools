"""Tests for dashboard/station_state.py — push seq guard + media pointers."""

from __future__ import annotations

from pathlib import Path

import pytest

import station_state as ss


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "detections.db"


class TestSeqIdempotency:
    def test_unseen_station_starts_at_zero(self, db_path: Path):
        assert ss.get_last_seq("gmn0002", path=db_path) == 0

    def test_fresh_positive_seq_not_replay(self, db_path: Path):
        assert ss.is_replay("gmn0002", 1, path=db_path) is False

    def test_seq_zero_is_replay_on_fresh(self, db_path: Path):
        # last_seq defaults to 0, so seq=0 is <= last_seq → replay/drop.
        assert ss.is_replay("gmn0002", 0, path=db_path) is True

    def test_advance_and_replay(self, db_path: Path):
        ss.advance_seq("gmn0002", 10, path=db_path)
        assert ss.get_last_seq("gmn0002", path=db_path) == 10
        assert ss.is_replay("gmn0002", 10, path=db_path) is True   # equal
        assert ss.is_replay("gmn0002", 9, path=db_path) is True    # older
        assert ss.is_replay("gmn0002", 11, path=db_path) is False  # newer

    def test_advance_is_monotonic_max(self, db_path: Path):
        ss.advance_seq("gmn0002", 10, path=db_path)
        ss.advance_seq("gmn0002", 5, path=db_path)  # stale — must not regress
        assert ss.get_last_seq("gmn0002", path=db_path) == 10

    def test_seq_is_per_station(self, db_path: Path):
        ss.advance_seq("gmn0002", 10, path=db_path)
        assert ss.get_last_seq("gmnro10", path=db_path) == 0


class TestMediaPointers:
    def test_upsert_and_get(self, db_path: Path):
        n = ss.upsert_media_pointers(
            "gmn0002",
            [{"cam": "RO000M", "date": "20260630", "kind": "timelapse",
              "filename": "a_timelapse.mp4", "night_stack": "a.webp"}],
            path=db_path,
        )
        assert n == 1
        rows = ss.get_media_pointers("gmn0002", path=db_path)
        assert len(rows) == 1
        assert rows[0]["filename"] == "a_timelapse.mp4"
        assert rows[0]["extra"] == {"night_stack": "a.webp"}

    def test_dedup_on_identity(self, db_path: Path):
        p = {"cam": "RO000M", "date": "20260630", "kind": "clip", "filename": "c.mkv"}
        ss.upsert_media_pointers("gmn0002", [p], path=db_path)
        ss.upsert_media_pointers("gmn0002", [{**p, "locked": True}], path=db_path)
        rows = ss.get_media_pointers("gmn0002", path=db_path)
        assert len(rows) == 1  # replaced, not duplicated
        assert rows[0]["extra"] == {"locked": True}

    def test_skips_incomplete_pointer(self, db_path: Path):
        n = ss.upsert_media_pointers(
            "gmn0002", [{"cam": "RO000M", "kind": "clip"}], path=db_path
        )
        assert n == 0

    def test_filter_by_date(self, db_path: Path):
        ss.upsert_media_pointers("gmn0002", [
            {"cam": "RO000M", "date": "20260630", "kind": "clip", "filename": "a.mkv"},
            {"cam": "RO000M", "date": "20260629", "kind": "clip", "filename": "b.mkv"},
        ], path=db_path)
        rows = ss.get_media_pointers("gmn0002", date="20260630", path=db_path)
        assert len(rows) == 1
        assert rows[0]["date"] == "20260630"
