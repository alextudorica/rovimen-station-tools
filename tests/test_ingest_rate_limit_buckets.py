"""Ingest requests without a valid key for the URL's station share one bucket.

The ingest limiter keys on the ``<station>`` in the URL and runs before the key
check, so junk POSTs to ``/api/ingest/v1/<station>/heartbeat`` used to spend
that station's real budget. The limiter now checks the key itself (one sha256
lookup) and counts a request whose key is not valid for that station in a
shared anonymous bucket; valid station traffic keeps its own bucket and limits.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from flask import Flask
from werkzeug.security import generate_password_hash

import ingest_api
import security
import station_keys

# Bound at collection, before any app fixture swaps it for a disabled limiter.
_init_limiter = security.init_limiter


class _Cache:
    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        pass

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        pass


def _build(tmp_path: Path, keys_path: Path) -> Any:
    app = Flask(__name__)
    app.testing = True
    cache = _Cache()
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=cache.set_status,
        cache_set_vitals=cache.set_vitals,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        detections_db_path=tmp_path / "detections.db",
        station_state_path=tmp_path / "state.db",
        station_keys_path=keys_path,
        limiter=_init_limiter(app),
    )
    return app.test_client()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
    monkeypatch.setattr(ingest_api, "_INGEST_RATE", "3/minute")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def minted(tmp_path: Path):
    keys_path = tmp_path / "station_keys.yaml"
    _, vaslui = station_keys.add_key("gmn0002", path=keys_path)
    _, roit = station_keys.add_key("gmnro10", path=keys_path)
    return _build(tmp_path, keys_path), vaslui, roit


class _Poster:
    def __init__(self, client: Any) -> None:
        self.client = client
        self.seq = 0

    def __call__(self, station: str, secret: str | None) -> int:
        self.seq += 1
        headers = {"X-Station-Key": secret} if secret is not None else {}
        return self.client.post(
            f"/api/ingest/v1/{station}/heartbeat",
            json={"seq": self.seq, "sent_at": "2026-11-22T12:00:00Z",
                  "payload": {"online": True}},
            headers=headers,
        ).status_code


def test_junk_posts_do_not_spend_the_stations_budget(minted):
    client, vaslui, roit = minted
    post = _Poster(client)

    assert [post("gmn0002", None), post("gmn0002", "rvmn_gmn0002_" + "0" * 64),
            post("gmn0002", roit)] == [401, 401, 403]

    assert [post("gmn0002", vaslui) for _ in range(4)] == [200, 200, 200, 429]


def test_valid_station_traffic_keeps_its_own_limit(minted):
    client, vaslui, roit = minted
    post = _Poster(client)

    assert [post("gmn0002", vaslui) for _ in range(4)] == [200, 200, 200, 429]
    assert post("gmnro10", roit) == 200


def test_junk_for_every_station_shares_one_bucket(minted):
    client, _, _ = minted
    post = _Poster(client)

    assert [post("gmn0002", "junk") for _ in range(3)] == [401, 401, 401]
    assert post("gmnro10", "junk") == 429
    assert post("gmn9999", "junk") == 429


def test_station_row_without_sha256_keeps_the_old_bucket_until_its_first_push(tmp_path):
    """A row minted before sha256 existed cannot be checked cheaply, so the
    limiter counts its URL's requests in the station bucket, as before; after
    the station's first valid push records the sha256, junk goes anonymous."""
    keys_path = tmp_path / "station_keys.yaml"
    keys_path.write_text(yaml.safe_dump({"keys": [{
        "id": "gmn0002", "station": "gmn0002",
        "secret_hash": generate_password_hash("s-vaslui"),
    }]}))
    post = _Poster(_build(tmp_path, keys_path))

    assert post("gmn0002", "junk") == 401          # station bucket: 1
    assert post("gmn0002", "s-vaslui") == 200      # station bucket: 2, records sha256
    assert [post("gmn0002", "junk") for _ in range(3)] == [401, 401, 401]  # anonymous
    assert post("gmn0002", "s-vaslui") == 200      # station bucket: 3
    assert post("gmn0002", "s-vaslui") == 429


def test_a_failing_key_check_in_the_limiter_keeps_the_station_bucket(minted, monkeypatch):
    client, vaslui, _ = minted
    post = _Poster(client)

    def _boom(*_a, **_k):
        raise RuntimeError("key store exploded")
    monkeypatch.setattr(station_keys, "authorizes_fast", _boom)

    assert [post("gmn0002", vaslui) for _ in range(4)] == [200, 200, 200, 429]


def test_soft_mode_keeps_per_station_buckets(minted, monkeypatch):
    client, _, _ = minted
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "0")
    post = _Poster(client)

    assert [post("gmn0002", None) for _ in range(4)] == [200, 200, 200, 429]
    assert post("gmnro10", None) == 200
