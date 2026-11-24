"""Archive routes live at module level (routes/archive.py).

The handlers and their helpers used to be closures inside
``register_archive_routes``, so no helper could be imported or unit-tested, the
nights_full pre-warm reached the deep walk through ``app._refresh_nights_full``
and tests reached the /api/spectro caches through ``app._spectro_*``. These
tests pin what the move must not change (URL rules, endpoint names, methods,
the auth and CSRF treatment of every view) and the new contract: helpers work
without an app, ``register_archive_routes`` stores the dependencies (the
/api/spectro caches with them, fresh per registration), and the hourly cache
cleanup is a module-level loop that checks its duty on every pass. That the
loop starts from the background workers rather than from route registration is
pinned in ``test_deferred_background_startup.py``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask
from werkzeug.exceptions import BadRequest

import auth
import security

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import anon, app_env  # noqa: E402,F401  (fixtures)

import routes.archive as archive  # noqa: E402

_GET = ["GET", "HEAD", "OPTIONS"]


def _row(rule: str, endpoint: str, *, methods: list[str] = _GET, public: bool = False,
         page: str | None = None, require_station: bool = False,
         require_auth: bool = False, csrf_exempt: bool = True) -> dict:
    return {"rule": rule, "endpoint": endpoint, "methods": methods, "public": public,
            "page": page, "require_station": require_station,
            "require_auth": require_auth, "csrf_exempt": csrf_exempt}


# Captured from origin/main ecf258f3, while these views were closures.
EXPECTED_ENDPOINTS = [
    _row("/api/archive/cameras", "api_archive_cameras"),
    _row("/api/archive/file/<camera>/<date>/<subdir>/<filename>", "api_archive_file"),
    _row("/api/archive/indexed/<camera>/<date>", "api_archive_indexed"),
    _row("/api/archive/meteors/<camera>/<date>", "api_archive_meteors"),
    _row("/api/archive/nights/<camera>", "api_archive_nights"),
    _row("/api/archive/nights_full/<camera>", "api_archive_nights_full"),
    _row("/api/archive/rms-detections/<camera>/<date>", "api_archive_rms_detections",
         require_station=True),
    _row("/api/archive/shortclip/<camera>/<date>/<filename>", "archive_shortclip",
         require_station=True),
    _row("/api/cached-files/<station_code>/<date>", "api_cached_files"),
    _row("/api/cached-video/<host_key>/<station_code>/<date>/<filename>",
         "api_cached_video", public=True, page="events"),
    _row("/api/calibration/astrometry_residual/<camera>",
         "api_calibration_astrometry_residual"),
    _row("/api/calibration/ff_intervals/<camera>", "api_calibration_ff_intervals"),
    _row("/api/calibration/plot/<kind>/<camera>", "api_calibration_plot"),
    _row("/api/rms/color-stack-latest/<host_key>/<camera>", "api_rms_color_stack_latest",
         public=True, page="overview"),
    _row("/api/rms/plots-latest", "api_rms_plots_latest_batch", public=True, page="overview"),
    _row("/api/rms/plots-latest/<host_key>/<camera>", "api_rms_plots_latest",
         public=True, page="overview"),
    _row("/api/spectro/<host_key>", "api_spectro", require_station=True),
    _row("/api/stitch-multi", "api_stitch_multi", methods=["OPTIONS", "POST"],
         require_auth=True),
    _row("/api/stitch-multi/<job>", "api_stitch_multi_result", require_auth=True),
    _row("/loop_clip_archive/<camera>/<date>/<filename>", "loop_clip_archive",
         require_station=True, csrf_exempt=False),
]

_CLIP = "PUB001_20260101_010101_color.mkv"
# One concrete request per endpoint and what an anonymous caller gets, for the
# request-level gate checks (statuses as on origin/main).
SAMPLE = {
    "api_archive_cameras": ("GET", "/api/archive/cameras", 401),
    "api_archive_file": ("GET", f"/api/archive/file/PUB001/20260101/meteors/{_CLIP}", 401),
    "api_archive_indexed": ("GET", "/api/archive/indexed/PUB001/20260101", 401),
    "api_archive_meteors": ("GET", "/api/archive/meteors/PUB001/20260101", 401),
    "api_archive_nights": ("GET", "/api/archive/nights/PUB001", 401),
    "api_archive_nights_full": ("GET", "/api/archive/nights_full/PUB001", 401),
    "api_archive_rms_detections": ("GET", "/api/archive/rms-detections/PUB001/20260101", 401),
    "archive_shortclip": ("GET", f"/api/archive/shortclip/PUB001/20260101/{_CLIP}", 401),
    "api_cached_files": ("GET", "/api/cached-files/PUB001/20260101", 401),
    # Anonymous visitors of a public station are sent to the keyless media surface.
    "api_cached_video": ("GET", f"/api/cached-video/pub_station/PUB001/20260101/{_CLIP}", 302),
    "api_calibration_astrometry_residual": (
        "GET", "/api/calibration/astrometry_residual/PUB001", 401),
    "api_calibration_ff_intervals": ("GET", "/api/calibration/ff_intervals/PUB001", 401),
    "api_calibration_plot": ("GET", "/api/calibration/plot/ff_intervals/PUB001", 401),
    "api_rms_color_stack_latest": ("GET", "/api/rms/color-stack-latest/pub_station/PUB001", 200),
    "api_rms_plots_latest_batch": ("GET", "/api/rms/plots-latest?cams=pub_station/PUB001", 200),
    "api_rms_plots_latest": ("GET", "/api/rms/plots-latest/pub_station/PUB001", 200),
    "api_spectro": ("GET", "/api/spectro/pub_station", 401),
    "api_stitch_multi": ("POST", "/api/stitch-multi", 401),
    "api_stitch_multi_result": ("GET", "/api/stitch-multi/" + "a" * 32, 401),
    # Not under /api/: the gate sends a browser to the login page.
    "loop_clip_archive": ("GET", f"/loop_clip_archive/PUB001/20260101/{_CLIP}", 302),
}


def _archive_endpoints(app: Flask) -> list[dict]:
    station_guard = auth.require_station(lambda **k: None).__code__
    auth_guard = auth.require_auth(lambda **k: None).__code__
    csrf = app.extensions["csrf"]
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        view = app.view_functions[rule.endpoint]
        if view.__module__ != "routes.archive":
            continue
        out.append({
            "rule": rule.rule,
            "endpoint": rule.endpoint,
            "methods": sorted(rule.methods),
            "public": bool(getattr(view, security._PUBLIC_ROUTE_ATTR, False)),
            "page": getattr(view, security._PUBLIC_PAGE_ATTR, None),
            "require_station": view.__code__ is station_guard,
            "require_auth": view.__code__ is auth_guard,
            "csrf_exempt": f"{view.__module__}.{view.__name__}" in csrf._exempt_views,
        })
    return out


def test_endpoints_keep_their_rules_methods_auth_and_csrf(app_env):  # noqa: F811
    assert _archive_endpoints(app_env) == EXPECTED_ENDPOINTS


@pytest.mark.parametrize("row", EXPECTED_ENDPOINTS, ids=lambda r: r["endpoint"])
def test_the_auth_gate_sees_the_same_public_views(app_env, anon, row):  # noqa: F811
    method, url, anon_status = SAMPLE[row["endpoint"]]
    with app_env.test_request_context(url, method=method):
        matched = security._matched_public_view(app_env)
    if row["public"]:
        assert matched is app_env.view_functions[row["endpoint"]]
    else:
        assert matched is None
    body = {} if method == "POST" else None
    assert anon.open(url, method=method, json=body).status_code == anon_status


def test_no_app_attributes_are_left_for_other_code_to_reach_into(app_env):  # noqa: F811
    for name in ("_refresh_nights_full", "_spectro_cache", "_spectro_miss", "_spectro_hits"):
        assert not hasattr(app_env, name), name


def test_helpers_work_without_an_app():
    names = {"X_color.details.mkv"}
    assert archive._prefer_details_variant("X_color.mkv", names) == "X_color.details.mkv"
    assert archive._prefer_details_variant("X_color.mp4", names) == "X_color.details.mkv"
    assert archive._prefer_details_variant("Y_color.mkv", names) == "Y_color.mkv"
    archive._validate_archive_path("RO000H", "20261122")
    with pytest.raises(BadRequest):
        archive._validate_archive_path("RO000H", "..")
    with pytest.raises(BadRequest):
        archive._validate_archive_path("a/b")


def _register_bare(monkeypatch, **overrides) -> tuple[Flask, SimpleNamespace]:
    # monkeypatch restores the deps of whichever app the suite built before.
    monkeypatch.setattr(archive, "_DEPS", archive._DEPS)
    app = Flask(__name__)
    config = SimpleNamespace(stations={})
    kwargs: dict = {"archive_idx": None, "nights_full_cache": {}, "nights_full_ttl": 120.0}
    kwargs.update(overrides)
    archive.register_archive_routes(app, config, None, **kwargs)
    return app, config


def test_each_registration_replaces_the_deps(monkeypatch):
    app1, config1 = _register_bare(monkeypatch)
    deps1 = archive._deps()
    app2, config2 = _register_bare(monkeypatch)
    deps2 = archive._deps()
    assert deps1.config is config1 and deps2.config is config2
    # The /api/spectro caches were per-registration closure state; they still are.
    assert deps1.spectro_cache is not deps2.spectro_cache
    assert deps1.spectro_miss is not deps2.spectro_miss
    assert deps1.spectro_hits is not deps2.spectro_hits
    # No shared tier passed: a disabled store keeps the per-worker behaviour.
    assert deps2.shared_spectro is not None


def test_deps_before_registration_is_an_error(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "_DEPS", None)
    with pytest.raises(RuntimeError):
        archive._deps()
    with pytest.raises(RuntimeError):
        archive.refresh_nights_full("RO000H")
    # The clip resolvers other route modules call stay usable without an index.
    monkeypatch.setattr(archive, "ARCHIVE_PATH", tmp_path)
    assert archive._archive_clip_path("RO000H", "20261122", "x_color.mkv") is None
    assert archive.archive_detection_window("RO000H", "20261122", "x_color.mkv", 1, 1) is None


def test_refresh_nights_full_fills_the_registered_cache(monkeypatch):
    clip = "RO000H_20261121_010101_color.mkv"
    index = SimpleNamespace(
        nights=lambda cam: ["20261121"],
        night_files_for_listing=lambda cam, date: SimpleNamespace(
            meteor_files=(clip, "RO000H_20261121_010101_color.details.mkv"),
            stack_files=frozenset({"RO000H_20261121_010101_stack.webp"}),
            timelapse_files=("RO000H_20261121_timelapse.mp4",
                             "RO000H_20261121_night_stack.webp"),
            state_chunks={clip: {"lock": {"meteor_time": "2026-11-21T01:01:05Z"}}},
        ),
    )
    cache: dict = {}
    written: list = []
    monkeypatch.setattr(archive, "_disk_cache_write",
                        lambda name, key, data: written.append((name, key, data)))
    _register_bare(monkeypatch, archive_idx=index, nights_full_cache=cache)

    archive.refresh_nights_full("RO000H")

    payload = [{
        "date": "20261121",
        "meteors": [{
            "filename": "RO000H_20261121_010101_color.details.mkv",
            "stack": "RO000H_20261121_010101_stack.webp",
            "stack_subdir": "stacks",
            "detection_offset_s": 4.0,  # meteor_time minus the chunk's 01:01:01 start
            "meteor_time": "2026-11-21T01:01:05Z",
        }],
        "timelapse": "RO000H_20261121_timelapse.mp4",
        "timelapse_size_mb": None,
        "timelapse_stack": "RO000H_20261121_night_stack.webp",
    }]
    assert cache["RO000H"][1] == payload
    assert written == [("nights_full", "RO000H", payload)]


# ── Hourly cache cleanup ─────────────────────────────────────────────────


def test_cleanup_pass_removes_expired_files_and_empty_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "CACHE_PATH", tmp_path)
    old_s = time.time() - (archive.CACHE_TTL_HOURS + 1) * 3600
    stale = tmp_path / "RO000H" / "20261120" / "a_color.mkv"
    fresh = tmp_path / "RO000H" / "20261121" / "b_color.mkv"
    for f in (stale, fresh):
        f.parent.mkdir(parents=True)
        f.write_bytes(b"x")
    os.utime(stale, (old_s, old_s))
    (tmp_path / "stitch").mkdir()
    (tmp_path / "stitch" / "job.mp4").write_bytes(b"x")

    archive._cache_cleanup_once()

    assert not stale.parent.exists(), "expired file and its emptied night dir go"
    assert fresh.exists()
    assert (tmp_path / "stitch" / "job.mp4").exists(), "only <station>/<date>/<file> is swept"


def test_cleanup_pass_tolerates_a_missing_cache_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(archive, "CACHE_PATH", tmp_path / "absent")
    archive._cache_cleanup_once()


class _Stop(BaseException):
    pass


def test_cleanup_loop_sweeps_hourly_only_while_it_holds_the_duty(monkeypatch):
    sleeps: list[float] = []
    passes: list[int] = []

    def _sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) > 3:
            raise _Stop

    monkeypatch.setattr(archive, "time", SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(archive, "_cache_cleanup_once", lambda: passes.append(len(sleeps)))
    duty = iter([True, False, True])

    with pytest.raises(_Stop):
        archive.cache_cleanup_loop(lambda: next(duty))

    assert sleeps == [3600] * 4, "sleeps an hour before every pass, the first included"
    assert passes == [1, 3]


def test_cleanup_loop_survives_a_failed_pass(monkeypatch):
    sleeps: list[float] = []

    def _sleep(s: float) -> None:
        sleeps.append(s)
        if len(sleeps) > 2:
            raise _Stop

    def _boom() -> None:
        raise OSError("mount gone")

    monkeypatch.setattr(archive, "time", SimpleNamespace(sleep=_sleep))
    monkeypatch.setattr(archive, "_cache_cleanup_once", _boom)
    with pytest.raises(_Stop):
        archive.cache_cleanup_loop(lambda: True)
    assert len(sleeps) == 3
