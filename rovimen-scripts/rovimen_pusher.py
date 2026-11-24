#!/usr/bin/env python3
"""rovimen_pusher.py — station-side push agent (reversed-HTTP data plane).

Runs on each ROVIMEN station and POSTs the station's own state to the VPS ingest
API (``/api/ingest/v1/<host_key>/*``). This is the station-side publisher half of
the design in ``docs/reversed_http_push_design.md`` (§2 ingest, §2.3 idempotency
envelope, §2.4 payload schemas).

The command worker that used to live here (reversed-HTTP push §3) was removed
2026-07: the server->station command channel is gone entirely, so a compromised
VPS cannot mutate stations. Interventions are SSH-only; fleet-wide changes ship
as signed bundles (``docs/signed_bundles.md``). The pusher is now telemetry-only.

Design properties honoured:

  * **Purely additive.** Reads the station's *existing* local data sources — the
    same ones the VPS poller reads — through the same code: listings, camera
    discovery and the detection index in-process via ``station_collect`` (the
    collectors behind ``station_api.py``), and ``/api/status``, ``/api/vitals``
    and the live thumbnail from ``station_api.py`` on ``127.0.0.1:7779`` (see
    "Local data sources" below). It does not touch RMS, capture or upload; it
    only reads. If the pusher dies the station is unaffected.
  * **Outbound-only.** Pure outbound HTTPS POSTs to the VPS edge. No inbound port.
  * **Opt-in per station.** Starts only if the ingest key file exists (see
    ``ingest.key`` under ``~/.config/rovimen/``). Absent key -> clean exit 0, so
    the systemd unit can be enabled fleet-wide while staying dormant until a
    station is enrolled in the pilot.
  * **Idempotent envelope.** Every body carries a monotonic per-station ``seq``
    (persisted across restarts, seeded from the clock) and ``sent_at`` (§2.3). Detections also dedup on
    their natural PK server-side.
  * **Robust.** Retries with backoff+jitter, honours ``Retry-After`` on 429,
    buffers heartbeat/vitals/media on a bounded on-disk spool when ingest is
    down, and never raises out of the main loop.

Config (mirrors how other station scripts read config, no hardcoded paths):

  * ``config.json`` (``~/rovimen_scripts/config.json``) — read for ``host_key``
    and, as a fallback, ``vps_host``. ``host_key`` may also come from the
    ``ROVIMEN_HOST_KEY`` env var or the key filename.
  * Ingest base URL: ``ROVIMEN_INGEST_BASE_URL`` env var, else config
    ``push.ingest_base_url``, else derived from ``vps_host``.
  * Ingest key: ``ROVIMEN_INGEST_KEY`` env var, else the first line of
    ``~/.config/rovimen/ingest.key`` (mode 0600). If neither is present the agent
    exits 0 (opt-in gate).

Run: ``python rovimen_pusher.py`` (long-running daemon, systemd ``Restart=always``).
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import logging
import os
import random
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

# 4xx-that-retrying-cannot-fix is decided once for both ingest agents.
from ingest_blob import is_permanent_status  # noqa: F401  (re-exported)

logger = logging.getLogger("rovimen_pusher")

AGENT_VERSION = "1.0.0"

BASE = Path.home()
SCRIPTS_DIR = BASE / "rovimen_scripts"
CONFIG_PATH = SCRIPTS_DIR / "config.json"

# Where the local station_api serves the same data the VPS poller reads.
STATION_API_BASE = "http://127.0.0.1:7779"

# Opt-in gate + durable state live under the operator's XDG config dir.
CONFIG_DIR = BASE / ".config" / "rovimen"
KEY_PATH = CONFIG_DIR / "ingest.key"
ENV_PATH = CONFIG_DIR / "ingest.env"
STATE_PATH = CONFIG_DIR / "pusher_state.json"
SPOOL_PATH = CONFIG_DIR / "pusher_spool.db"
# Station event history (see "Station events" below).
EVENTS_DB_PATH = CONFIG_DIR / "pusher_events.db"
EVENTS_STATE_PATH = CONFIG_DIR / "pusher_events_state.json"
ALIVE_PATH = CONFIG_DIR / "pusher_alive.json"
CLEAN_STOP_PATH = CONFIG_DIR / "pusher_clean_stop.json"
# Written by rovimen_watchdog.sh: the uplink class in its state file and one
# line per change of class (and rescue Wi-Fi up/down) in the events log.
WATCHDOG_STATE_PATH = SCRIPTS_DIR / "logs" / ".watchdog_state"
UPLINK_EVENTS_LOG = SCRIPTS_DIR / "logs" / "uplink_events.log"

# Cadences (seconds) — §2.1.
HEARTBEAT_INTERVAL = 45          # 30-60 s window
VITALS_INTERVAL = 30
DETECTIONS_INTERVAL = 300        # matches index_poller 5-min cadence
MEDIA_INTERVAL = 900             # dawn-process pointers change slowly
LIVE_THUMB_INTERVAL = 30         # §9 — newest FF maxpixel, 30-60 s window (fresher live preview)
CALIBRATION_INTERVAL = 900       # #1248 — platepar/mask/summary/hardware, POSTed only on change
CALIBRATION_REFRESH = 6 * 3600   # ...or when this long has passed since the last accepted POST
LIVE_FRAME_INTERVAL = 300        # #1249 — newest colour chunk stack per cam (sky-dome input)
LIVE_FRAME_WIDTH = 960           # px; the dome reprojects onto the platepar resolution anyway

# Detection hot window — matches index_poller's 14-day rolling overlap (§2.4.3).
DETECTION_WINDOW_DAYS = 14

# Rows per detections POST. The hot window is 14 rolling days of EVERY camera on
# the station, so a five-camera site in a shower month holds many thousands of
# rows — far more than belongs in one body, and more than the VPS accepts. The
# window is therefore shipped in batches, each acknowledged on its own. Kept an
# order of magnitude under the server-side per-POST cap so the busiest station in
# the fleet never rides the ceiling, and small enough that one rejected batch
# costs a fraction of the window rather than all of it.
DETECTION_BATCH_ROWS = 1000

# Locked-clip pointer window — how many recent night dirs per camera to scan for
# locked chunks. Locked clips are sparse (a handful per night) and only the
# recent ones matter to the live-feed / footage views, so a short window keeps
# the per-cycle station_api load bounded regardless of archive depth.
CLIP_WINDOW_NIGHTS = 3

# Networking.
HTTP_TIMEOUT = 20
MAX_BACKOFF = 300.0
BASE_BACKOFF = 2.0

# Spool bounds (§7 — bounded local buffer).
SPOOL_MAX_ROWS = 5000

# Station events: queued until the VPS acknowledges them, newest kept.
EVENTS_INTERVAL = 30
EVENTS_MAX_QUEUED = 500
EVENTS_BATCH = 100
EVENT_DETAIL_MAX_CHARS = 500
UPLINK_LOG_MAX_READ = 256 * 1024
# Alive stamp cadence, and how long a boot event waits for NTP before it is
# sent without a downtime (a station that never syncs still reports its boot).
ALIVE_EVERY = 60
BOOT_CLOCK_WAIT_S = 3600
CLOCK_CHECK_EVERY = 60


# ──────────────────────────────────────────────────────────────────────────
# Config / identity
# ──────────────────────────────────────────────────────────────────────────


def _load_config() -> dict:
    """Read config.json if present; tolerate a missing/broken file."""
    try:
        return json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        return {}
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not parse %s: %s", CONFIG_PATH, exc)
        return {}


def _ingest_env(env_path: Path | None = None) -> dict[str, str]:
    """Parse the enrollment env file the systemd units load via EnvironmentFile.

    Identity must resolve identically regardless of how the process was
    started: the pusher/archive-upload units inherit these vars from systemd,
    but cron-launched contexts (dawn_process and its in-run uploads) do not —
    without this fallback they resolve no host_key and silently upload
    nothing. Returns {} if the file is missing or unreadable.
    """
    if env_path is None:
        env_path = ENV_PATH  # module attr at call time — patchable in tests
    out: dict[str, str] = {}
    try:
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v:
                out[k] = v
    except FileNotFoundError:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not read %s: %s", env_path, exc)
    return out


def resolve_host_key(cfg: dict, key_path: Path = KEY_PATH) -> str | None:
    """Determine this station's host_key.

    Priority: ROVIMEN_HOST_KEY env -> ingest.env file -> config['host_key']
    -> config['push']['host_key'].
    Returns None if it cannot be determined.
    """
    env = os.environ.get("ROVIMEN_HOST_KEY") or _ingest_env().get("ROVIMEN_HOST_KEY")
    if env:
        return env.strip()
    hk = cfg.get("host_key")
    if isinstance(hk, str) and hk.strip():
        return hk.strip()
    push = cfg.get("push")
    if isinstance(push, dict):
        hk = push.get("host_key")
        if isinstance(hk, str) and hk.strip():
            return hk.strip()
    return None


def resolve_ingest_key(key_path: Path = KEY_PATH) -> str | None:
    """Read the per-station ingest key (opt-in gate).

    ROVIMEN_INGEST_KEY env wins (useful for tests / containers); otherwise the
    first non-empty line of the key file. Returns None if neither is present.
    """
    env = os.environ.get("ROVIMEN_INGEST_KEY")
    if env and env.strip():
        return env.strip()
    try:
        for line in key_path.read_text().splitlines():
            line = line.strip()
            if line:
                return line
    except FileNotFoundError:
        return None
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not read ingest key %s: %s", key_path, exc)
        return None
    return None


def resolve_interval(cfg: dict, name: str, default: int, lo: int = 5, hi: int = 86400) -> int:
    """Resolve a push cadence (seconds) so cadences are config-driven, not baked
    into the code — the fleet's collection rhythm is set uniformly from config.

    Priority: ``ROVIMEN_<NAME>_INTERVAL`` env -> ``config['push']['<name>_interval']``
    -> ``default``. Clamped to ``[lo, hi]``; an invalid value falls back to default."""
    raw: object = None
    env = os.environ.get(f"ROVIMEN_{name.upper()}_INTERVAL")
    if env and env.strip():
        raw = env.strip()
    else:
        push = cfg.get("push")
        if isinstance(push, dict):
            raw = push.get(f"{name}_interval")
    if raw is None:
        return default
    try:
        return max(lo, min(int(raw), hi))
    except (TypeError, ValueError):
        logger.warning("invalid %s_interval %r — using default %d", name, raw, default)
        return default


def resolve_live_thumb_interval(cfg: dict) -> int:
    """Live-thumbnail push cadence in seconds (§9). Default 30 (fresh live
    preview), clamped 10-300."""
    return resolve_interval(cfg, "live_thumb", LIVE_THUMB_INTERVAL, lo=10, hi=300)


def resolve_intervals(cfg: dict) -> dict[str, int]:
    """All push cadences, config-driven (see :func:`resolve_interval`)."""
    return {
        "heartbeat": resolve_interval(cfg, "heartbeat", HEARTBEAT_INTERVAL, lo=10, hi=3600),
        "vitals": resolve_interval(cfg, "vitals", VITALS_INTERVAL, lo=10, hi=3600),
        "detections": resolve_interval(cfg, "detections", DETECTIONS_INTERVAL, lo=30, hi=86400),
        "media": resolve_interval(cfg, "media", MEDIA_INTERVAL, lo=30, hi=86400),
        "calibration": resolve_interval(cfg, "calibration", CALIBRATION_INTERVAL, lo=60, hi=86400),
        "live_frame": resolve_interval(cfg, "live_frame", LIVE_FRAME_INTERVAL, lo=60, hi=3600),
        "live_thumb": resolve_live_thumb_interval(cfg),
        "chunks": resolve_interval(cfg, "chunks", CHUNKS_INTERVAL, lo=60, hi=86400),
        "events": resolve_interval(cfg, "events", EVENTS_INTERVAL, lo=10, hi=3600),
    }


def resolve_base_url(cfg: dict) -> str | None:
    """Determine the ingest base URL.

    Priority: ROVIMEN_INGEST_BASE_URL env -> ingest.env file
    -> config['push']['ingest_base_url'] -> derived https://<vps_host> from
    config. Returns None if undeterminable.
    """
    env = (os.environ.get("ROVIMEN_INGEST_BASE_URL")
           or _ingest_env().get("ROVIMEN_INGEST_BASE_URL"))
    if env and env.strip():
        return env.strip().rstrip("/")
    push = cfg.get("push")
    if isinstance(push, dict):
        url = push.get("ingest_base_url")
        if isinstance(url, str) and url.strip():
            return url.strip().rstrip("/")
    vps = cfg.get("vps_host")
    if isinstance(vps, str) and vps.strip():
        return f"https://{vps.strip()}".rstrip("/")
    return None


def resolve_fallback_url(cfg: dict) -> str | None:
    """The second route to the ingest API, tried after a transport failure.

    Priority: ROVIMEN_INGEST_FALLBACK_URL env -> ingest.env file
    -> config['push']['ingest_fallback_url']. The fleet default (config_defaults)
    is the VPS on the tailnet, which has no Cloudflare 100 s origin limit (see
    ingest_blob.CLOUDFLARE_ORIGIN_TIMEOUT). None when unset or equal to the
    primary.
    """
    url = (os.environ.get("ROVIMEN_INGEST_FALLBACK_URL")
           or _ingest_env().get("ROVIMEN_INGEST_FALLBACK_URL"))
    if not (url and url.strip()):
        push = cfg.get("push")
        url = push.get("ingest_fallback_url") if isinstance(push, dict) else None
    if not (isinstance(url, str) and url.strip()):
        return None
    url = url.strip().rstrip("/")
    return None if url == resolve_base_url(cfg) else url


# ──────────────────────────────────────────────────────────────────────────
# seq persistence (§2.3)
# ──────────────────────────────────────────────────────────────────────────


class SeqCounter:
    """Monotonic per-station sequence number persisted to a small state file.

    ``next()`` returns a strictly increasing int and durably records it so seq
    keeps climbing across restarts. The counter starts at the larger of the
    saved value and the clock in milliseconds (``fireball_watch`` stamps its
    bodies the same way): the VPS 200-and-drops every seq at or below the
    highest it has stored for the host key, so a reinstall, SD swap or new box
    that lost ``pusher_state.json`` and restarted at 1 looked offline for
    months. Pushes are far rarer than one per millisecond, so a restart always
    lands above the last seq sent; a clock behind the saved value (a Pi before
    NTP sync) keeps the saved value.
    """

    def __init__(self, state_path: Path = STATE_PATH,
                 clock: Callable[[], float] = time.time) -> None:
        self._state_path = state_path
        self._lock = threading.Lock()
        self._seq = max(self._read(), int(clock() * 1000))

    def _read(self) -> int:
        try:
            data = json.loads(self._state_path.read_text())
            val = int(data.get("last_seq", 0))
            return max(val, 0)
        except Exception:
            return 0

    @property
    def current(self) -> int:
        return self._seq

    def next(self) -> int:
        with self._lock:
            self._seq += 1
            self._persist(self._seq)
            return self._seq

    def _persist(self, seq: int) -> None:
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"last_seq": seq}))
            tmp.replace(self._state_path)
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("could not persist seq state: %s", exc)


# ──────────────────────────────────────────────────────────────────────────
# Envelope + payload construction (§2.4)
# ──────────────────────────────────────────────────────────────────────────


def _now_rfc3339() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_envelope(seq: int, payload: dict, sent_at: str | None = None) -> dict:
    """Wrap a payload in the common ingest envelope (§2.4)."""
    return {
        "seq": seq,
        "sent_at": sent_at or _now_rfc3339(),
        "agent_version": AGENT_VERSION,
        "payload": payload,
    }


def build_heartbeat_payload(status: dict, uplink: dict | None = None,
                            boot: dict | None = None) -> dict:
    """Project /api/status into the heartbeat payload (§2.4.1).

    Only the fields the dashboard reads are forwarded, so the pushed body
    matches what the poller feeds ``StationCache.set_status`` today.
    ``uplink`` (:func:`read_uplink_state`) and ``boot``
    (:meth:`BootTracker.boot_block`) are the pusher's own additions.

    ``last_updated`` is when the station computed the status. /api/status is
    served stale-while-revalidate, so a status whose refresh keeps failing is
    still sent; without its own stamp the VPS stamped it with the send time
    and it read as fresh. A status without one leaves the key out, and the
    VPS falls back to the send time as before.
    """
    payload = {
        "online": bool(status.get("online", True)),
        "services": status.get("services", {}),
        "cameras": status.get("cameras", []),
        "disk": status.get("disk", {}),
        "storage": status.get("storage", {}),
        "extra_disks": status.get("extra_disks", []),
        "rms_running": status.get("rms_running"),
        "color_capture_running": status.get("color_capture_running"),
        "network_name": status.get("network_name"),
        # Signed-bundle telemetry (docs/signed_bundles.md) — lets the
        # dashboard watchdog alert on failed verifies / stale builds / wrong
        # pin without any inbound call to the station.
        "bundle": status.get("bundle"),
        # Per-camera colour-segment cadence. Push-model stations are exactly
        # the ones the dashboard cannot poll, so a clip storm there would stay
        # invisible unless it rides the heartbeat.
        "segment_health": status.get("segment_health", {}),
        # Per-camera RMS capture evidence (newest FF, half-hour yield, FF
        # files per night): what "N of M cameras recording" is counted from.
        "capture": status.get("capture", {}),
        # The janitor's pressure episode (Storage-card red banner) and the
        # colour-capture fallback root. A poll station's /api/status carries
        # them; a push station only reaches the dashboard through this body.
        "disk_pressure": status.get("disk_pressure"),
        "capture_fallback": status.get("capture_fallback"),
        "uplink": uplink,
        "boot": boot,
    }
    if status.get("last_updated"):
        payload["last_updated"] = status["last_updated"]
    return payload


def build_vitals_payload(vitals: dict) -> dict:
    """Project /api/vitals into the vitals payload (§2.4.2)."""
    return {
        "cpu_pct": vitals.get("cpu_pct"),
        "ram_pct": vitals.get("ram_pct"),
        "ram_used_mb": vitals.get("ram_used_mb"),
        "ram_total_mb": vitals.get("ram_total_mb"),
        "temp_c": vitals.get("temp_c"),
        "total_cores": vitals.get("total_cores"),
        "cores": vitals.get("cores", []),
        "disk_write_mbps": vitals.get("disk_write_mbps"),
        "top_procs": vitals.get("top_procs", []),
    }


def build_detections_payload(
    window_since: str,
    detections: list[dict],
    night_sky: list[dict] | None = None,
) -> dict:
    """Wrap detection-index rows into the detections payload (§2.4.3).

    ``detections`` come straight from the local index (columns already match
    ``detection_db.DETECTION_COLS`` minus the VPS-only ``source``), so ingest is a
    pass-through to ``upsert_detections``.

    ``night_sky`` (optional) carries the per-night sky-quality rows on the same
    envelope so the VPS learns weather state without a second push. Only the
    first batch of a cycle carries it (the rows are per-night, not per-batch);
    later batches omit it. Absent → the VPS just doesn't update sky state.
    """
    payload: dict = {"window_since": window_since, "detections": detections}
    if night_sky:
        payload["night_sky"] = night_sky
    return payload


def _detection_order_key(row: dict) -> tuple[str, str, int]:
    """Sort key putting the oldest night first, stable within a night."""
    try:
        meteor_no = int(row.get("meteor_no") or 0)
    except (TypeError, ValueError):
        meteor_no = 0
    return (str(row.get("date") or ""), str(row.get("ff_file") or ""), meteor_no)


def chunk_detections(rows: list[dict],
                     size: int | None = None) -> list[list[dict]]:
    """Split detection rows into oldest-first batches of at most ``size`` rows.

    Ordering matters because batches are acknowledged independently: if a run
    stops partway, what reached the VPS is a contiguous prefix of the window
    (the oldest nights), never a hole in the middle.
    """
    if size is None:
        size = DETECTION_BATCH_ROWS   # module attr at call time
    if size < 1:
        size = 1
    ordered = sorted(rows, key=_detection_order_key)
    return [ordered[i:i + size] for i in range(0, len(ordered), size)]


def build_clip_pointers(chunks_by_cam_date: dict[tuple[str, str], list[dict]]) -> list[dict]:
    """Flatten locked chunks into clip pointers (§2.4.5).

    ``chunks_by_cam_date`` maps ``(cam, date)`` to the ``/api/chunks`` chunk
    dicts (already filtered to locked chunks). Each chunk carries ``filename``,
    ``locked``, ``lock_type`` and ``meteor_time``; we emit only the fields the
    ingest ``ClipPointer`` schema keys on plus ``lock_type`` (preserved verbatim
    in the pointer's ``extra`` blob server-side). Idempotent by construction: the
    pointers dedup on ``(cam, date, kind, filename)`` in ``media_pointers``, so
    re-scanning the same night is a no-op on identity.
    """
    clips: list[dict] = []
    for (cam, date), chunks in chunks_by_cam_date.items():
        for c in chunks or []:
            filename = c.get("filename")
            if not filename or not c.get("locked"):
                continue
            clips.append({
                "cam": cam,
                "date": date,
                "filename": filename,
                "locked": True,
                "meteor_time": c.get("meteor_time"),
                "lock_type": c.get("lock_type"),
            })
    return clips


def build_media_payload(
    timelapses_raw: dict,
    chunks_by_cam_date: dict[tuple[str, str], list[dict]] | None = None,
) -> dict:
    """Project /api/timelapses (+ locked chunks) into the media payload (§2.4.5).

    ``/api/timelapses`` returns ``{cam: [{date, filename, night_stack}, ...]}``.
    Flatten into the ``timelapses`` / ``nightstacks`` pointer lists. Locked-clip
    pointers come from ``chunks_by_cam_date`` (recent locked chunks per camera,
    fetched from ``/api/chunks/<cam>/<date>?locked_only=1``). The media *bytes*
    still reach the VPS via the independent SFTP path (§2.4.5) — these are only
    pointers telling the dashboard which locked clips exist, replacing the
    request-time :7779 fan-out.
    """
    timelapses: list[dict] = []
    nightstacks: list[dict] = []
    for cam, entries in (timelapses_raw or {}).items():
        for e in entries or []:
            date = e.get("date")
            filename = e.get("filename")
            night_stack = e.get("night_stack")
            if filename:
                timelapses.append({
                    "cam": cam,
                    "date": date,
                    "filename": filename,
                    "night_stack": night_stack,
                })
            if night_stack:
                nightstacks.append({
                    "cam": cam,
                    "date": date,
                    "filename": night_stack,
                })
    clips = build_clip_pointers(chunks_by_cam_date or {})
    return {"timelapses": timelapses, "nightstacks": nightstacks, "clips": clips}


def _gzip_stable(raw: bytes) -> bytes:
    """Gzip with a zero mtime, so the bytes, and so the change hash, are stable.

    ``gzip.compress(raw, mtime=0)`` needs Python 3.8; GzipFile takes ``mtime`` on
    3.7 too, which gmnro17/gmnro19 (Câmpenița, Raspberry Pi OS buster) still run.
    """
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(raw)
    return buf.getvalue()


def build_calibration_payload(
    platepar: dict | None,
    summaries: dict | None,
    hardware: dict | None,
    masks: dict | None,
) -> dict:
    """#1248 body. ``masks`` maps cam -> raw mask bytes and is the complete set;
    pass None when any mask read failed so the VPS keeps what it has. Every
    other section is likewise None when its read failed."""
    payload: dict = {
        "platepar": platepar,
        "calibration_summary": summaries,
        "hardware": hardware,
        "masks": None,
    }
    if masks is not None:
        payload["masks"] = {
            cam: {
                "encoding": "gzip+base64",
                "data_b64": base64.b64encode(_gzip_stable(raw)).decode("ascii"),
            }
            for cam, raw in sorted(masks.items())
        }
    return payload


def calibration_digest(payload: dict) -> str:
    """Change detector for the calibration push: a summary's ``date`` rolls at
    midnight and the hardware profile carries no clock, so equal digests mean
    nothing the dashboard shows has changed."""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def build_live_thumb_payload(webp: bytes, ff_timestamp: str | None) -> dict:
    """Wrap a WebP maxpixel into the live-thumbnail payload (§9).

    The image is tiny (few KB, newest FF only, no archival value) so the bytes
    ride the JSON envelope base64-encoded rather than taking the bytes→box /
    pointer→VPS split used for archival media (§9). ``content_type`` and
    ``ff_timestamp`` let the VPS serve it back verbatim to the existing consumer.
    """
    return {
        "content_type": "image/webp",
        "ff_timestamp": ff_timestamp,
        "data_b64": base64.b64encode(webp).decode("ascii"),
    }


# ── Chunk listings (#1250) ────────────────────────────────────────────────
# Each camera's newest nights, as a compact listing, so the dashboard serves
# tonight's videodb grid, all-footage grid and night pickers from VPS-local
# state instead of calling /api/chunks on this station per page load. Format:
# dashboard/chunk_listings.py. A camera-night is re-sent only when its listing
# changed since the last accepted push.
CHUNKS_INTERVAL = 600
CHUNK_LISTING_NIGHTS = 2


def build_chunk_listing_payload(cam: str, date: str, listing) -> dict | None:
    """Compact one ``station_collect.list_chunks`` result (see module note).

    Returns None when the night directory does not exist (``[]``)."""
    if isinstance(listing, dict):
        chunks = listing.get("chunks") or []
        morning_done = bool(listing.get("morning_done", False))
    elif isinstance(listing, list) and listing:
        chunks, morning_done = listing, False
    else:
        return None
    prefix = f"{cam}_"
    suffix = "_color.mkv"
    entries: list = []
    locks: dict = {}
    reencoded: list = []
    for c in chunks:
        if not isinstance(c, dict):
            continue
        name = c.get("filename") or ""
        if not (name.startswith(prefix) and name.endswith(suffix)):
            continue
        stamp = name[len(prefix):-len(suffix)]
        if len(stamp) != 15 or stamp[8] != "_":
            continue
        entry = [stamp, c.get("size_mb")]
        name_time = f"{stamp[9:11]}:{stamp[11:13]}:{stamp[13:15]}"
        if c.get("time") and c.get("time") != name_time:
            entry.append(c.get("time"))
        entries.append(entry)
        if c.get("locked") and c.get("lock_type"):
            locks[stamp] = [c.get("lock_type"), c.get("meteor_time"),
                            c.get("detection_offset_s")]
        if c.get("reencoded"):
            reencoded.append(stamp)
    return {
        "cam": cam,
        "date": date,
        "morning_done": morning_done,
        "chunks": entries,
        "locks": locks,
        "reencoded": reencoded,
    }


def fetch_chunk_listing(cam: str, date: str):
    """The full (not locked_only) /api/chunks payload for one camera-night."""
    data = _local("list_chunks", cam, date)
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/chunks/{cam}/{date}")
    return data


def push_chunk_listings(publisher: "Publisher") -> bool:
    """POST each camera's newest ``CHUNK_LISTING_NIGHTS`` nights whose listing
    changed since the last accepted push. Not spooled: a later cycle sends a
    fresher listing anyway."""
    import hashlib

    sent = getattr(publisher, "_chunk_listing_sigs", None)
    if sent is None:
        sent = {}
        publisher._chunk_listing_sigs = sent
    ok_all = True
    seen = set()
    for cam in fetch_camera_codes():
        for date in fetch_nights(cam, limit=CHUNK_LISTING_NIGHTS):
            seen.add((cam, date))
            payload = build_chunk_listing_payload(cam, date, fetch_chunk_listing(cam, date))
            if payload is None:
                continue
            sig = hashlib.sha1(
                json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
            if sent.get((cam, date)) == sig:
                continue
            body = build_envelope(publisher._seq.next(), payload)
            result = post_json(publisher._url("chunks"), publisher._key, body)
            if result.ok:
                sent[(cam, date)] = sig
                continue
            ok_all = False
            if result.status == 404:
                # VPS without the chunks endpoint yet: stop for this cycle.
                return False
            if result.retry_after:
                publisher._stop.wait(min(result.retry_after, MAX_BACKOFF))
    for key in [k for k in sent if k not in seen]:
        del sent[key]
    return ok_all


def _window_since(days: int = DETECTION_WINDOW_DAYS) -> str:
    from datetime import timedelta
    d = datetime.now(timezone.utc) - timedelta(days=days)
    return d.strftime("%Y%m%d")


# ──────────────────────────────────────────────────────────────────────────
# Local data sources (read-only)
# ──────────────────────────────────────────────────────────────────────────
#
# The listings, camera discovery and the detection index are read in-process
# through station_collect, the same functions the station_api routes run, so
# they no longer depend on station_api being up and cost no HTTP round trip.
# Status, vitals and the live thumbnail stay on :7779: vitals needs psutil
# and the thumbnail the RMS venv's numpy/Pillow, neither of which the system
# python3 this service runs on has, and /api/status shares its slow du walks
# through station_api's stale-while-revalidate cache. When station_collect
# cannot be imported or a local read raises, the HTTP route is used instead.

try:
    import station_collect as _collect
except Exception as _collect_exc:  # e.g. restarted before the bundle that ships it
    _collect = None
    logger.warning("station_collect unavailable, local reads use :7779: %s", _collect_exc)


def _local(fn_name: str, *args, **kwargs):
    """``station_collect.<fn_name>(...)``, or None if unavailable or it raised."""
    if _collect is None:
        return None
    try:
        return getattr(_collect, fn_name)(*args, **kwargs)
    except Exception as exc:
        logger.warning("local %s failed, using :7779: %s", fn_name, exc)
        return None


def _get_json(url: str, timeout: int = HTTP_TIMEOUT) -> dict | None:
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (localhost)
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        logger.warning("local GET %s failed: %s", url, exc)
        return None


def fetch_status() -> dict | None:
    return _get_json(f"{STATION_API_BASE}/api/status")


def fetch_vitals() -> dict | None:
    return _get_json(f"{STATION_API_BASE}/api/vitals")


def fetch_timelapses() -> dict | None:
    data = _local("list_timelapses")
    if data is not None:
        return data
    return _get_json(f"{STATION_API_BASE}/api/timelapses")


# One detection-cycle read serves both fetch_detections and fetch_night_sky.
_INDEX_MEMO_TTL = 60.0
_index_memo: dict = {}


def _detections_index(since: str) -> dict | None:
    """The /api/detections-index payload for ``since`` (memoised briefly)."""
    memo = _index_memo.get(since)
    if memo is not None and time.monotonic() - memo[0] < _INDEX_MEMO_TTL:
        return memo[1]
    data = _local("read_detections_index", since)
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/detections-index?since={since}")
    _index_memo.clear()
    _index_memo[since] = (time.monotonic(), data)
    return data


def fetch_detections(since: str) -> list[dict]:
    data = _detections_index(since)
    if not data or not data.get("ready"):
        return []
    return data.get("detections", [])


def fetch_night_sky(since: str) -> list[dict]:
    """Per-night sky-quality rows from the local index (empty on older DB)."""
    data = _detections_index(since)
    if not data or not data.get("ready"):
        return []
    return data.get("night_sky", []) or []


def fetch_camera_codes() -> list[str]:
    """Return the station's camera codes (RMS .configs, see
    station_collect._rms_config_paths), used to drive per-cam clip and
    live-thumbnail pushes."""
    data = _local("list_rms_cameras")
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/rms_cameras")
    if not isinstance(data, list):
        return []
    codes: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            code = entry.get("code")
            if isinstance(code, str) and code.strip():
                codes.append(code.strip())
    return codes


def fetch_live_camera_codes() -> list[str]:
    """Cameras to push a live tile for: RMS cameras plus colour cameras
    without RMS, e.g. GH662W (station_collect.list_live_cameras). Over HTTP it
    falls back to the RMS list when the station_api predates /api/live_cameras
    (pusher restarted first mid-update)."""
    data = _local("list_live_cameras")
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/live_cameras")
    if not isinstance(data, list):
        return fetch_camera_codes()
    codes: list[str] = []
    for entry in data:
        if isinstance(entry, dict):
            code = entry.get("code")
            if isinstance(code, str) and code.strip() and code.strip() not in codes:
                codes.append(code.strip())
    return codes


def fetch_nights(cam: str, limit: int | None = None) -> list[str]:
    """Return night-date dirs (YYYYMMDD, newest first) that hold clips for
    cam, at most ``limit`` of them."""
    data = _local("list_nights", cam, limit=limit)
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/nights/{cam}")
    if not isinstance(data, list):
        return []
    nights = [d for d in data if isinstance(d, str)]
    return nights if limit is None else nights[:limit]


def fetch_locked_chunks(cam: str, date: str) -> list[dict]:
    """Return locked chunk dicts for one cam/night (the /api/chunks?locked_only=1
    payload, station_collect.list_chunks).

    The payload is ``{"morning_done": bool, "chunks": [...]}`` (or a bare list
    on very old stations); normalise both. Only locked chunks are requested,
    but we defensively re-filter on ``locked``."""
    data = _local("list_chunks", cam, date, locked_only=True)
    if data is None:
        data = _get_json(f"{STATION_API_BASE}/api/chunks/{cam}/{date}?locked_only=1")
    if isinstance(data, dict):
        chunks = data.get("chunks", [])
    elif isinstance(data, list):
        chunks = data
    else:
        return []
    return [c for c in chunks if isinstance(c, dict) and c.get("locked")]


def fetch_locked_clips(nights: int = CLIP_WINDOW_NIGHTS) -> dict[tuple[str, str], list[dict]]:
    """Enumerate recent locked clips across all cameras.

    For each camera code, scan its most recent ``nights`` night dirs for locked
    chunks. Returns ``{(cam, date): [chunk, ...]}`` suitable for
    :func:`build_clip_pointers`."""
    out: dict[tuple[str, str], list[dict]] = {}
    for cam in fetch_camera_codes():
        for date in fetch_nights(cam, limit=nights):
            locked = fetch_locked_chunks(cam, date)
            if locked:
                out[(cam, date)] = locked
    return out


def _get_local(path: str) -> tuple[int | None, bytes]:
    """GET a station_api path on localhost: ``(status, body)``, status None on
    a connection error. Unlike :func:`_get_json` it keeps a 404 apart from a
    failure, which the calibration push needs (404 = "this camera has none")."""
    url = f"{STATION_API_BASE}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (localhost)
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:
        logger.warning("local GET %s failed: %s", url, exc)
        return None, b""


def _local_json(path: str) -> dict | None:
    status, body = _get_local(path)
    if status != 200:
        return None
    try:
        data = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def fetch_calibration_state() -> dict:
    """Read what the dashboard used to pull for the overview map, Events and
    the Calibration page, from the local station_api (#1248)."""
    platepar = _local_json("/api/platepar")
    cams = sorted(platepar) if platepar else fetch_camera_codes()

    hw_status, hw_body = _get_local("/api/hardware")
    hardware = None
    if hw_status == 200:
        try:
            hardware = json.loads(hw_body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            hardware = None

    summaries: dict | None = {}
    masks: dict | None = {}
    for cam in cams:
        summ = _local_json(f"/api/calibration_summary/{cam}")
        if summ is None:
            summaries = None
        elif summaries is not None:
            summaries[cam] = summ
        status, body = _get_local(f"/api/mask_image/{cam}")
        if status == 200 and body:
            if masks is not None:
                masks[cam] = body
        elif status != 404:
            masks = None  # unknown, not absent: keep what the VPS holds
    return {
        "platepar": platepar,
        "summaries": summaries,
        "hardware": hardware if isinstance(hardware, dict) else None,
        "masks": masks,
    }


def fetch_live_frame(cam: str) -> tuple[bytes, str, str | None] | None:
    """``(bytes, content_type, frame_time)`` of the camera's newest colour chunk
    stack (or RMS captured stack), downscaled by station_api, else None."""
    url = f"{STATION_API_BASE}/api/latest_frame/{cam}?w={LIVE_FRAME_WIDTH}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (localhost)
            data = resp.read()
            ctype = (resp.headers.get("Content-Type") or "image/webp").split(";")[0].strip()
            stamp = resp.headers.get("X-Frame-Time")
    except urllib.error.HTTPError as exc:
        logger.debug("live_frame GET %s -> HTTP %s", url, exc.code)
        return None
    except Exception as exc:
        logger.warning("live_frame GET %s failed: %s", url, exc)
        return None
    return (data, ctype, stamp) if data else None


def fetch_live_thumb(cam: str) -> tuple[bytes, str | None] | None:
    """Fetch the newest FF maxpixel WebP for ``cam`` from the local station_api.

    Returns ``(webp_bytes, ff_timestamp)`` or None if unavailable (404/503/error).
    ``ff_timestamp`` comes from the ``X-FF-Timestamp`` response header so the
    dashboard can show a freshness indicator without decoding the image.
    """
    url = f"{STATION_API_BASE}/api/latest_ff_maxpixel/{cam}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (localhost)
            if not (200 <= resp.status < 300):
                return None
            data = resp.read()
            ts = resp.headers.get("X-FF-Timestamp") if resp.headers else None
            return (data, ts) if data else None
    except urllib.error.HTTPError as exc:
        # 404 (no FF yet) / 503 (decode failed) are normal — camera may be idle.
        logger.debug("live_thumb GET %s -> HTTP %s", url, exc.code)
        return None
    except Exception as exc:
        logger.warning("live_thumb GET %s failed: %s", url, exc)
        return None


# ──────────────────────────────────────────────────────────────────────────
# On-disk spool for buffering when ingest is down (§7)
# ──────────────────────────────────────────────────────────────────────────


class Spool:
    """Bounded on-disk FIFO of pending POSTs, keyed by kind.

    Heartbeat/vitals are last-value: only the newest of each kind is retained
    (a backlog collapses to one POST, per §7). Media pointers likewise coalesce.
    Detection batches are re-derived from the local index on reconnect rather
    than spooled, so this spool never grows unbounded from detections.
    """

    COALESCE_KINDS = {"heartbeat", "vitals", "media"}

    def __init__(self, path: Path = SPOOL_PATH) -> None:
        self._path = path
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(self._path), check_same_thread=False)
        self._con.execute("PRAGMA journal_mode=WAL")
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS spool ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT, endpoint TEXT, "
            "body TEXT, created_at TEXT)"
        )
        self._con.commit()

    def add(self, kind: str, endpoint: str, body: dict) -> None:
        cur = self._con.cursor()
        if kind in self.COALESCE_KINDS:
            cur.execute("DELETE FROM spool WHERE kind=?", (kind,))
        cur.execute(
            "INSERT INTO spool (kind, endpoint, body, created_at) VALUES (?,?,?,?)",
            (kind, endpoint, json.dumps(body), _now_rfc3339()),
        )
        # Enforce bound: drop oldest beyond SPOOL_MAX_ROWS.
        cur.execute(
            "DELETE FROM spool WHERE id NOT IN "
            "(SELECT id FROM spool ORDER BY id DESC LIMIT ?)",
            (SPOOL_MAX_ROWS,),
        )
        self._con.commit()

    def pending(self) -> list[tuple[int, str, dict]]:
        rows = self._con.execute(
            "SELECT id, endpoint, body FROM spool ORDER BY id ASC"
        ).fetchall()
        return [(r[0], r[1], json.loads(r[2])) for r in rows]

    def remove(self, row_id: int) -> None:
        self._con.execute("DELETE FROM spool WHERE id=?", (row_id,))
        self._con.commit()

    def count(self) -> int:
        return int(self._con.execute("SELECT COUNT(*) FROM spool").fetchone()[0])

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # pragma: no cover
            pass


