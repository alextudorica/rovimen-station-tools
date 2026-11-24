"""Calibrated + bar-overlaid timelapse for colour-only cameras.

The GH662 spectro pair's raw stacks are flat and magenta (IMX662, no IR-cut,
static exposure) and their timelapse carried no time/date bar. The processed
build applies the clip pipeline's white balance + gamma/contrast and appends
the standard astromania bottom bar with per-frame chunk timestamps.
"""

from __future__ import annotations

import shutil
import sys
from datetime import timezone
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

PROJECT = Path(__file__).resolve().parent.parent
SCRIPTS = PROJECT / "rovimen-scripts"
sys.path.insert(0, str(SCRIPTS))

import timelapse_build  # noqa: E402

FONT = SCRIPTS / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = SCRIPTS / "assets" / "astromania_text.png"

CAM = "GH662S"
NIGHT = "20260916"


def _overlay_cfg() -> dict:
    return {
        "font": str(FONT),
        "logo": str(LOGO),
        "network": "ROVIMEN",
        "coords": "44.6080N 25.2822E",
        "text_opacity": 0.6,
        "style": "cinema",
    }


def _write_stacks(stacks_dir: Path, n: int = 12) -> None:
    stacks_dir.mkdir(parents=True)
    rng = np.random.default_rng(42)
    for i in range(n):
        # Magenta-cast, low-contrast frames like the raw GH662 output.
        base = rng.integers(90, 120, size=(72, 128, 3), dtype=np.uint8)
        base[:, :, 0] = np.clip(base[:, :, 0].astype(int) + 40, 0, 255)
        base[:, :, 2] = np.clip(base[:, :, 2].astype(int) + 35, 0, 255)
        name = f"{CAM}_{NIGHT}_18{i:02d}00_stack.webp"
        Image.fromarray(base).save(stacks_dir / name, format="WEBP")


def test_parse_stack_time():
    t = timelapse_build._parse_stack_time(f"{CAM}_{NIGHT}_181430_stack.webp")
    assert t is not None
    assert (t.year, t.month, t.day, t.hour, t.minute, t.second) == (2026, 9, 16, 18, 14, 30)
    assert t.tzinfo == timezone.utc
    assert timelapse_build._parse_stack_time("garbage.webp") is None


def test_bar_appends_below_the_frame():
    bar = timelapse_build._TimelapseBar(_overlay_cfg(), CAM, {"az": 32.2, "alt": 69.5}, width=128)
    assert bar.enabled
    frame = np.zeros((72, 128, 3), dtype=np.uint8)
    out = bar.stamp(frame, timelapse_build._parse_stack_time(f"{CAM}_{NIGHT}_181430_stack.webp"))
    assert out.shape[1] == 128
    assert out.shape[0] == 72 + bar.height
    # The original frame pixels are untouched — nothing drawn over the sky.
    assert out[:72].max() == 0
    # The bar carries visible content (text/logo on black).
    assert out[72:].max() > 0


def test_missing_font_disables_the_bar_not_the_build():
    cfgo = _overlay_cfg()
    cfgo["font"] = "/nonexistent/font.ttf"
    bar = timelapse_build._TimelapseBar(cfgo, CAM, {}, width=128)
    assert not bar.enabled


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_colour_only_camera_gets_the_processed_build(tmp_path):
    stacks = tmp_path / "stacks"
    _write_stacks(stacks)
    out_dir = tmp_path / "out"
    station_cfg = {"rms": False, "az": 32.2, "alt": 69.5,
                   "color_post": {"gamma": 0.75, "contrast": 1.15}}
    cfg = {"overlay": _overlay_cfg()}

    out = timelapse_build.build_timelapse(
        NIGHT, CAM, stacks, out_dir, station_cfg=station_cfg, cfg=cfg,
    )
    assert out is not None and out.is_file() and out.stat().st_size > 0

    # The encoded video is taller than the source frames: the bar is appended,
    # never drawn over the sky (the dome projection crops it away by height).
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    assert int(probe.stdout.strip()) > 72


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_rms_camera_keeps_the_plain_fast_path(tmp_path):
    stacks = tmp_path / "stacks"
    _write_stacks(stacks, n=8)
    out_dir = tmp_path / "out"

    out = timelapse_build.build_timelapse(
        NIGHT, CAM, stacks, out_dir,
        station_cfg={"rms_data_path": "/x"}, cfg={"overlay": _overlay_cfg()},
    )
    assert out is not None and out.is_file()
    import subprocess
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    # No bar appended on the RMS path.
    assert int(probe.stdout.strip()) == 72


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_stacker_burned_bar_is_stripped_not_stacked(tmp_path):
    """Stacks taller than the sky height carry the stacker's own burned bar
    (720 sky + bar) — it must be stripped so the fresh bar isn't stacked
    under a stale one that reads ALT 0.0 AZ 0.0."""
    import subprocess

    from stacker import HEIGHT as SKY_H

    stacks = tmp_path / "stacks"
    stacks.mkdir()
    rng = np.random.default_rng(7)
    for i in range(3):
        frame = rng.integers(60, 90, size=(SKY_H + 128, 1280, 3), dtype=np.uint8)
        frame[SKY_H:] = 0  # the burned bar region
        Image.fromarray(frame).save(
            stacks / f"{CAM}_{NIGHT}_19{i:02d}00_stack.webp", format="WEBP")
    out = timelapse_build.build_timelapse(
        NIGHT, CAM, stacks, tmp_path / "out",
        station_cfg={"rms": False}, cfg={"overlay": _overlay_cfg()},
    )
    assert out is not None
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
         "-show_entries", "stream=height", "-of", "csv=p=0", str(out)],
        capture_output=True, text=True, check=True,
    )
    bar = timelapse_build._TimelapseBar(_overlay_cfg(), CAM, {}, width=1280)
    # Sky + ONE fresh bar (even-padded), not sky + burned bar + fresh bar.
    assert int(probe.stdout.strip()) <= SKY_H + bar.height + 1
