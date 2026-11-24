"""The stacker's night pass, colour parity and failure handling, pinned end to end.

The 2026-09-26 review of ``stacker.py`` found a decode that could hang dawn, FPN
subtracted twice on encoded chunks, and several restack gaps. These tests pin
the behaviour the rework must keep (which chunks a night pass stacks, clip and
stack colour agreeing) and, section by section, the fixes it adds.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

import color_calibration as cc
import flags_manager
import stacker


CAM = "RO000E"
NIGHT = "20260818"


def _capture(tmp_path: Path, **cfg) -> tuple[dict, Path]:
    capture = tmp_path / "color_capture"
    night_dir = capture / CAM / NIGHT
    night_dir.mkdir(parents=True)
    camera = {"rotate": False, "color_adaptive_stack": {"enabled": False}}
    base = {"videocapture_path": str(capture), "stations": {CAM: camera},
            "overlay": {"enabled": False}}
    base.update(cfg)
    return base, night_dir


def _chunk(night_dir: Path, hhmmss: str, *, ready: bool = True, stacked: bool = False,
           cfg: dict) -> Path:
    mkv = night_dir / f"{CAM}_{NIGHT}_{hhmmss}_color.mkv"
    mkv.write_bytes(b"\x00" * 1024)
    if ready:
        flags_manager.mark_ready(CAM, NIGHT, mkv.name, cfg)
    if stacked:
        flags_manager.mark_stacked(CAM, NIGHT, mkv.name, cfg)
    return mkv


def _frame(value: int = 20, size: tuple[int, int] = (16, 16)) -> np.ndarray:
    return np.full((*size, 3), value, dtype=np.uint8)


# ---------------------------------------------------------------------------
# The night pass stacks exactly the ready, unstacked, single-time chunks.
# ---------------------------------------------------------------------------


class TestNightPassOverMixedChunks:
    def test_only_ready_unstacked_clips_are_stacked(self, tmp_path, monkeypatch, caplog):
        cfg, night_dir = _capture(tmp_path, stack_workers=1)
        raw = _chunk(night_dir, "215030", cfg=cfg)
        not_ready = _chunk(night_dir, "215050", ready=False, cfg=cfg)
        done = _chunk(night_dir, "215110", stacked=True, cfg=cfg)
        stitched = night_dir / f"{CAM}_{NIGHT}_215130_215150_color.mkv"
        stitched.write_bytes(b"\x00" * 1024)
        flags_manager.mark_ready(CAM, NIGHT, stitched.name, cfg)

        decoded: list[str] = []

        def _decode(mkv, *args, **kwargs):
            decoded.append(Path(mkv).name)
            return _frame(), _frame(10), None, 10

        monkeypatch.setattr(stacker, "_compute_maxpixel", _decode)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)

        with caplog.at_level(logging.INFO):
            stacker.process_night(CAM, NIGHT, cfg)

        # A boundary splice whose merged stack is missing is stacked from its
        # own video, under its own two-timestamp name.
        assert decoded == [raw.name, stitched.name]
        chunks = flags_manager.load(CAM, NIGHT, cfg)["chunks"]
        assert chunks[raw.name]["stacked"] is True
        assert not chunks.get(not_ready.name, {}).get("stacked")
        assert chunks[done.name]["stacked"] is True
        assert chunks[stitched.name]["stacked"] is True
        assert (night_dir / "stacks" / f"{CAM}_{NIGHT}_215030_stack.webp").stat().st_size > 0
        assert (night_dir / "stacks" / f"{CAM}_{NIGHT}_215130_215150_stack.webp").stat().st_size > 0
        assert not (night_dir / "stacks" / f"{CAM}_{NIGHT}_215130_stack.webp").exists()
        # Three were pending (not-ready, raw, stitched); the ready two stack.
        assert "Stack complete: 2/3 ok" in caplog.text

    def test_an_empty_stack_is_rebuilt(self, tmp_path, monkeypatch):
        """A writer killed mid-save leaves a 0-byte stack that the upload can
        never send; flagged stacked or not, it is not done."""
        cfg, night_dir = _capture(tmp_path, stack_workers=1)
        empty = _chunk(night_dir, "215030", stacked=True, cfg=cfg)
        good = _chunk(night_dir, "215050", stacked=True, cfg=cfg)
        (night_dir / "stacks").mkdir()
        (night_dir / "stacks" / f"{CAM}_{NIGHT}_215030_stack.webp").write_bytes(b"")
        (night_dir / "stacks" / f"{CAM}_{NIGHT}_215050_stack.webp").write_bytes(b"RIFF")
        decoded: list[str] = []

        def _decode(mkv, *args, **kwargs):
            decoded.append(Path(mkv).name)
            return _frame(), _frame(10), None, 10

        monkeypatch.setattr(stacker, "_compute_maxpixel", _decode)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)
        stacker.process_night(CAM, NIGHT, cfg)
        assert decoded == [empty.name]
        assert (night_dir / "stacks" / f"{CAM}_{NIGHT}_215030_stack.webp").stat().st_size > 0
        assert good.exists()

    def test_a_post_midnight_splice_stacks_into_its_night(self, tmp_path, monkeypatch):
        cfg, night_dir = _capture(tmp_path)
        splice = night_dir / f"{CAM}_20260819_013705_013723_color.mkv"
        splice.write_bytes(b"\x00" * 1024)
        flags_manager.mark_ready(CAM, NIGHT, splice.name, cfg)
        monkeypatch.setattr(stacker, "_compute_maxpixel",
                            lambda *a, **k: (_frame(), _frame(10), None, 10))
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)
        assert stacker.process_chunk(splice, cfg) is True
        assert (night_dir / "stacks" / f"{CAM}_20260819_013705_013723_stack.webp").stat().st_size > 0
        assert flags_manager.load(CAM, NIGHT, cfg)["chunks"][splice.name]["stacked"] is True

    def test_a_locked_splice_is_in_the_night_meteor_stack(self, tmp_path):
        cfg, night_dir = _capture(tmp_path)
        stacks = night_dir / "stacks"
        stacks.mkdir()
        splice = f"{CAM}_{NIGHT}_215130_215150"
        Image.fromarray(_frame(120)).save(stacks / f"{splice}_stack.webp", "WEBP")
        Image.fromarray(_frame(20)).save(stacks / f"{splice}_stack_avg.webp", "WEBP")
        flags_manager.mark_ready(CAM, NIGHT, f"{splice}_color.mkv", cfg)
        flags_manager.lock_chunk(CAM, NIGHT, f"{splice}_color.mkv",
                                 {"lock_type": "detection", "meteor_time": None}, cfg)
        out = stacker.build_night_color_meteor_stack(CAM, NIGHT, cfg)
        assert out is not None and out.stat().st_size > 0

    def test_nothing_pending_is_a_no_op(self, tmp_path, monkeypatch, caplog):
        cfg, night_dir = _capture(tmp_path)
        _chunk(night_dir, "215030", stacked=True, cfg=cfg)
        monkeypatch.setattr(stacker, "_compute_maxpixel",
                            lambda *a, **k: pytest.fail("nothing to decode"))
        with caplog.at_level(logging.INFO):
            stacker.process_night(CAM, NIGHT, cfg)
        assert "Nothing to stack" in caplog.text


# ---------------------------------------------------------------------------
# Clip and stack colour agree: both land the sky on one target.
# ---------------------------------------------------------------------------


def _sky_frames(n: int = 12, seed: int = 1) -> np.ndarray:
    """A warm, noisy raw sky (the camera's own cast), plus a few stars."""
    rng = np.random.default_rng(seed)
    base = np.array([30.0, 26.0, 20.0])
    frames = base + rng.normal(0.0, 3.0, size=(n, 72, 128, 3))
    frames[:, rng.integers(0, 72, 40), rng.integers(0, 128, 40)] = 250.0
    return np.clip(frames, 0, 255).astype(np.uint8)


def _sky_ratio(image: np.ndarray) -> tuple[float, float]:
    px = cc.sky_pixels(image)
    r, g, b = (float(v) for v in px.mean(axis=0))
    return r / g, b / g


class TestClipAndStackColourParity:
    def test_the_stack_lands_on_the_clip_target(self, tmp_path):
        frames = _sky_frames()
        station_cfg: dict = {}
        clip_curve = cc.resolve_adaptive_curve(frames, station_cfg, "video")
        assert clip_curve is not None
        clip_frame = cc.apply_adaptive_np(frames[0], clip_curve, neutral_highlights=False)

        maxpx = frames.max(axis=0)
        cfg, night_dir = _capture(tmp_path)
        cfg["stations"][CAM] = {"rotate": False}          # adaptive stack colour on
        mkv = night_dir / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00")
        assert stacker._finish_stack(
            mkv=mkv, maxpx=maxpx, avgpx=frames.mean(axis=0).astype(np.uint8),
            station_id=CAM, night_str=NIGHT, date_str=NIGHT, time_str="215030",
            cfg=cfg, is_encoded=False, refpx=None)
        stack = np.asarray(Image.open(night_dir / "stacks" / f"{CAM}_{NIGHT}_215030_stack.webp")
                           .convert("RGB"))

        clip_rg, clip_bg = _sky_ratio(clip_frame)
        stack_rg, stack_bg = _sky_ratio(stack)
        assert abs(clip_rg - stack_rg) < 0.03
        assert abs(clip_bg - stack_bg) < 0.03

    def test_an_encoded_chunk_gets_no_second_curve(self, tmp_path):
        coloured = _sky_frames().max(axis=0)
        cfg, night_dir = _capture(tmp_path)
        cfg["stations"][CAM] = {"rotate": True}           # adaptive on, rotated camera
        mkv = night_dir / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00")
        assert stacker._finish_stack(
            mkv=mkv, maxpx=coloured, avgpx=None,
            station_id=CAM, night_str=NIGHT, date_str=NIGHT, time_str="215030",
            cfg=cfg, is_encoded=True, refpx=None)
        stack = np.asarray(Image.open(night_dir / "stacks" / f"{CAM}_{NIGHT}_215030_stack.webp")
                           .convert("RGB"))
        # The re-encode already coloured and rotated it: published as it is.
        assert np.array_equal(stack, coloured)

    def test_colour_stack_is_pure_and_colours_avg_with_max(self):
        # The split lets parity be checked without a disk: same curve on both.
        frames = _sky_frames()
        maxpx, avgpx = frames.max(axis=0), frames.mean(axis=0).astype(np.uint8)
        out_max, out_avg = stacker.colour_stack(
            maxpx.copy(), avgpx.copy(), None, cfg={}, station_cfg={}, is_encoded=False)
        curve = cc.resolve_adaptive_curve(maxpx, {}, "stack")
        assert np.array_equal(out_max, cc.apply_adaptive_np(maxpx, curve))
        assert np.array_equal(out_avg, cc.apply_adaptive_np(avgpx, curve))

        same_max, same_avg = stacker.colour_stack(
            maxpx, avgpx, None, cfg={}, station_cfg={"rotate": True}, is_encoded=True)
        assert same_max is maxpx and same_avg is avgpx


# ---------------------------------------------------------------------------
# A decode never blocks on ffmpeg's error output.
# ---------------------------------------------------------------------------

import os  # noqa: E402
import sys  # noqa: E402
import threading  # noqa: E402

SMALL_H, SMALL_W = 24, 40


def _fake_ffmpeg(bindir: Path, *, stderr_bytes: int, frames: int, rc: int = 0) -> None:
    """An ``ffmpeg`` on PATH that floods stderr before writing any frame."""
    bindir.mkdir(parents=True, exist_ok=True)
    script = bindir / "ffmpeg"
    script.write_text(
        f"#!{sys.executable}\n"
        "import sys\n"
        "line = b'[h264 @ 0x55] error while decoding MB 12 7, bytestream -5\\n'\n"
        f"sys.stderr.buffer.write(line * ({stderr_bytes} // len(line) + 1))\n"
        "sys.stderr.buffer.flush()\n"
        f"frame = bytes([9]) * {SMALL_H * SMALL_W * 3}\n"
        f"for _ in range({frames}):\n"
        "    sys.stdout.buffer.write(frame)\n"
        "sys.stdout.buffer.flush()\n"
        f"sys.exit({rc})\n"
    )
    script.chmod(0o755)


@pytest.fixture
def small_decode(monkeypatch, tmp_path):
    monkeypatch.setattr(stacker, "HEIGHT", SMALL_H)
    monkeypatch.setattr(stacker, "WIDTH", SMALL_W)
    monkeypatch.setattr(stacker, "passthrough_args", lambda: ["-fps_mode", "passthrough"])
    bindir = tmp_path / "bin"
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return bindir


def _decode_in_thread(mkv: Path, timeout: float = 20.0):
    result: dict = {}

    def run() -> None:
        result["value"] = stacker._compute_maxpixel(mkv)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "the decode hung on a full stderr pipe"
    return result["value"]


class TestDecodeNeverBlocksOnStderr:
    def test_100_kb_of_decode_errors_do_not_hang_the_stacker(self, small_decode, tmp_path,
                                                             caplog):
        _fake_ffmpeg(small_decode, stderr_bytes=100_000, frames=5)
        with caplog.at_level(logging.INFO):
            maxpx, avgpx, _, n = _decode_in_thread(tmp_path / "damaged.mkv")
        assert n == 5
        assert (maxpx == 9).all() and (avgpx == 9).all()
        # Concealed damage is still reported, briefly.
        assert "reported" in caplog.text and "error while decoding MB" in caplog.text

    def test_a_failed_decode_logs_its_tail(self, small_decode, tmp_path, caplog):
        _fake_ffmpeg(small_decode, stderr_bytes=200, frames=0, rc=1)
        with caplog.at_level(logging.WARNING):
            got = _decode_in_thread(tmp_path / "broken.mkv")
        assert got == (None, None, None, 0)
        assert "rc=1" in caplog.text and "error while decoding MB" in caplog.text


# ---------------------------------------------------------------------------
# A decode that stalls (hung USB disk, bad stream) is killed, not waited on.
# ---------------------------------------------------------------------------

class _StallingFfmpeg:
    """A decode that writes *frames* frames and then stops sending, without
    closing its stdout, until it is killed. Time is simulated: the stall fires
    the armed watchdog timer instead of waiting for it."""

    def __init__(self, frames: int, timers: list):
        self._data = bytearray(bytes([9]) * (SMALL_H * SMALL_W * 3 * frames))
        self._timers = timers
        self.killed = False
        self.stdout = self

    def readinto(self, view) -> int:
        if self._data:
            n = min(len(view), len(self._data))
            view[:n] = self._data[:n]
            del self._data[:n]
            return n
        if not self.killed:
            armed = [t for t in self._timers if t.started and not t.cancelled]
            assert armed, "the decode stalled with no watchdog armed"
            armed[0].function()
        return 0  # a killed ffmpeg closes its end of the pipe

    def fileno(self) -> int:
        raise OSError("not a pipe")

    def close(self) -> None:
        pass

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout=None) -> int:
        return -9 if self.killed else 0


