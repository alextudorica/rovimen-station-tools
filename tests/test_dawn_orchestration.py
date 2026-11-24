"""Characterization tests for dawn_process orchestration.

These pin what the morning run does end to end (phase order, the RMS gate, the
two deadline gates) and the helper phases that had no tests of their own, so
the phase runner and the per-phase isolation can be changed without silently
reordering or dropping work.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime as _real_datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import dawn_process
import flags_manager


STATION_A = "RO000H"
STATION_B = "RO000J"
NIGHT = "20260322"
SCRIPT = Path(dawn_process.__file__)


class _FrozenClock(_real_datetime):
    """``datetime`` whose ``now`` is pinned; everything else is real."""

    frozen = _real_datetime(2026, 3, 23, 5, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # noqa: D102 - mirrors datetime.now
        return cls.frozen if tz is not None else cls.frozen.replace(tzinfo=None)


@pytest.fixture
def frozen(monkeypatch):
    """Pin dawn_process's clock to 05:00 UTC the morning after NIGHT."""
    _FrozenClock.frozen = _real_datetime(2026, 3, 23, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(dawn_process, "datetime", _FrozenClock)
    return _FrozenClock


def _session(rms: Path, station: str, night: str, hhmmss: str = "210000", *,
             ftpdet: bool = True, sub: str = "ArchivedFiles", ff: bool = True) -> Path:
    date_s = night
    if int(hhmmss[:2]) < 12:
        # A session that started after midnight is filed under the next date.
        d = _real_datetime.strptime(night, "%Y%m%d")
        date_s = d.replace(day=d.day + 1).strftime("%Y%m%d")
    s = rms / sub / f"{station}_{date_s}_{hhmmss}_000000"
    s.mkdir(parents=True, exist_ok=True)
    if ff:
        (s / f"FF_{station}_{date_s}_{hhmmss}_000_0000000.fits").write_bytes(b"x")
    if ftpdet:
        (s / f"FTPdetectinfo_{station}_{date_s}_{hhmmss}_000000.txt").write_text("x")
    return s


@pytest.fixture
def cfg(tmp_path):
    rms_a = tmp_path / "rms_a"
    rms_b = tmp_path / "rms_b"
    rms_a.mkdir()
    rms_b.mkdir()
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "log_path": str(tmp_path / "logs"),
        "stations": {
            STATION_A: {"rms_data_path": str(rms_a), "rotate": False},
            STATION_B: {"rms_data_path": str(rms_b), "rotate": False},
        },
        "services": {
            "stacker": {"enabled": True},
            "reencode": {"enabled": True},
            "archive_upload": {"enabled": True},
            "timelapse_build": {"enabled": True},
            "overlay_details": {"enabled": True},
        },
    }


def _write_cfg(tmp_path: Path, cfg: dict) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(cfg))
    return p


def _both_rms_done(cfg: dict) -> None:
    for sid, scfg in cfg["stations"].items():
        _session(Path(scfg["rms_data_path"]), sid, NIGHT)


# Phase functions _main_locked orchestrates, recorded by name.
_PHASES = (
    "_retry_deferred_timelapses",
    "_reconcile_ready",
    "_run_encode_locked_all",
    "_run_overlay_details_all",
    "_run_upload_all",
    "_run_detection_backfill",
    "_run_stack_all",
    "_verify_and_restack",
    "_run_backfill_upload",
    "_run_timelapse_all",
    "_run_reencode_all",
    "_sweep_stale_locks",
)


def _run_main(cfg_path: Path, *extra: str, record: list | None = None,
              overrides: dict | None = None) -> int:
    """Run _main_locked with every phase replaced by a recorder."""
    calls = record if record is not None else []
    overrides = overrides or {}
    patches = []
    for name in _PHASES:
        side = overrides.get(name, (lambda n: (lambda *a, **k: calls.append(n)))(name))
        patches.append(patch.object(dawn_process, name, side_effect=side))
    patches.append(patch.object(
        dawn_process.stacker, "build_night_color_meteor_stack",
        side_effect=lambda *a, **k: calls.append("meteor_stack")))
    patches.append(patch.object(dawn_process, "_attach_module_logs"))
    # No RMS process runs in a test; a test that needs one passes _rms_runs.
    patches.append(patch.object(dawn_process, "_rms_runs",
                                side_effect=overrides.get("_rms_runs", lambda *a, **k: {})))
    # stagger_hold_s reads the real clock; keep the slot retry out of these runs.
    patches.append(patch.object(dawn_process.archive_upload, "stagger_hold_s",
                                side_effect=overrides.get("stagger_hold_s", lambda *a, **k: 0.0)))
    patches.append(patch.object(sys, "argv", ["dawn_process.py", "-c", str(cfg_path), *extra]))
    for p in patches:
        p.start()
    try:
        return dawn_process._main_locked()
    finally:
        for p in reversed(patches):
            p.stop()


# ---------------------------------------------------------------------------
# The updater keys the VAAPI ground rule on this exact line
# ---------------------------------------------------------------------------

class TestPolicyMarker:
    def test_policy_line_is_at_column_zero(self):
        """updater.sh greps ``^FULL_NIGHT_DEADLINE_POLICY\\b``; an import or an
        indented assignment would silently skip the VAAPI step fleet-wide."""
        text = SCRIPT.read_text()
        assert re.search(r"^FULL_NIGHT_DEADLINE_POLICY\b", text, re.MULTILINE)

    def test_policy_marker_matches_updater(self):
        updater = (SCRIPT.parent / "updater.sh").read_text()
        assert 'VAAPI_POLICY_MARKER="FULL_NIGHT_DEADLINE_POLICY"' in updater


# ---------------------------------------------------------------------------
# _main_locked end to end
# ---------------------------------------------------------------------------

