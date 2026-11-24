"""Fireball alert channels (dashboard/alert_channels.py) and the /alerts page.

Pins the routing (preliminary and retracted alerts stay in the maintainer's private
chat; members' channels get the confirmed alert, maps, thumbnails, clips),
what each channel puts on the wire (Telegram Bot API, Discord webhook
multipart, Gmail SMTP with Bcc and inline images), the subscriber list from
users.yaml, and the email subscription routes. No network: every HTTP post
and SMTP connection is a fake.
"""

from __future__ import annotations

import email
import json
import os
import tempfile
from pathlib import Path

import pytest
import yaml

import alert_channels as ac
import security as _security

# Bound at collection, before any app fixture swaps security.init_limiter for a
# disabled limiter (see tests/test_limiter_fail_open.py).
_real_init_limiter = _security.init_limiter


class _Resp:
    def __init__(self, status: int = 200) -> None:
        self.status_code = status
        self.text = ""


class _Recorder:
    def __init__(self, status: int = 200) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.status = status

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Resp(self.status)


class _FakeChannel:
    def __init__(self, name: str) -> None:
        self.name = name
        self.got: list[tuple[str, object]] = []

    def send(self, text: str) -> bool:
        self.got.append(("text", text))
        return True

    def send_photos(self, photos) -> bool:
        self.got.append(("photos", photos))
        return True


class _FakeSMTP:
    sent: list = []
    logins: list = []
    envelopes: list = []

    def __init__(self, host, port):
        self.host, self.port = host, port

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        _FakeSMTP.logins.append((self.host, self.port, user, password))

    def send_message(self, msg, to_addrs=None):
        _FakeSMTP.sent.append(msg)
        _FakeSMTP.envelopes.append(to_addrs)


JPG = b"\xff\xd8\xff\xe0fakejpeg"


# ── routing ──────────────────────────────────────────────────────────────────

def test_default_routes_keep_preliminary_and_retracted_private() -> None:
    for name in ("telegram_community", "discord", "email"):
        kinds = ac.DEFAULT_ROUTES[name]
        assert "preliminary" not in kinds and "retracted" not in kinds and "pulled" not in kinds
        assert "confirmed" in kinds and "clips" in kinds and "solution" in kinds
    assert set(ac.DEFAULT_ROUTES["telegram"]) == set(ac.KINDS)
    assert "thumbs" not in ac.DEFAULT_ROUTES["email"]       # one email per alert, not per album


def test_broadcaster_sends_each_kind_only_where_routed_and_in_order() -> None:
    priv, comm = _FakeChannel("telegram"), _FakeChannel("discord")
    b = ac.Broadcaster([(priv, ac.KINDS), (comm, ac.COMMUNITY)])
    assert b.send("p", "preliminary") is True
    assert b.send("c", "confirmed") is True
    assert b.send_photos([("map", JPG)], "confirmed_map") is True
    assert b.send("r", "retracted") is True
    assert b.flush(10)
    assert [x[1] for x in priv.got] == ["p", "c", [("map", JPG)], "r"]
    assert [x[1] for x in comm.got] == ["c", [("map", JPG)]]


def test_broadcaster_without_channels_is_a_dry_run_and_rejects_unknown_kinds() -> None:
    b = ac.Broadcaster([])
    assert b.dry_run and b.send("x", "confirmed") is False
    with pytest.raises(ValueError):
        b.send("x", "nonsense")


def test_a_failing_channel_does_not_stop_the_others() -> None:
    class Boom(_FakeChannel):
        def send(self, text):
            raise RuntimeError("smtp down")
    bad, good = Boom("email"), _FakeChannel("discord")
    b = ac.Broadcaster([(bad, ac.KINDS), (good, ac.KINDS)])
    b.send("one", "confirmed")
    b.send("two", "clips")
    assert b.flush(10)
    assert [x[1] for x in good.got] == ["one", "two"]


# ── retries ──────────────────────────────────────────────────────────────────

class _JsonResp(_Resp):
    def __init__(self, status: int, body: dict | None = None, headers: dict | None = None) -> None:
        super().__init__(status)
        self.body = body
        self.headers = headers or {}

    def json(self):
        if self.body is None:
            raise ValueError("no json")
        return self.body


