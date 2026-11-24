"""platepar_store must merge through the file, not overwrite it from memory.

Each gunicorn worker imports its own copy of the module. Writing a worker's
whole in-memory store let the last writer erase every other worker's cameras
(a stale worker refreshing station B wrote station A's old pointing back over
the new one -- observed after the gmnro01 platepar refit, 2026-09-05).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import platepar_store as ps


def _pp(az: float) -> dict:
    return {"lat": 44.9, "lon": 25.7, "az_centre": az, "alt_centre": 40.0,
            "fov_h": 88.0, "fov_v": 47.0}


@pytest.fixture
def store(tmp_path: Path, monkeypatch):
    path = tmp_path / "platepar_store.json"
    monkeypatch.setattr(ps, "STORE_PATH", path)
    monkeypatch.setattr(ps, "_LOCK_PATH", path.with_suffix(".lock"))
    monkeypatch.setattr(ps, "_store", {})
    monkeypatch.setattr(ps, "_disk_mtime_ns", None)
    return path


def test_stale_worker_does_not_erase_other_workers_cameras(store: Path, monkeypatch):
    # Disk already carries the fresh RO000A (written by another worker) and RO000B.
    store.write_text(json.dumps({"RO000A": _pp(337.3), "RO000B": _pp(84.6)}))
    # This worker's memory still holds the OLD RO000A and never saw RO000B.
    monkeypatch.setattr(ps, "_store", {"RO000A": _pp(28.7)})

    ps.update_host({"RO001K": _pp(219.7)})

    on_disk = json.loads(store.read_text())
    assert on_disk["RO000A"]["az_centre"] == 337.3      # not reverted
    assert on_disk["RO000B"]["az_centre"] == 84.6       # not dropped
    assert on_disk["RO001K"]["az_centre"] == 219.7      # added
    assert ps.get_all()["RO000A"]["az_centre"] == 337.3  # memory follows the file


def test_get_all_adopts_a_write_from_another_process(store: Path, monkeypatch):
    store.write_text(json.dumps({"RO000A": _pp(1.0)}))
    ps.load()
    assert ps.get_all()["RO000A"]["az_centre"] == 1.0

    # Another worker rewrites the file with a newer pointing.
    import os, time
    store.write_text(json.dumps({"RO000A": _pp(2.0)}))
    os.utime(store, ns=(time.time_ns(), time.time_ns() + 10_000_000))

    assert ps.get_all()["RO000A"]["az_centre"] == 2.0


def test_incomplete_entries_are_skipped_and_nothing_is_written(store: Path):
    ps.update_host({"GH662S": {"error": "no platepar found"}})
    assert not store.exists()
    assert ps.get_all() == {}


def test_update_single_camera_merges_too(store: Path):
    store.write_text(json.dumps({"RO000B": _pp(84.6)}))
    ps.update("RO000A", _pp(337.3))
    on_disk = json.loads(store.read_text())
    assert set(on_disk) == {"RO000A", "RO000B"}
