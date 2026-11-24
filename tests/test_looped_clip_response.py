"""The three loop-download routes share one concat step.

``/loop_clip``, ``/cropped_loop_clip`` and ``/loop_clip_archive`` each cut a
segment their own way and hand it to ``routes.archive.looped_clip_response``,
which plays it N times in one stream-copied MP4 attachment.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from routes.archive import looped_clip_response

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
                       reason="needs ffmpeg"),
]


def _segment(tmp_path: Path) -> Path:
    seg = tmp_path / "segment.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "testsrc=size=64x36:rate=25:duration=1",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(seg)],
        check=True, capture_output=True, timeout=60,
    )
    return seg


def _duration_s(data: bytes, tmp_path: Path) -> float:
    out = tmp_path / "got.mp4"
    out.write_bytes(data)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "json", str(out)],
        check=True, capture_output=True, timeout=60,
    )
    return float(json.loads(probe.stdout)["format"]["duration"])


def test_plays_the_segment_n_times_as_an_mp4_attachment(tmp_path: Path) -> None:
    seg = _segment(tmp_path)
    resp = looped_clip_response(str(seg), 3, "RO000A_20260101_000000_loop3x.mp4")
    data = resp.get_data()
    assert resp.status_code == 200
    assert resp.headers["Content-Type"] == "video/mp4"
    assert resp.headers["Content-Disposition"] == (
        'attachment; filename="RO000A_20260101_000000_loop3x.mp4"')
    assert resp.headers["Content-Length"] == str(len(data))
    assert _duration_s(data, tmp_path) == pytest.approx(3.0, abs=0.2)


def test_bad_segment_raises_for_the_route_to_map(tmp_path: Path) -> None:
    seg = tmp_path / "segment.mp4"
    seg.write_bytes(b"not a video")
    with pytest.raises(subprocess.CalledProcessError):
        looped_clip_response(str(seg), 2, "x_loop2x.mp4")
