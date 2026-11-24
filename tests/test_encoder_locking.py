"""Two encoders never work on one chunk at once, and a crash never causes a second encode.

Four processes call encoder.process_chunk (dawn, the upload daemon, and the
station API's Process button and clip serving). They used to share one fixed
scratch name, so two of them on the same chunk truncated each other's output
and one could install a clip still being written, marked done forever. A
finished re-encode also carries a tag, so a crash between the rename and the
state.json mark heals the flag instead of encoding the clip twice.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import pytest

import color_calibration as cc
import encoder
import flags_manager

CAM = "RO000H"
NIGHT = "20260322"


class _SlowPopen:
    """ffmpeg that takes a while and records each launch."""
    launches: list[list[str]] = []
    delay = 0.4
    lock = threading.Lock()

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        with _SlowPopen.lock:
            _SlowPopen.launches.append(list(cmd))

    def communicate(self, timeout=None):
        time.sleep(_SlowPopen.delay)
        Path(self.cmd[-1]).write_bytes(b"encoded " + encoder.ENCODED_TAG.encode() + b"=QP20")
        return b"", b""

    def kill(self):
        pass


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "stations": {CAM: {"rotate": False}},
        "services": {"reencode": {"enabled": True}},
        "overlay": {"enabled": False},
    }


@pytest.fixture
def chunk(cfg, monkeypatch):
    night = Path(cfg["videocapture_path"]) / CAM / NIGHT
    night.mkdir(parents=True)
    mkv = night / f"{CAM}_{NIGHT}_210000_color.mkv"
    mkv.write_bytes(b"s" * 1000)
    flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
    monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
    monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)
    monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
    monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
    monkeypatch.setattr(encoder.subprocess, "Popen", _SlowPopen)
    encoder._quant_floor.clear()
    _SlowPopen.launches = []
    _SlowPopen.delay = 0.4
    return mkv


def _state(cfg, mkv) -> dict:
    return flags_manager.load(CAM, NIGHT, cfg)["chunks"][mkv.name]


def test_two_callers_on_one_chunk_encode_it_once(chunk, cfg):
    results: list[bool] = []

    def run():
        results.append(encoder.process_chunk(chunk, cfg))

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
        time.sleep(0.05)
    for t in threads:
        t.join(10)
    assert results == [True, True]
    assert len(_SlowPopen.launches) == 1     # the waiter found the work done
    assert encoder._has_encoded_marker(chunk)
    assert _state(cfg, chunk)["reencoded"] is True
    assert not list(chunk.parent.glob("*.tmp.mkv"))


def test_a_waiter_gives_up_after_its_wait_and_leaves_the_chunk(chunk, cfg):
    cfg["encode_lock_wait_sec"] = 0.3
    with encoder._chunk_encode_lock(chunk) as mine:
        assert mine is True
        t0 = time.monotonic()
        assert encoder.process_chunk(chunk, cfg) is False
        assert time.monotonic() - t0 < 3
    assert _SlowPopen.launches == []
    assert _state(cfg, chunk)["reencoded"] is False


def test_a_crash_between_rename_and_mark_heals_the_flag_without_a_second_encode(chunk, cfg):
    # The encoded clip is in place (it carries the tag) but state.json never
    # heard about it: the process died, or a full disk swallowed the save.
    chunk.write_bytes(b"x" * 600 + encoder.ENCODED_TAG.encode() + b"=QP20" + b"y" * 400)
    assert encoder.process_chunk(chunk, cfg) is True
    assert _SlowPopen.launches == []
    assert _state(cfg, chunk)["reencoded"] is True


def test_marker_found_near_the_end_too(tmp_path):
    f = tmp_path / "c.mkv"
    f.write_bytes(b"\0" * (encoder._MARKER_SCAN_BYTES * 3) + encoder.ENCODED_TAG.encode() + b"\0" * 10)
    assert encoder._has_encoded_marker(f)
    g = tmp_path / "raw.mkv"
    g.write_bytes(b"\0" * (encoder._MARKER_SCAN_BYTES * 3))
    assert not encoder._has_encoded_marker(g)
    assert not encoder._has_encoded_marker(tmp_path / "missing.mkv")


def test_the_final_command_writes_the_marker(chunk, cfg):
    _SlowPopen.delay = 0.0
    encoder.process_chunk(chunk, cfg)
    cmd = _SlowPopen.launches[0]
    assert cmd[-3:-1] == ["-metadata", f"{encoder.ENCODED_TAG}=CRF21"]


def test_the_lock_follows_a_chunk_replaced_while_waiting(tmp_path):
    mkv = tmp_path / "c_color.mkv"
    mkv.write_bytes(b"raw")
    got: dict = {}
    holder = encoder._acquire_encode_lock(mkv, 0.0)
    assert holder[0] == "held"

    def wait():
        got["status"], got["fd"] = encoder._acquire_encode_lock(mkv, 5.0)

    t = threading.Thread(target=wait)
    t.start()
    time.sleep(0.3)
    new = tmp_path / "new.tmp.mkv"
    new.write_bytes(b"encoded")
    os.rename(new, mkv)                     # the holder installs its encode
    os.close(holder[1])                     # ...and releases
    t.join(10)
    assert got["status"] == "held"
    assert os.fstat(got["fd"]).st_ino == os.stat(mkv).st_ino
    os.close(got["fd"])


def test_a_vanished_chunk_is_reported_gone(tmp_path):
    assert encoder._acquire_encode_lock(tmp_path / "nope_color.mkv", 0.0) == ("gone", None)


def test_scratch_names_are_private_per_thread_and_never_look_like_chunks(tmp_path):
    mkv = tmp_path / f"{CAM}_{NIGHT}_210000_color.mkv"
    names: list[Path] = [encoder._scratch_path(mkv)]
    t = threading.Thread(target=lambda: names.append(encoder._scratch_path(mkv)))
    t.start()
    t.join()
    assert names[0] != names[1]
    for n in names:
        assert n.parent == mkv.parent and n.name.endswith(".tmp.mkv")
        assert not n.name.endswith("_color.mkv")


def test_sweep_removes_only_old_scratch_files(tmp_path):
    night = tmp_path / "night"
    night.mkdir()
    chunk = night / f"{CAM}_{NIGHT}_210000_color.mkv"
    chunk.write_bytes(b"c")
    old_legacy = night / f"{chunk.name}.tmp.mkv"
    old_private = night / f"{chunk.name}.123-456.tmp.mkv"
    fresh = night / f"{chunk.name}.789-1.tmp.mkv"
    for p in (old_legacy, old_private, fresh):
        p.write_bytes(b"t")
    past = time.time() - encoder._STALE_TMP_AGE_S - 60
    for p in (old_legacy, old_private, chunk):
        os.utime(p, (past, past))
    assert encoder._sweep_stale_scratch(night) == 2
    assert chunk.exists() and fresh.exists()
    assert not old_legacy.exists() and not old_private.exists()


def test_night_passes_sweep_stale_scratch(chunk, cfg):
    stale = chunk.with_name(chunk.name + ".1-2.tmp.mkv")
    stale.write_bytes(b"t")
    past = time.time() - encoder._STALE_TMP_AGE_S - 60
    os.utime(stale, (past, past))
    _SlowPopen.delay = 0.0
    encoder.process_night(CAM, NIGHT, cfg)
    assert not stale.exists()
