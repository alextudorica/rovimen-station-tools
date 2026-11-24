#!/usr/bin/env python3
"""encoder.py — Per-chunk MKV re-encoder.

Codename: coppermind

Applies OSD overlay, rotation and colour calibration to MKV chunks, writing
reencoded=True into state.json on success. Lock state (state.json and the
.locked sidecar) is untouched by the in-place rename. Whether a clip has been
encoded is tracked by the reencoded flag, backed by the ROVIMEN_ENCODED tag in
the file itself; compression_level controls encode quality only.

compression_level scale (any other value is treated as 2):
  1  — VAAPI QP19 / libx264 CRF20 (always used for locked meteor clips)
  2  — VAAPI QP20 / libx264 CRF21 (default)
  3  — VAAPI QP21 / libx264 CRF22
  4  — VAAPI QP22 / libx264 CRF23

Called by dawn_process (locked clips in Phase 1, the full-night pass in
Phase 7), archive_upload and the station API, or standalone:
    python encoder.py --chunk /path/to/chunk.mkv -c config.json
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import math
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator, List, Tuple

from overlay import (
    build_branding_overlay,
    build_drawtext_annotations,
    relabel_fragment_output,
)
import flags_manager
import rovimen_config
import rovimen_lock
import rovimen_media
import rovimen_night
from rovimen_rms import enrich_station_cfg_pointing

logger = logging.getLogger(__name__)

COMPRESSION_QP  = {1: 19, 2: 20, 3: 21, 4: 22}   # h264_vaapi QP
COMPRESSION_CRF = {1: 20, 2: 21, 3: 22, 4: 23}   # libx264 CRF (fallback)
# libx264 runs in CRF mode, which has no minimum rate (x264 ignores -minrate:
# the bitstream is byte-identical without it); -maxrate/-bufsize cap spikes.
CRF_RATE_FLOOR = ['-maxrate', '8M', '-bufsize', '2M']
# Locked (detected meteor) clips are encoded at the highest quality tier (level 1)
# using the same VAAPI/libx264 path as all other clips.
LOCKED_QUALITY_LEVEL = 1

# Per camera/night calibration cache — computed once from the first chunk,
# reused for all subsequent chunks of the same camera/night.
_calibration_cache: dict[tuple[str, str], tuple[float, float, float, float]] = {}
_calibration_cache_lock = threading.Lock()

# A re-encode never replaces a clip with a bigger one. VAAPI encodes at a
# constant QP with no bitrate cap, so on a noisy camera level 1 (QP19) wrote
# 2.25x the capture (gmnro11 RO0017: 8.4 -> ~19 Mbit/s, 2026-09-24). Such an
# encode is retried at a coarser quantizer instead. Measured on RO0017: each QP
# step from 19 to 28 cut the output to 0.72-0.82x of the step before, so one
# retry aimed at 90% of the capture usually lands under it.
_QUANT_STEP_RATIO = 0.75
_RETRY_TARGET_RATIO = 0.9
_MAX_ENCODE_ATTEMPTS = 3
# Steps above level 1 (QP19 / CRF20). Past QP32 the clip keeps its capture.
_MAX_QUANT_STEPS = 13
# Steps above level 1 each camera-night has needed so far, so its later clips
# start there instead of paying for the too-big encode again.
_quant_floor: dict[tuple[str, str], int] = {}
_quant_floor_lock = threading.Lock()

# Four processes encode chunks: dawn (locked clips, full night), the upload
# daemon, and the station API (the dashboard's Process button, clip serving).
# Two of them on one chunk used to write the same fixed scratch file and could
# install a clip the other was still writing, marked done and never retried.
# An encode now holds an exclusive flock on the chunk file itself and re-checks
# under it; a second caller waits for the first and then finds the work done.
_ENCODE_LOCK_WAIT_S = 600.0
# Scratch encodes are private per process and thread; any left behind by a
# killed encoder are swept once per night pass after this age.
_STALE_TMP_AGE_S = 6 * 3600
# A finished re-encode carries this Matroska tag, so the file itself proves it
# was encoded even if the process died between the rename and the state.json
# mark (or a full disk swallowed the mark): the next pass heals the flag
# instead of encoding the clip a second time (a second OSD bar, a second curve).
# ffmpeg writes global tags in the header (byte ~500), and -c copy keeps them.
ENCODED_TAG = 'ROVIMEN_ENCODED'
_MARKER_SCAN_BYTES = 64 * 1024

# libx264 timeout. gmnro16 (Bacău) measured ~87 s per 20 s chunk at 2 threads,
# ~8.7 thread-seconds per clip-second; the old fixed 100 s (5x the segment)
# killed such encodes on every pass, forever. 'fast' (locked clips) costs
# about 1.5x 'veryfast'. The timeout allows twice the estimate, and never
# less than the old 5x the clip.
_X264_THREAD_S_PER_CLIP_S = 8.7
_X264_FAST_PRESET_FACTOR = 1.5
_X264_TIMEOUT_MARGIN = 2.0

def _retry_steps(ratio: float) -> int:
    """Quantizer steps to add after an encode came out *ratio* x the capture."""
    return max(1, math.ceil(math.log(_RETRY_TARGET_RATIO / ratio)
                            / math.log(_QUANT_STEP_RATIO)))


@dataclass(frozen=True)
class ChunkOutcome:
    """What one real encode cost and produced, for the full-night budget.

    ``dst_bytes`` is the re-encode's size even when it was discarded
    (``kept_source``), so a caller summing outcomes sees what the pass would
    have written.
    """
    name: str
    wall_s: float
    src_bytes: int
    dst_bytes: int
    kept_source: bool


# VAAPI detection (render node, libva driver, 720p probe, per-process cache)
# lives in rovimen_media; ensure_vaapi.sh runs the same probe.
_find_vaapi_device = rovimen_media.find_vaapi_device
vaapi_env = rovimen_media.vaapi_env


_RANGE_FIX = rovimen_media.FULL_RANGE_SCALE


def _libx264_thread_count(cfg: dict) -> int:
    """Compute per-chunk thread allocation for libx264.

    In batch mode (morning pass) cfg contains ``_encode_threads`` injected by
    process_night — use it directly.  In real-time capture mode (no override)
    use the conservative per-camera calculation that assumes all cameras may
    be encoding simultaneously.
    """
    if '_encode_threads' in cfg:
        return int(cfg['_encode_threads'])
    total = os.cpu_count() or 4
    max_threads = max(1, total - 1)
    num_cameras = max(1, len(cfg.get('stations', {}) or {}))
    threads_per_cam = 2 if max_threads >= num_cameras * 2 else 1
    return threads_per_cam


def _vaapi_filter_threads(cfg: dict) -> int:
    """Filter threads for a VAAPI encode; 0 leaves ffmpeg's default.

    ``services.reencode.vaapi_threads``: ``"auto"`` (default) uses the same
    per-chunk share as libx264 (``_libx264_thread_count``), an integer is used
    as is, 0 restores ffmpeg's default. The encode itself runs on the GPU, but
    the filters (colour LUT, BT.709 conversion, OSD bar, logos) run on the CPU
    in as many threads as the host has cores: measured on the admin workstation, a 20 s 720p
    chunk cost 7.6 CPU-s by default and 5.3 at two filter threads (-30%),
    through -vf and -filter_complex alike. Decoder and encoder -threads made
    no difference. A simple graph obeys -filter_threads, a complex one (logo)
    -filter_complex_threads, so both are passed.
    """
    re_cfg = (cfg.get('services') or {}).get('reencode') or {}
    setting = re_cfg.get('vaapi_threads', 'auto') if isinstance(re_cfg, dict) else 'auto'
    if setting is None or (isinstance(setting, str) and setting.strip().lower() == 'auto'):
        return _libx264_thread_count(cfg)
    try:
        return max(0, int(setting))
    except (TypeError, ValueError):
        return _libx264_thread_count(cfg)


def _encode_timeout(cfg: dict, *, use_hw: bool, threads: int, preset: str,
                    clip_s: float) -> float:
    """Seconds one ffmpeg attempt may run. An explicit ``timeout_s`` wins.

    VAAPI keeps 5x the clip length. libx264 scales with the clip, the
    threads it gets and the preset (see _X264_THREAD_S_PER_CLIP_S)."""
    if 'timeout_s' in cfg:
        return float(cfg['timeout_s'])
    floor = clip_s * 5
    if use_hw:
        return floor
    slow = _X264_FAST_PRESET_FACTOR if preset == 'fast' else 1.0
    estimate = clip_s * _X264_THREAD_S_PER_CLIP_S * slow / max(1, threads)
    return max(floor, estimate * _X264_TIMEOUT_MARGIN)


def _mark_encoded(station_id: str, night_str: str, chunk_name: str, cfg: dict, *,
                  quant: str | None, kept_source: bool) -> None:
    """``mark_reencoded`` plus what happened to the file, in one state.json write.

    Every path that sets ``reencoded`` goes through here and writes
    ``kept_source`` explicitly: False when a re-encode replaced the capture,
    True when the capture itself is the final file (the size rule kept it, or
    a spectroscope source is preserved). The stacker relies on it: it treats a
    chunk as already encoded (no second colour curve, crop the bar)
    only for ``reencoded`` with ``kept_source`` False, and falls back to its
    frame-height guess when the key is missing (chunks marked before this).
    ``enc`` is the quantizer the replacement was written at (e.g. ``QP20``).
    """
    fields: dict = {'reencoded': True, 'kept_source': bool(kept_source)}
    if quant and not kept_source:
        fields['enc'] = quant
        drop: tuple = ()
    else:
        drop = ('enc',)
    flags_manager.update_chunk(station_id, night_str, chunk_name, cfg, fields, drop)


def _batch_cfg(cfg: dict) -> dict:
    """Return a shallow-copy of cfg with batch encode thread count injected.

    dawn_encode_parallelism (default cpu_cores // 2) chunks run in parallel,
    each getting cpu_cores // parallelism libx264 threads.  For a 6-core
    machine this gives 3 parallel chunks × 2 threads = 6 cores fully used,
    vs the current 1 thread × 1 chunk = 1 core.
    """
    cpu_cores = cfg.get('_cpu_cores', os.cpu_count() or 4)
    parallelism = max(1, cfg.get('dawn_encode_parallelism', cpu_cores // 2))
    threads_per_chunk = max(1, cpu_cores // parallelism)
    return {**cfg, '_encode_threads': threads_per_chunk, '_encode_parallelism': parallelism}


def _cpu_filter_chain(gain_r: float, gain_g: float, gain_b: float, gamma: float,
                      rotate: bool,
                      annotation: list[str] | None = None,
                      bar_h: int = 0,
                      display_levels: dict | None = None,
                      calib_contrast: float = 1.0,
                      calib_vibrance: float = 0.0,
                      full_range: bool = False,
                      calib_filters: list[str] | None = None) -> list[str]:
    """The CPU filter nodes, in order."""
    import color_calibration as cc

    filters: list[str] = []
    # Some cameras (e.g. the Berlin XMEye DEZ0DQ/DEZ0S7/DEZ132) emit full-range pixel data
    # but flag it limited-range (color_range=tv) in the H.264 VUI. Honouring that
    # flag during the YUV->RGB conversion below crushes everything under 16 to
    # black. Force full-range interpretation so the calibration sees the real
    # data. No-op for cameras already tagged full (Romania). Pair with the
    # `-color_range pc` output flag so playback isn't re-crushed.
    if full_range:
        filters.append(_RANGE_FIX)
    # Colour calibration (gain -> gamma -> contrast) is built by the shared
    # ``color_calibration.build_ffmpeg_filter`` so this video path and the
    # stacker's ``apply_calibration_np`` stay in lockstep. ``calib_contrast``
    # is the camera's ``color_post.contrast`` — the same value the stack
    # applies — so a station that lifts calibration contrast gets it in both
    # the clip and its stack thumbnail. Rotation is emitted separately below
    # so it can precede the OSD pad, hence rotate=False here.
    #
    # ``calib_filters`` (the adaptive sky-match nodes, already fitted to this
    # clip) replaces the legacy gains chain when given; an empty list means
    # "leave the colour as captured".
    if calib_filters is None:
        calib = cc.build_ffmpeg_filter(
            gain_r, gain_g, gain_b, gamma, rotate=False, contrast=calib_contrast,
            vibrance=calib_vibrance,
        )
        # build_ffmpeg_filter returns a comma-joined chain; split back into atomic
        # filter nodes so the rest of the graph (pad, overlay, hwupload) can be
        # appended and reordered as individual entries.
        calib_nodes = calib.split(',') if calib else []
    else:
        calib_nodes = [f for f in calib_filters if f]
    if rotate:
        filters += ['vflip', 'hflip']
    filters.extend(calib_nodes)
    if calib_filters is not None:
        # Adaptive clips leave RGB here, once, with an explicit BT.709 matrix.
        # Everything after (pad, the OSD drawtexts, the logo overlay) then runs
        # on YUV. Left in RGB, the overlay forced its own implicit YUV
        # conversion per frame and the BT.709 step converted again: measured on
        # gmnro01, 26.8 CPU-s per 20 s chunk against 9.9 s this way, and the
        # implicit hop nudged the sky's B/G by +0.013.
        filters.append(f'scale=out_color_matrix=bt709:out_range={"pc" if full_range else "tv"},'
                       'format=yuv420p')
    # Display-only contrast/brightness lift for light-polluted sites. This is a
    # video-only knob (``display_levels``); the stack never applies it. It rides
    # on top of the calibration contrast already folded in above — the two
    # compose multiplicatively, both being linear multipliers about the
    # 0.5-luma pivot in ffmpeg ``eq``.
    if display_levels:
        eq_parts: list[str] = []
        contrast = display_levels.get('contrast')
        brightness = display_levels.get('brightness')
        if contrast is not None:
            eq_parts.append(f'contrast={contrast}')
        if brightness is not None:
            eq_parts.append(f'brightness={brightness}')
        if eq_parts:
            filters.append('eq=' + ':'.join(eq_parts))
    if bar_h > 0:
        filters.append(f'pad=iw:ih+{bar_h}:0:0:black')
    if annotation:
        filters.extend(annotation)
    return filters


@dataclass
class _FfmpegCmd:
    """One re-encode command, assembled in a fixed order.

    ``ffmpeg -y [-vaapi_device D] -i SRC [logo inputs]
    (-filter_complex G -map [out] | -vf CHAIN) CODEC... -an DST``. Exactly one
    of ``vf`` / ``filter_complex`` is normally set; neither means no filter
    (a libx264 encode with nothing to change). Callers add their own output
    options (threads, colour tags, the re-encode tag) just before DST.
    """
    src: Path
    dst: Path
    codec: list[str]
    device: str | None = None
    extra_inputs: list[str] = field(default_factory=list)
    vf: str | None = None
    filter_complex: str | None = None

    def argv(self) -> list[str]:
        cmd = ['ffmpeg', '-y']
        if self.device:
            cmd += ['-vaapi_device', self.device]
        cmd += ['-i', str(self.src), *self.extra_inputs]
        if self.filter_complex is not None:
            cmd += ['-filter_complex', self.filter_complex, '-map', '[out]']
        elif self.vf:
            cmd += ['-vf', self.vf]
        return cmd + [*self.codec, '-an', str(self.dst)]


def _filter_graph(cpu_filters: list[str], tail: str, *,
                  brand_fragment: str, use_logo: bool,
                  vaapi: bool) -> tuple[str | None, str | None]:
    """``(vf, filter_complex)`` for the CPU chain, the encoder-specific tail
    (``format=nv12,...,hwupload`` for VAAPI; ``setparams`` or nothing for
    libx264) and the branding fragment.

    A logo needs extra inputs, so it promotes the graph to -filter_complex;
    otherwise it is a plain -vf chain.
    """
    cpu_chain = ','.join(cpu_filters) if cpu_filters else 'null'
    if use_logo:
        main = f'[0:v]{cpu_chain}'
        if vaapi:
            return None, f'{main}[vmain];{brand_fragment};[vbrand]{tail}[out]'
        # libx264 needs no hwupload stage: the branding ends at [out] itself,
        # or at [vcol] when setparams still has to follow it.
        if tail:
            brand = relabel_fragment_output(brand_fragment, 'vbrand', 'vcol')
            return None, f'{main}[vmain];{brand};[vcol]{tail}[out]'
        return None, f'{main}[vmain];{relabel_fragment_output(brand_fragment, "vbrand", "out")}'
    chain = [*cpu_filters, tail] if tail else list(cpu_filters)
    return (','.join(chain) if chain else None), None


def _build_cmd(src: Path, dst_tmp: Path, compression_level: int,
               gain_r: float, gain_g: float, gain_b: float, gamma: float,
               rotate: bool,
               overlay_cfg: dict | None, station_id: str,
               station_cfg: dict, chunk_epoch: int,
               vaapi_device: str, use_vaapi: bool = True,
               display_levels: dict | None = None,
               calib_contrast: float = 1.0,
               calib_vibrance: float = 0.0,
               calib_filters: list[str] | None = None,
               full_range: bool = False,
               preset: str = 'fast',
               quant_offset: int = 0) -> list[str]:
    if compression_level not in COMPRESSION_QP:
        raise ValueError(f'compression_level must be one of {sorted(COMPRESSION_QP)}, '
                         f'got {compression_level!r}')

    # quant_offset: extra steps on top of the level, from process_chunk's
    # never-bigger retry.
    qp  = COMPRESSION_QP[compression_level] + quant_offset
    crf = COMPRESSION_CRF[compression_level] + quant_offset

    annotation: list[str] = []
    bar_h: int = 0
    if overlay_cfg and station_id and chunk_epoch:
        annotation, bar_h = build_drawtext_annotations(
            overlay_cfg, station_id, station_cfg, chunk_epoch,
        )

    # Branding overlay: astromania logo centred in the bottom bar + corner GMN
    # logo. Resolved once here so the VAAPI and libx264 paths share one layout
    # source. ``bar_h`` lets the logo be sized/placed inside the bar.
    logo_inputs: list[str] = []
    brand_fragment = ''
    if overlay_cfg and annotation:
        logo_inputs, brand_fragment = build_branding_overlay(
            overlay_cfg, in_label='vmain', out_label='vbrand', input_index=1,
            bar_h=bar_h,
        )
    use_logo = bool(logo_inputs)

    cpu_filters = _cpu_filter_chain(gain_r, gain_g, gain_b, gamma, rotate,
                                     annotation or None, bar_h,
                                     display_levels=display_levels,
                                     calib_contrast=calib_contrast,
                                     calib_vibrance=calib_vibrance,
                                     full_range=full_range,
                                     calib_filters=calib_filters)

    # The sky-match curve is fitted in RGB, so the RGB->YUV step decides what a
    # player shows. Left to ffmpeg, the matrix differs by version and the
    # camera's input tags can ride through onto differently-converted data
    # (measured: a BT.601 conversion flagged BT.709 turns the sky ~2-3% green).
    # Adaptive clips convert explicitly to BT.709 right after the colour nodes
    # (see _cpu_filter_chain) and are tagged to match (process_chunk adds the
    # output flags); the legacy path is left exactly as it was. setparams
    # carries the tags on the frames: ffmpeg >= 7 takes the encoder's colour
    # description from the frames and ignores -color_primaries/-color_trc
    # (measured on 8.0.1); 4.2.7 on the oldest stations supports it too.
    rng = 'pc' if full_range else 'tv'
    setp = (f'setparams=color_primaries=bt709:color_trc=bt709:colorspace=bt709:range={rng}'
            if calib_filters is not None else '')
    if use_vaapi:
        tail = f'format=nv12,{setp},hwupload' if setp else 'format=nv12,hwupload'
        codec = ['-c:v', 'h264_vaapi', '-qp', str(qp)]
    else:
        tail = setp
        codec = ['-c:v', 'libx264', '-crf', str(crf), '-preset', preset, *CRF_RATE_FLOOR]
    vf, filter_complex = _filter_graph(cpu_filters, tail,
                                       brand_fragment=brand_fragment, use_logo=use_logo,
                                       vaapi=use_vaapi)
    return _FfmpegCmd(src=src, dst=dst_tmp, codec=codec,
                      device=vaapi_device if use_vaapi else None,
                      extra_inputs=logo_inputs if use_logo else [],
                      vf=vf, filter_complex=filter_complex).argv()


def _preserves_source(station_cfg: dict) -> bool:
    """True when a camera's chunks must reach the archive exactly as captured.

    A spectroscope's pixels are measurements, not a picture. The re-encode
    applies colour calibration and gamma, pads an OSD bar on, and atomically
    replaces the original: for GH662S that turned a 1.9 MB 1280x720 capture
    into a 593 KB 1280x848 render, on the Storage Box copy too, so the raw
    frames were gone everywhere. A first-order spectrum is faint and broad and
    is precisely what a 3x re-compression destroys -- verified on the
    2026-09-21 21:12:36 event, where no dispersed light survived above the
    sky background.

    Opt out explicitly with ``preserve_source``; it defaults on for any camera
    carrying a ``spectro`` pointing block, so a science camera is protected
    without needing a per-station config edit.
    """
    if 'preserve_source' in station_cfg:
        return bool(station_cfg['preserve_source'])
    return bool(station_cfg.get('spectro'))


def _run_encode(cmd: list[str], src: Path, dst_tmp: Path, use_hw: bool,
                timeout_s: float) -> tuple[str, int, bytes]:
    """Run one ffmpeg encode of *src* into *dst_tmp*.

    Returns ``(outcome, returncode, stderr)`` with outcome 'ok', 'timeout' or
    'error'. The caller owns *dst_tmp* afterwards, whatever the outcome.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            env={**(vaapi_env() if use_hw else os.environ), 'TZ': 'UTC'},
        )
    except Exception as exc:
        logger.error('FAILED %s — could not launch ffmpeg: %s', src.name, exc)
        return 'error', -1, b''
    try:
        _, stderr_bytes = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return 'timeout', -1, b''
    return ('ok' if proc.returncode == 0 else 'error'), proc.returncode, stderr_bytes or b''


