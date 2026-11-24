"""Scheduled compilations are built when their time comes.

Nothing ever fired them: the tick lived at ``/internal/compilation/tick``,
outside ``/api/``, without ``@public_route``, so the session-less cron call
documented for it was redirected to /login before its token check (and a POST
also failed CSRF), and the documented cron pointed at the retired dev port.
The dashboard now runs the tick itself once a minute in one worker
(``run_compilation_ticks``); ``/api/internal/compilation/tick`` stays for
manual use, gated by its token alone.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml
from flask import Flask

import compilation_store

CAM = "RO0017"
DATE = "20261120"
CLIP = f"{CAM}_{DATE}_201500_color.mkv"
TOKEN = "tick-test-token"
TICK = "/api/internal/compilation/tick"

_HAVE_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture
def store(tmp_path, monkeypatch):
    """compilation_store on a tmp file (its path is bound at import)."""
    monkeypatch.setattr(compilation_store, "_PATH", tmp_path / "compilations.json")
    monkeypatch.setattr(compilation_store, "_LOCK_PATH", tmp_path / "compilations.lock")
    return tmp_path


@pytest.fixture
def media(tmp_path, monkeypatch):
    import routes.compilation as comp

    archive = tmp_path / "archive"
    out = tmp_path / "out"
    monkeypatch.setattr(comp, "ARCHIVE_PATH", archive)
    monkeypatch.setattr(comp, "COMPILATIONS_OUT_PATH", out)
    monkeypatch.setattr(comp, "COMPILATIONS_ARCHIVE_PATH", tmp_path / "out_archive")
    monkeypatch.setattr(comp, "INTRO_PATH", tmp_path / "no_intro.mp4")
    return archive, out


def _manifest(mid: str, scheduled_for: str | None, status: str) -> dict:
    return {
        "id": mid, "title": mid, "created_at": "2026-11-20T10:00:00+00:00",
        "scheduled_for": scheduled_for, "status": status,
        "clips": [{"host_key": "teststation", "cam": CAM, "date": DATE, "filename": CLIP,
                   "detection_offset_s": 1.0, "pre": 0.5, "post": 0.5, "label": ""}],
        "youtube": {"upload_after_build": False}, "intro_enabled": False,
    }


def _iso(delta: timedelta) -> str:
    # The cart sends Date.toISOString(): UTC with a Z.
    return (datetime.now(timezone.utc) + delta).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _wait_status(mid: str, timeout: float = 120.0) -> str:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = (compilation_store.get(mid) or {}).get("status")
        if status in ("done", "error"):
            return status
        time.sleep(0.05)
    raise AssertionError(f"{mid} still {status!r}")


# ── The tick itself ──────────────────────────────────────────────────────────


@pytest.mark.timeout(300)
@pytest.mark.skipif(not _HAVE_FFMPEG, reason="needs ffmpeg")
def test_tick_builds_a_manifest_whose_time_has_passed(store, media):
    from routes.compilation import register_compilation_routes

    archive, out = media
    clip = archive / CAM / DATE / "meteors" / CLIP
    clip.parent.mkdir(parents=True)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                    "-i", "testsrc=size=320x240:rate=25:duration=2",
                    "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                    "-y", str(clip)], check=True)
    compilation_store.upsert(_manifest("due", _iso(-timedelta(minutes=2)), "scheduled"))
    compilation_store.upsert(_manifest("later", _iso(timedelta(hours=2)), "scheduled"))
    compilation_store.upsert(_manifest("draft", None, "draft"))

    tick = register_compilation_routes(Flask(__name__), None, None, None)
    assert tick() == ["due"]
    assert _wait_status("due") == "done", compilation_store.get("due").get("error")
    assert (out / "due.mp4").stat().st_size > 1024
    assert compilation_store.get("later")["status"] == "scheduled"
    assert compilation_store.get("draft")["status"] == "draft"
    # Fired once: the next pass finds it no longer scheduled.
    assert tick() == []


def test_the_loop_ticks_only_in_the_leader_and_survives_a_failure():
    from routes.compilation import run_compilation_ticks

    leader = iter([False, True, True, False, True])
    calls: list[int] = []
    sleeps: list[float] = []

    def tick() -> list[str]:
        calls.append(len(sleeps))
        if len(calls) == 1:
            raise OSError("store unreadable")
        return []

    def sleep(s: float) -> None:
        if len(sleeps) == 5:
            raise StopIteration
        sleeps.append(s)

    with pytest.raises(StopIteration):
        run_compilation_ticks(tick, lambda: next(leader), sleep=sleep)
    assert sleeps == [60.0] * 5
    assert calls == [2, 3, 5]      # passes 2, 3 and 5 held the duty


# ── The manual route ─────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_comp_tick_"))
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
        "ROVIMEN_SECRET_KEY": "comp-tick-secret",
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


def test_route_takes_the_token_without_a_session(app_env, store, monkeypatch):
    monkeypatch.setenv("ROVIMEN_TICK_TOKEN", TOKEN)
    compilation_store.upsert(_manifest("later", _iso(timedelta(hours=2)), "scheduled"))
    anon = app_env.test_client()
    r = anon.get(TICK, headers={"X-Rovimen-Tick-Token": TOKEN})
    assert r.status_code == 200, r.data[:200]
    assert r.get_json()["fired"] == []
    # curl -X POST from cron: no Origin, no Sec-Fetch-Site, JSON body.
    r = anon.post(TICK, headers={"X-Rovimen-Tick-Token": TOKEN}, json={})
    assert r.status_code == 200, r.data[:200]
    assert compilation_store.get("later")["status"] == "scheduled"


@pytest.mark.parametrize("headers", [{}, {"X-Rovimen-Tick-Token": "wrong"}])
def test_route_is_404_without_the_token(app_env, store, monkeypatch, headers):
    monkeypatch.setenv("ROVIMEN_TICK_TOKEN", TOKEN)
    compilation_store.upsert(_manifest("due", _iso(-timedelta(minutes=2)), "scheduled"))
    r = app_env.test_client().get(TICK, headers=headers)
    assert r.status_code == 404
    assert compilation_store.get("due")["status"] == "scheduled"


def test_route_is_404_when_no_token_is_configured(app_env, store, monkeypatch):
    monkeypatch.delenv("ROVIMEN_TICK_TOKEN", raising=False)
    r = app_env.test_client().get(TICK, headers={"X-Rovimen-Tick-Token": ""})
    assert r.status_code == 404
