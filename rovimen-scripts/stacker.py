#!/usr/bin/env python3
"""stacker.py — Per-chunk maxpixel stack builder.

Codename: palinopsia

Reads each 20-second MKV chunk via ffmpeg rawvideo pipe, computes the
per-pixel maximum (and mean) across all frames, colours it, applies the OSD
overlay, saves a WebP stack image, its avg frame and thumbnail, and flags the
chunk ``stacked`` in state.json.

Decoding is always software (CPU).  VAAPI decode was benchmarked as slower
than software on the i5-8500 (i965 driver) due to GPU↔CPU transfer overhead,
and has been removed.  VAAPI is used only by the encoder (h264_vaapi encode).

Called by the morning processing sequence (process_night) or standalone:
    python stacker.py --chunk /path/to/chunk.mkv -c config.json
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Callable

if __name__ == '__main__':
    # color_capture runs this file once per 20-second chunk, and nothing on the
    # per-chunk path calls BLAS. At its default, numpy's OpenBLAS starts a
    # busy-waiting thread per core on import: ~0.5 CPU-s of pure spin per chunk
    # on an 8-core station that is already short of CPU. Set for the CLI only,
    # before numpy loads; a process importing this module keeps its own BLAS.
    os.environ.setdefault('OPENBLAS_NUM_THREADS', '1')

import numpy as np  # noqa: E402  (after the BLAS guard above)
from PIL import Image  # noqa: E402

import overlay  # noqa: E402
import flags_manager  # noqa: E402
import rovimen_config  # noqa: E402
import rovimen_media  # noqa: E402
import rovimen_lock  # noqa: E402
import rovimen_night  # noqa: E402
import rovimen_rms  # noqa: E402

logger = logging.getLogger(__name__)

# Serialises flags_manager read-modify-write when multiple workers run in parallel.
_flags_lock = threading.Lock()

WIDTH = rovimen_media.CAPTURE_WIDTH
HEIGHT = rovimen_media.CAPTURE_HEIGHT

# Per-chunk stack locks: <night>/.stack_locks/<chunk>.lock, shared with
# rovimen_media.chunk_lock(mkv, 'stack') (see there for the contract).
_STACK_LOCK_DIR = rovimen_media.lock_dir_name('stack')


def chunk_stack_lock(mkv: Path, timeout: float = 0.0):
    """Yield True while this process owns the right to stack *mkv*.

    ``rovimen_media.chunk_lock(mkv, 'stack')``: color_capture's per-chunk
    stacker and dawn's night pass both stack chunks, and the second to arrive
    stands down (False) instead of decoding the chunk twice.
    """
    return rovimen_media.chunk_lock(mkv, 'stack', timeout)


def _sweep_stale_locks(night_dir: Path) -> int:
    """Remove the night's stack lock files nobody holds; return how many went."""
    return rovimen_media.sweep_stale_locks(night_dir, 'stack')


def _lock_wait_seconds(cfg: dict) -> float:
    """Seconds a stacker waits for another stacker's chunk lock before skipping."""
    try:
        return max(0.0, float(cfg.get('stack_lock_wait_sec', 0) or 0))
    except (TypeError, ValueError):
        return 0.0


_FFPROBE_TIMEOUT_S = 20


def _probe_video_height(mkv: Path) -> int:
    """Return the encoded video height. Falls back to HEIGHT on error.

    A raw 1280x720 chunk answers from its Matroska header in well under a
    millisecond; it used to cost an ffprobe process (~0.1 s) per chunk. Anything
    else — an encoded chunk with its OSD bar, an unusual size, a header the
    reader cannot parse — still goes to ffprobe, which stays the reference.
    The ffprobe is bounded: a hung disk (the UAS hang on gmnro03 (Ciocârlia))
    must not stall the stacker before its decode even starts.
    """
    if rovimen_media.mkv_frame_size(mkv) == (WIDTH, HEIGHT):
        return HEIGHT
    height = rovimen_media.probe_video_height(mkv, timeout=_FFPROBE_TIMEOUT_S)
    return HEIGHT if height is None else height


# The frame-passthrough flag this box's ffmpeg knows (``-fps_mode`` from 5.1,
# ``-vsync passthrough`` on the 4.2 of gmnro05 (Bistrița) and gmnro08 (Brașov)).
passthrough_args = rovimen_media.passthrough_args


def _decode_args(bar_h: int, threads: int = 0,
                 full_range: bool = False) -> tuple[list[str], list[str]]:
    """Return (pre_input_args, vf_args) for the ffmpeg software decode command.

    threads=0 lets ffmpeg choose automatically.

    ``full_range`` forces full-range YUV interpretation for cameras that flag
    full-range data as limited. The encoder already does this when deriving
    clip gains (``color_calibration.sample_avg_frame``); without the same
    prefix here the stack would derive its gains from a differently-scaled
    reference than the clip and the two would disagree on colour.
    """
    chain = []
    if full_range:
        chain.append(rovimen_media.FULL_RANGE_SCALE)
    if bar_h > 0:
        chain.append(f'crop=in_w:in_h-{bar_h}:0:0')
    vf = ['-vf', ','.join(chain)] if chain else []
    # Emit exactly the coded frames.  Some stations write chunks whose container
    # timebase implies a nominal 1000 fps; ffmpeg's default CFR output then
    # duplicates every frame ~40x, which costs 200s+ per chunk instead of 6s.
    return ['-threads', str(threads)], vf + passthrough_args()


def _pedestal(frame: np.ndarray, pct: float) -> np.ndarray:
    """Per-channel background pedestal of *frame*: its *pct* percentile, uint8 (3,).

    Every 4th pixel on both axes (~1/16 of them) keeps the percentile at ~1 ms
    for a 720p frame. A percentile of uint8 data lies in 0..255, so the uint8
    cast is exact after the truncation to an integer level.
    """
    level = np.percentile(frame[::4, ::4], pct, axis=(0, 1))
    return level.astype(np.int16).astype(np.uint8)


