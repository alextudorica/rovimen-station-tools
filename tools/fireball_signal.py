#!/usr/bin/env python3
"""Receive live fireball alerts from the VPS and run a local command.

Runs on a Raspberry Pi (or any Linux box) on the GMN tailnet. The VPS alert
daemon (``dashboard/fireball_alert.py``) POSTs every preliminary, confirmed and
retracted alert here as JSON, with the shared secret in the
``X-Rovimen-Signal`` header. Each accepted alert starts ``--cmd`` once, in the
background, with the alert in its environment:

  FIREBALL_KIND      preliminary | confirmed | retracted
  FIREBALL_ID        event id, the begin time UTC (YYYYMMDD_HHMMSS)
  FIREBALL_UTC       event time, ISO 8601
  FIREBALL_CAMERAS   camera codes, space separated
  FIREBALL_STATIONS  stations with places, "; " separated
  FIREBALL_TEXT      the Telegram text without markup

A preliminary arrives about 10 s after the fireball and its confirmed or
retracted update about 15 s later, so a command that switches an LED on for
preliminary/confirmed can switch it off again on retracted.

Stdlib only. Example, an LED on GPIO 17 for two minutes (Raspberry Pi OS):

  python3 fireball_signal.py --secret-file ~/.fireball_signal_secret \\
      --cmd 'case "$FIREBALL_KIND" in retracted) pinctrl set 17 op dl;; \\
             *) pinctrl set 17 op dh; sleep 120; pinctrl set 17 op dl;; esac'

``--test`` runs the command once with a fake preliminary alert and exits.

Telegram mode (``--telegram-token-file``): for a receiver the VPS cannot
reach (the tailnet ACL allows the VPS only :7779 on a station), the alert
daemon also posts each alert as one ``FIREBALL_SIGNAL {json}`` line to a
Telegram group (``ROVIMEN_FIREBALL_SIGNAL_TELEGRAM_CHAT_IDS`` on the VPS).
Make it a channel with both bots as admins: Telegram never delivers one
bot's messages to another bot in a group, but every admin bot gets every
channel post. This receiver long-polls its own bot and runs ``--cmd`` for
signal lines posted in ``--telegram-chat`` (in a channel only admins can
post; in a group the line must come from the bot named in
``--telegram-from``). Only outbound HTTPS, no listening port. Alerts posted
while it was down are not replayed.
"""

from __future__ import annotations

import argparse
import hmac
import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

logger = logging.getLogger("fireball_signal")

MAX_BODY = 64 * 1024
KINDS = ("preliminary", "confirmed", "retracted")
SIGNAL_LINE_PREFIX = "FIREBALL_SIGNAL "   # dashboard/fireball_alert.py posts it
TELEGRAM_API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50


def alert_env(alert: dict[str, Any]) -> dict[str, str]:
    """The alert as FIREBALL_* variables on top of this process's environment."""
    env = dict(os.environ)
    env.update({
        "FIREBALL_KIND": str(alert.get("kind", "")),
        "FIREBALL_ID": str(alert.get("id", "")),
        "FIREBALL_UTC": str(alert.get("utc", "")),
        "FIREBALL_CAMERAS": " ".join(str(c) for c in alert.get("cameras") or []),
        "FIREBALL_STATIONS": "; ".join(str(s) for s in alert.get("stations") or []),
        "FIREBALL_TEXT": str(alert.get("text", "")),
    })
    return env


def run_command(cmd: str, alert: dict[str, Any]) -> subprocess.Popen:
    logger.info("%s %s: running command", alert.get("kind"), alert.get("id"))
    return subprocess.Popen(["/bin/sh", "-c", cmd], env=alert_env(alert))


def make_handler(secret: str, cmd: str, kinds: set[str]) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 (http.server API)
            if not hmac.compare_digest(self.headers.get("X-Rovimen-Signal", ""), secret):
                self._reply(403)
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._reply(400)
                return
            try:
                alert = json.loads(self.rfile.read(length))
            except ValueError:
                self._reply(400)
                return
            if not isinstance(alert, dict) or alert.get("kind") not in KINDS:
                self._reply(400)
                return
            if alert["kind"] in kinds:
                run_command(cmd, alert)
            self._reply(204)

        def _reply(self, code: int) -> None:
            self.send_response(code)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.info("%s %s", self.address_string(), fmt % args)

    return Handler


