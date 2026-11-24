"""Detections routes live at module level (routes/detections.py).

The handlers and their helpers used to be closures inside
``register_detections_routes``, so no helper could be imported or unit-tested,
the payload builder reached ``routes/highlights.py``, the public API and the
tests through ``app._compute_detections_payload``, the live-feed reset through
``app._reset_live_feed``, and the two archive readers came the other way, from
``create_app`` as ``app._archive_nights`` / ``app._read_archive_locked_chunks``.
These tests pin what the move must not change (URL rules, endpoint names,
methods, the auth and CSRF treatment of every view) and the new contract:
helpers work without an app, ``register_detections_routes`` stores the
dependencies (the per-app caches and live-feed state with them, fresh per
registration), and no ``app._x`` handoff is left.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from flask import Flask

import auth
import security

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import anon, app_env  # noqa: E402,F401  (fixtures)

import routes.detections as det  # noqa: E402

_GET = ["GET", "HEAD", "OPTIONS"]


def _row(rule: str, endpoint: str, *, page: str = "events") -> dict:
    return {"rule": rule, "endpoint": endpoint, "methods": _GET, "public": True,
            "page": page, "require_station": False, "require_auth": False,
            "csrf_exempt": True}


# Captured from origin/main 6b8ef951, while these views were closures.
EXPECTED_ENDPOINTS = [
    _row("/api/detections/<date>", "api_detections"),
    _row("/api/detections/grid/<date>", "api_detections_grid"),
    _row("/api/detections/nights", "api_detections_nights"),
    _row("/api/detections/range", "api_detections_range"),
    _row("/api/footage/grid/<date>", "api_footage_grid"),
    _row("/api/live-feed", "api_live_feed", page="live"),
]

# One concrete request per endpoint; on origin/main an anonymous caller got
# 200 from each (the "events" and "live" pages are public by default).
SAMPLE = {
    "api_detections": "/api/detections/20260101",
    "api_detections_grid": "/api/detections/grid/20260101",
    "api_detections_nights": "/api/detections/nights",
    "api_detections_range": "/api/detections/range?from=20260101&to=20260102",
    "api_footage_grid": "/api/footage/grid/20260101",
    "api_live_feed": "/api/live-feed",
}


def _detections_endpoints(app: Flask) -> list[dict]:
    station_guard = auth.require_station(lambda **k: None).__code__
    auth_guard = auth.require_auth(lambda **k: None).__code__
    csrf = app.extensions["csrf"]
    out = []
    for rule in sorted(app.url_map.iter_rules(), key=lambda r: r.rule):
        view = app.view_functions[rule.endpoint]
        if view.__module__ != "routes.detections":
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
    assert _detections_endpoints(app_env) == EXPECTED_ENDPOINTS


@pytest.mark.parametrize("row", EXPECTED_ENDPOINTS, ids=lambda r: r["endpoint"])
def test_the_auth_gate_sees_the_same_public_views(app_env, anon, row, monkeypatch):  # noqa: F811
    def _no_station(*a, **kw):
        raise AssertionError("an anonymous gate check must not reach a station")

    monkeypatch.setattr(det, "station_get_raw", _no_station)
    url = SAMPLE[row["endpoint"]]
    with app_env.test_request_context(url):
        matched = security._matched_public_view(app_env)
    assert matched is app_env.view_functions[row["endpoint"]]
    assert anon.get(url).status_code == 200


def test_no_app_attributes_are_left_for_other_code_to_reach_into(app_env):  # noqa: F811
    for name in ("_archive_nights", "_read_archive_locked_chunks",
                 "_compute_detections_payload", "_reset_live_feed"):
        assert not hasattr(app_env, name), name


def test_media_lists_archive_nights_through_the_detections_reader():
    import routes.media as media

    assert media._archive_nights is det._archive_nights


class _Index:
    """The two archive-index calls the readers make."""

    def __init__(self, meteors: tuple[str, ...] = (), stacks: frozenset = frozenset(),
                 nights: list[str] | None = None) -> None:
        self._listing = SimpleNamespace(meteor_files=meteors, stack_files=stacks)
        self._nights = nights or []

    def nights_with_meteors(self, cam: str) -> list[str]:
        return list(self._nights)

    def night_files(self, cam: str, date: str):
        return self._listing


def _register_bare(monkeypatch, **overrides) -> tuple[Flask, SimpleNamespace]:
    # monkeypatch restores the deps of whichever app the suite built before.
    monkeypatch.setattr(det, "_DEPS", det._DEPS)
    app = Flask(__name__)
    config = SimpleNamespace(stations={}, correlation_window_s=1)
    kwargs: dict = {"archive_idx": _Index(), "detections_cache": {},
                    "detections_ttl": 1800.0}
    kwargs.update(overrides)
    det.register_detections_routes(app, config, None, None, **kwargs)
    return app, config


def test_each_registration_replaces_the_deps(monkeypatch):
    _, config1 = _register_bare(monkeypatch)
    deps1 = det._deps()
    _, config2 = _register_bare(monkeypatch)
    deps2 = det._deps()
    assert deps1.config is config1 and deps2.config is config2
    # Closure state stays per registration, as it was per closure.
    assert deps1.footage_cache is not deps2.footage_cache
    assert deps1.refreshing is not deps2.refreshing
    assert deps1.refreshing_lock is not deps2.refreshing_lock
    assert deps1.live_feed_state is not deps2.live_feed_state
    assert deps1.live_feed_lock is not deps2.live_feed_lock
    assert deps1.live_feed_ready is not deps2.live_feed_ready
    # No shared tiers or night store passed: disabled ones keep the
    # per-worker behaviour.
    assert not deps2.shared_detections.enabled
    assert not deps2.shared_live_feed.enabled
    assert not deps2.night_store.enabled


def test_deps_before_registration_is_an_error(monkeypatch):
    monkeypatch.setattr(det, "_DEPS", None)
    with pytest.raises(RuntimeError):
        det._deps()
    with pytest.raises(RuntimeError):
        det.compute_detections_payload("20261121")
    with pytest.raises(RuntimeError):
        det._archive_nights("RO000H")


def test_archive_nights_reads_the_registered_index(monkeypatch):
    _register_bare(monkeypatch, archive_idx=_Index(nights=["20261121", "20261120"]))
    assert det._archive_nights("RO000H") == ["20261121", "20261120"]


def test_read_archive_locked_chunks_without_an_app(monkeypatch, tmp_path):
    night = tmp_path / "RO000H" / "20261121"
    night.mkdir(parents=True)
    locked = "RO000H_20261121_010000_color.mkv"
    spliced = "RO000H_20261121_020000_color.mkv"
    (night / "state.json").write_text(json.dumps({"chunks": {
        locked: {"lock": {"lock_type": "manual",
                          "meteor_time": "2026-11-21T01:00:05.500000Z"},
                 "reencoded": True},
        spliced: {"lock": {"lock_type": "detection",
                           "meteor_time": "2026-11-21T02:00:03Z"}},
        "RO000H_20261121_030000_color.mkv": {"reencoded": False},           # not locked
        "RO000H_20261121_040000_color.mkv": {"lock": {"lock_type": "detection"}},
        "not-a-dict": 1,
    }}))
    monkeypatch.setattr(det, "ARCHIVE_PATH", tmp_path)
    _register_bare(monkeypatch, archive_idx=_Index(
        # The 04:00 chunk is locked but its clip never reached meteors/.
        meteors=(spliced, locked, "RO000H_20261121_020000_stack.webp"),
        stacks=frozenset({"RO000H_20261121_010000_stack.webp"}),
    ))

    assert det._read_archive_locked_chunks("RO000H", "20261121") == [
        {"filename": locked, "time": "01:00:00",
         "stack": "RO000H_20261121_010000_stack.webp", "stack_subdir": "stacks",
         "size_mb": None, "locked": True, "lock_type": "manual",
         "detection_offset_s": 5.5, "meteor_time": "2026-11-21T01:00:05.500000Z",
         "reencoded": True, "source": "archive"},
        {"filename": spliced, "time": "02:00:00",
         "stack": "RO000H_20261121_020000_stack.webp", "stack_subdir": "meteors",
         "size_mb": None, "locked": True, "lock_type": "detection",
         "detection_offset_s": 3.0, "meteor_time": "2026-11-21T02:00:03Z",
         "reencoded": False, "source": "archive"},
    ]
    assert det._read_archive_locked_chunks("RO000H", "20261120") == []


def test_reset_live_feed_returns_the_registered_snapshot_to_cold(monkeypatch):
    _register_bare(monkeypatch)
    deps = det._deps()
    deps.live_feed_state.update(built=123.0, data={"x": {}}, refreshing=True)
    deps.live_feed_ready.set()

    det._reset_live_feed()

    assert deps.live_feed_state == {"built": 0.0, "data": None, "refreshing": False}
    assert not deps.live_feed_ready.is_set()
