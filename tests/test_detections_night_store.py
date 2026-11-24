"""Past-night Detections payloads: bounded parallel listing + on-disk store.

A night outside the archive index's hot window used to be listed one camera
at a time over sshfs (39 s for 50 cameras on 2026-09-26), past nginx's 30 s
proxy timeout. These tests pin the three parts of the fix: listings run a
few at a time, a finished build is kept on disk and served from there, and a
stale copy is served at once while it is rebuilt behind the request.
"""

from __future__ import annotations

import gzip
import json
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from flask import Flask

import night_payload_store as nps
import routes.detections as det
from models import CameraConfig, StationConfig
from night_payload_store import NightPayloadStore


# ── NightPayloadStore ────────────────────────────────────────────────────────

def test_store_round_trip_and_freshness(tmp_path):
    store = NightPayloadStore(tmp_path)
    assert store.save("20260905-1", {"date": "20260905"}, ttl_s=60, now=1000.0)
    got = store.load("20260905-1")
    assert got.payload == {"date": "20260905"}
    assert got.built_at == 1000.0 and got.fresh_until == 1060.0
    assert got.is_fresh(1059.0) and not got.is_fresh(1061.0)


def test_store_ignores_other_schemas_corrupt_files_and_bad_keys(tmp_path):
    store = NightPayloadStore(tmp_path)
    with gzip.open(tmp_path / "old-1.json.gz", "wt") as fh:
        json.dump({"schema": nps.SCHEMA + 1, "built_at": 0, "fresh_until": 9e9,
                   "payload": {}}, fh)
    assert store.load("old-1") is None
    (tmp_path / "bad-1.json.gz").write_bytes(b"not gzip")
    assert store.load("bad-1") is None
    assert store.load("missing-1") is None
    assert store.save("../escape", {}, 60) is False


def test_disabled_store_is_a_no_op():
    store = NightPayloadStore(None)
    assert not store.enabled
    assert store.save("20260905-1", {}, 60) is False
    assert store.load("20260905-1") is None
    assert store.try_claim("20260905-1") is True


def test_store_keeps_only_the_newest_files(tmp_path):
    store = NightPayloadStore(tmp_path, max_files=2)
    for i, key in enumerate(("a-1", "b-1", "c-1")):
        store.save(key, {"i": i}, 60)
        time.sleep(0.01)
    assert sorted(p.name for p in tmp_path.glob("*.json.gz")) == ["b-1.json.gz", "c-1.json.gz"]


def test_claims_are_exclusive_and_stale_ones_are_taken_over(tmp_path):
    store = NightPayloadStore(tmp_path)
    assert store.try_claim("20260905-1") is True
    assert store.try_claim("20260905-1") is False
    store.release("20260905-1")
    assert store.try_claim("20260905-1") is True
    # A claim left by a worker that died long ago does not block forever.
    assert store.try_claim("20260905-1", now=time.time() + nps.CLAIM_STALE_S + 1) is True


# ── rms_complete from the listings ───────────────────────────────────────────

class _NI:
    def __init__(self, rms_complete, state_chunks=None):
        self.rms_complete = rms_complete
        self.state_chunks = {} if state_chunks is None else state_chunks
        self.meteor_files = ()
        self.stack_files = frozenset()
        self.ffthumb_files = frozenset()


def _station(*codes):
    return StationConfig(ip="100.0.0.1", label="X", cameras=[
        CameraConfig(code=c, cam_ip="192.168.1.10") for c in codes])


def test_rms_complete_from_listings():
    from routes.detections import _rms_complete_from_listings as f
    st = _station("RO000A", "RO000B")
    assert f(st, {"RO000A": _NI(True), "RO000B": _NI(True)}) is True
    assert f(st, {"RO000A": _NI(True), "RO000B": _NI(False)}) is False
    # No state.json for a camera counts as complete, like _night_rms_complete.
    assert f(st, {"RO000A": _NI(True), "RO000B": None}) is True
    nostate = _NI(None)
    nostate.state_chunks = None
    assert f(st, {"RO000A": _NI(True), "RO000B": nostate}) is True
    # Unknown (listing restored without the field, or not listed at all).
    assert f(st, {"RO000A": _NI(True), "RO000B": _NI(None)}) is None
    assert f(st, {"RO000A": _NI(True)}) is None


# ── Route level ──────────────────────────────────────────────────────────────

class _Cache:
    def get_status(self, host_key):
        return {"online": False}


class _ArchiveIdx:
    ready = True

    def __init__(self, delay=0.0, unavailable=False):
        self.scan_unavailable = 0
        self.delay = delay
        self.unavailable = unavailable
        self.calls = []
        self.active = 0
        self.peak = 0
        self._lock = threading.Lock()

    def in_hot_window(self, date):
        return False

    def night_files(self, cam, date):
        with self._lock:
            self.calls.append(cam)
            self.active += 1
            self.peak = max(self.peak, self.active)
            if self.unavailable:
                self.scan_unavailable += 1
        time.sleep(self.delay)
        with self._lock:
            self.active -= 1
        return None


