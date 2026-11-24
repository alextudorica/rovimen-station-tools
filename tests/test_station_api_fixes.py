"""Regression tests for the station_api fixes of 2026-09-26.

Each test names the failure it pins: encoding/stats reading state.json, the
stitch endpoints leaking their /tmp output, PATCH never touching the read
cache before a successful write, the stale updater fields, rms/status
reporting capture all day, malformed numbers answering HTML 500, the noon
night rollover, duplicated and dropped rms-detections rows, doubled status
counts, and the live-tile render cache keyed on a growing file.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import config_patch
import flags_manager
import station_api
import station_collect

LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
CAM = "RO000A"
NIGHT = "20260925"


def _modules() -> list:
    return [station_api, station_collect]


def _patch(monkeypatch, name: str, value) -> None:
    hit = False
    for mod in _modules():
        if hasattr(mod, name):
            monkeypatch.setattr(mod, name, value)
            hit = True
    assert hit, f"no module defines {name}"


def _clear_cache() -> None:
    for mod in _modules():
        cache = getattr(mod, "_cache", None)
        lock = getattr(mod, "_cache_lock", None)
        if cache is not None and lock is not None:
            with lock:
                cache.clear()


@pytest.fixture
def station(tmp_path, monkeypatch):
    cap = tmp_path / "color_capture"
    cap.mkdir()
    rms = tmp_path / "RMS_data" / "cam1"
    cfg = {
        "videocapture_path": str(cap),
        "segment_duration": 20,
        "raw_bitrate_kbps": 400,
        "stations": {CAM: {"rms_data_path": str(rms)}},
    }
    _patch(monkeypatch, "load_config", lambda: cfg)
    _patch(monkeypatch, "capture_base", lambda: cap)
    _patch(monkeypatch, "_STATE_MANAGER_OK", True)
    _patch(monkeypatch, "_flags_manager", flags_manager)
    _clear_cache()
    station_api.app.testing = True
    yield {"cap": cap, "rms": rms, "cfg": cfg, "tmp": tmp_path,
           "client": station_api.app.test_client()}
    _clear_cache()


def _mkv(path: Path, size: int, age_s: float = 3600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


# ── encoding/stats reads state.json ─────────────────────────────────────────


def test_encoding_stats_counts_state_json_reencoded_chunks(station):
    night = station["cap"] / CAM / NIGHT
    a = _mkv(night / f"{CAM}_{NIGHT}_200000_color.mkv", 600_000).name
    b = _mkv(night / f"{CAM}_{NIGHT}_200020_color.mkv", 700_000).name
    _mkv(night / f"{CAM}_{NIGHT}_200040_color.mkv", 2_000_000)  # raw, not counted
    flags_manager.mark_ready_batch(CAM, NIGHT, [a, b], station["cfg"])
    flags_manager.mark_reencoded(CAM, NIGHT, a, station["cfg"])
    flags_manager.mark_reencoded(CAM, NIGHT, b, station["cfg"])
    body = station["client"].get("/api/encoding/stats").get_json()
    assert body["cameras"][CAM]["night"] == NIGHT
    assert body["cameras"][CAM]["chunks"] == 2
    assert body["overall"]["chunks"] == 2


def test_encoding_stats_still_reads_legacy_sidecars(station):
    night = station["cap"] / CAM / "20260301"
    mkv = _mkv(night / f"{CAM}_20260301_200000_color.mkv", 600_000)
    Path(str(mkv) + ".reencoded").touch()
    body = station["client"].get("/api/encoding/stats").get_json()
    assert body["cameras"][CAM]["chunks"] == 1


# ── stitch endpoints clean up /tmp ──────────────────────────────────────────


@pytest.fixture
def stitch(station, monkeypatch):
    night = station["cap"] / CAM / NIGHT
    _mkv(night / f"{CAM}_{NIGHT}_200000_color.mkv", 10)
    _mkv(night / f"{CAM}_{NIGHT}_200020_color.mkv", 10)
    tmpdir = station["tmp"] / "tmp"
    tmpdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmpdir))
    monkeypatch.setattr(station_api, "_ensure_overlaid", lambda *a, **k: True)
    return tmpdir


class _Proc:
    def __init__(self, returncode: int):
        self.returncode = returncode


@pytest.mark.parametrize("path", [
    f"/api/stitch_video/{CAM}/{NIGHT}",
    f"/api/shortclip_stitch/{CAM}/{NIGHT}",
])
def test_stitch_output_is_deleted_after_sending(station, stitch, monkeypatch, path):
    def fake_ffmpeg(cmd, **_k):
        Path(cmd[-1]).write_bytes(b"mp4data")
        return _Proc(0)

    monkeypatch.setattr(station_api.subprocess, "run", fake_ffmpeg)
    q = f"?f1={CAM}_{NIGHT}_200000_color.mkv&f2={CAM}_{NIGHT}_200020_color.mkv"
    resp = station["client"].get(path + q)
    assert resp.status_code == 200
    assert resp.data == b"mp4data"
    resp.close()
    assert list(stitch.iterdir()) == []


def test_stitch_output_is_deleted_when_ffmpeg_fails(station, stitch, monkeypatch):
    monkeypatch.setattr(station_api.subprocess, "run", lambda cmd, **k: _Proc(1))
    q = f"?f1={CAM}_{NIGHT}_200000_color.mkv&f2={CAM}_{NIGHT}_200020_color.mkv"
    resp = station["client"].get(f"/api/stitch_video/{CAM}/{NIGHT}" + q)
    assert resp.status_code == 500
    assert list(stitch.iterdir()) == []


# ── PATCH never poisons the read cache ──────────────────────────────────────


def test_failed_patch_leaves_cached_settings_untouched(tmp_path, monkeypatch):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"a": 1}))
    for mod in _modules():
        if hasattr(mod, "CONFIG_PATH"):
            monkeypatch.setattr(mod, "CONFIG_PATH", path)
    _clear_cache()
    client = station_api.app.test_client()
    assert client.get("/api/settings").get_json() == {"a": 1}

    def boom(update, config_path):
        raise OSError("disk full")

    monkeypatch.setattr(config_patch, "patch_config", boom)
    resp = client.patch("/api/settings", json={"a": 2}, environ_overrides=LOOPBACK)
    assert resp.status_code == 500
    assert client.get("/api/settings").get_json() == {"a": 1}
    _clear_cache()


# ── updater/status ──────────────────────────────────────────────────────────


def test_updater_status_reports_verifier_state_not_stale_remote(station, tmp_path, monkeypatch):
    state = tmp_path / "bundle_state.json"
    state.write_text(json.dumps({"build": 312, "last_verify": "ok",
                                 "verified_at": "2026-09-26T14:00:07Z"}))
    monkeypatch.setenv("ROVIMEN_BUNDLE_STATE", str(state))
    body = station["client"].get("/api/updater/status").get_json()
    assert "remote" not in body and "up_to_date" not in body
    assert body["status"] == "verify ok at 2026-09-26T14:00:07Z"
    assert body["bundle"]["build"] == 312


# ── rms/status ──────────────────────────────────────────────────────────────


class _Out:
    def __init__(self, stdout: str):
        self.stdout = stdout
        self.returncode = 0


def _session(root: Path, sub: str, name: str) -> Path:
    d = root / sub / name
    d.mkdir(parents=True)
    return d


def test_rms_status_capture_needs_a_live_capture_tree(station, monkeypatch):
    session = _session(station["rms"], "CapturedFiles", f"{CAM}_20260926_170000_123456")
    old = time.time() - 7200
    os.utime(session, (old, old))

    def fake_run(cmd, **_k):
        if "StartCapture" in cmd:
            return _Out(f"4242 python -m RMS.StartCapture --camera {CAM}\n")
        return _Out("")

    monkeypatch.setattr(station_api.subprocess, "run", fake_run)
    body = station["client"].get(f"/api/rms/status?date={NIGHT}").get_json()
    assert body["cameras"][CAM]["capturing"] is False  # process asleep all day

    os.utime(session, None)  # written just now
    body = station["client"].get(f"/api/rms/status?date={NIGHT}").get_json()
    assert body["cameras"][CAM]["capturing"] is True


def test_rms_status_processed_ignores_unfiltered_ftpdetectinfo(station, monkeypatch):
    monkeypatch.setattr(station_api.subprocess, "run", lambda cmd, **k: _Out(""))
    session = _session(station["rms"], "CapturedFiles", f"{CAM}_{NIGHT}_190000_123456")
    (session / f"FTPdetectinfo_{CAM}_{NIGHT}_190000_123456_unfiltered.txt").write_text("")
    body = station["client"].get(f"/api/rms/status?date={NIGHT}").get_json()
    assert body["cameras"][CAM]["processed"] is False
    (session / f"FTPdetectinfo_{CAM}_{NIGHT}_190000_123456.txt").write_text("")
    body = station["client"].get(f"/api/rms/status?date={NIGHT}").get_json()
    assert body["cameras"][CAM]["processed"] is True


# ── malformed numbers are 400 JSON, not HTML 500 ────────────────────────────


def test_logs_lines_must_be_a_number(station):
    resp = station["client"].get("/api/logs/dawn?lines=abc")
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "lines must be a number"}
    assert station["client"].get("/api/logs/dawn?lines=-5").status_code == 400


def test_overlay_preview_az_must_be_a_number(station):
    resp = station["client"].post("/api/overlay_preview", json={"az": "north"},
                                  environ_overrides=LOOPBACK)
    assert resp.status_code == 400
    assert resp.get_json() == {"error": "az must be a number"}


@pytest.mark.parametrize("query", ["?ss=abc&t=5", "?ss=1&t=nan", "?pre=x"])
def test_shortclip_numbers_are_checked_before_the_overlay_gate(station, monkeypatch, query):
    _mkv(station["cap"] / CAM / NIGHT / f"{CAM}_{NIGHT}_200000_color.mkv", 10)

    def gate(*_a, **_k):
        raise AssertionError("the overlay gate must not run for a bad request")

    monkeypatch.setattr(station_api, "_ensure_overlaid", gate)
    resp = station["client"].get(
        f"/api/shortclip/{CAM}/{NIGHT}/{CAM}_{NIGHT}_200000_color.mkv{query}")
    assert resp.status_code == 400
    assert "must be a" in resp.get_json()["error"]


# ── default night follows dawn's 14:00 rollover ─────────────────────────────


@pytest.mark.parametrize("hour,expected", [
    (3, "20260925"), (12, "20260925"), (13, "20260925"), (14, "20260926"), (23, "20260926"),
])
def test_last_completed_night_rolls_over_at_14_utc(hour, expected):
    now = datetime(2026, 9, 26, hour, 30, tzinfo=timezone.utc)
    assert station_api._last_completed_night(now) == expected


# ── rms-detections: sessions de-duplicated, indexer parse widths ────────────


_FTP = """Meteor Count = 000001
-----------------------------------------------------
Processed with RMS
-----------------------------------------------------
FF file processed
CAL file processed
Cam# Meteor# #Segments fps hnr mle bin Pix/fm Rho Phi
Per segment:  Frame# Col Row RA Dec Azim Elev Inten Mag
-------------------------------------------------------
{ff}
Recalibrated with RMS on: 2026-09-26 05:00:00.000000 UTC
{cam} 0001 0003 0025.00 000.0 000.0  00.0 000.0 0000.0 0000.0
0010.0000 0100.00 0200.00 010.0000 +20.0000 090.0000 +45.0000 000100 +2.50
0020.0000 0110.00 0210.00 011.0000 +21.0000 091.0000 +46.0000 000100 +1.50
0030.0000 0120.00 0220.00 012.0000 +22.0000 092.0000 +47.0000 000100 +2.00
"""


def test_rms_detections_counts_an_archived_session_once(station):
    name = f"{CAM}_{NIGHT}_190000_123456"
    ff = f"FF_{CAM}_{NIGHT}_201500_000_0000512.fits"
    for sub in ("CapturedFiles", "ArchivedFiles"):
        d = _session(station["rms"], sub, name)
        (d / f"FTPdetectinfo_{name}.txt").write_text(_FTP.format(ff=ff, cam=CAM))
    body = station["client"].get(f"/api/rms-detections/{CAM}/{NIGHT}").get_json()
    assert body["count"] == 1
    det = body["detections"][0]
    # Nine frame columns are enough: duration and speed are computed.
    assert det["duration_s"] == pytest.approx(0.8)
    assert det["angular_velocity"] is not None
    assert det["mag_apparent"] == 1.5


def test_rms_detections_rows_are_the_indexers_rows(station):
    """The live endpoint returns exactly what detection_indexer stores for the
    session (rovimen_rms), so a night's rows do not change when the dashboard
    switches from the station to the index the next morning."""
    import rovimen_rms

    name = f"{CAM}_{NIGHT}_190000_123456"
    ff = f"FF_{CAM}_{NIGHT}_201500_000_0000512.fits"
    session = _session(station["rms"], "ArchivedFiles", name)
    (session / f"FTPdetectinfo_{name}.txt").write_text(_FTP.format(ff=ff, cam=CAM))
    body = station["client"].get(f"/api/rms-detections/{CAM}/{NIGHT}").get_json()
    want = json.loads(json.dumps(rovimen_rms.session_detections(session)))
    assert body["detections"] == want
    # No radiants file: coordinates come from the FTPdetectinfo frames.
    assert body["detections"][0]["ra_beg"] == pytest.approx(10.0)
    assert body["detections"][0]["meteor_no"] == 1


def _platepar(path: Path, az: float, age_s: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"az_centre": az, "alt_centre": 40.0}))
    t = time.time() - age_s
    os.utime(path, (t, t))


def test_platepar_reads_the_live_rms_config_dir_not_a_stale_mirror(station, monkeypatch):
    """The live platepar sits in the RMS config directory (~/RMS_cam1 here,
    whose .config declares the camera); ~/source/Stations/<cam> is an inert
    mirror on this layout. The mirror is newer, and used to win."""
    import rovimen_rms

    home = station["tmp"]
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(rovimen_rms, "live_rms_config_dirs", lambda: [])
    live_dir = home / "RMS_cam1"
    live_dir.mkdir()
    (live_dir / ".config").write_text(f"[System]\nstationID: {CAM}\n")
    _platepar(live_dir / "platepar_cmn2010.cal", az=123.0, age_s=600)
    _platepar(home / "source" / "Stations" / CAM / "platepar_cmn2010.cal", az=10.0, age_s=60)
    _platepar(station["rms"] / "ArchivedFiles" / f"{CAM}_{NIGHT}_190000_123456"
              / "platepar_cmn2010.cal", az=120.0, age_s=86400)

    body = station["client"].get("/api/platepar").get_json()
    assert body[CAM]["az_centre"] == 123.0
    assert body[CAM]["source"] == str(live_dir / "platepar_cmn2010.cal")


def test_platepar_falls_back_to_the_newest_session_copy(station, monkeypatch):
    import rovimen_rms

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: station["tmp"]))
    monkeypatch.setattr(rovimen_rms, "live_rms_config_dirs", lambda: [])
    sessions = station["rms"] / "ArchivedFiles"
    _platepar(sessions / f"{CAM}_20260924_190000_000000" / "platepar_cmn2010.cal", 1.0, 90000)
    _platepar(sessions / f"{CAM}_{NIGHT}_190000_000000" / "platepar_cmn2010.cal", 2.0, 3600)
    body = station["client"].get("/api/platepar").get_json()
    assert body[CAM]["az_centre"] == 2.0


def test_rms_detections_empty_night_reports_zero(station):
    body = station["client"].get(f"/api/rms-detections/{CAM}/{NIGHT}").get_json()
    assert body == {"camera": CAM, "date": NIGHT, "count": 0, "detections": []}


def test_status_rms_count_is_not_doubled_by_archived_copies(station, monkeypatch):
    name = f"{CAM}_{NIGHT}_190000_123456"
    ff = f"FF_{CAM}_{NIGHT}_201500_000_0000512.fits"
    for sub in ("CapturedFiles", "ArchivedFiles"):
        d = _session(station["rms"], sub, name)
        (d / f"FTPdetectinfo_{name}.txt").write_text(_FTP.format(ff=ff, cam=CAM))
    _patch(monkeypatch, "_rms_camera_dirs", lambda: [("cam1", station["rms"], CAM)])
    _patch(monkeypatch, "_collect_service_states", lambda base: {})
    monkeypatch.setattr(station_api.subprocess, "run", lambda cmd, **k: _Out(""))
    body = station["client"].get("/api/status").get_json()
    assert body["rms"]["cam1"]["total_count"] == 1
    assert body["rms"]["cam1"]["session_count"] == 1


# ── live tile render cache ──────────────────────────────────────────────────


def test_live_tile_cache_key_is_stable_while_a_chunk_grows(station, monkeypatch):
    _patch(monkeypatch, "_FRAME_STABLE_BYTES", 1_000)
    chunk = _mkv(station["cap"] / CAM / NIGHT / f"{CAM}_{NIGHT}_200000_color.mkv",
                 2_000, age_s=0)
    keys = []

    def fake(path, inode, size_key):
        keys.append((path, inode, size_key))
        return b"RIFF"

    _patch(monkeypatch, "_colour_frame_webp_bytes", fake)
    station_collect._latest_colour_frame(CAM)
    with chunk.open("ab") as fh:
        fh.write(b"\0" * 5_000)
    station_collect._latest_colour_frame(CAM)
    assert keys[0] == keys[1]


# ── perf: fewer stats and no blocking CPU sample ────────────────────────────


def test_locked_only_stats_only_the_locked_chunks(station, monkeypatch):
    night = station["cap"] / CAM / NIGHT
    names = [_mkv(night / f"{CAM}_{NIGHT}_2000{s:02d}_color.mkv", 10).name
             for s in range(0, 60, 20)]
    flags_manager.mark_ready_batch(CAM, NIGHT, names, station["cfg"])
    flags_manager.lock_chunk(CAM, NIGHT, names[1], {
        "lock_type": "manual", "detection_time": f"{NIGHT}_200020", "meteor_time": None,
    }, station["cfg"])
    statted = []
    real_stat = Path.stat

    def counting_stat(self, *a, **k):
        if self.name.endswith("_color.mkv"):
            statted.append(self.name)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", counting_stat)
    body = station["client"].get(f"/api/chunks/{CAM}/{NIGHT}?locked_only=1").get_json()
    assert [c["filename"] for c in body["chunks"]] == [names[1]]
    assert statted == [names[1]]


class _CountingPsutil:
    intervals: list = []
    sensor_scans = 0

    class _Mem:
        percent = 10.0
        used = 1024 * 1024 * 1024
        total = 4 * 1024 * 1024 * 1024

    class _IO:
        write_bytes = 0

    @classmethod
    def cpu_percent(cls, interval=None, percpu=False):
        cls.intervals.append(interval)
        return [5.0, 15.0]

    @staticmethod
    def cpu_freq(percpu=False):
        return []

    @classmethod
    def virtual_memory(cls):
        return cls._Mem()

    @classmethod
    def sensors_temperatures(cls):
        cls.sensor_scans += 1
        return {}

    @classmethod
    def disk_io_counters(cls):
        return cls._IO()


def test_vitals_sample_cpu_without_blocking_after_the_first_call(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "psutil", _CountingPsutil)
    _CountingPsutil.intervals = []
    _CountingPsutil.sensor_scans = 0
    monkeypatch.setattr(station_collect, "_cpu_primed", False)
    station_collect.collect_vitals()
    station_collect.collect_vitals()
    assert _CountingPsutil.intervals == [0.5, None]
    assert _CountingPsutil.sensor_scans == 2  # one scan per reading, not three
