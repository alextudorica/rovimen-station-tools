"""Archive and video-cache routes -- storagebox reads, MKV remux, cached-video proxy.

Extracted from rovimen_dashboard.py. Wired in from ``create_app()`` via
``register_archive_routes`` (routes); ``rovimen_dashboard`` also imports
``refresh_nights_full`` (boot pre-warm) and ``cache_cleanup_loop`` (started by
its ``_start_background_workers``).

Module layout, the same as ``routes/sky_dome.py``:

- Every handler and helper is a module-level function, so a helper can be
  imported and unit-tested without building the app.
- What they need from ``create_app`` (``config``, ``tunnels``, the archive
  index, the nights_full cache and its TTL, the thumbnail prefetch, the
  /api/spectro shared tier) and the per-app /api/spectro caches sit in one
  frozen ``ArchiveDeps``. ``register_archive_routes`` builds it and stores it
  in ``_DEPS``; a function that needs a dependency reads it through
  ``_deps()`` in its first lines. Each registration replaces the holder, so in
  a process that builds several apps (the test suite) the last app registered
  wins; a gunicorn worker builds one app. The clip resolvers other route
  modules call (``_archive_clip_path``, ``archive_clip_path_bounded``,
  ``archive_detection_window``) work before registration too: without an
  archive index they trust only a direct stat.
- ``register_archive_routes`` is the only code that touches the Flask app:
  it sets ``_DEPS`` and adds the URL rules with ``app.add_url_rule``. The
  endpoint names stay the function names (``api_archive_file``) and
  ``@require_station`` / ``@require_auth`` / ``@public_route`` stay on the
  functions, so the auth gate (``security._matched_public_view``) and the CSRF
  exemption see the same views as when the routes were closures. There is no
  Blueprint: it would rename every endpoint to ``archive.<name>``.
- Registering the routes starts no thread. The hourly ``CACHE_PATH`` sweep,
  ``cache_cleanup_loop``, is started by ``_start_background_workers``.

Process-wide state (directory listings, archived RMS rows, branded stacks,
station download jobs, work pools) is module-private.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from flask import Flask, Response, abort, jsonify, redirect, request, send_file

from cache_store import (
    _ARCHIVE_HOT_DAYS,
    ARCHIVE_PATH,
    BRANDED_STACK_DIR,
    CACHE_PATH,
    CACHE_TTL_HOURS,
    NGINX_ACCEL,
    BoundedCache,
    _disk_cache_path,
    _disk_cache_write,
    _kick_cache_refresh,
    _drop_future_dates,
    _is_nonempty_file,
    _thumb_cache_path,
    rms_night_generation,
)

import clip_cache
import faststart_sibling
import gmn_data
import hidden_detections
import stack_overlay
import upload_state
from shared_snapshot import SharedSnapshotStore

from auth import (
    require_auth,
    require_station,
    _session_role,
    _has_camera_access,
    _host_keys_for_camera,
    camera_is_public_for_request,
    is_anonymous,
    station_is_public_for_request,
)
from fdp_products import (
    COLOR_METEOR_STACK_FILENAME,
    FDP_SUBDIR,
    archive_candidates as fdp_archive_candidates,
    product_filename as fdp_product_filename,
)
from http_caching import _json_cached
from route_helpers import (
    clip_base, compute_detection_offset, listed_clip_names, media_url, stack_name_for_clip,
    validate_media_params,
)
from security import public_route
from station_client import (
    station_url,
    station_get_raw,
    _session_for_url,
    _with_sshfs_timeout,
)
from tunnels import _TunnelDown

if TYPE_CHECKING:
    from cache_store import _ArchiveIndex
    from models import DashboardConfig
    from tunnels import TunnelManager

logger = logging.getLogger(__name__)

# Sentinel returned by the stack overlay gate when the branded variant could
# not be produced (storagebox read failure or a render error). The route maps
# it to 503 rather than serving the un-branded original.
_STACK_MISS = object()

# Shared cache of branded per-detection stacks, so a repeatedly-downloaded
# legacy stack is rendered once, not on every request.
_branded_stack_cache = stack_overlay.OverlaidStackCache(BRANDED_STACK_DIR)

# ── Archive-file existence: one listing per directory, not one stat per file ──
#
# A gallery asks for a whole directory at once. The Spectro tab renders a
# night as up to 170 pictures (gmnro01/GH662S/20260920: 82 event cards, each
# with a widefield frame, a grating frame and a reduced spectrum); the Archive
# and VDB tabs do the same with a night's stacks. Every one of those <img>
# requests used to re-stat its own file through the six-permit SSHFS lane in
# ``station_client``, whose acquire timeout is 2 s -- so opening the tab once
# saturated the lane and 66 of 170 pictures came back 503 (measured against
# prod, 2026-09-22): a third of the gallery rendered as broken images, and
# clicking one opened the lightbox on a broken image.
#
# One scandir answers the whole directory instead. A name the listing carries
# is served without touching the mount again; a name it does not carry still
# falls through to its own stat, so a file written after the listing was taken
# is found exactly as before and nothing new 404s. The TTL is short because a
# night gains its files in one dawn run; an unknown listing (shed lane, failed
# walk) is remembered far more briefly, only long enough that the burst does
# not re-walk once per request.
_ARCHIVE_DIR_TTL = 60.0
_ARCHIVE_DIR_UNKNOWN_TTL = 5.0
_ARCHIVE_DIR_MAX = 512
_archive_dir_cache: dict[tuple[str, str, str], tuple[float, frozenset[str] | None]] = {}
_archive_dir_locks: dict[tuple[str, str, str], threading.Lock] = {}
# Guards both dicts against concurrent insert-while-iterating during the prune,
# the same trap _spectro_miss_lock exists for.
_archive_dir_guard = threading.Lock()


def _prune_archive_dir_cache() -> None:
    """Bound the listing cache: one entry per camera/night/subdir would
    otherwise accumulate for the life of the deploy."""
    with _archive_dir_guard:
        if len(_archive_dir_cache) <= _ARCHIVE_DIR_MAX:
            return
        now = time.time()
        for key in [k for k, (exp, _) in list(_archive_dir_cache.items())
                    if exp <= now]:
            _archive_dir_cache.pop(key, None)
            _archive_dir_locks.pop(key, None)
        while len(_archive_dir_cache) > _ARCHIVE_DIR_MAX:
            key = next(iter(_archive_dir_cache))
            _archive_dir_cache.pop(key, None)
            _archive_dir_locks.pop(key, None)


def _archive_dir_names(camera: str, date: str,
                       subdir: str) -> frozenset[str] | None:
    """Entry names in ``<archive>/<camera>/<date>/<subdir>``, cached briefly.

    ``None`` means the listing is unknown -- the SSHFS guard shed it or the
    walk failed -- and the caller must fall back to its own check rather than
    read that as an empty directory. A per-key lock makes the burst that opens
    a tab take ONE listing: the first request walks, the rest wait a moment and
    read what it cached.
    """
    key = (camera, date, subdir)
    hit = _archive_dir_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    with _archive_dir_guard:
        lock = _archive_dir_locks.get(key)
        if lock is None:
            lock = _archive_dir_locks[key] = threading.Lock()
    with lock:
        hit = _archive_dir_cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        directory = ARCHIVE_PATH / camera / date / subdir

        def _walk() -> frozenset[str]:
            try:
                return frozenset(e.name for e in os.scandir(directory))
            except FileNotFoundError:
                return frozenset()

        # 8 s, the same budget /api/spectro gives the identical walk: one of
        # these directories holds a night's artifacts (352 entries on
        # GH662S/20260920) and a cold listing is slower than a stat.
        names = _with_sshfs_timeout(_walk, timeout=8.0, default=None)
        ttl = _ARCHIVE_DIR_UNKNOWN_TTL if names is None else _ARCHIVE_DIR_TTL
        _archive_dir_cache[key] = (time.time() + ttl, names)
    _prune_archive_dir_cache()
    return names


def _is_per_detection_stack(subdir: str, filename: str) -> bool:
    """True for a per-chunk meteor stack (``<stem>_stack.webp`` in meteors/stacks).

    The whole-night composite (``<cam>_<date>_night_stack.webp``) is excluded --
    it is already overlaid at generation and lives under the same subdirs.
    """
    return (
        subdir in ("meteors", "stacks")
        and filename.endswith("_stack.webp")
        and not filename.endswith("_night_stack.webp")
    )


# Work lanes. Downloads (shortclip trims, stitches) and first playback used
# to share one 2-thread pool, so two downloads in progress held up the remux
# a clip needs before it can play at all. Playback now has its own lane;
# background copies into the hot cache get a third, so neither can queue
# behind the other.
_media_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-remux")
_playback_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="media-playback")
_promote_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-promote")

# Bounds concurrent stitch-multi (filter_complex libx264) jobs. The work runs
# on ``_media_executor`` (2 threads) but a semaphore also caps how many request
# threads can be *waiting* on a stitch at once, so a burst of stitch POSTs
# can't pin the whole gunicorn gthread pool for 90 s each.
_STITCH_CONCURRENCY = int(os.environ.get("ROVIMEN_STITCH_CONCURRENCY", "2"))
_stitch_sem = threading.BoundedSemaphore(_STITCH_CONCURRENCY)


def _run_ffmpeg_capped(cmd: list[str], timeout: float) -> tuple[int, bytes, bool]:
    """Run an ffmpeg command, killing it on timeout.

    Returns ``(returncode, stderr, timed_out)``. Intended to be submitted to
    ``_media_executor`` so the heavy libx264 work runs on a bounded pool
    instead of directly on the gunicorn request thread.
    """
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _, stderr = proc.communicate(timeout=timeout)
        return proc.returncode, stderr, False
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        return -1, b"", True


_STITCH_TIMEOUT_S = 120.0


# Stitched composites render to a file and are fetched with a plain GET.
# Streaming one as a long chunked POST response broke behind Cloudflare once
# full-resolution tiles made it ~20 MB (2026-10-19: the edge cancelled the
# stream at 14 MB, "Failed to fetch"), and a slow render could pass nginx's
# 30 s limit. Files are keyed by a random job id and kept for an hour.
_STITCH_RETRY_AFTER_S = 5
_STITCH_KEEP_S = 3600.0
_STITCH_JOB_RE = re.compile(r"^[0-9a-f]{32}$")


def _stitch_dir() -> Path:
    d = CACHE_PATH / "stitch"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _stitch_prune(d: Path) -> None:
    """Drop stitch files older than _STITCH_KEEP_S."""
    cutoff = time.time() - _STITCH_KEEP_S
    for f in d.iterdir():
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def _stitch_render(cmd: list[str], job: str, d: Path) -> None:
    """Run one stitch ffmpeg into ``<job>.mp4`` (atomically), or leave
    ``<job>.err`` with the reason. Releases the stitch slot when done."""
    tmp, out, err = d / f"{job}.tmp.mp4", d / f"{job}.mp4", d / f"{job}.err"
    try:
        rc, stderr, timed_out = _run_ffmpeg_capped([*cmd, str(tmp)], _STITCH_TIMEOUT_S)
        if rc == 0 and not timed_out and tmp.exists():
            os.replace(tmp, out)
            return
        detail = "timed out" if timed_out else stderr.decode("utf-8", "replace")[-500:]
        logger.error("stitch-multi %s failed: %s", job, detail)
        err.write_text(detail)
    except Exception as exc:
        logger.exception("stitch-multi %s crashed", job)
        err.write_text(str(exc))
    finally:
        _silent_unlink(tmp)
        _stitch_sem.release()


# ── MKV to MP4 remux via thread pool ────────────────────────────────

def _silent_unlink(path: Path) -> None:
    """Remove *path* ignoring errors -- safe for call_on_close callbacks."""
    try:
        path.unlink(missing_ok=True)
    except Exception:
        pass


def _remux_to_mp4(source: Path, tmp_path: Path) -> None:
    """Remux into *tmp_path* (the caller's own temp) in the calling thread
    (submitted to a pool), 30 s cap. On any error the temp file is removed so
    the caller never sees a partial file."""
    faststart_sibling.remux(source, tmp_path, timeout=30, tmp=tmp_path)


# ── Archive-first clip serving ────────────────────────────────────────
#
# Push-era inversion: the pusher lands locked clips (and ready-made
# faststart .mp4 siblings) on the storage box within minutes of
# detection, while the station itself sits behind a ~10 Mbps home
# uplink. Locked clips are immutable, so an archive hit needs no
# revalidation — the archive is tried BEFORE the station proxy.


# ── Dependencies from create_app ────────────────────────────────────────

@dataclass(frozen=True)
class ArchiveDeps:
    """What the routes and helpers need from ``create_app``.

    ``nights_full_cache`` is the per-camera deep-walk cache
    ``rovimen_dashboard`` rehydrates from disk at boot, ``nights_full_ttl``
    how long an entry is served without a background refresh.
    ``prefetch_archive_thumbs(host_key, camera, date)`` warms a night's
    thumbnails after a thumb-cache miss. ``shared_spectro`` is /api/spectro's
    cross-worker tier; ``spectro_cache``, ``spectro_miss`` and
    ``spectro_hits`` are its per-app tier (see ``api_spectro``), new for each
    registration as they were when they lived in the closure.
    """

    config: DashboardConfig
    tunnels: TunnelManager
    archive_idx: _ArchiveIndex
    nights_full_cache: dict[str, tuple[float, list]]
    nights_full_ttl: float
    prefetch_archive_thumbs: Callable[[str, str, str], None] | None
    shared_spectro: SharedSnapshotStore
    # host -> (expires_at, payload)
    spectro_cache: dict[str, tuple[float, dict]] = field(default_factory=dict)
    # (cam, date) -> expiry, for night dirs that held no spectro products
    spectro_miss: dict[tuple[str, str], float] = field(default_factory=dict)
    # (cam, date) -> (expiry, night entry) for nights that did list products
    spectro_hits: dict[tuple[str, str], tuple[float, dict]] = field(default_factory=dict)


# Set by register_archive_routes; each registration replaces it.
_DEPS: ArchiveDeps | None = None


def _deps() -> ArchiveDeps:
    if _DEPS is None:
        raise RuntimeError("archive code used before register_archive_routes")
    return _DEPS


def _archive_idx_or_none() -> _ArchiveIndex | None:
    """The registered archive index, or None before ``register_archive_routes``.

    The clip resolvers below have half a dozen callers across the
    cached-video / shortclip / promote paths and other route modules; without
    an index they trust only a direct stat, as they always did.
    """
    return _DEPS.archive_idx if _DEPS is not None else None


def _archive_clip_path(station_code: str, date: str, name: str) -> Path | None:
    """First existing archive path for *name* (meteors/ first, then root).

    A direct stat on the SSHFS mount is shed or times out under the morning
    upload window, and reading that as "file absent" 404'd shortclips whose
    bytes were on the box the whole time (observed on the events page during
    the 2026-09-17 dawn window). When the stat fails but the archive index's
    night listing carries the name, trust the index and return the path —
    the subsequent read may be slow, but slow beats a wrong 404.
    """
    for subdir in ("meteors", ""):
        p = (
            ARCHIVE_PATH / station_code / date / subdir / name
            if subdir
            else ARCHIVE_PATH / station_code / date / name
        )
        if _is_nonempty_file(p):
            return p
    archive_idx = _archive_idx_or_none()
    if archive_idx is not None:
        try:
            ni = archive_idx.night_files(station_code, date)
        except Exception:
            ni = None
        if ni is not None and name in ni.meteor_files:
            return ARCHIVE_PATH / station_code / date / "meteors" / name
    return None


def archive_clip_path_bounded(station_code: str, date: str, name: str,
                              timeout: float = 5.0) -> Path | None:
    """``_archive_clip_path`` that cannot pin the caller on a stalled mount.

    The stat runs under the SSHFS guard; if it stalls, the archive index's
    night listing decides (the same trust ``_archive_clip_path`` gives it
    when a stat fails outright).
    """
    miss = object()
    got = _with_sshfs_timeout(
        lambda: _archive_clip_path(station_code, date, name),
        timeout=timeout, default=miss,
    )
    if got is not miss:
        return got
    archive_idx = _archive_idx_or_none()
    if archive_idx is not None:
        try:
            ni = archive_idx.night_files(station_code, date)
        except Exception:
            ni = None
        if ni is not None and name in ni.meteor_files:
            return ARCHIVE_PATH / station_code / date / "meteors" / name
    return None


def archive_media_path(station_code: str, date: str, subdir: str,
                       name: str) -> Path | None:
    """Archive path of a clip (``meteors``) or night product (``timelapse``)
    when the box has it, looked up the bounded way: the cached night listing
    first, one guarded stat only for a name the listing does not carry."""
    if subdir == "meteors":
        return archive_clip_path_bounded(station_code, date, name)
    names = _archive_dir_names(station_code, date, subdir)
    path = ARCHIVE_PATH / station_code / date / subdir / name
    if names is not None and name in names:
        return path
    if _with_sshfs_timeout(lambda: _is_nonempty_file(path), timeout=5.0,
                           default=False):
        return path
    return None


_promote_inflight: set[str] = set()
_promote_lock = threading.Lock()


def _promote_archive_mp4(
    source: Path, station_code: str, date: str, filename: str
) -> None:
    """Copy an archived faststart MP4 into the NVMe hot cache (background).

    Best-effort: the current request is already streaming from the
    archive; this just makes the NEXT click an X-Accel hit instead of
    an SSHFS read. Coalesced per target so repeated clicks don't queue
    duplicate copies.
    """
    hot = clip_cache.hot_mp4_path(station_code, date, filename)
    if hot is None or _is_nonempty_file(hot):
        return
    key = str(hot)
    with _promote_lock:
        if key in _promote_inflight:
            return
        _promote_inflight.add(key)

    def _copy() -> None:
        tmp = hot.with_name(
            f".{hot.name}.{os.getpid()}-{threading.get_ident()}.tmp"
        )
        try:
            hot.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, tmp)
            os.replace(tmp, hot)
        except Exception as exc:
            _silent_unlink(tmp)
            logger.warning("Hot-promote failed for %s: %s", hot.name, exc)
        finally:
            with _promote_lock:
                _promote_inflight.discard(key)

    _promote_executor.submit(_copy)


def _remux_persist_and_serve(
    source: Path, station_code: str, date: str, filename: str
) -> Response:
    """Remux to MP4 once, persisting into the NVMe hot clip cache.

    Subsequent requests for the same clip hit the X-Accel fast path
    instead of paying for another ffmpeg run. Falls back to the
    throwaway-tempfile remux when the clip cache is disabled.
    Concurrent remuxes of the same clip use unique tmp names, so the
    atomic ``os.replace`` is last-writer-wins with identical content.
    """
    hot = clip_cache.hot_mp4_path(station_code, date, filename)
    if hot is None:
        return _stream_mkv_as_mp4(source, filename)
    tmp = hot.with_name(f".{hot.name}.{os.getpid()}-{threading.get_ident()}.tmp")
    try:
        hot.parent.mkdir(parents=True, exist_ok=True)
        future = _playback_executor.submit(_remux_to_mp4, source, tmp)
        future.result(timeout=35)
        os.replace(tmp, hot)
    except Exception:
        _silent_unlink(tmp)
        logger.exception("Persistent remux failed for %s", filename)
        return _stream_mkv_as_mp4(source, filename)
    resp = clip_cache.serve_hot_mp4(station_code, date, filename)
    if resp is not None:
        return resp
    return send_file(hot, mimetype="video/mp4", conditional=True)


def _serve_archive_clip(
    station_code: str, date: str, filename: str, want_mp4: bool
) -> Response | None:
    """Serve a clip straight from the storagebox archive, or None on miss.

    Preference order for MP4 output: the ready-made faststart ``.mp4``
    sibling uploaded by the station (no ffmpeg at all), then a one-time
    persisted remux of the archived ``.mkv``. The direct sibling is also
    copied into the NVMe hot cache in the background so the next click
    is an X-Accel hit.
    """
    stem = filename.rsplit(".", 1)[0]
    if want_mp4:
        sibling = _archive_clip_path(station_code, date, stem + ".mp4")
        if sibling is not None:
            _promote_archive_mp4(sibling, station_code, date, filename)
            resp = send_file(
                sibling, mimetype="video/mp4", conditional=True,
                download_name=stem + ".mp4",
            )
            resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
            return resp
        mkv = _archive_clip_path(station_code, date, stem + ".mkv")
        if mkv is not None:
            return _remux_persist_and_serve(mkv, station_code, date, filename)
        return None
    exact = _archive_clip_path(station_code, date, filename)
    if exact is None:
        return None
    mime = "video/mp4" if filename.endswith(".mp4") else "video/x-matroska"
    resp = send_file(exact, mimetype=mime, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=86400, immutable"
    return resp


def _stream_mkv_as_mp4(source: Path, original_name: str) -> Response:
    """Remux MKV to MP4 via ffmpeg in a thread-pool worker, then send_file.

    The remux runs in ``_playback_executor`` to cap concurrent ffmpeg
    processes at 2.  The request worker still blocks on the result
    (``future.result``), but the thread pool prevents more than 2
    remuxes from running at once.  The resulting MP4 uses +faststart
    for seekable output.  The temp file is cleaned up after Flask
    finishes streaming the response.
    """
    mp4_name = original_name.rsplit(".", 1)[0] + ".mp4"
    fd, tmp_str = tempfile.mkstemp(suffix=".mp4", prefix="remux_")
    os.close(fd)
    tmp_path = Path(tmp_str)
    try:
        future = _playback_executor.submit(_remux_to_mp4, source, tmp_path)
        future.result(timeout=35)
    except Exception:
        _silent_unlink(tmp_path)
        logger.exception("MKV remux failed for %s", original_name)
        abort(500)
    resp = send_file(
        tmp_path,
        mimetype="video/mp4",
    )
    resp.call_on_close(lambda: _silent_unlink(tmp_path))
    return resp


# ── Trimmed downloads cut from the archive copy ─────────────────────

# nginx cuts a request off after 30 s without a byte, so a trim that runs
# longer is lost anyway; fail it cleanly first.
_TRIM_FFMPEG_TIMEOUT_S = 24.0
_CHUNK_START_RE = re.compile(r"^[A-Za-z0-9]+_(\d{8})_(\d{6})")


def trim_clip_response(src: Path, ss: float, t: float,
                       filename: str) -> Response | None:
    """Cut ``[ss, ss + t]`` of *src* into an MP4 attachment named
    ``<stem>_clip.mp4`` (``X-Clip-Trimmed: 1``), or None when ffmpeg fails
    or times out, so the caller can fall back to the whole clip and say so.
    """
    fd, tmp_str = tempfile.mkstemp(suffix=".mp4", prefix="trim_")
    os.close(fd)
    tmp_path = Path(tmp_str)
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ss", f"{ss:.3f}", "-i", str(src), "-t", f"{t:.3f}",
        "-fps_mode", "passthrough",  # MKV reports 1000 fps; see media._build_crop_cmd
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-an", "-movflags", "+faststart", "-y", str(tmp_path),
    ]
    future = _media_executor.submit(_run_ffmpeg_capped, cmd, _TRIM_FFMPEG_TIMEOUT_S)
    try:
        returncode, stderr, timed_out = future.result(timeout=_TRIM_FFMPEG_TIMEOUT_S + 4)
    except Exception:
        _silent_unlink(tmp_path)
        logger.exception("trim failed for %s", filename)
        return None
    if timed_out or returncode != 0:
        logger.error("trim ffmpeg %s for %s: %s",
                     "timed out" if timed_out else "failed", filename,
                     stderr.decode(errors="replace")[:300])
        _silent_unlink(tmp_path)
        return None
    stem = filename.rsplit(".", 1)[0].replace("_color", "")
    resp = send_file(tmp_path, mimetype="video/mp4", as_attachment=True,
                     download_name=f"{stem}_clip.mp4")
    resp.call_on_close(lambda: _silent_unlink(tmp_path))
    resp.headers["X-Clip-Trimmed"] = "1"
    return resp


def looped_clip_response(segment: str, loops: int, outname: str) -> Response:
    """Play ``segment`` ``loops`` times in one MP4 attachment named ``outname``.

    The loop-download routes (``/loop_clip``, ``/cropped_loop_clip``,
    ``/loop_clip_archive``) each cut their segment their own way and end here.
    ffmpeg's concat demuxer stream-copies (no re-encode), on the request
    thread like the cut before it, with a 120 s cap; ``concat.txt`` and
    ``output.mp4`` go next to ``segment``. Raises
    ``subprocess.CalledProcessError`` / ``TimeoutExpired`` for the caller.
    """
    tmpdir = os.path.dirname(segment)
    concat_path = f"{tmpdir}/concat.txt"
    output_path = f"{tmpdir}/output.mp4"
    with open(concat_path, "w") as f:
        for _ in range(loops):
            f.write(f"file '{segment}'\n")
    subprocess.run(
        ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
         "-i", concat_path, "-c", "copy", output_path],
        check=True, capture_output=True, timeout=120,
    )
    with open(output_path, "rb") as f:
        data = f.read()
    return Response(
        data, status=200,
        headers={
            "Content-Type": "video/mp4",
            "Content-Disposition": f'attachment; filename="{outname}"',
            "Content-Length": str(len(data)),
        },
    )


def archive_detection_window(camera: str, date: str, filename: str,
                             pre: float, post: float) -> tuple[float, float] | None:
    """``(ss, t)`` covering ``pre`` s before to ``post`` s after the clip's
    detection, from the lock's ``meteor_time`` in the night's archived
    state.json (via the archive index), or None when it is unknown."""
    archive_idx = _archive_idx_or_none()
    if archive_idx is None:
        return None
    try:
        ni = archive_idx.night_files(camera, date)
    except Exception:
        return None
    chunks = (getattr(ni, "state_chunks", None) if ni is not None else None) or {}
    stem = filename.rsplit(".", 1)[0]
    info = chunks.get(filename) or chunks.get(stem + ".mkv")
    lock = info.get("lock") if isinstance(info, dict) else None
    mt = lock.get("meteor_time") if isinstance(lock, dict) else None
    m = _CHUNK_START_RE.match(filename)
    if not mt or not m:
        return None
    try:
        start = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc)
        met = datetime.fromisoformat(str(mt).replace("Z", "+00:00"))
    except ValueError:
        return None
    if met.tzinfo is None:
        met = met.replace(tzinfo=timezone.utc)
    off = (met - start).total_seconds()
    if not 0 <= off <= 120:
        return None
    return max(0.0, off - pre), pre + post


# ── Station pulls that outlive one request ───────────────────────────
#
# Footage that never reaches the archive (Grid footage, Video DB chunks,
# tonight's clips before upload) has to come from the station, over a home
# uplink, and the station renders the overlay on demand before it sends the
# first byte (~1 min for a raw chunk). nginx gives up after 30 s without a
# byte (proxy_read_timeout, hand-managed on the VPS), so a pull done inside
# the request died there: ~40 x 502/504 in 14 days. Now one background job
# per clip downloads into CACHE_PATH, and a request waits at most
# STATION_WAIT_S: it streams the bytes that have arrived, serves the cached
# file once complete, or answers 202 + Retry-After so the player tries again
# while the job keeps going.

STATION_WAIT_S = float(os.environ.get("ROVIMEN_STATION_WAIT_S", "20"))
_STATION_RETRY_AFTER_S = 3
_station_fetch_executor = ThreadPoolExecutor(
    max_workers=int(os.environ.get("ROVIMEN_STATION_FETCH_WORKERS", "4")),
    thread_name_prefix="station-fetch",
)
# A finished job is kept this long so a retry in flight still finds its result.
_STATION_JOB_KEEP_S = 120.0


class _StationJob:
    """One background download of a station file into the local cache."""

    def __init__(self, dest: Path):
        self.dest = dest
        self.tmp = dest.with_name(f".{dest.name}.{os.getpid()}-{id(self)}.part")
        self.started = threading.Event()   # tmp exists and bytes are arriving
        self.done = threading.Event()
        self.size: int | None = None        # upstream Content-Length
        self.ok = False
        self.error: str | None = None
        self.finished_at = 0.0


_station_jobs: dict[str, _StationJob] = {}
_station_jobs_lock = threading.Lock()


def _run_station_fetch(job: _StationJob, url: str) -> None:
    resp = None
    try:
        # Connect fast, but allow the station a minute+ to render the overlay
        # before its first byte: the job, not a request, is waiting now.
        resp = _session_for_url(url).get(url, timeout=(10, 180), stream=True)
        resp.raise_for_status()
        cl = resp.headers.get("Content-Length")
        try:
            job.size = int(cl) if cl else None
        except ValueError:
            job.size = None
        job.dest.parent.mkdir(parents=True, exist_ok=True)
        with open(job.tmp, "wb", buffering=0) as fh:
            job.started.set()
            for chunk in resp.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
        got = job.tmp.stat().st_size
        if job.size is not None and got != job.size:
            raise OSError(f"truncated: expected {job.size} bytes, got {got}")
        os.replace(job.tmp, job.dest)
        job.ok = True
    except Exception as exc:
        job.error = str(exc)[:200]
        logger.warning("station fetch failed for %s: %s", job.dest.name, exc)
        # Readers following the growing file keep their open handle; the
        # name is only removed so a failed pull is never mistaken for a hit.
        _silent_unlink(job.tmp)
    finally:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        job.finished_at = time.time()
        job.started.set()   # wake waiters; ok/error says what happened
        job.done.set()


def _station_job(url: str, dest: Path) -> _StationJob:
    """The running (or just-finished) job for *dest*, starting one if none."""
    key = str(dest)
    now = time.time()
    with _station_jobs_lock:
        for k in [k for k, j in _station_jobs.items()
                  if j.done.is_set() and now - j.finished_at > _STATION_JOB_KEEP_S]:
            _station_jobs.pop(k, None)
        job = _station_jobs.get(key)
        if job is not None and not job.done.is_set():
            return job                      # in flight: join it
        if job is not None and job.ok and dest.exists():
            return job                      # just finished and still cached
        job = _StationJob(dest)
        _station_jobs[key] = job
    _station_fetch_executor.submit(_run_station_fetch, job, url)
    return job


def _preparing_response(detail: str) -> Response:
    """202: the file is on its way; the player retries after Retry-After."""
    resp = jsonify({
        "preparing": True,
        "detail": detail,
        "retry_after_seconds": _STATION_RETRY_AFTER_S,
    })
    resp.status_code = 202
    resp.headers["Retry-After"] = str(_STATION_RETRY_AFTER_S)
    resp.headers["Cache-Control"] = "no-store"
    return resp


def _follow_growing_file(job: _StationJob):
    """Yield the job's bytes as they land, until the download finishes."""
    try:
        fh = open(job.tmp, "rb")
    except OSError:
        # Finished (renamed) between the check and the open.
        fh = open(job.dest, "rb")
    with fh:
        while True:
            chunk = fh.read(65536)
            if chunk:
                yield chunk
                continue
            if job.done.is_set():
                rest = fh.read()
                if rest:
                    yield rest
                return
            job.done.wait(0.25)


def serve_station_clip(url: str, dest: Path, want_mp4: bool,
                       station_code: str, date: str, filename: str,
                       wait_s: float | None = None) -> Response:
    """Serve a station file through a background download into *dest*.

    Never holds the request past ``wait_s`` (default STATION_WAIT_S), so
    nginx's 30 s limit can't cut it off. MP4 needs the whole file to remux,
    so it waits for completion and otherwise answers 202; the original MKV
    streams as soon as bytes arrive.
    """
    wait_s = STATION_WAIT_S if wait_s is None else wait_s
    job = _station_job(url, dest)
    if want_mp4:
        job.done.wait(wait_s)
        if not job.done.is_set():
            return _preparing_response("Fetching the clip from the station")
    else:
        job.started.wait(wait_s)
        if not job.started.is_set():
            return _preparing_response("The station is preparing the clip")
    if job.done.is_set():
        if not job.ok:
            resp = jsonify({"error": "Video temporarily unavailable",
                            "detail": job.error or "station fetch failed"})
            resp.status_code = 502
            return resp
        if want_mp4:
            return _remux_persist_and_serve(dest, station_code, date, filename)
        return send_file(dest, mimetype="video/x-matroska", conditional=True)
    # Still downloading: stream what has arrived and follow the rest. No
    # Range support until it is complete, so a seek re-requests the file.
    headers = {"Content-Type": "video/x-matroska", "Cache-Control": "no-store"}
    if job.size is not None:
        headers["Content-Length"] = str(job.size)
    return Response(_follow_growing_file(job), status=200, headers=headers)


# ---------------------------------------------------------------------------
# RMS file parsers (read from archive rms/ subdir)
#
# The implementations now live in ``rms_parse`` (stdlib-only) so non-Flask
# code can reuse them. Re-exported here under their original underscore
# names to keep existing importers (routes.detections, routes.media) working.
# ---------------------------------------------------------------------------

from rms_parse import (  # noqa: E402
    parse_radiants_txt as _parse_radiants_txt,
    parse_ftpdetectinfo as _parse_ftpdetectinfo,
    parse_session_detections as _parse_session_detections,
)


def archive_rms_detections(camera: str, date: str) -> list[dict]:
    """A night's archived detection rows, from the canonical session parse.

    ``rms_parse.parse_session_detections`` joins radiants to FTPdetectinfo by
    meteor begin time and keys every row on its real FF file + meteor number,
    so a night whose two files differ in length or order still pairs each
    meteor with its own photometry — and every row carries the full science set
    (kinematics, az/elev, radiant, angular velocity) rather than the handful of
    fields a positional merge could safely assume.

    The station API answers ``/api/rms-detections`` from the same routine, so a
    clip's details do not change shape when a night ages out of the two-day
    live window and starts being served from the archive instead.

    Never raises: an unreadable ``rms/`` dir yields an empty list, leaving the
    caller with a clip and no detail panel rather than a failed request.
    """
    rms_dir = ARCHIVE_PATH / camera / date / "rms"
    if not rms_dir.is_dir():
        return []
    try:
        return _parse_session_detections(rms_dir)
    except OSError:
        logger.debug("archive rms parse failed for %s/%s", camera, date, exc_info=True)
        return []


# ── Archived RMS detections: parse once, share, bound ───────────────────────
#
# Archived nights are immutable once dawn processing has uploaded them, yet
# every RMS-tab open re-parsed FTPdetectinfo + radiants over SSHFS for EVERY
# night of the camera, unguarded, all at once (the tab fans out one request
# per night), and the clip player's detail panel did the same on every clip
# played. That was the single largest source of 504s on prod (109 in 2.6 h)
# and, because the parse held a gthread for as long as the Storage Box took,
# the reason cheap endpoints queued behind it timed out too. The rows are
# now parsed once per (camera, night), kept in a bounded per-worker cache and
# on disk so recycled and sibling workers reuse them, and the parse runs
# under the SSHFS guard so a slow box costs an empty panel, not a pinned
# thread. Callers that add fields to a row must copy it first: the rows are
# shared.
_RMS_ARCHIVE_TTL_S = 6 * 3600
_RMS_ARCHIVE_EMPTY_TTL_S = 600          # a night with no rms/ yet may still get one
_rms_archive_cache: BoundedCache = BoundedCache(512)   # (cam, date) -> (mono, rows, ttl, gen)
_rms_archive_lock = threading.Lock()


def _disk_cache_read(name: str, key: str, max_age_s: float) -> Any | None:
    """Counterpart of cache_store._disk_cache_write for one entry."""
    try:
        raw = json.loads(_disk_cache_path(name, key).read_text())
        if time.time() - float(raw.get("saved_at") or 0) > max_age_s:
            return None
        return raw.get("data")
    except Exception:
        return None


def _archive_rms_rows(cam_code: str, date: str) -> list[dict]:
    """A night's archived detection rows, cached and SSHFS-bounded."""
    key = (cam_code, date)
    now = time.monotonic()
    # A re-uploaded FTPdetectinfo (reprocess with a new platepar) bumps the
    # night's generation (cache_store.invalidate_rms_night), which retires
    # every worker's copy at once instead of after the 6 h TTL.
    gen = rms_night_generation(cam_code, date)
    with _rms_archive_lock:
        hit = _rms_archive_cache.get(key)
    if hit and now - hit[0] < hit[2] and hit[3] == gen:
        return hit[1]
    disk_key = f"{cam_code}_{date}"
    rows = _disk_cache_read("rms_detections", disk_key, _RMS_ARCHIVE_TTL_S)
    if rows is None:
        rows = _with_sshfs_timeout(
            lambda: archive_rms_detections(cam_code, date), timeout=8.0, default=None,
        )
        if rows is None:
            # Box unresponsive: serve what we last had, cache nothing.
            return hit[1] if hit else []
        # A re-upload that landed during the parse may have been read half
        # old: keep it out of the shared disk copy.
        if rows and rms_night_generation(cam_code, date) == gen:
            _disk_cache_write("rms_detections", disk_key, rows)
    ttl = _RMS_ARCHIVE_TTL_S if rows else _RMS_ARCHIVE_EMPTY_TTL_S
    with _rms_archive_lock:
        _rms_archive_cache[key] = (now, rows, ttl, gen)
    return rows


def _latest_rms_night(archive_idx, camera: str) -> tuple[str, frozenset[str]] | None:
    """``archive_idx.latest_rms_night(camera)`` if the index provides it.

    Tolerates an index without the accessor (test doubles, an older index
    pickled by a previous version) by reporting "unknown" rather than raising:
    the callers then behave exactly as they did before it existed.
    """
    accessor = getattr(archive_idx, "latest_rms_night", None)
    if not callable(accessor):
        return None
    try:
        return accessor(camera)
    except Exception:  # pragma: no cover - defensive
        logger.debug("latest_rms_night failed for %s", camera, exc_info=True)
        return None


def resolve_rms_plot_status(archive_idx, camera: str, suffix: str, now: datetime | None = None) -> dict:
    """Resolve "latest archived RMS plot matching *suffix*" for a camera,
    archive-index only (no station round-trip). Shared by the FF-intervals
    and astrometry-residual (calibration_variation.png) endpoints. Plain
    function (not a Flask view) so it's unit-testable without a request
    context.

    ``expected_date`` is the night whose processing should be DONE by now --
    always ``now - 1 day``, never ``now`` itself. A session that starts on
    the evening of day D is bucketed as D (station_api.py's night-date
    convention: frames captured after local midnight, hour<12 UTC, belong to
    the PREVIOUS calendar day's bucket) and finishes near dawn of D+1;
    ApplyRecalibrate/dawn_process only write the recalibration JSON and the
    plots for bucket D once that session ends, i.e. sometime during D+1.
    So at any point during D+1 -- morning OR afternoon OR evening -- the
    most recently completed, fully processed night is still D = (D+1) - 1.
    "Today" (``now`` unmodified) is never the right primary target: even
    late in the evening, tonight's session has at most just started
    capturing and won't be processed until tomorrow's dawn. (An earlier
    version branched on ``now.hour < 12`` and used ``now`` unmodified once
    past 12:00 UTC -- copied from station_api.py's ``api_calibration_summary``,
    which has the identical bug. That meant every camera showed "no data"
    for FF intervals/astrometry residual/drift all afternoon and evening,
    every day, since it was always asking for a night that hadn't been
    captured yet, let alone processed.)

    ``overdue`` flags when expected_date's plot still hasn't shown up 6h
    past its approximate night-end (midnight UTC the day after it
    started) -- dawn processing normally finishes hours before then, so a
    still-missing plot at that point is a real calibration-health signal
    worth surfacing, not something to walk past silently."""
    now = now or datetime.now(timezone.utc)
    expected_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    overdue = upload_state.is_overdue(expected_date, "plot", now)

    filename = archive_idx.rms_plot_file(camera, expected_date, suffix)
    if filename:
        return {"status": "ok", "date": expected_date,
                "expected_date": expected_date, "filename": filename,
                "overdue": False}

    # Not archived yet for the expected night -- fall back to the newest
    # older night that does have one. Archive-local lookups only, never a live
    # call to the camera. Nights the index holds in memory are free; a night
    # it does not is an on-demand SSHFS scan (guarded, 5 s), and the walk is
    # allowed at most _RMS_PLOT_WALK_ONDEMAND of those: the calibration page
    # renders every camera at once, and 45 cameras x 30 guarded scans for the
    # cameras that had stopped uploading saturated the SSHFS permits fleet-wide
    # and 504ed the page.
    cached_lookup = getattr(archive_idx, "rms_files_cached", None)
    ondemand_left = _RMS_PLOT_WALK_ONDEMAND if callable(cached_lookup) else 30
    for candidate in archive_idx.nights(camera)[:30]:
        if candidate > expected_date:
            continue
        cached = cached_lookup(camera, candidate) if callable(cached_lookup) else None
        if cached is not None:
            filename = next((f for f in cached if f.endswith(suffix)), None)
        else:
            if ondemand_left <= 0:
                continue
            ondemand_left -= 1
            filename = archive_idx.rms_plot_file(camera, candidate, suffix)
        if filename:
            return {"status": "ok", "date": candidate,
                    "expected_date": expected_date, "filename": filename,
                    "overdue": overdue}

    # Last resort: the archive index's resolved newest plot-bearing night.
    # Everything above reads the in-memory index, which only holds the last
    # few days -- so a camera dark for longer than that has NO candidate the
    # walk can inspect and used to land on "no data" while its plots sat in
    # the archive. The index resolves this one in its background writers, so
    # reading it here costs nothing.
    resolved = _latest_rms_night(archive_idx, camera)
    if resolved and resolved[0] <= expected_date:
        filename = next((f for f in resolved[1] if f.endswith(suffix)), None)
        if filename:
            return {"status": "ok", "date": resolved[0],
                    "expected_date": expected_date, "filename": filename,
                    "overdue": overdue if resolved[0] != expected_date else False}

    return {"status": "no_data", "date": None,
            "expected_date": expected_date, "filename": None,
            "overdue": overdue}


def resolve_ff_intervals_status(archive_idx, camera: str, now: datetime | None = None) -> dict:
    return resolve_rms_plot_status(archive_idx, camera, "_ff_intervals.png", now)


# Plots the Calibration page renders per camera, as URL segment -> archive
# filename suffix. An allow-list rather than a free-form suffix parameter:
# the segment reaches an archive glob, and the set of plots the page shows is
# a product decision, not a caller's.
CALIBRATION_PLOT_SUFFIXES: dict[str, str] = {
    "ff_intervals": "_ff_intervals.png",
    "astrometry_residual": "_calibration_variation.png",
    "astrometry_report": "_calib_report_astrometry.jpg",
}


# Filename suffix -> operator-facing label, mirroring station_api.py's
# ``_RMS_PLOT_LABELS``. Duplicated rather than imported because the station and
# the dashboard are separate deployments with no shared package; the pairing is
# pinned by a contract test so the two can't drift into disagreeing labels.
# Order is display order.
RMS_PLOT_LABELS: dict[str, str] = {
    "_DETECTED_thumbs.jpg":         "Detected thumbnails",
    "_CAPTURED_thumbs.jpg":         "Captured thumbnails",
    "_captured_stack.jpg":          "Captured stack",
    "_calib_report_astrometry.jpg": "Astrometry calibration",
    "_calib_report_photometry.png": "Photometry calibration",
    "_calibration_variation.png":   "Calibration variation",
    "_photometry_variation.png":    "Photometry variation",
    "_fieldsums.png":               "Field sums",
    "_fieldsums_noavg.png":         "Field sums (no avg)",
    "_observing_periods.png":       "Observing periods",
    "_ff_intervals.png":            "FF intervals",
    "_radiants.png":                "Radiants",
}
_RMS_PLOT_ORDER = list(RMS_PLOT_LABELS)

# How far back the walk will look for a night that actually has plots. A
# camera quiet for longer than this is reported as no_data rather than
# resurrecting something from months ago and calling it "latest".
_RMS_PLOT_MAX_LOOKBACK_NIGHTS = 30
# Uncached (SSHFS) lookups resolve_rms_plot_status may spend walking back.
_RMS_PLOT_WALK_ONDEMAND = 1


def label_rms_plots(filenames) -> list[dict]:
    """Map archive filenames to ``{filename, label, order}``, display-ordered.

    Same classification the station applies in ``api_rms_plots``, including
    its ``*_stack_*.jpg`` -> "Meteor stack" special case. Unrecognised files
    are dropped: the archive holds non-plot artefacts alongside the plots.
    """
    out: list[dict] = []
    for name in filenames:
        label = None
        order = 999
        for i, suffix in enumerate(_RMS_PLOT_ORDER):
            if name.endswith(suffix):
                label = RMS_PLOT_LABELS[suffix]
                order = i
                break
        if label is None and "_stack_" in name and name.endswith(".jpg"):
            label, order = "Meteor stack", 2
        if label is None:
            continue
        out.append({"filename": name, "label": label, "order": order})
    out.sort(key=lambda p: (p["order"], p["filename"]))
    return out


def resolve_latest_rms_plots(
    archive_idx, camera: str, now: datetime | None = None,
) -> dict:
    """Newest archived night that actually carries RMS plots for *camera*.

    The plot surfaces used to ask the STATION for one specific night, so a
    night that failed to process rendered as a blank card even though last
    week's plots sat in the archive -- and an offline station rendered blank
    for every night. The station page did walk back, but one live round-trip
    per night, and it displayed whatever it found with no indication of age:
    a three-week-old plot looked exactly like last night's.

    This resolves the same way the calibration endpoints already do -- newest
    night at or before ``expected_date`` that has plots -- and reports
    ``stale``/``overdue`` so the UI can colour it, which is the invariant the
    surfaces are supposed to hold: always show the last plot, marked if old.

    SSHFS budget: at most ONE uncached lookup per call, for ``expected_date``.
    The walk back over older nights reads the in-memory index only
    (``rms_files_cached``), because this runs once per camera and a page
    renders every camera at once -- a walk that could hit the storage box per
    night is exactly the fan-out that saturates the concurrency guard.
    """
    now = now or datetime.now(timezone.utc)
    expected_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    overdue = upload_state.is_overdue(expected_date, "plot", now)

    def _result(date: str | None, plots: list[dict]) -> dict:
        return {
            "status": "ok" if plots else "no_data",
            "date": date if plots else None,
            "expected_date": expected_date,
            "stale": bool(plots) and date != expected_date,
            "overdue": overdue if date != expected_date else False,
            "plots": plots,
        }

    # The one lookup allowed to reach the storage box.
    ni = archive_idx.night_files(camera, expected_date)
    if ni is not None:
        plots = label_rms_plots(ni.rms_files)
        if plots:
            return _result(expected_date, plots)

    for candidate in archive_idx.nights(camera)[:_RMS_PLOT_MAX_LOOKBACK_NIGHTS]:
        if candidate >= expected_date:
            continue
        cached = archive_idx.rms_files_cached(camera, candidate)
        if not cached:
            continue
        plots = label_rms_plots(cached)
        if plots:
            return _result(candidate, plots)

    # Same last resort as resolve_rms_plot_status: for a camera dark longer
    # than the index's hot window, the resolved newest plot-bearing night is
    # the only thing standing between the surfaces and a blank card.
    resolved = _latest_rms_night(archive_idx, camera)
    if resolved and resolved[0] < expected_date:
        plots = label_rms_plots(resolved[1])
        if plots:
            return _result(resolved[0], plots)

    return _result(None, [])


def resolve_rms_plots_for_night(
    archive_idx, camera: str, date: str, now: datetime | None = None,
) -> dict:
    """Archived RMS plots for one chosen night, same shape as
    ``resolve_latest_rms_plots``.

    Backs the Final Data Products date filter: the operator names the night,
    so there is no walk and nothing is "stale" -- an empty night is reported
    as ``no_data`` for that date rather than replaced by a neighbour. One
    archive lookup, the same budget as the latest resolver's first probe.
    """
    now = now or datetime.now(timezone.utc)
    expected_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    ni = archive_idx.night_files(camera, date)
    plots = label_rms_plots(ni.rms_files) if ni is not None else []
    return {
        "status": "ok" if plots else "no_data",
        "date": date if plots else None,
        "requested_date": date,
        "expected_date": expected_date,
        "stale": False,
        "overdue": False,
        "plots": plots,
    }


# Nights outside the index's memory that one colour-stack resolution may stat
# (two paths each). Only meteor nights are candidates, so the walk reaches the
# newest one without paying for cloudy nights in between.
_COLOR_STACK_WALK_IO = 3
_COLOR_STACK_MAX_LOOKBACK_NIGHTS = 30


def resolve_latest_color_stack(
    archive_idx, camera: str, now: datetime | None = None,
) -> dict:
    """Newest archived night that carries a colour meteor stack for *camera*.

    Backs the station page's colour-stack card, which used to walk the
    station's RMS nights and ask the station for each night's plot list (plus
    a colour-stack HEAD) until one had a stack -- a serial chain of station
    round-trips per camera on every Final Data Products load, and nothing at
    all while the station was offline.

    Only meteor nights (``nights_with_meteors``) can have a stack. A night in
    the index's hot window is decided from its in-memory ``stacks/`` listing,
    since nothing prunes a night that young. Older nights are not listed in
    memory, and their stack may survive only as the ``fdp/`` copy after the
    ``stacks/`` prune, so they cost a stat of both paths -- at most
    ``_COLOR_STACK_WALK_IO`` of them per call.
    """
    now = now or datetime.now(timezone.utc)
    expected_date = (now - timedelta(days=1)).strftime("%Y%m%d")
    hot_cutoff = (now - timedelta(days=_ARCHIVE_HOT_DAYS)).strftime("%Y%m%d")

    def _result(date: str | None) -> dict:
        return {
            "status": "ok" if date else "no_data",
            "date": date,
            "expected_date": expected_date,
            "stale": bool(date) and date != expected_date,
            "filename": COLOR_METEOR_STACK_FILENAME if date else None,
        }

    io_left = _COLOR_STACK_WALK_IO
    candidates = [
        d for d in archive_idx.nights_with_meteors(camera) if d <= expected_date
    ][:_COLOR_STACK_MAX_LOOKBACK_NIGHTS]
    for date in candidates:
        name = fdp_product_filename("color_meteor_stack", camera, date)
        ni = archive_idx.night_files_cached(camera, date)
        if ni is not None and name in ni.stack_files:
            return _result(date)
        if ni is not None and date >= hot_cutoff:
            continue  # fully listed and too young to have been pruned
        if io_left <= 0:
            continue
        io_left -= 1
        for path in fdp_archive_candidates(ARCHIVE_PATH, "color_meteor_stack", camera, date):
            if _with_sshfs_timeout(path.is_file, timeout=5.0, default=False):
                return _result(date)
    return _result(None)


# ── Route helpers ───────────────────────────────────────────────────────

def _validate_archive_path(*parts: str) -> None:
    for p in parts:
        if ".." in p or "/" in p:
            abort(400)


def _prefer_details_variant(name: str, meteor_names: set[str]) -> str:
    """Return the starprint details filename when the night's meteors/
    listing has it, else *name* unchanged.

    For a base meteor clip ``X_color.mkv`` (or its ``.mp4`` faststart) the
    details variant is ``X_color.details.mkv``. When that sibling is present
    in the archive, hand the frontend the details name so it requests it: the
    existing hot-cache / ``_serve_archive_clip`` path then serves the
    faststart ``.details.mp4`` (or remuxes the ``.details.mkv``) unchanged.
    Only ``_color.mkv``/``_color.mp4`` base names have a variant; anything
    else is returned as-is.

    The archive index's listing answers this, never the Storage Box: a stat
    per clip (two for a missing sibling, ~43 ms each over SSHFS) made
    nights_full for RO000H (Ciocârlia; 226 nights, 7,596 clips) take over
    ten minutes, past its 60 s cap, so the Archive tab came up empty.
    """
    if name.endswith("_color.mkv"):
        sibling = name[: -len(".mkv")] + ".details.mkv"
    elif name.endswith("_color.mp4"):
        sibling = name[: -len(".mp4")] + ".details.mkv"
    else:
        return name
    return sibling if sibling in meteor_names else name


def _serve_branded_stack(camera: str, date: str, filename: str, path: Path):
    """Return branded WebP bytes for a per-detection stack (or ``_STACK_MISS``).

    Cache hit -> cached branded bytes. Miss -> fetch the station's overlay
    config, render the overlay on demand via ``stack_overlay``, cache and
    return it; a stack that is already branded (or whose station has the
    overlay off) is returned as the archive holds it and not copied. A
    render/read failure returns ``_STACK_MISS`` so the caller refuses to
    serve the un-branded original.
    """
    config = _deps().config
    tunnels = _deps().tunnels
    cached = _branded_stack_cache.get(camera, date, filename)
    if cached is not None:
        data = _with_sshfs_timeout(cached.read_bytes, timeout=10.0, default=None)
        if data:
            return data

    owners = _host_keys_for_camera(config, camera)
    host_key = owners[0] if owners else camera
    overlay_cfg, station_cfg = stack_overlay.fetch_overlay_config(
        config, tunnels, host_key, camera
    )
    branded, rendered = stack_overlay.overlaid_stack_bytes(
        path, filename, overlay_cfg, camera, station_cfg
    )
    if branded is None:
        return _STACK_MISS
    if rendered:
        _branded_stack_cache.put(camera, date, filename, branded)
    return branded


# ── Archive endpoints (read from /srv/rovimen/archive/) ────────────

def api_archive_cameras():
    config = _deps().config
    archive_idx = _deps().archive_idx
    cameras = archive_idx.cameras()
    if _session_role() != "admin":
        cameras = [c for c in cameras if _has_camera_access(config, c)]
    return jsonify(cameras)


def api_archive_nights(camera: str):
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(camera)
    if not re.match(r"^[A-Z0-9]+$", camera, re.IGNORECASE):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    nights = archive_idx.nights(camera)
    return jsonify(_drop_future_dates(nights))


def api_archive_meteors(camera: str, date: str):
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(camera, date)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    ni = archive_idx.night_files(camera, date)
    if ni is None:
        return jsonify([])
    meteor_names = set(ni.meteor_files)
    results = []
    # One entry per clip: starprint details variants ('<stem>_color.details.mkv'
    # and its faststart '.details.mp4') carry the per-meteor row bar and are
    # served in place of their base clip, and the faststart '_color.mp4'
    # copy is listed only when its '.mkv' is gone.
    for name in listed_clip_names(ni.meteor_files):
        stack_name = stack_name_for_clip(name)
        if stack_name in meteor_names:
            stack, stack_subdir = stack_name, "meteors"
        elif stack_name in ni.stack_files:
            stack, stack_subdir = stack_name, "stacks"
        else:
            stack, stack_subdir = None, None
        results.append({
            "filename": _prefer_details_variant(name, meteor_names),
            "size_mb": None,
            "stack": stack,
            "stack_subdir": stack_subdir,
        })
    return jsonify(results)


def api_archive_indexed(camera: str, date: str):
    """Return set of filenames stored in the archive for a camera/date."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(camera, date)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    return jsonify(sorted(archive_idx.all_night_files(camera, date)))


@require_station
def api_archive_rms_detections(camera: str, date: str):
    """Return a night's archived detection rows (see _archive_rms_rows)."""
    config = _deps().config
    _validate_archive_path(camera, date)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)

    return _json_cached({
        "camera": camera,
        "date": date,
        # Copies: attach_trajectories writes into the rows, which are shared.
        "detections": gmn_data.attach_trajectories(
            camera, [dict(r) for r in _archive_rms_rows(camera, date)]
        ),
    }, max_age=300)


