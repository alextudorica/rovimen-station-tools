"""The spectro reducer decodes a window into one preallocated array.

``decode_window`` read ffmpeg's whole output with ``capture_output=True`` and
then wrapped it in an array: every frame was held twice (~2 GB peak for a
15 s window) on gmnro01 (Ghirdoveni), whose swap is full. It now reads the
frames straight into an array sized from the window's packet count.
"""

from __future__ import annotations

import shutil
import subprocess
import tracemalloc
from pathlib import Path

import numpy as np
import pytest

import spectro_pipeline as sp

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="needs ffmpeg and ffprobe")

pytestmark = [needs_ffmpeg, pytest.mark.timeout(300)]

W, H = 64, 36


@pytest.fixture
def small_frames(monkeypatch):
    monkeypatch.setattr(sp, "FRAME_W", W)
    monkeypatch.setattr(sp, "FRAME_H", H)


def _clip(path: Path, frames: int = 40) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi",
         "-i", f"testsrc2=size=96x54:rate=25:duration={frames / 25}",
         "-c:v", "ffv1", str(path)],
        check=True, timeout=120)
    return path


def _legacy_decode_window(mkv: Path) -> np.ndarray:
    """decode_window as it was: the whole output in memory, then an array."""
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(mkv), "-vsync", "passthrough",
         "-vf", f"scale={W}:{H}", "-f", "rawvideo", "-pix_fmt", "rgb24",
         "pipe:1"],
        capture_output=True, check=True, timeout=600)
    size = W * H * 3
    n = len(r.stdout) // size
    return np.frombuffer(r.stdout[:n * size], dtype=np.uint8).reshape(n, H, W, 3)


def test_the_frames_match_the_old_decode(small_frames, tmp_path):
    mkv = _clip(tmp_path / "window.mkv")
    got = sp.decode_window(mkv)
    want = _legacy_decode_window(mkv)
    assert got.shape == want.shape == (40, H, W, 3)
    assert got.dtype == np.uint8
    assert np.array_equal(got, want)


def test_the_frames_are_held_once(small_frames, tmp_path):
    mkv = _clip(tmp_path / "window.mkv", frames=60)
    decoded = 60 * W * H * 3
    tracemalloc.start()
    try:
        frames = sp.decode_window(mkv)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert len(frames) == 60
    assert peak < 1.5 * decoded, f"peak {peak} B for {decoded} B of frames"


def test_a_miscounted_window_still_yields_every_frame(small_frames, tmp_path, monkeypatch):
    mkv = _clip(tmp_path / "window.mkv")
    monkeypatch.setattr(sp, "_window_frame_count", lambda mkv: 7)
    assert np.array_equal(sp.decode_window(mkv), _legacy_decode_window(mkv))


def test_a_failed_decode_raises(small_frames, tmp_path):
    bad = tmp_path / "window.mkv"
    bad.write_bytes(b"not a video")
    with pytest.raises(subprocess.CalledProcessError):
        sp.decode_window(bad)
