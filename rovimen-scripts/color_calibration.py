"""Station-side color calibration.

Per-chunk adaptive white balance targeting a universal aesthetic night-sky
colour. Rationale: the raw IR-cut-removed sensor sees strong magenta bias;
pure gray-world correction drives the sky to neutral grey, which looks dull
and unnatural. Targeting a mildly blue-magenta sky colour matches what a
clean-IR sensor would see and matches what the human eye expects at night.

Per-camera differences (sensor variant, scene, light pollution) are absorbed
by the adaptive step: each chunk's own sky pixels drive the gains, so the
same target produces a consistent look across the fleet.

``color_post`` in ``station_configs/<host>/config.json`` can override the
universal target + gamma on a per-camera basis (rare — IMX662 cameras on
mixed fleets, or heavy-light-pollution outliers).

Each product can then override the camera's ``color_post`` on its own, because
one set of values cannot suit all of them: a chunk stack is a per-pixel max over
the chunk, so its sky sits brighter and its noise lower than any single video
frame, and a night composite is a max over a whole night of already tone-mapped
stacks. ``color_post_video`` (the encoded clip), ``color_post_stack`` (per-chunk
stacks) and ``color_post_night`` (night composites) each merge over
``color_post``; only the keys they set change. See ``resolve_color_post``.

Usage (numpy)::

    gr, gg, gb, gamma = resolve_calibration_from_frame(reference, station_cfg)
    out = apply_calibration_np(image, gr, gg, gb, gamma, rotate=False)

Usage (ffmpeg)::

    gr, gg, gb, gamma = resolve_calibration_from_mkv(mkv, station_cfg)
    vf = build_ffmpeg_filter(gr, gg, gb, gamma, rotate=False)

Everything above is the LEGACY pipeline. The fleet default since 2026-09-24 is
the adaptive "sky match" at the bottom of this module (``ADAPTIVE_DEFAULTS``,
``resolve_adaptive_curve``): a per-chunk, per-channel curve fitted so each
chunk's own sky lands on one target colour, with the brightness lifted for
moonlight and twilight. A camera goes back to the legacy pipeline only with
``color_adaptive: {"enabled": false}``.
"""

from __future__ import annotations

import logging
import math
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np

import rovimen_media

logger = logging.getLogger(__name__)


# Universal target for the calibrated night sky, as an R:G:B ratio with G
# normalised to 1.0. (1, 1, 1) is true gray-world: the adaptive step pulls
# each camera's mid-luma sky pixels to neutral grey, regardless of how much
# IR cast the sensor has. The previous (1.44, 1.00, 1.75) target was the raw
# IR-contaminated sky ratio itself, which caused the adaptive gains to be
# near-identity and let the magenta cast survive into maxpixel stacks.
FLEET_TARGET_RGB: tuple[float, float, float] = (1.00, 1.00, 1.00)

# Universal tone-curve gamma applied after gains. <1 = midtone lift.
FLEET_GAMMA: float = 0.75

# Highlight-protect knee — smoothstep fade of per-channel gains toward unity
# between knee_low and knee_high luma. Lowering the knee preserves more of
# the bright meteor trail / star core; raising it lets the gain bite harder
# into the highlights (useful when a station's WB drift contaminates them).
FLEET_HIGHLIGHT_KNEE_LOW:  float = 0.65
FLEET_HIGHLIGHT_KNEE_HIGH: float = 0.95

# Linear contrast multiplier applied after gamma. 1.0 = identity. 1.10 lifts
# contrast by 10 % around 0.5-luma pivot. Stations with a bright sky floor
# benefit from a small bump (~1.05–1.10).
FLEET_CONTRAST: float = 1.0

# Vibrance applied after contrast: saturation boost weighted toward
# low-saturation pixels, so a faint sky tint gains colour while already
# saturated meteor trails and lights stay put. 0.0 = off (fleet default);
# per-camera values from the 2026-09-18 fleet review sit in 0.01–0.10.
# Boost model: sat' = sat * (1 + vibrance * (1 - sat) * 1.6), applied as a
# per-pixel scale around Rec.709 luma.
FLEET_VIBRANCE: float = 0.0

# Default ``color_post`` when a camera doesn't override anything. Identity-ish
# wrt the adaptive base — the actual aesthetic is baked into FLEET_TARGET_RGB,
# not into multipliers.
FLEET_DEFAULT_COLOR_POST: dict[str, float] = {
    'target_r':   FLEET_TARGET_RGB[0],
    'target_g':   FLEET_TARGET_RGB[1],
    'target_b':   FLEET_TARGET_RGB[2],
    'gamma':      FLEET_GAMMA,
    'contrast':   FLEET_CONTRAST,
    'knee_low':   FLEET_HIGHLIGHT_KNEE_LOW,
    'knee_high':  FLEET_HIGHLIGHT_KNEE_HIGH,
    'vibrance':   FLEET_VIBRANCE,
}


def derive_adaptive_gains(
    image: np.ndarray,
    target_rgb: tuple[float, float, float] = FLEET_TARGET_RGB,
    percentile_low: float = 10.0,
    percentile_high: float = 85.0,
    gain_ceiling: float = 1.0,
) -> tuple[float, float, float]:
    """Derive per-channel gains that drive the image's sky pixels toward
    ``target_rgb`` (an R:G:B ratio).

    Sky pixels are selected by excluding the darkest ``percentile_low``%
    (noise floor) and the brightest ``100 - percentile_high``% (stars, trails,
    horizon lights). The mean of the remaining pixels per channel is mapped
    onto the target ratio.

    With ``gain_ceiling=1.0`` (default) the final gains are rescaled so the
    largest equals 1.0 — attenuation only, no headroom overflow.
    """
    if image.ndim != 3 or image.shape[2] != 3:
        raise ValueError(f'expected HxWx3 RGB, got shape {image.shape}')

    luma = image.astype(np.float32).mean(axis=2)
    lo = np.percentile(luma, percentile_low)
    hi = np.percentile(luma, percentile_high)
    mask = (luma >= lo) & (luma <= hi)
    pixels = image[mask].astype(np.float32)
    if pixels.size == 0:
        pixels = image.reshape(-1, 3).astype(np.float32)

    mean_r = float(pixels[:, 0].mean()) or 1.0
    mean_g = float(pixels[:, 1].mean()) or 1.0
    mean_b = float(pixels[:, 2].mean()) or 1.0

    tr, tg, tb = target_rgb
    gr, gg, gb = tr / mean_r, tg / mean_g, tb / mean_b

    # Target is a ratio, not a brightness; rescale so max gain == ceiling.
    # (Without this the absolute gains would follow target/mean which is tiny.)
    if gain_ceiling and gain_ceiling > 0:
        mx = max(gr, gg, gb)
        if mx > 0:
            s = gain_ceiling / mx
            gr, gg, gb = gr * s, gg * s, gb * s

    return gr, gg, gb


