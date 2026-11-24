"""Tests for station_api.cached_swr — stale-while-revalidate TTL cache.

/api/status is served through cached_swr so a slow collect() (du walks can
take >20 s on a busy capture disk) never blocks a caller once a value
exists. The pusher fetches the endpoint with a finite timeout to build its
status heartbeat: a blocked refresh used to become a missed heartbeat and a
false "offline" on the dashboard (gmn0004, NTFS-3g capture disk under
night-capture load).
"""

from __future__ import annotations

import threading
import time

import station_api


def _prime(key: str, val, age: float = 0.0) -> None:
    """Insert a cache entry aged `age` seconds into the past."""
    with station_api._cache_lock:
        station_api._cache[key] = {"val": val, "ts": time.monotonic() - age}


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def _cached_val(key: str):
    with station_api._cache_lock:
        entry = station_api._cache.get(key)
    return entry["val"] if entry else None


def test_fresh_value_served_without_calling_fn():
    key = "test-swr-fresh"
    _prime(key, "fresh")
    calls = []
    assert station_api.cached_swr(key, 60, lambda: calls.append(1) or "new") == "fresh"
    assert not calls


def test_cold_start_computes_inline():
    key = "test-swr-cold"
    assert station_api.cached_swr(key, 60, lambda: "computed") == "computed"
    assert _cached_val(key) == "computed"


def test_expired_value_served_stale_while_slow_refresh_runs():
    key = "test-swr-stale"
    _prime(key, "stale", age=120)
    release = threading.Event()

    def slow_fn():
        release.wait(timeout=5)
        return "refreshed"

    t0 = time.monotonic()
    assert station_api.cached_swr(key, 60, slow_fn) == "stale"
    # The caller must return immediately, not wait out the slow refresh.
    assert time.monotonic() - t0 < 1.0
    release.set()
    assert _wait_for(lambda: _cached_val(key) == "refreshed")


def test_expired_single_flight_refresh():
    key = "test-swr-singleflight"
    _prime(key, "stale", age=120)
    release = threading.Event()
    calls = []

    def slow_fn():
        calls.append(1)
        release.wait(timeout=5)
        return "refreshed"

    for _ in range(5):
        assert station_api.cached_swr(key, 60, slow_fn) == "stale"
    release.set()
    assert _wait_for(lambda: _cached_val(key) == "refreshed")
    assert len(calls) == 1


def test_failed_refresh_keeps_stale_and_allows_retry():
    key = "test-swr-fail"
    _prime(key, "stale", age=120)

    def boom():
        raise RuntimeError("du exploded")

    assert station_api.cached_swr(key, 60, boom) == "stale"
    # Failed refresh must clear the inflight marker so a later call retries.
    assert _wait_for(
        lambda: key not in station_api._cache_inflight
    ), "inflight marker leaked after failed refresh"
    assert station_api.cached_swr(key, 60, lambda: "recovered") == "stale"
    assert _wait_for(lambda: _cached_val(key) == "recovered")
