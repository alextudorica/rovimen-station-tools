"""Tests for the watchdog's signed-bundle checks (station_watchdog._check_bundle).

Three alerts ride the heartbeat `bundle` telemetry:
  * last_verify == "fail"          -> CRITICAL (enforce: updates blocked;
                                      warn: an unverified bundle was applied)
  * build lags the VPS channel AND no successful verify within
    bundle_stale_days                -> WARNING
  * verify_mode differs from expected_verify_mode (when configured)
                                     -> WARNING

Stations without telemetry (pre-rollout API) must stay silent.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import yaml

import station_watchdog as sw


class StubTracker:
    """Records report_problem/report_ok calls; enough surface for the checks."""

    def __init__(self) -> None:
        self.problems: dict[str, tuple[str, str, str]] = {}
        self.oks: list[str] = []

    def report_problem(
        self, key, severity, title, detail, host_key, station_label,
        grace_polls=1,
    ) -> None:
        self.problems[key] = (severity, title, detail)

    def report_ok(self, key) -> None:
        self.oks.append(key)


@pytest.fixture
def wcfg():
    return sw.WatchdogConfig()


@pytest.fixture
def vps_bundles(tmp_path, monkeypatch):
    """Fake /opt/rovimen bundle dirs; returns a writer for channel .version."""
    monkeypatch.setenv("ROVIMEN_BUNDLE_DIR", str(tmp_path))

    def _write(channel: str, version: str) -> None:
        d = tmp_path / ("station-bundle-dev" if channel == "dev" else "station-bundle")
        d.mkdir(exist_ok=True)
        (d / ".version").write_text(version + "\n")

    return _write


def _bundle(**over):
    base = {
        "version": "dev.276.2026.07.03", "build": 276, "channel": "dev",
        "verify_mode": "warn", "last_verify": "ok",
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "pin_fp": "ab12cd34ef56ab12",
    }
    base.update(over)
    return base


# ── _vps_channel_build ───────────────────────────────────────────────────────


def test_vps_build_dev_format(vps_bundles):
    vps_bundles("dev", "dev.300.2026.07.03")
    assert sw._vps_channel_build("dev") == 300


def test_vps_build_prod_format(vps_bundles):
    vps_bundles("main", "281.2026.07.03")
    assert sw._vps_channel_build("main") == 281


def test_vps_build_missing_file_is_none(vps_bundles):
    assert sw._vps_channel_build("dev") is None


def test_vps_build_garbage_is_none(vps_bundles):
    vps_bundles("dev", "not-a-version")
    assert sw._vps_channel_build("dev") is None


# ── _check_bundle: no telemetry ──────────────────────────────────────────────


def test_no_bundle_telemetry_stays_silent(wcfg, vps_bundles):
    tracker = StubTracker()
    sw._check_bundle("gmnro02", "Vaslui", {}, wcfg, tracker)
    assert tracker.problems == {}
    assert set(tracker.oks) == {
        "gmnro02:bundle_verify_failed",
        "gmnro02:bundle_stale",
        "gmnro02:bundle_verify_mode",
    }


# ── _check_bundle: verify failure ────────────────────────────────────────────


def test_verify_fail_is_critical(wcfg, vps_bundles):
    tracker = StubTracker()
    status = {"bundle": _bundle(last_verify="fail")}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    sev, title, _ = tracker.problems["gmnro02:bundle_verify_failed"]
    assert sev == sw.SEVERITY_CRITICAL
    assert "verify FAILED" in title


def test_verify_ok_reports_ok(wcfg, vps_bundles):
    tracker = StubTracker()
    sw._check_bundle("gmnro02", "Vaslui", {"bundle": _bundle()}, wcfg, tracker)
    assert "gmnro02:bundle_verify_failed" not in tracker.problems
    assert "gmnro02:bundle_verify_failed" in tracker.oks


# ── _check_bundle: staleness ─────────────────────────────────────────────────


def test_stale_when_build_lags_and_verify_old(wcfg, vps_bundles):
    vps_bundles("dev", "dev.300.2026.07.03")
    old = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()
    tracker = StubTracker()
    status = {"bundle": _bundle(build=276, verified_at=old)}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    sev, _, detail = tracker.problems["gmnro02:bundle_stale"]
    assert sev == sw.SEVERITY_WARNING
    assert "276" in detail and "300" in detail


def test_not_stale_when_build_current(wcfg, vps_bundles):
    vps_bundles("dev", "dev.276.2026.07.03")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    tracker = StubTracker()
    status = {"bundle": _bundle(build=276, verified_at=old)}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_stale" not in tracker.problems


def test_not_stale_when_lagging_but_recently_verified(wcfg, vps_bundles):
    # VPS just published a newer build; the station verified its current one
    # recently and simply hasn't pulled yet — normal update lag, no alert.
    vps_bundles("dev", "dev.300.2026.07.03")
    tracker = StubTracker()
    status = {"bundle": _bundle(build=276)}  # verified_at = now
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_stale" not in tracker.problems


def test_stale_when_lagging_and_never_verified(wcfg, vps_bundles):
    vps_bundles("dev", "dev.300.2026.07.03")
    tracker = StubTracker()
    status = {"bundle": _bundle(build=276, verified_at=None)}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_stale" in tracker.problems


def test_not_stale_when_vps_version_unreadable(wcfg, vps_bundles):
    # No baseline to compare against -> no alert (avoids false alarms when the
    # watchdog runs off-VPS or the bundle dir moves).
    tracker = StubTracker()
    status = {"bundle": _bundle(build=1)}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_stale" not in tracker.problems


# ── _check_bundle: verify-mode drift ─────────────────────────────────────────


def test_mode_drift_alerts_when_expectation_set(vps_bundles):
    wcfg = sw.WatchdogConfig(expected_verify_mode="enforce")
    tracker = StubTracker()
    status = {"bundle": _bundle(verify_mode="warn")}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    sev, _, detail = tracker.problems["gmnro02:bundle_verify_mode"]
    assert sev == sw.SEVERITY_WARNING
    assert "'warn'" in detail and "'enforce'" in detail


def test_mode_none_counts_as_drift(vps_bundles):
    # A station with telemetry but no mode file yet is NOT enforcing — that
    # must alert once the fleet expectation is enforce.
    wcfg = sw.WatchdogConfig(expected_verify_mode="enforce")
    tracker = StubTracker()
    status = {"bundle": _bundle(verify_mode=None)}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_verify_mode" in tracker.problems


def test_mode_match_is_ok(vps_bundles):
    wcfg = sw.WatchdogConfig(expected_verify_mode="enforce")
    tracker = StubTracker()
    status = {"bundle": _bundle(verify_mode="enforce")}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_verify_mode" not in tracker.problems


def test_no_expectation_no_drift_alert(wcfg, vps_bundles):
    # expected_verify_mode unset (rollout in progress) -> never alert on mode.
    tracker = StubTracker()
    status = {"bundle": _bundle(verify_mode="warn")}
    sw._check_bundle("gmnro02", "Vaslui", status, wcfg, tracker)
    assert "gmnro02:bundle_verify_mode" not in tracker.problems


# ── config plumbing ──────────────────────────────────────────────────────────


def test_load_watchdog_config_bundle_keys(tmp_path):
    p = tmp_path / "watchdog.yaml"
    p.write_text(yaml.safe_dump({
        "discord_webhook_url": "https://example.invalid/hook",
        "thresholds": {"bundle_stale_days": 7},
        "expected_verify_mode": "enforce",
    }))
    cfg = sw.load_watchdog_config(p)
    assert cfg.bundle_stale_days == 7
    assert cfg.expected_verify_mode == "enforce"


def test_load_watchdog_config_bundle_defaults(tmp_path):
    p = tmp_path / "watchdog.yaml"
    p.write_text(yaml.safe_dump({"discord_webhook_url": ""}))
    cfg = sw.load_watchdog_config(p)
    assert cfg.bundle_stale_days == 3
    assert cfg.expected_verify_mode == ""
