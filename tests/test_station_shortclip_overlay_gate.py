"""Overlay gate for the station download/serve routes (station_api.py).

The station burns the branding+data overlay into a ``_color.mkv`` in-place
*after* color_capture writes it raw, so there is a window where the chunk on
disk has no overlay. Every download/serve path (``/api/shortclip``,
``/api/shortclip_stitch``, ``/color_capture/...``) must funnel through
``_ensure_overlaid`` first: an un-overlaid chunk is re-encoded on demand
(``encoder.process_chunk``) rather than cut/served raw. If the overlay cannot
be produced the route refuses (503) instead of falling back to raw footage.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

import station_api


CAM = "RO0017"
DATE = "20260709"
CHUNK = f"{CAM}_{DATE}_214233_color.mkv"


def _mkv(tmp_path: Path) -> Path:
    """A closed chunk: written two minutes ago, so not the segment colour
    capture is writing (test_station_segment_in_progress covers that)."""
    p = tmp_path / CHUNK
    p.write_bytes(b"raw-matroska-bytes")
    old = time.time() - 120
    os.utime(p, (old, old))
    return p


class TestChunkIsReencoded:
    def test_state_flag_wins(self, monkeypatch):
        monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", True)
        monkeypatch.setattr(
            station_api._flags_manager, "load",
            lambda cam, date, cfg: {"chunks": {CHUNK: {"reencoded": True}}},
        )
        assert station_api._chunk_is_reencoded(CAM, DATE, Path(CHUNK), {}) is True

    def test_sidecar_fallback(self, tmp_path, monkeypatch):
        monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", False)
        mkv = _mkv(tmp_path)
        assert station_api._chunk_is_reencoded(CAM, DATE, mkv, {}) is False
        Path(str(mkv) + ".reencoded").write_text("")
        assert station_api._chunk_is_reencoded(CAM, DATE, mkv, {}) is True


class TestEnsureOverlaid:
    def test_already_overlaid_skips_encode(self, tmp_path, monkeypatch):
        """A reencoded chunk needs no work — process_chunk is never called."""
        mkv = _mkv(tmp_path)
        monkeypatch.setattr(
            station_api, "_chunk_is_reencoded",
            lambda cam, date, m, cfg: True,
        )
        called = {"n": 0}

        class _Enc:
            @staticmethod
            def process_chunk(*a, **k):
                called["n"] += 1
                return True

        monkeypatch.setitem(__import__("sys").modules, "encoder", _Enc)
        assert station_api._ensure_overlaid(CAM, DATE, mkv, {}) is True
        assert called["n"] == 0

    def test_overlay_disabled_is_not_a_bypass(self, tmp_path, monkeypatch):
        """When the station has the overlay switched off, the chunk as-is IS
        the processed variant — serving it is not a raw-footage bypass."""
        mkv = _mkv(tmp_path)
        monkeypatch.setattr(
            station_api, "_chunk_is_reencoded",
            lambda cam, date, m, cfg: False,
        )
        cfg = {"overlay": {"enabled": False}}
        assert station_api._ensure_overlaid(CAM, DATE, mkv, cfg) is True

    def test_renders_on_demand_when_not_overlaid(self, tmp_path, monkeypatch):
        """An un-overlaid chunk triggers a synchronous encode pass."""
        mkv = _mkv(tmp_path)
        monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", False)
        state = {"reencoded": False}

        class _Enc:
            @staticmethod
            def process_chunk(m, cfg):
                state["reencoded"] = True
                Path(str(m) + ".reencoded").write_text("")
                return True

        monkeypatch.setitem(__import__("sys").modules, "encoder", _Enc)
        assert station_api._ensure_overlaid(CAM, DATE, mkv, {}) is True
        assert state["reencoded"] is True

    def test_refuses_when_encode_fails(self, tmp_path, monkeypatch):
        """process_chunk returning False (and no overlay produced) means the
        route must NOT serve raw — _ensure_overlaid returns False so the caller
        aborts."""
        mkv = _mkv(tmp_path)
        monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", False)

        class _Enc:
            @staticmethod
            def process_chunk(m, cfg):
                return False

        monkeypatch.setitem(__import__("sys").modules, "encoder", _Enc)
        assert station_api._ensure_overlaid(CAM, DATE, mkv, {}) is False

    def test_refuses_when_encode_raises(self, tmp_path, monkeypatch):
        mkv = _mkv(tmp_path)
        monkeypatch.setattr(station_api, "_STATE_MANAGER_OK", False)

        class _Enc:
            @staticmethod
            def process_chunk(m, cfg):
                raise RuntimeError("ffmpeg blew up")

        monkeypatch.setitem(__import__("sys").modules, "encoder", _Enc)
        assert station_api._ensure_overlaid(CAM, DATE, mkv, {}) is False


# ── Route-level: /api/shortclip is gated on the overlay ─────────────────────


@pytest.fixture
def shortclip_env(monkeypatch, tmp_path):
    """Test client with a chunk on disk and ffmpeg stubbed out."""
    station_api.app.testing = True
    cap_dir = tmp_path / CAM / DATE
    cap_dir.mkdir(parents=True)
    (cap_dir / CHUNK).write_bytes(b"mkv-bytes")
    monkeypatch.setattr(station_api, "capture_base", lambda: tmp_path)
    monkeypatch.setattr(station_api, "load_config", lambda: {}, raising=False)

    # Stub the trim ffmpeg so a "rendered" clip is produced without a real
    # encode; a returncode-0 run writes a marker into the temp mp4.
    class _Ok:
        returncode = 0

    def _fake_run(cmd, **kw):
        # cmd[-1] is the output temp path (send_file reads it back).
        Path(cmd[-1]).write_bytes(b"trimmed-overlaid-clip")
        return _Ok()

    monkeypatch.setattr(station_api.subprocess, "run", _fake_run)
    return station_api.app.test_client()


def test_shortclip_renders_on_demand_then_serves(shortclip_env, monkeypatch):
    """An un-overlaid chunk is re-encoded on demand, then the trimmed clip is
    served — never the raw chunk."""
    calls = {"n": 0}

    def _gate(cam, date, mkv, cfg):
        calls["n"] += 1
        return True  # pretend the on-demand render succeeded

    monkeypatch.setattr(station_api, "_ensure_overlaid", _gate)
    r = shortclip_env.get(f"/api/shortclip/{CAM}/{DATE}/{CHUNK}?ss=1&t=2")
    assert r.status_code == 200
    assert calls["n"] == 1
    assert r.data == b"trimmed-overlaid-clip"


def test_shortclip_503_when_overlay_cannot_be_produced(shortclip_env, monkeypatch):
    """If the overlay can't be rendered the route refuses (503) rather than
    cutting a clip from the raw chunk."""
    monkeypatch.setattr(
        station_api, "_ensure_overlaid",
        lambda cam, date, mkv, cfg: False,
    )
    r = shortclip_env.get(f"/api/shortclip/{CAM}/{DATE}/{CHUNK}?ss=1&t=2")
    assert r.status_code == 503


def test_serve_capture_gates_color_mkv(shortclip_env, monkeypatch):
    """The full-clip serve route (dashboard /video + /download fallback) is
    gated too: a raw color chunk 503s instead of streaming un-overlaid."""
    monkeypatch.setattr(
        station_api, "_ensure_overlaid",
        lambda cam, date, mkv, cfg: False,
    )
    r = shortclip_env.get(f"/color_capture/{CAM}/{DATE}/{CHUNK}")
    assert r.status_code == 503
