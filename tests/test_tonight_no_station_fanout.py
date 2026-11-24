"""#1250 acceptance: tonight's Events and videodb pages make zero requests to
a station's port 7779, and what is not in the archive yet reads "not
uploaded yet".

Every request a page load makes is driven through the real app, with three
traps armed: the two ``station_get_raw`` imports the routes use and
``requests.Session.send`` for any URL on port 7779. Tonight's state is what a
station pushes: a chunk listing with one detection lock.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
import requests

from tests.test_detections_range_no_live import admin, app_env  # noqa: F401

CAM = "RO0017"
HOST = "teststation"


@pytest.fixture
def tonight(monkeypatch, tmp_path):
    import chunk_listings
    import routes.detections as det_routes
    import routes.media as media_routes
    import station_state

    station_calls: list[str] = []

    def trap(config, tunnels, host_key, path, *a, **kw):
        station_calls.append(path)
        raise AssertionError(f"station API called: {path}")

    monkeypatch.setattr(det_routes, "station_get_raw", trap)
    monkeypatch.setattr(media_routes, "station_get_raw", trap, raising=False)
    real_send = requests.Session.send

    def guarded_send(self, req, **kw):
        if ":7779" in (req.url or ""):
            station_calls.append(req.url)
            raise AssertionError(f"HTTP to a station: {req.url}")
        return real_send(self, req, **kw)

    monkeypatch.setattr(requests.Session, "send", guarded_send)

    db = tmp_path / "station_state.db"
    monkeypatch.setattr(station_state, "DB_PATH", db)
    monkeypatch.setattr(chunk_listings, "_schema_ready", set())
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    iso = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    chunk_listings.store(HOST, {
        "cam": CAM, "date": today, "morning_done": False,
        "chunks": [[f"{today}_{t}", 9.5] for t in ("000000", "000020", "000040")],
        "locks": {f"{today}_000020": ["detection", f"{iso} 00:00:31.4", 11.4]},
        "reencoded": [],
    }, path=db)
    yield today, station_calls


# Online first: the detections and footage payloads are cached per app, and
# the online station is the case that used to fan out.
@pytest.fixture(params=[True, False], ids=["online", "offline"])
def status(request, monkeypatch):
    import station_client
    online = request.param
    monkeypatch.setattr(
        station_client.StationCache, "get_status",
        lambda self, hk: {"online": online},
    )
    return online


def test_events_page_tonight(admin, tonight, status):  # noqa: F811
    today, calls = tonight
    nights = admin.get("/api/detections/nights")
    assert nights.status_code == 200
    assert today in nights.get_json()

    det = admin.get(f"/api/detections/{today}")
    assert det.status_code == 200
    chunks = det.get_json()["by_station"][HOST]["cameras"][CAM]
    (marker,) = [c for c in chunks if c.get("chunk_file") == f"{CAM}_{today}_000020_color.mkv"]
    assert marker["pending"] is True
    assert marker["upload_state"] == "not_uploaded_yet"
    assert marker["overdue"] is False
    assert marker["meteor_time"].endswith("00:00:31.4")
    flat = det.get_json()["detections"]
    assert any(d["pending"] and not d["overdue"] for d in flat)

    assert admin.get(f"/api/detections/grid/{today}").status_code == 200

    grid = admin.get(f"/api/footage/grid/{today}?from=00:00&to=00:10")
    assert grid.status_code == 200
    body = grid.get_json()
    assert body["degraded"] is False
    cells = [c for row in body["rows"] for cell in row.get("cells", {}).values()
             for c in cell["chunks"]]
    assert {c["filename"] for c in cells} >= {f"{CAM}_{today}_000000_color.mkv"}
    assert calls == []


def test_videodb_page_tonight(admin, tonight, status):  # noqa: F811
    today, calls = tonight
    nights = admin.get(f"/api/videodb/nights/{HOST}/{CAM}")
    assert nights.status_code == 200 and today in nights.get_json()

    chunks = admin.get(f"/api/videodb/chunks/{HOST}/{CAM}/{today}?locked_only=0")
    assert chunks.status_code == 200
    assert [c["time"] for c in chunks.get_json()] == ["00:00:00", "00:00:20", "00:00:40"]

    rms = admin.get(f"/api/videodb/rms-detections/{HOST}/{CAM}/{today}")
    assert rms.status_code == 200

    thumb = admin.get(f"/stack/{HOST}/{CAM}/{today}/{CAM}_{today}_000000_stack.webp")
    assert thumb.status_code == 404
    assert thumb.get_json()["error"] == "not_uploaded_yet"
    assert calls == []


def test_clip_not_in_archive_reads_not_uploaded_yet_when_offline(admin, tonight, monkeypatch):  # noqa: F811
    import station_client
    monkeypatch.setattr(station_client.StationCache, "get_status",
                        lambda self, hk: {"online": False})
    today, calls = tonight
    r = admin.get(f"/video/{HOST}/{CAM}/{today}/{CAM}_{today}_000020_color.mkv")
    assert r.status_code == 404
    body = r.get_json()
    assert body["error"] == "not_uploaded_yet"
    assert body["kind"] == "clip"
    tl = admin.get(f"/timelapse/{HOST}/{CAM}/{today}/{CAM}_{today}_timelapse.mp4")
    assert tl.status_code == 404
    assert tl.get_json()["kind"] == "timelapse"
    assert calls == []
