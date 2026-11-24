"""Archive fallback for /shortclip and /shortclip_stitch (routes/media.py).

The modal Download button hits /shortclip, which proxies to the station so
the clip can be trimmed from the raw chunk. Stations prune color chunks
after ``color_days`` (often 2 days) while playback keeps working from the
storagebox archive — so a station miss must fall back through the same
archive tiers as /api/cached-video (hot cache, archived .mp4 sibling,
remuxed archived .mkv) instead of 404ing while the video plays fine.

Same harness as test_cached_video_archive_first.py: a real ``create_app``
with the single configured station unreachable (127.0.0.1 refused), so any
success proves the bytes came from the archive tiers.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_shortclip_"))

    users_path = tmp / "users.yaml"
    users_path.write_text(yaml.safe_dump({
        "admin": {
            "display_name": "Admin",
            "password_hash": "",
            "role": "admin",
            "require_totp": False,
        },
    }))

    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststation": {
                "ip": "127.0.0.1",
                "label": "Test Station",
                "ssh_user": "test",
                "public": True,
                "lat": 44.4,
                "lon": 26.1,
                "cameras": [
                    {"code": "RO0017", "cam_ip": "127.0.0.1",
                     "label": "N", "rotate": False},
                ],
            },
        },
    }))

    os.environ["ROVIMEN_USERS_PATH"] = str(users_path)
    os.environ["ROVIMEN_SECRET_KEY"] = "shortclip-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_SHOWER_COUNTS_PATH"] = str(tmp / "shower_counts.json")

    import security

    _saved_threshold = security._LOGIN_THRESHOLD
    _saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    _saved_init_limiter = security.init_limiter
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = _saved_init_limiter(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter

    from rovimen_dashboard import load_config, create_app

    config = load_config(config_path)
    app = create_app(config, config_path)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        security._LOGIN_THRESHOLD = _saved_threshold
        security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
        security.init_limiter = _saved_init_limiter


@pytest.fixture
def admin(app_env):
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


@pytest.fixture
def media_roots(tmp_path, monkeypatch):
    """Point the archive and hot clip cache at fresh tmp dirs."""
    import clip_cache
    import routes.archive as archive_routes

    archive_root = tmp_path / "archive"
    hot_root = tmp_path / "hot"
    archive_root.mkdir()
    hot_root.mkdir()
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", archive_root)
    monkeypatch.setattr(clip_cache, "CLIP_CACHE_DIR", hot_root)
    return archive_root, hot_root


CAM = "RO0017"
DATE = "20260709"
STEM = f"{CAM}_{DATE}_214233_1_color"
URL = f"/shortclip/teststation/{CAM}/{DATE}/{STEM}.mkv"


def _mk(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


class _FakeResp:
    """Minimal streaming-proxy-compatible station response."""

    def __init__(self, status_code: int, body: bytes = b"",
                 content_type: str = "video/mp4"):
        self.status_code = status_code
        self._body = body
        self.headers = {"Content-Type": content_type,
                        "Content-Length": str(len(body))}

    def iter_content(self, chunk_size=None):
        yield self._body

    def close(self):
        pass


# ── Archive first ─────────────────────────────────────────────────────────


def _spy_trim(monkeypatch, body=b"archive-trimmed-bytes", fail=False):
    """Replace the ffmpeg cut with a recorder (test clips are not real video)."""
    import routes.media as media_routes
    from flask import Response

    calls: list[tuple] = []

    def fake_trim(src, ss, t, filename):
        calls.append((Path(src).name, round(ss, 2), round(t, 2)))
        if fail:
            return None
        resp = Response(body, mimetype="video/mp4")
        resp.headers["X-Clip-Trimmed"] = "1"
        return resp

    monkeypatch.setattr(media_routes, "trim_clip_response", fake_trim)
    return calls


class _SpySession:
    def __init__(self, resp=None, exc=None):
        self.calls = 0
        self._resp, self._exc = resp, exc

    def get(self, url, **kw):
        self.calls += 1
        if self._exc is not None:
            raise self._exc
        return self._resp


def test_archived_clip_is_trimmed_from_the_archive_copy(admin, media_roots, monkeypatch):
    """The archived clip is the same overlaid chunk the station would cut
    from, so an archived clip's ss/t download is cut locally and the station
    (whose overlay render outlasted nginx's 30 s) is never asked."""
    import routes.media as media_routes

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    station = _SpySession(resp=_FakeResp(200, b"station-trimmed-bytes"))
    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: station)
    calls = _spy_trim(monkeypatch)
    r = admin.get(f"{URL}?ss=1.00&t=2.00")
    assert r.status_code == 200
    assert r.data == b"archive-trimmed-bytes"
    assert r.headers.get("X-Clip-Trimmed") == "1"
    assert r.headers.get("X-Clip-Source") == "archive"
    assert calls == [(f"{STEM}.mkv", 1.0, 2.0)]
    assert station.calls == 0


