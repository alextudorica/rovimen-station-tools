"""On-demand encodes leave the segment colour capture is writing alone, and
run one at a time.

The encoder's only guard against a segment still being written is the
``ready`` flag, and the station API set it itself: a lock or a process_clip
marked the chunk ready before encoding it, and the overlay gate of the serve
routes marked it ready and encoded it at once. A request for tonight's newest
chunk re-encoded the partial file in place while capture kept writing to the
replaced file, so the rest of that segment was lost. The overlay gate also ran
outside process_clip's serial lock, so a burst of plays started one ffmpeg per
chunk at once.

A chunk modified within ``segment_duration`` + 10 s is now locked without
being marked ready, and encoded and uploaded once it is closed (the fireball
alert asks for the event's chunk minutes after it, and play-to-keep locks what
is being watched); the synchronous serve routes wait for it the same way.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import flags_manager
import station_api

CAM = "RO000H"
DATE = "20260101"
FILENAME = "RO000H_20260101_000000_color.mkv"
CFG = {"segment_duration": 20}


class _InlineThread:
    def __init__(self, target=None, args=(), kwargs=None, daemon=None):
        self._target, self._args, self._kwargs = target, args, kwargs or {}

    def start(self):
        self._target(*self._args, **self._kwargs)


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _chunk_state(cfg: dict) -> dict:
    return flags_manager.load(CAM, DATE, cfg)["chunks"].get(FILENAME, {})


@pytest.fixture
def station(monkeypatch, tmp_path):
    """A station with one chunk written a second ago, real state.json flags,
    inline threads and a recording upload."""
    station_api.app.testing = True
    mkv = tmp_path / CAM / DATE / FILENAME
    mkv.parent.mkdir(parents=True)
    mkv.write_bytes(b"mkv")
    _age(mkv, 1)
    cfg = dict(CFG, videocapture_path=str(tmp_path))
    events: list[str] = []

    def upload(cam, date, path, cfg, force=False):
        events.append(f"upload serial={station_api._process_clip_serial.locked()} "
                      f"ready={_chunk_state(cfg).get('ready')} force={force}")
        return True

    monkeypatch.setattr(station_api, "capture_base", lambda: tmp_path)
    monkeypatch.setattr(station_api, "load_config", lambda: cfg, raising=False)
    monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", True)
    monkeypatch.setattr(station_api, "_flags_manager", flags_manager, raising=False)
    monkeypatch.setattr(flags_manager, "_state_path",
                        lambda s, d, c: tmp_path / s / d / "state.json", raising=False)
    monkeypatch.setattr(station_api.archive_upload, "upload_chunk", upload)
    monkeypatch.setattr(station_api.threading, "Thread", _InlineThread)
    monkeypatch.setattr(station_api.rovimen_config, "service_enabled", lambda c, s: False)
    return station_api.app.test_client(), cfg, mkv, events


def _closes_when_waited(monkeypatch, mkv: Path, cfg: dict, events: list[str],
                        closes: bool = True) -> None:
    """_wait_segment_closed stand-in: records the chunk's flags while the
    request waits, then lets the segment close (or not)."""
    def wait(path, c):
        st = _chunk_state(cfg)
        events.append(f"wait lock={bool(st.get('lock'))} ready={st.get('ready')}")
        if closes:
            _age(path, 120)
        return closes

    monkeypatch.setattr(station_api, "_wait_segment_closed", wait)


# ---------------------------------------------------------------------------
# _wait_segment_closed
# ---------------------------------------------------------------------------

class _Clock:
    def __init__(self):
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t


def test_a_closed_segment_is_not_waited_for(tmp_path):
    mkv = tmp_path / FILENAME
    mkv.write_bytes(b"x")
    _age(mkv, 31)
    clock = _Clock()
    assert station_api._wait_segment_closed(mkv, CFG, sleep=clock.sleeps.append, clock=clock)
    assert clock.sleeps == []


def test_waits_until_the_segment_closes(tmp_path):
    mkv = tmp_path / FILENAME
    mkv.write_bytes(b"x")
    _age(mkv, 5)
    clock = _Clock()

    def sleep(s):
        clock.sleeps.append(s)
        clock.t += s
        _age(mkv, 5 + clock.t)    # no write since

    assert station_api._wait_segment_closed(mkv, CFG, sleep=sleep, clock=clock)
    assert len(clock.sleeps) <= 2 and 24 <= clock.t <= 26


def test_gives_up_on_a_file_that_keeps_growing(tmp_path):
    mkv = tmp_path / FILENAME
    mkv.write_bytes(b"x")
    clock = _Clock()

    def sleep(s):
        clock.t += s
        mkv.write_bytes(b"x" * int(clock.t + 2))   # still being written

    assert not station_api._wait_segment_closed(mkv, CFG, sleep=sleep, clock=clock)
    assert clock.t <= 2 * 30 + 0.5


def test_a_vanished_chunk_is_not_waited_for(tmp_path):
    assert not station_api._wait_segment_closed(tmp_path / FILENAME, CFG,
                                                sleep=pytest.fail, clock=_Clock())


# ---------------------------------------------------------------------------
# POST /api/lock (play-to-keep)
# ---------------------------------------------------------------------------

def test_lock_on_an_open_segment_locks_now_and_uploads_once_closed(station, monkeypatch):
    client, cfg, mkv, events = station
    _closes_when_waited(monkeypatch, mkv, cfg, events)
    r = client.post(f"/api/lock/{CAM}/{DATE}/{FILENAME}", json={"locked": True})
    assert r.get_json() == {"locked": True, "lock_type": "manual"}
    assert events == ["wait lock=True ready=False",
                      "upload serial=True ready=True force=False"]


def test_lock_on_a_segment_that_never_closes_leaves_it_to_dawn(station, monkeypatch):
    client, cfg, mkv, events = station
    _closes_when_waited(monkeypatch, mkv, cfg, events, closes=False)
    r = client.post(f"/api/lock/{CAM}/{DATE}/{FILENAME}", json={"locked": True})
    assert r.status_code == 200
    assert events == ["wait lock=True ready=False"]
    st = _chunk_state(cfg)
    assert st["lock"]["lock_type"] == "manual" and st["ready"] is False


def test_lock_on_a_closed_chunk_does_not_wait(station, monkeypatch):
    client, cfg, mkv, events = station
    _age(mkv, 120)
    monkeypatch.setattr(station_api, "_wait_segment_closed", pytest.fail)
    client.post(f"/api/lock/{CAM}/{DATE}/{FILENAME}", json={"locked": True})
    assert events == ["upload serial=True ready=True force=False"]


# ---------------------------------------------------------------------------
# POST /api/process_clip (fireball alert, clip requester, Process click)
# ---------------------------------------------------------------------------

def test_process_clip_on_an_open_segment_answers_200_and_delivers_once_closed(
        station, monkeypatch):
    client, cfg, mkv, events = station
    _closes_when_waited(monkeypatch, mkv, cfg, events)
    r = client.post(f"/api/process_clip/{CAM}/{DATE}/{FILENAME}")
    assert r.status_code == 200     # fireball_alert counts 200/202/409 as requested
    assert events == ["wait lock=True ready=False",
                      "upload serial=True ready=True force=False"]
    assert not station_api._process_clip_active


def test_process_clip_waits_on_an_open_segment_another_path_locked(station, monkeypatch):
    """detection_lock may lock (and mark ready) the chunk being written."""
    client, cfg, mkv, events = station
    flags_manager.lock_chunk(CAM, DATE, FILENAME, {"lock_type": "detection"}, cfg)
    _closes_when_waited(monkeypatch, mkv, cfg, events)
    client.post(f"/api/process_clip/{CAM}/{DATE}/{FILENAME}")
    assert events[0].startswith("wait ")
    assert events[1].startswith("upload serial=True")


def test_process_clip_force_reaches_the_upload(station):
    client, cfg, mkv, events = station
    _age(mkv, 120)
    client.post(f"/api/process_clip/{CAM}/{DATE}/{FILENAME}?force=1")
    assert events == ["upload serial=True ready=True force=True"]


# ---------------------------------------------------------------------------
# Overlay gate of the serve routes (_ensure_overlaid)
# ---------------------------------------------------------------------------

@pytest.fixture
def encoder(monkeypatch):
    calls: list[str] = []

    class _Enc:
        @staticmethod
        def process_chunk(path, cfg):
            calls.append(f"encode serial={station_api._process_clip_serial.locked()}")
            return True

    monkeypatch.setitem(__import__("sys").modules, "encoder", _Enc)
    return calls


def test_overlay_gate_encodes_one_at_a_time(station, encoder):
    _client, cfg, mkv, _events = station
    _age(mkv, 120)
    assert station_api._ensure_overlaid(CAM, DATE, mkv, cfg) is True
    assert encoder == ["encode serial=True"]


def test_overlay_gate_waits_for_an_open_segment(station, encoder, monkeypatch):
    _client, cfg, mkv, events = station
    _closes_when_waited(monkeypatch, mkv, cfg, events)
    assert station_api._ensure_overlaid(CAM, DATE, mkv, cfg) is True
    assert events == ["wait lock=False ready=None"]
    assert encoder == ["encode serial=True"]


def test_overlay_gate_never_marks_or_encodes_a_segment_still_written(
        station, encoder, monkeypatch):
    client, cfg, mkv, events = station
    _closes_when_waited(monkeypatch, mkv, cfg, events, closes=False)
    assert station_api._ensure_overlaid(CAM, DATE, mkv, cfg) is False
    assert encoder == []
    assert _chunk_state(cfg).get("ready") is None
    assert client.get(f"/color_capture/{CAM}/{DATE}/{FILENAME}").status_code == 503


# ---------------------------------------------------------------------------
# POST /api/encode_chunk (VDB Process button)
# ---------------------------------------------------------------------------

def test_encode_chunk_refuses_a_segment_still_written(station, encoder):
    client, _cfg, _mkv, _events = station
    r = client.post("/api/encode_chunk",
                    json={"camera": CAM, "date": DATE, "filename": FILENAME})
    assert r.status_code == 409
    assert "still writing" in r.get_json()["error"]
    assert encoder == []


def test_encode_chunk_encodes_a_closed_chunk(station, encoder):
    client, cfg, mkv, _events = station
    _age(mkv, 120)
    flags_manager.mark_ready(CAM, DATE, FILENAME, cfg)
    r = client.post("/api/encode_chunk",
                    json={"camera": CAM, "date": DATE, "filename": FILENAME})
    assert r.status_code == 200
    assert encoder == ["encode serial=False"]
