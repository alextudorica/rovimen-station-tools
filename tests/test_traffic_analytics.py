"""Traffic-analytics tests: activity-record fields, anonymous page views,
bot flagging, and the traffic report aggregation.

The record-shaping policy lives in ``security._build_activity_record`` (a
pure function of the request context + response), so these tests drive it
through ``test_request_context`` without spinning up the writer queue or a
full ``create_app`` instance.
"""

from __future__ import annotations

import pytest
from flask import Flask, Response, session

import security


@pytest.fixture
def app() -> Flask:
    a = Flask(__name__)
    a.secret_key = "traffic-test"
    return a


def _record(app: Flask, path: str, *, user: str | None = "alice",
            method: str = "GET", status: int = 200,
            headers: dict[str, str] | None = None) -> dict | None:
    """Run _build_activity_record inside a request context."""
    with app.test_request_context(path, method=method, headers=headers or {}):
        if user:
            session["user"] = user
        return security._build_activity_record(Response(status=status))


# ── Real client address + country behind the double proxy hop ─────────────


class TestAnalyticsFields:
    def test_country_header_present(self, app):
        rec = _record(app, "/events", headers={"Cf-Ipcountry": "RO"})
        assert rec is not None
        assert rec["country"] == "RO"

    def test_country_header_absent_is_null(self, app):
        rec = _record(app, "/events")
        assert rec is not None
        assert rec["country"] is None

    def test_ip_prefers_cf_connecting_ip(self, app):
        rec = _record(app, "/events", headers={
            "CF-Connecting-IP": "198.51.100.9",
            "X-Forwarded-For": "93.184.216.34, 127.0.0.1",
        })
        assert rec["ip"] == "198.51.100.9"

    def test_ip_falls_back_to_last_safe_xff_hop(self, app):
        # Production shape: client, then the loopback hop nginx appends.
        rec = _record(app, "/events", headers={
            "X-Forwarded-For": "93.184.216.34, 127.0.0.1",
        })
        assert rec["ip"] == "93.184.216.34"

    def test_ip_tailnet_xff_entry_is_a_real_client(self, app):
        rec = _record(app, "/events", headers={
            "X-Forwarded-For": "100.64.0.4, 127.0.0.1",
        })
        assert rec["ip"] == "100.64.0.4"

    def test_ip_all_internal_hops_falls_back_to_remote_addr(self, app):
        rec = _record(app, "/events", headers={
            "X-Forwarded-For": "10.0.0.5, 127.0.0.1",
        })
        # test_request_context has no remote_addr → the "-" fallback.
        assert rec["ip"] == "-"

    def test_ip_garbage_xff_entry_is_skipped(self, app):
        rec = _record(app, "/events", headers={
            "X-Forwarded-For": '93.184.216.34, "><script>, 127.0.0.1',
        })
        assert rec["ip"] == "93.184.216.34"

    def test_ua_recorded_and_truncated_to_120(self, app):
        rec = _record(app, "/events", headers={"User-Agent": "x" * 300})
        assert rec["ua"] == "x" * 120

    def test_ua_absent_is_null(self, app):
        rec = _record(app, "/events")
        assert rec["ua"] is None

    def test_client_ip_for_rate_limiting_ignores_a_cf_header_a_tailnet_peer_can_set(self, app):
        """Analytics trusts CF-Connecting-IP first; the rate-limit key does not
        (full cases in test_client_ip.py)."""
        with app.test_request_context(
            "/events",
            environ_base={"REMOTE_ADDR": "127.0.0.1"},
            headers={"CF-Connecting-IP": "198.51.100.9",
                     "X-Forwarded-For": "100.64.0.4, 127.0.0.1"},
        ):
            assert security._client_ip() == "100.64.0.4"
            assert security._analytics_ip() == "198.51.100.9"


_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


