"""``config_file``: the one place the dashboard and its services find and
load ``dashboard_config.yaml``."""

from __future__ import annotations

from pathlib import Path

import config_file


def test_env_precedence_and_default(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("ROVIMEN_CONFIG", raising=False)
    monkeypatch.delenv("ROVIMEN_DASHBOARD_CONFIG", raising=False)
    default = Path(config_file.__file__).resolve().parent / "dashboard_config.yaml"
    assert config_file.dashboard_config_path() == default
    monkeypatch.setenv("ROVIMEN_DASHBOARD_CONFIG", str(tmp_path / "b.yaml"))
    assert config_file.dashboard_config_path() == tmp_path / "b.yaml"
    monkeypatch.setenv("ROVIMEN_CONFIG", "  ")
    assert config_file.dashboard_config_path() == tmp_path / "b.yaml"
    monkeypatch.setenv("ROVIMEN_CONFIG", str(tmp_path / "a.yaml"))
    assert config_file.dashboard_config_path() == tmp_path / "a.yaml"


def test_load_config_validates_the_registry(tmp_path: Path):
    p = tmp_path / "dashboard_config.yaml"
    p.write_text("stations:\n  hk:\n    ip: 127.0.0.1\n    label: Roit\n    ssh_user: t\n"
                 "    cameras:\n      - {code: RO000E, cam_ip: 127.0.0.1}\n")
    cfg = config_file.load_config(p)
    assert cfg.stations["hk"].cameras[0].code == "RO000E"


def test_services_share_the_lookup():
    import cache_store
    import clip_requester
    import nightly_telegram

    assert cache_store.load_config is config_file.load_config
    assert clip_requester.load_config is config_file.load_config
    assert nightly_telegram.dashboard_config_path is config_file.dashboard_config_path
