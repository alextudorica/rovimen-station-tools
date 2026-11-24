#!/usr/bin/env python3
"""overlay_details.py — Burn per-meteor measurement rows into the bottom bar.

Codename: starprint

Runs AFTER the morning RMS/detection pipeline (dawn_process) has locked and
encoded a night's detection clips. For every confirmed detection it re-renders a
*details* variant of the locked colour clip, GROWING the bottom overlay bar
downward and adding one row per detected meteor with the science summary RMS
produced for it:

  shower (IAU code) · peak magnitude · altitude begin→end · angular velocity ·
  time-of-flight · UTC

A meteor GMN has since triangulated gains a SECOND row underneath its own,
carrying what only a multi-station solution knows: the true (geocentric)
radiant, the geocentric velocity, and the heights it burned between. That row
is absent at dawn — GMN publishes a night 4-15 days late — and appears when the
pass is re-run after publication, which is what makes re-running worth doing.

The meteor rows go INSIDE the bar (below the branding + station rows), never
over the sky. A single clip can hold two or three detections, so the bar grows
by one row per meteor: the sky field (the top 720 px, where the meteor travels)
stays completely clean.

Design
------
- Idempotent / re-runnable. The details clip is a DERIVED file
  (``<stem>.details.mkv``) rendered from the pristine, already-encoded
  ``_color.mkv`` base, which already carries the branding+station bar. This pass
  only PADS the frame downward by :func:`overlay.meteor_rows_extra_height` and
  draws the meteor rows into that new strip — it never rebuilds the branding or
  redraws the sky, so it cannot double-burn. Re-running (e.g. after a better
  calculation) overwrites the derived file with fresh values.
- Geometry is owned by overlay.py. This module imports ``bar_height``,
  ``meteor_rows_extra_height`` and ``build_meteor_row_annotations`` rather than
  re-deriving where the bar ends — the two-week logo-drift regression was caused
  by geometry re-derived in a second place, so there is only one here.
- Detection data is read directly from RMS outputs (FTPdetectinfo + radiants)
  via ``rovimen_rms.session_detections`` — the same canonical FTP↔radiants
  join the on-station detection index already uses, so a meteor gets identical
  measurements here and in the dashboard.
- Only DETECTED meteors get rows. Undetected clips are left as-is: the loop
  walks the locked chunks and the detections that fall inside them; a chunk with
  no matching detection is skipped.

Manual entry point::

    python overlay_details.py --station RO000H --date 20260801 [-c config.json]
    python overlay_details.py --clip /path/to/RO000H_20260801_190512_color.mkv \\
        --station RO000H --date 20260801
    # offline: feed a saved trajectories response instead of asking the VPS
    python overlay_details.py --station RO000H --date 20260801 \\
        --trajectories traj_RO000H_20260801.json

Automatic entry point: dawn_process calls ``run_night(station_id, date, cfg)``
after the detection path (lock + encode + upload) completes.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rovimen_config
import rovimen_night
import rovimen_media
import rovimen_rms

logger = logging.getLogger(__name__)

# Stitched boundary clip: RO000H_20260801_190512_190532_color.mkv
_STITCHED_CHUNK_RE = rovimen_night.STITCHED_CHUNK_RE

DETAILS_SUFFIX = '.details.mkv'
# The details render is a libx264 CRF20 'fast' pass over one meteor clip
# (20-40 s). gmnro16 (Bacău) takes ~87 s per 20 s chunk at 2 threads, so 15
# minutes only ever cuts off a stalled process.
DETAILS_ENCODE_TIMEOUT_S = 900.0

# Column separator between the science fields of one meteor row.
FIELD_SEP = '   '


# ---------------------------------------------------------------------------
# Field formatting — pure, testable
# ---------------------------------------------------------------------------

def _fmt_mag(mag: float | None) -> str | None:
    """Peak apparent magnitude. Lower is brighter; RMS uses absolute-value mags."""
    if mag is None:
        return None
    return f'mag {mag:+.1f}'


def _fmt_shower(shower: str | None) -> str | None:
    """IAU shower code, bare. 'SPO' (sporadic) is a real code and kept verbatim."""
    if not shower:
        return None
    return shower.strip().upper()


def _fmt_alt(elev_beg: float | None, elev_end: float | None) -> str | None:
    """Altitude of the meteor path, begin -> end (degrees above horizon)."""
    if elev_beg is None and elev_end is None:
        return None
    if elev_beg is not None and elev_end is not None:
        return f'ALT {elev_beg:.1f}°→{elev_end:.1f}°'
    single = elev_beg if elev_beg is not None else elev_end
    return f'ALT {single:.1f}°'


def _fmt_velocity(angular_velocity: float | None) -> str | None:
    """Apparent angular velocity across the sky (deg/s)."""
    if angular_velocity is None:
        return None
    return f'{angular_velocity:.1f}°/s'


def _fmt_tof(duration_s: float | None) -> str | None:
    """Time of flight — the visible duration of the meteor (seconds)."""
    if duration_s is None or duration_s <= 0:
        return None
    return f'ToF {duration_s:.2f}s'


def _fmt_geo_velocity(v_km_s: float | None) -> str | None:
    """Geocentric velocity — how fast the meteoroid actually met the Earth.

    Only a triangulated solution has this; the single-station row carries the
    apparent angular rate (:func:`_fmt_velocity`) instead, which is what the
    clip shows rather than what the rock was doing.
    """
    if v_km_s is None or v_km_s <= 0:
        return None
    return f'Vg {v_km_s:.1f} km/s'


def _fmt_radiant(ra_deg: float | None, dec_deg: float | None) -> str | None:
    """True (geocentric) radiant — where in the sky the meteoroid came from."""
    if ra_deg is None or dec_deg is None:
        return None
    return f'RAD {ra_deg:.1f}°,{dec_deg:+.1f}°'


def _fmt_heights(begin_km: float | None, end_km: float | None) -> str | None:
    """Entry and burnout heights of the triangulated path, in km."""
    if begin_km is None and end_km is None:
        return None
    if begin_km is not None and end_km is not None:
        return f'H {begin_km:.0f}→{end_km:.0f} km'
    single = begin_km if begin_km is not None else end_km
    return f'H {single:.0f} km'


def _fmt_station_count(n: int | None) -> str | None:
    """Right-hand marker of the trajectory row: how many cameras solved it."""
    if not n or n < 2:
        return None
    return f'{n} cams'


def _fmt_row_time(dt) -> str | None:
    """Per-meteor UTC clock, ``HH:MM:SSZ``. Static text, not a per-frame clock.

    The details pass runs post-calc so a meteor's begin time is a fixed value;
    it is drawn literally at the right of that meteor's row, mirroring the live
    bar's UTC clock but scoped to the one detection.
    """
    if dt is None:
        return None
    return dt.strftime('%H:%M:%SZ')


def format_detail_lines(detection: dict) -> list[str]:
    """Turn one detection record into the ordered science fields of its row.

    Accepts the merged-row shape emitted by the RMS parse+join (``shower``,
    ``mag_apparent``, ``elev_beg``/``elev_end``, ``angular_velocity``,
    ``duration_s``). Missing values are skipped rather than shown as blanks, so a
    partially-populated detection still yields a clean, shorter row. Order is
    fixed (shower, magnitude, altitude, velocity, ToF) so every row reads the
    same regardless of which fields are present.

    Altitude is where the meteor was in the sky, begin to end, and it is the one
    field a viewer can check against their own memory of the night — it belongs
    on the row alongside the numbers they cannot.

    These are the left-hand fields of one meteor row; the per-row UTC time is
    added on the right by :func:`format_meteor_row`.
    """
    traj = detection.get('trajectory') or {}
    # A triangulated solution identifies the shower from the true radiant, so it
    # supersedes the single-station guess when the two disagree.
    shower = traj.get('shower') or detection.get('shower')
    candidates = [
        _fmt_shower(shower),
        _fmt_mag(detection.get('mag_apparent')),
        _fmt_alt(detection.get('elev_beg'), detection.get('elev_end')),
        _fmt_velocity(detection.get('angular_velocity')),
        _fmt_tof(detection.get('duration_s')),
    ]
    return [line for line in candidates if line]


def format_trajectory_lines(trajectory: dict | None) -> list[str]:
    """Science fields of the multi-station row: what triangulation added.

    A single camera measures where a meteor appeared to go and how fast it
    appeared to move. Only a trajectory solved across two or more cameras knows
    the true radiant, the geocentric velocity and the heights it burned between
    — so those get their own row rather than competing for space with the
    single-station numbers, whose meaning they do not replace.
    """
    if not trajectory:
        return []
    candidates = [
        'GMN',
        _fmt_radiant(trajectory.get('ra_geo_deg'), trajectory.get('dec_geo_deg')),
        _fmt_geo_velocity(trajectory.get('velocity_geo_km_s')),
        _fmt_heights(trajectory.get('altitude_begin_km'), trajectory.get('altitude_end_km')),
    ]
    lines = [line for line in candidates if line]
    # 'GMN' alone is a label with nothing to label.
    return lines if len(lines) > 1 else []


def format_trajectory_row(trajectory: dict | None) -> tuple[str, str] | None:
    """One triangulated solution -> ``(left_text, right_text)`` for its own row."""
    fields = format_trajectory_lines(trajectory)
    if not fields:
        return None
    count = _fmt_station_count((trajectory or {}).get('station_count')) or ''
    return FIELD_SEP.join(fields), count


def format_meteor_row(detection: dict) -> tuple[str, str] | None:
    """One detection -> ``(left_text, right_text)`` for its bar row.

    ``left_text`` joins the science fields (shower · magnitude · velocity · ToF)
    with :data:`FIELD_SEP`; ``right_text`` is that meteor's own UTC time. Returns
    ``None`` when the detection carries nothing worth a row, so the caller can
    skip a fully-empty detection rather than draw a blank line.
    """
    fields = format_detail_lines(detection)
    row_time = _fmt_row_time(_detection_time(detection)) or ''
    if not fields and not row_time:
        return None
    return FIELD_SEP.join(fields), row_time


def format_meteor_rows(detection: dict) -> list[tuple[str, str]]:
    """Every bar row one detection earns: its own, plus a trajectory row if solved.

    A meteor GMN never triangulated (which, since GMN publishes a night 4-15
    days late, is every meteor at dawn) yields exactly one row — the pass is
    unchanged for it. One that was triangulated yields a second row underneath,
    and the bar grows by that row rather than the fields being crammed onto one
    line that would run into the clock at the right.
    """
    rows: list[tuple[str, str]] = []
    row = format_meteor_row(detection)
    if row is not None:
        rows.append(row)
    traj_row = format_trajectory_row(detection.get('trajectory'))
    if traj_row is not None:
        rows.append(traj_row)
    return rows


# ---------------------------------------------------------------------------
# Detection <-> clip mapping
# ---------------------------------------------------------------------------

def _capture_root(cfg: dict) -> Path:
    return rovimen_config.capture_root(cfg)


def _detection_time(detection: dict) -> datetime | None:
    """UTC time of the meteor's first frame, or None.

    ``rovimen_rms.merge_row`` sets ``time_utc`` to the FF block start plus
    first frame / fps (the radiants time for a radiant-only row), the time
    ``detection_lock`` stitches clips by and GMN solves on. The FF filename
    alone is only the start of the 256-frame block, up to 10.24 s before the
    meteor, which put a quarter of meteors in the wrong 20 s clip.
    """
    tu = detection.get('time_utc')
    if not tu:
        return None
    try:
        return datetime.fromisoformat(tu).replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def _load_night_detections(station_id: str, date_str: str, cfg: dict) -> list[dict]:
    """Parse every RMS session dir for this station/night into detection rows.

    ``rovimen_rms.night_detections``: FTPdetectinfo joined to radiants, with
    post-midnight sessions matched by night date, and each session read once.
    A session present in both CapturedFiles and ArchivedFiles (RMS mirrors it
    at the end of the night) used to be read from both, so every meteor in
    it got two rows in the details bar.
    """
    rms_path = cfg.get('stations', {}).get(station_id, {}).get('rms_data_path')
    if not rms_path:
        logger.warning('[%s] No rms_data_path in config', station_id)
        return []
    return rovimen_rms.night_detections(Path(rms_path), station_id, date_str)


def _clip_span(
    clip_name: str, segment_secs: int
) -> tuple[datetime, datetime] | None:
    """Time span ``[start, end]`` a clip covers, or None if the name is unparseable.

    A regular clip spans ``[start, start + segment_secs]``; a stitched clip spans
    ``[start, end]`` from its two-timestamp name (rolled forward a day if the end
    stamp crossed midnight relative to the start).
    """
    start = rovimen_night.chunk_start_utc(clip_name)
    if start is None:
        return None
    ms = _STITCHED_CHUNK_RE.match(clip_name)
    end = rovimen_night.utc_stamp(ms.group(2), ms.group(4)) if ms else None
    if end is None:
        end = start + timedelta(seconds=segment_secs)
    elif end <= start:  # end crossed midnight relative to start
        end += timedelta(days=1)
    return start, end


def match_detections_to_clip(
    clip_name: str,
    detections: list[dict],
    segment_secs: int,
) -> list[dict]:
    """Return EVERY detection whose begin time falls within the clip's span.

    A single clip (regular or stitched) can hold two or three detections; each
    gets its own row in the bar. The result is ordered by begin time so the rows
    read top-to-bottom in the order the meteors flew, matching the clip.
    """
    span = _clip_span(clip_name, segment_secs)
    if span is None:
        return []
    start, end = span
    matches: list[tuple[datetime, dict]] = []
    for det in detections:
        dt = _detection_time(det)
        if dt is not None and start <= dt <= end:
            matches.append((dt, det))
    matches.sort(key=lambda pair: pair[0])
    return [det for _dt, det in matches]


def match_detection_to_clip(
    clip_name: str,
    detections: list[dict],
    segment_secs: int,
) -> dict | None:
    """Return the single brightest detection inside a clip's span, or None.

    Retained for callers (and the CLI ``--clip`` path) that want the headline
    meteor. The full-bar render uses :func:`match_detections_to_clip` to get one
    row per meteor; this picks the lowest apparent magnitude among them.
    """
    matches = match_detections_to_clip(clip_name, detections, segment_secs)
    if not matches:
        return None

    def _brightness(det: dict) -> float:
        mag = det.get('mag_apparent')
        return mag if mag is not None else math.inf

    return min(matches, key=_brightness)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _overlay_cfg(cfg: dict) -> dict:
    overlay_cfg = cfg.get('overlay') or {}
    if not overlay_cfg.get('enabled', True):
        return {}
    return overlay_cfg


def _colour_tag_args(clip: Path) -> list[str]:
    """ffmpeg output options that repeat the base clip's colour tags.

    pad + drawtext stay in YUV, so the pixels pass through unconverted; the
    tags have to follow them or a player may decode the details clip with a
    different matrix than the base clip (adaptive clips are BT.709-tagged).
    Older ffmpeg builds do not carry the tags over on their own.
    """
    return rovimen_media.colour_tag_args(clip, timeout=20)


def _probe_video_height(clip: Path) -> int | None:
    """Height in px of the base clip (720 sky + live bar), or None on failure.

    The details pass pads BELOW this height, so it must know where the base frame
    ends. Read from the container header via ffprobe — no decode.
    """
    return rovimen_media.probe_video_height(clip, timeout=10)


def build_meteor_rows(detections: list[dict]) -> list[tuple[str, str]]:
    """Turn the clip's detections into ``(left, right)`` row pairs, skipping empties.

    A triangulated detection contributes two rows (its own measurements, then
    the multi-station solution), so the bar height follows the number of ROWS,
    not the number of meteors.
    """
    rows: list[tuple[str, str]] = []
    for det in detections:
        rows.extend(format_meteor_rows(det))
    return rows


def render_details_clip(
    clip: Path,
    detections: list[dict] | dict,
    cfg: dict,
    dst: Path | None = None,
) -> Path | None:
    """Render a details variant of ``clip`` growing the bar with one row per meteor.

    ``detections`` is the list of meteors that fall inside this clip (a single
    dict is accepted for the CLI single-meteor path). Reads the pristine encoded
    base clip — which already carries the branding + station bar — PADS the frame
    downward by :func:`overlay.meteor_rows_extra_height`, and draws one meteor row
    per detection into that new strip with a single ffmpeg pass, writing to
    ``<stem>.details.mkv`` (or ``dst``). Overwrites any existing details clip, so
    a re-run after a better calculation refreshes the values.

    The base clip is never modified and the sky is never touched, which is what
    makes the operation idempotent: repeated runs re-derive from the same source
    and re-pad the same amount, never stacking text onto an already-grown bar.
    Returns the output path on success, ``None`` on skip/failure.
    """
    if not clip.exists():
        logger.warning('Source clip missing: %s', clip)
        return None

    if isinstance(detections, dict):
        detections = [detections]

    overlay_cfg = _overlay_cfg(cfg)
    if not overlay_cfg:
        logger.info('Overlay disabled — skipping details for %s', clip.name)
        return None

    rows = build_meteor_rows(detections)
    if not rows:
        logger.info('No usable detail fields for %s — skipping', clip.name)
        return None

    font_path = overlay_cfg.get('font') or ''
    if not font_path or not Path(font_path).is_file():
        logger.info('No font configured for details overlay — skipping %s', clip.name)
        return None

    import overlay

    extra_h = overlay.meteor_rows_extra_height(overlay_cfg, len(rows))
    if extra_h <= 0:
        # Non-cinema style (no bar to grow) or nothing to add — leave the clip.
        logger.info('No bar to extend for %s (style=%s) — skipping',
                    clip.name, overlay_cfg.get('style', 'standard'))
        return None

    base_h = _probe_video_height(clip)
    if base_h is None:
        logger.error('Could not probe height of %s — skipping details', clip.name)
        return None

    row_filters = overlay.build_meteor_row_annotations(
        overlay_cfg, rows, base_h, extra_h,
    )
    if not row_filters:
        logger.info('No meteor-row filters for %s — skipping', clip.name)
        return None

    # Grow the bar: pad the SAME even width, extra_h taller, black, anchored at
    # the top-left so the padding is appended BELOW the existing frame (the sky
    # and the live bar are untouched); then draw the meteor rows into the strip.
    vf = ','.join([f'pad=iw:ih+{extra_h}:0:0:black', *row_filters])

    if dst is None:
        dst = clip.with_name(clip.name[: -len('.mkv')] + DETAILS_SUFFIX)

    # libx264 re-encode: drawtext forces a CPU pass regardless of the base codec.
    fd, tmp_name = tempfile.mkstemp(suffix='.mkv', dir=str(dst.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', str(clip),
        '-vf', vf,
        '-c:v', 'libx264', '-crf', '20', '-preset', 'fast',
        *_colour_tag_args(clip),
        '-an',
        str(tmp),
    ]
    try:
        result = subprocess.run(
            cmd, stderr=subprocess.PIPE, env={**os.environ, 'TZ': 'UTC'},
            timeout=DETAILS_ENCODE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        # A hung disk or decoder must not stall dawn's Phase 1b for ever.
        tmp.unlink(missing_ok=True)
        logger.error('Details render timed out after %.0fs for %s',
                     DETAILS_ENCODE_TIMEOUT_S, clip.name)
        return None
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        tail = result.stderr[-300:].decode('utf-8', errors='replace') if result.stderr else ''
        logger.error('Details render failed for %s: %s', clip.name, tail)
        return None
    os.replace(tmp, dst)
    logger.info('Details clip: %s (%d meteor row(s))', dst.name, len(rows))
    return dst


# ---------------------------------------------------------------------------
# Night driver
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# GMN trajectories — the "after a better calculation" input
# ---------------------------------------------------------------------------

# The station has no route to globalmeteornetwork.org and no reason to grow
# one; the VPS already caches GMN's monthly files for the dashboard. Ask it,
# over the same authenticated channel the pusher uses, and treat any failure as
# "no trajectories" — the pass then renders exactly what it renders today.
TRAJECTORY_FETCH_TIMEOUT_S = 10.0

# GMN publishes a night 4-15 days after it happened (median 14-15 through 2026).
# The dawn pass therefore runs against a night GMN cannot possibly have solved
# yet, and asking would be a guaranteed-empty request every station every
# morning. The fetch waits until the night is at least this old, which means
# dawn skips it on its own and a re-run a fortnight later picks it up -- no flag
# for dawn_process to remember to pass.
GMN_PUBLICATION_LAG_DAYS = 4


def _night_old_enough_for_gmn(date_str: str, now: datetime | None = None) -> bool:
    """True when a night is old enough that GMN might have published it."""
    try:
        night = datetime.strptime(date_str, '%Y%m%d').replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return False
    now = now or datetime.now(timezone.utc)
    return (now - night) >= timedelta(days=GMN_PUBLICATION_LAG_DAYS)


def _trajectory_key(dt) -> str | None:
    """Second-precision UTC key both sides agree on for one meteor."""
    return dt.strftime('%Y-%m-%dT%H:%M:%S') if dt is not None else None


def index_trajectories(rows: list[dict]) -> dict[str, dict]:
    """``[{time_utc, trajectory}]`` -> ``{time_utc: trajectory}``.

    Keys are second-precision, which is all two independent solvers agree on
    for the same meteor's begin time.
    """
    out: dict[str, dict] = {}
    for row in rows or []:
        t = (row or {}).get('time_utc')
        traj = (row or {}).get('trajectory')
        if isinstance(t, str) and isinstance(traj, dict):
            out[t[:19]] = traj
    return out


def load_trajectory_file(path: str | Path) -> dict[str, dict]:
    """Read a trajectories JSON off disk (the offline / manual re-run path).

    Accepts either the endpoint's own response body or the bare list inside it,
    so an operator can save the curl output verbatim.
    """
    try:
        with open(path) as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning('Could not read trajectories file %s: %s', path, exc)
        return {}
    rows = data.get('trajectories') if isinstance(data, dict) else data
    return index_trajectories(rows if isinstance(rows, list) else [])


def fetch_night_trajectories(cam: str, date_str: str, cfg: dict) -> dict[str, dict]:
    """Ask the VPS which of this camera's meteors GMN has since triangulated.

    Station-initiated, authenticated with the station's own ingest key, and
    entirely best-effort: a night younger than GMN's publication lag, an
    unenrolled station, an unreachable VPS or a night GMN has not published yet
    all yield ``{}`` and the pass renders the single-station rows alone.
    """
    import urllib.error
    import urllib.request

    if not _night_old_enough_for_gmn(date_str):
        logger.debug('%s is younger than GMN\'s publication lag — '
                     'skipping trajectory fetch', date_str)
        return {}

    try:
        import rovimen_pusher
    except ImportError:
        logger.debug('rovimen_pusher unavailable — skipping trajectory fetch')
        return {}

    key = rovimen_pusher.resolve_ingest_key()
    base = rovimen_pusher.resolve_base_url(cfg)
    host_key = rovimen_pusher.resolve_host_key(cfg)
    if not (key and base and host_key):
        logger.debug('Push credentials incomplete — skipping trajectory fetch')
        return {}

    url = f'{base}/api/ingest/v1/{host_key}/trajectories/{cam}/{date_str}'
    req = urllib.request.Request(url, headers={'X-Station-Key': key})
    try:
        with urllib.request.urlopen(req, timeout=TRAJECTORY_FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode('utf-8'))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as exc:
        logger.info('Trajectory fetch failed for %s/%s (%s) — '
                    'rendering single-station rows', cam, date_str, exc)
        return {}
    rows = data.get('trajectories') if isinstance(data, dict) else None
    index = index_trajectories(rows if isinstance(rows, list) else [])
    if index:
        logger.info('[%s] %d triangulated meteor(s) for %s', cam, len(index), date_str)
    return index


def attach_trajectories(detections: list[dict], index: dict[str, dict]) -> int:
    """Hang each detection's trajectory on it, in place. Returns the match count.

    Matched on begin time to the second, with a one-second window either side:
    RMS and GMN time the same meteor from different data, and one camera cannot
    retrigger fast enough for that window to reach a different meteor.
    """
    if not index:
        return 0
    matched = 0
    for det in detections:
        dt = _detection_time(det)
        if dt is None:
            continue
        for offset in (0, -1, 1):
            key = _trajectory_key(dt + timedelta(seconds=offset))
            traj = index.get(key) if key else None
            if traj is not None:
                det['trajectory'] = traj
                matched += 1
                break
    return matched


def _locked_clips(station_id: str, date_str: str, cfg: dict) -> list[Path]:
    """Locked (detection) colour clips for a night, from state.json + sidecars."""
    import flags_manager

    night_dir = _capture_root(cfg) / station_id / date_str
    if not night_dir.exists():
        return []
    chunks_state = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    locked: list[Path] = []
    for mkv in sorted(night_dir.glob('*_color.mkv')):
        is_locked = chunks_state.get(mkv.name, {}).get('lock') is not None
        if not is_locked:
            is_locked = Path(str(mkv) + '.locked').exists()
        if is_locked:
            locked.append(mkv)
    return locked


def run_night(
    station_id: str,
    date_str: str,
    cfg: dict,
    trajectories: dict[str, dict] | None = None,
    fetch_trajectories: bool = True,
) -> int:
    """Render detail clips for every locked detection of a station/night.

    Returns the number of detail clips written. Non-detection clips are never
    touched — only clips that are locked AND have a matching RMS detection get a
    details variant. Safe to re-run: each detail clip is regenerated from its
    pristine base.

    ``trajectories`` is a pre-built ``{time_utc: trajectory}`` index (the
    offline / manual path); when omitted and ``fetch_trajectories`` is set, the
    VPS is asked for the night. Either way a triangulated meteor gains a second
    bar row — which is the whole point of the re-run: dawn's clip shows what one
    camera measured, and the same command a fortnight later shows the orbit.
    """
    locked = _locked_clips(station_id, date_str, cfg)
    if not locked:
        logger.info('[%s] No locked clips for %s — nothing to detail', station_id, date_str)
        return 0
    detections = _load_night_detections(station_id, date_str, cfg)
    if not detections:
        logger.info('[%s] No RMS detections parsed for %s', station_id, date_str)
        return 0

    if trajectories is None and fetch_trajectories:
        trajectories = fetch_night_trajectories(station_id, date_str, cfg)
    attach_trajectories(detections, trajectories or {})

    segment_secs = int(cfg.get('segment_duration', 20))
    written = 0
    for clip in locked:
        clip_dets = match_detections_to_clip(clip.name, detections, segment_secs)
        if not clip_dets:
            logger.debug('[%s] No detection matched clip %s — leaving as-is',
                         station_id, clip.name)
            continue
        if render_details_clip(clip, clip_dets, cfg) is not None:
            written += 1
    logger.info('[%s] Details pass: %d/%d locked clip(s) annotated for %s',
                station_id, written, len(locked), date_str)
    return written


def run_clip(station_id: str, date_str: str, clip: Path, cfg: dict,
             trajectories: dict[str, dict] | None = None,
             fetch_trajectories: bool = True) -> bool | None:
    """Render the details clip for one base clip: True if one was written,
    False if rendering failed, None if no detection matched the clip.

    The single-clip form of ``run_night``: process_clip calls it for each clip
    it delivers on a station whose dawn renders no details (meteor clips
    encoded on demand).
    """
    detections = _load_night_detections(station_id, date_str, cfg)
    if trajectories is None and fetch_trajectories:
        trajectories = fetch_night_trajectories(station_id, date_str, cfg)
    attach_trajectories(detections, trajectories or {})
    segment_secs = int(cfg.get('segment_duration', 20))
    clip_dets = match_detections_to_clip(clip.name, detections, segment_secs)
    if not clip_dets:
        logger.info('[%s] No detection matched %s — nothing to burn', station_id, clip.name)
        return None
    return render_details_clip(clip, clip_dets, cfg) is not None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

DEFAULT_CONFIG = Path.home() / 'rovimen_scripts' / 'config.json'


def _load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description='Burn per-meteor detection details into the clip overlay',
    )
    parser.add_argument('-c', '--config', default=str(DEFAULT_CONFIG),
                        help='Path to config.json')
    parser.add_argument('--station', help='Station / camera code (e.g. RO000H)')
    parser.add_argument('--date', help='Night date YYYYMMDD')
    parser.add_argument('--clip', help='Single clip path (requires --station, --date)')
    parser.add_argument('--trajectories', metavar='FILE',
                        help='GMN trajectories JSON (as served by the ingest '
                             'trajectories endpoint); skips the VPS fetch')
    parser.add_argument('--no-gmn', action='store_true',
                        help='Render single-station rows only — do not ask the '
                             'VPS for triangulated solutions')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )

    try:
        cfg = _load_config(args.config)
    except FileNotFoundError:
        logger.error('Config not found: %s', args.config)
        return 1

    def _trajectory_index(cam: str, date_str: str) -> dict[str, dict]:
        if args.trajectories:
            return load_trajectory_file(args.trajectories)
        if args.no_gmn:
            return {}
        return fetch_night_trajectories(cam, date_str, cfg)

    if args.clip:
        if not (args.station and args.date):
            logger.error('--clip requires --station and --date')
            return 2
        written = run_clip(args.station, args.date, Path(args.clip), cfg,
                           trajectories=_trajectory_index(args.station, args.date),
                           fetch_trajectories=False)
        return 1 if written is False else 0

    if not (args.station and args.date):
        logger.error('Provide --station and --date (or --clip with both)')
        return 2

    run_night(
        args.station, args.date, cfg,
        trajectories=load_trajectory_file(args.trajectories) if args.trajectories else None,
        fetch_trajectories=not args.no_gmn,
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
