"""The full-night re-encode stops on stations that cannot finish it.

Measured 2026-09-24: the full-night pass (encoder.process_night, on wherever
VAAPI works) burned 18 h of ffmpeg wall on gmnro02 and 15 h on gmnro03, gmnro04
was still on the previous evening's chunks when the deadline cut it off, and on
gmnro11 the output was twice the size of the capture. Owner's rule: a station
that misses the deadline stops the full re-encode; detection and locked clips
are still encoded exactly as before.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

import dawn_process
import encoder
import flags_manager

CAM_A = "RO000H"
CAM_B = "RO000J"
NIGHT = "20260322"


def _outcome(i: int, wall: float = 15.0, src: int = 20_000_000,
             dst: int = 18_000_000) -> encoder.ChunkOutcome:
    return encoder.ChunkOutcome(name=f"c{i}", wall_s=wall, src_bytes=src,
                                dst_bytes=dst, kept_source=dst >= src)


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "log_path": str(tmp_path / "logs"),
        "stations": {CAM_A: {"rotate": False}, CAM_B: {"rotate": False}},
        "services": {"reencode": {"enabled": True, "full_night": "auto"}},
        "dawn_encode_parallelism": 2,
    }


def _make_night(cfg: dict, cam: str, n: int) -> list[Path]:
    night = Path(cfg["videocapture_path"]) / cam / NIGHT
    night.mkdir(parents=True, exist_ok=True)
    chunks = []
    for i in range(n):
        f = night / f"{cam}_{NIGHT}_2{i // 3600:01d}{(i // 60) % 60:02d}{i % 60:02d}_color.mkv"
        f.write_bytes(b"x")
        chunks.append(f)
    flags_manager.mark_ready_batch(cam, NIGHT, [c.name for c in chunks], cfg)
    return chunks


def _deadline(seconds_left: float) -> dawn_process.BulkDeadline:
    d = dawn_process.BulkDeadline(enabled=True)
    d.passed = lambda: False
    d.seconds_left = lambda: seconds_left
    return d


class _FakeEncoder:
    """Stands in for encoder.process_night: one outcome per chunk, polling
    should_abort before each, like the real worker pool does."""

    def __init__(self, cfg: dict, wall: float = 15.0, dst: int = 18_000_000,
                 after_each=None) -> None:
        self.cfg = cfg
        self.wall = wall
        self.dst = dst
        self.after_each = after_each
        self.calls: list[str] = []
        self.encoded = 0

    def __call__(self, station_id, date_str, cfg, should_abort=None, *,
                 on_encoded=None) -> bool:
        self.calls.append(station_id)
        night = Path(cfg["videocapture_path"]) / station_id / date_str
        for mkv in sorted(night.glob("*_color.mkv")):
            if should_abort is not None and should_abort():
                return False
            self.encoded += 1
            on_encoded(encoder.ChunkOutcome(mkv.name, self.wall, 20_000_000,
                                            self.dst, self.dst >= 20_000_000))
            if self.after_each is not None:
                self.after_each(self.encoded)
        return True


# ---------------------------------------------------------------------------
# The budget itself
# ---------------------------------------------------------------------------

class TestBudgetVerdict:
    def test_no_verdict_before_the_warmup_sample(self):
        b = dawn_process.FullNightBudget(1000, 2, lambda: 60.0, warmup_chunks=5)
        for i in range(4):
            b.record(_outcome(i))
        assert b.decision is None and not b.stopped()

    def test_projected_miss_stops_right_after_warmup(self):
        """996 left x 15 s / 2 workers = 7470 s > 600 s to the deadline."""
        b = dawn_process.FullNightBudget(1000, 2, lambda: 600.0, warmup_chunks=4,
                                         judge_after_s=0)
        for i in range(4):
            b.record(_outcome(i))
        assert b.stopped()
        assert b.decision["reason"] == "projected_miss"
        assert b.decision["projected_s"] == 7470
        assert b.decision["seconds_left"] == 600

    def test_a_pass_that_fits_keeps_going(self):
        b = dawn_process.FullNightBudget(100, 4, lambda: 3600.0, warmup_chunks=4)
        for i in range(50):
            b.record(_outcome(i))
        assert b.decision is None and not b.stopped()

    def test_net_enlarging_sample_stops(self):
        b = dawn_process.FullNightBudget(100, 2, lambda: 1e6, warmup_chunks=4)
        for i in range(4):
            b.record(_outcome(i, src=20_000_000, dst=21_000_000))
        assert b.stopped()
        assert b.decision["reason"] == "enlarges"

    def test_no_deadline_clock_means_no_projection(self):
        """--date manual runs have no deadline; only the size check applies."""
        b = dawn_process.FullNightBudget(10_000, 1, None, warmup_chunks=2)
        for i in range(5):
            b.record(_outcome(i, wall=100.0))
        assert b.decision is None

    def test_not_enforced_records_but_never_stops(self):
        b = dawn_process.FullNightBudget(1000, 2, lambda: 1.0, enforce=False,
                                         warmup_chunks=2, judge_after_s=0)
        for i in range(3):
            b.record(_outcome(i))
        assert b.decision["reason"] == "projected_miss"
        assert not b.stopped()

    def test_no_projected_miss_before_the_pass_has_run_long_enough(self):
        now = [0.0]
        b = dawn_process.FullNightBudget(1000, 2, lambda: 600.0, warmup_chunks=4,
                                         judge_after_s=900, clock=lambda: now[0])
        for i in range(20):
            now[0] = i * 10.0
            b.record(_outcome(i))
        assert b.decision is None and not b.stopped()
        now[0] = 900.0
        b.record(_outcome(20))
        assert b.decision["reason"] == "projected_miss"
        assert b.decision["elapsed_s"] == 900

    def test_a_slow_start_under_rms_post_processing_does_not_stop_the_pass(self):
        """gmnro22 (Ferigile), 2026-10-26: 26.8 s per chunk while RMS detected,
        11 s once it finished. 4241 chunks x 11 s / 3 workers fits in 31555 s;
        x 26.8 s does not. The verdict must come from the recent rate."""
        now = [0.0]
        b = dawn_process.FullNightBudget(4253, 3, lambda: 31555.0, judge_after_s=900,
                                         clock=lambda: now[0])
        i = 0
        while now[0] < 600:
            b.record(_outcome(i, wall=26.8, dst=3_000_000))
            now[0] += 26.8 / 3
            i += 1
        while now[0] < 1200:
            b.record(_outcome(i, wall=11.0, dst=3_000_000))
            now[0] += 11.0 / 3
            i += 1
        assert b.decision is None and not b.stopped()
        assert b._numbers()["recent_wall_s"] == 11.0

    def test_a_pass_that_stays_slow_is_still_stopped(self):
        now = [0.0]
        b = dawn_process.FullNightBudget(4253, 3, lambda: 31555.0, judge_after_s=900,
                                         clock=lambda: now[0])
        i = 0
        while not b.stopped() and now[0] < 2000:
            b.record(_outcome(i, wall=26.8, dst=3_000_000))
            now[0] += 26.8 / 3
            i += 1
        assert b.decision["reason"] == "projected_miss"
        assert 900 <= b.decision["elapsed_s"] < 910

    def test_default_warmup_scales_with_workers(self):
        assert dawn_process.FullNightBudget(1, 2, None).warmup == 12
        assert dawn_process.FullNightBudget(1, 6, None).warmup == 18


# ---------------------------------------------------------------------------
# Phase 7 wiring
# ---------------------------------------------------------------------------

class TestPhaseSevenStops:
    def test_projected_miss_stops_the_pass_and_writes_the_marker(self, cfg, monkeypatch):
        monkeypatch.setattr(dawn_process, "_BUDGET_JUDGE_AFTER_S", 0.0)
        _make_night(cfg, CAM_A, 200)
        _make_night(cfg, CAM_B, 200)
        fake = _FakeEncoder(cfg)
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"), \
             patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done") as done:
            dawn_process._run_reencode_all(cfg["stations"], NIGHT, cfg, _deadline(600))

        # Stopped right after the 12-chunk warm-up; camera B never started.
        assert fake.encoded == 12
        assert fake.calls == [CAM_A]
        # The night still completes for both cameras.
        assert done.call_count == 2
        marker = json.loads(flags_manager.FULL_NIGHT_MARKER_PATH.read_text())
        assert marker["reason"] == "projected_miss"
        assert marker["night"] == NIGHT
        assert marker["pending_chunks"] == 388
        assert marker["workers"] == 2

    def test_deadline_hit_during_the_pass_writes_the_marker(self, cfg):
        _make_night(cfg, CAM_A, 50)
        _make_night(cfg, CAM_B, 50)
        hit = threading.Event()
        fake = _FakeEncoder(cfg, after_each=lambda n: hit.set() if n == 5 else None)
        d = _deadline(1e6)
        d.passed = hit.is_set
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"), \
             patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done") as done:
            dawn_process._run_reencode_all(cfg["stations"], NIGHT, cfg, d)

        assert fake.encoded == 5
        done.assert_not_called()   # the caller's deferred-done pass handles it
        marker = json.loads(flags_manager.FULL_NIGHT_MARKER_PATH.read_text())
        assert marker["reason"] == "deadline_hit"
        assert marker["pending_chunks"] == 95

    def test_enlarging_pass_stops_and_writes_the_marker(self, cfg):
        _make_night(cfg, CAM_A, 40)
        fake = _FakeEncoder(cfg, dst=30_000_000)
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"), \
             patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done"):
            dawn_process._run_reencode_all({CAM_A: {}}, NIGHT, cfg, _deadline(1e6))
        assert fake.encoded == 12
        marker = flags_manager.read_full_night_marker()
        assert marker["reason"] == "enlarges"

    def test_a_pass_that_fits_writes_no_marker(self, cfg):
        _make_night(cfg, CAM_A, 30)
        fake = _FakeEncoder(cfg)
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"), \
             patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done") as done:
            dawn_process._run_reencode_all({CAM_A: {}}, NIGHT, cfg, _deadline(1e6))
        assert fake.encoded == 30
        assert done.call_count == 1
        assert flags_manager.read_full_night_marker() is None


class TestPerNightVerdict:
    """The verdict is re-taken every morning: a stop never carries over."""

    def _write_marker(self):
        flags_manager.write_full_night_marker({"reason": "projected_miss", "night": "20260321"})

    def test_auto_runs_again_the_morning_after_a_stop(self, cfg):
        self._write_marker()
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"):
            assert dawn_process._full_night_reencode_enabled(cfg) is True

    def test_a_completed_pass_clears_the_last_stop(self, cfg):
        self._write_marker()
        _make_night(cfg, CAM_A, 20)
        fake = _FakeEncoder(cfg)
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"), \
             patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done"):
            dawn_process._run_reencode_all({CAM_A: {}}, NIGHT, cfg, _deadline(1e6))
        assert fake.encoded == 20
        assert flags_manager.read_full_night_marker() is None

    def test_explicit_true_ignores_the_marker(self, cfg):
        self._write_marker()
        cfg["services"]["reencode"]["full_night"] = True
        assert dawn_process._full_night_reencode_enabled(cfg) is True

    def test_explicit_true_is_never_stopped_or_marked(self, cfg):
        """An operator override: the verdict is logged, the pass carries on."""
        cfg["services"]["reencode"]["full_night"] = True
        _make_night(cfg, CAM_A, 40)
        fake = _FakeEncoder(cfg)
        with patch.object(dawn_process.encoder, "process_night", side_effect=fake), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done"):
            dawn_process._run_reencode_all({CAM_A: {}}, NIGHT, cfg, _deadline(10))
        assert fake.encoded == 40
        assert flags_manager.read_full_night_marker() is None

    def test_explicit_false_is_unaffected(self, cfg):
        cfg["services"]["reencode"]["full_night"] = False
        _make_night(cfg, CAM_A, 10)
        with patch.object(dawn_process.encoder, "process_night") as pn, \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(flags_manager, "mark_morning_done") as done:
            dawn_process._run_reencode_all({CAM_A: {}}, NIGHT, cfg, _deadline(10))
        pn.assert_not_called()
        assert done.call_count == 1
        assert flags_manager.read_full_night_marker() is None

    def test_unreadable_marker_counts_as_present(self):
        flags_manager.FULL_NIGHT_MARKER_PATH.write_text("{not json")
        assert flags_manager.read_full_night_marker() == {"reason": "unreadable"}


class TestDetectionClipsUnaffected:
    def test_locked_clip_phase_runs_with_the_marker_present(self, cfg):
        flags_manager.write_full_night_marker({"reason": "deadline_hit", "night": NIGHT})
        chunks = _make_night(cfg, CAM_A, 2)
        flags_manager.lock_chunk(CAM_A, NIGHT, chunks[0].name, {"lock_type": "detection"}, cfg)
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk", return_value=True), \
             patch.object(dawn_process.encoder, "process_chunk", return_value=True) as pc:
            dawn_process._run_encode_locked_all({CAM_A: {}}, NIGHT, cfg)
        assert [c.args[0].name for c in pc.call_args_list] == [chunks[0].name]
        # Phase 1 is not the full-night pass: no budget hook on these encodes.
        assert pc.call_args.kwargs == {}

    def test_locked_only_pass_does_not_use_the_size_guard(self, cfg):
        chunks = _make_night(cfg, CAM_A, 2)
        flags_manager.lock_chunk(CAM_A, NIGHT, chunks[0].name, {"lock_type": "detection"}, cfg)
        with patch.object(encoder, "process_chunk", return_value=True) as pc:
            encoder.process_night_locked_only(CAM_A, NIGHT, cfg)
        assert pc.call_count == 1
        assert pc.call_args.kwargs == {}


# ---------------------------------------------------------------------------
# Per-chunk size guard inside process_chunk
# ---------------------------------------------------------------------------

class _FakePopen:
    """ffmpeg that writes the next of ``sizes`` bytes (else ``out_size``) to
    the command's last argument, and records every command it ran."""
    out_size = 0
    sizes: list[int] = []
    cmds: list[list[str]] = []

    def __init__(self, cmd, **kw):
        self.cmd = cmd
        self.returncode = 0
        _FakePopen.cmds.append(cmd)

    def communicate(self, timeout=None):
        size = _FakePopen.sizes.pop(0) if _FakePopen.sizes else _FakePopen.out_size
        Path(self.cmd[-1]).write_bytes(b"e" * size)
        return b"", b""

    def kill(self):
        pass


