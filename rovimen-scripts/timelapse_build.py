#!/usr/bin/env python3
"""timelapse_build.py — Per-night timelapse and night-stack builder.

Codename: bendalloy

Builds an MP4 timelapse from maxpixel stack WebPs and computes a per-pixel
maximum night stack from dark-sky thumbnails (20:00–02:00 UTC window).

Called by the morning processing sequence, or standalone:
    python timelapse_build.py -c config.json --station RO000H --date 20260315
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

import color_calibration as cc
import flags_manager
import rovimen_config
import rovimen_media
from ingest_blob import TUNNEL_MAX_BODY_BYTES

logger = logging.getLogger(__name__)


_CHUNK_STACK_RE = re.compile(r'^[A-Z0-9]+_\d{8}_\d{6}_stack\.webp$')
# The same shape for ffmpeg's -pattern_type glob (glob(3) bracket classes).
_CHUNK_STACK_GLOB = '*_' + '[0-9]' * 8 + '_' + '[0-9]' * 6 + '_stack.webp'

_TIMELAPSE_FPS = 25
# A timelapse over the tunnel's body cap never reaches the archive: at crf 18
# an RMS camera's night came out at 105-151 MB on DE001C (Johannismühle),
# MD0001 (Cahul) and RO0017 (Tărâța) in 2026-09. 90% of the cap leaves room
# for one VBV buffer and the container.
_TIMELAPSE_MAX_BYTES = TUNNEL_MAX_BODY_BYTES * 9 // 10
# One timelapse encode may take this long. A stalled ffmpeg (a hung USB
# capture disk) then fails this night's timelapse instead of holding dawn.
_ENCODE_TIMEOUT_S = 1800


def chunk_stacks(directory: Path) -> list[Path]:
    """Per-chunk stack WebPs in ``directory``, sorted by name.

    ``*_stack.webp`` also matches the night-level ``*_color_meteor_stack.webp``
    that ``stacker`` writes into the same folder; including it appends a
    max-of-differences frame to the timelapse and feeds it back into the night
    stack. Match the ``CAM_YYYYMMDD_HHMMSS_stack.webp`` shape instead.
    """
    return sorted(p for p in directory.glob('*_stack.webp')
                  if _CHUNK_STACK_RE.match(p.name))


def _size_cap_args(n_frames: int) -> list[str]:
    """ffmpeg rate-control flags that keep an ``n_frames`` timelapse under
    ``_TIMELAPSE_MAX_BYTES``.

    The crf still sets the quality; ``-maxrate`` only bites on a night whose
    crf bitrate would overshoot the budget. With a one-second buffer the file
    is at most the budget plus one second of video. ffmpeg refuses a rate over
    2^31 bit/s, which a clip of a few frames would ask for, so the rate is
    clamped at 100 Mbit/s; no crf 18 timelapse gets near it.
    """
    seconds = max(n_frames, 1) / _TIMELAPSE_FPS
    kbps = min(max(int(_TIMELAPSE_MAX_BYTES * 8 / seconds / 1000), 1), 100_000)
    return ['-maxrate', f'{kbps}k', '-bufsize', f'{kbps}k']


def _run_encode(cmd: list[str], station_id: str) -> int:
    """Run one timelapse encode; its exit code, -1 when it timed out (and
    ``subprocess.call`` killed it)."""
    try:
        return subprocess.call(cmd, timeout=_ENCODE_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        logger.error('[%s] Timelapse encode still running after %d s: killed',
                     station_id, _ENCODE_TIMEOUT_S)
        return -1


def _tmp_beside(out: Path) -> Path:
    """Hidden temp name next to ``out`` for an atomic write.

    The uploader ships whatever matches ``*_timelapse.mp4`` /
    ``*_night_stack.webp``; writing straight to those names let it upload a
    half-written file and flag the night as done. A dot-prefixed ``.tmp`` name
    matches neither glob.
    """
    return out.with_name(f'.{out.name}.{os.getpid()}.tmp')


def _publish(tmp: Path, out: Path, station_id: str) -> bool:
    """Move a finished temp file onto ``out`` (atomic on one filesystem)."""
    try:
        os.replace(tmp, out)
        return True
    except OSError as exc:
        logger.error('[%s] Could not publish %s: %s', station_id, out.name, exc)
        tmp.unlink(missing_ok=True)
        return False


def _parse_stack_time(name: str) -> datetime | None:
    """UTC chunk start from a stack filename (CAM_YYYYMMDD_HHMMSS[_...]_stack.webp)."""
    parts = name.split('_')
    if len(parts) >= 3:
        try:
            return datetime.strptime(
                parts[1] + parts[2], '%Y%m%d%H%M%S'
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class _AutoLevels:
    """EMA-smoothed percentile stretch for timelapse frames.

    A night spans bright moonlit cloud and near-black clear sky; a single
    fixed transfer curve washes one end out. Per-frame percentile bounds give
    every frame real contrast, and smoothing them with an EMA keeps the
    adaptation gradual so the timelapse doesn't pump between frames.
    """

    def __init__(self, alpha: float = 0.1):
        self._alpha = alpha
        self._lo: float | None = None
        self._hi: float | None = None

    def apply(self, img: np.ndarray) -> np.ndarray:
        lo = float(np.percentile(img, 1.0))
        hi = float(np.percentile(img, 99.7))
        if self._lo is None:
            self._lo, self._hi = lo, hi
        else:
            self._lo += self._alpha * (lo - self._lo)
            self._hi += self._alpha * (hi - self._hi)
        span = max(self._hi - self._lo, 16.0)
        out = (img.astype(np.float32) - self._lo) * (239.0 / span) + 8.0
        return np.clip(out, 0, 255).astype(np.uint8)


class _TimelapseBar:
    """The standard astromania/ROVIMEN bottom bar, rendered per frame in PIL.

    The clip encoder draws the same bar with ffmpeg drawtext keyed on a chunk
    epoch — correct for a real-time clip, but a timelapse frame's wall time
    jumps a whole chunk per frame, so the timestamp has to be drawn per frame.
    Geometry and assets come from the same ``overlay`` config block the clip
    bar uses (see overlay.py, the declared owner of the layout), so the two
    products stay visually consistent. The static parts (bar, station code,
    pointing, network wordmark, astromania logo) are pre-rendered once; only
    the timestamp is drawn per frame.
    """

    def __init__(self, overlay_cfg: dict, station_id: str, station_cfg: dict,
                 width: int):
        from overlay import (
            BAR_LOGO_MARGIN,
            NETWORK_MARK_ROW_SCALE,
            NETWORK_MARK_TOP_INSET,
            _base_bar_height,
            bar_height,
            bar_logo_scale_expr,
            effective_font_size,
            network_font_size,
            network_mark_crop,
            network_mark_path,
        )

        self.enabled = False
        font_path = overlay_cfg.get('font') or ''
        if not font_path or not Path(font_path).is_file():
            logger.warning('[%s] overlay font missing (%s) — timelapse bar skipped',
                           station_id, font_path)
            return
        # bar_height() is 0 for the 'standard' clip style, but the timelapse
        # always carries the bar — fall back to the same geometry the cinema
        # style would use.
        self.height = (
            bar_height(overlay_cfg, 0)
            or (_base_bar_height(overlay_cfg) + 15) // 16 * 16
        )
        self.width = width
        font_size = effective_font_size(overlay_cfg)
        opacity = float(overlay_cfg.get('text_opacity', 0.4))
        # The bar is pure black, so white-at-opacity compositing reduces to a
        # gray fill — no RGBA pass needed.
        self._fill = (int(255 * opacity),) * 3
        self._font = ImageFont.truetype(font_path, font_size)
        margin = 12

        base = Image.new('RGB', (width, self.height), (0, 0, 0))
        draw = ImageDraw.Draw(base)
        if overlay_cfg.get('show_station', True):
            draw.text((margin, 4), station_id, font=self._font, fill=self._fill)
        if overlay_cfg.get('show_coords', True) and overlay_cfg.get('coords'):
            draw.text((margin, 6 + font_size), str(overlay_cfg['coords']),
                      font=self._font, fill=self._fill)
        if overlay_cfg.get('show_pointing', True):
            az = station_cfg.get('az')
            alt = station_cfg.get('alt')
            if az is not None and alt is not None:
                txt = f'az {float(az):.0f}\N{DEGREE SIGN}  alt {float(alt):.0f}\N{DEGREE SIGN}'
                w = draw.textlength(txt, font=self._font)
                draw.text((width - margin - w, 4), txt,
                          font=self._font, fill=self._fill)
        mark = network_mark_path(overlay_cfg)
        if mark:
            # The ROVIMEN logo, sized and placed as in the clip bar so it leads.
            try:
                logo = Image.open(mark).convert('RGBA')
                row0_h = max(font_size, network_font_size(overlay_cfg))
                mh = int(round(row0_h * NETWORK_MARK_ROW_SCALE)) // 2 * 2
                logo = logo.resize((int(logo.width * mh / logo.height), mh), Image.LANCZOS)
                logo = logo.crop((0, network_mark_crop(mh), logo.width, mh))
                base.paste(logo, ((width - logo.width) // 2, NETWORK_MARK_TOP_INSET), logo)
            except Exception:
                logger.warning('network mark unreadable: %s', mark, exc_info=True)
                mark = None
        if not mark and overlay_cfg.get('show_network', True):
            net = str(overlay_cfg.get('network', 'ROVIMEN'))
            net_font = ImageFont.truetype(font_path, network_font_size(overlay_cfg))
            w = draw.textlength(net, font=net_font)
            draw.text(((width - w) // 2, 2), net, font=net_font, fill=self._fill)
        logo_path = overlay_cfg.get('logo') or ''
        if overlay_cfg.get('show_logo', True) and Path(logo_path).is_file():
            try:
                logo = Image.open(logo_path).convert('RGBA')
                # Bottom of the bar, centred, at the clip bar's size: a credit
                # under the ROVIMEN logo, never larger than it.
                target_h = int(bar_logo_scale_expr(overlay_cfg, self.height).split(':')[1])
                target_w = int(logo.width * target_h / logo.height)
                logo = logo.resize((target_w, target_h), Image.LANCZOS)
                base.paste(logo, ((width - target_w) // 2,
                                  self.height - target_h - BAR_LOGO_MARGIN), logo)
            except Exception:
                logger.warning('logo unreadable: %s', logo_path, exc_info=True)
        self._base = base
        self._ts_y = 6 + font_size
        self._margin = margin
        self._show_timestamp = overlay_cfg.get('show_timestamp', True)
        self.enabled = True

    def image(self, when: datetime | None = None) -> Image.Image:
        """The bar alone, with the timestamp for ``when`` (none if None)."""
        bar = self._base.copy()
        if self._show_timestamp and when is not None:
            draw = ImageDraw.Draw(bar)
            txt = when.strftime('%Y-%m-%d  %H:%M:%S UTC')
            w = draw.textlength(txt, font=self._font)
            draw.text((self.width - self._margin - w, self._ts_y), txt,
                      font=self._font, fill=self._fill)
        return bar

    def stamp(self, frame: np.ndarray, when: datetime | None) -> np.ndarray:
        """Return ``frame`` with the bar appended below (timestamp for ``when``)."""
        return np.vstack([frame, np.asarray(self.image(when), dtype=np.uint8)])


def _build_processed_timelapse(
    date_str: str, station_id: str, webps: list[Path], out: Path,
    station_cfg: dict, cfg: dict,
) -> Path | None:
    """Calibrated, bar-overlaid timelapse for colour-only (``rms: false``) cameras.

    Their raw stream carries no trustworthy in-camera white balance (IMX662,
    no IR-cut, static spectroscopy exposure), so the raw stacks come out flat
    and magenta. Each frame gets the same adaptive white balance +
    gamma/contrast the clip pipeline applies — one gain set derived from a
    mid-night frame, so the night doesn't flicker — then the standard
    astromania bottom bar with the per-frame chunk timestamp. The bar is
    APPENDED below the frame, so any consumer that crops to the sensor height
    (the dome projection does) never sees it.
    """
    tmp = _tmp_beside(out)
    import tempfile

    from color_calibration import (
        apply_calibration_np,
        resolve_calibration_from_frame,
        resolve_color_post,
    )
    _SKY_H = rovimen_media.CAPTURE_HEIGHT

    def _sky(arr: np.ndarray) -> np.ndarray:
        # The stacker composites its own bottom bar onto the stack WebPs
        # (stack height = sky HEIGHT + bar). Strip it by the same convention
        # the stacker uses, so the calibration never samples black bar pixels
        # and the fresh bar below isn't stacked under a stale one (the burned
        # bar predates the camera's pointing config — it reads ALT 0.0 AZ 0.0).
        return arr[:_SKY_H] if arr.shape[0] > _SKY_H else arr

    reference: np.ndarray | None = None
    for cand in (webps[len(webps) // 2], webps[len(webps) // 4], webps[0]):
        try:
            reference = _sky(np.asarray(Image.open(cand).convert('RGB')))
            break
        except Exception:
            continue
    if reference is None:
        logger.warning('[%s] No readable stack for calibration reference — %s',
                       station_id, date_str)
        return None
    # Sky-matched stacks (the fleet default) already carry the final look; a
    # second calibration or an auto-levels stretch on top would undo it.
    adaptive = cc.adaptive_enabled(station_cfg, 'stack')
    if adaptive:
        gr = gg = gb = gamma = contrast = 1.0
        levels = None
        logger.info('[%s] Timelapse from sky-matched stacks: no second calibration',
                    station_id)
    else:
        gr, gg, gb, gamma = resolve_calibration_from_frame(reference, station_cfg)
        post = resolve_color_post(station_cfg)
        contrast = float(post.get('contrast', 1.0))
        logger.info('[%s] Timelapse calibration: gains %.2f/%.2f/%.2f gamma %.2f '
                    'contrast %.2f', station_id, gr, gg, gb, gamma, contrast)
        levels = _AutoLevels() if post.get('stretch', True) else None
    bar = _TimelapseBar(cfg.get('overlay') or {}, station_id, station_cfg,
                        width=reference.shape[1])

    with tempfile.TemporaryDirectory(prefix='rovimen_tl_') as td:
        frames_dir = Path(td)
        idx = 0
        for wp in webps:
            try:
                img = _sky(np.asarray(Image.open(wp).convert('RGB')))
            except Exception:
                logger.warning('[%s] Skipping unreadable stack: %s',
                               station_id, wp.name)
                continue
            if not adaptive:
                img = apply_calibration_np(img, gr, gg, gb, gamma, contrast=contrast)
            if levels is not None:
                img = levels.apply(img)
            if bar.enabled:
                if img.shape[1] != bar.width:
                    img = np.asarray(
                        Image.fromarray(img).resize(
                            (bar.width,
                             int(img.shape[0] * bar.width / img.shape[1])),
                            Image.LANCZOS,
                        )
                    )
                img = bar.stamp(img, _parse_stack_time(wp.name))
            Image.fromarray(img).save(frames_dir / f'{idx:06d}.png')
            idx += 1
        if idx == 0:
            logger.warning('[%s] No frames survived processing for %s',
                           station_id, date_str)
            return None
        cmd = [
            'ffmpeg', '-hide_banner', '-loglevel', 'error',
            '-framerate', '25',
            '-i', str(frames_dir / '%06d.png'),
            # hqdn3d: the auto-levels stretch amplifies sensor/webp noise,
            # and at crf 18 that ballooned a night to 107 MB — past the
            # 100 MB Cloudflare request cap the ingest push runs through
            # (GH662S/20260915 failed to upload). Denoise + crf 23 lands the
            # same night at a streamable size with the structure intact.
            # pad: yuv420p needs even dimensions; the appended bar can make
            # the height odd.
            '-vf', 'hqdn3d=3:2:6:4,pad=ceil(iw/2)*2:ceil(ih/2)*2',
            '-c:v', 'libx264', '-crf', '23', *_size_cap_args(idx),
            '-pix_fmt', 'yuv420p',
            '-movflags', '+faststart',
            '-f', 'mp4', '-y', str(tmp),
        ]
        rc = _run_encode(cmd, station_id)
    if rc == 0 and _publish(tmp, out, station_id):
        logger.info('[%s] Processed timelapse saved: %s (%d frames)',
                    station_id, out.name, idx)
        return out
    tmp.unlink(missing_ok=True)
    logger.error('[%s] Processed timelapse build failed (rc=%d) for %s',
                 station_id, rc, date_str)
    return None


def build_timelapse(date_str: str, station_id: str, stacks_dir: Path,
                    out_dir: Path, *, rotate: bool = False,
                    station_cfg: dict | None = None,
                    cfg: dict | None = None) -> Path | None:
    """Build MP4 timelapse from stacks. Returns output path on success, None on failure.

    When rotate is True, applies vflip+hflip (180-degree rotation) to match
    the camera's physical orientation.  Stack images saved by stacker.py are
    already rotated, so this flag is only needed when stacks were produced
    from raw (unrotated) frames — kept as a safety net.
    """
    webps = chunk_stacks(stacks_dir)
    if not webps:
        logger.warning('[%s] No frames for timelapse: %s', station_id, date_str)
        return None
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f'{station_id}_{date_str}_timelapse.mp4'

    # Colour-only cameras get the calibrated + bar-overlaid build; RMS cameras
    # keep the plain fast path (their stacks are already presentable).
    if station_cfg is not None and not station_cfg.get('rms', True):
        logger.info('[%s] Building processed timelapse: %d frames → %s',
                    station_id, len(webps), out.name)
        return _build_processed_timelapse(
            date_str, station_id, webps, out, station_cfg, cfg or {},
        )
    logger.info('[%s] Building timelapse: %d frames → %s', station_id, len(webps), out.name)

    vf_filters: list[str] = []
    if rotate:
        vf_filters.extend(['vflip', 'hflip'])

    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'image2', '-pattern_type', 'glob',
        '-framerate', '25',
        # The chunk_stacks() shape as a glob: '*_stack.webp' also matches the
        # night's colour meteor composite, which then became the last frame.
        '-i', str(stacks_dir / _CHUNK_STACK_GLOB),
    ]
    if vf_filters:
        cmd += ['-vf', ','.join(vf_filters)]
    tmp = _tmp_beside(out)
    cmd += [
        '-c:v', 'libx264', '-crf', '18', *_size_cap_args(len(webps)),
        '-pix_fmt', 'yuv420p',
        '-movflags', '+faststart',
        '-f', 'mp4', '-y', str(tmp),
    ]
    rc = _run_encode(cmd, station_id)
    if rc == 0 and _publish(tmp, out, station_id):
        logger.info('[%s] Timelapse saved: %s', station_id, out.name)
        return out
    tmp.unlink(missing_ok=True)
    logger.error('[%s] Timelapse build failed (rc=%d) for %s', station_id, rc, date_str)
    return None


def _thumb_sky(img: Image.Image) -> Image.Image:
    """The sky of a chunk-stack thumbnail, without the bar under it.

    A thumbnail is the published stack (a ``CAPTURE_WIDTH`` x
    ``CAPTURE_HEIGHT`` sky with the OSD bar and its chunk clock appended
    below) scaled to the thumbnail width, so the sky is its top
    ``width * CAPTURE_HEIGHT / CAPTURE_WIDTH`` rows; anything shorter has no
    bar.
    """
    sky_h = max(1, round(img.width * rovimen_media.CAPTURE_HEIGHT
                         / rovimen_media.CAPTURE_WIDTH))
    return img.crop((0, 0, img.width, sky_h)) if img.height > sky_h else img


def _night_stack_bar(overlay_cfg: dict | None, station_id: str,
                     station_cfg: dict | None, width: int) -> np.ndarray | None:
    """The chunk stacks' bottom bar without a clock, scaled to ``width``.

    Rendered at the stack's own width and scaled down as its thumbnails are,
    so it has the size and layout of the bar the thumbnails carried. None when
    the stacks carry no bar (overlay off, or not the cinema style) or the
    overlay font is missing.
    """
    if not overlay_cfg or not overlay_cfg.get('enabled', True):
        return None
    from overlay import bar_height

    if bar_height(overlay_cfg, 0) == 0:
        return None
    bar = _TimelapseBar(overlay_cfg, station_id, station_cfg or {},
                        width=rovimen_media.CAPTURE_WIDTH)
    if not bar.enabled:
        return None
    img = bar.image(None)
    rows = max(1, round(img.height * width / img.width))
    return np.asarray(img.resize((width, rows), Image.LANCZOS), dtype=np.uint8)


def build_night_stack(date_str: str, station_id: str, thumbs_dir: Path,
                      out_dir: Path, station_cfg: dict | None = None,
                      overlay_cfg: dict | None = None) -> None:
    """Compute per-pixel maximum of dark-sky thumbnails (20:00–02:00 UTC).

    The max runs over each thumbnail's sky only: the bar under it carries
    that chunk's UTC clock, and a max over a night of them printed every
    clock on top of each other. The finished sky gets one static bar (no
    clock) from ``overlay_cfg``, the same bar the processed timelapse uses.
    """
    # bendalloy: overnight accumulation
    all_thumbs = chunk_stacks(thumbs_dir)
    thumbs = []
    for t in all_thumbs:
        parts = t.stem.split('_')
        if len(parts) >= 3:
            hhmm = int(parts[2][:4])
            if hhmm >= 2000 or hhmm <= 200:
                thumbs.append(t)
    if not thumbs:
        logger.warning('[%s] No thumbnails in 20:00–02:00 window for night stack: %s',
                       station_id, date_str)
        return
    logger.info('[%s] Building night stack: %d/%d thumbnails → %s',
                station_id, len(thumbs), len(all_thumbs), date_str)
    try:
        acc: np.ndarray | None = None
        h = w = 0
        skipped = 0
        for t in thumbs:
            try:
                img = _thumb_sky(Image.open(t).convert('RGB'))
            except Exception:
                logger.warning('[%s] Skipping unreadable thumbnail: %s', station_id, t.name)
                skipped += 1
                continue
            if acc is None:
                h, w = img.height, img.width
                acc = np.zeros((h, w, 3), dtype=np.uint8)
            elif img.size != (w, h):
                img = img.resize((w, h), Image.LANCZOS)
            acc = np.maximum(acc, np.array(img, dtype=np.uint8))
        if acc is None:
            logger.warning('[%s] All %d thumbnails unreadable for %s',
                           station_id, len(thumbs), date_str)
            return
        if skipped:
            logger.warning('[%s] Skipped %d/%d unreadable thumbnails for %s',
                           station_id, skipped, len(thumbs), date_str)
        # The contributing stacks are each white-balanced against their own
        # 20 s of sky, but per-pixel max is not linear: it lifts the sky to its
        # brightest value of the night and any residual cast rides up with it.
        # Re-neutralise the finished composite (white balance only — its tone
        # curve is already baked in).
        acc = cc.rebalance_composite(acc, station_cfg or {})
        try:
            bar = _night_stack_bar(overlay_cfg, station_id, station_cfg, w)
        except Exception:
            # The sky alone is still the product; never lose it to the bar.
            logger.warning('[%s] Night stack bar failed for %s, publishing the sky only',
                           station_id, date_str, exc_info=True)
            bar = None
        if bar is not None:
            acc = np.vstack([acc, bar])

        out = out_dir / f'{station_id}_{date_str}_night_stack.webp'
        tmp = _tmp_beside(out)
        Image.fromarray(acc).save(str(tmp), format='WEBP', lossless=True)
        if _publish(tmp, out, station_id):
            logger.info('[%s] Night stack saved: %s', station_id, out.name)
    except Exception:
        logger.exception('[%s] Night stack build failed for %s', station_id, date_str)


def build(station_id: str, date_str: str, cfg: dict) -> None:
    """Build timelapse + night stack for a station/date.

    Called by the morning processing sequence after stacker.process_night()
    has run. Reads stacks from videocapture_path/STATION/DATE/stacks/.
    Skips immediately if stacker is disabled in config (no stacks to assemble).
    """
    services = cfg.get('services', {})
    stacker_enabled = services.get('stacker', {}).get('enabled', True)

    if not stacker_enabled:
        logger.info('[%s] stacker disabled — skipping timelapse build for %s',
                    station_id, date_str)
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)
        return

    capture_path = rovimen_config.capture_root(cfg)
    stacks_dir = capture_path / station_id / date_str / 'stacks'
    thumbs_dir = stacks_dir / 'thumbs'
    tl_dir = capture_path / station_id / date_str

    if not stacks_dir.exists() or not any(stacks_dir.glob('*_stack.webp')):
        logger.warning('[%s] No stacks found in %s — timelapse skipped', station_id, stacks_dir)
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)
        return

    result = build_timelapse(
        date_str, station_id, stacks_dir, tl_dir,
        station_cfg=cfg.get('stations', {}).get(station_id, {}), cfg=cfg,
    )
    if result is not None:
        flags_manager.mark_timelapse_done(station_id, date_str, cfg)

    if thumbs_dir.exists():
        build_night_stack(date_str, station_id, thumbs_dir, tl_dir,
                          station_cfg=cfg.get('stations', {}).get(station_id, {}),
                          overlay_cfg=cfg.get('overlay'))


def main() -> int:
    parser = argparse.ArgumentParser(
        description='Build timelapse and night stack for a station/date'
    )
    parser.add_argument('-c', '--config', required=True, help='Path to config.json')
    parser.add_argument('--station', required=True, help='Station ID, e.g. RO000H')
    parser.add_argument('--date', required=True, help='Night date YYYYMMDD')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s [%(levelname)s] %(message)s')

    with open(args.config) as f:
        cfg = json.load(f)

    build(args.station, args.date, cfg)
    return 0


if __name__ == '__main__':
    sys.exit(main())