def _subtract_pedestal(frames: np.ndarray, pedestal: np.ndarray) -> np.ndarray:
    """Subtract *pedestal* from every frame of *frames* in place, clamped at 0.

    ``clip(frame - bg, 0, 255)`` is exactly ``max(frame, bg) - bg`` in uint8.
    The pedestal is tiled along a whole row so numpy's loops run 3840 wide
    instead of 3: the int16 form this replaces cost ~11 ms a frame, more than
    decoding it. Returns *frames*.
    """
    n, h, w, ch = frames.shape
    rows = frames.reshape(n, h, w * ch)
    if not np.shares_memory(rows, frames):
        np.maximum(frames, pedestal, out=frames)
        np.subtract(frames, pedestal, out=frames)
        return frames
    row = np.tile(pedestal, w)
    np.maximum(rows, row, out=rows)
    np.subtract(rows, row, out=rows)
    return frames


# A 64 KB pipe moves a 2.7 MB frame in ~42 reads; 1 MiB (the unprivileged
# maximum by default) cut ~0.2 CPU-s a chunk. Linux only; F_SETPIPE_SZ is
# missing from Python's fcntl before 3.10.
_PIPE_BYTES = 1 << 20
_F_SETPIPE_SZ = getattr(fcntl, 'F_SETPIPE_SZ', 1031)


def _widen_pipe(stream: BinaryIO | None) -> None:
    """Grow the decode pipe to 1 MiB where the OS allows; otherwise leave it."""
    try:
        fcntl.fcntl(stream.fileno(), _F_SETPIPE_SZ, _PIPE_BYTES)
    except (OSError, ValueError, AttributeError):
        pass


def _read_into(stream: BinaryIO, buf: bytearray) -> int:
    """Fill *buf* from *stream*; return the byte count, which is short only at EOF."""
    view = memoryview(buf)
    got = 0
    while got < len(buf):
        n = stream.readinto(view[got:])
        if not n:
            break
        got += n
    return got


# What a decode's stderr is kept to in the log: its last lines, which name the
# damage (``error while decoding MB ...``) without flooding the stacker log.
_STDERR_TAIL_LINES = 3
_STDERR_READ_CAP = 1 << 20


def _stderr_sink() -> BinaryIO | int:
    """Where a decode's stderr goes: an unlinked temp file, else /dev/null.

    Never a pipe. The decode loop reads only stdout, and a damaged chunk makes
    ffmpeg print a line per broken macroblock even at ``-loglevel error``; once
    that passed the 64 KB pipe buffer, ffmpeg blocked on its stderr, the loop
    blocked on stdout, and the stacker hung until something killed it (at dawn,
    nothing did before the 13:15 hard stop). A file cannot fill up that way.
    """
    try:
        return tempfile.TemporaryFile(prefix='rovimen_stack_err_')
    except OSError:
        return subprocess.DEVNULL


def _close_quietly(sink: BinaryIO | int) -> None:
    if not isinstance(sink, int):
        try:
            sink.close()
        except OSError:
            pass


def _stderr_summary(sink: BinaryIO | int) -> tuple[int, str]:
    """(line count, last lines) of what ffmpeg wrote to *sink*."""
    if isinstance(sink, int):
        return 0, ''
    try:
        sink.seek(0)
        data = sink.read(_STDERR_READ_CAP)
    except (OSError, ValueError):
        return 0, ''
    if not data:
        return 0, ''
    lines = data.decode('utf-8', 'replace').strip().splitlines()
    return len(lines), ' | '.join(lines[-_STDERR_TAIL_LINES:])


def _report_decode(mkv: Path, rc: int | None, n_frames: int, sink: BinaryIO | int) -> None:
    """Log what the decode of *mkv* complained about, then close *sink*."""
    try:
        count, tail = _stderr_summary(sink)
    finally:
        _close_quietly(sink)
    if rc not in (0, None) or n_frames == 0:
        logger.warning('Decode of %s ended rc=%s after %d frame(s)%s', mkv.name, rc, n_frames,
                       f': {tail}' if tail else '')
    elif count:
        # ffmpeg conceals broken macroblocks and still exits 0; the stack is
        # usable, but how often chunks arrive damaged is worth seeing.
        logger.info('Decode of %s reported %d error line(s): %s', mkv.name, count, tail)


# How long one chunk decode may take before it counts as stalled (a hung USB
# disk, a stream ffmpeg never finishes): ten times the clip's length, never
# under five minutes, because the stacker runs at nice 19 / ionice idle beside
# RMS capture and can legitimately crawl.
_DECODE_TIMEOUT_PER_CLIP_S = 10
_DECODE_TIMEOUT_FLOOR_S = 300


def _decode_timeout_s(mkv: Path, segment_s: float) -> float:
    """The stall limit for decoding *mkv*: a splice spans its own start..end,
    any other chunk one capture segment (``segment_duration``)."""
    try:
        length = float(segment_s or 20)
    except (TypeError, ValueError):
        length = 20.0
    name = rovimen_night.parse_chunk_name(mkv.name)
    if name is not None and name.end_time:
        start = datetime.strptime(name.time, '%H%M%S')
        end = datetime.strptime(name.end_time, '%H%M%S')
        length = (end - start).total_seconds() % 86400 or length
    return max(_DECODE_TIMEOUT_FLOOR_S, _DECODE_TIMEOUT_PER_CLIP_S * length)


