"""Tests for platepar_autofit.py — station-side automated platepar refit.

Focused on the parts that don't need a real RMS venv (numpy, astrometry.net,
compiled cython extensions): path resolution, candidate staging/accept
file operations, and the guarantee that every run_autofit() exit path
persists a result JSON (the bug this guards: an early version only wrote
the result on success, so a failed run showed as "running" forever to
station_api.py's autofit_status poller, which infers completion by
comparing the log file's mtime against the newest result JSON's mtime).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

import platepar_autofit as pa


# ---------------------------------------------------------------------------
# _night_date
# ---------------------------------------------------------------------------

class TestNightDate:

    def test_always_yesterday_regardless_of_hour(self):
        """Mirrors the #883 fix elsewhere in the codebase -- "today" is never
        the right default, since tonight's session isn't processed until
        tomorrow's dawn no matter what time it is right now."""
        for hour in (2, 8, 13, 20, 23):
            fixed_now = datetime(2026, 7, 30, hour, 0, tzinfo=timezone.utc)
            with patch('platepar_autofit.datetime') as mock_dt:
                mock_dt.now.return_value = fixed_now
                mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
                assert pa._night_date() == '20260729'


# ---------------------------------------------------------------------------
# _resolve_rms_config_dir
# ---------------------------------------------------------------------------

