"""Tests for the detection-index fast-path helpers (routes/detections.py).

These pure functions let past-night payloads come from detections.db + the
in-memory archive index instead of the per-camera SSHFS fan-out. They must
mirror the state.json path's chunk shape exactly — the frontend and the
highlights/grid/range consumers cannot tell the two sources apart.
"""

from __future__ import annotations

from routes.detections import (
    _chunk_from_index_row,
    _fallback_cams,
    _pending_chunk_from_row,
    _pending_stack,
    _resolve_chunk_file,
    _resolve_stitched_chunk,
    _row_brightness,
    _video_pruned,
)
from models import CameraConfig, StationConfig

RMS_FIELDS = (
    "shower", "mag_apparent", "mag_absolute",
    "duration_s", "angular_velocity",
    "ra_radiant", "dec_radiant", "radiant_elev",
    "solar_lon", "num_segments", "fps",
)


# ── _row_brightness ──────────────────────────────────────────────────────────

def test_row_brightness_prefers_apparent_then_absolute_then_inf():
    assert _row_brightness({"mag_apparent": -2.5, "mag_absolute": 1.0}) == -2.5
    assert _row_brightness({"mag_apparent": None, "mag_absolute": 1.0}) == 1.0
    assert _row_brightness({}) == float("inf")


def test_row_brightness_orders_brightest_first():
    rows = [{"mag_apparent": 1.5}, {"mag_apparent": -4.0}, {}]
    ordered = sorted(rows, key=_row_brightness)
    assert ordered[0]["mag_apparent"] == -4.0
    assert ordered[-1] == {}


# ── _resolve_chunk_file ──────────────────────────────────────────────────────

FILES = [
    "RO0017_20260706_194842_color.mkv",
    "RO0017_20260706_201123_color.mkv",
    "RO0017_20260706_235801_color.mkv",
    "RO0017_20260706_201123_stack.webp",  # non-video must be ignored
]


def test_resolve_picks_closest_preceding_chunk():
    assert (
        _resolve_chunk_file("2026-07-06T20:14:02", FILES)
        == "RO0017_20260706_201123_color.mkv"
    )


def test_resolve_accepts_space_separated_timestamp():
    assert (
        _resolve_chunk_file("2026-07-06 19:50:00.123456", FILES)
        == "RO0017_20260706_194842_color.mkv"
    )


def test_resolve_handles_midnight_wrap():
    # 00:03 belongs to the 23:58 chunk of the previous calendar day.
    assert (
        _resolve_chunk_file("2026-07-07T00:03:10", FILES)
        == "RO0017_20260706_235801_color.mkv"
    )


def test_resolve_rejects_beyond_one_hour_window():
    # 22:30 is >1h after the last preceding chunk start (20:11).
    assert _resolve_chunk_file("2026-07-06T22:30:00", FILES) is None


def test_resolve_degrades_on_garbage():
    assert _resolve_chunk_file(None, FILES) is None
    assert _resolve_chunk_file("not-a-time", FILES) is None
    assert _resolve_chunk_file("2026-07-06T20:14:02", []) is None
    assert _resolve_chunk_file("2026-07-06T20:14:02", ["noise.txt"]) is None


def test_resolve_mkv_wins_over_mp4_sibling_tie():
    """Push stations upload both an .mkv and its faststart .mp4 sibling.
    ``meteor_files`` is a set, so without a deterministic tie-break the
    emitted filename would flip between requests (intermittent 400s on
    DE001B were traced to exactly this)."""
    files = {
        "RO0017_20260706_201123_color.mp4",
        "RO0017_20260706_201123_color.mkv",
    }
    for _ in range(10):
        assert (
            _resolve_chunk_file("2026-07-06T20:14:02", files)
            == "RO0017_20260706_201123_color.mkv"
        )


# ── _resolve_stitched_chunk ──────────────────────────────────────────────────
# detection_lock.py splices the two chunks either side of a meteor that lands
# within 2 s of a 20 s chunk edge into <cam>_<date>_<Tstart>_<Tend>_color.mkv,
# then locks and uploads THAT while the raw chunks stay unlocked. The station's
# index still records the raw chunk name, so the archive legitimately has no
# file under it and an exact-name lookup reported "clip pending" forever — 748
# such detections over nights 20260723-25, ~20% of every station's total,
# which is exactly the 4 s of stitch zone in each 20 s chunk.

