"""Clip -> meteor resolution for archive-served nights.

Covers the three tiers in ``clip_detections`` plus the ``detections.db``
lookup they lean on, and the invariant that matters most: a night whose
``state.json`` lock was cleared by the station's dawn stale-lock sweep still
resolves a meteor time for every clip that holds one.
"""

from __future__ import annotations

import sqlite3

import pytest

import clip_detections
import detection_db


# ---------------------------------------------------------------------------
# chunk_span
# ---------------------------------------------------------------------------

def test_span_of_regular_clip_is_one_segment():
    start, end = clip_detections.chunk_span("RO0014_20260525_215813_color.mkv")
    assert start.isoformat() == "2026-05-25T21:58:13+00:00"
    assert (end - start).total_seconds() == 20


def test_span_honours_a_custom_segment_length():
    start, end = clip_detections.chunk_span(
        "RO0014_20260525_215813_color.mkv", segment_secs=30
    )
    assert (end - start).total_seconds() == 30


def test_stitched_clip_spans_to_its_second_stamp():
    """Matches overlay_details._clip_span, so the panel names the same meteor
    the overlay bar burned in."""
    start, end = clip_detections.chunk_span(
        "RO0014_20260525_221623_221643_color.mkv"
    )
    assert start.isoformat() == "2026-05-25T22:16:23+00:00"
    assert end.isoformat() == "2026-05-25T22:16:43+00:00"


def test_stitched_clip_crossing_midnight_rolls_the_end_forward():
    start, end = clip_detections.chunk_span(
        "RO0014_20260525_235955_000015_color.mkv"
    )
    assert start.isoformat() == "2026-05-25T23:59:55+00:00"
    assert end.isoformat() == "2026-05-26T00:00:15+00:00"


def test_faststart_mp4_survivor_still_parses():
    """The janitor reaps the .mkv on aged nights and leaves the .mp4 alone."""
    assert clip_detections.chunk_span("RO0014_20260525_215813_color.mp4") is not None


@pytest.mark.parametrize("name", [
    "not-a-clip.mkv",
    "RO0014_20260525_215813_color.webm",
    "RO0014_2026052_215813_color.mkv",
    "RO0014_20260525_2158_color.mkv",
])
def test_unparseable_names_have_no_span(name):
    assert clip_detections.chunk_span(name) is None


# ---------------------------------------------------------------------------
# match_by_window
# ---------------------------------------------------------------------------

def test_window_match_places_each_detection_in_its_clip():
    matched = clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv", "RO0014_20260525_223834_color.mkv"],
        ["2026-05-25T21:58:26", "2026-05-25T22:38:37"],
    )
    assert matched == {
        "RO0014_20260525_215813_color.mkv": "2026-05-25T21:58:26",
        "RO0014_20260525_223834_color.mkv": "2026-05-25T22:38:37",
    }


def test_window_match_skips_clips_holding_no_detection():
    matched = clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv", "RO0014_20260525_230000_color.mkv"],
        ["2026-05-25T21:58:26"],
    )
    assert "RO0014_20260525_230000_color.mkv" not in matched


def test_a_detection_just_past_the_segment_does_not_match():
    """21 s into a 20 s clip belongs to the next segment, not this one."""
    assert clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv"], ["2026-05-25T21:58:34"]
    ) == {}


def test_clip_holding_two_meteors_takes_the_earlier():
    matched = clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv"],
        ["2026-05-25T21:58:29", "2026-05-25T21:58:15"],
    )
    assert matched["RO0014_20260525_215813_color.mkv"] == "2026-05-25T21:58:15"


def test_matched_time_is_passed_through_verbatim():
    """The browser joins clip.meteor_time to detection.time_utc as a string;
    reformatting it here would break a match the source had right."""
    matched = clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv"], ["2026-05-25T21:58:26.947816"]
    )
    assert matched["RO0014_20260525_215813_color.mkv"] == "2026-05-25T21:58:26.947816"


def test_timezone_aware_detection_times_are_accepted():
    matched = clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv"], ["2026-05-25T21:58:26Z"]
    )
    assert matched == {"RO0014_20260525_215813_color.mkv": "2026-05-25T21:58:26Z"}


def test_unparseable_detection_times_are_ignored():
    assert clip_detections.match_by_window(
        ["RO0014_20260525_215813_color.mkv"], ["", None, "yesterday"]
    ) == {}


# ---------------------------------------------------------------------------
# resolve_meteor_times — tier fallthrough
# ---------------------------------------------------------------------------

NAMES = [
    "RO0014_20260525_215813_color.mkv",
    "RO0014_20260525_223834_color.mkv",
]


