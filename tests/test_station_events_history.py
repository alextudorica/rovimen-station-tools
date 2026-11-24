"""VPS side of the station event history: dashboard/station_events.py, the
ingest route and heartbeat fields (ingest_api), the offline mark in
station_client._demote_stale_push, the read endpoint's access rules
(routes/station_history.py) and the CLI (tools/station_event_history.py)."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from flask import Flask

import ingest_api
import station_client
import station_events
import station_keys
from models import DashboardConfig, StationConfig

ROOT = Path(__file__).resolve().parents[1]


NOW = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def _fresh(monkeypatch, tmp_path):
    # A fresh database per test (never one left by an earlier schema), and a
    # fixed clock so the clamping window does not drift with the calendar.
    monkeypatch.setenv("ROVIMEN_STATION_EVENTS_DB", str(tmp_path / "events.db"))
    monkeypatch.setattr(station_events, "_clock", lambda: NOW)
    station_events.reset_memos_for_tests()
    yield
    station_events.reset_memos_for_tests()


class TestEvictionAndOrdering:
    def test_station_flood_leaves_every_vps_row(self, monkeypatch):
        monkeypatch.setattr(station_events, "MAX_ROWS_PER_STATION", 10)
        station_events.note_heartbeat("gmnro09", now=NOW - 7200)
        station_events.mark_offline("gmnro09", "2026-11-21T09:00:00Z", now=NOW - 3600)
        station_events.note_heartbeat("gmnro09", now=NOW - 1800)
        station_events.append_events("gmnro09", [
            {"id": f"ul-{i}", "kind": "uplink", "ts": "2026-11-21T11:00:00Z"} for i in range(50)])
        station_events.prune(now=NOW)
        rows = station_events.list_events(["gmnro09"], limit=1000)
        vps = sorted(r["kind"] for r in rows if r["source"] == "vps")
        assert vps == ["offline", "online"]
        assert sum(r["source"] == "station" for r in rows) == 10

    def test_future_dated_station_event_is_clamped_and_sorts_below_newer_vps_row(self):
        # Received at NOW - 600 but dated a year ahead.
        station_events.append_events("gmnro09", [
            {"id": "ul-future", "kind": "uplink", "ts": "2027-11-21T00:00:00Z"}], now=NOW - 600)
        station_events.note_heartbeat("gmnro09", now=NOW - 7200)
        station_events.mark_offline("gmnro09", station_events._iso(NOW - 300), now=NOW)
        rows = station_events.list_events(["gmnro09"], limit=1)
        assert rows[0]["source"] == "vps" and rows[0]["kind"] == "offline"
        [st] = [r for r in station_events.list_events(["gmnro09"]) if r["source"] == "station"]
        assert st["ts_clamped"] is True
        assert st["ts"] == st["received_at"] == station_events._iso(NOW - 600)
        assert st["sent_ts"] == "2027-11-21T00:00:00Z"

    def test_small_skew_and_honest_backlog_times_kept(self):
        station_events.append_events("gmnro09", [
            {"id": "ul-skew", "kind": "uplink", "ts": station_events._iso(NOW + 120)},
            {"id": "ul-old", "kind": "uplink", "ts": "2026-11-13T02:00:00Z"},
            {"id": "ul-ancient", "kind": "uplink", "ts": "2024-01-01T00:00:00Z"},
        ])
        rows = {r["event_id"]: r for r in station_events.list_events(["gmnro09"])}
        assert rows["ul-skew"]["ts_clamped"] is False
        assert rows["ul-old"]["ts"] == "2026-11-13T02:00:00Z" and not rows["ul-old"]["ts_clamped"]
        assert rows["ul-ancient"]["ts_clamped"] is True
        assert rows["ul-ancient"]["ts"] == station_events._iso(NOW)

    def test_daily_quota_refuses_excess_and_resets_next_day(self, monkeypatch, caplog):
        monkeypatch.setattr(station_events, "STATION_EVENTS_PER_DAY", 5)
        batch = [{"id": f"ul-{i}", "kind": "uplink", "ts": None} for i in range(7)]
        with caplog.at_level("WARNING"):
            inserted, dups, over = station_events.append_events("gmnro09", batch, now=NOW)
        assert (inserted, dups, over) == (5, 0, ["ul-5", "ul-6"])
        assert "daily quota" in caplog.text
        # Re-sending what was stored is a duplicate, not quota use.
        assert station_events.append_events("gmnro09", batch[:2], now=NOW + 60) == (0, 2, [])
        # VPS rows are not station events and are never held by the quota.
        station_events.note_heartbeat("gmnro09", now=NOW)
        assert station_events.mark_offline("gmnro09", "2026-11-21T12:30:00Z", now=NOW + 3600)
        # Next UTC day: accepted again.
        tomorrow = NOW + 86400
        assert station_events.append_events("gmnro09", batch[5:], now=tomorrow) == (2, 0, [])

    def test_quota_rejections_reported_by_ingest(self, ingest, monkeypatch):
        monkeypatch.setattr(station_events, "STATION_EVENTS_PER_DAY", 1)
        r = _post(ingest, "events", 1, {"events": [
            {"id": "ul-a", "kind": "uplink"}, {"id": "ul-b", "kind": "uplink"}]})
        body = r.get_json()
        assert body["inserted"] == 1 and body["rejected"] == 1
        assert body["rejected_events"][0] == {"index": None, "id": "ul-b", "status": 429,
                                              "error": "daily_quota_exceeded"}

    def test_read_endpoint_rows_carry_both_times(self):
        station_events.append_events("gmnro09", [
            {"id": "ul-a", "kind": "uplink", "ts": "2026-11-20T00:00:00Z"}], now=NOW)
        [row] = station_events.list_events(["gmnro09"])
        assert row["ts"] == "2026-11-20T00:00:00Z"
        assert row["received_at"] == station_events._iso(NOW)


def _db() -> Path:
    return station_events.db_path()


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── station_events store ────────────────────────────────────────────────────


class TestStore:
    def test_duplicate_ids_stored_once(self):
        ev = {"id": "ul-abc", "kind": "uplink", "ts": "2026-11-13T02:00:00Z",
              "detail": {"text": "uplink ok -> isp after 5 min"}}
        assert station_events.append_events("gmnro09", [ev]) == (1, 0, [])
        assert station_events.append_events("gmnro09", [ev, ev]) == (0, 2, [])
        rows = station_events.list_events(["gmnro09"])
        assert len(rows) == 1
        assert rows[0]["detail"]["text"] == "uplink ok -> isp after 5 min"

    def test_same_id_on_two_stations_is_two_events(self):
        ev = {"id": "boot-x", "kind": "boot", "ts": None, "detail": None}
        station_events.append_events("gmnro09", [ev])
        station_events.append_events("gmnro03", [ev])
        assert len(station_events.list_events(None)) == 2

    def test_newest_first_limit_and_since(self):
        base = datetime(2026, 11, 1, tzinfo=timezone.utc)
        evs = [{"id": f"e{i}", "kind": "uplink", "ts": _iso(base + timedelta(days=i)),
                "detail": None} for i in range(5)]
        station_events.append_events("gmnro09", evs)
        rows = station_events.list_events(["gmnro09"], limit=2)
        assert [r["event_id"] for r in rows] == ["e4", "e3"]
        since = station_events.parse_since("2026-11-03")
        rows = station_events.list_events(["gmnro09"], since=since)
        assert [r["event_id"] for r in rows] == ["e4", "e3", "e2"]

    def test_empty_host_list_returns_nothing(self):
        station_events.append_events("gmnro09", [{"id": "a", "kind": "uplink", "ts": None}])
        assert station_events.list_events([]) == []

    def test_missing_db_reads_empty(self):
        assert station_events.list_events(None) == []

    def test_prune_by_age_keeps_twelve_months(self):
        now = 2_000_000_000.0
        station_events.append_events("gmnro09", [{"id": "old", "kind": "uplink", "ts": None}],
                                     now=now - 401 * 86400)
        station_events.append_events("gmnro09", [{"id": "year", "kind": "uplink", "ts": None}],
                                     now=now - 365 * 86400)
        assert station_events.prune(now=now) == 1
        assert [r["event_id"] for r in station_events.list_events(None)] == ["year"]

    def test_prune_caps_rows_per_station(self, monkeypatch):
        monkeypatch.setattr(station_events, "MAX_ROWS_PER_STATION", 3)
        station_events.append_events("gmnro09", [
            {"id": f"e{i}", "kind": "uplink", "ts": f"2026-11-0{i + 1}T00:00:00Z"}
            for i in range(5)])
        station_events.prune()
        assert [r["event_id"] for r in station_events.list_events(None)] == ["e4", "e3", "e2"]

    def test_validate_rejects_bad_ids_and_kinds(self):
        v = station_events.validate_station_event
        assert v({"id": "ok-1", "kind": "boot"}) is not None
        assert v({"id": "has space", "kind": "boot"}) is None
        assert v({"id": "x" * 81, "kind": "boot"}) is None
        assert v({"id": "ok", "kind": "Bad-Kind"}) is None
        assert v({"id": "ok", "kind": "offline"}) is None
        assert v({"id": "ok", "kind": "online"}) is None
        assert v({"id": "ok", "kind": "anything_else"}) is None
        assert station_events.check_station_event({"id": "ok", "kind": "offline"})[1] \
            == "kind_reserved_for_vps"
        # Every kind the pusher emits is accepted.
        for kind in ("boot", "uplink", "rescue_wifi", "station_log"):
            assert v({"id": "ok", "kind": kind}) is not None
        assert v("not a dict") is None
        assert v({"id": "ok", "kind": "boot", "ts": "garbage"})["ts"] is None
        assert v({"id": "ok", "kind": "boot", "ts": "2026-11-13T02:00:00+02:00"})["ts"] \
            == "2026-11-13T00:00:00Z"

    def test_oversized_detail_is_truncated(self):
        station_events.append_events("gmnro09", [
            {"id": "big", "kind": "station_log", "ts": None, "detail": "x" * 10_000}])
        detail = station_events.list_events(None)[0]["detail"]
        assert len(detail) < station_events.MAX_DETAIL_CHARS


class TestPresence:
    def test_first_heartbeat_records_no_event(self):
        assert station_events.note_heartbeat("gmnro09", now=1000.0) is False
        assert station_events.list_events(None) == []

    def test_offline_then_online_once_each(self):
        t0 = datetime(2026, 11, 13, 1, 0, tzinfo=timezone.utc).timestamp()
        station_events.note_heartbeat("gmnro09", now=t0)
        assert station_events.mark_offline("gmnro09", "2026-11-13T02:00:00+00:00",
                                           now=t0 + 3900) is True
        # Same silence noticed again (another worker, next cycle): no new row.
        station_events.reset_memos_for_tests()
        assert station_events.mark_offline("gmnro09", "2026-11-13T02:00:00+00:00",
                                           now=t0 + 4000) is False
        assert station_events.note_heartbeat("gmnro09", now=t0 + 7200) is True
        station_events.reset_memos_for_tests()
        assert station_events.note_heartbeat("gmnro09", now=t0 + 7201) is False
        rows = station_events.list_events(["gmnro09"])
        assert [r["kind"] for r in rows] == ["online", "offline"]
        assert rows[1]["ts"] == "2026-11-13T02:00:00Z"
        assert rows[1]["source"] == "vps"
        assert rows[0]["detail"]["offline_since"] == "2026-11-13T02:00:00Z"
        assert rows[0]["detail"]["offline_s"] == 3600

    def test_offline_without_presence_row_still_recorded(self):
        # A station that died before the history existed.
        assert station_events.mark_offline("gmnro03", "2026-11-10T01:00:00Z") is True
        assert station_events.note_heartbeat("gmnro03") is True

    def test_heartbeat_memo_skips_db_inside_window(self, monkeypatch):
        station_events.note_heartbeat("gmnro09", now=1000.0)
        calls = []
        real = station_events.sqlite_writer.run_write
        monkeypatch.setattr(station_events.sqlite_writer, "run_write",
                            lambda *a, **k: calls.append(1) or real(*a, **k))
        station_events.note_heartbeat("gmnro09", now=1045.0)
        assert calls == []
        station_events.note_heartbeat("gmnro09", now=1045.0 + station_events._ONLINE_MEMO_S + 1)
        assert calls == [1]

    def test_two_outages_are_two_offline_events(self):
        station_events.note_heartbeat("gmnro09", now=1000.0)
        station_events.mark_offline("gmnro09", "2026-11-13T02:00:00Z", now=2000.0)
        station_events.note_heartbeat("gmnro09", now=3000.0)
        station_events.mark_offline("gmnro09", "2026-11-14T02:00:00Z", now=4000.0)
        kinds = [r["kind"] for r in station_events.list_events(["gmnro09"])]
        assert kinds.count("offline") == 2 and kinds.count("online") == 1


# ── ingest ──────────────────────────────────────────────────────────────────


class _Cache:
    def __init__(self) -> None:
        self.status: dict = {}

    def set_status(self, host, data):
        self.status[host] = data

    def set_vitals(self, host, data):
        pass


@pytest.fixture
def ingest(tmp_path, monkeypatch):
    monkeypatch.setenv("ROVIMEN_STATION_KEYS_REQUIRED", "1")
    station_keys._invalidate_cache()
    keys = tmp_path / "keys.yaml"
    _, secret = station_keys.add_key("gmnro09", path=keys)
    station_keys._invalidate_cache()
    cache = _Cache()
    app = Flask(__name__)
    app.testing = True
    ingest_api.register_ingest_routes(
        app, cache_set_status=cache.set_status, cache_set_vitals=cache.set_vitals,
        known_stations=lambda: {"gmnro09", "gmnro03"},
        detections_db_path=tmp_path / "det.db", station_state_path=tmp_path / "state.db",
        station_keys_path=keys, limiter=None,
    )
    yield SimpleNamespace(client=app.test_client(), secret=secret, cache=cache)
    station_keys._invalidate_cache()


def _post(ing, path, seq, payload, secret=None):
    return ing.client.post(
        f"/api/ingest/v1/gmnro09/{path}",
        data=json.dumps({"seq": seq, "sent_at": "2026-11-21T20:00:00Z", "payload": payload}),
        headers={"X-Station-Key": secret or ing.secret, "Content-Type": "application/json"})


class TestIngest:
    def test_old_heartbeat_without_new_fields_still_accepted(self, ingest):
        r = _post(ingest, "heartbeat", 1, {"online": True, "services": {}})
        assert r.status_code == 200 and not r.get_json()["dropped"]
        st = ingest.cache.status["gmnro09"]
        assert st["disk_pressure"] is None and st["boot"] is None

    def test_new_heartbeat_fields_reach_the_cache(self, ingest):
        pressure = {"since": "2026-10-16T21:50:00Z", "filesystem": "/", "pct": 99}
        r = _post(ingest, "heartbeat", 1, {
            "online": True, "disk_pressure": pressure,
            "capture_fallback": {"configured": "/mnt/c", "active": "/home/gmn/c"},
            "uplink": {"class": "ok", "since": "2026-11-21T19:00:00Z"},
            "boot": {"boot_id": "b1", "uptime_s": 60, "previous": {"end": "unclean"}},
        })
        assert r.status_code == 200
        st = ingest.cache.status["gmnro09"]
        assert st["disk_pressure"] == pressure
        assert st["capture_fallback"]["active"] == "/home/gmn/c"
        assert st["uplink"]["class"] == "ok"
        assert st["boot"]["previous"]["end"] == "unclean"

    def test_events_stored_and_redelivery_idempotent(self, ingest):
        events = [
            {"id": "ul-1", "ts": "2026-11-13T02:00:00Z", "kind": "uplink",
             "detail": {"text": "uplink ok -> isp after 3 h 0 min"}},
            {"id": "boot-b1", "ts": "2026-11-13T05:00:00Z", "kind": "boot",
             "detail": {"text": "booted; the previous boot ended abruptly", "down_s": 600}},
        ]
        r = _post(ingest, "events", 5, {"events": events})
        body = r.get_json()
        assert r.status_code == 200 and body["inserted"] == 2 and body["duplicates"] == 0
        # Redelivered with an older seq after a lost answer: still 200, no doubles.
        r = _post(ingest, "events", 3, {"events": events})
        body = r.get_json()
        assert r.status_code == 200 and body["inserted"] == 0 and body["duplicates"] == 2
        rows = station_events.list_events(["gmnro09"])
        assert [r["kind"] for r in rows] == ["boot", "uplink"]
        assert all(r["source"] == "station" for r in rows)

    def test_malformed_event_skipped_not_batch(self, ingest):
        r = _post(ingest, "events", 1, {"events": [
            {"id": "bad id", "kind": "uplink"}, {"id": "ul-2", "kind": "uplink"}]})
        body = r.get_json()
        assert r.status_code == 200 and body["inserted"] == 1 and body["rejected"] == 1

    def test_events_need_the_station_key(self, ingest):
        r = ingest.client.post("/api/ingest/v1/gmnro09/events",
                               data=json.dumps({"seq": 1, "payload": {"events": []}}),
                               headers={"Content-Type": "application/json"})
        assert r.status_code == 401
        r = ingest.client.post("/api/ingest/v1/gmnro03/events",
                               data=json.dumps({"seq": 1, "payload": {"events": []}}),
                               headers={"X-Station-Key": ingest.secret,
                                        "Content-Type": "application/json"})
        assert r.status_code == 403

    def test_vps_kinds_and_unknown_kinds_rejected_per_event(self, ingest):
        r = _post(ingest, "events", 1, {"events": [
            {"id": "fake-off", "kind": "offline", "ts": "2026-11-13T02:00:00Z"},
            {"id": "fake-on", "kind": "online"},
            {"id": "what", "kind": "tailscale_admin"},
            {"id": "ul-3", "kind": "uplink", "detail": {"text": "uplink ok"}},
        ]})
        body = r.get_json()
        assert r.status_code == 200
        assert body["inserted"] == 1 and body["rejected"] == 3
        by_id = {e["id"]: e for e in body["rejected_events"]}
        assert by_id["fake-off"]["status"] == 422
        assert by_id["fake-off"]["error"] == "kind_reserved_for_vps"
        assert by_id["fake-on"]["error"] == "kind_reserved_for_vps"
        assert by_id["what"]["error"] == "unknown_kind"
        rows = station_events.list_events(["gmnro09"])
        assert [(r["kind"], r["source"]) for r in rows] == [("uplink", "station")]

    def test_key_writes_only_its_own_station(self, ingest):
        # Station A's key posting to station B's path is refused outright...
        r = ingest.client.post(
            "/api/ingest/v1/gmnro03/events",
            data=json.dumps({"seq": 1, "payload": {"events": [
                {"id": "ul-x", "kind": "uplink"}]}}),
            headers={"X-Station-Key": ingest.secret, "Content-Type": "application/json"})
        assert r.status_code == 403
        # ...and naming B (or a source) inside the payload changes nothing.
        r = _post(ingest, "events", 2, {"station": "gmnro03", "events": [
            {"id": "ul-y", "kind": "uplink", "station": "gmnro03", "host_key": "gmnro03",
             "source": "vps"}]})
        assert r.get_json()["inserted"] == 1
        assert station_events.list_events(["gmnro03"]) == []
        [row] = station_events.list_events(["gmnro09"])
        assert row["event_id"] == "ul-y" and row["source"] == "station"

    def test_station_cannot_suppress_the_vps_offline_record(self, ingest):
        _post(ingest, "heartbeat", 1, {"online": True})
        last_seen = "2026-11-13T02:00:00Z"
        # The station guesses the id the VPS will use and sends it first:
        # the ingest path refuses the reserved prefix...
        guessed = f"vps-offline-{last_seen}"
        r = _post(ingest, "events", 2, {"events": [
            {"id": guessed, "kind": "uplink", "ts": last_seen},
            {"id": "VPS-online-x", "kind": "uplink"}]})
        body = r.get_json()
        assert body["inserted"] == 0 and body["rejected"] == 2
        assert {e["error"] for e in body["rejected_events"]} == {"id_reserved_for_vps"}
        assert station_events.list_events(["gmnro09"]) == []
        # ...and even a station row with that id (written past the check)
        # cannot pre-empt the VPS row, because source is part of the key.
        assert station_events.append_events(
            "gmnro09", [{"id": guessed, "kind": "uplink", "ts": last_seen}]) == (1, 0, [])
        assert station_events.mark_offline("gmnro09", last_seen) is True
        rows = station_events.list_events(["gmnro09"])
        assert sorted((r["source"], r["kind"]) for r in rows) == [
            ("station", "uplink"), ("vps", "offline")]
        assert {r["event_id"] for r in rows} == {f"vps-offline-{last_seen}"}

    def test_oversized_detail_is_capped(self, ingest):
        _post(ingest, "events", 1, {"events": [
            {"id": "big", "kind": "station_log", "detail": {"text": "y" * 50_000}}]})
        stored = station_events.list_events(["gmnro09"])[0]["detail"]
        assert len(json.dumps(stored)) <= station_events.MAX_DETAIL_CHARS + 10

    def test_too_many_events_is_422(self, ingest):
        r = _post(ingest, "events", 1, {"events": [{"id": f"e{i}", "kind": "uplink"}
                                                   for i in range(201)]})
        assert r.status_code == 422

    def test_heartbeat_after_offline_records_online(self, ingest):
        _post(ingest, "heartbeat", 1, {"online": True})
        station_events.mark_offline("gmnro09", "2026-11-13T02:00:00Z")
        station_events.reset_memos_for_tests()
        _post(ingest, "heartbeat", 2, {"online": True})
        kinds = [r["kind"] for r in station_events.list_events(["gmnro09"])]
        assert kinds == ["online", "offline"]

    def test_event_store_failure_never_costs_the_heartbeat(self, ingest, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("disk full")
        monkeypatch.setattr(station_events, "note_heartbeat", boom)
        assert _post(ingest, "heartbeat", 1, {"online": True}).status_code == 200


# ── offline mark from the status cycle ──────────────────────────────────────


def _status_poll_fn(config, cache):
    targets = []

    def capture(*args, **kwargs):
        targets.append(kwargs.get("target") or (args[0] if args else None))
        return MagicMock()

    with patch("station_client.threading") as mock_threading:
        mock_threading.Thread.side_effect = capture
        station_client.start_polling(config, MagicMock(), cache)
    return targets[0]


def _run_once(fn) -> None:
    with patch("station_client.time") as mock_time:
        mock_time.sleep.side_effect = StopIteration("done")
        with pytest.raises(StopIteration):
            fn()


class TestDemotionRecordsOffline:
    def _config(self):
        return DashboardConfig(stations={
            "pusher": StationConfig(ip="100.0.0.2", label="Pusher", push_enabled=True)})

    def test_stale_push_station_gets_one_offline_event(self):
        last = (datetime.now(timezone.utc) - timedelta(seconds=1200)).isoformat()
        cache = MagicMock()
        cache.get_status.return_value = {"online": True, "received_at": last}
        _run_once(_status_poll_fn(self._config(), cache))
        rows = station_events.list_events(["pusher"])
        assert [r["kind"] for r in rows] == ["offline"]
        assert rows[0]["detail"]["last_seen"] == station_events.normalize_ts(last)

    def test_already_demoted_entry_backfills_offline_once(self):
        last = (datetime.now(timezone.utc) - timedelta(seconds=9000)).isoformat()
        cache = MagicMock()
        cache.get_status.return_value = {"online": False, "stale": True, "received_at": last}
        poll = _status_poll_fn(self._config(), cache)
        _run_once(poll)
        _run_once(poll)
        assert [r["kind"] for r in station_events.list_events(["pusher"])] == ["offline"]
        cache.set_status.assert_not_called()

    def test_fresh_station_records_nothing(self):
        last = datetime.now(timezone.utc).isoformat()
        cache = MagicMock()
        cache.get_status.return_value = {"online": True, "received_at": last}
        _run_once(_status_poll_fn(self._config(), cache))
        assert station_events.list_events(None) == []


# ── read endpoint ───────────────────────────────────────────────────────────


@pytest.fixture
def reader():
    from routes.station_history import register_station_history_routes

    app = Flask(__name__)
    app.secret_key = "test"
    app.testing = True
    config = SimpleNamespace(stations={"gmnro09": object(), "gmnro03": object()})
    register_station_history_routes(app, config)
    station_events.append_events("gmnro09", [{"id": "a", "kind": "boot", "ts": "2026-11-13T05:00:00Z"}])
    station_events.append_events("gmnro03", [{"id": "b", "kind": "uplink", "ts": "2026-11-14T05:00:00Z"}])
    return app.test_client()


def _login(client, role, stations=()):
    with client.session_transaction() as sess:
        sess["user"] = "someone"
        sess["role"] = role
        sess["stations"] = list(stations)


class TestReadAccess:
    def test_anonymous_is_401(self, reader):
        assert reader.get("/api/station_events/gmnro09").status_code == 401
        assert reader.get("/api/station_events").status_code == 401

    def test_admin_sees_any_station_and_all(self, reader):
        _login(reader, "admin")
        r = reader.get("/api/station_events/gmnro03")
        assert r.status_code == 200 and r.get_json()["events"][0]["event_id"] == "b"
        assert r.get_json()["events"][0]["source"] == "station"
        assert r.headers["Cache-Control"] == "private, no-store"
        assert reader.get("/api/station_events").get_json()["count"] == 2

    def test_host_sees_only_own_station(self, reader):
        _login(reader, "host", ["gmnro09"])
        assert reader.get("/api/station_events/gmnro09").status_code == 200
        assert reader.get("/api/station_events/gmnro03").status_code == 403
        rows = reader.get("/api/station_events").get_json()["events"]
        assert [r["station"] for r in rows] == ["gmnro09"]

    def test_visitor_and_press_are_403(self, reader):
        for role in ("visitor", "press"):
            _login(reader, role, ["gmnro09"])
            assert reader.get("/api/station_events/gmnro09").status_code == 403
            assert reader.get("/api/station_events").status_code == 403

    def test_unknown_station_404_and_bad_params_400(self, reader):
        _login(reader, "admin")
        assert reader.get("/api/station_events/nosuch").status_code == 404
        assert reader.get("/api/station_events/gmnro09?limit=x").status_code == 400
        assert reader.get("/api/station_events/gmnro09?since=yesterday").status_code == 400
        r = reader.get("/api/station_events/gmnro09?since=2026-11-14")
        assert r.get_json()["count"] == 0

    def test_not_on_the_public_api(self):
        import public_api
        assert "station_events" not in Path(public_api.__file__).read_text()


# ── CLI ─────────────────────────────────────────────────────────────────────


def test_cli_prints_history(capsys):
    sys.path.insert(0, str(ROOT / "tools"))
    try:
        import station_event_history as cli
    finally:
        sys.path.remove(str(ROOT / "tools"))
    station_events.append_events("gmnro09", [{"id": "a", "kind": "boot", "ts": "2026-11-13T05:00:00Z",
                                              "detail": {"text": "booted; host down 2 h 0 min"}}])
    assert cli.main(["gmnro09"]) == 0
    out = capsys.readouterr().out
    assert "2026-11-13 05:00:00" in out and "booted; host down 2 h 0 min" in out
    assert cli.main(["--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["event_id"] == "a"


def test_station_kinds_cover_what_the_pusher_emits():
    import rovimen_pusher as rp
    lines = ["2026-11-13T02:00:00Z uplink ok -> isp", "2026-11-13T02:00:00Z rescue wifi up",
             "2026-11-13T02:00:00Z something else"]
    kinds = {rp.parse_uplink_log_line(line)["kind"] for line in lines} | {"boot"}
    assert kinds == set(station_events.STATION_KINDS)
    assert not kinds & station_events.VPS_KINDS
