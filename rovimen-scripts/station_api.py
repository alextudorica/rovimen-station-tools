#!/usr/bin/env python3
"""
station_api.py — Station-side REST API

Run as a systemd service on each station (port 7779, the RMS venv's python).
Read-only from the network: a non-GET request runs only from loopback unless
its route is allowlisted (see the guard below).

The routes fall into four groups; the full table is pinned by
EXPECTED_ROUTES in tests/test_station_api_surface.py.

  Polled status  /api/status, /api/vitals, /api/nights, /api/chunks,
                 /api/timelapses, /api/rms_cameras, /api/live_cameras,
                 /api/latest_ff_maxpixel, /api/detections-index. What they
                 compute lives in station_collect.py (Flask-free, also
                 imported by the pusher); these are thin routes over it.
  Media          /color_capture, /color_timelapse, /night_stack, /thumbnail,
                 shortclip and the two stitch routes, /api/stream (MJPEG).
                 Serving a colour chunk first burns in the overlay.
  Operator       lock, process_clip, encode_chunk, probe, overlay_preview,
                 the calibration routes, the mask write and the overlay logo
                 choice (network allowlist); PATCH /api/settings (loopback, config_patch.py).
  Status pages   settings, logs, crons, dawn/progress, rms/status,
                 updater/status, encoding/stats, storagewatch, rms-detections,
                 fireball_pack, and the routes #1248-#1250 will retire.
"""

from __future__ import annotations

import base64
import binascii
import io
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, abort, jsonify, request, send_file, stream_with_context

import archive_upload
import config_patch
import ff_thumbs
import rovimen_config
import rovimen_lock
import rovimen_night
import rovimen_rms
import overlay
# The collectors live in station_collect (Flask-free, so the pusher can call
# them in-process). Re-exported: the routes below, other station scripts and
# the tests use these names through station_api.
from station_collect import (  # noqa: F401
    BASE, CONFIG_PATH, CHUNK_RE, _bundle_status, _system_zoneinfo, _chunk_utc_dt,
    _detection_offset_s, _cache, _cache_lock, _cache_inflight, cached, cached_swr,
    load_config, capture_base, _rms_camera_dirs, _du, _get_device, _STORAGE_TTL,
    _scan_storage, _night_date, _NIGHT_ROLLOVER_HOUR, _last_completed_night,
    _safe_iterdir, _safe_is_dir, _safe_is_file, _date_range, SEGMENT_HEALTH_WINDOW_S,
    SEGMENT_SHORT_MAX_S, SEGMENT_STALL_MIN_S, _SEGMENT_HEALTH_TTL, _segment_name_ts,
    segment_health, _segment_health_night_dir, _segment_health_for_camera,
    _collect_segment_health, _io_prev, _io_lock, _proc_cpu_prev, _proc_cpu_ts,
    _proc_cpu_result, _proc_cpu_result_ts, _proc_cpu_lock, _PROC_CACHE_TTL,
    _top_procs_by_cpu, _disk_write_mbps, _VITALS_TTL, _cpu_primed, _cpu_prime_lock,
    _cpu_percent_per_core, collect_vitals, SVC_ACTIVE, SVC_UNMANAGED, SVC_FAILED,
    SVC_INACTIVE, _ROVIMEN_SERVICES, _CAPTURE_CMD_RE, _RMS_UNIT_FAMILIES,
    _RMS_UNIT_FAMILIES_FIRST, _ACTIVE_RANK, _ENABLED_RANK, _UNIT_ACTIVE_OK,
    _UNIT_OFF_BY_CHOICE, _UnitInfo, _systemctl_lines, _merge_unit_rows, _unit_inventory,
    _service_unit_candidates, _resolve_unit, _proc_cwd, _capture_marker,
    _capture_process_markers, _rms_service_running, _service_state, _rms_service_names,
    _process_running, _collect_service_states, _SESSION_RE, _FTP_LINE_RE,
    _final_ftpdetectinfos, _rms_latest_night_summary, _INPROGRESS_GRACE_S,
    _segment_in_progress, list_chunks, _rms_config_paths, _parse_rms_config,
    _rms_config_entries, _ff_maxpixel_to_webp, _mtime_ns, _cam_sessions_newest_first,
    _newest_ff_in_session, _find_latest_ff, _FFRenderFailed, _latest_ff_webp_bytes,
    _newest_colour_chunks, _FRAME_STABLE_BYTES, _colour_frame_webp_bytes,
    _latest_colour_frame, _INDEX_DB, _INDEX_COLS, _NIGHT_SKY_COLS, _read_night_sky,
    collect_status, list_nights, list_timelapses, list_rms_cameras, list_live_cameras,
    read_detections_index,
)
try:
    import flags_manager as _flags_manager
    _STATE_MANAGER_OK = True
except ImportError:
    _STATE_MANAGER_OK = False

app = Flask(__name__)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Network read-only guard
# ---------------------------------------------------------------------------
# The station API is read-only from the network. A request that is not a
# GET/HEAD/OPTIONS may only come from loopback (local scripts, and operators
# over SSH running curl against localhost) unless its route is on the explicit
# allowlist below. Remote mutation is otherwise gone: the signed command
# channel was removed 2026-07, interventions are SSH-only and fleet-wide
# changes ship as signed bundles (docs/signed_bundles.md).
#
# The guard is fail-closed and unconditional. Until 2026-09-26 it only ran
# when ROVIMEN_STATION_READONLY=1, which a hand-applied systemd drop-in set in
# the July fleet round; nothing in the repo, the updater or the onboarding
# procedure ever set it, so a station installed afterwards answered reboot,
# PATCH /api/settings and updater/run from any tailnet peer allowed to reach
# :7779. The variable is no longer read: no environment can weaken the guard,
# and the old drop-ins are harmless. A new POST/PATCH route is blocked from
# the network until it is added to the allowlist on purpose, and
# tests/test_station_readonly_guard.py audits every route in the URL map.

# Loopback source addresses that are always permitted to mutate.
_LOOPBACK_ADDRS = frozenset({"127.0.0.1", "::1", "::ffff:127.0.0.1"})

# Methods that never change station state; never guarded.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Mutating routes (Flask view names) the dashboard may call over the network.
# View names rather than URL prefixes, so a rename fails loud in the audit
# test instead of silently changing what is reachable.
#
# api_lock: the one-way clip lock. Locking only preserves data (it pins the
# clip and kicks an upload), so ``{"locked": true}`` (the default for an empty
# body) is allowed; unlock deletes the archive copy and stays loopback-only
# (see _network_mutation_allowed).
#
# api_process_clip / api_encode_chunk: generate and preserve data for ONE
# chunk (lock if unlocked + re-encode + archive upload, or re-encode only);
# nothing is deleted.
#
# api_probe / api_overlay_preview: diagnostics. The probe re-runs
# hardware_assessment.sh and rewrites hardware.json; the preview renders a
# PNG into a temp file, from the body's display keys only (_preview_overrides).
#
# api_calibration_autofit / api_calibration_rebase_pointing: only ever
# produce a STAGED candidate, never touch the live platepar (see
# platepar_autofit.run_autofit).
#
# api_calibration_autofit_accept: this DOES mutate the live platepar, so
# (PR #884 review) it is not benign by default. What makes it safe to leave
# network-reachable is that platepar_autofit.accept_candidate() requires an
# accept_token minted per run and only obtainable by whoever triggered that
# run and read back its result; a stale or guessed candidate_id is not
# enough. It does not defend against a fully compromised VPS process, which
# could complete the same trigger-poll-accept sequence itself; closing that
# would mean SSH-only accept, which kills the review-and-accept dashboard UX
# this feature exists for.
#
# api_mask_write: reversible like the clip lock. It writes one config-derived
# path, keeps the displaced mask as a timestamped .bak beside it and changes
# only which part of the frame RMS searches; nothing is deleted and a bad
# write is a one-line SSH restore. Loopback-only would mean no browser mask
# editing at all.
#
# api_overlay_logo: sets overlay.logo to an image already in the station's
# assets directory (the bundle ships them); any other value is refused. The
# old choice is one dropdown pick away, and nothing is deleted.
_NETWORK_ALLOWED_ENDPOINTS = frozenset({
    "api_lock",
    "api_process_clip",
    "api_encode_chunk",
    "api_probe",
    "api_overlay_preview",
    "api_calibration_autofit",
    "api_calibration_rebase_pointing",
    "api_calibration_autofit_accept",
    "api_mask_write",
    "api_overlay_logo",
})


def _is_loopback(remote_addr: str | None) -> bool:
    return bool(remote_addr) and remote_addr in _LOOPBACK_ADDRS


def _network_mutation_allowed(endpoint: str | None) -> bool:
    """True if a non-loopback caller may run this mutating route."""
    if endpoint not in _NETWORK_ALLOWED_ENDPOINTS:
        return False
    if endpoint == "api_lock":
        body = request.get_json(silent=True) or {}
        return bool(body.get("locked", True))
    return True


@app.before_request
def _enforce_network_readonly():
    """403 a non-loopback caller of a mutating route that is not allowlisted.

    Returns ``None`` to let the request proceed: safe methods, loopback
    callers, allowlisted routes, and URLs that match no view (Flask answers
    those 404/405 without running anything)."""
    if request.method in _SAFE_METHODS or request.endpoint is None:
        return None
    if _is_loopback(request.remote_addr):
        return None
    if _network_mutation_allowed(request.endpoint):
        return None
    logger.warning(
        "readonly: blocked %s %s from %s",
        request.method, request.path, request.remote_addr,
    )
    resp = jsonify({
        "error": "station_readonly",
        "hint": "station is read-only; use SSH",
    })
    resp.status_code = 403
    return resp


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _BadParam(ValueError):
    """A malformed query or body parameter, answered as a 400 JSON error."""


@app.errorhandler(_BadParam)
def _bad_param(exc: _BadParam):
    return jsonify({'error': str(exc)}), 400


def _num_param(raw, name: str, cast=float):
    """``cast(raw)``, or a 400 JSON error naming the parameter.

    Without this a bad value (``?lines=abc``, ``?ss=nan``) raised out of the
    view and came back as Flask's HTML 500 page."""
    try:
        value = cast(raw)
    except (TypeError, ValueError):
        raise _BadParam(f'{name} must be a number') from None
    if isinstance(value, float) and not math.isfinite(value):
        raise _BadParam(f'{name} must be a finite number')
    return value


@app.route('/api/vitals')
def api_vitals():
    data = dict(cached('vitals', _VITALS_TTL, collect_vitals))
    try:
        data['top_procs'] = _top_procs_by_cpu()
    except Exception:
        data['top_procs'] = []
    return jsonify(data)


# ---------------------------------------------------------------------------
# GET /api/status
# ---------------------------------------------------------------------------

@app.route('/api/status')
def api_status():
    # Stale-while-revalidate: a caller must never block on a slow collect
    # (the du walks can take >20 s on a busy capture disk, and the pusher
    # fetches this endpoint with a finite timeout to build its status
    # heartbeat; a blocked refresh used to turn into a missed heartbeat and
    # a false "offline" on the dashboard).
    return jsonify(cached_swr('status', 60, collect_status))


# ---------------------------------------------------------------------------
# GET /api/nights/<cam>
# ---------------------------------------------------------------------------

@app.route('/api/nights/<cam>')
def api_nights(cam):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    return jsonify(list_nights(cam))


# ---------------------------------------------------------------------------
# GET /api/rmsnights/<cam>
# ---------------------------------------------------------------------------