def _acquire_encode_lock(mkv: Path, timeout: float) -> tuple[str, int | None]:
    """Take an exclusive flock on *mkv* itself. Returns (status, fd).

    status: 'held' (fd is locked), 'busy' (another encoder kept it past
    *timeout*), 'gone' (the chunk vanished) or 'unguarded' (the filesystem
    cannot lock; proceed as before rather than drop the chunk).

    The chunk file is the lock, so there are no lock files to litter the
    night directory. A finished encode renames its output over the chunk,
    which leaves a waiter holding the old inode: after acquiring, the inode
    is compared with the path's and the lock retaken on the new file.

    Deliberately not ``rovimen_media.chunk_lock``: a waiter here must see the
    encoded file that replaced the chunk (and, with the ROVIMEN_ENCODED tag,
    know it is done), which a separate lock file cannot tell it.
    """
    deadline = time.monotonic() + max(0.0, timeout)
    for _ in range(5):
        try:
            fd = os.open(mkv, os.O_RDONLY)
        except FileNotFoundError:
            return 'gone', None
        except OSError as exc:
            logger.debug('Encode lock unavailable for %s (%s): proceeding unguarded',
                         mkv.name, exc)
            return 'unguarded', None
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    return 'busy', None
                time.sleep(0.25)
            except OSError as exc:
                os.close(fd)
                logger.debug('Encode lock unavailable for %s (%s): proceeding unguarded',
                             mkv.name, exc)
                return 'unguarded', None
        try:
            now = os.stat(mkv)
        except FileNotFoundError:
            os.close(fd)
            return 'gone', None
        held = os.fstat(fd)
        if (held.st_ino, held.st_dev) == (now.st_ino, now.st_dev):
            return 'held', fd
        os.close(fd)   # replaced while we waited: lock the file now at the path
    return 'busy', None


