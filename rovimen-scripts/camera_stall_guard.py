#!/usr/bin/env python3
"""camera_stall_guard.py - reboot a camera that stops delivering frames at night.

gmnro20 (Brăila), RO20XB: at 22:48 UTC on 2026-10-04 the camera stopped
sending video while its RTSP port kept answering. RMS logged "Frame grabbing
failed" about once a minute, colour capture got 0.2 s of video, and nothing
recovered until RMS's sunrise mode switch rebooted the camera at 03:23. On the
two nights before, the same camera delivered 5% (189) and then 2% (80) of the
FF files a night allows while RMS reconnected ~1,000 times a night. The camera
is rebooted every evening at the sunset mode switch, so this is a state the
camera boots into and keeps until the next reboot, and a reboot is what
restored it.

Signal: RMS's own output, for every running RMS.StartCapture. After sunset
(sun below NIGHT_SUN_ALT_DEG), once RMS's night session (its newest
CapturedFiles directory) has run long enough to judge, a camera is

- stalled: no FF file for STALL_S, or LONG_STALL_S when the camera was
  already under DEGRADED_YIELD in the half hour before it went dark, with at
  least MIN_FAILS_STALL "Frame grabbing failed" lines in the last STALL_S;
- degraded: over the last DEGRADED_WINDOW_S, under DEGRADED_YIELD of the FF
  files its frame rate allows, with at least MIN_FAILS_DEGRADED grab failures
  and no camera reboot (RMS's or ours) inside it.

Action (``services.cam_stall.mode: "fix"``): RMS's own camera control,
``python -m Utils.CameraControl -c <config> reboot``, the path RMS's mode switch
uses, run with the RMS process's own interpreter and working directory. That
CLI exits 0 even when it cannot connect, so the guard confirms the reboot
itself: the camera's control port must stop answering within REBOOT_CONFIRM_S.
At most one reboot per run. A degraded camera gets at most
MAX_DEGRADED_REBOOTS a night, DEGRADED_COOLDOWN_S apart. Stalls are budgeted
per outage (an outage starts at the last FF file before it): each outage gets
a reboot, and if no FF arrives afterwards, one retry STALL_RETRY_S later
(MAX_REBOOTS_PER_OUTAGE), up to MAX_STALL_REBOOTS a night. So neither a
degraded evening nor its short holes use up the reboot a long outage needs:
RO20XB on 2026-10-04 was degraded from 17:00 with 30 FF holes of 5-20 min
that all ended by themselves, then went dark from 22:37 until the 03:23
sunrise reboot; replayed, the guard reboots it at 17:03 and 18:03 (degraded)
and at 23:03 and 23:33 (the outage), and for none of the short holes. A camera
that does not answer before the command is reported, not retried that night.
``"report"`` only logs.

Night means the sun below NIGHT_SUN_ALT_DEG AND, where RMS switches modes
(continuous capture), RMS's newest mode switch being to night: RMS ends night
capture at its own sunrise (03:24 at Brăila on 2026-10-06, sun still at about
-9 deg), reboots the camera and stops writing FF files, which the sun test alone
read as a stall and answered with two needless reboots at 03:37 and 03:40.

A camera can also boot into a lossy state: on 2026-10-06 three DVRIP reboots
of RO20XB came up with 41%, 0% and 45% loss on 1000-byte pings (RO001M,
not rebooted, 31-35% throughout), and each state held until the next reboot.
A lossy camera misses the station's TCP ACKs, stalls the stream and costs RMS a
25-35 s reconnect several times an hour. So once per camera boot at night
(LOSSY_SETTLE_S after it, again every LOSSY_RETEST_S) the guard sends
LOSSY_PINGS pings of LOSSY_PING_BYTES; over LOSSY_PCT loss is a "lossy"
finding, and fix mode reboots the camera until a boot tests clean, at most
MAX_LOSSY_REBOOTS a night. Each test result is logged. A lossy camera whose
reboot waited for the one-per-run limit is retested on the next run, not an
hour later: at 16:31 on 2026-10-06 RO001M took the run's reboot and RO001N
(74% loss) would otherwise have waited until 17:31.

Run by rovimen_watchdog.sh every 3 minutes; prints ``cam_stall`` lines for the
watchdog log and keeps its state in ~/rovimen_scripts/cam_stall_state.json.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, NamedTuple, Optional

import camera_fps_guard as fps
import rovimen_night

SCRIPTS_DIR = Path.home() / 'rovimen_scripts'
CONFIG_PATH = SCRIPTS_DIR / 'config.json'
STATE_PATH = SCRIPTS_DIR / 'cam_stall_state.json'

STALL_S = 600               # no FF for 10 min while RMS keeps failing
LONG_STALL_S = 1500         # 25 min, for a camera that was already degraded
MIN_FAILS_STALL = 3
LIVE_FAIL_S = 300           # the newest grab failure must be this recent
DEGRADED_WINDOW_S = 1800
DEGRADED_YIELD = 0.25       # under a quarter of the FF files the frame rate allows
MIN_FAILS_DEGRADED = 10
NIGHT_SUN_ALT_DEG = -6.0
MAX_STALL_REBOOTS = 6       # a night, over all outages
MAX_REBOOTS_PER_OUTAGE = 2
STALL_RETRY_S = 1800        # second reboot for the same outage
MAX_DEGRADED_REBOOTS = 2
DEGRADED_COOLDOWN_S = 3600
RECOVERED_FF_S = 120        # "delivering again" needs an FF this recent
FF_LOOKBACK_S = 16 * 3600
FRAMES_PER_FF = 256
CONTROL_PORT = 34567        # XMEye DVRIP, what RMS's CameraControl talks to
REBOOT_CONFIRM_S = 75
DOWN_PROBES = 3             # consecutive failed connects that count as "down"
CLI_TIMEOUT_S = 90
TAIL_BYTES = 2 * 1024 * 1024
LOSSY_PINGS = 50            # 0.2 s apart: a 10 s test
LOSSY_PING_BYTES = 1000
LOSSY_PCT = 10.0            # clean boots measured 0%, lossy ones 16-45% (RO20XB, 2026-10-06)
LOSSY_SETTLE_S = 180        # test a boot only once the camera has been up this long
LOSSY_RETEST_S = 3600
MAX_LOSSY_REBOOTS = 4       # a night

_STAMP = r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})'
_ANY_RE = re.compile(_STAMP)
_FAIL_RE = re.compile(_STAMP + r'\S*.*Frame grabbing failed')
_RMS_REBOOT_RE = re.compile(_STAMP + r'\S*.*Camera rebooting')
_LOSS_RE = re.compile(r'([\d.]+)% packet loss')
_SESSION_RE = re.compile(r'_(\d{8})_(\d{6})_\d+$')   # CapturedFiles/<CODE>_<date>_<time>_<us>

_IPV4_RE = re.compile(r'(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])')


class Camera(NamedTuple):
    rms: fps.RmsCamera
    process: fps.RmsProcess
    ip: Optional[str]


class Verdict(NamedTuple):
    kind: str               # 'stalled', 'degraded' or 'lossy'
    detail: str
    outage: Optional[datetime] = None   # stalled: the last FF before it (or session start)


def _ts(s: str) -> datetime:
    return datetime.strptime(s, '%Y/%m/%d %H:%M:%S').replace(tzinfo=timezone.utc)


def camera_ip(config: Path) -> Optional[str]:
    """The camera RMS controls: the first dotted quad of its ``device:`` URL.

    A relay URL carries it as ``?camera=<IP>``; 127.x would be the relay
    itself, which no camera command reaches.
    """
    try:
        text = config.read_text(errors='replace')
    except OSError:
        return None
    device = fps._config_value(text, 'device') or ''
    m = _IPV4_RE.search(device)
    if not m or m.group(1).startswith('127.'):
        return None
    return m.group(1)


def _session_dirs(cam: fps.RmsCamera) -> List[Path]:
    """RMS's capture-session directories for this camera, oldest first."""
    found: List[Path] = []
    for log_dir in cam.log_dirs:
        captured = log_dir.parent / 'CapturedFiles'
        if captured.is_dir():
            found.extend(d for d in captured.iterdir()
                         if d.is_dir() and d.name.startswith(cam.code + '_') and _SESSION_RE.search(d.name))
    return sorted(found, key=lambda d: d.name)