STITCHED = "RO000P_20260724_002833_002853_color.mkv"


def test_resolve_stitched_matches_detection_inside_the_span():
    # Raw index name RO000P_20260724_002823_color.mkv is absent from the box.
    assert _resolve_stitched_chunk("2026-07-24T00:28:41", {STITCHED}) == STITCHED


def test_resolve_stitched_accepts_both_span_endpoints():
    for t in ("2026-07-24T00:28:33", "2026-07-24T00:28:53"):
        assert _resolve_stitched_chunk(t, {STITCHED}) == STITCHED


def test_resolve_stitched_rejects_detection_outside_the_span():
    for t in ("2026-07-24T00:28:32", "2026-07-24T00:28:54"):
        assert _resolve_stitched_chunk(t, {STITCHED}) is None


def test_resolve_stitched_ignores_plain_chunk_names():
    """Plain chunks are _resolve_chunk_file's job. Matching them here would
    let a stitched lookup silently answer with an unrelated chunk."""
    plain = {
        "RO000P_20260724_002823_color.mkv",
        "RO000P_20260724_002843_color.mkv",
        "RO000P_20260724_002833_stack.webp",
    }
    assert _resolve_stitched_chunk("2026-07-24T00:28:41", plain) is None


def test_resolve_stitched_handles_midnight_wrap():
    wrapped = "RO000P_20260723_235950_000010_color.mkv"
    assert _resolve_stitched_chunk("2026-07-24T00:00:03", {wrapped}) == wrapped
    assert _resolve_stitched_chunk("2026-07-23T23:59:55", {wrapped}) == wrapped
    assert _resolve_stitched_chunk("2026-07-24T00:00:15", {wrapped}) is None


def test_resolve_stitched_rejects_implausible_span():
    """A malformed pair must not swallow a detection minutes away. Tend before
    Tstart wraps to a near-24 h span, which would otherwise match anything."""
    assert _resolve_stitched_chunk(
        "2026-07-24T04:00:00", {"RO000P_20260724_002833_002853_color.mkv"}
    ) is None
    assert _resolve_stitched_chunk(
        "2026-07-24T04:00:00", {"RO000P_20260724_010000_005900_color.mkv"}
    ) is None


def test_resolve_stitched_prefers_narrowest_span():
    """Consecutive boundary meteors produce overlapping splices; the tighter
    one puts the meteor nearer the middle of the clip."""
    wide = "RO000P_20260724_002813_002913_color.mkv"
    files = {wide, STITCHED}
    assert _resolve_stitched_chunk("2026-07-24T00:28:41", files) == STITCHED


def test_resolve_stitched_mkv_wins_over_mp4_sibling_tie():
    files = {
        "RO000P_20260724_002833_002853_color.mp4",
        "RO000P_20260724_002833_002853_color.mkv",
    }
    for _ in range(10):
        assert _resolve_stitched_chunk("2026-07-24T00:28:41", files) == STITCHED


def test_resolve_stitched_degrades_on_missing_or_garbage_time():
    assert _resolve_stitched_chunk(None, {STITCHED}) is None
    assert _resolve_stitched_chunk("not-a-time", {STITCHED}) is None
    assert _resolve_stitched_chunk("2026-07-24T00:28:41", set()) is None


# ── compute_detection_offset (route_helpers) ─────────────────────────────────

def test_compute_detection_offset_accepts_mp4_sibling_name():
    from route_helpers import compute_detection_offset

    off_mkv = compute_detection_offset(
        "RO0017_20260706_201123_color.mkv", "2026-07-06T20:12:03", None
    )
    off_mp4 = compute_detection_offset(
        "RO0017_20260706_201123_color.mp4", "2026-07-06T20:12:03", None
    )
    assert off_mkv == off_mp4 == 40.0