@contextmanager
def _chunk_encode_lock(mkv: Path, timeout: float = 0.0) -> Iterator[bool]:
    """Yield True while this process may encode *mkv*, False if it may not.

    False means another encoder held the chunk past *timeout* or the chunk
    vanished. The kernel drops an flock when its holder exits, so a killed
    encoder never leaves the chunk locked.
    """
    status, fd = _acquire_encode_lock(mkv, timeout)
    try:
        yield status in ('held', 'unguarded')
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)


def _has_encoded_marker(mkv: Path) -> bool:
    """True if *mkv* carries the ``ENCODED_TAG`` a finished re-encode writes."""
    key = ENCODED_TAG.encode('ascii')
    try:
        with open(mkv, 'rb') as fh:
            if key in fh.read(_MARKER_SCAN_BYTES):
                return True
            size = os.fstat(fh.fileno()).st_size
            if size > _MARKER_SCAN_BYTES:
                fh.seek(size - _MARKER_SCAN_BYTES)
                return key in fh.read(_MARKER_SCAN_BYTES)
    except OSError:
        return False
    return False


def _scratch_path(mkv: Path) -> Path:
    """This process and thread's private scratch output for *mkv* (same dir,
    so the final rename is atomic; ``.mkv`` last, so ffmpeg picks Matroska)."""
    return mkv.with_name(f'{mkv.name}.{os.getpid()}-{threading.get_ident()}.tmp.mkv')


