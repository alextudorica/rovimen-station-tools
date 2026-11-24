"""Convert an analysis figure into a fireball report figure.

Writes ``dashboard/fireball_reports_data/<event_id>/<name>.webp``: the
source image scaled down to at most ``--max-width`` pixels wide and saved as
WebP, which keeps a report's figures to a few hundred KB each in the repo.

    uv run tools/fireball_report_figure.py map.png 20261009_204945 map
"""

from __future__ import annotations

import argparse
import logging
import re
from pathlib import Path

from PIL import Image

logger = logging.getLogger(__name__)

REPORTS_DIR = Path(__file__).resolve().parent.parent / "dashboard" / "fireball_reports_data"


def convert(src: Path, event_id: str, name: str, max_width: int = 1800, quality: int = 85) -> Path:
    if not re.fullmatch(r"\d{8}_\d{6}", event_id):
        raise ValueError(f"event_id must be YYYYMMDD_HHMMSS, got {event_id!r}")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", name):
        raise ValueError(f"name must be [A-Za-z0-9_-]+, got {name!r}")
    out = REPORTS_DIR / event_id / f"{name}.webp"
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(src) as img:
        img = img.convert("RGB")
        if img.width > max_width:
            img = img.resize((max_width, round(img.height * max_width / img.width)), Image.LANCZOS)
        img.save(out, "WEBP", quality=quality, method=6)
    return out


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", type=Path)
    ap.add_argument("event_id")
    ap.add_argument("name")
    ap.add_argument("--max-width", type=int, default=1800)
    ap.add_argument("--quality", type=int, default=85)
    args = ap.parse_args()
    out = convert(args.src, args.event_id, args.name, args.max_width, args.quality)
    logger.info("wrote %s (%d KB)", out, out.stat().st_size // 1024)


if __name__ == "__main__":
    main()
