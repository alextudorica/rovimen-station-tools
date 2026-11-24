#!/usr/bin/env python3
"""VPS disk monitor -- durable, low-noise alerting for the three storage
surfaces that filled to 100% in July 2026.

Run every 30 min by ``rovimen-disk-monitor.timer``. For each watched volume it:
  - logs a WARNING to the journal when usage is over its threshold,
  - writes the full picture to ``ROVIMEN_DISK_STATUS_PATH`` (a JSON the
    dashboard can surface),
  - sends a Telegram alert ONLY on a fresh threshold *crossing* (over, or
    recovered), and ONLY to an operator chat (``ROVIMEN_TELEGRAM_CHAT_ID``) --
    never the public channel -- so it can't spam.

The thumb-cache SSD and the Storage Box are mount points. When one is not
mounted its path is a plain directory on root, so ``disk_usage`` there reads
root's usage and would report a full box as "recovered". Such a volume is not
measured: its last over-threshold flag is kept, and one "not mounted" alert goes
out per outage, then one "mounted again" when it returns.

Stdlib only. Thresholds are percent-used and env-overridable.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("disk_monitor")

STATUS_PATH = os.environ.get("ROVIMEN_DISK_STATUS_PATH", "/opt/rovimen/vps_disk_status.json")

# (label, path, percent-used alert threshold, path must be a mount point)
VOLUMES = [
    ("root", "/", int(os.environ.get("ROVIMEN_DISK_ROOT_PCT", "88")), False),
    ("thumb_cache_ssd", os.environ.get("THUMB_CACHE_DIR", "/opt/rovimen/thumb_cache"),
     int(os.environ.get("ROVIMEN_DISK_SSD_PCT", "88")), True),
    ("storage_box", os.environ.get("ROVIMEN_ARCHIVE_PATH", "/srv/rovimen/archive"),
     int(os.environ.get("ROVIMEN_DISK_BOX_PCT", "90")), True),
]


def usage(path: str) -> dict | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    pct = round(100.0 * u.used / u.total, 1) if u.total else 0.0
    return {"total_gb": round(u.total / 1e9, 1), "free_gb": round(u.free / 1e9, 1),
            "used_pct": pct}


def _telegram(text: str) -> None:
    token = os.environ.get("ROVIMEN_TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("ROVIMEN_TELEGRAM_CHAT_ID")  # operator chat only
    if not token or not chat:
        return
    try:
        data = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        urllib.request.urlopen(
            f"https://api.telegram.org/bot{token}/sendMessage", data=data, timeout=10)
    except Exception as exc:  # best-effort; never fail the run on a network blip
        logger.warning("telegram alert failed: %s", exc)


def _load_prev() -> dict:
    try:
        return json.loads(open(STATUS_PATH).read())
    except (OSError, ValueError):
        return {}


def main() -> int:
    prev = _load_prev()
    prev_over = prev.get("over", {})
    prev_unmounted = prev.get("unmounted", {})
    status = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
              "volumes": {}, "over": {}, "unmounted": {}}
    crossings: list[str] = []
    for label, path, thr, must_be_mount in VOLUMES:
        if must_be_mount and not os.path.ismount(path):
            # Unmounted (or a dead sshfs): the path would read root's usage.
            logger.warning("%s not mounted at %s; usage not checked", label, path)
            status["unmounted"][label] = True
            status["volumes"][label] = {"mounted": False, "threshold_pct": thr}
            if label in prev_over:
                status["over"][label] = prev_over[label]
            if not prev_unmounted.get(label):
                crossings.append(f"{label} not mounted at {path}; usage not checked "
                                 f"until it is back")
            continue
        if must_be_mount and prev_unmounted.get(label):
            crossings.append(f"{label} mounted again at {path}")
        u = usage(path)
        if u is None:
            continue
        over = u["used_pct"] >= thr
        status["volumes"][label] = {**u, "threshold_pct": thr, "over": over}
        status["over"][label] = over
        if over:
            logger.warning("%s at %.1f%% used (>=%d%% threshold, %.1f GB free)",
                           label, u["used_pct"], thr, u["free_gb"])
        was_over = bool(prev_over.get(label))
        if over and not was_over:
            crossings.append(f"⚠️ {label} {u['used_pct']:.1f}% used "
                             f"({u['free_gb']:.0f} GB free) -- over {thr}%")
        elif was_over and not over:
            crossings.append(f"✅ {label} recovered to {u['used_pct']:.1f}% used")
    try:
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(status, fh, indent=2)
        os.replace(tmp, STATUS_PATH)
    except OSError as exc:
        logger.warning("could not write status file: %s", exc)
    if crossings:
        _telegram("VPS disk:\n" + "\n".join(crossings))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
