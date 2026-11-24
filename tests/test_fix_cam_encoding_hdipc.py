"""Tests for the HDIPC/Heroseen family in rovimen-scripts/fix_cam_encoding.sh.

The functional tests run the real script against a fixture config.json with stub
curl/sleep/nmap binaries first on PATH. Camera replies live in FAKE_DIR as
<endpoint>.json (a missing file = connection refused, i.e. a dead camera); every
curl invocation is appended to FAKE_LOG so the tests can assert *which* writes
the script issued. A setVencConf write restarts the camera, so "did not POST when
nothing had drifted" is the property that matters most here.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXCAM = REPO_ROOT / "rovimen-scripts" / "fix_cam_encoding.sh"

# Fleet HDIPC standard, per docs/camera_firmware_heroseen.md (2026-07-09).
VENC_AT_STANDARD = {
    "channel": 0,
    "pic_width": 1280,
    "pic_height": 720,
    "frame_rate": 25,
    "gop": 12,
    "bitrate": 8192,
    "encode_type": 1,
    "encode_profile": 1,
    "rc_mode": 1,
    "max_framerate": 25,
    "code": 0,
    "device_ip": "192.0.2.10",
}
REBOOT_AT_STANDARD = {"mode": 2, "day_week": 0, "hour": 12, "minute": 0, "code": 0}

CURL_STUB = """#!/usr/bin/env bash
# Stub curl for fix_cam_encoding HDIPC tests.
url=""; payload=""; method=GET
while [ $# -gt 0 ]; do
    case "$1" in
        -X) method="$2"; shift 2 ;;
        -d) payload="$2"; shift 2 ;;
        http://*) url="$1"; shift ;;
        *) shift ;;
    esac
done
host="${url#http://}"; host="${host%%/*}"
endpoint="${url##*/action/}"
echo "$method $host $endpoint $payload" >> "$FAKE_LOG"
if [ "$method" = "POST" ]; then
    echo '{"code":0}'
    exit 0