def _sweep_stale_scratch(night_dir: Path, max_age_s: float = _STALE_TMP_AGE_S) -> int:
    """Delete this night's encoder scratch files (``*.tmp.mkv``) older than
    *max_age_s*: left by an encoder that was killed mid-run. Returns the count."""
    cutoff = time.time() - max_age_s
    removed = 0
    try:
        candidates = list(night_dir.glob('*.tmp.mkv'))
    except OSError:
        return 0
    for p in candidates:
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                removed += 1
        except OSError:
            pass
    if removed:
        logger.info('coppermind: removed %d stale scratch encode(s) in %s', removed, night_dir)
    return removed


def process_chunk(mkv: Path, cfg: dict, *,
                  on_encoded: Callable[[ChunkOutcome], None] | None = None) -> bool:
    """See ``_process_chunk``. Deletes the per-clip neutral-highlights LUT
    (``color_calibration.write_adaptive_cube``, ~7 MB) whatever path it took."""
    cubes: list[Path] = []
    try:
        return _process_chunk(mkv, cfg, on_encoded=on_encoded, _cubes=cubes)
    finally:
        for c in cubes:
            try:
                c.unlink()
            except OSError:
                pass


def _process_chunk(mkv: Path, cfg: dict, *,
                   on_encoded: Callable[[ChunkOutcome], None] | None = None,
                   _cubes: list[Path] | None = None) -> bool:
    """Re-encode an MKV chunk in-place, applying OSD overlay, rotation and colour
    calibration. Idempotent: returns True immediately if already reencoded.
    Detection locks live in state.json and the ``.locked`` sidecar, which the
    in-place rename leaves alone. One encoder at a time holds a chunk (see
    ``_chunk_encode_lock``); the others wait and then find it done.

    Never bigger: an encode that is not smaller than the capture is retried at
    a coarser quantizer (up to ``_MAX_ENCODE_ATTEMPTS`` encodes, QP32 at most),
    and the first one that fits replaces it. Clips of the camera-night then
    start at that quantizer. If none fits, the capture is kept, and the chunk
    is still marked ``reencoded`` so it is not retried — the same "final form
    reached" meaning ``_preserves_source`` gives the flag. This holds for
    locked (detection) clips too.

    ``on_encoded`` is called once per chunk whose ffmpeg ran, with the total
    wall time and the size of the last encode, kept or not.
    """
    # coppermind: VAAPI encode in-place with OSD annotation

    m = rovimen_night.CHUNK_RE.match(mkv.name) or rovimen_night.STITCHED_CHUNK_RE.match(mkv.name)
    if not m:
        logger.warning('Skipping — filename does not match pattern: %s', mkv.name)
        return False

    station_id, date_str, time_str = m.group(1), m.group(2), m.group(3)
    night_str = rovimen_night.night_of(date_str, time_str)

    # Load once — used for both reencoded check and lock check below.
    chunk_state = _load_chunk_state(station_id, night_str, date_str, mkv, cfg)

    # Idempotent check — prefer state.json, fall back to .reencoded sidecar for
    # nights that pre-date the pipeline rewrite.
    if chunk_state.get('reencoded', False):
        return True
    if Path(str(mkv) + '.reencoded').exists():
        return True

    if not chunk_state.get('ready', False):
        logger.warning('Skipping — chunk not ready: %s', mkv.name)
        return False

    station_cfg = cfg.get('stations', {}).get(station_id, {})
    if _preserves_source(station_cfg):
        # Mark it done rather than leaving it pending: `reencoded` is the sole
        # gate archive_upload reads for upload readiness, so a chunk that is
        # deliberately never encoded would otherwise never ship. The capture is
        # the final file (kept_source), so its stack still gets colour.
        _mark_encoded(station_id, night_str, mkv.name, cfg, quant=None, kept_source=True)
        logger.info('Preserving source, not re-encoding: %s', mkv.name)
        return True

    wait_s = _lock_wait_seconds(cfg)
    with _chunk_encode_lock(mkv, wait_s) as mine:
        if not mine:
            if not mkv.exists():
                logger.info('Skipping — source vanished: %s', mkv.name)
            else:
                logger.warning('Skipping %s: another encoder held it for %.0fs', mkv.name, wait_s)
            return False
        # Re-check under the lock: the holder we waited for has usually just
        # finished this very chunk.
        chunk_state = _load_chunk_state(station_id, night_str, date_str, mkv, cfg)
        if chunk_state.get('reencoded', False):
            return True
        if _has_encoded_marker(mkv):
            _mark_encoded(station_id, night_str, mkv.name, cfg, quant=None, kept_source=False)
            logger.info('%s already carries the re-encode tag — restored its state.json mark '
                        'instead of encoding it twice', mkv.name)
            return True
        job = _resolve_job(mkv, cfg, station_id, date_str, time_str, night_str,
                           chunk_state, station_cfg, _cubes)
        if job is None:
            return False
        result = _encode_with_size_rule(job)
        if result is None:
            return False
        return _commit(job, result, on_encoded)