class _Config:
    def __init__(self, n_cams):
        self.correlation_window_s = 1.0
        self.stations = {
            "alpha": _station(*[f"RO{i:04d}" for i in range(n_cams)]),
        }


OLD_NIGHT = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y%m%d")


@pytest.fixture
def make_app(tmp_path, monkeypatch):
    import detection_db

    rms_calls = []
    monkeypatch.setattr(det, "_night_rms_complete",
                        lambda station, date: rms_calls.append(date) or True)
    monkeypatch.setattr(detection_db, "covers_dates", lambda dates: True)
    monkeypatch.setattr(detection_db, "query_detections", lambda dates: [])
    monkeypatch.setattr(det, "_archive_nights", lambda cam: [])
    monkeypatch.setattr(det, "_read_archive_locked_chunks", lambda cam, date: [])
    # Each registration replaces the module's deps; monkeypatch puts back
    # those of whichever app the suite built before.
    monkeypatch.setattr(det, "_DEPS", det._DEPS)

    def _make(archive_idx, n_cams=3, cache=None):
        app = Flask(__name__)
        cache = {} if cache is None else cache
        det.register_detections_routes(
            app, _Config(n_cams), None, _Cache(),
            archive_idx=archive_idx, detections_cache=cache, detections_ttl=1800.0,
            night_store=NightPayloadStore(tmp_path / "nights"),
        )
        app._cache = cache
        return app

    _make.rms_calls = rms_calls
    _make.store_dir = tmp_path / "nights"
    return _make


def test_listings_run_in_parallel_but_bounded(make_app, monkeypatch):
    monkeypatch.setattr(det, "_NIGHT_LIST_WORKERS", 3)
    monkeypatch.setattr(det, "_NIGHT_LIST_GATE", threading.BoundedSemaphore(3))
    idx = _ArchiveIdx(delay=0.05)
    make_app(idx, n_cams=9)
    t0 = time.monotonic()
    det.compute_detections_payload(OLD_NIGHT)
    elapsed = time.monotonic() - t0
    assert sorted(idx.calls) == [f"RO{i:04d}" for i in range(9)]   # each camera once
    assert idx.peak == 3                                            # never more than the bound
    assert elapsed < 9 * 0.05                                       # not one by one


def test_old_night_is_kept_on_disk_and_served_from_there(make_app):
    idx = _ArchiveIdx()
    make_app(idx)
    first = det.compute_detections_payload(OLD_NIGHT)
    assert (make_app.store_dir / f"{OLD_NIGHT}-1.json.gz").exists()
    listed = len(idx.calls)

    # A sibling worker (or this one after a restart) with an empty memory
    # cache gets the stored build without listing anything.
    make_app(idx)
    assert det.compute_detections_payload(OLD_NIGHT) == first
    assert len(idx.calls) == listed


def test_stale_copy_is_served_at_once_and_rebuilt_behind_it(make_app):
    idx = _ArchiveIdx()
    app = make_app(idx)
    stale = {"date": OLD_NIGHT, "by_station": {}, "events": [], "detections": [],
             "correlation_window_s": 1.0, "marker": "stale"}
    NightPayloadStore(make_app.store_dir).save(f"{OLD_NIGHT}-1", stale, ttl_s=0)

    served = det.compute_detections_payload(OLD_NIGHT)
    assert served.get("marker") == "stale"          # no wait for the rebuild

    deadline = time.time() + 5
    while time.time() < deadline:
        got = NightPayloadStore(make_app.store_dir).load(f"{OLD_NIGHT}-1")
        if got and "marker" not in got.payload:
            break
        time.sleep(0.02)
    assert "marker" not in got.payload and got.is_fresh()
    # The rebuild also replaced this worker's memory copy.
    assert "marker" not in app._cache[(OLD_NIGHT, True)][1]


def test_a_build_that_could_not_reach_the_box_is_not_kept(make_app):
    idx = _ArchiveIdx(unavailable=True)
    make_app(idx)
    det.compute_detections_payload(OLD_NIGHT)
    assert not (make_app.store_dir / f"{OLD_NIGHT}-1.json.gz").exists()


def test_recent_nights_are_not_stored(make_app):
    class _Hot(_ArchiveIdx):
        def in_hot_window(self, date):
            return True
    make_app(_Hot())
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
    det.compute_detections_payload(yday)
    assert not list(make_app.store_dir.glob("*.json.gz"))