def api_calibration_ff_intervals(camera: str):
    """Resolve the newest archived FF-intervals plot for a camera --
    entirely from the local archive index, no station round-trip.

    Replaces an earlier design that walked /api/videodb/rmsnights (a live,
    uncached station proxy) plus up to 5 /api/rms/plots calls per camera:
    fleet-wide that fanned out 40+ guaranteed live station RTTs on every
    Calibration page load, each able to hold a worker for the full 10s
    station_get_raw timeout. This endpoint instead reads _ArchiveIndex,
    which archive_upload.py's _find_rms_files keeps stocked with each
    night's *_ff_intervals.png as a side effect of its existing (already
    running, already night-by-night deduped) upload sweep."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    return jsonify(resolve_ff_intervals_status(archive_idx, camera))


def api_calibration_plot(kind: str, camera: str):
    """Resolve the newest archived plot of *kind* for a camera.

    Generalises the two single-purpose routes below it (which stay as
    they are, being what the archive-resolution tests and older clients
    call) so the Calibration page can add a plot by naming it in
    ``CALIBRATION_PLOT_SUFFIXES`` instead of growing another route each
    time. Same archive-index-only resolution, same no-station-round-trip
    guarantee."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    suffix = CALIBRATION_PLOT_SUFFIXES.get(kind)
    if suffix is None:
        abort(404)
    _validate_archive_path(camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    return jsonify(resolve_rms_plot_status(archive_idx, camera, suffix))


@public_route(page="overview")
def api_rms_plots_latest(host_key: str, camera: str):
    """Newest archived night with RMS plots for a camera, plus staleness.

    Replaces the per-night fan-out the plot surfaces used to do. The
    overview asked the STATION for one specific night and rendered a blank
    card when that night had nothing; the station page walked back but with
    a live round-trip per night and no indication of how old the result
    was. Both broke whenever the station was offline. This answers the
    whole question in one archive-local call and reports ``stale`` /
    ``overdue`` so the card can be coloured.

    Public under the same "overview" toggle as ``/api/rms/plots``: the
    payload is filename / label / order plus dates, no ip or host paths,
    and a non-public station or its camera 404s before any lookup.
    """
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(host_key, camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if host_key not in config.stations:
        abort(404)
    # Unknown/private stations fail closed for anonymous callers, matching
    # station_is_public_for_request's contract on the sibling plots route.
    # The archive is read by camera, so the camera must be public too.
    if not (station_is_public_for_request(config, host_key)
            and camera_is_public_for_request(config, camera)):
        abort(404)
    # ``?date=YYYYMMDD``: the Final Data Products date filter asks for one
    # night instead of the newest. Same gate, same payload shape.
    date = request.args.get("date")
    if date is not None:
        if not re.match(r"^\d{8}$", date):
            abort(400)
        return _json_cached(
            resolve_rms_plots_for_night(archive_idx, camera, date),
            max_age=300,
        )
    # Short cache: the answer only changes when a night finishes
    # processing, but the surfaces poll it per camera on every render.
    return _json_cached(
        resolve_latest_rms_plots(archive_idx, camera), max_age=300,
    )


# Bound on one batch so a hand-written URL can't fan the resolver out
# across the whole archive; the fleet has about 45 cameras.
_PLOTS_LATEST_BATCH_MAX = 200
_BATCH_CAM_RE = re.compile(r"^[A-Z0-9]+$")


@public_route(page="overview")
def api_rms_plots_latest_batch():
    """``plots-latest`` for many cameras in one call:
    ``?cams=<host>/<CAM>,<host>/<CAM>,...`` -> ``{"<host>/<CAM>": payload}``.

    The overview map asked ``/api/rms/plots-latest/<host>/<cam>`` once per
    camera on every visit (about 40 requests). Same gates as the single
    route: pairs for unknown or (to anonymous callers) non-public stations,
    or with a malformed camera code, are left out of the answer rather
    than failing the whole batch.
    """
    config = _deps().config
    archive_idx = _deps().archive_idx
    out: dict[str, Any] = {}
    pairs = [p for p in request.args.get("cams", "").split(",") if p]
    for pair in pairs[:_PLOTS_LATEST_BATCH_MAX]:
        host_key, _, camera = pair.partition("/")
        if (not camera or ".." in pair or "/" in camera
                or not _BATCH_CAM_RE.match(camera)
                or host_key not in config.stations
                or not station_is_public_for_request(config, host_key)
                or not camera_is_public_for_request(config, camera)):
            continue
        out[pair] = resolve_latest_rms_plots(archive_idx, camera)
    return _json_cached(out, max_age=300)


@public_route(page="overview")
def api_rms_color_stack_latest(host_key: str, camera: str):
    """Newest archived night with a colour meteor stack for a camera (see
    ``resolve_latest_color_stack``). Same gate as ``plots-latest``: a
    non-public station or its camera 404s for anonymous callers before any lookup."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(host_key, camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if host_key not in config.stations:
        abort(404)
    if not (station_is_public_for_request(config, host_key)
            and camera_is_public_for_request(config, camera)):
        abort(404)
    return _json_cached(
        resolve_latest_color_stack(archive_idx, camera), max_age=300,
    )


def api_calibration_astrometry_residual(camera: str):
    """Resolve the newest archived astrometry-residual (calibration
    variation) plot for a camera -- same archive-only resolution as
    api_calibration_ff_intervals above, just a different plot suffix."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    _validate_archive_path(camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    return jsonify(resolve_rms_plot_status(archive_idx, camera, "_calibration_variation.png"))


def api_archive_file(camera: str, date: str, subdir: str, filename: str):
    config = _deps().config
    prefetch_archive_thumbs = _deps().prefetch_archive_thumbs
    _validate_archive_path(camera, date, subdir, filename)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if subdir not in ("meteors", "timelapse", "rms", "stacks", "ffthumbs",
                      "spectro", FDP_SUBDIR):
        abort(400)
    # jpg: the spectro pipeline's per-event preview frames. They are
    # photographic and one per event, so JPEG rather than the lossless PNG
    # the reduced-spectrum plot uses.
    if not re.match(r"^[\w._-]+\.(mkv|mp4|webp|txt|json|png|csv|jpg)$", filename):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)

    # Playback of an archived clip asks for ?format=mp4: Safari and iOS
    # cannot play MKV, and this route used to ignore the param. Same
    # tiers as /api/cached-video (hot cache, archived .mp4 sibling,
    # persisted remux). A download (?download=1) keeps the original.
    if (subdir == "meteors" and filename.endswith(".mkv")
            and request.args.get("format") == "mp4"
            and not request.args.get("download")):
        hot = clip_cache.serve_hot_mp4(camera, date, filename)
        if hot is not None:
            return hot
        served = _serve_archive_clip(camera, date, filename, want_mp4=True)
        if served is not None:
            return served
        abort(404)

    is_webp = filename.endswith(".webp")

    if is_webp:
        owners = _host_keys_for_camera(config, camera)
        if owners:
            host_key = owners[0]
            cache_file = _thumb_cache_path(host_key, camera, date, filename)
            # A 0-byte cache file is a MISS, not a hit: serving it (X-Accel
            # or send_file) would return an empty, 30-day-immutable image —
            # a permanently black thumbnail. Fall through to re-fetch instead.
            if _is_nonempty_file(cache_file):
                if NGINX_ACCEL:
                    accel = "/internal/thumb_cache/" + "/".join(
                        [host_key, camera, date, filename]
                    )
                    return Response(headers={
                        "X-Accel-Redirect": accel,
                        "Content-Type": "image/webp",
                        "Cache-Control": "public, max-age=2592000, immutable",
                    })
                resp = send_file(cache_file, mimetype="image/webp",
                                 conditional=True)
                resp.headers["Cache-Control"] = (
                    "public, max-age=2592000, immutable"
                )
                return resp
            if prefetch_archive_thumbs:
                threading.Thread(
                    target=prefetch_archive_thumbs,
                    args=(host_key, camera, date),
                    daemon=True,
                ).start()

    path = ARCHIVE_PATH / camera / date / subdir / filename
    # One cached directory listing answers a whole gallery, so a night's
    # worth of <img> requests no longer takes a lane permit each (see
    # _archive_dir_names). Only a name the listing does not carry pays for
    # its own stat, and that stat is guarded so a stalled storagebox mount
    # can't pin the worker thread on .exists(): a timeout returns the
    # sentinel and we report 503 (mount stalled) rather than masking it as
    # a 404.
    _MISS = object()
    names = _archive_dir_names(camera, date, subdir)
    exists: object = True
    if names is None or filename not in names:
        exists = _with_sshfs_timeout(path.exists, timeout=5.0, default=_MISS)
    if exists is _MISS:
        abort(503)
    if not exists:
        abort(404)
    dl = request.args.get("download")

    # Overlay gate: a downloadable per-detection stack must carry the
    # ROVIMEN/astromania branding+data strip, same as clips and night
    # stacks. Stacks produced after the station overlay landed are already
    # branded (marker passes through untouched); legacy un-branded stacks
    # are rendered on demand here and never served raw.
    if _is_per_detection_stack(subdir, filename):
        branded = _serve_branded_stack(camera, date, filename, path)
        if branded is _STACK_MISS:
            abort(503)
        resp = Response(branded, mimetype="image/webp")
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
        if dl:
            resp.headers["Content-Disposition"] = (
                f'attachment; filename="{filename}"'
            )
        return resp

    # rms/ files are replaced in place when a night is reprocessed (new
    # platepar): revalidate every time (ETag carries the file's mtime and
    # size) rather than trusting the app-wide 7-day max-age.
    resp = send_file(path, conditional=True, as_attachment=bool(dl),
                     download_name=filename if dl else None,
                     max_age=0 if subdir == "rms" else None)
    if is_webp:
        resp.headers["Cache-Control"] = "public, max-age=2592000, immutable"
    return resp


# ── nights_full (per-camera deep walk) ───────────────────────────────

def _build_nights_full(camera: str) -> list:
    """Build the per-night payload from the archive index.

    Both directory listings and state.json data come from the
    archive index -- no per-clip SSHFS I/O at request time.
    """
    archive_idx = _deps().archive_idx
    nights = _drop_future_dates(archive_idx.nights(camera))
    if not nights:
        return []
    result = []
    for date in nights:
        ni = archive_idx.night_files_for_listing(camera, date)
        if ni is None:
            continue
        night_state_chunks: dict = ni.state_chunks or {}
        meteor_names = set(ni.meteor_files)
        meteors_entries: list[dict] = []
        # One card per clip: details variants are swapped in for their base
        # clip (below) and the faststart '_color.mp4' copy is listed only
        # when its '.mkv' is gone (it used to show every recent clip twice).
        for name in listed_clip_names(ni.meteor_files):
            stack_name = stack_name_for_clip(name)
            if stack_name in meteor_names:
                stack, stack_subdir = stack_name, "meteors"
            elif stack_name in ni.stack_files:
                stack, stack_subdir = stack_name, "stacks"
            else:
                stack, stack_subdir = None, None
            # Lock/offset metadata is keyed by the BASE clip name; resolve it
            # before swapping in the details variant filename below.
            mt_str = None
            det_offset_s = None
            lock = night_state_chunks.get(clip_base(name), {}).get("lock")
            if lock:
                mt_str = lock.get("meteor_time")
                det_time_str = lock.get("detection_time")
                det_offset_s = compute_detection_offset(name, mt_str, det_time_str)
            meteors_entries.append({
                "filename": _prefer_details_variant(name, meteor_names),
                "stack": stack,
                "stack_subdir": stack_subdir,
                "detection_offset_s": det_offset_s,
                "meteor_time": mt_str,
            })
        timelapse = None
        timelapse_stack = None
        for fname in ni.timelapse_files:
            if fname.endswith(".mp4") and timelapse is None:
                timelapse = fname
            elif fname.endswith("_night_stack.webp") and timelapse_stack is None:
                timelapse_stack = fname
        result.append({"date": date, "meteors": meteors_entries,
                       "timelapse": timelapse, "timelapse_size_mb": None,
                       "timelapse_stack": timelapse_stack})
    return result


def refresh_nights_full(camera: str) -> None:
    """Run the deep walk and update both the in-memory and disk caches.
    Used by the live endpoint on stale-revalidate and by the startup
    pre-warm in rovimen_dashboard.py. Idempotent -- coalesced by
    `_kick_cache_refresh`."""
    nights_full_cache = _deps().nights_full_cache
    fresh = _with_sshfs_timeout(
        lambda: _build_nights_full(camera), timeout=60.0, default=None,
    )
    if fresh is not None:
        nights_full_cache[camera] = (time.monotonic(), fresh)
        _disk_cache_write("nights_full", camera, fresh)


def _nights_hidden_view(nights: list, camera: str) -> list:
    """The camera's archive nights as this request lists them: meteor clips
    a user hid as false positives (``hidden_detections``) left out, or kept
    and marked when a signed-in caller asks ``show_hidden=1``.

    Applied after the nights_full cache, never inside it, so a hide shows on
    the next request of every worker. The response stays a list (its
    consumer and the disk cache keep their shape); a night with hidden clips
    carries ``hidden_count``, the Archive tab's "Show hidden (N)". Clips are
    matched by camera and meteor time (the lock's ``meteor_time``); a clip
    without one is never hidden. The cached night dicts are never mutated."""
    snap = hidden_detections.snapshot()
    if not snap:
        return nights
    show = hidden_detections.wants_hidden(request.args.get("show_hidden"), is_anonymous())
    out: list = []
    for night in nights:
        meteors = (night.get("meteors") or []) if isinstance(night, dict) else []
        n_hidden = hidden_detections.count(meteors, snap, cam=camera)
        if not n_hidden:
            out.append(night)
            continue
        out.append({
            **night,
            "meteors": hidden_detections.apply(meteors, snap, show, cam=camera),
            "hidden_count": n_hidden,
        })
    return out


def _nights_max_age(anon_max_age: int) -> int:
    """A signed-in browser revalidates every time (a 304 when nothing
    changed), so a hide by another user shows on its next load."""
    return anon_max_age if is_anonymous() else 0


def api_archive_nights_full(camera: str):
    """All nights for a camera with meteors + timelapse in one call.

    Cache strategy: stale-while-revalidate. A request never blocks on
    SSHFS as long as we have *any* prior data for this camera --
    disk-persisted entries from the last process count. The user
    sees the previous result instantly and the next request within
    a few seconds picks up the freshly-walked one.

    Meteor clips hidden as false positives are left out after the cache
    (``_nights_hidden_view``; ``show_hidden=1`` for signed-in callers).
    """
    config = _deps().config
    nights_full_cache = _deps().nights_full_cache
    nights_full_ttl = _deps().nights_full_ttl
    _validate_archive_path(camera)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)

    cached = nights_full_cache.get(camera)
    now = time.monotonic()

    if cached and (now - cached[0]) < nights_full_ttl:
        return _json_cached(_nights_hidden_view(cached[1], camera), max_age=_nights_max_age(120))

    if cached is not None:
        _kick_cache_refresh("nights_full", camera,
                            lambda c=camera: refresh_nights_full(c))
        return _json_cached(_nights_hidden_view(cached[1], camera), max_age=_nights_max_age(15))

    # Truly cold (no in-memory and no disk-loaded entry). Block once
    # for a fresh build -- subsequent requests hit the cache. 60 s
    # SSHFS ceiling prevents a hung mount from freezing the worker.
    result = _with_sshfs_timeout(
        lambda: _build_nights_full(camera), timeout=60.0, default=None,
    )
    if result is None:
        return _json_cached([], max_age=15)
    nights_full_cache[camera] = (time.monotonic(), result)
    _disk_cache_write("nights_full", camera, result)
    return _json_cached(_nights_hidden_view(result, camera), max_age=_nights_max_age(120))


