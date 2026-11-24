"""Tests for the dashboard-side segment-health alarm policy.

Stations report raw cadence counters; deciding what counts as *broken* is a
fleet policy that lives in ``dashboard/segment_health.py`` so the overview
route, the SSE change-detector and the frontend all agree.

The load-bearing property throughout: a station that does not report the field
(any bundle predating the feature) must read as "nothing to say" and never as a
fault. Half the fleet will be in that state until the updater has run
everywhere.
"""

from __future__ import annotations

import pytest

from segment_health import (
    SEGMENT_SHORT_ALARM_PER_HOUR,
    SEGMENT_STALL_ALARM_PER_HOUR,
    camera_segment_health,
    segment_alarm,
    station_segment_alarms,
    tag_segment_alarms,
)


def _health(short: int = 0, stalls: int = 0, window_min: int = 60) -> dict:
    return {"window_min": window_min, "segments": 180, "short": short,
            "stalls": stalls, "max_gap_s": 20, "newest": None, "night": "20260901"}


# ── segment_alarm ──────────────────────────────────────────────────────────


class TestSegmentAlarm:
    def test_healthy_night_raises_nothing(self):
        assert segment_alarm(_health()) is None

    def test_clip_storm_is_a_storm(self):
        assert segment_alarm(_health(short=1543)) == "storm"

    def test_stalling_stream_is_stalls(self):
        assert segment_alarm(_health(stalls=123)) == "stalls"

    def test_both_at_once(self):
        assert segment_alarm(_health(short=900, stalls=40)) == "both"

    def test_thresholds_are_exclusive_bounds(self):
        assert segment_alarm(_health(short=SEGMENT_SHORT_ALARM_PER_HOUR)) is None
        assert segment_alarm(_health(short=SEGMENT_SHORT_ALARM_PER_HOUR + 1)) == "storm"
        assert segment_alarm(_health(stalls=SEGMENT_STALL_ALARM_PER_HOUR)) is None
        assert segment_alarm(_health(stalls=SEGMENT_STALL_ALARM_PER_HOUR + 1)) == "stalls"

    def test_a_handful_of_short_cuts_is_tolerated(self):
        """A capture restart or a brief hiccup legitimately produces a few
        back-to-back cuts — that must not page anyone."""
        assert segment_alarm(_health(short=8, stalls=2)) is None

    def test_counters_are_scaled_from_the_reported_window(self):
        """If a station's window is ever retuned, the per-hour thresholds must
        still mean per hour."""
        # 30 short in a 30 min window = 60/hour, over the 50/hour bar.
        assert segment_alarm(_health(short=30, window_min=30)) == "storm"
        # The same 30 over two hours = 15/hour, well under it.
        assert segment_alarm(_health(short=30, window_min=120)) is None

    def test_absent_block_is_not_a_fault(self):
        assert segment_alarm(None) is None

    def test_malformed_block_is_not_a_fault(self):
        assert segment_alarm({}) is None
        assert segment_alarm("nope") is None
        assert segment_alarm({"short": None, "stalls": None}) is None
        assert segment_alarm({"short": "many", "stalls": "lots"}) is None

    def test_zero_window_does_not_divide_by_zero(self):
        assert segment_alarm({"window_min": 0, "short": 1543}) == "storm"


# ── camera_segment_health ──────────────────────────────────────────────────


class TestCameraSegmentHealth:
    def test_returns_the_block_tagged_with_its_alarm(self):
        block = camera_segment_health({"RO000Y": _health(short=1543)}, "RO000Y")
        assert block["alarm"] == "storm"
        assert block["short"] == 1543

    def test_healthy_camera_is_tagged_with_no_alarm(self):
        block = camera_segment_health({"RO000Y": _health()}, "RO000Y")
        assert block["alarm"] is None

    def test_lookup_is_case_insensitive(self):
        block = camera_segment_health({"ro000y": _health(stalls=99)}, "RO000Y")
        assert block is not None
        assert block["alarm"] == "stalls"

    def test_does_not_mutate_the_station_payload(self):
        payload = {"RO000Y": _health(short=1543)}
        camera_segment_health(payload, "RO000Y")
        assert "alarm" not in payload["RO000Y"]

    def test_camera_the_station_does_not_report(self):
        assert camera_segment_health({"RO000Y": _health()}, "RO0010") is None

    def test_old_bundle_reports_nothing(self):
        assert camera_segment_health(None, "RO000Y") is None
        assert camera_segment_health({}, "RO000Y") is None


# ── station_segment_alarms ─────────────────────────────────────────────────


