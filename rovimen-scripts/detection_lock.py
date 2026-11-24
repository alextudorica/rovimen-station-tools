#!/usr/bin/env python3
"""detection_lock.py — End-of-night detection locking and reconciliation.

Codename: makeawish

Parses ArchivedFiles/FTPdetectinfo at end-of-night to lock matching MKV
chunks (confirmed detections) and remove false-positive detection locks.
Manual locks (an operator's, or a fireball alert's) are never removed.

Called by dawn_process.py, or standalone:
    python detection_lock.py -c config.json --station RO000H --date 20260315

Mid-night pass (codename: nightowl)
    The dawn FTPdetectinfo pass above only runs ~10h after the evening was
    captured, so the evening's colour chunks stay UNLOCKED all night.  When a
    small colourcap disk fills, the janitor's pressure eviction takes unlocked
    chunks before locked ones, so an evening meteor's chunk can go before dawn
    locks it.  gmnro16 (Bacău)'s 29 GB USB lost 1341 evening chunks this way at
    01:50 UTC on 20260802, under the date-dir tier that ordered eviction replaced.

    ``midnight_lock_pass`` closes that window.  RMS runs detection in real time
    during capture (StartCapture.QueuedPool over each FF block) but only writes
    FTPdetectinfo at dawn (closePool).  Each finished per-FF job is, however,
    backed up mid-night as an ``rms_queue_bkup_*.pickle`` in the CapturedFiles
    session dir.  We read those tolerantly (no RMS import), lock the colour chunk
    of every FF that already has a meteor, and stop there.  It is ADDITIVE ONLY:
    it never unlocks, never stitches, never touches FTPdetectinfo, so the worst
    case (no detections yet, or unreadable pickles) is a harmless no-op.  The
    dawn EON pass later reconciles these locks with sub-second FTPdetectinfo
    precision and removes any false positives.  Wired as midnight-lock.timer.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import pickle
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

import numpy as np
from PIL import Image

import flags_manager
import rovimen_config
import rovimen_night

logger = logging.getLogger(__name__)

FF_DURATION = 256 / 25.0  # 10.24 s — one FF block at 25 fps

# Chunk names (rovimen_night.CHUNK_RE) and FF names (rovimen_night.FF_NAME_RE)
# carry the same real-world UTC capture time, so an FF block's stamp matches
# the colour video directly with no RTSP-delay offset.

# RMS QueuedPool result backup files live in the CapturedFiles session dir.
_BKUP_GLOB = 'rms_queue_bkup_*.pickle'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_dt(date_s: str, time_s: str) -> datetime:
    return datetime.strptime(f'{date_s}_{time_s}', '%Y%m%d_%H%M%S')


_night_date = rovimen_night.night_of


def _is_detection_lock(lock: object) -> bool:
    """Whether a state.json lock came from detection (real-time, mid-night or
    EON), the only kind FTPdetectinfo can refute.

    A ``manual`` lock is an operator's (``/api/lock``, ``/api/process_clip``)
    or a fireball alert's, which calls process_clip at night, often for a
    fireball RMS did not detect; the EON pass used to remove it as a false
    positive, and the rolling release then deleted the clip before it
    uploaded. A lock without a ``lock_type`` predates the field and counts as
    a detection.
    """
    lock_type = lock.get('lock_type') if isinstance(lock, dict) else None
    return (lock_type or 'detection') == 'detection'


def _chunk_time(mkv: Path, cfg: dict | None = None) -> datetime | None:
    """A chunk's start as naive UTC, comparable with FF block times.

    Honours ``chunk_names_utc: false`` (pre-#851 local-named chunks) through
    rovimen_night.chunk_utc_dt, the conversion station_api already used; on
    every current station names are UTC and this is the plain parse.
    """
    m = rovimen_night.CHUNK_RE.match(mkv.name)
    return rovimen_night.chunk_utc_dt(m.group(2), m.group(3), cfg) if m else None


def _find_chunks_for_time(ff_time: datetime,
                           chunk_dir: Path,
                           segment_secs: int,
                           cfg: dict | None = None) -> list[Path]:
    """Return chunks in chunk_dir that overlap the FF block starting at ff_time."""
    if not chunk_dir.exists():
        return []
    pairs = sorted(
        [(ct, p) for p in chunk_dir.glob('*_color.mkv')
         if (ct := _chunk_time(p, cfg)) is not None],
        key=lambda x: x[0],
    )
    result: list[Path] = []
    for i, (ts, chunk) in enumerate(pairs):
        next_ts = (pairs[i + 1][0] if i + 1 < len(pairs)
                   else ts + timedelta(seconds=segment_secs + 5))
        if ts <= ff_time < next_ts:
            result.append(chunk)
            ff_end = ff_time + timedelta(seconds=FF_DURATION)
            if ff_end > next_ts and i + 1 < len(pairs):
                result.append(pairs[i + 1][1])
            break
    return result


def _parse_ftpdetectinfo(path: Path) -> list[datetime]:
    """Return list of meteor start datetimes from an FTPdetectinfo file."""
    times: list[datetime] = []
    try:
        lines = path.read_text().splitlines()
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith('FF_'):
                parts = line.split('_')
                try:
                    ff_ms = int(parts[4]) / 1000.0
                    ff_base = datetime.strptime(f'{parts[2]}_{parts[3]}', '%Y%m%d_%H%M%S')
                    ff_time = ff_base + timedelta(seconds=ff_ms)
                except (IndexError, ValueError):
                    i += 1
                    continue
                fps = 25.0
                meteor_time = ff_time
                for j in range(i + 1, min(i + 20, len(lines))):
                    seg = lines[j].strip().split()
                    if not seg:
                        continue
                    if lines[j].strip().startswith('FF_') or '---' in lines[j]:
                        break
                    # Detection row: seg[0] is a frame number (float) — must check
                    # before the fps/header check, because detection rows also have
                    # floats at seg[3] (RA) that would otherwise be mistaken for fps.
                    if re.match(r'^\d+\.\d+$', seg[0]):
                        try:
                            meteor_time = ff_time + timedelta(seconds=float(seg[0]) / fps)
                        except ValueError:
                            pass
                        break
                    # Camera header row: non-numeric seg[0], fps at seg[3].
                    if len(seg) >= 4 and re.match(r'^\d+\.\d+$', seg[3]):
                        try:
                            fps = float(seg[3])
                        except ValueError:
                            pass
                times.append(meteor_time)
            i += 1
    except OSError:
        pass
    return times


STITCH_BOUNDARY_SECS = 2.0  # stitch adjacent clips when detection is within 2s of clip edge

# ffmpeg budgets for the boundary stitch.  Every call is a stream copy: an
# extract reads at most one ~21 MB, 20 s chunk and writes ~10 MB to a local
# temp dir, and the concat only reads that temp dir.  gmnro01 (Ghirdoveni) ran
# 83 stitches with zero timeouts in EON passes of 6-30 s, so a stitch takes at
# most seconds and 60 s is more than an order of magnitude of headroom.  A copy that
# still has not finished by then is starved, not slow: on gmnro09 (Sibiciu),
# where capture and RMS share one USB HDD, 19 extracts of RO0013 ran into the
# old 300 s budget on 2026-10-26 and cost 5700 s of a 7522 s EON pass, before
# any encoding.  (The 300 s budget dated from gmnro05 (Bistrița), where a
# timeout escaped and aborted the pass; _run_ffmpeg now contains it.)  Missing
# a stitch costs little -- the raw chunk is locked instead -- so a tight budget
# plus the breaker below is the cheaper failure.
STITCH_EXTRACT_TIMEOUT_S = 60
STITCH_CONCAT_TIMEOUT_S = 60

# After this many ffmpeg timeouts in one camera-night the disk is not going to
# serve the rest of the stitches either: stop stitching for that camera-night
# and lock the raw chunks, as a failed stitch already does.
STITCH_TIMEOUT_BREAKER = 2


@dataclass
class _StitchBreaker:
    """Counts stitch timeouts for one camera-night and opens after the limit."""

    limit: int = STITCH_TIMEOUT_BREAKER
    timeouts: int = 0
    skipped: int = 0

    @property
    def open(self) -> bool:
        return self.timeouts >= self.limit


def _run_ffmpeg(cmd: list[str], timeout: int,
                station_id: str, what: str,
                breaker: _StitchBreaker | None = None) -> subprocess.CompletedProcess | None:
    """Run an ffmpeg step, returning None instead of raising on failure.

    ``subprocess.run(timeout=...)`` RAISES TimeoutExpired; it does not return a
    non-zero result.  That exception used to escape _stitch_clips and
    process_night entirely, so one slow stitch aborted the rest of the night's
    lock pass for that camera: every later detection stayed unlocked, was never
    encoded or uploaded, showed as "clip pending" on the dashboard, and was
    reaped by the janitor once the chunk retention window closed.  A stitch is
    an enhancement over locking the raw chunk -- it must never cost the night.

    stdin is /dev/null (and the commands pass ``-nostdin``): ffmpeg otherwise
    reads the inherited stdin for its interactive keys, and a dawn started
    from a shell can stop on it (SIGTTIN) until the timeout.  A timeout is
    counted on *breaker*.
    """
    try:
        return subprocess.run(cmd, stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if breaker is not None:
            breaker.timeouts += 1
        logger.error('[%s] Stitch: %s timed out after %ds — falling back to '
                     'a plain single-chunk lock', station_id, what, timeout)
    except OSError as e:
        logger.error('[%s] Stitch: %s failed to run: %s', station_id, what, e)
    return None


def _stitch_clips(clip_a: Path, clip_b: Path,
                  station_id: str, date_str: str,
                  chunk_dir: Path, cfg: dict,
                  breaker: _StitchBreaker | None = None) -> Path | None:
    """Stitch the last 10s of clip_a and the first 10s of clip_b into a new clip.

    Used when a detection falls within STITCH_BOUNDARY_SECS of a clip boundary,
    ensuring the full event is captured in a single locked file rather than
    across two separate clips.

    Output filename: {station_id}_{date_str}_{T_start}_{T_end}_color.mkv
      T_start = clip_a.start_time + half_segment  (start of extracted window)
      T_end   = clip_b.start_time + half_segment  (end of extracted window)

    Returns the output Path on success, or None on failure.  Once *breaker*
    is open an already stitched clip is still reused, but no new one is made.
    """
    segment_secs = cfg.get('segment_duration', 20)
    half = segment_secs // 2  # 10s for the default 20s segment

    m_a = rovimen_night.CHUNK_RE.match(clip_a.name)
    m_b = rovimen_night.CHUNK_RE.match(clip_b.name)
    if not m_a or not m_b:
        logger.warning('[%s] Cannot stitch: unexpected filename format (%s, %s)',
                       station_id, clip_a.name, clip_b.name)
        return None

    ts_a = _parse_dt(m_a.group(2), m_a.group(3))
    ts_b = _parse_dt(m_b.group(2), m_b.group(3))

    t_start = (ts_a + timedelta(seconds=half)).strftime('%H%M%S')
    t_end   = (ts_b + timedelta(seconds=half)).strftime('%H%M%S')
    # Use ts_a's calendar date in the filename — regular chunks also embed the
    # calendar date, and encoder._night_date() converts it back to the night
    # date to locate state.json.  Using date_str (night date) here causes
    # _night_date() to subtract an extra day for early-morning clips, pointing
    # to the wrong state.json and breaking the ready/locked lookup.
    cal_date = (ts_a + timedelta(seconds=half)).strftime('%Y%m%d')
    out_name = f'{station_id}_{cal_date}_{t_start}_{t_end}_color.mkv'
    out_path = chunk_dir / out_name

    if out_path.exists():
        logger.info('[%s] Stitched clip already exists: %s', station_id, out_name)
        _stitch_stacks(clip_a, clip_b, out_path, station_id, date_str, cfg)
        return out_path

    if breaker is not None and breaker.open:
        breaker.skipped += 1
        return None

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        part_a = tmp / 'part_a.mkv'
        part_b = tmp / 'part_b.mkv'
        concat_list = tmp / 'concat.txt'
        # Write concat output inside tmp_dir first — avoids ffmpeg muxer errors
        # on non-local filesystems (e.g. NTFS via fuseblk on /mnt/data).
        out_tmp_local = tmp / 'stitched.mkv'
        out_tmp = out_path.with_name(out_path.name + '.tmp')

        # Extract last `half` seconds of clip_a
        r = _run_ffmpeg(
            ['ffmpeg', '-nostdin', '-y', '-ss', str(half), '-i', str(clip_a),
             '-t', str(half), '-c', 'copy', '-avoid_negative_ts', '1', str(part_a)],
            STITCH_EXTRACT_TIMEOUT_S, station_id, f'tail extract of {clip_a.name}',
            breaker,
        )
        if r is None:
            return None
        if r.returncode != 0:
            logger.error('[%s] Stitch: failed extracting tail of %s: %s',
                         station_id, clip_a.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Extract first `half` seconds of clip_b
        r = _run_ffmpeg(
            ['ffmpeg', '-nostdin', '-y', '-t', str(half), '-i', str(clip_b),
             '-c', 'copy', str(part_b)],
            STITCH_EXTRACT_TIMEOUT_S, station_id, f'head extract of {clip_b.name}',
            breaker,
        )
        if r is None:
            return None
        if r.returncode != 0:
            logger.error('[%s] Stitch: failed extracting head of %s: %s',
                         station_id, clip_b.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Concatenate the two parts into local tmp (avoids NTFS muxer issues)
        concat_list.write_text(f"file '{part_a}'\nfile '{part_b}'\n")
        r = _run_ffmpeg(
            ['ffmpeg', '-nostdin', '-y', '-f', 'concat', '-safe', '0',
             '-i', str(concat_list), '-c', 'copy', str(out_tmp_local)],
            STITCH_CONCAT_TIMEOUT_S, station_id,
            f'concat of {clip_a.name} + {clip_b.name}',
            breaker,
        )
        if r is None:
            return None
        if r.returncode != 0:
            logger.error('[%s] Stitch: concat failed for %s + %s: %s',
                         station_id, clip_a.name, clip_b.name,
                         r.stderr[-300:].decode('utf-8', errors='replace'))
            return None

        # Move from local tmp to final destination atomically
        shutil.move(str(out_tmp_local), out_tmp)
        try:
            out_tmp.rename(out_path)
        except OSError as e:
            out_tmp.unlink(missing_ok=True)
            logger.error('[%s] Stitch rename failed: %s', station_id, e)
            return None

    logger.info('[%s] Created stitched clip: %s', station_id, out_name)

    _stitch_stacks(clip_a, clip_b, out_path, station_id, date_str, cfg)
    return out_path


def _overlay_module():
    """Import the sibling ``overlay`` module, or None when it is not installed.

    Imported lazily: detection_lock runs on stations whose bundle predates
    overlay.py, and the stitch path must degrade rather than crash.
    """
    try:
        import overlay
        return overlay
    except Exception:
        return None


def _overlay_bar_height(cfg: dict) -> int:
    """Height of the burned-in data bar for this station, 0 if not determinable."""
    ov = _overlay_module()
    if ov is None or not hasattr(ov, 'bar_height'):
        return 0
    overlay_cfg = (cfg or {}).get('overlay') or {}
    if not overlay_cfg.get('enabled', True):
        return 0
    try:
        return int(ov.bar_height(overlay_cfg))
    except Exception:
        return 0


def _mark_overlaid(path: Path) -> None:
    """Best-effort stamp of the overlaid-still EXIF marker on *path*."""
    ov = _overlay_module()
    if ov is None or not hasattr(ov, 'mark_still_overlaid'):
        return
    try:
        ov.mark_still_overlaid(path)
    except Exception:
        logger.debug('could not mark %s as overlaid', path.name)


def _stitch_clips_safe(clip_a: Path, clip_b: Path,
                       station_id: str, date_str: str,
                       chunk_dir: Path, cfg: dict,
                       breaker: _StitchBreaker | None = None) -> Path | None:
    """_stitch_clips with a hard guarantee that it never raises.

    The stitch is best-effort: when it fails the caller locks the raw chunk
    instead, which still captures the meteor (split across two files at worst).
    Letting an exception out costs every REMAINING detection of the night, so
    the blast radius of a failure must stop here.  _stitch_clips already
    handles the failures it knows about; this catches the rest (a full disk on
    the move, an unreadable stack, a PIL blow-up).
    """
    try:
        return _stitch_clips(clip_a, clip_b, station_id, date_str, chunk_dir, cfg,
                             breaker=breaker)
    except Exception:
        logger.exception('[%s] Stitch of %s + %s failed — falling back to a '
                         'plain single-chunk lock',
                         station_id, clip_a.name, clip_b.name)
        return None


def _stitch_stacks(clip_a: Path, clip_b: Path, stitched_clip: Path,
                   station_id: str, date_str: str, cfg: dict) -> None:
    """Create a max-pixel merged stack for a stitched clip.

    Combines the two source chunk stacks into one stack matching the
    stitched clip's filename, so the archive has a stack for every
    detection clip.
    """
    stacks_dir = clip_a.parent / 'stacks'
    if not stacks_dir.exists():
        return

    stack_a = stacks_dir / clip_a.name.replace('_color.mkv', '_stack.webp')
    stack_b = stacks_dir / clip_b.name.replace('_color.mkv', '_stack.webp')
    out_name = stitched_clip.name.replace('_color.mkv', '_stack.webp')
    out_path = stacks_dir / out_name

    # An empty file is a merge that was killed mid-save, not a stack.
    if out_path.exists() and out_path.stat().st_size > 0:
        return

    if not stack_a.exists() or not stack_b.exists():
        logger.warning('[%s] Cannot stitch stacks: missing %s or %s',
                       station_id, stack_a.name, stack_b.name)
        return

    try:
        a = np.array(Image.open(stack_a))
        b = np.array(Image.open(stack_b))
        if a.shape != b.shape:
            logger.warning('[%s] Cannot stitch stacks: shape mismatch %s vs %s',
                           station_id, a.shape, b.shape)
            return

        # Both sources already carry the burned-in data bar, so a plain
        # max-combine of the whole frame max-blends the two bars on top of each
        # other: two timestamps, two logos and two station rows overprinted into
        # unreadable mush. Merge the SKY only (that is where the meteor is) and
        # take the bar verbatim from the earlier chunk, whose timestamp is the
        # stitched clip's start and matches the first stamp in its filename.
        bar_h = _overlay_bar_height(cfg)
        sky_h = a.shape[0] - bar_h
        if bar_h > 0 and sky_h > 0:
            merged = a.copy()
            merged[:sky_h] = np.maximum(a[:sky_h], b[:sky_h])
        else:
            merged = np.maximum(a, b)

        # Stamp the overlaid-still marker. Without it the dashboard's stack gate
        # reads the merge as un-branded and burns a SECOND bar underneath the one
        # already in the pixels -- a 976px-tall image with two strips, which is
        # what shipped for every stitched stack until now. PIL's save() writes no
        # EXIF, so the marker has to be re-applied here explicitly.
        #
        # Written to a scratch file and renamed into place: color_capture
        # SIGTERMs a dawn_process still running when capture closes a chunk,
        # and a save into out_path itself left a 0-byte stack that the
        # exists() check above then took as done (gmnro07 (București) and
        # gmnro04 (Vâlcele), 2026-09-30 to 10-05).
        ov = _overlay_module()
        if ov is not None and hasattr(ov, 'atomic_still_write'):
            with ov.atomic_still_write(out_path) as tmp:
                Image.fromarray(merged).save(tmp, 'WEBP', quality=95, method=4)
                _mark_overlaid(tmp)
        else:
            Image.fromarray(merged).save(out_path, 'WEBP', quality=95, method=4)
            _mark_overlaid(out_path)

        flags_manager.mark_stacked(station_id, date_str, stitched_clip.name, cfg)
        logger.info('[%s] Created stitched stack: %s', station_id, out_name)
    except Exception:
        logger.exception('[%s] Failed to stitch stacks for %s', station_id, out_name)


# ---------------------------------------------------------------------------
# Mid-night additive lock pass (nightowl)
# ---------------------------------------------------------------------------

# Modules whose classes we allow the unpickler to actually resolve.  Everything
# else (including RMS classes and any reduce/exec gadget) is replaced with an
# inert stub — see _TolerantUnpickler.
_PICKLE_SAFE_MODULES = frozenset({'builtins', 'collections', 'numpy', 'numpy.core.multiarray'})


class _TolerantUnpickler(pickle.Unpickler):
    """Restricted unpickler: resolves only safe data classes, stubs the rest.

    RMS ``rms_queue_bkup_*.pickle`` files hold a ``BackupContainer`` whose
    ``inputs`` list embeds an RMS ``Config`` object.  We only read the FF name (a
    str) and whether the meteor list is non-empty, so neither the Config nor the
    BackupContainer class needs to truly resolve.

    These pickles are RMS's own local output, written by the same station user
    under the station's RMS_data dir — not an untrusted external feed.  Even so,
    ``find_class`` is allowlisted to a handful of pure-data modules and stubs
    everything else, so a ``__reduce__``/exec gadget in a tampered pickle cannot
    resolve a callable (e.g. ``os.system``) and therefore cannot execute.  The
    stub also lets the file load when RMS is not importable in this interpreter.
    """

    def find_class(self, module: str, name: str):  # noqa: D401
        if module in _PICKLE_SAFE_MODULES:
            try:
                return super().find_class(module, name)
            except Exception:
                pass

        class _Stub:
            def __init__(self, *a, **k):
                pass

            def __setstate__(self, state):
                if isinstance(state, dict):
                    self.__dict__.update(state)

        return _Stub


def _ff_meteors_from_backup(pkl: Path) -> tuple[str, bool] | None:
    """Return (ff_name, has_meteor) from one RMS result backup, or None.

    ``BackupContainer.outputs`` is ``(ff_name, star_list, meteor_list)`` as
    returned by ``detectStarsAndMeteors``; a non-empty ``meteor_list`` means RMS
    already found a meteor on that FF block.  Falls back to ``inputs[1]`` for the
    FF name.  Never raises — a corrupt or half-written pickle just yields None.
    """
    try:
        data = pkl.read_bytes()
    except OSError:
        return None
    try:
        obj = _TolerantUnpickler(io.BytesIO(data)).load()
    except Exception:
        # Half-written (capture is still appending) or an unexpected format.
        return None

    outputs = getattr(obj, 'outputs', None)
    inputs = getattr(obj, 'inputs', None)

    ff_name: str | None = None
    if isinstance(outputs, (list, tuple)) and outputs and isinstance(outputs[0], str):
        ff_name = outputs[0]
    elif isinstance(inputs, (list, tuple)) and len(inputs) >= 2 and isinstance(inputs[1], str):
        ff_name = inputs[1]
    if not ff_name or not rovimen_night.FF_NAME_RE.match(ff_name):
        return None

    has_meteor = False
    if isinstance(outputs, (list, tuple)) and len(outputs) >= 3:
        try:
            has_meteor = bool(len(outputs[2]))
        except TypeError:
            has_meteor = bool(outputs[2])
    return ff_name, has_meteor


def midnight_lock_pass(station_id: str, date_str: str, cfg: dict) -> int:
    """Additively lock colour chunks of detections found so far tonight.

    Runs periodically THROUGH the night (midnight-lock.timer) so a meteor's
    colour chunk is locked hours before the janitor's pressure eviction could
    delete it — closing the ~10h window between capture and the dawn EON pass.

    Reads RMS's real-time per-FF result backups (rms_queue_bkup_*.pickle in the
    CapturedFiles session dir), locks the colour chunk overlapping every FF that
    already has a meteor, and does nothing else.  ADDITIVE ONLY: it never
    unlocks, never stitches, never writes FTPdetectinfo.  The dawn EON pass
    (process_night) later reconciles these locks against the finalised
    FTPdetectinfo, upgrading their timestamps and removing false positives.

    Returns the number of chunks it newly locked (0 = harmless no-op).
    """
    dl_cfg = cfg.get('services', {}).get('detection_lock') or {}
    if not dl_cfg.get('enabled', True):
        return 0
    # Separate opt-out for just the mid-night pass (dawn EON locking stays on).
    if not dl_cfg.get('midnight_enabled', True):
        return 0

    try:
        rms_data = Path(cfg['stations'][station_id]['rms_data_path'])
    except KeyError:
        logger.warning('[%s] No rms_data_path in config — skipping midnight lock', station_id)
        return 0
    segment_secs = cfg.get('segment_duration', 20)
    videocapture_path = rovimen_config.capture_root(cfg)

    # In-progress RMS session dirs for tonight live under CapturedFiles (the
    # ArchivedFiles mirror only appears at dawn).  Convert each dir's timestamp
    # to a night date so post-midnight sessions match (same logic as the EON
    # pass).  We deliberately read ONLY CapturedFiles: that is where RMS points
    # QueuedPool.backup_dir during capture.
    session_dirs: list[Path] = []
    base = rms_data / 'CapturedFiles'
    try:
        entries = list(base.iterdir())
    except OSError:
        logger.debug('[%s] No CapturedFiles under %s — skipping midnight lock',
                     station_id, rms_data)
        return 0
    for d in entries:
        if (d.is_dir()
                and (m := rovimen_night.SESSION_DIR_RE.match(d.name))
                and m.group(1) == station_id
                and _night_date(m.group(2), m.group(3)) == date_str):
            session_dirs.append(d)
    if not session_dirs:
        return 0

    # Collect FF blocks that already have a meteor.  A pickle is written per FF
    # block regardless of detection, so most yield has_meteor=False.
    meteor_ff_times: list[datetime] = []
    for sess in session_dirs:
        for pkl in sess.glob(_BKUP_GLOB):
            res = _ff_meteors_from_backup(pkl)
            if res is None:
                continue
            ff_name, has_meteor = res
            if not has_meteor:
                continue
            ff_time = rovimen_night.rms_frame_time(ff_name)
            if ff_time is not None:
                meteor_ff_times.append(ff_time)

    if not meteor_ff_times:
        return 0

    chunk_dir = videocapture_path / station_id / date_str
    if not chunk_dir.exists():
        return 0

    # Lock the colour chunk(s) overlapping each detected FF block.  lock_chunk is
    # idempotent at the state level, so re-running the pass never double-locks.
    already = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    newly_locked = 0
    for ff_time in meteor_ff_times:
        for mkv in _find_chunks_for_time(ff_time, chunk_dir, segment_secs, cfg):
            if (already.get(mkv.name) or {}).get('lock') is not None:
                continue
            lock_info = {
                'lock_type': 'detection',
                'detection_time': ff_time.strftime('%Y%m%d_%H%M%S'),
                'meteor_time': ff_time.isoformat(),
            }
            try:
                flags_manager.lock_chunk(station_id, date_str, mkv.name, lock_info, cfg)
                # Reflect the write locally so a second FF in the same chunk
                # within this pass is not counted or re-locked.
                already.setdefault(mkv.name, {})['lock'] = lock_info
                newly_locked += 1
                logger.info('[%s] Midnight lock: %s', station_id, mkv.name)
            except Exception:
                logger.exception('[%s] Midnight lock failed for %s', station_id, mkv.name)

    if newly_locked:
        logger.info('[%s] Midnight lock pass: %d chunk(s) newly locked for %s '
                    '(from %d detected FF block(s))',
                    station_id, newly_locked, date_str, len(meteor_ff_times))
    return newly_locked


# The EON pass writes its confirmed locks in batches: one state.json rewrite
# per EON_FLUSH_EVERY relocks, or sooner once EON_FLUSH_INTERVAL_S has passed,
# so a pass killed mid-way (color_capture SIGTERMs a dawn still running when
# capture starts) keeps most of its work.  Unlocks wait for the final write,
# because a chunk is a false positive only once every detection has been seen.
EON_FLUSH_EVERY = 50
EON_FLUSH_INTERVAL_S = 120.0


class _LockBatch:
    """Pending EON relocks for one camera-night, written in batches.

    Crash safety: an interrupted pass leaves only correct relocks of
    confirmed chunks on disk, its false positives still locked, and
    ``eon_done`` unset, so dawn reruns the pass and redoes the rest.
    """

    def __init__(self, station_id: str, date_str: str, cfg: dict,
                 clock: Callable[[], float] | None = None) -> None:
        self.station_id = station_id
        self.date_str = date_str
        self.cfg = cfg
        self._clock = clock or time.monotonic
        self._relocks: dict[str, dict] = {}
        self._last_flush = self._clock()

    def relock(self, chunk_name: str, lock_info: dict) -> None:
        self._relocks[chunk_name] = lock_info
        if (len(self._relocks) >= EON_FLUSH_EVERY
                or self._clock() - self._last_flush >= EON_FLUSH_INTERVAL_S):
            self.flush()

    def flush(self, unlocks: Iterable[str] = ()) -> bool:
        relocks, self._relocks = self._relocks, {}
        self._last_flush = self._clock()
        try:
            return flags_manager.apply_lock_changes(
                self.station_id, self.date_str, self.cfg,
                relocks=relocks, unlocks=unlocks)
        except Exception:
            logger.exception('[%s] Failed to write %d lock change(s) to state.json',
                             self.station_id, len(relocks))
            return False


def process_night(station_id: str, date_str: str, cfg: dict) -> bool:
    """EON FTPdetectinfo pass — confirm confirmed detections, remove false positives.

    makeawish: post-night FTPdetectinfo reconciliation pass.

    Returns True when the reconciliation actually ran (an FTPdetectinfo was
    found and locks were confirmed against it), False when it was skipped —
    callers use this to retry the pass on a later run instead of treating the
    night as processed.
    """
    scfg = cfg['stations'][station_id]
    if not scfg.get('rms', True) or not scfg.get('rms_data_path'):
        # Colour/spectro-only camera (rms: false) — no RMS ever runs, so
        # there is nothing to reconcile and nothing to retry later.
        logger.info('[%s] No RMS on this camera — skipping EON pass', station_id)
        return False
    rms_data = Path(scfg['rms_data_path'])
    segment_secs = cfg.get('segment_duration', 20)
    videocapture_path = rovimen_config.capture_root(cfg)

    # Match RMS session dirs whose observation night equals date_str.
    # Sessions that started after midnight are filed under the next calendar
    # date (e.g. RO000W_20260412_005904_* belongs to night 20260411), so a
    # plain glob on date_str misses them.  Use _night_date() to convert each
    # directory's timestamp to a night date before comparing.
    #
    # RMS writes FTPdetectinfo into the CapturedFiles session dir when
    # detection completes and only later mirrors it to ArchivedFiles.  dawn's
    # RMS-finished gate accepts either location, so this pass must too —
    # reading only ArchivedFiles opened a race where dawn started in the gap,
    # locked nothing, and the whole night showed "clip pending" until the
    # janitor reaped the unlocked chunks.  ArchivedFiles wins when a session
    # exists in both (it holds the final, filtered copy).
    night_dirs, listed_any = rovimen_night.scan_rms_sessions(rms_data, station_id, date_str)
    if not listed_any:
        logger.error('[%s] Cannot list RMS data dirs under %s — skipping EON pass',
                     station_id, rms_data)
        return False
    if not night_dirs:
        logger.warning('[%s] No RMS session dir for %s — skipping confirm pass',
                       station_id, date_str)
        return False

    ftpdets: list[Path] = []
    for _name, night_dir in sorted(night_dirs.items()):
        ftpdets.extend(rovimen_night.ftpdetect_files(night_dir))
    if not ftpdets:
        # Session dir exists but detection output isn't written yet — retry
        # later rather than treating an in-flight night as "no detections"
        # (which would strip its real-time locks as false positives).
        logger.warning('[%s] No FTPdetectinfo for %s yet — skipping confirm pass',
                       station_id, date_str)
        return False

    confirmed_times: list[datetime] = []
    for ftpdet in ftpdets:
        confirmed_times.extend(_parse_ftpdetectinfo(ftpdet))

    logger.info('[%s] %d confirmed detection(s) for %s', station_id,
                len(confirmed_times), date_str)

    chunk_dir = videocapture_path / station_id / date_str
    if not chunk_dir.exists():
        return False

    all_chunks = sorted(
        [(ct, p) for p in chunk_dir.glob('*_color.mkv')
         if (ct := _chunk_time(p, cfg)) is not None],
        key=lambda x: x[0],
    )

    batch = _LockBatch(station_id, date_str, cfg)
    breaker = _StitchBreaker()

    def _lock(mkv: Path, lock_info: dict) -> None:
        if mkv in confirmed_chunks:
            return
        confirmed_chunks.add(mkv)
        batch.relock(mkv.name, lock_info)
        logger.info('[%s] Confirmed lock: %s', station_id, mkv.name)

    confirmed_chunks: set[Path] = set()
    for ft in confirmed_times:
        # FTPdetectinfo times are real-world frame timestamps that match the color
        # video directly — match chunks on ft with no RTSP delay offset.
        # (rtsp_capture_delay_s is relevant only for real-time event-driven locking.)
        for idx, (chunk_ts, mkv) in enumerate(all_chunks):
            chunk_end = chunk_ts + timedelta(seconds=segment_secs)
            if not (chunk_ts <= ft <= chunk_end):
                continue

            video_pos = (ft - chunk_ts).total_seconds()
            lock_info = {
                'lock_type': 'detection',
                'detection_time': ft.strftime('%Y%m%d_%H%M%S'),
                'meteor_time': ft.isoformat(),
            }

            # Boundary stitch: detection within 1s of a clip edge → stitch the two
            # adjacent clips and lock the resulting file instead of either original.
            # Falls back to a normal single-clip lock if stitching fails.
            stitched = False
            if video_pos < STITCH_BOUNDARY_SECS and idx > 0:
                stitched_path = _stitch_clips_safe(
                    all_chunks[idx - 1][1], mkv,
                    station_id, date_str, chunk_dir, cfg, breaker=breaker,
                )
                if stitched_path:
                    batch.relock(stitched_path.name, lock_info)
                    confirmed_chunks.add(stitched_path)
                    logger.info('[%s] Boundary stitch (pre): %s + %s → %s',
                                station_id, all_chunks[idx - 1][1].name,
                                mkv.name, stitched_path.name)
                    stitched = True

            elif video_pos > segment_secs - STITCH_BOUNDARY_SECS and idx + 1 < len(all_chunks):
                stitched_path = _stitch_clips_safe(
                    mkv, all_chunks[idx + 1][1],
                    station_id, date_str, chunk_dir, cfg, breaker=breaker,
                )
                if stitched_path:
                    batch.relock(stitched_path.name, lock_info)
                    confirmed_chunks.add(stitched_path)
                    logger.info('[%s] Boundary stitch (post): %s + %s → %s',
                                station_id, mkv.name,
                                all_chunks[idx + 1][1].name, stitched_path.name)
                    stitched = True

            if not stitched:
                _lock(mkv, lock_info)

            break

    # Remove false-positive real-time locks, in the same write as the last
    # relocks.  Relocks touch only confirmed chunks, so reading the state
    # before that write sees every unconfirmed chunk as it will be.
    false_positives: list[str] = []
    sstate = flags_manager.load(station_id, date_str, cfg)
    for _, mkv in all_chunks:
        if mkv in confirmed_chunks:
            continue
        lock = sstate.get('chunks', {}).get(mkv.name, {}).get('lock')
        if lock is not None:
            if _is_detection_lock(lock):
                false_positives.append(mkv.name)
            continue
        # Also check legacy .locked sidecar for backward compat
        if Path(str(mkv) + '.locked').exists():
            false_positives.append(mkv.name)
    removed = 0
    if batch.flush(unlocks=false_positives):
        removed = len(false_positives)
        for name in false_positives:
            logger.info('[%s] Removed false-positive lock: %s', station_id, name)

    if breaker.open:
        logger.warning('[%s] Stitch breaker: %d ffmpeg timeout(s) for %s, so %d later '
                       'boundary detection(s) were locked as raw chunks, unstitched',
                       station_id, breaker.timeouts, date_str, breaker.skipped)
    logger.info('[%s] EON lock pass: %d confirmed, %d false positives removed for %s',
                station_id, len(confirmed_chunks), removed, date_str)
    return True


def _tonight_date() -> str:
    """UTC night date for the in-progress night.

    Before local noon UTC the observing night is dated yesterday (RMS files a
    post-midnight session under the next calendar day); at/after noon a new
    night has begun.  Uses UTC so it is correct on RTC-less/mis-zoned hosts.
    """
    return rovimen_night.current_night()


def _run_midnight_all(cfg: dict, date_str: str) -> int:
    """Run the additive midnight lock pass for every configured station."""
    total = 0
    for station_id in cfg.get('stations', {}):
        try:
            total += midnight_lock_pass(station_id, date_str, cfg)
        except Exception:
            logger.exception('[%s] Midnight lock pass raised', station_id)
    logger.info('Midnight lock pass complete for %s: %d chunk(s) locked across %d station(s)',
                date_str, total, len(cfg.get('stations', {})))
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description='EON FTPdetectinfo lock reconciliation')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--station', help='Station ID (EON pass; all stations for --midnight)')
    parser.add_argument('--date', help='Night date YYYYMMDD (default: tonight for --midnight)')
    parser.add_argument('--midnight', action='store_true',
                        help='Run the additive mid-night lock pass instead of the EON pass')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    if args.midnight:
        date_str = args.date or _tonight_date()
        if args.station:
            midnight_lock_pass(args.station, date_str, cfg)
        else:
            _run_midnight_all(cfg, date_str)
        return 0

    if not args.station or not args.date:
        parser.error('--station and --date are required for the EON pass')
    process_night(args.station, args.date, cfg)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
