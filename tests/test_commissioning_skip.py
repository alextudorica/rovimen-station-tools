"""Tests for the commissioning-station poll opt-out.

A station with ``status: commissioning`` is being set up (usually no cameras
wired yet, often a powered-off box). The dashboard must NEVER contact it: an
offline commissioning peer's repeated background polls wedge tailscaled's
magicsock (DERP route-churn) and once took the whole tailnet path to the VPS
down. The gate is ``StationConfig.should_contact`` and it must be honoured by:

  * station_client.start_polling status + vitals fan-out
  * index_poller._load_stations (detection-index poll)
  * the dashboard thumb-prefetch loop (covered indirectly — empty cameras)

Active stations (the default) stay contactable, even when temporarily offline,
so the dashboard still notices when they recover.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

import station_client
import index_poller
from models import CameraConfig, StationConfig, DashboardConfig


# ── Helpers (mirror test_push_enabled_skip) ───────────────────────────────────

def _capture_poll_targets(config, cache):
    tunnels = MagicMock()
    ran_target: list = []

    def capture_thread(*args, **kwargs):
        target = kwargs.get("target") or (args[0] if args else None)
        ran_target.append(target)
        return MagicMock()

    with patch("station_client.threading") as mock_threading:
        mock_threading.Thread.side_effect = capture_thread
        station_client.start_polling(config, tunnels, cache)

    assert len(ran_target) == 2, "expected poll_status and poll_vitals threads"
    return ran_target[0], ran_target[1]


def _run_once(poll_fn) -> None:
    def fake_sleep(_seconds):
        raise StopIteration("done")

    with patch("station_client.time") as mock_time:
        mock_time.sleep.side_effect = fake_sleep
        with pytest.raises(StopIteration):
            poll_fn()


# ── should_contact property ───────────────────────────────────────────────────

class TestShouldContactProperty:
    def test_commissioning_station_is_not_contactable(self):
        st = StationConfig(ip="100.64.0.13", label="Runcu Stone", status="commissioning")
        assert st.should_contact is False

    def test_commissioning_without_ip_is_not_contactable(self):
        # e.g. Cahul: registered, no IP assigned yet.
        st = StationConfig(ip="", label="Cahul", status="commissioning")
        assert st.should_contact is False

    def test_active_station_is_contactable(self):
        st = StationConfig(ip="100.0.0.1", label="Normal")  # status defaults to active
        assert st.should_contact is True

    def test_active_station_with_cameras_is_contactable(self):
        st = StationConfig(
            ip="100.0.0.1",
            label="Normal",
            cameras=[CameraConfig(code="RO000A", cam_ip="192.168.1.10")],
        )
        assert st.should_contact is True


# ── station_client status/vitals poll skip ────────────────────────────────────

class TestStatusVitalsPollSkip:
    def _config(self) -> DashboardConfig:
        return DashboardConfig(
            stations={
                "normal": StationConfig(ip="100.0.0.1", label="Normal"),
                "sarm": StationConfig(
                    ip="100.64.0.13", label="Runcu Stone", status="commissioning"
                ),
            }
        )

    def test_status_poll_skips_commissioning_station(self):
        config = self._config()
        cache = MagicMock()
        cache.get_status.return_value = None
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"online": True}

        poll_status_fn, _ = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_status_fn)

        assert "normal" in polled, "active station must still be polled"
        assert "sarm" not in polled, "commissioning station must never be contacted"
        set_keys = [c.args[0] for c in cache.set_status.call_args_list]
        assert "sarm" not in set_keys

    def test_vitals_poll_skips_commissioning_station(self):
        config = self._config()
        cache = MagicMock()
        polled: list[str] = []

        def fake_get(cfg, tunnels, key, endpoint, timeout):
            polled.append(key)
            return {"cpu": 1}

        _, poll_vitals_fn = _capture_poll_targets(config, cache)
        with patch("station_client.station_get_status", side_effect=fake_get):
            _run_once(poll_vitals_fn)

        assert "normal" in polled
        assert "sarm" not in polled
        set_keys = [c.args[0] for c in cache.set_vitals.call_args_list]
        assert "sarm" not in set_keys


# ── index_poller detection-index poll skip ────────────────────────────────────

class TestIndexPollerSkip:
    def _write_config(self, tmp_path, stations: dict):
        import yaml
        cfg = {"station_api_port": 7779, "stations": stations}
        path = tmp_path / "dashboard_config.yaml"
        path.write_text(yaml.safe_dump(cfg))
        return path

    def test_load_stations_skips_commissioning(self, tmp_path):
        path = self._write_config(tmp_path, {
            "normal": {
                "ip": "100.0.0.1", "label": "Normal",
                "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
            },
            "sarm": {
                "ip": "100.64.0.13", "label": "Runcu Stone",
                "status": "commissioning",
                "cameras": [],
            },
        })
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "sarm" not in stations, "commissioning station must not be polled"

    def test_load_stations_skips_zero_camera_station(self, tmp_path):
        path = self._write_config(tmp_path, {
            "normal": {
                "ip": "100.0.0.1", "label": "Normal",
                "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
            },
            "nocams": {"ip": "100.0.0.9", "label": "No cams", "cameras": []},
        })
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
        assert "nocams" not in stations, "no detection index without cameras"

    def test_load_stations_keeps_active_station_with_cameras(self, tmp_path):
        path = self._write_config(tmp_path, {
            "normal": {
                "ip": "100.0.0.1", "label": "Normal",
                "cameras": [{"code": "RO000A", "cam_ip": "192.168.1.10"}],
            },
        })
        with patch.object(index_poller, "CONFIG_PATH", path):
            stations = index_poller._load_stations()
        assert "normal" in stations
