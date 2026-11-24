"""Tests for the auto-fit in-flight inference's exit condition
(station_api.py's ``_autofit_run_in_flight`` / ``api_calibration_autofit_status``).

The wedge these guard: "running" was inferred purely from the log file being
newer than the newest staged result. A run that CRASHES never writes a
result, so that condition stayed true forever -- the camera reported
``running`` on every poll from then on, and every later trigger got the
single-flight 409. One failed solve took a camera's auto-fit permanently out
of service, with no way back short of SSH.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / 'rovimen-scripts'))
import station_api as sa  # noqa: E402


@pytest.fixture
def station(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(sa, 'load_config', lambda: {'log_path': str(tmp_path)})
    return {'cfg': {'log_path': str(tmp_path)}, 'tmp': tmp_path}


def _age(path: Path, seconds: float) -> None:
    when = time.time() - seconds
    os.utime(path, (when, when))


class TestInFlight:

    def test_fresh_log_with_no_result_is_in_flight(self, station):
        (station['tmp'] / 'platepar_autofit_RO000H.log').write_text('solving')
        assert sa._autofit_run_in_flight(station['cfg'], 'RO000H') is True

    def test_stale_log_with_no_process_is_not_in_flight(self, station):
        """The crashed run. Log frozen well past the ceiling, nothing alive."""
        log = station['tmp'] / 'platepar_autofit_RO000H.log'
        log.write_text('Traceback (most recent call last):')
        _age(log, sa._AUTOFIT_STALE_AFTER_S + 60)
        assert sa._autofit_run_in_flight(station['cfg'], 'RO000H') is False

    def test_stale_log_with_a_live_process_is_still_in_flight(self, station, monkeypatch):
        """A long solve that has gone quiet is not a dead one. Process
        liveness overrides the age ceiling, so raising the ceiling is never
        needed to accommodate a slow camera."""
        log = station['tmp'] / 'platepar_autofit_RO000H.log'
        log.write_text('solving')
        _age(log, sa._AUTOFIT_STALE_AFTER_S + 600)
        monkeypatch.setattr(sa, '_autofit_process_alive', lambda cam: True)
        assert sa._autofit_run_in_flight(station['cfg'], 'RO000H') is True

    def test_result_newer_than_log_is_not_in_flight(self, station):
        log = station['tmp'] / 'platepar_autofit_RO000H.log'
        log.write_text('done')
        cand = station['tmp'] / 'rovimen_scripts' / 'calibration_candidates' / 'RO000H'
        cand.mkdir(parents=True)
        (cand / 'RO000H_20260920_120000.json').write_text('{}')
        _age(log, 30)
        assert sa._autofit_run_in_flight(station['cfg'], 'RO000H') is False


class TestProcessAlive:

    def test_this_process_is_never_matched(self, station):
        """The pgrep trap, avoided: a cmdline scan that matched the caller
        would report every camera as permanently running."""
        assert sa._autofit_process_alive('RO000H') is False

    def test_requires_both_the_script_and_the_camera(self, station, monkeypatch, tmp_path):
        proc = tmp_path / 'proc'
        (proc / '4242').mkdir(parents=True)
        (proc / '4242' / 'cmdline').write_bytes(
            b'python\0/home/gmn/rovimen_scripts/platepar_autofit.py\0--camera\0RO000H\0')
        monkeypatch.setattr(sa.os, 'listdir', lambda p: ['4242'] if str(p) == '/proc' else [])
        real_read = Path.read_bytes

        def fake_read(self):
            if str(self).startswith('/proc/4242'):
                return (proc / '4242' / 'cmdline').read_bytes()
            return real_read(self)

        monkeypatch.setattr(Path, 'read_bytes', fake_read)
        assert sa._autofit_process_alive('RO000H') is True
        assert sa._autofit_process_alive('RO000L') is False


class TestStatusEndpoint:

    def test_a_crashed_run_reports_the_error_not_no_run(self, station):
        """``no_run`` means "nothing was ever started here", which is a
        different and much less actionable answer than "the run you started
        died"; the log tail comes back so the operator can see why."""
        log = station['tmp'] / 'platepar_autofit_RO000H.log'
        log.write_text('ModuleNotFoundError: No module named \'RMS\'\n')
        _age(log, sa._AUTOFIT_STALE_AFTER_S + 60)
        body = sa.app.test_client().get('/api/calibration/autofit_status/RO000H').get_json()
        assert body['status'] == 'error'
        assert 'without writing a result' in body['error']
        assert 'ModuleNotFoundError' in body['detail']

    def test_no_log_at_all_is_still_no_run(self, station):
        body = sa.app.test_client().get('/api/calibration/autofit_status/RO000H').get_json()
        assert body['status'] == 'no_run'

    def test_a_live_run_still_reports_running(self, station):
        (station['tmp'] / 'platepar_autofit_RO000H.log').write_text('solving')
        body = sa.app.test_client().get('/api/calibration/autofit_status/RO000H').get_json()
        assert body['status'] == 'running'