class _Script:
    """A fake requests.post answering from a script (a response, or an exception to raise)."""

    def __init__(self, *answers) -> None:
        self.answers = list(answers)
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        a = self.answers.pop(0) if self.answers else _JsonResp(200)
        if isinstance(a, BaseException):
            raise a
        return a


def test_telegram_429_waits_retry_after_then_resends_only_that_message() -> None:
    import requests
    post = _Script(_JsonResp(429, {"ok": False, "parameters": {"retry_after": 7}}), _JsonResp(200),
                   requests.ConnectTimeout("connect"), _JsonResp(200), _JsonResp(200))
    slept: list[float] = []
    b = ac.Broadcaster([(ac.TelegramChat("telegram", "TOK", "42", post=post), ac.KINDS)], sleep=slept.append)
    b.send("one", "confirmed")
    b.send("two", "clips")
    b.send("three", "clips")
    assert b.flush(10)
    assert [c[1]["data"]["text"] for c in post.calls] == ["one", "one", "two", "two", "three"]
    assert slept == [8.0, ac.RETRY_BACKOFF_S]       # retry_after + 1 s, then the first backoff


def test_retries_give_up_after_three_and_never_retry_a_rejected_message() -> None:
    import requests
    post = _Script(_JsonResp(502), requests.ReadTimeout("read"), _JsonResp(503), _JsonResp(500),
                   _JsonResp(400), _JsonResp(200))
    slept: list[float] = []
    b = ac.Broadcaster([(ac.TelegramChat("telegram", "TOK", "42", post=post), ac.KINDS)], sleep=slept.append)
    b.send("lost", "confirmed")          # 1 attempt + SEND_RETRIES retries, then dropped
    b.send("bad html", "confirmed")      # 400: the server will never take it, no retry
    b.send("next", "confirmed")
    assert b.flush(10)
    assert [c[1]["data"]["text"] for c in post.calls] == ["lost"] * (ac.SEND_RETRIES + 1) + ["bad html", "next"]
    assert slept == [ac.RETRY_BACKOFF_S * 2 ** k for k in range(ac.SEND_RETRIES)]


def test_discord_429_uses_its_retry_after_and_photos_are_retried_too() -> None:
    post = _Script(_JsonResp(429, {"retry_after": 1.5}), _JsonResp(204))
    slept: list[float] = []
    b = ac.Broadcaster([(ac.DiscordWebhook("https://discord.example/hook", post=post), ac.KINDS)],
                       sleep=slept.append)
    b.send_photos([("map", JPG)], "confirmed_map")
    assert b.flush(10)
    assert len(post.calls) == 2 and slept == [2.5]


def test_a_retrying_channel_does_not_hold_up_the_others() -> None:
    import threading
    release, retrying = threading.Event(), threading.Event()

    def blocked_sleep(s: float) -> None:
        retrying.set()
        release.wait(10)

    post = _Script(_JsonResp(503), _JsonResp(200))
    good = _FakeChannel("discord")
    b = ac.Broadcaster([(ac.TelegramChat("telegram", "TOK", "42", post=post), ac.KINDS), (good, ac.KINDS)],
                       sleep=blocked_sleep)
    b.send("one", "confirmed")
    b.send("two", "clips")
    assert retrying.wait(10)
    for _ in range(1000):                # the other channel delivers while telegram waits to retry
        if len(good.got) == 2:
            break
        threading.Event().wait(0.01)
    assert [x[1] for x in good.got] == ["one", "two"]
    assert len(post.calls) == 1
    release.set()
    assert b.flush(10)
    assert [c[1]["data"]["text"] for c in post.calls] == ["one", "one", "two"]


