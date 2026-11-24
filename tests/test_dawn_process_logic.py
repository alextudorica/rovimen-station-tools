"""Tests for dawn_process.py pipeline logic — P1 #9.

P1 #9: A single stuck camera blocks all cameras until 11:00 UTC deadline.
       Phase 6 (bulk reencode) is permanently skipped if deadline hits.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock, call

import pytest

import flags_manager


STATION_A = "RO000H"
STATION_B = "RO000J"
DATE_STR = "20260322"


@pytest.fixture
def cfg(tmp_path):
    rms_a = tmp_path / "rms_a"
    rms_b = tmp_path / "rms_b"
    rms_a.mkdir()
    rms_b.mkdir()
    return {
        "videocapture_path": str(tmp_path / "color_capture"),
        "segment_duration": 20,
        "compression_level": 2,
        "stations": {
            STATION_A: {"rms_data_path": str(rms_a), "rotate": False},
            STATION_B: {"rms_data_path": str(rms_b), "rotate": False},
        },
        "services": {
            "stacker": {"enabled": False},
            "reencode": {"enabled": True},
            "detection_lock": {"enabled": True},
            "archive_upload": {"enabled": False},
            "timelapse_build": {"enabled": False},
        },
        "overlay": {"enabled": False},
    }


class TestStuckCameraBlocking:
    """Document: the dawn pipeline waits for ALL cameras to finish RMS
    before processing ANY of them."""

    def test_rms_done_check_gates_all_cameras(self, cfg):
        """If station A is done but station B is not, neither is processed."""
        import dawn_process

        stations = cfg["stations"]

        # Simulate: station A has FTPdetectinfo in ArchivedFiles, station B does not
        rms_a = Path(cfg["stations"][STATION_A]["rms_data_path"])
        archived_a = rms_a / "ArchivedFiles"
        session_a = archived_a / f"{STATION_A}_{DATE_STR}_210000_000000"
        session_a.mkdir(parents=True)
        (session_a / f"FTPdetectinfo_{STATION_A}_{DATE_STR}_210000_000000.txt").write_text(
            "Meteor Count = 0\n"
        )

        # Station B has no session at all — _rms_done returns False
        done_a = dawn_process._rms_done(STATION_A, DATE_STR, str(rms_a))
        done_b = dawn_process._rms_done(STATION_B, DATE_STR,
                                        cfg["stations"][STATION_B]["rms_data_path"])

        assert done_a is True
        assert done_b is False

        # The pipeline checks ALL stations and returns 0 (retry) if any are waiting
        waiting = [
            sid for sid, scfg in stations.items()
            if dawn_process._rms_wait_reason(sid, scfg, DATE_STR) is not None
        ]
        assert STATION_B in waiting
        assert len(waiting) == 1

    def test_rms_false_camera_never_blocks_the_gate(self, cfg):
        """A colour/spectro-only camera (rms: false) must not be waited on.

        Regression test for the 2026-09-17 gmnro01 stall: GH662S/GH662W carry
        ``rms: false`` and no ``rms_data_path``, so ``_rms_done`` is False
        forever and the gate deadlocked all six cameras until the 11:00 UTC
        deadline.
        """
        import dawn_process

        cfg["stations"]["GH662S"] = {"rms": False, "rotate": False}
        waiting = [
            sid for sid, scfg in cfg["stations"].items()
            if dawn_process._rms_wait_reason(sid, scfg, DATE_STR) is not None
        ]
        assert "GH662S" not in waiting
        # RMS cameras without FTPdetectinfo still gate as before.
        assert STATION_A in waiting
        assert STATION_B in waiting

    def test_decommissioned_camera_excluded_from_wait(self, cfg):
        """A decommissioned camera should not block the pipeline."""
        cfg["stations"][STATION_B]["decommissioned"] = True
        stations = {
            sid: scfg for sid, scfg in cfg["stations"].items()
            if not scfg.get("decommissioned")
        }
        assert STATION_B not in stations
        assert STATION_A in stations


class TestDeadlinePhaseSkipping:
    """Document: past-deadline behaviour skips Phase 6 (bulk reencode)
    and marks morning_done, but the deferred reencode is never retried."""

    def test_morning_done_set_after_deadline_skip(self, tmp_path, cfg):
        """After deadline, morning_done is set even though Phase 6 didn't run."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        # Manually set morning_done to simulate the deadline path
        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)
        assert flags_manager.is_morning_done(STATION_A, DATE_STR, cfg) is True

    def test_morning_done_blocks_next_run(self, tmp_path, cfg):
        """Once morning_done, the dawn pipeline returns 0 without processing."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)
        flags_manager.mark_morning_done(STATION_B, DATE_STR, cfg)

        # Both stations are morning_done → pending is empty
        pending = {
            sid: scfg for sid, scfg in cfg["stations"].items()
            if not flags_manager.is_morning_done(sid, DATE_STR, cfg)
        }
        assert len(pending) == 0

    def test_unreencoded_chunks_remain_after_deadline(self, tmp_path, cfg):
        """Chunks that weren't reencoded before the deadline remain unreencoded.
        morning_done is True, so the pipeline won't retry them."""
        cap = Path(cfg["videocapture_path"])
        night_a = cap / STATION_A / DATE_STR
        night_a.mkdir(parents=True)

        chunk_name = f"{STATION_A}_{DATE_STR}_210000_color.mkv"
        (night_a / chunk_name).write_bytes(b"raw-video")
        flags_manager.mark_ready(STATION_A, DATE_STR, chunk_name, cfg)

        # Deadline hits → mark morning_done without reencoding
        flags_manager.mark_morning_done(STATION_A, DATE_STR, cfg)

        state = flags_manager.load(STATION_A, DATE_STR, cfg)
        chunk = state["chunks"].get(chunk_name, {})
        assert chunk.get("reencoded") is False
        assert state.get("morning_done") is True


