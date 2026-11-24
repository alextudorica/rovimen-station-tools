"""Tests for rovimen_watchdog.sh service-health pass.

The functional tests run the watchdog with --services-only against stub
systemctl/sudo binaries placed first on PATH. Stub state lives in files under
FAKE_DIR: enabled_<unit> (is-enabled reply), state_<unit> (is-active reply),
stubborn_<unit> (marker: restart does NOT revive the unit). Every stub
invocation is appended to FAKE_LOG. Only system-scope units are exercised —
/run/user/<uid> and a --user session bus are not guaranteed on CI.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
WATCHDOG = REPO_ROOT / "rovimen-scripts" / "rovimen_watchdog.sh"
UPDATER = REPO_ROOT / "rovimen-scripts" / "updater.sh"

SYSTEMCTL_STUB = """#!/usr/bin/env bash
# Stub systemctl for watchdog tests. State in $FAKE_DIR, call log in $FAKE_LOG.
echo "systemctl $*" >> "$FAKE_LOG"
[ "${1:-}" = "--user" ] && shift
verb="${1:-}"; unit="${2:-}"
unit="${unit%.service}"
case "$verb" in
    is-enabled)
        f="$FAKE_DIR/enabled_$unit"
        if [ -f "$f" ]; then cat "$f"; [ "$(cat "$f")" = "enabled" ] && exit 0 || exit 1
        else echo "not-found"; exit 4; fi
        ;;
    is-active)
        f="$FAKE_DIR/state_$unit"
        if [ -f "$f" ]; then cat "$f"; [ "$(cat "$f")" = "active" ] && exit 0 || exit 3
        else echo "unknown"; exit 3; fi
        ;;
    reset-failed)
        exit 0
        ;;
    restart)
        if [ -f "$FAKE_DIR/stubborn_$unit" ]; then exit 1; fi
        echo "active" > "$FAKE_DIR/state_$unit"
        exit 0
        ;;
    *)
        exit 0
        ;;
esac
"""

SUDO_STUB = """#!/usr/bin/env bash
echo "sudo $*" >> "$FAKE_LOG"
[ "$1" = -n ] && shift
case "$1" in */reboot|reboot|shutdown|*/shutdown|*/poweroff|poweroff) exit 0 ;; esac
exec "$@"
"""


def test_watchdog_bash_syntax():
    subprocess.run(["bash", "-n", str(WATCHDOG)], check=True)


def test_updater_bash_syntax():
    subprocess.run(["bash", "-n", str(UPDATER)], check=True)


@pytest.fixture()
def watchdog_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_dir = tmp_path / "fake"
    home = tmp_path / "home"
    fake_bin.mkdir()
    fake_dir.mkdir()
    home.mkdir()

    for name, body in (("systemctl", SYSTEMCTL_STUB), ("sudo", SUDO_STUB)):
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    fake_log = fake_dir / "calls.log"
    fake_log.write_text("")

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "HOME": str(home),
            "FAKE_DIR": str(fake_dir),
            "FAKE_LOG": str(fake_log),
            "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0",
        }
    )
    return env, fake_dir, fake_log, home


def run_watchdog(env):
    return subprocess.run(
        ["bash", str(WATCHDOG), "--services-only"],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def watchdog_log(home: Path) -> str:
    logfile = home / "rovimen_scripts" / "logs" / "watchdog.log"
    return logfile.read_text() if logfile.exists() else ""


def test_dead_enabled_unit_restarted(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    (fake_dir / "enabled_color-capture").write_text("enabled")
    (fake_dir / "state_color-capture").write_text("failed")

    result = run_watchdog(env)
    assert result.returncode == 0, result.stderr

    calls = fake_log.read_text()
    assert "systemctl reset-failed color-capture" in calls
    assert "systemctl restart color-capture" in calls
    # system scope must escalate through sudo
    assert "sudo -n systemctl restart color-capture" in calls
    assert (fake_dir / "state_color-capture").read_text().strip() == "active"
    assert "RECOVERED" in watchdog_log(home)


def test_disabled_unit_never_touched(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    # Buzau scenario: color-capture deliberately disabled AND dead
    (fake_dir / "enabled_color-capture").write_text("disabled")
    (fake_dir / "state_color-capture").write_text("failed")

    result = run_watchdog(env)
    assert result.returncode == 0, result.stderr

    calls = fake_log.read_text()
    assert "restart color-capture" not in calls
    assert "reset-failed color-capture" not in calls
    assert (fake_dir / "state_color-capture").read_text().strip() == "failed"


def test_active_unit_never_touched(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    (fake_dir / "enabled_rovimen-station-api").write_text("enabled")
    (fake_dir / "state_rovimen-station-api").write_text("active")

    result = run_watchdog(env)
    assert result.returncode == 0, result.stderr
    assert "restart rovimen-station-api" not in fake_log.read_text()


def test_restart_budget_exhausts(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    (fake_dir / "enabled_color-capture").write_text("enabled")
    (fake_dir / "state_color-capture").write_text("failed")
    (fake_dir / "stubborn_color-capture").write_text("")

    for _ in range(5):
        result = run_watchdog(env)
        assert result.returncode == 0, result.stderr

    restarts = fake_log.read_text().count("sudo -n systemctl restart color-capture")
    assert restarts == 3  # SERVICE_RESTART_BUDGET
    assert "budget exhausted" in watchdog_log(home)


def test_pusher_skipped_without_ingest_key(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    # Even enabled+dead, the pusher is opt-in — no ingest.key means no restart
    (fake_dir / "enabled_rovimen-pusher").write_text("enabled")
    (fake_dir / "state_rovimen-pusher").write_text("failed")

    result = run_watchdog(env)
    assert result.returncode == 0, result.stderr
    assert "restart rovimen-pusher" not in fake_log.read_text()


def test_pusher_healed_with_ingest_key(watchdog_env):
    env, fake_dir, fake_log, home = watchdog_env
    (fake_dir / "enabled_rovimen-pusher").write_text("enabled")
    (fake_dir / "state_rovimen-pusher").write_text("failed")
    key = home / ".config" / "rovimen" / "ingest.key"
    key.parent.mkdir(parents=True)
    key.write_text("test")

    result = run_watchdog(env)
    assert result.returncode == 0, result.stderr
    assert "sudo -n systemctl restart rovimen-pusher" in fake_log.read_text()
