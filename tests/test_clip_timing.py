"""clip_timing.measure_meteor_start finds a meteor's first frame in a clip."""

from __future__ import annotations

import shutil
import subprocess

import numpy as np
import pytest

import clip_timing

# Real ffmpeg encodes/decodes: 9-17 s each locally, 4x that on a slow CI runner,
# so the suite-wide 60 s timeout (pyproject) would flake on them.
pytestmark = [
    pytest.mark.skipif(shutil.which('ffmpeg') is None, reason='needs ffmpeg'),
    pytest.mark.timeout(300),
]

FPS = 25


def _clip(tmp_path, appear_s: float, x: int, y: int, frames: int = 250, rows: int = 720):
    """A noisy dark 1280-wide clip where a streak appears at *appear_s* near
    (x, y) for 6 frames; *rows* > 720 adds an overlay strip below, like the
    re-encoded detection clips."""
    rng = np.random.default_rng(1)
    path = tmp_path / 'RO000X_20261020_220000_color.mkv'
    proc = subprocess.Popen(
        ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'gray',
         '-s', f'1280x{rows}', '-r', str(FPS), '-i', 'pipe:0',
         '-c:v', 'libx264', '-qp', '10', '-g', '50', '-pix_fmt', 'yuv420p', str(path)],
        stdin=subprocess.PIPE)
    first = round(appear_s * FPS)
    for i in range(frames):
        f = (30 + rng.normal(0, 3, (rows, 1280))).clip(0, 255).astype(np.uint8)
        if first <= i < first + 6:
            k = i - first
            f[y - 3 + k * 4:y + 3 + k * 4, x - 8 + k * 6:x + 8 + k * 6] = 230
        proc.stdin.write(f.tobytes())
    proc.stdin.close()
    assert proc.wait() == 0
    return path


def test_finds_the_first_frame_of_the_meteor(tmp_path):
    clip = _clip(tmp_path, 4.12, 1011, 548)
    got = clip_timing.measure_meteor_start(clip, 1011, 548, expected_s=3.68)
    assert got == pytest.approx(4.12, abs=0.05)


def test_overlay_strip_does_not_shift_coordinates(tmp_path):
    clip = _clip(tmp_path, 2.0, 400, 300, rows=848)
    assert clip_timing.measure_meteor_start(clip, 400, 300, expected_s=2.6) == pytest.approx(2.0, abs=0.05)


def test_rotated_camera_flips_rms_coordinates(tmp_path):
    # rotate: true clips are turned 180° above the overlay strip; RMS reports the unturned frame.
    clip = _clip(tmp_path, 2.0, 1280 - 400, 720 - 300, rows=848)
    assert clip_timing.measure_meteor_start(clip, 400, 300, expected_s=2.6, rotate=True) == pytest.approx(2.0, abs=0.05)


def test_none_where_no_meteor_is_visible(tmp_path):
    clip = _clip(tmp_path, 4.12, 1011, 548)
    assert clip_timing.measure_meteor_start(clip, 200, 200, expected_s=3.68) is None


def test_none_for_a_missing_clip(tmp_path):
    assert clip_timing.measure_meteor_start(tmp_path / 'nope.mkv', 1, 1, 2.0) is None
