"""Tests for POST /api/process_clip/<cam>/<date>/<filename> (station_api.py).

The endpoint delivers ONE clip to the archive: it locks the chunk if it is not
already locked, then kicks archive_upload.upload_chunk (re-encode + upload) in
a background thread. Its readonly-guard exemption is covered separately in
test_station_readonly_guard.py; here we test the handler itself.

Invariants under test:
  * malformed cam / date / filename -> 400
  * missing chunk file -> 404
  * unlocked chunk -> a fresh manual lock is created and the upload kicked
  * already-locked chunk -> the existing lock is PRESERVED (never overwritten,
    so a detection lock keeps its meteor_time) and the upload is still kicked
  * single-flight: a second POST while a delivery is in flight answers 202
    without kicking another upload; the key is released when delivery ends
"""

from __future__ import annotations

import os
import sys
import time
import types
from pathlib import Path

import pytest

import station_api

CAM = "RO000H"
DATE = "20260101"
FILENAME = "RO000H_20260101_000000_color.mkv"
PATH = f"/api/process_clip/{CAM}/{DATE}/{FILENAME}"


class _FlagsStub:
    """Minimal flags_manager stand-in: canned lock state + lock_chunk recorder."""

    def __init__(self, lock: dict | None = None):
        self._lock = lock
        self.chunk: dict = {}     # extra per-chunk state (reencoded/uploaded)
        self.lock_calls: list[tuple] = []

    def load(self, cam, date, cfg):
        if self._lock is None and not self.chunk:
            return {"chunks": {}}
        entry = dict(self.chunk)
        if self._lock is not None:
            entry["lock"] = dict(self._lock)
        return {"chunks": {FILENAME: entry}}

    def lock_chunk(self, cam, date, filename, info, cfg, ready=True):
        self.lock_calls.append((cam, date, filename, info))


