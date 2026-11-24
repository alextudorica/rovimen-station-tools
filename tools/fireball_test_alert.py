#!/usr/bin/env python3
"""Send TEST fireball alerts through every configured channel, with the live routing.

Builds the same Broadcaster the alert daemon uses (dashboard/alert_channels.py)
from the same settings, then sends one clearly marked TEST message per kind:
a preliminary alert (private Telegram only), a confirmed alert, its ground
track maps (real maps from dashboard/fireball_map.py) and a clips message.
Each channel gets only the kinds its route lists, so the result shows the
routing as well as the delivery.

On the VPS, as root (reads the same env files as the service):
    /opt/rovimen/venv/bin/python /opt/rovimen/fireball_test_alert.py --list
    /opt/rovimen/venv/bin/python /opt/rovimen/fireball_test_alert.py [--only discord,email] [--kinds confirmed,clips]

--list prints the channels and their routes and sends nothing.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
for cand in (HERE, HERE.parent / "dashboard"):  # VPS: flat /opt/rovimen; repo: tools/
    if (cand / "alert_channels.py").exists():
        sys.path.insert(0, str(cand))
        break

import alert_channels  # noqa: E402

ENV_FILES = ("/etc/rovimen-dashboard.env", "/etc/rovimen-fireball.env", "/etc/rovimen-mail.env")
DEFAULT_KINDS = ("preliminary", "confirmed", "confirmed_map", "clips")


def load_env_files(paths: list[str]) -> None:
    for p in paths:
        for k, v in alert_channels.read_env_file(Path(p)).items():
            os.environ.setdefault(k, v)


def test_maps() -> list[tuple[str, bytes]]:
    """Two real track maps (fireball_map) over Romania; [] if rendering fails."""
    try:
        import fireball_map
        tracks = [{"label": "TEST camera A", "cam": "TEST1", "begin": (45.80, 25.40), "end": (45.55, 25.95),
                   "h_begin": 95.0, "h_end": 32.0}]
        cameras = [("TEST camera A", 45.30, 25.00), ("TEST camera B", 46.10, 26.40)]
        title = "TEST, not a real fireball\nground track drawn from made-up numbers"
        return [("<b>TEST</b> confirmed ground track (made-up numbers)",
                 fireball_map.render_track_map(tracks, cameras, title, overview=True)),
                ("<b>TEST</b> close-up of the trail", fireball_map.render_track_map(tracks, cameras, title))]
    except Exception as exc:
        print(f"map rendering failed ({exc}); sending no maps")
        return []


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--only", default="", help="comma-separated channel names (default: every configured one)")
    ap.add_argument("--kinds", default=",".join(DEFAULT_KINDS), help=f"default {','.join(DEFAULT_KINDS)}")
    ap.add_argument("--env-file", action="append", default=None,
                    help=f"env file(s) to load (default: {', '.join(ENV_FILES)})")
    ap.add_argument("--list", action="store_true", help="print channels and routes, send nothing")
    a = ap.parse_args(argv)

    load_env_files(a.env_file or list(ENV_FILES))
    only = {c.strip() for c in a.only.split(",") if c.strip()} or None
    b = alert_channels.broadcaster_from_env(only=only)
    if not b.workers:
        print("no channel configured (check the env files)")
        return 1
    for w in b.workers:
        print(f"{w.channel.name:20s} {' '.join(w.kinds)}")
        if w.channel.name == "email":
            print(f"{'':20s} recipients: {len(w.channel.recipients())}")
    if a.list:
        return 0

    kinds = alert_channels.parse_kinds(a.kinds.replace(",", " "))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    head = "<b>TEST</b>, not a real fireball: checking the alert channels"
    texts = {
        "preliminary": f"<b>Fireball alert (preliminary)</b>\n{now} UTC\n{head}. Private chat only.",
        "confirmed": f"<b>Fireball confirmed</b>, {now} UTC\n{head}.\nIn a real alert this lists the cameras, "
                     "heights, speed and ground track.",
        "retracted": f"<b>Alert retracted</b>, {now} UTC\n{head}. Private chat only.",
        "withdrawn": f"<b>Correction: false alarm</b>, {now} UTC\n{head}.\nIn a real withdrawal: the confirmed "
                     "alert taken back, and why.",
        "pulled": f"<b>Fireball data pulled</b>, {now} UTC\n{head}. Private chat only.",
        "clips": f"<b>Fireball clips</b>, TEST\n{head}.\nIn a real alert: one link per camera.",
        "solution": f"<b>Automated trajectory solution</b>, TEST\n{head}.",
        "test": f"{head}.",
    }
    for kind in kinds:
        if kind in ("confirmed_map", "preliminary_map"):
            maps = test_maps()
            sent = b.send_photos(maps, kind) if maps else False
        elif kind == "thumbs":
            maps = test_maps()[:1]
            sent = b.send_photos(maps, kind) if maps else False
        else:
            sent = b.send(texts[kind], kind)
        targets = [w.channel.name for w in b.workers if kind in w.kinds]
        print(f"{kind:16s} -> {', '.join(targets) if sent else 'no channel'}")
    ok = b.flush(timeout=180)
    print("all queued messages handed over" if ok else "timed out waiting for a channel")
    print("Check each channel; send errors are logged above.")
    return 0 if ok else 1


if __name__ == "__main__":
    import logging
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    sys.exit(main())
