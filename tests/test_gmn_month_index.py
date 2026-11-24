"""The GMN monthly file is parsed once per VPS, not once per consumer per boot.

``gmn_data._month_index`` turns a cached ``traj_summary_monthly_*.txt`` into
every projection the dashboard serves (nights covered, per-night totals,
RO-witnessed counts, our-camera events) in one pass, keeps it in memory keyed
on the file's CONTENT, and persists it next to the file so sibling gunicorn
workers and recycled workers load JSON instead of re-walking 110 MB of text.
Before this, three workers recycling every four minutes spent most of the VPS
CPU re-parsing the same bytes (33 walks per worker boot from the poller alone).
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


def _row(ymd: str, idx: int, stations: str, hhmmss: str = "01:02:03") -> str:
    cells = [""] * _TOTAL_COLUMNS
    cells[gmn_data._COL_ID] = f"{ymd}{hhmmss.replace(':', '')}_{idx:05d}"
    cells[gmn_data._COL_TIME_UTC] = f"{ymd[:4]}-{ymd[4:6]}-{ymd[6:8]} {hhmmss}.000000"
    cells[gmn_data._COL_STATIONS] = stations
    cells[gmn_data._COL_PEAK_MAG] = "-2.5"
    return ";".join(cells)


def _write_month(cache_dir: Path, month_key: str, rows: list[str]) -> Path:
    path = cache_dir / f"traj_summary_monthly_{month_key}.txt"
    path.write_text("# Trajectory summary\n" + "".join(r + "\n" for r in rows), encoding="utf-8")
    return path


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    d = tmp_path / "gmn_cache"
    d.mkdir()
    monkeypatch.setattr(gmn_data, "CACHE_DIR", d)
    gmn_data._month_index_cache.clear()
    gmn_data._month_source_keys.clear()
    gmn_data._month_extent_cache.clear()
    gmn_data._daily_count_cache.clear()
    gmn_data._our_daily_count_cache.clear()
    gmn_data._traj_index_cache.clear()
    monkeypatch.setattr(
        gmn_data.requests, "get",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("test hit the network")),
    )
    return d


def _count_builds(monkeypatch) -> list[int]:
    calls: list[int] = []
    real = gmn_data._build_month_index

    def counting(path):
        calls.append(1)
        return real(path)

    monkeypatch.setattr(gmn_data, "_build_month_index", counting)
    return calls


def test_one_parse_serves_every_projection(cache_dir, monkeypatch):
    builds = _count_builds(monkeypatch)
    rows = [
        _row("20260810", 1, "RO000H,RO0015"),            # ours, RO
        _row("20260810", 2, "DE001B,DE0099"),            # ours, Berlin-only
        _row("20260810", 3, "US0008,SI0001"),            # foreign multi-station
        _row("20260811", 4, "MD0001,RO0003"),            # ours, RO + Cahul
        _row("20260811", 5, "MD0001,UA0001"),            # Cahul only: join yes, overlay no
        _row("20260812", 6, "US0008"),                   # single-station: not an orbit
        "20260813999999_bogus;short;row",                # malformed: still a published night
    ]
    _write_month(cache_dir, "202608", rows)

    idx = gmn_data._month_index(2026, 8)
    assert idx is not None
    assert idx["totals"] == {"20260810": 3, "20260811": 2}
    assert idx["ro_counts"] == {"20260810": 1, "20260811": 1}
    assert set(idx["all_dates"]) == {"20260810", "20260811", "20260812", "20260813"}
    assert [e["id"][-5:] for e in idx["events"]["20260810"]] == ["00001", "00002"]
    assert [e["id"][-5:] for e in idx["events"]["20260811"]] == ["00004", "00005"]

    # Every public projection is answered from that single pass.
    day = gmn_data.events_for_date("2026-08-10")
    assert day["total_upstream"] == 3
    assert [e["id"][-5:] for e in day["events"]] == ["00001", "00002"]
    assert day["has_ro_count"] == 1 and day["de_only_count"] == 1
    assert "has_de" not in day["events"][0]           # legacy event shape kept
    cahul = gmn_data.events_for_date("2026-08-11")
    assert [e["id"][-5:] for e in cahul["events"]] == ["00004"]   # MD-only stays off the overlay

    assert gmn_data._daily_counts_for_month(2026, 8) == {"20260810": 1, "20260811": 1}
    assert gmn_data.our_daily_counts_for_month(2026, 8, frozenset({"MD0001"})) == {"20260811": 2}
    assert gmn_data._upstream_dates_for_month(2026, 8) == {"20260810", "20260811", "20260812", "20260813"}
    assert [e["id"][-5:] for e in gmn_data.events_for_month(2026, 8)] == ["00001", "00002", "00004", "00005"]

    joined = gmn_data._traj_index_for_month(2026, 8)
    assert {k for k in joined} == {"RO000H", "RO0015", "DE001B", "DE0099", "MD0001", "RO0003", "UA0001"}
    assert gmn_data.match_trajectory("MD0001", "2026-08-11T01:02:04")["id"].endswith("00004") or \
        gmn_data.match_trajectory("MD0001", "2026-08-11T01:02:04")["id"].endswith("00005")

    assert len(builds) == 1, "every projection must come from ONE walk of the file"


def test_recycled_worker_loads_the_persisted_index_instead_of_parsing(cache_dir, monkeypatch):
    builds = _count_builds(monkeypatch)
    _write_month(cache_dir, "202608", [_row("20260810", 1, "RO000H,RO0015")])
    first = gmn_data.events_for_date("2026-08-10")
    assert len(builds) == 1
    index_file = gmn_data._index_path_for_month(2026, 8)
    assert index_file.exists()

    # A fresh process has an empty memory cache but the same disk.
    gmn_data._month_index_cache.clear()
    gmn_data._daily_count_cache.clear()
    again = gmn_data.events_for_date("2026-08-10")
    assert again["events"] == first["events"]
    assert again["total_upstream"] == first["total_upstream"]
    assert len(builds) == 1, "the persisted index must be reused, not rebuilt"


def test_a_304_mtime_bump_does_not_reparse_but_new_content_does(cache_dir, monkeypatch):
    builds = _count_builds(monkeypatch)
    path = _write_month(cache_dir, "202608", [_row("20260810", 1, "RO000H,RO0015")])
    assert gmn_data.events_for_date("2026-08-10")["total_upstream"] == 1
    assert len(builds) == 1

    # ensure_monthly_cached bumps mtime on a 304: same bytes, no re-parse.
    import os
    os.utime(path, (path.stat().st_atime + 100, path.stat().st_mtime + 100))
    assert gmn_data.events_for_date("2026-08-10")["total_upstream"] == 1
    assert len(builds) == 1

    # Upstream published more orbits: content changed, one re-parse.
    _write_month(cache_dir, "202608", [
        _row("20260810", 1, "RO000H,RO0015"),
        _row("20260810", 2, "RO0003,RO0004", "02:00:00"),
    ])
    os.utime(path, (path.stat().st_atime + 200, path.stat().st_mtime + 200))
    assert gmn_data.events_for_date("2026-08-10")["total_upstream"] == 2
    assert len(builds) == 2


def test_a_stale_persisted_index_is_ignored(cache_dir, monkeypatch):
    builds = _count_builds(monkeypatch)
    _write_month(cache_dir, "202608", [_row("20260810", 1, "RO000H,RO0015")])
    gmn_data.events_for_date("2026-08-10")
    index_file = gmn_data._index_path_for_month(2026, 8)
    # Simulate an index left behind by an older file (or an older format).
    index_file.write_text('{"format": 1, "source_key": "0:dead", "totals": {}, "events": {}, '
                          '"ro_counts": {}, "station_lists": {}, "all_dates": []}')
    gmn_data._month_index_cache.clear()
    assert gmn_data.events_for_date("2026-08-10")["total_upstream"] == 1
    assert len(builds) == 2


def test_the_in_memory_cache_is_bounded(cache_dir):
    for m in range(1, 7):
        _write_month(cache_dir, f"2026{m:02d}", [_row(f"2026{m:02d}10", m, "RO000H,RO0015")])
        assert gmn_data._daily_counts_for_month(2026, m) == {f"2026{m:02d}10": 1}
    assert len(gmn_data._month_index_cache) == gmn_data._MONTH_INDEX_KEEP
    # The small count maps survive eviction of the full index.
    assert gmn_data._daily_counts_for_month(2026, 1) == {"20260110": 1}


def test_a_missing_month_is_simply_absent(cache_dir):
    assert gmn_data._month_index(2025, 1) is None
    assert gmn_data._daily_counts_for_month(2025, 1) == {}
    assert gmn_data.events_for_month(2025, 1) == []
    assert gmn_data._traj_index_for_month(2025, 1) == {}


def test_lru_eviction_never_rehashes_an_unchanged_file(cache_dir, monkeypatch):
    """Orbit counting touches 13 months while the full index keeps 3: the
    evicted months' memos must answer from a stat, not a 110 MB hash or a
    reload of the multi-MB index file."""
    hashes = []
    real = gmn_data._source_key

    def counting(path):
        hashes.append(path.name)
        return real(path)

    monkeypatch.setattr(gmn_data, "_source_key", counting)
    months = range(1, gmn_data._TRAJ_INDEX_KEEP + 1)       # 6 > the 3-slot index LRU
    for m in months:
        _write_month(cache_dir, f"2026{m:02d}", [_row(f"2026{m:02d}10", m, "RO000H,RO0015")])
    for m in months:
        assert gmn_data._daily_counts_for_month(2026, m) == {f"2026{m:02d}10": 1}
        gmn_data._traj_index_for_month(2026, m)
        gmn_data._upstream_dates_for_month(2026, m)
    assert len(hashes) == len(months)          # one hash per month, ever
    assert len(gmn_data._month_index_cache) == gmn_data._MONTH_INDEX_KEEP
    hashes.clear()
    # A second full pass: every memo answers; nothing is hashed and the
    # evicted full indexes are not even reloaded from disk.
    loads = []
    real_load = gmn_data._load_index_file
    monkeypatch.setattr(gmn_data, "_load_index_file",
                        lambda *a, **k: loads.append(1) or real_load(*a, **k))
    for m in months:
        assert gmn_data._daily_counts_for_month(2026, m) == {f"2026{m:02d}10": 1}
        assert "RO000H" in gmn_data._traj_index_for_month(2026, m)
        assert gmn_data._upstream_dates_for_month(2026, m) == {f"2026{m:02d}10"}
    assert hashes == [] and loads == []
