"""Sun-scheduled colour capture for cameras that never run RMS.

Regression tests for 2026-09-17: capture start was exclusively FF-triggered
(inotify on RMS CapturedFiles), so the GH662 spectro pair (``rms: false``)
had no trigger at all once the hand-placed station hack was clobbered by a
bundle update — zero chunks captured on night 20260916 while RO000A wrote
2004. These cameras now open/close their capture window on solar altitude
against the config's ``night_sun_alt``.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "rovimen-scripts"))

from color_capture import _sun_altitude_deg, _sun_capture_action  # noqa: E402


GHIRDOVENI = (44.607966, 25.282230)


class TestSunAltitude:
    """NOAA low-precision solar altitude, anchored to checkable geometry."""

    def test_solar_noon_matches_declination_geometry(self):
        # Noon altitude = 90 - |lat - dec|; Sep 17 dec ~ +2.2 deg -> ~47.2.
        alt = _sun_altitude_deg(*GHIRDOVENI,
                                datetime(2026, 9, 17, 10, 13, tzinfo=timezone.utc))
        assert 45.0 < alt < 49.0

    def test_civil_dawn_crossing_matches_the_dome_scheduler(self):
        # The dome scheduler observed gmnro01's -6 deg crossing at ~03:25 UTC
        # on 2026-09-17 (journal, "dawn detected" era).
        alt = _sun_altitude_deg(*GHIRDOVENI,
                                datetime(2026, 9, 17, 3, 25, tzinfo=timezone.utc))
        assert -9.0 < alt < -4.0

    def test_evening_capture_window_is_open_at_the_fireball_time(self):
        # 2026-09-16 18:14 UTC (21:14 EEST) was deep dusk — well below the
        # station's night_sun_alt of -12.
        alt = _sun_altitude_deg(*GHIRDOVENI,
                                datetime(2026, 9, 16, 18, 14, tzinfo=timezone.utc))
        assert alt < -12.0

    def test_midnight_is_deeply_negative(self):
        alt = _sun_altitude_deg(*GHIRDOVENI,
                                datetime(2026, 9, 16, 22, 0, tzinfo=timezone.utc))
        assert alt < -30.0


class TestCaptureAction:
    def test_night_starts_a_stopped_camera(self):
        assert _sun_capture_action(-12.5, -12.0, running=False) == "start"

    def test_night_leaves_a_running_camera_alone(self):
        assert _sun_capture_action(-12.5, -12.0, running=True) is None

    def test_day_stops_a_running_camera(self):
        assert _sun_capture_action(-10.5, -12.0, running=True) == "stop"

    def test_day_leaves_a_stopped_camera_alone(self):
        assert _sun_capture_action(20.0, -12.0, running=False) is None

    def test_hysteresis_band_never_flaps(self):
        # Between night_alt and night_alt + 1 nothing changes in either
        # direction, so jitter around the threshold can't cycle ffmpeg.
        assert _sun_capture_action(-11.5, -12.0, running=True) is None
        assert _sun_capture_action(-11.5, -12.0, running=False) is None


# ── Station location ─────────────────────────────────────────────────────────
#
# The scheduler finds the location the way the colour pipeline does: camera
# latitude/longitude, then the station's, then overlay.coords. It used to read
# only the top level, so a station with just overlay.coords got the moon boost
# in its colour while its sun-scheduled cameras never started.

import logging  # noqa: E402

import pytest  # noqa: E402

import color_capture  # noqa: E402


def _capture(cfg: dict) -> color_capture.ColorCapture:
    cc = object.__new__(color_capture.ColorCapture)
    cc.cfg = cfg
    cc.log = logging.getLogger('test_sun_schedule')
    cc._ffmpeg = {}
    cc._tee_watchers = {}
    cc.color_tee = False
    cc._last_ff = {}
    cc._disk_paused = set()
    cc.started = []
    cc._start_ffmpeg = lambda sid: cc.started.append(sid)
    cc._stop_ffmpeg = lambda sid: None
    return cc


@pytest.fixture
def sun_at(monkeypatch):
    calls = []

    def fake(lat, lon, when):
        calls.append((round(lat, 3), round(lon, 3)))
        return -20.0

    monkeypatch.setattr(color_capture, '_sun_altitude_deg', fake)
    return calls


class TestScheduleLocation:
    def test_overlay_coords_alone_start_the_camera(self, sun_at):
        cc = _capture({'stations': {'GH662S': {'rms': False}},
                       'overlay': {'coords': '45.30N  26.11E'}})
        cc._schedule_sun_captures()
        assert cc.started == ['GH662S']
        assert sun_at == [(45.30, 26.11)]

    def test_camera_location_wins_over_the_station(self, sun_at):
        cc = _capture({'latitude': 44.0, 'longitude': 25.0,
                       'stations': {'GH662S': {'rms': False, 'latitude': 45.5,
                                               'longitude': 26.5},
                                    'GH662W': {'rms': False}}})
        cc._schedule_sun_captures()
        assert sorted(cc.started) == ['GH662S', 'GH662W']
        assert sorted(sun_at) == [(44.0, 25.0), (45.5, 26.5)]

    def test_no_location_anywhere_skips_with_a_warning(self, sun_at, caplog):
        cc = _capture({'stations': {'GH662S': {'rms': False}}})
        with caplog.at_level(logging.WARNING):
            cc._schedule_sun_captures()
        assert cc.started == []
        assert sun_at == []
        assert 'GH662S' in caplog.text

    def test_rms_cameras_are_not_sun_scheduled(self, sun_at):
        cc = _capture({'latitude': 44.0, 'longitude': 25.0, 'stations': {'RO000A': {}}})
        cc._schedule_sun_captures()
        assert cc.started == [] and sun_at == []
