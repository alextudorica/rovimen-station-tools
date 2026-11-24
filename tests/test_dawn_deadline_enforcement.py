"""The 11:00 UTC deadline has to stop a bulk run that is already under way.

Before this, the deadline was read once, just before Phase 4. A run that
started at 04:00 and passed that check never looked at the clock again:
gmnro10 and gmnro12 stacked for ten hours every morning, and the 12:00 reboot
guard — which waits on morning_done and does not reboot on timeout — left
those stations up all day.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import dawn_process
import flags_manager


STATION_A = "RO000H"
STATION_B = "RO000J"
DATE_STR = "20260322"


@pytest.fixture
def cfg(tmp_path):
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "log_path": str(tmp_path / "logs"),
        "stations": {
            STATION_A: {"rotate": False},
            STATION_B: {"rotate": False},
        },
        "services": {"reencode": {"enabled": True}},
    }


class TestDeadlineFlag:
    def test_disabled_never_passes(self):
        """--date is the manual path: it must run to completion."""
        d = dawn_process.BulkDeadline(enabled=False, hour=0)
        assert d.passed() is False
        assert d() is False

    def test_passes_once_the_hour_is_reached(self):
        d = dawn_process.BulkDeadline(enabled=True, hour=11)
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = datetime(2026, 3, 22, 10, 59, tzinfo=timezone.utc)
            assert d.passed() is False
            dt.now.return_value = datetime(2026, 3, 22, 11, 0, tzinfo=timezone.utc)
            assert d.passed() is True

    def test_expired_event_wins_before_the_hour(self):
        """The watchdog fires on its own clock; callers poll one predicate."""
        d = dawn_process.BulkDeadline(enabled=True, hour=23)
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = datetime(2026, 3, 22, 4, 0, tzinfo=timezone.utc)
            assert d() is False
            d.expired.set()
            assert d() is True

    def test_seconds_left_is_zero_past_the_hour(self):
        d = dawn_process.BulkDeadline(enabled=True, hour=11)
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = datetime(2026, 3, 22, 14, 19, tzinfo=timezone.utc)
            assert d.seconds_left() == 0.0


class TestWatchdogEscalation:
    def test_finishing_in_time_neither_expires_nor_kills(self):
        killed = threading.Event()
        d = dawn_process.BulkDeadline(enabled=True, hour=11, grace_s=0.01)
        d._hard_stop = killed.set
        with patch.object(d, "seconds_left", return_value=0.05):
            d.arm()
            d.disarm()
            d._thread.join(timeout=2)
        assert not d.expired.is_set()
        assert not killed.is_set()

    def test_overrunning_the_grace_period_hard_stops(self):
        """A phase that ignores the flag still gets terminated."""
        killed = threading.Event()
        d = dawn_process.BulkDeadline(enabled=True, hour=11, grace_s=0.05)
        d._hard_stop = killed.set
        with patch.object(d, "seconds_left", return_value=0.0):
            d.arm()
            d._thread.join(timeout=5)
        assert d.expired.is_set()
        assert killed.wait(timeout=2)

    def test_hard_stop_runs_the_bookkeeping_before_killing(self):
        """morning_done has to be written, or the reboot guard waits 4 h for a
        process that no longer exists and then refuses to reboot."""
        calls = []
        d = dawn_process.BulkDeadline(enabled=True, hour=11,
                                      on_hard_stop=lambda: calls.append("marked"))
        with patch.object(dawn_process, "_terminate_children",
                          side_effect=lambda: calls.append("killed")), \
             patch.object(dawn_process.os, "_exit",
                          side_effect=lambda code: calls.append(f"exit{code}")):
            d._hard_stop()
        assert calls == ["marked", "killed", "exit3"]


class TestPhasesStopAtTheDeadline:
    def test_stack_all_skips_the_cameras_it_has_not_started(self, cfg):
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._run_stack_all(cfg["stations"], DATE_STR, cfg,
                                        should_abort=lambda: True)
        stack.assert_not_called()

    def test_stack_all_forwards_the_callback_to_the_stacker(self, cfg):
        """Per-camera granularity is not enough — one camera is ~1900 chunks."""
        flag = {"abort": False}
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._run_stack_all(cfg["stations"], DATE_STR, cfg,
                                        should_abort=lambda: flag["abort"])
        assert stack.call_count == len(cfg["stations"])
        flag["abort"] = True
        for c in stack.call_args_list:
            assert c.args[-1]()

    def test_reencode_leaves_an_interrupted_camera_unmarked(self, cfg):
        """The caller's deferred-done pass explains the night; this camera must
        not be recorded as a clean completion."""
        with patch.object(dawn_process.encoder, "process_night"), \
             patch.object(dawn_process, "_stacker_enabled", return_value=False), \
             patch.object(dawn_process, "_full_night_reencode_enabled", return_value=True), \
             patch.object(flags_manager, "mark_morning_done") as marked:
            dawn_process._run_reencode_all(cfg["stations"], DATE_STR, cfg,
                                           should_abort=lambda: True)
        marked.assert_not_called()

    def test_upload_of_full_night_stacks_is_skipped_not_half_done(self, cfg):
        with patch.object(dawn_process.archive_upload, "run_night") as upload:
            dawn_process._run_upload_all(cfg["stations"], DATE_STR, cfg,
                                         should_abort=lambda: True)
        upload.assert_not_called()

    def test_upload_checks_the_deadline_before_every_file(self, cfg):
        """The entry check alone let a slow uplink upload for hours past 13:15
        (gmnro01 (Ghirdoveni), 2026-10-24); run_night must get the check too."""
        abort = Mock(return_value=False)
        with patch.object(dawn_process.archive_upload, "run_night") as upload:
            dawn_process._run_upload_all(cfg["stations"], DATE_STR, cfg,
                                         should_abort=abort)
        assert upload.call_count == len(cfg["stations"])
        for c in upload.call_args_list:
            assert c.args[-1] is abort

    def test_detection_upload_is_bounded_by_the_deadline(self, cfg):
        deadline = dawn_process.BulkDeadline(enabled=True)
        with patch.object(dawn_process, "_run_upload_all") as upload_all:
            dawn_process._run_detection_upload(cfg["stations"], DATE_STR, cfg,
                                               None, deadline)
        assert upload_all.call_args.args[-1] is deadline

    def test_timelapse_defers_every_camera_it_did_not_build(self, cfg):
        with patch.object(dawn_process.timelapse_build, "build") as build, \
             patch.object(flags_manager, "is_timelapse_done", return_value=False), \
             patch.object(flags_manager, "mark_timelapse_deferred") as deferred:
            dawn_process._run_timelapse_all(cfg["stations"], DATE_STR, cfg,
                                            should_abort=lambda: True)
        build.assert_not_called()
        assert {c.args[0] for c in deferred.call_args_list} == set(cfg["stations"])


class TestWorkersStopMidNight:
    def test_stacker_stops_starting_chunks_once_the_flag_flips(self, tmp_path):
        import stacker

        chunks = [tmp_path / f"RO000H_20260322_0000{i:02d}_color.mkv" for i in range(6)]
        for c in chunks:
            c.write_bytes(b"")
        flip = threading.Event()
        started = []

        def fake_process_one(mkv, *a, **kw):
            started.append(mkv)
            flip.set()          # first chunk trips the deadline
            return True

        with patch.object(stacker, "process_chunk", side_effect=fake_process_one):
            ok = stacker._stack_night_perfile(
                "RO000H", chunks, chunks, "20260322", {"stack_workers": 1},
                1, should_abort=flip.is_set,
            )
        assert ok == 1
        assert len(started) == 1

    def test_encoder_stops_starting_chunks_once_the_flag_flips(self, tmp_path):
        import encoder

        night = tmp_path / "color_capture" / "RO000H" / "20260322"
        night.mkdir(parents=True)
        for i in range(6):
            (night / f"RO000H_20260322_0000{i:02d}_color.mkv").write_bytes(b"")
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}

        with patch.object(encoder, "process_chunk") as chunk:
            encoder.process_night("RO000H", "20260322", cfg, should_abort=lambda: True)
        chunk.assert_not_called()


class TestFixedSchedule:
    """2026-09-24: deadline 13:15, reboot 13:45, night rollover 14:00 UTC."""

    def test_deadline_is_13_15(self):
        d = dawn_process.BulkDeadline(enabled=True)
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 24, 13, 14, tzinfo=timezone.utc)
            assert not d.passed()
            dt.now.return_value = datetime(2026, 9, 24, 13, 15, tzinfo=timezone.utc)
            assert d.passed()

    def test_seconds_left_counts_to_13_15(self):
        d = dawn_process.BulkDeadline(enabled=True)
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
            assert d.seconds_left() == 75 * 60

    def test_a_run_before_14_utc_processes_last_night(self):
        from datetime import datetime as real
        with patch.object(dawn_process, "datetime") as dt:
            dt.now.return_value = real(2026, 9, 24, 12, 30, tzinfo=timezone.utc)
            assert dawn_process._night_date() == "20260923"
