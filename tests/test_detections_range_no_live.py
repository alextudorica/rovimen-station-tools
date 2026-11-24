"""The Sort & Filter range view must never fan out to live stations.

``/api/detections/range`` aggregates a multi-night window; a single cold
request used to fan out per-camera over HTTP to ~19 stations for the current
night (``allow_live`` defaulting to True), which pushed the route past the
gunicorn timeout and produced the recurring 504. The route now passes
``allow_live=False`` — matching ``routes/highlights.py`` — so even a range
that includes today is served from the SQLite index / archive, with zero
per-camera station calls.

This drives the real ``create_app`` + Flask test client (same harness as
test_shortclip_archive_fallback.py) and asserts the live fan-out call
(``station_get_raw``) is never reached for a range covering today.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_range_"))

    users_path = tmp / "users.yaml"
    users_path.write_text(yaml.safe_dump({
        "admin": {
            "display_name": "Admin",
            "password_hash": "",
            "role": "admin",
            "require_totp": False,
        },
    }))

    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststation": {
                "ip": "127.0.0.1",
                "label": "Test Station",
                "ssh_user": "test",
                "public": True,
                "lat": 44.4,
                "lon": 26.1,
                "cameras": [
                    {"code": "RO0017", "cam_ip": "127.0.0.1",
                     "label": "N", "rotate": False},
                ],
            },
        },
    }))

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "range-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_SHOWER_COUNTS_PATH"] = str(tmp / "shower_counts.json")

    import security

    _saved_threshold = security._LOGIN_THRESHOLD
    _saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    _saved_init_limiter = security.init_limiter
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = _saved_init_limiter(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter

    from rovimen_dashboard import load_config, create_app

    config = load_config(config_path)
    app = create_app(config, config_path)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        security._LOGIN_THRESHOLD = _saved_threshold
        security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
        security.init_limiter = _saved_init_limiter


@pytest.fixture
def admin(app_env):
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


def test_range_over_today_never_fans_out_to_live_stations(admin, monkeypatch):
    """A range covering today (where allow_live would normally fan out) must
    not call the live per-camera station endpoint. The station is marked
    online so ``allow_live=True`` WOULD reach station_get_raw; proving it is
    never called shows the range route forces ``allow_live=False``."""
    import routes.detections as det_routes
    import station_client

    # Force the station "online" so allow_live=True would take the live path.
    monkeypatch.setattr(
        station_client.StationCache, "get_status",
        lambda self, hk: {"online": True},
    )

    calls: list[str] = []

    def _boom(config, tunnels, host_key, path, timeout=None, **kw):
        calls.append(path)
        raise AssertionError(f"live fan-out reached for {path}")

    monkeypatch.setattr(det_routes, "station_get_raw", _boom)

    # The per-station rms_complete SSHFS pass is skipped for the aggregate
    # view — record any call so we can assert it never runs on the range path.
    rms_calls: list[str] = []

    def _record_rms(station, date):
        rms_calls.append(getattr(station, "label", "?"))
        return True

    monkeypatch.setattr(det_routes, "_night_rms_complete", _record_rms)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    yday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")

    r = admin.get(f"/api/detections/range?from={yday}&to={today}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["from"] == yday and body["to"] == today
    # The whole point: not a single live station call for the range view.
    assert calls == [], f"range view fanned out to live stations: {calls}"
    # And no per-station rms_complete SSHFS pass (the residual per-date cost).
    assert rms_calls == [], f"range view ran the rms_complete SSHFS pass: {rms_calls}"


def test_single_night_today_makes_no_station_call(admin, monkeypatch):
    """The single-night /api/detections/<today> route keeps allow_live=True
    (the Process-night flag) but no longer fans out to the stations (#1250):
    tonight comes from the pushed detection rows and locks, even with the
    station online."""
    import routes.detections as det_routes
    import station_client

    monkeypatch.setattr(
        station_client.StationCache, "get_status",
        lambda self, hk: {"online": True},
    )

    calls: list[str] = []

    def _record(config, tunnels, host_key, path, timeout=None, **kw):
        calls.append(path)
        return {}

    monkeypatch.setattr(det_routes, "station_get_raw", _record)

    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    r = admin.get(f"/api/detections/{today}")
    assert r.status_code == 200
    assert calls == [], f"single-night today view called the station: {calls}"
    # It still computes the per-station rms_complete flag (Process-night
    # button), which the aggregate view skips: from the night listings the
    # index path already holds, or from state.json when they cannot say.
    assert "rms_complete" in r.get_json()["by_station"]["teststation"]
