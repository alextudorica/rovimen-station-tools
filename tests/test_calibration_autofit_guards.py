"""Tests for the platepar-autofit trigger endpoint's single-flight and
capture-window guards, and the accept endpoint's accept_token pass-through
(station_api.py). Added per the PR #884 review:

  - No single-flight guard meant a double-click, client retry, or hostile
    loop could spawn N concurrent astrometry.net blind solves on one
    station.
  - No capture-window guard meant an admin clicking "Run auto-fit" during
    active capture could starve CPU and drop frames -- lost meteors.
  - accept previously trusted candidate_id alone (a predictable string);
    it must now also carry a valid accept_token.

See tests/test_calibration_autofit_endpoints.py for the (unrelated,
still-passing) readonly-gate carve-out tests.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import station_api as sa  # noqa: E402


# ---------------------------------------------------------------------------
# _autofit_run_in_flight
# ---------------------------------------------------------------------------

class TestAutofitRunInFlight:

    def test_no_log_file_means_not_in_flight(self, tmp_path):
        cfg = {'log_path': str(tmp_path)}
        assert sa._autofit_run_in_flight(cfg, 'RO000H') is False

    def test_log_newer_than_any_result_means_in_flight(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        cfg = {'log_path': str(tmp_path)}
        (tmp_path / 'platepar_autofit_RO000H.log').write_text('running')
        assert sa._autofit_run_in_flight(cfg, 'RO000H') is True

    def test_result_newer_than_log_means_finished(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, 'home', lambda: tmp_path)
        cfg = {'log_path': str(tmp_path)}
        log_file = tmp_path / 'platepar_autofit_RO000H.log'
        log_file.write_text('done')
        cand_dir = tmp_path / 'rovimen_scripts' / 'calibration_candidates' / 'RO000H'
        cand_dir.mkdir(parents=True)
        result_file = cand_dir / 'RO000H_20260315_201500.json'
        result_file.write_text('{}')
        # Force the result to be newer than the log (both may land in the
        # same tick otherwise).
        import os
        import time
        now = time.time()
        os.utime(log_file, (now - 10, now - 10))
        os.utime(result_file, (now, now))
        assert sa._autofit_run_in_flight(cfg, 'RO000H') is False


# ---------------------------------------------------------------------------
# _camera_is_capturing
# ---------------------------------------------------------------------------

class TestCameraIsCapturing:
    """The guard has to answer "is this camera busy right now", and on this
    fleet the StartCapture process cannot answer it: RMS runs as a persistent
    service that sleeps through the day waiting for dusk, so pgrep matches at
    noon exactly as it does at midnight. Guarding on liveness alone refused
    the auto-fit trigger on every camera at every hour (verified against the
    live fleet 2026-09-21: capturing:true fleet-wide at 11:20 UTC), and the
    dashboard button sends no body so it could not pass force either -- the
    feature was unreachable from the UI it exists for.
    """

    @staticmethod
    def _capturing_proc(line='12345 python StartCapture.py RO000H\n'):
        return type('P', (), {'stdout': line})()

    @staticmethod
    def _tree(tmp_path, cam='RO000H', age_s=0.0):
        """A CapturedFiles tree for *cam* whose newest write is age_s old."""
        session = tmp_path / 'CapturedFiles' / f'{cam}_20260920_180000_000000'
        session.mkdir(parents=True)
        ff = session / f'FF_{cam}_20260920_180000_000_0000000.fits'
        ff.write_bytes(b'x')
        when = time.time() - age_s
        os.utime(ff, (when, when))
        os.utime(session, (when, when))
        return str(tmp_path)

    def test_live_process_and_a_freshly_written_tree_is_capturing(self, tmp_path):
        path = self._tree(tmp_path)
        with patch('station_api.subprocess.run', return_value=self._capturing_proc()):
            assert sa._camera_is_capturing('RO000H', path) is True

    def test_live_process_but_a_quiet_tree_is_not_capturing(self, tmp_path):
        """Daytime: the service is up, nothing has been written for hours.
        This is the case the auto-fit button exists for."""
        path = self._tree(tmp_path, age_s=sa._CAPTURE_ACTIVE_WINDOW_S + 3600)
        with patch('station_api.subprocess.run', return_value=self._capturing_proc()):
            assert sa._camera_is_capturing('RO000H', path) is False

    def test_rms_camn_suffix_also_identifies_the_process(self, tmp_path):
        path = self._tree(tmp_path / 'cam2', cam='RO000J')
        proc = self._capturing_proc('12345 python StartCapture.py --config RMS_cam2/.config\n')
        with patch('station_api.subprocess.run', return_value=proc):
            assert sa._camera_is_capturing('RO000J', path) is True

    def test_no_process_is_never_capturing(self, tmp_path):
        """Liveness is still authoritative in the one direction it is
        reliable in, and skips the filesystem walk entirely."""
        path = self._tree(tmp_path)
        with patch('station_api.subprocess.run', return_value=self._capturing_proc('')):
            assert sa._camera_is_capturing('RO000H', path) is False

    def test_another_cameras_activity_does_not_count(self, tmp_path):
        path = self._tree(tmp_path, cam='RO000L')
        with patch('station_api.subprocess.run', return_value=self._capturing_proc()):
            assert sa._camera_is_capturing('RO000H', path) is False

    def test_missing_rms_data_path_does_not_raise(self):
        with patch('station_api.subprocess.run', return_value=self._capturing_proc()):
            assert sa._camera_is_capturing('RO000H', '') is False

    def test_missing_captured_files_dir_does_not_raise(self, tmp_path):
        with patch('station_api.subprocess.run', return_value=self._capturing_proc()):
            assert sa._camera_is_capturing('RO000H', str(tmp_path)) is False


# ---------------------------------------------------------------------------
# Trigger endpoint -- single-flight + capture-window guards, through a real
# Flask request (loopback, so the readonly gate itself never interferes).
# ---------------------------------------------------------------------------

class TestTriggerEndpointGuards:

    @pytest.fixture
    def client(self, monkeypatch):
        sa.app.testing = False
        # api_calibration_autofit 404s early if CONFIG_PATH.parent (normally
        # ~/rovimen_scripts, not this checkout) doesn't have
        # platepar_autofit.py sitting next to it -- point it at the real
        # repo copy so these tests exercise the guards, not that early exit.
        monkeypatch.setattr(sa, 'CONFIG_PATH', Path(__file__).parent.parent / 'rovimen-scripts' / 'config.json')
        return sa.app.test_client()

    def _cfg(self, tmp_path):
        return {'stations': {'RO000H': {'rms_data_path': '/home/gmn/RMS_data/cam1'}},
                'log_path': str(tmp_path)}

    def test_refuses_when_already_in_flight(self, client, tmp_path):
        cfg = self._cfg(tmp_path)
        (tmp_path / 'platepar_autofit_RO000H.log').write_text('running')
        with patch('station_api.load_config', return_value=cfg):
            resp = client.post('/api/calibration/autofit/RO000H')
        assert resp.status_code == 409
        assert 'already in progress' in resp.get_json()['error']

    def test_refuses_when_capturing_and_not_forced(self, client, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch('station_api.load_config', return_value=cfg), \
             patch('station_api._camera_is_capturing', return_value=True):
            resp = client.post('/api/calibration/autofit/RO000H')
        assert resp.status_code == 409
        assert 'force' in resp.get_json()['error']

    def test_force_bypasses_capture_guard(self, client, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch('station_api.load_config', return_value=cfg), \
             patch('station_api._camera_is_capturing', return_value=True), \
             patch('station_api.subprocess.Popen') as popen:
            popen.return_value.pid = 4242
            resp = client.post('/api/calibration/autofit/RO000H', json={'force': True})
        assert resp.status_code == 200
        assert resp.get_json()['pid'] == 4242

    def test_spawns_niced_and_ioniced(self, client, tmp_path):
        cfg = self._cfg(tmp_path)
        with patch('station_api.load_config', return_value=cfg), \
             patch('station_api._camera_is_capturing', return_value=False), \
             patch('station_api.subprocess.Popen') as popen:
            popen.return_value.pid = 1
            client.post('/api/calibration/autofit/RO000H')
        cmd = popen.call_args.args[0]
        assert cmd[:4] == ['nice', '-n', '15', 'ionice']

    def test_unknown_camera_404s_before_any_guard(self, client, tmp_path):
        cfg = {'stations': {}, 'log_path': str(tmp_path)}
        with patch('station_api.load_config', return_value=cfg):
            resp = client.post('/api/calibration/autofit/RO000H')
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Accept endpoint -- accept_token pass-through to platepar_autofit
# ---------------------------------------------------------------------------

class TestAcceptEndpointTokenPassthrough:

    @pytest.fixture
    def client(self):
        sa.app.testing = False
        return sa.app.test_client()

    def test_accept_token_forwarded_to_accept_candidate(self, client):
        cfg = {'stations': {'RO000H': {'rms_data_path': '/home/gmn/RMS_data/cam1'}}}
        fake_module = type(sys)('platepar_autofit')
        fake_module.accept_candidate = lambda cam, scfg, candidate_id, accept_token='': {
            'status': 'applied', 'seen_token': accept_token,
        }
        with patch('station_api.load_config', return_value=cfg), \
             patch.dict(sys.modules, {'platepar_autofit': fake_module}):
            resp = client.post(
                '/api/calibration/autofit_accept/RO000H',
                json={'candidate_id': 'RO000H_20260315_201500', 'accept_token': 'sekrit'},
            )
        assert resp.status_code == 200
        assert resp.get_json()['seen_token'] == 'sekrit'

    def test_missing_accept_token_still_forwarded_as_empty(self, client):
        cfg = {'stations': {'RO000H': {'rms_data_path': '/home/gmn/RMS_data/cam1'}}}
        fake_module = type(sys)('platepar_autofit')
        fake_module.accept_candidate = lambda cam, scfg, candidate_id, accept_token='': {
            'status': 'error', 'seen_token': accept_token,
        }
        with patch('station_api.load_config', return_value=cfg), \
             patch.dict(sys.modules, {'platepar_autofit': fake_module}):
            resp = client.post(
                '/api/calibration/autofit_accept/RO000H',
                json={'candidate_id': 'RO000H_20260315_201500'},
            )
        assert resp.get_json()['seen_token'] == ''
