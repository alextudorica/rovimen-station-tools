"""Multi-station event correlation, shared by the Detections/Events routes
and the VPS jobs that act on events (``clip_requester``).

Pure functions over detection dicts and archive file names: no Flask, no
I/O, so a standalone job computes exactly the events the Events page shows.
"""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta

# Fixed reference point + unit for turning a parsed meteor_time into integer
# microseconds. Naive subtraction (no timezone/DST semantics) so the difference
# between two timestamps is exactly what ``t1 - t0`` produced before.
_CORR_EPOCH = datetime(1970, 1, 1)
_ONE_US = timedelta(microseconds=1)

# Matches a boundary-stitched clip: RO000P_20260724_002833_002853_color.mkv
_STITCHED_FNAME_RE = re.compile(
    r"^[A-Z0-9]+_\d{8}_(\d{6})_(\d{6})_color\.(?:mkv|mp4)$"
)

# A stitch never spans more than two chunks, so reject a wild Tstart/Tend pair
# rather than let it swallow a detection minutes away.
_MAX_STITCH_SPAN_S = 600


def resolve_stitched_chunk(time_utc: str | None, meteor_files) -> str | None:
    """Map a detection time onto a boundary-stitched clip that spans it.

    ``detection_lock._stitch_clips`` splices the two chunks either side of a
    meteor that lands within STITCH_BOUNDARY_SECS of a chunk edge into
    ``<cam>_<date>_<Tstart>_<Tend>_color.mkv``, then locks and uploads *that*
    while the raw chunks stay unlocked. The station's detection index still
    records the raw chunk name, so an exact-name lookup reports "clip pending"
    for a clip already sitting in the archive.

    The stitch window contains the meteor by construction (Tstart is one half
    chunk before the boundary the meteor straddles, Tend one half after), so
    match on the span rather than on a filename-timestamp delta. Prefer the
    narrowest containing span, and let .mkv beat its .mp4 sibling — the caller
    passes a set, whose iteration order would otherwise flip the emitted name
    between requests.
    """
    if not time_utc or not meteor_files:
        return None
    try:
        t = datetime.fromisoformat(time_utc.replace(" ", "T")[:19])
    except ValueError:
        return None
    det_s = t.hour * 3600 + t.minute * 60 + t.second
    best_name: str | None = None
    best_span: int | None = None
    for fname in meteor_files:
        m = _STITCHED_FNAME_RE.match(fname)
        if not m:
            continue
        a, b = m.group(1), m.group(2)
        start_s = int(a[:2]) * 3600 + int(a[2:4]) * 60 + int(a[4:6])
        end_s = int(b[:2]) * 3600 + int(b[2:4]) * 60 + int(b[4:6])
        span = (end_s - start_s) % 86400
        if not 0 < span <= _MAX_STITCH_SPAN_S:
            continue
        if (det_s - start_s) % 86400 > span:
            continue
        if (
            best_span is None
            or span < best_span
            or (span == best_span and fname.endswith(".mkv"))
        ):
            best_name, best_span = fname, span
    return best_name


# ── Geometry: can two stations' detections be one meteor? ─────────────────
#
# Each detection carries RMS's begin/end azimuth and elevation (FTPdetectinfo,
# through the station index). With the station's position they span a plane
# through the station and the trail. Two detections of one meteor give two
# planes that meet along the trail, so each camera's begin/end rays, carried
# to the other camera's plane, land at meteor heights. Two different meteors
# land underground, in space, or behind the camera. This is the plane test of
# the live fireball alert (fireball_geometry.pair_consistency) without its
# per-frame track, since the index stores only the two end points.
#
# Measured on 42 nights GMN had published (2026-09-01..10-12): of 15,334
# cross-station pairs GMN triangulated as one meteor, 44 fail (0.3 %) and 477
# stay undecided; of 566 pairs GMN put in two different meteors, 460 fail.
# (GMN sometimes publishes one meteor twice from different station subsets;
# trajectories with radiants within 5 deg and speeds within 3 km/s were
# counted as one.)

_WGS_A = 6378137.0
_WGS_E2 = (1 / 298.257223563) * (2 - 1 / 298.257223563)
MAX_PAIR_KM = 900.0      # two stations this far apart cannot share a meteor at 120 km
MIN_PLANE_DEG = 3.0      # planes closer than this: heights are noise, leave it to time
MIN_TRAIL_DEG = 0.3      # a trail shorter than this spans no plane
H_MIN_KM, H_MAX_KM = 20.0, 200.0
_MAX_RANGE_M = 1.5e6

_Vec = tuple[float, float, float]


def _dot(a: _Vec, b: _Vec) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _cross(a: _Vec, b: _Vec) -> _Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def _norm(a: _Vec) -> float:
    return math.sqrt(_dot(a, a))


