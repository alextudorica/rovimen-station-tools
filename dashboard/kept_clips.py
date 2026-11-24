"""Clips a signed-in user watched, kept forever like a manual lock.

Playing a clip in the video modal POSTs ``/api/keep/...`` (routes/media.py),
which appends the clip here and locks it on its station. The station lock
uploads it; this file is what keeps it on the Storage Box afterwards:
``storagebox_janitor.load_protected`` never prunes a clip listed here. The
activity log cannot do that job, because logrotate drops its old lock lines.

One JSON object per line (``cam``, ``date``, ``chunk``, ``user``, ``ts``),
append-only so every gunicorn worker can write without a lock; readers dedup.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

KEPT_CLIPS_PATH = Path(
    os.environ.get("ROVIMEN_KEPT_CLIPS_PATH", "/opt/rovimen/kept_clips.jsonl")
)

# The video names one clip can be served under; all map to its capture chunk.
CLIP_NAME_RE = re.compile(r"^([\w.-]+_color)(?:\.details)?\.(?:mkv|mp4)$")

# Per-worker memory of what this process already wrote, so replaying a clip
# does not grow the file. Another worker may write the same clip once more;
# readers dedup.
_written: set[tuple[Path, tuple[str, str, str]]] = set()
_written_lock = threading.Lock()


def chunk_name(filename: str) -> str | None:
    """``X_color.mkv`` for any served video name of a clip, else None."""
    m = CLIP_NAME_RE.match(filename)
    return f"{m.group(1)}.mkv" if m else None


def record(cam: str, date: str, chunk: str, user: str | None,
           path: Path | None = None) -> None:
    """Append one kept clip. Raises OSError if the file cannot be written."""
    path = path or KEPT_CLIPS_PATH
    key = (cam.upper(), date, chunk)
    with _written_lock:
        if (path, key) in _written:
            return
    line = json.dumps({
        "cam": key[0], "date": date, "chunk": chunk, "user": user,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }, separators=(",", ":")) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(line)
    with _written_lock:
        _written.add((path, key))


def load(path: Path | None = None) -> set[tuple[str, str, str]]:
    """Every kept clip as ``(CAM, YYYYMMDD, X_color.mkv)``; empty if no file."""
    path = path or KEPT_CLIPS_PATH
    keys: set[tuple[str, str, str]] = set()
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rec = json.loads(line)
                    keys.add((rec["cam"].upper(), rec["date"], rec["chunk"]))
                except (ValueError, KeyError, TypeError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    return keys
