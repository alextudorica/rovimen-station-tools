"""Detections routes -- fan-out, correlation, live-feed.

Extracted from rovimen_dashboard.py. Wired in from ``create_app()`` via
``register_detections_routes`` (routes); ``rovimen_dashboard`` also imports
``compute_detections_payload`` for the public API, and ``routes/highlights.py``
imports it for the range aggregation.

Module layout, the same as ``routes/sky_dome.py`` and ``routes/archive.py``:

- Every handler and helper is a module-level function, so a helper can be
  imported and unit-tested without building the app.
- What they need from ``create_app`` (``config``, ``tunnels``, the station
  status ``cache``, the archive index, the per-night payload cache and its
  TTL, the shared detections and live-feed tiers, the on-disk night store)
  and the per-app state (the footage cache, the background-rebuild set, the
  live-feed snapshot) sit in one frozen ``DetectionsDeps``.
  ``register_detections_routes`` builds it and stores it in ``_DEPS``; a
  function that needs a dependency reads it through ``_deps()`` in its first
  lines. Each registration replaces the holder, so in a process that builds
  several apps (the test suite) the last app registered wins; a gunicorn
  worker builds one app.
- ``register_detections_routes`` is the only code that touches the Flask
  app: it sets ``_DEPS`` and adds the URL rules with ``app.add_url_rule``.
  The endpoint names stay the function names (``api_detections``) and
  ``@public_route`` stays on the functions, so the auth gate
  (``security._matched_public_view``) and the CSRF exemption see the same
  views as when the routes were closures. There is no Blueprint: it would
  rename every endpoint to ``detections.<name>``.
- The archive readers ``_archive_nights`` and ``_read_archive_locked_chunks``
  used to be defined in ``create_app`` and handed over as app attributes;
  they live here now (``routes/media.py`` imports ``_archive_nights``).

Process-wide state (the night-listing gate and the snapshot policy knobs) is
module-private.
"""

from __future__ import annotations

import json
import logging
import os
import re
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from flask import Flask, abort, jsonify, request

from auth import is_anonymous, public_camera_codes, public_host_keys
from cache_store import _drop_future_dates, ARCHIVE_PATH, BoundedCache
from models import StationConfig

import chunk_listings
import detection_db
import gmn_data
import hidden_detections
import upload_state
from http_caching import _json_cached
from event_correlation import (
    correlate_detections as _correlate_detections,
    station_coords,
    resolve_stitched_chunk as _resolve_stitched_chunk,
)
from route_helpers import compute_detection_offset, stack_name_for_clip
from routes.archive import _parse_radiants_txt
from night_payload_store import NightPayloadStore
from security import public_route
from shared_snapshot import SharedSnapshotStore
from station_client import _with_sshfs_timeout, station_get_raw
from tunnels import _TunnelDown

if TYPE_CHECKING:
    from cache_store import _ArchiveIndex
    from models import DashboardConfig
    from station_client import StationCache
    from tunnels import TunnelManager

logger = logging.getLogger(__name__)

# /api/live-feed snapshot policy (module level so tests can tune it): serve a
# snapshot younger than FRESH as is, refresh one older than FRESH in the
# background while still serving it up to STALE, and never let one build run
# past BUDGET seconds of wall clock.
#
# FRESH was 20 s, which meant a visitor polling every 30 s missed the fresh
# window on every single poll and paid a fleet-wide camera fan-out (measured
# 5.4-5.7 s on prod, 15 s while the dawn upload burst had the storagebox mount
# saturated). A live feed showing the last few chunks per camera does not need
# 20 s granularity, so the window now covers a poll interval.
_LIVE_FEED_FRESH_S = float(os.environ.get("ROVIMEN_LIVE_FEED_FRESH_S", "60"))
_LIVE_FEED_STALE_S = float(os.environ.get("ROVIMEN_LIVE_FEED_STALE_S", "300"))
_LIVE_FEED_BUDGET_S = float(os.environ.get("ROVIMEN_LIVE_FEED_BUDGET_S", "20"))

# Single shared-tier key: the snapshot is one whole-fleet object, so there is
# nothing to key it by beyond the namespace.
_LIVE_FEED_SHARED_KEY = "fleet"

# Past-night Detections builds. A night outside the archive index's hot window
# is listed camera by camera over sshfs (~1.2 s each, 50 cameras): 39 s one at
# a time, which is what nginx's 30 s proxy timeout cut off (73 x 504 in the 14
# days to 2026-09-26). The listings run _NIGHT_LIST_WORKERS at a time, below
# the per-process sshfs permits (station_client._SSHFS_MAX_CONCURRENCY = 6) so
# a concurrent request is not shed, and the finished build is kept on disk
# (night_payload_store) and served at once from then on.
_NIGHT_LIST_WORKERS = int(os.environ.get("ROVIMEN_NIGHT_LIST_WORKERS", "4"))
# One gate for every build in the process: /api/detections/range builds four
# nights at once, and four builds x four listings would exceed the sshfs
# permits and get shed (shed listings read as "clip pending").
_NIGHT_LIST_GATE = threading.BoundedSemaphore(max(1, _NIGHT_LIST_WORKERS))
# How long a stale stored build is served while its rebuild runs.
_STALE_NIGHT_SERVE_S = 30.0
# Nights of all-footage payloads kept per worker (/api/footage/grid).
_FOOTAGE_CACHE_MAX = 50
# Clips still "in transit" make a night rebuild every 60 s; after this many
# days a missing clip is not coming any more, so the normal TTL applies.
_IN_TRANSIT_DAYS = 7


def _night_rms_complete(station: StationConfig, date: str) -> bool:
    """True unless a camera's archived ``state.json`` positively reports that
    RMS has not finished for this night. Defaults to True (processed) on any
    missing state / read error / SSHFS timeout, so the admin "Process night"
    button only surfaces when we positively know work is outstanding. Reads
    are timeout-guarded so a stalled storagebox mount can't pin the request;
    short-circuits on the first incomplete camera.
    """
    for cam in station.cameras:
        state_path = ARCHIVE_PATH / cam.code / date / "state.json"

        def _read() -> bool | None:
            try:
                if not state_path.is_file():
                    return None
                return bool(json.loads(state_path.read_text()).get("rms_complete"))
            except Exception:
                return None

        if _with_sshfs_timeout(_read, timeout=2.0, default=None) is False:
            return False
    return True


_UNLISTED = object()


def _rms_complete_from_listings(station: StationConfig, listings: dict) -> bool | None:
    """``_night_rms_complete`` answered from night listings already in hand
    (``cache_store._NightIndex.rms_complete``, read in the same pass as the
    chunks), or None when a camera's answer is unknown and the state.json has
    to be read after all. Same reading as ``_night_rms_complete``: a camera
    without a readable state.json counts as complete, one whose state says
    otherwise makes the station incomplete."""
    unknown = False
    for cam in station.cameras:
        ni = listings.get(cam.code.upper(), _UNLISTED)
        if ni is _UNLISTED:
            unknown = True
            continue
        if ni is None or getattr(ni, "state_chunks", None) is None:
            continue
        rc = getattr(ni, "rms_complete", None)
        if rc is False:
            return False
        if rc is None:
            unknown = True
    return None if unknown else True


# ── Detection-grid helpers (issue #313) ──────────────────────────────────────
# Pure functions kept at module scope so they are unit-testable without a live
# Flask app or station fan-out. The grid view re-shapes the cached flat
# detections list into a time-bucket x camera matrix.

def _parse_hhmm(raw: str | None) -> int | None:
    """Parse "HH:MM" (UTC) into minutes-of-day, or None if absent/invalid.

    Lenient on purpose: a bad value collapses to "no bound" rather than a 400,
    so a stale query string never blanks the grid.
    """
    if not raw:
        return None
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", raw.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 24 or mm > 59 or (hh == 24 and mm != 0):
        return None
    return hh * 60 + mm


def _grid_columns(config) -> list[dict]:
    """Ordered camera columns across the whole fleet.

    Grouped by station (host_key sorted) so cameras from the same site sit
    next to each other; each column carries enough metadata for the frontend
    to label and group without a second /api/stations round-trip.
    """
    columns: list[dict] = []
    for host_key, station in sorted(config.stations.items()):
        for cam in station.cameras:
            columns.append({
                "cam": cam.code,
                "host_key": host_key,
                "station_label": station.label,
            })
    return columns


