"""The admin config save writes every model field (P2 #16).

Every admin save rewrites the live ``dashboard_config.yaml`` on the VPS from
``config_file.config_to_dict``. It used to be a hand-written serializer
nested in ``create_app``, which no test could import, so the tests kept a
copy of it: a field the real serializer forgot passed the copy's test and was
dropped from the registry on the next save. The serializer is now the
models' own ``model_dump``; these tests call it, and the app's.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

from config_file import config_to_dict, load_config
from models import PUBLIC_PAGE_KEYS, CameraConfig, DashboardConfig, StationConfig

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import admin, app_env  # noqa: E402,F401  (fixtures)

REPO = Path(__file__).resolve().parents[1]
REPO_REGISTRIES = [
    REPO / "dashboard" / "dashboard_config.example.yaml",
    REPO / "tests" / "e2e" / "fixtures" / "dashboard_config.yaml",
]


def _save_and_reload(data: dict, model: type[DashboardConfig] = DashboardConfig):
    """What ``ConfigStore`` writes, read back the way ``load_config`` reads it."""
    text = yaml.dump(data, default_flow_style=False, allow_unicode=True)
    return model.model_validate(yaml.safe_load(text))


def _full_camera() -> CameraConfig:
    return CameraConfig(code="RO000A", cam_ip="192.168.1.10", rotate=True, clips=False,
                        label="NW", az=315.0, alt=25.0,
                        rtsp_url="rtsp://192.168.1.10:554/live", spectro=True,
                        host="100.64.0.12")


def _full_station() -> StationConfig:
    return StationConfig(
        ip="100.1.2.3", label="Ghirdoveni", ssh_user="rmsuser",
        cameras=[_full_camera()], jump_hosts=["100.64.0.7"],
        ssh_host_key_fingerprints=["SHA256:abc"], proxy_media=True,
        lat=44.8, lon=25.5, show_on_map=False, public_tabs=["archive", "videodb"],
        public=False, location_name="Ghirdoveni, Dâmbovița", status="commissioning",
        push_enabled=True,
    )


def test_every_field_of_a_fully_set_registry_is_written():
    cfg = DashboardConfig(station_api_port=7780, correlation_window_s=2,
                          stations={"gmnro01": _full_station()},
                          public_pages=["events", "live"])
    out = config_to_dict(cfg)
    assert out.keys() == DashboardConfig.model_fields.keys()
    assert out["stations"]["gmnro01"].keys() == StationConfig.model_fields.keys()
    cam = cfg.stations["gmnro01"].cameras[0]
    set_fields = {name for name, field in CameraConfig.model_fields.items()
                  if getattr(cam, name) != field.get_default(call_default_factory=True)}
    assert set_fields <= out["stations"]["gmnro01"]["cameras"][0].keys()
    assert _save_and_reload(out) == cfg


def test_written_registry_keeps_its_established_shape():
    """Station keys are written even at their defaults (only an empty host-key
    pin list is left out); a camera always carries code, cam_ip, rotate and
    label, and its other keys only when they differ from the default."""
    cfg = DashboardConfig(stations={
        "plain": StationConfig(ip="100.0.0.1", label="Plain", cameras=[
            CameraConfig(code="RO0001", cam_ip=""),
            CameraConfig(code="RO0002", cam_ip="10.0.0.2", az=0.0, alt=0.0, clips=True,
                         spectro=False, host=""),
            CameraConfig(code="RO0003", cam_ip="10.0.0.3", rtsp_url="", clips=False,
                         spectro=True, host="100.64.0.12"),
        ]),
        "pinned": StationConfig(ip="100.0.0.2", label="Pinned",
                                ssh_host_key_fingerprints=["SHA256:x"]),
    })
    out = config_to_dict(cfg)
    assert out.keys() == DashboardConfig.model_fields.keys()
    assert out["public_pages"] == sorted(PUBLIC_PAGE_KEYS)

    plain = out["stations"]["plain"]
    assert plain.keys() == StationConfig.model_fields.keys() - {"ssh_host_key_fingerprints"}
    assert (plain["ssh_user"], plain["lat"], plain["show_on_map"], plain["public"],
            plain["status"], plain["push_enabled"]) == ("gmn", None, True, True, "active", False)
    assert out["stations"]["pinned"]["ssh_host_key_fingerprints"] == ["SHA256:x"]
    assert out["stations"]["pinned"]["cameras"] == []

    assert plain["cameras"] == [
        {"code": "RO0001", "cam_ip": "", "rotate": False, "label": ""},
        {"code": "RO0002", "cam_ip": "10.0.0.2", "rotate": False, "label": "",
         "az": 0.0, "alt": 0.0},
        {"code": "RO0003", "cam_ip": "10.0.0.3", "rotate": False, "label": "",
         "clips": False, "rtsp_url": "", "spectro": True, "host": "100.64.0.12"},
    ]


def test_a_null_left_by_an_unvalidated_camera_patch_is_written_as_the_default():
    """The camera PATCH route applies its body with ``model_copy(update=)``,
    which skips validation. A JSON null in a field that does not accept None
    must not reach the file, or the registry stops loading; the hand-written
    serializer wrote '' for label and left host and spectro out."""
    cam = CameraConfig(code="RO0001", cam_ip="10.0.0.1", host="100.64.0.12",
                       spectro=True, label="N")
    cam = cam.model_copy(update={"label": None, "host": None, "spectro": None,
                                 "clips": None, "rotate": None})
    cfg = DashboardConfig(stations={"a": StationConfig(ip="100.0.0.1", label="A",
                                                       cameras=[cam])})
    out = config_to_dict(cfg)
    assert out["stations"]["a"]["cameras"] == [
        {"code": "RO0001", "cam_ip": "10.0.0.1", "rotate": False, "label": ""},
    ]
    reloaded = _save_and_reload(out).stations["a"].cameras[0]
    assert reloaded == CameraConfig(code="RO0001", cam_ip="10.0.0.1")


def test_an_empty_public_page_list_is_written_not_dropped():
    """``public_pages: []`` (nothing public) must not fall back to the
    all-pages default on the next load."""
    cfg = DashboardConfig(public_pages=[])
    assert config_to_dict(cfg)["public_pages"] == []
    assert _save_and_reload(config_to_dict(cfg)).public_pages == []


@pytest.mark.parametrize("path", REPO_REGISTRIES, ids=lambda p: str(p.relative_to(REPO)))
def test_saving_a_repo_registry_loses_nothing(path):
    cfg = load_config(path)
    once = config_to_dict(cfg)
    reloaded = _save_and_reload(once)
    assert reloaded == cfg
    # A second save writes the same file: nothing drifts save over save.
    assert config_to_dict(reloaded) == once


# ── A field added to the models later survives a save ────────────────────
#
# Subclasses stand in for a future edit of the models: the serializer has no
# field list of its own, so whatever the models declare is what it writes.


class _NewCamera(CameraConfig):
    exposure_profile: str = "night"


class _NewStation(StationConfig):
    cameras: list[_NewCamera] = []
    uplink: str | None = None
    backup_ip: str = ""


class _NewConfig(DashboardConfig):
    stations: dict[str, _NewStation] = {}
    alert_radius_km: int = 300


def test_a_field_added_to_the_models_survives_a_save():
    cfg = _NewConfig(alert_radius_km=450, stations={"x": _NewStation(
        ip="100.0.0.9", label="X", uplink="lte",
        cameras=[_NewCamera(code="RO0009", cam_ip="10.0.0.9", exposure_profile="moon")],
    )})
    out = config_to_dict(cfg)
    assert out["alert_radius_km"] == 450
    assert out["stations"]["x"]["uplink"] == "lte"
    # A station field is written even at its default, like every station field.
    assert out["stations"]["x"]["backup_ip"] == ""
    assert out["stations"]["x"]["cameras"][0]["exposure_profile"] == "moon"
    assert _save_and_reload(out, _NewConfig) == cfg


def test_a_new_camera_field_at_its_default_is_left_out():
    cfg = _NewConfig(stations={"x": _NewStation(
        ip="100.0.0.9", label="X", cameras=[_NewCamera(code="RO0009", cam_ip="10.0.0.9")],
    )})
    cam = config_to_dict(cfg)["stations"]["x"]["cameras"][0]
    assert "exposure_profile" not in cam
    assert _save_and_reload(config_to_dict(cfg), _NewConfig) == cfg


# ── The app saves and serves the real serializer's output ────────────────


def test_the_app_saves_and_serves_the_model_serializer(app_env, admin):  # noqa: F811
    store = app_env._config_store
    # Bound to the live config object, which ConfigStore updates in place on
    # every reload, so a save always writes what the workers serve.
    assert store.to_dict.func is config_to_dict
    assert store.to_dict.args == (store.config,)
    expected = config_to_dict(store.config)
    assert store.to_dict() == expected
    # The admin network-config view returns the same document.
    body = admin.get("/api/admin/network-config").get_json()
    assert body == json.loads(json.dumps(expected))


class TestCameraClipsFlagIsAdminWritable:
    """`clips` must be reachable through the admin API, not just the YAML.

    The live registry on the VPS is admin-API managed and excluded from
    deploys, so a field the camera PATCH allowlist does not carry cannot be
    set on a real station at all.
    """

    def test_clips_in_camera_patch_allowlist(self):
        import re
        src = (REPO / "dashboard" / "admin_config.py").read_text()
        m = re.search(r"cam_allowed = \{(.*?)\}", src, re.S)
        assert m, "cam_allowed allowlist not found"
        assert '"clips"' in m.group(1)
