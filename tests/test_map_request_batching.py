"""Cheaper return visits to the overview map.

- ``/api/rms/plots-latest?cams=`` answers every camera's newest archived plots
  in one call; the map used to ask once per camera (about 40 requests a visit).
- The pushed live thumbnail behind ``/api/latest_ff_maxpixel`` carries an ETag,
  so the 15 s pollers get a bodiless 304 while the image is unchanged (all day,
  when capture is off) instead of the same bytes again.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
CAM = "RO000W"


@pytest.fixture
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_mapbatch_"))
    archive = tmp / "archive"
    (archive / CAM / "20260920" / "rms").mkdir(parents=True)
    (archive / CAM / "20260920" / "rms" / f"{CAM}_20260920_radiants.png").write_bytes(b"png")
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "mapbatch-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(archive)
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")

    import cache_store
    import routes.archive as ra
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", archive)
    monkeypatch.setattr(ra, "ARCHIVE_PATH", archive)

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    monkeypatch.setattr(security, "init_limiter", _noop_limiter)

    station = {
        "cameras": [{"code": CAM, "label": "N", "cam_ip": "127.0.0.1", "rotate": False}],
        "ip": "127.0.0.1", "jump_hosts": [], "label": "Test", "lat": 45.0, "lon": 25.0,
        "proxy_media": False, "public_tabs": [], "ssh_user": "test",
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump({
        "correlation_window_s": 1, "station_api_port": 7779,
        "stations": {"teststn1": {**station, "public": True},
                     "private1": {**station, "public": False}},
    }))
    from rovimen_dashboard import load_config, create_app
    flask_app = create_app(load_config(cfg_path), cfg_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin_test"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


def test_batch_matches_the_per_camera_route(client):
    single = client.get(f"/api/rms/plots-latest/teststn1/{CAM}").get_json()
    batch = client.get(f"/api/rms/plots-latest?cams=teststn1/{CAM}")
    assert batch.status_code == 200
    assert batch.get_json() == {f"teststn1/{CAM}": single}
    assert "max-age=300" in batch.headers["Cache-Control"]


def test_batch_leaves_out_bad_pairs_instead_of_failing(client):
    r = client.get(
        f"/api/rms/plots-latest?cams=teststn1/{CAM},nostation/{CAM},"
        f"teststn1/bad..cam,teststn1,teststn1/a/b,,teststn1/{CAM}"
    )
    assert r.status_code == 200
    assert list(r.get_json()) == [f"teststn1/{CAM}"]


def test_batch_hides_non_public_stations_from_anonymous_callers(app, monkeypatch):
    import routes.archive as ra
    monkeypatch.setattr(ra, "station_is_public_for_request",
                        lambda _cfg, host: host != "private1")
    r = app.test_client().get(f"/api/rms/plots-latest?cams=private1/{CAM}")
    # Either the page gate refuses anonymous callers outright, or the private
    # station is left out of the answer; it is never included.
    assert r.status_code in (200, 302, 401)
    if r.status_code == 200:
        assert f"private1/{CAM}" not in r.get_json()


def test_pushed_live_thumb_revalidates_to_304(client, monkeypatch):
    import station_client
    thumb = {"data": b"RIFF-webp-bytes", "content_type": "image/webp",
             "ff_timestamp": "2026-09-26T10:00:00", "received_at": 0.0}
    monkeypatch.setattr(station_client.StationCache, "get_live_thumb",
                        lambda self, host, cam: thumb)
    url = f"/api/latest_ff_maxpixel/teststn1/{CAM}"
    first = client.get(url)
    assert first.status_code == 200
    assert first.data == thumb["data"]
    assert first.headers["Cache-Control"] == "no-cache"
    etag = first.headers["ETag"]
    again = client.get(url, headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.data == b""
    assert again.headers.get("X-FF-Timestamp") == thumb["ff_timestamp"]
    # A new frame changes the tag, so the browser gets the new image.
    thumb["data"] = b"RIFF-next-frame"
    newer = client.get(url, headers={"If-None-Match": etag})
    assert newer.status_code == 200
    assert newer.data == b"RIFF-next-frame"
