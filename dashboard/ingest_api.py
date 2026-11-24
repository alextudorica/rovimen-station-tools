"""Reversed-HTTP push ingest API for the ROVIMEN dashboard (Phase A).

Stations POST their own telemetry to the VPS over HTTPS instead of the VPS
polling into :7779. See ``docs/reversed_http_push_design.md`` §2. This module is
the VPS-side ingest surface: a thin Flask blueprint-style set of routes mounted
in the same process as the dashboard (exactly like ``public_api.py``), sharing
``detection_db``, the in-process ``StationCache``, and ``station_state`` (the
push seq guard and media pointers) without any IPC.

Phase A is **additive**: pushed data lands in the *same* caches and tables the
existing pollers (``station_client.py``, ``index_poller.py``) write, so the
dashboard reads either source transparently. No poller is modified and no
station-side code ships in this phase.

Endpoints (all POST, all under ``/api/ingest/v1/<station>/``)::

    heartbeat    status/heartbeat  → StationCache.set_status
    vitals       vitals            → StationCache.set_vitals
    detections   detection batch   → detection_db.upsert_detections(source="push")
    media        timelapse/stack/clip pointers → station_state.media_pointers
    live_thumb   newest-FF maxpixel WebP (base64) → StationCache.set_live_thumb
    events       station event batch (uplink log lines, boots) → station_events

Auth (§2.2): ``X-Station-Key`` (or ``Authorization: Bearer``). A key authorises
exactly one ``host_key``; a request whose path ``<station>`` differs from the
key's station is 403. No/invalid key is 401. Idempotency (§2.3): every body
carries a monotonic ``seq``; ``seq <= last_seq`` for that station is 200-and-drop.
"""

from __future__ import annotations

import base64
import binascii
import zlib
import functools
import logging
import os
import re
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, g, jsonify, request
from pydantic import BaseModel, Field, ValidationError, field_validator

import blob_spool
import chunk_listings
from atomic_write import write_atomic
import cache_store
from cache_store import invalidate_rms_night
import clip_cache
import detection_db
import faststart_sibling
import fireball_store
import gmn_data
import platepar_store
import pushed_calibration
import station_events
import station_keys
import station_state as station_state_mod

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "1.0.0"

# Path-segment validation. host_key is lowercase alnum (gmn0002, gmnro10).
_STATION_RE = re.compile(r"^[a-z0-9]{1,16}$")
# Camera codes are uppercase alnum (RO000M, DE001E) — matches station_api.
_CAM_RE = re.compile(r"^[A-Z0-9]{1,16}$")

# Live-thumbnail bytes cap. A BW maxpixel WebP is a few KB, but a colour camera
# on a bright, star-dense field encodes far larger: gmnde00's DE001E sat just
# over the old 512 KB ceiling and had its live view rejected 413 52x/day — a
# dead panel on the dashboard for a perfectly healthy camera. 1 MiB clears that
# class of frame while still bounding a misbehaving station's per-POST body, and
# is far inside nginx's 500 MB client_max_body_size.
_MAX_LIVE_THUMB_BYTES = 1024 * 1024
_ALLOWED_THUMB_TYPES = {"image/webp", "image/jpeg", "image/png"}

# Per-key + per-IP rate budgets, consistent with public_api.py. Heartbeat/vitals
# fire every 30-60 s per station; detections/media are bursty on dawn-process, so
# the default is generous. A looping or compromised station is throttled in
# isolation (§7 backpressure) — it can never starve other stations.
_INGEST_RATE = "120/minute;3000/hour"
_DETECTIONS_RATE = "60/minute;1000/hour"

# Cap on a single detection batch — a runaway station can't push an unbounded
# body that pins the SQLite writer.
#
# Sized against the worst *legitimate* body, not the typical one. A station
# ships its whole 14-day hot window, so the peak is (busiest 5-camera site) x
# (14 nights of a shower month): the fleet's largest observed window is ~6k rows
# and a Perseid-peak five-camera site reaches ~10k. 20 000 leaves ~2x headroom
# over that while still bounding a misbehaving station: at ~650 bytes of JSON per
# row it is a ~13 MB body, comfortably inside nginx's 500 MB
# client_max_body_size on both tiers (gunicorn caps header size, not body).
#
# Stations that carry the batching agent never come near this — they ship
# DETECTION_BATCH_ROWS (1 000) per POST. The cap is the backstop for stations
# still running an older bundle, so it has to clear a whole window on its own.
_MAX_DETECTIONS = 20000
_MAX_MEDIA_POINTERS = 5000
# Fireball watcher batches: a block's spikes or one FR file's lines, a few kB.
_MAX_FIREBALL_EVENTS = 200
_MAX_FIREBALL_BODY = 512 * 1024
_FIREBALL_KINDS = {"fs", "fr", "platepar", "status"}

# Archive blob upload (§9, HTTPS-ingest byte plane). A station POSTs the actual
# clip/timelapse/stack bytes here; the VPS streams them onto the Storage Box
# mount. Bytes never ride an SSH connection — the station holds only its ingest
# key. A 20 s colour MKV chunk / timelapse is tens of MB; 256 MB is a generous
# ceiling that still bounds a misbehaving station's per-POST body.
_MAX_BLOB_BYTES = 256 * 1024 * 1024
_MAX_FFTHUMB_BYTES = 2 * 1024 * 1024
# Bursty on dawn-process, and the bucket is per-station (see _limiter_identity),
# so the minute component has to fit one station's whole dawn flush rather than
# an average. Measured on the VPS access log (2026-09-01/02): the flush is
# dominated by per-FF stack blobs — a 4-camera site ships ~12 000 of them a day
# — and peaks at 669 POSTs in a single minute for gmnro04 and 620 for gmnde00,
# both of which crossed the old 600/minute ceiling and 429'd (121 rejections
# over the two days). The same stations peak at ~2 500 POSTs/hour, so the hourly
# component was never the binding constraint and is left alone: it stays the
# real abuse ceiling while the minute component absorbs the burst. 1800/minute
# is ~2.7x the observed peak — room for a site with twice gmnro04's cameras.
_BLOB_RATE = "1800/minute;20000/hour"
# Free space the spool filesystem keeps after admitting a blob. The spool shares
# the VPS boot disk with Redis, SQLite and the logs; on 2026-09-22 the dawn burst
# filled it to 0 bytes free, Redis went MISCONF and heartbeats 500'd along with
# the blobs. Below the reserve, ingest answers 503 + Retry-After instead: the
# station keeps the file and resends it after the flusher has drained the spool.
_BLOB_SPOOL_MIN_FREE_BYTES = int(
    os.environ.get("ROVIMEN_BLOB_SPOOL_MIN_FREE_BYTES", str(3 * 1024**3))
)
_BLOB_SPOOL_FULL_RETRY_AFTER_S = 300
_BLOB_SUBDIRS = {"meteors", "stacks", "timelapse", "rms", "ffthumbs", "spectro"}
_BLOB_ROOT_FILES = {"state.json"}
# Filenames are station-generated: RO000E_20260709_214720_123456_color.mkv etc.
_BLOB_FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_DATE_RE = re.compile(r"^\d{8}$")


def _compact_validation_detail(exc: ValidationError, *,
                               max_errors: int = 5,
                               max_chars: int = 512) -> str:
    """Render a ValidationError WITHOUT echoing the rejected input.

    Pydantic v2 attaches the offending value to every error dict, so
    ``exc.errors()`` on an over-long detection batch carries the entire batch —
    a multi-megabyte 422 body shipped back to the station on every retry (a
    capped five-camera station was returning ~4.5 MB per POST every 5 minutes,
    around a gigabyte a day of pure error payload). Keep only what identifies
    the problem: the error type, where it happened, the message, and — for a
    length violation — the actual count against the limit.
    """
    errors = exc.errors(include_url=False)
    parts: list[str] = []
    for err in errors[:max_errors]:
        loc = ".".join(str(seg) for seg in err.get("loc", ())) or "<body>"
        piece = f"{loc}: {err.get('type')}"
        ctx = err.get("ctx") or {}
        actual, limit = ctx.get("actual_length"), ctx.get("max_length")
        if actual is not None and limit is not None:
            piece += f" ({actual} items, limit {limit})"
        msg = err.get("msg")
        if msg:
            piece += f" - {msg}"
        parts.append(piece)
    if len(errors) > max_errors:
        parts.append(f"and {len(errors) - max_errors} more")
    detail = "; ".join(parts) or "payload failed validation"
    return detail if len(detail) <= max_chars else detail[:max_chars - 3] + "..."


