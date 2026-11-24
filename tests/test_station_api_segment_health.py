"""Tests for station_api segment health — the colour-capture cadence watchdog.

The failure this guards against: a corrupt camera stream makes ffmpeg's segment
muxer cut on nearly every packet, so a night directory fills with thousands of
~1 s files instead of 20 s ones (RO000Y: 1543 in one hour), while every other
signal on the dashboard stays green. The mirror case is a stalling stream that
leaves multi-minute holes.

``segment_health`` is a pure function of (filenames, now) so the storm and stall
shapes can be pinned without touching a disk.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

import station_api


NOW = datetime(2026, 9, 1, 22, 0, 0)


def _names(start: datetime, count: int, spacing_s: float, cam: str = "RO000Y") -> list[str]:
    """Colour segment filenames starting at ``start``, one every ``spacing_s``."""
    return [
        f"{cam}_{(start + timedelta(seconds=spacing_s * i)).strftime('%Y%m%d_%H%M%S')}_color.mkv"
        for i in range(count)
    ]


# ── Healthy cadence ────────────────────────────────────────────────────────


class TestHealthyNight:
    """A 20 s cadence must never raise either counter, however long it runs."""

    def test_full_hour_of_20s_segments_is_clean(self):
        h = station_api.segment_health(_names(NOW - timedelta(hours=1), 180, 20), NOW)
        assert h["segments"] == 180
        assert h["short"] == 0
        assert h["stalls"] == 0
        assert h["max_gap_s"] == 20

    def test_newest_segment_timestamp_is_reported(self):
        names = _names(NOW - timedelta(minutes=10), 30, 20)
        h = station_api.segment_health(names, NOW)
        assert h["newest"] == "2026-09-01T21:59:40Z"

    def test_window_minutes_is_derived_from_the_window(self):
        h = station_api.segment_health([], NOW)
        assert h["window_min"] == 60
        h2 = station_api.segment_health([], NOW, window_s=1800)
        assert h2["window_min"] == 30

    def test_a_single_segment_has_no_spacing_to_judge(self):
        h = station_api.segment_health(_names(NOW - timedelta(minutes=5), 1, 20), NOW)
        assert h == {
            "window_min": 60,
            "segments": 1,
            "short": 0,
            "stalls": 0,
            "max_gap_s": None,
            "newest": "2026-09-01T21:55:00Z",
        }


# ── Clip storm ─────────────────────────────────────────────────────────────


class TestClipStorm:
    """The RO000Y signature: ~1 s segments, thousands per hour."""

    def test_one_second_segments_all_count_as_short(self):
        h = station_api.segment_health(_names(NOW - timedelta(hours=1), 1543, 1), NOW)
        assert h["segments"] == 1543
        assert h["short"] == 1542          # one spacing per consecutive pair
        assert h["stalls"] == 0
        assert h["max_gap_s"] == 1

    def test_storm_confined_to_part_of_the_hour_still_counts(self):
        """RO001E's shape: a healthy stretch, then a burst of short cuts."""
        healthy = _names(NOW - timedelta(minutes=60), 60, 20)     # 20 min of 20 s
        storm_start = NOW - timedelta(minutes=40)
        storm = _names(storm_start, 116, 1)
        h = station_api.segment_health(healthy + storm, NOW)
        assert h["short"] >= 115
        assert h["segments"] == len(healthy) + len(storm)

    def test_exactly_at_the_short_threshold_counts_as_short(self):
        """3 s spacing is short; the bound is inclusive."""
        h = station_api.segment_health(_names(NOW - timedelta(minutes=5), 5, 3), NOW)
        assert h["short"] == 4

    def test_just_above_the_short_threshold_does_not_count(self):
        h = station_api.segment_health(_names(NOW - timedelta(minutes=5), 5, 4), NOW)
        assert h["short"] == 0

    def test_threshold_is_injectable(self):
        names = _names(NOW - timedelta(minutes=5), 5, 4)
        h = station_api.segment_health(names, NOW, short_max_s=5)
        assert h["short"] == 4


# ── Stalls ─────────────────────────────────────────────────────────────────


