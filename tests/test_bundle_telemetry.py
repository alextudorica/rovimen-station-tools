"""Tests for station_api._bundle_status — signed-bundle telemetry.

The helper reads the verifier's on-disk artefacts (.version, .bundle_state.json,
update_verify_mode, pinned pubkey) and must never raise: a missing file yields
nulls so the dashboard watchdog can treat the station as "not yet rolled out".
All paths are env-overridable, mirroring bundle_verify.py.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import station_api


@pytest.fixture
def bundle_env(tmp_path, monkeypatch, station_patch):
    """Point every _bundle_status input at tmp_path and return the paths."""
    scripts = tmp_path / "rovimen_scripts"
    scripts.mkdir()
    station_patch("CONFIG_PATH", scripts / "config.json")
    state = scripts / ".bundle_state.json"
    mode = tmp_path / "update_verify_mode"
    pin = tmp_path / "bundle_signing_pub.pem"
    monkeypatch.setenv("ROVIMEN_BUNDLE_STATE", str(state))
    monkeypatch.setenv("ROVIMEN_UPDATE_VERIFY_MODE_FILE", str(mode))
    monkeypatch.setenv("ROVIMEN_BUNDLE_PUBKEY", str(pin))
    return {"scripts": scripts, "state": state, "mode": mode, "pin": pin}


def test_all_missing_yields_nulls(bundle_env):
    status = station_api._bundle_status()
    assert status == {
        "version": None,
        "build": None,
        "channel": None,
        "verify_mode": None,
        "last_verify": None,
        "verified_at": None,
        "pin_fp": None,
        "update": None,
    }


def test_fully_populated(bundle_env):
    (bundle_env["scripts"] / ".version").write_text("dev.276.2026.07.03\n")
    bundle_env["state"].write_text(json.dumps({
        "channel": "dev", "build": 276, "version": "dev.276.2026.07.03",
        "last_verify": "ok", "verified_at": "2026-07-03T14:00:00+00:00",
        "mode": "warn",
    }))
    bundle_env["mode"].write_text("warn\n")
    pin_bytes = b"-----BEGIN PUBLIC KEY-----\nAAAA\n-----END PUBLIC KEY-----\n"
    bundle_env["pin"].write_bytes(pin_bytes)

    status = station_api._bundle_status()
    assert status["version"] == "dev.276.2026.07.03"
    assert status["build"] == 276
    assert status["channel"] == "dev"
    assert status["verify_mode"] == "warn"
    assert status["last_verify"] == "ok"
    assert status["verified_at"] == "2026-07-03T14:00:00+00:00"
    assert status["pin_fp"] == hashlib.sha256(pin_bytes).hexdigest()[:16]


def test_update_outcome_is_forwarded(bundle_env):
    """The updater's health verdict rides the same telemetry, so the dashboard
    shows an update whose services did not come back."""
    update = {"status": "unhealthy", "version": "550.2026.11.21",
              "at": "2026-11-21T14:05:00+00:00", "failed_services": ["color-capture"]}
    bundle_env["state"].write_text(json.dumps({"build": 550, "update": update}))
    assert station_api._bundle_status()["update"] == update


def test_non_dict_update_is_null(bundle_env):
    bundle_env["state"].write_text(json.dumps({"update": "garbage"}))
    assert station_api._bundle_status()["update"] is None


def test_enforce_mode_read(bundle_env):
    bundle_env["mode"].write_text("  ENFORCE \n")
    assert station_api._bundle_status()["verify_mode"] == "enforce"


def test_garbage_mode_normalises_to_warn(bundle_env):
    # An unrecognised mode value must not surface verbatim — the verifier
    # itself treats anything but "enforce" as warn, telemetry mirrors that.
    bundle_env["mode"].write_text("yolo\n")
    assert station_api._bundle_status()["verify_mode"] == "warn"


def test_corrupt_state_json_does_not_raise(bundle_env):
    bundle_env["state"].write_text("{not json")
    (bundle_env["scripts"] / ".version").write_text("281.2026.07.03")
    status = station_api._bundle_status()
    assert status["build"] is None
    assert status["last_verify"] is None
    assert status["version"] == "281.2026.07.03"


def test_empty_version_file_is_null(bundle_env):
    (bundle_env["scripts"] / ".version").write_text("")
    assert station_api._bundle_status()["version"] is None
