"""Every ffmpeg call on the dawn and stacker path carries a timeout.

``rovimen_media`` states the rule: a hung USB capture disk (the UAS hang on
gmnro03 (Ciocârlia)) must stall one call, not a whole dawn run. The timelapse
encodes, the stack overlay render and the spectro frame extraction ran without
one. A call that times out is a failed product: its partial output is removed
and the caller carries on as for any other ffmpeg failure.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import overlay
import spectro_pipeline as sp
import timelapse_build

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
FONT = SCRIPTS / "fonts" / "VCR_OSD_MONO_1.001.ttf"
CAM = "RO000E"
NIGHT = "20260818"


def _stacks(tmp_path: Path) -> Path:
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    for hhmmss in ("210000", "210020"):
        Image.fromarray(np.full((48, 64, 3), 40, dtype=np.uint8)).save(
            stacks / f"{CAM}_{NIGHT}_{hhmmss}_stack.webp", format="WEBP", lossless=True)
    return stacks


def _hung_encode(seen: dict):
    """subprocess.call as it behaves when its timeout expires: the child has
    written part of its output, is killed, and TimeoutExpired is raised."""
    def call(cmd, *a, **kw):
        seen.update(kw)
        Path(cmd[-1]).write_bytes(b"partial")
        if kw.get("timeout") is None:
            return 0  # the old code: no timeout, the hang would never end
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])
    return call


class TestTimelapseEncodes:
    def test_a_hung_timelapse_encode_fails_and_leaves_nothing(self, tmp_path, monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(timelapse_build.subprocess, "call", _hung_encode(seen))
        out = tmp_path / "out"
        assert timelapse_build.build_timelapse(NIGHT, CAM, _stacks(tmp_path), out) is None
        assert seen["timeout"] == timelapse_build._ENCODE_TIMEOUT_S == 1800
        assert list(out.iterdir()) == []

    def test_a_hung_processed_timelapse_encode_fails_and_leaves_nothing(self, tmp_path,
                                                                        monkeypatch):
        seen: dict = {}
        monkeypatch.setattr(timelapse_build.subprocess, "call", _hung_encode(seen))
        out = tmp_path / "out"
        result = timelapse_build.build_timelapse(
            NIGHT, CAM, _stacks(tmp_path), out, station_cfg={"rms": False}, cfg={})
        assert result is None
        assert seen["timeout"] == 1800
        assert list(out.iterdir()) == []


class TestStackOverlayRender:
    def test_a_hung_render_raises_like_a_failed_one(self, monkeypatch):
        seen: dict = {}

        def run(cmd, **kw):
            seen.update(kw)
            if kw.get("timeout") is None:
                return subprocess.CompletedProcess(cmd, 0, stdout=b"")
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(overlay.subprocess, "run", run)
        ocfg = {"enabled": True, "style": "cinema", "font": str(FONT), "show_timestamp": True}
        still = Image.fromarray(np.zeros((72, 128, 3), dtype=np.uint8), "RGB")
        # stacker._publish_stack catches the RuntimeError and writes the plain stack.
        with pytest.raises(RuntimeError, match="timed out"):
            overlay.render_still(still, ocfg, CAM, {"az": 170.0, "alt": 40.0}, 1_800_000_000)
        assert seen["timeout"] == 120


class TestSpectroFrameExtraction:
    def test_a_hung_extraction_removes_its_partial_frames(self, tmp_path, monkeypatch):
        mkv = tmp_path / f"{CAM}_{NIGHT}_210000_color.mkv"
        out = tmp_path / "frames"
        out.mkdir()
        other = out / "other_chunk_001.png"
        other.write_bytes(b"png")
        seen: dict = {}

        def run(cmd, **kw):
            seen.update(kw)
            for i in (1, 2):
                (out / f"{mkv.stem}_{i:03d}.png").write_bytes(b"png")
            if kw.get("timeout") is None:
                return subprocess.CompletedProcess(cmd, 0)
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])

        monkeypatch.setattr(sp.subprocess, "run", run)
        with pytest.raises(subprocess.TimeoutExpired):
            sp.extract_frames(mkv, out)
        assert seen["timeout"] == sp.EXTRACT_TIMEOUT_S
        assert sorted(p.name for p in out.iterdir()) == [other.name]
