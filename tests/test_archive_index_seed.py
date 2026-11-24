"""Guards for the archive-index warm-boot fast path.

After a gunicorn worker recycle, the last few nights must load instantly from
the on-disk archive-index cache (zero per-request SSHFS) instead of triggering
the full-archive ``build_full`` rescan that saturated the storagebox mount for
~40-90 s. Two invariants:

  1. A night restored via ``load_from_disk`` is served by ``night_files``
     straight from ``_data`` -- it must never fall through to an SSHFS scan.
  2. ``_archive_index_cache_is_fresh`` correctly classifies fresh / stale /
     missing cache files (the gate that skips the boot-time mount storm).
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import cache_store
from cache_store import _ArchiveIndex, _NightIndex


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d")


def test_loaded_recent_night_never_scans_sshfs(tmp_path):
    day = _today()
    ni = _NightIndex(
        meteor_files=("RO000A_20260706_224512_0042.mkv",),
        stack_files=frozenset({"RO000A_20260706_stack.webp"}),
        timelapse_files=(),
        state_chunks=None,
    )
    src = _ArchiveIndex()
    with src._lock:
        src._data = {"RO000A": {day: ni}}
        src._dates = {"RO000A": {day}}
        src._has_meteors = {"RO000A": {day}}

    disk = tmp_path / "archive_index.json"
    src.save_to_disk(disk)

    dst = _ArchiveIndex()
    loaded = dst.load_from_disk(disk)
    assert loaded >= 1
    assert dst.ready is True

    # If night_files fell through to SSHFS this would raise; the loaded night
    # must be answered from _data instead.
    dst._scan_night = lambda *_a, **_k: (_ for _ in ()).throw(  # type: ignore[assignment]
        AssertionError("night_files hit SSHFS for a disk-loaded night")
    )
    got = dst.night_files("RO000A", day)
    assert got is not None
    assert got.meteor_files == ni.meteor_files
    assert got.stack_files == ni.stack_files


def test_scan_night_indexes_ffthumbs(tmp_path):
    """A live night often holds ONLY the indexer-pushed BW thumbs (clip and
    stack bytes arrive later), so ffthumbs alone must produce an index entry —
    otherwise the pending-marker thumbnail lookup sees an empty night."""
    night = tmp_path / "RO000A" / "20260727"
    (night / "ffthumbs").mkdir(parents=True)
    (night / "ffthumbs" / "RO000A_20260727_214512.webp").write_bytes(b"RIFFx")

    ni = _ArchiveIndex._scan_night(str(night))
    assert ni is not None
    assert ni.ffthumb_files == frozenset({"RO000A_20260727_214512.webp"})
    assert ni.meteor_files == ()
    assert ni.stack_files == frozenset()


def test_ffthumbs_survive_disk_roundtrip(tmp_path):
    day = _today()
    ni = _NightIndex(
        meteor_files=(),
        stack_files=frozenset(),
        timelapse_files=(),
        state_chunks=None,
        ffthumb_files=frozenset({"RO000A_20260727_214512.webp"}),
    )
    src = _ArchiveIndex()
    with src._lock:
        src._data = {"RO000A": {day: ni}}
        src._dates = {"RO000A": {day}}
        src._has_meteors = {"RO000A": set()}

    disk = tmp_path / "archive_index.json"
    src.save_to_disk(disk)

    dst = _ArchiveIndex()
    assert dst.load_from_disk(disk) >= 1
    got = dst._data["RO000A"][day]
    # An ffthumbs-only night must stay a hot entry (the load-time emptiness
    # gate has to count "f" too) and keep its file set intact.
    assert got.ffthumb_files == ni.ffthumb_files


def test_scan_night_indexes_rms_plot(tmp_path):
    """FF-intervals PNGs ride into the archive's rms/ subdir alongside
    FTPdetectinfo/radiants (archive_upload.py's _find_rms_files) so the
    Calibration page can resolve them from the archive index alone -- no
    live station call. An rms-only night must still produce an index entry."""
    night = tmp_path / "RO000A" / "20260727"
    (night / "rms").mkdir(parents=True)
    (night / "rms" / "RO000A_20260727_180000_000000_ff_intervals.png").write_bytes(b"\x89PNG")

    ni = _ArchiveIndex._scan_night(str(night))
    assert ni is not None
    assert ni.rms_files == frozenset({"RO000A_20260727_180000_000000_ff_intervals.png"})


def test_ff_intervals_file_finds_the_plot(tmp_path):
    idx = _ArchiveIndex()
    day = "20260727"
    ni = _NightIndex(
        meteor_files=(), stack_files=frozenset(), timelapse_files=(),
        rms_files=frozenset({
            "RO000A_20260727_180000_000000_ff_intervals.png",
            "FTPdetectinfo_RO000A_20260727_180000_000000.txt",
        }),
    )
    with idx._lock:
        idx._data = {"RO000A": {day: ni}}
        idx._dates = {"RO000A": {day}}

    assert idx.ff_intervals_file("RO000A", day) == "RO000A_20260727_180000_000000_ff_intervals.png"
    assert idx.ff_intervals_file("RO000A", "20260101") is None
    assert idx.ff_intervals_file("UNKNOWNCAM", day) is None


def test_cache_freshness_helper(tmp_path):
    import rovimen_dashboard as rd

    missing = tmp_path / "nope.json"
    assert rd._archive_index_cache_is_fresh(missing) is False

    fresh = tmp_path / "archive_index.json"
    fresh.write_text("{}")
    assert rd._archive_index_cache_is_fresh(fresh) is True

    # Backdate well past the default 24 h window -> stale -> full rescan path.
    old = datetime.now(timezone.utc).timestamp() - 2 * 86400
    os.utime(fresh, (old, old))
    assert rd._archive_index_cache_is_fresh(fresh) is False
    # Explicit override still honoured.
    assert rd._archive_index_cache_is_fresh(fresh, max_age_s=3 * 86400) is True
