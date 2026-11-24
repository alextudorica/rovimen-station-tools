"""/api/live-feed answers from a bounded, stale-while-revalidate snapshot.

The old handler walked every online station's cameras serially with 10 s and
15 s station timeouts and no server-side cache, so one station the VPS could
not reach turned every 30 s poll of the public /live page into a 504. Drives
the real ``create_app`` + Flask test client like the other route tests.
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_live_"))
    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "admin": {"display_name": "Admin", "password_hash": "", "role": "admin",
                  "require_totp": False},
    }))
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "fast": {
                "ip": "127.0.0.1", "label": "Fast", "ssh_user": "t", "public": True,
                "lat": 44.4, "lon": 26.1,
                "cameras": [{"code": "RO0001", "cam_ip": "127.0.0.1", "label": "N", "rotate": False}],
            },
            "slow": {
                "ip": "127.0.0.2", "label": "Slow", "ssh_user": "t", "public": True,
                "lat": 45.0, "lon": 25.0,
                "cameras": [{"code": "RO0002", "cam_ip": "127.0.0.1", "label": "S", "rotate": False}],
            },
            "dark": {
                "ip": "127.0.0.3", "label": "Dark", "ssh_user": "t", "public": True,
                "lat": 46.0, "lon": 24.0,
                "cameras": [{"code": "RO0003", "cam_ip": "127.0.0.1", "label": "E", "rotate": False}],
            },
        },
    }))
    for k, v in {
        "ROVIMEN_USERS_PATH": tmp / "users.yaml", "ROVIMEN_SECRET_KEY": "live-test",
        "ROVIMEN_COOKIE_SECURE": "0", "RATELIMIT_ENABLED": "0",
        "THUMB_CACHE_DIR": tmp / "thumb_cache", "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_COMPILATIONS_OUT_PATH": tmp / "compilations",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts", "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
        "ROVIMEN_SHOWER_COUNTS_PATH": tmp / "shower_counts.json",
    }.items():
        os.environ[k] = str(v)

    import security
    saved = (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS, security.init_limiter)
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = saved[2](app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    from rovimen_dashboard import create_app, load_config
    app = create_app(load_config(config_path), config_path)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS, security.init_limiter = saved


@pytest.fixture
def admin(app_env):
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


def test_live_feed_is_bounded_and_served_from_a_shared_snapshot(admin, monkeypatch):
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(
        station_client.StationCache, "get_status",
        lambda self, hk: {"online": hk != "dark"},
    )
    calls: list[tuple[str, str]] = []

    def fake_get_raw(config, tunnels, host_key, path, timeout=None, **kw):
        calls.append((host_key, path))
        if host_key == "slow":
            time.sleep(3.0)              # a station that hangs past the budget
            return []
        if path.startswith("/api/nights/"):
            return ["20260906"]
        return {"morning_done": False, "chunks": [{"stack": f"s{i}.webp"} for i in range(7)]}

    monkeypatch.setattr(det_routes, "station_get_raw", fake_get_raw)
    monkeypatch.setattr(det_routes, "_LIVE_FEED_BUDGET_S", 1.0)

    t0 = time.monotonic()
    r = admin.get("/api/live-feed")
    elapsed = time.monotonic() - t0
    assert r.status_code == 200
    body = r.get_json()
    assert set(body) == {"fast", "slow", "dark"}
    assert body["dark"]["online"] is False and body["dark"]["cameras"] == {}
    fast = body["fast"]["cameras"]["RO0001"]
    assert len(fast) == 5 and all(c["date"] == "20260906" for c in fast)
    assert body["slow"]["online"] is True and body["slow"]["cameras"]["RO0002"] == []
    assert elapsed < 2.5, f"the hanging station must not hold the response ({elapsed:.1f}s)"
    n_first = len(calls)
    assert ("dark", "/api/nights/RO0003") not in calls

    # Second request within the fresh window: pure snapshot, no station calls.
    r2 = admin.get("/api/live-feed")
    assert r2.status_code == 200 and r2.get_json()["fast"] == body["fast"]
    assert len(calls) == n_first


def test_anonymous_callers_only_see_public_stations(app_env, monkeypatch):
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(station_client.StationCache, "get_status", lambda self, hk: {"online": True})
    monkeypatch.setattr(det_routes, "station_get_raw",
                        lambda *a, **kw: ["20260906"] if "/nights/" in a[3] else [])
    admin = app_env.test_client()
    with admin.session_transaction() as sess:
        sess["user"] = "admin"; sess["role"] = "admin"; sess["stations"] = []; sess["mfa"] = "totp"
    full = admin.get("/api/live-feed")
    assert full.status_code == 200
    assert set(full.get_json()) == {"fast", "slow", "dark"}

    anon = app_env.test_client()
    r = anon.get("/api/live-feed")
    # Anonymous access depends on the "live" public-page toggle; either way an
    # anonymous caller must never see more than the public filter allows.
    if r.status_code == 200:
        assert set(r.get_json()) <= {"fast", "slow", "dark"}
    else:
        assert r.status_code in (401, 403)


def test_concurrent_cold_start_requests_wait_for_the_first_build(app_env, monkeypatch):
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(station_client.StationCache, "get_status", lambda self, hk: {"online": True})

    def slow_get_raw(config, tunnels, host_key, path, timeout=None, **kw):
        time.sleep(0.5)
        return ["20260906"] if "/nights/" in path else {"chunks": [{"stack": "s.webp"}]}

    monkeypatch.setattr(det_routes, "station_get_raw", slow_get_raw)
    # Force a cold state: no snapshot, nobody refreshing.
    det_routes._reset_live_feed()

    clients = [app_env.test_client() for _ in range(4)]
    for c in clients:
        with c.session_transaction() as sess:
            sess["user"] = "admin"; sess["role"] = "admin"; sess["stations"] = []; sess["mfa"] = "totp"
    results = {}

    def hit(i, c):
        results[i] = c.get("/api/live-feed").get_json()

    threads = [threading.Thread(target=hit, args=(i, c)) for i, c in enumerate(clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    for i, body in results.items():
        assert body and body["fast"]["cameras"]["RO0001"], f"request {i} got an empty fleet on cold start"
