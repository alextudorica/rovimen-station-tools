"""Per-camera capture state: is a camera recording, from the FF files RMS wrote.

On 2026-11-04 at 10:00 UTC the overview read "59 cameras capturing" in
daylight: it counted every registry camera of a station with any rms-* unit
up, including gmnro13 (Drăgșina), offline since 2026-09-30, and RO0015 (Iași)
and RO001S (Ferigile), which wrote no FF file for weeks with their units
active. The station now reports per-camera FF evidence
(station_collect.summarise_capture) and the dashboard classifies it
(capture_state.camera_capture_state).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import station_collect as sc

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "dashboard"))
import capture_state as cs  # noqa: E402

# Cluj (gmnro06): night of 2026-11-03 ran 15:58:41-04:20:20 UTC (RMS log).
LAT, LON = 46.47, 23.19
NIGHT = datetime(2026, 11, 3, 22, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc)


def _ffs(start: datetime, count: int, period: float = 10.24, code: str = "RO000R") -> list[str]:
    return [
        f"FF_{code}_{(start + timedelta(seconds=period * i)).strftime('%Y%m%d_%H%M%S')}_000_{i * 256:07d}.fits"
        for i in range(count)
    ]


def _iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


# ── Station side ───────────────────────────────────────────────────────────


class TestSummariseCapture:
    def test_full_cadence_counts_window_and_night(self):
        start = datetime(2026, 11, 3, 15, 59, 34, tzinfo=timezone.utc)
        n = int((NIGHT - start).total_seconds() / 10.24)
        names = _ffs(start, n) + ["CALSTARS_RO000R.txt", "FS_RO000R_x.bin"]
        b = sc.summarise_capture(names, NIGHT, 25.0, start, "night", None)
        assert b["ff_window_expected"] == 175
        assert 174 <= b["ff_window"] <= 176
        assert b["nights"] == {"20261103": n}
        assert b["newest_ff"] <= _iso(NIGHT)
        assert b["rms_state"] == "night" and b["guard"] is None

    def test_post_midnight_files_count_for_the_evening_night(self):
        start = datetime(2026, 11, 3, 23, 50, tzinfo=timezone.utc)
        b = sc.summarise_capture(_ffs(start, 200), start + timedelta(hours=1), 25.0, start, "night", None)
        assert b["nights"] == {"20261103": 200}

    def test_expected_is_limited_to_how_long_the_session_ran(self):
        start = NIGHT - timedelta(minutes=5)
        b = sc.summarise_capture(_ffs(start, 29), NIGHT, 25.0, start, "night", None)
        assert b["ff_window_expected"] == 29

    def test_future_names_are_ignored(self):
        names = _ffs(NIGHT + timedelta(hours=3), 5)
        b = sc.summarise_capture(names, NIGHT, 25.0, None, None, None)
        assert b["newest_ff"] is None and b["nights"] == {}

    def test_keeps_the_two_newest_nights(self):
        names = (_ffs(datetime(2026, 11, 1, 20, tzinfo=timezone.utc), 3)
                 + _ffs(datetime(2026, 11, 2, 20, tzinfo=timezone.utc), 4)
                 + _ffs(datetime(2026, 11, 3, 20, tzinfo=timezone.utc), 5))
        b = sc.summarise_capture(names, NIGHT, 25.0, None, None, None)
        assert b["nights"] == {"20261103": 5, "20261102": 4}


class TestNightCountsOutliveTheirFiles:
    """2026-11-12: RO000T (Vâlcele) read "0 FF, none" for night 1111 once the
    janitor had freed its CapturedFiles session, though the night was
    captured in full (4,450 FF) and uploaded to GMN."""

    EVENING = datetime(2026, 11, 12, 4, 30, tzinfo=timezone.utc)
    LATER = datetime(2026, 11, 12, 18, 30, tzinfo=timezone.utc)

    def test_a_freed_session_keeps_its_count(self, tmp_path):
        path = tmp_path / "counts.json"
        assert sc.remember_night_counts("RO000T", {"20261111": 4450}, self.EVENING, path) == {"20261111": 4450}
        # The session is freed: only tonight's few files remain on disk.
        got = sc.remember_night_counts("RO000T", {"20261112": 950}, self.LATER, path)
        assert got == {"20261112": 950, "20261111": 4450}

    def test_a_shrinking_count_never_lowers_the_record(self, tmp_path):
        path = tmp_path / "counts.json"
        sc.remember_night_counts("RO000T", {"20261111": 4450}, self.EVENING, path)
        got = sc.remember_night_counts("RO000T", {"20261111": 11}, self.LATER, path)
        assert got["20261111"] == 4450

    def test_counts_survive_a_restart(self, tmp_path):
        path = tmp_path / "counts.json"
        sc.remember_night_counts("RO000T", {"20261111": 4450}, self.EVENING, path)
        sc._capture_counts["data"] = None                       # a new process
        assert sc.remember_night_counts("RO000T", {}, self.LATER, path) == {"20261111": 4450}

    def test_writes_are_throttled_but_merge_the_other_process(self, tmp_path):
        path = tmp_path / "counts.json"
        sc.remember_night_counts("RO000T", {"20261111": 100}, self.EVENING, path)
        # The pusher (another process) saved a camera this one has not seen.
        on_disk = json.loads(path.read_text())
        on_disk["RO0014"] = {"20261111": 4671}
        path.write_text(json.dumps(on_disk))
        soon = self.EVENING + timedelta(seconds=60)
        sc.remember_night_counts("RO000T", {"20261111": 200}, soon, path)
        assert json.loads(path.read_text())["RO000T"]["20261111"] == 100      # not yet
        later = self.EVENING + timedelta(seconds=sc.CAPTURE_COUNTS_SAVE_S + 1)
        sc.remember_night_counts("RO000T", {"20261111": 300}, later, path)
        saved = json.loads(path.read_text())
        assert saved == {"RO000T": {"20261111": 300}, "RO0014": {"20261111": 4671}}

    def test_old_nights_are_dropped_and_two_reported(self, tmp_path):
        path = tmp_path / "counts.json"
        nights = {"20261101": 4000, "20261110": 4400, "20261111": 4450}
        got = sc.remember_night_counts("RO000T", nights, self.EVENING, path)
        assert got == {"20261111": 4450, "20261110": 4400}
        assert "20261101" not in json.loads(path.read_text())["RO000T"]


class TestRmsStateFromLog:
    def test_continuous_capture_mode_switch_with_its_time(self):
        lines = [
            "2026/11/03 13:45:41-INFO-CaptureModeSwitcher-line:104 - Next event is a sunset "
            "(2026-11-03 15:58:40.844808), switching to daytime mode",
            "2026/11/03 15:58:40-INFO-CaptureModeSwitcher-line:121 - Next event is a sunrise "
            "(2026-11-04 04:20:20.611295), switching to nighttime mode",
            "2026/11/03 15:58:43-INFO-BufferedCapture-line:2315 - Day->Night transition detected",
        ]
        assert sc.rms_state_from_lines(lines) == ("night", "2026-11-03T15:58:40Z")

    def test_scheduled_capture_markers(self):
        assert sc.rms_state_from_lines(
            ["2026/11/04 04:20:00-INFO-StartCapture - Ending capture..."])[0] == "ended"
        assert sc.rms_state_from_lines(
            ["2026/11/04 06:00:00-INFO-StartCapture - Waiting 9:41:12 to start recording for 12.1 hrs"]
        )[0] == "waiting"

    def test_no_marker_keeps_the_previous_state(self):
        prev = ("night", "2026-11-03T15:58:40Z")
        assert sc.rms_state_from_lines(["2026/11/03 22:00:00-INFO-x - Grabbing a new block"], prev) == prev

    def test_incremental_scan_remembers_a_switch_older_than_any_tail(self, tmp_path):
        log = tmp_path / "log_RO000R_20261103_134538_001.log"
        log.write_text("2026/11/03 15:58:40-INFO-CaptureModeSwitcher - switching to nighttime mode\n")
        sc._rms_log_scan.clear()
        assert sc._rms_log_state(log)[0] == "night"
        with log.open("a") as f:
            f.write("2026/11/03 16:00:00-INFO-x - filler\n" * 50000)
        assert sc._rms_log_state(log) == ("night", "2026-11-03T15:58:40Z")
        with log.open("a") as f:
            f.write("2026/11/04 04:20:20-INFO-CaptureModeSwitcher - switching to daytime mode\n")
        assert sc._rms_log_state(log)[0] == "day"


# ── Dashboard side ─────────────────────────────────────────────────────────


def _status(now: datetime, block: dict | None, code: str = "RO000R") -> dict:
    st = {"online": True, "last_updated": _iso(now)}
    if block is not None:
        st["capture"] = {code: block}
    return st


def _block(newest: datetime | None, got: int = 175, want: int = 175, state: str | None = "night",
           guard: str | None = None, nights: dict | None = None, since: str | None = None) -> dict:
    return {"newest_ff": _iso(newest) if newest else None, "ff_window": got,
            "ff_window_expected": want, "window_min": 30, "ff_period_s": 10.24,
            "rms_state": state, "rms_state_since": since, "guard": guard,
            "nights": nights or {}}


def _classify(now: datetime, status: dict | None, online: bool = True) -> dict:
    return cs.camera_capture_state("RO000R", status, online=online, now=now, lat=LAT, lon=LON)


class TestCameraCaptureState:
    def test_recording(self):
        r = _classify(NIGHT, _status(NIGHT, _block(NIGHT - timedelta(seconds=8))))
        assert r["state"] == "recording"

    def test_low_yield_is_degraded(self):
        r = _classify(NIGHT, _status(NIGHT, _block(NIGHT - timedelta(seconds=8), got=40)))
        assert r["state"] == "degraded"

    def test_stall_guard_lossy_verdict_is_degraded(self):
        # RO001N (Brăila), 2026-11-03: 40-60% ping loss all night, DFR 98.7%.
        r = _classify(NIGHT, _status(NIGHT, _block(NIGHT - timedelta(seconds=8), guard="lossy")))
        assert r["state"] == "degraded"

    def test_no_ff_in_night_mode_is_stalled(self):
        # RO0015 (Iași): RMS active, camera off the wire, 0 FF files.
        r = _classify(NIGHT, _status(NIGHT, _block(None, got=0, since="2026-11-03T15:58:40Z")))
        assert r["state"] == "stalled"

    def test_first_minutes_of_night_capture_are_not_a_stall(self):
        now = datetime(2026, 11, 3, 16, 0, tzinfo=timezone.utc)
        r = _classify(now, _status(now, _block(None, got=0, since="2026-11-03T15:58:40Z")))
        assert r["state"] == "idle"

    def test_daytime_is_idle_and_reports_last_night(self):
        r = _classify(DAY, _status(DAY, _block(datetime(2026, 11, 4, 4, 20, tzinfo=timezone.utc),
                                                state="day", nights={"20261103": 4337})))
        assert r["state"] == "idle"
        ln = r["last_night"]
        assert ln["night"] == "20261103" and ln["n_ff"] == 4337
        assert 4300 <= ln["expected"] <= 4400
        assert ln["verdict"] == "full"

    def test_daytime_with_no_ff_last_night(self):
        r = _classify(DAY, _status(DAY, _block(None, state="day")))
        assert r["state"] == "idle" and r["last_night"]["verdict"] == "none"

    def test_partial_night(self):
        r = _classify(DAY, _status(DAY, _block(None, state="day", nights={"20261103": 3775})))
        assert r["last_night"]["verdict"] == "partial"

    def test_offline_station(self):
        # gmnro13 (Drăgșina): last report 2026-09-30, services still read active.
        old = {"online": True, "last_updated": "2026-09-30T07:52:37Z", "capture": {}}
        assert _classify(NIGHT, old)["state"] == "offline"
        assert _classify(NIGHT, _status(NIGHT, _block(NIGHT)), online=False)["state"] == "offline"

    def test_old_bundle_without_capture_is_unknown(self):
        assert _classify(NIGHT, _status(NIGHT, None))["state"] == "unknown"

    def test_camera_without_rms(self):
        # GH662W (Ghirdoveni) is in the registry with no RMS unit.
        st = _status(NIGHT, _block(NIGHT), code="RO000A")
        assert _classify(NIGHT, st)["state"] == "no_rms"

    def test_without_rms_state_the_sun_decides(self):
        assert _classify(NIGHT, _status(NIGHT, _block(None, state=None)))["state"] == "stalled"
        assert _classify(DAY, _status(DAY, _block(None, state=None)))["state"] == "idle"


@pytest.mark.parametrize("now,alt_above,night", [
    (datetime(2026, 11, 4, 10, 0, tzinfo=timezone.utc), True, "20261103"),
    (datetime(2026, 11, 4, 13, 0, tzinfo=timezone.utc), True, "20261103"),
    (datetime(2026, 11, 4, 22, 0, tzinfo=timezone.utc), False, "20261103"),
    (datetime(2026, 11, 5, 3, 0, tzinfo=timezone.utc), False, "20261103"),
])
def test_last_finished_night(now, alt_above, night):
    alt = cs.sun_altitude_deg(LAT, LON, now)
    assert (alt > -9.0) == alt_above
    assert cs.last_finished_night(now, alt) == night
