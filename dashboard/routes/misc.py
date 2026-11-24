"""Simple page renders and utility routes."""

import json
import logging
import os
from pathlib import Path
from flask import Flask, Response, abort, jsonify, redirect, render_template, request, session, url_for

from auth import require_admin, station_is_public_for_request
from mdc_poller import get_shower_data
from route_helpers import lookup_station
from security import public_route

logger = logging.getLogger(__name__)


_SHOWER_COUNTS_PATH = Path(
    os.environ.get("ROVIMEN_SHOWER_COUNTS_PATH", "/opt/rovimen/shower_year_counts.json")
)


def register_misc_routes(app: Flask, config, build_version: str) -> None:
    _require_station_impl = lookup_station

    # ── Service Worker ────────────────────────────────────────────────────

    # The SW file is physically stored under dashboard/static/sw.js, but we
    # also expose it at the origin root.  A SW served from /static/sw.js
    # would only control the /static/ scope, which is useless to us -- we
    # need full origin scope so navigation requests and other static
    # requests both flow through it.
    @app.route("/sw.js")
    def service_worker():
        """Serve the Service Worker from the origin root so its scope is '/'.

        The ``__BUILD_VERSION__`` token in sw.js is replaced with
        BUILD_VERSION, so every deploy serves different bytes: browsers
        install the new worker, whose activate handler deletes the old
        caches. ``no-cache`` makes every update check reach the server even
        in a browser that would otherwise honour the HTTP cache for it.
        """
        sw_path = Path(app.static_folder) / "sw.js"
        body = sw_path.read_text().replace("__BUILD_VERSION__", build_version)
        return Response(body, mimetype="application/javascript",
                        headers={"Cache-Control": "no-cache",
                                 "Service-Worker-Allowed": "/"})

    # ── Pages ─────────────────────────────────────────────────────────────

    @app.route("/")
    @public_route(page="overview")
    def index():
        # The landing page for everyone: the intro (hero), the network numbers,
        # the Hall of Fame and the latest plots per station. The map, the
        # station status and the live snapshots are on /network;
        # overview.html renders only the sections of ``ov_page``.
        # Its data (``/api/overview``, ``/api/overview/stacks``, all
        # page="overview") applies the per-station public filter and the
        # admin-field redaction for anon, so no private station or IP reaches
        # an anonymous caller. page="overview": an operator who turns the
        # overview off gates the landing for anon too (fail-closed).
        return render_template("overview.html", ov_page="home")

    @app.route("/map")
    @app.route("/stations")
    @public_route(page="overview")
    def fleet_map():
        # The map lives on the Network overview now; old links and bookmarks
        # land there with their query string (the browser keeps the #hash).
        qs = request.query_string.decode()
        return redirect("/network" + (f"?{qs}" if qs else ""), code=302)

    @app.route("/network")
    @public_route(page="overview")
    def network_page():
        # Network overview (header tab): the coverage numbers, the fleet map,
        # every station's status, and the live snapshot of every capturing
        # camera, loaded only on request. Same data and public rule as the
        # landing, which used to carry the status table.
        return render_template("overview.html", ov_page="network")

    @app.route("/station/<host_key>")
    @app.route("/station/<host_key>/<tab>")
    @public_route(page="station")
    def station_detail(host_key: str, tab: str = ""):
        _require_station_impl(config, host_key)
        # A commissioning / opted-out (public: false) station must not be
        # viewable by anonymous visitors even by direct deep-link; 404 keeps
        # its very existence undisclosed. Logged-in users keep full access.
        if not station_is_public_for_request(config, host_key):
            abort(404)
        return render_template("dashboard.html", default_station=host_key)

    @app.route("/config")
    def config_page():
        return render_template("config.html")

    @app.route("/admin")
    @require_admin
    def admin_page():
        return render_template("admin.html")

    # ── Events page ──────────────────────────────────────────────────────

    @app.route("/events")
    @public_route(page="events")
    def events_page():
        return render_template("events.html")

    # ── Highlights page ──────────────────────────────────────────────────

    @app.route("/highlights")
    @public_route(page="highlights")
    def highlights_page():
        # Anon-reachable only when "highlights" is in ``public_pages``; the
        # curated top-10 data route carries the same page key so the page and
        # its data flip together. The page itself renders no admin fields.
        return render_template("highlights.html", build_version=build_version)

    # ── Live view page ───────────────────────────────────────────────────

    @app.route("/live")
    @public_route(page="live")
    def live_view():
        # The live snapshots are a section of Network overview now; old links land there.
        return redirect(url_for("network_page") + "#live")

    # ── Meteor showers page ──────────────────────────────────────────────

    @app.route("/showers")
    @public_route(page="showers")
    def showers_page():
        return render_template("showers.html")

    # ── Calibration page ─────────────────────────────────────────────────
    # Not public (no @public_route) -- internal ops page, login required by
    # the default deny-by-default gate.

    @app.route("/calibration")
    def calibration_page():
        return render_template("calibration.html")

    # ── Build guide / wiki page ──────────────────────────────────────────

    # Translations are separate templates rather than a gettext catalogue: the
    # page is one long prose document, and a Romanian operator reading a
    # half-translated page is worse served than one reading either language
    # whole. The allow-list keeps ``/wiki/<lang>`` from reaching the template
    # loader with arbitrary input.
    WIKI_TEMPLATES = {"en": "wiki.html", "ro": "wiki_ro.html"}

    @app.route("/wiki")
    @app.route("/build")
    @app.route("/wiki/<lang>")
    @public_route(page="wiki")
    def wiki_page(lang: str = "en"):
        # Static reference content answering the recurring "how do I build a
        # station?" questions. Anon-reachable only when "wiki" is in
        # ``public_pages``; carries no admin fields or station data.
        template = WIKI_TEMPLATES.get(lang)
        if template is None:
            # 404 rather than falling back to English: a wrong language code is
            # a broken link somewhere, and a silent fallback hides it.
            abort(404)
        return render_template(template, build_version=build_version)

    # ── Meteor shower reference data (public — IAU reference, not sensitive) ─

    @app.route("/api/showers")
    @public_route
    def api_showers():
        """Return MDC shower data keyed by IAU code."""
        return jsonify(get_shower_data())

    # ── Measured activity shapes (GMN), for the Showers page ───────────────

    _SHOWER_PROFILES_PATH = Path(__file__).resolve().parent.parent / "shower_data" / "profiles.json"

    @app.route("/api/shower-profiles")
    @public_route
    def api_shower_profiles():
        """Each shower's GMN multi-station meteors per sporadic meteor, per
        half day of solar longitude, scaled to 1 at its maximum. Built offline by
        tools/build_shower_profiles.py and committed; {} when it is missing."""
        try:
            payload = json.loads(_SHOWER_PROFILES_PATH.read_text())
        except (OSError, ValueError):
            logger.warning("shower profiles unavailable at %s", _SHOWER_PROFILES_PATH)
            return jsonify({})
        resp = jsonify(payload)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # ── Per-shower ROVIMEN detection counts for the current year ──────────

    @app.route("/api/shower-year-counts")
    @public_route
    def api_shower_year_counts():
        """Return {IAU_CODE: count} for all ROVIMEN detections in the current year.

        Written by scan_shower_counts.py (daily cron on the VPS).
        Returns {} if the file hasn't been generated yet.
        """
        if not _SHOWER_COUNTS_PATH.exists():
            return jsonify({})
        try:
            payload = json.loads(_SHOWER_COUNTS_PATH.read_text())
            return jsonify(payload.get("counts", {}))
        except Exception:
            logger.exception("failed to read %s", _SHOWER_COUNTS_PATH)
            return jsonify({})
