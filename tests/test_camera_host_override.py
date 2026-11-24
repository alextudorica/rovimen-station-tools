"""One logical station spanning two physical hosts (per-camera ``host``).

gmnro17 (Câmpenița) is a single station to the operator, the map and the
dashboard, but it is physically two Raspberry Pis: RO0001 on the station
``ip`` and RO0002 on its own Tailscale address. Camera-scoped station-API
requests must land on the box that actually holds that camera's data;
station-scoped requests must keep going to the station ``ip``.

These tests pin that routing at the two layers that decide it:
``StationConfig.host_for_path`` (the rule) and ``TunnelManager.get_api_base``
(the caller on the request hot path).
"""

from __future__ import annotations

import pytest

import tunnels
from models import CameraConfig, DashboardConfig, StationConfig

STATION_IP = "100.64.0.14"   # Pi A — serves RO0001
CAMERA_HOST = "100.64.0.15"   # Pi B — serves RO0002


@pytest.fixture
def split_station() -> StationConfig:
    return StationConfig(
        ip=STATION_IP,
        label="Câmpenița",
        ssh_user="pi",
        cameras=[
            CameraConfig(code="RO0001", cam_ip="192.168.42.10"),
            # Same private camera address as RO0001 — legitimately, because
            # it sits on the OTHER Pi's own 192.168.42.0/24.
            CameraConfig(
                code="RO0002", cam_ip="192.168.42.10", host=CAMERA_HOST
            ),
        ],
    )


@pytest.fixture
def single_station() -> StationConfig:
    return StationConfig(
        ip="100.64.0.16",
        label="Bacău",
        cameras=[CameraConfig(code="RO001E", cam_ip="192.168.4.220")],
    )


class TestCameraHostField:
    def test_host_defaults_to_empty(self):
        cam = CameraConfig(code="RO0001", cam_ip="192.168.42.10")
        assert cam.host == ""

    def test_host_accepts_ipv4_and_hostname(self):
        assert CameraConfig(code="A", cam_ip="", host=CAMERA_HOST).host == CAMERA_HOST
        assert (
            CameraConfig(code="A", cam_ip="", host="gmnro19.example.ts.net").host
            == "gmnro19.example.ts.net"
        )

    def test_host_is_stripped(self):
        assert CameraConfig(code="A", cam_ip="", host="  100.64.0.15 ").host == CAMERA_HOST

    def test_host_rejects_garbage(self):
        with pytest.raises(ValueError):
            CameraConfig(code="A", cam_ip="", host="http://100.64.0.15:7779")


class TestHostForPath:
    def test_camera_scoped_path_routes_to_camera_host(self, split_station):
        assert split_station.host_for_path("/api/nights/RO0002") == CAMERA_HOST
        assert (
            split_station.host_for_path("/api/chunks/RO0002/20260824") == CAMERA_HOST
        )
        assert split_station.host_for_path("/api/platepar?cam=RO0002") == CAMERA_HOST

    def test_camera_without_override_stays_on_station_ip(self, split_station):
        assert split_station.host_for_path("/api/nights/RO0001") == STATION_IP

    def test_station_scoped_path_stays_on_station_ip(self, split_station):
        for path in ("/api/status", "/api/settings", "/api/rovimen/status", ""):
            assert split_station.host_for_path(path) == STATION_IP

    def test_code_must_match_as_a_whole_token(self, split_station):
        """A longer code that merely CONTAINS RO0002 must not be captured."""
        assert split_station.host_for_path("/api/nights/RO0002X") == STATION_IP
        assert split_station.host_for_path("/api/nights/XRO0002") == STATION_IP

    def test_single_host_station_is_unaffected(self, single_station):
        assert single_station.host_for_path("/api/nights/RO001E") == "100.64.0.16"
        assert single_station.host_for_path("/api/status") == "100.64.0.16"

    def test_camera_host_helper(self, split_station):
        assert split_station.camera_host("RO0002") == CAMERA_HOST
        assert split_station.camera_host("RO0001") == STATION_IP
        # An unknown code must not break routing.
        assert split_station.camera_host("RO9999") == STATION_IP

    def test_is_split_host(self, split_station, single_station):
        assert split_station.is_split_host is True
        assert single_station.is_split_host is False


class TestGetApiBaseRouting:
    def _manager(self, station: StationConfig) -> tunnels.TunnelManager:
        cfg = DashboardConfig(stations={"gmnro17": station})
        return tunnels.TunnelManager(cfg)

    def test_camera_scoped_request_hits_the_second_box(self, split_station):
        mgr = self._manager(split_station)
        assert (
            mgr.get_api_base("gmnro17", "/api/nights/RO0002")
            == f"http://{CAMERA_HOST}:7779"
        )

    def test_station_scoped_request_hits_the_station_ip(self, split_station):
        mgr = self._manager(split_station)
        assert mgr.get_api_base("gmnro17", "/api/status") == f"http://{STATION_IP}:7779"

    def test_omitted_path_keeps_legacy_behaviour(self, split_station):
        """Existing callers that never pass a path must be unchanged."""
        mgr = self._manager(split_station)
        assert mgr.get_api_base("gmnro17") == f"http://{STATION_IP}:7779"

    def test_station_url_routes_by_path(self, split_station):
        import station_client

        cfg = DashboardConfig(stations={"gmnro17": split_station})
        mgr = tunnels.TunnelManager(cfg)
        assert station_client.station_url(
            cfg, mgr, "gmnro17", "/api/nights/RO0002"
        ) == f"http://{CAMERA_HOST}:7779/api/nights/RO0002"
        assert station_client.station_url(
            cfg, mgr, "gmnro17", "/api/status"
        ) == f"http://{STATION_IP}:7779/api/status"
