"""overlay.py — Shared OSD annotation utilities.

Used by both coppermind (video encoding) and nightwatcher (maxpixel stacks)
so that both produce visually identical overlays from the same config.

Public API
----------
build_drawtext_annotations(overlay_cfg, station_id, station_cfg, chunk_epoch)
    → (list[str], int)   ffmpeg drawtext filter strings + cinema bar height

annotate_still(src, dst, overlay_cfg, station_id, station_cfg, chunk_epoch)
    → bool               apply overlay to a still image (WebP/PNG) via ffmpeg

render_still(image, overlay_cfg, station_id, station_cfg, chunk_epoch)
    → Image | None       the same overlay, rendered from an in-memory image

save_overlaid_webp(rendered, dst)
                         publish a rendered still exactly as annotate_still does
"""

from __future__ import annotations

import io
import logging
import math
import os
import struct
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

try:
    from PIL import Image as _PILImage, ImageDraw as _PILDraw, ImageFont as _PILFont
    _HAS_PIL = True
except ImportError:
    _HAS_PIL = False


# EXIF UserComment tag id. A still image that carries this marker has already
# had the ROVIMEN/astromania branding+data overlay burned in, so downstream
# consumers (the dashboard serve/download gate) must not stamp it a second
# time. Written by ``mark_still_overlaid`` after every successful still write.
_OVERLAY_MARKER = 'ROVIMEN-STILL-OVERLAY-v1'
_EXIF_USER_COMMENT = 0x9286


@contextmanager
def atomic_still_write(dst: str | Path) -> Iterator[Path]:
    """Yield a scratch path in *dst*'s directory, moved onto *dst* on success.

    Every still this module and the stacker produce goes through here, so the
    destination is never itself opened for writing. That matters because a
    still is routinely rewritten in place (annotate, then EXIF-stamp) while
    another process may be reading — or writing — the very same path: the
    stations run a per-chunk stacker from color_capture at the same time as
    dawn's night pass, and color_capture kills a stacker that overruns its
    120 s budget. Writing into the destination turned every one of those
    interruptions into a 0-byte file that still looked "stacked"; writing into
    a private scratch file and calling ``os.replace`` only once the bytes are
    complete leaves the previous image untouched instead.

    The scratch name is unique per call (so two writers never share it) and
    lands in *dst*'s own directory (so the replace is a same-filesystem rename,
    which is atomic — a reader sees the old image or the new one, never half).

    Raises if the block leaves no bytes behind: an empty render is a failure to
    report, not a stack to publish.
    """
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=dst.parent, prefix=f'{dst.stem}.', suffix=f'.tmp{dst.suffix}',
    )
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        yield tmp
        if not tmp.exists() or tmp.stat().st_size == 0:
            raise RuntimeError(f'no bytes were written for {dst.name}')
        os.replace(tmp, dst)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass


def still_overlay_marker() -> str:
    """Return the sentinel written into overlaid still images (EXIF UserComment)."""
    return _OVERLAY_MARKER


def still_is_overlaid(path: str | Path) -> bool:
    """True when *path* carries the burned-in-overlay marker.

    Reads the EXIF UserComment written by ``mark_still_overlaid``. A missing
    marker (legacy stack, or a still that never went through ``annotate_still``)
    reads False, which the dashboard gate treats as "needs overlay".
    """
    if not _HAS_PIL:
        return False
    try:
        with _PILImage.open(path) as im:
            exif = im.getexif()
        raw = exif.get(_EXIF_USER_COMMENT)
    except Exception:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode('ascii', 'ignore')
    return isinstance(raw, str) and _OVERLAY_MARKER in raw


def mark_still_overlaid(path: str | Path) -> bool:
    """Stamp *path* (a WebP/PNG still) with the overlaid-still EXIF marker.

    Re-saves the image with the marker embedded so a later reader
    (``still_is_overlaid``) can tell the branding is already present. The
    re-save goes through ``atomic_still_write``: the pixels are read into
    memory first and the marked copy replaces the original in one rename, so
    an interrupted stamp cannot destroy the image it was stamping. Returns
    True on success; a failure is non-fatal (the image is served un-marked and
    the gate re-renders it, which is correct if slower).
    """
    if not _HAS_PIL:
        return False
    path = Path(path)
    try:
        with _PILImage.open(path) as im:
            im.load()
            exif = im.getexif()
            exif[_EXIF_USER_COMMENT] = _OVERLAY_MARKER
            fmt = (im.format or path.suffix.lstrip('.')).upper()
            with atomic_still_write(path) as tmp:
                if fmt == 'WEBP':
                    im.save(tmp, 'WEBP', quality=95, exif=exif)
                else:
                    im.save(tmp, exif=exif)
        return True
    except Exception as e:
        logger.debug('mark_still_overlaid failed for %s: %s', path, e)
        return False


# ---------------------------------------------------------------------------
# Branding layout defaults
# ---------------------------------------------------------------------------
#
# Nothing is drawn over the sky/meteor. The astromania logo and the ROVIMEN
# wordmark sit CENTRED inside the bottom overlay bar (the black data strip); the
# GMN logo sits small in a corner. Every position and size below is a plain
# fraction of the bar/frame so replacement artwork from the association drops in
# without touching the code — only these constants change.
#
# GEOMETRY IS CODE-OWNED, NOT CONFIG-OWNED. The fractions below are deliberately
# NOT readable from a station's ``overlay`` block: a config key that shadows one
# of them is invisible in review, survives every release, and silently voids any
# change made here -- which is exactly how ``bar_logo_height_frac`` = 0.62 (merged
# into all 18 station configs from config_defaults.json on 2026-08-05) absorbed
# four successive fleet-wide logo shrinks without altering a single clip.
# ``CODE_OWNED_OVERLAY_KEYS`` names the keys that must never come back as config:
# config_migrate purges them from every station on update, and tests assert that
# neither the shipped defaults nor this module's own source reintroduces one.
# Add a key here whenever a new geometry constant appears below.
#
# Genuinely per-station values (``font_size``, ``coords``, ``style``, the
# ``show_*`` toggles, ``font``/``logo`` paths, opacities) stay in config.
CODE_OWNED_OVERLAY_KEYS = frozenset({
    'bar_logo_height_frac',
    'bar_logo_width_frac',
    'bar_logo_max_height_frac',
    'bar_logo_max_width_frac',
    'text_scale',
    'network_text_scale',
    'gmn_logo_frac',
})

# Overlay keys that no longer reach any render path. Unlike the code-owned
# fractions above these were never geometry we took back -- they are dead
# surface: ``logo_size``/``logo_opacity`` predate the bar layout and
# ``watermark_*`` predates the bar entirely. They kept being shipped in
# config_defaults.json, kept being merged into every station config, and kept
# being offered as live spinners in the dashboard's overlay editor, so an
# operator could set "Logo size: 40 px", save, see nothing change, and have no
# way to tell the control was inert. Purged on update alongside the code-owned
# keys (config_migrate.REMOVED_NESTED_KEYS) and removed from the editor UI; a
# test asserts this module never starts reading one again.
INERT_OVERLAY_KEYS = frozenset({
    'logo_size',
    'logo_opacity',
    'watermark_width_frac',
    'watermark_opacity',
})

