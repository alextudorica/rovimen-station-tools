"""rovimen_watchdog.sh reboot rung waits for daylight.

2026-09-08: an upstream repeater outage cut gmnro01 (Ghirdoveni) off the
internet, and the watchdog rebooted it every hour, night included, chopping
capture that was recording fine to local disk. Its reboots were then switched
off entirely (watchdog.allow_reboot=false), which also stopped the daily 13:45
reboot for three weeks.

Full watchdog runs against stubs first on PATH: ping never answers and `ip`
lists no default route (uplink class link), tailscaled gives no status. The
ladder waits one run, restarts networking on the second, and reaches the
reboot rung on the third. The sudo stub records a reboot and never runs one.
"""

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

# 45.0N 25.0E: sun about -38 deg at 22:00 UTC and +40 deg at 10:00 UTC in late September.
NIGHT = "2026-09-28T22:00:00Z"
DAY = "2026-09-29T10:00:00Z"
RUNS_TO_REBOOT = 3

STUBS = {
    "ping": 'echo "ping $*" >> "$FAKE_LOG"; exit 1\n',
    "ip": "exit 0\n",
    "sudo": (
        'echo "sudo $*" >> "$FAKE_LOG"\n[ "$1" = -n ] && shift\n'
        'case "$1" in */reboot|reboot|shutdown|*/shutdown) exit 0 ;; esac\n'
        'exec "$@"\n'
    ),
    "systemctl": 'echo "systemctl $*" >> "$FAKE_LOG"; exit 0\n',
    "tailscale": "exit 0\n",
    "getent": "exit 2\n",
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
    scripts = home / "rovimen_scripts"
    scripts.mkdir()
    for mod in ("rovimen_config.py", "rovimen_night.py"):
        shutil.copy(SCRIPTS / mod, scripts / mod)
    log = fake_dir / "calls.log"
    log.write_text("")
    e = os.environ.copy()
    e.update({"PATH": f"{fake_bin}:{e['PATH']}", "HOME": str(home), "FAKE_DIR": str(fake_dir),
              "FAKE_LOG": str(log), "ROVIMEN_WATCHDOG_RECOVERY_WAIT": "0",
              "ROVIMEN_WATCHDOG_RECHECK_WAIT": "0", "ROVIMEN_WATCHDOG_SETTLE_WAIT": "0",
              "ROVIMEN_DAWN_LOCK": str(tmp_path / "dawn.lock"),
              "ROVIMEN_RESCUE_HELPER": str(tmp_path / "no-helper"),
              "ROVIMEN_DAWN_CRON": str(tmp_path / "rovimen-dawn"),
              "ROVIMEN_RMS_UNIT_DIR": str(tmp_path / "units")})
    return e, log, home


def write_config(home: Path, cfg: dict) -> None:
    (home / "rovimen_scripts" / "config.json").write_text(json.dumps(cfg))


def run(e: dict, now: str, times: int = RUNS_TO_REBOOT) -> None:
    """Watchdog runs; after each, the outage is dated back past the 30-min reboot minimum."""
    state = Path(e["HOME"]) / "rovimen_scripts" / "logs" / ".watchdog_state"
    for _ in range(times):
        r = subprocess.run(["bash", str(WATCHDOG)], env={**e, "ROVIMEN_WATCHDOG_NOW": now},
                           capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr
        lines = [ln for ln in state.read_text().splitlines() if not ln.startswith("outage_start=")]
        state.write_text("\n".join(lines + [f"outage_start={int(time.time()) - 1900}"]) + "\n")


def wlog(home: Path) -> str:
    f = home / "rovimen_scripts" / "logs" / "watchdog.log"
    return f.read_text() if f.exists() else ""


def rebooted(log: Path) -> bool:
    return re.search(r"^sudo -n \S*reboot$", log.read_text(), re.M) is not None


def test_no_reboot_at_night(env):
    e, log, home = env
    write_config(home, {"latitude": 45.0, "longitude": 25.0})
    run(e, NIGHT)
    assert not rebooted(log)
    assert "REBOOT DEFERRED until sunrise" in wlog(home)


def test_reboot_in_daylight(env):
    e, log, home = env
    write_config(home, {"latitude": 45.0, "longitude": 25.0})
    run(e, DAY, times=RUNS_TO_REBOOT - 1)
    assert not rebooted(log), "the networking restart gets a run to work first"
    run(e, DAY, times=1)
    assert rebooted(log)
    assert "RECOVERY: REBOOTING" in wlog(home)


def test_deferral_costs_no_cooldown(env):
    """The first daytime run after a night of deferrals still reboots."""
    e, log, home = env
    write_config(home, {"latitude": 45.0, "longitude": 25.0})
    run(e, NIGHT, times=4)
    assert not rebooted(log)
    run(e, DAY, times=1)
    assert rebooted(log)


def test_overlay_coords_locate_the_station(env):
    e, log, home = env
    write_config(home, {"overlay": {"coords": "45.00N  25.00E"}})
    run(e, NIGHT)
    assert not rebooted(log)


def test_unknown_location_keeps_old_behaviour(env):
    """No location in config.json or any RMS .config: reboot as before rather than never."""
    e, log, home = env
    write_config(home, {})
    run(e, NIGHT)
    assert rebooted(log)


def test_allow_reboot_false_still_wins(env):
    e, log, home = env
    write_config(home, {"latitude": 45.0, "longitude": 25.0, "watchdog": {"allow_reboot": False}})
    run(e, DAY, times=5)
    assert not rebooted(log)
    assert "REBOOT DISABLED by config" in wlog(home)


# RMS post-processing defers the reboot (2026-11-22: the rung rebooted gmnro09
# (Sibiciu) at 08:56 UTC while RMS ran ApplyRecalibrate on the night). The fake
# dawn_process stands in for the real gate helpers, _rms_runs and _rms_night,
# with what FAKE_DIR/rms.json says, and records the night it was asked about.
FAKE_DAWN_PROCESS = '''
import json, os
from pathlib import Path
from types import SimpleNamespace

def _state():
    return json.loads((Path(os.environ["FAKE_DIR"]) / "rms.json").read_text())

def _rms_runs():
    return {cam: SimpleNamespace(idle=r.get("idle", False), capture_state=r.get("capture_state"))
            for cam, r in _state()["runs"].items()}

def _rms_night(station_id, date_str, rms_data_path, run=None):
    (Path(os.environ["FAKE_DIR"]) / "asked").write_text(" ".join([station_id, date_str, rms_data_path]))
    n = _state().get("night", {})
    return SimpleNamespace(postprocessing=n.get("postprocessing", []), unprocessed=n.get("unprocessed", []))
'''

RMS_CFG = {"latitude": 45.0, "longitude": 25.0,
           "stations": {"RO000Z": {"rms_data_path": "/data/RMS_data"}}}
SESSION = "RO000Z_20260928_163000_000000"
POSTPROCESSING = {"runs": {"RO000Z": {"capture_state": "ended"}}, "night": {"postprocessing": [SESSION]}}
MORNING = "2026-09-29T08:56:00Z"


def fake_rms(e: dict, home: Path, state: dict) -> None:
    (home / "rovimen_scripts" / "dawn_process.py").write_text(FAKE_DAWN_PROCESS)
    (Path(e["FAKE_DIR"]) / "rms.json").write_text(json.dumps(state))


def test_reboot_deferred_while_rms_postprocesses(env):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    run(e, MORNING, times=RUNS_TO_REBOOT + 2)
    assert not rebooted(log)
    assert wlog(home).count("RECOVERY: REBOOT DEFERRED while RMS post-processes the night "
                            "(night 20260928: RO000Z post-processing)") == 3
    assert (Path(e["FAKE_DIR"]) / "asked").read_text() == "RO000Z 20260928 /data/RMS_data"
    # RMS finishes: the next run reboots, because the deferrals consumed no cooldown.
    fake_rms(e, home, {"runs": {"RO000Z": {"capture_state": "ended"}}, "night": {}})
    run(e, MORNING, times=1)
    assert rebooted(log)


def test_detection_running_after_capture_ended_defers(env):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, {"runs": {"RO000Z": {"capture_state": "ended"}}, "night": {"unprocessed": [SESSION]}})
    run(e, MORNING)
    assert not rebooted(log)
    assert "(night 20260928: RO000Z detection)" in wlog(home)


@pytest.mark.parametrize("state", [
    pytest.param({"runs": {"RO000Z": {"capture_state": "ended"}}, "night": {}}, id="rms-done"),
    pytest.param({"runs": {}, "night": {"postprocessing": [SESSION]}}, id="no-rms-process"),
    pytest.param({"runs": {"RO000Z": {"idle": True, "capture_state": "waiting"}},
                  "night": {"postprocessing": [SESSION]}}, id="rms-idle-until-night"),
    pytest.param({"runs": {"RO000Z": {"capture_state": "capturing"}}, "night": {"unprocessed": [SESSION]}},
                 id="rms-capturing-not-detecting"),
    pytest.param({"runs": {"RO999X": {"capture_state": "ended"}}, "night": {"postprocessing": [SESSION]}},
                 id="camera-not-in-config"),
])
def test_reboot_proceeds_when_rms_not_processing(env, state):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, state)
    run(e, MORNING)
    assert rebooted(log)
    assert "post-process" not in wlog(home)