class TestMainOrchestration:
    def test_start_gate_skips_after_the_deadline(self, tmp_path, cfg, frozen):
        frozen.frozen = _real_datetime(2026, 3, 23, 13, 20, tzinfo=timezone.utc)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert calls == []

    def test_waits_for_rms_without_running_any_night_phase(self, tmp_path, cfg, frozen):
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        night_phases = set(calls) - {"_retry_deferred_timelapses"}
        assert night_phases == set()
        assert not flags_manager.is_morning_done(STATION_A, NIGHT, cfg)

    def test_morning_done_night_is_not_reprocessed(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        for sid in cfg["stations"]:
            flags_manager.mark_morning_done(sid, NIGHT, cfg)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" not in calls

    def test_detection_path_before_bulk_path(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        det = calls.index("_run_encode_locked_all")
        assert calls.index("_reconcile_ready") < det
        # Every bulk phase comes after the detection upload.
        first_upload = calls.index("_run_upload_all")
        for bulk in ("_run_stack_all", "_verify_and_restack", "_run_timelapse_all",
                     "_run_reencode_all"):
            assert calls.index(bulk) > first_upload
        assert calls.index("_run_stack_all") < calls.index("_verify_and_restack")
        assert calls.index("_run_timelapse_all") < calls.index("_run_reencode_all")
        assert calls[-1] == "_sweep_stale_locks"

    def test_deadline_after_detection_defers_bulk_and_marks_done(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        with patch.object(dawn_process.BulkDeadline, "passed", return_value=True):
            assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        for bulk in ("_run_stack_all", "_run_timelapse_all", "_run_reencode_all"):
            assert bulk not in calls
        assert "_run_encode_locked_all" in calls
        for sid in cfg["stations"]:
            assert flags_manager.is_morning_done(sid, NIGHT, cfg)

    def test_decommissioned_camera_is_not_waited_on(self, tmp_path, cfg, frozen):
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        cfg["stations"][STATION_B]["decommissioned"] = True
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" in calls


# ---------------------------------------------------------------------------
# RMS gate
# ---------------------------------------------------------------------------

class TestRmsDone:
    def test_single_processed_session(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT)
        assert dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_no_session(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        assert not dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_unfiltered_and_backup_do_not_count(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        s = _session(rms, STATION_A, NIGHT, ftpdet=False)
        (s / f"FTPdetectinfo_{STATION_A}_{NIGHT}_210000_000000_unfiltered.txt").write_text("x")
        (s / f"FTPdetectinfo_{STATION_A}_{NIGHT}_210000_000000_backup.txt").write_text("x")
        assert not dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_other_camera_does_not_count(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_B, NIGHT)
        assert not dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_post_midnight_session_belongs_to_the_previous_night(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, "005904")
        assert dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_captured_files_copy_counts(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, sub="CapturedFiles")
        assert dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_split_night_waits_for_the_second_session(self, cfg):
        """After a mid-night RMS restart the first session is detected at once
        and the second is still in detection at dawn; the gate must wait."""
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, "190000")
        _session(rms, STATION_A, NIGHT, "220000", sub="CapturedFiles", ftpdet=False)
        night = dawn_process._rms_night(STATION_A, NIGHT, str(rms))
        assert night.unprocessed == [f"{STATION_A}_{NIGHT}_220000_000000"]
        assert not dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_split_night_done_once_both_sessions_are_detected(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, "190000")
        _session(rms, STATION_A, NIGHT, "220000", sub="CapturedFiles")
        night = dawn_process._rms_night(STATION_A, NIGHT, str(rms))
        assert night.done
        assert len(night.ftpdetectinfo) == 2

    def test_archived_copy_completes_a_captured_session(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, sub="CapturedFiles", ftpdet=False)
        _session(rms, STATION_A, NIGHT, sub="ArchivedFiles", ff=False)
        assert dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_session_without_frames_does_not_hold_the_night(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT, "190000")
        _session(rms, STATION_A, NIGHT, "230000", sub="CapturedFiles", ftpdet=False, ff=False)
        assert dawn_process._rms_done(STATION_A, NIGHT, str(rms))

    def test_unreadable_rms_tree_is_not_done_and_does_not_raise(self, cfg, monkeypatch):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT)
        real_iterdir = Path.iterdir

        def flaky(self):
            if self.name in ("ArchivedFiles", "CapturedFiles"):
                raise OSError("EIO")
            return real_iterdir(self)

        monkeypatch.setattr(Path, "iterdir", flaky)
        assert not dawn_process._rms_done(STATION_A, NIGHT, str(rms))


class TestRmsGateTimeout:
    def test_waits_before_the_timeout(self, tmp_path, cfg, frozen):
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" not in calls

    def test_proceeds_past_the_timeout_without_eon_for_the_stuck_camera(
            self, tmp_path, cfg, frozen):
        frozen.frozen = _real_datetime(2026, 3, 23, 10, 5, tzinfo=timezone.utc)
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        seen: dict = {}

        def encode_locked(stations, date_str, cfg_, skip_eon=frozenset()):
            seen["skip"] = set(skip_eon)

        assert _run_main(_write_cfg(tmp_path, cfg),
                         overrides={"_run_encode_locked_all": encode_locked}) == 0
        assert seen["skip"] == {STATION_B}

    def test_timeout_hour_is_configurable(self, tmp_path, cfg, frozen):
        frozen.frozen = _real_datetime(2026, 3, 23, 10, 5, tzinfo=timezone.utc)
        cfg["dawn_rms_wait_until_hour"] = 11
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" not in calls


BUSY = dawn_process.RmsRun(idle=False, writes_summary=True)


def _status(session: Path, archiving: str) -> None:
    (session / dawn_process.RMS_STATUS_FILE).write_text(json.dumps({"archiving": archiving}))


class TestRmsPostProcessing:
    """RMS writes the filtered FTPdetectinfo at the start of post-processing;
    gmnro09 (Sibiciu) and gmnro22 (Ferigile) dawn ran beside RMS's archiving
    on 2026-10-25/26. A detected session counts as finished only once RMS says so."""

    def _captured(self, cfg) -> Path:
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        return _session(rms, STATION_A, NIGHT, sub="CapturedFiles")

    def _night(self, cfg, run):
        return dawn_process._rms_night(
            STATION_A, NIGHT, cfg["stations"][STATION_A]["rms_data_path"], run)

    def test_status_started_waits(self, cfg):
        _status(self._captured(cfg), "started")
        night = self._night(cfg, BUSY)
        assert night.detected and not night.done
        assert night.postprocessing == [f"{STATION_A}_{NIGHT}_210000_000000"]

    def test_status_completed_proceeds(self, cfg):
        _status(self._captured(cfg), "completed")
        assert self._night(cfg, BUSY).done

    def test_completed_in_either_copy_wins(self, cfg):
        _status(self._captured(cfg), "completed")
        archived = _session(Path(cfg["stations"][STATION_A]["rms_data_path"]),
                            STATION_A, NIGHT, sub="ArchivedFiles")
        _status(archived, "started")
        assert self._night(cfg, BUSY).done

    def test_no_status_waits_for_the_final_summary(self, cfg):
        s = self._captured(cfg)
        (s / f"{s.name}_observation_summary_working.json").write_text("{}")
        assert not self._night(cfg, BUSY).done
        (s / f"{s.name}_observation_summary.json").write_text("{}")
        assert self._night(cfg, BUSY).done

    def test_no_status_and_an_rms_without_summaries_keeps_the_old_rule(self, cfg):
        self._captured(cfg)
        assert self._night(cfg, dawn_process.RmsRun(idle=False, writes_summary=False)).done

    def test_no_rms_process_proceeds_on_the_ftpdetectinfo(self, cfg):
        _status(self._captured(cfg), "started")
        assert self._night(cfg, None).done

    def test_rms_idle_until_its_next_night_proceeds(self, cfg):
        """A 'started' record RMS will never finish (crashed mid-archive,
        restarted without auto_reprocess) must not hold dawn until 10:00."""
        _status(self._captured(cfg), "started")
        assert self._night(cfg, dawn_process.RmsRun(idle=True, writes_summary=True)).done

    def test_started_without_detections_is_still_detection(self, cfg):
        s = _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT,
                     sub="CapturedFiles", ftpdet=False)
        _status(s, "started")
        reason = dawn_process._rms_wait_reason(STATION_A, cfg["stations"][STATION_A], NIGHT,
                                               {STATION_A: BUSY})
        assert reason == "detection"

    def test_gate_waits_and_says_why(self, tmp_path, cfg, frozen, caplog):
        _status(self._captured(cfg), "started")
        _session(Path(cfg["stations"][STATION_B]["rms_data_path"]), STATION_B, NIGHT)
        calls: list = []
        with caplog.at_level("INFO", logger="dawn_process"):
            assert _run_main(_write_cfg(tmp_path, cfg), record=calls,
                             overrides={"_rms_runs": lambda *a, **k: {STATION_A: BUSY}}) == 0
        assert "_run_encode_locked_all" not in calls
        assert f"Waiting for RMS to finish on: {STATION_A} (post-processing)" in caplog.text

    def test_gate_proceeds_once_completed(self, tmp_path, cfg, frozen):
        _status(self._captured(cfg), "completed")
        _session(Path(cfg["stations"][STATION_B]["rms_data_path"]), STATION_B, NIGHT)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls,
                         overrides={"_rms_runs": lambda *a, **k: {STATION_A: BUSY}}) == 0
        assert "_run_encode_locked_all" in calls

    def test_past_the_timeout_a_postprocessing_camera_keeps_its_eon_pass(
            self, tmp_path, cfg, frozen):
        frozen.frozen = _real_datetime(2026, 3, 23, 10, 5, tzinfo=timezone.utc)
        _status(self._captured(cfg), "started")
        seen: dict = {}

        def encode_locked(stations, date_str, cfg_, skip_eon=frozenset()):
            seen["skip"] = set(skip_eon)

        assert _run_main(_write_cfg(tmp_path, cfg),
                         overrides={"_run_encode_locked_all": encode_locked,
                                    "_rms_runs": lambda *a, **k: {STATION_A: BUSY}}) == 0
        assert seen["skip"] == {STATION_B}


class TestRmsRuns:
    def _rms(self, tmp_path, *, log_line: str, summary_module: bool) -> Path:
        proc = tmp_path / "proc"
        checkout = tmp_path / "source" / "RMS"
        data = tmp_path / "RMS_data"
        (data / "logs").mkdir(parents=True)
        checkout.mkdir(parents=True)
        if summary_module:
            (checkout / "RMS" / "Formats").mkdir(parents=True)
            (checkout / "RMS" / "Formats" / "ObservationSummary.py").write_text("")
        (checkout / ".config").write_text(
            f"[System]\nstationID: {STATION_A}\n[Capture]\ndata_dir: {data}\nlog_dir: logs\n")
        (data / "logs" / f"log_{STATION_A}_20260322_120000.log").write_text(log_line + "\n")
        d = proc / "4242"
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"python\0-m\0RMS.StartCapture\0")
        (d / "cwd").symlink_to(checkout)
        return proc

    def test_busy_rms_with_summaries(self, tmp_path):
        proc = self._rms(tmp_path, summary_module=True,
                         log_line="2026/03/23 04:01:00-INFO-Reprocess-line:255 - Recalibrating astrometry")
        assert dawn_process._rms_runs(proc) == {
            STATION_A: dawn_process.RmsRun(idle=False, writes_summary=True)}

    def test_idle_rms_without_summaries(self, tmp_path):
        proc = self._rms(tmp_path, summary_module=False,
                         log_line="2026/03/23 07:10:00-INFO-StartCapture-line:1397 - "
                                  "Waiting 9:41:12 to start recording for 11.493 hrs")
        assert dawn_process._rms_runs(proc) == {
            STATION_A: dawn_process.RmsRun(idle=True, capture_state="waiting", writes_summary=False)}

    def test_rms_after_capture_ended(self, tmp_path):
        """The watchdog's reboot deferral reads capture_state 'ended' as detection running."""
        proc = self._rms(tmp_path, summary_module=True,
                         log_line="2026/03/23 03:58:00-INFO-StartCapture-line:900 - Ending capture...")
        assert dawn_process._rms_runs(proc) == {
            STATION_A: dawn_process.RmsRun(idle=False, capture_state="ended", writes_summary=True)}

    def test_no_proc_means_no_rms(self, tmp_path):
        assert dawn_process._rms_runs(tmp_path / "absent") == {}