def test_email_smtp_errors_are_retried_but_a_sent_email_is_not_sent_again() -> None:
    import smtplib
    sent: list = []

    class Flaky(_FakeSMTP):
        logins_left_to_fail = [smtplib.SMTPServerDisconnected("closed"), smtplib.SMTPResponseException(421, b"busy")]
        quit_fails = True

        def login(self, user, password):
            if Flaky.logins_left_to_fail:
                raise Flaky.logins_left_to_fail.pop(0)

        def send_message(self, msg, to_addrs=None):
            sent.append(msg["Subject"])

        def __exit__(self, *exc):
            if exc[0] is None and Flaky.quit_fails:   # QUIT answered with something other than 221
                Flaky.quit_fails = False
                raise smtplib.SMTPResponseException(451, b"quit failed")
            return False

    s = {"host": "h", "port": "587", "user": "u", "password": "p", "from_name": "n", "extra_to": ""}
    slept: list[float] = []
    b = ac.Broadcaster([(ac.EmailChannel(s, lambda: ["a@x.org"], smtp_factory=Flaky), ac.KINDS)],
                       sleep=slept.append)
    b.send("<b>Fireball confirmed</b>", "confirmed")
    b.send("<b>Fireball clips</b>", "clips")
    assert b.flush(10)
    assert sent == ["ROVIMEN: Fireball confirmed", "ROVIMEN: Fireball clips"]
    assert slept == [ac.RETRY_BACKOFF_S, ac.RETRY_BACKOFF_S * 2]


def test_send_errors_never_log_the_bot_token_or_webhook_secret(caplog) -> None:
    """requests puts the request URL in its exception text, and the URL holds the
    Telegram bot token or the Discord webhook secret."""
    import logging

    import requests
    token, hook = "123456:SECRET-BOT-TOKEN", "https://discord.com/api/webhooks/1/SECRET-WEBHOOK"
    tg_post = _Script(*[requests.ConnectionError(f"Max retries exceeded with url: /bot{token}/sendMessage")]
                      * (ac.SEND_RETRIES + 1))
    dc_post = _Script(requests.ReadTimeout(f"Read timed out: {hook}"), _JsonResp(429, {"retry_after": 1}),
                      RuntimeError(f"unexpected: {hook}"))

    class Smtp(_FakeSMTP):
        def __exit__(self, *exc):
            if exc[0] is None:
                raise OSError(f"quit failed, password {token}")
            return False

    s = {"host": "h", "port": "587", "user": "u", "password": token, "from_name": "n", "extra_to": ""}
    caplog.set_level(logging.DEBUG, logger="alert_channels")
    b = ac.Broadcaster([(ac.TelegramChat("telegram", token, "42", post=tg_post), ac.KINDS),
                        (ac.DiscordWebhook(hook, post=dc_post), ac.KINDS),
                        (ac.EmailChannel(s, lambda: ["a@x.org"], smtp_factory=Smtp), ac.KINDS)],
                       sleep=lambda s: None)
    b.send("one", "confirmed")
    assert b.flush(10)
    assert len(tg_post.calls) == ac.SEND_RETRIES + 1 and len(dc_post.calls) == 3
    assert "ConnectionError" in caplog.text and "ReadTimeout" in caplog.text and "HTTP 429" in caplog.text
    assert "SECRET" not in caplog.text


def test_route_override_from_env() -> None:
    assert ac.route_for("discord", {"ROVIMEN_FIREBALL_ROUTE_DISCORD": "confirmed, clips"}) == ("confirmed", "clips")
    assert ac.route_for("email", {"ROVIMEN_FIREBALL_ROUTE_EMAIL": "*"}) == ac.KINDS
    with pytest.raises(ValueError):
        ac.route_for("email", {"ROVIMEN_FIREBALL_ROUTE_EMAIL": "confirmd"})


def test_broadcaster_from_env_builds_only_configured_channels(tmp_path) -> None:
    env = {"ROVIMEN_TELEGRAM_BOT_TOKEN": "tok", "ROVIMEN_FIREBALL_TELEGRAM_CHAT_ID": "1",
           "ROVIMEN_FIREBALL_DISCORD_WEBHOOK_URL": "https://discord.example/api/webhooks/1/x",
           "ROVIMEN_MAIL_ENV": str(tmp_path / "missing.env"),
           "ROVIMEN_FIREBALL_ENV": str(tmp_path / "missing2.env")}
    assert ac.broadcaster_from_env(env).channel_names == ["telegram", "discord"]
    (tmp_path / "mail.env").write_text('# gmail\nROVIMEN_SMTP_USER=alerts@gmail.com\n'
                                       'ROVIMEN_SMTP_PASSWORD="abcd efgh ijkl mnop"\n')
    env["ROVIMEN_MAIL_ENV"] = str(tmp_path / "mail.env")
    env["ROVIMEN_FIREBALL_TELEGRAM_COMMUNITY_CHAT_ID"] = "-100"
    b = ac.broadcaster_from_env(env)
    assert b.channel_names == ["telegram", "telegram_community", "discord", "email"]
    assert ac.mail_settings(env)["password"] == "abcdefghijklmnop"   # Gmail shows it in groups of 4
    assert ac.broadcaster_from_env(env, only={"email"}).channel_names == ["email"]


