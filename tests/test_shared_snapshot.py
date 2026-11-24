"""Contract tests for the cross-worker snapshot cache.

Proves that:

* a store with no Redis client is a total no-op, so dev / tests / single-worker
  deployments keep the exact per-process behaviour they had before this tier;
* two stores sharing one Redis (i.e. two gunicorn workers) see each other's
  entries — the whole point: one build per fleet instead of one per worker;
* the remaining-TTL a reader is handed shrinks with the entry's age, so an
  adopting worker expires on the original build's schedule rather than
  extending it;
* an entry past its TTL reads as a miss;
* a payload that is not strictly JSON-encodable, or is over the size cap, is
  NOT shared — the alternative is an L2 hit whose shape differs from the L1
  hit it stands in for;
* every Redis fault degrades to a miss / no-op instead of raising into a
  request.
"""

from __future__ import annotations

import json

import pytest

from shared_snapshot import (
    SharedSnapshotStore,
    make_shared_snapshot_store,
)

fakeredis = pytest.importorskip("fakeredis")


def _client():
    return fakeredis.FakeStrictRedis(decode_responses=True)


def _store(client=None, **kwargs) -> SharedSnapshotStore:
    return SharedSnapshotStore(client, namespace="test", prefix="t", **kwargs)


class _BrokenRedis:
    """Every operation raises, like a Redis that has gone away mid-request."""

    def get(self, *a, **k):
        raise ConnectionError("redis is down")

    def set(self, *a, **k):
        raise ConnectionError("redis is down")

    def delete(self, *a, **k):
        raise ConnectionError("redis is down")


# ── Disabled store: the default everywhere Redis is not configured ────────

class TestDisabled:
    def test_get_always_misses(self):
        store = _store(None)
        store.set("k", {"a": 1}, 60)
        assert store.get("k") is None

    def test_reports_not_enabled(self):
        assert _store(None).enabled is False

    def test_invalidate_is_a_noop(self):
        _store(None).invalidate("k")  # must not raise

    def test_factory_without_redis_url_is_disabled(self, monkeypatch):
        monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
        assert make_shared_snapshot_store("detections").enabled is False

    def test_factory_with_unreachable_redis_misses_without_raising(self):
        # A dead Redis at boot must not crash a booting worker, and must not
        # latch the store off for the worker's life either: it keeps the
        # client, so it serves again once Redis recovers.
        store = make_shared_snapshot_store(
            "detections", redis_url="redis://127.0.0.1:1/0"
        )
        assert store.enabled is True
        store.set("k", {"a": 1}, 60)  # must not raise
        assert store.get("k") is None

    def test_factory_with_unbuildable_client_is_disabled(self):
        store = make_shared_snapshot_store("detections", redis_url="notredis://x")
        assert store.enabled is False


# ── The cross-worker property ─────────────────────────────────────────────

class TestSharedAcrossWorkers:
    def test_second_worker_sees_first_workers_entry(self):
        client = _client()
        worker_a, worker_b = _store(client), _store(client)
        payload = {"date": "20260908", "by_station": {"gmnro04": {"cameras": {}}}}

        worker_a.set("20260908:1", payload, 60)
        hit = worker_b.get("20260908:1")

        assert hit is not None
        got, age, remaining = hit
        assert got == payload
        assert age == pytest.approx(0, abs=2)
        assert remaining == pytest.approx(60, abs=2)

    def test_namespaces_do_not_collide(self):
        client = _client()
        detections = SharedSnapshotStore(client, namespace="detections", prefix="t")
        live_feed = SharedSnapshotStore(client, namespace="live_feed", prefix="t")

        detections.set("k", {"which": "detections"}, 60)
        live_feed.set("k", {"which": "live_feed"}, 60)

        assert detections.get("k")[0] == {"which": "detections"}
        assert live_feed.get("k")[0] == {"which": "live_feed"}

    def test_distinct_keys_are_independent(self):
        store = _store(_client())
        store.set("20260908:1", {"n": 1}, 60)
        store.set("20260909:1", {"n": 2}, 60)
        assert store.get("20260908:1")[0] == {"n": 1}
        assert store.get("20260909:1")[0] == {"n": 2}

    def test_miss_on_unknown_key(self):
        assert _store(_client()).get("never-written") is None


# ── TTL accounting: an adopter must not extend the original lifetime ──────

