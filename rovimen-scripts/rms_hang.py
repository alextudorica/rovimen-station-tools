#!/usr/bin/env python3
"""rms_hang.py - restart an RMS capture that hung, while dawn waits on it.

When the disk fills, RMS logs "[Errno 28] No space left on device" (often
after "The compressor has died, restarting the capture!") and then goes
silent with its processes still alive. It never finishes the night: no
detection, no FTPdetectinfo, no upload. Dawn then logs "Waiting for RMS to
finish" every 15 minutes until its wait deadline, and the camera stays hung
until the station's next reboot. gmnro02 (Vaslui) hung this way at 23:58 on
2026-09-24 and gmnro14 (Chiribiș) at 22:08 on 2026-10-16.

Signal: a camera dawn is waiting on whose RMS process is alive but whose
newest RMS log has not been written for HANG_MINUTES. RMS logs every frame
block while it captures and every FF file while it detects, so a live,
unfinished RMS is rarely that quiet. Post-processing can be: since dawn also
waits for RMS's archiving (dawn_process._rms_night), it sees quiet steps such
as timelapse preparation (47 min on gmnro09 (Sibiciu)) and bz2 archiving. So a
quiet log alone is not a hang: the RMS process tree must also have used under
CPU_IDLE_FRACTION of one core over at least CPU_WINDOW_MIN, measured between
two dawn ticks (the sample is kept in the night state under CPU_KEY). A log
that ends in one of the _HANG_MARKERS (within its last MARKER_TAIL_LINES
lines) is a hang whatever the CPU says, because after "No space left on
device" the capture process can keep spinning on frames. A marker further back
is not: RMS often logs "The compressor has died" and then carries on.

CPU alone misreads an RMS that is waiting on a saturated disk: on gmnro05
(Bistrița), five RMS post-processing at once on one HDD sat in D state with
/proc/pressure/io "full" near 90%, each using under 1% of a core while still
reading 2-3 MB/s, and every dawn from 2026-11-13 restarted 3-6 of them, so
each restart redid that camera's night and added to the load. So the sample
also carries the tree's storage I/O (read_bytes + write_bytes from
/proc/<pid>/io, per PID), and an RMS that moved IO_MIN_BYTES_PER_S on average
over the window is progressing, not hung. A process stuck in D state on a dead
disk moves no bytes, so it still counts as hung.

Action (``services.rms_hang.mode: "fix"``, the default): restart the systemd
unit that runs that RMS process, found from its cgroup (system units with
``sudo -n systemctl``, user units with ``systemctl --user``). RMS with
``auto_reprocess: true`` then detects and uploads the unfinished night on
start. At most once per camera-night, and never while the disk still has
less than MIN_FREE_GB free, because RMS would only hang again. ``"report"``
only logs; ``enabled: false`` turns it off.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

import camera_fps_guard as fps
import flags_manager

logger = logging.getLogger(__name__)

HANG_MINUTES = 30
MIN_FREE_GB = 5.0
RESTART_TIMEOUT_S = 90
TAIL_BYTES = 64 * 1024
# A log "ends in" a hang marker when it is among these last lines (an ENOSPC
# traceback runs to about a dozen). Further back, RMS logged on after it: it
# often recovers from a dead compressor and keeps working.
MARKER_TAIL_LINES = 20
# Night-state key recording the restart, so a camera is restarted once a night.
STATE_KEY = 'rms_hang_restart'
# Night-state key holding the last CPU sample of a quiet RMS: {pid, ticks, at}.
CPU_KEY = 'rms_hang_cpu'
# A quiet RMS counts as hung only after this long with under this share of one
# core (bz2 archiving or a timelapse keeps a core near 100%).
CPU_WINDOW_MIN = 10
CPU_IDLE_FRACTION = 0.01
# ...and under this much storage I/O per second (an RMS starved by a busy disk
# still reads or writes tens of kB/s; a hung one, nothing).
IO_MIN_BYTES_PER_S = 1024
# What the log says just before RMS hangs, most specific first.
_HANG_MARKERS = ('No space left on device', 'The compressor has died')
_RMS_UNIT_RE = re.compile(r'^(rms|gmn)[\w@.-]*\.service$')


class HungRms(NamedTuple):
    code: str
    config: Path
    log: Path
    idle_min: float
    reason: str
    unit: Optional[str]
    user_scope: bool
    auto_reprocess: bool
    pid: int = 0
    cpu_ticks: Optional[int] = None
    io_bytes: Optional[Dict[int, int]] = None


def hang_mode(cfg: dict) -> str:
    """``off`` / ``report`` / ``fix`` from services.rms_hang."""
    svc = (cfg.get('services') or {}).get('rms_hang') or {}
    if not isinstance(svc, dict) or not svc.get('enabled', True):
        return 'off'
    mode = svc.get('mode', 'fix')
    return mode if mode in ('report', 'fix') else 'report'


def unit_of(pid: int, proc: Path = Path('/proc')) -> Tuple[Optional[str], bool]:
    """The systemd service running *pid* and whether it is a user unit.

    Read from the process's cgroup path, e.g. ``0::/system.slice/rms-cam1.service``
    or ``0::/user.slice/user-1000.slice/user@1000.service/app.slice/rms-cam1.service``.
    (None, False) for a process no RMS service owns: started from a shell, or
    from cron, whose own cron.service must never be restarted for it. The
    fleet's RMS units are rms-cam{N}, rms-capture[-cam{N}], gmn-capture[-cam{N}]
    and rms.service (station_collect._RMS_UNIT_FAMILIES).
    """
    try:
        text = (proc / str(pid) / 'cgroup').read_text()
    except OSError:
        return None, False
    for line in text.splitlines():
        path = line.split(':', 2)[-1]
        parts = path.split('/')
        services = [p for p in parts if p.endswith('.service') and not p.startswith('user@')]
        if services and _RMS_UNIT_RE.match(services[-1]):
            return services[-1], any(p.startswith('user@') for p in parts)
    return None, False


def _stat_fields(pid: str, proc: Path) -> Optional[List[str]]:
    """/proc/<pid>/stat from field 3 (state) on; comm may hold spaces and ')'."""
    try:
        return (proc / pid / 'stat').read_text().rsplit(')', 1)[1].split()
    except (OSError, IndexError):
        return None


def _process_tree(pid: int, proc: Path) -> Tuple[List[int], Dict[int, int]]:
    """(PIDs of *pid* and its descendants, CPU ticks per PID); ([], {}) if *pid* is gone."""
    children: Dict[int, List[int]] = {}
    ticks: Dict[int, int] = {}
    for entry in proc.iterdir() if proc.is_dir() else []:
        if not entry.name.isdigit():
            continue
        f = _stat_fields(entry.name, proc)
        if f is None or len(f) < 15:
            continue
        try:
            ticks[int(entry.name)] = sum(int(v) for v in f[11:15])
            children.setdefault(int(f[1]), []).append(int(entry.name))
        except ValueError:
            continue
    if pid not in ticks:
        return [], ticks
    tree, todo = [], [pid]
    while todo:
        p = todo.pop()
        tree.append(p)
        todo.extend(children.get(p, []))
    return tree, ticks


def process_tree_cpu_ticks(pid: int, proc: Path = Path('/proc')) -> Optional[int]:
    """CPU clock ticks used by *pid* and every descendant, or None if *pid* is gone.

    utime + stime + cutime + cstime of each process in the tree, so the time of
    a worker that has exited and been reaped stays in its parent's total.
    """
    tree, ticks = _process_tree(pid, proc)
    return sum(ticks.get(p, 0) for p in tree) if tree else None


def _io_bytes(pid: int, proc: Path) -> Optional[int]:
    """read_bytes + write_bytes of one process (storage I/O, not page-cache hits)."""
    try:
        fields = dict(line.split(':', 1) for line in (proc / str(pid) / 'io').read_text().splitlines()
                      if ':' in line)
        return int(fields['read_bytes']) + int(fields['write_bytes'])
    except (OSError, KeyError, ValueError):
        return None


def process_tree_io_bytes(pid: int, proc: Path = Path('/proc')) -> Optional[Dict[int, int]]:
    """Storage I/O bytes per PID in *pid*'s tree, or None if none is readable.

    Per PID, because a reaped worker's I/O leaves the tree (unlike its CPU
    time, which cutime keeps): io_progress() sums each PID's own growth.
    """
    tree, _ticks = _process_tree(pid, proc)
    out = {p: b for p in tree for b in [_io_bytes(p, proc)] if b is not None}
    return out or None


def io_progress(prev: object, now: Dict[int, int]) -> int:
    """Bytes the tree moved since *prev* (a stored {pid: bytes}); new PIDs count whole."""
    before: Dict[int, int] = {}
    if isinstance(prev, dict):
        for k, v in prev.items():
            try:
                before[int(k)] = int(v)
            except (TypeError, ValueError):
                continue
    return sum(max(0, b - before.get(p, 0)) for p, b in now.items())


def _clock_ticks() -> int:
    try:
        return int(os.sysconf('SC_CLK_TCK'))
    except (ValueError, OSError, AttributeError):
        return 100


def cpu_stalled(h: HungRms, date_str: str, cfg: dict, now: datetime) -> bool:
    """True once a quiet RMS's process tree has made neither CPU nor I/O progress.

    Compares with the sample an earlier dawn tick left in the night state; the
    first sight of a quiet RMS (or a new PID) only records one. A sample younger
    than CPU_WINDOW_MIN is kept as it is, so the window always spans at least
    that long. Without a CPU reading (the PID vanished between the scans) the
    quiet log is the only evidence, as before the CPU check existed; without an
    I/O reading in either sample, CPU decides alone.
    """
    if h.cpu_ticks is None:
        return True
    prev = flags_manager.peek(h.code, date_str, cfg).get(CPU_KEY)
    if isinstance(prev, dict) and prev.get('pid') == h.pid:
        try:
            age_s = (now - datetime.fromisoformat(prev['at'])).total_seconds()
            used_s = (h.cpu_ticks - int(prev['ticks'])) / _clock_ticks()
        except (KeyError, TypeError, ValueError):
            age_s, used_s = -1.0, 0.0
        if 0 <= age_s < CPU_WINDOW_MIN * 60:
            return False
        if age_s >= 0 and used_s < CPU_IDLE_FRACTION * age_s:
            moved = (io_progress(prev.get('io'), h.io_bytes)
                     if h.io_bytes is not None and prev.get('io') is not None else None)
            if moved is None or moved < IO_MIN_BYTES_PER_S * age_s:
                return True
            logger.info('[%s] RMS log quiet and CPU idle, but RMS moved %.1f MB on disk in the '
                        'last %.0f min: waiting on the disk, not hung', h.code, moved / 1e6, age_s / 60)
        elif age_s >= 0:
            logger.info('[%s] RMS log quiet, but RMS used %.0f s of CPU in the last %.0f min: '
                        'still working, not hung', h.code, used_s, age_s / 60)

    def _sample(state: dict, pid: int = h.pid, ticks: int = h.cpu_ticks,
                io: Optional[Dict[int, int]] = h.io_bytes) -> None:
        state[CPU_KEY] = {'pid': pid, 'ticks': ticks, 'at': now.isoformat()}
        if io is not None:
            state[CPU_KEY]['io'] = {str(k): v for k, v in io.items()}

    flags_manager.edit_state(h.code, date_str, cfg, _sample)
    return False


def _auto_reprocess(config: Path) -> bool:
    try:
        value = fps._config_value(config.read_text(errors='replace'), 'auto_reprocess')
    except OSError:
        return False
    return (value or '').strip().lower() in ('true', '1', 'yes')


def find_hung(codes: Iterable[str], now: datetime, hang_minutes: float = HANG_MINUTES,
              proc: Path = Path('/proc')) -> List[HungRms]:
    """The cameras among *codes* whose running RMS has stopped logging.

    Only the log is judged here; recover() adds the CPU check (cpu_stalled)
    for a log that went quiet without a hang marker.
    """
    wanted = set(codes)
    hung: List[HungRms] = []
    for pid, p in fps.rms_process_pids(proc):
        cam = fps.parse_rms_config(p.config)
        if cam is None or cam.code not in wanted:
            continue
        log = fps.latest_log(cam)
        if log is None:
            continue
        try:
            idle_min = (now.timestamp() - log.stat().st_mtime) / 60
            tail = '\n'.join(fps._tail_lines(log, TAIL_BYTES)[-MARKER_TAIL_LINES:])
        except OSError:
            continue
        if idle_min < hang_minutes:
            continue
        reason = next((m for m in _HANG_MARKERS if m in tail), 'log idle')
        unit, user_scope = unit_of(pid, proc)
        hung.append(HungRms(cam.code, p.config, log, idle_min, reason, unit, user_scope,
                            _auto_reprocess(p.config), pid, process_tree_cpu_ticks(pid, proc),
                            process_tree_io_bytes(pid, proc)))
    return hung


def restart_unit(unit: str, user_scope: bool) -> Tuple[bool, str]:
    """Restart one systemd unit; (ok, error text)."""
    if user_scope:
        cmd = ['systemctl', '--user', 'restart', unit]
        env = dict(os.environ)
        env.setdefault('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
    else:
        cmd = ['sudo', '-n', 'systemctl', 'restart', unit]
        env = None
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=RESTART_TIMEOUT_S, env=env)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    return r.returncode == 0, (r.stderr or r.stdout).strip()


def _free_gb(path: Path) -> Optional[float]:
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return None


def recover(waiting: Iterable[str], date_str: str, cfg: dict,
            now: Optional[datetime] = None,
            restart: Callable[[str, bool], Tuple[bool, str]] = restart_unit,
            proc: Path = Path('/proc')) -> List[str]:
    """Restart every hung RMS among the cameras dawn waits on; returns the codes restarted."""
    mode = hang_mode(cfg)
    if mode == 'off':
        return []
    now = now or datetime.now(timezone.utc)
    restarted: List[str] = []
    for h in find_hung(waiting, now, proc=proc):
        head = (f'[{h.code}] RMS looks hung: log {h.log.name} idle {h.idle_min:.0f} min '
                f'({h.reason})')
        if flags_manager.peek(h.code, date_str, cfg).get(STATE_KEY):
            logger.warning('%s — already restarted once this night, leaving it', head)
            continue
        if h.reason == 'log idle':
            if not cpu_stalled(h, date_str, cfg, now):
                continue
            head += ', no CPU or disk progress'
        if h.unit is None:
            logger.warning('%s — no systemd unit runs it, restart it by hand', head)
            continue
        free = _free_gb(h.log.parent)
        if free is not None and free < MIN_FREE_GB:
            logger.warning('%s — only %.1f GB free, not restarting %s until the janitor frees space',
                           head, free, h.unit)
            continue
        if mode == 'report':
            logger.warning('%s — would restart %s (services.rms_hang.mode is "report")', head, h.unit)
            continue
        ok, err = restart(h.unit, h.user_scope)
        if not ok:
            logger.error('%s — restarting %s failed: %s', head, h.unit, err or 'no output')
            continue
        stamp = now.isoformat()

        def _mark(state: dict, stamp: str = stamp, unit: str = h.unit) -> None:
            state[STATE_KEY] = {'at': stamp, 'unit': unit}

        flags_manager.edit_state(h.code, date_str, cfg, _mark)
        restarted.append(h.code)
        if h.auto_reprocess:
            logger.warning('%s — restarted %s; RMS reprocesses the night on start', head, h.unit)
        else:
            logger.warning('%s — restarted %s, but auto_reprocess is off in %s, so the night '
                           'stays unprocessed until someone runs RMS on it', head, h.unit, h.config)
    return restarted