def session_start(cam: fps.RmsCamera, now: datetime) -> Optional[datetime]:
    """When RMS started its current capture session: the newest CapturedFiles
    directory name that is not in the future."""
    starts = []
    for d in _session_dirs(cam):
        m = _SESSION_RE.search(d.name)
        t = rovimen_night.utc_stamp(m.group(1), m.group(2))
        if t is not None and t <= now:
            starts.append(t)
    return max(starts) if starts else None


def ff_times(cam: fps.RmsCamera, since: datetime) -> List[datetime]:
    """Start times of the FF files RMS wrote since ``since`` (from their names)."""
    times: List[datetime] = []
    for d in _session_dirs(cam)[-2:]:
        for name in os.listdir(d):
            t = rovimen_night.ff_file_start(name)
            if t is not None and t > since:
                times.append(t)
    return sorted(times)


def assess(lines: List[str], ffs: List[datetime], fps_nominal: float, now: datetime,
           last_our_reboot: Optional[datetime], session: Optional[datetime]) -> Optional[Verdict]:
    """Stalled or degraded, from the RMS log tail, the FF start times and the
    start of RMS's capture session (no verdict without one)."""
    if session is None:
        return None
    fails = [_ts(m.group(1)) for m in map(_FAIL_RE.match, lines) if m]
    rms_reboots = [_ts(m.group(1)) for m in map(_RMS_REBOOT_RE.match, lines) if m]
    if not fails or (now - fails[-1]).total_seconds() > LIVE_FAIL_S:
        return None  # RMS is not failing right now
    reboots = rms_reboots + ([last_our_reboot] if last_our_reboot else [])

    def since(seconds: float) -> datetime:
        return now - timedelta(seconds=seconds)

    last_ff = ffs[-1] if ffs else None
    outage = last_ff if last_ff and last_ff > session else session
    gone = (now - outage).total_seconds()
    # A camera that was already limping before it went dark (under
    # DEGRADED_YIELD over the half hour before) comes back by itself from
    # holes of up to ~20 min (RO20XB, 2026-10-04): give it LONG_STALL_S.
    history = min(DEGRADED_WINDOW_S, (outage - session).total_seconds())
    before = sum(1 for t in ffs if outage - timedelta(seconds=history) < t <= outage)
    limping = history >= 300 and before < DEGRADED_YIELD * history * fps_nominal / FRAMES_PER_FF
    need = LONG_STALL_S if limping else STALL_S
    if gone >= need and not any(t > since(STALL_S) for t in reboots):
        n_fail = sum(1 for t in fails if t > since(STALL_S))
        if n_fail >= MIN_FAILS_STALL:
            return Verdict('stalled', f'no FF for {int(gone // 60)} min, {n_fail} grab failures in {STALL_S // 60} min',
                           outage)

    first = next((_ts(m.group(1)) for m in map(_ANY_RE.match, lines) if m), None)
    if first is None or max(first, session) > since(DEGRADED_WINDOW_S):
        return None  # the session has not been running the whole window
    if any(t > since(DEGRADED_WINDOW_S) for t in reboots):
        return None
    n_fail = sum(1 for t in fails if t > since(DEGRADED_WINDOW_S))
    got = sum(1 for t in ffs if t > since(DEGRADED_WINDOW_S))
    expected = DEGRADED_WINDOW_S * fps_nominal / FRAMES_PER_FF
    if n_fail >= MIN_FAILS_DEGRADED and got < DEGRADED_YIELD * expected:
        return Verdict('degraded', f'{got} of {expected:.0f} FFs ({100 * got / expected:.0f}%) in '
                                   f'{DEGRADED_WINDOW_S // 60} min, {n_fail} grab failures')
    return None


