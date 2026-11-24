"""``GET /api/gmn/latest_date`` — what the orbits overlay seeds its picker from.

The route hands the frontend the newest night GMN has actually published so
the layer opens on a night with traces on it instead of on an empty
"yesterday". It reads the monthly cache already on disk, never upstream, and
sits behind the same login gate as the orbits endpoint it partners.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))


@pytest.fixture
def app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_gmnlatest_test_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "gmn-latest-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(tmp / "archive")
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    security.init_limiter = _noop_limiter

    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [{"code": "TST001", "label": "N", "cam_ip": "127.0.0.1",
                             "rotate": False, "az": 0.0, "alt": 45.0}],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Test Alpha",
                "show_on_map": True, "status": "active",
            },
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    from rovimen_dashboard import load_config, create_app
    config = load_config(cfg_path)
    flask_app = create_app(config, cfg_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


def test_route_reports_the_newest_published_night(client, monkeypatch):
    import gmn_data
    monkeypatch.setattr(gmn_data, "latest_available_date", lambda: "2026-08-20")
    r = client.get("/api/gmn/latest_date")
    assert r.status_code == 200
    assert r.get_json() == {"latest_available_date": "2026-08-20"}


def test_route_reports_null_on_a_cold_cache(client, monkeypatch):
    """A cold worker answers "don't know" — the frontend then keeps its own
    fallback night rather than being handed a wrong one."""
    import gmn_data
    monkeypatch.setattr(gmn_data, "latest_available_date", lambda: None)
    r = client.get("/api/gmn/latest_date")
    assert r.status_code == 200
    assert r.get_json() == {"latest_available_date": None}


def test_route_survives_a_broken_cache(client, monkeypatch):
    """A read failure must not 500 the overlay's first request."""
    import gmn_data

    def _boom():
        raise OSError("cache directory vanished")

    monkeypatch.setattr(gmn_data, "latest_available_date", _boom)
    r = client.get("/api/gmn/latest_date")
    assert r.status_code == 200
    assert r.get_json() == {"latest_available_date": None}


def test_route_needs_a_session_just_like_the_orbits_endpoint(app):
    anon = app.test_client()
    latest = anon.get("/api/gmn/latest_date")
    orbits = anon.get("/api/gmn/multistation/2026-08-20")
    assert latest.status_code != 200
    assert latest.status_code == orbits.status_code