# ── channels on the wire ─────────────────────────────────────────────────────

def test_telegram_text_and_album() -> None:
    rec = _Recorder()
    t = ac.TelegramChat("telegram", "TOK", "42", post=rec)
    assert t.send("<b>Fireball confirmed</b>")
    url, = rec.calls[0][0]
    assert url.endswith("/botTOK/sendMessage") and rec.calls[0][1]["data"]["parse_mode"] == "HTML"
    assert t.send_photos([("a", JPG), ("b", JPG)])
    assert rec.calls[1][0][0].endswith("/sendMediaGroup")
    assert len(json.loads(rec.calls[1][1]["data"]["media"])) == 2
    assert t.send_photos([("a", JPG)])
    assert rec.calls[2][0][0].endswith("/sendPhoto")


def test_discord_markdown_no_mentions_and_attachments() -> None:
    rec = _Recorder(204)
    d = ac.DiscordWebhook("https://discord.example/hook", post=rec)
    assert d.send("<b>Fireball confirmed</b>, 20:16:34 UTC\nRO0003 (Bistri&#539;a) x RO000P (Cluj) @everyone")
    body = rec.calls[0][1]["json"]
    assert body["content"].startswith("**Fireball confirmed**, 20:16:34 UTC")
    assert "Bistrița" in body["content"]
    assert body["allowed_mentions"] == {"parse": []}
    assert d.send("x" * 5000)
    assert len(rec.calls[1][1]["json"]["content"]) == ac.DISCORD_MAX_CHARS
    assert d.send_photos([("<b>map</b>", JPG), ("close-up", JPG)])
    kw = rec.calls[2][1]
    payload = json.loads(kw["data"]["payload_json"])
    assert [a["filename"] for a in payload["attachments"]] == ["p0.jpg", "p1.jpg"]
    assert set(kw["files"]) == {"files[0]", "files[1]"}
    assert "**map**" in payload["content"]


def test_email_goes_bcc_with_inline_images_over_ssl() -> None:
    _FakeSMTP.sent.clear()
    _FakeSMTP.logins.clear()
    _FakeSMTP.envelopes.clear()
    s = {"host": "smtp.gmail.com", "port": "587", "user": "alerts@gmail.com", "password": "pw",
         "from_name": "ROVIMEN fireball alerts", "extra_to": ""}
    ch = ac.EmailChannel(s, lambda: ["a@x.org", "b@y.org"], smtp_factory=_FakeSMTP)
    assert ch.send("<b>Fireball confirmed</b>, 20:16:34 UTC\nline two")
    assert ch.send_photos([("<b>Confirmed ground track</b>", JPG), ("Close-up", JPG)])
    assert _FakeSMTP.logins == [("smtp.gmail.com", 587, "alerts@gmail.com", "pw")] * 2
    m1, m2 = _FakeSMTP.sent
    assert m1["Subject"] == "ROVIMEN: Fireball confirmed, 20:16:34 UTC"
    # recipients only in the envelope: no copy to the sending account, no addresses in headers
    assert _FakeSMTP.envelopes == [["a@x.org", "b@y.org"]] * 2
    assert m1["To"] == "undisclosed-recipients:;" and m1["Bcc"] is None
    assert "a@x.org" not in m1.as_string() and "Unsubscribe" in m1.as_string()
    assert "line two" in m1.get_body(("plain",)).get_content()
    assert m2["Subject"] == "ROVIMEN: Confirmed ground track (+1 images)"
    parsed = email.message_from_bytes(m2.as_bytes())
    imgs = [p for p in parsed.walk() if p.get_content_type() == "image/jpeg"]
    assert len(imgs) == 2 and all(p["Content-ID"] for p in imgs)