# ── On-demand video caching ───────────────────────────────────────────

@public_route(page="events")
def api_cached_video(host_key: str, station_code: str, date: str, filename: str):
    """Serve cached video instantly, or stream from station while caching.

    Query params:
        format=mp4  Convert MKV to MP4 on the fly via ffmpeg.

    Anonymous public visitors are NOT served through the gated station
    proxy (which needs a station tunnel + a login-scoped cache). Instead,
    for a ``public: true`` station's clip we 302-redirect to the keyless
    public media surface (``/media/v1/clip/...``, served from the storage
    box archive). The media handler enforces the same per-station public
    flag and returns a clean 404 when the clip was never uploaded — which
    the video modal renders as a tidy "clip not available" state rather
    than a dead player. A clip belonging to a ``public: false`` station is
    404'd here before any redirect so its existence stays undisclosed.
    Logged-in operators keep the full station-proxy path below.
    """
    config = _deps().config
    tunnels = _deps().tunnels
    # Detection payloads may name either the .mkv or its .mp4 sibling
    # (push stations upload both); a requested .mp4 name IS an mp4
    # request regardless of the format param.
    want_mp4 = request.args.get("format") == "mp4" or filename.endswith(".mp4")
    if is_anonymous():
        # Fail-closed: only clips from opted-in public stations, and only
        # a locked clip name. Everything else is 404 (existence undisclosed).
        if not (station_is_public_for_request(config, host_key)
                and camera_is_public_for_request(config, station_code)):
            abort(404)
        if not re.match(r"^[\w._-]+\.(mkv|mp4)$", filename) or ".." in date or "/" in date:
            abort(404)
        iso_date = f"{date[0:4]}-{date[4:6]}-{date[6:8]}" if re.match(r"^\d{8}$", date) else date
        target = f"/media/v1/clip/{station_code}/{iso_date}/{filename}"
        if want_mp4:
            target += "?format=mp4"
        return redirect(target, code=302)
    validate_media_params(
        config, host_key, station_code, date, filename, r"^[\w._-]+\.(mkv|mp4)$"
    )
    # NVMe hot cache: pre-remuxed faststart MP4s (warmer / ingest tee /
    # earlier promote). A hit is served by nginx (X-Accel) — no station
    # round-trip, no SSHFS, no per-request ffmpeg.
    if want_mp4:
        hot = clip_cache.serve_hot_mp4(station_code, date, filename)
        if hot is not None:
            return hot
    # Storagebox archive next — locked clips land here within minutes
    # (push stations) and the box is local to the dashboard, while the
    # station path below crosses a slow home uplink.
    archived = _serve_archive_clip(station_code, date, filename, want_mp4)
    if archived is not None:
        return archived
    cache_dir = CACHE_PATH / station_code / date
    cached = cache_dir / filename
    try:
        url = media_url(config, tunnels, host_key, "color_capture", station_code, date, filename)
    except _TunnelDown:
        if cached.exists():
            if want_mp4:
                return _remux_persist_and_serve(cached, station_code, date, filename)
            return send_file(cached, conditional=True)
        return jsonify({"error": "station tunnel down", "offline": True}), 502

    if cached.exists():
        stale = False
        try:
            head = _session_for_url(url).head(url, timeout=10, allow_redirects=True)
            upstream_size = head.headers.get("Content-Length")
            if (
                head.status_code == 200
                and upstream_size is not None
                and int(upstream_size) != cached.stat().st_size
            ):
                stale = True
                logger.info(
                    "Cache invalidated for %s/%s/%s: upstream=%s cached=%d",
                    station_code, date, filename, upstream_size, cached.stat().st_size,
                )
        except Exception as exc:
            logger.debug("HEAD validate failed for %s, serving cached: %s", filename, exc)
        if stale:
            try:
                cached.unlink()
            except OSError:
                pass
        else:
            if want_mp4:
                return _remux_persist_and_serve(cached, station_code, date, filename)
            return send_file(cached, conditional=True)
    # Station: a background download into the local cache, never holding
    # this request past STATION_WAIT_S (see serve_station_clip).
    resp = serve_station_clip(url, cached, want_mp4, station_code, date, filename)
    if resp.status_code == 502:
        # The station fetch failed; the clip may have landed in the
        # archive since the check at the top of the request. One last look.
        archived = _serve_archive_clip(station_code, date, filename, want_mp4)
        if archived is not None:
            return archived
    return resp


