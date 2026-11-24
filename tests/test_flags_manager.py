"""Tests for flags_manager.py — central state.json manager."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

import flags_manager


# -- Fixtures ----------------------------------------------------------------

@pytest.fixture
def station():
    return "RO000H"


@pytest.fixture
def date():
    return "20260322"


@pytest.fixture
def chunk():
    return "RO000H_20260322_210000_color.mkv"


@pytest.fixture
def cfg(tmp_path):
    """Config dict pointing videocapture_path at a tmp dir."""
    return {"videocapture_path": str(tmp_path / "color_capture")}


@pytest.fixture(autouse=True)
def _clear_warn_set():
    """Reset the module-level warning dedup set between tests."""
    flags_manager._unreadable_warned.clear()
    yield
    flags_manager._unreadable_warned.clear()


# -- _state_path fallback chain ---------------------------------------------

class TestStatePath:
    def test_uses_videocapture_path(self, tmp_path, station, date):
        cfg = {"videocapture_path": str(tmp_path / "vcap")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "vcap" / station / date / "state.json"
        )

    def test_falls_back_to_color_video_path(self, tmp_path, station, date):
        cfg = {"color_video_path": str(tmp_path / "cvid")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "cvid" / station / date / "state.json"
        )

    def test_falls_back_to_reenc_path(self, tmp_path, station, date):
        cfg = {"reenc_path": str(tmp_path / "reenc")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "reenc" / station / date / "state.json"
        )

    def test_falls_back_to_color_capture_path(self, tmp_path, station, date):
        cfg = {"color_capture_path": str(tmp_path / "ccap")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "ccap" / station / date / "state.json"
        )

    def test_falls_back_to_ssd_color_path(self, tmp_path, station, date):
        cfg = {"ssd_color_path": str(tmp_path / "ssd")}
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "ssd" / station / date / "state.json"
        )

    def test_falls_back_to_home_color_capture(self, station, date):
        cfg = {}
        expected = Path.home() / "color_capture" / station / date / "state.json"
        assert flags_manager._state_path(station, date, cfg) == expected

    def test_first_non_empty_wins(self, tmp_path, station, date):
        """videocapture_path="" should be falsy, so color_video_path wins."""
        cfg = {
            "videocapture_path": "",
            "color_video_path": str(tmp_path / "winner"),
        }
        assert flags_manager._state_path(station, date, cfg) == (
            tmp_path / "winner" / station / date / "state.json"
        )


# -- _empty_state / _empty_chunk --------------------------------------------

class TestEmptyStructures:
    def test_empty_state_fields(self, station, date):
        state = flags_manager._empty_state(station, date)
        assert state["station"] == station
        assert state["date"] == date
        assert state["chunks"] == {}
        assert state["rms_complete"] is False
        assert state["morning_done"] is False
        assert state["timelapse_done"] is False
        assert state["timelapse_deferred"] is False
        assert state["timelapse_uploaded"] is False
        assert state["night_stack_uploaded"] is False

    def test_empty_chunk_fields(self):
        chunk = flags_manager._empty_chunk()
        assert chunk == {
            "ready": False,
            "stacked": False,
            "reencoded": False,
            "lock": None,
            "uploaded": False,
            "stack_uploaded": False,
        }


# -- Load / Save core -------------------------------------------------------

class TestLoadSave:
    def test_load_returns_empty_when_no_file(self, station, date, cfg):
        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station
        assert state["chunks"] == {}

    def test_save_creates_directory_structure(self, station, date, cfg):
        state = flags_manager._empty_state(station, date)
        flags_manager.save(state, station, date, cfg)
        path = flags_manager._state_path(station, date, cfg)
        assert path.exists()
        assert path.parent.is_dir()

    def test_save_writes_valid_json(self, station, date, cfg):
        state = flags_manager._empty_state(station, date)
        state["rms_complete"] = True
        flags_manager.save(state, station, date, cfg)
        path = flags_manager._state_path(station, date, cfg)
        loaded = json.loads(path.read_text())
        assert loaded["rms_complete"] is True

    def test_load_after_save_roundtrips(self, station, date, cfg, chunk):
        state = flags_manager._empty_state(station, date)
        state["chunks"][chunk] = flags_manager._empty_chunk()
        state["chunks"][chunk]["ready"] = True
        flags_manager.save(state, station, date, cfg)

        loaded = flags_manager.load(station, date, cfg)
        assert loaded == state

    def test_load_corrupted_json_returns_empty(self, station, date, cfg):
        """A corrupted state.json must not crash -- return empty state."""
        path = flags_manager._state_path(station, date, cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{{{not valid json")

        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station
        assert state["chunks"] == {}

    def test_load_binary_garbage_returns_empty(self, station, date, cfg):
        path = flags_manager._state_path(station, date, cfg)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"\x00\xff\xfe\x80")

        state = flags_manager.load(station, date, cfg)
        assert state["station"] == station


# -- Chunk-level mark functions ----------------------------------------------

class TestMarkReady:
    def test_sets_ready_flag(self, station, date, chunk, cfg):
        flags_manager.mark_ready(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["ready"] is True

    def test_creates_chunk_entry_if_missing(self, station, date, chunk, cfg):
        flags_manager.mark_ready(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        # Should have all default fields besides ready=True
        assert state["chunks"][chunk]["stacked"] is False
        assert state["chunks"][chunk]["lock"] is None


class TestMarkStacked:
    def test_sets_stacked_flag(self, station, date, chunk, cfg):
        flags_manager.mark_stacked(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["stacked"] is True


class TestMarkReencoded:
    def test_sets_reencoded_flag(self, station, date, chunk, cfg):
        flags_manager.mark_reencoded(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["reencoded"] is True


class TestEditState:
    def test_edit_is_saved(self, station, date, cfg):
        def edit(state):
            state["custom"] = [1, 2]

        assert flags_manager.edit_state(station, date, cfg, edit) is True
        assert flags_manager.load(station, date, cfg)["custom"] == [1, 2]

    def test_returning_false_skips_the_write(self, station, date, cfg):
        path = flags_manager._state_path(station, date, cfg)

        def edit(state):
            state["custom"] = "not saved"
            return False

        assert flags_manager.edit_state(station, date, cfg, edit) is False
        assert not path.exists()

    def test_runs_under_the_night_lock(self, station, date, cfg, monkeypatch):
        taken = []
        real = flags_manager._lock

        def spy(*args):
            taken.append(args[:2])
            return real(*args)

        monkeypatch.setattr(flags_manager, "_lock", spy)
        flags_manager.edit_state(station, date, cfg, lambda s: None)
        assert taken == [(station, date)]


class TestUpdateChunk:
    def test_sets_and_drops_fields_in_one_write(self, station, date, chunk, cfg):
        flags_manager.update_chunk(station, date, chunk, cfg, {"enc": "QP20", "x": 1})
        flags_manager.update_chunk(station, date, chunk, cfg,
                                   {"reencoded": True, "kept_source": True}, ("enc",))
        entry = flags_manager.load(station, date, cfg)["chunks"][chunk]
        assert entry["reencoded"] is True and entry["kept_source"] is True
        assert "enc" not in entry and entry["x"] == 1
        # A new entry starts from the full empty-chunk template.
        assert set(flags_manager._empty_chunk()) <= set(entry)


# -- Lock management ---------------------------------------------------------

class TestLockChunk:
    def test_sets_lock_metadata(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection", "detection_time": "20260322_210500"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == lock_info

    def test_also_marks_ready(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["ready"] is True

    def test_creates_chunk_entry_if_missing(self, station, date, cfg):
        new_chunk = "RO000H_20260322_220000_color.mkv"
        lock_info = {"lock_type": "manual"}
        flags_manager.lock_chunk(station, date, new_chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert new_chunk in state["chunks"]
        assert state["chunks"][new_chunk]["lock"] == lock_info


class TestUnlockChunk:
    def test_clears_lock(self, station, date, chunk, cfg):
        lock_info = {"lock_type": "detection"}
        flags_manager.lock_chunk(station, date, chunk, lock_info, cfg)
        flags_manager.unlock_chunk(station, date, chunk, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] is None

    def test_noop_on_nonexistent_chunk(self, station, date, cfg):
        """Unlocking a chunk that does not exist in state should not crash."""
        flags_manager.unlock_chunk(station, date, "nonexistent.mkv", cfg)
        state = flags_manager.load(station, date, cfg)
        assert "nonexistent.mkv" not in state["chunks"]


class TestRelockChunk:
    def test_replaces_lock_metadata(self, station, date, chunk, cfg):
        original = {"lock_type": "detection", "detection_time": "20260322_210500"}
        updated = {
            "lock_type": "detection",
            "detection_time": "20260322_210500",
            "meteor_time": "2026-03-22T21:05:03.456",
        }
        flags_manager.lock_chunk(station, date, chunk, original, cfg)
        flags_manager.relock_chunk(station, date, chunk, updated, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == updated
        assert state["chunks"][chunk]["lock"]["meteor_time"] == "2026-03-22T21:05:03.456"

    def test_relock_on_unlocked_chunk(self, station, date, chunk, cfg):
        """Relock should work even if the chunk was never locked."""
        lock_info = {"lock_type": "eon_pass"}
        flags_manager.relock_chunk(station, date, chunk, lock_info, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["chunks"][chunk]["lock"] == lock_info


class TestApplyLockChanges:
    """The batch must leave exactly the state the per-chunk helpers leave."""

    def _seed(self, station, date, cfg):
        flags_manager.mark_ready(station, date, "RO000H_20260322_210000_color.mkv", cfg)
        flags_manager.lock_chunk(station, date, "RO000H_20260322_210020_color.mkv",
                                 {"lock_type": "detection", "detection_time": "a"}, cfg)
        flags_manager.lock_chunk(station, date, "RO000H_20260322_210040_color.mkv",
                                 {"lock_type": "detection", "detection_time": "b"}, cfg)
        flags_manager.mark_stacked(station, date, "RO000H_20260322_210040_color.mkv", cfg)

    def test_matches_the_per_chunk_helpers(self, tmp_path, station, date):
        relocks = {
            "RO000H_20260322_210000_color.mkv": {"lock_type": "detection", "m": 1},
            "RO000H_20260322_210020_color.mkv": {"lock_type": "detection", "m": 2},
            "RO000H_20260322_210110_210130_color.mkv": {"lock_type": "detection", "m": 3},
        }
        unlocks = ["RO000H_20260322_210040_color.mkv", "never_seen.mkv"]

        per_chunk = {"videocapture_path": str(tmp_path / "a")}
        self._seed(station, date, per_chunk)
        for name, info in relocks.items():
            flags_manager.relock_chunk(station, date, name, info, per_chunk)
        for name in unlocks:
            flags_manager.unlock_chunk(station, date, name, per_chunk)

        batched = {"videocapture_path": str(tmp_path / "b")}
        self._seed(station, date, batched)
        assert flags_manager.apply_lock_changes(
            station, date, batched, relocks=relocks, unlocks=unlocks) is True

        assert (flags_manager.load(station, date, batched)
                == flags_manager.load(station, date, per_chunk))

    def test_one_write_for_the_whole_batch(self, station, date, cfg, monkeypatch):
        saves = []
        real_save = flags_manager.save
        monkeypatch.setattr(flags_manager, "save",
                            lambda *a, **kw: saves.append(1) or real_save(*a, **kw))
        flags_manager.apply_lock_changes(
            station, date, cfg,
            relocks={f"RO000H_20260322_2100{i:02d}_color.mkv": {"lock_type": "detection"}
                     for i in range(0, 60, 20)},
            unlocks=["RO000H_20260322_210100_color.mkv"])
        assert len(saves) == 1

    def test_no_changes_writes_nothing(self, station, date, cfg):
        assert flags_manager.apply_lock_changes(station, date, cfg) is True
        assert not flags_manager._state_path(station, date, cfg).exists()

    def test_reports_a_failed_write(self, station, date, cfg, monkeypatch):
        monkeypatch.setattr(flags_manager, "save", lambda *a, **kw: False)
        assert flags_manager.apply_lock_changes(
            station, date, cfg, relocks={"x_color.mkv": {"lock_type": "detection"}}) is False


# -- Night-level flags -------------------------------------------------------

class TestRmsComplete:
    def test_marks_rms_complete(self, station, date, cfg):
        flags_manager.mark_rms_complete(station, date, cfg)
        state = flags_manager.load(station, date, cfg)
        assert state["rms_complete"] is True


class TestMorningDone:
    def test_marks_morning_done(self, station, date, cfg):
        flags_manager.mark_morning_done(station, date, cfg)
        assert flags_manager.is_morning_done(station, date, cfg) is True

    def test_is_morning_done_false_by_default(self, station, date, cfg):
        assert flags_manager.is_morning_done(station, date, cfg) is False


class TestStageTimes:
    """stage_times feeds the dashboard's night timeline."""

    def _clock(self, monkeypatch, *stamps):
        it = iter(stamps)
        real = flags_manager.time.strptime
        monkeypatch.setattr(flags_manager.time, "gmtime", lambda: real(next(it), "%Y-%m-%dT%H:%M:%SZ"))

    def test_records_each_stage_once(self, monkeypatch, station, date, chunk, cfg):
        # one reading per stamp: a stage already recorded reads no clock
        self._clock(monkeypatch, "2026-03-23T05:40:00Z",
                    "2026-03-23T06:05:00Z", "2026-03-23T06:05:00Z", "2026-03-23T07:10:00Z")
        flags_manager.mark_rms_complete(station, date, cfg)
        flags_manager.mark_rms_complete(station, date, cfg)   # a reprocess keeps the first
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        flags_manager.mark_morning_done(station, date, cfg)
        times = flags_manager.load(station, date, cfg)["stage_times"]
        assert times == {
            "rms_complete": "2026-03-23T05:40:00Z",
            "first_upload": "2026-03-23T06:05:00Z",
            "last_upload": "2026-03-23T06:05:00Z",
            "morning_done": "2026-03-23T07:10:00Z",
        }

    def test_batch_upload_moves_last_upload_only(self, monkeypatch, station, date, chunk, cfg):
        self._clock(monkeypatch, "2026-03-23T06:05:00Z", "2026-03-23T06:05:00Z",
                    "2026-03-23T09:30:00Z")
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        flags_manager.mark_uploaded_batch(
            station, date, [(flags_manager.MARK_CHUNK, chunk, True)], cfg)
        times = flags_manager.load(station, date, cfg)["stage_times"]
        assert times == {"first_upload": "2026-03-23T06:05:00Z", "last_upload": "2026-03-23T09:30:00Z"}