def _meteor_minute_of_day(meteor_time: str) -> int | None:
    """UTC minute-of-day for an ISO meteor_time, or None if unparseable."""
    if not meteor_time:
        return None
    try:
        dt = datetime.fromisoformat(meteor_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return dt.hour * 60 + dt.minute


def _build_detection_grid(
    detections: list[dict],
    events: list[dict],
    columns: list[dict],
    bucket_min: int,
    time_from: int | None,
    time_to: int | None,
) -> list[dict]:
    """Re-shape the flat detections list into time-bucket rows.

    A row exists only for buckets that contain at least one detection (sparse
    rows -- a 5-min grid over a 12-hour night is 144 slots, mostly empty).
    Each row's ``cells`` is keyed by camera code. ``multi`` flags detections
    that belong to a multi-station event so the frontend can highlight rows
    where several cameras line up -- the whole point of the view.

    Rows are returned newest-first to match the rest of the Detections UI.
    """
    # Fast membership test: "is this (filename, cam) part of a 2+ station
    # event?" Pending detections carry no filename, so fall back to the
    # meteor_time — otherwise every pending row of a camera would collide on
    # (None, cam) and all inherit one event's multi flag.
    multi_set: set[tuple[str, str]] = set()
    for ev in events:
        if not ev or (ev.get("witness_count") or 0) <= 1:
            continue
        for w in ev.get("witnesses", []) or []:
            multi_set.add((w.get("filename") or w.get("meteor_time"), w.get("cam")))

    valid_cams = {c["cam"] for c in columns}
    buckets: dict[int, dict] = {}

    for det in detections:
        cam = det.get("cam")
        if cam not in valid_cams:
            continue
        minute = _meteor_minute_of_day(det.get("meteor_time"))
        if minute is None:
            continue
        if time_from is not None and minute < time_from:
            continue
        if time_to is not None and minute > time_to:
            continue

        bucket_idx = (minute // bucket_min) * bucket_min
        row = buckets.get(bucket_idx)
        if row is None:
            row = {
                "bucket_start_min": bucket_idx,
                "label": f"{bucket_idx // 60:02d}:{bucket_idx % 60:02d}",
                "cells": {},
                "total": 0,
                "station_count": 0,
                "has_multi": False,
            }
            buckets[bucket_idx] = row

        cell = {
            "host_key": det.get("host_key"),
            "cam": cam,
            "station_label": det.get("station_label"),
            "filename": det.get("filename"),
            "meteor_time": det.get("meteor_time"),
            "detection_offset_s": det.get("detection_offset_s"),
            "stack": det.get("stack"),
            "rms": det.get("rms"),
            "pending": bool(det.get("pending")),
            "no_clip": bool(det.get("no_clip")),
            "pruned": bool(det.get("pruned")),
            "reencoded": bool(det.get("reencoded")),
            "multi": (det.get("filename") or det.get("meteor_time"), cam) in multi_set,
        }
        row["cells"].setdefault(cam, []).append(cell)
        row["total"] += 1
        if cell["multi"]:
            row["has_multi"] = True

    rows = sorted(buckets.values(), key=lambda r: r["bucket_start_min"], reverse=True)
    for row in rows:
        row["station_count"] = len({
            c["host_key"]
            for cells in row["cells"].values()
            for c in cells
        })
    return rows


# ── All-footage grid helpers (issue: inspect every camera in a time window) ───
# Unlike the detection grid above, the footage grid shows *continuous* recording
# (every 20 s color chunk), not just locked meteors, so an operator can sweep
# all cameras between two times when something happened that RMS didn't trigger
# on (a reported fireball, a satellite re-entry, aurora). Continuous chunks only
# live on-station for ``video_days_to_keep`` (~2 days); older nights degrade to
# the locked clips that survive in the VPS archive (``degraded`` flag).
#
# Pure + module-scoped so they are unit-testable without a live Flask app.

# Minutes from midnight to a noon-anchored ordinal (0 = 12:00, 720 = 00:00,
# 1440-1 = just before next noon). RMS nights run noon->noon, so this is what
# makes 02:00 (morning) sort *after* 22:00 (evening) chronologically.
def _noon_anchor(minute: int) -> int:
    return (minute - 720 + 1440) % 1440


def _hhmmss_to_min(time_str: str | None) -> int | None:
    """UTC minute-of-day for an "HH:MM:SS" (or "HH:MM") string, or None."""
    if not time_str:
        return None
    m = re.match(r"^(\d{1,2}):(\d{2})", time_str.strip())
    if not m:
        return None
    hh, mm = int(m.group(1)), int(m.group(2))
    if hh > 24 or mm > 59:
        return None
    return hh * 60 + mm


def _in_footage_window(minute: int, time_from: int | None, time_to: int | None) -> bool:
    """Inclusive window test that also handles a window crossing midnight.

    When ``time_from > time_to`` (e.g. 22:00 -> 02:00) the window wraps past
    midnight, so a minute passes if it is *either* after ``from`` or before
    ``to`` -- mirroring the station API's own from/to handling.
    """
    if time_from is None and time_to is None:
        return True
    if time_from is not None and time_to is not None and time_from > time_to:
        return minute >= time_from or minute <= time_to
    if time_from is not None and minute < time_from:
        return False
    if time_to is not None and minute > time_to:
        return False
    return True


# The footage grid shows *every* clip in the window (no representative/expand),
# so the window is capped: 5 min at a 20 s segment cadence is ~15 clips/camera,
# already a dense row. Anything wider would flood the matrix.
FOOTAGE_MAX_WINDOW_MIN = 5


def _cap_footage_window(
    time_from: int | None,
    time_to: int | None,
    max_min: int = FOOTAGE_MAX_WINDOW_MIN,
) -> tuple[int | None, int | None]:
    """Clamp a [from, to] UTC-minute window to at most ``max_min`` minutes.

    Wrap-aware (a window may cross midnight). A single bound is expanded to a
    full ``max_min`` window; an empty/over-long span is clamped to ``max_min``.
    Returns the (possibly adjusted) bounds; both-None is left untouched.
    """
    if time_from is not None and time_to is not None:
        span = (time_to - time_from) % 1440
        if span == 0 or span > max_min:
            time_to = (time_from + max_min) % 1440
    elif time_from is not None:
        time_to = (time_from + max_min) % 1440
    elif time_to is not None:
        time_from = (time_to - max_min) % 1440
    return time_from, time_to


def _build_footage_grid(
    footage_by_cam: dict[str, dict],
    columns: list[dict],
    bucket_min: int,
    time_from: int | None,
    time_to: int | None,
) -> list[dict]:
    """Re-shape per-camera continuous chunks into time-bucket rows.

    Rows are chronological time buckets (earliest first, matching the
    noon->noon night), one per populated bucket. Each cell carries *every*
    chunk that camera recorded in that bucket; the frontend renders them all
    (the window is capped upstream, so a cell holds only a handful). The
    frontend transposes this into cameras-as-rows / time-as-columns.
    """
    valid_cams = {c["cam"] for c in columns}
    buckets: dict[int, dict] = {}

    for cam, info in footage_by_cam.items():
        if cam not in valid_cams:
            continue
        host_key = info.get("host_key")
        for ch in info.get("chunks", []) or []:
            minute = _hhmmss_to_min(ch.get("time"))
            if minute is None:
                continue
            if not _in_footage_window(minute, time_from, time_to):
                continue
            bucket_idx = (minute // bucket_min) * bucket_min
            row = buckets.get(bucket_idx)
            if row is None:
                row = {
                    "bucket_start_min": bucket_idx,
                    "label": f"{bucket_idx // 60:02d}:{bucket_idx % 60:02d}",
                    "cells": {},
                    "total": 0,
                    "station_count": 0,
                    "has_detection": False,
                }
                buckets[bucket_idx] = row
            row["cells"].setdefault(cam, []).append({
                "host_key": host_key,
                "filename": ch.get("filename"),
                "time": ch.get("time"),
                "stack": ch.get("stack"),
                "locked": bool(ch.get("locked")),
                "lock_type": ch.get("lock_type"),
                "meteor_time": ch.get("meteor_time"),
                "detection_offset_s": ch.get("detection_offset_s"),
                "reencoded": bool(ch.get("reencoded")),
            })
            row["total"] += 1
            if ch.get("locked"):
                row["has_detection"] = True

    rows = sorted(buckets.values(), key=lambda r: _noon_anchor(r["bucket_start_min"]))
    for row in rows:
        row["station_count"] = len({
            c["host_key"]
            for cells in row["cells"].values()
            for c in cells
        })
        # Each cell carries the full (time-sorted) chunk list; the frontend
        # renders every clip in the bucket.
        cells_out: dict[str, dict] = {}
        for cam, chunks in row["cells"].items():
            chunks.sort(key=lambda c: c.get("time") or "")
            cells_out[cam] = {
                "host_key": chunks[0]["host_key"],
                "count": len(chunks),
                "chunks": chunks,
                "has_detection": any(c.get("locked") for c in chunks),
            }
        row["cells"] = cells_out
    return rows


# ── Detection-index fast-path helpers ────────────────────────────────────────
# Pure functions at module scope so they are unit-testable without a Flask app.
# They let past-night payloads be served from detections.db + the in-memory
# archive index instead of the per-camera SSHFS fan-out (state.json +
# radiants parsing), which is what made Highlights / Detections / grid / range
# painfully slow on cold caches.

# Matches the HHMMSS chunk-start in names like RO000M_20260601_221530_color.mkv.
# Also accepts a bare-stem name (RO000M_20260601_221530.webp) so the same
# matcher resolves the indexer-pushed FF thumbnails, which carry no suffix
# between the time stamp and the extension.
_CHUNK_FNAME_TIME_RE = re.compile(r"^[A-Z0-9]+_\d{8}_(\d{6})[_.]")


def _row_brightness(row: dict) -> float:
    """Sort key: brightest (most negative magnitude) first, unknown last."""
    mag = row.get("mag_apparent")
    if mag is None:
        mag = row.get("mag_absolute")
    return mag if mag is not None else float("inf")


def _resolve_chunk_file(
    time_utc: str | None, meteor_files, suffixes: tuple[str, ...] = (".mkv", ".mp4")
) -> str | None:
    """Map a detection time onto the archived clip chunk that contains it.

    Needed for index rows predating the ``chunk_file`` column. Picks the
    chunk whose filename start-time most closely precedes the detection
    (midnight-wrap aware); gives up beyond a 1 h window so a bogus timestamp
    never attaches a meteor to an unrelated chunk. When a .mkv and its
    .mp4 sibling tie (same timestamp), the .mkv wins deterministically —
    ``meteor_files`` is a set, so iteration order alone would flip the
    emitted filename between requests.

    ``suffixes`` selects the candidate kind: chunk-stack webps carry the same
    start-time stamp as their clip, so the same matching resolves a pending
    detection's thumbnail from the stacks that travel ahead of the clip bytes.
    """
    if not time_utc or not meteor_files:
        return None
    try:
        t = datetime.fromisoformat(time_utc.replace(" ", "T")[:19])
    except ValueError:
        return None
    det_s = t.hour * 3600 + t.minute * 60 + t.second
    best_name: str | None = None
    best_delta: int | None = None
    for fname in meteor_files:
        if not fname.endswith(suffixes):
            continue
        m = _CHUNK_FNAME_TIME_RE.match(fname)
        if not m:
            continue
        ts = m.group(1)
        start_s = int(ts[:2]) * 3600 + int(ts[2:4]) * 60 + int(ts[4:6])
        delta = (det_s - start_s) % 86400
        if delta < 3600 and (
            best_delta is None
            or delta < best_delta
            or (delta == best_delta and fname.endswith(".mkv"))
        ):
            best_name, best_delta = fname, delta
    return best_name


def _chunk_from_index_row(
    row: dict,
    fname: str,
    meteor_names: set[str],
    stack_names,
    rms_fields: tuple[str, ...],
    camera_lag_s: float | None = None,
) -> dict:
    """Build one locked-chunk dict (same shape as the state.json path).

    ``camera_lag_s`` corrects the clip-name estimate of a clip the station
    has not measured yet: the camera's median (measured - estimated) offset
    (see _camera_lags)."""
    time_str = "00:00:00"
    parts = fname.split("_")
    if len(parts) >= 3 and len(parts[2]) == 6 and parts[2].isdigit():
        tt = parts[2]
        time_str = f"{tt[:2]}:{tt[2:4]}:{tt[4:6]}"
    stack_name = stack_name_for_clip(fname)
    if stack_name in (stack_names or frozenset()):
        stack_subdir = "stacks"
    elif stack_name in meteor_names:
        stack_subdir = "meteors"
    else:
        stack_subdir = None
    mt_str = row.get("time_utc")
    # The station measures where the meteor starts in this very clip
    # (clip_offset_s, clip_timing.py); the clip-name estimate is 0.5-1.5 s off.
    measured = row.get("clip_offset_s")
    if measured is not None and row.get("chunk_file") == fname:
        offset = round(float(measured), 2)
    else:
        offset = compute_detection_offset(fname, mt_str, None)
        if offset is not None and camera_lag_s is not None:
            offset = round(max(0.0, offset + camera_lag_s), 2)
    return {
        "filename": fname,
        "time": time_str,
        "stack": stack_name if stack_subdir else None,
        "stack_subdir": stack_subdir,
        "size_mb": None,
        "locked": True,
        "lock_type": "detection",
        "detection_offset_s": offset,
        "meteor_time": mt_str,
        # Anything on the Storage Box went through the re-encode pipeline
        # (the ``reencoded`` flag gates upload), so archived == re-encoded.
        "reencoded": True,
        "source": "index",
        "rms": {f: row.get(f) for f in rms_fields},
    }


def _camera_lags(rows) -> dict[str, float]:
    """Per camera, the median of (measured - clip-name estimated) offset over
    the rows the station has measured. The difference is the camera/relay
    latency plus keyframe alignment: a steady per-camera bias (2026-10-21:
    RO001R (Ferigile) +0.94 s, RO000T (Vâlcele) -0.24 s) that also holds for
    its clips not measured yet."""
    diffs: dict[str, list[float]] = {}
    for row in rows:
        measured, chunk = row.get("clip_offset_s"), row.get("chunk_file")
        if measured is None or not chunk:
            continue
        est = compute_detection_offset(chunk, row.get("time_utc"), None)
        if est is None:
            continue
        diffs.setdefault((row.get("cam") or "").upper(), []).append(float(measured) - est)
    return {cam: statistics.median(d) for cam, d in diffs.items() if d}


def _pending_stack(
    row: dict, stack_names, meteor_names: set[str], ffthumb_names=frozenset()
) -> tuple[str | None, str | None]:
    """Resolve the thumbnail for a clip-pending detection.

    The per-chunk stack webp is a few dozen KB and uploads well ahead of the
    clip bytes (stacks are the first category in every archive_upload cycle),
    so a pending marker can almost always carry its real thumbnail instead of
    a grey placeholder. Derive the stack name from the known chunk file when
    the row has one; otherwise time-match against the archived stack names.

    When no colour stack has landed, fall back to the BW FF thumbnail the
    station's detection indexer pushes seconds after the detection is indexed
    (``ffthumbs/<chunk_stem>.webp``) — the colour stack replaces it in a later
    payload once it arrives, because this fallback only fires on a stack miss.
    Returns ``(name, subdir)`` — ``(None, None)`` when nothing has landed.
    """
    stack_names = stack_names or frozenset()
    ffthumb_names = ffthumb_names or frozenset()
    fname = row.get("chunk_file")
    cand: str | None = None
    if fname and fname.endswith(".mkv"):
        sibling = fname.replace("_color.mkv", "_stack.webp")
        if sibling in stack_names or sibling in meteor_names:
            cand = sibling
    if cand is None:
        cand = _resolve_chunk_file(
            row.get("time_utc"), stack_names, suffixes=("_stack.webp",)
        )
    if cand is not None:
        return cand, ("stacks" if cand in stack_names else "meteors")
    if fname and fname.endswith("_color.mkv"):
        sibling = fname[: -len("_color.mkv")] + ".webp"
        if sibling in ffthumb_names:
            return sibling, "ffthumbs"
    ff_cand = _resolve_chunk_file(
        row.get("time_utc"), ffthumb_names, suffixes=(".webp",)
    )
    if ff_cand is not None:
        return ff_cand, "ffthumbs"
    return None, None


def _pending_chunk_from_row(
    row: dict,
    rms_fields: tuple[str, ...],
    stack: str | None = None,
    stack_subdir: str | None = None,
    no_clip: bool = False,
    pruned: bool = False,
) -> dict:
    """Build a clip-pending marker for a detection whose clip has not reached
    the archive yet.

    Push stations land the detection row (HTTP ingest) minutes-to-hours
    before ``archive_upload`` ships the clip bytes, so a row without an
    archived meteor file is usually *in transit*, not lost. Same shape as
    ``_chunk_from_index_row`` so it slots into the by_station payload, but
    with ``pending: True`` and no playable filename — the frontend renders it
    as a non-clickable "clip pending" marker. ``stack`` carries the chunk's
    already-archived stack webp (see ``_pending_stack``) so the marker still
    shows a real thumbnail.

    ``no_clip`` marks the case where "in transit" is a lie: the camera has no
    clip pipeline at all (``CameraConfig.clips`` false), so the bytes are not
    late, they are never coming. The marker is otherwise identical — the
    frontend just labels it "no video" instead of "clip pending".

    ``pruned`` marks a clip the storage-box retention janitor removed
    (``video_pruned`` in the night's state.json): it did reach the archive,
    and its stack and analysis are still there, but the video is gone for
    good, so the frontend labels it "removed" rather than "pending".
    """
    mt_str = row.get("time_utc")
    time_str = "00:00:00"
    if mt_str:
        iso = mt_str.replace(" ", "T")
        if len(iso) >= 19:
            time_str = iso[11:19]
    return {
        "filename": None,
        "pending": True,
        "no_clip": bool(no_clip),
        "pruned": bool(pruned),
        "time": time_str,
        "stack": stack,
        "stack_subdir": stack_subdir if stack else None,
        "size_mb": None,
        "locked": True,
        "lock_type": "detection",
        "detection_offset_s": None,
        "meteor_time": mt_str,
        "reencoded": False,
        "source": "index",
        "rms": {f: row.get(f) for f in rms_fields},
    }


def _mt_seconds(mt: str | None) -> float | None:
    """A meteor time (``YYYY-MM-DD[T ]HH:MM:SS[.f]``) as epoch seconds, or
    None. Lock times and RMS detection times use either separator."""
    if not mt:
        return None
    try:
        return datetime.fromisoformat(
            str(mt).replace(" ", "T").rstrip("Z")
        ).replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None


def _video_pruned(night_index, chunk_file: str | None) -> bool:
    """True when the night's state.json says the retention janitor removed
    this chunk's video (``video_pruned``)."""
    if night_index is None or not chunk_file:
        return False
    chunks = getattr(night_index, "state_chunks", None) or {}
    info = chunks.get(chunk_file)
    return isinstance(info, dict) and bool(info.get("video_pruned"))


def _archive_rms_by_time(cam: str, date: str) -> dict[str, dict]:
    """The night's archived RMS detections (``rms/*_radiants.txt``) keyed by
    ``time_utc``; ``{}`` when there are none or the Storage Box mount stalls
    past 5 s."""
    def _read() -> dict[str, dict]:
        rms_dir = ARCHIVE_PATH / cam / date / "rms"
        out: dict[str, dict] = {}
        if rms_dir.is_dir():
            for f in sorted(rms_dir.glob("*_radiants.txt")):
                for d in _parse_radiants_txt(f):
                    t = d.get("time_utc")
                    if t:
                        out[t] = d
        return out

    return _with_sshfs_timeout(_read, timeout=5.0, default={})


def _fallback_cams(stations, db_cams: set[str], has_archive_files) -> list[tuple[str, str]]:
    """Cameras that need the archive fallback in the index fast path.

    A camera with archived meteor files but ZERO detection rows for the night
    means the station's metadata push is broken (station index never built,
    schema-stale indexer) even though its byte plane works — the DB-only fast
    path would silently blank it. Returns ``(host_key, cam_code)`` pairs;
    normally empty.
    """
    out: list[tuple[str, str]] = []
    for hk, st in stations.items():
        for c in st.cameras:
            if c.code.upper() in db_cams:
                continue
            if not has_archive_files(c.code.upper()):
                continue
            out.append((hk, c.code))
    return out

# ── Dependencies from create_app ────────────────────────────────────────

@dataclass(frozen=True)
class DetectionsDeps:
    """What the routes and helpers need from ``create_app``.

    ``cache`` is the station status cache (``get_status``).
    ``detections_cache`` is the per-worker payload cache keyed on
    ``(date, allow_live)``, ``detections_ttl`` its normal lifetime;
    ``shared_detections`` and ``shared_live_feed`` are the cross-worker tiers
    and ``night_store`` the on-disk tier for past nights (see
    ``_NIGHT_LIST_WORKERS``). The remaining fields are per-app state, new for
    each registration as they were when they lived in the closure: the
    all-footage cache, the nights being rebuilt in the background, and the
    /api/live-feed snapshot with its lock and first-build event.
    """

    config: DashboardConfig
    tunnels: TunnelManager
    cache: StationCache
    archive_idx: _ArchiveIndex
    detections_cache: dict[tuple[str, bool], tuple[float, dict]]
    detections_ttl: float
    shared_detections: SharedSnapshotStore
    shared_live_feed: SharedSnapshotStore
    night_store: NightPayloadStore
    # {date: (expires_at, payload)}, capped on insert; never iterated, so
    # concurrent request threads can write it safely.
    footage_cache: BoundedCache = field(
        default_factory=lambda: BoundedCache(_FOOTAGE_CACHE_MAX))
    # (date, allow_live) pairs whose stored payload is being rebuilt.
    refreshing: set[tuple[str, bool]] = field(default_factory=set)
    refreshing_lock: threading.Lock = field(default_factory=threading.Lock)
    live_feed_lock: threading.Lock = field(default_factory=threading.Lock)
    live_feed_state: dict[str, Any] = field(
        default_factory=lambda: {"built": 0.0, "data": None, "refreshing": False})
    # Set once the first live-feed snapshot exists.
    live_feed_ready: threading.Event = field(default_factory=threading.Event)


# Set by register_detections_routes; each registration replaces it.
_DEPS: DetectionsDeps | None = None


def _deps() -> DetectionsDeps:
    if _DEPS is None:
        raise RuntimeError("detections code used before register_detections_routes")
    return _DEPS


# ── Archive readers (formerly create_app closures) ──────────────────────

def _archive_nights(cam_code: str) -> list[str]:
    """Return dates that have at least one .mkv/.mp4 in meteors/.
    Reads directly from the in-memory archive index — O(1), no SSHFS."""
    return _deps().archive_idx.nights_with_meteors(cam_code)


def _read_archive_locked_chunks(cam_code: str, date: str) -> list[dict]:
    """Return locked-chunk dicts for a (cam, date) from the VPS archive.

    Uses the uploaded state.json (same schema as the station's) to recover
    full lock metadata — lock_type, meteor_time — so the result is
    interchangeable with the station API's /api/chunks response shape.

    Empty list if no archive, or no state.json, or no locked chunks.
    """
    archive_idx = _deps().archive_idx
    state_path = ARCHIVE_PATH / cam_code / date / "state.json"

    def _read_state():
        if not state_path.exists():
            return None
        return json.loads(state_path.read_text())

    state = _with_sshfs_timeout(_read_state, timeout=5.0, default=None)
    if state is None:
        return []
    ni = archive_idx.night_files(cam_code, date)
    meteor_names = set(ni.meteor_files) if ni else set()
    stack_names = ni.stack_files if ni else frozenset()
    out: list[dict] = []
    for fname, info in (state.get("chunks") or {}).items():
        if not isinstance(info, dict):
            continue
        lock = info.get("lock")
        if not lock:
            continue
        if fname not in meteor_names:
            continue
        time_str = "00:00:00"
        parts = fname.split("_")
        if len(parts) >= 3 and len(parts[2]) == 6 and parts[2].isdigit():
            t = parts[2]
            time_str = f"{t[:2]}:{t[2:4]}:{t[4:6]}"
        stack_name = stack_name_for_clip(fname)
        if stack_name in stack_names:
            stack_subdir = "stacks"
        elif stack_name in meteor_names:
            stack_subdir = "meteors"
        else:
            stack_subdir = None
        mt_str = lock.get("meteor_time") if isinstance(lock, dict) else None
        det_time_str = lock.get("detection_time") if isinstance(lock, dict) else None
        det_offset = compute_detection_offset(fname, mt_str, det_time_str)
        out.append({
            "filename": fname,
            "time": time_str,
            "stack": stack_name if stack_subdir else None,
            "stack_subdir": stack_subdir,
            "size_mb": None,
            "locked": True,
            "lock_type": lock.get("lock_type") if isinstance(lock, dict) else "detection",
            "detection_offset_s": det_offset,
            "meteor_time": mt_str,
            "reencoded": info.get("reencoded", False),
            "source": "archive",
        })
    out.sort(key=lambda c: c["time"])
    return out

# ── Anonymous public filtering ────────────────────────────────────────
# /events is a public page (page="events"), and its JS pulls detection
# data from the routes below. For an anonymous caller those routes must
# show ONLY ``public: true`` stations — commissioning / opted-out sites
# never leak into the anon surface — while a logged-in operator keeps the
# full fleet (fleet-wide read rule). The shared ``compute_detections_payload``
# cache is deliberately keyed on the *full fleet* (never on the anon flag),
# so we filter its OUTPUT per request rather than caching an anon-specific
# payload — this mirrors the highlights route (routes/highlights.py) and
# guarantees an anon caller can never be handed a cached full-fleet body.
# The detection payloads carry no IP / cam_ip / ssh_user / host-path
# fields to begin with (only labels, camera codes, timestamps, filenames,
# RMS science metadata), so dropping non-public stations is the whole of
# the redaction here.

def _public_host_keys() -> set[str]:
    return public_host_keys(_deps().config)


def _public_cam_codes() -> set[str]:
    return public_camera_codes(_deps().config)


def _filter_payload_for_anon(payload: dict) -> dict:
    """Return a copy of a per-night detections payload with non-public
    stations/cameras stripped, for anonymous callers. A no-op (returns the
    same object) for logged-in callers."""
    if not is_anonymous():
        return payload
    pub_hosts = _public_host_keys()
    pub_cams = _public_cam_codes()

    by_station = {
        hk: data for hk, data in (payload.get("by_station") or {}).items()
        if hk in pub_hosts
    }
    detections = [
        d for d in (payload.get("detections") or [])
        if d.get("host_key") in pub_hosts
    ]
    # Re-filter each event's witnesses to public cameras and drop any
    # event that no longer spans >=2 public stations, so a non-public
    # witness can never appear (not even as an unlabelled marker) in the
    # anon events feed.
    events = []
    for ev in (payload.get("events") or []):
        witnesses = [
            w for w in (ev.get("witnesses") or [])
            if w.get("host_key") in pub_hosts
            and (w.get("cam") or "").upper() in pub_cams
        ]
        station_set = {w.get("host_key") for w in witnesses}
        if len(station_set) >= 2:
            events.append({
                **ev,
                "witnesses": witnesses,
                "witness_count": len(witnesses),
                "station_count": len(station_set),
            })
    return {
        **payload,
        "by_station": by_station,
        "detections": detections,
        "events": events,
    }


def _hidden_view(payload: dict) -> tuple[dict, Any, bool]:
    """The payload as this request lists it: detections a user hid as false
    positives left out (``hidden_detections``), or kept and marked when a
    signed-in caller asks ``show_hidden=1``. Applied after the per-night
    cache, never inside it, so a hide shows on the next request of every
    worker without invalidating anything. Returns ``(payload, snapshot,
    show_hidden)``; the snapshot's version keys any response cache."""
    snap = hidden_detections.snapshot()
    show = hidden_detections.wants_hidden(request.args.get("show_hidden"), is_anonymous())
    return hidden_detections.apply_payload(payload, snap, show), snap, show


# ── Detections fan-out ────────────────────────────────────────────────

@public_route(page="events")
def api_detections_nights():
    """Return union of all stations' capture nights, newest-first.

    Includes both (a) nights currently on-station and (b) nights retained
    only in the VPS archive. Without (b), the picker disappears nights
    within a few days as stations rotate out old MKVs.

    Public under the "events" page toggle (the Detections night picker
    needs it). Anonymous callers only union nights from ``public: true``
    stations, so a night that exists solely on a commissioning station
    never appears in the anon picker.
    """
    config = _deps().config
    nights: set[str] = set()
    anon = is_anonymous()
    pub_hosts = _public_host_keys() if anon else None

    # Station-resident nights come from what the stations pushed (chunk
    # listings, media pointers), never from a per-station /api/nights
    # call: this route runs on every Events load (#1250).
    pushed_cams = {
        cam.code for hk, st in config.stations.items()
        if pub_hosts is None or hk in pub_hosts
        for cam in st.cameras
    }
    try:
        nights.update(chunk_listings.nights(pushed_cams))
        nights.update(chunk_listings.media_pointer_nights(pushed_cams))
    except Exception:
        logger.warning("pushed night listing failed", exc_info=True)

    # Union in archive-resident nights per camera from the VPS storagebox.
    cam_codes = [
        cam.code for hk, st in config.stations.items()
        if pub_hosts is None or hk in pub_hosts
        for cam in st.cameras
    ]
    with ThreadPoolExecutor(max_workers=8) as pool:
        for ns in pool.map(_archive_nights, cam_codes):
            for n in ns or []:
                nights.add(n)

    return jsonify(_drop_future_dates(sorted(nights, reverse=True)))


# Fields surfaced from RMS detection metadata onto each locked chunk.
_RMS_META_FIELDS = (
    "shower", "mag_apparent", "mag_absolute",
    "duration_s", "angular_velocity",
    "ra_beg", "dec_beg", "ra_end", "dec_end",
    "azim_beg", "elev_beg", "azim_end", "elev_end",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "solar_lon", "num_segments", "fps",
)


def _index_by_station(date: str, listings: dict | None = None) -> dict[str, Any] | None:
    """Past-night fast path: build ``by_station`` from detections.db plus
    the archive index — zero SSHFS reads for nights in the index's hot
    window, one listing per camera (run _NIGHT_LIST_WORKERS at a time)
    for older ones. ``listings``, when given, receives every camera's
    night listing (cam -> _NightIndex | None) for the caller to reuse.

    Returns None when the query fails so the caller falls back to the
    state.json fan-out. Rows whose clip can't be resolved to an archived
    meteor file are surfaced as ``pending: True`` markers (detection row
    arrived over HTTP ingest, clip bytes still in transit) instead of
    being silently dropped — otherwise push stations look like they had
    no detections until the archive upload catches up.
    """
    config = _deps().config
    cache = _deps().cache
    archive_idx = _deps().archive_idx
    try:
        rows = detection_db.query_detections([date])
    except Exception:
        logger.warning(
            "detection_db query failed for %s; using SSHFS fallback", date
        )
        return None

    cam_to_host = {
        c.code.upper(): hk
        for hk, st in config.stations.items()
        for c in st.cameras
    }
    # Cameras that never deliver clips (see CameraConfig.clips). Their
    # detections still render, but as "no video" rather than a pending
    # marker that resolves on no timescale.
    no_clip_cams = {
        c.code.upper()
        for st in config.stations.values()
        for c in st.cameras
        if not c.clips
    }

    # Same skeleton the fan-out produces: every configured station
    # present with label/online, every camera present (empty list when
    # the night had no detections).
    result: dict[str, Any] = {}
    for hk, st in config.stations.items():
        status = cache.get_status(hk)
        result[hk] = {
            "label": st.label,
            "online": bool(status and status.get("online")),
            "cameras": {c.code: [] for c in st.cameras},
        }

    night_cache: dict[str, Any] = {} if listings is None else listings

    def _night(cam: str):
        if cam not in night_cache:
            night_cache[cam] = archive_idx.night_files(cam, date)
        return night_cache[cam]

    # Every configured camera's listing is needed (the rows below, and the
    # archive-fallback check for cameras with no rows), so fetch them up
    # front a few at a time instead of one by one as the loops reach them.
    all_cams = sorted({
        c.code.upper() for st in config.stations.values() for c in st.cameras
    })
    def _listed(cam: str):
        with _NIGHT_LIST_GATE:
            return _night(cam)

    with ThreadPoolExecutor(max_workers=max(1, _NIGHT_LIST_WORKERS)) as list_pool:
        list(list_pool.map(_listed, all_cams))

    # ni.meteor_files is a tuple, and the loops below need set membership.
    # Building that set per detection ROW made the cost rows x files: a
    # camera with a full night of chunks re-hashed every filename for every
    # one of its detections. Build it once per camera instead.
    meteor_name_cache: dict[str, set[str]] = {}

    def _meteor_names(cam: str) -> set[str]:
        names = meteor_name_cache.get(cam)
        if names is None:
            ni = _night(cam)
            names = set(ni.meteor_files) if ni else set()
            meteor_name_cache[cam] = names
        return names

    # A chunk file can hold several meteors (one DB row each); the
    # payload models one locked chunk per file — keep the brightest.
    best: dict[tuple[str, str], dict] = {}
    # Rows whose clip isn't archived (yet): keyed by (cam, chunk_file)
    # when known, else (cam, time_utc), keeping the brightest — one
    # pending marker per in-transit chunk, not one per meteor.
    pending: dict[tuple[str, str], dict] = {}
    for row in rows:
        cam = (row.get("cam") or "").upper()
        hk = cam_to_host.get(cam)
        if hk is None:
            continue  # removed / unconfigured camera
        meteor_names = _meteor_names(cam)
        fname = row.get("chunk_file")
        if not fname:
            fname = _resolve_chunk_file(row.get("time_utc"), meteor_names)
        elif fname not in meteor_names:
            # chunk_file names the raw capture chunk, but a meteor landing
            # near a chunk edge is locked and uploaded as a stitched splice
            # under a different name, leaving the raw chunk unlocked and so
            # legitimately absent from the archive. Resolve to the splice
            # instead of reporting an archived clip as still pending. Keep
            # the raw name when nothing spans the detection, so a genuinely
            # in-transit clip still dedups its pending marker by chunk.
            fname = (
                _resolve_stitched_chunk(row.get("time_utc"), meteor_names)
                or fname
            )
        if not fname or fname not in meteor_names:
            pkey = (cam, fname or (row.get("time_utc") or ""))
            prev = pending.get(pkey)
            if prev is None or _row_brightness(row) < _row_brightness(prev):
                pending[pkey] = row
            continue
        key = (cam, fname)
        prev = best.get(key)
        if prev is None or _row_brightness(row) < _row_brightness(prev):
            best[key] = row

    lags = _camera_lags(rows)
    for (cam, fname), row in best.items():
        hk = cam_to_host[cam]
        ni = _night(cam)
        meteor_names = _meteor_names(cam)
        stack_names = ni.stack_files if ni else frozenset()
        chunk = _chunk_from_index_row(
            row, fname, meteor_names, stack_names, _RMS_META_FIELDS,
            camera_lag_s=lags.get(cam),
        )
        result[hk]["cameras"].setdefault(cam, []).append(chunk)

    for (cam, _), row in pending.items():
        hk = cam_to_host[cam]
        ni = _night(cam)
        stack, stack_subdir = _pending_stack(
            row,
            ni.stack_files if ni else frozenset(),
            _meteor_names(cam),
            ffthumb_names=ni.ffthumb_files if ni else frozenset(),
        )
        chunk = _pending_chunk_from_row(
            row, _RMS_META_FIELDS, stack=stack, stack_subdir=stack_subdir,
            no_clip=cam in no_clip_cams,
            pruned=_video_pruned(ni, row.get("chunk_file")),
        )
        result[hk]["cameras"].setdefault(cam, []).append(chunk)

    # Cameras with archived clips but ZERO detection rows would be
    # silently blanked by the DB-only fast path — serve them from the
    # archive state.json instead (see _fallback_cams). Bounded: normally
    # zero cameras take this path.
    db_cams = {c for (c, _f) in best} | {c for (c, _k) in pending}

    def _has_archive_files(cam: str) -> bool:
        ni = _night(cam)
        return bool(ni and ni.meteor_files)

    for hk, cam_code in _fallback_cams(
        config.stations, db_cams, _has_archive_files
    ):
        try:
            locked = _read_archive_locked_chunks(cam_code, date)
        except Exception:
            continue
        if not locked:
            continue
        logger.warning(
            "%s/%s: %d archived clips but 0 detection rows for %s — "
            "serving archive fallback (station metadata push broken?)",
            hk, cam_code, len(locked), date,
        )

        # Merge RMS science metadata from the uploaded radiants files,
        # same as the fan-out path, so the fallback isn't second-class.
        rms_by_time = _archive_rms_by_time(cam_code, date)
        for chunk in locked:
            mt = chunk.get("meteor_time")
            src = rms_by_time.get(mt.split(".", 1)[0]) if mt else None
            if src:
                chunk["rms"] = {f: src.get(f) for f in _RMS_META_FIELDS}
        result[hk]["cameras"][cam_code] = locked

    for st_data in result.values():
        for chunks in st_data["cameras"].values():
            chunks.sort(key=lambda c: c["time"])
    return result


def _merge_pushed_locks(by_station: dict[str, Any], date: str,
                        listings: dict[str, Any]) -> None:
    """Add a "not uploaded yet" marker for every locked chunk a station
    announced (chunk listing or locked-clip pointer) that the payload does
    not hold yet.

    This is what tonight's live fan-out used to provide: RMS writes its
    FTPdetectinfo (hence the detection rows) only at dawn, but the colour
    pipeline locks a chunk on a live detection within seconds, and the
    pusher reports those locks. A lock already covered by an archived
    clip, or by a detection row within 2 s, is skipped."""
    config = _deps().config
    archive_idx = _deps().archive_idx
    try:
        locks = chunk_listings.pushed_locks(date)
    except Exception:
        logger.warning("pushed locks unavailable for %s", date, exc_info=True)
        return
    if not locks:
        return
    cam_to_host = {
        c.code.upper(): hk
        for hk, st in config.stations.items()
        for c in st.cameras
    }
    no_clip_cams = {
        c.code.upper()
        for st in config.stations.values()
        for c in st.cameras
        if not c.clips
    }
    for cam, entries in locks.items():
        cam_u = (cam or "").upper()
        hk = cam_to_host.get(cam_u)
        if hk is None or hk not in by_station:
            continue
        chunks = by_station[hk].setdefault("cameras", {}).setdefault(cam_u, [])
        known_files = {c.get("filename") for c in chunks if c.get("filename")}
        known_files |= {c.get("chunk_file") for c in chunks if c.get("chunk_file")}
        known_secs = [
            s for s in (_mt_seconds(c.get("meteor_time")) for c in chunks)
            if s is not None
        ]
        ni = listings[cam_u] if cam_u in listings else archive_idx.night_files(cam_u, date)
        meteor_names = set(ni.meteor_files) if ni else set()
        added = False
        for e in entries:
            mt = e.get("meteor_time")
            fname = e.get("filename")
            if not mt or not fname:
                continue
            if fname in known_files or fname in meteor_names:
                continue
            sec = _mt_seconds(mt)
            if sec is not None and any(abs(sec - k) <= 2.0 for k in known_secs):
                continue
            row = {"time_utc": mt, "chunk_file": fname, "cam": cam_u}
            stack, stack_subdir = _pending_stack(
                row,
                ni.stack_files if ni else frozenset(),
                meteor_names,
                ffthumb_names=ni.ffthumb_files if ni else frozenset(),
            )
            chunk = _pending_chunk_from_row(
                row, _RMS_META_FIELDS, stack=stack, stack_subdir=stack_subdir,
                no_clip=cam_u in no_clip_cams,
            )
            chunk["rms"] = None
            chunk["lock_type"] = e.get("lock_type") or "detection"
            chunk["chunk_file"] = fname
            chunk["source"] = "pushed"
            chunks.append(chunk)
            known_files.add(fname)
            if sec is not None:
                known_secs.append(sec)
            added = True
        if added:
            chunks.sort(key=lambda c: c.get("time") or "")


def _mark_upload_state(by_station: dict[str, Any], date: str) -> None:
    """Stamp every clip-pending marker with the shared upload state
    (``upload_state``): ``not_uploaded_yet`` or ``overdue``."""
    st = upload_state.upload_state(date, "clip")
    for st_data in by_station.values():
        for chunks in (st_data.get("cameras") or {}).values():
            for c in chunks:
                if c.get("pending") and not c.get("no_clip") and not c.get("pruned"):
                    c["upload_state"] = st["state"]
                    c["overdue"] = st["overdue"]


def compute_detections_payload(date: str, allow_live: bool = True) -> dict:
    """Build the per-night detections payload (cached).

    Shared by /api/detections/<date> and /api/detections/range. Tiers: this
    worker's memory, the shared Redis snapshot, then for nights outside
    the archive index's hot window the on-disk store, whose copy is served
    at once and rebuilt in the background once stale. Only a miss on all
    three builds on the request thread.

    ``allow_live`` (default True) lets the CURRENT night fan out to the
    station APIs over HTTP for freshest live data. Aggregate consumers like
    the highlights range pass ``allow_live=False`` so even today is served
    from detections.db (or bounded archive reads) — no per-camera station
    fan-out — keeping a wide range fast and off the box. Cached separately
    per flag so the live single-night view is never served a DB-only today.
    """
    detections_cache = _deps().detections_cache
    shared_detections = _deps().shared_detections
    night_store = _deps().night_store
    cache_key = (date, allow_live)
    cached = detections_cache.get(cache_key)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    # Cross-worker tier: a sibling gunicorn worker may already have paid
    # for this exact payload. Adopting its build costs a Redis GET plus a
    # JSON decode instead of the seconds-long index/fan-out below. The
    # shared key mirrors the local one — (date, allow_live), never the anon
    # flag — because this payload is always the FULL fleet and is filtered
    # per request afterwards (see _filter_payload_for_anon above).
    shared_key = f"{date}:{int(bool(allow_live))}"
    shared_hit = shared_detections.get(shared_key)
    if shared_hit is not None:
        shared_payload, _age, remaining = shared_hit
        # Pin the local copy to the shared entry's REMAINING lifetime, not
        # a fresh full TTL, so every worker expires with the shared entry
        # and the rebuild happens once rather than drifting per worker.
        detections_cache[cache_key] = (time.monotonic() + remaining, shared_payload)
        return shared_payload

    store_key = f"{date}-{int(bool(allow_live))}"
    if _storable_night(date, allow_live):
        stored = night_store.load(store_key)
        if stored is not None:
            now = time.time()
            if stored.is_fresh(now):
                ttl = stored.fresh_until - now
            else:
                # Serve the last build now and rebuild behind it; the
                # rebuild replaces this entry as soon as it lands.
                ttl = _STALE_NIGHT_SERVE_S
                _refresh_in_background(date, allow_live)
            detections_cache[cache_key] = (time.monotonic() + ttl, stored.payload)
            return stored.payload

    payload, ttl, storable = _build_detections_payload(date, allow_live)
    _commit_detections_payload(date, allow_live, payload, ttl, storable)
    return payload


def _storable_night(date: str, allow_live: bool) -> bool:
    """Past nights outside the archive index's hot window: the ones whose
    cold build lists every camera over sshfs, and which rarely change."""
    archive_idx = _deps().archive_idx
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    if date >= today:
        return False
    in_hot = getattr(archive_idx, "in_hot_window", None)
    return not (in_hot(date) if callable(in_hot) else True)


def _refresh_in_background(date: str, allow_live: bool) -> None:
    """Rebuild a stale stored night off the request thread, once per
    process at a time; the store's claim file keeps sibling workers from
    listing the same night concurrently."""
    night_store = _deps().night_store
    _refreshing = _deps().refreshing
    _refreshing_lock = _deps().refreshing_lock
    key = (date, allow_live)
    with _refreshing_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)
    store_key = f"{date}-{int(bool(allow_live))}"

    def _run() -> None:
        claimed = night_store.try_claim(store_key)
        try:
            if not claimed:
                return
            payload, ttl, storable = _build_detections_payload(date, allow_live)
            _commit_detections_payload(date, allow_live, payload, ttl, storable)
        except Exception:
            logger.warning("background rebuild of %s failed", date, exc_info=True)
        finally:
            if claimed:
                night_store.release(store_key)
            with _refreshing_lock:
                _refreshing.discard(key)

    threading.Thread(target=_run, name=f"detections-refresh-{date}", daemon=True).start()


def _commit_detections_payload(date: str, allow_live: bool, payload: dict,
                               ttl: float, storable: bool) -> None:
    detections_cache = _deps().detections_cache
    shared_detections = _deps().shared_detections
    night_store = _deps().night_store
    cache_key = (date, allow_live)
    # The cache caps itself on insert (create_app passes a BoundedCache).
    # No manual eviction: iterating it while another request thread
    # writes raised "OrderedDict mutated during iteration", and the range
    # and highlights totals lost that night.
    detections_cache[cache_key] = (time.monotonic() + ttl, payload)
    # Publish for the sibling workers so they adopt this build instead of
    # each repeating it. Fail-soft: a Redis problem here only costs the
    # per-worker rebuild we used to do unconditionally.
    shared_detections.set(f"{date}:{int(bool(allow_live))}", payload, ttl)
    if storable:
        night_store.save(f"{date}-{int(bool(allow_live))}", payload, ttl)


def _build_detections_payload(date: str, allow_live: bool) -> tuple[dict, float, bool]:
    """One uncached build: (payload, ttl, storable). ``storable`` is False
    when the build may be incomplete (archive index not ready, or a night
    listing could not reach the box), so it is not kept on disk."""
    config = _deps().config
    cache = _deps().cache
    archive_idx = _deps().archive_idx
    detections_ttl = _deps().detections_ttl
    by_station: dict[str, Any] = {}
    listings: dict[str, Any] = {}
    unavailable_before = getattr(archive_idx, "scan_unavailable", 0)

    is_tonight = allow_live and date >= datetime.now(timezone.utc).strftime("%Y%m%d")

    # Past nights covered by the detection index are served straight from
    # detections.db (single SQL query + in-memory archive index) instead
    # of the per-camera SSHFS fan-out below. ``covers_dates`` fails closed
    # on a mid-bootstrap or stale index, and any error here degrades to
    # the fan-out rather than a 500.
    # Tonight takes the same index path whatever ``covers_dates`` says:
    # the pushed detection rows plus the archive are all the VPS holds for
    # it, and the alternative is no longer a station fan-out (#1250) but
    # the slower per-camera archive walk below.
    used_index = False
    try:
        if is_tonight or detection_db.covers_dates([date]):
            idx_result = _index_by_station(date, listings)
            if idx_result is not None:
                by_station = idx_result
                used_index = True
    except Exception:
        logger.warning(
            "detection index fast path failed for %s; using archive walk",
            date, exc_info=True,
        )

    def fetch_one_camera(host_key: str, cam_code: str, online: bool) -> tuple[str, list[dict]]:
        """Fetch one camera's locked chunks + RMS detection metadata from
        the VPS archive. Never calls the station (#1250): tonight's locks
        the archive does not hold yet arrive as pushed-lock markers (see
        _merge_pushed_locks)."""
        locked: list[dict] = []
        try:
            locked = _read_archive_locked_chunks(cam_code, date)
        except Exception:
            pass
        rms_by_time = _archive_rms_by_time(cam_code, date)
        # Merge RMS metadata onto matching locked chunks (second-precision match).
        for c in locked:
            mt = c.get("meteor_time")
            if not mt:
                continue
            key = mt.split(".", 1)[0]
            src = rms_by_time.get(key)
            if src:
                c["rms"] = {f: src.get(f) for f in _RMS_META_FIELDS}
        return cam_code, locked

    def fetch_station(host_key: str, station: StationConfig) -> tuple[str, dict]:
        result: dict[str, Any] = {"label": station.label, "online": False, "cameras": {}}
        status = cache.get_status(host_key)
        online = bool(status and status.get("online"))
        result["online"] = online
        cams = list(station.cameras)
        if not cams:
            return host_key, result
        with ThreadPoolExecutor(max_workers=min(8, len(cams))) as cam_pool:
            cam_futures = [
                cam_pool.submit(fetch_one_camera, host_key, c.code, online)
                for c in cams
            ]
            for fut in as_completed(cam_futures):
                try:
                    cam_code, locked = fut.result()
                except Exception:
                    continue
                result["cameras"][cam_code] = locked
        return host_key, result

    if not used_index:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = {
                pool.submit(fetch_station, key, st): key
                for key, st in config.stations.items()
            }
            for future in as_completed(futures):
                try:
                    host_key, data = future.result()
                    by_station[host_key] = data
                except Exception:
                    pass

    recent_night = date >= (
        datetime.now(timezone.utc) - timedelta(days=_IN_TRANSIT_DAYS)
    ).strftime("%Y%m%d")
    if recent_night:
        _merge_pushed_locks(by_station, date, listings)
    _mark_upload_state(by_station, date)

    # Label every chunk with the GMN trajectory it fed, if any. Done here,
    # after assembly, because the three paths above (index fast path,
    # station fan-out, archive fallback) all converge on this structure —
    # one pass keeps a triangulated meteor described identically whichever
    # path served the night.
    for st_data in by_station.values():
        for cam_code, chunks in st_data.get("cameras", {}).items():
            for chunk in chunks:
                rms = chunk.get("rms")
                if isinstance(rms, dict):
                    gmn_data.attach_trajectory(rms, cam_code, chunk.get("meteor_time"))

    # Build flat list of all detections for correlation
    all_det: list[dict] = []
    for host_key, st_data in by_station.items():
        for cam_code, chunks in st_data.get("cameras", {}).items():
            for chunk in chunks:
                mt = chunk.get("meteor_time")
                if mt:
                    all_det.append({
                        "host_key": host_key,
                        "station_label": st_data["label"],
                        "cam": cam_code,
                        "filename": chunk["filename"],
                        "meteor_time": mt,
                        "detection_offset_s": chunk.get("detection_offset_s"),
                        "stack": chunk.get("stack"),
                        "time": chunk.get("time"),
                        "lock_type": chunk.get("lock_type"),
                        "rms": chunk.get("rms"),
                        "pending": bool(chunk.get("pending")),
                        "no_clip": bool(chunk.get("no_clip")),
                        "pruned": bool(chunk.get("pruned")),
                        # Drives the modal's "Processed" marker in place
                        # of a second Process clip run.
                        "reencoded": bool(chunk.get("reencoded")),
                        "overdue": bool(chunk.get("overdue")),
                    })

    all_det.sort(key=lambda x: x["meteor_time"])
    window = config.correlation_window_s
    events = _correlate_detections(all_det, window, station_coords(config))

    # Per-station RMS-complete flag for the admin "Process night" button.
    # This is a per-station SSHFS state.json read (~300ms each over the
    # storagebox mount), so across ~19 stations it costs seconds PER DATE
    # even on the DB index fast path. Only the single-night live view
    # renders the Process-night button, so skip the whole pass for the
    # aggregate consumers (allow_live=False: the range/Sort&Filter view and
    # highlights) and default the flag to True (processed) — that was the
    # residual per-date cost keeping a wide range past the timeout. One
    # parallel pass with guarded reads so it never serialises the
    # per-station archive latency onto the request thread.
    if allow_live:
        # The index path already holds every camera's listing, which
        # carries state.json's rms_complete; only stations it cannot
        # answer for pay the per-camera state.json reads.
        pending_hks = []
        for hk in by_station:
            if hk not in config.stations:
                continue
            known = (_rms_complete_from_listings(config.stations[hk], listings)
                     if used_index else None)
            if known is None:
                pending_hks.append(hk)
            else:
                by_station[hk]["rms_complete"] = known
        with ThreadPoolExecutor(max_workers=8) as proc_pool:
            proc_futs = {
                proc_pool.submit(_night_rms_complete, config.stations[hk], date): hk
                for hk in pending_hks
            }
            for fut in as_completed(proc_futs):
                hk = proc_futs[fut]
                try:
                    by_station[hk]["rms_complete"] = fut.result()
                except Exception:
                    by_station[hk]["rms_complete"] = True
    else:
        for hk in by_station:
            by_station[hk]["rms_complete"] = True

    payload = {
        "date": date,
        "correlation_window_s": window,
        "by_station": by_station,
        "events": events,
        "detections": all_det,
    }
    recent = date >= (
        datetime.now(timezone.utc) - timedelta(days=_IN_TRANSIT_DAYS)
    ).strftime("%Y%m%d")
    ttl = detections_ttl
    if not is_tonight and not archive_idx.ready:
        ttl = 10
    elif not is_tonight and recent and any(
        d.get("pending") and not d.get("no_clip") and not d.get("pruned")
        for d in all_det
    ):
        # A past night still has clips in transit: the detection rows are
        # indexed within seconds (HTTP ingest) but archive_upload ships the
        # clip bytes minutes-to-hours later. The archive-index refresher
        # picks up arrived clips every ~60 s, so capping the payload TTL at
        # ~60 s here lets those clips replace their "clip pending" markers
        # promptly instead of persisting for the full 30-min cache window.
        #
        # A no_clip camera is excluded: nothing is in transit for it, so
        # counting it would pin every night it appears in to the 60 s TTL
        # forever and rebuild this payload ~30x more often than needed.
        # So is a clip the retention removed, and so is any night older
        # than _IN_TRANSIT_DAYS, whose missing clips are not coming.
        ttl = 60
    storable = (
        _storable_night(date, allow_live)
        and bool(getattr(archive_idx, "ready", False))
        and getattr(archive_idx, "scan_unavailable", 0) == unavailable_before
    )
    return payload, ttl, storable


def _payload_build_stamp(date: str, allow_live: bool = True) -> float | None:
    """Identity of the CURRENT build of a cached per-night payload.

    Each rebuild stores a fresh ``time.monotonic() + ttl`` expiry, so that
    float doubles as a build stamp: same stamp means the very same payload
    content. Used to key the serialized-response cache, which must never
    outlive the payload it was rendered from. None when the entry has been
    evicted -- the caller then skips the response cache rather than risk a
    key that no longer pins a specific build.
    """
    detections_cache = _deps().detections_cache
    entry = detections_cache.get((date, allow_live))
    return entry[0] if entry else None


@public_route(page="events")
def api_detections(date: str):
    if not re.match(r"^\d{8}$", date):
        abort(400)
    payload, snap, show_hidden = _hidden_view(
        _filter_payload_for_anon(compute_detections_payload(date)))
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # A signed-in browser revalidates every time (a 304 when nothing
    # changed), so a hide by another user shows on its next load instead of
    # an hour later.
    max_age = 0 if not is_anonymous() else (60 if date >= today else 3600)
    # Memoize the serialized body. A fleet night is several MiB of JSON and
    # the payload itself is already cached, so re-encoding it on every
    # request was the bulk of the warm response time. The stamp pins the
    # key to one build of one payload, the anon flag distinguishes the
    # public projection from the full one, and the hidden-store version and
    # flag pin the hide filter.
    stamp = _payload_build_stamp(date)
    cache_key = (
        ("detections", date, stamp, is_anonymous(), snap.version, show_hidden)
        if stamp is not None else None
    )
    return _json_cached(payload, max_age=max_age, cache_key=cache_key)


@public_route(page="events")
def api_detections_grid(date: str):
    """Time-bucket x camera matrix for one night (issue #313).

    Rows are fixed-width time buckets (default 5 min); columns are the
    cameras across the whole fleet. Each cell carries the detections that
    camera locked inside that bucket, so multi-station coincidences line
    up on the same row. The heavy fan-out is shared with the per-night
    detections endpoint via ``compute_detections_payload`` -- this route
    only re-shapes the already-cached flat list, so a warm cache makes it
    cheap.

    Query params:
      bucket  -- bucket width in minutes (1-60, default 5)
      from    -- inclusive lower bound, "HH:MM" UTC (optional)
      to      -- inclusive upper bound, "HH:MM" UTC (optional)
    """
    config = _deps().config
    if not re.match(r"^\d{8}$", date):
        abort(400)
    try:
        bucket_min = int(request.args.get("bucket", "5"))
    except (TypeError, ValueError):
        abort(400)
    if bucket_min < 1 or bucket_min > 60:
        abort(400)
    time_from = _parse_hhmm(request.args.get("from"))
    time_to = _parse_hhmm(request.args.get("to"))

    # Anon callers only see public stations: filter the shared per-night
    # payload (drops non-public detections/events/witnesses) AND the fleet
    # columns, so a non-public camera never appears as a grid column or
    # cell. Logged-in callers get the full-fleet grid.
    payload, _snap, _show = _hidden_view(
        _filter_payload_for_anon(compute_detections_payload(date)))
    columns = _grid_columns(config)
    if is_anonymous():
        pub_hosts = _public_host_keys()
        columns = [c for c in columns if c.get("host_key") in pub_hosts]
    rows = _build_detection_grid(
        payload.get("detections", []) or [],
        payload.get("events", []) or [],
        columns,
        bucket_min,
        time_from,
        time_to,
    )
    result = {
        "date": date,
        "bucket_min": bucket_min,
        "columns": columns,
        "rows": rows,
        "hidden_count": payload.get("hidden_count", 0),
    }
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    # A signed-in browser revalidates every time (a 304 when nothing
    # changed), so a hide by another user shows on its next load instead of
    # an hour later.
    max_age = 0 if not is_anonymous() else (60 if date >= today else 3600)
    return _json_cached(result, max_age=max_age)


# ── All-footage fan-out (continuous chunks, not just detections) ──────

def _compute_footage_payload(date: str) -> dict:
    """Every camera's continuous chunks for a night (cached).

    Served from the chunk listings the stations push (#1250), never from a
    live /api/chunks call; cameras without a pushed listing for the night
    (older nights, stations on a bundle without the push) fall back to
    the locked clips kept in the VPS archive. ``degraded`` is true when no
    camera has a pushed listing -- the frontend then warns that only
    detections survive for that night.
    """
    config = _deps().config
    cache = _deps().cache
    footage_cache = _deps().footage_cache
    cached = footage_cache.get(date)
    if cached and time.monotonic() < cached[0]:
        return cached[1]

    footage_by_cam: dict[str, dict] = {}

    def fetch_cam(host_key: str, station: StationConfig, cam_code: str, online: bool):
        chunks: list[dict] = []
        source = "empty"
        try:
            pushed = chunk_listings.get(cam_code, date)
        except Exception:
            pushed = None
        if pushed and pushed.get("chunks"):
            chunks = pushed["chunks"]
            source = "live"
        if not chunks:
            try:
                arch = _read_archive_locked_chunks(cam_code, date)
                if arch:
                    chunks = arch
                    source = "archive"
            except Exception:
                pass
        return cam_code, host_key, station.label, chunks, source

    jobs: list[tuple[str, StationConfig, str, bool]] = []
    for host_key, station in config.stations.items():
        status = cache.get_status(host_key)
        online = bool(status and status.get("online"))
        for cam in station.cameras:
            jobs.append((host_key, station, cam.code, online))

    any_live = False
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(fetch_cam, *job) for job in jobs]
        for fut in as_completed(futures):
            try:
                cam_code, host_key, label, chunks, source = fut.result()
            except Exception:
                continue
            if source == "live":
                any_live = True
            footage_by_cam[cam_code] = {
                "host_key": host_key,
                "station_label": label,
                "chunks": chunks,
                "source": source,
            }

    payload = {
        "date": date,
        "footage_by_cam": footage_by_cam,
        "degraded": not any_live,
    }
    is_tonight = date >= datetime.now(timezone.utc).strftime("%Y%m%d")
    ttl = 30 if is_tonight else 300
    footage_cache[date] = (time.monotonic() + ttl, payload)
    return payload


@public_route(page="events")
def api_footage_grid(date: str):
    """Time-bucket x camera matrix of *all* footage for one night.

    Every continuous chunk is a cell, so an operator can inspect every
    camera between two times. The window is capped to FOOTAGE_MAX_WINDOW_MIN
    because the view shows every clip. Query params:
      bucket -- bucket width in minutes (1-60, default 1)
      from   -- inclusive lower bound, "HH:MM" UTC (optional)
      to     -- inclusive upper bound, "HH:MM" UTC (optional; wraps midnight)

    Public under the "events" page toggle (the Detections all-footage grid
    needs it). Anonymous callers only ever see columns/cells for
    ``public: true`` stations: the shared footage cache stays full-fleet, so
    we drop non-public host_keys from both the columns and the footage map
    before building the grid. The cells carry host_key/cam/filename only —
    no ip/cam_ip/host-path — so the public-station filter is the whole of
    the redaction.
    """
    config = _deps().config
    if not re.match(r"^\d{8}$", date):
        abort(400)
    try:
        bucket_min = int(request.args.get("bucket", "1"))
    except (TypeError, ValueError):
        abort(400)
    if bucket_min < 1 or bucket_min > 60:
        abort(400)
    time_from = _parse_hhmm(request.args.get("from"))
    time_to = _parse_hhmm(request.args.get("to"))
    # Defensive cap: the UI clamps too, but a raw API call must not be able
    # to request the whole night (thousands of clips) in this all-clips view.
    time_from, time_to = _cap_footage_window(time_from, time_to)

    payload = _compute_footage_payload(date)
    footage_by_cam = payload.get("footage_by_cam", {}) or {}
    columns = _grid_columns(config)
    if is_anonymous():
        pub_hosts = _public_host_keys()
        columns = [c for c in columns if c.get("host_key") in pub_hosts]
        footage_by_cam = {
            cam: data for cam, data in footage_by_cam.items()
            if data.get("host_key") in pub_hosts
        }
    rows = _build_footage_grid(
        footage_by_cam,
        columns,
        bucket_min,
        time_from,
        time_to,
    )
    result = {
        "date": date,
        "bucket_min": bucket_min,
        "max_window_min": FOOTAGE_MAX_WINDOW_MIN,
        "columns": columns,
        "rows": rows,
        "degraded": payload.get("degraded", False),
    }
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    max_age = 30 if date >= today else 300
    return _json_cached(result, max_age=max_age)


# One per-night payload per date in the window, on a public route: a
# season (92 nights) covers every shower link and the 7-night default.
_RANGE_MAX_DAYS = 92


@public_route(page="events")
def api_detections_range():
    """Flat chronological list of locked detections across a date range.

    Query params: from=YYYYMMDD, to=YYYYMMDD (inclusive), at most
    ``_RANGE_MAX_DAYS`` nights (400 with a JSON ``error`` beyond).
    Each detection inherits the per-date enrichment: when RMS metadata
    exists for that meteor_time it is in the `rms` field, otherwise null.

    Public under the "events" page toggle. Anonymous callers only ever get
    detections from ``public: true`` stations; the shared per-date payload
    cache stays full-fleet, so we drop non-public host_keys per request.
    """
    date_from = request.args.get("from", "")
    date_to = request.args.get("to", "")
    if not re.match(r"^\d{8}$", date_from) or not re.match(r"^\d{8}$", date_to):
        abort(400)
    try:
        d_from = datetime.strptime(date_from, "%Y%m%d").date()
        d_to = datetime.strptime(date_to, "%Y%m%d").date()
    except ValueError:
        abort(400)
    if d_from > d_to:
        abort(400)
    if (d_to - d_from).days + 1 > _RANGE_MAX_DAYS:
        return jsonify({
            "error": f"Pick a range of at most {_RANGE_MAX_DAYS} days.",
            "max_days": _RANGE_MAX_DAYS,
        }), 400

    dates: list[str] = []
    cur = d_from
    while cur <= d_to:
        dates.append(cur.strftime("%Y%m%d"))
        cur += timedelta(days=1)

    anon = is_anonymous()
    pub_hosts = _public_host_keys() if anon else None
    snap = hidden_detections.snapshot()
    show_hidden = hidden_detections.wants_hidden(request.args.get("show_hidden"), anon)
    hidden_count = 0

    # Compute per-date in parallel; cache hits make most calls cheap.
    # Aggregate view: serve every night (incl. today) from the index /
    # archive, never the per-camera station HTTP fan-out. A multi-night
    # range doesn't need live freshness, and the fan-out across ~19
    # stations is what pushed this route past the gunicorn timeout (504).
    # Mirrors routes/highlights.py's fetch_date.
    all_det: list[dict] = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = {
            pool.submit(compute_detections_payload, ds, allow_live=False): ds
            for ds in dates
        }
        for future in as_completed(futures):
            ds = futures[future]
            try:
                payload = future.result()
            except Exception:
                continue
            for det in payload.get("detections", []) or []:
                if pub_hosts is not None and det.get("host_key") not in pub_hosts:
                    continue
                rec = snap.match(det)
                if rec is not None:
                    hidden_count += 1
                    if not show_hidden:
                        continue
                    det = hidden_detections.mark(det, rec)
                enriched = dict(det)
                enriched["date"] = ds
                all_det.append(enriched)

    all_det.sort(key=lambda x: x.get("meteor_time", ""))
    return jsonify({
        "from": date_from,
        "to": date_to,
        "count": len(all_det),
        "hidden_count": hidden_count,
        "detections": all_det,
    })


# ── Aggregated live-feed API ──────────────────────────────────────────

# ── Live feed: stale-while-revalidate, bounded fan-out ────────────────
# The old handler walked every online station's cameras SERIALLY (two
# station calls each, 10 s + 15 s timeouts) and waited for every future,
# with no server-side cache: one unreachable-from-the-VPS station made the
# anonymous /live page 504 at nginx's 30 s on every 30 s poll, from every
# visitor. Now one snapshot is built per worker at a time, on a thread pool
# over (station, camera) pairs with a hard wall-clock budget, and requests
# are answered from the last snapshot while a refresh runs in the
# background. The full-fleet snapshot is filtered per request for
# anonymous callers, so no anon caller is ever handed a cached full body.
# The snapshot, its lock and the first-build event are per app
# (``DetectionsDeps.live_feed_*``).

def _live_feed_camera(host_key: str, cam_code: str) -> list:
    config = _deps().config
    tunnels = _deps().tunnels
    nights = station_get_raw(
        config, tunnels, host_key, f"/api/nights/{cam_code}", timeout=8,
    )
    if not nights or not isinstance(nights, list):
        return []
    latest_date = nights[0]
    raw = station_get_raw(
        config, tunnels, host_key, f"/api/chunks/{cam_code}/{latest_date}", timeout=10,
    )
    # New station API returns {"morning_done": bool, "chunks": [...]};
    # old API returns a plain list -- handle both.
    if isinstance(raw, dict) and "chunks" in raw:
        chunks = raw["chunks"]
    elif isinstance(raw, list):
        chunks = raw
    else:
        chunks = []
    for c in chunks:
        c["date"] = latest_date
    return chunks[-5:]


def _build_live_feed() -> dict[str, Any]:
    config = _deps().config
    cache = _deps().cache
    results: dict[str, Any] = {}
    jobs: list[tuple[str, str]] = []
    for key, station in config.stations.items():
        # Commissioning / camera-less stations have nothing to show —
        # without this they render as permanent "Station offline" cards
        # on the live grid (and skew the online count).
        if not (station.cameras and station.should_contact):
            continue
        status = cache.get_status(key)
        online = bool(status and status.get("online"))
        results[key] = {"label": station.label, "cameras": {}, "online": online}
        if not online:
            continue
        for cam in station.cameras:
            results[key]["cameras"][cam.code] = []
            jobs.append((key, cam.code))
    if not jobs:
        return results
    pool = ThreadPoolExecutor(max_workers=min(16, len(jobs)), thread_name_prefix="live-feed")
    futures = {pool.submit(_live_feed_camera, k, c): (k, c) for k, c in jobs}
    try:
        for future in as_completed(futures, timeout=_LIVE_FEED_BUDGET_S):
            key, cam_code = futures[future]
            try:
                results[key]["cameras"][cam_code] = future.result()
            except Exception:
                pass          # that camera stays []
    except TimeoutError:
        logger.info("live-feed: %d camera fetches still pending at the %.0fs budget",
                    sum(1 for f in futures if not f.done()), _LIVE_FEED_BUDGET_S)
    finally:
        # Do not wait for stragglers; their own timeouts end them.
        pool.shutdown(wait=False, cancel_futures=True)
    return results


def _adopt_shared_live_feed() -> dict[str, Any] | None:
    """Adopt a sibling worker's snapshot when it is still fresh.

    Called OUTSIDE ``_live_feed_lock`` — it does Redis I/O, which must
    never run under a lock that request threads contend on. Returns the
    snapshot now in local state, or None when the shared tier has nothing
    fresh enough to spare us a build.
    """
    shared_live_feed = _deps().shared_live_feed
    _live_feed_lock = _deps().live_feed_lock
    _live_feed_state = _deps().live_feed_state
    _live_feed_ready = _deps().live_feed_ready
    hit = shared_live_feed.get(_LIVE_FEED_SHARED_KEY)
    if hit is None:
        return None
    data, age, _remaining = hit
    if age >= _LIVE_FEED_FRESH_S:
        # Past FRESH: whoever built it owes a refresh, and so do we.
        return None
    with _live_feed_lock:
        local = _live_feed_state["data"]
        local_age = time.monotonic() - _live_feed_state["built"]
        if local is not None and local_age <= age:
            return local          # ours is at least as fresh; keep it
        # Back-date "built" by the shared entry's age so this snapshot
        # expires on the original build's schedule, not ours.
        _live_feed_state["data"] = data
        _live_feed_state["built"] = time.monotonic() - age
    _live_feed_ready.set()
    return data


def _refresh_live_feed() -> dict[str, Any]:
    shared_live_feed = _deps().shared_live_feed
    _live_feed_lock = _deps().live_feed_lock
    _live_feed_state = _deps().live_feed_state
    _live_feed_ready = _deps().live_feed_ready
    try:
        data = _build_live_feed()
        with _live_feed_lock:
            _live_feed_state["data"] = data
            _live_feed_state["built"] = time.monotonic()
        _live_feed_ready.set()
        # Offer the build to the sibling workers. STALE (not FRESH) is the
        # right lifetime: past FRESH they stop adopting it anyway, but
        # keeping it readable up to STALE lets a cold worker serve a
        # slightly stale fleet instead of an empty one.
        shared_live_feed.set(_LIVE_FEED_SHARED_KEY, data, _LIVE_FEED_STALE_S)
        return data
    except Exception:
        logger.exception("live-feed refresh failed")
        with _live_feed_lock:
            return _live_feed_state["data"] or {}
    finally:
        with _live_feed_lock:
            _live_feed_state["refreshing"] = False


def _live_feed_snapshot() -> dict[str, Any]:
    _live_feed_lock = _deps().live_feed_lock
    _live_feed_state = _deps().live_feed_state
    _live_feed_ready = _deps().live_feed_ready
    with _live_feed_lock:
        data = _live_feed_state["data"]
        age = time.monotonic() - _live_feed_state["built"]
        if data is not None and age < _LIVE_FEED_FRESH_S:
            return data
    # Local snapshot is cold or past FRESH. Before spending a fleet-wide
    # camera fan-out, check whether a sibling worker already built one.
    adopted = _adopt_shared_live_feed()
    if adopted is not None:
        return adopted
    # Nothing fresh to adopt: fall through to the original build/serve-stale
    # decision, re-reading state because the lock was released above.
    with _live_feed_lock:
        data = _live_feed_state["data"]
        age = time.monotonic() - _live_feed_state["built"]
        if data is not None and age < _LIVE_FEED_FRESH_S:
            return data          # a sibling's refresh landed meanwhile
        if _live_feed_state["refreshing"]:
            if data is not None:
                # A refresh is in flight: serve the stale snapshot.
                return data
            building_cold = True
        else:
            building_cold = False
            _live_feed_state["refreshing"] = True
        stale_ok = data is not None and age < _LIVE_FEED_STALE_S
    if building_cold:
        # Cold start with a build already running in another thread: wait
        # for it (bounded by the build budget) instead of telling this
        # visitor the whole fleet is offline.
        _live_feed_ready.wait(_LIVE_FEED_BUDGET_S + 5.0)
        with _live_feed_lock:
            return _live_feed_state["data"] or {}
    if stale_ok:
        threading.Thread(target=_refresh_live_feed, name="live-feed-refresh", daemon=True).start()
        return data
    return _refresh_live_feed()


def _reset_live_feed() -> None:
    """Test hook: back to the cold-start state."""
    _live_feed_lock = _deps().live_feed_lock
    _live_feed_state = _deps().live_feed_state
    _live_feed_ready = _deps().live_feed_ready
    with _live_feed_lock:
        _live_feed_state.update(built=0.0, data=None, refreshing=False)
    _live_feed_ready.clear()


@public_route(page="live")
def api_live_feed():
    """For each online station, the most recent 5 chunks per camera.

    Public under the "live" page toggle: the /live PAGE was already
    anon-eligible, but this data route stayed login-gated, so anonymous
    visitors saw only a fetch error. Anonymous callers get the same
    per-station ``public: true`` filter as the rest of the anon surface;
    logged-in users keep the full fleet.
    """
    from auth import station_is_public_for_request

    config = _deps().config
    snapshot = _live_feed_snapshot()
    results = {
        key: data for key, data in snapshot.items()
        if station_is_public_for_request(config, key)
    }
    return _json_cached(results, max_age=10)


# ── Registration ────────────────────────────────────────────────────────

def register_detections_routes(
    app: Flask,
    config: DashboardConfig,
    tunnels: TunnelManager,
    cache: StationCache,
    *,
    archive_idx: _ArchiveIndex,
    detections_cache: dict[tuple[str, bool], tuple[float, dict]],
    detections_ttl: float,
    shared_detections: SharedSnapshotStore | None = None,
    shared_live_feed: SharedSnapshotStore | None = None,
    night_store: NightPayloadStore | None = None,
) -> None:
    """Attach the detections, grid, footage, range and live-feed routes.

    Stores the dependencies (see ``DetectionsDeps``) in ``_DEPS`` for the
    module-level handlers and helpers, replacing any earlier registration's,
    then adds the URL rules. Each endpoint name is the view function's name,
    as ``@app.route`` gave it.
    """
    global _DEPS
    _DEPS = DetectionsDeps(
        config=config,
        tunnels=tunnels,
        cache=cache,
        archive_idx=archive_idx,
        detections_cache=detections_cache,
        detections_ttl=detections_ttl,
        # Shared (cross-worker) second tier behind the per-process caches.
        # Defaulting to a disabled store keeps every caller that does not pass
        # one — tests, single-worker dev — on the exact per-worker behaviour.
        shared_detections=shared_detections or SharedSnapshotStore(
            None, namespace="detections"
        ),
        shared_live_feed=shared_live_feed or SharedSnapshotStore(
            None, namespace="live_feed"
        ),
        # Disk tier for past-night payloads (see _NIGHT_LIST_WORKERS). Disabled
        # unless create_app passes one.
        night_store=night_store or NightPayloadStore(None),
    )
    app.add_url_rule("/api/detections/nights", view_func=api_detections_nights)
    app.add_url_rule("/api/detections/<date>", view_func=api_detections)
    app.add_url_rule("/api/detections/grid/<date>", view_func=api_detections_grid)
    app.add_url_rule("/api/footage/grid/<date>", view_func=api_footage_grid)
    app.add_url_rule("/api/detections/range", view_func=api_detections_range)
    app.add_url_rule("/api/live-feed", view_func=api_live_feed)