class TestColourGate:
    """colour_capture SIGTERMed dawn on every closed chunk, and RMS cameras
    record a 30-min idle tail after RMS ends its night, so the 04:00 and 04:15
    runs died within ~15 s every morning. Dawn now waits for the recording."""

    def _chunk(self, cfg, sid: str, age_s: float) -> Path:
        night_dir = Path(cfg["videocapture_path"]) / sid / NIGHT
        night_dir.mkdir(parents=True, exist_ok=True)
        chunk = night_dir / f"{sid}_20260323_045000_color.mkv"
        chunk.write_bytes(b"x")
        t = time.time() - age_s
        os.utime(chunk, (t, t))
        return chunk

    def test_fresh_chunk_waits(self, tmp_path, cfg, frozen, caplog):
        _both_rms_done(cfg)
        self._chunk(cfg, STATION_B, 10)
        calls: list = []
        with caplog.at_level("INFO", logger="dawn_process"):
            assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" not in calls
        assert f"Waiting for colour capture to finish on: {STATION_B}" in caplog.text
        assert not flags_manager.is_morning_done(STATION_B, NIGHT, cfg)

    def test_settled_chunks_proceed(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        self._chunk(cfg, STATION_B, 600)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" in calls

    def test_camera_without_colour_capture_is_not_waited_on(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        self._chunk(cfg, STATION_B, 10)
        cfg["stations"][STATION_B]["color_capture"] = False
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" in calls

    def test_gives_up_with_the_rms_wait(self, tmp_path, cfg, frozen):
        frozen.frozen = _real_datetime(2026, 3, 23, 10, 5, tzinfo=timezone.utc)
        _both_rms_done(cfg)
        self._chunk(cfg, STATION_B, 10)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "_run_encode_locked_all" in calls


class TestEonPass:
    def test_skipped_camera_keeps_its_locks_and_still_encodes(self, cfg):
        for sid in (STATION_A, STATION_B):
            name = _chunk(cfg, sid, "210000")
            flags_manager.lock_chunk(sid, NIGHT, name, {"lock_type": "detection"}, cfg)
        with patch.object(dawn_process.detection_lock, "process_night") as dl, \
             patch.object(dawn_process.encoder, "process_chunk", return_value=True) as enc:
            dawn_process._run_encode_locked_all(cfg["stations"], NIGHT, cfg,
                                                skip_eon=frozenset({STATION_B}))
        assert [c.args[0] for c in dl.call_args_list] == [STATION_A]
        assert {c.args[0].name.split("_")[0] for c in enc.call_args_list} == {STATION_A, STATION_B}
        assert not flags_manager.load(STATION_B, NIGHT, cfg).get("rms_complete")
        assert flags_manager.load(STATION_B, NIGHT, cfg)["chunks"][
            f"{STATION_B}_{NIGHT}_210000_color.mkv"]["lock"] is not None

    def test_eon_pass_records_its_ftpdetectinfo_sources(self, cfg):
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        _session(rms, STATION_A, NIGHT)
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True):
            assert dawn_process._run_eon_pass(STATION_A, NIGHT, cfg)
        assert dawn_process._eon_sources(STATION_A, NIGHT, cfg) == [
            f"FTPdetectinfo_{STATION_A}_{NIGHT}_210000_000000.txt"]
        assert flags_manager.is_eon_done(STATION_A, NIGHT, cfg)

    def test_skipped_eon_pass_records_nothing(self, cfg):
        with patch.object(dawn_process.detection_lock, "process_night", return_value=False):
            assert not dawn_process._run_eon_pass(STATION_A, NIGHT, cfg)
        assert dawn_process._eon_sources(STATION_A, NIGHT, cfg) is None


class TestHardStopAndSigterm:
    def test_blocked_bookkeeping_does_not_block_the_kill(self):
        """A worker stuck on a hung disk can hold the state lock the bookkeeping
        needs; the kill must happen anyway."""
        import threading
        release = threading.Event()
        calls: list = []
        d = dawn_process.BulkDeadline(enabled=True, hour=11, bookkeeping_s=0.1,
                                      on_hard_stop=lambda: release.wait(5))
        try:
            with patch.object(dawn_process, "_terminate_children",
                              side_effect=lambda: calls.append("killed")), \
                 patch.object(dawn_process.os, "_exit",
                              side_effect=lambda code: calls.append(f"exit{code}")):
                d._hard_stop()
        finally:
            release.set()
        assert calls == ["killed", "exit3"]

    def test_sigterm_in_the_morning_kills_children_and_marks_nothing(self, frozen, monkeypatch):
        marked: list = []
        calls: list = []
        d = dawn_process.BulkDeadline(enabled=True, on_hard_stop=lambda: marked.append(1))
        monkeypatch.setattr(dawn_process, "_active_deadline", d)
        with patch.object(dawn_process, "_terminate_children",
                          side_effect=lambda: calls.append("killed")), \
             patch.object(dawn_process.os, "_exit",
                          side_effect=lambda code: calls.append(f"exit{code}")):
            dawn_process._on_sigterm(15, None)
        assert calls == ["killed", "exit143"]
        assert marked == []

    def test_sigterm_past_the_deadline_does_the_bookkeeping(self, frozen, monkeypatch):
        frozen.frozen = _real_datetime(2026, 3, 23, 13, 18, tzinfo=timezone.utc)
        calls: list = []
        d = dawn_process.BulkDeadline(enabled=True, on_hard_stop=lambda: calls.append("marked"))
        monkeypatch.setattr(dawn_process, "_active_deadline", d)
        with patch.object(dawn_process, "_terminate_children",
                          side_effect=lambda: calls.append("killed")), \
             patch.object(dawn_process.os, "_exit",
                          side_effect=lambda code: calls.append(f"exit{code}")):
            dawn_process._on_sigterm(15, None)
        assert calls == ["marked", "killed", "exit3"]

    def test_sigterm_before_the_run_has_a_deadline(self, monkeypatch):
        calls: list = []
        monkeypatch.setattr(dawn_process, "_active_deadline", None)
        with patch.object(dawn_process, "_terminate_children",
                          side_effect=lambda: calls.append("killed")), \
             patch.object(dawn_process.os, "_exit",
                          side_effect=lambda code: calls.append(f"exit{code}")):
            dawn_process._on_sigterm(15, None)
        assert calls == ["killed", "exit143"]

    def test_main_installs_the_handler(self, tmp_path, monkeypatch):
        monkeypatch.setattr(dawn_process, "_LOCK_FILE", tmp_path / "dawn.lock")
        with patch.object(dawn_process.signal, "signal") as sig, \
             patch.object(dawn_process, "_main_locked", return_value=0):
            assert dawn_process.main() == 0
        sig.assert_called_once_with(dawn_process.signal.SIGTERM, dawn_process._on_sigterm)

    def test_run_clears_the_active_deadline(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        _run_main(_write_cfg(tmp_path, cfg))
        assert dawn_process._active_deadline is None


class TestTimelapseRetries:
    def test_not_retried_while_rms_is_still_detecting(self, tmp_path, cfg, frozen):
        _session(Path(cfg["stations"][STATION_A]["rms_data_path"]), STATION_A, NIGHT)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), record=calls)
        assert calls == []

    def test_retried_once_the_night_is_done(self, tmp_path, cfg, frozen):
        for sid in cfg["stations"]:
            flags_manager.mark_morning_done(sid, NIGHT, cfg)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), record=calls)
        assert calls == ["_retry_deferred_timelapses"]

    def test_retried_after_tonights_timelapse(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), record=calls)
        assert calls.index("_run_timelapse_all") < calls.index("_retry_deferred_timelapses") \
            < calls.index("_run_reencode_all")

    def test_a_failing_build_is_given_up_after_the_cap(self, cfg, frozen):
        _chunk(cfg, STATION_A, "210000", stack=True)
        flags_manager.mark_timelapse_deferred(STATION_A, NIGHT, cfg, reason="t")
        with patch.object(dawn_process.timelapse_build, "build",
                          side_effect=RuntimeError("ffmpeg")) as build:
            for _ in range(dawn_process._TIMELAPSE_RETRY_MAX + 2):
                dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        assert build.call_count == dawn_process._TIMELAPSE_RETRY_MAX

    def test_a_successful_build_is_not_counted(self, cfg, frozen):
        _chunk(cfg, STATION_A, "210000", stack=True)
        flags_manager.mark_timelapse_deferred(STATION_A, NIGHT, cfg, reason="t")
        with patch.object(dawn_process.timelapse_build, "build",
                          side_effect=lambda s, d, c: flags_manager.mark_timelapse_done(s, d, c)):
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        assert dawn_process._timelapse_attempts(STATION_A, NIGHT, cfg) == 0