def _ecef(lat: float, lon: float) -> _Vec:
    la, lo = math.radians(lat), math.radians(lon)
    n = _WGS_A / math.sqrt(1 - _WGS_E2 * math.sin(la) ** 2)
    return (n * math.cos(la) * math.cos(lo), n * math.cos(la) * math.sin(lo),
            n * (1 - _WGS_E2) * math.sin(la))


def _height_km(p: _Vec) -> float:
    r = math.hypot(p[0], p[1])
    lat = math.atan2(p[2], r * (1 - _WGS_E2))
    h = 0.0
    for _ in range(5):
        n = _WGS_A / math.sqrt(1 - _WGS_E2 * math.sin(lat) ** 2)
        h = r / max(math.cos(lat), 1e-12) - n
        lat = math.atan2(p[2], r * (1 - _WGS_E2 * n / (n + h)))
    return h / 1000.0


def _ray(lat: float, lon: float, az: float, el: float) -> _Vec:
    """Unit vector (ECEF) for an azimuth east of north and an elevation, degrees."""
    la, lo, a, e = map(math.radians, (lat, lon, az, el))
    ce = math.cos(e)
    east = (-math.sin(lo), math.cos(lo), 0.0)
    north = (-math.sin(la) * math.cos(lo), -math.sin(la) * math.sin(lo), math.cos(la))
    up = (math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la))
    return tuple(ce * math.sin(a) * east[k] + ce * math.cos(a) * north[k] + math.sin(e) * up[k]
                 for k in range(3))  # type: ignore[return-value]


def _sky(det: dict) -> tuple[float, float, float, float] | None:
    """Begin/end azimuth and elevation, from the detection or its ``rms`` block."""
    rms = det.get("rms") if isinstance(det.get("rms"), dict) else {}
    vals = []
    for k in ("azim_beg", "elev_beg", "azim_end", "elev_end"):
        v = det.get(k, rms.get(k))
        if v is None:
            return None
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            return None
    if not any(vals):
        return None  # RMS writes zeros when the camera has no platepar
    return vals[0], vals[1], vals[2], vals[3]


def _observation(det: dict, coords: tuple[float, float]):
    """(station position, [begin ray, end ray], plane normal) or None."""
    sky = _sky(det)
    if sky is None:
        return None
    lat, lon = coords
    r1, r2 = _ray(lat, lon, sky[0], sky[1]), _ray(lat, lon, sky[2], sky[3])
    n = _cross(r1, r2)
    nn = _norm(n)
    if nn < math.sin(math.radians(MIN_TRAIL_DEG)):
        return None
    return _ecef(lat, lon), (r1, r2), (n[0] / nn, n[1] / nn, n[2] / nn)


def _carried_points(pa: _Vec, rays, pb: _Vec, nb: _Vec) -> list[_Vec] | None:
    """Camera A's begin/end rays carried to camera B's plane: points on the
    line where the two planes meet, or None if a ray points away from it."""
    out = []
    d = (pb[0] - pa[0], pb[1] - pa[1], pb[2] - pa[2])
    for r in rays:
        den = _dot(nb, r)
        if abs(den) < 1e-9:
            return None
        t = _dot(nb, d) / den
        if t <= 0 or t > _MAX_RANGE_M:
            return None
        out.append((pa[0] + t * r[0], pa[1] + t * r[1], pa[2] + t * r[2]))
    return out


def pair_geometry(a: dict, b: dict, coords_a: tuple[float, float] | None,
                  coords_b: tuple[float, float] | None) -> str:
    """``ok``, ``fail`` or ``undecided`` for two detections at different stations."""
    if coords_a is None or coords_b is None:
        return "undecided"
    pa, pb = _ecef(*coords_a), _ecef(*coords_b)
    sep_km = _norm((pa[0] - pb[0], pa[1] - pb[1], pa[2] - pb[2])) / 1000.0
    if sep_km > MAX_PAIR_KM:
        return "fail"
    oa, ob = _observation(a, coords_a), _observation(b, coords_b)
    if oa is None or ob is None:
        return "undecided"
    q = math.degrees(math.acos(min(1.0, abs(_dot(oa[2], ob[2])))))
    if q < MIN_PLANE_DEG:
        return "undecided"
    pts_a = _carried_points(oa[0], oa[1], ob[0], ob[2])
    pts_b = _carried_points(ob[0], ob[1], oa[0], oa[2])
    if pts_a is None or pts_b is None:
        return "fail"
    # Requiring the two cameras' pieces of the line to overlap and run the
    # same way was tried (2026-10-23, 42 GMN nights): it rejected 22 more
    # chance pairs and 76 more real ones, so heights alone decide.
    return "ok" if all(H_MIN_KM <= _height_km(p) <= H_MAX_KM for p in pts_a + pts_b) else "fail"


