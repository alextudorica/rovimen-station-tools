"""The shared snapshot tier is actually wired into the two hot caches.

``test_shared_snapshot.py`` covers the store's own contract. This file covers
the wiring, which is where a mistake would really hide: it builds TWO apps
(standing in for two gunicorn workers) against ONE Redis and proves the second
one adopts the first one's work instead of repeating it.

That repetition was the measured cause of the 2026-09-09 "dashboard is slow
again" report: ten identical back-to-back calls to /api/detections/<date> on
prod produced exactly three 5.4-7.1 s responses (one per worker) and seven
~18 ms ones.
"""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest
import yaml

fakeredis = pytest.importorskip("fakeredis")


@pytest.fixture(scope="module")
def two_workers():
    """Two create_app() instances sharing one fake Redis, as two workers do."""
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_shared_"))
    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "admin": {"display_name": "Admin", "password_hash": "", "role": "admin",
                  "require_totp": False},
    }))
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "alpha": {
                "ip": "127.0.0.1", "label": "Alpha", "ssh_user": "t", "public": True,
                "lat": 44.4, "lon": 26.1,
                "cameras": [{"code": "RO0001", "cam_ip": "127.0.0.1", "label": "N",
                             "rotate": False}],
            },
            "beta": {
                "ip": "127.0.0.2", "label": "Beta", "ssh_user": "t", "public": True,
                "lat": 45.0, "lon": 25.0,
                "cameras": [{"code": "RO0002", "cam_ip": "127.0.0.1", "label": "S",
                             "rotate": False}],
            },
        },
    }))
    for k, v in {
        "ROVIMEN_USERS_PATH": tmp / "users.yaml", "ROVIMEN_SECRET_KEY": "shared-test",
        "ROVIMEN_COOKIE_SECURE": "0", "RATELIMIT_ENABLED": "0",
        "THUMB_CACHE_DIR": tmp / "thumb_cache", "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_COMPILATIONS_OUT_PATH": tmp / "compilations",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
        "ROVIMEN_SHOWER_COUNTS_PATH": tmp / "shower_counts.json",
        # Opts both apps into the shared tier. The URL is never dialled --
        # connect_redis is patched below to hand back one fake.
        "ROVIMEN_REDIS_URL": "redis://shared-test/0",
        # This fixture's archive index is cold, so the detections route hands
        # the store a 10 s TTL, which the publish floor would skip. Drop the
        # floor so the wiring under test is actually exercised.
        "ROVIMEN_SHARED_SNAPSHOT_MIN_TTL": "0",
    }.items():
        os.environ[k] = str(v)

    import security
    import shared_snapshot

    shared_client = fakeredis.FakeStrictRedis(decode_responses=True)
    saved_factory = shared_snapshot.connect_redis
    shared_snapshot.connect_redis = lambda url, purpose: shared_client

    saved = (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS,
             security.init_limiter)
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = saved[2](app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    import routes.detections as det_routes
    import routes.media as media_routes
    from rovimen_dashboard import create_app, load_config
    worker_a = create_app(load_config(config_path), config_path)
    worker_a.extensions[_DETECTIONS_DEPS] = det_routes._DEPS
    worker_a.extensions[_MEDIA_DEPS] = media_routes._DEPS
    worker_b = create_app(load_config(config_path), config_path)
    worker_b.extensions[_DETECTIONS_DEPS] = det_routes._DEPS
    worker_b.extensions[_MEDIA_DEPS] = media_routes._DEPS
    worker_a.config["TESTING"] = True
    worker_b.config["TESTING"] = True
    try:
        yield worker_a, worker_b, shared_client
    finally:
        shared_snapshot.connect_redis = saved_factory
        (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS,
         security.init_limiter) = saved
        os.environ.pop("ROVIMEN_REDIS_URL", None)


# The detections and media routes are module-level and serve the deps of the
# app registered last (routes/detections.py, routes/media.py). In a real
# deployment each worker is its own process; here both apps share one, so each
# test points the modules at the worker it is about to use, or worker B would
# silently reuse worker A's local caches and the adoption under test would
# pass vacuously.
_DETECTIONS_DEPS = "test_detections_deps"
_MEDIA_DEPS = "test_media_deps"


def _as_worker(app, monkeypatch):
    import routes.detections as det_routes
    import routes.media as media_routes
    monkeypatch.setattr(det_routes, "_DEPS", app.extensions[_DETECTIONS_DEPS])
    monkeypatch.setattr(media_routes, "_DEPS", app.extensions[_MEDIA_DEPS])
    return app


def _admin(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


def test_both_workers_opted_into_the_shared_tier(two_workers):
    """Guards the wiring itself: a disabled store would make the rest of this
    file pass vacuously (every adopt would just be a local cache hit)."""
    import shared_snapshot

    store = shared_snapshot.make_shared_snapshot_store("detections")
    assert store.enabled is True


def test_second_worker_adopts_the_live_feed_without_re_fanning_out(two_workers, monkeypatch):
    worker_a, worker_b, _client = two_workers
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(
        station_client.StationCache, "get_status", lambda self, hk: {"online": True},
    )
    calls: list[tuple[str, str]] = []

    def fake_get_raw(config, tunnels, host_key, path, timeout=None, **kw):
        calls.append((host_key, path))
        if path.startswith("/api/nights/"):
            return ["20260906"]
        return {"morning_done": False, "chunks": [{"stack": "s.webp"}]}

    monkeypatch.setattr(det_routes, "station_get_raw", fake_get_raw)
    _as_worker(worker_b, monkeypatch)
    det_routes._reset_live_feed()
    _as_worker(worker_a, monkeypatch)
    det_routes._reset_live_feed()

    first = _admin(worker_a).get("/api/live-feed")
    assert first.status_code == 200
    assert calls, "worker A was supposed to build the snapshot"
    after_a = len(calls)

    # Worker B has never built a snapshot. Before this tier it would have
    # repeated the whole fleet fan-out; now it adopts A's build.
    second = _admin(_as_worker(worker_b, monkeypatch)).get("/api/live-feed")
    assert second.status_code == 200
    assert second.get_json() == first.get_json()
    assert len(calls) == after_a, (
        "worker B re-contacted stations instead of adopting the shared snapshot"
    )


def test_stale_shared_snapshot_is_not_adopted(two_workers, monkeypatch):
    """Past FRESH, a worker must rebuild rather than serve a stale fleet."""
    worker_a, worker_b, _client = two_workers
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(
        station_client.StationCache, "get_status", lambda self, hk: {"online": True},
    )
    calls: list[tuple[str, str]] = []

    def fake_get_raw(config, tunnels, host_key, path, timeout=None, **kw):
        calls.append((host_key, path))
        if path.startswith("/api/nights/"):
            return ["20260906"]
        return {"morning_done": False, "chunks": [{"stack": "s.webp"}]}

    monkeypatch.setattr(det_routes, "station_get_raw", fake_get_raw)
    # FRESH of 0 means nothing shared can ever count as fresh.
    monkeypatch.setattr(det_routes, "_LIVE_FEED_FRESH_S", 0.0)
    _as_worker(worker_b, monkeypatch)
    det_routes._reset_live_feed()
    _as_worker(worker_a, monkeypatch)
    det_routes._reset_live_feed()

    assert _admin(worker_a).get("/api/live-feed").status_code == 200
    after_a = len(calls)
    assert _admin(_as_worker(worker_b, monkeypatch)).get("/api/live-feed").status_code == 200
    assert len(calls) > after_a, "a stale shared snapshot must not be adopted"


def test_detections_payload_is_published_and_adopted(two_workers, monkeypatch):
    import routes.detections as det_routes

    worker_a, worker_b, client = two_workers
    date = "20260906"

    _as_worker(worker_a, monkeypatch)
    payload_a = det_routes.compute_detections_payload(date)

    # Published under the (date, allow_live) key -- never the anon flag, since
    # the payload is always the full fleet and is filtered per request.
    assert client.get(f"rovimen:snapshot:detections:{date}:1") is not None

    # Worker B has an empty local cache; it must come back with A's build.
    _as_worker(worker_b, monkeypatch)
    assert (date, True) not in det_routes._deps().detections_cache
    payload_b = det_routes.compute_detections_payload(date)
    assert payload_b == payload_a


def test_a_shared_entry_past_its_ttl_is_not_adopted(two_workers, monkeypatch):
    """The adopter inherits the entry's REMAINING lifetime, so once the shared
    entry ages out nobody keeps serving it -- one worker rebuilds and
    republishes for the rest, instead of each sliding its own window forward.
    """
    worker_a, worker_b, client = two_workers
    import routes.detections as det_routes
    import shared_snapshot

    date = "20260905"
    clock = {"now": time.time()}
    monkeypatch.setattr(shared_snapshot.time, "time", lambda: clock["now"])

    _as_worker(worker_a, monkeypatch)
    det_routes.compute_detections_payload(date)
    key = f"rovimen:snapshot:detections:{date}:1"
    assert client.get(key) is not None

    clock["now"] += 1801          # past the 1800 s detections TTL
    store = shared_snapshot.make_shared_snapshot_store("detections")
    assert store.get(f"{date}:1") is None, "an aged-out entry must read as a miss"
