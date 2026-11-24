"""Each dashboard app keeps its built past-night payloads under the cache path
in effect when ``create_app()`` runs.

``rovimen_dashboard`` built its night store from ``CACHE_PATH``, a constant the
module captured at import, so every app one process created shared the first
importer's store. The store is keyed by date alone: a second app asking for the
same old night got the first app's fleet. That is how
``test_public_dashboard_authz`` and ``test_shared_snapshot_wiring`` came to fail
on CI from 2026-09-26, the day their fixed dates left the archive index's hot
window and started going through the store.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


def _make_app(root: Path, monkeypatch: pytest.MonkeyPatch, station: str):
    root.mkdir(parents=True)
    (root / "users.yaml").write_text(yaml.safe_dump({
        "admin": {"display_name": "Admin", "password_hash": "", "role": "admin",
                  "require_totp": False},
    }))
    config_path = root / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "station_api_port": 7779,
        "stations": {
            station: {
                "ip": "127.0.0.1", "label": station, "ssh_user": "t", "public": True,
                "lat": 44.4, "lon": 26.1,
                "cameras": [{"code": "RO0001", "cam_ip": "127.0.0.1", "label": "N",
                             "rotate": False}],
            },
        },
    }))
    for key, value in {
        "ROVIMEN_USERS_PATH": root / "users.yaml", "ROVIMEN_SECRET_KEY": "night-store",
        "ROVIMEN_COOKIE_SECURE": "0", "RATELIMIT_ENABLED": "0",
        "THUMB_CACHE_DIR": root / "thumb_cache", "ROVIMEN_CACHE_PATH": root / "cache",
        "ROVIMEN_COMPILATIONS_OUT_PATH": root / "compilations",
        "ROVIMEN_KNOWN_HOSTS": root / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": root / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": root / "activity.log",
        "ROVIMEN_STATION_STATE_DB": root / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": root / "detections.db",
        "ROVIMEN_SHOWER_COUNTS_PATH": root / "shower_counts.json",
    }.items():
        monkeypatch.setenv(key, str(value))
    monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)

    from rovimen_dashboard import create_app, load_config
    app = create_app(load_config(config_path), config_path)
    app.config["TESTING"] = True
    return app


def test_a_second_app_does_not_read_the_first_apps_stored_nights(tmp_path, monkeypatch):
    # Far outside the hot window, so the payload goes through the night store.
    night = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y%m%d")

    _make_app(tmp_path / "a", monkeypatch, "alpha")
    # A night the first app built completely. Written directly, because a
    # fixture app has no ready archive index, so its own builds are never kept.
    from night_payload_store import NightPayloadStore
    NightPayloadStore(tmp_path / "a" / "cache" / "detections_nights").save(
        f"{night}-1", {"date": night, "by_station": {"alpha": {"cameras": {}}}}, 3600,
    )

    _make_app(tmp_path / "b", monkeypatch, "beta")
    import routes.detections as det
    # The module serves the app registered last: the second one.
    by_station = det.compute_detections_payload(night).get("by_station") or {}
    assert "alpha" not in by_station, "the second app was served the first app's stored night"
    assert "beta" in by_station
