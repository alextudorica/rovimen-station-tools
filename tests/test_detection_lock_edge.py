"""Tests for detection_lock.py edge cases — P1 #6, #7.

P1 #6: Chunk matching when chunks have time gaps > segment_secs + 5.
P1 #7: Manual locks survive process_night (only detection locks can be false positives).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import detection_lock
import flags_manager


STATION = "RO000H"
DATE_STR = "20260322"


@pytest.fixture
def chunk_dir(tmp_path, sample_config):
    """Create chunk directory structure."""
    cap = Path(sample_config["videocapture_path"])
    d = cap / STATION / DATE_STR
    d.mkdir(parents=True)
    return d


def _touch_chunk(chunk_dir: Path, time_str: str) -> Path:
    p = chunk_dir / f"{STATION}_{DATE_STR}_{time_str}_color.mkv"
    p.write_bytes(b"")
    return p


# ── P1 #6: Chunk matching with time gaps ──────────────────────────────────


class TestChunkMatchingWithGaps:
    """_find_chunks_for_time uses next chunk's timestamp as the boundary.
    When there's a gap, it falls back to ts + segment_secs + 5."""

    def test_contiguous_chunks_match(self, chunk_dir):
        """No gaps: detection at 21:00:05 matches chunk starting at 21:00:00.
        FF_DURATION is 10s, so detection at 21:00:05 spans 21:00:05-21:00:15,
        which fits within the 210000 chunk (ends at 210020)."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        ff_time = datetime(2026, 3, 22, 21, 0, 5)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_within_fallback_window_still_matches(self, chunk_dir):
        """Gap of segment_secs + 4 (< segment_secs + 5): detection still matches."""
        _touch_chunk(chunk_dir, "210000")
        # Next chunk at 210044 (gap of 24s where segment=20 → fallback window is 25s)
        _touch_chunk(chunk_dir, "210044")

        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_with_next_chunk_extends_boundary(self, chunk_dir):
        """When a next chunk exists, the boundary extends to next_ts regardless
        of segment_secs. A detection in a gap between two chunks is attributed
        to the earlier chunk — even though the video doesn't cover that time.
        This documents the incorrect-attribution behaviour."""
        _touch_chunk(chunk_dir, "210000")
        # Next chunk at 210200 (120s gap, segment=20)
        _touch_chunk(chunk_dir, "210200")

        # Detection at 21:00:40 — 40s after chunk 1 started, but chunk 1 is
        # only 20s long. Still matches because next_ts = 210200.
        ff_time = datetime(2026, 3, 22, 21, 0, 40)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210000" in result[0].name

    def test_gap_beyond_last_chunk_fallback_misses(self, chunk_dir):
        """The last chunk uses fallback ts + segment_secs + 5. A detection
        beyond that window is not matched."""
        _touch_chunk(chunk_dir, "210000")
        # No more chunks after this one

        # Detection at 21:00:30 — beyond fallback (210000 + 25s = 210025)
        ff_time = datetime(2026, 3, 22, 21, 0, 30)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 0

    def test_detection_at_last_chunk_uses_fallback(self, chunk_dir):
        """Last chunk has no successor: uses ts + segment_secs + 5."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        # Detection at 21:00:30 — within last chunk's fallback (210020 + 25 = 210045)
        ff_time = datetime(2026, 3, 22, 21, 0, 30)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1
        assert "210020" in result[0].name

    def test_ff_spanning_two_chunks(self, chunk_dir):
        """FF block spanning chunk boundary returns both chunks."""
        _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")

        # FF at 21:00:18 with FF_DURATION=10s spans into 210020 chunk
        ff_time = datetime(2026, 3, 22, 21, 0, 18)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 2

    def test_no_chunks_returns_empty(self, tmp_path):
        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, tmp_path / "nonexistent", segment_secs=20)
        assert result == []

    def test_nonmatching_files_ignored(self, chunk_dir):
        """Files that don't match _CHUNK_RE are silently skipped."""
        (chunk_dir / "README.txt").write_text("ignore me")
        (chunk_dir / "some_other.mkv").write_bytes(b"")
        _touch_chunk(chunk_dir, "210000")

        ff_time = datetime(2026, 3, 22, 21, 0, 10)
        result = detection_lock._find_chunks_for_time(ff_time, chunk_dir, segment_secs=20)
        assert len(result) == 1


# ── P1 #7: Manual locks survive process_night ───────────────────────────


def _empty_ftpdetectinfo(sample_config) -> None:
    """An FTPdetectinfo with no meteors: every unconfirmed lock is tested."""
    rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
    session_dir = rms_path / "ArchivedFiles" / f"{STATION}_{DATE_STR}_210000_000000"
    session_dir.mkdir(parents=True)
    (session_dir / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt").write_text(
        "------------------------------\n"
        "Meteor Count = 0\n"
        "------------------------------\n"
    )