class TestForceRerun:
    def test_date_alone_does_not_rerun_a_done_night(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        for sid in cfg["stations"]:
            flags_manager.mark_morning_done(sid, NIGHT, cfg)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), "--date", NIGHT, record=calls)
        assert "_run_encode_locked_all" not in calls

    def test_force_reruns_a_done_night(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        for sid in cfg["stations"]:
            flags_manager.mark_morning_done(sid, NIGHT, cfg)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), "--date", NIGHT, "--force", record=calls)
        assert "_run_encode_locked_all" in calls
        assert "_run_stack_all" in calls

    def test_force_does_not_wait_for_rms(self, tmp_path, cfg, frozen):
        seen: dict = {}

        def encode_locked(stations, date_str, cfg_, skip_eon=frozenset()):
            seen["skip"] = set(skip_eon)

        _run_main(_write_cfg(tmp_path, cfg), "--date", NIGHT, "--force",
                  overrides={"_run_encode_locked_all": encode_locked})
        assert seen["skip"] == {STATION_A, STATION_B}

    def test_force_needs_a_date(self, tmp_path, cfg, frozen):
        with pytest.raises(SystemExit):
            _run_main(_write_cfg(tmp_path, cfg), "--force")


class TestStaleLockSweepWindow:
    def test_nights_far_below_the_cutoff_are_not_reread(self, cfg, frozen):
        ancient = "20250101"
        name = _chunk(cfg, STATION_A, "210000", night=ancient)
        flags_manager.lock_chunk(STATION_A, ancient, name, {"lock_type": "detection"}, cfg)
        with patch.object(flags_manager, "peek", wraps=flags_manager.peek) as peek:
            dawn_process._sweep_stale_locks(cfg)
        assert all(c.args[1] != ancient for c in peek.call_args_list)


