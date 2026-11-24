"""Live-tile clicks play the latest clip instead of hanging (2026-09-25).

The overview's Live Cameras tiles pointed a <video> straight at the newest
colour chunk. That chunk is raw at night, and the station's overlay gate
re-encodes it before sending a byte: 7-9 s on VAAPI hosts, 23-56 s on the
gmnde01 (Berlin) Pi 5, past nginx's 30 s read timeout, so Berlin clicks got
504s and nothing played. /api/latest_chunk now reports whether the chunk is
overlaid, starts the render in the background once per camera, and answers
polls for the same chunk without starting more renders.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
from pathlib import Path

import pytest
import yaml

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

import routes.sky_dome as sd  # noqa: E402


def _chunk(t: str, reencoded: bool = False) -> dict:
    return {"filename": f"CAM1_20260925_{t}_color.mkv",
            "time": f"{t[:2]}:{t[2:4]}:{t[4:]}", "reencoded": reencoded}


CHUNKS = [_chunk("190302"), _chunk("190322"), _chunk("190342")]


class TestPickLiveChunk:
    def test_newest_raw_chunk_when_nothing_is_ready(self):
        assert sd._pick_live_chunk(CHUNKS)["filename"].endswith("190342_color.mkv")

    def test_a_recently_rendered_chunk_is_reused(self):
        chunks = CHUNKS[:1] + [_chunk("190322", True)] + CHUNKS[2:]
        assert sd._pick_live_chunk(chunks)["filename"].endswith("190322_color.mkv")

    def test_an_old_rendered_chunk_is_not_live(self):
        chunks = [_chunk("180000", True)] + [_chunk("19%04d" % i) for i in range(20)]
        pick = sd._pick_live_chunk(chunks, reuse=15)
        assert pick["filename"] == chunks[-1]["filename"]

    def test_the_chunk_already_rendering_wins_over_a_newer_one(self):
        pick = sd._pick_live_chunk(CHUNKS, inflight=CHUNKS[0]["filename"])
        assert pick is CHUNKS[0]

    def test_a_poll_returns_exactly_that_chunk_or_nothing(self):
        assert sd._pick_live_chunk(CHUNKS, want=CHUNKS[1]["filename"]) is CHUNKS[1]
        assert sd._pick_live_chunk(CHUNKS, want="CAM1_20260925_000000_color.mkv") is None

    def test_no_chunks(self):
        assert sd._pick_live_chunk([]) is None


class TestLiveChunkWarmer:
    def _warmer(self, **kw):
        calls: list[tuple] = []
        gate = threading.Event()

        def fetch(*args):
            calls.append(args)
            gate.wait(2)
        return sd._LiveChunkWarmer(fetch, **kw), calls, gate

    def test_one_render_per_chunk(self):
        w, calls, gate = self._warmer()
        assert w.start("h", "CAM1", "20260925", "a.mkv") is True
        assert w.start("h", "CAM1", "20260925", "a.mkv") is False
        assert w.inflight("h", "CAM1") == "a.mkv"
        gate.set()

    def test_the_cap_bounds_renders_across_cameras(self):
        w, _, gate = self._warmer(max_inflight=2)
        assert w.start("h", "C1", "d", "1.mkv")
        assert w.start("h", "C2", "d", "2.mkv")
        assert not w.start("h", "C3", "d", "3.mkv")
        gate.set()

    def test_inflight_expires(self):
        now = [0.0]
        w = sd._LiveChunkWarmer(lambda *a: None, ttl_s=10, clock=lambda: now[0])
        w.start("h", "CAM1", "d", "a.mkv")
        assert w.inflight("h", "CAM1") == "a.mkv"
        now[0] = 11.0
        assert w.inflight("h", "CAM1") is None

    def test_a_failed_render_frees_its_slot(self):
        def boom(*a):
            raise RuntimeError("station down")
        w = sd._LiveChunkWarmer(boom, max_inflight=1)
        assert w.start("h", "C1", "d", "1.mkv")
        for _ in range(100):
            if w._running == 0:
                break
            threading.Event().wait(0.01)
        assert w.start("h", "C2", "d", "2.mkv")


@pytest.fixture
def app(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_live_clip_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "live-clip-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(tmp / "archive")
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    monkeypatch.setattr(security, "init_limiter", _noop_limiter)

    cfg = {
        "correlation_window_s": 1, "station_api_port": 7779,
        "stations": {"teststn1": {
            "cameras": [{"code": "CAM1", "label": "N", "cam_ip": "127.0.0.1",
                         "rotate": False, "az": 0.0, "alt": 45.0}],
            "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
            "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
            "public_tabs": [], "ssh_user": "test", "location_name": "Test Alpha",
            "show_on_map": True, "status": "active",
        }},
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(cfg))
    import rovimen_dashboard as rd
    flask_app = rd.create_app(rd.load_config(cfg_path), cfg_path)
    flask_app.config["TESTING"] = True
    return flask_app


@pytest.fixture
def station(monkeypatch):
    """A station whose chunk list the test controls; renders are recorded,
    never performed."""
    state = {"chunks": list(CHUNKS), "listed": []}

    def fake_get_raw(config, tunnels, host_key, path, **kw):
        state["listed"].append(path)
        if not path.endswith("/20260925"):
            raise RuntimeError("no such night")
        return {"chunks": state["chunks"]}
    monkeypatch.setattr(sd, "station_get_raw", fake_get_raw)
    return state


@pytest.fixture
def client(app, station, monkeypatch):
    renders: list[tuple] = []
    # latest_chunk only lists chunks on a station reported online (#1249).
    app._station_cache.set_status("teststn1", {"online": True})
    warmer = app._live_chunk_warmer
    monkeypatch.setattr(warmer, "_fetch", lambda *a: renders.append(a))
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    c.renders = renders
    return c


def _wait_for(pred, timeout=2.0):
    ev = threading.Event()
    for _ in range(int(timeout / 0.01)):
        if pred():
            return True
        ev.wait(0.01)
    return pred()


class TestLatestChunkRoute:
    def test_first_click_starts_one_render_and_says_so(self, client):
        r = client.get("/api/latest_chunk/teststn1/CAM1?date=20260925")
        d = r.get_json()
        assert r.status_code == 200
        assert d["filename"].endswith("190342_color.mkv")
        assert d["overlaid"] is False and d["rendering"] is True
        assert _wait_for(lambda: len(client.renders) == 1)
        assert client.renders[0][3] == d["filename"]

    def test_polls_never_start_renders(self, client):
        fn = CHUNKS[-1]["filename"]
        for _ in range(3):
            d = client.get(f"/api/latest_chunk/teststn1/CAM1?date=20260925&filename={fn}").get_json()
            assert d["filename"] == fn and d["overlaid"] is False
        assert client.renders == []

    def test_a_rendered_chunk_plays_immediately(self, client, station):
        station["chunks"][-1] = dict(station["chunks"][-1], reencoded=True)
        d = client.get("/api/latest_chunk/teststn1/CAM1?date=20260925").get_json()
        assert d["overlaid"] is True and d["rendering"] is False
        assert d["video_url"] == f"/video/teststn1/CAM1/20260925/{d['filename']}"
        assert client.renders == []

    def test_a_reclick_waits_on_the_same_render(self, client, station):
        first = client.get("/api/latest_chunk/teststn1/CAM1?date=20260925").get_json()
        station["chunks"].append(_chunk("190402"))   # a newer chunk lands
        again = client.get("/api/latest_chunk/teststn1/CAM1?date=20260925").get_json()
        assert again["filename"] == first["filename"]
        assert again["rendering"] is True
        assert _wait_for(lambda: len(client.renders) == 1)

    def test_a_pruned_poll_target_is_a_404(self, client):
        r = client.get("/api/latest_chunk/teststn1/CAM1?date=20260925"
                       "&filename=CAM1_20260925_000000_color.mkv")
        assert r.status_code == 404

    @pytest.mark.parametrize("qs", ["filename=../../etc/passwd",
                                    "filename=CAM1_x_color.mkv",
                                    "date=2026-09-25"])
    def test_bad_poll_params_are_rejected(self, client, qs):
        assert client.get(f"/api/latest_chunk/teststn1/CAM1?{qs}").status_code == 400