class TestAnonymousPageViews:
    @pytest.mark.parametrize("path", [
        "/", "/map", "/stations", "/events", "/highlights", "/live",
        "/showers", "/wiki", "/build", "/social", "/login",
        "/station/gmnro03", "/wiki/de",
    ])
    def test_anon_page_get_is_logged_with_null_user(self, app, path):
        rec = _record(app, path, user=None,
                      headers={"User-Agent": _BROWSER_UA})
        assert rec is not None, f"{path} should be logged for anon"
        assert rec["user"] is None

    @pytest.mark.parametrize("path", [
        "/api/overview", "/api/detections/nights", "/media/v1/x.mp4",
        "/static/dashboard.css", "/favicon.ico", "/unknown",
        "/live_view",
    ])
    def test_anon_non_page_paths_are_not_logged(self, app, path):
        assert _record(app, path, user=None) is None

    def test_anon_post_is_not_logged(self, app):
        assert _record(app, "/login", user=None, method="POST") is None

    def test_anon_error_status_is_not_logged(self, app):
        assert _record(app, "/events", user=None, status=404) is None
        assert _record(app, "/events", user=None, status=500) is None

    def test_anon_redirect_is_logged(self, app):
        rec = _record(app, "/events", user=None, status=302)
        assert rec is not None

    @pytest.mark.parametrize("ua", [
        "Mozilla/5.0 (compatible; Googlebot/2.1)",
        "Mozilla/5.0 (compatible; AhrefsBot/7.0)",
        "Baiduspider+(+http://www.baidu.com/search/spider.htm)",
        "python-requests/2.32.0",
        "curl/8.5.0",
        "Mozilla/5.0 zgrab/0.x",
        "Mozilla/5.0 (compatible; CensysInspect/1.1)",
        "Expanse, a Palo Alto Networks company",
        "SomeCrawler/1.0",
    ])
    def test_bot_ua_gets_bot_flag(self, app, ua):
        rec = _record(app, "/", user=None, headers={"User-Agent": ua})
        assert rec is not None, "bots must be logged, not dropped"
        assert rec.get("bot") is True

    def test_browser_ua_has_no_bot_flag(self, app):
        rec = _record(app, "/", user=None,
                      headers={"User-Agent": _BROWSER_UA})
        assert "bot" not in rec

    def test_authenticated_records_keep_previous_shape(self, app):
        rec = _record(app, "/api/detections/nights", user="alice")
        assert rec is not None
        assert rec["user"] == "alice"
        assert "bot" not in rec

    def test_authenticated_skip_prefixes_still_apply(self, app):
        assert _record(app, "/static/x.css", user="alice") is None
        assert _record(app, "/api/vitals/gmnro03", user="alice") is None


class TestIsInternalHop:
    @pytest.mark.parametrize("value", [
        "127.0.0.1", "10.1.2.3", "192.168.0.9", "172.16.4.4",
        "169.254.0.1", "0.0.0.0", "not-an-ip", "", "::1", "fd00::1",
    ])
    def test_internal(self, value):
        assert security._is_internal_hop(value) is True

    @pytest.mark.parametrize("value", [
        "93.184.216.34", "8.8.8.8", "100.64.0.4", "2606:2800:220:1::1",
    ])
    def test_external(self, value):
        assert security._is_internal_hop(value) is False


# ── compute_traffic_report ────────────────────────────────────────────────


import json
from datetime import datetime, timedelta, timezone

import usage_stats


@pytest.fixture(autouse=True)
def _clear_traffic_cache():
    usage_stats._traffic_cache = None
    yield
    usage_stats._traffic_cache = None


def _ts(minutes_ago: int = 0) -> str:
    dt = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    return dt.isoformat(timespec="seconds")


_UA_DESKTOP = "Mozilla/5.0 (X11; Linux x86_64) Chrome/126.0"
_UA_MOBILE = "Mozilla/5.0 (Linux; Android 14; Pixel 8) Mobile Safari/537.36"
_UA_BOT = "Mozilla/5.0 (compatible; Googlebot/2.1)"