# ``TEXT_SCALE`` enlarges every drawtext element uniformly (~+50%) while keeping
# the shipped ``font_size`` values untouched, so the enlargement stays legible
# at 720p and is tunable from one place.

TEXT_SCALE = 1.5

# The ROVIMEN wordmark carries the branding, so it takes an additional scale on
# top of ``TEXT_SCALE`` -- the operator logo beside it is a credit, not the
# headline.
NETWORK_TEXT_SCALE = 5 / 3

# The ROVIMEN logo (wordmark with the tricolour fireball as the i-dot) drawn in
# row 0 of the bar in place of the text wordmark, the same artwork as the
# website. It fills the row the text wordmark occupied (``network_font_size``
# tall), so the bar geometry is unchanged. Drawn only for the default network
# name and only when the asset ships; otherwise the text wordmark stays.
NETWORK_MARK_PATH = Path(__file__).resolve().parent / 'assets' / 'rovimen_mark.png'
NETWORK_MARK_NAME = 'ROVIMEN'
# The logo is drawn 1.4x the row height, top-anchored, so it is the most
# prominent mark in the bar. On the standard 128 px bar that is 66 px tall
# (~283 px wide): its bottom stays 14 px above the operator logo and its sides
# clear the data columns (coords end ~266 px, the clock starts ~863 px on a
# 1280 px frame).
NETWORK_MARK_ROW_SCALE = 1.4
# The mark's ink starts NETWORK_MARK_TOP_INSET px below the video edge (the top of
# the bar), so the operator logo under it gets the freed room. rovimen_mark.png
# carries 17 fully transparent rows above the spark tail (of 160); they are cropped after
# scaling, so nothing is composited over the video. A constant, not a measured
# value, because PIL is optional here (see bar_logo_headroom);
# tests/test_overlay.py checks it against the asset.
NETWORK_MARK_TOP_INSET = 4
NETWORK_MARK_INK_TOP = 17 / 160


def network_mark_crop(mark_h: int) -> int:
    """Transparent rows to cut from the top of the mark scaled to ``mark_h``."""
    return int(mark_h * NETWORK_MARK_INK_TOP)

# Astromania logo, centred inside the bottom bar (never over the video).
#
# The logo is scaled to a fraction of the bar HEIGHT so it always fits the strip
# regardless of frame width. The legacy ``logo_opacity``/``logo_size`` and the
# old ``watermark_width_frac`` overlay keys (still shipped in
# config_defaults.json and per-station configs, still editable in the dashboard
# config UI) no longer feed any render path; they are kept as inert config
# surface for backward compatibility, not consumed here.
#
# THE LOGO SIZE LIVES HERE AND ONLY HERE. Four PRs between 2026-08-05 and
# 2026-08-16 walked this constant 0.42 -> 0.315 -> 0.104 -> 0.052 with no visible
# effect on any station, because ``config_defaults.json`` shipped a
# ``bar_logo_height_frac`` of 0.62 and config_migrate.py wrote it into every
# station's config.json, where the per-station override outranked the constant.
# The override reads are gone, the default is gone, and the stale keys are purged
# on update (config_migrate.REMOVED_NESTED_KEYS), so this line is the only lever.
# Do not reintroduce a config default for it.
#
# astromania_text.png is a ~9.8:1 wordmark, so every unit of height it gets is
# multiplied by ~10 in width -- which is what let it dominate the bar. The same
# slot carries every operator's mark (Astromania, SARM, Astroclubul Bucuresti),
# so a change here rebalances all of them at once.
#
# 0.17 of the standard 128 px bar renders 22x215 px on a 1280 px frame: about the
# width of the 47 px ROVIMEN wordmark above it, and clear of the coords (ends
# ~266 px) and the UTC clock (starts ~863 px) sharing that row. Small enough to
# read as a credit, large enough to read at all -- the 0.052 this replaces would
# have rendered a 6 px tall, 59 px wide smudge once it finally took effect.
#
# 0.155 (2026-10-20, was 0.17): the operator mark steps down to 20x196 px so the
# ROVIMEN logo above it leads the bar.
# 0.22 and a 4 px inset (2026-10-29, were 0.155 and 14): the ROVIMEN mark moved up
# to NETWORK_MARK_TOP_INSET, and the operator mark takes the room it freed, so
# the astromania wordmark renders 28x274 px and a square badge fills the headroom.
BAR_LOGO_HEIGHT_FRAC = 0.22   # operator logo height as a fraction of the bar height
BAR_LOGO_OPACITY = 0.9        # legible in the bar, not competing with the data text
BAR_LOGO_MARGIN = 4           # px inset from the bar edges

# EQUAL INK ACROSS ASSET SHAPES.
#
# Sizing by height alone hands every operator's mark the same HEIGHT, so its
# width -- the thing you actually read -- comes out proportional to its aspect
# ratio. On 2026-08-25 an audit of the rendered bar on all 21 stations found the
# fleet split in two by asset shape, not by any setting: the 9.79:1
# ``astromania_text`` wordmark rendered 22x215 px and read fine on 16 stations,
# while the near-square marks rendered 22x22 px -- a tenth of the footprint, an
# unreadable speck -- on the three stations that carry their own badge
# (``sternfreunde_berlin`` 345x357, ``ab1`` 644x600,
# ``gmn_sphere`` 160x159). Those three looked un-branded while their
# configs said ``show_logo: true``, which is why nobody read it as a bug.
#
# Scaling by ``sqrt(REF_ASPECT / aspect)`` equalises the logo's AREA instead of
# its height: a square badge asks for ~68x68 px against the wordmark's 22x215,
# and the headroom clamp below then trims it to 42x42 -- the room row 1 of the
# bar actually owns. Against the 22x22 it used to get, that is twice the height
# and nearly four times the ink; against the wordmark it is smaller, which is the
# honest trade for keeping every mark inside its own row. The reference aspect is
# the astromania asset itself, so the 16 stations carrying it are numerically
# unchanged (factor == 1.0) and this stays a fix for the badge shapes rather than
# a fleet-wide resize.
#
# The caps exist because sqrt cuts in the other direction too. A taller badge
# has to stay in the strip the bar actually leaves for it -- the room under the
# ROVIMEN wordmark, above the bottom inset -- which :func:`bar_logo_headroom`
# derives from the same BAR_MARGIN/BAR_GAP/row-height terms as
# :func:`bar_height`, rather than from a fraction that would silently go stale
# the next time TEXT_SCALE or font_size moves. MAX_HEIGHT_FRAC stays as a blunt
# backstop under it. And an ultra-wide banner -- an operator's 19:1 one-line
# wordmark -- must not creep across the bar into the coords and clock columns
# (MAX_WIDTH, in units of bar height, set to the wordmark's own 215 px so
# nothing renders wider than today's widest).
BAR_LOGO_REF_ASPECT = 2056 / 210  # astromania_text.png, the asset 0.17 was tuned on
BAR_LOGO_MAX_HEIGHT_FRAC = 0.62   # blunt backstop under the derived headroom
BAR_LOGO_MAX_WIDTH_FRAC = 2.2     # in units of bar height: 282 px, the ROVIMEN mark's width