class TestParallelPhases:
    """Verify _run_encode_locked_all and _run_upload_all invoke all stations."""

    def test_encode_locked_all_calls_both_stations(self, cfg):
        """EON runs per camera; the locked clips of both cameras share one
        encode pool with the batch thread plan."""
        import dawn_process
        cap = Path(cfg["videocapture_path"])
        locked = []
        for sid in [STATION_A, STATION_B]:
            (cap / sid / DATE_STR).mkdir(parents=True)
            name = f"{sid}_{DATE_STR}_210000_color.mkv"
            (cap / sid / DATE_STR / name).write_bytes(b"x")
            flags_manager.lock_chunk(sid, DATE_STR, name, {"lock_type": "detection"}, cfg)
            locked.append(name)

        with patch("dawn_process.detection_lock") as mock_dl, \
             patch.object(dawn_process.encoder, "process_chunk", return_value=True) as enc:
            dawn_process._run_encode_locked_all(cfg["stations"], DATE_STR, cfg)
            assert mock_dl.process_night.call_count == 2
            assert sorted(c.args[0].name for c in enc.call_args_list) == sorted(locked)
            for c in enc.call_args_list:
                assert "_encode_parallelism" in c.args[1]

    def test_upload_all_calls_both_stations(self, cfg):
        import dawn_process
        with patch("dawn_process.archive_upload") as mock_au:
            dawn_process._run_upload_all(cfg["stations"], DATE_STR, cfg)
            assert mock_au.run_night.call_count == 2


class TestDetectionBackfill:
    """_run_detection_backfill retries the EON pass for recent nights that
    slipped through (RMS late, mid-run reboot, FTPdetectinfo race) while
    their chunks are still on disk."""

    def _prep_night(self, cfg, station, night, rms_done=True):
        cap = Path(cfg["videocapture_path"]) / station / night
        cap.mkdir(parents=True, exist_ok=True)
        if rms_done:
            rms = Path(cfg["stations"][station]["rms_data_path"])
            session = rms / "ArchivedFiles" / f"{station}_{night}_210000_000000"
            session.mkdir(parents=True, exist_ok=True)
            (session / f"FTPdetectinfo_{station}_{night}_210000_000000.txt").write_text("x")
        return cap

    def test_unprocessed_night_is_retried_and_marked(self, cfg):
        import dawn_process

        night = "20260321"  # date_str 20260322 minus 1
        self._prep_night(cfg, STATION_A, night)

        with patch.object(dawn_process.detection_lock, "process_night",
                          return_value=True) as dl, \
             patch.object(dawn_process.encoder, "process_night_locked_only") as enc:
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)

        dl.assert_called_once_with(STATION_A, night, cfg)
        enc.assert_called_once()
        assert flags_manager.is_eon_done(STATION_A, night, cfg)

    def test_eon_done_night_skipped(self, cfg):
        import dawn_process

        night = "20260321"
        self._prep_night(cfg, STATION_A, night)
        flags_manager.mark_eon_done(STATION_A, night, cfg)

        with patch.object(dawn_process.detection_lock, "process_night") as dl:
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)
        dl.assert_not_called()

    def test_reaped_night_skipped(self, cfg):
        """No chunk dir left (janitor reaped it) — nothing to lock."""
        import dawn_process

        with patch.object(dawn_process.detection_lock, "process_night") as dl:
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)
        dl.assert_not_called()

    def test_rms_not_done_left_for_later(self, cfg):
        """RMS output still missing — no eon_done mark, retried next run."""
        import dawn_process

        night = "20260321"
        self._prep_night(cfg, STATION_A, night, rms_done=False)

        with patch.object(dawn_process.detection_lock, "process_night") as dl:
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)
        dl.assert_not_called()
        assert not flags_manager.is_eon_done(STATION_A, night, cfg)

    def test_failed_pass_not_marked_done(self, cfg):
        """process_night returning False (skipped) leaves the flag unset."""
        import dawn_process

        night = "20260321"
        self._prep_night(cfg, STATION_A, night)

        with patch.object(dawn_process.detection_lock, "process_night",
                          return_value=False), \
             patch.object(dawn_process.encoder, "process_night_locked_only") as enc:
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)
        enc.assert_not_called()
        assert not flags_manager.is_eon_done(STATION_A, night, cfg)

    def test_late_eon_pass_keeps_manual_locks(self, cfg):
        """RMS finished the night after dawn, so the backfill runs its EON
        pass a day later. A fireball alert's process_clip lock (manual) on a
        chunk RMS did not detect must survive it; an unconfirmed detection
        lock still goes."""
        import dawn_process

        night = "20260321"
        cap = self._prep_night(cfg, STATION_A, night)
        rms = Path(cfg["stations"][STATION_A]["rms_data_path"])
        ftp = next((rms / "ArchivedFiles").glob("*/FTPdetectinfo_*.txt"))
        ftp.write_text("------------------------------\nMeteor Count = 0\n"
                       "------------------------------\n")
        fireball = cap / f"{STATION_A}_{night}_223000_color.mkv"
        false_alarm = cap / f"{STATION_A}_{night}_224000_color.mkv"
        for mkv in (fireball, false_alarm):
            mkv.write_bytes(b"")
        flags_manager.lock_chunk(STATION_A, night, fireball.name,
                                 {"lock_type": "manual", "detection_time": f"{night}_223010",
                                  "meteor_time": None}, cfg)
        flags_manager.lock_chunk(STATION_A, night, false_alarm.name,
                                 {"lock_type": "detection",
                                  "detection_time": f"{night}_224000"}, cfg)

        with patch.object(dawn_process.encoder, "process_night_locked_only"):
            dawn_process._run_detection_backfill(
                {STATION_A: cfg["stations"][STATION_A]}, DATE_STR, cfg)

        assert flags_manager.is_eon_done(STATION_A, night, cfg)
        chunks = flags_manager.load(STATION_A, night, cfg)["chunks"]
        assert chunks[fireball.name]["lock"]["lock_type"] == "manual"
        assert chunks[false_alarm.name]["lock"] is None


