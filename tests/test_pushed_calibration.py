"""#1248: stations push platepar, mask, calibration summary and hardware, and
the dashboard serves them without contacting port 7779.

Three layers: the ingest route stores what arrives, the pusher builds and
de-duplicates the body, and the four read routes answer from the pushed copy
with every live-station call stubbed to fail the test.
"""

from __future__ import annotations

import base64
import gzip
import json
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

import ingest_api
import platepar_store
import pushed_calibration
import rovimen_pusher as rp
import station_keys

import sys

sys.path.insert(0, str(Path(__file__).parent))
from test_public_dashboard_authz import app_env, viewer  # noqa: E402,F401  (fixtures)

MASK = b"BM" + bytes(54) + bytes([255]) * 4000 + bytes(800)
PP = {"az_centre": 120.0, "alt_centre": 40.0, "fov_h": 88.0, "fov_v": 48.0,
      "lat": 45.0, "lon": 25.0, "rotation_from_horiz": 1.0}


@pytest.fixture(autouse=True)
def _pushed_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVIMEN_PUSHED_STATE_DIR", str(tmp_path / "pushed"))
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    keys_path = tmp_path / "station_keys.yaml"
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()
    seen: list[dict] = []
    monkeypatch.setattr(platepar_store, "update_host", lambda payload: seen.append(payload))
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=lambda *a, **k: None,
        cache_set_vitals=lambda *a, **k: None,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        cam_owners=lambda cam: {"gmnro10"} if cam == "RO000E" else {"gmn0002"},
        detections_db_path=tmp_path / "det.db",
        station_state_path=tmp_path / "state.db",
        station_keys_path=keys_path,
        limiter=None,
    )
    return {"client": app.test_client(), "secret": secret, "platepar_store": seen}


def _post(env: dict, seq: int, payload: dict[str, Any], station: str = "gmn0002"):
    return env["client"].post(
        f"/api/ingest/v1/{station}/calibration",
        headers={"X-Station-Key": env["secret"]},
        json={"schema_version": 1, "seq": seq, "sent_at": "2026-11-02T12:00:00Z",
              "payload": payload},
    )


def _mask_blob(raw: bytes = MASK) -> dict:
    return {"encoding": "gzip+base64",
            "data_b64": base64.b64encode(gzip.compress(raw)).decode()}


# ── ingest ──────────────────────────────────────────────────────────────


def test_a_push_stores_every_section(ingest):
    r = _post(ingest, 1, {
        "platepar": {"RO000M": PP},
        "calibration_summary": {"RO000M": {"status": "ok", "date": "20261101"}},
        "hardware": {"cpu": "N100"},
        "masks": {"RO000M": _mask_blob()},
    })
    assert r.status_code == 200, r.get_json()
    assert pushed_calibration.get_platepar("gmn0002") == {"RO000M": PP}
    assert pushed_calibration.get_hardware("gmn0002") == {"cpu": "N100"}
    assert pushed_calibration.get_calibration_summary("gmn0002", "RO000M")["date"] == "20261101"
    state, path = pushed_calibration.mask_state("gmn0002", "RO000M")
    assert state == "mask" and path.read_bytes() == MASK
    # Coverage / network map read platepar_store; the push feeds it directly.
    assert ingest["platepar_store"] == [{"RO000M": PP}]


def test_a_missing_section_keeps_the_stored_one(ingest):
    _post(ingest, 1, {"platepar": {"RO000M": PP}, "hardware": {"cpu": "N100"}})
    _post(ingest, 2, {"platepar": None, "hardware": {"cpu": "i5"}})
    assert pushed_calibration.get_platepar("gmn0002") == {"RO000M": PP}
    assert pushed_calibration.get_hardware("gmn0002") == {"cpu": "i5"}


def test_the_mask_set_is_authoritative(ingest):
    _post(ingest, 1, {"masks": {"RO000M": _mask_blob(), "RO000N": _mask_blob()}})
    _post(ingest, 2, {"masks": {"RO000M": _mask_blob()}})
    assert pushed_calibration.mask_state("gmn0002", "RO000M")[0] == "mask"
    assert pushed_calibration.mask_state("gmn0002", "RO000N") == ("none", None)


