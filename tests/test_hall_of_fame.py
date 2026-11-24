"""Hall-of-fame store + routes: curation write-path auth, anon read
filtering, public API projection, and the /api/live-feed anon fix.

Follows the real-create_app pattern from ``test_public_page_toggles.py`` so
the genuine auth gate + decorators + config model are exercised.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ── Store unit tests (no app) ───────────────────────────────────────────


@pytest.fixture
def hof_store(tmp_path):
    import hall_of_fame_store as store

    orig_path, orig_lock = store._PATH, store._LOCK_PATH
    store._PATH = tmp_path / "hall_of_fame.json"
    store._LOCK_PATH = tmp_path / "hof.lock"
    yield store
    store._PATH, store._LOCK_PATH = orig_path, orig_lock


def _entry(i: int, **over) -> dict:
    base = {
        "id": f"hof_{i:012d}",
        "camera": "PUB001",
        "date": "2026-07-01",
        "filename": f"PUB001_20260701_22000{i}_color.mkv",
        "order": i,
        "added_at": f"2026-07-0{min(i + 1, 9)}T00:00:00+00:00",
    }
    base.update(over)
    return base


class TestStore:
    def test_load_all_is_newest_first_rolling_window(self, hof_store):
        # _entry(i) has added_at = 2026-07-0{i+1}, so higher i == newer.
        hof_store.add(_entry(1))   # 2026-07-02
        hof_store.add(_entry(3))   # 2026-07-04 (newest)
        hof_store.add(_entry(2))   # 2026-07-03
        # Newest-added first — the landing draws its rolling window off the front.
        assert [e["id"] for e in hof_store.load_all()] == [
            "hof_000000000003", "hof_000000000002", "hof_000000000001",
        ]

    def test_add_duplicate_clip_raises(self, hof_store):
        hof_store.add(_entry(1))
        with pytest.raises(ValueError):
            hof_store.add(_entry(2, filename=_entry(1)["filename"]))

    def test_patch_and_delete(self, hof_store):
        hof_store.add(_entry(1))
        assert hof_store.patch("hof_000000000001", {"caption": "Wow"})["caption"] == "Wow"
        assert hof_store.patch("missing", {"caption": "x"}) is None
        assert hof_store.delete("hof_000000000001") is True
        assert hof_store.delete("hof_000000000001") is False
        assert hof_store.load_all() == []

    def test_find_by_clip(self, hof_store):
        e = _entry(1)
        hof_store.add(e)
        assert hof_store.find_by_clip(e["camera"], e["date"], e["filename"])["id"] == e["id"]
        assert hof_store.find_by_clip("OTHER0", e["date"], e["filename"]) is None

    def test_corrupt_file_recovers_empty(self, hof_store):
        hof_store._PATH.write_text("{not json")
        assert hof_store.load_all() == []


# ── App factory (real create_app; pattern from test_public_page_toggles) ──


def _build_app(public_pages=("overview", "live")):
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_hof_"))

    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "admin":  {"display_name": "Admin",  "password_hash": "",
                   "role": "admin",   "require_totp": False},
        "viewer": {"display_name": "Viewer", "password_hash": "",
                   "role": "visitor", "require_totp": False},
    }))

    cfg = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "public_pages": list(public_pages),
        "stations": {
            "pub_station": {
                "ip": "127.0.0.1", "label": "Public Station",
                "ssh_user": "test", "public": True,
                "lat": 45.0, "lon": 25.0,
                "cameras": [{"code": "PUB001", "cam_ip": "127.0.0.1",
                             "label": "N", "rotate": False}],
            },
            "priv_station": {
                "ip": "127.0.0.2", "label": "Private Station",
                "ssh_user": "test", "public": False,
                "lat": 46.0, "lon": 26.0,
                "cameras": [{"code": "PRIV01", "cam_ip": "127.0.0.2",
                             "label": "S", "rotate": False}],
            },
            # No cameras + commissioning: must never surface on the live
            # grid (would render as a permanent "Station offline" card).
            "comm_station": {
                "ip": "127.0.0.3", "label": "Commissioning Station",
                "ssh_user": "test", "public": True,
                "status": "commissioning",
                "lat": 47.0, "lon": 27.0,
                "cameras": [],
            },
        },
    }
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))

    os.environ["ROVIMEN_USERS_PATH"] = str(tmp / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "hof-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_COMPILATIONS_OUT_PATH"] = str(tmp / "compilations")
    os.environ["ROVIMEN_KNOWN_HOSTS"] = str(tmp / "known_hosts")
    os.environ["ROVIMEN_AUDIT_LOG_PATH"] = str(tmp / "audit.log")
    os.environ["ROVIMEN_ACTIVITY_LOG_PATH"] = str(tmp / "activity.log")
    os.environ["ROVIMEN_STATION_STATE_DB"] = str(tmp / "station_state.db")
    os.environ["ROVIMEN_DETECTIONS_DB"] = str(tmp / "detections.db")
    os.environ["ROVIMEN_SHOWER_COUNTS_PATH"] = str(tmp / "shower_counts.json")

    import security

    _saved_threshold = security._LOGIN_THRESHOLD
    _saved_lockout = security._LOGIN_LOCKOUT_SECONDS
    _saved_init_limiter = security.init_limiter
    security._LOGIN_THRESHOLD = 10_000
    security._LOGIN_LOCKOUT_SECONDS = 0

    def _noop_limiter(app):
        lim = _saved_init_limiter(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter

    from rovimen_dashboard import load_config, create_app

    config = load_config(config_path)
    app = create_app(config, config_path)
    app.config["TESTING"] = True

    security._LOGIN_THRESHOLD = _saved_threshold
    security._LOGIN_LOCKOUT_SECONDS = _saved_lockout
    security.init_limiter = _saved_init_limiter
    return app, tmp


def _logged_in(app, user="admin", role="admin"):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = user
        sess["role"] = role
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


@pytest.fixture
def hof_app(hof_store):
    app, tmp = _build_app()
    yield app, tmp, hof_store


def _seed(store, camera="PUB001", i=1, **over):
    store.add(_entry(i, camera=camera, added_by="admin", **over))


# ── Read surface ───────────────────────────────────────────────────────


class TestReadSurface:
    def test_anon_sees_only_public_cameras_and_no_curator(self, hof_app):
        app, _, store = hof_app
        _seed(store, "PUB001", 1)
        _seed(store, "PRIV01", 2)

        anon = app.test_client().get("/api/hall_of_fame")
        assert anon.status_code == 200
        entries = anon.get_json()["entries"]
        assert [e["camera"] for e in entries] == ["PUB001"]
        assert "added_by" not in entries[0]
        assert entries[0]["clip_url"].startswith("/media/v1/clip/PUB001/")
        # MKV clips advertise the remuxed MP4 variant for Safari/iOS.
        assert entries[0]["clip_mp4_url"].endswith("?format=mp4")
        # The station key lets the landing link the station and camera.
        assert entries[0]["host_key"] == "pub_station"

        user = _logged_in(app).get("/api/hall_of_fame")
        got = user.get_json()["entries"]
        assert {e["camera"] for e in got} == {"PUB001", "PRIV01"}
        assert got[0]["added_by"] == "admin"

    def test_anon_gated_when_overview_toggled_off(self, hof_store):
        app, _ = _build_app(public_pages=("live",))
        assert app.test_client().get("/api/hall_of_fame").status_code in (302, 401)
        assert _logged_in(app).get("/api/hall_of_fame").status_code == 200

    def test_stack_url_derived_when_entry_saved_without_stack(self, hof_app):
        # Clips featured from the multi-station event view arrive with no
        # stack field; the thumbnail must still resolve from the clip name.
        app, _, store = hof_app
        _seed(store, "PUB001", 1)   # _entry() sets no "stack"
        got = _logged_in(app).get("/api/hall_of_fame").get_json()["entries"][0]
        assert got["filename"] == "PUB001_20260701_220001_color.mkv"
        assert got["stack_url"] == (
            "/media/v1/stack/PUB001/2026-07-01/PUB001_20260701_220001_stack.webp")


# ── Thumbnail fallback (/media/v1/stack) ───────────────────────────────

_STACK_URL = "/media/v1/stack/PUB001/2026-07-01/PUB001_20260701_220001_stack.webp"


@pytest.fixture
def media_dirs(hof_app, monkeypatch, tmp_path):
    """Point the archive and the cold thumb archive at empty temp trees."""
    import cache_store
    import rovimen_dashboard as rd

    archive, thumbs = tmp_path / "archive", tmp_path / "thumb_archive"
    archive.mkdir()
    thumbs.mkdir()
    monkeypatch.setattr(rd, "ARCHIVE_PATH", archive)
    monkeypatch.setattr(cache_store, "THUMB_ARCHIVE_DIR", thumbs)
    return hof_app[0], archive, thumbs


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


class TestStackThumbnailFallback:
    """A featured clip whose stack never reached the archive (station with the
    colour stacker off, e.g. gmnro16 Bacău) or whose night ``stacks/`` was
    pruned must still get a thumbnail instead of a blank card."""

    def test_ffthumb_served_when_station_uploads_no_stacks(self, media_dirs):
        app, archive, _ = media_dirs
        _write(archive / "PUB001/20260701/ffthumbs/PUB001_20260701_220001.webp", b"FF")
        resp = app.test_client().get(_STACK_URL)
        assert resp.status_code == 200
        assert resp.data == b"FF"
        assert resp.headers["Cache-Control"] == "public, max-age=300"

    def test_cold_thumb_archive_served_after_stacks_pruned(self, media_dirs):
        app, _, thumbs = media_dirs
        _write(thumbs / "pub_station/PUB001/20260701/PUB001_20260701_220001_stack.webp", b"COLD")
        resp = app.test_client().get(_STACK_URL)
        assert resp.status_code == 200
        assert resp.data == b"COLD"

    def test_archived_stack_wins_over_fallbacks(self, media_dirs):
        app, archive, thumbs = media_dirs
        _write(archive / "PUB001/20260701/stacks/PUB001_20260701_220001_stack.webp", b"STACK")
        _write(archive / "PUB001/20260701/ffthumbs/PUB001_20260701_220001.webp", b"FF")
        _write(thumbs / "pub_station/PUB001/20260701/PUB001_20260701_220001_stack.webp", b"COLD")
        resp = app.test_client().get(_STACK_URL)
        assert resp.data == b"STACK"
        assert "immutable" in resp.headers["Cache-Control"]

    def test_still_404_when_nothing_exists(self, media_dirs):
        app, _, _ = media_dirs
        assert app.test_client().get(_STACK_URL).status_code == 404

    def test_private_camera_ffthumb_stays_hidden(self, media_dirs):
        app, archive, _ = media_dirs
        _write(archive / "PRIV01/20260701/ffthumbs/PRIV01_20260701_220001.webp", b"FF")
        resp = app.test_client().get(
            "/media/v1/stack/PRIV01/2026-07-01/PRIV01_20260701_220001_stack.webp")
        assert resp.status_code == 404


# ── Write surface ──────────────────────────────────────────────────────


def _clip_payload(camera="PUB001"):
    return {
        "camera": camera,
        "date": "2026-07-05",
        "filename": f"{camera}_20260705_221530_color.mkv",
        "stack": f"{camera}_20260705_221530_stack.webp",
        "caption": "Bright fireball",
        "shower": "per",
        "peak_magnitude": -4.2,
        "duration_s": 2.4,
        "time_utc": "2026-07-05T22:15:33",
        "station_label": "Public Station",
        "detection_offset_s": 12.3,
    }


def _make_archive_clip(archive: Path, payload: dict) -> None:
    d = archive / payload["camera"] / payload["date"].replace("-", "") / "meteors"
    d.mkdir(parents=True, exist_ok=True)
    (d / payload["filename"]).write_bytes(b"fake mkv")


class TestWriteSurface:
    def test_anon_and_non_admin_cannot_write(self, hof_app):
        app, _, _ = hof_app
        assert app.test_client().post(
            "/api/hall_of_fame", json=_clip_payload()).status_code in (302, 401)
        assert _logged_in(app, "viewer", "visitor").post(
            "/api/hall_of_fame", json=_clip_payload()).status_code == 403
        assert _logged_in(app, "viewer", "visitor").delete(
            "/api/hall_of_fame/x").status_code == 403

    def test_admin_roundtrip_add_patch_delete(self, hof_app, tmp_path):
        app, _, store = hof_app
        import rovimen_dashboard as rd

        payload = _clip_payload()
        archive = tmp_path / "archive"
        _make_archive_clip(archive, payload)
        admin = _logged_in(app)

        with patch.object(rd, "ARCHIVE_PATH", archive):
            created = admin.post("/api/hall_of_fame", json=payload)
            assert created.status_code == 201
            entry = created.get_json()
            assert entry["shower"] == "PER"
            assert entry["period"] == "2026-07"      # defaulted from date
            assert entry["added_by"] == "admin"

            # Double-click safety: identical clip refuses with 409.
            assert admin.post("/api/hall_of_fame",
                              json=payload).status_code == 409

        patched = admin.patch(f"/api/hall_of_fame/{entry['id']}",
                              json={"caption": "Renamed", "order": 5})
        assert patched.status_code == 200
        assert patched.get_json()["caption"] == "Renamed"
        assert patched.get_json()["order"] == 5

        assert admin.patch(f"/api/hall_of_fame/{entry['id']}",
                           json={}).status_code == 400
        # json={} sets Content-Type: application/json, which the interim
        # csrf-origin guard requires on headerless (non-browser) mutations.
        assert admin.delete(f"/api/hall_of_fame/{entry['id']}",
                            json={}).status_code == 200
        assert admin.delete(f"/api/hall_of_fame/{entry['id']}",
                            json={}).status_code == 404

    def test_admin_add_rejects_missing_clip_and_private_station(self, hof_app, tmp_path):
        app, _, _ = hof_app
        import rovimen_dashboard as rd

        admin = _logged_in(app)
        with patch.object(rd, "ARCHIVE_PATH", tmp_path / "archive"):
            # Clip not in the archive -> 404.
            assert admin.post("/api/hall_of_fame",
                              json=_clip_payload()).status_code == 404
            # Private-station camera -> 400 even if a file existed.
            priv = _clip_payload("PRIV01")
            _make_archive_clip(tmp_path / "archive", priv)
            assert admin.post("/api/hall_of_fame", json=priv).status_code == 400

    def test_admin_add_accepts_compact_yyyymmdd_date(self, hof_app, tmp_path):
        # The station Video DB and the map's GMN clips send date as compact
        # YYYYMMDD; the endpoint must normalise it (Highlights sends YYYY-MM-DD).
        app, _, _ = hof_app
        import rovimen_dashboard as rd

        payload = _clip_payload()                       # date 2026-07-05
        _make_archive_clip(tmp_path / "archive", payload)
        compact = dict(payload, date="20260705")        # station/VDB form
        admin = _logged_in(app)
        with patch.object(rd, "ARCHIVE_PATH", tmp_path / "archive"):
            resp = admin.post("/api/hall_of_fame", json=compact)
        assert resp.status_code == 201, resp.get_json()
        entry = resp.get_json()
        assert entry["date"] == "2026-07-05"            # canonicalised
        assert "/media/v1/clip/PUB001/2026-07-05/" in entry["clip_url"]

    def test_add_error_is_json_with_reason(self, hof_app, tmp_path):
        # A rejected add returns JSON {"error": ...} so the player toast can
        # show the reason rather than a bare "HTTP 400".
        app, _, _ = hof_app
        import rovimen_dashboard as rd
        with patch.object(rd, "ARCHIVE_PATH", tmp_path / "archive"):
            resp = _logged_in(app).post("/api/hall_of_fame",
                                        json=_clip_payload("PRIV01"))
        assert resp.status_code in (400, 404)
        assert resp.is_json and resp.get_json().get("error")


# ── Public API projection ──────────────────────────────────────────────


class TestPublicApi:
    def test_key_gated_endpoint_projects_absolute_urls(self, hof_app, monkeypatch):
        app, _, store = hof_app
        _seed(store, "PUB001", 1)
        _seed(store, "PRIV01", 2)
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "1")

        anon = app.test_client()
        assert anon.get("/api/public/v1/hall_of_fame").status_code == 401

        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "0")
        resp = anon.get("/api/public/v1/hall_of_fame")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert [e["camera"] for e in data["entries"]] == ["PUB001"]
        assert data["entries"][0]["clip_url"].startswith("http")
        assert "/media/v1/clip/PUB001/" in data["entries"][0]["clip_url"]
        assert "added_by" not in data["entries"][0]
        # The seeded entry has no stack field; the public surface must still
        # project a derived stack thumbnail (external consumers embed it),
        # matching the dashboard route's behaviour rather than emitting null.
        stack_url = data["entries"][0]["stack_url"]
        assert stack_url.startswith("http")
        assert stack_url.endswith(
            "/media/v1/stack/PUB001/2026-07-01/PUB001_20260701_220001_stack.webp")

    def test_listed_in_discovery_index(self, hof_app):
        app, _, _ = hof_app
        idx = app.test_client().get("/api/public/v1").get_json()
        assert idx["endpoints"]["hall_of_fame"] == "/api/public/v1/hall_of_fame"


# ── /api/live-feed anon fix ────────────────────────────────────────────


class TestLiveFeedAnon:
    def test_anon_reachable_and_filtered_to_public_stations(self, hof_app):
        app, _, _ = hof_app
        resp = app.test_client().get("/api/live-feed")
        assert resp.status_code == 200
        # No pollers in test mode: stations report offline, but the shape and
        # the per-station public filter are what we're proving here.
        assert set(resp.get_json().keys()) == {"pub_station"}
        # Logged-in users keep the full fleet.
        full = _logged_in(app).get("/api/live-feed")
        assert set(full.get_json().keys()) == {"pub_station", "priv_station"}

    def test_anon_gated_when_live_toggled_off(self, hof_store):
        app, _ = _build_app(public_pages=("overview",))
        assert app.test_client().get("/api/live-feed").status_code in (302, 401)
        assert _logged_in(app).get("/api/live-feed").status_code == 200

    def test_commissioning_station_hidden_from_live_grid(self, hof_app):
        """A commissioning / camera-less station has nothing to show on the
        live grid — it must be absent for anonymous AND logged-in callers,
        not rendered as a permanent 'Station offline' card."""
        app, _, _ = hof_app
        assert "comm_station" not in app.test_client().get("/api/live-feed").get_json()
        assert "comm_station" not in _logged_in(app).get("/api/live-feed").get_json()


# ── Fireball reports linked from the Hall of Fame ──────────────────────


@pytest.fixture
def report_dir(tmp_path, monkeypatch):
    """One report covering PUB001's first test clip and PRIV01's."""
    import json

    import fireball_reports as fr

    d = tmp_path / "reports" / "20260701_220000"
    d.mkdir(parents=True)
    (d / "report.json").write_text(json.dumps({
        "event_id": "20260701_220000",
        "title": "Fireball over the test range",
        "time_utc": "2026-07-01 22:00:00 UTC",
        "analysed": "2026-07-02",
        "status": "preliminary",
        "authors": "Test Authors",
        "summary": ["It was bright."],
        "cameras": ["PUB001", "PRIV01"],
        "facts": [{"label": "Entry speed", "value": "20 km/s"}],
        "clips": [
            {"camera": "PUB001", "date": "2026-07-01", "filename": "PUB001_20260701_220001_color.mkv"},
            {"camera": "PRIV01", "date": "2026-07-01", "filename": "PRIV01_20260701_220000_color.mkv"},
        ],
        "sections": [{"heading": "Map", "paragraphs": [],
                      "figures": [{"file": "map.webp", "caption": "The path."}]}],
        "notes": ["Recomputed after a correction."],
    }))
    (d / "map.webp").write_bytes(b"RIFF0000WEBP")
    monkeypatch.setattr(fr, "REPORTS_DIR", tmp_path / "reports")
    return d


class TestFireballReports:
    def test_entry_links_its_report(self, hof_app, report_dir):
        app, _, store = hof_app
        _seed(store, "PUB001", 1)   # PUB001_20260701_220001_color.mkv, listed in the report
        _seed(store, "PUB001", 2)   # not listed
        entries = {e["filename"]: e for e in
                   app.test_client().get("/api/hall_of_fame").get_json()["entries"]}
        linked = entries["PUB001_20260701_220001_color.mkv"]
        assert linked["report_url"] == "/fireball/20260701_220000"
        assert linked["report_title"] == "Fireball over the test range"
        assert entries["PUB001_20260701_220002_color.mkv"]["report_url"] is None

    def test_page_renders_for_anon_without_private_clips(self, hof_app, report_dir):
        app, _, _ = hof_app
        resp = app.test_client().get("/fireball/20260701_220000")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "Fireball over the test range" in html
        assert "Analysed 2026-07-02, the day after the event" in html
        # The camera's place comes from the station registry.
        assert "Public Station" in html
        assert "/media/v1/clip/PUB001/2026-07-01/PUB001_20260701_220001_color.mkv?format=mp4" in html
        assert "PRIV01_20260701_220000_color.mkv" not in html
        # Neither the private camera nor its station's place is named.
        assert "PRIV01" not in html and "Private Station" not in html
        assert "/fireball/20260701_220000/map.webp" in html
        assert "Recomputed after a correction." in html
        # A signed-in operator also gets the private station's camera and clip.
        html = _logged_in(app).get("/fireball/20260701_220000").get_data(as_text=True)
        assert "PRIV01_20260701_220000_color.mkv" in html
        assert "Private Station" in html

    def test_play_all_in_sync_needs_two_visible_clips(self, hof_app, report_dir):
        app, _, _ = hof_app
        # Anon sees one clip (PUB001), so there is nothing to sync.
        html = app.test_client().get("/fireball/20260701_220000").get_data(as_text=True)
        assert "fr-play-all" not in html
        # The operator sees both; each video carries its chunk's UTC start and
        # the section the event's begin time (2026-07-01 22:00:00 UTC).
        html = _logged_in(app).get("/fireball/20260701_220000").get_data(as_text=True)
        assert "Play all 2 cameras in sync" in html
        assert 'data-event="1782943200.0"' in html
        assert 'data-start="1782943201.0"' in html and 'data-start="1782943200.0"' in html
        assert "dashboard-fireball-report.js" in html

    def test_figures_served_only_when_listed_and_visible(self, hof_app, report_dir):
        import json
        app, _, _ = hof_app
        anon = app.test_client()
        resp = anon.get("/fireball/20260701_220000/map.webp")
        assert resp.status_code == 200 and resp.data == b"RIFF0000WEBP"
        # report.json and unlisted files are never served.
        assert anon.get("/fireball/20260701_220000/report.json").status_code == 404
        assert anon.get("/fireball/20260701_220000/other.webp").status_code == 404
        # A report showing only private cameras hides its figures from anon too.
        p = report_dir / "report.json"
        data = json.loads(p.read_text())
        data["cameras"] = ["PRIV01"]
        p.write_text(json.dumps(data))
        assert anon.get("/fireball/20260701_220000/map.webp").status_code == 404
        assert _logged_in(app).get("/fireball/20260701_220000/map.webp").status_code == 200

    def test_video_figure_renders_inline_and_is_served(self, hof_app, report_dir):
        import json
        app, _, _ = hof_app
        p = report_dir / "report.json"
        data = json.loads(p.read_text())
        data["sections"][0]["figures"].append(
            {"file": "train.mp4", "poster": "train.webp", "size": "third", "caption": "The train."})
        p.write_text(json.dumps(data))
        (report_dir / "train.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        (report_dir / "train.webp").write_bytes(b"RIFF0000WEBP")
        anon = app.test_client()
        html = anon.get("/fireball/20260701_220000").get_data(as_text=True)
        assert ('<video src="/fireball/20260701_220000/train.mp4" '
                'poster="/fireball/20260701_220000/train.webp" controls muted loop playsinline') in html
        assert "fr-figure--third" in html
        # The image figure still renders as an image.
        assert '<img src="/fireball/20260701_220000/map.webp"' in html
        resp = anon.get("/fireball/20260701_220000/train.mp4")
        assert resp.status_code == 200 and resp.mimetype == "video/mp4"
        # The poster is served like a figure.
        assert anon.get("/fireball/20260701_220000/train.webp").status_code == 200

    def test_svg_figure_is_served_with_a_locked_down_policy(self, hof_app, report_dir):
        import json
        app, _, _ = hof_app
        p = report_dir / "report.json"
        data = json.loads(p.read_text())
        data["sections"][0]["figures"].append({"file": "spectrum.svg", "caption": "A vector plot."})
        p.write_text(json.dumps(data))
        (report_dir / "spectrum.svg").write_text('<svg xmlns="http://www.w3.org/2000/svg"/>')
        anon = app.test_client()
        html = anon.get("/fireball/20260701_220000").get_data(as_text=True)
        assert '<img src="/fireball/20260701_220000/spectrum.svg"' in html
        resp = anon.get("/fireball/20260701_220000/spectrum.svg")
        assert resp.status_code == 200 and resp.mimetype == "image/svg+xml"
        # Opened on its own, the SVG may not run scripts or load anything external.
        assert resp.headers["Content-Security-Policy"].startswith("default-src 'none'")
        assert "script-src" not in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        # Raster figures do not get the SVG policy.
        raster = anon.get("/fireball/20260701_220000/map.webp")
        assert "img-src data:" not in raster.headers.get("Content-Security-Policy", "")

    def test_report_with_only_private_cameras_is_404_for_anon(self, hof_app, report_dir):
        import json
        app, _, _ = hof_app
        p = report_dir / "report.json"
        data = json.loads(p.read_text())
        data["cameras"] = ["PRIV01"]
        data["clips"] = data["clips"][1:]
        p.write_text(json.dumps(data))
        assert app.test_client().get("/fireball/20260701_220000").status_code == 404
        assert _logged_in(app).get("/fireball/20260701_220000").status_code == 200

    def test_unknown_or_malformed_event_is_404(self, hof_app, report_dir):
        app, _, _ = hof_app
        c = app.test_client()
        assert c.get("/fireball/20260701_220001").status_code == 404
        assert c.get("/fireball/2026-07-01").status_code == 404

    def test_page_gated_when_overview_toggled_off(self, hof_store, report_dir):
        app, _ = _build_app(public_pages=("live",))
        assert app.test_client().get("/fireball/20260701_220000").status_code in (302, 401)
        assert _logged_in(app).get("/fireball/20260701_220000").status_code == 200

    def test_public_api_projects_absolute_report_url(self, hof_app, report_dir, monkeypatch):
        app, _, store = hof_app
        _seed(store, "PUB001", 1)
        monkeypatch.setenv("ROVIMEN_API_KEYS_REQUIRED", "0")
        entry = app.test_client().get("/api/public/v1/hall_of_fame").get_json()["entries"][0]
        assert entry["report_url"].startswith("http")
        assert entry["report_url"].endswith("/fireball/20260701_220000")
        assert entry["report_title"] == "Fireball over the test range"
