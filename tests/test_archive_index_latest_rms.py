"""A dark camera's last plots must stay findable, cheaply.

The archive index keeps full file listings only for the last
``_ARCHIVE_HOT_DAYS`` nights; older nights are date-only entries. The plot
resolvers walk back over nights reading MEMORY ONLY (an on-demand storage-box
lookup per night per camera is the fan-out that saturates the SSHFS guard and
504s a page that renders every camera at once). Those two facts together meant
a camera that stopped uploading more than three days ago had NO candidate night
the walk could inspect -- so every plot surface reported "no data" for it while
last week's plots sat in the archive. Missing, not stale, which is the one
thing the plot surfaces are not allowed to be.

``_index_latest_rms`` closes that gap from the index writers (background
threads): it resolves each camera's newest plot-bearing night once and
``latest_rms_night`` serves it from memory forever after. These tests pin the
resolution, the storage-box budget that keeps it affordable, and the
persistence without which a warm boot -- which skips ``build_full`` entirely --
starts blind again.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import cache_store
from cache_store import _ArchiveIndex, _NightIndex

CAM = "RO0015"


def _ymd(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


def _night(root, cam: str, date: str, files: list[str]) -> None:
    """Materialise ``<cam>/<date>/rms/`` on the fake storage box."""
    d = root / cam / date / "rms"
    d.mkdir(parents=True, exist_ok=True)
    for name in files:
        (d / name).write_bytes(b"x")


@pytest.fixture
def box(tmp_path, monkeypatch):
    """Point the index at a temp dir standing in for the storage box."""
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", tmp_path)
    # Keep the on-disk night cache per test too (CACHE_PATH/archive_nights).
    monkeypatch.setattr(cache_store, "CACHE_PATH", tmp_path / "cache")
    return tmp_path


def _hot(date: str, files: list[str]) -> dict[str, _NightIndex]:
    return {date: _NightIndex(
        meteor_files=(), stack_files=frozenset(), timelapse_files=(),
        rms_files=frozenset(files),
    )}


def test_a_camera_that_is_uploading_costs_no_storage_box_read(box):
    """The common case is free: the answer is in the just-scanned hot nights."""
    idx = _ArchiveIndex()
    yesterday = _ymd(1)
    # Nothing on the box at all -- a hit can only have come from `hot`.
    idx._index_latest_rms(CAM, _hot(yesterday, ["a_captured_stack.jpg"]), {yesterday})
    assert idx.latest_rms_night(CAM) == (yesterday, frozenset({"a_captured_stack.jpg"}))


def test_the_newest_hot_night_with_plots_wins(box):
    idx = _ArchiveIndex()
    hot = _hot(_ymd(2), ["old_radiants.png"])
    hot.update(_hot(_ymd(1), ["new_radiants.png"]))
    hot.update(_hot(_ymd(3), []))
    idx._index_latest_rms(CAM, hot, set(hot))
    assert idx.latest_rms_night(CAM)[0] == _ymd(1)


def test_a_dark_camera_is_resolved_off_the_box(box):
    """THE BUG: nothing in the hot window, plots weeks back in the archive.

    The two newest night dirs exist but their rms/ dirs are empty -- exactly
    what a camera that died mid-processing leaves behind -- so the walk has to
    keep going rather than conclude "no plots".
    """
    _night(box, CAM, _ymd(20), [])
    _night(box, CAM, _ymd(21), [])
    _night(box, CAM, _ymd(22), ["RO0015_x_captured_stack.jpg", "RO0015_x_radiants.png"])
    _night(box, CAM, _ymd(30), ["RO0015_older_radiants.png"])

    idx = _ArchiveIndex()
    idx._index_latest_rms(CAM, {}, {_ymd(20), _ymd(21), _ymd(22), _ymd(30)})

    date, files = idx.latest_rms_night(CAM)
    assert date == _ymd(22)
    assert "RO0015_x_captured_stack.jpg" in files


def test_a_detections_only_night_does_not_end_the_walk(box):
    """A night can hold RMS text and no plot at all.

    Stations running a bundle older than the whole-check-plot-set fix uploaded
    FTPdetectinfo + radiants.txt and nothing else (Dragsina's last four nights
    are exactly that). Stopping there would record a night that renders as a
    blank card, which is the failure mode this memo exists to remove.
    """
    _night(box, CAM, _ymd(5), ["FTPdetectinfo_RO0015_x.txt"])
    _night(box, CAM, _ymd(6), ["RO0015_x_radiants.txt"])
    _night(box, CAM, _ymd(14), ["RO0015_x_ff_intervals.png"])

    idx = _ArchiveIndex()
    idx._index_latest_rms(CAM, {}, {_ymd(5), _ymd(6), _ymd(14)})
    assert idx.latest_rms_night(CAM)[0] == _ymd(14)


def test_a_detections_only_hot_night_does_not_end_it_either(box):
    _night(box, CAM, _ymd(9), ["RO0015_x_captured_stack.jpg"])
    hot = _hot(_ymd(1), ["FTPdetectinfo_RO0015_x.txt"])

    idx = _ArchiveIndex()
    idx._index_latest_rms(CAM, hot, {_ymd(1), _ymd(9)})
    assert idx.latest_rms_night(CAM)[0] == _ymd(9)


def test_future_night_dirs_are_never_resolved(box):
    """A stray future-dated dir (clock skew during a restore) is not "latest"."""
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y%m%d")
    _night(box, CAM, tomorrow, ["a_radiants.png"])
    _night(box, CAM, _ymd(9), ["real_radiants.png"])

    idx = _ArchiveIndex()
    idx._index_latest_rms(CAM, {}, {tomorrow, _ymd(9)})
    assert idx.latest_rms_night(CAM)[0] == _ymd(9)


def test_a_resolved_camera_is_not_rewalked_on_every_refresh_tick(box, monkeypatch):
    """THE BUDGET: refresh_recent runs every 60 s in every worker.

    Rewalking each dark camera's history on every tick is precisely the
    storage-box churn this memo exists to avoid.
    """
    _night(box, CAM, _ymd(15), ["a_radiants.png"])
    dates = {_ymd(15)}
    idx = _ArchiveIndex()

    calls: list[str] = []
    real = _ArchiveIndex._scan_rms_dir
    monkeypatch.setattr(
        _ArchiveIndex, "_scan_rms_dir",
        staticmethod(lambda p: (calls.append(p), real(p))[1]),
    )

    idx._index_latest_rms(CAM, {}, dates)
    first = len(calls)
    assert first >= 1
    idx._index_latest_rms(CAM, {}, dates)
    assert len(calls) == first, "second pass must be served from the memo"


def test_a_camera_with_no_plots_anywhere_is_not_rewalked_either(box, monkeypatch):
    """A negative is memoised too -- and reported as "unknown", not a hit."""
    _night(box, CAM, _ymd(5), [])
    dates = {_ymd(5)}
    idx = _ArchiveIndex()

    calls: list[str] = []
    real = _ArchiveIndex._scan_rms_dir
    monkeypatch.setattr(
        _ArchiveIndex, "_scan_rms_dir",
        staticmethod(lambda p: (calls.append(p), real(p))[1]),
    )

    idx._index_latest_rms(CAM, {}, dates)
    assert idx.latest_rms_night(CAM) is None
    first = len(calls)
    idx._index_latest_rms(CAM, {}, dates)
    assert len(calls) == first


def test_a_pruned_answer_is_re_resolved_before_its_ttl(box):
    """Retention removed the night we recorded -- rewalk instead of pointing
    the surfaces at a directory that is no longer on the box."""
    _night(box, CAM, _ymd(12), ["still_here_radiants.png"])
    idx = _ArchiveIndex()
    idx._latest_rms[CAM] = (
        datetime.now(timezone.utc).timestamp(), _ymd(40), frozenset({"gone.png"}),
    )
    idx._index_latest_rms(CAM, {}, {_ymd(12)})
    assert idx.latest_rms_night(CAM)[0] == _ymd(12)


def test_an_answer_past_its_ttl_is_still_served(box):
    """The TTL gates rewalking, never reading.

    Expiring the answer on read would put the blank card straight back --
    a stale plot night is still the last one we know about.
    """
    idx = _ArchiveIndex()
    idx._latest_rms[CAM] = (0.0, _ymd(50), frozenset({"a_radiants.png"}))
    assert idx.latest_rms_night(CAM) == (_ymd(50), frozenset({"a_radiants.png"}))


def test_it_survives_a_save_load_round_trip(tmp_path):
    """A warm boot skips build_full entirely, so an unpersisted memo means
    every dark camera renders "no data" again after each restart."""
    idx = _ArchiveIndex()
    idx._dates = {CAM: {_ymd(22)}}
    idx._latest_rms = {
        CAM: (1.0, _ymd(22), frozenset({"RO0015_x_captured_stack.jpg"})),
    }
    disk = tmp_path / "archive_index.json"
    idx.save_to_disk(disk)

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(disk)
    assert reloaded.latest_rms_night(CAM) == (
        _ymd(22), frozenset({"RO0015_x_captured_stack.jpg"}),
    )


def test_the_reserved_key_is_never_mistaken_for_a_camera(tmp_path):
    """It shares the top level of the index file with the camera entries."""
    idx = _ArchiveIndex()
    idx._dates = {CAM: {_ymd(2)}}
    idx._latest_rms = {CAM: (1.0, _ymd(2), frozenset({"a_radiants.png"}))}
    disk = tmp_path / "archive_index.json"
    idx.save_to_disk(disk)

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(disk)
    assert "__latest_rms__" not in reloaded.cameras()
    assert reloaded.nights("__latest_rms__") == []
    assert reloaded.nights(CAM) == [_ymd(2)]


def test_an_index_file_written_before_the_memo_existed_still_loads(tmp_path):
    idx = _ArchiveIndex()
    idx._dates = {CAM: {_ymd(2)}}
    disk = tmp_path / "archive_index.json"
    idx.save_to_disk(disk)
    assert "__latest_rms__" not in disk.read_text()

    reloaded = _ArchiveIndex()
    assert reloaded.load_from_disk(disk) >= 1
    assert reloaded.latest_rms_night(CAM) is None
