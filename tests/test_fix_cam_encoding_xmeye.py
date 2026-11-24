"""Tests for the XMEye/DVRIP encoder target in rovimen-scripts/fix_cam_encoding.sh.

Same shape as test_fix_cam_encoding_hdipc.py — the real script runs against a
fixture config.json with stubs first on PATH and the FIXCAM_* path seams
pointed at tmp_path — with two additions the DVRIP family needs:

* a ``timeout`` stub, which answers the script's ``/dev/tcp/<ip>/34567``
  reachability probes from a fixture list and hands every other command to the
  real timeout, so a camera can be "up" without a listening socket;
* a fake ``~/rovimen_scripts/dvrip_vendored.py``. That path is the script's own
  first choice for DVRIPCam (it prefers the bundled module over any installed
  package), and HOME is tmp_path here, so the fake needs no production seam.
  It serves the camera's config documents from a JSON file and appends every
  ``set_info`` to a log, which is what lets these tests assert *which* writes
  the script issued.

A Simplify.Encode write bounces the camera's stream, so "did not write when
nothing had drifted" is the property that matters most — including when the
target itself comes from a ``camera_venc`` override.
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

# Fleet XMEye standard enforced when no camera_venc override exists.
FLEET_TARGET = ("720P", "CBR", 8192, 6)

TIMEOUT_STUB = """#!/usr/bin/env bash
# Stub timeout: answer /dev/tcp reachability probes from $FAKE_DIR/tcp_open,
# pass everything else to the real timeout.
dur="$1"; shift
cmd="$*"
case "$cmd" in
    */dev/tcp/*)
        target="${cmd##*/dev/tcp/}"
        grep -qxF "$target" "$FAKE_DIR/tcp_open" 2>/dev/null && exit 0
        exit 1
        ;;
