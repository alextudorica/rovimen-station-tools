"""The rate limiter fails open when its Redis storage errors.

On 2026-09-22 a full VPS root disk put Redis in MISCONF; flask-limiter raised
on every counter increment, so every rate-limited route (station heartbeats,
blob ingest, the public API) returned 500. A limiter outage must cost rate
limiting, never the route.
"""

from __future__ import annotations

from flask import Flask

import security

# Bound at collection, before any app fixture swaps security.init_limiter for
# a disabled limiter (several do, for the whole of their module).
_init_limiter = security.init_limiter


def _app(monkeypatch, redis_url: str | None) -> Flask:
    if redis_url is None:
        monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
    else:
        monkeypatch.setenv("ROVIMEN_REDIS_URL", redis_url)
    app = Flask(__name__)
    limiter = _init_limiter(app)

    @app.route("/limited")
    @limiter.limit("2/minute")
    def limited():
        return "ok"

    return app


def test_dead_redis_does_not_500_a_limited_route(monkeypatch) -> None:
    client = _app(monkeypatch, "redis://127.0.0.1:6390/0").test_client()
    assert [client.get("/limited").status_code for _ in range(2)] == [200, 200]


def test_dead_redis_falls_back_to_memory_counters(monkeypatch) -> None:
    # The fallback still limits, per worker, rather than disabling limits.
    client = _app(monkeypatch, "redis://127.0.0.1:6390/0").test_client()
    codes = [client.get("/limited").status_code for _ in range(4)]
    assert codes[-1] == 429


def test_memory_storage_still_limits(monkeypatch) -> None:
    client = _app(monkeypatch, None).test_client()
    codes = [client.get("/limited").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