# -- Upload tracking ---------------------------------------------------------

class TestChunkUploaded:
    def test_mark_and_query(self, station, date, chunk, cfg):
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is False
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is True

    def test_unmark_clears_uploaded_and_stack_uploaded(self, station, date, chunk, cfg):
        flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
        flags_manager.mark_chunk_stack_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is True
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is True

        flags_manager.unmark_chunk_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_uploaded(station, date, chunk, cfg) is False
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is False

    def test_unmark_noop_on_missing_chunk(self, station, date, cfg):
        """Unmarking a non-existent chunk should not crash or create it."""
        flags_manager.unmark_chunk_uploaded(station, date, "ghost.mkv", cfg)
        state = flags_manager.load(station, date, cfg)
        assert "ghost.mkv" not in state["chunks"]


class TestChunkStackUploaded:
    def test_mark_and_query(self, station, date, chunk, cfg):
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is False
        flags_manager.mark_chunk_stack_uploaded(station, date, chunk, cfg)
        assert flags_manager.is_chunk_stack_uploaded(station, date, chunk, cfg) is True


class TestTimelapseUploaded:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_uploaded(station, date, cfg) is False
        flags_manager.mark_timelapse_uploaded(station, date, cfg)
        assert flags_manager.is_timelapse_uploaded(station, date, cfg) is True