class _FakeTimer:
    def __init__(self, interval: float, function, timers: list):
        self.interval, self.function = interval, function
        self.started = self.cancelled = False
        self.daemon = False
        timers.append(self)

    def start(self) -> None:
        self.started = True

    def cancel(self) -> None:
        self.cancelled = True


class TestStalledDecodeIsKilled:
    @pytest.fixture
    def stall(self, monkeypatch):
        monkeypatch.setattr(stacker, "HEIGHT", SMALL_H)
        monkeypatch.setattr(stacker, "WIDTH", SMALL_W)
        monkeypatch.setattr(stacker, "passthrough_args", lambda: ["-fps_mode", "passthrough"])
        timers: list = []
        monkeypatch.setattr(stacker.threading, "Timer",
                            lambda interval, fn: _FakeTimer(interval, fn, timers))

        def run(frames: int, **kw):
            proc = _StallingFfmpeg(frames, timers)
            monkeypatch.setattr(stacker.subprocess, "Popen", lambda cmd, **k: proc)
            return proc, timers, stacker._compute_maxpixel(Path(
                f"{CAM}_{NIGHT}_215030_color.mkv"), **kw)

        return run

    def test_a_stalled_decode_is_killed_and_fails(self, stall, caplog):
        with caplog.at_level(logging.WARNING):
            proc, timers, got = stall(frames=15)
        assert proc.killed
        # Frames read before the stall are not a stack: the chunk stays
        # unstacked and the next pass retries it.
        assert got == (None, None, None, 0)
        assert "stalled" in caplog.text

    def test_a_finished_decode_cancels_its_watchdog(self, monkeypatch):
        monkeypatch.setattr(stacker, "HEIGHT", SMALL_H)
        monkeypatch.setattr(stacker, "WIDTH", SMALL_W)
        monkeypatch.setattr(stacker, "passthrough_args", lambda: ["-fps_mode", "passthrough"])
        timers: list = []
        monkeypatch.setattr(stacker.threading, "Timer",
                            lambda interval, fn: _FakeTimer(interval, fn, timers))
        import io
        data = bytes([9]) * (SMALL_H * SMALL_W * 3 * 5)

        class _Done:
            stdout = io.BufferedReader(io.BytesIO(data))

            def wait(self, timeout=None):
                return 0

            def kill(self):
                raise AssertionError("a finished decode must not be killed")

        monkeypatch.setattr(stacker.subprocess, "Popen", lambda cmd, **k: _Done())
        maxpx, _, _, n = stacker._compute_maxpixel(Path(f"{CAM}_{NIGHT}_215030_color.mkv"))
        assert n == 5 and (maxpx == 9).all()
        assert len(timers) == 1 and timers[0].started and timers[0].cancelled

    @pytest.mark.parametrize("name, segment, want", [
        # A 20 s chunk gets the floor: the stacker runs at nice 19 / ionice
        # idle beside capture and may legitimately crawl.
        (f"{CAM}_{NIGHT}_215030_color.mkv", 20, 300),
        (f"{CAM}_{NIGHT}_215030_color.mkv", 60, 600),
        # A splice spans its own start..end, across midnight too.
        (f"{CAM}_{NIGHT}_235950_000030_color.mkv", 20, 400),
        ("not_a_chunk.mkv", 20, 300),
    ])
    def test_the_limit_scales_with_the_clip_length(self, name, segment, want):
        assert stacker._decode_timeout_s(Path(name), segment) == want


