"""Tests for routes.archive.resolve_rms_plot_status / resolve_ff_intervals_status
-- the Calibration page's archive-only RMS-plot resolver (PR #878 follow-up:
replaces a live per-camera station fan-out with a pure archive-index lookup)."""

from __future__ import annotations

from datetime import datetime, timezone

from routes.archive import resolve_ff_intervals_status, resolve_rms_plot_status


class _FakeArchiveIndex:
    """Minimal stand-in for cache_store._ArchiveIndex exposing only the
    methods the resolver calls."""

    def __init__(self, plots: dict[str, str] | None = None, all_nights: list[str] | None = None):
        self._plots = plots or {}          # date -> filename
        self._nights = all_nights or []    # newest-first

    def rms_plot_file(self, cam: str, date: str, suffix: str) -> str | None:
        filename = self._plots.get(date)
        return filename if filename and filename.endswith(suffix) else None

    def ff_intervals_file(self, cam: str, date: str) -> str | None:
        return self.rms_plot_file(cam, date, "_ff_intervals.png")

    def nights(self, cam: str) -> list[str]:
        return self._nights


# expected_date is ALWAYS now - 1 day, regardless of hour -- see
# resolve_rms_plot_status's docstring for why "today" is never correct here.
NOON_JUL30 = datetime(2026, 7, 30, 13, 0, tzinfo=timezone.utc)    # -> expected = 20260729
EVENING_JUL30 = datetime(2026, 7, 30, 20, 0, tzinfo=timezone.utc)  # -> expected = 20260729
MORNING_JUL30 = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)  # -> expected = 20260729


def test_expected_date_is_always_yesterday_regardless_of_hour():
    """The bug this guards: an earlier version used "today" once past 12:00
    UTC, so every camera showed no_data all afternoon/evening, every day --
    it was asking for tonight's still-capturing session instead of last
    night's finished one. expected_date must be now-1-day no matter the
    hour."""
    idx = _FakeArchiveIndex()
    for now in (MORNING_JUL30, NOON_JUL30, EVENING_JUL30):
        r = resolve_ff_intervals_status(idx, "CAM", now=now)
        assert r["expected_date"] == "20260729"


def test_expected_night_available_is_ok_not_overdue():
    idx = _FakeArchiveIndex(plots={"20260729": "CAM_20260729_..._ff_intervals.png"})
    r = resolve_ff_intervals_status(idx, "CAM", now=NOON_JUL30)
    assert r == {
        "status": "ok", "date": "20260729", "expected_date": "20260729",
        "filename": "CAM_20260729_..._ff_intervals.png", "overdue": False,
    }


def test_falls_back_to_newest_older_night_within_grace():
    # now=20260730 02:00 -> expected_date="20260729", nothing there yet, but
    # still within the 6h grace past its night-end (20260730 00:00 + 6h).
    early = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)
    idx = _FakeArchiveIndex(
        plots={"20260727": "CAM_20260727_..._ff_intervals.png"},
        all_nights=["20260729", "20260728", "20260727"],
    )
    r = resolve_ff_intervals_status(idx, "CAM", now=early)
    assert r["status"] == "ok"
    assert r["date"] == "20260727"
    assert r["expected_date"] == "20260729"
    assert r["overdue"] is False


def test_falls_back_and_flags_overdue_past_grace_window():
    # now=20260801 08:00 -> expected_date="20260731". Its night-end is
    # 20260801 00:00, +6h grace = 20260801 06:00 -- "now" is past that,
    # so the still-missing expected night is flagged even though an older
    # plot is available to fall back to.
    late = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    idx = _FakeArchiveIndex(
        plots={"20260729": "CAM_20260729_..._ff_intervals.png"},
        all_nights=["20260731", "20260730", "20260729"],
    )
    r = resolve_ff_intervals_status(idx, "CAM", now=late)
    assert r["status"] == "ok"
    assert r["date"] == "20260729"
    assert r["expected_date"] == "20260731"
    assert r["overdue"] is True


def test_no_data_at_all_reports_no_data_and_overdue_flag():
    idx = _FakeArchiveIndex()
    late = datetime(2026, 8, 1, 8, 0, tzinfo=timezone.utc)
    r = resolve_ff_intervals_status(idx, "CAM", now=late)
    assert r["status"] == "no_data"
    assert r["date"] is None
    assert r["filename"] is None
    assert r["overdue"] is True


def test_not_yet_overdue_within_grace_window():
    """Well before the expected night's grace deadline -- no plot yet is
    normal (dawn processing still running), not a signal to raise."""
    fresh = datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc)  # expected_date="20260729"
    idx = _FakeArchiveIndex(plots={}, all_nights=[])
    r = resolve_ff_intervals_status(idx, "CAM", now=fresh)
    assert r["status"] == "no_data"
    assert r["overdue"] is False


def test_future_dated_nights_are_never_returned():
    """A clock-skewed or bogus future-dated archive dir must not be surfaced
    as 'the latest plot' -- only dates <= expected_date are candidates."""
    idx = _FakeArchiveIndex(
        plots={"20260805": "CAM_20260805_..._ff_intervals.png"},
        all_nights=["20260805"],
    )
    r = resolve_ff_intervals_status(idx, "CAM", now=NOON_JUL30)
    assert r["status"] == "no_data"


def test_walk_is_capped_at_thirty_candidates():
    """Bounds the archive-local walk so a camera with years of history
    doesn't turn one request into hundreds of index lookups."""
    nights = [f"202601{str(d).zfill(2)}" for d in range(1, 40)][::-1]  # 39 nights, newest-first
    idx = _FakeArchiveIndex(plots={}, all_nights=nights)
    call_log = []
    orig = idx.rms_plot_file
    idx.rms_plot_file = lambda cam, date, suffix: call_log.append(date) or orig(cam, date, suffix)
    resolve_ff_intervals_status(idx, "CAM", now=NOON_JUL30)
    assert len(call_log) <= 31  # 1 for expected_date + up to 30 walked candidates


def test_generic_resolver_works_for_astrometry_residual_suffix():
    """resolve_rms_plot_status is shared by both the FF-intervals and
    astrometry-residual (calibration_variation.png) endpoints -- confirm it
    actually discriminates by suffix rather than only ever matching
    ff_intervals filenames."""
    idx = _FakeArchiveIndex(plots={
        "20260729": "CAM_20260729_..._calibration_variation.png",
    })
    r = resolve_rms_plot_status(idx, "CAM", "_calibration_variation.png", now=NOON_JUL30)
    assert r["status"] == "ok"
    assert r["filename"] == "CAM_20260729_..._calibration_variation.png"

    # A plot present for the wrong suffix must not match.
    idx2 = _FakeArchiveIndex(plots={"20260729": "CAM_20260729_..._ff_intervals.png"})
    r2 = resolve_rms_plot_status(idx2, "CAM", "_calibration_variation.png", now=NOON_JUL30)
    assert r2["status"] == "no_data"
