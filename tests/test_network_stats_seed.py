"""Tests for the on-disk seed in dashboard/network_stats.py.

The seed lets a recycled gunicorn worker inherit a prior worker's coverage
results from disk instead of recomputing the whole-fleet point-in-polygon
math on boot (the recycle boot-storm that produced multi-minute 504 windows).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

import network_stats as ns


def _platepars() -> dict[str, dict]:
    return {
        "RO000A": {"lat": 45.0, "lon": 25.0, "az_centre": 180.0,
                   "alt_centre": 45.0, "fov_h": 88.0, "fov_v": 47.0},
        "RO000B": {"lat": 45.1, "lon": 25.1, "az_centre": 90.0,
                   "alt_centre": 45.0, "fov_h": 88.0, "fov_v": 47.0},
    }


def _config() -> dict:
    return {"stations": {"gmnro01": {"label": "Test", "cameras": [
        {"code": "RO000A"}, {"code": "RO000B"}]}}}


@pytest.fixture(autouse=True)
def _reset_caches():
    """Module-level caches persist across calls — reset around every test."""
    ns._cache = None
    ns._computing = False
    yield
    ns._cache = None
    ns._computing = False


def test_seed_disabled_without_env(monkeypatch, tmp_path):
    monkeypatch.delenv(ns._SEED_DIR_ENV, raising=False)
    assert ns._seed_dir() is None
    result = ns.get_network_stats(_platepars(), _config())
    assert result["network_summary"]["total_cameras"] == 2
    # Nothing written anywhere.
    assert not list(tmp_path.iterdir())


def test_network_stats_seed_roundtrip(monkeypatch, tmp_path):
    monkeypatch.setenv(ns._SEED_DIR_ENV, str(tmp_path))

    first = ns.get_network_stats(_platepars(), _config())
    seeds = list(tmp_path.glob("netstats-*.json"))
    assert len(seeds) == 1, "first call should compute and persist a seed"

    # A recycled worker: empty in-memory cache, but compute must NOT run.
    ns._cache = None
    with patch.object(ns, "compute_network_stats",
                      side_effect=AssertionError("recomputed instead of seeding")):
        second = ns.get_network_stats(_platepars(), _config())
    assert second == first


def test_precompute_writes_only_the_network_stats_seed(monkeypatch, tmp_path):
    """The background precompute warms /api/network-stats and nothing else:
    the coverage grid it also computed per altitude had no caller (the map
    draws coverage in the browser) and is gone."""
    import platepar_store

    monkeypatch.setenv(ns._SEED_DIR_ENV, str(tmp_path))
    monkeypatch.setattr(platepar_store, "get_all", _platepars)
    monkeypatch.setattr(ns, "_precompute_config", _config())
    ns._precompute_all()
    assert [p.name.split("-")[0] for p in tmp_path.iterdir()] == ["netstats"]


def test_seed_prunes_superseded_hash(monkeypatch, tmp_path):
    monkeypatch.setenv(ns._SEED_DIR_ENV, str(tmp_path))

    ns.get_network_stats(_platepars(), _config())
    first_seeds = {p.name for p in tmp_path.glob("netstats-*.json")}
    assert len(first_seeds) == 1

    # Recalibration: a different platepar set -> new hash -> old seed pruned.
    moved = _platepars()
    moved["RO000A"]["az_centre"] = 270.0
    ns._cache = None
    ns.get_network_stats(moved, _config())
    seeds = {p.name for p in tmp_path.glob("netstats-*.json")}
    assert len(seeds) == 1
    assert seeds != first_seeds
