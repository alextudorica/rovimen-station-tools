#!/usr/bin/env python3
"""station_watchdog.py — Station health monitor with Discord/Telegram alerts.

Polls the active stations every N minutes via their REST APIs and alerts on
state transitions (new problem, resolved) over whichever transports are
configured: a Discord webhook (color-coded embeds), a Telegram operator chat
(plain text), both, or neither — with neither it is a log-only journal.

Noise control, in the order it applies:
  * only registry-``active`` stations are checked at all
  * ``mute_stations`` hosts are checked and logged but never alert
  * nothing alerts during the first ``startup_grace_polls`` cycles, so a
    restart never replays the fleet's standing problems

Runs on the VPS as a systemd service alongside rovimen-dashboard.

Environment (from EnvironmentFile=/etc/rovimen-dashboard.env):
    ROVIMEN_TELEGRAM_BOT_TOKEN          bot the nightly summary already uses
    ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID   operator chat — NOT the public channel
    ROVIMEN_WATCHDOG_WEBHOOK            Discord webhook, if one is used

Usage:
    python3 station_watchdog.py [--config dashboard_config.yaml] [--watchdog-config watchdog_config.yaml]
"""

import argparse
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests
import yaml

from rovimen_dashboard import (
    DashboardConfig,
    TunnelManager,
    load_config,
    station_get_status,
    station_url,
)

logger = logging.getLogger("station_watchdog")

SEVERITY_CRITICAL = "critical"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

EMBED_COLORS = {
    SEVERITY_CRITICAL: 0xFF0000,
    SEVERITY_WARNING: 0xFF8C00,
    SEVERITY_INFO: 0x00CC00,
}

SEVERITY_EMOJI = {
    SEVERITY_CRITICAL: "\U0001f6a8",
    SEVERITY_WARNING: "⚠️",
    SEVERITY_INFO: "✅",
}


def service_state_is_up(state: object) -> bool:
    """True if a /api/status service value means "the work is running".

    The station API reports "active" for a service running under an active
    systemd unit and "active (unmanaged)" when it found the process running
    without one (legacy nohup/cron installs, or a unit whose name the API
    could not resolve). Both are up; only "inactive" / "failed" are not.
    """
    if isinstance(state, str):
        return state.startswith("active")
    return bool(state)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class WatchdogConfig:
    discord_webhook_url: str = ""
    # Telegram transport. The token is the same bot the nightly summary posts
    # with (ROVIMEN_TELEGRAM_BOT_TOKEN); the chat is deliberately NOT the
    # public "Meteori România" channel — fleet alerts go to an operator chat
    # named by ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID.
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    poll_interval_s: int = 300
    disk_warn_pct: int = 85
    disk_critical_pct: int = 95
    janitor_stale_hours: int = 26
    dawn_deadline_utc: str = "12:00"
    bundle_stale_days: int = 3
    repeat_alert_minutes: int = 120
    resolve_delay_polls: int = 2
    # Polls a problem must persist before it pages. Services and disks are
    # both prone to a single-poll blip (a unit mid-restart, a disk that a
    # janitor pass is about to drain), so neither pages on first sight.
    services_grace_polls: int = 3
    disk_grace_polls: int = 3
    # No alert of any kind leaves the process during its first N cycles. A
    # restart therefore never replays the fleet's standing problems, and the
    # very first run does not page for every station that is already degraded.
    startup_grace_polls: int = 3
    # Host keys that are checked and logged but never alert — long-known-dead
    # stations, or ones under maintenance. Alerting stays off until the entry
    # is removed, so the fleet's real problems are not buried.
    mute_stations: list[str] = field(default_factory=list)
    dawn_check_start: str = "08:00"
    dawn_check_end: str = "14:00"
    # When set (e.g. "enforce"), any station whose heartbeat reports a different
    # bundle verify_mode raises a warning. Empty string disables the check —
    # keep it unset until the fleet-wide enforce cutover is complete.
    expected_verify_mode: str = ""


