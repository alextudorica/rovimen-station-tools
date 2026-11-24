#!/usr/bin/env python3
"""dawn_process.py — Post-RMS morning processing cron job.

Codename: sunheart

Runs every 15 minutes from 04:00 UTC (cron ``*/15 4-12``) and starts nothing
past the 13:15 UTC deadline.  Exits immediately if RMS has not finished the
night (detection and post-processing, see _rms_night), if colour capture is
still writing the night's chunks (_colour_recording), or if morning_done is
already True for a station.  When all cameras have finished RMS, runs the
morning pipeline. Detections are
processed FIRST so a station too slow to finish stacking before the reboot still
gets its meteors locked, encoded and published (the old stack-everything-first
order starved the lock pass, and recent nights showed no detections):

  Detection path (always, first):
    Phase 1 — EON detection pass; stack, then encode, the locked colour clips
    Phase 3 — Upload locked clips to archive
  Bulk path (lower priority; skipped past the 13:15 UTC deadline):
    Phase 4 — Stack all cameras + verify/rerun for gaps, then the colour
              detection night-stack (Phase 2, needs every locked chunk's stack).
              A camera is only started if its whole night fits before the
              deadline at the measured rate (StackPlan); the others keep their
              locked stacks only and get no timelapse that night
    Phase 5 — Upload full-night stacks + backfill previous nights
    Phase 6 — Timelapse (needs the stacks only; minutes per camera)
    Phase 7 — Bulk reencode remaining chunks (long-running; idempotent;
              gated by services.reencode.full_night, off on libx264-only boxes)

morning_done is set per camera once the bulk path completes (Phase 7), or, past
the deadline, once the detection path is done (bulk deferred to a later run).

Cron entry — installed to /etc/cron.d/rovimen-dawn:
  */15 4-12 * * * {user} {venv}/bin/python {scripts}/dawn_process.py \\
      >> ~/logs/rovimen_dawn.log 2>&1

Standalone usage:
  python dawn_process.py [-c config.json] [--station RO000H] [--date 20260322]
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import zip_longest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, Iterator, List, Tuple

import archive_upload
import camera_fps_guard
import detection_lock
import encoder
import overlay_details
import stacker
import flags_manager
import rovimen_config
import rovimen_lock
import rovimen_media
import rovimen_night
import rms_hang
import timelapse_build

_LOCK_FILE = rovimen_night.DAWN_LOCK_FILE

logger = logging.getLogger('dawn_process')

DEFAULT_CONFIG = Path.home() / 'rovimen_scripts' / 'config.json'



# Bulk processing must be finished by 13:15 UTC. The fleet reboots at a fixed
# 13:45 UTC (reboot_guard cron), 34 min before the earliest sunset anywhere in
# the fleet (14:19 UTC, Iasi, early December; capture starts later still, once
# the sun is well below the horizon), so the deadline leaves the guard 30 min
# for the hard stop to mark the night done. Was 11:00 until 2026-09-23 and
# 12:45 until 2026-09-24; each move gives the bulk path more of the idle day.
_DEADLINE_HOUR = 13
_DEADLINE_MINUTE = 15
_ABORT_GRACE_S = 300       # how long a cooperative stop gets before the kill
# How long the hard stop waits for its bookkeeping before killing anyway: the
# bookkeeping takes the same per-night locks a worker stuck on a hung disk
# (the gmnro03 USB/UAS hang) may be holding.
_HARD_STOP_BOOKKEEPING_S = 60.0


# ---------------------------------------------------------------------------
# The 13:15 UTC bulk deadline
# ---------------------------------------------------------------------------

def _terminate_children() -> None:
    """SIGKILL every direct child of this process (the ffmpeg workers)."""
    me = os.getpid()
    for entry in Path('/proc').iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # /proc/<pid>/stat: "pid (comm) state ppid ..." — comm can contain
            # spaces and parentheses, so split after the LAST ')'.
            ppid = int(entry.joinpath('stat').read_text().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        if ppid != me:
            continue
        try:
            os.kill(int(entry.name), signal.SIGKILL)
        except OSError:
            pass


class BulkDeadline:
    """Stops the bulk path at 13:15 UTC, however long it has been running.

    The deadline used to be read once, just before Phase 4. A run that started
    at 04:00 and got that far therefore never checked it again: gmnro10 and
    gmnro12 stacked for more than ten hours every morning, and the reboot guard
    (then at 12:00, now 13:45) — which waits for ``morning_done`` and
    deliberately does not reboot on timeout — left those stations up all day.

    So the deadline has to fire mid-run. Call the instance to ask whether to
    stop; it is the ``should_abort`` callback the stacker and encoder poll per
    chunk. ``arm()`` also starts a watchdog thread that escalates: at the
    deadline it sets the flag, and if the bulk path has not unwound
    ``grace_s`` later it kills the ffmpeg children and exits the process,
    running ``on_hard_stop`` first so the night is still marked done and the
    reboot guard is not left waiting on a process that no longer exists.
    """

    def __init__(self, enabled: bool, hour: int = _DEADLINE_HOUR,
                 grace_s: float = _ABORT_GRACE_S,
                 on_hard_stop: Callable[[], None] | None = None,
                 minute: int | None = None,
                 bookkeeping_s: float = _HARD_STOP_BOOKKEEPING_S) -> None:
        self.enabled = enabled
        self.bookkeeping_s = bookkeeping_s
        self.hour = hour
        # An explicit hour with no minute means the top of that hour, so a
        # caller passing only ``hour`` keeps its meaning.
        self.minute = (_DEADLINE_MINUTE if hour == _DEADLINE_HOUR else 0) if minute is None else minute
        self.grace_s = grace_s
        self.on_hard_stop = on_hard_stop
        self.expired = threading.Event()
        self._finished = threading.Event()
        self._thread: threading.Thread | None = None

    def passed(self) -> bool:
        """True once the wall clock is past the deadline hour."""
        if not self.enabled:
            return False
        now = datetime.now(timezone.utc)
        return (now.hour, now.minute) >= (self.hour, self.minute)

    def __call__(self) -> bool:
        return self.expired.is_set() or self.passed()

    def seconds_left(self) -> float:
        now = datetime.now(timezone.utc)
        target = now.replace(hour=self.hour, minute=self.minute, second=0, microsecond=0)
        return max(0.0, (target - now).total_seconds())

    def arm(self) -> None:
        if not self.enabled or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._watch, name='bulk-deadline',
                                        daemon=True)
        self._thread.start()

    def disarm(self) -> None:
        self._finished.set()

    def _watch(self) -> None:
        if self._finished.wait(self.seconds_left()):
            return
        self.expired.set()
        logger.warning('%02d:%02d UTC deadline reached — stopping bulk processing '
                       'at the next chunk boundary', self.hour, self.minute)
        if self._finished.wait(self.grace_s):
            return
        logger.error('Bulk processing still running %.0fs past the deadline — '
                     'killing the encode/stack children and exiting',
                     self.grace_s)
        self._hard_stop()

    def _hard_stop(self) -> None:
        """Last resort: bookkeep, kill the ffmpeg children, leave.

        ``os._exit`` rather than ``sys.exit`` because this runs on the watchdog
        thread, where an exception only unwinds the watchdog. The bookkeeping
        runs first so the reboot guard is not left waiting on a process that no
        longer exists, but on its own thread and for at most ``bookkeeping_s``:
        it needs the per-night state locks, and a worker stuck on a hung disk
        can hold one forever, which used to hang the kill with it.
        """
        if self.on_hard_stop is not None:
            done = threading.Event()

            def _bookkeep() -> None:
                try:
                    self.on_hard_stop()
                except Exception:
                    logger.exception('Hard-stop bookkeeping failed')
                finally:
                    done.set()

            threading.Thread(target=_bookkeep, name='hard-stop-bookkeeping',
                             daemon=True).start()
            if not done.wait(self.bookkeeping_s):
                logger.error('Hard-stop bookkeeping still blocked after %.0fs (hung disk?) '
                             '— killing anyway', self.bookkeeping_s)
        _terminate_children()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(3)


# ---------------------------------------------------------------------------
# Night-date helpers
# ---------------------------------------------------------------------------

def _night_date() -> str:
    """Return the date string for the most recently completed night.

    ``rovimen_night.last_completed_night``: the cron window is 04:00–13:15
    UTC, so before 14:00 the night that just ended is under yesterday's date.
    (The boundary was noon, which would have sent a 12:00-13:15 run to
    tonight's empty directory.)
    """
    return rovimen_night.last_completed_night(datetime.now(timezone.utc))


# ---------------------------------------------------------------------------
# RMS completion check
# ---------------------------------------------------------------------------

# Past this UTC hour the gate stops waiting on cameras whose RMS has not
# finished: they get the rest of the morning pipeline without the EON pass (see
# _main_locked), so one stuck camera cannot block every camera and the reboot.
# Override with ``dawn_rms_wait_until_hour``.
_RMS_WAIT_UNTIL_HOUR = 10


@dataclass
class RmsNight:
    """What RMS has produced for one camera-night.

    ``sessions`` counts the night's session directories (CapturedFiles and
    ArchivedFiles copies of one session count once). ``unprocessed`` names the
    sessions that captured frames but have no valid FTPdetectinfo yet: after a
    mid-night RMS restart the first session is detected straight away while
    the second is still in detection at dawn, and reconciling locks against
    the first alone strips the second session's real detections as false
    positives. A session with no FF files has nothing to detect and never gets
    an FTPdetectinfo, so it does not hold the night up.

    ``postprocessing`` names the detected sessions RMS is still working on
    (_still_postprocessing): the filtered FTPdetectinfo appears at the START of
    RMS's post-processing, which then runs recalibration, flux, the timelapse
    and archiving for 10 min to 3 h. Dawn started on the FTPdetectinfo alone ran
    beside it: gmnro09 (Sibiciu) passed the gate at 04:00 on 2026-10-25 and
    2026-10-26 while RMS archived until 06:44-07:10, and on gmnro22 (Ferigile)
    dawn's timelapse took 324 s instead of 115 s under the overlap.
    """

    listed: bool = False
    sessions: int = 0
    unprocessed: List[str] = field(default_factory=list)
    postprocessing: List[str] = field(default_factory=list)
    ftpdetectinfo: List[str] = field(default_factory=list)

    @property
    def detected(self) -> bool:
        """Every session has its detections (the EON pass can run)."""
        return self.listed and self.sessions > 0 and not self.unprocessed

    @property
    def done(self) -> bool:
        """RMS has nothing of this night left to do."""
        return self.detected and not self.postprocessing


def _valid_ftpdetectinfo(session_dir: Path) -> List[str]:
    return sorted(
        f.name for f in session_dir.glob('FTPdetectinfo_*.txt')
        if '_unfiltered' not in f.name and '_backup' not in f.name
    )


def _has_frames(session_dir: Path) -> bool:
    return next(session_dir.glob('FF_*'), None) is not None


# RMS's per-session processing record (RMS.Reprocess.updateProcessingStatus,
# config.processing_status_file): JSON whose ``archiving`` is 'started' when
# processNight begins and 'completed' once archiveDetections returns.
RMS_STATUS_FILE = '.rms_processing_status'


def _archiving_status(session_dir: Path) -> str | None:
    """``archiving`` from the session's RMS status record, None without one.

    Like RMS.Reprocess.readProcessingStatus, a missing or corrupt record means
    nothing is known (an RMS from before the record existed).
    """
    try:
        status = json.loads((session_dir / RMS_STATUS_FILE).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(status, dict):
        return None
    got = status.get('archiving')
    return got if isinstance(got, str) else None


def _has_final_summary(session_dir: Path, session: str) -> bool:
    """RMS's ``<session>_observation_summary.json``, written just before archiving
    (the ``_working`` copy RMS keeps while processing does not count)."""
    return (session_dir / f'{session}_observation_summary.json').is_file()


@dataclass
class RmsRun:
    """What the running RMS process of one camera tells the gate (_rms_runs)."""

    # Logged "Waiting ... to start recording": RMS finished its last night
    # (processing and upload) and sleeps until the next one.
    idle: bool = False
    # camera_fps_guard.rms_capture_state of its log: 'capturing', 'ended'
    # (detecting and post-processing the night), 'waiting', or None.
    capture_state: str | None = None
    # Its checkout writes <session>_observation_summary.json before archiving.
    writes_summary: bool = False


def _rms_runs(proc: Path = Path('/proc')) -> Dict[str, RmsRun]:
    """The running RMS.StartCapture of each camera code. Never raises."""
    runs: Dict[str, RmsRun] = {}
    try:
        procs = camera_fps_guard.rms_process_pids(proc)
    except OSError as exc:
        logger.warning('Cannot list RMS processes: %s', exc)
        return runs
    for _pid, p in procs:
        cam = camera_fps_guard.parse_rms_config(p.config)
        if cam is None:
            continue
        state = None
        try:
            log = camera_fps_guard.latest_log(cam)
            if log is not None:
                state = camera_fps_guard.rms_capture_state(camera_fps_guard._tail_lines(log))
        except OSError:
            pass
        writes_summary = (p.cwd is not None and
                          (p.cwd / 'RMS' / 'Formats' / 'ObservationSummary.py').is_file())
        runs[cam.code] = RmsRun(idle=state == 'waiting', capture_state=state,
                                writes_summary=writes_summary)
    return runs


def _still_postprocessing(archiving: str | None, has_summary: bool, run: RmsRun | None) -> bool:
    """Whether RMS is still working on a session that has its FTPdetectinfo.

    The status record decides when RMS writes one (only 'completed' is done).
    Without one (an RMS from before it existed) the final observation summary
    marks the end of post-processing, if that RMS writes one. Neither is
    waited for when no RMS process runs for the camera, or when RMS is idle
    until its next night: a crashed or restarted RMS that will never finish
    the record must not hold dawn until dawn_rms_wait_until_hour.
    """
    if run is None or run.idle:
        return False
    if archiving is not None:
        return archiving != 'completed'
    return run.writes_summary and not has_summary


def _rms_night(station_id: str, date_str: str, rms_data_path: str,
               run: RmsRun | None = None) -> RmsNight:
    """Scan both RMS trees for this camera-night's sessions. Never raises.

    *run* is the camera's running RMS (_rms_runs); None means none runs, and
    then an FTPdetectinfo alone makes a session done.
    """
    night = RmsNight()
    if not rms_data_path:
        return night
    rms_data = Path(rms_data_path)
    dirs: Dict[str, List[Path]] = {}
    for subdir_name in ('ArchivedFiles', 'CapturedFiles'):
        base = rms_data / subdir_name
        try:
            entries = sorted(base.iterdir())
        except FileNotFoundError:
            continue
        except OSError as exc:
            logger.warning('[%s] Cannot list %s: %s', station_id, base, exc)
            continue
        night.listed = True
        for arc_dir in entries:
            m = rovimen_night.SESSION_DIR_RE.match(arc_dir.name)
            if not m or m.group(1) != station_id:
                continue
            if rovimen_night.night_of(m.group(2), m.group(3)) != date_str:
                continue
            dirs.setdefault(arc_dir.name, []).append(arc_dir)
    names: set = set()
    for session, copies in sorted(dirs.items()):
        found: set = set()
        frames = False
        archiving: str | None = None
        summary = False
        for d in copies:
            try:
                if not d.is_dir():
                    continue
                found.update(_valid_ftpdetectinfo(d))
                frames = frames or (not found and _has_frames(d))
                if run is not None:
                    status = _archiving_status(d)
                    if status is not None and archiving != 'completed':
                        archiving = status
                    summary = summary or _has_final_summary(d, session)
            except OSError as exc:
                logger.warning('[%s] Cannot read %s: %s', station_id, d, exc)
                frames = True   # unreadable: assume it holds work still to do
        night.sessions += 1
        if found:
            names.update(found)
            if _still_postprocessing(archiving, summary, run):
                night.postprocessing.append(session)
        elif frames:
            night.unprocessed.append(session)
        # else: nothing captured, so nothing to detect and no FTPdetectinfo coming
    night.ftpdetectinfo = sorted(names)
    return night


def _rms_done(station_id: str, date_str: str, rms_data_path: str,
              run: RmsRun | None = None) -> bool:
    """True once RMS has finished every session of this night (see _rms_night)."""
    return _rms_night(station_id, date_str, rms_data_path, run).done


# A colour chunk modified this recently is still being recorded (segments are
# 20 s; this leaves room for a slow disk).
_COLOUR_ACTIVE_S = 90


def _colour_recording(station_id: str, scfg: dict, date_str: str, cfg: dict,
                      now_ts: float) -> bool:
    """True while colour capture still writes this camera-night's chunks.

    The gate waits for it rather than starting beside it: colour_capture used
    to SIGTERM dawn on every chunk that closed, and the idle tail it records
    after RMS ends its night killed the 04:00 and 04:15 runs within ~15 s on
    17 stations every morning (color_capture._stop_stale_dawn). Never raises.
    """
    if not scfg.get('color_capture', True):
        return False
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    try:
        chunks = list(night_dir.glob('*_color.mkv'))
    except OSError:
        return False
    for chunk in chunks:
        try:
            if now_ts - chunk.stat().st_mtime < _COLOUR_ACTIVE_S:
                return True
        except OSError:
            continue
    return False


def _rms_wait_expired(cfg: dict) -> bool:
    """True once the RMS gate should stop waiting (``dawn_rms_wait_until_hour``)."""
    try:
        until = int(cfg.get('dawn_rms_wait_until_hour', _RMS_WAIT_UNTIL_HOUR))
    except (TypeError, ValueError):
        until = _RMS_WAIT_UNTIL_HOUR
    return datetime.now(timezone.utc).hour >= until


# Where the EON pass records the FTPdetectinfo files it reconciled against, so
# a file that appears later (a split night's second session) triggers a rerun.
_EON_SOURCES_KEY = 'eon_ftpdetectinfo'


def _eon_sources(station_id: str, date_str: str, cfg: dict) -> List[str] | None:
    """FTPdetectinfo names the night's EON pass used, or None if unrecorded."""
    got = flags_manager.peek(station_id, date_str, cfg).get(_EON_SOURCES_KEY)
    return list(got) if isinstance(got, list) else None