@app.route('/api/rmsnights/<cam>')
def api_rmsnights(cam):
    """Return list of nights (YYYYMMDD) for which RMS data exists for this camera."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cam_dir = None
    for _label, dir_path, station_code in _rms_camera_dirs():
        if station_code == cam:
            cam_dir = dir_path
            break
    if not cam_dir or not cam_dir.exists():
        return jsonify([])
    dates: set[str] = set()
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = cam_dir / sub
        if not sub_dir.exists():
            continue
        for d in sub_dir.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r'^[A-Z0-9]+_(\d{8})_', d.name)
            if m:
                dates.add(m.group(1))
    return jsonify(sorted(dates, reverse=True))


# ---------------------------------------------------------------------------
# GET /api/chunks/<cam>/<date>
# ---------------------------------------------------------------------------

@app.route('/api/chunks/<cam>/<date>')
def api_chunks(cam, date):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    return jsonify(list_chunks(
        cam, date,
        locked_only=request.args.get('locked_only') == '1',
        lock_type_filter=request.args.get('lock_type', ''),
        from_s=request.args.get('from', '').replace(':', ''),
        to_s=request.args.get('to', '').replace(':', ''),
    ))


# A colour segment modified within one segment plus this grace may still be
# the file colour capture is writing (the window list_chunks hides, see
# station_collect._segment_in_progress). The encoder's only guard against it
# is the ready flag, so marking it ready or encoding it re-encoded the partial
# file in place, and capture went on writing to the replaced file: the rest of
# the segment was lost.
def _segment_window_s(cfg: dict) -> float:
    return float(int(cfg.get('segment_duration', 20) or 20) + _INPROGRESS_GRACE_S)


def _segment_open(cam: str, date: str, mkv: Path, cfg: dict) -> bool:
    """True if colour capture may still be writing *mkv*. A re-encoded chunk
    is finished whatever its mtime (the encoder replaced the file)."""
    try:
        age = time.time() - mkv.stat().st_mtime
    except OSError:
        return False
    return age < _segment_window_s(cfg) and not _chunk_is_reencoded(cam, date, mkv, cfg)


def _wait_segment_closed(mkv: Path, cfg: dict, *, sleep=time.sleep,
                         clock=time.monotonic) -> bool:
    """Wait until *mkv* has gone one segment window without a write. A live
    segment closes within one window, so the wait is bounded to two; False if
    it is still being written then, or is gone."""
    window = _segment_window_s(cfg)
    deadline = clock() + 2 * window
    while True:
        try:
            age = time.time() - mkv.stat().st_mtime
        except OSError:
            return False
        if age >= window:
            return True
        left = deadline - clock()
        if left <= 0:
            return False
        sleep(max(0.5, min(window - age, left)))


def _ready_once_closed(cam: str, date: str, mkv: Path, cfg: dict) -> bool:
    """Wait for colour capture to close *mkv*, then mark it ready (as the
    lock would have; its watcher normally has already). False if it is still
    being written: its lock stands, and dawn encodes and uploads it."""
    if not _wait_segment_closed(mkv, cfg):
        logger.warning('%s is still being written after %.0f s: leaving its encode '
                       'and upload to dawn', mkv.name, 2 * _segment_window_s(cfg))
        return False
    if _STATE_MANAGER_OK:
        try:
            _flags_manager.mark_ready(cam, date, mkv.name, cfg)
        except Exception:
            logger.exception('marking %s ready failed', mkv.name)
    return True


def _upload_locked_chunk(cam: str, date: str, mkv: Path, cfg: dict,
                         segment_open: bool = False) -> None:
    """Encode and upload a just-locked chunk, one at a time with process_clip
    deliveries. The dashboard locks every clip a signed-in user plays, so
    locks can arrive in bursts, and each upload re-encodes its chunk first.
    A chunk colour capture is still writing is encoded once it is closed."""
    if segment_open and not _ready_once_closed(cam, date, mkv, cfg):
        return
    with _process_clip_serial:
        try:
            archive_upload.upload_chunk(cam, date, mkv, cfg)
        except Exception:
            logger.exception('lock: uploading %s/%s/%s failed', cam, date, mkv.name)


# ---------------------------------------------------------------------------
# POST /api/lock/<cam>/<date>/<filename>   {"locked": true|false}
# ---------------------------------------------------------------------------

@app.route('/api/lock/<cam>/<date>/<filename>', methods=['POST'])
def api_lock(cam, date, filename):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    m = CHUNK_RE.match(filename)
    if not m:
        abort(400)
    body = request.get_json(silent=True) or {}
    lock_it = bool(body.get('locked', True))
    cap_dir = capture_base() / cam / date
    mkv = cap_dir / filename
    if not mkv.exists():
        abort(404)
    try:
        cfg = load_config()
        if lock_it:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            # Locked now either way; a segment still being written is not
            # marked ready, and is encoded and uploaded once it is closed.
            segment_open = _segment_open(cam, date, mkv, cfg)
            if _STATE_MANAGER_OK:
                lock_info = {
                    'lock_type': 'manual',
                    'detection_time': now.strftime('%Y%m%d_%H%M%S'),
                    'meteor_time': None,
                }
                existing = _flags_manager.lock_chunk_if_unlocked(
                    cam, date, filename, lock_info, cfg, ready=not segment_open)
                if existing is not None:
                    # Already locked: keep its lock (a detection stays a
                    # detection) and leave its upload to the path that owns it.
                    return jsonify({'locked': True,
                                    'lock_type': existing.get('lock_type') or 'detection'})
            threading.Thread(
                target=_upload_locked_chunk,
                args=(cam, date, mkv, cfg, segment_open),
                daemon=True,
            ).start()
        else:
            rovimen_lock.unlock(mkv)  # remove legacy sidecar if present
            if _STATE_MANAGER_OK:
                _flags_manager.unlock_chunk(cam, date, filename, cfg)
            threading.Thread(
                target=archive_upload.delete_chunk,
                args=(cam, date, filename, cfg),
                daemon=True,
            ).start()
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify({'locked': lock_it, 'lock_type': 'manual' if lock_it else None})


# ---------------------------------------------------------------------------
# POST /api/process_clip/<cam>/<date>/<filename>
# ---------------------------------------------------------------------------

# Chunks with an in-flight process_clip delivery, keyed "cam/date/filename".
# Single-flight: a second POST while the encode+upload still runs answers 202
# instead of stacking another ffmpeg pass on the same file.
_process_clip_active: set[str] = set()
_process_clip_guard = threading.Lock()
# Deliveries encode one at a time. The dashboard's clip requester sends a
# batch per pass (multi-station witnesses a station does not encode at dawn),
# and a thread per clip must not become that many ffmpeg passes at once on a
# CPU-starved box. A Process click queues behind at most one batch.
_process_clip_serial = threading.Lock()
# Retry-After for a request refused because dawn is running (?idle=1).
_PROCESS_CLIP_BUSY_RETRY_S = 900


def _stack_for_delivery(mkv: Path, cfg: dict) -> None:
    """Stack a chunk the night pass left unstacked, before it is encoded (from
    the raw capture, as dawn's Phase 1 does). Under ``services.stacker.scope:
    "detections"`` a chunk that is not a detection has no stack until it is
    delivered here. stacker.process_chunk is idempotent, and it takes a
    boundary splice whose merged stack detection_lock could not write."""
    if not rovimen_config.service_enabled(cfg, 'stacker'):
        return
    try:
        import stacker
        stacker.process_chunk(mkv, cfg)
    except Exception:
        logger.exception('process_clip: stacking %s failed', mkv.name)


def _details_for_delivery(cam: str, date: str, mkv: Path, cfg: dict) -> None:
    """Render the delivered clip's details variant where dawn renders none
    (meteor clips encoded on demand); archive_upload ships it after the base
    clip that was just uploaded."""
    if (rovimen_config.meteor_clips_encoded(cfg)
            or not rovimen_config.service_enabled(cfg, 'overlay_details')):
        return
    try:
        import overlay_details
        overlay_details.run_clip(cam, date, mkv, cfg)
    except Exception:
        logger.exception('process_clip: details for %s failed', mkv.name)


def _clip_processed(cam: str, date: str, mkv: Path) -> bool:
    """True once the chunk is both re-encoded and uploaded (state.json, with
    the ``.reencoded`` sidecar as the legacy re-encode marker). A re-encode
    that has not reached the archive yet is NOT processed: delivering it is
    exactly what process_clip is for."""
    if not _STATE_MANAGER_OK:
        return False
    try:
        cfg = load_config()
        chunk = _flags_manager.load(cam, date, cfg).get('chunks', {}).get(mkv.name, {})
    except Exception:
        return False
    reencoded = bool(chunk.get('reencoded')) or Path(str(mkv) + '.reencoded').exists()
    return reencoded and bool(chunk.get('uploaded'))


def _is_clip_name(filename: str) -> bool:
    """A capture chunk or a boundary splice (detection_lock's
    ``<cam>_<date>_<start>_<end>_color.mkv``). A meteor at a chunk edge is
    locked as the splice, so that is the clip a multi-station event asks for;
    the encoder, the stacker and overlay_details take both names."""
    return rovimen_night.parse_chunk_name(filename) is not None


@app.route('/api/process_clip/<cam>/<date>/<filename>', methods=['GET'])
def api_process_clip_status(cam, date, filename):
    """What a POST would find now, read-only: whether the chunk exists, is
    already processed, has a delivery in flight, and whether dawn is running
    (a POST with ``?idle=1`` would be refused). The VPS clip requester probes
    it once per pass: a bundle older than ``?idle=1`` answers 405 here, and
    the requester leaves that station alone until it updates.
    ``clips_at_dawn`` false (``services.reencode.meteor_clips: false``) lets
    the requester ask for last night's clips as soon as dawn is done instead
    of waiting for 12:00 UTC, because nothing else will deliver them."""
    if not (re.match(r'^[A-Z0-9]+$', cam) and re.match(r'^\d{8}$', date)
            and _is_clip_name(filename)):
        abort(400)
    mkv = capture_base() / cam / date / filename
    exists = mkv.exists()
    with _process_clip_guard:
        running = f'{cam}/{date}/{filename}' in _process_clip_active
        queued = len(_process_clip_active)
    return jsonify({
        'exists': exists,
        'processed': exists and _clip_processed(cam, date, mkv),
        'running': running,
        'queued': queued,
        'dawn_running': rovimen_night.dawn_running(),
        'idle_supported': True,
        'clips_at_dawn': rovimen_config.meteor_clips_encoded(load_config()),
    })


@app.route('/api/process_clip/<cam>/<date>/<filename>', methods=['POST'])
def api_process_clip(cam, date, filename):
    """Deliver ONE clip to the archive: lock it if it is not already locked,
    then re-encode + upload in a background thread (archive_upload.upload_chunk,
    the same path a manual lock kicks).

    Unlike api_lock this never overwrites an existing lock — a detection lock
    keeps its meteor_time (the shortclip seek offset depends on it); only an
    unlocked chunk gets a fresh manual lock.

    Network-allowed (see _NETWORK_ALLOWED_ENDPOINTS): it only generates and
    preserves data, so the dashboard can call it where the whole-night dawn
    run is SSH-only.

    ``?idle=1`` (the dashboard's automatic clip requests) is refused with 503
    while dawn_process runs, so the request waits for the morning run instead
    of competing with it; a Process click leaves it off and runs at once."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not _is_clip_name(filename):
        abort(400)
    cap_dir = capture_base() / cam / date
    mkv = cap_dir / filename
    if not mkv.exists():
        abort(404)
    if request.args.get('force') != '1' and _clip_processed(cam, date, mkv):
        return jsonify({'ok': False, 'processed': True,
                        'error': 'already processed: re-encoded and uploaded'}), 409
    if request.args.get('idle') == '1' and rovimen_night.dawn_running():
        resp = jsonify({'ok': False, 'busy': 'dawn',
                        'error': 'dawn processing is running; retry later'})
        resp.headers['Retry-After'] = str(_PROCESS_CLIP_BUSY_RETRY_S)
        return resp, 503

    key = f'{cam}/{date}/{filename}'
    with _process_clip_guard:
        if key in _process_clip_active:
            return jsonify({'ok': True, 'running': True}), 202
        _process_clip_active.add(key)
    try:
        cfg = load_config()

        # Existing lock wins (state.json primary, .locked sidecar fallback).
        lock_type = None
        if _STATE_MANAGER_OK:
            try:
                night_state = _flags_manager.load(cam, date, cfg)
                lock = night_state.get('chunks', {}).get(filename, {}).get('lock')
                if lock:
                    lock_type = lock.get('lock_type')
            except Exception:
                pass
        if lock_type is None:
            li = rovimen_lock.get_lock_info(mkv)
            if li:
                lock_type = li.get('lock_type')

        # The fireball alert asks for the event's chunk minutes after it, maybe
        # while colour capture is still writing it: lock it now, deliver it
        # once it is closed (see _segment_window_s).
        segment_open = _segment_open(cam, date, mkv, cfg)
        if lock_type is None and _STATE_MANAGER_OK:
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            _flags_manager.lock_chunk(cam, date, filename, {
                'lock_type': 'manual',
                'detection_time': now.strftime('%Y%m%d_%H%M%S'),
                'meteor_time': None,
            }, cfg, ready=not segment_open)
            lock_type = 'manual'
        force = request.args.get('force') == '1'

        def _deliver():
            try:
                if segment_open and not _ready_once_closed(cam, date, mkv, cfg):
                    return
                with _process_clip_serial:
                    _stack_for_delivery(mkv, cfg)
                    if archive_upload.upload_chunk(cam, date, mkv, cfg, force=force):
                        _details_for_delivery(cam, date, mkv, cfg)
            except Exception:
                logger.exception('process_clip delivery failed for %s', key)
            finally:
                with _process_clip_guard:
                    _process_clip_active.discard(key)

        threading.Thread(target=_deliver, daemon=True).start()
    except Exception as exc:
        with _process_clip_guard:
            _process_clip_active.discard(key)
        return jsonify({'error': str(exc)}), 500
    return jsonify({'ok': True, 'locked': True, 'lock_type': lock_type})


# ---------------------------------------------------------------------------
# Overlay gate for download/serve routes
# ---------------------------------------------------------------------------

def _chunk_is_reencoded(cam: str, date: str, mkv: Path, cfg: dict) -> bool:
    """True when *mkv* already carries the burned-in OSD overlay.

    A chunk is overlaid once ``encoder.process_chunk`` has re-encoded it
    in-place; that is recorded in state.json (``reencoded``) with a
    ``.reencoded`` sidecar fallback for nights predating the state manager.
    """
    if _STATE_MANAGER_OK:
        try:
            cstate = (_flags_manager.load(cam, date, cfg)
                      .get('chunks', {}).get(mkv.name, {}))
            if cstate.get('reencoded', False):
                return True
        except Exception:
            pass
    return Path(str(mkv) + '.reencoded').exists()


def _ensure_overlaid(cam: str, date: str, mkv: Path, cfg: dict) -> bool:
    """Guarantee *mkv* carries the branding+data overlay, rendering on demand.

    color_capture writes a chunk raw and the encoder later re-encodes it
    in-place to burn in the overlay, so there is a window where the
    ``_color.mkv`` on disk has no overlay. Every download/serve route funnels
    through here first: an un-overlaid chunk is re-encoded synchronously
    (``encoder.process_chunk`` is idempotent, ~1 min) rather than served raw.

    The encode waits for a segment colour capture is still writing to close
    (bounded, see _wait_segment_closed), then runs one at a time with the
    process_clip deliveries (_process_clip_serial): a burst of plays used to
    start one full-priority ffmpeg per chunk at once.

    Returns True once the overlay is present, False if it could not be
    produced (caller must refuse to serve raw rather than fall back).
    """
    if _chunk_is_reencoded(cam, date, mkv, cfg):
        return True
    # Overlay disabled fleet-wide/per-station means the "processed" variant is
    # the chunk as-is; nothing to burn in, so serving it is not a bypass.
    overlay_cfg = cfg.get('overlay') or {}
    if not overlay_cfg.get('enabled', True):
        return True
    if not _wait_segment_closed(mkv, cfg):
        logger.warning('on-demand overlay: %s is still being written; not encoding it',
                       mkv.name)
        return False
    try:
        import encoder as _encoder  # local import: only this path needs it
        with _process_clip_serial:
            # Another delivery may have encoded it while this one queued.
            if _chunk_is_reencoded(cam, date, mkv, cfg):
                return True
            # A chunk that is fully on disk (downloadable) is complete even if
            # the ready flag was never set (legacy nights); mark it so
            # process_chunk doesn't skip it. The night key is derived from the
            # chunk filename the same way process_chunk derives it.
            if _STATE_MANAGER_OK:
                cm = CHUNK_RE.match(mkv.name)
                if cm:
                    try:
                        night_str = _night_date(cm.group(2), cm.group(3))
                        _flags_manager.mark_ready(cam, night_str, mkv.name, cfg)
                    except Exception:
                        pass
            if _encoder.process_chunk(mkv, cfg):
                return True
    except Exception:
        logger.exception('on-demand overlay render failed for %s', mkv.name)
    # Re-check: a concurrent encoder pass may have finished it meanwhile.
    return _chunk_is_reencoded(cam, date, mkv, cfg)


# ---------------------------------------------------------------------------
# GET /api/shortclip/<cam>/<date>/<filename>?pre=2&post=5
# ---------------------------------------------------------------------------

@app.route('/api/shortclip/<cam>/<date>/<filename>')
def api_shortclip(cam, date, filename):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    m = CHUNK_RE.match(filename)
    if not m:
        abort(400)
    cfg_now = load_config()
    cap_dir = capture_base() / cam / date
    mkv     = cap_dir / filename
    if not mkv.exists():
        abort(404)

    # Validate the numbers before the overlay gate, which can spend a minute
    # re-encoding the chunk.
    explicit = 'ss' in request.args and 't' in request.args
    if explicit:
        # Explicit seek offset + duration supplied by the trim UI
        offset   = max(0.0, _num_param(request.args['ss'], 'ss'))
        duration = max(0.5, _num_param(request.args['t'], 't'))
    else:
        pre  = max(0.0, _num_param(
            request.args.get('pre', cfg_now.get('pre_detection_seconds', 2)), 'pre'))
        post = max(0.0, _num_param(
            request.args.get('post', cfg_now.get('post_detection_seconds', 5)), 'post'))

    # Overlay gate: never cut a download clip from a chunk that has not yet had
    # the branding+data overlay burned in. Renders it on demand (~1 min) rather
    # than serving raw footage that could be re-badged.
    if not _ensure_overlaid(cam, date, mkv, cfg_now):
        abort(503)

    if not explicit:
        duration = pre + post
        # Compute seek offset from detection time (state.json first, sidecar fallback)
        offset = max(0.0, 10.0 - pre)  # fallback: centre of 20 s chunk
        try:
            _lock_info = None
            if _STATE_MANAGER_OK:
                try:
                    night_state = _flags_manager.load(cam, date, cfg_now)
                    _lock_info = night_state.get('chunks', {}).get(filename, {}).get('lock')
                except Exception:
                    pass
            if _lock_info is None:
                _lock_info = rovimen_lock.get_lock_info(mkv)
            mt_str   = _lock_info.get('meteor_time') if _lock_info else None
            det_time = _lock_info.get('detection_time') if _lock_info else None
            off = _detection_offset_s(_chunk_utc_dt(m.group(2), m.group(3), cfg_now),
                                      mt_str, det_time)
            if off is not None:
                offset = max(0.0, off - pre)
        except (ValueError, AttributeError):
            pass

    import tempfile, os
    fd, tmp_path = tempfile.mkstemp(suffix='.mp4')
    os.close(fd)
    try:
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-ss', f'{offset:.3f}', '-i', str(mkv),
            '-t', f'{duration:.3f}',
            '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
            '-an', '-movflags', '+faststart',
            '-y', tmp_path,
        ]
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0:
            abort(500)
        clip_stem = mkv.stem.replace('_color', '')
        return send_file(tmp_path, mimetype='video/mp4', as_attachment=True,
                         download_name=f'{clip_stem}_clip.mp4')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GET /api/stitch_video/<cam>/<date>?f1=<filename1>&f2=<filename2>
# GET /api/shortclip_stitch/<cam>/<date>?f1=...&f2=...&ss=...&t=...
# ---------------------------------------------------------------------------

def _stitch_concat_list(cap_dir: Path, f1: str, f2: str) -> tuple[Path, Path, Path] | None:
    """Validate f1/f2, return (mkv1, mkv2, cap_dir). Aborts on bad input."""
    for fn in (f1, f2):
        if not re.match(r'^[\w.\-]+\.mkv$', fn):
            abort(400)
    mkv1 = cap_dir / f1
    mkv2 = cap_dir / f2
    if not mkv1.exists() or not mkv2.exists():
        abort(404)
    return mkv1, mkv2


def _send_stitched(mkv1: Path, mkv2: Path, codec_args: list[str], **send_kwargs):
    """Concatenate two chunks with ffmpeg into a temp MP4 and send it.

    ``send_file`` opens the file before it returns, so both temp files are
    unlinked here whatever the outcome; the open handle keeps the data
    readable until the response is sent. The MP4 used to be left behind in
    /tmp on every call (the unit has no PrivateTmp), filling the root disk
    until the station's daily reboot."""
    import tempfile
    fd_list, list_path = tempfile.mkstemp(suffix='.txt')
    fd_out, out_path = tempfile.mkstemp(suffix='.mp4')
    os.close(fd_out)
    try:
        with os.fdopen(fd_list, 'w') as fh:
            fh.write(f"file '{mkv1}'\nfile '{mkv2}'\n")
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-f', 'concat', '-safe', '0', '-i', list_path,
            *codec_args, '-movflags', '+faststart',
            '-y', out_path,
        ]
        result = subprocess.run(cmd, stderr=subprocess.DEVNULL, timeout=120)
        if result.returncode != 0:
            abort(500)
        return send_file(out_path, mimetype='video/mp4', **send_kwargs)
    finally:
        for path in (list_path, out_path):
            try:
                os.unlink(path)
            except OSError:
                pass


