"""Fireball alert channels: Telegram, Discord and email, with routing per message kind.

The alert daemon (:mod:`fireball_alert`) hands every message to a
:class:`Broadcaster` together with its kind. Each configured channel gets the
kinds its route lists, through its own queue and worker thread, so a slow
SMTP server never holds up the alert loop and each channel keeps the order of
its messages. A send that fails for now (HTTP 429 or 5xx, a timeout or
connection error, an SMTP error) is retried up to SEND_RETRIES times, after
Telegram's or Discord's ``retry_after`` on a 429 and with backoff otherwise;
the channel's later messages wait behind it, the other channels do not.

Kinds:
    preliminary, preliminary_map  first trigger, and its ground-track maps
    confirmed, confirmed_map      the confirmation 15 s later, and its maps
    retracted                     a preliminary that did not hold
    withdrawn                     a confirmed alert taken back (no FR file in its own cameras' packs, or by hand)
    pulled                        data-pull summary (paths, failures: internal)
    thumbs                        FF maxpixel thumbnails from the pulled packs
    clips                         links to the colour clips once archived
    solution                      the automated trajectory solution
    test                          tools/fireball_test_alert.py

Channels and the kinds they get by default (``ROVIMEN_FIREBALL_ROUTE_<NAME>``,
space or comma separated kinds or ``*``, overrides one):
    telegram            the maintainer's private chat (ROVIMEN_FIREBALL_TELEGRAM_CHAT_ID)    all
    telegram_community  the members' channel (..._TELEGRAM_COMMUNITY_CHAT_ID)       COMMUNITY
    discord             a webhook (ROVIMEN_FIREBALL_DISCORD_WEBHOOK_URL)            COMMUNITY
    email               Gmail SMTP to the subscribers                               EMAIL

Preliminary and retracted alerts stay private (a withdrawal goes wherever the confirmation went): one false preliminary
(2026-10-03 20:49, an RO0003 (Bistrița) light step) would otherwise have gone
to everyone.

Settings come from the environment, then /etc/rovimen-fireball.env, then
/etc/rovimen-mail.env (both root:rovimen 0640; see :func:`load_settings`), so
the dashboard, whose unit loads neither, can show the join links and send a
subscription confirmation. Email: SMTP with STARTTLS (default smtp.gmail.com:587; Hetzner blocks 465)
with ROVIMEN_SMTP_USER and ROVIMEN_SMTP_PASSWORD (a Gmail app password). Recipients:
every ``alert_email`` in users.yaml (set on the dashboard's /alerts page) plus
ROVIMEN_FIREBALL_EMAIL_TO, all in Bcc.
"""

from __future__ import annotations

import html
import json
import logging
import os
import queue
import re
import smtplib
import ssl
import threading
import time
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Any, Callable, Protocol

logger = logging.getLogger("alert_channels")

KINDS = ("preliminary", "preliminary_map", "confirmed", "confirmed_map", "retracted",
         "withdrawn", "pulled", "thumbs", "clips", "solution", "test")
COMMUNITY = ("confirmed", "confirmed_map", "withdrawn", "thumbs", "clips", "solution", "test")
EMAIL = ("confirmed", "confirmed_map", "withdrawn", "clips", "solution", "test")
DEFAULT_ROUTES: dict[str, tuple[str, ...]] = {
    "telegram": KINDS,
    "telegram_community": COMMUNITY,
    "discord": COMMUNITY,
    "email": EMAIL,
}

TELEGRAM_API = "https://api.telegram.org"
DISCORD_MAX_CHARS = 2000
DISCORD_MAX_FILES = 10
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SEND_RETRIES = 3            # retries after a first failed attempt, so 4 attempts in all
RETRY_BACKOFF_S = 5.0       # waits 5, 10, 20 s between attempts ...
RETRY_AFTER_MAX_S = 300.0   # ... or the 429's retry_after + 1 s, at most this