def load_watchdog_config(path: Path) -> WatchdogConfig:
    # An empty or comment-only file parses to None; treat it as "all defaults"
    # rather than letting the .get() calls below raise AttributeError.
    raw = yaml.safe_load(path.read_text()) or {}
    cfg = WatchdogConfig()
    # Secrets: the config file wins when it carries a value, otherwise the
    # service environment (EnvironmentFile=/etc/rovimen-dashboard.env) does.
    # The env fallback is what the unit has always advertised in its comments,
    # and it keeps tokens out of a second on-disk copy.
    cfg.discord_webhook_url = (
        raw.get("discord_webhook_url") or os.environ.get("ROVIMEN_WATCHDOG_WEBHOOK", "")
    )
    cfg.telegram_bot_token = (
        raw.get("telegram_bot_token") or os.environ.get("ROVIMEN_TELEGRAM_BOT_TOKEN", "")
    )
    cfg.telegram_chat_id = str(
        raw.get("telegram_chat_id")
        or os.environ.get("ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID", "")
    )
    cfg.poll_interval_s = raw.get("poll_interval_s", 300)
    cfg.startup_grace_polls = raw.get("startup_grace_polls", 3)
    cfg.mute_stations = list(raw.get("mute_stations") or [])
    t = raw.get("thresholds", {})
    cfg.disk_warn_pct = t.get("disk_warn_pct", 85)
    cfg.disk_critical_pct = t.get("disk_critical_pct", 95)
    cfg.janitor_stale_hours = t.get("janitor_stale_hours", 26)
    cfg.dawn_deadline_utc = t.get("dawn_deadline_utc", "12:00")
    cfg.bundle_stale_days = t.get("bundle_stale_days", 3)
    cfg.expected_verify_mode = raw.get("expected_verify_mode", "")
    c = raw.get("cooldowns", {})
    cfg.repeat_alert_minutes = c.get("repeat_alert_minutes", 120)
    cfg.resolve_delay_polls = c.get("resolve_delay_polls", 2)
    cfg.services_grace_polls = c.get("services_grace_polls", 3)
    cfg.disk_grace_polls = c.get("disk_grace_polls", 3)
    w = raw.get("dawn_check_window_utc", {})
    cfg.dawn_check_start = w.get("start", "08:00")
    cfg.dawn_check_end = w.get("end", "14:00")
    return cfg


def plan_coverage(
    config: DashboardConfig, wcfg: WatchdogConfig
) -> tuple[list[str], list[str], list[str]]:
    """Split the registry into (checked, skipped, muted) host keys.

    Only stations whose registry status is ``active`` are checked at all: a
    commissioning box is half-built by definition and an inactive one is not
    expected to answer, so neither may page anyone. Muted stations are still
    checked (their state stays visible in the log) but never alert; they are
    part of ``checked`` and are listed again in ``muted``.
    """
    muted_keys = set(wcfg.mute_stations)
    checked: list[str] = []
    skipped: list[str] = []
    muted: list[str] = []
    for host_key, station in config.stations.items():
        if getattr(station, "status", "active") != "active":
            skipped.append(host_key)
            continue
        checked.append(host_key)
        if host_key in muted_keys:
            muted.append(host_key)
    return checked, skipped, muted


# ---------------------------------------------------------------------------
# Alert state tracking
# ---------------------------------------------------------------------------


@dataclass
class AlertEntry:
    alert_key: str
    severity: str
    title: str
    detail: str
    host_key: str
    station_label: str
    first_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_alerted: datetime | None = None
    ok_polls: int = 0
    problem_polls: int = 0
    grace_polls: int = 1


@dataclass
class Notification:
    entry: AlertEntry
    kind: str  # "alert", "resolve", or "info"


