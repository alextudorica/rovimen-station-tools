"""/api/status keeps refreshing on a failing disk, and says how old it is.

A capture disk that answers EIO made ``_scan_storage`` raise on its bare
``iterdir()`` of CapturedFiles/ArchivedFiles, so every status refresh failed
and the stale-while-revalidate cache served the last good status forever.
``cached()`` never set its in-flight event when the computation raised, so a
concurrent caller sat out ttl + 30 s (930 s for the storage scan). And the
heartbeat dropped ``last_updated``, so the VPS stamped each push with its
send time and a frozen status read as fresh.
"""

from __future__ import annotations

import errno
import threading
from pathlib import Path

import pytest

import rovimen_pusher
import station_collect


# ---------------------------------------------------------------------------
# _scan_storage
# ---------------------------------------------------------------------------

@pytest.fixture
def rms_tree(tmp_path, monkeypatch):
    cam = tmp_path / "RMS_data" / "RO000H"
    (cam / "CapturedFiles" / "20261120").mkdir(parents=True)
    (cam / "ArchivedFiles" / "20261119").mkdir(parents=True)
    monkeypatch.setattr(station_collect, "_rms_camera_dirs", lambda: [("RO000H", cam, "RO000H")])
    monkeypatch.setattr(station_collect, "capture_base", lambda: tmp_path / "no_colour_disk")
    monkeypatch.setattr(station_collect, "_du", lambda path: 0)
    monkeypatch.setattr(station_collect, "_get_device", lambda path: "/dev/test")
    return cam


def _eio_listing(monkeypatch, broken: Path) -> None:
    real = Path.iterdir

    def iterdir(self):
        if self == broken:
            raise OSError(errno.EIO, "Input/output error", str(self))
        return real(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)


def test_storage_scan_survives_an_unreadable_rms_dir(rms_tree, monkeypatch):
    _eio_listing(monkeypatch, rms_tree / "CapturedFiles")
    storage = station_collect._scan_storage()
    assert storage["rms"]["newest"] == "20261119"
    assert storage["rms"]["days"] == 1


def test_storage_scan_lists_both_rms_dirs(rms_tree):
    storage = station_collect._scan_storage()
    assert (storage["rms"]["oldest"], storage["rms"]["newest"]) == ("20261119", "20261120")


# ---------------------------------------------------------------------------
# cached()
# ---------------------------------------------------------------------------

def test_a_failed_computation_releases_its_waiters():
    key = "test:failing"
    seen: list[threading.Event] = []

    def boom():
        seen.append(station_collect._cache_inflight[key])
        raise OSError(errno.EIO, "Input/output error")

    with pytest.raises(OSError):
        station_collect.cached(key, 900, boom)
    assert seen and seen[0].is_set()
    assert key not in station_collect._cache_inflight


def test_a_waiter_recomputes_at_once_after_the_holder_fails(monkeypatch):
    key = "test:waiter"
    started, release, waiting = threading.Event(), threading.Event(), threading.Event()
    results: list[object] = []

    class _Tracked(threading.Event):
        def wait(self, timeout=None):
            waiting.set()
            return super().wait(timeout)

    monkeypatch.setattr(station_collect.threading, "Event", _Tracked)

    def slow_failure():
        started.set()
        release.wait(5)
        raise OSError(errno.EIO, "Input/output error")

    def holder():
        try:
            station_collect.cached(key, 900, slow_failure)
        except OSError:
            pass

    def waiter():
        results.append(station_collect.cached(key, 900, lambda: "fresh"))

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    assert started.wait(5)
    w = threading.Thread(target=waiter, daemon=True)
    w.start()
    assert waiting.wait(5)          # the waiter is blocked on the holder
    release.set()
    t.join(5)
    w.join(5)
    assert not w.is_alive(), "the waiter sat out ttl + 30 s"
    assert results == ["fresh"]
    station_collect._cache.pop(key, None)


def test_a_successful_computation_is_cached():
    key = "test:ok"
    calls: list[int] = []

    def fn():
        calls.append(1)
        return 42

    assert station_collect.cached(key, 900, fn) == 42
    assert station_collect.cached(key, 900, fn) == 42
    assert calls == [1]
    assert key not in station_collect._cache_inflight
    station_collect._cache.pop(key, None)


# ---------------------------------------------------------------------------
# Heartbeat
# ---------------------------------------------------------------------------

def test_heartbeat_carries_when_the_status_was_computed():
    p = rovimen_pusher.build_heartbeat_payload(
        {"online": True, "last_updated": "2026-11-21T22:00:00+00:00"})
    assert p["last_updated"] == "2026-11-21T22:00:00+00:00"


def test_heartbeat_from_a_status_without_a_stamp_leaves_it_to_the_vps():
    """The VPS stamps a heartbeat without one with its send time; a null
    would read as "never reported" (capture_state)."""
    assert "last_updated" not in rovimen_pusher.build_heartbeat_payload({})
