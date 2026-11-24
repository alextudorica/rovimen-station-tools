"""Station-side fireball analysis pack (/api/fireball_pack/<cam>/<date>).

The pack must hand an off-station analyst everything astrometry/photometry
needs for one event: the night's platepars, FTPdetectinfo, CALSTARS, RMS
.config, mask, the FF/FR frames around the event time, and the night's
best-star-count FF as a calibration reference. These tests build a fake
RMS_data tree and pin the selection rules — window filtering, the frame
cap trimming edges (never the event), CALSTARS-driven best-FF choice, and
the morning-hours date rollover.
"""

from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pytest

import station_api

CAM = "RO0017"
NIGHT = "20260730"
SESSION = f"{CAM}_{NIGHT}_204512_123456"


def _ff(time_s: str, suffix: str = "fits", prefix: str = "FF") -> str:
    return f"{prefix}_{CAM}_{NIGHT}_{time_s}_000_0000000.{suffix}"


def _make_night(tmp_path: Path) -> dict:
    """Fake RMS tree with one archived session; returns the station cfg."""
    sess = tmp_path / "RMS_data" / CAM / "ArchivedFiles" / SESSION
    sess.mkdir(parents=True)
    (sess / "platepar_cmn2010.cal").write_text("{}")
    (sess / "platepars_all_recalibrated.json").write_text("{}")
    (sess / f"FTPdetectinfo_{SESSION}.txt").write_text("detections")
    (sess / ".config").write_text("[System]")
    (sess / "mask.bmp").write_text("mask")
    (sess / f"CALSTARS_{SESSION}.txt").write_text(
        "==========\n"
        f"{_ff('210000')}\n"
        "  10.0  20.0  1000  50.0\n"
        "  11.0  21.0  1100  51.0\n"
        f"{_ff('220301')}\n"
        "  10.0  20.0  1000  50.0\n"
        "  11.0  21.0  1100  51.0\n"
        "  12.0  22.0  1200  52.0\n"
    )
    # Frames: one inside the ±60 s default window of 22:03:39, one at the
    # window edge, one far outside, plus an FR bin inside.
    for t in ("210000", "220301", "220342", "230000"):
        (sess / _ff(t)).write_bytes(b"F" * 32)
    (sess / _ff("220322", suffix="bin", prefix="FR")).write_bytes(b"R" * 32)
    return {"stations": {CAM: {"rms_data_path": str(tmp_path / "RMS_data" / CAM)}}}


@pytest.fixture()
def client():
    return station_api.app.test_client()


class TestHelpers:
    def test_calstars_counts(self, tmp_path):
        cfg = _make_night(tmp_path)
        cs = (Path(cfg["stations"][CAM]["rms_data_path"]) / "ArchivedFiles"
              / SESSION / f"CALSTARS_{SESSION}.txt")
        counts = station_api._parse_calstars_counts(cs)
        assert counts[_ff("210000")] == 2
        assert counts[_ff("220301")] == 3