def test_a_replayed_push_is_dropped(ingest):
    _post(ingest, 5, {"hardware": {"cpu": "new"}})
    r = _post(ingest, 4, {"hardware": {"cpu": "old"}})
    assert r.get_json()["dropped"] is True
    assert pushed_calibration.get_hardware("gmn0002") == {"cpu": "new"}


def test_a_camera_of_another_station_is_refused(ingest):
    r = _post(ingest, 1, {"platepar": {"RO000E": PP}})
    assert r.status_code == 403
    assert pushed_calibration.load("gmn0002") is None


@pytest.mark.parametrize("blob", [
    {"encoding": "gzip+base64", "data_b64": "not base64!"},
    {"encoding": "gzip+base64", "data_b64": base64.b64encode(b"not gzip").decode()},
    {"encoding": "raw", "data_b64": base64.b64encode(MASK).decode()},
    # A decompression bomb: 64 MB of zeros gzips to ~64 kB.
    {"encoding": "gzip+base64",
     "data_b64": base64.b64encode(gzip.compress(bytes(64 * 1024 * 1024))).decode()},
])
def test_a_bad_mask_is_rejected_without_storing_anything(ingest, blob):
    r = _post(ingest, 1, {"hardware": {"cpu": "x"}, "masks": {"RO000M": blob}})
    assert r.status_code == 422
    assert pushed_calibration.load("gmn0002") is None


def test_the_masks_of_one_push_share_a_total_cap(ingest, monkeypatch):
    monkeypatch.setattr(ingest_api, "_MAX_MASKS_TOTAL_RAW_BYTES", len(MASK) * 2)
    ok = _post(ingest, 1, {"masks": {"RO000M": _mask_blob(), "RO000N": _mask_blob()}})
    assert ok.status_code == 200
    r = _post(ingest, 2, {"masks": {c: _mask_blob() for c in ("RO000M", "RO000N", "RO000P")}})
    assert r.status_code == 422


def test_the_ingest_index_lists_the_route(ingest):
    eps = ingest["client"].get("/api/ingest/v1").get_json()["endpoints"]
    assert eps["calibration"] == "/api/ingest/v1/<station>/calibration"


# ── pusher ──────────────────────────────────────────────────────────────


class _Pub(rp.Publisher):
    def __init__(self) -> None:  # no spool / seq file needed
        self._base = "http://vps"
        self._host_key = "gmn0002"
        self._key = "k"
        self._stop = __import__("threading").Event()
        self._calibration_digest = None
        self._calibration_sent_at = 0.0

        class _Seq:
            n = 0

            def next(self) -> int:
                self.n += 1
                return self.n

        self._seq = _Seq()


def _local_station(monkeypatch, mask_status: int = 200):
    responses = {
        "/api/platepar": (200, json.dumps({"RO000M": PP}).encode()),
        "/api/hardware": (200, b'{"cpu": "N100"}'),
        "/api/calibration_summary/RO000M": (200, b'{"status": "ok", "date": "20261101"}'),
        "/api/mask_image/RO000M": (mask_status, MASK if mask_status == 200 else b""),
    }
    monkeypatch.setattr(rp, "_get_local", lambda path: responses.get(path, (None, b"")))


def test_the_pusher_posts_once_until_something_changes(monkeypatch):
    _local_station(monkeypatch)
    posts: list[dict] = []
    monkeypatch.setattr(rp, "post_json",
                        lambda url, key, body, **k: posts.append(body) or rp.PostResult(True, 200))
    pub = _Pub()
    assert pub.push_calibration() and pub.push_calibration()
    assert len(posts) == 1
    payload = posts[0]["payload"]
    assert payload["platepar"] == {"RO000M": PP}
    assert gzip.decompress(base64.b64decode(payload["masks"]["RO000M"]["data_b64"])) == MASK

    _local_station(monkeypatch, mask_status=404)  # operator removed the mask
    assert pub.push_calibration()
    assert len(posts) == 2 and posts[1]["payload"]["masks"] == {}