class TestStaleLockSweepTakesTheFlagsLock:
    def test_locked_night_is_rewritten_under_the_lock(self, cfg, frozen):
        old = "20260301"
        name = _chunk(cfg, STATION_A, "210000", night=old)
        flags_manager.lock_chunk(STATION_A, old, name, {"lock_type": "detection"}, cfg)
        _chunk(cfg, STATION_A, "210000", night="20260302")      # old, no locks
        real = flags_manager._lock
        taken: list = []

        def spy(sid, dstr, c):
            taken.append(dstr)
            return real(sid, dstr, c)

        with patch.object(flags_manager, "_lock", side_effect=spy):
            dawn_process._sweep_stale_locks(cfg)
        assert "20260301" in taken
        assert "20260302" not in taken
        assert flags_manager.load(STATION_A, old, cfg)["chunks"][name]["lock"] is None


class TestStackPool:
    def test_a_slow_camera_does_not_hold_back_the_next(self, cfg):
        """Two workers, three cameras: the third starts as soon as the fast one
        finishes, not after the slow one (fixed batches waited for it)."""
        import threading
        cfg["dawn_stack_parallelism"] = 2
        stations = {"CAM1": {}, "CAM2": {}, "CAM3": {}}
        slow_release = threading.Event()
        third_started = threading.Event()

        def fake(sid, date_str, cfg_, abort, **kw):
            if sid == "CAM1":
                assert third_started.wait(5), "CAM3 waited for the slow camera"
                slow_release.set()
            elif sid == "CAM3":
                third_started.set()

        with patch.object(dawn_process.stacker, "process_night", side_effect=fake):
            dawn_process._run_stack_all(stations, NIGHT, cfg)
        assert slow_release.is_set()

    def test_abort_mid_pass_skips_cameras_not_yet_started(self, cfg):
        cfg["dawn_stack_parallelism"] = 1
        stations = {"CAM1": {}, "CAM2": {}, "CAM3": {}}
        started: list = []
        flag = {"abort": False}

        def fake(sid, date_str, cfg_, abort, **kw):
            started.append(sid)
            flag["abort"] = True

        with patch.object(dawn_process.stacker, "process_night", side_effect=fake):
            dawn_process._run_stack_all(stations, NIGHT, cfg,
                                        should_abort=lambda: flag["abort"])
        assert len(started) == 1