def _record_eon_sources(station_id: str, date_str: str, names: List[str], cfg: dict) -> None:
    def _edit(state: dict) -> None:
        state[_EON_SOURCES_KEY] = sorted(names)

    flags_manager.edit_state(station_id, date_str, cfg, _edit)


def _rms_wait_reason(station_id: str, scfg: dict, date_str: str,
                     runs: Dict[str, RmsRun] | None = None) -> str | None:
    """Why the morning gate must keep waiting on this camera's RMS, or None.

    ``'detection'`` while a session has no detections yet (the EON pass cannot
    run without them), ``'post-processing'`` while RMS is still working on a
    detected night. *runs* is _rms_runs(); a camera missing from it has no
    RMS process, so its FTPdetectinfo is enough.

    A camera with ``rms: false`` (colour/spectro-only, e.g. the GH662 grating
    pair on gmnro01) never produces an FTPdetectinfo, so waiting on it
    deadlocks the gate until the bulk deadline kills the whole night's
    processing for every camera on the station.
    """
    if not scfg.get('rms', True):
        return None
    night = _rms_night(station_id, date_str, scfg.get('rms_data_path', ''),
                       (runs or {}).get(station_id))
    if night.done:
        return None
    return 'post-processing' if night.detected else 'detection'


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _svc_enabled(cfg: dict, service: str) -> bool:
    """Return True if the service is enabled in config."""
    services = cfg.get('services', {})
    svc = services.get(service, {})
    if isinstance(svc, dict):
        return bool(svc.get('enabled', False))
    return service in cfg.get('active_services', [])


def _stacker_enabled(cfg: dict) -> bool:
    if _svc_enabled(cfg, 'stacker'):
        return True
    return int(cfg.get('stacker_mode', 0)) != 0


def _uploader_enabled(cfg: dict) -> bool:
    if _svc_enabled(cfg, 'archive_upload'):
        return True
    return bool(cfg.get('archive', {}).get('enabled', False))


def _full_night_setting(cfg: dict) -> str:
    """services.reencode.full_night normalised to 'true', 'false' or 'auto'."""
    svc = cfg.get('services', {}).get('reencode', {})
    setting = svc.get('full_night', 'auto') if isinstance(svc, dict) else 'auto'
    if isinstance(setting, bool):
        return 'true' if setting else 'false'
    if isinstance(setting, str):
        token = setting.strip().lower()
        if token in ('true', 'on', 'yes', '1'):
            return 'true'
        if token in ('false', 'off', 'no', '0'):
            return 'false'
    return 'auto'


def _full_night_reencode_enabled(cfg: dict) -> bool:
    """Whether Phase 7 re-encodes ALL chunks, or only the detection clips.

    Detection clips are always re-encoded in Phase 1 (process_night_locked_only);
    that is what the dashboard's detection grid and the archive serve. The full
    night pass additionally re-encodes non-detection chunks so the VDB can play
    arbitrary times — a surface rarely used. Timelapses and night-stacks do NOT
    depend on it (they read *_stack.webp and the raw _color.mkv).

    On boxes with no hardware encoder (Pi-class, libx264 fallback) that bulk pass
    is thousands of software encodes per night: it cannot finish inside the dawn
    window, overruns into the next capture, starves RMS and thermally throttles
    the SoC. So the default 'auto' turns it OFF wherever VAAPI is unavailable and
    ON where hardware encode makes it cheap. Whether the pass is worth running
    is decided afresh every morning by ``FullNightBudget``: long winter nights
    stop early, short summer nights finish. Explicit true/false overrides it.
    """
    setting = _full_night_setting(cfg)
    if setting != 'auto':
        return setting == 'true'
    return rovimen_media.find_vaapi_device(cfg) is not None


# The full-night pass measures itself on this many real encodes before judging
# whether it can finish. With the fleet's ~15 s per 20 s chunk and 2-4 workers
# that is the first one to three minutes of the pass. The first chunks of a
# camera also pay one-off colour/overlay setup, so a smaller sample
# over-projects; the verdict is re-taken after every chunk from then on.
_BUDGET_WARMUP_MIN_CHUNKS = 12
_BUDGET_WARMUP_CHUNKS_PER_WORKER = 3

# A "cannot finish" verdict waits until the pass has run this long, and is
# projected from the last ``warmup`` chunks rather than the mean since the
# start. Dawn starts while RMS is still running its own post-capture detection,
# flux and timelapse (about 30 min, all cameras at once), and a sample taken
# under that burst over-projects 2-3x: gmnro22 (Ferigile) measured 26.8 s per
# chunk at 04:27 on 2026-10-26 with RMS detecting until 04:31, against 11 s on
# 2026-10-25 when it started at 04:51 after RMS and the whole pass finished in
# 2 h 10 min. Stopped at chunk 12, three passes there that would have fitted
# never ran. The size verdict ("enlarges") is not delayed: contention does not
# change output size.
_BUDGET_JUDGE_AFTER_S = 900.0