def alert_from_update(update: dict[str, Any], chat_id: str, from_bot: str) -> dict[str, Any] | None:
    """The alert in one getUpdates entry, or None if it is not a signal line
    posted in *chat_id* by the bot *from_bot* (username, without @)."""
    if "channel_post" in update:
        # Only a channel's admins can post in it, so the chat id is the check.
        msg = update["channel_post"]
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            return None
    else:
        msg = update.get("message") or {}
        sender = msg.get("from") or {}
        if str((msg.get("chat") or {}).get("id")) != str(chat_id):
            return None
        if not sender.get("is_bot") or (sender.get("username") or "").lower() != from_bot.lower().lstrip("@"):
            return None
    text = msg.get("text") or ""
    if not text.startswith(SIGNAL_LINE_PREFIX):
        return None
    try:
        alert = json.loads(text[len(SIGNAL_LINE_PREFIX):])
    except ValueError:
        return None
    if not isinstance(alert, dict) or alert.get("kind") not in KINDS:
        return None
    return alert


def _get_updates(token: str, offset: int | None, timeout: int) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"timeout": timeout, "allowed_updates": json.dumps(["message", "channel_post"])}
    if offset is not None:
        params["offset"] = offset
    url = f"{TELEGRAM_API}/bot{token}/getUpdates?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=timeout + 15) as r:
        data = json.loads(r.read())
    if not data.get("ok"):
        raise RuntimeError(f"getUpdates: {data.get('description')}")
    return data.get("result") or []


def poll_telegram(token: str, chat_id: str, from_bot: str, cmd: str, kinds: set[str],
                  get_updates=_get_updates, sleep=time.sleep, max_loops: int | None = None) -> None:
    """Long-poll the bot and run *cmd* for each signal line. Skips the backlog
    at start, so a restart never replays an old alert."""
    offset: int | None = None
    try:
        backlog = get_updates(token, -1, 0)
        if backlog:
            offset = backlog[-1]["update_id"] + 1
    except Exception as exc:
        logger.warning("could not skip the update backlog: %s", exc)
    logger.info("polling Telegram chat %s for signals from @%s (%s)",
                chat_id, from_bot.lstrip("@"), ", ".join(sorted(kinds)))
    backoff = 5.0
    loops = 0
    while max_loops is None or loops < max_loops:
        loops += 1
        try:
            updates = get_updates(token, offset, POLL_TIMEOUT_S)
            backoff = 5.0
        except Exception as exc:
            logger.warning("getUpdates failed (%s), retrying in %.0fs", exc, backoff)
            sleep(backoff)
            backoff = min(backoff * 2, 300.0)
            continue
        for update in updates:
            offset = update["update_id"] + 1
            alert = alert_from_update(update, chat_id, from_bot)
            if alert is not None and alert["kind"] in kinds:
                run_command(cmd, alert)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--cmd", required=True, help="shell command run for each alert (FIREBALL_* in its env)")
    ap.add_argument("--secret-file", type=Path, help="file holding the shared secret (else $FIREBALL_SIGNAL_SECRET)")
    ap.add_argument("--bind", default="0.0.0.0", help="address to listen on (default all)")
    ap.add_argument("--port", type=int, default=8791)
    ap.add_argument("--kinds", default=",".join(KINDS), help="alert kinds that run the command")
    ap.add_argument("--test", action="store_true", help="run the command once with a fake alert and exit")
    ap.add_argument("--telegram-token-file", type=Path,
                    help="poll this bot's Telegram updates instead of listening for POSTs")
    ap.add_argument("--telegram-chat", help="the signal group's chat id (Telegram mode)")
    ap.add_argument("--telegram-from", help="username of the alert bot whose lines count (Telegram mode)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.test:
        fake = {"kind": "preliminary", "id": "20261021_201634", "utc": "2026-10-21T20:16:34Z",
                "cameras": ["RO0003", "RO000P"], "stations": ["gmnro05 (Bistrița)", "gmnro06 (Cluj)"],
                "text": "TEST: fake fireball alert"}
        return run_command(args.cmd, fake).wait()

    kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    if args.telegram_token_file:
        if not (args.telegram_chat and args.telegram_from):
            ap.error("Telegram mode needs --telegram-chat and --telegram-from")
        try:
            poll_telegram(args.telegram_token_file.read_text().strip(), args.telegram_chat,
                          args.telegram_from, args.cmd, kinds)
        except KeyboardInterrupt:
            pass
        return 0

    secret = (args.secret_file.read_text().strip() if args.secret_file
              else os.environ.get("FIREBALL_SIGNAL_SECRET", "").strip())
    if not secret:
        ap.error("no secret: pass --secret-file or set FIREBALL_SIGNAL_SECRET")
    server = ThreadingHTTPServer((args.bind, args.port), make_handler(secret, args.cmd, kinds))
    logger.info("listening on %s:%d for %s", args.bind, args.port, ", ".join(sorted(kinds)))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
