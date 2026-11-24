"""services.reencode.meteor_clips=false: dawn leaves meteor clips for on-demand encoding.

CPU-starved stations keep every core for RMS and the dawn stacks; their clips
are encoded only when the dashboard's Process button calls /api/process_clip.
"""

from __future__ import annotations

import flags_manager
import encoder

STATION, NIGHT = "RO0003", "20260923"


def _cfg(tmp_path, meteor_clips):
    reencode = {"enabled": True, "full_night": False}
    if meteor_clips is not None:
        reencode["meteor_clips"] = meteor_clips
    return {
        "videocapture_path": str(tmp_path / "cap"),
        "stations": {STATION: {"rotate": False}},
        "services": {"reencode": reencode},
        "overlay": {"enabled": False},
        "capabilities": {"hwencode": False},
    }


def _locked_chunk(tmp_path, cfg):
    night = tmp_path / "cap" / STATION / NIGHT
    night.mkdir(parents=True)
    mkv = night / f"{STATION}_20260924_021500_color.mkv"
    mkv.write_bytes(b"x")
    flags_manager.mark_ready(STATION, NIGHT, mkv.name, cfg)
    (night / (mkv.name + ".locked")).write_text("{}")
    return mkv


def test_on_demand_station_skips_the_dawn_meteor_clip_encode(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, False)
    _locked_chunk(tmp_path, cfg)
    calls = []
    monkeypatch.setattr(encoder, "process_chunk", lambda mkv, c: calls.append(mkv) or True)
    encoder.process_night_locked_only(STATION, NIGHT, cfg)
    assert calls == []


def test_default_still_encodes_meteor_clips_at_dawn(tmp_path, monkeypatch):
    for value in (None, True):
        sub = tmp_path / str(value)
        sub.mkdir()
        cfg = _cfg(sub, value)
        mkv = _locked_chunk(sub, cfg)
        calls = []
        monkeypatch.setattr(encoder, "process_chunk", lambda m, c: calls.append(m) or True)
        encoder.process_night_locked_only(STATION, NIGHT, cfg)
        assert calls == [mkv], value


def test_on_demand_path_is_not_gated(tmp_path, monkeypatch):
    """process_chunk itself (what /api/process_clip and /api/encode_chunk run)
    never reads meteor_clips, so a requested clip still encodes."""
    import inspect
    src = inspect.getsource(encoder.process_chunk)
    assert "meteor_clips" not in src