def _is_locked(chunk_state: dict, mkv: Path) -> bool:
    """A detection lock in state.json, or the legacy ``.locked`` sidecar."""
    return rovimen_lock.chunk_is_locked(mkv, chunk_state)


def _load_chunk_state(station_id: str, night_str: str, date_str: str, mkv: Path,
                      cfg: dict) -> dict:
    """The chunk's state.json entry ({} when it has none)."""
    chunk_state = flags_manager.load(
        station_id, night_str, cfg
    ).get('chunks', {}).get(mkv.name, {})
    # Stitched clips created before the calendar-date naming fix used the night
    # date in their filename, so the night rule subtracts an extra day and misses
    # the state.json entry. Fall back to date_str directly as a migration shim.
    if not chunk_state and rovimen_night.STITCHED_CHUNK_RE.match(mkv.name) and night_str != date_str:
        chunk_state = flags_manager.load(
            station_id, date_str, cfg
        ).get('chunks', {}).get(mkv.name, {})
    return chunk_state


def _lock_wait_seconds(cfg: dict) -> float:
    """Seconds an encoder waits for another encoder's hold on the same chunk."""
    try:
        return max(0.0, float(cfg.get('encode_lock_wait_sec', _ENCODE_LOCK_WAIT_S)))
    except (TypeError, ValueError):
        return _ENCODE_LOCK_WAIT_S


@dataclass
class _EncodeJob:
    """One chunk's encode, resolved before ffmpeg runs: colour, overlay,
    encoder choice, quality tier and limits. ``fall_back_to_cpu`` switches a
    VAAPI job to libx264 after a runtime error."""
    mkv: Path
    cfg: dict
    station_id: str
    night_str: str
    dst_tmp: Path
    t0: float
    level: int
    preset: str
    use_hw: bool
    vaapi_device: str | None
    x264_threads: int
    vaapi_threads: int
    clip_s: float
    timeout_s: float
    full_range: bool
    calib_filters: list[str] | None
    # The colour, overlay and rotation arguments _build_cmd takes.
    build_kwargs: dict

    @property
    def quant(self) -> tuple[str, int]:
        """Quantizer scale and its value at this job's level (QP or CRF)."""
        return (('QP', COMPRESSION_QP[self.level]) if self.use_hw
                else ('CRF', COMPRESSION_CRF[self.level]))

    def fall_back_to_cpu(self) -> None:
        self.use_hw, self.vaapi_device = False, None
        self.timeout_s = _encode_timeout(self.cfg, use_hw=False, threads=self.x264_threads,
                                         preset=self.preset, clip_s=self.clip_s)


@dataclass(frozen=True)
class _EncodeResult:
    """What the size rule settled on: the last encode's size (kept or not)."""
    src_bytes: int
    dst_bytes: int
    quant: str          # e.g. 'QP20', the quantizer of the last encode


EncodeRunner = Callable[[List[str], Path, Path, bool, float], Tuple[str, int, bytes]]