@app.route('/api/stitch_video/<cam>/<date>')
def api_stitch_video(cam, date):
    """Concatenate two MKV chunks (copy codec) and serve as seekable MP4."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    f1 = request.args.get('f1', '')
    f2 = request.args.get('f2', '')
    cap_dir = capture_base() / cam / date
    mkv1, mkv2 = _stitch_concat_list(cap_dir, f1, f2)

    cfg_now = load_config()
    if not (_ensure_overlaid(cam, date, mkv1, cfg_now)
            and _ensure_overlaid(cam, date, mkv2, cfg_now)):
        abort(503)
    return _send_stitched(mkv1, mkv2, ['-c', 'copy'], conditional=True)


@app.route('/api/shortclip_stitch/<cam>/<date>')
def api_shortclip_stitch(cam, date):
    """Concatenate two chunks, then trim and re-encode as a download clip."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    f1 = request.args.get('f1', '')
    f2 = request.args.get('f2', '')
    offset   = max(0.0, _num_param(request.args.get('ss', 0), 'ss'))
    duration = max(0.5, _num_param(request.args.get('t', 10), 't'))
    cap_dir = capture_base() / cam / date
    mkv1, mkv2 = _stitch_concat_list(cap_dir, f1, f2)

    cfg_now = load_config()
    if not (_ensure_overlaid(cam, date, mkv1, cfg_now)
            and _ensure_overlaid(cam, date, mkv2, cfg_now)):
        abort(503)
    stem = Path(f1).stem.replace('_color', '')
    return _send_stitched(
        mkv1, mkv2,
        ['-ss', f'{offset:.3f}', '-t', f'{duration:.3f}',
         '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20', '-an'],
        as_attachment=True, download_name=f'{stem}_stitch_clip.mp4')


# ---------------------------------------------------------------------------
# GET /api/timelapses
# ---------------------------------------------------------------------------

@app.route('/api/timelapses')
def api_timelapses():
    return jsonify(list_timelapses())


# ---------------------------------------------------------------------------
# GET /api/settings
# ---------------------------------------------------------------------------

@app.route('/api/settings')
def api_settings():
    try:
        return jsonify(load_config())
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/hardware')
def api_hardware():
    """Return the hardware profile written by hardware_assessment.sh."""
    hw_path = CONFIG_PATH.parent / 'hardware.json'
    if not hw_path.exists():
        return jsonify({'__missing': True,
                        'hint': 'Run hardware_assessment.sh to generate hardware profile'}), 404
    try:
        return jsonify(json.loads(hw_path.read_text()))
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


@app.route('/api/probe', methods=['POST'])
def api_probe():
    """Run hardware_assessment.sh and return the updated hardware profile."""
    import subprocess
    probe_path = CONFIG_PATH.parent / 'hardware_assessment.sh'
    if not probe_path.exists():
        return jsonify({'__error': 'hardware_assessment.sh not found in rovimen_scripts/'}), 404
    try:
        subprocess.run(
            ['bash', str(probe_path)],
            cwd=str(CONFIG_PATH.parent),
            timeout=30,
            check=True,
        )
        hw_path = CONFIG_PATH.parent / 'hardware.json'
        return jsonify(json.loads(hw_path.read_text()))
    except subprocess.TimeoutExpired:
        return jsonify({'__error': 'Probe timed out after 30s'}), 504
    except subprocess.CalledProcessError as e:
        return jsonify({'__error': f'Probe failed: {e}'}), 500
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


# ---------------------------------------------------------------------------
# GET /api/rms-detections/<cam>/<date>
# ---------------------------------------------------------------------------

def _rms_sessions_for_night(cam: str, date: str, cfg: dict) -> list[Path]:
    """Return all RMS session dirs for a given camera and observation night.

    A single observation night may span multiple sessions (pre-midnight +
    post-midnight), grouped by the shared noon rule. A session RMS has
    archived exists in both CapturedFiles and ArchivedFiles under the same
    name; only the ArchivedFiles copy is returned (rovimen_night.rms_sessions,
    as in detection_lock and the indexer). Sorted by session name.
    """
    rms_path = cfg.get('stations', {}).get(cam, {}).get('rms_data_path')
    if not rms_path:
        return []
    return rovimen_night.rms_sessions(Path(rms_path), None, date)


@app.route('/api/rms-detections/<cam>/<date>')
def api_rms_detections(cam: str, date: str):
    """Return enriched meteor detection data for a camera and observation night.

    Each session's FTPdetectinfo (FF file, duration, angular velocity) is
    joined to its _radiants.txt (shower, radiant, magnitudes) by begin time,
    through rovimen_rms.session_detections: the same parser, column widths
    and join detection_indexer writes to detections_index.db, so a night's
    rows do not change when the dashboard moves from this live endpoint to
    the index. Cached for 120 s.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)

    def collect():
        detections: list[dict] = []
        for session in _rms_sessions_for_night(cam, date, load_config()):
            detections.extend(rovimen_rms.session_detections(session))
        return {'camera': cam, 'date': date, 'count': len(detections),
                'detections': detections}

    return jsonify(cached(f'rms-det:{cam}:{date}', 120, collect))


@app.route('/api/encoding/stats')
def api_encoding_stats():
    """Return file-size reduction stats for the most recent reencoded night.

    Compares actual MKV sizes against expected raw size (bitrate × segment_duration).
    Returns per-camera and overall stats so the dashboard can show real reduction numbers.

    "Re-encoded" comes from state.json, where the encoder has recorded it
    since 2026-03-26 (7def9e7e); the ``.reencoded`` sidecars it looked for
    before stopped being written then, so this returned nothing for six
    months while listing every night of every camera. The sidecar is still
    honoured for nights older than that.
    """
    def _reencoded_names(cam: str, night_dir: Path, cfg: dict) -> list[str]:
        names: set[str] = set()
        if _STATE_MANAGER_OK:
            try:
                chunks = _flags_manager.load(cam, night_dir.name, cfg).get('chunks', {})
                names.update(n for n, c in chunks.items() if c.get('reencoded'))
            except Exception:
                pass
        try:
            names.update(p.name[:-len('.reencoded')]
                         for p in night_dir.glob('*_color.mkv.reencoded'))
        except OSError:
            pass
        return sorted(names)

    def _compute() -> dict:
        cfg          = load_config()
        raw_kbps     = cfg.get('raw_bitrate_kbps', 8192)
        segment_s    = cfg.get('segment_duration', 20)
        raw_mb       = raw_kbps / 8 / 1000 * segment_s
        cap_base     = capture_base()

        per_cam: dict[str, dict] = {}
        all_pcts: list[float]    = []
        all_sizes: list[float]   = []

        for cam_dir in sorted(_safe_iterdir(cap_base)):
            if not _safe_is_dir(cam_dir):
                continue
            cam = cam_dir.name
            # The most recent night with at least one re-encoded chunk, looking
            # back at most 14 nights so an idle camera costs a bounded walk.
            nights = sorted((d for d in _safe_iterdir(cam_dir)
                             if re.match(r'^\d{8}$', d.name) and _safe_is_dir(d)),
                            reverse=True)[:14]
            night_dir: Path | None = None
            names: list[str] = []
            for d in nights:
                names = _reencoded_names(cam, d, cfg)
                if names:
                    night_dir = d
                    break
            if night_dir is None:
                continue

            pcts: list[float]  = []
            sizes: list[float] = []
            for name in names:
                try:
                    actual_mb = (night_dir / name).stat().st_size / 1e6
                except OSError:
                    continue
                # Skip corrupted/truncated chunks and obvious raw-passthrough outliers
                if actual_mb < 0.5 or actual_mb > raw_mb * 1.1:
                    continue
                pct = 100.0 * (1.0 - actual_mb / raw_mb)
                pcts.append(pct)
                sizes.append(actual_mb)

            if not pcts:
                continue

            pcts_sorted  = sorted(pcts)
            sizes_sorted = sorted(sizes)
            per_cam[cam] = {
                'night':    night_dir.name,
                'chunks':   len(pcts),
                'avg':      round(sum(pcts) / len(pcts), 1),
                'median':   round(pcts_sorted[len(pcts) // 2], 1),
                'min':      round(pcts_sorted[0], 1),
                'max':      round(pcts_sorted[-1], 1),
                'size_avg': round(sum(sizes) / len(sizes), 1),
                'size_min': round(sizes_sorted[0], 1),
                'size_max': round(sizes_sorted[-1], 1),
            }
            all_pcts.extend(pcts)
            all_sizes.extend(sizes)

        overall: dict = {}
        if all_pcts:
            all_sorted    = sorted(all_pcts)
            sizes_sorted  = sorted(all_sizes)
            overall       = {
                'chunks':   len(all_pcts),
                'avg':      round(sum(all_pcts) / len(all_pcts), 1),
                'median':   round(all_sorted[len(all_pcts) // 2], 1),
                'min':      round(all_sorted[0], 1),
                'max':      round(all_sorted[-1], 1),
                'size_avg': round(sum(all_sizes) / len(all_sizes), 1),
                'size_min': round(sizes_sorted[0], 1),
                'size_max': round(sizes_sorted[-1], 1),
            }

        return {'cameras': per_cam, 'overall': overall, 'raw_mb': round(raw_mb, 1)}

    try:
        return jsonify(cached('encoding_stats', 3600, _compute))
    except Exception as e:
        return jsonify({'__error': str(e)}), 500


# What an overlay preview request may change: how the overlay looks, never
# what it draws or reads. The text (coords, network) and every path (font,
# gmn_logo) come from the station's own config. The body used to be merged
# over the whole overlay block, and coords reached the drawtext filter raw, so
# a network caller could append drawtext=textfile=<any file> and read the
# file back in the PNG.
_PREVIEW_NUMBER_KEYS = ('font_size', 'text_opacity', 'bar_logo_opacity', 'az', 'alt')
_PREVIEW_FLAG_KEYS = ('show_logo', 'show_network', 'show_station', 'show_coords',
                      'show_pointing', 'show_timestamp')
_PREVIEW_STYLES = ('standard', 'cinema')


def _preview_logo(value) -> str | None:
    """*value* if it names an image in the assets directory, the same files
    /api/overlay/logo may save, so the dropdown's pick previews before it is
    saved; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    path = _ASSETS_DIR / Path(value).name
    if str(path) != value or path.suffix.lower() not in _LOGO_EXTS or not path.is_file():
        return None
    return value


def _preview_overrides(body) -> dict:
    """The display keys of an overlay preview request, validated."""
    if not isinstance(body, dict):
        return {}
    out: dict = {}
    if body.get('style') in _PREVIEW_STYLES:
        out['style'] = body['style']
    for key in _PREVIEW_NUMBER_KEYS:
        if key in body:
            out[key] = _num_param(body[key], key)
    for key in _PREVIEW_FLAG_KEYS:
        if key in body:
            out[key] = bool(body[key])
    logo = _preview_logo(body.get('logo'))
    if logo:
        out['logo'] = logo
    return out


@app.route('/api/overlay_preview', methods=['POST'])
def api_overlay_preview():
    """Render a preview frame with the SAME overlay burned into real clips.

    Both the text (``build_drawtext_annotations``) and the branding
    (bottom-bar astromania logo + corner GMN logo via
    ``build_branding_overlay``) come from the shared ``overlay`` module, so the
    admin preview cannot drift from the encoder/still output — the branding sits
    in the bottom bar, never over the video. Draws onto a synthetic 1280×720
    frame; ``now`` supplies the timestamp epoch. The body may override only
    display keys (see _preview_overrides).
    """
    import os, tempfile
    cfg = load_config()
    ov  = {**cfg.get('overlay', {}),
           **_preview_overrides(request.get_json(silent=True))}

    font_path = ov.get('font', '')
    az        = _num_param(ov.get('az', 0), 'az')
    alt       = _num_param(ov.get('alt', 0), 'alt')

    stations   = cfg.get('stations', {})
    station_id = next(iter(stations), 'STATION')
    station_cfg = {'az': az, 'alt': alt}

    if not font_path or not Path(font_path).exists():
        return jsonify({'error': 'font not found'}), 400

    now_epoch = int(datetime.now(timezone.utc).timestamp())

    filters, bar_h = overlay.build_drawtext_annotations(
        ov, station_id, station_cfg, now_epoch,
    )
    logo_inputs, brand_fragment = overlay.build_branding_overlay(
        ov, in_label='vmain', out_label='out', input_index=1, bar_h=bar_h,
    )
    use_logo = bool(logo_inputs)

    fd, out_path = tempfile.mkstemp(suffix='.png')
    os.close(fd)
    try:
        base = ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
                '-f', 'lavfi', '-i',
                'color=c=0x111111:size=1280x720:rate=1:duration=1']
        vf_parts: list[str] = []
        if bar_h > 0:
            vf_parts.append(f'pad=iw:ih+{bar_h}:0:0:black')
        vf_parts.extend(filters)

        if use_logo:
            cpu_chain = ','.join(vf_parts) if vf_parts else 'null'
            fc = f'[0:v]{cpu_chain}[vmain];{brand_fragment}'
            cmd = [*base, *logo_inputs, '-filter_complex', fc,
                   '-map', '[out]', '-frames:v', '1', out_path]
        else:
            vf  = ','.join(vf_parts) if vf_parts else 'null'
            cmd = [*base, '-vf', vf, '-frames:v', '1', out_path]

        r = subprocess.run(cmd, capture_output=True, timeout=15)
        if r.returncode != 0:
            return jsonify({'error': r.stderr.decode(errors='replace')[-300:]}), 500
        return Response(Path(out_path).read_bytes(), mimetype='image/png')
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# GET /api/logs/<service>?lines=200
# ---------------------------------------------------------------------------

_ALLOWED_LOG_SERVICES = {'color-capture', 'rovimen-station-api', 'dawn'}

# File-based log services: service name → filename under log_path (~/logs/ by default)
_FILE_LOG_SERVICES: dict[str, str] = {
    'dawn':      'rovimen_dawn.log',
    'stacker':   'stacker.log',
    'encoder':   'encoder.log',
    'timelapse': 'timelapse.log',
    'janitor':   'janitor.log',
}


# RMS names a capture log for the session that opened it, e.g.
# log_RO000E_20260921_081451_002.log. The date in the name is what makes a
# past night addressable: without it the only reachable log is whichever file
# was touched most recently, which during the day is an idle EventMonitor, so
# the "Dropped frames: N" counter for the night you actually care about is
# unreadable.
_RMS_LOG_DATE_RE = re.compile(r'_(\d{8})_\d{6}')


def _rms_log_dir(station_id: str, cfg: dict) -> tuple[Path | None, str]:
    """Directory holding a camera's RMS logs, and the glob that selects them.

    RMS may write logs to either:
      - <rms_data_path>/logs/          (per-camera, e.g. RMS_data/cam1/logs/)
      - <rms_data_path>/../logs/       (shared parent dir, e.g. RMS_data/logs/)
    The per-camera dir wins; the shared dir must be filtered by station_id
    because it holds every camera's logs.
    """
    scfg = cfg.get('stations', {}).get(station_id, {})
    rms_data_path = scfg.get('rms_data_path', '')
    if not rms_data_path:
        return None, ''
    logs_dir = Path(rms_data_path) / 'logs'
    if logs_dir.exists() and any(logs_dir.glob('*.log')):
        return logs_dir, '*.log'
    parent_logs_dir = Path(rms_data_path).parent / 'logs'
    if parent_logs_dir.exists():
        return parent_logs_dir, f'*{station_id}*.log'
    return None, ''


def _rms_log_dates(station_id: str, cfg: dict) -> list[str]:
    """Capture dates that have at least one log, newest first."""
    logs_dir, pattern = _rms_log_dir(station_id, cfg)
    if logs_dir is None:
        return []
    dates = set()
    for f in logs_dir.glob(pattern):
        m = _RMS_LOG_DATE_RE.search(f.name)
        if m:
            dates.add(m.group(1))
    return sorted(dates, reverse=True)