# The updater's VAAPI ground-rule step greps for this line before granting
# render-group access, since GPU access turns the full-night pass on under "auto".
FULL_NIGHT_DEADLINE_POLICY = 1


class FullNightBudget:
    """Decides, from its own first encodes, whether the full-night pass is worth it.

    Measured 2026-09-24: gmnro02 spent 18 h of ffmpeg wall time on it, gmnro04
    was still on the previous evening's chunks when the deadline cut it off,
    and on gmnro11/03/06 the output was BIGGER than the capture. Both are
    wasted compute, so once ``warmup`` chunks have been encoded (and after
    every chunk from then on) the budget stops the pass when

    * ``enlarges``: the chunks encoded so far total at least their sources, or
    * ``projected_miss``: once the pass has run ``judge_after_s``, remaining
      chunks (all cameras) x mean wall of the last ``warmup`` chunks / workers
      is more than the time left before the bulk deadline.

    The deadline itself stopping the pass is recorded too (``deadline_hit``).
    ``enforce=False`` (an explicit ``full_night: true``) only logs the verdict.
    One instance spans every camera of the morning: the pass runs camera by
    camera, but the deadline is shared.
    """

    def __init__(self, pending_chunks: int, parallelism: int,
                 seconds_left: Callable[[], float] | None,
                 enforce: bool = True, warmup_chunks: int | None = None,
                 judge_after_s: float | None = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.pending = max(0, pending_chunks)
        self.parallelism = max(1, parallelism)
        self.seconds_left = seconds_left
        self.enforce = enforce
        self.warmup = warmup_chunks or max(
            _BUDGET_WARMUP_MIN_CHUNKS, _BUDGET_WARMUP_CHUNKS_PER_WORKER * self.parallelism)
        self.judge_after_s = _BUDGET_JUDGE_AFTER_S if judge_after_s is None else judge_after_s
        self.clock = clock
        self.started = clock()
        self.encoded = 0
        self.wall_s = 0.0
        self.recent_walls: deque[float] = deque(maxlen=self.warmup)
        self.src_bytes = 0
        self.dst_bytes = 0
        self.kept_source = 0
        self.decision: dict | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    def stopped(self) -> bool:
        return self._stop.is_set()

    def record(self, outcome: encoder.ChunkOutcome) -> None:
        """process_chunk's ``on_encoded`` hook; called from the worker threads."""
        with self._lock:
            self.encoded += 1
            self.pending = max(0, self.pending - 1)
            self.wall_s += outcome.wall_s
            self.recent_walls.append(outcome.wall_s)
            self.src_bytes += outcome.src_bytes
            self.dst_bytes += outcome.dst_bytes
            self.kept_source += int(outcome.kept_source)
            if self.decision is None and self.encoded >= self.warmup:
                self._evaluate()

    def _numbers(self) -> dict:
        mean_wall = self.wall_s / self.encoded if self.encoded else 0.0
        recent_wall = (sum(self.recent_walls) / len(self.recent_walls)
                       if self.recent_walls else 0.0)
        return {
            'encoded_chunks': self.encoded,
            'pending_chunks': self.pending,
            'workers': self.parallelism,
            'elapsed_s': round(self.clock() - self.started),
            'mean_wall_s': round(mean_wall, 2),
            'recent_wall_s': round(recent_wall, 2),
            'projected_s': round(self.pending * recent_wall / self.parallelism),
            'seconds_left': (round(self.seconds_left())
                             if self.seconds_left is not None else None),
            'src_mb': round(self.src_bytes / 1e6, 1),
            'dst_mb': round(self.dst_bytes / 1e6, 1),
            'kept_source_chunks': self.kept_source,
        }

    def _evaluate(self) -> None:
        n = self._numbers()
        if self.src_bytes and self.dst_bytes >= self.src_bytes:
            self._decide('enlarges', n)
        elif (n['seconds_left'] is not None and n['elapsed_s'] >= self.judge_after_s
              and n['projected_s'] > n['seconds_left']):
            self._decide('projected_miss', n)

    def _decide(self, reason: str, numbers: dict) -> None:
        self.decision = {'reason': reason, **numbers}
        if self.enforce:
            self._stop.set()

    def deadline_hit(self) -> None:
        """The deadline stopped the pass with chunks still to do."""
        with self._lock:
            if self.decision is None:
                self._decide('deadline_hit', self._numbers())


def _pending_full_night_chunks(station_id: str, date_str: str, cfg: dict) -> int:
    """Chunks the full-night pass would still encode for one camera-night."""
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.is_dir():
        return 0
    if encoder._preserves_source(cfg.get('stations', {}).get(station_id, {})):
        return 0
    chunks = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    count = 0
    for mkv in night_dir.glob('*_color.mkv'):
        c = chunks.get(mkv.name, {})
        if (c.get('ready') and not c.get('reencoded')
                and not Path(str(mkv) + '.reencoded').exists()):
            count += 1
    return count


def _record_full_night_stop(budget: FullNightBudget, date_str: str) -> None:
    """Log the budget's verdict and record it as this morning's outcome."""
    d = budget.decision
    if d is None:
        return
    summary = ('%(reason)s: %(encoded_chunks)d encoded, %(pending_chunks)d left, '
               'recent %(recent_wall_s).1fs/chunk (mean %(mean_wall_s).1fs) on '
               '%(workers)d workers after %(elapsed_s)ds, projected '
               '%(projected_s)ds vs %(seconds_left)ss to the deadline, '
               '%(src_mb).1f MB in -> %(dst_mb).1f MB out' % d)
    if not budget.enforce:
        logger.warning('Full-night re-encode would be stopped (%s) — carrying on, '
                       'services.reencode.full_night is explicitly true', summary)
        return
    flags_manager.write_full_night_marker({
        **d,
        'night': date_str,
        'decided_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'note': 'this morning only: the pass is measured again every morning',
    })
    logger.warning('Full-night re-encode STOPPED for this night (%s). Detection and '
                   'locked clips are unaffected; the pass is measured again next '
                   'morning', summary)


# ---------------------------------------------------------------------------
# Stale lock sweep
# ---------------------------------------------------------------------------

# The sweep runs every morning, so a night's locks are cleared the first
# morning it is older than stale_lock_days; only this many nights below the
# cutoff are re-read (a station offline longer than this catches up by hand).
# Re-reading every older night parsed a year of state.json files on gmnde00.
_STALE_LOCK_WINDOW_DAYS = 30


def _sweep_stale_locks(cfg: dict) -> None:
    """Clear lock fields in state.json for nights older than stale_lock_days."""
    days = cfg.get('stale_lock_days', 7)
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).strftime('%Y%m%d')
    oldest = (now - timedelta(days=days + _STALE_LOCK_WINDOW_DAYS)).strftime('%Y%m%d')
    capture_root = rovimen_config.capture_root(cfg)
    if not capture_root.exists():
        return
    count = 0
    try:
        station_dirs = list(capture_root.iterdir())
    except OSError as exc:
        logger.warning('Cannot list %s: %s', capture_root, exc)
        return
    for station_dir in station_dirs:
        if not station_dir.is_dir():
            continue
        try:
            date_dirs = list(station_dir.iterdir())
        except OSError:
            continue
        for date_dir in date_dirs:
            if not (oldest <= date_dir.name < cutoff) or not date_dir.is_dir():
                continue
            sid = station_dir.name
            dstr = date_dir.name
            if not any(c.get('lock') is not None
                       for c in flags_manager.peek(sid, dstr, cfg).get('chunks', {}).values()):
                continue
            # Under the flags lock, re-read: an unlocked load-modify-save here
            # could overwrite a concurrent writer (station_api, the uploader).
            cleared: List[str] = []

            def _clear(sstate: dict) -> bool:
                for chunk_name, chunk in sstate.get('chunks', {}).items():
                    if chunk.get('lock') is not None:
                        chunk['lock'] = None
                        cleared.append(chunk_name)
                return bool(cleared)

            flags_manager.edit_state(sid, dstr, cfg, _clear)
            for chunk_name in cleared:
                logger.info('[stale-lock] Cleared lock for %s/%s/%s', sid, dstr, chunk_name)
            count += len(cleared)
    if count:
        logger.info('[stale-lock] Cleared %d lock(s) older than %d days', count, days)


# ---------------------------------------------------------------------------
# Phase runners
# ---------------------------------------------------------------------------

def _reconcile_ready(stations: dict, date_str: str, cfg: dict) -> None:
    """Mark on-disk chunks as ready if their flag is missing in state.json.

    Fixes chunks whose IN_CLOSE_WRITE event was dropped by the inotify watcher
    (typically after a CPU/I/O stall that causes a burst of events, crashing
    the watcher before mark_ready() is called).  Safe to call on completed
    nights — all MKV files in the night directory are fully written by dawn.
    """
    capture_root = rovimen_config.capture_root(cfg)
    for station_id in stations:
        night_dir = capture_root / station_id / date_str
        if not night_dir.exists():
            continue
        state = flags_manager.load(station_id, date_str, cfg)
        chunk_states = state.get('chunks', {})
        to_fix = [
            mkv.name for mkv in sorted(night_dir.glob('*_color.mkv'))
            if rovimen_night.CHUNK_RE.match(mkv.name)
            and not chunk_states.get(mkv.name, {}).get('ready', False)
        ]
        if to_fix:
            flags_manager.mark_ready_batch(station_id, date_str, to_fix, cfg)
            logger.warning(
                '[%s] Reconciled %d not-ready chunk(s) for %s — '
                'inotify events likely dropped during watcher crash: %s',
                station_id, len(to_fix), date_str, ', '.join(to_fix),
            )


# Phase 4 fit check. Stacking a full night is hours of software decode (about
# 2.4 h per 2,380-chunk camera on gmnro20 (Brăila)), so a camera is started
# only when its whole night fits before the deadline. Measured 2026-11-17 on
# gmnro20 after its third and fourth cameras: 4 x 2,379 chunks at 3.6 s per
# chunk need 9.5 h, the 04:45-13:15 window is 8.5 h, so every morning stacked
# three cameras partly, one not at all, and built no timelapse.
#
# The rate is measured on this morning's own chunks: the first camera(s) start
# as a probe, and once ``warmup`` chunks are done every camera still to start is
# judged at the live rate. A probe camera that does not fit is stopped there.
_STACK_WARMUP_MIN_CHUNKS = 12
_STACK_WARMUP_CHUNKS_PER_WORKER = 3
# Projected stacking time is padded by this factor, and this much time is kept
# after the last stack for the restack check, the timelapses and the upload.
_STACK_SAFETY = 1.1
_STACK_RESERVE_S = 900.0
# The night's verdict per camera, in its state.json. Timelapse, restack and the
# Phase 7 final stack pass read it, so a camera that did not fit stays on its
# locked chunks for the whole run.
_STACK_PLAN_KEY = 'stack_plan'


