"""rovimen_watchdog.sh brings the rescue Wi-Fi up only while the cable fails (#1432).

Full watchdog runs against PATH stubs: `ip` lists a default route on eno1
(and on wlp0s20f3 once the rescue profile is up); `ping` answers 8.8.8.8
except when it is bound to eno1 (`-I eno1`) and $FAKE_DIR/cable_down exists.
The helper is a stub that records up/down and keeps its state in a file.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

WATCHDOG = Path(__file__).resolve().parent.parent / "rovimen-scripts" / "rovimen_watchdog.sh"

STUBS = {
    # $FAKE_DIR/rescue_dead: the rescue link is up but reaches nothing.
    "ping": (
        'echo "ping $*" >> "$FAKE_LOG"\n'
        'dead=""; { [ -f "$FAKE_DIR/cable_down" ] || [ -f "$FAKE_DIR/isp_down" ]; } && dead=1\n'
        'case " $* " in *" -I eno1 "*) [ -n "$dead" ] && exit 1 ;; esac\n'
        'case " $* " in *" -I "*) exit 0 ;; esac\n'
        '[ -n "$dead" ] && { [ ! -f "$FAKE_DIR/rescue_up" ] || [ -f "$FAKE_DIR/rescue_dead" ]; } && exit 1\n'
        'exit 0\n'
    ),
    "ip": (
        'if [ "$1 $2" = "route show" ]; then\n'
        '  [ -f "$FAKE_DIR/cable_down" ] || echo "default via 192.168.1.1 dev eno1 proto dhcp metric 100"\n'
        '  [ -f "$FAKE_DIR/rescue_up" ] && echo "default via 172.20.10.1 dev wlp0s20f3 proto dhcp metric 5"\n'
        'fi\nexit 0\n'
    ),
    # Never passes a reboot through: a cable-down run reaches the reboot rung.
    "sudo": (
        'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\n'
        'case "$1" in */reboot|reboot|shutdown|*/shutdown|*/poweroff|poweroff) exit 0 ;; esac\n'
        'exec "$@"\n'
    ),
    "systemctl": (
        'echo "systemctl $*" >> "$FAKE_LOG"\n'
        '[ "$*" = "list-units --type=service" ] && echo "NetworkManager.service loaded active running"\n'
        'exit 0\n'
    ),
    # Online while some path to the internet works (cable, or a live rescue).
    "tailscale": (
        'online=true; { [ -f "$FAKE_DIR/cable_down" ] || [ -f "$FAKE_DIR/isp_down" ]; } '
        '&& { [ ! -f "$FAKE_DIR/rescue_up" ] || [ -f "$FAKE_DIR/rescue_dead" ]; } && online=false\n'
        'echo "{\\"BackendState\\": \\"Running\\", \\"Self\\": {\\"Online\\": $online}}"\n'
    ),
    "getent": "exit 0\n",
    "sync": "exit 0\n",
}

# $FAKE_DIR/no_hotspot: netplan applies the profile but nothing associates,
# so the helper reports "waiting" and refuses a second `up`.
HELPER = (
    'echo "helper $1" >> "$FAKE_LOG"\n'
    'case "$1" in\n'
    '  status) if [ -f "$FAKE_DIR/rescue_up" ]; then echo up; elif [ -f "$FAKE_DIR/rescue_live" ]; then echo waiting; else echo down; fi ;;\n'
    '  up) [ -n "${FAKE_HELPER_FAIL:-}" ] && exit 1\n'
    '      [ -f "$FAKE_DIR/rescue_live" ] && exit 1\n'
    '      if [ -f "$FAKE_DIR/no_hotspot" ]; then touch "$FAKE_DIR/rescue_live"; exit 1; fi\n'
    '      touch "$FAKE_DIR/rescue_up" ;;\n'
    '  down) rm -f "$FAKE_DIR/rescue_up" "$FAKE_DIR/rescue_live" ;;\n'
    'esac\n'
)


def _exe(p: Path, body: str) -> None:
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@pytest.fixture()
def env(tmp_path):
    fake_bin, fake_dir, home, sysfs = (tmp_path / d for d in ("bin", "fake", "home", "sysfs"))
    for d in (fake_bin, fake_dir, home, sysfs / "eno1"):
        d.mkdir(parents=True)
    for name, body in STUBS.items():
        _exe(fake_bin / name, body)
    (sysfs / "eno1" / "carrier").write_text("1\n")
    (home / "rovimen_scripts").mkdir()
    (home / "rovimen_scripts" / "config.json").write_text(json.dumps({}))
    helper = tmp_path / "rovimen-rescue-wifi"
    _exe(helper, HELPER)
    conf = tmp_path / "rescue.conf"
    conf.write_text("backend=netplan\niface=wlp0s20f3\nssid=ROVIMEN-RESCUE\n")
    log = fake_dir / "calls.log"
    log.write_text("")
    e = os.environ.copy()
    e.update({"PATH": f"{fake_bin}:{e['PATH']}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
              "FAKE_LOG": str(log), "ROVIMEN_WATCHDOG_SYSFS_NET": str(sysfs),
              "ROVIMEN_WATCHDOG_NOW": "2026-11-02T10:00:00Z", "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0",
              "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0", "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0",
              "ROVIMEN_RESCUE_HELPER": str(helper), "ROVIMEN_RESCUE_CONF": str(conf)})
    return {"env": e, "log": log, "home": home, "fake": fake_dir, "helper": helper, "conf": conf}


def run(ctx: dict) -> None:
    r = subprocess.run(["bash", str(WATCHDOG)], env=ctx["env"], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr


def calls(ctx: dict, action: str) -> int:
    return ctx["log"].read_text().splitlines().count(f"helper {action}")


def events(ctx: dict) -> str:
    p = ctx["home"] / "rovimen_scripts/logs/uplink_events.log"
    return p.read_text() if p.exists() else ""


def test_a_working_cable_never_brings_the_rescue_up(env):
    for _ in range(4):
        run(env)
    assert calls(env, "up") == 0 and not (env["fake"] / "rescue_up").exists()


def test_rescue_comes_up_after_two_failed_runs_and_down_after_three_good(env):
    (env["fake"] / "cable_down").touch()
    run(env)
    assert calls(env, "up") == 0, "one failed run is not enough"
    run(env)
    assert calls(env, "up") == 1 and (env["fake"] / "rescue_up").exists()
    assert "rescue wifi up on wlp0s20f3: cable uplink failed 2 runs" in events(env)
    run(env)
    assert calls(env, "up") == 1, "already up: not called again"

    (env["fake"] / "cable_down").unlink()
    for _ in range(2):
        run(env)
    assert calls(env, "down") == 0, "two good runs are not enough"
    run(env)
    assert calls(env, "down") == 1 and not (env["fake"] / "rescue_up").exists()
    assert "rescue wifi down: cable uplink back for 3 runs" in events(env)


def test_a_good_run_resets_the_failure_count(env):
    (env["fake"] / "cable_down").touch()
    run(env)
    (env["fake"] / "cable_down").unlink()
    run(env)
    (env["fake"] / "cable_down").touch()
    run(env)
    assert calls(env, "up") == 0


def test_the_cable_is_judged_through_eno1_while_the_rescue_carries_traffic(env):
    """Link up, router/ISP dead: eno1 keeps its route but its pings fail."""
    (env["fake"] / "isp_down").touch()
    run(env)
    run(env)
    run(env)  # rescue up now; the cable is still judged on eno1 only
    log = env["log"].read_text()
    assert "-I eno1" in log
    assert "-I wlp0s20f3" not in log, "the rescue interface must not count as the cable"
    assert calls(env, "down") == 0, "traffic over the rescue Wi-Fi is not a working cable"


def test_without_the_helper_nothing_happens(env):
    env["helper"].unlink()
    (env["fake"] / "cable_down").touch()
    run(env)
    run(env)
    assert "helper" not in env["log"].read_text() and "rescue wifi" not in events(env)


def test_config_can_switch_it_off(env):
    (env["home"] / "rovimen_scripts/config.json").write_text(json.dumps({"watchdog": {"rescue_wifi": False}}))
    (env["fake"] / "cable_down").touch()
    run(env)
    run(env)
    assert calls(env, "up") == 0


def test_a_failed_up_is_logged_and_retried(env):
    env["env"]["FAKE_HELPER_FAIL"] = "1"
    (env["fake"] / "cable_down").touch()
    run(env)
    run(env)
    wlog = (env["home"] / "rovimen_scripts/logs/watchdog.log").read_text()
    assert "bringing wlp0s20f3 up FAILED" in wlog and "rescue wifi up" not in events(env)
    del env["env"]["FAKE_HELPER_FAIL"]
    run(env)
    assert (env["fake"] / "rescue_up").exists()


def flag(ctx: dict) -> Path:
    return ctx["home"] / "rovimen_scripts/rescue_wifi_active"


def wlog(ctx: dict) -> str:
    return (ctx["home"] / "rovimen_scripts/logs/watchdog.log").read_text()


def test_while_the_rescue_is_up_the_ladder_is_skipped_and_the_flag_is_written(env):
    (env["fake"] / "cable_down").touch()
    run(env)
    assert not flag(env).exists()
    run(env)  # the rescue comes up in this run
    assert (env["fake"] / "rescue_up").exists() and flag(env).exists()
    assert "internet over the rescue Wi-Fi: OK" in wlog(env)
    assert "rescue wifi carries traffic: recovery ladder skipped" in wlog(env)
    for _ in range(3):
        run(env)
    log = env["log"].read_text()
    assert "systemctl restart NetworkManager" not in log
    assert "reboot" not in log
    assert flag(env).exists()

    (env["fake"] / "cable_down").unlink()
    for _ in range(3):
        run(env)
    assert not (env["fake"] / "rescue_up").exists() and not flag(env).exists()


def test_rescue_with_link_up_behind_a_dead_router_also_skips_the_ladder(env):
    (env["fake"] / "isp_down").touch()
    for _ in range(4):
        run(env)
    assert flag(env).exists()
    assert "systemctl restart NetworkManager" not in env["log"].read_text()


def test_a_stale_flag_goes_when_the_rescue_is_not_installed(env):
    env["helper"].unlink()
    flag(env).parent.mkdir(parents=True, exist_ok=True)
    flag(env).write_text("left over\n")
    run(env)
    assert not flag(env).exists()


def test_rescue_up_without_internet_runs_the_ladder(env):
    """The phone's hotspot is up but reaches nothing: the outage still gets recovery."""
    (env["fake"] / "cable_down").touch()
    (env["fake"] / "rescue_dead").touch()
    run(env)
    run(env)  # rescue comes up, reaches nothing; second failed run restarts networking
    assert (env["fake"] / "rescue_up").exists()
    assert "internet over the rescue Wi-Fi: UNREACHABLE" in wlog(env)
    assert "recovery ladder skipped" not in wlog(env)
    assert "sudo -n systemctl restart NetworkManager" in env["log"].read_text()
    assert not flag(env).exists(), "no upload pause while the rescue carries nothing"


def test_rescue_waiting_for_a_hotspot_runs_the_ladder_and_is_not_reapplied(env):
    """netplan: profile live, nobody in range. Status says waiting, not up."""
    (env["fake"] / "cable_down").touch()
    (env["fake"] / "no_hotspot").touch()
    for _ in range(5):
        run(env)
    assert calls(env, "up") == 1, "a live profile is not applied again every run"
    assert "profile live but no hotspot associated" in wlog(env)
    assert "recovery ladder skipped" not in wlog(env)
    assert "sudo -n systemctl restart NetworkManager" in env["log"].read_text()
    assert not flag(env).exists()

    (env["fake"] / "cable_down").unlink()
    for _ in range(3):
        run(env)
    assert calls(env, "down") == 1 and not (env["fake"] / "rescue_live").exists()
