"""Sky-dome routes live at module level (routes/sky_dome.py).

The handlers, helpers and pre-render scheduler used to be closures inside
``register_sky_dome_routes``, so no helper could be imported or unit-tested and
the scheduler was handed to ``start_dome_scheduler`` through a function
attribute. These tests pin what the move must not change (URL rules, endpoint
names, methods, the auth and CSRF treatment of every view) and the new
contract: helpers work without an app, ``register_sky_dome_routes`` stores the
dependencies, and ``start_dome_scheduler`` runs the module-level scheduler with
them.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import auth
import security

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import anon, app_env  # noqa: E402,F401  (fixtures)

import routes.sky_dome as sky_dome  # noqa: E402

_GET = ["GET", "HEAD", "OPTIONS"]

# Captured from origin/main 65cc35db, while these views were closures.
EXPECTED_ENDPOINTS = [
    {"rule": "/api/latest_chunk/<host_key>/<cam>", "endpoint": "api_latest_chunk",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/latest_ff_maxpixel/<host_key>/<cam>", "endpoint": "api_latest_ff_maxpixel",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/latest_frame/<host_key>/<cam>", "endpoint": "api_latest_frame",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/sky_dome/<host_key>.png", "endpoint": "api_sky_dome",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/sky_dome_meta/<host_key>", "endpoint": "api_sky_dome_meta",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/sky_dome_timelapse/<host_key>/<date>.mp4",
     "endpoint": "api_sky_dome_timelapse_mp4",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/sky_dome_timelapse/<host_key>/dates",
     "endpoint": "api_sky_dome_timelapse_dates",
     "methods": _GET, "public": False, "page": None, "require_station": True,
     "csrf_exempt": True},
    {"rule": "/api/twilight/<host_key>/<date>", "endpoint": "api_twilight",
     "methods": _GET, "public": True, "page": None, "require_station": False,
     "csrf_exempt": True},
    {"rule": "/api/twilight/network/<date>", "endpoint": "api_twilight_network",
     "methods": _GET, "public": True, "page": None, "require_station": False,
     "csrf_exempt": True},
]

# One concrete URL per endpoint, for the request-level gate checks.
SAMPLE_URL = {
    "api_latest_chunk": "/api/latest_chunk/pub_station/PUB001",
    "api_latest_ff_maxpixel": "/api/latest_ff_maxpixel/pub_station/PUB001",
    "api_latest_frame": "/api/latest_frame/pub_station/PUB001",
    "api_sky_dome": "/api/sky_dome/pub_station.png",
    "api_sky_dome_meta": "/api/sky_dome_meta/pub_station",
    "api_sky_dome_timelapse_mp4": "/api/sky_dome_timelapse/pub_station/20261101.mp4",
    "api_sky_dome_timelapse_dates": "/api/sky_dome_timelapse/pub_station/dates",
    "api_twilight": "/api/twilight/pub_station/20260101",
    "api_twilight_network": "/api/twilight/network/20260101",
}


def _sky_dome_endpoints(app: Flask) -> list[dict]:
    station_guard = auth.require_station(lambda **k: None).__code__
    csrf = app.extensions["csrf"]
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        view = app.view_functions[rule.endpoint]
        if view.__module__ != "routes.sky_dome":
            continue
        out.append({
            "rule": rule.rule,
            "endpoint": rule.endpoint,
            "methods": sorted(rule.methods),
            "public": bool(getattr(view, security._PUBLIC_ROUTE_ATTR, False)),
            "page": getattr(view, security._PUBLIC_PAGE_ATTR, None),
            "require_station": view.__code__ is station_guard,
            "csrf_exempt": f"{view.__module__}.{view.__name__}" in csrf._exempt_views,
        })
    return out


def test_endpoints_keep_their_rules_methods_auth_and_csrf(app_env):  # noqa: F811
    assert _sky_dome_endpoints(app_env) == EXPECTED_ENDPOINTS


@pytest.mark.parametrize("row", EXPECTED_ENDPOINTS, ids=lambda r: r["endpoint"])
def test_the_auth_gate_sees_the_same_public_views(app_env, anon, row):  # noqa: F811
    url = SAMPLE_URL[row["endpoint"]]
    with app_env.test_request_context(url):
        matched = security._matched_public_view(app_env)
    if row["public"]:
        assert matched is app_env.view_functions[row["endpoint"]]
        assert anon.get(url).status_code == 200
    else:
        assert matched is None
        assert anon.get(url).status_code == 401


def test_helpers_work_without_an_app():
    assert sky_dome._sky_dome_cache_path("gmnro07", "ff_max", overlay=True) == (
        sky_dome.THUMB_CACHE_DIR / "gmnro07" / "sky_dome_ff_max_overlay.png")
    assert sky_dome._dome_capture_night(
        datetime(2026, 11, 22, 3, 0, tzinfo=timezone.utc)) == "20261121"
    assert sky_dome._dome_capture_night(
        datetime(2026, 11, 22, 13, 0, tzinfo=timezone.utc)) == "20261122"


def _register_bare(monkeypatch, stations: dict) -> tuple[Flask, SimpleNamespace]:
    # monkeypatch restores the deps of whichever app the suite built before.
    monkeypatch.setattr(sky_dome, "_DEPS", sky_dome._DEPS)
    app = Flask(__name__)
    config = SimpleNamespace(stations=stations)
    sky_dome.register_sky_dome_routes(
        app, config, None, None,
        platepar_cache={}, get_timelapses_payload=lambda host_key: {},
    )
    return app, config


def test_each_registration_replaces_the_deps(monkeypatch):
    app1, config1 = _register_bare(monkeypatch, {})
    deps1 = sky_dome._deps()
    app2, config2 = _register_bare(monkeypatch, {})
    deps2 = sky_dome._deps()
    assert deps1.config is config1 and deps2.config is config2
    assert app1._live_chunk_warmer is deps1.live_warmer
    assert app2._live_chunk_warmer is deps2.live_warmer
    assert deps1.live_warmer is not deps2.live_warmer


def test_deps_before_registration_is_an_error(monkeypatch):
    monkeypatch.setattr(sky_dome, "_DEPS", None)
    with pytest.raises(RuntimeError):
        sky_dome._deps()
    with pytest.raises(RuntimeError):
        sky_dome.start_dome_scheduler()


def test_the_scheduler_runs_with_the_registered_deps(monkeypatch):
    _register_bare(monkeypatch, {})
    got: list = []
    monkeypatch.setattr(sky_dome, "_sky_dome_scheduler", lambda deps: got.append(deps))
    t = sky_dome.start_dome_scheduler()
    t.join(timeout=5)
    assert got == [sky_dome._deps()]
    assert t.name == "sky-dome-scheduler" and t.daemon
    assert not hasattr(sky_dome.register_sky_dome_routes, "_scheduler")
    assert isinstance(t, threading.Thread)