def _find_latest_rms_log(station_id: str, cfg: dict,
                         date: str | None = None) -> Path | None:
    """Newest RMS log for a station, optionally restricted to one capture date.

    Without ``date`` this returns the most recently modified log, which is the
    right answer for "what is happening now". With ``date`` it returns the
    newest log NAMED for that date -- a session RMS rotates (``_001``,
    ``_002``) keeps one name per rotation, and the last one holds the end of
    the night. mtime alone cannot pick that file: a log from any night can be
    touched later by a process still holding it open.
    """
    logs_dir, pattern = _rms_log_dir(station_id, cfg)
    if logs_dir is None:
        return None
    logs = list(logs_dir.glob(pattern))
    if not date:
        # Live view: the file being written right now is the one to show, and
        # mtime is what identifies it.
        if not logs:
            return None
        return max(logs, key=lambda p: p.stat().st_mtime)
    logs = [f for f in logs if f'_{date}_' in f.name]
    if not logs:
        return None
    return max(logs, key=lambda p: p.name)


def _tail_file(path: Path, lines: int) -> list[str]:
    """Return last `lines` lines of a file. lines=0 returns last 5000 lines."""
    if lines == 0:
        lines = 5000
    result = subprocess.run(
        ['tail', '-n', str(lines), str(path)],
        capture_output=True, text=True, timeout=5,
    )
    return result.stdout.splitlines()


