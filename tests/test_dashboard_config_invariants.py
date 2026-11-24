"""Structural invariants for dashboard/dashboard_config.yaml.

These tests load the REAL config file (not a fixture) so that CI catches
config drift the moment a bad edit is committed. The stale Iasi IP
(100.64.0.10 surviving long after the physical machine was decommissioned
on 2026-06-18) is the canonical example of the class of bug these guard
against.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = REPO_ROOT / "dashboard" / "dashboard_config.example.yaml"

VALID_STATUSES = {"active", "commissioning", "offline"}
# Tailscale assigns IPs in 100.64.0.0/10 (CGNAT range), which maps to
# 100.64.x.x - 100.127.x.x. We accept the full 100.x.x.x prefix because
# individual station IPs have been observed across 100.67-100.126.
_TAILSCALE_RE = re.compile(r"^100\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
_IPV4_RE = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")


@pytest.fixture(scope="module")
def config() -> dict:
    """Load dashboard_config.yaml once per module."""
    raw = CONFIG_PATH.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    assert isinstance(data, dict), "dashboard_config.yaml must be a YAML mapping"
    return data


@pytest.fixture(scope="module")
def stations(config: dict) -> dict:
    return config.get("stations", {})


class TestCameraCodeUniqueness:
    """Every camera code must be unique across the entire fleet."""

    def test_no_duplicate_camera_codes(self, stations: dict):
        seen: dict[str, str] = {}  # code -> station_id
        duplicates: list[str] = []
        for station_id, station in stations.items():
            for cam in station.get("cameras", []):
                code = cam.get("code", "")
                if not code:
                    continue
                if code in seen:
                    duplicates.append(
                        f"{code!r} appears in both {seen[code]!r} and {station_id!r}"
                    )
                else:
                    seen[code] = station_id
        assert not duplicates, (
            "Duplicate camera codes detected (fleet-wide codes must be unique):\n"
            + "\n".join(f"  - {d}" for d in duplicates)
        )


class TestCameraIpUniquenessWithinStation:
    """No two cameras on the same HOST may share a cam_ip (unless empty).

    The scope is the host, not the station: a station split across two boxes
    (per-camera ``host`` override) has two independent camera LANs, and both
    operators' boxes hand out the same private addresses. gmnro17 (Câmpenița)
    is two Raspberry Pis that are each the .1 of their own 192.168.42.0/24
    with the camera at .10 — two cameras, one cam_ip, no conflict, because
    nothing ever resolves that address on the same box. Sharing a cam_ip
    within ONE host is still a real misconfiguration and stays forbidden.
    """

    def test_no_duplicate_cam_ips_within_host(self, stations: dict):
        violations: list[str] = []
        for station_id, station in stations.items():
            station_ip = station.get("ip", "")
            # (host, ip) -> first code that claimed it
            seen_ips: dict[tuple[str, str], str] = {}
            for cam in station.get("cameras", []):
                ip = (cam.get("cam_ip") or "").strip()
                code = cam.get("code", "<unknown>")
                if not ip:
                    # Empty ip is allowed (camera not yet connected)
                    continue
                host = (cam.get("host") or "").strip() or station_ip
                key = (host, ip)
                if key in seen_ips:
                    violations.append(
                        f"{station_id}: {code!r} shares cam_ip {ip!r} "
                        f"with {seen_ips[key]!r} on host {host!r}"
                    )
                else:
                    seen_ips[key] = code
        assert not violations, (
            "Cameras on the same host must not share cam_ip:\n"
            + "\n".join(f"  - {v}" for v in violations)
        )


class TestCameraHostOverride:
    """Per-camera ``host`` overrides must be routable and well-formed."""

    def test_camera_hosts_are_tailscale_ips(self, stations: dict):
        bad: list[str] = []
        for station_id, station in stations.items():
            for cam in station.get("cameras", []):
                host = (cam.get("host") or "").strip()
                if not host:
                    continue
                if not _TAILSCALE_RE.match(host):
                    bad.append(
                        f"{station_id}/{cam.get('code', '<unknown>')}: "
                        f"host {host!r} is not a Tailscale 100.x address"
                    )
        assert not bad, (
            "Camera host overrides must be Tailscale addresses:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )

    def test_camera_host_differs_from_station_ip(self, stations: dict):
        """An override equal to the station ip is dead weight — it means the
        camera is NOT on a second box and the field should be empty."""
        redundant: list[str] = []
        for station_id, station in stations.items():
            for cam in station.get("cameras", []):
                host = (cam.get("host") or "").strip()
                if host and host == station.get("ip", ""):
                    redundant.append(
                        f"{station_id}/{cam.get('code', '<unknown>')}: "
                        f"host {host!r} equals the station ip; leave it empty"
                    )
        assert not redundant, (
            "Redundant camera host overrides:\n"
            + "\n".join(f"  - {r}" for r in redundant)
        )

    def test_split_host_stations_have_no_jump_hosts(self, stations: dict):
        """A jump-hosted station is reached through an SSH tunnel that
        forwards to ``ip`` only, so a second box behind it is unreachable.
        Split stations must be directly routable on the tailnet."""
        bad: list[str] = []
        for station_id, station in stations.items():
            has_override = any(
                (c.get("host") or "").strip() for c in station.get("cameras", [])
            )
            if has_override and station.get("jump_hosts"):
                bad.append(
                    f"{station_id}: has a per-camera host override AND "
                    f"jump_hosts {station['jump_hosts']!r}"
                )
        assert not bad, (
            "Split-host stations cannot be reached through a jump host:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )


class TestStationStatusValues:
    """Every station status must be one of the accepted enum values."""

    def test_status_in_allowed_set(self, stations: dict):
        bad: list[str] = []
        for station_id, station in stations.items():
            status = station.get("status", "active")
            if status not in VALID_STATUSES:
                bad.append(
                    f"{station_id}: status={status!r} not in {sorted(VALID_STATUSES)}"
                )
        assert not bad, (
            "Station status must be one of "
            f"{sorted(VALID_STATUSES)}:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )


class TestPublicStationCoordinates:
    """Every public station must have non-null lat and lon."""

    def test_public_stations_have_coordinates(self, stations: dict):
        missing: list[str] = []
        for station_id, station in stations.items():
            if not station.get("public", True):
                continue
            lat = station.get("lat")
            lon = station.get("lon")
            if lat is None or lon is None:
                missing.append(
                    f"{station_id}: public=true but lat={lat!r}, lon={lon!r}"
                )
        assert not missing, (
            "Public stations must have non-null lat and lon "
            "(needed for map rendering and public API):\n"
            + "\n".join(f"  - {m}" for m in missing)
        )


class TestStationIpFormat:
    """Every station ip must be a valid dotted-quad IPv4 address.

    Tailscale IPs use the 100.x.x.x range (CGNAT / RFC6598).  We accept any
    dotted-quad so the test remains useful if a station is ever reached via a
    non-Tailscale address, but the pattern check still catches obvious typos
    like a hostname, URL, or stale placeholder left after a migration.
    """

    def test_station_ips_are_dotted_quad(self, stations: dict):
        bad: list[str] = []
        for station_id, station in stations.items():
            ip = station.get("ip", "")
            if not _IPV4_RE.match(ip):
                bad.append(f"{station_id}: ip={ip!r} is not a dotted-quad IPv4")
        assert not bad, (
            "Station IPs must be dotted-quad IPv4 addresses:\n"
            + "\n".join(f"  - {b}" for b in bad)
        )

    def test_station_ips_are_tailscale_range(self, stations: dict):
        """Fleet IPs should be in 100.x.x.x (Tailscale CGNAT range).

        Fail with an informative message rather than silently passing a
        non-Tailscale address — a direct LAN IP in the config usually means
        the Tailscale address was not updated after a machine migration.
        """
        bad: list[str] = []
        for station_id, station in stations.items():
            ip = station.get("ip", "")
            if _IPV4_RE.match(ip) and not _TAILSCALE_RE.match(ip):
                bad.append(
                    f"{station_id}: ip={ip!r} is a valid IPv4 but not in "
                    "the 100.x.x.x Tailscale range — update after machine migration"
                )
        assert not bad, (
            "Station IPs must be Tailscale addresses (100.x.x.x):\n"
            + "\n".join(f"  - {b}" for b in bad)
        )
