"""Tests for the station-side per-night sky-quality signal (cloud_fraction).

RMS writes one CALSTARS block per FF frame listing extracted stars; few stars
means cloud. The indexer derives per night: n_ff, median_stars, and
cloud_fraction (share of FF blocks below a low star threshold). A high
cloud_fraction on a ~0-detection night is the weather-vs-fault discriminator.

These cover the pure math, the threshold derivation, and that a missing/empty
CALSTARS skips the sky row without breaking detection indexing.
"""

from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

import detection_indexer


# ── CALSTARS fixture helpers ──────────────────────────────────────────────────

def _calstars(path: Path, blocks: list[tuple[str, int]]) -> None:
    """Write a minimal CALSTARS file: each (ff_name, star_count) becomes one
    FF block followed by ``star_count`` numeric star rows."""
    lines = ["RMS star catalog", "-----"]
    for ff, n in blocks:
        lines.append(ff)
        for i in range(n):
            # x, y, bg_intensity, amplitude, fwhm — all numeric so the parser
            # counts the row as one star.
            lines.append(f"{100 + i}.5 {200 + i}.5 30.0 {500 + i}.0 3.2")
        lines.append("-----")
    path.write_text("\n".join(lines))


def _ff(idx: int) -> str:
    return f"FF_RO000M_20260623_20{idx:02d}00_000_0000000.fits"


def _install_empty_rms_reader(monkeypatch) -> None:
    """Make ``from RMS.Formats import CALSTARS`` resolve to a reader that
    returns no blocks — a station whose RMS reader does not recognise the
    header shape of an otherwise readable CALSTARS."""
    rms = types.ModuleType("RMS")
    formats = types.ModuleType("RMS.Formats")
    calstars = types.ModuleType("RMS.Formats.CALSTARS")
    calstars.readCALSTARS = lambda _dir, _name: ([], None)
    formats.CALSTARS = calstars
    rms.Formats = formats
    for name, mod in (("RMS", rms), ("RMS.Formats", formats),
                      ("RMS.Formats.CALSTARS", calstars)):
        monkeypatch.setitem(sys.modules, name, mod)


# ── cloud_fraction / median / n_ff math ───────────────────────────────────────

class TestCloudFractionMath:
    def test_all_low_star_frames_are_fully_overcast(self):
        counts = [2, 3, 1, 0, 4]  # all below threshold 13
        m = detection_indexer._compute_night_sky(counts, threshold=13)
        assert m["cloud_fraction"] == 1.0
        assert m["n_ff"] == 5
        assert m["median_stars"] == 2.0

    def test_all_high_star_frames_are_clear(self):
        counts = [40, 55, 48, 60, 52]  # all above threshold
        m = detection_indexer._compute_night_sky(counts, threshold=13)
        assert m["cloud_fraction"] == 0.0
        assert m["median_stars"] == 52.0

    def test_mixed_frames_give_a_fraction(self):
        # 3 of 5 below threshold 13 → 0.6
        counts = [2, 3, 4, 40, 50]
        m = detection_indexer._compute_night_sky(counts, threshold=13)
        assert m["cloud_fraction"] == 0.6
        assert m["n_ff"] == 5

    def test_even_length_median_averages_the_middle_pair(self):
        m = detection_indexer._compute_night_sky([10, 20, 30, 40], threshold=13)
        assert m["median_stars"] == 25.0  # (20+30)/2

    def test_empty_counts_yield_none_not_a_crash(self):
        m = detection_indexer._compute_night_sky([], threshold=13)
        assert m["n_ff"] == 0
        assert m["cloud_fraction"] is None
        assert m["median_stars"] is None


class TestThresholdDerivation:
    def test_default_rms_floor_gives_thirteen(self):
        # ff_min_stars absent → RMS default 40 → max(8, 40//3) = 13
        assert detection_indexer._cloud_star_threshold({}) == 13

    def test_small_ff_min_stars_clamps_to_floor(self):
        # 9//3 = 3, below the floor of 8 → 8
        assert detection_indexer._cloud_star_threshold({"ff_min_stars": 9}) == 8

    def test_large_ff_min_stars_scales(self):
        assert detection_indexer._cloud_star_threshold({"ff_min_stars": 60}) == 20

    def test_garbage_ff_min_stars_falls_back_to_default(self):
        assert detection_indexer._cloud_star_threshold({"ff_min_stars": "x"}) == 13


# ── CALSTARS reading ──────────────────────────────────────────────────────────

class TestCalstarsStarCounts:
    def test_counts_stars_per_block(self, tmp_path):
        cs = tmp_path / "CALSTARS_RO000M_20260623.txt"
        _calstars(cs, [(_ff(0), 40), (_ff(1), 2), (_ff(2), 45)])
        counts = detection_indexer._calstars_star_counts(cs)
        assert sorted(counts) == [2, 40, 45]

    def test_missing_file_returns_empty(self, tmp_path):
        assert detection_indexer._calstars_star_counts(tmp_path / "absent.txt") == []

    def test_empty_rms_reader_result_falls_back_to_text_parse(self, tmp_path,
                                                              monkeypatch):
        """An RMS reader that returns nothing must not shadow the text parse.

        The RMS reader skips a fixed-length header, so on a readable CALSTARS
        whose header shape it does not recognise it returns [] rather than
        raising — and the station silently loses its sky signal for the night.
        """
        _install_empty_rms_reader(monkeypatch)
        cs = tmp_path / "CALSTARS_RO000M_20260623.txt"
        _calstars(cs, [(_ff(0), 40), (_ff(1), 2), (_ff(2), 45)])
        assert sorted(detection_indexer._calstars_star_counts(cs)) == [2, 40, 45]


