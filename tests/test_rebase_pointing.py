"""Tests for platepar_autofit.run_rebase_pointing.

Re-basing is the cheap counterpart to the blind auto-fit, and the right tool
for the ordinary case: the distortion model is fine and the camera has
settled since the platepar was solved. It performs no fit -- RMS already
refits pointing every night (recalibrateFF minimises over RA_d, dec_d,
pos_angle_ref and F_scale, copying distortion across untouched) and writes
the per-frame results to platepars_all_recalibrated.json.

Verified against the live fleet before writing this: a recalibrated entry
has exactly the same field set as the base platepar, with byte-identical
distortion polynomials, and RO000M's base platepar sits ~38 arcmin from
where its recalibrated frames say the camera points.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import platepar_autofit as pa  # noqa: E402

CAM = 'RO000M'
NIGHT = '20260920'

BASE_AZ, BASE_ALT = 90.4857, 42.4339      # the platepar on disk
TRUE_AZ, TRUE_ALT = 90.1273, 41.8636      # where the night says it points


def _platepar(az, alt, *, rot=-160.13, scale=15.6647, jd=2461186.42020794,
              distortion=(1.0, 2.0)):
    return {
        'RA_d': 300.82, 'dec_d': 28.96, 'pos_angle_ref': 154.05,
        'F_scale': scale, 'JD': jd,
        'az_centre': az, 'alt_centre': alt, 'rotation_from_horiz': rot,
        'X_res': 1280, 'Y_res': 720,
        'distortion_type': 'radial7-odd', 'poly_length': 7,
        'x_poly_fwd': list(distortion), 'y_poly_fwd': list(distortion),
        'x_poly_rev': list(distortion), 'y_poly_rev': list(distortion),
        'auto_recalibrated': True,
    }


@pytest.fixture
def station(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    config_dir = tmp_path / 'RMS_cam1'
    config_dir.mkdir()
    (config_dir / '.config').write_text(f'stationID: {CAM}\nwidth: 1280\nheight: 720\n')
    (config_dir / 'platepar_cmn2010.cal').write_text(
        json.dumps(_platepar(BASE_AZ, BASE_ALT)))
    night_dir = tmp_path / 'RMS_data' / 'cam1' / 'ArchivedFiles' / f'{CAM}_{NIGHT}_170140_336919'
    night_dir.mkdir(parents=True)
    return {
        'scfg': {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam1')},
        'config_dir': config_dir, 'night_dir': night_dir, 'tmp': tmp_path,
    }


def _write_night(station, n=60, *, spread_deg=0.01, recalibrated=True, **kw):
    """A night of recalibrated platepars scattered about the true pointing."""
    entries = {}
    for i in range(n):
        jitter = ((i % 7) - 3) / 3.0 * spread_deg
        e = _platepar(TRUE_AZ + jitter, TRUE_ALT + jitter, **kw)
        e['auto_recalibrated'] = recalibrated
        entries[f'FF_{CAM}_{NIGHT}_18{i:04d}_000_0000000.fits'] = e
    (station['night_dir'] / 'platepars_all_recalibrated.json').write_text(json.dumps(entries))
    return entries


class TestHappyPath:

    def test_reports_the_pointing_shift_it_would_apply(self, station):
        _write_night(station)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        assert r['status'] == 'done', r.get('detail')
        expected = pa._angsep_deg(BASE_AZ, BASE_ALT, TRUE_AZ, TRUE_ALT) * 60
        assert r['pointing_shift_arcmin'] == pytest.approx(expected, abs=0.6)
        # The real RO000M offset, as a sanity anchor on the fixture.
        assert 30 < r['pointing_shift_arcmin'] < 45

    def test_stages_a_candidate_and_never_touches_the_live_platepar(self, station):
        _write_night(station)
        before = (station['config_dir'] / 'platepar_cmn2010.cal').read_text()
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        assert (station['config_dir'] / 'platepar_cmn2010.cal').read_text() == before
        assert Path(r['candidate_path']).exists()
        assert r['accept_token']

    def test_the_candidate_keeps_the_distortion_model_exactly(self, station):
        _write_night(station)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        base = json.loads((station['config_dir'] / 'platepar_cmn2010.cal').read_text())
        cand = json.loads(Path(r['candidate_path']).read_text())
        for k in ('x_poly_fwd', 'y_poly_fwd', 'x_poly_rev', 'y_poly_rev',
                  'distortion_type', 'poly_length'):
            assert cand[k] == base[k], k
        assert r['distortion_preserved'] is True

    def test_the_candidate_carries_the_corrected_pointing(self, station):
        _write_night(station)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        cand = json.loads(Path(r['candidate_path']).read_text())
        assert cand['az_centre'] == pytest.approx(TRUE_AZ, abs=0.02)
        assert cand['alt_centre'] == pytest.approx(TRUE_ALT, abs=0.02)

    def test_the_candidate_is_one_real_frame_not_an_average(self, station):
        """Averaging platepar fields independently makes an internally
        inconsistent platepar; the medoid is a frame RMS actually produced."""
        entries = _write_night(station)
        cand = json.loads(Path(pa.run_rebase_pointing(
            CAM, station['scfg'], NIGHT)['candidate_path']).read_text())
        assert any(cand == e for e in entries.values())

    def test_accept_applies_it_and_keeps_a_backup(self, station, monkeypatch):
        # accept_candidate reaches into the RMS venv for one thing only --
        # the platepar filename out of .config -- which is not available in a
        # test environment and is not what this test is about.
        import types
        monkeypatch.setattr(pa.ff_thumbs, 'ensure_rms_venv_on_path', lambda: True)
        fake = types.ModuleType('RMS.ConfigReader')
        fake.parse = lambda path: types.SimpleNamespace(platepar_name='platepar_cmn2010.cal')
        monkeypatch.setitem(sys.modules, 'RMS', types.ModuleType('RMS'))
        monkeypatch.setitem(sys.modules, 'RMS.ConfigReader', fake)
        _write_night(station)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        applied = pa.accept_candidate(CAM, station['scfg'], r['candidate_id'],
                                      accept_token=r['accept_token'])
        assert applied['status'] == 'applied', applied
        live = json.loads((station['config_dir'] / 'platepar_cmn2010.cal').read_text())
        assert live['az_centre'] == pytest.approx(TRUE_AZ, abs=0.02)
        assert list(station['config_dir'].glob('platepar_cmn2010.cal.bak*'))


class TestGuards:

    def test_too_few_recalibrated_frames_refuses(self, station):
        _write_night(station, n=5)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        assert r['status'] == 'error'
        assert 'needed to re-base' in r['detail']

    def test_a_failed_run_is_persisted_to_disk_like_a_successful_one(self, station):
        """station_api.py's autofit_status poll infers "still running" by
        comparing the trigger log's mtime against the newest result JSON's
        mtime -- an early-failure path that never wrote a result used to look
        identically "running" as a genuinely wedged process for up to 30
        minutes (station_api.py's stale-log grace window), well past the
        dashboard's ~7.5-minute poll timeout, so a fast, clean failure like
        this one surfaced as a generic "timed out" instead of the real,
        already-known error. run_autofit's wrapper always persists; this
        pins that run_rebase_pointing's wrapper does too."""
        _write_night(station, n=5)  # triggers the "too few frames" refusal
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        cand_dir = station['tmp'] / 'rovimen_scripts' / 'calibration_candidates' / CAM
        result_files = list(cand_dir.glob(f"{r['run_id']}.json"))
        assert len(result_files) == 1
        on_disk = json.loads(result_files[0].read_text())
        assert on_disk['status'] == 'error'
        assert on_disk['detail'] == r['detail']
        assert on_disk['finished_at'] is not None

    def test_a_night_where_recalibration_mostly_failed_refuses(self, station):
        _write_night(station, n=60, recalibrated=False)
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        assert r['status'] == 'error'
        assert r['n_frames_recalibrated'] == 0

    def test_a_changed_distortion_model_refuses_rather_than_swapping_it(self, station):
        """If the night ran against a different platepar -- someone re-solved
        in SkyFit2 since -- adopting a frame would silently replace the
        distortion model, which is the one thing re-basing promises not to
        do."""
        _write_night(station, distortion=(9.9, 8.8))
        r = pa.run_rebase_pointing(CAM, station['scfg'], NIGHT)
        assert r['status'] == 'error'
        assert 'SkyFit2' in r['detail']
        assert 'x_poly_fwd' in r['distortion_mismatch']

    def test_a_missing_night_is_reported_not_crashed(self, station):
        r = pa.run_rebase_pointing(CAM, station['scfg'], '20250101')
        assert r['status'] == 'error'
        assert 'no capture directory' in r['detail']

    def test_an_unresolvable_camera_is_reported(self, station):
        r = pa.run_rebase_pointing('XX9999', {'rms_data_path': ''}, NIGHT)
        assert r['status'] == 'error'
        assert 'config directory' in r['detail']


class TestHorizontalFrameHelpers:

    def test_circular_median_crosses_the_zero_seam(self):
        """Azimuths near north straddle 0/360; a plain median would answer
        180 degrees away from the truth."""
        assert pa._circular_median_deg([359.0, 359.5, 0.5, 1.0]) == pytest.approx(0.0, abs=0.6)

    def test_circular_median_matches_a_plain_one_away_from_the_seam(self):
        assert pa._circular_median_deg([90.0, 90.2, 90.4]) == pytest.approx(90.2, abs=0.01)

    def test_angular_separation_shrinks_with_altitude(self):
        """A degree of azimuth is a degree on sky only at the horizon."""
        at_horizon = pa._angsep_deg(0.0, 0.0, 1.0, 0.0)
        high_up = pa._angsep_deg(0.0, 60.0, 1.0, 60.0)
        assert at_horizon == pytest.approx(1.0, abs=1e-6)
        assert high_up == pytest.approx(math.cos(math.radians(60.0)), abs=1e-3)

    def test_separation_is_symmetric_and_zero_for_identical_directions(self):
        assert pa._angsep_deg(136.2, 49.3, 136.2, 49.3) == pytest.approx(0.0, abs=1e-9)
        assert pa._angsep_deg(10.0, 20.0, 40.0, 50.0) == pytest.approx(
            pa._angsep_deg(40.0, 50.0, 10.0, 20.0), abs=1e-9)
