"""The janitor keeps the free space RMS needs on a disk it shares with colour.

gmnro22 (Ferigile): 232 GB, three cameras, ~46 GB of colour per camera-night.
Every pass protected the night being recorded, so free space fell from 27 GB
at 02:40 to 0.9 GB at 04:20 UTC each clear night of 2026-11-09..12, and RMS,
short of its 20 GB "Need ... for next night", deleted all of its own data
including the unprocessed night. The RMS floor gives up non-detection colour
of any age (tonight's included) before that happens; detections stay.

Free space is modelled from the surviving chunk files (1 GB each), so the
janitor's re-checks see the disk relieve as it deletes.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import flags_manager
import janitor_storage_watchdog as jsw

CAM = "RO001T"
GB = 1.0


class FakeDisk:
    def __init__(self, capture: Path, total_gb: float, extra_free: float = 0.0):
        self.capture = capture
        self.total_gb = total_gb
        self.extra_free = extra_free

    def stats(self, _path):
        used = sum(1 for _ in self.capture.rglob("*.mkv")) * GB
        avail = max(0.0, self.total_gb - used + self.extra_free)
        return int((self.total_gb - avail) * 100 // self.total_gb), avail


def make_janitor(tmp_path: Path, need_gib: float | None = 20.03, disk: dict | None = None,
                 rms_elsewhere: bool = False) -> jsw.Janitor:
    capture = tmp_path / "color_capture"
    (capture / CAM).mkdir(parents=True)
    rms = tmp_path / "RMS_data" / "cam3"
    (rms / "logs").mkdir(parents=True)
    (rms / "CapturedFiles").mkdir()
    if need_gib is not None:
        (rms / "logs" / f"log_{CAM}_20261111_134531_001.log").write_text(
            f"2026/11/11 15:49:53-INFO-DeleteOldObservations-line:909 - "
            f"Need {need_gib} GB for next night\n")
    cfg = {
        "videocapture_path": str(capture),
        "log_path": str(tmp_path / "logs"),
        "stations": {CAM: {"rms_data_path": str(rms)}},
        "archive": {"enabled": True},
        "disk": disk or {"warn_pct": 85, "min_free_gb": 0},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(cfg))
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
    j = jsw.Janitor(str(path))
    j._trigger_uploader = lambda: None  # type: ignore[method-assign]
    if rms_elsewhere:
        j.rms_on_capture_fs = False
    return j


def chunk(j: jsw.Janitor, start: datetime, *, locked: bool = False) -> Path:
    night = start.strftime("%Y%m%d")
    d = j.capture_path / CAM / night / "videos"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{CAM}_{night}_{start:%H%M%S}_color.mkv"
    f.write_bytes(b"x")
    if locked:
        flags_manager.lock_chunk(CAM, night, f.name,
                                 {"lock_type": "detection",
                                  "detection_time": start.strftime("%Y%m%d_%H%M%S")}, j.cfg)
    return f


@pytest.fixture
def now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def test_floor_is_rms_need_plus_margin(tmp_path):
    j = make_janitor(tmp_path, need_gib=20.03)
    # RMS logs GiB; the floor is in GB.
    assert j._rms_floor_gb() == pytest.approx(20.03 * 1024 ** 3 / 1e9 + jsw.RMS_FLOOR_MARGIN_GB)


def test_floor_before_rms_logged_a_need(tmp_path):
    j = make_janitor(tmp_path, need_gib=None)
    assert j._rms_floor_gb() == jsw.FALLBACK_FLOOR_GB


def test_floor_can_be_set_per_station(tmp_path):
    j = make_janitor(tmp_path, disk={"warn_pct": 85, "rms_floor_gb": 40})
    assert j._rms_floor_gb() == 40


def test_no_floor_when_rms_records_on_another_disk(tmp_path, monkeypatch, now):
    j = make_janitor(tmp_path, rms_elsewhere=True)
    for i in range(10):
        chunk(j, now - timedelta(hours=5, minutes=i))
    monkeypatch.setattr(jsw, "_fs_stats", FakeDisk(j.capture_path, 12).stats)
    assert j._rms_floor_gb() is None
    assert j._evict_for_rms() == 0


def test_tonights_non_detections_go_until_rms_has_its_floor(tmp_path, monkeypatch, now):
    """The Ferigile night: everything on disk is tonight's, inside protect_hours."""
    j = make_janitor(tmp_path)
    floor = j._rms_floor_gb()                       # ~26.5 GB
    detection = chunk(j, now - timedelta(hours=16), locked=True)
    plain = [chunk(j, now - timedelta(hours=9, minutes=10 * i)) for i in range(40)]
    young = chunk(j, now - timedelta(minutes=20))
    disk = FakeDisk(j.capture_path, total_gb=62)    # 42 chunks: 20 GB free
    monkeypatch.setattr(jsw, "_fs_stats", disk.stats)

    deleted = j._evict_for_rms()

    assert disk.stats(None)[1] >= floor
    assert deleted == 7                             # 20 -> 27 GB
    assert detection.exists() and young.exists()
    gone = [p for p in plain if not p.exists()]
    # Oldest first: the seven chunks that started earliest.
    assert set(gone) == set(sorted(plain, key=lambda p: p.name)[:7])
    assert j._unrelieved is None