def _fake_build_cmd(src, dst_tmp, level, *a, quant_offset=0, **k):
    # The quantizer the real _build_cmd would use rides along in the command.
    return ["ffmpeg", f"level={level}", f"offset={quant_offset}", str(dst_tmp)]


def _offsets() -> list[int]:
    return [int(c[2].split("=")[1]) for c in _FakePopen.cmds]


class TestSizeGuard:
    """A re-encode never replaces a clip with a bigger one (2026-09-24: gmnro11
    wrote 2.25x its capture at QP19), for every caller and every clip."""
    SRC = b"s" * 1000

    @pytest.fixture
    def chunk(self, cfg, monkeypatch):
        import color_calibration as cc
        c = _make_night(cfg, CAM_A, 1)[0]
        c.write_bytes(self.SRC)
        monkeypatch.setattr(cc, "adaptive_enabled", lambda *a, **k: False)
        monkeypatch.setattr(cc, "resolve_color_post",
                            lambda *a, **k: {"contrast": 1.0, "vibrance": 0.0, "gamma": 1.0})
        monkeypatch.setattr(cc, "resolve_calibration_from_mkv",
                            lambda *a, **k: (1.0, 1.0, 1.0, 1.0))
        monkeypatch.setattr(encoder, "enrich_station_cfg_pointing", lambda s, sc, c: sc)
        monkeypatch.setattr(encoder, "_find_vaapi_device", lambda *a, **k: None)
        monkeypatch.setattr(encoder, "_build_cmd", _fake_build_cmd)
        monkeypatch.setattr(encoder.subprocess, "Popen", _FakePopen)
        encoder._calibration_cache.clear()
        encoder._quant_floor.clear()
        _FakePopen.sizes = []
        _FakePopen.cmds = []
        return c

    def _run(self, chunk, cfg, out_size, sizes=()):
        _FakePopen.out_size = out_size
        _FakePopen.sizes = list(sizes)
        outcomes: list[encoder.ChunkOutcome] = []
        ok = encoder.process_chunk(chunk, cfg, on_encoded=outcomes.append)
        state = flags_manager.load(CAM_A, NIGHT, cfg)["chunks"][chunk.name]
        return ok, outcomes, state

    def _second_chunk(self, cfg, cam=CAM_A) -> Path:
        night = Path(cfg["videocapture_path"]) / cam / NIGHT
        night.mkdir(parents=True, exist_ok=True)
        c = night / f"{cam}_{NIGHT}_230000_color.mkv"
        c.write_bytes(self.SRC)
        flags_manager.mark_ready_batch(cam, NIGHT, [c.name], cfg)
        return c

    def test_smaller_output_replaces_the_source_first_time(self, chunk, cfg):
        ok, outcomes, state = self._run(chunk, cfg, 400)
        assert chunk.stat().st_size == 400
        assert outcomes[0].kept_source is False
        assert state["reencoded"] is True
        assert _offsets() == [0]

    def test_too_big_encode_is_retried_coarser_and_the_fit_replaces_it(self, chunk, cfg):
        ok, outcomes, state = self._run(chunk, cfg, 0, sizes=[2250, 800])
        assert ok is True
        # 2.25x -> ceil(log(0.9/2.25)/log(0.75)) = 4 steps coarser
        assert _offsets() == [0, 4]
        assert chunk.stat().st_size == 800
        assert outcomes == [encoder.ChunkOutcome(chunk.name, outcomes[0].wall_s,
                                                 1000, 800, False)]
        assert state["reencoded"] is True
        assert not (chunk.parent / (chunk.name + ".tmp.mkv")).exists()

    def test_equal_size_counts_as_too_big(self, chunk, cfg):
        self._run(chunk, cfg, 0, sizes=[1000, 700])
        assert _offsets() == [0, 1]
        assert chunk.stat().st_size == 700

    def test_nothing_fits_keeps_the_source_and_marks_processed(self, chunk, cfg):
        ok, outcomes, state = self._run(chunk, cfg, 1500)
        assert ok is True
        assert len(_FakePopen.cmds) == encoder._MAX_ENCODE_ATTEMPTS
        assert chunk.read_bytes() == self.SRC
        assert state["reencoded"] is True           # not retried, nothing waits
        assert outcomes[0].kept_source is True
        assert outcomes[0].dst_bytes == 1500
        assert not (chunk.parent / (chunk.name + ".tmp.mkv")).exists()

    def test_locked_clip_never_grows_either(self, chunk, cfg):
        flags_manager.lock_chunk(CAM_A, NIGHT, chunk.name, {"lock_type": "detection"}, cfg)
        ok, outcomes, _ = self._run(chunk, cfg, 0, sizes=[2250, 800])
        assert _FakePopen.cmds[0][1] == f"level={encoder.LOCKED_QUALITY_LEVEL}"
        assert chunk.stat().st_size == 800
        self._run(chunk, cfg, 1500)                 # already reencoded: no-op
        chunk2 = self._second_chunk(cfg)
        flags_manager.lock_chunk(CAM_A, NIGHT, chunk2.name, {"lock_type": "detection"}, cfg)
        _FakePopen.cmds = []
        _FakePopen.out_size = 1500
        encoder.process_chunk(chunk2, cfg)
        assert chunk2.read_bytes() == self.SRC      # nothing fit: capture kept

    def test_later_clips_of_the_camera_night_start_at_the_learned_quantizer(self, chunk, cfg):
        self._run(chunk, cfg, 0, sizes=[2250, 800])
        chunk2 = self._second_chunk(cfg)
        _FakePopen.cmds = []
        _FakePopen.sizes = [790]
        encoder.process_chunk(chunk2, cfg)
        assert _offsets() == [4]                    # no second too-big encode
        assert chunk2.stat().st_size == 790

    def test_learned_quantizer_is_per_camera(self, chunk, cfg):
        self._run(chunk, cfg, 0, sizes=[2250, 800])
        other = self._second_chunk(cfg, cam=CAM_B)
        _FakePopen.cmds = []
        _FakePopen.sizes = [600]
        encoder.process_chunk(other, cfg)
        assert _offsets() == [0]

    def test_floor_learned_by_a_meteor_clip_carries_to_the_station_level(self, chunk, cfg):
        # Level 1 (meteor clip) needed 4 steps; the full-night pass at level 3
        # is already 2 steps above level 1, so it starts 2 steps up.
        flags_manager.lock_chunk(CAM_A, NIGHT, chunk.name, {"lock_type": "detection"}, cfg)
        self._run(chunk, cfg, 0, sizes=[2250, 800])
        chunk2 = self._second_chunk(cfg)
        cfg["compression_level"] = 3
        _FakePopen.cmds = []
        _FakePopen.sizes = [700]
        encoder.process_chunk(chunk2, cfg)
        assert _FakePopen.cmds[0][1:3] == ["level=3", "offset=2"]

    def test_quantizer_never_passes_the_cap(self, chunk, cfg):
        cfg["compression_level"] = 4                # 3 steps above level 1
        encoder._quant_floor[(CAM_A, NIGHT)] = encoder._MAX_QUANT_STEPS - 1
        self._run(chunk, cfg, 0, sizes=[5000, 5000, 5000])
        assert max(_offsets()) + 3 == encoder._MAX_QUANT_STEPS
        assert len(_FakePopen.cmds) == 2            # at the cap: stop, keep capture
        assert chunk.read_bytes() == self.SRC