def test_hot_copy_is_the_fastest_source_to_cut_from(admin, media_roots, monkeypatch):
    archive_root, hot_root = media_roots
    _mk(hot_root, f"{CAM}/{DATE}/{STEM}.mp4", b"hot-bytes")
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    calls = _spy_trim(monkeypatch)
    r = admin.get(f"{URL}?ss=3.00&t=4.00")
    assert r.status_code == 200
    assert calls == [(f"{STEM}.mp4", 3.0, 4.0)]


def test_detection_window_from_archived_state_json(admin, media_roots, monkeypatch):
    """Without ss/t the window is pre/post around the lock's meteor_time."""
    import rovimen_dashboard
    from cache_store import _NightIndex

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")
    ni = _NightIndex(
        meteor_files=(f"{STEM}.mkv",), stack_files=frozenset(), timelapse_files=(),
        state_chunks={f"{STEM}.mkv": {"lock": {"meteor_time": "2026-07-09T21:42:45.5"}}},
    )
    monkeypatch.setattr(rovimen_dashboard._archive_idx, "night_files",
                        lambda cam, date: ni)
    calls = _spy_trim(monkeypatch)
    r = admin.get(URL)
    assert r.status_code == 200
    # Chunk starts 21:42:33, meteor at 21:42:45.5 -> 12.5 s in; 2 s before, 5 after.
    assert calls == [(f"{STEM}.mkv", 10.5, 7.0)]


def test_no_window_serves_the_whole_clip_flagged_untrimmed(admin, media_roots, monkeypatch):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    calls = _spy_trim(monkeypatch)
    r = admin.get(URL)
    assert r.status_code == 200
    assert r.data == b"sibling-bytes"
    assert calls == []
    assert r.headers.get("X-Clip-Trimmed") == "0"


def test_failed_cut_falls_back_to_the_whole_clip_flagged(admin, media_roots, monkeypatch):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    _spy_trim(monkeypatch, fail=True)
    r = admin.get(f"{URL}?ss=1.00&t=2.00")
    assert r.status_code == 200
    assert r.data == b"sibling-bytes"
    assert r.headers.get("X-Clip-Trimmed") == "0"
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


# ── Station only when nothing is archived ─────────────────────────────────


def test_unarchived_clip_is_cut_by_the_station(admin, media_roots, monkeypatch):
    import routes.media as media_routes

    station = _SpySession(resp=_FakeResp(200, b"station-trimmed-bytes"))
    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: station)
    r = admin.get(f"{URL}?ss=1.00&t=2.00")
    assert r.status_code == 200
    assert r.data == b"station-trimmed-bytes"
    assert station.calls == 1


def test_slow_station_answers_retry_later_before_nginx_gives_up(admin, media_roots, monkeypatch):
    import requests
    import routes.media as media_routes

    station = _SpySession(exc=requests.exceptions.ReadTimeout("overlay render"))
    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: station)
    r = admin.get(f"{URL}?ss=1.00&t=2.00")
    assert r.status_code == 503
    assert r.headers.get("Retry-After") == "60"
    assert r.get_json()["error"] == "shortclip_preparing"


# ── Archive tiers (station at 127.0.0.1 refuses — genuinely down) ─────────


def test_station_down_hot_cache_hit(admin, media_roots):
    _, hot_root = media_roots
    _mk(hot_root, f"{CAM}/{DATE}/{STEM}.mp4", b"hot-bytes")
    r = admin.get(URL)
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"hot-bytes"
    # Archive-served payload is the full untrimmed chunk: tag it so callers can
    # tell it was not cut to the requested ss/t window.
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