@app.route('/api/logs/<service>', methods=['GET'])
def api_logs(service):
    raw = _num_param(request.args.get('lines', 200), 'lines', int)
    if raw < 0:
        raise _BadParam('lines must be >= 0')
    lines = 0 if raw == 0 else min(raw, 1000)

    # File-based logs (dawn, stacker, encoder, timelapse)
    if service in _FILE_LOG_SERVICES:
        cfg = load_config()
        log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
        log_file = log_dir / _FILE_LOG_SERVICES[service]
        if not log_file.exists():
            return jsonify({'service': service, 'lines': [], 'source': 'file',
                            'note': f'No {service} log found yet'})
        try:
            return jsonify({'service': service, 'lines': _tail_file(log_file, lines), 'source': 'file'})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    # RMS per-camera log (rms-RO000H, rms-RO000J, …)
    if service.startswith('rms-'):
        station_id = service[4:]
        cfg = load_config()
        date = (request.args.get('date') or '').strip()
        if date and not re.fullmatch(r'\d{8}', date):
            return jsonify({'error': 'date must be YYYYMMDD'}), 400
        log_file = _find_latest_rms_log(station_id, cfg, date=date or None)
        if not log_file:
            note = f'No RMS log found for {station_id}'
            if date:
                note = f'No RMS log for {station_id} on {date}'
            return jsonify({'service': service, 'lines': [], 'source': 'file',
                            'note': note, 'date': date or None,
                            'available_dates': _rms_log_dates(station_id, cfg)})
        try:
            return jsonify({'service': service, 'lines': _tail_file(log_file, lines),
                            'source': 'file', 'log_file': log_file.name,
                            'date': date or None,
                            'available_dates': _rms_log_dates(station_id, cfg)})
        except Exception as exc:
            return jsonify({'error': str(exc)}), 500

    # Systemd journal for remaining services
    if service not in _ALLOWED_LOG_SERVICES:
        return jsonify({'error': f'Service {service!r} not in allowed list'}), 400
    try:
        result = subprocess.run(
            ['journalctl', '-u', service, '-n', str(lines), '--no-pager', '--output=short-iso'],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({'service': service, 'lines': result.stdout.splitlines(), 'source': 'journal'})
    except subprocess.TimeoutExpired:
        return jsonify({'error': 'journalctl timed out'}), 500
    except Exception as exc:
        return jsonify({'error': str(exc)})


@app.route('/api/settings', methods=['PATCH'])
def api_settings_patch():
    """Deep-merge a JSON object into config.json (loopback only).

    Goes through config_patch.patch_config: the file is re-read under the
    shared config lock, so a write another process made moments ago is not
    lost, and the read cache is dropped only after the write succeeded (it
    used to be merged in place first, so a failed write left the cache
    serving values that were never saved)."""
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    try:
        config_patch.patch_config(payload, CONFIG_PATH)
    except Exception as e:
        logger.exception('PATCH /api/settings failed')
        return jsonify({'error': str(e)}), 500
    with _cache_lock:
        _cache.pop('config', None)
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Updater / service / dawn status (read-only)
# ---------------------------------------------------------------------------

@app.route('/api/updater/status')
def api_updater_status():
    """Return the installed bundle version, channel and VPS host (no network).

    ``status`` is the bundle verifier's last verdict (``ok``/``fail``) and
    when it ran, so it says whether the installed bundle checked out, not
    whether a newer one exists. The ``remote``/``up_to_date`` fields this used
    to return came from a /tmp cache that only the removed updater/check
    route wrote, so they were blank or stale on every station; they are gone
    rather than guessed.
    """
    cfg = load_config()
    version_file = CONFIG_PATH.parent / '.version'
    local_ver = version_file.read_text().strip() if version_file.exists() else 'none'
    bundle = _bundle_status()
    status = ''
    if bundle.get('last_verify'):
        status = f"verify {bundle['last_verify']}"
        if bundle.get('verified_at'):
            status += f" at {bundle['verified_at']}"
    return jsonify({
        'local':    local_ver,
        'channel':  cfg.get('update_channel', 'main'),
        'vps_host': cfg.get('vps_host', ''),
        'status':   status,
        'bundle':   bundle,
    })


@app.route('/api/rovimen/status')
def api_rovimen_status():
    """Run toggle_rovimen.sh status and return the output."""
    script = Path.home() / 'rovimen_scripts' / 'toggle_rovimen.sh'
    if not script.exists():
        return jsonify({'error': 'toggle_rovimen.sh not found'}), 500
    try:
        result = subprocess.run(
            ['bash', str(script), 'status'],
            capture_output=True, text=True, timeout=15,
        )
        return jsonify({'ok': True, 'output': result.stdout + result.stderr})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/dawn/progress')
def api_dawn_progress():
    """Return per-camera morning pipeline progress for the given date.

    Query param: date=YYYYMMDD (defaults to last completed night).
    """
    cfg = load_config()
    date_str = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        date_str = _last_completed_night()

    cameras: dict[str, dict] = {}
    for cam in cfg.get('stations', {}):
        if not _STATE_MANAGER_OK:
            cameras[cam] = {'error': 'flags_manager not available'}
            continue
        try:
            state = _flags_manager.load(cam, date_str, cfg)
            # Only count real captured chunks (STATION_YYYYMMDD_HHMMSS_color.mkv).
            # Other entries (e.g. night stack artefacts from an archive_upload bug)
            # are excluded so the denominator stays accurate.
            chunks = {
                k: v for k, v in state.get('chunks', {}).items()
                if CHUNK_RE.match(k)
            }
            total     = len(chunks)
            stacked   = sum(1 for c in chunks.values() if c.get('stacked'))
            reencoded = sum(1 for c in chunks.values() if c.get('reencoded'))
            locked    = sum(
                1 for c in chunks.values()
                if isinstance(c.get('lock'), dict)
                and c['lock'].get('lock_type') == 'detection'
            )
            uploaded  = sum(1 for c in chunks.values() if c.get('uploaded'))
            cameras[cam] = {
                'total':              total,
                'stacked':            stacked,
                'reencoded':          reencoded,
                'locked':             locked,
                'uploaded':           uploaded,
                'rms_complete':       bool(state.get('rms_complete', False)),
                'morning_done':       bool(state.get('morning_done', False)),
                'timelapse_done':     bool(state.get('timelapse_done', False)),
                'timelapse_uploaded': bool(state.get('timelapse_uploaded', False)),
            }
        except Exception as e:
            cameras[cam] = {'error': str(e)}

    try:
        r = subprocess.run(['pgrep', '-f', 'dawn_process'], capture_output=True)
        running = r.returncode == 0
    except Exception:
        running = False

    # Why the latest morning's full-night re-encode was stopped
    # (projected_miss / deadline_hit / enlarges), or None; re-measured daily.
    full_night_off = None
    if _STATE_MANAGER_OK:
        try:
            full_night_off = _flags_manager.read_full_night_marker()
        except Exception:
            full_night_off = None

    return jsonify({'date': date_str, 'cameras': cameras, 'running': running,
                    'full_night_reencode_off': full_night_off})

# ---------------------------------------------------------------------------
# GET /api/crons
# ---------------------------------------------------------------------------

@app.route('/api/crons')
def api_crons():
    """Return parsed crontab and /etc/cron.d/ entries for the station user."""
    entries: list[dict] = []

    # User crontab
    try:
        r = subprocess.run(['crontab', '-l'], capture_output=True, text=True)
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                parts = line.split(None, 5)
                if len(parts) >= 6:
                    entries.append({
                        'schedule': ' '.join(parts[:5]),
                        'command':  parts[5],
                        'source':   'crontab',
                    })
    except Exception:
        pass

    # /etc/cron.d/ — lines have format: schedule user command
    cron_d = Path('/etc/cron.d')
    if cron_d.exists():
        for f in sorted(cron_d.iterdir()):
            if not f.is_file() or f.name.startswith('.'):
                continue
            try:
                for line in f.read_text(errors='ignore').splitlines():
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    parts = line.split(None, 6)
                    if len(parts) >= 7:
                        entries.append({
                            'schedule': ' '.join(parts[:5]),
                            'command':  parts[6],
                            'source':   f'/etc/cron.d/{f.name}',
                        })
            except Exception:
                pass

    return jsonify(entries)


# ---------------------------------------------------------------------------
# GET /api/rms/status
# ---------------------------------------------------------------------------

@app.route('/api/rms/status')
def api_rms_status():
    """Return RMS processed/uploaded status per camera for a given night date.

    Query param: date=YYYYMMDD (defaults to last completed night).
    Processed: a final FTPdetectinfo_*.txt (not ``_unfiltered``/``_backup``)
               in a CapturedFiles or ArchivedFiles session dir.
    Uploaded:  session exists in ArchivedFiles (sent to GMN).
    Capturing: see _camera_is_capturing (process AND a recently written
               capture tree; StartCapture alone sleeps through the day).
    """
    date_str = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        date_str = _last_completed_night()

    cfg = load_config()
    result: dict[str, dict] = {}

    # Which RMS instances are processing (DetectStarsAndMeteors running) and
    # which have a StartCapture process at all; one pgrep each for the station.
    detecting_procs = subprocess.run(
        ['pgrep', '-af', 'DetectStarsAndMeteors'],
        capture_output=True, text=True
    ).stdout.lower()
    capture_procs = _capture_procs()

    for cam, sinfo in cfg.get('stations', {}).items():
        rms_data_path = sinfo.get('rms_data_path')
        if not rms_data_path:
            result[cam] = {'processed': False, 'uploaded': False,
                           'capturing': False, 'processing': False}
            continue

        rms_dir = Path(rms_data_path)
        processed = False
        uploaded   = False

        # Match sessions on observation night, not raw calendar date in the
        # dir name. A post-midnight session (e.g. RO000W_20260412_005904_*)
        # belongs to night 20260411 and a substring match on the date_str
        # would miss it. Uses the shared session regex and noon rule, as
        # detection_lock.process_night() does. Both trees are read: a session
        # in ArchivedFiles is the one RMS has uploaded.
        for sub, is_archived in (('ArchivedFiles', True), ('CapturedFiles', False)):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            try:
                sessions = list(sub_dir.iterdir())
            except OSError:
                continue
            for session_dir in sessions:
                if not session_dir.is_dir():
                    continue
                m = rovimen_night.SESSION_DIR_RE.match(session_dir.name)
                if not m:
                    continue
                if _night_date(m.group(2), m.group(3)) != date_str:
                    continue
                # The final file only: RMS writes _unfiltered/_backup variants
                # before detection finishes, and counting them marked a night
                # processed while it was still running.
                if rovimen_night.ftpdetect_files(session_dir):
                    processed = True
                if is_archived:
                    uploaded = True

        # Derive RMS instance index from rms_data_path (e.g. .../cam2 → 2)
        m = re.search(r'cam(\d+)$', rms_data_path.rstrip('/'))
        cam_idx = m.group(1) if m else None
        capturing = _camera_is_capturing(cam, rms_data_path, capture_procs)
        processing = (cam in detecting_procs
                      or bool(cam_idx and f'rms_cam{cam_idx}' in detecting_procs))

        result[cam] = {'processed': processed, 'uploaded': uploaded,
                       'capturing': capturing, 'processing': processing}

    return jsonify({'date': date_str, 'cameras': result})


# ---------------------------------------------------------------------------
# GET /api/rms/plots/<cam>/<date>
# GET /api/rms/plot_image/<cam>/<date>/<filename>
# ---------------------------------------------------------------------------

_RMS_PLOT_LABELS: dict[str, str] = {
    '_DETECTED_thumbs.jpg':           'Detected thumbnails',
    '_CAPTURED_thumbs.jpg':           'Captured thumbnails',
    '_captured_stack.jpg':            'Captured stack',
    '_calib_report_astrometry.jpg':   'Astrometry calibration',
    '_calib_report_photometry.png':   'Photometry calibration',
    '_calibration_variation.png':     'Calibration variation',
    '_photometry_variation.png':      'Photometry variation',
    '_fieldsums.png':                 'Field sums',
    '_fieldsums_noavg.png':           'Field sums (no avg)',
    '_observing_periods.png':         'Observing periods',
    '_ff_intervals.png':              'FF intervals',
    '_radiants.png':                  'Radiants',
}
_RMS_PLOT_ORDER = list(_RMS_PLOT_LABELS.keys())


def _find_rms_session_dir(cam: str, date: str, cfg: dict) -> Path | None:
    """Return the most-recent RMS session dir for cam+date (ArchivedFiles preferred)."""
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return None
    rms_dir = Path(rms_path)
    prefix = f'{cam}_{date}_'
    candidates: list[Path] = []
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        for d in sub_dir.iterdir():
            if d.is_dir() and d.name.startswith(prefix):
                candidates.append(d)
    if not candidates:
        return None
    return max(candidates, key=lambda d: d.stat().st_mtime)


def _find_latest_platepar(cam: str, cfg: dict) -> Path | None:
    """Return the newest platepar_cmn2010.cal for a camera.

    rovimen_rms.find_latest_platepar, the lookup platepar_autofit uses: the
    platepar in the camera's resolved RMS config directory (the one RMS
    loads, found by the stationID its .config declares or a running
    StartCapture's -c path) pooled with every session copy under
    rms_data_path; newest mtime wins. ``~/source/Stations/<cam>`` counts only
    when it is that directory: on several stations it is an inert mirror, and
    reading it unconditionally showed a stale pointing after a manual
    recalibration while the live platepar under ``~/RMS_*`` was never read.
    """
    return rovimen_rms.find_latest_platepar(cam, cfg)


def _rms_config_dir(cam: str, cfg: dict) -> Path | None:
    """Resolve the RMS station-config directory for a camera — the directory
    holding ``.config``, ``platepar_cmn2010.cal`` and the live ``mask.bmp``.

    Delegates to ``rovimen_rms.resolve_rms_config_dir`` (platepar_autofit
    uses the same function), which both the mask endpoints and the auto-fit
    run need to agree on. Resolution is by
    the ``stationID`` declared in a candidate ``.config`` rather than by
    deriving a directory name from ``rms_data_path``, since the fleet runs
    several layouts and most derivations are wrong on at least one of them
    (gmn0000's ``rms_data_path`` ends in the camera code, so it produced
    ``~/RMS_DE001B``, which does not exist -- that camera is
    ``~/RMS_cam1``). Candidates include directories a live
    ``RMS.StartCapture`` process is actually reading its config from, not
    just ``~/RMS_*``-style names -- see ``resolve_rms_config_dir`` and
    ``_candidate_config_dirs`` for the full order.

    ``~/source/Stations/<cam>/`` is genuinely live on some stations
    (gmnro05: ``DetectionTools.loadMask`` there reads
    ``~/source/Stations/RO0003/mask.bmp``, confirmed from the running
    ``RMS.StartCapture -c ~/source/Stations/RO0003/.config`` process) and an
    inert stale mirror on others (where ``~/RMS_*`` is what capture logs
    show RMS actually loading, e.g. "Loaded mask: /home/gmn/RMS_cam1/mask.bmp").
    Don't assume either way from the path alone -- that's what the
    live-process check settles.
    """
    station_cfg = cfg.get('stations', {}).get(cam, {})
    return rovimen_rms.resolve_rms_config_dir(cam, station_cfg)


def _mask_write_target(cam: str, cfg: dict) -> Path | None:
    """The one mask path a write must land on for RMS to pick it up.

    Deliberately NOT ``_find_latest_mask``'s newest-wins pooling: the session
    copies that pooling can return are snapshots RMS took at capture start,
    and writing one back changes nothing about what the next run loads.
    """
    config_dir = _rms_config_dir(cam, cfg)
    return config_dir / 'mask.bmp' if config_dir else None


def _find_latest_mask(cam: str, cfg: dict) -> Path | None:
    """Return the mask.bmp a camera is currently detecting with.

    The RMS config directory copy wins outright when it exists — that is the
    file RMS loads (see ``_rms_config_dir``), so it is the answer to "what
    mask is this camera using", regardless of mtimes. Only when a station has
    no config-dir mask do we fall back to pooling the historical copies and
    taking the newest.

    Fallback order (all candidates pooled, newest wins):
    1. ~/source/Stations/<cam>/mask.bmp           — legacy RMS layout
    2. <rms_data_path>/ArchivedFiles/<session>/   — archived session masks
    3. <rms_data_path>/CapturedFiles/<session>/   — captured session masks
    """
    live_mask = _mask_write_target(cam, cfg)
    if live_mask and live_mask.exists():
        return live_mask

    candidates: list[Path] = []

    active_mask = BASE / 'source' / 'Stations' / cam / 'mask.bmp'
    if active_mask.exists():
        candidates.append(active_mask)

    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if rms_path:
        rms_dir = Path(rms_path)
        for sub in ('CapturedFiles', 'ArchivedFiles'):
            sub_dir = rms_dir / sub
            if not sub_dir.exists():
                continue
            for session in sub_dir.iterdir():
                if not session.is_dir():
                    continue
                m = session / 'mask.bmp'
                if m.exists():
                    candidates.append(m)

    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _mask_contour(mask_path: Path, max_points: int = 64) -> list[list[float]] | None:
    """Derive a simplified boundary polygon of the UNMASKED region.

    RMS masks are grayscale bitmaps where 0 = masked (blocked by trees, buildings,
    horizon) and >0 = active (observable sky). We return a closed polygon of
    ``[nx, ny]`` points in normalised sensor coordinates (each in [-1, 1], matching
    the convention consumed by the dashboard's distortion model where
    ``x_img = nx * x_res / 2``), so the frontend can walk the actual sky boundary
    instead of the full sensor edge.

    Cheap column-scan envelope: for a downsampled set of columns we take the
    topmost and bottommost active pixel, then stitch the top edge (left→right) and
    bottom edge (right→left) into one loop. This captures the common horizon/tree
    occlusions (vertical clipping per column) without OpenCV/skimage. Returns
    None when no usable contour exists or the mask is effectively full
    (active fraction >= 0.999) so callers fall back to the sensor-edge walk.

    Interior holes and pure left/right side masks are not represented in v1 — see
    follow-up note in the PR.
    """
    if not _ensure_rms_venv_on_path():
        return None
    try:
        import numpy as np
        from PIL import Image
    except ImportError:
        return None

    try:
        with Image.open(mask_path) as im:
            arr = np.asarray(im)
    except Exception:
        return None

    if arr.ndim == 3:
        arr = arr[..., 0]
    if arr.ndim != 2 or arr.size == 0:
        return None

    height, width = arr.shape
    if height < 2 or width < 2:
        return None

    active = arr > 0
    if active.mean() >= 0.999:
        return None  # mask is effectively full — use sensor-edge walk

    n_cols = max(2, min(max_points // 2, width))
    cols = np.linspace(0, width - 1, n_cols).astype(int)

    top: list[tuple[int, int]] = []
    bot: list[tuple[int, int]] = []
    for c in cols:
        rows = np.nonzero(active[:, int(c)])[0]
        if rows.size == 0:
            continue
        top.append((int(c), int(rows[0])))
        bot.append((int(c), int(rows[-1])))

    if len(top) < 2:
        return None

    def _norm(x: int, y: int) -> list[float]:
        return [round(2.0 * x / width - 1.0, 4), round(2.0 * y / height - 1.0, 4)]

    poly = [_norm(x, y) for x, y in top] + [_norm(x, y) for x, y in reversed(bot)]
    return poly


@app.route('/api/platepar')
def api_platepar():
    """Return platepar data (pointing, FOV, rotation) for each configured camera."""
    cfg = load_config()
    stations = cfg.get('stations', {})
    result: dict[str, dict] = {}
    for cam in stations:
        pp_path = _find_latest_platepar(cam, cfg)
        if not pp_path:
            result[cam] = {'error': 'no platepar found'}
            continue
        try:
            pp = json.loads(pp_path.read_text())
        except Exception as e:
            result[cam] = {'error': str(e)}
            continue
        result[cam] = {
            'az_centre':               pp.get('az_centre'),
            'alt_centre':              pp.get('alt_centre'),
            'rotation_from_horiz':     pp.get('rotation_from_horiz'),
            'fov_h':                   pp.get('fov_h'),
            'fov_v':                   pp.get('fov_v'),
            'lat':                     pp.get('lat'),
            'lon':                     pp.get('lon'),
            'F_scale':                 pp.get('F_scale'),
            'X_res':                   pp.get('X_res'),
            'Y_res':                   pp.get('Y_res'),
            'x_poly_fwd':              pp.get('x_poly_fwd'),
            'y_poly_fwd':              pp.get('y_poly_fwd'),
            'equal_aspect':            pp.get('equal_aspect'),
            'asymmetry_corr':          pp.get('asymmetry_corr'),
            'force_distortion_centre': pp.get('force_distortion_centre'),
            'x_poly':                  pp.get('x_poly'),  # legacy / fallback
            'y_poly':                  pp.get('y_poly'),
            'distortion_type':         pp.get('distortion_type'),
            'RA_d':                    pp.get('RA_d'),
            'dec_d':                   pp.get('dec_d'),
            'pos_angle_ref':           pp.get('pos_angle_ref'),
            'JD':                      pp.get('JD'),
            'Ho':                      pp.get('Ho'),
            'elev':                    pp.get('elev'),
            'refraction':              pp.get('refraction'),
            'source':                  str(pp_path),
        }
        # Optional: boundary of the unmasked sky region, so the dashboard can
        # render the FOV polygon clipped to the actual observable area rather
        # than the full sensor edge. None/absent → frontend uses sensor edges.
        try:
            mask_path = _find_latest_mask(cam, cfg)
            if mask_path is not None:
                contour = _mask_contour(mask_path)
                if contour:
                    result[cam]['mask_contour'] = contour
                    result[cam]['mask_source'] = str(mask_path)
        except Exception:
            pass
    return jsonify(result)


@app.route('/api/mask_image/<cam>')
def api_mask_image(cam: str):
    """Serve the raw mask.bmp bytes for a camera. Browsers render BMP natively,
    so no conversion is needed for the Calibration page's stack+mask overlay."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    mask_path = _find_latest_mask(cam, cfg)
    if not mask_path:
        abort(404)
    return send_file(str(mask_path), conditional=True)


def _rms_frame_size(cam: str, cfg: dict) -> tuple[int, int] | None:
    """Sensor frame size RMS is configured to capture at, from the camera's
    ``.config``. This is the authoritative mask size: a mask of any other
    dimensions is silently useless to RMS, so an uploaded mask is resampled
    to it rather than trusted to arrive correct from a browser whose canvas
    was sized by CSS."""
    config_dir = _rms_config_dir(cam, cfg)
    if not config_dir:
        return None
    config_file = config_dir / '.config'
    try:
        text = config_file.read_text()
    except OSError:
        return None
    dims = {}
    for key in ('width', 'height'):
        m = re.search(rf'^\s*{key}\s*:\s*(\d+)', text, re.MULTILINE)
        if m:
            dims[key] = int(m.group(1))
    if 'width' not in dims or 'height' not in dims:
        return None
    return dims['width'], dims['height']


@app.route('/api/mask/<cam>', methods=['POST'])
def api_mask_write(cam: str):
    """Replace a camera's live ``mask.bmp`` with one drawn in the browser.

    Body: ``{"png_b64": "<base64 PNG>"}`` — an RGBA/greyscale PNG where dark
    pixels mean "masked out" and bright pixels mean "active sky", matching
    RMS's own convention (0 = blocked, 255 = observable). Anything that is
    not fully dark is treated as active, so a half-painted antialiased brush
    edge resolves to sky rather than quietly eating a strip of the FOV.

    Three things make this safe to expose to the network (the same bar
    ``api_calibration_autofit_accept`` had to clear):

      * It writes exactly one file, whose path the caller cannot influence —
        ``_mask_write_target`` derives it from the station's own config.
      * The previous mask is kept as a timestamped ``.bak`` next to it, so
        every write is reversible from SSH with a ``cp``.
      * The write is atomic (tmp + ``os.replace``), so a dropped connection
        mid-upload cannot leave RMS with a truncated bitmap.

    The new mask applies at the next detection pass without restarting
    anything: ``RMS.DetectionTools`` re-reads the file per run rather than
    caching it at capture start.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    if cam not in cfg.get('stations', {}):
        return jsonify({'error': f'unknown camera {cam}'}), 404

    target = _mask_write_target(cam, cfg)
    if target is None:
        return jsonify({'error': 'could not resolve this camera\'s RMS config directory'}), 500
    if not target.parent.is_dir():
        return jsonify({'error': f'RMS config directory {target.parent} does not exist'}), 500

    payload = request.get_json(silent=True) or {}
    png_b64 = payload.get('png_b64') or ''
    if not isinstance(png_b64, str) or not png_b64:
        return jsonify({'error': 'png_b64 is required'}), 400
    # A 1280x720 1-bit mask is ~10 KB as PNG; the cap is three orders of
    # magnitude above that purely to bound a malformed request.
    if len(png_b64) > 8_000_000:
        return jsonify({'error': 'mask image too large'}), 413

    try:
        raw = base64.b64decode(png_b64, validate=True)
    except (ValueError, binascii.Error):
        return jsonify({'error': 'png_b64 is not valid base64'}), 400

    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            mask = im.convert('L')
    except Exception as e:
        return jsonify({'error': f'could not decode the uploaded image: {e}'}), 400

    size = _rms_frame_size(cam, cfg)
    if size and mask.size != size:
        mask = mask.resize(size, Image.NEAREST)

    # Binarise last, after any resampling: NEAREST can't invent midtones but
    # a future change to the resample filter could, and RMS treats every
    # non-zero pixel as active sky.
    mask = mask.point(lambda v: 0 if v < 128 else 255, mode='L')

    tmp = target.with_name('mask.bmp.tmp')
    try:
        if target.exists():
            backup = target.with_name(
                f'mask.bmp.bak.{datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")}')
            shutil.copy2(target, backup)
        mask.save(tmp, format='BMP')
        os.replace(tmp, target)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        return jsonify({'error': f'could not write {target}: {e}'}), 500

    # getcolors() yields (count, value) pairs, not (value, count).
    masked_px = sum(count for count, value in mask.getcolors() or [] if value == 0)
    total_px = mask.size[0] * mask.size[1]
    logger.info('[%s] mask replaced (%dx%d, %.1f%% masked)',
                cam, mask.size[0], mask.size[1], 100.0 * masked_px / total_px)
    return jsonify({
        'ok': True,
        'path': str(target),
        'width': mask.size[0],
        'height': mask.size[1],
        'masked_pct': round(100.0 * masked_px / total_px, 2),
    })


def _analyze_recalib_json(data: dict) -> dict | None:
    """Astrometric drift summary from a platepars_all_recalibrated.json dict.

    Ports the fleet calibration check's analyze_platepar_data
     without a numpy dependency, so it can run under the bare
    station venv rather than requiring the RMS venv. Returns None if the data
    can't be analyzed (empty, too short, unparseable timestamps).

    Pointing drift is measured in the HORIZONTAL frame (az/alt), not in
    RA/Dec. A camera is bolted to the ground, so azimuth and altitude are what
    stay fixed while it is not moving; the RA/Dec of the field centre sweep
    across the night simply because the Earth turns.

    Measuring in RA/Dec produced a number that was not merely noisy but
    meaningless, and differently meaningless per station: whether each
    recalibrated platepar carries its own frame epoch or one shared reference
    epoch varies with the RMS version a station runs.

        DE001E   JD spread over the night 6.735 h -> RA_d swept 333 to 75 deg
        RO000M   JD spread over the night 0.000 h -> RA_d moved 0.02 deg

    Both cameras were steady -- DE001E's azimuth held to 0.001 deg all night.
    The old fit read 19.78 arcmin/h for the first and 0.08 for the second, so
    identical physical stability reported 250x worse on one camera than the
    other purely from a file-format difference, and the fleet-wide column
    could be neither compared nor sorted.
    """
    if not data:
        return None

    def _ff_name_to_jd(ff_name: str) -> float | None:
        m = re.search(r'_(\d{8})_(\d{6})_(\d+)_', ff_name)
        if not m:
            return None
        dt = datetime.strptime(m.group(1) + m.group(2), '%Y%m%d%H%M%S').replace(tzinfo=timezone.utc)
        ms = int(m.group(3))
        unix = dt.timestamp() + ms / 1000.0
        return unix / 86400.0 + 2440587.5

    keyed = sorted(
        ((_ff_name_to_jd(k) or 0.0, k, v) for k, v in data.items()),
        key=lambda t: t[0],
    )
    if not keyed or keyed[0][0] == 0.0:
        return None

    jds = [jd for jd, _, _ in keyed]
    entries = [v for _, _, v in keyed]
    hours = [(jd - jds[0]) * 24.0 for jd in jds]
    duration_h = hours[-1]
    if duration_h < 0.05:
        return None

    def _linfit(xs: list[float], ys: list[float]) -> tuple[float, float]:
        n = len(xs)
        mean_x = sum(xs) / n
        mean_y = sum(ys) / n
        num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
        den = sum((x - mean_x) ** 2 for x in xs)
        slope = num / den if den else 0.0
        return slope, mean_y - slope * mean_x

    def _angular_distance(ra1: float, dec1: float, ra2: float, dec2: float) -> float:
        ra1, dec1, ra2, dec2 = (math.radians(v) for v in (ra1, dec1, ra2, dec2))
        cos_d = (math.sin(dec1) * math.sin(dec2)
                 + math.cos(dec1) * math.cos(dec2) * math.cos(ra1 - ra2))
        return math.degrees(math.acos(max(-1.0, min(1.0, cos_d))))

    recalib = [bool(e.get('auto_recalibrated', False)) for e in entries]
    recalib_frac = (sum(recalib) / len(recalib)) if recalib else 0.0

    summary = {
        'n_frames':       len(entries),
        'duration_h':     round(duration_h, 2),
        'recalib_ok_pct': round(recalib_frac * 100, 1),
    }

    # Drift is fitted to recalibrated frames only. A frame whose recalibration
    # failed carries the starting platepar unchanged -- every such frame in a
    # night has the identical az/alt -- so it is not a measurement. RO0006 on
    # 2026-09-22: 6 unrecalibrated frames sat ~1 deg from the 21 good ones, all
    # at the start of a 1.17 h night, and read as 58 arcmin/h on a camera that
    # held to 0.005 deg. recalib_ok_pct already reports the failures, and the
    # dashboard leads with it for that reason.
    fit = [(h, e) for h, e, ok in zip(hours, entries, recalib) if ok]
    if len(fit) < 3:
        return summary
    fit_hours = [h for h, _ in fit]
    fit_entries = [e for _, e in fit]
    fit_span = fit_hours[-1] - fit_hours[0]
    if fit_span < 0.05:
        return summary

    rot_slope, _ = _linfit(fit_hours, [e['rotation_from_horiz'] for e in fit_entries])
    summary['rotation_arcmin_per_h'] = round(abs(rot_slope) * 60, 2)

    # az/alt, not RA/Dec -- see the docstring. Absent on a file old enough not
    # to carry them, in which case no pointing figure is reported rather than
    # falling back to the RA/Dec one, which is wrong on exactly the stations
    # most likely to have such a file.
    if all('az_centre' in e and 'alt_centre' in e for e in fit_entries):
        az_slope, az_i = _linfit(fit_hours, [e['az_centre'] for e in fit_entries])
        alt_slope, alt_i = _linfit(fit_hours, [e['alt_centre'] for e in fit_entries])
        t0, t1 = fit_hours[0], fit_hours[-1]
        # _angular_distance is a spherical separation: altitude plays the part
        # of declination and azimuth that of right ascension.
        total_pointing = _angular_distance(
            az_slope * t0 + az_i, alt_slope * t0 + alt_i,
            az_slope * t1 + az_i, alt_slope * t1 + alt_i,
        )
        summary['pointing_arcmin_per_h'] = round((total_pointing / fit_span) * 60, 2)
        summary['pointing_frame'] = 'horizontal'

    return summary


@app.route('/api/calibration_summary/<cam>')
def api_calibration_summary(cam: str):
    """Astrometric drift summary for a camera's last completed night.

    Reuses the platepars_all_recalibrated.json RMS's own ApplyRecalibrate already
    writes every night (the same source the
    fleet calibration check reads) — deliberately not a from-scratch residual recomputation.

    Default date is always "yesterday" (now - 1 day), never "today" -- a
    session starting the evening of day D is bucketed as D and only finishes
    (and gets recalibrated/summarized) near dawn of D+1, so at any point
    during D+1 the most recently completed night is still D = (D+1) - 1.
    An earlier version used "today" once past 12:00 UTC, which meant this
    endpoint returned no_data for every camera all afternoon and evening,
    every day -- it was asking for tonight's still-in-progress session
    instead of last night's finished one.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    date_str = request.args.get('date', '')
    if not re.match(r'^\d{8}$', date_str):
        now = datetime.now(timezone.utc)
        date_str = (now - timedelta(days=1)).strftime('%Y%m%d')

    session_dir = _find_rms_session_dir(cam, date_str, cfg)
    if not session_dir:
        return jsonify({'status': 'no_data', 'date': date_str})

    json_path = session_dir / 'platepars_all_recalibrated.json'
    if not json_path.exists():
        return jsonify({'status': 'no_data', 'date': date_str})

    try:
        data = json.loads(json_path.read_text())
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)})

    summary = _analyze_recalib_json(data)
    if summary is None:
        return jsonify({'status': 'no_data', 'date': date_str})

    summary['status'] = 'ok'
    summary['date'] = date_str
    return jsonify(summary)


# ---------------------------------------------------------------------------
# GET /api/fireball_pack/<cam>/<date>?time=HHMMSS&window=<seconds>
#
# Everything needed to run astrometry/photometry on a fireball off-station in
# one zip: the night's platepars (session .cal + per-FF recalibrated JSON),
# FTPdetectinfo, CALSTARS, RMS .config, mask, the FF frames around the event
# time, the FR bins in the same window, and the night's best-star-count FF
# (per CALSTARS) as an astrometry reference. Read-only; consumed by the
# dashboard's admin-gated basket checkout (?fireball=1).
# ---------------------------------------------------------------------------

# Bounds keeping a pack shippable over a station tunnel: FF fits are ~1-4 MB
# each, FR bins a few MB — 24 files ≈ 100 MB worst case.
_PACK_MAX_FRAMES = 24
_PACK_MAX_WINDOW_S = 600.0
_PACK_DEFAULT_WINDOW_S = 60.0


def _parse_calstars_counts(path: Path) -> dict[str, int]:
    """Map FF filename -> extracted star count from an RMS CALSTARS file.

    Format-tolerant: any line naming an FF_*.fits starts a block; every
    subsequent line whose tokens are all numeric counts as one star row.
    """
    counts: dict[str, int] = {}
    current: str | None = None
    try:
        text = path.read_text(errors='replace')
    except OSError:
        return counts
    for raw in text.splitlines():
        line = raw.strip()
        ff = re.search(r'(FF_[A-Z0-9]+_\d{8}_\d{6}_\d{3}_\d+\.fits)', line)
        if ff:
            current = ff.group(1)
            counts.setdefault(current, 0)
            continue
        if current and line:
            tokens = line.replace(',', ' ').split()
            try:
                for tok in tokens:
                    float(tok)
            except ValueError:
                continue
            counts[current] += 1
    return counts


def _fireball_pack_entries(cam: str, date: str, cfg: dict,
                           event_dt: datetime | None,
                           window_s: float) -> tuple[list[tuple[str, Path]], dict]:
    """Collect (arcname, path) pairs for a fireball analysis pack.

    Returns the entries plus a pack_info dict recording what was found,
    what was capped, and what is missing — the consumer needs to know a
    hole is "not on disk any more", not "forgot to ask".
    """
    sessions = _rms_sessions_for_night(cam, date, cfg)
    entries: list[tuple[str, Path]] = []
    seen_arcnames: set[str] = set()
    info: dict = {
        'camera': cam, 'date': date,
        'event_time': event_dt.strftime('%H%M%S') if event_dt else None,
        'window_s': window_s,
        'sessions': [s.name for s in sessions],
        'missing': [],
    }

    def _add(arcname: str, path: Path) -> None:
        if arcname not in seen_arcnames and path.is_file():
            seen_arcnames.add(arcname)
            entries.append((arcname, path))

    # Night-level calibration artefacts, per session (a night can span a
    # pre-midnight + post-midnight session pair).
    calstars_files: list[Path] = []
    for sess in sessions:
        for pat in ('platepar_cmn2010.cal', 'platepars_all_recalibrated.json',
                    'FTPdetectinfo_*.txt', 'CALSTARS*.txt', '.config',
                    'mask.bmp', '*.config'):
            for f in sorted(sess.glob(pat)):
                _add(f'{sess.name}/{f.name}', f)
                if f.name.startswith('CALSTARS'):
                    calstars_files.append(f)

    # Fall back to the camera's active platepar/mask when the session dirs
    # carry none (CapturedFiles before RMS processing finishes).
    if not any(a.endswith('platepar_cmn2010.cal') for a, _ in entries):
        pp = _find_latest_platepar(cam, cfg)
        if pp:
            _add(f'active/{pp.name}', pp)
        else:
            info['missing'].append('platepar')
    if not any(a.endswith('mask.bmp') for a, _ in entries):
        mk = _find_latest_mask(cam, cfg)
        if mk:
            _add(f'active/{mk.name}', mk)
        else:
            info['missing'].append('mask')

    # FF frames + FR bins around the event time.
    frames: list[tuple[datetime, str, Path]] = []
    if event_dt is not None:
        for sess in sessions:
            for f in sess.iterdir():
                if not (f.name.startswith(('FF_', 'FR_'))
                        and f.suffix in ('.fits', '.bin')):
                    continue
                start = rovimen_night.rms_frame_time(f.name)
                if start and abs((start - event_dt).total_seconds()) <= window_s:
                    frames.append((start, f.name, f))
        # Nearest-first so the cap trims the window edges, not the event.
        frames.sort(key=lambda e: (abs((e[0] - event_dt).total_seconds()), e[1]))
        if len(frames) > _PACK_MAX_FRAMES:
            info['frames_capped'] = len(frames) - _PACK_MAX_FRAMES
            frames = frames[:_PACK_MAX_FRAMES]
        for _, name, f in sorted(frames, key=lambda e: e[1]):
            _add(f'frames/{name}', f)
        if not frames:
            info['missing'].append('event_frames')
    else:
        info['missing'].append('event_frames (no time param)')

    # Best-star-count FF of the night per CALSTARS — the astrometry reference.
    counts: dict[str, int] = {}
    for cs in calstars_files:
        counts.update(_parse_calstars_counts(cs))
    if counts:
        best_ff, best_n = max(counts.items(), key=lambda kv: kv[1])
        info['best_calstars_ff'] = {'name': best_ff, 'stars': best_n}
        for sess in sessions:
            f = sess / best_ff
            if f.is_file():
                _add(f'best_calstars_ff/{best_ff}', f)
                break
        else:
            info['missing'].append(f'best_calstars_ff ({best_ff} not on disk)')
    else:
        info['missing'].append('calstars')

    return entries, info


@app.route('/api/fireball_pack/<cam>/<date>')
def api_fireball_pack(cam: str, date: str):
    """Zip the night's fireball-analysis artefacts for one camera."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    time_s = request.args.get('time', '')
    event_dt: datetime | None = None
    if time_s:
        if not re.match(r'^\d{6}$', time_s):
            abort(400)
        # Clip names carry the event's own calendar day, but the observation
        # night is named for the evening — roll pre-noon times to the next
        # calendar day so post-midnight events land in the right window.
        try:
            base = datetime.strptime(date, '%Y%m%d')
            event_dt = base.replace(hour=int(time_s[:2]), minute=int(time_s[2:4]),
                                    second=int(time_s[4:6]))
            if event_dt.hour < 12:
                event_dt += timedelta(days=1)
        except ValueError:
            abort(400)
    try:
        window_s = float(request.args.get('window', _PACK_DEFAULT_WINDOW_S))
    except ValueError:
        abort(400)
    window_s = max(5.0, min(window_s, _PACK_MAX_WINDOW_S))

    cfg = load_config()
    entries, info = _fireball_pack_entries(cam, date, cfg, event_dt, window_s)
    if not entries:
        return jsonify({'status': 'no_data', **info}), 404

    import tempfile, zipfile
    fd, tmp_path = tempfile.mkstemp(suffix='.zip')
    os.close(fd)
    try:
        with zipfile.ZipFile(tmp_path, 'w', zipfile.ZIP_STORED) as zf:
            for arcname, path in entries:
                zf.write(path, arcname)
            info['files'] = [a for a, _ in entries]
            zf.writestr('pack_info.json', json.dumps(info, indent=2))
        return send_file(tmp_path, mimetype='application/zip', as_attachment=True,
                         download_name=f'{cam}_{date}_fireball_pack.zip')
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _autofit_log_file(cfg: dict, cam: str) -> Path:
    log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
    return log_dir / f'platepar_autofit_{cam}.log'


# A blind astrometry.net solve is minutes, not tens of minutes. Past this,
# a run that has neither written a result nor left a live process behind is
# dead, not slow.
_AUTOFIT_STALE_AFTER_S = 30 * 60


def _autofit_process_alive(cam: str) -> bool:
    """True if a platepar_autofit.py subprocess for *cam* is actually running.

    Reads /proc directly rather than shelling out to pgrep: pgrep -f matches
    on whole command lines and would happily match the caller that spawned
    it (the updater has been bitten by exactly that), whereas here we can
    skip our own pid and require both the script name and the camera to
    appear as separate argv entries.
    """
    try:
        pids = [entry for entry in os.listdir('/proc') if entry.isdigit()]
    except OSError:  # pragma: no cover - /proc always exists on Linux
        return False
    own_pid = str(os.getpid())
    for pid in pids:
        if pid == own_pid:
            continue
        try:
            argv = Path('/proc', pid, 'cmdline').read_bytes().decode().split('\0')
        except (OSError, UnicodeDecodeError):
            continue  # process exited between listdir and read, or is not ours
        if any(a.endswith('platepar_autofit.py') for a in argv) and cam in argv:
            return True
    return False


def _autofit_run_in_flight(cfg: dict, cam: str) -> bool:
    """True if a previously-spawned autofit subprocess for this camera is
    still running.

    The trigger hands the subprocess a log file and keeps no handle on it, so
    the starting point is the same inference the status endpoint has always
    made: the log exists and is newer than the newest staged result JSON,
    meaning a run started and hasn't produced its result yet. That is shared
    with the trigger so it can single-flight-guard too (PR #884 review: a
    double-click, client retry, or hostile loop previously spawned a fresh
    concurrent blind solve on every POST).

    On its own, though, that inference has no exit: a run that CRASHES never
    writes a result, so the log stays permanently newer than it and the
    camera is wedged at "running" forever -- status never resolves and every
    later trigger 409s. So the freshly-started case is confirmed against
    reality before it is believed: a live process says yes outright, and
    without one the log has to have been touched within
    ``_AUTOFIT_STALE_AFTER_S`` (the subprocess writes progress to it, so its
    mtime tracks a live run and freezes the moment one dies).
    """
    log_file = _autofit_log_file(cfg, cam)
    if not log_file.exists():
        return False
    log_mtime = log_file.stat().st_mtime
    cand_dir = Path.home() / 'rovimen_scripts' / 'calibration_candidates' / cam
    result_files = sorted(cand_dir.glob('*.json'), key=lambda p: p.stat().st_mtime) if cand_dir.exists() else []
    newest_result_mtime = result_files[-1].stat().st_mtime if result_files else 0
    if log_mtime <= newest_result_mtime + 1:
        return False
    if _autofit_process_alive(cam):
        return True
    return (time.time() - log_mtime) < _AUTOFIT_STALE_AFTER_S


def _autofit_log_tail(cfg: dict, cam: str, lines: int = 12) -> str:
    """Last few log lines, for reporting a crashed run instead of a bare
    status string. Bounded read -- the log is appended to across runs."""
    log_file = _autofit_log_file(cfg, cam)
    try:
        with open(log_file, 'rb') as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 8192))
            tail = fh.read().decode(errors='replace')
    except OSError:
        return ''
    return '\n'.join(tail.splitlines()[-lines:]).strip()


# How recently the capture tree must have been written for the camera to
# count as busy. RMS lands an FF roughly every 10 s while capturing, and the
# morning reprocess writes continuously too, so anything quieter than this is
# genuinely idle.
_CAPTURE_ACTIVE_WINDOW_S = 300


def _capture_tree_is_active(cam: str, rms_data_path: str) -> bool:
    """True if this camera's capture tree was written to in the last few
    minutes -- the signal that it is busy *right now*."""
    if not rms_data_path:
        return False
    captured = Path(rms_data_path) / 'CapturedFiles'
    if not captured.is_dir():
        return False
    try:
        sessions = sorted(
            (d for d in captured.iterdir() if d.is_dir() and d.name.startswith(f'{cam}_')),
            key=lambda d: d.name,
        )
    except OSError:
        return False
    if not sessions:
        return False
    cutoff = time.time() - _CAPTURE_ACTIVE_WINDOW_S
    try:
        newest = sessions[-1]
        if newest.stat().st_mtime >= cutoff:
            return True
        return any(f.stat().st_mtime >= cutoff for f in newest.iterdir())
    except OSError:
        return False


def _capture_procs() -> str:
    """``pgrep -af StartCapture`` output ('' when pgrep cannot run)."""
    try:
        return subprocess.run(
            ['pgrep', '-af', 'StartCapture'], capture_output=True, text=True, timeout=20,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ''


def _camera_is_capturing(cam: str, rms_data_path: str,
                         capture_procs: str | None = None) -> bool:
    """True if this camera is capturing (or reprocessing) at this moment.

    The point of the question is whether spending minutes of CPU on a blind
    astrometry.net solve would cost frames, so it has to be about what the
    camera is doing now, not about what is installed.

    Process liveness alone cannot answer it: RMS's StartCapture runs as a
    persistent service on this fleet and sleeps through the day waiting for
    dusk, so ``pgrep -af StartCapture`` matches at noon exactly as it does at
    midnight. Guarding on that alone meant the auto-fit trigger refused with
    "capture is currently active" on every camera, at every hour, all year --
    the dashboard button sends no body, so it could never pass the
    ``force`` override either, and the feature was unreachable from the UI
    it was built for. (Verified 2026-09-21: /api/rms/status reported
    capturing:true for all cameras on gmnro02/03/07 at 11:20 UTC.)

    So liveness is kept only for the direction it is reliable in -- no
    process means definitely not capturing, and skips the filesystem walk --
    and the actual answer comes from whether the capture tree is still being
    written to.

    ``capture_procs`` lets a caller asking about every camera share one
    ``pgrep`` (see _capture_procs); /api/rms/status uses this too, so its
    ``capturing`` flag no longer reads true all day.
    """
    if capture_procs is None:
        capture_procs = _capture_procs()
    m = re.search(r'cam(\d+)$', (rms_data_path or '').rstrip('/'))
    running = cam in capture_procs or bool(m and f'RMS_cam{m.group(1)}' in capture_procs)
    if not running:
        return False
    return _capture_tree_is_active(cam, rms_data_path)


@app.route('/api/calibration/autofit/<cam>', methods=['POST'])
def api_calibration_autofit(cam: str):
    """Start platepar_autofit.py for the requested camera in the background.

    Same shape as api_dawn_run: spawns the script as a detached subprocess
    and returns immediately; the caller polls autofit_status for the
    result. The fit is STAGED (candidate file + result JSON under
    ~/rovimen_scripts/calibration_candidates/<cam>/), never applied live --
    see api_calibration_autofit_accept for that step.

    Two guards added per the PR #884 review:
      - Single-flight: refuses (409) if a run for this camera already looks
        in-flight, rather than spawning a concurrent astrometry.net solve.
      - Capture-window: a blind solve is minutes of sustained CPU, and on
        this fleet CPU starvation during capture means dropped FFs -- i.e.
        lost meteors, the same mechanism behind the Bacau/Buzau frame-drop
        investigations. Refuses (409) while this camera's capture is
        active unless the caller passes {"force": true}; the spawned
        subprocess also always runs niced (CPU) and ioniced (disk) so even
        a forced run doesn't compete at full priority against capture on
        other cameras sharing the same box. Daytime (this endpoint reads
        last night's already-ArchivedFiles data) is the natural window for
        this job, so the guard should rarely need overriding in practice.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    autofit_path = CONFIG_PATH.parent / 'platepar_autofit.py'
    if not autofit_path.exists():
        return jsonify({'error': 'platepar_autofit.py not found'}), 404
    try:
        cfg = load_config()
        scfg = cfg.get('stations', {}).get(cam)
        if scfg is None:
            return jsonify({'error': f'unknown camera {cam}'}), 404

        if _autofit_run_in_flight(cfg, cam):
            return jsonify({'error': 'an autofit run is already in progress for this camera'}), 409

        payload = request.get_json(silent=True) or {}
        force = bool(payload.get('force'))
        if not force and _camera_is_capturing(cam, scfg.get('rms_data_path', '')):
            return jsonify({
                'error': 'capture is currently active for this camera; a blind astrometry.net solve '
                         'risks dropped frames. Pass {"force": true} to run anyway.',
            }), 409

        log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = _autofit_log_file(cfg, cam)
        with open(log_file, 'a') as lf:
            proc = subprocess.Popen(
                ['nice', '-n', '15', 'ionice', '-c', '3',
                 sys.executable, str(autofit_path), '-c', str(CONFIG_PATH), '--camera', cam, '--json'],
                start_new_session=True,
                stdout=lf,
                stderr=lf,
            )
        return jsonify({'ok': True, 'pid': proc.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


def _latest_autofit_result(cam: str) -> dict | None:
    """Return the most recently written candidate result JSON for cam, or
    None if no run has ever produced one."""
    cand_dir = Path.home() / 'rovimen_scripts' / 'calibration_candidates' / cam
    if not cand_dir.exists():
        return None
    result_files = sorted(cand_dir.glob('*.json'), key=lambda p: p.stat().st_mtime, reverse=True)
    if not result_files:
        return None
    try:
        return json.loads(result_files[0].read_text())
    except (OSError, ValueError):
        return None


@app.route('/api/calibration/rebase_pointing/<cam>', methods=['POST'])
def api_calibration_rebase_pointing(cam: str):
    """Re-base a camera's platepar onto the pointing RMS recalibrated to last
    night, preserving its distortion model.

    Shares the auto-fit machinery -- same candidate directory, same status
    endpoint, same accept path -- because the output is the same thing: a
    staged platepar awaiting review. What differs is the cost. No fit is
    performed and no blind solve is attempted; it reads
    platepars_all_recalibrated.json and picks the frame that best represents
    the night, so it runs in seconds on the plain station venv rather than
    minutes of astrometry.net on the RMS one.

    That also makes the capture-window guard unnecessary: there is no
    sustained CPU to starve capture with. Single-flight still applies, so a
    re-base cannot race a blind solve on the same camera.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    autofit_path = CONFIG_PATH.parent / 'platepar_autofit.py'
    if not autofit_path.exists():
        return jsonify({'error': 'platepar_autofit.py not found'}), 404
    try:
        cfg = load_config()
        scfg = cfg.get('stations', {}).get(cam)
        if scfg is None:
            return jsonify({'error': f'unknown camera {cam}'}), 404
        if _autofit_run_in_flight(cfg, cam):
            return jsonify({'error': 'a calibration run is already in progress for this camera'}), 409

        log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
        log_dir.mkdir(parents=True, exist_ok=True)
        with open(_autofit_log_file(cfg, cam), 'a') as lf:
            proc = subprocess.Popen(
                ['nice', '-n', '10',
                 sys.executable, str(autofit_path), '-c', str(CONFIG_PATH),
                 '--camera', cam, '--rebase', '--json'],
                start_new_session=True, stdout=lf, stderr=lf,
            )
        return jsonify({'ok': True, 'pid': proc.pid, 'mode': 'rebase'})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/calibration/autofit_status/<cam>')
def api_calibration_autofit_status(cam: str):
    """Poll the result of the most recent autofit run for a camera.

    The trigger endpoint spawns the fit in the background with no direct
    handle back to this request, so "running" is inferred via
    _autofit_run_in_flight -- see its docstring.

    A run that started and then died without writing a result reports
    ``error`` with the tail of its log, rather than the ``no_run`` it used to
    fall through to: "nothing ever ran here" and "the run you just started
    crashed" are different answers and only one of them is actionable.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    latest = _latest_autofit_result(cam)
    log_file = _autofit_log_file(cfg, cam)

    if not log_file.exists():
        if latest is None:
            return jsonify({'status': 'no_run'})
        return jsonify(latest)

    if _autofit_run_in_flight(cfg, cam):
        return jsonify({'status': 'running'})

    # The log outliving the newest result means the last run started and
    # never finished; _autofit_run_in_flight has already ruled out it still
    # being alive.
    newest_result_mtime = 0.0
    cand_dir = Path.home() / 'rovimen_scripts' / 'calibration_candidates' / cam
    if cand_dir.exists():
        results = sorted(cand_dir.glob('*.json'), key=lambda p: p.stat().st_mtime)
        if results:
            newest_result_mtime = results[-1].stat().st_mtime
    if log_file.stat().st_mtime > newest_result_mtime + 1:
        return jsonify({
            'status': 'error',
            'error': 'the auto-fit run exited without writing a result',
            'detail': _autofit_log_tail(cfg, cam) or 'no output was logged',
        })

    if latest is None:
        return jsonify({'status': 'no_run'})
    return jsonify(latest)


@app.route('/api/calibration/autofit_accept/<cam>', methods=['POST'])
def api_calibration_autofit_accept(cam: str):
    """Apply a staged autofit candidate as the live platepar.

    Body: {"candidate_id": "...", "accept_token": "..."}. accept_token must
    match the token minted into that candidate's own result JSON (see
    platepar_autofit.accept_candidate's docstring for why -- PR #884
    review: candidate_id alone was the only check, and it's a predictable
    string). Backs up the current live platepar to a timestamped .bak
    first. Delegates to platepar_autofit.accept_candidate so the CLI
    (--apply) and this endpoint share exactly one code path.

    accept_token binds an accept to a run the caller has actually seen; it
    is not authentication. The tailnet ACL is the real security boundary
    for this endpoint -- anyone who can reach the station API at all is
    already trusted to write here."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    payload = request.get_json(silent=True) or {}
    candidate_id = payload.get('candidate_id', '')
    accept_token = payload.get('accept_token', '')
    if not re.match(r'^[A-Z0-9]+_\d{8}_\d{6}$', candidate_id):
        return jsonify({'status': 'error', 'error': 'invalid or missing candidate_id'}), 400

    cfg = load_config()
    scfg = cfg.get('stations', {}).get(cam)
    if scfg is None:
        return jsonify({'status': 'error', 'error': f'unknown camera {cam}'}), 404

    scripts_dir = str(CONFIG_PATH.parent)
    if scripts_dir not in sys.path:  # once, not a new entry per accept
        sys.path.insert(0, scripts_dir)
    import platepar_autofit
    result = platepar_autofit.accept_candidate(cam, scfg, candidate_id, accept_token=accept_token)
    status_code = 200 if result.get('status') == 'applied' else 409
    return jsonify(result), status_code


@app.route('/api/rms_cameras')
def api_rms_cameras():
    """Camera codes and IPs of every RMS instance on the station (see
    station_collect._rms_config_paths for the layouts scanned)."""
    return jsonify(list_rms_cameras())


@app.route('/api/live_cameras')
def api_live_cameras():
    """Every camera that can produce a live tile (station_collect.list_live_cameras).
    The pusher drives its live-thumbnail loop from this list; /api/rms_cameras
    stays RMS-only for the clip scan."""
    return jsonify(list_live_cameras())


@app.route('/api/rms/plots/<cam>/<date>')
def api_rms_plots(cam: str, date: str):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    cfg = load_config()
    session_dir = _find_rms_session_dir(cam, date, cfg)
    if not session_dir:
        return jsonify([])
    plots: list[dict] = []
    for f in session_dir.iterdir():
        if f.suffix not in ('.jpg', '.png'):
            continue
        label: str | None = None
        order = 999
        for i, suffix in enumerate(_RMS_PLOT_ORDER):
            if f.name.endswith(suffix):
                label = _RMS_PLOT_LABELS[suffix]
                order = i
                break
        if label is None and '_stack_' in f.name and f.name.endswith('.jpg'):
            label = 'Meteor stack'
            order = 2
        if label is None:
            continue
        plots.append({'filename': f.name, 'label': label, 'order': order})
    plots.sort(key=lambda p: p['order'])
    return jsonify(plots)


@app.route('/api/rms/plot_image/<cam>/<date>/<filename>')
def api_rms_plot_image(cam: str, date: str, filename: str):
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(jpg|png)$', filename):
        abort(400)
    cfg = load_config()
    session_dir = _find_rms_session_dir(cam, date, cfg)
    if not session_dir:
        abort(404)
    path = session_dir / filename
    if not path.exists() or not path.is_file():
        abort(404)
    return send_file(str(path), conditional=True)


def _find_latest_captured_stack(cam: str, cfg: dict) -> Path | None:
    """Return the most recently modified *_captured_stack.jpg across all RMS sessions.

    Search ArchivedFiles first (preferred — these are post-processing copies),
    then CapturedFiles. Pool all matches and pick by mtime so we always serve
    the freshest one regardless of which subtree it lives under.
    """
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return None
    rms_dir = Path(rms_path)
    candidates: list[Path] = []
    for sub in ('ArchivedFiles', 'CapturedFiles'):
        sub_dir = rms_dir / sub
        if not sub_dir.exists():
            continue
        for session in sub_dir.iterdir():
            if not session.is_dir() or not session.name.startswith(f'{cam}_'):
                continue
            for f in session.iterdir():
                if f.name.endswith('_captured_stack.jpg'):
                    candidates.append(f)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@app.route('/api/latest_stack/<cam>')
def api_latest_stack(cam: str):
    """Serve the most recent _captured_stack.jpg for a camera, regardless of date.

    Used by the dashboard celestial-dome route to assemble one current-pointing
    background image per camera without walking the night/plots tree from the
    client side.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    cfg = load_config()
    path = _find_latest_captured_stack(cam, cfg)
    if not path:
        abort(404)
    return send_file(str(path), conditional=True)


def _find_latest_chunk_stack(cam: str) -> Path | None:
    """Return the freshest per-chunk colour ``*_stack.webp`` for a camera.

    Walks ``<capture_base>/<cam>/<night>/stacks/`` across all nights and picks
    the most recently modified file matching ``*_stack.webp`` (excluding the
    ``*_avg.webp`` avgpx companions written by ``stacker._finish_stack`` and
    the per-night ``*_color_meteor_stack.webp`` composite).

    This is the time-varying source used by the dashboard sky-dome panorama:
    each capture chunk is ~5 min, so the background rolls forward with the
    night. Falls back to the RMS captured_stack via ``_find_latest_captured_stack``
    in the route handler when no per-chunk colour stack exists yet (early in
    the evening, station with stacker disabled).
    """
    cap_root = capture_base() / cam
    if not cap_root.exists():
        return None
    candidates: list[Path] = []
    try:
        night_dirs = sorted(
            (d for d in cap_root.iterdir() if d.is_dir() and re.match(r'^\d{8}$', d.name)),
            key=lambda d: d.name,
            reverse=True,
        )
    except OSError:
        return None
    # Scan only the two most-recent night directories. Older stacks are stale
    # for "live" purposes, and on stations with many nights the full walk
    # adds avoidable I/O to a frequently-hit endpoint.
    for night_dir in night_dirs[:2]:
        stacks_dir = night_dir / 'stacks'
        if not stacks_dir.is_dir():
            continue
        try:
            for f in stacks_dir.iterdir():
                name = f.name
                if not name.endswith('_stack.webp'):
                    continue
                if name.endswith('_avg.webp'):
                    continue  # avgpx companion, not a display stack
                if name.endswith('_color_meteor_stack.webp'):
                    continue  # per-night composite, not per-chunk
                if f.is_file():
                    candidates.append(f)
        except OSError:
            continue
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


@app.route('/api/latest_frame/<cam>')
def api_latest_frame(cam: str):
    """Return the freshest visual snapshot for a camera, in this order:

      1. Latest per-chunk colour ``*_stack.webp`` (rolling ~5 min, full colour).
      2. RMS ``_captured_stack.jpg`` (per-session FF mosaic, BW, ~10 min).

    Single endpoint for the dashboard "live frame" tile — keeps the
    fallback decision station-side so the dashboard doesn't have to chase
    two 404s round-trip when a camera has no colour stacker output.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    width = request.args.get('w', type=int)
    chunk = _find_latest_chunk_stack(cam)
    if chunk:
        return _send_frame(chunk, width, mimetype='image/webp')
    cfg = load_config()
    capt = _find_latest_captured_stack(cam, cfg)
    if capt:
        return _send_frame(capt, width)
    abort(404)


def _send_frame(path: Path, width: int | None, mimetype: str | None = None):
    """Send ``path``, or a WebP copy at most ``width`` px wide (``?w=``, 64-4096).

    The pusher asks for a small copy to push the dashboard's sky dome input
    (#1249); the dome reprojects onto the platepar's resolution anyway. The
    original's mtime rides along as ``X-Frame-Time`` (UTC ISO) either way.
    """
    mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
    stamp = mtime.isoformat(timespec='seconds').replace('+00:00', 'Z')
    if width is not None and 64 <= width <= 4096:
        try:
            from PIL import Image
            with Image.open(path) as img:
                img = img.convert('RGB')
                if img.width > width:
                    img = img.resize((width, max(1, round(img.height * width / img.width))),
                                     Image.LANCZOS)
                buf = io.BytesIO()
                img.save(buf, format='WEBP', quality=80, method=4)
            resp = Response(buf.getvalue(), mimetype='image/webp')
            resp.headers['X-Frame-Time'] = stamp
            return resp
        except Exception as exc:
            app.logger.warning('latest_frame resize of %s failed, sending original: %s', path, exc)
    resp = send_file(str(path), conditional=True, mimetype=mimetype)
    resp.headers['X-Frame-Time'] = stamp
    return resp


@app.route('/api/color-meteor-stack/<cam>/<date>')
def api_color_meteor_stack(cam: str, date: str):
    """Serve the per-night colour meteor stack built by
    ``stacker.build_night_color_meteor_stack`` during dawn processing."""
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    path = capture_base() / cam / date / 'stacks' / f'{cam}_{date}_color_meteor_stack.webp'
    if not path.exists():
        abort(404)
    return send_file(str(path), conditional=True, mimetype='image/webp')


# ---------------------------------------------------------------------------
# GET /api/logo/list
# ---------------------------------------------------------------------------

_ASSETS_DIR = BASE / 'rovimen_scripts' / 'assets'
_LOGO_EXTS  = {'.png', '.jpg', '.jpeg', '.webp', '.bmp', '.gif'}


@app.route('/api/logo/list')
def api_logo_list():
    """List image files available in the assets directory."""
    if not _ASSETS_DIR.exists():
        return jsonify([])
    files = sorted(
        f.name for f in _ASSETS_DIR.iterdir()
        if f.is_file() and f.suffix.lower() in _LOGO_EXTS
    )
    return jsonify([{'filename': f, 'path': str(_ASSETS_DIR / f)} for f in files])


@app.route('/api/overlay/logo', methods=['POST'])
def api_overlay_logo():
    """Point ``overlay.logo`` at one of the images in the assets directory.

    Body ``{"filename": "<name>"}``: a bare file name that /api/logo/list
    returns, nothing else. It changes one config key to a file the bundle
    shipped, so it is reversible from the same dropdown and is network
    allowlisted for the dashboard (see _NETWORK_ALLOWED_ENDPOINTS)."""
    body = request.get_json(silent=True) or {}
    name = body.get('filename')
    if not isinstance(name, str) or not name or Path(name).name != name:
        return jsonify({'error': 'filename must be a bare file name'}), 400
    path = _ASSETS_DIR / name
    if path.suffix.lower() not in _LOGO_EXTS or not path.is_file():
        return jsonify({'error': f'no logo {name!r} in {_ASSETS_DIR}'}), 404
    try:
        config_patch.patch_config({'overlay': {'logo': str(path)}}, CONFIG_PATH)
    except Exception as e:
        logger.exception('POST /api/overlay/logo failed')
        return jsonify({'error': str(e)}), 500
    with _cache_lock:
        _cache.pop('config', None)
    logger.info('overlay.logo set to %s from %s', path, request.remote_addr)
    return jsonify({'ok': True, 'logo': str(path)})


# ---------------------------------------------------------------------------
# GET /api/storagewatch/status
# ---------------------------------------------------------------------------

_STORAGEWATCH_STATE = BASE / 'logs' / 'janitor_state.json'


@app.route('/api/storagewatch/status')
def api_storagewatch_status():
    """Return janitor last-run state written by janitor_storage_watchdog.py."""
    if not _STORAGEWATCH_STATE.exists():
        return jsonify({'error': 'no_state'}), 404
    try:
        return jsonify(json.loads(_STORAGEWATCH_STATE.read_text()))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# POST /api/encode_chunk
# ---------------------------------------------------------------------------

@app.route('/api/encode_chunk', methods=['POST'])
def api_encode_chunk():
    """Encode a single raw MKV chunk in-place (overlay, colour cal, rotation).

    Body JSON: {"camera": "RO000H", "date": "20260326", "filename": "...mkv"}
    Returns 200 {"ok": true} on success, or 4xx/5xx with {"error": "..."}.
    Idempotent: if .reencoded sidecar already exists, returns 200 immediately.
    """
    data = request.get_json(silent=True) or {}
    cam      = data.get('camera', '')
    date     = data.get('date', '')
    filename = data.get('filename', '')
    if not re.match(r'^[A-Z0-9]+$', cam):
        return jsonify({'error': 'invalid camera'}), 400
    if not re.match(r'^\d{8}$', date):
        return jsonify({'error': 'invalid date'}), 400
    if not re.match(r'^[\w.\-]+\.mkv$', filename):
        return jsonify({'error': 'invalid filename'}), 400

    mkv = capture_base() / cam / date / filename
    if not mkv.exists():
        return jsonify({'error': 'file not found'}), 404

    try:
        import encoder as _encoder  # local import — only needed for this endpoint
        cfg = load_config()
        if _segment_open(cam, date, mkv, cfg):
            retry = int(_segment_window_s(cfg))
            return jsonify({'ok': False, 'retry_after': retry,
                            'error': f'colour capture is still writing this chunk; '
                                     f'try again in {retry} s'}), 409
        ok  = _encoder.process_chunk(mkv, cfg)
        if not ok:
            return jsonify({'ok': False, 'error': 'encoder skipped chunk (not ready or already encoded)'}), 422
        return jsonify({'ok': True})
    except Exception as e:
        logger.exception('encode_chunk failed: %s', e)
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# File serving  (with HTTP Range support for video seeking)
# ---------------------------------------------------------------------------

THUMB_W = 480  # max thumbnail width (px)
THUMB_Q = 80   # lossy WebP quality


def _serve(path: Path):
    if not path.exists():
        abort(404)
    return send_file(path, conditional=True)


def _make_thumbnail(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-y', '-i', str(src),
         '-vf', f'scale={THUMB_W}:-2',
         '-quality', str(THUMB_Q),
         str(dst)],
        capture_output=True, check=True, timeout=30,
    )