# Products whose colour settings can diverge from the camera's color_post.
COLOR_POST_PRODUCTS: tuple[str, ...] = ('video', 'stack', 'night')

# A night composite is assembled from stacks whose tone curve is already baked
# in, so its override is a SECOND pass applied on top: identity by default, and
# gamma/contrast/vibrance here are relative to the stacks, not absolute.
NIGHT_TONE_IDENTITY: dict[str, float] = {'gamma': 1.0, 'contrast': 1.0, 'vibrance': 0.0}


def resolve_color_post(station_cfg: dict, product: str | None = None) -> dict:
    """Return the camera's color_post merged over the fleet default, then the
    ``color_post_<product>`` override over that, so the caller never has to
    check for absence. ``product`` is one of COLOR_POST_PRODUCTS or None (the
    base values, for callers with no product of their own)."""
    post = {**FLEET_DEFAULT_COLOR_POST, **(station_cfg.get('color_post') or {})}
    if product is not None:
        if product not in COLOR_POST_PRODUCTS:
            raise ValueError(f'unknown colour product {product!r}')
        post.update(station_cfg.get(f'color_post_{product}') or {})
    return post


def resolve_calibration_from_frame(
    reference: np.ndarray,
    station_cfg: dict,
    product: str | None = None,
) -> tuple[float, float, float, float]:
    """Derive adaptive gains for ``reference`` using the camera's target
    (falls back to fleet default), return ``(gain_r, gain_g, gain_b, gamma)``.
    Ready to hand to ``apply_calibration_np`` or ``build_ffmpeg_filter``.
    """
    post = resolve_color_post(station_cfg, product)
    target = (float(post['target_r']), float(post['target_g']), float(post['target_b']))
    gr, gg, gb = derive_adaptive_gains(reference, target_rgb=target)
    return gr, gg, gb, float(post['gamma'])


def _apply_gains_highlight_protect(
    image: np.ndarray,
    gr: float,
    gg: float,
    gb: float,
    knee_low: float = FLEET_HIGHLIGHT_KNEE_LOW,
    knee_high: float = FLEET_HIGHLIGHT_KNEE_HIGH,
) -> np.ndarray:
    """Apply per-channel gains with smoothstep fade toward unity in highlights
    so bright stars and meteor trails don't pick up a tint."""
    out = image.astype(np.float32)
    # Compute luma reference from a separate array — Python 3.14's JIT
    # miscompiles the in-place ``out *= factor`` below when ``img01`` is
    # derived from ``out`` (both alias the same base buffer to the JIT),
    # silently replacing ``out`` values with the 0–1 normalised scale.
    img01 = image.astype(np.float32) / 255.0
    luma = 0.2126 * img01[:, :, 0] + 0.7152 * img01[:, :, 1] + 0.0722 * img01[:, :, 2]
    t = np.clip((luma - knee_low) / max(knee_high - knee_low, 1e-6), 0.0, 1.0)
    w = 1.0 - (t * t * (3.0 - 2.0 * t))  # smoothstep fade-out
    out[:, :, 0] *= 1.0 + w * (gr - 1.0)
    out[:, :, 1] *= 1.0 + w * (gg - 1.0)
    out[:, :, 2] *= 1.0 + w * (gb - 1.0)
    return np.clip(out, 0, 255).astype(np.uint8)