def _compute_maxpixel(
    mkv: Path,
    bar_h: int = 0,
    threads: int = 0,
    bg_subtract_pct: float | None = None,
    full_range: bool = False,
    timeout_s: float | None = None,
) -> tuple[np.ndarray | None, np.ndarray | None, np.ndarray | None, int]:
    """Compute per-pixel max *and* per-pixel mean across all frames.

    Returns ``(maxpx, avgpx, refpx, n)``. The avg frame averages out meteors
    and transient lights, unlike maxpx.

    ``refpx`` is the white-balance reference: the same mean *without*
    ``bg_subtract_pct`` applied. Subtracting the background pedestal moves the
    sky toward black, and a grey-world derivation on that is measuring the
    residual, not the sky — while the clip derives its gains from the
    untouched frames. ``refpx`` is None when no pedestal was subtracted
    (``avgpx`` is then already the right reference).

    If ``bar_h > 0``, the encoded video is HEIGHT+bar_h rows tall (annotation
    bar at bottom). ffmpeg crops the bar before outputting raw bytes so the
    numpy buffer always receives exactly WIDTH * HEIGHT * 3 bytes per frame.

    If ``bg_subtract_pct`` is set, the frames have their Nth-percentile
    per-channel background pedestal (one per 10-frame batch) subtracted before
    both accumulators.  This is a display-only adjustment for light-polluted
    sites (e.g. Bortle 8-9).

    A decode still running after ``timeout_s`` (default: ``_decode_timeout_s``
    of a 20 s chunk) is killed and counts as failed, so a stalled ffmpeg cannot
    block the stacker, or a Process click waiting behind it, forever.
    """
    if timeout_s is None:
        timeout_s = _decode_timeout_s(mkv, 20)
    pre, vf = _decode_args(bar_h, threads, full_range)
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        *pre,
        '-i', str(mkv),
        *vf,
        '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1',
    ]
    stderr_log = _stderr_sink()
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=stderr_log)
    except FileNotFoundError:
        _close_quietly(stderr_log)
        logger.error('ffmpeg not found')
        return None, None, None, 0
    _widen_pipe(proc.stdout)

    frame_shape = (HEIGHT, WIDTH, 3)
    frame_bytes = WIDTH * HEIGHT * 3
    batch_frames = 10
    # One batch buffer, refilled in place: a fresh 27 MB ``bytes`` per read cost
    # a page fault every 4 KB, fifty times a chunk.
    buf = bytearray(frame_bytes * batch_frames)
    batch = np.frombuffer(buf, dtype=np.uint8).reshape(batch_frames, *frame_shape)
    maxpx = np.zeros(frame_shape, dtype=np.uint8)
    # Integer sums: a batch fits uint16 (10 x 255) and a whole chunk uint32. The
    # float32 sums this replaces held the same integers exactly (every partial
    # sum stays below 2**24), so the means are bit-identical; casting each frame
    # to float32 was just the costliest step of the loop.
    avg_total = np.zeros(frame_shape, dtype=np.uint32)
    ref_total = (np.zeros(frame_shape, dtype=np.uint32)
                 if bg_subtract_pct is not None else None)
    batch_max = np.empty(frame_shape, dtype=np.uint8)
    batch_sum = np.empty(frame_shape, dtype=np.uint16)
    n = 0
    stalled = threading.Event()

    def _kill_stalled() -> None:
        stalled.set()
        try:
            proc.kill()
        except OSError:
            pass

    watchdog = threading.Timer(timeout_s, _kill_stalled)
    watchdog.daemon = True
    watchdog.start()
    try:
        while True:
            got = _read_into(proc.stdout, buf)
            frames = got // frame_bytes
            if frames == 0:
                break
            arr = batch[:frames]
            if bg_subtract_pct is not None:
                # The white-balance reference keeps the untouched frames; both
                # accumulators get the pedestal-subtracted ones. One pedestal
                # per 10-frame batch (0.4 s of sky), measured on its middle
                # frame: per frame it cost ~1.3 s a chunk on gmnro07 (București).
                np.add.reduce(arr, axis=0, dtype=np.uint16, out=batch_sum)
                ref_total += batch_sum
                arr = _subtract_pedestal(arr, _pedestal(arr[frames // 2], bg_subtract_pct))
            np.maximum.reduce(arr, axis=0, out=batch_max)
            np.maximum(maxpx, batch_max, out=maxpx)
            np.add.reduce(arr, axis=0, dtype=np.uint16, out=batch_sum)
            avg_total += batch_sum
            n += frames
            if got < len(buf):
                break  # a short read means ffmpeg closed the pipe
    finally:
        watchdog.cancel()
        proc.stdout.close()
        try:
            rc = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait()
            logger.warning('ffmpeg hung during maxpixel — killed: %s', mkv.name)
        _report_decode(mkv, rc, n, stderr_log)

    if stalled.is_set():
        logger.warning('ffmpeg stalled decoding %s: killed after %.0f s (%d frame(s) read)',
                       mkv.name, timeout_s, n)
        return None, None, None, 0
    if n == 0:
        return None, None, None, 0
    avgpx = (avg_total.astype(np.float32) / n).clip(0, 255).astype(np.uint8)
    refpx = (None if ref_total is None
             else (ref_total.astype(np.float32) / n).clip(0, 255).astype(np.uint8))
    return maxpx, avgpx, refpx, n


def _save_webp_lossless(image: np.ndarray, path: Path) -> None:
    # The scratch name used to be a fixed ``<stem>.tmp.webp``, which every
    # concurrent writer of the same stack shared — one would rename it out from
    # under the other. ``atomic_still_write`` hands out a private one.
    try:
        with overlay.atomic_still_write(path) as tmp:
            Image.fromarray(image, 'RGB').save(str(tmp), 'webp', lossless=True)
    except Exception as exc:
        raise RuntimeError(f'failed writing stack image: {exc}') from exc


THUMB_WIDTH = 480


def _save_thumbnail(image: Image.Image, dst: Path) -> None:
    """Write the 480 px dashboard thumbnail of the stack held in memory.

    This used to be an ffmpeg process per chunk (``scale=480:-2 -quality 80``)
    that decoded the stack just written; Pillow scales the pixels already in
    hand, with the same size rule and encoder settings.
    """
    try:
        w, h = image.size
        # scale=480:-2 — the aspect-preserving height, rounded to an even number.
        thumb_h = max(2, (THUMB_WIDTH * h + w) // (2 * w) * 2)
        thumb = image.resize((THUMB_WIDTH, thumb_h), Image.BICUBIC)
        # A 0-byte render is caught by atomic_still_write, which refuses to
        # publish an empty thumbnail.
        with overlay.atomic_still_write(dst) as tmp:
            thumb.save(tmp, 'WEBP', quality=80)
    except Exception as e:
        logger.warning('Thumbnail failed for %s: %s', dst.name, e)


def _publish_stack(
    webp: Path,
    maxpx: np.ndarray,
    overlay_cfg: dict | None,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> Image.Image:
    """Write the chunk's stack to *webp* once; return the image written.

    The stack used to be saved as a lossless WebP, then decoded again by an
    ffmpeg overlay pass that replaced it with the annotated copy, so every chunk
    paid for a lossless encode nobody kept. The overlay is now rendered from the
    pixels in memory and the annotated stack published in a single atomic
    write, byte for byte what the old sequence left behind. When there is
    nothing to draw, or the overlay fails, the plain lossless stack is written
    instead — the same image the old sequence left in those cases.
    """
    still = Image.fromarray(maxpx, 'RGB')
    if overlay_cfg:
        try:
            rendered = overlay.render_still(still, overlay_cfg, station_id,
                                            station_cfg, chunk_epoch)
            if rendered is not None:
                overlay.save_overlaid_webp(rendered, webp)
                return rendered
        except Exception as e:
            # The un-annotated stack written below is still a usable image, and
            # the dashboard's overlay gate brands it on the way out.
            logger.warning('[%s] Overlay failed for %s — stack left un-annotated: %s',
                           station_id, webp, e)
    _save_webp_lossless(maxpx, webp)
    return still


def colour_stack(
    maxpx: np.ndarray,
    avgpx: np.ndarray | None,
    refpx: np.ndarray | None,
    *,
    cfg: dict,
    station_cfg: dict,
    is_encoded: bool,
    when: datetime | None = None,
    label: str = '',
) -> tuple[np.ndarray, np.ndarray | None]:
    """Colour (and turn) a chunk's maxpixel and avg frame; touches no disk.

    Returns ``(maxpx, avgpx)`` in the colour the stack is published in. The
    avg frame always gets the maxpixel's transform, so dawn's ``max - avg``
    meteor composite (build_night_color_meteor_stack) works in one colour space
    instead of picking up the calibration difference as spurious colour.
    """
    import color_calibration as cc

    if is_encoded:
        # The dawn re-encode already coloured (and rotated) this chunk. Fitting
        # a curve on it again used to colour it twice, which a stacker lagging
        # behind the re-encode would do to whole nights. Its stack keeps the
        # clip's colour as it is.
        return maxpx, avgpx

    rot = bool(station_cfg.get('rotate', False))
    if cc.adaptive_enabled(station_cfg, 'stack'):
        # Sky match, fitted on the stack itself so its sky lands on the same
        # target as the clip's frames (a maxpixel sky sits above any single
        # frame's, so a curve borrowed from the avg frame would leave it bright).
        curve = cc.resolve_adaptive_curve(
            maxpx, station_cfg, 'stack',
            location=rovimen_config.station_location(cfg, station_cfg), when=when,
        )
        if curve is not None:
            logger.debug('adaptive colour %s: %s', label, curve.describe())
            maxpx = cc.apply_adaptive_np(maxpx, curve, rotate=rot)
            if avgpx is not None:
                avgpx = cc.apply_adaptive_np(avgpx, curve, rotate=rot)
        elif rot:
            maxpx = np.rot90(maxpx, k=2)
            if avgpx is not None:
                avgpx = np.rot90(avgpx, k=2)
        return maxpx, avgpx

    # Legacy opt-out: white balance toward the fleet's night-sky colour,
    # derived from the chunk's own avg frame (masks out transient lights;
    # falls back to maxpx if no avg was provided). refpx is avgpx without the
    # background-subtraction pedestal; it is the frame the clip's own gains
    # were derived from, so using it keeps the two in the same colour space on
    # pedestal-subtracted stations.
    ref = refpx if refpx is not None else (avgpx if avgpx is not None else maxpx)
    gr, gg, gb, gamma = cc.resolve_calibration_from_frame(ref, station_cfg, 'stack')
    # The full tone-curve set (contrast + highlight knees), not just gains and
    # gamma: the encoder applies ``color_post.contrast`` to the clip, so the
    # stack must apply the same value or the two diverge.
    post = cc.resolve_color_post(station_cfg, 'stack')
    tone = {
        'contrast': float(post['contrast']),
        'knee_low': float(post['knee_low']),
        'knee_high': float(post['knee_high']),
        'vibrance': float(post['vibrance']),
    }
    maxpx = cc.apply_calibration_np(maxpx, gr, gg, gb, gamma, rotate=rot, **tone)
    if avgpx is not None:
        avgpx = cc.apply_calibration_np(avgpx, gr, gg, gb, gamma, rotate=rot, **tone)
    return maxpx, avgpx


def publish_chunk_stack(
    mkv: Path,
    maxpx: np.ndarray,
    avgpx: np.ndarray | None,
    station_id: str,
    night_str: str,
    date_str: str,
    time_str: str,
    cfg: dict,
    station_cfg: dict,
) -> bool:
    """Write a coloured stack, its avg frame and thumbnail; flag the chunk stacked.

    Returns True only when a non-empty stack image is on disk. ``stacked`` is
    the flag that stops every later pass from rebuilding the chunk, so it is
    set from the file that actually exists rather than from having reached the
    end of this function — a failed overlay used to be discarded silently and
    the chunk was flagged anyway, which is how five 0-byte stacks survived on
    a station until an operator noticed.
    """
    overlay_cfg = cfg.get('overlay')
    overlay_enabled = overlay_cfg.get('enabled', True) if overlay_cfg else True

    stacks_dir = rovimen_config.capture_root(cfg) / station_id / night_str / 'stacks'
    thumbs_dir = stacks_dir / 'thumbs'
    stacks_dir.mkdir(parents=True, exist_ok=True)

    ts = datetime.strptime(f'{date_str}_{time_str}', '%Y%m%d_%H%M%S')
    stem = _stack_stem(mkv)
    webp = stacks_dir / f'{stem}.webp'

    published = _publish_stack(
        webp, maxpx, overlay_cfg if overlay_enabled else None, station_id, station_cfg,
        int(ts.replace(tzinfo=timezone.utc).timestamp()),
    )

    # The chunk's avepixel, so dawn can build a night-level colour meteor
    # stack via ``max - avg`` per chunk, max-combined across the night. Lossy
    # WebP is fine (never displayed, only subtracted): ~50 KB per chunk vs
    # ~500 KB lossless, ~10 MB/cam/night saved.
    if avgpx is not None:
        avg_webp = stacks_dir / f'{stem}_avg.webp'
        try:
            with overlay.atomic_still_write(avg_webp) as tmp_avg:
                Image.fromarray(avgpx, mode='RGB').save(
                    tmp_avg, format='WEBP', quality=85, method=4,
                )
        except Exception as e:
            logger.warning('[%s] avg save failed for %s: %s', station_id, avg_webp.name, e)

    # Always rewritten: a restack (after colour tuning, or the regeneration
    # recipe in known failure mode 31) used to keep the old thumbnail, and the
    # night stack is built from the thumbnails.
    _save_thumbnail(published, thumbs_dir / webp.name)

    try:
        written = webp.stat().st_size
    except OSError:
        written = 0
    if written == 0:
        logger.warning('[%s] Stack output missing or empty for %s — not marking stacked',
                       station_id, webp)
        return False

    with _flags_lock:
        flags_manager.mark_stacked(station_id, night_str, mkv.name, cfg)
    return True


def _finish_stack(
    mkv: Path,
    maxpx: np.ndarray,
    avgpx: np.ndarray | None,
    station_id: str,
    night_str: str,
    date_str: str,
    time_str: str,
    cfg: dict,
    is_encoded: bool,
    refpx: np.ndarray | None = None,
) -> bool:
    """Colour a chunk's decoded stack and publish it (see colour_stack, publish_chunk_stack)."""
    station_cfg = cfg.get('stations', {}).get(station_id, {})
    station_cfg = rovimen_rms.enrich_station_cfg_pointing(station_id, station_cfg, cfg)
    maxpx, avgpx = colour_stack(
        maxpx, avgpx, refpx, cfg=cfg, station_cfg=station_cfg, is_encoded=is_encoded,
        when=rovimen_night.chunk_midpoint_utc(date_str, time_str), label=f'[{station_id}] {mkv.name}',
    )
    return publish_chunk_stack(mkv, maxpx, avgpx, station_id, night_str, date_str,
                               time_str, cfg, station_cfg)


def _chunk_state(mkv: Path, station_id: str, night_str: str, cfg: dict) -> dict:
    with _flags_lock:
        return flags_manager.load(station_id, night_str, cfg).get('chunks', {}).get(mkv.name, {})


def _stack_stem(mkv: Path) -> str:
    """``<clip stem>_stack``: the stack is named after its clip, so a boundary
    splice's stack keeps both of the splice's timestamps."""
    return mkv.name[:-len('_color.mkv')] + '_stack'


def _is_stacked(mkv: Path, chunk_state: dict) -> bool:
    """True when *mkv* is flagged stacked (state or legacy sidecar) and its
    stack is not an empty file. A writer killed mid-save leaves 0 bytes behind
    (dawn's splice stacks did, 2026-09-30 to 10-05), and the upload can never
    send it, so an empty stack is rebuilt rather than taken as done."""
    if not (chunk_state.get('stacked', False) or Path(str(mkv) + '.stacked').exists()):
        return False
    try:
        return (mkv.parent / 'stacks' / f'{_stack_stem(mkv)}.webp').stat().st_size > 0
    except OSError:
        return True   # flagged, and no file to judge (uploaded and cleaned up)


def _chunk_encoding(mkv: Path, chunk_state: dict, station_id: str) -> tuple[bool, int]:
    """(is_encoded, bar_h) for one chunk: has the re-encode already coloured it?

    An encoded chunk was coloured and rotated by the encoder, so the stack
    must take it as it is. That used
    to be guessed from the frame height alone (a chunk taller than HEIGHT
    carries the OSD bar), which is wrong both ways: an encode with the overlay
    off or its font missing is 720 rows tall, and a chunk whose state was lost
    still has its bar. The state's ``reencoded`` flag decides now. It is also
    set when the encoder kept the capture because no encode came out smaller
    (and for preserved spectroscope sources), so it counts as "encoded" only
    together with ``kept_source: false``, which the encoder records since
    2026-09-26. Where the state cannot answer (older state, no
    ``kept_source``) or contradicts the frame, the height guess stands, with a
    warning when the two disagree. The bar is cropped whatever the answer.
    """
    bar_h = max(0, _probe_video_height(mkv) - HEIGHT)
    reencoded = bool(chunk_state.get('reencoded'))
    if 'kept_source' in chunk_state:
        encoded = reencoded and not chunk_state.get('kept_source')
        if encoded or not bar_h:
            return encoded, bar_h
        logger.warning('[%s] %s carries a %d-row OSD bar but its state says it was not '
                       're-encoded; treating it as encoded', station_id, mkv.name, bar_h)
        return True, bar_h
    if bar_h and not reencoded:
        logger.warning('[%s] %s carries a %d-row OSD bar but is not flagged re-encoded '
                       '(state lost?); treating it as encoded', station_id, mkv.name, bar_h)
    return bar_h > 0, bar_h


@dataclass(frozen=True, eq=False)
class NightInputs:
    """Decode inputs a night pass loads once and shares across its chunks."""

    bg_subtract_pct: float | None


def _night_inputs(station_id: str, night_str: str, cfg: dict) -> NightInputs:
    display_levels = cfg.get('display_levels') or {}
    return NightInputs(display_levels.get('background_subtract_pct'))


def process_chunk(
    mkv: Path,
    cfg: dict,
    *,
    ffmpeg_threads: int | None = None,
    inputs: NightInputs | None = None,
) -> bool:
    """Stack one chunk and flag it stacked in state.json. Idempotent.

    The one entry point for both callers. color_capture runs it through the
    CLI once per closed segment, with the defaults: the realtime thread plan
    (see decode_threads) and the chunk's night inputs loaded here. The night
    pass passes its per-worker thread count and the inputs it loaded once for
    the night, and its routine skips (chunks not ready yet) log at debug.

    Returns True when the chunk is stacked (now or already), False when it was
    skipped or failed; a skipped chunk is left for the next pass.
    """
    # palinopsia: per-chunk maxpixel accumulator
    night_pass = inputs is not None
    # A capture chunk or a boundary splice. detection_lock merges a splice's
    # stack from its two chunks' stacks; when that merge could not run (a
    # source stack missing, the writer killed), the splice is stacked from its
    # own video here like any chunk.
    name = rovimen_night.parse_chunk_name(mkv.name)
    if name is None:
        if not night_pass:
            logger.warning('Skipping — filename does not match pattern: %s', mkv.name)
        return False
    station_id, date_str, time_str = name.camera, name.date, name.time
    night_str = name.night

    chunk_state = _chunk_state(mkv, station_id, night_str, cfg)
    if _is_stacked(mkv, chunk_state):
        return True
    if not chunk_state.get('ready', False):
        (logger.debug if night_pass else logger.warning)(
            'Skipping — chunk not ready: %s', mkv.name)
        return False
    if not mkv.exists() or mkv.stat().st_size == 0:
        logger.warning('[%s] Skipping empty/missing MKV: %s', station_id, mkv.name)
        return False

    # Dawn encodes locked/detection clips in place ahead of stacking. Crop the
    # OSD bar so _compute_maxpixel receives exactly HEIGHT rows (otherwise it
    # ladders down the maxpixel), and take an encoded chunk as it is: no second
    # colour curve, no pedestal.
    is_encoded, bar_h = _chunk_encoding(mkv, chunk_state, station_id)
    if is_encoded:
        bg_pct = None
    else:
        if inputs is None:
            inputs = _night_inputs(station_id, night_str, cfg)
        bg_pct = inputs.bg_subtract_pct

    with chunk_stack_lock(mkv, _lock_wait_seconds(cfg)) as owned:
        if not owned:
            logger.info('[%s] Another stacker holds %s — skipping (next pass picks it up)',
                        station_id, mkv.name)
            return False
        # The lock may have been waited for, or won just after another stacker
        # finished this chunk: decoding it again would be pure waste.
        if _is_stacked(mkv, _chunk_state(mkv, station_id, night_str, cfg)):
            return True

        t0 = time.monotonic()
        if ffmpeg_threads is None:
            ffmpeg_threads = decode_threads(cfg)
        maxpx, avgpx, refpx, n_frames = _compute_maxpixel(
            mkv, bar_h=bar_h, threads=ffmpeg_threads,
            bg_subtract_pct=bg_pct,
            full_range=bool(cfg.get('full_range_fix', False)),
            timeout_s=_decode_timeout_s(mkv, cfg.get('segment_duration', 20)))
        if maxpx is None:
            logger.warning('[%s] Maxpixel failed (0 frames): %s', station_id, mkv.name)
            return False

        try:
            if not _finish_stack(mkv, maxpx, avgpx, station_id, night_str, date_str,
                                 time_str, cfg, is_encoded=is_encoded, refpx=refpx):
                return False
        except Exception:
            logger.exception('[%s] Post-process failed for %s', station_id, mkv.name)
            return False

    logger.info('[%s] Stacked %s  frames=%d  wall=%.2fs',
                station_id, mkv.name, n_frames, time.monotonic() - t0)
    return True


# The realtime lane's decode threads: color_capture runs one or two stackers at
# a time beside RMS capture, and ffmpeg's automatic count (a thread per core)
# cost ~45% more CPU per chunk than one thread for the same wall time on
# gmnro01 (Ghirdoveni).
_REALTIME_DECODE_THREADS = 2


def decode_threads(cfg: dict, night_workers: int | None = None) -> int:
    """ffmpeg ``-threads`` for one chunk decode (0 lets ffmpeg choose).

    ``stacker_ffmpeg_threads`` sets it: an integer applies everywhere (0 is
    ffmpeg's own choice, the old realtime behaviour); ``"auto"`` (the default)
    gives the realtime CLI 2, and a night pass with *night_workers* workers
    its share of the cores, ``cpu // (workers * dawn_stack_parallelism)``:
    dawn stacks ``dawn_stack_parallelism`` cameras at once (default
    ``cpu // 3``, as dawn computes it), each with ``stack_workers`` decodes,
    and handing every decode ``cpu // workers`` threads ran 16 decode threads
    on 8 cores on gmnro01 (Ghirdoveni).
    """
    setting = cfg.get('stacker_ffmpeg_threads', 'auto')
    if setting not in (None, 'auto'):
        try:
            return max(0, int(setting))
        except (TypeError, ValueError):
            logger.warning('stacker_ffmpeg_threads %r is not an integer or "auto"; '
                           'using auto', setting)
    if night_workers is None:
        return _REALTIME_DECODE_THREADS
    try:
        cpu = int(cfg.get('_cpu_cores') or os.cpu_count() or 4)
    except (TypeError, ValueError):
        cpu = os.cpu_count() or 4
    try:
        parallelism = int(cfg.get('dawn_stack_parallelism', cpu // 3))
    except (TypeError, ValueError):
        parallelism = 1
    return max(1, cpu // (max(1, night_workers) * max(1, parallelism)))


def _stack_night_perfile(
    station_id: str,
    pending: list[Path],
    chunks: list[Path],
    date_str: str,
    cfg: dict,
    workers: int,
    should_abort: Callable[[], bool] | None = None,
    on_stacked: Callable[[], None] | None = None,
) -> int:
    """Stack via parallel per-file workers.

    Each worker runs its own ffmpeg process on a single chunk.
    Returns count of successfully stacked chunks.

    ``should_abort`` is polled once per chunk, before its ffmpeg starts, so a
    caller with a deadline stops at a chunk boundary rather than mid-decode.
    Every stack is written atomically, so the chunks already done stay done and
    the next run picks up where this one stopped.
    """
    ffmpeg_threads = decode_threads(cfg, night_workers=workers)
    inputs = _night_inputs(station_id, date_str, cfg)

    logger.info(
        '[%s] Stacking %d/%d chunks for %s '
        '(mode=perfile, workers=%d, threads/worker=%d, decode=software)',
        station_id, len(pending), len(chunks), date_str,
        workers, ffmpeg_threads,
    )

    def _one(mkv: Path) -> bool:
        if should_abort is not None and should_abort():
            return False
        try:
            return process_chunk(mkv, cfg, ffmpeg_threads=ffmpeg_threads, inputs=inputs)
        finally:
            if on_stacked is not None:
                on_stacked()

    ok = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_one, mkv): mkv for mkv in pending}
        for fut in as_completed(futures):
            try:
                if fut.result():
                    ok += 1
            except Exception:
                logger.exception('[%s] Worker failed', station_id)
    return ok


def stack_scope(cfg: dict) -> str:
    """``services.stacker.scope``: which chunks the night pass stacks.

    ``"all"`` (default): every chunk, which the timelapse and the night
    composite need. ``"detections"``: only locked chunks. A CPU-starved station
    sets it when stacking every chunk does not fit its dawn: gmnro05 (Bistrița)
    spent 7.4 h stacking 5 x ~2,230 chunks on 2026-09-27 and hit the 13:15 UTC
    deadline. A chunk delivered on demand (process_clip) is stacked then.
    """
    st = (cfg.get('services') or {}).get('stacker') or {}
    scope = st.get('scope', 'all') if isinstance(st, dict) else 'all'
    return 'detections' if scope == 'detections' else 'all'


def pending_chunks(station_id: str, date_str: str, cfg: dict,
                   scope: str | None = None) -> list[Path]:
    """The night's chunks the stack pass would still stack, in time order.

    ``scope`` overrides ``services.stacker.scope`` for this call: dawn passes
    ``"detections"`` for a camera whose full night does not fit before the
    deadline.
    """
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.is_dir():
        return []
    chunk_states = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    pending = [mkv for mkv in sorted(night_dir.glob('*_color.mkv'))
               if not _is_stacked(mkv, chunk_states.get(mkv.name, {}))]
    if (scope or stack_scope(cfg)) == 'detections':
        pending = [mkv for mkv in pending
                   if rovimen_lock.chunk_is_locked(mkv, chunk_states.get(mkv.name, {}))]
    return pending


def process_night(station_id: str, date_str: str, cfg: dict,
                  should_abort: Callable[[], bool] | None = None, *,
                  scope: str | None = None,
                  on_stacked: Callable[[], None] | None = None) -> None:
    """Stack the MKV chunks of a completed night (all, or only the locked
    ones under ``services.stacker.scope: "detections"``). Idempotent.

    One ffmpeg per chunk, ``stack_workers`` chunks at a time. (The ``concat``
    mode, one ffmpeg per batch, silently abandoned chunks whose decoded frame
    count fell short of the container's; no station used it, and it is gone.)
    A night mixes raw 720p chunks with encoded locked/detection clips (dawn
    encodes those in place before this pass), so each worker decides for its
    own chunk whether it is encoded and crops its bar.

    ``should_abort`` stops the pass at the next chunk boundary; the caller owns
    the reason (dawn_process stops the bulk path at 13:15 UTC). ``scope``
    overrides ``services.stacker.scope`` for this call, and ``on_stacked`` is
    called after every chunk the pass ran (dawn measures its rate with it).
    """
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.exists():
        logger.warning('[%s] Night dir not found for batch stack: %s', station_id, night_dir)
        return
    swept = _sweep_stale_locks(night_dir)
    if swept:
        logger.info('[%s] Removed %d stale stack lock file(s) for %s', station_id, swept, date_str)

    chunks = sorted(night_dir.glob('*_color.mkv'))
    pending = pending_chunks(station_id, date_str, cfg, scope)
    if (scope or stack_scope(cfg)) == 'detections':
        logger.info('[%s] Stack scope is detections: %d locked chunk(s) to stack for %s; '
                    'the rest stay unstacked', station_id, len(pending), date_str)

    if not pending:
        logger.info('[%s] Nothing to stack for %s', station_id, date_str)
        return

    workers = max(1, cfg.get('stack_workers', 2))
    if cfg.get('stack_mode', 'perfile') != 'perfile':
        logger.warning('[%s] stack_mode %r is no longer supported; stacking per file',
                       station_id, cfg.get('stack_mode'))
    ok = _stack_night_perfile(station_id, pending, chunks, date_str, cfg, workers, should_abort,
                              on_stacked)

    if should_abort is not None and should_abort():
        logger.warning('[%s] Stack stopped early: %d/%d ok for %s — '
                       'the rest are left for the next run',
                       station_id, ok, len(pending), date_str)
    else:
        logger.info('[%s] Stack complete: %d/%d ok for %s', station_id, ok, len(pending), date_str)


def build_night_color_meteor_stack(
    station_id: str,
    night_str: str,
    cfg: dict,
) -> Path | None:
    """Build a per-night colour meteor stack for one camera.

    For every locked chunk in the night we have a ``*_stack.webp`` (maxpx)
    and a ``*_avg.webp`` (avgpx) saved by ``_finish_stack``. ``max - avg``
    per chunk isolates the transient meteor trail (and any other transients
    like aircraft — RMS's detection filter already ran, so locked==meteor).
    We max-combine across the night and apply the station's rotate flag.

    Output: ``<station_id>_<night_str>_color_meteor_stack.webp`` in
    ``<capture_path>/<station_id>/<night_str>/stacks/``.
    Returns the output path on success, or None.
    """
    stacks_dir = rovimen_config.capture_root(cfg) / station_id / night_str / 'stacks'
    if not stacks_dir.is_dir():
        logger.info('[%s] no stacks dir for %s — skipping night meteor stack', station_id, night_str)
        return None

    import color_calibration as cc
    station_cfg = cfg.get('stations', {}).get(station_id, {})

    flags = flags_manager.load(station_id, night_str, cfg).get('chunks', {})
    locked_chunk_stems = set()
    for chunk_name, st in flags.items():
        if not isinstance(st, dict):
            continue
        if not st.get('lock'):
            continue
        # A meteor at a chunk edge is locked as its splice, so splices count.
        if rovimen_night.parse_chunk_name(chunk_name) is None:
            continue
        locked_chunk_stems.add(_stack_stem(Path(chunk_name)))

    if not locked_chunk_stems:
        logger.info('[%s] no locked chunks for %s — no meteor stack to build', station_id, night_str)
        return None

    accum = None
    used = 0
    for stem in sorted(locked_chunk_stems):
        max_webp = stacks_dir / f'{stem}.webp'
        avg_webp = stacks_dir / f'{stem}_avg.webp'
        if not max_webp.exists() or not avg_webp.exists():
            logger.debug('[%s] skip %s (missing max or avg webp)', station_id, stem)
            continue
        try:
            mx = np.array(Image.open(max_webp).convert('RGB'), dtype=np.int16)
            av = np.array(Image.open(avg_webp).convert('RGB'), dtype=np.int16)
        except Exception as e:
            logger.warning('[%s] could not load %s: %s', station_id, stem, e)
            continue
        # The max stack may have an overlay bar appended (extra rows at bottom).
        # Crop mx to av's spatial dimensions before subtracting so the shapes
        # always align regardless of overlay height.
        if mx.shape != av.shape:
            h = min(mx.shape[0], av.shape[0])
            w = min(mx.shape[1], av.shape[1])
            mx = mx[:h, :w]
            av = av[:h, :w]
        diff = np.clip(mx - av, 0, 255).astype(np.uint8)
        accum = diff if accum is None else np.maximum(accum, diff)
        used += 1

    if accum is None or used == 0:
        logger.info('[%s] no usable meteor pairs for %s', station_id, night_str)
        return None

    # Rotation was already applied to both max and avg stacks when they were
    # saved by _finish_stack, so the difference is already correctly oriented.
    # Do NOT rotate again here.

    # Brighten: diff values are typically small. Linear 4× boost followed by
    # gamma=0.5 (sqrt) lifts dim trails into the visible range while preserving
    # colour ratios. Clip keeps us in [0, 255].
    stretched = np.clip(accum.astype(np.float32) * 2.0, 0, 255)
    accum = (np.power(stretched / 255.0, 0.5) * 255).clip(0, 255).astype(np.uint8)

    # Same second-pass white balance as the night stack: the per-chunk
    # calibration is baked into the inputs, but max-combining across a night
    # of differences lets whichever channel has the fattest noise tail win,
    # so the composite needs its own grey point. White balance only — the
    # stretch above is this product's tone curve.
    accum = cc.rebalance_composite(accum, station_cfg)

    out = stacks_dir / f'{station_id}_{night_str}_color_meteor_stack.webp'
    try:
        # Published by rename: the uploader may be reading the previous one.
        with overlay.atomic_still_write(out) as tmp:
            Image.fromarray(accum, mode='RGB').save(
                tmp, format='WEBP', lossless=True, quality=100, method=4,
            )
        logger.info('[%s] built colour meteor stack from %d locked chunks → %s',
                    station_id, used, out.name)
        return out
    except Exception as e:
        logger.warning('[%s] meteor stack save failed: %s', station_id, e)
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description='Compute maxpixel stack for a chunk')
    parser.add_argument('--chunk', required=True, help='Path to .mkv chunk')
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    ok = process_chunk(Path(args.chunk), cfg)
    return 0 if ok else 1


if __name__ == '__main__':
    raise SystemExit(main())
