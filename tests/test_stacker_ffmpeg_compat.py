"""stacker.passthrough_args picks the frame-passthrough flag the local ffmpeg knows.

2026-09-21..23: gmnro05 and gmnro08 run ffmpeg 4.2, which rejects -fps_mode, so
every chunk decoded to 0 frames and no stack or timelapse was made there.
"""

from __future__ import annotations

import subprocess

import pytest

import rovimen_media
import stacker


@pytest.fixture(autouse=True)
def _clear_cache():
    rovimen_media._ffmpeg_version.cache_clear()
    yield
    rovimen_media._ffmpeg_version.cache_clear()


def _fake_version(monkeypatch, first_line: str) -> None:
    def run(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, stdout=first_line + "\nbuilt with gcc\n", stderr="")
    monkeypatch.setattr(stacker.subprocess, "run", run)


@pytest.mark.parametrize("line", [
    "ffmpeg version 4.2.7-0ubuntu0.1 Copyright (c) 2000-2022 the FFmpeg developers",
    "ffmpeg version 5.0.1 Copyright (c) 2000-2022",
])
def test_old_ffmpeg_gets_vsync(monkeypatch, line):
    _fake_version(monkeypatch, line)
    assert stacker.passthrough_args() == ["-vsync", "passthrough"]


@pytest.mark.parametrize("line", [
    "ffmpeg version 5.1.7-0+deb12u1+rpt1 Copyright",
    "ffmpeg version 6.1.1-3ubuntu5 Copyright",
    "ffmpeg version 8.0.1-3ubuntu2 Copyright",
    "ffmpeg version n7.1 Copyright",
    "ffmpeg version N-112345-gabcdef Copyright",
])
def test_current_or_unknown_ffmpeg_gets_fps_mode(monkeypatch, line):
    _fake_version(monkeypatch, line)
    assert stacker.passthrough_args() == ["-fps_mode", "passthrough"]


@pytest.mark.parametrize("exc", [
    FileNotFoundError("ffmpeg"),
    subprocess.TimeoutExpired(["ffmpeg", "-version"], 30),
])
def test_failed_probe_gets_vsync_and_is_not_cached(monkeypatch, exc):
    # 2026-10-30, gmnro05 (Bistrița): a probe that failed under dawn load fell
    # back to -fps_mode, ffmpeg 4.2 rejected it and the cached answer failed
    # every stack of the run. -vsync works on 4.2 through 8.0.
    def boom(cmd, **kw):
        raise exc
    monkeypatch.setattr(stacker.subprocess, "run", boom)
    assert stacker.passthrough_args() == ["-vsync", "passthrough"]

    _fake_version(monkeypatch, "ffmpeg version 6.1.1-3ubuntu5 Copyright")
    assert stacker.passthrough_args() == ["-fps_mode", "passthrough"]


def test_decode_args_uses_the_detected_flag(monkeypatch):
    _fake_version(monkeypatch, "ffmpeg version 4.2.7-0ubuntu0.1")
    assert stacker._decode_args(0, 0)[1] == ["-vsync", "passthrough"]