# Per-artwork enlargement on top of equal ink, keyed by asset file name and
# applied to the width budget too. Code-owned like the fractions above: a
# station picks the artwork, the artwork decides its size. abtext.png
# (Astroclubul Bucuresti) rendered 24x173 px and read
# small next to the wordmark; 1.3 takes it to 32x230 px, still clear of the
# coords and clock columns.
BAR_LOGO_ASSET_SCALE = {
    'abtext.png': 1.3,
}

# GMN corner logo. No station configures ``gmn_logo`` today (all null), so this
# block is dormant; the fraction is of the ASSET's own width, not the frame's
# (``scale`` cannot see ``main_w``) -- see :func:`gmn_logo_scale_expr`.
GMN_LOGO_FRAC = 0.10          # logo width as a fraction of the asset's own width
GMN_LOGO_OPACITY = 0.85
GMN_LOGO_CORNER = 'br'        # tl | tr | bl | br
GMN_LOGO_MARGIN = 14          # px inset from the frame edge


# Bar layout. ``BAR_MARGIN``/``BAR_GAP`` were function-local constants inside
# ``build_drawtext_annotations``, which meant any other module that needed to know
# where the bar starts had to re-derive it -- and re-deriving geometry is how the
# logo size drifted for two weeks. Lifted to module scope so
# :func:`bar_height` is the one answer to "how tall is the strip".
BAR_MARGIN = 14
BAR_GAP = 10

# A single meteor row is one data line plus the inter-row gap. The details pass
# (overlay_details.py) appends one such row per detection to the bar, growing it
# downward without ever touching the sky above.
def meteor_row_height(overlay_cfg: dict) -> int:
    """Height in px of one per-meteor row appended to the bar by the details pass."""
    return effective_font_size(overlay_cfg) + BAR_GAP


def _base_bar_height(overlay_cfg: dict) -> int:
    """Unaligned height of the LIVE bar (branding + station row), no meteor rows."""
    font_size = effective_font_size(overlay_cfg)
    row0_h = max(font_size, network_font_size(overlay_cfg))
    return 2 * BAR_MARGIN + row0_h + font_size + 1 * BAR_GAP


def bar_height(overlay_cfg: dict, n_meteors: int = 0) -> int:
    """Height in px of the cinema-style bottom data bar, or 0 for other styles.

    The single definition of the bar geometry. Three callers rely on it:
    ``build_drawtext_annotations`` pads the LIVE frame with ``n_meteors=0``;
    ``detection_lock._stitch_stacks`` slices sky from bar on a LIVE stack, also
    ``n_meteors=0``; and ``overlay_details`` asks for ``n_meteors=N`` to size the
    taller bar that carries one row per detected meteor. All must agree exactly
    or a merge/pad slices through the text.

    With ``n_meteors=0`` this returns exactly what the fixed two-row bar always
    did -- branding row + station row -- so the base clip's bar is unchanged.
    Each additional meteor adds one ``meteor_row_height`` line; the whole bar is
    16px-aligned once, at the end, for the VAAPI encoder.
    """
    if overlay_cfg.get('style', 'standard') != 'cinema':
        return 0
    h = _base_bar_height(overlay_cfg)
    if n_meteors > 0:
        # A thin separator gap, one data line per meteor, then a trailing
        # BAR_MARGIN so the last row isn't flush against the frame edge (the
        # base bar spends its own bottom margin on the station row).
        h += BAR_GAP + n_meteors * meteor_row_height(overlay_cfg) + BAR_MARGIN
    return (h + 15) // 16 * 16  # align to 16px for VAAPI encoder


def meteor_rows_extra_height(overlay_cfg: dict, n_meteors: int) -> int:
    """Px to append BELOW the live bar to make room for ``n_meteors`` rows.

    The details pass never rebuilds the branding+station bar the base clip
    already carries: it pads the frame by exactly this much at the bottom and
    draws the meteor rows into the new strip. Defined as the difference of two
    :func:`bar_height` values so the base bar and the extension can never
    disagree about where the live bar ends. Zero for a non-cinema style or no
    meteors, so the details pass is a no-op there.
    """
    if n_meteors <= 0 or overlay_cfg.get('style', 'standard') != 'cinema':
        return 0
    return bar_height(overlay_cfg, n_meteors) - bar_height(overlay_cfg, 0)


def effective_font_size(overlay_cfg: dict) -> int:
    """Return the drawn font size: ``font_size`` scaled by ``text_scale``.

    Keeping the shipped ``font_size`` and scaling here enlarges every overlay
    element (~+50% by default) from a single knob without rewriting configs.
    ``font_size`` is per-station; the scale is code-owned (see
    ``CODE_OWNED_OVERLAY_KEYS``) so a config key cannot shadow it.
    """
    base = overlay_cfg.get('font_size', 19)
    return max(1, round(base * TEXT_SCALE))


def network_font_size(overlay_cfg: dict) -> int:
    """Return the drawn font size for the ROVIMEN wordmark.

    ``effective_font_size`` scaled again by ``NETWORK_TEXT_SCALE`` so the
    wordmark outranks the data columns without enlarging them too. Code-owned
    for the same reason as ``TEXT_SCALE``.
    """
    return max(1, round(effective_font_size(overlay_cfg) * NETWORK_TEXT_SCALE))


def _corner_overlay_xy(corner: str, margin: int) -> tuple[str, str]:
    """Return ffmpeg ``overlay`` (x, y) expressions for a frame corner.

    ``W``/``H`` are the main frame size and ``w``/``h`` the overlaid image size
    in ffmpeg's overlay expression namespace.
    """
    corner = (corner or GMN_LOGO_CORNER).lower()
    x = str(margin) if corner in ('tl', 'bl') else f'W-w-{margin}'
    y = str(margin) if corner in ('tl', 'tr') else f'H-h-{margin}'
    return x, y


def gmn_logo_spec(overlay_cfg: dict) -> tuple[str, float, str, str] | None:
    """Resolve the GMN corner logo into (path, opacity, overlay_x, overlay_y).

    Returns None when no usable ``gmn_logo`` is configured. The width is scaled
    from the frame at overlay time, so a replacement asset of any aspect ratio
    slots in unchanged.
    """
    path = overlay_cfg.get('gmn_logo') or ''
    if not path or not Path(path).is_file():
        return None
    opacity = float(overlay_cfg.get('gmn_logo_opacity', GMN_LOGO_OPACITY))
    margin = int(overlay_cfg.get('gmn_logo_margin', GMN_LOGO_MARGIN))
    corner = overlay_cfg.get('gmn_corner', GMN_LOGO_CORNER)
    x, y = _corner_overlay_xy(corner, margin)
    return path, opacity, x, y


