"""Hide and unhide one detection as a false positive (bird, satellite, ...).

``POST /api/detections/hide`` and ``POST /api/detections/unhide`` take a JSON
body naming one detection: ``cam`` plus either its key (``ff_file`` and
``meteor_no``) or its ``time_utc`` (the clip cards on most pages know only
the meteor time; the server resolves it to the detection row, the brightest
one of that camera in that second). Optional ``reason`` (one of
``hidden_detections.REASONS``) and ``page`` (where the click came from).

Who may: any signed-in account allowed to see the camera
(``auth.may_hide_detections`` and ``auth._has_camera_access``); not anonymous
visitors, not public-API keys (this is a session route) and not magic-link
sessions. Each write is rate-limited per account and written to the audit
log. The store is ``hidden_detections``; listings read it through its
in-memory snapshot.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from flask import Flask, abort, jsonify, request, session

import detection_db
import hidden_detections
import security
from auth import _has_camera_access, _host_keys_for_camera, may_hide_detections

logger = logging.getLogger(__name__)

_CAM_RE = re.compile(r"^[A-Za-z0-9]{2,16}$")
_FF_RE = re.compile(r"^[\w.-]{1,160}$")
_TIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}")

# A person going through a magnitude-sorted list with the keyboard manages
# about one a second; a script would do thousands a minute.
HIDE_RATE_LIMITS = ("60 per minute", "600 per hour")


def _account_key() -> str:
    return f"detection-hide:{session.get('user') or security._client_ip()}"


def _parse_body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400)
    cam = str(body.get("cam") or "").strip().upper()
    if not _CAM_RE.match(cam):
        abort(400)
    ff_file = body.get("ff_file")
    meteor_no = body.get("meteor_no")
    time_utc = body.get("time_utc")
    if ff_file is not None or meteor_no is not None:
        if not isinstance(ff_file, str) or not _FF_RE.match(ff_file):
            abort(400)
        try:
            meteor_no = int(meteor_no)
        except (TypeError, ValueError):
            abort(400)
        if not 0 <= meteor_no <= 100000:
            abort(400)
        time_utc = None
    elif not isinstance(time_utc, str) or not _TIME_RE.match(time_utc):
        abort(400)
    reason = body.get("reason") or None
    if reason is not None and reason not in hidden_detections.REASONS:
        abort(400)
    page = body.get("page") or None
    if page is not None and page not in hidden_detections.PAGES:
        page = "other"
    return {"cam": cam, "ff_file": ff_file, "meteor_no": meteor_no,
            "time_utc": time_utc, "reason": reason, "page": page}


def _check_permission() -> None:
    """401 without a session, 403 for a session that may not hide (magic
    link, unknown role)."""
    if not session.get("user"):
        abort(401)
    if not may_hide_detections():
        abort(403)


def register_hidden_detection_routes(app: Flask, config) -> None:
    limiter = app._limiter  # type: ignore[attr-defined]

    def _resolve(args: dict[str, Any]) -> dict[str, Any]:
        _check_permission()
        # A camera no station owns is 404; one this session may not see, 403.
        if not _host_keys_for_camera(config, args["cam"]):
            abort(404)
        if not _has_camera_access(config, args["cam"]):
            abort(403)
        try:
            det = detection_db.find_detection(
                args["cam"], ff_file=args["ff_file"], meteor_no=args["meteor_no"],
                time_utc=args["time_utc"], path=detection_db.DB_PATH)
        except Exception:
            logger.warning("hide: detection lookup failed", exc_info=True)
            det = None
        if det is None:
            abort(404)
        return det

    def _respond(det: dict[str, Any], state: dict[str, Any], changed: bool):
        return jsonify({
            "cam": det["cam"], "ff_file": det["ff_file"], "meteor_no": det["meteor_no"],
            "date": det.get("date"), "time_utc": det.get("time_utc"),
            "changed": changed, **state,
        })

    @app.route("/api/detections/hide", methods=["POST"])
    @limiter.limit(HIDE_RATE_LIMITS[0], key_func=_account_key)
    @limiter.limit(HIDE_RATE_LIMITS[1], key_func=_account_key)
    def api_detection_hide():
        args = _parse_body()
        det = _resolve(args)
        user = session["user"]
        try:
            state, changed = hidden_detections.hide(
                det, user, reason=args["reason"], page=args["page"])
        except Exception:
            logger.exception("hide: could not write the hidden-detections store")
            return jsonify({"error": "could not save the hide"}), 500
        security.audit_request(
            "detection_hide", user=user, cam=det["cam"], ff_file=det["ff_file"],
            meteor_no=det["meteor_no"], reason=args["reason"], page=args["page"],
            changed=changed)
        return _respond(det, state, changed)

    @app.route("/api/detections/unhide", methods=["POST"])
    @limiter.limit(HIDE_RATE_LIMITS[0], key_func=_account_key)
    @limiter.limit(HIDE_RATE_LIMITS[1], key_func=_account_key)
    def api_detection_unhide():
        args = _parse_body()
        det = _resolve(args)
        user = session["user"]
        try:
            state, changed = hidden_detections.unhide(
                det["cam"], det["ff_file"], det["meteor_no"], user, page=args["page"])
        except Exception:
            logger.exception("unhide: could not write the hidden-detections store")
            return jsonify({"error": "could not save the unhide"}), 500
        security.audit_request(
            "detection_unhide", user=user, cam=det["cam"], ff_file=det["ff_file"],
            meteor_no=det["meteor_no"], page=args["page"], changed=changed)
        return _respond(det, state, changed)

    @app.route("/api/detections/hidden/history", methods=["GET"])
    def api_detection_hide_history():
        """Every hide and unhide of one detection (signed-in users)."""
        if not session.get("user"):
            abort(401)
        cam = (request.args.get("cam") or "").upper()
        ff_file = request.args.get("ff_file") or ""
        try:
            meteor_no = int(request.args.get("meteor_no", ""))
        except ValueError:
            abort(400)
        if not _CAM_RE.match(cam) or not _FF_RE.match(ff_file):
            abort(400)
        if not _has_camera_access(config, cam):
            abort(403)
        return jsonify({"history": hidden_detections.history(cam, ff_file, meteor_no)})
