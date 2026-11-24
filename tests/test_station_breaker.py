"""Station reachability breaker: one dead Tailscale peer must not cost every
request that touches it a full connect timeout.

Before this, an offline station was re-probed by every route that called it
(no failure memory), each probe holding a gthread for the 10 s connect
timeout; the calibration and RMS tabs fan out dozens of such calls at once,
which starved the worker's 32 threads and 504ed unrelated endpoints.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import requests
import urllib3.exceptions

PROJECT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT / "dashboard"))

import station_client  # noqa: E402


class _Tunnels:
    def get_api_base(self, host_key, path):
        return f"http://{host_key}.station.test:7779"


def _base(host_key: str) -> str:
    return _Tunnels().get_api_base(host_key, "/")


def _refused(msg: str = "refused") -> requests.exceptions.ConnectionError:
    """What requests raises for a SYN that is dropped or refused."""
    return requests.exceptions.ConnectionError(
        urllib3.exceptions.MaxRetryError(None, "http://x/", urllib3.exceptions.NewConnectionError(None, msg))
    )


def _reset_mid_response() -> requests.exceptions.ConnectionError:
    """What requests raises when the box answered, then dropped the socket."""
    return requests.exceptions.ConnectionError(
        urllib3.exceptions.ProtocolError("Connection aborted.", ConnectionResetError(104, "reset"))
    )


class _Resp:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _Session:
    """Scripted session: each ``get`` pops the next outcome."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def get(self, url, timeout=None, **kw):
        self.calls.append((url, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        if isinstance(outcome, _Resp):
            return outcome
        return _Resp(outcome)


@pytest.fixture
def breaker(monkeypatch):
    station_client._breaker_state.clear()
    clock = {"t": 1000.0}
    monkeypatch.setattr(station_client.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(station_client, "_BREAKER_BASE_S", 60.0)
    monkeypatch.setattr(station_client, "_BREAKER_MAX_S", 480.0)
    return clock


def _install(monkeypatch, session):
    monkeypatch.setattr(station_client, "_raw_session_for_url", lambda url: session)
    return session


def test_connect_failure_opens_the_breaker_and_later_calls_skip_the_network(breaker, monkeypatch):
    sess = _install(monkeypatch, _Session([requests.exceptions.ConnectTimeout("dead peer")]))
    with pytest.raises(requests.exceptions.ConnectTimeout):
        station_client.station_get_raw(None, _Tunnels(), "gmnro99", "/api/x")
    assert station_client.station_breaker_open(_base("gmnro99"))

    with pytest.raises(station_client.StationUnreachable):
        station_client.station_get_raw(None, _Tunnels(), "gmnro99", "/api/x")
    assert len(sess.calls) == 1, "an open breaker must not touch the network"


def test_the_window_expires_doubles_on_repeat_and_clears_on_success(breaker, monkeypatch):
    sess = _install(monkeypatch, _Session([
        _refused(),
        _refused(),
        {"ok": True},
    ]))
    t = _Tunnels()
    with pytest.raises(requests.exceptions.ConnectionError):
        station_client.station_get_raw(None, t, "h", "/a")
    breaker["t"] += 61                       # first window (60 s) elapsed -> half-open probe
    with pytest.raises(requests.exceptions.ConnectionError):
        station_client.station_get_raw(None, t, "h", "/a")
    breaker["t"] += 61                       # second window is 120 s: still open
    assert station_client.station_breaker_open(_base("h"))
    with pytest.raises(station_client.StationUnreachable):
        station_client.station_get_raw(None, t, "h", "/a")
    breaker["t"] += 60                       # now past 120 s
    assert station_client.station_get_raw(None, t, "h", "/a") == {"ok": True}
    assert not station_client.station_breaker_open(_base("h"))
    assert len(sess.calls) == 3


def test_read_timeouts_and_http_errors_do_not_trip_it(breaker, monkeypatch):
    class _Bad(_Resp):
        def raise_for_status(self):
            raise requests.exceptions.HTTPError("500")

    sess = _Session([requests.exceptions.ReadTimeout("slow"), None])
    sess.outcomes[1] = _Bad(None)
    _install(monkeypatch, sess)
    with pytest.raises(requests.exceptions.ReadTimeout):
        station_client.station_get_raw(None, _Tunnels(), "slow", "/a")
    assert not station_client.station_breaker_open(_base("slow"))
    with pytest.raises(requests.exceptions.HTTPError):
        station_client.station_get_raw(None, _Tunnels(), "slow", "/a")
    assert not station_client.station_breaker_open(_base("slow"))


def test_status_probe_is_never_blocked_but_feeds_the_breaker(breaker, monkeypatch):
    sess = _install(monkeypatch, _Session([
        requests.exceptions.ConnectTimeout("dead"),
        requests.exceptions.ConnectTimeout("dead"),
        {"services": {}},
    ]))
    t = _Tunnels()
    assert station_client.station_get_status(None, t, "p", "/api/status")["online"] is False
    assert station_client.station_breaker_open(_base("p"))
    # The poller still probes while the breaker is open...
    assert station_client.station_get_status(None, t, "p", "/api/status")["online"] is False
    assert len(sess.calls) == 2
    # ...and its first success clears the breaker for the request path.
    assert station_client.station_get_status(None, t, "p", "/api/status")["online"] is True
    assert not station_client.station_breaker_open(_base("p"))


def test_scalar_timeouts_get_a_capped_connect_half(breaker, monkeypatch):
    monkeypatch.setattr(station_client, "_STATION_CONNECT_TIMEOUT_S", 5.0)
    sess = _install(monkeypatch, _Session([{"a": 1}, {"a": 1}, {"a": 1}]))
    station_client.station_get_raw(None, _Tunnels(), "h", "/a", timeout=15)
    station_client.station_get_raw(None, _Tunnels(), "h", "/a", timeout=3)
    station_client.station_session_get("h", "http://x/y", timeout=(2, 90))
    assert [c[1] for c in sess.calls] == [(5.0, 15.0), (3.0, 3.0), (2, 90)]


def test_session_get_helper_honours_the_breaker(breaker, monkeypatch):
    sess = _install(monkeypatch, _Session([_refused()]))
    with pytest.raises(requests.exceptions.ConnectionError):
        station_client.station_session_get("s", "http://x/y", timeout=15, stream=True)
    with pytest.raises(station_client.StationUnreachable):
        station_client.station_session_get("s", "http://x/y", timeout=15)
    assert len(sess.calls) == 1
    assert sess.calls[0][1] == (5.0, 15.0)


def test_plain_session_fetches_are_covered_once_station_url_ran(breaker, monkeypatch):
    """The ~40 media/proxy call sites build a station URL and fetch it with
    ``_session_for_url(url).get(...)`` themselves; they must get the breaker
    without knowing about it. ``station_url`` records base -> host_key, and the
    session front recognises the base."""
    raw = _Session([requests.exceptions.ConnectTimeout("dead"), {"ok": 1}, {"ok": 2}])
    monkeypatch.setattr(station_client, "_get_station_session", lambda base: raw)
    url = station_client.station_url(None, _Tunnels(), "gmnro07", "/color_capture/x.mkv")
    assert url.startswith("http://gmnro07.station.test:7779/")

    with pytest.raises(requests.exceptions.ConnectTimeout):
        station_client._session_for_url(url).get(url, timeout=120, stream=True)
    assert station_client.station_breaker_open(_base("gmnro07"))
    with pytest.raises(station_client.StationUnreachable):
        station_client._session_for_url(url).get(url, timeout=120, stream=True)
    with pytest.raises(station_client.StationUnreachable):
        station_client._session_for_url(url).head(url, timeout=10)
    with pytest.raises(station_client.StationUnreachable):
        station_client._session_for_url(url).post(url, json={}, timeout=15)
    assert len(raw.calls) == 1, "an open breaker never reaches the pooled session"
    assert raw.calls[0][1] == (5.0, 120.0), "the connect half of a scalar timeout is capped"

    # A URL no station claimed passes straight through (only the connect cap).
    other = "http://unrelated.example:9/x"
    assert station_client._session_for_url(other).get(other, timeout=30).json() == {"ok": 1}
    assert raw.calls[-1][1] == (5.0, 30.0)

    # The pollers' probe still runs on the raw session and clears the breaker.
    breaker["t"] += 1
    assert station_client.station_get_status(None, _Tunnels(), "gmnro07", "/api/status")["online"] is True
    assert not station_client.station_breaker_open(_base("gmnro07"))


def test_session_front_keeps_verb_defaults_and_delegates_the_rest(breaker, monkeypatch):
    seen = {}

    class _Real:
        headers = {"X": "1"}

        def head(self, url, **kw):
            seen["head"] = kw
            return _Resp(None)

        def close(self):
            seen["closed"] = True

    front = station_client._BreakerSession(_Real(), None, None)
    front.head("http://a/b", timeout=7)
    assert seen["head"] == {"timeout": (5.0, 7.0)}   # HEAD reaches Session.head, not request()
    assert front.headers == {"X": "1"}
    front.close()
    assert seen["closed"] is True


def test_a_reset_after_the_box_answered_does_not_trip_and_two_boxes_are_independent(breaker, monkeypatch):
    raw = _Session([_reset_mid_response(), _refused(), {"ok": 1}])
    monkeypatch.setattr(station_client, "_get_station_session", lambda base: raw)

    class _Split:
        """One dashboard station spanning two boxes (per-camera host override)."""

        def get_api_base(self, host_key, path):
            return "http://pi.station.test:7779" if "GH662" in path else "http://pc.station.test:7779"

    t = _Split()
    # Station API restarted mid-request on the PC: a ConnectionError, but the
    # box was reachable — no fail-fast window.
    with pytest.raises(requests.exceptions.ConnectionError):
        station_client.station_get_raw(None, t, "gmnro01", "/api/chunks/RO000A/20260906")
    assert not station_client.station_breaker_open("http://pc.station.test:7779")

    # The Pi is dead: only the Pi's base trips; the PC keeps serving.
    with pytest.raises(requests.exceptions.ConnectionError):
        station_client.station_get_raw(None, t, "gmnro01", "/api/chunks/GH662S/20260906")
    assert station_client.station_breaker_open("http://pi.station.test:7779")
    assert not station_client.station_breaker_open("http://pc.station.test:7779")
    assert station_client.station_get_raw(None, t, "gmnro01", "/api/chunks/RO000A/20260906") == {"ok": 1}
    with pytest.raises(station_client.StationUnreachable):
        station_client.station_get_raw(None, t, "gmnro01", "/api/chunks/GH662W/20260906")
    assert len(raw.calls) == 3


def test_weeks_of_failures_never_overflow_the_backoff(breaker):
    base = _base("chiribis")
    for _ in range(3000):                      # ~2 days of per-minute poll failures
        station_client.station_breaker_trip(base, requests.exceptions.ConnectTimeout("dead"))
        breaker["t"] += 60
    assert station_client.station_breaker_open(base)
    with station_client._breaker_lock:
        until, failures = station_client._breaker_state[base]
    assert failures == 3000
    assert until - breaker["t"] <= station_client._BREAKER_MAX_S
