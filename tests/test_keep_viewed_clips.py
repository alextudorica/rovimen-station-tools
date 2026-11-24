"""A clip a signed-in user plays is kept like a manual lock.

POST /api/keep (routes/media.py) records the clip in kept_clips.jsonl, which
the Storage Box janitor never prunes, and locks the chunk on its station so it
uploads now. The station keeps an existing lock (station_api.api_lock), so a
detection stays a detection, and lock uploads run one at a time.

Dashboard harness as in test_process_clip_proxy_idempotent.py.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

import kept_clips
import storagebox_janitor as sj

CAM = "RO0017"
DATE = "20261105"
CHUNK = f"{CAM}_{DATE}_190538_color.mkv"
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


def _url(filename: str = CHUNK) -> str:
    return f"/api/keep/teststation/{CAM}/{DATE}/{filename}"


# ── kept_clips ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("name", [
    CHUNK,
    f"{CAM}_{DATE}_190538_color.mp4",
    f"{CAM}_{DATE}_190538_color.details.mkv",
    f"{CAM}_{DATE}_190538_color.details.mp4",
])
def test_every_served_name_maps_to_the_chunk(name):
    assert kept_clips.chunk_name(name) == CHUNK


@pytest.mark.parametrize("name", ["FF_RO0017_x.fits", "x_stack.webp", "a/../b_color.mkv"])
def test_other_names_are_not_clips(name):
    assert kept_clips.chunk_name(name) is None


def test_record_and_load_round_trip_and_dedup(tmp_path, monkeypatch):
    monkeypatch.setattr(kept_clips, "_written", set())
    path = tmp_path / "kept.jsonl"
    kept_clips.record("ro0017", DATE, CHUNK, "operator", path)
    kept_clips.record("RO0017", DATE, CHUNK, "operator", path)
    path.write_text(path.read_text() + "not json\n")
    assert len(path.read_text().splitlines()) == 2
    assert kept_clips.load(path) == {(CAM, DATE, CHUNK)}
    assert kept_clips.load(tmp_path / "absent.jsonl") == set()


def test_janitor_protects_kept_clips(tmp_path, monkeypatch):
    monkeypatch.setattr(kept_clips, "_written", set())
    path = tmp_path / "kept.jsonl"
    kept_clips.record(CAM, DATE, CHUNK, "visitor1", path)
    protected = sj.load_protected(None, None, path)
    assert sj._clip_key(CAM, DATE, CHUNK.replace(".mkv", ".mp4")) in protected


# ── POST /api/keep ───────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_keep_"))
    users_path = tmp / "users.yaml"
    users_path.write_text(yaml.safe_dump({
        "admin": {"display_name": "Admin", "password_hash": "",
                  "role": "admin", "require_totp": False},
        "viewer": {"display_name": "Viewer", "password_hash": "",
                   "role": "visitor", "require_totp": False},
    }))
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump({
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststation": {
                "ip": "127.0.0.1", "label": "Test Station", "ssh_user": "test",
                "public": True, "lat": 44.4, "lon": 26.1,
                "cameras": [{"code": CAM, "cam_ip": "127.0.0.1",
                             "label": "N", "rotate": False}],
            },
        },
    }))
    for key, val in {
        "ROVIMEN_USERS_PATH": users_path,
        "ROVIMEN_SECRET_KEY": "keep-test-secret",
        "ROVIMEN_COOKIE_SECURE": "0",
        "RATELIMIT_ENABLED": "0",
        "THUMB_CACHE_DIR": tmp / "thumb_cache",
        "ROVIMEN_CACHE_PATH": tmp / "cache",
        "ROVIMEN_COMPILATIONS_OUT_PATH": tmp / "compilations",
        "ROVIMEN_KNOWN_HOSTS": tmp / "known_hosts",
        "ROVIMEN_AUDIT_LOG_PATH": tmp / "audit.log",
        "ROVIMEN_ACTIVITY_LOG_PATH": tmp / "activity.log",
        "ROVIMEN_STATION_STATE_DB": tmp / "station_state.db",
        "ROVIMEN_DETECTIONS_DB": tmp / "detections.db",
        "ROVIMEN_SHOWER_COUNTS_PATH": tmp / "shower_counts.json",
    }.items():
        os.environ[key] = str(val)

    import security

    saved = (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS,
             security.init_limiter)
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = saved[2](app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    from rovimen_dashboard import create_app, load_config

    app = create_app(load_config(config_path), config_path)
    app.config["TESTING"] = True
    try:
        yield app
    finally:
        (security._LOGIN_THRESHOLD, security._LOGIN_LOCKOUT_SECONDS,
         security.init_limiter) = saved


def _client(app, user: str, role: str):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = role
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


class _Resp:
    def __init__(self, status: int, body: dict | None = None):
        self.status_code = status
        self._body = body or {}

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


@pytest.fixture
def station(tmp_path, monkeypatch):
    """kept_clips in tmp + a fake station session; set ``reply`` to change
    what the station answers."""
    import routes.media as media_routes

    kept = tmp_path / "kept.jsonl"
    monkeypatch.setattr(kept_clips, "KEPT_CLIPS_PATH", kept)
    monkeypatch.setattr(kept_clips, "_written", set())
    posts: list[tuple[str, dict]] = []
    state = {"reply": _Resp(200, {"locked": True, "lock_type": "manual"})}

    class _Session:
        def post(self, url, **kw):
            posts.append((url, kw.get("json")))
            reply = state["reply"]
            if isinstance(reply, Exception):
                raise reply
            return reply

    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: _Session())
    return kept, posts, state


def test_anonymous_cannot_keep(app_env, station):
    kept, posts, _ = station
    r = app_env.test_client().post(_url(), headers=SAME_ORIGIN)
    assert r.status_code == 401
    assert posts == [] and not kept.exists()


@pytest.mark.parametrize("user,role", [("admin", "admin"), ("viewer", "visitor")])
def test_any_account_keeps_and_locks_the_chunk(app_env, station, user, role):
    kept, posts, _ = station
    r = _client(app_env, user, role).post(
        _url(CHUNK.replace(".mkv", ".mp4")), headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert r.get_json() == {"kept": True, "on_station": True,
                            "locked": True, "lock_type": "manual"}
    assert kept_clips.load(kept) == {(CAM, DATE, CHUNK)}
    assert len(posts) == 1
    url, body = posts[0]
    assert url.endswith(f"/api/lock/{CAM}/{DATE}/{CHUNK}")
    assert body == {"locked": True}


def test_a_chunk_gone_from_the_station_is_still_kept(app_env, station):
    kept, _posts, state = station
    state["reply"] = _Resp(404)
    r = _client(app_env, "admin", "admin").post(_url(), headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert r.get_json() == {"kept": True, "on_station": False, "locked": None}
    assert kept_clips.load(kept) == {(CAM, DATE, CHUNK)}


def test_an_unreachable_station_does_not_lose_the_keep(app_env, station):
    kept, _posts, state = station
    state["reply"] = ConnectionError("down")
    r = _client(app_env, "admin", "admin").post(_url(), headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert r.get_json()["kept"] is True
    assert kept_clips.load(kept) == {(CAM, DATE, CHUNK)}


@pytest.mark.parametrize("filename", ["RO0017_x_stack.webp", "notaclip.mkv"])
def test_non_clip_names_are_refused(app_env, station, filename):
    kept, posts, _ = station
    r = _client(app_env, "admin", "admin").post(_url(filename), headers=SAME_ORIGIN)
    assert r.status_code == 400
    assert posts == [] and not kept.exists()
