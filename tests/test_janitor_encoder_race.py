"""A chunk the janitor deletes while an encoder works on it stays deleted.

The encoder holds an exclusive flock on the chunk file while it encodes, then
renames its finished clip over the chunk's path. The janitor took no lock, so
a chunk it deleted mid-encode came back as the encoded clip: a file no
retention pass had accounted for, on a disk the janitor had just counted as
relieved. The janitor now skips a chunk an encoder holds, and the encoder
installs its clip only if the chunk is still there.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import color_calibration as cc
import encoder
import flags_manager
import janitor_storage_watchdog as jsw

CAM = "RO000H"
NIGHT = "20260322"


@pytest.fixture(autouse=True)
def _reset_janitor_logger():
    yield
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
        h.close()


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "log_path": str(tmp_path / "logs"),
        "segment_duration": 20,
        "stations": {CAM: {"rotate": False}},
        "services": {"reencode": {"enabled": True}},
        "overlay": {"enabled": False},
    }


@pytest.fixture
def janitor(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    Path(cfg["videocapture_path"]).mkdir(parents=True, exist_ok=True)
    return jsw.Janitor(str(tmp_path / "config.json"))


@pytest.fixture
def chunk(cfg, monkeypatch):
    night = Path(cfg["videocapture_path"]) / CAM / NIGHT
    night.mkdir(parents=True, exist_ok=True)
    mkv = night / f"{CAM}_{NIGHT}_210000_color.mkv"
    mkv.write_bytes(b"s" * 1000)
    flags_manager.mark_ready_batch(CAM, NIGHT, [mkv.name], cfg)
    monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: True)
    monkeypatch.setattr(cc, "resolve_adaptive_from_mkv", lambda *a, **k: None)
    monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
    monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
    encoder._quant_floor.clear()
    return mkv


def _ffmpeg(during=lambda: None):
    """An ffmpeg that runs *during* while it "encodes", then writes a smaller clip."""

    class _Popen:
        def __init__(self, cmd, **kw):
            self.cmd = cmd
            self.returncode = 0

        def communicate(self, timeout=None):
            during()
            Path(self.cmd[-1]).write_bytes(b"encoded " + encoder.ENCODED_TAG.encode())
            return b"", b""

        def kill(self):
            pass

    return _Popen


def test_janitor_skips_a_chunk_an_encoder_holds(janitor, chunk):
    with encoder._chunk_encode_lock(chunk) as mine:
        assert mine is True
        assert janitor._unlink_chunk(CAM, chunk) is False
        assert chunk.exists()
    assert janitor._unlink_chunk(CAM, chunk) is True
    assert not chunk.exists()


def test_a_gone_chunk_is_not_counted_as_deleted(janitor, tmp_path):
    assert janitor._unlink_chunk(CAM, tmp_path / f"{CAM}_{NIGHT}_210000_color.mkv") is False


def test_janitor_during_an_encode_leaves_the_encoded_clip(janitor, chunk, monkeypatch):
    tried: list[bool] = []
    monkeypatch.setattr(encoder.subprocess, "Popen",
                        _ffmpeg(lambda: tried.append(janitor._unlink_chunk(CAM, chunk))))
    assert encoder.process_chunk(chunk, janitor.cfg) is True
    assert tried == [False]
    assert encoder._has_encoded_marker(chunk)


def test_an_encode_of_a_chunk_deleted_meanwhile_is_discarded(chunk, cfg, monkeypatch):
    """Something that does not take the encoder's lock deleted the chunk
    mid-encode: the finished clip must not bring it back."""
    monkeypatch.setattr(encoder.subprocess, "Popen", _ffmpeg(chunk.unlink))
    assert encoder.process_chunk(chunk, cfg) is False
    assert not chunk.exists()
    assert not list(chunk.parent.glob("*.tmp.mkv"))
    assert flags_manager.load(CAM, NIGHT, cfg)["chunks"][chunk.name]["reencoded"] is False


def test_eviction_counts_only_what_it_deleted(janitor, chunk, caplog):
    """A held chunk is passed over: not counted, not logged as data loss,
    and the eviction goes on to the next chunk."""
    other = chunk.with_name(f"{CAM}_{NIGHT}_210020_color.mkv")
    other.write_bytes(b"o")
    dt = datetime.now(timezone.utc) - timedelta(hours=30)
    janitor._over_limit = lambda: other.exists()
    with encoder._chunk_encode_lock(chunk), caplog.at_level("WARNING", logger="atium"):
        n = janitor._evict_chunks([(dt, True, CAM, NIGHT, chunk),
                                   (dt + timedelta(seconds=20), True, CAM, NIGHT, other)],
                                  phase="test")
    assert n == 1
    assert chunk.exists() and not other.exists()
    lost = [r.getMessage() for r in caplog.records if jsw.DATA_LOSS_MARKER in r.getMessage()]
    assert len(lost) == 1 and other.name in lost[0]