# ──────────────────────────────────────────────────────────────────────────
# Station events: what happened to this station, kept until the VPS has it
# ──────────────────────────────────────────────────────────────────────────
#
# Unlike the coalescing spool, every event is kept (newest EVENTS_MAX_QUEUED)
# until a POST to /events succeeds, so what happened during an outage reaches
# the VPS after it. Each event carries a station-side id and the VPS stores an
# id once, so a resend after a lost answer never doubles a line.
#
# Two sources:
#   * new lines of ~/rovimen_scripts/logs/uplink_events.log (the watchdog's
#     uplink class changes and rescue Wi-Fi up/down), id = hash of the line;
#   * one ``boot`` event per boot (BootTracker), id = the kernel boot_id.

_EVENT_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_json_file(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _write_json_atomic(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(path)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("could not write %s: %s", path, exc)


class EventQueue:
    """Bounded on-disk queue of station events awaiting the VPS's 2xx.

    ``add`` ignores an id already queued; beyond ``max_rows`` the oldest
    queued events are dropped (logged), so an outage of any length costs at
    most the oldest lines, never the disk.
    """

    def __init__(self, path: Path = EVENTS_DB_PATH, max_rows: int = EVENTS_MAX_QUEUED) -> None:
        self._max = max_rows
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._con = sqlite3.connect(str(path), check_same_thread=False)
        self._con.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "event_id TEXT PRIMARY KEY, ts TEXT, kind TEXT, detail TEXT, added REAL)"
        )
        self._con.commit()

    def add(self, event: dict) -> bool:
        """Queue one event; False when its id is already queued."""
        with self._lock:
            cur = self._con.execute(
                "INSERT OR IGNORE INTO events (event_id, ts, kind, detail, added) "
                "VALUES (?,?,?,?,?)",
                (event["id"], event.get("ts"), event["kind"],
                 json.dumps(event.get("detail")), time.time()),
            )
            added = cur.rowcount == 1
            dropped = self._con.execute(
                "DELETE FROM events WHERE rowid NOT IN "
                "(SELECT rowid FROM events ORDER BY rowid DESC LIMIT ?)", (self._max,)
            ).rowcount
            self._con.commit()
        if dropped:
            logger.warning("event queue full: dropped %d oldest undelivered events", dropped)
        return added

    def pending(self, limit: int = EVENTS_BATCH) -> list[dict]:
        with self._lock:
            rows = self._con.execute(
                "SELECT event_id, ts, kind, detail FROM events ORDER BY rowid ASC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for event_id, ts, kind, detail in rows:
            try:
                parsed = json.loads(detail) if detail else None
            except ValueError:
                parsed = detail
            out.append({"id": event_id, "ts": ts, "kind": kind, "detail": parsed})
        return out

    def remove(self, ids: list[str]) -> None:
        if not ids:
            return
        with self._lock:
            self._con.executemany("DELETE FROM events WHERE event_id=?", [(i,) for i in ids])
            self._con.commit()

    def count(self) -> int:
        with self._lock:
            return int(self._con.execute("SELECT COUNT(*) FROM events").fetchone()[0])

    def close(self) -> None:
        try:
            self._con.close()
        except Exception:  # pragma: no cover
            pass


def parse_uplink_log_line(line: str) -> dict | None:
    """One uplink_events.log line as an event, or None for a blank line.

    Lines are ``<ISO-UTC> <text>`` (rovimen_watchdog.sh record_uplink and
    _uplink_event). A line without a leading timestamp is still sent, with
    ts None (the VPS stamps its receipt time).
    """
    line = line.strip()
    if not line:
        return None
    head, _, rest = line.partition(" ")
    if _EVENT_TS_RE.match(head):
        ts, text = head, rest.strip()
    else:
        ts, text = None, line
    if text.startswith("uplink "):
        kind = "uplink"
    elif text.startswith("rescue wifi"):
        kind = "rescue_wifi"
    else:
        kind = "station_log"
    digest = hashlib.sha1(line.encode("utf-8", "replace")).hexdigest()[:20]
    return {"id": "ul-" + digest, "ts": ts, "kind": kind,
            "detail": {"text": text[:EVENT_DETAIL_MAX_CHARS], "source": "uplink_events.log"}}


class UplinkLogTailer:
    """Turns new complete lines of uplink_events.log into queued events.

    The read position (inode + byte offset + id of the last line read) is
    kept in ``state_path``. The watchdog trims the log to its newest 500 lines
    by writing a new file (UPLINK_EVENTS_KEEP), so a replaced or truncated
    log resumes after the last line already read when it is still there, and
    restarts at 0 otherwise. With no saved position the whole log is read
    once, which backfills earlier outages (the queue keeps the newest
    EVENTS_MAX_QUEUED, and the VPS ignores ids it already has).
    """

    def _resume_offset(self, last_id: str | None) -> int:
        """Byte offset just past the last line whose event id is ``last_id``
        in the current file, or 0."""
        if not last_id:
            return 0
        try:
            with open(str(self._log), "rb") as fh:
                data = fh.read(UPLINK_LOG_MAX_READ)
        except OSError:
            return 0
        offset = 0
        found = 0
        for raw in data.splitlines(keepends=True):
            offset += len(raw)
            if not raw.endswith(b"\n"):
                break
            event = parse_uplink_log_line(raw.decode("utf-8", "replace"))
            if event is not None and event["id"] == last_id:
                found = offset
        return found

    def __init__(self, log_path: Path = UPLINK_EVENTS_LOG,
                 state_path: Path = EVENTS_STATE_PATH) -> None:
        self._log = log_path
        self._state_path = state_path

    def poll(self, queue: EventQueue) -> int:
        try:
            st = self._log.stat()
        except OSError:
            return 0
        state = _read_json_file(self._state_path) or {}
        pos = state.get("uplink_log") if isinstance(state.get("uplink_log"), dict) else {}
        try:
            offset = int(pos.get("offset") or 0)
        except (TypeError, ValueError):
            offset = 0
        last_id = pos.get("last_id") if isinstance(pos.get("last_id"), str) else None
        if pos.get("inode") != st.st_ino or st.st_size < offset:
            offset = self._resume_offset(last_id)
        if st.st_size == offset:
            if pos.get("inode") != st.st_ino:
                state["uplink_log"] = {"inode": st.st_ino, "offset": offset, "last_id": last_id}
                _write_json_atomic(self._state_path, state)
            return 0
        try:
            with open(str(self._log), "rb") as fh:
                fh.seek(offset)
                chunk = fh.read(UPLINK_LOG_MAX_READ)
        except OSError as exc:
            logger.warning("could not read %s: %s", self._log, exc)
            return 0
        end = chunk.rfind(b"\n")
        if end < 0:
            if len(chunk) < UPLINK_LOG_MAX_READ:
                return 0  # a line still being written
            end = len(chunk) - 1  # one absurdly long line: take it as is
        complete = chunk[:end + 1]
        added = 0
        for raw in complete.decode("utf-8", "replace").splitlines():
            event = parse_uplink_log_line(raw)
            if event is None:
                continue
            last_id = event["id"]
            if queue.add(event):
                added += 1
        state["uplink_log"] = {"inode": st.st_ino, "offset": offset + len(complete),
                               "last_id": last_id}
        _write_json_atomic(self._state_path, state)
        return added


def read_uplink_state(path: Path = WATCHDOG_STATE_PATH) -> dict | None:
    """The watchdog's latest uplink class: ``{"class": ok|isp|router|link,
    "since": ISO}``, or None when the watchdog has not classified yet."""
    try:
        text = path.read_text()
    except OSError:
        return None
    values: dict = {}
    for line in text.splitlines():
        key, sep, val = line.partition("=")
        if sep:
            values[key.strip()] = val.strip()
    cls = values.get("uplink_state")
    if not cls:
        return None
    since = None
    try:
        since = _iso_utc(float(values.get("uplink_since", "")))
    except (ValueError, OverflowError, OSError):
        pass
    return {"class": cls, "since": since}


def read_boot_id() -> str | None:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip() or None
    except OSError:
        return None


def read_uptime() -> float | None:
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def clock_synchronized() -> bool | None:
    """systemd's NTPSynchronized (the kernel's "clock synced" flag, set by
    timesyncd and chrony alike). None when it cannot be asked."""
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            universal_newlines=True, timeout=5,
        )
    except Exception:
        return None
    value = (out.stdout or "").strip().lower()
    if out.returncode != 0 or value not in ("yes", "no"):
        return None
    return value == "yes"


def system_state() -> str | None:
    """``systemctl is-system-running``: "stopping" during a shutdown/reboot."""
    try:
        out = subprocess.run(["systemctl", "is-system-running"],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True, timeout=3)
    except Exception:
        return None
    return (out.stdout or "").strip() or None


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min"
    days, hours = divmod(hours, 24)
    return f"{days} d {hours} h"


_BOOT_TEXT = {
    "clean": "booted; the previous boot ended with a clean shutdown",
    "unclean": "booted; the previous boot ended abruptly (power cut, freeze or reset)",
    "unknown": "booted; the pusher was stopped before the previous boot ended",
}


class BootTracker:
    """Whether the previous boot ended cleanly, and how long the host was down.

    No root and no new unit: the pusher keeps ``alive_path`` (boot_id + time,
    rewritten every ALIVE_EVERY s) and writes ``stop_path`` when it gets
    SIGTERM, noting ``systemctl is-system-running`` ("stopping" during a
    shutdown or reboot). At the next start:

      * same boot_id: a service restart; the stop marker is discarded;
      * new boot_id and a marker from the previous boot with "stopping": the
        previous boot ended cleanly;
      * new boot_id, no marker: it ended abruptly (power cut, freeze, reset);
      * new boot_id, marker without "stopping": unknown (the pusher was
        stopped by hand before the boot ended).

    Downtime is the new boot time minus the last alive stamp, computed only
    when both were taken on an NTP-synchronised clock: a Pi has no RTC and
    boots with a wrong clock. Until the clock syncs the previous boot's stamp
    is left untouched and the boot event waits; after BOOT_CLOCK_WAIT_S of
    uptime it is sent without a downtime.
    """

    def __init__(self, alive_path: Path = ALIVE_PATH, stop_path: Path = CLEAN_STOP_PATH,
                 *, boot_id_fn=read_boot_id, uptime_fn=read_uptime,
                 clock_fn=clock_synchronized, now_fn=time.time,
                 state_fn=system_state) -> None:
        self._alive_path = alive_path
        self._stop_path = stop_path
        self._uptime_fn = uptime_fn
        self._clock_fn = clock_fn
        self._now_fn = now_fn
        self._state_fn = state_fn
        self._lock = threading.Lock()
        self._clock_checked = float("-inf")
        self._clock_ok = None  # type: bool | None
        self._last_alive_write = float("-inf")
        self.boot_id = boot_id_fn()
        self.previous = None  # type: dict | None  # how the previous boot ended
        self.pending = None   # type: dict | None  # previous-boot facts awaiting a trusted clock
        prev = _read_json_file(alive_path)
        if not self.boot_id:
            return
        stops = self._read_stops()
        if prev and prev.get("boot_id") and prev.get("boot_id") != self.boot_id:
            stop = stops.get(prev.get("boot_id"))
            if isinstance(stop, dict):
                end = "clean" if stop.get("system_state") == "stopping" else "unknown"
            else:
                end = "unclean"
            self.pending = {
                "previous_boot_id": prev.get("boot_id"),
                "end": end,
                "last_alive": prev.get("last_alive"),
                "last_alive_trusted": bool(prev.get("trusted")),
            }
            # The previous boot's marker stays until the boot event is queued,
            # so a restart before the clock syncs classifies it the same way;
            # this boot's own marker (a service restart) goes.
            self._write_stops({k: v for k, v in stops.items() if k == prev.get("boot_id")})
            return
        if prev and prev.get("boot_id") == self.boot_id:
            kept = prev.get("previous")
            self.previous = kept if isinstance(kept, dict) else None
        self._discard_marker()

    def _read_stops(self) -> dict:
        data = _read_json_file(self._stop_path) or {}
        stops = data.get("stops")
        return stops if isinstance(stops, dict) else {}

    def _write_stops(self, stops: dict) -> None:
        if stops:
            _write_json_atomic(self._stop_path, {"stops": stops})
        else:
            self._discard_marker()

    def _discard_marker(self) -> None:
        try:
            self._stop_path.unlink()
        except OSError:
            pass

    def _clock_trusted(self, now: float) -> bool:
        if now - self._clock_checked >= CLOCK_CHECK_EVERY:
            self._clock_checked = now
            self._clock_ok = self._clock_fn()
        # A host that cannot be asked (no timedatectl) is taken at its word.
        return self._clock_ok is not False

    def _boot_time(self, now: float) -> float | None:
        uptime = self._uptime_fn()
        return None if uptime is None else now - uptime

    def _boot_event(self, now: float, trusted: bool) -> dict:
        p = self.pending or {}
        boot_time = self._boot_time(now)
        last_alive = p.get("last_alive")
        alive_ok = bool(p.get("last_alive_trusted")) and isinstance(last_alive, (int, float))
        down_s = None
        if trusted and alive_ok and boot_time is not None:
            down_s = max(0, int(boot_time - last_alive))
        end = p.get("end")
        text = _BOOT_TEXT.get(end, "booted")
        if down_s is not None:
            text += "; host down " + _fmt_duration(down_s)
        elif not trusted:
            text += "; downtime unknown (clock not synchronised)"
        boot_iso = _iso_utc(boot_time) if (trusted and boot_time is not None) else None
        detail = {
            "text": text,
            "boot_id": self.boot_id,
            "boot_time": boot_iso,
            "previous_boot_id": p.get("previous_boot_id"),
            "previous_end": end,
            "last_alive": _iso_utc(last_alive) if alive_ok else None,
            "down_s": down_s,
            "clock": "synced" if trusted else "unsynced",
        }
        self.previous = {"end": end, "down_s": down_s,
                         "previous_boot_id": p.get("previous_boot_id")}
        return {"id": "boot-" + str(self.boot_id), "ts": boot_iso or _iso_utc(now),
                "kind": "boot", "detail": detail}

    def tick(self, queue: EventQueue | None) -> None:
        """Emit the pending boot event when the clock allows, then keep the
        alive stamp fresh. Called every few seconds from a daemon thread."""
        if not self.boot_id:
            return
        with self._lock:
            now = self._now_fn()
            trusted = self._clock_trusted(now)
            if self.pending is not None:
                uptime = self._uptime_fn()
                if not trusted and (uptime is None or uptime < BOOT_CLOCK_WAIT_S):
                    return  # keep the previous boot's stamp until it can be used
                event = self._boot_event(now, trusted)
                if queue is not None:
                    queue.add(event)
                logger.info("boot event: %s", event["detail"]["text"])
                self.pending = None
                self._discard_marker()
                self._last_alive_write = float("-inf")
            if now - self._last_alive_write >= ALIVE_EVERY:
                _write_json_atomic(self._alive_path, {
                    "boot_id": self.boot_id,
                    "boot_time": self._boot_time(now),
                    "last_alive": now,
                    "trusted": trusted,
                    "previous": self.previous,
                })
                self._last_alive_write = now

    def record_stop(self) -> None:
        """SIGTERM: note the stop and whether the system is shutting down."""
        if not self.boot_id:
            return
        keep = {self.boot_id}
        if self.pending is not None:
            keep.add(self.pending.get("previous_boot_id"))
        stops = {k: v for k, v in self._read_stops().items() if k in keep}
        stops[self.boot_id] = {"at": self._now_fn(), "system_state": self._state_fn()}
        self._write_stops(stops)

    def boot_block(self) -> dict | None:
        """The heartbeat's ``boot`` field."""
        if not self.boot_id:
            return None
        now = self._now_fn()
        uptime = self._uptime_fn()
        trusted = self._clock_ok is not False
        return {
            "boot_id": self.boot_id,
            "boot_time": _iso_utc(now - uptime) if (uptime is not None and trusted) else None,
            "uptime_s": int(uptime) if uptime is not None else None,
            "clock_synced": self._clock_ok,
            "previous": self.previous,
            "pending": self.pending is not None,
        }


# ──────────────────────────────────────────────────────────────────────────
# HTTP POST with retry/backoff
# ──────────────────────────────────────────────────────────────────────────


class PostResult:
    __slots__ = ("ok", "status", "retry_after")

    def __init__(self, ok: bool, status: int | None = None,
                 retry_after: float | None = None) -> None:
        self.ok = ok
        self.status = status
        self.retry_after = retry_after


def post_json(url: str, key: str, body: dict, timeout: int = HTTP_TIMEOUT) -> PostResult:
    """Single POST attempt. Returns PostResult; never raises.

    2xx -> ok. 429 -> not ok, carries Retry-After. Other errors -> not ok.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("X-Station-Key", key)
    req.add_header("User-Agent", f"rovimen-pusher/{AGENT_VERSION}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return PostResult(ok=200 <= resp.status < 300, status=resp.status)
    except urllib.error.HTTPError as exc:
        retry_after = None
        if exc.code == 429:
            ra = exc.headers.get("Retry-After") if exc.headers else None
            try:
                retry_after = float(ra) if ra is not None else None
            except (TypeError, ValueError):
                retry_after = None
        logger.warning("POST %s -> HTTP %s", url, exc.code)
        return PostResult(ok=False, status=exc.code, retry_after=retry_after)
    except Exception as exc:
        logger.warning("POST %s failed: %s", url, exc)
        return PostResult(ok=False, status=None)


def backoff_delay(attempt: int) -> float:
    """Exponential backoff with jitter, capped at MAX_BACKOFF."""
    raw = min(BASE_BACKOFF * (2 ** attempt), MAX_BACKOFF)
    return raw / 2 + random.uniform(0, raw / 2)


# ──────────────────────────────────────────────────────────────────────────
# Publisher
# ──────────────────────────────────────────────────────────────────────────


class Publisher:
    """Drives the periodic POSTs and the spool drain."""

    def __init__(self, base_url: str, host_key: str, key: str,
                 seq: SeqCounter, spool: Spool,
                 events: EventQueue | None = None,
                 boot: BootTracker | None = None,
                 uplink_tailer: UplinkLogTailer | None = None,
                 watchdog_state_path: Path | None = None) -> None:
        self._base = base_url.rstrip("/")
        # Station event history (all optional: tests and callers that predate
        # it construct a Publisher without them).
        self._events = events
        self._boot = boot
        self._uplink_tailer = uplink_tailer
        self._watchdog_state_path = watchdog_state_path or WATCHDOG_STATE_PATH
        self._host_key = host_key
        self._key = key
        self._seq = seq
        self._spool = spool
        self._stop = threading.Event()
        # Last calibration body the VPS accepted, and when (monotonic).
        self._calibration_digest: str | None = None
        self._calibration_sent_at = 0.0
        # Digest of the last live frame the VPS accepted, per camera: a
        # daytime captured stack does not change for hours.
        self._frame_digests: dict = {}

    def _url(self, endpoint: str) -> str:
        return f"{self._base}/api/ingest/v1/{self._host_key}/{endpoint}"

    def stop(self) -> None:
        self._stop.set()

    def _post(self, kind: str, endpoint: str, payload: dict) -> PostResult:
        """Build an enveloped body, POST it, spool it on failure.

        Returns the full :class:`PostResult` so a caller can tell a permanent
        rejection (a 4xx the same body will never pass) from a transient one
        (5xx / connection error / 429) and react differently.
        """
        body = build_envelope(self._seq.next(), payload)
        result = post_json(self._url(endpoint), self._key, body)
        if result.ok:
            return result
        if kind != "detections":
            # Buffer everything except detections (those re-derive from the index).
            self._spool.add(kind, endpoint, body)
        if result.retry_after:
            self._stop.wait(min(result.retry_after, MAX_BACKOFF))
        return result

    def _send(self, kind: str, endpoint: str, payload: dict) -> bool:
        """Boolean form of :meth:`_post` — success/failure is all most kinds need."""
        return self._post(kind, endpoint, payload).ok

    def drain_spool(self) -> None:
        """Attempt to flush buffered POSTs; stop at the first failure."""
        for row_id, endpoint, body in self._spool.pending():
            if self._stop.is_set():
                return
            result = post_json(self._url(endpoint), self._key, body)
            if result.ok:
                self._spool.remove(row_id)
            else:
                if result.retry_after:
                    self._stop.wait(min(result.retry_after, MAX_BACKOFF))
                return

    def push_heartbeat(self) -> bool:
        status = fetch_status()
        if status is None:
            return False
        uplink = read_uplink_state(self._watchdog_state_path)
        boot = None
        if self._boot is not None:
            try:
                boot = self._boot.boot_block()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("boot block failed: %s", exc)
        return self._send("heartbeat", "heartbeat",
                          build_heartbeat_payload(status, uplink=uplink, boot=boot))

    def push_events(self) -> bool:
        """Queue new uplink log lines, then POST queued events oldest first.

        A 2xx acknowledges the whole batch (the VPS stores new ids and skips
        ones it has). 404 means a VPS without the route yet: keep everything
        for later. A 422 can never pass, so that batch is dropped with an
        error rather than blocking every event behind it.
        """
        if self._events is None:
            return True
        if self._uplink_tailer is not None:
            try:
                self._uplink_tailer.poll(self._events)
            except Exception as exc:
                logger.warning("uplink log scan failed: %s", exc)
        while not self._stop.is_set():
            batch = self._events.pending(EVENTS_BATCH)
            if not batch:
                return True
            body = build_envelope(self._seq.next(), {"events": batch})
            result = post_json(self._url("events"), self._key, body)
            ids = [e["id"] for e in batch]
            if result.ok:
                self._events.remove(ids)
                continue
            if result.status == 422:
                logger.error("events batch REJECTED (HTTP 422), dropping %d events: %s",
                             len(ids), ", ".join(ids[:5]))
                self._events.remove(ids)
                continue
            if result.status == 404:
                return True  # VPS predates the events route: keep them queued
            if result.retry_after:
                self._stop.wait(min(result.retry_after, MAX_BACKOFF))
            return False
        return True

    def push_vitals(self) -> bool:
        vitals = fetch_vitals()
        if vitals is None:
            return False
        return self._send("vitals", "vitals", build_vitals_payload(vitals))

    def push_detections(self) -> bool:
        """POST the hot window in bounded batches, oldest night first.

        Every batch carries its own envelope and is acknowledged on its own, so
        a rejected batch never marks the batches after it as delivered. The run
        stops at the first failure and the next cycle re-derives the window from
        the local index and starts over — free, because the VPS upserts on
        (cam, date, ff_file, meteor_no), so re-sending an already-accepted batch
        is a no-op on identity.
        """
        since = _window_since()
        detections = fetch_detections(since)
        night_sky = fetch_night_sky(since)
        if not detections:
            # A clouded-out window can be all-sky, no detections. Still push the
            # sky rows (one envelope) so the VPS labels the night as weather.
            if not night_sky:
                return True  # nothing to send is success
            result = self._post("detections", "detections",
                                build_detections_payload(since, [], night_sky))
            return result.ok
        batches = chunk_detections(detections)
        for index, batch in enumerate(batches, start=1):
            # Sky rows are per-night, not per-batch: ship them once, on the
            # first batch, so a multi-batch window doesn't re-send them N times.
            sky = night_sky if index == 1 else None
            result = self._post("detections", "detections",
                                build_detections_payload(since, batch, sky))
            if result.ok:
                continue
            if is_permanent_status(result.status):
                # A body the VPS will never accept. Say so loudly and stop: the
                # index on the VPS is stale for this station from here on, and
                # no amount of retrying inside this cycle changes that.
                logger.error(
                    "detections push REJECTED for station %s: HTTP %s on batch "
                    "%d/%d (%d rows of %d, window since %s) — the VPS detection "
                    "index for this station is STALE from this batch onward",
                    self._host_key, result.status, index, len(batches),
                    len(batch), len(detections), since,
                )
            else:
                logger.warning(
                    "detections push failed for station %s: HTTP %s on batch "
                    "%d/%d (%d rows) — %d batches deferred to the next cycle",
                    self._host_key, result.status, index, len(batches),
                    len(batch), len(batches) - index,
                )
            return False
        return True

    def push_media(self) -> bool:
        tl = fetch_timelapses()
        if tl is None:
            return False
        # Locked-clip pointers ride the same media POST (§2.4.5). A failed clip
        # scan (station_api hiccup) still lets timelapse/nightstack pointers
        # through — clips just default to empty that cycle and refresh next time.
        try:
            clips = fetch_locked_clips()
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("locked-clip scan failed: %s", exc)
            clips = {}
        return self._send("media", "media", build_media_payload(tl, clips))

    def push_calibration(self) -> bool:
        """POST platepar, masks, last night's calibration summary and the
        hardware profile when any of them changed (#1248), or every
        CALIBRATION_REFRESH so a VPS that lost its copy recovers."""
        state = fetch_calibration_state()
        payload = build_calibration_payload(
            state["platepar"], state["summaries"], state["hardware"], state["masks"],
        )
        if all(v is None for v in payload.values()):
            return False  # station_api unreachable; nothing worth sending
        digest = calibration_digest(payload)
        now = time.monotonic()
        if (digest == self._calibration_digest
                and now - self._calibration_sent_at < CALIBRATION_REFRESH):
            return True
        # Not spooled: the next cycle re-reads and re-sends the current state,
        # and a spooled body the VPS rejects would hold up the whole spool.
        body = build_envelope(self._seq.next(), payload)
        result = post_json(self._url("calibration"), self._key, body)
        if not result.ok:
            if result.retry_after:
                self._stop.wait(min(result.retry_after, MAX_BACKOFF))
            return False
        self._calibration_digest = digest
        self._calibration_sent_at = now
        return True

    def push_live_frames(self) -> bool:
        """POST each camera's newest frame for the VPS sky dome (#1249), only
        when it changed. Not spooled: a stale frame is worth nothing."""
        ok_all = True
        for cam in fetch_live_camera_codes():
            fetched = fetch_live_frame(cam)
            if fetched is None:
                continue
            data, ctype, stamp = fetched
            digest = hashlib.sha256(data).hexdigest()
            if self._frame_digests.get(cam) == digest:
                continue
            payload = {"content_type": ctype, "frame_time": stamp,
                       "data_b64": base64.b64encode(data).decode("ascii")}
            body = build_envelope(self._seq.next(), payload)
            result = post_json(self._url(f"live_frame/{cam}"), self._key, body)
            if result.ok:
                self._frame_digests[cam] = digest
            else:
                ok_all = False
                if result.retry_after:
                    self._stop.wait(min(result.retry_after, MAX_BACKOFF))
        return ok_all

    def push_live_thumbs(self) -> bool:
        """Push the newest BW maxpixel WebP for each camera (§9).

        Ephemeral: the newest FF only, no archival value, so the small WebP bytes
        go to the VPS live_thumb ingest (base64 in the envelope) rather than the
        bytes→box path used for archival media. Not spooled — a stale live thumb
        is worthless, so a failed push is simply retried on the next cycle.
        Returns True if every camera with a thumbnail pushed successfully (a
        camera with no current FF is not a failure)."""
        ok_all = True
        for cam in fetch_live_camera_codes():
            fetched = fetch_live_thumb(cam)
            if fetched is None:
                continue  # no FF yet / decode failed — not a push failure
            webp, ts = fetched
            body = build_envelope(self._seq.next(), build_live_thumb_payload(webp, ts))
            result = post_json(self._url(f"live_thumb/{cam}"), self._key, body)
            if not result.ok:
                ok_all = False
                if result.retry_after:
                    self._stop.wait(min(result.retry_after, MAX_BACKOFF))
        return ok_all


# ──────────────────────────────────────────────────────────────────────────
# Main loop
# ──────────────────────────────────────────────────────────────────────────


def run(publisher: Publisher, stop_event: threading.Event,
        live_thumb_interval: int = LIVE_THUMB_INTERVAL,
        intervals: dict[str, int] | None = None) -> None:
    """Scheduling loop. Each kind fires on its own cadence; spool drained first.

    ``intervals`` (config-resolved) overrides all cadences; when None the module
    defaults are used (with ``live_thumb_interval`` for the live tile) so existing
    callers/tests keep working."""
    if intervals is None:
        intervals = {
            "heartbeat": HEARTBEAT_INTERVAL,
            "vitals": VITALS_INTERVAL,
            "detections": DETECTIONS_INTERVAL,
            "media": MEDIA_INTERVAL,
            "calibration": CALIBRATION_INTERVAL,
            "live_frame": LIVE_FRAME_INTERVAL,
            "live_thumb": live_thumb_interval,
            "chunks": CHUNKS_INTERVAL,
            "events": EVENTS_INTERVAL,
        }
    last = {k: 0.0 for k in intervals}
    handlers = {
        "heartbeat": publisher.push_heartbeat,
        "vitals": publisher.push_vitals,
        "detections": publisher.push_detections,
        "media": publisher.push_media,
        "calibration": publisher.push_calibration,
        "live_frame": publisher.push_live_frames,
        "live_thumb": publisher.push_live_thumbs,
        "chunks": lambda: push_chunk_listings(publisher),
        "events": publisher.push_events,
    }
    fail_streak = 0
    while not stop_event.is_set():
        # Flush buffered POSTs before new ones so ordering roughly holds.
        publisher.drain_spool()
        now = time.monotonic()
        any_fail = False
        for kind, interval in intervals.items():
            if stop_event.is_set():
                break
            if now - last[kind] >= interval:
                try:
                    ok = handlers[kind]()
                except Exception as exc:  # never let a kind crash the loop
                    logger.exception("push %s raised: %s", kind, exc)
                    ok = False
                last[kind] = now
                any_fail = any_fail or not ok
        if any_fail:
            fail_streak += 1
            stop_event.wait(backoff_delay(min(fail_streak, 6)))
        else:
            fail_streak = 0
            stop_event.wait(1.0)


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("ROVIMEN_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    cfg = _load_config()
    key = resolve_ingest_key()
    if not key:
        # Opt-in gate: no key -> station not enrolled in the push pilot. Exit
        # cleanly so the systemd unit can be enabled everywhere yet stay dormant.
        logger.info("no ingest key at %s and ROVIMEN_INGEST_KEY unset — "
                    "push not enabled for this station, exiting", KEY_PATH)
        return 0

    host_key = resolve_host_key(cfg)
    if not host_key:
        logger.error("could not determine host_key (set ROVIMEN_HOST_KEY or "
                     "config.json host_key) — refusing to push")
        return 1

    base_url = resolve_base_url(cfg)
    if not base_url:
        logger.error("could not determine ingest base URL (set "
                     "ROVIMEN_INGEST_BASE_URL or config vps_host) — refusing to push")
        return 1

    logger.info("rovimen-pusher %s starting: host_key=%s base=%s",
                AGENT_VERSION, host_key, base_url)

    seq = SeqCounter()
    spool = Spool()
    events = None
    boot = None
    try:
        events = EventQueue()
        boot = BootTracker()
    except Exception as exc:  # never let the history cost the telemetry
        logger.warning("station event history unavailable: %s", exc)
    publisher = Publisher(base_url, host_key, key, seq, spool,
                          events=events, boot=boot,
                          uplink_tailer=UplinkLogTailer() if events is not None else None)
    stop_event = threading.Event()

    def _handle_signal(signum, _frame):
        logger.info("received signal %s, shutting down", signum)
        if signum == signal.SIGTERM and boot is not None:
            try:
                boot.record_stop()
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("could not record stop marker: %s", exc)
        stop_event.set()
        publisher.stop()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    def _alive_loop() -> None:
        # Own thread so the alive stamp stays fresh while the main loop sits
        # in a long backoff or a slow POST during an uplink outage.
        while not stop_event.is_set():
            try:
                boot.tick(events)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("boot tracker tick failed: %s", exc)
            stop_event.wait(10.0)

    if boot is not None:
        threading.Thread(target=_alive_loop, name="boot-tracker", daemon=True).start()

    try:
        run(publisher, stop_event, intervals=resolve_intervals(cfg))
    finally:
        stop_event.set()
        spool.close()
        if events is not None:
            events.close()
    logger.info("rovimen-pusher stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