def api_cached_files(station_code: str, date: str):
    """Return list of cached filenames for a station/date."""
    config = _deps().config
    for p in (station_code, date):
        if ".." in p or "/" in p:
            abort(400)
    if not _has_camera_access(config, station_code):
        abort(403)
    cache_dir = CACHE_PATH / station_code / date
    if not cache_dir.exists():
        return jsonify([])
    return jsonify(sorted(f.name for f in cache_dir.iterdir() if f.is_file()))


# ── Cache cleanup (background) ────────────────────────────────────────

def _iterdir_or_empty(d: Path) -> list[Path]:
    try:
        return list(d.iterdir())
    except FileNotFoundError:
        return []


_CACHE_CLEANUP_INTERVAL_S = 3600


def _cache_cleanup_once() -> None:
    """Delete files older than ``CACHE_TTL_HOURS`` from
    ``CACHE_PATH/<station>/<date>/`` and the directories that leaves empty."""
    if not CACHE_PATH.exists():
        return
    cutoff = datetime.now() - timedelta(hours=CACHE_TTL_HOURS)
    # The clip cache prunes the same tree, so a file listed a moment ago may
    # be gone by the time it is stat'ed. That is not an error and must not
    # abort the whole pass (it did: one FileNotFoundError per hour left the
    # rest of the tree unpruned). Skip what vanished, keep going.
    for station_dir in CACHE_PATH.iterdir():
        if not station_dir.is_dir():
            continue
        for date_dir in _iterdir_or_empty(station_dir):
            if not date_dir.is_dir():
                continue
            for f in _iterdir_or_empty(date_dir):
                try:
                    if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                        f.unlink(missing_ok=True)
                except FileNotFoundError:
                    continue
                except OSError:
                    logger.debug("cache cleanup: could not remove %s", f, exc_info=True)
            try:
                date_dir.rmdir()
            except OSError:
                pass
        try:
            station_dir.rmdir()
        except OSError:
            pass


