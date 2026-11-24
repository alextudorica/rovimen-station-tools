"""Where a meteor starts in its colour clip, measured from the video itself.

A colour chunk is named after the wall-clock second ffmpeg opened it
(``<CAM>_<YYYYMMDD>_<HHMMSS>_color.mkv``), but its first frame was captured
earlier by the camera/relay latency, segments start on keyframes, and the name
has whole-second precision. So "meteor time minus clip-name time" puts the
meteor 0.5-1.5 s off, differently per camera and per clip: measured
2026-10-21, RO001R (Ferigile) appeared a median +0.94 s after that estimate
(-0.03..+1.60 s, 15 meteors), RO000T (Vâlcele) -0.24 s (-0.75..+0.47 s, 8).

``measure_meteor_start`` decodes only a window around the estimate and only a
small crop around the meteor's first RMS position (FTPdetectinfo x, y; the
colour clip comes from the same camera, so pixel coordinates agree), subtracts
the per-pixel median of that window and returns the time of the first frame
whose brightening crosses 30 % of the window's peak. The result is the
offset the players should seek to.

Python 3.8 safe (station scripts, see the development guide); numpy only, ffmpeg via a
rawvideo pipe like the stacker.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import rovimen_media

logger = logging.getLogger(__name__)

RMS_WIDTH = 1280            # FTPdetectinfo pixel frame of every fleet camera
RMS_HEIGHT = 720
CROP = 120                  # px square around the meteor's first position
WINDOW_S = 5.0              # search this far either side of the estimate
MIN_RISE = 25.0             # peak brightening (8-bit) that counts as visible
TOP_PIXELS = 20             # brightest pixels averaged per frame
RISE_FRACTION = 0.3         # of the peak brightening: the meteor's first frame


def _probe(clip: Path) -> Optional[tuple]:
    """(width, height, fps) of the clip's video stream, or None.

    A frame rate ffprobe cannot state (``0/0``, ``0/1``) reads as 25 fps.
    """
    stream = rovimen_media.first_stream(rovimen_media.ffprobe_json(
        clip, timeout=30, entries='stream=width,height,r_frame_rate'))
    try:
        w, h = int(stream['width']), int(stream['height'])
        num, _, den = str(stream['r_frame_rate']).partition('/')
        den_f = float(den or 1)
        fps = float(num) / den_f if den_f else 0.0
    except (KeyError, TypeError, ValueError):
        return None
    return w, h, fps if fps > 0 else 25.0


def measure_meteor_start(clip: Path, x: float, y: float, expected_s: float,
                         window_s: float = WINDOW_S,
                         rotate: bool = False) -> Optional[float]:
    """Seconds from the clip's first frame to the meteor's first frame, or
    None if the meteor is not visible near *expected_s* (faint in colour,
    clouds, a clip that does not contain it).

    *x*, *y* are the meteor's first position in RMS pixels (1280 wide);
    *expected_s* is the clip-name estimate (meteor time minus the time in the
    chunk's filename). *rotate*: the camera's ``rotate`` setting — the
    encoder turns those clips 180° (vflip+hflip, above the overlay strip), RMS
    sees them unturned.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    if shutil.which('ffmpeg') is None or not Path(clip).is_file():
        return None
    probe = _probe(Path(clip))
    if probe is None:
        return None
    width, _height, fps = probe
    scale = width / float(RMS_WIDTH)
    cx, cy = x * scale, y * scale
    if rotate:
        cx, cy = width - cx, RMS_HEIGHT * scale - cy
    crop = max(16, int(CROP * scale)) // 2 * 2
    x0 = int(max(0, min(width - crop, cx - crop / 2)))
    y0 = int(max(0, cy - crop / 2))
    start = max(0.0, expected_s - window_s)
    try:
        nice = ['nice', '-n', '19'] if shutil.which('nice') else []
        raw = subprocess.run(
            nice + ['ffmpeg', '-v', 'error', '-ss', f'{start:.3f}', '-i', str(clip),
             '-t', f'{2 * window_s:.3f}',
             '-vf', f'crop={crop}:{crop}:{x0}:{y0},format=gray',
             '-f', 'rawvideo', '-pix_fmt', 'gray', 'pipe:1'],
            capture_output=True, timeout=120, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug('clip timing: decode of %s failed: %s', clip, exc)
        return None
    n = len(raw) // (crop * crop)
    if n < 10:
        return None
    frames = np.frombuffer(raw[:n * crop * crop], dtype=np.uint8).reshape(n, crop * crop)
    frames = frames.astype(np.float32)
    rise = np.maximum(frames - np.median(frames, axis=0), 0)
    rise = np.sort(rise, axis=1)[:, -TOP_PIXELS:].mean(axis=1)
    base = float(np.median(rise))
    peak = float(rise.max())
    if peak - base < MIN_RISE:
        return None
    first = int(np.argmax(rise > base + RISE_FRACTION * (peak - base)))
    return round(start + first / fps, 3)
