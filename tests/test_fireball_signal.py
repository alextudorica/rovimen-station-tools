"""Fireball signals: the VPS daemon's POST to a Pi and the Pi-side receiver.

``fireball_alert.Signals`` sends every preliminary/confirmed/retracted message
to the receivers in ROVIMEN_FIREBALL_SIGNAL_URLS; ``tools/fireball_signal.py``
checks the shared secret and runs a local command with the alert in its env.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "dashboard"))
sys.path.insert(0, str(REPO / "tools"))

import fireball_alert as fa  # noqa: E402
import fireball_signal as fs  # noqa: E402

INFO = {
    "RO0003": fa.CamInfo("gmnro05", "Bistrița"),
    "RO000P": fa.CamInfo("gmnro06", "Cluj"),
    "RO000G": fa.CamInfo("gmnro10", "Roit"),
}
SECRET = "s3cret"


def _msg(kind: str = "preliminary") -> fa.Message:
    al = fa.Alert(id="20261021_201634", t_event=1792613794.0, created=1792613804.0,
                  rule_a={"spikes": [{"cam": "RO0003"}, {"cam": "RO000P"}]},
                  pairs=[{"a": {"cam": "RO000P"}, "b": {"cam": "RO000G"}}])
    return fa.Message(kind=kind, alert=al, text="<b>Fireball</b> &amp; more", at=0.0)


def test_payload_names_cameras_stations_and_plain_text():
    body = fa.signal_payload(_msg("confirmed"), INFO)
    assert body["kind"] == "confirmed"
    assert body["id"] == "20261021_201634"
    assert body["utc"] == "2026-10-21T20:16:34Z"
    assert body["cameras"] == ["RO0003", "RO000G", "RO000P"]
    assert body["stations"] == ["gmnro05 (Bistrița)", "gmnro06 (Cluj)", "gmnro10 (Roit)"]
    assert body["text"] == "Fireball & more"


def test_no_urls_means_nothing_is_sent(monkeypatch):
    monkeypatch.delenv("ROVIMEN_FIREBALL_SIGNAL_URLS", raising=False)
    assert fa.signals_from_env().send(_msg(), INFO) == []


def test_urls_split_on_spaces_and_commas(monkeypatch):
    monkeypatch.setenv("ROVIMEN_FIREBALL_SIGNAL_URLS", " http://a:1/ ,http://b:2/  http://c:3/")
    monkeypatch.setenv("ROVIMEN_FIREBALL_SIGNAL_SECRET", " x ")
    s = fa.signals_from_env()
    assert s.urls == ["http://a:1/", "http://b:2/", "http://c:3/"]
    assert s.secret == "x"


@pytest.fixture
def receiver(tmp_path):
    out = tmp_path / "fired"
    cmd = f'echo "$FIREBALL_KIND|$FIREBALL_ID|$FIREBALL_CAMERAS|$FIREBALL_STATIONS" >> {out}'
    server = fs.ThreadingHTTPServer(("127.0.0.1", 0), fs.make_handler(SECRET, cmd, {"preliminary", "confirmed"}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/", out
    server.shutdown()


def _wait_lines(path: Path, n: int) -> list[str]:
    for _ in range(100):
        if path.exists() and len(path.read_text().splitlines()) >= n:
            break
        time.sleep(0.02)
    return path.read_text().splitlines() if path.exists() else []


def test_daemon_signal_runs_the_command_on_the_pi(receiver):
    url, out = receiver
    for t in fa.Signals([url], SECRET).send(_msg("preliminary"), INFO):
        t.join(5)
    assert _wait_lines(out, 1) == [
        "preliminary|20261021_201634|RO0003 RO000G RO000P|gmnro05 (Bistrița); gmnro06 (Cluj); gmnro10 (Roit)"]


def test_kinds_not_selected_are_accepted_but_run_nothing(receiver):
    url, out = receiver
    for t in fa.Signals([url], SECRET).send(_msg("retracted"), INFO):
        t.join(5)
    time.sleep(0.2)
    assert not out.exists()


def _post(url: str, body: bytes, secret: str) -> int:
    req = urllib.request.Request(url, data=body, method="POST",
                                 headers={"X-Rovimen-Signal": secret, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


def test_wrong_secret_is_refused(receiver):
    url, out = receiver
    assert _post(url, json.dumps({"kind": "confirmed"}).encode(), "nope") == 403
    assert not out.exists()


@pytest.mark.parametrize("body", [b"not json", b"[1, 2]", json.dumps({"kind": "launch"}).encode()])
def test_malformed_alerts_are_refused(receiver, body):
    url, out = receiver
    assert _post(url, body, SECRET) == 400
    assert not out.exists()


# ---------------------------------------------------------------------------
# Telegram signal chats (receivers the VPS cannot reach over the tailnet)
# ---------------------------------------------------------------------------

def test_signal_line_goes_to_each_telegram_chat(monkeypatch):
    posted = []

    class _R:
        status_code = 200
        text = ""

    import requests
    monkeypatch.setattr(requests, "post", lambda url, data, timeout: posted.append((url, data)) or _R())
    s = fa.Signals([], "", telegram_chats=["-100"], telegram_token="TOK")
    for t in s.send(_msg("confirmed"), INFO):
        t.join()
    assert len(posted) == 1
    url, data = posted[0]
    assert url.endswith("/botTOK/sendMessage") and data["chat_id"] == "-100"
    assert "parse_mode" not in data
    assert data["text"].startswith(fa.SIGNAL_LINE_PREFIX)
    body = json.loads(data["text"][len(fa.SIGNAL_LINE_PREFIX):])
    assert body["kind"] == "confirmed" and "text" not in body


def test_no_token_means_no_telegram_signals():
    assert fa.Signals([], "", telegram_chats=["-100"], telegram_token="").send(_msg(), INFO) == []


def _update(uid, text, chat=-100, username="example_alert_bot", is_bot=True):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text,
                                          "from": {"is_bot": is_bot, "username": username}}}


def _line(kind="preliminary"):
    return fa.SIGNAL_LINE_PREFIX + json.dumps({"kind": kind, "id": "20261021_201634"})


def test_receiver_accepts_only_the_alert_bot_in_the_signal_chat():
    assert fs.alert_from_update(_update(1, _line()), "-100", "@example_alert_bot")["kind"] == "preliminary"
    assert fs.alert_from_update(_update(1, _line(), chat=-200), "-100", "example_alert_bot") is None
    assert fs.alert_from_update(_update(1, _line(), username="someone", is_bot=False),
                                "-100", "example_alert_bot") is None
    assert fs.alert_from_update(_update(1, "hi"), "-100", "example_alert_bot") is None
    assert fs.alert_from_update(_update(1, fa.SIGNAL_LINE_PREFIX + "{bad"), "-100", "example_alert_bot") is None


def test_poll_skips_the_backlog_and_runs_the_command(monkeypatch):
    ran = []
    monkeypatch.setattr(fs, "run_command", lambda cmd, alert: ran.append(alert["kind"]))
    calls = []

    def get_updates(token, offset, timeout):
        calls.append(offset)
        if offset == -1:
            return [_update(7, _line("confirmed"))]          # old alert: skipped
        if offset == 8:
            return [_update(8, _line("preliminary")), _update(9, _line("retracted"))]
        return []

    fs.poll_telegram("TOK", "-100", "example_alert_bot", "true", {"preliminary", "confirmed"},
                     get_updates=get_updates, sleep=lambda s: None, max_loops=2)
    assert ran == ["preliminary"]
    assert calls == [-1, 8, 10]


def test_poll_backs_off_on_errors(monkeypatch):
    slept = []

    def get_updates(token, offset, timeout):
        if offset == -1:
            return []
        raise OSError("network down")

    fs.poll_telegram("TOK", "-100", "b", "true", set(KINDS_ALL),
                     get_updates=get_updates, sleep=slept.append, max_loops=3)
    assert slept == [5.0, 10.0, 20.0]


KINDS_ALL = ("preliminary", "confirmed", "retracted")


def test_receiver_accepts_channel_posts_in_the_signal_channel():
    post = {"update_id": 3, "channel_post": {"chat": {"id": -100, "type": "channel"},
                                             "sender_chat": {"id": -100}, "text": _line("confirmed")}}
    assert fs.alert_from_update(post, "-100", "example_alert_bot")["kind"] == "confirmed"
    post["channel_post"]["chat"]["id"] = -200
    assert fs.alert_from_update(post, "-100", "example_alert_bot") is None