class TestMeteorStackAfterChunkStacks:
    def test_built_after_phase_4_and_before_the_bulk_upload(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        first_upload = calls.index("_run_upload_all")
        bulk_upload = len(calls) - 1 - calls[::-1].index("_run_upload_all")
        meteor = calls.index("meteor_stack")
        assert first_upload < calls.index("_verify_and_restack") < meteor < bulk_upload

    def test_still_built_when_the_deadline_skips_the_bulk_path(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        with patch.object(dawn_process.BulkDeadline, "passed", return_value=True):
            assert _run_main(_write_cfg(tmp_path, cfg), record=calls) == 0
        assert "meteor_stack" in calls
        assert "_run_stack_all" not in calls


class TestStackBeforeEncode:
    def _locked(self, cfg, hhmmss: str, **kw) -> str:
        name = _chunk(cfg, STATION_A, hhmmss, **kw)
        flags_manager.lock_chunk(STATION_A, NIGHT, name, {"lock_type": "detection"}, cfg)
        return name

    def test_only_raw_unstacked_locked_chunks_are_stacked_first(self, cfg):
        locked = self._locked(cfg, "210000")
        _chunk(cfg, STATION_A, "210020")                       # unlocked
        stacked = self._locked(cfg, "210040")
        flags_manager.mark_stacked(STATION_A, NIGHT, stacked, cfg)
        encoded = self._locked(cfg, "210100")
        flags_manager.mark_reencoded(STATION_A, NIGHT, encoded, cfg)
        stitched = f"{STATION_A}_{NIGHT}_210120_210140_color.mkv"
        (_night_dir(cfg, STATION_A) / stitched).write_bytes(b"x")
        flags_manager.lock_chunk(STATION_A, NIGHT, stitched, {"lock_type": "detection"}, cfg)
        jobs = dawn_process._locked_chunk_jobs({STATION_A: {}}, NIGHT, cfg)
        assert {mkv.name: raw for _, mkv, raw in jobs} == {
            locked: True, stacked: False, encoded: False, stitched: False}

    def test_cameras_are_interleaved(self, cfg):
        for t in ("210000", "210020", "210040"):
            self._locked(cfg, t)
        name = _chunk(cfg, STATION_B, "210000")
        flags_manager.lock_chunk(STATION_B, NIGHT, name, {"lock_type": "detection"}, cfg)
        jobs = dawn_process._locked_chunk_jobs({STATION_A: {}, STATION_B: {}}, NIGHT, cfg)
        assert [sid for sid, _, _ in jobs][:2] == [STATION_A, STATION_B]

    def test_phase_1_stacks_each_chunk_before_encoding_it(self, cfg):
        self._locked(cfg, "210000")
        order: list = []
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk",
                          side_effect=lambda *a, **k: order.append("stack") or True), \
             patch.object(dawn_process.encoder, "process_chunk",
                          side_effect=lambda *a, **k: order.append("encode") or True):
            dawn_process._run_encode_locked_all({STATION_A: cfg["stations"][STATION_A]},
                                                NIGHT, cfg)
        assert order == ["stack", "encode"]

    def test_stacker_disabled_encodes_without_stacking(self, cfg):
        cfg["services"]["stacker"] = {"enabled": False}
        self._locked(cfg, "210000")
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk") as pc, \
             patch.object(dawn_process.encoder, "process_chunk", return_value=True) as enc:
            dawn_process._run_encode_locked_all({STATION_A: cfg["stations"][STATION_A]},
                                                NIGHT, cfg)
        pc.assert_not_called()
        enc.assert_called_once()

    def test_meteor_clips_off_stacks_but_does_not_encode(self, cfg):
        cfg["services"]["reencode"]["meteor_clips"] = False
        self._locked(cfg, "210000")
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk", return_value=True) as pc, \
             patch.object(dawn_process.encoder, "process_chunk") as enc:
            dawn_process._run_encode_locked_all({STATION_A: cfg["stations"][STATION_A]},
                                                NIGHT, cfg)
        pc.assert_called_once()
        enc.assert_not_called()

    def test_a_stack_failure_does_not_block_the_encode(self, cfg):
        self._locked(cfg, "210000")
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk", side_effect=RuntimeError), \
             patch.object(dawn_process.encoder, "process_chunk", return_value=True) as enc:
            dawn_process._run_encode_locked_all({STATION_A: cfg["stations"][STATION_A]},
                                                NIGHT, cfg)
        enc.assert_called_once()

    def test_one_pool_serves_every_camera(self, cfg):
        """A peak night on one camera no longer runs on a single worker."""
        import threading
        cfg["dawn_encode_parallelism"] = 2
        cfg["_cpu_cores"] = 4
        for t in ("210000", "210020"):
            self._locked(cfg, t)
        both = threading.Barrier(2, timeout=5)
        with patch.object(dawn_process.detection_lock, "process_night", return_value=True), \
             patch.object(dawn_process.stacker, "process_chunk", return_value=True), \
             patch.object(dawn_process.encoder, "process_chunk",
                          side_effect=lambda *a, **k: both.wait() is not None):
            dawn_process._run_encode_locked_all({STATION_A: cfg["stations"][STATION_A]},
                                                NIGHT, cfg)
        assert not both.broken


class TestBackfillRerunsOnNewFtpdetectinfo:
    PREV = "20260321"

    def _prep(self, cfg, *hhmmss):
        _night_dir(cfg, STATION_A, self.PREV)
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        for t in hhmmss:
            _session(rms, STATION_A, self.PREV, t)
        return rms

    def _backfill(self, cfg):
        with patch.object(dawn_process.detection_lock, "process_night",
                          return_value=True) as dl, \
             patch.object(dawn_process.encoder, "process_night_locked_only"), \
             patch.object(dawn_process.archive_upload, "run_night"):
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, NIGHT, cfg)
        return dl

    def test_second_session_detected_later_triggers_a_rerun(self, cfg):
        self._prep(cfg, "190000")
        self._backfill(cfg)                     # first reconciliation
        assert flags_manager.is_eon_done(STATION_A, self.PREV, cfg)
        self._prep(cfg, "220000")               # RMS finishes the second session
        dl = self._backfill(cfg)
        dl.assert_called_once_with(STATION_A, self.PREV, cfg)
        assert len(dawn_process._eon_sources(STATION_A, self.PREV, cfg)) == 2

    def test_unchanged_sources_do_not_rerun(self, cfg):
        self._prep(cfg, "190000")
        self._backfill(cfg)
        dl = self._backfill(cfg)
        dl.assert_not_called()

    def test_unrecorded_night_adopts_a_baseline_without_rerunning(self, cfg):
        self._prep(cfg, "190000")
        flags_manager.mark_eon_done(STATION_A, self.PREV, cfg)
        dl = self._backfill(cfg)
        dl.assert_not_called()
        assert dawn_process._eon_sources(STATION_A, self.PREV, cfg) is not None


# ---------------------------------------------------------------------------
# Helper phases
# ---------------------------------------------------------------------------

def _night_dir(cfg: dict, sid: str, night: str = NIGHT) -> Path:
    d = Path(cfg["videocapture_path"]) / sid / night
    d.mkdir(parents=True, exist_ok=True)
    return d


def _chunk(cfg: dict, sid: str, hhmmss: str, night: str = NIGHT, *, ready: bool = True,
           stack: bool = False) -> str:
    name = f"{sid}_{night}_{hhmmss}_color.mkv"
    nd = _night_dir(cfg, sid, night)
    (nd / name).write_bytes(b"x")
    if ready:
        flags_manager.mark_ready(sid, night, name, cfg)
    if stack:
        (nd / "stacks").mkdir(exist_ok=True)
        (nd / "stacks" / name.replace("_color.mkv", "_stack.webp")).write_bytes(b"x")
    return name


