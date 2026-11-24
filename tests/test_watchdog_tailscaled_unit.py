"""rovimen_watchdog.sh must check and restart the tailscaled unit that exists.

Tailscale is a snap on gmnro08 (Brașov), so its daemon is
snap.tailscale.tailscaled. The watchdog asked systemd about `tailscaled` and
logged "tailscaled service: inactive" on every run while Tailscale worked, and
its recovery rungs would have restarted a unit that does not exist.

Full watchdog runs against stubs on PATH: internet answers, tailscaled reports
Running with Self.Online false, so the third run restarts tailscaled.
"""

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
WATCHDOG = SCRIPTS / "rovimen_watchdog.sh"

STUBS = {
    "ping": 'echo "ping $*" >> "$FAKE_LOG"; exit 0\n',
    "sudo": 'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\ncase "$1" in */reboot|reboot) exit 0 ;; esac\nexec "$@"\n',
    "systemctl": (
        'echo "systemctl $*" >> "$FAKE_LOG"\n'
        'case "$*" in\n'
        '  "list-unit-files $FAKE_TS_UNIT.service --no-legend") echo "$FAKE_TS_UNIT.service enabled enabled" ;;\n'
        '  "is-active $FAKE_TS_UNIT") echo active ;;\n'
        'esac\n'
        'exit 0\n'
    ),
    # Internet up, tailscaled offline: the third run restarts tailscaled.
    "tailscale": 'echo \'{"BackendState": "Running", "Self": {"Online": false}}\'\n',
    "getent": "exit 0\n",
}


@pytest.fixture()
def run_watchdog(tmp_path):
    fake_bin, home = tmp_path / "bin", tmp_path / "home"
    fake_bin.mkdir()
    for name, body in STUBS.items():
        p = fake_bin / name
        p.write_text("#!/usr/bin/env bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    scripts = home / "rovimen_scripts"
    scripts.mkdir(parents=True)
    for mod in ("rovimen_config.py", "rovimen_night.py"):
        shutil.copy(SCRIPTS / mod, scripts / mod)
    (scripts / "config.json").write_text(json.dumps({"watchdog": {"allow_reboot": False}}))
    calls = tmp_path / "calls.log"

    def run(unit: str) -> tuple[str, str]:
        calls.write_text("")
        e = os.environ.copy()
        e.update({"PATH": f"{fake_bin}:{e['PATH']}", "HOME": str(home), "FAKE_LOG": str(calls),
                  "FAKE_TS_UNIT": unit, "ROVIMEN_WATCHDOG_NOW": "2026-09-29T10:00:00Z",
                  "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0", "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0",
                  "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0"})
        state = scripts / "logs" / ".watchdog_state"
        if state.exists():
            state.unlink()
        for _ in range(3):
            subprocess.run(["bash", str(WATCHDOG)], env=e, capture_output=True, text=True, timeout=120)
        return (scripts / "logs" / "watchdog.log").read_text(), calls.read_text()

    return run


def test_snap_tailscale_is_checked_and_restarted_by_its_unit(run_watchdog):
    wlog, calls = run_watchdog("snap.tailscale.tailscaled")
    assert "tailscaled service: active (snap.tailscale.tailscaled)" in wlog
    assert "sudo -n systemctl restart snap.tailscale.tailscaled" in calls
    assert "sudo -n systemctl restart tailscaled\n" not in calls


def test_packaged_tailscale_keeps_the_tailscaled_unit(run_watchdog):
    wlog, calls = run_watchdog("tailscaled")
    assert "tailscaled service: active (tailscaled)" in wlog
    assert "sudo -n systemctl restart tailscaled\n" in calls
