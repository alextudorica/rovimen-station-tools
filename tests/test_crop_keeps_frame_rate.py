"""Crops keep the clip's own frames.

A station colour chunk (RTSP copied into Matroska) reports 1000 fps, its
millisecond timebase, and an mp4 encode without ``-fps_mode passthrough``
wrote it as constant 1000 fps: 5,595 frames for a 5.6 s crop of an RO0017
(Tărâța) clip on 2026-11-11, 30 s of encoding, past nginx's 30 s read
timeout, so the dashboard showed "Crop download failed". With passthrough the
same crop was 140 frames in 3.2 s. Synthetic test clips report their real
rate, so this checks the command rather than re-encoding one.
"""
from __future__ import annotations

import pytest

import routes.media as media


def _passthrough(cmd: list[str]) -> bool:
    return any(a == "-fps_mode" and b == "passthrough" for a, b in zip(cmd, cmd[1:]))


@pytest.mark.parametrize("overlay_cfg", [{}, {"enabled": True}])
def test_clip_crop_keeps_source_timestamps(overlay_cfg: dict) -> None:
    cmd = media._build_crop_cmd(
        "in.mkv", "out.mp4", 0.1, 0.1, 0.5, 0.5,
        "RO0017", "RO0017_20261111_002652_color.mkv", 5.169, 5.561,
        overlay_cfg, {},
    )
    assert _passthrough(cmd)
    assert cmd.index("-fps_mode") < cmd.index("out.mp4")


def test_still_frame_crop_is_unchanged() -> None:
    cmd = media._build_crop_cmd(
        "in.mkv", "out.jpg", 0.1, 0.1, 0.5, 0.5,
        "RO0017", "RO0017_20261111_002652_color.mkv", 5.0, 0.1,
        {}, {}, vframes=1,
    )
    assert not _passthrough(cmd)
