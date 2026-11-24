"""POST /api/process-clip refuses a clip that is already processed (routes/media.py).

The modal's Process clip button used to be offered, and accepted, for a clip
the archive already held: on 2026-09-25 RO000A_20260924_190538_color.mkv
(gmnro01 Ghirdoveni) sat in ``meteors/`` with ``reencoded: true`` on the
station and the button still fired. The proxy now answers 409 for a clip in
the archive unless ``?force=1``, before the station is ever contacted.

Harness as in test_shortclip_archive_fallback.py: a real ``create_app`` with
the one configured station unreachable, the station session faked.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest
import yaml

CAM = "RO0017"
DATE = "20260924"
FILENAME = f"{CAM}_{DATE}_190538_color.mkv"
URL = f"/api/process-clip/teststation/{CAM}/{DATE}/{FILENAME}"
# What a browser sends on a same-origin fetch; the CSRF guard rejects headerless POSTs.
SAME_ORIGIN = {"Sec-Fetch-Site": "same-origin"}


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_process_clip_"))
    users_path = tmp / "users.yaml"
    users_path.write_text(yaml.safe_dump({
        "admin": {"display_name": "Admin", "password_hash": "",
                  "role": "admin", "require_totp": False},
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
        "ROVIMEN_SECRET_KEY": "process-clip-test-secret",
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


@pytest.fixture
def admin(app_env):
    c = app_env.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "admin"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


class _StationResp:
    status_code = 200

    def json(self):
        return {"ok": True, "locked": True, "lock_type": "detection"}


@pytest.fixture
def station(tmp_path, monkeypatch):
    """Archive root in tmp + a fake station session recording its POSTs."""
    import routes.media as media_routes

    archive = tmp_path / "archive"
    archive.mkdir()
    monkeypatch.setattr(media_routes, "ARCHIVE_PATH", archive)
    posts: list[str] = []

    class _Session:
        def post(self, url, **kw):
            posts.append(url)
            return _StationResp()

    monkeypatch.setattr(media_routes, "_session_for_url", lambda url: _Session())
    return archive, posts


def _archive_clip(archive: Path) -> None:
    p = archive / CAM / DATE / "meteors" / FILENAME
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"processed")


def test_a_clip_already_in_the_archive_is_refused(admin, station):
    archive, posts = station
    _archive_clip(archive)
    r = admin.post(URL, headers=SAME_ORIGIN)
    assert r.status_code == 409
    assert r.get_json()["processed"] is True
    assert posts == []          # the station is never asked to redo it


def test_a_clip_not_yet_delivered_is_forwarded(admin, station):
    _archive, posts = station
    r = admin.post(URL, headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert len(posts) == 1
    assert posts[0].endswith(f"/api/process_clip/{CAM}/{DATE}/{FILENAME}")


def test_force_re_delivers_and_tells_the_station(admin, station):
    archive, posts = station
    _archive_clip(archive)
    r = admin.post(URL + "?force=1", headers=SAME_ORIGIN)
    assert r.status_code == 200
    assert len(posts) == 1
    assert posts[0].endswith("?force=1")