def test_compute_detection_offset_accepts_stitched_name():
    """Without this the map's play window is [NaN, NaN] for every stitched
    clip: offset None makes winEnd = null + dur + 3."""
    from route_helpers import compute_detection_offset

    # Splice starts 00:28:33; meteor at 00:28:41.4167 sits 8.42 s in.
    assert compute_detection_offset(
        STITCHED, "2026-07-24T00:28:41.416700", None
    ) == 8.42
    assert compute_detection_offset(
        "RO000P_20260724_002833_002853_color.mp4", "2026-07-24T00:28:41", None
    ) == 8.0


def test_compute_detection_offset_stitched_from_detection_time():
    from route_helpers import compute_detection_offset

    assert compute_detection_offset(
        STITCHED, None, "20260724_002841"
    ) == 8.0


# ── _chunk_from_index_row ────────────────────────────────────────────────────

def _row(**over):
    base = {
        "time_utc": "2026-07-06T20:12:02",
        "shower": "SPO",
        "mag_apparent": -1.2,
        "mag_absolute": -0.8,
        "duration_s": 0.6,
        "angular_velocity": 12.3,
        "ra_radiant": 100.0,
        "dec_radiant": 45.0,
        "radiant_elev": 30.0,
        "solar_lon": 105.0,
        "num_segments": 9,
        "fps": 25.0,
        # DB columns the chunk shape must NOT leak:
        "cam": "RO000G",
        "ff_file": "FF_RO000G_20260706_201202_000.fits",
        "source": "push",
    }
    base.update(over)
    return base


FNAME = "RO000G_20260706_201148_color.mkv"
STACK = "RO000G_20260706_201148_stack.webp"


def test_chunk_shape_matches_state_json_path():
    chunk = _chunk_from_index_row(
        _row(), FNAME, {FNAME}, frozenset({STACK}), RMS_FIELDS
    )
    assert chunk["filename"] == FNAME
    assert chunk["time"] == "20:11:48"
    assert chunk["stack"] == STACK
    assert chunk["stack_subdir"] == "stacks"
    assert chunk["locked"] is True
    assert chunk["lock_type"] == "detection"
    assert chunk["meteor_time"] == "2026-07-06T20:12:02"
    assert chunk["reencoded"] is True
    assert chunk["source"] == "index"
    # Offset: 20:12:02 - 20:11:48 = 14s
    assert chunk["detection_offset_s"] == 14.0
    assert chunk["rms"] == {f: _row()[f] for f in RMS_FIELDS}
    # DB-only columns must not leak into the chunk dict.
    assert "cam" not in chunk and "ff_file" not in chunk


def test_chunk_stack_falls_back_to_meteors_dir():
    chunk = _chunk_from_index_row(
        _row(), FNAME, {FNAME, STACK}, frozenset(), RMS_FIELDS
    )
    assert chunk["stack"] == STACK
    assert chunk["stack_subdir"] == "meteors"


def test_chunk_without_stack_anywhere():
    chunk = _chunk_from_index_row(_row(), FNAME, {FNAME}, frozenset(), RMS_FIELDS)
    assert chunk["stack"] is None
    assert chunk["stack_subdir"] is None


def test_chunk_offset_none_when_no_meteor_time():
    chunk = _chunk_from_index_row(
        _row(time_utc=None), FNAME, {FNAME}, frozenset(), RMS_FIELDS
    )
    assert chunk["detection_offset_s"] is None
    assert chunk["meteor_time"] is None


def test_chunk_shape_for_stitched_clip():
    """A stitched splice must yield a fully playable chunk: clip start time,
    the matching stitched stack (_stitch_stacks writes one alongside), and a
    real offset. Anything None here shows up as a broken player on the map."""
    stitched_stack = "RO000P_20260724_002833_002853_stack.webp"
    chunk = _chunk_from_index_row(
        _row(time_utc="2026-07-24T00:28:41.416700"),
        STITCHED,
        {STITCHED},
        frozenset({stitched_stack}),
        RMS_FIELDS,
    )
    assert chunk["filename"] == STITCHED
    assert chunk["time"] == "00:28:33"
    assert chunk["stack"] == stitched_stack
    assert chunk["stack_subdir"] == "stacks"
    assert chunk["detection_offset_s"] == 8.42
    assert chunk["locked"] is True