def _resolve_job(mkv: Path, cfg: dict, station_id: str, date_str: str, time_str: str,
                 night_str: str, chunk_state: dict, station_cfg: dict,
                 _cubes: list[Path] | None) -> _EncodeJob | None:
    """Fit the clip's colour and settle every encode decision. None when the
    source vanished meanwhile."""
    import color_calibration as cc
    # The chunk's cost starts here, colour sampling and fit included: the
    # full-night budget projects from ChunkOutcome.wall_s, and leaving out the
    # ~0.8 s fit under-projected the pass.
    t0 = time.monotonic()
    station_cfg = enrich_station_cfg_pointing(station_id, station_cfg, cfg)
    # Cameras that emit full-range data tagged limited (color_range=tv) need
    # full-range interpretation during calibration + a full-range output tag,
    # else the YUV->RGB crushes blacks (Berlin XMEye). Station-level opt-in.
    full_range = bool(cfg.get('full_range_fix', False))
    # Locked (detected meteor) clips get the best quality tier, and the
    # neutral-highlights LUT when they clip.
    is_locked = _is_locked(chunk_state, mkv)

    display_levels = cfg.get('display_levels') or {}
    calib_filters: list[str] | None = None
    if cc.adaptive_enabled(station_cfg, 'video'):
        # Sky match: fit THIS clip's own sky, every clip. The legacy path below
        # froze one gain set per camera-night, so the first clip of the night
        # decided the colour of all the others while the raw sky kept moving.
        gain_r = gain_g = gain_b = gamma = 1.0
        calib_contrast, calib_vibrance = 1.0, 0.0
        # The adaptive curve owns the whole look, so the legacy video-only
        # display lift would double up; the stacker's pedestal subtraction
        # (background_subtract_pct) is untouched.
        display_levels = {}
        try:
            curve = cc.resolve_adaptive_from_mkv(
                mkv, station_cfg,
                location=rovimen_config.station_location(cfg, station_cfg),
                when=rovimen_night.chunk_midpoint_utc(date_str, time_str),
                full_range=full_range,
            )
        except subprocess.TimeoutExpired:
            # A hung disk or decoder: encode it with its colour as captured
            # rather than failing the chunk (which then uploads raw).
            curve = None
        if curve is None:
            logger.warning('adaptive colour: sky unreadable or too dark in %s — '
                           'leaving its colour as captured', mkv.name)
            calib_filters = []
        else:
            cube = None
            if cc.wants_neutral_lut(curve, is_locked):
                import tempfile
                fd, name = tempfile.mkstemp(prefix='rovimen_adaptive_', suffix='.cube')
                os.close(fd)
                cube = cc.write_adaptive_cube(curve, Path(name))
                if _cubes is not None:
                    _cubes.append(cube)
            calib_filters = cc.build_adaptive_ffmpeg_filter(curve, cube).split(',')
            logger.info('[%s] adaptive colour %s: %s%s', station_id, mkv.name,
                        curve.describe(), ' +neutral highlights (lut3d)' if cube else '')
    else:
        cache_key = (station_id, night_str)
        with _calibration_cache_lock:
            cached = _calibration_cache.get(cache_key)

        # ``color_post.contrast`` is part of the calibration tone curve and MUST be
        # applied here to match the stack (stacker.py hands it to
        # apply_calibration_np). It is distinct from ``display_levels.contrast``,
        # a video-only site display lift that the stack never applies; the two
        # compose multiplicatively in the ffmpeg ``eq`` filter downstream.
        post = cc.resolve_color_post(station_cfg, 'video')
        calib_contrast = float(post['contrast'])
        calib_vibrance = float(post['vibrance'])

        if cached is not None:
            gain_r, gain_g, gain_b, gamma = cached
        else:
            try:
                calib = cc.resolve_calibration_from_mkv(mkv, station_cfg, full_range=full_range,
                                                        product='video')
            except subprocess.TimeoutExpired:
                calib = None
            if calib is None:
                logger.warning('adaptive gain derivation failed for %s — skipping calibration', mkv.name)
                gain_r, gain_g, gain_b, gamma = 1.0, 1.0, 1.0, float(post['gamma'])
            else:
                gain_r, gain_g, gain_b, gamma = calib
                with _calibration_cache_lock:
                    _calibration_cache[cache_key] = calib
    rotate = station_cfg.get('rotate', False)
    compression_level = cfg.get('compression_level', 2)
    if compression_level not in (1, 2, 3, 4):
        compression_level = 2
    caps = cfg.get('capabilities', {})
    want_hw = caps.get('hwencode', True)
    vaapi_device = _find_vaapi_device(cfg) if want_hw else None
    use_hw = vaapi_device is not None

    # Stitched boundary clips have mid-stream H.264 parameter changes that cause
    # VAAPI hwupload to fail when reinitializing the filter graph at the splice
    # point. Force CPU path for these clips.
    if rovimen_night.STITCHED_CHUNK_RE.match(mkv.name):
        use_hw = False
        vaapi_device = None

    # Force quality level 1 (best) for locked clips, regardless of config.
    effective_level = LOCKED_QUALITY_LEVEL if is_locked else compression_level
    if effective_level not in (1, 2, 3, 4):
        effective_level = 2
    preset = 'fast' if is_locked else 'veryfast'
    x264_threads = _libx264_thread_count(cfg)
    segment_s = float(cfg.get('segment_duration', 20))
    # A stitched boundary clip spans two chunks.
    clip_s = segment_s * (2 if rovimen_night.STITCHED_CHUNK_RE.match(mkv.name) else 1)

    encoder_name = (f'h264_vaapi ({vaapi_device})' if use_hw
                    else f'libx264 CRF{COMPRESSION_CRF[effective_level]}')  # vaapi_device non-None when use_hw
    logger.debug('%s clip — encoder: %s  QP/CRF=%s',
                 'Locked' if is_locked else 'Unlocked', encoder_name,
                 COMPRESSION_QP[effective_level] if use_hw else COMPRESSION_CRF[effective_level])

    overlay_cfg = cfg.get('overlay') or {}
    if not overlay_cfg.get('enabled', True):
        overlay_cfg = {}
    chunk_epoch = 0
    if overlay_cfg:
        try:
            chunk_epoch = int(datetime(
                int(date_str[:4]), int(date_str[4:6]), int(date_str[6:]),
                int(time_str[:2]), int(time_str[2:4]), int(time_str[4:]),
                tzinfo=timezone.utc,
            ).timestamp())
        except Exception:
            pass

    if not mkv.exists():
        logger.info('Skipping — source vanished: %s', mkv.name)
        return None

    return _EncodeJob(
        mkv=mkv, cfg=cfg, station_id=station_id, night_str=night_str,
        dst_tmp=_scratch_path(mkv), t0=t0, level=effective_level, preset=preset,
        use_hw=use_hw, vaapi_device=vaapi_device, x264_threads=x264_threads,
        vaapi_threads=_vaapi_filter_threads(cfg), clip_s=clip_s,
        timeout_s=_encode_timeout(cfg, use_hw=use_hw, threads=x264_threads, preset=preset,
                                  clip_s=clip_s),
        full_range=full_range, calib_filters=calib_filters,
        build_kwargs=dict(
            gain_r=gain_r, gain_g=gain_g, gain_b=gain_b, gamma=gamma, rotate=rotate,
            overlay_cfg=overlay_cfg, station_id=station_id,
            station_cfg=station_cfg, chunk_epoch=chunk_epoch,
            display_levels=display_levels,
            calib_contrast=calib_contrast,
            calib_vibrance=calib_vibrance,
        ),
    )


