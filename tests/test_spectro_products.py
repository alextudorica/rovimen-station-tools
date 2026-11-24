"""Spectroscopy products surface: /api/spectro listing + archive file serving.

The spectro pipeline (deployed 2026-09-16) wrote its products only to the
station's ~/spectro_events — nothing reached the dashboard. The station now
uploads each event's small artifacts to ``<cam>/<night>/spectro/`` and the
Final Data Products tab lists them through /api/spectro/<host>.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from typing import TYPE_CHECKING

import pytest
import yaml

if TYPE_CHECKING:
    from routes.archive import ArchiveDeps

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

SPECTRO_CAM = "GH662S"
PLAIN_CAM = "TST001"
EV = "FF_RO000A_20260916_181303"


def _ymd(days_ago: int) -> str:
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).strftime("%Y%m%d")


NIGHT = _ymd(1)


@pytest.fixture
def archive(monkeypatch):
    root = Path(tempfile.mkdtemp(prefix="rovimen_spectro_test_"))
    sp = root / SPECTRO_CAM / NIGHT / "spectro"
    sp.mkdir(parents=True)
    (sp / f"{EV}__event.json").write_text('{"event_id": "%s"}' % EV)
    (sp / f"{EV}__reduction.json").write_text("{}")
    (sp / f"{EV}__spectrum.csv").write_text("wavelength_nm,flux\n589.3,1.0\n")
    (sp / f"{EV}__spectrum.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
    (sp / "not_an_event_artifact.txt").write_text("ignored: no __ separator")

    import cache_store
    import routes.archive as ra
    monkeypatch.setattr(cache_store, "ARCHIVE_PATH", root)
    monkeypatch.setattr(ra, "ARCHIVE_PATH", root)
    return root


@pytest.fixture
def app(archive):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_spectro_app_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "spectro-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["ROVIMEN_DISABLE_BACKGROUND"] = "1"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(archive)
    os.environ["ROVIMEN_GMN_CACHE_DIR"] = str(tmp / "gmn_cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")

    import security
    original_init_limiter = security.init_limiter

    def _noop_limiter(a):
        lim = original_init_limiter(a)
        lim.enabled = False
        return lim
    security.init_limiter = _noop_limiter

    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [
                    {"code": PLAIN_CAM, "label": "N", "cam_ip": "127.0.0.1",
                     "rotate": False, "az": 0.0, "alt": 45.0},
                    {"code": SPECTRO_CAM, "label": "Spectro", "cam_ip": "127.0.0.1",
                     "rotate": False, "spectro": True},
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Alpha",
                "show_on_map": True, "status": "active",
            },
            "teststn2": {
                "cameras": [
                    {"code": "TST004", "label": "N", "cam_ip": "127.0.0.1",
                     "rotate": False},
                ],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Beta",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Beta",
                "show_on_map": True, "status": "active",
            },
        },
    }
    cfg_path = tmp / "dashboard_config.yaml"
    cfg_path.write_text(yaml.dump(config_data))

    from rovimen_dashboard import load_config, create_app
    config = load_config(cfg_path)
    flask_app = create_app(config, cfg_path)
    flask_app.config["TESTING"] = True

    import rovimen_dashboard as rd
    with rd._archive_idx._lock:
        rd._archive_idx._dates.setdefault(SPECTRO_CAM, set()).add(NIGHT)
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


def test_spectro_listing_groups_artifacts_by_event(client):
    r = client.get("/api/spectro/teststn1")
    assert r.status_code == 200
    j = r.get_json()
    assert j["spectro"] is True
    cams = {c["cam"]: c for c in j["cameras"]}
    assert list(cams) == [SPECTRO_CAM], "only spectro-flagged cameras are listed"
    nights = cams[SPECTRO_CAM]["nights"]
    assert nights and nights[0]["date"] == NIGHT
    (event,) = nights[0]["events"]
    assert event["id"] == EV
    assert event["files"] == ["event.json", "reduction.json",
                              "spectrum.csv", "spectrum.png"]


def test_station_without_spectro_cameras_says_so(client):
    r = client.get("/api/spectro/teststn2")
    assert r.status_code == 200
    assert r.get_json() == {"spectro": False, "cameras": []}


def test_spectro_artifacts_are_served_from_the_archive(client, archive):
    # The preview frames are JPEG; the archive file route used to allow only
    # mkv/mp4/webp/txt/json/png/csv, so every per-event preview 400'd.
    (archive / SPECTRO_CAM / NIGHT / "spectro" / f"{EV}__wide.jpg").write_bytes(
        b"\xff\xd8\xff\xe0fake")
    base = f"/api/archive/file/{SPECTRO_CAM}/{NIGHT}/spectro/{EV}__"
    for name, sniff in (("spectrum.csv", b"wavelength_nm"),
                        ("spectrum.png", b"\x89PNG"),
                        ("reduction.json", b"{}"),
                        ("wide.jpg", b"\xff\xd8\xff")):
        r = client.get(base + name)
        assert r.status_code == 200, name
        assert r.data.startswith(sniff), name


def test_camera_spectro_flag_round_trips_the_admin_serializer():
    from models import CameraConfig

    assert CameraConfig(code="X1", cam_ip="1.2.3.4").spectro is False
    assert CameraConfig(code="X1", cam_ip="1.2.3.4", spectro=True).spectro is True


# ── Listing failures must not read as "no events" ────────────────────────
# A night's listing goes through the shed-protected SSHFS wrapper, which
# returns its default when the lane is saturated. That default used to be
# indistinguishable from an empty archive: the night was dropped and the tab
# rendered "no spectroscopy events yet". On 2026-09-19 gmnro01's FDP tab showed
# exactly that while 264 artifacts sat on the box, because the 28 listings a
# cold build ran (14 nights x 2 cameras, 26 of them misses) overran nginx's
# 30 s cap and returned 504.

def _spectro_caches() -> ArchiveDeps:
    """The /api/spectro caches of the app built last (``routes.archive.ArchiveDeps``)."""
    import routes.archive as ra
    return ra._deps()


def _index_nights(cam: str, *dates: str) -> None:
    import rovimen_dashboard as rd
    with rd._archive_idx._lock:
        rd._archive_idx._dates.setdefault(cam, set()).update(dates)


def test_shed_listing_is_reported_as_partial_not_empty(app, client, monkeypatch):
    import routes.archive as ra
    monkeypatch.setattr(ra, "_with_sshfs_timeout",
                        lambda fn, timeout=5.0, default=None: default)
    _spectro_caches().spectro_cache.clear()

    j = client.get("/api/spectro/teststn1").get_json()

    assert j["spectro"] is True
    assert j["partial"] is True, "a listing that never came back must say so"
    assert j["cameras"][0]["nights"] == []
    assert not _spectro_caches().spectro_miss, "a shed listing is unknown, not an empty night"


def test_partial_listings_are_not_cached_over_the_real_thing(app, client, monkeypatch):
    import routes.archive as ra
    real = ra._with_sshfs_timeout
    monkeypatch.setattr(ra, "_with_sshfs_timeout",
                        lambda fn, timeout=5.0, default=None: default)
    _spectro_caches().spectro_cache.clear()
    assert client.get("/api/spectro/teststn1").get_json()["partial"] is True

    # The partial entry carries a seconds-long TTL, not the full 300 s, so the
    # next load retries rather than serving the empty list for five minutes.
    monkeypatch.setattr(ra, "_with_sshfs_timeout", real)
    _spectro_caches().spectro_cache.clear()
    j = client.get("/api/spectro/teststn1").get_json()
    assert j["partial"] is False
    assert j["cameras"][0]["nights"][0]["date"] == NIGHT


def test_settled_empty_nights_are_listed_once_then_remembered(app, client, monkeypatch):
    import routes.archive as ra
    old_a, old_b = _ymd(6), _ymd(7)
    _index_nights(SPECTRO_CAM, old_a, old_b)

    real = ra._with_sshfs_timeout
    calls: list[str] = []

    def counting(fn, timeout=5.0, default=None):
        calls.append("x")
        return real(fn, timeout=timeout, default=default)

    monkeypatch.setattr(ra, "_with_sshfs_timeout", counting)
    _spectro_caches().spectro_cache.clear()
    _spectro_caches().spectro_miss.clear()

    first = client.get("/api/spectro/teststn1").get_json()
    assert first["partial"] is False
    n_first = len(calls)
    assert n_first >= 3, "cold build lists every indexed night"
    assert (SPECTRO_CAM, old_a) in _spectro_caches().spectro_miss
    assert (SPECTRO_CAM, old_b) in _spectro_caches().spectro_miss

    calls.clear()
    _spectro_caches().spectro_cache.clear()     # force a rebuild, keep the miss cache
    second = client.get("/api/spectro/teststn1").get_json()

    assert second["cameras"] == first["cameras"], "same answer, fewer listings"
    assert len(calls) < n_first, "settled empty nights are not re-listed"
    listed = {c for c in (old_a, old_b)}
    assert all((SPECTRO_CAM, d) in _spectro_caches().spectro_miss for d in listed), \
        "the misses that saved those listings are still held"


def test_recent_empty_nights_keep_being_rechecked(app, client, monkeypatch):
    """Tonight's dir is empty until the dawn run uploads; it must not be
    remembered for a day."""
    import time as _time
    import routes.archive as ra
    today = _ymd(0)
    _index_nights(SPECTRO_CAM, today)
    _spectro_caches().spectro_cache.clear()
    _spectro_caches().spectro_miss.clear()

    client.get("/api/spectro/teststn1")

    exp = _spectro_caches().spectro_miss.get((SPECTRO_CAM, today))
    assert exp is not None
    assert exp - _time.time() <= 600, "a recent night is re-checked, not pinned for a day"


# ── Per-night index.json ────────────────────────────────────────────────────
# The station uploads one summary per night so the tab can badge each event
# (trigger, outcome, time) without the dashboard opening two JSONs per event —
# 76 SSHFS reads for a 38-event night.

def _write_index(archive, events, night=NIGHT, cam=SPECTRO_CAM):
    import json as _json
    (archive / cam / night / "spectro" / "index.json").write_text(
        _json.dumps({"night": night, "spectro_code": cam,
                     "counts": {}, "events": events}))


def test_index_metadata_reaches_the_payload(app, archive, client):
    _write_index(archive, [{"id": EV, "time": "18:13:03", "trigger": "widefield",
                            "source": "GH662W", "state": "spectrum", "score": 9.9}])
    _spectro_caches().spectro_cache.clear()

    (night,) = client.get("/api/spectro/teststn1").get_json()["cameras"][0]["nights"]
    (event,) = night["events"]

    assert event["trigger"] == "widefield"
    assert event["state"] == "spectrum"
    assert event["time"] == "18:13:03"
    assert event["score"] == 9.9
    assert night["counts"]["spectra"] == 1


def test_index_is_not_listed_as_an_event(app, archive, client):
    _write_index(archive, [])
    _spectro_caches().spectro_cache.clear()

    (night,) = client.get("/api/spectro/teststn1").get_json()["cameras"][0]["nights"]

    assert [e["id"] for e in night["events"]] == [EV], \
        "index.json carries no '__' and must never group as an event"


def test_index_cannot_invent_events_that_are_not_on_the_archive(app, archive, client):
    _write_index(archive, [{"id": "GHOST_20260101_000000", "state": "spectrum"},
                           {"id": EV, "state": "transient"}])
    _spectro_caches().spectro_cache.clear()

    (night,) = client.get("/api/spectro/teststn1").get_json()["cameras"][0]["nights"]

    assert [e["id"] for e in night["events"]] == [EV]
    assert night["counts"]["events"] == 1


def test_a_malformed_index_falls_back_to_what_the_files_say(app, archive, client):
    (archive / SPECTRO_CAM / NIGHT / "spectro" / "index.json").write_text("{nope")
    _spectro_caches().spectro_cache.clear()

    (night,) = client.get("/api/spectro/teststn1").get_json()["cameras"][0]["nights"]
    (event,) = night["events"]

    assert event["state"] == "spectrum", "derived from spectrum.png being present"
    assert event["trigger"] == "rms"
    assert event["time"] == "18:13:03"
    assert night["counts"] == {"events": 1, "detected": 1, "spectra": 1,
                               "rms": 1, "widefield": 0}


def test_nights_without_an_index_still_get_state_and_counts(app, client):
    """Nights the station produced before it wrote an index — the metadata is
    derived from the filenames so old nights render in the same tab."""
    j = client.get("/api/spectro/teststn1").get_json()
    (night,) = j["cameras"][0]["nights"]

    assert night["events"][0]["state"] == "spectrum"
    assert night["counts"]["events"] == 1


def _archive_root(app) -> "Path":
    import routes.archive as ra
    return Path(ra.ARCHIVE_PATH)


def test_settled_nights_with_products_are_listed_once_then_held(app, client, monkeypatch):
    """A settled night's products never change after its dawn run, so a rebuild
    must not re-list it (2026-09-23: re-listing every night every 5 min queued
    the walk behind another tab's fan-out until nginx 504ed it)."""
    import routes.archive as ra
    old = _ymd(6)
    d = _archive_root(app) / SPECTRO_CAM / old / "spectro"
    d.mkdir(parents=True, exist_ok=True)
    (d / "EVX_20260917_010203__wide.jpg").write_bytes(b"x")
    _index_nights(SPECTRO_CAM, old)

    real = ra._with_sshfs_timeout
    listed: list[str] = []

    def counting(fn, timeout=5.0, default=None):
        listed.append("x")
        return real(fn, timeout=timeout, default=default)

    monkeypatch.setattr(ra, "_with_sshfs_timeout", counting)
    caches = _spectro_caches()
    caches.spectro_cache.clear(); caches.spectro_miss.clear(); caches.spectro_hits.clear()
    first = client.get("/api/spectro/teststn1").get_json()
    assert any(n["date"] == old for n in first["cameras"][0]["nights"])
    assert (SPECTRO_CAM, old) in _spectro_caches().spectro_hits
    n_first = len(listed)

    listed.clear(); _spectro_caches().spectro_cache.clear()
    second = client.get("/api/spectro/teststn1").get_json()
    assert second["cameras"] == first["cameras"]
    assert len(listed) < n_first, "the settled night came from the hit cache"


def test_build_budget_reports_partial_instead_of_timing_out(app, client, monkeypatch):
    import routes.archive as ra
    import time as _time
    _index_nights(SPECTRO_CAM, _ymd(8), _ymd(9))
    caches = _spectro_caches()
    caches.spectro_cache.clear(); caches.spectro_miss.clear(); caches.spectro_hits.clear()
    # Every monotonic read after the first is past any budget.
    t = iter([0.0] + [1e9] * 1000)
    monkeypatch.setattr(ra.time, "monotonic", lambda: next(t))
    j = client.get("/api/spectro/teststn1").get_json()
    assert j["partial"] is True
    assert j["cameras"][0]["nights"] == []