def test_previous_nights_colour_goes_before_tonights(tmp_path, monkeypatch, now):
    j = make_janitor(tmp_path)
    old = [chunk(j, now - timedelta(hours=30, minutes=i)) for i in range(3)]
    tonight = [chunk(j, now - timedelta(hours=5, minutes=i)) for i in range(10)]
    # 13 chunks, floor ~26.5: 2 deletions take free space from 25 to 27 GB.
    monkeypatch.setattr(jsw, "_fs_stats", FakeDisk(j.capture_path, total_gb=38).stats)
    assert j._evict_for_rms() == 2
    assert sum(p.exists() for p in old) == 1
    assert all(p.exists() for p in tonight)


def test_rms_sessions_already_at_gmn_go_before_any_colour(tmp_path, monkeypatch, now):
    j = make_janitor(tmp_path)
    plain = [chunk(j, now - timedelta(hours=5, minutes=i)) for i in range(10)]
    disk = FakeDisk(j.capture_path, total_gb=30)    # 20 GB free
    monkeypatch.setattr(jsw, "_fs_stats", disk.stats)

    def free_sent_sessions(over_limit):
        disk.extra_free = 15.0
        return 1

    monkeypatch.setattr(j, "_escalate_captured_sent", free_sent_sessions)
    assert j._evict_for_rms() == 0
    assert all(p.exists() for p in plain)


def test_detections_rms_found_tonight_are_locked_before_eviction(tmp_path, monkeypatch, now):
    j = make_janitor(tmp_path)
    meteor = chunk(j, now - timedelta(hours=8))
    rest = [chunk(j, now - timedelta(hours=7, minutes=i)) for i in range(20)]
    monkeypatch.setattr(jsw, "_fs_stats", FakeDisk(j.capture_path, total_gb=26).stats)

    def lock_pass():
        flags_manager.lock_chunk(CAM, meteor.parent.parent.name, meteor.name,
                                 {"lock_type": "detection"}, j.cfg)

    monkeypatch.setattr(j, "_lock_tonights_detections", lock_pass)
    j._evict_for_rms()
    assert meteor.exists()
    assert not rest[-1].exists()                    # the oldest unlocked went first


def test_only_detections_left_is_reported_not_taken(tmp_path, monkeypatch, now):
    j = make_janitor(tmp_path)
    locked = [chunk(j, now - timedelta(hours=5, minutes=i), locked=True) for i in range(10)]
    monkeypatch.setattr(jsw, "_fs_stats", FakeDisk(j.capture_path, total_gb=20).stats)
    assert j._evict_for_rms() == 0
    assert all(p.exists() for p in locked)
    assert j._unrelieved is not None and j._unrelieved[2] == pytest.approx(10.0)


def test_run_frees_space_for_rms_below_the_pressure_limit(tmp_path, monkeypatch, now):
    """A big disk at 80% can still hold less than RMS needs: the floor does not
    wait for warn_pct."""
    j = make_janitor(tmp_path, disk={"warn_pct": 95, "min_free_gb": 0})
    plain = [chunk(j, now - timedelta(hours=6, minutes=i)) for i in range(80)]
    disk = FakeDisk(j.capture_path, total_gb=100)   # 80% used, 20 GB free
    monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
    monkeypatch.setattr(jsw, "_disk_pct", lambda p: disk.stats(p)[0])
    assert not j._over_limit()
    j.run()
    assert disk.stats(None)[1] >= j._rms_floor_gb()
    assert sum(not p.exists() for p in plain) == 7
