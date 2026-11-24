"""camera_fps_guard: RMS block timing -> slow camera -> fix_cam_encoding --only.

DE001E (Johannismühle) ran at ~12 fps all night on 2026-09-27: RMS blocks of
256 frames arrived ~21-22 s apart instead of 10.24 s, and nothing re-applied the
camera's settings until the next boot run of fix_cam_encoding.sh.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import camera_fps_guard as g

# Johannismühle; 22:00 UTC on 2026-09-27 is deep night there.
LAT, LON = 52.07, 13.76
NIGHT = datetime(2026, 9, 27, 22, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 9, 28, 11, 0, tzinfo=timezone.utc)


def _blocks(end: datetime, gap_s: float, n: int = 20) -> list[str]:
    t0 = end - timedelta(seconds=gap_s * (n - 1))
    return [
        f"{(t0 + timedelta(seconds=gap_s * i)).strftime('%Y/%m/%d %H:%M:%S')}"
        f"-INFO-BufferedCapture-line:1948 - Grabbing a new block of 256 frames..."
        for i in range(n)
    ]


def _camera(tmp_path: Path, code: str, gap_s: float, now: datetime,
            mode_lines: tuple[str, ...] = ()) -> g.RmsCamera:
    rms_dir = tmp_path / f"RMS_{code}"
    data = tmp_path / "RMS_data" / code
    (data / "logs").mkdir(parents=True)
    rms_dir.mkdir()
    cfg = rms_dir / ".config"
    cfg.write_text(f"[System]\nstationID: {code}\nlatitude: {LAT}\nlongitude: {LON}\n"
                   f"[Capture]\ndata_dir: {data}\nlog_dir: logs\nfps: 25.0 ; frames per second\n")
    lines = ["2026/09/27 17:00:00-INFO-StartCapture - starting", *mode_lines] + _blocks(now, gap_s)
    (data / "logs" / f"log_{code}_20260927_134539_001.log").write_text("\n".join(lines) + "\n")
    cam = g.parse_rms_config(cfg)
    assert cam is not None
    return cam


class _Fix:
    def __init__(self, rc: int = 0):
        self.rc, self.calls = rc, []

    def __call__(self, code: str) -> int:
        self.calls.append(code)
        return self.rc


# ── measurement ─────────────────────────────────────────────────────────────


def test_full_rate_blocks_measure_as_nominal():
    m = g.measure(_blocks(NIGHT, 10.24), 25.0, NIGHT)
    assert m is not None and m.nominal_gap_s == 10.24
    assert m.ratio < g.SLOW_RATIO


def test_slow_shutter_blocks_measure_about_twice_nominal():
    m = g.measure(_blocks(NIGHT, 21.5), 25.0, NIGHT)
    assert m is not None and 2.0 < m.ratio < 2.2


def test_no_verdict_when_rms_stopped_capturing_or_too_few_blocks():
    assert g.measure(_blocks(NIGHT - timedelta(minutes=5), 21.5), 25.0, NIGHT) is None
    assert g.measure(_blocks(NIGHT, 21.5, n=4), 25.0, NIGHT) is None


def test_parse_rms_config_reads_code_fps_position_and_log_dir(tmp_path):
    cam = _camera(tmp_path, "DE001E", 10.24, NIGHT)
    assert (cam.code, cam.fps, cam.lat, cam.lon) == ("DE001E", 25.0, LAT, LON)
    assert g.latest_log(cam).name.startswith("log_DE001E_")


def test_running_rms_configs_uses_c_flag_or_working_directory(tmp_path):
    proc = tmp_path / "proc"
    with_c = proc / "101"
    no_c = proc / "102"
    other = proc / "103"
    for d in (with_c, no_c, other):
        d.mkdir(parents=True)
    (with_c / "cmdline").write_bytes(b"python\0-m\0RMS.StartCapture\0-c\0/home/gmn/RMS_cam1/.config\0")
    (no_c / "cmdline").write_bytes(b"python\0-m\0RMS.StartCapture\0")
    os.symlink("/home/rmsuser/source/RMS", no_c / "cwd")
    (other / "cmdline").write_bytes(b"python\0station_api.py\0")
    assert g.running_rms_configs(proc) == [Path("/home/gmn/RMS_cam1/.config"),
                                           Path("/home/rmsuser/source/RMS/.config")]


def test_mode_defaults_to_report():
    assert g.guard_mode({}) == "report"
    assert g.guard_mode({"services": {"fps_guard": {"enabled": True, "mode": "fix"}}}) == "fix"
    assert g.guard_mode({"services": {"fps_guard": {"enabled": False, "mode": "fix"}}}) == "off"
    assert g.guard_mode({"services": {"fps_guard": {"mode": "bogus"}}}) == "report"


# ── decisions ───────────────────────────────────────────────────────────────


def test_report_mode_logs_but_never_touches_the_camera(tmp_path):
    cam = _camera(tmp_path, "DE001E", 21.5, NIGHT)
    fix, state = _Fix(), {}
    lines = g.check([cam], "report", state, NIGHT, fix=fix)
    assert fix.calls == []
    # RMS logs whole seconds, so 21.5 s gaps read as 21/22 s: median 22.0 s.
    assert len(lines) == 1 and "DE001E: SLOW 11.6 fps of 25" in lines[0] and "mode=report" in lines[0]
    assert state["DE001E"]["slow_since"]


def test_fix_mode_reapplies_once_then_waits_out_the_cooldown(tmp_path):
    cam = _camera(tmp_path, "DE001E", 21.5, NIGHT)
    fix, state = _Fix(0), {}
    g.check([cam], "fix", state, NIGHT, fix=fix)
    assert fix.calls == ["DE001E"]
    assert state["DE001E"]["last_result"] == "settings re-applied"

    later = NIGHT + timedelta(minutes=10)
    cam = _camera(tmp_path / "b", "DE001E", 21.5, later)
    lines = g.check([cam], "fix", state, later, fix=fix)
    assert fix.calls == ["DE001E"]
    assert "re-applied 10 min ago" in lines[0]


def test_at_most_three_reapplies_a_night(tmp_path):
    fix, state = _Fix(0), {}
    for i in range(5):
        now = NIGHT + timedelta(minutes=31 * i)
        g.check([_camera(tmp_path / str(i), "DE001E", 21.5, now)], "fix", state, now, fix=fix)
    assert len(fix.calls) == g.MAX_ACTIONS_PER_NIGHT


def test_an_unreachable_camera_is_not_retried_tonight(tmp_path):
    fix, state = _Fix(2), {}
    g.check([_camera(tmp_path / "a", "RO000E", 25.6, NIGHT)], "fix", state, NIGHT, fix=fix)
    later = NIGHT + timedelta(hours=1)
    lines = g.check([_camera(tmp_path / "b", "RO000E", 25.6, later)], "fix", state, later, fix=fix)
    assert fix.calls == ["RO000E"]
    assert "could not reach it" in lines[0]


def test_one_reapply_per_run(tmp_path):
    cams = [_camera(tmp_path, "DE001D", 21.5, NIGHT), _camera(tmp_path, "DE001E", 21.5, NIGHT)]
    fix, state = _Fix(0), {}
    lines = g.check(cams, "fix", state, NIGHT, fix=fix)
    assert fix.calls == ["DE001D"]
    assert "one re-apply per run" in lines[1]


def test_nothing_happens_in_daylight_or_at_full_rate(tmp_path):
    fix, state = _Fix(0), {}
    assert g.check([_camera(tmp_path / "a", "DE001E", 21.5, DAY)], "fix", state, DAY, fix=fix) == []
    assert g.check([_camera(tmp_path / "b", "DE001B", 10.24, NIGHT)], "fix", state, NIGHT, fix=fix) == []
    assert fix.calls == []


def test_recovery_is_logged_once(tmp_path):
    fix, state = _Fix(0), {}
    g.check([_camera(tmp_path / "a", "DE001E", 21.5, NIGHT)], "fix", state, NIGHT, fix=fix)
    later = NIGHT + timedelta(minutes=6)
    lines = g.check([_camera(tmp_path / "b", "DE001E", 10.24, later)], "fix", state, later, fix=fix)
    assert len(lines) == 1 and "back to normal (25.6 fps)" in lines[0]  # 10/11 s gaps
    assert "slow_since" not in state["DE001E"]


def test_a_new_night_resets_the_budget(tmp_path):
    fix, state = _Fix(2), {}
    g.check([_camera(tmp_path / "a", "RO000E", 25.6, NIGHT)], "fix", state, NIGHT, fix=fix)
    next_night = NIGHT + timedelta(days=1)
    g.check([_camera(tmp_path / "b", "RO000E", 25.6, next_night)], "fix", state, next_night, fix=fix)
    assert fix.calls == ["RO000E", "RO000E"]


def test_rms_capture_state_reads_the_newest_state_line():
    start = "2026/10/25 16:02:11-INFO-StartCapture-line:1523 - Starting capture for 13.10 hours"
    end = "2026/10/26 05:08:40-INFO-StartCapture-line:1247 - Ending capture..."
    wait = "2026/10/26 07:10:02-INFO-StartCapture-line:1397 - Waiting 8:52:09 to start recording for 13.124 hrs"
    other = "2026/10/26 05:30:00-INFO-Reprocess-line:466 - Generating a timelapse..."
    assert g.rms_capture_state([start, "x"]) == "capturing"
    assert g.rms_capture_state([start, end, other]) == "ended"
    assert g.rms_capture_state([start, end, other, wait]) == "waiting"
    assert g.rms_capture_state([other]) is None


# ── RMS day mode ────────────────────────────────────────────────────────────
# gmnro22 (Ferigile) logged "SLOW 11.9-14.2 fps" ~15 times every morning from
# 04:37 UTC, after RMS switched its cameras to day mode at 04:36 with the sun
# still below -6 deg. In fix mode that would run fix_cam on a camera at dawn.

_TO_NIGHT = ("2026/09/27 17:05:00-INFO-CaptureModeSwitcher-line:208 - Next event is a sunrise "
             "(2026-09-28 04:36:00.000000), switching to nighttime mode")
_TO_DAY = ("2026/09/27 21:50:00-INFO-CaptureModeSwitcher-line:208 - Next event is a sunset "
           "(2026-09-28 16:20:00.000000), switching to daytime mode")


def test_rms_mode_reads_the_newest_switch():
    assert g.rms_mode([]) is None
    assert g.rms_mode([_TO_NIGHT]) == "night"
    assert g.rms_mode([_TO_NIGHT, _TO_DAY]) == "day"
    assert g.rms_mode([_TO_DAY, _TO_NIGHT]) == "night"


def test_slow_camera_with_rms_in_night_mode_is_still_reported(tmp_path):
    cam = _camera(tmp_path, "DE001E", 21.5, NIGHT, (_TO_NIGHT,))
    fix, state = _Fix(0), {}
    lines = g.check([cam], "fix", state, NIGHT, fix=fix)
    assert fix.calls == ["DE001E"]
    assert len(lines) == 1 and "DE001E: SLOW" in lines[0]


def test_rms_in_day_mode_is_skipped_below_minus_six_and_logged_once(tmp_path):
    fix, state = _Fix(0), {}
    lines = g.check([_camera(tmp_path / "a", "DE001E", 21.5, NIGHT, (_TO_NIGHT, _TO_DAY))],
                    "fix", state, NIGHT, fix=fix)
    assert lines == ["DE001E: skipped: RMS in day mode"]
    later = NIGHT + timedelta(minutes=3)
    lines = g.check([_camera(tmp_path / "b", "DE001E", 21.5, later, (_TO_NIGHT, _TO_DAY))],
                    "fix", state, later, fix=fix)
    assert lines == []
    assert fix.calls == []
    assert "slow_since" not in state["DE001E"]


def test_day_mode_skip_is_logged_again_after_rms_returns_to_night(tmp_path):
    fix, state = _Fix(0), {}
    g.check([_camera(tmp_path / "a", "DE001E", 21.5, NIGHT, (_TO_DAY,))], "report", state, NIGHT, fix=fix)
    later = NIGHT + timedelta(minutes=3)
    g.check([_camera(tmp_path / "b", "DE001E", 21.5, later, (_TO_DAY, _TO_NIGHT))], "report", state, later, fix=fix)
    assert "rms_day" not in state["DE001E"]
    again = later + timedelta(minutes=3)
    lines = g.check([_camera(tmp_path / "c", "DE001E", 21.5, again, (_TO_NIGHT, _TO_DAY))],
                    "report", state, again, fix=fix)
    assert lines[-1] == "DE001E: skipped: RMS in day mode"


def test_no_mode_switch_in_the_log_behaves_as_before(tmp_path):
    cam = _camera(tmp_path, "DE001E", 21.5, NIGHT)
    lines = g.check([cam], "report", {}, NIGHT, fix=_Fix(0))
    assert len(lines) == 1 and "DE001E: SLOW" in lines[0]


def test_no_rms_log_found_gives_no_verdict(tmp_path):
    cam = _camera(tmp_path, "DE001E", 21.5, NIGHT)
    for log in (p for d in cam.log_dirs if d.is_dir() for p in d.glob("*.log")):
        log.unlink()
    state: dict = {}
    assert g.check([cam], "fix", state, NIGHT, fix=_Fix(0)) == []
    assert state == {}
