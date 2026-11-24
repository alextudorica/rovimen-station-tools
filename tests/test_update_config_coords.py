"""update_config_coords_from_platepar writes config.json under the shared
writer lock through a private temp file (no fixed config.json.tmp)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

import update_config_coords_from_platepar as ucc


@pytest.fixture
def station(tmp_path, monkeypatch):
    cfg = tmp_path / 'scripts' / 'config.json'
    cfg.parent.mkdir()
    cfg.write_text(json.dumps({'stations': {'RO000M': {'az': 1.0}}, 'keep': True}, indent=4))
    pp_dir = tmp_path / 'RMS_cam1'
    pp_dir.mkdir()
    # The camera's RMS config dir: its .config declares the station ID.
    (pp_dir / '.config').write_text('[System]\nstationID: RO000M\n')
    (pp_dir / 'platepar_cmn2010.cal').write_text(json.dumps({
        'station_code': 'RO000M', 'lat': 46.1, 'lon': 23.5, 'elev': 400.0,
        'az_centre': 123.456, 'alt_centre': 45.678}))
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setattr(ucc.rovimen_rms, 'live_rms_config_dirs', lambda: [])
    monkeypatch.setattr(ucc, 'CONFIG_PATH', cfg)
    return cfg


def _run(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, 'argv', ['update_config_coords_from_platepar.py', *args])
    ucc.main()


def test_updates_under_the_lock_without_a_fixed_temp(station, monkeypatch):
    import contextlib

    seen = []
    real_lock = ucc.rovimen_config.config_lock

    @contextlib.contextmanager
    def spy(path):
        seen.append(Path(path))
        with real_lock(path):
            yield

    monkeypatch.setattr(ucc.rovimen_config, 'config_lock', spy)
    _run(monkeypatch)
    data = json.loads(station.read_text())
    assert data['stations']['RO000M'] == {'az': 123.5, 'alt': 45.7}
    assert (data['latitude'], data['longitude'], data['elevation']) == (46.1, 23.5, 400.0)
    assert data['keep'] is True
    assert seen == [station]
    assert sorted(p.name for p in station.parent.iterdir()) == ['.config.lock', 'config.json']


def test_dry_run_writes_nothing(station, monkeypatch):
    before = station.read_text()
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, '--dry-run')
    assert exc.value.code == 0
    assert station.read_text() == before