def _make_thumbnail_from_video(mkv: Path, dst: Path) -> None:
    """Extract a single frame from the middle of an MKV and save as WebP thumbnail."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ['ffmpeg', '-y', '-ss', '10', '-i', str(mkv),
         '-frames:v', '1', '-vf', f'scale={THUMB_W}:-2',
         '-quality', str(THUMB_Q),
         str(dst)],
        capture_output=True, check=True, timeout=30,
    )


def _find_ff_maxpixels(cam, date, time_str):
    """Find ALL FF FITS files that overlap a 20s chunk window.

    FF timestamps are always UTC (RMS names them from the UTC clock).
    Returns a list of FF paths covering the chunk's time range.
    """
    cfg = load_config()
    station_cfg = cfg.get('stations', {}).get(cam, {})
    rms_path = station_cfg.get('rms_data_path')
    if not rms_path:
        return []
    segment_duration = cfg.get('segment_duration', 20)

    # Match window in UTC epoch terms (see ff_thumbs.find_ff_maxpixels):
    # FF names carry UTC dates, so the module derives the searched dates
    # from the window endpoints, covering the midnight boundary.
    return ff_thumbs.find_ff_maxpixels(
        cam, Path(rms_path), _chunk_utc_dt(date, time_str, cfg),
        segment_duration=segment_duration,
    )


def _ensure_rms_venv_on_path() -> bool:
    """Add RMS venv site-packages and RMS source to sys.path. Returns True if numpy available."""
    return ff_thumbs.ensure_rms_venv_on_path(BASE)


def _read_ff_maxpixel(ff_path: Path):
    """Read maxpixel data from an FF FITS file. Returns numpy array or None."""
    return ff_thumbs.read_ff_maxpixel(ff_path)


@app.route('/api/latest_ff_maxpixel/<cam>')
def api_latest_ff_maxpixel(cam: str):
    """Single newest FF maxpixel for <cam>, BW WebP, no disk write. 404 if none.

    Used by the dashboard's "Sky right now" widget on the Video DB tab —
    one FF covers ~10 s of sky, so polling this every minute keeps the
    live celestial dome rolling forward without waiting for the ~5 min
    per-chunk colour stacker pass.
    """
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    ff = _find_latest_ff(cam)
    if ff is None:
        # No RMS on this camera: a frame from its newest colour chunk.
        cfg = load_config()
        scfg = cfg.get('stations', {}).get(cam)
        if not isinstance(scfg, dict) or not scfg.get('color_capture', True):
            abort(404)
        got = _latest_colour_frame(cam)
        if got is None:
            abort(404)
        data, chunk = got
        resp = send_file(io.BytesIO(data), mimetype='image/webp')
        resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
        resp.headers['Pragma'] = 'no-cache'
        resp.headers['X-Thumb-Kind'] = 'colour-frame'
        m = re.match(r'^[A-Z0-9]+_(\d{8})_(\d{6})', chunk.name)
        if m:
            t = _chunk_utc_dt(m.group(1), m.group(2), cfg)
            resp.headers['X-FF-Timestamp'] = t.strftime('%Y-%m-%dT%H:%M:%S.000Z')
            resp.headers['Access-Control-Expose-Headers'] = 'X-FF-Timestamp'
        return resp
    try:
        st = ff.stat()
        data = _latest_ff_webp_bytes(str(ff), st.st_mtime_ns, st.st_size)
    except (OSError, _FFRenderFailed):
        abort(503)
    resp = send_file(io.BytesIO(data), mimetype='image/webp')
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    resp.headers['Pragma'] = 'no-cache'
    # FF filename format: FF_<STATION>_<YYYYMMDD>_<HHMMSS>_<MS>_<FRAMENUMBER>.fits
    # Surface the capture timestamp to the dashboard so it can show a
    # freshness indicator next to the dome image.
    m = re.match(r'^FF_[A-Z0-9]+_(\d{8})_(\d{6})_(\d{3})', ff.name)
    if m:
        d, t, ms = m.group(1), m.group(2), m.group(3)
        resp.headers['X-FF-Timestamp'] = (
            f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}.{ms}Z"
        )
        resp.headers['Access-Control-Expose-Headers'] = 'X-FF-Timestamp'
    return resp


@app.route('/thumbnail/<cam>/<date>/<filename>')
def serve_thumbnail(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.webp$', filename):
        abort(400)

    cap_dir = capture_base() / cam / date
    src = cap_dir / filename
    dst = cap_dir / 'thumbs' / filename

    # Color stack may be at flat path (legacy) or stacks/ subdir (new layout)
    src_subdir = cap_dir / 'stacks' / filename
    color_stack = src if src.exists() else (src_subdir if src_subdir.exists() else None)

    if not dst.exists():
        if color_stack is not None:
            # Stack exists — generate thumbnail from it
            try:
                _make_thumbnail(color_stack, dst)
            except subprocess.CalledProcessError:
                abort(500)
        else:
            stem = filename.replace('_stack.webp', '')
            mkv = cap_dir / f'{stem}_color.mkv'

            # No color stack — serve FF BW maxpixel directly from memory (no disk write)
            m = re.match(r'^([A-Z0-9]+)_(\d{8})_(\d{6})$', stem)
            if m:
                ffs = _find_ff_maxpixels(m.group(1), m.group(2), m.group(3))
                if ffs:
                    buf = _ff_maxpixel_to_webp(ffs)
                    if buf:
                        return send_file(buf, mimetype='image/webp')

            # Fallback: extract a frame from the MKV
            if not mkv.exists():
                abort(404)
            try:
                _make_thumbnail_from_video(mkv, dst)
            except subprocess.CalledProcessError:
                abort(500)

    return send_file(dst, conditional=True)


@app.route('/color_capture/<cam>/<date>/<filename>')
def serve_capture(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(mkv|webp)$', filename):
        abort(400)
    # Check flat layout first, then videos/ and stacks/ subdirectories
    base = capture_base() / cam / date
    path = base / filename
    if not path.exists():
        path = base / 'videos' / filename
    if not path.exists():
        path = base / 'stacks' / filename
    if path.exists():
        # Overlay gate: a color chunk served for download/playback must carry
        # the burned-in overlay. Render it on demand rather than exposing raw
        # footage (webp stacks/thumbnails are unaffected). Covers single-chunk
        # and boundary-stitched color names alike.
        if filename.endswith('_color.mkv'):
            if not _ensure_overlaid(cam, date, path, load_config()):
                abort(503)
        return _serve(path)

    # No stack on disk — serve FF BW maxpixel directly from memory
    if filename.endswith('_stack.webp'):
        stem = filename[:-len('_stack.webp')]
        m = re.match(r'^([A-Z0-9]+)_(\d{8})_(\d{6})$', stem)
        if m:
            ffs = _find_ff_maxpixels(m.group(1), m.group(2), m.group(3))
            if ffs:
                buf = _ff_maxpixel_to_webp(ffs)
                if buf:
                    return send_file(buf, mimetype='image/webp')

    return _serve(path)  # triggers 404


@app.route('/color_timelapse/<cam>/<date>/<filename>')
@app.route('/night_stack/<cam>/<date>/<filename>')
def serve_timelapse(cam, date, filename):
    for part in (cam, date, filename):
        if '..' in part or '/' in part:
            abort(400)
    if not re.match(r'^[A-Z0-9]+$', cam):
        abort(400)
    if not re.match(r'^\d{8}$', date):
        abort(400)
    if not re.match(r'^[\w._-]+\.(mp4|webp)$', filename):
        abort(400)
    return _serve(capture_base() / cam / date / filename)


# ---------------------------------------------------------------------------
# Live MJPEG stream — called by the dashboard instead of SSH+shell ffmpeg
# ---------------------------------------------------------------------------

_STREAM_SEMS: dict[str, threading.Semaphore] = {}
_STREAM_SEM_LOCK = threading.Lock()
_STREAM_MAX_PER_CAM = 2
_STREAM_GLOBAL_SEM = threading.Semaphore(6)

_CAM_CODE_RE = re.compile(r'^[A-Z0-9]+$', re.IGNORECASE)
_IP_RE = re.compile(r'^(\d{1,3}\.){3}\d{1,3}$')


def _stream_sem(cam: str) -> threading.Semaphore:
    with _STREAM_SEM_LOCK:
        if cam not in _STREAM_SEMS:
            _STREAM_SEMS[cam] = threading.Semaphore(_STREAM_MAX_PER_CAM)
        return _STREAM_SEMS[cam]


@app.route('/api/stream/<cam_code>')
def api_stream(cam_code: str):
    """Stream MJPEG from a camera's RTSP feed via local ffmpeg.

    The dashboard calls this over HTTP instead of constructing a shell
    command string passed through SSH — eliminating shell-injection risk.
    """
    if not _CAM_CODE_RE.match(cam_code):
        abort(400)

    cfg = load_config()
    stations = cfg.get('stations', {})
    cam_entry = None
    for _sid, scfg in stations.items():
        if isinstance(scfg, dict) and scfg.get('station_id') == cam_code:
            cam_entry = scfg
            break
        if isinstance(scfg, dict):
            cam_ip = scfg.get('cam_ip', '')
            if scfg.get('station_id') == cam_code or _sid == cam_code:
                cam_entry = scfg
                break

    cam_ip = None
    rtsp_url = None
    rotate = False

    if cam_entry:
        cam_ip = cam_entry.get('cam_ip', '')
        # The URL colour capture reads (color_capture.py): the loopback relay
        # when the station has one, so a live view is not a second pull on
        # the camera itself.
        rtsp_url = (cam_entry.get('capture_rtsp') or cam_entry.get('rtsp_url')
                    or cam_entry.get('camera_rtsp'))
        rotate = bool(cam_entry.get('rotate', False))

    if not cam_ip and not rtsp_url:
        for i in range(1, 9):
            cfg_path = BASE / f'RMS_cam{i}' / '.config'
            if cfg_path.exists():
                try:
                    for line in cfg_path.read_text().splitlines():
                        line_s = line.strip()
                        if line_s.startswith('stationID'):
                            sid = line_s.split(':', 1)[-1].strip()
                            if sid == cam_code:
                                for l2 in cfg_path.read_text().splitlines():
                                    l2s = l2.strip()
                                    if l2s.startswith('camera_ip'):
                                        cam_ip = l2s.split(':', 1)[-1].strip()
                                    elif l2s.startswith('device') and not cam_ip:
                                        cam_ip = rovimen_rms.camera_ip_from_device(l2s)
                                break
                except Exception:
                    pass

    if not rtsp_url:
        if not cam_ip or not _IP_RE.match(cam_ip):
            return jsonify({'error': 'camera not found or invalid IP'}), 404
        rtsp_url = (
            f"rtsp://admin:@{cam_ip}:554/"
            f"user=admin&password=&channel=1&stream=0.sdp"
        )

    rotate_q = request.args.get('rotate', '0')
    if rotate_q == '1':
        rotate = True

    vf = "scale=854:480,vflip,hflip" if rotate else "scale=854:480"

    per_cam = _stream_sem(cam_code)
    if not per_cam.acquire(blocking=False):
        return jsonify({'error': 'too_many_streams', 'scope': 'per_camera'}), 429
    if not _STREAM_GLOBAL_SEM.acquire(blocking=False):
        per_cam.release()
        return jsonify({'error': 'too_many_streams', 'scope': 'global'}), 429

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-rtsp_transport", "tcp",
        "-i", rtsp_url,
        "-vf", vf,
        "-r", "12", "-q:v", "5",
        "-f", "mjpeg", "pipe:1",
    ]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        per_cam.release()
        _STREAM_GLOBAL_SEM.release()
        return jsonify({'error': 'ffmpeg failed to start'}), 500

    import selectors

    def generate():
        buf = b""
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ)
        try:
            while True:
                ready = sel.select(timeout=30)
                if not ready:
                    break
                chunk = proc.stdout.read(32768)
                if not chunk:
                    break
                buf += chunk
                while True:
                    soi = buf.find(b"\xff\xd8")
                    if soi == -1:
                        buf = b""
                        break
                    eoi = buf.find(b"\xff\xd9", soi + 2)
                    if eoi == -1:
                        buf = buf[soi:]
                        break
                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/jpeg\r\n\r\n"
                        + frame + b"\r\n"
                    )
        finally:
            sel.close()
            proc.kill()
            proc.wait()
            per_cam.release()
            _STREAM_GLOBAL_SEM.release()

    return Response(
        stream_with_context(generate()),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route('/api/detections-index')
def api_detections_index():
    """Return all indexed detections for dates >= ?since=YYYYMMDD.

    The VPS index_poller calls this endpoint every few minutes (for stations
    that do not push) and merges results into the fleet-wide detections.db.
    """
    since = request.args.get('since', '')
    if since and not re.match(r'^\d{8}$', since):
        abort(400, description='since must be YYYYMMDD')
    return jsonify(read_detections_index(since))


# ---------------------------------------------------------------------------

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=7779, threaded=True)