def test_test_alert_tool_sends_every_kind(monkeypatch, tmp_path) -> None:
    """tools/fireball_test_alert.py --kinds accepts every kind in KINDS (withdrawn raised KeyError)."""
    import fireball_test_alert as fta
    ch = _FakeChannel("telegram")
    monkeypatch.setattr(ac, "broadcaster_from_env", lambda only=None: ac.Broadcaster([(ch, ac.KINDS)]))
    monkeypatch.setattr(fta, "test_maps", lambda: [("map", JPG), ("close-up", JPG)])
    assert fta.main(["--env-file", str(tmp_path / "none.env"), "--kinds", ",".join(ac.KINDS)]) == 0
    assert len(ch.got) == len(ac.KINDS)
    withdrawn = ch.got[ac.KINDS.index("withdrawn")]
    assert withdrawn[0] == "text" and "Correction: false alarm" in withdrawn[1] and "TEST" in withdrawn[1]


def test_starttls_on_587_and_implicit_tls_on_465(monkeypatch) -> None:
    calls = []

    class Plain:
        def __init__(self, host, port, timeout):
            calls.append(("SMTP", host, port))

        def starttls(self, context):
            calls.append(("starttls",))

    class Ssl:
        def __init__(self, host, port, context, timeout):
            calls.append(("SMTP_SSL", host, port))

    monkeypatch.setattr(ac.smtplib, "SMTP", Plain)
    monkeypatch.setattr(ac.smtplib, "SMTP_SSL", Ssl)
    ac._smtp_connect("smtp.gmail.com", 587)
    ac._smtp_connect("smtp.gmail.com", 465)
    assert calls == [("SMTP", "smtp.gmail.com", 587), ("starttls",), ("SMTP_SSL", "smtp.gmail.com", 465)]
    assert ac.mail_settings({"ROVIMEN_MAIL_ENV": "/nonexistent", "ROVIMEN_FIREBALL_ENV": "/nonexistent"})["port"] == "587"


def test_email_without_subscribers_sends_nothing() -> None:
    _FakeSMTP.sent.clear()
    ch = ac.EmailChannel({"host": "h", "port": "465", "user": "u", "password": "p", "from_name": "n",
                          "extra_to": ""}, lambda: [], smtp_factory=_FakeSMTP)
    assert ch.send("<b>x</b>") and _FakeSMTP.sent == []


def test_subscribers_come_from_users_yaml(tmp_path) -> None:
    p = tmp_path / "users.yaml"
    p.write_text(yaml.safe_dump({"users": {
        "a": {"role": "host", "alert_email": "A@Example.org "},
        "b": {"role": "visitor", "alert_email": None},
        "c": {"role": "admin", "alert_email": "not-an-address"},
        "d": {"role": "host", "alert_email": "a@example.org"},
        "e": {"role": "host", "alert_email": "z@example.org"},
    }}))
    assert ac.subscriber_emails(p) == ["a@example.org", "z@example.org"]
    assert ac.subscriber_emails(tmp_path / "missing.yaml") == []


# ── /alerts page and the subscription routes ─────────────────────────────────