# ---------------------------------------------------------------------------
# An encoded chunk is taken as it is: no second curve, no pedestal.
# ---------------------------------------------------------------------------


class TestEncodedChunks:
    def _run(self, tmp_path, monkeypatch, state: dict, height: int,
             entry: str = "process_chunk") -> dict:
        cfg, night_dir = _capture(tmp_path, display_levels={"background_subtract_pct": 20.0})
        mkv = night_dir / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00" * 1024)
        chunk = {"ready": True, "stacked": False, **state}
        monkeypatch.setattr(stacker.flags_manager, "load",
                            lambda sid, night, c: {"chunks": {mkv.name: dict(chunk)}})
        monkeypatch.setattr(stacker.flags_manager, "mark_stacked", lambda *a, **k: None)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: height)
        seen: dict = {}

        def _decode(mkv, **kwargs):
            seen.update(kwargs)
            return _frame(), _frame(10), None, 10

        def _finish(*args, **kwargs):
            seen["is_encoded"] = kwargs["is_encoded"]
            return True

        monkeypatch.setattr(stacker, "_compute_maxpixel", _decode)
        monkeypatch.setattr(stacker, "_finish_stack", _finish)
        if entry == "process_chunk":
            assert stacker.process_chunk(mkv, cfg) is True
        else:
            assert stacker.process_night(CAM, NIGHT, cfg) is None
        return seen

    @pytest.mark.parametrize("entry", ["process_chunk", "process_night"])
    def test_an_encoded_chunk_gets_no_pedestal(self, tmp_path, monkeypatch, entry):
        seen = self._run(tmp_path, monkeypatch, {"reencoded": True, "kept_source": False},
                         stacker.HEIGHT + 80, entry)
        assert seen["is_encoded"] is True
        assert seen["bar_h"] == 80
        assert seen["bg_subtract_pct"] is None

    def test_an_encode_without_a_bar_is_still_encoded(self, tmp_path, monkeypatch):
        # overlay.enabled false, or a missing font: a 720-row encoded clip.
        seen = self._run(tmp_path, monkeypatch, {"reencoded": True, "kept_source": False},
                         stacker.HEIGHT)
        assert seen["is_encoded"] is True
        assert seen["bar_h"] == 0
        assert seen["bg_subtract_pct"] is None

    def test_a_kept_capture_is_raw(self, tmp_path, monkeypatch):
        # No encode came out smaller, so the capture stayed: it needs its colour.
        seen = self._run(tmp_path, monkeypatch, {"reencoded": True, "kept_source": True},
                         stacker.HEIGHT)
        assert seen["is_encoded"] is False
        assert seen["bg_subtract_pct"] == 20.0

    def test_older_state_falls_back_to_the_height(self, tmp_path, monkeypatch):
        # Before kept_source was recorded, a flagged 720-row chunk was a kept capture.
        seen = self._run(tmp_path, monkeypatch, {"reencoded": True}, stacker.HEIGHT)
        assert seen["is_encoded"] is False

    def test_a_bar_without_the_flag_is_encoded_and_logged(self, tmp_path, monkeypatch,
                                                          caplog):
        with caplog.at_level(logging.WARNING):
            seen = self._run(tmp_path, monkeypatch, {}, stacker.HEIGHT + 80)
        assert seen["is_encoded"] is True
        assert seen["bg_subtract_pct"] is None
        assert "not flagged re-encoded" in caplog.text


