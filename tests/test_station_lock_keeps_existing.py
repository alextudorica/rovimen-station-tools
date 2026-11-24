"""POST /api/lock keeps an existing lock and uploads one chunk at a time.

The dashboard locks every clip a signed-in user plays (/api/keep), so a lock
request now often hits a chunk RMS already locked. Overwriting that lock with
a manual one would drop its detection and meteor times; and every lock starts
a re-encode + upload, so bursts must not become parallel ffmpeg runs on the
station (station_api._upload_locked_chunk shares process_clip's serial lock).
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import flags_manager
import station_api

CAM = "RO000H"
DATE = "20260101"
FILENAME = "RO000H_20260101_000000_color.mkv"
PATH = f"/api/lock/{CAM}/{DATE}/{FILENAME}"


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def env(monkeypatch, tmp_path):
    station_api.app.testing = True
    cap_dir = tmp_path / CAM / DATE
    cap_dir.mkdir(parents=True)
    (cap_dir / FILENAME).write_bytes(b"mkv")
    # A closed chunk (one still being written: test_station_segment_in_progress).
    old = time.time() - 120
    os.utime(cap_dir / FILENAME, (old, old))
    cfg = {"videocapture_path": str(tmp_path)}
    uploads: list[bool] = []

    def _upload(*a):
        # Records whether the serial lock was held during the upload.
        uploads.append(station_api._process_clip_serial.locked())

    monkeypatch.setattr(station_api, "capture_base", lambda: tmp_path)
    monkeypatch.setattr(station_api, "load_config", lambda: cfg, raising=False)
    monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", True)
    monkeypatch.setattr(station_api, "_flags_manager", flags_manager, raising=False)
    monkeypatch.setattr(flags_manager, "_state_path",
                        lambda s, d, c: tmp_path / s / d / "state.json", raising=False)
    monkeypatch.setattr(station_api.archive_upload, "upload_chunk", _upload)
    monkeypatch.setattr(station_api.threading, "Thread", _InlineThread)
    return station_api.app.test_client(), cfg, uploads


def _lock_of(cfg: dict) -> dict | None:
    return flags_manager.load(CAM, DATE, cfg)["chunks"][FILENAME]["lock"]


def test_unlocked_chunk_gets_a_manual_lock_and_a_serial_upload(env):
    client, cfg, uploads = env
    r = client.post(PATH, json={"locked": True})
    assert r.get_json() == {"locked": True, "lock_type": "manual"}
    assert _lock_of(cfg)["lock_type"] == "manual"
    assert uploads == [True]


def test_detection_lock_is_kept(env):
    client, cfg, uploads = env
    det = {"lock_type": "detection", "detection_time": "20260101_000005",
           "meteor_time": "20260101_000006.120"}
    flags_manager.lock_chunk(CAM, DATE, FILENAME, det, cfg)
    r = client.post(PATH, json={"locked": True})
    assert r.get_json() == {"locked": True, "lock_type": "detection"}
    assert _lock_of(cfg) == det
    assert uploads == []


def test_second_lock_of_a_manual_lock_is_a_no_op(env):
    client, cfg, uploads = env
    client.post(PATH, json={"locked": True})
    first = _lock_of(cfg)
    r = client.post(PATH, json={"locked": True})
    assert r.get_json() == {"locked": True, "lock_type": "manual"}
    assert _lock_of(cfg) == first
    assert uploads == [True]