def gmn_logo_scale_expr(overlay_cfg: dict) -> str:
    """ffmpeg scale target for the GMN corner logo.

    ``iw`` inside ``scale`` is the logo input's width, not the frame's, so this
    is a fraction of the ASSET's own width -- see ``GMN_LOGO_FRAC``. Code-owned,
    like every other geometry fraction in this module.
    """
    return f'trunc(iw*{GMN_LOGO_FRAC:.4f}/2)*2'


def _png_size(path: str) -> tuple[int, int] | None:
    """Width/height from a PNG's IHDR, or None if it is not a readable PNG.

    Header-parsed rather than PIL-loaded because this runs per render on every
    station: 24 bytes and no decode. PIL is only an optional import here (see
    ``_HAS_PIL``), so an operator who drops in a ``.webp``/``.jpg`` mark falls
    through to it below rather than getting no size at all.
    """
    try:
        with open(path, 'rb') as fh:
            head = fh.read(24)
    except OSError:
        return None
    if len(head) < 24 or head[:8] != b'\x89PNG\r\n\x1a\n' or head[12:16] != b'IHDR':
        return None
    width, height = struct.unpack('>II', head[16:24])
    return (width, height) if width > 0 and height > 0 else None


def bar_logo_aspect(overlay_cfg: dict) -> float:
    """Aspect ratio of the configured operator mark, or the reference aspect.

    Falling back to ``BAR_LOGO_REF_ASPECT`` for anything unreadable keeps a
    missing, truncated or exotic asset rendering exactly as the fleet's wordmark
    does today instead of at some accidental size -- and keeps the size a pure
    function of the artwork, never of config.
    """
    path = overlay_cfg.get('logo') or ''
    size = _png_size(path)
    if size is None and _HAS_PIL and path:
        try:
            with _PILImage.open(path) as im:
                size = im.size
        except Exception:
            size = None
    if not size or size[1] <= 0 or size[0] <= 0:
        return BAR_LOGO_REF_ASPECT
    return size[0] / size[1]


def network_mark_path(overlay_cfg: dict) -> str | None:
    """The ROVIMEN logo PNG to draw instead of the text wordmark, or None.

    None when the wordmark is hidden, when a station names a different network
    (its text is drawn as before), or when the asset is missing from the bundle.
    """
    if not overlay_cfg.get('show_network', True):
        return None
    if str(overlay_cfg.get('network', NETWORK_MARK_NAME)) != NETWORK_MARK_NAME:
        return None
    return str(NETWORK_MARK_PATH) if NETWORK_MARK_PATH.is_file() else None


def bar_logo_asset_scale(overlay_cfg: dict) -> float:
    """Extra size factor for the configured artwork (``BAR_LOGO_ASSET_SCALE``)."""
    return BAR_LOGO_ASSET_SCALE.get(Path(overlay_cfg.get('logo') or '').name, 1.0)


def bar_logo_headroom(overlay_cfg: dict, bar_h: int) -> float:
    """Height in px the bar leaves for the logo under the network wordmark.

    Row 0 of the bar carries the ROVIMEN wordmark inset ``BAR_MARGIN`` from the
    top, then ``BAR_GAP`` of air; the logo is inset ``BAR_LOGO_MARGIN`` from the
    bottom. What is left between them is what a mark may occupy -- built from the
    same ``row0_h`` term as :func:`bar_height`, so the two agree on where row 0
    ends and this tracks a font_size or TEXT_SCALE change instead of going
    quietly stale.

    The row's NOMINAL height, deliberately, not its measured glyph ink. Two
    reasons, and the first one bites: ``measure_text_height`` needs PIL, which is
    an optional import here, so measuring would make the rendered logo size
    depend on the environment -- a station with PIL burning a 56 px badge while
    the dashboard rendered the same station's stack at 42 px, which is precisely
    the kind of "same code, different pixels" divergence that made the logo
    regressions so hard to see. Second, the reserved band belongs to row 0
    whether or not the glyphs fill it: VCR ink measures 34 px in a 47 px box, and
    a badge sized off the ink sits 3 px inside a band the layout already
    allocated.
    """
    row0_h = max(effective_font_size(overlay_cfg), network_font_size(overlay_cfg))
    row0_bottom = BAR_MARGIN + row0_h
    if network_mark_path(overlay_cfg):
        # The ROVIMEN mark replaces the text row: it ends where its cropped image
        # ends (see NETWORK_MARK_TOP_INSET), 1.4x the row but higher in the bar.
        mark_h = int(round(row0_h * NETWORK_MARK_ROW_SCALE)) // 2 * 2
        row0_bottom = NETWORK_MARK_TOP_INSET + mark_h - network_mark_crop(mark_h)
    return max(2.0, bar_h - row0_bottom - BAR_GAP - BAR_LOGO_MARGIN)


