"""Encoder CPU cuts from the 2026-09-26 review.

- VAAPI encodes cap their CPU filter threads (measured on the admin workstation: 7.6 -> 5.3
  CPU-s per 20 s 720p chunk at two filter threads), behind
  services.reencode.vaapi_threads.
- Colour sampling reads its 5 frames with one ffmpeg instead of five, with
  identical pixels (~170 ms less per clip).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

import color_calibration as cc
import encoder
import flags_manager

CAM = "RO000H"
NIGHT = "20260322"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


class TestVaapiFilterThreads:
    @pytest.mark.parametrize("setting,want", [
        (None, 2), ("auto", 2), ("AUTO", 2), (0, 0), (3, 3), ("4", 4), ("junk", 2), (-1, 0)])
    def test_setting(self, setting, want):
        cfg = {"_encode_threads": 2, "services": {"reencode": {}}}
        if setting is not None:
            cfg["services"]["reencode"]["vaapi_threads"] = setting
        assert encoder._vaapi_filter_threads(cfg) == want

    def test_auto_follows_the_realtime_share_without_a_batch(self):
        cfg = {"stations": {"A": {}, "B": {}}}
        assert encoder._vaapi_filter_threads(cfg) == encoder._libx264_thread_count(cfg)


class _Popen:
    launches: list[list[str]] = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        _Popen.launches.append(list(cmd))

    def communicate(self, timeout=None):
        Path(self.cmd[-1]).write_bytes(b"e" * 10)
        return b"", b""

    def kill(self):
        pass


@pytest.fixture
def run_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
    monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)
    monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
    monkeypatch.setattr(encoder.subprocess, "Popen", _Popen)
    encoder._quant_floor.clear()

    def go(hw: bool, **reencode) -> list[str]:
        _Popen.launches = []
        monkeypatch.setattr(encoder, "_find_vaapi_device",
                            lambda *a, **k: "/dev/dri/renderD128" if hw else None)
        cfg = {
            "videocapture_path": str(tmp_path / "color_capture"),
            "segment_duration": 20, "_encode_threads": 2,
            "stations": {CAM: {}}, "overlay": {"enabled": False},
            "services": {"reencode": {"enabled": True, **reencode}},
        }
        night = Path(cfg["videocapture_path"]) / CAM / NIGHT
        night.mkdir(parents=True, exist_ok=True)
        mkv = night / f"{CAM}_{NIGHT}_21{len(list(night.glob('*_color.mkv'))):04d}_color.mkv"
        mkv.write_bytes(b"s" * 1000)
        flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
        assert encoder.process_chunk(mkv, cfg)
        return _Popen.launches[0]
    return go


class TestFinalCommandThreads:
    def test_vaapi_caps_both_filter_thread_pools_by_default(self, run_chunk):
        cmd = run_chunk(True)
        an = cmd.index("-an")
        assert cmd[an + 1:an + 5] == ["-filter_threads", "2", "-filter_complex_threads", "2"]
        assert "-threads" not in cmd

    def test_zero_restores_ffmpeg_defaults(self, run_chunk):
        cmd = run_chunk(True, vaapi_threads=0)
        assert "-filter_threads" not in cmd and "-filter_complex_threads" not in cmd

    def test_explicit_count(self, run_chunk):
        cmd = run_chunk(True, vaapi_threads=3)
        assert cmd[cmd.index("-filter_threads") + 1] == "3"

    def test_libx264_keeps_its_own_threads_only(self, run_chunk):
        cmd = run_chunk(False)
        assert "-filter_threads" not in cmd
        assert cmd[cmd.index("-threads") + 1] == "2"


def _loop_only(monkeypatch):
    monkeypatch.setattr(cc, "_sample_frames_one_process", lambda *a, **k: (None, False))


def _clip(path: Path, timescale: bool = False, gop: int = 12) -> None:
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
           "-i", "testsrc2=size=320x180:rate=25:duration=4,noise=alls=20:allf=t",
           "-vf", "format=yuvj420p", "-c:v", "libx264", "-preset", "ultrafast",
           "-g", str(gop)]
    if timescale:
        cmd += ["-video_track_timescale", "1000"]
    subprocess.run(cmd + [str(path)], check=True)


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
class TestOneProcessSampling:
    @pytest.mark.parametrize("timescale,gop,full_range", [
        (False, 12, False), (True, 12, False), (False, 50, True)])
    def test_same_pixels_as_one_ffmpeg_per_frame(self, tmp_path, monkeypatch,
                                                 timescale, gop, full_range):
        clip = tmp_path / "c.mkv"
        _clip(clip, timescale, gop)
        one = cc.sample_frames(clip, full_range=full_range)
        with monkeypatch.context() as m:
            _loop_only(m)
            loop = cc.sample_frames(clip, full_range=full_range)
        assert one is not None and one.shape == (5, 180, 320, 3)
        assert np.array_equal(one, loop)

    def test_it_is_one_launch(self, tmp_path, monkeypatch):
        clip = tmp_path / "c.mkv"
        _clip(clip)
        real = subprocess.run
        ffmpegs: list[list[str]] = []

        def spy(cmd, **kw):
            if cmd[0] == "ffmpeg":
                ffmpegs.append(cmd)
            return real(cmd, **kw)
        monkeypatch.setattr(cc.subprocess, "run", spy)
        assert cc.sample_frames(clip) is not None
        assert len(ffmpegs) == 1


class _R:
    def __init__(self, out):
        self.returncode = 0
        self.stdout = out


def _fake_run(one_process_out, seek_out=bytes(12)):
    calls: list[str] = []

    def run(cmd, **kw):
        if cmd[0] == "ffprobe":
            calls.append("probe")
            return _R('{"streams": [{"width": 2, "height": 2}], "format": {"duration": "20"}}')
        if "-filter_complex" in cmd:
            calls.append("one")
            if isinstance(one_process_out, Exception):
                raise one_process_out
            return _R(one_process_out)
        calls.append("seek")
        return _R(seek_out)
    return run, calls


def test_a_short_one_process_read_falls_back_to_per_frame_seeks(tmp_path, monkeypatch):
    run, calls = _fake_run(bytes(12 * 3))        # 3 frames, not 5
    monkeypatch.setattr(cc.subprocess, "run", run)
    frames = cc.sample_frames(tmp_path / "c.mkv")
    assert frames.shape == (5, 2, 2, 3)
    assert calls == ["probe", "one"] + ["seek"] * 5


class TestFullNightSkipsUnstackedChunks:
    """With the stacker on, the full-night pass leaves unstacked chunks raw so
    their stacks are fitted on the capture, not on an encoded clip."""

    @pytest.fixture
    def night(self, tmp_path, monkeypatch):
        monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
        monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)
        monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
        monkeypatch.setattr(encoder.subprocess, "Popen", _Popen)
        encoder._quant_floor.clear()
        _Popen.launches = []
        cfg = {
            "videocapture_path": str(tmp_path / "color_capture"),
            "segment_duration": 20, "stations": {CAM: {}}, "overlay": {"enabled": False},
            "services": {"reencode": {"enabled": True}},
        }
        d = Path(cfg["videocapture_path"]) / CAM / NIGHT
        d.mkdir(parents=True)
        chunks = []
        for i in range(3):
            c = d / f"{CAM}_{NIGHT}_21000{i}_color.mkv"
            c.write_bytes(b"s" * 1000)
            chunks.append(c)
        flags_manager.mark_ready_batch(CAM, NIGHT, [c.name for c in chunks], cfg)
        flags_manager.mark_stacked(CAM, NIGHT, chunks[0].name, cfg)
        flags_manager.mark_stacked(CAM, NIGHT, chunks[2].name, cfg)
        return cfg, chunks

    def _encoded(self) -> set[str]:
        return {Path(c[c.index("-i") + 1]).name for c in _Popen.launches}

    def test_stacker_on_encodes_only_stacked_chunks(self, night):
        cfg, chunks = night
        cfg["services"]["stacker"] = {"enabled": True}
        assert encoder.process_night(CAM, NIGHT, cfg) is True
        assert self._encoded() == {chunks[0].name, chunks[2].name}

    @pytest.mark.parametrize("stacker", [None, {"enabled": False}])
    def test_without_the_stacker_everything_is_encoded(self, night, stacker):
        cfg, chunks = night
        if stacker is not None:
            cfg["services"]["stacker"] = stacker
        encoder.process_night(CAM, NIGHT, cfg)
        assert self._encoded() == {c.name for c in chunks}


def test_a_stalled_one_process_read_does_not_retry_frame_by_frame(tmp_path, monkeypatch):
    run, calls = _fake_run(subprocess.TimeoutExpired("ffmpeg", 60))
    monkeypatch.setattr(cc.subprocess, "run", run)
    assert cc.sample_frames(tmp_path / "c.mkv") is None
    assert calls == ["probe", "one"]
