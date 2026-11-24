"""Dashboard-side overlay gate for per-detection meteor stack images.

The station stacker (``rovimen-scripts/stacker.py`` -> ``overlay.annotate_still``)
burns the ROVIMEN/astromania branding + bottom data strip into every per-chunk
``*_stack.webp`` at generation, so freshly produced stacks reach the archive
already attributed. This module closes the gap for the images that predate that
overlay (older nights, or stacks produced while the station overlay was off):
before the dashboard serves or downloads a per-detection stack it funnels the
bytes through :func:`ensure_overlaid_stack_bytes`, which renders the overlay on
demand and never returns the un-branded original.

The overlay is applied with the *same* ``overlay.py`` the station encoder uses
(loaded from the deployed station bundle), so the on-demand variant is visually
identical to the station-burned one. An EXIF UserComment marker
(``overlay.still_overlay_marker()``) written by ``annotate_still`` lets this gate
recognise an already-branded stack and pass it through untouched -- no double
stamping.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
import time
import types
from pathlib import Path

from atomic_write import write_atomic

logger = logging.getLogger(__name__)


def _bundle_path() -> Path:
    """Return the deployed station-bundle dir (``ROVIMEN_BUNDLE_DIR`` for tests)."""
    base = Path(os.environ.get("ROVIMEN_BUNDLE_DIR", "/opt/rovimen"))
    return base / "station-bundle"


def _load_overlay_module() -> types.ModuleType | None:
    """Compile the bundled ``overlay.py`` in memory.

    In-memory compile (not ``spec_from_file_location``) mirrors
    ``routes/media.py``: a file-based loader writes ``__pycache__/`` into the
    signed station bundle, which then fails the stations' exact-set manifest
    verification on the next pull.
    """
    p = _bundle_path() / "overlay.py"
    if not p.exists():
        return None
    try:
        source = p.read_bytes()
        mod = types.ModuleType("rovimen_overlay_stackgate")
        mod.__file__ = str(p)
        exec(compile(source, str(p), "exec"), mod.__dict__)  # noqa: S102
        return mod
    except Exception:
        logger.exception("failed to load bundled overlay.py for stack gate")
        return None


_overlay_mod = _load_overlay_module()


#: Mark used when a station's own asset is not shipped in the bundle.
_FALLBACK_LOGO = "astromania_text.png"


def bundle_logo_for(overlay_cfg: dict) -> Path | None:
    """The bundle copy of the logo THIS station renders, by filename.

    The station config points ``logo`` at the station's own home dir, which does
    not exist on the VPS, so the path has to be remapped to the bundle before the
    dashboard can render with it. Remapping to a fixed
    ``astromania_text.png`` -- as this did until 2026-08-25 -- silently
    re-attributed every dashboard-rendered image: the partner clubs'
    emblems and the GMN
    sphere all came back stamped ASTROMANIA, crediting the wrong association on
    exactly the images a visitor downloads. Matching on the BASENAME keeps the
    station's own mark whenever the bundle ships it (every asset in
    ``rovimen-scripts/assets/`` does), and falls back to the network wordmark
    only for a station-local file the VPS has never seen -- branded beats
    unbranded, but only as a last resort.
    """
    assets = _bundle_path() / "assets"
    name = Path(str(overlay_cfg.get("logo") or "")).name
    if name:
        own = assets / name
        if own.is_file():
            return own
    fallback = assets / _FALLBACK_LOGO
    return fallback if fallback.is_file() else None


def _bundle_overlay_cfg(overlay_cfg: dict) -> dict:
    """Remap station-local font/logo paths onto the VPS bundle copies."""
    cfg = dict(overlay_cfg or {})
    font = _bundle_path() / "fonts" / "VCR_OSD_MONO_1.001.ttf"
    if font.exists():
        cfg["font"] = str(font)
    logo = bundle_logo_for(cfg)
    if logo is not None:
        cfg["logo"] = str(logo)
    return cfg


def _chunk_epoch_from_name(filename: str) -> int:
    """Derive the UTC epoch of the chunk start from ``<cam>_<YYYYMMDD>_<HHMMSS>_...``."""
    import re
    from datetime import datetime, timezone

    m = re.match(r"^.+?_(\d{8})_(\d{6})", filename)
    if not m:
        return 0
    try:
        dt = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(
            tzinfo=timezone.utc
        )
        return int(dt.timestamp())
    except ValueError:
        return 0


def stack_is_overlaid(path: str | Path) -> bool:
    """True when the stack at *path* already carries the burned-in overlay."""
    if _overlay_mod is None:
        return False
    try:
        return bool(_overlay_mod.still_is_overlaid(path))
    except Exception:
        return False


# ``<cam>_<YYYYMMDD>_<HHMMSS>_<HHMMSS>_stack.webp`` -- a stack built for a clip
# stitched across a chunk boundary. Single-timestamp stacks come straight out of
# stacker.py, which stamps the EXIF marker; stitched ones are max-combined from
# two ALREADY-OVERLAID stacks by detection_lock._stitch_stacks, whose PIL save
# dropped the marker, so this gate re-branded them and shipped a second bar.
# detection_lock now stamps the marker, but every stitched stack already on the
# storage box (~30 per camera per night, and they are disproportionately the
# long/bright meteors that cross a boundary) is still unmarked, and re-saving
# archive WebPs just to add EXIF would re-compress them. Recognising the name is
# free and exactly as sound: a stitched stack can only be made from overlaid
# sources, so it always already carries the bar.
_STITCHED_STACK_RE = re.compile(r"_\d{8}_\d{6}_\d{6}_stack\.(?:webp|png)$", re.I)


def stack_name_implies_overlaid(filename: str) -> bool:
    """True when *filename* is a stitched-clip stack, which is barred by construction."""
    return bool(_STITCHED_STACK_RE.search(filename or ""))


def ensure_overlaid_stack_bytes(
    src: str | Path,
    filename: str,
    overlay_cfg: dict,
    station_code: str,
    station_cfg: dict,
) -> bytes | None:
    """Return the branded WebP bytes for the per-detection stack at *src*
    (see :func:`overlaid_stack_bytes`)."""
    return overlaid_stack_bytes(src, filename, overlay_cfg, station_code, station_cfg)[0]


def overlaid_stack_bytes(
    src: str | Path,
    filename: str,
    overlay_cfg: dict,
    station_code: str,
    station_cfg: dict,
) -> tuple[bytes | None, bool]:
    """Return the branded WebP bytes for the per-detection stack at *src*, and
    whether they were rendered here (False: *src* passed through unchanged, or
    no bytes).

    - If *src* already carries the overlay marker, its bytes are returned
      unchanged (no double stamping).
    - Otherwise the overlay is rendered on demand with the bundled
      ``overlay.annotate_still`` and the branded bytes are returned.
    - If the overlay cannot be produced (no bundle, ffmpeg/font failure), returns
      ``None`` so the caller refuses to serve the un-branded original.

    When the station-wide overlay is switched off (``overlay.enabled`` false),
    the stack as-is *is* the attributed variant, so the original bytes are
    returned -- serving them is not a bypass.
    """
    src = Path(src)
    try:
        original = src.read_bytes()
    except OSError:
        return None, False

    overlay_cfg = overlay_cfg or {}
    if not overlay_cfg.get("enabled", True):
        return original, False

    if _overlay_mod is None:
        # No bundle to render with -- refuse rather than leak an un-branded image.
        logger.warning("stack overlay gate: no bundled overlay.py, refusing %s", filename)
        return None, False

    if stack_is_overlaid(src) or stack_name_implies_overlaid(filename):
        return original, False

    bundle_cfg = _bundle_overlay_cfg(overlay_cfg)
    chunk_epoch = _chunk_epoch_from_name(filename)

    tmp_dir = tempfile.mkdtemp(prefix="rovimen_stackovl_")
    try:
        tmp_out = Path(tmp_dir) / "out.webp"
        ok = _overlay_mod.annotate_still(
            src, tmp_out, bundle_cfg, station_code, station_cfg or {}, chunk_epoch
        )
        if not ok or not tmp_out.exists():
            logger.warning("stack overlay gate: render failed for %s", filename)
            return None, False
        return tmp_out.read_bytes(), True
    except Exception:
        logger.exception("stack overlay gate: render errored for %s", filename)
        return None, False
    finally:
        import shutil

        shutil.rmtree(tmp_dir, ignore_errors=True)


# Fleet-default overlay used when a station is unreachable (offline nights). It
# still burns the ROVIMEN network name + station + timestamp data strip; az/alt
# fall back to 0 rather than being fabricated, so the attribution lands even
# without a live station round-trip. Font/logo are remapped to bundle copies by
# ``_bundle_overlay_cfg``.
_FLEET_DEFAULT_OVERLAY: dict = {
    "enabled": True,
    "network": "ROVIMEN",
    "style": "cinema",
    "show_logo": True,
    "show_network": True,
    "show_station": True,
    "show_timestamp": True,
    "show_pointing": False,
    "show_coords": True,
}

def _has_pointing(station_cfg: dict) -> bool:
    """True when *station_cfg* already carries a usable, non-zero az/alt.

    The station-burned overlay uses ``station_cfg['az'/'alt']`` verbatim, so a
    live value of 0.0 (the sentinel a station without a platepar sends) is
    treated as "no pointing" and falls through to the dashboard fallbacks.
    """
    try:
        az = station_cfg.get("az")
        alt = station_cfg.get("alt")
    except AttributeError:
        return False
    if az is None or alt is None:
        return False
    try:
        return float(az) != 0.0 or float(alt) != 0.0
    except (TypeError, ValueError):
        return False


def resolve_camera_pointing(
    config,
    host_key: str,
    station_code: str,
    station_cfg: dict,
) -> dict:
    """Fill a camera's az/alt from the dashboard's own knowledge when the station
    settings lack it.

    The dashboard render path only reads pointing from the station's live
    ``/api/settings``; an unreachable station (e.g. gmnro16/RO001E) then renders
    ALT 0.0 / AZ 0.0 even though the dashboard already knows where the camera
    points. Fall back, in order, to:

      1. the ``station_cfg`` az/alt the station itself returned (when non-zero);
      2. ``config.stations[host_key].cameras[<code>].az/alt`` (the pointing an
         admin set in ``dashboard_config.yaml``);
      3. ``platepar_store``'s ``az_centre``/``alt_centre`` for the camera code
         (the last-known platepar, kept even while the station is offline).

    Returns a station_cfg dict with ``az``/``alt`` populated; the input is not
    mutated. Leaves az/alt absent only when no source has them, so the overlay
    still degrades to 0.0 rather than fabricating a value.
    """
    station_cfg = dict(station_cfg or {})
    if _has_pointing(station_cfg):
        return station_cfg

    # 2. dashboard_config.yaml camera pointing.
    try:
        station = config.stations.get(host_key) if config is not None else None
    except AttributeError:
        station = None
    if station is not None:
        cam = next((c for c in station.cameras if c.code == station_code), None)
        if cam is not None and cam.az is not None and cam.alt is not None:
            station_cfg["az"] = float(cam.az)
            station_cfg["alt"] = float(cam.alt)
            logger.debug(
                "%s/%s: az/alt from dashboard config: az=%.1f alt=%.1f",
                host_key, station_code, station_cfg["az"], station_cfg["alt"],
            )
            return station_cfg

    # 3. last-known platepar (az_centre/alt_centre), even while offline.
    try:
        import platepar_store

        pp = platepar_store.get_all().get(station_code)
    except Exception:
        pp = None
    if isinstance(pp, dict):
        az_c = pp.get("az_centre")
        alt_c = pp.get("alt_centre")
        if az_c is not None and alt_c is not None:
            station_cfg["az"] = float(az_c)
            station_cfg["alt"] = float(alt_c)
            logger.debug(
                "%s/%s: az/alt from platepar_store: az=%.1f alt=%.1f",
                host_key, station_code, station_cfg["az"], station_cfg["alt"],
            )

    return station_cfg


# (settings or None, expiry) keyed by host_key. 30 min, so a stack download or
# a crop does not round-trip to the station on every request; a failed read is
# cached too, so an offline station is not retried per request.
_settings_cache: dict[str, tuple[dict | None, float]] = {}
_OV_TTL_S = 1800.0


def station_settings(config, tunnels, host_key: str) -> dict | None:
    """The station's live ``/api/settings``, cached 30 min per host; None when
    it could not be read or was not a JSON object (offline node, old night).

    Shared by the two overlay-config readers: :func:`fetch_overlay_config`
    (branded stack downloads) and ``routes.media``'s crop/fullstack renders,
    which differ only in what they make of a missing ``overlay`` block.
    """
    now = time.monotonic()
    cached = _settings_cache.get(host_key)
    if cached and cached[1] > now:
        return cached[0]
    settings: dict | None = None
    try:
        from station_client import station_get_raw

        raw = station_get_raw(config, tunnels, host_key, "/api/settings", timeout=8)
        if isinstance(raw, dict):
            settings = raw
    except Exception:
        logger.debug("station settings fetch failed for %s", host_key)
    _settings_cache[host_key] = (settings, now + _OV_TTL_S)
    return settings


def fetch_overlay_config(
    config,
    tunnels,
    host_key: str,
    station_code: str,
) -> tuple[dict, dict]:
    """Return ``(overlay_cfg, station_cfg)`` for a station, cached 30 min.

    Pulls the live ``overlay`` block + per-station pointing from the station's
    ``/api/settings``; on any failure (offline node, old night) or an empty
    ``overlay`` block falls back to the fleet-default overlay so a legacy
    un-branded stack can still be attributed. When the station settings lack
    the camera's az/alt, the pointing is filled from the dashboard config /
    platepar store (see ``resolve_camera_pointing``) so an offline station
    still renders its real ALT/AZ, not 0.0.
    """
    settings = station_settings(config, tunnels, host_key)
    overlay_cfg: dict = dict(_FLEET_DEFAULT_OVERLAY)
    stations_cfg: dict = {}
    if settings is not None:
        live = settings.get("overlay")
        if isinstance(live, dict) and live:
            overlay_cfg = dict(live)
        sc = settings.get("stations")
        if isinstance(sc, dict):
            stations_cfg = sc
    return overlay_cfg, resolve_camera_pointing(
        config, host_key, station_code, stations_cfg.get(station_code, {})
    )


class OverlaidStackCache:
    """Small on-disk cache of branded per-detection stacks.

    Keyed by ``(camera, date, filename)``. The gate renders the overlay once,
    caches the branded WebP under ``cache_dir``, and serves it on subsequent
    hits so a repeatedly-downloaded stack is not re-rendered every time. Only
    renders are cached (a stack already branded is served from the archive),
    and ``cache_store._prune_branded_stacks`` ages night dirs out hourly.
    """

    def __init__(self, cache_dir: str | Path) -> None:
        self._dir = Path(cache_dir)

    def _path(self, camera: str, date: str, filename: str) -> Path:
        return self._dir / camera / date / filename

    def get(self, camera: str, date: str, filename: str) -> Path | None:
        p = self._path(camera, date, filename)
        if p.exists() and p.stat().st_size > 0:
            return p
        return None

    def put(self, camera: str, date: str, filename: str, data: bytes) -> Path | None:
        p = self._path(camera, date, filename)
        try:
            write_atomic(
                p, data, mkdir=True,
                tmp=p.with_suffix(p.suffix + f".{os.getpid()}.{int(time.time()*1000)}.part"),
            )
            return p
        except OSError:
            logger.debug("stack overlay cache write failed for %s", p)
            return None