# ---------------------------------------------------------------------------
# A restack replaces everything it owns; a finished chunk is not decoded twice.
# ---------------------------------------------------------------------------


def _finish(cfg: dict, mkv: Path, value: int) -> bool:
    return stacker._finish_stack(
        mkv=mkv, maxpx=_frame(value), avgpx=_frame(value // 2),
        station_id=CAM, night_str=NIGHT, date_str=NIGHT, time_str="215030",
        cfg=cfg, is_encoded=True, refpx=None)


class TestRestack:
    def test_a_restack_regenerates_the_thumbnail(self, tmp_path):
        cfg, night_dir = _capture(tmp_path)
        mkv = night_dir / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00")
        thumb = night_dir / "stacks" / "thumbs" / f"{CAM}_{NIGHT}_215030_stack.webp"

        assert _finish(cfg, mkv, 40)
        first = np.asarray(Image.open(thumb).convert("RGB")).mean()
        assert _finish(cfg, mkv, 200)
        second = np.asarray(Image.open(thumb).convert("RGB")).mean()
        assert second > first + 100, "the thumbnail must follow the restacked image"

    def test_a_chunk_finished_while_waiting_is_not_decoded_again(self, tmp_path, monkeypatch):
        cfg, night_dir = _capture(tmp_path)
        mkv = night_dir / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00" * 1024)
        states = iter([{"ready": True, "stacked": False}, {"ready": True, "stacked": True}])
        monkeypatch.setattr(stacker.flags_manager, "load",
                            lambda sid, night, c: {"chunks": {mkv.name: next(states)}})
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)
        monkeypatch.setattr(stacker, "_compute_maxpixel",
                            lambda *a, **k: pytest.fail("another stacker already did it"))
        assert stacker.process_chunk(mkv, cfg) is True

    def test_the_fallback_ffprobe_is_bounded(self, tmp_path, monkeypatch):
        mkv = tmp_path / "odd.mkv"
        mkv.write_bytes(b"\x00" * 64)
        seen: dict = {}

        def _run(cmd, **kwargs):
            seen.update(kwargs)
            raise stacker.subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

        monkeypatch.setattr(stacker.subprocess, "run", _run)
        assert stacker._probe_video_height(mkv) == stacker.HEIGHT
        assert seen.get("timeout")


