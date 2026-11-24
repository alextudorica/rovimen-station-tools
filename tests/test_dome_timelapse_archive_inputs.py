"""The VPS dome timelapse reads its per-camera timelapses from the archive.

It used to ask each station for ``/api/timelapses`` and download the MP4s from
the station. During dawn that request timed out on busy stations (gmnro05
Bistrița, gmnro08 Brașov, gmnro18 Bisoca) and logged a full traceback, 23 a
day on 2026-10-04. Uploaded artifacts are served from the archive, never the
station (the development guide), so the build now takes the archive-only listing and reads
``<archive>/<cam>/<date>/timelapse/<file>``; a timelapse that is not uploaded
yet is simply absent and the scheduler retries.
"""

from __future__ import annotations

from pathlib import Path

import routes.sky_dome as sky_dome

ROOT = Path("/srv/rovimen/archive")


def _entry(date: str, filename: str) -> dict:
    return {"date": date, "filename": filename, "night_stack": None, "source": "archive"}


def test_each_camera_maps_to_its_archived_timelapse_for_the_night():
    listing = {
        "RO0003": [_entry("20261004", "RO0003_20261004_timelapse.mp4"),
                   _entry("20261003", "RO0003_20261003_timelapse.mp4")],
        "RO0004": [_entry("20261004", "RO0004_20261004_timelapse.mp4")],
    }
    got = sky_dome._archived_night_timelapses(listing, ["RO0003", "RO0004"], "20261004", ROOT)
    assert got == {
        "RO0003": ROOT / "RO0003" / "20261004" / "timelapse" / "RO0003_20261004_timelapse.mp4",
        "RO0004": ROOT / "RO0004" / "20261004" / "timelapse" / "RO0004_20261004_timelapse.mp4",
    }


def test_camera_without_an_uploaded_timelapse_is_absent():
    listing = {"RO0003": [_entry("20261003", "RO0003_20261003_timelapse.mp4")]}
    assert sky_dome._archived_night_timelapses(
        listing, ["RO0003", "RO0005"], "20261004", ROOT) == {}


def test_unsafe_filenames_are_skipped():
    listing = {"RO0003": [_entry("20261004", "../../etc/passwd")],
               "RO0004": [_entry("20261004", "")]}
    assert sky_dome._archived_night_timelapses(
        listing, ["RO0003", "RO0004"], "20261004", ROOT) == {}


def test_dome_module_never_asks_a_station_for_its_timelapses():
    source = Path(sky_dome.__file__).read_text()
    assert '"/api/timelapses"' not in source
    assert '"color_timelapse"' not in source