esac
exec "$REAL_TIMEOUT" "$dur" "$@"
"""

# No HTTP config surface on this family: any curl call is a bug, and must not
# reach the network from a test either.
CURL_STUB = """#!/usr/bin/env bash
echo "$@" >> "$FAKE_DIR/curl.log"
exit 7
"""

# The script's waits are real 5s/10s/30s sleeps; the tests must not serve them.
SLEEP_STUB = """#!/usr/bin/env bash
exit 0
"""

# The script's embedded Python waits too (time.sleep(3) after an encoder write,
# 5 s and 30 s between reconnects). Loaded through PYTHONPATH as sitecustomize,
# this makes those waits free without touching the script.
NO_SLEEP_SITECUSTOMIZE = "import time\ntime.sleep = lambda *_a, **_k: None\n"

# The subnet scan must not actually scan during tests.
NMAP_STUB = """#!/usr/bin/env bash
exit 0
"""

# Stands in for the bundled dvrip_vendored.py the script loads by path.
DVRIP_FAKE = '''"""Fake DVRIPCam for the fix_cam_encoding XMEye tests."""

import copy
import json
import os
from datetime import datetime


def _log(entry):
    with open(os.environ["DVRIP_LOG"], "a") as fh:
        fh.write(json.dumps(entry) + "\\n")


class DVRIPCam:
    def __init__(self, ip, *args, **kwargs):
        self.ip = ip
        with open(os.environ["DVRIP_STATE"]) as fh:
            self.state = json.load(fh)

    def login(self, *args, **kwargs):
        return True

    def get_info(self, key):
        return copy.deepcopy(self.state.get(key))

    def set_info(self, key, value):
        _log({"ip": self.ip, "set": key, "value": value})
        return {"Ret": 100}

    def get_time(self):
        return datetime.utcnow()

    def set_time(self, when):
        _log({"ip": self.ip, "set": "time", "value": when.isoformat()})

    def close(self):
        pass
'''


def _station(ip: str, code: str = "RO001E", **extra) -> dict:
    entry = {
        "rms_data_path": f"/home/rmsuser/RMS_data/{code}",
        "camera_rtsp": f"rtsp://admin:@{ip}:554/stream=0.sdp",
        "camera_api": "xmeye",
        "rotate": False,
    }
    entry.update(extra)
    return {code: entry}


def _camera_state(
    resolution: str = "720P",
    rc: str = "CBR",
    bitrate: int = 8192,
    quality: int = 6,
    substream: bool = False,
    ntp: dict | None = None,
) -> dict:
    """Camera config documents, everything but the encoder already at standard.

    Only the encoder is under test here, so DayNightColor/exposure/gain/fog and
    the reboot schedule are seeded compliant — an unrelated drift would show up
    as extra set_info rows in every assertion. ``ntp`` adds a NetWork.NetNTP
    document; without it the camera answers None, as for an unreadable key.
    """
    state = {
        "Simplify.Encode": [
            {
                "MainFormat": {
                    "Video": {
                        "Compression": "H.264",
                        "Resolution": resolution,
                        "BitRateControl": rc,
                        "BitRate": bitrate,
                        "Quality": quality,
                        "FPS": 25,
                    }
                },
                "ExtraFormat": {"VideoEnable": substream},
            }
        ],
        "Camera": {
            "Param": [
                {
                    "DayNightColor": "0x00000001",
                    "IrcutSwap": 1,
                    "EsShutter": "0x00000000",
                    "WhiteBalance": "0x00000002",
                    "DncThr": 50,
                    "ElecLevel": 40,
                    "AeSensitivity": 1,
                    "ExposureParam": {
                        "LeastTime": "0x00009C40",
                        "Level": 0,
                        "MostTime": "0x00009C40",
                    },
                    "GainParam": {"AutoGain": 1, "Gain": 60},
                }
            ],
            "ClearFog": [{"enable": False, "level": 30}],
        },
        "General.AutoMaintain": {"AutoRebootDay": "Everyday", "AutoRebootHour": 12},
    }
    if ntp is not None:
        state["NetWork.NetNTP"] = ntp
    return state


@pytest.fixture()
def fixcam_env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_dir = tmp_path / "fake"
    home = tmp_path / "home"
    scripts = home / "rovimen_scripts"
    for d in (fake_bin, fake_dir, home, scripts):
        d.mkdir()

    real_timeout = shutil.which("timeout")
    assert real_timeout, "coreutils timeout is required by fix_cam_encoding.sh"

    stubs = (
        ("timeout", TIMEOUT_STUB),
        ("curl", CURL_STUB),
        ("sleep", SLEEP_STUB),
        ("nmap", NMAP_STUB),
    )
    for name, body in stubs:
        p = fake_bin / name
        p.write_text(body)
        p.chmod(p.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    (scripts / "dvrip_vendored.py").write_text(DVRIP_FAKE)
    dvrip_log = fake_dir / "dvrip.log"
    dvrip_log.write_text("")

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
            "REAL_TIMEOUT": real_timeout,
            "DVRIP_LOG": str(dvrip_log),
            "DVRIP_STATE": str(fake_dir / "camera.json"),
            "FIXCAM_PYTHON": sys.executable,
            "FIXCAM_CONFIG": str(tmp_path / "config.json"),
            "FIXCAM_LOG": str(tmp_path / "fix_cam_encoding.log"),
            "FIXCAM_STATUS": str(tmp_path / "fixcam_status.json"),
            # One attempt per camera: a dead camera must not cost 5 real minutes.
            "FIXCAM_WAIT_ATTEMPTS": "1",
        }
    )
    return env, tmp_path, fake_dir, dvrip_log


def write_config(tmp_path: Path, stations: dict, **top_level) -> None:
    doc = {"stations": stations}
    doc.update(top_level)
    (tmp_path / "config.json").write_text(json.dumps(doc))


def write_camera(fake_dir: Path, ip: str = "192.0.2.11", **state) -> None:
    """Seed the camera's config documents and mark its DVRIP port reachable."""
    (fake_dir / "camera.json").write_text(json.dumps(_camera_state(**state)))
    (fake_dir / "tcp_open").write_text(f"{ip}/34567\n")


def run_fixcam(env) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", str(FIXCAM)], env=env, capture_output=True, text=True, timeout=50
    )


def read_log(tmp_path: Path) -> str:
    log = tmp_path / "fix_cam_encoding.log"
    return log.read_text() if log.exists() else ""


def encode_writes(dvrip_log: Path) -> list[dict]:
    """The Simplify.Encode set_info calls, newest last."""
    rows = [json.loads(ln) for ln in dvrip_log.read_text().splitlines() if ln.strip()]
    return [r for r in rows if r["set"] == "Simplify.Encode"]


