"""/sw.js carries the build version, so every deploy installs a new worker.

The route used to replace the literal ``const CACHE_VERSION = 'v20';`` while
sw.js had moved on to hand-bumped values ('v36'), so the stamp never applied
and old caches were never deleted. sw.js now carries a token the route
replaces with BUILD_VERSION.
"""

from __future__ import annotations

from pathlib import Path

from flask import Flask

from routes.misc import register_misc_routes

STATIC = Path(__file__).resolve().parent.parent / "dashboard" / "static"


def _client(build_version: str):
    app = Flask(__name__, static_folder=str(STATIC))
    register_misc_routes(app, config=None, build_version=build_version)
    return app.test_client()


def test_sw_is_stamped_with_the_build_version():
    r = _client("1763812345").get("/sw.js")
    assert r.status_code == 200
    body = r.get_data(as_text=True)
    assert "const CACHE_VERSION = '1763812345';" in body
    assert "__BUILD_VERSION__" not in body
    assert r.mimetype == "application/javascript"
    assert r.headers["Service-Worker-Allowed"] == "/"


def test_each_deploy_serves_different_bytes():
    """A browser installs a new worker only when the script's bytes change."""
    a = _client("1763812345").get("/sw.js").get_data()
    b = _client("1763899999").get("/sw.js").get_data()
    assert a != b


def test_update_checks_are_not_served_from_the_http_cache():
    r = _client("1763812345").get("/sw.js")
    assert r.headers["Cache-Control"] == "no-cache"
