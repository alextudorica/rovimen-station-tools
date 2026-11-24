"""Overview and status routes -- config/all, overview stacks, GMN data,
station status/vitals, SSE event stream.

Extracted from rovimen_dashboard.py.  All routes preserved verbatim --
same URLs, same behaviour, same decorators.
Wired in from ``create_app()`` via ``register_overview_routes``.

Closure state (overview stacks cache, status/vitals last-seen dicts,
kick/record helpers, GMN cache access, morning_done) is passed in from
the caller so that the main module and this module share the same
mutable instances.
"""

from __future__ import annotations

import logging
import os
import queue
import secrets
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from flask import Flask, Response, abort, jsonify, request, session

import gmn_data
import station_orbits
from auth import (
    require_auth,
    require_admin,
    require_station,
    _session_role,
    _has_station_access,
    is_anonymous,
    public_host_keys,
)
from gmn_poller import (
    _gmn_cache,
    _gmn_cache_lock,
    _gmn_fetch_multistation,
    _gmn_get_cached,
    _gmn_monthly_count,
)
import detection_db
from http_caching import _json_cached
from route_helpers import lookup_station
from security import public_route
from capture_state import camera_capture_state
from segment_health import camera_segment_health, station_segment_alarms
from station_client import station_get_raw

logger = logging.getLogger(__name__)

_STATION_POLL_EXECUTOR = ThreadPoolExecutor(
    max_workers=8, thread_name_prefix="overview-poll"
)

_SSE_DEADLINE_S = 300  # 5 min — auto-close SSE to free the WSGI worker

# Fallback card images, best first, for a camera that has no archived captured
# stack at all. Image-like plots before charts: the grid is a wall of sky
# frames and a chart in it reads as an anomaly, which is the correct signal
# here (that camera never uploaded a stack).
_ARCHIVE_CARD_PREFERRED = (
    "_captured_stack.", "_meteors.", "_CAPTURED_thumbs.", "_DETECTED_thumbs.",
)

# Detection-index freshness, cached process-wide. A station whose detection feed
# has wedged (a rejected ingest batch, a dead indexer) still heartbeats, so the
# overview shows it online and healthy while nothing new reaches the map — the
# failure mode that hid a five-camera station's outage for two weeks. Surfacing
# the newest indexed night per station makes it visible at a glance.
#
# One indexed MAX(date) GROUP BY cam per TTL for the whole fleet; /api/overview
# is itself only cached for 30 s and is hit by every connected dashboard.
_DET_FRESHNESS_TTL_S = 120.0
# Nights are only indexed after the following dawn, so "yesterday" is the
# freshest a healthy station can ever look. Flag from three days back.
_DET_STALE_DAYS = 2
# A station reads as clouded (not faulty) when this share of the latest night's
# FF frames were below the cloud star threshold. Only consulted on a station
# that is ALREADY stale/zero-detection, so it never overrides a healthy read.
_OVERCAST_CLOUD_FRACTION = 0.6
_det_freshness: dict[str, Any] = {"ts": -_DET_FRESHNESS_TTL_S, "by_cam": {}}
_det_freshness_lock = threading.Lock()
_sky_freshness: dict[str, Any] = {"ts": -_DET_FRESHNESS_TTL_S, "by_cam": {}}
_sky_freshness_lock = threading.Lock()


def _detection_freshness_by_cam() -> dict[str, str]:
    """Newest indexed night per camera code, refreshed at most once per TTL."""
    now = time.monotonic()
    with _det_freshness_lock:
        if now - _det_freshness["ts"] < _DET_FRESHNESS_TTL_S:
            return _det_freshness["by_cam"]
    try:
        by_cam = {k.upper(): v for k, v in detection_db.latest_date_by_cam().items()}
    except Exception:
        logger.warning("overview: detection-index freshness unavailable", exc_info=True)
        by_cam = {}
    with _det_freshness_lock:
        _det_freshness["ts"] = now
        _det_freshness["by_cam"] = by_cam
    return by_cam