def test_surviving_lock_wins_and_costs_no_fallback():
    """A night inside stale_lock_days must not pay for a DB query or an
    SSHFS read."""
    calls = []

    def _db():
        calls.append("db")
        return {}

    def _dets():
        calls.append("archive")
        return []

    resolved = clip_detections.resolve_meteor_times(
        NAMES,
        lock_times={
            NAMES[0]: "2026-05-25T21:58:26.508664",
            NAMES[1]: "2026-05-25T22:38:37.011168",
        },
        db_times=_db,
        detection_times=_dets,
    )
    assert resolved[NAMES[0]] == "2026-05-25T21:58:26.508664"
    assert calls == []


def test_swept_lock_falls_through_to_the_detections_db():
    resolved = clip_detections.resolve_meteor_times(
        NAMES,
        lock_times={NAMES[0]: None, NAMES[1]: None},
        db_times={
            NAMES[0]: "2026-05-25T21:58:26",
            NAMES[1]: "2026-05-25T22:38:37",
        },
        detection_times=lambda: pytest.fail("window tier ran despite a DB hit"),
    )
    assert resolved == {
        NAMES[0]: "2026-05-25T21:58:26",
        NAMES[1]: "2026-05-25T22:38:37",
    }


def test_night_predating_chunk_file_falls_through_to_window_matching():
    """May/June nights have no chunk_file in the DB at all; the archive's own
    radiants still place every meteor."""
    resolved = clip_detections.resolve_meteor_times(
        NAMES,
        lock_times={},
        db_times={},
        detection_times=["2026-05-25T21:58:26", "2026-05-25T22:38:37"],
    )
    assert resolved == {
        NAMES[0]: "2026-05-25T21:58:26",
        NAMES[1]: "2026-05-25T22:38:37",
    }


def test_tiers_combine_per_clip():
    """One clip keeps its lock, one is recovered from the DB, one from the
    archive window — within a single night."""
    third = "RO0014_20260525_230500_color.mkv"
    resolved = clip_detections.resolve_meteor_times(
        [*NAMES, third],
        lock_times={NAMES[0]: "2026-05-25T21:58:26.508664"},
        db_times={NAMES[1]: "2026-05-25T22:38:37"},
        detection_times=["2026-05-25T23:05:11"],
    )
    assert resolved == {
        NAMES[0]: "2026-05-25T21:58:26.508664",
        NAMES[1]: "2026-05-25T22:38:37",
        third: "2026-05-25T23:05:11",
    }


def test_clip_with_no_meteor_anywhere_resolves_to_none():
    resolved = clip_detections.resolve_meteor_times(
        NAMES, lock_times={}, db_times={}, detection_times=[]
    )
    assert resolved == {NAMES[0]: None, NAMES[1]: None}


def test_every_requested_clip_gets_an_entry():
    resolved = clip_detections.resolve_meteor_times(NAMES)
    assert set(resolved) == set(NAMES)


def test_empty_lock_string_is_treated_as_missing():
    resolved = clip_detections.resolve_meteor_times(
        [NAMES[0]],
        lock_times={NAMES[0]: ""},
        db_times={NAMES[0]: "2026-05-25T21:58:26"},
    )
    assert resolved[NAMES[0]] == "2026-05-25T21:58:26"


# ---------------------------------------------------------------------------
# detection_db.chunk_meteor_times
# ---------------------------------------------------------------------------

