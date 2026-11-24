"""Fireball report page -- the write-up a Hall of Fame card opens.

Reports are files in ``fireball_reports_data/<event_id>/`` (see
``fireball_reports.py``); these routes render one as a page and serve its
figures. Both share the "overview" public-page toggle with the Hall of Fame
that links to them, and an anonymous visitor sees a report, its cameras and
its clips only for cameras owned by ``public: true`` stations, the same rule
as ``/media/v1/*``.
"""

import logging
import re
from datetime import datetime, timezone

from flask import Flask, abort, render_template, send_from_directory

import fireball_reports as fr
from auth import camera_owner, camera_owner_visible, is_anonymous
from security import public_route

logger = logging.getLogger(__name__)

# <CAM>_<YYYYMMDD>_<HHMMSS>[_<HHMMSS>]_color.mkv: the chunk's UTC start.
_CHUNK_START_RE = re.compile(r"_(\d{8})_(\d{6})(?:_\d{6})?_color\.(?:mkv|mp4)$")


def _utc_epoch(ymd: str, hms: str) -> float | None:
    """Epoch seconds of a YYYYMMDD + HHMMSS UTC stamp, or None if malformed."""
    try:
        return datetime.strptime(ymd + hms, "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _chunk_start_epoch(filename: str) -> float | None:
    """UTC start of a colour chunk from its filename (None if it has none)."""
    m = _CHUNK_START_RE.search(filename)
    return _utc_epoch(m.group(1), m.group(2)) if m else None


def register_fireball_report_routes(app: Flask, config) -> None:

    import public_api as _pub

    def _camera_is_visible(camera: str, anonymous: bool) -> bool:
        return camera_owner_visible(config, camera, anonymous)

    def _camera_place(camera: str) -> str:
        _, st = camera_owner(config, camera)
        return st.label if st is not None and st.label else ""

    def _visible_report(event_id: str, anon: bool) -> fr.Report:
        """The report, or 404 when it is unknown or shows no camera this
        visitor may see."""
        if not fr.EVENT_ID_RE.match(event_id):
            abort(404)
        report = fr.all_reports().get(event_id)
        if report is None or not any(_camera_is_visible(c, anon) for c in report.cameras):
            abort(404)
        return report

    @app.route("/fireball/<event_id>")
    @public_route(page="overview")
    def fireball_report_page(event_id: str):
        anon = is_anonymous()
        report = _visible_report(event_id, anon)

        cameras = [{"code": c, "place": _camera_place(c)}
                   for c in report.cameras if _camera_is_visible(c, anon)]
        clips = []
        for clip in report.clips:
            if not _camera_is_visible(clip.camera, anon):
                continue
            url = f"/media/v1/clip/{clip.camera}/{clip.date}/{clip.filename}"
            stack = _pub._derive_stack_name(clip.filename)
            clips.append({
                "camera": clip.camera,
                "place": _camera_place(clip.camera),
                # MKV needs the on-the-fly MP4 remux for Safari/iOS playback.
                "src": url + "?format=mp4" if clip.filename.lower().endswith(".mkv") else url,
                "poster": f"/media/v1/stack/{clip.camera}/{clip.date}/{stack}" if stack else None,
                # Lets "Play all in sync" seek every clip to the same UTC moment.
                "start": _chunk_start_epoch(clip.filename),
            })

        return render_template(
            "fireball_report.html",
            report=report,
            cameras=cameras,
            clips=clips,
            event_epoch=_utc_epoch(*report.event_id.split("_")),
            days_after=(report.analysed - report.event_date).days,
        )

    @app.route("/fireball/<event_id>/<filename>")
    @public_route(page="overview")
    def fireball_report_figure(event_id: str, filename: str):
        report = _visible_report(event_id, is_anonymous())
        # Only figures the report lists, so report.json itself is never served.
        if filename not in report.figure_files:
            abort(404)
        resp = send_from_directory(fr.REPORTS_DIR / event_id, filename, conditional=True)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        if filename.endswith(".svg"):
            # A vector figure opened on its own must not run anything on this
            # origin: no scripts, no external loads, only inline styles and
            # embedded (data:) images.
            resp.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:")
            resp.headers["X-Content-Type-Options"] = "nosniff"
        return resp