class TestMeteorCompositeIsAtomic:
    def _night(self, tmp_path) -> tuple[dict, Path]:
        cfg, night_dir = _capture(tmp_path)
        stacks = night_dir / "stacks"
        stacks.mkdir()
        name = f"{CAM}_{NIGHT}_215030_color.mkv"
        flags_manager.mark_ready(CAM, NIGHT, name, cfg)
        flags_manager.lock_chunk(CAM, NIGHT, name, {"lock_type": "detection"}, cfg)
        Image.fromarray(_frame(120, (48, 64))).save(stacks / f"{CAM}_{NIGHT}_215030_stack.webp",
                                                    "WEBP", lossless=True)
        Image.fromarray(_frame(20, (48, 64))).save(
            stacks / f"{CAM}_{NIGHT}_215030_stack_avg.webp", "WEBP", lossless=True)
        return cfg, stacks

    def test_success_leaves_no_scratch_file(self, tmp_path):
        cfg, stacks = self._night(tmp_path)
        out = stacker.build_night_color_meteor_stack(CAM, NIGHT, cfg)
        assert out is not None and out.stat().st_size > 0
        assert [p.name for p in stacks.iterdir() if ".tmp" in p.name] == []

    def test_a_failed_save_keeps_the_previous_composite(self, tmp_path, monkeypatch):
        cfg, stacks = self._night(tmp_path)
        out = stacker.build_night_color_meteor_stack(CAM, NIGHT, cfg)
        before = out.read_bytes()

        real_save = Image.Image.save

        def _half_write(self, fp, *args, **kwargs):
            Path(fp).write_bytes(b"RIFF")
            raise OSError("disk full")

        monkeypatch.setattr(Image.Image, "save", _half_write)
        assert stacker.build_night_color_meteor_stack(CAM, NIGHT, cfg) is None
        monkeypatch.setattr(Image.Image, "save", real_save)
        assert out.read_bytes() == before


