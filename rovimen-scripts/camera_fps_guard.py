#!/usr/bin/env python3
"""camera_fps_guard.py - notice a camera running below its frame rate at night,
and re-apply its settings.

An XMEye camera whose image settings revert (EsShutter back to slow shutter,
exposure unpinned) halves its frame rate once the sky is dark: DE001E
(Johannismühle) ran at ~12 fps all night on 2026-09-27 after its sunset mode
switch rebooted it into factory image settings, and nothing repaired it,
because fix_cam_encoding.sh runs only at boot and at 15:30 UTC.

Signal: RMS's own log. RMS logs "Grabbing a new block of 256 frames" each time
it starts a block, so consecutive lines are 256 / fps seconds apart (10.24 s at
25 fps) while the camera delivers its full rate, and ~21 s at 12 fps. A camera
is slow when the median gap over the last few minutes is at least SLOW_RATIO
times nominal, after sunset (sun below NIGHT_SUN_ALT_DEG) when slow shutter
engages, and while RMS's newest mode switch is not to day: RMS ends night
capture at its own sunrise (sun near -9 deg) and puts the camera in day mode,
where gmnro22 (Ferigile) read 11.9-14.2 fps every morning from 04:37 UTC.

Action (``services.fps_guard.mode: "fix"``): ``fix_cam_encoding.sh --only
<CODE>``, the fleet's settings enforcement for that one camera. It writes the
night values without a camera reboot and never re-IPs a camera. At most one
action per run, COOLDOWN_S apart and MAX_ACTIONS_PER_NIGHT per camera; a
camera fix_cam cannot reach is reported, not retried. ``"report"`` only logs.

Run by rovimen_watchdog.sh every 3 minutes; prints one line per finding for the
watchdog log and keeps its state in ~/rovimen_scripts/fps_guard_state.json.
"""
from __future__ import annotations

import json
import os
import re
import statistics
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

import rovimen_night

SCRIPTS_DIR = Path.home() / 'rovimen_scripts'
CONFIG_PATH = SCRIPTS_DIR / 'config.json'
STATE_PATH = SCRIPTS_DIR / 'fps_guard_state.json'
FIX_CAM = SCRIPTS_DIR / 'fix_cam_encoding.sh'

WINDOW_S = 240              # gaps measured over the last 4 minutes
MIN_GAPS = 5                # fewer gaps than this: not enough evidence
STALE_S = 90                # no block for this long: RMS is not capturing
SLOW_RATIO = 1.3            # median gap >= 1.3 x nominal (25 fps -> < 19 fps)
NIGHT_SUN_ALT_DEG = -6.0    # slow shutter engages in low light only
COOLDOWN_S = 1800
MAX_ACTIONS_PER_NIGHT = 3
FIX_TIMEOUT_S = 150
TAIL_BYTES = 256 * 1024

_BLOCK_RE = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\S*.*Grabbing a new block of (\d+) frames')
# CaptureModeSwitcher: "Next event is a sunset (...), switching to daytime mode"
_MODE_RE = re.compile(r'^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\S*.*switching to (day|night)time mode')


class RmsCamera(NamedTuple):
    code: str
    config: Path
    fps: float
    lat: Optional[float]
    lon: Optional[float]
    log_dirs: List[Path]


class Measurement(NamedTuple):
    median_gap_s: float
    nominal_gap_s: float
    gaps: int
    last_block: datetime

    @property
    def ratio(self) -> float:
        return self.median_gap_s / self.nominal_gap_s


def _config_value(text: str, key: str) -> Optional[str]:
    m = re.search(rf'(?m)^{re.escape(key)}:\s*([^;\n]*?)\s*(?:;.*)?$', text)
    return m.group(1) if m else None


def _float(value: Optional[str]) -> Optional[float]:
    try:
        return float(value) if value not in (None, '') else None
    except ValueError:
        return None


def parse_rms_config(config: Path) -> Optional[RmsCamera]:
    """The camera an RMS .config captures, or None when it has no stationID."""
    try:
        text = config.read_text(errors='replace')
    except OSError:
        return None
    code = _config_value(text, 'stationID')
    if not code:
        return None
    data_dir = _config_value(text, 'data_dir') or '~/RMS_data'
    log_dir = _config_value(text, 'log_dir') or 'logs'
    base = Path(os.path.expanduser(data_dir))
    if not base.is_absolute():
        base = config.parent / base
    dirs = [base / log_dir, base / code / log_dir]
    return RmsCamera(code, config, _float(_config_value(text, 'fps')) or 25.0,
                     _float(_config_value(text, 'latitude')),
                     _float(_config_value(text, 'longitude')), dirs)


class RmsProcess(NamedTuple):
    config: Path
    python: str             # the interpreter RMS runs under (its venv)
    cwd: Optional[Path]     # the RMS checkout, where ``-m Utils.CameraControl`` resolves


def rms_processes(proc: Path = Path('/proc')) -> List[RmsProcess]:
    """Every running RMS.StartCapture, one per .config: its ``-c`` argument, or
    ``.config`` in its working directory (rms.service on gmnro18 (Bisoca) and
    the user units on gmnro06 (Cluj) run RMS from ~/source/RMS with no -c)."""
    return [p for _, p in rms_process_pids(proc)]


