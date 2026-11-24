"""Tests for the detection-index staleness surface.

A station whose detection feed has wedged keeps heartbeating, so the overview
showed it online and healthy while nothing new reached the map — how a
five-camera site went two weeks without a single detection landing before anyone
noticed (2026-08-09). These cover the newest-night-per-camera query and the
age/flag derivation the overview payload exposes.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import detection_db
from routes.overview import (
    _DET_STALE_DAYS,
    _OVERCAST_CLOUD_FRACTION,
    _detections_age_days,
    _station_cloud_fraction,
    _weather_hint,
)


def _seed(path: Path, rows: list[tuple[str, str]]) -> None:
    detection_db.open_db(path).close()
    detection_db.upsert_detections(
        [
            {"cam": cam, "date": date,
             "ff_file": f"FF_{cam}_{date}_000.fits", "meteor_no": 1}
            for cam, date in rows
        ],
        source="push",
        path=path,
    )


class TestLatestDateByCam:
    def test_returns_newest_night_per_camera(self, tmp_path):
        db = tmp_path / "detections.db"
        _seed(db, [
            ("RO0003", "20260809"), ("RO0003", "20260824"),
            ("RO0004", "20260731"),
        ])
        assert detection_db.latest_date_by_cam(path=db) == {
            "RO0003": "20260824",
            "RO0004": "20260731",
        }

    def test_missing_db_is_empty_not_an_error(self, tmp_path):
        assert detection_db.latest_date_by_cam(path=tmp_path / "absent.db") == {}

    def test_empty_db_is_empty(self, tmp_path):
        db = tmp_path / "detections.db"
        detection_db.open_db(db).close()
        assert detection_db.latest_date_by_cam(path=db) == {}


class TestDetectionsAgeDays:
    def _ymd(self, days_ago: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")

    def test_last_night_is_one_day_old(self):
        assert _detections_age_days(self._ymd(1)) == 1

    def test_a_wedged_feed_reads_as_many_days(self):
        assert _detections_age_days(self._ymd(16)) == 16

    def test_unknown_when_never_indexed(self):
        assert _detections_age_days(None) is None

    def test_unparseable_date_does_not_raise(self):
        assert _detections_age_days("not-a-date") is None

    def test_a_healthy_station_never_trips_the_flag(self):
        # Nights are indexed after the following dawn, so "yesterday" is the
        # freshest a healthy station can look — the threshold must clear it.
        assert _detections_age_days(self._ymd(1)) <= _DET_STALE_DAYS

    def test_threshold_catches_a_multi_day_gap(self):
        assert _detections_age_days(self._ymd(3)) > _DET_STALE_DAYS


class TestLatestSkyByCam:
    def test_returns_newest_night_metrics_per_camera(self, tmp_path):
        db = tmp_path / "detections.db"
        detection_db.open_db(db).close()
        detection_db.upsert_night_sky(
            [
                {"cam": "RO0003", "night": "20260809",
                 "n_ff": 900, "median_stars": 45.0, "cloud_fraction": 0.05},
                {"cam": "RO0003", "night": "20260824",
                 "n_ff": 900, "median_stars": 3.0, "cloud_fraction": 0.92},
                {"cam": "RO0004", "night": "20260731",
                 "n_ff": 800, "median_stars": 30.0, "cloud_fraction": 0.2},
            ],
            path=db,
        )
        by_cam = detection_db.latest_sky_by_cam(path=db)
        # RO0003 → the 08-24 (newest) row, not the clear 08-09 one.
        assert by_cam["RO0003"]["night"] == "20260824"
        assert by_cam["RO0003"]["cloud_fraction"] == 0.92
        assert by_cam["RO0004"]["cloud_fraction"] == 0.2

    def test_missing_db_is_empty(self, tmp_path):
        assert detection_db.latest_sky_by_cam(path=tmp_path / "absent.db") == {}

    def test_rows_without_cam_or_night_are_skipped(self, tmp_path):
        db = tmp_path / "detections.db"
        detection_db.open_db(db).close()
        # Missing cam and missing night are both rejected by the upsert.
        written = detection_db.upsert_night_sky(
            [
                {"cam": "", "night": "20260824", "cloud_fraction": 1.0},
                {"cam": "RO0009", "night": "", "cloud_fraction": 1.0},
                {"cam": "RO0009", "night": "20260824", "cloud_fraction": 0.8},
            ],
            path=db,
        )
        assert written == 1
        assert set(detection_db.latest_sky_by_cam(path=db)) == {"RO0009"}


class _Cam:
    """Minimal stand-in for a StationConfig camera (only .code is read)."""
    def __init__(self, code: str):
        self.code = code


class TestStationCloudFraction:
    def test_takes_the_max_across_cameras(self):
        by_cam = {
            "RO0010": {"cloud_fraction": 0.1},
            "RO000Y": {"cloud_fraction": 0.85},
        }
        cams = [_Cam("RO0010"), _Cam("RO000Y")]
        assert _station_cloud_fraction(cams, by_cam) == 0.85

    def test_none_when_no_camera_has_sky_data(self):
        assert _station_cloud_fraction([_Cam("RO0010")], {}) is None

    def test_ignores_cameras_without_a_fraction(self):
        by_cam = {"RO0010": {"cloud_fraction": None},
                  "RO000Y": {"cloud_fraction": 0.7}}
        cams = [_Cam("RO0010"), _Cam("RO000Y")]
        assert _station_cloud_fraction(cams, by_cam) == 0.7


class TestWeatherHint:
    def test_overcast_only_when_stale_and_clouded(self):
        assert _weather_hint(True, 0.9) == "overcast"

    def test_no_hint_when_not_stale_even_if_clouded(self):
        # A healthy station is never relabelled as weather.
        assert _weather_hint(False, 0.99) is None

    def test_no_hint_when_stale_but_sky_was_clear(self):
        # Clear but quiet stays 'stale' — a real (benign) thing to notice.
        assert _weather_hint(True, 0.1) is None

    def test_no_hint_when_cloud_fraction_unknown(self):
        assert _weather_hint(True, None) is None

    def test_threshold_is_strictly_above_the_cutoff(self):
        assert _weather_hint(True, _OVERCAST_CLOUD_FRACTION) is None
        assert _weather_hint(True, _OVERCAST_CLOUD_FRACTION + 0.01) == "overcast"
