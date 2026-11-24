"""Archive-index revalidation by directory signature, and the persisted
timelapse catalogue.

Every worker used to relist every hot night (five ``scandir`` walks over
directories of ~1,400 entries on the sshfs Storage Box) once a minute, and
after a restart the timelapses payload scanned a camera's whole history night
by night. A night is now relisted only when its directories changed (or its
listing is too old to trust), the signatures survive a save/load round trip so
a restarted worker revalidates with ``stat`` calls, and every listed night's
timelapse files are remembered and persisted.

Correctness guards: a change is always picked up, a listing that may have
raced a same-second write is not trusted, and a trusted listing still expires.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import cache_store
from cache_store import _ArchiveIndex

CAM = "RO000M"


def _ymd(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


def _age_tree(path: Path, seconds: float = 60.0) -> None:
    """Back-date every directory/file under ``path`` so its mtimes are settled."""
    t = time.time() - seconds
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            os.utime(os.path.join(root, name), (t, t))
    os.utime(path, (t, t))


def _night(archive: Path, date: str, **subdirs: list[str]) -> Path:
    night = archive / CAM / date
    for sub, files in subdirs.items():
        d = night / sub
        d.mkdir(parents=True, exist_ok=True)
        for f in files:
            (d / f).write_bytes(b"x")
    return night


@pytest.fixture
def archive(tmp_path, monkeypatch) -> Path:
    root = tmp_path / "archive"
    root.mkdir()
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", root)
    return root


@pytest.fixture
def scans(monkeypatch) -> list[str]:
    """Record every full night listing."""
    calls: list[str] = []
    real = _ArchiveIndex._scan_night

    def counting(night_path: str):
        calls.append(os.path.basename(night_path))
        return real(night_path)

    monkeypatch.setattr(_ArchiveIndex, "_scan_night", staticmethod(counting))
    return calls


def test_unchanged_hot_night_is_not_relisted(archive, scans):
    day = _ymd(1)
    _night(archive, day, meteors=["a_color.mkv"], stacks=["s1.webp"])
    _age_tree(archive)
    idx = _ArchiveIndex()

    idx.refresh_recent(days=3)
    assert scans == [day]
    idx.refresh_recent(days=3)
    idx.refresh_recent(days=3)
    assert scans == [day], "an unchanged night must be revalidated by stat, not relisted"
    assert idx.night_files(CAM, day).meteor_files == ("a_color.mkv",)


def test_a_new_file_is_picked_up_on_the_next_refresh(archive, scans):
    day = _ymd(1)
    night = _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx.refresh_recent(days=3)

    (night / "meteors" / "b_color.mkv").write_bytes(b"x")  # bumps meteors/ mtime
    idx.refresh_recent(days=3)
    assert idx.night_files(CAM, day).meteor_files == ("a_color.mkv", "b_color.mkv")
    assert scans == [day, day]


def test_state_json_rewrite_is_picked_up(archive, scans):
    day = _ymd(1)
    night = _night(archive, day, meteors=["a_color.mkv"])
    (night / "state.json").write_text(json.dumps({"chunks": {"a": 1}}))
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx.refresh_recent(days=3)

    (night / "state.json").write_text(json.dumps({"chunks": {"a": 1, "b": 2}}))
    idx.refresh_recent(days=3)
    assert idx.night_files(CAM, day).state_chunks == {"a": 1, "b": 2}


def test_listing_that_may_have_raced_a_write_is_not_trusted(archive, scans):
    """Directories modified within the settle window of the listing could hide
    a same-second write, so the night is listed again until it settles."""
    day = _ymd(1)
    _night(archive, day, meteors=["a_color.mkv"])  # mtimes == now
    idx = _ArchiveIndex()
    idx.refresh_recent(days=3)
    idx.refresh_recent(days=3)
    assert scans == [day, day]


def test_trusted_listing_still_expires(archive, scans, monkeypatch):
    day = _ymd(1)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx.refresh_recent(days=3)
    monkeypatch.setattr(cache_store, "_SIG_MAX_AGE_S", -1.0)
    idx.refresh_recent(days=3)
    assert scans == [day, day]


def test_signatures_survive_a_restart_so_boot_revalidates_by_stat(archive, scans, tmp_path):
    day = _ymd(1)
    _night(archive, day, meteors=["a_color.mkv"], rms=["x_ff_intervals.png"])
    _age_tree(archive)
    src = _ArchiveIndex()
    src.refresh_recent(days=3)
    disk = tmp_path / "archive_index.json"
    src.save_to_disk(disk)

    restarted = _ArchiveIndex()
    assert restarted.load_from_disk(disk) >= 1
    restarted.refresh_recent(days=3)
    assert scans == [day], "a restarted worker must not relist an unchanged night"
    ni = restarted.night_files(CAM, day)
    assert ni.rms_files == frozenset({"x_ff_intervals.png"})
    assert ni.sig == src.night_files(CAM, day).sig


def test_restart_still_sees_changes_made_while_down(archive, scans, tmp_path):
    day = _ymd(1)
    night = _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    src = _ArchiveIndex()
    src.refresh_recent(days=3)
    disk = tmp_path / "archive_index.json"
    src.save_to_disk(disk)

    (night / "rms").mkdir()
    (night / "rms" / "x_ff_intervals.png").write_bytes(b"png")
    restarted = _ArchiveIndex()
    restarted.load_from_disk(disk)
    restarted.refresh_recent(days=3)
    assert restarted.night_files(CAM, day).rms_files == frozenset({"x_ff_intervals.png"})


def test_malformed_signature_on_disk_means_relist(archive, scans, tmp_path):
    day = _ymd(1)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    src = _ArchiveIndex()
    src.refresh_recent(days=3)
    disk = tmp_path / "archive_index.json"
    src.save_to_disk(disk)
    raw = json.loads(disk.read_text())
    raw[CAM][day]["g"] = ["junk"]
    disk.write_text(json.dumps(raw))

    restarted = _ArchiveIndex()
    restarted.load_from_disk(disk)
    assert restarted.night_files(CAM, day).sig is None
    restarted.refresh_recent(days=3)
    assert scans == [day, day]


def test_corrupt_index_file_is_tolerated(tmp_path):
    disk = tmp_path / "archive_index.json"
    disk.write_text("{truncated")
    idx = _ArchiveIndex()
    assert idx.load_from_disk(disk) == 0
    assert not idx.is_built()


def test_expired_on_demand_night_is_rearmed_by_stat(archive, scans, monkeypatch):
    old = _ymd(20)
    _night(archive, old, timelapse=["n_timelapse.mp4"])
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx._dates = {CAM: {old}}

    assert idx.night_files(CAM, old).timelapse_files == ("n_timelapse.mp4",)
    with idx._lock:  # expire the entry
        exp, ni = idx._ondemand[(CAM, old)]
        idx._ondemand[(CAM, old)] = (time.time() - 1, ni)
    assert idx.night_files(CAM, old).timelapse_files == ("n_timelapse.mp4",)
    assert scans == [old]


# -- timelapse catalogue ------------------------------------------------------


def test_timelapses_of_old_nights_are_learned_and_persisted(archive, tmp_path):
    old, older, hot = _ymd(20), _ymd(40), _ymd(1)
    _night(archive, old, timelapse=["o_timelapse.mp4", "o_night_stack.webp"])
    _night(archive, older, meteors=["m_color.mkv"])  # no timelapse dir
    _night(archive, hot, timelapse=["h_timelapse.mp4"])
    idx = _ArchiveIndex()
    idx.build_full()

    assert idx.timelapse_files_known(CAM, old) == ("o_night_stack.webp", "o_timelapse.mp4")
    assert idx.timelapse_files_known(CAM, older) == ()
    assert idx.timelapse_files_known(CAM, hot) == ("h_timelapse.mp4",)
    assert idx.timelapse_files_known(CAM, "19990101") is None

    disk = tmp_path / "archive_index.json"
    idx.save_to_disk(disk)
    restarted = _ArchiveIndex()
    restarted.load_from_disk(disk)
    assert restarted.timelapse_files_known(CAM, old) == ("o_night_stack.webp", "o_timelapse.mp4")
    assert restarted.timelapse_files_known(CAM, older) == ()
    assert restarted.timelapse_files_known(CAM, hot) == ("h_timelapse.mp4",)


def test_hourly_rebuild_relists_an_old_timelapse_dir_only_when_it_changed(archive, monkeypatch):
    old = _ymd(20)
    night = _night(archive, old, timelapse=["o_timelapse.mp4"])
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx.build_full()

    listed: list[str] = []
    real_scandir = os.scandir

    def counting_scandir(path):
        if str(path).endswith("timelapse"):
            listed.append(str(path))
        return real_scandir(path)

    monkeypatch.setattr(cache_store.os, "scandir", counting_scandir)
    idx.build_full()
    assert listed == []

    (night / "timelapse" / "late_timelapse.mp4").write_bytes(b"x")
    idx.build_full()
    assert len(listed) == 1
    assert idx.timelapse_files_known(CAM, old) == ("late_timelapse.mp4", "o_timelapse.mp4")


def test_on_demand_scan_feeds_the_catalogue(archive):
    old = _ymd(20)
    _night(archive, old, timelapse=["o_timelapse.mp4"])
    idx = _ArchiveIndex()
    idx._dates = {CAM: {old}}
    assert idx.timelapse_files_known(CAM, old) is None
    idx.night_files(CAM, old)
    assert idx.timelapse_files_known(CAM, old) == ("o_timelapse.mp4",)


def test_legacy_index_file_without_catalogue_loads(tmp_path):
    """A file written before the catalogue existed: old nights are unknown
    (None), never mistaken for "no timelapse"."""
    old = _ymd(20)
    disk = tmp_path / "archive_index.json"
    disk.write_text(json.dumps({CAM: {old: {"m": [], "s": [], "t": []}}}))
    idx = _ArchiveIndex()
    assert idx.load_from_disk(disk) == 1
    assert idx.timelapse_files_known(CAM, old) is None


# -- exit flush ---------------------------------------------------------------


def test_exit_flushes_run_and_isolate_failures(monkeypatch):
    import worker_exit

    monkeypatch.setattr(worker_exit, "_exit_flushes", [])
    ran: list[str] = []

    def boom() -> None:
        raise RuntimeError("disk full")

    worker_exit.register_exit_flush("boom", boom)
    worker_exit.register_exit_flush("ok", lambda: ran.append("ok"))
    worker_exit.run_exit_flushes()
    assert ran == ["ok"]


# ── Old nights: the listing is shared on disk ──────────────────────────────
# An old night is listed on demand, and each gunicorn worker (and every
# restart) used to list it again: five directories over sshfs, stacks/ alone
# ~1,900 entries (1.5 s on 2026-09-27). The listing now goes to disk and any
# worker revalidates it with the signature stats.

@pytest.fixture
def cache_dir(tmp_path, monkeypatch) -> Path:
    d = tmp_path / "cache"
    monkeypatch.setattr(cache_store, "CACHE_PATH", d)
    return d


def test_old_night_listing_is_shared_through_disk(archive, scans, cache_dir):
    day = _ymd(20)
    _night(archive, day, meteors=["a_color.mkv"], stacks=["a_stack.webp"])
    _age_tree(archive)

    first = _ArchiveIndex().night_files(CAM, day)
    assert first.meteor_files == ("a_color.mkv",)
    assert (cache_dir / "archive_nights" / CAM / f"{day}.json.gz").is_file()

    other_worker = _ArchiveIndex()
    again = other_worker.night_files(CAM, day)
    assert again.meteor_files == ("a_color.mkv",)
    assert again.stack_files == frozenset({"a_stack.webp"})
    assert scans == [day], "another worker must revalidate from disk, not relist"


def test_old_night_change_is_seen_through_the_disk_copy(archive, scans, cache_dir):
    day = _ymd(20)
    night = _night(archive, day, meteors=["a_color.mkv", "b_color.mkv"])
    _age_tree(archive)
    _ArchiveIndex().night_files(CAM, day)

    (night / "meteors" / "b_color.mkv").unlink()   # the janitor pruned a clip
    assert _ArchiveIndex().night_files(CAM, day).meteor_files == ("a_color.mkv",)
    assert scans == [day, day]


def test_old_night_disk_copy_expires(archive, scans, cache_dir, monkeypatch):
    day = _ymd(20)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    _ArchiveIndex().night_files(CAM, day)
    monkeypatch.setattr(cache_store, "_SIG_MAX_AGE_OLD_S", -1.0)
    _ArchiveIndex().night_files(CAM, day)
    assert scans == [day, day]


def test_hot_night_is_not_written_to_the_night_files(archive, scans, cache_dir):
    day = _ymd(0)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    _ArchiveIndex().night_files(CAM, day)
    assert not (cache_dir / "archive_nights").exists()


def test_unreadable_disk_copy_means_relist(archive, scans, cache_dir):
    day = _ymd(20)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    path = cache_dir / "archive_nights" / CAM / f"{day}.json.gz"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not gzip")
    assert _ArchiveIndex().night_files(CAM, day).meteor_files == ("a_color.mkv",)
    assert scans == [day]


def test_unreachable_box_serves_the_disk_copy(archive, scans, cache_dir, monkeypatch):
    """A stalled mount must not turn a listed old night into "no files"."""
    import station_client

    day = _ymd(20)
    _night(archive, day, meteors=["a_color.mkv"])
    _age_tree(archive)
    _ArchiveIndex().night_files(CAM, day)
    monkeypatch.setattr(station_client, "_with_sshfs_timeout",
                        lambda fn, timeout=5.0, default=None: default)
    idx = _ArchiveIndex()
    assert idx.night_files(CAM, day).meteor_files == ("a_color.mkv",)
    assert idx.scan_unavailable == 1


# ── Whole-history walks take the last known listing ───────────────────────
# nights_full lists every night a camera has (~220 on RO000H (Ciocârlia)); a
# Storage Box re-check per night alone outlasted its 60 s budget.

def test_listing_walk_does_not_recheck_an_expired_old_night(archive, scans, monkeypatch):
    old = _ymd(20)
    _night(archive, old, meteors=["a_color.mkv"])
    _age_tree(archive)
    idx = _ArchiveIndex()
    idx.night_files(CAM, old)
    with idx._lock:  # expire the entry
        exp, ni = idx._ondemand[(CAM, old)]
        idx._ondemand[(CAM, old)] = (time.time() - 1, ni)
    monkeypatch.setattr(cache_store, "_night_signature",
                        lambda p: (_ for _ in ()).throw(AssertionError("re-checked the box")))
    assert idx.night_files_for_listing(CAM, old).meteor_files == ("a_color.mkv",)


def test_listing_walk_reads_the_disk_copy_then_scans_only_the_unknown(archive, scans, cache_dir):
    listed, never = _ymd(20), _ymd(21)
    _night(archive, listed, meteors=["a_color.mkv"])
    _night(archive, never, meteors=["b_color.mkv"])
    _age_tree(archive)
    _ArchiveIndex().night_files(CAM, listed)  # another worker saved it to disk
    scans.clear()

    idx = _ArchiveIndex()
    assert idx.night_files_for_listing(CAM, listed).meteor_files == ("a_color.mkv",)
    assert idx.night_files_for_listing(CAM, never).meteor_files == ("b_color.mkv",)
    assert scans == [never]
