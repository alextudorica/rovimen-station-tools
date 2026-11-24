"""Derive a meteor clip's faststart ``.mp4`` sibling on the VPS, at ingest.

Every archived meteor clip exists twice in ``meteors/``: the station's
Matroska original (``X_color.mkv``, ``X_color.details.mkv``) and a
stream-copied faststart MP4 of the same bitstream (``X_color.mp4``,
``X_color.details.mp4``). The ``.mkv`` is the canonical name: state.json keys,
media pointers, the clip requester's "is it archived" check, stack and lock
resolution and the ``.mkv``-only media routes (crop, loop, zip, stack
generation) all use it. The ``.mp4`` is what the players get: the archive
routes, ``/media/v1`` and the hot clip cache serve it directly and only remux
the ``.mkv`` on demand when it is missing.

Stations used to remux and upload both, so every clip crossed the uplink twice
(gmnro01 (Ghirdoveni), night 20261023: 8.6 GB in 9,433 s, half of it the same
H.264 bytes again). Stations now send only the ``.mkv``; the blob flusher calls
:func:`derive` on each spooled ``meteors/*.mkv`` before copying it to the
archive, which writes the ``.mp4`` into the spool next to it (so the flusher
archives it like any pushed blob) and into the hot clip cache. The archive
ends up with the same two files as before, so no reader changes.

Best-effort: a failed remux leaves the ``.mkv`` to flush on its own, and the
readers fall back to their on-demand remux, as they did when a station's remux
failed.

:func:`remux` is also the dashboard's one MKV-to-MP4 stream copy: the
on-demand remuxes (``routes.archive``, ``public_api``) and the hot-cache
warmer (``clip_cache_warmer``, stdlib-only, which is why ``clip_cache`` is
imported where it is used) call it with their own timeouts and temp paths.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

REMUX_TIMEOUT_S = 120


def sibling_name(filename: str) -> str | None:
    """The ``.mp4`` sibling of a meteor-clip ``.mkv`` name, else None."""
    if not filename.endswith(".mkv"):
        return None
    return filename[: -len(".mkv")] + ".mp4"


def remux(source: Path, dest: Path, *, timeout: float = REMUX_TIMEOUT_S,
          tmp: Path | None = None) -> None:
    """Stream-copy ``source`` into a faststart MP4 at ``dest`` (no re-encode).

    Writes ``tmp`` and renames it onto ``dest``. The default temp is
    dot-prefixed in ``dest``'s directory, so neither a reader nor the spool
    flusher (which skips dot-files) ever sees a partial file; ``tmp=dest``
    writes in place, for a caller whose ``dest`` is already its own private
    temp. ffmpeg is killed after ``timeout`` seconds. The temp is removed on
    any failure. Raises on failure."""
    if tmp is None:
        tmp = dest.with_name(f".{dest.name}.remux-{os.getpid()}-{threading.get_ident()}")
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-c", "copy", "-movflags", "+faststart",
                "-f", "mp4", str(tmp),
            ],
            capture_output=True, timeout=timeout,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"ffmpeg exited {proc.returncode}: "
                f"{proc.stderr.decode('utf-8', errors='replace')[:200]}")
        if tmp != dest:
            os.replace(tmp, dest)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def derive(archive_rel: Path, source: Path, spool_dir: Path) -> Path | None:
    """Write the faststart sibling of the spooled clip ``source`` into
    ``spool_dir`` and the hot clip cache; return the spooled ``.mp4``.

    ``archive_rel`` is the clip's archive-relative path
    (``<cam>/<date>/meteors/<name>.mkv``); anything else is ignored. A sibling
    already in the spool (pushed by a station still on the old bundle, or
    derived by an earlier attempt) is left as it is; one already flushed is
    derived again, an identical overwrite. Never raises."""
    parts = archive_rel.parts
    if len(parts) != 4 or parts[2] != "meteors":
        return None
    cam, date, _subdir, filename = parts
    mp4_name = sibling_name(filename)
    if mp4_name is None:
        return None
    dest = spool_dir / mp4_name
    if dest.exists() or any(spool_dir.glob(f"{mp4_name}.flushing-*")):
        return None
    try:
        remux(source, dest)
    except Exception as exc:
        logger.warning("faststart sibling for %s/%s/%s not derived: %s", cam, date, filename, exc)
        return None
    _tee_hot(cam, date, mp4_name, dest)
    return dest


def _tee_hot(cam: str, date: str, mp4_name: str, mp4: Path) -> None:
    """Copy the new sibling into the NVMe hot clip cache, so the first click
    on a fresh clip is served without an archive read. Best-effort."""
    import clip_cache

    hot = clip_cache.hot_mp4_path(cam, date, mp4_name)
    if hot is None or hot.exists():
        return
    tmp = hot.with_name(f".{hot.name}.part-{os.getpid()}-{threading.get_ident()}")
    try:
        hot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(mp4, tmp)
        os.replace(tmp, hot)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        logger.debug("hot clip cache tee failed for %s: %s", mp4_name, exc)