def _output_options(job: _EncodeJob, quant_offset: int) -> list[str]:
    """Output options that go just before the output path, in order: threads,
    colour tags, the re-encode tag."""
    opts: list[str] = []
    if not job.use_hw:
        opts += ['-threads', str(job.x264_threads)]
    elif job.vaapi_threads > 0:
        n = str(job.vaapi_threads)
        opts += ['-filter_threads', n, '-filter_complex_threads', n]
    if job.calib_filters is not None:
        # Adaptive clips were converted explicitly to BT.709 (see _build_cmd);
        # tag them so every player decodes them with the same matrix.
        opts += ['-colorspace', 'bt709', '-color_primaries', 'bt709', '-color_trc', 'bt709',
                 '-color_range', 'pc' if job.full_range else 'tv']
    elif job.full_range:
        # Tag the output full-range so players don't re-crush the (now correct)
        # full-range data.
        opts += ['-color_range', 'pc']
    # The file's own proof that it is a finished re-encode (ENCODED_TAG).
    name, base = job.quant
    opts += ['-metadata', f'{ENCODED_TAG}={name}{base + quant_offset}']
    return opts


def _job_cmd(job: _EncodeJob, quant_offset: int) -> list[str]:
    cmd = _build_cmd(job.mkv, job.dst_tmp, job.level,
                     vaapi_device=job.vaapi_device, use_vaapi=job.use_hw,
                     calib_filters=job.calib_filters, full_range=job.full_range,
                     preset=job.preset, quant_offset=quant_offset, **job.build_kwargs)
    return cmd[:-1] + _output_options(job, quant_offset) + [cmd[-1]]


def _encode_with_size_rule(job: _EncodeJob,
                           run: EncodeRunner | None = None) -> _EncodeResult | None:
    """Encode until the output is smaller than the capture or the attempts
    run out (see ``_process_chunk``). None when ffmpeg timed out or failed.

    *run* executes one attempt (``_run_encode`` by default), so the policy can
    be exercised without ffmpeg.
    """
    run = run or _run_encode
    mkv, dst_tmp = job.mkv, job.dst_tmp
    # Levels are consecutive steps of one quantizer scale (QP19-22 / CRF20-23),
    # so a floor learned at one level carries over to another.
    level_steps = job.level - 1
    floor_key = (job.station_id, job.night_str)
    with _quant_floor_lock:
        offset = max(0, _quant_floor.get(floor_key, 0) - level_steps)
    src_bytes = mkv.stat().st_size
    dst_bytes = 0
    retried = False
    fell_back = False
    attempt = 0
    while True:
        attempt += 1
        cmd = _job_cmd(job, offset)
        outcome, rc, stderr_bytes = run(cmd, mkv, dst_tmp, job.use_hw, job.timeout_s)
        if outcome == 'timeout':
            dst_tmp.unlink(missing_ok=True)
            logger.warning('TIMEOUT %s  timeout=%ds', mkv.name, job.timeout_s)
            return None
        if outcome == 'error':
            dst_tmp.unlink(missing_ok=True)
            tail = stderr_bytes[-300:].decode('utf-8', errors='replace') if stderr_bytes else ''
            if job.use_hw and not fell_back:
                # A VAAPI runtime failure (a driver hiccup, a stream the GPU
                # rejects) used to fail the chunk on every pass, so it was
                # never marked and never uploaded. Retry it once on the CPU;
                # that is not a size-rule attempt.
                logger.warning('VAAPI FAILED %s  rc=%d  stderr=...%s — retrying on libx264',
                               mkv.name, rc, tail)
                fell_back = True
                job.fall_back_to_cpu()
                attempt -= 1
                continue
            logger.error('FAILED %s  rc=%d  wall=%.1fs  stderr=...%s',
                         mkv.name, rc, time.monotonic() - job.t0, tail)
            return None
        dst_bytes = dst_tmp.stat().st_size if dst_tmp.exists() else 0
        if dst_bytes < src_bytes or src_bytes == 0:
            break
        if attempt >= _MAX_ENCODE_ATTEMPTS or level_steps + offset >= _MAX_QUANT_STEPS:
            break
        quant_name, quant_base = job.quant
        new_offset = min(offset + _retry_steps(dst_bytes / src_bytes),
                         _MAX_QUANT_STEPS - level_steps)
        logger.info('RETRY %s  %s%d wrote %.1fMB, not under the capture\'s %.1fMB '
                    '— re-encoding at %s%d', mkv.name, quant_name, quant_base + offset,
                    dst_bytes / 1e6, src_bytes / 1e6, quant_name, quant_base + new_offset)
        dst_tmp.unlink(missing_ok=True)
        offset = new_offset
        retried = True
    if retried:
        with _quant_floor_lock:
            _quant_floor[floor_key] = max(_quant_floor.get(floor_key, 0),
                                          level_steps + offset)
    quant_name, quant_base = job.quant
    return _EncodeResult(src_bytes=src_bytes, dst_bytes=dst_bytes,
                         quant=f'{quant_name}{quant_base + offset}')


def _commit(job: _EncodeJob, result: _EncodeResult,
            on_encoded: Callable[[ChunkOutcome], None] | None) -> bool:
    """Install the encode (or keep the capture), mark state, report.

    False, installing and marking nothing, when the chunk was deleted while
    it encoded: renaming the encode over its path would bring it back. The
    janitor skips a chunk this process holds (_chunk_encode_lock), so this
    catches a deletion that did not take the lock, or a filesystem that
    cannot lock.
    """
    mkv = job.mkv
    src_bytes, dst_bytes = result.src_bytes, result.dst_bytes
    wall = time.monotonic() - job.t0
    if not mkv.exists():
        job.dst_tmp.unlink(missing_ok=True)
        logger.info('Discarding the encode of %s: the chunk was deleted while it encoded',
                    mkv.name)
        return False
    # Success: rename tmp over original (in-place).
    # The .locked sidecar (chunk.mkv.locked) is a separate file — it survives
    # the rename untouched, so no unlock/relock dance is needed.
    kept_source = src_bytes > 0 and dst_bytes >= src_bytes
    if kept_source:
        job.dst_tmp.unlink(missing_ok=True)
    else:
        job.dst_tmp.rename(mkv)           # atomic in-place replace

    _mark_encoded(job.station_id, job.night_str, mkv.name, job.cfg,
                  quant=result.quant, kept_source=kept_source)
    ratio = 100.0 * (1 - dst_bytes / src_bytes) if src_bytes else 0.0
    segment_secs = job.cfg.get('segment_duration', 20)
    speed = segment_secs / wall if wall else 0
    logger.info('%s %s  wall=%.1fs  src=%.1fMB  dst=%.1fMB  saved=%.1f%%  speed=%.2fx  %s',
                'KEPT SOURCE (no re-encode came out smaller)' if kept_source else 'DONE',
                mkv.name, wall, src_bytes / 1e6, dst_bytes / 1e6, ratio, speed,
                result.quant)
    if on_encoded is not None:
        on_encoded(ChunkOutcome(name=mkv.name, wall_s=wall, src_bytes=src_bytes,
                                dst_bytes=dst_bytes, kept_source=kept_source))
    return True


