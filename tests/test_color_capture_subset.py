"""Tests for per-station colour capture opt-out (``color_capture: false``).

A night of colour video costs ~25-30 GB per camera, so a station whose
capture disk cannot hold every camera runs colour on a subset. Every other
service must still see the full station list.
"""

from __future__ import annotations

from color_capture import ColorCapture


def _capture(stations: dict) -> ColorCapture:
    """Build a ColorCapture without running __init__ (which touches disk)."""
    obj = ColorCapture.__new__(ColorCapture)
    obj.cfg = {'stations': stations}
    return obj


class TestColorStations:
    def test_all_enabled_by_default(self):
        cap = _capture({'RO000T': {}, 'RO000U': {}, 'RO000V': {}})
        assert set(cap._color_stations()) == {'RO000T', 'RO000U', 'RO000V'}

    def test_false_excludes_station(self):
        cap = _capture({
            'RO000T': {'color_capture': False},
            'RO000U': {'color_capture': True},
            'RO000V': {'color_capture': False},
        })
        assert set(cap._color_stations()) == {'RO000U'}

    def test_explicit_true_included(self):
        cap = _capture({'RO000U': {'color_capture': True}})
        assert set(cap._color_stations()) == {'RO000U'}

    def test_all_disabled_yields_empty(self):
        cap = _capture({'RO000T': {'color_capture': False}})
        assert cap._color_stations() == {}

    def test_station_config_is_preserved(self):
        cap = _capture({'RO000U': {'rms_data_path': '/x/cam2'}})
        assert cap._color_stations()['RO000U']['rms_data_path'] == '/x/cam2'


class TestGetStationForFF:
    def test_disabled_station_ignored(self):
        cap = _capture({
            'RO000T': {'color_capture': False},
            'RO000U': {'color_capture': True},
        })
        ff = 'FF_RO000T_20260812_180000_000_0000000.fits'
        assert cap._get_station_for_ff(ff) is None

    def test_enabled_station_matched(self):
        cap = _capture({
            'RO000T': {'color_capture': False},
            'RO000U': {'color_capture': True},
        })
        ff = 'FF_RO000U_20260812_180000_000_0000000.fits'
        assert cap._get_station_for_ff(ff) == 'RO000U'

    def test_unknown_station_returns_none(self):
        cap = _capture({'RO000U': {}})
        ff = 'FF_RO0099_20260812_180000_000_0000000.fits'
        assert cap._get_station_for_ff(ff) is None