class TestReconcileReady:
    def test_marks_unflagged_chunks_ready(self, cfg):
        name = _chunk(cfg, STATION_A, "210000", ready=False)
        dawn_process._reconcile_ready({STATION_A: {}}, NIGHT, cfg)
        assert flags_manager.load(STATION_A, NIGHT, cfg)["chunks"][name]["ready"]

    def test_ignores_stitched_and_foreign_names(self, cfg):
        nd = _night_dir(cfg, STATION_A)
        stitched = f"{STATION_A}_{NIGHT}_210000_210020_color.mkv"
        (nd / stitched).write_bytes(b"x")
        dawn_process._reconcile_ready({STATION_A: {}}, NIGHT, cfg)
        assert stitched not in flags_manager.load(STATION_A, NIGHT, cfg)["chunks"]

    def test_missing_night_dir_is_a_no_op(self, cfg):
        with patch.object(flags_manager, "mark_ready_batch") as batch:
            dawn_process._reconcile_ready({STATION_A: {}}, NIGHT, cfg)
        batch.assert_not_called()


class TestVerifyAndRestack:
    def test_restacks_a_camera_with_gaps(self, cfg):
        _chunk(cfg, STATION_A, "210000")
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._verify_and_restack({STATION_A: {}}, NIGHT, cfg)
        stack.assert_called_once()

    def test_complete_camera_is_left_alone(self, cfg):
        _chunk(cfg, STATION_A, "210000", stack=True)
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._verify_and_restack({STATION_A: {}}, NIGHT, cfg)
        stack.assert_not_called()

    def test_abort_skips_the_pass(self, cfg):
        _chunk(cfg, STATION_A, "210000")
        with patch.object(dawn_process.stacker, "process_night") as stack:
            dawn_process._verify_and_restack({STATION_A: {}}, NIGHT, cfg,
                                             should_abort=lambda: True)
        stack.assert_not_called()


class TestRetryDeferredTimelapses:
    def test_deferred_night_with_recovered_coverage_is_built(self, cfg, frozen):
        _chunk(cfg, STATION_A, "210000", stack=True)
        flags_manager.mark_timelapse_deferred(STATION_A, NIGHT, cfg, reason="t")
        with patch.object(dawn_process.timelapse_build, "build") as build:
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        build.assert_called_once_with(STATION_A, NIGHT, cfg)

    def test_low_coverage_waits_on_the_first_day(self, cfg, frozen):
        _chunk(cfg, STATION_A, "210000")
        flags_manager.mark_timelapse_deferred(STATION_A, NIGHT, cfg, reason="t")
        with patch.object(dawn_process.timelapse_build, "build") as build:
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        build.assert_not_called()

    def test_gives_up_waiting_after_two_days(self, cfg, frozen):
        older = "20260320"
        _chunk(cfg, STATION_A, "210000", night=older)
        flags_manager.mark_timelapse_deferred(STATION_A, older, cfg, reason="t")
        with patch.object(dawn_process.timelapse_build, "build") as build:
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        build.assert_called_once_with(STATION_A, older, cfg)

    def test_done_timelapse_is_skipped(self, cfg, frozen):
        flags_manager.mark_timelapse_deferred(STATION_A, NIGHT, cfg, reason="t")
        flags_manager.mark_timelapse_done(STATION_A, NIGHT, cfg)
        with patch.object(dawn_process.timelapse_build, "build") as build:
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        build.assert_not_called()

    def test_morning_done_night_without_timelapse_is_owed_one(self, cfg, frozen):
        _chunk(cfg, STATION_A, "210000", stack=True)
        flags_manager.mark_morning_done(STATION_A, NIGHT, cfg)
        with patch.object(dawn_process.timelapse_build, "build") as build:
            dawn_process._retry_deferred_timelapses({STATION_A: {}}, cfg)
        build.assert_called_once_with(STATION_A, NIGHT, cfg)


class TestSweepStaleLocks:
    def test_clears_locks_older_than_the_cutoff(self, cfg, frozen):
        old = "20260301"
        name = _chunk(cfg, STATION_A, "210000", night=old)
        flags_manager.lock_chunk(STATION_A, old, name, {"lock_type": "detection"}, cfg)
        dawn_process._sweep_stale_locks(cfg)
        assert flags_manager.load(STATION_A, old, cfg)["chunks"][name]["lock"] is None

    def test_recent_locks_are_kept(self, cfg, frozen):
        name = _chunk(cfg, STATION_A, "210000")
        flags_manager.lock_chunk(STATION_A, NIGHT, name, {"lock_type": "detection"}, cfg)
        dawn_process._sweep_stale_locks(cfg)
        assert flags_manager.load(STATION_A, NIGHT, cfg)["chunks"][name]["lock"] is not None

    def test_missing_capture_root_is_a_no_op(self, tmp_path, frozen):
        dawn_process._sweep_stale_locks({"videocapture_path": str(tmp_path / "nope")})