# ── _pending_chunk_from_row ──────────────────────────────────────────────────
# A detection row whose clip hasn't reached the archive yet (push station,
# bytes in transit) is surfaced as a pending marker instead of dropped.

def test_pending_chunk_shape():
    chunk = _pending_chunk_from_row(_row(), RMS_FIELDS)
    assert chunk["pending"] is True
    assert chunk["filename"] is None
    assert chunk["stack"] is None
    assert chunk["stack_subdir"] is None
    assert chunk["size_mb"] is None
    assert chunk["locked"] is True
    assert chunk["lock_type"] == "detection"
    assert chunk["detection_offset_s"] is None
    assert chunk["meteor_time"] == "2026-07-06T20:12:02"
    # Never claim a re-encoded archived clip exists.
    assert chunk["reencoded"] is False
    assert chunk["source"] == "index"
    assert chunk["rms"] == {f: _row()[f] for f in RMS_FIELDS}
    # DB-only columns must not leak into the chunk dict.
    assert "cam" not in chunk and "ff_file" not in chunk


def test_pending_chunk_time_from_time_utc():
    # "time" drives the frontend sort — derived from the detection timestamp.
    chunk = _pending_chunk_from_row(_row(), RMS_FIELDS)
    assert chunk["time"] == "20:12:02"


def test_pending_chunk_accepts_space_separated_timestamp():
    chunk = _pending_chunk_from_row(
        _row(time_utc="2026-07-06 20:12:02.123456"), RMS_FIELDS
    )
    assert chunk["time"] == "20:12:02"
    assert chunk["meteor_time"] == "2026-07-06 20:12:02.123456"


def test_pending_chunk_degrades_on_missing_or_short_timestamp():
    assert _pending_chunk_from_row(_row(time_utc=None), RMS_FIELDS)["time"] == "00:00:00"
    assert _pending_chunk_from_row(_row(time_utc="20:12"), RMS_FIELDS)["time"] == "00:00:00"


def test_pending_chunk_carries_resolved_stack():
    chunk = _pending_chunk_from_row(
        _row(), RMS_FIELDS, stack=STACK, stack_subdir="stacks"
    )
    assert chunk["pending"] is True
    assert chunk["filename"] is None
    assert chunk["stack"] == STACK
    assert chunk["stack_subdir"] == "stacks"


# ── _pending_stack ───────────────────────────────────────────────────────────
# The chunk stack webp uploads ahead of the clip bytes, so a pending marker
# should carry the real thumbnail whenever the stack has already landed.

PENDING_STACKS = frozenset({
    "RO000G_20260706_201148_stack.webp",
    "RO000G_20260706_194842_stack.webp",
})


def test_pending_stack_derived_from_chunk_file():
    stack, subdir = _pending_stack(
        _row(chunk_file=FNAME), PENDING_STACKS, set()
    )
    assert stack == STACK
    assert subdir == "stacks"


def test_pending_stack_time_match_without_chunk_file():
    # 20:12:02 detection → closest preceding stack start-time is 20:11:48.
    stack, subdir = _pending_stack(_row(), PENDING_STACKS, set())
    assert stack == STACK
    assert subdir == "stacks"


def test_pending_stack_meteors_dir_fallback():
    stack, subdir = _pending_stack(
        _row(chunk_file=FNAME), frozenset(), {STACK}
    )
    assert stack == STACK
    assert subdir == "meteors"


def test_pending_stack_none_when_not_landed():
    assert _pending_stack(_row(), frozenset(), set()) == (None, None)
    # A bogus timestamp must not attach a random stack.
    assert _pending_stack(
        _row(time_utc="garbage"), PENDING_STACKS, set()
    ) == (None, None)


def test_resolve_chunk_file_with_stack_suffix():
    assert _resolve_chunk_file(
        "2026-07-06T20:12:02", PENDING_STACKS, suffixes=("_stack.webp",)
    ) == STACK


# ── _pending_stack: BW ffthumb fallback ─────────────────────────────────────
# The detection indexer pushes ffthumbs/<chunk_stem>.webp seconds after a
# detection lands, so a pending marker whose colour stack has not arrived yet
# can still carry a real (BW) image instead of a grey placeholder.

