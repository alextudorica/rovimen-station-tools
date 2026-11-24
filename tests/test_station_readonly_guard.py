"""Tests for the station API network read-only guard (station_api.py).

The guard is fail-closed and unconditional: a request that is not
GET/HEAD/OPTIONS runs only from loopback, unless its route is on
``_NETWORK_ALLOWED_ENDPOINTS``. Remote state changes are otherwise gone (the
command channel was removed 2026-07); interventions are SSH-only and local
scripts still reach the API from loopback.

Invariants under test:
  * every mutating route in the URL map is either allowlisted or answers a
    non-loopback caller with 403 station_readonly (audit over app.url_map)
  * the allowlist names only routes that exist and are mutating
  * ROVIMEN_STATION_READONLY no longer matters: unset or "0" cannot weaken it
  * loopback callers of a blocked route are NOT blocked
  * read (GET) routes are never blocked, from any address
  * one-way clip lock: POST /api/lock with locked=true passes from the
    network (preserves data), locked=false is 403 (deletes archive copy)
  * process_clip, encode_chunk, probe, overlay_preview, overlay/logo and the
    calibration routes pass from the network (see the allowlist comment in station_api)
"""

from __future__ import annotations

import pytest

import station_api

REMOTE = {"REMOTE_ADDR": "100.64.0.9"}
READONLY = {"error": "station_readonly", "hint": "station is read-only; use SSH"}

_SAMPLE_ARGS = {
    "cam": "RO000H",
    "cam_code": "RO000H",
    "date": "20260101",
    "filename": "RO000H_20260101_000000_color.mkv",
    "service": "color-capture",
}


@pytest.fixture
def client(monkeypatch, station_patch):
    station_api.app.testing = True
    station_patch("load_config", lambda: {}, raising=False)
    return station_api.app.test_client()


def _mutating_rules():
    for rule in station_api.app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        methods = rule.methods - station_api._SAFE_METHODS
        for method in sorted(methods):
            yield rule, method


def _url(rule) -> str:
    adapter = station_api.app.url_map.bind("localhost")
    return adapter.build(rule.endpoint, {a: _SAMPLE_ARGS[a] for a in rule.arguments},
                         method=None)


# ── audit over the whole URL map ─────────────────────────────────────────────