class StackPlan:
    """Admits a camera to the full-night stack only if its night fits.

    ``pending`` is each camera's unstacked chunk count, in the order the cameras
    will be offered. ``admit(sid)`` is asked as each camera is about to start:

    * no deadline (manual ``--date`` run): always admitted;
    * no rate yet (fewer than ``warmup`` chunks stacked this morning): admitted
      as a probe;
    * otherwise admitted if the chunks still owed by the admitted cameras plus
      this camera's, at the live rate x ``_STACK_SAFETY``, plus
      ``_STACK_RESERVE_S``, fit in the time left.

    When the ``warmup``-th chunk is recorded the probe cameras are judged the
    same way, in admission order; ``revoked(sid)`` turns True for a probe that
    does not fit, and its stacker stops at the next chunk. A measured admission
    is never revoked: from there only the deadline stops it.
    """

    def __init__(self, pending: Dict[str, int], workers: int,
                 seconds_left: Callable[[], float] | None,
                 warmup_chunks: int | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        clock = clock or time.monotonic
        self.pending = dict(pending)
        self.seconds_left = seconds_left
        self.warmup = warmup_chunks or max(
            _STACK_WARMUP_MIN_CHUNKS, _STACK_WARMUP_CHUNKS_PER_WORKER * max(1, workers))
        self.clock = clock
        self.started = clock()
        self.done = 0
        self.done_by: Dict[str, int] = {}
        self.stamps: deque[float] = deque(maxlen=2 * self.warmup)
        self.admitted: Dict[str, str] = {}      # sid -> 'measured' | 'probe' | 'no deadline'
        self.finished: set = set()
        self.verdicts: Dict[str, dict] = {}
        self._lock = threading.Lock()

    def rate(self) -> float | None:
        """Chunks per second over the last ``2 x warmup`` stacked chunks."""
        if self.done < self.warmup or len(self.stamps) < 2:
            return None
        span = self.stamps[-1] - self.stamps[0]
        if span <= 0:
            return None
        return (len(self.stamps) - 1) / span

    def _owed(self, sid: str) -> int:
        return max(0, self.pending.get(sid, 0) - self.done_by.get(sid, 0))

    def _fits(self, sid: str, committed: int, rate: float) -> dict:
        """The numbers behind admitting ``sid`` on top of ``committed`` chunks."""
        owed = self._owed(sid)
        projected = (committed + owed) / rate * _STACK_SAFETY + _STACK_RESERVE_S
        left = self.seconds_left() if self.seconds_left is not None else 0.0
        return {
            'fits': projected <= left,
            'pending_chunks': owed,
            'committed_chunks': committed,
            'rate_chunks_per_s': round(rate, 3),
            'projected_s': round(projected),
            'seconds_left': round(left),
        }

    def _committed(self, exclude: str | None = None) -> int:
        return sum(self._owed(sid) for sid, how in self.admitted.items()
                   if sid != exclude and sid not in self.finished and how != 'revoked')

    def admit(self, sid: str) -> bool:
        with self._lock:
            if self.seconds_left is None:
                self.admitted[sid] = 'no deadline'
                self.verdicts[sid] = {'fits': True, 'reason': 'no deadline'}
                return True
            rate = self.rate()
            if rate is None:
                self.admitted[sid] = 'probe'
                logger.info('[%s] Stack plan: no rate yet — starting it as the probe, judged '
                            'after %d chunk(s)', sid, self.warmup)
                return True
            v = self._fits(sid, self._committed(), rate)
            self._judge(sid, v, 'measured')
            return v['fits']

    def _judge(self, sid: str, v: dict, how: str) -> None:
        v['reason'] = 'fits' if v['fits'] else 'projected_miss'
        self.verdicts[sid] = v
        self.admitted[sid] = how if v['fits'] else 'revoked'
        line = ('%(pending_chunks)d chunk(s) on top of %(committed_chunks)d at '
                '%(rate_chunks_per_s).3f chunk/s: projected %(projected_s)ds vs '
                '%(seconds_left)ds left' % v)
        if v['fits']:
            logger.info('[%s] Stack plan: full night fits (%s)', sid, line)
        else:
            logger.warning('[%s] Stack plan: full night does NOT fit (%s) — locked chunks '
                           'only, no timelapse for this night', sid, line)

    def record(self, sid: str) -> None:
        """One chunk of ``sid`` was stacked; called from the stacker's workers."""
        with self._lock:
            self.done += 1
            self.done_by[sid] = self.done_by.get(sid, 0) + 1
            self.stamps.append(self.clock())
            if self.done != self.warmup:
                return
            rate = self.rate()
            if rate is None:
                return
            committed = 0
            for probe in [s for s, how in self.admitted.items() if how == 'probe']:
                v = self._fits(probe, committed, rate)
                self._judge(probe, v, 'measured')
                if v['fits']:
                    committed += v['pending_chunks']

    def finish(self, sid: str) -> None:
        with self._lock:
            self.finished.add(sid)

    def revoked(self, sid: str) -> bool:
        return self.admitted.get(sid) == 'revoked'

    def skip(self, sid: str, reason: str) -> None:
        """Record a camera that was never offered a start (deadline, failure)."""
        with self._lock:
            self.verdicts.setdefault(sid, {'fits': False, 'reason': reason,
                                           'pending_chunks': self._owed(sid)})


def _record_stack_plan(sid: str, date_str: str, cfg: dict, verdict: dict) -> None:
    stamped = {**verdict,
               'decided_at': datetime.now(timezone.utc).isoformat(timespec='seconds')}

    def _edit(state: dict) -> None:
        state[_STACK_PLAN_KEY] = stamped

    try:
        flags_manager.edit_state(sid, date_str, cfg, _edit)
    except Exception:
        logger.exception('[%s] Could not record the stack plan for %s', sid, date_str)


def _stack_fits(station_id: str, date_str: str, cfg: dict) -> bool:
    """False once this morning's StackPlan left the camera on its locked chunks."""
    plan = flags_manager.peek(station_id, date_str, cfg).get(_STACK_PLAN_KEY)
    return not (isinstance(plan, dict) and plan.get('fits') is False)


def _stack_order(station_ids: List[str], date_str: str) -> List[str]:
    """Rotate the camera order by night, so a camera that does not fit is a
    different one each morning rather than always the last in config.json."""
    if not station_ids:
        return []
    try:
        k = datetime.strptime(date_str, '%Y%m%d').toordinal() % len(station_ids)
    except ValueError:
        k = 0
    return station_ids[k:] + station_ids[:k]


def _run_stack_all(stations: dict, date_str: str, cfg: dict,
                   should_abort: Callable[[], bool] | None = None) -> None:
    """Phase 4: stack the cameras whose whole night fits, with controlled parallelism.

    ``dawn_stack_parallelism`` cameras (default cpu_cores // 3, at least 1) are
    stacked at once, from one pool: the next camera starts as soon as any
    worker frees up. (Fixed batches waited for the slowest camera of each
    batch, so one camera with a big backlog idled the other workers.) On a
    6-core i5-8500 with stack_workers=2 each camera uses ~3 cores on average;
    two cameras in parallel fill all 6 cores. Set ``dawn_stack_parallelism: 1``
    to go sequential, or raise it for NVMe-backed stations with more cores.

    Each camera must pass ``StackPlan.admit`` before it starts. A camera that
    does not fit is left on its locked chunks (stacked in Phase 1), and the
    verdict goes into its night state for the later phases.
    """
    cpu_cores = cfg.get('_cpu_cores', 4)
    parallelism = max(1, cfg.get('dawn_stack_parallelism', cpu_cores // 3))
    station_ids = _stack_order(list(stations), date_str)
    if not station_ids:
        return
    if should_abort is not None and should_abort():
        logger.warning('Deadline reached — not starting the remaining %d camera(s)',
                       len(station_ids))
        return

    pending = {sid: len(stacker.pending_chunks(sid, date_str, cfg)) for sid in station_ids}
    seconds_left = (should_abort.seconds_left
                    if isinstance(should_abort, BulkDeadline) and should_abort.enabled
                    else None)
    workers = max(1, cfg.get('stack_workers', 2)) * min(parallelism, len(station_ids))
    plan = StackPlan(pending, workers, seconds_left)
    logger.info('Stack plan: %d chunk(s) to stack across %d camera(s) (%s)%s',
                sum(pending.values()), len(station_ids),
                ', '.join('%s %d' % kv for kv in pending.items()),
                '' if seconds_left is None else
                ', %.0f min to the deadline' % (seconds_left() / 60))

    def _stack_one(sid: str) -> bool:
        """Stack one camera; False if the deadline stopped it before it started."""
        if should_abort is not None and should_abort():
            plan.skip(sid, 'deadline')
            return False
        if pending[sid] == 0:
            # Nothing to judge; the pass still picks up chunks closed since.
            plan.verdicts[sid] = {'fits': True, 'reason': 'nothing to stack'}
            with _timed_camera('stack', sid):
                stacker.process_night(sid, date_str, cfg, should_abort)
            return True
        if not plan.admit(sid):
            return True

        def camera_abort() -> bool:
            return plan.revoked(sid) or (should_abort is not None and should_abort())

        try:
            with _timed_camera('stack', sid):
                stacker.process_night(sid, date_str, cfg, camera_abort,
                                      on_stacked=lambda: plan.record(sid))
        finally:
            plan.finish(sid)
        return True

    not_started = 0
    with ThreadPoolExecutor(max_workers=min(parallelism, len(station_ids))) as pool:
        futs = {pool.submit(_stack_one, sid): sid for sid in station_ids}
        for fut in as_completed(futs):
            try:
                if not fut.result():
                    not_started += 1
            except Exception:
                logger.exception('[%s] Stacking failed', futs[fut])
    if not_started:
        logger.warning('Deadline reached — %d camera(s) not started', not_started)
    skipped = [sid for sid, v in plan.verdicts.items() if v.get('fits') is False]
    for sid in station_ids:
        if sid in plan.verdicts:
            _record_stack_plan(sid, date_str, cfg, plan.verdicts[sid])
    if skipped:
        logger.warning('Stack plan: %d of %d camera(s) left on their locked chunks for %s: %s',
                       len(skipped), len(station_ids), date_str, ', '.join(skipped))


_STACK_COVERAGE_THRESHOLD = 0.85  # defer timelapse if fewer than 85% of stacks are present


def _stack_coverage(station_id: str, date_str: str, cfg: dict) -> tuple[int, int]:
    """Return (ready_count, missing_stack_count) for a station/date.

    A chunk counts as missing if it is ready in state.json but has no stack
    WebP in <night_dir>/stacks/.
    """
    capture_root = rovimen_config.capture_root(cfg)
    night_dir = capture_root / station_id / date_str
    stacks_dir = night_dir / 'stacks'
    sstate = flags_manager.load(station_id, date_str, cfg)
    chunk_states = sstate.get('chunks', {})
    ready = [n for n, v in chunk_states.items() if v.get('ready')]
    missing = [
        n for n in ready
        if not chunk_states[n].get('stacked')
        and not (stacks_dir / n.replace('_color.mkv', '_stack.webp')).exists()
    ]
    return len(ready), len(missing)


def _run_timelapse_all(stations: dict, date_str: str, cfg: dict,
                       should_abort: Callable[[], bool] | None = None) -> None:
    """Phase 6: Build timelapse for cameras with adequate stack coverage.

    Cameras missing more than 15% of stacks are skipped and flagged as
    timelapse_deferred so the retry sweep can pick them up on subsequent days.
    """
    for station_id in stations:
        if should_abort is not None and should_abort():
            logger.warning('Deadline reached — deferring the remaining timelapse(s)')
            for sid in stations:
                if not flags_manager.is_timelapse_done(sid, date_str, cfg):
                    flags_manager.mark_timelapse_deferred(sid, date_str, cfg,
                                                          reason='deadline reached')
            return
        if flags_manager.is_timelapse_done(station_id, date_str, cfg):
            logger.info('[%s] Timelapse already done for %s — skipping', station_id, date_str)
            continue
        if not _stack_fits(station_id, date_str, cfg):
            logger.info('[%s] Full-night stack did not fit — no timelapse for %s',
                        station_id, date_str)
            continue
        ready, missing = _stack_coverage(station_id, date_str, cfg)
        if ready > 0:
            coverage = (ready - missing) / ready
            if coverage < _STACK_COVERAGE_THRESHOLD:
                logger.warning(
                    '[%s] Stack coverage %.1f%% (%d/%d missing) — deferring timelapse for %s',
                    station_id, coverage * 100, missing, ready, date_str,
                )
                flags_manager.mark_timelapse_deferred(
                    station_id, date_str, cfg,
                    reason=f'stack coverage {coverage * 100:.1f}% below '
                           f'{_STACK_COVERAGE_THRESHOLD * 100:.0f}%')
                continue
        try:
            with _timed_camera('timelapse', station_id):
                timelapse_build.build(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] Timelapse failed', station_id)


# A deferred timelapse whose build keeps failing is retried at most this many
# times per night. Every attempt rebuilds the night stack as well, and the
# retry sweep runs on every dawn tick once the night is done.
_TIMELAPSE_RETRY_MAX = 3
_TIMELAPSE_ATTEMPTS_KEY = 'timelapse_retry_attempts'


def _timelapse_attempts(station_id: str, date_str: str, cfg: dict) -> int:
    try:
        return int(flags_manager.peek(station_id, date_str, cfg).get(_TIMELAPSE_ATTEMPTS_KEY, 0))
    except (TypeError, ValueError):
        return 0


def _record_timelapse_attempt(station_id: str, date_str: str, cfg: dict) -> None:
    def _edit(state: dict) -> None:
        try:
            state[_TIMELAPSE_ATTEMPTS_KEY] = int(state.get(_TIMELAPSE_ATTEMPTS_KEY, 0)) + 1
        except (TypeError, ValueError):
            state[_TIMELAPSE_ATTEMPTS_KEY] = 1

    flags_manager.edit_state(station_id, date_str, cfg, _edit)


def _retry_deferred_timelapses(stations: dict, cfg: dict) -> None:
    """Sweep the last 3 nights for deferred timelapses and retry if coverage improved.

    After 2 days we build with whatever stacks exist rather than waiting further.
    Runs after tonight's timelapse, and on every tick once the night is done, so
    retries happen on nights already marked morning_done; a build that keeps
    failing is given up after ``_TIMELAPSE_RETRY_MAX`` attempts.
    """
    today = datetime.now(timezone.utc).date()
    for days_ago in range(1, 4):
        date_str = (today - timedelta(days=days_ago)).strftime('%Y%m%d')
        for station_id in stations:
            if flags_manager.is_timelapse_done(station_id, date_str, cfg):
                continue
            if _timelapse_attempts(station_id, date_str, cfg) >= _TIMELAPSE_RETRY_MAX:
                logger.debug('[%s] Deferred timelapse for %s: gave up after %d attempts',
                             station_id, date_str, _TIMELAPSE_RETRY_MAX)
                continue
            # A night finished (morning_done) without a timelapse is also owed
            # one: before 2026-09-23 the timelapse ran after the re-encode, and a
            # reboot or deadline between the two left no deferred flag behind.
            if not (flags_manager.is_timelapse_deferred(station_id, date_str, cfg)
                    or flags_manager.is_morning_done(station_id, date_str, cfg)):
                continue
            # Only its locked chunks were stacked: a "timelapse" of those is a
            # handful of frames, so the give-up rule below must not build one.
            if not _stack_fits(station_id, date_str, cfg):
                continue
            ready, missing = _stack_coverage(station_id, date_str, cfg)
            coverage = (ready - missing) / ready if ready > 0 else 1.0
            give_up = days_ago >= 2
            if coverage >= _STACK_COVERAGE_THRESHOLD or give_up:
                reason = 'coverage recovered' if coverage >= _STACK_COVERAGE_THRESHOLD else 'deadline (2d)'
                logger.info(
                    '[%s] Retrying deferred timelapse for %s (%s, coverage=%.1f%%)',
                    station_id, date_str, reason, coverage * 100,
                )
                try:
                    timelapse_build.build(station_id, date_str, cfg)
                except Exception:
                    logger.exception('[%s] Deferred timelapse retry failed for %s',
                                     station_id, date_str)
                if not flags_manager.is_timelapse_done(station_id, date_str, cfg):
                    _record_timelapse_attempt(station_id, date_str, cfg)
            else:
                logger.info(
                    '[%s] Deferred timelapse for %s still waiting (coverage=%.1f%%)',
                    station_id, date_str, coverage * 100,
                )


def _verify_and_restack(stations: dict, date_str: str, cfg: dict,
                        should_abort: Callable[[], bool] | None = None) -> None:
    """Phase 4, second pass: verify stacking completeness and rerun for gaps.

    After the initial stack pass, checks each camera for missing stacks
    and reruns the stacker for cameras with gaps.
    """
    for station_id in stations:
        if should_abort is not None and should_abort():
            logger.warning('Deadline reached — skipping the restack verification pass')
            return
        if not _stack_fits(station_id, date_str, cfg):
            continue
        ready, missing = _stack_coverage(station_id, date_str, cfg)
        if missing > 0:
            logger.info('[%s] %d/%d stacks missing after first pass — restacking',
                        station_id, missing, ready)
            try:
                stacker.process_night(station_id, date_str, cfg, should_abort)
            except Exception:
                logger.exception('[%s] Restack pass failed', station_id)
            ready2, missing2 = _stack_coverage(station_id, date_str, cfg)
            if missing2 > 0:
                logger.warning('[%s] Still %d/%d stacks missing after restack',
                               station_id, missing2, ready2)
        else:
            logger.info('[%s] All %d stacks present', station_id, ready)


def _run_eon_pass(station_id: str, date_str: str, cfg: dict) -> bool:
    """Reconcile the night's locks against FTPdetectinfo; True when it ran.

    Records which FTPdetectinfo files it used, so the backfill can tell when a
    later one (a split night's second session) calls for a rerun.
    """
    scfg = cfg.get('stations', {}).get(station_id, {})
    sources = _rms_night(station_id, date_str, scfg.get('rms_data_path', '')).ftpdetectinfo
    if not detection_lock.process_night(station_id, date_str, cfg):
        return False
    flags_manager.mark_eon_done(station_id, date_str, cfg)
    _record_eon_sources(station_id, date_str, sources, cfg)
    return True


def _locked_chunks(station_id: str, date_str: str, cfg: dict) -> List[Path]:
    """The night's locked (detection) chunks, stitched clips included."""
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.is_dir():
        return []
    chunks = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    return [
        mkv for mkv in sorted(night_dir.glob('*_color.mkv'))
        if rovimen_lock.chunk_is_locked(mkv, chunks.get(mkv.name, {}))
    ]


_meteor_clips_encoded = rovimen_config.meteor_clips_encoded


def _locked_chunk_jobs(stations: dict, date_str: str,
                       cfg: dict) -> List[Tuple[str, Path, bool]]:
    """(camera, locked chunk, stack it first?) for every camera, interleaved.

    A chunk is stacked before it is encoded when it is still raw and unstacked:
    Phase 1 encodes locked chunks in place, and a stack made afterwards comes
    from the encoded clip (the clip's colour curve, fitted to single frames,
    rides into a maxpixel sky that then sits above target; the height probe is
    all that stands between it and a second colour/rotation pass). Stacking
    first costs ~2 s per chunk. Stitched boundary clips have no per-chunk stack.
    Interleaving the cameras keeps one camera's peak night from starving the
    others' clips.
    """
    per_camera: List[List[Tuple[str, Path, bool]]] = []
    for station_id in stations:
        chunks = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
        jobs = []
        for mkv in _locked_chunks(station_id, date_str, cfg):
            c = chunks.get(mkv.name, {})
            raw = (bool(rovimen_night.CHUNK_RE.match(mkv.name)) and bool(c.get('ready'))
                   and not c.get('stacked') and not c.get('reencoded'))
            jobs.append((station_id, mkv, raw))
        per_camera.append(jobs)
    return [job for group in zip_longest(*per_camera) for job in group if job is not None]


def _stack_then_encode(station_id: str, mkv: Path, stack_first: bool, encode: bool,
                       cfg: dict, encode_cfg: dict) -> bool:
    if stack_first:
        try:
            stacker.process_chunk(mkv, cfg)
        except Exception:
            logger.exception('[%s] Pre-encode stack failed for %s', station_id, mkv.name)
    if not encode:
        return False
    return bool(encoder.process_chunk(mkv, encode_cfg))


def _run_encode_locked_all(stations: dict, date_str: str, cfg: dict,
                           skip_eon: frozenset = frozenset()) -> None:
    """Phase 1: EON detection pass, then stack and encode the locked clips.

    Runs detection_lock per camera to reconcile FTPdetectinfo locks, then
    stacks (while still raw) and encodes the locked (detection) clips with the
    full pipeline (rotate + colour calibration + overlay). process_chunk() is
    idempotent — clips encoded here are skipped by encoder.process_night() in
    Phase 7.

    ``skip_eon`` names cameras whose RMS has not finished every session (the
    gate gave up waiting): reconciling them now would strip the unprocessed
    sessions' real-time locks as false positives, so they keep their real-time
    locks, get those clips encoded, and are reconciled by the detection
    backfill once RMS completes.

    The EON passes run in parallel per camera (each writes its own state.json);
    the clips of all cameras then share one pool of ``dawn_encode_parallelism``
    workers (see encoder._batch_cfg). Each camera used to get one encode worker
    of its own, so a peak night on one camera (197 locked clips on gmnro01
    (Ghirdoveni), 09-22) ran serially while the other workers sat idle.
    """
    for station_id in stations:
        if station_id not in skip_eon:
            flags_manager.mark_rms_complete(station_id, date_str, cfg)

    def _eon(station_id: str) -> None:
        with _timed_camera('eon', station_id):
            if station_id in skip_eon:
                logger.warning('[%s] RMS not finished for %s — EON pass deferred to the '
                               'detection backfill; real-time locks kept', station_id, date_str)
                return
            try:
                _run_eon_pass(station_id, date_str, cfg)
            except Exception:
                logger.exception('[%s] EON detection pass failed', station_id)

    if len(stations) <= 1:
        for sid in stations:
            _eon(sid)
    else:
        with ThreadPoolExecutor(max_workers=len(stations)) as pool:
            for fut in as_completed([pool.submit(_eon, sid) for sid in stations]):
                fut.result()

    jobs = _locked_chunk_jobs(stations, date_str, cfg)
    if not jobs:
        logger.info('No locked chunks for %s', date_str)
        return
    stack = _stacker_enabled(cfg)
    encode = _meteor_clips_encoded(cfg)
    if not encode:
        logger.info('Meteor clips are encoded on demand only '
                    '(services.reencode.meteor_clips=false) — not encoding %d locked chunk(s)',
                    len(jobs))
        if not stack:
            return
    encode_cfg = encoder._batch_cfg(cfg)
    workers = max(1, min(int(encode_cfg['_encode_parallelism']), len(jobs)))
    logger.info('Phase 1: %d locked chunk(s) across %d camera(s) (workers=%d, threads/chunk=%d)',
                len(jobs), len(stations), workers, encode_cfg['_encode_threads'])
    t0 = time.monotonic()
    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_stack_then_encode, sid, mkv, stack and raw, encode,
                            cfg, encode_cfg): (sid, mkv)
                for sid, mkv, raw in jobs}
        for fut in as_completed(futs):
            sid, mkv = futs[fut]
            try:
                ok += int(fut.result())
            except Exception:
                logger.exception('[%s] Locked clip encode failed for %s', sid, mkv.name)
    logger.info('Phase 1: %d/%d locked chunk(s) encoded in %.1fs', ok, len(jobs),
                time.monotonic() - t0)


def _run_overlay_details_all(stations: dict, date_str: str, cfg: dict) -> None:
    """Burn per-meteor measurement details into locked detection clips.

    Runs after the detection clips are locked and encoded (Phase 1), so the
    pristine ``_color.mkv`` bases exist and never change again. For each locked
    clip with a matching RMS detection it writes a ``<stem>.details.mkv`` variant
    carrying shower / magnitude / altitude / velocity / ToF in the free overlay
    real estate. Idempotent — safe on every tick and safe to re-run after a
    better calculation. Gated by ``services.overlay_details.enabled`` (on in
    config_defaults.json; a station can turn the extra CPU pass off).
    """
    for station_id in stations:
        try:
            overlay_details.run_night(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] Overlay-details pass failed', station_id)


# How many previous nights the detection backfill sweeps. Bounded by chunk
# retention (video_days_to_keep is 2-5 fleet-wide): older nights have no
# chunks left to lock, so sweeping further back is pointless.
DETECTION_BACKFILL_NIGHTS = 3


def _run_detection_backfill(stations: dict, date_str: str, cfg: dict) -> None:
    """Re-run the detection path for recent nights whose EON pass never ran.

    A night can slip through the per-night pipeline: RMS finishing after the
    last dawn tick, a reboot mid-run, or FTPdetectinfo appearing between ticks
    (see detection_lock.process_night).  Without a retry those detections stay
    "clip pending" on the dashboard until the janitor reaps the unlocked
    chunks — the clips are then gone for good.  Locking + encoding detection
    clips is cheap relative to the bulk phases, so sweep the last few nights
    every morning until each night's EON pass has actually run.
    """
    try:
        night0 = datetime.strptime(date_str, '%Y%m%d')
    except ValueError:
        return
    capture_root = rovimen_config.capture_root(cfg)
    for offset in range(1, DETECTION_BACKFILL_NIGHTS + 1):
        night = (night0 - timedelta(days=offset)).strftime('%Y%m%d')
        for station_id, scfg in stations.items():
            eon_done = flags_manager.is_eon_done(station_id, night, cfg)
            if eon_done and not scfg.get('rms', True):
                continue
            if not (capture_root / station_id / night).is_dir():
                continue  # chunks already reaped (or night never captured)
            rms = _rms_night(station_id, night, scfg.get('rms_data_path', ''))
            if not rms.detected:
                continue  # RMS output still missing — leave for a later run
            if eon_done:
                recorded = _eon_sources(station_id, night, cfg)
                if recorded is None:
                    # Reconciled before sources were recorded: adopt today's
                    # set as the baseline rather than rerun every old night.
                    _record_eon_sources(station_id, night, rms.ftpdetectinfo, cfg)
                    continue
                new = sorted(set(rms.ftpdetectinfo) - set(recorded))
                if not new:
                    continue
                logger.info('[%s] Detection backfill: new FTPdetectinfo for %s (%s) — '
                            're-running the EON pass', station_id, night, ', '.join(new))
            else:
                logger.info('[%s] Detection backfill: EON pass never ran for %s — retrying',
                            station_id, night)
            try:
                if not _run_eon_pass(station_id, night, cfg):
                    continue
                flags_manager.mark_rms_complete(station_id, night, cfg)
            except Exception:
                logger.exception('[%s] Detection backfill failed for %s', station_id, night)
                continue
            try:
                encoder.process_night_locked_only(station_id, night, cfg)
            except Exception:
                logger.exception('[%s] Backfill locked-clip encode failed for %s',
                                 station_id, night)
            if _uploader_enabled(cfg):
                try:
                    archive_upload.run_night(station_id, night, cfg)
                except Exception:
                    logger.exception('[%s] Backfill upload failed for %s', station_id, night)


def _run_upload_all(stations: dict, date_str: str, cfg: dict,
                    should_abort: Callable[[], bool] | None = None) -> None:
    """Phase 5: Upload stacks + locked clips to archive.

    Only the current night's data is uploaded here — backlog from previous
    nights is handled by _run_backfill_upload() in Phase 5.

    Cameras are uploaded in parallel — each camera creates an independent
    _Uploader and writes to its own state.json.

    ``should_abort`` is checked before every file, not just here: on a slow
    uplink one night's clips can take hours, and gmnro01 (Ghirdoveni)'s
    detection upload on 2026-10-24 ran from 11:55 to 14:32 UTC, holding the
    night unmarked past the reboot guard's wait. Whatever is left goes with
    the upload daemon.
    """
    if should_abort is not None and should_abort():
        logger.warning('Deadline reached — skipping the upload; '
                       'the upload daemon picks it up')
        return
    if len(stations) <= 1:
        for station_id in stations:
            try:
                archive_upload.run_night(station_id, date_str, cfg, should_abort)
            except Exception:
                logger.exception('[%s] Current-night upload failed', station_id)
    else:
        with ThreadPoolExecutor(max_workers=len(stations)) as pool:
            futs = {pool.submit(archive_upload.run_night, sid, date_str, cfg,
                                should_abort): sid
                    for sid in stations}
            for fut in as_completed(futs):
                try:
                    fut.result()
                except Exception:
                    logger.exception('[%s] Current-night upload failed', futs[fut])


# Seconds past the stagger slot before the retried detection upload starts, so
# run_night's own slot check (from its own clock reading) sees the slot open.
_SLOT_MARGIN_S = 5.0


class _SlotUpload:
    """Re-runs the Phase 3 detection upload once this station's stagger slot opens.

    archive_upload.run_night skips (returns 0) before the station's dawn slot,
    and the dawn lock makes the next cron ticks exit, so the detection clips
    used to wait for Phase 5, after all the stacking; only the separate upload
    daemon covered the gap. The retry runs on a timer thread while the bulk
    path carries on; ``settle`` makes the Phase 5 upload wait for a retry that
    is already under way instead of uploading the same night alongside it.
    """

    def __init__(self, stations: dict, date_str: str, cfg: dict) -> None:
        self.stations = stations
        self.date_str = date_str
        self.cfg = cfg
        self._timer: threading.Timer | None = None
        self._deadline: BulkDeadline | None = None
        self._started = threading.Event()
        self._done = threading.Event()

    def schedule(self, deadline: BulkDeadline) -> None:
        try:
            hold = archive_upload.stagger_hold_s(self.cfg, archive_upload.stagger_id(self.cfg))
        except Exception:
            logger.exception('Dawn stagger: could not read the upload slot')
            return
        if hold <= 0:
            return
        if deadline.enabled and hold + _SLOT_MARGIN_S >= deadline.seconds_left():
            logger.info('Dawn stagger: slot opens after the deadline — the detection upload '
                        'is left to the upload daemon')
            return
        logger.info('Dawn stagger: detection upload retried when the slot opens in %.0fs', hold)
        self._deadline = deadline
        self._timer = threading.Timer(hold + _SLOT_MARGIN_S, self._run)
        self._timer.daemon = True
        self._timer.start()

    def _run(self) -> None:
        self._started.set()
        try:
            _run_upload_all(self.stations, self.date_str, self.cfg, self._deadline)
        except Exception:
            logger.exception('Retried detection upload failed')
        finally:
            self._done.set()

    def settle(self) -> None:
        """Wait for a retry already under way; one not yet due is left to its timer."""
        if self._started.is_set():
            self._done.wait()


def _run_detection_upload(stations: dict, date_str: str, cfg: dict,
                          slot: _SlotUpload | None, deadline: BulkDeadline) -> None:
    """Phase 3: upload the locked clips now, and again at the stagger slot if held.

    Stops at the deadline like the bulk path: the clips are already encoded,
    so the upload daemon carries the rest, and an upload past 13:15 only kept
    the night unmarked and the station from its reboot."""
    _run_upload_all(stations, date_str, cfg, deadline)
    if slot is not None:
        slot.schedule(deadline)


def _run_bulk_upload(stations: dict, date_str: str, cfg: dict, deadline: BulkDeadline,
                     slot: _SlotUpload | None) -> None:
    """Phase 5: upload the full-night stacks (after any detection retry under way)."""
    if slot is not None:
        slot.settle()
    _run_upload_all(stations, date_str, cfg, deadline)


def _run_backfill_upload(cfg: dict, stop: threading.Event | None = None) -> None:
    """Phase 5 backfill: upload previous nights if time permits.

    Runs a full-scan pass (all stations, all nights) so that stacks and
    meteor clips from earlier nights are eventually synced to the archive.
    Skipped if fewer than 20 minutes remain before the 13:15 UTC hard
    deadline — not enough time to make meaningful progress. ``stop`` ends the
    pass at the next file (``_BackfillUpload`` sets it at the deadline).
    Gated by the dawn stagger like archive_upload.run_once.
    """
    now = datetime.now(timezone.utc)
    deadline = now.replace(hour=_DEADLINE_HOUR, minute=_DEADLINE_MINUTE, second=0, microsecond=0)
    minutes_left = (deadline - now).total_seconds() / 60
    if minutes_left < 20:
        logger.info('Only %.0f min before deadline — skipping backfill upload', minutes_left)
        return
    if not cfg.get('stations'):
        return
    try:
        uploader = archive_upload._Uploader(cfg)
        if not uploader.enabled:
            return
        hold = archive_upload.stagger_hold_s(cfg, archive_upload.stagger_id(cfg))
        if hold > 0:
            logger.info('Dawn stagger: skipping the backfill upload, slot opens in %.0fs', hold)
            return
        logger.info('%.0f min before deadline — running backfill upload sweep', minutes_left)
        count = uploader.cycle(stop)
        logger.info('Backfill upload sweep: %d file(s)', count)
    except Exception:
        logger.exception('Backfill upload sweep failed')


# How long a stopped backfill upload gets to reach a file boundary.
_BACKFILL_STOP_GRACE_S = 60.0


class _BackfillUpload:
    """The backfill upload, on a thread alongside Phases 6-7.

    It is a network-bound full scan of every night, so running it before the
    timelapse and the re-encode (as it did) spent CPU-idle time the bulk path
    needed. It now starts after the Phase 5 upload, stops at the deadline, and
    ``finish`` (after Phase 7) waits for it until then.
    """

    def __init__(self, cfg: dict, deadline: BulkDeadline) -> None:
        self.cfg = cfg
        self.deadline = deadline
        self.stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._timer: threading.Timer | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=_run_backfill_upload,
                                        args=(self.cfg, self.stop),
                                        name='backfill-upload', daemon=True)
        self._thread.start()
        if self.deadline.enabled:
            self._timer = threading.Timer(self.deadline.seconds_left(), self.stop.set)
            self._timer.daemon = True
            self._timer.start()

    def finish(self) -> None:
        if self._thread is None:
            return
        self._thread.join(self.deadline.seconds_left() if self.deadline.enabled else None)
        if self._thread.is_alive():
            logger.info('Deadline reached — stopping the backfill upload at the next file')
            self.stop.set()
            self._thread.join(_BACKFILL_STOP_GRACE_S)
            if self._thread.is_alive():
                logger.warning('Backfill upload still busy %.0fs after the stop — leaving it',
                               _BACKFILL_STOP_GRACE_S)
        if self._timer is not None:
            self._timer.cancel()