def last_boot(lines: List[str], last_ours: Optional[datetime],
              session: Optional[datetime]) -> Optional[datetime]:
    """The camera's newest boot this session: RMS's or our reboot, else the session start."""
    boots = [_ts(m.group(1)) for m in map(_RMS_REBOOT_RE.match, lines) if m]
    if last_ours:
        boots.append(last_ours)
    return max(boots) if boots else session


def ping_loss(ip: str) -> Optional[float]:
    """Percent loss of LOSSY_PINGS pings of LOSSY_PING_BYTES, or None if ping did not run."""
    cmd = ['ping', '-q', '-n', '-c', str(LOSSY_PINGS), '-i', '0.2', '-s', str(LOSSY_PING_BYTES), '-W', '1', ip]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=LOSSY_PINGS * 0.2 + 15).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = _LOSS_RE.search(out)
    return float(m.group(1)) if m else None


def lossy_verdict(cam: Camera, cs: dict, boot: Optional[datetime], now: datetime,
                  pinger: Callable[[str], Optional[float]], out: List[str]) -> Optional[Verdict]:
    """Ping-test this boot of the camera once it has settled, and hourly after.

    Logs every result; returns a 'lossy' verdict over LOSSY_PCT."""
    if not cam.ip or boot is None or (now - boot).total_seconds() < LOSSY_SETTLE_S:
        return None
    boot_key = boot.isoformat(timespec='seconds')
    deferred = cs.get('lossy_deferred') == boot_key  # check() could not reboot it last run
    if not deferred and cs.get('ping_boot') == boot_key and cs.get('ping_at') and \
            (now - datetime.fromisoformat(cs['ping_at'])).total_seconds() < LOSSY_RETEST_S:
        return None
    loss = pinger(cam.ip)
    if loss is None:
        return None
    cs.pop('lossy_deferred', None)
    cs.update(ping_boot=boot_key, ping_at=now.isoformat(timespec='seconds'), ping_loss=loss)
    detail = f'ping loss {loss:.0f}% ({LOSSY_PINGS} x {LOSSY_PING_BYTES} B) after the {boot:%H:%M} boot'
    if loss > LOSSY_PCT:
        return Verdict('lossy', detail)
    if cs.get('kind') == 'lossy':
        out.append(f'{cam.rms.code}: {detail}, clean again after lossy since {cs["bad_since"]}')
        cs.pop('bad_since', None)
        cs.pop('kind', None)
    else:
        out.append(f'{cam.rms.code}: {detail}')
    return None


