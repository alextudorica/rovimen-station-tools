"""Tests for _analyze_recalib_json's pointing-drift figure (station_api.py).

The bug: drift was fitted to RA_d/dec_d. A camera is bolted to the ground, so
what stays fixed while it is not moving is azimuth and altitude -- the RA/Dec
of the field centre sweep across the night because the Earth turns. Whether
that sweep appears in the file at all depends on the RMS version, because
some write each recalibrated platepar at its own frame epoch and some at one
shared reference epoch:

    DE001E  JD spread 6.735 h over the night -> RA_d swept 333 deg to 75 deg
    RO000M  JD spread 0.000 h over the night -> RA_d moved 0.02 deg

Both cameras were steady; DE001E's azimuth held to 0.001 deg all night. The
old metric read 19.78 arcmin/h for DE001E and 0.08 for RO000M, so identical
stability reported 250x apart and the fleet column could not be sorted.
"""

from __future__ import annotations

import math
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import station_api as sa  # noqa: E402


def _night(n=60, *, hours=8.0, az=136.2646, alt=49.2811, rot=-98.96,
           az_drift_deg=0.0, sweep_ra=False, epoch_per_frame=True,
           recalibrated=True):
    """A synthetic platepars_all_recalibrated.json.

    ``sweep_ra`` reproduces the per-frame-epoch stations, where RA_d advances
    with sidereal time while the camera itself has not moved.
    """
    out = {}
    base_jd = 2461304.3103730786
    for i in range(n):
        frac = i / (n - 1)
        t = datetime(2026, 9, 20, 18, 0, 0) + timedelta(seconds=frac * hours * 3600)
        key = f'FF_DE001E_{t:%Y%m%d_%H%M%S}_{i:03d}_0000000.fits'
        out[key] = {
            'JD': base_jd + (frac * hours / 24.0 if epoch_per_frame else 0.0),
            'RA_d': (333.25 + frac * 101.0) % 360 if sweep_ra else 300.82,
            'dec_d': 17.9,
            'az_centre': az + frac * az_drift_deg,
            'alt_centre': alt,
            'rotation_from_horiz': rot,
            'auto_recalibrated': recalibrated,
        }
    return out


class TestSkyRotationIsNotDrift:

    def test_a_steady_camera_whose_ra_sweeps_reports_no_drift(self):
        """The DE001E case. RA_d crosses 100 degrees of sky; azimuth does not
        move; the answer must be ~0."""
        s = sa._analyze_recalib_json(_night(sweep_ra=True, epoch_per_frame=True))
        assert s['pointing_arcmin_per_h'] == pytest.approx(0.0, abs=0.05)
        assert s['pointing_frame'] == 'horizontal'

    def test_the_same_verdict_for_a_shared_epoch_station(self):
        """The RO000M case: same steady camera, file written the other way.
        The two must now agree, which is the whole point."""
        swept = sa._analyze_recalib_json(_night(sweep_ra=True, epoch_per_frame=True))
        fixed = sa._analyze_recalib_json(_night(sweep_ra=False, epoch_per_frame=False))
        assert swept['pointing_arcmin_per_h'] == pytest.approx(
            fixed['pointing_arcmin_per_h'], abs=0.05)

    def test_ra_wraparound_through_zero_does_not_register(self):
        """DE001E's RA ran 333 deg -> 75 deg, straight through 0/360."""
        s = sa._analyze_recalib_json(_night(sweep_ra=True))
        assert s['pointing_arcmin_per_h'] < 0.1


