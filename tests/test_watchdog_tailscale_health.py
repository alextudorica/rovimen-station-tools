"""rovimen_watchdog.sh judges Tailscale by its real state and acts by uplink class.

2026-11-20: gmnro11 (Tărâța) had both NICs down and no default route from
03:21 to 17:22 UTC. tailscaled itself reported Self.Online=false, but the
watchdog's health check pinged 100.100.100.100, which the local tailscaled
answers with or without a network, so every run logged "tailscale: OK", the
recovery ladder never started, and a person rebooted the laptop after 14 h.

Health now comes from `tailscale status --json` (Running and Self.Online),
and the ladder follows the uplink class: tailscaled restarts only when the
internet works, networking restarts and reboots only for classes link and
router, each with a budget.

Full watchdog runs against PATH stubs. `ping` answers the hosts listed in
$FAKE_DIR/up (100.100.100.100 always, as the real tailscaled does);
`tailscale status --json` prints $FAKE_DIR/ts.json; `timeout` runs its command
without a clock, except that it answers 124 at once for a hung `tailscale`
($FAKE_DIR/ts_hang). Nothing waits in real time.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
WATCHDOG = SCRIPTS / "rovimen_watchdog.sh"
GW = "192.168.1.1"
# 45.0N 25.0E: sun about +25 deg at 10:00 UTC and -50 deg at 22:00 UTC in late November.
DAY = "2026-11-21T10:00:00Z"
NIGHT = "2026-11-20T22:00:00Z"

STUBS = {
    "ping": (
        'echo "ping $*" >> "$FAKE_LOG"\n'
        'host="${@: -1}"\n'
        '[ "$host" = 100.100.100.100 ] && exit 0\n'
        'grep -qxF "$host" "$FAKE_DIR/up" 2>/dev/null\n'
    ),
    "ip": (
        'if [ "$1 $2" = "route show" ]; then\n'
        '  [ -f "$FAKE_DIR/no_route" ] || echo "default via 192.168.1.1 dev eno1 proto dhcp metric 100"\n'
        'fi\nexit 0\n'
    ),
    "tailscale": (
        'echo "tailscale $*" >> "$FAKE_LOG"\n'
        '[ -f "$FAKE_DIR/ts.json" ] && cat "$FAKE_DIR/ts.json"\nexit 0\n'
    ),
    "timeout": (
        'echo "timeout $*" >> "$FAKE_LOG"\n'
        'while [ "${1#-}" != "$1" ]; do case "$1" in -k|-s) shift 2 ;; *) shift ;; esac; done\n'
        'shift\n'
        '[ "$1" = tailscale ] && [ -f "$FAKE_DIR/ts_hang" ] && exit 124\n'
        'exec "$@"\n'
    ),
    "sudo": (
        'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\n'
        'case "$1" in */reboot|reboot|shutdown|*/shutdown|*/poweroff|poweroff) exit 0 ;; esac\n'
        'exec "$@"\n'
    ),
    "systemctl": (
        'echo "systemctl $*" >> "$FAKE_LOG"\n'
        'case "$*" in\n'
        '  "list-units --type=service") echo "NetworkManager.service loaded active running Network Manager" ;;\n'
        '  "list-unit-files tailscaled.service --no-legend") echo "tailscaled.service enabled enabled" ;;\n'
        '  is-active*) echo active ;;\n'
        'esac\nexit 0\n'
    ),
    "getent": "exit 0\n",
    "sync": "exit 0\n",  # a real sync before the stubbed reboot can take seconds on a busy disk
    "nmcli":'echo "nmcli $*" >> "$FAKE_LOG"; exit 0\n',
}


def _exe(p: Path, body: str) -> None:
    p.write_text("#!/usr/bin/env bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class Station:
    def __init__(self, tmp: Path):
        self.bin, self.fake, self.home, self.sysfs = (tmp / d for d in ("bin", "fake", "home", "sysfs"))
        for d in (self.bin, self.fake, self.home, self.sysfs / "eno1"):
            d.mkdir(parents=True)
        for name, body in STUBS.items():
            _exe(self.bin / name, body)
        (self.sysfs / "eno1" / "carrier").write_text("1\n")
        self.scripts = self.home / "rovimen_scripts"
        self.scripts.mkdir()
        for mod in ("rovimen_config.py", "rovimen_night.py"):
            shutil.copy(SCRIPTS / mod, self.scripts / mod)
        self.config({"latitude": 45.0, "longitude": 25.0})
        self.calls_log = self.fake / "calls.log"
        self.calls_log.write_text("")
        self.lock = tmp / "rovimen_dawn.lock"
        e = os.environ.copy()
        e.update({"PATH": f"{self.bin}:{e['PATH']}", "HOME": str(self.home), "FAKE_DIR": str(self.fake),
                  "FAKE_LOG": str(self.calls_log), "ROVIMEN_WATCHDOG_SYSFS_NET": str(self.sysfs),
                  "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0", "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0",
                  "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0", "ROVIMEN_DAWN_LOCK": str(self.lock),
                  "ROVIMEN_RESCUE_HELPER": str(tmp / "no-helper"),
                  "ROVIMEN_DAWN_CRON": str(tmp / "rovimen-dawn"),
                  "ROVIMEN_RMS_UNIT_DIR": str(tmp / "units")})
        self.env = e
        self.internet(True)
        self.ts(backend="Running", online=True)

    def config(self, cfg: dict) -> None:
        (self.scripts / "config.json").write_text(json.dumps(cfg))

    def internet(self, ok: bool, gateway: bool = False) -> None:
        hosts = (["8.8.8.8", "1.1.1.1"] if ok else []) + ([GW] if gateway or ok else [])
        (self.fake / "up").write_text("".join(h + "\n" for h in hosts))

    def ts(self, backend: str, online: bool) -> None:
        (self.fake / "ts.json").write_text(json.dumps(
            {"BackendState": backend, "Self": {"Online": online, "TailscaleIPs": ["100.64.0.9"]}}))

    def run(self, now: str = DAY, times: int = 1) -> None:
        for _ in range(times):
            r = subprocess.run(["bash", str(WATCHDOG)], env={**self.env, "ROVIMEN_WATCHDOG_NOW": now},
                               capture_output=True, text=True, timeout=60)
            assert r.returncode == 0, r.stderr

    @property
    def calls(self) -> str:
        return self.calls_log.read_text()

    @property
    def wlog(self) -> str:
        f = self.scripts / "logs" / "watchdog.log"
        return f.read_text() if f.exists() else ""

    @property
    def events(self) -> list[str]:
        f = self.scripts / "logs" / "uplink_events.log"
        return f.read_text().splitlines() if f.exists() else []

    @property
    def state_file(self) -> Path:
        return self.scripts / "logs" / ".watchdog_state"

    def set_state(self, key: str, value: str) -> None:
        lines = [ln for ln in self.state_file.read_text().splitlines() if not ln.startswith(key + "=")]
        self.state_file.write_text("\n".join(lines + [f"{key}={value}"]) + "\n")

    def age_outage(self, seconds: int = 1900) -> None:
        """Move the current outage's start back, past the 30-min reboot minimum."""
        self.set_state("outage_start", str(int(time.time()) - seconds))

    def count(self, line: str) -> int:
        """How many times exactly this command line was run."""
        return self.calls.splitlines().count(line)

    def reboots(self) -> int:
        return len(re.findall(r"^sudo -n \S*reboot$", self.calls, re.M))