def guard_mode(cfg: dict) -> str:
    """``off`` / ``report`` / ``fix`` from services.cam_stall."""
    svc = (cfg.get('services') or {}).get('cam_stall') or {}
    if not isinstance(svc, dict) or not svc.get('enabled', True):
        return 'off'
    mode = svc.get('mode', 'report')
    return mode if mode in ('report', 'fix') else 'report'


def decide(cs: dict, v: Verdict, now: datetime) -> Optional[str]:
    """Why not to reboot for this finding now, or None to reboot."""
    if cs.get('unreachable'):
        return 'camera did not answer earlier tonight'
    if v.kind == 'lossy':
        # No cooldown: the next test waits LOSSY_SETTLE_S after the reboot.
        if cs.get('lossy_reboots', 0) >= MAX_LOSSY_REBOOTS:
            return f'{MAX_LOSSY_REBOOTS} lossy reboots already tonight'
        return None
    if v.kind == 'degraded':
        if cs.get('degraded_reboots', 0) >= MAX_DEGRADED_REBOOTS:
            return f'{MAX_DEGRADED_REBOOTS} degraded reboots already tonight'
        last, wait = cs.get('last_degraded_reboot'), DEGRADED_COOLDOWN_S
    else:
        if cs.get('stalled_reboots', 0) >= MAX_STALL_REBOOTS:
            return f'{MAX_STALL_REBOOTS} stall reboots already tonight'
        if v.outage is None or cs.get('outage') != v.outage.isoformat():
            return None  # a new outage
        if cs.get('outage_reboots', 0) >= MAX_REBOOTS_PER_OUTAGE:
            return f'rebooted {MAX_REBOOTS_PER_OUTAGE} times for this outage'
        last, wait = cs.get('last_stalled_reboot'), STALL_RETRY_S
    if last:
        age = (now - datetime.fromisoformat(last)).total_seconds()
        if age < wait:
            return f'{v.kind} reboot {int(age // 60)} min ago'
    return None


def _port_open(ip: str, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, CONTROL_PORT), timeout=timeout):
            return True
    except OSError:
        return False


def _answers(ip: str, probe: Callable[[str], bool]) -> bool:
    return any(probe(ip) for _ in range(DOWN_PROBES))


