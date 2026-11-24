#!/usr/bin/env python3
"""Station-side sky-dome renderer (PR2: dome rendering moved off the VPS).

Historically the dashboard composited every station's celestial dome itself —
fetching each camera's platepar + latest stack over the network and reprojecting
them into a 1600px hemisphere, per station, in every gunicorn worker. That render
(and especially the nightly dome *timelapse*) ratcheted VPS RAM into the cgroup
memory-throttle that froze the site.

This script moves that work to the station, which already holds the platepars and
stacks locally. It renders the dome PNG (clean + annotated) exactly the way the
dashboard did — via the SAME ``celestial_dome`` module and the SAME local
station-API endpoints the VPS used to call remotely — and uploads the finished
PNG over the existing HTTPS ingest byte-plane (the channel ``archive_upload`` and
``rovimen_pusher`` already use; no new endpoint, no SSH key). The dashboard then
serves the uploaded file and only renders as a fallback for stations that don't
run this yet (see ``dashboard/routes/sky_dome.py`` and the shared upload contract
in ``celestial_dome`` — ``station_dome_primary_cam`` / ``station_dome_png_name``).

Opt-in: this ships in the bundle but the systemd unit is enabled only when
``services.sky_dome_render.enabled`` is true in ``config.json`` (default false),
so it can be validated on one station before fleet rollout.

Usage:
    python sky_dome_render.py -c ~/rovimen_scripts/config.json          # one pass
    python sky_dome_render.py -c ~/rovimen_scripts/config.json --daemon # loop
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import rovimen_config

logger = logging.getLogger("sky_dome_render")

DEFAULT_CONFIG = Path.home() / "rovimen_scripts" / "config.json"
DEFAULT_INTERVAL_S = 300  # match the dashboard's ~5-min night TTL
_HTTP_TIMEOUT = 20


def _load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _dome_capture_night(now: datetime | None = None) -> str:
    """Capture-night date (YYYYMMDD): before noon UTC belongs to the prior
    evening. Mirrors the dashboard so the upload lands where it looks."""
    now = now or datetime.now(timezone.utc)
    if now.hour < 12:
        return (now - timedelta(days=1)).strftime("%Y%m%d")
    return now.strftime("%Y%m%d")


def _station_api_base(cfg: dict) -> str:
    port = cfg.get("station_api_port", 7779)
    return f"http://127.0.0.1:{port}"


def _get(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=_HTTP_TIMEOUT) as resp:  # noqa: S310
            if 200 <= resp.status < 300:
                return resp.read()
    except (urllib.error.URLError, OSError) as exc:
        logger.debug("GET %s failed: %s", url, exc)
    return None


def _get_json(url: str) -> dict | None:
    raw = _get(url)
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _cameras(cfg: dict) -> dict[str, dict]:
    """Camera code -> its config block: the keys of ``config.json``'s
    ``stations``, the same set station_api's ``/api/platepar`` answers for."""
    cams = cfg.get("stations")
    if not isinstance(cams, dict):
        return {}
    return {code: (block if isinstance(block, dict) else {})
            for code, block in cams.items() if code}


def _host_key(cfg: dict) -> str | None:
    """This station's host key, resolved as the pusher and uploader do."""
    try:
        from rovimen_pusher import resolve_host_key
    except Exception as exc:  # noqa: BLE001
        logger.debug("cannot import rovimen_pusher: %s", exc)
        return None
    return resolve_host_key(cfg)


def _fetch_stack_bytes(api_base: str, cam_code: str) -> bytes | None:
    """Freshest colour chunk stack for a camera, mirroring the dashboard's
    source order: latest_frame (rolling chunk stack) then latest_stack."""
    for endpoint in ("latest_frame", "latest_stack"):
        data = _get(f"{api_base}/api/{endpoint}/{cam_code}")
        if data:
            return data
    return None


def _build_plates_with_images(cfg: dict, api_base: str):
    """Return ``[(Plate, np.ndarray)]`` for every camera with a valid platepar
    and a reachable stack — the exact input ``render_station_dome`` expects."""
    import numpy as np
    from PIL import Image

    from celestial_dome import Plate

    platepars = _get_json(f"{api_base}/api/platepar")
    if not platepars:
        logger.warning("no platepar payload from station API (%s/api/platepar)", api_base)
        return []

    out: list[tuple] = []
    for code, cam in _cameras(cfg).items():
        entry = platepars.get(code)
        if not entry or entry.get("error"):
            continue
        try:
            plate = Plate.from_dict(entry, code)
        except (KeyError, TypeError, ValueError):
            logger.debug("platepar unusable for %s", code)
            continue
        img_bytes = _fetch_stack_bytes(api_base, code)
        if not img_bytes:
            logger.debug("no stack image for %s", code)
            continue
        try:
            pil = Image.open(io.BytesIO(img_bytes)).convert("RGB")
            # Undo the display 180 rotation so pixels match the platepar frame
            # (the dashboard applies the same correction).
            if cam.get("rotate"):
                pil = pil.rotate(180)
            out.append((plate, np.asarray(pil)))
        except Exception:  # noqa: BLE001 - never let one bad image abort the dome
            logger.debug("stack image decode failed for %s", code, exc_info=True)
            continue
    return out


