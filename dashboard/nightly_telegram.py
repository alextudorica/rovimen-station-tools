#!/usr/bin/env python3
"""nightly_telegram.py -- VPS-side nightly meteor summary -> Telegram.

Posts the night's results for the ROVIMEN cameras to the "Meteori România"
channel (``@meteori_romania``). It reads only local VPS state -- the fleet-wide
detection index (:mod:`detection_db`) and the storage-box archive
(:data:`cache_store.ARCHIVE_PATH`) that the dashboard already maintains -- so it
has no station connectivity dependency. Runs once per night from
``rovimen-nightly-summary.timer`` and is idempotent per night via a sent marker.

The channel is Romanian-language and public, so the post is written in Romanian.
It is pure data -- every shower, fireball and reporting camera the index holds
for the night, with no generated prose and nothing topped-N, so a reader can
reconcile it against the dashboard line for line. Operational detail (cameras that went
quiet, cameras whose detection files stopped arriving) goes to the operator chat
only, where it is actionable.

Media comes from the ``media_pointers`` index rather than the filesystem: it is
what the dashboard itself serves from, it records the clip's real ``meteor_time``
(the FF block timestamp in a detection row does NOT match the colour clip's
filename -- matching on it found 6 of 200 clips; matching on ``meteor_time``
within a 30 s window finds 172 of 200), and it knows which cameras actually have
a night stack instead of making the gate a glob that silently misses.

Secrets and paths come from the environment (the same
``/etc/rovimen-dashboard.env`` the dashboard and index-poller already use):

    ROVIMEN_TELEGRAM_BOT_TOKEN     Telegram bot token (required to post)
    ROVIMEN_TELEGRAM_RO_CHAT_ID    "Meteori România" channel chat id (required)
    ROVIMEN_TELEGRAM_CHAT_ID       optional operator chat; gets the ops appendix
    ROVIMEN_CONFIG                 dashboard_config.yaml path (falls back to
                                   ROVIMEN_DASHBOARD_CONFIG then the sibling file)
    ROVIMEN_PUBLIC_BASE_URL        dashboard origin used for the "see more" link
    ROVIMEN_NIGHTLY_CAM_PREFIXES   camera prefixes to include, default "RO,MD"
    ROVIMEN_NIGHTLY_FIREBALL_MAG   fireball threshold, default -4.0
    ROVIMEN_NIGHTLY_STALE_HOURS    station-freshness window, default 30
    ROVIMEN_NIGHTLY_MAX_STACKS     cap on stack images posted, default 30
    ROVIMEN_NIGHTLY_SEND_CLIP      "1"/"0" post the brightest clip, default "1"
    ROVIMEN_NIGHTLY_CLIP_WINDOW_S  clip<->detection match window, default 30
    ROVIMEN_NIGHTLY_STATE_DIR      sent-marker dir, default <detections.db dir>/nightly_telegram_state

Usage:
    python nightly_telegram.py [YYYYMMDD] [--dry-run] [--force] [--no-clip]
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

import cache_store
from config_file import dashboard_config_path
import detection_db
import hidden_detections

logger = logging.getLogger("nightly_telegram")

# A meteor at least this bright (more negative) is a fireball. The old value of
# 0.0 mag reported 212 "fireballs" on a night that produced 8, and 1329 on a
# night that produced 154 -- every post overstated the count by more than an
# order of magnitude. -4 is the conventional threshold (roughly Venus).
DEFAULT_FIREBALL_MAG = -4.0

# Anything at least this bright is worth naming in the post even when it is not
# a fireball, so quiet nights still have a headline.
NOTABLE_MAG = -2.0

TELEGRAM_API = "https://api.telegram.org"

# Telegram caps a media group at 10 items.
MEDIA_GROUP_MAX = 10

# IAU shower codes -> Romanian names, covering the showers that actually produce
# counts over Romania. An unlisted code is shown as the bare IAU code rather than
# guessed at or dropped: the post has to stay complete, and a wrong translation
# is worse than the code every observer already recognises.
SHOWER_RO: dict[str, str] = {
    "PER": "Perseide",
    "ORI": "Orionide",
    "GEM": "Geminide",
    "QUA": "Quadrantide",
    "LYR": "Lyride",
    "ETA": "Eta Aquaride",
    "SDA": "Delta Aquaride sudice",
    "NDA": "Delta Aquaride nordice",
    "CAP": "Alfa Capricornide",
    "KCG": "Kappa Cygnide",
    "NIA": "iota Aquaride nordice",
    "SIA": "iota Aquaride sudice",
    "PAU": "Piscis Austrinide",
    "JPE": "Pegaside de iulie",
    "SPE": "Perseide de septembrie",
    "AUD": "Aurigide",
    "ERI": "Eta Eridanide",
    "TAU": "Tauride",
    "STA": "Tauride sudice",
    "NTA": "Tauride nordice",
    "LEO": "Leonide",
    "URS": "Urside",
    "MON": "Monocerotide",
    "HYD": "Sigma Hydride",
    "COM": "Comae Berenicide",
    "SPO": "sporadici",
}

LUNI_RO = [
    "ianuarie", "februarie", "martie", "aprilie", "mai", "iunie",
    "iulie", "august", "septembrie", "octombrie", "noiembrie", "decembrie",
]


# ---------------------------------------------------------------------------
# Environment / configuration
# ---------------------------------------------------------------------------

def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name) or default)
    except ValueError:
        logger.warning("%s is not a number; using %s", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name) or default)
    except ValueError:
        logger.warning("%s is not an integer; using %s", name, default)
        return default


def _state_dir() -> Path:
    val = _env("ROVIMEN_NIGHTLY_STATE_DIR")
    if val:
        return Path(val)
    # Next to the detection DB (i.e. /opt/rovimen), which is writable by the
    # service and honours ROVIMEN_DETECTIONS_DB rather than hardcoding a path.
    return detection_db.DB_PATH.parent / "nightly_telegram_state"


def cam_prefixes() -> tuple[str, ...]:
    raw = _env("ROVIMEN_NIGHTLY_CAM_PREFIXES") or "RO,MD"
    return tuple(p.strip().upper() for p in raw.split(",") if p.strip())


def default_night_id() -> str:
    """Evening date (YYYYMMDD) of the last night that ended by 06:00 UTC.

    Evening date ``D`` counts as ended at ``D+1 06:00`` UTC, so this is
    ``now - 30 h``. The timer fires at 06:30 and names last night; an afternoon
    run (the timer's ``Persistent=true`` catch-up after a VPS boot, or a manual
    re-run) names the same night, never tonight's, which has not happened yet
    and whose ``.sent`` marker would make the next morning's summary skip.
    This matches the ``date`` the detection index stamps on a session (the
    archive ``<cam>/<YYYYMMDD>/`` night dir, i.e. the evening date).
    """
    return (datetime.now(timezone.utc) - timedelta(hours=30)).strftime("%Y%m%d")


def format_night_ro(night_id: str) -> str:
    d = datetime.strptime(night_id, "%Y%m%d")
    return f"{d.day} {LUNI_RO[d.month - 1]} {d.year}"


def shower_ro(code: str | None) -> str:
    if not code:
        return "sporadici"
    return SHOWER_RO.get(code.upper(), code.upper())


# ---------------------------------------------------------------------------
# Station registry
# ---------------------------------------------------------------------------

def load_stations(config_path: Path) -> tuple[dict[str, list[str]], dict[str, str], set[str]]:
    """Return (station -> [cam codes], cam code -> location, commissioning cams).

    Commissioning stations are INCLUDED. Excluding them (the previous behaviour,
    a ``status == "active"`` filter) dropped RO001F/Bisoca -- which on 2026-08-16
    recorded every single one of the night's 8 real fireballs, including a -6.4,
    while the post announced "177 fireballs" and named none of them. A station
    still being commissioned produces real science; it is only its *health* that
    is not yet guaranteed, so its cameras are reported and its silence is not
    treated as an outage.
    """
    config = cache_store.load_config(config_path)
    prefixes = cam_prefixes()
    station_cams: dict[str, list[str]] = {}
    cam_location: dict[str, str] = {}
    commissioning: set[str] = set()

    for host_key, st in config.stations.items():
        status = getattr(st, "status", "active")
        if status not in ("active", "commissioning"):
            continue
        cams = [c.code for c in st.cameras
                if c.code.upper().startswith(prefixes)]
        if not cams:
            continue
        station_cams[host_key] = cams
        location = (st.location_name or st.label or host_key)
        for code in cams:
            cam_location[code] = location
            if status == "commissioning":
                commissioning.add(code)
    return station_cams, cam_location, commissioning


# ---------------------------------------------------------------------------
# Detection data
# ---------------------------------------------------------------------------

def _mag(row: dict[str, Any]) -> float | None:
    m = row.get("mag_apparent")
    if m is None:
        m = row.get("mag_absolute")
    return m


def collect_night(night_id: str, cams: set[str], fireball_mag: float) -> dict[str, Any]:
    """Aggregate the night's detections for *cams* from the index."""
    # Pass the path explicitly: detection_db binds DB_PATH as a default argument
    # at import time, so a later change to the module attribute (tests, or an
    # env var read after import) would otherwise be ignored.
    rows = detection_db.query_detections(
        [night_id], cam_filter=cams, path=detection_db.DB_PATH)

    per_cam: dict[str, dict[str, Any]] = {}
    showers: dict[str, int] = {}
    brightest: dict[str, Any] | None = None
    fireballs: list[dict[str, Any]] = []

    # Detections a user hid as false positives (birds, satellites) still
    # count as detections, but are never the brightest or a fireball.
    hidden = hidden_detections.snapshot()
    for r in rows:
        cam = r["cam"]
        slot = per_cam.setdefault(cam, {"count": 0, "fireballs": 0, "brightest": None})
        slot["count"] += 1

        code = (r.get("shower") or "SPO").upper()
        showers[code] = showers.get(code, 0) + 1

        m = _mag(r)
        if m is None or hidden.match(r) is not None:
            continue
        if slot["brightest"] is None or m < slot["brightest"]:
            slot["brightest"] = m
        entry = {
            "mag": m, "cam": cam, "ff_file": r.get("ff_file"),
            "date": r.get("date"), "shower": r.get("shower"),
            "time_utc": r.get("time_utc"),
        }
        if m <= fireball_mag:
            slot["fireballs"] += 1
            fireballs.append(entry)
        if brightest is None or m < brightest["mag"]:
            brightest = entry

    fireballs.sort(key=lambda d: d["mag"])
    return {
        "night_id": night_id,
        "total": len(rows),
        "per_cam": per_cam,
        "showers": showers,
        "brightest": brightest,
        "fireballs": fireballs,
    }


