"""Ground-track map for the fireball alert: OpenStreetMap tiles with the part of the
trail each camera saw (from the FR pair geometry) and the cameras that saw it.

The alert daemon calls :func:`render_track_map` for the preliminary and the confirmed
message when at least one FR pair passed the geometry test, and sends the JPEG as a photo.
Tiles come from tile.openstreetmap.org with an identifying User-Agent, a handful per event
(the mosaic is capped at MAX_TILES), which is inside the OSM tile usage policy for this volume;
the attribution is drawn on the image. ``fetch_tile`` is injectable so the tests draw with
blank tiles and no network.
"""

from __future__ import annotations

import io
import logging
import math
from typing import Any, Callable

logger = logging.getLogger("fireball_map")

TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "ROVIMEN fireball alert/1.0 (+https://dashboard.rovimen.org)"
TILE = 256
MAX_W, MAX_H = 1000, 760
MAX_TILES = 30
PAD = 0.3            # fraction of the track/camera extent added on each side
ZOOM_MAX, ZOOM_MIN = 11, 5
ATTRIBUTION = "© OpenStreetMap contributors"

_cache: dict[tuple[int, int, int], bytes | None] = {}


def _fetch_osm(z: int, x: int, y: int) -> bytes | None:
    key = (z, x, y)
    if key in _cache:
        return _cache[key]
    import requests
    data = None
    try:
        r = requests.get(TILE_URL.format(z=z, x=x, y=y), headers={"User-Agent": USER_AGENT}, timeout=10)
        if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
            data = r.content
        else:
            logger.warning("tile %d/%d/%d: HTTP %d", z, x, y, r.status_code)
    except Exception as exc:
        logger.warning("tile %d/%d/%d: %s", z, x, y, type(exc).__name__)
    if len(_cache) > 2000:
        _cache.clear()
    _cache[key] = data
    return data


def _xy(lat: float, lon: float, z: int) -> tuple[float, float]:
    """Fractional tile coordinates (Web Mercator)."""
    n = 2 ** z
    x = (lon + 180.0) / 360.0 * n
    la = math.radians(max(-85.05, min(85.05, lat)))
    y = (1.0 - math.log(math.tan(la) + 1.0 / math.cos(la)) / math.pi) / 2.0 * n
    return x, y


def _km_per_px(lat: float, z: int) -> float:
    return 40075.016686 * math.cos(math.radians(lat)) / (TILE * 2 ** z)


ROMANIA_BOX = (43.5, 48.4, 20.1, 30.0)   # lat0, lat1, lon0, lon1: the country with a small margin


def overview_box(lats: list[float], lons: list[float]) -> tuple[float, float, float, float]:
    """Romania, widened when the trail or a camera (Moldova, Germany) lies outside it."""
    lat0, lat1, lon0, lon1 = ROMANIA_BOX
    return (min(lat0, min(lats) - 0.3), max(lat1, max(lats) + 0.3),
            min(lon0, min(lons) - 0.4), max(lon1, max(lons) + 0.4))


def choose_zoom(lats: list[float], lons: list[float],
                fixed_box: tuple[float, float, float, float] | None = None) -> tuple[int, tuple[float, float, float, float]]:
    """Largest zoom whose padded bounding box fits MAX_W x MAX_H pixels and MAX_TILES tiles.
    ``fixed_box`` (lat0, lat1, lon0, lon1) replaces the box around the points, without padding.
    Returns (zoom, (x0, y0, x1, y1)) in fractional tile units at that zoom."""
    if fixed_box is not None:
        lat0, lat1, lon0, lon1 = fixed_box
    else:
        lat0, lat1 = min(lats), max(lats)
        lon0, lon1 = min(lons), max(lons)
        dlat = max(lat1 - lat0, 0.15)
        dlon = max(lon1 - lon0, 0.25)
        # no strips: the frame is at least half as tall as it is wide (in ground distance)
        lat_c, lon_c = (lat0 + lat1) / 2, (lon0 + lon1) / 2
        min_dlat = 0.5 * dlon * math.cos(math.radians(lat_c))
        if dlat < min_dlat:
            dlat = min_dlat
        lat0, lat1 = lat_c - dlat / 2 - PAD * dlat, lat_c + dlat / 2 + PAD * dlat
        lon0, lon1 = lon_c - dlon / 2 - PAD * dlon, lon_c + dlon / 2 + PAD * dlon
    for z in range(ZOOM_MAX, ZOOM_MIN - 1, -1):
        x0, y0 = _xy(lat1, lon0, z)
        x1, y1 = _xy(lat0, lon1, z)
        w, h = (x1 - x0) * TILE, (y1 - y0) * TILE
        ntiles = (math.floor(x1) - math.floor(x0) + 1) * (math.floor(y1) - math.floor(y0) + 1)
        if w <= MAX_W and h <= MAX_H and ntiles <= MAX_TILES:
            return z, (x0, y0, x1, y1)
    z = ZOOM_MIN
    x0, y0 = _xy(lat1, lon0, z)
    x1, y1 = _xy(lat0, lon1, z)
    return z, (x0, y0, x1, y1)