def written_video(dvrip_log: Path) -> dict:
    writes = encode_writes(dvrip_log)
    assert len(writes) == 1, writes
    return writes[0]["value"][0]["MainFormat"]["Video"]


def video_target(video: dict) -> tuple:
    return (
        video["Resolution"],
        video["BitRateControl"],
        video["BitRate"],
        video["Quality"],
    )


def test_no_override_enforces_the_fleet_target(fixcam_env):
    """Unconfigured stations keep the historical 720P/CBR/8192/Q6 behaviour."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    write_camera(fake_dir, resolution="1080P", rc="VBR", bitrate=4096, quality=4)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert video_target(written_video(dvrip_log)) == FLEET_TARGET
    log = read_log(tmp_path)
    assert "set Encode 720P/CBR/8192/Q6 (was ('1080P', 'VBR', 4096, 4))" in log


def test_no_override_at_standard_writes_nothing(fixcam_env):
    """A camera already at the fleet target must not be written to at all."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert dvrip_log.read_text().strip() == ""
    assert "Encode OK (720P/CBR/8192/Q6, sub-stream off)" in read_log(tmp_path)


def test_camera_ntp_on_is_turned_off(fixcam_env):
    """gmnro21 (Vârteșcoiu): factory NTP in GMT+8 kept both clocks 8 h ahead."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    write_camera(fake_dir, ntp={"Enable": True, "TimeZone": 13, "UpdatePeriod": 60})

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    rows = [json.loads(ln) for ln in dvrip_log.read_text().splitlines() if ln.strip()]
    assert [r for r in rows if r["set"].startswith("NetWork.NetNTP")] == [
        {"ip": "192.0.2.11", "set": "NetWork.NetNTP.Enable", "value": False}
    ]
    assert "turned camera NTP off (TimeZone 13)" in read_log(tmp_path)


def test_camera_ntp_off_is_left_alone(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    write_camera(fake_dir, ntp={"Enable": False, "TimeZone": 13, "UpdatePeriod": 60})

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert dvrip_log.read_text().strip() == ""
    assert "NTP" not in read_log(tmp_path)


def _automaintain_writes(dvrip_log: Path) -> list:
    rows = [json.loads(ln) for ln in dvrip_log.read_text().splitlines() if ln.strip()]
    return [r["value"] for r in rows if r["set"] == "General.AutoMaintain"]


def test_camera_reboot_day_makes_the_auto_reboot_weekly(fixcam_env):
    """gmnro20 (Brăila): every camera reboot re-rolls a lossy/clean state."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", camera_reboot_day="Sunday"))
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert _automaintain_writes(dvrip_log) == [{"AutoRebootDay": "Sunday", "AutoRebootHour": 12}]
    assert "set AutoReboot=Sunday/12 (was Everyday/12)" in read_log(tmp_path)


