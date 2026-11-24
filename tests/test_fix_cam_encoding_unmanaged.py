"""camera_api "openipc" / "none" in rovimen-scripts/fix_cam_encoding.sh.

Such a camera is not ours to manage: DE0018 (Berlin) runs OpenIPC with its own
control on TCP 9600 and its clock from the station's chrony. The property that
matters is that the script never touches it: no HTTP call, no 34567 subnet scan
(which re-IPs the XM camera it finds onto the configured address), no settings.
Harness (stub curl/sleep/nmap/timeout, fixture config) from the HDIPC tests.
"""

import stat
import subprocess

from tests.test_fix_cam_encoding_hdipc import (  # noqa: F401  (fixture)
    FIXCAM,
    REBOOT_AT_STANDARD,
    VENC_AT_STANDARD,
    _station,
    fixcam_env,
    read_log,
    read_status,
    run_fixcam,
    write_camera,
    write_config,
)

# Logs every nmap call, so a subnet scan shows up in the curl log.
NMAP_LOGGING_STUB = """#!/usr/bin/env bash
echo "NMAP $*" >> "$FAKE_LOG"
exit 0
"""


def _log_nmap(tmp_path):
    p = tmp_path / "bin" / "nmap"
    p.write_text(NMAP_LOGGING_STUB)
    p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _openipc(ip="192.0.2.65", code="DE0018", api="openipc"):
    st = _station(ip, code=code, camera_api=api)
    st[code]["camera_rtsp"] = f"rtsp://{ip}:554/"
    return st


def test_openipc_camera_is_never_touched(fixcam_env):
    env, tmp_path, fake_dir, fake_log = fixcam_env
    _log_nmap(tmp_path)
    write_config(tmp_path, _openipc())

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert fake_log.read_text() == ""            # no HTTP call, no nmap scan
    log = read_log(tmp_path)
    assert "camera_api=openipc, not managed by this script" in log
    assert "unmanaged: DE0018" in log
    assert "NOTHING ENFORCED" not in log

    status = read_status(tmp_path)
    assert status["cameras"]["DE0018"] == {"api": "openipc", "state": "unmanaged"}
    assert status["ok"] == [] and status["skipped"] == [] and status["failed"] == []
    assert status["none_enforced"] is False


def test_none_is_the_same_as_openipc(fixcam_env):
    env, tmp_path, fake_dir, fake_log = fixcam_env
    _log_nmap(tmp_path)
    write_config(tmp_path, _openipc(api="none"))

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert fake_log.read_text() == ""
    assert read_status(tmp_path)["cameras"]["DE0018"] == {"api": "none", "state": "unmanaged"}


def test_only_on_an_unmanaged_camera_exits_clean(fixcam_env):
    """The fps guard's fix mode calls --only <CODE>; it must not reach the camera."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    _log_nmap(tmp_path)
    write_config(tmp_path, _openipc())

    result = subprocess.run(["bash", str(FIXCAM), "--only", "DE0018"], env=env,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert fake_log.read_text() == ""
    assert "--only DE0018 done (rc=0, unmanaged)" in read_log(tmp_path)


def test_unmanaged_camera_next_to_a_managed_one(fixcam_env):
    """The managed camera is enforced as before; the unmanaged one is left alone."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    _log_nmap(tmp_path)
    stations = _station("192.0.2.10", code="DE0019", camera_api="hdipc")
    stations.update(_openipc())
    write_config(tmp_path, stations)
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    calls = fake_log.read_text()
    assert "192.0.2.10" in calls
    assert "192.0.2.65" not in calls and "NMAP" not in calls
    status = read_status(tmp_path)
    assert status["ok"] == ["DE0019"]
    assert status["cameras"]["DE0018"]["state"] == "unmanaged"
    assert status["none_enforced"] is False