def _mosaic(z: int, box: tuple[float, float, float, float], fetch_tile: Callable[[int, int, int], bytes | None]) -> Any:
    """PIL image of the box, cropped from the tiles that cover it."""
    from PIL import Image
    x0, y0, x1, y1 = box
    tx0, ty0, tx1, ty1 = math.floor(x0), math.floor(y0), math.floor(x1), math.floor(y1)
    img = Image.new("RGB", ((tx1 - tx0 + 1) * TILE, (ty1 - ty0 + 1) * TILE), (225, 225, 220))
    n = 2 ** z
    for tx in range(tx0, tx1 + 1):
        for ty in range(ty0, ty1 + 1):
            if not (0 <= ty < n):
                continue
            data = fetch_tile(z, tx % n, ty)
            if not data:
                continue
            try:
                tile = Image.open(io.BytesIO(data)).convert("RGB")
            except Exception:
                continue
            img.paste(tile, ((tx - tx0) * TILE, (ty - ty0) * TILE))
    left, top = int((x0 - tx0) * TILE), int((y0 - ty0) * TILE)
    right, bottom = int((x1 - tx0) * TILE), int((y1 - ty0) * TILE)
    return img.crop((left, top, max(right, left + 1), max(bottom, top + 1)))


def render_track_map(tracks: list[dict[str, Any]], cameras: list[tuple[str, float, float]], title: str,
                     fetch_tile: Callable[[int, int, int], bytes | None] | None = None,
                     overview: bool = False) -> bytes:
    """JPEG of the ground tracks and cameras over OSM tiles.

    tracks: {"label", "begin": (lat, lon), "end": (lat, lon), "h_begin", "h_end"} per camera, heights in km.
    cameras: (label, lat, lon). ``overview`` frames the whole country instead of the trail.
    Raises if there is nothing to draw.
    """
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import FancyArrowPatch

    fetch_tile = fetch_tile or _fetch_osm
    lats = [p[0] for t in tracks for p in (t["begin"], t["end"])] + [c[1] for c in cameras]
    lons = [p[1] for t in tracks for p in (t["begin"], t["end"])] + [c[2] for c in cameras]
    if not lats:
        raise ValueError("nothing to draw")
    z, box = choose_zoom(lats, lons, overview_box(lats, lons) if overview else None)
    img = _mosaic(z, box, fetch_tile)
    w, h = img.size

    def px(lat: float, lon: float) -> tuple[float, float]:
        x, y = _xy(lat, lon, z)
        return (x - box[0]) * TILE, (y - box[1]) * TILE

    fig = plt.figure(figsize=(w / 100, h / 100), dpi=100)
    ax = fig.add_axes((0, 0, 1, 1))
    ax.imshow(img, extent=(0, w, h, 0), interpolation="bilinear")
    ax.set_xlim(0, w)
    ax.set_ylim(h, 0)
    ax.set_axis_off()
    colours = ["#d62728", "#ff7f0e", "#9467bd", "#2ca02c", "#17becf"]
    handles = []
    # The whole trail: from the highest point any camera saw to the lowest, as one thick arrow.
    top = max(tracks, key=lambda t: t["h_begin"])
    low = min(tracks, key=lambda t: t["h_end"])
    (xa, ya), (xb, yb) = px(*top["begin"]), px(*low["end"])
    ax.add_patch(FancyArrowPatch((xa, ya), (xb, yb), arrowstyle="-|>", mutation_scale=22, lw=4,
                                 color="#111111", alpha=0.85, zorder=6))
    ax.plot([xa], [ya], "o", color="#111111", ms=7, zorder=7)
    dx, dy = xb - xa, yb - ya
    norm = math.hypot(dx, dy) or 1.0
    nx, ny = -dy / norm, dx / norm          # unit normal, for offsets beside the line
    ax.annotate(f"{top['h_begin']:.0f} km", (xa, ya), xytext=(-14 * nx - 8 * dx / norm, -14 * ny - 8 * dy / norm),
                textcoords="offset points", fontsize=10, fontweight="bold", ha="center", va="center", zorder=9,
                bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#111111", "alpha": 0.9})
    ax.annotate(f"{low['h_end']:.0f} km", (xb, yb), xytext=(14 * nx + 10 * dx / norm, 14 * ny + 10 * dy / norm),
                textcoords="offset points", fontsize=10, fontweight="bold", ha="center", va="center", zorder=9,
                bbox={"boxstyle": "round,pad=0.25", "fc": "white", "ec": "#111111", "alpha": 0.9})
    # What each camera saw: a thin line beside the trail, one colour per camera, named in the legend.
    cam_colour: dict[str, str] = {}
    for k, t in enumerate(sorted(tracks, key=lambda t: -t["h_begin"])):
        c = colours[k % len(colours)]
        cam_colour[t["label"]] = c
        off = 5.0 * (k + 1)
        (x0, y0), (x1, y1) = px(*t["begin"]), px(*t["end"])
        line, = ax.plot([x0 + off * nx, x1 + off * nx], [y0 + off * ny, y1 + off * ny], "-", color=c, lw=2.2,
                        alpha=0.95, zorder=5, solid_capstyle="round",
                        label=f"{t['label']}: {t['h_begin']:.0f} to {t['h_end']:.0f} km")
        handles.append(line)
    for label, lat, lon in cameras:
        x, y = px(lat, lon)
        c = cam_colour.get(label, "#1f4e9c")
        ax.plot([x], [y], marker="^", color=c, ms=12, mec="white", mew=1.3, zorder=8)
        ax.annotate(label, (x, y), xytext=(0, -15), textcoords="offset points", fontsize=8.5,
                    color="#222", ha="center", va="top", zorder=8,
                    bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.85})
    if handles:
        leg = ax.legend(handles=handles, loc="upper right", fontsize=8.5, framealpha=0.92, title="each camera saw",
                        title_fontsize=8.5, borderpad=0.6)
        leg.set_zorder(10)
    # scale bar, bottom left
    lat_c = sum(lats) / len(lats)
    kmpp = _km_per_px(lat_c, z)
    bar_km = next((v for v in (200, 100, 50, 20, 10, 5, 2) if v / kmpp <= w * 0.3), 1)
    bar_px = bar_km / kmpp
    ax.plot([12, 12 + bar_px], [h - 14, h - 14], color="black", lw=3, zorder=9,
            solid_capstyle="butt")
    ax.annotate(f"{bar_km} km", (12 + bar_px / 2, h - 18), fontsize=8, ha="center", va="bottom", zorder=9,
                bbox={"boxstyle": "round,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.8})
    ax.text(6, 6, title, fontsize=10, va="top", ha="left", zorder=9,
            bbox={"boxstyle": "round,pad=0.3", "fc": "white", "ec": "none", "alpha": 0.9})
    ax.text(w - 4, h - 4, ATTRIBUTION, fontsize=7, ha="right", va="bottom", color="#333", zorder=9,
            bbox={"boxstyle": "square,pad=0.15", "fc": "white", "ec": "none", "alpha": 0.8})
    buf = io.BytesIO()
    fig.savefig(buf, format="jpeg", dpi=100, pil_kwargs={"quality": 88})
    plt.close(fig)
    return buf.getvalue()
