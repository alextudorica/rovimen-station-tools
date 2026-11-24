#!/usr/bin/env python3
"""Storage-box retention janitor (VPS-side).

The Hetzner storage box keeps every uploaded detection clip forever, with no
time-based culling -- so it fills up (it hit 99% on a 1 TB box in June 2026).
Color-video MKVs are the bulk; stacks/metadata are tiny.

This janitor enforces a *value-based* retention policy on the cold tier:

    A clip's color-video MKV is pruned once it is older than RETENTION_DAYS,
    UNLESS the clip is "worth keeping forever" by any of:
      - manually locked by an operator      (lock_type == "manual")
      - part of a multi-station event        (GMN-confirmed OR internally
                                              correlated across >=2 cameras)
      - in the top BRIGHT_PERCENTILE %        (brightest, by peak magnitude)
      - in the top LONG_PERCENTILE %          (longest, by duration)
      - in the Hall of Fame                  (hall_of_fame.json)
      - locked or unlocked by an operator    (POST /api/lock in activity.log
                                              or its rotated segments)
      - played by a signed-in user           (kept_clips.jsonl, kept_clips.py)

A clip counts as locked when its state.json chunk has ``lock`` or, on the
box, ``archived_lock``: the station's dawn sweep clears ``lock`` a week after
the night, and the ingest flush keeps the last lock it saw as
``archived_lock`` (``blob_spool.merge_state_json``). Nights archived before
that merge existed have neither; ``--include-unlocked`` also considers their
clips (every clip on the box was uploaded because it was locked), which is
what a dry run should report before the timer gets the flag.

Only the clip's video is ever removed: the uploaded ``*_color.mkv``, the
stream-copied ``*_color.mp4`` the dashboard serves (uploaded alongside since
night 20260906), and the ``*_color.details.{mkv,mp4}`` overlay renders. The
stack ``.webp``, the ``rms/`` analysis, and ``state.json`` are always kept, so
the visual record and all metadata survive forever and the dashboard still
shows the detection (just without the full-resolution clip). A clip pruned
before the ``.mp4`` copies were handled lost only its ``.mkv``; the next run
removes the copies it left behind.

Brightness/duration percentile thresholds are computed across the WHOLE
archive (all ages), so "top 20% brightest" means brightest of all time, not
brightest of the pruned cohort. That population includes clips whose video is
gone (pruned by an earlier run, or never uploaded): their ``rms/`` detections
are never pruned, so they are still measured, and leaving them out made each
run's cut-off tighter than the last. A cut-off is never stricter than the one
over the clips still on the box (the rule before), so counting them only ever
keeps more.

SAFETY: dry-run by default. Pass ``--apply`` to actually delete. A date whose
``state.json`` can't be read is skipped entirely (never prune blind). Runs on
the VPS, which owns the read-write sshfs mount, so deletion is a plain
``os.remove`` on the mounted path.

Typical use:
    # see what would be pruned, reclaimable GB, without touching anything
    python storagebox_janitor.py --report /opt/rovimen/janitor_report.json

    # actually prune, capped at 5000 deletions per run as a throttle
    python storagebox_janitor.py --apply --max-deletes 5000
"""

from __future__ import annotations

