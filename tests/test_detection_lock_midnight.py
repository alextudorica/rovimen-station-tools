"""Tests for the additive mid-night lock pass in detection_lock.py (nightowl).

The pass reads RMS's real-time per-FF result backups (rms_queue_bkup_*.pickle in
the CapturedFiles session dir), locks the colour chunk of every FF that already
has a meteor, and does nothing else. It must be additive only: never unlock,
never touch FTPdetectinfo, and be a harmless no-op when there are no detections.
"""

from __future__ import annotations

import io
import pickle

import pytest

import detection_lock
import flags_manager


STATION = "RO001E"
DATE_STR = "20260801"


# --- Stand-in for RMS.QueuedPool.BackupContainer -----------------------------
# The real pickles hold this shape: .inputs = [night_dir, ff_name, config],
# .outputs = (ff_name, star_list, meteor_list). We rebuild it locally so the
# tests don't require RMS to be importable. Placing it in a throwaway module
# name also exercises the tolerant unpickler's stub path (the class won't
# resolve at load time), exactly like the real RMS class does off-station.
class _FakeBackupContainer:
    def __init__(self, inputs, outputs):
        self.inputs = inputs
        self.outputs = outputs


def _write_backup(session_dir, idx, ff_name, meteor_list):
    inputs = [str(session_dir), ff_name, {"stationID": STATION}]
    outputs = (ff_name, [[], [], [], []], meteor_list)
    obj = _FakeBackupContainer(inputs, outputs)
    buf = io.BytesIO()
    pickle.dump(obj, buf, protocol=2)
    p = session_dir / f"rms_queue_bkup_{idx}_abcdefghi.pickle"
    p.write_bytes(buf.getvalue())
    return p


@pytest.fixture
def station_layout(tmp_path):
    """Build an RMS session dir + colour chunk dir and return a config dict."""
    rms_data = tmp_path / "RMS_data" / STATION
    session = rms_data / "CapturedFiles" / f"{STATION}_{DATE_STR}_183000_000000"
    session.mkdir(parents=True)

    capture = tmp_path / "color_capture"
    chunk_dir = capture / STATION / DATE_STR
    chunk_dir.mkdir(parents=True)

    # Colour chunks at 20s cadence starting 19:05:00.
    chunk_names = [
        f"{STATION}_{DATE_STR}_190500_color.mkv",
        f"{STATION}_{DATE_STR}_190520_color.mkv",
        f"{STATION}_{DATE_STR}_190540_color.mkv",
    ]
    for n in chunk_names:
        (chunk_dir / n).write_bytes(b"")

    cfg = {
        "stations": {STATION: {"rms_data_path": str(rms_data)}},
        "videocapture_path": str(capture),
        "segment_duration": 20,
        "services": {"detection_lock": {"enabled": True, "midnight_enabled": True}},
    }
    return {"cfg": cfg, "session": session, "chunk_dir": chunk_dir, "chunks": chunk_names}


# --- Tolerant unpickler ------------------------------------------------------

