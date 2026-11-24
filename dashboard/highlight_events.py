"""Group one night's detections into the events the Highlights page shows.

An event is one meteor and every ROVIMEN camera that recorded it. Two
sources define one:

* a GMN trajectory (``gmn_data.events_for_date``): authoritative, with an
  absolute magnitude, speed and heights, but published 4-15 days after the
  night;
* a ROVIMEN coincidence (``event_correlation``): two or more of our stations
  within the correlation window, available the morning after. These carry
  the brightest apparent magnitude among their witnesses and rank after the
  confirmed events.

A detection joins a GMN event when its camera is on the trajectory and its
time is within ``GMN_MATCH_S`` of the trajectory's begin time; a coincidence
is kept only when none of its witnesses already joined a GMN event, so one
meteor never appears twice.

The functions are pure (no Flask, no I/O) so ``tests/test_highlight_events.py``
can drive them with plain dicts.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Callable, Iterable

GMN_MATCH_S = 3.0
FIREBALL_MAG = -4.0
# Per night and overall: the page shows the brightest events, and keeping a
# bounded set per night keeps a 365-day range from holding every event.
TOP_EVENTS = 24

_CHUNK_TIME_RE = re.compile(r"_(\d{6})(?:_|\.)")


def norm_time(ts: str | None) -> str:
    """``YYYY-MM-DDTHH:MM:SS`` from an ISO or space-separated timestamp."""
    return ts.replace(" ", "T")[:19] if ts else ""


def _seconds_apart(a: str, b: str) -> float:
    try:
        return abs((datetime.fromisoformat(a) - datetime.fromisoformat(b)).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def detection_offset(filename: str, time_iso: str | None) -> float | None:
    """Seconds from the chunk start (HHMMSS in the filename) to ``time_iso``."""
    m = _CHUNK_TIME_RE.search(filename or "")
    if not (m and time_iso):
        return None
    try:
        chk = m.group(1)
        chk_s = int(chk[:2]) * 3600 + int(chk[2:4]) * 60 + int(chk[4:6])
        t = datetime.fromisoformat(time_iso.replace(" ", "T").rstrip("Z"))
    except ValueError:
        return None
    diff = t.hour * 3600 + t.minute * 60 + t.second + t.microsecond / 1e6 - chk_s
    if diff < -43200:
        diff += 86400
    return round(max(0.0, diff), 2)


def witness(det: dict, date_iso: str) -> dict | None:
    """One camera's view of an event, with its archive media URLs.

    ``clip_url`` is None when the clip is not in the archive (pending,
    no video, pruned); the stack can still exist."""
    cam = det.get("cam") or det.get("camera")
    filename = det.get("filename")
    if not (cam and filename):
        return None
    rms = det.get("rms") or {}
    mag = rms.get("mag_apparent")
    if mag is None:
        mag = rms.get("mag_absolute")
    stack = det.get("stack")
    has_clip = not (det.get("pending") or det.get("no_clip") or det.get("pruned"))
    time_utc = norm_time(det.get("meteor_time"))
    offset = det.get("detection_offset_s")
    if offset is None:
        offset = detection_offset(filename, det.get("meteor_time"))
    out = {
        "camera":             cam,
        "host_key":           det.get("host_key"),
        "station_label":      det.get("station_label"),
        "filename":           filename,
        "date":               date_iso,
        "time_utc":           time_utc or None,
        "apparent_magnitude": mag,
        "duration_s":         rms.get("duration_s"),
        "shower":             rms.get("shower"),
        "detection_offset_s": offset,
        "clip_url":           f"/api/highlights/clip/{cam}/{date_iso}/{filename}" if has_clip else None,
        "stack_url":          f"/api/highlights/stack/{cam}/{date_iso}/{stack}" if stack else None,
    }
    if det.get("hidden"):
        # Marked by hidden_detections.apply_payload (a false positive a user hid).
        for k in ("hidden", "hidden_by", "hidden_at", "hidden_reason"):
            out[k] = det.get(k)
    return out


def _listed(ws: list[dict], show_hidden: bool) -> list[dict]:
    """The witnesses an event lists: hidden ones only under ``show_hidden``."""
    return ws if show_hidden else [w for w in ws if not w.get("hidden")]


def _dedupe(witnesses: Iterable[dict]) -> list[dict]:
    """One tile per camera: a camera can lock two adjacent chunks for one
    meteor; keep the one with a clip, then the most pre-event context."""
    best: dict[str, dict] = {}
    for w in witnesses:
        cur = best.get(w["camera"])
        rank = (w["clip_url"] is not None, w.get("detection_offset_s") or 0)
        if cur is None or rank > (cur["clip_url"] is not None, cur.get("detection_offset_s") or 0):
            best[w["camera"]] = w
    return sorted(best.values(), key=lambda w: (w["clip_url"] is None, w.get("station_label") or "", w["camera"]))


def _min_mag(witnesses: list[dict]) -> float | None:
    mags = [w["apparent_magnitude"] for w in witnesses if w.get("apparent_magnitude") is not None]
    return min(mags) if mags else None


def night_events(
    detections: list[dict],
    gmn_events: list[dict],
    coincidences: list[dict],
    date_iso: str,
    visible: Callable[[dict], bool] = lambda w: True,
    show_hidden: bool = False,
) -> list[dict]:
    """The events of one night, best first, at most ``TOP_EVENTS``.

    ``detections`` and each coincidence's ``witnesses`` are raw detections
    (``cam``, ``filename``, ``meteor_time``, ``rms``, ...); ``gmn_events``
    are ``gmn_data`` event dicts. ``visible`` drops witnesses the caller may
    not see (anonymous visitors: non-public stations). An event needs at
    least one visible witness with a clip.

    Detections marked ``hidden`` (false positives a user hid, see
    ``hidden_detections``) are not listed unless ``show_hidden``: an event
    whose witnesses are all hidden is left out, one with some hidden lists
    the others and ranks by them. With ``show_hidden`` a fully hidden event
    carries ``hidden: true``."""
    witnesses = [w for w in (witness(d, date_iso) for d in detections) if w and visible(w)]

    by_gmn: dict[str, list[dict]] = {}
    gmn_by_id: dict[str, dict] = {}
    joined: set[tuple[str, str]] = set()
    for ev in gmn_events:
        ev_id = str(ev.get("id") or ev.get("time") or "")
        ev_t = norm_time(ev.get("time"))
        cams = set(ev.get("stations") or [])
        if not (ev_id and ev_t and cams):
            continue
        for w in witnesses:
            if w["camera"] in cams and w["time_utc"] and _seconds_apart(w["time_utc"], ev_t) <= GMN_MATCH_S:
                by_gmn.setdefault(ev_id, []).append(w)
                gmn_by_id[ev_id] = ev
                joined.add((w["camera"], w["filename"]))

    events: list[dict] = []
    for ev_id, ws in by_gmn.items():
        ws = _listed(_dedupe(ws), show_hidden)
        if not any(w["clip_url"] for w in ws):
            continue
        ev = gmn_by_id[ev_id]
        mag = ev.get("peak_mag")
        events.append({
            "id":              f"gmn-{ev_id}",
            "confirmed":       True,
            "time_utc":        norm_time(ev.get("time")),
            "date":            date_iso,
            "peak_magnitude":  mag if mag is not None else _min_mag(ws),
            "magnitude_kind":  "absolute" if mag is not None else "apparent",
            "duration_s":      ev.get("duration_s"),
            "shower":          ev.get("shower") or "SPO",
            # Entry speed only: falling back to the geocentric Vg under the
            # same "Speed" label would show 4.7 km/s for a slow meteoroid.
            "velocity_kms":    ev.get("velocity_init"),
            "height_begin_km": ev.get("altitude_begin"),
            "height_end_km":   ev.get("altitude_end"),
            "gmn_cameras":     sorted(ev.get("stations") or []),
            "witnesses":       ws,
        })

    for co in coincidences:
        raw = [witness(d, date_iso) for d in (co.get("witnesses") or [])]
        ws = [w for w in raw if w and visible(w)]
        if any((w["camera"], w["filename"]) in joined for w in ws):
            continue
        ws = _dedupe(ws)
        if len({w["host_key"] for w in ws}) < 2:
            continue
        ws = _listed(ws, show_hidden)
        if not any(w["clip_url"] for w in ws):
            continue
        first = ws[0]
        durations = [w["duration_s"] for w in ws if w.get("duration_s") is not None]
        showers = [w["shower"] for w in ws if w.get("shower")]
        events.append({
            "id":              f"rv-{first['camera']}-{norm_time(co.get('event_time')).replace(':', '')}",
            "confirmed":       False,
            "time_utc":        norm_time(co.get("event_time")),
            "date":            date_iso,
            "peak_magnitude":  _min_mag(ws),
            "magnitude_kind":  "apparent",
            "duration_s":      max(durations) if durations else None,
            "shower":          showers[0] if showers else "SPO",
            "velocity_kms":    None,
            "height_begin_km": None,
            "height_end_km":   None,
            "gmn_cameras":     [],
            "witnesses":       ws,
        })

    for ev in events:
        if show_hidden and all(w.get("hidden") for w in ev["witnesses"]):
            ev["hidden"] = True
        ev["station_count"] = len({w["host_key"] for w in ev["witnesses"]})
        ev["fireball"] = ev["peak_magnitude"] is not None and ev["peak_magnitude"] <= FIREBALL_MAG

    events.sort(key=rank_key)
    return events[:TOP_EVENTS]


def rank_key(ev: dict) -> tuple:
    """GMN-confirmed first, then brightest, then longest, then most cameras."""
    mag = ev.get("peak_magnitude")
    return (
        not ev.get("confirmed"),
        mag if mag is not None else float("inf"),
        -(ev.get("duration_s") or 0),
        -len(ev.get("witnesses") or ()),
    )


def report_time_matches(event_id: str, time_utc: str, tolerance_s: float = 30.0) -> bool:
    """True when a report's ``event_id`` (``YYYYMMDD_HHMMSS``) is within
    ``tolerance_s`` of an event time."""
    try:
        rep_t = datetime.strptime(event_id, "%Y%m%d_%H%M%S").isoformat()
    except ValueError:
        return False
    return _seconds_apart(rep_t, norm_time(time_utc)) <= tolerance_s


def attach_reports(events: list[dict], reports: Iterable[Any]) -> None:
    """Set ``report`` ({url, title, status}) on every event a fireball report
    covers: the report lists one of its clips, or its time is within 30 s."""
    reports = list(reports)
    for ev in events:
        ev["report"] = None
        clips = {(w["camera"], w["filename"]) for w in ev["witnesses"]}
        for rep in reports:
            if any((c.camera, c.filename) in clips for c in rep.clips) or report_time_matches(
                rep.event_id, ev["time_utc"]
            ):
                ev["report"] = {"url": rep.url, "title": rep.title, "status": rep.status}
                break
