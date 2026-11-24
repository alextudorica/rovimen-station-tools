"""Characterization tests for encoder.py: pin what it runs before it is restructured.

* Golden snapshots of ``_build_cmd`` over every graph branch (VAAPI/libx264 x
  no overlay / drawtext / one logo / two logos x colour path x range), and
  of the full ffmpeg command ``process_chunk`` launches (with the thread and
  colour-tag splices). The overlay builders are replaced by deterministic fakes
  so these pin the encoder's own assembly, not overlay.py's layout.
* Real libx264 runs of every branch through ``process_chunk``: exit code, the
  OSD bar height, the BT.709 tags and the packet count (no frame lost or added).
* Stitched boundary clips never go to VAAPI.

Regenerate the golden file only for a deliberate command change:
    ENCODER_GOLDEN_UPDATE=1 uv run pytest tests/test_encoder_characterization.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import color_calibration as cc
import encoder
import rovimen_media
import flags_manager
import overlay

REPO = Path(__file__).resolve().parent.parent
GOLDEN = Path(__file__).resolve().parent / "fixtures" / "encoder_commands.json"
UPDATE = os.environ.get("ENCODER_GOLDEN_UPDATE") == "1"
HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None

CAM = "RO000H"
NIGHT = "20260322"
CURVE = cc.AdaptiveCurve(gains=(0.91, 1.0, 0.88), exponent=0.82, knee=242.25,
                         saturation=0.94, target=(44.0, 47.0, 53.0), boost=0.0,
                         sky_mean=(40.0, 42.0, 39.0))


# ---------------------------------------------------------------------------
# Golden file helpers
# ---------------------------------------------------------------------------

_golden_cache: dict | None = None
_golden_new: dict = {}


def _golden() -> dict:
    global _golden_cache
    if _golden_cache is None:
        _golden_cache = json.loads(GOLDEN.read_text()) if GOLDEN.exists() else {}
    return _golden_cache


def _check_golden(key: str, value: list[str]) -> None:
    if UPDATE:
        data = _golden()
        data[key] = value
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(json.dumps(data, indent=1, sort_keys=True) + "\n")
        return
    assert key in _golden(), f"no golden command for {key}; regenerate deliberately"
    assert value == _golden()[key]


def _norm(cmd: list[str] | None, *subs: tuple[str, str]) -> list[str]:
    assert cmd is not None
    out = []
    for arg in cmd:
        for old, new in subs:
            arg = arg.replace(old, new)
        out.append(arg)
    return out


# ---------------------------------------------------------------------------
# Deterministic overlay fakes
# ---------------------------------------------------------------------------

def _fake_annotations(overlay_cfg, station_id, station_cfg, chunk_epoch):
    if not overlay_cfg.get("fake_annot"):
        return [], 0
    return [f"drawtext=text={station_id}:x=10:y=h-30",
            f"drawtext=text=%{{pts\\:gmtime\\:{chunk_epoch}}}:x=w-200:y=h-30"], 40


def _fake_branding(overlay_cfg, in_label, out_label, input_index, bar_h=0):
    n = int(overlay_cfg.get("fake_logos", 0))
    if n == 0:
        return [], f"[{in_label}]null[{out_label}]"
    inputs: list[str] = []
    stages: list[str] = []
    cur, idx = in_label, input_index
    for i in range(n):
        inputs += ["-i", f"/logos/logo{i}.png"]
        after = f"stage{idx}"
        stages.append(f"[{idx}:v]scale=-1:{bar_h or 30},format=rgba,colorchannelmixer=aa=0.8[logo{idx}]")
        stages.append(f"[{cur}][logo{idx}]overlay=x=(W-w)/2:y=H-h-{4 + i}[{after}]")
        cur, idx = after, idx + 1
    return inputs, overlay.relabel_fragment_output(";".join(stages), cur, out_label)


@pytest.fixture
def fake_overlay(monkeypatch):
    monkeypatch.setattr(encoder, "build_drawtext_annotations", _fake_annotations)
    monkeypatch.setattr(encoder, "build_branding_overlay", _fake_branding)


BRANDING = {
    "none": None,
    "annot": {"enabled": True, "fake_annot": True, "fake_logos": 0},
    "logo1": {"enabled": True, "fake_annot": True, "fake_logos": 1},
    "logo2": {"enabled": True, "fake_annot": True, "fake_logos": 2},
}

COLOUR = {
    "legacy_identity": dict(gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0),
    "legacy_gains": dict(gain_r=0.9, gain_g=1.0, gain_b=0.8, gamma=0.75, rotate=True,
                         calib_contrast=1.1, calib_vibrance=0.2,
                         display_levels={"contrast": 1.1, "brightness": 0.02}),
    "adaptive": dict(gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0,
                     calib_filters=["lutrgb=r=val:g=val:b=val", "colorchannelmixer=rr=0.9"]),
    "adaptive_empty": dict(gain_r=1.0, gain_g=1.0, gain_b=1.0, gamma=1.0, calib_filters=[]),
}


def _build(*, vaapi: bool, branding: str, colour: str,
           full_range: bool, level: int = 2, preset: str = "veryfast",
           quant_offset: int = 0) -> list[str]:
    kw = dict(COLOUR[colour])
    rotate = kw.pop("rotate", False)
    cmd = encoder._build_cmd(
        Path("/cap/src_color.mkv"), Path("/cap/dst.tmp.mkv"), level,
        rotate=rotate, overlay_cfg=BRANDING[branding], station_id=CAM,
        station_cfg={}, chunk_epoch=1774213200,
        vaapi_device="/dev/dri/renderD128", use_vaapi=vaapi,
        full_range=full_range, preset=preset, quant_offset=quant_offset, **kw)
    return _norm(cmd)


BUILD_CASES = [
    (vaapi, branding, colour, full_range)
    for vaapi in (True, False)
    for branding in BRANDING
    for colour in COLOUR
    for full_range in (False, True)
]


class TestBuildCmdGolden:
    @pytest.mark.parametrize("vaapi,branding,colour,full_range", BUILD_CASES)
    def test_matrix(self, fake_overlay, vaapi, branding, colour, full_range):
        cmd = _build(vaapi=vaapi, branding=branding, colour=colour, full_range=full_range)
        key = (f"build|{'vaapi' if vaapi else 'x264'}|{branding}"
               f"|{colour}|{'full' if full_range else 'tv'}")
        _check_golden(key, cmd)

    @pytest.mark.parametrize("vaapi", [True, False])
    @pytest.mark.parametrize("level,preset,offset", [(1, "fast", 0), (4, "veryfast", 3)])
    def test_levels_presets_offsets(self, fake_overlay, vaapi, level, preset, offset):
        cmd = _build(vaapi=vaapi, branding="logo1", colour="adaptive",
                     full_range=False, level=level, preset=preset, quant_offset=offset)
        _check_golden(f"build|{'vaapi' if vaapi else 'x264'}|level{level}|{preset}|off{offset}", cmd)


# ---------------------------------------------------------------------------
# The full command process_chunk launches
# ---------------------------------------------------------------------------

class _CapturePopen:
    cmds: list[list[str]] = []
    envs: list[dict] = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        _CapturePopen.cmds.append(list(cmd))
        _CapturePopen.envs.append(kw.get("env") or {})

    def communicate(self, timeout=None):
        Path(self.cmd[-1]).write_bytes(b"e" * 10)
        return b"", b""

    def kill(self):
        pass


def _chunk_cfg(tmp_path: Path, **extra) -> dict:
    cfg = {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {CAM: {"rotate": False}},
        "services": {"reencode": {"enabled": True}},
        "overlay": {"enabled": True, "fake_annot": True, "fake_logos": 1},
        "_encode_threads": 2,
    }
    cfg.update(extra)
    return cfg


def _make_chunk(cfg: dict, name: str = f"{CAM}_{NIGHT}_210000_color.mkv",
                size: int = 1000) -> Path:
    night = Path(cfg["videocapture_path"]) / CAM / NIGHT
    night.mkdir(parents=True, exist_ok=True)
    mkv = night / name
    mkv.write_bytes(b"s" * size)
    flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
    return mkv


@pytest.fixture
def capture(monkeypatch, fake_overlay):
    monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
    monkeypatch.setattr(encoder.subprocess, "Popen", _CapturePopen)
    encoder._calibration_cache.clear()
    encoder._quant_floor.clear()
    _CapturePopen.cmds = []
    _CapturePopen.envs = []
    return _CapturePopen


def _colour(monkeypatch, mode: str) -> None:
    if mode == "legacy":
        monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: False)
        monkeypatch.setattr(cc, "resolve_color_post",
                            lambda *a, **k: {"contrast": 1.0, "vibrance": 0.0, "gamma": 1.0})
        monkeypatch.setattr(cc, "resolve_calibration_from_mkv",
                            lambda *a, **k: (0.9, 1.0, 0.85, 1.0))
    else:
        monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
        curve = CURVE if mode == "adaptive" else None
        monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: curve)


def _final_norm(cmd: list[str], tmp_path: Path) -> list[str]:
    # The scratch name is the encoder's own business (it becomes per-process);
    # pin that it is a sibling of the chunk ending in .tmp.mkv, then mask it.
    out = cmd[-1]
    assert out.endswith(".tmp.mkv")
    assert Path(out).parent == Path(cmd[cmd.index("-i") + 1]).parent
    return _norm(cmd[:-1], (str(tmp_path), "<DIR>")) + ["<TMP>"]


FINAL_CASES = [
    (hw, colour, full_range, locked)
    for hw in (True, False)
    for colour in ("adaptive", "captured", "legacy")
    for full_range in (False, True)
    for locked in (False, True)
]


class TestFinalCommand:
    @pytest.mark.parametrize("hw,colour,full_range,locked", FINAL_CASES)
    def test_matrix(self, tmp_path, monkeypatch, capture, hw, colour, full_range, locked):
        _colour(monkeypatch, colour)
        monkeypatch.setattr(encoder, "_find_vaapi_device",
                            lambda *a, **k: "/dev/dri/renderD128" if hw else None)
        cfg = _chunk_cfg(tmp_path, full_range_fix=full_range)
        mkv = _make_chunk(cfg)
        if locked:
            flags_manager.lock_chunk(CAM, NIGHT, mkv.name, {"lock_type": "detection"}, cfg)
        assert encoder.process_chunk(mkv, cfg) is True
        assert len(capture.cmds) == 1
        cmd = capture.cmds[0]
        key = (f"final|{'vaapi' if hw else 'x264'}|{colour}|{'full' if full_range else 'tv'}"
               f"|{'locked' if locked else 'unlocked'}")
        _check_golden(key, _final_norm(cmd, tmp_path))

    def test_splices_sit_between_the_codec_options_and_the_output(self, tmp_path, monkeypatch,
                                                                  capture):
        _colour(monkeypatch, "adaptive")
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
        cfg = _chunk_cfg(tmp_path, full_range_fix=True)
        encoder.process_chunk(_make_chunk(cfg), cfg)
        cmd = capture.cmds[0]
        an = cmd.index("-an")
        assert cmd[an + 1:an + 3] == ["-threads", "2"]
        assert cmd[an + 3:-3] == ["-colorspace", "bt709", "-color_primaries", "bt709",
                                  "-color_trc", "bt709", "-color_range", "pc"]
        assert cmd[-3:-1] == ["-metadata", f"{encoder.ENCODED_TAG}=CRF21"]

    def test_legacy_full_range_gets_only_the_range_tag(self, tmp_path, monkeypatch, capture):
        _colour(monkeypatch, "legacy")
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        cfg = _chunk_cfg(tmp_path, full_range_fix=True)
        encoder.process_chunk(_make_chunk(cfg), cfg)
        cmd = capture.cmds[0]
        assert cmd[-5:-3] == ["-color_range", "pc"]
        assert "-colorspace" not in cmd

    def test_ffmpeg_runs_in_utc_with_the_probed_libva_driver(self, tmp_path, monkeypatch,
                                                             capture):
        _colour(monkeypatch, "adaptive")
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        monkeypatch.setattr(rovimen_media, "_vaapi_driver_cache", "i965")
        cfg = _chunk_cfg(tmp_path)
        encoder.process_chunk(_make_chunk(cfg), cfg)
        env = capture.envs[0]
        assert env["TZ"] == "UTC"
        assert env["LIBVA_DRIVER_NAME"] == "i965"


class TestStitchedClipsAvoidVaapi:
    def test_stitched_clip_is_encoded_on_the_cpu(self, tmp_path, monkeypatch, capture):
        _colour(monkeypatch, "adaptive")
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        cfg = _chunk_cfg(tmp_path)
        mkv = _make_chunk(cfg, name=f"{CAM}_{NIGHT}_210000_210020_color.mkv")
        assert encoder.process_chunk(mkv, cfg) is True
        cmd = capture.cmds[0]
        assert "-vaapi_device" not in cmd
        assert "libx264" in cmd
        assert not any("hwupload" in a for a in cmd)

    def test_plain_chunk_with_the_same_device_uses_vaapi(self, tmp_path, monkeypatch, capture):
        _colour(monkeypatch, "adaptive")
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: "/dev/dri/renderD128")
        cfg = _chunk_cfg(tmp_path)
        encoder.process_chunk(_make_chunk(cfg), cfg)
        assert "h264_vaapi" in capture.cmds[0]


# ---------------------------------------------------------------------------
# Real libx264 runs of every graph branch
# ---------------------------------------------------------------------------

W, H, SECONDS, FPS = 320, 180, 2, 25


def _make_source(path: Path) -> None:
    """A noisy, full-range, BT.709-tagged night-sky clip with a 12-frame GOP."""
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
         "-i", f"color=c=0x54435a:s={W}x{H}:r={FPS}:d={SECONDS},noise=alls=24:allf=t",
         "-vf", "scale=out_color_matrix=bt709:out_range=pc,format=yuvj420p",
         "-c:v", "libx264", "-preset", "ultrafast", "-crf", "8", "-g", "12",
         "-colorspace", "bt709", "-color_range", "pc", str(path)],
        check=True)


def _probe(path: Path) -> dict:
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries",
         "stream=width,height,nb_read_packets,color_space,color_primaries,color_transfer,color_range",
         "-of", "json", str(path)],
        capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["streams"][0]


def _real_overlay(logo: bool = False, gmn: bool = False) -> dict:
    cfg = {
        "enabled": True, "style": "cinema",
        "font": str(REPO / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"),
        "font_size": 12, "network": "ROVIMEN", "coords": "45.30N  26.11E",
        "text_opacity": 0.8, "show_logo": logo,
    }
    if logo:
        cfg["logo"] = str(REPO / "rovimen-scripts" / "assets" / "astromania_text.png")
    if gmn:
        cfg["gmn_logo"] = str(REPO / "rovimen-scripts" / "assets" / "gmn_sphere.png")
    return cfg


REAL_BRANCHES = {
    "plain": dict(overlay={"enabled": False}),
    "drawtext": dict(overlay=_real_overlay()),
    "logo": dict(overlay=_real_overlay(logo=True)),
    "logo_gmn": dict(overlay=_real_overlay(logo=True, gmn=True)),
    "rotate": dict(overlay=_real_overlay(), rotate=True),
    "full_range": dict(overlay=_real_overlay(logo=True), full_range=True),
    "legacy_colour": dict(overlay=_real_overlay(), legacy=True),
    "colour_as_captured": dict(overlay=_real_overlay(), captured=True),
}


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg and ffprobe")
class TestRealLibx264Branches:
    @pytest.mark.parametrize("branch", sorted(REAL_BRANCHES))
    def test_branch_encodes(self, tmp_path, monkeypatch, branch):
        spec = REAL_BRANCHES[branch]
        encoder._calibration_cache.clear()
        encoder._quant_floor.clear()
        cfg = {
            "videocapture_path": str(tmp_path / "color_capture"),
            "segment_duration": SECONDS, "compression_level": 2,
            "stations": {CAM: {"rotate": bool(spec.get("rotate"))}},
            "services": {"reencode": {"enabled": True}},
            "overlay": spec["overlay"],
            "capabilities": {"hwencode": False},
            "full_range_fix": bool(spec.get("full_range")),
            "timeout_s": 120,
        }
        night = Path(cfg["videocapture_path"]) / CAM / NIGHT
        night.mkdir(parents=True)
        mkv = night / f"{CAM}_{NIGHT}_210000_color.mkv"
        _make_source(mkv)
        src = _probe(mkv)
        flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
        if spec.get("legacy"):
            monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: False)
        if spec.get("captured"):
            monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)

        assert encoder.process_chunk(mkv, cfg) is True
        state = flags_manager.load(CAM, NIGHT, cfg)["chunks"][mkv.name]
        assert state["reencoded"] is True

        out = _probe(mkv)
        bar_h = 0
        if spec["overlay"].get("enabled"):
            _, bar_h = overlay.build_drawtext_annotations(
                spec["overlay"], CAM, cfg["stations"][CAM], 1774213200)
            assert bar_h > 0
        assert out["width"] == W
        assert out["height"] == H + bar_h, "the capture was kept instead of re-encoded"
        assert int(out["nb_read_packets"]) == int(src["nb_read_packets"]) == SECONDS * FPS
        assert encoder._has_encoded_marker(mkv)
        if not spec.get("legacy"):
            assert out["color_space"] == "bt709"
            assert out["color_primaries"] == "bt709"
            assert out["color_transfer"] == "bt709"
            assert out["color_range"] == ("pc" if spec.get("full_range") else "tv")
        assert not list(night.glob("*.tmp.mkv"))