@pytest.fixture()
def st(tmp_path) -> Station:
    return Station(tmp_path)


NM_RESTART = "sudo -n systemctl restart NetworkManager"
TS_RESTART = "sudo -n systemctl restart tailscaled"


def test_gmnro11_case_quad100_answers_but_offline_without_route_starts_the_ladder(st):
    """Interfaces down, no default route, tailscaled Online=false, quad-100 still answers."""
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run()
    assert "tailscale: UNREACHABLE (backend Running, Self.Online=false)" in st.wlog
    assert "tailscale: OK" not in st.wlog
    assert "uplink: link (gateway none on none)" in st.wlog
    assert st.count(NM_RESTART) == 0, "one failed run is not enough"
    st.run()
    assert st.count(NM_RESTART) == 1
    st.run()
    assert st.reboots() == 0, "no reboot in the first 30 minutes of an outage"
    assert "REBOOT WAITS: outage" in st.wlog
    st.age_outage()
    st.run()
    assert st.reboots() == 1, "by day, a 30-min link outage the networking restart did not fix reboots"
    assert "RECOVERY: REBOOTING (uplink link" in st.wlog


def test_owner_power_cycling_the_router_does_not_reboot(st):
    """A 10-min daytime link outage: one networking restart, no reboot."""
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(times=4)
    assert st.count(NM_RESTART) == 1 and st.reboots() == 0
    st.internet(True)
    (st.fake / "no_route").unlink()
    st.ts(backend="Running", online=True)
    st.run()
    assert "outage_start=0" in st.state_file.read_text()


