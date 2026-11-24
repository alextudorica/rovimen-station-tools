"""Tests for the storage-box retention janitor.

Builds a synthetic archive tree (state.json + rms/radiants + FTPdetectinfo)
in tmp_path and exercises the keep/prune decision end-to-end. GMN lookups are
monkeypatched so the tests never touch the network.
"""

from __future__ import annotations

import gzip
import json
import random
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import storagebox_janitor as sj


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

class TestPercentile:
    def test_empty(self):
        assert sj._percentile([], 20) is None

    def test_single(self):
        assert sj._percentile([3.0], 20) == 3.0

    def test_interpolated(self):
        vals = [-2.0, 4.5, 5.0, 5.0, 5.0]
        # rank = 0.2 * 4 = 0.8 -> -2 + 0.8*(4.5 - -2) = 3.2
        assert sj._percentile(vals, 20) == pytest.approx(3.2)
        # P80 -> rank 3.2 -> 5.0 + 0.2*(5.0-5.0) = 5.0
        assert sj._percentile(vals, 80) == pytest.approx(5.0)


class TestFfTimeUtc:
    def test_valid(self):
        assert sj._ff_time_utc("FF_RO000H_20260314_023422_123_0001.fits") == \
            "2026-03-14T02:34:22"

    def test_malformed(self):
        assert sj._ff_time_utc("garbage.fits") is None
        assert sj._ff_time_utc("FF_RO000H_2026_02.fits") is None


# ---------------------------------------------------------------------------
# Synthetic archive builder
# ---------------------------------------------------------------------------

def _old_date() -> str:
    return (datetime.now(timezone.utc) - timedelta(days=400)).strftime("%Y%m%d")


def _recent_date() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


class _ArchiveBuilder:
    def __init__(self, root: Path):
        self.root = root
        # (cam, date) -> list of (radiants_row, ftp_block)
        self._rms: dict[tuple[str, str], list[tuple[str, str]]] = {}
        # (cam, date) -> state chunks dict
        self._state: dict[tuple[str, str], dict] = {}

    def add_clip(self, cam, date, hhmmss, mag, last_frame, lock_type,
                 ff_hhmmss=None, video=True, pruned=False,
                 size=10 * 1024 * 1024):
        # ff_hhmmss lets the RMS FF (detection) block start at a DIFFERENT second
        # than the color clip -- the independent-block-boundary reality that the
        # interval-containment match must handle. Defaults to aligned.
        # video=False: a locked chunk whose clip never reached the box.
        # pruned=True: a clip an earlier run pruned (marked, video gone).
        ff_ts = ff_hhmmss or hhmmss
        fname = f"{cam}_{date}_{hhmmss}_color.mkv"
        date_dir = self.root / cam / date
        meteors = date_dir / "meteors"
        meteors.mkdir(parents=True, exist_ok=True)
        if video and not pruned:
            # ~10 MB dummy video
            (meteors / fname).write_bytes(b"\0" * size)
        # stack we must never delete
        (meteors / fname.replace("_color.mkv", "_stack.webp")).write_bytes(b"webp")

        iso_d = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
        meteor_time = f"{iso_d}T{hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}.000000"
        chunk = {"lock": {"lock_type": lock_type, "meteor_time": meteor_time}}
        if pruned:
            chunk["video_pruned"] = True
        self._state.setdefault((cam, date), {})[fname] = chunk

        # radiants row: 17 comma fields; time at idx 0, shower idx 3, mag idx 14/15
        row = ["0"] * 17
        row[0] = f"{date} {hhmmss[:2]}:{hhmmss[2:4]}:{hhmmss[4:6]}"
        row[3] = "SPO"
        row[14] = str(mag)
        row[15] = str(mag)
        radiants_row = ",".join(row)

        # FTPdetectinfo block: fps 25, frames 0..last_frame -> dur = last/25.
        # Per-frame photometry (last field) carries the magnitude, so the
        # detection's peak_mag == mag -- this is the robust source the janitor
        # now reads (matched by the shared block-start token).
        ff = f"FF_{cam}_{date}_{ff_ts}_000_0001.fits"
        ftp_block = "\n".join([
            ff,
            "Recalibrated",
            f"{cam} 0001 5 25.0",
            f"0.0 0 0 0 0 0 0 0 {mag}",
            f"{float(last_frame)} 0 0 0 0 0 0 0 {mag}",
        ])
        self._rms.setdefault((cam, date), []).append((radiants_row, ftp_block))

    def write(self):
        for (cam, date), chunks in self._state.items():
            date_dir = self.root / cam / date
            (date_dir / "state.json").write_text(json.dumps({"chunks": chunks}))
            rows = self._rms.get((cam, date), [])
            rms_dir = date_dir / "rms"
            rms_dir.mkdir(parents=True, exist_ok=True)
            (rms_dir / f"{cam}_{date}_radiants.txt").write_text(
                "\n".join(r for r, _ in rows) + "\n")
            (rms_dir / f"FTPdetectinfo_{cam}_{date}.txt").write_text(
                "\n".join(b for _, b in rows) + "\n")