def _run_reencode_all(stations: dict, date_str: str, cfg: dict,
                      should_abort: Callable[[], bool] | None = None) -> None:
    """Phase 7: Full bulk reencode of all chunks for all cameras.

    Runs after upload so the archive receives clips without waiting for the
    full night's pass.  process_night() is idempotent — locked clips already
    encoded in Phase 1 are skipped automatically.

    This is the long-running phase and the one the 13:15 UTC deadline usually
    stops. morning_done is marked per camera as each camera's reencode
    completes; a camera the deadline interrupts is left unmarked here and the
    caller marks it done with the bulk path deferred. The next morning only
    processes the new night, so an interrupted pass is not resumed.

    When services.reencode.full_night resolves to False (default 'auto' on
    libx264-only boxes), the bulk pass is skipped: detection clips are already
    re-encoded in Phase 1, and stacks/timelapses do not need it. The final
    stacking pass and morning_done still run so the night completes cleanly.

    When it runs, a ``FullNightBudget`` watches it: a pass that cannot finish
    before the deadline, or that makes the clips bigger, is stopped and the
    decision is written to ``flags_manager.FULL_NIGHT_MARKER_PATH`` as this
    morning's outcome; the next morning measures again (a pass that runs to
    completion clears it). Skipping it is the same state a
    ``full_night: false`` station is in: unlocked chunks keep
    ``reencoded: false``, which nothing waits on (archive_upload only ships
    locked clips, and station_api renders an unencoded chunk on demand).
    """
    full_night = _full_night_reencode_enabled(cfg)
    if not full_night:
        logger.info('Full-night reencode disabled (services.reencode.full_night) '
                    '— detection clips already encoded in Phase 1; skipping bulk pass')
    budget: FullNightBudget | None = None

    def encode_abort() -> bool:
        """Stop at the next chunk: the budget's verdict, or the deadline."""
        if budget is not None and budget.stopped():
            return True
        return should_abort is not None and should_abort()

    if full_night:
        deadline_clock = (should_abort.seconds_left
                          if isinstance(should_abort, BulkDeadline) and should_abort.enabled
                          else None)
        budget = FullNightBudget(
            pending_chunks=sum(_pending_full_night_chunks(sid, date_str, cfg)
                               for sid in stations),
            parallelism=encoder._batch_cfg(cfg)['_encode_parallelism'],
            seconds_left=deadline_clock,
            enforce=_full_night_setting(cfg) == 'auto',
        )

    recorded = False
    for station_id in stations:
        if should_abort is not None and should_abort():
            logger.warning('[%s] Deadline reached — re-encode not started', station_id)
            if budget is not None and not recorded and budget.pending > 0:
                budget.deadline_hit()
                _record_full_night_stop(budget, date_str)
            return
        if budget is not None and not budget.stopped():
            try:
                with _timed_camera('reencode', station_id):
                    encoder.process_night(station_id, date_str, cfg, encode_abort,
                                          on_encoded=budget.record)
            except Exception:
                logger.exception('[%s] Re-encode failed', station_id)
            if (budget.decision is None and budget.pending > 0
                    and should_abort is not None and should_abort()):
                budget.deadline_hit()
            if not recorded and budget.decision is not None:
                _record_full_night_stop(budget, date_str)
                recorded = True
        # Final stacking pass: picks up any chunks written after Phase 4 ran
        # (write-race at 04:00 — last ffmpeg segments close while dawn is already
        # stacking earlier chunks; the encoder globs them fresh so they get
        # reencoded, but stacking is never revisited without this pass).
        # A camera the stack plan left out gets only its late locked chunks.
        if _stacker_enabled(cfg):
            try:
                stacker.process_night(
                    station_id, date_str, cfg, should_abort,
                    scope=None if _stack_fits(station_id, date_str, cfg) else 'detections')
            except Exception:
                logger.exception('[%s] Final stack pass failed', station_id)
        if should_abort is not None and should_abort():
            # Stopped part-way through this camera: leave it unmarked here and
            # let the caller's deferred-done pass explain why.
            logger.warning('[%s] Deadline reached during the re-encode pass', station_id)
            return
        flags_manager.mark_morning_done(station_id, date_str, cfg)
        logger.info('=== [%s] Morning sequence complete: %s ===', station_id, date_str)
    if budget is not None and budget.decision is None and not budget.stopped():
        flags_manager.clear_full_night_marker()