class RetryableSendError(Exception):
    """The server did not take the message this time (HTTP 429 or 5xx);
    ``retry_after`` is the wait a 429 asked for, if it named one."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def _retry_after(r: Any) -> float | None:
    """Seconds a 429 asks to wait: Telegram's ``parameters.retry_after``, Discord's
    ``retry_after``, else the Retry-After header."""
    try:
        body = r.json()
    except Exception:
        body = None
    if isinstance(body, dict):
        params = body.get("parameters")
        for v in ((params or {}).get("retry_after") if isinstance(params, dict) else None, body.get("retry_after")):
            try:
                if v is not None:
                    return float(v)
            except (TypeError, ValueError):
                pass
    try:
        return float(getattr(r, "headers", {}).get("Retry-After"))
    except (TypeError, ValueError):
        return None


def _safe_error(exc: BaseException) -> str:
    """A send error for the log, with no secret in it: requests puts the request URL in its
    exception text, and that URL holds the Telegram bot token or the Discord webhook secret.
    So only the exception type (and an SMTP reply code) is logged, except for our own
    RetryableSendError, built from the HTTP status and the response body."""
    if isinstance(exc, RetryableSendError):
        return str(exc)
    code = getattr(exc, "smtp_code", None)
    return f"{type(exc).__name__} {code}" if code else type(exc).__name__


def _http_result(r: Any, ok: bool, what: str) -> bool:
    """True when the server took the message; RetryableSendError on 429/5xx; False
    (logged) on any other status, which a retry would not change."""
    if ok:
        return True
    if r.status_code == 429 or r.status_code >= 500:
        raise RetryableSendError(f"{what} HTTP {r.status_code}: {r.text[:200]}",
                                 _retry_after(r) if r.status_code == 429 else None)
    logger.error("%s HTTP %d: %s", what, r.status_code, r.text[:200])
    return False


class Channel(Protocol):
    name: str

    def send(self, text: str) -> bool: ...

    def send_photos(self, photos: list[tuple[str, bytes]]) -> bool: ...


def html_to_text(text: str) -> str:
    """Telegram HTML (the alert texts use <b> and entities only) to plain text."""
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def html_to_markdown(text: str) -> str:
    """Telegram HTML to Discord markdown: <b> becomes **, other tags are dropped."""
    text = re.sub(r"</?b>", "**", text)
    return html.unescape(re.sub(r"<[^>]+>", "", text))


def parse_kinds(spec: str) -> tuple[str, ...]:
    spec = spec.strip()
    if spec == "*":
        return KINDS
    kinds = tuple(k for k in re.split(r"[\s,]+", spec) if k)
    unknown = [k for k in kinds if k not in KINDS]
    if unknown:
        raise ValueError(f"unknown alert kinds: {', '.join(unknown)}")
    return kinds


# ── Telegram ──────────────────────────────────────────────────────────

class TelegramChat:
    """One Telegram chat through the Bot API (HTML parse mode)."""

    def __init__(self, name: str, token: str, chat_id: str, post: Callable[..., Any] | None = None) -> None:
        self.name = name
        self.token = token
        self.chat_id = chat_id
        self._post = post

    def _requests_post(self, *args: Any, **kwargs: Any) -> Any:
        if self._post is not None:
            return self._post(*args, **kwargs)
        import requests
        return requests.post(*args, **kwargs)

    def send(self, text: str) -> bool:
        r = self._requests_post(f"{TELEGRAM_API}/bot{self.token}/sendMessage",
                                data={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                      "disable_web_page_preview": "true"}, timeout=15)
        return _http_result(r, r.status_code == 200, f"{self.name} sendMessage")

    def send_photos(self, photos: list[tuple[str, bytes]]) -> bool:
        if not photos:
            return True
        if len(photos) == 1:
            cap, jpg = photos[0]
            r = self._requests_post(f"{TELEGRAM_API}/bot{self.token}/sendPhoto",
                                    data={"chat_id": self.chat_id, "caption": cap[:1024], "parse_mode": "HTML"},
                                    files={"photo": ("p0.jpg", jpg, "image/jpeg")}, timeout=60)
        else:
            media, files = [], {}
            for k, (cap, jpg) in enumerate(photos[:10]):
                media.append({"type": "photo", "media": f"attach://p{k}", "caption": cap[:1024],
                              "parse_mode": "HTML"})
                files[f"p{k}"] = (f"p{k}.jpg", jpg, "image/jpeg")
            r = self._requests_post(f"{TELEGRAM_API}/bot{self.token}/sendMediaGroup",
                                    data={"chat_id": self.chat_id, "media": json.dumps(media)},
                                    files=files, timeout=60)
        return _http_result(r, r.status_code == 200, f"{self.name} photos")


# ── Discord ───────────────────────────────────────────────────────────

class DiscordWebhook:
    """A Discord channel webhook: text as markdown, photos as attachments."""

    def __init__(self, url: str, username: str = "ROVIMEN fireball alerts",
                 post: Callable[..., Any] | None = None) -> None:
        self.name = "discord"
        self.url = url
        self.username = username
        self._post = post

    def _requests_post(self, *args: Any, **kwargs: Any) -> Any:
        if self._post is not None:
            return self._post(*args, **kwargs)
        import requests
        return requests.post(*args, **kwargs)

    @staticmethod
    def _clip(text: str) -> str:
        return text if len(text) <= DISCORD_MAX_CHARS else text[:DISCORD_MAX_CHARS - 1] + "…"

    def send(self, text: str) -> bool:
        body = {"content": self._clip(html_to_markdown(text)), "username": self.username,
                "allowed_mentions": {"parse": []}}
        r = self._requests_post(self.url, json=body, timeout=15)
        return _http_result(r, r.status_code < 300, "discord")

    def send_photos(self, photos: list[tuple[str, bytes]]) -> bool:
        if not photos:
            return True
        photos = photos[:DISCORD_MAX_FILES]
        content = "\n".join(f"{k + 1}. {html_to_markdown(cap)}" for k, (cap, _) in enumerate(photos))
        payload = {"content": self._clip(content), "username": self.username,
                   "allowed_mentions": {"parse": []},
                   "attachments": [{"id": k, "filename": f"p{k}.jpg"} for k in range(len(photos))]}
        files = {f"files[{k}]": (f"p{k}.jpg", jpg, "image/jpeg") for k, (_, jpg) in enumerate(photos)}
        r = self._requests_post(self.url, data={"payload_json": json.dumps(payload)}, files=files, timeout=60)
        return _http_result(r, r.status_code < 300, "discord photos")


# ── Email ─────────────────────────────────────────────────────────────

def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines (optional quotes, # comments); missing or unreadable -> {}."""
    out: dict[str, str] = {}
    try:
        text = path.read_text()
    except OSError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        out[k.strip()] = v
    return out


