"""Two gunicorn workers editing dashboard_config.yaml must not clobber each other.

Before ConfigStore each worker mutated its own in-memory DashboardConfig and
dumped it whole, so a PATCH handled by worker A was overwritten by the next
save on worker B (gmnro01, 2026-09-05: RO000A's IP flipped back on every other
request). These tests drive two stores over one file the way two workers do.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from cache_store import ConfigStore, load_config
from models import CameraConfig, DashboardConfig, StationConfig


def _seed(path: Path) -> None:
    cfg = DashboardConfig(stations={
        "gmnro01": StationConfig(
            ip="100.64.0.11", label="Ghirdoveni", ssh_user="gmn",
            cameras=[CameraConfig(code="RO000A", cam_ip="192.168.42.13"),
                     CameraConfig(code="RO000B", cam_ip="192.168.42.14")],
        ),
    })
    path.write_text(yaml.safe_dump(json.loads(cfg.model_dump_json())))


def _worker(path: Path) -> ConfigStore:
    config = load_config(path)
    return ConfigStore(config, path, to_dict=lambda: json.loads(config.model_dump_json()))


def _cam_ip(config: DashboardConfig, code: str) -> str:
    return next(c.cam_ip for c in config.stations["gmnro01"].cameras if c.code == code)


def test_two_workers_saving_different_cameras_keep_both(tmp_path: Path):
    path = tmp_path / "dashboard_config.yaml"
    _seed(path)
    a, b = _worker(path), _worker(path)          # both loaded the same stale file

    with a.mutation() as cfg:
        cfg.stations["gmnro01"].cameras[0].cam_ip = "192.168.0.144"   # RO000A
    with b.mutation() as cfg:                    # b never saw a's change in memory
        cfg.stations["gmnro01"].cameras[1].cam_ip = "192.168.0.186"   # RO000B

    on_disk = load_config(path)
    assert _cam_ip(on_disk, "RO000A") == "192.168.0.144"
    assert _cam_ip(on_disk, "RO000B") == "192.168.0.186"
    # b's live object was refreshed before it mutated, so it carries a's edit too
    assert _cam_ip(b.config, "RO000A") == "192.168.0.144"


def test_refresh_if_changed_adopts_another_workers_save(tmp_path: Path):
    path = tmp_path / "dashboard_config.yaml"
    _seed(path)
    a, b = _worker(path), _worker(path)

    assert b.refresh_if_changed() is False       # nothing happened yet
    with a.mutation() as cfg:
        cfg.stations["gmnro01"].label = "Ghirdoveni x86"
    assert _cam_ip(b.config, "RO000A") == "192.168.42.13"
    assert b.config.stations["gmnro01"].label == "Ghirdoveni"

    assert b.refresh_if_changed() is True
    assert b.config.stations["gmnro01"].label == "Ghirdoveni x86"
    assert b.refresh_if_changed() is False       # one stat, no reload


def test_live_object_identity_is_preserved_across_reload(tmp_path: Path):
    """Closures across the app hold the DashboardConfig instance; a reload
    must land in that object, not replace it."""
    path = tmp_path / "dashboard_config.yaml"
    _seed(path)
    a, b = _worker(path), _worker(path)
    held = b.config
    with a.mutation() as cfg:
        cfg.stations["gmnro01"].ip = "100.0.0.1"
    b.refresh_if_changed()
    assert held is b.config
    assert held.stations["gmnro01"].ip == "100.0.0.1"


def test_torn_file_keeps_serving_and_does_not_raise(tmp_path: Path):
    path = tmp_path / "dashboard_config.yaml"
    _seed(path)
    a = _worker(path)
    path.write_text("stations: [this is not: valid yaml")  # a half-written save
    assert a.refresh_if_changed() is False
    assert "gmnro01" in a.config.stations


def test_store_without_a_path_is_a_no_op(tmp_path: Path):
    config = DashboardConfig()
    store = ConfigStore(config, None, to_dict=lambda: {})
    with store.mutation() as cfg:
        cfg.stations["x"] = StationConfig(ip="1.2.3.4", label="x", ssh_user="gmn")
    assert store.refresh_if_changed() is False
    assert "x" in config.stations