def test_logged_out_takes_no_action_and_records_one_event(st):
    st.ts(backend="NeedsLogin", online=False)
    st.run(times=5)
    assert "tailscale: logged out (NeedsLogin)" in st.wlog
    assert not [ln for ln in st.calls.splitlines() if ln.startswith("sudo ")]
    logged = [e for e in st.events if "tailscale logged out" in e]
    assert len(logged) == 1 and "(NeedsLogin)" in logged[0]
    st.ts(backend="Running", online=True)
    st.run()
    assert any("tailscale no longer logged out (was NeedsLogin" in e for e in st.events)


@pytest.mark.parametrize("backend", ["NeedsMachineAuth", "Stopped"])
def test_other_logged_out_states_take_no_action(st, backend):
    st.ts(backend=backend, online=False)
    st.run(times=4)
    assert f"tailscale: logged out ({backend})" in st.wlog
    assert not [ln for ln in st.calls.splitlines() if ln.startswith("sudo ")]


def test_control_plane_outage_restarts_tailscaled_after_three_runs_then_waits(st):
    st.ts(backend="Running", online=False)
    st.run(times=2)
    assert st.count(TS_RESTART) == 0
    st.run()
    assert st.count(TS_RESTART) == 1
    st.run(times=5)
    assert st.count(TS_RESTART) == 1, "at most once per 30 minutes"
    assert st.count(NM_RESTART) == 0 and st.reboots() == 0
    st.set_state("last_ts_restart", str(int(time.time()) - 1801))
    st.run()
    assert st.count(TS_RESTART) == 2
    assert st.count(NM_RESTART) == 0 and st.reboots() == 0


def test_dead_tailscaled_with_internet_never_reboots_in_daylight(st):
    (st.fake / "ts.json").unlink()  # tailscaled not answering: no status at all
    st.run(times=12)
    assert "tailscale: UNREACHABLE (no status: tailscaled not answering)" in st.wlog
    assert st.count(TS_RESTART) == 1
    assert st.count(NM_RESTART) == 0 and st.reboots() == 0


def test_networking_restart_budget_at_night(st):
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(now=NIGHT, times=2)
    st.age_outage()
    st.run(now=NIGHT, times=4)
    assert st.count(NM_RESTART) == 1, "one restart per 30 minutes"
    assert st.reboots() == 0
    assert "REBOOT DEFERRED until sunrise" in st.wlog
    st.set_state("last_net_restart", str(int(time.time()) - 1801))
    st.run(now=NIGHT)
    assert st.count(NM_RESTART) == 2
    st.set_state("last_net_restart", str(int(time.time()) - 1801))
    st.run(now=NIGHT)
    assert st.count(NM_RESTART) == 2, "a second futile restart doubles the wait to 60 minutes"


def test_router_class_gets_no_networking_restart_at_night(st):
    st.internet(False)  # link up, gateway silent
    st.ts(backend="Running", online=False)
    st.run(now=NIGHT, times=2)
    st.age_outage()
    st.run(now=NIGHT, times=4)
    assert "uplink: router" in st.wlog
    assert st.count(NM_RESTART) == 0 and st.reboots() == 0
    assert st.wlog.count("networking restart for uplink router DEFERRED until sunrise") == 5
    st.run(now=DAY)
    assert st.count(NM_RESTART) == 1, "the first daylight run restarts networking"


def test_outage_end_resets_the_budget(st):
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(now=NIGHT, times=2)
    assert st.count(NM_RESTART) == 1
    st.internet(True)
    (st.fake / "no_route").unlink()
    st.ts(backend="Running", online=True)
    st.run(now=NIGHT)
    st.internet(False)
    (st.fake / "no_route").touch()
    st.run(now=NIGHT)
    assert st.count(NM_RESTART) == 1, "a new outage waits for its second failed run"


def test_router_class_reboots_at_most_once_per_six_hours(st):
    st.internet(False)  # link up, gateway silent
    st.ts(backend="Running", online=False)
    st.run(times=2)
    st.age_outage()
    st.run()
    assert "uplink: router" in st.wlog
    assert st.reboots() == 1
    st.set_state("last_reboot", str(int(time.time()) - 3700))
    st.run()
    assert st.reboots() == 1
    assert "REBOOT SKIPPED (cooldown for uplink router" in st.wlog