def test_station_wide_reboot_day_and_hour(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"), camera_reboot_day="Never", camera_reboot_hour=11)
    write_camera(fake_dir)

    assert run_fixcam(env).returncode == 0
    assert _automaintain_writes(dvrip_log) == [{"AutoRebootDay": "Never", "AutoRebootHour": 11}]


def test_unknown_reboot_day_keeps_everyday(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", camera_reboot_day="weekly"))
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert _automaintain_writes(dvrip_log) == []
    assert "AutoReboot OK" in read_log(tmp_path)


def test_bitrate_override_changes_only_the_bitrate(fixcam_env):
    """Bacau RO001E: 8192 kbps starves the night scene at Quality 6."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", camera_venc={"bitrate": 12288}))
    write_camera(fake_dir)  # at the fleet target, i.e. drifted from the override

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    # The override is a delta: only BitRate moves, in the comparison that
    # decided to write and in the payload that was written.
    assert video_target(written_video(dvrip_log)) == ("720P", "CBR", 12288, 6)
    log = read_log(tmp_path)
    assert "set Encode 720P/CBR/12288/Q6 (was ('720P', 'CBR', 8192, 6))" in log


def test_override_with_no_drift_issues_no_write(fixcam_env):
    """The override target is compared, not blindly re-applied every day.

    A Simplify.Encode write bounces the camera, so an override the camera
    already satisfies has to be a no-op.
    """
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", camera_venc={"bitrate": 12288}))
    write_camera(fake_dir, bitrate=12288)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    assert dvrip_log.read_text().strip() == ""
    assert "Encode OK (720P/CBR/12288/Q6, sub-stream off)" in read_log(tmp_path)


def test_resolution_quality_and_rc_alias_are_overridable(fixcam_env):
    """Every XMEye encoder field can be overridden; "rc" aliases bitrate_control."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(
        tmp_path,
        _station(
            "192.0.2.11",
            camera_venc={"resolution": "1080P", "rc": "VBR", "quality": 4},
        ),
    )
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    # bitrate was not overridden, so it keeps the fleet default.
    assert video_target(written_video(dvrip_log)) == ("1080P", "VBR", 8192, 4)


def test_per_camera_override_wins_over_the_station_wide_one(fixcam_env):
    """Same precedence the HDIPC family has had since camera_venc was added."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(
        tmp_path,
        _station("192.0.2.11", camera_venc={"bitrate": 12288}),
        camera_venc={"bitrate": 6144},
    )
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert written_video(dvrip_log)["BitRate"] == 12288


def test_station_wide_override_applies_without_a_per_camera_key(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"), camera_venc={"bitrate": 6144})
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert written_video(dvrip_log)["BitRate"] == 6144


def test_unusable_override_value_falls_back_and_says_so(fixcam_env):
    """A value with whitespace would shift every later column of the row."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11", camera_venc={"resolution": "720P HD"}))
    write_camera(fake_dir)

    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr

    # Fleet default kept, so the camera is left at standard and nothing is written.
    assert dvrip_log.read_text().strip() == ""
    assert "ERROR: ignoring camera_venc.resolution=" in result.stderr
    assert "Encode OK (720P/CBR/8192/Q6, sub-stream off)" in read_log(tmp_path)


def test_hdipc_camera_ignores_the_xmeye_columns(fixcam_env):
    """An override travels to whichever family the camera belongs to, once."""
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(
        tmp_path,
        _station(
            "192.0.2.10",
            code="RO0014",
            camera_api="hdipc",
            camera_venc={"bitrate": 12288, "quality": 4},
        ),
    )
    write_camera(fake_dir, ip="192.0.2.10")

    # No HDIPC fixture replies -> the camera reads as dead, which is enough:
    # the run must not fall through to the DVRIP path or fail parsing the row.
    result = run_fixcam(env)
    assert result.returncode == 0, result.stderr
    assert dvrip_log.read_text().strip() == ""
    log = read_log(tmp_path)
    assert "HTTP API not reachable after 5 min, skipping" in log
    assert json.loads((tmp_path / "fixcam_status.json").read_text())["cameras"][
        "RO0014"
    ] == {"api": "hdipc", "state": "skipped"}


# --only <CODE>: camera_fps_guard.py re-applies one camera at night.


def _run_only(env, code: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(FIXCAM), "--only", code], env=env,
                          capture_output=True, text=True, timeout=50)


def test_only_touches_that_one_camera_and_leaves_the_status_file(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    stations = _station("192.0.2.11", code="RO001E")
    stations.update(_station("192.0.2.12", code="RO001F"))
    write_config(tmp_path, stations)
    write_camera(fake_dir, ip="192.0.2.11", resolution="1080P")

    result = _run_only(env, "RO001E")
    assert result.returncode == 0, result.stderr

    rows = [json.loads(ln) for ln in dvrip_log.read_text().splitlines() if ln.strip()]
    assert rows and {r["ip"] for r in rows} == {"192.0.2.11"}
    assert "RO001F" not in read_log(tmp_path)
    assert not (tmp_path / "fixcam_status.json").exists()


def test_only_reports_an_unreachable_camera_as_2_without_rescanning(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    # no write_camera: the DVRIP port is closed

    result = _run_only(env, "RO001E")
    assert result.returncode == 2, result.stderr
    assert "scanning" not in read_log(tmp_path)


def test_only_an_unknown_code_fails(fixcam_env):
    env, tmp_path, fake_dir, dvrip_log = fixcam_env
    write_config(tmp_path, _station("192.0.2.11"))
    write_camera(fake_dir)

    result = _run_only(env, "XX9999")
    assert result.returncode == 1
    assert dvrip_log.read_text().strip() == ""