def load_settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    """The alert settings: the environment over ROVIMEN_FIREBALL_ENV
    (/etc/rovimen-fireball.env) over ROVIMEN_MAIL_ENV (/etc/rovimen-mail.env).
    Read at use, so the dashboard (whose unit loads neither file) sees them and
    a changed join link shows without a restart."""
    env = dict(os.environ if environ is None else environ)
    out: dict[str, str] = {}
    for key, default in (("ROVIMEN_MAIL_ENV", "/etc/rovimen-mail.env"),
                         ("ROVIMEN_FIREBALL_ENV", "/etc/rovimen-fireball.env")):
        out.update(read_env_file(Path(env.get(key, default))))
    out.update({k: v for k, v in env.items() if v})
    return out


def mail_settings(environ: dict[str, str] | None = None) -> dict[str, str]:
    """SMTP settings (see :func:`load_settings`)."""
    env = load_settings(environ)
    get = lambda k, d="": (env.get(k) or d).strip()  # noqa: E731
    return {"host": get("ROVIMEN_SMTP_HOST", "smtp.gmail.com"),
            "port": get("ROVIMEN_SMTP_PORT", "587"),
            "user": get("ROVIMEN_SMTP_USER"),
            "password": get("ROVIMEN_SMTP_PASSWORD").replace(" ", ""),
            "from_name": get("ROVIMEN_FIREBALL_EMAIL_FROM_NAME", "ROVIMEN fireball alerts"),
            "extra_to": get("ROVIMEN_FIREBALL_EMAIL_TO")}


