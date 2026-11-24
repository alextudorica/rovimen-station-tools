"""Stacks are published by rename, never by writing into the live file.

A station stacks the same chunk from two directions -- color_capture fires
``stacker.py --chunk`` as each segment closes (and kills it at 120 s), while
dawn's night pass sweeps everything still unflagged. Both wrote the stack
straight into ``<chunk>_stack.webp``: ffmpeg annotated it in place, Pillow
re-saved it in place to stamp the EXIF marker, and a fixed ``.tmp.webp``
scratch name was shared by every writer of the same image. Any interruption in
that window published a 0-byte stack -- and the chunk was flagged ``stacked``
anyway, so no later pass rebuilt it.

These tests pin the three properties that make that impossible: every write
lands in a private scratch file that replaces the destination only when it
holds bytes, a failed render leaves the previous image untouched and says so,
and a second stacker on the same chunk stands down instead of racing.
"""

from __future__ import annotations

import fcntl
import logging
import os
import subprocess
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import flags_manager
import overlay
import stacker


REPO_ROOT = Path(__file__).resolve().parent.parent
FONT = REPO_ROOT / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf"
LOGO = REPO_ROOT / "rovimen-scripts" / "assets" / "astromania_text.png"

CAM = "RO000E"
CHUNK = f"{CAM}_20260818_215030_color.mkv"
NIGHT = "20260818"
STACK_NAME = f"{CAM}_20260818_215030_stack.webp"


def _stack_image(path: Path, size: tuple[int, int] = (320, 180)) -> Path:
    Image.new("RGB", size, (5, 8, 20)).save(path, "WEBP", quality=95)
    return path


def _overlay_cfg() -> dict:
    return {
        "enabled": True,
        "font": str(FONT),
        "logo": str(LOGO),
        "font_size": 20,
        "network": "ROVIMEN",
        "style": "cinema",
        "show_logo": True,
        "show_network": True,
        "show_station": True,
        "show_coords": True,
        "show_pointing": True,
        "show_timestamp": True,
        "coords": "45.0N 25.0E",
    }


def _scratch_files(directory: Path) -> list[Path]:
    return [p for p in directory.iterdir() if ".tmp" in p.name or p.name.startswith("tmp")]


# ---------------------------------------------------------------------------
# The helper every still write goes through.
# ---------------------------------------------------------------------------


class TestAtomicStillWrite:
    def test_success_replaces_the_destination(self, tmp_path):
        dst = _stack_image(tmp_path / STACK_NAME)
        before = dst.read_bytes()

        with overlay.atomic_still_write(dst) as tmp:
            assert tmp != dst, "the destination must never be handed out as the scratch file"
            assert tmp.parent == dst.parent, "the rename must stay on one filesystem"
            Image.new("RGB", (64, 64), (200, 30, 30)).save(tmp, "WEBP")

        assert dst.read_bytes() != before
        assert Image.open(dst).size == (64, 64)
        assert _scratch_files(tmp_path) == []

    def test_failure_leaves_the_original_intact(self, tmp_path):
        dst = _stack_image(tmp_path / STACK_NAME)
        before = dst.read_bytes()

        with pytest.raises(RuntimeError, match="render blew up"):
            with overlay.atomic_still_write(dst) as tmp:
                tmp.write_bytes(b"half an im")
                raise RuntimeError("render blew up")

        assert dst.read_bytes() == before, "a failed write must not touch the published image"
        assert _scratch_files(tmp_path) == []

    def test_an_empty_render_is_refused(self, tmp_path):
        dst = _stack_image(tmp_path / STACK_NAME)
        before = dst.read_bytes()

        # The exact regression: the writer produced nothing. Publishing that
        # would leave the 0-byte stack this whole change exists to prevent.
        with pytest.raises(RuntimeError, match="no bytes"):
            with overlay.atomic_still_write(dst):
                pass

        assert dst.read_bytes() == before
        assert dst.stat().st_size > 0
        assert _scratch_files(tmp_path) == []

    def test_a_missing_destination_is_created(self, tmp_path):
        dst = tmp_path / "nested" / STACK_NAME
        with overlay.atomic_still_write(dst) as tmp:
            Image.new("RGB", (32, 32)).save(tmp, "WEBP")
        assert dst.exists() and dst.stat().st_size > 0

    def test_concurrent_writers_get_private_scratch_files(self, tmp_path):
        dst = _stack_image(tmp_path / STACK_NAME)
        with overlay.atomic_still_write(dst) as first:
            with overlay.atomic_still_write(dst) as second:
                # The old fixed ``<stem>.tmp.webp`` was shared by both writers,
                # so one renamed the other's half-written file into place.
                assert first != second
                Image.new("RGB", (16, 16)).save(second, "WEBP")
            Image.new("RGB", (48, 48)).save(first, "WEBP")

        assert Image.open(dst).size == (48, 48)
        assert _scratch_files(tmp_path) == []

    def test_scratch_names_do_not_look_like_stacks(self, tmp_path):
        dst = tmp_path / STACK_NAME
        with overlay.atomic_still_write(dst) as tmp:
            # The uploader and the janitor walk ``*_stack.webp``; a scratch file
            # must never be mistaken for a finished stack.
            assert not tmp.name.endswith("_stack.webp")
            tmp.write_bytes(b"x")