def _build_app(tmp: Path):
    (tmp / "users.yaml").write_text(yaml.safe_dump({"users": {
        "ana": {"display_name": "Ana", "password_hash": "", "role": "host", "require_totp": False},
    }}))
    cfg = {"correlation_window_s": 1, "station_api_port": 7779, "public_pages": ["overview"],
           "stations": {"pub_station": {
               "ip": "127.0.0.1", "label": "Public Station", "ssh_user": "test", "public": True,
               "lat": 45.0, "lon": 25.0,
               "cameras": [{"code": "PUB001", "cam_ip": "127.0.0.1", "label": "N", "rotate": False}]}}}
    config_path = tmp / "dashboard_config.yaml"
    config_path.write_text(yaml.safe_dump(cfg))
    for key, name in [("ROVIMEN_USERS_PATH", "users.yaml"), ("THUMB_CACHE_DIR", "thumb_cache"),
                      ("ROVIMEN_CACHE_PATH", "cache"), ("ROVIMEN_COMPILATIONS_OUT_PATH", "compilations"),
                      ("ROVIMEN_KNOWN_HOSTS", "known_hosts"), ("ROVIMEN_AUDIT_LOG_PATH", "audit.log"),
                      ("ROVIMEN_ACTIVITY_LOG_PATH", "activity.log"),
                      ("ROVIMEN_STATION_STATE_DB", "station_state.db"),
                      ("ROVIMEN_DETECTIONS_DB", "detections.db"),
                      ("ROVIMEN_SHOWER_COUNTS_PATH", "shower_counts.json")]:
        os.environ[key] = str(tmp / name)
    os.environ["ROVIMEN_SECRET_KEY"] = "alerts-test-secret"
    os.environ["ROVIMEN_COOKIE_SECURE"] = "0"
    import security
    saved = security.init_limiter

    def _noop_limiter(app):
        lim = saved(app)
        lim.enabled = False
        return lim

    security.init_limiter = _noop_limiter
    try:
        from rovimen_dashboard import create_app, load_config
        app = create_app(load_config(config_path), config_path)
    finally:
        security.init_limiter = saved
    app.config["TESTING"] = True
    return app


@pytest.fixture(autouse=True)
def _no_server_env_files(monkeypatch, tmp_path):
    """Never read a real /etc/rovimen-*.env from the machine running the tests."""
    monkeypatch.setenv("ROVIMEN_MAIL_ENV", str(tmp_path / "no-mail.env"))
    monkeypatch.setenv("ROVIMEN_FIREBALL_ENV", str(tmp_path / "no-fireball.env"))


def test_settings_merge_env_over_fireball_file_over_mail_file(tmp_path) -> None:
    (tmp_path / "m.env").write_text("ROVIMEN_SMTP_USER=a@gmail.com\nX=mail\n")
    (tmp_path / "f.env").write_text("ROVIMEN_FIREBALL_DISCORD_INVITE_URL=https://discord.gg/q\nX=fireball\n")
    s = ac.load_settings({"ROVIMEN_MAIL_ENV": str(tmp_path / "m.env"),
                          "ROVIMEN_FIREBALL_ENV": str(tmp_path / "f.env"), "Y": "env"})
    assert s["ROVIMEN_SMTP_USER"] == "a@gmail.com" and s["X"] == "fireball" and s["Y"] == "env"
    assert s["ROVIMEN_FIREBALL_DISCORD_INVITE_URL"] == "https://discord.gg/q"


@pytest.fixture(scope="module")
def app_and_dir():
    tmp = Path(tempfile.mkdtemp(prefix="rovimen_alerts_"))
    return _build_app(tmp), tmp


@pytest.fixture(autouse=True)
def _users_file(request, monkeypatch):
    """auth fixes USERS_PATH at first import; in a shared pytest worker that
    can be another test's file, so point it at this module's users.yaml."""
    if "app_and_dir" not in request.fixturenames:
        return
    import auth
    _, tmp = request.getfixturevalue("app_and_dir")
    monkeypatch.setattr(auth, "USERS_PATH", tmp / "users.yaml")
    monkeypatch.setattr(auth, "USERS_LOCK_PATH", tmp / "users.lock")
    monkeypatch.setenv("ROVIMEN_USERS_PATH", str(tmp / "users.yaml"))
    auth._invalidate_users_cache()


def _signed_in(app):
    c = app.test_client()
    with c.session_transaction() as sess:
        sess["user"] = "ana"
        sess["role"] = "host"
        sess["stations"] = []
        sess["mfa"] = "totp"
    return c


def _json(c, method, body=None):
    return c.open("/api/alerts/email", method=method, data=json.dumps(body) if body is not None else None,
                  content_type="application/json", headers={"Sec-Fetch-Site": "same-origin"})


def test_alerts_page_and_api_need_a_signed_in_user(app_and_dir) -> None:
    app, _ = app_and_dir
    c = app.test_client()
    assert c.get("/alerts").status_code == 302
    assert _json(c, "POST", {"email": "a@b.org"}).status_code == 401