# ---------------------------------------------------------------------------
# Phase runner
# ---------------------------------------------------------------------------

# (name, zero-argument callable). typing.List/Tuple: evaluated at import on 3.8.
Phase = Tuple[str, Callable[[], object]]


@contextmanager
def _timed_camera(phase: str, station_id: str) -> Iterator[None]:
    """Log how long one camera spent in one phase."""
    t0 = time.monotonic()
    try:
        yield
    finally:
        logger.info('[%s] %s: %.1fs', station_id, phase, time.monotonic() - t0)


def _run_phase(name: str, fn: Callable[[], object],
               failures: List[str] | None = None) -> bool:
    """Run one phase, logging its start and elapsed time.

    A phase that raises is logged and recorded in ``failures``, and the run
    carries on with the next phase: one broken phase (an OSError from a hung
    disk, a bug in one runner) must not cost the rest of the morning, and above
    all must not leave the night without ``morning_done`` — the reboot guard
    waits on that flag and refuses to reboot on timeout.
    """
    t0 = time.monotonic()
    logger.info('--- phase %s: start', name)
    try:
        fn()
    except Exception:
        logger.exception('--- phase %s FAILED after %.1fs — continuing with the next phase',
                         name, time.monotonic() - t0)
        if failures is not None:
            failures.append(name)
        return False
    logger.info('--- phase %s: %.1fs', name, time.monotonic() - t0)
    return True