FFTHUMB = "RO000G_20260706_201148.webp"
PENDING_FFTHUMBS = frozenset({FFTHUMB, "RO000G_20260706_194842.webp"})


def test_pending_ffthumb_derived_from_chunk_file():
    stack, subdir = _pending_stack(
        _row(chunk_file=FNAME), frozenset(), set(),
        ffthumb_names=PENDING_FFTHUMBS,
    )
    assert stack == FFTHUMB
    assert subdir == "ffthumbs"


def test_pending_ffthumb_time_match_without_chunk_file():
    # 20:12:02 detection → closest preceding ffthumb start-time is 20:11:48.
    stack, subdir = _pending_stack(
        _row(), frozenset(), set(), ffthumb_names=PENDING_FFTHUMBS
    )
    assert stack == FFTHUMB
    assert subdir == "ffthumbs"


def test_pending_colour_stack_beats_ffthumb():
    # Both landed: the colour stack must win, the BW thumb is only a fallback.
    stack, subdir = _pending_stack(
        _row(chunk_file=FNAME), PENDING_STACKS, set(),
        ffthumb_names=PENDING_FFTHUMBS,
    )
    assert stack == STACK
    assert subdir == "stacks"


def test_pending_ffthumb_none_on_garbage_time():
    # A bogus timestamp must not attach a random ffthumb either.
    assert _pending_stack(
        _row(time_utc="garbage"), frozenset(), set(),
        ffthumb_names=PENDING_FFTHUMBS,
    ) == (None, None)


def test_resolve_chunk_file_matches_bare_webp_stems():
    # ffthumb names end straight in ".webp" after the timestamp (no "_stack"
    # infix), so the shared filename-time regex must accept "TIMESTAMP.".
    assert _resolve_chunk_file(
        "2026-07-06T20:12:02", PENDING_FFTHUMBS, suffixes=(".webp",)
    ) == FFTHUMB


# ── _fallback_cams ────────────────────────────────────────────────────────────

def _stations():
    return {
        "st_ok": StationConfig(
            ip="100.0.0.1", label="OK",
            cameras=[CameraConfig(code="RO000A", cam_ip="192.168.1.10")],
        ),
        "st_broken": StationConfig(
            ip="100.0.0.2", label="Broken",
            cameras=[
                CameraConfig(code="DE001B", cam_ip="192.168.1.11"),
                CameraConfig(code="DE001C", cam_ip="192.168.1.12"),
            ],
        ),
    }


def test_fallback_selects_archived_cams_with_zero_db_rows():
    """A camera with archived clips but no detection rows is the broken-
    metadata-push signature — it must be served from the archive, not
    silently blanked by the DB-only fast path."""
    picked = _fallback_cams(
        _stations(),
        db_cams={"RO000A"},
        has_archive_files=lambda cam: True,
    )
    assert picked == [("st_broken", "DE001B"), ("st_broken", "DE001C")]


def test_fallback_skips_cams_with_db_rows():
    picked = _fallback_cams(
        _stations(),
        db_cams={"RO000A", "DE001B", "DE001C"},
        has_archive_files=lambda cam: True,
    )
    assert picked == []


def test_fallback_skips_cams_without_archive_files():
    """No archive files + no DB rows = genuinely no detections that night —
    nothing to fall back to, and no false 'broken push' signal."""
    picked = _fallback_cams(
        _stations(),
        db_cams=set(),
        has_archive_files=lambda cam: False,
    )
    assert picked == []


def test_fallback_matches_db_cams_case_insensitively():
    """db_cams keys are uppercased by the fast path; a lowercase-configured
    camera code must still be recognised as covered."""
    stations = {
        "st": StationConfig(
            ip="100.0.0.3", label="Lower",
            cameras=[CameraConfig(code="ro000x", cam_ip="192.168.1.13")],
        ),
    }
    picked = _fallback_cams(
        stations, db_cams={"RO000X"}, has_archive_files=lambda cam: True
    )
    assert picked == []


# ── _night_rms_complete (Process-night button gate) ──────────────────────────

import json as _json
import routes.detections as _det
from routes.detections import _night_rms_complete


