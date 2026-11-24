"""Media routes live at module level (routes/media.py).

The 24 handlers and their helpers used to be closures inside
``register_media_routes``, so no helper (the videodb chunk list, the thumbnail
tiers, the archive-or-station clip server) could be imported or unit-tested
without building the app. These tests pin what the move must not change (URL
rules, endpoint names, methods, the auth and CSRF treatment of every view, the
anonymous gate) and the new contract: handlers and helpers are module
functions, ``register_media_routes`` stores the dependencies (the per-app
archive chunk-list cache with them, fresh per registration) and touches the
app only to add URL rules.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from werkzeug.exceptions import NotFound

import auth
import security

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import anon, app_env  # noqa: E402,F401  (fixtures)

import routes.media as media  # noqa: E402

_GET = ["GET", "HEAD", "OPTIONS"]
_POST = ["OPTIONS", "POST"]


def _row(rule: str, endpoint: str, *, methods: list[str] = _GET, public: bool = False,
         require_station: bool = False, require_station_lock: bool = False,
         csrf_exempt: bool = False) -> dict:
    return {"rule": rule, "endpoint": endpoint, "methods": methods, "public": public,
            "page": None, "require_station": require_station,
            "require_station_lock": require_station_lock, "require_auth": False,
            "csrf_exempt": csrf_exempt}


_M = "<host_key>/<station_code>/<date>/<filename>"

# Captured from origin/main 4315cec8, while these views were closures.
EXPECTED_ENDPOINTS = [
    _row("/api/batch-download/<host_key>/<station_code>/<date>", "api_batch_download",
         methods=_POST, require_station=True, csrf_exempt=True),
    _row(f"/api/keep/{_M}", "api_keep", methods=_POST, require_station_lock=True,
         csrf_exempt=True),
    _row(f"/api/lock/{_M}", "api_lock", methods=_POST, require_station_lock=True,
         csrf_exempt=True),
    _row(f"/api/process-clip/{_M}", "api_process_clip", methods=_POST,
         require_station=True, csrf_exempt=True),
    _row("/api/videodb/chunks/<host_key>/<cam_code>/<date>", "api_videodb_chunks",
         require_station=True, csrf_exempt=True),
    _row("/api/videodb/nights/<host_key>/<cam_code>", "api_videodb_nights",
         require_station=True, csrf_exempt=True),
    _row("/api/videodb/rms-detections/<host_key>/<cam_code>/<date>",
         "api_videodb_rms_detections", require_station=True, csrf_exempt=True),
    _row("/api/videodb/rmsnights/<host_key>/<cam_code>", "api_videodb_rmsnights",
         require_station=True, csrf_exempt=True),
    _row(f"/cropped_loop_clip/{_M}", "cropped_loop_clip", require_station=True),
    _row(f"/download/{_M}", "video_download", public=True),
    _row(f"/download_cropped/{_M}", "download_cropped", require_station=True),
    _row(f"/download_cropped_frame/{_M}", "download_cropped_frame", require_station=True),
    _row(f"/fullstack/{_M}", "fullstack", public=True),
    _row(f"/loop_clip/{_M}", "loop_clip", require_station=True),
    _row(f"/night_stack/{_M}", "night_stack", public=True),
    _row(f"/shortclip/{_M}", "shortclip", require_station=True),
    _row("/shortclip_stitch/<host_key>/<station_code>/<date>", "shortclip_stitch",
         require_station=True),
    _row(f"/stack/{_M}", "stack_image", public=True),
    _row("/stitch_video/<host_key>/<station_code>/<date>", "stitch_video",
         require_station=True),
    _row("/stream/<host_key>/<camera_code>", "stream", require_station=True),
    _row(f"/thumbnail/{_M}", "thumbnail", public=True),
    _row(f"/timelapse/{_M}", "timelapse", public=True),
    _row(f"/timelapse_download/{_M}", "timelapse_download", public=True),
    _row(f"/video/{_M}", "video_serve", public=True),
]

_D = "20260101"
_P = f"pub_station/PUB001/{_D}"
_CLIP = "PUB001_20260101_010101_color.mkv"
_STACK = "PUB001_20260101_010101_stack.webp"
_TL = "PUB001_20260101_timelapse.mp4"
# One concrete request per endpoint and what an anonymous caller gets
# (statuses as on origin/main): the public media views reach the media layer,
# whose archive miss is a 404; the rest is login-gated, 401 under /api/ and a
# redirect to the login page elsewhere.
SAMPLE = {
    "api_batch_download": ("POST", f"/api/batch-download/{_P}", 401),
    "api_keep": ("POST", f"/api/keep/{_P}/{_CLIP}", 401),
    "api_lock": ("POST", f"/api/lock/{_P}/{_CLIP}", 401),
    "api_process_clip": ("POST", f"/api/process-clip/{_P}/{_CLIP}", 401),
    "api_videodb_chunks": ("GET", f"/api/videodb/chunks/{_P}", 401),
    "api_videodb_nights": ("GET", "/api/videodb/nights/pub_station/PUB001", 401),
    "api_videodb_rms_detections": ("GET", f"/api/videodb/rms-detections/{_P}", 401),
    "api_videodb_rmsnights": ("GET", "/api/videodb/rmsnights/pub_station/PUB001", 401),
    "cropped_loop_clip": ("GET", f"/cropped_loop_clip/{_P}/{_CLIP}", 302),
    "video_download": ("GET", f"/download/{_P}/{_CLIP}", 404),
    "download_cropped": ("GET", f"/download_cropped/{_P}/{_CLIP}", 302),
    "download_cropped_frame": ("GET", f"/download_cropped_frame/{_P}/{_CLIP}", 302),
    "fullstack": ("GET", f"/fullstack/{_P}/{_STACK}", 404),
    "loop_clip": ("GET", f"/loop_clip/{_P}/{_CLIP}?start=1&end=2", 302),
    "night_stack": ("GET", f"/night_stack/{_P}/PUB001_{_D}_night_stack.webp", 404),
    "shortclip": ("GET", f"/shortclip/{_P}/{_CLIP}", 302),
    "shortclip_stitch": ("GET", f"/shortclip_stitch/{_P}", 302),
    "stack_image": ("GET", f"/stack/{_P}/{_STACK}", 404),
    "stitch_video": ("GET", f"/stitch_video/{_P}", 302),
    "stream": ("GET", "/stream/pub_station/PUB001", 302),
    "thumbnail": ("GET", f"/thumbnail/{_P}/{_STACK}", 404),
    "timelapse": ("GET", f"/timelapse/{_P}/{_TL}", 404),
    "timelapse_download": ("GET", f"/timelapse_download/{_P}/{_TL}", 404),
    "video_serve": ("GET", f"/video/{_P}/{_CLIP}", 404),
}


def _media_endpoints(app: Flask) -> list[dict]:
    station_guard = auth.require_station(lambda **k: None).__code__
    lock_guard = auth.require_station_lock(lambda **k: None).__code__
    auth_guard = auth.require_auth(lambda **k: None).__code__
    csrf = app.extensions["csrf"]
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        view = app.view_functions[rule.endpoint]
        if view.__module__ != "routes.media":
            continue
        out.append({
            "rule": rule.rule,
            "endpoint": rule.endpoint,
            "methods": sorted(rule.methods),
            "public": bool(getattr(view, security._PUBLIC_ROUTE_ATTR, False)),
            "page": getattr(view, security._PUBLIC_PAGE_ATTR, None),
            "require_station": view.__code__ is station_guard,
            "require_station_lock": view.__code__ is lock_guard,
            "require_auth": view.__code__ is auth_guard,
            "csrf_exempt": f"{view.__module__}.{view.__name__}" in csrf._exempt_views,
        })
    return out


def test_endpoints_keep_their_rules_methods_auth_and_csrf(app_env):  # noqa: F811
    assert _media_endpoints(app_env) == EXPECTED_ENDPOINTS


@pytest.mark.parametrize("row", EXPECTED_ENDPOINTS, ids=lambda r: r["endpoint"])
def test_the_auth_gate_sees_the_same_public_views(app_env, anon, row, monkeypatch):  # noqa: F811
    def _no_station(url):
        raise AssertionError("an anonymous gate check must not reach a station")

    monkeypatch.setattr(media, "_session_for_url", _no_station)
    method, url, anon_status = SAMPLE[row["endpoint"]]
    with app_env.test_request_context(url, method=method):
        matched = security._matched_public_view(app_env)
    if row["public"]:
        assert matched is app_env.view_functions[row["endpoint"]]
    else:
        assert matched is None
    body = {} if method == "POST" else None
    assert anon.open(url, method=method, json=body).status_code == anon_status


def test_every_view_is_the_module_function(app_env):  # noqa: F811
    for row in EXPECTED_ENDPOINTS:
        view = app_env.view_functions[row["endpoint"]]
        assert view is getattr(media, row["endpoint"]), row["endpoint"]
        assert "<locals>" not in view.__qualname__


# ── Registration ─────────────────────────────────────────────────────────


class _Store:
    """In-memory stand-in for the cross-worker ``SharedSnapshotStore``."""

    enabled = True

    def __init__(self) -> None:
        self.data: dict[str, tuple[object, float]] = {}

    def get(self, key: str):
        hit = self.data.get(key)
        if hit is None:
            return None
        payload, stored = hit
        return payload, time.monotonic() - stored, 600.0

    def set(self, key: str, payload, ttl_s: float) -> None:
        self.data[key] = (payload, time.monotonic())


class _Index:
    """The archive-index calls the chunk list makes, counting listings."""

    def __init__(self, meteors: tuple[str, ...] = (), *, hot: bool = False) -> None:
        self.listing = SimpleNamespace(
            meteor_files=meteors, stack_files=frozenset(), sig=None, state_chunks={})
        self.hot = hot
        self.scan_unavailable = 0
        self.listed: list[tuple[str, str]] = []

    def night_files(self, cam: str, date: str):
        self.listed.append((cam, date))
        return self.listing

    def in_hot_window(self, date: str) -> bool:
        return self.hot

    def nights(self, cam: str) -> list[str]:
        return []


def _register_bare(monkeypatch, **overrides) -> tuple[Flask, SimpleNamespace]:
    # monkeypatch restores the deps of whichever app the suite built before.
    monkeypatch.setattr(media, "_DEPS", getattr(media, "_DEPS", None), raising=False)
    app = Flask(__name__)
    config = SimpleNamespace(stations={"alpha": SimpleNamespace(cameras=[])})
    kwargs: dict = {
        "archive_idx": _Index(),
        "prefetch_executor": SimpleNamespace(submit=lambda *a, **k: None),
        "prefetch_archive_thumbs": lambda *a, **k: None,
        "live_stream_triple_sem": lambda user, host_key, cam: None,
        "live_stream_global_sem": None,
        "live_stream_retry_after_s": 15,
    }
    kwargs.update(overrides)
    media.register_media_routes(app, config, None, None, **kwargs)
    return app, config


def test_registration_touches_the_app_only_to_add_url_rules(monkeypatch):
    before = Flask(__name__)
    baseline = set(vars(before))
    app, _ = _register_bare(monkeypatch)
    assert set(vars(app)) == baseline
    assert sorted(set(app.view_functions) - {"static"}) == sorted(
        r["endpoint"] for r in EXPECTED_ENDPOINTS)


def test_each_registration_replaces_the_deps(monkeypatch):
    _, config1 = _register_bare(monkeypatch)
    deps1 = media._deps()
    _, config2 = _register_bare(monkeypatch)
    deps2 = media._deps()
    assert deps1.config is config1 and deps2.config is config2
    # The chunk-list cache was per-registration closure state; it still is.
    assert deps1.archive_chunks_cache is not deps2.archive_chunks_cache
    # No shared tier passed: a disabled store keeps the per-worker behaviour.
    assert not deps2.shared_videodb.enabled


def test_deps_before_registration_is_an_error(monkeypatch):
    monkeypatch.setattr(media, "_DEPS", None, raising=False)
    with pytest.raises(RuntimeError):
        media._deps()
    with pytest.raises(RuntimeError):
        media._require_station("alpha")
    with pytest.raises(RuntimeError):
        media._archive_chunks_cached("alpha", "RO000H", "20261121")


def test_station_lookup_and_media_url_use_the_registered_config(monkeypatch):
    _, config = _register_bare(monkeypatch)
    assert media._require_station("alpha") is config.stations["alpha"]
    with pytest.raises(NotFound):
        media._require_station("nope")
    seen: list[tuple] = []
    monkeypatch.setattr(media, "_media_url_base",
                        lambda cfg, tun, hk, *parts: seen.append((cfg, hk, parts)) or "u")
    assert media._media_url("alpha", "color_capture", "RO000H") == "u"
    assert seen == [(config, "alpha", ("color_capture", "RO000H"))]


def test_clip_stem_collapses_both_clip_names():
    assert media._clip_stem("RO000H_20261121_010101_color.mkv") == "RO000H_20261121_010101"
    assert media._clip_stem("RO000H_20261121_010101_color.mp4") == "RO000H_20261121_010101"
    assert media._clip_stem("other.webp") == "other.webp"


def test_chunk_list_is_cached_per_registration_and_shared_across_workers(monkeypatch):
    clip = "RO000H_20261101_010101_color.mkv"
    monkeypatch.setattr(media, "_archive_clip_sizes", lambda *a: {})
    monkeypatch.setattr(media, "_archive_detection_times", lambda *a: [])
    monkeypatch.setattr(media.detection_db, "chunk_meteor_times", lambda *a: {})
    shared = _Store()

    index_a = _Index((clip,))
    _register_bare(monkeypatch, archive_idx=index_a, shared_videodb=shared)
    first = media._archive_chunks_cached("alpha", "RO000H", "20261101")
    assert [c["filename"] for c in first] == [clip]
    assert media._archive_chunks_cached("alpha", "RO000H", "20261101") is first
    assert index_a.listed == [("RO000H", "20261101")], "the second call is an L1 hit"

    # A second worker: its own empty L1, the same shared tier.
    index_b = _Index((clip,))
    _register_bare(monkeypatch, archive_idx=index_b, shared_videodb=shared)
    assert media._archive_chunks_cached("alpha", "RO000H", "20261101") == first
    assert index_b.listed == [], "adopted from the shared tier, not listed again"


def test_an_empty_hot_night_is_not_cached(monkeypatch):
    index = _Index((), hot=True)
    _register_bare(monkeypatch, archive_idx=index)
    assert media._archive_chunks_cached("alpha", "RO000H", "20261121") == []
    assert media._archive_chunks_cached("alpha", "RO000H", "20261121") == []
    assert len(index.listed) == 2, "clips may still land, so the night is re-listed"