def test_reboot_proceeds_after_dawn_deadline_although_rms_busy(env):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    run(e, "2026-09-29T13:15:00Z")
    assert rebooted(log)
    assert ("RMS still post-processing (night 20260928: RO000Z post-processing; past 13:15 UTC): "
            "rebooting anyway") in wlog(home)


def test_reboot_proceeds_after_six_hour_outage_although_rms_busy(env):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    run(e, MORNING, times=RUNS_TO_REBOOT - 1)
    assert not rebooted(log)
    state = home / "rovimen_scripts" / "logs" / ".watchdog_state"
    lines = [ln for ln in state.read_text().splitlines() if not ln.startswith("outage_start=")]
    state.write_text("\n".join(lines + [f"outage_start={int(time.time()) - 6 * 3600 - 60}"]) + "\n")
    subprocess.run(["bash", str(WATCHDOG)], env={**e, "ROVIMEN_WATCHDOG_NOW": MORNING},
                   capture_output=True, text=True, timeout=120, check=True)
    assert rebooted(log)
    assert "outage over 6 h): rebooting anyway" in wlog(home)


@pytest.mark.parametrize("broken", ["missing-helper", "helper-raises", "bad-config"])
def test_rms_check_error_means_no_deferral(env, broken):
    """No dawn_process, a helper that raises, or an unusable config: reboot as before."""
    e, log, home = env
    write_config(home, RMS_CFG)
    if broken == "helper-raises":
        fake_rms(e, home, POSTPROCESSING)
        (home / "rovimen_scripts" / "dawn_process.py").write_text(
            FAKE_DAWN_PROCESS + "\n\ndef _rms_runs():\n    raise OSError('boom')\n")
    elif broken == "bad-config":
        fake_rms(e, home, POSTPROCESSING)
        write_config(home, {"latitude": 45.0, "longitude": 25.0, "stations": ["RO000Z"]})
    run(e, MORNING)
    assert rebooted(log)
    assert "DEFERRED while RMS" not in wlog(home)
    expected = {"missing-helper": "ModuleNotFoundError: No module named 'dawn_process'",
                "helper-raises": "OSError: boom",
                "bad-config": "AttributeError: 'list' object has no attribute 'get'"}[broken]
    assert f"RECOVERY: RMS post-processing check unavailable (python3: {expected}): not deferring" in wlog(home)