def test_link_reboot_interval_doubles_within_one_outage(st):
    """1 h, 2 h, 4 h, then 6 h between watchdog reboots in one link outage."""
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(times=2)
    st.age_outage(30000)
    st.run()
    assert st.reboots() == 1
    for done, wait in ((1, 7200), (2, 14400), (3, 21600), (4, 21600)):
        st.set_state("last_reboot", str(int(time.time()) - wait + 120))
        st.run()
        assert st.reboots() == done, f"reboot {done + 1} must wait {wait}s"
        assert f"< {wait}s since last watchdog reboot" in st.wlog
        st.set_state("last_reboot", str(int(time.time()) - wait - 10))
        st.run()
        assert st.reboots() == done + 1
    assert "outage_reboots=5" in st.state_file.read_text()


def test_no_reboot_while_dawn_process_runs(st):
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(times=2)
    st.age_outage()
    with open(st.lock, "w") as fd:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st.run(times=2)
        assert st.reboots() == 0
        assert "REBOOT DEFERRED while dawn_process runs" in st.wlog
    st.run()
    assert st.reboots() == 1


def test_isp_outage_does_nothing(st):
    st.internet(False, gateway=True)
    st.ts(backend="Running", online=False)
    st.run(times=4)
    assert "uplink: isp" in st.wlog
    assert not [ln for ln in st.calls.splitlines() if ln.startswith("sudo ")]


def test_icmp_blocked_but_tailscale_online_does_nothing(st):
    st.internet(False)
    st.ts(backend="Running", online=True)
    st.run(times=4)
    assert "internet probes fail but tailscale is online: no action" in st.wlog
    assert not [ln for ln in st.calls.splitlines() if ln.startswith("sudo ")]


def test_hung_tailscale_ends_the_run_through_the_timeout(st):
    (st.fake / "ts_hang").touch()
    st.run()
    assert "timeout -k 5 15 tailscale status --json" in st.calls
    assert "tailscale: UNREACHABLE (tailscale status timed out after 15s)" in st.wlog
    assert "WATCHDOG RUN COMPLETE" in st.wlog
    st.run(times=2)
    assert st.count(TS_RESTART) == 1, "a wedged tailscaled is restarted after three runs"
    assert st.count(NM_RESTART) == 0 and st.reboots() == 0


def test_every_tailscale_call_has_a_timeout(st):
    st.ts(backend="Running", online=False)
    st.run(times=3)
    ts_calls = [ln for ln in st.calls.splitlines() if ln.startswith("tailscale ")]
    timed = [ln for ln in st.calls.splitlines() if ln.startswith("timeout ") and " tailscale " in ln]
    assert ts_calls and len(timed) == len(ts_calls)


@pytest.mark.parametrize("garbage", [
    b"\x00\xff\xfe garbage\nts_fail_count=9x\nnet_fail_count=\nlast_net_restart=abc\n",
    b"ts_fail_count=" + b"9" * 40 + b"\nnet_fail_count=-3\n",
    b"",
])
def test_corrupt_state_reads_as_no_history(st, garbage):
    (st.scripts / "logs").mkdir()
    st.state_file.write_bytes(garbage)
    st.ts(backend="Running", online=False)
    st.run()
    assert st.count(TS_RESTART) == 0, "a fresh count needs three runs"
    assert "(1/3 runs)" in st.wlog
    st.state_file.write_bytes(garbage)
    st.internet(False)
    (st.fake / "no_route").touch()
    st.run()
    assert st.count(NM_RESTART) == 0
    assert "(1/2 runs)" in st.wlog


def test_restart_time_in_the_future_blocks_rather_than_acts(st):
    """A clock stepped back (no RTC) must not read as "long ago"."""
    st.ts(backend="Running", online=False)
    st.run(times=2)
    st.set_state("last_ts_restart", str(int(time.time()) + 86400))
    st.run()
    assert st.count(TS_RESTART) == 0


def test_rms_config_locates_the_station_for_the_sun_test(st):
    shutil.copy(SCRIPTS / "camera_fps_guard.py", st.scripts / "camera_fps_guard.py")
    st.config({})
    rms = st.home / "source" / "RMS"
    rms.mkdir(parents=True)
    (rms / ".config").write_text("[System]\nstationID: RO0017\nlatitude: 45.0\nlongitude: 25.0\nelevation: 300\n")
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(now=NIGHT, times=2)
    st.age_outage()
    st.run(now=NIGHT)
    assert st.reboots() == 0
    assert "REBOOT DEFERRED until sunrise" in st.wlog


@pytest.mark.parametrize("start", ["", "garbage", str(10 ** 11)])
def test_unknown_outage_start_reads_as_just_begun(st, start):
    st.internet(False)
    (st.fake / "no_route").touch()
    st.ts(backend="Running", online=False)
    st.run(times=2)
    st.set_state("outage_start", start)
    st.run()
    assert st.reboots() == 0
    assert "REBOOT WAITS: outage" in st.wlog
