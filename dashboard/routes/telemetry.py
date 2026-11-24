"""Client-event beacon: failures users saw and tab views inside pages.

``POST /api/client-event`` takes a small batch from ``dashboard-common.js``
and appends one activity-log record per valid event (validation in
``client_events.build_records``). It exists because the server alone cannot
see a 504 that nginx served while Flask kept working, nor a tab switch that
makes no request; the admin Usage Stats read the records back
(``usage_stats``).

Authenticated only: the auth gate answers anonymous callers with 401 and the
browser stops reporting for the rest of the page load. Bounded three ways:
at most ``client_events.MAX_EVENTS_PER_POST`` events per POST, a per-account
rate limit (keyed by account rather than IP, because behind the tunnel every
visitor shares the proxy's address), and the activity-log queue's own drop
policy. Answers 204 (429 over the limit) with no body, so the client never
has anything to retry; it stops reporting on 401, 403 or 429.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from flask import Flask, Response, request, session

logger = logging.getLogger(__name__)


def _account_key() -> str:
    return f"client-event:{session.get('user') or '-'}"


def register_telemetry_routes(app: Flask) -> None:
    import client_events
    import security

    limiter = app._limiter  # type: ignore[attr-defined]

    @app.route("/api/client-event", methods=["POST"])
    @limiter.limit("12 per minute", key_func=_account_key)
    @limiter.limit("240 per hour", key_func=_account_key)
    def api_client_event():
        user = session.get("user")
        if user:
            try:
                records = client_events.build_records(
                    user,
                    request.get_json(silent=True),
                    ip=security._analytics_ip(),
                    ua=request.headers.get("User-Agent"),
                    now=datetime.now(timezone.utc),
                )
                for record in records:
                    security.enqueue_activity_record(record)
            except Exception:
                # Telemetry must never turn into an error the page sees.
                logger.exception("client-event ingest failed")
        return Response(status=204)