def test_page_shows_join_links_and_the_email_form(app_and_dir, monkeypatch) -> None:
    app, _ = app_and_dir
    monkeypatch.setenv("ROVIMEN_FIREBALL_TELEGRAM_JOIN_URL", "https://t.me/+abc")
    monkeypatch.setenv("ROVIMEN_FIREBALL_DISCORD_INVITE_URL", "https://discord.gg/xyz")
    monkeypatch.setenv("ROVIMEN_SMTP_USER", "alerts@gmail.com")
    monkeypatch.setenv("ROVIMEN_SMTP_PASSWORD", "pw")
    html = _signed_in(app).get("/alerts").get_data(as_text=True)
    assert "https://t.me/+abc" in html and "https://discord.gg/xyz" in html
    assert 'id="al-email-form"' in html
    monkeypatch.delenv("ROVIMEN_FIREBALL_DISCORD_INVITE_URL")
    monkeypatch.delenv("ROVIMEN_SMTP_PASSWORD")
    html = _signed_in(app).get("/alerts").get_data(as_text=True)
    assert "The Discord channel is not set up yet." in html
    assert "Email alerts are not set up yet." in html


def test_subscribe_confirm_and_unsubscribe(app_and_dir, monkeypatch) -> None:
    app, tmp = app_and_dir
    sent = []
    monkeypatch.setattr(ac, "send_confirmation_email", lambda addr, name: sent.append((addr, name)) or (True, ""))
    c = _signed_in(app)
    r = _json(c, "POST", {"email": " Ana@Example.ORG "})
    assert r.status_code == 200 and r.get_json() == {"alert_email": "ana@example.org", "confirmation_sent": True,
                                                    "confirmation_error": ""}
    assert sent == [("ana@example.org", "Ana")]
    users = yaml.safe_load((tmp / "users.yaml").read_text())["users"]
    assert users["ana"]["alert_email"] == "ana@example.org"
    assert users["ana"]["role"] == "host"                     # nothing else touched
    assert ac.subscriber_emails(tmp / "users.yaml") == ["ana@example.org"]
    assert _json(c, "POST", {"email": "nope"}).status_code == 400
    assert _json(c, "DELETE").get_json() == {"alert_email": None}
    assert ac.subscriber_emails(tmp / "users.yaml") == []


def test_send_confirmation_email_reports_missing_settings(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("ROVIMEN_SMTP_USER", raising=False)
    monkeypatch.delenv("ROVIMEN_SMTP_PASSWORD", raising=False)
    ok, err = ac.send_confirmation_email("a@b.org")
    assert not ok and "not configured" in err


def test_subscribe_is_rate_limited_per_account(monkeypatch) -> None:
    """Each subscribe sends a confirmation email, so one account gets 3 an hour;
    another account (same IP behind the tunnel) keeps its own budget, and
    unsubscribing is not limited."""
    from flask import Flask

    import routes.alerts as alert_routes
    import security
    from models import UserConfig

    monkeypatch.delenv("ROVIMEN_REDIS_URL", raising=False)
    store = {"ana": UserConfig(role="host"), "bo": UserConfig(role="visitor")}
    monkeypatch.setattr(alert_routes, "_load_users", lambda: store)
    monkeypatch.setattr(alert_routes, "_update_users", lambda mutator: mutator(store))
    monkeypatch.setattr(security, "audit_request", lambda *a, **k: None)
    sent: list[str] = []
    monkeypatch.setattr(ac, "send_confirmation_email", lambda addr, name: sent.append(addr) or (True, ""))
    app = Flask(__name__)
    app.secret_key = "alerts-limit-secret"
    app._limiter = _real_init_limiter(app)  # type: ignore[attr-defined]
    alert_routes.register_alert_routes(app, "test")

    def client(user: str):
        c = app.test_client()
        with c.session_transaction() as sess:
            sess["user"] = user
        return c

    ana, bo = client("ana"), client("bo")
    codes = [_json(ana, "POST", {"email": f"ana{i}@example.org"}).status_code for i in range(4)]
    assert codes == [200, 200, 200, 429]
    assert sent == ["ana0@example.org", "ana1@example.org", "ana2@example.org"]
    assert _json(bo, "POST", {"email": "bo@example.org"}).status_code == 200
    assert _json(ana, "DELETE").status_code == 200