def _run_phases(phases: List[Phase], failures: List[str] | None = None) -> None:
    for name, fn in phases:
        _run_phase(name, fn, failures)


def _run_meteor_stack_all(stations: dict, date_str: str, cfg: dict) -> None:
    """The per-night colour meteor stack, composited from the locked chunks' stacks."""
    for station_id in stations:
        try:
            stacker.build_night_color_meteor_stack(station_id, date_str, cfg)
        except Exception as e:
            logger.warning('[%s] night meteor stack failed: %s', station_id, e)


def _run_stack_phase(stations: dict, date_str: str, cfg: dict,
                     should_abort: Callable[[], bool] | None) -> None:
    """Phase 4: stack every chunk, then restack cameras left with gaps."""
    _run_stack_all(stations, date_str, cfg, should_abort)
    _verify_and_restack(stations, date_str, cfg, should_abort)


def _mark_all_morning_done(stations: dict, date_str: str, cfg: dict) -> None:
    for station_id in stations:
        flags_manager.mark_morning_done(station_id, date_str, cfg)
        logger.info('=== [%s] Morning sequence complete: %s ===', station_id, date_str)


def _detection_phases(pending: dict, stations: dict, date_str: str, cfg: dict,
                      manual: bool, skip_eon: frozenset = frozenset(),
                      slot: _SlotUpload | None = None,
                      deadline: BulkDeadline | None = None) -> List[Phase]:
    """The detection path, in order. Runs first, always, and has no deadline."""
    phases: List[Phase] = [
        # Chunks whose ready flag the inotify watcher missed.
        ('reconcile-ready', lambda: _reconcile_ready(pending, date_str, cfg)),
    ]
    phases.append(('1 detection',
                   lambda: _run_encode_locked_all(pending, date_str, cfg, skip_eon)))
    # Per-meteor measurement details (shower/mag/alt/velocity/ToF) burned into
    # the locked clips, now that RMS has the numbers and the base clips exist.
    # Not where meteor clips are encoded on demand: a details clip uploads only
    # after its base clip, so dawn would render ~217 a day on gmnro05 (Bistrița),
    # 1-1.5 h of libx264, that never leave the station. process_clip renders
    # the details of each clip it delivers.
    if _svc_enabled(cfg, 'overlay_details') and _meteor_clips_encoded(cfg):
        phases.append(('1b overlay-details',
                       lambda: _run_overlay_details_all(pending, date_str, cfg)))
    # Upload the locked clips straight away so detections reach the archive
    # before the heavy bulk work.
    if _uploader_enabled(cfg):
        phases.append(('3 detection-upload',
                       lambda: _run_detection_upload(pending, date_str, cfg, slot,
                                                     deadline or BulkDeadline(enabled=False))))
    # Retry the EON pass for recent nights that slipped through (RMS late,
    # mid-run reboot, FTPdetectinfo race) while their chunks are still on disk.
    if not manual:
        phases.append(('detection-backfill',
                       lambda: _run_detection_backfill(stations, date_str, cfg)))
    return phases


def _bulk_phases(pending: dict, date_str: str, cfg: dict, deadline: BulkDeadline,
                 manual: bool, slot: _SlotUpload | None = None) -> List[Phase]:
    """The bulk path, in order. Every phase polls ``deadline``."""
    phases: List[Phase] = []
    if _stacker_enabled(cfg):
        phases.append(('4 stack', lambda: _run_stack_phase(pending, date_str, cfg, deadline)))
        # The colour meteor stack composites the locked chunks' stacks, so it
        # is built once they exist (it used to run before Phase 4 and silently
        # skipped every locked chunk not yet stacked), and before Phase 5
        # uploads it.
        phases.append(('4b meteor-stack', lambda: _run_meteor_stack_all(pending, date_str, cfg)))
    # Full-night stacks go up BEFORE the reencode marks morning_done, so an
    # interrupted reencode never leaves a night flagged done with its stacks
    # unuploaded.
    backfill: _BackfillUpload | None = None
    if _uploader_enabled(cfg):
        phases.append(('5 upload',
                       lambda: _run_bulk_upload(pending, date_str, cfg, deadline, slot)))
        if not manual:
            backfill = _BackfillUpload(cfg, deadline)
            phases.append(('5 backfill-upload start', backfill.start))
    # Timelapse straight after the stacks it is built from (1-4 min per camera).
    # It used to run after the full-night re-encode, which it does not need, and
    # the 3-4 camera stations reached the deadline before ever getting there.
    # The timelapse needs every chunk stacked, so not under scope "detections".
    if (_stacker_enabled(cfg) and _svc_enabled(cfg, 'timelapse_build')
            and stacker.stack_scope(cfg) == 'all'):
        phases.append(('6 timelapse', lambda: _run_timelapse_all(pending, date_str, cfg, deadline)))
        if not manual:
            phases.append(('6b deferred-timelapses',
                           lambda: _retry_deferred_timelapses(pending, cfg)))
    # Bulk reencode (long-running; marks morning_done per camera as each
    # finishes, with a final stacking pass for late chunks).
    if _svc_enabled(cfg, 'reencode'):
        phases.append(('7 reencode', lambda: _run_reencode_all(pending, date_str, cfg, deadline)))
    else:
        phases.append(('7 mark-done', lambda: _mark_all_morning_done(pending, date_str, cfg)))
    if backfill is not None:
        phases.append(('7b backfill-upload finish', backfill.finish))
    return phases


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_MODULE_LOGS: dict[str, str] = {
    'stacker': 'stacker.log',
    'encoder': 'encoder.log',
    'timelapse_build': 'timelapse.log',
}

_fmt = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s',
                         datefmt='%Y-%m-%dT%H:%M:%S')