class _InlineThread:
    """threading.Thread stand-in that runs the target synchronously on start(),
    so the delivery (and its finally-cleanup) completes before the response is
    asserted."""

    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target:
            self._target(*self._args, **self._kwargs)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Test client + patched collaborators. Returns (client, flags, uploads)."""
    station_api.app.testing = True
    cap_dir = tmp_path / CAM / DATE
    cap_dir.mkdir(parents=True)
    (cap_dir / FILENAME).write_bytes(b"mkv")
    # A closed chunk: one written within a segment is delivered once it
    # closes (tests/test_station_segment_in_progress.py).
    old = time.time() - 120
    os.utime(cap_dir / FILENAME, (old, old))

    flags = _FlagsStub()
    uploads: list[tuple] = []

    monkeypatch.setattr(station_api, "capture_base", lambda: tmp_path)
    monkeypatch.setattr(station_api, "load_config", lambda: {}, raising=False)
    monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", True)
    monkeypatch.setattr(station_api, "_flags_manager", flags, raising=False)
    monkeypatch.setattr(
        station_api.archive_upload, "upload_chunk",
        lambda *a, **k: uploads.append(a),
    )
    monkeypatch.setattr(station_api.threading, "Thread", _InlineThread)
    return station_api.app.test_client(), flags, uploads


# ── validation ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        f"/api/process_clip/ro000h/{DATE}/{FILENAME}",            # lowercase cam
        f"/api/process_clip/{CAM}/2026-01-01/{FILENAME}",          # dashed date
        f"/api/process_clip/{CAM}/{DATE}/notachunk.mkv",           # not CHUNK_RE
        f"/api/process_clip/{CAM}/{DATE}/{CAM}_{DATE}_000000_color.mp4",  # mp4
    ],
)
def test_malformed_params_rejected(env, path):
    client, _flags, uploads = env
    assert client.post(path).status_code == 400
    assert uploads == []


def test_missing_chunk_404(env):
    client, _flags, uploads = env
    resp = client.post(
        f"/api/process_clip/{CAM}/{DATE}/{CAM}_{DATE}_235959_color.mkv"
    )
    assert resp.status_code == 404
    assert uploads == []


# ── unlocked chunk: lock created + upload kicked ─────────────────────────────


def test_unlocked_chunk_gets_manual_lock_and_upload(env, tmp_path):
    client, flags, uploads = env
    resp = client.post(PATH)
    assert resp.status_code == 200
    assert resp.get_json() == {"ok": True, "locked": True, "lock_type": "manual"}

    assert len(flags.lock_calls) == 1
    cam, date, filename, info = flags.lock_calls[0]
    assert (cam, date, filename) == (CAM, DATE, FILENAME)
    assert info["lock_type"] == "manual"
    assert info["meteor_time"] is None

    assert len(uploads) == 1
    u_cam, u_date, u_mkv, _cfg = uploads[0]
    assert (u_cam, u_date) == (CAM, DATE)
    assert Path(u_mkv) == tmp_path / CAM / DATE / FILENAME


# ── locked chunk: existing lock preserved ────────────────────────────────────


def test_detection_lock_preserved(env):
    client, flags, uploads = env
    flags._lock = {
        "lock_type": "detection",
        "detection_time": "20260101_000010",
        "meteor_time": "20260101_000012.345",
    }
    resp = client.post(PATH)
    assert resp.status_code == 200
    assert resp.get_json() == {
        "ok": True, "locked": True, "lock_type": "detection",
    }
    # The existing lock must never be overwritten — meteor_time drives the
    # shortclip seek offset and a manual re-lock would blank it.
    assert flags.lock_calls == []
    assert len(uploads) == 1


def test_manual_lock_preserved(env):
    client, flags, uploads = env
    flags._lock = {"lock_type": "manual", "detection_time": "20260101_000010",
                   "meteor_time": None}
    resp = client.post(PATH)
    assert resp.status_code == 200
    assert resp.get_json()["lock_type"] == "manual"
    assert flags.lock_calls == []
    assert len(uploads) == 1


# ── single-flight ────────────────────────────────────────────────────────────


def test_second_post_while_running_answers_202(env, monkeypatch):
    client, _flags, uploads = env
    key = f"{CAM}/{DATE}/{FILENAME}"
    # Simulate an in-flight delivery.
    with station_api._process_clip_guard:
        station_api._process_clip_active.add(key)
    try:
        resp = client.post(PATH)
        assert resp.status_code == 202
        assert resp.get_json() == {"ok": True, "running": True}
        assert uploads == []
    finally:
        with station_api._process_clip_guard:
            station_api._process_clip_active.discard(key)


def test_key_released_after_delivery(env):
    client, _flags, uploads = env
    assert client.post(PATH).status_code == 200
    # The inline thread ran the delivery synchronously, so the key must be
    # free again: a second POST kicks a second upload rather than 202ing.
    assert client.post(PATH).status_code == 200
    assert len(uploads) == 2


def test_key_released_when_upload_raises(env, monkeypatch):
    client, _flags, _uploads = env

    def _boom(*_a, **_k):
        raise RuntimeError("encoder exploded")

    monkeypatch.setattr(station_api.archive_upload, "upload_chunk", _boom)
    # The request still succeeds (delivery is fire-and-forget) and the
    # single-flight key is released by the finally block.
    assert client.post(PATH).status_code == 200
    assert not station_api._process_clip_active


# ── already processed: refused unless forced ─────────────────────────────────


def test_reencoded_and_uploaded_chunk_is_refused(env):
    client, flags, uploads = env
    flags.chunk = {"reencoded": True, "uploaded": True}
    resp = client.post(PATH)
    assert resp.status_code == 409
    assert resp.get_json()["processed"] is True
    assert uploads == []
    assert flags.lock_calls == []


def test_legacy_reencoded_sidecar_counts_as_reencoded(env, tmp_path):
    client, flags, uploads = env
    flags.chunk = {"uploaded": True}
    (tmp_path / CAM / DATE / (FILENAME + ".reencoded")).write_text("")
    assert client.post(PATH).status_code == 409
    assert uploads == []


def test_reencoded_but_not_uploaded_is_still_delivered(env):
    """Delivery is what process_clip is for: an encode that never reached
    the archive is not processed yet."""
    client, flags, uploads = env
    flags.chunk = {"reencoded": True, "uploaded": False}
    assert client.post(PATH).status_code == 200
    assert len(uploads) == 1


def test_force_re_delivers_a_processed_chunk(env):
    client, flags, uploads = env
    flags.chunk = {"reencoded": True, "uploaded": True}
    assert client.post(PATH + "?force=1").status_code == 200
    assert len(uploads) == 1


# ── ?idle=1: the dashboard's automatic requests wait for dawn ───────────────


def test_idle_request_is_refused_while_dawn_runs(env, monkeypatch):
    client, flags, uploads = env
    monkeypatch.setattr(station_api.rovimen_night, "dawn_running", lambda: True)
    resp = client.post(PATH + "?idle=1")
    assert resp.status_code == 503
    assert resp.get_json()["busy"] == "dawn"
    assert int(resp.headers["Retry-After"]) > 0
    assert uploads == [] and flags.lock_calls == []


def test_idle_request_runs_once_dawn_is_done(env, monkeypatch):
    client, _flags, uploads = env
    monkeypatch.setattr(station_api.rovimen_night, "dawn_running", lambda: False)
    assert client.post(PATH + "?idle=1").status_code == 200
    assert len(uploads) == 1


def test_process_click_does_not_wait_for_dawn(env, monkeypatch):
    client, _flags, uploads = env
    monkeypatch.setattr(station_api.rovimen_night, "dawn_running", lambda: True)
    assert client.post(PATH).status_code == 200
    assert len(uploads) == 1


def test_processed_clip_answers_409_even_while_dawn_runs(env, monkeypatch):
    """The requester learns a clip is done without waiting for dawn."""
    client, flags, _uploads = env
    flags.chunk = {"reencoded": True, "uploaded": True}
    monkeypatch.setattr(station_api.rovimen_night, "dawn_running", lambda: True)
    assert client.post(PATH + "?idle=1").status_code == 409


def test_deliveries_encode_one_at_a_time(env, monkeypatch):
    client, _flags, _uploads = env
    held: list[bool] = []
    monkeypatch.setattr(
        station_api.archive_upload, "upload_chunk",
        lambda *a, **k: held.append(station_api._process_clip_serial.locked()),
    )
    assert client.post(PATH).status_code == 200
    assert held == [True]
    assert not station_api._process_clip_serial.locked()


# ── GET: read-only status, and the requester's capability probe ─────────────


def test_status_reports_without_side_effects(env, monkeypatch):
    client, flags, uploads = env
    monkeypatch.setattr(station_api.rovimen_night, "dawn_running", lambda: True)
    resp = client.get(PATH)
    assert resp.status_code == 200
    body = resp.get_json()
    assert body["exists"] is True and body["processed"] is False
    assert body["dawn_running"] is True and body["idle_supported"] is True
    assert uploads == [] and flags.lock_calls == []


@pytest.mark.parametrize("cfg, at_dawn", [
    ({}, True),
    ({"services": {"reencode": {"meteor_clips": False}}}, False),
])
def test_status_says_whether_dawn_encodes_meteor_clips(env, monkeypatch, cfg, at_dawn):
    client, _flags, _uploads = env
    monkeypatch.setattr(station_api, "load_config", lambda: cfg, raising=False)
    assert client.get(PATH).get_json()["clips_at_dawn"] is at_dawn


def test_status_of_a_missing_chunk_is_200_not_404(env):
    """404 would read like an old station; exists=false says the chunk is gone."""
    client, _flags, _uploads = env
    resp = client.get(f"/api/process_clip/{CAM}/{DATE}/{CAM}_{DATE}_235959_color.mkv")
    assert resp.status_code == 200
    assert resp.get_json()["exists"] is False


# ── boundary splices ─────────────────────────────────────────────────────────

# detection_lock writes a splice into the night directory under the clock date
# of its start, so a post-midnight splice carries the next day's date.
SPLICE = f"{CAM}_20260102_013705_013723_color.mkv"


def test_splice_status_is_reported(env, tmp_path):
    """The requester probes with the event's clip, which is the splice for a
    meteor at a chunk edge; a 400 here stopped every request to the station."""
    client, _flags, _uploads = env
    (tmp_path / CAM / DATE / SPLICE).write_bytes(b"mkv")
    resp = client.get(f"/api/process_clip/{CAM}/{DATE}/{SPLICE}")
    assert resp.status_code == 200
    assert resp.get_json()["exists"] is True


def test_splice_is_stacked_and_delivered(env, tmp_path, monkeypatch):
    """The stacker takes splices (it skips one whose merged stack exists), so a
    delivered splice gets a stack even where detection_lock could not merge one."""
    client, flags, uploads = env
    (tmp_path / CAM / DATE / SPLICE).write_bytes(b"mkv")
    old = time.time() - 120
    os.utime(tmp_path / CAM / DATE / SPLICE, (old, old))
    stacked: list[str] = []
    monkeypatch.setitem(sys.modules, "stacker",
                        types.SimpleNamespace(process_chunk=lambda mkv, cfg: stacked.append(mkv.name)))
    resp = client.post(f"/api/process_clip/{CAM}/{DATE}/{SPLICE}")
    assert resp.status_code == 200
    assert [Path(u[2]).name for u in uploads] == [SPLICE]
    assert [c[2] for c in flags.lock_calls] == [SPLICE]
    assert stacked == [SPLICE]


def test_missing_splice_is_404(env):
    client, _flags, uploads = env
    assert client.post(f"/api/process_clip/{CAM}/{DATE}/{SPLICE}").status_code == 404
    assert uploads == []