def _sky_by_cam() -> dict[str, dict[str, Any]]:
    """Latest night's sky-quality metrics per camera code, TTL-cached.

    The per-night sibling of :func:`_detection_freshness_by_cam`, from the same
    detection index. Empty when the sky feed is absent (older DBs / stations
    without the indexer change) so ``weather`` degrades to None."""
    now = time.monotonic()
    with _sky_freshness_lock:
        if now - _sky_freshness["ts"] < _DET_FRESHNESS_TTL_S:
            return _sky_freshness["by_cam"]
    try:
        by_cam = {k.upper(): v for k, v in detection_db.latest_sky_by_cam().items()}
    except Exception:
        logger.warning("overview: night-sky freshness unavailable", exc_info=True)
        by_cam = {}
    with _sky_freshness_lock:
        _sky_freshness["ts"] = now
        _sky_freshness["by_cam"] = by_cam
    return by_cam


def _station_cloud_fraction(
    cameras, sky_by_cam: dict[str, dict[str, Any]]
) -> float | None:
    """Highest latest-night cloud_fraction across a station's cameras, or None.

    Max (not mean) so a single clear camera can't mask an otherwise clouded
    station — the signal only needs to explain why detections are absent."""
    fractions = [
        sky_by_cam[c.code.upper()].get("cloud_fraction")
        for c in cameras
        if c.code.upper() in sky_by_cam
    ]
    fractions = [f for f in fractions if f is not None]
    return max(fractions) if fractions else None


def _camera_sky(sky_by_cam: dict[str, dict[str, Any]], code: str) -> dict[str, Any] | None:
    """One camera's latest-night sky row (night, cloud_fraction, median_stars), or None."""
    row = sky_by_cam.get(code.upper())
    if not row or row.get("cloud_fraction") is None:
        return None
    return {"night": row.get("night"), "cloud_fraction": row.get("cloud_fraction"),
            "median_stars": row.get("median_stars")}


def _weather_hint(stale: bool, cloud_fraction: float | None) -> str | None:
    """'overcast' when a stale/zero-detection station's sky was clouded, else None.

    Deliberately conservative: reported only when the station is already stale,
    so a healthy night is never labelled weather. A clear night that simply had
    no meteors stays 'stale' (a real, if benign, thing to notice)."""
    if stale and cloud_fraction is not None and cloud_fraction > _OVERCAST_CLOUD_FRACTION:
        return "overcast"
    return None


