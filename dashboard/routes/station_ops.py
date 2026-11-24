"""Station operations -- reboot, probe, updater, services, RMS plots, encoding.

Extracted from rovimen_dashboard.py as part of issue #328.  All routes
preserved verbatim -- same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_station_ops_routes``.

Closure state (``_proxy_cache``, ``_proxy_cache_lock``, the archive index)
is passed in from the caller so that the main module and this module share
the same instances.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import threading
import time
from typing import Any

import requests
from flask import Flask, Response, abort, jsonify, request, send_file

from cache_store import (
    ARCHIVE_PATH,
    THUMB_CACHE_DIR,
    BoundedCache,
    _thumb_cache_has_space,
    rms_night_generation,
)

import pushed_calibration
from auth import (
    camera_is_public_for_request,
    require_admin,
    require_station,
    station_is_public_for_request,
)
from http_caching import _json_cached
from security import public_route
from fdp_products import (
    archive_candidates as fdp_archive_candidates,
    kind_for_synthetic as fdp_kind_for_synthetic,
    product_filename as fdp_product_filename,
)
from route_helpers import (
    COLOR_METEOR_STACK_FILENAME,
    lookup_station,
    invalidate_proxy_cache,
    proxy_error_response,
    proxy_get,
    media_url,
)
from station_client import (
    _session_for_url,
    _with_sshfs_timeout,
    station_get_raw,
    station_url,
)
from tunnels import _TunnelDown

logger = logging.getLogger(__name__)

# Sanitisation regex for systemd-style service names.  Shared with the
# main module (defined there as _SERVICE_NAME_RE); kept as a local constant
# so this module is self-contained.
_SERVICE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")


# How long a cached mask.bmp may be served before the station is asked again.
# Ten minutes: a mask changes only when someone redraws it, but the page draws
# it over a night stack as a statement of what the camera excludes, so an
# unbounded cache turns a stale file into a standing false claim.
MASK_CACHE_TTL = 600


def station_json_payload(resp) -> tuple[dict, int]:
    """Coerce a station response into (JSON body, status) for a proxy to return.

    A station that answers at all does not necessarily answer in JSON: the
    commonest case is Flask's HTML 404 page from a station whose bundle
    predates the endpoint being called, which every station runs for the
    hours between a deploy and its next updater window. Calling ``.json()``
    on that raises out of the view, the dashboard returns its own HTML 500,
    and the browser-side caller -- which reads ``error`` out of a JSON body
    -- can only report "unknown error" for the one failure an operator is
    most likely to hit. So the station's status is passed through with a
    message that names it, plus an excerpt of what it actually said.
    """
    try:
        body = resp.json()
    except ValueError:
        excerpt = (resp.text or "").strip()[:200]
        return {
            "error": f"station returned HTTP {resp.status_code} (not JSON)",
            "detail": excerpt or "empty response body",
        }, resp.status_code
    if not isinstance(body, dict):
        return {"value": body}, resp.status_code
    return body, resp.status_code


_CHUNK_NAME_RE = re.compile(r"^[A-Z0-9]+_(\d{8})_(\d{6})_color\.mkv$")


def _stage_minutes(stage_times: dict | None, noon) -> dict[str, float]:
    """state.json's stage_times ("YYYY-MM-DDTHH:MM:SSZ") as minutes after
    the night's noon; unreadable entries are left out."""
    from datetime import datetime, timezone
    out: dict[str, float] = {}
    for stage, stamp in (stage_times or {}).items():
        try:
            t = datetime.strptime(str(stamp), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        out[str(stage)] = round((t - noon).total_seconds() / 60, 2)
    return out


def night_capture_summary(chunks: dict, date: str, stage_times: dict | None = None) -> dict:
    """One camera's night from its pushed state.json chunks: capture spans
    and pipeline counts, plus when each pipeline stage finished (``stages``,
    same minutes; empty for nights from stations that predate stage_times).

    Spans are [start, end] in minutes after 12:00 UTC on ``date`` (the noon
    anchor the twilight slider uses). A chunk name carries only its start, so
    each chunk is taken to last the median spacing between starts, and a
    spacing over 2.5 of those starts a new span."""
    from datetime import datetime, timezone
    noon = datetime.strptime(date + "12", "%Y%m%d%H").replace(tzinfo=timezone.utc)
    starts: list[float] = []
    real: dict = {}
    for name, c in (chunks or {}).items():
        m = _CHUNK_NAME_RE.match(name)
        if not m:
            continue
        real[name] = c if isinstance(c, dict) else {}
        t = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        starts.append((t - noon).total_seconds() / 60)
    starts.sort()
    steps = sorted(b - a for a, b in zip(starts, starts[1:]) if b > a)
    seg = steps[len(steps) // 2] if steps else 20 / 60
    spans: list[list[float]] = []
    for s in starts:
        if spans and s - spans[-1][1] <= seg * 1.5:
            spans[-1][1] = s + seg
        else:
            spans.append([s, s + seg])
    vals = real.values()
    return {
        "stages": _stage_minutes(stage_times, noon),
        "spans": [[round(a, 2), round(b, 2)] for a, b in spans],
        "total": len(real),
        "stacked": sum(1 for c in vals if c.get("stacked")),
        "reencoded": sum(1 for c in vals if c.get("reencoded")),
        "locked": sum(1 for c in vals if isinstance(c.get("lock"), dict)
                      and c["lock"].get("lock_type") == "detection"),
        "uploaded": sum(1 for c in vals if c.get("uploaded")),
    }


def register_station_ops_routes(
    app: Flask,
    config,
    tunnels,
    cache,
    *,
    proxy_cache: dict[tuple[str, str], tuple[float, Any]],
    proxy_cache_lock: threading.Lock,
    archive_idx=None,
) -> None:
    _require_station_impl = lookup_station
    _invalidate_proxy_cache_impl = invalidate_proxy_cache
    _proxy_error_response_impl = proxy_error_response
    _proxy_get_impl = proxy_get
    def _known_online(host_key: str) -> bool:
        """True only when the status cache positively reports the station online."""
        status = cache.get_status(host_key)
        return bool((status or {}).get("online", False))

    def _media_url(host_key: str, *path_parts: str) -> str:
        return media_url(config, tunnels, host_key, *path_parts)

    # ── RMS cameras ──────────────────────────────────────────────────────

    @app.route("/api/rms_cameras/<host_key>")
    @require_station
    def api_rms_cameras_proxy(host_key: str):
        _require_station_impl(config, host_key)
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, "/api/rms_cameras"))
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # Station mutations (reboot, updater, service restarts, settings, toggles)
    # were removed 2026-07 with the read-only station rollout — interventions
    # are SSH-only; fleet changes ship as signed bundles. Only read proxies
    # and the lock action (media.py, lock direction only) remain.

    # ── Probe ────────────────────────────────────────────────────────────

    @app.route("/api/probe/<host_key>", methods=["POST"])
    @require_station
    def api_probe(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/probe")
            resp = _session_for_url(url).post(url, timeout=60)
            return jsonify(resp.json()), resp.status_code
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Encoding stats ───────────────────────────────────────────────────

    @app.route("/api/encoding/stats/<host_key>")
    @require_station
    def api_encoding_stats(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/encoding/stats", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=5)

    # ── Overlay preview ──────────────────────────────────────────────────

    @app.route("/api/overlay_preview/<host_key>", methods=["POST"])
    @require_station
    def api_overlay_preview(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/overlay_preview")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=20,
            )
            resp.raise_for_status()
            return Response(resp.content, mimetype="image/png")
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Overlay logo ─────────────────────────────────────────────────────

    @app.route("/api/logo/list/<host_key>")
    @require_station
    def api_logo_list_proxy(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/logo/list", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, status_on_error=502, cache_ttl=60)

    @app.route("/api/overlay/logo/<host_key>", methods=["POST"])
    @require_admin
    def api_overlay_logo_proxy(host_key: str):
        """Set the station's overlay logo to one of its shipped images.

        The one config key the dashboard writes; the station accepts only a
        bare filename from its assets directory. The cached settings are
        dropped so the Station Admin page re-reads what actually landed."""
        _require_station_impl(config, host_key)
        body = request.get_json(silent=True) or {}
        try:
            url = station_url(config, tunnels, host_key, "/api/overlay/logo")
            resp = _session_for_url(url).post(url, json={"filename": body.get("filename")}, timeout=15)
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)
        if resp.status_code == 200:
            _invalidate_proxy_cache_impl(proxy_cache, proxy_cache_lock, host_key, "/api/settings")
        payload, status = station_json_payload(resp)
        return jsonify(payload), status

    # ── Updater (read-only status) ───────────────────────────────────────

    @app.route("/api/updater/status/<host_key>")
    @require_station
    def api_updater_status(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/updater/status", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, cache_ttl=5)

    # ── One night per camera, from the archive ───────────────────────────

    @app.route("/api/station_night/<host_key>/<date>")
    @require_station
    def api_station_night(host_key: str, date: str):
        """Capture spans and pipeline progress of each camera for one night,
        from the state.json the station pushes to the archive (#880: no
        station call). A camera whose state.json has not arrived yet is
        ``null``; ``state_at`` is when the station last pushed it (epoch s),
        so the page can say how far behind the station its copy is."""
        station = _require_station_impl(config, host_key)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        cameras: dict[str, dict | None] = {}
        for cam in station.cameras:
            ni = archive_idx.night_files(cam.code, date) if archive_idx is not None else None
            if ni is None or ni.state_chunks is None:
                cameras[cam.code] = None
                continue
            cameras[cam.code] = {
                **night_capture_summary(ni.state_chunks, date, ni.stage_times),
                "rms_complete": ni.rms_complete,
                "morning_done": ni.morning_done,
                "timelapse_done": ni.timelapse_done,
                "state_at": ni.state_mtime(),
            }
        return jsonify({"date": date, "cameras": cameras})

    # ── Crons ────────────────────────────────────────────────────────────

    @app.route("/api/crons/<host_key>")
    @require_station
    def api_crons(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(config, tunnels, host_key, "/api/crons", proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock, json_cached=_json_cached, status_on_error=502, cache_ttl=30)

    # ── RMS plots ────────────────────────────────────────────────────────

    # Colour meteor stack presence per camera night: (expiry_mono, present).
    # A stack older than the stacks/ prune lives only in fdp/, which the
    # archive index does not list, so answering "absent" costs a stat; this
    # keeps the no-meteor nights (most of them) from paying it per request.
    # BoundedCache is not thread-safe on its own, so every access holds the lock.
    _colour_stack_seen: BoundedCache = BoundedCache(maxsize=4096)
    _colour_stack_seen_lock = threading.Lock()
    _COLOUR_STACK_SEEN_TTL = 600.0

    def _archived_colour_stack(cam: str, date: str, ni) -> bool:
        name = fdp_product_filename("color_meteor_stack", cam, date)
        if ni is not None and name in ni.stack_files:
            return True
        key = (cam, date)
        now_mono = time.monotonic()
        with _colour_stack_seen_lock:
            hit = _colour_stack_seen.get(key)
        if hit is not None and hit[0] > now_mono:
            return hit[1]
        # The stat runs outside the lock: it can take seconds on a slow box.
        permanent = fdp_archive_candidates(ARCHIVE_PATH, "color_meteor_stack", cam, date)[0]
        present = bool(_with_sshfs_timeout(permanent.is_file, timeout=5.0, default=False))
        with _colour_stack_seen_lock:
            _colour_stack_seen[key] = (now_mono + _COLOUR_STACK_SEEN_TTL, present)
        return present

    def archived_plot_list(cam: str, date: str) -> list[dict]:
        """A night's plot list from the archive: the uploaded rms/ plots,
        labelled the way the station labels them, plus the synthetic colour
        meteor stack entry when that night's stack is archived."""
        from routes.archive import label_rms_plots
        ni = archive_idx.night_files(cam, date) if archive_idx is not None else None
        plots = label_rms_plots(ni.rms_files) if ni is not None else []
        if _archived_colour_stack(cam, date, ni):
            plots.append({
                "filename": COLOR_METEOR_STACK_FILENAME,
                "label":    "Meteor stack (color)",
                "order":    2,
            })
        return plots

    @app.route("/api/rms/plots/<host_key>/<cam>/<date>")
    @public_route(page="overview")
    def api_rms_plots_proxy(host_key: str, cam: str, date: str):
        """A night's RMS plot list, from the archive only (#1247).

        This used to ask the station for the list and HEAD its colour meteor
        stack -- two round-trips per night on every RMS-tab and station-page
        load -- with a disk copy for when the station was away. Every plot it
        listed is uploaded to the night's rms/ directory and the colour stack
        to stacks/ (fdp/ once pruned), so the archive answers the same
        question without the station. A night that is not uploaded yet lists
        nothing.

        Public under the "overview" page toggle: the payload carries only
        ``filename`` / ``label`` / ``order`` per plot (no ip / cam_ip / host
        paths), and a non-public station or its camera 404s before any lookup. Logged-in
        operators keep the full fleet.
        """
        _require_station_impl(config, host_key)
        if not (station_is_public_for_request(config, host_key)
                and camera_is_public_for_request(config, cam)):
            abort(404)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        return _json_cached(archived_plot_list(cam, date), max_age=300)

    def _read_first_archived(paths: list) -> bytes | None:
        """Bytes of the first of *paths* that exists on the archive, else None.
        Each probe is SSHFS-guarded so a stalled mount costs a bounded wait."""
        for path in paths:
            if not _with_sshfs_timeout(path.is_file, timeout=5.0, default=False):
                continue
            data = _with_sshfs_timeout(path.read_bytes, timeout=10.0, default=None)
            if data is not None:
                return data
        return None

    def _cache_and_serve(cache_file, data: bytes, content_type: str) -> Response:
        if _thumb_cache_has_space():
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(data)
            except Exception:
                pass
        # max_age=0 + ETag: the browser revalidates (a cheap 304) instead of
        # keeping a replaced plot for the app-wide 7-day max-age.
        return send_file(
            io.BytesIO(data), mimetype=content_type, max_age=0, conditional=True,
            etag=hashlib.sha1(data).hexdigest(),
        )

    def _serve_cached_rms_plot(cache_file, cam: str, date: str) -> Response | None:
        """The cached copy of an archived RMS plot, unless the archive's copy
        was replaced after it was cached (cache_store.invalidate_rms_night:
        a reprocess re-uploads the plots under the same names)."""
        try:
            mtime_ns = cache_file.stat().st_mtime_ns
        except OSError:
            return None
        if mtime_ns < rms_night_generation(cam, date):
            return None
        return send_file(cache_file, conditional=True, max_age=0)

    def _serve_night_stack_bw(host_key: str, cam: str, date: str, filename: str):
        """Serve the per-night colour composite as a GMN-style BW stack.

        Colour/spectro-only cameras (``rms: false``, e.g. gmnro01's GH662
        pair) upload no RMS plots at all, so the overview tile falls back to
        the dawn pipeline's ``<cam>_<date>_night_stack.webp`` (archived under
        ``<cam>/<date>/timelapse/``). Grayscaling gives the classic GMN
        captured-stack look; the render is done once and cached on NVMe.
        """
        cache_file = (
            THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / f"bw_{filename}"
        )
        if cache_file.exists():
            return send_file(cache_file, conditional=True)
        # The permanent fdp/ copy first: the timelapse/ upload copy is pruned
        # with the timelapse MP4 (see fdp_products).
        if filename == fdp_product_filename("night_composite", cam, date):
            sources = fdp_archive_candidates(ARCHIVE_PATH, "night_composite", cam, date)
        else:
            sources = [ARCHIVE_PATH / cam / date / "timelapse" / filename]
        data = _read_first_archived(sources)
        if data is None:
            abort(404)
        from io import BytesIO

        from PIL import Image

        try:
            img = Image.open(BytesIO(data)).convert("L")
            buf = BytesIO()
            img.save(buf, format="WEBP", quality=90)
            out = buf.getvalue()
        except Exception:
            logger.warning("night-stack BW render failed for %s/%s/%s",
                           cam, date, filename, exc_info=True)
            abort(404)
        if _thumb_cache_has_space():
            try:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_bytes(out)
            except Exception:
                pass
        return Response(out, status=200, headers={"Content-Type": "image/webp"})

    @app.route("/api/rms/plot_image/<host_key>/<cam>/<date>/<filename>")
    @public_route(page="overview")
    def api_rms_plot_image_proxy(host_key: str, cam: str, date: str, filename: str):
        """Serve RMS plot images from the archive -- raw bytes, no rotation.

        FF frames are always in native sensor orientation. Cameras flagged
        ``rotate: true`` in dashboard_config.yaml render upside-down here;
        the client applies a 180-degree CSS transform (``.thumb-rotated``)
        on the ``<img>`` instead. Avoids a PIL decode/re-encode per cache
        miss and lets the compositor flip after first paint.

        The colour meteor stack is already rotated station-side before it is
        uploaded, so the client skips the CSS class for that filename.

        Public under the "overview" page toggle: these JPG/PNG/WEBP plots are
        non-sensitive science imagery (captured-star stacks, photometry /
        detection plots) shown in the overview "Latest Plots and Data" grid.
        Anonymous callers may only fetch plots for ``public: true`` stations:
        a private/commissioning station or its camera 404s before any file read, matching ``station_is_public_for_request``'s unknown-key
        fail-closed rule. Logged-in operators keep the full fleet.
        """
        _require_station_impl(config, host_key)
        if not (station_is_public_for_request(config, host_key)
                and camera_is_public_for_request(config, cam)):
            abort(404)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        if not re.match(r"^\d{8}$", date):
            abort(400)
        if not re.match(r"^[\w._-]+\.(jpg|png|webp)$", filename):
            abort(400)
        if filename.endswith("_night_stack.webp"):
            return _serve_night_stack_bw(host_key, cam, date, filename)
        cache_file = THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / filename
        cached = _serve_cached_rms_plot(cache_file, cam, date)
        if cached is not None:
            return cached

        # Final Data Products (colour meteor stack, night composite): the
        # archive's permanent fdp/ copy, then the subdir it was uploaded into.
        # The night composite is served in colour here (the BW render above is
        # the overview tile's look, keyed by the real filename).
        fdp_kind = fdp_kind_for_synthetic(filename)
        if fdp_kind is not None:
            data = _read_first_archived(
                fdp_archive_candidates(ARCHIVE_PATH, fdp_kind, cam, date)
            )
            if data is not None:
                return _cache_and_serve(cache_file, data, "image/webp")
            abort(404)

        # Archive only (#1247): every caller builds this URL from an
        # archive-backed list (plots-latest, the archive plot list above, the
        # overview tiles), so a miss here means "not uploaded", not "ask the
        # station". The station prunes older nights anyway, so it could not
        # serve most of what the lists resolve to.
        archived = ARCHIVE_PATH / cam / date / "rms" / filename
        data = _read_first_archived([archived])
        if data is None:
            abort(404)
        return _cache_and_serve(
            cache_file, data,
            "image/jpeg" if filename.endswith(".jpg") else "image/png",
        )

    # ── Calibration page ─────────────────────────────────────────────────

    @app.route("/api/calibration_summary/<host_key>/<cam>")
    @require_station
    def api_calibration_summary_proxy(host_key: str, cam: str):
        """Thin passthrough to the station's astrometric drift summary.

        Path + forwarded query string, no server-side caching — the station endpoint itself is a single local
        JSON-file read, cheap enough not to need it)."""
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        # Pushed by the station for its last completed night (#1248). A
        # request for that night, or for no night in particular, is answered
        # from the pushed copy; only an explicit older date still goes live.
        pushed = pushed_calibration.get_calibration_summary(host_key, cam)
        want = request.args.get("date", "")
        if pushed is not None and (not want or want == pushed.get("date")):
            return jsonify(pushed)
        if not _known_online(host_key):
            return jsonify({"status": "no_data", "date": want or (pushed or {}).get("date"),
                            "detail": "station offline; no pushed summary for this night"})
        qs = request.query_string.decode()
        path = f"/api/calibration_summary/{cam}"
        if qs:
            path += f"?{qs}"
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, path))
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/calibration/autofit/<host_key>/<cam>", methods=["POST"])
    @require_station
    def api_calibration_autofit_proxy(host_key: str, cam: str):
        """Trigger a platepar auto-fit run on the station. Same shape as
        api_probe above -- a live POST, since this is an operator-initiated
        on-demand action (issue #880's push-first doctrine explicitly
        carves those out as a sanctioned live path, distinct from routine
        page rendering). require_station's write branch (admin or the
        owning host) gates who can call it.

        Forwards the request body (e.g. {"force": true} to override the
        station's capture-window guard) -- previously dropped entirely, so
        force could never actually reach the station through the dashboard."""
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        body = request.get_json(silent=True) or {}
        try:
            url = station_url(config, tunnels, host_key, f"/api/calibration/autofit/{cam}")
            resp = _session_for_url(url).post(url, json=body, timeout=15)
            return jsonify(resp.json()), resp.status_code
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/calibration/rebase_pointing/<host_key>/<cam>", methods=["POST"])
    @require_station
    def api_calibration_rebase_proxy(host_key: str, cam: str):
        """Trigger a pointing re-base. Same shape and same write gate as the
        auto-fit trigger; the result is polled through the shared autofit
        status endpoint, since both stage a candidate in the same place."""
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        body = request.get_json(silent=True) or {}
        try:
            url = station_url(config, tunnels, host_key, f"/api/calibration/rebase_pointing/{cam}")
            resp = _session_for_url(url).post(url, json=body, timeout=15)
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)
        payload, status = station_json_payload(resp)
        return jsonify(payload), status

    @app.route("/api/calibration/autofit_status/<host_key>/<cam>")
    @require_station
    def api_calibration_autofit_status_proxy(host_key: str, cam: str):
        """Poll the most recent auto-fit run's result. A read -- open to any
        signed-in account, same as api_calibration_summary_proxy above."""
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        try:
            return jsonify(station_get_raw(config, tunnels, host_key, f"/api/calibration/autofit_status/{cam}"))
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/calibration/autofit_accept/<host_key>/<cam>", methods=["POST"])
    @require_station
    def api_calibration_autofit_accept_proxy(host_key: str, cam: str):
        """Apply a staged auto-fit candidate as the live platepar. Same
        write-access gate as the trigger above."""
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        body = request.get_json(silent=True) or {}
        try:
            url = station_url(config, tunnels, host_key, f"/api/calibration/autofit_accept/{cam}")
            resp = _session_for_url(url).post(url, json=body, timeout=15)
            return jsonify(resp.json()), resp.status_code
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)

    @app.route("/api/latest_stack/<host_key>/<cam>")
    @require_station
    def api_latest_stack_proxy(host_key: str, cam: str):
        """Serve a camera's newest archived captured stack (#1247).

        The station's own ``/api/latest_stack`` serves its newest
        ``*_captured_stack.jpg``, which archive_upload also pushes into the
        night's rms/ directory -- so this resolves the same file from the
        archive index (newest night at or before last night that has one) and
        never asks the station. 404 until a night with a stack is uploaded.
        The file is per-night and immutable, so it shares the plot-image
        route's NVMe cache.
        """
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        if archive_idx is None:
            abort(404)
        from routes.archive import resolve_rms_plot_status
        res = resolve_rms_plot_status(archive_idx, cam, "_captured_stack.jpg")
        if res.get("status") != "ok" or not res.get("filename"):
            abort(404)
        date, filename = res["date"], res["filename"]
        cache_file = THUMB_CACHE_DIR / host_key / "rms_plots" / cam / date / filename
        cached = _serve_cached_rms_plot(cache_file, cam, date)
        if cached is not None:
            return cached
        data = _read_first_archived([ARCHIVE_PATH / cam / date / "rms" / filename])
        if data is None:
            abort(404)
        return _cache_and_serve(cache_file, data, "image/jpeg")

    @app.route("/api/mask_image/<host_key>/<cam>")
    @require_station
    def api_mask_image_proxy(host_key: str, cam: str):
        """Serve a camera's raw mask.bmp bytes, cached briefly on disk so the
        Calibration page's stack+mask overlay doesn't round-trip to every
        station on every load.

        The cache is time-bounded, unlike the plot-image cache it was modelled
        on. That cache is keyed by a per-night filename, so its entries are
        immutable by construction and can live forever. A mask is the opposite:
        one fixed path whose CONTENT changes whenever an operator redraws it.
        Serving it unconditionally meant the first fetch won permanently --
        every mask on the box was stamped 2026-07-30 and had been served
        unchanged for 53 days, including for cameras whose station no longer
        reports a mask at all. An overlay drawn on a night stack is a claim
        about what that camera is excluding right now, so it has to expire.

        A 404 evicts the entry outright rather than letting it age out: the
        station saying "no mask here" is positive evidence that a cached one
        is wrong, and continuing to draw it would assert something the station
        has just denied.
        """
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        # The station pushes its masks whenever one changes (#1248), so the
        # pushed copy is current by construction and needs no TTL. "none"
        # means the station's last push listed no mask for this camera.
        state, pushed_path = pushed_calibration.mask_state(host_key, cam)
        if state == "mask":
            resp = send_file(pushed_path, mimetype="image/bmp", conditional=True, max_age=0)
            resp.headers["Cache-Control"] = "no-cache"
            return resp
        if state == "none":
            abort(404)
        cache_file = THUMB_CACHE_DIR / host_key / "mask_image" / f"{cam}.bmp"
        if cache_file.exists() and (time.time() - cache_file.stat().st_mtime) < MASK_CACHE_TTL:
            return send_file(cache_file, conditional=True)
        if not _known_online(host_key):
            abort(404)
        try:
            url = _media_url(host_key, "api", "mask_image", cam)
            resp = _session_for_url(url).get(url, timeout=15)
            if resp.status_code == 200:
                data = resp.content
                if _thumb_cache_has_space():
                    try:
                        cache_file.parent.mkdir(parents=True, exist_ok=True)
                        cache_file.write_bytes(data)
                    except Exception:
                        pass
                return Response(data, status=200, headers={"Content-Type": "image/bmp"})
            if resp.status_code == 404:
                try:
                    cache_file.unlink(missing_ok=True)
                except OSError:
                    pass
            return Response(resp.content, status=resp.status_code)
        except Exception:
            abort(502)

    @app.route("/api/mask/<host_key>/<cam>", methods=["POST"])
    @require_station
    def api_mask_write_proxy(host_key: str, cam: str):
        """Replace a camera's detection mask with one drawn in the browser.

        An operator-initiated write, so the same carve-out the autofit
        endpoints get from issue #880's push-first doctrine applies: routine
        page rendering stays archive-only, a deliberate button press may go
        live. ``require_station``'s write branch means admin or the owning
        host, and the station keeps a timestamped backup of what it replaced.

        The cached copy of the old mask is dropped on success -- the
        Calibration page re-fetches ``/api/mask_image`` right after saving to
        show what actually landed, and that read is served from this cache
        with no expiry of its own.
        """
        _require_station_impl(config, host_key)
        if not re.match(r"^[A-Z0-9]+$", cam):
            abort(400)
        body = request.get_json(silent=True) or {}
        try:
            url = station_url(config, tunnels, host_key, f"/api/mask/{cam}")
            resp = _session_for_url(url).post(url, json=body, timeout=30)
        except (_TunnelDown, requests.RequestException, Exception) as exc:
            return _proxy_error_response_impl(host_key, exc)
        if resp.status_code == 200:
            try:
                (THUMB_CACHE_DIR / host_key / "mask_image" / f"{cam}.bmp").unlink(missing_ok=True)
                # The pushed copy is now the old mask; read the station until
                # the pusher delivers the new one.
                pushed_calibration.drop_mask(host_key, cam)
            except OSError:
                pass
        payload, status = station_json_payload(resp)
        return jsonify(payload), status

    # ── Storage watch ────────────────────────────────────────────────────

    @app.route("/api/storagewatch/<host_key>")
    @require_station
    def api_storagewatch(host_key: str):
        _require_station_impl(config, host_key)
        return _proxy_get_impl(
            config, tunnels, host_key, "/api/storagewatch/status",
            proxy_cache=proxy_cache, proxy_cache_lock=proxy_cache_lock,
            json_cached=_json_cached, status_on_error=502, cache_ttl=5,
        )

    # ── Encode chunk ─────────────────────────────────────────────────────

    @app.route("/api/encode_chunk/<host_key>", methods=["POST"])
    @require_station
    def api_encode_chunk(host_key: str):
        _require_station_impl(config, host_key)
        try:
            url = station_url(config, tunnels, host_key, "/api/encode_chunk")
            resp = _session_for_url(url).post(
                url, json=request.get_json(silent=True), timeout=120,
            )
            return jsonify(resp.json()), resp.status_code
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)

    # ── Log viewer proxy ─────────────────────────────────────────────────

    @app.route("/api/logs/<host_key>/<service>")
    @require_station
    def api_logs(host_key: str, service: str):
        _require_station_impl(config, host_key)
        if not _SERVICE_NAME_RE.match(service):
            return jsonify({"error": "invalid_service_name"}), 400
        lines = request.args.get("lines", "200")
        if not lines.isdigit() or int(lines) > 5000:
            lines = "200"
        date = request.args.get("date", "")
        if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            date = ""
        qs = f"lines={lines}" + (f"&date={date}" if date else "")
        try:
            url = station_url(config, tunnels, host_key, f"/api/logs/{service}?{qs}")
            resp = _session_for_url(url).get(url, timeout=15)
            resp.raise_for_status()
            return jsonify(resp.json())
        except Exception as exc:
            return _proxy_error_response_impl(host_key, exc)