def process_night_locked_only(station_id: str, date_str: str, cfg: dict) -> None:
    """Re-encode only locked (detection) chunks — called before archive upload.

    Applies the full pipeline (rotate + colour calibration + overlay) so that
    uploaded clips are presentation-quality without waiting for the bulk
    reencode pass.  process_chunk() is idempotent — these chunks are skipped
    when process_night() runs later.
    """
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.exists():
        return
    _sweep_stale_scratch(night_dir)

    if not rovimen_config.meteor_clips_encoded(cfg):
        # CPU-starved boxes (e.g. gmnro05: 5 cameras on a 4-core Haswell) keep
        # every core for RMS and the dawn stacks. Their meteor clips are encoded
        # only when someone asks: the dashboard's Process button calls the
        # station's /api/process_clip, which encodes + uploads that one clip.
        # Until then a clip is not reencoded, so archive_upload holds it back.
        logger.info('[%s] coppermind: meteor clips are encoded on demand only '
                    '(services.reencode.meteor_clips=false) — skipping %s',
                    station_id, date_str)
        return

    state = flags_manager.load(station_id, date_str, cfg)
    chunks_state = state.get('chunks', {})

    locked = []
    for mkv in sorted(night_dir.glob('*_color.mkv')):
        if _is_locked(chunks_state.get(mkv.name, {}), mkv):
            locked.append(mkv)

    if not locked:
        logger.info('[%s] coppermind: no locked chunks for %s', station_id, date_str)
        return

    bcfg = _batch_cfg(cfg)
    parallelism = bcfg['_encode_parallelism']
    logger.info('[%s] coppermind: encoding %d locked chunk(s) for %s (workers=%d, threads/chunk=%d)',
                station_id, len(locked), date_str, parallelism, bcfg['_encode_threads'])
    ok = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(process_chunk, mkv, bcfg): mkv for mkv in locked}
        for fut in as_completed(futures):
            mkv = futures[fut]
            try:
                if fut.result():
                    ok += 1
            except Exception:
                logger.exception('[%s] coppermind locked encode failed for %s', station_id, mkv.name)
    logger.info('[%s] coppermind: %d/%d locked chunk(s) encoded for %s',
                station_id, ok, len(locked), date_str)


def _drop_unstacked(station_id: str, date_str: str, chunks: list[Path],
                    cfg: dict) -> list[Path]:
    """Leave out chunks the stacker has not stacked yet, when it is enabled.

    A stack is fitted on the raw capture; one built from an already-encoded
    clip inherits the clip's per-frame curve and comes out brighter than its
    neighbours. The full-night pass runs after dawn's stacking, so a chunk
    still unstacked here is left raw for the final stack pass (and then keeps
    its capture). Only an explicit ``services.stacker.enabled: true`` counts.
    """
    svc = (cfg.get('services') or {}).get('stacker')
    if not (isinstance(svc, dict) and svc.get('enabled') is True):
        return chunks
    state = flags_manager.load(station_id, date_str, cfg).get('chunks', {})
    kept: list[Path] = []
    for mkv in chunks:
        entry = state.get(mkv.name, {})
        if entry.get('stacked') or entry.get('reencoded'):
            kept.append(mkv)
    skipped = len(chunks) - len(kept)
    if skipped:
        logger.info('[%s] coppermind: leaving %d not-yet-stacked chunk(s) of %s raw for the '
                    'stack pass', station_id, skipped, date_str)
    return kept


def process_night(station_id: str, date_str: str, cfg: dict,
                  should_abort: Callable[[], bool] | None = None, *,
                  on_encoded: Callable[[ChunkOutcome], None] | None = None) -> bool:
    """Re-encode all raw MKV chunks for a completed night (morning coppermind pass).

    Chunks are encoded in parallel using dawn_encode_parallelism workers
    (default: cpu_cores // 2), each worker getting cpu_cores // parallelism
    libx264 threads.  On a 6-core machine this gives 3 workers × 2 threads,
    fully utilising all cores vs the 1-core real-time encoding path.

    process_chunk() is idempotent — already-encoded chunks (including locked
    clips encoded by process_night_locked_only) are skipped automatically.

    ``should_abort`` is polled once per chunk, before its ffmpeg starts, so a
    caller with a deadline stops at a chunk boundary. Each chunk is encoded to a
    scratch file and renamed, so stopping never leaves a half-written clip in
    place of a good one.

    ``on_encoded`` is forwarded to process_chunk; dawn_process uses it to
    budget the pass. Returns False
    when ``should_abort`` stopped the pass early, True otherwise.
    """
    night_dir = rovimen_config.capture_root(cfg) / station_id / date_str
    if not night_dir.exists():
        logger.warning('[%s] coppermind: night dir not found: %s', station_id, night_dir)
        return True
    _sweep_stale_scratch(night_dir)

    chunks = sorted(night_dir.glob('*_color.mkv'))
    chunks = _drop_unstacked(station_id, date_str, chunks, cfg)
    bcfg = _batch_cfg(cfg)
    parallelism = bcfg['_encode_parallelism']
    logger.info('[%s] coppermind: processing %d chunks for %s (workers=%d, threads/chunk=%d)',
                station_id, len(chunks), date_str, parallelism, bcfg['_encode_threads'])
    chunk_kwargs: dict = {}
    if on_encoded is not None:
        chunk_kwargs['on_encoded'] = on_encoded

    def _one(mkv: Path) -> bool:
        if should_abort is not None and should_abort():
            return False
        return process_chunk(mkv, bcfg, **chunk_kwargs)

    ok_count = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_one, mkv): mkv for mkv in chunks}
        for fut in as_completed(futures):
            mkv = futures[fut]
            try:
                if fut.result():
                    ok_count += 1
            except Exception:
                logger.exception('[%s] coppermind failed for %s', station_id, mkv.name)
    if should_abort is not None and should_abort():
        logger.warning('[%s] coppermind stopped early: %d/%d ok for %s — '
                       'the rest are left for the next run',
                       station_id, ok_count, len(chunks), date_str)
        return False
    logger.info('[%s] coppermind complete: %d/%d ok for %s',
                station_id, ok_count, len(chunks), date_str)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description='VAAPI re-encode a chunk to disk')
    parser.add_argument('--chunk', required=True, help='Path to ramdisk .mkv chunk')
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