def station_coords(config) -> dict[str, tuple[float, float]]:
    """Host key -> (lat, lon) from a DashboardConfig, for stations that have both."""
    return {hk: (float(st.lat), float(st.lon)) for hk, st in config.stations.items()
            if st.lat is not None and st.lon is not None}


def correlate_detections(all_det: list[dict], window: float,
                         station_coords: dict[str, tuple[float, float]] | None = None) -> list[dict]:
    """Group detections seen by >=2 stations within ``window`` seconds whose
    geometry allows one meteor.

    ``all_det`` must already be sorted by ``meteor_time`` (the caller sorts it);
    each entry carries at least ``host_key``, ``cam`` and ``meteor_time``, and
    for the geometry ``azim_beg``/``elev_beg``/``azim_end``/``elev_end`` (on the
    entry or in its ``rms`` block). ``station_coords`` maps host key to
    (lat, lon); without it, or without a detection's sky positions, a pair is
    judged on time alone, exactly as before 2026-10-23.

    Grouping: the earliest unused detection anchors a group; every later one
    within ``window`` of it joins unless :func:`pair_geometry` says ``fail``
    against every member at another station (one camera with a poor platepar
    must not split a real event: the 2026-08-30 fireball's RO000R (Cluj)
    failed only against RO0001 (Câmpenița)). A detection that is turned away stays
    free to anchor or join a later group, so a chance coincidence no longer
    swallows the real event beside it. An event is ``geometry: "confirmed"``
    when at least one cross-station pair passed the plane test, else
    ``"time_only"``.

    Timestamps are parsed once into integer microseconds, and the inner scan
    stops at the first detection past the window: the clustering is O(n) per
    window instead of the O(n^2) ISO re-parsing that once pushed busy nights
    past the gunicorn timeout. The early stop needs non-decreasing times; that
    is verified in one pass, with the exhaustive scan kept otherwise. An
    unparseable timestamp fails to correlate.
    """
    window_us = window * 1_000_000
    det_us: list[int | None] = []
    for d in all_det:
        try:
            dt = datetime.fromisoformat(d["meteor_time"])
            det_us.append((dt - _CORR_EPOCH.replace(tzinfo=dt.tzinfo)) // _ONE_US)
        except (ValueError, TypeError):
            det_us.append(None)
    ordered = all(
        a is None or b is None or a <= b for a, b in zip(det_us, det_us[1:])
    )
    coords = station_coords or {}
    verdicts: dict[tuple[int, int], str] = {}

    def verdict(i: int, j: int) -> str:
        key = (i, j) if i < j else (j, i)
        if key not in verdicts:
            a, b = all_det[key[0]], all_det[key[1]]
            verdicts[key] = pair_geometry(a, b, coords.get(a["host_key"]), coords.get(b["host_key"]))
        return verdicts[key]

    used: set[int] = set()
    events: list[dict] = []
    for i, det in enumerate(all_det):
        if i in used:
            continue
        members = [i]
        used.add(i)
        t0 = det_us[i]
        for j in range(i + 1, len(all_det)):
            t1 = det_us[j]
            if t0 is None or t1 is None:
                continue
            if ordered and t1 - t0 > window_us:
                break  # sorted ascending: nothing further can be in range
            if j in used:
                continue
            if abs(t1 - t0) > window_us:
                continue
            host = all_det[j]["host_key"]
            cross = [verdict(m, j) for m in members if all_det[m]["host_key"] != host] if coords else []
            if cross and all(v == "fail" for v in cross):
                continue
            members.append(j)
            used.add(j)
        group = [all_det[m] for m in members]
        # Deduplicate by (host_key, cam): the same camera may lock two adjacent
        # chunks for one meteor (edge detection). Keep the one with the larger
        # detection_offset_s -- it has more pre-event context.
        seen: dict[tuple, dict] = {}
        for w in group:
            key = (w["host_key"], w["cam"])
            if key not in seen or (w.get("detection_offset_s") or 0) > (
                seen[key].get("detection_offset_s") or 0
            ):
                seen[key] = w
        witnesses = list(seen.values())
        station_set = {w["host_key"] for w in witnesses}
        if len(station_set) >= 2:
            confirmed = coords and any(
                all_det[m]["host_key"] != all_det[n]["host_key"] and verdict(m, n) == "ok"
                for k, m in enumerate(members) for n in members[k + 1:])
            events.append({
                "event_time": det["meteor_time"],
                "witness_count": len(witnesses),
                "station_count": len(station_set),
                "witnesses": witnesses,
                "geometry": "confirmed" if confirmed else "time_only",
            })
    return events