def apply_calibration_np(
    image: np.ndarray,
    gr: float,
    gg: float,
    gb: float,
    gamma: float,
    rotate: bool = False,
    contrast: float = FLEET_CONTRAST,
    knee_low: float = FLEET_HIGHLIGHT_KNEE_LOW,
    knee_high: float = FLEET_HIGHLIGHT_KNEE_HIGH,
    vibrance: float = FLEET_VIBRANCE,
) -> np.ndarray:
    """Apply gains (with highlight-protect), gamma, optional contrast bump,
    optional vibrance, and optional 180° rotation.

    Gamma uses the numpy convention ``(x/255)^gamma``: gamma < 1 lifts midtones.
    Contrast is a linear multiplier around the 0.5-luma pivot: 1.0 = identity,
    1.10 = +10 % contrast. Vibrance scales chroma away from Rec.709 luma with a
    boost of ``1 + vibrance * (1 - sat) * 1.6`` — strongest on near-neutral
    pixels, fading to nothing on fully saturated ones.
    """
    out = _apply_gains_highlight_protect(image, gr, gg, gb, knee_low, knee_high)
    if gamma and abs(gamma - 1.0) > 1e-3:
        f = (out.astype(np.float32) / 255.0) ** float(gamma)
        out = (np.clip(f, 0, 1) * 255).astype(np.uint8)
    if contrast and abs(contrast - 1.0) > 1e-3:
        f = out.astype(np.float32) / 255.0
        f = (f - 0.5) * float(contrast) + 0.5
        out = (np.clip(f, 0, 1) * 255).astype(np.uint8)
    if vibrance and vibrance > 1e-3:
        f = out.astype(np.float32)
        mx = f.max(axis=2)
        mn = f.min(axis=2)
        sat = np.where(mx > 8.0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
        boost = 1.0 + float(vibrance) * (1.0 - sat) * 1.6
        luma = 0.2126 * f[:, :, 0] + 0.7152 * f[:, :, 1] + 0.0722 * f[:, :, 2]
        f = luma[:, :, None] + (f - luma[:, :, None]) * boost[:, :, None]
        out = np.clip(f, 0, 255).astype(np.uint8)
    if rotate:
        out = np.rot90(out, k=2)
    return out


def rebalance_composite(
    image: np.ndarray,
    station_cfg: dict,
    min_signal: float = 6.0,
) -> np.ndarray:
    """Second-pass white balance for a night-level composite.

    Night products (``np.maximum`` over a night of chunk stacks, or the
    ``max - avg`` meteor composite) are assembled from stacks that
    ``apply_calibration_np`` already tone-mapped. Re-running the full curve on
    the composite would apply gamma, contrast and vibrance a second time, so
    this pass corrects **white balance** (plus the optional ``color_post_night``
    tone pass, identity unless set): gains derived from the
    composite's own percentile-masked sky, applied with the camera's highlight
    knees so star and meteor trails keep their colour.

    A composite needs its own white balance because per-pixel max is not a
    linear operator: every pixel is lifted to its brightest value of the night
    (measured 1.2-3.1x the per-chunk sky luma), and a residual cast that reads
    as near-black at chunk level is a visible colour wash at composite level.

    Returns the image unchanged when the masked sky carries less than
    ``min_signal`` mean level (0-255) — a near-black difference image has no
    meaningful grey point to pull toward.
    """
    if image.ndim != 3 or image.shape[2] != 3:
        return image

    luma = image.astype(np.float32).mean(axis=2)
    lo, hi = np.percentile(luma, 10.0), np.percentile(luma, 85.0)
    mask = (luma >= lo) & (luma <= hi)
    sel = image[mask] if mask.any() else image.reshape(-1, 3)
    if sel.size == 0 or float(sel.astype(np.float32).mean()) < min_signal:
        logger.debug('composite below %.1f mean signal — white balance skipped', min_signal)
        return image

    post = resolve_color_post(station_cfg)
    if adaptive_enabled(station_cfg, 'stack'):
        # Sky-matched stacks already carry the adaptive look, so the composite
        # is pulled back to the same sky ratio — white balance only. The legacy
        # color_post_night targets and tone pass were tuned for the old stacks.
        params = resolve_adaptive(station_cfg, 'stack')
        gr, gg, gb = derive_adaptive_gains(
            image, target_rgb=(float(params['r_g']), 1.0, float(params['b_g'])))
        return _apply_gains_highlight_protect(
            image, gr, gg, gb, float(post['knee_low']), float(post['knee_high']))
    night = station_cfg.get('color_post_night') or {}
    # The night override's target keys steer this white balance; its tone keys
    # (gamma/contrast/vibrance) are a second pass relative to the stacks.
    target = tuple(float(night.get(k, post[k])) for k in ('target_r', 'target_g', 'target_b'))
    tone = {**NIGHT_TONE_IDENTITY, **{k: night[k] for k in NIGHT_TONE_IDENTITY if k in night}}
    gr, gg, gb = derive_adaptive_gains(image, target_rgb=target)
    return apply_calibration_np(
        image, gr, gg, gb,
        gamma=float(tone['gamma']), rotate=False, contrast=float(tone['contrast']),
        vibrance=float(tone['vibrance']),
        knee_low=float(post['knee_low']), knee_high=float(post['knee_high']),
    )


def build_ffmpeg_filter(
    gr: float,
    gg: float,
    gb: float,
    gamma: float,
    rotate: bool = False,
    contrast: float = FLEET_CONTRAST,
    vibrance: float = FLEET_VIBRANCE,
) -> str:
    """Filter chain for ffmpeg: per-channel gain → gamma → contrast → vibrance
    → optional 180° flip. This is the single source of truth for the video
    colour-post so the encoder and the numpy stack (``apply_calibration_np``)
    apply the same gain, gamma and contrast — matching clip colour to its
    stack thumbnail.

    Vibrance rides on ffmpeg's own ``vibrance`` filter with
    ``intensity = vibrance * 1.6``. Its saturation weighting is not
    bit-identical to the numpy boost model, but both scale chroma toward
    low-saturation pixels with the same first-order strength; the residual
    difference is bounded and far below the highlight-protect mismatch
    already accepted for this path (see below and issue #508).

    Note on gamma: ffmpeg's ``eq=gamma=G`` uses ``pow(val/255, 1/G) * 255``,
    the *inverse* of the numpy convention. We invert here so the semantics
    match ``apply_calibration_np``.

    ffmpeg ``eq=contrast=X`` already uses 1.0 = identity, so the value is
    passed through unchanged. Combining gamma and contrast into a single
    ``eq=`` filter (instead of chaining two) is cheaper.

    Highlight-protect (the smoothstep gain fade in ``apply_calibration_np``)
    is intentionally NOT emitted here: with attenuation-only gains it touches
    only star cores and meteor trails, not the sky colour, and reproducing it
    faithfully in ffmpeg needs a per-pixel ``geq``/``maskedmerge`` mask that
    would not compose cleanly with the encoder's VAAPI/logo graph. The
    residual is bounded to the brightest pixels; see issue #508.
    """
    parts: list[str] = []
    # Skip a near-identity gain node: colorchannelmixer at rr=gg=bb=1 is a
    # no-op and just adds a filter-graph hop (matches apply_calibration_np,
    # where identity gains leave the image untouched).
    if not (abs(gr - 1.0) < 1e-3 and abs(gg - 1.0) < 1e-3 and abs(gb - 1.0) < 1e-3):
        parts.append(f'colorchannelmixer=rr={gr:.4f}:gg={gg:.4f}:bb={gb:.4f}')
    eq_terms: list[str] = []
    if gamma and abs(gamma - 1.0) > 1e-3:
        eq_terms.append(f'gamma={1.0 / float(gamma):.4f}')
    if contrast and abs(contrast - 1.0) > 1e-3:
        eq_terms.append(f'contrast={float(contrast):.4f}')
    if eq_terms:
        parts.append('eq=' + ':'.join(eq_terms))
    if vibrance and vibrance > 1e-3:
        parts.append(f'vibrance=intensity={float(vibrance) * 1.6:.4f}')
    if rotate:
        parts.append('vflip,hflip')  # 180°
    return ','.join(parts)


SAMPLE_TIMEOUT_S = 60.0


def sample_avg_frame(mkv: Path, n_samples: int = 5, full_range: bool = False) -> np.ndarray | None:
    """Decode up to ``n_samples`` evenly-spaced frames from an MKV and return
    their per-pixel mean. Used by encoders that don't stream all frames
    through numpy to build a sky reference for adaptive derivation.
    """
    # Timeouts: a hung disk (the UAS hang on gmnro03 (Ciocârlia)) otherwise
    # blocks the caller, and with it dawn, indefinitely. -count_frames decodes
    # the whole chunk, as does the sampling decode.
    meta = rovimen_media.first_stream(rovimen_media.ffprobe_json(
        mkv, timeout=SAMPLE_TIMEOUT_S, args=('-count_frames',),
        entries='stream=width,height,nb_read_frames,duration'))
    try:
        w, h = int(meta['width']), int(meta['height'])
    except (KeyError, TypeError, ValueError):
        logger.warning('ffprobe failed or timed out (%.0fs) on %s', SAMPLE_TIMEOUT_S, mkv)
        return None
    nb = int(meta.get('nb_read_frames') or 0)
    if nb <= 0:
        try:
            nb = max(1, int(float(meta.get('duration') or 0) * 25))
        except ValueError:
            nb = 500

    stride = max(1, nb // max(1, n_samples))
    # Force full-range interpretation for cameras that mis-tag full-range data
    # as limited (color_range=tv) — otherwise the reference frame is crushed and
    # the derived gains are wrong. No-op for already-full (pc) streams.
    range_prefix = 'scale=in_range=full:out_range=full,' if full_range else ''
    try:
        raw = subprocess.run(
            ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-i', str(mkv),
             '-vf', f'{range_prefix}select=not(mod(n\\,{stride})),format=rgb24',
             '-vsync', 'vfr', '-f', 'rawvideo', '-'],
            capture_output=True, check=False, timeout=SAMPLE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        logger.warning('frame sampling timed out after %.0fs on %s', SAMPLE_TIMEOUT_S, mkv)
        return None
    if raw.returncode != 0 or not raw.stdout:
        return None
    frame_bytes = w * h * 3
    got = len(raw.stdout) // frame_bytes
    if got == 0:
        return None
    frames = np.frombuffer(raw.stdout[:got * frame_bytes], dtype=np.uint8).reshape(got, h, w, 3)
    return frames.mean(axis=0).astype(np.uint8)


def resolve_calibration_from_mkv(
    mkv: Path,
    station_cfg: dict,
    full_range: bool = False,
    product: str | None = None,
) -> tuple[float, float, float, float] | None:
    """Encoder-side helper: sample the MKV, derive adaptive gains, return
    ``(gain_r, gain_g, gain_b, gamma)``. Returns None if sampling failed.

    ``full_range`` forces full-range YUV interpretation for cameras that flag
    full-range data as limited (see ``sample_avg_frame``)."""
    ref = sample_avg_frame(mkv, n_samples=5, full_range=full_range)
    if ref is None:
        return None
    return resolve_calibration_from_frame(ref, station_cfg, product)


# ---------------------------------------------------------------------------
# Adaptive, reference-matched colour ("sky match") — the fleet default
# ---------------------------------------------------------------------------
#
# The gains pipeline above left the look to fixed per-camera numbers: gains
# came from one reference (the encoder froze them for a whole night), gamma and
# contrast were constants, and ffmpeg's ``eq`` contrast acts on luma alone, so
# on a dark sky it roughly doubled whatever cast the gains left. Without an
# IR-cut filter the raw sky colour swings through a night — moon up or down,
# twilight, cloud — so no fixed curve holds the look.
#
# Sky match fits, per chunk, per-channel white-balance GAINS and ONE tone
# exponent g shared by all three channels:
# ``out_c = 255 * (h_c(x) / 255) ** g`` with ``h_c(x) = gain_c * x`` below a
# knee, blending to ``h_c(255) = 255`` above it. The fit lands the mean of the
# chunk's own sky pixels (luma between its 10th and 85th percentile) on one
# target colour, then chroma is scaled by ``saturation``. The largest gain is
# 1.0 (white balance only attenuates, so nothing new clips), and the knee keeps
# 255 at 255, so saturated star cores and meteor trails stay white.
#
# Until 2026-09-25 each channel had its OWN exponent (up to 3.0). Matching the
# sky with three different exponents is exact only at the sky's brightness: a
# power curve raises a pixel's colour ratio to (roughly) its exponent, so with
# the near-full moon (raw sky 130-240 against a target of ~64, exponents 2-3)
# the sky average landed on target while moonlit cloud came out pink/magenta
# (RO000T 3.00/2.76/2.71: sky r/g 0.92 on target, brightest 10% at 233/165/198).
# With one shared exponent a pixel keeps the sky's correction at any
# brightness; only its own deviation from the sky colour is raised to g, and
# ``exp_max`` caps g at 1.4 so that stays small. A sky too bright for the cap
# to reach the target lands brighter instead of being crushed, i.e. the target
# effectively rises with the raw sky (moon, light pollution, overexposure) --
# one bound on the thing that damages colour, whatever made the sky bright.
# A frame with more than ``clip_max_frac`` clipped pixels gets its gains but no
# tone curve: darkening an overexposed frame cannot restore what clipped and
# only exaggerates the tint of what did not. Past ``clip_unusable_frac`` (RO0014
# Vâlcele: 32% on 2026-09-24, 100% of its maxpixel on 2026-09-25) nothing is
# left to correct and the frame stays as captured; that camera needs its
# exposure fixed, not a curve.
#
# The target was learned from a clip picked as the fleet reference
# (RO001L_20260924_025155: sky RGB 43.5/47.6/51.9, luma ~47), nudged very
# slightly bluer and less green on review. Its brightness is lifted while the
# moon is up or the sun is in twilight, so a moonlit night still reads
# brighter, as the eye perceives it.
#
# ``color_adaptive`` on a camera overrides any key below; ``{"enabled": false}``
# restores the legacy ``color_post`` pipeline for that camera.
# ``color_adaptive_video`` / ``color_adaptive_stack`` merge over it per product.

ADAPTIVE_DEFAULTS: dict[str, float | bool] = {
    'enabled': True,
    # Target sky, as the mean of the sky-mask pixels: Rec.709 luma plus R/G and
    # B/G ratios. Output lands ~0.015 below r_g/b_g after 4:2:0 encoding.
    'sky_luma': 47.0,
    'r_g': 0.935,
    'b_g': 1.13,
    # Chroma scale applied after the curve (1.0 = unchanged). The fit is
    # pre-compensated, so the sky itself still lands on the target.
    'saturation': 0.94,
    # Sky-brightness lift, as a fraction of sky_luma: a full moon at the zenith
    # adds moon_boost (scaled by phase-law flux x sin(altitude), to the 0.4
    # power); twilight adds up to twilight_boost as the sun climbs from
    # twilight_start to twilight_end (deg, smoothstep). Astronomical twilight
    # adds nothing: the reference clip itself was shot with the sun at -14 deg.
    # The sum is capped at max_boost.
    'moon_boost': 0.5,
    'twilight_boost': 1.2,
    'twilight_start': -12.0,
    'twilight_end': -4.0,
    'max_boost': 1.5,
    # A raw sky mean below min_sky (0-255) is too dark to fit (dead or covered
    # camera): the frame is left as captured. The shared exponent is clamped to
    # [exp_min, exp_max]; see the section comment for why exp_max is 1.4.
    'min_sky': 2.0,
    'exp_min': 0.35,
    'exp_max': 1.4,
    # Gains fade to 1.0 between gain_knee x 255 and 255, per channel, so 255
    # stays 255. 0.95, not lower: on the 2026-09-25 moonlit stacks a 0.9 knee
    # left the brightest 10% up to +0.18 r/g redder than the sky (the red
    # channel of bright cloud reaches the knee first), 0.95 at most +0.07.
    'gain_knee': 0.95,
    # More than clip_max_frac of the frame's pixels with any channel at or
    # above clip_level: gains only, no tone curve. More than clip_unusable_frac
    # (RO0014 Valcele on 2026-09-25: 100% of its maxpixel): nothing left to
    # correct, the frame is left as captured, like a sky too dark to fit.
    'clip_level': 250.0,
    'clip_max_frac': 0.05,
    'clip_unusable_frac': 0.5,
}

ADAPTIVE_PRODUCTS: tuple[str, ...] = ('video', 'stack')

# Rec.709 luma weights, shared by the target maths and the saturation step.
_LUMA_W = (0.2126, 0.7152, 0.0722)


def resolve_adaptive(station_cfg: dict, product: str | None = None) -> dict:
    """Fleet ``ADAPTIVE_DEFAULTS`` merged with the camera's ``color_adaptive``,
    then its ``color_adaptive_<product>`` override."""
    params = {**ADAPTIVE_DEFAULTS, **(station_cfg.get('color_adaptive') or {})}
    if product is not None:
        if product not in ADAPTIVE_PRODUCTS:
            raise ValueError(f'unknown adaptive colour product {product!r}')
        params.update(station_cfg.get(f'color_adaptive_{product}') or {})
    return params


def adaptive_enabled(station_cfg: dict, product: str | None = None) -> bool:
    """True unless the camera (or that product) opted out with enabled=false."""
    return bool(resolve_adaptive(station_cfg, product).get('enabled', True))


def sun_moon_state(when: datetime, lat: float, lon: float) -> dict[str, float]:
    """Low-precision sun and moon for sky-brightness purposes.

    Sun to ~0.01 deg, moon to ~0.3 deg (main periodic terms of Meeus ch. 47),
    plenty for "how much moonlight is on this sky". Returns altitudes in
    degrees (moon corrected for parallax), the moon's phase angle, illuminated
    fraction, and its flux relative to full (Allen's phase law).
    """
    jd = when.timestamp() / 86400.0 + 2440587.5
    d = jd - 2451545.0
    rad = math.radians
    eps = rad(23.439 - 3.6e-7 * d)
    g = rad((357.529 + 0.98560028 * d) % 360.0)
    q = (280.459 + 0.98564736 * d) % 360.0
    lam_s = rad((q + 1.915 * math.sin(g) + 0.020 * math.sin(2 * g)) % 360.0)
    lp = (218.3164 + 13.17639648 * d) % 360.0
    elong = rad((297.8502 + 12.19074912 * d) % 360.0)
    mp = rad((134.9634 + 13.06499295 * d) % 360.0)
    f = rad((93.2721 + 13.22935024 * d) % 360.0)
    lam_m = rad(lp + 6.289 * math.sin(mp) + 1.274 * math.sin(2 * elong - mp)
                + 0.658 * math.sin(2 * elong) + 0.214 * math.sin(2 * mp)
                - 0.186 * math.sin(g) - 0.114 * math.sin(2 * f))
    beta_m = rad(5.128 * math.sin(f) + 0.281 * math.sin(mp + f)
                 + 0.278 * math.sin(mp - f) + 0.173 * math.sin(2 * elong - f))
    lst = rad((280.46061837 + 360.98564736629 * d + lon) % 360.0)
    phi = rad(lat)

    def _altitude(lam: float, beta: float) -> float:
        ra = math.atan2(math.sin(lam) * math.cos(eps) - math.tan(beta) * math.sin(eps),
                        math.cos(lam))
        dec = math.asin(math.sin(beta) * math.cos(eps)
                        + math.cos(beta) * math.sin(eps) * math.sin(lam))
        s = (math.sin(phi) * math.sin(dec)
             + math.cos(phi) * math.cos(dec) * math.cos(lst - ra))
        return math.degrees(math.asin(max(-1.0, min(1.0, s))))

    moon_alt = _altitude(lam_m, beta_m)
    moon_alt -= 0.95 * math.cos(rad(moon_alt))
    cos_psi = math.cos(beta_m) * math.cos(lam_m - lam_s)
    phase = 180.0 - math.degrees(math.acos(max(-1.0, min(1.0, cos_psi))))
    return {
        'sun_alt': _altitude(lam_s, 0.0),
        'moon_alt': moon_alt,
        'moon_phase': phase,
        'moon_illum': (1.0 + math.cos(rad(phase))) / 2.0,
        'moon_rel_flux': 10.0 ** (-0.4 * (0.026 * phase + 4e-9 * phase ** 4)),
    }


def sky_brightness_boost(params: dict, location: tuple[float, float] | None,
                         when: datetime | None) -> float:
    """Fractional lift of the target sky luma for moonlight and twilight.

    Zero without a location or a time (the dark-sky target applies).
    """
    if location is None or when is None:
        return 0.0
    eph = sun_moon_state(when, location[0], location[1])
    moon_light = eph['moon_rel_flux'] * max(0.0, math.sin(math.radians(eph['moon_alt'])))
    boost = float(params['moon_boost']) * moon_light ** 0.4
    start, end = float(params['twilight_start']), float(params['twilight_end'])
    t = min(1.0, max(0.0, (eph['sun_alt'] - start) / max(end - start, 1e-6)))
    boost += float(params['twilight_boost']) * t * t * (3.0 - 2.0 * t)
    return min(float(params['max_boost']), boost)


def adaptive_target_rgb(params: dict, boost: float = 0.0) -> tuple[float, float, float]:
    """Target sky RGB (0-255) from the luma and R/G, B/G of ``params``."""
    luma = float(params['sky_luma']) * (1.0 + boost)
    rg, bg = float(params['r_g']), float(params['b_g'])
    g = luma / (_LUMA_W[0] * rg + _LUMA_W[1] + _LUMA_W[2] * bg)
    return (min(250.0, rg * g), min(250.0, g), min(250.0, bg * g))


def _pre_saturation_target(target: tuple[float, float, float],
                           saturation: float) -> tuple[float, float, float]:
    """The pre-saturation mean that lands on ``target`` after the chroma scale.

    Saturation acts per pixel as ``y + s * (c - y)`` with y the Rec.709 luma,
    which is linear, so it maps the sky mean exactly the same way.
    """
    if abs(saturation - 1.0) < 1e-6:
        return target
    y = sum(w * c for w, c in zip(_LUMA_W, target))
    return tuple(min(254.0, max(0.5, y + (c - y) / saturation)) for c in target)


def sky_pixels(image_or_frames: np.ndarray, max_samples: int = 60000) -> np.ndarray:
    """Sky pixels (N x 3, float32 0-255) of one image or a stack of frames.

    The sky mask is taken from the (mean) image's luma between its 10th and
    85th percentile — the same mask as ``derive_adaptive_gains`` — excluding
    the dark foreground and the brightest stars, lights and trails. With
    several frames, pixels from every frame are kept so the fit sees the
    per-frame noise the video actually shows.
    """
    arr = np.asarray(image_or_frames)
    frames = arr[None] if arr.ndim == 3 else arr
    if frames.ndim != 4 or frames.shape[-1] != 3:
        raise ValueError(f'expected HxWx3 or NxHxWx3 RGB, got shape {arr.shape}')
    mean = frames.astype(np.float32).mean(axis=0)
    luma = mean.mean(axis=2)
    lo, hi = np.percentile(luma, 10.0), np.percentile(luma, 85.0)
    mask = (luma >= lo) & (luma <= hi)
    if not mask.any():
        mask = np.ones_like(luma, dtype=bool)
    px = frames[:, mask, :].reshape(-1, 3)
    step = max(1, px.shape[0] // max(1, max_samples))
    return px[::step].astype(np.float32)


def _knee_gain(v: np.ndarray, gain: float, knee: float) -> np.ndarray:
    """``gain * v`` below ``knee`` (0-255 scale), then a straight line to
    255 at 255, so a saturated pixel stays saturated whatever the gain."""
    v = np.asarray(v, dtype=np.float64)
    k = float(knee)
    if k >= 255.0:
        return v * gain
    if gain >= 1.0 - 1e-9:
        return v
    slope = (255.0 - gain * k) / (255.0 - k)
    return np.where(v <= k, gain * v, gain * k + slope * (v - k))


def _channel_mean(values: np.ndarray, gain: float, exponent: float, knee: float) -> float:
    """Mean of ``255 * (h(v) / 255) ** exponent`` over one channel's pixels."""
    h = np.clip(_knee_gain(values, gain, knee) / 255.0, 1e-6, 1.0)
    return 255.0 * float(np.exp(exponent * np.log(h)).mean())


def fit_gains_and_exponent(pixels: np.ndarray, target: tuple[float, float, float],
                           exp_min: float = 0.35, exp_max: float = 1.4,
                           knee: float = 242.25, exponent: float | None = None,
                           iterations: int = 40) -> tuple[tuple[float, float, float], float, bool]:
    """Per-channel gains (max 1.0) and one shared exponent landing the sky
    mean on ``target``. Returns ``(gains, exponent, capped)``.

    For a given exponent g and no clipping, ``mean((a*x/255)**g) =
    a**g * P(g)`` with ``P(g) = mean((x/255)**g)``, so the gains that give the
    target colour RATIO are closed-form, the channel needing the least
    attenuation gets gain 1, and the resulting sky luma falls as g grows:
    bisection on g finds the target luma. A luma the clamp cannot reach is
    left short (``capped`` when it is exp_max). ``exponent`` fixes g instead
    (the clipped-frame path uses 1.0). The gains are then refined per channel
    through the knee, which only matters for skies near saturation.
    """
    x = np.clip(pixels.astype(np.float64), 0.0, 255.0)
    logs = np.log(np.clip(x / 255.0, 1e-4, 1.0))
    ratio = np.array([target[0] / target[1], 1.0, target[2] / target[1]], dtype=np.float64)
    w = np.array(_LUMA_W, dtype=np.float64)
    want_luma = float(np.dot(w, np.asarray(target, dtype=np.float64)))

    def at(g: float) -> tuple[np.ndarray, float]:
        p = 255.0 * np.exp(g * logs).mean(axis=0)
        s = float(np.min(p / ratio))
        gains = np.clip((ratio * s / p) ** (1.0 / g), 0.0, 1.0)
        return gains, s * float(np.dot(w, ratio))

    capped = False
    if exponent is not None:
        g = float(exponent)
    else:
        lo, hi = float(exp_min), float(exp_max)
        if at(lo)[1] <= want_luma:
            g = lo
        elif at(hi)[1] >= want_luma:
            g, capped = hi, True
        else:
            for _ in range(iterations):
                mid = 0.5 * (lo + hi)
                if at(mid)[1] > want_luma:
                    lo = mid
                else:
                    hi = mid
            g = 0.5 * (lo + hi)
    gains, _ = at(g)
    # Through the knee: the anchor channel (gain 1) is unchanged, each other
    # one is re-solved so its sky mean keeps the ratio to the anchor's. A
    # channel whose sky sits mostly above the knee (a saturated frame) keeps
    # the closed-form gain: the knee holds those values up whatever the gain,
    # and the search would drive it to 0 and crush the rest of the channel.
    anchor = int(np.argmax(gains))
    anchor_mean = _channel_mean(x[:, anchor], 1.0, g, knee)
    out = []
    for c in range(3):
        if c == anchor:
            out.append(1.0)
            continue
        if float((x[:, c] > knee).mean()) > 0.5:
            out.append(float(gains[c]))
            continue
        want = anchor_mean * ratio[c] / ratio[anchor]
        lo_a, hi_a = 0.5 * float(gains[c]), 1.0
        if _channel_mean(x[:, c], hi_a, g, knee) <= want:
            out.append(1.0)
            continue
        if _channel_mean(x[:, c], lo_a, g, knee) >= want:
            out.append(lo_a)
            continue
        for _ in range(iterations):
            mid = 0.5 * (lo_a + hi_a)
            if _channel_mean(x[:, c], mid, g, knee) > want:
                hi_a = mid
            else:
                lo_a = mid
        out.append(0.5 * (lo_a + hi_a))
    return (out[0], out[1], out[2]), g, capped


@dataclass(frozen=True)
class AdaptiveCurve:
    """One chunk's sky-match curve: per-channel gains, one shared exponent,
    and a chroma scale."""

    gains: tuple[float, float, float]
    exponent: float
    knee: float
    saturation: float
    target: tuple[float, float, float]
    boost: float
    sky_mean: tuple[float, float, float]
    capped: bool = False
    clipped_frac: float = 0.0
    tone_skipped: bool = False

    def describe(self) -> str:
        a = self.gains
        t = self.target
        s = self.sky_mean
        flags = ''
        if self.tone_skipped:
            flags = f' clipped {self.clipped_frac:.0%}: gains only'
        elif self.capped:
            flags = ' exp capped'
        return (f'gain {a[0]:.3f}/{a[1]:.3f}/{a[2]:.3f} exp {self.exponent:.3f} '
                f'sat {self.saturation:.2f} '
                f'sky {s[0]:.1f}/{s[1]:.1f}/{s[2]:.1f} -> {t[0]:.1f}/{t[1]:.1f}/{t[2]:.1f} '
                f'boost {self.boost:+.2f}{flags}')


def clipped_fraction(image_or_frames: np.ndarray, level: float = 250.0) -> float:
    """Fraction of pixels with any channel at or above ``level``."""
    arr = np.asarray(image_or_frames)
    frames = arr[None] if arr.ndim == 3 else arr
    step = max(1, int(np.sqrt(frames.shape[1] * frames.shape[2] / 250000.0)))
    sub = frames[:, ::step, ::step, :]
    return float((sub.max(axis=-1) >= level).mean())


def resolve_adaptive_curve(
    image_or_frames: np.ndarray,
    station_cfg: dict,
    product: str,
    *,
    location: tuple[float, float] | None = None,
    when: datetime | None = None,
    already_calibrated: bool = False,
) -> AdaptiveCurve | None:
    """Fit the sky-match curve for one chunk (frames for a clip, the maxpixel
    for a stack). ``already_calibrated`` skips the chroma scale (for callers
    fitting on footage that already carries it). Returns None when the raw
    sky is too dark to fit or most of the frame is clipped."""
    params = resolve_adaptive(station_cfg, product)
    px = sky_pixels(image_or_frames)
    clip_level = float(params['clip_level'])
    # A clipped pixel carries no colour: on an overexposed frame the sky mask
    # (luma 10th-85th percentile) reaches into the clipped part and would pull
    # the white balance toward (255, 255, 255).
    unclipped = px[px.max(axis=1) < clip_level]
    if len(unclipped) >= max(500, len(px) // 20):
        px = unclipped
    sky_mean = tuple(float(v) for v in px.mean(axis=0))
    if max(sky_mean) < float(params['min_sky']):
        return None
    boost = sky_brightness_boost(params, location, when)
    target = adaptive_target_rgb(params, boost)
    saturation = 1.0 if already_calibrated else float(params['saturation'])
    knee = 255.0 * float(params['gain_knee'])
    clipped = clipped_fraction(image_or_frames, clip_level)
    if clipped > float(params['clip_unusable_frac']):
        return None
    tone_skipped = clipped > float(params['clip_max_frac'])
    gains, exponent, capped = fit_gains_and_exponent(
        px, _pre_saturation_target(target, saturation),
        float(params['exp_min']), float(params['exp_max']), knee,
        exponent=1.0 if tone_skipped else None)
    return AdaptiveCurve(gains, exponent, knee, saturation, target, boost,  # type: ignore[arg-type]
                         sky_mean, capped=capped and not tone_skipped,
                         clipped_frac=clipped, tone_skipped=tone_skipped)


def _curve_lut(gain: float, exponent: float, knee: float) -> np.ndarray:
    v = np.arange(256, dtype=np.float64)
    h = np.clip(_knee_gain(v, gain, knee) / 255.0, 0.0, 1.0)
    return np.clip(np.floor(255.0 * h ** float(exponent) + 0.5), 0, 255).astype(np.uint8)


# Clipped highlights go neutral. The knee brings each channel back to 255 on
# its own, so where one channel clips and the others do not, the others keep
# their white-balance gain and the clipped one does not: around the moon the
# no-IR-cut red channel clips first and draws a pink/red ring, and a disc where
# red and green clip but blue does not turns blue (2026-09-25 stacks: rings on
# RO000T, RO0016, MD0002, RO0012, RO001M; blue core on RO0014). A clipped
# channel carries no colour, so from the knee up (by the RAW pixel's brightest
# channel) the output blends toward white at its own brightest channel, with
# one weight for all three channels. Below the knee nothing changes, so star
# and meteor colour below saturation survives.
HIGHLIGHT_NEUTRAL_LO = 242.25   # the default gain knee, 0.95 x 255
HIGHLIGHT_NEUTRAL_HI = 255.0
# Video pays for the neutral step with a per-clip 3D LUT (lut3d ~10 ms/frame
# at 720p on one desktop CPU core vs ~1 ms for lutrgb), so only meteor clips whose
# sampled frames actually clip get it; bulk full-night encodes do not.
HIGHLIGHT_NEUTRAL_MIN_CLIPPED = 0.001
ADAPTIVE_CUBE_SIZE = 65


def _neutral_weight(raw_max: np.ndarray) -> np.ndarray:
    t = np.clip((raw_max - HIGHLIGHT_NEUTRAL_LO)
                / (HIGHLIGHT_NEUTRAL_HI - HIGHLIGHT_NEUTRAL_LO), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def _neutralize_highlights(out: np.ndarray, raw: np.ndarray) -> np.ndarray:
    """Blend ``out`` (float, 0-255) toward its own brightest channel where the
    raw pixel's brightest channel nears clipping."""
    w = _neutral_weight(np.asarray(raw, dtype=np.float32).max(axis=-1))[..., None]
    m = out.max(axis=-1, keepdims=True)
    return out + (m - out) * w


def apply_adaptive_np(image: np.ndarray, curve: AdaptiveCurve,
                      rotate: bool = False, neutral_highlights: bool = True) -> np.ndarray:
    """Apply a sky-match curve (+ chroma scale, + neutral clipped highlights,
    + optional 180° turn) to an RGB image. ``neutral_highlights=False`` gives
    exactly what the plain ``lutrgb`` video chain renders."""
    src = np.clip(image, 0, 255).astype(np.uint8) if image.dtype != np.uint8 else image
    out = np.empty_like(src)
    for c in range(3):
        out[..., c] = _curve_lut(curve.gains[c], curve.exponent, curve.knee)[src[..., c]]
    f = out.astype(np.float32)
    if abs(curve.saturation - 1.0) > 1e-3:
        y = _LUMA_W[0] * f[..., 0] + _LUMA_W[1] * f[..., 1] + _LUMA_W[2] * f[..., 2]
        f = y[..., None] + (f - y[..., None]) * float(curve.saturation)
    if neutral_highlights and int(src.max(initial=0)) > HIGHLIGHT_NEUTRAL_LO:
        f = _neutralize_highlights(f, src)
    out = np.clip(f + 0.5, 0, 255).astype(np.uint8)
    if rotate:
        out = np.rot90(out, k=2)
    return out


def adaptive_transform_float(rgb: np.ndarray, curve: AdaptiveCurve) -> np.ndarray:
    """The same transform as ``apply_adaptive_np`` evaluated on float RGB
    (0-255, any shape ending in 3), without 8-bit steps: the 3D LUT for video
    samples it between integer levels."""
    x = np.asarray(rgb, dtype=np.float64)
    f = np.empty_like(x)
    for c in range(3):
        h = np.clip(_knee_gain(x[..., c], curve.gains[c], curve.knee) / 255.0, 0.0, 1.0)
        f[..., c] = 255.0 * h ** float(curve.exponent)
    if abs(curve.saturation - 1.0) > 1e-3:
        y = _LUMA_W[0] * f[..., 0] + _LUMA_W[1] * f[..., 1] + _LUMA_W[2] * f[..., 2]
        f = y[..., None] + (f - y[..., None]) * float(curve.saturation)
    f = _neutralize_highlights(f, x)
    return np.clip(f, 0.0, 255.0)


def write_adaptive_cube(curve: AdaptiveCurve, path: Path,
                        size: int = ADAPTIVE_CUBE_SIZE) -> Path:
    """Write the whole adaptive transform as an ffmpeg ``lut3d`` .cube file
    (red index fastest, values 0-1)."""
    v = np.linspace(0.0, 255.0, size)
    b, g, r = np.meshgrid(v, v, v, indexing="ij")
    grid = np.stack([r, g, b], axis=-1).reshape(-1, 3)
    out = adaptive_transform_float(grid, curve) / 255.0
    with open(path, "w") as fh:
        fh.write(f"LUT_3D_SIZE {size}\n")
        np.savetxt(fh, out, fmt="%.6f")
    return path


def wants_neutral_lut(curve: AdaptiveCurve | None, locked: bool) -> bool:
    """Whether a video encode should carry the neutral-highlights 3D LUT."""
    return bool(locked and curve is not None
                and curve.clipped_frac >= HIGHLIGHT_NEUTRAL_MIN_CLIPPED)


def _lut_expr(gain: float, exponent: float, knee: float) -> str:
    """Comma-free lutrgb expression for one channel of the curve.

    The knee is written with ``abs``: ``max(0, t) = (t + abs(t)) / 2``, so the
    expression needs no filtergraph escaping in -vf or -filter_complex (the
    encoder splits the chain on commas). log(0) is -inf, so black stays black.
    """
    g = float(exponent)
    if gain >= 1.0 - 1e-9:
        return f'255*exp({g:.5f}*log(val/255))+0.5'
    k = float(knee)
    # h(v) = gain*v + (slope - gain) * max(0, v - k); slope >= 1 >= gain.
    slope = (255.0 - gain * k) / (255.0 - k) if k < 255.0 else gain
    h = (f'({gain:.6f}*val+{slope - gain:.6f}*((val-{k:.3f})+abs(val-{k:.3f}))/2)')
    return f'255*exp({g:.5f}*log({h}/255))+0.5'


def build_adaptive_ffmpeg_filter(curve: AdaptiveCurve, cube: Path | None = None) -> str:
    """ffmpeg filter chain for a sky-match curve: lutrgb, then a desaturation
    matrix, both in RGB.

    The chroma scale is a ``colorchannelmixer`` matrix around Rec.709 luma —
    the same maths as ``apply_adaptive_np``. ``eq=saturation`` is NOT used: it
    works on the YUV planes and was measured to shift the sky toward green
    (R/G 0.910 -> 0.866 at saturation 0.94), not just desaturate it.
    """
    if cube is not None:
        # The whole transform, including neutral clipped highlights, from
        # write_adaptive_cube. The path must not contain ',' or ':' (the
        # encoder splits the chain on commas).
        return f'lut3d=file={cube}:interp=tetrahedral'
    lut = ':'.join(f'{ch}={_lut_expr(a, curve.exponent, curve.knee)}'
                   for ch, a in zip('rgb', curve.gains))
    parts = [f'lutrgb={lut}']
    s = float(curve.saturation)
    if abs(s - 1.0) > 1e-3:
        w = [(1.0 - s) * x for x in _LUMA_W]
        terms = []
        for i, out in enumerate('rgb'):
            for j, inp in enumerate('rgb'):
                terms.append(f'{out}{inp}={w[j] + (s if i == j else 0.0):.5f}')
        parts.append('colorchannelmixer=' + ':'.join(terms))
    return ','.join(parts)


def _sample_frames_one_process(mkv: Path, w: int, h: int, duration: float, n_samples: int,
                               vf: str, timeout: float) -> tuple[np.ndarray | None, bool]:
    """All sample frames from ONE ffmpeg: each sample point is its own seeked
    input, trimmed to its first frame and concatenated. Returns (frames,
    stalled); frames is None unless exactly ``n_samples`` came back.

    Same seeks and conversion as the per-frame loop, so the same pixels
    (checked identical on 2026-09-26, including a 1/1000 time base and a
    50-frame GOP) for one process launch instead of five: ~170 ms less per
    clip. ``-vsync 0`` keeps the one-frame segments, whose timestamps collide,
    from being dropped. Any other frame count falls back to the loop; a
    timeout means the source is stalled, and the loop would stall too.
    """
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error']
    for i in range(n_samples):
        cmd += ['-ss', f'{duration * (i + 0.5) / n_samples:.2f}', '-i', str(mkv)]
    graph = ';'.join(f'[{i}:v]trim=end_frame=1,setpts=PTS-STARTPTS,{vf}[s{i}]'
                     for i in range(n_samples))
    graph += ';' + ''.join(f'[s{i}]' for i in range(n_samples)) + f'concat=n={n_samples}:v=1:a=0[out]'
    cmd += ['-filter_complex', graph, '-map', '[out]', '-vsync', '0',
            '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-']
    try:
        raw = subprocess.run(cmd, capture_output=True, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.warning('frame sampling timed out after %.0fs on %s', timeout, mkv)
        return None, True
    frame_bytes = w * h * 3
    if raw.returncode != 0 or len(raw.stdout) != frame_bytes * n_samples:
        return None, False
    return np.frombuffer(raw.stdout, dtype=np.uint8).reshape(n_samples, h, w, 3), False


def sample_frames(mkv: Path, n_samples: int = 5, full_range: bool = False,
                  timeout: float = 60.0) -> np.ndarray | None:
    """Decode ``n_samples`` frames spread over an MKV (N x H x W x 3 uint8).

    Seeks to each sample point instead of decoding the whole chunk, so it
    costs a handful of short decodes per clip — the adaptive path runs this
    for every clip (nothing is cached across clips any more).

    A probe or decode that exceeds *timeout* counts as a failed sample (None
    when nothing was read), so the encoder leaves that clip's colour as
    captured instead of failing the chunk.
    """
    meta = rovimen_media.ffprobe_json(mkv, timeout=timeout,
                                      entries='stream=width,height:format=duration')
    stream = rovimen_media.first_stream(meta)
    try:
        w, h = int(stream['width']), int(stream['height'])
    except (KeyError, TypeError, ValueError):
        logger.warning('ffprobe failed or timed out (%.0fs) on %s', timeout, mkv)
        return None
    try:
        duration = float(((meta or {}).get('format') or {}).get('duration') or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0.0:
        duration = 20.0
    vf = ('scale=in_range=full:out_range=full,' if full_range else '') + 'format=rgb24'
    one, stalled = _sample_frames_one_process(mkv, w, h, duration, n_samples, vf, timeout)
    if one is not None:
        return one
    if stalled:
        return None
    frames: list[np.ndarray] = []
    for i in range(n_samples):
        t = duration * (i + 0.5) / n_samples
        try:
            raw = subprocess.run(
                ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-ss', f'{t:.2f}',
                 '-i', str(mkv), '-frames:v', '1', '-vf', vf,
                 '-f', 'rawvideo', '-pix_fmt', 'rgb24', '-'],
                capture_output=True, check=False, timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning('frame sample at %.1fs timed out after %.0fs on %s', t, timeout, mkv)
            break   # the source is stalled; later seeks would stall too
        if raw.returncode == 0 and len(raw.stdout) >= w * h * 3:
            frames.append(np.frombuffer(raw.stdout[:w * h * 3], dtype=np.uint8).reshape(h, w, 3))
    if not frames:
        return None
    return np.stack(frames)


def resolve_adaptive_from_mkv(
    mkv: Path,
    station_cfg: dict,
    *,
    location: tuple[float, float] | None = None,
    when: datetime | None = None,
    full_range: bool = False,
) -> AdaptiveCurve | None:
    """Encoder-side helper: sample the clip and fit its sky-match curve.
    None if sampling failed, the sky is too dark to fit, or most of the clip
    is clipped."""
    frames = sample_frames(mkv, n_samples=5, full_range=full_range)
    if frames is None:
        return None
    return resolve_adaptive_curve(frames, station_cfg, 'video', location=location, when=when)