class TestStalls:
    def test_two_minute_holes_count_as_stalls(self):
        h = station_api.segment_health(_names(NOW - timedelta(hours=1), 20, 120), NOW)
        assert h["stalls"] == 19
        assert h["short"] == 0
        assert h["max_gap_s"] == 120

    def test_exactly_at_the_stall_threshold_is_not_a_stall(self):
        """40 s is two missed segments' worth of slack, still tolerated."""
        h = station_api.segment_health(_names(NOW - timedelta(minutes=10), 5, 40), NOW)
        assert h["stalls"] == 0
        assert h["max_gap_s"] == 40

    def test_just_above_the_stall_threshold_is_a_stall(self):
        h = station_api.segment_health(_names(NOW - timedelta(minutes=10), 5, 41), NOW)
        assert h["stalls"] == 4

    def test_max_gap_reports_the_worst_hole_not_the_last(self):
        base = NOW - timedelta(minutes=50)
        names = [
            f"RO000Y_{(base + timedelta(seconds=s)).strftime('%Y%m%d_%H%M%S')}_color.mkv"
            for s in (0, 20, 400, 420, 440)
        ]
        h = station_api.segment_health(names, NOW)
        assert h["stalls"] == 1
        assert h["max_gap_s"] == 380

    def test_a_storm_and_a_stall_are_counted_independently(self):
        base = NOW - timedelta(minutes=30)
        names = _names(base, 10, 1) + _names(base + timedelta(seconds=600), 3, 20)
        h = station_api.segment_health(names, NOW)
        assert h["short"] == 9
        assert h["stalls"] == 1


# ── Window boundaries and junk input ───────────────────────────────────────


class TestWindow:
    def test_segments_older_than_the_window_are_dropped(self):
        old = _names(NOW - timedelta(hours=5), 100, 1)
        recent = _names(NOW - timedelta(minutes=10), 30, 20)
        h = station_api.segment_health(old + recent, NOW)
        assert h["segments"] == 30
        assert h["short"] == 0

    def test_no_phantom_gap_across_the_window_edge(self):
        """A segment just outside the window must not create a huge gap with
        the first one inside it — that would fake a stall on every poll."""
        names = (_names(NOW - timedelta(hours=3), 1, 20)
                 + _names(NOW - timedelta(minutes=5), 15, 20))
        h = station_api.segment_health(names, NOW)
        assert h["stalls"] == 0

    def test_future_timestamps_are_ignored(self):
        """A mis-clocked or mis-named artefact must not inflate the gap on
        either side of itself."""
        names = _names(NOW - timedelta(minutes=5), 15, 20) + [
            "RO000Y_20260902_235900_color.mkv",
        ]
        h = station_api.segment_health(names, NOW)
        assert h["segments"] == 15
        assert h["stalls"] == 0

    def test_unrelated_files_are_ignored(self):
        names = _names(NOW - timedelta(minutes=5), 5, 20) + [
            "state.json",
            "stacks",
            "RO000Y_20260901_215500_color.mkv.locked",
            "RO000Y_20260901_215500_timelapse.mp4",
            "readme.txt",
        ]
        h = station_api.segment_health(names, NOW)
        assert h["segments"] == 5

    def test_unsorted_input_gives_the_same_answer(self):
        names = _names(NOW - timedelta(minutes=30), 40, 1)
        assert (station_api.segment_health(list(reversed(names)), NOW)
                == station_api.segment_health(names, NOW))

    def test_empty_directory_is_zeros_not_an_error(self):
        h = station_api.segment_health([], NOW)
        assert h["segments"] == 0
        assert h["short"] == 0
        assert h["stalls"] == 0
        assert h["max_gap_s"] is None
        assert h["newest"] is None

    def test_impossible_timestamp_in_a_name_is_skipped(self):
        h = station_api.segment_health(["RO000Y_20260231_250000_color.mkv"], NOW)
        assert h["segments"] == 0