class AlertTracker:
    def __init__(self, wcfg: WatchdogConfig) -> None:
        self._wcfg = wcfg
        self._active: dict[str, AlertEntry] = {}
        self._pending: list[Notification] = []
        self._notified_dawn: dict[str, datetime] = {}
        self._muted: set[str] = set(wcfg.mute_stations)
        self._cycles = 0

    def begin_cycle(self) -> None:
        """Advance the cycle counter — call once per poll, before the checks."""
        self._cycles += 1

    @property
    def in_startup_grace(self) -> bool:
        """True while the process is still inside its quiet startup window."""
        return self._cycles <= self._wcfg.startup_grace_polls

    def _emit(self, entry: AlertEntry, kind: str) -> bool:
        """Queue a notification unless it is muted or inside startup grace.

        Returns whether it was queued, so the caller can leave ``last_alerted``
        unset on a suppressed alert: the problem then pages on the first cycle
        after the grace window instead of waiting out a repeat cooldown it
        never actually spent.
        """
        summary = f"{entry.title} — {entry.detail}" if entry.detail else entry.title
        if entry.host_key in self._muted:
            logger.info("[muted] %s (%s) %s: %s", entry.station_label,
                        entry.host_key, kind, summary)
            return False
        if self.in_startup_grace:
            logger.info(
                "[startup grace %d/%d] %s (%s) %s: %s",
                self._cycles, self._wcfg.startup_grace_polls,
                entry.station_label, entry.host_key, kind, summary,
            )
            return False
        self._pending.append(Notification(entry, kind))
        return True

    def report_problem(
        self,
        key: str,
        severity: str,
        title: str,
        detail: str,
        host_key: str,
        station_label: str,
        grace_polls: int = 1,
    ) -> None:
        now = datetime.now(timezone.utc)
        existing = self._active.get(key)

        if existing:
            existing.ok_polls = 0
            existing.problem_polls += 1
            existing.severity = severity
            existing.detail = detail
            existing.title = title
            if existing.last_alerted is None and existing.problem_polls >= existing.grace_polls:
                if self._emit(existing, "alert"):
                    existing.last_alerted = now
            elif existing.last_alerted is not None:
                cooldown = timedelta(minutes=self._wcfg.repeat_alert_minutes)
                if now - existing.last_alerted >= cooldown:
                    if self._emit(existing, "alert"):
                        existing.last_alerted = now
            return

        entry = AlertEntry(
            alert_key=key,
            severity=severity,
            title=title,
            detail=detail,
            host_key=host_key,
            station_label=station_label,
            first_seen=now,
            problem_polls=1,
            grace_polls=grace_polls,
        )
        self._active[key] = entry
        if grace_polls <= 1 and self._emit(entry, "alert"):
            entry.last_alerted = now

    def report_info(self, title: str, detail: str, host_key: str, station_label: str) -> None:
        entry = AlertEntry(
            alert_key="", severity=SEVERITY_INFO,
            title=title, detail=detail,
            host_key=host_key, station_label=station_label,
        )
        self._emit(entry, "info")

    def report_ok(self, key: str) -> None:
        existing = self._active.get(key)
        if existing is None:
            return
        existing.ok_polls += 1
        if existing.ok_polls >= self._wcfg.resolve_delay_polls:
            del self._active[key]
            # A problem that never paged (muted, or raised inside the startup
            # grace) must not announce its own resolution either.
            if existing.last_alerted is not None:
                self._emit(existing, "resolve")

    def drain_notifications(self) -> list[Notification]:
        pending = self._pending
        self._pending = []
        return pending

    @property
    def active_count(self) -> int:
        return len(self._active)


# ---------------------------------------------------------------------------
# Discord messaging
# ---------------------------------------------------------------------------

_last_discord_ts: float = 0.0