# ---------------------------------------------------------------------------
# Lock files do not pile up, and removing them never lets two stackers in.
# ---------------------------------------------------------------------------

import fcntl  # noqa: E402
import time  # noqa: E402


class TestLockFilesAreCleanedUp:
    def test_a_released_lock_leaves_no_file(self, tmp_path):
        mkv = tmp_path / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00")
        with stacker.chunk_stack_lock(mkv) as owned:
            assert owned is True
        assert list((tmp_path / stacker._STACK_LOCK_DIR).iterdir()) == []

    def test_a_waiter_on_a_removed_file_does_not_share_the_chunk(self, tmp_path):
        mkv = tmp_path / f"{CAM}_{NIGHT}_215030_color.mkv"
        mkv.write_bytes(b"\x00")
        b_has_it = threading.Event()
        b_may_leave = threading.Event()
        b_result: list[bool] = []

        def waiter() -> None:
            # Opens the file while A holds it, then wins it after A unlinks it.
            with stacker.chunk_stack_lock(mkv, timeout=5.0) as owned:
                b_result.append(owned)
                b_has_it.set()
                b_may_leave.wait(5.0)

        with stacker.chunk_stack_lock(mkv) as a:
            assert a is True
            t = threading.Thread(target=waiter, daemon=True)
            t.start()
            time.sleep(0.4)                      # B is now waiting on A's file
        assert b_has_it.wait(5.0)
        try:
            assert b_result == [True]
            with stacker.chunk_stack_lock(mkv) as c:
                assert c is False, "C must not win a fresh file while B holds the chunk"
        finally:
            b_may_leave.set()
            t.join(5.0)

    def test_the_night_pass_sweeps_unheld_lock_files(self, tmp_path, monkeypatch):
        cfg, night_dir = _capture(tmp_path)
        _chunk(night_dir, "215030", stacked=True, cfg=cfg)
        locks = night_dir / stacker._STACK_LOCK_DIR
        locks.mkdir()
        stale = locks / f"{CAM}_{NIGHT}_215010_color.mkv.lock"
        stale.write_bytes(b"")
        held = locks / f"{CAM}_{NIGHT}_215050_color.mkv.lock"
        held.write_bytes(b"")
        fd = os.open(held, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            stacker.process_night(CAM, NIGHT, cfg)
            assert not stale.exists()
            assert held.exists(), "a lock somebody holds must stay"
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Decode threads follow a plan instead of one thread per core per decode.
# ---------------------------------------------------------------------------


class TestDecodeThreadPlan:
    def test_the_realtime_lane_decodes_on_two_threads(self):
        assert stacker.decode_threads({}) == 2

    @pytest.mark.parametrize("cpu, workers, parallelism, want", [
        (8, 2, 2, 2),       # gmnro01: 2 cameras x 2 workers share 8 cores
        (8, 2, None, 2),    # default parallelism cpu // 3 == 2
        (6, 2, 1, 3),
        (4, 2, None, 2),    # cpu // 3 == 1
        (2, 4, 4, 1),       # never below one thread
    ])
    def test_a_night_pass_shares_the_cores_across_cameras_and_workers(
            self, cpu, workers, parallelism, want):
        cfg = {"_cpu_cores": cpu}
        if parallelism is not None:
            cfg["dawn_stack_parallelism"] = parallelism
        assert stacker.decode_threads(cfg, night_workers=workers) == want

    @pytest.mark.parametrize("setting, want", [(3, 3), ("4", 4), (0, 0)])
    def test_an_explicit_setting_wins_everywhere(self, setting, want):
        cfg = {"stacker_ffmpeg_threads": setting, "_cpu_cores": 16}
        assert stacker.decode_threads(cfg) == want
        assert stacker.decode_threads(cfg, night_workers=2) == want

    def test_a_bad_setting_falls_back_to_auto(self, caplog):
        with caplog.at_level(logging.WARNING):
            assert stacker.decode_threads({"stacker_ffmpeg_threads": "lots"}) == 2
        assert "stacker_ffmpeg_threads" in caplog.text

    def test_the_cli_and_the_night_pass_pass_their_plans_to_ffmpeg(self, tmp_path,
                                                                  monkeypatch):
        cfg, night_dir = _capture(tmp_path, stack_workers=2, _cpu_cores=8,
                                  dawn_stack_parallelism=2)
        mkv = _chunk(night_dir, "215030", cfg=cfg)
        monkeypatch.setattr(stacker, "_probe_video_height", lambda p: stacker.HEIGHT)
        seen: list[int] = []

        def _decode(mkv, **kwargs):
            seen.append(kwargs["threads"])
            return None, None, None, 0

        monkeypatch.setattr(stacker, "_compute_maxpixel", _decode)
        stacker.process_chunk(mkv, cfg)
        stacker.process_night(CAM, NIGHT, cfg)
        assert seen == [2, 2]
        cfg["_cpu_cores"] = 16
        stacker.process_night(CAM, NIGHT, cfg)
        assert seen[-1] == 4
