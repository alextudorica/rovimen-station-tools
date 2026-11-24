"""Fireball alerts page: where a signed-in user chooses how to get alerts.

Telegram and Discord are channels the user joins (links from
ROVIMEN_FIREBALL_TELEGRAM_JOIN_URL and ROVIMEN_FIREBALL_DISCORD_INVITE_URL,
shown only to signed-in users); email is a per-user subscription stored as
``alert_email`` in users.yaml, which :mod:`alert_channels` reads at every send.
Every subscribe sends a confirmation email, so it is rate-limited per account
(keyed by account rather than IP, because behind the tunnel every visitor
shares the proxy's address); unsubscribing sends nothing and is not limited.
"""

from __future__ import annotations

import logging

from flask import Flask, abort, jsonify, render_template, request, session

import alert_channels
import security
from auth import _load_users, _update_users, require_auth

logger = logging.getLogger(__name__)


def _env(name: str) -> str:
    return (alert_channels.load_settings().get(name) or "").strip()


def _account_key() -> str:
    return f"alert-email:{session.get('user') or '-'}"


def register_alert_routes(app: Flask, build_version: str) -> None:
    limiter = app._limiter  # type: ignore[attr-defined]

    @app.route("/alerts")
    @require_auth
    def alerts_page():
        u = _load_users().get(session["user"])
        mail = alert_channels.mail_settings()
        return render_template(
            "alerts.html", build_version=build_version,
            telegram_url=_env("ROVIMEN_FIREBALL_TELEGRAM_JOIN_URL"),
            discord_url=_env("ROVIMEN_FIREBALL_DISCORD_INVITE_URL"),
            email_enabled=bool(mail["user"] and mail["password"]),
            alert_email=u.alert_email if u else None)

    def _set(username: str, address: str | None) -> None:
        def mutate(users):
            if username not in users:
                abort(404)
            users[username].alert_email = address
        _update_users(mutate)

    @app.route("/api/alerts/email", methods=["POST"])
    @limiter.limit("3 per hour", key_func=_account_key)
    @require_auth
    def alerts_email_subscribe():
        username = session["user"]
        if _load_users().get(username) is None:
            # environment admins have no users.yaml row to store it in
            return jsonify({"error": "this account cannot store a subscription"}), 400
        address = str((request.get_json(silent=True) or {}).get("email", "")).strip().lower()
        if len(address) > 254 or not alert_channels.EMAIL_RE.match(address):
            return jsonify({"error": "not a valid email address"}), 400
        _set(username, address)
        security.audit_request("fireball_alert_email_subscribe", user=username)
        u = _load_users().get(username)
        ok, err = alert_channels.send_confirmation_email(address, (u.display_name if u else "") or username)
        return jsonify({"alert_email": address, "confirmation_sent": ok, "confirmation_error": err})

    @app.route("/api/alerts/email", methods=["DELETE"])
    @require_auth
    def alerts_email_unsubscribe():
        username = session["user"]
        if _load_users().get(username) is None:
            return jsonify({"error": "this account cannot store a subscription"}), 400
        _set(username, None)
        security.audit_request("fireball_alert_email_unsubscribe", user=username)
        return jsonify({"alert_email": None})