def rms_process_pids(proc: Path = Path('/proc')) -> List[Tuple[int, RmsProcess]]:
    """rms_processes() with the lowest PID found running each one."""
    found: List[Tuple[int, RmsProcess]] = []
    pids = sorted((e for e in proc.iterdir() if e.name.isdigit()), key=lambda e: int(e.name)) if proc.is_dir() else []
    for entry in pids:
        try:
            argv = (entry / 'cmdline').read_bytes().decode(errors='replace').split('\0')
        except OSError:
            continue
        if 'RMS.StartCapture' not in argv:
            continue
        try:
            cwd: Optional[Path] = Path(os.readlink(entry / 'cwd'))
        except OSError:
            cwd = None
        config = None
        for flag in ('-c', '--config'):
            if flag in argv and argv.index(flag) + 1 < len(argv):
                config = Path(argv[argv.index(flag) + 1])
                break
        if config is None:
            if cwd is None:
                continue
            config = cwd / '.config'
        if not config.is_absolute():
            if cwd is None:
                continue
            config = cwd / config
        python = argv[0]
        if not os.path.isabs(python):
            try:
                python = os.readlink(entry / 'exe')
            except OSError:
                pass
        if all(p.config != config for _, p in found):
            found.append((int(entry.name), RmsProcess(config, python, cwd)))
    return found


def running_rms_configs(proc: Path = Path('/proc')) -> List[Path]:
    """The .config of every running RMS.StartCapture (see rms_processes)."""
    return [p.config for p in rms_processes(proc)]


def latest_log(cam: RmsCamera) -> Optional[Path]:
    logs: List[Path] = []
    for d in cam.log_dirs:
        if d.is_dir():
            logs.extend(d.glob(f'log_{cam.code}_*.log'))
    return max(logs, key=lambda p: p.stat().st_mtime) if logs else None


def _tail_lines(path: Path, nbytes: int = TAIL_BYTES) -> List[str]:
    with open(path, 'rb') as f:
        f.seek(0, os.SEEK_END)
        f.seek(max(0, f.tell() - nbytes))
        return f.read().decode(errors='replace').splitlines()


# The capture-state lines RMS.StartCapture logs, matched by substring:
# "Starting capture for 11.52 hours", "Ending capture..." (also "Ending capture
# before reboot..."), "Waiting 0:41:12 to start recording for 11.493 hrs".
_CAPTURE_STATE_MARKERS = (
    ('Starting capture for', 'capturing'),
    ('Ending capture', 'ended'),
    ('to start recording for', 'waiting'),
)


def rms_capture_state(lines: List[str]) -> Optional[str]:
    """The newest capture state in an RMS log tail, or None if it shows none.

    ``capturing`` while a night runs; ``ended`` once capture stopped (RMS then
    detects and post-processes the night); ``waiting`` once RMS is idle until
    its next night, which it logs only after processing and uploading.
    """
    for line in reversed(lines):
        for marker, state in _CAPTURE_STATE_MARKERS:
            if marker in line:
                return state
    return None


def rms_mode(lines: List[str]) -> Optional[str]:
    """'day' or 'night' from RMS's newest mode switch in the log tail; None
    when RMS does not switch modes (no continuous capture) or the switch is
    older than the tail.

    RMS ends night capture at its own sunrise (sun near -9 deg) and puts the
    camera in day mode, so a sun-altitude test alone still reads that as night.
    Shared by the frame-rate and stall guards.
    """
    mode = None
    for m in map(_MODE_RE.match, lines):
        if m:
            mode = m.group(2)
    return mode


def measure(lines: List[str], fps: float, now: datetime) -> Optional[Measurement]:
    """Median gap between RMS block starts over the last WINDOW_S seconds.

    None when there is too little to judge: RMS not capturing (no block for
    STALE_S), or fewer than MIN_GAPS gaps in the window.
    """
    stamps: List[datetime] = []
    block = 256
    for line in lines:
        m = _BLOCK_RE.match(line)
        if m:
            stamps.append(datetime.strptime(m.group(1), '%Y/%m/%d %H:%M:%S').replace(tzinfo=timezone.utc))
            block = int(m.group(2))
    if not stamps or (now - stamps[-1]).total_seconds() > STALE_S:
        return None
    recent = [t for t in stamps if (now - t).total_seconds() <= WINDOW_S]
    gaps = [(b - a).total_seconds() for a, b in zip(recent, recent[1:])]
    if len(gaps) < MIN_GAPS:
        return None
    return Measurement(statistics.median(gaps), block / fps, len(gaps), stamps[-1])


def night_key(now: datetime) -> str:
    """The capture night a moment belongs to (its evening's date)."""
    return (now - timedelta(hours=12)).strftime('%Y%m%d')