# ---------------------------------------------------------------------------
# annotate_still: the in-place ffmpeg call that truncated the five Roit stacks.
# ---------------------------------------------------------------------------


class TestAnnotateStillIsAtomic:
    def test_ffmpeg_failure_leaves_the_source_intact(self, tmp_path, monkeypatch, caplog):
        src = _stack_image(tmp_path / STACK_NAME)
        before = src.read_bytes()

        def _failing_run(cmd, **kwargs):
            return subprocess.CompletedProcess(cmd, 1)

        monkeypatch.setattr(overlay.subprocess, "run", _failing_run)

        with caplog.at_level(logging.WARNING):
            ok = overlay.annotate_still(
                src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
            )

        assert ok is False
        assert src.read_bytes() == before, "src == dst: a failed annotate must not truncate it"
        assert src.stat().st_size > 0
        assert str(src) in caplog.text, "the failure must name the file an operator has to look at"
        assert _scratch_files(tmp_path) == []

    def test_a_broken_render_leaves_the_source_intact(self, tmp_path, monkeypatch, caplog):
        # ffmpeg reports success but produces an unreadable intermediate: the
        # Pillow conversion raises, and the published stack must survive it.
        src = _stack_image(tmp_path / STACK_NAME)
        before = src.read_bytes()

        def _lying_run(cmd, **kwargs):
            Path(cmd[-1]).write_bytes(b"not a png")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(overlay.subprocess, "run", _lying_run)

        with caplog.at_level(logging.WARNING):
            ok = overlay.annotate_still(
                src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
            )

        assert ok is False
        assert src.read_bytes() == before
        assert _scratch_files(tmp_path) == []

    def test_success_publishes_a_whole_marked_image(self, tmp_path):
        src = _stack_image(tmp_path / STACK_NAME, size=(640, 360))

        ok = overlay.annotate_still(
            src, src, _overlay_cfg(), CAM, {"az": 180.0, "alt": 45.0}, 1_800_000_000
        )

        assert ok is True
        assert src.stat().st_size > 0
        assert overlay.still_is_overlaid(src) is True
        assert _scratch_files(tmp_path) == [], "the PNG intermediate must not be left behind"

    def test_marking_never_destroys_the_image_it_stamps(self, tmp_path):
        src = _stack_image(tmp_path / STACK_NAME)
        assert overlay.mark_still_overlaid(src) is True
        assert src.stat().st_size > 0
        assert overlay.still_is_overlaid(src) is True
        assert _scratch_files(tmp_path) == []


# ---------------------------------------------------------------------------
# _finish_stack: ``stacked`` is set from the file on disk, not from reaching
# the end of the function.
# ---------------------------------------------------------------------------


def _finish_stack_env(tmp_path) -> tuple[dict, Path]:
    capture = tmp_path / "color_capture"
    night_dir = capture / CAM / NIGHT
    night_dir.mkdir(parents=True)
    mkv = night_dir / CHUNK
    mkv.write_bytes(b"\x00" * 1024)
    cfg = {"videocapture_path": str(capture), "stations": {CAM: {"rotate": False}}}
    return cfg, mkv


def _finish(cfg: dict, mkv: Path) -> bool:
    frame = np.zeros((16, 16, 3), dtype=np.uint8)
    return stacker._finish_stack(
        mkv, frame, frame, CAM, NIGHT, "20260818", "215030", cfg, True,
    )