def test_station_down_archive_mp4_sibling(admin, media_roots):
    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")
    r = admin.get(URL)
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"sibling-bytes"
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


def test_station_down_archive_mkv_remuxed(admin, media_roots, monkeypatch):
    """Only the archived .mkv exists: it is remuxed (stub) and served."""
    import routes.archive as archive_routes

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mkv", b"matroska-bytes")

    def fake_remux(source: Path, tmp_path: Path) -> None:
        tmp_path.write_bytes(source.read_bytes())

    monkeypatch.setattr(archive_routes, "_remux_to_mp4", fake_remux)
    r = admin.get(URL)
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"matroska-bytes"
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


def test_station_404_falls_back_to_archive(admin, media_roots, monkeypatch):
    """A station that answers 404 (chunk pruned) is not the end of the
    road — the archived copy is served instead of the old stale error."""
    import routes.media as media_routes

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{STEM}.mp4", b"sibling-bytes")

    class _Gone:
        def get(self, url, **kw):
            return _FakeResp(404, b"<html>not found</html>", "text/html")

    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: _Gone())
    r = admin.get(URL)
    assert r.status_code == 200
    assert r.data == b"sibling-bytes"
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


def test_station_down_and_no_archive_404s_accurately(admin, media_roots):
    r = admin.get(URL)
    assert r.status_code == 404
    body = r.get_json()
    assert body["error"] == "shortclip_unavailable"
    assert "archive" in body["detail"]
    # The old message claimed the archive could never serve shortclips.
    assert "can't be reconstructed" not in body["detail"]


# ── Stitched (boundary-splice) fallback ───────────────────────────────────

F1 = f"{CAM}_{DATE}_214940_5_color.mkv"
F2 = f"{CAM}_{DATE}_215000_6_color.mkv"
SPLICE_STEM = f"{CAM}_{DATE}_214950_215010_color"
STITCH_URL = (
    f"/shortclip_stitch/teststation/{CAM}/{DATE}"
    f"?f1={F1}&f2={F2}&ss=8.00&t=3.00"
)


def _fake_night_index(*meteor_files: str):
    from cache_store import _NightIndex
    return _NightIndex(
        meteor_files=tuple(meteor_files),
        stack_files=frozenset(),
        timelapse_files=(),
    )


def test_stitch_station_down_serves_archived_splice(
    admin, media_roots, monkeypatch
):
    """The raw chunks are gone but detection_lock uploaded the boundary
    splice <cam>_<date>_<Tstart>_<Tend>_color — resolve it from the archive
    index by the f2 boundary time and serve it."""
    import rovimen_dashboard

    archive_root, _ = media_roots
    _mk(archive_root, f"{CAM}/{DATE}/meteors/{SPLICE_STEM}.mp4",
        b"splice-bytes")
    monkeypatch.setattr(
        rovimen_dashboard._archive_idx, "night_files",
        lambda cam, date: _fake_night_index(
            f"{SPLICE_STEM}.mkv", f"{SPLICE_STEM}.mp4"),
    )
    r = admin.get(STITCH_URL)
    assert r.status_code == 200
    assert r.mimetype == "video/mp4"
    assert r.data == b"splice-bytes"
    assert r.headers.get("X-Clip-Source") == "archive-full-chunk"


def test_stitch_station_down_no_splice_404s(admin, media_roots, monkeypatch):
    import rovimen_dashboard

    monkeypatch.setattr(
        rovimen_dashboard._archive_idx, "night_files",
        lambda cam, date: _fake_night_index(F1, F2),  # raw names, no splice
    )
    r = admin.get(STITCH_URL)
    assert r.status_code == 404
    body = r.get_json()
    assert body["error"] == "shortclip_stitch_unavailable"


def test_stitch_station_down_missing_f2_404s(admin, media_roots, monkeypatch):
    import rovimen_dashboard

    monkeypatch.setattr(
        rovimen_dashboard._archive_idx, "night_files",
        lambda cam, date: _fake_night_index(f"{SPLICE_STEM}.mkv"),
    )
    r = admin.get(f"/shortclip_stitch/teststation/{CAM}/{DATE}?f1={F1}")
    assert r.status_code == 404