@pytest.fixture
def no_gmn(monkeypatch):
    monkeypatch.setattr(sj.gmn_data, "events_for_date",
                        lambda d: {"events": []})


@pytest.fixture
def archive(tmp_path, no_gmn):
    """8 clips (7 old + 1 recent), all on one camera so internal correlation
    never fires. With this population the top-20% cuts are unambiguous:
      bright (2): K1,K2 ; long (2): L1,L2 ; manual: D ; recent: R.
    Only the two plain dim+short old clips (A, E) are prune candidates."""
    b = _ArchiveBuilder(tmp_path)
    old = _old_date()
    b.add_clip("RO000H", old, "020001", mag=-3.0, last_frame=5,   lock_type="detection")  # K1 bright
    b.add_clip("RO000H", old, "020011", mag=-2.0, last_frame=5,   lock_type="detection")  # K2 bright
    b.add_clip("RO000H", old, "020021", mag=5.5,  last_frame=300, lock_type="detection")  # L1 long
    b.add_clip("RO000H", old, "020031", mag=5.6,  last_frame=250, lock_type="detection")  # L2 long
    b.add_clip("RO000H", old, "020041", mag=5.0,  last_frame=5,   lock_type="manual")     # D  manual
    b.add_clip("RO000H", old, "020051", mag=5.0,  last_frame=5,   lock_type="detection")  # A  PRUNE
    b.add_clip("RO000H", old, "020101", mag=4.8,  last_frame=7,   lock_type="detection")  # E  PRUNE
    # Recent clip: always kept regardless of how dim/short.
    b.add_clip("RO000H", _recent_date(), "030000", mag=6.0, last_frame=1, lock_type="detection")
    b.write()
    return tmp_path


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------

def _run(root, **kw):
    defaults = dict(
        archive=root, retention_days=180, bright_pct=20.0, long_pct=20.0,
        tol_s=3.0, corr_window_s=3.0, apply=False, max_deletes=0, report_path=None,
    )
    defaults.update(kw)
    return sj.run(**defaults)


def test_dry_run_selects_only_plain_old_clips(archive):
    stats = _run(archive)
    # A and E pruned; K1/K2 bright, L1/L2 long, D manual, recent kept.
    assert stats.pruned == 2
    assert stats.kept_bright == 2
    assert stats.kept_long == 2
    assert stats.kept_manual == 1
    assert stats.kept_recent == 1
    # Dry run deletes nothing.
    old = _old_date()
    assert (archive / "RO000H" / old / "meteors" /
            f"RO000H_{old}_020051_color.mkv").exists()


def test_apply_deletes_mkv_keeps_stack_and_marks_state(archive):
    stats = _run(archive, apply=True)
    assert stats.pruned == 2
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    # Pruned MKVs gone (A, E).
    assert not (md / f"RO000H_{old}_020051_color.mkv").exists()
    assert not (md / f"RO000H_{old}_020101_color.mkv").exists()
    # Kept MKVs remain.
    assert (md / f"RO000H_{old}_020001_color.mkv").exists()  # bright K1
    assert (md / f"RO000H_{old}_020021_color.mkv").exists()  # long L1
    assert (md / f"RO000H_{old}_020041_color.mkv").exists()  # manual D
    # Stacks for pruned clips are NEVER deleted.
    assert (md / f"RO000H_{old}_020051_stack.webp").exists()
    # state.json marks pruned clips.
    state = json.loads((archive / "RO000H" / old / "state.json").read_text())
    assert state["chunks"][f"RO000H_{old}_020051_color.mkv"]["video_pruned"] is True
    assert "video_pruned" not in state["chunks"][f"RO000H_{old}_020001_color.mkv"]