class TestTtl:
    def test_remaining_shrinks_with_age(self, monkeypatch):
        client = _client()
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr("shared_snapshot.time.time", lambda: clock["now"])

        store = _store(client)
        store.set("k", {"a": 1}, 100)

        clock["now"] += 40
        _payload, age, remaining = store.get("k")
        assert age == pytest.approx(40)
        assert remaining == pytest.approx(60)

    def test_entry_past_its_ttl_reads_as_a_miss(self, monkeypatch):
        client = _client()
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr("shared_snapshot.time.time", lambda: clock["now"])

        store = _store(client)
        store.set("k", {"a": 1}, 30)

        clock["now"] += 31
        assert store.get("k") is None

    def test_non_positive_ttl_is_not_written(self):
        store = _store(_client())
        store.set("k", {"a": 1}, 0)
        assert store.get("k") is None

    def test_ttl_below_the_publish_floor_is_not_shared(self):
        """A payload about to expire is not worth several MiB of Redis traffic
        and a re-decode in every worker. L1 still caches it."""
        store = _store(_client(), min_ttl_s=30)
        store.set("k", {"a": 1}, 10)
        assert store.get("k") is None

    def test_ttl_at_the_floor_is_shared(self):
        store = _store(_client(), min_ttl_s=30)
        store.set("k", {"a": 1}, 30)
        assert store.get("k") is not None

    def test_publish_floor_reads_the_env_at_construction(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_SHARED_SNAPSHOT_MIN_TTL", "600")
        store = _store(_client())
        store.set("k", {"a": 1}, 120)
        assert store.get("k") is None

    def test_publish_floor_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("ROVIMEN_SHARED_SNAPSHOT_MIN_TTL", "0")
        store = _store(_client())
        store.set("k", {"a": 1}, 5)
        assert store.get("k") is not None

    def test_backwards_clock_does_not_report_negative_age(self, monkeypatch):
        client = _client()
        clock = {"now": 1_000_000.0}
        monkeypatch.setattr("shared_snapshot.time.time", lambda: clock["now"])

        store = _store(client)
        store.set("k", {"a": 1}, 100)
        clock["now"] -= 10          # NTP step backwards
        _payload, age, _remaining = store.get("k")
        assert age >= 0


# ── What must never be shared ─────────────────────────────────────────────

class TestRefusedPayloads:
    def test_non_json_payload_is_not_shared(self, caplog):
        store = _store(_client())
        store.set("k", {"when": object()}, 60)   # must not raise
        assert store.get("k") is None

    def test_nan_is_not_shared(self):
        # allow_nan=False: NaN/Infinity are not valid JSON, and jsonify would
        # not have emitted them either.
        store = _store(_client())
        store.set("k", {"v": float("nan")}, 60)
        assert store.get("k") is None

    def test_oversized_payload_is_not_shared(self):
        store = _store(_client(), max_bytes=256)
        store.set("k", {"blob": "x" * 4096}, 60)
        assert store.get("k") is None

    def test_payload_just_under_the_cap_is_shared(self):
        store = _store(_client(), max_bytes=4096)
        store.set("k", {"blob": "x" * 64}, 60)
        assert store.get("k") is not None

    def test_unknown_envelope_version_reads_as_a_miss(self):
        client = _client()
        store = _store(client, version=1)
        store.set("k", {"a": 1}, 60)
        # A future worker's envelope, as a rolling deploy would produce.
        future = _store(client, version=99)
        assert future.get("k") is None

    def test_malformed_envelope_reads_as_a_miss(self):
        client = _client()
        store = _store(client)
        client.set("t:snapshot:test:k", "not json at all")
        assert store.get("k") is None


# ── Fail-soft under Redis faults ──────────────────────────────────────────

class TestFailSoft:
    def test_read_error_is_a_miss(self):
        assert _store(_BrokenRedis()).get("k") is None

    def test_write_error_does_not_raise(self):
        _store(_BrokenRedis()).set("k", {"a": 1}, 60)

    def test_invalidate_error_does_not_raise(self):
        _store(_BrokenRedis()).invalidate("k")

    def test_fault_is_logged_once_not_per_call(self, caplog):
        store = _store(_BrokenRedis())
        with caplog.at_level("WARNING"):
            for _ in range(25):
                store.get("k")
        assert len([r for r in caplog.records if "Redis read failed" in r.message]) == 1


# ── Round-trip fidelity ───────────────────────────────────────────────────

class TestRoundTrip:
    def test_json_native_payload_survives_unchanged(self):
        store = _store(_client())
        payload = {
            "date": "20260908",
            "correlation_window_s": 1.5,
            "by_station": {"gmnro04": {"label": "X", "cameras": {"RO000J": []}}},
            "events": [],
            "detections": [{"cam": "RO000J", "pending": False, "n": 3}],
        }
        store.set("k", payload, 60)
        assert store.get("k")[0] == payload

    def test_shared_shape_matches_a_plain_json_round_trip(self):
        # An L2 hit must be indistinguishable from what the HTTP layer would
        # have emitted from the L1 payload.
        store = _store(_client())
        payload = {"a": [1, 2, {"b": None, "c": True}]}
        store.set("k", payload, 60)
        assert store.get("k")[0] == json.loads(json.dumps(payload))

    def test_invalidate_removes_the_entry(self):
        store = _store(_client())
        store.set("k", {"a": 1}, 60)
        store.invalidate("k")
        assert store.get("k") is None
