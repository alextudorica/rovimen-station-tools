"""Keyed public API routes count each consumer in its own key bucket.

The key gate has to run before the rate limit: then the limiter sees the
validated key and buckets ``key:<id>``, and a request with a bad key is
answered 401 without being counted. ``hall_of_fame`` had the two decorators
the other way round, so every consumer shared one bucket keyed on the proxy
address and bad keys used it up.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest
from flask import Flask

import api_keys
import public_api
import security
from rovimen_dashboard import DashboardConfig

# Bound at collection, before any app fixture swaps security.init_limiter for a
# disabled limiter.
_init_limiter = security.init_limiter
_validate = api_keys.validate

URL = "/api/public/v1/hall_of_fame"


@pytest.fixture
def keyed_app(tmp_path: Path, monkeypatch):
    import hall_of_fame_store

    monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "1")
    monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
    monkeypatch.setattr(hall_of_fame_store, "_PATH", tmp_path / "hall_of_fame.json")
    monkeypatch.setattr(hall_of_fame_store, "_LOCK_PATH", tmp_path / "hof.lock")
    keys_path = tmp_path / "api_keys.yaml"
    api_keys._invalidate_cache()
    _, key_a = api_keys.add_key("site-a", path=keys_path)
    _, key_b = api_keys.add_key("site-b", path=keys_path)
    monkeypatch.setattr(api_keys, "validate", functools.partial(_validate, path=keys_path))
    monkeypatch.setattr(public_api, "_DEFAULT_RATE", "2/minute")

    app = Flask(__name__)
    app.testing = True
    public_api.register_public_routes(
        app,
        config=DashboardConfig(),
        compute_detections_payload=lambda _date: {},
        get_timelapses_payload=lambda _host: {},
        station_cache_get_status=lambda _host: None,
        limiter=_init_limiter(app),
    )
    yield app.test_client(), key_a, key_b
    api_keys._invalidate_cache()


def test_hall_of_fame_counts_each_key_in_its_own_bucket(keyed_app):
    client, key_a, key_b = keyed_app

    codes_a = [client.get(URL, headers={"X-API-Key": key_a}).status_code for _ in range(3)]

    assert codes_a == [200, 200, 429]
    assert client.get(URL, headers={"X-API-Key": key_b}).status_code == 200


def test_hall_of_fame_bad_keys_do_not_use_up_a_consumers_budget(keyed_app):
    client, key_a, _ = keyed_app

    for _ in range(5):
        assert client.get(URL, headers={"X-API-Key": "0" * 64}).status_code == 401

    assert client.get(URL, headers={"X-API-Key": key_a}).status_code == 200