def users_path() -> Path:
    return Path(os.environ.get("ROVIMEN_USERS_PATH", "/opt/rovimen/users.yaml"))


def subscriber_emails(path: Path | None = None) -> list[str]:
    """Every valid ``alert_email`` in users.yaml (lower-cased, unique, sorted)."""
    import yaml
    try:
        raw = yaml.safe_load((path or users_path()).read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        logger.error("cannot read subscribers from users.yaml: %s", exc)
        return []
    rows = raw.get("users", {}) if isinstance(raw, dict) else {}
    out = set()
    for row in rows.values() if isinstance(rows, dict) else []:
        addr = (row or {}).get("alert_email") if isinstance(row, dict) else None
        if isinstance(addr, str) and EMAIL_RE.match(addr.strip()):
            out.add(addr.strip().lower())
    return sorted(out)


def build_email(sender: str, from_name: str, to: list[str], subject: str, html_body: str,
                photos: list[tuple[str, bytes]] | None = None) -> EmailMessage:
    """HTML email (plain-text alternative) with photos inline. The recipients go
    only in the SMTP envelope (see EmailChannel._deliver): with the sender in To,
    the sending account got a copy of every alert (2026-11-09)."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, sender))
    msg["To"] = "undisclosed-recipients:;"
    msg.set_content(html_to_text(html_body.replace("<br>", "\n")))
    cids = [make_msgid(domain="rovimen.org") for _ in photos or []]
    figs = "".join(f'<p><img src="cid:{cid[1:-1]}" style="max-width:100%"><br>{cap}</p>'
                   for cid, (cap, _) in zip(cids, photos or []))
    msg.add_alternative(f"<html><body style=\"font-family:sans-serif\">{html_body}{figs}"
                        f"<p style=\"color:#777;font-size:12px\">ROVIMEN fireball alerts. To stop "
                        f"them, click Unsubscribe on the dashboard's Fireball alerts page.</p></body></html>",
                        subtype="html")
    if photos:
        html_part = msg.get_payload()[1]
        for cid, (_, jpg) in zip(cids, photos):
            html_part.add_related(jpg, maintype="image", subtype="jpeg", cid=cid)
    return msg


def _smtp_connect(host: str, port: int) -> smtplib.SMTP:
    """Implicit TLS on 465, STARTTLS on anything else. Hetzner Cloud blocks
    outgoing 25 and 465 (2026-11-09: 465 timed out from the VPS, 587 open),
    so the default is 587 with STARTTLS; the session is encrypted either way."""
    ctx = ssl.create_default_context()
    if port == 465:
        return smtplib.SMTP_SSL(host, port, context=ctx, timeout=30)
    smtp = smtplib.SMTP(host, port, timeout=30)
    smtp.starttls(context=ctx)
    return smtp


class EmailChannel:
    """Gmail (or any SMTP over SSL) to every subscriber, one message per alert."""

    def __init__(self, settings: dict[str, str], recipients: Callable[[], list[str]],
                 smtp_factory: Callable[..., Any] | None = None) -> None:
        self.name = "email"
        self.settings = settings
        self.recipients = recipients
        self._smtp_factory = smtp_factory

    def _deliver(self, subject: str, html_body: str, photos: list[tuple[str, bytes]] | None = None) -> bool:
        to = self.recipients()
        if not to:
            logger.info("email: no subscribers, nothing sent")
            return True
        s = self.settings
        msg = build_email(s["user"], s["from_name"], to, subject, html_body, photos)
        port = int(s["port"])
        factory = self._smtp_factory or _smtp_connect
        sent = False
        try:
            with factory(s["host"], port) as smtp:
                smtp.login(s["user"], s["password"])
                smtp.send_message(msg, to_addrs=to)
                sent = True
        except Exception as exc:
            # An error closing the session (QUIT not answered 221) comes after the server
            # took the message: report it delivered, so no retry sends it twice.
            if not sent:
                raise
            logger.warning("email: sent, but closing the SMTP session failed: %s", _safe_error(exc))
        return True

    @staticmethod
    def _subject(text: str) -> str:
        first = html_to_text(text.split("\n", 1)[0]).strip()
        return f"ROVIMEN: {first}"[:150]

    def send(self, text: str) -> bool:
        return self._deliver(self._subject(text), text.replace("\n", "<br>"))

    def send_photos(self, photos: list[tuple[str, bytes]]) -> bool:
        if not photos:
            return True
        subject = self._subject(photos[0][0])
        if len(photos) > 1:
            subject += f" (+{len(photos) - 1} images)"
        return self._deliver(subject, "", photos)


def send_confirmation_email(address: str, display_name: str = "") -> tuple[bool, str]:
    """One message telling a new subscriber they are on the list. (ok, error text)."""
    s = mail_settings()
    if not (s["user"] and s["password"]):
        return False, "email sending is not configured on the server"
    ch = EmailChannel(s, lambda: [address])
    who = html.escape(display_name or address)
    body = (f"<b>Fireball alerts by email are on</b><br>Hello {who},<br>you will get an email when the "
            f"ROVIMEN network confirms a fireball, with its ground track, the clips and the automated "
            f"solution. To stop them, click Unsubscribe on the dashboard's Fireball alerts page.")
    try:
        ch._deliver("ROVIMEN: fireball alerts by email are on", body)
        return True, ""
    except Exception as exc:
        logger.error("confirmation email to %s failed: %s", address, exc)
        return False, type(exc).__name__


# ── routing ───────────────────────────────────────────────────────────

class _Worker:
    """A channel with its own FIFO and thread: in-order, never blocking the caller."""

    def __init__(self, channel: Channel, kinds: tuple[str, ...],
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.channel = channel
        self.kinds = kinds
        self.sleep = sleep
        self.q: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self.thread = threading.Thread(target=self._run, name=f"alert-{channel.name}", daemon=True)
        self.thread.start()

    def _run(self) -> None:
        while True:
            item = self.q.get()
            try:
                if item is None:
                    return
                self._deliver(*item)
            finally:
                self.q.task_done()

    def _deliver(self, op: str, arg: Any) -> bool:
        """Send one message, retrying a 429, 5xx, timeout, connection or SMTP error
        (requests' and smtplib's exceptions are OSErrors) up to SEND_RETRIES times.
        Only this message is sent again, and the queue behind it waits, so the
        channel's order holds; other channels have their own threads. A read timeout
        after the server took the message can still show it twice."""
        for attempt in range(SEND_RETRIES + 1):
            try:
                return self.channel.send(arg) if op == "text" else self.channel.send_photos(arg)
            except (RetryableSendError, OSError) as exc:
                if attempt == SEND_RETRIES:
                    logger.error("%s: send failed %d times, dropped: %s", self.channel.name, attempt + 1,
                                 _safe_error(exc))
                    return False
                after = getattr(exc, "retry_after", None)
                wait = (min(after + 1.0, RETRY_AFTER_MAX_S) if after is not None
                        else RETRY_BACKOFF_S * 2 ** attempt)
                logger.warning("%s: send failed (%s), retry %d of %d in %.0f s",
                               self.channel.name, _safe_error(exc), attempt + 1, SEND_RETRIES, wait)
                self.sleep(wait)
            except Exception as exc:
                logger.error("%s: send failed: %s", self.channel.name, _safe_error(exc))
                return False
        return False


class Broadcaster:
    """Fan a message out to every channel whose route includes its kind."""

    def __init__(self, routes: list[tuple[Channel, tuple[str, ...]]], prefix: str = "",
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.prefix = prefix
        self.workers = [_Worker(ch, kinds, sleep) for ch, kinds in routes]

    @property
    def channel_names(self) -> list[str]:
        return [w.channel.name for w in self.workers]

    @property
    def dry_run(self) -> bool:
        return not self.workers

    def _for(self, kind: str) -> list[_Worker]:
        if kind not in KINDS:
            raise ValueError(f"unknown alert kind {kind!r}")
        return [w for w in self.workers if kind in w.kinds]

    def send(self, text: str, kind: str) -> bool:
        """Queue to every channel routed for ``kind``; False when none is."""
        body = (self.prefix + "\n" + text) if self.prefix else text
        targets = self._for(kind)
        if not targets:
            logger.info("alert %s (no channel):\n%s", kind, body)
        for w in targets:
            w.q.put(("text", body))
        return bool(targets)

    def send_photos(self, photos: list[tuple[str, bytes]], kind: str) -> bool:
        if not photos:
            return True
        if self.prefix:
            photos = [(self.prefix + " " + cap, jpg) for cap, jpg in photos]
        targets = self._for(kind)
        if not targets:
            logger.info("alert %s (no channel): %d photos", kind, len(photos))
        for w in targets:
            w.q.put(("photos", photos))
        return bool(targets)

    def flush(self, timeout: float = 120.0) -> bool:
        """Wait until every queued message has been handed to its channel."""
        deadline = time.time() + timeout
        for w in self.workers:
            while w.q.unfinished_tasks and time.time() < deadline:
                time.sleep(0.1)
        return all(not w.q.unfinished_tasks for w in self.workers)


def route_for(name: str, environ: dict[str, str]) -> tuple[str, ...]:
    spec = environ.get(f"ROVIMEN_FIREBALL_ROUTE_{name.upper()}")
    return parse_kinds(spec) if spec else DEFAULT_ROUTES[name]


def broadcaster_from_env(environ: dict[str, str] | None = None, prefix: str = "",
                         only: set[str] | None = None) -> Broadcaster:
    """Every channel whose settings are present; ``only`` limits to those names."""
    env = load_settings(environ)
    token = (env.get("ROVIMEN_FIREBALL_TELEGRAM_BOT_TOKEN") or env.get("ROVIMEN_TELEGRAM_BOT_TOKEN", "")).strip()
    routes: list[tuple[Channel, tuple[str, ...]]] = []
    candidates: list[tuple[str, Callable[[], Channel | None]]] = [
        ("telegram", lambda: TelegramChat("telegram", token, env.get("ROVIMEN_FIREBALL_TELEGRAM_CHAT_ID", "").strip())
         if token and env.get("ROVIMEN_FIREBALL_TELEGRAM_CHAT_ID", "").strip() else None),
        ("telegram_community", lambda: TelegramChat(
            "telegram_community", token, env.get("ROVIMEN_FIREBALL_TELEGRAM_COMMUNITY_CHAT_ID", "").strip())
         if token and env.get("ROVIMEN_FIREBALL_TELEGRAM_COMMUNITY_CHAT_ID", "").strip() else None),
        ("discord", lambda: DiscordWebhook(env["ROVIMEN_FIREBALL_DISCORD_WEBHOOK_URL"].strip())
         if env.get("ROVIMEN_FIREBALL_DISCORD_WEBHOOK_URL", "").strip() else None),
    ]
    mail = mail_settings(env)
    extra = [a.strip().lower() for a in re.split(r"[\s,;]+", mail["extra_to"]) if EMAIL_RE.match(a.strip())]
    candidates.append(("email", lambda: EmailChannel(
        mail, lambda: sorted(set(subscriber_emails()) | set(extra)))
        if mail["user"] and mail["password"] else None))
    for name, make in candidates:
        if only is not None and name not in only:
            continue
        ch = make()
        if ch is not None:
            routes.append((ch, route_for(name, env)))
    return Broadcaster(routes, prefix=prefix)
