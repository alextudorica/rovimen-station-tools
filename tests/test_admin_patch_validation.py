"""Admin PATCH edits are validated before they reach the registry or users.yaml.

The station and camera PATCH routes applied their bodies with Pydantic
``model_copy(update=)``, which does not validate, and the user PATCH route
assigned body values to the model's attributes, which does not validate
either. A body such as ``{"label": null}`` or ``{"alt": "abc"}`` was stored
in memory and written to ``dashboard_config.yaml`` (the admin-managed station
registry on the VPS), which then no longer loaded; a user edit such as
``{"display_name": null}`` made ``users.yaml`` skip that account, so it could
not sign in. These tests drive the real routes over real files: an invalid
body gets a 4xx and leaves the memory and the file as they were, and the body
shapes the admin UI sends still save.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from flask import Flask, abort

from cache_store import ConfigStore, load_config
from models import CameraConfig, DashboardConfig, StationConfig, UserConfig

HOST = "gmnro01"


def _seed_registry(path: Path) -> None:
    cfg = DashboardConfig(stations={
        HOST: StationConfig(
            ip="100.64.0.11", label="Ghirdoveni", ssh_user="gmn",
            jump_hosts=["gmnro02"], lat=44.61, lon=26.11,
            public_tabs=["timelapse"], location_name="Ghirdoveni, Dâmbovița",
            cameras=[
                CameraConfig(code="RO000A", cam_ip="192.168.42.13", label="N",
                             az=0.0, alt=45.0),
                CameraConfig(code="RO000B", cam_ip="192.168.42.14", rotate=True,
                             host="100.64.0.12", spectro=True),
            ],
        ),
        "gmnro02": StationConfig(ip="100.64.0.17", label="Vaslui"),
    })
    path.write_text(yaml.safe_dump(json.loads(cfg.model_dump_json()), allow_unicode=True))


class _Registry:
    """The admin network routes over a real ``dashboard_config.yaml``."""

    def __init__(self, path: Path) -> None:
        import admin_config

        self.path = path
        self.config = load_config(path)
        store = ConfigStore(self.config, path,
                            to_dict=lambda: json.loads(self.config.model_dump_json()))

        def require_station(host_key: str) -> StationConfig:
            if host_key not in self.config.stations:
                abort(404)
            return self.config.stations[host_key]

        app = Flask(__name__)
        app.secret_key = "test"
        admin_config.register_admin_network_routes(
            app, self.config,
            save_config=store.save,
            config_to_dict=lambda: json.loads(self.config.model_dump_json()),
            require_station=require_station,
            session_for_url=None,
            config_store=store,
        )
        self.client = app.test_client()
        with self.client.session_transaction() as sess:
            sess["user"] = "operator"
            sess["role"] = "admin"

    def station(self) -> StationConfig:
        return self.config.stations[HOST]

    def camera(self, code: str) -> CameraConfig:
        return next(c for c in self.station().cameras if c.code == code)

    def on_disk(self) -> DashboardConfig:
        return load_config(self.path)


@pytest.fixture
def registry(tmp_path: Path) -> _Registry:
    path = tmp_path / "dashboard_config.yaml"
    _seed_registry(path)
    return _Registry(path)


def _assert_rejected_and_untouched(reg: _Registry, resp, before_bytes: bytes,
                                   before_mtime: int, before: DashboardConfig,
                                   status: int = 422) -> None:
    assert resp.status_code == status, resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["ok"] is False
    assert body["error"]
    # The file is not rewritten at all, not just rewritten with equal content.
    assert reg.path.read_bytes() == before_bytes
    assert reg.path.stat().st_mtime_ns == before_mtime
    assert reg.on_disk() == before
    assert reg.config == before


def _snapshot(reg: _Registry) -> tuple[bytes, int, DashboardConfig]:
    return reg.path.read_bytes(), reg.path.stat().st_mtime_ns, reg.on_disk()


# ── Station PATCH ───────────────────────────────────────────────────────────

INVALID_STATION_BODIES = [
    {"label": None},
    {"ip": None},
    {"ssh_user": None},
    {"lat": "abc"},
    {"lon": [25.6]},
    {"jump_hosts": "gmnro02"},
    {"jump_hosts": [None]},
    {"public_tabs": None},
    {"proxy_media": "sometimes"},
    {"show_on_map": None},
    {"public": None},
    {"push_enabled": "maybe"},
    {"location_name": None},
    {"status": None},
    # Unknown, or a model field the route does not let an admin set.
    {"bogus": 1},
    {"cameras": []},
    {"ssh_host_key_fingerprints": ["SHA256:x"]},
    # A valid edit alongside an invalid one applies neither.
    {"label": "Ghirdoveni x86", "lat": "abc"},
    {"label": "Ghirdoveni x86", "bogus": 1},
]


@pytest.mark.parametrize("body", INVALID_STATION_BODIES, ids=lambda b: json.dumps(b))
def test_an_invalid_station_edit_is_rejected_and_nothing_is_saved(registry, body):
    before = _snapshot(registry)
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    _assert_rejected_and_untouched(registry, resp, *before)
    assert resp.get_json()["error"].startswith("Validation failed: ")


def test_the_station_edit_the_network_tab_sends_is_saved(registry):
    """``ncSave`` in dashboard-admin.js: every station field it shows, lat/lon
    only when the inputs hold a number."""
    body = {"label": "Ghirdoveni x86", "ip": "100.64.0.18", "ssh_user": "rmsuser",
            "proxy_media": True, "jump_hosts": ["gmnro02", "gmnro03"],
            "lat": 44.608, "lon": 25.282}
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True}
    for cfg in (registry.config, registry.on_disk()):
        st = cfg.stations[HOST]
        assert (st.label, st.ip, st.ssh_user, st.proxy_media) == (
            "Ghirdoveni x86", "100.64.0.18", "rmsuser", True)
        assert st.jump_hosts == ["gmnro02", "gmnro03"]
        assert (st.lat, st.lon) == (44.608, 25.282)
        # Fields the body leaves out keep their values: a partial update.
        assert st.public_tabs == ["timelapse"]
        assert st.location_name == "Ghirdoveni, Dâmbovița"
        assert [c.code for c in st.cameras] == ["RO000A", "RO000B"]
        assert st.cameras[1].host == "100.64.0.12"


def test_the_network_tab_edit_with_blank_coordinates_keeps_them(registry):
    body = {"label": "Ghirdoveni", "ip": "100.64.0.11", "ssh_user": "gmn",
            "proxy_media": False, "jump_hosts": []}
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    assert resp.status_code == 200
    st = registry.on_disk().stations[HOST]
    assert st.jump_hosts == []
    assert (st.lat, st.lon) == (44.61, 26.11)


def test_the_data_visibility_switch_edit_is_saved(registry):
    """``saveDataVisibility`` in dashboard-sysadmin.js."""
    body = {"public_tabs": ["timelapse", "videodb"]}
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    assert resp.status_code == 200
    assert registry.on_disk().stations[HOST].public_tabs == ["timelapse", "videodb"]
    assert registry.on_disk().stations[HOST].label == "Ghirdoveni"


def test_the_onboarding_edit_is_saved(registry):
    """Step 9 of the station onboarding runbook."""
    body = {"public": True, "status": "commissioning"}
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    assert resp.status_code == 200
    st = registry.on_disk().stations[HOST]
    assert (st.public, st.status) == (True, "commissioning")


def test_every_allowlisted_station_field_can_be_set(registry):
    body = {"ip": "100.64.0.18", "label": "G", "ssh_user": "rmsuser",
            "proxy_media": True, "jump_hosts": [], "lat": None, "lon": None,
            "show_on_map": False, "public_tabs": [], "public": False,
            "location_name": "G", "status": "commissioning", "push_enabled": True}
    resp = registry.client.patch(f"/api/admin/network-config/station/{HOST}", json=body)
    assert resp.status_code == 200
    st = registry.on_disk().stations[HOST]
    assert st.model_dump(include=set(body)) == body


def test_a_station_edit_for_an_unknown_station_is_404(registry):
    before = _snapshot(registry)
    resp = registry.client.patch("/api/admin/network-config/station/nope", json={"label": "x"})
    assert resp.status_code == 404
    assert registry.path.read_bytes() == before[0]


# ── Camera PATCH ────────────────────────────────────────────────────────────

INVALID_CAMERA_BODIES = [
    {"label": None},
    {"host": None},
    {"spectro": None},
    {"clips": None},
    {"rotate": None},
    {"rotate": "sideways"},
    {"alt": "abc"},
    {"az": [0]},
    {"cam_ip": None},
    {"cam_ip": "camera.local"},
    {"host": "not a host"},
    {"code": None},
    {"code": ""},
    {"code": "   "},
    # Unknown, or a model field the route does not let an admin set.
    {"bogus": 1},
    {"rtsp_url": "rtsp://x"},
    {"cam_ip": "192.168.42.20", "alt": "abc"},
    {"cam_ip": "192.168.42.20", "bogus": 1},
]


@pytest.mark.parametrize("body", INVALID_CAMERA_BODIES, ids=lambda b: json.dumps(b))
def test_an_invalid_camera_edit_is_rejected_and_nothing_is_saved(registry, body):
    before = _snapshot(registry)
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A", json=body)
    _assert_rejected_and_untouched(registry, resp, *before)
    assert resp.get_json()["error"].startswith("Validation failed: ")


def test_renaming_a_camera_onto_another_cameras_code_is_refused(registry):
    """Two cameras with one code make every per-camera lookup pick the first."""
    before = _snapshot(registry)
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A", json={"code": "RO000B"})
    _assert_rejected_and_untouched(registry, resp, *before, status=409)


def test_the_camera_edit_the_network_tab_sends_is_saved(registry):
    """``ncSave`` in dashboard-admin.js sends cam_ip, rotate, code and host
    for every existing camera row."""
    body = {"cam_ip": "192.168.42.20", "rotate": True, "code": "RO000A", "host": ""}
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A", json=body)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "station_push": None}
    for cfg in (registry.config, registry.on_disk()):
        a, b = cfg.stations[HOST].cameras
        assert (a.code, a.cam_ip, a.rotate, a.host) == ("RO000A", "192.168.42.20", True, "")
        # Fields the body leaves out keep their values: a partial update.
        assert (a.label, a.az, a.alt) == ("N", 0.0, 45.0)
        assert b == CameraConfig(code="RO000B", cam_ip="192.168.42.14", rotate=True,
                                 host="100.64.0.12", spectro=True)


def test_the_network_tab_renames_a_camera(registry):
    body = {"cam_ip": "192.168.42.13", "rotate": False, "code": "RO000C", "host": ""}
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A", json=body)
    assert resp.status_code == 200
    assert [c.code for c in registry.on_disk().stations[HOST].cameras] == ["RO000C", "RO000B"]


def test_every_allowlisted_camera_field_can_be_set(registry):
    body = {"cam_ip": "", "rotate": False, "az": 180.5, "alt": 30.0, "label": "S",
            "code": "RO000A", "host": "gmnro17-b", "clips": False, "spectro": True}
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A", json=body)
    assert resp.status_code == 200
    cam = registry.on_disk().stations[HOST].cameras[0]
    assert cam.model_dump(include=set(body)) == body


def test_a_camera_edit_clears_its_pointing_with_null(registry):
    """az and alt accept None ("not pointed"), so null is a valid edit."""
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A",
        json={"az": None, "alt": None})
    assert resp.status_code == 200
    cam = registry.on_disk().stations[HOST].cameras[0]
    assert (cam.az, cam.alt) == (None, None)


def test_a_camera_edit_stores_the_validated_value(registry):
    """Pydantic's lax coercion stores the number, not the string a form sent,
    and the IP validator strips it."""
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO000A",
        json={"alt": "30", "cam_ip": " 192.168.42.20 "})
    assert resp.status_code == 200
    cam = registry.camera("RO000A")
    assert (cam.alt, cam.cam_ip) == (30.0, "192.168.42.20")


def test_a_camera_edit_for_an_unknown_camera_is_404(registry):
    before = _snapshot(registry)
    resp = registry.client.patch(
        f"/api/admin/network-config/station/{HOST}/camera/RO9999", json={"label": "x"})
    assert resp.status_code == 404
    assert registry.path.read_bytes() == before[0]


# ── User PATCH ──────────────────────────────────────────────────────────────


@pytest.fixture
def users(tmp_path: Path, monkeypatch):
    """The admin user routes over a real ``users.yaml``."""
    import auth
    from admin_config import register_admin_user_routes

    path = tmp_path / "users.yaml"
    path.write_text(yaml.safe_dump({"users": {
        "operator": UserConfig(role="admin").model_dump(),
        "bob": UserConfig(role="host", display_name="Bob", stations=["gmnro02"],
                          session_epoch=3).model_dump(),
    }}))
    monkeypatch.setattr(auth, "USERS_PATH", path)
    monkeypatch.setattr(auth, "USERS_LOCK_PATH", path.with_suffix(".lock"))
    monkeypatch.setattr(auth, "_users_cache", None)

    app = Flask(__name__)
    app.secret_key = "test"
    register_admin_user_routes(app)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user"] = "operator"
        sess["role"] = "admin"
    return client, path


INVALID_USER_BODIES = [
    {"display_name": None},
    {"display_name": 5},
    {"stations": "gmnro02"},
    {"stations": [1]},
    {"expires_at": 5},
    {"bogus": 1},
    {"email": "bob@example.org"},
    {"display_name": "Bobby", "stations": "gmnro02"},
]


@pytest.mark.parametrize("body", INVALID_USER_BODIES, ids=lambda b: json.dumps(b))
def test_an_invalid_user_edit_is_rejected_and_nothing_is_saved(users, body):
    import auth

    client, path = users
    before = path.read_bytes()
    resp = client.patch("/api/admin/users/bob", json=body)
    assert resp.status_code == 400, resp.get_data(as_text=True)
    assert resp.get_json()["error"]
    assert path.read_bytes() == before
    bob = auth._load_users()["bob"]
    assert (bob.display_name, bob.stations, bob.session_epoch) == ("Bob", ["gmnro02"], 3)


def test_the_user_edit_the_users_tab_sends_is_saved(users):
    """``usersSave`` in dashboard-admin.js, edit mode."""
    import auth

    client, _ = users
    body = {"display_name": "Bobby", "role": "host", "stations": ["gmnro02", "gmnro03"],
            "expires_at": "2026-12-31T22:00:00.000Z", "password": "a-long-enough-pw-1"}
    resp = client.patch("/api/admin/users/bob", json=body)
    assert resp.status_code == 200
    bob = auth._load_users()["bob"]
    assert (bob.display_name, bob.role) == ("Bobby", "host")
    assert bob.stations == ["gmnro02", "gmnro03"]
    assert bob.expires_at == "2026-12-31T22:00:00.000Z"
    assert bob.password_hash
    assert bob.session_epoch == 4


def test_the_users_tab_clears_an_expiry_and_keeps_the_password(users):
    """Without a new password the tab leaves the key out; a cleared expiry
    field arrives as null."""
    import auth

    client, _ = users
    body = {"display_name": "Bob", "role": "host", "stations": ["gmnro02"], "expires_at": None}
    resp = client.patch("/api/admin/users/bob", json=body)
    assert resp.status_code == 200
    bob = auth._load_users()["bob"]
    assert bob.expires_at is None
    # Nothing privilege-relevant moved, so live sessions are not revalidated.
    assert bob.session_epoch == 3
