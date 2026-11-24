"""Station proxy routes -- timelapses, settings, hardware, platepar.

Extracted from rovimen_dashboard.py as part of the route-splitting effort.
All routes preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_station_proxy_routes``.

Closure state (``_proxy_cache``, ``_proxy_cache_lock``, SWR caches,
platepar cache) is passed in from the caller so that the main module
and this module share the same cache instances.
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed, CancelledError
from typing import Any, Callable

from flask import Flask, abort, jsonify, request

import pushed_calibration
import upload_state
from auth import is_anonymous, require_station, station_is_public_for_request
from http_caching import _json_cached
from route_helpers import (
    lookup_station,
    invalidate_proxy_cache,
    proxy_error_response,
    proxy_get,
)
from security import public_route
from station_client import station_url, station_get_raw, _session_for_url
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

_STATION_POLL_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="timelapse-poll"
)


def register_station_proxy_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    timelapses_swr_cache: dict[str, tuple[float, dict[str, Any]]],
    timelapses_swr_inflight: set[str],
    timelapses_swr_lock: threading.Lock,
    timelapses_fresh_ttl: float,
    platepar_cache: dict[str, tuple[float, Any]],
    platepar_ttl: float,
    proxy_cache: dict[tuple[str, str], tuple[float, Any]],
    proxy_cache_lock: threading.Lock,
    prefetch_executor: ThreadPoolExecutor,
    archive_idx,
) -> Callable[[str], dict[str, Any]]:
    """Register station proxy routes and return ``_get_timelapses_swr``.

    The returned accessor is the SWR-cached timelapses lookup, needed by
    ``public_api.register_public_routes`` as ``get_timelapses_payload``.
    """
    _require_station_impl = lookup_station
    _invalidate_proxy_cache_impl = invalidate_proxy_cache
    _proxy_error_response_impl = proxy_error_response
    _proxy_get_impl = proxy_get

    # ── Timelapses (SWR-cached, archive-only) ───────────────────────────

    def _walk_archive_timelapses(cam_code: str) -> list[dict]:
        out: list[dict] = []
        for date in archive_idx.nights(cam_code):
            # The index remembers every listed night's timelapse files, so a
            # walk over a camera's whole history stays in RAM; only a night
            # never listed before costs an on-demand sshfs scan (once).
            timelapse_files = archive_idx.timelapse_files_known(cam_code, date)
            if timelapse_files is None:
                ni = archive_idx.night_files(cam_code, date)
                if ni is None:
                    continue
                timelapse_files = ni.timelapse_files
            mp4s = sorted(f for f in timelapse_files if f.endswith(".mp4"))
            if not mp4s:
                continue
            webp_names = {f for f in timelapse_files if f.endswith(".webp")}
            mp4 = mp4s[0]
            stack_name = mp4.replace("_timelapse.mp4", "_night_stack.webp")
            out.append({
                "date": date,
                "filename": mp4,
                "night_stack": stack_name if stack_name in webp_names else None,
                "source": "archive",
            })
        return out

    def _compute_timelapses_payload(host_key: str) -> dict[str, Any]:
        """Per-camera timelapse listing for a station, from the archive only.

        The station's own ``/api/timelapses`` used to be asked first and the
        archive merged in behind it. Everything it listed is uploaded by
        archive_upload, the entries have the same shape, and the public API
        already built every URL from the Storage Box -- so a station-only entry
        was a dead link there anyway. A night appears once its timelapse is
        uploaded.
        """
        station_obj = config.stations.get(host_key)
        cam_codes = [c.code for c in station_obj.cameras] if station_obj else []
        # Walks for different cameras hit independent SSHFS subtrees and
        # are otherwise independent, so fan them out in parallel. A 5-cam
        # station used to take up to 5 × 30 s of serial SSHFS
        # walking on a cold mount; now bounded by the slowest single walk.
        archive_results: dict[str, list[dict]] = {}
        if cam_codes:
            future_to_cam = {
                _STATION_POLL_EXECUTOR.submit(_walk_archive_timelapses, c): c
                for c in cam_codes
            }
            try:
                for fut in as_completed(future_to_cam, timeout=15):
                    cam_code = future_to_cam[fut]
                    try:
                        archive_results[cam_code] = fut.result() or []
                    except (Exception, CancelledError):
                        archive_results[cam_code] = []
            except TimeoutError:
                logger.warning("timelapse archive walk timed out for %s, using partial results", host_key)
                for fut, cam_code in future_to_cam.items():
                    if cam_code not in archive_results:
                        fut.cancel()
                        archive_results[cam_code] = []
        return {
            cam_code: archive_results[cam_code]
            for cam_code in cam_codes
            if archive_results.get(cam_code)
        }

    def _refresh_timelapses(host_key: str) -> None:
        # Coalesced background refresh. On failure (exception OR empty
        # result that wasn't empty before — though we don't currently
        # distinguish), keep the previous cache rather than poison it.
        with timelapses_swr_lock:
            if host_key in timelapses_swr_inflight:
                return
            timelapses_swr_inflight.add(host_key)
        try:
            data = _compute_timelapses_payload(host_key)
            with timelapses_swr_lock:
                timelapses_swr_cache[host_key] = (time.monotonic(), data)
        except Exception:
            logger.exception("background timelapses refresh failed for %s", host_key)
        finally:
            with timelapses_swr_lock:
                timelapses_swr_inflight.discard(host_key)

    def _get_timelapses_swr(host_key: str) -> dict[str, Any]:
        """Stale-while-revalidate accessor for the per-host timelapses payload.

        Returns the cached dict immediately on any hit (fresh or stale),
        kicking a background refresh on stale; falls through to one
        synchronous compute on a truly cold cache. Used by both the
        authenticated dashboard route and the public API — neither should
        ever pay the full per-cam SSHFS walk on the request thread when a
        prior payload exists.
        """
        now_mono = time.monotonic()
        with timelapses_swr_lock:
            cached = timelapses_swr_cache.get(host_key)
            if cached is None and host_key in timelapses_swr_inflight:
                return {}
        if cached is not None:
            age = now_mono - cached[0]
            if age >= timelapses_fresh_ttl:
                prefetch_executor.submit(_refresh_timelapses, host_key)
            return cached[1]
        with timelapses_swr_lock:
            timelapses_swr_inflight.add(host_key)
        try:
            data = _compute_timelapses_payload(host_key)
        finally:
            with timelapses_swr_lock:
                timelapses_swr_inflight.discard(host_key)
        with timelapses_swr_lock:
            timelapses_swr_cache[host_key] = (time.monotonic(), data)
        return data

    @app.route("/api/timelapses/<host_key>")
    @require_station
    def api_timelapses(host_key: str):
        """SWR-cached archive listing (see ``_get_timelapses_swr`` and
        ``_compute_timelapses_payload``); never contacts the station."""
        _require_station_impl(config, host_key)
        return _json_cached(_get_timelapses_swr(host_key), max_age=60)

    # ── Settings / hardware / platepar ───────────────────────────────────

    def _known_online(host_key: str) -> bool:
        """True only when the status cache positively reports the station online."""
        status = cache.get_status(host_key)
        return bool((status or {}).get("online", False))


    @app.route("/api/settings/<host_key>")
    @require_station
    def api_settings(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/settings", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=10)

    # The settings PATCH proxy was removed 2026-07 with the read-only station
    # rollout — station config changes are SSH-only / signed-bundle-only.

    @app.route("/api/hardware/<host_key>")
    @require_station
    def api_hardware(host_key: str):
        _require_station_impl(config, host_key)
        # Pushed by the station with its calibration state (#1248); the live
        # proxy is only for a station whose pusher has not delivered one yet.
        pushed = pushed_calibration.get_hardware(host_key)
        if pushed is not None:
            return _json_cached(pushed, max_age=60)
        if not _known_online(host_key):
            return jsonify({"__missing": True, "error": "no hardware profile pushed yet"}), 404
        return _proxy_get_impl(config, tunnels, host_key, "/api/hardware", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=60)

    @app.route("/api/platepar/<host_key>")
    @public_route(page="events")
    def api_platepar_proxy(host_key: str):
        """Cached server-side (5 min via platepar_cache) + offline-tolerant.
        Client-side `Cache-Control: max-age=5` only — platepars are tuned
        interactively when calibrating cameras, and a 5-minute browser cache
        meant operator edits weren't visible until the cache expired even
        though the server had the new value. Keep the server cache long
        (round-trips to the station are expensive) but let the browser
        re-validate on every interaction; ETag makes the re-validation a
        304 in the unchanged case.

        Public under the "events" page toggle: the Detections page draws the
        FOV / radiant overlay from each camera's platepar (az_centre etc.).
        The payload carries only astrometric calibration (RA/Dec centre,
        rotation, scale) — no IP / cam_ip / host paths — so exposing it adds
        nothing sensitive. A GET is a pure read; the write path (recalibration)
        never touches this route. Anonymous callers may only read platepars for
        ``public: true`` stations: a private/commissioning station 404s before
        any station round-trip, matching ``station_is_public_for_request``'s
        unknown-key fail-closed rule. Logged-in operators keep the full fleet.

        A public page never reaches a station: an anonymous request gets the
        pushed copy (#1248) or a 404 "not uploaded yet". Only a logged-in
        operator falls back to the live call, for a station whose pusher has
        not delivered one yet (the same rule as /api/hardware)."""
        station = _require_station_impl(config, host_key)
        if not station_is_public_for_request(config, host_key):
            abort(404)
        # The station pushes its platepars on change (#1248): serve that copy
        # and never contact the station when it exists.
        pushed = pushed_calibration.get_platepar(host_key)
        if pushed is not None:
            return _json_cached(pushed, max_age=5)
        if is_anonymous():
            return jsonify({
                "error": upload_state.NOT_UPLOADED,
                "kind": "platepar",
                "detail": "platepar not uploaded yet",
            }), 404
        cached = platepar_cache.get(host_key)
        now_mono = time.monotonic()
        status = cache.get_status(host_key)
        online = (status is None) or bool((status or {}).get("online", False))
        # Serve fresh cache hit
        if cached and now_mono < cached[0]:
            return _json_cached(cached[1], max_age=5)
        # Try live fetch when online; fall back to stale on failure. A
        # commissioning station is never contacted (Station.should_contact).
        if online and station.should_contact:
            try:
                payload = station_get_raw(config, tunnels, host_key, "/api/platepar")
                platepar_cache[host_key] = (now_mono + platepar_ttl, payload)
                import platepar_store; platepar_store.update_host(payload)
                return _json_cached(payload, max_age=5)
            except Exception:
                pass
        # Offline or live fetch failed — serve stale if we have any.
        if cached:
            return _json_cached(cached[1], max_age=5)
        if not station.should_contact:
            return jsonify({"error": "commissioning station, no platepar"}), 404
        return jsonify({"error": "station unreachable, no cached platepar"}), 503

    return _get_timelapses_swr