def cache_cleanup_loop(should_run: Callable[[], bool] = lambda: True) -> None:
    """Daemon: sweep ``CACHE_PATH`` hourly (``_cache_cleanup_once``).

    ``rovimen_dashboard._start_background_workers`` starts it in every
    gunicorn worker (never under ``ROVIMEN_DISABLE_BACKGROUND=1``), but the
    workers share ``CACHE_PATH``, so a pass runs only while ``should_run()``
    (the worker's "cache_cleanup" duty-lock check) says this worker owns the
    sweep. Checked per pass, so the duty moves to another worker when its
    owner recycles. A failed pass is logged and the next one runs an hour
    later.
    """
    while True:
        time.sleep(_CACHE_CLEANUP_INTERVAL_S)
        try:
            if should_run():
                _cache_cleanup_once()
        except Exception:
            logger.exception("Cache cleanup error")


# ── Multi-detection stitch ────────────────────────────────────────────

_STITCH_CAM_RE      = re.compile(r"^[A-Z0-9]{1,16}$")
_STITCH_DATE_RE     = re.compile(r"^\d{8}$")
_STITCH_FILENAME_RE = re.compile(r"^[A-Za-z0-9_]+\.(mkv|mp4)$")


@require_auth
def api_stitch_multi():
    """Compose multiple detection clips into a single MP4.

    Reading, not writing: any signed-in account that may view every
    camera involved can stitch. It used to sit behind ``require_station``
    with no host key, whose write gate only admins pass, so every
    operator got 403.

    Body JSON:
      videos:       list of {cam, date, filename, offset}
      layout:       "grid" | "focused"
      focused_idx:  int (index into videos, -1 = none)
      master_start: float (seconds relative to detection, typically -winPre)
      master_end:   float (seconds relative to detection, typically +winPost)

    Answers 202 with ``url``: the composite renders in the background
    into the cache, and ``GET url`` answers 202 (Retry-After) until it is
    ready, then serves the MP4 with a Content-Length. ffmpeg is killed at
    120 s.
    """
    config = _deps().config
    body = request.get_json(force=True, silent=True) or {}
    videos      = body.get("videos") or []
    layout      = body.get("layout", "grid")
    # 0 is the first clip: ``int(x or -1)`` used to turn it into "no focus".
    focused_idx = body.get("focused_idx", -1)
    if focused_idx is None:
        focused_idx = -1
    if isinstance(focused_idx, bool) or not isinstance(focused_idx, int):
        return jsonify({"error": "focused_idx must be an integer"}), 400
    master_start = float(body.get("master_start", -5))
    master_end   = float(body.get("master_end",    5))

    if not videos or len(videos) > 8:
        return jsonify({"error": "need 1–8 videos"}), 400

    # Validate and resolve each clip path
    # (path, ss, duration, head pad): the head pad is the part of the
    # window before the chunk began, shown as black so this tile stays
    # aligned with the others instead of cutting the window short.
    clips: list[tuple[Path, float, float, float]] = []
    archive_root = ARCHIVE_PATH.resolve()
    for v in videos:
        cam      = (v.get("cam") or "")
        date     = (v.get("date") or "")
        filename = (v.get("filename") or "")
        offset   = float(v.get("offset") or 0)
        if not _STITCH_CAM_RE.match(cam) \
           or not _STITCH_DATE_RE.match(date) \
           or not _STITCH_FILENAME_RE.match(filename):
            return jsonify({"error": f"invalid clip: {cam}/{date}/{filename}"}), 400
        if not _has_camera_access(config, cam):
            return jsonify({"error": f"no access to {cam}"}), 403
        clip_path = ARCHIVE_PATH / cam / date / "meteors" / filename
        try:
            if not clip_path.resolve().is_relative_to(archive_root):
                return jsonify({"error": "path escape"}), 400
        except (ValueError, OSError):
            return jsonify({"error": "path resolve failed"}), 400
        if not clip_path.exists():
            return jsonify({"error": f"not found: {cam}/{date}/{filename}"}), 404
        want_ss  = offset + master_start
        ss       = max(0.0, want_ss)
        duration = max(0.5, offset + master_end - ss)
        clips.append((clip_path, ss, duration, ss - want_ss))

    n = len(clips)
    # Every tile spans the whole requested window: a chunk that starts
    # after the window opens is padded with black at the head, one that
    # ends before it closes at the tail. Cutting every tile to the
    # shortest made a 10 s request come out 8 s (2026-10-19).
    window_s = max(0.5, master_end - master_start)

    # --- Build FFmpeg filter_complex ---
    # One clip: native resolution, only trimmed. Grid: up to 4 clips keep
    # the native 1280x848 frame per tile (2 clips 2560x848, 3-4 clips
    # 2560x1696); 5-8 clips use half-size tiles (up to 1920x1272) to stay
    # a sane size. Half-size tiles for every grid made a 4-camera
    # download 1280x848 in all. Focused: the focused clip at full width
    # over a strip of the others.
    TILE_W, TILE_H = (1280, 848) if n <= 4 else (640, 424)
    FOCUS_W, FOCUS_H = 1280, 848
    STRIP_H = 212

    def _pad(tag_in: str, w: int, h: int, tag_out: str) -> str:
        return (f"[{tag_in}]scale={w}:{h}:force_original_aspect_ratio=decrease,"
                f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1[{tag_out}]")

    def _span(i: int, head: float, tag_out: str) -> str:
        """Input *i* padded to the full window (black head and tail)."""
        return (f"[{i}:v]tpad=start_duration={head:.3f}:color=black,"
                f"tpad=stop_duration={window_s:.3f}:color=black,"
                f"trim=duration={window_s:.3f},setpts=PTS-STARTPTS[{tag_out}]")

    filter_parts: list[str] = []
    inp_args: list[str] = []
    if n == 1:
        path, ss, dur, _head = clips[0]
        inp_args = ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]
        filter_parts.append("[0:v]copy[out]")
    elif layout == "focused" and 0 <= focused_idx < n:
        strip_clips = [c for i, c in enumerate(clips) if i != focused_idx]
        ordered = [clips[focused_idx]] + strip_clips
        n_strip = len(ordered) - 1
        strip_w = FOCUS_W // n_strip
        for i, (path, ss, dur, head) in enumerate(ordered):
            inp_args += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]
            filter_parts.append(_span(i, head, f"w{i}"))
        filter_parts.append(_pad("w0", FOCUS_W, FOCUS_H, "fv"))
        for i in range(1, len(ordered)):
            filter_parts.append(_pad(f"w{i}", strip_w, STRIP_H, f"sv{i}"))
        if n_strip > 1:
            strip_inputs = "".join(f"[sv{i}]" for i in range(1, len(ordered)))
            filter_parts.append(f"{strip_inputs}hstack=inputs={n_strip}[strip]")
            filter_parts.append(_pad("strip", FOCUS_W, STRIP_H, "strip_p"))
        else:
            filter_parts.append(_pad("sv1", FOCUS_W, STRIP_H, "strip_p"))
        filter_parts.append("[fv][strip_p]vstack=shortest=1[out]")
    else:
        cols = math.ceil(math.sqrt(n))
        for i, (path, ss, dur, head) in enumerate(clips):
            inp_args += ["-ss", f"{ss:.3f}", "-t", f"{dur:.3f}", "-i", str(path)]
            filter_parts.append(_span(i, head, f"w{i}"))
            filter_parts.append(_pad(f"w{i}", TILE_W, TILE_H, f"v{i}"))
        grid_layout = "|".join(
            f"{(i % cols) * TILE_W}_{(i // cols) * TILE_H}" for i in range(n))
        tiles = "".join(f"[v{i}]" for i in range(n))
        filter_parts.append(
            f"{tiles}xstack=inputs={n}:layout={grid_layout}:fill=black:shortest=1[out]")

    filter_str = ";".join(filter_parts)

    # Cap concurrent stitches: acquire without blocking so a flood of
    # stitch POSTs returns 503 immediately instead of parking request
    # threads. The slot is held until the stream ends.
    if not _stitch_sem.acquire(blocking=False):
        return jsonify({"error": "busy", "detail": "too many stitches in progress"}), 503

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        *inp_args,
        "-filter_complex", filter_str,
        "-map", "[out]",
        # CRF 18: a composite of night-sky clips at CRF 22 came out at
        # 1.4 Mbps for 4 cameras, visibly blocky in the dark sky.
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p", "-an",
        "-movflags", "+faststart",
        "-f", "mp4", "-y",
    ]
    job = uuid.uuid4().hex
    try:
        d = _stitch_dir()
        _stitch_prune(d)
        (d / f"{job}.job").touch()
        logger.info("stitch-multi %s: %s clips layout=%s", job, n, layout)
        threading.Thread(target=_stitch_render, args=(cmd, job, d),
                         name=f"stitch-{job[:8]}", daemon=True).start()
    except Exception:
        _stitch_sem.release()
        raise
    resp = jsonify({"preparing": True, "url": f"/api/stitch-multi/{job}",
                    "retry_after_seconds": _STITCH_RETRY_AFTER_S})
    resp.status_code = 202
    resp.headers["Retry-After"] = str(_STITCH_RETRY_AFTER_S)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@require_auth
