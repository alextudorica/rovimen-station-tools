"""VPS-local store for the calibration state stations push (#1248).

The pusher (``rovimen-scripts/rovimen_pusher.py`` ``push_calibration``) POSTs
each camera's platepar, mask, last-night calibration summary and the station's
hardware profile to ``/api/ingest/v1/<station>/calibration`` whenever any of
them changes, and at least every few hours. The dashboard's ``/api/platepar``,
``/api/mask_image``, ``/api/calibration_summary`` and ``/api/hardware`` routes
serve from here, so loading the overview map, Events or the Calibration page
no longer contacts the stations.

Layout under ``ROVIMEN_PUSHED_STATE_DIR`` (default ``/opt/rovimen/pushed_state``)::

    <host_key>/calibration.json   platepar, calibration_summary, hardware, masks
    <host_key>/masks/<CAM>.bmp    raw mask bytes, as RMS reads them

Plain files written with an atomic replace: the documents are small, change a
few times a day, and every gunicorn worker reads the same bytes without a
shared cache or a SQLite write lock.

A section the station did not send (``None``) keeps the stored one, so a
station_api hiccup on one read does not erase what the dashboard knows. A
section it did send replaces the stored one whole.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any

from atomic_write import write_atomic

logger = logging.getLogger(__name__)

_HOST_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CAM_RE = re.compile(r"^[A-Z0-9]{1,16}$")

SECTIONS = ("platepar", "calibration_summary", "hardware")


def root() -> Path:
    """Read per call so tests and the E2E fixture can repoint it."""
    return Path(os.environ.get("ROVIMEN_PUSHED_STATE_DIR", "/opt/rovimen/pushed_state"))


def _host_dir(host_key: str) -> Path | None:
    if not _HOST_RE.match(host_key or ""):
        return None
    return root() / host_key


def load(host_key: str) -> dict[str, Any] | None:
    """The stored document for ``host_key``, or None if it never pushed one."""
    d = _host_dir(host_key)
    if d is None:
        return None
    try:
        doc = json.loads((d / "calibration.json").read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("pushed_calibration: unreadable doc for %s: %s", host_key, exc)
        return None
    return doc if isinstance(doc, dict) else None


def save(
    host_key: str,
    *,
    platepar: dict[str, Any] | None = None,
    calibration_summary: dict[str, Any] | None = None,
    hardware: dict[str, Any] | None = None,
    masks: dict[str, bytes] | None = None,
    sent_at: str | None = None,
) -> dict[str, Any]:
    """Merge one push into the stored document and return it.

    ``masks`` maps camera code to raw mask bytes and is the complete set when
    given: a stored mask for a camera not in it is removed, because the
    station has said it has none.
    """
    d = _host_dir(host_key)
    if d is None:
        raise ValueError(f"invalid host_key {host_key!r}")
    doc = load(host_key) or {}
    for name, value in (
        ("platepar", platepar),
        ("calibration_summary", calibration_summary),
        ("hardware", hardware),
    ):
        if value is not None:
            doc[name] = value
    if masks is not None:
        mask_dir = d / "masks"
        kept: list[str] = []
        for cam, data in sorted(masks.items()):
            if not _CAM_RE.match(cam) or not data:
                continue
            target = mask_dir / f"{cam}.bmp"
            try:
                unchanged = target.read_bytes() == data
            except OSError:
                unchanged = False
            if not unchanged:
                write_atomic(target, data, mkdir=True)
            kept.append(cam)
        if mask_dir.is_dir():
            for stale in mask_dir.glob("*.bmp"):
                if stale.stem not in kept:
                    stale.unlink(missing_ok=True)
        doc["masks"] = kept
    doc["received_at"] = time.time()
    if sent_at:
        doc["sent_at"] = sent_at
    write_atomic(d / "calibration.json", json.dumps(doc, default=str).encode(), mkdir=True)
    return doc


def get_platepar(host_key: str) -> dict[str, Any] | None:
    doc = load(host_key)
    pp = (doc or {}).get("platepar")
    return pp if isinstance(pp, dict) else None


def get_hardware(host_key: str) -> dict[str, Any] | None:
    doc = load(host_key)
    hw = (doc or {}).get("hardware")
    return hw if isinstance(hw, dict) else None


def get_calibration_summary(host_key: str, cam: str) -> dict[str, Any] | None:
    doc = load(host_key)
    summaries = (doc or {}).get("calibration_summary")
    if not isinstance(summaries, dict):
        return None
    entry = summaries.get(cam)
    return entry if isinstance(entry, dict) else None


def mask_state(host_key: str, cam: str) -> tuple[str, Path | None]:
    """``("mask", path)`` when a pushed mask exists, ``("none", None)`` when
    the station's last push listed no mask for ``cam``, and
    ``("unknown", None)`` when it never pushed masks."""
    d = _host_dir(host_key)
    if d is None or not _CAM_RE.match(cam or ""):
        return "unknown", None
    path = d / "masks" / f"{cam}.bmp"
    if path.is_file():
        return "mask", path
    doc = load(host_key)
    if doc is not None and isinstance(doc.get("masks"), list):
        return "none", None
    return "unknown", None


def drop_mask(host_key: str, cam: str) -> None:
    """Forget a pushed mask after an operator replaced it on the station, so
    the next read goes to the station (the write path) until the pusher
    delivers the new one. The doc's mask list loses ``cam`` too, so the
    route reports "unknown" rather than "this camera has no mask"."""
    d = _host_dir(host_key)
    if d is None or not _CAM_RE.match(cam or ""):
        return
    (d / "masks" / f"{cam}.bmp").unlink(missing_ok=True)
    doc = load(host_key)
    if doc is not None and isinstance(doc.get("masks"), list):
        doc.pop("masks", None)
        write_atomic(d / "calibration.json", json.dumps(doc, default=str).encode(), mkdir=True)


# ── Live frames (#1249) ─────────────────────────────────────────────────
# The newest colour chunk stack per camera (or RMS's captured stack when the
# camera has no colour stacker), downscaled on the station and pushed every
# few minutes. The sky dome reprojects these; nothing else pulls a frame.

_FRAME_TYPES = {"image/webp": "webp", "image/jpeg": "jpg", "image/png": "png"}


def save_live_frame(
    host_key: str, cam: str, data: bytes, content_type: str, frame_time: str | None,
) -> None:
    d = _host_dir(host_key)
    ext = _FRAME_TYPES.get(content_type)
    if d is None or ext is None or not _CAM_RE.match(cam or ""):
        raise ValueError("invalid live frame target")
    frame_dir = d / "live_frame"
    write_atomic(frame_dir / f"{cam}.{ext}", data, mkdir=True)
    for other in _FRAME_TYPES.values():
        if other != ext:
            (frame_dir / f"{cam}.{other}").unlink(missing_ok=True)
    meta = {"content_type": content_type, "frame_time": frame_time,
            "received_at": time.time()}
    write_atomic(frame_dir / f"{cam}.json", json.dumps(meta).encode(), mkdir=True)


def get_live_frame(host_key: str, cam: str, max_age_s: float | None = None) -> dict[str, Any] | None:
    """``{"data", "content_type", "frame_time", "received_at"}`` for the newest
    pushed frame, or None if there is none or it arrived over ``max_age_s`` ago."""
    d = _host_dir(host_key)
    if d is None or not _CAM_RE.match(cam or ""):
        return None
    frame_dir = d / "live_frame"
    try:
        meta = json.loads((frame_dir / f"{cam}.json").read_text())
        ext = _FRAME_TYPES[meta["content_type"]]
        data = (frame_dir / f"{cam}.{ext}").read_bytes()
    except (OSError, ValueError, KeyError, TypeError):
        return None
    received = float(meta.get("received_at") or 0)
    if max_age_s is not None and time.time() - received > max_age_s:
        return None
    return {"data": data, "content_type": meta["content_type"],
            "frame_time": meta.get("frame_time"), "received_at": received}
