"""services.stacker.scope "detections" and on-demand details (gmnro05 (Bistrița)).

A CPU-starved station stacks only its locked chunks at dawn, renders no details
clips for meteor clips that are encoded on demand, and builds both when a clip
is delivered through process_clip.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import types
from pathlib import Path

import numpy as np
import pytest

import dawn_process
import flags_manager
import overlay_details
import stacker
import station_api

CAM = "RO0003"
NIGHT = "20260927"


def _cfg(tmp_path: Path, *, scope=None, meteor_clips=True, **extra) -> dict:
    stacker_cfg = {"enabled": True}
    if scope is not None:
        stacker_cfg["scope"] = scope
    cfg = {
        "videocapture_path": str(tmp_path / "cap"),
        "stations": {CAM: {"rotate": False, "color_adaptive_stack": {"enabled": False}}},
        "overlay": {"enabled": False},
        "stack_workers": 1,
        "services": {
            "stacker": stacker_cfg,
            "reencode": {"enabled": True, "meteor_clips": meteor_clips},
            "overlay_details": {"enabled": True},
            "timelapse_build": {"enabled": True},
        },
    }
    cfg.update(extra)
    return cfg


def _chunk(cfg: dict, hhmmss: str, *, locked: bool = False) -> Path:
    night = Path(cfg["videocapture_path"]) / CAM / NIGHT
    night.mkdir(parents=True, exist_ok=True)
    mkv = night / f"{CAM}_{NIGHT}_{hhmmss}_color.mkv"
    mkv.write_bytes(b"\x00" * 1024)
    flags_manager.mark_ready(CAM, NIGHT, mkv.name, cfg)
    if locked:
        # A legacy .locked sidecar: chunk_is_locked still honours one.
        mkv.with_suffix(".mkv.locked").write_text('{"lock_type": "detection"}')
    return mkv


def _stub_decode(monkeypatch) -> list[str]:
    decoded: list[str] = []

    def _decode(mkv, *a, **k):
        decoded.append(Path(mkv).name)
        frame = np.full((16, 16, 3), 20, dtype=np.uint8)
        return frame, frame, None, 10

    monkeypatch.setattr(stacker, "_compute_maxpixel", _decode)
    monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)
    return decoded


# ── stacker scope ────────────────────────────────────────────────────────────


def test_scope_defaults_to_all(tmp_path):
    assert stacker.stack_scope(_cfg(tmp_path)) == "all"
    assert stacker.stack_scope({"services": {"stacker": {"scope": "bogus"}}}) == "all"
    assert stacker.stack_scope(_cfg(tmp_path, scope="detections")) == "detections"


def test_detections_scope_stacks_only_locked_chunks(tmp_path, monkeypatch, caplog):
    cfg = _cfg(tmp_path, scope="detections")
    locked = _chunk(cfg, "215030", locked=True)
    _chunk(cfg, "215050")
    _chunk(cfg, "215110")
    decoded = _stub_decode(monkeypatch)
    with caplog.at_level(logging.INFO):
        stacker.process_night(CAM, NIGHT, cfg)
    assert decoded == [locked.name]
    assert "Stack scope is detections: 1 locked chunk(s)" in caplog.text


def test_all_scope_still_stacks_every_chunk(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    for t in ("215030", "215050"):
        _chunk(cfg, t)
    decoded = _stub_decode(monkeypatch)
    stacker.process_night(CAM, NIGHT, cfg)
    assert len(decoded) == 2


# ── dawn phases ──────────────────────────────────────────────────────────────


def _phase_names(phases) -> list[str]:
    return [name for name, _fn in phases]


def test_no_details_pass_when_meteor_clips_are_on_demand(tmp_path):
    names = _phase_names(dawn_process._detection_phases(
        {CAM: {}}, {CAM: {}}, NIGHT, _cfg(tmp_path, meteor_clips=False), manual=True))
    assert "1b overlay-details" not in names
    names = _phase_names(dawn_process._detection_phases(
        {CAM: {}}, {CAM: {}}, NIGHT, _cfg(tmp_path), manual=True))
    assert "1b overlay-details" in names


def test_no_timelapse_when_only_detections_are_stacked(tmp_path):
    deadline = dawn_process.BulkDeadline(enabled=False)
    names = _phase_names(dawn_process._bulk_phases(
        {CAM: {}}, NIGHT, _cfg(tmp_path, scope="detections"), deadline, manual=True))
    assert "4 stack" in names and "6 timelapse" not in names
    names = _phase_names(dawn_process._bulk_phases(
        {CAM: {}}, NIGHT, _cfg(tmp_path), deadline, manual=True))
    assert "6 timelapse" in names


# ── overlay_details.run_clip ─────────────────────────────────────────────────


def test_run_clip_without_a_matching_detection_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(overlay_details, "_load_night_detections", lambda *a: [])
    rendered: list[Path] = []
    monkeypatch.setattr(overlay_details, "render_details_clip",
                        lambda clip, dets, cfg: rendered.append(clip))
    clip = tmp_path / f"{CAM}_20260928_005437_color.mkv"
    assert overlay_details.run_clip(CAM, NIGHT, clip, {}, fetch_trajectories=False) is None
    assert rendered == []


def test_run_clip_renders_a_matched_detection(tmp_path, monkeypatch):
    det = {"time_utc": "2026-09-28T00:54:49"}
    monkeypatch.setattr(overlay_details, "_load_night_detections", lambda *a: [det])
    monkeypatch.setattr(overlay_details, "match_detections_to_clip", lambda *a: [det])
    monkeypatch.setattr(overlay_details, "render_details_clip",
                        lambda clip, dets, cfg: clip.with_suffix(".details.mkv"))
    clip = tmp_path / f"{CAM}_20260928_005437_color.mkv"
    assert overlay_details.run_clip(CAM, NIGHT, clip, {}, fetch_trajectories=False) is True


# ── process_clip delivery: stack, encode + upload, details ──────────────────


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


@pytest.fixture
def deliver(monkeypatch, tmp_path):
    """Run one process_clip delivery inline; returns (post, calls)."""
    calls: list[str] = []
    fake_stacker = types.SimpleNamespace(
        process_chunk=lambda mkv, cfg: calls.append(f"stack {mkv.name}"))
    fake_details = types.SimpleNamespace(
        run_clip=lambda cam, date, mkv, cfg: calls.append(f"details {mkv.name}"))
    monkeypatch.setitem(sys.modules, "stacker", fake_stacker)
    monkeypatch.setitem(sys.modules, "overlay_details", fake_details)
    monkeypatch.setattr(station_api.threading, "Thread", _InlineThread)
    monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", False)
    monkeypatch.setattr(station_api, "capture_base", lambda: tmp_path)
    (tmp_path / CAM / NIGHT).mkdir(parents=True)
    name = f"{CAM}_20260928_005437_color.mkv"
    (tmp_path / CAM / NIGHT / name).write_bytes(b"mkv")
    old = time.time() - 120        # closed, not the segment being written
    os.utime(tmp_path / CAM / NIGHT / name, (old, old))
    station_api.app.testing = True
    client = station_api.app.test_client()

    def post(cfg: dict, uploaded: bool) -> int:
        monkeypatch.setattr(station_api, "load_config", lambda: cfg, raising=False)
        monkeypatch.setattr(station_api.archive_upload, "upload_chunk",
                            lambda *a, **k: calls.append("upload") or uploaded)
        return client.post(f"/api/process_clip/{CAM}/{NIGHT}/{name}").status_code

    return post, calls, name


def test_on_demand_delivery_stacks_then_uploads_then_renders_details(deliver, tmp_path):
    post, calls, name = deliver
    assert post(_cfg(tmp_path, meteor_clips=False), uploaded=True) == 200
    assert calls == [f"stack {name}", "upload", f"details {name}"]


def test_no_details_when_the_upload_did_not_happen(deliver, tmp_path):
    post, calls, name = deliver
    post(_cfg(tmp_path, meteor_clips=False), uploaded=False)
    assert calls == [f"stack {name}", "upload"]


def test_no_details_where_dawn_renders_them(deliver, tmp_path):
    post, calls, name = deliver
    post(_cfg(tmp_path), uploaded=True)
    assert calls == [f"stack {name}", "upload"]