def api_stitch_multi_result(job: str):
    """A stitch job's MP4 once rendered; 202 while it runs, 500 if it
    failed, 404 for an unknown or expired job. ``?name=`` sets the saved
    file's name (letters, digits and underscores)."""
    if not _STITCH_JOB_RE.match(job):
        abort(404)
    d = _stitch_dir()
    out, err, marker = d / f"{job}.mp4", d / f"{job}.err", d / f"{job}.job"
    if out.exists():
        name = re.sub(r"[^A-Za-z0-9_]", "", request.args.get("name", ""))[:60] or "rovimen_multi"
        resp = send_file(out, mimetype="video/mp4", as_attachment=True,
                         download_name=f"{name}.mp4", conditional=True)
        resp.headers["Cache-Control"] = "no-store"
        return resp
    if err.exists():
        return jsonify({"error": "ffmpeg failed", "detail": err.read_text()[-500:]}), 500
    try:
        started = marker.stat().st_mtime
    except OSError:
        abort(404)
    if time.time() - started > _STITCH_TIMEOUT_S + 30:
        # The worker that ran it is gone (restart) without a result.
        return jsonify({"error": "stitch lost", "detail": "please try again"}), 500
    resp = jsonify({"preparing": True, "retry_after_seconds": _STITCH_RETRY_AFTER_S})
    resp.status_code = 202
    resp.headers["Retry-After"] = str(_STITCH_RETRY_AFTER_S)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@require_station