fi
f="$FAKE_DIR/$endpoint.json"
if [ -f "$f" ]; then cat "$f"; exit 0; fi
exit 7
"""

# The script's waits are real 5s/30s sleeps; the tests must not serve them.
SLEEP_STUB = """#!/usr/bin/env bash
exit 0
"""

# 192.0.2.x (TEST-NET-1) never answers, so every /dev/tcp probe of port 34567
# waited out its full `timeout 2`. Fail those probes at once, which is what
# they would do anyway, and pass every other command to the real timeout.
TIMEOUT_STUB = """#!/usr/bin/env bash
dur="$1"; shift
case "$*" in
    */dev/tcp/*) exit 1 ;;
esac
exec "$REAL_TIMEOUT" "$dur" "$@"
"""

# The script's embedded Python waits too (time.sleep(3) after an encoder write,
# 5 s and 30 s between reconnects). Loaded through PYTHONPATH as sitecustomize,
# this makes those waits free without touching the script.
NO_SLEEP_SITECUSTOMIZE = "import time\ntime.sleep = lambda *_a, **_k: None\n"

# The XMEye subnet scan must not actually scan during tests.
NMAP_STUB = """#!/usr/bin/env bash
exit 0
"""


def _station(ip: str, code: str = "RO0010", **extra) -> dict:
    entry = {
        "rms_data_path": f"/home/rmsuser/RMS_data/{code}",
        "camera_rtsp": f"rtsp://admin:123456@{ip}:554/stream_0",
        "rotate": False,
    }
    entry.update(extra)
    return {code: entry}


@pytest.fixture()
def fixcam_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_dir = tmp_path / "fake"
    home = tmp_path / "home"
    for d in (fake_bin, fake_dir, home):
        d.mkdir()

    real_timeout = shutil.which("timeout")
    assert real_timeout, "coreutils timeout is required by fix_cam_encoding.sh"

    for name, body in (("curl", CURL_STUB), ("sleep", SLEEP_STUB), ("nmap", NMAP_STUB),
                       ("timeout", TIMEOUT_STUB)):
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    fake_log = fake_dir / "curl.log"
    fake_log.write_text("")

    no_sleep = tmp_path / "pysite"
    no_sleep.mkdir()
    (no_sleep / "sitecustomize.py").write_text(NO_SLEEP_SITECUSTOMIZE)

    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:{env['PATH']}",
            "PYTHONPATH": os.pathsep.join(filter(None, [str(no_sleep), env.get("PYTHONPATH")])),
            "HOME": str(home),
            "FAKE_DIR": str(fake_dir),
            "FAKE_LOG": str(fake_log),
            "REAL_TIMEOUT": real_timeout,
            "FIXCAM_PYTHON": sys.executable,
            "FIXCAM_CONFIG": str(tmp_path / "config.json"),
            "FIXCAM_LOG": str(tmp_path / "fix_cam_encoding.log"),
            "FIXCAM_STATUS": str(tmp_path / "fixcam_status.json"),
            # One attempt per camera: a dead camera must not cost 5 real minutes.
            "FIXCAM_WAIT_ATTEMPTS": "1",
        }
    )
    return env, tmp_path, fake_dir, fake_log


def write_config(tmp_path: Path, stations: dict) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"stations": stations}))


def write_camera(fake_dir: Path, venc: dict | None = None, reboot: dict | None = None) -> None:
    if venc is not None:
        (fake_dir / "getVencConf.json").write_text(json.dumps(venc))
    if reboot is not None:
        (fake_dir / "getRebootConf.json").write_text(json.dumps(reboot))


def run_fixcam(env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(FIXCAM)],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def read_log(tmp_path: Path) -> str:
    log = tmp_path / "fix_cam_encoding.log"
    return log.read_text() if log.exists() else ""


def read_status(tmp_path: Path) -> dict:
    return json.loads((tmp_path / "fixcam_status.json").read_text())


def test_fix_cam_encoding_bash_syntax():
    subprocess.run(["bash", "-n", str(FIXCAM)], check=True)


def test_no_drift_issues_no_write(fixcam_env):
    """The whole point: a camera already at standard must not be POSTed to.

    setVencConf restarts the camera (~15-20 s of lost frames), so an
    unconditional daily write would punch a nightly hole for nothing.
    """
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10", camera_api="hdipc"))
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    calls = fake_log.read_text()
    assert "POST" not in calls
    log = read_log(tmp_path)
    assert "Encode OK (1280x720/25fps/8192k/rc1/gop12)" in log
    assert "AutoReboot OK" in log

    status = read_status(tmp_path)
    assert status["ok"] == ["RO0010"]
    assert status["cameras"]["RO0010"] == {"api": "hdipc", "state": "ok"}
    assert status["none_enforced"] is False


def test_encoder_drift_is_corrected(fixcam_env):
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10", camera_api="hdipc"))
    drifted = dict(VENC_AT_STANDARD, gop=15, pic_width=1920, pic_height=1080)
    write_camera(fake_dir, drifted, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    posts = [ln for ln in fake_log.read_text().splitlines() if ln.startswith("POST")]
    assert len(posts) == 1, posts
    assert "setVencConf" in posts[0]
    payload = json.loads(posts[0].split("setVencConf ", 1)[1])
    assert payload["gop"] == 12
    assert (payload["pic_width"], payload["pic_height"]) == (1280, 720)
    assert payload["frame_rate"] == 25
    assert payload["bitrate"] == 8192
    assert payload["rc_mode"] == 1
    # Codec fields are echoed back as read, never re-codeced from a constant.
    assert payload["encode_type"] == drifted["encode_type"]
    assert payload["encode_profile"] == drifted["encode_profile"]
    assert payload["channel"] == drifted["channel"]

    assert "set Encode 1280x720/25fps/8192k/rc1/gop12" in read_log(tmp_path)
    assert read_status(tmp_path)["ok"] == ["RO0010"]


def test_reboot_hour_drift_is_corrected(fixcam_env):
    """Live state found on Brasov RO0010: daily reboot drifted to 15:00."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10", camera_api="hdipc"))
    write_camera(fake_dir, VENC_AT_STANDARD, dict(REBOOT_AT_STANDARD, hour=15))

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    posts = [ln for ln in fake_log.read_text().splitlines() if ln.startswith("POST")]
    assert len(posts) == 1, posts
    assert "setRebootConf" in posts[0]
    payload = json.loads(posts[0].split("setRebootConf ", 1)[1])
    assert payload == {"mode": 2, "day_week": 0, "hour": 12, "minute": 0}
    assert "set AutoReboot=Everyday/12:00 (was mode=2 hour=15)" in read_log(tmp_path)


def test_reboot_hour_is_configurable(fixcam_env):
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(
        tmp_path, _station("192.0.2.10", camera_api="hdipc", camera_reboot_hour=11)
    )
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    assert run_fixcam(env).returncode == 0
    posts = [ln for ln in fake_log.read_text().splitlines() if ln.startswith("POST")]
    assert len(posts) == 1
    assert json.loads(posts[0].split("setRebootConf ", 1)[1])["hour"] == 11


def test_venc_targets_are_configurable(fixcam_env):
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(
        tmp_path,
        _station("192.0.2.10", camera_api="hdipc", camera_venc={"gop": 25, "bitrate": 6144}),
    )
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    assert run_fixcam(env).returncode == 0
    posts = [ln for ln in fake_log.read_text().splitlines() if ln.startswith("POST")]
    assert len(posts) == 1
    payload = json.loads(posts[0].split("setVencConf ", 1)[1])
    assert payload["gop"] == 25
    assert payload["bitrate"] == 6144
    # Unspecified fields keep the fleet standard.
    assert payload["pic_height"] == 720


def test_unreadable_venc_fails_loudly_without_posting(fixcam_env):
    """An HTML error page must never be spliced into a JSON payload."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10", camera_api="hdipc"))
    # Readiness probe passes (pic_width present) but the fields are unusable.
    (fake_dir / "getVencConf.json").write_text('{"pic_width":"n/a"}')
    write_camera(fake_dir, reboot=REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 1, result.stderr
    assert "POST" not in fake_log.read_text()
    log = read_log(tmp_path)
    assert "ERROR getVencConf unreadable" in log
    assert "DONE WITH FAILURES" in log
    assert read_status(tmp_path)["cameras"]["RO0010"]["state"] == "failed"


def test_all_cameras_skipped_is_loud(fixcam_env):
    """"All done (ok: none)" is how weeks of unchecked settings looked healthy."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10", camera_api="hdipc"))
    # No fixture files -> the stub curl exits 7, i.e. a dead camera.

    result = run_fixcam(env)
    # Exit stays 0: a known-dead camera must not page every day.
    assert result.returncode == 0, result.stderr

    log = read_log(tmp_path)
    assert "HTTP API not reachable after 5 min, skipping" in log
    assert "NOTHING ENFORCED" in log
    assert "All done" not in log

    status = read_status(tmp_path)
    assert status["skipped"] == ["RO0010"]
    assert status["none_enforced"] is True
    assert status["cameras"]["RO0010"] == {"api": "hdipc", "state": "skipped"}


def test_config_with_no_cameras_is_an_error(fixcam_env):
    """A config that yields no camera rows must not report success."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, {"RO0010": {"rms_data_path": "/x"}})  # no camera_rtsp

    result = run_fixcam(env)
    assert result.returncode == 1, result.stderr
    log = read_log(tmp_path)
    assert "no camera rows parsed" in log
    assert "All done" not in log


def test_probe_detects_hdipc_without_config_key(fixcam_env):
    """Stations whose live config predates camera_api still get enforced."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.10"))  # no camera_api
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert "Fixing 192.0.2.10 (HDIPC HTTP API)" in read_log(tmp_path)
    assert read_status(tmp_path)["cameras"]["RO0010"]["api"] == "hdipc"


def test_xmeye_camera_never_touches_the_http_api(fixcam_env):
    """An XMEye station's behaviour is unchanged — no HTTP config surface."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", code="RO000T", camera_api="xmeye"))
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    # No getVencConf probe, no setVencConf: the DVRIP path owns this camera.
    assert fake_log.read_text().strip() == ""
    log = read_log(tmp_path)
    assert "HDIPC" not in log
    assert "not reachable after 5 min, skipping" in log
    assert read_status(tmp_path)["cameras"]["RO000T"] == {
        "api": "xmeye",
        "state": "skipped",
    }


def test_mixed_family_station_dispatches_per_camera(fixcam_env):
    """Valcele shape: three XMEye cameras plus one HDIPC on the same LAN."""
    env, tmp_path, fake_dir, fake_log = fixcam_env
    stations = {}
    stations.update(_station("192.0.2.11", code="RO000T", camera_api="xmeye"))
    stations.update(_station("192.0.2.10", code="RO0014", camera_api="hdipc"))
    write_config(tmp_path, stations)
    write_camera(fake_dir, VENC_AT_STANDARD, REBOOT_AT_STANDARD)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    status = read_status(tmp_path)
    assert status["cameras"]["RO0014"] == {"api": "hdipc", "state": "ok"}
    assert status["cameras"]["RO000T"] == {"api": "xmeye", "state": "skipped"}
    # The HDIPC camera was enforced, so the run is not a total no-op.
    assert status["none_enforced"] is False
    # Only the HDIPC camera's IP ever saw an HTTP request.
    assert "192.0.2.11" not in fake_log.read_text()
