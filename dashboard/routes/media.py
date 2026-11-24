"""Media, videodb, and live-stream routes.

Extracted from rovimen_dashboard.py. Wired in from ``create_app()`` via
``register_media_routes``; ``routes/basket.py`` imports
``_stream_zip_from_files``.

Module layout, the same as ``routes/sky_dome.py``, ``routes/archive.py`` and
``routes/detections.py``:

- Every handler and helper is a module-level function, so a helper can be
  imported and unit-tested without building the app.
- What they need from ``create_app`` (``config``, ``tunnels``, the station
  status ``cache``, the archive index, the thumbnail prefetch executor and
  job, the live-stream semaphores and their retry hint, the videodb shared
  tier) and the per-app chunk-list cache sit in one frozen ``MediaDeps``.
  ``register_media_routes`` builds it and stores it in ``_DEPS``; a function
  that needs a dependency reads it through ``_deps()`` in its first lines.
  Each registration replaces the holder, so in a process that builds several
  apps (the test suite) the last app registered wins; a gunicorn worker
  builds one app.
- ``register_media_routes`` is the only code that touches the Flask app: it
  sets ``_DEPS`` and adds the URL rules with ``app.add_url_rule``. The
  endpoint names stay the function names (``api_videodb_chunks``) and
  ``@public_route`` / ``@require_station`` / ``@require_station_lock`` stay on
  the functions, so the auth gate (``security._matched_public_view``) and the
  CSRF exemption see the same views as when the routes were closures. There
  is no Blueprint: it would rename every endpoint to ``media.<name>``.

Process-wide state (the archived RMS-night probe state, the overlay module)
is module-private.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import types
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import parse_qs

import requests
from flask import (
    Flask,
    Response,
    abort,
    jsonify,
    request,
    send_file,
    session,
    stream_with_context,
)

from cache_store import (
    ARCHIVE_PATH,
    _disk_cache_path,
    _disk_cache_write,
    CACHE_PATH,
    NGINX_ACCEL,
    THUMB_ARCHIVE_DIR,
    THUMB_CACHE_DIR,
    BoundedCache,
    _downsample_thumbnail_bytes,
    _drop_future_dates,
    _is_nonempty_file,
    _thumb_archive_path,
    _thumb_cache_has_space,
    _thumb_cache_path,
    ffthumb_local_path,
)

from auth import (
    camera_is_public_for_request,
    is_anonymous,
    require_station,
    require_station_lock,
    station_is_public_for_request,
)
from http_caching import _json_cached
from shared_snapshot import SharedSnapshotStore
from security import public_route
from atomic_write import write_atomic
import chunk_listings
import clip_cache
import clip_detections
import detection_db
import gmn_data
import hidden_detections
import kept_clips
import stack_overlay
import upload_state
from routes.archive import (
    STATION_WAIT_S, _archive_clip_path, _archive_rms_rows, _disk_cache_read,
    _serve_archive_clip, archive_clip_path_bounded, archive_detection_window,
    archive_media_path, looped_clip_response, serve_station_clip,
    trim_clip_response,
)
from routes.detections import _archive_nights, _resolve_stitched_chunk
from route_helpers import (
    lookup_station,
    compute_detection_offset,
    proxy_error_response as _proxy_error_response,
    validate_media_params as _validate_media_params_base,
    media_url as _media_url_base,
)
from station_client import (
    station_url,
    _session_for_url,
    _streaming_proxy,
    _with_sshfs_timeout,
)
from tunnels import _TunnelDown

if TYPE_CHECKING:
    from cache_store import _ArchiveIndex
    from models import DashboardConfig, StationConfig
    from station_client import StationCache
    from tunnels import TunnelManager

logger = logging.getLogger(__name__)

# Per-file body cap when fetching a clip for batch download (a single
# pathological chunk can't blow past this).
_BATCH_PER_FILE_MAX_BYTES = 50 * 1024 * 1024
# Per-request aggregate cap across all selected files. gunicorn runs 1 worker
# with 32 threads on the VPS; without this a single request (50 files just
# under the per-file cap) could pull ~2.5 GB into one temp dir and several
# concurrent requests would fill the disk / OOM the box. Configurable via env.
_BATCH_TOTAL_MAX_BYTES = int(
    os.environ.get("ROVIMEN_BATCH_DOWNLOAD_MAX_BYTES", str(200 * 1024 * 1024))
)
# Cap on the upper download size for the single-clip crop fallback (the source
# is streamed to /tmp before ffmpeg runs); a 15-min capture chunk is ~900 MB.
_CROP_SOURCE_MAX_BYTES = int(
    os.environ.get("ROVIMEN_CROP_SOURCE_MAX_BYTES", str(500 * 1024 * 1024))
)

# Browser cache policy for thumbnail/stack images served by Flask (the NVMe
# miss paths). Matches the 30-day immutable policy nginx applies on the
# X-Accel-Redirect path so a thumb gets the same Cache-Control no matter
# which tier served it.
_THUMB_IMMUTABLE_MAX_AGE_S = 30 * 86400


def _current_night(now: datetime | None = None) -> str:
    """Folder name (YYYYMMDD) of the night in progress.

    A night folder is named for the evening it starts, so until 12:00 UTC the
    night in progress is yesterday's date: at 03:00 UTC on 20 Sep it is
    20260919. Gating on the calendar date instead treated the second half of
    every night as a past night.
    """
    now = now or datetime.now(timezone.utc)
    return (now - timedelta(hours=12)).strftime("%Y%m%d")


def _live_night_floor(now: datetime | None = None) -> str:
    """Oldest night whose stack thumbs may still change in place: the night in
    progress and the one before it, because dawn processing recolours stacks
    until the next afternoon (13:15 UTC deadline)."""
    night = datetime.strptime(_current_night(now), "%Y%m%d")
    return (night - timedelta(days=1)).strftime("%Y%m%d")


def _thumb_cache_control(date: str, live_floor: str) -> str:
    """Cache-Control for thumbnail/stack image responses.

    Nights older than ``live_floor`` are immutable -- the archive never
    rewrites a stack thumb -- so they get the same 30-day immutable policy as
    the nginx X-Accel path. Live nights get a short TTL instead: the BW
    thumbnail is overwritten in place (same URL) by the color pass after
    morning processing, and an immutable header would pin the BW version in
    the browser for a month.
    """
    if date < live_floor:
        return f"public, max-age={_THUMB_IMMUTABLE_MAX_AGE_S}, immutable"
    return "public, max-age=300"


# ── Crop+overlay helpers (module-level) ────────────────────────────────────────

_BUNDLE_PATH = Path("/opt/rovimen/station-bundle")
_BUNDLE_FONT = _BUNDLE_PATH / "fonts" / "VCR_OSD_MONO_1.001.ttf"


def _silent_unlink_path(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _stream_download_capped(resp, dest_path: str, max_bytes: int) -> bool:
    """Stream a ``requests`` response body to ``dest_path``, aborting if it
    exceeds ``max_bytes``.

    Returns True on success, False if the cap was hit (the partial file is
    removed). The crop routes call this for the station fallback so a giant
    capture chunk can't fill the VPS /tmp partition before ffmpeg starts.
    """
    written = 0
    with open(dest_path, "wb") as f:
        for chunk in resp.iter_content(65536):
            written += len(chunk)
            if written > max_bytes:
                f.close()
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass
                return False
            f.write(chunk)
    return True


class _ZipSink:
    """Append-only file-like that buffers what ``zipfile`` writes and lets a
    generator drain it.

    ``ZipFile`` records absolute stream offsets in each entry header and the
    central directory, so the object it writes to must report a monotonically
    increasing ``tell()`` even though we discard bytes after yielding them.
    This sink keeps the running offset while emptying its buffer on each
    ``drain()`` -- that is what lets us stream the archive without holding it
    all in RAM. ``zipfile`` only writes sequentially here (no seek back), so a
    forward-only sink is sufficient.
    """

    def __init__(self) -> None:
        self._buf = bytearray()
        self._pos = 0

    def write(self, data: bytes) -> int:
        self._buf.extend(data)
        self._pos += len(data)
        return len(data)

    def tell(self) -> int:
        return self._pos

    def flush(self) -> None:  # pragma: no cover - zipfile calls this
        pass

    def drain(self) -> bytes:
        if not self._buf:
            return b""
        out = bytes(self._buf)
        self._buf.clear()
        return out


def _expand_zip_entries(entries):
    """Flatten ``_stream_zip_from_files`` entries, running lazy producers."""
    for item in entries:
        if callable(item):
            yield from (item() or ())
        else:
            yield item


def _stream_zip_from_files(entries, cleanup_dir: str | None = None):
    """Yield a ZIP archive (stored, no compression) built from on-disk files.

    ``entries`` is an ordered iterable of ``(arcname, source_path)`` tuples,
    or of zero-argument callables returning such tuples: those run when the
    stream reaches them, so a slow producer (an ffmpeg cut, a station pack,
    a manifest that reports their outcome) no longer holds back the first
    byte.
    Each source file is streamed straight into the zip entry and the sink is
    drained between blocks, so the full archive never resides in RAM at once --
    this is what keeps the batch-download route from OOM-ing the single
    gunicorn worker. ``cleanup_dir`` (if given) is removed once the generator
    is exhausted or closed.
    """
    try:
        sink = _ZipSink()
        with zipfile.ZipFile(sink, mode="w", compression=zipfile.ZIP_STORED) as zf:
            for arcname, source_path in _expand_zip_entries(entries):
                with zf.open(arcname, mode="w") as dst, open(source_path, "rb") as src:
                    while True:
                        block = src.read(65536)
                        if not block:
                            break
                        dst.write(block)
                        chunk = sink.drain()
                        if chunk:
                            yield chunk
                # Flush the per-entry data descriptor before the next entry.
                chunk = sink.drain()
                if chunk:
                    yield chunk
        # Central directory written on ZipFile close.
        tail = sink.drain()
        if tail:
            yield tail
    finally:
        if cleanup_dir is not None:
            shutil.rmtree(cleanup_dir, ignore_errors=True)


def _load_overlay_module():
    # Compile in-memory rather than via spec_from_file_location: a file-based
    # loader writes __pycache__/ into the signed station bundle, which then
    # fails the stations' exact-set manifest verification on the next pull.
    p = _BUNDLE_PATH / "overlay.py"
    if not p.exists():
        return None
    try:
        source = p.read_bytes()
        mod = types.ModuleType("rovimen_overlay")
        mod.__file__ = str(p)
        exec(compile(source, str(p), "exec"), mod.__dict__)
        return mod
    except Exception:
        return None


_overlay_mod = _load_overlay_module()


def _crop_overlay_config(config, tunnels, host_key: str,
                         station_code: str) -> tuple[dict, dict]:
    """``(overlay_cfg, station_cfg)`` for the crop and fullstack renders, from
    the station's cached ``/api/settings`` (``stack_overlay.station_settings``).

    Font and logo paths are remapped to the VPS station-bundle copies so the
    overlay rendered here matches the one burned in by the station encoder.
    Unlike ``stack_overlay.fetch_overlay_config``, an unreachable station or an
    empty ``overlay`` block gives the station's own (empty) block plus the
    bundle font/logo, not the fleet default. When the station settings lack
    the camera's az/alt, the pointing is filled from the dashboard config /
    platepar store so an unreachable station renders its real ALT/AZ, not 0.0.
    """
    settings = stack_overlay.station_settings(config, tunnels, host_key) or {}
    overlay_cfg = dict(settings.get("overlay", {}))
    stations_cfg = settings.get("stations", {})
    # Remap station-local font/logo paths to VPS bundle equivalents. The
    # logo is resolved by filename so a station keeps its OWN mark here --
    # see stack_overlay.bundle_logo_for.
    if _BUNDLE_FONT.exists():
        overlay_cfg["font"] = str(_BUNDLE_FONT)
    bundle_logo = stack_overlay.bundle_logo_for(overlay_cfg)
    if bundle_logo is not None:
        overlay_cfg["logo"] = str(bundle_logo)
    return overlay_cfg, stack_overlay.resolve_camera_pointing(
        config, host_key, station_code, stations_cfg.get(station_code, {})
    )


def _build_crop_cmd(
    in_path: str, out_path: str,
    x: float, y: float, w: float, h: float,
    station_code: str, filename: str,
    ss: float, t: float,
    overlay_cfg: dict, station_cfg: dict,
    vframes: int | None = None,
) -> list[str]:
    """Build ffmpeg cmd list: crop → fit into 1280×720 → station overlay.

    Uses the same overlay.py logic as the station encoder so style is identical.
    Falls back to bare crop+scale if overlay module or config is unavailable.

    When vframes is set, omits -t and uses -vframes instead (for still-frame
    extraction). The output path extension determines the codec (e.g. .jpg).
    """
    # Fit the crop inside 1280x720 and letterbox the rest: forcing it to
    # 1280x720 stretched every selection that was not 16:9. The frame stays
    # 1280x720 so the overlay bar and annotations keep their layout.
    crop_scale = (
        f"crop=iw*{w:.4f}:ih*{h:.4f}:iw*{x:.4f}:ih*{y:.4f},"
        f"scale=1280:720:force_original_aspect_ratio=decrease:flags=lanczos,"
        f"pad=1280:720:(ow-iw)/2:(oh-ih)/2:black,setsar=1"
    )

    # epoch of the trimmed clip start (drawtext renders it through gmtime)
    chunk_epoch = 0
    m = re.match(r"^.+?_(\d{8})_(\d{6})", filename)
    if m:
        try:
            dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
            chunk_epoch = int(dt.timestamp()) + int(ss)
        except Exception:
            pass

    if vframes is not None:
        base_cmd = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", in_path]
        enc_args  = ["-vframes", str(vframes), "-q:v", "2"]
    else:
        base_cmd = ["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-t", f"{t:.3f}", "-i", in_path]
        # Keep the source timestamps: a Matroska clip reports 1000 fps (its
        # millisecond timebase), and a CFR mp4 output duplicated every frame
        # 40 times (5,595 frames for a 5.6 s crop, 30 s of encoding, past
        # nginx's 30 s read timeout, so the browser saw the crop fail).
        enc_args  = ["-fps_mode", "passthrough",
                     "-c:v", "libx264", "-preset", "fast", "-crf", "18", "-an"]

    if _overlay_mod is None or not overlay_cfg:
        return [*base_cmd, "-vf", crop_scale, *enc_args, out_path]

    try:
        filters, bar_h = _overlay_mod.build_drawtext_annotations(
            overlay_cfg, station_code, station_cfg, chunk_epoch,
        )
    except Exception:
        filters, bar_h = [], 0

    vf_parts = [crop_scale]
    if bar_h > 0:
        vf_parts.append(f"pad=iw:ih+{bar_h}:0:0:black")
    vf_parts.extend(filters)

    # Branding (bottom-bar astromania logo + corner GMN logo) comes from the same
    # shared builder the encoder uses, so this preview cannot drift from real
    # clips. ``bar_h`` places the logo inside the bar, not over the video.
    try:
        logo_inputs, brand_fragment = _overlay_mod.build_branding_overlay(
            overlay_cfg, in_label="vmain", out_label="out", input_index=1,
            bar_h=bar_h,
        )
    except Exception:
        logo_inputs, brand_fragment = [], ""

    if logo_inputs:
        fc = f"[0:v]{','.join(vf_parts)}[vmain];{brand_fragment}"
        return [*base_cmd, *logo_inputs, "-filter_complex", fc, "-map", "[out]", *enc_args, out_path]

    return [*base_cmd, "-vf", ",".join(vf_parts), *enc_args, out_path]


def _archive_detection_times(cam_code: str, date: str) -> list[str]:
    """Begin times of a night's archived detections, for window matching.

    The rows are the ones the rms-detections panel serves (``_archive_rms_rows``:
    parsed once per night, kept on disk, SSHFS-bounded). The Detections tab asks
    for both at once, and parsing FTPdetectinfo a second time here cost each
    cold chunk list up to 5 s more on the Storage Box.
    """
    return [det["time_utc"] for det in _archive_rms_rows(cam_code, date)
            if det.get("time_utc")]


def _archive_clip_sizes(cam_code: str, date: str, meteors_mtime: int | None,
                        names: list[str]) -> dict[str, int]:
    """Byte sizes of a night's listed clips.

    The sshfs listing carries no sizes, so each is its own ``stat`` round
    trip (~45 ms on 2026-09-27), and statting every entry of meteors/ one
    by one overran the 5 s guard on most nights, so cards showed no size.
    Only the clips are stat'ed, four at a time, and the result is kept on
    disk against meteors/'s mtime: a night pays once, until a clip is
    added, replaced or removed.
    """
    disk_key = f"{cam_code}_{date}"
    if meteors_mtime:
        hit = _disk_cache_read("archive_sizes", disk_key, 30 * 86400)
        if (isinstance(hit, dict) and hit.get("mtime") == meteors_mtime
                and isinstance(hit.get("sizes"), dict)
                and all(n in hit["sizes"] for n in names)):
            return hit["sizes"]
    meteors_dir = ARCHIVE_PATH / cam_code / date / "meteors"

    def _size(name: str) -> int | None:
        try:
            return (meteors_dir / name).stat().st_size
        except OSError:
            return None

    def _stat_all() -> dict[str, int]:
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="clip-size") as pool:
            return {n: sz for n, sz in zip(names, pool.map(_size, names)) if sz is not None}

    sizes = _with_sshfs_timeout(_stat_all, timeout=5.0, default=None)
    if sizes is None:
        return {}
    if meteors_mtime and len(sizes) == len(names):
        _disk_cache_write("archive_sizes", disk_key,
                          {"mtime": meteors_mtime, "sizes": sizes})
    return sizes


# ── Archived RMS-night lists: probe once, share, bound ───────────────────────
#
# Which archived nights have an rms/ dir gets the same treatment as the
# archived detection rows (``routes.archive._archive_rms_rows``): nights
# already known to have one are never stat'ed again, and the probe runs under
# the SSHFS guard.
_RMS_NIGHTS_TTL_S = 600                  # re-probe a night still lacking rms/ after this
_RMS_NIGHTS_PROBE_BUDGET_S = 4.0         # cooperative deadline inside the guarded probe
_RMS_NIGHTS_DISK_TTL_S = 30 * 86400
# cam -> {"with_rms": set[str], "checked": {night: wall_ts}}
_rms_nights_cache: dict[str, dict[str, Any]] = {}
_rms_nights_lock = threading.Lock()


def _rms_nights_disk_mtime(cam_code: str) -> int:
    try:
        return _disk_cache_path("rms_nights", cam_code).stat().st_mtime_ns
    except OSError:
        return 0


def _load_rms_nights_state(cam_code: str) -> dict[str, Any]:
    """This worker's probe state for ``cam_code``, merged with what other
    workers saved since it last looked. Each of the three gunicorn workers
    used to read the file once and then probe the box on its own, so a
    camera's first visits got partial night lists (67 of 86 nights for
    RO000Z (Sibiciu) on 2026-09-27) and one came back empty."""
    mtime = _rms_nights_disk_mtime(cam_code)
    with _rms_nights_lock:
        st = _rms_nights_cache.get(cam_code)
        if st is not None and st["disk_mtime"] >= mtime:
            return st
    disk = _disk_cache_read("rms_nights", cam_code, _RMS_NIGHTS_DISK_TTL_S)
    with_rms: set[str] = set()
    checked: dict[str, float] = {}
    if isinstance(disk, list):                       # first-generation format
        with_rms = {str(d) for d in disk}
    elif isinstance(disk, dict):
        with_rms = {str(d) for d in disk.get("with_rms") or ()}
        checked = {str(k): float(v) for k, v in (disk.get("checked") or {}).items()}
    with _rms_nights_lock:
        st = _rms_nights_cache.setdefault(
            cam_code, {"with_rms": set(), "checked": {}, "disk_mtime": 0})
        st["with_rms"] |= with_rms
        for d, ts in checked.items():
            if ts > st["checked"].get(d, 0.0):
                st["checked"][d] = ts
        st["disk_mtime"] = max(st["disk_mtime"], mtime)
        return st


def _archive_rms_nights(archive_idx, cam_code: str) -> set[str]:
    """Archived nights of ``cam_code`` that carry an ``rms/`` directory.

    Incremental and cooperative: a night known to have ``rms/`` is never
    stat'ed again (the archive is append-only per night), a night found
    without one is re-checked after ``_RMS_NIGHTS_TTL_S``, and one call probes
    for at most ``_RMS_NIGHTS_PROBE_BUDGET_S`` (newest nights first) under the
    SSHFS guard, keeping whatever it managed. A camera with hundreds of nights
    used to be one all-or-nothing 8 s probe per request that, on a slow box,
    timed out every time and never got anywhere; now each call is bounded and
    makes progress, and the state persists across workers and recycles.
    """
    st = _load_rms_nights_state(cam_code)
    now_wall = time.time()
    with_rms: set[str] = st["with_rms"]
    checked: dict[str, float] = st["checked"]
    candidates = [
        d for d in archive_idx.nights(cam_code)          # newest first
        if d not in with_rms and now_wall - checked.get(d, 0.0) >= _RMS_NIGHTS_TTL_S
    ]
    if not candidates:
        return set(with_rms)

    found: set[str] = set()
    probed: list[str] = []
    probe_lock = threading.Lock()

    def _probe_one(d: str) -> None:
        if time.monotonic() > deadline:
            return
        has_rms = (ARCHIVE_PATH / cam_code / d / "rms").is_dir()
        with probe_lock:
            probed.append(d)
            if has_rms:
                found.add(d)

    def _probe() -> bool:
        # Four at a time: each probe is one stat round trip (~45 ms), so a
        # camera's ~90 nights fit the budget instead of about half of them.
        with ThreadPoolExecutor(max_workers=4, thread_name_prefix="rms-probe") as pool:
            list(pool.map(_probe_one, candidates))
        return True

    deadline = time.monotonic() + _RMS_NIGHTS_PROBE_BUDGET_S

    _with_sshfs_timeout(_probe, timeout=_RMS_NIGHTS_PROBE_BUDGET_S + 4.0, default=None)
    # Even a guard timeout leaves partial progress in `probed`/`found`.
    with probe_lock:
        probed_now, found_now = list(probed), set(found)
    with _rms_nights_lock:
        for d in probed_now:
            checked[d] = now_wall
        with_rms |= found_now
        snapshot = {"with_rms": sorted(with_rms),
                    "checked": {d: ts for d, ts in checked.items() if now_wall - ts < _RMS_NIGHTS_DISK_TTL_S}}
    if probed_now:
        # Another worker may have saved since this one loaded: fold its
        # progress in first, so neither write loses the other's.
        other = _load_rms_nights_state(cam_code)
        with _rms_nights_lock:
            snapshot["with_rms"] = sorted(set(snapshot["with_rms"]) | other["with_rms"])
            for d, ts in other["checked"].items():
                if ts > snapshot["checked"].get(d, 0.0):
                    snapshot["checked"][d] = ts
        _disk_cache_write("rms_nights", cam_code, snapshot)
        with _rms_nights_lock:
            st["disk_mtime"] = max(st["disk_mtime"], _rms_nights_disk_mtime(cam_code))
    return set(with_rms)


def _hidden_listing(items: list[dict[str, Any]], cam_code: str) -> tuple[list[dict[str, Any]], int]:
    """A camera-night's detections or detection clips as this request lists
    them: hidden false positives left out, or marked under ``show_hidden=1``
    (see ``hidden_detections``). Returns ``(items, hidden_count)``. Applied
    after every cache, so a hide shows on the next request of any worker."""
    snap = hidden_detections.snapshot()
    show = hidden_detections.wants_hidden(request.args.get("show_hidden"), is_anonymous())
    return (hidden_detections.apply(items, snap, show, cam=cam_code),
            hidden_detections.count(items, snap, cam=cam_code))


def _videodb_rms_from_archive(cam_code: str, date: str):
    """Return RMS detection data parsed from archive rms/ files for old nights.

    Trajectories are attached per request on copies of the cached rows: GMN
    publishes a night 4-15 days after it happened, so a row cached before
    that must still pick the solution up.
    """
    rows, hidden_count = _hidden_listing(
        [dict(r) for r in _archive_rms_rows(cam_code, date)], cam_code)
    return _json_cached({
        "camera": cam_code,
        "date": date,
        "hidden_count": hidden_count,
        "detections": gmn_data.attach_trajectories(cam_code, rows),
    }, max_age=0 if not is_anonymous() else 300)


# ── Dependencies from create_app ────────────────────────────────────────

# Archived nights are immutable, but the fallback still costs a listing
# revalidation (~0.3 s over sshfs). A short in-memory cache
# (``MediaDeps.archive_chunks_cache``) makes repeat opens of an old night
# instant. A night in the hot window whose list is empty is not cached, so
# clips still landing keep being re-checked; an old night's empty list is an
# answer too, unless the box failed to answer while it was built (which reads
# as empty).
_ARCHIVE_CHUNKS_TTL_S = 600.0
_ARCHIVE_CHUNKS_CACHE_MAX = 1024


@dataclass(frozen=True)
class MediaDeps:
    """What the routes and helpers need from ``create_app``.

    ``cache`` is the station status cache (``get_status``).
    ``prefetch_archive_thumbs(host_key, cam, date, names=None)`` copies a
    night's archived stack thumbnails to the NVMe cache; it runs on
    ``prefetch_executor``, never on the request thread.
    ``live_stream_triple_sem(user, host_key, camera_code)`` returns the
    per-(user, host, camera) semaphore and ``live_stream_global_sem`` caps
    live streams across the worker; ``live_stream_retry_after_s`` is the
    hint a refused stream gets. ``shared_videodb`` is the cross-worker tier
    behind the chunk-list cache: each gunicorn worker kept its own copy, so a
    chunk list (1.1-1.4 s cold, ~1,600 calls/day) was built three times (10
    identical calls on prod 2026-09-25 gave exactly 3 slow ones); a no-op
    without ROVIMEN_REDIS_URL. ``archive_chunks_cache`` is per-app state, new
    for each registration as it was when it lived in the closure.
    """

    config: DashboardConfig
    tunnels: TunnelManager
    cache: StationCache
    archive_idx: _ArchiveIndex
    prefetch_executor: ThreadPoolExecutor
    prefetch_archive_thumbs: Callable[..., None]
    live_stream_triple_sem: Callable[[str, str, str], threading.BoundedSemaphore]
    live_stream_global_sem: threading.BoundedSemaphore
    live_stream_retry_after_s: int
    shared_videodb: SharedSnapshotStore
    # ("archive-chunks", cam, date) -> (built_at_monotonic, chunk list)
    archive_chunks_cache: BoundedCache = field(
        default_factory=lambda: BoundedCache(maxsize=_ARCHIVE_CHUNKS_CACHE_MAX))


# Set by register_media_routes; each registration replaces it.
_DEPS: MediaDeps | None = None


def _deps() -> MediaDeps:
    if _DEPS is None:
        raise RuntimeError("media code used before register_media_routes")
    return _DEPS


def _require_station(host_key: str) -> StationConfig:
    return lookup_station(_deps().config, host_key)


def _clip_stem(name: str) -> str:
    """``X`` for ``X_color.mkv`` and its faststart ``X_color.mp4``, so the two
    copies of one clip compare equal. Unlike ``route_helpers.clip_base`` it
    strips the suffix and leaves ``.details`` names as they are."""
    return name.replace("_color.mkv", "").replace("_color.mp4", "")


# ── Archive helpers ──────────────────────────────────────────────────────

def _archive_chunks_data(host_key: str, cam_code: str, date: str) -> list[dict[str, Any]]:
    """Locked-chunk list built from the storagebox archive (no station)."""
    archive_idx = _deps().archive_idx
    prefetch_executor = _deps().prefetch_executor
    prefetch_archive_thumbs = _deps().prefetch_archive_thumbs
    ni = archive_idx.night_files(cam_code, date)
    if ni is None:
        return []
    meteor_names = set(ni.meteor_files)

    # state.json recovers per-chunk meteor_time. The listing read it in the
    # same pass; read it here only for a listing restored without it.
    if ni.state_chunks is not None:
        state_chunks = ni.state_chunks
    else:
        state_path = ARCHIVE_PATH / cam_code / date / "state.json"

        def _read_state():
            if not state_path.exists():
                return {}
            return json.loads(state_path.read_text()).get("chunks") or {}
        state_chunks = _with_sshfs_timeout(_read_state, timeout=5.0, default={})

    # Faststart stations publish BOTH ``X_color.mkv`` and its seekable
    # ``X_color.mp4`` sibling into meteors/, so a naive pass lists every
    # clip twice (the "100 clips" = 50 pairs bug in the VDB). Collapse to
    # one entry per clip: prefer the canonical ``.mkv`` (its name drives
    # stack + lock resolution) and keep the ``.mp4`` only when it is the
    # sole survivor — the storagebox janitor reaps the heavy ``.mkv`` but
    # leaves the light ``.mp4`` behind on aged nights.
    mkv_bases = {
        _clip_stem(n) for n in ni.meteor_files if n.endswith("_color.mkv")
    }

    chunks: list[dict[str, Any]] = []
    lock_times: dict[str, str | None] = {}
    lock_detection_times: dict[str, str | None] = {}
    for name in ni.meteor_files:
        if not (name.endswith(".mkv") or name.endswith(".mp4")):
            continue
        if name.endswith(".details.mkv") or name.endswith(".details.mp4"):
            continue  # starprint details variant of a base chunk, not its own chunk
        if name.endswith(".mp4") and _clip_stem(name) in mkv_bases:
            continue  # faststart sibling of a still-present .mkv
        # Stack name from the clip base so an .mp4-only survivor resolves
        # the same ``…_stack.webp`` the .mkv would have.
        stack_name = f"{_clip_stem(name)}_stack.webp"
        time_str = "00:00:00"
        stem = name.rsplit(".", 1)[0]
        parts = stem.split("_")
        if len(parts) >= 3:
            t = parts[2]
            if len(t) == 6 and t.isdigit():
                time_str = f"{t[:2]}:{t[2:4]}:{t[4:6]}"
        if stack_name in ni.stack_files:
            stack_subdir = "stacks"
        elif stack_name in meteor_names:
            stack_subdir = "meteors"
        else:
            stack_subdir = None
        chunk_state = state_chunks.get(name) or {}
        lock = chunk_state.get("lock") if isinstance(chunk_state.get("lock"), dict) else {}
        meteor_time = lock.get("meteor_time")
        lock_times[name] = meteor_time
        lock_detection_times[name] = lock.get("detection_time")
        chunks.append({
            "filename": name,
            "time": time_str,
            "stack": stack_name if stack_subdir else None,
            "stack_subdir": stack_subdir,
            "size_mb": None,          # filled below, once for every clip
            "locked": True,
            "lock_type": lock.get("lock_type", "detection"),
            # Both filled by the resolve pass below, once every clip's
            # meteor time is known: the offset lets the player seek
            # straight to the detection with no station round-trip.
            "detection_offset_s": None,
            "meteor_time": None,
            "reencoded": False,
            "source": "archive",
        })
    if chunks:
        file_sizes = _archive_clip_sizes(
            cam_code, date, ni.sig[0] if ni.sig else None,
            [c["filename"] for c in chunks])
        for chunk in chunks:
            size_bytes = file_sizes.get(chunk["filename"])
            chunk["size_mb"] = round(size_bytes / 1_048_576, 1) if size_bytes else None
        # The archived lock is the clip's pointer to its meteor, but the
        # station's dawn sweep nulls it once the clip leaves local disk, so
        # every night older than stale_lock_days (7) arrives here with no
        # meteor_time at all — no info panel under the clip, no seek to the
        # detection. Recover it from the sources the sweep cannot reach.
        # Both fallbacks are lazy: a night whose locks survived pays for
        # neither.
        resolved = clip_detections.resolve_meteor_times(
            [c["filename"] for c in chunks],
            lock_times=lock_times,
            db_times=lambda: detection_db.chunk_meteor_times(cam_code, date),
            detection_times=lambda: _archive_detection_times(cam_code, date),
        )
        for chunk in chunks:
            meteor_time = resolved.get(chunk["filename"])
            chunk["meteor_time"] = meteor_time
            chunk["detection_offset_s"] = compute_detection_offset(
                chunk["filename"],
                meteor_time,
                lock_detection_times.get(chunk["filename"]),
            )
        # Bounded via the shared prefetch executor; a bare Thread here spawned
        # one unbounded OS thread per cache-miss under load. Only these
        # clips' stacks: the night's whole stacks/ is one per 20 s chunk
        # (~1,900 files), and copying it for every night a Detections visit
        # listed kept the Storage Box busy for hours.
        clip_stacks = [c["stack"] for c in chunks
                       if c["stack"] and c["stack_subdir"] == "stacks"]
        if clip_stacks:
            prefetch_executor.submit(
                prefetch_archive_thumbs, host_key, cam_code, date, clip_stacks)
    return chunks


def _swr_get(l1: BoundedCache, key: tuple, fresh_s: float) -> tuple[float, Any] | None:
    """L1 entry ``(built_at_monotonic, payload)`` for ``key``, topped up
    from the shared tier when L1 is missing or no longer fresh.

    An adopted entry is back-dated by its age, so every worker applies the
    same fresh/stale windows to the same build and they refresh together,
    and a worker whose own copy went stale takes a newer one another
    worker published instead of re-fetching it.
    """
    _shared_vdb = _deps().shared_videodb
    now = time.monotonic()
    cached = l1.get(key)
    if cached is not None and now - cached[0] < fresh_s:
        return cached
    hit = _shared_vdb.get("/".join(key))
    if hit is not None:
        payload, age, _remaining = hit
        built = now - age
        if cached is None or built > cached[0]:
            cached = (built, payload)
            l1[key] = cached
    return cached


def _swr_put(l1: BoundedCache, key: tuple, payload: Any, ttl_s: float) -> None:
    _shared_vdb = _deps().shared_videodb
    l1[key] = (time.monotonic(), payload)
    _shared_vdb.set("/".join(key), payload, ttl_s)


def _archive_chunks_cached(host_key: str, cam_code: str, date: str) -> list[dict[str, Any]]:
    archive_idx = _deps().archive_idx
    _archive_chunks_cache = _deps().archive_chunks_cache
    key = ("archive-chunks", cam_code, date)
    cached = _swr_get(_archive_chunks_cache, key, _ARCHIVE_CHUNKS_TTL_S)
    if cached and (time.monotonic() - cached[0]) < _ARCHIVE_CHUNKS_TTL_S:
        return cached[1]
    unavailable_before = archive_idx.scan_unavailable
    data = _archive_chunks_data(host_key, cam_code, date)
    if data or (not archive_idx.in_hot_window(date)
                and archive_idx.scan_unavailable == unavailable_before):
        _swr_put(_archive_chunks_cache, key, data, _ARCHIVE_CHUNKS_TTL_S)
    return data


# ── Videodb endpoints ────────────────────────────────────────────────

@require_station
def api_videodb_nights(host_key: str, cam_code: str):
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam_code, re.IGNORECASE):
        abort(400)
    # Station-resident nights as the station pushed them (chunk listings,
    # media pointers), not a live /api/nights call (#1250).
    try:
        station_nights = sorted(
            set(chunk_listings.nights({cam_code}))
            | set(chunk_listings.media_pointer_nights({cam_code})),
            reverse=True,
        )
    except Exception:
        station_nights = []
    archive = _archive_nights(cam_code)
    station_set = set(station_nights)
    merged = station_nights + [n for n in archive if n not in station_set]
    merged = _drop_future_dates(merged)
    merged.sort(reverse=True)
    # Night list changes once per day at most; the camera "Nights" picker
    # is hit on every station tab switch. ETag + 30 s lets repeated tab
    # toggles short-circuit to 304.
    return _json_cached(merged, max_age=30)


@require_station
def api_videodb_rmsnights(host_key: str, cam_code: str):
    archive_idx = _deps().archive_idx
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam_code):
        abort(400)
    # Archive only: a night is listed once its rms/ directory is uploaded.
    # The station's /api/rmsnights used to be merged in first, which put a
    # station round-trip on every RMS-tab and station-page load for nights
    # whose plots the archive then could not serve anyway.
    with_rms = _archive_rms_nights(archive_idx, cam_code)
    nights = sorted(
        (d for d in archive_idx.nights(cam_code) if d in with_rms),
        reverse=True,
    )
    return _json_cached(_drop_future_dates(nights), max_age=60)


@require_station
def api_videodb_rms_detections(host_key: str, cam_code: str, date: str):
    """Proxy enriched RMS detection data (magnitude, shower, radiant, etc.)."""
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam_code):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    try:
        req_date = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        abort(400)
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    if req_date < cutoff:
        return _videodb_rms_from_archive(cam_code, date)
    # Recent nights: the rows the station pushed into detections.db. The
    # station indexes them with the same parser as its /api/rms-detections
    # (rovimen_rms.session_detections), so nothing changes but the source;
    # this route no longer calls the station (#1250).
    try:
        rows = detection_db.query_detections([date], cam_filter={cam_code.upper()})
    except Exception:
        logger.warning("detection index read failed for %s/%s", cam_code, date,
                       exc_info=True)
        rows = []
    if not rows:
        return _videodb_rms_from_archive(cam_code, date)
    rows, hidden_count = _hidden_listing([dict(r) for r in rows], cam_code)
    return jsonify({
        "camera": cam_code,
        "date": date,
        "count": len(rows),
        "hidden_count": hidden_count,
        "detections": gmn_data.attach_trajectories(cam_code, rows),
    })


def _pushed_chunks_payload(host_key: str, cam_code: str, date: str,
                           qs: str) -> list[dict[str, Any]] | None:
    """A recent night's chunk list from the listing the station pushed
    (``chunk_listings``), with the station's own ``/api/chunks`` filters
    applied here. None when the station pushed no listing for the night
    (the caller falls back to the archive). Never calls the station
    (#1250).
    """
    pushed = chunk_listings.get(cam_code.upper(), date)
    if not pushed or not pushed.get("chunks"):
        return None
    chunks_list = pushed["chunks"]
    params = parse_qs(qs)
    locked_only = params.get("locked_only", [""])[0] == "1"
    lock_type = params.get("lock_type", [""])[0]
    from_s = params.get("from", [""])[0].replace(":", "")
    to_s = params.get("to", [""])[0].replace(":", "")
    if locked_only or lock_type or from_s or to_s:
        def _keep(c: dict[str, Any]) -> bool:
            if locked_only and not c.get("locked"):
                return False
            if lock_type and c.get("lock_type") != lock_type:
                return False
            parts = (c.get("filename") or "").split("_")
            hhmm = parts[2][:4] if len(parts) > 2 else ""
            if from_s and to_s and from_s > to_s:
                return not (hhmm < from_s and hhmm > to_s)
            if from_s and hhmm < from_s:
                return False
            if to_s and hhmm > to_s:
                return False
            return True
        chunks_list = [c for c in chunks_list if _keep(c)]
        if not chunks_list:
            return None
    # Default (unfiltered) view: the station list alone misses every
    # detection clip the retention janitor already reaped locally --
    # uploaded (= detection-locked) clips are deleted first while
    # unlocked bulk footage survives, so the list is non-empty yet has
    # no detections. Merge the archived locked clips back in so
    # detections are visible by default, not only behind the lock
    # filter. Filtered requests keep their exact station semantics --
    # the RMS tab depends on an empty locked list falling through to the
    # archive fallback.
    if not locked_only and not lock_type:
        station_bases = {
            _clip_stem(c["filename"]) for c in chunks_list if c.get("filename")
        }
        archived = [
            c for c in _archive_chunks_cached(host_key, cam_code, date)
            if _clip_stem(c["filename"]) not in station_bases
        ]
        if archived:
            # Filenames carry <cam>_<date>_<HHMMSS>, so a filename sort
            # keeps the merged night chronological.
            chunks_list = sorted(
                chunks_list + archived, key=lambda c: c.get("filename", ""))
    return chunks_list


@require_station
def api_videodb_chunks(host_key: str, cam_code: str, date: str):
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam_code, re.IGNORECASE):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    # Old nights are immutable on the archive.
    try:
        req_date = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        abort(400)
    cutoff = datetime.now(timezone.utc) - timedelta(days=2)
    # The locked list (``locked_only=1``) is the Detection tab's list of
    # detection clips: hidden false positives are left out of it (or marked
    # under show_hidden=1). The full footage list stays footage.
    detections_view = request.args.get("locked_only") == "1"
    chunks_list = None
    max_age = 60
    if req_date >= cutoff:
        try:
            chunks_list = _pushed_chunks_payload(
                host_key, cam_code, date, request.query_string.decode())
        except Exception:
            logger.warning("pushed chunk listing failed for %s/%s", cam_code, date,
                           exc_info=True)
            chunks_list = None
        # ETag short-circuits the 200+ KB payload to 304 on poll cycles
        # where the chunk list hasn't changed.
        max_age = 15
    if chunks_list is None:
        chunks_list = _archive_chunks_cached(host_key, cam_code, date)
        max_age = 60
    if detections_view:
        chunks_list, _n = _hidden_listing(chunks_list, cam_code)
        if not is_anonymous():
            max_age = 0
    elif not is_anonymous() and isinstance(chunks_list, list):
        # The footage list (Video DB) keeps every chunk; a detection chunk
        # someone hid is marked, so its card shows Unhide (never dropped).
        chunks_list = hidden_detections.apply(
            chunks_list, hidden_detections.snapshot(), True, cam=cam_code)
    return _json_cached(chunks_list, max_age=max_age)


# ── Media proxy (redirect to station fileserver) ──────────────────────

def _validate_media_params(
    host_key: str, station_code: str, date: str, filename: str, ext_re: str,
) -> None:
    config = _deps().config
    # Per-station public-flag enforcement for anonymous visitors. When the
    # dashboard is publicly exposed, media for a ``public: false`` station
    # must 404 for anyone without a login session (closes the gap the
    # security review flagged: the flag was honoured only in public_api.py,
    # not in these internal serving handlers). Logged-in users keep full
    # fleet-wide access. Runs BEFORE base validation so a private station's
    # existence isn't disclosed via a differential error path. The camera
    # is checked too: the archive is read by camera code, so a public
    # host_key must not carry a private station's camera.
    if not (station_is_public_for_request(config, host_key)
            and camera_is_public_for_request(config, station_code)):
        abort(404)
    _validate_media_params_base(config, host_key, station_code, date, filename, ext_re)


def _media_url(host_key: str, *path_parts: str) -> str:
    config = _deps().config
    tunnels = _deps().tunnels
    return _media_url_base(config, tunnels, host_key, *path_parts)


def _serve_thumbnail(host_key: str, *path_parts: str) -> Response:
    """Serve thumbnails via tiered cache with write-through promotion.

    Lookup order -- fastest first, colour before BW:
      1. SSD hot cache (THUMB_CACHE_DIR, 3-day retention) -> nginx
         X-Accel-Redirect when NGINX_ACCEL=1.
      2. Cold-tier thumb archive (THUMB_ARCHIVE_DIR on storage box,
         unlimited retention) -> hot-promote to SSD, serve inline.
      3. Full-res archive stacks on storage box -> downsample +
         hot-promote to SSD, serve inline.
      4. BW FF thumbnail pushed live by the station's detection indexer
         (ffthumbs/ on storage box) -> serve inline, never promoted.
      5. Station HTTP -- for live nights not yet in any cache.
    """
    prefetch_executor = _deps().prefetch_executor
    prefetch_archive_thumbs = _deps().prefetch_archive_thumbs
    cam, date, filename = path_parts[1], path_parts[2], path_parts[3]
    cache_file = _thumb_cache_path(host_key, cam, date, filename)
    now = datetime.now(timezone.utc)
    current_night = _current_night(now)
    live_floor = _live_night_floor(now)
    cache_control = _thumb_cache_control(date, live_floor)

    # 1) NVMe cache hit (the common case once a night is prefetched).
    if cache_file.exists():
        if NGINX_ACCEL:
            accel_path = "/internal/thumb_cache/" + "/".join(
                [host_key, cam, date, filename]
            )
            return Response(
                headers={"X-Accel-Redirect": accel_path, "Content-Type": "image/webp"}
            )
        resp = send_file(cache_file, mimetype="image/webp", conditional=True)
        resp.headers["Cache-Control"] = cache_control
        return resp

    # NVMe miss on a finished night: kick the bulk archive prefetch so the
    # rest of the night's thumbs are warming while this one is served
    # from the slow tiers below. Coalesced inside prefetch_archive_thumbs
    # (one job per night, not one per image). Covers direct image hits --
    # deep links, browser-cached JSON payloads -- that never touch the
    # chunks/detections endpoints which normally trigger the prefetch.
    if date < current_night:
        # Bounded via the shared prefetch executor; a bare Thread here spawned
        # one unbounded OS thread per direct-image cache-miss under load.
        prefetch_executor.submit(prefetch_archive_thumbs, host_key, cam, date)

    # 2) Cold-tier thumb archive on storage box (unlimited retention).
    # Already-downsampled thumbnails promoted here by the hourly prune.
    cold_path = _thumb_archive_path(host_key, cam, date, filename)
    if cold_path is not None:
        try:
            # SSHFS stat -- guard so a stalled storagebox mount can't pin
            # the worker thread here.
            if _with_sshfs_timeout(cold_path.is_file, timeout=5.0, default=False):
                # Guard the READ too, not just the stat: a stalled mount can
                # hang mid-read and pin the worker. None -> skip to next tier.
                data = _with_sshfs_timeout(cold_path.read_bytes, timeout=10.0, default=None)
                if data is not None:
                    if _thumb_cache_has_space():
                        try:
                            cache_file.parent.mkdir(parents=True, exist_ok=True)
                            cache_file.write_bytes(data)
                        except Exception:
                            pass
                    if NGINX_ACCEL and cache_file.exists():
                        accel_path = "/internal/thumb_cache/" + "/".join(
                            [host_key, cam, date, filename]
                        )
                        return Response(
                            headers={"X-Accel-Redirect": accel_path,
                                     "Content-Type": "image/webp"},
                        )
                    return Response(data, status=200,
                                   headers={"Content-Type": "image/webp",
                                            "Cache-Control": cache_control})
        except OSError:
            pass

    # 3) Storage-box archive stacks. Most thumbs live in stacks/; legacy
    # captures wrote them flat alongside the .mkv in meteors/. Checked
    # before the station AND before the ffthumbs tier so an archived
    # colour stack always beats the BW fallback below.
    for subdir in ("stacks", "meteors"):
        src = ARCHIVE_PATH / cam / date / subdir / filename
        try:
            # SSHFS stat under the timeout guard (stalled mount -> skip).
            if not _with_sshfs_timeout(src.is_file, timeout=5.0, default=False):
                continue
        except OSError:
            continue
        if not _thumb_cache_has_space():
            # Cache is full -- serve straight from SSHFS rather than
            # bomb the worker on a disk-full copy. Skipping the
            # promotion keeps the request alive even when the NVMe
            # is past the eviction threshold.
            resp = send_file(src, mimetype="image/webp", conditional=True)
            resp.headers["Cache-Control"] = cache_control
            return resp
        try:
            # Hot-promote to NVMe so the next hit goes through X-Accel-Redirect.
            # Atomic .part-rename to avoid serving truncated webp under load.
            # Downsample on write so legacy full-res stacks in the archive
            # don't get promoted verbatim into the (small) NVMe cache.
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            # Guard the READ (not just the stat above): a stalled mount can
            # hang mid-read. None -> skip this subdir.
            src_bytes = _with_sshfs_timeout(src.read_bytes, timeout=10.0, default=None)
            if src_bytes is None:
                continue
            write_atomic(cache_file, _downsample_thumbnail_bytes(src_bytes),
                         tmp=cache_file.with_suffix(cache_file.suffix + ".part"))
            if NGINX_ACCEL:
                accel_path = "/internal/thumb_cache/" + "/".join(
                    [host_key, cam, date, filename]
                )
                return Response(
                    headers={"X-Accel-Redirect": accel_path,
                             "Content-Type": "image/webp"},
                )
            resp = send_file(cache_file, mimetype="image/webp", conditional=True)
            resp.headers["Cache-Control"] = cache_control
            return resp
        except Exception:
            # If the copy failed (disk full / permission), serve directly
            # from SSHFS -- slower, but the user still sees the image.
            resp = send_file(src, mimetype="image/webp", conditional=True)
            resp.headers["Cache-Control"] = cache_control
            return resp

    # 4) BW FF thumbnail pushed live by the station's detection indexer
    # (ffthumbs/ on the storage box, named <chunk_stem>.webp). NOT
    # date-gated: it covers exactly the gap where the colour stack has
    # not reached the archive yet -- tonight, and stations the VPS
    # cannot reach. Served inline with a short TTL and never promoted
    # into the NVMe cache, so the colour stack wins in the tiers above
    # the moment it lands.
    if filename.endswith("_stack.webp"):
        ff_name = filename[: -len("_stack.webp")] + ".webp"
    elif filename.endswith(".webp"):
        ff_name = filename
    else:
        ff_name = None
    if ff_name:
        # Pushed thumbnails land on the VPS disk (ingest_api._store_ffthumb);
        # the archive holds only those pushed before that, through the spool.
        local = ffthumb_local_path(cam, date, ff_name)
        if local.is_file():
            resp = send_file(local, mimetype="image/webp", conditional=True)
            resp.headers["Cache-Control"] = "public, max-age=300"
            return resp
        ff_src = ARCHIVE_PATH / cam / date / "ffthumbs" / ff_name
        try:
            if _with_sshfs_timeout(ff_src.is_file, timeout=5.0, default=False):
                data = _with_sshfs_timeout(ff_src.read_bytes, timeout=10.0, default=None)
                if data is not None:
                    return Response(
                        data, status=200,
                        headers={"Content-Type": "image/webp",
                                 "Cache-Control": "public, max-age=300"},
                    )
        except OSError:
            pass

    # No station tier (#1250): a thumbnail the archive and the pushed
    # ffthumbs do not hold is "not uploaded yet", whatever the night. The
    # dashboard's background thumb prefetch still warms tier 1 from
    # reachable stations; this request path never calls one.
    return upload_state.not_uploaded_response("stack", cam, date, filename)


def _serve_or_archive(host_key: str, archive_subdir: str, endpoint: str,
                      station_code: str, date: str, filename: str,
                      want_mp4: bool | None = None) -> Response:
    """Prefer the storagebox archive (processed/re-encoded clip); fall
    back to the station for tonight's live data or when the archive copy
    doesn't exist yet.

    ``endpoint`` is the station-side route the fallback pulls from
    (``/<endpoint>/<cam>/<date>/<filename>``, see ``_media_url``).
    ``?format=mp4`` on a clip (or ``want_mp4=True``, which overrides the
    query) asks for the stream-copied MP4 (Safari and iOS cannot play
    MKV): hot cache, archive sibling or remux, and after a station pull a
    remux of the pulled file.
    """
    cache = _deps().cache
    is_clip = filename.endswith(".mkv")
    if want_mp4 is None:
        want_mp4 = request.args.get("format") == "mp4"
    want_mp4 = is_clip and want_mp4

    # --- Archive (primary): processed / re-encoded clip on storagebox ---
    # Looked up through the cached night listing and the archive index,
    # so a stalled mount during the dawn upload window neither pins the
    # worker nor reads as "not archived" (which sent the request to a
    # station that had already pruned the chunk).
    if want_mp4:
        hot = clip_cache.serve_hot_mp4(station_code, date, filename)
        if hot is not None:
            return hot
        archived = _serve_archive_clip(station_code, date, filename, want_mp4=True)
        if archived is not None:
            return archived
    else:
        archive_file = archive_media_path(station_code, date, archive_subdir, filename)
        if archive_file is not None:
            mime = "video/mp4" if filename.endswith(".mp4") else (
                "video/x-matroska" if is_clip else (
                    "image/webp" if filename.endswith(".webp") else "application/octet-stream"
                )
            )
            return send_file(archive_file, mimetype=mime, conditional=True,
                             as_attachment=True, download_name=filename)

    # --- Station fallback: only a clip of the night in progress --------
    # Uploaded artifacts (timelapses, night stacks) are archive-only. A
    # clip the archive does not hold yet is pulled from the station only
    # for the night still in progress and only while its pushed status
    # says it is online: an explicit play click on live footage, the one
    # station read this path keeps (#1250). Everything else answers the
    # shared "not uploaded yet" state without touching the station.
    kind = "clip" if is_clip else archive_subdir
    status = cache.get_status(host_key)
    online = bool(status and status.get("online"))
    live_night = date >= _live_night_floor(datetime.now(timezone.utc))
    if is_clip and online and live_night:
        try:
            url = _media_url(host_key, endpoint, station_code, date, filename)
        except _TunnelDown:
            url = None
        if url is not None:
            # Clips go through the background pull, which never holds the
            # request past nginx's 30 s limit (202 + Retry-After instead).
            resp = serve_station_clip(
                url, CACHE_PATH / station_code / date / filename, want_mp4,
                station_code, date, filename,
            )
            if resp.status_code != 502:
                return resp

    return upload_state.not_uploaded_response(kind, station_code, date, filename)


# ── Media serving routes ─────────────────────────────────────────────

@public_route
def timelapse(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mp4$"
    )
    return _serve_or_archive(host_key, "timelapse", "color_timelapse", station_code, date, filename)


@public_route
def stack_image(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
    )
    return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)


@public_route
def thumbnail(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
    )
    return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)


@public_route
def video_serve(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    return _serve_or_archive(host_key, "meteors", "color_capture", station_code, date, filename)


@public_route
def video_download(host_key: str, station_code: str, date: str, filename: str):
    """The whole clip as an attachment: the stream-copied MP4 (the same
    frames, and it plays in every browser and desktop player without
    VLC), or the original Matroska file with ``?format=mkv``."""
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    want_mp4 = request.args.get("format") != "mkv"
    resp = _serve_or_archive(host_key, "meteors", "color_capture", station_code, date, filename,
                             want_mp4=want_mp4)
    # A 202 "preparing" or an error body is not the clip: only a served
    # file gets the attachment name. Tuples are the 404 fallback.
    if isinstance(resp, Response) and resp.status_code in (200, 206):
        name = filename[:-4] + ".mp4" if want_mp4 else filename
        resp.headers["Content-Disposition"] = f'attachment; filename="{name}"'
    return resp


def _brand_fullstack_bytes(
    host_key: str, station_code: str, filename: str, src: Path,
) -> bytes | None:
    """Return the branded full-res stack for *src*, or None if it can't be produced.

    Renders the ROVIMEN/astromania overlay on demand (already-branded stacks
    pass through untouched via the embedded marker), so no full-res
    per-detection stack is ever downloaded un-attributed.
    """
    config = _deps().config
    tunnels = _deps().tunnels
    overlay_cfg, station_cfg = _crop_overlay_config(config, tunnels, host_key, station_code)
    return stack_overlay.ensure_overlaid_stack_bytes(
        src, filename, overlay_cfg, station_code, station_cfg,
    )


@public_route
def fullstack(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
    )
    cache_control = _thumb_cache_control(date, _live_night_floor())
    is_detection_stack = filename.endswith("_stack.webp") and not filename.endswith(
        "_night_stack.webp"
    )

    # Source the full-resolution stack from the archive (it holds the
    # branding marker for stacks produced after the station overlay
    # landed); the bytes are branded on demand before serving so a
    # downloadable full-res stack is never un-attributed. Archive only: an
    # uploaded artifact is never pulled from the station, so a miss falls
    # back to the (display) thumbnail tiers, whose own miss is "not
    # uploaded yet".
    src_path: Path | None = None
    for subdir in ("stacks", "meteors"):
        cand = ARCHIVE_PATH / station_code / date / subdir / filename
        if _with_sshfs_timeout(cand.is_file, timeout=5.0, default=False):
            src_path = cand
            break
    if src_path is None:
        return _serve_thumbnail(host_key, "thumbnail", station_code, date, filename)

    if is_detection_stack:
        branded = _brand_fullstack_bytes(host_key, station_code, filename, src_path)
        if branded is None:
            abort(503)
        resp = Response(branded, mimetype="image/webp")
        resp.headers["Cache-Control"] = cache_control
        return resp

    resp = send_file(src_path, mimetype="image/webp", conditional=True)
    resp.headers["Cache-Control"] = cache_control
    return resp


@public_route
def night_stack(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.webp$"
    )
    # On the storagebox, the per-night composite (<cam>_<date>_night_stack.webp)
    # lives next to the timelapse mp4 in the <cam>/<date>/timelapse/ subdir,
    # not in stacks/ (which is per-chunk thumbnails).
    return _serve_or_archive(host_key, "timelapse", "night_stack", station_code, date, filename)


@public_route
def timelapse_download(
    host_key: str, station_code: str, date: str, filename: str
):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mp4$"
    )
    return _serve_or_archive(host_key, "timelapse", "color_timelapse", station_code, date, filename)


def _shortclip_archive_fallback(
    station_code: str, date: str, filename: str
) -> Response | None:
    """Archive tiers for a shortclip whose source chunk left the station:
    NVMe hot cache, then the archived faststart ``.mp4`` sibling, then a
    persisted remux of the archived ``.mkv`` -- the same order as
    ``/api/cached-video``. Returns None when the archive has nothing
    either. The payload is the full ~20 s chunk (untrimmed): the ss/t
    trim params need the raw chunk, which only the station had, so the
    widened response carries ``X-Clip-Source: archive-full-chunk`` to
    flag that it was not trimmed to the requested window.
    """
    hot = clip_cache.serve_hot_mp4(station_code, date, filename)
    if hot is not None:
        hot.headers["X-Clip-Source"] = "archive-full-chunk"
        return hot
    stem = filename.rsplit(".", 1)[0]
    # Guard the SSHFS stats (same pattern as _serve_or_archive): a
    # stalled storagebox mount returns False here and the route 404s
    # instead of pinning the worker thread on a dead mount.
    if not _with_sshfs_timeout(
        lambda: any(
            _archive_clip_path(station_code, date, stem + ext) is not None
            for ext in (".mp4", ".mkv")
        ),
        timeout=5.0,
        default=False,
    ):
        return None
    archived = _serve_archive_clip(station_code, date, filename, want_mp4=True)
    if archived is not None:
        archived.headers["X-Clip-Source"] = "archive-full-chunk"
    return archived


def _shortclip_source(station_code: str, date: str, filename: str) -> Path | None:
    """The clip to cut a trimmed download from, fastest copy first: the
    NVMe hot MP4, then the archived MKV, then its MP4 sibling."""
    hot = clip_cache.hot_mp4_path(station_code, date, filename)
    if hot is not None and _is_nonempty_file(hot):
        return hot
    stem = filename.rsplit(".", 1)[0]
    for name in (stem + ".mkv", stem + ".mp4"):
        src = archive_clip_path_bounded(station_code, date, name)
        if src is not None:
            return src
    return None


def _untrimmed(resp: Response) -> Response:
    """Flag a whole-clip answer to a trimmed-download request, so the
    page can say the trim could not be applied."""
    resp.headers["X-Clip-Source"] = "archive-full-chunk"
    resp.headers["X-Clip-Trimmed"] = "0"
    return resp


@require_station
def shortclip(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    # Archive first. The archived clip is the same overlaid chunk the
    # station would cut from, it is local, and the station path waits on
    # an on-demand overlay render (~1 min) that nginx's 30 s limit cut
    # off before the old archive fallback could run. The window is the
    # trim UI's ss/t, else pre/post around the detection time recorded
    # in the archived state.json.
    src = _shortclip_source(station_code, date, filename)
    if src is not None:
        window = None
        try:
            if "ss" in request.args and "t" in request.args:
                window = (max(0.0, min(300.0, float(request.args["ss"]))),
                          min(120.0, max(0.5, float(request.args["t"]))))
            else:
                pre = max(0.0, min(60.0, float(request.args.get("pre", 2))))
                post = max(0.0, min(60.0, float(request.args.get("post", 5))))
                window = archive_detection_window(station_code, date, filename, pre, post)
        except (TypeError, ValueError):
            abort(400)
        if window is not None:
            cut = trim_clip_response(src, window[0], window[1], filename)
            if cut is not None:
                cut.headers["X-Clip-Source"] = "archive"
                return cut
        # No known window, or the cut failed: the whole clip, flagged.
        archived = _shortclip_archive_fallback(station_code, date, filename)
        if archived is not None:
            return _untrimmed(archived)

    # Not archived yet (tonight): only the station has it.
    try:
        url = _media_url(host_key, "api/shortclip", station_code, date, filename)
    except _TunnelDown:
        url = None
    if url is not None:
        qs = request.query_string.decode()
        if qs:
            url += f"?{qs}"
        # Always proxy -- never redirect. A redirect to the station IP is
        # cross-origin, and browsers ignore the `download` attribute for
        # cross-origin URLs, causing the browser to navigate to the URL
        # instead of downloading the file.
        try:
            # Bounded below nginx's 30 s: the station may first render the
            # overlay (~1 min). It keeps going after we give up, so a retry
            # a minute later finds it ready.
            resp = _session_for_url(url).get(
                url, timeout=(5, STATION_WAIT_S), stream=True)
            if resp.status_code == 200:
                out_headers: dict[str, str] = {
                    "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
                    "X-Clip-Trimmed": "1",
                }
                for h in ("Content-Length", "Content-Disposition"):
                    if h in resp.headers:
                        out_headers[h] = resp.headers[h]
                return Response(
                    _streaming_proxy(resp),
                    status=200,
                    headers=out_headers,
                )
            # Drop the upstream HTML 404 body -- otherwise the browser
            # saves it as <filename>.html when the user clicked download.
            try:
                resp.close()
            except Exception:
                pass
        except requests.exceptions.ReadTimeout:
            busy = jsonify({
                "error": "shortclip_preparing",
                "detail": ("The station is still preparing this clip (it "
                           "burns in the overlay first). Try again in a minute."),
                "retry_after_seconds": 60,
            })
            busy.status_code = 503
            busy.headers["Retry-After"] = "60"
            return busy
        except Exception:
            pass
    archived = _shortclip_archive_fallback(station_code, date, filename)
    if archived is not None:
        return _untrimmed(archived)
    return jsonify({
        "error": "shortclip_unavailable",
        "detail": (
            f"{filename} is no longer on the station (likely pruned by "
            f"color_days retention) and has no copy in the archive."
        ),
    }), 404


@require_station
def stitch_video(host_key: str, station_code: str, date: str):
    config = _deps().config
    tunnels = _deps().tunnels
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", station_code):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    url = station_url(config, tunnels, host_key, f"/api/stitch_video/{station_code}/{date}")
    qs = request.query_string.decode()
    if qs:
        url += f"?{qs}"
    try:
        resp = _session_for_url(url).get(url, timeout=60, stream=True)
        if resp.status_code != 200:
            try:
                resp.close()
            except Exception:
                pass
            return jsonify({
                "error": "stitch_unavailable",
                "detail": "one or both source chunks no longer on the station",
            }), 404
        out_headers: dict[str, str] = {
            "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
            "Accept-Ranges": "bytes",
        }
        for h in ("Content-Length", "Content-Range", "Content-Disposition"):
            if h in resp.headers:
                out_headers[h] = resp.headers[h]
        return Response(
            _streaming_proxy(resp),
            status=200,
            headers=out_headers,
        )
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


def _archived_stitch_name(station_code: str, date: str) -> str | None:
    """Archived boundary-splice filename for the f1/f2 pair in the query
    string, or None.

    ``detection_lock._stitch_clips`` uploads exactly one splice
    (``<cam>_<date>_<Tstart>_<Tend>_color.mkv``) per chunk edge, spanning
    half a segment either side of the boundary. The boundary is f2's
    start time, so match it against the stitched names in the archive
    index rather than re-deriving detection_lock's half-segment
    arithmetic (the segment duration is station config we don't have).
    """
    archive_idx = _deps().archive_idx
    f2 = request.args.get("f2", "")
    m = re.match(r"^[A-Z0-9]+_(\d{8})_(\d{6})_", f2)
    if not m:
        return None
    d, t = m.group(1), m.group(2)
    boundary = f"{d[0:4]}-{d[4:6]}-{d[6:8]} {t[0:2]}:{t[2:4]}:{t[4:6]}"
    ni = archive_idx.night_files(station_code, date)
    if ni is None:
        return None
    return _resolve_stitched_chunk(boundary, ni.meteor_files)


@require_station
def shortclip_stitch(host_key: str, station_code: str, date: str):
    config = _deps().config
    tunnels = _deps().tunnels
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", station_code):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    # Station first (it concatenates the two raw chunks and trims); when
    # the chunks are pruned, fall back to the boundary splice the station
    # uploaded to the archive when it locked this meteor.
    try:
        url = station_url(config, tunnels, host_key, f"/api/shortclip_stitch/{station_code}/{date}")
    except _TunnelDown:
        url = None
    if url is not None:
        qs = request.query_string.decode()
        if qs:
            url += f"?{qs}"
        try:
            resp = _session_for_url(url).get(url, timeout=120, stream=True)
            if resp.status_code == 200:
                out_headers: dict[str, str] = {
                    "Content-Type": resp.headers.get("Content-Type", "video/mp4"),
                }
                for h in ("Content-Length", "Content-Disposition"):
                    if h in resp.headers:
                        out_headers[h] = resp.headers[h]
                return Response(
                    _streaming_proxy(resp),
                    status=200,
                    headers=out_headers,
                )
            # Drop the upstream 404 body (see shortclip above).
            try:
                resp.close()
            except Exception:
                pass
        except Exception:
            pass
    stitched = _archived_stitch_name(station_code, date)
    if stitched is not None:
        archived = _shortclip_archive_fallback(station_code, date, stitched)
        if archived is not None:
            return archived
    return jsonify({
        "error": "shortclip_stitch_unavailable",
        "detail": (
            "the source chunks are no longer on the station and the "
            "archive holds no stitched splice for this chunk boundary"
        ),
    }), 404


@require_station
def loop_clip(host_key: str, station_code: str, date: str, filename: str):
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    try:
        start = float(request.args.get("start", 0))
        end   = float(request.args.get("end",   0))
        loops = max(1, min(10, int(request.args.get("loops", 2))))
    except (ValueError, TypeError):
        abort(400)
    if end <= start:
        abort(400)
    duration = end - start

    # Fetch the trimmed segment from the station's shortclip endpoint
    seg_url = _media_url(host_key, "api/shortclip", station_code, date, filename)
    seg_url += f"?ss={start:.3f}&t={duration:.3f}"
    try:
        resp = _session_for_url(seg_url).get(seg_url, timeout=120, stream=True)
        if resp.status_code != 200:
            try:
                resp.close()
            except Exception:
                pass
            return jsonify({"error": "source_unavailable",
                            "detail": "clip not found on station"}), 404

        with tempfile.TemporaryDirectory(prefix="rovimen_media_") as tmpdir:
            seg_path = f"{tmpdir}/segment.mp4"
            with open(seg_path, "wb") as f:
                for chunk in resp.iter_content(65536):
                    f.write(chunk)
            stem = filename.rsplit(".", 1)[0]
            return looped_clip_response(seg_path, loops, f"{stem}_loop{loops}x.mp4")
    except subprocess.CalledProcessError as exc:
        logger.error("loop_clip ffmpeg failed: %s", exc.stderr)
        abort(500)
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


@require_station
def download_cropped(host_key: str, station_code: str, date: str, filename: str):
    """Crop a trimmed clip to a region of interest and scale to 1280×720."""
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
    try:
        x  = max(0.0, min(0.99, float(request.args.get("x", 0))))
        y  = max(0.0, min(0.99, float(request.args.get("y", 0))))
        w  = max(0.01, min(1.0,  float(request.args.get("w", 1))))
        h  = max(0.01, min(1.0,  float(request.args.get("h", 1))))
        ss = max(0.0,  float(request.args.get("ss", 0)))
        t  = max(0.1,  float(request.args.get("t",  20)))
    except (ValueError, TypeError):
        abort(400)
    # Clamp so crop doesn't exceed frame bounds
    w = min(w, 1.0 - x)
    h = min(h, 1.0 - y)

    out_fd, out_str = tempfile.mkstemp(suffix=".mp4", prefix="crop_")
    os.close(out_fd)
    out_path = out_str
    try:
        with tempfile.TemporaryDirectory(prefix="rovimen_media_") as tmpdir:
            # Resolve source — archive (processed, with overlay) first,
            # then station's color_capture endpoint as fallback.
            archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
            if archive_file.exists() and archive_file.is_file():
                in_path = str(archive_file)
            else:
                try:
                    src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                except Exception:
                    os.unlink(out_path)
                    return jsonify({"error": "source_unavailable",
                                    "detail": "station unreachable"}), 502
                resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                if resp.status_code != 200:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    os.unlink(out_path)
                    return jsonify({"error": "source_unavailable",
                                    "detail": "clip not found"}), 404
                download_in = f"{tmpdir}/input.mkv"
                try:
                    ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                finally:
                    resp.close()
                if not ok:
                    os.unlink(out_path)
                    return jsonify({"error": "source_too_large",
                                    "detail": "clip exceeds size cap"}), 413
                in_path = download_in

            overlay_cfg, station_cfg_ov = _crop_overlay_config(config, tunnels, host_key, station_code)
            cmd = _build_crop_cmd(
                in_path, out_path, x, y, w, h,
                station_code, filename, ss, t,
                overlay_cfg, station_cfg_ov,
            )
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)

        stem    = filename.rsplit(".", 1)[0]
        outname = f"{stem}_crop.mp4"
        resp_out = send_file(
            out_path, mimetype="video/mp4",
            as_attachment=True, download_name=outname,
        )
        resp_out.call_on_close(lambda: _silent_unlink_path(out_path))
        return resp_out
    except subprocess.CalledProcessError as exc:
        _silent_unlink_path(out_path)
        logger.error("download_cropped ffmpeg failed: %s", exc.stderr)
        abort(500)
    except Exception as exc:
        _silent_unlink_path(out_path)
        return _proxy_error_response(host_key, exc)


@require_station
def download_cropped_frame(host_key: str, station_code: str, date: str, filename: str):
    """Extract a single cropped frame with the same station overlay as clips."""
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
    try:
        x  = max(0.0, min(0.99, float(request.args.get("x", 0))))
        y  = max(0.0, min(0.99, float(request.args.get("y", 0))))
        w  = max(0.01, min(1.0,  float(request.args.get("w", 1))))
        h  = max(0.01, min(1.0,  float(request.args.get("h", 1))))
        ss = max(0.0,  float(request.args.get("ss", 0)))
    except (ValueError, TypeError):
        abort(400)
    w = min(w, 1.0 - x)
    h = min(h, 1.0 - y)

    try:
        with tempfile.TemporaryDirectory(prefix="rovimen_media_") as tmpdir:
            archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
            if archive_file.exists() and archive_file.is_file():
                in_path = str(archive_file)
            else:
                try:
                    src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                except Exception:
                    return jsonify({"error": "source_unavailable",
                                    "detail": "station unreachable"}), 502
                resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                if resp.status_code != 200:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return jsonify({"error": "source_unavailable",
                                    "detail": "clip not found"}), 404
                download_in = f"{tmpdir}/input.mkv"
                try:
                    ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                finally:
                    resp.close()
                if not ok:
                    return jsonify({"error": "source_too_large",
                                    "detail": "clip exceeds size cap"}), 413
                in_path = download_in

            overlay_cfg, station_cfg_ov = _crop_overlay_config(config, tunnels, host_key, station_code)
            out_path = f"{tmpdir}/frame.jpg"
            cmd = _build_crop_cmd(
                in_path, out_path, x, y, w, h,
                station_code, filename, ss, 0,
                overlay_cfg, station_cfg_ov,
                vframes=1,
            )
            subprocess.run(cmd, check=True, capture_output=True, timeout=60)
            with open(out_path, "rb") as f:
                data = f.read()

        stem    = filename.rsplit(".", 1)[0]
        sec_str = f"{ss:.2f}".replace(".", "s")
        outname = f"{stem}_crop_{sec_str}.jpg"
        return Response(
            data, status=200,
            headers={
                "Content-Type": "image/jpeg",
                "Content-Disposition": f'attachment; filename="{outname}"',
                "Content-Length": str(len(data)),
            },
        )
    except subprocess.CalledProcessError as exc:
        logger.error("download_cropped_frame ffmpeg failed: %s", exc.stderr)
        abort(500)
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


@require_station
def cropped_loop_clip(host_key: str, station_code: str, date: str, filename: str):
    """Crop a trimmed clip, add overlay, and loop N times."""
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(host_key, station_code, date, filename, r"^[\w._-]+\.mkv$")
    try:
        x     = max(0.0, min(0.99, float(request.args.get("x", 0))))
        y     = max(0.0, min(0.99, float(request.args.get("y", 0))))
        w     = max(0.01, min(1.0,  float(request.args.get("w", 1))))
        h     = max(0.01, min(1.0,  float(request.args.get("h", 1))))
        ss    = max(0.0,  float(request.args.get("ss", 0)))
        t     = max(0.1,  float(request.args.get("t",  20)))
        loops = max(1, min(10, int(request.args.get("loops", 2))))
    except (ValueError, TypeError):
        abort(400)
    w = min(w, 1.0 - x)
    h = min(h, 1.0 - y)

    try:
        with tempfile.TemporaryDirectory(prefix="rovimen_media_") as tmpdir:
            archive_file = ARCHIVE_PATH / station_code / date / "meteors" / filename
            if archive_file.exists() and archive_file.is_file():
                in_path = str(archive_file)
            else:
                try:
                    src_url = _media_url(host_key, "color_capture", station_code, date, filename)
                except Exception:
                    return jsonify({"error": "source_unavailable",
                                    "detail": "station unreachable"}), 502
                resp = _session_for_url(src_url).get(src_url, timeout=120, stream=True)
                if resp.status_code != 200:
                    try:
                        resp.close()
                    except Exception:
                        pass
                    return jsonify({"error": "source_unavailable",
                                    "detail": "clip not found"}), 404
                download_in = f"{tmpdir}/input.mkv"
                try:
                    ok = _stream_download_capped(resp, download_in, _CROP_SOURCE_MAX_BYTES)
                finally:
                    resp.close()
                if not ok:
                    return jsonify({"error": "source_too_large",
                                    "detail": "clip exceeds size cap"}), 413
                in_path = download_in

            overlay_cfg, station_cfg_ov = _crop_overlay_config(config, tunnels, host_key, station_code)
            seg_path = f"{tmpdir}/segment.mp4"
            cmd = _build_crop_cmd(
                in_path, seg_path, x, y, w, h,
                station_code, filename, ss, t,
                overlay_cfg, station_cfg_ov,
            )
            subprocess.run(cmd, check=True, capture_output=True, timeout=180)
            stem = filename.rsplit(".", 1)[0]
            return looped_clip_response(seg_path, loops, f"{stem}_crop_loop{loops}x.mp4")
    except subprocess.CalledProcessError as exc:
        logger.error("cropped_loop_clip ffmpeg failed: %s", exc.stderr)
        abort(500)
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


@require_station
def api_batch_download(host_key: str, station_code: str, date: str):
    config = _deps().config
    tunnels = _deps().tunnels
    if not re.match(r"^[A-Z0-9]+$", station_code, re.IGNORECASE):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)

    filenames = request.form.getlist("files")
    if not filenames or len(filenames) > 50:
        abort(400)
    for fn in filenames:
        if not re.match(r"^[\w._-]+_color\.mkv$", fn):
            abort(400)

    # Shared aggregate-size budget across the parallel fetch workers. The
    # old code buffered every file in RAM and then built the whole ZIP in
    # a second in-RAM buffer (peak ~2x the data) -- a single 50-file
    # request could OOM the worker. We now stream each fetched file to a
    # temp dir on disk, enforce a per-request aggregate cap, and stream the
    # ZIP back to the client without holding it all in memory.
    budget_lock = threading.Lock()
    budget = {"used": 0}
    tmpdir = tempfile.mkdtemp(prefix="batchdl_")

    def _fetch_one(args: tuple[int, str]) -> tuple[str, str, Path] | None:
        # Combine lock-type lookup + media fetch into a single future
        # per file so 8 workers run the (lock + media) pairs in
        # parallel. The previous serial loop paid ~125 ms x 2 RTT
        # x N files at minimum; the bottleneck is now the slowest
        # single file rather than their sum.
        idx, fn = args
        try:
            lock_url = station_url(
                config, tunnels, host_key,
                f"/api/lock/{station_code}/{date}/{fn}",
            )
            sess = _session_for_url(lock_url)
        except Exception:
            return None
        lock_type = None
        try:
            lr = sess.get(lock_url, timeout=5)
            if lr.ok:
                lock_type = lr.json().get("lock_type")
        except Exception:
            pass

        if lock_type == "detection":
            media_url = station_url(
                config, tunnels, host_key,
                f"/api/shortclip/{station_code}/{date}/{fn}",
            )
            zip_name = fn.replace("_color.mkv", "_clip.mp4")
        else:
            media_url = station_url(
                config, tunnels, host_key,
                f"/color_capture/{station_code}/{date}/{fn}",
            )
            zip_name = fn

        out_path = Path(tmpdir) / f"{idx:03d}.part"
        try:
            resp = _session_for_url(media_url).get(
                media_url, timeout=120, stream=True,
            )
            if not resp.ok:
                resp.close()
                return None
            written = 0
            try:
                with open(out_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        written += len(chunk)
                        # Per-file cap (a single bad chunk can't run away)...
                        if written > _BATCH_PER_FILE_MAX_BYTES:
                            return None
                        # ...and the shared per-request aggregate cap.
                        with budget_lock:
                            if budget["used"] + len(chunk) > _BATCH_TOTAL_MAX_BYTES:
                                return None
                            budget["used"] += len(chunk)
                        f.write(chunk)
            finally:
                resp.close()
            return fn, zip_name, out_path
        except Exception:
            return None

    results: dict[str, tuple[str, Path]] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        for r in pool.map(_fetch_one, list(enumerate(filenames))):
            if r is None:
                continue
            fn, zip_name, out_path = r
            results[fn] = (zip_name, out_path)

    # Preserve the caller-supplied order so the zip matches the selection
    # UI on the client side.
    entries = [
        (results[fn][0], results[fn][1])
        for fn in filenames
        if fn in results
    ]

    zip_name  = f"rovimen_{station_code}_{date}.zip"
    return Response(
        stream_with_context(
            _stream_zip_from_files(entries, cleanup_dir=tmpdir)
        ),
        mimetype="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@require_station_lock
def api_lock(
    host_key: str, station_code: str, date: str, filename: str
):
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    try:
        url = station_url(
            config, tunnels, host_key,
            f"/api/lock/{station_code}/{date}/{filename}",
        )
        resp = _session_for_url(url).post(
            url,
            json=request.get_json(silent=True),
            timeout=10,
        )
        resp.raise_for_status()
        return jsonify(resp.json())
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


@require_station_lock
def api_keep(
    host_key: str, station_code: str, date: str, filename: str
):
    """Keep a clip a signed-in user is playing, as if they had locked it.

    Records it in ``kept_clips`` (the Storage Box janitor never prunes a
    listed clip), then locks it on its station, which uploads it at once
    instead of waiting for dawn. The station keeps an existing lock, so a
    detection stays a detection. A chunk the station no longer holds
    (already aged out locally, or never captured there) is kept on the box
    only: ``on_station`` is false. ``filename`` may be any served name of
    the clip (``.mp4``, ``.details.*``); the chunk is its ``_color.mkv``.
    """
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(
        host_key, station_code, date, filename, kept_clips.CLIP_NAME_RE.pattern
    )
    chunk = kept_clips.chunk_name(filename)
    if chunk is None:
        abort(400)
    try:
        kept_clips.record(station_code, date, chunk, session.get("user"))
    except OSError:
        logger.exception("kept clips: could not record %s/%s/%s",
                         station_code, date, chunk)
        return jsonify({"kept": False, "error": "could not record the clip"}), 500
    try:
        url = station_url(
            config, tunnels, host_key, f"/api/lock/{station_code}/{date}/{chunk}",
        )
        resp = _session_for_url(url).post(url, json={"locked": True}, timeout=10)
        if resp.status_code == 404:
            return jsonify({"kept": True, "on_station": False, "locked": None})
        resp.raise_for_status()
        return jsonify({"kept": True, "on_station": True, **resp.json()})
    except Exception as exc:
        # The keep is recorded; only the station's immediate upload is
        # missing, and the caller has nothing it could do about that.
        logger.warning("keep %s/%s/%s: station lock failed: %s",
                       host_key, station_code, chunk, exc)
        return jsonify({"kept": True, "on_station": None, "locked": None})


@require_station
def api_process_clip(
    host_key: str, station_code: str, date: str, filename: str
):
    """Deliver one clip to the archive: proxy the station's clip-scoped
    POST /api/process_clip (lock if unlocked + re-encode + archive upload).
    ``require_station`` gates this write to admins and the owning host.
    The station endpoint is on its read-only guard's network allowlist
    (it only preserves data); a whole-night dawn run is SSH-only.

    A clip already delivered to the archive (``meteors/<filename>``; the
    ``reencoded`` flag gates that upload) is refused with 409 unless
    ``?force=1``: a second run would redo delivered work. The station
    applies the same rule from its own state.json, so the refusal holds
    even when this check is shed by a stalled mount."""
    config = _deps().config
    tunnels = _deps().tunnels
    _validate_media_params(
        host_key, station_code, date, filename, r"^[\w._-]+\.mkv$"
    )
    force = request.args.get("force") == "1"
    archived = ARCHIVE_PATH / station_code / date / "meteors" / filename
    if not force and _with_sshfs_timeout(
        archived.is_file, timeout=5.0, default=False,
    ):
        return jsonify({
            "ok": False, "processed": True,
            "error": "already processed: the clip is in the archive",
        }), 409
    try:
        url = station_url(
            config, tunnels, host_key,
            f"/api/process_clip/{station_code}/{date}/{filename}"
            + ("?force=1" if force else ""),
        )
        resp = _session_for_url(url).post(url, timeout=15)
        return jsonify(resp.json()), resp.status_code
    except Exception as exc:
        return _proxy_error_response(host_key, exc)


# ── Live camera stream (proxied via SSH+ffmpeg on station) ────────────

@require_station
def stream(host_key: str, camera_code: str):
    config = _deps().config
    tunnels = _deps().tunnels
    _live_stream_triple_sem = _deps().live_stream_triple_sem
    _live_stream_global_sem = _deps().live_stream_global_sem
    _LIVE_STREAM_RETRY_AFTER_S = _deps().live_stream_retry_after_s
    station = _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", camera_code, re.IGNORECASE):
        abort(400)

    cam = next((c for c in station.cameras if c.code == camera_code), None)
    if cam is None:
        abort(404)

    # Concurrency cap: per-(user, host, camera) + global. Both are
    # acquired non-blockingly so a refused request fails fast with 429
    # rather than tying up a Flask worker.
    user = session.get("user") or "anonymous"
    triple_sem = _live_stream_triple_sem(user, host_key, camera_code)
    if not triple_sem.acquire(blocking=False):
        return jsonify({
            "error": "too_many_streams",
            "scope": "per_user_station_camera",
            "retry_after_s": _LIVE_STREAM_RETRY_AFTER_S,
        }), 429
    if not _live_stream_global_sem.acquire(blocking=False):
        triple_sem.release()
        return jsonify({
            "error": "too_many_streams",
            "scope": "global",
            "retry_after_s": _LIVE_STREAM_RETRY_AFTER_S,
        }), 429

    rotate_flag = "1" if cam.rotate else "0"
    stream_path = f"/api/stream/{camera_code}?rotate={rotate_flag}"

    try:
        url = station_url(config, tunnels, host_key, stream_path)
    except _TunnelDown:
        triple_sem.release()
        _live_stream_global_sem.release()
        return jsonify({"error": "station_unreachable"}), 502

    try:
        upstream = requests.get(url, stream=True, timeout=(8, 60))
        upstream.raise_for_status()
    except Exception as exc:
        triple_sem.release()
        _live_stream_global_sem.release()
        logger.warning("Live stream %s/%s failed: %s", host_key, camera_code, exc)
        return jsonify({"error": "stream_unavailable"}), 502

    def relay():
        try:
            for chunk in upstream.iter_content(chunk_size=32768):
                if chunk:
                    yield chunk
        except GeneratorExit:
            pass
        finally:
            upstream.close()
            try:
                _live_stream_global_sem.release()
            except ValueError:
                pass
            try:
                triple_sem.release()
            except ValueError:
                pass

    return Response(
        relay(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Registration ────────────────────────────────────────────────────────

def register_media_routes(
    app: Flask,
    config: DashboardConfig,
    tunnels: TunnelManager,
    cache: StationCache,
    *,
    archive_idx: _ArchiveIndex,
    prefetch_executor: ThreadPoolExecutor,
    prefetch_archive_thumbs: Callable[..., None],
    live_stream_triple_sem: Callable[[str, str, str], threading.BoundedSemaphore],
    live_stream_global_sem: threading.BoundedSemaphore,
    live_stream_retry_after_s: int,
    shared_videodb: SharedSnapshotStore | None = None,
) -> None:
    """Attach the videodb, media-serving, clip-cutting, lock/keep/process and
    live-stream routes.

    Stores the dependencies (see ``MediaDeps``) in ``_DEPS`` for the
    module-level handlers and helpers, replacing any earlier registration's,
    then adds the URL rules. Each endpoint name is the view function's name,
    as ``@app.route`` gave it.
    """
    global _DEPS
    _DEPS = MediaDeps(
        config=config,
        tunnels=tunnels,
        cache=cache,
        archive_idx=archive_idx,
        prefetch_executor=prefetch_executor,
        prefetch_archive_thumbs=prefetch_archive_thumbs,
        live_stream_triple_sem=live_stream_triple_sem,
        live_stream_global_sem=live_stream_global_sem,
        live_stream_retry_after_s=live_stream_retry_after_s,
        # Defaulting to a disabled store keeps callers that pass none --
        # tests, single-worker dev -- on the exact per-worker behaviour.
        shared_videodb=shared_videodb or SharedSnapshotStore(None, namespace="videodb"),
    )
    app.add_url_rule("/api/videodb/nights/<host_key>/<cam_code>",
                     view_func=api_videodb_nights)
    app.add_url_rule("/api/videodb/rmsnights/<host_key>/<cam_code>",
                     view_func=api_videodb_rmsnights)
    app.add_url_rule("/api/videodb/rms-detections/<host_key>/<cam_code>/<date>",
                     view_func=api_videodb_rms_detections)
    app.add_url_rule("/api/videodb/chunks/<host_key>/<cam_code>/<date>",
                     view_func=api_videodb_chunks)
    app.add_url_rule("/timelapse/<host_key>/<station_code>/<date>/<filename>",
                     view_func=timelapse)
    app.add_url_rule("/stack/<host_key>/<station_code>/<date>/<filename>",
                     view_func=stack_image)
    app.add_url_rule("/thumbnail/<host_key>/<station_code>/<date>/<filename>",
                     view_func=thumbnail)
    app.add_url_rule("/video/<host_key>/<station_code>/<date>/<filename>",
                     view_func=video_serve)
    app.add_url_rule("/download/<host_key>/<station_code>/<date>/<filename>",
                     view_func=video_download)
    app.add_url_rule("/fullstack/<host_key>/<station_code>/<date>/<filename>",
                     view_func=fullstack)
    app.add_url_rule("/night_stack/<host_key>/<station_code>/<date>/<filename>",
                     view_func=night_stack)
    app.add_url_rule("/timelapse_download/<host_key>/<station_code>/<date>/<filename>",
                     view_func=timelapse_download)
    app.add_url_rule("/shortclip/<host_key>/<station_code>/<date>/<filename>",
                     view_func=shortclip)
    app.add_url_rule("/stitch_video/<host_key>/<station_code>/<date>",
                     view_func=stitch_video)
    app.add_url_rule("/shortclip_stitch/<host_key>/<station_code>/<date>",
                     view_func=shortclip_stitch)
    app.add_url_rule("/loop_clip/<host_key>/<station_code>/<date>/<filename>",
                     view_func=loop_clip)
    app.add_url_rule("/download_cropped/<host_key>/<station_code>/<date>/<filename>",
                     view_func=download_cropped)
    app.add_url_rule("/download_cropped_frame/<host_key>/<station_code>/<date>/<filename>",
                     view_func=download_cropped_frame)
    app.add_url_rule("/cropped_loop_clip/<host_key>/<station_code>/<date>/<filename>",
                     view_func=cropped_loop_clip)
    app.add_url_rule("/api/batch-download/<host_key>/<station_code>/<date>",
                     view_func=api_batch_download, methods=["POST"])
    app.add_url_rule("/api/lock/<host_key>/<station_code>/<date>/<filename>",
                     view_func=api_lock, methods=["POST"])
    app.add_url_rule("/api/keep/<host_key>/<station_code>/<date>/<filename>",
                     view_func=api_keep, methods=["POST"])
    app.add_url_rule("/api/process-clip/<host_key>/<station_code>/<date>/<filename>",
                     view_func=api_process_clip, methods=["POST"])
    app.add_url_rule("/stream/<host_key>/<camera_code>", view_func=stream)
