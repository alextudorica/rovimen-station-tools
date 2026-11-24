"""Network map, network stats, version, and admin network config routes."""

import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request

from auth import require_admin, is_anonymous, station_is_public_for_request
from security import public_route
import gmn_data

logger = logging.getLogger(__name__)


def _bundle_version_file() -> Path:
    """Path to the ``.version`` of the bundle channel THIS tier publishes.

    Both channels are staged under the PROD tree — CI pushes the dev channel to
    ``/opt/rovimen/station-bundle-dev``, not into ``/opt/rovimen-dev`` — so the
    base directory is shared and only the channel sub-directory differs per
    tier. ``ROVIMEN_BUNDLE_DIR`` overrides the base (same convention as
    ``stack_overlay`` and ``station_watchdog``); the channel comes from
    ``ROVIMEN_BUNDLE_CHANNEL`` when set, otherwise it is inferred from the
    install root so the dev tier is correct without an env-file edit.
    """
    base = Path(os.environ.get("ROVIMEN_BUNDLE_DIR", "/opt/rovimen"))
    channel = os.environ.get("ROVIMEN_BUNDLE_CHANNEL", "").strip().lower()
    if not channel:
        # routes/network.py -> <install root>. /opt/rovimen-dev => dev tier.
        install_root = Path(__file__).resolve().parent.parent
        channel = "dev" if install_root.name.endswith("-dev") else "main"
    sub = "station-bundle-dev" if channel == "dev" else "station-bundle"
    return base / sub / ".version"


def bundle_version() -> str:
    """Version string of the station bundle this tier publishes, or ``unknown``."""
    try:
        return _bundle_version_file().read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def register_network_routes(
    app: Flask,
    config,
    *,
    save_config,
) -> None:
    """Attach network-related routes to *app*.

    Parameters
    ----------
    config:
        The live ``DashboardConfig`` instance (shared with the rest of the
        dashboard — mutations here are visible everywhere).
    save_config:
        Closure that atomically persists ``config`` to
        ``dashboard_config.yaml`` (defined in ``create_app()``).
    """
    import network_stats as _net_stats_mod

    # The coverage precompute and the GMN orbit-count warmup start in
    # rovimen_dashboard._start_background_workers (one worker, off the boot
    # path); these routes compute on demand when that has not run here.

    # ── Network stats ────────────────────────────────────────────────────

    @app.route("/api/network-stats")
    @public_route
    def api_network_stats():
        # Plain @public_route (no page key): powers the "Network Statistics"
        # block of the About modal, which every public page can open. The
        # payload is aggregate network science — covered area, double-station
        # coverage by altitude, atmospheric volume, expected flux, and total
        # station/camera/platepar counts. No IPs, cam_ip, hostnames, host
        # paths, or per-user data. For anonymous visitors the per-station
        # breakdown is filtered to ``public: true`` stations so a
        # commissioning/opted-out site's existence isn't disclosed (same
        # per-station rule as /api/stations, PR #628). Logged-in accounts get
        # the full fleet breakdown.
        import platepar_store
        platepars = platepar_store.get_all()
        cfg_raw = config.model_dump()
        result = _net_stats_mod.get_network_stats(platepars, cfg_raw)
        try:
            result["orbit_counts"] = gmn_data.orbit_counts()
        except Exception:
            logger.exception("orbit_counts failed")
        if is_anonymous():
            per_station = result.get("per_station")
            if isinstance(per_station, dict):
                result = dict(result)  # don't mutate the cached compute result
                result["per_station"] = {
                    hk: row
                    for hk, row in per_station.items()
                    if station_is_public_for_request(config, hk)
                }
        return jsonify(result)

    # ── Version endpoint ─────────────────────────────────────────────────

    @app.route("/api/version")
    def api_version():
        return jsonify({"version": bundle_version()})

    # ── Global config PATCH ──────────────────────────────────────────────

    @app.route("/api/admin/network-config/global", methods=["PATCH"])
    @require_admin
    def api_admin_network_config_global():
        body = request.get_json(force=True) or {}
        if "correlation_window_s" in body:
            v = body["correlation_window_s"]
            if isinstance(v, int) and 1 <= v <= 3600:
                config.correlation_window_s = v
        save_config()
        return jsonify({"ok": True, "correlation_window_s": config.correlation_window_s})

