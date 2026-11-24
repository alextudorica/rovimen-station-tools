#!/usr/bin/env python3
"""Project per-station camera stacks onto a single celestial hemisphere.

The one dome renderer: stations import it (``sky_dome_render.py``), and the
deploy copies this file next to the dashboard modules on the VPS for the
fallback dome and the dome timelapse (``dashboard/routes/sky_dome.py``). Reads
each camera's RMS platepar to map every output dome pixel back to a camera
pixel, samples the latest stack image, and blends overlapping FOVs. Draws
cardinal-point labels and per-camera FOV outlines on top.

Phase 1: gnomonic projection using only alt_centre / az_centre / F_scale /
rotation_from_horiz from the platepar. No radial-distortion polynomial — for
~90 deg FOV cameras the corner deflection is only a couple of pixels, which is
below one dome-pixel on a 1600 px output. Distortion correction is a clean
drop-in if the visual is later judged not sharp enough.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

DOME_PX = 1600
HORIZON_FRAC = 0.93  # outer circle radius / half-image, leaves margin for labels
BACKGROUND_RGB = (5, 5, 16)
REPO_ROOT = Path(__file__).resolve().parents[1]


# ── Station-rendered dome upload contract ────────────────────────────────
#
# The dome is composited per station (multi-camera), but the ingest byte-plane
# only accepts blobs under <cam>/<date>/<subdir> with subdir in a fixed
# whitelist (see dashboard/ingest_api.py: meteors/stacks/timelapse/rms). So a
# station uploads its rendered dome under its *primary* camera's night tree
# using sentinel filenames the dashboard knows to look for. Both the station
# renderer (rovimen-scripts/sky_dome_render.py) and the dashboard serve path
# (dashboard/routes/sky_dome.py) import these helpers so the location can never
# drift between producer and consumer.
#
# Sentinel names deliberately do NOT match ingest_api's media-pointer patterns
# (``*_color.mkv`` / ``*_timelapse.mp4`` / ``*_night_stack.webp``) so an
# uploaded dome writes bytes only and never surfaces as a normal clip/timelapse.

STATION_DOME_PNG_SUBDIR = "stacks"
STATION_DOME_TIMELAPSE_SUBDIR = "timelapse"
STATION_DOME_TIMELAPSE_NAME = "rovimen_sky_dome.mp4"


def station_dome_primary_cam(cam_codes: list[str]) -> str | None:
    """Deterministic primary camera for a station's dome upload/serve path.

    Lexicographically smallest camera code, so producer and consumer agree
    regardless of config ordering as long as they share the same camera set.
    Returns None when the station has no cameras.
    """
    valid = sorted(c for c in cam_codes if c)
    return valid[0] if valid else None


def station_dome_png_name(variant: str = "stack", overlay: bool = False) -> str:
    """Sentinel filename for a station-uploaded dome PNG of this variant."""
    suffix = "_overlay" if overlay else ""
    stem = "rovimen_sky_dome" if variant == "stack" else f"rovimen_sky_dome_{variant}"
    return f"{stem}{suffix}.png"


@dataclass
class Plate:
    code: str
    alt_centre: float
    az_centre: float
    f_scale: float
    rotation_from_horiz: float
    x_res: int
    y_res: int
    fov_h: float
    fov_v: float
    rotate: bool = False  # camera physically mounted upside-down; image must be flipped before projection

    @classmethod
    def from_dict(cls, d: dict, code: str, *, rotate: bool = False) -> "Plate":
        """Build a Plate from an already-parsed platepar dict.

        Accepts either the full RMS platepar (keys like ``alt_centre``,
        ``F_scale``) or the trimmed payload from station_api.py's
        ``/api/platepar`` (same field names). The dashboard uses this branch
        so it doesn't have to round-trip via disk.

        ``rotate`` should be set to True when the station config has
        ``rotate: true`` — the captured/stacked frames are stored rotated 180°
        for display, but the platepar was calibrated against the raw frame.
        ``normalize_camera_image`` will flip the image before projection.
        """
        return cls(
            code=code,
            alt_centre=float(d["alt_centre"]),
            az_centre=float(d["az_centre"]),
            f_scale=float(d["F_scale"]),
            rotation_from_horiz=float(d["rotation_from_horiz"]),
            x_res=int(d["X_res"]),
            y_res=int(d["Y_res"]),
            fov_h=float(d["fov_h"]),
            fov_v=float(d["fov_v"]),
            rotate=rotate,
        )

    @classmethod
    def load(cls, path: Path, code: str) -> "Plate":
        with path.open() as f:
            d = json.load(f)
        return cls.from_dict(d, code)


def altaz_to_pixel(
    alt_deg: np.ndarray, az_deg: np.ndarray, plate: Plate
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Sky (alt, az) -> image pixel (px, py) + in-bounds mask.

    Uses an azimuthal equidistant projection on the local alt-az sphere. The
    image plane is rotated by ``rotation_from_horiz`` so that the +X image axis
    sweeps the appropriate horizon direction.
    """
    alt0 = math.radians(plate.alt_centre)
    az0 = math.radians(plate.az_centre)
    pa = math.radians(plate.rotation_from_horiz)

    alt = np.radians(alt_deg)
    az = np.radians(az_deg)

    daz = az - az0
    cos_rho = np.sin(alt) * math.sin(alt0) + np.cos(alt) * math.cos(alt0) * np.cos(daz)
    cos_rho = np.clip(cos_rho, -1.0, 1.0)
    rho = np.arccos(cos_rho)

    y_pa = np.sin(daz) * np.cos(alt)
    x_pa = math.cos(alt0) * np.sin(alt) - math.sin(alt0) * np.cos(alt) * np.cos(daz)
    theta = np.arctan2(y_pa, x_pa)  # 0 = towards increasing alt (zenith), +ve = east-of-zenith

    rho_deg = np.degrees(rho)
    d_az_tan = rho_deg * np.sin(theta)  # along "horizon-east" axis
    d_alt_tan = rho_deg * np.cos(theta)  # along "zenith" axis

    cos_r = math.cos(pa)
    sin_r = math.sin(pa)
    # Inverse of the (x->east, -y->up) ⊗ rotation_from_horiz mapping
    dx = (d_az_tan * cos_r + d_alt_tan * sin_r) * plate.f_scale
    dy = (d_az_tan * sin_r - d_alt_tan * cos_r) * plate.f_scale

    px = plate.x_res / 2.0 + dx
    py = plate.y_res / 2.0 + dy

    in_bounds = (
        (px >= 0)
        & (px <= plate.x_res - 1)
        & (py >= 0)
        & (py <= plate.y_res - 1)
        & (alt_deg >= 0)
    )
    return px, py, in_bounds


