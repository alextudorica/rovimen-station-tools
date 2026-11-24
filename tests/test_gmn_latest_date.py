"""GMN publication lag: which night is the newest one upstream actually has.

GMN solves a night 4-15 days after it happened, so the orbits overlay used
to open on "yesterday" and paint "no orbits for this night" over five
perfectly normal nights in a row. The backend knows better — the monthly
traj_summary file it already caches ends at the last solved night — so it
now reports that night and marks anything newer as ``not_published_yet``,
which is a different message from "published, but none of our cameras were
on it".

These tests pin: the newest night is found across the month boundary, a
night with no RO witness still counts as published, a future-stamped row is
ignored, and ``events_for_date`` carries both fields.
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

import gmn_data  # noqa: E402

_TOTAL_COLUMNS = 86
_OUR_STATIONS = "RO000H,RO0015"
_FOREIGN_STATIONS = "US0008,SI0001"


def _row(ymd: str, idx: int, stations: str) -> str:
    """One traj_summary data row: id + begin time + participating stations.

    Every other column is left empty — the parser reads them through
    ``_maybe_float``, which maps blanks to ``None``.
    """
    cells = [""] * _TOTAL_COLUMNS
    cells[gmn_data._COL_ID] = f"{ymd}_{idx:05d}"
    cells[gmn_data._COL_TIME_UTC] = (
        f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} 01:02:03.000000"
    )
    cells[gmn_data._COL_STATIONS] = stations
    return ";".join(cells)


def _seed_cache(cache_dir: Path, nights: dict[str, str], now: date) -> None:
    """Write monthly cache files covering ``nights`` = {YYYYMMDD: stations}.

    Both the current and the previous month file are always created, even
    when empty: ``ensure_monthly_cached`` only goes to the network when the
    file is missing or stale, and these tests must never touch upstream.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    prev = now.replace(day=1) - timedelta(days=1)
    by_month: dict[str, list[str]] = {
        f"{now.year:04d}{now.month:02d}": [],
        f"{prev.year:04d}{prev.month:02d}": [],
    }
    for i, (ymd, stations) in enumerate(sorted(nights.items())):
        by_month.setdefault(ymd[:6], []).append(_row(ymd, i, stations))
    for month_key, rows in by_month.items():
        path = cache_dir / f"traj_summary_monthly_{month_key}.txt"
        body = "# Trajectory summary\n" + "".join(r + "\n" for r in rows)
        path.write_text(body, encoding="utf-8")


@pytest.fixture
def gmn_cache(tmp_path, monkeypatch):
    """Point gmn_data at an isolated cache dir with all memo caches cleared."""
    cache_dir = tmp_path / "gmn_cache"
    cache_dir.mkdir()
    monkeypatch.setattr(gmn_data, "CACHE_DIR", cache_dir)
    gmn_data._month_extent_cache.clear()
    gmn_data._daily_count_cache.clear()
    gmn_data._our_daily_count_cache.clear()
    gmn_data._traj_index_cache.clear()

    def _no_network(*a, **kw):
        raise AssertionError("test hit the network")

    monkeypatch.setattr(gmn_data.requests, "get", _no_network)
    return cache_dir


def _today() -> date:
    return datetime.now(timezone.utc).date()


def _ymd(days_ago: int) -> str:
    return (_today() - timedelta(days=days_ago)).strftime("%Y%m%d")


def _iso(days_ago: int) -> str:
    return (_today() - timedelta(days=days_ago)).isoformat()


def test_latest_available_date_is_the_newest_solved_night(gmn_cache):
    _seed_cache(
        gmn_cache,
        {_ymd(5): _OUR_STATIONS, _ymd(9): _OUR_STATIONS},
        _today(),
    )
    assert gmn_data.latest_available_date() == _iso(5)


def test_latest_available_date_spans_the_month_boundary(gmn_cache):
    # The last night of the previous month is exactly one month file back on
    # every calendar day, so this only passes if the previous month's file is
    # consulted too. A fixed days-back offset would drift two months back
    # early in the month, past the current+previous window the production
    # code deliberately scans.
    prev_last = _today().replace(day=1) - timedelta(days=1)
    _seed_cache(
        gmn_cache, {prev_last.strftime("%Y%m%d"): _OUR_STATIONS}, _today()
    )
    assert gmn_data.latest_available_date() == prev_last.isoformat()


def test_a_night_without_our_cameras_still_counts_as_published(gmn_cache):
    """The newest night is about GMN's batch, not about our fleet.

    If this used the RO-only tally, a published night nobody of ours saw
    would masquerade as "not published yet" — exactly the distinction the
    banner is trying to draw.
    """
    _seed_cache(
        gmn_cache,
        {_ymd(4): _FOREIGN_STATIONS, _ymd(8): _OUR_STATIONS},
        _today(),
    )
    assert gmn_data.latest_available_date() == _iso(4)


def test_future_stamped_rows_are_ignored(gmn_cache):
    _seed_cache(
        gmn_cache,
        {_ymd(5): _OUR_STATIONS, _ymd(-3): _OUR_STATIONS},
        _today(),
    )
    assert gmn_data.latest_available_date() == _iso(5)


def test_latest_available_date_is_none_on_a_cold_cache(gmn_cache):
    assert gmn_data.latest_available_date() is None


def test_latest_available_date_is_none_when_the_file_holds_no_rows(gmn_cache):
    _seed_cache(gmn_cache, {}, _today())
    assert gmn_data.latest_available_date() is None


def test_events_for_date_flags_a_night_upstream_has_not_solved(gmn_cache):
    _seed_cache(gmn_cache, {_ymd(5): _OUR_STATIONS}, _today())
    payload = gmn_data.events_for_date(_iso(1))
    assert payload["events"] == []
    assert payload["not_published_yet"] is True
    assert payload["latest_available_date"] == _iso(5)
    assert payload.get("error") is None


def test_events_for_date_on_the_newest_night_returns_our_orbits(gmn_cache):
    _seed_cache(gmn_cache, {_ymd(5): _OUR_STATIONS}, _today())
    payload = gmn_data.events_for_date(_iso(5))
    assert payload["has_ro_count"] == 1
    assert payload["not_published_yet"] is False
    assert payload["latest_available_date"] == _iso(5)


def test_a_published_night_with_no_ro_witness_is_not_flagged_unpublished(gmn_cache):
    """The red "try another date" case: solved upstream, none of ours on it."""
    _seed_cache(
        gmn_cache,
        {_ymd(5): _OUR_STATIONS, _ymd(6): _FOREIGN_STATIONS},
        _today(),
    )
    payload = gmn_data.events_for_date(_iso(6))
    assert payload["events"] == []
    assert payload["not_published_yet"] is False


def test_an_invalid_date_still_carries_the_publication_fields(gmn_cache):
    _seed_cache(gmn_cache, {_ymd(5): _OUR_STATIONS}, _today())
    payload = gmn_data.events_for_date("not-a-date")
    assert payload["error"]
    assert payload["not_published_yet"] is False
    assert payload["latest_available_date"] == _iso(5)


def test_the_legacy_payload_fields_are_unchanged(gmn_cache):
    """Contract guard: consumers (public API, janitor, highlights) read these."""
    _seed_cache(gmn_cache, {_ymd(5): _OUR_STATIONS}, _today())
    payload = gmn_data.events_for_date(_iso(5))
    for key in ("date", "total_upstream", "events", "has_ro_count",
                "de_only_count", "last_fetched_utc"):
        assert key in payload, f"missing legacy field {key}"