def _detections_age_days(last_date: str | None) -> int | None:
    """Whole days between ``last_date`` (YYYYMMDD) and today, UTC."""
    if not last_date:
        return None
    try:
        indexed = datetime.strptime(last_date, "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc).date() - indexed.date()).days


def archive_night_stack_entry(archive_idx, host_key: str, cam_code: str) -> dict | None:
    """Newest archived night composite for a camera with no RMS output.

    Colour/spectro-only cameras (``rms: false``, e.g. gmnro01's GH662
    grating pair) never upload a captured stack or any other RMS plot, so
    both resolution tiers above come back empty and the overview grid
    rendered a permanent "no data" tile. Their dawn pipeline does build a
    per-night composite (``<cam>_<date>_night_stack.webp``, archived under
    ``<cam>/<date>/timelapse/``), which the plot-image route serves as a
    GMN-style black-and-white stack.

    Runs only inside the background fan-out (never on a request thread)
    and only for cameras whose RMS tiers both came back empty, so the
    worst case is a handful of ``night_files`` lookups — recent nights are
    in the hot index, and a cold lookup goes through the shed-protected
    SSHFS timeout wrapper.
    """
    if archive_idx is None:
        return None
    try:
        for date in archive_idx.nights(cam_code)[:7]:
            ni = archive_idx.night_files(cam_code, date)
            if ni is None:
                continue
            fname = f"{cam_code}_{date}_night_stack.webp"
            if fname in ni.timelapse_files:
                return {
                    "host": host_key, "cam": cam_code, "date": date,
                    "filename": fname, "label": "Night stack",
                }
    except Exception:
        logger.debug(
            "archive night-stack fallback failed for %s/%s",
            host_key, cam_code, exc_info=True,
        )
    return None


def resolve_archive_cam_stack(archive_idx, host_key: str, cam_code: str) -> dict | None:
    """Newest ARCHIVED plot for a camera -- no station contact at all.

    The overview's only resolver. The station poll it replaced could only see
    what the station still held, so a station that was offline (or whose
    recent nights failed to process) contributed nothing to the fan-out and
    the grid rendered a permanent "no data" tile for it. The last-known-good cache that used to cover
    that is per-worker and in-memory, so it was empty for every station
    that had been offline since the last dashboard restart -- and a
    station down for a week never refilled it. The result contradicted the
    invariant the whole plot surface is built on: a plot that was uploaded
    once is never missing, only stale.

    Resolving from the archive instead makes it hold by construction. The
    entry has the same shape as the live one, so the card renders normally
    and the frontend colours the date by age (red once it is >2 days old).

    A captured stack wins wherever one exists. The other candidates cover
    a camera whose station only ever uploads part of the plot set -- the
    Câmpenița Pis upload ff_intervals + calibration_variation and no
    stack at all, and a chart labelled with its real night still beats an
    empty tile.

    Module-level (not a closure in ``register_overview_routes``) so tests can
    stand in for it when they need to control the fan-out's timing.
    """
    if archive_idx is None:
        return None
    from routes.archive import (
        resolve_latest_rms_plots,
        resolve_rms_plot_status,
    )

    def _entry(date: str, filename: str, label: str) -> dict:
        return {
            "host": host_key, "cam": cam_code, "date": date,
            "filename": filename, "label": label,
        }

    try:
        res = resolve_rms_plot_status(
            archive_idx, cam_code, "_captured_stack.jpg",
        )
        if res.get("status") == "ok" and res.get("filename"):
            return _entry(res["date"], res["filename"], "Captured stack")
        latest = resolve_latest_rms_plots(archive_idx, cam_code)
        plots = latest.get("plots") or []
        if not plots:
            return archive_night_stack_entry(archive_idx, host_key, cam_code)
        preferred = next(
            (p for p in plots
             if any(s in p["filename"] for s in _ARCHIVE_CARD_PREFERRED)),
            plots[0],
        )
        return _entry(
            latest["date"], preferred["filename"],
            preferred.get("label", "Plot"),
        )
    except Exception:
        logger.debug(
            "archive stack fallback failed for %s/%s",
            host_key, cam_code, exc_info=True,
        )
        return None


def register_overview_routes(
    app: Flask,
    config,
    cache,
    tunnels,
    *,
    # Overview stacks cache
    overview_stacks_cache: dict[tuple[str, str], dict[str, Any]],
    overview_stacks_cache_lock: threading.Lock,
    overview_stacks_full_ts: list[float],  # mutable container: [monotonic_ts]
    overview_stacks_ttl: float,
    # Status / vitals last-seen
    status_last_seen: dict[str, tuple[float, dict[str, Any]]],
    vitals_last_seen: dict[str, tuple[float, dict[str, Any]]],
    status_last_seen_lock: threading.Lock,
    vitals_last_seen_lock: threading.Lock,
    last_seen_max_age: float,
    # Kick / record helpers
    kick_status_refresh,
    kick_vitals_refresh,
    record_status_last_seen,
    record_vitals_last_seen,
    archive_idx=None,
) -> None:
    limiter = app._limiter  # type: ignore[attr-defined]

    _require_station_impl = lookup_station

    # ── Config-all endpoint (parallel fetch from all stations) ───────────

    @app.route("/api/config/all")
    @require_admin
    def api_config_all():
        def fetch_one(host_key: str):
            try:
                cfg = station_get_raw(config, tunnels, host_key, "/api/settings", timeout=10)
                return host_key, {"ok": True, "config": cfg}
            except Exception as exc:
                return host_key, {"ok": False, "error": str(exc)}

        result: dict[str, Any] = {}
        futures = {
            _STATION_POLL_EXECUTOR.submit(fetch_one, hk): hk
            for hk in config.stations
        }
        for fut in as_completed(futures):
            hk, data = fut.result()
            result[hk] = data
        return jsonify(result)

    # ── Overview captured stacks (latest night per camera) ──────────────

    # Cleared until the first full fan-out publishes. A warming snapshot is
    # empty (fresh worker) or covers only what the leader's prefetch walk has
    # reached, and must NOT carry max-age: a browser that caches an empty
    # snapshot for 60 s serves it back to every retry in the frontend ladder,
    # which turns a 4 s cold window into a minute of "no data" tiles.
    overview_stacks_warm = threading.Event()

    def _refresh_overview_stacks() -> None:
        """Background fan-out updater for overview_stacks_cache. NEVER blocks
        an HTTP request; called from a daemon thread by api_overview_stacks
        and at dashboard startup.

        Every camera resolves from the archive; no station is contacted. The
        fan-out used to ask each online station for its nights and plot lists
        first and fall back to the archive, which cost two station round-trips
        per camera per tick and held a poll thread for the full timeout on any
        station that was slow rather than known-offline. Everything the card
        shows is uploaded by archive_upload anyway, so the station leg only
        ever bought the few minutes between a night's processing and its
        upload.
        """
        tasks = [
            (host_key, cam.code,
             _STATION_POLL_EXECUTOR.submit(
                 resolve_archive_cam_stack, archive_idx, host_key, cam.code))
            for host_key, station in config.stations.items()
            for cam in station.cameras
        ]
        # Collect every result first, then publish in ONE locked update.
        # Publishing per-future (the previous behaviour) made the cache
        # readable mid-fan-out, so api_overview_stacks could snapshot a
        # *prefix* of the fleet in config order -- the frontend has no way to
        # tell that from "these cameras have no stack" and renders the rest as
        # "no data" tiles. Atomic publish means a reader sees either the
        # previous generation or the complete new one, never a prefix.
        fresh: dict[tuple[str, str], dict[str, Any]] = {}
        for host_key, cam_code, future in tasks:
            try:
                r = future.result()
            except Exception:
                r = None
            if r:
                fresh[(host_key, cam_code)] = r
            # None -> keep last-known-good entry untouched (merge, not replace)
        with overview_stacks_cache_lock:
            for key, entry in fresh.items():
                prev = overview_stacks_cache.get(key)
                # A card never moves backwards in time. A listing that briefly
                # misses a night (a storage-box scan that timed out and served
                # an older answer) must not make the grid regress, and entries
                # cached before this became archive-only can be a night ahead
                # of the upload.
                if prev and str(prev.get("date", "")) > str(entry.get("date", "")):
                    continue
                overview_stacks_cache[key] = entry
        # One full fan-out has now published, so the cache is authoritative
        # rather than a warming artefact. Latches on -- later fan-outs only
        # refine entries.
        overview_stacks_warm.set()

    # Stash on app so the prefetch loop in rovimen_dashboard.py can call it.
    app._refresh_overview_stacks = _refresh_overview_stacks  # type: ignore[attr-defined]

    @app.route("/api/overview/stacks")
    @public_route(page="overview")
    def api_overview_stacks():
        """Always returns cached entries instantly. If the cache is older
        than overview_stacks_ttl, spawns a background refresh thread (does
        NOT block the user's request). Subsequent calls within ~5-30 s
        will see the updated cache.

        Trade-off: first page-load after a fresh dashboard restart returns
        an empty list (cache empty), and the user has to retry to get
        populated data. The frontend handles this with a JS-side retry
        on empty response. The startup hook also kicks off one prefetch
        iteration synchronously in a thread so the cold window is short.

        Public under the "overview" page toggle (the fleet map's latest-night
        thumbnail strip needs it). Anonymous callers only ever get stacks from
        ``public: true`` stations; the shared cache stays full-fleet, so we
        drop non-public host_keys per request. Each cached entry carries only
        host_key/cam/date/filename/label — no ip/cam_ip/host-path — so the
        redaction is entirely a public-station filter.
        """
        now_mono = time.monotonic()
        if now_mono - overview_stacks_full_ts[0] >= overview_stacks_ttl:
            overview_stacks_full_ts[0] = now_mono
            threading.Thread(target=_refresh_overview_stacks, daemon=True).start()
        # Snapshot under the lock -- the refresh thread mutates the dict
        # concurrently, and an un-guarded list() can hit a "dictionary
        # changed size during iteration" RuntimeError mid-poll.
        with overview_stacks_cache_lock:
            snapshot = list(overview_stacks_cache.values())
        if is_anonymous():
            pub_hosts = public_host_keys(config)
            snapshot = [s for s in snapshot if s.get("host") in pub_hosts]
        if not overview_stacks_warm.is_set():
            # Still warming: hand the snapshot over uncacheable so the
            # frontend retry ladder actually reaches the server, and flag it
            # so the frontend keeps retrying even on a non-empty prefix.
            resp = jsonify(snapshot)
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["X-Stacks-Warming"] = "1"
            return resp
        # Heavy payload (~50-200 KB) that rarely changes minute-to-minute --
        # ETag lets the browser short-circuit to 304 on repeat polls.
        return _json_cached(snapshot, max_age=60)

    # ── GMN multi-station trajectories -- served from background cache ────

    @app.route("/api/gmn/multistation/<date>")
    def api_gmn_multistation(date: str):
        """Return GMN multi-station events for ``date``. Tries the in-memory
        cache first; on a miss, fetches on-demand via gmn_data so arbitrary
        historical dates work without waiting for the background poller."""
        payload = _gmn_get_cached(date)
        if not payload.get("events") and not payload.get("error"):
            try:
                payload = _gmn_fetch_multistation(date)
                with _gmn_cache_lock:
                    _gmn_cache[date] = payload
            except Exception as exc:
                # Swallowing this made a broken fetch indistinguishable from
                # a night with no orbits — the frontend painted "no orbits"
                # over an outage. Surface it on the payload instead so the
                # error branch renders, and leave the cache untouched.
                logger.exception("GMN: on-demand fetch failed for %s", date)
                payload = {**payload, "error": str(exc)}
        return _json_cached(payload, max_age=300)

    @app.route("/api/gmn/latest_date")
    def api_gmn_latest_date():
        """Newest UTC night GMN has published trajectories for.

        GMN solves a night 4-15 days after it happened, so the overlay
        cannot sensibly default to "yesterday". Reads the monthly cache
        already on disk — no upstream call — so it is cheap enough for the
        frontend to ask on every layer open.
        """
        try:
            latest = gmn_data.latest_available_date()
        except Exception:
            logger.exception("GMN: latest_available_date failed")
            latest = None
        return _json_cached({"latest_available_date": latest}, max_age=300)

    @app.route("/api/gmn/monthly_count")
    def api_gmn_monthly_count():
        """Sum has_ro_count across cached entries for a given month.
        Accepts ?month=YYYY-MM; defaults to current UTC month."""
        month_param = request.args.get("month", "")
        target: datetime | None = None
        if month_param:
            try:
                target = datetime.strptime(month_param + "-15", "%Y-%m-%d").replace(
                    tzinfo=timezone.utc
                )
            except ValueError:
                pass
        return _json_cached(_gmn_monthly_count(target), max_age=300)

    @app.route("/api/_debug/gmn/inject", methods=["POST"])
    def api_debug_gmn_inject():
        """Debug-only: inject a mock GMN event into the cache so the
        frontend can be exercised while the real upstream is rate-limited
        or otherwise unavailable. Body: { "date": "YYYY-MM-DD", "event": {...} }.
        Requires ROVIMEN_DEBUG_TOKEN env var; always 403 if unset."""
        expected = os.environ.get("ROVIMEN_DEBUG_TOKEN", "")
        token = request.headers.get("X-Debug-Token", "")
        if not expected or not secrets.compare_digest(expected, token):
            return jsonify({"error": "forbidden"}), 403
        body = request.get_json(force=True, silent=True) or {}
        date = body.get("date")
        event = body.get("event")
        if not date or not isinstance(event, dict):
            return jsonify({"error": "need {date, event}"}), 400
        with _gmn_cache_lock:
            entry = _gmn_cache.get(date) or {
                "date": date, "total_upstream": 0, "events": [],
                "has_ro_count": 0, "de_only_count": 0,
                "last_fetched_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            # If the cache has an error stub, clear it so the frontend doesn't
            # paint "GMN data unavailable" over our mock data.
            entry.pop("error", None)
            entry["events"].append(event)
            if event.get("has_ro"):  entry["has_ro_count"]  += 1
            if event.get("de_only"): entry["de_only_count"] += 1
            entry["total_upstream"] = max(entry["total_upstream"], len(entry["events"]))
            # Survive the next poll cycle -- without this flag the backfill
            # would refetch this date and trample the mock with an error stub.
            entry["pinned"] = True
            _gmn_cache[date] = entry
        return jsonify({"ok": True, "events_for_date": len(entry["events"]), "pinned": True})

    # ── Overview endpoint (all stations health at a glance) ─────────────

    @app.route("/api/overview")
    @public_route(page="overview")
    def api_overview():
        is_admin = _session_role() == "admin"
        # Anonymous public visitors only ever see ``public: true`` stations —
        # commissioning / opted-out sites never appear in the public overview.
        # Logged-in accounts keep the full fleet (fleet-wide read rule).
        anon = is_anonymous()
        det_by_cam = _detection_freshness_by_cam()
        sky_by_cam = _sky_by_cam()
        now_utc = datetime.now(timezone.utc)
        result = {}
        for key, station in config.stations.items():
            if anon and not station.public:
                continue
            status = cache.get_status(key) or {}
            vitals = cache.get_vitals(key) or {}
            # Newest night indexed for ANY of the station's cameras. A station
            # that is online but whose newest night is days old has a broken
            # detection feed, however healthy its heartbeat looks.
            last_det = max(
                (d for d in (det_by_cam.get(c.code.upper()) for c in station.cameras) if d),
                default=None,
            )
            det_age = _detections_age_days(last_det)
            online = status.get("online", False)
            stale = bool(online and det_age is not None and det_age > _DET_STALE_DAYS)
            # Latest-night cloud fraction (same detection feed). Rides next to
            # detections_stale so the frontend can show a clouded station as
            # weather rather than a fault.
            cloud_fraction = _station_cloud_fraction(station.cameras, sky_by_cam)
            weather = _weather_hint(stale, cloud_fraction)
            # Per-camera colour-segment cadence. Absent on station bundles that
            # predate the field, which reads as "nothing reported" — never as a
            # fault — all the way through to the frontend.
            seg_by_cam = status.get("segment_health") or {}
            result[key] = {
                "label": station.label,
                **({"ip": station.ip} if is_admin else {}),
                "cameras": [
                    {"code": c.code, **({"cam_ip": c.cam_ip} if is_admin else {}),
                     "rotate": c.rotate, "az": c.az, "alt": c.alt,
                     "segment_health": camera_segment_health(seg_by_cam, c.code),
                     # Recording / degraded / stalled / idle, from the FF
                     # files RMS wrote (capture_state.py).
                     "capture": camera_capture_state(
                         c.code, status, online=online, now=now_utc,
                         lat=station.lat, lon=station.lon),
                     # Latest night's sky from the detection index: drives
                     # the per-camera cloud marker.
                     "sky": _camera_sky(sky_by_cam, c.code)}
                    for c in station.cameras
                ],
                # Station-level rollup so a fleet card can badge without
                # walking the camera list: {camera code: 'storm'|'stalls'|'both'}
                "segment_alarm": station_segment_alarms(seg_by_cam),
                "online": online,
                "detections_last_date": last_det,
                "detections_age_days": det_age,
                "detections_stale": stale,
                "cloud_fraction": cloud_fraction,
                "weather": weather,
                "services": status.get("services", {}),
                "disk": status.get("disk"),
                "cpu_pct": vitals.get("cpu_pct"),
                "ram_pct": vitals.get("ram_pct"),
                "temp_c": vitals.get("temp_c"),
                "last_updated": status.get("last_updated"),
                "lat": station.lat,
                "lon": station.lon,
                "show_on_map": station.show_on_map,
                "status": station.status,
            }
        return _json_cached(result, max_age=30)

    @app.route("/api/overview/station_orbits")
    @public_route(page="overview")
    def api_overview_station_orbits():
        """GMN orbits per station, last 30 days and all time, for the map
        hover. ``ready: false`` until the first background pass finishes;
        ``history_complete: false`` while older years are still loading."""
        anon = is_anonymous()
        cam_to_station = {
            c.code.upper(): key
            for key, st in config.stations.items()
            for c in st.cameras if c.code
        }
        res = station_orbits.station_orbit_counts(cam_to_station)
        if res is None:
            return _json_cached({"ready": False, "stations": {}}, max_age=30)
        visible = {k for k, st in config.stations.items() if not anon or st.public}
        return _json_cached({
            "ready": True,
            "history_complete": res["history_complete"],
            "computed_at": res["computed_at"],
            "stations": {k: v for k, v in res["stations"].items() if k in visible},
        }, max_age=300)

    # ── Config endpoint (single source of truth for JS) ───────────────────

    @app.route("/api/stations")
    @public_route
    def api_stations():
        # Plain @public_route (no page key): this is the static station
        # metadata registry — label, camera codes/pointing, coords, public
        # tabs — that EVERY public page (events, showers, station, highlights)
        # needs to label + place detections, independent of whether the
        # fleet-map "overview" toggle is on. Decoupling it from page="overview"
        # lets an operator expose events/showers/highlights WITHOUT enabling
        # the live fleet map. It still applies the same anon safeguards as the
        # rest of the surface: the sensitive cam_ip stays admin-only, and
        # anonymous visitors see ONLY ``public: true`` stations — the same
        # opt-in set the versioned /api/public/v1 surface exposes.
        is_admin = _session_role() == "admin"
        anon = is_anonymous()
        result = {}
        for key, station in config.stations.items():
            if anon and not station.public:
                continue
            result[key] = {
                "label": station.label,
                "cameras": [
                    {"code": c.code, **({"cam_ip": c.cam_ip} if is_admin else {}),
                     "rotate": c.rotate, "az": c.az, "alt": c.alt,
                     # Emitted only when set (like the config serializer): the
                     # station page shows its Spectro tab from this flag, so it
                     # must not need a probe request to know the tab exists.
                     **({"spectro": True} if getattr(c, "spectro", False) else {})}
                    for c in station.cameras
                ],
                "lat": station.lat,
                "lon": station.lon,
                "show_on_map": station.show_on_map,
                "public_tabs": station.public_tabs,
            }
        # The non-admin response is identical for everyone, so it's cacheable;
        # the admin variant carries cam_ip, so don't let it be cached/shared.
        if is_admin:
            return jsonify(result)
        return _json_cached(result, max_age=300)

    # ── Cached proxy endpoints ────────────────────────────────────────────

    @app.route("/api/status/<host_key>")
    @require_station
    def api_status(host_key: str):
        _require_station_impl(config, host_key)
        data = cache.get_status(host_key)
        if data is not None:
            record_status_last_seen(host_key, data)
            return jsonify(data)
        # Cold miss: never hold the request worker for a 15 s SSH-tunneled
        # fetch. Kick a coalesced background refresh and reply with the
        # most recent known-good snapshot (flagged stale) or a cold-start
        # stub the frontend already handles.
        kick_status_refresh(host_key)
        with status_last_seen_lock:
            stale = status_last_seen.get(host_key)
        if stale is not None and (time.time() - stale[0]) < last_seen_max_age:
            payload = dict(stale[1])
            payload["stale"] = True
            return jsonify(payload)
        return jsonify({"online": False, "stale": True, "reason": "cold-start"})

    @app.route("/api/vitals/<host_key>")
    @require_station
    def api_vitals(host_key: str):
        _require_station_impl(config, host_key)
        data = cache.get_vitals(host_key)
        if data is not None:
            record_vitals_last_seen(host_key, data)
            return jsonify(data)
        kick_vitals_refresh(host_key)
        with vitals_last_seen_lock:
            stale = vitals_last_seen.get(host_key)
        if stale is not None and (time.time() - stale[0]) < last_seen_max_age:
            payload = dict(stale[1])
            payload["stale"] = True
            return jsonify(payload)
        return jsonify({"online": False, "stale": True, "reason": "cold-start"})

    # ── Aggregator endpoints: collapse N per-station calls into one ───────
    # Browser-side fan-out across stations is a hot path for users on
    # high-latency links (e.g. Romania -> Falkenstein, ~70 ms RTT). One
    # aggregator call saves N x RTT plus N x TCP/TLS connection overhead.
    # Backed by the same StationCache the per-host routes use -- no extra load.

    @app.route("/api/status/all")
    @public_route(page="overview")
    def api_status_all():
        # Anonymous public visitors on the fleet map only ever see the online
        # flag (plus label/coords, which are already public metadata) for
        # ``public: true`` stations — never the raw station status dict, which
        # can carry host-internal service names, disk mounts, and per-poll
        # timing that must not leak. Logged-in accounts keep the full-fleet,
        # full-fidelity status they rely on for ops.
        if is_anonymous():
            result: dict[str, Any] = {}
            for key, station in config.stations.items():
                if not station.public:
                    continue
                status = cache.get_status(key) or {}
                result[key] = {
                    "online": bool(status.get("online", False)),
                    "label": station.label,
                    "lat": station.lat,
                    "lon": station.lon,
                    "show_on_map": station.show_on_map,
                }
            return _json_cached(result, max_age=30)
        keys = [k for k in config.stations if _has_station_access(k)]
        result = {key: (cache.get_status(key) or {}) for key in keys}
        return _json_cached(result, max_age=30)

    @app.route("/api/vitals/all")
    @require_auth
    def api_vitals_all():
        keys = [k for k in config.stations if _has_station_access(k)]
        result = {key: (cache.get_vitals(key) or {}) for key in keys}
        return _json_cached(result, max_age=15)

    # ── Server-Sent Events: push station status diffs instead of polling ───
    # The aggregator above gets re-fetched by every connected dashboard every
    # 60 s. Station status is mostly stationary (services up/down, disk pct,
    # online flag), so 95%+ of those round-trips return an identical body --
    # they're polling waste. This endpoint flips the model: the client opens
    # one long-lived stream, gets a `snapshot` event on connect, then receives
    # a `status` event only when a station's state actually changes.
    #
    # Trade-offs:
    #   - Each SSE connection holds a WSGI worker for up to _SSE_DEADLINE_S
    #     seconds before auto-closing (issue #329). The browser reconnects
    #     transparently via EventSource's built-in retry logic.
    #     Next step if needed: ``gunicorn --worker-class=gevent`` turns each
    #     connection into a greenlet (~4 KB) instead of a thread (~8 MB stack).
    #     If we ever move to gunicorn with multiple sync workers, a single
    #     in-process pub/sub set won't fan out across workers -- we'd need
    #     an external bus (redis pub/sub, etc.).
    #   - If a client disconnects mid-yield, Flask raises GeneratorExit on the
    #     next yield, not immediately. The 25-s heartbeat ensures we hit a
    #     yield boundary within at most 25 s of disconnect, so the listener is
    #     unregistered promptly. The ``finally`` block is the safety net.
    #   - Slow consumers are dropped at the queue level (set_status never
    #     blocks). If a listener queue stays full, the affected client misses
    #     events between drops -- they'll resync via the client-side
    #     fetchAllStatus() fallback or the next legitimate change.
    @app.route("/api/events/status")
    def api_events_status_stream():
        # Cap-check up front: we still need the cap to gate the 503 response,
        # but we DO NOT hold the registration across the Response construction.
        # If we did and Flask never started iterating gen() (e.g. client
        # disconnected before the first byte, or a middleware short-circuited
        # the response), the finally inside gen() would never run and the
        # listener would leak. Instead we register at the very top of gen() --
        # by the time we're inside the generator's try, iteration has begun
        # and the finally clause is guaranteed to execute on close.
        if not cache.can_register_status_listener():
            # Listener cap hit -- tell the client to back off. EventSource's
            # default reconnect (3 s) will keep retrying; once an existing
            # listener disconnects, the next attempt gets through.
            return Response(
                "listener cap reached",
                status=503,
                mimetype="text/plain",
                headers={"Retry-After": "10"},
            )

        def gen():
            listener = cache.register_status_listener()
            if listener is None:
                # Racy cap hit between can_register_status_listener() and
                # the actual register call. End the stream cleanly so the
                # client retries.
                return
            try:
                # Initial snapshot so the client doesn't need a separate
                # /api/status/all fetch on top of subscribing. Encoded once
                # and shared across listeners that arrive at the same cache
                # version (reconnect-storm protection).
                yield cache.snapshot_bytes(tuple(config.stations))
                deadline = time.monotonic() + _SSE_DEADLINE_S
                while time.monotonic() < deadline:
                    try:
                        # 25 s < typical proxy idle timeout (nginx 60 s default,
                        # cloudflare 100 s). A `: ping` comment keeps the TCP
                        # connection warm without producing a parseable event.
                        yield listener.get(timeout=25)
                    except queue.Empty:
                        yield b": ping\n\n"
                # Deadline reached -- close with a retry hint so the browser
                # reconnects in 3 s (short enough to be imperceptible).
                yield b"retry: 3000\nevent: reconnect\ndata: deadline\n\n"
            except GeneratorExit:
                # Client disconnected -- fall through to finally.
                pass
            finally:
                cache.unregister_status_listener(listener)

        return Response(
            gen(),
            mimetype="text/event-stream",
            headers={
                # No caching for an infinite stream; intermediate proxies
                # MUST NOT buffer (nginx-specific header but harmless
                # elsewhere). Connection: keep-alive is implicit for HTTP/1.1
                # but spelled out for clarity.
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
