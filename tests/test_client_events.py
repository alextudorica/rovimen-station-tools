"""Client-event beacon: failures users saw and in-page tab views.

nginx gives up on a slow request at 30 s and the user gets a 504, but Flask
finishes later and logs a 200, so 73 such 504s on /api/detections in 14 days
never reached the Usage Stats. Tab switches inside the Detections page make no
request at all. The browser reports both to POST /api/client-event; these
tests pin the validation, the route's auth and bounds, and the report.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import yaml

import client_events as ce
import usage_stats

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


def _build(events, user="operator"):
    return ce.build_records(user, {"events": events}, ip="1.2.3.4", ua="UA", now=NOW)


# ── build_records ────────────────────────────────────────────────────────────

def test_failure_and_tab_records_share_the_request_record_keys():
    recs = _build([
        {"type": "failure", "path": "/api/detections/20260921", "status": 504,
         "reason": "http", "page": "/events"},
        {"type": "tab", "page": "/events", "tab": "grid"},
    ])
    fail, tab = recs
    assert fail == {"ts": "2026-09-26T12:00:00+00:00", "user": "operator", "method": "CLIENT",
                    "ip": "1.2.3.4", "ua": "UA", "kind": "client_failure",
                    "path": "/api/detections/20260921", "status": 504, "reason": "http",
                    "page": "/events"}
    assert tab["kind"] == "tab_view" and tab["path"] == "/events" and tab["tab"] == "grid"


@pytest.mark.parametrize("ev", [
    {"type": "failure", "path": "/api/x", "status": 504, "reason": "bogus"},
    {"type": "failure", "path": "https://evil.example/api/x", "reason": "http"},
    {"type": "failure", "path": "//evil.example/x", "reason": "http"},
    {"type": "failure", "path": "relative/path", "reason": "http"},
    {"type": "failure", "path": "/api/a b", "reason": "http"},
    {"type": "tab", "page": "/events", "tab": "Grid<script>"},
    {"type": "tab", "tab": "grid"},
    {"type": "other", "path": "/api/x"},
    "not-a-dict",
])
def test_malformed_events_are_dropped(ev):
    assert _build([ev]) == []


def test_query_and_fragment_never_reach_the_log():
    rec, = _build([{"type": "failure", "path": "/api/cached-video/a/b?format=mp4#t=3",
                    "reason": "media", "page": "/events?date=20260921"}])
    assert rec["path"] == "/api/cached-video/a/b"
    assert rec["page"] == "/events"


@pytest.mark.parametrize("status,expected", [(504, 504), (0, 0), (700, 0), (-1, 0), (True, 0), ("504", 0)])
def test_status_is_a_plain_http_code_or_zero(status, expected):
    rec, = _build([{"type": "failure", "path": "/x", "status": status, "reason": "http"}])
    assert rec["status"] == expected


def test_a_post_yields_at_most_the_batch_cap_and_needs_a_user():
    many = [{"type": "tab", "page": "/events", "tab": "grid"}] * 50
    assert len(_build(many)) == ce.MAX_EVENTS_PER_POST
    assert _build(many, user=None) == []
    assert ce.build_records("operator", "junk", ip=None, ua=None, now=NOW) == []
    assert ce.build_records("operator", {"events": "junk"}, ip=None, ua=None, now=NOW) == []


def test_the_beacon_request_itself_is_not_logged_as_a_view():
    import security
    assert "/api/client-event" in security._ACTIVITY_SKIP_PREFIXES


# ── route ────────────────────────────────────────────────────────────────────

def _build_app():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_client_events_"))
    (tmp / "users.yaml").write_text(yaml.safe_dump({
        "operator": {"display_name": "Alex", "password_hash": "", "role": "admin",
                 "require_totp": False},
    }))
    cfg = {
        "correlation_window_s": 1, "station_api_port": 7779,
        "public_pages": ["overview"],
        "stations": {"pub_station": {
            "ip": "127.0.0.1", "label": "Public Station", "ssh_user": "test",
            "public": True, "lat": 45.0, "lon": 25.0,
            "cameras": [{"code": "PUB001", "cam_ip": "127.0.0.1", "label": "N",
                         "rotate": False}],
        }},
    }
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    for key, name in [("ROVIMEN_USERS_PATH", "users.yaml"), ("THUMB_CACHE_DIR", "thumb_cache"),
                      ("ROVIMEN_CACHE_PATH", "cache"), ("ROVIMEN_COMPILATIONS_OUT_PATH", "compilations"),
                      ("ROVIMEN_KNOWN_HOSTS", "known_hosts"), ("ROVIMEN_AUDIT_LOG_PATH", "audit.log"),
                      ("ROVIMEN_ACTIVITY_LOG_PATH", "activity.log"),
                      ("ROVIMEN_STATION_STATE_DB", "station_state.db"),
                      ("ROVIMEN_DETECTIONS_DB", "detections.db"),
                      ("ROVIMEN_SHOWER_COUNTS_PATH", "shower_counts.json")]:
        os.environ[key] = str(tmp / name)
    os.environ["ROVIMEN_SECRET_KEY"] = "client-events-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"

    import security
    saved = security.init_limiter

    def _noop_limiter(app):
        lim = saved(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    try:
        from rovimen_dashboard import create_app, load_config
        app = create_app(load_config(config_path), config_path)
    finally:
        security.init_limiter = saved
    app.config["TESTING"] = True
    return app


@pytest.fixture
def captured(monkeypatch):
    import security
    out: list[dict] = []
    monkeypatch.setattr(security, "enqueue_activity_record", lambda r: out.append(r) or True)
    return out


@pytest.fixture(scope="module")
def app():
    return _build_app()


def _post(client, body):
    return client.post("/api/client-event", data=json.dumps(body),
                       content_type="application/json",
                       headers={"Sec-Fetch-Site": "same-origin"})


def test_anonymous_callers_are_turned_away_and_nothing_is_logged(app, captured):
    r = _post(app.test_client(), {"events": [{"type": "tab", "page": "/events", "tab": "grid"}]})
    assert r.status_code == 401
    assert captured == []


def test_signed_in_events_are_logged_under_the_session_user(app, captured):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "operator"
        sess["role"] = "admin"
        sess["stations"] = []
        sess["mfa"] = "totp"
    r = _post(c, {"events": [
        {"type": "failure", "path": "/api/detections/20260921", "status": 504,
         "reason": "http", "page": "/events"},
        {"type": "tab", "page": "/events", "tab": "sortfilter"},
        {"type": "failure", "path": "https://evil.example/x", "reason": "http"},
    ]})
    assert r.status_code == 204 and r.data == b""
    assert [(x["user"], x["kind"]) for x in captured] == [
        ("operator", "client_failure"), ("operator", "tab_view")]
    # Garbage bodies are a quiet 204 too: nothing for the client to retry.
    assert _post(c, "junk").status_code == 204
    assert len(captured) == 2


# ── usage report ─────────────────────────────────────────────────────────────

def _ts(minutes_ago: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


@pytest.fixture
def activity(tmp_path, monkeypatch):
    # All three report caches outlive a test by 60 s; a traffic report cached by
    # another file (test_public_dashboard_authz asks for days=7) was served here.
    usage_stats._usage_cache = None
    usage_stats._activity_cache = None
    usage_stats._traffic_cache = None
    path = tmp_path / "activity.log"
    monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", path)
    monkeypatch.setattr(usage_stats, "AUDIT_LOG_PATH", tmp_path / "audit.log")
    yield path
    usage_stats._usage_cache = None
    usage_stats._activity_cache = None
    usage_stats._traffic_cache = None


def test_report_groups_failures_and_tab_views_and_keeps_them_out_of_requests(activity):
    recs = [
        {"ts": _ts(30), "user": "operator", "method": "GET", "path": "/events", "status": 200},
        {"ts": _ts(29), "user": "operator", "method": "CLIENT", "kind": "client_failure",
         "path": "/api/detections/20260921", "status": 504, "reason": "http", "page": "/events"},
        {"ts": _ts(28), "user": "viewer", "method": "CLIENT", "kind": "client_failure",
         "path": "/api/detections/20260920", "status": 504, "reason": "http", "page": "/events"},
        {"ts": _ts(27), "user": "viewer", "method": "CLIENT", "kind": "client_failure",
         "path": "/api/cached-video/gmnmd01/MD0002/20260924/MD0002_20260924_213458_color.mkv",
         "status": 0, "reason": "media", "page": "/events"},
        {"ts": _ts(26), "user": "operator", "method": "CLIENT", "kind": "tab_view",
         "path": "/events", "tab": "grid", "status": 0},
        {"ts": _ts(25), "user": "viewer", "method": "CLIENT", "kind": "tab_view",
         "path": "/events", "tab": "grid", "status": 0},
    ]
    activity.write_text("".join(json.dumps(r) + "\n" for r in recs))
    rep = usage_stats.compute_usage(days=7)

    assert rep["totals"]["requests"] == 1
    assert [f["hits"] for f in rep["features"]] == [1]   # the one GET, no client records
    assert "viewer" not in {u["user"] for u in rep["users"]}   # only client records

    f = rep["client_failures"]
    assert f["total"] == 3 and f["users"] == 2
    fam = {x["family"]: x for x in f["families"]}
    assert fam["/api/detections"]["count"] == 2
    assert fam["/api/detections"]["outcomes"] == {"504": 2}
    assert fam["/api/cached-video"]["outcomes"] == {"media": 1}
    assert f["by_user"] == [{"user": "viewer", "count": 2}, {"user": "operator", "count": 1}]
    assert f["recent"][0]["reason"] == "media"            # newest first
    assert sum(d["count"] for d in f["daily"]) == 3 and len(f["daily"]) == 7

    assert rep["tab_views"] == [{"page": "/events", "tab": "grid", "views": 2, "users": 2,
                                 "last": rep["tab_views"][0]["last"]}]


def test_client_records_stay_out_of_the_traffic_report(activity):
    activity.write_text(json.dumps({"ts": _ts(5), "user": "operator", "method": "CLIENT",
                                    "kind": "tab_view", "path": "/events", "tab": "grid",
                                    "status": 0}) + "\n")
    rep = usage_stats.compute_traffic_report(days=7)
    assert rep["totals"]["visits"] == 0


@pytest.mark.parametrize("path,family", [
    ("/api/cached-video/gmnro07/RO000W/20260920/RO000W_20260920_213000_color.mkv", "/api/cached-video"),
    ("/api/detections/20260921", "/api/detections"),
    ("/api/detections/grid/20260921", "/api/detections/grid"),
    ("/stack/gmnro07/RO000W/20260919/x_stack.webp", "/stack"),
    ("/media/v1/clip/GH662S/2026-09-20/a.mp4", "/media/v1/clip"),
    ("/api/latest_ff_maxpixel/gmnde00/DE001B", "/api/latest_ff_maxpixel"),
])
def test_endpoint_family(path, family):
    assert usage_stats._endpoint_family(path) == family