def _write_state(archive, cam, date, **flags):
    d = archive / cam / date
    d.mkdir(parents=True, exist_ok=True)
    (d / "state.json").write_text(_json.dumps(flags))


def _two_cam_station():
    return StationConfig(
        ip="100.0.0.1", label="X",
        cameras=[
            CameraConfig(code="RO000A", cam_ip="192.168.1.10"),
            CameraConfig(code="RO000B", cam_ip="192.168.1.11"),
        ],
    )


def test_night_rms_complete_true_when_all_cameras_complete(tmp_path, monkeypatch):
    monkeypatch.setattr(_det, "ARCHIVE_PATH", tmp_path)
    _write_state(tmp_path, "RO000A", "20260730", rms_complete=True)
    _write_state(tmp_path, "RO000B", "20260730", rms_complete=True)
    assert _night_rms_complete(_two_cam_station(), "20260730") is True


def test_night_rms_complete_false_when_any_camera_incomplete(tmp_path, monkeypatch):
    monkeypatch.setattr(_det, "ARCHIVE_PATH", tmp_path)
    _write_state(tmp_path, "RO000A", "20260730", rms_complete=True)
    _write_state(tmp_path, "RO000B", "20260730", rms_complete=False)
    assert _night_rms_complete(_two_cam_station(), "20260730") is False


def test_night_rms_complete_defaults_true_when_state_missing(tmp_path, monkeypatch):
    # No state.json -> unknown -> treated as complete so the button stays hidden.
    monkeypatch.setattr(_det, "ARCHIVE_PATH", tmp_path)
    assert _night_rms_complete(_two_cam_station(), "20260730") is True


# ── no_clip: pending that will never resolve ─────────────────────────────────
# A camera with no colour-clip pipeline (gmnro17's plots-only Pis) or one whose
# station has archive uploads switched off pushes detections that no clip will
# ever back. Rendering those as "clip pending" promises an upload that is not
# coming, so CameraConfig.clips drives a distinct no_clip marker instead.

def test_pending_chunk_defaults_to_clip_in_transit():
    assert _pending_chunk_from_row(_row(), RMS_FIELDS)["no_clip"] is False


def test_pending_chunk_marks_no_clip_cameras():
    chunk = _pending_chunk_from_row(_row(), RMS_FIELDS, no_clip=True)
    # Still a pending marker in every other respect — same shape, same
    # thumbnail handling, still not clickable.
    assert chunk["pending"] is True
    assert chunk["filename"] is None
    assert chunk["no_clip"] is True


def test_camera_config_delivers_clips_by_default():
    assert CameraConfig(code="RO000H", cam_ip="192.168.1.10").clips is True
    assert CameraConfig(code="RO0001", cam_ip="192.168.42.10", clips=False).clips is False


# ── pruned: retention removed the video ──────────────────────────────────────
# The storage-box janitor deletes an unremarkable clip's video after 14 days
# and marks it video_pruned in state.json. Labelling that "clip pending" read
# as "still uploading" for a clip that is gone for good.

class _Night:
    def __init__(self, chunks):
        self.state_chunks = chunks


def test_pending_chunk_marks_pruned_clips():
    chunk = _pending_chunk_from_row(_row(), RMS_FIELDS, pruned=True)
    assert chunk["pending"] is True and chunk["filename"] is None
    assert chunk["pruned"] is True
    assert _pending_chunk_from_row(_row(), RMS_FIELDS)["pruned"] is False


def test_video_pruned_reads_the_nights_state_json():
    ni = _Night({"RO000Y_20260906_184716_color.mkv": {"lock": {}, "video_pruned": True},
                 "RO000Y_20260906_185142_color.mkv": {"lock": {}}})
    assert _video_pruned(ni, "RO000Y_20260906_184716_color.mkv") is True
    assert _video_pruned(ni, "RO000Y_20260906_185142_color.mkv") is False
    assert _video_pruned(ni, "missing.mkv") is False
    assert _video_pruned(ni, None) is False
    assert _video_pruned(None, "RO000Y_20260906_184716_color.mkv") is False
    assert _video_pruned(_Night(None), "x.mkv") is False
