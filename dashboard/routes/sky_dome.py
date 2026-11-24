"""Sky dome rendering, timelapse, latest-frame proxies, and twilight.

Extracted from rovimen_dashboard.py. Wired in from ``create_app()`` via
``register_sky_dome_routes`` (routes) and ``start_dome_scheduler`` (background
daemon thread).

Module layout, the pattern for the dashboard's route modules:

- Every handler and helper is a module-level function, so a helper can be
  imported and unit-tested without building the app.
- What they need from ``create_app`` (``config``, ``tunnels``, ``cache``,
  ``platepar_cache``, ``get_timelapses_payload``) and the per-app live-chunk
  warmer sit in one frozen ``SkyDomeDeps``. ``register_sky_dome_routes``
  builds it and stores it in ``_DEPS``; a function that needs a dependency
  reads it through ``_deps()`` in its first lines. Each registration replaces
  the holder, so in a process that builds several apps (the test suite) the
  last app registered wins; a gunicorn worker builds one app.
- ``register_sky_dome_routes`` is the only code that touches the Flask app:
  it sets ``_DEPS`` and adds the URL rules with ``app.add_url_rule``. The
  endpoint names stay the function names (``api_sky_dome``) and
  ``@require_station`` / ``@public_route`` stay on the functions, so the auth
  gate (``security._matched_public_view``) and the CSRF exemption see the same
  views as when the routes were closures. There is no Blueprint: it would
  rename every endpoint to ``sky_dome.<name>``, and nothing here uses
  blueprint hooks or ``url_for``.
- The pre-render scheduler ``_sky_dome_scheduler(deps)`` is a module-level
  function; ``start_dome_scheduler`` starts it with the registered deps.

Process-wide state (render locks, TTL constants, scheduler dicts) is
module-private. ``platepar_cache`` is the dict the ``/api/platepar`` route in
``routes/station_proxy.py`` fills, so both read the same cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from atomic_write import write_atomic

from flask import Flask, Response, abort, jsonify, request, send_file

from cache_store import THUMB_CACHE_DIR, _thumb_cache_has_space, ARCHIVE_PATH

from astronomy import (_compute_twilight, _compute_moon_phase, _compute_moon_rise_set,
                       capture_window, network_capture_window)
from auth import require_station, station_is_public_for_request
from capture_state import sun_altitude_deg
from http_caching import _json_cached
import pushed_calibration
from route_helpers import lookup_station, media_url as _rh_media_url
from security import public_route
from station_client import station_url, station_get_raw, _session_for_url

if TYPE_CHECKING:
    from models import DashboardConfig, StationConfig
    from station_client import StationCache
    from tunnels import TunnelManager

logger = logging.getLogger(__name__)

# ── Dome cache TTLs ─────────────────────────────────────────────────────
# Night TTL is the *fallback* freshness window: if the background
# scheduler stalls (slow station, mid-warmup, transient timeout), the
# next /api/sky_dome GET re-renders synchronously after this many
# seconds. Kept just above the scheduler's 60 s cadence so a healthy
# station serves the pre-rendered file, but a stalled one self-heals
# within ~90 s instead of going stale for five minutes.
_SKY_DOME_NIGHT_TTL = 90    # 90 s — slightly above scheduler cadence
# Day source is "static" only in the sense that no fresh capture is
# happening — but a station that just came online at noon (or a config
# change pointing latest_stack at a new file) would otherwise wait an
# hour for the first colour stack. 30 min is a workable compromise
# between bandwidth and "stale-on-first-load" UX (P1-43).
_SKY_DOME_DAY_TTL   = 1800  # 30 min — source is static-ish
# A pushed live frame older than this is "no recent frame": the station has
# stopped pushing, and a day-old sky would read as tonight's.
_LIVE_FRAME_MAX_AGE_S = 6 * 3600

# ── Per-station render serialisation ────────────────────────────────────
_sky_dome_locks: dict[tuple, threading.Lock] = {}
_sky_dome_locks_lock = threading.Lock()

# ── Sky-dome pre-render scheduler constants ─────────────────────────────
# Cadences are env-overridable so the VPS can be throttled without a redeploy
# (each 1600x1600 render is ~1-2 CPU-s x cameras x 2 variants; the whole-fleet
# load scales with station count / cadence — see docs/reversed_http_push_design.md §9.2).
_DOME_PRERENDER_INTERVAL_NIGHT_S = int(os.environ.get("ROVIMEN_DOME_INTERVAL_NIGHT_S", "60"))  # night cadence (sun < 0°)
_DOME_PRERENDER_INTERVAL_DAY_S = int(os.environ.get("ROVIMEN_DOME_INTERVAL_DAY_S", "1800"))    # day cadence (P1-43)
_DOME_TIMELAPSE_RETENTION_DAYS = 92
_DOME_FAILURE_MUTE_THRESHOLD = 3           # consecutive failures before muting
# A brief network blip used to freeze a station's dome for 10 minutes,
# which is far longer than the user is willing to stare at a stale
# widget while clouds roll in. 2 min still avoids beating on a truly
# dead station every cycle, but recovers within one "feels live" window.
_DOME_FAILURE_MUTE_DURATION_S = 120        # 2 min mute
_DOME_FRAME_FPS = 12                       # output MP4 framerate
_DOME_COLOUR_NIGHT_INTERVAL_S = int(os.environ.get("ROVIMEN_DOME_COLOUR_INTERVAL_NIGHT_S", "600"))  # colour cadence at night
_DOME_TIMELAPSE_TARGET_FRAMES = 200        # how many dome frames to render from camera timelapses
_DOME_TIMELAPSE_PX = 800                   # dome resolution for timelapse (lower than dashboard 1600)

# ── Scheduler runtime state ─────────────────────────────────────────────
_dome_next_render: dict[str, float] = {}              # host_key -> next render epoch
_dome_consecutive_failures: dict[str, int] = {}       # host_key -> count
_dome_failure_mute_until: dict[str, float] = {}       # host_key -> epoch


def _dome_cache_is_fresh(path: Path, max_age_s: float, now: float) -> bool:
    """True when the cached dome PNG at ``path`` exists and is younger than
    ``max_age_s`` seconds relative to ``now``.

    Missing / unreadable -> False (so the caller renders). This is the freshness
    gate that stops a recycled or respawned worker from re-rendering the whole
    fleet on startup — the render storm that pegged the VPS CPU and tripped the
    gunicorn WORKER TIMEOUT -> SIGKILL -> respawn loop that froze the dashboard.
    """
    try:
        return (now - path.stat().st_mtime) < max_age_s
    except OSError:
        return False


def _write_atomic(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` through a temp file beside it and
    ``os.replace``, so nothing ever reads a half-written file. The temp name
    is unique per process and thread: the route and the scheduler (in any
    worker) can write the same dome at once."""
    write_atomic(
        path, data, mkdir=True,
        tmp=path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"),
    )


def _encode_dome_timelapse(frames_dir: Path, out: Path, label: str) -> bool:
    """Stitch ``frames_dir/*.png`` into the MP4 ``out`` at _DOME_FRAME_FPS.

    ffmpeg writes ``<out>.part`` and only a finished encode is renamed into
    place: the scheduler records the night's attempt marker before the encode
    and never retries it, so an encode cut short (timeout, ffmpeg error, a
    killed worker) must not leave a broken MP4 that the dates route lists and
    the MP4 route serves. ``label`` (``host/night``) is for the log.
    """
    part = out.with_name(out.name + ".part")
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(_DOME_FRAME_FPS),
        "-pattern_type", "glob",
        "-i", str(frames_dir / "*.png"),
        "-c:v", "libx264", "-preset", "veryfast",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        "-f", "mp4", str(part),
    ]
    try:
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=300, check=False)
        except FileNotFoundError:
            logger.warning("dome timelapse build: ffmpeg not on PATH")
            return False
        except subprocess.TimeoutExpired:
            logger.warning("dome timelapse build: ffmpeg stitch timed out for %s", label)
            return False
        if result.returncode != 0:
            tail = result.stderr.decode(errors="replace")[-500:] if result.stderr else ""
            logger.warning("dome timelapse build: ffmpeg stitch failed for %s: %s",
                           label, tail)
            return False
        os.replace(part, out)
        return True
    finally:
        try:
            part.unlink(missing_ok=True)
        except OSError:
            pass


# ── Dome timelapse paths ────────────────────────────────────────────────

def _sky_dome_timelapse_dir(host_key: str) -> Path:
    return THUMB_CACHE_DIR / host_key / "sky_dome_timelapse"


def _sky_dome_timelapse_path(host_key: str, date_yyyymmdd: str) -> Path:
    return _sky_dome_timelapse_dir(host_key) / f"{date_yyyymmdd}.mp4"


def _sky_dome_timelapse_attempt_marker(host_key: str, date_yyyymmdd: str) -> Path:
    """On-disk record that this station's dome timelapse for ``date`` was
    already attempted, so a respawned scheduler doesn't redo the build.

    Deliberately NOT keyed off the output MP4: most stations produce no dome
    timelapse at all on a given night (no per-camera MP4s, no platepar), so
    "output missing" is the steady state for them and would leave exactly
    those stations retrying the full build on every restart.
    """
    return _sky_dome_timelapse_dir(host_key) / f"{date_yyyymmdd}.attempted"


