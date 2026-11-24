"""The night stack: a per-pixel max of the night's chunk-stack thumbnails.

Each thumbnail is the published stack scaled to 480 px, bottom bar and its
chunk clock included, so the max over ~1,000 of them printed every clock of
the night on top of each other, and the bar's black rows went into
``rebalance_composite``'s sky sample. The sky is now cropped from each
thumbnail before the max, and the night stack gets one static bar (no clock).
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

import timelapse_build

SCRIPTS = Path(__file__).resolve().parent.parent / "rovimen-scripts"
FONT = SCRIPTS / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = SCRIPTS / "assets" / "astromania_text.png"

CAM = "RO001R"
NIGHT = "20261110"
THUMB_W = 480
SKY_ROWS = 270   # 720 sky rows of a 1280-wide stack, scaled to 480
BAR_ROWS = 48    # a 128-row stack bar, scaled to 480
SKY = 40


def _overlay_cfg(**extra) -> dict:
    return {"font": str(FONT), "logo": str(LOGO), "network": "ROVIMEN",
            "coords": "45.18N  24.24E", "text_opacity": 0.6, "style": "cinema",
            **extra}


def _write_thumbs(thumbs: Path, clock_x: int = 0) -> None:
    """Three thumbnails in the night window, each with its own clock in the bar."""
    thumbs.mkdir(parents=True)
    for i, hhmmss in enumerate(("210000", "220000", "230000")):
        img = np.zeros((SKY_ROWS + BAR_ROWS, THUMB_W, 3), dtype=np.uint8)
        img[:SKY_ROWS] = SKY
        x = clock_x + 60 * i  # the clock's digits move from chunk to chunk
        img[SKY_ROWS + 10:SKY_ROWS + 30, x:x + 40] = 255
        Image.fromarray(img).save(
            thumbs / f"{CAM}_{NIGHT}_{hhmmss}_stack.webp", lossless=True)


def _night_stack(out_dir: Path) -> np.ndarray:
    return np.asarray(Image.open(
        out_dir / f"{CAM}_{NIGHT}_night_stack.webp").convert("RGB"))


def test_the_max_and_the_rebalance_see_only_sky(tmp_path, monkeypatch):
    _write_thumbs(tmp_path / "thumbs")
    seen: list[np.ndarray] = []
    monkeypatch.setattr(timelapse_build.cc, "rebalance_composite",
                        lambda acc, cfg: seen.append(acc) or acc)
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    timelapse_build.build_night_stack(NIGHT, CAM, tmp_path / "thumbs", out_dir)
    assert seen[0].shape == (SKY_ROWS, THUMB_W, 3)
    assert int(seen[0].max()) == SKY
    # No overlay config: the sky alone is published.
    assert _night_stack(out_dir).shape == (SKY_ROWS, THUMB_W, 3)


def test_the_night_stack_carries_one_static_bar(tmp_path, monkeypatch):
    monkeypatch.setattr(timelapse_build.cc, "rebalance_composite", lambda acc, cfg: acc)
    stacks = []
    for night_clock_x in (0, 200):
        _write_thumbs(tmp_path / f"thumbs{night_clock_x}", clock_x=night_clock_x)
        out_dir = tmp_path / f"out{night_clock_x}"
        out_dir.mkdir()
        timelapse_build.build_night_stack(
            NIGHT, CAM, tmp_path / f"thumbs{night_clock_x}", out_dir,
            overlay_cfg=_overlay_cfg())
        stacks.append(_night_stack(out_dir))
    bar = timelapse_build._TimelapseBar(_overlay_cfg(), CAM, {}, width=1280)
    bar_rows = round(bar.height * THUMB_W / 1280)
    for stack in stacks:
        assert stack.shape == (SKY_ROWS + bar_rows, THUMB_W, 3)
        assert int(stack[:SKY_ROWS].max()) == SKY
        assert stack[SKY_ROWS:].max() > 0  # the bar carries its text and logos
    # The bar does not depend on the chunks' clocks.
    assert np.array_equal(stacks[0][SKY_ROWS:], stacks[1][SKY_ROWS:])


def test_no_bar_when_the_overlay_is_off(tmp_path, monkeypatch):
    monkeypatch.setattr(timelapse_build.cc, "rebalance_composite", lambda acc, cfg: acc)
    _write_thumbs(tmp_path / "thumbs")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    timelapse_build.build_night_stack(NIGHT, CAM, tmp_path / "thumbs", out_dir,
                                      overlay_cfg=_overlay_cfg(enabled=False))
    assert _night_stack(out_dir).shape == (SKY_ROWS, THUMB_W, 3)


def test_build_hands_the_overlay_config_to_the_night_stack(tmp_path, monkeypatch):
    night_dir = tmp_path / CAM / NIGHT
    stacks = night_dir / "stacks"
    _write_thumbs(stacks / "thumbs")
    Image.new("RGB", (8, 8)).save(stacks / f"{CAM}_{NIGHT}_210000_stack.webp")
    monkeypatch.setattr(timelapse_build, "build_timelapse", lambda *a, **k: None)
    monkeypatch.setattr(timelapse_build.cc, "rebalance_composite", lambda acc, cfg: acc)
    cfg = {"videocapture_path": str(tmp_path), "overlay": _overlay_cfg(),
           "stations": {CAM: {}}}
    timelapse_build.build(CAM, NIGHT, cfg)
    stack = np.asarray(Image.open(
        night_dir / f"{CAM}_{NIGHT}_night_stack.webp").convert("RGB"))
    bar = timelapse_build._TimelapseBar(_overlay_cfg(), CAM, {}, width=1280)
    assert stack.shape[0] == SKY_ROWS + round(bar.height * THUMB_W / 1280)
    # The first chunk's clock block is not in the bar.
    assert not (stack[SKY_ROWS + 10:SKY_ROWS + 30, 0:40] == 255).all()
