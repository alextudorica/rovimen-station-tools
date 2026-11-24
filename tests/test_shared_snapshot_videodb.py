"""The camera tab's recent-night videodb payloads come from pushed state.

Until #1250, /api/videodb/chunks and /api/videodb/rms-detections called the
station for any night younger than two days, behind a stale-while-revalidate
cache shared across gunicorn workers (2026-09-25: 1.1-1.4 s per cold chunk
list, ~160 ms per RMS call). Both now read VPS-local state the stations push:
the chunk listing (``chunk_listings``) and the detection index
(``detection_db``). These tests run two apps (as two workers) with a station
that fails the test if it is ever called.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

pytest.importorskip("fakeredis")

from tests.test_shared_snapshot_wiring import _admin, _as_worker, two_workers  # noqa: E402,F401

TODAY = datetime.now(timezone.utc).strftime("%Y%m%d")
ISO_DAY = f"{TODAY[:4]}-{TODAY[4:6]}-{TODAY[6:]}"


@pytest.fixture
def no_station(monkeypatch, tmp_path):
    """No station may be contacted; pushed listings and detection rows live
    in tmp state."""
    import chunk_listings
    import detection_db
    import routes.media as media_routes
    import station_client
    import station_state

    monkeypatch.setattr(
        station_client.StationCache, "get_status", lambda self, hk: {"online": True},
    )

    def boom(config, tunnels, host_key, path, timeout=None, **kw):
        raise AssertionError(f"station called: {path}")

    monkeypatch.setattr(media_routes, "station_get_raw", boom, raising=False)

    db = tmp_path / "station_state.db"
    monkeypatch.setattr(station_state, "DB_PATH", db)
    monkeypatch.setattr(chunk_listings, "_schema_ready", set())
    chunk_listings.store("alpha", {
        "cam": "RO0001", "date": TODAY, "morning_done": False,
        "chunks": [[f"{TODAY}_010000", 8.0], [f"{TODAY}_010020", 8.1]],
        "locks": {f"{TODAY}_010000": ["detection", f"{ISO_DAY} 01:00:05.0", 5.0]},
        "reencoded": [],
    }, path=db)

    rows = {
        "RO0002": [{"cam": "RO0002", "date": TODAY, "ff_file": "FF_x.fits",
                    "meteor_no": 1, "time_utc": f"{ISO_DAY}T01:00:05",
                    "mag_apparent": -1.2}],
    }

    def fake_query(dates, cam_filter=None, **kw):
        return [r for cam in (cam_filter or rows) for r in rows.get(cam, [])
                if r["date"] in dates]

    monkeypatch.setattr(detection_db, "query_detections", fake_query)


def test_both_workers_serve_the_pushed_chunk_list(two_workers, no_station, monkeypatch):
    worker_a, worker_b, _ = two_workers
    url = f"/api/videodb/chunks/alpha/RO0001/{TODAY}?locked_only=1"
    first = _admin(_as_worker(worker_a, monkeypatch)).get(url)
    assert first.status_code == 200
    assert [c["filename"] for c in first.get_json()] == [f"RO0001_{TODAY}_010000_color.mkv"]
    second = _admin(_as_worker(worker_b, monkeypatch)).get(url)
    assert second.get_json() == first.get_json()


def test_rms_detections_come_from_the_detection_index(two_workers, no_station, monkeypatch):
    worker_a, worker_b, _ = two_workers
    url = f"/api/videodb/rms-detections/beta/RO0002/{TODAY}"
    first = _admin(_as_worker(worker_a, monkeypatch)).get(url)
    assert first.status_code == 200
    body = first.get_json()
    assert body["count"] == 1
    row = body["detections"][0]
    assert row["mag_apparent"] == -1.2
    # Rows from this young a night are labelled, so the panel can say "not
    # triangulated (yet)" instead of leaving provenance undefined.
    assert row["provenance"] == "single_station"
    assert _admin(_as_worker(worker_b, monkeypatch)).get(url).get_json() == body


def test_rms_detections_fall_back_to_the_archive_without_rows(two_workers, no_station,
                                                             monkeypatch):
    worker_a, _, _ = two_workers
    r = _admin(_as_worker(worker_a, monkeypatch)).get(f"/api/videodb/rms-detections/alpha/RO0001/{TODAY}")
    assert r.status_code == 200
    assert r.get_json()["detections"] == []