def _archived_night_timelapses(
    listing: dict[str, list[dict]], cams: Iterable[str], date_yyyymmdd: str,
    archive_root: Path,
) -> dict[str, Path]:
    """Archive path of each camera's colour timelapse MP4 for one night.

    *listing* is the archive-only per-camera timelapse listing that
    ``/api/timelapses/<host>`` serves (``station_proxy._get_timelapses_swr``),
    so a camera whose timelapse is not uploaded yet is simply absent.
    """
    out: dict[str, Path] = {}
    for cam in cams:
        entry = next((e for e in listing.get(cam, []) if e.get("date") == date_yyyymmdd), None)
        filename = (entry or {}).get("filename")
        if not filename or "/" in filename or ".." in filename:
            continue
        out[cam] = archive_root / cam / date_yyyymmdd / "timelapse" / filename
    return out


_DOME_TIMELAPSE_RETRY_S = 1800     # between build attempts while inputs are missing
_DOME_TIMELAPSE_GIVEUP_UTC_HOUR = 12  # stop retrying; stations publish by ~08:00 UTC


def _timelapse_retry_state(
    mp4_exists: bool, marker_exists: bool,
    sun_alt: float | None, utc_hour: int,
) -> str:
    """Decide the night-timelapse action for one station on one pass.

    The trigger used to be the civil-dawn crossing — a single shot at ~03:20
    UTC, hours before stations build their per-camera timelapses (~07:20), so
    every build found no inputs, wrote its attempt marker and never retried
    (zero fleet dome timelapses since 2026-09-08). State lives on disk (the
    night's MP4 + attempt marker), so worker recycles and mid-day deploys
    resume where the last worker stopped.

    Returns ``"due"`` (try a build now), ``"give_up"`` (record the attempt
    marker so the night closes), or ``"skip"``.
    """
    if utc_hour >= 14 or mp4_exists or marker_exists:
        # After 14:00 UTC the night key (now - 2 h, before-noon rule) would
        # flip to a night that has not happened yet.
        return "skip"
    if sun_alt is None or sun_alt < -6.0:
        # Night is still running (or no coordinates) — nothing to build yet.
        return "skip"
    if utc_hour >= _DOME_TIMELAPSE_GIVEUP_UTC_HOUR:
        return "give_up"
    return "due"


# ── Live-tile clip (overview "Live Cameras") ──────────────────────────────
#
# The newest colour chunk is always raw during the night, and the station's
# overlay gate (station_api._ensure_overlaid) re-encodes it before sending the
# first byte: 7-9 s on the VAAPI hosts, 23-56 s on the Pi 5 at gmnde01
# (Berlin), measured 2026-09-25. A <video src> pointed straight at it outlived
# nginx's 30 s proxy_read_timeout, so Berlin clicks never played at all. The
# click now starts that render in the background and the page polls until the
# station lists the chunk as reencoded, which it then serves at once.

# How far back from the newest chunk an already-overlaid one still counts as
# "live" (15 chunks x 20 s = 5 min): a retry, or a click on another worker,
# plays the chunk a previous click rendered instead of starting a new render.
LIVE_REUSE_CHUNKS = 15
# One render per camera at a time; a re-click within this window waits on the
# chunk already rendering rather than queueing another encode on the station.
LIVE_WARM_TTL_S = 180.0
# Renders one worker keeps in flight across the fleet. Each is a full chunk
# re-encode on a station that is also capturing, so a click storm must not
# fan out without bound.
LIVE_WARM_MAX = 4
# Socket read timeout for the render request: the station sends no header
# until the overlay is burned in (56 s seen on the Pi with two renders).
LIVE_WARM_READ_TIMEOUT_S = 150.0

_LIVE_CHUNK_RE = re.compile(r"^[A-Z0-9]+_\d{8}_\d{6}(?:_\d{6})?_color\.mkv$")


def _pick_live_chunk(
    chunks: list[dict],
    want: str | None = None,
    inflight: str | None = None,
    reuse: int = LIVE_REUSE_CHUNKS,
) -> dict | None:
    """Choose the chunk a live-tile click should play.

    ``want`` (a poll for one specific chunk) returns that chunk or None.
    Otherwise, in order: the newest already-overlaid chunk among the last
    ``reuse`` (instant playback), the chunk this worker is already rendering
    for the camera, else the newest chunk.
    """
    if not chunks:
        return None
    if want:
        return next((c for c in chunks if c.get("filename") == want), None)
    for c in reversed(chunks[-reuse:]):
        if c.get("reencoded"):
            return c
    if inflight:
        hit = next((c for c in chunks if c.get("filename") == inflight), None)
        if hit is not None:
            return hit
    return chunks[-1]


