"""rtsp_relay: the relay's camera map and the RMS / colour-capture repointing.

The property that matters most: RMS keeps controlling its camera. RMS takes the
camera it talks DVRIP to (day/night mode switch) from the first dotted-quad IP
in its device URL, so a relay URL on 127.0.0.1 would silently break the mode
switch on gmnro01 (Ghirdoveni), which has switch_camera_modes on.
"""

from __future__ import annotations

import re
from pathlib import Path

import rtsp_relay as rr

XMEYE = "rtsp://admin:@192.168.0.144:554/user=admin&password=&channel=1&stream=0.sdp"
PORT = 8554


def _cfg() -> dict:
    return {"stations": {
        "RO000A": {"camera_rtsp": XMEYE, "rms_data_path": "/home/gmn/RMS_data/cam1"},
        "GH662S": {"camera_rtsp": "rtsp://root:secret@192.168.0.94:554/stream0"},
        "NOCAM": {"camera_rtsp": ""},
    }}


def _rms_config(tmp_path: Path, device: str) -> Path:
    cfg = tmp_path / ".config"
    cfg.write_text(f"[System]\nstationID: RO000A\ndevice: {device}\nfps: 25\n")
    return cfg


def _device(cfg: Path) -> str:
    return re.search(r"(?m)^device: (.*)$", cfg.read_text()).group(1)


def test_relay_paths_name_camN_or_code_and_skip_empty():
    paths = rr.relay_paths(_cfg())
    assert set(paths) == {"cam1", "gh662s"}
    assert paths["cam1"].host == "192.168.0.144"
    # MediaMTX rejects an empty-password userinfo; a real password stays.
    assert paths["cam1"].url.startswith("rtsp://192.168.0.144:554/")
    assert paths["gh662s"].url == "rtsp://root:secret@192.168.0.94:554/stream0"


def test_yaml_pulls_cameras_directly_by_default():
    # The ffmpeg re-stamping shim made RMS drop 280-2,700 frames per 10 min
    # on 11 stations (2026-09-28); MediaMTX's own pull dropped ~0.
    text = rr.mediamtx_yaml(rr.relay_paths(_cfg()), "127.0.0.1:8554")
    assert "ffmpeg" not in text and "runOn" not in text
    assert text.count("sourceOnDemand: yes") == 2
    assert 'source: "rtsp://192.168.0.144:554/user=admin' in text


def test_restamp_uses_always_on_publishers_with_a_stall_timeout():
    text = rr.mediamtx_yaml(rr.relay_paths(_cfg()), "127.0.0.1:8554", restamp=True)
    # runOnDemand's single SIGINT left hung ffmpeg publishers pulling the camera.
    assert "runOnDemand" not in text
    assert text.count("runOnInit: >") == 2 and text.count("runOnInitRestart: yes") == 2
    assert "-timeout 10000000" in text
    assert "rtspAddress: 127.0.0.1:8554" in text
    assert "rtsp://127.0.0.1:8554/cam1" in text


def test_rms_device_url_keeps_the_camera_rms_controls():
    path = rr.relay_paths(_cfg())["cam1"]
    url = rr.rms_device_url(path, PORT, XMEYE)
    assert url == "rtsp://localhost:8554/cam1?camera=192.168.0.144"
    assert rr.rms_control_ip(url) == "192.168.0.144"
    assert rr.rms_control_ip("rtsp://127.0.0.1:8554/cam1") == "127.0.0.1"


def test_rms_device_url_carries_a_real_dvrip_password():
    path = rr.relay_paths(_cfg())["cam1"]
    old = "rtsp://192.168.0.144:554/user=admin&password=s3cret&channel=1&stream=0.sdp"
    url = rr.rms_device_url(path, PORT, old)
    assert url.endswith("?camera=192.168.0.144&user=admin&password=s3cret")


def test_repoint_rewrites_the_device_line_with_a_backup(tmp_path):
    cfg = _rms_config(tmp_path, XMEYE)
    changed, msg = rr.repoint_rms_config(cfg, rr.relay_paths(_cfg()), PORT, "20260928_130000")
    assert changed, msg
    assert _device(cfg) == "rtsp://localhost:8554/cam1?camera=192.168.0.144"
    assert "fps: 25" in cfg.read_text()
    assert _device(Path(f"{cfg}.bak.relay.20260928_130000")) == XMEYE


def test_repoint_is_a_no_op_the_second_time(tmp_path):
    cfg = _rms_config(tmp_path, XMEYE)
    paths = rr.relay_paths(_cfg())
    rr.repoint_rms_config(cfg, paths, PORT, "a")
    changed, msg = rr.repoint_rms_config(cfg, paths, PORT, "b")
    assert not changed and "already" in msg
    assert not Path(f"{cfg}.bak.relay.b").exists()


def test_repoint_upgrades_the_old_loopback_relay_form(tmp_path):
    cfg = _rms_config(tmp_path, "rtsp://127.0.0.1:8554/cam1")
    changed, _ = rr.repoint_rms_config(cfg, rr.relay_paths(_cfg()), PORT, "a")
    assert changed
    assert _device(cfg) == "rtsp://localhost:8554/cam1?camera=192.168.0.144"


def test_repoint_refuses_a_different_stream_on_the_same_camera(tmp_path):
    # RO0014 (Vâlcele): RMS read /Streaming/Channels/101, the relay pulls the
    # XMEye .sdp path. Same camera, maybe not the same stream: left to a person.
    other = "rtsp://admin:@192.168.0.144:554/Streaming/Channels/101"
    cfg = _rms_config(tmp_path, other)
    changed, msg = rr.repoint_rms_config(cfg, rr.relay_paths(_cfg()), PORT, "a")
    assert not changed and "repoint by hand" in msg
    assert _device(cfg) == other


def test_repoint_skips_a_camera_without_camera_rtsp(tmp_path):
    cfg = _rms_config(tmp_path, "rtsp://192.168.0.50:554/stream")
    changed, msg = rr.repoint_rms_config(cfg, rr.relay_paths(_cfg()), PORT, "a")
    assert not changed and "no camera_rtsp" in msg


def test_capture_patch_points_colour_at_loopback():
    patch = rr.capture_patch(_cfg(), rr.relay_paths(_cfg()), PORT)
    assert patch == {"stations": {
        "RO000A": {"capture_rtsp": "rtsp://127.0.0.1:8554/cam1"},
        "GH662S": {"capture_rtsp": "rtsp://127.0.0.1:8554/gh662s"},
    }}


def test_station_discovery_reports_the_camera_behind_the_relay(tmp_path):
    import station_collect

    cfg = _rms_config(tmp_path, "rtsp://localhost:8554/cam1?camera=192.168.0.144")
    assert station_collect._parse_rms_config(cfg)["cam_ip"] == "192.168.0.144"
    cfg.write_text(f"stationID: RO000A\ndevice: {XMEYE}\n")
    assert station_collect._parse_rms_config(cfg)["cam_ip"] == "192.168.0.144"
