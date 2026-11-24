"""The chunk filename stamp must be UTC even on a station with a local timezone.

ffmpeg's segment muxer expands ``-strftime`` patterns through ``localtime()``,
and the chunk name is the only timestamp ``detection_lock``,
``detection_indexer`` and the stacker have: they all parse it as UTC. A station
running Europe/Bucharest therefore stamped every chunk +3 h, which made
detection locks land on the chunk from three hours earlier and left every
early-evening detection with no matching chunk at all. These tests pin the
mechanism that prevents it: ffmpeg is launched with ``TZ=UTC``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from color_capture import ColorCapture


@pytest.fixture()
def capture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ColorCapture:
    cfg = {
        'color_capture_path': str(tmp_path / 'color'),
        'log_path': str(tmp_path / 'logs'),
        'stations': {
            'RO0019': {'camera_rtsp': 'rtsp://cam/stream_0'},
        },
    }
    cfg_path = tmp_path / 'config.json'
    cfg_path.write_text(json.dumps(cfg))
    cap = ColorCapture(str(cfg_path))
    monkeypatch.setattr(cap, '_disk_ok', lambda: True)
    return cap


class _FakeProc:
    def poll(self):
        return None


def _launch(cap: ColorCapture, monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def fake_popen(cmd, **kwargs):
        captured['cmd'] = cmd
        captured['kwargs'] = kwargs
        return _FakeProc()

    monkeypatch.setattr(subprocess, 'Popen', fake_popen)
    cap._start_ffmpeg('RO0019')
    assert 'cmd' in captured, '_start_ffmpeg did not launch ffmpeg'
    return captured


def test_ffmpeg_env_pins_tz_to_utc(capture, monkeypatch):
    env = _launch(capture, monkeypatch)['kwargs'].get('env')
    assert env is not None, 'ffmpeg inherited the ambient environment'
    assert env.get('TZ') == 'UTC'


def test_ffmpeg_env_still_carries_the_rest_of_the_environment(capture, monkeypatch):
    monkeypatch.setenv('ROVIMEN_TEST_MARKER', 'kept')
    env = _launch(capture, monkeypatch)['kwargs']['env']
    assert env.get('ROVIMEN_TEST_MARKER') == 'kept', (
        'overriding TZ must not drop PATH/HOME and friends'
    )


def test_strftime_pattern_is_still_what_needs_pinning(capture, monkeypatch):
    """Guard the premise: if the name stops coming from -strftime, revisit TZ."""
    cmd = _launch(capture, monkeypatch)['cmd']
    assert '-strftime' in cmd
    assert any(a.endswith('_%Y%m%d_%H%M%S_color.mkv') for a in cmd)


def test_non_utc_host_is_reported(capture, monkeypatch, caplog):
    """A station on a local timezone must say so: chunks it wrote before this
    fix are stamped with the offset and cannot be matched retroactively."""
    import time as _time

    monkeypatch.setattr(_time, 'daylight', 0)
    monkeypatch.setattr(_time, 'timezone', -3 * 3600)
    monkeypatch.setattr(capture, '_start_ffmpeg', lambda sid: None)
    monkeypatch.setattr(capture, '_run_loop', lambda: None, raising=False)
    with caplog.at_level('WARNING'):
        try:
            capture.run()
        except Exception:
            pass
    assert any('timezone' in r.message.lower() for r in caplog.records), (
        'no warning emitted for a non-UTC host clock'
    )