def _unlink_quiet(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def _safe_blob_relpath(relpath: str) -> tuple[str, str] | None:
    """Validate a blob path under ``<cam>/<date>/``. Returns ``(subdir, filename)``
    with ``subdir=''`` for a night-root file (``state.json``), or ``None`` if the
    path is unsafe. Rejects traversal (``..``), absolute/backslash paths, empty
    segments, unknown subdirs, and non-conforming filenames — so a station key
    can only ever write inside its own camera/night tree."""
    if not relpath or relpath != relpath.strip() or "\\" in relpath or relpath.startswith("/"):
        return None
    parts = relpath.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    if len(parts) == 1:
        fn = parts[0]
        if fn in _BLOB_ROOT_FILES and _BLOB_FILENAME_RE.match(fn):
            return "", fn
        return None
    if len(parts) == 2:
        subdir, fn = parts
        if subdir in _BLOB_SUBDIRS and _BLOB_FILENAME_RE.match(fn):
            return subdir, fn
    return None


_spool_full_logged_at = float("-inf")


def _spool_full_response(spool_root: Path, incoming: int) -> Response | None:
    """507 + Retry-After when admitting ``incoming`` bytes would leave the spool
    filesystem under ``_BLOB_SPOOL_MIN_FREE_BYTES``, else None. A spool root
    that does not exist yet (first blob on a fresh tier) measures its parent."""
    probe = spool_root
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return None
    if free - incoming >= _BLOB_SPOOL_MIN_FREE_BYTES:
        return None
    global _spool_full_logged_at
    now = time.monotonic()
    if now - _spool_full_logged_at >= 60.0:
        # Once a minute: a dawn burst would otherwise log every rejected POST.
        _spool_full_logged_at = now
        logger.warning(
            "blob ingest: spool %s has %d MB free (reserve %d MB); answering 507",
            spool_root, free // 1024**2, _BLOB_SPOOL_MIN_FREE_BYTES // 1024**2,
        )
    resp = jsonify({"error": "spool_full", "detail": "ingest spool is draining; retry later"})
    resp.status_code = 507
    resp.headers["Retry-After"] = str(_BLOB_SPOOL_FULL_RETRY_AFTER_S)
    return resp


def _blob_pointer(subdir: str, filename: str, cam: str, date: str) -> dict[str, Any] | None:
    """Map an arriving blob to a ``media_pointers`` row so the dashboard surfaces
    it the instant the bytes land (no SSHFS scan). Only the file kinds the
    dashboard indexes get a pointer; stacks/rms/state.json write bytes only."""
    if subdir == "meteors" and filename.endswith("_color.mkv"):
        return {"cam": cam, "date": date, "kind": "clip", "filename": filename, "locked": True}
    if subdir == "timelapse" and filename.endswith("_timelapse.mp4"):
        return {"cam": cam, "date": date, "kind": "timelapse", "filename": filename}
    if subdir == "timelapse" and filename.endswith("_night_stack.webp"):
        return {"cam": cam, "date": date, "kind": "nightstack", "filename": filename}
    return None


# ── Envelope + payload schemas (§2.4) ────────────────────────────────────


class FireballPayload(BaseModel):
    """Batch from rovimen-scripts/fireball_watch.py. Events stay loosely typed
    (the alert daemon reads the fields it knows); only the envelope shape,
    the kind and the batch size are enforced here."""

    model_config = {"extra": "ignore"}

    events: list[dict[str, Any]] = Field(max_length=_MAX_FIREBALL_EVENTS)
    watcher: str | None = None

    @field_validator("events")
    @classmethod
    def _known_kinds(cls, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for ev in events:
            if ev.get("kind") not in _FIREBALL_KINDS:
                raise ValueError(f"unknown fireball event kind: {ev.get('kind')!r}")
        return events


class Envelope(BaseModel):
    """Common wrapper for every ingest body (§2.4).

    ``payload`` is validated per-endpoint. ``seq`` drives idempotency; ``sent_at``
    is advisory (display / TTL) — ordering is by ``seq``, never wall-clock.
    """

    model_config = {"extra": "ignore"}

    seq: int = Field(ge=0)
    sent_at: str | None = None
    agent_version: str | None = None
    payload: dict[str, Any]


class HeartbeatPayload(BaseModel):
    """§2.4.1 — mirrors GET /api/status diff fields. Extra keys tolerated so a
    newer agent can add fields without a VPS bump."""

    model_config = {"extra": "allow"}

    online: bool = True
    services: dict[str, str] = Field(default_factory=dict)
    cameras: list[dict[str, Any]] = Field(default_factory=list)
    disk: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] = Field(default_factory=dict)
    extra_disks: list[dict[str, Any]] = Field(default_factory=list)
    rms_running: bool | None = None
    color_capture_running: bool | None = None
    network_name: str | None = None
    # Station-side health a push station must carry itself, since the VPS
    # never calls it: the janitor's pressure episode (the Storage-card red
    # banner), the colour-capture fallback root, the watchdog's uplink class
    # and the boot block (rovimen_pusher.boot_block). All optional: a station
    # on an older bundle sends none of them.
    disk_pressure: dict[str, Any] | None = None
    capture_fallback: dict[str, Any] | None = None
    uplink: dict[str, Any] | None = None
    boot: dict[str, Any] | None = None


# Station event batch (rovimen_pusher EventQueue). Events stay loosely typed
# and are checked one by one (station_events.check_station_event), so a
# malformed event is skipped rather than holding up the station's queue.
_MAX_STATION_EVENTS = 200


class StationEventsPayload(BaseModel):
    model_config = {"extra": "ignore"}

    events: list[Any] = Field(default_factory=list, max_length=_MAX_STATION_EVENTS)


class VitalsPayload(BaseModel):
    """§2.4.2 — mirrors GET /api/vitals."""

    model_config = {"extra": "allow"}

    cpu_pct: float | None = None
    ram_pct: float | None = None
    ram_used_mb: float | None = None
    ram_total_mb: float | None = None
    temp_c: float | None = None
    total_cores: int | None = None
    cores: list[dict[str, Any]] = Field(default_factory=list)
    disk_write_mbps: float | None = None
    top_procs: list[dict[str, Any]] = Field(default_factory=list)


class DetectionRow(BaseModel):
    """§2.4.3 — one detection. Columns match detection_db.DETECTION_COLS so
    ingest is a pass-through to upsert_detections. ``source`` is set by the
    handler, not the station."""

    model_config = {"extra": "ignore"}

    cam: str
    date: str
    ff_file: str
    meteor_no: int = 1
    time_utc: str | None = None
    jd: float | None = None
    solar_lon: float | None = None
    shower: str | None = None
    mag_apparent: float | None = None
    mag_absolute: float | None = None
    duration_s: float | None = None
    ra_beg: float | None = None
    dec_beg: float | None = None
    ra_end: float | None = None
    dec_end: float | None = None
    ra_radiant: float | None = None
    dec_radiant: float | None = None
    radiant_elev: float | None = None
    angular_velocity: float | None = None
    num_segments: int | None = None
    fps: float | None = None
    azim_beg: float | None = None
    elev_beg: float | None = None
    azim_end: float | None = None
    elev_end: float | None = None
    chunk_file: str | None = None
    # The meteor's first RMS pixel and the measured time from chunk_file's
    # first frame to the meteor's first frame (station clip_timing).
    x_beg: float | None = None
    y_beg: float | None = None
    clip_offset_s: float | None = None


class NightSkyRow(BaseModel):
    """§2.4.3 — one per-night sky-quality summary. Columns match
    detection_db.upsert_night_sky so ingest is a pass-through."""

    model_config = {"extra": "ignore"}

    cam: str
    night: str
    n_ff: int | None = None
    median_stars: float | None = None
    cloud_fraction: float | None = None
    updated: str | None = None


class DetectionsPayload(BaseModel):
    model_config = {"extra": "ignore"}

    window_since: str | None = None
    detections: list[DetectionRow] = Field(default_factory=list, max_length=_MAX_DETECTIONS)
    # Per-night sky quality rides the detections envelope (first batch only).
    # Bounded like detections so a malformed station can't blow the writer up.
    night_sky: list[NightSkyRow] = Field(default_factory=list, max_length=_MAX_DETECTIONS)


class TimelapsePointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str
    night_stack: str | None = None


class NightstackPointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str


class ClipPointer(BaseModel):
    model_config = {"extra": "allow"}

    cam: str
    date: str
    filename: str
    locked: bool | None = None
    meteor_time: str | None = None


class MediaPayload(BaseModel):
    model_config = {"extra": "ignore"}

    timelapses: list[TimelapsePointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)
    nightstacks: list[NightstackPointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)
    clips: list[ClipPointer] = Field(default_factory=list, max_length=_MAX_MEDIA_POINTERS)


# ── Chunk listing (#1250) ─────────────────────────────────────────────
# One camera-night's chunk listing in the compact format documented in
# chunk_listings.py. A 20 s segment night is ~2,000 chunks; the cap leaves
# room for 10 s segments without letting one body grow unbounded.
_MAX_LISTING_CHUNKS = 10_000


_STAMP_RE = re.compile(r"^\d{8}_\d{6}$")
_CLOCK_RE = re.compile(r"^\d{2}:\d{2}:\d{2}(\.\d{1,6})?$")
# meteor_time is an ISO-8601 instant; only its characters are allowed, since
# the value is rendered on the videodb and Events pages.
_INSTANT_RE = re.compile(r"^[0-9][0-9T:.+\-Z ]{0,39}$")
_LOCK_TYPES = {"detection", "manual"}


def _is_number(v: Any) -> bool:
    return v is None or (isinstance(v, (int, float)) and not isinstance(v, bool))


class ChunkListingPayload(BaseModel):
    """Every element is checked here, the one path that writes a listing:
    chunk entries are ``[stamp, size_mb]`` or ``[stamp, size_mb, "HH:MM:SS"]``,
    locks map a stamp to ``[lock_type, meteor_time, detection_offset_s]``."""

    model_config = {"extra": "ignore"}

    cam: str = Field(max_length=16)
    date: str = Field(max_length=8)
    morning_done: bool = False
    chunks: list[list[Any]] = Field(default_factory=list, max_length=_MAX_LISTING_CHUNKS)
    locks: dict[str, list[Any]] = Field(default_factory=dict)
    reencoded: list[str] = Field(default_factory=list, max_length=_MAX_LISTING_CHUNKS)

    @field_validator("chunks")
    @classmethod
    def _valid_chunks(cls, v: list[list[Any]]) -> list[list[Any]]:
        for e in v:
            if not 2 <= len(e) <= 3:
                raise ValueError("a chunk entry has 2 or 3 elements")
            if not isinstance(e[0], str) or not _STAMP_RE.match(e[0]):
                raise ValueError("chunk stamp must be YYYYMMDD_HHMMSS")
            if not _is_number(e[1]):
                raise ValueError("size_mb must be a number or null")
            if len(e) == 3 and e[2] is not None and not (
                    isinstance(e[2], str) and _CLOCK_RE.match(e[2])):
                raise ValueError("chunk time must be HH:MM:SS")
        return v

    @field_validator("locks")
    @classmethod
    def _valid_locks(cls, v: dict[str, list[Any]]) -> dict[str, list[Any]]:
        if len(v) > _MAX_LISTING_CHUNKS:
            raise ValueError(f"at most {_MAX_LISTING_CHUNKS} locks")
        for stamp, lock in v.items():
            if not _STAMP_RE.match(stamp):
                raise ValueError("lock key must be YYYYMMDD_HHMMSS")
            if not 1 <= len(lock) <= 3 or lock[0] not in _LOCK_TYPES:
                raise ValueError(f"lock must be [{'|'.join(sorted(_LOCK_TYPES))}, meteor_time, offset]")
            if len(lock) > 1 and lock[1] is not None and not (
                    isinstance(lock[1], str) and _INSTANT_RE.match(lock[1])):
                raise ValueError("meteor_time must be an ISO timestamp")
            if len(lock) > 2 and not _is_number(lock[2]):
                raise ValueError("detection_offset_s must be a number or null")
        return v

    @field_validator("reencoded")
    @classmethod
    def _valid_reencoded(cls, v: list[str]) -> list[str]:
        if any(not _STAMP_RE.match(s) for s in v):
            raise ValueError("reencoded entries must be YYYYMMDD_HHMMSS")
        return v


# A mask is a camera-sized BMP (1280x720 8-bit is ~0.9 MB raw, 2560x1440 24-bit
# ~11 MB), almost all one value, so it gzips to a few kB. The per-mask and
# per-push caps fit that and stop a decompression bomb from holding hundreds
# of MB in one request.
_MAX_MASK_GZ_BYTES = 512 * 1024
_MAX_MASK_RAW_BYTES = 12 * 1024 * 1024
_MAX_MASKS_TOTAL_RAW_BYTES = 48 * 1024 * 1024
_MAX_CALIBRATION_CAMS = 16


class MaskBlob(BaseModel):
    model_config = {"extra": "ignore"}

    encoding: str = "gzip+base64"
    data_b64: str


class CalibrationPayload(BaseModel):
    """#1248 — what the dashboard used to pull from :7779 for the overview map,
    Events and the Calibration page. A section left out (None) keeps what the
    VPS already holds; ``masks`` is the station's complete set when present."""

    model_config = {"extra": "ignore"}

    platepar: dict[str, dict[str, Any]] | None = None
    calibration_summary: dict[str, dict[str, Any]] | None = None
    hardware: dict[str, Any] | None = None
    masks: dict[str, MaskBlob] | None = None

    @field_validator("platepar", "calibration_summary", "masks")
    @classmethod
    def _bounded_cams(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        if len(v) > _MAX_CALIBRATION_CAMS:
            raise ValueError(f"at most {_MAX_CALIBRATION_CAMS} cameras")
        for cam in v:
            if not _CAM_RE.match(cam):
                raise ValueError(f"invalid camera code {cam!r}")
        return v


def _decode_mask(blob: MaskBlob, limit: int = _MAX_MASK_RAW_BYTES) -> bytes | None:
    """Raw mask bytes from a pushed blob, or None if it is malformed or over
    ``limit`` (at most the per-mask cap)."""
    limit = min(limit, _MAX_MASK_RAW_BYTES)
    if blob.encoding != "gzip+base64":
        return None
    try:
        packed = base64.b64decode(blob.data_b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not packed or len(packed) > _MAX_MASK_GZ_BYTES:
        return None
    try:
        d = zlib.decompressobj(16 + zlib.MAX_WBITS)
        raw = d.decompress(packed, limit + 1)
    except zlib.error:
        return None
    if not raw or len(raw) > limit or d.unconsumed_tail:
        return None
    return raw


_MAX_LIVE_FRAME_BYTES = 4 * 1024 * 1024


class LiveFramePayload(BaseModel):
    """#1249 — the newest colour chunk stack (or RMS captured stack) for one
    camera, downscaled on the station; input to the VPS sky-dome render."""

    model_config = {"extra": "ignore"}

    content_type: str = "image/webp"
    frame_time: str | None = Field(default=None, max_length=40)
    data_b64: str


class LiveThumbPayload(BaseModel):
    """§9 — a single newest-FF BW maxpixel WebP, base64 in the envelope.

    The image is tiny and ephemeral (newest FF only, no archival value), so
    unlike archival media (bytes→box + pointer→VPS) the bytes ride the JSON body
    directly and are cached latest-per-(station,cam) in memory on the VPS."""

    model_config = {"extra": "ignore"}

    content_type: str = "image/webp"
    ff_timestamp: str | None = None
    data_b64: str


def _limiter_identity(
    station_key_id: str | None,
    url_station: str | None,
    client_ip: str,
) -> str:
    """Resolve the rate-limit bucket identity for an ingest request.

    flask-limiter evaluates the ``key_func`` BEFORE the view body runs, so the
    validated key that ``_authorize`` attaches to ``g`` is not available yet
    (``station_key_id`` is normally None here). Keying on ``client_ip`` used
    to collapse the ENTIRE fleet into one shared bucket per endpoint, because
    it read ``127.0.0.1`` behind the local reverse proxy, so a single busy
    station 429'd everyone (the fleet-wide ``live_thumb`` throttling). The
    ``<station>`` host_key from the URL is available at limit-eval time and
    gives each station its own bucket. The caller sends requests whose key is
    provably not that station's to ``_ANON_BUCKET`` instead.
    """
    if station_key_id is not None:
        return f"station:{station_key_id}"
    if url_station:
        return f"station:{url_station}"
    return client_ip


# Limiter bucket for ingest requests whose key is not valid for the URL's
# station (missing, wrong, disabled, another station's): one per endpoint,
# shared, so junk POSTs to a station's URL never spend that station's budget.
_ANON_BUCKET = "ingest-anon"


# ── Registration ─────────────────────────────────────────────────────────


def register_ingest_routes(
    app: Flask,
    *,
    cache_set_status: Callable[[str, dict[str, Any]], None],
    cache_set_vitals: Callable[[str, dict[str, Any]], None],
    cache_set_live_thumb: Callable[..., None] | None = None,
    known_stations: Callable[[], set[str]] | None = None,
    cam_owners: Callable[[str], set[str]] | None = None,
    detections_db_path: Any = None,
    station_state_path: Any = None,
    station_keys_path: Any = None,
    archive_media_path: Any = None,
    blob_spool_dir: Any = None,
    station_events_path: Any = None,
    limiter: Any = None,
) -> None:
    """Mount the ingest API onto an existing Flask app.

    Args:
        app: The Flask application to attach routes to.
        cache_set_status: ``StationCache.set_status`` — the *same* writer the
            status poll thread uses, so a pushed heartbeat fans out over the
            existing SSE channel unchanged (coexistence).
        cache_set_vitals: ``StationCache.set_vitals``.
        cache_set_live_thumb: ``StationCache.set_live_thumb`` — stores the latest
            pushed newest-FF maxpixel WebP per (station, cam) in memory (§9).
            When None, the live_thumb ingest route is not mounted (tests may pass
            a stub); the on-demand :7779 pull remains the fallback either way.
        known_stations: Optional callable returning the set of valid host_keys
            (from ``dashboard_config.yaml``). When provided, a POST to an
            unknown station 404s before touching the key store. When None, any
            path that passes the regex is accepted (tests / standalone).
        cam_owners: Optional callable mapping an uppercase camera code to the
            set of host_keys that own it (from ``dashboard_config.yaml`` via
            ``auth._host_keys_for_camera``). When provided, a station may only
            write bytes/pointers for cameras it owns: a camera KNOWN to belong
            to a *different* station is 403. Cameras absent from the config
            (freshly added / commissioning, or before a VPS config sync) are
            allowed — fail-open on the unknown so a new camera is never blocked
            before its registry entry lands. When None, no ownership gate is
            applied (tests / standalone).
        detections_db_path / station_state_path / station_keys_path: path
            overrides for the respective stores (tests). Default to each
            module's configured path.
        limiter: Flask-Limiter instance from ``security.init_limiter``. When
            None (tests / standalone), rate limiting is a no-op.
    """
    det_path = detections_db_path if detections_db_path is not None else detection_db.DB_PATH
    st_path = station_state_path if station_state_path is not None else station_state_mod.DB_PATH
    keys_path = station_keys_path if station_keys_path is not None else station_keys.DEFAULT_KEYS_PATH
    # None -> station_events.db_path() at call time (env-resolved).
    events_path = Path(station_events_path) if station_events_path is not None else None
    # Root of the Storage Box mount the dashboard also serves /media/v1/* from.
    # When None (tests that don't exercise blobs), the blob route is not mounted.
    blob_root = Path(archive_media_path) if archive_media_path is not None else None

    # Local-disk spool + background flusher for pushed blobs. The request path
    # writes the blob to NVMe and returns immediately; the flusher moves the
    # bytes onto the sshfs archive off-thread, so a stalled Storage Box can never
    # block an ingest request thread (and, via the shared gthread pool, freeze
    # the public site). See dashboard/blob_spool.py.
    blob_spooler: blob_spool.BlobSpool | None = None
    if blob_root is not None:
        # Dedicated flush lane, NOT the shared read guard: otherwise a read
        # burst starves the flusher and its files livelock in the spool (see
        # station_client.with_sshfs_flush_timeout).
        from station_client import with_sshfs_flush_timeout

        def _register_pointer_on_flush(rel: Path, _local: Path) -> None:
            """Register the media pointer only once the blob is durably on the
            archive. Invariant: a pointer never precedes its bytes, so a serve
            request can't 404 during the flush window (the pre-spool code wrote
            the pointer synchronously after the archive write; spooling would
            otherwise surface a pointer to a not-yet-flushed blob). ``rel`` is
            station-prefixed: ``<station>/<cam>/<date>/<subdir?>/<filename>``."""
            parts = rel.parts
            if len(parts) < 4:
                return
            station, cam, date = parts[0], parts[1], parts[2]
            subdir = parts[3] if len(parts) >= 5 else ""
            filename = parts[-1]
            if subdir == "rms":
                # The archive now holds a new version under the same name.
                invalidate_rms_night(cam, date, filename)
            ptr = _blob_pointer(subdir, filename, cam, date)
            if ptr is None:
                return
            try:
                station_state_mod.upsert_media_pointers(station, [ptr], path=st_path)
            except Exception:
                logger.exception("blob pointer upsert failed for %s", rel)

        blob_spooler = blob_spool.BlobSpool(
            blob_root,
            spool_root=Path(blob_spool_dir) if blob_spool_dir is not None else None,
            sshfs_guard=with_sshfs_flush_timeout,
            on_flushed=_register_pointer_on_flush,
            # Stations upload a clip's .mkv only; its faststart .mp4 sibling
            # is derived here, so each bitstream crosses the uplink once.
            before_flush=lambda archive_rel, claimed: faststart_sibling.derive(
                archive_rel, claimed, claimed.parent),
            route_prefix_parts=1,  # spool layout is <station>/<cam>/<date>/...
        )
        # Exposed so tests can drive flush_once() deterministically.
        app._blob_spooler = blob_spooler  # type: ignore[attr-defined]
        # Gate on the same env the dashboard uses to keep tests / the E2E
        # launcher from spawning the flusher thread.
        if os.environ.get("ROVIMEN_DISABLE_BACKGROUND", "0") != "1":
            blob_spooler.start()

    # Ensure both schemas exist so a pushed batch never hits a missing table on
    # a fresh install. In production the index poller / bootstrap already create
    # the detections table; this is idempotent and just guarantees ordering.
    try:
        detection_db.open_db(det_path).close()
    except Exception:
        logger.exception("ingest_api: could not initialise detections DB at %s", det_path)
    try:
        station_state_mod.ensure_schema(st_path)
    except Exception:
        logger.exception("ingest_api: could not initialise station_state DB at %s", st_path)

    def _request_secret() -> str:
        return (
            request.headers.get("X-Station-Key")
            or request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
            or ""
        ).strip()

    def _may_hold_station_key(station: str) -> bool:
        """False only when a sha256 lookup proves the request's key is not a
        valid key for ``station``. True in soft mode, while ``station``'s row
        has no sha256 yet (only the slow check in ``_authorize`` can tell),
        and if the check itself fails: those requests keep today's bucket."""
        if not station_keys.is_required():
            return True
        try:
            verdict = station_keys.authorizes_fast(_request_secret(), station, path=keys_path)
        except Exception:
            logger.exception("ingest limiter: key check failed for %s", station)
            return True
        return verdict is not False

    def _limiter_key() -> str:
        """Rate-limit identity: the URL's ``<station>`` when the request may
        carry that station's key (see :func:`_limiter_identity`), else the
        shared :data:`_ANON_BUCKET`. Valid station traffic keeps exactly the
        bucket it had before the key check moved in here."""
        from security import _client_ip

        key = getattr(g, "station_key", None)
        url_station = (request.view_args or {}).get("station")
        if key is None and url_station and not _may_hold_station_key(url_station):
            return _ANON_BUCKET
        return _limiter_identity(
            key.id if key is not None else None, url_station, _client_ip()
        )

    def _rate_limited(rate: str) -> Callable:
        if limiter is None:
            return lambda fn: fn

        def _effective_limit() -> str:
            key = getattr(g, "station_key", None)
            if key is not None and key.rate_limit_override:
                return key.rate_limit_override
            return rate

        return limiter.limit(_effective_limit, key_func=_limiter_key)

    def _err(code: int, error: str, detail: str) -> Response:
        resp = jsonify({"error": error, "detail": detail})
        resp.status_code = code
        return resp

    def _authorize(station: str) -> Response | None:
        """Auth gate for an ingest route (§2.2).

        Returns an error Response to short-circuit, or None on success (having
        attached the matching :class:`station_keys.StationKey` to ``g`` for the
        rate limiter). ``?key=`` in the query string is rejected outright — it
        leaks into access logs / referers, same rule as the public API.
        """
        if "key" in request.args:
            return _err(
                400, "url_key_param_disabled",
                "station keys must be passed via the X-Station-Key header or "
                "Authorization: Bearer; the ?key= URL parameter is not accepted.",
            )
        secret = _request_secret()

        if not station_keys.is_required():
            # Soft-mode (dev/test): attach the key if supplied so per-key rate
            # limiting still works, but do not reject.
            key = station_keys.validate(secret, path=keys_path) if secret else None
            if key is not None:
                g.station_key = key
            return None

        key = station_keys.validate(secret, path=keys_path) if secret else None
        if key is None:
            resp = _err(
                401, "missing_or_invalid_station_key",
                "supply your station key via the X-Station-Key header or "
                "Authorization: Bearer <key>",
            )
            resp.headers["WWW-Authenticate"] = 'StationKey realm="rovimen-ingest"'
            return resp
        g.station_key = key
        if key.station != station:
            # Authenticated but wrong station: a valid key can only write its
            # own host_key's state. This is the core §2.2 property.
            return _err(
                403, "station_mismatch",
                f"this key authorises {key.station!r}, not {station!r}",
            )
        return None

    def _validate_station(station: str) -> Response | None:
        if not _STATION_RE.match(station):
            return _err(404, "unknown_station", f"invalid station id: {station!r}")
        if known_stations is not None and station not in known_stations():
            return _err(404, "unknown_station", f"no such station: {station!r}")
        return None

    def _authorize_cam(station: str, cam: str) -> Response | None:
        """Reject a cross-tenant camera write (§2.2 blast-radius containment).

        ``_authorize`` proves the caller holds ``station``'s key, but the blob
        tree and media pointers are keyed by *camera code*, not station — so
        without this an authenticated station could write/overwrite bytes for
        any other operator's camera (all served publicly under
        ``/media/v1/<cam>/…``). Bind the arriving camera to its config owner.

        Fail-open on an *unknown* camera: a code absent from the registry
        (freshly added / commissioning, or before a VPS config sync) is
        allowed, so a new camera isn't blocked before its entry lands — while a
        camera KNOWN to belong to a different station is refused.
        """
        if cam_owners is None:
            return None
        owners = cam_owners(cam.upper())
        if owners and station not in owners:
            return _err(
                403, "cam_not_owned",
                f"camera {cam!r} is not owned by station {station!r}",
            )
        return None

    def _parse_envelope() -> tuple[Envelope | None, Response | None]:
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, _err(422, "invalid_body", "body must be a JSON object")
        try:
            env = Envelope.model_validate(body)
        except ValidationError as exc:
            return None, _err(422, "invalid_envelope", _compact_validation_detail(exc))
        return env, None

    def _accepted(seq: int, *, dropped: bool = False, **extra: Any) -> Response:
        resp = jsonify({"ok": True, "seq": seq, "dropped": dropped, **extra})
        resp.status_code = 200
        return resp

    # ── Detections-index freshness feed ───────────────────────────────
    # Push stations are skipped by index_poller, so poll_state.last_polled —
    # which detection_db.covers_dates() reads as its liveness gate — would
    # freeze as the fleet converges on push, silently disabling the index
    # fast path for every past-night query. Any accepted push is the
    # push-era equivalent of a successful poll: it proves station → ingest
    # → DB works end to end. Rate-limited per station so the 30 s
    # heartbeat/live cadences don't become a SQLite write apiece.
    touch_min_interval_s = 60.0
    last_touch: dict[str, float] = {}
    touch_lock = threading.Lock()

    def _touch_freshness(station: str) -> None:
        import time as _time

        now = _time.time()
        with touch_lock:
            if now - last_touch.get(station, 0.0) < touch_min_interval_s:
                return
            last_touch[station] = now
        try:
            detection_db.touch_poll_state(station, path=det_path)
        except sqlite3.OperationalError as exc:
            # A contended writer lock on the *freshness stamp* must never cost
            # the station its push. The stamp only feeds covers_dates(), which
            # tolerates 2x the poll interval of staleness, and the next accepted
            # push refreshes it — whereas failing here would throw away a
            # detection batch that is already durably written. Logged at warning
            # without a traceback: under a lock storm this fires per push and the
            # stack tells nobody anything the message does not.
            logger.warning(
                "ingest: poll_state freshness stamp for %s skipped (%s); "
                "the push itself was accepted", station, exc,
            )
        except Exception:
            logger.exception("ingest: could not update poll_state for %s", station)

    # ── JSON 429 for the ingest surface ───────────────────────────────
    @app.errorhandler(429)
    def _ingest_429(exc):  # noqa: ANN001
        path = request.path or ""
        if not path.startswith("/api/ingest/v1/"):
            return exc.get_response()
        retry_after = 60
        try:
            reset_at = getattr(exc, "reset_at", None) or getattr(
                getattr(exc, "limit", None), "reset_at", None
            )
            if reset_at:
                import time as _time

                retry_after = max(1, int(reset_at - _time.time()))
        except Exception:
            pass
        resp = jsonify({
            "error": "rate_limit_exceeded",
            "detail": str(getattr(exc, "description", "Too many requests")),
            "retry_after_seconds": retry_after,
        })
        resp.status_code = 429
        resp.headers["Retry-After"] = str(retry_after)
        return resp

    # ── Index (unauthenticated discovery) ─────────────────────────────
    @app.route("/api/ingest/v1", methods=["GET"])
    @app.route("/api/ingest/v1/", methods=["GET"])
    def ingest_index():
        return jsonify({
            "service": "rovimen-ingest-api",
            "schema_version": SCHEMA_VERSION,
            "endpoints": {
                "heartbeat":  "/api/ingest/v1/<station>/heartbeat",
                "vitals":     "/api/ingest/v1/<station>/vitals",
                "detections": "/api/ingest/v1/<station>/detections",
                "media":      "/api/ingest/v1/<station>/media",
                "live_thumb": "/api/ingest/v1/<station>/live_thumb/<cam>",
                "blob":       "/api/ingest/v1/<station>/blob/<cam>/<date>/<relpath>",
                "trajectories": "/api/ingest/v1/<station>/trajectories/<cam>/<date>",
                "fireball":   "/api/ingest/v1/<station>/fireball",
                "chunks":     "/api/ingest/v1/<station>/chunks",
                "calibration": "/api/ingest/v1/<station>/calibration",
                "live_frame": "/api/ingest/v1/<station>/live_frame/<cam>",
                "events":     "/api/ingest/v1/<station>/events",
            },
            "auth": "X-Station-Key header (or Authorization: Bearer)",
        })

    # ── Trajectories (the one read on this surface) ───────────────────
    # Everything else here is station → VPS. This is the return leg: GMN's
    # multi-station solutions live on the VPS (gmn_data's monthly cache) and a
    # station has no route to globalmeteornetwork.org of its own, so the
    # overlay-details pass asks for the night it is re-rendering. It stays a
    # station-initiated pull — the VPS never reaches into a station (#880) —
    # and it is authenticated and camera-scoped like every write beside it.
    @app.route("/api/ingest/v1/<station>/trajectories/<cam>/<date>", methods=["GET"])
    @_rate_limited(_INGEST_RATE)
    def ingest_trajectories(station: str, cam: str, date: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        if not _CAM_RE.match(cam):
            return _err(422, "invalid_cam", f"invalid camera code: {cam!r}")
        if not _DATE_RE.match(date):
            return _err(422, "invalid_date", f"date must be YYYYMMDD: {date!r}")
        bad = _authorize_cam(station, cam)
        if bad is not None:
            return bad
        try:
            rows = gmn_data.trajectories_for_camera_night(cam.upper(), date)
        except Exception:
            # An unpublished night and a broken cache both mean "no better
            # numbers than the station already has" — the caller keeps its
            # single-station values either way, so never 500 over it.
            logger.warning("trajectory lookup failed for %s/%s", cam, date, exc_info=True)
            rows = []
        return jsonify({
            "camera": cam.upper(),
            "date": date,
            "count": len(rows),
            "trajectories": rows,
        })

    # ── Heartbeat / status ────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/heartbeat", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_heartbeat(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = HeartbeatPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))

        # Atomic seq check-and-bump (§2.3). On the Redis path this is a single
        # Lua op — the per-push seq write never touches the single-writer SQLite
        # DB. Returns False for a replay/older seq, which we 200-and-drop.
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")

        status = payload.model_dump()
        # Stamp last_seen so downstream (staleness badge, public API) has it.
        if env.sent_at:
            status.setdefault("last_updated", env.sent_at)
        # Server-clock receipt stamp: the staleness gate that demotes a dead
        # push station to offline (station_client._demote_stale_push) measures
        # age against THIS, not the station-stamped sent_at, so a station with
        # a skewed clock can't be presented as offline while still pushing.
        status["received_at"] = datetime.now(timezone.utc).isoformat()
        # Same writer the poll thread uses — preserves the SSE diff/fan-out.
        cache_set_status(station, status)
        _touch_freshness(station)
        try:
            # Presence history: an "online" event if the VPS had marked this
            # station offline. Never costs the station its heartbeat.
            station_events.note_heartbeat(station, path=events_path)
        except Exception:
            logger.exception("ingest: presence update failed for %s", station)
        return _accepted(env.seq)

    # ── Station events (append-only history) ──────────────────────────
    # Not seq-gated: the station's event queue redelivers after an outage
    # with fresh envelopes, and each event is idempotent by its own id.
    # Trust model (station_events module doc): rows are written under the
    # host_key _authorize proved the key holds (the path segment, which must
    # equal key.station), with source "station"; nothing in the payload names
    # a station or a source, and VPS-only kinds are refused per event.
    @app.route("/api/ingest/v1/<station>/events", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_station_events(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = StationEventsPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))
        valid = []
        rejected: list[dict[str, Any]] = []
        for index, raw in enumerate(payload.events):
            ev, reason = station_events.check_station_event(raw)
            if ev is None:
                event_id = raw.get("id") if isinstance(raw, dict) else None
                rejected.append({
                    "index": index,
                    "id": event_id if isinstance(event_id, str) else None,
                    "status": 422,
                    "error": reason,
                })
            else:
                valid.append(ev)
        try:
            inserted, duplicates, over_quota = station_events.append_events(
                station, valid, path=events_path)
        except Exception:
            logger.exception("ingest: could not store events for %s", station)
            return _err(503, "events_unavailable", "event store busy; retry later")
        for event_id in over_quota:
            # Logged by append_events. Reported per event; the station drops
            # them, since a resend tomorrow would only crowd that day.
            rejected.append({"index": None, "id": event_id, "status": 429,
                             "error": "daily_quota_exceeded"})
        if rejected:
            reasons: dict[str, int] = {}
            for r in rejected:
                reasons[r["error"]] = reasons.get(r["error"], 0) + 1
            logger.warning("ingest: %s sent %d rejected events of %d: %s",
                           station, len(rejected), len(payload.events),
                           ", ".join(f"{k}={v}" for k, v in sorted(reasons.items())))
        return _accepted(env.seq, inserted=inserted, duplicates=duplicates,
                         rejected=len(rejected), rejected_events=rejected[:50])

    # ── Vitals ─────────────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/vitals", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_vitals(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = VitalsPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))

        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")

        vitals = payload.model_dump()
        cache_set_vitals(station, vitals)
        _touch_freshness(station)
        return _accepted(env.seq)

    # ── Detections ─────────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/detections", methods=["POST"])
    @_rate_limited(_DETECTIONS_RATE)
    def ingest_detections(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = DetectionsPayload.model_validate(env.payload)
        except ValidationError as exc:
            detail = _compact_validation_detail(exc)
            raw = env.payload.get("detections") if isinstance(env.payload, dict) else None
            n_rows = len(raw) if isinstance(raw, list) else 0
            if n_rows > _MAX_DETECTIONS:
                # This is silent data loss, not a bad request: the station is
                # healthy and heartbeating, but its whole detection window is
                # being dropped on the floor. Make it findable in the journal.
                logger.warning(
                    "ingest: DROPPED detections batch from station %s — %d rows "
                    "exceeds the per-POST cap of %d; this station's detection "
                    "index on the VPS is now stale and will stay stale until it "
                    "ships batches under the cap",
                    station, n_rows, _MAX_DETECTIONS,
                )
            else:
                logger.warning(
                    "ingest: rejected detections batch from station %s "
                    "(%d rows): %s", station, n_rows, detail,
                )
            return _err(422, "invalid_payload", detail)

        # Detections are PK-idempotent (cam,date,ff_file,meteor_no) regardless of
        # seq, so a stale-seq replay is still safe — but honour the envelope for
        # consistency and to spare the writer a no-op batch. Atomic decide+bump.
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay", written=0)

        rows = [d.model_dump() for d in payload.detections]
        written = detection_db.upsert_detections(rows, source="push", path=det_path)
        if payload.night_sky:
            try:
                detection_db.upsert_night_sky(
                    [s.model_dump() for s in payload.night_sky], path=det_path,
                )
            except Exception:
                logger.warning(
                    "ingest: night_sky upsert failed for station %s", station,
                    exc_info=True,
                )
        _touch_freshness(station)
        return _accepted(env.seq, written=written)

    # ── Media pointers ─────────────────────────────────────────────────
    @app.route("/api/ingest/v1/<station>/media", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_media(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = MediaPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))

        # A pointer's cam is station-supplied; reject the batch if any names a
        # camera owned by another station (same cross-tenant class as blobs).
        for grp in (payload.timelapses, payload.nightstacks, payload.clips):
            for p in grp:
                bad = _authorize_cam(station, p.cam)
                if bad is not None:
                    return bad

        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay", written=0)

        pointers: list[dict[str, Any]] = []
        for tl in payload.timelapses:
            pointers.append({"kind": "timelapse", **tl.model_dump()})
        for ns in payload.nightstacks:
            pointers.append({"kind": "nightstack", **ns.model_dump()})
        for clip in payload.clips:
            pointers.append({"kind": "clip", **clip.model_dump()})

        written = station_state_mod.upsert_media_pointers(station, pointers, path=st_path)
        _touch_freshness(station)
        return _accepted(env.seq, written=written)

    # ── Chunk listings (#1250) ─────────────────────────────────────────
    # A camera-night's chunk listing, so tonight's videodb/footage views and
    # night pickers read VPS-local state instead of the station's /api/chunks.
    # Last-value per (cam, date); seq-guarded like every ingest body.
    @app.route("/api/ingest/v1/<station>/chunks", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_chunks(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = ChunkListingPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))
        if not _CAM_RE.match(payload.cam):
            return _err(422, "invalid_cam", f"invalid camera code: {payload.cam!r}")
        if not _DATE_RE.match(payload.date):
            return _err(422, "invalid_date", f"date must be YYYYMMDD: {payload.date!r}")
        bad = _authorize_cam(station, payload.cam)
        if bad is not None:
            return bad
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")
        chunk_listings.store(station, payload.model_dump(), path=st_path)
        _touch_freshness(station)
        return _accepted(env.seq, chunks=len(payload.chunks))

    # ── Calibration state (platepar, mask, summary, hardware) ──────────
    # #1248: the dashboard used to pull these from :7779 on every overview-map,
    # Events and Calibration page load. The station now pushes them on change
    # and the dashboard serves pushed_calibration's copy.
    @app.route("/api/ingest/v1/<station>/calibration", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_calibration(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = CalibrationPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))

        cams: set[str] = set()
        for section in (payload.platepar, payload.calibration_summary, payload.masks):
            cams.update(section or {})
        for cam in sorted(cams):
            bad = _authorize_cam(station, cam)
            if bad is not None:
                return bad

        masks: dict[str, bytes] | None = None
        if payload.masks is not None:
            masks = {}
            budget = _MAX_MASKS_TOTAL_RAW_BYTES
            for cam, blob in payload.masks.items():
                raw = _decode_mask(blob, limit=budget)
                if raw is not None:
                    budget -= len(raw)
                if raw is None:
                    return _err(422, "invalid_mask", f"mask for {cam} is not valid gzip+base64 within limits")
                masks[cam] = raw

        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")

        try:
            pushed_calibration.save(
                station,
                platepar=payload.platepar,
                calibration_summary=payload.calibration_summary,
                hardware=payload.hardware,
                masks=masks,
                sent_at=env.sent_at,
            )
        except OSError as exc:
            logger.error("calibration store write failed for %s: %s", station, exc)
            return _err(500, "write_failed", "could not store calibration state")
        if payload.platepar:
            # Coverage, the network map and the public API read platepar_store;
            # feed it here so it stays current without anyone loading a page.
            try:
                platepar_store.update_host(payload.platepar)
            except Exception as exc:  # never fail an accepted push on this
                logger.warning("platepar_store update from push failed for %s: %s", station, exc)
        _touch_freshness(station)
        return _accepted(env.seq, cams=len(cams), masks=len(masks or {}))

    # ── Live frame (sky-dome input, #1249) ─────────────────────────────
    @app.route("/api/ingest/v1/<station>/live_frame/<cam>", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_live_frame(station: str, cam: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        if not _CAM_RE.match(cam):
            return _err(404, "invalid_cam", f"invalid camera code: {cam!r}")
        bad = _authorize_cam(station, cam)
        if bad is not None:
            return bad
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = LiveFramePayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))
        if payload.content_type not in _ALLOWED_THUMB_TYPES:
            return _err(422, "invalid_content_type",
                        f"content_type must be one of {sorted(_ALLOWED_THUMB_TYPES)}")
        try:
            raw = base64.b64decode(payload.data_b64, validate=True)
        except (binascii.Error, ValueError):
            return _err(422, "invalid_base64", "data_b64 is not valid base64")
        if not raw:
            return _err(422, "empty_frame", "decoded frame is empty")
        if len(raw) > _MAX_LIVE_FRAME_BYTES:
            return _err(413, "frame_too_large", f"frame exceeds {_MAX_LIVE_FRAME_BYTES} bytes")
        if not station_state_mod.accept_seq(station, env.seq, path=st_path):
            return _accepted(env.seq, dropped=True, reason="replay")
        try:
            pushed_calibration.save_live_frame(
                station, cam, raw, payload.content_type, payload.frame_time)
        except (OSError, ValueError) as exc:
            logger.error("live frame write failed for %s/%s: %s", station, cam, exc)
            return _err(500, "write_failed", "could not store live frame")
        _touch_freshness(station)
        return _accepted(env.seq, bytes=len(raw))

    # ── Fireball watcher events ────────────────────────────────────────
    # Small JSON events from rovimen-scripts/fireball_watch.py (fieldsum
    # spikes, FR line tracks, platepars, watcher status), appended to
    # fireball_store for the alert daemon (fireball_alert.py). Not seq-gated:
    # the watcher is a separate process from the pusher, so it cannot share
    # the station's seq counter; every event carries its own id and the
    # daemon keeps the latest state per id.
    @app.route("/api/ingest/v1/<station>/fireball", methods=["POST"])
    @_rate_limited(_INGEST_RATE)
    def ingest_fireball(station: str):
        bad = _validate_station(station) or _authorize(station)
        if bad is not None:
            return bad
        if (request.content_length or 0) > _MAX_FIREBALL_BODY:
            return _err(413, "body_too_large", f"fireball batch over {_MAX_FIREBALL_BODY} bytes")
        env, err = _parse_envelope()
        if err is not None:
            return err
        try:
            payload = FireballPayload.model_validate(env.payload)
        except ValidationError as exc:
            return _err(422, "invalid_payload", _compact_validation_detail(exc))
        for ev in payload.events:
            cam = ev.get("cam")
            if cam is not None:
                if not isinstance(cam, str) or not _CAM_RE.match(cam):
                    return _err(422, "invalid_cam", f"invalid camera code: {cam!r}")
                bad = _authorize_cam(station, cam)
                if bad is not None:
                    return bad
        try:
            written = fireball_store.insert(station, payload.events)
        except Exception:
            logger.exception("fireball ingest: store write failed for %s", station)
            return _err(503, "store_unavailable", "fireball store write failed; retry")
        return _accepted(env.seq, written=written)

    # ── Live thumbnail (newest-FF maxpixel WebP) ───────────────────────
    if cache_set_live_thumb is not None:

        @app.route("/api/ingest/v1/<station>/live_thumb/<cam>", methods=["POST"])
        @_rate_limited(_INGEST_RATE)
        def ingest_live_thumb(station: str, cam: str):
            bad = _validate_station(station) or _authorize(station)
            if bad is not None:
                return bad
            if not _CAM_RE.match(cam):
                return _err(404, "invalid_cam", f"invalid camera code: {cam!r}")
            bad = _authorize_cam(station, cam)
            if bad is not None:
                return bad
            env, err = _parse_envelope()
            if err is not None:
                return err
            try:
                payload = LiveThumbPayload.model_validate(env.payload)
            except ValidationError as exc:
                return _err(422, "invalid_payload", _compact_validation_detail(exc))

            if payload.content_type not in _ALLOWED_THUMB_TYPES:
                return _err(
                    422, "invalid_content_type",
                    f"content_type must be one of {sorted(_ALLOWED_THUMB_TYPES)}",
                )
            try:
                raw = base64.b64decode(payload.data_b64, validate=True)
            except (binascii.Error, ValueError):
                return _err(422, "invalid_base64", "data_b64 is not valid base64")
            if not raw:
                return _err(422, "empty_thumb", "decoded thumbnail is empty")
            if len(raw) > _MAX_LIVE_THUMB_BYTES:
                return _err(
                    413, "thumb_too_large",
                    f"thumbnail exceeds {_MAX_LIVE_THUMB_BYTES} bytes",
                )

            # Seq-guarded like every ingest body (§2.3): a stale/replayed thumb is
            # 200-and-dropped so an out-of-order POST can't overwrite a newer one.
            # Atomic decide+bump so a race can't let a stale frame win.
            if not station_state_mod.accept_seq(station, env.seq, path=st_path):
                return _accepted(env.seq, dropped=True, reason="replay")

            cache_set_live_thumb(
                station, cam, raw,
                content_type=payload.content_type,
                ff_timestamp=payload.ff_timestamp,
            )
            _touch_freshness(station)
            return _accepted(env.seq, bytes=len(raw))

    # ── Archive blob (clip / timelapse / stack / rms / state bytes) ─────
    # The HTTPS-ingest byte plane (§9): the station streams archival file bytes
    # here with its ingest key; the VPS writes them onto the Storage Box mount.
    # This replaces the station→VPS rsync-over-SSH path, so a station holds NO
    # SSH key — the only station→VPS channel is this authenticated POST.
    if blob_root is not None:

        def _store_ffthumb(station: str, cam: str, date: str, filename: str):
            """A BW FF thumbnail goes straight to the VPS disk (cache_store.FFTHUMB_DIR),
            where the thumbnail route serves it, and never through the blob spool:
            one Storage Box write per ~1.7 KB file is what the spool cannot keep up
            with at one thumbnail per chunk per camera."""
            data = request.stream.read(_MAX_FFTHUMB_BYTES + 1)
            if len(data) > _MAX_FFTHUMB_BYTES:
                return _err(413, "blob_too_large", f"ffthumb exceeds {_MAX_FFTHUMB_BYTES} bytes")
            if not data:
                return _err(422, "empty_blob", "no bytes received")
            dest = cache_store.ffthumb_local_path(cam, date, filename)
            try:
                write_atomic(
                    dest, data, mkdir=True,
                    tmp=dest.with_name(f".{filename}.part-{os.getpid()}-{threading.get_ident()}"),
                )
            except OSError as exc:
                logger.error("ffthumb write failed for %s: %s", dest, exc)
                return _err(500, "write_failed", "could not store ffthumb")
            _touch_freshness(station)
            return jsonify({"ok": True, "bytes": len(data), "spooled": False,
                            "path": f"{cam}/{date}/ffthumbs/{filename}"})

        @app.route(
            "/api/ingest/v1/<station>/blob/<cam>/<date>/<path:relpath>",
            methods=["POST"],
        )
        @_rate_limited(_BLOB_RATE)
        def ingest_blob(station: str, cam: str, date: str, relpath: str):
            bad = _validate_station(station) or _authorize(station)
            if bad is not None:
                return bad
            if not _CAM_RE.match(cam):
                return _err(404, "invalid_cam", f"invalid camera code: {cam!r}")
            bad = _authorize_cam(station, cam)
            if bad is not None:
                return bad
            if not _DATE_RE.match(date):
                return _err(422, "invalid_date", f"date must be YYYYMMDD: {date!r}")
            safe = _safe_blob_relpath(relpath)
            if safe is None:
                return _err(422, "invalid_path", f"unsafe or unsupported blob path: {relpath!r}")
            subdir, filename = safe

            clen = request.content_length
            if clen is not None and clen > _MAX_BLOB_BYTES:
                return _err(413, "blob_too_large", f"body exceeds {_MAX_BLOB_BYTES} bytes")

            if subdir == "ffthumbs":
                return _store_ffthumb(station, cam, date, filename)

            spool_full = _spool_full_response(blob_spooler.spool_root, clen or 0)
            if spool_full is not None:
                return spool_full

            # Write to the LOCAL spool, not the sshfs archive. The background
            # flusher (blob_spool.BlobSpool) moves it onto the Storage Box
            # off-thread, so a stalled box can't block this request thread.
            rel = f"{cam}/{date}/{subdir}/{filename}" if subdir else f"{cam}/{date}/{filename}"
            # Spool under a per-station prefix so the flusher can attribute the
            # media pointer to the pushing station (the archive path drops it).
            spool_final = blob_spooler.spool_path_for(f"{station}/{rel}")
            target_dir = spool_final.parent

            # A re-POST of a blob still pending in the spool replaces it below
            # (os.replace). No size-equality shortcut: a recalibrated
            # FTPdetectinfo is a same-size rewrite, and skipping it answered
            # 200 for content the archive never received.
            try:
                target_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.error("blob spool mkdir failed for %s: %s", target_dir, exc)
                return _err(500, "spool_unwritable", "could not create spool directory")

            tmp = target_dir / f".{filename}.part-{os.getpid()}-{threading.get_ident()}"

            # Tee meteors/*.mp4 into the NVMe hot clip cache while the bytes
            # stream through: the first dashboard click on a fresh clip is
            # then an nginx X-Accel hit — no SSHFS read-back, no remux, no
            # wait for the 15-min warmer tick. Best-effort only: a cache
            # write failure never fails the ingest (the archive is truth).
            hot: Path | None = None
            hot_tmp: Path | None = None
            hot_fh = None
            if subdir == "meteors" and filename.endswith(".mp4"):
                hot = clip_cache.hot_mp4_path(cam, date, filename)
            if hot is not None and not hot.exists():
                try:
                    hot.parent.mkdir(parents=True, exist_ok=True)
                    hot_tmp = hot.with_name(
                        f".{hot.name}.part-{os.getpid()}-{threading.get_ident()}"
                    )
                    hot_fh = open(hot_tmp, "wb")
                except OSError:
                    hot_fh = None
                    hot_tmp = None

            def _hot_abort() -> None:
                nonlocal hot_fh
                if hot_fh is not None:
                    try:
                        hot_fh.close()
                    except Exception:
                        pass
                    hot_fh = None
                if hot_tmp is not None:
                    _unlink_quiet(hot_tmp)

            written = 0
            too_large = False
            try:
                stream = request.stream
                with open(tmp, "wb") as fh:
                    while True:
                        chunk = stream.read(256 * 1024)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > _MAX_BLOB_BYTES:
                            too_large = True
                            break
                        fh.write(chunk)
                        if hot_fh is not None:
                            try:
                                hot_fh.write(chunk)
                            except OSError:
                                _hot_abort()
            except OSError as exc:
                _unlink_quiet(tmp)
                _hot_abort()
                logger.error("blob spool write failed for %s: %s", spool_final, exc)
                return _err(500, "write_failed", "could not write blob to spool")

            if too_large:
                _unlink_quiet(tmp)
                _hot_abort()
                return _err(413, "blob_too_large", f"body exceeds {_MAX_BLOB_BYTES} bytes")
            if written == 0:
                _unlink_quiet(tmp)
                _hot_abort()
                return _err(422, "empty_blob", "no bytes received")

            try:
                os.replace(tmp, spool_final)
            except OSError as exc:
                _unlink_quiet(tmp)
                _hot_abort()
                logger.error("blob spool commit failed for %s: %s", spool_final, exc)
                return _err(500, "commit_failed", "could not commit blob to spool")

            # Commit the hot-cache copy now that the blob is durably spooled: the
            # first dashboard click on a fresh clip is an X-Accel hit even before
            # the flusher has moved it to the archive.
            if hot_fh is not None:
                try:
                    hot_fh.close()
                    hot_fh = None
                    os.replace(hot_tmp, hot)
                except OSError:
                    _hot_abort()

            # Hand off to the background flusher. The sshfs archive write AND the
            # media-pointer upsert (_register_pointer_on_flush) happen off the
            # request thread, so a pointer never precedes its archived bytes and
            # a serve request can't 404 in the flush window.
            blob_spooler.enqueue()

            _touch_freshness(station)
            return jsonify(
                {"ok": True, "bytes": written, "spooled": True,
                 "path": f"{cam}/{date}/{relpath}"}
            )