class TestBackfillUpload:
    @pytest.fixture
    def uploader(self, monkeypatch):
        """A fake _Uploader whose cycle records the stop event it was given."""
        seen: dict = {"cycles": []}

        class FakeUploader:
            enabled = True

            def __init__(self, cfg):
                seen["cfg"] = cfg

            def cycle(self, stop_event=None):
                seen["cycles"].append(stop_event)
                if seen.get("block"):
                    seen["block"](stop_event)
                return 3

        monkeypatch.setattr(dawn_process.archive_upload, "_Uploader", FakeUploader)
        monkeypatch.setattr(dawn_process.archive_upload, "stagger_hold_s", lambda *a, **k: 0.0)
        return seen

    def test_skipped_with_less_than_twenty_minutes_left(self, cfg, frozen, uploader):
        frozen.frozen = _real_datetime(2026, 3, 23, 13, 0, tzinfo=timezone.utc)
        dawn_process._run_backfill_upload(cfg)
        assert uploader["cycles"] == []

    def test_runs_a_full_scan_with_the_stop_event(self, cfg, frozen, uploader):
        import threading
        stop = threading.Event()
        dawn_process._run_backfill_upload(cfg, stop)
        assert uploader["cycles"] == [stop]

    def test_waits_for_the_dawn_stagger(self, cfg, frozen, uploader, monkeypatch):
        monkeypatch.setattr(dawn_process.archive_upload, "stagger_hold_s", lambda *a, **k: 90.0)
        dawn_process._run_backfill_upload(cfg)
        assert uploader["cycles"] == []

    def test_no_stations_no_pass(self, frozen, uploader):
        dawn_process._run_backfill_upload({"stations": {}})
        assert uploader["cycles"] == []

    def test_upload_failure_is_contained(self, cfg, frozen, uploader):
        def boom(stop):
            raise RuntimeError("boom")
        uploader["block"] = boom
        dawn_process._run_backfill_upload(cfg)

    def test_background_pass_is_stopped_at_the_deadline(self, cfg, frozen, uploader):
        """finish() waits until the deadline, then stops the pass at a file boundary."""
        import threading
        started = threading.Event()

        def block(stop):
            started.set()
            assert stop.wait(5), "the backfill was never told to stop"

        uploader["block"] = block
        deadline = dawn_process.BulkDeadline(enabled=True)
        bf = dawn_process._BackfillUpload(cfg, deadline)
        with patch.object(deadline, "seconds_left", return_value=0.0):
            bf.start()
            assert started.wait(5)
            bf.finish()
        assert bf.stop.is_set()
        assert not bf._thread.is_alive()

    def test_background_pass_that_finishes_is_simply_joined(self, cfg, frozen, uploader):
        deadline = dawn_process.BulkDeadline(enabled=True)
        bf = dawn_process._BackfillUpload(cfg, deadline)
        bf.start()
        bf.finish()
        assert len(uploader["cycles"]) == 1
        assert not bf.stop.is_set()

    def test_runs_alongside_the_reencode_and_is_joined_after_it(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        _run_main(_write_cfg(tmp_path, cfg), record=calls)
        assert "_run_backfill_upload" in calls
        assert calls.index("_run_upload_all") < calls.index("_run_backfill_upload")


class TestDetectionUploadAtTheStaggerSlot:
    def test_held_upload_is_retried_when_the_slot_opens(self, cfg, monkeypatch):
        import threading
        monkeypatch.setattr(dawn_process, "_SLOT_MARGIN_S", 0.0)
        monkeypatch.setattr(dawn_process.archive_upload, "stagger_hold_s",
                            lambda *a, **k: 0.05)
        uploads: list = []
        second = threading.Event()

        def fake_upload(stations, date_str, cfg_, should_abort=None):
            uploads.append(date_str)
            if len(uploads) == 2:
                second.set()

        monkeypatch.setattr(dawn_process, "_run_upload_all", fake_upload)
        slot = dawn_process._SlotUpload(cfg["stations"], NIGHT, cfg)
        dawn_process._run_detection_upload(cfg["stations"], NIGHT, cfg, slot,
                                           dawn_process.BulkDeadline(enabled=False))
        assert second.wait(5)
        assert uploads == [NIGHT, NIGHT]

    def test_open_slot_uploads_once(self, cfg, monkeypatch):
        monkeypatch.setattr(dawn_process.archive_upload, "stagger_hold_s", lambda *a, **k: 0.0)
        with patch.object(dawn_process, "_run_upload_all") as up:
            slot = dawn_process._SlotUpload(cfg["stations"], NIGHT, cfg)
            dawn_process._run_detection_upload(cfg["stations"], NIGHT, cfg, slot,
                                               dawn_process.BulkDeadline(enabled=False))
        assert up.call_count == 1
        assert slot._timer is None

    def test_slot_after_the_deadline_is_left_to_the_daemon(self, cfg, monkeypatch):
        monkeypatch.setattr(dawn_process.archive_upload, "stagger_hold_s",
                            lambda *a, **k: 3600.0)
        deadline = dawn_process.BulkDeadline(enabled=True)
        slot = dawn_process._SlotUpload(cfg["stations"], NIGHT, cfg)
        with patch.object(deadline, "seconds_left", return_value=600.0):
            slot.schedule(deadline)
        assert slot._timer is None

    def test_bulk_upload_waits_for_a_retry_under_way(self, cfg, monkeypatch):
        import threading
        order: list = []
        release = threading.Event()

        def fake_upload(stations, date_str, cfg_, should_abort=None):
            if should_abort is None:           # the retried detection upload
                order.append("retry-start")
                release.wait(5)
                order.append("retry-end")
            else:
                order.append("bulk")

        monkeypatch.setattr(dawn_process, "_run_upload_all", fake_upload)
        slot = dawn_process._SlotUpload(cfg["stations"], NIGHT, cfg)
        t = threading.Thread(target=slot._run)
        t.start()
        assert slot._started.wait(5)
        threading.Timer(0.1, release.set).start()
        dawn_process._run_bulk_upload(cfg["stations"], NIGHT, cfg,
                                      dawn_process.BulkDeadline(enabled=False), slot)
        t.join(5)
        assert order == ["retry-start", "retry-end", "bulk"]


class TestPhaseIsolation:
    """One failing phase must not cost the rest of the morning or the reboot."""

    def _boom(self, *a, **k):
        raise OSError("EIO")

    def test_a_failing_phase_does_not_stop_later_phases(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        calls: list = []
        assert _run_main(_write_cfg(tmp_path, cfg), record=calls,
                         overrides={"_reconcile_ready": self._boom}) == 0
        for later in ("_run_encode_locked_all", "_run_stack_all", "_run_reencode_all"):
            assert later in calls

    def test_a_failed_phase_still_marks_the_night_done(self, tmp_path, cfg, frozen):
        """The re-encode phase is the one that marks morning_done; if it raises,
        the bookkeeping still happens so the reboot guard is not left waiting."""
        _both_rms_done(cfg)
        assert _run_main(_write_cfg(tmp_path, cfg),
                         overrides={"_run_reencode_all": self._boom}) == 0
        for sid in cfg["stations"]:
            assert flags_manager.is_morning_done(sid, NIGHT, cfg)

    def test_a_clean_run_leaves_marking_to_the_reencode_phase(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        assert _run_main(_write_cfg(tmp_path, cfg)) == 0
        # The recorder stands in for _run_reencode_all, which did not mark.
        assert not flags_manager.is_morning_done(STATION_A, NIGHT, cfg)

    def test_an_orchestration_error_still_marks_done_and_propagates(self, tmp_path, cfg, frozen):
        _both_rms_done(cfg)
        with patch.object(dawn_process, "_bulk_phases", side_effect=RuntimeError("bug")):
            with pytest.raises(RuntimeError):
                _run_main(_write_cfg(tmp_path, cfg))
        for sid in cfg["stations"]:
            assert flags_manager.is_morning_done(sid, NIGHT, cfg)

    def test_run_phase_reports_failure(self):
        failures: list = []
        assert dawn_process._run_phase("x", self._boom, failures) is False
        assert failures == ["x"]
        assert dawn_process._run_phase("y", lambda: None, failures) is True
        assert failures == ["x"]