# The check runs under dawn's interpreter: dawn_process imports numpy and PIL,
# which the system python3 lacks on 7 of 20 stations (measured 2026-11-22).
def fake_python(path: Path, log: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/usr/bin/env bash\necho "fakepy {path}" >> "{log}"\nexec python3 "$@"\n')
    path.chmod(0o755)
    return path


def test_rms_check_runs_under_the_dawn_cron_interpreter(env, tmp_path):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    py = fake_python(tmp_path / "venv" / "bin" / "python3", log)
    Path(e["ROVIMEN_DAWN_CRON"]).write_text(
        f"*/15 4-12 * * * gmn nice -n 10 ionice -c 3 {py} {home}/rovimen_scripts/dawn_process.py"
        f" >> {home}/logs/rovimen_dawn.log 2>&1\n")
    run(e, MORNING)
    assert not rebooted(log)
    assert f"fakepy {py}" in log.read_text()
    assert "REBOOT DEFERRED while RMS post-processes the night" in wlog(home)


def test_rms_check_falls_back_to_the_rms_unit_interpreter(env, tmp_path):
    """No dawn cron file (or one naming a missing interpreter): the RMS unit's ExecStart venv."""
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    Path(e["ROVIMEN_DAWN_CRON"]).write_text(
        f"*/15 4-12 * * * gmn {tmp_path}/gone/bin/python3 {home}/rovimen_scripts/dawn_process.py\n")
    py = fake_python(tmp_path / "rmsvenv" / "bin" / "python3", log)
    units = Path(e["ROVIMEN_RMS_UNIT_DIR"])
    units.mkdir()
    (units / "rms-cam1.service").write_text(f"[Service]\nExecStart={py} -m RMS.StartCapture\n")
    run(e, MORNING)
    assert not rebooted(log)
    assert f"fakepy {py}" in log.read_text()


def test_rms_check_without_cron_or_venv_uses_python3(env):
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    run(e, MORNING)
    assert "fakepy" not in log.read_text()
    assert "REBOOT DEFERRED while RMS post-processes the night" in wlog(home)


def test_import_failure_under_dawn_interpreter_is_logged_and_reboots(env, tmp_path):
    """The fleet case: dawn_process cannot import numpy under the chosen interpreter."""
    e, log, home = env
    write_config(home, RMS_CFG)
    fake_rms(e, home, POSTPROCESSING)
    (home / "rovimen_scripts" / "dawn_process.py").write_text("import numpy_not_installed_here\n")
    py = fake_python(tmp_path / "venv" / "bin" / "python3", log)
    Path(e["ROVIMEN_DAWN_CRON"]).write_text(
        f"*/15 4-12 * * * gmn {py} {home}/rovimen_scripts/dawn_process.py\n")
    run(e, MORNING)
    assert rebooted(log)
    assert (f"RECOVERY: RMS post-processing check unavailable ({py}: ModuleNotFoundError: "
            "No module named 'numpy_not_installed_here'): not deferring") in wlog(home)