class TestTolerantUnpickler:
    def test_extracts_ff_and_meteor_true(self, tmp_path):
        p = _write_backup(tmp_path, 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits",
                          [("rho", "theta", [[10, 1, 1, 1]])])
        res = detection_lock._ff_meteors_from_backup(p)
        assert res == (f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits", True)

    def test_extracts_ff_and_meteor_false(self, tmp_path):
        p = _write_backup(tmp_path, 1, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits", [])
        res = detection_lock._ff_meteors_from_backup(p)
        assert res == (f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits", False)

    def test_corrupt_pickle_returns_none(self, tmp_path):
        p = tmp_path / "rms_queue_bkup_2_x.pickle"
        p.write_bytes(b"\x80\x02not a real pickle stream")
        assert detection_lock._ff_meteors_from_backup(p) is None

    def test_reduce_gadget_cannot_execute(self, tmp_path):
        """A __reduce__ exec gadget must not resolve a callable through find_class."""
        import os
        from pathlib import Path

        sentinel = tmp_path / "pwned_by_midnight_lock_test"

        class Evil:
            def __reduce__(self):
                return (os.system, (f"touch {sentinel}",))

        buf = io.BytesIO()
        pickle.dump(Evil(), buf, protocol=2)
        p = tmp_path / "rms_queue_bkup_3_x.pickle"
        p.write_bytes(buf.getvalue())
        # Must not raise, must not run os.system, must not produce the sentinel:
        # os.system is not in the allowlisted modules, so find_class returns an
        # inert stub and the REDUCE opcode calls the stub instead of os.system.
        detection_lock._ff_meteors_from_backup(p)
        assert not sentinel.exists()


# --- Full pass ---------------------------------------------------------------

class TestMidnightLockPass:
    def test_locks_only_detected_chunk(self, station_layout):
        s = station_layout
        # Meteor on the FF at 19:05:03 -> block (10.24s) ends 19:05:13.24,
        # wholly inside the first (19:05:00-19:05:20) chunk.
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190503_000_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        # A no-meteor FF in a different chunk -> must stay unlocked.
        _write_backup(s["session"], 1, f"FF_{STATION}_{DATE_STR}_190525_000_0000000.fits", [])

        n = detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"])
        assert n == 1

        state = flags_manager.load(STATION, DATE_STR, s["cfg"])
        locked = {name for name, c in state["chunks"].items() if c.get("lock")}
        assert locked == {f"{STATION}_{DATE_STR}_190500_color.mkv"}
        info = state["chunks"][f"{STATION}_{DATE_STR}_190500_color.mkv"]["lock"]
        assert info["lock_type"] == "detection"

    def test_boundary_ff_locks_both_overlapping_chunks(self, station_layout):
        """An FF block spanning a chunk edge locks both chunks so the whole
        event survives (same _find_chunks_for_time behaviour the EON pass uses)."""
        s = station_layout
        # 19:05:12.123 + 10.24s = 19:05:22.36 -> spills into the 2nd chunk.
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        n = detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"])
        assert n == 2
        state = flags_manager.load(STATION, DATE_STR, s["cfg"])
        locked = {name for name, c in state["chunks"].items() if c.get("lock")}
        assert locked == {
            f"{STATION}_{DATE_STR}_190500_color.mkv",
            f"{STATION}_{DATE_STR}_190520_color.mkv",
        }

    def test_idempotent_no_double_lock(self, station_layout):
        s = station_layout
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190503_000_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        first = detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"])
        second = detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"])
        assert first == 1
        assert second == 0  # already locked -> no new locks

    def test_no_detections_is_noop(self, station_layout):
        s = station_layout
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits", [])
        assert detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"]) == 0
        state = flags_manager.load(STATION, DATE_STR, s["cfg"])
        assert not any(c.get("lock") for c in state.get("chunks", {}).values())

    def test_missing_session_is_noop(self, station_layout):
        s = station_layout
        # Point at a night with no CapturedFiles session.
        assert detection_lock.midnight_lock_pass(STATION, "20260101", s["cfg"]) == 0

    def test_never_unlocks_existing(self, station_layout):
        """Additive only: an existing lock on a now-undetected chunk must survive."""
        s = station_layout
        other = f"{STATION}_{DATE_STR}_190540_color.mkv"
        flags_manager.lock_chunk(STATION, DATE_STR, other,
                                 {"lock_type": "detection", "detection_time": "x"}, s["cfg"])
        # This pass has a detection only in the first chunk; it must NOT remove
        # the pre-existing lock on the third chunk.
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"])
        state = flags_manager.load(STATION, DATE_STR, s["cfg"])
        assert state["chunks"][other].get("lock") is not None

    def test_disabled_via_midnight_flag(self, station_layout):
        s = station_layout
        s["cfg"]["services"]["detection_lock"]["midnight_enabled"] = False
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        assert detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"]) == 0

    def test_disabled_via_detection_lock_flag(self, station_layout):
        s = station_layout
        s["cfg"]["services"]["detection_lock"]["enabled"] = False
        _write_backup(s["session"], 0, f"FF_{STATION}_{DATE_STR}_190512_123_0000000.fits",
                      [("rho", "theta", [[10, 1, 1, 1]])])
        assert detection_lock.midnight_lock_pass(STATION, DATE_STR, s["cfg"]) == 0