class TestResolveRmsConfigDir:
    """The camN derivation these used to assert is now only a fallback, and
    returning a path unconditionally was the bug: on gmn0000 it produced
    ~/RMS_DE001B, which does not exist. See
    tests/test_rms_config_dir_resolution.py for the stationID resolution that
    replaced it; these keep the fallback and the override honest.
    """

    def test_explicit_override_wins(self):
        scfg = {'rms_config_path': '/custom/path', 'rms_data_path': '/home/gmn/RMS_data/cam2'}
        assert pa.resolve_rms_config_dir('RO000M', scfg) == Path('/custom/path')

    def test_falls_back_to_the_cam_suffix_when_the_directory_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        (tmp_path / 'RMS_cam2').mkdir()
        scfg = {'rms_data_path': str(tmp_path / 'RMS_data' / 'cam2')}
        assert pa.resolve_rms_config_dir('RO000M', scfg) == tmp_path / 'RMS_cam2'

    def test_a_derived_directory_that_does_not_exist_is_not_returned(self, tmp_path, monkeypatch):
        """The production failure: the endpoint reported "RMS config directory
        /home/gmn/RMS_DE001B does not exist" because the derivation was
        trusted without checking."""
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg = {'rms_data_path': str(tmp_path / 'RMS_data' / 'DE001B')}
        assert pa.resolve_rms_config_dir('DE001B', scfg) is None

    def test_missing_rms_data_path_resolves_to_nothing(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        assert pa.resolve_rms_config_dir('RO000M', {}) is None


# ---------------------------------------------------------------------------
# _arcmin_reject_for_camera
# ---------------------------------------------------------------------------

class TestMinStarsAfterReject:

    def test_scales_with_poly_length(self):
        # radial7-odd (poly_length=7): ceil(3.5*7) = 25
        assert pa._min_stars_after_reject(7) == 25

    def test_smaller_distortion_model_gives_smaller_floor(self):
        # radial3-odd (poly_length=6): ceil(3.5*6) = 21
        assert pa._min_stars_after_reject(6) == 21

    def test_none_falls_back_to_absolute_floor(self):
        assert pa._min_stars_after_reject(None) == pa.MIN_STARS_ABSOLUTE_FLOOR

    def test_zero_falls_back_to_absolute_floor(self):
        assert pa._min_stars_after_reject(0) == pa.MIN_STARS_ABSOLUTE_FLOOR

    def test_absolute_floor_wins_for_tiny_distortion_models(self):
        # poly_length=1 -> ceil(3.5) = 4, but the absolute floor (8) wins.
        assert pa._min_stars_after_reject(1) == pa.MIN_STARS_ABSOLUTE_FLOOR


# ---------------------------------------------------------------------------
# _field_quadrant / _field_radius_frac
# ---------------------------------------------------------------------------

class TestFieldGeometry:

    def test_quadrants(self):
        # 100x80 image, center at (50, 40).
        assert pa._field_quadrant(10, 10, 100, 80) == 'NW'
        assert pa._field_quadrant(90, 10, 100, 80) == 'NE'
        assert pa._field_quadrant(10, 70, 100, 80) == 'SW'
        assert pa._field_quadrant(90, 70, 100, 80) == 'SE'

    def test_radius_frac_center_is_zero(self):
        assert pa._field_radius_frac(50, 40, 100, 80) == 0.0

    def test_radius_frac_corner_is_one(self):
        assert pa._field_radius_frac(100, 80, 100, 80) == pytest.approx(1.0)

    def test_radius_frac_handles_zero_resolution(self):
        assert pa._field_radius_frac(0, 0, 0, 0) == 0.0


# ---------------------------------------------------------------------------
# _find_night_dir
# ---------------------------------------------------------------------------

class TestFindNightDir:

    def test_prefers_archived_over_captured(self, tmp_path):
        rms_dir = tmp_path
        (rms_dir / 'ArchivedFiles' / 'RO000H_20260315_200000_000000').mkdir(parents=True)
        (rms_dir / 'CapturedFiles' / 'RO000H_20260315_200000_000000').mkdir(parents=True)
        found = pa._find_night_dir(rms_dir, 'RO000H', '20260315')
        assert found == rms_dir / 'ArchivedFiles' / 'RO000H_20260315_200000_000000'

    def test_falls_back_to_captured(self, tmp_path):
        rms_dir = tmp_path
        (rms_dir / 'CapturedFiles' / 'RO000H_20260315_200000_000000').mkdir(parents=True)
        found = pa._find_night_dir(rms_dir, 'RO000H', '20260315')
        assert found == rms_dir / 'CapturedFiles' / 'RO000H_20260315_200000_000000'

    def test_none_when_nothing_found(self, tmp_path):
        assert pa._find_night_dir(tmp_path, 'RO000H', '20260315') is None

    def test_newest_match_wins(self, tmp_path):
        rms_dir = tmp_path
        base = rms_dir / 'ArchivedFiles'
        base.mkdir(parents=True)
        (base / 'RO000H_20260315_200000_000000').mkdir()
        (base / 'RO000H_20260315_210000_000000').mkdir()
        found = pa._find_night_dir(rms_dir, 'RO000H', '20260315')
        assert found.name == 'RO000H_20260315_210000_000000'


# ---------------------------------------------------------------------------
# run_autofit -- result-persistence guarantee (no real RMS needed: the
# RMS-unavailable path is the very first check in _run_autofit_inner)
# ---------------------------------------------------------------------------

class TestRunAutofitPersistsResultAlways:

    def test_rms_unavailable_still_writes_result_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        with patch('platepar_autofit.ff_thumbs.ensure_rms_venv_on_path', return_value=False):
            result = pa.run_autofit('RO000H', {'rms_data_path': str(tmp_path / 'rms')}, '20260315')

        assert result['status'] == 'error'
        assert 'RMS venv' in result['detail']
        assert result['finished_at'] is not None

        cand_dir = tmp_path / 'rovimen_scripts' / 'calibration_candidates' / 'RO000H'
        result_files = list(cand_dir.glob('*.json'))
        assert len(result_files) == 1
        on_disk = json.loads(result_files[0].read_text())
        assert on_disk['status'] == 'error'
        assert on_disk['run_id'] == result['run_id']

    def test_no_night_dir_still_writes_result_json(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        with patch('platepar_autofit.ff_thumbs.ensure_rms_venv_on_path', return_value=True), \
             patch.dict(sys.modules, {
                 'RMS': __import__('types').ModuleType('RMS'),
                 'RMS.ConfigReader': __import__('types').ModuleType('RMS.ConfigReader'),
                 'RMS.Astrometry': __import__('types').ModuleType('RMS.Astrometry'),
                 'RMS.Astrometry.ApplyAstrometry': __import__('types').ModuleType('RMS.Astrometry.ApplyAstrometry'),
                 'RMS.Astrometry.AutoPlatepar': __import__('types').ModuleType('RMS.Astrometry.AutoPlatepar'),
                 'RMS.Astrometry.CheckFit': __import__('types').ModuleType('RMS.Astrometry.CheckFit'),
                 'RMS.Astrometry.StarClasses': __import__('types').ModuleType('RMS.Astrometry.StarClasses'),
                 'RMS.Formats': __import__('types').ModuleType('RMS.Formats'),
                 'RMS.Formats.Platepar': __import__('types').ModuleType('RMS.Formats.Platepar'),
                 'RMS.Math': __import__('types').ModuleType('RMS.Math'),
                 'numpy': __import__('types').ModuleType('numpy'),
             }):
            # Stub attributes the module body imports by name (`from X import Y`)
            sys.modules['RMS.Astrometry.ApplyAstrometry'].xyToRaDecPP = lambda *a, **k: None
            sys.modules['RMS.Astrometry.AutoPlatepar'].autoFitPlatepar = lambda *a, **k: (None, None, None)
            sys.modules['RMS.Astrometry.CheckFit'].matchStarsResiduals = lambda *a, **k: None
            sys.modules['RMS.Astrometry.CheckFit'].starListToDict = lambda *a, **k: None
            sys.modules['RMS.Astrometry.StarClasses'].PairedStars = object
            sys.modules['RMS.Formats.Platepar'].Platepar = object
            sys.modules['RMS.Formats.Platepar'].getCatalogStarsImagePositions = lambda *a, **k: None
            sys.modules['RMS.Math'].angularSeparation = lambda *a, **k: None
            import RMS.Formats  # noqa: F401  (needed as a submodule of RMS.Formats.Platepar's parent)
            sys.modules['RMS.Formats'].CALSTARS = __import__('types').ModuleType('CALSTARS')
            sys.modules['RMS.Formats'].StarCatalog = __import__('types').ModuleType('StarCatalog')

            result = pa.run_autofit('RO000H', {'rms_data_path': str(tmp_path / 'rms')}, '20260315')

        assert result['status'] == 'no_data'
        assert 'night directory' in result['detail']

        cand_dir = tmp_path / 'rovimen_scripts' / 'calibration_candidates' / 'RO000H'
        result_files = list(cand_dir.glob('*.json'))
        assert len(result_files) == 1


# ---------------------------------------------------------------------------
# _run_cli -- threshold/sigma-clip-factor pass-through. The actual
# self-calibrating derivation happens inside _run_autofit_inner (needs a
# real fit's residual distribution); this just verifies _run_cli doesn't
# second-guess the caller's choice between an explicit override and the
# sigma-clip default the way the old per-camera lookup used to.
# ---------------------------------------------------------------------------

class TestRunCliPassesThresholdArgsThrough:

    def test_none_threshold_passed_through_unchanged(self):
        cfg = {'stations': {'RO0019': {}}}
        with patch('platepar_autofit.run_autofit', return_value={'status': 'no_data', 'detail': 'x'}) as m:
            pa._run_cli(cfg, 'RO0019', '20260730', apply=False,
                        arcmin_reject_threshold=None, sigma_clip_factor=3.0,
                        match_radius_px=10.0, as_json=True)
        assert m.call_args.kwargs['arcmin_reject_threshold'] is None
        assert m.call_args.kwargs['sigma_clip_factor'] == 3.0

    def test_explicit_cli_value_passed_through_unchanged(self):
        cfg = {'stations': {'RO0019': {}}}
        with patch('platepar_autofit.run_autofit', return_value={'status': 'no_data', 'detail': 'x'}) as m:
            pa._run_cli(cfg, 'RO0019', '20260730', apply=False,
                        arcmin_reject_threshold=2.0, sigma_clip_factor=3.0,
                        match_radius_px=10.0, as_json=True)
        assert m.call_args.kwargs['arcmin_reject_threshold'] == 2.0

    def test_unknown_camera_returns_error_without_calling_run_autofit(self):
        cfg = {'stations': {}}
        with patch('platepar_autofit.run_autofit') as m:
            rc = pa._run_cli(cfg, 'RO0019', '20260730', apply=False,
                              arcmin_reject_threshold=None, sigma_clip_factor=3.0,
                              match_radius_px=10.0, as_json=True)
        assert rc == 1
        m.assert_not_called()


# ---------------------------------------------------------------------------
# accept_candidate
# ---------------------------------------------------------------------------

class _FakeRmsConfig:
    platepar_name = 'platepar_cmn2010.cal'


class TestAcceptCandidate:

    ACCEPT_TOKEN = 'deadbeef' * 4

    def _setup(self, tmp_path):
        config_dir = tmp_path / 'RMS_cam1'
        config_dir.mkdir(parents=True)
        (config_dir / '.config').write_text('# fake rms config')
        live_platepar = config_dir / 'platepar_cmn2010.cal'
        live_platepar.write_text('OLD PLATEPAR CONTENT')

        cand_dir = tmp_path / 'rovimen_scripts' / 'calibration_candidates' / 'RO000H'
        cand_dir.mkdir(parents=True)
        candidate_id = 'RO000H_20260315_201500'
        (cand_dir / f'{candidate_id}.cal').write_text('NEW PLATEPAR CONTENT')
        (cand_dir / f'{candidate_id}.json').write_text(
            json.dumps({'status': 'done', 'accept_token': self.ACCEPT_TOKEN})
        )

        scfg = {'rms_config_path': str(config_dir)}
        return scfg, candidate_id, live_platepar, cand_dir

    def test_applies_and_backs_up(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, candidate_id, live_platepar, cand_dir = self._setup(tmp_path)

        # RMS isn't importable at all in this sandbox (no RMS venv on path) --
        # inject a stub module so `import RMS.ConfigReader as rms_cr` resolves.
        import types
        fake_rms_cr = types.ModuleType('RMS.ConfigReader')
        fake_rms_cr.parse = lambda path: _FakeRmsConfig()
        with patch('platepar_autofit.ff_thumbs.ensure_rms_venv_on_path', return_value=True), \
             patch.dict(sys.modules, {'RMS': types.ModuleType('RMS'), 'RMS.ConfigReader': fake_rms_cr}):
            result = pa.accept_candidate('RO000H', scfg, candidate_id, accept_token=self.ACCEPT_TOKEN)

        assert result['status'] == 'applied'
        assert live_platepar.read_text() == 'NEW PLATEPAR CONTENT'

        backups = list(live_platepar.parent.glob('platepar_cmn2010.cal.bak_*'))
        assert len(backups) == 1
        assert backups[0].read_text() == 'OLD PLATEPAR CONTENT'

        # Staged candidate + its result JSON are cleaned up after apply.
        assert not (cand_dir / f'{candidate_id}.cal').exists()
        assert not (cand_dir / f'{candidate_id}.json').exists()

    def test_missing_candidate_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, _candidate_id, _live, _cand_dir = self._setup(tmp_path)
        result = pa.accept_candidate('RO000H', scfg, 'RO000H_19700101_000000',
                                      accept_token=self.ACCEPT_TOKEN)
        assert result['status'] == 'error'
        assert 'not found' in result['error']

    def test_missing_rms_config_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, candidate_id, _live, _cand_dir = self._setup(tmp_path)
        scfg['rms_config_path'] = str(tmp_path / 'nowhere')
        result = pa.accept_candidate('RO000H', scfg, candidate_id, accept_token=self.ACCEPT_TOKEN)
        assert result['status'] == 'error'
        assert '.config not found' in result['error']

    def test_wrong_token_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, candidate_id, live_platepar, _cand_dir = self._setup(tmp_path)
        result = pa.accept_candidate('RO000H', scfg, candidate_id, accept_token='wrong')
        assert result['status'] == 'error'
        assert 'accept_token' in result['error']
        # Nothing was touched.
        assert live_platepar.read_text() == 'OLD PLATEPAR CONTENT'

    def test_missing_token_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, candidate_id, live_platepar, _cand_dir = self._setup(tmp_path)
        result = pa.accept_candidate('RO000H', scfg, candidate_id)
        assert result['status'] == 'error'
        assert 'accept_token' in result['error']
        assert live_platepar.read_text() == 'OLD PLATEPAR CONTENT'

    def test_missing_result_json_is_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        scfg, candidate_id, live_platepar, cand_dir = self._setup(tmp_path)
        (cand_dir / f'{candidate_id}.json').unlink()
        result = pa.accept_candidate('RO000H', scfg, candidate_id, accept_token=self.ACCEPT_TOKEN)
        assert result['status'] == 'error'
        assert 'accept_token' in result['error']
        assert live_platepar.read_text() == 'OLD PLATEPAR CONTENT'
