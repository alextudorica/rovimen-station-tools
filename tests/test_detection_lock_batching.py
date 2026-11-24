"""The EON pass writes its lock changes to state.json in batches.

It used to rewrite state.json once per relock and once per false-positive
unlock, ~300 locked rewrites a camera-night. On gmnro09 (Sibiciu), where
state.json shares a USB HDD with capture, each cost 3-4 s (0.2 s on NVMe) and
105 of 348 took 8 s or more on 2026-10-26. Batching must not change the
resulting state, and a pass killed between batches must leave a state the next
run completes.
"""

from __future__ import annotations

import copy
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

import detection_lock
import flags_manager

STATION = "RO000H"
DATE_STR = "20260322"
START = datetime(2026, 3, 22, 21, 0, 0)
N_CHUNKS = 31

# Chunk indices: detections 10 s into these (no stitch) ...
DETECTED = (0, 3, 6, 9, 12, 15, 18, 21, 24, 27)
# ... one 19 s into chunk 29, which stitches 29 + 30 ...
BOUNDARY = 29
# ... earlier real-time locks that FTPdetectinfo does not confirm ...
FALSE_POSITIVES = (1, 4, 13)
# ... and an old-style .locked sidecar on a chunk state.json never listed.
LEGACY_SIDECAR = 7


class _Killed(BaseException):
    """Stands in for the SIGTERM color_capture sends a dawn still running."""


def _name(i: int) -> str:
    return f"{STATION}_{(START + timedelta(seconds=20 * i)).strftime('%Y%m%d_%H%M%S')}_color.mkv"


def _ff_block(t: datetime) -> str:
    return (
        f"FF_{STATION}_{t.strftime('%Y%m%d_%H%M%S')}_000\n"
        "720  576 25.00  fps\n"
        "   0.00  320.5  240.2  1000  5.5  32.1  123.4  45.6\n"
        "------------------------------\n"
    )


def _build_night(cfg: dict) -> Path:
    chunk_dir = Path(cfg["videocapture_path"]) / STATION / DATE_STR
    chunk_dir.mkdir(parents=True)
    for i in range(N_CHUNKS):
        (chunk_dir / _name(i)).write_bytes(b"")
    (chunk_dir / (_name(LEGACY_SIDECAR) + ".locked")).write_text("")

    rms = Path(cfg["stations"][STATION]["rms_data_path"])
    session = rms / "ArchivedFiles" / f"{STATION}_{DATE_STR}_210000_000000"
    session.mkdir(parents=True)
    times = [START + timedelta(seconds=20 * i + 10) for i in DETECTED]
    times.append(START + timedelta(seconds=20 * BOUNDARY + 19))
    (session / f"FTPdetectinfo_{STATION}_{DATE_STR}_210000_000000.txt").write_text(
        "------------------------------\n"
        f"Meteor Count = {len(times)}\n"
        "------------------------------\n"
        + "".join(_ff_block(t) for t in times)
    )

    for i in FALSE_POSITIVES + (DETECTED[1],):
        flags_manager.lock_chunk(STATION, DATE_STR, _name(i),
                                 {"lock_type": "detection", "detection_time": "realtime"}, cfg)
    flags_manager.mark_ready_batch(STATION, DATE_STR, [_name(i) for i in range(N_CHUNKS)], cfg)
    return chunk_dir


def _fake_stitch(clip_a: Path, clip_b: Path, station_id: str, date_str: str,
                 chunk_dir: Path, cfg: dict, **_kw) -> Path:
    out = chunk_dir / f"{station_id}_{date_str}_{clip_a.name[16:22]}_{clip_b.name[16:22]}_color.mkv"
    out.write_bytes(b"")
    return out


def _cfg_at(sample_config: dict, root: Path) -> dict:
    cfg = copy.deepcopy(sample_config)
    cfg["videocapture_path"] = str(root / "color_capture")
    cfg["stations"][STATION]["rms_data_path"] = str(root / "rms_data")
    return cfg


def _run(cfg: dict) -> bool:
    with patch.object(detection_lock, "_stitch_clips", side_effect=_fake_stitch):
        return detection_lock.process_night(STATION, DATE_STR, cfg)