def test_every_mutating_route_is_blocked_or_allowlisted(client, monkeypatch):
    # Neutralise side effects in case a route ever slipped through the guard.
    monkeypatch.setattr(station_api.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(station_api.subprocess, "run", lambda *a, **k: None)
    blocked = []
    for rule, method in _mutating_rules():
        if rule.endpoint in station_api._NETWORK_ALLOWED_ENDPOINTS:
            continue
        resp = client.open(_url(rule), method=method, environ_overrides=REMOTE)
        assert resp.status_code == 403, (rule.rule, method)
        assert resp.get_json() == READONLY
        blocked.append(rule.endpoint)
    assert "api_settings_patch" in blocked


def test_allowlist_names_only_existing_mutating_routes():
    mutating = {rule.endpoint for rule, _ in _mutating_rules()}
    assert station_api._NETWORK_ALLOWED_ENDPOINTS <= mutating


def test_allowlist_is_exactly_the_live_network_callers():
    # A change here must be deliberate: each entry is a dashboard feature that
    # mutates the station over the network (see the station_api comment).
    assert station_api._NETWORK_ALLOWED_ENDPOINTS == {
        "api_lock",
        "api_process_clip",
        "api_encode_chunk",
        "api_probe",
        "api_overlay_preview",
        "api_calibration_autofit",
        "api_calibration_rebase_pointing",
        "api_calibration_autofit_accept",
        "api_mask_write",
        "api_overlay_logo",
    }


# ── the old opt-in variable cannot weaken the guard ─────────────────────────


@pytest.mark.parametrize("value", [None, "0", "", "false"])
def test_settings_patch_blocked_from_remote_whatever_the_env(client, monkeypatch, value):
    if value is None:
        monkeypatch.delenv("ROVIMEN_STATION_READONLY", raising=False)
    else:
        monkeypatch.setenv("ROVIMEN_STATION_READONLY", value)
    resp = client.patch("/api/settings", json={"x": 1}, environ_overrides=REMOTE)
    assert resp.status_code == 403
    assert resp.get_json() == READONLY


@pytest.mark.parametrize("loopback", ["127.0.0.1", "::1", "::ffff:127.0.0.1"])
def test_blocked_route_allowed_from_loopback(client, loopback):
    # Bad body -> handler aborts 400; the point is it is NOT the guard's 403.
    resp = client.patch("/api/settings", json=[], environ_overrides={"REMOTE_ADDR": loopback})
    assert resp.status_code == 400


# ── one-way clip lock ────────────────────────────────────────────────────────

_LOCK_PATH = "/api/lock/RO000H/20260101/RO000H_20260101_000000_color.mkv"


def test_lock_true_allowed_from_remote(client):
    # Guard lets locked=true through; the handler then 404s (no such file on
    # the test box), proving the request reached the handler, not the guard.
    resp = client.post(_LOCK_PATH, json={"locked": True}, environ_overrides=REMOTE)
    assert resp.status_code == 404


def test_lock_empty_body_defaults_to_lock_and_is_allowed(client):
    # The handler treats a missing "locked" as true; the guard mirrors that
    # default or a bodyless lock request would be spuriously blocked.
    resp = client.post(_LOCK_PATH, environ_overrides=REMOTE)
    assert resp.status_code == 404


def test_unlock_blocked_from_remote(client):
    resp = client.post(_LOCK_PATH, json={"locked": False}, environ_overrides=REMOTE)
    assert resp.status_code == 403
    assert resp.get_json() == READONLY


def test_unlock_allowed_from_loopback(client):
    resp = client.post(_LOCK_PATH, json={"locked": False},
                       environ_overrides={"REMOTE_ADDR": "127.0.0.1"})
    assert resp.status_code == 404


# ── allowlisted routes reach their handlers from the network ────────────────


def test_process_clip_allowed_from_remote(client):
    resp = client.post(
        "/api/process_clip/RO000H/20260101/RO000H_20260101_000000_color.mkv",
        environ_overrides=REMOTE,
    )
    assert resp.status_code == 404


def test_probe_allowed_from_remote(client, monkeypatch, tmp_path, station_patch):
    station_patch("CONFIG_PATH", tmp_path / "config.json")
    resp = client.post("/api/probe", environ_overrides=REMOTE)
    # 404 from the handler: no hardware_assessment.sh on the test box.
    assert resp.status_code == 404
    assert resp.get_json().get("error") != "station_readonly"


@pytest.mark.parametrize("path", ["/api/overlay_preview", "/api/encode_chunk"])
def test_diagnostic_posts_allowed_from_remote(client, path, monkeypatch):
    # A 4xx from the handler (bad body / missing font) is fine; the point is
    # it is NOT the guard's 403. The config's nonexistent font makes
    # overlay_preview bail before it would shell out to ffmpeg (the body
    # cannot name a font).
    monkeypatch.setattr(station_api, "load_config",
                        lambda: {"overlay": {"font": "/nonexistent/font.ttf"}})
    resp = client.post(path, json={"font": "/nonexistent/font.ttf"},
                       environ_overrides=REMOTE)
    assert resp.status_code == 400
    assert (resp.get_json(silent=True) or {}).get("error") != "station_readonly"


# ── reads are never blocked ──────────────────────────────────────────────────


def test_get_settings_never_blocked(client):
    resp = client.get("/api/settings", environ_overrides=REMOTE)
    assert resp.status_code == 200


def test_unknown_url_is_a_plain_404_not_a_guard_403(client):
    resp = client.post("/api/no-such-route", environ_overrides=REMOTE)
    assert resp.status_code == 404


# ── unit-level helpers ──────────────────────────────────────────────────────


def test_is_loopback():
    assert station_api._is_loopback("127.0.0.1")
    assert station_api._is_loopback("::1")
    assert not station_api._is_loopback("100.64.0.9")
    assert not station_api._is_loopback(None)
