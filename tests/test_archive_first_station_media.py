"""Station media the archive already holds is served from the archive only (#1247).

Five dashboard routes asked the station first for data that archive_upload
had already put on the Storage Box: the latest captured stack, the timelapse
listing, the RMS-nights listing, a night's RMS plot list (plus a live HEAD for
the colour meteor stack) and the overview's stack tiles. Each put a station
round-trip on a routine page load, so one slow station held a dashboard worker
for its full timeout and the same pages came up half-empty whenever the
station was offline.

Every test here runs with a station stub that fails the test if anything
contacts a station, over a fake archive laid out the way archive_upload writes
it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

CAM = "AFS001"       # recent nights fully archived
OLD_CAM = "AFS002"   # colour stack only on an old night, only in fdp/
EMPTY_CAM = "AFS003"  # configured, nothing archived


def _ymd(days_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


D1 = _ymd(1)  # last night: rms/, colour stack, timelapse, meteors
D2 = _ymd(2)  # rms/ plots but no colour stack
D40 = _ymd(40)  # outside the index's hot window


def _write(path: Path, data: bytes = b"x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


@pytest.fixture
def archive(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="rovimen_afs_archive_"))
    night = root / CAM / D1
    _write(night / "rms" / f"{CAM}_{D1}_170000_000000_captured_stack.jpg", b"stack-d1")
    _write(night / "rms" / f"{CAM}_{D1}_170000_000000_radiants.png", b"radiants")
    _write(night / "stacks" / f"{CAM}_{D1}_color_meteor_stack.webp", b"colour-d1")
    _write(night / "timelapse" / f"{CAM}_{D1}_timelapse.mp4", b"mp4")
    _write(night / "timelapse" / f"{CAM}_{D1}_night_stack.webp", b"webp")
    _write(night / "meteors" / f"{CAM}_{D1}_201500_color.mkv", b"mkv")
    # Three chunks, a 10-minute hole, two more; plus a stray non-chunk entry.
    chunk = lambda t, **f: (f"{CAM}_{D1}_{t}_color.mkv", {"stacked": True, **f})
    _write(night / "state.json", json.dumps({
        "rms_complete": True,
        "morning_done": False,
        "stage_times": {"rms_complete": f"{D1[:4]}-{D1[4:6]}-{D1[6:]}T23:00:00Z", "bad": "x"},
        "chunks": dict([
            chunk("160000", uploaded=True, lock={"lock_type": "detection"}),
            chunk("160020"), chunk("160040"),
            chunk("161100", lock={"lock_type": "detection"}), chunk("161120"),
            (f"{CAM}_{D1}_night_stack.webp", {"uploaded": True}),
        ]),
    }).encode())
    _write(root / CAM / D2 / "rms" / f"{CAM}_{D2}_170000_000000_captured_stack.jpg", b"stack-d2")
    # OLD_CAM: last night had meteors but no colour stack; its newest stack is
    # 40 nights back and survives only as the fdp/ copy (stacks/ pruned).
    _write(root / OLD_CAM / D1 / "meteors" / f"{OLD_CAM}_{D1}_201500_color.mkv", b"mkv")
    _write(root / OLD_CAM / D40 / "meteors" / f"{OLD_CAM}_{D40}_201500_color.mkv", b"mkv")
    _write(root / OLD_CAM / D40 / "fdp" / f"{OLD_CAM}_{D40}_color_meteor_stack.webp", b"colour-d40")

    import cache_store
    import routes.archive as ra
    import routes.media as rm
    import routes.station_ops as so
    for mod in (cache_store, ra, rm, so):
        if hasattr(mod, "ARCHIVE_PATH"):
            monkeypatch.setattr(mod, "ARCHIVE_PATH", root)
    return root


@pytest.fixture
def app(archive, monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_afs_app_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "afs-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(archive)
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    # station_ops binds THUMB_CACHE_DIR at import; point it at this test's dir
    # so a cached image from another module's test can never answer here.
    import routes.station_ops as so
    monkeypatch.setattr(so, "THUMB_CACHE_DIR", tmp / "thumb_cache")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    monkeypatch.setattr(security, "init_limiter", _noop_limiter)

    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [
                    {"code": c, "label": "N", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 0.0, "alt": 45.0}
                    for c in (CAM, OLD_CAM, EMPTY_CAM)
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Test Alpha",
                "show_on_map": True, "status": "active",
            },
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    import rovimen_dashboard as rd
    flask_app = rd.create_app(rd.load_config(cfg_path), cfg_path)
    flask_app.config["TESTING"] = True
    # Index the fake archive the way the background builder would.
    rd._archive_idx.build_full()
    return flask_app


@pytest.fixture
def no_station(monkeypatch):
    """Fail the test on any station contact from the routes under test."""
    contacted: list[str] = []

    def _refuse(*args, **kwargs):
        contacted.append(repr(args[3] if len(args) > 3 else args))
        raise AssertionError(f"station contacted: {args!r}")

    import routes.media as rm
    import routes.station_ops as so
    import routes.station_proxy as sp
    for mod in (rm, so, sp):
        for name in ("station_get_raw", "_session_for_url", "station_url"):
            if hasattr(mod, name):
                monkeypatch.setattr(mod, name, _refuse)
    return contacted


@pytest.fixture
def client(app, no_station):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


# ── timelapses ─────────────────────────────────────────────────────────────


def test_timelapse_listing_comes_from_the_archive(client, no_station):
    r = client.get("/api/timelapses/teststn1")
    assert r.status_code == 200
    body = r.get_json()
    assert body == {CAM: [{
        "date": D1,
        "filename": f"{CAM}_{D1}_timelapse.mp4",
        "night_stack": f"{CAM}_{D1}_night_stack.webp",
        "source": "archive",
    }]}
    assert no_station == []


# ── RMS nights ─────────────────────────────────────────────────────────────


def test_rms_nights_are_the_archived_ones(client, no_station):
    r = client.get(f"/api/videodb/rmsnights/teststn1/{CAM}")
    assert r.status_code == 200
    assert r.get_json() == [D1, D2]
    assert no_station == []


def test_rms_nights_for_an_unarchived_camera_are_empty(client, no_station):
    r = client.get(f"/api/videodb/rmsnights/teststn1/{EMPTY_CAM}")
    assert r.status_code == 200
    assert r.get_json() == []
    assert no_station == []


# ── latest captured stack ──────────────────────────────────────────────────


def test_latest_stack_is_the_newest_archived_captured_stack(client, no_station):
    r = client.get(f"/api/latest_stack/teststn1/{CAM}")
    assert r.status_code == 200
    assert r.data == b"stack-d1"
    assert r.headers["Content-Type"] == "image/jpeg"
    assert no_station == []


def test_latest_stack_is_404_until_one_is_uploaded(client, no_station):
    r = client.get(f"/api/latest_stack/teststn1/{EMPTY_CAM}")
    assert r.status_code == 404
    assert no_station == []


# ── a night's RMS plot list ────────────────────────────────────────────────


def test_plot_list_is_the_archived_rms_plots_plus_colour_stack(client, no_station):
    r = client.get(f"/api/rms/plots/teststn1/{CAM}/{D1}")
    assert r.status_code == 200
    names = [p["filename"] for p in r.get_json()]
    assert names == [
        f"{CAM}_{D1}_170000_000000_captured_stack.jpg",
        f"{CAM}_{D1}_170000_000000_radiants.png",
        "__color_meteor_stack__.webp",
    ]
    assert no_station == []


def test_plot_list_omits_the_colour_stack_when_none_is_archived(client, no_station):
    r = client.get(f"/api/rms/plots/teststn1/{CAM}/{D2}")
    assert r.status_code == 200
    assert [p["filename"] for p in r.get_json()] == [
        f"{CAM}_{D2}_170000_000000_captured_stack.jpg",
    ]
    assert no_station == []


def test_plot_list_finds_a_colour_stack_that_only_survives_in_fdp(client, archive, no_station):
    """stacks/ is pruned after 60 days and only the fdp/ copy remains, which
    the archive index does not list -- the colour stack must still show."""
    old = _ymd(90)
    _write(archive / CAM / old / "rms" / f"{CAM}_{old}_170000_000000_radiants.png")
    _write(archive / CAM / old / "fdp" / f"{CAM}_{old}_color_meteor_stack.webp")
    r = client.get(f"/api/rms/plots/teststn1/{CAM}/{old}")
    assert r.status_code == 200
    assert "__color_meteor_stack__.webp" in [p["filename"] for p in r.get_json()]
    assert no_station == []


def test_plot_list_for_an_unarchived_night_is_empty(client, no_station):
    r = client.get(f"/api/rms/plots/teststn1/{CAM}/{_ymd(0)}")
    assert r.status_code == 200
    assert r.get_json() == []
    assert no_station == []


# ── plot images ────────────────────────────────────────────────────────────


def test_plot_images_are_served_from_the_archive(client, no_station):
    r = client.get(f"/api/rms/plot_image/teststn1/{CAM}/{D1}/{CAM}_{D1}_170000_000000_radiants.png")
    assert r.status_code == 200 and r.data == b"radiants"
    r = client.get(f"/api/rms/plot_image/teststn1/{CAM}/{D1}/__color_meteor_stack__.webp")
    assert r.status_code == 200 and r.data == b"colour-d1"
    assert no_station == []


def test_a_replaced_plot_is_served_fresh_and_revalidated(client, archive, no_station,
                                                        tmp_path, monkeypatch):
    """A reprocess re-uploads the plots under the same names: the next request
    gets the new bytes (not the dashboard's disk cache), and the browser is
    told to revalidate rather than keep the old image for 7 days."""
    import cache_store
    monkeypatch.setattr(cache_store, "CACHE_PATH", tmp_path / "cache")
    name = f"{CAM}_{D1}_170000_000000_radiants.png"
    url = f"/api/rms/plot_image/teststn1/{CAM}/{D1}/{name}"
    r = client.get(url)
    assert r.data == b"radiants"
    assert "max-age=0" in r.headers.get("Cache-Control", "")

    _write(archive / CAM / D1 / "rms" / name, b"radiants-recalibrated")
    cache_store.invalidate_rms_night(CAM, D1, name)
    r = client.get(url)
    assert r.status_code == 200 and r.data == b"radiants-recalibrated"
    assert "max-age=0" in r.headers.get("Cache-Control", "")


def test_a_plot_image_missing_from_the_archive_is_404_not_a_station_call(client, no_station):
    r = client.get(f"/api/rms/plot_image/teststn1/{CAM}/{_ymd(0)}/{CAM}_{_ymd(0)}_000000_000000_radiants.png")
    assert r.status_code == 404
    r = client.get(f"/api/rms/plot_image/teststn1/{CAM}/{D2}/__color_meteor_stack__.webp")
    assert r.status_code == 404
    assert no_station == []


# ── newest colour meteor stack ─────────────────────────────────────────────


def test_colour_stack_latest_is_last_night_when_archived(client, no_station):
    r = client.get(f"/api/rms/color-stack-latest/teststn1/{CAM}")
    assert r.status_code == 200
    body = r.get_json()
    assert body["status"] == "ok"
    assert body["date"] == D1
    assert body["stale"] is False
    assert body["filename"] == "__color_meteor_stack__.webp"
    assert no_station == []


def test_colour_stack_latest_walks_back_to_an_fdp_only_night(client, no_station):
    """Last night had meteors but no stack; the newest one is 40 nights old
    and only its fdp/ copy is left. The card must still find it, and the
    image route must serve it."""
    r = client.get(f"/api/rms/color-stack-latest/teststn1/{OLD_CAM}")
    body = r.get_json()
    assert body["date"] == D40
    assert body["stale"] is True
    img = client.get(f"/api/rms/plot_image/teststn1/{OLD_CAM}/{D40}/__color_meteor_stack__.webp")
    assert img.status_code == 200 and img.data == b"colour-d40"
    assert no_station == []


def test_colour_stack_latest_for_a_camera_with_none_is_no_data(client, no_station):
    body = client.get(f"/api/rms/color-stack-latest/teststn1/{EMPTY_CAM}").get_json()
    assert body["status"] == "no_data"
    assert body["date"] is None
    assert no_station == []


def test_colour_stack_latest_rejects_unknown_stations_and_bad_cameras(client):
    assert client.get(f"/api/rms/color-stack-latest/nosuch/{CAM}").status_code == 404
    assert client.get("/api/rms/color-stack-latest/teststn1/bad-cam").status_code == 400


def test_colour_stack_walk_stats_at_most_its_budget(monkeypatch):
    """Nights outside the index's memory cost a storage-box stat each; a
    camera with a long run of stack-less meteor nights must not turn one card
    into a scan of its whole history."""
    import routes.archive as ra

    class _Idx:
        def nights_with_meteors(self, cam):
            return [_ymd(d) for d in range(10, 40)]

        def night_files_cached(self, cam, date):
            return None

    probed: list[str] = []

    def _fake_timeout(fn, timeout, default):
        probed.append(str(fn.__self__))
        return False

    monkeypatch.setattr(ra, "_with_sshfs_timeout", _fake_timeout)
    res = ra.resolve_latest_color_stack(_Idx(), CAM)
    assert res["status"] == "no_data"
    nights_probed = {Path(p).parent.parent.name for p in probed}
    assert len(nights_probed) == ra._COLOR_STACK_WALK_IO


# ── old-night chunk list: what it costs on the Storage Box ─────────────────
# A cold chunk list took 12-14 s per night on 2026-09-27: a stat per meteors/
# entry for sizes, a second state.json read, a second FTPdetectinfo parse,
# and a background copy of the night's whole stacks/ (~1,900 files).


@pytest.fixture
def cache_dir(tmp_path, monkeypatch):
    import cache_store
    d = tmp_path / "cache"
    monkeypatch.setattr(cache_store, "CACHE_PATH", d)
    return d


def test_old_night_chunk_list_prefetches_only_its_clip_stacks(client, archive, cache_dir,
                                                              monkeypatch, tmp_path):
    import cache_store
    import rovimen_dashboard as rd
    thumbs = tmp_path / "thumbs"
    monkeypatch.setattr(cache_store, "THUMB_CACHE_DIR", thumbs)
    # The prefetch reads rovimen_dashboard's ARCHIVE_PATH, bound at its first
    # import (another test's archive when this one runs later).
    monkeypatch.setattr(rd, "ARCHIVE_PATH", archive)
    night = archive / OLD_CAM / D40
    _write(night / "stacks" / f"{OLD_CAM}_{D40}_201500_stack.webp", b"clip-stack")
    _write(night / "stacks" / f"{OLD_CAM}_{D40}_201520_stack.webp", b"footage-stack")

    r = client.get(f"/api/videodb/chunks/teststn1/{OLD_CAM}/{D40}?locked_only=1")
    assert r.status_code == 200
    [chunk] = r.get_json()
    assert chunk["stack"] == f"{OLD_CAM}_{D40}_201500_stack.webp"
    deadline = time.time() + 5
    while time.time() < deadline and not list(thumbs.rglob("*.webp")):
        time.sleep(0.05)
    time.sleep(0.2)
    assert [p.name for p in thumbs.rglob("*.webp")] == [f"{OLD_CAM}_{D40}_201500_stack.webp"]


def test_clip_sizes_are_stat_once_per_night_and_only_for_clips(archive, cache_dir, monkeypatch):
    import routes.media as rm
    meteors = archive / OLD_CAM / D40 / "meteors"
    clip = f"{OLD_CAM}_{D40}_201500_color.mkv"
    _write(meteors / clip, b"x" * 2048)
    _write(meteors / f"{OLD_CAM}_{D40}_201500_stack.webp")
    real_stat = Path.stat
    mtime = real_stat(meteors).st_mtime_ns
    stats: list[str] = []

    def spy(self, *a, **k):
        if self.parent == meteors:
            stats.append(self.name)
        return real_stat(self, *a, **k)

    monkeypatch.setattr(Path, "stat", spy)
    assert rm._archive_clip_sizes(OLD_CAM, D40, mtime, [clip]) == {clip: 2048}
    assert stats == [clip], "only the clip is stat'ed, not the stack beside it"
    # Another worker (or a later visit) reads the sizes from disk.
    assert rm._archive_clip_sizes(OLD_CAM, D40, mtime, [clip]) == {clip: 2048}
    assert stats == [clip]
    # meteors/ changed since: the sizes are taken again.
    assert rm._archive_clip_sizes(OLD_CAM, D40, mtime + 1, [clip]) == {clip: 2048}
    assert stats == [clip, clip]


def test_chunk_list_reuses_the_detection_rows_of_the_rms_panel(archive, cache_dir, monkeypatch):
    import routes.archive as ra
    import routes.media as rm
    ra._rms_archive_cache.clear()
    parses: list[tuple[str, str]] = []

    def parse(cam, date):
        parses.append((cam, date))
        return [{"time_utc": f"{D40[:4]}-{D40[4:6]}-{D40[6:]}T20:15:05"}]

    monkeypatch.setattr(ra, "archive_rms_detections", parse)
    assert rm._archive_detection_times(OLD_CAM, D40) == [f"{D40[:4]}-{D40[4:6]}-{D40[6:]}T20:15:05"]
    ra._archive_rms_rows(OLD_CAM, D40)
    assert parses == [(OLD_CAM, D40)]


def test_rms_detections_route_parses_a_night_once(client, cache_dir, monkeypatch):
    """The clip player's detail panel (/api/archive/rms-detections) re-parsed
    FTPdetectinfo over SSHFS on every clip played, unguarded; it now reads
    the same cached, bounded rows as the Detections tab."""
    import gmn_data
    import routes.archive as ra

    night = _ymd(45)
    parses: list[tuple[str, str]] = []

    def parse(cam, date):
        parses.append((cam, date))
        return [{"time_utc": f"{night[:4]}-{night[4:6]}-{night[6:]}T20:15:05", "ra": 10.0}]

    def attach(row, cam, time_utc):
        row["trajectory"] = {"id": "x"}
        return row

    monkeypatch.setattr(ra, "archive_rms_detections", parse)
    monkeypatch.setattr(gmn_data, "attach_trajectory", attach)
    url = f"/api/archive/rms-detections/{EMPTY_CAM}/{night}"
    for _ in range(2):
        r = client.get(url)
        assert r.status_code == 200
        (row,) = r.get_json()["detections"]
        assert row["ra"] == 10.0 and row["trajectory"] == {"id": "x"}
    assert parses == [(EMPTY_CAM, night)]
    # The trajectory is attached to a copy, not to the cached rows.
    assert "trajectory" not in ra._archive_rms_rows(EMPTY_CAM, night)[0]


def test_rms_detections_route_is_empty_when_the_box_stalls(client, cache_dir, monkeypatch):
    """The parse runs under the SSHFS guard: a stalled box costs an empty
    panel, not a request thread held for as long as the mount hangs."""
    import routes.archive as ra

    def unguarded(cam, date):
        raise AssertionError("parsed outside the SSHFS guard")

    monkeypatch.setattr(ra, "archive_rms_detections", unguarded)
    monkeypatch.setattr(ra, "_with_sshfs_timeout", lambda fn, timeout, default: default)
    r = client.get(f"/api/archive/rms-detections/{EMPTY_CAM}/{_ymd(46)}")
    assert r.status_code == 200
    assert r.get_json()["detections"] == []


def test_old_night_empty_chunk_list_is_cached(client, archive, cache_dir, monkeypatch):
    import rovimen_dashboard as rd
    night = archive / OLD_CAM / _ymd(41)
    _write(night / "stacks" / f"{OLD_CAM}_{_ymd(41)}_201500_stack.webp")   # no clips
    calls: list[str] = []
    real = rd._archive_idx.night_files

    def counting(cam, date):
        calls.append(date)
        return real(cam, date)

    monkeypatch.setattr(rd._archive_idx, "night_files", counting)
    for _ in range(2):
        r = client.get(f"/api/videodb/chunks/teststn1/{OLD_CAM}/{_ymd(41)}?locked_only=1")
        assert r.status_code == 200 and r.get_json() == []
    assert calls == [_ymd(41)]


# ── Station Admin night timeline ───────────────────────────────────────────


def test_station_night_comes_from_the_pushed_state(client, no_station):
    r = client.get(f"/api/station_night/teststn1/{D1}")
    assert r.status_code == 200
    cams = r.get_json()["cameras"]
    # when the station's copy of state.json last arrived (here: the fixture write)
    assert abs(cams[CAM].pop("state_at") - time.time()) < 600
    # 16:00 is 240 min after noon; chunks are 20 s apart, so 1/3 min each.
    assert cams[CAM] == {
        "spans": [[240.0, 241.0], [251.0, 251.67]],
        "stages": {"rms_complete": 660.0},
        "total": 5, "stacked": 5, "reencoded": 0, "locked": 2, "uploaded": 1,
        "rms_complete": True, "morning_done": False, "timelapse_done": False,
    }
    assert cams[EMPTY_CAM] is None
    assert no_station == []


def test_station_night_rejects_a_bad_date(client):
    assert client.get("/api/station_night/teststn1/2026-10-21").status_code == 400


def test_night_capture_summary_spans_midnight():
    from routes.station_ops import night_capture_summary
    chunks = {f"X1_20261022_{t}_color.mkv": {} for t in ("235940", "000000", "000020")}
    chunks["X1_20261022_013000_color.mkv"] = {}
    # 23:59:40 on the 22nd is 719.67 min after noon of the 22nd; the
    # 00:00 chunks belong to the next calendar day.
    out = night_capture_summary({k.replace("20261022_0", "20261023_0"): v
                                 for k, v in chunks.items()}, "20261022")
    assert out["spans"] == [[719.67, 720.67], [810.0, 810.33]]
    assert out["total"] == 4
    assert night_capture_summary({}, "20261022")["spans"] == []
    # stage times after midnight are later the same night, not earlier
    assert night_capture_summary({}, "20261022", {"morning_done": "2026-10-23T07:30:00Z"})["stages"] == {"morning_done": 1170.0}
