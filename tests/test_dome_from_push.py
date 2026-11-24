"""#1249: the sky dome and the live frame tile come from pushed inputs only.

A cold dome render for a station makes zero requests to port 7779: platepars
come from the calibration push (#1248), the colour frame from the live_frame
push, the BW frame from the live_thumb push. Every station call is stubbed to
fail the test.
"""

from __future__ import annotations

import base64
import io
import os
import sys
from pathlib import Path

import pytest
from flask import Flask
from PIL import Image

import ingest_api
import pushed_calibration
import rovimen_pusher as rp
import station_keys

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import app_env, viewer  # noqa: E402,F401  (fixtures)

PP = {"az_centre": 180.0, "alt_centre": 45.0, "rotation_from_horiz": 0.0,
      "fov_h": 90.0, "fov_v": 50.0, "lat": 52.5, "lon": 13.4,
      "F_scale": 7.0, "X_res": 640, "Y_res": 360}


def _webp(colour=(40, 60, 90), size=(320, 180)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, colour).save(buf, format="WEBP")
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _pushed_dir(tmp_path, monkeypatch):
    import routes.sky_dome as sky_dome
    # Module-level paths bind at import, which may precede the app fixture's env.
    monkeypatch.setattr(sky_dome, "THUMB_CACHE_DIR", tmp_path / "thumbs")
    monkeypatch.setenv("ROVIMEN_PUSHED_STATE_DIR", str(tmp_path / "pushed"))
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def no_station(monkeypatch):
    import route_helpers
    import routes.sky_dome as sky_dome

    def _boom(*a, **k):
        raise AssertionError("the dashboard contacted a station")

    for mod in (sky_dome, route_helpers):
        monkeypatch.setattr(mod, "station_get_raw", _boom, raising=False)
        monkeypatch.setattr(mod, "_session_for_url", _boom, raising=False)


def _fresh_dome_cache(app):
    """Each test gets its own THUMB_CACHE_DIR (see _pushed_dir), so every
    render here is cold."""


# ── dome ────────────────────────────────────────────────────────────────


def test_cold_stack_dome_renders_from_pushed_inputs(app_env, viewer, no_station):  # noqa: F811
    _fresh_dome_cache(app_env)
    pushed_calibration.save("pub_station", platepar={"PUB001": PP})
    pushed_calibration.save_live_frame("pub_station", "PUB001", _webp(), "image/webp",
                                       "2026-11-02T01:00:00Z")
    r = viewer.get("/api/sky_dome/pub_station.png?variant=stack")
    assert r.status_code == 200 and r.mimetype == "image/png"


def test_cold_ff_dome_renders_from_the_pushed_thumb(app_env, viewer, no_station):  # noqa: F811
    _fresh_dome_cache(app_env)
    pushed_calibration.save("pub_station", platepar={"PUB001": PP})
    app_env._station_cache.set_live_thumb("pub_station", "PUB001", _webp((90, 90, 90)),
                                 content_type="image/webp",
                                 ff_timestamp="2026-11-02T01:00:05Z")
    r = viewer.get("/api/sky_dome/pub_station.png?variant=ff_max")
    assert r.status_code == 200
    meta = viewer.get("/api/sky_dome_meta/pub_station?variant=ff_max").get_json()
    assert meta["capture_time"] == "2026-11-02T01:00:05Z"


def test_no_recent_frame_is_a_503_without_station_contact(app_env, viewer, no_station):  # noqa: F811
    _fresh_dome_cache(app_env)
    pushed_calibration.save("pub_station", platepar={"PUB001": PP})
    r = viewer.get("/api/sky_dome/pub_station.png?variant=stack")
    assert r.status_code == 503


def test_a_stale_pushed_frame_is_not_used(app_env, viewer, no_station, monkeypatch):  # noqa: F811
    _fresh_dome_cache(app_env)
    import routes.sky_dome as sky_dome
    pushed_calibration.save("pub_station", platepar={"PUB001": PP})
    pushed_calibration.save_live_frame("pub_station", "PUB001", _webp(), "image/webp", None)
    monkeypatch.setattr(sky_dome, "_LIVE_FRAME_MAX_AGE_S", -1)
    assert viewer.get("/api/sky_dome/pub_station.png?variant=stack").status_code == 503


