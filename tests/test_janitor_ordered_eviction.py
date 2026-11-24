"""Ordered disk-pressure eviction in the station janitor.

This is fleet-wide data-deletion logic, so the tests pin the behaviour the
operator specified exactly:

  * Base retention keeps colour video up to 2 days (48h).
  * Under disk pressure, evict in a strict order:
      1. NON-DETECTION (unlocked) chunks OLDER THAN 24h, oldest-first.
      2. If still over the limit, ANYTHING older than 24h (locked detections and
         stitched splices included), oldest-first.
  * HARD INVARIANT: nothing <=24h old is ever deleted, protecting the whole
    current night (detections AND non-detections).
  * Within a phase the candidates go archived-first: sorted by
    ``(not uploaded, age)``, so a chunk the storagebox already holds is spent
    before an older chunk that exists nowhere else, and evicting an
    un-uploaded detection is announced at ERROR as data loss.

Plus the accurate-fullness fix: the janitor's percentage must match ``df`` for
the non-root pipeline user (statvfs f_bavail), not the reserve-inclusive total,
so a disk that is full for the capture user actually reads as full and the
eviction can fire before ENOSPC.

Disk pressure is simulated by monkeypatching ``_fs_stats`` with a model that
recomputes free space from the surviving files on disk, so the janitor's
mid-loop re-checks see the disk relieve exactly as it would in production.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import janitor_storage_watchdog as jsw


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

STATION = "RO000H"
CHUNK_BYTES = 1_000_000_000  # 1 GB per synthetic chunk (content is tiny; size is modelled)


def _mkv_name(dt: datetime, end: datetime | None = None) -> str:
    """Colour-chunk filename for a start time (and optional splice end)."""
    base = f"{STATION}_{dt.strftime('%Y%m%d')}_{dt.strftime('%H%M%S')}"
    if end is not None:
        base += f"_{end.strftime('%H%M%S')}"
    return base + "_color.mkv"


class FakeDisk:
    """Models a filesystem whose fullness depends on the surviving chunks.

    total_gb / per-chunk GB let the tests express pressure as "over the limit
    until N chunks are gone". The janitor's _fs_stats is patched to read this,
    so its mid-loop re-checks relieve as files are unlinked — exactly the
    production feedback loop.
    """

    def __init__(self, capture: Path, total_gb: float, chunk_gb: float, floor_gb: float):
        self.capture = capture
        self.total_gb = total_gb
        self.chunk_gb = chunk_gb
        self.floor_gb = floor_gb  # avail below which the disk is "over limit"

    def _used_gb(self) -> float:
        n = sum(1 for _ in self.capture.rglob("*.mkv"))
        return n * self.chunk_gb

    def stats(self, _path):
        used = self._used_gb()
        avail = max(0.0, self.total_gb - used)
        pct = int(used * 100 // self.total_gb) if self.total_gb else 100
        return pct, avail


def make_janitor(tmp_path: Path, disk_cfg: dict | None = None) -> jsw.Janitor:
    capture = tmp_path / "colorcap"
    capture.mkdir(parents=True, exist_ok=True)
    (capture / STATION).mkdir(parents=True, exist_ok=True)
    cfg = {
        "videocapture_path": str(capture),
        "log_path": str(tmp_path / "logs"),
        "stations": {STATION: {"rms_data_path": str(tmp_path / "rms")}},
        "archive": {"enabled": True},
        "disk": disk_cfg or {"warn_pct": 85, "min_free_gb": 0},
    }
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(cfg))
    # Each Janitor() adds handlers to the module logger; clear first so repeated
    # instances across tests don't stack them and multiply log output.
    for h in list(jsw.logger.handlers):
        jsw.logger.removeHandler(h)
    j = jsw.Janitor(str(cfg_path))
    # Never actually spawn systemctl in tests.
    j._trigger_uploader = lambda: None  # type: ignore[method-assign]
    return j


def write_chunk(
    j: jsw.Janitor, start: datetime, *, locked: bool, end: datetime | None = None,
    uploaded: bool = False,
) -> Path:
    """Create a colour chunk file at the given UTC start, optionally locked.

    ``uploaded=True`` marks it as already on the storagebox, via the same
    state.json flag archive_upload sets in production.
    """
    date_str = start.strftime("%Y%m%d")
    night_dir = j.capture_path / STATION / date_str / "videos"
    night_dir.mkdir(parents=True, exist_ok=True)
    f = night_dir / _mkv_name(start, end)
    f.write_bytes(b"x")
    if locked:
        # Use the state.json lock path so it matches production lock detection.
        import flags_manager
        flags_manager.lock_chunk(
            STATION, date_str, f.name,
            {"lock_type": "detection", "detection_time": start.strftime("%Y%m%d_%H%M%S")},
            j.cfg,
        )
    if uploaded:
        import flags_manager
        flags_manager.mark_chunk_uploaded(STATION, date_str, f.name, j.cfg)
    return f


def surviving(j: jsw.Janitor) -> set[str]:
    return {p.name for p in j.capture_path.rglob("*.mkv")}


# ===========================================================================
# Accurate-fullness detection (statvfs f_bavail vs the root reserve)
# ===========================================================================

class TestAccurateFullness:
    def test_df_pct_matches_available_not_reserve(self, monkeypatch):
        """At a true df 100% (f_bavail == 0) the metric must read 100%, not the
        ~95% the reserve-inclusive ``used*100//total`` produced — the wedge that
        stopped gmnro16's janitor from ever firing on a full 29 GB disk."""
        frsize = 4096
        f_blocks = 7_000_000                 # ~28.7 GB, like the colourcap USB
        reserve = int(f_blocks * 0.05)       # ext4 default 5% root reserve

        def fake_statvfs(_p, f_bavail):
            return os.statvfs_result((
                frsize, frsize, f_blocks,
                f_bavail + reserve,          # f_bfree = nonroot avail + reserve
                f_bavail,                    # f_bavail = nonroot available
                0, 0, 0, 0, 255,
            ))

        # Disk full for the pipeline user: f_bavail == 0.
        monkeypatch.setattr(jsw.os, "statvfs", lambda p: fake_statvfs(p, 0))
        pct, avail_gb = jsw._fs_stats(Path("/"))
        assert pct == 100
        assert avail_gb == pytest.approx(0.0)

        # The naive reserve-inclusive metric would have under-read to ~95%.
        naive = int((f_blocks - reserve) * 100 // f_blocks)
        assert naive < 96  # demonstrates the old bug it replaces

    def test_df_pct_tracks_available(self, monkeypatch):
        frsize = 4096
        f_blocks = 10_000_000
        reserve = int(f_blocks * 0.05)
        # Half the user-usable space consumed.
        usable = f_blocks - reserve
        f_bavail = usable // 2

        def fake(_p):
            return os.statvfs_result((
                frsize, frsize, f_blocks,
                f_bavail + reserve, f_bavail, 0, 0, 0, 0, 255,
            ))

        monkeypatch.setattr(jsw.os, "statvfs", fake)
        pct, avail_gb = jsw._fs_stats(Path("/"))
        assert pct == pytest.approx(50, abs=1)
        assert avail_gb == pytest.approx(f_bavail * frsize / 1e9, rel=1e-6)


# ===========================================================================
# Filename timestamp / boundary math (UTC)
# ===========================================================================

class TestBoundaryMath:
    def test_regular_chunk_start(self):
        assert jsw._chunk_start_dt("RO000H_20260322_210000_color.mkv") == \
            datetime(2026, 3, 22, 21, 0, 0, tzinfo=timezone.utc)

    def test_splice_uses_start_not_end(self):
        # <CAM>_<date>_<start>_<end>_color.mkv — age is measured from the start.
        assert jsw._chunk_start_dt("RO000P_20260724_002833_002853_color.mkv") == \
            datetime(2026, 7, 24, 0, 28, 33, tzinfo=timezone.utc)

    def test_non_matching_returns_none(self):
        assert jsw._chunk_start_dt("state.json") is None
        assert jsw._chunk_start_dt("RO000H_stack.webp") is None

    def test_file_age_falls_back_to_mtime(self, tmp_path):
        f = tmp_path / "weird_name.mkv"
        f.write_bytes(b"x")
        past = datetime(2020, 1, 1, tzinfo=timezone.utc).timestamp()
        os.utime(f, (past, past))
        dt = jsw._file_age_dt(f)
        assert dt is not None and dt.year == 2020


# ===========================================================================
# The hard 24h invariant
# ===========================================================================

class TestHardInvariant:
    def test_never_deletes_anything_within_24h(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85, "min_free_gb": 0})
        now = datetime.now(timezone.utc)
        # A fresh non-detection and a fresh detection, both <24h old.
        fresh_bulk = write_chunk(j, now - timedelta(hours=2), locked=False)
        fresh_det = write_chunk(j, now - timedelta(hours=10), locked=True)
        # Force the disk to be permanently over the limit.
        monkeypatch.setattr(jsw, "_fs_stats", lambda _p: (99, 0.0))
        j._run_disk_check()
        # Both must survive — the current night is untouchable.
        assert fresh_bulk.exists()
        assert fresh_det.exists()

    def test_protect_window_is_configurable(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85, "protect_hours": 12, "min_free_gb": 0})
        now = datetime.now(timezone.utc)
        # 18h-old non-detection: outside a 12h window, so now eligible.
        old = write_chunk(j, now - timedelta(hours=18), locked=False)
        young = write_chunk(j, now - timedelta(hours=6), locked=False)
        disk = FakeDisk(j.capture_path, total_gb=1.0, chunk_gb=1.0, floor_gb=0.0)
        # over-limit while >=1 chunk survives (avail 0 with total 1GB & 1GB/chunk)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j._run_disk_check()
        assert not old.exists()   # 18h old, deleted under a 12h window
        assert young.exists()     # 6h old, always protected