@pytest.fixture
def traffic_log(tmp_path, monkeypatch):
    """Synthetic activity log covering auth/anon/bot, old-shape lines,
    self-referrals, and non-page records that must be excluded."""
    records = [
        # Authenticated page views (count toward traffic).
        {"ts": _ts(50), "user": "alice", "method": "GET", "path": "/",
         "status": 200, "ip": "1.2.3.4", "country": "RO",
         "ua": _UA_DESKTOP, "ref": None},
        {"ts": _ts(45), "user": "alice", "method": "GET",
         "path": "/station/gmnro03", "status": 200, "ip": "1.2.3.4",
         "country": "RO", "ua": _UA_DESKTOP,
         "ref": "https://dashboard.rovimen.org/"},
        # Authenticated API call — NOT a page view, excluded from traffic.
        {"ts": _ts(44), "user": "alice", "method": "GET",
         "path": "/api/detections/nights", "status": 200, "ip": "1.2.3.4",
         "country": "RO", "ua": _UA_DESKTOP, "ref": None},
        # Old-shape authenticated line (pre-country/ua fields).
        {"ts": _ts(40), "user": "bob", "method": "GET", "path": "/events",
         "status": 200, "ip": "127.0.0.1", "ref": None},
        # Anonymous visitors.
        {"ts": _ts(30), "user": None, "method": "GET", "path": "/events",
         "status": 200, "ip": "93.184.216.34", "country": "DE",
         "ua": _UA_MOBILE, "ref": "https://www.facebook.com/groups/astro"},
        {"ts": _ts(20), "user": None, "method": "GET", "path": "/",
         "status": 200, "ip": "5.6.7.8", "country": "RO", "ua": _UA_DESKTOP,
         "ref": "https://dashboard.rovimen.org/events"},  # self — excluded
        # Bot: counted in volume/devices/daily, excluded from tables.
        {"ts": _ts(10), "user": None, "method": "GET", "path": "/",
         "status": 200, "ip": "66.249.66.1", "country": "US",
         "ua": _UA_BOT, "bot": True, "ref": "https://google.com/"},
        # Mutations never count as visits.
        {"ts": _ts(5), "user": "alice", "method": "POST", "path": "/events",
         "status": 200, "ip": "1.2.3.4", "country": "RO",
         "ua": _UA_DESKTOP, "ref": None},
    ]
    log = tmp_path / "activity.log"
    with open(log, "w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    monkeypatch.setattr(usage_stats, "ACTIVITY_LOG_PATH", log)
    return log


class TestComputeTrafficReport:
    def test_totals(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        t = rpt["totals"]
        assert t["visits"] == 6          # 3 auth + 2 anon + 1 bot pages
        assert t["auth_visits"] == 3
        assert t["anon_visits"] == 2
        assert t["bot_visits"] == 1
        # 1.2.3.4, 127.0.0.1, 93.184.216.34, 5.6.7.8 — bot IP excluded
        assert t["unique_visitors"] == 4
        assert t["countries"] == 2       # RO + DE; "??" (bob) not a country

    def test_country_rows_split_auth_anon(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        rows = {r["country"]: r for r in rpt["countries"]}
        assert rows["RO"]["visits"] == 3
        assert rows["RO"]["auth"] == 2
        assert rows["RO"]["anon"] == 1
        assert rows["RO"]["unique_ips"] == 2   # 1.2.3.4 + 5.6.7.8
        assert rows["DE"]["visits"] == 1
        assert rows["DE"]["anon"] == 1
        # Old-shape line without a country lands in the "??" bucket.
        assert rows["??"]["visits"] == 1
        # Bots never reach the country table.
        assert "US" not in rows

    def test_device_split(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        assert rpt["devices"] == {
            "mobile": 1, "desktop": 3, "bot": 1, "unknown": 1,
        }

    def test_daily_series_is_dense_and_split(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        assert len(rpt["daily"]) == 7
        today = rpt["daily"][-1]
        assert today["auth"] == 3
        assert today["anon"] == 2
        assert today["bot"] == 1
        assert rpt["daily"][0] == {
            "date": rpt["daily"][0]["date"], "auth": 0, "anon": 0, "bot": 0,
        }

    def test_top_pages_use_page_labels(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        pages = {p["page"]: p for p in rpt["pages"]}
        assert pages["Overview"]["visits"] == 2      # alice + anon; bot excluded
        assert pages["Detections"]["visits"] == 2    # bob + anon /events
        assert pages["Station dashboard"]["visits"] == 1
        assert "Api" not in pages

    def test_referrers_group_by_domain_and_exclude_self(self, traffic_log):
        rpt = usage_stats.compute_traffic_report(days=7)
        domains = {r["domain"]: r["visits"] for r in rpt["referrers"]}
        assert domains == {"facebook.com": 1}  # www. stripped, self + bot out

    def test_report_is_cached(self, traffic_log):
        first = usage_stats.compute_traffic_report(days=7)
        # Delete the log: a second call inside the TTL must not re-scan.
        traffic_log.unlink()
        assert usage_stats.compute_traffic_report(days=7) is first


class TestPageLabel:
    @pytest.mark.parametrize("path,label", [
        ("/", "Overview"),
        ("", "Overview"),
        ("/map", "Fleet map"),
        ("/stations", "Fleet map"),
        ("/events", "Detections"),
        ("/highlights", "Highlights"),
        ("/live", "Live View"),
        ("/showers", "Showers"),
        ("/wiki", "Build Guide"),
        ("/wiki/de", "Build Guide"),
        ("/build", "Build Guide"),
        ("/social", "Social"),
        ("/login", "Login"),
        ("/station/gmnro03", "Station dashboard"),
        ("/station/gmnro03/vdb", "Station dashboard"),
    ])
    def test_pages(self, path, label):
        assert usage_stats._page_label(path) == label

    @pytest.mark.parametrize("path", [
        "/live_view", "/api/events", "/static/x.css", "/unknown",
        "/eventsfoo",
    ])
    def test_non_pages(self, path):
        assert usage_stats._page_label(path) is None