def _chunks(cfg: dict) -> dict:
    return flags_manager.load(STATION, DATE_STR, cfg)["chunks"]


@pytest.fixture
def clean_state(tmp_path, sample_config) -> dict:
    """The state an uninterrupted run with one write per change leaves."""
    cfg = _cfg_at(sample_config, tmp_path / "reference")
    _build_night(cfg)
    with patch.object(detection_lock, "EON_FLUSH_EVERY", 1):
        assert _run(cfg) is True
    return _chunks(cfg)


def test_reference_run_locks_what_it_should(clean_state):
    locked = {n for n, e in clean_state.items() if e.get("lock")}
    stitched = [n for n in locked if n.count("_") == 4]
    assert len(stitched) == 1
    assert {_name(i) for i in DETECTED} <= locked
    assert not locked & {_name(i) for i in FALSE_POSITIVES}
    assert _name(BOUNDARY) not in locked


def test_batched_run_leaves_identical_state(tmp_path, sample_config, clean_state):
    cfg = _cfg_at(sample_config, tmp_path / "batched")
    _build_night(cfg)
    writes = []
    real = flags_manager.apply_lock_changes
    with patch.object(flags_manager, "apply_lock_changes",
                      side_effect=lambda *a, **kw: writes.append(kw) or real(*a, **kw)):
        assert _run(cfg) is True
    assert _chunks(cfg) == clean_state
    # Eleven relocks and three unlocks fit one write.
    assert len(writes) == 1
    assert len(writes[0]["relocks"]) == len(DETECTED) + 1
    assert sorted(writes[0]["unlocks"]) == sorted(
        _name(i) for i in FALSE_POSITIVES + (LEGACY_SIDECAR,))


def test_flushes_once_the_interval_has_passed(tmp_path, sample_config, clean_state):
    cfg = _cfg_at(sample_config, tmp_path / "slow")
    _build_night(cfg)
    ticks = iter(range(0, 10_000, 100))  # 100 s per clock read
    writes = []
    real = flags_manager.apply_lock_changes
    with patch.object(detection_lock.time, "monotonic", side_effect=lambda: next(ticks)), \
         patch.object(flags_manager, "apply_lock_changes",
                      side_effect=lambda *a, **kw: writes.append(kw) or real(*a, **kw)):
        assert _run(cfg) is True
    assert len(writes) > 1
    assert all(not w["unlocks"] for w in writes[:-1])
    assert _chunks(cfg) == clean_state


def test_killed_between_batches_leaves_a_state_the_rerun_completes(
        tmp_path, sample_config, clean_state):
    cfg = _cfg_at(sample_config, tmp_path / "killed")
    _build_night(cfg)
    real = flags_manager.apply_lock_changes
    calls = []

    def _die_on_second(*a, **kw):
        calls.append(kw)
        if len(calls) == 2:
            raise _Killed()
        return real(*a, **kw)

    with patch.object(detection_lock, "EON_FLUSH_EVERY", 3), \
         patch.object(flags_manager, "apply_lock_changes", side_effect=_die_on_second), \
         pytest.raises(_Killed):
        _run(cfg)

    after_kill = _chunks(cfg)
    # The first batch landed, and only as relocks of confirmed chunks...
    for name, info in calls[0]["relocks"].items():
        assert after_kill[name]["lock"] == info == clean_state[name]["lock"]
    # ...the false positives are still locked, nothing was unlocked...
    for i in FALSE_POSITIVES:
        assert after_kill[_name(i)]["lock"] is not None
    # ...and EON is not marked done, so dawn reruns the pass.
    assert not flags_manager.is_eon_done(STATION, DATE_STR, cfg)

    assert _run(cfg) is True
    assert _chunks(cfg) == clean_state


def test_failed_final_write_reports_no_removals(tmp_path, sample_config, caplog):
    cfg = _cfg_at(sample_config, tmp_path / "fail")
    _build_night(cfg)
    with patch.object(flags_manager, "save", return_value=False), \
         caplog.at_level("INFO", logger="detection_lock"):
        assert _run(cfg) is True
    assert "0 false positives removed" in caplog.text
    assert "Removed false-positive lock" not in caplog.text
