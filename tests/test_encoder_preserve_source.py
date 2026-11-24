"""A spectroscope's chunks are measurements and must not be re-rendered.

`process_chunk` re-encodes in place: colour calibration, gamma, rotation and an
OSD bar padded onto the frame, then an atomic rename over the original. For a
picture camera that is the product. For GH662S it is destruction -- locking the
2026-09-21 21:12:36 fireball turned a 1.9 MB 1280x720 capture into a 593 KB
1280x848 render, and because the archive copy is made from the same file the
raw frames were gone everywhere. A first-order spectrum is faint and broad and
is exactly what a 3x re-compression removes.

The guard has one trap worth pinning: `reencoded` is the sole gate
`archive_upload` reads for upload readiness, so a chunk that is deliberately
never encoded still has to be marked, or the camera silently stops uploading.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "rovimen-scripts"
sys.path.insert(0, str(SCRIPTS))

import encoder  # noqa: E402


class TestPreservesSource:
    def test_a_spectro_camera_is_protected_without_a_config_edit(self):
        """Defaults on for any camera carrying a spectro pointing block, so
        this reaches the fleet without touching per-station config."""
        assert encoder._preserves_source({"spectro": {"scale_x": 25.26}}) is True

    def test_an_ordinary_camera_is_unaffected(self):
        assert encoder._preserves_source({}) is False
        assert encoder._preserves_source({"rotate": True}) is False

    def test_the_explicit_flag_wins_in_both_directions(self):
        """An operator can protect a non-spectro camera, and can opt a spectro
        camera back in if they ever want the rendered product."""
        assert encoder._preserves_source({"preserve_source": True}) is True
        assert encoder._preserves_source(
            {"spectro": {"scale_x": 1.0}, "preserve_source": False}) is False


class TestProcessChunkGuard:
    @pytest.fixture()
    def chunk(self, tmp_path):
        f = tmp_path / "GH662S_20260921_211236_color.mkv"
        f.write_bytes(b"original pixels")
        return f

    def _cfg(self, tmp_path, station_cfg):
        return {
            "stations": {"GH662S": station_cfg},
            "videocapture_path": str(tmp_path),
        }

    def test_the_file_is_left_byte_for_byte_alone(self, chunk, tmp_path, monkeypatch):
        marked = {}
        monkeypatch.setattr(encoder.flags_manager, "load",
                            lambda *a, **k: {"chunks": {chunk.name: {"ready": True}}})
        monkeypatch.setattr(encoder, "_mark_encoded",
                            lambda s, d, n, c, **kw: marked.setdefault("n", n))
        before = chunk.read_bytes()
        ok = encoder.process_chunk(
            chunk, self._cfg(tmp_path, {"spectro": {"scale_x": 25.26}}))
        assert ok is True
        assert chunk.read_bytes() == before

    def test_it_is_still_marked_reencoded_so_uploads_do_not_stall(
            self, chunk, tmp_path, monkeypatch):
        """The trap: `reencoded` gates upload readiness."""
        marked = {}
        monkeypatch.setattr(encoder.flags_manager, "load",
                            lambda *a, **k: {"chunks": {chunk.name: {"ready": True}}})
        monkeypatch.setattr(encoder, "_mark_encoded",
                            lambda s, d, n, c, **kw: marked.update(name=n, **kw))
        encoder.process_chunk(
            chunk, self._cfg(tmp_path, {"spectro": {"scale_x": 25.26}}))
        assert marked.get("name") == chunk.name
        # The capture is the final file: its stack must still get colour.
        assert marked.get("kept_source") is True

    def test_an_unready_chunk_is_still_skipped_first(
            self, chunk, tmp_path, monkeypatch):
        """The readiness check must keep precedence: a chunk still being
        written is not something to mark done."""
        monkeypatch.setattr(encoder.flags_manager, "load",
                            lambda *a, **k: {"chunks": {chunk.name: {"ready": False}}})
        called = []
        monkeypatch.setattr(encoder, "_mark_encoded",
                            lambda *a, **kw: called.append(a))
        assert encoder.process_chunk(
            chunk, self._cfg(tmp_path, {"spectro": {"scale_x": 1.0}})) is False
        assert called == []
