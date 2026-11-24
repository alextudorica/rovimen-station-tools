"""RMS plots must survive an archive-index save/load round-trip.

``save_to_disk`` persisted meteors, stacks, timelapses, state chunks and
ffthumbs -- but not ``rms_files``. So every night rehydrated from disk came
back claiming it had no RMS plots, and ``night_files`` serves that cached
entry WITHOUT rescanning. After any dashboard restart the whole index reported
"no RMS plots" for every camera until a full rebuild happened to repopulate
it, which is why plot panels render empty for cameras whose plots are sitting
in the archive.

Nothing caught it because every other file type round-tripped fine and the
consumers degrade silently: the calibration endpoints just report no_data.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def _recent(days_ago: int = 1) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


def _index_with(night: str, **kw):
    from cache_store import _ArchiveIndex, _NightIndex

    kw.setdefault("meteor_files", ())
    kw.setdefault("stack_files", frozenset())
    kw.setdefault("timelapse_files", ())
    idx = _ArchiveIndex()
    idx._data = {"RO000W": {night: _NightIndex(**kw)}}
    idx._dates = {"RO000W": {night}}
    return idx


def test_rms_files_survive_a_save_load_round_trip(tmp_path):
    """THE BUG: plots went in, nothing came back."""
    from cache_store import _ArchiveIndex

    night = _recent()
    plots = ["RO000W_x_ff_intervals.png", "RO000W_x_calibration_variation.png"]
    idx = _index_with(night, rms_files=frozenset(plots), stack_files=frozenset(["s.webp"]))

    path = tmp_path / "archive_index.json"
    idx.save_to_disk(path)

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(path)

    ni = reloaded._data.get("RO000W", {}).get(night)
    assert ni is not None, "night vanished across the round trip"
    assert set(ni.rms_files) == set(plots)


def test_a_night_with_only_rms_plots_is_still_loaded(tmp_path):
    """A quiet night can have plots and nothing else. Dropping it from _data
    is the same failure as loading it with an empty plot set."""
    from cache_store import _ArchiveIndex

    night = _recent()
    idx = _index_with(night, rms_files=frozenset(["RO000W_x_radiants.png"]))

    path = tmp_path / "archive_index.json"
    idx.save_to_disk(path)

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(path)

    ni = reloaded._data.get("RO000W", {}).get(night)
    assert ni is not None, "a plots-only night must still be indexed"
    assert set(ni.rms_files) == {"RO000W_x_radiants.png"}


def test_rms_plot_lookup_works_after_a_reload(tmp_path):
    """The consumer path, end to end: this is what the calibration endpoints
    and the plot surfaces actually call."""
    from cache_store import _ArchiveIndex

    night = _recent()
    idx = _index_with(night, rms_files=frozenset(["RO000W_x_ff_intervals.png"]))
    path = tmp_path / "archive_index.json"
    idx.save_to_disk(path)

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(path)

    assert reloaded.rms_plot_file("RO000W", night, "_ff_intervals.png") == (
        "RO000W_x_ff_intervals.png"
    )
    # And the memory-only accessor the plot walk-back depends on.
    assert reloaded.rms_files_cached("RO000W", night) == frozenset(
        ["RO000W_x_ff_intervals.png"]
    )


def test_absent_rms_key_loads_as_empty_not_error(tmp_path):
    """Indexes written before this fix have no "r" key; they must still load."""
    import json

    from cache_store import _ArchiveIndex

    night = _recent()
    path = tmp_path / "archive_index.json"
    path.write_text(json.dumps({"RO000W": {night: {"m": [], "s": ["a.webp"], "t": []}}}))

    reloaded = _ArchiveIndex()
    reloaded.load_from_disk(path)

    ni = reloaded._data.get("RO000W", {}).get(night)
    assert ni is not None
    assert ni.rms_files == frozenset()