import argparse
import bisect
import json
import logging
import os
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Sibling dashboard modules are imported by bare name (the service puts the
# dashboard dir on sys.path). When run as a standalone script, add our own dir.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from atomic_write import write_atomic  # noqa: E402
import gmn_data  # noqa: E402
import kept_clips  # noqa: E402
import usage_stats  # noqa: E402
from rms_parse import _filtered_ftp_paths, _ftp_rows  # noqa: E402

logger = logging.getLogger("storagebox_janitor")

DEFAULT_ARCHIVE = os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive")
DEFAULT_RETENTION_DAYS = 180          # ~6 months
DEFAULT_BRIGHT_PCT = 20.0             # keep brightest 20%
DEFAULT_LONG_PCT = 20.0               # keep longest 20%
DEFAULT_EVENT_TOLERANCE_S = 3.0       # clip<->GMN event time match window
DEFAULT_CORRELATION_WINDOW_S = 3.0    # internal multi-station coincidence window
DEFAULT_HOF_PATH = os.environ.get("ROVIMEN_HOF_PATH", "/opt/rovimen/hall_of_fame.json")
DEFAULT_ACTIVITY_LOG = os.environ.get("ROVIMEN_ACTIVITY_LOG_PATH", "/opt/rovimen/activity.log")

# Non-camera top-level dirs on the box that must never be scanned for clips.
_SKIP_DIRS = {"compilations", "skyfit", ".thumb_cache", ".thumb_cache_dev"}
_DATE_DIR_LEN = 8  # YYYYMMDD


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Clip:
    cam: str
    date: str                 # YYYYMMDD
    filename: str             # the *_color.mkv chunk name (state.json key)
    mkv_path: Path
    meteor_time: str | None
    lock_type: str | None
    # Brightest detection between this clip's start and the next clip that
    # has video on the box (the measurement the keep rules always used).
    mag: float | None = None          # peak magnitude (lower = brighter)
    duration_s: float | None = None
    multistation: bool = False
    # Brightest detection inside this clip's own block (up to the next chunk
    # of the night, whatever its video state): its rank among all clips.
    own_mag: float | None = None
    own_duration_s: float | None = None
    size_bytes: int = 0
    # Every video file of this clip on the box (see _video_names).
    video_paths: list[Path] = field(default_factory=list)
    # Already marked video_pruned by an earlier run; video_paths are the
    # copies that run left behind.
    leftover: bool = False
    # No video to judge (pruned, or never uploaded): only counted for the
    # all-time brightest/longest cut-offs.
    measure_only: bool = False

    @property
    def date_obj(self) -> datetime:
        return datetime.strptime(self.date, "%Y%m%d").replace(tzinfo=timezone.utc)


@dataclass
class Detection:
    """One RMS detection: frame-precise begin time (naive UTC) + peak magnitude.

    Both come straight from ``FTPdetectinfo`` -- every detection has them -- so
    resolving a clip's magnitude from detections never depends on ``radiants.txt``
    being present or on a fuzzy time tolerance.
    """
    time: datetime
    peak_mag: float | None
    duration_s: float | None
    multistation: bool = False


@dataclass
class Stats:
    scanned_clips: int = 0
    scanned_dates: int = 0
    skipped_dates: int = 0
    pruned: int = 0
    pruned_bytes: int = 0
    leftovers: int = 0            # earlier-pruned clips whose copies were removed
    leftover_bytes: int = 0
    kept_recent: int = 0
    kept_manual: int = 0
    kept_multistation: int = 0
    kept_bright: int = 0
    kept_long: int = 0
    kept_protected: int = 0       # Hall of Fame, an operator lock action or a play
    multistation_faint: int = 0   # multi-station but fainter than the mag gate
    resolved_mag: int = 0         # clips that resolved a magnitude
    measured_all_time: int = 0    # clips ranked for the all-time cut-offs, video or not
    errors: int = 0
    keep_reasons: dict[str, int] = field(default_factory=dict)
    # Would-prune / pruned totals per camera: {cam: [clips, bytes]}.
    per_camera: dict[str, list[int]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Time helpers
# ---------------------------------------------------------------------------

def _iso_date(yyyymmdd: str) -> str:
    return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"


def _parse_iso(ts: str | None) -> datetime | None:
    """Parse an ISO-ish timestamp to a naive UTC datetime (seconds precision)."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace(" ", "T")).replace(tzinfo=None)
    except ValueError:
        return None


def _ff_time_utc(ff_file: str) -> str | None:
    """Extract ``YYYY-MM-DDTHH:MM:SS`` from an FF filename like
    ``FF_RO000H_20260314_023422_123_0123456.fits``."""
    parts = ff_file.split("_")
    if len(parts) < 4 or len(parts[2]) != 8 or len(parts[3]) != 6:
        return None
    d, t = parts[2], parts[3]
    return f"{d[:4]}-{d[4:6]}-{d[6:8]}T{t[:2]}:{t[2:4]}:{t[4:6]}"


# ---------------------------------------------------------------------------
# Percentiles (stdlib-only, no numpy)
# ---------------------------------------------------------------------------

def _more_lenient(pick: Callable[[float, float], float],
                  a: float | None, b: float | None) -> float | None:
    """``pick(a, b)``, or whichever of the two is set."""
    if a is None or b is None:
        return b if a is None else a
    return pick(a, b)


def _percentile(sorted_vals: list[float], pct: float) -> float | None:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    rank = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = rank - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


# ---------------------------------------------------------------------------
# Archive scanning
# ---------------------------------------------------------------------------

def _read_state(date_dir: Path) -> dict | None:
    state_path = date_dir / "state.json"
    if not state_path.exists():
        return None
    try:
        return json.loads(state_path.read_text())
    except (OSError, ValueError):
        return None


# A clip/FF filename carries a ``YYYYMMDD_HHMMSS`` block-start token. The color
# clip and the RMS FF are SEPARATE captures with independent block boundaries,
# so their tokens do NOT line up to the second -- which is why matching a clip
# to its detection must be done by TIME CONTAINMENT (which color block contains
# the meteor's precise time), not by equal tokens or a tight tolerance.
_TOKEN_RE = re.compile(r"(\d{8}_\d{6})")


def _effective_lock(info: dict) -> dict | None:
    """The chunk's lock, or the one the box archived before the station's
    dawn sweep cleared it."""
    lock = info.get("lock") or info.get("archived_lock")
    return lock if isinstance(lock, dict) else (lock or None)


def _clip_key(cam: str, date: str, name: str) -> tuple[str, str, str]:
    """(CAM, YYYYMMDD, X_color.mkv) for any video name of a clip."""
    for suffix in (".details.mkv", ".details.mp4", ".mp4"):
        if name.endswith(suffix):
            name = name[: -len(suffix)] + ".mkv"
            break
    return cam.upper(), date.replace("-", ""), name


def load_protected(
    hof_path: Path | None, activity_log: Path | None,
    kept_path: Path | None = None,
) -> set[tuple[str, str, str]]:
    """Clips retention must never prune whatever their age: Hall of Fame
    entries, every clip an operator locked or unlocked from the dashboard
    (its lock POSTs are in the activity log; the lock itself may be gone), and
    every clip a signed-in user played (``kept_clips``)."""
    keep: set[tuple[str, str, str]] = set()
    if kept_path is not None:
        try:
            keep |= kept_clips.load(kept_path)
        except OSError as exc:
            logger.warning("kept clips %s unreadable (%s): no viewed-clip protection",
                           kept_path, exc)
    if hof_path is not None:
        try:
            for e in json.loads(hof_path.read_text()) or []:
                if isinstance(e, dict) and e.get("camera") and e.get("date") and e.get("filename"):
                    keep.add(_clip_key(e["camera"], e["date"], e["filename"]))
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as exc:
            logger.warning("hall of fame %s unreadable (%s): no HoF protection", hof_path, exc)
    if activity_log is not None:
        # logrotate moves the log into dated, gzipped segments beside it
        # (deployment/vps/logrotate/rovimen-dashboard); a lock action keeps
        # protecting its clip after its line rotates out of the live file.
        try:
            segments = [activity_log, *usage_stats._rotated_segments(activity_log)]
        except OSError as exc:
            logger.warning("activity log segments of %s unlistable (%s): "
                           "reading the live file only", activity_log, exc)
            segments = [activity_log]
        for segment in segments:
            keep |= _lock_action_clips(segment)
    return keep


def _lock_action_clips(segment: Path) -> set[tuple[str, str, str]]:
    """Clips with a dashboard lock or unlock POST in one activity-log segment
    (the live file or a rotated one, gzipped or not)."""
    keep: set[tuple[str, str, str]] = set()
    try:
        with usage_stats._open_segment(segment) as fh:
            for line in fh:
                if '"/api/lock/' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if rec.get("method") != "POST":
                    continue
                parts = (rec.get("path") or "").split("/")
                # /api/lock/<host>/<cam>/<date>/<file>
                if len(parts) == 7 and parts[1:3] == ["api", "lock"]:
                    keep.add(_clip_key(parts[4], parts[5], parts[6]))
    except FileNotFoundError:
        pass
    except (OSError, EOFError) as exc:
        logger.warning("activity log %s unreadable (%s): its lock actions are not protected",
                       segment, exc)
    return keep


def _video_names(mkv_name: str) -> tuple[str, ...]:
    """Names of every video file one clip can have in ``meteors/``: the
    uploaded MKV, its stream-copied MP4, and the overlay renders of both."""
    stem = mkv_name[: -len(".mkv")]
    return (mkv_name, f"{stem}.mp4", f"{stem}.details.mkv", f"{stem}.details.mp4")


def _fname_token(name: str) -> str | None:
    """First ``YYYYMMDD_HHMMSS`` token in a clip or FF filename (the block
    start), or None."""
    m = _TOKEN_RE.search(name)
    return m.group(1) if m else None


def _token_dt(name: str) -> datetime | None:
    """The block-start of a clip/FF filename as a naive UTC datetime, or None."""
    tok = _fname_token(name)
    if tok is None:
        return None
    try:
        return datetime.strptime(tok, "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def _parse_cam_detections(date_dir: Path) -> list[Detection]:
    """Every RMS detection for one camera-night, sorted by time.

    Magnitude and a frame-precise begin time come straight from
    ``FTPdetectinfo`` (via ``_ftp_rows``), which carries them for EVERY
    detection -- so this never depends on ``radiants.txt`` or a time tolerance.
    """
    rms_dir = date_dir / "rms"
    out: list[Detection] = []
    if not rms_dir.is_dir():
        return out
    try:
        paths = _filtered_ftp_paths(rms_dir)
    except OSError:
        return out
    for f in paths:
        try:
            rows = _ftp_rows(f)
        except OSError:
            continue
        for r in rows:
            t = r.get("_begin_dt")
            if t is None:
                continue
            out.append(Detection(
                time=t.replace(tzinfo=None),
                peak_mag=r.get("peak_mag"),
                duration_s=r.get("duration_s"),
            ))
    out.sort(key=lambda d: d.time)
    return out


def _assign_detection(det_time: datetime, clip_starts: list[datetime],
                      max_gap_s: float) -> int | None:
    """Index of the color clip whose block contains ``det_time``.

    Color blocks are contiguous, so the containing block is the clip with the
    greatest start <= det_time. A small clock skew between the color-capture and
    RMS pipelines only ever shifts this by at most one 20 s block (harmless for
    a keep/prune decision). Returns None when the nearest block start is more
    than ``max_gap_s`` away -- i.e. the meteor's block was never uploaded.
    """
    if not clip_starts:
        return None
    idx = bisect.bisect_right(clip_starts, det_time) - 1
    if idx < 0:
        idx = 0
    if abs((det_time - clip_starts[idx]).total_seconds()) > max_gap_s:
        return None
    return idx


def _gmn_station_times(date_iso: str) -> dict[str, list[datetime]]:
    """Map camera-code -> list of GMN multi-station event datetimes for a date."""
    out: dict[str, list[datetime]] = {}
    try:
        payload = gmn_data.events_for_date(date_iso)
    except Exception:
        logger.debug("GMN lookup failed for %s", date_iso, exc_info=True)
        return out
    for ev in payload.get("events") or []:
        if len(ev.get("stations") or []) < 2:
            continue
        dt = _parse_iso(ev.get("time"))
        if dt is None:
            continue
        for sta in ev["stations"]:
            out.setdefault(sta.upper(), []).append(dt)
    return out


def _existing_videos(meteors_dir: Path, mkv_name: str,
                     present: set[str]) -> tuple[list[Path], int]:
    """The clip's video files that exist (per the night's listing) and their
    total size. A file that vanishes between listing and stat is skipped."""
    paths: list[Path] = []
    size = 0
    for name in _video_names(mkv_name):
        if name not in present:
            continue
        p = meteors_dir / name
        try:
            size += p.stat().st_size
        except OSError:
            continue
        paths.append(p)
    return paths, size


def scan_date(cam_dirs: list[Path], date: str, tol_s: float,
              corr_window_s: float, stats: Stats,
              include_unlocked: bool = False) -> list[Clip]:
    """Scan every camera's clips for one date, meteor-first.

    1. Parse every camera's RMS detections (frame-precise time + peak_mag).
    2. Flag multi-station detections by correlating those precise times ACROSS
       cameras (and confirming against GMN events) -- RMS is the authority, so
       this never depends on the color-capture ``meteor_time`` being set.
    3. Map each detection onto the color clip whose block contains it (time
       containment), so every locked clip inherits the magnitude / duration /
       multi-station status of its meteor. A clip with no detection in its
       window is a non-detection block and stays unmeasured (kept conservatively
       if multi-station can't apply, else pruned by age).

    Returns the clips with video on the box, plus measure-only clips (video
    pruned by an earlier run, or never uploaded) that carry a detection or
    leftover copies: their ``own_mag``/``own_duration_s`` count toward the
    all-time cut-offs.
    """
    date_iso = _iso_date(date)
    gmn_times = _gmn_station_times(date_iso)
    max_gap_s = max(corr_window_s, 30.0)  # a detection lands within ~1 block

    # 1. detections per camera
    cam_dets: dict[str, list[Detection]] = {}
    for cam_dir in cam_dirs:
        dd = cam_dir / date
        if dd.is_dir():
            cam_dets[cam_dir.name] = _parse_cam_detections(dd)

    # 2a. cross-station correlation on frame-precise times
    flat = sorted(((d.time, cam, d) for cam, ds in cam_dets.items() for d in ds),
                  key=lambda x: x[0])
    for i, (ta, cama, da) in enumerate(flat):
        for tb, camb, db in flat[i + 1:]:
            if (tb - ta).total_seconds() > corr_window_s:
                break
            if camb != cama:
                da.multistation = True
                db.multistation = True
    # 2b. GMN multi-station confirmation
    for cam, dets in cam_dets.items():
        evs = gmn_times.get(cam.upper(), [])
        if not evs:
            continue
        for d in dets:
            if any(abs((ev - d.time).total_seconds()) <= tol_s for ev in evs):
                d.multistation = True

    # 3. map detections onto locked clips per camera
    clips: list[Clip] = []
    for cam_dir in cam_dirs:
        cam = cam_dir.name
        date_dir = cam_dir / date
        if not date_dir.is_dir():
            continue
        state = _read_state(date_dir)
        if state is None:
            # Can't know lock status -> never prune blind. Skip this (cam,date).
            stats.skipped_dates += 1
            logger.debug("skip %s/%s: no readable state.json", cam, date)
            continue
        meteors_dir = date_dir / "meteors"
        # One listing per night instead of a stat per possible file name:
        # this runs over sshfs across the whole archive.
        try:
            present = set(os.listdir(meteors_dir))
        except OSError:
            present = set()

        # Locked clips that still have video on the box (judged), and every
        # chunk of the night in time order (measured): a clip's own block runs
        # to the next chunk whatever its video state, while the window the
        # keep rules always used runs to the next clip with video.
        judged: list[tuple[datetime, Clip]] = []
        blocks: list[tuple[datetime, Clip | None]] = []
        chunks = state.get("chunks") or {}
        entries = [(f, i) for f, i in chunks.items() if isinstance(i, dict)]
        if include_unlocked:
            # Clips on the box that state.json doesn't list (spliced clips,
            # nights whose state.json was rewritten) are clips too.
            known = {f for f, _ in entries}
            for name in sorted(present):
                if not name.endswith(("_color.mkv", "_color.mp4")) or ".details." in name:
                    continue
                mkv = _clip_key(cam, date, name)[2]
                if mkv not in known:
                    known.add(mkv)
                    entries.append((mkv, {}))
        for fname, info in entries:
            if not fname.endswith("_color.mkv"):
                continue
            lock = _effective_lock(info) or {}
            sdt = _token_dt(fname)
            pruned = bool(info.get("video_pruned"))
            if not pruned and not lock and not include_unlocked:
                # Lock-less nights are only judged in --include-unlocked mode;
                # the chunk still ends the block before it.
                if sdt is not None:
                    blocks.append((sdt, None))
                continue
            paths, size = _existing_videos(meteors_dir, fname, present)
            clip = Clip(
                cam=cam, date=date, filename=fname, mkv_path=meteors_dir / fname,
                meteor_time=lock.get("meteor_time") if isinstance(lock, dict) else None,
                lock_type=(lock.get("lock_type") if isinstance(lock, dict) else None),
                size_bytes=size, video_paths=paths, measure_only=True,
                # Pruned by an earlier run that removed only the .mkv; any
                # .mp4 / details copy still here is a leftover of that decision.
                leftover=pruned and bool(paths),
            )
            if not pruned and paths and sdt is not None:
                clip.measure_only = False
                judged.append((sdt, clip))
            if sdt is not None:
                blocks.append((sdt, clip))
            elif clip.leftover:
                clips.append(clip)
        judged.sort(key=lambda x: x[0])
        # A clip sharing its start with an unmeasured chunk (a stitched clip
        # and its first chunk) sorts last, so the detection lands on the clip.
        blocks.sort(key=lambda x: (x[0], x[1] is not None))
        judged_starts = [s for s, _ in judged]
        block_starts = [s for s, _ in blocks]

        # Brightest mag wins a block with multiple detections, its duration
        # comes with it; multi-station is sticky.
        for d in cam_dets.get(cam, []):
            ci = _assign_detection(d.time, judged_starts, max_gap_s)
            if ci is not None:
                c = judged[ci][1]
                if d.peak_mag is not None and (c.mag is None or d.peak_mag < c.mag):
                    c.mag, c.duration_s = d.peak_mag, d.duration_s
                if d.multistation:
                    c.multistation = True
            bi = _assign_detection(d.time, block_starts, max_gap_s)
            own = blocks[bi][1] if bi is not None else None
            if own is not None and d.peak_mag is not None and (
                    own.own_mag is None or d.peak_mag < own.own_mag):
                own.own_mag, own.own_duration_s = d.peak_mag, d.duration_s

        for _, measured in blocks:
            if measured is None or (measured.measure_only and not (
                    measured.leftover or measured.own_mag is not None)):
                continue
            clips.append(measured)
            if not measured.measure_only:
                stats.scanned_clips += 1

    return clips


# ---------------------------------------------------------------------------
# Main pass
# ---------------------------------------------------------------------------

def run(archive: Path, retention_days: int, bright_pct: float, long_pct: float,
        tol_s: float, corr_window_s: float, apply: bool, max_deletes: int,
        report_path: Path | None, multistation_max_mag: float | None = None,
        include_unlocked: bool = False,
        protected: set[tuple[str, str, str]] | None = None) -> Stats:
    stats = Stats()
    protected = protected or set()
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)

    cam_dirs = sorted(
        d for d in archive.iterdir()
        if d.is_dir() and d.name not in _SKIP_DIRS and not d.name.startswith(".")
    )

    # Collect the union of all date dirs across cameras.
    all_dates: set[str] = set()
    for d in cam_dirs:
        try:
            for sub in d.iterdir():
                if sub.is_dir() and len(sub.name) == _DATE_DIR_LEN and sub.name.isdigit():
                    all_dates.add(sub.name)
        except OSError:
            stats.errors += 1
    dates_sorted = sorted(all_dates)
    logger.info("scanning %d cameras across %d dates in %s",
                len(cam_dirs), len(dates_sorted), archive)

    # Pass 1: enrich every clip.
    all_clips: list[Clip] = []
    for date in dates_sorted:
        stats.scanned_dates += 1
        all_clips.extend(scan_date(cam_dirs, date, tol_s, corr_window_s, stats,
                                   include_unlocked=include_unlocked))

    # Copies left by earlier runs that removed only the .mkv: the prune was
    # already decided, so finish it (not counted against --max-deletes, which
    # throttles new decisions). A manual lock added since then keeps them.
    leftovers = [c for c in all_clips if c.leftover]
    for clip in leftovers:
        if clip.lock_type == "manual" or _clip_key(clip.cam, clip.date, clip.filename) in protected:
            continue
        logger.info("%s leftover %s/%s/%s  %d file(s)  %.1f MB",
                    "APPLY" if apply else "DRY", clip.cam, clip.date, clip.filename,
                    len(clip.video_paths), clip.size_bytes / 1e6)
        if apply and not _delete_videos(clip):
            stats.errors += 1
            continue
        stats.leftovers += 1
        stats.leftover_bytes += clip.size_bytes

    # Fleet-wide percentile thresholds over all ages, and over every clip
    # ever measured: one whose video an earlier run pruned (or that never
    # reached the box) still ranks, so the cut-off does not tighten run by run.
    all_mags = sorted(c.own_mag for c in all_clips if c.own_mag is not None)
    all_durs = sorted(c.own_duration_s for c in all_clips if c.own_duration_s is not None)
    # The rule before counted only clips still on the box. A cut-off is the
    # more lenient of the two, so counting gone clips only ever keeps more.
    judged = [c for c in all_clips if not c.measure_only]
    mags = sorted(c.mag for c in judged if c.mag is not None)
    durs = sorted(c.duration_s for c in judged if c.duration_s is not None)
    # Brightest = smallest magnitudes => keep mag <= P(bright_pct).
    bright_thr = _more_lenient(max, _percentile(all_mags, bright_pct),
                               _percentile(mags, bright_pct))
    # Longest = largest durations => keep duration >= P(100 - long_pct).
    long_thr = _more_lenient(min, _percentile(all_durs, 100.0 - long_pct),
                             _percentile(durs, 100.0 - long_pct))
    stats.resolved_mag = len(mags)
    stats.measured_all_time = len(all_mags)
    logger.info("thresholds: brightest<=%.2f mag (n=%d/%d resolved, %d all-time), "
                "longest>=%.2f s (n=%d, %d all-time)",
                bright_thr if bright_thr is not None else float("nan"),
                len(mags), len(judged), len(all_mags),
                long_thr if long_thr is not None else float("nan"), len(durs),
                len(all_durs))
    if multistation_max_mag is not None:
        logger.info("multi-station retention gated at mag<=%.2f", multistation_max_mag)

    # Pass 2: decide + (optionally) delete. Only clips with video are judged.
    capped = False
    for clip in judged:
        if clip.date_obj >= cutoff:
            stats.kept_recent += 1
            continue

        reasons: list[str] = []
        if clip.lock_type == "manual":
            reasons.append("manual")
        if _clip_key(clip.cam, clip.date, clip.filename) in protected:
            reasons.append("protected")
        if clip.multistation:
            # Keep a multi-station clip's video forever only if it is bright
            # enough (or its magnitude couldn't be measured -- conservative, so
            # we never prune what we can't assess). Faint multi-station clips
            # fall through to the age rule; their stack + analysis are kept
            # regardless, so the trajectory science survives either way.
            if (multistation_max_mag is None
                    or clip.mag is None
                    or clip.mag <= multistation_max_mag):
                reasons.append("multistation")
            else:
                stats.multistation_faint += 1
        # Either measurement qualifies: the window the rule always used, or
        # the clip's own block (what it ranks by among all clips).
        if bright_thr is not None and any(
                m is not None and m <= bright_thr for m in (clip.mag, clip.own_mag)):
            reasons.append("bright")
        if long_thr is not None and any(
                d is not None and d >= long_thr for d in (clip.duration_s, clip.own_duration_s)):
            reasons.append("long")

        if reasons:
            for r in reasons:
                stats.keep_reasons[r] = stats.keep_reasons.get(r, 0) + 1
            if "manual" in reasons:
                stats.kept_manual += 1
            if "multistation" in reasons:
                stats.kept_multistation += 1
            if "bright" in reasons:
                stats.kept_bright += 1
            if "long" in reasons:
                stats.kept_long += 1
            if "protected" in reasons:
                stats.kept_protected += 1
            continue

        # Prune candidate.
        if max_deletes and stats.pruned >= max_deletes:
            capped = True
            continue

        logger.info("%s prune %s/%s/%s  mag=%s dur=%s  %.1f MB",
                    "APPLY" if apply else "DRY", clip.cam, clip.date, clip.filename,
                    clip.mag, clip.duration_s, clip.size_bytes / 1e6)
        if apply and not _prune_clip(clip):
            stats.errors += 1
            continue
        stats.pruned += 1
        stats.pruned_bytes += clip.size_bytes
        cam_tot = stats.per_camera.setdefault(clip.cam, [0, 0])
        cam_tot[0] += 1
        cam_tot[1] += clip.size_bytes

    if capped:
        logger.warning("max-deletes cap (%d) reached -- more clips remain "
                       "eligible; re-run to continue", max_deletes)

    _log_summary(stats, apply, capped, max_deletes)
    if report_path is not None:
        _write_report(report_path, stats, archive, retention_days,
                      bright_thr, long_thr, apply, capped)
    return stats


def _delete_videos(clip: Clip) -> bool:
    """Delete every video file of the clip. True when none is left."""
    ok = True
    for p in clip.video_paths or [clip.mkv_path]:
        try:
            p.unlink()
        except FileNotFoundError:
            pass  # already gone
        except OSError as exc:
            logger.error("failed to delete %s: %s", p, exc)
            ok = False
    return ok


def _prune_clip(clip: Clip) -> bool:
    """Delete the clip's video (MKV, MP4 copy, overlay renders) from the box
    and mark it pruned in state.json. The stack .webp and rms/ are left
    untouched. Returns True on success."""
    if not _delete_videos(clip):
        return False

    # Mark pruned in state.json so future runs skip it and the dashboard can
    # tell the clip's full video was archived-out (stack still served).
    state_path = clip.mkv_path.parent.parent / "state.json"
    try:
        state = json.loads(state_path.read_text())
        chunk = (state.get("chunks") or {}).get(clip.filename)
        if isinstance(chunk, dict):
            chunk["video_pruned"] = True
            chunk["video_pruned_at"] = datetime.now(timezone.utc).isoformat(
                timespec="seconds")
            write_atomic(state_path, json.dumps(state, indent=2),
                         tmp=state_path.with_suffix(".json.tmp"))
    except (OSError, ValueError) as exc:
        logger.warning("deleted %s but could not update state.json: %s",
                       clip.mkv_path, exc)
    return True


def _log_summary(stats: Stats, apply: bool, capped: bool, max_deletes: int) -> None:
    logger.info("---- %s summary ----", "APPLY" if apply else "DRY-RUN")
    logger.info("clips scanned:        %d (%d dates, %d skipped)",
                stats.scanned_clips, stats.scanned_dates, stats.skipped_dates)
    logger.info("magnitude resolved:   %d / %d", stats.resolved_mag,
                stats.scanned_clips)
    logger.info("kept (still recent):  %d", stats.kept_recent)
    logger.info("kept manual:          %d", stats.kept_manual)
    logger.info("kept multi-station:   %d", stats.kept_multistation)
    logger.info("multi-station faint (gated out): %d", stats.multistation_faint)
    logger.info("kept bright:          %d", stats.kept_bright)
    logger.info("kept long:            %d", stats.kept_long)
    logger.info("kept HoF/lock action: %d", stats.kept_protected)
    logger.info("%s:              %d clips, %.2f GB",
                "PRUNED" if apply else "WOULD PRUNE",
                stats.pruned, stats.pruned_bytes / 1e9)
    logger.info("%s:   %d clips, %.2f GB",
                "LEFTOVERS REMOVED" if apply else "WOULD REMOVE LEFTOVERS",
                stats.leftovers, stats.leftover_bytes / 1e9)
    if stats.errors:
        logger.warning("errors:               %d", stats.errors)


def _write_report(path: Path, stats: Stats, archive: Path, retention_days: int,
                  bright_thr: float | None, long_thr: float | None,
                  apply: bool, capped: bool) -> None:
    report = {
        "ran_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "archive": str(archive),
        "applied": apply,
        "capped": capped,
        "retention_days": retention_days,
        "bright_threshold_mag": bright_thr,
        "long_threshold_s": long_thr,
        "scanned_clips": stats.scanned_clips,
        "scanned_dates": stats.scanned_dates,
        "skipped_dates": stats.skipped_dates,
        "kept_recent": stats.kept_recent,
        "kept_manual": stats.kept_manual,
        "kept_multistation": stats.kept_multistation,
        "multistation_faint_gated": stats.multistation_faint,
        "kept_bright": stats.kept_bright,
        "kept_long": stats.kept_long,
        "kept_protected": stats.kept_protected,
        "resolved_mag": stats.resolved_mag,
        "measured_all_time": stats.measured_all_time,
        "pruned": stats.pruned,
        "pruned_gb": round(stats.pruned_bytes / 1e9, 3),
        "leftovers_removed": stats.leftovers,
        "leftovers_gb": round(stats.leftover_bytes / 1e9, 3),
        "per_camera": {cam: {"clips": n, "gb": round(b / 1e9, 3)}
                       for cam, (n, b) in sorted(stats.per_camera.items())},
        "errors": stats.errors,
    }
    try:
        path.write_text(json.dumps(report, indent=2))
        logger.info("report written to %s", path)
    except OSError as exc:
        logger.warning("could not write report to %s: %s", path, exc)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--archive", type=Path, default=Path(DEFAULT_ARCHIVE),
                   help=f"archive root (default {DEFAULT_ARCHIVE})")
    p.add_argument("--retention-days", type=int, default=DEFAULT_RETENTION_DAYS,
                   help=f"prune clips older than N days (default {DEFAULT_RETENTION_DAYS})")
    p.add_argument("--bright-percentile", type=float, default=DEFAULT_BRIGHT_PCT,
                   help=f"keep brightest N%% by magnitude (default {DEFAULT_BRIGHT_PCT})")
    p.add_argument("--long-percentile", type=float, default=DEFAULT_LONG_PCT,
                   help=f"keep longest N%% by duration (default {DEFAULT_LONG_PCT})")
    p.add_argument("--event-tolerance-s", type=float, default=DEFAULT_EVENT_TOLERANCE_S,
                   help="clip<->GMN/RMS time match window (s)")
    p.add_argument("--correlation-window-s", type=float,
                   default=DEFAULT_CORRELATION_WINDOW_S,
                   help="internal multi-station coincidence window (s)")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default: dry-run, delete nothing)")
    p.add_argument("--max-deletes", type=int, default=0,
                   help="cap deletions per run as a throttle (0 = unlimited)")
    p.add_argument("--multistation-max-mag", type=float, default=None,
                   help="keep a multi-station clip's video forever only if its "
                        "peak magnitude is <= this (brighter); fainter ones age "
                        "out. Unset = keep all multi-station (legacy behaviour). "
                        "Unmeasured magnitudes are always kept.")
    p.add_argument("--include-unlocked", action="store_true",
                   help="also judge clips whose night has no lock record on the box "
                        "(archived before archived_lock existed). Run a dry run with "
                        "this before adding it to the timer.")
    p.add_argument("--hof", type=Path, default=Path(DEFAULT_HOF_PATH),
                   help="Hall of Fame JSON; its clips are never pruned")
    p.add_argument("--activity-log", type=Path, default=Path(DEFAULT_ACTIVITY_LOG),
                   help="dashboard activity log; clips with a lock action are never pruned")
    p.add_argument("--kept-clips", type=Path, default=kept_clips.KEPT_CLIPS_PATH,
                   help="clips a signed-in user played (kept_clips.py); never pruned")
    p.add_argument("--report", type=Path, default=None,
                   help="write a JSON summary report to this path")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="log every prune decision at INFO")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # Always show our own INFO summary lines.
    logger.setLevel(logging.INFO)

    if not args.archive.is_dir():
        logger.error("archive path %s is not a directory (is the box mounted?)",
                     args.archive)
        return 2
    run(
        archive=args.archive,
        retention_days=args.retention_days,
        bright_pct=args.bright_percentile,
        long_pct=args.long_percentile,
        tol_s=args.event_tolerance_s,
        corr_window_s=args.correlation_window_s,
        apply=args.apply,
        max_deletes=args.max_deletes,
        report_path=args.report,
        multistation_max_mag=args.multistation_max_mag,
        include_unlocked=args.include_unlocked,
        protected=load_protected(args.hof, args.activity_log, args.kept_clips),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