def pixel_to_altaz(
    px: np.ndarray, py: np.ndarray, plate: Plate
) -> tuple[np.ndarray, np.ndarray]:
    """Image pixel -> (alt, az) — analytic inverse of altaz_to_pixel."""
    alt0 = math.radians(plate.alt_centre)
    az0 = math.radians(plate.az_centre)
    pa = math.radians(plate.rotation_from_horiz)

    dx = px - plate.x_res / 2.0
    dy = py - plate.y_res / 2.0

    cos_r = math.cos(pa)
    sin_r = math.sin(pa)
    d_az_tan = (dx * cos_r + dy * sin_r) / plate.f_scale
    d_alt_tan = (dx * sin_r - dy * cos_r) / plate.f_scale

    rho_deg = np.hypot(d_az_tan, d_alt_tan)
    theta = np.arctan2(d_az_tan, d_alt_tan)
    rho = np.radians(rho_deg)

    sin_alt = math.sin(alt0) * np.cos(rho) + math.cos(alt0) * np.sin(rho) * np.cos(theta)
    sin_alt = np.clip(sin_alt, -1.0, 1.0)
    alt = np.arcsin(sin_alt)

    y_az = np.sin(theta) * np.sin(rho) * math.cos(alt0)
    x_az = np.cos(rho) - math.sin(alt0) * np.sin(alt)
    daz = np.arctan2(y_az, x_az)
    az = (az0 + daz) % (2 * math.pi)

    return np.degrees(alt), np.degrees(az)