class TestManualLockPreservation:
    """process_night removes the detection locks FTPdetectinfo does not
    confirm. A manual lock (an operator's /api/lock or Process click, or a
    fireball alert's process_clip at night) is not a detection RMS could
    refute: removing it let the rolling release delete a fireball RMS
    missed before it uploaded."""

    def _lock(self, sample_config, mkv: Path, lock_info: dict) -> None:
        flags_manager.lock_chunk(STATION, DATE_STR, mkv.name, lock_info, sample_config)

    def _lock_after_eon(self, sample_config, mkv: Path):
        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            assert detection_lock.process_night(STATION, DATE_STR, sample_config)
        state = flags_manager.load(STATION, DATE_STR, sample_config)
        return state.get("chunks", {}).get(mkv.name, {}).get("lock")

    def test_manual_lock_survives_the_false_positive_sweep(self, chunk_dir, sample_config):
        mkv = _touch_chunk(chunk_dir, "210000")
        _touch_chunk(chunk_dir, "210020")
        self._lock(sample_config, mkv, {"lock_type": "manual", "reason": "interesting"})
        _empty_ftpdetectinfo(sample_config)

        lock = self._lock_after_eon(sample_config, mkv)

        assert lock == {"lock_type": "manual", "reason": "interesting"}

    def test_unconfirmed_detection_lock_is_removed(self, chunk_dir, sample_config):
        mkv = _touch_chunk(chunk_dir, "210000")
        self._lock(sample_config, mkv, {"lock_type": "detection",
                                        "detection_time": f"{DATE_STR}_210000"})
        _empty_ftpdetectinfo(sample_config)

        assert self._lock_after_eon(sample_config, mkv) is None

    def test_lock_without_a_type_counts_as_a_detection(self, chunk_dir, sample_config):
        mkv = _touch_chunk(chunk_dir, "210000")
        self._lock(sample_config, mkv, {"detection_time": f"{DATE_STR}_210000"})
        _empty_ftpdetectinfo(sample_config)

        assert self._lock_after_eon(sample_config, mkv) is None

    def test_legacy_sidecar_lock_is_still_listed_as_a_false_positive(
            self, chunk_dir, sample_config):
        """Unchanged: a chunk locked only by a legacy .locked sidecar is
        counted with the false positives (its state entry is cleared)."""
        mkv = _touch_chunk(chunk_dir, "210000")
        Path(str(mkv) + ".locked").write_text(json.dumps({"lock_type": "detection"}))
        _empty_ftpdetectinfo(sample_config)

        with patch.object(detection_lock, "_stitch_clips", return_value=None), \
             patch.object(flags_manager, "apply_lock_changes",
                          wraps=flags_manager.apply_lock_changes) as apply:
            detection_lock.process_night(STATION, DATE_STR, sample_config)

        assert list(apply.call_args.kwargs["unlocks"]) == [mkv.name]

    def test_detection_lock_preserved(self, chunk_dir, sample_config):
        """A detection-locked chunk that IS in FTPdetectinfo survives."""
        mkv = _touch_chunk(chunk_dir, "213045")

        # process_night reads from ArchivedFiles/<session>/
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        archived = rms_path / "ArchivedFiles"
        session_dir = archived / f"{STATION}_{DATE_STR}_210000_000000"
        session_dir.mkdir(parents=True)
        ftp = session_dir / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt"
        ftp.write_text(
            "------------------------------\n"
            "Meteor Count = 1\n"
            "------------------------------\n"
            f"FF_{STATION}_{DATE_STR}_213045_000\n"
            "720  576 25.00  fps\n"
            "   1.50  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
            "------------------------------\n"
        )

        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            detection_lock.process_night(STATION, DATE_STR, sample_config)

        state = flags_manager.load(STATION, DATE_STR, sample_config)
        chunk_entry = state.get("chunks", {}).get(mkv.name, {})
        assert chunk_entry.get("lock") is not None
        assert chunk_entry["lock"]["lock_type"] == "detection"


# ── CapturedFiles fallback + return value (FTPdetectinfo race) ────────────


FTP_ONE_DETECTION = (
    "------------------------------\n"
    "Meteor Count = 1\n"
    "------------------------------\n"
    f"FF_{STATION}_{DATE_STR}_213045_000\n"
    "720  576 25.00  fps\n"
    "   1.50  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
    "------------------------------\n"
)

FTP_NO_DETECTIONS = (
    "------------------------------\n"
    "Meteor Count = 0\n"
    "------------------------------\n"
)