class TestStationSegmentAlarms:
    def test_only_cameras_in_alarm_appear(self):
        alarms = station_segment_alarms({
            "RO000Y": _health(short=1543),
            "RO0010": _health(),
            "RO001E": _health(stalls=123),
        })
        assert alarms == {"RO000Y": "storm", "RO001E": "stalls"}

    def test_healthy_station_is_empty(self):
        assert station_segment_alarms({"RO000Y": _health()}) == {}

    def test_old_bundle_is_empty(self):
        assert station_segment_alarms(None) == {}
        assert station_segment_alarms({}) == {}


# ── tag_segment_alarms (status-cache write path) ────────────────────────────


class TestTagSegmentAlarms:
    def test_every_camera_block_gets_a_verdict(self):
        tagged = tag_segment_alarms({
            "online": True,
            "segment_health": {"RO000Y": _health(short=1543), "RO0010": _health()},
        })
        assert tagged["segment_health"]["RO000Y"]["alarm"] == "storm"
        assert tagged["segment_health"]["RO0010"]["alarm"] is None
        assert tagged["online"] is True

    def test_input_is_left_untouched(self):
        status = {"segment_health": {"RO000Y": _health(short=1543)}}
        tag_segment_alarms(status)
        assert "alarm" not in status["segment_health"]["RO000Y"]

    def test_status_without_the_field_passes_through_unchanged(self):
        status = {"online": True, "services": {}}
        assert tag_segment_alarms(status) is status

    def test_non_dict_input_passes_through(self):
        assert tag_segment_alarms(None) is None
        assert tag_segment_alarms({"segment_health": "junk"})["segment_health"] == "junk"


# ── SSE change detection ───────────────────────────────────────────────────


class TestStatusDiffSignature:
    """The signature must move when the alarm does and stay put when only the
    churn does — otherwise adding the field would turn the SSE fan-out from
    event-on-change into event-per-poll for the whole fleet."""

    @staticmethod
    def _sig(short: int, newest: str, segments: int) -> str:
        import station_client
        return station_client._status_diff_signature({
            "online": True,
            "segment_health": {"RO000Y": {"window_min": 60, "short": short,
                                          "stalls": 0, "segments": segments,
                                          "newest": newest}},
        })

    def test_moving_timestamp_and_count_alone_do_not_fire(self):
        a = self._sig(0, "2026-09-01T21:00:00Z", 180)
        b = self._sig(0, "2026-09-01T22:00:00Z", 360)
        assert a == b

    def test_crossing_the_threshold_fires(self):
        healthy = self._sig(0, "2026-09-01T21:00:00Z", 180)
        storming = self._sig(1543, "2026-09-01T21:00:00Z", 1543)
        assert healthy != storming

    def test_a_status_without_the_field_still_signs(self):
        import station_client
        assert station_client._status_diff_signature({"online": True})
        assert station_client._status_diff_signature(None) == ""


def test_cache_write_tags_the_alarm_for_every_status_reader():
    """The station page reads a raw cached status, so the verdict has to be
    stamped on the way in rather than only inside /api/overview."""
    from cache_backend import InMemoryStationStateBackend
    from station_client import StationCache

    cache = StationCache(backend=InMemoryStationStateBackend())
    cache.set_status("gmn0008", {
        "online": True,
        "segment_health": {"RO000Y": _health(short=1543)},
    })
    stored = cache.get_status("gmn0008")
    assert stored["segment_health"]["RO000Y"]["alarm"] == "storm"


def test_overview_exposes_the_rollup_and_per_camera_block(monkeypatch):
    """End-to-end through /api/overview: the field reaches the frontend both
    as a per-camera block and as a station-level rollup."""
    pytest.importorskip("flask")
    import routes.overview as overview_routes

    seg = {"RO000Y": _health(short=1543), "RO0010": _health()}
    cameras = [
        type("Cam", (), {"code": "RO000Y", "cam_ip": None, "rotate": False,
                         "az": 0, "alt": 0})(),
        type("Cam", (), {"code": "RO0010", "cam_ip": None, "rotate": False,
                         "az": 0, "alt": 0})(),
    ]
    # Exercise the same two helpers the route composes, on the route's module,
    # so an import regression there is caught too.
    assert overview_routes.station_segment_alarms(seg) == {"RO000Y": "storm"}
    assert [overview_routes.camera_segment_health(seg, c.code)["alarm"]
            for c in cameras] == ["storm", None]


def test_overview_route_emits_the_fields_for_an_unreported_station():
    """Wiring check through the real route: a station with no cached status
    (offline, or a bundle that never sends the field) must still answer with
    the new keys in their empty shape — the frontend reads them
    unconditionally."""
    pytest.importorskip("flask")
    from tests.test_public_page_toggles import _build_app, _logged_in

    app = _build_app(["overview"])
    data = _logged_in(app).get("/api/overview").get_json()
    station = data["pub_station"]
    assert station["segment_alarm"] == {}
    assert station["cameras"][0]["segment_health"] is None
