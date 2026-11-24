"""Phase 4 starts a camera's full-night stack only if the whole night fits.

Measured 2026-11-17 on gmnro20 (Brăila) after its third and fourth cameras:
4 x 2,379 chunks at 3.6 s per chunk need 9.5 h, the 04:45-13:15 window is
8.5 h. Every morning the stacker ran to the deadline, left three cameras part
stacked and one untouched, and built no timelapse. Owner's rule: check that it
can finish, then spend the hours.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

import dawn_process
import flags_manager

NIGHT = "20261116"
CAMS = ["RO001M", "RO001N", "RO001U", "RO001V"]
S_PER_CHUNK = 3.6


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "log_path": str(tmp_path / "logs"),
        "stations": {c: {"rotate": False} for c in CAMS},
        "services": {"reencode": {"enabled": True}},
        "dawn_stack_parallelism": 1,
        "stack_workers": 2,
    }


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def _deadline(clock: _Clock, deadline_at: float) -> dawn_process.BulkDeadline:
    d = dawn_process.BulkDeadline(enabled=True)
    d.passed = lambda: clock.t >= deadline_at
    d.seconds_left = lambda: max(0.0, deadline_at - clock.t)
    return d


def _run(cfg, clock: _Clock, deadline_at: float | None, per_cam: int):
    """Run Phase 4 with a stacker that takes S_PER_CHUNK of fake time per chunk."""
    stacked: dict[str, int] = {c: 0 for c in CAMS}

    def fake_process_night(sid, date_str, cfg_, abort=None, *, scope=None, on_stacked=None):
        for _ in range(per_cam - stacked[sid]):
            if abort is not None and abort():
                return
            clock.t += S_PER_CHUNK
            stacked[sid] += 1
            if on_stacked is not None:
                on_stacked()

    abort = _deadline(clock, deadline_at) if deadline_at is not None else None
    with patch.object(dawn_process.stacker, "pending_chunks",
                      side_effect=lambda sid, d, c, scope=None: [Path("x")] * (per_cam - stacked[sid])), \
         patch.object(dawn_process.stacker, "process_night", side_effect=fake_process_night), \
         patch.object(dawn_process.time, "monotonic", clock):
        dawn_process._run_stack_all(cfg["stations"], NIGHT, cfg, abort)
    return stacked


def _fits(cfg, cam):
    return dawn_process._stack_fits(cam, NIGHT, cfg)


class TestBrailaNight:
    # 100 chunks per camera = 360 s each, padded to 396 s, plus the 900 s reserve.
    # Three cameras fit in 2,200 s; the fourth would start at 1,080 s with
    # 1,120 s left and needs 1,296 s.
    def test_stacks_the_cameras_that_fit_and_never_starts_the_rest(self, cfg):
        stacked = _run(cfg, _Clock(), deadline_at=2200, per_cam=100)
        order = dawn_process._stack_order(CAMS, NIGHT)
        assert [stacked[c] for c in order] == [100, 100, 100, 0]
        assert [_fits(cfg, c) for c in order] == [True, True, True, False]

    def test_the_verdict_and_its_numbers_are_in_the_night_state(self, cfg):
        _run(cfg, _Clock(), deadline_at=2200, per_cam=100)
        skipped = dawn_process._stack_order(CAMS, NIGHT)[-1]
        plan = flags_manager.peek(skipped, NIGHT, cfg)["stack_plan"]
        assert plan["fits"] is False and plan["reason"] == "projected_miss"
        assert plan["pending_chunks"] == 100
        assert plan["projected_s"] > plan["seconds_left"]

    def test_everything_fits_when_there_is_time(self, cfg):
        stacked = _run(cfg, _Clock(), deadline_at=10_000, per_cam=100)
        assert set(stacked.values()) == {100}
        assert all(_fits(cfg, c) for c in CAMS)

    def test_a_manual_run_has_no_deadline_and_stacks_everything(self, cfg):
        stacked = _run(cfg, _Clock(), deadline_at=None, per_cam=100)
        assert set(stacked.values()) == {100}


class TestProbe:
    def test_a_probe_that_cannot_finish_stops_after_the_warmup(self, cfg):
        # One camera alone needs 2,380 x 3.6 s; only 2 h are left.
        stacked = _run(cfg, _Clock(), deadline_at=7200, per_cam=2380)
        first = dawn_process._stack_order(CAMS, NIGHT)[0]
        warmup = dawn_process._STACK_WARMUP_MIN_CHUNKS
        assert stacked[first] == warmup
        assert not any(_fits(cfg, c) for c in CAMS)
        assert sum(stacked.values()) == warmup

    def test_the_plan_judges_from_the_measured_rate(self):
        clock = _Clock()
        plan = dawn_process.StackPlan({"A": 50, "B": 100}, workers=2,
                                      seconds_left=lambda: 1000.0, clock=clock)
        assert plan.admit("A")                       # probe
        for _ in range(plan.warmup):
            clock.t += 1.0
            plan.record("A")
        assert plan.rate() == pytest.approx(1.0)
        # A: 38 left -> 942 s with the reserve, fits in 1000 s; B adds 100 chunks -> 1052 s.
        assert plan.admitted["A"] == "measured"
        assert not plan.admit("B")


class TestLaterPhasesFollowThePlan:
    @pytest.fixture
    def skipped(self, cfg):
        _run(cfg, _Clock(), deadline_at=2200, per_cam=100)
        return dawn_process._stack_order(CAMS, NIGHT)[-1]

    def test_no_timelapse_for_a_camera_left_on_its_locked_chunks(self, cfg, skipped):
        with patch.object(dawn_process.timelapse_build, "build") as build, \
             patch.object(dawn_process, "_stack_coverage", return_value=(10, 0)):
            dawn_process._run_timelapse_all({skipped: {}}, NIGHT, cfg)
        build.assert_not_called()

    def test_the_retry_sweep_does_not_build_one_from_the_locked_stacks(self, cfg, skipped):
        flags_manager.mark_morning_done(skipped, NIGHT, cfg)
        with patch.object(dawn_process, "datetime") as dt, \
             patch.object(dawn_process.timelapse_build, "build") as build:
            dt.now.return_value = dawn_process.datetime(2026, 11, 19, 6, tzinfo=dawn_process.timezone.utc)
            dawn_process._retry_deferred_timelapses({skipped: {}}, cfg)
        build.assert_not_called()

    def test_the_restack_pass_skips_it(self, cfg, skipped):
        with patch.object(dawn_process, "_stack_coverage", return_value=(10, 10)), \
             patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._verify_and_restack({skipped: {}}, NIGHT, cfg)
        stack.assert_not_called()

    def test_the_final_stack_pass_takes_only_its_locked_chunks(self, cfg, skipped):
        cfg["services"]["reencode"]["full_night"] = False
        cfg["services"]["stacker"] = {"enabled": True}
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._run_reencode_all({skipped: {}}, NIGHT, cfg)
        assert stack.call_args.kwargs["scope"] == "detections"


def test_the_left_out_camera_rotates_from_night_to_night():
    lasts = {dawn_process._stack_order(CAMS, d)[-1]
             for d in ("20261116", "20261117", "20261118", "20261119")}
    assert lasts == set(CAMS)