def guard_mode(cfg: dict) -> str:
    """``off`` / ``report`` / ``fix`` from services.fps_guard."""
    svc = (cfg.get('services') or {}).get('fps_guard') or {}
    if not isinstance(svc, dict) or not svc.get('enabled', True):
        return 'off'
    mode = svc.get('mode', 'report')
    return mode if mode in ('report', 'fix') else 'report'


def decide(cam_state: dict, night: str, now: datetime) -> Optional[str]:
    """Why not to act on a slow camera now, or None to act."""
    if cam_state.get('night') != night:
        return None
    if cam_state.get('unreachable'):
        return 'fix_cam could not reach it earlier tonight'
    if cam_state.get('actions', 0) >= MAX_ACTIONS_PER_NIGHT:
        return f'{MAX_ACTIONS_PER_NIGHT} re-applies already tonight'
    last = cam_state.get('last_action')
    if last:
        age = (now - datetime.fromisoformat(last)).total_seconds()
        if age < COOLDOWN_S:
            return f're-applied {int(age // 60)} min ago'
    return None


def run_fix(code: str) -> int:
    """fix_cam_encoding.sh --only <code>: 0 ok, 2 unreachable, other = failed."""
    env = dict(os.environ, FIXCAM_WAIT_ATTEMPTS='2')
    try:
        return subprocess.run(['bash', str(FIX_CAM), '--only', code], env=env,
                              timeout=FIX_TIMEOUT_S, capture_output=True).returncode
    except subprocess.TimeoutExpired:
        return 124


def _load_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(state: dict, path: Path) -> None:
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(path)


def check(cameras: List[RmsCamera], mode: str, state: dict, now: datetime,
          fix=run_fix) -> List[str]:
    """One pass over the cameras; updates ``state`` in place, returns log lines."""
    out: List[str] = []
    acted = False
    night = night_key(now)
    for cam in cameras:
        log = latest_log(cam)
        if log is None:
            continue
        lines = _tail_lines(log)
        m = measure(lines, cam.fps, now)
        cs = state.get(cam.code) or {}
        if cs.get('night') != night:
            cs = {'night': night}
        day_mode = rms_mode(lines) == 'day'
        if not day_mode:
            cs.pop('rms_day', None)
        if m is None:  # RMS not capturing, or too few blocks yet: no verdict
            state[cam.code] = cs
            continue
        if m.ratio < SLOW_RATIO:
            if cs.get('slow_since'):
                out.append(f'{cam.code}: back to normal ({cam.fps / m.ratio:.1f} fps) '
                           f'after slow since {cs["slow_since"]}')
                cs.pop('slow_since', None)
            state[cam.code] = cs
            continue
        if cam.lat is not None and cam.lon is not None:
            if rovimen_night.sun_altitude_deg(cam.lat, cam.lon, now) > NIGHT_SUN_ALT_DEG:
                state[cam.code] = cs
                continue
        if day_mode:
            # RMS ended night capture at its own sunrise and switched the
            # camera to day mode, where the frame rate changes legitimately.
            if not cs.get('rms_day'):
                cs['rms_day'] = now.isoformat(timespec='seconds')
                out.append(f'{cam.code}: skipped: RMS in day mode')
            state[cam.code] = cs
            continue
        fps_now = cam.fps / m.ratio
        cs.setdefault('slow_since', now.isoformat(timespec='seconds'))
        cs['last_fps'] = round(fps_now, 1)
        head = (f'{cam.code}: SLOW {fps_now:.1f} fps of {cam.fps:g} (median block gap '
                f'{m.median_gap_s:.1f} s vs {m.nominal_gap_s:.2f} s over {m.gaps} gaps), '
                f'slow since {cs["slow_since"]}')
        why_not = decide(cs, night, now)
        if mode != 'fix':
            out.append(f'{head}; mode={mode}, not re-applying')
        elif why_not:
            out.append(f'{head}; not re-applying: {why_not}')
        elif acted:
            out.append(f'{head}; not re-applying: one re-apply per run')
        else:
            acted = True
            rc = fix(cam.code)
            cs['actions'] = cs.get('actions', 0) + 1
            cs['last_action'] = now.isoformat(timespec='seconds')
            result = {0: 'settings re-applied', 2: 'camera unreachable over DVRIP'}.get(rc, f'fix_cam failed (rc={rc})')
            cs['last_result'] = result
            if rc == 2:
                cs['unreachable'] = True
            out.append(f'{head}; ran fix_cam_encoding.sh --only {cam.code}: {result}')
        state[cam.code] = cs
    return out


def main() -> int:
    cfg = _load_json(CONFIG_PATH)
    mode = guard_mode(cfg)
    if mode == 'off':
        return 0
    cameras = [c for c in (parse_rms_config(p) for p in running_rms_configs()) if c]
    state = _load_json(STATE_PATH)
    lines = check(cameras, mode, state, datetime.now(timezone.utc))
    for line in lines:
        print(f'fps_guard {line}')
    _save_state(state, STATE_PATH)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception as exc:  # the watchdog must never fail because of the guard
        print(f'fps_guard ERROR {type(exc).__name__}: {exc}', file=sys.stderr)
        raise SystemExit(0)