# ===========================================================================
# Eviction order (the operator spec)
# ===========================================================================

class TestEvictionOrder:
    def _two_nights(self, j: jsw.Janitor, now: datetime) -> dict[str, Path]:
        """Previous night (>24h) bulk + detections, plus current-night clips."""
        prev = now - timedelta(hours=30)   # yesterday, >24h old
        older = now - timedelta(hours=40)  # older still
        cur = now - timedelta(hours=4)     # tonight, <24h
        return {
            "older_bulk": write_chunk(j, older, locked=False),
            "prev_bulk": write_chunk(j, prev, locked=False),
            "prev_det": write_chunk(j, prev + timedelta(minutes=5), locked=True),
            "cur_bulk": write_chunk(j, cur, locked=False),
            "cur_det": write_chunk(j, cur + timedelta(minutes=5), locked=True),
        }

    def test_phase1_deletes_non_detections_oldest_first(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85, "min_free_gb": 0})
        now = datetime.now(timezone.utc)
        files = self._two_nights(j, now)
        # 5 files on a 10 GB disk -> avail 5 GB. Floor 5.5 -> over by 0.5 GB, so
        # exactly ONE deletion (the oldest non-detection) relieves it.
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=5.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 5.5
        j._run_disk_check()
        # The single oldest NON-DETECTION (older_bulk) goes first; nothing else.
        assert not files["older_bulk"].exists()
        assert files["prev_bulk"].exists()
        assert files["prev_det"].exists()
        assert files["cur_bulk"].exists()
        assert files["cur_det"].exists()

    def test_phase1_exhausts_all_old_non_detections_before_phase2(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        files = self._two_nights(j, now)
        # 5 files -> avail 5 GB. Floor 6.5 needs BOTH old non-detections gone
        # (avail 5 -> 7), but not the detection.
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=6.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 6.5
        j._run_disk_check()
        # Both old NON-DETECTIONS gone (oldest-first); detections + tonight kept.
        assert not files["older_bulk"].exists()
        assert not files["prev_bulk"].exists()
        assert files["prev_det"].exists()   # detection survives phase 1
        assert files["cur_bulk"].exists()
        assert files["cur_det"].exists()

    def test_phase2_deletes_locked_when_non_detections_not_enough(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        files = self._two_nights(j, now)
        # 5 files -> avail 5 GB. Floor 7.5 needs 3 deletions: both old bulk
        # (phase 1) + the old detection (phase 2). Tonight's two (<24h) stay.
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=7.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 7.5
        j._run_disk_check()
        assert not files["older_bulk"].exists()
        assert not files["prev_bulk"].exists()
        assert not files["prev_det"].exists()   # locked detection deleted in phase 2
        assert files["cur_bulk"].exists()        # <24h always safe
        assert files["cur_det"].exists()

    def test_splice_treated_as_old_clip_by_start_time(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        prev = now - timedelta(hours=30)
        # A stitched splice from last night, locked (detection). Phase 2 territory.
        splice = write_chunk(j, prev, locked=True, end=prev + timedelta(seconds=20))
        bulk = write_chunk(j, prev - timedelta(hours=1), locked=False)
        # 2 files -> avail 8 GB. Floor 8.5 -> one deletion suffices; the unlocked
        # bulk goes in phase 1 and the splice is never reached.
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=8.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 8.5
        j._run_disk_check()
        # bulk (unlocked) goes first; splice only if still over — here freeing
        # one is enough, so the splice survives, proving order not name.
        assert not bulk.exists()
        assert splice.exists()


# ===========================================================================
# Upload-state ordering — spend the archived copy, not the only copy
# ===========================================================================

class TestUploadStateOrdering:
    """Age alone used to pick the victim, so sustained pressure could delete
    the only copy of an un-uploaded detection while an older chunk that was
    safely on the storagebox survived. Both deletions free the same bytes;
    only the surviving copy differs. Order is now ``(not uploaded, age)``."""

    def test_uploaded_chunk_goes_before_an_older_unuploaded_one(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        orphan = write_chunk(j, now - timedelta(hours=40), locked=False, uploaded=False)
        archived = write_chunk(j, now - timedelta(hours=30), locked=False, uploaded=True)
        # 2 files -> avail 8 GB. Floor 8.5 -> exactly one deletion relieves it.
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=8.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 8.5
        j._run_disk_check()
        assert not archived.exists(), "the storagebox copy should have been spent first"
        assert orphan.exists(), "deleted the only copy while an archived chunk survived"

    def test_uploaded_candidates_are_still_oldest_first(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        uploaded = {
            h: write_chunk(j, now - timedelta(hours=h), locked=False, uploaded=True)
            for h in (30, 40, 50)
        }
        orphan = write_chunk(j, now - timedelta(hours=60), locked=False, uploaded=False)
        # 4 files -> avail 6 GB. Floor 7.5 -> two deletions (avail 6 -> 8).
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=7.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 7.5
        j._run_disk_check()
        assert not uploaded[50].exists() and not uploaded[40].exists()
        assert uploaded[30].exists(), "took a third uploaded chunk after relief"
        assert orphan.exists(), "the 60h orphan is the oldest but the only copy"

    def test_phase2_spends_the_uploaded_detection_first(self, tmp_path, monkeypatch):
        """Two old detections, one archived: the archived one is the victim
        even though it is the NEWER of the pair."""
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        orphan_det = write_chunk(j, now - timedelta(hours=40), locked=True, uploaded=False)
        archived_det = write_chunk(j, now - timedelta(hours=30), locked=True, uploaded=True)
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=8.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 8.5
        j._run_disk_check()
        assert not archived_det.exists()
        assert orphan_det.exists(), "lost the only copy of a meteor to save an archived one"

    def test_phase_order_still_beats_upload_state(self, tmp_path, monkeypatch):
        """Upload-awareness reorders WITHIN a phase; it must not collapse the
        phases. An un-uploaded non-detection is still taken before any
        detection, archived or not."""
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        bulk = write_chunk(j, now - timedelta(hours=26), locked=False, uploaded=False)
        archived_det = write_chunk(j, now - timedelta(hours=50), locked=True, uploaded=True)
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=8.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 8.5
        j._run_disk_check()
        assert not bulk.exists(), "phase 1 must still exhaust non-detections first"
        assert archived_det.exists()

    def test_pure_age_order_survives_when_nothing_is_uploaded(self, tmp_path, monkeypatch):
        """Guard for the pinned behaviour above: with no upload flags set the
        sort key ties on the first term and the legacy oldest-first order is
        exactly what remains."""
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        olds = [
            write_chunk(j, now - timedelta(hours=30 + i), locked=False)
            for i in range(4)
        ]
        disk = FakeDisk(j.capture_path, total_gb=6.0, chunk_gb=1.0, floor_gb=3.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 3.5
        j._run_disk_check()
        assert not olds[3].exists() and not olds[2].exists()
        assert olds[0].exists() and olds[1].exists()


# ===========================================================================
# Data-loss announcement — the ERROR line fleet alerting keys on
# ===========================================================================

class TestDataLossLine:
    def _force_one_eviction(self, j, monkeypatch):
        disk = FakeDisk(j.capture_path, total_gb=10.0, chunk_gb=1.0, floor_gb=9.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 9.5

    def test_unuploaded_detection_eviction_is_an_error(self, tmp_path, monkeypatch, caplog):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        victim = write_chunk(j, now - timedelta(hours=30), locked=True, uploaded=False)
        self._force_one_eviction(j, monkeypatch)
        with caplog.at_level(logging.DEBUG, logger="atium"):
            j._run_disk_check()
        assert not victim.exists()
        hits = [
            r for r in caplog.records
            if jsw.DATA_LOSS_MARKER in r.getMessage()
        ]
        assert hits, f"no {jsw.DATA_LOSS_MARKER} line for an irreplaceable deletion"
        assert all(r.levelno >= logging.ERROR for r in hits), (
            "data loss must be announced at ERROR, not buried at WARNING"
        )
        assert victim.name in hits[0].getMessage(), "the line must name the lost chunk"

    def test_uploaded_detection_eviction_is_not_data_loss(self, tmp_path, monkeypatch, caplog):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        victim = write_chunk(j, now - timedelta(hours=30), locked=True, uploaded=True)
        self._force_one_eviction(j, monkeypatch)
        with caplog.at_level(logging.DEBUG, logger="atium"):
            j._run_disk_check()
        assert not victim.exists()
        assert not [
            r for r in caplog.records if jsw.DATA_LOSS_MARKER in r.getMessage()
        ], "the storagebox still has this clip — crying data loss would train alert fatigue"

    def test_unlocked_eviction_is_not_data_loss(self, tmp_path, monkeypatch, caplog):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        victim = write_chunk(j, now - timedelta(hours=30), locked=False, uploaded=False)
        self._force_one_eviction(j, monkeypatch)
        with caplog.at_level(logging.DEBUG, logger="atium"):
            j._run_disk_check()
        assert not victim.exists()
        assert not [
            r for r in caplog.records if jsw.DATA_LOSS_MARKER in r.getMessage()
        ], "bulk sky with no detection on it is not a lost meteor"

    def test_marker_is_a_stable_greppable_token(self):
        """Fleet alerting greps for this string; changing it silently breaks
        the alert rather than the build, so pin it here."""
        assert jsw.DATA_LOSS_MARKER == "ROVIMEN-DATA-LOSS"


# ===========================================================================
# Lock / detection awareness
# ===========================================================================

class TestLockAwareness:
    def test_locked_state_json_recognised(self, tmp_path):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        f = write_chunk(j, now - timedelta(hours=30), locked=True)
        chunks = j._collect_chunks()
        assert len(chunks) == 1
        _dt, locked, _sid, _date, path = chunks[0]
        assert locked is True and path == f

    def test_unlocked_recognised(self, tmp_path):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        write_chunk(j, now - timedelta(hours=30), locked=False)
        _dt, locked, *_ = j._collect_chunks()[0]
        assert locked is False


# ===========================================================================
# Idempotency + no-op safety
# ===========================================================================

class TestIdempotency:
    def test_second_run_is_noop_when_under_limit(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        files = TestEvictionOrder()._two_nights(j, now)
        disk = FakeDisk(j.capture_path, total_gb=5.0, chunk_gb=1.0, floor_gb=1.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 1.5

        j._run_disk_check()
        after_first = surviving(j)
        j._run_disk_check()          # disk now under limit -> must change nothing
        after_second = surviving(j)
        assert after_first == after_second

    def test_no_deletion_when_not_over_limit(self, tmp_path, monkeypatch):
        j = make_janitor(tmp_path, {"warn_pct": 85, "min_free_gb": 0})
        now = datetime.now(timezone.utc)
        old = write_chunk(j, now - timedelta(hours=40), locked=False)
        # Plenty of space: 20% used, far under warn.
        monkeypatch.setattr(jsw, "_fs_stats", lambda _p: (20, 100.0))
        j._run_disk_check()
        assert old.exists()

    def test_relieves_only_to_the_floor(self, tmp_path, monkeypatch):
        """Eviction stops the instant the disk is back under the limit — it does
        not keep deleting old clips it is allowed to touch."""
        j = make_janitor(tmp_path, {"warn_pct": 85})
        now = datetime.now(timezone.utc)
        # Four old non-detections available to delete.
        olds = [
            write_chunk(j, now - timedelta(hours=30 + i), locked=False)
            for i in range(4)
        ]
        disk = FakeDisk(j.capture_path, total_gb=6.0, chunk_gb=1.0, floor_gb=3.5)
        monkeypatch.setattr(jsw, "_fs_stats", disk.stats)
        j.min_free_gb = 3.5
        j._run_disk_check()
        # Start avail = 6 - 4 = 2 GB (< 3.5 -> over). Delete oldest until avail
        # >= 3.5 -> after deleting 2 (avail 4). So exactly 2 of 4 remain.
        remaining = [f for f in olds if f.exists()]
        assert len(remaining) == 2
        # olds[i] has start now-(30+i)h, so olds[3] is the OLDEST. Oldest-first
        # deletes olds[3] and olds[2]; the two youngest-of-the-old survive.
        assert olds[3].exists() is False and olds[2].exists() is False
        assert olds[0].exists() and olds[1].exists()
