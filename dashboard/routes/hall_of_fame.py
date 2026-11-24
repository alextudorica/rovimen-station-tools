"""Hall-of-fame routes -- manually curated featured meteors.

Admins pick "meteor of the week/month" clips (from the highlights page);
the curated list renders prominently on the landing (overview) page and on
the versioned public API. Entries reference archive clips served through
the keyless ``/media/v1/*`` surface, so featuring a clip never widens
access beyond what the per-station ``public`` flag already allows — and
the write path refuses cameras owned by non-public stations outright.
"""

import logging
import re
import uuid
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, request, session

import fireball_reports
import hall_of_fame_store as hof_store
from auth import camera_owner, is_anonymous, public_camera_codes, require_admin
from security import public_route

logger = logging.getLogger(__name__)

_CAPTION_MAX = 300
_PERIOD_RE = re.compile(r"^[0-9A-Za-z ._/-]{0,40}$")
_CLIP_EXT_RE = re.compile(r"\.(mkv|mp4)$", re.IGNORECASE)
_STACK_EXT_RE = re.compile(r"\.(webp|jpg|jpeg|png)$", re.IGNORECASE)


def register_hall_of_fame_routes(app: Flask, config) -> None:

    # Lazy-import siblings that live in the same dashboard package dir.
    import public_api as _pub

    def _camera_owned_by_public_station(camera: str) -> bool:
        """True when ``camera`` belongs to at least one ``public: true``
        station. Enforced on WRITE (not just read) so an entry can never be
        created for a private/commissioning station — featuring implies
        publishing."""
        return camera.upper() in public_camera_codes(config)

    def _host_for_camera(camera: str) -> str | None:
        """Station key that owns ``camera`` (for station / Video DB links)."""
        return camera_owner(config, camera)[0]

    def _entry_to_json(entry: dict, *, anonymous: bool) -> dict:
        cam, date, filename = entry["camera"], entry["date"], entry["filename"]
        clip_url = f"/media/v1/clip/{cam}/{date}/{filename}"
        stack = entry.get("stack") or _pub._derive_stack_name(filename)
        report = fireball_reports.report_for_clip(cam, filename)
        out = {
            "id":                  entry["id"],
            "camera":              cam,
            "date":                date,
            "filename":            filename,
            "caption":             entry.get("caption") or "",
            "period":              entry.get("period") or "",
            "order":               entry.get("order", 0),
            "shower":              entry.get("shower"),
            "peak_magnitude":      entry.get("peak_magnitude"),
            "duration_s":          entry.get("duration_s"),
            "time_utc":            entry.get("time_utc"),
            "station_label":       entry.get("station_label"),
            "host_key":            _host_for_camera(cam),
            "detection_offset_s":  entry.get("detection_offset_s"),
            "clip_url":            clip_url,
            # MKV needs the on-the-fly MP4 remux for Safari/iOS playback.
            "clip_mp4_url":        clip_url + "?format=mp4"
                                   if filename.lower().endswith(".mkv") else clip_url,
            "stack_url":           f"/media/v1/stack/{cam}/{date}/{stack}"
                                   if stack else None,
            "report_url":          report.url if report else None,
            "report_title":        report.title if report else None,
        }
        if not anonymous:
            out["added_by"] = entry.get("added_by")
            out["added_at"] = entry.get("added_at")
        return out

    def _visible_entries() -> list[dict]:
        """All entries for logged-in users; public-camera entries for anon.

        The public-station filter also runs at write time, so this read-side
        pass only matters when a station is flipped ``public: false`` AFTER
        one of its clips was featured — the entry vanishes from the anon
        surface immediately, same fail-closed behaviour as /media/v1/*.
        """
        entries = hof_store.load_all()
        if is_anonymous():
            entries = [e for e in entries
                       if _camera_owned_by_public_station(e.get("camera", ""))]
        return entries

    # ── Read (landing page + highlights picker) ──────────────────────────

    @app.route("/api/hall_of_fame")
    @public_route(page="overview")
    def hall_of_fame_list():
        """Curated entries in display order. Anonymous callers see only
        public-station entries and no curator identity fields; the route
        shares the "overview" page toggle with the landing page it feeds."""
        anon = is_anonymous()
        return jsonify({
            "entries": [_entry_to_json(e, anonymous=anon)
                        for e in _visible_entries()],
        })

    # ── Admin write surface ──────────────────────────────────────────────

    def _validated_optional_float(payload: dict, field: str) -> float | None:
        raw = payload.get(field)
        if raw is None or raw == "":
            return None
        try:
            return float(raw)
        except (TypeError, ValueError):
            abort(400, description=f"{field} must be a number")

    def _validated_caption(raw) -> str:
        caption = str(raw or "").strip()
        if len(caption) > _CAPTION_MAX:
            abort(400, description=f"caption longer than {_CAPTION_MAX} chars")
        return caption

    def _validated_period(raw, date_iso: str) -> str:
        period = str(raw or "").strip()
        if not period:
            # Default period = calendar month of the clip's night date.
            return date_iso[:7]
        if not _PERIOD_RE.match(period):
            abort(400, description="period contains unsupported characters")
        return period

    @app.route("/api/hall_of_fame", methods=["POST"])
    @require_admin
    def hall_of_fame_add():
        payload = request.get_json(silent=True) or {}
        camera   = str(payload.get("camera", "")).strip().upper()
        filename = str(payload.get("filename", "")).strip()

        # Clip surfaces disagree on the date shape: Highlights and the landing
        # gallery send YYYY-MM-DD, but the station Video DB and the map's GMN
        # clips send compact YYYYMMDD. Normalise to the canonical YYYY-MM-DD the
        # media layer expects, so a clip can be featured from ANY player.
        raw_date = str(payload.get("date", "")).strip()
        if re.fullmatch(r"\d{8}", raw_date):
            date_iso = f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:8]}"
        else:
            date_iso = raw_date

        # Return JSON (not Flask's HTML abort page) so the player's error
        # toast can show the actual reason instead of a bare "HTTP 400".
        def _bad(msg: str, code: int = 400):
            return jsonify({"error": msg}), code

        if not _pub._CAMERA_RE.match(camera):
            return _bad("invalid camera code")
        if not _pub._DATE_ISO_RE.match(date_iso):
            return _bad("invalid date; expected YYYY-MM-DD or YYYYMMDD")
        if not (_pub._FILENAME_RE.match(filename) and _CLIP_EXT_RE.search(filename)):
            return _bad("invalid clip filename")
        if not _camera_owned_by_public_station(camera):
            return _bad("this camera's station is not public, so its clips can't be featured")
        if _pub._resolve_media_path(camera, date_iso, filename, "meteors") is None:
            return _bad("clip not found in the public archive — only uploaded meteor clips can be featured", 404)

        stack = str(payload.get("stack", "") or "").strip() or None
        if stack and not (_pub._FILENAME_RE.match(stack)
                          and _STACK_EXT_RE.search(stack)):
            return _bad("invalid stack filename")

        entries = hof_store.load_all()
        entry = {
            "id":                 f"hof_{uuid.uuid4().hex[:12]}",
            "camera":             camera,
            "date":               date_iso,
            "filename":           filename,
            "stack":              stack,
            "caption":            _validated_caption(payload.get("caption")),
            "period":             _validated_period(payload.get("period"), date_iso),
            "order":              max((e.get("order", 0) for e in entries), default=0) + 1,
            "shower":             (str(payload.get("shower")).strip().upper()[:8]
                                   if payload.get("shower") else None),
            "peak_magnitude":     _validated_optional_float(payload, "peak_magnitude"),
            "duration_s":         _validated_optional_float(payload, "duration_s"),
            "detection_offset_s": _validated_optional_float(payload, "detection_offset_s"),
            "time_utc":           (str(payload.get("time_utc")).strip()[:32]
                                   if payload.get("time_utc") else None),
            "station_label":      (str(payload.get("station_label")).strip()[:64]
                                   if payload.get("station_label") else None),
            "added_by":           session.get("user"),
            "added_at":           datetime.now(timezone.utc).isoformat(),
        }
        try:
            hof_store.add(entry)
        except ValueError:
            return jsonify({"error": "clip already featured"}), 409
        logger.info("hall-of-fame add %s %s/%s/%s by %s", entry["id"],
                    camera, date_iso, filename, entry["added_by"])
        return jsonify(_entry_to_json(entry, anonymous=False)), 201

    @app.route("/api/hall_of_fame/<entry_id>", methods=["PATCH"])
    @require_admin
    def hall_of_fame_patch(entry_id: str):
        payload = request.get_json(silent=True) or {}
        fields: dict = {}
        if "caption" in payload:
            fields["caption"] = _validated_caption(payload["caption"])
        if "period" in payload:
            existing = hof_store.get(entry_id)
            if existing is None:
                abort(404)
            fields["period"] = _validated_period(payload["period"], existing["date"])
        if "order" in payload:
            try:
                fields["order"] = int(payload["order"])
            except (TypeError, ValueError):
                abort(400, description="order must be an integer")
        if not fields:
            abort(400, description="no editable fields in payload "
                                   "(caption, period, order)")
        updated = hof_store.patch(entry_id, fields)
        if updated is None:
            abort(404)
        return jsonify(_entry_to_json(updated, anonymous=False))

    @app.route("/api/hall_of_fame/<entry_id>", methods=["DELETE"])
    @require_admin
    def hall_of_fame_delete(entry_id: str):
        if not hof_store.delete(entry_id):
            abort(404)
        logger.info("hall-of-fame delete %s by %s", entry_id, session.get("user"))
        return jsonify({"ok": True})