def bar_logo_scale_expr(overlay_cfg: dict, bar_h: int) -> str:
    """ffmpeg scale target for the operator logo inside the bottom bar.

    The logo is sized for equal INK: the height is a fraction of ``bar_h``
    adjusted by ``sqrt(BAR_LOGO_REF_ASPECT / aspect)``, so a square badge and a
    9.79:1 wordmark occupy the same area instead of the same height (see the
    constants above). The result is clamped to the room under the network
    wordmark (:func:`bar_logo_headroom`) and to the wordmark's own width, so
    neither a tall badge nor a wide banner escapes its slot. The
    ``scale`` filter only sees the logo input (``iw``/``ih``) -- frame-width
    variables like ``main_w`` are valid only in the ``overlay`` filter -- so the
    height is a fixed pixel value and the width is ``-2`` (even,
    aspect-preserving).

    Only the ASSET's own dimensions feed this; every fraction is deliberately NOT
    overridable from ``overlay_cfg``, since a station config key here shadows the
    fleet default silently, which is how four successive shrink attempts shipped
    without changing a single rendered clip. ``overlay_cfg`` stays in the
    signature so callers keep one call shape for both styles.
    """
    aspect = bar_logo_aspect(overlay_cfg)
    scale = bar_logo_asset_scale(overlay_cfg)
    baseline = bar_h * BAR_LOGO_HEIGHT_FRAC
    height = baseline * math.sqrt(BAR_LOGO_REF_ASPECT / aspect) * scale
    # The clamps only ever hold a mark BACK. Floored at the baseline so a short
    # bar -- or a font we cannot measure, which makes the headroom pessimistic --
    # can never render a logo smaller than the height-only rule already did:
    # this is a fix for the badges, and a fix may not regress the wordmark.
    height = min(
        height,
        max(baseline, bar_logo_headroom(overlay_cfg, bar_h)),  # clear of ROVIMEN
        max(baseline, bar_h * BAR_LOGO_MAX_HEIGHT_FRAC),       # inside the bar
        max(baseline, float(bar_h - 2 * BAR_LOGO_MARGIN)),     # clear of the edges
        max(baseline, bar_h * BAR_LOGO_MAX_WIDTH_FRAC * scale / aspect),  # width budget
    )
    target_h = max(2, int(round(height)) // 2 * 2)
    return f'-2:{target_h}'


def bar_logo_opacity(overlay_cfg: dict) -> float:
    """Alpha for the astromania logo drawn inside the bottom bar."""
    return float(overlay_cfg.get('bar_logo_opacity', BAR_LOGO_OPACITY))


# Standard style has no black bar to size against, so the bottom-anchored logo is
# sized as a fraction of the artwork's OWN width. ``iw`` inside ``scale`` is the
# logo input's width, not the frame's (only ``overlay`` sees ``main_w``), so this
# fraction cannot be read as "x% of the frame" however the old comment phrased it.
# The whole fleet runs the cinema style, so this is the fallback path; keep it at
# roughly the pixel width the bar style produces for the shipped wordmark --
# 0.105 of the 2056 px astromania asset is 215 px, matching BAR_LOGO_HEIGHT_FRAC.
STANDARD_LOGO_WIDTH_FRAC = 0.105
STANDARD_LOGO_MARGIN = 14  # px from the frame bottom when there is no bar


def _standard_logo_scale_expr(overlay_cfg: dict) -> str:
    """ffmpeg scale target for the bottom-anchored logo in the bar-less style.

    Not overridable from ``overlay_cfg``, for the same reason as
    :func:`bar_logo_scale_expr`: the constant is the fleet's single lever.
    """
    return f'trunc(iw*{STANDARD_LOGO_WIDTH_FRAC:.4f}/2)*2:-2'


# ---------------------------------------------------------------------------
# Text measurement helpers
# ---------------------------------------------------------------------------

def measure_text_width(font_path: str, font_size: int, text: str) -> int:
    """Return pixel width of text at the given font. Falls back to monospace estimate."""
    if _HAS_PIL:
        try:
            img  = _PILImage.new('RGB', (4000, 200))
            draw = _PILDraw.Draw(img)
            font = _PILFont.truetype(font_path, font_size)
            bb   = draw.textbbox((0, 0), text, font=font)
            return bb[2] - bb[0]
        except Exception:
            pass
    return int(font_size * 0.65 * len(text))


def measure_text_height(font_path: str, font_size: int, text: str = 'Ag') -> int:
    """Return pixel height of text bounding box at the given font. Falls back to font_size."""
    if _HAS_PIL:
        try:
            img  = _PILImage.new('RGB', (4000, 200))
            draw = _PILDraw.Draw(img)
            font = _PILFont.truetype(font_path, font_size)
            bb   = draw.textbbox((0, 0), text, font=font)
            return bb[3] - bb[1]
        except Exception:
            pass
    return font_size


# ---------------------------------------------------------------------------
# Drawtext filter builder
# ---------------------------------------------------------------------------

def build_drawtext_annotations(
    overlay_cfg: dict,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> tuple[list[str], int]:
    """Build drawtext filter strings for OSD annotation.

    Returns (filters, bar_h) where bar_h > 0 for cinema style (pad height to add).

    Nothing is drawn over the sky/meteor: the branding (astromania logo + the
    ROVIMEN wordmark) and every data field live in the bottom overlay bar.

    Cinema style:   black bar added at the bottom; all elements inside it.
      - Centre:  ROVIMEN wordmark (top row) over the astromania logo (bottom row),
                 both horizontally centred; the logo is composited by
                 ``build_branding_overlay``.
      - Left:    station code (top) / coordinates (bottom).
      - Right:   az+alt pointing (top) / UTC timestamp (bottom).
      - Corner:  small GMN logo (composited by ``build_branding_overlay``).
    Standard style: no black bar; the same fields are anchored to the bottom edge
      and the branding stays horizontally centred at the bottom — never the
      middle of the frame.
    """
    font_path = overlay_cfg.get('font') or ''
    font_size = effective_font_size(overlay_cfg)
    net_size  = network_font_size(overlay_cfg)
    opacity   = float(overlay_cfg.get('text_opacity', 0.4))
    coords    = overlay_cfg.get('coords', '')
    network   = overlay_cfg.get('network', 'ROVIMEN')
    az        = float(station_cfg.get('az', overlay_cfg.get('az', 0)))
    alt       = float(station_cfg.get('alt', overlay_cfg.get('alt', 0)))
    style     = overlay_cfg.get('style', 'standard')

    show_logo      = overlay_cfg.get('show_logo',      True)  # noqa: F841 (used by caller)
    show_station   = overlay_cfg.get('show_station',   True)
    show_coords    = overlay_cfg.get('show_coords',    True)
    show_pointing  = overlay_cfg.get('show_pointing',  True)
    show_timestamp = overlay_cfg.get('show_timestamp', True)
    show_network   = overlay_cfg.get('show_network',   True)

    # Aliases for the module-level geometry -- never redefine the values here,
    # or bar_height() and this layout can disagree.
    MARGIN = BAR_MARGIN
    GAP    = BAR_GAP

    if not font_path or not Path(font_path).is_file():
        return [], 0

    shadow_opacity = opacity * 0.8

    # Every value drawn here is literal text from the config (station id,
    # coords, network) or computed (pointing), never a drawtext expansion; the
    # clock, which is one, has its own builder (ts below). Escaped, because a
    # comma in coords used to end this filter and start one of the config's
    # choosing (drawtext=textfile=<any file>).
    def dt(text_val: str, x_expr: str, y_expr: str, size: int | None = None) -> str:
        return (
            f'drawtext=fontfile={font_path}'
            f':text={escape_drawtext(str(text_val))}'
            f':fontsize={font_size if size is None else size}'
            f':fontcolor=white@{opacity:.2f}'
            f':shadowcolor=black@{shadow_opacity:.2f}'
            f':shadowx=2:shadowy=2'
            f':x={x_expr}:y={y_expr}'
        )

    def ts(x_expr: str, y_expr: str) -> str:
        # ``expansion=strftime`` formats basetime through localtime(), so on a
        # host that is not UTC the clock reads local time under a "UTC" label.
        # ``%{pts:gmtime:<epoch>:<fmt>}`` formats the same epoch through
        # gmtime(), which carries the timezone in the filter and cannot be
        # undone by the host's TZ. The colons are escaped for the filtergraph
        # parser and the value is single-quoted so it survives as one option.
        return (
            f'drawtext=fontfile={font_path}'
            rf":text='%{{pts\:gmtime\:{chunk_epoch}\:%Y-%m-%d  %T}} UTC'"
            f':fontsize={font_size}'
            f':fontcolor=white@{opacity:.2f}'
            f':shadowcolor=black@{shadow_opacity:.2f}'
            f':shadowx=2:shadowy=2'
            f':x={x_expr}:y={y_expr}'
        )

    az_alt_text = f'ALT {alt:.1f}  AZ {az:.1f}'

    if style == 'cinema':
        # Row 0 is as tall as the larger of the wordmark and the data text, so
        # enlarging the wordmark grows the bar instead of clipping it.
        row0_h = max(font_size, net_size)
        bar_h = bar_height(overlay_cfg)
        y = [
            f'h-{bar_h}+{MARGIN}',
            f'h-{bar_h}+{MARGIN + row0_h + GAP}',
        ]
        # Data columns on row 0 sit centred against the taller wordmark.
        y0_data = f'h-{bar_h}+{MARGIN + (row0_h - font_size) // 2}'
        filters = []
        # Centre column: the ROVIMEN wordmark on the top row, the astromania logo
        # (composited by build_branding_overlay) on the bottom row — both inside
        # the bar, nothing over the sky. The wordmark is on its own row so it
        # never touches the left/right data columns even at text_scale=1.5.
        # When the logo PNG ships, build_branding_overlay draws it in this row
        # instead of the text.
        if show_network and not network_mark_path(overlay_cfg):
            filters.append(dt(network, '(w-tw)/2', y[0], net_size))
        # Left column: station code over coordinates.
        if show_station:
            filters.append(dt(station_id, str(MARGIN), y0_data))
        if show_coords:
            filters.append(dt(coords, str(MARGIN), y[1]))
        # Right column: pointing over the UTC clock.
        if show_pointing:
            filters.append(dt(az_alt_text, f'w-{MARGIN}-tw', y0_data))
        if show_timestamp:
            filters.append(ts(f'w-{MARGIN}-tw', y[1]))
        return filters, bar_h

    else:  # standard
        # No black bar; anchor the same fields to the bottom edge and keep the
        # branding centred at the BOTTOM (never mid-frame over the meteor).
        filters = []
        if show_timestamp:
            filters.append(ts(f'w-{MARGIN}-tw', f'h-{MARGIN}-th'))
        if show_pointing:
            filters.append(dt(az_alt_text, f'w-{MARGIN}-tw', f'h-{MARGIN}-2*th-{GAP}'))
        if show_coords:
            filters.append(dt(coords, str(MARGIN), f'h-{MARGIN}-2*th-{GAP}'))
        if show_station:
            filters.append(dt(station_id, str(MARGIN), f'h-{MARGIN}-th'))
        if show_network:
            # Bottom-centre, clear of the data columns and above the logo.
            # ``th`` resolves to this element's own height, so the larger
            # wordmark lifts itself clear without extra offsets.
            filters.append(dt(network, '(w-tw)/2', f'h-{MARGIN}-2*th-{GAP}', net_size))
        return filters, 0


# ---------------------------------------------------------------------------
# Per-meteor rows (details pass) — one row per detected meteor, appended to the
# bar below the branding+station rows. Lives here so bar_height() and the row
# layout share one definition; overlay_details.py builds no geometry of its own.
# ---------------------------------------------------------------------------

def escape_drawtext(text: str) -> str:
    """Quote a literal string as an ffmpeg drawtext ``text=`` value inside a
    filtergraph, so it draws exactly *text* and cannot end the filter.

    ffmpeg unescapes the value three times (the filtergraph parser, then the
    filter's option parser, then drawtext's text expansion), so it is escaped
    for each in the reverse order:

    1. drawtext's text expansion: ``\\`` escapes the next character and
       ``%`` starts an expansion, so both are backslash-escaped.
    2. the filter's option parser, which splits options at ``:``: ``\\``,
       ``'`` and ``:`` are backslash-escaped.
    3. the filtergraph parser, which splits filters at ``,`` and ``;``: the
       value is single-quoted, and a quote inside it closes the quoted run,
       is backslash-escaped and reopens it (inside quotes a backslash is
       literal, so ``\\'`` alone would end the quoting).

    Text with no quote, backslash or percent comes out as before (quoted,
    colons escaped). Shared by the live bar and the details pass.
    """
    literal = text.replace('\\', '\\\\').replace('%', '\\%')
    option = literal.replace('\\', '\\\\').replace("'", "\\'").replace(':', '\\:')
    return "'" + option.replace("'", "'\\''") + "'"


def build_meteor_row_annotations(
    overlay_cfg: dict,
    meteor_rows: list[tuple[str, str]],
    base_frame_h: int,
    extra_h: int,
) -> list[str]:
    """Build drawtext filters for the per-meteor rows in the bar extension.

    ``meteor_rows`` is one ``(left_text, right_text)`` pair per detected meteor,
    in clip order. ``left_text`` carries the science summary (shower · magnitude
    · velocity · ToF); ``right_text`` carries that meteor's own UTC time. Both
    are drawn as literal text (the details pass runs post-calc, so every value is
    already known and static — no per-frame ``pts`` clock here).

    ``base_frame_h`` is the height of the pristine base clip (720 sky + the live
    branding+station bar). ``extra_h`` is :func:`meteor_rows_extra_height`: the
    strip the details pass padded on below it. Rows are laid on the same
    ``BAR_MARGIN``/``BAR_GAP`` grid as the live bar, starting one ``BAR_GAP``
    below the join so a thin separator of black sits between the station row and
    the first meteor row. A separator rule is drawn at the top of the extension.

    Returns ``[]`` when there is no usable font or nothing to draw.
    """
    if not meteor_rows or extra_h <= 0:
        return []
    font_path = overlay_cfg.get('font') or ''
    if not font_path or not Path(font_path).is_file():
        return []

    font_size = effective_font_size(overlay_cfg)
    opacity = float(overlay_cfg.get('text_opacity', 0.4))
    shadow_opacity = opacity * 0.8
    row_h = meteor_row_height(overlay_cfg)

    def dt(text_val: str, x_expr: str, y: int) -> str:
        return (
            f'drawtext=fontfile={font_path}'
            f':text={escape_drawtext(text_val)}'
            f':fontsize={font_size}'
            f':fontcolor=white@{opacity:.2f}'
            f':shadowcolor=black@{shadow_opacity:.2f}'
            f':shadowx=2:shadowy=2'
            f':x={x_expr}:y={y}'
        )

    filters: list[str] = []
    # A faint separator rule marking where the live bar ends and the meteor rows
    # begin. Sits in the BAR_GAP the extension opens with, drawn full width.
    rule_y = base_frame_h + BAR_GAP // 2
    filters.append(
        f'drawbox=x={BAR_MARGIN}:y={rule_y}:w=iw-{2 * BAR_MARGIN}:h=1'
        f':color=white@{min(opacity + 0.15, 1.0):.2f}:t=fill'
    )
    # First meteor row starts one gap below the join, then one row per meteor.
    y0 = base_frame_h + BAR_GAP
    for i, (left_text, right_text) in enumerate(meteor_rows):
        y = y0 + i * row_h
        if left_text:
            filters.append(dt(left_text, str(BAR_MARGIN), y))
        if right_text:
            filters.append(dt(right_text, f'w-{BAR_MARGIN}-tw', y))
    return filters


# ---------------------------------------------------------------------------
# Branding overlay (bottom-bar astromania logo + corner GMN logo)
# ---------------------------------------------------------------------------

def build_branding_overlay(
    overlay_cfg: dict,
    in_label: str,
    out_label: str,
    input_index: int,
    bar_h: int = 0,
) -> tuple[list[str], str]:
    """Composite the astromania logo (in the bottom bar) and the corner GMN logo.

    Given the label of the drawtext-annotated stream (``in_label``, e.g.
    ``vmain``) and the desired output label, returns:

      * ``inputs``  — extra ``-i <path>`` arguments to append to the ffmpeg
        command, in the order their filter labels reference them.
      * ``fragment`` — a filter_complex fragment (``;``-joined) that scales each
        logo, applies its opacity, overlays it, and ends at ``[out_label]``.

    ``input_index`` is the ffmpeg input number of the first logo (the caller's
    video is input 0, so pass 1 here). The astromania ``logo`` is drawn CENTRED
    inside the bottom overlay bar (bottom-anchored so it never covers the sky);
    the ``gmn_logo`` is drawn small in a corner. Either may be absent; when both
    are, the fragment is a passthrough copy to ``out_label``.

    ``bar_h`` is the cinema black-bar height from
    :func:`build_drawtext_annotations`. When it is > 0 the astromania logo is
    sized to fit the bar; when it is 0 (standard style, no bar) the logo falls
    back to a fraction of the frame height and stays bottom-anchored — still in
    the bottom strip, never mid-frame.

    Layout and sizing come entirely from the module constants and their
    ``overlay`` overrides, so replacement artwork drops in without code changes.
    """
    inputs: list[str] = []
    stages: list[str] = []
    cur = in_label
    idx = input_index

    # ROVIMEN logo in row 0 of the bar, 1.4x the row the text wordmark used
    # (cinema style only; the bar-less style keeps the text).
    mark = network_mark_path(overlay_cfg) if bar_h > 0 else None
    if mark:
        inputs += ['-i', mark]
        row0_h = max(effective_font_size(overlay_cfg), network_font_size(overlay_cfg))
        mark_h = int(round(row0_h * NETWORK_MARK_ROW_SCALE)) // 2 * 2
        after = f'mark{idx}'
        cut = network_mark_crop(mark_h)
        stages.append(
            f'[{idx}:v]scale=-2:{mark_h},crop=iw:ih-{cut}:0:{cut},format=rgba[logo{idx}]'
        )
        stages.append(
            f'[{cur}][logo{idx}]overlay=x=(W-w)/2:y=H-{bar_h}+{NETWORK_MARK_TOP_INSET}[{after}]'
        )
        cur = after
        idx += 1

    show_logo = overlay_cfg.get('show_logo', True)
    logo_path = overlay_cfg.get('logo') or ''
    use_bar_logo = bool(show_logo and logo_path and Path(logo_path).is_file())

    if use_bar_logo:
        inputs += ['-i', logo_path]
        # Size by the bar height (falls back to a frame-height fraction when the
        # style has no bar), and bottom-anchor centred so the logo lives in the
        # bottom strip and never overlaps the video above it.
        effective_bar = bar_h if bar_h > 0 else 0
        logo_scale = bar_logo_scale_expr(overlay_cfg, effective_bar) if effective_bar \
            else _standard_logo_scale_expr(overlay_cfg)
        logo_alpha = bar_logo_opacity(overlay_cfg)
        # The bar-less style anchors over the video itself, so it keeps the old
        # 14 px inset instead of hugging the frame edge.
        margin = int(overlay_cfg.get(
            'bar_logo_margin', BAR_LOGO_MARGIN if effective_bar else STANDARD_LOGO_MARGIN))
        after = f'brand{idx}'
        stages.append(
            f'[{idx}:v]scale={logo_scale},format=rgba,'
            f'colorchannelmixer=aa={logo_alpha}[logo{idx}]'
        )
        stages.append(
            f'[{cur}][logo{idx}]overlay=x=(W-w)/2:y=H-h-{margin}[{after}]'
        )
        cur = after
        idx += 1

    gmn = gmn_logo_spec(overlay_cfg)
    if gmn:
        gmn_path, gmn_alpha, gmn_x, gmn_y = gmn
        inputs += ['-i', gmn_path]
        gmn_scale = gmn_logo_scale_expr(overlay_cfg)
        after = f'gmn{idx}'
        stages.append(
            f'[{idx}:v]scale={gmn_scale}:-2,format=rgba,'
            f'colorchannelmixer=aa={gmn_alpha}[logo{idx}]'
        )
        stages.append(
            f'[{cur}][logo{idx}]overlay=x={gmn_x}:y={gmn_y}[{after}]'
        )
        cur = after
        idx += 1

    if cur != in_label:
        # ``cur`` is the label of the last stage's output; relabel it explicitly
        # rather than string-slicing, so a caller can retarget the fragment.
        return inputs, relabel_fragment_output(';'.join(stages), cur, out_label)

    # No logos: passthrough copy so the caller can always map [out_label].
    return inputs, f'[{in_label}]null[{out_label}]'


def relabel_fragment_output(fragment: str, old_label: str, new_label: str) -> str:
    """Rename a filter_complex fragment's final output pad label.

    ``build_branding_overlay`` ends its fragment at a known pad (e.g. ``vbrand``).
    The VAAPI path chains another stage onto that pad, but the libx264 path wants
    the fragment to terminate directly at ``[out]``. Swapping the trailing
    ``[old_label]`` for ``[new_label]`` is explicit and robust: it targets the
    exact pad by name instead of slicing on the last ``[``, which would corrupt
    the graph if an overlay expression ever contained a bracket.
    """
    suffix = f'[{old_label}]'
    if not fragment.endswith(suffix):
        raise ValueError(
            f'fragment does not end with [{old_label}]: {fragment!r}'
        )
    return fragment[: -len(suffix)] + f'[{new_label}]'


# ---------------------------------------------------------------------------
# Still-image annotation
# ---------------------------------------------------------------------------

def _still_filter_args(overlay_cfg: dict, filters: list[str], bar_h: int) -> list[str]:
    """ffmpeg arguments that follow a still's own ``-i``: logo inputs + filter graph.

    Shared by :func:`annotate_still` (file in, file out) and
    :func:`render_still` (pipe in, pipe out) so the two cannot drift: the
    stacker's in-memory stacks are pixel-identical to the old file-based ones
    only because both paths hand ffmpeg the same graph.
    """
    logo_inputs, brand_fragment = build_branding_overlay(
        overlay_cfg, in_label='vmain', out_label='out', input_index=1,
        bar_h=bar_h,
    )
    vf_parts = []
    if bar_h > 0:
        vf_parts.append(f'pad=iw:ih+{bar_h}:0:0:black')
    vf_parts.extend(filters)
    if logo_inputs:
        cpu_chain = ','.join(vf_parts) if vf_parts else 'null'
        return [
            *logo_inputs,
            '-filter_complex', f'[0:v]{cpu_chain}[vmain];{brand_fragment}',
            '-map', '[out]',
        ]
    return ['-vf', ','.join(vf_parts)]


# One still's overlay render takes well under a second; a stalled ffmpeg must
# fail this stack's overlay (the plain stack is published), not the stacker.
_RENDER_STILL_TIMEOUT_S = 120


def render_still(
    image: _PILImage.Image,
    overlay_cfg: dict,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> _PILImage.Image | None:
    """Render the OSD onto an in-memory still (a PIL image, RGB).

    Returns the rendered PIL image — the pixels :func:`annotate_still` produces
    from a lossless WebP of *image* — or None when this config draws nothing
    (no usable font), in which case the plain stack is the finished product.
    Raises RuntimeError when ffmpeg fails or runs past
    ``_RENDER_STILL_TIMEOUT_S``.

    The stacker used to write the stack as a lossless WebP only for ffmpeg to
    decode it again here, then re-encode it. Piping the pixels in and the
    render out skips that disk round trip and the lossless encode, which the
    annotated stack replaced anyway. Two details keep the output identical:
    the frame goes in as ARGB, the format ffmpeg's WebP decoder gives the
    filter graph for a lossless still, so format negotiation picks the same
    conversions; and the render comes out as PNG, as annotate_still writes it,
    so ffmpeg picks the same output format. ``-compression_level 0`` only skips
    the deflate work, which never touches a pixel.
    """
    if not _HAS_PIL:
        raise RuntimeError('Pillow is not available')
    filters, bar_h = build_drawtext_annotations(
        overlay_cfg, station_id, station_cfg, chunk_epoch,
    )
    if not filters and bar_h == 0:
        return None

    rgb = image if image.mode == 'RGB' else image.convert('RGB')
    w, h = rgb.size
    # An 'RGBA' image whose bands hold A, R, G, B serialises as ARGB bytes.
    opaque = _PILImage.new('L', rgb.size, 255)
    argb = _PILImage.merge('RGBA', (opaque, *rgb.split())).tobytes()
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error',
        '-f', 'rawvideo', '-pix_fmt', 'argb', '-s', f'{w}x{h}', '-i', 'pipe:0',
        *_still_filter_args(overlay_cfg, filters, bar_h),
        '-c:v', 'png', '-compression_level', '0', '-f', 'image2pipe', 'pipe:1',
    ]
    try:
        result = subprocess.run(
            cmd, input=argb, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env={**os.environ, 'TZ': 'UTC'},
            timeout=_RENDER_STILL_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f'ffmpeg timed out after {_RENDER_STILL_TIMEOUT_S} s') from None
    if result.returncode != 0 or not result.stdout:
        raise RuntimeError(f'ffmpeg exited {result.returncode} '
                           f'with {len(result.stdout)} bytes of output')
    rendered = _PILImage.open(io.BytesIO(result.stdout))
    rendered.load()
    return rendered


def _overlay_exif() -> _PILImage.Exif:
    """An EXIF block carrying only the overlay marker (see still_is_overlaid)."""
    exif = _PILImage.Exif()
    exif[_EXIF_USER_COMMENT] = _OVERLAY_MARKER
    return exif


def save_overlaid_webp(rendered: _PILImage.Image, dst: str | Path) -> None:
    """Publish a :func:`render_still` result at *dst*: one q95 WebP with the marker.

    annotate_still saves ffmpeg's render as a q95 WebP, and mark_still_overlaid
    then decodes that file and saves it again, q95, to stamp the overlay marker
    into its EXIF: two generations of WebP loss. Here the render is encoded
    once, marker included, so a stack loses one generation less than the file
    path gives it (its pixels are exactly the first of annotate_still's two
    encodes) and the stacker skips a decode and an encode per chunk. A failed
    EXIF stamp publishes the unmarked encode, as annotate_still does.

    The bytes reach *dst* through :func:`atomic_still_write`, in one rename,
    so a reader sees the previous file or this one and never an empty stack.
    Raises when nothing could be written.
    """
    data = b''
    try:
        marked = io.BytesIO()
        rendered.save(marked, 'WEBP', quality=95, exif=_overlay_exif())
        data = marked.getvalue()
    except Exception as e:
        logger.debug('overlay marker failed for %s: %s', dst, e)
    if not data:
        plain = io.BytesIO()
        rendered.save(plain, 'webp', quality=95)
        data = plain.getvalue()
    with atomic_still_write(dst) as tmp:
        tmp.write_bytes(data)


def annotate_still(
    src: Path,
    dst: Path,
    overlay_cfg: dict,
    station_id: str,
    station_cfg: dict,
    chunk_epoch: int,
) -> bool:
    """Apply OSD annotation to a still image (WebP/PNG) via ffmpeg.

    Uses the same drawtext filters and layout as coppermind uses for video,
    so stacks and encoded clips look identical. Replaces dst atomically, and
    only with a complete image — src == dst is the normal stacker call, and on
    failure that source survives un-annotated rather than being truncated.

    Returns True on success, False on ffmpeg/Pillow error (already logged).
    """
    filters, bar_h = build_drawtext_annotations(
        overlay_cfg, station_id, station_cfg, chunk_epoch,
    )

    if not filters and bar_h == 0:
        if src != dst:
            import shutil
            try:
                with atomic_still_write(dst) as tmp_out:
                    shutil.copy2(src, tmp_out)
            except Exception as e:
                logger.warning('annotate_still copy failed for %s: %s', dst, e)
                return False
        return True

    # The output path is appended at render time: it is always a scratch file,
    # never *dst*.
    webp_lossless = ['-quality', '95'] if dst.suffix.lower() == '.webp' else []
    cmd = [
        'ffmpeg', '-hide_banner', '-loglevel', 'error', '-y',
        '-i', str(src),
        *_still_filter_args(overlay_cfg, filters, bar_h),
        *webp_lossless,
    ]

    # ffmpeg never writes to *dst* — it renders into a scratch file that is
    # renamed over the destination only once the image is complete. The stacker
    # calls this with src == dst, so pointing ffmpeg (or Pillow) at dst meant
    # writing the file that was being read: any interruption mid-write — a
    # killed stacker, a second stacker on the same chunk — published a 0-byte
    # stack. Now a failure leaves the un-annotated source exactly as it was and
    # is reported to the caller.
    # When dst is WebP, ffmpeg may lack libwebp; route through a PNG temp and
    # let Pillow handle the final WebP write.
    is_webp = dst.suffix.lower() == '.webp'

    try:
        with atomic_still_write(dst) as tmp_out:
            if is_webp:
                fd, png_tmp = tempfile.mkstemp(suffix='.png', dir=dst.parent)
                os.close(fd)
                try:
                    result = subprocess.run(
                        [*cmd, png_tmp], stderr=subprocess.DEVNULL,
                        env={**os.environ, 'TZ': 'UTC'},
                    )
                    if result.returncode != 0:
                        raise RuntimeError(f'ffmpeg exited {result.returncode}')
                    from PIL import Image as _PILImg
                    _PILImg.open(png_tmp).save(str(tmp_out), 'webp', quality=95)
                finally:
                    try:
                        os.unlink(png_tmp)
                    except OSError:
                        pass
            else:
                result = subprocess.run(
                    [*cmd, str(tmp_out)], stderr=subprocess.DEVNULL,
                    env={**os.environ, 'TZ': 'UTC'},
                )
                if result.returncode != 0:
                    raise RuntimeError(f'ffmpeg exited {result.returncode}')
            # Stamp the scratch copy so the marker and the pixels land in the
            # destination together, in the single rename below.
            mark_still_overlaid(tmp_out)
    except Exception as e:
        logger.warning('annotate_still failed for %s — source left intact: %s', dst, e)
        return False
    return True