def altaz_to_dome_xy(
    alt_deg: np.ndarray, az_deg: np.ndarray, dome_px: int
) -> tuple[np.ndarray, np.ndarray]:
    """Polar plot: zenith at centre, horizon at ``HORIZON_FRAC`` radius.

    Azimuth 0 deg (N) is image-up, 90 deg (E) is image-right.
    """
    cx = cy = dome_px / 2.0
    radius = (dome_px / 2.0) * HORIZON_FRAC
    r = radius * (90.0 - alt_deg) / 90.0
    az_rad = np.radians(az_deg)
    x = cx + r * np.sin(az_rad)
    y = cy - r * np.cos(az_rad)
    return x, y


def dome_xy_to_altaz(
    x: np.ndarray, y: np.ndarray, dome_px: int
) -> tuple[np.ndarray, np.ndarray]:
    cx = cy = dome_px / 2.0
    radius = (dome_px / 2.0) * HORIZON_FRAC
    dx = x - cx
    dy = y - cy
    r = np.hypot(dx, dy)
    alt = 90.0 - 90.0 * (r / radius)
    az = (np.degrees(np.arctan2(dx, -dy))) % 360.0
    return alt, az


def sample_image_bilinear(img: np.ndarray, px: np.ndarray, py: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    x0 = np.floor(px).astype(np.int64)
    y0 = np.floor(py).astype(np.int64)
    fx = (px - x0).astype(np.float32)
    fy = (py - y0).astype(np.float32)
    x0c = np.clip(x0, 0, w - 1)
    x1c = np.clip(x0 + 1, 0, w - 1)
    y0c = np.clip(y0, 0, h - 1)
    y1c = np.clip(y0 + 1, 0, h - 1)

    if img.ndim == 2:
        p00 = img[y0c, x0c]
        p10 = img[y0c, x1c]
        p01 = img[y1c, x0c]
        p11 = img[y1c, x1c]
        return (
            p00 * (1 - fx) * (1 - fy)
            + p10 * fx * (1 - fy)
            + p01 * (1 - fx) * fy
            + p11 * fx * fy
        )

    p00 = img[y0c, x0c, :].astype(np.float32)
    p10 = img[y0c, x1c, :].astype(np.float32)
    p01 = img[y1c, x0c, :].astype(np.float32)
    p11 = img[y1c, x1c, :].astype(np.float32)
    fx3 = fx[..., None]
    fy3 = fy[..., None]
    return (
        p00 * (1 - fx3) * (1 - fy3)
        + p10 * fx3 * (1 - fy3)
        + p01 * (1 - fx3) * fy3
        + p11 * fx3 * fy3
    )


def render_dome(
    cameras: list[tuple[Plate, np.ndarray]], dome_px: int = DOME_PX
) -> Image.Image:
    """Blend each camera's (already normalised) image onto the dome."""
    return DomeMaps([plate for plate, _ in cameras], dome_px=dome_px).render(
        [image for _, image in cameras]
    )


_FONT_CANDIDATES = [
    # Station deployment: ~/rovimen_scripts/fonts/
    Path.home() / "rovimen_scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf",
    # VPS deployment: assets/ sits next to this file (/opt/rovimen[-dev]/)
    Path(__file__).parent / "assets" / "VCR_OSD_MONO_1.001.ttf",
    # Repo-local development
    REPO_ROOT / "rovimen-scripts" / "fonts" / "VCR_OSD_MONO_1.001.ttf",
    # Generic fallbacks available on most Linux systems
    "DejaVuSans-Bold.ttf",
    "DejaVuSans.ttf",
]


def _font(size: int) -> ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def draw_metadata(
    img: Image.Image,
    station: str,
    label: str,
    lat: float | None = None,
    lon: float | None = None,
    timestamp: str | None = None,
    dome_px: int = DOME_PX,
) -> None:
    """Draw station name, coordinates, and timestamp in the bottom-right."""
    draw = ImageDraw.Draw(img, "RGBA")
    font = _font(18)
    lines: list[str] = []
    lines.append(f"{station} - {label}" if label else station)
    if lat is not None and lon is not None:
        ns = "N" if lat >= 0 else "S"
        ew = "E" if lon >= 0 else "W"
        lines.append(f"{abs(lat):.2f}{ns}  {abs(lon):.2f}{ew}")
    if timestamp:
        lines.append(timestamp)
    text = "\n".join(lines)
    bbox = draw.multiline_textbbox((0, 0), text, font=font)
    tw = bbox[2] - bbox[0]
    th = bbox[3] - bbox[1]
    pad = 8
    x = dome_px - tw - 20
    y = dome_px - th - 20
    draw.rounded_rectangle(
        [x - pad, y - pad, x + tw + pad, y + th + pad],
        radius=6,
        fill=(0, 0, 0, 140),
    )
    draw.multiline_text((x, y), text, fill=(200, 200, 215, 230), font=font)


def draw_overlays(img: Image.Image, plates: list[Plate], dome_px: int = DOME_PX) -> None:
    draw = ImageDraw.Draw(img, "RGBA")
    cx = cy = dome_px / 2.0
    horizon = (dome_px / 2.0) * HORIZON_FRAC

    # Horizon circle
    draw.ellipse(
        [cx - horizon, cy - horizon, cx + horizon, cy + horizon],
        outline=(220, 220, 230, 220),
        width=2,
    )
    # Altitude=30 deg and =60 deg reference rings, faint
    for alt_ref in (30, 60):
        r_ref = horizon * (90 - alt_ref) / 90.0
        draw.ellipse(
            [cx - r_ref, cy - r_ref, cx + r_ref, cy + r_ref],
            outline=(120, 120, 140, 100),
            width=1,
        )

    # Cardinal labels
    font_card = _font(40)
    cards = {"N": 0, "E": 90, "S": 180, "W": 270}
    for label, az in cards.items():
        x_h, y_h = altaz_to_dome_xy(np.array([0.0]), np.array([float(az)]), dome_px)
        hx, hy = float(x_h[0]), float(y_h[0])
        dxn = hx - cx
        dyn = hy - cy
        length = math.hypot(dxn, dyn) or 1.0
        lx = cx + dxn * (horizon + 34) / length
        ly = cy + dyn * (horizon + 34) / length
        bbox = draw.textbbox((0, 0), label, font=font_card)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        draw.text((lx - tw / 2, ly - th / 2 - 6), label, fill=(255, 230, 120, 255), font=font_card)

    palette = [
        (255, 90, 90),
        (90, 200, 255),
        (120, 255, 130),
        (255, 200, 90),
        (220, 130, 255),
    ]
    font_cam = _font(22)

    # FOV outlines per camera.  Outline segments that fall within the angular
    # FOV cone of any other camera are suppressed so intersections don't
    # look cluttered.  We use the same blend-weight criterion as render_dome:
    # a sky point "belongs" to camera j if its angular distance to j's centre
    # is less than j's FOV diagonal half-angle (rho_diag).  This catches both
    # strict pixel-bounds overlap and adjacent cameras that share sky area via
    # the soft-feathered blend without needing a pixel-space projection.
    for i, plate in enumerate(plates):
        color = palette[i % len(palette)] + (230,)
        edges = [
            (0, 0, plate.x_res - 1, 0),
            (plate.x_res - 1, 0, plate.x_res - 1, plate.y_res - 1),
            (plate.x_res - 1, plate.y_res - 1, 0, plate.y_res - 1),
            (0, plate.y_res - 1, 0, 0),
        ]
        N = 120
        for px_s, py_s, px_e, py_e in edges:
            t = np.linspace(0.0, 1.0, N)
            pxs = px_s + (px_e - px_s) * t
            pys = py_s + (py_e - py_s) * t
            alt, az = pixel_to_altaz(pxs, pys, plate)
            x, y = altaz_to_dome_xy(alt, az, dome_px)

            # Suppress segments that fall within any other camera's blend cone.
            in_overlap = np.zeros(N, dtype=bool)
            alt_r = np.radians(alt)
            az_r = np.radians(az)
            for j, other in enumerate(plates):
                if j == i:
                    continue
                alt0_j = math.radians(other.alt_centre)
                az0_j = math.radians(other.az_centre)
                cos_rho = np.clip(
                    np.sin(alt_r) * math.sin(alt0_j)
                    + np.cos(alt_r) * math.cos(alt0_j) * np.cos(az_r - az0_j),
                    -1.0, 1.0,
                )
                rho_j = np.degrees(np.arccos(cos_rho))
                rho_diag_j = math.hypot(other.fov_h / 2.0, other.fov_v / 2.0)
                in_overlap |= rho_j < rho_diag_j

            for k in range(N - 1):
                if alt[k] < 0 or alt[k + 1] < 0:
                    continue
                if in_overlap[k] or in_overlap[k + 1]:
                    continue
                draw.line(
                    [(float(x[k]), float(y[k])), (float(x[k + 1]), float(y[k + 1]))],
                    fill=color,
                    width=1,
                )

    # Camera legend — top-left corner, one row per camera, swatch colour
    # matches that camera's FOV outline. Sits off the data area so it never
    # masks the sky.
    legend_x = 22
    legend_y = 22
    swatch = 18
    row_h = 30
    for i, plate in enumerate(plates):
        color = palette[i % len(palette)] + (230,)
        row_y = legend_y + i * row_h
        draw.rectangle(
            [(legend_x, row_y + 4), (legend_x + swatch, row_y + 4 + swatch)],
            fill=color,
            outline=(0, 0, 0, 200),
            width=1,
        )
        draw.text(
            (legend_x + swatch + 8, row_y + 4),
            plate.code,
            fill=color,
            font=font_cam,
        )


def draw_timelapse_branding(
    img: Image.Image,
    *,
    timestamp: str | None = None,
    logo_path: "Path | str | None" = None,
    dome_px: int = DOME_PX,
) -> None:
    """Draw ROVIMEN branding for the nightly sky dome timelapse.

    Bottom-left: logo (if found) + "ROVIMEN" name.
    Bottom-right: date/time in a semi-transparent box.
    """
    draw = ImageDraw.Draw(img, "RGBA")
    pad = 16

    # ── Logo loading ─────────────────────────────────────────────────────────
    # The ROVIMEN logo leads; the operator logo is a small credit under it, the
    # same hierarchy as the clip bar. Text "ROVIMEN" only if the asset is missing.
    logo_img: "Image.Image | None" = None
    if logo_path is not None:
        try:
            logo_img = Image.open(logo_path).convert("RGBA")
            logo_h = 18
            logo_w = int(logo_img.width * logo_h / logo_img.height)
            if logo_w > 180:  # wide wordmarks: cap the width instead
                logo_w = 180
                logo_h = max(8, int(logo_img.height * logo_w / logo_img.width))
            logo_img = logo_img.resize((logo_w, logo_h), Image.LANCZOS)
        except Exception:
            logo_img = None
    mark_img: "Image.Image | None" = None
    try:
        mark_img = Image.open(Path(__file__).resolve().parent / "assets" / "rovimen_mark.png").convert("RGBA")
        mark_h = 64
        mark_img = mark_img.resize((int(mark_img.width * mark_h / mark_img.height), mark_h), Image.LANCZOS)
    except Exception:
        mark_img = None

    # ── Bottom-left: ROVIMEN logo over the operator logo ─────────────────────
    font_brand = _font(30)
    brand_text = "ROVIMEN"
    if mark_img:
        bw, bh = mark_img.width, mark_img.height
    else:
        b_bbox = draw.textbbox((0, 0), brand_text, font=font_brand)
        bw = b_bbox[2] - b_bbox[0]
        bh = b_bbox[3] - b_bbox[1]
    gap = 10 if logo_img else 0
    total_w = max(bw, logo_img.width if logo_img else 0)
    total_h = bh + gap + (logo_img.height if logo_img else 0)

    box_x = pad
    box_y = dome_px - total_h - pad * 2
    draw.rounded_rectangle(
        [box_x - 10, box_y - 8, box_x + total_w + 10, box_y + total_h + 8],
        radius=8,
        fill=(0, 0, 0, 150),
    )
    if mark_img:
        img.paste(mark_img, (box_x + (total_w - bw) // 2, box_y), mark_img)
    else:
        draw.text((box_x + (total_w - bw) // 2, box_y), brand_text,
                  fill=(230, 230, 245, 240), font=font_brand)
    if logo_img:
        img.paste(logo_img, (box_x + (total_w - logo_img.width) // 2, box_y + bh + gap), logo_img)

    # ── Bottom-right: date/time ───────────────────────────────────────────────
    if timestamp:
        font_ts = _font(22)
        ts_bbox = draw.textbbox((0, 0), timestamp, font=font_ts)
        tw = ts_bbox[2] - ts_bbox[0]
        th = ts_bbox[3] - ts_bbox[1]
        tx = dome_px - tw - pad * 2
        ty = dome_px - th - pad * 2
        draw.rounded_rectangle(
            [tx - 8, ty - 6, tx + tw + 8, ty + th + 6],
            radius=6,
            fill=(0, 0, 0, 150),
        )
        draw.text((tx, ty), timestamp, fill=(200, 200, 215, 230), font=font_ts)


def render_station_dome_timelapse(
    plates_with_images: "list[tuple[Plate, np.ndarray]]",
    *,
    timestamp: str | None = None,
    logo_path: "Path | str | None" = None,
    dome_px: int = DOME_PX,
) -> "Image.Image":
    """Render a sky dome frame for the nightly timelapse.

    Differs from ``render_station_dome`` in that it always draws overlays
    (cardinal directions, altitude rings, FOV outlines, camera codes at FOV
    centres) and the timelapse-specific branding (ROVIMEN logo bottom-left,
    timestamp bottom-right). No station metadata block.
    """
    normalized: "list[tuple[Plate, np.ndarray]]" = [
        (plate, normalize_camera_image(img, plate)) for plate, img in plates_with_images
    ]
    dome = render_dome(normalized, dome_px=dome_px)
    draw_overlays(dome, [plate for plate, _ in normalized], dome_px=dome_px)
    draw_timelapse_branding(dome, timestamp=timestamp, logo_path=logo_path, dome_px=dome_px)
    return dome


# ---------------------------------------------------------------------------
# Batch timelapse rendering with precomputed geometry (fast path)
# ---------------------------------------------------------------------------

class DomeMaps:
    """Precomputed dome-projection geometry for a fixed set of camera plates.

    Compute once before the frame loop; reuse across hundreds of frames.
    The per-camera warp coordinates (px_arr, py_arr) and blend weights (w)
    are constant across frames — only the pixel values change.
    """

    def __init__(self, plates: "list[Plate]", dome_px: int = DOME_PX) -> None:
        self.dome_px = dome_px
        ys, xs = np.indices((dome_px, dome_px))
        alt_grid, az_grid = dome_xy_to_altaz(
            xs.astype(np.float32), ys.astype(np.float32), dome_px
        )
        horizon_r = (dome_px / 2.0) * HORIZON_FRAC
        dome_mask = (
            (xs - dome_px / 2.0) ** 2 + (ys - dome_px / 2.0) ** 2
        ) <= horizon_r ** 2
        self._dome_mask = dome_mask
        self._bg = np.array(BACKGROUND_RGB, dtype=np.float32)

        self._cams: "list[dict | None]" = []
        for plate in plates:
            px_arr, py_arr, in_bounds = altaz_to_pixel(alt_grid, az_grid, plate)
            valid = in_bounds & dome_mask
            if not valid.any():
                self._cams.append(None)
                continue
            rho_diag = math.hypot(plate.fov_h / 2.0, plate.fov_v / 2.0)
            alt_c = math.radians(plate.alt_centre)
            az_c = math.radians(plate.az_centre)
            a = np.radians(alt_grid)
            cos_rho = np.clip(
                np.sin(a) * math.sin(alt_c)
                + np.cos(a) * math.cos(alt_c) * np.cos(np.radians(az_grid) - az_c),
                -1.0, 1.0,
            )
            rho = np.degrees(np.arccos(cos_rho))
            w = np.clip(1.0 - (rho / rho_diag) ** 2, 0.0, 1.0)
            w = np.where(valid, w, 0.0).astype(np.float32)
            self._cams.append({"px": px_arr, "py": py_arr, "w": w})

    def render(self, camera_images: "list[np.ndarray | None]") -> "Image.Image":
        """Render one dome frame from pre-normalised camera images."""
        dome_px = self.dome_px
        canvas = np.zeros((dome_px, dome_px, 3), dtype=np.float32)
        weight = np.zeros((dome_px, dome_px), dtype=np.float32)

        for cam_d, image in zip(self._cams, camera_images):
            if cam_d is None or image is None:
                continue
            img3 = np.stack([image, image, image], axis=-1) if image.ndim == 2 else image
            sampled = sample_image_bilinear(img3.astype(np.float32), cam_d["px"], cam_d["py"])
            w = cam_d["w"]
            canvas += sampled * w[..., None]
            weight += w

        nonzero = weight > 1e-6
        canvas[nonzero] = canvas[nonzero] / weight[nonzero, None]
        canvas[~nonzero] = self._bg
        canvas[~self._dome_mask] = self._bg
        return Image.fromarray(np.clip(canvas, 0, 255).astype(np.uint8))


def precompute_timelapse_overlay(
    plates: "list[Plate]", dome_px: int = DOME_PX
) -> "Image.Image":
    """Render cardinal rings, altitude circles, FOV outlines, and camera legend
    once onto a transparent RGBA image.  Composite onto each timelapse frame
    instead of redrawing per frame.
    """
    overlay = Image.new("RGBA", (dome_px, dome_px), (0, 0, 0, 0))
    draw_overlays(overlay, plates, dome_px=dome_px)
    return overlay


def render_timelapse_batch(
    plates: "list[Plate]",
    normalized_frames_per_cam: "list[list[np.ndarray]]",
    n_frames: int,
    *,
    timestamps: "list[str] | None" = None,
    logo_path: "Path | str | None" = None,
    dome_px: int = DOME_PX,
    n_workers: int = 4,
    _maps: "DomeMaps | None" = None,
    _overlay: "Image.Image | None" = None,
) -> "list[Image.Image]":
    """Render ``n_frames`` dome images efficiently.

    Differences from calling ``render_station_dome_timelapse`` in a loop:

    * ``DomeMaps`` are precomputed once — per-frame geometry (altaz_to_pixel,
      blend weights) is computed once, not 200+ times.
    * Overlays (static annotations) are precomputed once and composited.
    * Frames are rendered in parallel using a ``ThreadPoolExecutor``.
      NumPy releases the GIL during array operations, so multiple threads
      can overlap the bilinear-sampling and blending work.

    Parameters
    ----------
    plates:
        List of ``Plate`` objects (same order as ``normalized_frames_per_cam``).
    normalized_frames_per_cam:
        ``normalized_frames_per_cam[camera_index][frame_index]`` is a uint8
        ndarray that has already been passed through ``normalize_camera_image``.
    n_frames:
        Number of frames to render (``min`` across cameras).
    timestamps, logo_path, dome_px:
        Same meaning as in ``render_station_dome_timelapse``.
    n_workers:
        Thread-pool size.  Defaults to 4 — a reasonable balance for a
        shared VPS; callers may pass ``os.cpu_count() or 4`` for full use.
    _maps, _overlay:
        Pre-built ``DomeMaps`` and overlay RGBA image.  Pass these when
        calling in a loop (e.g. streaming batches) to avoid recomputing
        the dome geometry on every call.
    """
    from concurrent.futures import ThreadPoolExecutor

    maps = _maps if _maps is not None else DomeMaps(plates, dome_px=dome_px)
    overlay = _overlay if _overlay is not None else precompute_timelapse_overlay(plates, dome_px=dome_px)

    def _render_one(i: int) -> "Image.Image":
        images = [
            frames[i] if i < len(frames) else None
            for frames in normalized_frames_per_cam
        ]
        dome = maps.render(images)
        dome_rgba = dome.convert("RGBA")
        dome_rgba.alpha_composite(overlay)
        dome = dome_rgba.convert("RGB")
        ts = timestamps[i] if timestamps else None
        draw_timelapse_branding(dome, timestamp=ts, logo_path=logo_path, dome_px=dome_px)
        return dome

    results: "list[Image.Image | None]" = [None] * n_frames
    with ThreadPoolExecutor(max_workers=n_workers) as pool:
        futs = {pool.submit(_render_one, i): i for i in range(n_frames)}
        for fut in futs:
            results[futs[fut]] = fut.result()

    return results  # type: ignore[return-value]


def normalize_camera_image(img: np.ndarray, plate: Plate) -> np.ndarray:
    """Resize and optionally de-rotate ``img`` to match the platepar frame.

    The renderer assumes per-pixel correspondence between the stack image and
    the platepar's reported resolution; mismatches show up as warped FOV edges.

    When ``plate.rotate`` is True the stored image has been rotated 180° for
    display purposes, but the platepar was calibrated against the raw frame.
    We flip it back so that ``altaz_to_pixel`` samples the correct pixels.
    """
    if img.ndim == 2:
        img = np.stack([img, img, img], axis=-1)
    if plate.rotate:
        img = np.rot90(img, 2)
    if (img.shape[1], img.shape[0]) != (plate.x_res, plate.y_res):
        img = np.asarray(
            Image.fromarray(img).resize((plate.x_res, plate.y_res), Image.LANCZOS)
        )
    return img


def render_station_dome(
    plates_with_images: list[tuple[Plate, np.ndarray]],
    *,
    station: str | None = None,
    label: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    timestamp: str | None = None,
    dome_px: int = DOME_PX,
    overlay: bool = False,
) -> Image.Image:
    """Assemble a celestial-dome image from per-camera (plate, image) pairs.

    Public entry point used by the station renderer (``sky_dome_render.py``)
    and the dashboard's fallback ``/api/sky_dome/<host>.png`` render. Caller is
    responsible for fetching the
    platepars and stack images; this function just reprojects, blends, and
    optionally draws overlays.

    When ``overlay`` is False (the default for the dashboard), the image is a
    clean mosaic with no annotations. When True, cardinal labels, FOV outlines,
    camera legend, and the station metadata block are drawn on top.
    """
    normalized: list[tuple[Plate, np.ndarray]] = [
        (plate, normalize_camera_image(img, plate)) for plate, img in plates_with_images
    ]
    dome = render_dome(normalized, dome_px=dome_px)
    if overlay:
        draw_overlays(dome, [plate for plate, _ in normalized], dome_px=dome_px)
        if station:
            draw_metadata(dome, station, label or "", lat=lat, lon=lon,
                          timestamp=timestamp, dome_px=dome_px)
    return dome