class TestClockInjection:
    """The caller injects the name→UTC converter so legacy locally-named
    artefacts land in the same clock as ``now``. Without it the whole window
    would silently read as empty on such a station."""

    def test_offset_converter_shifts_names_into_the_window(self):
        # Names three hours ahead of UTC (pre-#851 local naming, EEST).
        names = _names(NOW + timedelta(hours=3) - timedelta(minutes=10), 30, 20)
        assert station_api.segment_health(names, NOW)["segments"] == 0

        def _to_utc(date_str: str, time_str: str) -> datetime:
            return (station_api._segment_name_ts(date_str, time_str)
                    - timedelta(hours=3))

        assert station_api.segment_health(names, NOW, to_utc=_to_utc)["segments"] == 30

    def test_default_converter_treats_the_name_as_utc(self):
        assert (station_api._segment_name_ts("20260901", "213000")
                == datetime(2026, 9, 1, 21, 30, 0))


# ── Directory layout / collection ──────────────────────────────────────────


class TestNightDirLayout:
    def test_per_camera_layout_wins(self, tmp_path):
        night = "20260901"
        primary = tmp_path / "RO000Y" / night
        primary.mkdir(parents=True)
        (tmp_path / night).mkdir()
        assert (station_api._segment_health_night_dir(tmp_path, "RO000Y", night)
                == primary)

    def test_flat_layout_is_the_fallback(self, tmp_path):
        night = "20260901"
        flat = tmp_path / night
        flat.mkdir()
        assert (station_api._segment_health_night_dir(tmp_path, "RO000Y", night)
                == flat)

    def test_missing_night_dir_returns_none(self, tmp_path):
        assert station_api._segment_health_night_dir(
            tmp_path, "RO000Y", "20260901") is None


class TestPerCameraCollection:
    def test_missing_night_dir_is_idle_not_an_error(self, tmp_path):
        h = station_api._segment_health_for_camera(
            tmp_path, "RO000Y", "20260901", NOW, station_api._segment_name_ts)
        assert h["night"] is None
        assert h["segments"] == 0
        assert h["short"] == 0
        assert h["stalls"] == 0

    def test_storm_on_disk_is_counted(self, tmp_path):
        night = "20260901"
        night_dir = tmp_path / "RO000Y" / night
        night_dir.mkdir(parents=True)
        for name in _names(NOW - timedelta(minutes=30), 200, 1):
            (night_dir / name).touch()
        h = station_api._segment_health_for_camera(
            tmp_path, "RO000Y", night, NOW, station_api._segment_name_ts)
        assert h["night"] == night
        assert h["segments"] == 200
        assert h["short"] == 199

    def test_collection_covers_every_configured_camera(self, tmp_path, monkeypatch, station_patch):
        utc_now = datetime.now(timezone.utc)
        night = station_api._night_date(
            utc_now.strftime("%Y%m%d"), utc_now.strftime("%H%M%S"))
        cap = tmp_path / "color_capture"
        (cap / "RO000Y" / night).mkdir(parents=True)
        cfg = {"videocapture_path": str(cap),
               "stations": {"RO000Y": {}, "RO0010": {}}}
        station_patch("load_config", lambda: cfg)
        station_patch("capture_base", lambda: cap)
        health = station_api._collect_segment_health()
        assert set(health) == {"RO000Y", "RO0010"}
        assert health["RO000Y"]["night"] == night     # dir exists
        assert health["RO0010"]["night"] is None      # dir absent, still reported

    def test_collection_never_raises(self, monkeypatch, station_patch):
        def _boom():
            raise OSError("capture mount gone")

        station_patch("load_config", _boom)
        assert station_api._collect_segment_health() == {}


def test_thresholds_are_named_constants():
    assert station_api.SEGMENT_HEALTH_WINDOW_S == 3600
    assert station_api.SEGMENT_SHORT_MAX_S == 3
    assert station_api.SEGMENT_STALL_MIN_S == 40


def test_heartbeat_payload_forwards_segment_health():
    """Push-model stations are the ones the dashboard cannot poll, so the
    field has to survive the pusher's whitelist projection."""
    rovimen_pusher = pytest.importorskip("rovimen_pusher")
    payload = rovimen_pusher.build_heartbeat_payload(
        {"online": True, "segment_health": {"RO000Y": {"short": 900}}})
    assert payload["segment_health"] == {"RO000Y": {"short": 900}}
    # Old station bundles do not send the field at all.
    assert rovimen_pusher.build_heartbeat_payload({"online": True})["segment_health"] == {}
