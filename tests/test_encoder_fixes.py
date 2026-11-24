"""Encoder robustness fixes from the 2026-09-26 review.

- The full-night budget sees the whole per-chunk cost, colour sampling included.
- A VAAPI runtime error retries the chunk once on libx264 instead of failing it
  on every pass (it was never marked, so never uploaded).
- A sampling timeout leaves the clip's colour as captured instead of failing it.
- The libx264 timeout scales with the clip, threads and preset.
- state.json records the quantizer used, and when the capture was kept.
- Every VAAPI probe encodes at the capture size.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

import color_calibration as cc
import encoder
import rovimen_media
import flags_manager
import overlay_details

REPO = Path(__file__).resolve().parent.parent
CAM = "RO000H"
NIGHT = "20260322"


class _Popen:
    """ffmpeg stand-in: writes ``size`` bytes, or fails when ``fail_if`` says so."""
    launches: list[list[str]] = []
    timeouts: list[float | None] = []
    size = 400
    fail_if = staticmethod(lambda cmd: False)

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 1 if _Popen.fail_if(cmd) else 0
        _Popen.launches.append(list(cmd))

    def communicate(self, timeout=None):
        _Popen.timeouts.append(timeout)
        if self.returncode == 0:
            Path(self.cmd[-1]).write_bytes(b"e" * _Popen.size)
            return b"", b""
        return b"", b"[h264_vaapi] Failed to upload frame"

    def kill(self):
        pass


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "stations": {CAM: {"rotate": False}},
        "services": {"reencode": {"enabled": True}},
        "overlay": {"enabled": False},
        "_encode_threads": 2,
    }


@pytest.fixture
def chunk(cfg, monkeypatch):
    night = Path(cfg["videocapture_path"]) / CAM / NIGHT
    night.mkdir(parents=True)
    mkv = night / f"{CAM}_{NIGHT}_210000_color.mkv"
    mkv.write_bytes(b"s" * 1000)
    flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
    monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
    monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)
    monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
    monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
    monkeypatch.setattr(encoder.subprocess, "Popen", _Popen)
    encoder._quant_floor.clear()
    _Popen.launches, _Popen.timeouts, _Popen.size = [], [], 400
    _Popen.fail_if = staticmethod(lambda cmd: False)
    return mkv


def _entry(cfg, mkv) -> dict:
    return flags_manager.load(CAM, NIGHT, cfg)["chunks"][mkv.name]


def test_the_budget_counts_colour_sampling(chunk, cfg, monkeypatch):
    def slow_fit(*a, **k):
        time.sleep(0.3)
        return None
    monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", slow_fit)
    outcomes: list[encoder.ChunkOutcome] = []
    assert encoder.process_chunk(chunk, cfg, on_encoded=outcomes.append)
    assert outcomes[0].wall_s >= 0.3


class TestVaapiRuntimeFallback:
    def test_a_vaapi_failure_is_retried_once_on_libx264(self, chunk, cfg, monkeypatch):
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        _Popen.fail_if = staticmethod(lambda cmd: "-vaapi_device" in cmd)
        assert encoder.process_chunk(chunk, cfg) is True
        assert len(_Popen.launches) == 2
        assert "h264_vaapi" in _Popen.launches[0]
        assert "libx264" in _Popen.launches[1] and "-vaapi_device" not in _Popen.launches[1]
        assert not any("hwupload" in a for a in _Popen.launches[1])
        assert _Popen.launches[1][-2] == f"{encoder.ENCODED_TAG}=CRF21"
        assert _entry(cfg, chunk)["enc"] == "CRF21"
        # libx264 gets its own (longer) timeout
        assert _Popen.timeouts[1] > _Popen.timeouts[0]

    def test_a_chunk_failing_on_both_encoders_fails_once_each(self, chunk, cfg, monkeypatch):
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        _Popen.fail_if = staticmethod(lambda cmd: True)
        assert encoder.process_chunk(chunk, cfg) is False
        assert len(_Popen.launches) == 2
        assert _entry(cfg, chunk)["reencoded"] is False

    def test_a_libx264_failure_is_not_retried(self, chunk, cfg):
        _Popen.fail_if = staticmethod(lambda cmd: True)
        assert encoder.process_chunk(chunk, cfg) is False
        assert len(_Popen.launches) == 1


class TestSamplingTimeout:
    def test_sample_frames_returns_none_when_the_probe_hangs(self, tmp_path, monkeypatch):
        def hang(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
        monkeypatch.setattr(cc.subprocess, "run", hang)
        assert cc.sample_frames(tmp_path / "c.mkv") is None
        assert cc.sample_avg_frame(tmp_path / "c.mkv") is None

    def test_sample_frames_keeps_the_frames_read_before_a_stall(self, tmp_path, monkeypatch):
        seeks = {"n": 0}

        class _R:
            def __init__(self, out, text=False):
                self.returncode = 0
                self.stdout = out

        def run(cmd, **kw):
            if cmd[0] == "ffprobe":
                return _R('{"streams": [{"width": 2, "height": 2}], "format": {"duration": "20"}}')
            if "-filter_complex" in cmd:
                return _R(b"")            # the one-process sampler came back short
            seeks["n"] += 1
            if seeks["n"] > 2:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))
            return _R(bytes(range(12)))
        monkeypatch.setattr(cc.subprocess, "run", run)
        frames = cc.sample_frames(tmp_path / "c.mkv")
        assert frames is not None and frames.shape == (2, 2, 2, 3)
        assert seeks["n"] == 3            # stopped at the first stalled seek

    def test_a_sampling_timeout_encodes_the_colour_as_captured(self, chunk, cfg, monkeypatch):
        def hang(*a, **k):
            raise subprocess.TimeoutExpired("ffmpeg", 60)
        monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", hang)
        assert encoder.process_chunk(chunk, cfg) is True
        joined = " ".join(_Popen.launches[0])
        assert "lutrgb" not in joined
        assert "scale=out_color_matrix=bt709" in joined


class TestEncodeTimeout:
    def test_explicit_timeout_wins(self):
        assert encoder._encode_timeout({"timeout_s": 42}, use_hw=False, threads=1,
                                       preset="fast", clip_s=20) == 42.0

    def test_vaapi_keeps_five_times_the_clip(self):
        assert encoder._encode_timeout({}, use_hw=True, threads=1, preset="veryfast",
                                       clip_s=20) == 100.0

    def test_libx264_scales_with_threads_and_preset(self):
        t = lambda th, preset, clip=20: encoder._encode_timeout(  # noqa: E731
            {}, use_hw=False, threads=th, preset=preset, clip_s=clip)
        # gmnro16 (Bacau): ~87 s per 20 s chunk at 2 threads; the old 100 s killed it
        assert t(2, "veryfast") >= 2 * 87
        assert t(1, "veryfast") == pytest.approx(2 * t(2, "veryfast"))
        assert t(2, "fast") == pytest.approx(1.5 * t(2, "veryfast"))
        assert t(2, "veryfast", clip=40) == pytest.approx(2 * t(2, "veryfast"))
        assert t(16, "veryfast") == 100.0       # never under the old floor

    def test_process_chunk_passes_the_scaled_timeout(self, chunk, cfg):
        encoder.process_chunk(chunk, cfg)
        assert _Popen.timeouts[0] == encoder._encode_timeout(
            cfg, use_hw=False, threads=2, preset="veryfast", clip_s=20)

    def test_stitched_clips_get_twice_the_time(self, cfg, monkeypatch, chunk):
        mkv = chunk.with_name(f"{CAM}_{NIGHT}_210000_210020_color.mkv")
        mkv.write_bytes(b"s" * 1000)
        flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
        encoder.process_chunk(mkv, cfg)
        assert _Popen.timeouts[0] == encoder._encode_timeout(
            cfg, use_hw=False, threads=2, preset="veryfast", clip_s=40)


class TestStateRecordsTheEncode:
    """Every path that sets ``reencoded`` writes ``kept_source`` explicitly.

    Interface with the stacker: it treats a chunk as already encoded (no second
    colour curve, crop the OSD bar) only for reencoded + kept_source
    False, and falls back to its frame-height guess when the key is missing.
    A False on a kept or preserved capture would strip the colour from its
    stack (spectroscope stacks included).
    """

    def test_replaced_capture_is_kept_source_false(self, chunk, cfg):
        encoder.process_chunk(chunk, cfg)
        e = _entry(cfg, chunk)
        assert e["reencoded"] is True and e["kept_source"] is False
        assert e["enc"] == "CRF21"

    def test_size_rule_kept_capture_is_kept_source_true(self, chunk, cfg):
        _Popen.size = 5000                       # never smaller than the capture
        encoder.process_chunk(chunk, cfg)
        e = _entry(cfg, chunk)
        assert e["reencoded"] is True and e["kept_source"] is True
        assert "enc" not in e
        assert chunk.read_bytes() == b"s" * 1000

    def test_preserved_spectroscope_source_is_kept_source_true(self, chunk, cfg):
        cfg["stations"][CAM] = {"spectro": {"scale_x": 25.26}}
        assert encoder.process_chunk(chunk, cfg) is True
        e = _entry(cfg, chunk)
        assert e["reencoded"] is True and e["kept_source"] is True
        assert _Popen.launches == []
        assert chunk.read_bytes() == b"s" * 1000

    def test_healed_marker_is_kept_source_false(self, chunk, cfg):
        chunk.write_bytes(b"x" * 100 + encoder.ENCODED_TAG.encode() + b"=QP20")
        assert encoder.process_chunk(chunk, cfg) is True
        e = _entry(cfg, chunk)
        assert e["reencoded"] is True and e["kept_source"] is False
        assert _Popen.launches == []

    def test_re_marking_a_kept_capture_after_a_replacement_flips_it(self, chunk, cfg):
        encoder._mark_encoded(CAM, NIGHT, chunk.name, cfg, quant="QP20", kept_source=False)
        encoder._mark_encoded(CAM, NIGHT, chunk.name, cfg, quant="QP25", kept_source=True)
        e = _entry(cfg, chunk)
        assert e["kept_source"] is True and "enc" not in e

    def test_lock_state_survives_the_mark(self, chunk, cfg):
        flags_manager.lock_chunk(CAM, NIGHT, chunk.name, {"lock_type": "detection"}, cfg)
        encoder.process_chunk(chunk, cfg)
        e = _entry(cfg, chunk)
        assert e["lock"] == {"lock_type": "detection"} and e["ready"] is True


def test_every_vaapi_probe_encodes_at_the_capture_size(monkeypatch):
    seen: list[list[str]] = []

    class _R:
        returncode = 0

    monkeypatch.setattr(encoder.subprocess, "run", lambda cmd, **kw: seen.append(cmd) or _R())
    monkeypatch.setattr(rovimen_media, "_vaapi_ok", {})
    assert rovimen_media.check_vaapi("/dev/dri/renderD128")
    assert "size=1280x720" in rovimen_media.VAAPI_PROBE_SOURCE
    assert rovimen_media.VAAPI_PROBE_SOURCE in seen[0]
    # The encoder probes through rovimen_media, not a copy of its own.
    assert encoder._find_vaapi_device is rovimen_media.find_vaapi_device
    for script in ("ensure_vaapi.sh", "hardware_assessment.sh"):
        text = (REPO / "rovimen-scripts" / script).read_text()
        assert f"'{rovimen_media.VAAPI_PROBE_SOURCE}'" in text, script
        assert "size=32x32" not in text and "size=64x64" not in text


def test_overlay_details_render_times_out_instead_of_hanging(tmp_path, monkeypatch):
    import overlay
    clip = tmp_path / f"{CAM}_{NIGHT}_210000_color.mkv"
    clip.write_bytes(b"x")
    monkeypatch.setattr(overlay_details, "build_meteor_rows", lambda d: [("a", "b")])
    monkeypatch.setattr(overlay_details, "_probe_video_height", lambda c: 848)
    monkeypatch.setattr(overlay_details, "_colour_tag_args", lambda c: [])
    monkeypatch.setattr(overlay, "meteor_rows_extra_height", lambda *a, **k: 40)
    monkeypatch.setattr(overlay, "build_meteor_row_annotations",
                        lambda *a, **k: ["drawtext=text=X"])

    def hang(cmd, **kw):
        assert kw.get("timeout") == overlay_details.DETAILS_ENCODE_TIMEOUT_S
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    monkeypatch.setattr(overlay_details.subprocess, "run", hang)
    font = str(REPO / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf")
    out = overlay_details.render_details_clip(
        clip, [{"mag": -1.0}], {"overlay": {"enabled": True, "font": font, "style": "cinema"}})
    assert out is None
    assert list(tmp_path.glob("*.mkv")) == [clip]    # the scratch file is gone


def test_the_size_rule_runs_with_an_injected_runner(chunk, cfg):
    """The policy is testable without faking subprocess: a runner that writes
    2.25x then 0.8x the capture gets one coarser retry, 4 steps up."""
    job = encoder._resolve_job(chunk, cfg, CAM, NIGHT, "210000", NIGHT,
                               {"ready": True}, cfg["stations"][CAM], None)
    sizes = [2250, 800]
    tags: list[str] = []

    def run(cmd, src, dst, use_hw, timeout_s):
        tags.append(cmd[-2])                    # the re-encode tag carries the quantizer
        dst.write_bytes(b"e" * sizes.pop(0))
        return "ok", 0, b""

    result = encoder._encode_with_size_rule(job, run=run)
    assert tags == [f"{encoder.ENCODED_TAG}=CRF21", f"{encoder.ENCODED_TAG}=CRF25"]
    assert (result.src_bytes, result.dst_bytes, result.quant) == (1000, 800, "CRF25")
    assert _Popen.launches == []
