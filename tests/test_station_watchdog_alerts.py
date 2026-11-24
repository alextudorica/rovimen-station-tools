"""Tests for the fleet watchdog's alert transports and noise gates.

Enabling the watchdog on the live fleet only became safe once three things
held, and each is asserted here:

  * coverage — a commissioning or otherwise non-active station is never
    checked, and a muted host is checked but never alerts
  * startup grace — nothing at all leaves the process during its first N
    cycles, so the first run does not page for every station that is already
    degraded and a restart is quiet
  * transports — Discord and Telegram are independent; with neither
    configured the batch is logged and nothing is posted

The Telegram assertions pin the exact wire call (URL, chat, plain text with
no parse_mode) because the operator chat is a different destination from the
public channel the nightly summary posts to, and a silent regression there
would send fleet noise to the public.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml

import station_watchdog as sw
from models import DashboardConfig, StationConfig


# ── Helpers ──────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _reset_transport_pacing(monkeypatch):
    """Both senders pace themselves off a module-level last-send timestamp.

    Zeroing it keeps a test from sleeping out another test's rate limit.
    """
    monkeypatch.setattr(sw, "_last_telegram_ts", 0.0)
    monkeypatch.setattr(sw, "_last_discord_ts", 0.0)


def _config() -> DashboardConfig:
    return DashboardConfig(
        stations={
            "gmnro02": StationConfig(ip="100.0.0.2", label="Vaslui"),
            "gmnro10": StationConfig(ip="100.0.0.10", label="Chiribis"),
            "sarm": StationConfig(
                ip="100.0.0.99", label="Runcu Stone", status="commissioning"
            ),
        }
    )


def _problem(tracker: sw.AlertTracker, host_key: str = "gmnro02", **kw) -> None:
    tracker.report_problem(
        kw.get("key", f"{host_key}:services_down"),
        sw.SEVERITY_CRITICAL,
        f"{host_key} services down",
        "2 service(s) down: `color-capture`",
        host_key,
        kw.get("label", "Vaslui"),
        grace_polls=kw.get("grace_polls", 1),
    )


# ── Coverage: non-active stations are never checked ──────────────────────────


def test_commissioning_station_is_not_checked():
    checked, skipped, muted = sw.plan_coverage(_config(), sw.WatchdogConfig())
    assert checked == ["gmnro02", "gmnro10"]
    assert skipped == ["sarm"]
    assert muted == []


def test_unknown_status_is_not_checked():
    config = _config()
    config.stations["gmnro02"].status = "inactive"
    checked, skipped, _ = sw.plan_coverage(config, sw.WatchdogConfig())
    assert checked == ["gmnro10"]
    assert set(skipped) == {"gmnro02", "sarm"}


def test_muted_station_is_still_checked():
    wcfg = sw.WatchdogConfig(mute_stations=["gmnro10"])
    checked, _, muted = sw.plan_coverage(_config(), wcfg)
    assert "gmnro10" in checked, "a muted station is still polled and logged"
    assert muted == ["gmnro10"]


def test_run_cycle_only_visits_the_given_hosts():
    config = _config()
    wcfg = sw.WatchdogConfig(startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    visited: list[str] = []

    with patch.object(sw, "check_station", side_effect=lambda hk, *a: visited.append(hk)):
        sw._run_cycle(config, MagicMock(), wcfg, tracker, ["gmnro02"])

    assert visited == ["gmnro02"]


# ── Muting: checked and logged, never alerted ────────────────────────────────


def test_muted_station_never_alerts(caplog):
    wcfg = sw.WatchdogConfig(mute_stations=["gmnro10"], startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()

    with caplog.at_level(logging.INFO, logger="station_watchdog"):
        _problem(tracker, "gmnro10", label="Chiribis")

    assert tracker.drain_notifications() == []
    assert "[muted]" in caplog.text
    assert "gmnro10" in caplog.text


def test_muted_station_stays_silent_across_many_cycles():
    wcfg = sw.WatchdogConfig(mute_stations=["gmnro10"], startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    for _ in range(10):
        tracker.begin_cycle()
        _problem(tracker, "gmnro10", label="Chiribis")
    assert tracker.drain_notifications() == []
    # The problem is still tracked — only the notification is withheld.
    assert tracker.active_count == 1


def test_muted_station_does_not_announce_its_resolution():
    wcfg = sw.WatchdogConfig(
        mute_stations=["gmnro10"], startup_grace_polls=0, resolve_delay_polls=1
    )
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    _problem(tracker, "gmnro10", label="Chiribis")
    tracker.report_ok("gmnro10:services_down")
    assert tracker.drain_notifications() == []


def test_unmuted_station_still_alerts_normally():
    wcfg = sw.WatchdogConfig(mute_stations=["gmnro10"], startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    _problem(tracker, "gmnro02")
    notifs = tracker.drain_notifications()
    assert [n.entry.host_key for n in notifs] == ["gmnro02"]


# ── Startup grace: quiet for the first N cycles ──────────────────────────────


def test_startup_grace_suppresses_first_cycles(caplog):
    wcfg = sw.WatchdogConfig(startup_grace_polls=3)
    tracker = sw.AlertTracker(wcfg)

    with caplog.at_level(logging.INFO, logger="station_watchdog"):
        for _ in range(3):
            tracker.begin_cycle()
            _problem(tracker)
            assert tracker.drain_notifications() == []

    assert "[startup grace" in caplog.text


def test_alert_fires_on_the_first_cycle_after_the_grace_window():
    wcfg = sw.WatchdogConfig(startup_grace_polls=3)
    tracker = sw.AlertTracker(wcfg)
    for _ in range(3):
        tracker.begin_cycle()
        _problem(tracker)
        tracker.drain_notifications()

    tracker.begin_cycle()
    _problem(tracker)
    notifs = tracker.drain_notifications()
    assert len(notifs) == 1, "a standing problem must page once the window ends"
    assert notifs[0].kind == "alert"


def test_startup_grace_suppresses_info_and_resolve():
    wcfg = sw.WatchdogConfig(startup_grace_polls=2, resolve_delay_polls=1)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    _problem(tracker)
    tracker.report_info("Vaslui morning complete", "3 cameras", "gmnro02", "Vaslui")
    tracker.report_ok("gmnro02:services_down")
    assert tracker.drain_notifications() == []


def test_grace_does_not_burn_the_repeat_cooldown():
    # A problem suppressed during startup must not look "already alerted":
    # last_alerted stays unset so it pages immediately after the window.
    wcfg = sw.WatchdogConfig(startup_grace_polls=1, repeat_alert_minutes=120)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    _problem(tracker)
    assert tracker._active["gmnro02:services_down"].last_alerted is None
    tracker.drain_notifications()

    tracker.begin_cycle()
    _problem(tracker)
    assert len(tracker.drain_notifications()) == 1


def test_disk_and_services_wait_three_polls_by_default():
    wcfg = sw.WatchdogConfig()
    assert wcfg.services_grace_polls == 3
    assert wcfg.disk_grace_polls == 3

    quiet = sw.WatchdogConfig(startup_grace_polls=0)
    tracker = sw.AlertTracker(quiet)
    for cycle in range(1, 4):
        tracker.begin_cycle()
        _problem(tracker, grace_polls=3)
        got = tracker.drain_notifications()
        assert bool(got) is (cycle == 3), f"cycle {cycle} should not page"


# ── Telegram transport ───────────────────────────────────────────────────────


def _alert_message() -> sw.AlertMessage:
    return sw.AlertMessage(
        severity=sw.SEVERITY_CRITICAL,
        title="CRITICAL: Vaslui (gmnro02)",
        lines=["2 service(s) down: `color-capture`"],
        footer="ROVIMEN Watchdog | 1 issue(s)",
    )


def test_telegram_text_is_plain():
    text = sw.render_telegram_text(_alert_message())
    assert text.splitlines() == [
        "CRITICAL: Vaslui (gmnro02)",
        "• 2 service(s) down: color-capture",
        "ROVIMEN Watchdog | 1 issue(s)",
    ]
    assert "`" not in text and "~~" not in text


def test_telegram_send_posts_expected_payload():
    with patch.object(sw.requests, "post") as post:
        post.return_value = MagicMock(status_code=200)
        assert sw.send_telegram_message("BOT:TOKEN", "-100999", "hello") is True

    url, = post.call_args.args
    assert url == "https://api.telegram.org/botBOT:TOKEN/sendMessage"
    data = post.call_args.kwargs["data"]
    assert data["chat_id"] == "-100999"
    assert data["text"] == "hello"
    # No parse_mode: a station error string may contain any character and an
    # unescaped one would make Telegram reject the whole alert.
    assert "parse_mode" not in data
    assert post.call_args.kwargs["timeout"] == 10


def test_telegram_send_needs_both_token_and_chat():
    with patch.object(sw.requests, "post") as post:
        assert sw.send_telegram_message("", "-100999", "x") is False
        assert sw.send_telegram_message("BOT:TOKEN", "", "x") is False
    post.assert_not_called()


def test_telegram_rejection_is_reported():
    with patch.object(sw.requests, "post") as post:
        post.return_value = MagicMock(status_code=400, text="Bad Request")
        assert sw.send_telegram_message("BOT:TOKEN", "-100999", "x") is False


def test_dispatch_sends_telegram_when_configured():
    wcfg = sw.WatchdogConfig(telegram_bot_token="BOT:TOKEN", telegram_chat_id="-100999")
    entry = sw.AlertEntry(
        alert_key="k", severity=sw.SEVERITY_CRITICAL,
        title="Vaslui services down", detail="2 service(s) down: `color-capture`",
        host_key="gmnro02", station_label="Vaslui",
    )

    with patch.object(sw, "send_telegram_message", return_value=True) as tg, \
            patch.object(sw, "send_discord_embed") as discord:
        sent = sw.dispatch_notifications_batched(wcfg, [sw.Notification(entry, "alert")])

    assert sent == 1
    discord.assert_not_called()
    token, chat_id, text = tg.call_args.args
    assert (token, chat_id) == ("BOT:TOKEN", "-100999")
    assert text.startswith("CRITICAL: Vaslui (gmnro02)")
    assert "color-capture" in text


def test_dispatch_sends_both_transports_independently():
    wcfg = sw.WatchdogConfig(
        discord_webhook_url="https://example.invalid/hook",
        telegram_bot_token="BOT:TOKEN", telegram_chat_id="-100999",
    )
    entry = sw.AlertEntry(
        alert_key="k", severity=sw.SEVERITY_WARNING,
        title="Vaslui disk warning", detail="`/mnt/data` at 88%",
        host_key="gmnro02", station_label="Vaslui",
    )

    with patch.object(sw, "send_telegram_message", return_value=True) as tg, \
            patch.object(sw, "send_discord_embed", return_value=True) as discord:
        sw.dispatch_notifications_batched(wcfg, [sw.Notification(entry, "alert")])

    assert tg.call_count == 1
    assert discord.call_count == 1
    # The Discord rendering is unchanged: severity emoji, bullets, markup.
    _, title, description, *_ = discord.call_args.args
    assert title == f"{sw.SEVERITY_EMOJI[sw.SEVERITY_WARNING]} WARNING: Vaslui (gmnro02)"
    assert description == "• `/mnt/data` at 88%"


def test_dispatch_with_no_transport_is_log_only(caplog):
    wcfg = sw.WatchdogConfig()
    entry = sw.AlertEntry(
        alert_key="k", severity=sw.SEVERITY_CRITICAL,
        title="Vaslui unreachable", detail="Station API not responding",
        host_key="gmnro02", station_label="Vaslui",
    )

    with patch.object(sw.requests, "post") as post:
        with caplog.at_level(logging.INFO, logger="station_watchdog"):
            sent = sw.dispatch_notifications_batched(wcfg, [sw.Notification(entry, "alert")])

    post.assert_not_called()
    assert sent == 1
    assert "[log-only]" in caplog.text
    assert "Station API not responding" in caplog.text


# ── Config plumbing ──────────────────────────────────────────────────────────


def _write(tmp_path, payload: dict):
    p = tmp_path / "watchdog.yaml"
    p.write_text(yaml.safe_dump(payload))
    return p


def test_config_reads_new_keys(tmp_path):
    cfg = sw.load_watchdog_config(_write(tmp_path, {
        "telegram_bot_token": "FILE:TOKEN",
        "telegram_chat_id": "-100111",
        "startup_grace_polls": 5,
        "mute_stations": ["gmnro10", "gmnro11"],
        "cooldowns": {"services_grace_polls": 4, "disk_grace_polls": 2},
    }))
    assert cfg.telegram_bot_token == "FILE:TOKEN"
    assert cfg.telegram_chat_id == "-100111"
    assert cfg.startup_grace_polls == 5
    assert cfg.mute_stations == ["gmnro10", "gmnro11"]
    assert cfg.services_grace_polls == 4
    assert cfg.disk_grace_polls == 2


def test_config_defaults_for_new_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("ROVIMEN_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID", raising=False)
    cfg = sw.load_watchdog_config(_write(tmp_path, {}))
    assert cfg.telegram_bot_token == ""
    assert cfg.telegram_chat_id == ""
    assert cfg.startup_grace_polls == 3
    assert cfg.mute_stations == []
    assert cfg.services_grace_polls == 3
    assert cfg.disk_grace_polls == 3


def test_env_supplies_telegram_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVIMEN_TELEGRAM_BOT_TOKEN", "ENV:TOKEN")
    monkeypatch.setenv("ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID", "-100222")
    monkeypatch.setenv("ROVIMEN_WATCHDOG_WEBHOOK", "https://example.invalid/env")
    cfg = sw.load_watchdog_config(_write(tmp_path, {}))
    assert cfg.telegram_bot_token == "ENV:TOKEN"
    assert cfg.telegram_chat_id == "-100222"
    assert cfg.discord_webhook_url == "https://example.invalid/env"


def test_config_file_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID", "-100222")
    cfg = sw.load_watchdog_config(_write(tmp_path, {"telegram_chat_id": "-100333"}))
    assert cfg.telegram_chat_id == "-100333"


def test_numeric_chat_id_is_coerced_to_str(tmp_path):
    # YAML parses a bare -100999 as an int; requests would still send it, but
    # every comparison downstream expects a string.
    cfg = sw.load_watchdog_config(_write(tmp_path, {"telegram_chat_id": -100999}))
    assert cfg.telegram_chat_id == "-100999"


def test_example_config_parses_and_matches_defaults():
    example = (
        Path(__file__).resolve().parent.parent
        / "dashboard" / "watchdog_config.example.yaml"
    )
    cfg = sw.load_watchdog_config(example)
    defaults = sw.WatchdogConfig()
    assert cfg.startup_grace_polls == defaults.startup_grace_polls
    assert cfg.services_grace_polls == defaults.services_grace_polls
    assert cfg.disk_grace_polls == defaults.disk_grace_polls
    assert cfg.mute_stations == []
    assert cfg.poll_interval_s == defaults.poll_interval_s
    assert cfg.repeat_alert_minutes == defaults.repeat_alert_minutes


# ── Janitor disk pressure ────────────────────────────────────────────────────

_PRESSURE = {"since": "2026-10-16T18:00:02+00:00", "runs": 25,
             "filesystem": "/home/gmn/color_capture", "pct": 98, "free_gb": 3.6}


def test_unrelieved_janitor_pressure_alerts_after_the_disk_grace():
    wcfg = sw.WatchdogConfig(startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    for _ in range(wcfg.disk_grace_polls):
        sw._check_disk_pressure("gmnro14", "Chiribiș", {"pressure": _PRESSURE}, wcfg, tracker)
    alert = tracker._active["gmnro14:disk_pressure"]
    assert alert.severity == sw.SEVERITY_WARNING
    assert "98%" in alert.detail and "3.6 GB free" in alert.detail and "25 janitor runs" in alert.detail


def test_relieved_janitor_clears_the_pressure_alert():
    wcfg = sw.WatchdogConfig(startup_grace_polls=0)
    tracker = sw.AlertTracker(wcfg)
    tracker.begin_cycle()
    for _ in range(wcfg.disk_grace_polls):
        sw._check_disk_pressure("gmnro14", "Chiribiș", {"pressure": _PRESSURE}, wcfg, tracker)
    for _ in range(wcfg.resolve_delay_polls):
        sw._check_disk_pressure("gmnro14", "Chiribiș", {"pressure": None}, wcfg, tracker)
    assert "gmnro14:disk_pressure" not in tracker._active
    assert [n.kind for n in tracker.drain_notifications()] == ["alert", "resolve"]