def loop_clip_archive(camera: str, date: str, filename: str):
    """Build a looped MP4 from an archived MKV on the storagebox."""
    _validate_archive_path(camera, date, "meteors", filename)
    if not re.match(r"^[\w._-]+\.mkv$", filename):
        abort(400)
    try:
        start  = float(request.args.get("start", 0))
        end    = float(request.args.get("end",   0))
        loops  = max(1, min(10, int(request.args.get("loops", 2))))
    except (ValueError, TypeError):
        abort(400)
    if end <= start:
        abort(400)
    duration = end - start

    src_path = ARCHIVE_PATH / camera / date / "meteors" / filename
    if not src_path.exists():
        return jsonify({"error": "not found"}), 404

    with tempfile.TemporaryDirectory(prefix="rovimen_stitch_") as tmpdir:
        seg_path = f"{tmpdir}/segment.mp4"
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", f"{start:.3f}", "-t", f"{duration:.3f}",
                 "-i", str(src_path), "-c:v", "libx264", "-crf", "23",
                 "-preset", "fast", "-an", "-movflags", "+faststart", seg_path],
                check=True, capture_output=True, timeout=120,
            )
        except subprocess.CalledProcessError as exc:
            logger.error("loop_clip_archive trim failed: %s", exc.stderr[-300:] if exc.stderr else "")
            abort(500)
        stem = filename.rsplit(".", 1)[0]
        return looped_clip_response(seg_path, loops, f"{stem}_loop{loops}x.mp4")


# ── Spectroscopy products ─────────────────────────────────────────────
#
# /api/spectro keeps three per-app caches on ArchiveDeps: the payload per host
# (spectro_cache), and per (cam, date) the nights that listed empty
# (spectro_miss) or listed products (spectro_hits).
_SPECTRO_CACHE_TTL = 300.0
_SPECTRO_NIGHTS = 14
# A listing that came back incomplete is cached only briefly, so the next
# page load retries instead of showing a short list for five minutes.
_SPECTRO_PARTIAL_TTL = 20.0
# Misses, (cam, date) -> expiry, for night dirs that held no spectro products.
# The dawn pipeline writes a night's products once, the morning after, and
# never revisits it, so a settled night that listed empty stays empty. Without
# this every cold build re-pays one SSHFS listing per night per camera to
# rediscover the same misses: 26 of gmnro01's 28 on 2026-09-19, each able to
# wait _SSHFS_ACQUIRE_TIMEOUT for a lane permit, which is what pushed the
# request past nginx's 30 s proxy_read_timeout and returned a 504 to the tab.
_SPECTRO_MISS_TTL = 86400.0        # settled night: hold the miss for a day
_SPECTRO_MISS_TTL_RECENT = 300.0   # recent night: its run may still be pending
_SPECTRO_MISS_RECENT_DAYS = 2
# Bounded like the other per-process caches: (cam, date) pairs would
# otherwise accumulate one entry per camera per night over deploy uptime.
_SPECTRO_MISS_MAX = 4096
# Hits, (cam, date) -> (expiry, night entry), for nights that DID list
# products, held on the same schedule as a miss: a settled night's products
# are written once by its dawn run and never change, so re-listing all 14
# nights x cameras every five minutes only competed for the six-permit
# SSHFS lane. On 2026-09-23 that walk, queued behind another tab's fan-out,
# outran nginx's 30 s proxy timeout and the tab showed "Could not list".
# They share the miss TTLs and bound above.

# Wall-clock budget for one uncached build, well inside nginx's 30 s
# proxy_read_timeout. Nights not reached in time are reported partial.
_SPECTRO_BUILD_BUDGET_S = float(os.environ.get("ROVIMEN_SPECTRO_BUILD_BUDGET_S", "18"))
# Per-night summary the station uploads alongside the artifacts. Not an
# event file: it carries no "__" so the event grouping skips it.
_INDEX_NAME = "index.json"
# Guards the prune below only: single-key reads and writes are atomic, but
# iterating the dict while another worker thread inserts raises
# "dictionary changed size during iteration" (the same trap
# _overview_stacks_cache_lock exists for).
_spectro_miss_lock = threading.Lock()


