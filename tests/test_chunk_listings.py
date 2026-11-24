"""Pushed chunk listings (#1250): pusher format, ingest route, VPS store.

The station compacts each camera-night's ``/api/chunks`` listing
(``rovimen_pusher.build_chunk_listing_payload``), POSTs it to
``/api/ingest/v1/<station>/chunks``, and the dashboard expands it back
(``chunk_listings.expand``). The round trip must reproduce the station's own
chunk dicts, because the videodb and footage views consume them unchanged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from flask import Flask

import chunk_listings
import ingest_api
import rovimen_pusher as rp
import station_keys
import upload_state


# A recent night: the store drops listings older than RETENTION_DAYS, so a
# fixed date stopped round-tripping once it aged out (2026-11-15).
_NIGHT = datetime.now(timezone.utc).date() - timedelta(days=1)
DATE = _NIGHT.strftime("%Y%m%d")
# Upload deadlines are pure functions of a night and a time: a fixed night.
FIXED_NIGHT = "20261101"
NEXT = (_NIGHT + timedelta(days=1)).strftime("%Y%m%d")
CAM = "RO000M"


def _station_chunks() -> list[dict[str, Any]]:
    """What station_collect.list_chunks returns for three chunks."""
    return [
        {"filename": f"{CAM}_{DATE}_183000_color.mkv", "time": "18:30:00",
         "stack": f"{CAM}_{DATE}_183000_stack.webp", "size_mb": 12.3,
         "locked": False, "lock_type": None, "detection_offset_s": None,
         "meteor_time": None, "reencoded": True},
        {"filename": f"{CAM}_{DATE}_190020_color.mkv", "time": "19:00:20",
         "stack": f"{CAM}_{DATE}_190020_stack.webp", "size_mb": 11.9,
         "locked": True, "lock_type": "detection", "detection_offset_s": 11.2,
         "meteor_time": "2026-11-01 19:00:31.2", "reencoded": False},
        # Legacy local-time name: the UTC start differs from the name.
        {"filename": f"{CAM}_{NEXT}_020000_color.mkv", "time": "00:00:00",
         "stack": f"{CAM}_{NEXT}_020000_stack.webp", "size_mb": 10.0,
         "locked": True, "lock_type": "manual", "detection_offset_s": None,
         "meteor_time": None, "reencoded": False},
    ]


def test_pusher_payload_round_trips_to_the_station_chunk_dicts():
    listing = {"morning_done": True, "chunks": _station_chunks()}
    payload = rp.build_chunk_listing_payload(CAM, DATE, listing)
    assert payload["morning_done"] is True
    # Compact: the stamp, the size, and a UTC time only where it differs.
    assert payload["chunks"][0] == [f"{DATE}_183000", 12.3]
    assert payload["chunks"][2] == [f"{NEXT}_020000", 10.0, "00:00:00"]
    assert payload["reencoded"] == [f"{DATE}_183000"]
    expanded = chunk_listings.expand(CAM, payload)
    for got in expanded:
        assert got.pop("source") == "pushed"
    assert expanded == _station_chunks()


def test_pusher_skips_missing_nights_and_foreign_names():
    assert rp.build_chunk_listing_payload(CAM, DATE, []) is None
    assert rp.build_chunk_listing_payload(CAM, DATE, None) is None
    odd = {"chunks": [{"filename": f"RO0001_{DATE}_183000_color.mkv"},
                      {"filename": f"{CAM}_{DATE}_183000_000020_color.mkv"}]}
    assert rp.build_chunk_listing_payload(CAM, DATE, odd)["chunks"] == []


class _Pub:
    """Just enough Publisher for push_chunk_listings."""

    def __init__(self, seq_path: Path) -> None:
        import threading
        self._seq = rp.SeqCounter(seq_path)
        self._key = "k"
        self._stop = threading.Event()

    def _url(self, endpoint: str) -> str:
        return f"https://vps/api/ingest/v1/gmn0002/{endpoint}"


def test_push_resends_a_night_only_when_it_changed(monkeypatch, tmp_path):
    listing = {"morning_done": False, "chunks": _station_chunks()}
    monkeypatch.setattr(rp, "fetch_camera_codes", lambda: [CAM])
    monkeypatch.setattr(rp, "fetch_nights", lambda cam, limit=None: [DATE])
    monkeypatch.setattr(rp, "fetch_chunk_listing", lambda cam, date: listing)
    posts: list[dict] = []

    def fake_post(url, key, body, timeout=rp.HTTP_TIMEOUT):
        posts.append(body)
        return rp.PostResult(True, 200)

    monkeypatch.setattr(rp, "post_json", fake_post)
    pub = _Pub(tmp_path / "seq")
    assert rp.push_chunk_listings(pub) is True
    assert rp.push_chunk_listings(pub) is True
    assert len(posts) == 1, "unchanged listing was re-sent"
    listing["chunks"] = listing["chunks"] + [dict(listing["chunks"][0], filename=f"{CAM}_{DATE}_183020_color.mkv")]
    assert rp.push_chunk_listings(pub) is True
    assert len(posts) == 2


# ── ingest route ────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _require_keys(monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    yield
    station_keys._invalidate_cache()


@pytest.fixture
def env(tmp_path: Path):
    keys_path = tmp_path / "station_keys.yaml"
    state_path = tmp_path / "state.db"
    _, secret = station_keys.add_key("gmn0002", path=keys_path)
    station_keys._invalidate_cache()
    owners = {"RO000M": {"gmn0002"}, "RO000E": {"gmnro10"}}
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app,
        cache_set_status=lambda *a, **k: None,
        cache_set_vitals=lambda *a, **k: None,
        known_stations=lambda: {"gmn0002", "gmnro10"},
        cam_owners=lambda cam: owners.get(cam, set()),
        detections_db_path=tmp_path / "detections.db",
        station_state_path=state_path,
        station_keys_path=keys_path,
        archive_media_path=tmp_path / "archive",
        limiter=None,
    )
    return app.test_client(), {"X-Station-Key": secret}, state_path


def _body(seq: int, payload: dict) -> dict:
    return {"seq": seq, "sent_at": "2026-11-01T22:00:00Z", "payload": payload}


def test_ingest_stores_a_listing_the_dashboard_reads_back(env):
    client, hdr, state_path = env
    payload = rp.build_chunk_listing_payload(
        CAM, DATE, {"morning_done": False, "chunks": _station_chunks()})
    r = client.post("/api/ingest/v1/gmn0002/chunks", json=_body(1, payload), headers=hdr)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["chunks"] == 3
    got = chunk_listings.get(CAM, DATE, path=state_path)
    assert got["host_key"] == "gmn0002"
    assert [c["filename"] for c in got["chunks"]] == [c["filename"] for c in _station_chunks()]
    assert chunk_listings.nights({CAM}, path=state_path) == [DATE]
    locks = chunk_listings.pushed_locks(DATE, path=state_path)[CAM]
    assert {e["lock_type"] for e in locks} == {"detection", "manual"}


def test_ingest_rejects_another_stations_camera_and_bad_keys(env):
    client, hdr, _ = env
    foreign = {"cam": "RO000E", "date": DATE, "chunks": []}
    assert client.post("/api/ingest/v1/gmn0002/chunks", json=_body(1, foreign),
                       headers=hdr).status_code == 403
    mine = {"cam": CAM, "date": DATE, "chunks": []}
    assert client.post("/api/ingest/v1/gmn0002/chunks", json=_body(2, mine),
                       headers={"X-Station-Key": "nope"}).status_code in (401, 403)
    bad_date = {"cam": CAM, "date": "2026-11-01", "chunks": []}
    assert client.post("/api/ingest/v1/gmn0002/chunks", json=_body(3, bad_date),
                       headers=hdr).status_code == 422



@pytest.mark.parametrize("field,value", [
    ("chunks", [[f"{DATE}_220000", 15.0, "<img src=x onerror=alert(1)>"]]),
    ("chunks", [["../../etc", 15.0]]),
    ("chunks", [[f"{DATE}_220000", "15MB"]]),
    ("chunks", [[f"{DATE}_220000", 15.0, "22:00:00", "extra"]]),
    ("locks", {f"{DATE}_220000": ["<script>", None, None]}),
    ("locks", {f"{DATE}_220000": ["detection", "<b>2026</b>", None]}),
    ("locks", {"not-a-stamp": ["manual", None, None]}),
    ("locks", {f"{DATE}_220000": ["detection", None, None, "x" * 100]}),
    ("reencoded", ["<svg/onload=1>"]),
])
def test_ingest_rejects_malformed_listing_elements(env, field, value):
    """The listing reaches the videodb and Events pages, so every element is
    typed at ingest: stamps, HH:MM:SS times, the two lock types, ISO instants."""
    client, hdr, state_path = env
    payload = {"cam": CAM, "date": DATE, "chunks": [], field: value}
    r = client.post("/api/ingest/v1/gmn0002/chunks", json=_body(1, payload), headers=hdr)
    assert r.status_code == 422, r.get_json()
    assert chunk_listings.get(CAM, DATE, path=state_path) is None


def test_ingest_accepts_real_station_lock_values(env):
    client, hdr, _ = env
    payload = {"cam": CAM, "date": DATE,
               "chunks": [[f"{DATE}_220000", 15.2, "22:00:01"], [f"{DATE}_220020", None]],
               "locks": {f"{DATE}_220000": ["detection", "2026-11-01T22:00:07.123456+00:00", 7.1],
                         f"{DATE}_220020": ["manual", None, None]},
               "reencoded": [f"{DATE}_220000"]}
    r = client.post("/api/ingest/v1/gmn0002/chunks", json=_body(1, payload), headers=hdr)
    assert r.status_code == 200, r.get_json()

def test_ingest_drops_a_replayed_seq(env):
    client, hdr, state_path = env
    new = {"cam": CAM, "date": DATE, "chunks": [[f"{DATE}_183000", 1.0], [f"{DATE}_183020", 1.0]]}
    old = {"cam": CAM, "date": DATE, "chunks": [[f"{DATE}_183000", 1.0]]}
    client.post("/api/ingest/v1/gmn0002/chunks", json=_body(5, new), headers=hdr)
    r = client.post("/api/ingest/v1/gmn0002/chunks", json=_body(4, old), headers=hdr)
    assert r.get_json()["dropped"] is True
    assert len(chunk_listings.get(CAM, DATE, path=state_path)["chunks"]) == 2


def test_old_listings_are_pruned_on_write(tmp_path):
    db = tmp_path / "s.db"
    old = (datetime.now(timezone.utc) - timedelta(days=chunk_listings.RETENTION_DAYS + 2)).strftime("%Y%m%d")
    chunk_listings.store("gmn0002", {"cam": CAM, "date": old, "chunks": []}, path=db)
    chunk_listings.store("gmn0002", {"cam": CAM, "date": DATE, "chunks": []}, path=db)
    assert old not in chunk_listings.nights(path=db)


def test_reads_are_empty_before_any_station_pushed(tmp_path):
    db = tmp_path / "absent.db"
    assert chunk_listings.get(CAM, DATE, path=db) is None
    assert chunk_listings.nights(path=db) == []
    assert chunk_listings.pushed_locks(DATE, path=db) == {}


# ── shared upload state ─────────────────────────────────────────────────


def test_upload_state_deadlines_per_kind():
    # Night 20261101 ends 20261102 00:00 UTC; plots are due 6 h later, every
    # other artifact at 14:00 UTC, after the 13:15 dawn deadline.
    at = lambda h: datetime(2026, 11, 2, h, 30, tzinfo=timezone.utc)
    assert upload_state.is_overdue(FIXED_NIGHT, "plot", at(5)) is False
    assert upload_state.is_overdue(FIXED_NIGHT, "plot", at(6)) is True
    assert upload_state.is_overdue(FIXED_NIGHT, "clip", at(13)) is False
    assert upload_state.is_overdue(FIXED_NIGHT, "clip", at(14)) is True
    st = upload_state.upload_state(FIXED_NIGHT, "timelapse", at(1))
    assert st == {"state": "not_uploaded_yet", "overdue": False, "due": "2026-11-02T14:00+00:00"}
    assert upload_state.upload_state("nonsense")["overdue"] is False


def test_not_uploaded_response_body():
    app = Flask(__name__)
    with app.app_context():
        resp, code = upload_state.not_uploaded_response(
            "clip", CAM, FIXED_NIGHT, "x.mkv", now=datetime(2026, 11, 3, tzinfo=timezone.utc))
    assert code == 404
    body = resp.get_json()
    assert body["error"] == "not_uploaded_yet"
    assert body["state"] == "overdue" and body["overdue"] is True
    assert "upload overdue" in body["detail"]