def test_a_failed_cache_write_keeps_the_previous_dome(app_env, viewer, no_station, monkeypatch):  # noqa: F811
    """The route writes the cached PNG through a temp file and os.replace, as
    the scheduler does. Written in place, a write cut short left a truncated
    PNG that the route then served as fresh for the whole TTL."""
    import routes.sky_dome as sky_dome
    pushed_calibration.save("pub_station", platepar={"PUB001": PP})
    pushed_calibration.save_live_frame("pub_station", "PUB001", _webp(), "image/webp",
                                       "2026-11-02T01:00:00Z")
    assert viewer.get("/api/sky_dome/pub_station.png?variant=stack").status_code == 200
    cache_file = sky_dome.THUMB_CACHE_DIR / "pub_station" / "sky_dome.png"
    previous = cache_file.read_bytes()
    os.utime(cache_file, (0, 0))              # past its TTL: the next GET re-renders

    real_write_bytes = Path.write_bytes

    def write_half_then_fail(self, data):
        if "sky_dome" not in self.name:
            return real_write_bytes(self, data)
        real_write_bytes(self, data[: len(data) // 2])
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(Path, "write_bytes", write_half_then_fail)
    assert viewer.get("/api/sky_dome/pub_station.png?variant=stack").status_code == 200
    monkeypatch.undo()
    assert cache_file.read_bytes() == previous
    assert [p.name for p in cache_file.parent.iterdir() if "sky_dome.png" in p.name] == [
        "sky_dome.png"]


# ── tiles ───────────────────────────────────────────────────────────────


def test_latest_frame_serves_the_pushed_frame(viewer, no_station):  # noqa: F811
    data = _webp()
    pushed_calibration.save_live_frame("pub_station", "PUB001", data, "image/webp",
                                       "2026-11-02T01:00:00Z")
    r = viewer.get("/api/latest_frame/pub_station/PUB001")
    assert r.status_code == 200 and r.data == data
    assert r.headers["X-Frame-Time"] == "2026-11-02T01:00:00Z"
    again = viewer.get("/api/latest_frame/pub_station/PUB001",
                       headers={"If-None-Match": r.headers["ETag"]})
    assert again.status_code == 304


def test_latest_frame_without_a_push_is_no_recent_frame(viewer, no_station):  # noqa: F811
    r = viewer.get("/api/latest_frame/pub_station/PUB001")
    assert r.status_code == 404 and r.get_json()["state"] == "no_recent_frame"


def test_ff_tile_and_chunk_skip_a_station_not_known_online(viewer, no_station):  # noqa: F811
    assert viewer.get("/api/latest_ff_maxpixel/priv_station/PRV001").status_code == 503
    assert viewer.get("/api/latest_chunk/priv_station/PRV001").status_code == 404


# ── ingest + pusher ─────────────────────────────────────────────────────


@pytest.fixture
def ingest(tmp_path):
    keys_path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=lambda *a, **k: None,
        cache_set_vitals=lambda *a, **k: None,
        known_stations=lambda: {"gmn0002"},
        cam_owners=lambda cam: {"gmnro10"} if cam == "RO000E" else {"gmn0002"},
        detections_db_path=tmp_path / "det.db",
        station_state_path=tmp_path / "state.db",
        station_keys_path=keys_path,
        limiter=None,
    )
    return {"client": app.test_client(), "secret": secret}


def _post_frame(env, seq, cam, data, ctype="image/webp"):
    return env["client"].post(
        f"/api/ingest/v1/gmn0002/live_frame/{cam}",
        headers={"X-Station-Key": env["secret"]},
        json={"schema_version": 1, "seq": seq, "sent_at": "2026-11-02T01:00:00Z",
              "payload": {"content_type": ctype, "frame_time": "2026-11-02T00:59:00Z",
                          "data_b64": base64.b64encode(data).decode()}},
    )


def test_ingest_stores_a_live_frame(ingest):
    data = _webp()
    assert _post_frame(ingest, 1, "RO000M", data).status_code == 200
    frame = pushed_calibration.get_live_frame("gmn0002", "RO000M")
    assert frame["data"] == data and frame["frame_time"] == "2026-11-02T00:59:00Z"


def test_ingest_switching_format_leaves_one_file(ingest):
    _post_frame(ingest, 1, "RO000M", _webp())
    _post_frame(ingest, 2, "RO000M", b"\xff\xd8jpeg", ctype="image/jpeg")
    frame = pushed_calibration.get_live_frame("gmn0002", "RO000M")
    assert frame["content_type"] == "image/jpeg"
    files = sorted(p.name for p in (pushed_calibration.root() / "gmn0002" / "live_frame").iterdir())
    assert files == ["RO000M.jpg", "RO000M.json"]


@pytest.mark.parametrize("cam,ctype,status", [
    ("RO000E", "image/webp", 403),   # another station's camera
    ("RO000M", "text/html", 422),
])
def test_ingest_refuses_bad_frames(ingest, cam, ctype, status):
    assert _post_frame(ingest, 1, cam, _webp(), ctype=ctype).status_code == status


def test_the_pusher_resends_a_frame_only_when_it_changes(monkeypatch):
    frames = iter([b"a", b"a", b"b"])
    monkeypatch.setattr(rp, "fetch_live_camera_codes", lambda: ["RO000M"])
    monkeypatch.setattr(rp, "fetch_live_frame",
                        lambda cam: (next(frames), "image/webp", "2026-11-02T01:00:00Z"))
    posts: list[str] = []
    monkeypatch.setattr(rp, "post_json",
                        lambda url, key, body, **k: posts.append(url) or rp.PostResult(True, 200))

    from test_pushed_calibration import _Pub
    pub = _Pub()
    pub._frame_digests = {}
    for _ in range(3):
        assert pub.push_live_frames()
    assert posts == ["http://vps/api/ingest/v1/gmn0002/live_frame/RO000M"] * 2


def test_the_live_frame_cadence_is_scheduled():
    assert rp.resolve_intervals({})["live_frame"] == rp.LIVE_FRAME_INTERVAL


# ── station_api: the downscaled frame the pusher asks for ───────────────


def test_station_latest_frame_downscales_and_stamps(tmp_path):
    import station_api

    src = tmp_path / "RO000M_20261102_010000_stack.webp"
    src.write_bytes(_webp(size=(1920, 1080)))
    with station_api.app.test_request_context():
        resp = station_api._send_frame(src, 960, mimetype="image/webp")
        resp.direct_passthrough = False
        img = Image.open(io.BytesIO(resp.get_data()))
        assert img.size == (960, 540)
        assert resp.headers["X-Frame-Time"].endswith("Z")

        full = station_api._send_frame(src, None, mimetype="image/webp")
        full.direct_passthrough = False
        assert full.get_data() == src.read_bytes()
