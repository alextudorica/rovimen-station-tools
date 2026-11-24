"""camera_stall_guard: RMS grab failures + missing FF files -> camera reboot.

RO20XB (Brăila) stopped delivering frames at 22:48 UTC on 2026-10-04 while its
RTSP port kept answering. RMS logged "Frame grabbing failed" about once a
minute until the sunrise mode switch rebooted the camera at 03:23, and on the
nights before it delivered 5% and then 2% of its FF files.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import camera_fps_guard as fps
import camera_stall_guard as g

# Brăila; 23:00 UTC on 2026-10-04 is deep night there, 10:00 UTC is day.
LAT, LON = 44.942, 27.603
NIGHT = datetime(2026, 10, 4, 23, 0, tzinfo=timezone.utc)
DAY = datetime(2026, 10, 5, 10, 0, tzinfo=timezone.utc)
DIRECT = "rtsp://192.168.100.224:554/user=admin&password=&channel=1&stream=0.sdp"


def _stamp(t: datetime) -> str:
    return t.strftime('%Y/%m/%d %H:%M:%S')


def _fail(t: datetime) -> str:
    return (f"{_stamp(t)}-INFO-BufferedCapture-line:2096 - "
            "Frame grabbing failed, video device is probably disconnected!")


def _camera(tmp_path: Path, now: datetime, *, ff_every_s: float | None = 10.24,
            ff_stop: datetime | None = None, fail_every_s: float | None = None,
            log_start: datetime | None = None, rms_reboot: datetime | None = None,
            code: str = "RO20XB", device: str = DIRECT,
            session: datetime | None = None, extra: list[str] | None = None) -> g.Camera:
    """An RMS camera on disk: .config, a log tail and CapturedFiles FF names.

    ``extra`` adds log lines (sorted in by their timestamp)."""
    root = tmp_path / code
    data = root / "RMS_data"
    (data / "logs").mkdir(parents=True)
    cfg = root / ".config"
    cfg.write_text(f"[System]\nstationID: {code}\nlatitude: {LAT}\nlongitude: {LON}\n"
                   f"[Capture]\ndevice: {device}\ndata_dir: {data}\nlog_dir: logs\nfps: 25.0\n")
    start = log_start or now - timedelta(hours=3)
    lines = [f"{_stamp(start)}-INFO-StartCapture-line:900 - Starting capture"]
    if rms_reboot:
        lines.append(f"{_stamp(rms_reboot)}-INFO-CameraControl-line:92 - Camera rebooting, please wait....")
    if fail_every_s:
        t = start
        while t <= now:
            lines.append(_fail(t))
            t += timedelta(seconds=fail_every_s)
    lines.extend(extra or [])
    lines.sort(key=lambda s: s[:19])
    (data / "logs" / f"log_{code}_20261004_134528_001.log").write_text("\n".join(lines) + "\n")
    session = session or start
    night_dir = data / "CapturedFiles" / f"{code}_{session.strftime('%Y%m%d_%H%M%S')}_221350"
    night_dir.mkdir(parents=True)
    if ff_every_s:
        t, end = start, ff_stop or now
        while t <= end:
            (night_dir / f"FF_{code}_{t.strftime('%Y%m%d_%H%M%S')}_000_0000000.fits").touch()
            t += timedelta(seconds=ff_every_s)
        if ff_stop:  # the last FF is exactly at ff_stop
            (night_dir / f"FF_{code}_{ff_stop.strftime('%Y%m%d_%H%M%S')}_000_0000000.fits").touch()
    rms = fps.parse_rms_config(cfg)
    assert rms is not None
    proc = fps.RmsProcess(cfg, "/home/gmn/RMS/venv/bin/python", Path("/home/gmn/RMS"))
    return g.Camera(rms, proc, g.camera_ip(cfg))


def _verdict(cam: g.Camera, now: datetime, last_ours=None):
    lines = fps._tail_lines(fps.latest_log(cam.rms), g.TAIL_BYTES)
    ffs = g.ff_times(cam.rms, now - timedelta(seconds=g.DEGRADED_WINDOW_S + 60))
    return g.assess(lines, ffs, cam.rms.fps, now, last_ours, g.session_start(cam.rms, now))


class _Reboot:
    def __init__(self, result: str = "rebooted"):
        self.result, self.calls = result, []

    def __call__(self, cam: g.Camera) -> str:
        self.calls.append(cam.rms.code)
        return self.result


# ── detection ───────────────────────────────────────────────────────────────


def test_no_frames_while_rms_keeps_failing_is_a_stall(tmp_path):
    # 22:48 on 2026-10-04: frames stop, RMS fails every ~45 s.
    cam = _camera(tmp_path, NIGHT, ff_stop=NIGHT - timedelta(minutes=12), fail_every_s=45)
    v = _verdict(cam, NIGHT)
    assert v is not None and v.kind == "stalled"
    assert "no FF for 12 min" in v.detail


def test_full_rate_capture_with_odd_reconnects_is_fine(tmp_path):
    cam = _camera(tmp_path, NIGHT, fail_every_s=900)  # FFs every 10.24 s
    assert _verdict(cam, NIGHT) is None


def test_two_percent_yield_with_constant_reconnects_is_degraded(tmp_path):
    # Night 2026-10-04: 80 FFs in ~11 h, ~1,000 reconnects.
    cam = _camera(tmp_path, NIGHT, ff_every_s=480, fail_every_s=45)
    v = _verdict(cam, NIGHT)
    assert v is not None and v.kind == "degraded"
    assert "of 176 FFs" in v.detail


def test_no_verdict_when_rms_is_not_failing_right_now(tmp_path):
    # RMS stopped capturing (no FFs, no failures for 10 min): not a camera fault.
    cam = _camera(tmp_path, NIGHT, ff_stop=NIGHT - timedelta(minutes=20), fail_every_s=None)
    assert _verdict(cam, NIGHT) is None


def test_a_camera_reboot_inside_the_window_is_given_time(tmp_path):
    rms = _camera(tmp_path / "a", NIGHT, ff_every_s=480, fail_every_s=45,
                  rms_reboot=NIGHT - timedelta(minutes=20))
    assert _verdict(rms, NIGHT) is None
    ours = _camera(tmp_path / "b", NIGHT, ff_every_s=480, fail_every_s=45)
    assert _verdict(ours, NIGHT, last_ours=NIGHT - timedelta(minutes=5)) is None


def test_degraded_needs_rms_running_the_whole_window(tmp_path):
    cam = _camera(tmp_path, NIGHT, ff_every_s=480, fail_every_s=45,
                  log_start=NIGHT - timedelta(minutes=8))
    assert _verdict(cam, NIGHT) is None


def test_no_verdict_in_the_first_minutes_of_a_night_session(tmp_path):
    # 16:30 on 2026-10-04: RMS's night session began at 16:31; failures logged
    # around the sunset mode switch are not a stall of the night's capture.
    cam = _camera(tmp_path, NIGHT, ff_every_s=None, fail_every_s=45,
                  log_start=NIGHT - timedelta(hours=3), session=NIGHT - timedelta(minutes=5))
    assert _verdict(cam, NIGHT) is None
    assert g.assess([], [], 25.0, NIGHT, None, None) is None  # no session at all


def test_session_start_ignores_directories_from_the_future(tmp_path):
    cam = _camera(tmp_path, NIGHT, session=NIGHT - timedelta(hours=2))
    later_dir = cam.rms.log_dirs[0].parent / "CapturedFiles" / "RO20XB_20261005_033018_000001"
    later_dir.mkdir()
    assert g.session_start(cam.rms, NIGHT) == NIGHT - timedelta(hours=2)


def test_camera_ip_from_direct_and_relay_urls():
    assert g._IPV4_RE.search(DIRECT).group(1) == "192.168.100.224"


def test_camera_ip_relay_url_and_loopback(tmp_path):
    cfg = tmp_path / ".config"
    cfg.write_text("device: rtsp://localhost:8554/cam1?camera=192.168.100.223\n")
    assert g.camera_ip(cfg) == "192.168.100.223"
    cfg.write_text("device: rtsp://127.0.0.1:8554/cam1\n")
    assert g.camera_ip(cfg) is None


def test_mode_defaults_to_report():
    assert g.guard_mode({}) == "report"
    assert g.guard_mode({"services": {"cam_stall": {"mode": "fix"}}}) == "fix"
    assert g.guard_mode({"services": {"cam_stall": {"enabled": False, "mode": "fix"}}}) == "off"
    assert g.guard_mode({"services": {"cam_stall": {"mode": "bogus"}}}) == "report"


# ── decisions ───────────────────────────────────────────────────────────────


def _stalled(tmp_path: Path, now: datetime, code: str = "RO20XB") -> g.Camera:
    return _camera(tmp_path, now, ff_stop=now - timedelta(minutes=15), fail_every_s=45, code=code)


def test_report_mode_logs_but_never_reboots(tmp_path):
    reboot, state = _Reboot(), {}
    lines = g.check([_stalled(tmp_path, NIGHT)], "report", state, NIGHT, reboot=reboot)
    assert reboot.calls == []
    assert len(lines) == 1 and lines[0].startswith("RO20XB: STALLED") and "mode=report" in lines[0]


def test_fix_mode_reboots_once_then_waits_out_the_cooldown(tmp_path):
    reboot, state = _Reboot("rebooted"), {}
    stop, start = NIGHT - timedelta(minutes=15), NIGHT - timedelta(hours=3)
    lines = g.check([_camera(tmp_path / "a", NIGHT, ff_stop=stop, fail_every_s=45, log_start=start)],
                    "fix", state, NIGHT, reboot=reboot)
    assert reboot.calls == ["RO20XB"]
    assert "camera reboot via RMS CameraControl (192.168.100.224): rebooted" in lines[0]
    later = NIGHT + timedelta(minutes=12)  # the same outage
    lines = g.check([_camera(tmp_path / "b", later, ff_stop=stop, fail_every_s=45, log_start=start)],
                    "fix", state, later, reboot=reboot)
    assert reboot.calls == ["RO20XB"]
    assert "stalled reboot 12 min ago" in lines[0]


def test_an_outage_gets_a_reboot_and_one_retry_30_min_later(tmp_path):
    reboot, state, at = _Reboot("rebooted"), {}, []
    stop = NIGHT - timedelta(minutes=15)  # the same outage the whole time
    for minute in range(0, 120, 10):
        now = NIGHT + timedelta(minutes=minute)
        before = len(reboot.calls)
        cam = _camera(tmp_path / str(minute), now, ff_stop=stop, fail_every_s=45,
                      log_start=NIGHT - timedelta(hours=3))
        g.check([cam], "fix", state, now, reboot=reboot)
        if len(reboot.calls) > before:
            at.append(minute)
    assert at == [0, 30]
    assert state["RO20XB"]["outage_reboots"] == g.MAX_REBOOTS_PER_OUTAGE


def test_each_new_outage_gets_its_own_reboot_up_to_the_night_cap(tmp_path):
    reboot, state = _Reboot("rebooted"), {}
    for i in range(8):  # eight separate outages, 25 min apart
        now = NIGHT - timedelta(hours=4) + timedelta(minutes=25 * i)
        g.check([_stalled(tmp_path / str(i), now)], "fix", state, now, reboot=reboot)
    assert len(reboot.calls) == g.MAX_STALL_REBOOTS


def test_a_limping_camera_gets_25_min_before_a_hole_counts_as_a_stall(tmp_path):
    # RO20XB on 2026-10-04: an FF every few minutes, holes of up to 20 min that
    # ended by themselves, then dark from 22:37.
    last = datetime(2026, 10, 4, 22, 37, 19, tzinfo=timezone.utc)
    for minutes, kind in ((21, "degraded"), (26, "stalled")):
        now = last + timedelta(minutes=minutes)
        cam = _camera(tmp_path / str(minutes), now, ff_every_s=480, ff_stop=last, fail_every_s=45)
        v = _verdict(cam, now)
        assert v is not None and v.kind == kind
    # A camera that was delivering normally: 10 min is enough.
    healthy = _camera(tmp_path / "h", NIGHT, ff_stop=NIGHT - timedelta(minutes=11), fail_every_s=45)
    assert _verdict(healthy, NIGHT).kind == "stalled"


def _degraded(tmp_path: Path, now: datetime) -> g.Camera:
    return _camera(tmp_path, now, ff_every_s=480, fail_every_s=45)


def test_degraded_reboots_do_not_use_up_the_stall_budget(tmp_path):
    # RO20XB on 2026-10-04: degraded from ~17:00, stopped completely at 22:48.
    reboot, state, kinds = _Reboot("rebooted"), {}, []
    evening = datetime(2026, 10, 4, 17, 0, tzinfo=timezone.utc)
    for minute in range(0, 300, 10):  # degraded 17:00-21:50
        now = evening + timedelta(minutes=minute)
        before = len(reboot.calls)
        g.check([_degraded(tmp_path / f"d{minute}", now)], "fix", state, now, reboot=reboot)
        if len(reboot.calls) > before:
            kinds.append(("degraded", minute))
    assert [m for _, m in kinds] == [0, 60]  # 2 a night, 60 min apart
    last = datetime(2026, 10, 4, 22, 37, 19, tzinfo=timezone.utc)  # RMS's last FF that night
    stop = last + timedelta(minutes=26)
    cam = _camera(tmp_path / "s", stop, ff_every_s=480, ff_stop=last, fail_every_s=45)
    lines = g.check([cam], "fix", state, stop, reboot=reboot)
    assert len(reboot.calls) == 3 and "STALLED" in lines[0] and ": rebooted" in lines[0]
    assert (state["RO20XB"]["degraded_reboots"], state["RO20XB"]["stalled_reboots"]) == (2, 1)


def test_an_unreachable_camera_is_not_retried_tonight(tmp_path):
    reboot, state = _Reboot("unreachable"), {}
    g.check([_stalled(tmp_path / "a", NIGHT)], "fix", state, NIGHT, reboot=reboot)
    later = NIGHT + timedelta(hours=1)
    lines = g.check([_stalled(tmp_path / "b", later)], "fix", state, later, reboot=reboot)
    assert reboot.calls == ["RO20XB"]
    assert "did not answer earlier tonight" in lines[0]


def test_one_reboot_per_run(tmp_path):
    cams = [_stalled(tmp_path, NIGHT, "RO001M"), _stalled(tmp_path, NIGHT, "RO20XB")]
    reboot, state = _Reboot(), {}
    lines = g.check(cams, "fix", state, NIGHT, reboot=reboot)
    assert reboot.calls == ["RO001M"]
    assert "one reboot per run" in lines[1]


def test_nothing_happens_in_daylight_or_while_capturing(tmp_path):
    reboot, state = _Reboot(), {}
    assert g.check([_stalled(tmp_path / "a", DAY)], "fix", state, DAY, reboot=reboot) == []
    assert g.check([_camera(tmp_path / "b", NIGHT, code="RO001M")], "fix", state, NIGHT, reboot=reboot) == []
    assert reboot.calls == []


def test_recovery_is_logged_once(tmp_path):
    reboot, state = _Reboot(), {}
    g.check([_stalled(tmp_path / "a", NIGHT)], "report", state, NIGHT, reboot=reboot)
    later = NIGHT + timedelta(minutes=6)
    lines = g.check([_camera(tmp_path / "b", later, fail_every_s=900)], "report", state, later, reboot=reboot)
    assert len(lines) == 1 and "delivering frames again after stalled" in lines[0]
    later2 = later + timedelta(minutes=3)
    assert g.check([_camera(tmp_path / "c", later2, fail_every_s=900)], "report", state, later2, reboot=reboot) == []


def test_no_recovery_line_while_a_rebooted_camera_still_sends_nothing(tmp_path):
    reboot, state = _Reboot("rebooted"), {}
    g.check([_stalled(tmp_path / "a", NIGHT)], "fix", state, NIGHT, reboot=reboot)
    later = NIGHT + timedelta(minutes=3)  # inside the post-reboot grace, still no FFs
    assert g.check([_stalled(tmp_path / "b", later)], "fix", state, later, reboot=reboot) == []
    assert state["RO20XB"]["bad_since"]


def test_a_new_night_resets_the_budget(tmp_path):
    reboot, state = _Reboot("unreachable"), {}
    g.check([_stalled(tmp_path / "a", NIGHT)], "fix", state, NIGHT, reboot=reboot)
    next_night = NIGHT + timedelta(days=1)
    g.check([_stalled(tmp_path / "b", next_night)], "fix", state, next_night, reboot=reboot)
    assert reboot.calls == ["RO20XB", "RO20XB"]


# ── RMS's own night ─────────────────────────────────────────────────────────


def _switch(t: datetime, to: str) -> str:
    nxt = "sunset" if to == "day" else "sunrise"
    return (f"{_stamp(t)}-INFO-CaptureModeSwitcher-line:104 - Next event is a {nxt} "
            f"(2026-10-06 16:27:31.871432), switching to {to}time mode")


def test_rms_ending_night_capture_is_not_a_stall(tmp_path):
    # 2026-10-06: RMS switched to day mode at 03:24 (sun -9.3 deg) and rebooted
    # the camera; at 03:37 the sun was still at -7 deg and the guard rebooted
    # RO20XB for "no FF for 13 min".
    switch = datetime(2026, 10, 6, 3, 24, 18, tzinfo=timezone.utc)
    now = datetime(2026, 10, 6, 3, 37, tzinfo=timezone.utc)
    assert g.rovimen_night.sun_altitude_deg(LAT, LON, now) < g.NIGHT_SUN_ALT_DEG
    day = [_switch(now - timedelta(hours=11), "night"), _switch(switch, "day")]
    cam = _camera(tmp_path / "a", now, ff_stop=switch, fail_every_s=45, extra=day)
    reboot = _Reboot()
    assert g.check([cam], "fix", {}, now, reboot=reboot, pinger=lambda ip: 0.0) == []
    assert reboot.calls == []
    # The same camera after RMS's switch to NIGHT is still judged.
    night = [_switch(now - timedelta(hours=11), "night")]
    cam = _camera(tmp_path / "b", now, ff_stop=switch, fail_every_s=45, extra=night)
    assert "STALLED" in g.check([cam], "fix", {}, now, reboot=reboot)[0]


# ── lossy boots ─────────────────────────────────────────────────────────────


class _Pinger:
    def __init__(self, *losses: float):
        self.losses, self.calls = list(losses), []

    def __call__(self, ip: str) -> float:
        self.calls.append(ip)
        return self.losses.pop(0)


def test_a_lossy_boot_is_rebooted_until_a_boot_tests_clean(tmp_path):
    # RO20XB on 2026-10-06: boots at 41%, 0%, 45% loss.
    reboot, state, pinger = _Reboot("rebooted"), {}, _Pinger(41.0, 0.0)
    lines = g.check([_camera(tmp_path / "a", NIGHT)], "fix", state, NIGHT, reboot=reboot, pinger=pinger)
    assert reboot.calls == ["RO20XB"]
    assert lines[0].startswith("RO20XB: LOSSY ping loss 41% (50 x 1000 B)") and ": rebooted" in lines[0]
    soon = NIGHT + timedelta(minutes=2)  # the new boot has not settled yet
    assert g.check([_camera(tmp_path / "b", soon)], "fix", state, soon, reboot=reboot, pinger=pinger) == []
    assert len(pinger.calls) == 1
    later = NIGHT + timedelta(minutes=4)
    lines = g.check([_camera(tmp_path / "c", later)], "fix", state, later, reboot=reboot, pinger=pinger)
    assert reboot.calls == ["RO20XB"]
    assert len(lines) == 1 and "ping loss 0%" in lines[0] and "clean again after lossy" in lines[0]
    assert "bad_since" not in state["RO20XB"]


def test_a_clean_boot_is_tested_once_then_hourly(tmp_path):
    reboot, state, pinger = _Reboot(), {}, _Pinger(0.0, 0.0)
    lines = g.check([_camera(tmp_path / "a", NIGHT)], "fix", state, NIGHT, reboot=reboot, pinger=pinger)
    assert lines == ["RO20XB: ping loss 0% (50 x 1000 B) after the 20:00 boot"]
    for minutes in (3, 30, 57):  # the same session, so the same boot
        now = NIGHT + timedelta(minutes=minutes)
        cam = _camera(tmp_path / str(minutes), now, log_start=NIGHT - timedelta(hours=3))
        assert g.check([cam], "fix", state, now, reboot=reboot, pinger=pinger) == []
    hour = NIGHT + timedelta(minutes=61)
    g.check([_camera(tmp_path / "h", hour, log_start=NIGHT - timedelta(hours=3))], "fix", state, hour,
            reboot=reboot, pinger=pinger)
    assert len(pinger.calls) == 2 and reboot.calls == []


def test_a_lossy_camera_deferred_by_the_one_reboot_limit_is_retested_next_run(tmp_path):
    # Brăila 2026-10-06 16:31: RO001M (40%) took the run's reboot; RO001N (74%)
    # was not retested until 17:31.
    reboot, state, pinger = _Reboot("rebooted"), {}, _Pinger(40.0, 74.0, 70.0)
    start = NIGHT - timedelta(hours=3)

    def run(now: datetime, tag: str) -> list[str]:
        cams = [_camera(tmp_path / tag, now, code=code, log_start=start) for code in ("RO001M", "RO001N")]
        return g.check(cams, "fix", state, now, reboot=reboot, pinger=pinger)

    lines = run(NIGHT, "a")
    assert reboot.calls == ["RO001M"] and "one reboot per run" in lines[1]
    soon = NIGHT + timedelta(minutes=3)  # RO001M's new boot has not settled
    lines = run(soon - timedelta(seconds=30), "b")
    assert reboot.calls == ["RO001M", "RO001N"] and len(pinger.calls) == 3
    assert lines[0].startswith("RO001N: LOSSY ping loss 70%") and ": rebooted" in lines[0]
    assert "lossy_deferred" not in state["RO001N"]


def test_lossy_reboots_stop_at_the_nightly_cap(tmp_path):
    reboot, state = _Reboot("rebooted"), {}
    pinger = _Pinger(*([45.0] * (g.MAX_LOSSY_REBOOTS + 1)))
    for i in range(g.MAX_LOSSY_REBOOTS + 1):
        now = NIGHT + timedelta(minutes=5 * i)
        lines = g.check([_camera(tmp_path / str(i), now, log_start=NIGHT - timedelta(hours=3))], "fix",
                        state, now, reboot=reboot, pinger=pinger)
    assert len(reboot.calls) == g.MAX_LOSSY_REBOOTS
    assert f"{g.MAX_LOSSY_REBOOTS} lossy reboots already tonight" in lines[0]


def test_lossy_in_report_mode_only_logs(tmp_path):
    reboot = _Reboot()
    lines = g.check([_camera(tmp_path, NIGHT)], "report", {}, NIGHT, reboot=reboot, pinger=_Pinger(30.0))
    assert reboot.calls == [] and "LOSSY ping loss 30%" in lines[0] and "mode=report" in lines[0]


def test_ping_loss_parses_ping_output(monkeypatch):
    class _Done:
        stdout = "50 packets transmitted, 29 received, 42% packet loss, time 9812ms\n"
    monkeypatch.setattr(g.subprocess, "run", lambda *a, **k: _Done())
    assert g.ping_loss("192.168.100.224") == 42.0


# ── the reboot itself ───────────────────────────────────────────────────────


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


class _Proc:
    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def _probe(answers: list[bool]):
    seq = iter(answers)
    return lambda ip: next(seq, True)


def test_reboot_is_confirmed_when_the_control_port_drops(tmp_path):
    cam = _stalled(tmp_path, NIGHT)
    clock, launched = _Clock(), []

    def popen(cmd, **kw):
        launched.append((cmd, kw["cwd"]))
        return _Proc()

    probe = _probe([True] + [True] * 5 + [False] * 3)
    assert g.reboot_camera(cam, probe=probe, clock=clock, sleep=clock.sleep, popen=popen) == "rebooted"
    cmd, cwd = launched[0]
    assert cmd[:3] == ["/home/gmn/RMS/venv/bin/python", "-m", "Utils.CameraControl"]
    assert cmd[-1] == "reboot" and cwd == "/home/gmn/RMS"


def test_reboot_that_never_drops_the_port_is_unconfirmed(tmp_path):
    clock = _Clock()
    # Lossy link: single missed connects do not count as the camera going down.
    probe = _probe([True] + [True, False, True, False, False, True] * 30)
    result = g.reboot_camera(_stalled(tmp_path, NIGHT), probe=probe, clock=clock,
                             sleep=clock.sleep, popen=lambda *a, **k: _Proc())
    assert result == "unconfirmed"


def test_a_camera_that_does_not_answer_is_unreachable(tmp_path):
    clock, launched = _Clock(), []
    result = g.reboot_camera(_stalled(tmp_path, NIGHT), probe=lambda ip: False, clock=clock,
                             sleep=clock.sleep, popen=lambda *a, **k: launched.append(a) or _Proc())
    assert result == "unreachable" and launched == []


# ── RMS process discovery ───────────────────────────────────────────────────


def test_rms_processes_carry_interpreter_and_checkout(tmp_path):
    proc = tmp_path / "proc"
    for pid in ("101", "102"):  # RMS forks: parent and child share one .config
        d = proc / pid
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"/home/gmn/RMS/venv/bin/python\0-m\0RMS.StartCapture\0-c\0/home/gmn/RMS_cam2/.config\0")
        os.symlink("/home/gmn/RMS", d / "cwd")
    found = fps.rms_processes(proc)
    assert found == [fps.RmsProcess(Path("/home/gmn/RMS_cam2/.config"), "/home/gmn/RMS/venv/bin/python",
                                    Path("/home/gmn/RMS"))]