# ── End-to-end: indexer writes the night_sky row ──────────────────────────────

def _session(tmp_path: Path, blocks: list[tuple[str, int]],
             with_detections: bool = False) -> Path:
    d = tmp_path / "RO000M_20260623_180000_000000"
    d.mkdir()
    _calstars(d / "CALSTARS_RO000M_20260623_180000.txt", blocks)
    if with_detections:
        ftp = ("Meteor Count = 000001\n-----\nCam# Meteor# #Segments fps\n-----\n"
               f"{_ff(0)}\nRO000M 0001 0002 0025.00 0 0 0 0 0 0\n"
               "0025.0000 100 100 10 20 130 45 100 -2.00 0 10 0\n"
               "0050.0000 110 110 11 21 131 46 100 -1.50 0 10 0\n")
        (d / "FTPdetectinfo_RO000M_20260623_180000_000000.txt").write_text(ftp)
    return d


def _sky_row(con: sqlite3.Connection, cam: str, night: str) -> dict | None:
    con.row_factory = sqlite3.Row
    r = con.execute(
        "SELECT * FROM night_sky WHERE cam=? AND night=?", (cam, night)
    ).fetchone()
    return dict(r) if r else None


def test_indexer_writes_overcast_row_on_clouded_night(tmp_path, monkeypatch):
    # A clouded-out night: no detections, all CALSTARS frames star-starved.
    monkeypatch.setattr(detection_indexer, "DB_PATH", tmp_path / "idx.db")
    session = _session(tmp_path, [(_ff(i), 1) for i in range(10)])
    con = detection_indexer._open_db()
    n = detection_indexer._index_session(con, "RO000M", "20260623", session, {})
    assert n == 0  # no detections
    row = _sky_row(con, "RO000M", "20260623")
    assert row is not None
    assert row["cloud_fraction"] == 1.0
    assert row["n_ff"] == 10
    con.close()


def test_indexer_writes_clear_row_alongside_detections(tmp_path, monkeypatch):
    monkeypatch.setattr(detection_indexer, "DB_PATH", tmp_path / "idx.db")
    session = _session(tmp_path, [(_ff(i), 50) for i in range(8)],
                       with_detections=True)
    con = detection_indexer._open_db()
    n = detection_indexer._index_session(con, "RO000M", "20260623", session, {})
    assert n >= 1  # detections indexed too
    row = _sky_row(con, "RO000M", "20260623")
    assert row is not None
    assert row["cloud_fraction"] == 0.0
    assert row["median_stars"] == 50.0
    con.close()


def test_empty_rms_reader_still_writes_the_sky_row(tmp_path, monkeypatch):
    """A station whose RMS reader yields nothing must still get its row.

    Trusting that empty result skipped the night_sky upsert entirely, so the
    station lost the weather-vs-fault discriminator without any error anywhere.
    """
    monkeypatch.setattr(detection_indexer, "DB_PATH", tmp_path / "idx.db")
    _install_empty_rms_reader(monkeypatch)
    session = _session(tmp_path, [(_ff(i), 1) for i in range(6)])
    con = detection_indexer._open_db()
    detection_indexer._index_night_sky(con, "RO000M", "20260623", session, {})
    row = _sky_row(con, "RO000M", "20260623")
    assert row is not None, "empty RMS reader result must fall through to the text parse"
    assert row["n_ff"] == 6
    assert row["cloud_fraction"] == 1.0
    con.close()


def test_missing_calstars_skips_sky_row_but_indexing_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(detection_indexer, "DB_PATH", tmp_path / "idx.db")
    # Detections present, but NO CALSTARS file at all (older/partial night).
    d = tmp_path / "RO000M_20260623_180000_000000"
    d.mkdir()
    ftp = ("Meteor Count = 000001\n-----\nCam# Meteor# #Segments fps\n-----\n"
           f"{_ff(0)}\nRO000M 0001 0002 0025.00 0 0 0 0 0 0\n"
           "0025.0000 100 100 10 20 130 45 100 -2.00 0 10 0\n"
           "0050.0000 110 110 11 21 131 46 100 -1.50 0 10 0\n")
    (d / "FTPdetectinfo_RO000M_20260623_180000_000000.txt").write_text(ftp)
    con = detection_indexer._open_db()
    n = detection_indexer._index_session(con, "RO000M", "20260623", d, {})
    assert n >= 1  # indexing still succeeds
    assert _sky_row(con, "RO000M", "20260623") is None  # no sky row
    con.close()


def test_recompute_is_gated_on_calstars_mtime(tmp_path, monkeypatch):
    monkeypatch.setattr(detection_indexer, "DB_PATH", tmp_path / "idx.db")
    session = _session(tmp_path, [(_ff(i), 1) for i in range(4)])
    con = detection_indexer._open_db()
    detection_indexer._index_night_sky(con, "RO000M", "20260623", session, {})
    first = _sky_row(con, "RO000M", "20260623")
    # Second call with unchanged CALSTARS mtime must be a no-op (same row).
    detection_indexer._index_night_sky(con, "RO000M", "20260623", session, {})
    assert _sky_row(con, "RO000M", "20260623")["updated"] == first["updated"]
    con.close()