def _open_index_ro() -> sqlite3.Connection | None:
    db = detection_db.DB_PATH
    if not db.exists():
        return None
    try:
        return sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=30)
    except sqlite3.Error as exc:
        logger.warning("cannot open detection index read-only: %s", exc)
        return None


def clip_counts(night_id: str, cams: set[str]) -> dict[str, int]:
    """Uploaded detection clips per camera for the night, from ``media_pointers``.

    This is the signal that separates "clear sky, nothing to see" from "the
    camera is detecting fine but its FTPdetectinfo stopped reaching the index".
    On 2026-08-16 eight cameras (Bistrița and Vaslui) uploaded 65-136 detection
    clips each while the index held zero rows for them, and the post filed all
    eight under "senin / fără meteori".
    """
    con = _open_index_ro()
    if con is None or not cams:
        return {}
    try:
        ph = ",".join("?" * len(cams))
        rows = con.execute(
            f"SELECT cam, COUNT(*) FROM media_pointers "
            f"WHERE date=? AND kind='clip' AND cam IN ({ph}) GROUP BY cam",
            [night_id, *cams],
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("clip_counts failed: %s", exc)
        return {}
    finally:
        con.close()
    return {cam: n for cam, n in rows}


def station_last_activity(station_cams: dict[str, list[str]]) -> dict[str, float | None]:
    """Most-recent activity timestamp per station (unix seconds), or None.

    Reads ``poll_state.last_polled``, which ``ingest_api.touch_poll_state``
    refreshes on any accepted push. ``ingested_files`` is deliberately NOT
    consulted: the fleet finished migrating to push, so its newest row is weeks
    old and it can only ever drag a live station towards "offline".
    """
    result: dict[str, float | None] = {hk: None for hk in station_cams}
    con = _open_index_ro()
    if con is None:
        return result
    try:
        for hk in station_cams:
            row = con.execute(
                "SELECT last_polled, error_count FROM poll_state WHERE host_key=?",
                (hk,),
            ).fetchone()
            if row and row[0] and (row[1] or 0) == 0:
                result[hk] = row[0]
    except sqlite3.Error as exc:
        logger.warning("station_last_activity failed: %s", exc)
    finally:
        con.close()
    return result


def classify(
    night: dict[str, Any],
    station_cams: dict[str, list[str]],
    commissioning: set[str],
    stale_hours: float,
) -> dict[str, Any]:
    """Split cameras into reported / index_gap / quiet / offline.

    reported:  has detections in the index.
    index_gap: no index rows but the camera uploaded detection clips -- it saw
               meteors, the detection file just did not arrive. An operator
               problem, never "clear skies".
    quiet:     no detections and no clips, station fresh -- genuinely nothing
               (cloud, or a camera that needs looking at).
    offline:   no detections and the station has not reported recently.

    Commissioning cameras never land in ``offline``: they are expected to be
    intermittent, and reporting them as an outage every night is noise.
    """
    per_cam = night["per_cam"]
    all_cams = {c for cams in station_cams.values() for c in cams}
    clips = clip_counts(night["night_id"], all_cams)
    last = station_last_activity(station_cams)
    now = time.time()
    stale_s = stale_hours * 3600.0

    reported: list[dict[str, Any]] = []
    index_gap: list[dict[str, Any]] = []
    quiet: list[str] = []
    offline: list[str] = []

    for hk, cams in station_cams.items():
        ts = last.get(hk)
        fresh = ts is not None and (now - ts) <= stale_s
        has_det = any(per_cam.get(c, {}).get("count", 0) > 0 for c in cams)
        online = has_det or fresh
        for cam in cams:
            slot = per_cam.get(cam)
            if slot and slot["count"] > 0:
                reported.append({"cam": cam, **slot})
            elif clips.get(cam, 0) > 0:
                index_gap.append({"cam": cam, "clips": clips[cam]})
            elif online or cam in commissioning:
                quiet.append(cam)
            else:
                offline.append(cam)

    reported.sort(key=lambda d: d["count"], reverse=True)
    index_gap.sort(key=lambda d: d["clips"], reverse=True)
    return {
        "reported": reported,
        "index_gap": index_gap,
        "quiet": sorted(quiet),
        "offline": sorted(offline),
    }


# ---------------------------------------------------------------------------
# Summary text
# ---------------------------------------------------------------------------

def _e(value: Any) -> str:
    """Escape for Telegram's HTML parse mode."""
    return html.escape(str(value), quote=False)


def build_message(night: dict[str, Any], groups: dict[str, Any],
                  cam_location: dict[str, str], fireball_mag: float) -> str:
    """The public post: every figure the index holds for the night, in Romanian.

    Nothing is topped-N. A reader comparing this against the dashboard should
    find the same numbers and the same number of rows, so every shower, every
    fireball and every reporting camera is listed. :func:`split_message` keeps
    that within Telegram's per-message limit rather than truncating.

    "detecții" is deliberate and not interchangeable with "meteori": one meteor
    seen by three cameras is three rows in the index, so the total is a count of
    detections, not of distinct meteors.
    """
    total = night["total"]
    lines = [f"<b>Rezumatul nopții — {_e(format_night_ro(night['night_id']))}</b>", ""]

    if not groups["reported"]:
        lines.append("Nicio detecție înregistrată în această noapte.")
        return "\n".join(lines)

    locations = len({cam_location.get(i["cam"]) for i in groups["reported"]})
    lines.append(
        f"<b>{total}</b> detecții · <b>{len(groups['reported'])}</b> camere · "
        f"<b>{locations}</b> locații"
    )

    showers = sorted(night["showers"].items(), key=lambda kv: kv[1], reverse=True)
    named = [(c, n) for c, n in showers if c != "SPO"]
    if named:
        lines += ["", "<b>Roiuri</b>"]
        for code, n in named:
            pct = 100.0 * n / total if total else 0.0
            lines.append(f"  {_e(shower_ro(code))}: <b>{n}</b> ({pct:.0f}%)")
    spo = night["showers"].get("SPO", 0)
    if spo:
        pct = 100.0 * spo / total if total else 0.0
        lines.append(f"  sporadici: <b>{spo}</b> ({pct:.0f}%)")

    fireballs = night["fireballs"]
    if fireballs:
        word = "bolid" if len(fireballs) == 1 else "bolizi"
        lines += ["", f"<b>{len(fireballs)} {word}</b> (mag ≤ {fireball_mag:+.0f})"]
        for fb in fireballs:
            loc = cam_location.get(fb["cam"], "")
            when = (fb.get("time_utc") or "")[11:16]
            bits = [f"mag <b>{fb['mag']:+.1f}</b>"]
            if loc:
                bits.append(_e(loc))
            if when:
                bits.append(f"{when} UTC")
            lines.append(f"  {' · '.join(bits)} — {_e(shower_ro(fb.get('shower')))}")
    else:
        br = night["brightest"]
        if br and br["mag"] <= NOTABLE_MAG:
            loc = cam_location.get(br["cam"], "")
            when = (br.get("time_utc") or "")[11:16]
            bits = [f"<b>mag {br['mag']:+.1f}</b>"]
            if loc:
                bits.append(_e(loc))
            if when:
                bits.append(f"{when} UTC")
            lines += ["", "Cea mai luminoasă: " + " · ".join(bits)]

    lines += ["", "<b>Camere</b>"]
    for item in groups["reported"]:
        loc = cam_location.get(item["cam"], item["cam"])
        line = f"  {_e(loc)} ({_e(item['cam'])}): <b>{item['count']}</b>"
        if item["brightest"] is not None:
            line += f" · max {item['brightest']:+.1f}"
        lines.append(line)

    base = _env("ROVIMEN_PUBLIC_BASE_URL")
    if base:
        lines += ["", f'<a href="{_e(base.rstrip("/"))}">Harta și arhiva completă</a>']
    return "\n".join(lines)


def _strip_html(text: str) -> str:
    """Plain-text form of an HTML-formatted block, for the journal."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def build_ops_appendix(groups: dict[str, Any], cam_location: dict[str, str]) -> str | None:
    """Operator-only detail. Never goes to the public channel."""
    if not (groups["index_gap"] or groups["quiet"] or groups["offline"]):
        return None
    lines = ["<b>Ops</b>"]
    if groups["index_gap"]:
        lines.append("Clips uploaded but no detections in the index "
                     "(FTPdetectinfo not arriving):")
        for item in groups["index_gap"]:
            loc = cam_location.get(item["cam"], "")
            lines.append(f"  {_e(item['cam'])} ({_e(loc)}): {item['clips']} clips, 0 rows")
    if groups["quiet"]:
        lines.append(f"No detections, no clips: {_e(', '.join(groups['quiet']))}")
    if groups["offline"]:
        lines.append(f"Not reporting: {_e(', '.join(groups['offline']))}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Media resolution (media_pointers index + storage-box archive)
# ---------------------------------------------------------------------------

def night_stacks(night_id: str, cams: list[str]) -> dict[str, Path]:
    """cam -> whole-night stack path, for the cameras that actually have one.

    Read from ``media_pointers`` (kind ``nightstack``) instead of globbing
    ``<cam>/<date>/timelapse/``: cameras that never produced a timelapse dir
    used to come back "MISSING" from a glob with no way to tell a missing upload
    from a wrong path.
    """
    con = _open_index_ro()
    if con is None or not cams:
        return {}
    try:
        ph = ",".join("?" * len(cams))
        rows = con.execute(
            f"SELECT cam, filename FROM media_pointers "
            f"WHERE date=? AND kind='nightstack' AND cam IN ({ph})",
            [night_id, *cams],
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("night_stacks failed: %s", exc)
        return {}
    finally:
        con.close()

    out: dict[str, Path] = {}
    for cam, filename in rows:
        p = cache_store.ARCHIVE_PATH / cam / night_id / "timelapse" / filename
        if p.exists():
            out[cam] = p
    return out


def find_clip(cam: str, night_id: str, detection: dict[str, Any],
              window_s: float) -> Path | None:
    """The colour clip whose ``meteor_time`` is closest to *detection*.

    The FF filename's timestamp is the start of the FF block, not the meteor, so
    it does not appear in the clip's filename. ``media_pointers.extra`` carries
    the clip's own ``meteor_time``, which does line up.
    """
    when = detection.get("time_utc")
    if not when:
        return None
    try:
        target = datetime.fromisoformat(when)
    except ValueError:
        return None
    if target.tzinfo is not None:
        target = target.astimezone(timezone.utc).replace(tzinfo=None)

    con = _open_index_ro()
    if con is None:
        return None
    try:
        rows = con.execute(
            "SELECT filename, extra FROM media_pointers "
            "WHERE date=? AND cam=? AND kind='clip'",
            (night_id, cam),
        ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("find_clip failed: %s", exc)
        return None
    finally:
        con.close()

    best: tuple[float, str] | None = None
    for filename, extra in rows:
        try:
            meteor_time = (json.loads(extra or "{}") or {}).get("meteor_time")
        except (TypeError, ValueError):
            meteor_time = None
        if not meteor_time:
            continue
        try:
            t = datetime.fromisoformat(meteor_time)
        except ValueError:
            continue
        if t.tzinfo is not None:
            t = t.astimezone(timezone.utc).replace(tzinfo=None)
        delta = abs((t - target).total_seconds())
        if delta <= window_s and (best is None or delta < best[0]):
            best = (delta, filename)
    if best is None:
        return None

    d = cache_store.ARCHIVE_PATH / cam / night_id / "meteors"
    stem = Path(best[1]).stem
    # Prefer the already-remuxed mp4 the archive keeps beside the mkv.
    for candidate in (d / f"{stem}.mp4", d / best[1]):
        if candidate.exists():
            return candidate
    return None


def webp_to_jpg(src: Path, dst: Path) -> bool:
    """Telegram sendPhoto is unreliable with WebP; convert to JPEG first."""
    try:
        from PIL import Image
        with Image.open(src) as im:
            im.convert("RGB").save(dst, "JPEG", quality=90)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("webp->jpg failed for %s: %s", src, exc)
        return False


def mkv_to_mp4(src: Path, dst: Path) -> bool:
    """Remux to MP4 (H.264) so Telegram plays it inline. Copy streams if possible."""
    if not shutil.which("ffmpeg"):
        return False
    try:
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-c", "copy", "-movflags",
             "+faststart", str(dst)],
            capture_output=True, timeout=180,
        )
        if r.returncode == 0 and dst.exists() and dst.stat().st_size > 0:
            return True
        # Fall back to a re-encode if stream copy produced nothing usable.
        r = subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-fps_mode", "passthrough",
             "-c:v", "libx264", "-preset",
             "veryfast", "-movflags", "+faststart", "-an", str(dst)],
            capture_output=True, timeout=300,
        )
        return r.returncode == 0 and dst.exists() and dst.stat().st_size > 0
    except (subprocess.SubprocessError, OSError) as exc:
        logger.warning("mkv->mp4 failed for %s: %s", src, exc)
        return False


# ---------------------------------------------------------------------------
# Telegram
# ---------------------------------------------------------------------------

# Telegram rejects a sendMessage over 4096 characters outright. A complete post
# lists every camera, so on a busy night it can cross that -- and a rejected post
# is worse than a split one, since the whole night would be lost. Split on line
# boundaries, well under the limit to leave room for the HTML entities Telegram
# counts.
MESSAGE_LIMIT = 3800


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split *text* into <= *limit* char chunks, breaking only between lines.

    A single line longer than *limit* is emitted whole rather than cut mid-tag:
    a broken <b> would make Telegram reject the chunk, and no line this module
    builds comes close to the limit on its own.
    """
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.split("\n"):
        add = len(line) + (1 if current else 0)
        if current and size + add > limit:
            chunks.append("\n".join(current))
            current, size = [line], len(line)
            continue
        current.append(line)
        size += add
    if current:
        chunks.append("\n".join(current))
    return chunks


class Telegram:
    """Minimal Telegram client that reports whether a send actually landed.

    Every send checks the HTTP result and honours ``retry_after`` on 429. The
    previous version discarded the response object, so a rejected post (a bad
    parse_mode, a caption over the limit, a rate limit) was indistinguishable
    from a delivered one and the run still wrote its sent-marker.
    """

    def __init__(self, token: str, chat_ids: list[str], *, pause: float = 1.5):
        self.token = token
        self.chat_ids = chat_ids
        self.pause = pause

    def _url(self, method: str) -> str:
        return f"{TELEGRAM_API}/bot{self.token}/{method}"

    def _post(self, method: str, chat_id: str, *, data: dict[str, Any],
              files: dict[str, Any] | None = None, timeout: int = 60,
              attempts: int = 3) -> bool:
        for attempt in range(1, attempts + 1):
            if files:
                # A retry has to re-open the handles; a consumed file object
                # uploads zero bytes.
                handles = {k: open(v, "rb") for k, v in files.items()}
            else:
                handles = None
            try:
                resp = requests.post(self._url(method), data=data,
                                     files=handles, timeout=timeout)
            except requests.RequestException as exc:
                logger.warning("%s to %s failed (attempt %d/%d): %s",
                               method, chat_id, attempt, attempts, exc)
                time.sleep(self.pause * attempt)
                continue
            finally:
                for fh in (handles or {}).values():
                    fh.close()

            if resp.status_code == 200:
                return True
            if resp.status_code == 429:
                wait = self.pause * attempt
                try:
                    wait = float(resp.json()["parameters"]["retry_after"]) + 1.0
                except (ValueError, KeyError, TypeError):
                    pass
                logger.warning("%s rate limited, waiting %.0fs", method, wait)
                time.sleep(wait)
                continue
            logger.error("%s to %s rejected: HTTP %s %s",
                         method, chat_id, resp.status_code, resp.text[:300])
            return False
        return False

    def send_message(self, text: str, chat_ids: list[str] | None = None) -> bool:
        ok = True
        for chat_id in (chat_ids if chat_ids is not None else self.chat_ids):
            for chunk in split_message(text):
                time.sleep(self.pause)
                ok &= self._post("sendMessage", chat_id, data={
                    "chat_id": chat_id, "text": chunk, "parse_mode": "HTML",
                    "disable_web_page_preview": True,
                }, timeout=30)
        return ok

    def send_photo_album(self, photos: list[tuple[Path, str]]) -> bool:
        """Post photos as albums instead of one message each.

        15 separate ``sendPhoto`` calls meant 15 notifications a night in a
        channel whose whole content is one summary.
        """
        ok = True
        for chat_id in self.chat_ids:
            for i in range(0, len(photos), MEDIA_GROUP_MAX):
                chunk = photos[i:i + MEDIA_GROUP_MAX]
                media = []
                files = {}
                for n, (path, caption) in enumerate(chunk):
                    key = f"photo{n}"
                    files[key] = path
                    item: dict[str, Any] = {"type": "photo", "media": f"attach://{key}"}
                    if caption:
                        item["caption"] = caption
                        item["parse_mode"] = "HTML"
                    media.append(item)
                time.sleep(self.pause)
                ok &= self._post("sendMediaGroup", chat_id, data={
                    "chat_id": chat_id, "media": json.dumps(media),
                }, files=files, timeout=180)
        return ok

    def send_video(self, path: Path, caption: str) -> bool:
        ok = True
        for chat_id in self.chat_ids:
            time.sleep(self.pause)
            ok &= self._post("sendVideo", chat_id, data={
                "chat_id": chat_id, "caption": caption, "parse_mode": "HTML",
                "supports_streaming": True,
            }, files={"video": path}, timeout=300)
        return ok


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def collect_media(night: dict[str, Any], groups: dict[str, Any],
                  cam_location: dict[str, str], *, send_clip: bool,
                  max_stacks: int, window_s: float,
                  workdir: Path) -> tuple[list[tuple[Path, str]], tuple[Path, str] | None]:
    """Resolve and prepare the night's media. Returns (photos, clip)."""
    photos: list[tuple[Path, str]] = []
    cams = [item["cam"] for item in groups["reported"]]
    stacks = night_stacks(night["night_id"], cams)
    for item in groups["reported"]:
        if len(photos) >= max_stacks:
            break
        cam = item["cam"]
        src = stacks.get(cam)
        if not src:
            continue
        jpg = workdir / f"{cam}_{night['night_id']}_stack.jpg"
        if not webp_to_jpg(src, jpg):
            continue
        loc = cam_location.get(cam, cam)
        photos.append((jpg, f"{loc} ({cam}) — {item['count']} detecții"))

    clip: tuple[Path, str] | None = None
    if send_clip:
        # The brightest fireball if there was one, else the brightest meteor.
        target = night["fireballs"][0] if night["fireballs"] else night["brightest"]
        if target:
            found = find_clip(target["cam"], night["night_id"], target, window_s)
            if found:
                if found.suffix.lower() == ".mp4":
                    mp4: Path | None = found
                else:
                    candidate = workdir / f"brightest_{found.stem}.mp4"
                    mp4 = candidate if mkv_to_mp4(found, candidate) else None
                if mp4:
                    loc = cam_location.get(target["cam"], target["cam"])
                    caption = (f"{loc} ({target['cam']}) — mag "
                               f"{target['mag']:+.1f} · {shower_ro(target.get('shower'))}")
                    clip = (mp4, caption)
            else:
                logger.info("no clip within %.0fs of the brightest detection on %s",
                            window_s, target["cam"])
    return photos, clip


def run(night_id: str, *, dry_run: bool, force: bool, send_clip: bool) -> int:
    config_path = dashboard_config_path()
    if not config_path.exists():
        logger.error("config not found: %s", config_path)
        return 2

    station_cams, cam_location, commissioning = load_stations(config_path)
    all_cams = {c for cams in station_cams.values() for c in cams}
    if not all_cams:
        logger.error("no cameras matching %s in registry %s",
                     cam_prefixes(), config_path)
        return 2

    if not detection_db.is_ready(detection_db.DB_PATH):
        logger.error("detection index not ready at %s -- nothing to post",
                     detection_db.DB_PATH)
        return 2

    fireball_mag = _env_float("ROVIMEN_NIGHTLY_FIREBALL_MAG", DEFAULT_FIREBALL_MAG)
    stale_hours = _env_float("ROVIMEN_NIGHTLY_STALE_HOURS", 30.0)
    max_stacks = _env_int("ROVIMEN_NIGHTLY_MAX_STACKS", 30)
    window_s = _env_float("ROVIMEN_NIGHTLY_CLIP_WINDOW_S", 30.0)

    night = collect_night(night_id, all_cams, fireball_mag)
    groups = classify(night, station_cams, commissioning, stale_hours)
    message = build_message(night, groups, cam_location, fireball_mag)
    ops = build_ops_appendix(groups, cam_location)

    logger.info(
        "night %s: %d detections, %d cams reported, %d index-gap, %d quiet, %d offline",
        night_id, night["total"], len(groups["reported"]),
        len(groups["index_gap"]), len(groups["quiet"]), len(groups["offline"]),
    )

    if dry_run:
        with tempfile.TemporaryDirectory(prefix="nightly_tg_") as tmp:
            photos, clip = collect_media(
                night, groups, cam_location, send_clip=send_clip,
                max_stacks=max_stacks, window_s=window_s, workdir=Path(tmp),
            )
            print("=== nightly_telegram DRY RUN (public post) ===")
            print(message)
            if ops:
                print("\n=== operator appendix ===")
                print(ops)
            print(f"\n=== media: {len(photos)} stack(s), "
                  f"clip={'yes' if clip else 'no'} ===")
            for _p, caption in photos:
                print(f"  photo: {caption}")
            if clip:
                print(f"  video: {clip[1]}")
            missing = [i["cam"] for i in groups["reported"]
                       if i["cam"] not in night_stacks(night_id, [i["cam"]])]
            if missing:
                print(f"  no night stack for: {', '.join(missing)}")
        return 0

    state_dir = _state_dir()
    marker = state_dir / f"{night_id}.sent"
    if marker.exists() and not force:
        logger.info("already posted for %s (%s); use --force to repost",
                    night_id, marker)
        return 0

    token = _env("ROVIMEN_TELEGRAM_BOT_TOKEN")
    ro_chat = _env("ROVIMEN_TELEGRAM_RO_CHAT_ID")
    if not token or not ro_chat:
        logger.error("ROVIMEN_TELEGRAM_BOT_TOKEN and ROVIMEN_TELEGRAM_RO_CHAT_ID required")
        return 2
    ops_chat = _env("ROVIMEN_TELEGRAM_CHAT_ID")
    chat_ids = [ro_chat]
    if ops_chat and ops_chat != ro_chat:
        chat_ids.append(ops_chat)

    tg = Telegram(token, chat_ids)
    if not tg.send_message(message):
        logger.error("the summary itself did not send; not writing a sent-marker")
        return 1

    with tempfile.TemporaryDirectory(prefix="nightly_tg_") as tmp:
        photos, clip = collect_media(
            night, groups, cam_location, send_clip=send_clip,
            max_stacks=max_stacks, window_s=window_s, workdir=Path(tmp),
        )
        if photos:
            tg.send_photo_album(photos)
        if clip:
            tg.send_video(*clip)

    # Ops detail goes only to the operator chat -- a public channel does not
    # want a nightly list of camera codes that need attention.
    if ops:
        if ops_chat:
            tg.send_message(ops, chat_ids=[ops_chat])
        else:
            # No operator chat is configured on this deployment, so the appendix
            # has nowhere to go -- but it is the only place the index-gap
            # cameras are named, and dropping it silently means nobody ever
            # learns which cameras stopped landing detections. The journal is
            # the fallback destination, not /dev/null.
            logger.warning(
                "no ROVIMEN_TELEGRAM_CHAT_ID configured; operator detail only "
                "in this log:\n%s", _strip_html(ops))

    state_dir.mkdir(parents=True, exist_ok=True)
    marker.write_text(datetime.now(timezone.utc).isoformat())
    logger.info("posted nightly summary for %s to %d chat(s)", night_id, len(chat_ids))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Post the nightly ROVIMEN meteor summary to Telegram.")
    parser.add_argument("night", nargs="?", help="night id YYYYMMDD (default: last night)")
    parser.add_argument("--dry-run", action="store_true", help="compute and print, do not post")
    parser.add_argument("--force", action="store_true", help="repost even if already sent")
    parser.add_argument("--no-clip", action="store_true", help="skip the brightest-clip video")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    night_id = args.night or default_night_id()
    if not (len(night_id) == 8 and night_id.isdigit()):
        parser.error(f"invalid night id {night_id!r} (expected YYYYMMDD)")

    send_clip = not args.no_clip and _env("ROVIMEN_NIGHTLY_SEND_CLIP", "1") != "0"
    return run(night_id, dry_run=args.dry_run, force=args.force, send_clip=send_clip)


if __name__ == "__main__":
    sys.exit(main())
