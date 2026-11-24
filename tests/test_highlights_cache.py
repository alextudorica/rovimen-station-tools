"""Tests for the /api/highlights/data aggregation cache.

The highlights range aggregation is expensive enough to blow nginx's upstream
timeout (and, on the single gthread worker, stall sibling requests). The route
caches the assembled payload per (start, end). These tests prove a repeat
request is served from cache without re-running the per-date aggregation, and
that a different range still recomputes.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

PROJECT = Path(__file__).resolve().parent.parent
FIXTURES = PROJECT / "tests" / "e2e" / "fixtures"
sys.path.insert(0, str(PROJECT / "dashboard"))

PAST = "2024-01-15"          # fully in the past -> long TTL, deterministic
PAST2 = "2024-02-20"


@pytest.fixture
def app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_hl_test_"))
    os.environ["ROVIMEN_USERS_PATH"] = str(FIXTURES / "users.yaml")
    os.environ["ROVIMEN_SECRET_KEY"] = "hl-test-key"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    os.environ["RATELIMIT_ENABLED"] = "0"
    os.environ["ROVIMEN_API_KEYS_REQUIRED"] = "0"
    os.environ["THUMB_CACHE_DIR"] = str(tmp / "thumb_cache")
    os.environ["ROVIMEN_CACHE_PATH"] = str(tmp / "cache")
    os.environ["ROVIMEN_ARCHIVE_PATH"] = str(tmp / "archive")  # empty -> no clips
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

    import yaml
    config_data = {
        "correlation_window_s": 1,
        "station_api_port": 7779,
        "stations": {
            "teststn1": {
                "cameras": [{"code": "TST001", "label": "N", "cam_ip": "127.0.0.1",
                             "rotate": False, "az": 0.0, "alt": 45.0}],
                "ip": "127.0.0.1", "jump_hosts": [], "label": "Test Alpha",
                "lat": 45.0, "lon": 25.0, "proxy_media": False, "public": True,
                "public_tabs": [], "ssh_user": "test", "location_name": "Test Alpha",
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
    return flask_app


@pytest.fixture
def client(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "alice"
        sess["role"] = "admin"
        sess["stations"] = []
    return c


@pytest.fixture
def gmn_counter(monkeypatch):
    """Count events_for_date calls; return empty events so the route is fast."""
    import gmn_data
    calls: list[str] = []

    def _stub(date_str):
        calls.append(date_str)
        return {"events": [], "date": date_str}
    monkeypatch.setattr(gmn_data, "events_for_date", _stub)
    # Avoid network/file reads for orbit tallies too.
    monkeypatch.setattr(gmn_data, "_daily_counts_for_month", lambda y, m: {})
    return calls


def test_repeat_request_hits_cache(client, gmn_counter):
    r1 = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r1.status_code == 200
    after_first = len(gmn_counter)
    assert after_first >= 1  # recomputed: at least one date aggregated

    r2 = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r2.status_code == 200
    # Cache hit: no further per-date aggregation happened.
    assert len(gmn_counter) == after_first
    assert r1.get_json() == r2.get_json()


def test_different_range_recomputes(client, gmn_counter):
    client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    after_first = len(gmn_counter)
    client.get(f"/api/highlights/data?start={PAST2}&end={PAST2}")
    assert len(gmn_counter) > after_first  # distinct key -> recompute


def test_response_carries_etag(client, gmn_counter):
    r = client.get(f"/api/highlights/data?start={PAST}&end={PAST}")
    assert r.status_code == 200
    assert r.headers.get("ETag")
    assert "max-age" in r.headers.get("Cache-Control", "")


def test_invalid_dates_not_cached(client, gmn_counter):
    r = client.get("/api/highlights/data?start=nonsense&end=also-bad")
    assert r.status_code == 400


# ── The modal's "Download clip": the MP4 copy as an attachment ───────────────

def test_clip_download_param_serves_the_mp4_as_attachment(client, monkeypatch, tmp_path):
    import public_api
    monkeypatch.setattr(public_api.rd, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "TST001" / "20240115" / "meteors"
    meteors.mkdir(parents=True)
    name = "TST001_20240115_213000_color.mkv"
    (meteors / name).write_bytes(b"original-bytes")
    url = f"/api/highlights/clip/TST001/2024-01-15/{name}"

    # Playback defaults to a stream-copied MP4 (Safari/iOS); ?format=mkv is
    # the original, still inline.
    inline = client.get(url + "?format=mkv")
    assert inline.status_code == 200
    assert inline.data == b"original-bytes"
    assert "attachment" not in (inline.headers.get("Content-Disposition") or "")

    # The modal's "Download clip" gets the MP4 copy as an attachment...
    (meteors / name.replace(".mkv", ".mp4")).write_bytes(b"mp4-bytes")
    dl = client.get(url + "?download=1")
    assert dl.status_code == 200
    assert dl.data == b"mp4-bytes"
    assert dl.headers["Content-Disposition"].startswith("attachment")
    assert name.replace(".mkv", ".mp4") in dl.headers["Content-Disposition"]

    # ...and ?format=mkv&download=1 the original Matroska file.
    raw = client.get(url + "?format=mkv&download=1")
    assert raw.data == b"original-bytes"
    assert raw.headers["Content-Disposition"].startswith("attachment")
    assert name in raw.headers["Content-Disposition"]


# ── The modal's trimmed download: the shared, bounded trim ───────────────────

def _shortclip_setup(monkeypatch, tmp_path) -> tuple[str, list]:
    """Archive one clip, stub the shared trim, and refuse any ffmpeg run on
    the request thread. Returns the route URL and the trim calls."""
    import subprocess

    import public_api
    import routes.archive as archive_routes
    from flask import Response

    monkeypatch.setattr(public_api.rd, "ARCHIVE_PATH", tmp_path)
    meteors = tmp_path / "TST001" / "20240115" / "meteors"
    meteors.mkdir(parents=True)
    name = "TST001_20240115_213000_color.mkv"
    (meteors / name).write_bytes(b"original-bytes")

    def _no_request_thread_ffmpeg(*a, **k):
        raise AssertionError("ffmpeg ran on the request thread")
    monkeypatch.setattr(subprocess, "run", _no_request_thread_ffmpeg)

    calls: list = []

    def _fake_trim(src, ss, t, filename):
        calls.append((Path(src).name, ss, t, filename))
        return Response(b"trimmed", mimetype="video/mp4")
    monkeypatch.setattr(archive_routes, "trim_clip_response", _fake_trim)
    return f"/api/highlights/shortclip/TST001/2024-01-15/{name}", calls


def test_shortclip_uses_the_bounded_media_trim(client, monkeypatch, tmp_path):
    """The trim runs through archive.trim_clip_response (2-lane media
    executor, 24 s cap), never as its own 120 s ffmpeg on the request
    thread; the seek is capped at 300 s like /api/archive/shortclip."""
    url, calls = _shortclip_setup(monkeypatch, tmp_path)
    resp = client.get(url + "?ss=900&t=12")
    assert resp.status_code == 200
    assert resp.data == b"trimmed"
    name = url.rsplit("/", 1)[1]
    assert calls == [(name, 300.0, 12.0, name)]


def test_shortclip_failed_trim_is_500(client, monkeypatch, tmp_path):
    import routes.archive as archive_routes

    url, _ = _shortclip_setup(monkeypatch, tmp_path)
    monkeypatch.setattr(archive_routes, "trim_clip_response", lambda *a: None)
    assert client.get(url + "?ss=2&t=5").status_code == 500


def test_data_returns_events_not_top10(client, gmn_counter):
    body = client.get(f"/api/highlights/data?start={PAST}&end={PAST}").get_json()
    assert body["events"] == []
    assert "top10" not in body


def test_reports_list_every_visible_camera_with_media(client, monkeypatch):
    import fireball_reports as fr

    report = fr.Report.model_validate({
        "event_id": "20240115_201500", "title": "Test fireball", "time_utc": "2024-01-15 20:15:00",
        "analysed": "2024-01-20", "status": "final", "authors": "A", "summary": ["Bright."],
        "cameras": ["TST001", "ZZZ999"], "facts": [{"label": "Speed", "value": "30 km/s"}],
        "clips": [
            {"camera": "TST001", "date": "2024-01-15", "filename": "TST001_20240115_201450_color.mkv"},
            {"camera": "ZZZ999", "date": "2024-01-15", "filename": "ZZZ999_20240115_201452_color.mkv"},
        ],
    })
    monkeypatch.setattr(fr, "all_reports", lambda reports_dir=None: {report.event_id: report})

    body = client.get("/api/highlights/reports").get_json()
    (rep,) = body["reports"]
    assert rep["url"] == "/fireball/20240115_201500"
    assert rep["cameras"] == ["TST001"]
    (w,) = rep["witnesses"]
    assert w["station_label"] == "Test Alpha"
    assert w["clip_url"] == "/api/highlights/clip/TST001/2024-01-15/TST001_20240115_201450_color.mkv"
    assert w["stack_url"] == "/api/highlights/stack/TST001/2024-01-15/TST001_20240115_201450_stack.webp"
    assert w["detection_offset_s"] == 10.0
    assert rep["cover_url"] is None   # the test report has no figures
    assert rep["slides"] == []


def test_report_slides_are_every_still_figure_in_reading_order(client, monkeypatch):
    import fireball_reports as fr

    report = fr.Report.model_validate({
        "event_id": "20240115_201500", "title": "Test fireball", "time_utc": "2024-01-15 20:15:00",
        "analysed": "2024-01-20", "status": "final", "authors": "A", "summary": ["Bright."],
        "cameras": ["TST001"], "facts": [],
        "clips": [{"camera": "TST001", "date": "2024-01-15", "filename": "TST001_20240115_201450_color.mkv"}],
        "sections": [
            {"heading": "Track", "paragraphs": ["p"], "figures": [
                {"file": "map.webp", "caption": "Map"},
                {"file": "train.mp4", "caption": "No poster: left out"},
                {"file": "train_3d.mp4", "caption": "3D", "poster": "train_3d.webp"},
            ]},
            {"heading": "Orbit", "paragraphs": ["p"], "figures": [
                {"file": "orbit.webp", "caption": "Orbit"},
                {"file": "map.webp", "caption": "Repeated: listed once"},
            ]},
        ],
    })
    monkeypatch.setattr(fr, "all_reports", lambda reports_dir=None: {report.event_id: report})

    (rep,) = client.get("/api/highlights/reports").get_json()["reports"]
    base = "/fireball/20240115_201500/"
    assert rep["slides"] == [base + "map.webp", base + "train_3d.webp", base + "orbit.webp"]
    assert rep["cover_url"] == rep["slides"][0]
