#!/usr/bin/env python3
"""Learn a camera's adaptive-colour target from a clip that looks right.

Measures the sky of a clip (or a stack image) the way the sky match does —
mean of the pixels whose luma sits between the 10th and 85th percentile — and
prints the ``color_adaptive`` override that makes that camera's future clips
and stacks land on the same sky. If the clip was shot under the moon or in
twilight, the brightness lift for that moment is divided out, so the learned
``sky_luma`` is the dark-sky base the lift is applied to.

    uv run python tools/learn_colour_reference.py RO001L_20260924_025155_color.mkv \\
        --camera RO001L --lat 44.607966 --lon 25.282230

Apply the printed payload on the station with config_patch.py (it deep-merges
under the config lock), then mirror it into ``station_configs/<host>/config.json``:

    echo '<payload>' | ssh <station> python3 ~/rovimen_scripts/config_patch.py -

Colour profiles are per camera: learn from that camera's own clip.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rovimen-scripts"))
import color_calibration as cc  # noqa: E402

logger = logging.getLogger(__name__)

_NAME_TIME_RE = re.compile(r"_(\d{8})_(\d{6})")


def _clip_frames(path: Path, every_s: float = 1.0) -> np.ndarray:
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,color_space", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    )
    st = json.loads(probe.stdout)["streams"][0]
    w, h = int(st["width"]), int(st["height"])
    if st.get("color_space") in (None, "unknown"):
        logger.warning("%s carries no colour-matrix tag; its colours depend on the "
                       "decoder, so treat the learned values as approximate", path.name)
    raw = subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-vf", f"fps=1/{every_s},format=rgb24", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    ).stdout
    n = len(raw) // (w * h * 3)
    frames = np.frombuffer(raw[: n * w * h * 3], dtype=np.uint8).reshape(n, h, w, 3)
    # Clips carry the overlay bar below the 720-row sky; keep the sky only.
    return frames[:, :720] if h > 720 else frames


def _image_frames(path: Path) -> np.ndarray:
    img = np.asarray(Image.open(path).convert("RGB"))
    return (img[:720] if img.shape[0] > 720 else img)[None]


def learn(path: Path, location: tuple[float, float] | None) -> dict:
    frames = (_image_frames(path) if path.suffix.lower() in (".webp", ".png", ".jpg", ".jpeg")
              else _clip_frames(path))
    px = cc.sky_pixels(frames)
    r, g, b = (float(px[:, i].mean()) for i in range(3))
    luma = 0.2126 * r + 0.7152 * g + 0.0722 * b
    boost = 0.0
    m = _NAME_TIME_RE.search(path.name)
    if m and location is not None:
        when = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc) + timedelta(seconds=10)
        boost = cc.sky_brightness_boost(cc.resolve_adaptive({}), location, when)
    return {
        "sky_luma": round(luma / (1.0 + boost), 1),
        "r_g": round(r / g, 3),
        "b_g": round(b / g, 3),
        "_measured": {"sky_rgb": [round(r, 1), round(g, 1), round(b, 1)],
                      "luma": round(luma, 1), "brightness_lift": round(boost, 3)},
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("clip", type=Path, help="clip (.mkv/.mp4) or stack image (.webp/.png)")
    ap.add_argument("--camera", help="camera code, to print the PATCH payload")
    ap.add_argument("--lat", type=float, help="station latitude (divides out the moon/twilight lift)")
    ap.add_argument("--lon", type=float, help="station longitude, east positive")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    location = (args.lat, args.lon) if args.lat is not None and args.lon is not None else None
    learned = learn(args.clip, location)
    measured = learned.pop("_measured")
    print(json.dumps({"measured": measured, "color_adaptive": learned}, indent=2))
    if args.camera:
        print(json.dumps({"stations": {args.camera: {"color_adaptive": learned}}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