class TestFullNightReencodeGate:
    """services.reencode.full_night gates Phase 6 (bulk reencode-all). Default
    'auto' turns it OFF where hardware encode (VAAPI) is unavailable — Pi-class
    boxes that can't finish a night of software encodes — and ON where it is
    cheap. Detection clips are re-encoded in Phase 4 regardless."""

    def test_explicit_true_skips_vaapi_probe(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True, "full_night": True}}}
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device") as probe:
            assert dawn_process._full_night_reencode_enabled(cfg) is True
            probe.assert_not_called()

    def test_explicit_false_skips_vaapi_probe(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True, "full_night": False}}}
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device") as probe:
            assert dawn_process._full_night_reencode_enabled(cfg) is False
            probe.assert_not_called()

    def test_string_off_disables(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True, "full_night": "off"}}}
        assert dawn_process._full_night_reencode_enabled(cfg) is False

    def test_auto_on_with_vaapi(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True, "full_night": "auto"}}}
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value="/dev/dri/renderD128"):
            assert dawn_process._full_night_reencode_enabled(cfg) is True

    def test_auto_off_without_vaapi(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True, "full_night": "auto"}}}
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value=None):
            assert dawn_process._full_night_reencode_enabled(cfg) is False

    def test_missing_key_defaults_to_auto(self):
        import dawn_process
        cfg = {"services": {"reencode": {"enabled": True}}}
        with patch.object(dawn_process.rovimen_media, "find_vaapi_device",
                          return_value=None):
            assert dawn_process._full_night_reencode_enabled(cfg) is False

    def test_run_reencode_all_skips_bulk_when_disabled(self, cfg):
        """Phase 6 skips encoder.process_night when full_night is off, but still
        marks morning_done so the night completes cleanly."""
        import dawn_process
        cfg["services"]["reencode"]["full_night"] = False
        with patch.object(dawn_process, "encoder") as enc, \
             patch.object(dawn_process, "stacker"), \
             patch.object(dawn_process.flags_manager, "mark_morning_done") as md:
            dawn_process._run_reencode_all(cfg["stations"], DATE_STR, cfg)
            enc.process_night.assert_not_called()
            assert md.call_count == len(cfg["stations"])

    def test_run_reencode_all_runs_bulk_when_enabled(self, cfg):
        """full_night True keeps the bulk pass: process_night per camera."""
        import dawn_process
        cfg["services"]["reencode"]["full_night"] = True
        with patch.object(dawn_process.encoder, "process_night") as enc_night, \
             patch.object(dawn_process, "stacker"), \
             patch.object(dawn_process.flags_manager, "mark_morning_done"):
            dawn_process._run_reencode_all(cfg["stations"], DATE_STR, cfg)
            assert enc_night.call_count == len(cfg["stations"])
