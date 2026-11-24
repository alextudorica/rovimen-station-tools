"""NVMe hot cache for detection clips — pre-remuxed faststart MP4s.

``clip_cache_warmer.py`` (systemd timer) remuxes the last N nights of
``meteors/*.mkv`` from the storage-box archive into seekable MP4s under
``ROVIMEN_CLIP_CACHE_DIR``. This module is the read side: given a clip
request it answers "is there a hot MP4 for this?" and serves it via
nginx X-Accel (or ``send_file`` without nginx) — no SSHFS stat, no
ffmpeg spawn, no worker thread pinned for the transfer.

Layout: ``<CLIP_CACHE_DIR>/<CAMERA>/<YYYYMMDD>/<stem>.mp4``.

Disabled entirely when ``ROVIMEN_CLIP_CACHE_DIR`` is unset (tests,
stations, dev boxes without the volume) — every lookup returns None and
callers fall through to their existing slow paths.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from flask import Response, send_file

from cache_store import NGINX_ACCEL, _is_nonempty_file

logger = logging.getLogger(__name__)

_dir = os.environ.get("ROVIMEN_CLIP_CACHE_DIR", "")
CLIP_CACHE_DIR: Path | None = Path(_dir) if _dir else None

# Conservative allow-lists — this module joins request-derived strings into
# filesystem paths, so it validates independently of whatever the caller
# already checked.
_CAMERA_RE = re.compile(r"^[A-Z0-9]{4,12}$")
_DATE_RE = re.compile(r"^\d{8}$")
_CLIP_RE = re.compile(r"^[\w.-]+\.(mkv|mp4)$")


def hot_mp4_path(camera: str, date_compact: str, clip_name: str) -> Path | None:
    """Path a hot MP4 for this clip would live at, or None.

    ``clip_name`` may be the ``.mkv`` or the ``.mp4`` sibling — both map
    to the same stem-keyed cache entry, so frontends that learned either
    filename hit the same hot file.

    None when the cache is disabled or any component fails validation.
    Does NOT check existence — that's the caller's (or ``serve_hot_mp4``'s)
    job, so the warmer can use this for its write targets too.
    """
    if CLIP_CACHE_DIR is None:
        return None
    if not _CAMERA_RE.match(camera or ""):
        return None
    if not _DATE_RE.match(date_compact or ""):
        return None
    if not _CLIP_RE.match(clip_name or "") or ".." in clip_name:
        return None
    stem = clip_name.rsplit(".", 1)[0]
    return CLIP_CACHE_DIR / camera / date_compact / f"{stem}.mp4"


def serve_hot_mp4(camera: str, date_compact: str, clip_name: str) -> Response | None:
    """Serve the hot MP4 for this clip, or None on miss.

    Hit + nginx: X-Accel-Redirect to the internal ``/internal/clip_cache/``
    location — nginx streams the file (full Range support) and the worker
    thread is released immediately. Hit without nginx: ``send_file``.

    A 0-byte file is a MISS (mirrors the thumb-cache guard): the warmer
    writes via tmp+rename so this shouldn't happen, but serving an empty
    immutable video would wedge the player until the browser cache expires.
    """
    path = hot_mp4_path(camera, date_compact, clip_name)
    if path is None or not _is_nonempty_file(path):
        return None
    if NGINX_ACCEL:
        accel = "/internal/clip_cache/" + "/".join(
            [camera, date_compact, path.name]
        )
        return Response(headers={
            "X-Accel-Redirect": accel,
            "Content-Type": "video/mp4",
            "Cache-Control": "public, max-age=86400, immutable",
        })
    resp = send_file(path, mimetype="video/mp4", conditional=True,
                     download_name=path.name)
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp
