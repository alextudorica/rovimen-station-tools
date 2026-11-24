"""Per-station GMN orbit counts behind the overview map hover."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "dashboard"))

import station_orbits  # noqa: E402


def _row(traj_id: str, stations: str) -> str:
    # Only the ID (first column) and station list (last column) matter.
    return ";".join([traj_id] + ["x"] * 84 + [stations]) + "\n"


def test_reduce_lines_keeps_only_our_cameras_per_month() -> None:
    lines = [
        "# header\n",
        _row("20240105123456_abc", "RO000E, RO000G, US0001"),
        _row("20240106123456_abd", "RO000G,RO000E"),
        _row("20240203123456_abe", "US0001,CA0002"),          # none of ours
        _row("20240204123456_abf", "DE001E,XX0009,MD0001"),   # DE001E via extra codes
        _row("20240205123456_abg", "ZZ0001,AB1234"),          # registry-only code
    ]
    out = station_orbits.reduce_lines(lines, frozenset({"ZZ0001"}))
    assert out == {
        "202401": {"RO000E,RO000G": 2},
        "202402": {"DE001E,MD0001": 1, "ZZ0001": 1},
    }


def test_aggregate_counts_each_orbit_once_per_station() -> None:
    cam_to_station = {"RO000E": "gmnro10", "RO000G": "gmnro10", "RO0003": "gmnro05"}
    history = {"202312": {"RO000E,RO000G": 5, "RO0003": 2}}
    window = {
        "20261001": {"RO000E,RO000G": 3, "RO000E,RO0003": 1},
        "20260901": {"RO0003": 4, "RO9999": 7},  # RO9999: not in the registry
    }
    out = station_orbits.aggregate(cam_to_station, history, window, cutoff_30d="20260920")
    assert out["gmnro10"] == {"orbits_30d": 4, "orbits_all": 9, "first_month": "202312"}
    assert out["gmnro05"] == {"orbits_30d": 1, "orbits_all": 7, "first_month": "202312"}
    assert set(out) == {"gmnro10", "gmnro05"}


def test_history_never_streams_when_disabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(station_orbits.gmn_data, "CACHE_DIR", tmp_path)
    monkeypatch.setenv("ROVIMEN_STATION_ORBITS_HISTORY", "0")

    def boom(*a, **k):
        raise AssertionError("must not download")

    monkeypatch.setattr(station_orbits, "_fetch_year", boom)
    assert station_orbits.ensure_history([2023], frozenset())["years"] == {}


def test_history_is_built_once_and_persisted(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(station_orbits.gmn_data, "CACHE_DIR", tmp_path)
    monkeypatch.delenv("ROVIMEN_STATION_ORBITS_HISTORY", raising=False)
    calls: list[int] = []

    def fake_fetch(year: int, extra: frozenset[str]) -> dict:
        calls.append(year)
        return {f"{year}01": {"RO000E": year - 2000}}

    monkeypatch.setattr(station_orbits, "_fetch_year", fake_fetch)
    data = station_orbits.ensure_history([2022, 2023], frozenset())
    assert data["years"]["2023"] == {"202301": {"RO000E": 23}}
    station_orbits.ensure_history([2022, 2023], frozenset())
    assert calls == [2022, 2023]  # second call read the file
