"""The admin-UI config push must not commit a half-written snapshot.

save_config() writes dashboard_config.yaml the moment an admin edits it, and
config_sync pushes that file to the branch. tests/test_dashboard_config_invariants
loads the REAL committed config, so a snapshot caught mid-edit turned CI red on
every open PR until someone reverted it by hand. These cover the structural
guard that now stands between the snapshot and the commit.
"""

from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest
import yaml

import config_sync

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG = REPO_ROOT / "dashboard" / "dashboard_config.example.yaml"


def _station(**overrides) -> dict:
    station = {
        "ip": "100.64.0.1",
        "cameras": [{"code": "RO000M", "cam_ip": "192.168.42.10"}],
    }
    station.update(overrides)
    return station


def _config(**stations) -> dict:
    return {"stations": stations or {"gmnro01": _station()}}


class TestValidateConfig:
    def test_real_committed_config_passes(self):
        """The guard must agree with the config the fleet actually runs.

        If this ever fails the guard is too strict, and it would silently stop
        syncing every legitimate admin edit -- a worse failure than the one it
        prevents, because nothing would look broken.
        """
        data = yaml.safe_load(REAL_CONFIG.read_text(encoding="utf-8"))
        assert config_sync.validate_config(data) == []

    def test_minimal_valid_config_passes(self):
        assert config_sync.validate_config(_config()) == []

    def test_camera_without_cam_ip_is_allowed(self):
        cfg = _config(gmnro01=_station(cameras=[{"code": "RO000M", "cam_ip": ""}]))
        assert config_sync.validate_config(cfg) == []

    def test_same_cam_ip_on_different_hosts_is_allowed(self):
        """A station split across two boxes has two independent camera LANs."""
        cfg = _config(gmnro17=_station(cameras=[
            {"code": "RO0001", "cam_ip": "192.168.42.10"},
            {"code": "RO0002", "cam_ip": "192.168.42.10", "host": "100.64.0.2"},
        ]))
        assert config_sync.validate_config(cfg) == []

    @pytest.mark.parametrize("bad", [None, [], "stations:", 42])
    def test_non_mapping_root_is_rejected(self, bad):
        assert config_sync.validate_config(bad)

    def test_missing_stations_is_rejected(self):
        assert config_sync.validate_config({"station_api_port": 7779})

    def test_empty_stations_is_rejected(self):
        assert config_sync.validate_config({"stations": {}})

    def test_missing_required_station_key_is_rejected(self):
        cfg = {"stations": {"gmnro01": {"ip": "100.64.0.1"}}}
        violations = config_sync.validate_config(cfg)
        assert any("cameras" in v for v in violations)

    def test_non_ipv4_station_ip_is_rejected(self):
        violations = config_sync.validate_config(_config(gmnro01=_station(ip="")))
        assert any("dotted-quad" in v for v in violations)

    def test_duplicate_cam_ip_within_a_host_is_rejected(self):
        cfg = _config(gmnro01=_station(cameras=[
            {"code": "RO000M", "cam_ip": "192.168.42.10"},
            {"code": "RO000N", "cam_ip": "192.168.42.10"},
        ]))
        violations = config_sync.validate_config(cfg)
        assert any("shares cam_ip" in v for v in violations)

    @pytest.mark.parametrize("code", ["", "RO00", "ro000m", "RO000MM", None])
    def test_malformed_camera_code_is_rejected(self, code):
        cfg = _config(gmnro01=_station(
            cameras=[{"code": code, "cam_ip": "192.168.42.10"}]
        ))
        violations = config_sync.validate_config(cfg)
        assert any("station code" in v for v in violations)


class TestSyncSkipsInvalidSnapshots:
    """The guard must sit before the network call, not after it."""

    @pytest.fixture
    def calls(self, monkeypatch):
        seen: list[str] = []

        def _fake_urlopen(req, *a, **kw):
            seen.append(req.get_method())
            # Abort the sync after recording; reaching the network at all is
            # the only thing under test here.
            raise urllib.error.URLError("stubbed")

        monkeypatch.setattr(config_sync, "GITHUB_TOKEN", "stub-token")
        monkeypatch.setattr(config_sync.urllib.request, "urlopen", _fake_urlopen)
        return seen

    def _write(self, tmp_path: Path, data: dict) -> Path:
        p = tmp_path / "dashboard_config.yaml"
        p.write_text(yaml.safe_dump(data), encoding="utf-8")
        return p

    def test_valid_config_reaches_github(self, tmp_path, calls):
        config_sync._sync_to_github(self._write(tmp_path, _config()))
        assert calls == ["GET"]

    def test_invalid_config_is_never_pushed(self, tmp_path, calls, caplog):
        bad = _config(gmnro01=_station(cameras=[
            {"code": "RO000M", "cam_ip": "192.168.42.10"},
            {"code": "RO000N", "cam_ip": "192.168.42.10"},
        ]))
        config_sync._sync_to_github(self._write(tmp_path, bad))
        assert calls == [], "an invalid snapshot must not reach GitHub"
        assert "refusing to push" in caplog.text
        assert "shares cam_ip" in caplog.text, "the log must name the violation"

    def test_unparseable_yaml_is_never_pushed(self, tmp_path, calls, caplog):
        p = tmp_path / "dashboard_config.yaml"
        p.write_text("stations:\n  gmnro01:\n   - ip: [unclosed\n", encoding="utf-8")
        config_sync._sync_to_github(p)
        assert calls == []
        assert "refusing to push" in caplog.text