def _attach_module_logs(cfg: dict) -> None:
    """Attach per-module FileHandlers after config is loaded.

    Each of stacker, encoder, and timelapse_build gets its own log file under
    log_path (default ~/logs/).  Records still propagate to the root handler so
    rovimen_dawn.log remains a complete record of the morning sequence.
    """
    log_dir = Path(cfg.get('log_path') or (Path.home() / 'logs'))
    log_dir.mkdir(parents=True, exist_ok=True)
    for module_name, filename in _MODULE_LOGS.items():
        mod_logger = logging.getLogger(module_name)
        if any(isinstance(h, logging.FileHandler) and
               Path(h.baseFilename).name == filename
               for h in mod_logger.handlers):
            continue  # already attached (e.g. second call in same process)
        fh = logging.FileHandler(log_dir / filename)
        fh.setFormatter(_fmt)
        mod_logger.addHandler(fh)


# The run's deadline, for the SIGTERM handler (None outside the night's run).
_active_deadline: BulkDeadline | None = None


def _on_sigterm(signum: int, frame: object) -> None:
    """Stop at once on SIGTERM, killing the ffmpeg children with us.

    color_capture sends SIGTERM to dawn_process when a chunk of a new night
    closes (evening capture has started, so a manual run still going must
    yield), and a shutdown sends it too. The default action killed dawn but
    left its encode/stack ffmpeg children running into the capture. Nothing is
    marked done here: the next dawn tick must resume the night. Past the deadline the night would be
    marked done anyway, so there the handler does the hard stop's bookkeeping.
    """
    deadline = _active_deadline
    if deadline is not None and deadline.enabled and deadline.passed():
        logger.warning('SIGTERM past the %02d:%02d UTC deadline — marking the night done '
                       'and exiting', deadline.hour, deadline.minute)
        deadline._hard_stop()
        return
    logger.warning('SIGTERM (evening colour capture started or host shutting down) — '
                   'killing the encode/stack children and exiting; nothing marked done, '
                   'the next dawn run resumes the night')
    _terminate_children()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(128 + signum)


def main() -> int:
    # Ensure only one instance runs at a time — cron fires every 15 min and
    # processing a full night can take longer than that on slow stations.
    _lock_fd = open(_LOCK_FILE, 'w')
    try:
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        _lock_fd.close()
        print('dawn_process: another instance is running — exiting', file=sys.stderr)
        return 0

    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        return _main_locked()
    finally:
        _lock_fd.close()


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='Post-RMS morning processing')
    parser.add_argument('-c', '--config', default=str(DEFAULT_CONFIG),
                        help='Path to config.json')
    parser.add_argument('--station', help='Process a specific station only')
    parser.add_argument('--date', help='Override night date (YYYYMMDD)')
    parser.add_argument('--force', action='store_true',
                        help='With --date: re-run a night already marked done, and do '
                             'not wait for RMS (cameras whose RMS is unfinished skip '
                             'the EON pass)')
    args = parser.parse_args(argv)
    if args.force and not args.date:
        parser.error('--force needs --date')
    return args


def _load_cfg(config_path: str) -> dict | None:
    try:
        with open(config_path) as f:
            cfg = json.load(f)
    except FileNotFoundError:
        logger.error('Config not found: %s', config_path)
        return None
    # Inject cpu_cores from hardware.json (co-located with config.json) so
    # phase functions can derive parallelism without needing the config path.
    hw_path = Path(config_path).parent / 'hardware.json'
    try:
        hw = json.loads(hw_path.read_text())
        cfg['_cpu_cores'] = int(hw.get('cpu_cores', 4))
    except Exception:
        cfg['_cpu_cores'] = 4
    return cfg


def _active_stations(stations: dict) -> dict:
    """Cameras dawn processes: everything not flagged ``decommissioned``.

    A decommissioned camera (dead hardware whose rms-camN.service is disabled)
    left in the loop blocks the entire station: the RMS-finished gate waits for
    an FTPdetectinfo that will never appear, every dawn cron tick, until the
    bulk deadline kills the whole night's processing for the surviving cameras
    too. reboot_guard.sh applies the same filter.
    """
    return {sid: scfg for sid, scfg in stations.items() if not scfg.get('decommissioned')}


def _main_locked() -> int:
    args = _parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    cfg = _load_cfg(args.config)
    if cfg is None:
        return 1
    _attach_module_logs(cfg)

    date_str = args.date or _night_date()

    # Hard deadline — do not start (or continue) automatic processing after 13:15 UTC.
    # Protects against a late-starting cron slot or manual invocation creeping into
    # the reboot window.  Bypass with --date for manual/debug runs.
    if not args.date:
        now_utc = datetime.now(timezone.utc)
        if (now_utc.hour, now_utc.minute) >= (_DEADLINE_HOUR, _DEADLINE_MINUTE):
            logger.info('Past %02d:%02d UTC deadline (%s) — skipping',
                        _DEADLINE_HOUR, _DEADLINE_MINUTE, now_utc.strftime('%H:%M'))
            return 0

    stations = cfg.get('stations', {})
    if args.station:
        if args.station not in stations:
            logger.error('Station %s not in config', args.station)
            return 1
        stations = {args.station: stations[args.station]}

    decommissioned = [sid for sid in stations if sid not in _active_stations(stations)]
    if decommissioned:
        logger.info('Skipping decommissioned cameras: %s', ', '.join(decommissioned))
        stations = _active_stations(stations)
        if not stations:
            logger.info('No active stations remaining — nothing to do')
            return 0

    timelapses = _stacker_enabled(cfg) and _svc_enabled(cfg, 'timelapse_build')

    # Only process once ALL pending stations have finished RMS post-processing.
    # Processing one camera while others are still running DetectStarsAndMeteors
    # starves RMS of CPU headroom.
    if args.force:
        pending = dict(stations)
        logger.info('--force: re-running %s for %s', date_str, ', '.join(pending))
    else:
        pending = {
            sid: scfg for sid, scfg in stations.items()
            if not flags_manager.is_morning_done(sid, date_str, cfg)
        }

    if not pending:
        logger.debug('All stations already morning_done for %s', date_str)
        # Retry timelapses deferred from previous nights (low stack coverage).
        # Only here and after tonight's timelapse: while RMS is still detecting
        # a rebuild would compete with it for the CPU.
        if timelapses:
            _run_phase('deferred-timelapses', lambda: _retry_deferred_timelapses(stations, cfg))
        return 0

    runs = _rms_runs()
    waiting: Dict[str, str] = {}
    for sid, scfg in pending.items():
        reason = _rms_wait_reason(sid, scfg, date_str, runs)
        if reason is not None:
            waiting[sid] = reason
    if waiting:
        if not (args.force or _rms_wait_expired(cfg)):
            # An RMS that hung on a full disk never finishes; restart it
            # rather than wait out the deadline (rms_hang module docstring).
            try:
                rms_hang.recover(list(waiting), date_str, cfg)
            except Exception:
                logger.exception('RMS hang check failed')
            logger.info('Waiting for RMS to finish on: %s — will retry',
                        ', '.join('%s (%s)' % item for item in waiting.items()))
            return 0
        why = ('--force' if args.force else 'past %02d:00 UTC' % int(
            cfg.get('dawn_rms_wait_until_hour', _RMS_WAIT_UNTIL_HOUR)))
        logger.warning('RMS still not finished on %s (%s) — processing the night anyway; '
                       'cameras still in detection skip their EON pass and the detection '
                       'backfill reconciles them once RMS completes',
                       ', '.join('%s (%s)' % item for item in waiting.items()), why)
    # Only a camera without detections skips the EON pass; one whose RMS is still
    # post-processing already has the FTPdetectinfo the pass reconciles against.
    rms_incomplete = frozenset(sid for sid, reason in waiting.items() if reason == 'detection')

    recording = [sid for sid, scfg in pending.items()
                 if _colour_recording(sid, scfg, date_str, cfg, time.time())]
    if recording:
        if not (args.force or _rms_wait_expired(cfg)):
            logger.info('Waiting for colour capture to finish on: %s — will retry',
                        ', '.join(recording))
            return 0
        logger.warning('Colour capture still writing on %s past the wait — processing the '
                       'night anyway', ', '.join(recording))

    def _mark_bulk_deferred(reason: str) -> None:
        """Finish the night with the bulk path left for the next run.

        The 13:45 reboot guard waits on ``morning_done`` and deliberately does
        not reboot on timeout, so a night left unmarked keeps the station up
        all day. By this point the detections are locked, encoded and
        uploaded; stacks, re-encode and timelapse are best-effort.
        """
        for station_id in pending:
            try:
                if not flags_manager.is_morning_done(station_id, date_str, cfg):
                    flags_manager.mark_morning_done(station_id, date_str, cfg)
                    logger.info('=== [%s] Morning sequence complete (%s, bulk deferred): '
                                '%s ===', station_id, reason, date_str)
            except Exception:
                logger.exception('[%s] Could not mark %s done', station_id, date_str)

    # --date is the manual/debug path: no deadline, no watchdog.
    deadline = BulkDeadline(enabled=not args.date,
                            on_hard_stop=lambda: _mark_bulk_deferred('hard stop'))
    global _active_deadline
    _active_deadline = deadline
    # Manual runs are outside the dawn window; the stagger never holds them.
    slot = None if args.date else _SlotUpload(pending, date_str, cfg)
    failures: List[str] = []
    completed = False
    try:
        # ── Detection path — runs FIRST and always. Locking + encoding the
        #    detection clips is what makes a night's meteors visible (dashboard +
        #    archive), so it must never be starved by the bulk colour work. The
        #    old order stacked every chunk first; on a station too slow to finish
        #    stacking before the reboot, the lock pass never ran and recent nights
        #    showed no detections at all. Detections first fixes that. ──
        _run_phases(_detection_phases(pending, stations, date_str, cfg,
                                      manual=bool(args.date), skip_eon=rms_incomplete,
                                      slot=slot, deadline=deadline),
                    failures)

        # ── Bulk colour stacks/clips, then timelapse — lower priority. Full
        #    stacking is the slow phase, so it runs AFTER the detection path and
        #    is skipped past the 13:15 UTC deadline: a slow station still gets its
        #    detections, and the bulk stacks are best-effort. ──
        if deadline.passed():
            logger.info('Past %02d:%02d UTC deadline after detection path — '
                        'skipping bulk stacking, reencode and timelapse',
                        _DEADLINE_HOUR, _DEADLINE_MINUTE)
            # Still composite whatever stacks exist (the locked chunks were
            # stacked in Phase 1); it takes seconds.
            if _stacker_enabled(cfg):
                _run_phase('4b meteor-stack',
                           lambda: _run_meteor_stack_all(pending, date_str, cfg), failures)
        else:
            # The bulk path can outlast the deadline it was cleared to start
            # under, so from here on every phase polls `deadline` and stops at
            # the next chunk boundary once it fires.
            deadline.arm()
            try:
                _run_phases(_bulk_phases(pending, date_str, cfg, deadline,
                                         manual=bool(args.date), slot=slot), failures)
            finally:
                deadline.disarm()
        completed = True
    finally:
        # Bookkeeping, whatever happened above. Detections are locked and
        # uploaded by now (or failed in a way a rerun would repeat); a night left
        # unmarked keeps the station up all day and respins the detection path
        # every tick.
        if deadline():
            _mark_bulk_deferred('deadline')
        elif not completed:
            _mark_bulk_deferred('run aborted')
        elif failures:
            _mark_bulk_deferred('failed phase(s): ' + ', '.join(failures))
        _active_deadline = None

    try:
        _sweep_stale_locks(cfg)
    except Exception:
        logger.exception('Stale lock sweep failed (EIO?) — continuing')

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