class TestRealMovementIsStillDetected:

    def test_a_camera_that_actually_slews_in_azimuth_is_reported(self):
        """Half a degree of azimuth over 8 hours = 30 arcmin / 8 h, scaled by
        cos(alt) because azimuth degrees are not sky degrees away from the
        horizon."""
        s = sa._analyze_recalib_json(_night(az_drift_deg=0.5, hours=8.0, alt=49.2811))
        expected = 0.5 * math.cos(math.radians(49.2811)) * 60 / 8.0
        assert s['pointing_arcmin_per_h'] == pytest.approx(expected, rel=0.02)

    def test_rotation_drift_is_unchanged_by_this_fix(self):
        """rotation_from_horiz was already in the horizontal frame and was
        never affected by the epoch convention."""
        data = _night()
        keys = sorted(data)
        for i, k in enumerate(keys):
            data[k]['rotation_from_horiz'] = -98.96 + i * (0.1 / (len(keys) - 1))
        s = sa._analyze_recalib_json(data)
        assert s['rotation_arcmin_per_h'] == pytest.approx(0.1 * 60 / 8.0, rel=0.02)


class TestReportingContract:

    def test_recalib_pct_is_reported(self):
        data = _night(n=10)
        for k in sorted(data)[:3]:
            data[k]['auto_recalibrated'] = False
        assert sa._analyze_recalib_json(data)['recalib_ok_pct'] == 70.0

    def test_a_file_without_horizontal_coords_omits_the_figure(self):
        """Rather than falling back to the RA/Dec computation, which is wrong
        on exactly the stations old enough to produce such a file."""
        data = _night(n=10)
        for v in data.values():
            v.pop('az_centre')
            v.pop('alt_centre')
        s = sa._analyze_recalib_json(data)
        assert 'pointing_arcmin_per_h' not in s
        assert s['recalib_ok_pct'] == 100.0

    def test_empty_input_is_none(self):
        assert sa._analyze_recalib_json({}) is None

    def test_a_too_short_night_is_none(self):
        assert sa._analyze_recalib_json(_night(n=3, hours=0.01)) is None


class TestOnlyRecalibratedFramesAreFitted:
    """A frame whose recalibration failed carries the starting platepar
    unchanged, so its az/alt is not a measurement. RO0006, 2026-09-22: 6 such
    frames ~1 deg from the 21 good ones at the start of a 1.17 h night read as
    58 arcmin/h on a camera that held to 0.005 deg."""

    @staticmethod
    def _ro0006_night():
        data = _night(n=27, hours=1.17, az=149.575, alt=31.466, rot=-11.55)
        for k in sorted(data)[:6]:
            data[k].update(az_centre=148.946, alt_centre=30.621,
                           rotation_from_horiz=-11.626, auto_recalibrated=False)
        return data

    def test_stale_unrecalibrated_frames_do_not_read_as_drift(self):
        s = sa._analyze_recalib_json(self._ro0006_night())
        assert s['pointing_arcmin_per_h'] < 1.0
        assert s['rotation_arcmin_per_h'] < 1.0

    def test_the_failures_still_show_in_recalib_pct(self):
        s = sa._analyze_recalib_json(self._ro0006_night())
        assert s['recalib_ok_pct'] == pytest.approx(21 / 27 * 100, abs=0.1)
        assert s['n_frames'] == 27

    def test_a_night_with_no_recalibrated_frames_reports_no_drift(self):
        s = sa._analyze_recalib_json(_night(n=10, recalibrated=False))
        assert s['recalib_ok_pct'] == 0.0
        assert 'pointing_arcmin_per_h' not in s
        assert 'rotation_arcmin_per_h' not in s

    def test_fewer_than_three_recalibrated_frames_reports_no_drift(self):
        data = _night(n=10)
        for k in sorted(data)[2:]:
            data[k]['auto_recalibrated'] = False
        s = sa._analyze_recalib_json(data)
        assert 'pointing_arcmin_per_h' not in s

    def test_real_movement_among_recalibrated_frames_is_still_reported(self):
        data = _night(az_drift_deg=0.5, hours=8.0, alt=49.2811)
        for k in sorted(data)[:5]:
            data[k]['auto_recalibrated'] = False
        s = sa._analyze_recalib_json(data)
        expected = 0.5 * math.cos(math.radians(49.2811)) * 60 / 8.0
        assert s['pointing_arcmin_per_h'] == pytest.approx(expected, rel=0.02)
