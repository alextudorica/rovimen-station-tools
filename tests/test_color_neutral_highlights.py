"""Clipped highlights go neutral instead of pink.

On the 2026-09-25 moonlit stacks the moon rendered as a pink/red ring (RO000T,
RO0016, MD0002, RO0012, RO001M) or a pink ring with a blue core (RO0014): the
curve's per-channel knee brought the clipped (red) channel back to 255 while the
others kept their white-balance gains. From the knee up, by the raw pixel's
brightest channel, the output now blends toward white; below it nothing changes.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rovimen-scripts"))

import color_calibration as cc  # noqa: E402
import encoder  # noqa: E402

# A red-heavy no-IR-cut sky corrected toward the fleet target: red gain lowest.
CURVE = cc.AdaptiveCurve(gains=(0.58, 0.82, 1.0), exponent=1.25, knee=242.25,
                         saturation=0.94, target=(44.0, 47.0, 53.0), boost=0.3,
                         sky_mean=(120.0, 100.0, 95.0), clipped_frac=0.004)


def _px(*rgb) -> np.ndarray:
    return np.array([[rgb]], dtype=np.uint8)


def test_a_pixel_whose_red_clips_comes_out_neutral_not_pink():
    out = cc.apply_adaptive_np(_px(255, 190, 170), CURVE)[0, 0].astype(int)
    assert out.max() - out.min() <= 3


def test_white_stays_white():
    assert cc.apply_adaptive_np(_px(255, 255, 255), CURVE)[0, 0].tolist() == [255, 255, 255]


def test_a_bright_unclipped_meteor_keeps_its_colour():
    out = cc.apply_adaptive_np(_px(90, 235, 110), CURVE)[0, 0].astype(int)
    assert out[1] > out[0] + 60 and out[1] > out[2] + 40


def test_a_clipped_moon_does_not_change_any_pixel_below_the_knee():
    rng = np.random.default_rng(0)
    sky = rng.integers(20, 200, size=(64, 64, 3), dtype=np.uint8)
    with_moon = sky.copy()
    with_moon[20:30, 20:30] = (255, 200, 180)
    a = cc.apply_adaptive_np(sky, CURVE)
    b = cc.apply_adaptive_np(with_moon, CURVE)
    mask = np.ones(sky.shape[:2], bool)
    mask[20:30, 20:30] = False
    assert np.array_equal(a[mask], b[mask])


def test_the_float_transform_matches_the_8bit_one():
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(40, 40, 3), dtype=np.uint8)
    a = cc.apply_adaptive_np(img, CURVE).astype(int)
    b = np.floor(cc.adaptive_transform_float(img, CURVE) + 0.5).astype(int)
    assert np.abs(a - b).max() <= 1


def test_the_cube_holds_the_transform_with_red_fastest(tmp_path):
    cube = cc.write_adaptive_cube(CURVE, tmp_path / "c.cube", size=5)
    lines = cube.read_text().splitlines()
    assert lines[0] == "LUT_3D_SIZE 5" and len(lines) == 1 + 5 ** 3
    # entry index r + 5*g + 25*b; take r=4 (255), g=2, b=1
    v = np.linspace(0.0, 255.0, 5)
    got = np.array([float(x) for x in lines[1 + 4 + 5 * 2 + 25 * 1].split()]) * 255.0
    want = cc.adaptive_transform_float(np.array([v[4], v[2], v[1]]), CURVE)
    assert got == pytest.approx(want, abs=0.01)


def test_the_video_filter_is_one_lut3d_without_commas(tmp_path):
    f = cc.build_adaptive_ffmpeg_filter(CURVE, tmp_path / "x.cube")
    assert f.startswith("lut3d=file=") and "," not in f
    assert cc.build_adaptive_ffmpeg_filter(CURVE).startswith("lutrgb=")


@pytest.mark.parametrize("locked,clipped,want", [
    (True, 0.004, True), (False, 0.004, False), (True, 0.0, False)])
def test_only_locked_clips_that_clip_pay_for_the_lut(locked, clipped, want):
    c = cc.AdaptiveCurve(**{**CURVE.__dict__, "clipped_frac": clipped})
    assert cc.wants_neutral_lut(c, locked) is want


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
def test_ffmpeg_lut3d_matches_the_stack_maths(tmp_path):
    h, w = 64, 96
    y, x = np.mgrid[0:h, 0:w]
    img = np.stack([(x * 255 // (w - 1)), (y * 255 // (h - 1)), ((x + y) * 255 // (w + h - 2))],
                   axis=-1).astype(np.uint8)
    img[20:40, 30:60] = (255, 200, 170)       # a clipped-red moon
    raw = tmp_path / "in.rgb"
    raw.write_bytes(img.tobytes())
    cube = cc.write_adaptive_cube(CURVE, tmp_path / "c.cube")
    r = subprocess.run(["ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
                        "-i", str(raw), "-vf", cc.build_adaptive_ffmpeg_filter(CURVE, cube),
                        "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True)
    got = np.frombuffer(r.stdout, np.uint8).reshape(h, w, 3).astype(int)
    want = cc.apply_adaptive_np(img, CURVE).astype(int)
    d = np.abs(got - want)
    assert d.mean() < 1.0 and d.max() <= 8


def test_process_chunk_deletes_the_clip_lut_whatever_happens(tmp_path, monkeypatch):
    cube = tmp_path / "rovimen_adaptive_x.cube"

    def fake(mkv, cfg, *, on_encoded=None, _cubes=None):
        cube.write_text("LUT_3D_SIZE 2\n")
        _cubes.append(cube)
        raise RuntimeError("encode blew up")

    monkeypatch.setattr(encoder, "_process_chunk", fake)
    with pytest.raises(RuntimeError):
        encoder.process_chunk(tmp_path / "c.mkv", {})
    assert not cube.exists()
