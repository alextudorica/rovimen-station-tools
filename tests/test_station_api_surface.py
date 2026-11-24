"""Characterization tests for the station API's HTTP surface (station_api.py).

Pins what the dashboard, the VPS poller and the pusher depend on before the
file is restructured:

  * the route inventory, so a deleted or renamed route is a deliberate edit
    here rather than a silent break of a caller;
  * the JSON shape of /api/status and /api/vitals (dashboard/ingest_api.py
    HeartbeatPayload/VitalsPayload and rovimen_pusher build the heartbeat
    from them);
  * /api/chunks (locked_only, the from/to window wrapping midnight, the
    in-progress segment skip, the state.json ready filter), /api/nights,
    /api/timelapses and /api/detections-index;
  * _INDEX_COLS staying equal to detection_indexer.COLS;
  * the loopback PATCH /api/settings write channel end to end.

The collectors live in the Flask-free station_collect module and station_api
re-exports them; ``_patch`` sets a name in both so these tests describe
behaviour, not file layout.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pytest

import detection_indexer
import flags_manager
import station_api
import station_collect

LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}

EXPECTED_ROUTES = {
    ('/api/calibration/autofit/<cam>', ('POST',), 'api_calibration_autofit'),
    ('/api/calibration/autofit_accept/<cam>', ('POST',), 'api_calibration_autofit_accept'),
    ('/api/calibration/autofit_status/<cam>', ('GET',), 'api_calibration_autofit_status'),
    ('/api/calibration/rebase_pointing/<cam>', ('POST',), 'api_calibration_rebase_pointing'),
    ('/api/calibration_summary/<cam>', ('GET',), 'api_calibration_summary'),
    ('/api/chunks/<cam>/<date>', ('GET',), 'api_chunks'),
    ('/api/color-meteor-stack/<cam>/<date>', ('GET',), 'api_color_meteor_stack'),
    ('/api/crons', ('GET',), 'api_crons'),
    ('/api/dawn/progress', ('GET',), 'api_dawn_progress'),
    ('/api/detections-index', ('GET',), 'api_detections_index'),
    ('/api/encode_chunk', ('POST',), 'api_encode_chunk'),
    ('/api/encoding/stats', ('GET',), 'api_encoding_stats'),
    ('/api/fireball_pack/<cam>/<date>', ('GET',), 'api_fireball_pack'),
    ('/api/hardware', ('GET',), 'api_hardware'),
    ('/api/latest_ff_maxpixel/<cam>', ('GET',), 'api_latest_ff_maxpixel'),
    ('/api/latest_frame/<cam>', ('GET',), 'api_latest_frame'),
    ('/api/latest_stack/<cam>', ('GET',), 'api_latest_stack'),
    ('/api/live_cameras', ('GET',), 'api_live_cameras'),
    ('/api/lock/<cam>/<date>/<filename>', ('POST',), 'api_lock'),
    ('/api/logo/list', ('GET',), 'api_logo_list'),
    ('/api/overlay/logo', ('POST',), 'api_overlay_logo'),
    ('/api/logs/<service>', ('GET',), 'api_logs'),
    ('/api/mask/<cam>', ('POST',), 'api_mask_write'),
    ('/api/mask_image/<cam>', ('GET',), 'api_mask_image'),
    ('/api/nights/<cam>', ('GET',), 'api_nights'),
    ('/api/overlay_preview', ('POST',), 'api_overlay_preview'),
    ('/api/platepar', ('GET',), 'api_platepar'),
    ('/api/probe', ('POST',), 'api_probe'),
    ('/api/process_clip/<cam>/<date>/<filename>', ('POST',), 'api_process_clip'),
    ('/api/process_clip/<cam>/<date>/<filename>', ('GET',), 'api_process_clip_status'),
    ('/api/rms-detections/<cam>/<date>', ('GET',), 'api_rms_detections'),
    ('/api/rms/plot_image/<cam>/<date>/<filename>', ('GET',), 'api_rms_plot_image'),
    ('/api/rms/plots/<cam>/<date>', ('GET',), 'api_rms_plots'),
    ('/api/rms/status', ('GET',), 'api_rms_status'),
    ('/api/rms_cameras', ('GET',), 'api_rms_cameras'),
    ('/api/rmsnights/<cam>', ('GET',), 'api_rmsnights'),
    ('/api/rovimen/status', ('GET',), 'api_rovimen_status'),
    ('/api/settings', ('GET',), 'api_settings'),
    ('/api/settings', ('PATCH',), 'api_settings_patch'),
    ('/api/shortclip/<cam>/<date>/<filename>', ('GET',), 'api_shortclip'),
    ('/api/shortclip_stitch/<cam>/<date>', ('GET',), 'api_shortclip_stitch'),
    ('/api/status', ('GET',), 'api_status'),
    ('/api/stitch_video/<cam>/<date>', ('GET',), 'api_stitch_video'),
    ('/api/storagewatch/status', ('GET',), 'api_storagewatch_status'),
    ('/api/stream/<cam_code>', ('GET',), 'api_stream'),
    ('/api/timelapses', ('GET',), 'api_timelapses'),
    ('/api/updater/status', ('GET',), 'api_updater_status'),
    ('/api/vitals', ('GET',), 'api_vitals'),
    ('/color_capture/<cam>/<date>/<filename>', ('GET',), 'serve_capture'),
    ('/color_timelapse/<cam>/<date>/<filename>', ('GET',), 'serve_timelapse'),
    ('/night_stack/<cam>/<date>/<filename>', ('GET',), 'serve_timelapse'),
    ('/thumbnail/<cam>/<date>/<filename>', ('GET',), 'serve_thumbnail'),
}


def _modules() -> list:
    return [station_api, station_collect]


def _patch(monkeypatch, name: str, value) -> None:
    """Set ``name`` on every module that defines it (station_api re-exports
    the collectors from station_collect once they move there)."""
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
    cfg = {
        "videocapture_path": str(cap),
        "segment_duration": 20,
        "stations": {"RO000A": {}},
    }
    _patch(monkeypatch, "load_config", lambda: cfg)
    _patch(monkeypatch, "capture_base", lambda: cap)
    _patch(monkeypatch, "_STATE_MANAGER_OK", True)
    _patch(monkeypatch, "_flags_manager", flags_manager)
    _clear_cache()
    station_api.app.testing = True
    yield {"cap": cap, "cfg": cfg, "client": station_api.app.test_client()}
    _clear_cache()


def _touch(path: Path, age_s: float = 3600.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\x1a\x45\xdf\xa3")
    t = time.time() - age_s
    os.utime(path, (t, t))
    return path


# ── Route inventory ─────────────────────────────────────────────────────────


def test_route_inventory_is_deliberate():
    routes = {
        (r.rule, tuple(sorted(r.methods - {"HEAD", "OPTIONS"})), r.endpoint)
        for r in station_api.app.url_map.iter_rules()
        if r.endpoint != "static"
    }
    assert routes == EXPECTED_ROUTES


def test_index_cols_match_indexer():
    assert tuple(station_api._INDEX_COLS) == tuple(detection_indexer.COLS)


def test_station_collect_imports_without_third_party_packages():
    """The pusher runs on the system python3, which has no Flask, psutil,
    numpy or Pillow; station_collect must import there."""
    import subprocess

    code = (
        "import sys\n"
        "for name in ('flask', 'psutil', 'numpy', 'PIL', 'backports'):\n"
        "    sys.modules[name] = None\n"
        "import station_collect\n"
        "assert not hasattr(station_collect, 'app')\n"
    )
    scripts = Path(station_collect.__file__).parent
    result = subprocess.run([sys.executable, "-c", code], cwd=str(scripts),
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr


# ── /api/status and /api/vitals shape ───────────────────────────────────────


class _Done:
    def __init__(self, stdout: str = "", returncode: int = 0):
        self.stdout = stdout
        self.returncode = returncode
        self.stderr = ""


def _fake_run(cmd, *a, **k):
    if cmd[:2] == ["df", "-BM"] and cmd[2].startswith("--output"):
        return _Done("Filesystem 1M-blocks Used Avail Use% Mounted on\n"
                     "/dev/sda1 100000M 40000M 60000M 40% /\n"
                     "/dev/sdb1 200000M 1000M 199000M 1% /mnt/data\n"
                     "/dev/sdc1 29000M 5M 28995M 1% /media/pi/SANDISK USB\n")
    if cmd[:2] == ["df", "-BM"]:
        return _Done("Filesystem 1M-blocks Used Available Use% Mounted on\n"
                     "/dev/sda1 100000M 40000M 60000M 40% /\n")
    if cmd[:2] == ["df", "--output=source"]:
        return _Done("Filesystem\n/dev/sda1\n")
    if cmd[:1] == ["du"]:
        return _Done("1024\t/x\n")
    return _Done("")


def test_status_shape(station, monkeypatch):
    monkeypatch.setattr(station_api.subprocess, "run", _fake_run)
    _patch(monkeypatch, "_collect_service_states", lambda base: {"color-capture": "active"})
    _patch(monkeypatch, "_rms_camera_dirs", lambda: [])
    resp = station["client"].get("/api/status")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("online", "last_updated", "services", "storage", "disk",
                "extra_disks", "rms", "segment_health", "bundle"):
        assert key in body, key
    assert body["online"] is True
    assert body["services"] == {"color-capture": "active"}
    assert set(body["disk"]) == {"used_mb", "total_mb", "pct", "other_mb", "device"}
    assert body["disk"]["pct"] == 40
    assert body["extra_disks"] == [{
        "source": "/dev/sdb1", "mount": "/mnt/data", "total_mb": 200000,
        "used_mb": 1000, "avail_mb": 199000, "pct": 1,
    }, {
        # A mount point with a space (desktop automount of a USB stick)
        "source": "/dev/sdc1", "mount": "/media/pi/SANDISK USB", "total_mb": 29000,
        "used_mb": 5, "avail_mb": 28995, "pct": 1,
    }]
    assert "RO000A" in body["segment_health"]
    assert set(body["bundle"]) >= {"version", "build", "channel", "verify_mode"}


class _FakePsutil:
    """Just enough of psutil for /api/vitals: the dev venv does not ship it,
    the station RMS venvs do."""

    class NoSuchProcess(Exception):
        pass

    class AccessDenied(Exception):
        pass

    class _Freq:
        current = 2400.0

    class _Mem:
        percent = 41.5
        used = 3 * 1024 * 1024 * 1024
        total = 8 * 1024 * 1024 * 1024

    class _Temp:
        current = 48.3

    class _IO:
        write_bytes = 1_000_000

    @staticmethod
    def cpu_percent(interval=None, percpu=False):
        return [10.0, 30.0] if percpu else 20.0

    @classmethod
    def cpu_freq(cls, percpu=False):
        return [cls._Freq(), cls._Freq()]

    @classmethod
    def virtual_memory(cls):
        return cls._Mem()

    @classmethod
    def sensors_temperatures(cls):
        return {"coretemp": [cls._Temp()]}

    @classmethod
    def disk_io_counters(cls):
        return cls._IO()

    @staticmethod
    def process_iter(attrs=None):
        return iter(())


def test_vitals_shape(station, monkeypatch):
    monkeypatch.setitem(sys.modules, "psutil", _FakePsutil)
    resp = station["client"].get("/api/vitals")
    assert resp.status_code == 200
    body = resp.get_json()
    for key in ("online", "last_updated", "cpu_pct", "cores", "ram_pct",
                "ram_used_mb", "ram_total_mb", "total_cores",
                "disk_write_mbps", "top_procs"):
        assert key in body, key
    assert isinstance(body["cores"], list) and body["cores"]
    assert set(body["cores"][0]) == {"load_pct", "freq_ghz"}
    assert body["total_cores"] == len(body["cores"]) == 2
    assert body["cpu_pct"] == 20.0
    assert body["temp_c"] == 48.3
    assert body["ram_total_mb"] == 8192


# ── /api/chunks ─────────────────────────────────────────────────────────────


NIGHT = "20260925"
CAM = "RO000A"


def _night_dir(station) -> Path:
    return station["cap"] / CAM / NIGHT


def _chunk(station, stamp: str, age_s: float = 3600.0) -> Path:
    date, hhmmss = stamp.split("_")
    return _touch(_night_dir(station) / f"{CAM}_{date}_{hhmmss}_color.mkv", age_s)


def _names(body) -> list[str]:
    return [c["filename"] for c in body["chunks"]]


def test_chunks_legacy_night_without_state_lists_all(station):
    _chunk(station, "20260925_200000")
    _chunk(station, "20260925_235950")
    body = station["client"].get(f"/api/chunks/{CAM}/{NIGHT}").get_json()
    assert body["morning_done"] is False
    assert _names(body) == [
        f"{CAM}_20260925_200000_color.mkv", f"{CAM}_20260925_235950_color.mkv",
    ]
    first = body["chunks"][0]
    assert set(first) == {"filename", "time", "stack", "size_mb", "locked",
                          "lock_type", "detection_offset_s", "meteor_time",
                          "reencoded"}
    assert first["time"] == "20:00:00"
    assert first["stack"] == f"{CAM}_20260925_200000_stack.webp"
    assert first["locked"] is False


def test_chunks_missing_night_is_empty_list(station):
    assert station["client"].get(f"/api/chunks/{CAM}/{NIGHT}").get_json() == []


def test_chunks_from_to_window_wraps_midnight(station):
    for stamp in ("20260925_200000", "20260925_235950", "20260926_000010",
                  "20260926_020000"):
        _chunk(station, stamp)
    body = station["client"].get(
        f"/api/chunks/{CAM}/{NIGHT}?from=23:00&to=01:00").get_json()
    assert _names(body) == [
        f"{CAM}_20260925_235950_color.mkv", f"{CAM}_20260926_000010_color.mkv",
    ]


def test_chunks_skip_segment_still_being_written(station):
    _chunk(station, "20260925_200000")
    _chunk(station, "20260925_200020", age_s=1.0)
    body = station["client"].get(f"/api/chunks/{CAM}/{NIGHT}").get_json()
    assert _names(body) == [f"{CAM}_20260925_200000_color.mkv"]


def test_chunks_state_ready_filter_and_locked_only(station):
    cfg = station["cfg"]
    a = _chunk(station, "20260925_200000").name
    b = _chunk(station, "20260925_200020").name
    _chunk(station, "20260925_200040")  # on disk, never marked ready
    flags_manager.mark_ready_batch(CAM, NIGHT, [a, b], cfg)
    flags_manager.lock_chunk(CAM, NIGHT, b, {
        "lock_type": "detection",
        "detection_time": "20260925_200025",
        "meteor_time": "2026-09-25T20:00:27.500000",
    }, cfg)
    client = station["client"]

    body = client.get(f"/api/chunks/{CAM}/{NIGHT}").get_json()
    assert _names(body) == [a, b]

    locked = client.get(f"/api/chunks/{CAM}/{NIGHT}?locked_only=1").get_json()
    assert _names(locked) == [b]
    row = locked["chunks"][0]
    assert row["locked"] is True
    assert row["lock_type"] == "detection"
    assert row["detection_offset_s"] == 7.5
    assert row["meteor_time"] == "2026-09-25T20:00:27.500000"

    manual = client.get(f"/api/chunks/{CAM}/{NIGHT}?lock_type=manual").get_json()
    assert _names(manual) == []


def test_chunks_reports_reencoded_from_state(station):
    cfg = station["cfg"]
    a = _chunk(station, "20260925_200000").name
    flags_manager.mark_ready_batch(CAM, NIGHT, [a], cfg)
    flags_manager.mark_reencoded(CAM, NIGHT, a, cfg)
    body = station["client"].get(f"/api/chunks/{CAM}/{NIGHT}").get_json()
    assert body["chunks"][0]["reencoded"] is True


@pytest.mark.parametrize("path", [
    "/api/chunks/ro000a/20260925", "/api/chunks/RO000A/2026092",
])
def test_chunks_rejects_bad_path(station, path):
    assert station["client"].get(path).status_code == 400


# ── /api/nights and /api/timelapses ─────────────────────────────────────────


def test_nights_lists_dirs_holding_clips_newest_first(station):
    _chunk(station, "20260925_200000")
    _touch(station["cap"] / CAM / "20260923" / f"{CAM}_20260923_200000_color.mkv")
    (station["cap"] / CAM / "20260924").mkdir()          # empty night
    (station["cap"] / CAM / "stacks").mkdir()            # not a night
    body = station["client"].get(f"/api/nights/{CAM}").get_json()
    assert body == ["20260925", "20260923"]


def test_nights_unknown_camera_is_empty(station):
    assert station["client"].get("/api/nights/RO000Z").get_json() == []


def test_timelapses_listing(station):
    n1 = station["cap"] / CAM / "20260925"
    n2 = station["cap"] / CAM / "20260924"
    _touch(n1 / f"{CAM}_20260925_timelapse.mp4")
    _touch(n1 / f"{CAM}_20260925_night_stack.webp")
    _touch(n2 / f"{CAM}_20260924_timelapse.mp4")
    body = station["client"].get("/api/timelapses").get_json()
    assert body == {CAM: [
        {"date": "20260925", "filename": f"{CAM}_20260925_timelapse.mp4",
         "night_stack": f"{CAM}_20260925_night_stack.webp"},
        {"date": "20260924", "filename": f"{CAM}_20260924_timelapse.mp4",
         "night_stack": None},
    ]}


# ── /api/detections-index ───────────────────────────────────────────────────


def _index_db(path: Path) -> Path:
    con = sqlite3.connect(str(path))
    con.executescript(detection_indexer._SCHEMA)
    rows = [
        ("RO000A", "20260920", "FF_RO000A_20260920_210000_000_0000000.fits", 1,
         "2026-09-20T21:00:01"),
        ("RO000A", "20260925", "FF_RO000A_20260925_210000_000_0000000.fits", 1,
         "2026-09-25T21:00:01"),
    ]
    con.executemany(
        "INSERT INTO detections (cam, date, ff_file, meteor_no, time_utc) VALUES (?,?,?,?,?)",
        rows)
    con.execute(
        "INSERT INTO night_sky (cam, night, n_ff, median_stars, cloud_fraction, updated)"
        " VALUES ('RO000A', '20260925', 900, 55.0, 0.1, '2026-09-26T05:00:00')")
    con.commit()
    con.close()
    return path


def test_detections_index_filters_by_since(station, tmp_path, monkeypatch):
    _patch(monkeypatch, "_INDEX_DB", _index_db(tmp_path / "detections_index.db"))
    body = station["client"].get("/api/detections-index?since=20260921").get_json()
    assert body["ready"] is True
    assert body["count"] == 1
    assert body["detections"][0]["date"] == "20260925"
    assert set(body["detections"][0]) == set(detection_indexer.COLS)
    assert body["night_sky"] == [{
        "cam": "RO000A", "night": "20260925", "n_ff": 900, "median_stars": 55.0,
        "cloud_fraction": 0.1, "updated": "2026-09-26T05:00:00",
    }]
    everything = station["client"].get("/api/detections-index").get_json()
    assert everything["count"] == 2


def test_detections_index_without_db_is_not_ready(station, tmp_path, monkeypatch):
    _patch(monkeypatch, "_INDEX_DB", tmp_path / "missing.db")
    body = station["client"].get("/api/detections-index").get_json()
    assert body == {"detections": [], "night_sky": [], "ready": False}


def test_detections_index_rejects_bad_since(station):
    assert station["client"].get("/api/detections-index?since=2026").status_code == 400


# ── PATCH /api/settings (loopback write channel) ────────────────────────────


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "rovimen_scripts" / "config.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"a": {"b": 1, "c": 2}, "x": 1}))
    monkeypatch.setattr(station_api, "CONFIG_PATH", path)
    for mod in _modules():
        if hasattr(mod, "CONFIG_PATH"):
            monkeypatch.setattr(mod, "CONFIG_PATH", path)
    _clear_cache()
    station_api.app.testing = True
    yield path
    _clear_cache()


def test_patch_settings_deep_merges_into_config(config_file):
    client = station_api.app.test_client()
    resp = client.patch("/api/settings", json={"a": {"b": 5}, "y": [1]},
                        environ_overrides=LOOPBACK)
    assert resp.status_code == 200
    assert resp.get_json()["ok"] is True
    assert json.loads(config_file.read_text()) == {
        "a": {"b": 5, "c": 2}, "x": 1, "y": [1],
    }
    # The cached config is refreshed, so a following GET sees the write.
    assert client.get("/api/settings").get_json()["a"]["b"] == 5


def test_patch_settings_rejects_non_object(config_file):
    client = station_api.app.test_client()
    resp = client.patch("/api/settings", json=[1, 2], environ_overrides=LOOPBACK)
    assert resp.status_code == 400
    assert json.loads(config_file.read_text())["a"] == {"b": 1, "c": 2}