def test_a_failed_mask_read_sends_masks_as_unknown(monkeypatch):
    _local_station(monkeypatch, mask_status=500)
    state = rp.fetch_calibration_state()
    assert state["masks"] is None and state["platepar"] == {"RO000M": PP}


def test_a_failed_post_is_retried_next_cycle(monkeypatch):
    _local_station(monkeypatch)
    results = iter([rp.PostResult(False, 503), rp.PostResult(True, 200)])
    monkeypatch.setattr(rp, "post_json", lambda *a, **k: next(results))
    pub = _Pub()
    assert pub.push_calibration() is False
    assert pub.push_calibration() is True


def test_the_calibration_cadence_is_scheduled():
    assert rp.resolve_intervals({})["calibration"] == rp.CALIBRATION_INTERVAL


# ── read routes: zero station contact ───────────────────────────────────


@pytest.fixture
def no_station(monkeypatch):
    import routes.station_ops as station_ops
    import routes.station_proxy as station_proxy

    def _boom(*a, **k):
        raise AssertionError("the dashboard contacted a station")

    import route_helpers

    for mod in (station_proxy, station_ops, route_helpers):
        monkeypatch.setattr(mod, "station_get_raw", _boom, raising=False)
        monkeypatch.setattr(mod, "_session_for_url", _boom, raising=False)


def _seed_pub_station() -> None:
    pushed_calibration.save(
        "pub_station",
        platepar={"PUB001": PP},
        calibration_summary={"PUB001": {"status": "ok", "date": "20261101", "drift": 0.1}},
        hardware={"cpu": "N100"},
        masks={"PUB001": MASK},
    )


def test_pushed_state_serves_all_four_routes_without_the_station(viewer, no_station):  # noqa: F811
    _seed_pub_station()
    assert viewer.get("/api/platepar/pub_station").get_json() == {"PUB001": PP}
    assert viewer.get("/api/hardware/pub_station").get_json() == {"cpu": "N100"}
    summ = viewer.get("/api/calibration_summary/pub_station/PUB001").get_json()
    assert summ["date"] == "20261101" and summ["drift"] == 0.1
    r = viewer.get("/api/mask_image/pub_station/PUB001")
    assert r.status_code == 200 and r.data == MASK and r.mimetype == "image/bmp"


def test_offline_station_without_a_push_is_not_contacted(viewer, no_station):  # noqa: F811
    """Nothing pushed yet and no positive "online" in the status cache: every
    route answers from what the VPS has (nothing) instead of trying :7779."""
    assert viewer.get("/api/hardware/priv_station").status_code == 404
    summ = viewer.get("/api/calibration_summary/priv_station/PRV001").get_json()
    assert summ["status"] == "no_data"
    assert viewer.get("/api/mask_image/priv_station/PRV001").status_code == 404


def test_a_station_that_pushed_no_mask_404s(viewer, no_station):  # noqa: F811
    pushed_calibration.save("pub_station", masks={})
    assert viewer.get("/api/mask_image/pub_station/PUB001").status_code == 404


def test_an_older_night_is_not_answered_with_last_nights_summary(viewer, no_station):  # noqa: F811
    _seed_pub_station()
    summ = viewer.get("/api/calibration_summary/pub_station/PUB001?date=20261020").get_json()
    assert summ["status"] == "no_data" and summ["date"] == "20261020"


def test_drop_mask_forgets_only_that_camera(tmp_path):
    pushed_calibration.save("gmn0002", masks={"RO000M": MASK, "RO000N": MASK})
    pushed_calibration.drop_mask("gmn0002", "RO000M")
    assert pushed_calibration.mask_state("gmn0002", "RO000M") == ("unknown", None)
    assert pushed_calibration.mask_state("gmn0002", "RO000N")[0] == "mask"


def test_invalid_host_keys_never_touch_the_filesystem():
    assert pushed_calibration.load("../etc") is None
    with pytest.raises(ValueError):
        pushed_calibration.save("../etc", hardware={})
    assert Path(pushed_calibration.root()).exists() is False