def test_idempotent_second_run_prunes_nothing(archive):
    _run(archive, apply=True)
    stats2 = _run(archive, apply=True)
    assert stats2.pruned == 0


def test_unreadable_state_skips_date_never_prunes(archive, tmp_path):
    old = _old_date()
    (tmp_path / "RO000H" / old / "state.json").write_text("{ not json")
    stats = _run(archive)
    assert stats.pruned == 0
    assert stats.skipped_dates >= 1


def test_max_deletes_throttle(archive):
    stats = _run(archive, apply=True, max_deletes=1)
    assert stats.pruned == 1  # capped


def test_gmn_multistation_keeps_old_dim_clip(tmp_path, monkeypatch):
    old = _old_date()
    iso = f"{old[:4]}-{old[4:6]}-{old[6:8]}"
    monkeypatch.setattr(sj.gmn_data, "events_for_date", lambda d: {
        "events": [{"stations": ["RO000H", "RO000J"],
                    "time": f"{iso}T02:00:01"}]
    })
    b = _ArchiveBuilder(tmp_path)
    b.add_clip("RO000H", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.write()
    stats = _run(tmp_path)
    assert stats.pruned == 0
    assert stats.kept_multistation == 1


def test_internal_correlation_keeps_coincident_clips(tmp_path, no_gmn):
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    # Two cameras, same second -> mutual multi-station witnesses.
    b.add_clip("RO000H", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.add_clip("RO000J", old, "020001", mag=9.0, last_frame=1, lock_type="detection")
    b.write()
    stats = _run(tmp_path)
    assert stats.pruned == 0
    assert stats.kept_multistation == 2


class TestTokenMatch:
    def test_fname_token(self):
        assert sj._fname_token("RO000H_20260719_210347_color.mkv") == "20260719_210347"
        assert sj._fname_token(
            "RO000H_20260719_200214_200234_color.mkv") == "20260719_200214"  # block start
        assert sj._fname_token("FF_RO000H_20260719_210347_000_0001.fits") == "20260719_210347"
        assert sj._fname_token("garbage.mkv") is None


def test_magnitude_resolves_from_ftp_without_radiants(tmp_path, no_gmn):
    """Magnitude resolves straight from FTPdetectinfo -- no radiants needed."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    b.add_clip("RO000H", old, "020001", mag=-3.0, last_frame=5, lock_type="detection")
    b.write()
    # Nuke radiants entirely -- the meteor-centric path never touches it.
    (tmp_path / "RO000H" / old / "rms" / f"RO000H_{old}_radiants.txt").unlink()
    stats = _run(tmp_path)
    assert stats.resolved_mag == 1            # resolved despite no radiants
    assert stats.kept_bright == 1             # -3.0 is in the brightest bucket


def test_containment_resolves_across_block_offset(tmp_path, no_gmn):
    """The whole point of the rewrite: the color clip's block starts at a
    DIFFERENT second than the RMS FF (independent pipelines). Interval
    containment must still resolve the magnitude -- the old exact-token match
    would have returned None here and lost the clip."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    # clip block starts at 02:00:00; its detection's FF block starts 7 s later.
    b.add_clip("RO000H", old, "020000", mag=-3.0, last_frame=5,
               lock_type="detection", ff_hhmmss="020007")
    b.write()
    stats = _run(tmp_path)
    assert stats.resolved_mag == 1     # containment matched across the 7 s offset
    assert stats.kept_bright == 1
    assert stats.pruned == 0


def test_multistation_mag_gate_prunes_faint_keeps_bright(tmp_path, monkeypatch):
    old = _old_date()
    iso = f"{old[:4]}-{old[4:6]}-{old[6:8]}"
    # Both clips are GMN multi-station; only brightness differs.
    monkeypatch.setattr(sj.gmn_data, "events_for_date", lambda d: {
        "events": [
            {"stations": ["RO000H", "RO000J"], "time": f"{iso}T02:00:01"},
            {"stations": ["RO000H", "RO000J"], "time": f"{iso}T02:05:01"},
        ]
    })
    b = _ArchiveBuilder(tmp_path)
    b.add_clip("RO000H", old, "020001", mag=-2.0, last_frame=5, lock_type="detection")  # bright
    b.add_clip("RO000H", old, "020501", mag=4.5,  last_frame=1, lock_type="detection")  # faint+short
    b.write()
    # Legacy behaviour: both kept as multi-station.
    assert _run(tmp_path).kept_multistation == 2
    # With the gate at mag<=1 (and bright/long keepers disabled to isolate it):
    # the faint clip is gated out of the multi-station keeper and pruned.
    stats = _run(tmp_path, multistation_max_mag=1.0, bright_pct=0.0, long_pct=0.0)
    assert stats.kept_multistation == 1
    assert stats.multistation_faint == 1
    assert stats.pruned == 1


def test_clip_without_detection_is_unmeasured_and_ages_out(tmp_path, no_gmn):
    """Meteor-centric model: multi-station status now comes from RMS detections,
    and every RMS detection carries a magnitude -- so 'multi-station but
    unmeasured' cannot occur. A locked clip that maps to NO detection (rms
    stripped) is not a confirmed meteor: unmeasured, not multi-station, so it
    ages out past retention. Its stack + metadata are still kept."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    b.add_clip("RO000H", old, "020001", mag=4.5, last_frame=1, lock_type="detection")
    b.write()
    for f in (tmp_path / "RO000H" / old / "rms").iterdir():
        f.unlink()
    stats = _run(tmp_path, multistation_max_mag=1.0)
    assert stats.resolved_mag == 0
    assert stats.kept_multistation == 0
    assert stats.pruned == 1
    # the stack is never touched
    assert (tmp_path / "RO000H" / old / "meteors" /
            f"RO000H_{old}_020001_stack.webp").exists()


# ---------------------------------------------------------------------------
# The whole clip goes: MKV, stream-copied MP4 and overlay renders
# ---------------------------------------------------------------------------

def _copies(md, stem):
    """Add the .mp4 copy and both overlay renders next to an existing MKV."""
    for suffix in (".mp4", ".details.mkv", ".details.mp4"):
        (md / f"{stem}{suffix}").write_bytes(b"\0" * 1024)


def test_apply_deletes_every_video_copy_of_a_pruned_clip(archive):
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    _copies(md, f"RO000H_{old}_020051_color")   # A: prune candidate
    _copies(md, f"RO000H_{old}_020001_color")   # K1: kept (bright)
    stats = _run(archive, apply=True)
    assert stats.pruned == 2
    for suffix in (".mkv", ".mp4", ".details.mkv", ".details.mp4"):
        assert not (md / f"RO000H_{old}_020051_color{suffix}").exists()
        assert (md / f"RO000H_{old}_020001_color{suffix}").exists()
    assert (md / f"RO000H_{old}_020051_stack.webp").exists()
    # Bytes count every file, not just the MKV.
    assert stats.pruned_bytes == 2 * 10 * 1024 * 1024 + 3 * 1024


def test_leftover_copies_of_an_earlier_prune_are_removed(archive):
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    # An earlier run removed only A's MKV and marked it pruned.
    state_path = archive / "RO000H" / old / "state.json"
    state = json.loads(state_path.read_text())
    state["chunks"][f"RO000H_{old}_020051_color.mkv"]["video_pruned"] = True
    state_path.write_text(json.dumps(state))
    (md / f"RO000H_{old}_020051_color.mkv").unlink()
    (md / f"RO000H_{old}_020051_color.mp4").write_bytes(b"\0" * 2048)

    dry = _run(archive)
    assert dry.leftovers == 1 and dry.leftover_bytes == 2048
    assert (md / f"RO000H_{old}_020051_color.mp4").exists()   # dry run deletes nothing

    stats = _run(archive, apply=True)
    assert stats.leftovers == 1
    assert not (md / f"RO000H_{old}_020051_color.mp4").exists()
    assert stats.pruned == 1                  # only E is a new decision
    assert _run(archive, apply=True).leftovers == 0


def test_manual_lock_added_after_the_prune_keeps_the_leftover(archive):
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    state_path = archive / "RO000H" / old / "state.json"
    state = json.loads(state_path.read_text())
    chunk = state["chunks"][f"RO000H_{old}_020051_color.mkv"]
    chunk["video_pruned"] = True
    chunk["lock"]["lock_type"] = "manual"
    state_path.write_text(json.dumps(state))
    (md / f"RO000H_{old}_020051_color.mkv").unlink()
    (md / f"RO000H_{old}_020051_color.mp4").write_bytes(b"\0" * 2048)
    stats = _run(archive, apply=True)
    assert stats.leftovers == 0
    assert (md / f"RO000H_{old}_020051_color.mp4").exists()


def test_clip_present_only_as_mp4_is_still_pruned(archive):
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    mkv = md / f"RO000H_{old}_020101_color.mkv"   # E: prune candidate
    mkv.rename(md / f"RO000H_{old}_020101_color.mp4")
    stats = _run(archive, apply=True)
    assert stats.pruned == 2
    assert not (md / f"RO000H_{old}_020101_color.mp4").exists()
    state = json.loads((archive / "RO000H" / old / "state.json").read_text())
    assert state["chunks"][f"RO000H_{old}_020101_color.mkv"]["video_pruned"] is True


# ---------------------------------------------------------------------------
# Lock records that survive the station's dawn sweep, and protected clips
# ---------------------------------------------------------------------------

def _sweep_locks(archive, cam, date, keep_archived=True):
    """What a station's dawn sweep + re-upload leaves on the box: lock null,
    and (after the ingest merge) the old lock as archived_lock."""
    state_path = archive / cam / date / "state.json"
    state = json.loads(state_path.read_text())
    for info in state["chunks"].values():
        if keep_archived:
            info["archived_lock"] = info["lock"]
        info["lock"] = None
    state_path.write_text(json.dumps(state))


def test_archived_lock_keeps_swept_nights_in_scope(archive):
    old = _old_date()
    _sweep_locks(archive, "RO000H", old)
    stats = _run(archive)
    assert stats.pruned == 2 and stats.kept_manual == 1


def test_swept_nights_without_a_record_need_include_unlocked(archive):
    old = _old_date()
    _sweep_locks(archive, "RO000H", old, keep_archived=False)
    assert _run(archive).pruned == 0                      # the weekly timer today
    stats = _run(archive, include_unlocked=True)
    # The manual lock is gone with the sweep, so D is judged like any clip.
    assert stats.pruned == 3 and stats.kept_manual == 0
    assert stats.per_camera == {"RO000H": [3, 3 * 10 * 1024 * 1024]}


def test_hall_of_fame_and_lock_actions_are_never_pruned(archive, tmp_path):
    old = _old_date()
    iso = f"{old[:4]}-{old[4:6]}-{old[6:]}"
    hof = tmp_path / "hof.json"
    hof.write_text(json.dumps([{"camera": "RO000H", "date": iso,
                                "filename": f"RO000H_{old}_020051_color.mkv"}]))
    log = tmp_path / "activity.log"
    log.write_text(json.dumps({"method": "POST", "path":
        f"/api/lock/gmnro03/RO000H/{old}/RO000H_{old}_020101_color.mkv"}) + "\n"
        + "not json\n")
    protected = sj.load_protected(hof, log)
    assert protected == {("RO000H", old, f"RO000H_{old}_020051_color.mkv"),
                         ("RO000H", old, f"RO000H_{old}_020101_color.mkv")}
    stats = _run(archive, apply=True, protected=protected)
    assert stats.pruned == 0 and stats.kept_protected == 2
    md = archive / "RO000H" / old / "meteors"
    assert (md / f"RO000H_{old}_020051_color.mkv").exists()


def test_include_unlocked_sees_clips_state_json_does_not_list(archive):
    old = _old_date()
    md = archive / "RO000H" / old / "meteors"
    (md / f"RO000H_{old}_020201_020221_color.mkv").write_bytes(b"\0" * 1024)
    assert _run(archive).pruned == 2
    assert _run(archive, include_unlocked=True).pruned == 3


def test_lock_actions_in_rotated_activity_logs_still_protect(archive, tmp_path_factory):
    """logrotate moves activity.log into dated segments, gzipped after the
    first rotation (deployment/vps/logrotate/rovimen-dashboard); a lock action
    in one of them protects its clip exactly like one in the live file."""
    old = _old_date()
    logs = tmp_path_factory.mktemp("logs")
    live = logs / "activity.log"
    live.write_text("")                       # just rotated: the live file is empty

    def lock_line(hhmmss: str) -> str:
        return json.dumps({"method": "POST", "path":
            f"/api/lock/gmnro03/RO000H/{old}/RO000H_{old}_{hhmmss}_color.mkv"}) + "\n"

    with gzip.open(logs / "activity.log-20260801-1754006400.gz", "wt") as fh:
        fh.write(lock_line("020051"))
    (logs / "activity.log-20260901-1756684800").write_text(lock_line("020101"))
    (logs / "activity.log-20261001-1759276800.gz").write_bytes(b"not gzip")

    protected = sj.load_protected(None, live)
    assert protected == {("RO000H", old, f"RO000H_{old}_020051_color.mkv"),
                         ("RO000H", old, f"RO000H_{old}_020101_color.mkv")}
    # With the live file gone too, the rotated segments still count.
    live.unlink()
    assert sj.load_protected(None, live) == protected
    stats = _run(archive, apply=True, protected=protected)
    assert stats.pruned == 0 and stats.kept_protected == 2


# ---------------------------------------------------------------------------
# All-time cut-offs: clips whose video is gone still count
# ---------------------------------------------------------------------------

def _mkv(root: Path, cam: str, date: str, hhmmss: str) -> Path:
    return root / cam / date / "meteors" / f"{cam}_{date}_{hhmmss}_color.mkv"


def test_second_run_keeps_the_all_time_brightest(tmp_path, no_gmn):
    """The first run keeps the brightest 20% of every clip (mag 0, 1, 2) and
    prunes the rest. Measured over the clips still on the box, the second run's
    cut-off moved to mag 0.6 and pruned mag 1 and 2, both still in the all-time
    top 20%, and each later run would have tightened it again."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    for i in range(10):
        b.add_clip("RO000H", old, f"02{i:02d}00", mag=float(i), last_frame=5,
                   lock_type="detection", size=1024)
    b.add_clip("RO000H", _recent_date(), "030000", mag=10.0, last_frame=1000,
               lock_type="detection", size=1024)
    b.write()

    first = _run(tmp_path, apply=True, long_pct=0.0)
    assert first.pruned == 7 and first.kept_bright == 3
    second = _run(tmp_path, apply=True, long_pct=0.0)
    assert second.pruned == 0 and second.kept_bright == 3
    for i in range(3):
        assert _mkv(tmp_path, "RO000H", old, f"02{i:02d}00").exists()


def test_second_run_keeps_the_all_time_longest(tmp_path, no_gmn):
    """Same ratchet on duration: 3.2, 3.6 and 4.0 s are the longest 20%."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    for i in range(10):
        b.add_clip("RO000H", old, f"02{i:02d}00", mag=5.0, last_frame=10 * (i + 1),
                   lock_type="detection", size=1024)
    b.add_clip("RO000H", _recent_date(), "030000", mag=-10.0, last_frame=1,
               lock_type="detection", size=1024)
    b.write()

    first = _run(tmp_path, apply=True, bright_pct=0.0)
    assert first.pruned == 7 and first.kept_long == 3
    second = _run(tmp_path, apply=True, bright_pct=0.0)
    assert second.pruned == 0 and second.kept_long == 3
    for i in (7, 8, 9):
        assert _mkv(tmp_path, "RO000H", old, f"02{i:02d}00").exists()


def test_clips_that_never_reached_the_box_count_toward_the_cut_offs(tmp_path, no_gmn):
    """A locked chunk whose clip is not on the box still has its detection in
    rms/, which is never pruned, so it ranks among all clips ever recorded."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    for i in range(15):
        b.add_clip("RO000H", old, f"02{i:02d}00", mag=float(i), last_frame=5,
                   lock_type="detection", video=i < 5, size=1024)
    b.add_clip("RO000H", _recent_date(), "030000", mag=20.0, last_frame=1000,
               lock_type="detection", size=1024)
    b.write()

    stats = _run(tmp_path, long_pct=0.0)
    # Over all 16 measured clips the brightest 20% are mag <= 3; over the six
    # with video it would be mag <= 1, pruning mag 2 and 3 as well.
    assert stats.kept_bright == 4
    assert stats.pruned == 1
    assert stats.scanned_clips == 6           # only clips with video are judged


def test_measure_only_clips_never_tighten_a_cut_off(tmp_path, no_gmn):
    """Ten fireballs whose clips never reached the box would pull the all-time
    brightest-20% cut-off to mag -5 and leave clips the box keeps today
    unprotected. A cut-off is the more lenient of the all-time one and the one
    over clips with video (the rule before), so counting gone clips only ever
    keeps more."""
    old = _old_date()
    b = _ArchiveBuilder(tmp_path)
    for i in range(10):
        b.add_clip("RO000H", old, f"02{i:02d}00", mag=float(i), last_frame=5,
                   lock_type="detection", size=1024)
        b.add_clip("RO000H", old, f"03{i:02d}00", mag=-5.0, last_frame=5,
                   lock_type="detection", video=False)
    b.add_clip("RO000H", _recent_date(), "030000", mag=10.0, last_frame=1000,
               lock_type="detection", size=1024)
    b.write()

    stats = _run(tmp_path, long_pct=0.0)
    assert stats.kept_bright == 3 and stats.pruned == 7


def _random_archive(root: Path, rng: random.Random) -> list[dict]:
    """Two old nights and a recent one on one camera, 15 locked chunks each on
    a 20 s grid (so neighbours are common), one detection inside each chunk's
    own block. A chunk's clip is on the box, pruned by an earlier run, or never
    uploaded."""
    b = _ArchiveBuilder(root)
    now = datetime.now(timezone.utc)
    dates = [(now - timedelta(days=d)).strftime("%Y%m%d") for d in (401, 400, 0)]
    specs: list[dict] = []
    for date in dates:
        for slot in sorted(rng.sample(range(60), 15)):
            start = datetime(2026, 1, 1, 2) + timedelta(seconds=20 * slot)
            kind = rng.choice(["video", "video", "pruned", "gone"])
            spec = {
                "date": date, "hhmmss": start.strftime("%H%M%S"), "kind": kind,
                "mag": round(rng.uniform(-3.0, 6.0), 2),
                "last_frame": rng.randint(1, 300),
            }
            ff = start + timedelta(seconds=rng.randint(0, 19))
            b.add_clip("RO000H", date, spec["hhmmss"], mag=spec["mag"],
                       last_frame=spec["last_frame"],
                       lock_type="manual" if rng.random() < 0.05 else "detection",
                       ff_hhmmss=ff.strftime("%H%M%S"),
                       video=kind == "video", pruned=kind == "pruned", size=64)
            specs.append(spec)
    b.write()
    return specs


def _strip_gone_chunks(root: Path, specs: list[dict]) -> None:
    """The archive as the janitor measured it before: chunks without video
    on the box dropped from state.json (their detections stay in rms/)."""
    for spec in specs:
        if spec["kind"] == "video":
            continue
        state_path = root / "RO000H" / spec["date"] / "state.json"
        state = json.loads(state_path.read_text())
        del state["chunks"][f"RO000H_{spec['date']}_{spec['hhmmss']}_color.mkv"]
        state_path.write_text(json.dumps(state))


@pytest.mark.parametrize("seed", range(40))
def test_measuring_gone_clips_only_ever_keeps_more(tmp_path, no_gmn, seed):
    """Over random archives: (1) a run prunes a subset of what it prunes when
    it measures only the clips still on the box, which is all it measured
    before; (2) a second run never prunes a clip in the all-time top 20% by
    brightness or by duration."""
    rng = random.Random(seed)
    full, box_only = tmp_path / "full", tmp_path / "box_only"
    specs = _random_archive(full, rng)
    shutil.copytree(full, box_only)
    _strip_gone_chunks(box_only, specs)
    on_box = [s for s in specs if s["kind"] == "video"]

    def gone(root: Path) -> set[tuple[str, str]]:
        return {(s["date"], s["hhmmss"]) for s in on_box
                if not _mkv(root, "RO000H", s["date"], s["hhmmss"]).exists()}

    _run(full, apply=True)
    _run(box_only, apply=True)
    assert gone(full) <= gone(box_only)

    _run(full, apply=True)
    bright_thr = sj._percentile(sorted(s["mag"] for s in specs), 20.0)
    long_thr = sj._percentile(sorted(s["last_frame"] / 25.0 for s in specs), 80.0)
    all_time_top = {(s["date"], s["hhmmss"]) for s in on_box
                    if s["mag"] <= bright_thr or s["last_frame"] / 25.0 >= long_thr}
    assert not gone(full) & all_time_top
