"""Read side of the station event history (``station_events.py``).

``GET /api/station_events/<host_key>``  one station's events, newest first
``GET /api/station_events``             every station the caller may see

Query parameters: ``limit`` (default 50, at most 1000) and ``since`` (an ISO
instant, or a date meaning its midnight UTC). Visible to admins and to the
host accounts assigned to the station (``auth._has_station_write_access``):
the history quotes the station's own log lines, which name its LAN gateway and
interfaces, so it follows the per-station scope rather than the fleet-wide
read rule. Nothing here is mounted on the public API.
"""

from __future__ import annotations

from typing import Any

from flask import Flask, abort, jsonify, request, session

import station_events
from auth import _has_station_write_access, _session_role, require_auth

DEFAULT_LIMIT = 50
MAX_LIMIT = 1000


def _query_args() -> tuple[int, str | None]:
    try:
        limit = int(request.args.get("limit", DEFAULT_LIMIT))
    except (TypeError, ValueError):
        abort(400)
    limit = max(1, min(limit, MAX_LIMIT))
    raw_since = request.args.get("since")
    since = station_events.parse_since(raw_since)
    if raw_since and since is None:
        abort(400)
    return limit, since


def register_station_history_routes(app: Flask, config: Any) -> None:
    @app.route("/api/station_events/<host_key>")
    @require_auth
    def api_station_events(host_key: str):
        if host_key not in config.stations:
            abort(404)
        if not _has_station_write_access(host_key):
            abort(403)
        limit, since = _query_args()
        events = station_events.list_events([host_key], limit=limit, since=since)
        resp = jsonify({"station": host_key, "count": len(events), "events": events})
        resp.headers["Cache-Control"] = "private, no-store"
        return resp

    @app.route("/api/station_events")
    @require_auth
    def api_station_events_all():
        role = _session_role()
        if role == "admin":
            hosts = None
        elif role == "host":
            hosts = [h for h in (session.get("stations") or []) if h in config.stations]
        else:
            abort(403)
        limit, since = _query_args()
        events = station_events.list_events(hosts, limit=limit, since=since)
        resp = jsonify({"count": len(events), "events": events})
        resp.headers["Cache-Control"] = "private, no-store"
        return resp