class TestFinishStackFlagsOnlyRealOutput:
    def test_a_written_stack_is_flagged(self, tmp_path):
        cfg, mkv = _finish_stack_env(tmp_path)

        assert _finish(cfg, mkv) is True

        stack = mkv.parent / "stacks" / STACK_NAME
        assert stack.stat().st_size > 0
        assert flags_manager.load(CAM, NIGHT, cfg)["chunks"][CHUNK]["stacked"] is True

    def test_an_empty_stack_is_not_flagged(self, tmp_path, monkeypatch, caplog):
        cfg, mkv = _finish_stack_env(tmp_path)

        def _write_nothing(image, path: Path) -> None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"")

        monkeypatch.setattr(stacker, "_save_webp_lossless", _write_nothing)

        with caplog.at_level(logging.WARNING):
            assert _finish(cfg, mkv) is False

        # Left unflagged, the next pass rebuilds the chunk instead of leaving a
        # 0-byte stack on the dashboard forever.
        assert flags_manager.load(CAM, NIGHT, cfg)["chunks"].get(CHUNK, {}).get("stacked") is not True
        assert STACK_NAME in caplog.text

    def test_a_failed_overlay_is_reported_with_the_path(self, tmp_path, monkeypatch, caplog):
        cfg, mkv = _finish_stack_env(tmp_path)
        cfg["overlay"] = _overlay_cfg()
        calls: list[tuple[int, int]] = []

        def _refuse(image, *args, **kwargs):
            calls.append(image.size)
            raise RuntimeError("ffmpeg exited 1")

        # The stacker renders the overlay from memory now (render_still); a
        # failure there must leave the same un-annotated stack annotate_still did.
        monkeypatch.setattr(overlay, "render_still", _refuse)

        with caplog.at_level(logging.WARNING):
            result = _finish(cfg, mkv)

        stack = mkv.parent / "stacks" / STACK_NAME
        assert calls == [(16, 16)], "the overlay must be attempted"
        assert str(stack) in caplog.text, "a discarded False is how this went unnoticed for a week"
        # The un-annotated stack is a real image, so the chunk is done: the
        # dashboard's overlay gate brands it on the way out.
        assert result is True
        assert stack.stat().st_size > 0
        assert overlay.still_is_overlaid(stack) is False


# ---------------------------------------------------------------------------
# The per-chunk lock: two stackers can no longer write the same stack.
# ---------------------------------------------------------------------------


class TestChunkStackLock:
    def test_the_second_stacker_stands_down(self, tmp_path):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(b"\x00")

        with stacker.chunk_stack_lock(mkv) as first:
            assert first is True
            with stacker.chunk_stack_lock(mkv) as second:
                assert second is False

    def test_the_lock_is_released_on_exit(self, tmp_path):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(b"\x00")

        with stacker.chunk_stack_lock(mkv) as owned:
            assert owned is True
        with stacker.chunk_stack_lock(mkv) as owned:
            assert owned is True, "a finished stacker must not keep the chunk hostage"

    def test_the_lock_is_released_when_the_body_raises(self, tmp_path):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(b"\x00")

        with pytest.raises(ValueError):
            with stacker.chunk_stack_lock(mkv):
                raise ValueError("stack failed")

        with stacker.chunk_stack_lock(mkv) as owned:
            assert owned is True

    def test_a_different_chunk_is_not_blocked(self, tmp_path):
        mine = tmp_path / CHUNK
        other = tmp_path / f"{CAM}_20260818_215050_color.mkv"
        for p in (mine, other):
            p.write_bytes(b"\x00")

        with stacker.chunk_stack_lock(mine) as a, stacker.chunk_stack_lock(other) as b:
            assert (a, b) == (True, True)

    def test_the_lock_file_sits_beside_the_night_not_in_stacks(self, tmp_path):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(b"\x00")

        with stacker.chunk_stack_lock(mkv):
            locks = list((tmp_path / stacker._STACK_LOCK_DIR).iterdir())
        assert [p.name for p in locks] == [f"{CHUNK}.lock"]
        assert list(tmp_path.glob("*.webp")) == []

    def test_the_wait_is_bounded(self, tmp_path):
        mkv = tmp_path / CHUNK
        mkv.write_bytes(b"\x00")
        lock_path = tmp_path / stacker._STACK_LOCK_DIR / f"{CHUNK}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with stacker.chunk_stack_lock(mkv, timeout=0.6) as owned:
                assert owned is False, "waiting forever would stall the whole night pass"
        finally:
            os.close(fd)

    def test_process_chunk_skips_a_locked_chunk(self, tmp_path, monkeypatch):
        cfg, mkv = _finish_stack_env(tmp_path)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: 720)
        monkeypatch.setattr(
            stacker.flags_manager, "load",
            lambda sid, night, c: {"chunks": {CHUNK: {"ready": True, "stacked": False}}},
        )

        def _must_not_decode(*args, **kwargs):
            raise AssertionError("a locked chunk must not be decoded a second time")

        monkeypatch.setattr(stacker, "_compute_maxpixel", _must_not_decode)

        lock_path = mkv.parent / stacker._STACK_LOCK_DIR / f"{CHUNK}.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            assert stacker.process_chunk(mkv, cfg) is False
        finally:
            os.close(fd)

    def test_process_chunk_runs_when_the_chunk_is_free(self, tmp_path, monkeypatch):
        cfg, mkv = _finish_stack_env(tmp_path)
        frame = np.zeros((16, 16, 3), dtype=np.uint8)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: 720)
        monkeypatch.setattr(
            stacker.flags_manager, "load",
            lambda sid, night, c: {"chunks": {CHUNK: {"ready": True, "stacked": False}}},
        )
        monkeypatch.setattr(
            stacker, "_compute_maxpixel", lambda *a, **k: (frame, frame, None, 10),
        )

        assert stacker.process_chunk(mkv, cfg) is True
        assert (mkv.parent / "stacks" / STACK_NAME).stat().st_size > 0