def _upload_png(cfg: dict, primary_cam: str, night: str, filename: str, png: bytes) -> bool:
    """Upload one dome PNG over the ingest byte-plane, under the primary
    camera's night tree in the allowed ``stacks/`` subdir (see the contract in
    celestial_dome). Reuses archive_upload/rovimen_pusher cred resolution."""
    try:
        from ingest_blob import post_blob
        from rovimen_pusher import resolve_base_url, resolve_host_key, resolve_ingest_key

        from celestial_dome import STATION_DOME_PNG_SUBDIR
    except Exception as exc:  # noqa: BLE001
        logger.error("cannot import upload helpers: %s", exc)
        return False

    base_url = resolve_base_url(cfg)
    host_key = resolve_host_key(cfg)
    ingest_key = resolve_ingest_key()
    if not (base_url and host_key and ingest_key):
        logger.warning("ingest not configured (base_url/host_key/key); cannot upload dome")
        return False

    relpath = f"{STATION_DOME_PNG_SUBDIR}/{filename}"
    url = f"{base_url.rstrip('/')}/api/ingest/v1/{host_key}/blob/{primary_cam}/{night}/{relpath}"
    return post_blob(url, ingest_key, png)


def render_once(cfg: dict) -> bool:
    """Render the station's dome (clean + annotated) and upload both. Returns
    True if at least one PNG was rendered and uploaded."""
    from celestial_dome import render_station_dome, station_dome_png_name, station_dome_primary_cam

    primary_cam = station_dome_primary_cam(list(_cameras(cfg)))
    if primary_cam is None:
        logger.warning("station has no cameras; nothing to render")
        return False

    api_base = _station_api_base(cfg)
    plates_with_images = _build_plates_with_images(cfg, api_base)
    if not plates_with_images:
        logger.info("no camera has both a platepar and a stack yet; skipping dome render")
        return False

    # A station knows its host key but not the place name the dashboard
    # registry gives it, so the metadata block reads just the host key.
    host_key = _host_key(cfg)
    location = rovimen_config.station_location(cfg)
    lat, lon = location if location is not None else (None, None)
    night = _dome_capture_night()
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    any_ok = False
    for overlay in (False, True):
        try:
            dome = render_station_dome(
                plates_with_images,
                station=host_key,
                label="",
                lat=lat,
                lon=lon,
                timestamp=timestamp,
                overlay=overlay,
            )
            buf = io.BytesIO()
            dome.save(buf, format="PNG", optimize=True)
        except Exception:  # noqa: BLE001
            logger.exception("dome render failed (overlay=%s)", overlay)
            continue
        filename = station_dome_png_name("stack", overlay)
        if _upload_png(cfg, primary_cam, night, filename, buf.getvalue()):
            any_ok = True
            logger.info("uploaded %s (%d cams, %d bytes)",
                        filename, len(plates_with_images), buf.tell())
        else:
            logger.warning("upload failed for %s", filename)
    return any_ok


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", type=Path, default=DEFAULT_CONFIG,
                        help=f"path to config.json (default {DEFAULT_CONFIG})")
    parser.add_argument("--daemon", action="store_true",
                        help="loop forever at the configured interval")
    parser.add_argument("--interval", type=int, default=None,
                        help="seconds between passes in --daemon mode")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        cfg = _load_config(args.config)
    except (OSError, ValueError) as exc:
        logger.error("cannot read config %s: %s", args.config, exc)
        return 2

    interval = args.interval or int(
        (cfg.get("services", {}).get("sky_dome_render", {}) or {}).get("interval_s", DEFAULT_INTERVAL_S)
    )

    if not args.daemon:
        return 0 if render_once(cfg) else 1

    logger.info("sky_dome_render daemon: interval=%ds", interval)
    while True:
        try:
            render_once(cfg)
        except Exception:  # noqa: BLE001 - a bad pass must never kill the loop
            logger.exception("render pass failed")
        time.sleep(max(30, interval))


if __name__ == "__main__":
    sys.exit(main())