def send_discord_embed(
    webhook_url: str,
    title: str,
    description: str,
    color: int,
    footer: str = "",
) -> bool:
    global _last_discord_ts
    if not webhook_url:
        logger.warning("No Discord webhook URL — skipping: %s", title)
        return False

    elapsed = time.time() - _last_discord_ts
    if elapsed < 2.0:
        time.sleep(2.0 - elapsed)

    embed: dict[str, Any] = {
        "title": title,
        "description": description,
        "color": color,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if footer:
        embed["footer"] = {"text": footer}

    # Tight timeout (P1-16): Discord webhook calls run inside the
    # watchdog poll loop. A black-holed webhook URL with no timeout
    # would hang every cycle indefinitely and silently freeze health
    # checks for the entire fleet. Ten seconds is generous enough for
    # a real slow upstream, short enough to recover within a single
    # poll window. The broad except below already prevents a webhook
    # failure from propagating out of this function.
    try:
        resp = requests.post(webhook_url, json={"embeds": [embed]}, timeout=10)
        _last_discord_ts = time.time()
        ok = resp.status_code in (200, 204)
        if not ok:
            logger.error("Discord returned %d: %s", resp.status_code, resp.text[:200])
        return ok
    except requests.Timeout:
        # Distinguished log line so an operator can tell "Discord slow"
        # from any other failure mode without grep'ing stack traces.
        logger.warning("Discord webhook timed out after 10s; skipping: %s", title)
        return False
    except Exception:
        logger.exception("Failed to send Discord embed")
        return False


@dataclass
class AlertMessage:
    """One batched alert, rendered independently by each transport.

    ``title`` carries no severity emoji — a transport prepends
    ``SEVERITY_EMOJI[severity]`` if its rendering wants one. ``lines`` hold
    the bare detail text with Discord-flavoured markup (backticks around
    paths/unit names, ``~~`` around a resolved title); a plain-text transport
    strips it. Bullets are added at render time, not stored here.
    """

    severity: str
    title: str
    lines: list[str]
    footer: str


def build_alert_messages(notifications: list[Notification]) -> list[AlertMessage]:
    """Group notifications by (host_key, kind) — one message per group.

    Batching per station keeps a station with six simultaneous problems to a
    single notification instead of six.
    """
    from collections import defaultdict

    groups: dict[tuple[str, str], list[Notification]] = defaultdict(list)
    for notif in notifications:
        groups[(notif.entry.host_key, notif.kind)].append(notif)

    messages: list[AlertMessage] = []
    for (host_key, kind), group in groups.items():
        label = group[0].entry.station_label
        if kind == "alert":
            worst = SEVERITY_WARNING
            lines = []
            for notif in group:
                if notif.entry.severity == SEVERITY_CRITICAL:
                    worst = SEVERITY_CRITICAL
                lines.append(notif.entry.detail)
            messages.append(AlertMessage(
                severity=worst,
                title=f"{worst.upper()}: {label} ({host_key})",
                lines=lines,
                footer=f"ROVIMEN Watchdog | {len(group)} issue(s)",
            ))
        elif kind == "info":
            messages.append(AlertMessage(
                severity=SEVERITY_INFO,
                title=f"{label} ({host_key})",
                lines=[notif.entry.detail for notif in group],
                footer="ROVIMEN Watchdog",
            ))
        else:
            now = datetime.now(timezone.utc)
            lines = []
            for notif in group:
                duration = now - notif.entry.first_seen
                hours, remainder = divmod(int(duration.total_seconds()), 3600)
                minutes = remainder // 60
                dur_str = f"{hours}h {minutes}m" if hours else f"{minutes}m"
                lines.append(f"~~{notif.entry.title}~~ ({dur_str})")
            messages.append(AlertMessage(
                severity=SEVERITY_INFO,
                title=f"RESOLVED: {label} ({host_key})",
                lines=lines,
                footer=f"ROVIMEN Watchdog | {len(group)} resolved",
            ))
    return messages


def send_discord_message(webhook_url: str, msg: AlertMessage) -> bool:
    return send_discord_embed(
        webhook_url,
        f"{SEVERITY_EMOJI[msg.severity]} {msg.title}",
        "\n".join(f"• {line}" for line in msg.lines),
        EMBED_COLORS[msg.severity],
        msg.footer,
    )


# ---------------------------------------------------------------------------
# Telegram messaging
# ---------------------------------------------------------------------------

TELEGRAM_API = "https://api.telegram.org"

_last_telegram_ts: float = 0.0


def _strip_markup(text: str) -> str:
    """Drop the Discord-only markup so a detail line reads as plain text."""
    return text.replace("~~", "").replace("`", "")


def render_telegram_text(msg: AlertMessage) -> str:
    """Plain-text rendering of a batched alert.

    Sent without a parse_mode: a station API error string can carry any
    character, and an unescaped ``<`` or ``&`` would make Telegram reject the
    whole message — losing the alert exactly when something is wrong.
    """
    lines = [msg.title]
    lines += [f"• {_strip_markup(line)}" for line in msg.lines]
    if msg.footer:
        lines.append(msg.footer)
    return "\n".join(lines)


def send_telegram_message(bot_token: str, chat_id: str, text: str) -> bool:
    """Post one alert to the operator chat. Same API shape as nightly_telegram."""
    global _last_telegram_ts
    if not (bot_token and chat_id):
        return False

    # Telegram throttles a chat at roughly 20 messages a minute; a fleet-wide
    # bad night can queue one message per station in a single cycle.
    elapsed = time.time() - _last_telegram_ts
    if elapsed < 1.5:
        time.sleep(1.5 - elapsed)

    try:
        resp = requests.post(
            f"{TELEGRAM_API}/bot{bot_token}/sendMessage",
            data={
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        _last_telegram_ts = time.time()
        if resp.status_code != 200:
            logger.error("Telegram returned %d: %s", resp.status_code, resp.text[:200])
            return False
        return True
    except requests.Timeout:
        logger.warning("Telegram send timed out after 10s; skipping: %s", text[:80])
        return False
    except Exception:
        logger.exception("Failed to send Telegram alert")
        return False


def dispatch_notifications_batched(wcfg: WatchdogConfig, notifications: list[Notification]) -> int:
    """Send one message per (host_key, kind) group over every configured
    transport. Returns the number of messages dispatched.

    Discord and Telegram are independent: either, both, or neither may be
    configured. With neither, the batch is logged and nothing is sent — the
    watchdog stays useful as a journal without an alert destination.
    """
    telegram_ready = bool(wcfg.telegram_bot_token and wcfg.telegram_chat_id)
    messages = build_alert_messages(notifications)
    for msg in messages:
        if wcfg.discord_webhook_url:
            send_discord_message(wcfg.discord_webhook_url, msg)
        if telegram_ready:
            send_telegram_message(
                wcfg.telegram_bot_token, wcfg.telegram_chat_id,
                render_telegram_text(msg),
            )
        if not (wcfg.discord_webhook_url or telegram_ready):
            logger.info(
                "[log-only] %s | %s",
                msg.title, "; ".join(_strip_markup(line) for line in msg.lines),
            )
    return len(messages)


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------


def check_station(
    host_key: str,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    station = config.stations[host_key]
    label = station.label

    status = station_get_status(config, tunnels, host_key, "/api/status")

    key_reach = f"{host_key}:unreachable"
    if not status.get("online"):
        tracker.report_problem(
            key_reach, SEVERITY_CRITICAL,
            f"{label} unreachable",
            f"Station API not responding: {status.get('error', 'unknown')}",
            host_key, label,
            grace_polls=3,
        )
        return
    tracker.report_ok(key_reach)

    key_api = f"{host_key}:api_error"
    if "error" in status and status.get("online"):
        tracker.report_problem(
            key_api, SEVERITY_WARNING,
            f"{label} API error",
            f"Status endpoint error: {status['error']}",
            host_key, label,
            grace_polls=3,
        )
    else:
        tracker.report_ok(key_api)

    # Services — values are "active" / "active (unmanaged)" / "inactive" /
    # "failed" strings. "active (unmanaged)" means the station API found the
    # work running without an active systemd unit behind it (legacy nohup or
    # per-user installs): up, so not an alert.
    services = status.get("services", {})
    down_svcs = []
    for svc_name, svc_state in services.items():
        if not service_state_is_up(svc_state):
            down_svcs.append(svc_name)

    key_svc = f"{host_key}:services_down"
    if down_svcs:
        svcs_str = ", ".join(f"`{s}`" for s in down_svcs)
        tracker.report_problem(
            key_svc, SEVERITY_CRITICAL,
            f"{label} services down",
            f"{len(down_svcs)} service(s) down: {svcs_str}",
            host_key, label,
            grace_polls=wcfg.services_grace_polls,
        )
    else:
        tracker.report_ok(key_svc)

    # Disk usage — main disk from status['disk'] and extra mounts from status['extra_disks']
    disks_to_check: list[tuple[str, int]] = []
    main_disk = status.get("disk", {})
    if isinstance(main_disk, dict) and "pct" in main_disk:
        device = main_disk.get("device", "root")
        disks_to_check.append((device, main_disk["pct"]))
    for ed in status.get("extra_disks", []):
        if isinstance(ed, dict) and "pct" in ed:
            mount = ed.get("mount", ed.get("source", "unknown"))
            disks_to_check.append((mount, ed["pct"]))

    for mount, pct in disks_to_check:
        key_disk = f"{host_key}:disk:{mount}"
        if pct >= wcfg.disk_critical_pct:
            tracker.report_problem(
                key_disk, SEVERITY_CRITICAL,
                f"{label} disk critical",
                f"`{mount}` at {pct}%",
                host_key, label,
                grace_polls=wcfg.disk_grace_polls,
            )
        elif pct >= wcfg.disk_warn_pct:
            tracker.report_problem(
                key_disk, SEVERITY_WARNING,
                f"{label} disk warning",
                f"`{mount}` at {pct}%",
                host_key, label,
                grace_polls=wcfg.disk_grace_polls,
            )
        else:
            tracker.report_ok(key_disk)

    # Bundle integrity (signed-update telemetry from /api/status)
    _check_bundle(host_key, label, status, wcfg, tracker)

    # Janitor staleness
    _check_janitor(host_key, label, config, tunnels, wcfg, tracker)

    # Dawn completeness (only during the check window)
    _check_dawn(host_key, label, station, config, tunnels, wcfg, tracker)


def _vps_channel_build(channel: str) -> int | None:
    """Current build number of the VPS bundle for a channel, or None.

    Reads the same `.version` file CI writes into the bundle dir. Version
    formats: dev = `dev.<run>.<date>` (build = parts[1]), prod = `<run>.<date>`
    (build = parts[0]). ROVIMEN_BUNDLE_DIR overrides the base dir for tests.
    """
    base = Path(os.environ.get("ROVIMEN_BUNDLE_DIR", "/opt/rovimen"))
    bundle_dir = "station-bundle-dev" if channel == "dev" else "station-bundle"
    try:
        version = (base / bundle_dir / ".version").read_text().strip()
        parts = version.split(".")
        return int(parts[1]) if parts[0] == "dev" else int(parts[0])
    except (OSError, ValueError, IndexError):
        return None


def _check_bundle(
    host_key: str,
    label: str,
    status: dict,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    bundle = status.get("bundle")
    key_fail = f"{host_key}:bundle_verify_failed"
    key_stale = f"{host_key}:bundle_stale"
    key_mode = f"{host_key}:bundle_verify_mode"

    if not isinstance(bundle, dict):
        # Pre-PR3 station API — no telemetry yet, nothing to judge.
        tracker.report_ok(key_fail)
        tracker.report_ok(key_stale)
        tracker.report_ok(key_mode)
        return

    # A failed verify is the headline alert: in enforce it means updates are
    # blocked; in warn it means an unverified bundle was applied.
    if bundle.get("last_verify") == "fail":
        tracker.report_problem(
            key_fail, SEVERITY_CRITICAL,
            f"{label} bundle verify FAILED",
            f"Last bundle verification failed (mode: {bundle.get('verify_mode')}, "
            f"build: {bundle.get('build')}, channel: {bundle.get('channel')})",
            host_key, label,
        )
    else:
        tracker.report_ok(key_fail)

    # Staleness: station build lags the VPS channel build AND the last
    # successful verify is older than the threshold.
    stale = False
    channel = bundle.get("channel")
    vps_build = _vps_channel_build(channel) if channel else None
    station_build = bundle.get("build")
    if (
        vps_build is not None
        and isinstance(station_build, int)
        and station_build < vps_build
    ):
        verified_at = bundle.get("verified_at")
        age_ok = False
        if verified_at:
            try:
                v_dt = datetime.fromisoformat(verified_at)
                if v_dt.tzinfo is None:
                    v_dt = v_dt.replace(tzinfo=timezone.utc)
                age_ok = datetime.now(timezone.utc) - v_dt <= timedelta(
                    days=wcfg.bundle_stale_days
                )
            except (ValueError, TypeError):
                age_ok = False
        stale = not age_ok
    if stale:
        tracker.report_problem(
            key_stale, SEVERITY_WARNING,
            f"{label} bundle stale",
            f"Station on build {station_build}, VPS {channel} channel at "
            f"{vps_build}; no successful verify in {wcfg.bundle_stale_days}d",
            host_key, label,
        )
    else:
        tracker.report_ok(key_stale)

    # Verify-mode drift (only when an expectation is configured).
    if wcfg.expected_verify_mode:
        if bundle.get("verify_mode") != wcfg.expected_verify_mode:
            tracker.report_problem(
                key_mode, SEVERITY_WARNING,
                f"{label} bundle verify mode drift",
                f"verify_mode is {bundle.get('verify_mode')!r}, expected "
                f"{wcfg.expected_verify_mode!r}",
                host_key, label,
            )
        else:
            tracker.report_ok(key_mode)
    else:
        tracker.report_ok(key_mode)


def _check_janitor(
    host_key: str,
    label: str,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    key = f"{host_key}:janitor_stale"
    try:
        url = station_url(config, tunnels, host_key, "/api/storagewatch/status")
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception:
        return

    _check_disk_pressure(host_key, label, data, wcfg, tracker)

    last_run = data.get("last_run")
    if not last_run:
        tracker.report_ok(key)
        return

    try:
        last_dt = datetime.fromisoformat(last_run)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        tracker.report_ok(key)
        return

    age = datetime.now(timezone.utc) - last_dt
    if age > timedelta(hours=wcfg.janitor_stale_hours):
        hours = int(age.total_seconds() / 3600)
        tracker.report_problem(
            key, SEVERITY_WARNING,
            f"{label} janitor stale",
            f"Last janitor run {hours}h ago (threshold: {wcfg.janitor_stale_hours}h)",
            host_key, label,
        )
    else:
        tracker.report_ok(key)


def _check_disk_pressure(
    host_key: str,
    label: str,
    data: dict[str, Any],
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    """Alert while the janitor's escalation cannot bring a disk back under its limit.

    The percentage alerts only say a disk is full; this says the janitor has
    run out of things it may delete, which is what precedes ENOSPC hanging
    every RMS camera (gmnro14 (Chiribiș), 22:08 UTC on 2026-10-16, after five
    days of the janitor logging it to nobody).
    """
    key = f"{host_key}:disk_pressure"
    pressure = data.get("pressure")
    if not isinstance(pressure, dict) or not pressure.get("since"):
        tracker.report_ok(key)
        return
    tracker.report_problem(
        key, SEVERITY_WARNING,
        f"{label} janitor cannot free disk",
        f"`{pressure.get('filesystem', '?')}` at {pressure.get('pct', '?')}% "
        f"({pressure.get('free_gb', '?')} GB free) after full escalation, "
        f"since {pressure['since']} ({pressure.get('runs', '?')} janitor runs)",
        host_key, label,
        grace_polls=wcfg.disk_grace_polls,
    )


def _check_dawn(
    host_key: str,
    label: str,
    station: Any,
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
) -> None:
    now_utc = datetime.now(timezone.utc)
    start_h, start_m = map(int, wcfg.dawn_check_start.split(":"))
    end_h, end_m = map(int, wcfg.dawn_check_end.split(":"))
    start_minutes = start_h * 60 + start_m
    end_minutes = end_h * 60 + end_m
    now_minutes = now_utc.hour * 60 + now_utc.minute

    if not (start_minutes <= now_minutes <= end_minutes):
        return

    yesterday = (now_utc - timedelta(days=1)).strftime("%Y%m%d")
    key = f"{host_key}:dawn:{yesterday}"

    if key in tracker._notified_dawn:
        return

    cutoff = now_utc - timedelta(days=7)
    tracker._notified_dawn = {
        k: v for k, v in tracker._notified_dawn.items() if v > cutoff
    }

    all_done = True
    for cam in station.cameras:
        try:
            url = station_url(config, tunnels, host_key, f"/api/chunks/{cam.code}/{yesterday}")
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            all_done = False
            continue

        morning_done = data.get("morning_done", False) if isinstance(data, dict) else False
        if not morning_done:
            all_done = False

    if all_done and station.cameras:
        tracker._notified_dawn[key] = now_utc
        tracker.report_info(
            f"{label} morning complete",
            f"All {len(station.cameras)} cameras processed for {yesterday}",
            host_key, label,
        )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------


def _run_cycle(
    config: DashboardConfig,
    tunnels: TunnelManager,
    wcfg: WatchdogConfig,
    tracker: AlertTracker,
    host_keys: list[str] | None = None,
) -> None:
    tracker.begin_cycle()
    for host_key in (host_keys if host_keys is not None else list(config.stations)):
        try:
            check_station(host_key, config, tunnels, wcfg, tracker)
        except Exception:
            logger.exception("Unexpected error checking %s", host_key)

    notifications = tracker.drain_notifications()
    if notifications:
        try:
            sent = dispatch_notifications_batched(wcfg, notifications)
        except Exception:
            logger.exception("Failed to dispatch notifications")
            sent = 0
    else:
        sent = 0

    logger.info(
        "Cycle complete: %d active alerts, %d issues across %d messages",
        tracker.active_count, len(notifications), sent,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="ROVIMEN Station Health Watchdog")
    parser.add_argument(
        "--config",
        default=str(Path(__file__).parent / "dashboard_config.yaml"),
        help="Path to dashboard_config.yaml",
    )
    parser.add_argument(
        "--watchdog-config",
        default=str(Path(__file__).parent / "watchdog_config.yaml"),
        help="Path to watchdog_config.yaml",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    config = load_config(Path(args.config))

    # The watchdog config is gitignored (it holds the alert webhook URL), so it
    # is created by hand on the VPS and is not part of the deployed tree. Until
    # the deploy rsync learned to --exclude it, every deploy deleted it and this
    # process died on an unhandled FileNotFoundError. Exit 0, not non-zero: the
    # unit is Restart=on-failure, so a zero exit leaves the service cleanly
    # inactive with one actionable log line instead of restarting every 30s
    # forever and burying the reason in a wall of identical tracebacks.
    watchdog_config_path = Path(args.watchdog_config)
    if not watchdog_config_path.is_file():
        logger.error(
            "Watchdog config not found at %s — no alert destination or "
            "thresholds to run with, so there is nothing to do. Create the file "
            "(mode 0600) and start this service again.",
            watchdog_config_path,
        )
        sys.exit(0)

    wcfg = load_watchdog_config(watchdog_config_path)

    telegram_ready = bool(wcfg.telegram_bot_token and wcfg.telegram_chat_id)
    if wcfg.discord_webhook_url or telegram_ready:
        logger.info(
            "Alert transports: Discord %s, Telegram %s",
            "on" if wcfg.discord_webhook_url else "off",
            "on" if telegram_ready else "off",
        )
    else:
        logger.warning(
            "No alert transport configured (discord_webhook_url empty and no "
            "ROVIMEN_TELEGRAM_BOT_TOKEN + ROVIMEN_WATCHDOG_TELEGRAM_CHAT_ID) "
            "— alerts will only be logged"
        )

    checked, skipped, muted = plan_coverage(config, wcfg)
    unknown_mutes = sorted(set(wcfg.mute_stations) - set(config.stations))
    logger.info(
        "Coverage: %d station(s) checked; %d alerting, %d muted (%s); "
        "%d skipped as non-active (%s)%s",
        len(checked), len(checked) - len(muted),
        len(muted), ", ".join(muted) or "none",
        len(skipped), ", ".join(skipped) or "none",
        f"; mute entries not in the registry: {', '.join(unknown_mutes)}"
        if unknown_mutes else "",
    )

    tunnels = TunnelManager(config)
    tunnels.start_all()
    tunnels.start_watchdog()

    tracker = AlertTracker(wcfg)

    shutdown = False

    def _handle_signal(signum: int, frame: Any) -> None:
        nonlocal shutdown
        logger.info("Received signal %d, shutting down", signum)
        shutdown = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logger.info(
        "Watchdog started — polling %d stations every %ds; no alerts for the "
        "first %d cycle(s)",
        len(checked), wcfg.poll_interval_s, wcfg.startup_grace_polls,
    )

    while not shutdown:
        cycle_start = time.monotonic()
        try:
            _run_cycle(config, tunnels, wcfg, tracker, checked)
        except Exception:
            logger.exception("Poll cycle failed")
        elapsed = time.monotonic() - cycle_start
        sleep_time = max(1, wcfg.poll_interval_s - elapsed)
        logger.info("Next poll in %.0fs", sleep_time)

        deadline = time.monotonic() + sleep_time
        while not shutdown and time.monotonic() < deadline:
            time.sleep(min(1.0, deadline - time.monotonic()))

    tunnels.shutdown()
    logger.info("Watchdog stopped")


if __name__ == "__main__":
    main()