def reboot_camera(cam: Camera, probe: Callable[[str], bool] = _port_open,
                  clock: Callable[[], float] = time.monotonic,
                  sleep: Callable[[float], None] = time.sleep,
                  popen=subprocess.Popen) -> str:
    """Reboot through RMS's CameraControl and confirm the camera went down.

    Returns 'rebooted', 'unconfirmed' (command ran, control port never
    dropped), 'unreachable' (no answer before the command) or 'failed'.
    """
    if not cam.ip:
        return 'failed'
    if not _answers(cam.ip, probe):
        return 'unreachable'
    cmd = [cam.process.python, '-m', 'Utils.CameraControl', '-c', str(cam.rms.config), 'reboot']
    try:
        proc = popen(cmd, cwd=str(cam.process.cwd) if cam.process.cwd else None,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        return 'failed'
    went_down = False
    misses = 0
    deadline = clock() + REBOOT_CONFIRM_S
    while clock() < deadline and not went_down:
        misses = 0 if probe(cam.ip) else misses + 1
        went_down = misses >= DOWN_PROBES
        if not went_down:
            sleep(1.0)
    try:
        proc.wait(timeout=CLI_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 'rebooted' if went_down else 'unconfirmed'


def night_key(now: datetime) -> str:
    return fps.night_key(now)


def check(cameras: List[Camera], mode: str, state: dict, now: datetime,
          reboot: Callable[[Camera], str] = reboot_camera,
          pinger: Optional[Callable[[str], Optional[float]]] = None) -> List[str]:
    """One pass over the cameras; updates ``state`` in place, returns log lines.

    ``pinger`` (``ping_loss`` in production) enables the lossy-boot test."""
    out: List[str] = []
    acted = False
    night = night_key(now)
    for cam in cameras:
        code = cam.rms.code
        cs = state.get(code) or {}
        if cs.get('night') != night:
            cs = {'night': night}
        state[code] = cs
        if cam.rms.lat is not None and cam.rms.lon is not None:
            if rovimen_night.sun_altitude_deg(cam.rms.lat, cam.rms.lon, now) > NIGHT_SUN_ALT_DEG:
                continue
        log = fps.latest_log(cam.rms)
        if log is None:
            continue
        lines = fps._tail_lines(log, TAIL_BYTES)
        if fps.rms_mode(lines) == 'day':
            continue  # RMS has ended night capture and writes no FF files until sunset
        last_ours = datetime.fromisoformat(cs['last_reboot']) if cs.get('last_reboot') else None
        # The whole night: an outage can be hours old, and its start is judged
        # against the half hour before it.
        ffs = ff_times(cam.rms, now - timedelta(seconds=FF_LOOKBACK_S))
        session = session_start(cam.rms, now)
        v = assess(lines, ffs, cam.rms.fps, now, last_ours, session)
        if v is None and pinger is not None and session is not None:
            v = lossy_verdict(cam, cs, last_boot(lines, last_ours, session), now, pinger, out)
        if v is None:
            # No verdict also covers the minutes after a reboot; call it a
            # recovery only once FF files are arriving again. A lossy camera
            # never stopped delivering; lossy_verdict reports its recovery.
            if cs.get('bad_since') and cs.get('kind') != 'lossy' and ffs \
                    and (now - ffs[-1]).total_seconds() <= RECOVERED_FF_S:
                out.append(f'{code}: delivering frames again after {cs.get("kind", "trouble")} '
                           f'since {cs["bad_since"]}')
                cs.pop('bad_since', None)
                cs.pop('kind', None)
            continue
        cs.setdefault('bad_since', now.isoformat(timespec='seconds'))
        cs['kind'] = v.kind
        head = f'{code}: {v.kind.upper()} {v.detail}, since {cs["bad_since"]}'
        why_not = decide(cs, v, now)
        if mode != 'fix':
            out.append(f'{head}; mode={mode}, not rebooting')
        elif not cam.ip:
            out.append(f'{head}; not rebooting: no camera IP in the RMS device URL')
        elif why_not:
            out.append(f'{head}; not rebooting: {why_not}')
        elif acted:
            out.append(f'{head}; not rebooting: one reboot per run')
            if v.kind == 'lossy':
                cs['lossy_deferred'] = cs['ping_boot']
        else:
            acted = True
            result = reboot(cam)
            if result == 'unreachable':
                cs['unreachable'] = True
            else:
                stamp = now.isoformat(timespec='seconds')
                cs[f'{v.kind}_reboots'] = cs.get(f'{v.kind}_reboots', 0) + 1
                cs[f'last_{v.kind}_reboot'] = stamp
                cs['last_reboot'] = stamp  # any reboot gives the camera time (assess)
                if v.outage is not None:
                    key = v.outage.isoformat()
                    cs['outage_reboots'] = cs.get('outage_reboots', 0) + 1 if cs.get('outage') == key else 1
                    cs['outage'] = key
            cs['last_result'] = result
            out.append(f'{head}; camera reboot via RMS CameraControl ({cam.ip}): {result}')
    return out


def cameras_now() -> List[Camera]:
    cams: List[Camera] = []
    for p in fps.rms_processes():
        rms = fps.parse_rms_config(p.config)
        if rms:
            cams.append(Camera(rms, p, camera_ip(p.config)))
    return cams


def main() -> int:
    cfg = fps._load_json(CONFIG_PATH)
    mode = guard_mode(cfg)
    if mode == 'off':
        return 0
    cams = cameras_now()
    state = fps._load_json(STATE_PATH)
    lines = check(cams, mode, state, datetime.now(timezone.utc), pinger=ping_loss)
    print(f'cam_stall status mode={mode} cameras={",".join(c.rms.code for c in cams) or "none"}')
    for line in lines:
        print(f'cam_stall {line}')
    fps._save_state(state, STATE_PATH)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # the watchdog must never fail because of the guard
        print(f'cam_stall ERROR {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(0)