class TestPackEntries:
    def test_window_selects_event_frames_only(self, tmp_path):
        cfg = _make_night(tmp_path)
        event = datetime(2026, 7, 30, 22, 3, 39)
        entries, info = station_api._fireball_pack_entries(
            CAM, NIGHT, cfg, event, 60.0)
        arcs = [a for a, _ in entries]
        frames = [a for a in arcs if a.startswith("frames/")]
        assert f"frames/{_ff('220301')}" in frames
        assert f"frames/{_ff('220342')}" in frames
        assert f"frames/{_ff('220322', suffix='bin', prefix='FR')}" in frames
        assert not any("230000" in a or "210000" in a for a in frames)

    def test_night_artefacts_and_best_ff(self, tmp_path):
        cfg = _make_night(tmp_path)
        entries, info = station_api._fireball_pack_entries(
            CAM, NIGHT, cfg, datetime(2026, 7, 30, 22, 3, 39), 60.0)
        arcs = [a for a, _ in entries]
        for name in ("platepar_cmn2010.cal", "platepars_all_recalibrated.json",
                     f"FTPdetectinfo_{SESSION}.txt", ".config", "mask.bmp",
                     f"CALSTARS_{SESSION}.txt"):
            assert f"{SESSION}/{name}" in arcs
        # 220301 has 3 CALSTARS rows vs 2 — it is the reference frame.
        assert info["best_calstars_ff"] == {"name": _ff("220301"), "stars": 3}
        assert f"best_calstars_ff/{_ff('220301')}" in arcs

    def test_frame_cap_trims_window_edges_not_event(self, tmp_path):
        cfg = _make_night(tmp_path)
        sess = (Path(cfg["stations"][CAM]["rms_data_path"]) / "ArchivedFiles"
                / SESSION)
        # Flood the window with more frames than the cap allows.
        for i in range(station_api._PACK_MAX_FRAMES + 10):
            (sess / _ff(f"2202{i % 60:02d}")).write_bytes(b"F")
        event = datetime(2026, 7, 30, 22, 2, 30)
        entries, info = station_api._fireball_pack_entries(
            CAM, NIGHT, cfg, event, 300.0)
        frames = [a for a, _ in entries if a.startswith("frames/")]
        assert len(frames) == station_api._PACK_MAX_FRAMES
        assert info["frames_capped"] > 0
        # The frame at the event second must have survived the cap.
        assert f"frames/{_ff('220230')}" in frames

    def test_no_event_time_still_ships_calibration(self, tmp_path):
        cfg = _make_night(tmp_path)
        entries, info = station_api._fireball_pack_entries(
            CAM, NIGHT, cfg, None, 60.0)
        arcs = [a for a, _ in entries]
        assert not any(a.startswith("frames/") for a in arcs)
        assert f"{SESSION}/platepar_cmn2010.cal" in arcs
        assert "event_frames (no time param)" in info["missing"]


class TestEndpoint:
    def test_rejects_bad_params(self, client):
        assert client.get("/api/fireball_pack/bad-cam/20260730").status_code == 400
        assert client.get(f"/api/fireball_pack/{CAM}/2026-07-30").status_code == 400
        assert client.get(
            f"/api/fireball_pack/{CAM}/{NIGHT}?time=9999").status_code == 400

    def test_404_when_no_night_data(self, client, tmp_path):
        cfg = {"stations": {CAM: {"rms_data_path": str(tmp_path / "nope")}}}
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api, "BASE", tmp_path):
            r = client.get(f"/api/fireball_pack/{CAM}/{NIGHT}?time=220339")
        assert r.status_code == 404
        assert r.get_json()["status"] == "no_data"

    def test_zip_roundtrip_with_pack_info(self, client, tmp_path):
        cfg = _make_night(tmp_path)
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api, "BASE", tmp_path):
            r = client.get(f"/api/fireball_pack/{CAM}/{NIGHT}?time=220339")
        assert r.status_code == 200
        assert r.mimetype == "application/zip"
        zf = zipfile.ZipFile(io.BytesIO(r.data))
        names = set(zf.namelist())
        assert "pack_info.json" in names
        assert f"frames/{_ff('220301')}" in names
        info = json.loads(zf.read("pack_info.json"))
        assert info["camera"] == CAM
        assert info["event_time"] == "220339"
        assert set(info["files"]) == names - {"pack_info.json"}

    def test_morning_time_rolls_to_next_calendar_day(self, client, tmp_path):
        """An 01:xx event on night 20260730 lives in FF frames stamped
        20260731 — the endpoint must roll the clip's morning time forward."""
        cfg = _make_night(tmp_path)
        sess = (Path(cfg["stations"][CAM]["rms_data_path"]) / "ArchivedFiles"
                / SESSION)
        morning_ff = f"FF_{CAM}_20260731_011530_000_0000000.fits"
        (sess / morning_ff).write_bytes(b"F" * 32)
        with patch.object(station_api, "load_config", return_value=cfg), \
             patch.object(station_api, "BASE", tmp_path):
            r = client.get(f"/api/fireball_pack/{CAM}/{NIGHT}?time=011540")
        assert r.status_code == 200
        names = set(zipfile.ZipFile(io.BytesIO(r.data)).namelist())
        assert f"frames/{morning_ff}" in names
