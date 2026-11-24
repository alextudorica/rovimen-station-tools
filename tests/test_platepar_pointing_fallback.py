"""The overlay's pointing fallback (rovimen_rms.enrich_station_cfg_pointing)
reads the platepar RMS actually loads, once an hour per camera, and never
raises into an encode or a stack.

17 of 22 station configs carry no ``az``/``alt``, so the stacker (per chunk)
and the encoder (per clip) looked the pointing up from the platepar every
time, at night, beside capture. That lookup listed every session directory
with an unguarded ``stat``, read the ``~/source/Stations/<cam>`` mirror that
is inert on several stations, and never the live ``~/RMS_*`` config dir. It
lived in overlay.py, which must stay free of sibling imports (the dashboard
compiles it in memory), so it moved next to find_latest_platepar.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

import rovimen_rms
import update_config_coords_from_platepar as ucc

CAM = "RO000M"


def _rms_config(directory: Path, station_id: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".config").write_text(f"[System]\nstationID: {station_id}\n")
    return directory


def _platepar(path: Path, az: float | None, alt: float | None, **extra) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = dict(extra)
    if az is not None:
        data["az_centre"] = az
    if alt is not None:
        data["alt_centre"] = alt
    path.write_text(json.dumps(data))
    return path


@pytest.fixture
def home(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(rovimen_rms, "live_rms_config_dirs", lambda: [])
    rovimen_rms._platepar_pointing.cache_clear()
    yield tmp_path
    rovimen_rms._platepar_pointing.cache_clear()


def _cfg(home: Path) -> dict:
    return {"stations": {CAM: {"rms_data_path": str(home / "RMS_data" / "cam1")}}}


class TestPointingSource:
    def test_the_live_rms_config_platepar_wins_over_the_inert_mirror(self, home):
        live = _platepar(_rms_config(home / "RMS_cam1", CAM) / rovimen_rms.PLATEPAR_NAME,
                         170.0, 40.0)
        mirror = _platepar(home / "source" / "Stations" / CAM / rovimen_rms.PLATEPAR_NAME,
                           10.0, 20.0)
        now = time.time()
        os.utime(live, (now - 60, now - 60))
        os.utime(mirror, (now - 30, now - 30))  # a stale mirror that happens to be newer
        cfg = _cfg(home)
        got = rovimen_rms.enrich_station_cfg_pointing(CAM, cfg["stations"][CAM], cfg)
        assert (got["az"], got["alt"]) == (170.0, 40.0)

    def test_configured_pointing_is_not_looked_up(self, home, monkeypatch):
        def _must_not_look(*a, **k):
            raise AssertionError("az/alt in the config: no platepar lookup")

        monkeypatch.setattr(rovimen_rms, "find_latest_platepar", _must_not_look)
        scfg = {"az": 1.0, "alt": 2.0}
        assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, {"stations": {CAM: scfg}}) is scfg

    def test_a_platepar_without_az_centre_gives_no_pointing(self, home):
        _platepar(_rms_config(home / "RMS_cam1", CAM) / rovimen_rms.PLATEPAR_NAME, None, 40.0)
        cfg = _cfg(home)
        scfg = cfg["stations"][CAM]
        assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, cfg) == scfg

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a mode-000 directory")
    def test_an_unreadable_session_tree_does_not_raise_into_the_encode(self, home):
        captured = home / "RMS_data" / "cam1" / "CapturedFiles"
        _platepar(captured / f"{CAM}_20260920_180000_000000" / rovimen_rms.PLATEPAR_NAME,
                  170.0, 40.0)
        captured.chmod(0)
        try:
            cfg = _cfg(home)
            scfg = cfg["stations"][CAM]
            assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, cfg) == scfg
        finally:
            captured.chmod(0o755)

    def test_a_failing_lookup_leaves_the_camera_without_pointing(self, home, monkeypatch):
        def _eio(*a, **k):
            raise OSError(5, "Input/output error")

        monkeypatch.setattr(rovimen_rms, "find_latest_platepar", _eio)
        cfg = _cfg(home)
        scfg = cfg["stations"][CAM]
        assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, cfg) == scfg


class TestPointingCache:
    def test_one_lookup_per_camera_per_hour(self, home, monkeypatch):
        pp = _platepar(_rms_config(home / "RMS_cam1", CAM) / rovimen_rms.PLATEPAR_NAME,
                       170.0, 40.0)
        calls: list[str] = []
        real = rovimen_rms.find_latest_platepar

        def counting(cam, cfg, *a, **k):
            calls.append(cam)
            return real(cam, cfg, *a, **k)

        monkeypatch.setattr(rovimen_rms, "find_latest_platepar", counting)
        clock = [1_800_000_000.0]
        monkeypatch.setattr(rovimen_rms.time, "time", lambda: clock[0])
        cfg = _cfg(home)
        scfg = cfg["stations"][CAM]
        for _ in range(5):
            assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, cfg)["az"] == 170.0
        assert calls == [CAM]

        # A refit is picked up once the hour turns.
        _platepar(pp, 171.5, 40.0)
        clock[0] += rovimen_rms.POINTING_TTL_S
        assert rovimen_rms.enrich_station_cfg_pointing(CAM, scfg, cfg)["az"] == 171.5
        assert calls == [CAM, CAM]


class TestUpdateConfigCoords:
    def test_finds_the_platepar_outside_rms_cam_dirs(self, home):
        # gmnro06 (Cluj) keeps its live config in ~/source/RMS.
        _platepar(_rms_config(home / "source" / "RMS", CAM) / rovimen_rms.PLATEPAR_NAME,
                  123.456, 45.678, lat=46.1, lon=23.5, elev=400.0)
        cfg = _cfg(home)
        got = ucc.load_platepars(cfg)
        assert got[CAM]["az_centre"] == 123.456

    def test_a_platepar_without_az_centre_does_not_crash(self, home, capsys):
        _platepar(_rms_config(home / "RMS_cam1", CAM) / rovimen_rms.PLATEPAR_NAME,
                  None, None, lat=46.1, lon=23.5, elev=400.0)
        got = ucc.load_platepars(_cfg(home))
        assert CAM in got
        assert "az=?" in capsys.readouterr().out
