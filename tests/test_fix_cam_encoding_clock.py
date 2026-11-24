"""fix_cam_encoding.sh sets a camera's clock only from a synchronised host clock.

A Raspberry Pi has no RTC: after a power cut it boots with the clock it had at
shutdown until NTP answers, and the @reboot run of fix_cam (``sleep 120``) can
come first when the uplink is slow to return. gmnro16 (Bacău) logged wrong
times after the 2026-11-09 power cut. Pushing that clock into a camera moves its
12:00 AutoReboot (camera time) into the night's capture until the next run.

Runs the real script with the fixture from test_fix_cam_encoding_xmeye.py, plus
fake ``timedatectl`` and ``chronyc`` first on PATH. A fake that has no answer
file exits 127, which is what the script sees when the command is missing.
"""

import json
import stat
from pathlib import Path

import pytest

from tests.test_fix_cam_encoding_xmeye import (  # noqa: F401  (fixture)
    _station,
    fixcam_env,
    read_log,
    run_fixcam,
    write_camera,
    write_config,
)

# Answers from $FAKE_DIR/<name>_<subcommand>; no such file = command missing.
TIMEDATECTL_STUB = """#!/usr/bin/env bash
case "$1" in
    show) f="$FAKE_DIR/timedatectl_show" ;;
    status|"") f="$FAKE_DIR/timedatectl_status" ;;
    *) exit 1 ;;
esac
[ -f "$f" ] || exit 127
cat "$f"
"""

CHRONYC_STUB = """#!/usr/bin/env bash
f="$FAKE_DIR/chronyc_tracking"
[ -f "$f" ] || exit 127
cat "$f"
"""

# The fake camera's clock runs this far ahead of UTC.
CAMERA_AHEAD_S = 3600

@pytest.fixture()
def clock_env(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    fake_bin = Path(env["PATH"].split(":", 1)[0])
    for name, body in (("timedatectl", TIMEDATECTL_STUB), ("chronyc", CHRONYC_STUB)):
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    write_config(tmp_path, _station("192.0.2.11"))
    # A camera with NTP on and a drifted reboot slot, so "every other setting"
    # has something to write besides the clock.
    write_camera(fake_dir, ntp={"Enable": True, "TimeZone": 13, "UpdatePeriod": 60})
    state = json.loads((fake_dir / "camera.json").read_text())
    state["General.AutoMaintain"] = {"AutoRebootDay": "Everyday", "AutoRebootHour": 4}
    (fake_dir / "camera.json").write_text(json.dumps(state))
    return env, tmp_path, fake_dir, dvrip_log


def _writes(dvrip_log: Path) -> list[str]:
    return [json.loads(ln)["set"] for ln in dvrip_log.read_text().splitlines() if ln.strip()]


def _status(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "fixcam_status.json").read_text())


def _camera_ahead(fake_dir: Path) -> None:
    """Make the fake camera's clock read CAMERA_AHEAD_S ahead of UTC."""
    vendored = Path(fake_dir).parent / "home" / "rovimen_scripts" / "dvrip_vendored.py"
    src = vendored.read_text()
    vendored.write_text(src.replace(
        "    def get_time(self):\n        return datetime.utcnow()\n",
        "    def get_time(self):\n        from datetime import timedelta\n"
        f"        return datetime.utcnow() + timedelta(seconds={CAMERA_AHEAD_S})\n",
    ))


def test_synced_host_sets_the_camera_time(clock_env):
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    (fake_dir / "timedatectl_show").write_text("yes\n")
    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert "time" in _writes(dvrip_log)
    assert f"synced clock to UTC (was +{CAMERA_AHEAD_S}s off)" in read_log(tmp_path)
    status = _status(tmp_path)
    assert status["host_clock_synced"] is True and status["host_clock_source"] == "timedatectl"


def test_unsynced_host_leaves_the_time_and_applies_everything_else(clock_env):
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    (fake_dir / "timedatectl_show").write_text("no\n")
    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    writes = _writes(dvrip_log)
    assert "time" not in writes
    assert "General.AutoMaintain" in writes and "NetWork.NetNTP.Enable" in writes
    log = read_log(tmp_path)
    assert "clock not synchronised, camera time left alone" in log
    assert "synced clock" not in log
    status = _status(tmp_path)
    assert status["host_clock_synced"] is False and status["host_clock_source"] == "timedatectl"
    assert status["ok"] == ["RO001E"]


def test_chrony_normal_overrides_a_timedatectl_no(clock_env):
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    (fake_dir / "timedatectl_show").write_text("no\n")
    (fake_dir / "chronyc_tracking").write_text(
        "Reference ID    : C0A80001 (192.168.0.1)\nLeap status     : Normal\n")
    assert run_fixcam(env).returncode == 0
    assert "time" in _writes(dvrip_log)
    assert _status(tmp_path)["host_clock_source"] == "chronyc"


def test_chrony_not_synchronised_leaves_the_time(clock_env):
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    (fake_dir / "chronyc_tracking").write_text(
        "Reference ID    : 00000000 ()\nLeap status     : Not synchronised\n")
    assert run_fixcam(env).returncode == 0
    assert "time" not in _writes(dvrip_log)
    assert _status(tmp_path)["host_clock_source"] == "chronyc"


def test_old_systemd_status_line_is_read(clock_env):
    """systemd before 239 has no `show -p NTPSynchronized`; status says 'NTP synchronized'."""
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    (fake_dir / "timedatectl_status").write_text(
        "      Local time: Sat 2026-11-21 20:00:00 UTC\n NTP synchronized: yes\n")
    assert run_fixcam(env).returncode == 0
    assert "time" in _writes(dvrip_log)
    assert _status(tmp_path)["host_clock_source"] == "timedatectl-status"


def test_missing_timedatectl_and_chronyc_counts_as_unsynced(clock_env):
    env, tmp_path, fake_dir, dvrip_log = clock_env
    _camera_ahead(fake_dir)
    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    writes = _writes(dvrip_log)
    assert "time" not in writes and "General.AutoMaintain" in writes
    assert "clock not synchronised, camera time left alone" in read_log(tmp_path)
    status = _status(tmp_path)
    assert status["host_clock_synced"] is False and status["host_clock_source"] == "unknown"
