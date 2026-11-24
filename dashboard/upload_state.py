"""Shared "not uploaded yet / overdue" state for archive artifacts (#1250).

Anything a station uploads (clips, timelapses, night stacks, stacks, RMS
plots) is served from the archive, never pulled from the station. A file the
archive does not hold yet is therefore *not uploaded yet*, and once its
night's processing should long have finished, *overdue*. Every consumer
(the clip/timelapse/thumbnail routes, the detections payload's pending
markers, the calibration page's plot status) asks this module, so the same
night reads the same way everywhere.

Night convention: a night bucket ``D`` starts on the evening of ``D`` and
ends near dawn of ``D + 1``; ``night_end`` is ``D + 1 00:00 UTC``. The
deadline per kind is counted from there:

* ``plot``: 6 h. ApplyRecalibrate and the RMS plots land within hours of
  dawn (the value ``routes/archive.resolve_rms_plot_status`` always used).
* every other kind: 14 h, i.e. 14:00 UTC on ``D + 1``. Dawn processing
  stops at the 13:15 UTC deadline (``services.reencode.full_night``), and the
  fleet reboots at 13:45 UTC, so an upload still missing at 14:00 is late.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from flask import Response, jsonify

OVERDUE_HOURS: dict[str, float] = {"plot": 6.0}
DEFAULT_OVERDUE_HOURS = 14.0

NOT_UPLOADED = "not_uploaded_yet"
OVERDUE = "overdue"


def night_end(date: str) -> datetime:
    """``D + 1 00:00 UTC`` for night bucket ``date`` (``YYYYMMDD``)."""
    start = datetime.strptime(date, "%Y%m%d").replace(tzinfo=timezone.utc)
    return start + timedelta(days=1)


def upload_due(date: str, kind: str = "clip") -> datetime:
    """When an artifact of ``kind`` for night ``date`` should be archived."""
    hours = OVERDUE_HOURS.get(kind, DEFAULT_OVERDUE_HOURS)
    return night_end(date) + timedelta(hours=hours)


def is_overdue(date: str, kind: str = "clip", now: datetime | None = None) -> bool:
    """True once ``kind`` for ``date`` is past its upload deadline. A
    malformed date is never overdue (callers validate dates themselves)."""
    now = now or datetime.now(timezone.utc)
    try:
        return now > upload_due(date, kind)
    except ValueError:
        return False


def upload_state(date: str, kind: str = "clip", now: datetime | None = None) -> dict[str, Any]:
    """``{"state", "overdue", "due"}`` for a missing artifact.

    ``state`` is ``"overdue"`` past the deadline, else ``"not_uploaded_yet"``;
    ``due`` is the deadline as ISO 8601 (None for a malformed date)."""
    try:
        due = upload_due(date, kind)
    except ValueError:
        return {"state": NOT_UPLOADED, "overdue": False, "due": None}
    now = now or datetime.now(timezone.utc)
    overdue = now > due
    return {
        "state": OVERDUE if overdue else NOT_UPLOADED,
        "overdue": overdue,
        "due": due.isoformat(timespec="minutes"),
    }


def not_uploaded_response(
    kind: str, cam: str, date: str, filename: str, now: datetime | None = None,
) -> tuple[Response, int]:
    """The 404 every archive-first media route answers for a missing file.

    The body carries ``error: "not_uploaded_yet"`` plus the shared state, so
    the frontend can say "not uploaded yet" (or "upload overdue") instead of
    a bare "not found"."""
    st = upload_state(date, kind, now)
    label = "upload overdue" if st["overdue"] else "not uploaded yet"
    body = {
        "error": NOT_UPLOADED,
        "kind": kind,
        "cam": cam,
        "date": date,
        "filename": filename,
        **st,
        "detail": f"{filename}: {label} ({cam}/{date})",
    }
    return jsonify(body), 404