@require_station
def api_spectro(host_key: str):
    """Spectroscopy products for a station's spectro cameras.

    Lists ``<cam>/<night>/spectro/`` on the archive for the newest nights
    and groups artifacts by event id (the station uploads them as
    ``<event_id>__<name>``). Stations without a ``spectro: true`` camera
    answer ``{"spectro": false}`` so the Final Data Products tab knows not
    to render the section. The spectro subdir is not in the archive
    index's night scan, so listings go through the shed-protected SSHFS
    wrapper and are cached per station, in the shared tier as well as
    per-process.

    A night whose listing did not come back is reported as ``partial:
    true`` rather than dropped silently, because the two are
    indistinguishable in the payload otherwise: an empty archive and a
    saturated SSHFS lane both used to render as "no events".
    """
    config = _deps().config
    archive_idx = _deps().archive_idx
    shared_spectro = _deps().shared_spectro
    _spectro_cache = _deps().spectro_cache
    _spectro_miss = _deps().spectro_miss
    _spectro_hits = _deps().spectro_hits
    station = config.stations.get(host_key)
    if station is None:
        abort(404)
    cams = [c.code for c in station.cameras if getattr(c, "spectro", False)]
    if not cams:
        return jsonify({"spectro": False, "cameras": []})
    now = time.time()
    hit = _spectro_cache.get(host_key)
    if hit and hit[0] > now:
        return jsonify(hit[1])
    shared_hit = shared_spectro.get(host_key)
    if shared_hit is not None:
        shared_payload, _age, remaining = shared_hit
        # Pin the local copy to the shared entry's REMAINING lifetime, not a
        # fresh full TTL, so every worker expires with the shared entry.
        _spectro_cache[host_key] = (now + remaining, shared_payload)
        return jsonify(shared_payload)

    def _list_dir(path: Path) -> list[str] | None:
        try:
            return sorted(
                e.name for e in os.scandir(path)
                if e.is_file(follow_symlinks=False)
            )
        except FileNotFoundError:
            return []
        except OSError:
            return None

    def _merge_night_index(cam: str, date: str, entry: dict) -> bool:
        """Fold the station's per-night index.json into *entry* in place.

        The station writes it at push time from files it already has open,
        so one read replaces two per event (76 SSHFS reads on a 38-event
        night). Only events that are actually on the archive survive the
        merge: the index is written last, but a half-finished upload would
        otherwise advertise artifacts the tab then 404s on.
        """
        raw = _with_sshfs_timeout(
            lambda p=ARCHIVE_PATH / cam / date / "spectro" / _INDEX_NAME:
                p.read_bytes(),
            timeout=8.0, default=None,
        )
        if raw is None:
            _derive_night_meta(entry)
            return False   # shed read: the entry lacks the index's detail
        try:
            idx = json.loads(raw)
            meta = {e["id"]: e for e in idx.get("events", [])
                    if isinstance(e, dict) and e.get("id")}
        except (ValueError, TypeError, AttributeError, KeyError):
            _derive_night_meta(entry)
            return True    # unreadable index: re-reading will not change it
        for ev in entry["events"]:
            m = meta.get(ev["id"])
            if not m:
                continue
            for k in ("time", "trigger", "source", "state", "score", "note"):
                if m.get(k) is not None:
                    ev[k] = m[k]
        _derive_night_meta(entry)   # fills anything the index did not carry
        return True

    def _derive_night_meta(entry: dict) -> None:
        """Fill per-event state and the night's counts from what is on the
        archive, for nights the station indexed before this existed."""
        counts = {"events": 0, "detected": 0, "spectra": 0,
                  "rms": 0, "widefield": 0}
        for ev in entry["events"]:
            files = ev.get("files", [])
            if "state" not in ev:
                ev["state"] = "spectrum" if "spectrum.png" in files else "none"
            if "trigger" not in ev:
                ev["trigger"] = ("widefield" if ev["id"].startswith("WIDE_")
                                 else "rms")
            if "time" not in ev:
                parts = ev["id"].split("_")
                hms = parts[-1] if len(parts) >= 3 and len(parts[-1]) == 6 else ""
                ev["time"] = (f"{hms[:2]}:{hms[2:4]}:{hms[4:6]}" if hms else "")
            counts["events"] += 1
            counts[ev["trigger"] if ev["trigger"] in ("rms", "widefield")
                   else "rms"] += 1
            if ev["state"] in ("transient", "spectrum"):
                counts["detected"] += 1
            if ev["state"] == "spectrum":
                counts["spectra"] += 1
        entry["counts"] = counts

    def _miss_ttl(date: str) -> float:
        """How long to remember that *date* listed empty.

        A night older than the last couple of dawn runs will not gain
        products any more; a recent one still might, so it is re-checked on
        the normal cache cadence.
        """
        try:
            d = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
        except ValueError:
            return _SPECTRO_MISS_TTL_RECENT
        age_days = (datetime.now(timezone.utc) - d).days
        return (_SPECTRO_MISS_TTL if age_days > _SPECTRO_MISS_RECENT_DAYS
                else _SPECTRO_MISS_TTL_RECENT)

    if len(_spectro_miss) > _SPECTRO_MISS_MAX:
        with _spectro_miss_lock:
            for k in [k for k, exp in list(_spectro_miss.items()) if exp <= now]:
                _spectro_miss.pop(k, None)
            while len(_spectro_miss) > _SPECTRO_MISS_MAX:
                _spectro_miss.pop(next(iter(_spectro_miss)), None)
    if len(_spectro_hits) > _SPECTRO_MISS_MAX:
        with _spectro_miss_lock:
            for k in [k for k, (exp, _e) in list(_spectro_hits.items()) if exp <= now]:
                _spectro_hits.pop(k, None)
            while len(_spectro_hits) > _SPECTRO_MISS_MAX:
                _spectro_hits.pop(next(iter(_spectro_hits)), None)
    deadline = time.monotonic() + _SPECTRO_BUILD_BUDGET_S

    partial = False
    out_cams = []
    for cam in cams:
        nights_payload = []
        for date in archive_idx.nights(cam)[:_SPECTRO_NIGHTS]:
            miss_until = _spectro_miss.get((cam, date))
            if miss_until is not None and miss_until > now:
                continue
            held = _spectro_hits.get((cam, date))
            if held is not None and held[0] > now:
                nights_payload.append(held[1])
                continue
            if time.monotonic() > deadline:
                # Out of budget: answer with what is known rather than let
                # nginx time the whole request out.
                partial = True
                continue
            names = _with_sshfs_timeout(
                lambda p=ARCHIVE_PATH / cam / date / "spectro": _list_dir(p),
                timeout=8.0, default=None,
            )
            if names is None:
                # Shed or timeout: the night is unknown, not empty. Never
                # cache that as a miss, and mark the payload partial.
                partial = True
                continue
            if not names:
                _spectro_miss[(cam, date)] = now + _miss_ttl(date)
                continue
            events: dict[str, list[str]] = {}
            for n in names:
                ev, sep, fname = n.partition("__")
                if sep and fname:
                    events.setdefault(ev, []).append(fname)
            if events:
                night_entry = {
                    "date": date,
                    "events": [
                        {"id": ev, "files": sorted(fs)}
                        for ev, fs in sorted(events.items())
                    ],
                }
                complete = True
                if _INDEX_NAME in names:
                    complete = _merge_night_index(cam, date, night_entry)
                else:
                    _derive_night_meta(night_entry)
                nights_payload.append(night_entry)
                if complete:
                    _spectro_hits[(cam, date)] = (now + _miss_ttl(date), night_entry)
            else:
                _spectro_miss[(cam, date)] = now + _miss_ttl(date)
        out_cams.append({"cam": cam, "nights": nights_payload})
    payload = {"spectro": True, "cameras": out_cams, "partial": partial}
    ttl = _SPECTRO_PARTIAL_TTL if partial else _SPECTRO_CACHE_TTL
    _spectro_cache[host_key] = (now + ttl, payload)
    # A partial listing is one worker's transient bad luck with the SSHFS
    # lane; keep it out of the tier the other workers read.
    if not partial:
        shared_spectro.set(host_key, payload, ttl)
    return jsonify(payload)


@require_station
def archive_shortclip(camera: str, date: str, filename: str):
    """Trim an archived clip to [ss, ss+t] and serve it as an MP4 attachment.

    Powers the "Download clip" button in the archive/VDB video modal, which
    forwards the operator's trim-handle selection as ``ss``/``t``. Mirrors
    the public highlights shortclip endpoint but is operator-authenticated
    (``require_station``) rather than public, so it also serves non-public
    commissioning cameras and is not subject to the anonymous rate limit.
    The ``as_attachment`` + ``download_name`` header is what gives the saved
    file a real name (the old client-side ``<a download>`` dropped it on any
    cross-origin/inline response).
    """
    config = _deps().config
    _validate_archive_path(camera, date, "meteors", filename)
    if not re.match(r"^[A-Z0-9]+$", camera):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if not re.match(r"^[\w._-]+\.(mkv|mp4)$", filename):
        abort(400)
    if not _has_camera_access(config, camera):
        abort(403)
    try:
        ss = max(0.0, min(300.0, float(request.args.get("ss", 0))))
        t  = min(120.0, max(0.5, float(request.args.get("t", 20))))
    except (TypeError, ValueError):
        abort(400)

    # Prefer whichever sibling exists (mkv is canonical; mp4 faststart may
    # be the only one present for some nights).
    src = _archive_clip_path(camera, date, filename)
    if src is None:
        stem0 = filename.rsplit(".", 1)[0]
        for alt in (stem0 + ".mkv", stem0 + ".mp4"):
            src = _archive_clip_path(camera, date, alt)
            if src is not None:
                break
    if src is None:
        abort(404)

    resp = trim_clip_response(src, ss, t, filename)
    if resp is None:
        abort(500)
    return resp


# ── Registration ────────────────────────────────────────────────────────

def register_archive_routes(
    app: Flask,
    config: DashboardConfig,
    tunnels: TunnelManager,
    *,
    archive_idx: _ArchiveIndex,
    nights_full_cache: dict[str, tuple[float, list]],
    nights_full_ttl: float,
    prefetch_archive_thumbs: Callable[[str, str, str], None] | None = None,
    shared_spectro: SharedSnapshotStore | None = None,
) -> None:
    """Attach the archive, calibration-plot, cached-video, stitch, loop,
    spectro and shortclip routes.

    Stores the dependencies (see ``ArchiveDeps``) in ``_DEPS`` for the
    module-level handlers and helpers, replacing any earlier registration's,
    then adds the URL rules. Each endpoint name is the view function's name,
    as ``@app.route`` gave it.
    """
    global _DEPS
    _DEPS = ArchiveDeps(
        config=config,
        tunnels=tunnels,
        archive_idx=archive_idx,
        nights_full_cache=nights_full_cache,
        nights_full_ttl=nights_full_ttl,
        prefetch_archive_thumbs=prefetch_archive_thumbs,
        # Cross-worker second tier for /api/spectro. Defaulting to a disabled
        # store keeps callers that pass none -- tests, single-worker dev -- on
        # the exact per-worker behaviour.
        shared_spectro=shared_spectro or SharedSnapshotStore(None, namespace="spectro"),
    )
    app.add_url_rule("/api/archive/cameras", view_func=api_archive_cameras)
    app.add_url_rule("/api/archive/nights/<camera>", view_func=api_archive_nights)
    app.add_url_rule("/api/archive/meteors/<camera>/<date>", view_func=api_archive_meteors)
    app.add_url_rule("/api/archive/indexed/<camera>/<date>", view_func=api_archive_indexed)
    app.add_url_rule("/api/archive/rms-detections/<camera>/<date>",
                     view_func=api_archive_rms_detections)
    app.add_url_rule("/api/calibration/ff_intervals/<camera>",
                     view_func=api_calibration_ff_intervals)
    app.add_url_rule("/api/calibration/plot/<kind>/<camera>", view_func=api_calibration_plot)
    app.add_url_rule("/api/rms/plots-latest/<host_key>/<camera>",
                     view_func=api_rms_plots_latest)
    app.add_url_rule("/api/rms/plots-latest", view_func=api_rms_plots_latest_batch)
    app.add_url_rule("/api/rms/color-stack-latest/<host_key>/<camera>",
                     view_func=api_rms_color_stack_latest)
    app.add_url_rule("/api/calibration/astrometry_residual/<camera>",
                     view_func=api_calibration_astrometry_residual)
    app.add_url_rule("/api/archive/file/<camera>/<date>/<subdir>/<filename>",
                     view_func=api_archive_file)
    app.add_url_rule("/api/archive/nights_full/<camera>", view_func=api_archive_nights_full)
    app.add_url_rule("/api/cached-video/<host_key>/<station_code>/<date>/<filename>",
                     view_func=api_cached_video)
    app.add_url_rule("/api/cached-files/<station_code>/<date>", view_func=api_cached_files)
    app.add_url_rule("/api/stitch-multi", view_func=api_stitch_multi, methods=["POST"])
    app.add_url_rule("/api/stitch-multi/<job>", view_func=api_stitch_multi_result)
    app.add_url_rule("/loop_clip_archive/<camera>/<date>/<filename>",
                     view_func=loop_clip_archive)
    app.add_url_rule("/api/spectro/<host_key>", view_func=api_spectro)
    app.add_url_rule("/api/archive/shortclip/<camera>/<date>/<filename>",
                     view_func=archive_shortclip)
