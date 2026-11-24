"""Tests for detection_indexer's chunk lookup.

``chunk_file`` is what the dashboard's index fast path uses to name a
detection's clip, so it has to name the file that actually gets uploaded. A
meteor landing within STITCH_BOUNDARY_SECS of a chunk edge is locked and
uploaded as the boundary splice detection_lock._stitch_clips writes, while both
raw chunks stay unlocked and never reach the archive. Recording the raw name
there made 748 detections over nights 20260723-25 render as a permanent "clip
pending" marker with no video behind it.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import detection_indexer

CAM = "RO000P"
NIGHT = "20260723"

# Three consecutive 20 s chunks plus the splice across the 00:28:43 boundary.
RAW_A = "RO000P_20260724_002823_color.mkv"
RAW_B = "RO000P_20260724_002843_color.mkv"
RAW_C = "RO000P_20260724_002903_color.mkv"
SPLICE = "RO000P_20260724_002833_002853_color.mkv"


@pytest.fixture
def cap_dir(tmp_path):
    d = tmp_path / "color_capture" / CAM / NIGHT
    d.mkdir(parents=True)
    return d


def _cfg(tmp_path) -> dict:
    return {"videocapture_path": str(tmp_path / "color_capture")}


def _touch(cap_dir, *names: str) -> None:
    for n in names:
        (cap_dir / n).write_bytes(b"")


def _index(tmp_path, cap_dir, *names: str):
    _touch(cap_dir, *names)
    return detection_indexer._build_chunk_index(CAM, NIGHT, _cfg(tmp_path))


# ── _build_chunk_index ───────────────────────────────────────────────────────

def test_index_gives_raw_chunks_a_20s_span(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_A)
    assert len(idx) == 1
    start, end, name = idx[0]
    assert name == RAW_A
    assert start == datetime(2026, 7, 24, 0, 28, 23, tzinfo=timezone.utc)
    assert (end - start).total_seconds() == 20


def test_index_includes_the_boundary_splice(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_B, SPLICE)
    assert [n for _s, _e, n in idx] == [RAW_A, SPLICE, RAW_B]
    start, end, _ = idx[1]
    assert (end - start).total_seconds() == 20
    assert start == datetime(2026, 7, 24, 0, 28, 33, tzinfo=timezone.utc)


def test_index_handles_a_splice_across_midnight(tmp_path, cap_dir):
    wrapped = "RO000P_20260723_235950_000010_color.mkv"
    idx = _index(tmp_path, cap_dir, wrapped)
    start, end, _ = idx[0]
    assert (end - start).total_seconds() == 20
    assert end == datetime(2026, 7, 24, 0, 0, 10, tzinfo=timezone.utc)


def test_index_drops_implausible_and_unparseable_names(tmp_path, cap_dir):
    idx = _index(
        tmp_path, cap_dir,
        "RO000P_20260724_010000_005900_color.mkv",  # Tend before Tstart: ~24 h
        "RO000P_20260724_993333_color.mkv",         # impossible hour
        "not_a_chunk_color.mkv",
        RAW_A,
    )
    assert [n for _s, _e, n in idx] == [RAW_A]


def test_index_empty_for_missing_directory(tmp_path):
    assert detection_indexer._build_chunk_index(CAM, "20990101", _cfg(tmp_path)) == []


# ── _find_chunk ──────────────────────────────────────────────────────────────

def test_find_returns_raw_chunk_for_a_mid_chunk_detection(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_B, RAW_C)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:53") == RAW_B


def test_find_prefers_the_splice_over_the_raw_chunk(tmp_path, cap_dir):
    """The detection sits 1.4 s into RAW_B, inside the stitch zone, so the
    station locked and uploaded SPLICE and RAW_B never reaches the archive."""
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_B, RAW_C, SPLICE)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:44.4") == SPLICE


def test_find_prefers_the_splice_from_the_chunk_before_the_boundary(tmp_path, cap_dir):
    """Same splice, meteor 19 s into RAW_A instead — a pre-boundary stitch.
    The covering splice starts before RAW_A's successor, so a lookup that only
    inspects the newest entry starting before the detection misses it."""
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_B, RAW_C, SPLICE)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:42.1") == SPLICE


def test_find_falls_back_to_raw_when_no_splice_spans_the_detection(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_B, RAW_C, SPLICE)
    # 00:29:13 is inside RAW_C, well past SPLICE's 00:28:53 end.
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:29:13") == RAW_C


def test_find_returns_none_for_a_gap_between_chunks(tmp_path, cap_dir):
    """A dropped chunk must not attach its meteor to the surviving neighbour."""
    idx = _index(tmp_path, cap_dir, RAW_A, RAW_C)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:50") is None


def test_find_returns_none_before_the_first_chunk(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_B)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:00") is None


def test_find_degrades_on_empty_index_or_bad_time(tmp_path, cap_dir):
    idx = _index(tmp_path, cap_dir, RAW_A)
    assert detection_indexer._find_chunk([], "2026-07-24T00:28:25") is None
    assert detection_indexer._find_chunk(idx, "") is None
    assert detection_indexer._find_chunk(idx, "not-a-time") is None


def test_legacy_local_chunk_names_are_matched_in_utc(tmp_path, cap_dir):
    """chunk_names_utc: false (pre-#851 local-named chunks): the index converts
    names to UTC like station_api, so a UTC detection time finds its chunk."""
    (cap_dir / "RO000P_20260724_032843_color.mkv").write_bytes(b"")  # 03:28:43 EEST
    cfg = dict(_cfg(tmp_path), chunk_names_utc=False, legacy_chunk_tz="Europe/Bucharest")
    idx = detection_indexer._build_chunk_index(CAM, NIGHT, cfg)
    assert idx[0][0] == datetime(2026, 7, 24, 0, 28, 43, tzinfo=timezone.utc)
    assert detection_indexer._find_chunk(idx, "2026-07-24T00:28:50") == \
        "RO000P_20260724_032843_color.mkv"