def test_rms_complete_comes_from_the_listings_on_the_index_path(make_app):
    class _Listed(_ArchiveIdx):
        def night_files(self, cam, date):
            super().night_files(cam, date)
            return _NI(True)
    make_app.rms_calls.clear()
    make_app(_Listed())
    payload = det.compute_detections_payload(OLD_NIGHT)
    assert payload["by_station"]["alpha"]["rms_complete"] is True
    assert make_app.rms_calls == []      # no per-station state.json reads


def test_night_listing_records_rms_complete(tmp_path):
    from cache_store import _ArchiveIndex
    night = tmp_path / "RO000A" / "20260905"
    (night / "meteors").mkdir(parents=True)
    (night / "meteors" / "RO000A_20260905_201500_color.mkv").write_bytes(b"")
    state = night / "state.json"
    state.write_text(json.dumps({"rms_complete": True, "chunks": {}}))
    assert _ArchiveIndex._scan_night(str(night)).rms_complete is True
    state.write_text(json.dumps({"rms_complete": False, "chunks": {}}))
    assert _ArchiveIndex._scan_night(str(night)).rms_complete is False
    # Missing key reads as not complete, as _night_rms_complete does.
    state.write_text(json.dumps({"chunks": {}}))
    assert _ArchiveIndex._scan_night(str(night)).rms_complete is False
    state.unlink()
    ni = _ArchiveIndex._scan_night(str(night))
    assert ni.rms_complete is None and ni.state_chunks is None


def test_concurrent_builds_share_one_listing_bound(make_app, monkeypatch):
    """The range view builds several nights at once; together they must stay
    within the gate, or the sshfs guard sheds listings."""
    monkeypatch.setattr(det, "_NIGHT_LIST_WORKERS", 3)
    monkeypatch.setattr(det, "_NIGHT_LIST_GATE", threading.BoundedSemaphore(3))
    idx = _ArchiveIdx(delay=0.05)
    make_app(idx, n_cams=6)
    nights = [(datetime.now(timezone.utc) - timedelta(days=d)).strftime("%Y%m%d")
              for d in (20, 21, 22, 23)]
    threads = [threading.Thread(target=det.compute_detections_payload, args=(n, False))
               for n in nights]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(idx.calls) == 4 * 6
    assert idx.peak <= 3


# ── Per-worker caches under concurrent writers ───────────────────────────────
#
# Request threads write the same per-worker caches. A manual eviction that
# iterated the detections cache (and a sweep over the footage cache) raised
# "mutated during iteration" when another thread wrote meanwhile, and the
# range / highlights totals silently lost that night. BoundedCache caps both
# on insert, so nothing iterates them any more.


class _Hot(_ArchiveIdx):
    def in_hot_window(self, date):
        return True


def _nights(n, start=40):
    return [(datetime.now(timezone.utc) - timedelta(days=start + d)).strftime("%Y%m%d")
            for d in range(n)]


def test_detections_cache_survives_a_write_during_eviction(make_app):
    from cache_store import BoundedCache

    class _RacingCache(BoundedCache):
        """Lets a second thread write while anyone iterates the cache."""

        def items(self):
            it = iter(super().items())
            yield next(it)
            writer = threading.Thread(target=self.__setitem__,
                                      args=(("19990101", True), (0.0, {})))
            writer.start()
            writer.join()
            yield from it

    cache = _RacingCache(128)
    for night in _nights(101, start=400):
        cache[(night, True)] = (time.monotonic() + 600, {})
    make_app(_Hot(), cache=cache)
    payload = det.compute_detections_payload(OLD_NIGHT)
    assert payload["date"] == OLD_NIGHT
    assert cache[(OLD_NIGHT, True)][1] is payload
    assert len(cache) <= 128


def test_detections_cache_concurrent_writers(make_app):
    from cache_store import BoundedCache

    cache = BoundedCache(128)
    make_app(_Hot(), n_cams=1, cache=cache)
    errors: list[BaseException] = []
    nights = _nights(1200)

    def build(chunk):
        for night in chunk:
            try:
                det.compute_detections_payload(night)
            except BaseException as exc:  # noqa: BLE001 - collected for the assert
                errors.append(exc)

    saved = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=build, args=(nights[i::8],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(saved)
    assert errors == []
    assert len(cache) <= 128


def test_footage_cache_concurrent_writers(make_app, monkeypatch):
    import chunk_listings

    monkeypatch.setattr(chunk_listings, "get", lambda cam, date: None)
    app = make_app(_Hot(), n_cams=1)
    statuses: list[int] = []
    nights = _nights(600)

    def fetch(chunk):
        client = app.test_client()
        for night in chunk:
            statuses.append(client.get(f"/api/footage/grid/{night}").status_code)

    saved = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        threads = [threading.Thread(target=fetch, args=(nights[i::8],)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    finally:
        sys.setswitchinterval(saved)
    assert statuses and set(statuses) == {200}