class _LiveChunkWarmer:
    """Start (at most one per camera) background requests that make the
    station burn the overlay into a chunk, without holding a web request.

    ``fetch(host_key, cam, date, filename)`` performs the blocking request;
    it runs on a daemon thread. State is per worker process; the station's
    ``reencoded`` flag is the cross-worker truth (see ``_pick_live_chunk``).
    """

    def __init__(self, fetch: Callable[[str, str, str, str], None],
                 max_inflight: int = LIVE_WARM_MAX,
                 ttl_s: float = LIVE_WARM_TTL_S,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._max = max_inflight
        self._ttl = ttl_s
        self._clock = clock
        self._lock = threading.Lock()
        self._inflight: dict[tuple[str, str], tuple[str, float]] = {}
        self._running = 0

    def inflight(self, host_key: str, cam: str) -> str | None:
        """Filename being (or recently) rendered for this camera, if fresh."""
        with self._lock:
            entry = self._inflight.get((host_key, cam))
            if entry and self._clock() - entry[1] < self._ttl:
                return entry[0]
            return None

    def start(self, host_key: str, cam: str, date: str, filename: str) -> bool:
        """Kick off a render; False when one is already running for this
        chunk, or the per-worker cap is reached."""
        key = (host_key, cam)
        now = self._clock()
        with self._lock:
            entry = self._inflight.get(key)
            if entry and entry[0] == filename and now - entry[1] < self._ttl:
                return False
            if self._running >= self._max:
                return False
            self._inflight[key] = (filename, now)
            self._running += 1

        def _run() -> None:
            try:
                self._fetch(host_key, cam, date, filename)
            except Exception:
                logger.info("live chunk render failed for %s/%s %s",
                            host_key, cam, filename, exc_info=True)
            finally:
                with self._lock:
                    self._running -= 1

        threading.Thread(target=_run, name=f"live-warm-{cam}", daemon=True).start()
        return True


# ── Dependencies from create_app ────────────────────────────────────────

@dataclass(frozen=True)
class SkyDomeDeps:
    """What the routes, helpers and scheduler need from ``create_app``.

    ``platepar_cache`` is the ``/api/platepar`` route's cache
    (``routes/station_proxy.py``, which owns its TTL).
    ``get_timelapses_payload`` is the archive-only per-camera timelapse
    listing that ``register_station_proxy_routes`` returns; the dome
    timelapse build reads its inputs from the archive, never the station.
    ``live_warmer`` holds this app's in-flight live-chunk renders.
    """

    config: DashboardConfig
    tunnels: TunnelManager
    cache: StationCache
    platepar_cache: dict[str, tuple[float, Any]]
    get_timelapses_payload: Callable[[str], dict[str, Any]]
    live_warmer: _LiveChunkWarmer


# Set by register_sky_dome_routes; each registration replaces it.
_DEPS: SkyDomeDeps | None = None


def _deps() -> SkyDomeDeps:
    if _DEPS is None:
        raise RuntimeError("sky-dome code used before register_sky_dome_routes")
    return _DEPS


def _require_station(host_key: str) -> StationConfig:
    return lookup_station(_deps().config, host_key)


def _media_url(host_key: str, *path_parts: str) -> str:
    config = _deps().config
    tunnels = _deps().tunnels
    return _rh_media_url(config, tunnels, host_key, *path_parts)


# ── Dome cache path helpers ──────────────────────────────────────────

def _sky_dome_cache_path(host_key: str, variant: str = "stack", overlay: bool = False) -> Path:
    # Colour stack keeps the legacy filename so existing cached PNGs aren't
    # orphaned; BW FF maxpixel gets its own sidecar.
    suffix = "_overlay" if overlay else ""
    filename = f"sky_dome{suffix}.png" if variant == "stack" else f"sky_dome_{variant}{suffix}.png"
    return THUMB_CACHE_DIR / host_key / filename


def _sky_dome_cache_mtime(host_key: str, variant: str = "stack") -> float:
    """mtime (epoch) of the cached dome PNG, or 0.0 if absent/unreadable.

    Lets the scheduler seed its freshness state from disk so a recycled or
    respawned worker inherits the previous worker's renders instead of
    re-rendering the whole fleet on startup — mtime survives a respawn,
    in-memory scheduler state does not.
    """
    try:
        return _sky_dome_cache_path(host_key, variant).stat().st_mtime
    except OSError:
        return 0.0


def _sky_dome_meta_path(host_key: str, variant: str = "stack") -> Path:
    """Sidecar JSON next to the cached PNG carrying capture-time
    provenance for the freshness indicator on the dashboard card.

    Only written for the ``ff_max`` variant today — the colour stack
    path doesn't surface a per-camera capture timestamp from the
    station.
    """
    filename = "sky_dome.meta.json" if variant == "stack" else f"sky_dome_{variant}.meta.json"
    return THUMB_CACHE_DIR / host_key / filename


def _sky_dome_lock_for(host_key: str, variant: str = "stack", overlay: bool = False) -> threading.Lock:
    # Key locks on (host, variant, overlay) so a slow colour render doesn't serialize
    # the parallel BW render (and vice-versa).
    key = (host_key, variant, overlay)
    with _sky_dome_locks_lock:
        lock = _sky_dome_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _sky_dome_locks[key] = lock
        return lock


def _sky_dome_ttl_for(host_key: str) -> int:
    """Return cache TTL in seconds — short at night (chunk stacks roll
    forward every ~5 min), long during the day (source is static).

    Uses civil twilight (sun at -6 deg) as the night boundary. Stations
    without lat/lon fall back to the night TTL, since the dome only
    ever updates as a function of new data anyway.
    """
    config = _deps().config
    station = config.stations.get(host_key)
    if station is None or station.lat is None or station.lon is None:
        return _SKY_DOME_NIGHT_TTL
    now = datetime.now(timezone.utc)
    try:
        tw = _compute_twilight(
            float(station.lat), float(station.lon),
            now.strftime("%Y%m%d"), horizon=-6.0,
        )
        sunset_min = tw.get("sunset_min")
        sunrise_min = tw.get("sunrise_min")
    except Exception:
        return _SKY_DOME_NIGHT_TTL
    if sunset_min is None or sunrise_min is None:
        return _SKY_DOME_NIGHT_TTL
    now_min = now.hour * 60 + now.minute
    # Night window crosses UTC midnight at most longitudes — handle
    # both orderings rather than assuming sunset > sunrise.
    if sunset_min > sunrise_min:
        is_night = now_min >= sunset_min or now_min < sunrise_min
    else:
        is_night = not (sunset_min <= now_min < sunrise_min)
    return _SKY_DOME_NIGHT_TTL if is_night else _SKY_DOME_DAY_TTL


# ── Platepar fetcher (reuses shared cache) ───────────────────────────

def _fetch_platepar_for_dome(host_key: str) -> dict[str, Any] | None:
    """The station's pushed platepars (#1248), else the last copy the
    /api/platepar route cached, else platepar_store's last-known entries
    for this station's cameras. Never contacts the station (#1249)."""
    config = _deps().config
    platepar_cache = _deps().platepar_cache
    pushed = pushed_calibration.get_platepar(host_key)
    if pushed:
        return pushed
    cached = platepar_cache.get(host_key)
    if cached:
        return cached[1]
    station = config.stations.get(host_key)
    if station is None:
        return None
    import platepar_store
    known = platepar_store.get_all()
    out = {c.code: known[c.code] for c in station.cameras if c.code in known}
    return out or None


# ── Dome PNG renderer ────────────────────────────────────────────────

def _render_sky_dome_png(host_key: str, variant: str = "stack", overlay: bool = False) -> bytes | None:
    """Build the celestial-dome PNG for ``host_key`` from pushed inputs
    only (#1249): no request reaches the station. Returns None if no
    camera has both a platepar and a recent pushed image ("no recent
    frame"); the route then serves the last rendered PNG if it has one.

    ``variant``:
      - ``stack`` (default): the pushed colour chunk stack, or RMS's
        captured stack on a camera without colour (``live_frame`` push,
        every 5 min). Ignored once older than ``_LIVE_FRAME_MAX_AGE_S``.
      - ``ff_max``: the pushed newest BW FF maxpixel (``live_thumb``
        push, every 30 s).
    """
    config = _deps().config
    cache = _deps().cache
    import io as _io
    # Prefer a station-rendered dome (PR2): if the station pushed its own
    # composite, serve those bytes and skip the VPS render entirely. Falls
    # through to the local render for stations on an older bundle / offline.
    station_png = _read_station_dome_png(host_key, variant, overlay)
    if station_png is not None:
        return station_png

    import numpy as _np
    from PIL import Image as _PILImage
    from celestial_dome import Plate, render_station_dome

    platepar_payload = _fetch_platepar_for_dome(host_key)
    if not platepar_payload:
        return None

    station = config.stations.get(host_key)
    if station is None:
        return None
    label = station.label or host_key
    cams = [c.code for c in station.cameras]

    plates_with_images: list[tuple[Plate, _np.ndarray]] = []
    # Track newest FF capture time across cameras for the freshness label.
    # Stations on pre-bundle-261 don't emit X-FF-Timestamp; cameras_used
    # stays empty in that case and the sidecar JSON is not written.
    cameras_used: list[dict[str, str]] = []
    for cam_code in cams:
        entry = platepar_payload.get(cam_code)
        if not entry or entry.get("error"):
            continue
        try:
            plate = Plate.from_dict(entry, cam_code)
        except (KeyError, TypeError, ValueError):
            continue
        img_bytes: bytes | None = None
        if variant == "ff_max":
            # The pushed newest-FF maxpixel (~10 s of sky, BW), the same
            # thumbnail the live tiles show.
            thumb = cache.get_live_thumb(host_key, cam_code)
            if thumb and thumb.get("data"):
                img_bytes = thumb["data"]
                if thumb.get("ff_timestamp"):
                    cameras_used.append(
                        {"code": cam_code, "capture_time": thumb["ff_timestamp"]})
        else:
            # The pushed newest colour chunk stack (or RMS captured stack).
            frame = pushed_calibration.get_live_frame(
                host_key, cam_code, max_age_s=_LIVE_FRAME_MAX_AGE_S)
            if frame is not None:
                img_bytes = frame["data"]
        if img_bytes is None:
            continue
        try:
            pil_img = _PILImage.open(_io.BytesIO(img_bytes)).convert("RGB")
            # Stack images from the station stacker are pre-rotated 180
            # when the camera config says rotate=true. The dome projection
            # uses platepar coordinates that map native sensor pixels, so
            # undo the rotation to restore native orientation.
            cam_cfg = next(
                (c for c in station.cameras if c.code == cam_code), None
            )
            if cam_cfg and cam_cfg.rotate and variant != "ff_max":
                pil_img = pil_img.rotate(180)
            img = _np.asarray(pil_img)
        except Exception:
            continue
        plates_with_images.append((plate, img))

    if not plates_with_images:
        return None

    dome = render_station_dome(
        plates_with_images,
        station=host_key,
        label=label,
        lat=station.lat if station else None,
        lon=station.lon if station else None,
        timestamp=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        overlay=overlay,
    )
    buf = _io.BytesIO()
    dome.save(buf, format="PNG", optimize=True)

    # Freshness sidecar — written next to the PNG so the dashboard's
    # "Image vs Now" label has accurate provenance. Only meaningful for
    # ff_max (the colour stack route doesn't surface per-FF timestamps).
    if variant == "ff_max" and cameras_used:
        try:
            meta = {
                "variant": variant,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "capture_time": max(c["capture_time"] for c in cameras_used),
                "cameras_used": cameras_used,
            }
            meta_path = _sky_dome_meta_path(host_key, variant)
            meta_path.parent.mkdir(parents=True, exist_ok=True)
            meta_path.write_text(json.dumps(meta))
        except Exception:
            logger.warning(
                "sky_dome meta write failed for %s/%s",
                host_key, variant, exc_info=True,
            )

    return buf.getvalue()


# Cache headers for every dome response — the scheduler keeps the disk
# file hot, so clients should always re-validate rather than serve a
# stale browser-cached PNG.
_SKY_DOME_NO_STORE_HEADERS = {
    "Cache-Control": "no-store, no-cache, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}


def _sky_dome_send_file(cache_file: Path) -> Response:
    resp = send_file(cache_file, mimetype="image/png", conditional=True)
    for k, v in _SKY_DOME_NO_STORE_HEADERS.items():
        resp.headers[k] = v
    return resp


# ── /api/sky_dome/<host_key>.png ─────────────────────────────────────

@require_station
def api_sky_dome(host_key: str):
    """Per-station celestial hemisphere — each camera's freshest stack
    reprojected onto a single 1600px disc, with FOV outlines and
    cardinal labels.

    Query param ``variant``:
      - ``stack`` (default): colour chunk stack / RMS captured_stack —
        the original behaviour, kept for backward compat and scheduler
        reuse.
      - ``ff_max``: BW FF maxpixel (single newest FF per camera, ~10 s
        of sky). Drives the live "Sky right now" widget on the Video DB
        tab.

    Query param ``overlay``:
      - ``false`` (default): clean mosaic without cardinal labels,
        FOV outlines, camera legend, or metadata text.
      - ``true`` / ``1`` / ``yes``: full annotated dome with overlays.

    TTL is dynamic: ~5 min at night (so the panorama rolls forward
    with each new colour chunk stack) and 1 h during the day (the
    source image doesn't change). Falls back to the last-known PNG
    when the station is offline.
    """
    _require_station(host_key)
    variant = request.args.get("variant", "stack")
    if variant not in ("stack", "ff_max"):
        variant = "stack"
    overlay = request.args.get("overlay", "false").lower() in ("true", "1", "yes")
    cache_file = _sky_dome_cache_path(host_key, variant, overlay=overlay)
    ttl = _sky_dome_ttl_for(host_key)
    now = time.time()

    if cache_file.exists() and now - cache_file.stat().st_mtime < ttl:
        return _sky_dome_send_file(cache_file)

    lock = _sky_dome_lock_for(host_key, variant, overlay=overlay)
    with lock:
        # Recheck after acquiring the lock — another worker may have
        # rendered while we were waiting.
        if cache_file.exists() and time.time() - cache_file.stat().st_mtime < ttl:
            return _sky_dome_send_file(cache_file)

        try:
            png_bytes = _render_sky_dome_png(host_key, variant, overlay=overlay)
        except Exception:
            logger.exception(
                "sky_dome render failed for %s variant=%s overlay=%s",
                host_key, variant, overlay,
            )
            png_bytes = None

        if png_bytes is not None and _thumb_cache_has_space():
            try:
                _write_atomic(cache_file, png_bytes)
            except Exception:
                logger.warning(
                    "sky_dome cache write failed for %s variant=%s",
                    host_key, variant, exc_info=True,
                )

    if png_bytes is not None:
        resp = Response(png_bytes, status=200, mimetype="image/png")
        for k, v in _SKY_DOME_NO_STORE_HEADERS.items():
            resp.headers[k] = v
        return resp
    # Render failed — serve stale cached PNG if we have one.
    if cache_file.exists():
        return _sky_dome_send_file(cache_file)
    return jsonify({"error": "no platepar/stack available"}), 503


# ── /api/sky_dome_meta/<host_key> ────────────────────────────────────

@require_station
def api_sky_dome_meta(host_key: str):
    """Freshness sidecar for the dome card on the dashboard. Reports the
    newest FF capture time across cameras the last render used, plus
    when the render finished. The frontend uses this to colour-code an
    ``Image: HH:MM:SS UTC`` label next to a live ``Now`` clock.

    Returns 200 with ``{capture_time: null}`` (rather than 404) when no
    sidecar exists yet — keeps the frontend code simple.
    """
    _require_station(host_key)
    variant = request.args.get("variant", "ff_max")
    if variant not in ("stack", "ff_max"):
        variant = "ff_max"
    meta_file = _sky_dome_meta_path(host_key, variant)
    if not meta_file.exists():
        return jsonify({
            "capture_time": None,
            "generated_at": None,
            "cameras_used": [],
            "variant": variant,
        })
    try:
        data = json.loads(meta_file.read_text())
    except Exception:
        return jsonify({"capture_time": None, "generated_at": None,
                        "cameras_used": [], "variant": variant})
    resp = jsonify(data)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp


# ── Sky-dome pre-render scheduler + nightly timelapse ────────────────
# The scheduler thread keeps both dome variants warm on a twilight-aware
# cadence and, the morning after, builds the night's 12 fps dome MP4 from
# the cameras' archived timelapses. Failures back off via a
# consecutive-failure -> 2-min mute. See the constants block near
# ``_SKY_DOME_DAY_TTL`` for the knobs.

# ── Station-uploaded dome (PR2: render moved to stations) ────────────
# Stations running sky_dome_render.py composite their own dome and push it
# via the ingest byte-plane into ARCHIVE_PATH/<primary_cam>/<night>/. The
# dashboard PREFERS that upload and only renders/builds as a FALLBACK for
# stations still on an older bundle (or offline), so the ~0.7 GB-per-
# timelapse VPS render disappears for updated stations. Producer and
# consumer share the path contract from celestial_dome.
_STATION_DOME_PNG_MAX_STALE_S = 36 * 3600  # ignore a long-dead station's dome


def _station_dome_primary_cam(host_key: str) -> str | None:
    config = _deps().config
    from celestial_dome import station_dome_primary_cam
    station = config.stations.get(host_key)
    if station is None:
        return None
    return station_dome_primary_cam([c.code for c in station.cameras])


def _sshfs_exists(path: Path) -> bool:
    """``is_file()`` on the (SSHFS) archive, guarded so a stalled mount
    returns False instead of blocking the render/serve thread."""
    from station_client import _with_sshfs_timeout
    return bool(_with_sshfs_timeout(path.is_file, timeout=3.0, default=False))


def _station_dome_png_path(
    host_key: str, variant: str, overlay: bool, night: str,
) -> Path | None:
    from celestial_dome import STATION_DOME_PNG_SUBDIR, station_dome_png_name
    cam = _station_dome_primary_cam(host_key)
    if cam is None:
        return None
    return (
        ARCHIVE_PATH / cam / night / STATION_DOME_PNG_SUBDIR
        / station_dome_png_name(variant, overlay)
    )


def _read_station_dome_png(host_key: str, variant: str, overlay: bool) -> bytes | None:
    """Fresh station-uploaded dome PNG bytes for this variant, else None.

    Checks tonight's capture-night then yesterday's (so the widget keeps
    serving last night's final dome through the day). Guarded against an
    SSHFS stall; a dome older than ``_STATION_DOME_PNG_MAX_STALE_S`` is
    ignored so a long-offline station falls back to the VPS render path.
    """
    from station_client import _with_sshfs_timeout
    now = datetime.now(timezone.utc)
    nights = [_dome_capture_night(now), _dome_capture_night(now - timedelta(days=1))]
    for night in nights:
        p = _station_dome_png_path(host_key, variant, overlay, night)
        if p is None:
            return None

        def _read(pp: Path = p) -> bytes | None:
            if not pp.is_file():
                return None
            st = pp.stat()
            if st.st_size <= 0 or (time.time() - st.st_mtime) > _STATION_DOME_PNG_MAX_STALE_S:
                return None
            return pp.read_bytes()

        data = _with_sshfs_timeout(_read, timeout=5.0, default=None)
        if data:
            return data
    return None


def _station_dome_timelapse_archive_path(host_key: str, date_yyyymmdd: str) -> Path | None:
    from celestial_dome import STATION_DOME_TIMELAPSE_NAME, STATION_DOME_TIMELAPSE_SUBDIR
    cam = _station_dome_primary_cam(host_key)
    if cam is None:
        return None
    return (
        ARCHIVE_PATH / cam / date_yyyymmdd / STATION_DOME_TIMELAPSE_SUBDIR
        / STATION_DOME_TIMELAPSE_NAME
    )


def _station_dome_timelapse_dates(host_key: str) -> set[str]:
    from celestial_dome import STATION_DOME_TIMELAPSE_NAME, STATION_DOME_TIMELAPSE_SUBDIR
    from station_client import _with_sshfs_timeout
    cam = _station_dome_primary_cam(host_key)
    if cam is None:
        return set()
    cam_dir = ARCHIVE_PATH / cam

    def _scan() -> set[str]:
        out: set[str] = set()
        if not cam_dir.is_dir():
            return out
        for d in cam_dir.iterdir():
            if (
                len(d.name) == 8 and d.name.isdigit()
                and (d / STATION_DOME_TIMELAPSE_SUBDIR / STATION_DOME_TIMELAPSE_NAME).is_file()
            ):
                out.add(d.name)
        return out

    return _with_sshfs_timeout(_scan, timeout=5.0, default=set()) or set()


def _station_sun_alt(host_key: str) -> float | None:
    """Sun altitude in degrees right now for the station's lat/lon, or
    None when the station has no coords."""
    config = _deps().config
    station = config.stations.get(host_key)
    if station is None or station.lat is None or station.lon is None:
        return None
    try:
        return sun_altitude_deg(float(station.lat), float(station.lon),
                                datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return None


def _dome_capture_night(now: datetime | None = None) -> str:
    """Return the capture-night date string (YYYYMMDD) for ``now``.

    A capture night is identified by its evening UTC date — frames
    captured after UTC midnight still belong to the prior evening's
    night so they end up in the same archive directory and the same
    timelapse. Before noon UTC we attribute to the previous day; after
    noon to today.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    if now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


def _render_and_archive_sky_dome(
    host_key: str, variant: str, skip_if_fresh_s: float | None = None,
) -> bool:
    """Render ``variant`` and atomically swap the cached PNG. Returns
    True on success.

    When ``skip_if_fresh_s`` is set and the cached PNG on disk is younger
    than that many seconds, the render is skipped and True is returned —
    the caller treats the existing file as up to date. This is what keeps a
    recycled/respawned worker from re-rendering the whole fleet (the render
    storm that pegged the CPU and tripped the gunicorn WORKER TIMEOUT).
    """
    if skip_if_fresh_s is not None and _dome_cache_is_fresh(
        _sky_dome_cache_path(host_key, variant), skip_if_fresh_s, time.time(),
    ):
        return True
    try:
        png_bytes = _render_sky_dome_png(host_key, variant=variant)
    except TypeError:
        # Pre-merge: ``_render_sky_dome_png`` doesn't yet accept
        # ``variant``. Fall back to the unparameterised call so the
        # scheduler keeps at least the colour cache hot until the
        # parallel branch lands.
        try:
            png_bytes = _render_sky_dome_png(host_key)
        except Exception:
            logger.exception(
                "dome render crashed for %s variant=%s", host_key, variant,
            )
            return False
    except Exception:
        logger.exception(
            "dome render crashed for %s variant=%s", host_key, variant,
        )
        return False
    if not png_bytes:
        return False
    cache_file = _sky_dome_cache_path(host_key, variant)
    if not _thumb_cache_has_space():
        return False
    try:
        _write_atomic(cache_file, png_bytes)
    except OSError:
        logger.warning(
            "dome cache write failed for %s variant=%s",
            host_key, variant, exc_info=True,
        )
        return False
    return True


def _build_sky_dome_timelapse_from_mp4s(host_key: str, date_yyyymmdd: str) -> bool:
    """Build sky dome timelapse by projecting per-camera color timelapse MP4s.

    Downloads each camera's timelapse MP4 for ``date_yyyymmdd``, extracts
    evenly-spaced frames, projects them through the platepar onto a celestial
    dome, then stitches the projected frames into an MP4.
    """
    config = _deps().config
    get_timelapses_payload = _deps().get_timelapses_payload
    # At most one REAL attempt per station per night — but "the station
    # has not published its timelapses yet" is not an attempt. The marker
    # is recorded by _record_timelapse_attempt() once the inputs exist
    # (just before the ~0.7 GB, multi-minute reproject+encode), so the
    # scheduler's state machine keeps retrying an inputs-missing night
    # and a recycle mid-encode still can't loop the heavy phase.
    _marker = _sky_dome_timelapse_attempt_marker(host_key, date_yyyymmdd)
    if _marker.exists():
        logger.debug(
            "dome timelapse %s/%s: already attempted this night, skipping",
            host_key, date_yyyymmdd,
        )
        return _sky_dome_timelapse_path(host_key, date_yyyymmdd).is_file()

    def _record_timelapse_attempt() -> None:
        try:
            _marker.parent.mkdir(parents=True, exist_ok=True)
            _marker.touch()
        except OSError:
            # An unwritable cache volume costs the idempotency guard, not
            # the build -- the scheduler backoff is the primary defence.
            logger.warning(
                "dome timelapse %s/%s: could not record attempt marker",
                host_key, date_yyyymmdd, exc_info=True,
            )

    # PR2: if the station uploaded its own dome timelapse for this night,
    # serve that and skip the ~0.7 GB VPS build entirely.
    _station_tl = _station_dome_timelapse_archive_path(host_key, date_yyyymmdd)
    if _station_tl is not None and _sshfs_exists(_station_tl):
        logger.info(
            "dome timelapse %s/%s: station-provided, skipping VPS build",
            host_key, date_yyyymmdd,
        )
        _record_timelapse_attempt()
        return True
    import io as _io
    import os as _os
    import tempfile
    import numpy as _np
    from PIL import Image as _PILImage
    from celestial_dome import (
        Plate, normalize_camera_image as _normalize_cam,
        render_timelapse_batch, render_station_dome_timelapse,
    )

    # Logo path: VPS deploys assets/ one level above routes/, then repo fallback.
    _LOGO_CANDIDATES = [
        Path(__file__).parents[1] / "assets" / "gmn_sphere.png",
        Path(__file__).parents[2] / "rovimen-scripts" / "assets" / "gmn_sphere.png",
    ]
    _logo_path = next((p for p in _LOGO_CANDIDATES if p.exists()), None)

    platepar_payload = _fetch_platepar_for_dome(host_key)
    if not platepar_payload:
        logger.warning("dome timelapse build: no platepar for %s", host_key)
        return False

    station = config.stations.get(host_key)
    if not station:
        return False

    # The per-camera timelapses come from the archive. Asking the station
    # for its list timed out during dawn on busy stations (gmnro05
    # Bistrița, gmnro08 Brașov, gmnro18 Bisoca) and logged a traceback
    # each time; a night whose timelapse is not uploaded is just absent.
    sources = _archived_night_timelapses(
        get_timelapses_payload(host_key), (c.code for c in station.cameras),
        date_yyyymmdd, ARCHIVE_PATH,
    )

    with tempfile.TemporaryDirectory(prefix="rovimen_dome_") as _tmpdir:
        tmpdir = Path(_tmpdir)

        # Copy each camera's timelapse MP4 for this night off the archive.
        from station_client import _with_sshfs_timeout
        cam_mp4: dict[str, Path] = {}
        for cam, src in sources.items():
            mp4_path = tmpdir / f"{cam}.mp4"
            copied = _with_sshfs_timeout(
                lambda src=src, dst=mp4_path: shutil.copyfile(src, dst),
                timeout=180.0, default=None,
            )
            if copied is None:
                logger.warning(
                    "dome timelapse build: could not read %s from the archive", src,
                )
                continue
            cam_mp4[cam] = mp4_path

        if not cam_mp4:
            # No marker: the station has most likely not uploaded its
            # per-camera timelapses yet (they land mid-morning) — the
            # scheduler retries until _DOME_TIMELAPSE_GIVEUP_UTC_HOUR.
            logger.info(
                "dome timelapse build: no timelapse MP4s yet for %s/%s "
                "— will retry", host_key, date_yyyymmdd,
            )
            return False

        # Inputs exist — from here on the build is heavy (reproject +
        # encode), so this night now counts as attempted.
        _record_timelapse_attempt()

        # Extract evenly-spaced frames from each MP4
        cam_frames: dict[str, list[Path]] = {}
        for cam, mp4_path in cam_mp4.items():
            try:
                probe = subprocess.run(
                    ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
                     "-count_packets", "-show_entries", "stream=nb_read_packets",
                     "-of", "csv=p=0", str(mp4_path)],
                    capture_output=True, text=True, timeout=30, check=False,
                )
                total = int(probe.stdout.strip())
            except Exception:
                total = 0
            if total < 10:
                logger.warning(
                    "dome timelapse build: too few frames (%d) in %s/%s",
                    total, host_key, cam,
                )
                continue
            step = max(1, total // _DOME_TIMELAPSE_TARGET_FRAMES)
            frames_dir = tmpdir / f"frames_{cam}"
            frames_dir.mkdir()
            result = subprocess.run(
                ["ffmpeg", "-y", "-i", str(mp4_path),
                 "-vf", f"select=not(mod(n\\,{step}))",
                 "-vsync", "0",
                 str(frames_dir / "%04d.png")],
                capture_output=True, timeout=180, check=False,
            )
            if result.returncode != 0:
                logger.warning(
                    "dome timelapse build: frame extract failed for %s/%s",
                    host_key, cam,
                )
                continue
            frames = sorted(frames_dir.glob("*.png"))
            if frames:
                cam_frames[cam] = frames

        if not cam_frames:
            return False

        n = min(len(v) for v in cam_frames.values())
        if n < 10:
            logger.warning(
                "dome timelapse build: too few extracted frames (%d) for %s/%s",
                n, host_key, date_yyyymmdd,
            )
            return False

        # Compute per-frame UTC timestamps by interpolating between sunset
        # and sunrise for this night using civil twilight (-6 deg).
        _sunset_min: float | None = None
        _night_dur_min: float | None = None
        _night_date_str = f"{date_yyyymmdd[:4]}-{date_yyyymmdd[4:6]}-{date_yyyymmdd[6:]}"
        if station.lat is not None and station.lon is not None:
            try:
                from astronomy import _compute_twilight
                tw = _compute_twilight(
                    float(station.lat), float(station.lon),
                    date_yyyymmdd, horizon=-6.0,
                )
                sm = tw.get("sunset_min")
                rm = tw.get("sunrise_min")
                if sm is not None and rm is not None:
                    _sunset_min = float(sm)
                    # Night spans sunset → midnight → sunrise next day
                    _night_dur_min = float((1440 - sm) + rm)
            except Exception:
                pass

        def _frame_timestamp(i: int, total: int) -> str:
            if _sunset_min is not None and _night_dur_min is not None and total > 1:
                offset = (i / (total - 1)) * _night_dur_min
                abs_min = (_sunset_min + offset) % 1440
                h = int(abs_min // 60)
                m = int(abs_min % 60)
                return f"{_night_date_str}  {h:02d}:{m:02d} UTC"
            return _night_date_str

        # Build plate list and pre-normalize all frames (rotation + crop done once,
        # not per frame).  Frames that fail to load are stored as None so the
        # index alignment across cameras is preserved for render_timelapse_batch.
        plates_ordered: list = []
        normalized_per_cam: list[list] = []
        for cam_cfg in station.cameras:
            cam = cam_cfg.code
            frames = cam_frames.get(cam)
            if not frames:
                continue
            entry = platepar_payload.get(cam)
            if not entry or entry.get("error"):
                continue
            try:
                plate = Plate.from_dict(entry, cam, rotate=bool(cam_cfg.rotate))
            except (KeyError, TypeError, ValueError):
                continue
            y_res = int(entry.get("Y_res", 0))
            cam_normed: list = []
            for frame_path in frames[:n]:
                try:
                    pil_img = _PILImage.open(frame_path).convert("RGB")
                    arr = _np.asarray(pil_img)
                    if y_res:
                        arr = arr[:y_res, :, :].copy()
                    cam_normed.append(_normalize_cam(arr, plate))
                except Exception:
                    cam_normed.append(None)
            plates_ordered.append(plate)
            normalized_per_cam.append(cam_normed)

        if not plates_ordered:
            logger.warning(
                "dome timelapse build: no valid cameras for %s/%s",
                host_key, date_yyyymmdd,
            )
            return False

        timestamps = [_frame_timestamp(i, n) for i in range(n)]
        n_workers = min(_os.cpu_count() or 4, 8)
        try:
            dome_images = render_timelapse_batch(
                plates_ordered,
                normalized_per_cam,
                n,
                timestamps=timestamps,
                logo_path=_logo_path,
                dome_px=_DOME_TIMELAPSE_PX,
                n_workers=n_workers,
            )
        except Exception:
            logger.warning(
                "dome timelapse build: batch render failed for %s/%s, falling back",
                host_key, date_yyyymmdd, exc_info=True,
            )
            dome_images = []

        dome_frames_dir = tmpdir / "dome_frames"
        dome_frames_dir.mkdir()
        rendered = 0
        for i, dome in enumerate(dome_images):
            if dome is None:
                continue
            try:
                buf = _io.BytesIO()
                dome.save(buf, format="PNG")
                (dome_frames_dir / f"{i:04d}.png").write_bytes(buf.getvalue())
                rendered += 1
            except Exception:
                logger.warning(
                    "dome timelapse build: frame save failed at %d for %s",
                    i, host_key,
                )

        if rendered < 10:
            logger.warning(
                "dome timelapse build: only %d dome frames rendered for %s/%s",
                rendered, host_key, date_yyyymmdd,
            )
            return False

        out = _sky_dome_timelapse_path(host_key, date_yyyymmdd)
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning(
                "dome timelapse build: mkdir failed for %s/%s",
                host_key, date_yyyymmdd, exc_info=True,
            )
            return False

        if not _encode_dome_timelapse(
                dome_frames_dir, out, f"{host_key}/{date_yyyymmdd}"):
            return False

        logger.info(
            "dome timelapse built from camera timelapses %s/%s (%d frames)",
            host_key, date_yyyymmdd, rendered,
        )
        return True


def _prune_sky_dome_timelapses() -> None:
    """Delete dome timelapse MP4s older than the retention window.

    Sweeps the per-night attempt markers on the same cutoff so they age out
    with the nights they guard rather than accumulating one file per
    station per night forever, and any ``.mp4.part`` a killed encode left
    (see ``_encode_dome_timelapse``).
    """
    cutoff = time.time() - _DOME_TIMELAPSE_RETENTION_DAYS * 86400
    removed = 0
    try:
        for host_dir in THUMB_CACHE_DIR.glob("*/sky_dome_timelapse"):
            for stale in (*host_dir.glob("*.mp4"), *host_dir.glob("*.attempted"),
                          *host_dir.glob("*.mp4.part")):
                try:
                    if stale.stat().st_mtime < cutoff:
                        stale.unlink()
                        removed += 1
                except OSError:
                    continue
    except OSError:
        logger.warning("dome timelapse prune walk failed", exc_info=True)
        return
    if removed:
        logger.info("dome timelapse prune: removed %d expired file(s)", removed)


# ── Scheduler function (started via start_dome_scheduler) ────────────

def _sky_dome_scheduler(deps: SkyDomeDeps) -> None:
    """Daemon loop — pre-renders both dome variants per station on a
    twilight-aware cadence and builds the night's dome timelapse the
    morning after.

    Cadence per station (independent):
      - BW (``ff_max``): every ``_DOME_PRERENDER_INTERVAL_NIGHT_S``
        when sun < -6 deg, otherwise ``_DOME_PRERENDER_INTERVAL_DAY_S``.
      - Colour (``stack``): every ``_DOME_COLOUR_NIGHT_INTERVAL_S`` at
        night, otherwise daily.

    Per-station consecutive failures (>= ``_DOME_FAILURE_MUTE_THRESHOLD``)
    mute the station for ``_DOME_FAILURE_MUTE_DURATION_S``. Exceptions
    in one station's render never starve the others — the loop
    catches and carries on.

    Renders are parallelised across stations so that slow/unreachable
    stations don't starve reachable ones.  The previous night's MP4 build
    is driven by an on-disk state machine (``_timelapse_retry_state``):
    once the sun is above civil twilight it is attempted every
    ``_DOME_TIMELAPSE_RETRY_S`` until the station's per-camera timelapses
    appear (they land mid-morning), a real build runs (attempt marker),
    or ``_DOME_TIMELAPSE_GIVEUP_UTC_HOUR`` closes the night.  Daily prune
    sweeps expired MP4s.
    """
    config = deps.config
    logger.info("sky-dome scheduler starting")
    last_prune = 0.0
    # Per-station backoff for the night-timelapse state machine below —
    # without it a "waiting for inputs" night would re-download the
    # station MP4 list on every 15 s pass.
    _dome_tl_next_try: dict[str, float] = {}
    last_color_render: dict[str, float] = {}

    # Bound concurrent renders so the scheduler can't monopolise the box.
    # Env-overridable (ROVIMEN_DOME_POOL_SIZE) so the VPS can be dialled down
    # to 1-2 without a redeploy; default keeps the historical min(3, N).
    _DOME_POOL_SIZE = (
        int(os.environ.get("ROVIMEN_DOME_POOL_SIZE", "0"))
        or min(3, len(config.stations) or 1)
    )

    def _render_station(host_key: str, now: float) -> tuple[str, bool]:
        """Render ff_max (+ colour when due) for one station.

        Returns (host_key, ok_ff).  Runs inside the thread pool.
        """
        ok_ff = False
        try:
            ok_ff = _render_and_archive_sky_dome(host_key, "ff_max")
        except Exception:
            logger.warning(
                "dome ff_max render failed for %s",
                host_key, exc_info=True,
            )

        sun_alt = _station_sun_alt(host_key)
        is_night = sun_alt is not None and sun_alt < 0.0
        colour_interval = (
            _DOME_COLOUR_NIGHT_INTERVAL_S if is_night
            else _DOME_PRERENDER_INTERVAL_DAY_S
        )
        if now - last_color_render.get(host_key, 0) >= colour_interval:
            try:
                _render_and_archive_sky_dome(host_key, "stack")
            except Exception:
                logger.warning(
                    "dome stack render failed for %s",
                    host_key, exc_info=True,
                )
            last_color_render[host_key] = now

        return host_key, ok_ff

    def _ff_interval(host_key: str) -> int:
        sun_alt = _station_sun_alt(host_key)
        is_night = sun_alt is not None and sun_alt < 0.0
        return (
            _DOME_PRERENDER_INTERVAL_NIGHT_S if is_night
            else _DOME_PRERENDER_INTERVAL_DAY_S
        )

    def _colour_interval(host_key: str) -> int:
        sun_alt = _station_sun_alt(host_key)
        is_night = sun_alt is not None and sun_alt < 0.0
        return (
            _DOME_COLOUR_NIGHT_INTERVAL_S if is_night
            else _DOME_PRERENDER_INTERVAL_DAY_S
        )

    # Seed freshness from the on-disk cache so a recycled/respawned worker
    # inherits the previous worker's renders instead of re-rendering the
    # whole fleet on startup. That full-fleet re-storm on every worker start
    # pegged the CPU and tripped the gunicorn WORKER TIMEOUT -> SIGKILL ->
    # respawn -> re-render loop that froze the dashboard. mtime survives the
    # respawn; the in-memory _dome_next_render / last_color_render do not.
    for host_key in config.stations:
        ff_mtime = _sky_dome_cache_mtime(host_key, "ff_max")
        if ff_mtime:
            _dome_next_render[host_key] = ff_mtime + _ff_interval(host_key)
        stack_mtime = _sky_dome_cache_mtime(host_key, "stack")
        if stack_mtime:
            last_color_render[host_key] = stack_mtime

    # Warm-up — parallel render per station, both variants, so the disk
    # cache is hot before the first user request lands. skip_if_fresh makes
    # this a no-op for stations already hot on disk (the common case after a
    # recycle), so only genuinely-stale/cold stations pay the render cost.
    def _warmup_station(host_key: str) -> None:
        try:
            _render_and_archive_sky_dome(
                host_key, "stack", skip_if_fresh_s=_colour_interval(host_key),
            )
        except Exception:
            logger.warning(
                "dome warm-up (stack) failed for %s", host_key, exc_info=True,
            )
        try:
            _render_and_archive_sky_dome(
                host_key, "ff_max", skip_if_fresh_s=_ff_interval(host_key),
            )
        except Exception:
            logger.warning(
                "dome warm-up (ff_max) failed for %s", host_key, exc_info=True,
            )

    with ThreadPoolExecutor(max_workers=_DOME_POOL_SIZE) as pool:
        list(pool.map(_warmup_station, list(config.stations)))

    while True:
        try:
            now = time.time()

            if now - last_prune > 86400:
                try:
                    _prune_sky_dome_timelapses()
                except Exception:
                    logger.exception("dome timelapse prune failed")
                last_prune = now

            # Collect stations that are due for a render.
            due: list[str] = []
            for host_key in list(config.stations):
                if _dome_failure_mute_until.get(host_key, 0) > now:
                    continue
                if _dome_next_render.get(host_key, 0) <= now:
                    due.append(host_key)

            # Render due stations in parallel.
            if due:
                with ThreadPoolExecutor(max_workers=_DOME_POOL_SIZE) as pool:
                    futures = {
                        pool.submit(_render_station, hk, now): hk
                        for hk in due
                    }
                    for fut in as_completed(futures):
                        host_key = futures[fut]
                        try:
                            _, ok_ff = fut.result()
                        except Exception:
                            logger.exception(
                                "dome render worker crashed for %s", host_key,
                            )
                            ok_ff = False

                        sun_alt = _station_sun_alt(host_key)
                        is_night = sun_alt is not None and sun_alt < 0.0
                        interval = (
                            _DOME_PRERENDER_INTERVAL_NIGHT_S if is_night
                            else _DOME_PRERENDER_INTERVAL_DAY_S
                        )

                        if ok_ff:
                            _dome_consecutive_failures[host_key] = 0
                        else:
                            _dome_consecutive_failures[host_key] = (
                                _dome_consecutive_failures.get(host_key, 0) + 1
                            )
                            if (_dome_consecutive_failures[host_key]
                                    >= _DOME_FAILURE_MUTE_THRESHOLD):
                                _dome_failure_mute_until[host_key] = (
                                    now + _DOME_FAILURE_MUTE_DURATION_S
                                )
                                logger.warning(
                                    "dome render muted for %s for %ds",
                                    host_key, _DOME_FAILURE_MUTE_DURATION_S,
                                )
                                _dome_consecutive_failures[host_key] = 0

                        logger.debug(
                            "dome scheduled %s: sun_alt=%s is_night=%s next=+%ds",
                            host_key,
                            "%.1f" % sun_alt if sun_alt is not None else "n/a",
                            is_night, interval,
                        )
                        _dome_next_render[host_key] = time.time() + interval

            # Night-timelapse builds run in the main thread, driven by
            # on-disk state (the night's MP4 + attempt marker) instead of
            # a single dawn-crossing shot: the crossing fires at ~03:20
            # UTC, hours before stations publish their per-camera
            # timelapses, so the one attempt always found nothing and the
            # marker closed the night (zero dome timelapses fleet-wide
            # since 2026-09-08). A build whose inputs are missing leaves
            # no marker and is retried every _DOME_TIMELAPSE_RETRY_S; the
            # marker is written once a real build ran, or at give-up.
            now_utc = datetime.now(timezone.utc)
            tl_night = _dome_capture_night(now_utc - timedelta(hours=2))
            for host_key in list(config.stations):
                sun_alt = _station_sun_alt(host_key)
                if _dome_tl_next_try.get(host_key, 0.0) > time.time():
                    continue
                state = _timelapse_retry_state(
                    _sky_dome_timelapse_path(host_key, tl_night).is_file(),
                    _sky_dome_timelapse_attempt_marker(
                        host_key, tl_night).exists(),
                    sun_alt, now_utc.hour,
                )
                if state == "skip":
                    continue
                _dome_tl_next_try[host_key] = (
                    time.time() + _DOME_TIMELAPSE_RETRY_S
                )
                if state == "give_up":
                    logger.warning(
                        "dome timelapse %s/%s: inputs never appeared, "
                        "closing the night", host_key, tl_night,
                    )
                    try:
                        marker = _sky_dome_timelapse_attempt_marker(
                            host_key, tl_night)
                        marker.parent.mkdir(parents=True, exist_ok=True)
                        marker.touch()
                    except OSError:
                        pass
                    continue
                logger.info(
                    "dome timelapse due for %s, building for %s",
                    host_key, tl_night,
                )
                try:
                    _build_sky_dome_timelapse_from_mp4s(host_key, tl_night)
                except Exception:
                    logger.warning(
                        "dome timelapse build failed for %s/%s",
                        host_key, tl_night, exc_info=True,
                    )
        except Exception:
            logger.exception("sky-dome scheduler outer loop error")
        time.sleep(15)


# ── /api/sky_dome_timelapse routes ───────────────────────────────────

@require_station
def api_sky_dome_timelapse_dates(host_key: str):
    """List available BW dome timelapse dates for a station, newest first."""
    _require_station(host_key)
    d = _sky_dome_timelapse_dir(host_key)
    local_dates = {p.stem for p in d.glob("*.mp4")} if d.exists() else set()
    # Union in nights the station uploaded its own dome timelapse for (PR2).
    dates = sorted(local_dates | _station_dome_timelapse_dates(host_key), reverse=True)
    return jsonify({"dates": dates})


@require_station
def api_sky_dome_timelapse_mp4(host_key: str, date: str):
    """Serve a single BW dome timelapse MP4 for ``date`` (YYYYMMDD)."""
    _require_station(host_key)
    if not re.fullmatch(r"\d{8}", date):
        abort(400)
    # Prefer a station-uploaded dome timelapse from the archive (PR2);
    # fall back to the VPS-built one in the local cache.
    st = _station_dome_timelapse_archive_path(host_key, date)
    if st is not None and _sshfs_exists(st):
        path = st
    else:
        path = _sky_dome_timelapse_path(host_key, date)
        if not path.exists():
            abort(404)
    response = send_file(path, mimetype="video/mp4", conditional=True)
    response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ── /api/latest_frame, /api/latest_ff_maxpixel, /api/latest_chunk ───

@require_station
def api_latest_frame(host_key: str, cam: str):
    """A camera's newest colour chunk stack (or RMS captured stack), as the
    station last pushed it (#1249). 404 ``no_recent_frame`` when nothing
    arrived in ``_LIVE_FRAME_MAX_AGE_S``; the station is never contacted.
    ``X-Frame-Time`` is the source file's time on the station."""
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
        abort(400)
    frame = pushed_calibration.get_live_frame(
        host_key, cam.upper(), max_age_s=_LIVE_FRAME_MAX_AGE_S)
    if frame is None:
        return jsonify({"error": "no recent frame", "state": "no_recent_frame"}), 404
    hdrs = {"Content-Type": frame["content_type"], "Cache-Control": "no-cache",
            "Access-Control-Expose-Headers": "X-Frame-Time"}
    if frame.get("frame_time"):
        hdrs["X-Frame-Time"] = frame["frame_time"]
    resp = Response(frame["data"], status=200, headers=hdrs)
    resp.set_etag(hashlib.blake2b(frame["data"], digest_size=12).hexdigest())
    return resp.make_conditional(request)


@require_station
def api_latest_ff_maxpixel(host_key: str, cam: str):
    """BW FF maxpixel for the overview map station band.

    Prefers the latest pushed thumbnail (reversed-HTTP push §9): the station
    pusher POSTs the newest FF maxpixel to the VPS ingest and it is cached
    latest-per-(station,cam) in memory, so the dashboard need not reach into
    :7779. Falls back to the on-demand :7779 proxy when no pushed thumbnail
    is present (station not yet enrolled in push, or dashboard just
    restarted) — the pull path is preserved unchanged.
    """
    cache = _deps().cache
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
        abort(400)
    # 1) Pushed thumbnail (no station reach). Serve if present.
    thumb = cache.get_live_thumb(host_key, cam.upper())
    if thumb and thumb.get("data"):
        # no-cache + ETag instead of no-store: the map and Live View poll
        # every 15 s, and between FF files (all day, when capture is off)
        # the browser revalidates and gets a bodiless 304 rather than the
        # same image again. X-FF-Timestamp rides on the 304 too.
        hdrs = {
            "Content-Type": thumb.get("content_type", "image/webp"),
            "Cache-Control": "no-cache",
            "Access-Control-Expose-Headers": "X-FF-Timestamp",
            "X-Thumb-Source": "push",
        }
        if thumb.get("ff_timestamp"):
            hdrs["X-FF-Timestamp"] = thumb["ff_timestamp"]
        resp = Response(thumb["data"], status=200, headers=hdrs)
        resp.set_etag(hashlib.blake2b(thumb["data"], digest_size=12).hexdigest())
        return resp.make_conditional(request)
    # 2) Short-circuit a station we already KNOW is down. The pull below
    #    reaches into the station with a 15 s timeout, so an overnight tab
    #    polling a handful of dead cameras every 15 s pinned request threads
    #    on connect timeouts just to produce the same 503 (3,751 of them
    #    between 00:00 and 05:00 on 2026-08-25). The status poller already
    #    demotes a stale snapshot to online=False, and this is the same
    #    signal the overview map renders from, so the answer cannot differ
    #    -- we just reach it immediately.
    #    Only a station positively reported online is tried (#1249): a
    #    status the poller has not filled yet is not a reason to reach in.
    status = cache.get_status(host_key)
    if not (status or {}).get("online", False):
        return jsonify({"error": "station offline"}), 503
    # 3) Fallback: on-demand pull to the station :7779 (unchanged).
    try:
        url = _media_url(host_key, "api", "latest_ff_maxpixel", cam)
        resp = _session_for_url(url).get(url, timeout=15)
        if resp.status_code == 200 and resp.content:
            hdrs = {
                "Content-Type": resp.headers.get("Content-Type", "image/webp"),
                "Cache-Control": "public, max-age=30",
                "Access-Control-Expose-Headers": "X-FF-Timestamp",
            }
            ff_ts = resp.headers.get("X-FF-Timestamp")
            if ff_ts:
                hdrs["X-FF-Timestamp"] = ff_ts
            return Response(resp.content, status=200, headers=hdrs)
    except Exception:
        pass
    return jsonify({"error": "no ff maxpixel available"}), 503


def _render_live_chunk(host_key: str, cam: str, date: str, filename: str) -> None:
    """Ask the station for the chunk and hang up once headers arrive.

    The station answers only after its overlay gate has burned the
    overlay in, so the headers are the "rendered" signal; the 15 MB body
    is not needed (the page streams it through /video/ afterwards).
    """
    config = _deps().config
    tunnels = _deps().tunnels
    url = _rh_media_url(config, tunnels, host_key, "color_capture", cam, date, filename)
    resp = _session_for_url(url).get(
        url, timeout=(5, LIVE_WARM_READ_TIMEOUT_S), stream=True)
    resp.close()


@require_station
def api_latest_chunk(host_key: str, cam: str):
    """The chunk a live-tile click should play, and whether it is ready.

    ``overlaid: true`` means the station will stream it at once. Otherwise
    the first call starts the station-side overlay render in the
    background (``rendering: true``) and the page polls with
    ``?date=&filename=`` for that same chunk until it is overlaid; a poll
    never starts a render, so polls landing on other workers cannot fan
    out extra encodes.
    """
    config = _deps().config
    tunnels = _deps().tunnels
    cache = _deps().cache
    live_warmer = _deps().live_warmer
    _require_station(host_key)
    if not re.match(r"^[A-Z0-9]+$", cam, re.IGNORECASE):
        abort(400)
    want = request.args.get("filename") or None
    want_date = request.args.get("date") or None
    if want and not _LIVE_CHUNK_RE.match(want):
        abort(400)
    if want_date and not re.match(r"^\d{8}$", want_date):
        abort(400)
    # Playing a chunk streams it from the station, so an offline station
    # has nothing to offer: answer at once instead of a timed-out pull.
    if not (cache.get_status(host_key) or {}).get("online", False):
        return jsonify({"error": "station offline"}), 404
    from datetime import datetime as _dt, timedelta as _td, timezone as _tz
    now = _dt.now(_tz.utc)
    dates = ([want_date] if want_date else
             [now.strftime("%Y%m%d"), (now - _td(days=1)).strftime("%Y%m%d")])
    for date in dates:
        try:
            raw = station_get_raw(
                config, tunnels, host_key, f"/api/chunks/{cam}/{date}"
            )
        except Exception:
            continue
        chunks = raw.get("chunks", raw) if isinstance(raw, dict) else raw
        pick = _pick_live_chunk(chunks or [], want=want,
                                inflight=live_warmer.inflight(host_key, cam))
        if pick is None:
            continue
        fn = pick.get("filename", "")
        overlaid = bool(pick.get("reencoded"))
        rendering = False
        if not overlaid:
            rendering = want is not None or live_warmer.inflight(host_key, cam) == fn
            if not want:
                rendering = live_warmer.start(host_key, cam, date, fn) or rendering
        return jsonify({
            "date": date,
            "filename": fn,
            "video_url": f"/video/{host_key}/{cam}/{date}/{fn}",
            "time": pick.get("time"),
            "overlaid": overlaid,
            "rendering": rendering,
        })
    return jsonify({"error": "no chunks available"}), 404


# ── /api/twilight/network/<date> ─────────────────────────────────────

@public_route
def api_twilight_network(date: str):
    """The fleet's footage window for one night (astronomy.capture_window,
    sun -9° → -9° + 30 min): the earliest start and the latest end over
    every station in the live registry with coordinates (UTC minutes of
    day), so a new station counts from the night it is added. It bounds
    the time pickers on Detections, Multi-Station Events and the Grid.
    Anonymous callers get it over the public stations only, like the
    per-station endpoint below."""
    config = _deps().config
    if not re.match(r"^\d{8}$", date):
        abort(400)
    windows = {}
    for host_key, station in config.stations.items():
        if station.lat is None or station.lon is None:
            continue
        if not station_is_public_for_request(config, host_key):
            continue
        win = capture_window(station.lat, station.lon, date)
        if win:
            windows[host_key] = win
    net = network_capture_window(windows)
    if net is None:
        return _json_cached({"capture_start_min": None, "capture_end_min": None, "station_count": 0}, max_age=3600)
    return _json_cached(net, max_age=3600)


# ── /api/twilight/<host_key>/<date> ──────────────────────────────────

@public_route
def api_twilight(host_key: str, date: str):
    """Return sunset/sunrise UTC times for a station on a given date.

    Pure-function output (station lat/lon x date) that never changes
    once computed — paired with lru_cache on the underlying compute
    helpers, a 1 h browser cache keeps tab-switching free of work.

    Plain (page-less) ``@public_route``: the Detections + station-detail
    twilight sliders all need it, so it's always-eligible rather than tied
    to one page toggle. The payload is derived purely from the station's
    published lat/lon and the date (sunset/sunrise/moon) — no IP, cam_ip,
    or host path. Anonymous callers may still only read it for a
    ``public: true`` station: a private/commissioning station 404s so its
    very coordinates aren't disclosed. Logged-in operators keep the fleet."""
    config = _deps().config
    _require_station(host_key)
    if not station_is_public_for_request(config, host_key):
        abort(404)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    station = config.stations.get(host_key)
    if not station or station.lat is None or station.lon is None:
        return _json_cached({
            "sunset_utc": "18:00", "sunrise_utc": "06:00",
            "sunset_min": 1080, "sunrise_min": 360,
            "duration_hours": 12.0,
            "nautical_sunset_min": 1065, "nautical_sunrise_min": 375,
            "civil_sunset_min": 1050, "civil_sunrise_min": 390,
            "capture_start_min": 1065, "capture_end_min": 405,
        }, max_age=3600)
    astro    = _compute_twilight(station.lat, station.lon, date, horizon=-18.0)
    nautical = _compute_twilight(station.lat, station.lon, date, horizon=-12.0)
    civil    = _compute_twilight(station.lat, station.lon, date, horizon=-6.0)
    capture  = capture_window(station.lat, station.lon, date)
    return _json_cached({
        **astro,
        "nautical_sunset_min":  nautical.get("sunset_min"),
        "nautical_sunrise_min": nautical.get("sunrise_min"),
        "civil_sunset_min":     civil.get("sunset_min"),
        "civil_sunrise_min":    civil.get("sunrise_min"),
        # footage window (sun -9° → -9° + 30 min, astronomy.capture_window):
        # the time pickers' limits
        "capture_start_min":    capture[0] if capture else None,
        "capture_end_min":      capture[1] if capture else None,
        **_compute_moon_phase(date),
        **_compute_moon_rise_set(station.lat, station.lon, date),
    }, max_age=3600)


# ── Registration ────────────────────────────────────────────────────────

def register_sky_dome_routes(
    app: Flask,
    config: DashboardConfig,
    tunnels: TunnelManager,
    cache: StationCache,
    *,
    platepar_cache: dict[str, tuple[float, Any]],
    get_timelapses_payload: Callable[[str], dict[str, Any]],
) -> None:
    """Attach sky-dome, latest-frame/ff/chunk, and twilight routes.

    Stores the dependencies (see ``SkyDomeDeps``) in ``_DEPS`` for the
    module-level handlers, helpers and scheduler, replacing any earlier
    registration's, then adds the URL rules. Each endpoint name is the
    view function's name, as ``@app.route`` gave it.
    """
    global _DEPS
    live_warmer = _LiveChunkWarmer(_render_live_chunk)
    app._live_chunk_warmer = live_warmer  # tests
    _DEPS = SkyDomeDeps(
        config=config,
        tunnels=tunnels,
        cache=cache,
        platepar_cache=platepar_cache,
        get_timelapses_payload=get_timelapses_payload,
        live_warmer=live_warmer,
    )
    app.add_url_rule("/api/sky_dome/<host_key>.png", view_func=api_sky_dome)
    app.add_url_rule("/api/sky_dome_meta/<host_key>", view_func=api_sky_dome_meta)
    app.add_url_rule("/api/sky_dome_timelapse/<host_key>/dates",
                     view_func=api_sky_dome_timelapse_dates)
    app.add_url_rule("/api/sky_dome_timelapse/<host_key>/<date>.mp4",
                     view_func=api_sky_dome_timelapse_mp4)
    app.add_url_rule("/api/latest_frame/<host_key>/<cam>", view_func=api_latest_frame)
    app.add_url_rule("/api/latest_ff_maxpixel/<host_key>/<cam>",
                     view_func=api_latest_ff_maxpixel)
    app.add_url_rule("/api/latest_chunk/<host_key>/<cam>", view_func=api_latest_chunk)
    app.add_url_rule("/api/twilight/network/<date>", view_func=api_twilight_network)
    app.add_url_rule("/api/twilight/<host_key>/<date>", view_func=api_twilight)


def start_dome_scheduler() -> threading.Thread:
    """Create and start the sky-dome background scheduler thread.

    Must be called after ``register_sky_dome_routes``: the scheduler runs
    with the deps registered last.
    """
    if _DEPS is None:
        raise RuntimeError(
            "start_dome_scheduler called before register_sky_dome_routes"
        )
    t = threading.Thread(
        target=_sky_dome_scheduler,
        args=(_DEPS,),
        daemon=True,
        name="sky-dome-scheduler",
    )
    t.start()
    return t
