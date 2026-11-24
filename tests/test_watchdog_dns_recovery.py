"""rovimen_watchdog.sh DNS rung.

2026-09-23: gmnmd01 (Cahul) reached 8.8.8.8 and Tailscale for 7.5 h while every
name lookup failed, so nothing uploaded and the watchdog, which only acts on a
Tailscale failure, logged "dns: FAILED" every 3 minutes and did nothing. Its
DNS check was also broken on stations without host/nslookup installed.

Full watchdog runs against stubs first on PATH: ping always answers, getent
fails until the stub nmcli has re-activated the uplink, ip reports a wlan0
default route, systemctl records. sudo allows only the rules updater.sh
generates for this station (as on gmnro07 (București), gmnro08 (Brașov) and
gmnde00 (Johannismühle), which have no other sudo) and runs what they allow.
"""

import os
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parent.parent / "rovimen-scripts" / "rovimen_watchdog.sh"
UPDATER = WATCHDOG.parent / "updater.sh"
UPLINK_UUID = "3f0b9c4e-1d2a-4e5b-8c7d-6a5b4c3d2e1f"

STUBS = {
    "ping": 'echo "ping $*" >> "$FAKE_LOG"; exit 0\n',
    "getent": 'echo "getent $*" >> "$FAKE_LOG"; [ -f "$FAKE_DIR/dns_ok" ] && { echo "1.2.3.4 $2"; exit 0; }; exit 2\n',
    "ip": 'if [ "$1 $2" = "route show" ]; then echo "default via 192.168.178.1 dev wlan0 proto dhcp"; fi; exit 0\n',
    "nmcli": (
        'echo "nmcli $*" >> "$FAKE_LOG"\n'
        'case "$*" in\n'
        f'  *"GENERAL.CON-UUID device show wlan0"*) echo "{UPLINK_UUID}" ;;\n'
        '  *"device show wlan0"*) echo "HomeWiFi" ;;\n'
        '  *"connection up"*) [ -f "$FAKE_DIR/bounce_fixes" ] && touch "$FAKE_DIR/dns_ok" ;;\n'
        'esac\nexit 0\n'
    ),
    # Allows a command only when it matches a NOPASSWD rule of the sudoers
    # file the updater wrote (exactly, or a rule naming the command alone),
    # and fails the rest at once as sudo -n does. Never passes a reboot
    # through: test_no_bounce_when_internet_is_down reaches the reboot rung,
    # and exec-ing /sbin/reboot would ask logind to reboot the machine.
    "sudo": (
        'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\n'
        'if [ "$1 $2" = "tee /etc/sudoers.d/rovimen" ]; then\n'
        '  [ -f "$FAKE_DIR/sudoers" ] || { cat > "$FAKE_DIR/sudoers"; exit 0; }\n'
        'fi\n'
        'full=$(command -v "$1" || echo "$1"); line="$full"; [ $# -gt 1 ] && line="$full ${*:2}"\n'
        'sed -n "s/^[^#].*NOPASSWD: //p" "$FAKE_DIR/sudoers" 2>/dev/null | grep -qxF -e "$line" -e "$full" '
        '|| { echo "sudo: a password is required" >&2; exit 1; }\n'
        'case "$1" in */reboot|reboot|shutdown|*/shutdown) exit 0 ;; esac\n'
        'exec "$@"\n'
    ),
    "systemctl": 'echo "systemctl $*" >> "$FAKE_LOG"; exit 0\n',
    "tailscale": 'echo \'{"BackendState": "Running", "Self": {"Online": true}}\'\n',
    "sync": "exit 0\n",
}


@pytest.fixture()
def env(tmp_path):
    fake_bin, fake_dir, home = tmp_path / "bin", tmp_path / "fake", tmp_path / "home"
    for d in (fake_bin, fake_dir, home):
        d.mkdir()
    for name, body in STUBS.items():
        p = fake_bin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    log = fake_dir / "calls.log"
    log.write_text("")
    e = os.environ.copy()
    e.update({"PATH": f"{fake_bin}:{e['PATH']}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
              "FAKE_LOG": str(log), "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0",
              "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0", "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0", "ROVIMEN_WATCHDOG_DNS_FAIL_THRESHOLD": "3"})
    e.pop("SUDO_USER", None)
    # The station's /etc/sudoers.d/rovimen, as the updater writes it here.
    subprocess.run(
        ["bash", "-c", "set -euo pipefail; log() { :; }; "
         f"LOCAL_DIR={UPDATER.parent}; "
         f"eval \"$(sed -n '/^_ensure_sudoers() {{/,/^}}/p' {UPDATER})\"; _ensure_sudoers"],
        env=e, capture_output=True, text=True, timeout=60, check=True)
    assert (fake_dir / "sudoers").is_file()
    log.write_text("")
    return e, fake_dir, log, home


def run(e) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(WATCHDOG)], env=e, capture_output=True, text=True, timeout=120)


def wlog(home: Path) -> str:
    f = home / "rovimen_scripts" / "logs" / "watchdog.log"
    return f.read_text() if f.exists() else ""


def test_dns_check_uses_getent(env):
    e, fake_dir, log, home = env
    (fake_dir / "dns_ok").touch()
    assert run(e).returncode == 0
    assert "getent hosts dashboard.rovimen.org" in log.read_text()
    assert "dns: OK" in wlog(home)


def test_no_bounce_before_threshold(env):
    e, fake_dir, log, home = env
    for _ in range(2):
        run(e)
    assert "connection up" not in log.read_text()


def test_bounces_only_the_uplink_after_threshold_and_recovers(env):
    e, fake_dir, log, home = env
    (fake_dir / "bounce_fixes").touch()
    for _ in range(3):
        run(e)
    calls = log.read_text()
    assert f"sudo -n nmcli -w 30 connection up uuid {UPLINK_UUID}" in calls
    assert "trying nmcli without sudo" not in wlog(home)
    assert "restart NetworkManager" not in calls and "reboot" not in calls
    assert "DNS RECOVERY SUCCESS" in wlog(home)


def test_cooldown_blocks_a_second_bounce(env):
    e, fake_dir, log, home = env
    for _ in range(5):
        run(e)
    assert log.read_text().count("\nsudo -n nmcli -w 30 connection up") == 1
    assert "did not restore name resolution" in wlog(home)


def test_no_bounce_when_internet_is_down(env):
    e, fake_dir, log, home = env
    (Path(e["PATH"].split(":")[0]) / "ping").write_text("#!/usr/bin/env bash\nexit 1\n")
    for _ in range(4):
        run(e)
    assert "connection up" not in log.read_text()
