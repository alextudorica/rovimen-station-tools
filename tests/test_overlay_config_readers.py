"""The two overlay-config readers share one cached /api/settings fetch and
keep their own fallbacks.

``stack_overlay.fetch_overlay_config`` (branded stack downloads) falls back to
the fleet-default overlay; ``routes.media._crop_overlay_config`` (crops and the
fullstack render) keeps the station's own block, empty when the station is
unreachable, with the bundle font/logo remapped in. Both used to fetch and
cache the settings separately.
"""

from __future__ import annotations

import pytest

import routes.media as media
import stack_overlay
import station_client


@pytest.fixture
def settings(monkeypatch, tmp_path):
    """Stub the station's /api/settings; ``box["resp"]`` is what it returns
    (an Exception instance is raised). Records every fetch."""
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))  # no bundle logo
    monkeypatch.setattr(media, "_BUNDLE_FONT", tmp_path / "no-font.ttf")
    stack_overlay._settings_cache.clear()
    box: dict = {"resp": None, "calls": 0}

    def fake(config, tunnels, host_key, path, timeout=None):
        assert path == "/api/settings"
        box["calls"] += 1
        if isinstance(box["resp"], Exception):
            raise box["resp"]
        return box["resp"]

    monkeypatch.setattr(station_client, "station_get_raw", fake)
    yield box
    stack_overlay._settings_cache.clear()


def _both() -> tuple[tuple[dict, dict], tuple[dict, dict]]:
    return (stack_overlay.fetch_overlay_config(None, None, "hk", "TST001"),
            media._crop_overlay_config(None, None, "hk", "TST001"))


def test_unreachable_station_fleet_default_vs_empty_block(settings):
    settings["resp"] = ConnectionError("offline")
    (stack_cfg, stack_cam), (crop_cfg, crop_cam) = _both()
    assert stack_cfg == stack_overlay._FLEET_DEFAULT_OVERLAY
    assert crop_cfg == {}
    assert stack_cam == crop_cam == {}


def test_empty_overlay_block_fleet_default_vs_empty_block(settings):
    settings["resp"] = {"overlay": {}, "stations": {}}
    (stack_cfg, _), (crop_cfg, _) = _both()
    assert stack_cfg == stack_overlay._FLEET_DEFAULT_OVERLAY
    assert crop_cfg == {}


def test_live_overlay_and_pointing_reach_both(settings):
    live = {"enabled": True, "network": "ROVIMEN", "style": "cinema"}
    settings["resp"] = {"overlay": live,
                        "stations": {"TST001": {"az": 120.0, "alt": 40.0}}}
    (stack_cfg, stack_cam), (crop_cfg, crop_cam) = _both()
    assert stack_cfg == crop_cfg == live
    assert stack_cam == crop_cam == {"az": 120.0, "alt": 40.0}


def test_one_fetch_serves_both_readers_for_30_min(settings):
    settings["resp"] = {"overlay": {"enabled": True}, "stations": {}}
    _both()
    _both()
    assert settings["calls"] == 1


def test_returned_configs_are_private_copies(settings):
    settings["resp"] = {"overlay": {"enabled": True}, "stations": {}}
    stack_cfg, _ = stack_overlay.fetch_overlay_config(None, None, "hk", "TST001")
    stack_cfg["enabled"] = False
    crop_cfg, _ = media._crop_overlay_config(None, None, "hk", "TST001")
    assert crop_cfg == {"enabled": True}
