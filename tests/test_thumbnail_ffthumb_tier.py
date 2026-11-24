"""The BW ffthumb tier inside _serve_thumbnail (routes/media.py).

Stations push a tiny BW FF thumbnail (ffthumbs/<chunk_stem>.webp) seconds
after a detection is indexed, so the dashboard can show an image for every
detection at any time — including tonight, before the colour stack uploads.
The tier ordering under test:

  archived colour stack (stacks/, meteors/)  >  BW ffthumb  >  station proxy

and the ffthumb tier must NOT be date-gated (it exists precisely for the
window where the colour bytes have not landed) and must never be promoted
into the NVMe thumb cache (a cached BW image would shadow the colour stack
forever once it arrives).

Same real-create_app pattern as test_cached_video_archive_first.py: the one
configured station is unreachable (127.0.0.1 refused), so a 200 proves the
bytes came from the archive tiers.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml


@pytest.fixture(scope="module")
def app_env():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_ffthumb_"))

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
    os.environ["ROVIMEN_SECRET_KEY"] = "ffthumb-test-secret"
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
def media_env(tmp_path, monkeypatch):
    """Point the thumbnail tiers at fresh tmp dirs.

    The cold thumb archive is disabled (None) so requests fall straight
    through NVMe-miss -> stacks -> ffthumbs -> station, which is the
    ordering under test.
    """
    import routes.media as media_routes
    import routes.archive as archive_routes

    archive_root = tmp_path / "archive"
    cache_root = tmp_path / "thumb_cache"
    archive_root.mkdir()
    cache_root.mkdir()

    monkeypatch.setattr(media_routes, "ARCHIVE_PATH", archive_root)
    monkeypatch.setattr(archive_routes, "ARCHIVE_PATH", archive_root)
    monkeypatch.setattr(
        media_routes, "_thumb_cache_path",
        lambda hk, cam, date, fn: cache_root / hk / cam / date / fn,
    )
    monkeypatch.setattr(media_routes, "_thumb_archive_path",
                        lambda *a: None)
    monkeypatch.setattr(media_routes, "NGINX_ACCEL", False)
    monkeypatch.setattr(media_routes, "_thumb_cache_has_space", lambda: True)
    return archive_root, cache_root


CAM = "RO0017"
DATE = "20260709"  # past night
STEM = f"{CAM}_{DATE}_214233"
FFTHUMB_BYTES = b"RIFF....WEBP-bw-ffthumb"
STACK_BYTES = b"RIFF....WEBP-colour-stack"


def _mk(root: Path, rel: str, data: bytes) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _url(date: str, filename: str) -> str:
    return f"/thumbnail/teststation/{CAM}/{date}/{filename}"


def test_ffthumb_served_when_no_stack(admin, media_env):
    archive_root, _ = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 200
    assert r.data == FFTHUMB_BYTES
    assert r.mimetype == "image/webp"


def test_ffthumb_short_ttl_never_immutable(admin, media_env):
    # The BW thumb is superseded in place by the colour stack, so the
    # browser must re-check within minutes -- 30-day immutable would pin
    # the BW image until the URL changes.
    archive_root, _ = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    cc = r.headers.get("Cache-Control", "")
    assert "max-age=300" in cc
    assert "immutable" not in cc


def test_archived_colour_stack_beats_ffthumb(admin, media_env):
    archive_root, _ = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    _mk(archive_root, f"{CAM}/{DATE}/stacks/{STEM}_stack.webp", STACK_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 200
    # _downsample_thumbnail_bytes passes non-decodable bytes through
    # unchanged, so an exact match proves the stack tier answered.
    assert r.data == STACK_BYTES


def test_ffthumb_not_date_gated(admin, media_env):
    # Tonight's date: the station tier would normally be the only source,
    # but the unreachable station must never be needed when the ffthumb
    # has already been pushed. Patching the session getter to raise proves
    # the ffthumb tier answers BEFORE the station proxy is attempted.
    import routes.media as media_routes
    archive_root, _ = media_env
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    stem = f"{CAM}_{today}_214233"
    _mk(archive_root, f"{CAM}/{today}/ffthumbs/{stem}.webp", FFTHUMB_BYTES)

    def _boom(url):
        raise AssertionError("station proxy contacted before ffthumb tier")

    orig = media_routes._session_for_url
    media_routes._session_for_url = _boom
    try:
        r = admin.get(_url(today, f"{stem}_stack.webp"))
    finally:
        media_routes._session_for_url = orig
    assert r.status_code == 200
    assert r.data == FFTHUMB_BYTES


def test_bare_webp_filename_also_resolves(admin, media_env):
    # Pending rows emit the ffthumb's own name (<stem>.webp, no _stack
    # infix); the tier must map that too.
    archive_root, _ = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(_url(DATE, f"{STEM}.webp"))
    assert r.status_code == 200
    assert r.data == FFTHUMB_BYTES


def test_ffthumb_never_promoted_to_nvme(admin, media_env):
    # A promoted BW thumb would win tier 1 forever and shadow the colour
    # stack when it lands minutes later.
    archive_root, cache_root = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 200
    cached = list(cache_root.rglob("*.webp"))
    assert cached == []


def test_404_when_nothing_landed(admin, media_env):
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 404


def test_archive_file_route_serves_ffthumbs_subdir(admin, media_env):
    # The pending-marker payload points the frontend at
    # /api/archive/file/<cam>/<date>/ffthumbs/<stem>.webp -- the subdir
    # must be in the route allowlist.
    archive_root, _ = media_env
    _mk(archive_root, f"{CAM}/{DATE}/ffthumbs/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(f"/api/archive/file/{CAM}/{DATE}/ffthumbs/{STEM}.webp")
    assert r.status_code == 200
    assert r.data == FFTHUMB_BYTES


# ── Live nights follow the night folder, not the calendar day ──────────────
# Night folders are named for the evening they start, so after 00:00 UTC the
# night in progress has yesterday's date. The thumbnail cache TTL keys on
# this; the station tier it used to gate is gone (#1250).


def test_current_night_rolls_over_at_noon_utc():
    import routes.media as media_routes
    at = lambda h: datetime(2026, 9, 20, h, 0, tzinfo=timezone.utc)
    assert media_routes._current_night(at(3)) == "20260919"
    assert media_routes._current_night(at(11)) == "20260919"
    assert media_routes._current_night(at(13)) == "20260920"
    assert media_routes._live_night_floor(at(3)) == "20260918"
    assert media_routes._live_night_floor(at(13)) == "20260919"


def test_no_station_tier_for_the_night_in_progress(admin, media_env, monkeypatch):
    """A thumb of the night in progress that no archive tier holds is "not
    uploaded yet"; the station is not contacted, not even for tonight
    (#1250). The background prefetch warms tier 1 from reachable stations."""
    import routes.media as media_routes
    night = media_routes._current_night()
    stem = f"{CAM}_{night}_231500"

    def _boom(url):
        raise AssertionError("station contacted for a thumbnail")

    monkeypatch.setattr(media_routes, "_session_for_url", _boom)
    r = admin.get(_url(night, f"{stem}_stack.webp"))
    assert r.status_code == 404
    body = r.get_json()
    assert body["error"] == "not_uploaded_yet"
    assert body["state"] == "not_uploaded_yet"
    assert body["overdue"] is False


def test_station_tier_gated_on_the_night_not_the_calendar_date(monkeypatch):
    # 03:00 UTC on 20 Sep: night 20260919 is in progress, so it is live;
    # 20260918 is finished and archive-only.
    import routes.media as media_routes
    now = datetime(2026, 9, 20, 3, 0, tzinfo=timezone.utc)
    assert "20260919" >= media_routes._current_night(now)
    assert "20260918" < media_routes._current_night(now)
    # Its thumbs keep the short TTL through dawn recolouring the next day.
    assert "immutable" not in media_routes._thumb_cache_control(
        "20260918", media_routes._live_night_floor(now))


def test_station_tier_skipped_for_finished_nights(admin, media_env, monkeypatch):
    # Last night's stacks come from the archive once uploaded; before that
    # they are absent rather than pulled live (#880/#1247).
    import routes.media as media_routes
    old = media_routes._live_night_floor()

    def _boom(url):
        raise AssertionError("station contacted for a finished night")

    monkeypatch.setattr(media_routes, "_session_for_url", _boom)
    r = admin.get(_url(old, f"{CAM}_{old}_231500_stack.webp"))
    assert r.status_code == 404


def test_local_ffthumb_served_without_the_archive(admin, media_env, tmp_path, monkeypatch):
    """Thumbnails pushed since the local store landed sit on the VPS disk."""
    import routes.media as media_routes
    local_root = tmp_path / "ffthumbs"
    monkeypatch.setattr(media_routes, "ffthumb_local_path",
                        lambda cam, date, fn: local_root / cam / date / fn)
    _mk(local_root, f"{CAM}/{DATE}/{STEM}.webp", FFTHUMB_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 200
    assert r.data == FFTHUMB_BYTES


def test_archived_colour_stack_beats_local_ffthumb(admin, media_env, tmp_path, monkeypatch):
    import routes.media as media_routes
    archive_root, _ = media_env
    local_root = tmp_path / "ffthumbs"
    monkeypatch.setattr(media_routes, "ffthumb_local_path",
                        lambda cam, date, fn: local_root / cam / date / fn)
    _mk(local_root, f"{CAM}/{DATE}/{STEM}.webp", FFTHUMB_BYTES)
    _mk(archive_root, f"{CAM}/{DATE}/stacks/{STEM}_stack.webp", STACK_BYTES)
    r = admin.get(_url(DATE, f"{STEM}_stack.webp"))
    assert r.status_code == 200
    assert r.data != FFTHUMB_BYTES


def test_prune_ffthumbs_drops_old_nights_only(tmp_path, monkeypatch):
    import cache_store
    monkeypatch.setattr(cache_store, "FFTHUMB_DIR", tmp_path)
    _mk(tmp_path, f"{CAM}/20260701/a.webp", b"x")
    _mk(tmp_path, f"{CAM}/20260709/b.webp", b"x")
    assert cache_store._prune_ffthumbs("20260705") == 1
    assert not (tmp_path / CAM / "20260701").exists()
    assert (tmp_path / CAM / "20260709" / "b.webp").exists()
