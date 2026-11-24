"""Live tiles for the GH662 pair on gmnro01 (Ghirdoveni).

Neither had a tile: GH662S runs RMS (rms-cam5, since 2026-09-25) but rovimen
treats it as colour-only (``rms: false``, no ``rms_data_path``), so the FF
lookup gave up; GH662W runs no RMS at all, so the pusher never asked for it.
The FF lookup now falls back to the RMS .config's ``data_dir``, a colour-only
camera gets a frame from its newest colour chunk, and /api/live_cameras lists
both for the pusher.
"""

from __future__ import annotations

import pytest

import rovimen_pusher as rp
import station_api

CONFIG = {
    "chunk_names_utc": True,
    "stations": {
        "RO000A": {"rms_data_path": "/nonexistent/cam1", "color_capture": True},
        "GH662S": {"rms": False, "color_capture": True},
        "GH662W": {"rms": False, "color_capture": True},
        "RO999X": {"color_capture": False},
    },
}


@pytest.fixture
def station(tmp_path, monkeypatch, station_patch):
    for n, code in ((1, "RO000A"), (5, "GH662S")):
        d = tmp_path / f"RMS_cam{n}"
        d.mkdir()
        (d / ".config").write_text(
            f"stationID: {code}\n"
            f"device: rtsp://root:x@192.168.0.9{n}:554/stream0\n"
            f"data_dir: {tmp_path}/RMS_data/cam{n}\n")
    station_patch("BASE", tmp_path)
    station_patch("load_config", lambda: CONFIG)
    station_patch("capture_base", lambda: tmp_path / "color_capture")
    station_api._cam_sessions_newest_first.cache_clear()
    station_api._newest_ff_in_session.cache_clear()
    return tmp_path


class TestFfLookup:
    def test_an_rms_camera_without_rms_data_path_is_found_via_its_rms_config(self, station):
        session = station / "RMS_data" / "cam5" / "CapturedFiles" / "GH662S_20260925_201904_060205"
        session.mkdir(parents=True)
        for hms in ("201910", "202000"):
            (session / f"FF_GH662S_20260925_{hms}_123_0000512.fits").write_bytes(b"x")
        ff = station_api._find_latest_ff("GH662S")
        assert ff is not None and ff.name.startswith("FF_GH662S_20260925_202000")

    def test_a_camera_without_rms_has_no_ff(self, station):
        assert station_api._find_latest_ff("GH662W") is None


class TestColourFrameTile:
    def _chunks(self, station, names):
        night = station / "color_capture" / "GH662W" / "20260925"
        night.mkdir(parents=True)
        for n in names:
            (night / n).write_bytes(b"mkv")
        return night

    def test_colour_only_camera_serves_its_newest_chunk_frame(self, station, monkeypatch, station_patch):
        self._chunks(station, ["GH662W_20260925_205519_color.mkv",
                               "GH662W_20260925_205539_color.mkv"])
        seen = []

        def fake(chunk, inode, size_key):
            seen.append(chunk)
            return b"RIFFwebp"
        station_patch("_colour_frame_webp_bytes", fake)
        resp = station_api.app.test_client().get("/api/latest_ff_maxpixel/GH662W")
        assert resp.status_code == 200
        assert resp.data == b"RIFFwebp"
        assert resp.headers["X-Thumb-Kind"] == "colour-frame"
        assert resp.headers["X-FF-Timestamp"] == "2026-09-25T20:55:39.000Z"
        assert seen[0].endswith("GH662W_20260925_205539_color.mkv")

    def test_a_chunk_that_does_not_decode_falls_back_to_the_previous(self, station, monkeypatch, station_patch):
        self._chunks(station, ["GH662W_20260925_205519_color.mkv",
                               "GH662W_20260925_205539_color.mkv"])

        def fake(chunk, inode, size_key):
            if chunk.endswith("205539_color.mkv"):
                raise station_api._FFRenderFailed(chunk)
            return b"RIFFold"
        station_patch("_colour_frame_webp_bytes", fake)
        resp = station_api.app.test_client().get("/api/latest_ff_maxpixel/GH662W")
        assert resp.status_code == 200 and resp.data == b"RIFFold"
        assert resp.headers["X-FF-Timestamp"] == "2026-09-25T20:55:19.000Z"

    def test_no_chunks_is_still_404(self, station):
        resp = station_api.app.test_client().get("/api/latest_ff_maxpixel/GH662W")
        assert resp.status_code == 404

    def test_a_camera_with_colour_capture_off_gets_no_fallback(self, station):
        resp = station_api.app.test_client().get("/api/latest_ff_maxpixel/RO999X")
        assert resp.status_code == 404


class TestCameraLists:
    def test_live_cameras_adds_colour_only_cameras_once(self, station):
        data = station_api.app.test_client().get("/api/live_cameras").get_json()
        codes = [e["code"] for e in data]
        assert codes == ["RO000A", "GH662S", "GH662W"]

    def test_rms_cameras_is_unchanged(self, station):
        data = station_api.app.test_client().get("/api/rms_cameras").get_json()
        assert data == [{"code": "RO000A", "cam_ip": "192.168.0.91"},
                        {"code": "GH662S", "cam_ip": "192.168.0.95"}]


class TestPusherCameraList:
    def test_live_cameras_are_read_in_process(self, station):
        assert rp.fetch_live_camera_codes() == ["RO000A", "GH662S", "GH662W"]

    def test_live_cameras_drive_the_thumbnail_loop_over_http(self, monkeypatch):
        monkeypatch.setattr(rp, "_collect", None)
        monkeypatch.setattr(rp, "_get_json", lambda url: [{"code": "RO000A"}, {"code": "GH662W"}]
                            if url.endswith("/api/live_cameras") else None)
        assert rp.fetch_live_camera_codes() == ["RO000A", "GH662W"]

    def test_an_older_station_api_falls_back_to_the_rms_list(self, monkeypatch):
        def fake(url):
            return [{"code": "RO000A", "cam_ip": ""}] if url.endswith("/api/rms_cameras") else None
        monkeypatch.setattr(rp, "_collect", None)
        monkeypatch.setattr(rp, "_get_json", fake)
        assert rp.fetch_live_camera_codes() == ["RO000A"]

    def test_push_live_thumbs_pushes_the_colour_only_camera(self, tmp_path, monkeypatch):
        seq = rp.SeqCounter(state_path=tmp_path / "state.json")
        spool = rp.Spool(path=tmp_path / "spool.db")
        pub = rp.Publisher("https://vps.example", "gmnro01", "k", seq, spool)
        monkeypatch.setattr(rp, "fetch_live_camera_codes", lambda: ["RO000A", "GH662W"])
        monkeypatch.setattr(rp, "fetch_live_thumb", lambda cam: (b"img", None))
        urls = []
        monkeypatch.setattr(rp, "post_json",
                            lambda url, key, body: urls.append(url) or rp.PostResult(ok=True, status=200))
        assert pub.push_live_thumbs() is True
        assert urls[-1].endswith("/live_thumb/GH662W")
        spool.close()