def _seed_db(tmp_path, rows):
    path = tmp_path / "detections.db"
    con = detection_db.open_db(path)
    con.executemany(
        "INSERT OR REPLACE INTO detections "
        "(cam, date, ff_file, meteor_no, time_utc, chunk_file) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    con.commit()
    con.close()
    return path


def test_chunk_meteor_times_maps_clip_to_detection(tmp_path):
    path = _seed_db(tmp_path, [
        ("RO0014", "20260731", "ff1", 1, "2026-07-31T19:09:01",
         "RO0014_20260731_190842_color.mkv"),
        ("RO0014", "20260731", "ff2", 2, "2026-07-31T19:10:55",
         "RO0014_20260731_191042_color.mkv"),
    ])
    assert detection_db.chunk_meteor_times("RO0014", "20260731", path) == {
        "RO0014_20260731_190842_color.mkv": "2026-07-31T19:09:01",
        "RO0014_20260731_191042_color.mkv": "2026-07-31T19:10:55",
    }


def test_chunk_meteor_times_takes_the_earliest_of_a_shared_clip(tmp_path):
    path = _seed_db(tmp_path, [
        ("RO0014", "20260731", "ff1", 1, "2026-07-31T19:09:05",
         "RO0014_20260731_190842_color.mkv"),
        ("RO0014", "20260731", "ff2", 2, "2026-07-31T19:08:50",
         "RO0014_20260731_190842_color.mkv"),
    ])
    assert detection_db.chunk_meteor_times("RO0014", "20260731", path) == {
        "RO0014_20260731_190842_color.mkv": "2026-07-31T19:08:50",
    }


def test_chunk_meteor_times_skips_rows_with_no_clip(tmp_path):
    """Pre-July rows carry a detection but no chunk_file."""
    path = _seed_db(tmp_path, [
        ("RO0014", "20260525", "ff1", 1, "2026-05-25T21:58:26", None),
        ("RO0014", "20260525", "ff2", 2, "2026-05-25T22:38:37", ""),
    ])
    assert detection_db.chunk_meteor_times("RO0014", "20260525", path) == {}


def test_chunk_meteor_times_is_scoped_to_one_camera_night(tmp_path):
    path = _seed_db(tmp_path, [
        ("RO0014", "20260731", "ff1", 1, "2026-07-31T19:09:01",
         "RO0014_20260731_190842_color.mkv"),
        ("RO0015", "20260731", "ff2", 1, "2026-07-31T19:09:01",
         "RO0015_20260731_190842_color.mkv"),
        ("RO0014", "20260730", "ff3", 1, "2026-07-30T19:09:01",
         "RO0014_20260730_190842_color.mkv"),
    ])
    assert list(detection_db.chunk_meteor_times("RO0014", "20260731", path)) == [
        "RO0014_20260731_190842_color.mkv"
    ]


def test_chunk_meteor_times_degrades_quietly_without_a_db(tmp_path):
    """A missing pointer must not fail the night's chunk listing."""
    assert detection_db.chunk_meteor_times(
        "RO0014", "20260731", tmp_path / "nope.db"
    ) == {}


def test_chunk_meteor_times_degrades_quietly_on_a_corrupt_db(tmp_path):
    path = tmp_path / "detections.db"
    path.write_bytes(b"not a database at all")
    assert detection_db.chunk_meteor_times("RO0014", "20260731", path) == {}


@pytest.mark.parametrize("cam,date", [("", "20260731"), ("RO0014", "")])
def test_chunk_meteor_times_rejects_empty_keys(cam, date, tmp_path):
    assert detection_db.chunk_meteor_times(cam, date, tmp_path / "x.db") == {}


# ---------------------------------------------------------------------------
# End to end: the regression this whole module exists for
# ---------------------------------------------------------------------------

def test_swept_night_still_resolves_every_clip(tmp_path):
    """A night past stale_lock_days: state.json says lock=null for every clip,
    but detections.db still knows which meteor each one holds."""
    clips = [
        "MD0001_20260813_182132_color.mkv",
        "MD0001_20260813_183232_color.mkv",
    ]
    path = _seed_db(tmp_path, [
        ("MD0001", "20260813", "ff1", 1, "2026-08-13T18:21:48", clips[0]),
        ("MD0001", "20260813", "ff2", 1, "2026-08-13T18:32:34", clips[1]),
    ])
    resolved = clip_detections.resolve_meteor_times(
        clips,
        lock_times={clips[0]: None, clips[1]: None},
        db_times=lambda: detection_db.chunk_meteor_times("MD0001", "20260813", path),
        detection_times=lambda: [],
    )
    assert resolved == {
        clips[0]: "2026-08-13T18:21:48",
        clips[1]: "2026-08-13T18:32:34",
    }


def test_resolved_time_seconds_match_the_detection_row():
    """dashboard-rms.js keys detections by time_utc and looks them up with
    meteor_time truncated to the second — the two must agree there."""
    lock_form = "2026-08-28T18:16:50.508664"
    radiants_form = "2026-08-28T18:16:50"
    assert lock_form[:19] == radiants_form[:19]

    resolved = clip_detections.resolve_meteor_times(
        ["RO0014_20260828_181644_color.mkv"],
        lock_times={},
        detection_times=[radiants_form],
    )
    assert resolved["RO0014_20260828_181644_color.mkv"][:19] == radiants_form[:19]


def test_sqlite_error_type_is_actually_caught(tmp_path):
    """Guard the except clause: _connect raises sqlite3.Error subclasses."""
    path = tmp_path / "detections.db"
    path.write_bytes(b"\x00" * 64)
    with pytest.raises(sqlite3.Error):
        con = sqlite3.connect(str(path))
        con.execute("SELECT * FROM detections").fetchall()