class TestNightStackUploaded:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_night_stack_uploaded(station, date, cfg) is False
        flags_manager.mark_night_stack_uploaded(station, date, cfg)
        assert flags_manager.is_night_stack_uploaded(station, date, cfg) is True


# -- Timelapse done / deferred -----------------------------------------------

class TestTimelapseDone:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_done(station, date, cfg) is False
        flags_manager.mark_timelapse_done(station, date, cfg)
        assert flags_manager.is_timelapse_done(station, date, cfg) is True

    def test_mark_done_clears_deferred(self, station, date, cfg):
        flags_manager.mark_timelapse_deferred(station, date, cfg)
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is True

        flags_manager.mark_timelapse_done(station, date, cfg)
        assert flags_manager.is_timelapse_done(station, date, cfg) is True
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is False


class TestTimelapseDeferred:
    def test_mark_and_query(self, station, date, cfg):
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is False
        flags_manager.mark_timelapse_deferred(station, date, cfg)
        assert flags_manager.is_timelapse_deferred(station, date, cfg) is True


# -- Concurrent operations ---------------------------------------------------

class TestConcurrency:
    def test_parallel_marks_do_not_corrupt_state(self, station, date, cfg):
        """10 threads each marking a different chunk must not lose any entry."""
        chunk_names = [f"RO000H_20260322_21{i:02d}00_color.mkv" for i in range(10)]
        errors: list[Exception] = []

        def mark_one(name: str):
            try:
                flags_manager.mark_ready(station, date, name, cfg)
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=mark_one, args=(n,)) for n in chunk_names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        state = flags_manager.load(station, date, cfg)
        assert len(state["chunks"]) == 10
        for name in chunk_names:
            assert state["chunks"][name]["ready"] is True

    def test_parallel_mixed_operations(self, station, date, cfg):
        """Mixed mark/lock/unlock from multiple threads must not crash."""
        chunk = "RO000H_20260322_210000_color.mkv"
        errors: list[Exception] = []

        def worker(op: str):
            try:
                if op == "ready":
                    flags_manager.mark_ready(station, date, chunk, cfg)
                elif op == "lock":
                    flags_manager.lock_chunk(station, date, chunk,
                                             {"lock_type": "detection"}, cfg)
                elif op == "unlock":
                    flags_manager.unlock_chunk(station, date, chunk, cfg)
                elif op == "stacked":
                    flags_manager.mark_stacked(station, date, chunk, cfg)
                elif op == "uploaded":
                    flags_manager.mark_chunk_uploaded(station, date, chunk, cfg)
            except Exception as exc:
                errors.append(exc)

        ops = ["ready", "lock", "stacked", "uploaded", "unlock",
               "ready", "lock", "stacked", "uploaded", "unlock"]
        threads = [threading.Thread(target=worker, args=(op,)) for op in ops]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == [], f"Thread errors: {errors}"
        # State should load cleanly regardless of operation order
        state = flags_manager.load(station, date, cfg)
        assert chunk in state["chunks"]


