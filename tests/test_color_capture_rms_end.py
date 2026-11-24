"""Colour capture ends an RMS camera's recording soon after RMS ends its night,
and SIGTERMs dawn only when a new night's capture begins.

Every morning the 30-min idle tail recorded after RMS ended its night closed
chunks into the 04:00 and 04:15 dawn runs, and each closed chunk SIGTERMed
dawn_process, so both runs died within ~15 s on 17 stations.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import camera_fps_guard
import color_capture

CODE = 'RO0012'


def _capture(cfg: dict) -> color_capture.ColorCapture:
    cc = object.__new__(color_capture.ColorCapture)
    cc.cfg = cfg
    cc.log = logging.getLogger('test_rms_end')
    cc.idle_timeout = 30 * 60
    cc._ffmpeg = {CODE: object()}
    cc._tee_watchers = {}
    cc._last_ff = {CODE: 1000.0}
    cc.stopped = []
    cc._stop_ffmpeg = lambda sid: cc.stopped.append(sid)
    cc._stop_tee_watcher = lambda sid: cc.stopped.append(('tee', sid))
    return cc


class TestReaper:
    def test_rms_ended_stops_after_the_grace(self, monkeypatch):
        cc = _capture({'stations': {CODE: {}}})
        monkeypatch.setattr(cc, '_rms_night_over', lambda sid: True)
        cc._reap_idle_captures(1000.0 + color_capture.RMS_END_GRACE_S - 10)
        assert cc.stopped == []
        cc._reap_idle_captures(1000.0 + color_capture.RMS_END_GRACE_S + 10)
        assert cc.stopped == [CODE]

    def test_mid_night_rms_restart_keeps_the_full_idle_timeout(self, monkeypatch):
        cc = _capture({'stations': {CODE: {}}})
        monkeypatch.setattr(cc, '_rms_night_over', lambda sid: False)
        cc._reap_idle_captures(1000.0 + 29 * 60)
        assert cc.stopped == []
        cc._reap_idle_captures(1000.0 + 31 * 60)
        assert cc.stopped == [CODE]

    def test_tee_watcher_is_stopped_the_same_way(self, monkeypatch):
        cc = _capture({'stations': {CODE: {}}})
        cc._ffmpeg = {}
        cc._tee_watchers = {CODE: object()}
        monkeypatch.setattr(cc, '_rms_night_over', lambda sid: True)
        cc._reap_idle_captures(1000.0 + color_capture.RMS_END_GRACE_S + 10)
        assert cc.stopped == [('tee', CODE)]


class TestRmsNightOver:
    @pytest.fixture
    def rms(self, tmp_path, monkeypatch):
        """A running RMS for CODE whose log the test fills in."""
        data = tmp_path / 'RMS_data'
        (data / 'logs').mkdir(parents=True)
        config = tmp_path / '.config'
        config.write_text(f'[System]\nstationID: {CODE}\n[Capture]\ndata_dir: {data}\nlog_dir: logs\n')
        proc = camera_fps_guard.RmsProcess(config, 'python', None)
        monkeypatch.setattr(color_capture.camera_fps_guard, 'rms_process_pids',
                            lambda proc_root=Path('/proc'): [(4242, proc)])
        return data / 'logs' / f'log_{CODE}_20261025_150000.log'

    @pytest.fixture
    def night(self, monkeypatch):
        monkeypatch.setattr(color_capture, '_sun_altitude_deg', lambda lat, lon, when: -11.0)

    CFG = {'latitude': 45.1, 'longitude': 24.2, 'stations': {CODE: {}}}

    def test_ending_capture_in_the_rms_log(self, rms, night):
        rms.write_text('2026/10/25 16:02:11-INFO-StartCapture - Starting capture for 13.10 hours\n'
                       '2026/10/26 04:31:40-INFO-StartCapture - Ending capture...\n')
        assert _capture(self.CFG)._rms_night_over(CODE)

    def test_rms_still_capturing(self, rms, night):
        rms.write_text('2026/10/25 16:02:11-INFO-StartCapture - Starting capture for 13.10 hours\n')
        assert not _capture(self.CFG)._rms_night_over(CODE)

    def test_sun_up_ends_the_night_without_a_log(self, monkeypatch):
        monkeypatch.setattr(color_capture.camera_fps_guard, 'rms_process_pids', lambda: [])
        monkeypatch.setattr(color_capture, '_sun_altitude_deg', lambda lat, lon, when: -4.0)
        assert _capture(self.CFG)._rms_night_over(CODE)

    def test_no_rms_and_dark_is_not_over(self, monkeypatch, night):
        monkeypatch.setattr(color_capture.camera_fps_guard, 'rms_process_pids', lambda: [])
        assert not _capture(self.CFG)._rms_night_over(CODE)


@pytest.mark.parametrize('date_str, time_str, opens', [
    ('20261025', '160211', True),    # evening chunk: tonight's capture has begun
    ('20261026', '043500', False),   # idle-tail chunk of the night dawn processes
    ('20261026', '115959', False),
    ('20261026', '120000', True),
])
def test_only_a_new_nights_chunk_stops_dawn(date_str, time_str, opens):
    assert color_capture.chunk_opens_a_night(date_str, time_str) is opens
