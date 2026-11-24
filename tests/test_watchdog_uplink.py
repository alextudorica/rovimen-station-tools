"""rovimen_watchdog.sh says where a lost uplink breaks, and skips a pointless ladder.

2026-10-20: gmnro14 (Chiribiș) lost its uplink at 17:21 UTC for 13+ h and the
watchdog log could not say whether the router, the ISP or the PC had failed.
Each run now classifies the uplink (ok / isp / router / link) and appends one
line per change to logs/uplink_events.log. When the gateway answers and the
internet does not, restarting the station's networking cannot help, so the
recovery ladder is skipped.

Full watchdog runs against PATH stubs: `ip` reports a default route via
192.168.1.1 on eno1, `ping` answers only the hosts listed in $FAKE_DIR/up.
"""

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parent.parent / "rovimen-scripts" / "rovimen_watchdog.sh"
GW = "192.168.1.1"
DAY = "2026-10-21T10:00:00Z"

STUBS = {
    "ping": (
        'echo "ping $*" >> "$FAKE_LOG"\n'
        'host="${@: -1}"\n'
        'grep -qxF "$host" "$FAKE_DIR/up" 2>/dev/null\n'
    ),
    "ip": (
        'if [ "$1 $2" = "route show" ]; then\n'
        '  [ -f "$FAKE_DIR/no_route" ] || echo "default via 192.168.1.1 dev eno1 proto dhcp metric 100"\n'
        'fi\n'
        'exit 0\n'
    ),
    "sudo": (
        'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\n'
        'case "$1" in */reboot|reboot) exit 0 ;; esac\n'
        'exec "$@"\n'
    ),
    "systemctl": 'echo "systemctl $*" >> "$FAKE_LOG"; exit 0\n',
    # Online exactly when the internet answers, as tailscaled reports it.
    "tailscale": (
        'online=false; grep -qxF 8.8.8.8 "$FAKE_DIR/up" 2>/dev/null && online=true\n'
        'echo "{\\"BackendState\\": \\"Running\\", \\"Self\\": {\\"Online\\": $online}}"\n'
    ),
    "getent": "exit 2\n",
}


@pytest.fixture()
def env(tmp_path):
    fake_bin, fake_dir, home, sysfs = (tmp_path / d for d in ("bin", "fake", "home", "sysfs"))
    for d in (fake_bin, fake_dir, home, sysfs / "eno1"):
        d.mkdir(parents=True)
    for name, body in STUBS.items():
        p = fake_bin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (sysfs / "eno1" / "carrier").write_text("1\n")
    (home / "rovimen_scripts").mkdir()
    (home / "rovimen_scripts" / "config.json").write_text(json.dumps({}))
    log = fake_dir / "calls.log"
    log.write_text("")
    e = os.environ.copy()
    e.update({"PATH": f"{fake_bin}:{e['PATH']}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
              "FAKE_LOG": str(log), "ROVIMEN_WATCHDOG_SYSFS_NET": str(sysfs),
              "ROVIMEN_WATCHDOG_NOW": DAY, "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0",
              "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0", "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0"})
    return e, log, home, fake_dir, sysfs


def up(fake_dir: Path, *hosts: str) -> None:
    (fake_dir / "up").write_text("".join(h + "\n" for h in hosts))


def run(e: dict) -> None:
    assert subprocess.run(["bash", str(WATCHDOG)], env=e, capture_output=True,
                          text=True, timeout=120).returncode == 0


def wlog(home: Path) -> str:
    return (home / "rovimen_scripts" / "logs" / "watchdog.log").read_text()


def events(home: Path) -> list[str]:
    f = home / "rovimen_scripts" / "logs" / "uplink_events.log"
    return f.read_text().splitlines() if f.exists() else []


def test_healthy_uplink_is_ok(env):
    e, log, home, fake_dir, _ = env
    up(fake_dir, "8.8.8.8", "100.100.100.100", GW)
    run(e)
    assert f"uplink: ok (gateway {GW} on eno1)" in wlog(home)
    assert "sudo" not in log.read_text()


def test_gateway_answers_internet_not_is_isp_and_skips_ladder(env):
    e, log, home, fake_dir, _ = env
    up(fake_dir, GW)
    run(e)
    assert "uplink: isp" in wlog(home)
    assert "outage is past the router" in wlog(home)
    calls = log.read_text()
    assert "restart" not in calls and "reboot" not in calls


def test_silent_gateway_is_router_and_keeps_ladder(env):
    e, log, home, fake_dir, _ = env
    up(fake_dir)
    run(e)
    assert "uplink: router" in wlog(home)
    assert "restart" not in log.read_text(), "one failed run is not enough to act"
    run(e)
    assert "sudo -n systemctl restart NetworkManager" in log.read_text() or \
        "RECOVERY: restarting networking" in wlog(home)


def test_no_carrier_is_link(env):
    e, _, home, fake_dir, sysfs = env
    up(fake_dir, GW)
    (sysfs / "eno1" / "carrier").write_text("0\n")
    run(e)
    assert "uplink: link" in wlog(home)


def test_no_default_route_is_link(env):
    e, _, home, fake_dir, _ = env
    up(fake_dir)
    (fake_dir / "no_route").write_text("")
    run(e)
    assert "uplink: link (gateway none on none)" in wlog(home)


def test_events_record_changes_only(env):
    e, _, home, fake_dir, _ = env
    up(fake_dir, "8.8.8.8", "100.100.100.100", GW)
    run(e)
    run(e)
    up(fake_dir, GW)
    run(e)
    run(e)
    up(fake_dir, "8.8.8.8", "100.100.100.100", GW)
    run(e)
    ev = events(home)
    assert len(ev) == 3
    assert "uplink ok (first run" in ev[0]
    assert "uplink ok -> isp after 0h00m (gateway 192.168.1.1 on eno1)" in ev[1]
    assert "uplink isp -> ok after" in ev[2]
