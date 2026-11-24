"""Adding a camera through the admin API keeps a north (0 deg) azimuth.

``az=body.get("az") or None`` read 0 as "not given", so every camera added
pointing north (gmnro15 (Sighetu Marmației), gmnro21 (Vârteșcoiu) and gmnro22
(Ferigile) on 2026-09-27) lost its azimuth and needed a second PATCH.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from flask import Flask

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))


@pytest.fixture
def client():
    import admin_config
    from models import DashboardConfig, StationConfig

    config = DashboardConfig(stations={"gmnro99": StationConfig(ip="100.64.0.99", label="Test")})
    app = Flask(__name__)
    app.secret_key = "test"
    admin_config.register_admin_network_routes(
        app, config,
        save_config=lambda: None,
        config_to_dict=lambda: {},
        require_station=lambda h: config.stations[h],
        session_for_url=None,
    )
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "operator"
        sess["role"] = "admin"
    c.config = config
    return c


@pytest.mark.parametrize("az, expected", [(0, 0.0), (0.0, 0.0), (180, 180.0), ("", None), (None, None)])
def test_camera_add_keeps_the_given_azimuth(client, az, expected):
    body = {"code": "RO99XA", "cam_ip": "", "alt": 45.0}
    if az is not None:
        body["az"] = az
    r = client.post("/api/admin/network-config/station/gmnro99/camera", json=body)
    assert r.status_code == 200
    [cam] = client.config.stations["gmnro99"].cameras
    assert cam.az == expected
    assert cam.alt == 45.0
