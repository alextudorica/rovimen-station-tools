"""Shared helpers for route modules.

Avoids duplicating validation, proxy, and media URL logic across modules.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any

from flask import abort, jsonify

from station_client import station_get_raw, station_url

logger = logging.getLogger(__name__)

# Synthetic filename injected into the RMS plots list by the frontend and the
# plot-image proxy so that the colour meteor stack can be fetched through the
# same /api/rms/plot_image/... path as ordinary RMS plot images.  Must match
# the value used in static JS (overview.js / view.js). Defined with the other
# Final Data Products names in fdp_products.
from fdp_products import COLOR_METEOR_STACK_FILENAME  # noqa: E402,F401


def lookup_station(config, host_key: str):
    station = config.stations.get(host_key)
    if station is None:
        abort(404)
    return station


def invalidate_proxy_cache(
    proxy_cache: dict, proxy_cache_lock, host_key: str, path: str,
) -> None:
    with proxy_cache_lock:
        proxy_cache.pop((host_key, path), None)


def proxy_error_response(
    host_key: str, exc: Exception, *, status: int = 502,
) -> tuple:
    logger.warning("Proxy to %s failed: %s", host_key, exc)
    return jsonify({"error": "station unavailable", "offline": True}), status


def proxy_get(
    config, tunnels, host_key: str, path: str,
    *,
    proxy_cache: dict,
    proxy_cache_lock,
    json_cached,
    status_on_error: int = 503,
    cache_ttl: float = 0.0,
):
    if cache_ttl <= 0:
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, path)), 200
        except Exception as exc:
            return proxy_error_response(host_key, exc, status=status_on_error)

    key = (host_key, path)
    now_mono = time.monotonic()
    with proxy_cache_lock:
        entry = proxy_cache.get(key)
    if entry is not None and now_mono < entry[0]:
        return json_cached(entry[1], max_age=int(cache_ttl))
    try:
        data = station_get_raw(config, tunnels, host_key, path)
    except Exception as exc:
        if entry is not None:
            return json_cached(entry[1], max_age=int(cache_ttl))
        return proxy_error_response(host_key, exc, status=status_on_error)
    with proxy_cache_lock:
        if len(proxy_cache) > 500:
            cache_keys = list(proxy_cache.keys())
            for k in cache_keys:
                if proxy_cache.get(k, (0,))[0] < now_mono:
                    proxy_cache.pop(k, None)
            if len(proxy_cache) > 500:
                by_age = sorted(proxy_cache.items(), key=lambda x: x[1][0])
                for k, _ in by_age[:100]:
                    proxy_cache.pop(k, None)
        proxy_cache[key] = (now_mono + cache_ttl, data)
    return json_cached(data, max_age=int(cache_ttl))


def media_url(config, tunnels, host_key: str, *path_parts: str) -> str:
    return station_url(config, tunnels, host_key, "/" + "/".join(path_parts))


def validate_media_params(
    config, host_key: str, station_code: str, date: str,
    filename: str, ext_re: str,
) -> None:
    lookup_station(config, host_key)
    for part in (station_code, date, filename):
        if ".." in part or "/" in part:
            abort(400)
    if not re.match(r"^[A-Z0-9]+$", station_code, re.IGNORECASE):
        abort(400)
    if not re.match(r"^\d{8}$", date):
        abort(400)
    if not re.match(ext_re, filename):
        abort(400)


def compute_detection_offset(
    filename: str,
    meteor_time_str: str | None,
    detection_time_str: str | None,
) -> float | None:
    """Compute seconds offset of a detection within a chunk video.

    Uses the chunk filename timestamp (UTC) and the meteor_time or
    detection_time from state.json lock metadata.  Returns None when
    the filename doesn't match the standard chunk pattern or no
    usable time is available.

    Accepts both the plain chunk form ``<cam>_<date>_<Tstart>_color.mkv`` and
    the boundary-stitched form ``<cam>_<date>_<Tstart>_<Tend>_color.mkv`` that
    detection_lock.py writes when a meteor lands near a chunk edge.  Both name
    the clip's own start time first, so the offset arithmetic is identical.
    """
    fn_m = re.match(
        r'^[A-Z0-9]+_\d{8}_(\d{6})(?:_\d{6})?_color\.(mkv|mp4)$', filename
    )
    if not fn_m:
        return None
    tp = fn_m.group(1)
    chk_s = int(tp[:2]) * 3600 + int(tp[2:4]) * 60 + int(tp[4:6])
    try:
        if meteor_time_str:
            mt = datetime.fromisoformat(meteor_time_str)
            det_s = mt.hour * 3600 + mt.minute * 60 + mt.second + mt.microsecond / 1e6
            diff = det_s - chk_s
            if diff < -43200:
                diff += 86400
            elif diff < 0:
                diff = 0.0
            return round(diff, 2)
        if detection_time_str:
            ts = detection_time_str.split("_", 1)[-1]
            if len(ts) == 6:
                det_s = int(ts[:2]) * 3600 + int(ts[2:4]) * 60 + int(ts[4:6])
                diff = det_s - chk_s
                if diff < -43200:
                    diff += 86400
                elif diff < 0:
                    diff = 0.0
                return round(diff, 1)
    except Exception:
        pass
    return None


# A clip's stack shares its base name: ``X_color.mkv``, its faststart
# ``X_color.mp4`` copy and the ``X_color.details.*`` overlay renders all map to
# ``X_stack.webp``. The old ``.replace("_color.mkv", ...).replace(".mp4", ...)``
# chain turned ``X_color.mp4`` into ``X_color_stack.webp``, so every .mp4 card
# had no thumbnail.
_CLIP_SUFFIXES = (".details.mkv", ".details.mp4", "_color.mkv", "_color.mp4", ".mkv", ".mp4")


def clip_base(name: str) -> str:
    """``X_color.mkv`` for any of a clip's video names (``.mp4`` copy and
    ``.details`` renders included); other names come back unchanged."""
    for suffix in (".details.mkv", ".details.mp4"):
        if name.endswith(suffix):
            name = name[: -len(suffix)] + ".mkv"
            break
    if name.endswith("_color.mp4"):
        return name[: -len(".mp4")] + ".mkv"
    return name


def stack_name_for_clip(name: str) -> str | None:
    """The stack webp for a clip video name, or None for a non-clip name."""
    base = clip_base(name)
    for suffix in ("_color.mkv", ".mkv", ".mp4"):
        if base.endswith(suffix):
            return base[: -len(suffix)] + "_stack.webp"
    return None


def listed_clip_names(meteor_files) -> list[str]:
    """One name per clip for archive listings, in listing order: the base
    ``_color.mkv`` (or a bare ``.mkv``/``.mp4``), with the faststart
    ``_color.mp4`` copy listed only when its ``.mkv`` is gone, and the
    ``.details`` renders never (callers swap those in for their base)."""
    names = [n for n in meteor_files if n.endswith((".mkv", ".mp4"))]
    present = set(names)
    out: list[str] = []
    for name in names:
        if name.endswith((".details.mkv", ".details.mp4")):
            continue
        if name.endswith("_color.mp4") and clip_base(name) in present:
            continue
        out.append(name)
    return out
