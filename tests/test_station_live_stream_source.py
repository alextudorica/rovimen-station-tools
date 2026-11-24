"""The live view reads the camera the way colour capture does.

Each camera is pulled once, through the loopback RTSP relay (the development guide):
colour capture reads ``capture_rtsp`` when it is set. /api/stream used to
take ``rtsp_url`` or ``camera_rtsp``, opening a second pull on the camera
itself whenever someone watched the live view.
"""

from __future__ import annotations

import pytest

import station_api

CAM = "RO000H"


class _Refuse(Exception):
    pass


@pytest.fixture
def started(monkeypatch):
    """Record the ffmpeg argv /api/stream starts, without starting it."""
    cmds: list[list[str]] = []

    def popen(cmd, *a, **kw):
        cmds.append(cmd)
        raise _Refuse

    monkeypatch.setattr(station_api.subprocess, "Popen", popen)
    return cmds


def _source(cmds: list[list[str]]) -> str:
    cmd = cmds[-1]
    return cmd[cmd.index("-i") + 1]


def _get(monkeypatch, cam_cfg: dict):
    monkeypatch.setattr(station_api, "load_config", lambda: {"stations": {CAM: cam_cfg}})
    return station_api.app.test_client().get(f"/api/stream/{CAM}")


@pytest.mark.parametrize("cam_cfg", [
    {"capture_rtsp": "rtsp://127.0.0.1:8554/ro000h",
     "camera_rtsp": "rtsp://192.168.42.10:554/stream"},
    {"capture_rtsp": "rtsp://127.0.0.1:8554/ro000h",
     "rtsp_url": "rtsp://192.168.42.10:554/other"},
])
def test_stream_reads_the_relay_colour_capture_reads(monkeypatch, started, cam_cfg):
    resp = _get(monkeypatch, cam_cfg)
    assert resp.status_code == 500          # the stubbed Popen refused to start
    assert _source(started) == "rtsp://127.0.0.1:8554/ro000h"


def test_without_a_relay_the_camera_url_is_used(monkeypatch, started):
    _get(monkeypatch, {"camera_rtsp": "rtsp://192.168.42.10:554/stream"})
    assert _source(started) == "rtsp://192.168.42.10:554/stream"


def test_rtsp_url_still_wins_over_camera_rtsp(monkeypatch, started):
    _get(monkeypatch, {"rtsp_url": "rtsp://192.168.42.10:554/a",
                       "camera_rtsp": "rtsp://192.168.42.10:554/b"})
    assert _source(started) == "rtsp://192.168.42.10:554/a"