# -- make_state_json fixture integration -------------------------------------

class TestMakeStateJsonFixture:
    def test_pre_populated_state_loads(self, make_state_json, station, date, cfg, chunk):
        """The conftest make_state_json fixture should produce loadable state."""
        state = flags_manager._empty_state(station, date)
        state["chunks"][chunk] = flags_manager._empty_chunk()
        state["chunks"][chunk]["ready"] = True
        state["chunks"][chunk]["lock"] = {"lock_type": "detection"}
        make_state_json(station, date, state, cfg)

        loaded = flags_manager.load(station, date, cfg)
        assert loaded["chunks"][chunk]["ready"] is True
        assert loaded["chunks"][chunk]["lock"]["lock_type"] == "detection"


# -- mark_ready_batch --------------------------------------------------------

class TestMarkReadyBatch:

    def test_marks_multiple_chunks(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        names = ["RO000H_20260315_210000_color.mkv", "RO000H_20260315_210020_color.mkv"]
        flags_manager.mark_ready_batch("RO000H", "20260315", names, cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        for name in names:
            assert state["chunks"][name]["ready"] is True

    def test_empty_list_is_noop(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        flags_manager.mark_ready_batch("RO000H", "20260315", [], cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        assert state["chunks"] == {}

    def test_preserves_existing_chunks(self, tmp_path):
        cfg = {"videocapture_path": str(tmp_path / "color_capture")}
        night = tmp_path / "color_capture" / "RO000H" / "20260315"
        night.mkdir(parents=True)
        flags_manager.mark_ready("RO000H", "20260315", "existing.mkv", cfg)
        flags_manager.mark_ready_batch("RO000H", "20260315", ["new.mkv"], cfg)
        state = flags_manager.load("RO000H", "20260315", cfg)
        assert state["chunks"]["existing.mkv"]["ready"] is True
        assert state["chunks"]["new.mkv"]["ready"] is True


def test_save_reports_failure_and_mark_does_not_log_false_success(tmp_path, monkeypatch, caplog):
    """save() logs and swallows a failed write; it returns False so callers
    such as mark_morning_done don't log a success that never hit the disk."""
    import errno

    import flags_manager as fm

    cfg = {'videocapture_path': str(tmp_path)}

    def full_disk(src, dst, *a, **k):
        raise OSError(errno.ENOSPC, 'No space left on device')

    monkeypatch.setattr(fm.os, 'replace', full_disk)
    with caplog.at_level('INFO', logger=fm.logger.name):
        assert fm.save(fm._empty_state('RO000H', '20260920'), 'RO000H', '20260920', cfg) is False
        assert fm.mark_morning_done('RO000H', '20260920', cfg) is False
    assert not any('marked done' in r.getMessage() for r in caplog.records)
    monkeypatch.undo()
    assert fm.mark_morning_done('RO000H', '20260920', cfg) is True


# -- A state.json that exists but cannot be read is never overwritten ---------

_NIGHT = ('RO000H', '20261108')
_LOCKED = 'RO000H_20261108_223000_color.mkv'

# Every writer, with the change it would make. Each used to load() an empty
# state on ANY read error and save it with its one change over the file.
_WRITERS = {
    'mark_ready': lambda cfg: flags_manager.mark_ready(*_NIGHT, 'x_color.mkv', cfg),
    'mark_ready_batch': lambda cfg: flags_manager.mark_ready_batch(*_NIGHT, ['x_color.mkv'], cfg),
    'lock_chunk': lambda cfg: flags_manager.lock_chunk(
        *_NIGHT, 'x_color.mkv', {'lock_type': 'detection'}, cfg),
    'lock_chunk_if_unlocked': lambda cfg: flags_manager.lock_chunk_if_unlocked(
        *_NIGHT, 'x_color.mkv', {'lock_type': 'manual'}, cfg),
    'unlock_chunk': lambda cfg: flags_manager.unlock_chunk(*_NIGHT, _LOCKED, cfg),
    'relock_chunk': lambda cfg: flags_manager.relock_chunk(
        *_NIGHT, 'x_color.mkv', {'lock_type': 'detection'}, cfg),
    'apply_lock_changes': lambda cfg: flags_manager.apply_lock_changes(
        *_NIGHT, cfg, relocks={'x_color.mkv': {'lock_type': 'detection'}}),
    'mark_stacked': lambda cfg: flags_manager.mark_stacked(*_NIGHT, 'x_color.mkv', cfg),
    'mark_reencoded': lambda cfg: flags_manager.mark_reencoded(*_NIGHT, 'x_color.mkv', cfg),
    'mark_rms_complete': lambda cfg: flags_manager.mark_rms_complete(*_NIGHT, cfg),
    'mark_eon_done': lambda cfg: flags_manager.mark_eon_done(*_NIGHT, cfg),
    'mark_morning_done': lambda cfg: flags_manager.mark_morning_done(*_NIGHT, cfg),
    'mark_chunk_uploaded': lambda cfg: flags_manager.mark_chunk_uploaded(*_NIGHT, 'x_color.mkv', cfg),
    'unmark_chunk_uploaded': lambda cfg: flags_manager.unmark_chunk_uploaded(*_NIGHT, _LOCKED, cfg),
    'mark_chunk_stack_uploaded': lambda cfg: flags_manager.mark_chunk_stack_uploaded(
        *_NIGHT, 'x_color.mkv', cfg),
    'mark_timelapse_uploaded': lambda cfg: flags_manager.mark_timelapse_uploaded(*_NIGHT, cfg),
    'mark_night_stack_uploaded': lambda cfg: flags_manager.mark_night_stack_uploaded(*_NIGHT, cfg),
    'mark_product_uploaded': lambda cfg: flags_manager.mark_product_uploaded(
        *_NIGHT, 'timelapse', 1, 2, cfg),
    'mark_uploaded_batch': lambda cfg: flags_manager.mark_uploaded_batch(
        *_NIGHT, [(flags_manager.MARK_CHUNK, 'x_color.mkv', True)], cfg),
    'mark_rms_file_uploaded': lambda cfg: flags_manager.mark_rms_file_uploaded(
        *_NIGHT, 'FTPdetectinfo.txt', [1, 2, 'a'], cfg),
    'mark_ffthumb_uploaded': lambda cfg: flags_manager.mark_ffthumb_uploaded(*_NIGHT, 'ff.webp', cfg),
    'mark_timelapse_done': lambda cfg: flags_manager.mark_timelapse_done(*_NIGHT, cfg),
    'mark_timelapse_deferred': lambda cfg: flags_manager.mark_timelapse_deferred(*_NIGHT, cfg),
    'edit_state': lambda cfg: flags_manager.edit_state(
        *_NIGHT, cfg, lambda s: s.__setitem__('x', 1)),
    'update_chunk': lambda cfg: flags_manager.update_chunk(
        *_NIGHT, 'x_color.mkv', cfg, {'reencoded': True}),
}


class TestUnreadableStateIsNeverOverwritten:
    """gmnro01 (Ghirdoveni), 2026-11-09: its USB disk returned EIO reading a
    state.json. load() answered every read error with an empty state, and the
    next writer saved that empty state plus its one change, wiping the
    night's locks, upload flags and morning_done for good."""

    @pytest.fixture
    def night(self, cfg):
        """A settled night: a fireball lock, an upload, morning_done."""
        flags_manager.lock_chunk(*_NIGHT, _LOCKED, {'lock_type': 'manual'}, cfg)
        flags_manager.mark_chunk_uploaded(*_NIGHT, _LOCKED, cfg)
        flags_manager.mark_morning_done(*_NIGHT, cfg)
        path = flags_manager._state_path(*_NIGHT, cfg)
        return path, path.read_bytes()

    @pytest.fixture
    def eio(self, monkeypatch):
        """Reads of state.json fail with EIO while the returned flag is set."""
        import errno

        failing = {'on': True}
        real = Path.read_bytes
        real_text = Path.read_text

        def read_bytes(self):
            if failing['on'] and self.name == 'state.json':
                raise OSError(errno.EIO, 'Input/output error')
            return real(self)

        def read_text(self, *a, **kw):
            if failing['on'] and self.name == 'state.json':
                raise OSError(errno.EIO, 'Input/output error')
            return real_text(self, *a, **kw)

        monkeypatch.setattr(Path, 'read_bytes', read_bytes)
        monkeypatch.setattr(Path, 'read_text', read_text)
        return failing

    @pytest.mark.parametrize('writer', sorted(_WRITERS))
    def test_a_read_error_leaves_the_file_alone(self, cfg, night, eio, writer):
        path, before = night
        result = _WRITERS[writer](cfg)
        assert result in (None, False)
        eio['on'] = False
        assert path.read_bytes() == before
        state = flags_manager.load(*_NIGHT, cfg)
        assert state['chunks'][_LOCKED]['lock'] == {'lock_type': 'manual'}
        assert state['morning_done'] is True

    def test_writes_resume_once_the_file_reads_again(self, cfg, night, eio):
        assert flags_manager.mark_rms_complete(*_NIGHT, cfg) is False
        eio['on'] = False
        assert flags_manager.mark_rms_complete(*_NIGHT, cfg) is True
        state = flags_manager.load(*_NIGHT, cfg)
        assert state['rms_complete'] is True
        assert state['chunks'][_LOCKED]['uploaded'] is True

    def test_readers_still_get_an_empty_state(self, cfg, night, eio):
        state = flags_manager.load(*_NIGHT, cfg)
        assert state['chunks'] == {}
        assert flags_manager.is_unreadable(state)
        assert flags_manager.night_unreadable(*_NIGHT, cfg)
        eio['on'] = False
        assert not flags_manager.night_unreadable(*_NIGHT, cfg)

    def test_a_missing_file_is_not_unreadable(self, cfg):
        assert not flags_manager.night_unreadable(*_NIGHT, cfg)
        assert not flags_manager.is_unreadable(flags_manager.load(*_NIGHT, cfg))
        flags_manager.mark_morning_done(*_NIGHT, cfg)
        assert flags_manager.is_morning_done(*_NIGHT, cfg)

    def test_scan_cache_keeps_the_unreadable_mark(self, cfg, night, eio):
        with flags_manager.scan_cache():
            assert flags_manager.night_unreadable(*_NIGHT, cfg)
            assert flags_manager.night_unreadable(*_NIGHT, cfg)


class TestCorruptStateIsMovedAside:
    """A state.json that reads but does not parse cannot be repaired; a writer
    keeps it as state.json.bad-<UTC time> and starts the night again, so the
    night's flags work again and the damaged file survives for a look."""

    @pytest.mark.parametrize('content', [b'{{{not valid json', b'\x00\xff\xfe\x80', b'[]'],
                             ids=['json', 'binary', 'not-an-object'])
    def test_writer_moves_it_aside_and_starts_fresh(self, cfg, content, caplog):
        path = flags_manager._state_path(*_NIGHT, cfg)
        path.parent.mkdir(parents=True)
        path.write_bytes(content)
        assert flags_manager.night_unreadable(*_NIGHT, cfg)

        with caplog.at_level('ERROR', logger=flags_manager.logger.name):
            assert flags_manager.mark_morning_done(*_NIGHT, cfg) is True

        bad = list(path.parent.glob('state.json.bad-*'))
        assert len(bad) == 1
        assert bad[0].read_bytes() == content
        assert flags_manager.load(*_NIGHT, cfg)['morning_done'] is True
        assert not flags_manager.night_unreadable(*_NIGHT, cfg)
        assert any(bad[0].name in r.getMessage() for r in caplog.records)

    def test_no_write_when_it_cannot_be_moved_aside(self, cfg, monkeypatch):
        path = flags_manager._state_path(*_NIGHT, cfg)
        path.parent.mkdir(parents=True)
        path.write_bytes(b'{{{')

        def no_rename(src, dst):
            raise OSError(30, 'Read-only file system')

        monkeypatch.setattr(flags_manager.os, 'replace', no_rename)
        assert flags_manager.mark_morning_done(*_NIGHT, cfg) is False
        monkeypatch.undo()
        assert path.read_bytes() == b'{{{'
