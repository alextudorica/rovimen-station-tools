"""Client-reported events: what the server cannot see on its own.

Two things never reach ``activity.log`` through the normal request hook:

* **Failures the user saw.** When nginx gives up on a slow request at its
  30 s ``proxy_read_timeout`` the browser gets a 504, but Flask finishes
  later and logs a 200. Over 14 days to 2026-09-26 nginx served 73 such
  504s on ``/api/detections`` that the Usage Stats never showed. Media
  elements (``<video>``) that fail to load are invisible for the same
  reason.
* **Tab views inside a page.** The Detections tabs (All Detections,
  Multi-Station Events, Grid, Sort & Filter) switch without a request, so
  their use could not be measured at all.

The browser batches both into ``POST /api/client-event`` (see
``dashboard-common.js``). This module turns one POST body into activity-log
records. It is pure and strict: anything malformed is dropped, paths are
same-origin paths only (no scheme, host, query or fragment), and a POST
yields at most ``MAX_EVENTS_PER_POST`` records.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

KIND_FAILURE = "client_failure"
KIND_TAB = "tab_view"
CLIENT_EVENT_KINDS = frozenset({KIND_FAILURE, KIND_TAB})

MAX_EVENTS_PER_POST = 20
_PATH_MAX = 200
_REASONS = frozenset({"http", "network", "timeout", "media"})
_TAB_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")


def _clean_path(value: Any) -> str | None:
    """A same-origin absolute path, without query or fragment, or None."""
    if not isinstance(value, str):
        return None
    path = value.strip().split("?", 1)[0].split("#", 1)[0]
    if not path.startswith("/") or path.startswith("//"):
        return None
    if any(ch.isspace() or ord(ch) < 32 for ch in path):
        return None
    return path[:_PATH_MAX]


def _status(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value if 0 <= value <= 599 else 0


def build_records(
    user: str,
    payload: Any,
    *,
    ip: str | None,
    ua: str | None,
    now: datetime,
) -> list[dict[str, Any]]:
    """Activity-log records for one client-event POST body.

    Body shape::

        {"events": [
          {"type": "failure", "path": "/api/detections/20260921",
           "status": 504, "reason": "http", "page": "/events"},
          {"type": "tab", "page": "/events", "tab": "grid"}
        ]}

    Records share the request records' keys (``ts``, ``user``, ``method``,
    ``path``, ``status``, ``ip``, ``ua``) plus ``kind``, so every existing
    reader still parses them; readers that count requests skip anything
    carrying ``kind``.
    """
    events = payload.get("events") if isinstance(payload, dict) else None
    if not user or not isinstance(events, list):
        return []
    base = {
        "ts": now.isoformat(timespec="seconds"),
        "user": user,
        "method": "CLIENT",
        "ip": ip,
        "ua": (ua or "")[:120] or None,
    }
    out: list[dict[str, Any]] = []
    for ev in events[:MAX_EVENTS_PER_POST]:
        if not isinstance(ev, dict):
            continue
        page = _clean_path(ev.get("page"))
        kind = ev.get("type")
        if kind == "failure":
            path = _clean_path(ev.get("path"))
            reason = ev.get("reason")
            if not path or reason not in _REASONS:
                continue
            out.append({**base, "kind": KIND_FAILURE, "path": path,
                        "status": _status(ev.get("status")), "reason": reason,
                        "page": page})
        elif kind == "tab":
            tab = ev.get("tab")
            if not page or not isinstance(tab, str) or not _TAB_RE.match(tab):
                continue
            out.append({**base, "kind": KIND_TAB, "path": page, "status": 0,
                        "tab": tab})
    return out
