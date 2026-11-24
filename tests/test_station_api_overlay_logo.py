"""POST /api/overlay/logo: the dashboard's logo picker.

It may only point ``overlay.logo`` at an image already in the station's
assets directory, and it is network-allowlisted for that reason.
"""

from __future__ import annotations

import json

import pytest

import station_api

REMOTE = {"REMOTE_ADDR": "100.64.0.9"}


@pytest.fixture
def setup(tmp_path, monkeypatch, station_patch):
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "astromania_text.png").write_bytes(b"png")
    (assets / "astroian_observatory.png").write_bytes(b"png")
    (assets / "notes.txt").write_text("x")
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"overlay": {"logo": str(assets / "astromania_text.png"), "style": "cinema"}}))
    monkeypatch.setattr(station_api, "_ASSETS_DIR", assets)
    station_patch("CONFIG_PATH", cfg)
    station_api.app.testing = True
    return station_api.app.test_client(), cfg, assets


def test_sets_logo_from_the_network(setup):
    client, cfg, assets = setup
    resp = client.post("/api/overlay/logo", json={"filename": "astroian_observatory.png"},
                       environ_overrides=REMOTE)
    assert resp.status_code == 200
    overlay = json.loads(cfg.read_text())["overlay"]
    assert overlay["logo"] == str(assets / "astroian_observatory.png")
    assert overlay["style"] == "cinema"


@pytest.mark.parametrize("name", ["../config.json", "/etc/passwd", "sub/astromania_text.png", "", None, 3])
def test_refuses_anything_but_a_bare_name(setup, name):
    client, cfg, _ = setup
    before = cfg.read_text()
    resp = client.post("/api/overlay/logo", json={"filename": name}, environ_overrides=REMOTE)
    assert resp.status_code == 400
    assert cfg.read_text() == before


@pytest.mark.parametrize("name", ["missing.png", "notes.txt"])
def test_refuses_files_that_are_not_shipped_images(setup, name):
    client, cfg, _ = setup
    before = cfg.read_text()
    resp = client.post("/api/overlay/logo", json={"filename": name}, environ_overrides=REMOTE)
    assert resp.status_code == 404
    assert cfg.read_text() == before