def _write_session(rms_path: Path, sub: str, content: str | None) -> Path:
    session = rms_path / sub / f"{STATION}_{DATE_STR}_210000_000000"
    session.mkdir(parents=True, exist_ok=True)
    if content is not None:
        (session / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt").write_text(content)
    return session


class TestCapturedFilesRace:
    """RMS writes FTPdetectinfo into CapturedFiles first and mirrors it to
    ArchivedFiles later. The EON pass must accept either location (matching
    dawn's _rms_done gate) or dawn can start in the gap, lock nothing, and the
    whole night stays "clip pending" until the janitor reaps the chunks."""

    def test_capturedfiles_only_still_locks(self, chunk_dir, sample_config):
        mkv = _touch_chunk(chunk_dir, "213045")
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        _write_session(rms_path, "CapturedFiles", FTP_ONE_DETECTION)

        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            assert detection_lock.process_night(STATION, DATE_STR, sample_config) is True

        state = flags_manager.load(STATION, DATE_STR, sample_config)
        assert state["chunks"][mkv.name]["lock"]["lock_type"] == "detection"

    def test_archivedfiles_wins_when_session_in_both(self, chunk_dir, sample_config):
        """The ArchivedFiles copy (final, filtered) takes precedence: a
        detection present only in the stale CapturedFiles copy is ignored."""
        mkv = _touch_chunk(chunk_dir, "213045")
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        _write_session(rms_path, "CapturedFiles", FTP_ONE_DETECTION)
        _write_session(rms_path, "ArchivedFiles", FTP_NO_DETECTIONS)

        with patch.object(detection_lock, "_stitch_clips", return_value=None):
            assert detection_lock.process_night(STATION, DATE_STR, sample_config) is True

        state = flags_manager.load(STATION, DATE_STR, sample_config)
        assert state.get("chunks", {}).get(mkv.name, {}).get("lock") is None

    def test_no_session_dirs_returns_false(self, chunk_dir, sample_config):
        _touch_chunk(chunk_dir, "213045")
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        (rms_path / "ArchivedFiles").mkdir(parents=True)

        assert detection_lock.process_night(STATION, DATE_STR, sample_config) is False

    def test_session_without_ftpdetectinfo_skips_and_keeps_locks(self, chunk_dir, sample_config):
        """A session dir whose detection output isn't written yet must not
        strip real-time locks as false positives — the pass retries later."""
        mkv = _touch_chunk(chunk_dir, "213045")
        flags_manager.lock_chunk(
            STATION, DATE_STR, mkv.name, {"lock_type": "event"}, sample_config)
        rms_path = Path(sample_config["stations"][STATION]["rms_data_path"])
        _write_session(rms_path, "CapturedFiles", None)

        assert detection_lock.process_night(STATION, DATE_STR, sample_config) is False
        state = flags_manager.load(STATION, DATE_STR, sample_config)
        assert state["chunks"][mkv.name]["lock"] is not None


class TestColourOnlyCameraSkip:
    """process_night must skip, not crash, on a camera that never runs RMS.

    Regression: GH662S/GH662W (rms: false, no rms_data_path) raised KeyError
    on cfg['stations'][cam]['rms_data_path'] every dawn (2026-09-17), logged
    as 'EON detection pass failed' with a traceback.
    """

    def test_rms_false_camera_is_skipped_cleanly(self):
        import detection_lock

        cfg = {"stations": {"GH662S": {"rms": False}}}
        assert detection_lock.process_night("GH662S", "20260916", cfg) is False

    def test_missing_rms_data_path_is_skipped_cleanly(self):
        import detection_lock

        cfg = {"stations": {"GH662W": {}}}
        assert detection_lock.process_night("GH662W", "20260916", cfg) is False


def test_legacy_local_chunk_names_match_utc_ff_times(chunk_dir):
    """chunk_names_utc: false (pre-#851 local-named chunks): FF times are UTC,
    so the chunk start must be converted before comparing, as station_api
    does. Default (UTC names) is the plain parse."""
    _touch_chunk(chunk_dir, "230000")  # 23:00:00 EET = 21:00:00 UTC in March
    _touch_chunk(chunk_dir, "230020")
    ff_time = datetime(2026, 3, 22, 21, 0, 5)
    legacy = {"chunk_names_utc": False, "legacy_chunk_tz": "Europe/Bucharest"}
    hit = detection_lock._find_chunks_for_time(ff_time, chunk_dir, 20, legacy)
    assert [p.name for p in hit] == [f"{STATION}_{DATE_STR}_230000_color.mkv"]
    assert detection_lock._find_chunks_for_time(ff_time, chunk_dir, 20) == []
    assert detection_lock._find_chunks_for_time(ff_time, chunk_dir, 20, {}) == []
