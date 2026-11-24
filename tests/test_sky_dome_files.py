"""Sky-dome files on the VPS cache volume: written whole or not at all, and
no per-night frame archive.

The dome timelapse MP4 is encoded to ``<night>.mp4.part`` and renamed into
place, because the scheduler writes the night's attempt marker before the
encode and never retries it: an MP4 encoded in place and cut short stayed
listed and served, broken, for the 92-day retention. The ff_max frame
archive (``sky_dome_archive/<night>/``) fed a stitcher nothing called since
the timelapse is built from the cameras' own timelapses.
"""

from __future__ import annotations

import math
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import capture_state
import routes.sky_dome as sky_dome


def _frames(tmp_path: Path) -> Path:
    frames = tmp_path / "dome_frames"
    frames.mkdir()
    (frames / "0000.png").write_bytes(b"png")
    return frames


def _fake_ffmpeg(monkeypatch, *, returncode: int = 0, raises: Exception | None = None):
    """Stand-in for subprocess.run: writes half an MP4 to the output path
    ffmpeg was given, then fails the way the real one does."""
    seen: dict = {}

    def run(cmd, **kwargs):
        seen["cmd"] = cmd
        Path(cmd[-1]).write_bytes(b"\0\0\0\x18ftypmp42" if raises or returncode else
                                  b"\0\0\0\x18ftypmp42 complete")
        if raises is not None:
            raise raises
        return subprocess.CompletedProcess(cmd, returncode, b"", b"boom")

    monkeypatch.setattr(sky_dome.subprocess, "run", run)
    return seen


def test_finished_encode_is_renamed_into_place(tmp_path, monkeypatch):
    seen = _fake_ffmpeg(monkeypatch)
    out = tmp_path / "20261121.mp4"
    assert sky_dome._encode_dome_timelapse(_frames(tmp_path), out, "gmnro07/20261121")
    assert out.read_bytes().endswith(b"complete")
    assert seen["cmd"][-3:-1] == ["-f", "mp4"]  # the .part name carries no format
    assert sorted(p.name for p in tmp_path.iterdir()) == ["20261121.mp4", "dome_frames"]


@pytest.mark.parametrize("failure", [
    {"raises": subprocess.TimeoutExpired("ffmpeg", 300)},
    {"returncode": 1},
    {"raises": FileNotFoundError("ffmpeg")},
])
def test_failed_encode_leaves_no_mp4(tmp_path, monkeypatch, failure):
    _fake_ffmpeg(monkeypatch, **failure)
    out = tmp_path / "20261121.mp4"
    assert sky_dome._encode_dome_timelapse(_frames(tmp_path), out, "gmnro07/20261121") is False
    assert sorted(p.name for p in tmp_path.iterdir()) == ["dome_frames"]


def test_a_part_left_by_a_killed_worker_is_never_listed(tmp_path):
    """A SIGKILLed worker leaves ``<night>.mp4.part``: the dates route and both
    prunes match ``*.mp4`` only, so it is never listed or served, and the
    dome prune ages it out with the night's MP4s."""
    (tmp_path / "20261121.mp4.part").write_bytes(b"half")
    assert list(tmp_path.glob("*.mp4")) == []


def test_ff_max_frames_are_no_longer_archived():
    source = Path(sky_dome.__file__).read_text()
    assert "sky_dome_archive" not in source
    assert "_build_sky_dome_timelapse(" not in source


def _noaa_sun_alt(lat: float, lon: float, now: datetime) -> float:
    """The sky-dome module's own NOAA solar altitude, as it stood before it
    switched to capture_state.sun_altitude_deg."""
    year, month, day = now.year, now.month, now.day
    if month <= 2:
        year -= 1
        month += 12
    a = int(year / 100)
    b = 2 - a + int(a / 4)
    day_frac = (now.hour + now.minute / 60.0 + now.second / 3600.0) / 24.0
    jd = (int(365.25 * (year + 4716)) + int(30.6001 * (month + 1))
          + day + day_frac + b - 1524.5)
    jc = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + jc * (36000.76983 + 0.0003032 * jc)) % 360
    m = math.radians((357.52911 + jc * (35999.05029 - 0.0001537 * jc)) % 360)
    c = (math.sin(m) * (1.914602 - jc * (0.004817 + 0.000014 * jc))
         + math.sin(2 * m) * (0.019993 - 0.000101 * jc)
         + math.sin(3 * m) * 0.000289)
    omega = 125.04 - 1934.136 * jc
    sun_app = l0 + c - 0.00569 - 0.00478 * math.sin(math.radians(omega))
    obliq = (23.0 + (26.0 + (21.448 - jc * (46.815 + jc * (0.00059 - jc * 0.001813)))
                     / 60.0) / 60.0 + 0.00256 * math.cos(math.radians(omega)))
    dec = math.asin(math.sin(math.radians(obliq)) * math.sin(math.radians(sun_app)))
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
            + jc * jc * (0.000387933 - jc / 38710000.0)) % 360
    ra = math.degrees(math.atan2(
        math.cos(math.radians(obliq)) * math.sin(math.radians(sun_app)),
        math.cos(math.radians(sun_app)))) % 360
    ha = math.radians((gmst + lon - ra + 540) % 360 - 180)
    lat_r = math.radians(lat)
    sin_alt = (math.sin(lat_r) * math.sin(dec)
               + math.cos(lat_r) * math.cos(dec) * math.cos(ha))
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


@pytest.mark.parametrize("lat,lon", [
    (52.5, 13.4),    # gmnde01 (Berlin)
    (45.9, 28.2),    # gmnmd01 (Cahul)
    (44.4, 26.1),    # gmnro07 (București)
    (47.2, 27.6),    # gmnro12 (Iași)
    (64.1, -21.9),   # far north and west of the fleet
])
def test_capture_state_sun_altitude_matches_the_dome_formula(lat, lon):
    """capture_state.sun_altitude_deg replaces the dome's own copy. Over a
    year of hourly samples the two agree to 0.01 deg (measured worst case
    0.007 deg), far inside the 0 and -6 deg cadence and dawn thresholds."""
    t = datetime(2026, 1, 1, 0, 7, 30, tzinfo=timezone.utc)
    worst = 0.0
    for _ in range(366 * 24):
        worst = max(worst, abs(capture_state.sun_altitude_deg(lat, lon, t)
                               - _noaa_sun_alt(lat, lon, t)))
        t += timedelta(hours=1)
    assert worst < 0.01
