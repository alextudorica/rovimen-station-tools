"""The ingest blob transport's retry policy (ingest_blob.py) and the archive
uploader's use of it: status classification, Retry-After, the auth pause, the
breaker that stops a VPS outage from stalling dawn, and aborts."""

from __future__ import annotations

import email.utils
import json
import time
import urllib.error
from pathlib import Path

import pytest

import archive_upload
import flags_manager
import ingest_blob
from ingest_blob import BlobSender, BlobStatus

URL = "https://vps.example/api/ingest/v1/gmnro03/blob/C/20260920/stacks/x_stack.webp"


class _Transport:
    """Scripted statuses; the last one repeats. Records every POST."""

    def __init__(self, *statuses: int) -> None:
        self._statuses = list(statuses)
        self.calls: list[str] = []

    def __call__(self, url: str, key: str, data: bytes, timeout: int) -> int:
        self.calls.append(url)
        if len(self._statuses) > 1:
            return self._statuses.pop(0)
        return self._statuses[0]


@pytest.fixture
def slept(monkeypatch) -> list[float]:
    record: list[float] = []
    monkeypatch.setattr(ingest_blob.time, "sleep", record.append)
    return record


@pytest.fixture
def clock(monkeypatch) -> dict:
    now = {"t": 1000.0}
    monkeypatch.setattr(ingest_blob.time, "monotonic", lambda: now["t"])
    return now


def _send(sender: BlobSender, url: str = URL, **kw) -> bool:
    return sender.send(url, "KEY", b"bytes", 5, "x_stack.webp", **kw)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status,verdict", [
    (200, ingest_blob.OK), (201, ingest_blob.OK),
    (408, ingest_blob.TRANSIENT), (500, ingest_blob.TRANSIENT),
    (502, ingest_blob.TRANSIENT), (503, ingest_blob.TRANSIENT),
    (0, ingest_blob.TRANSIENT),
    (429, ingest_blob.RATE_LIMITED), (507, ingest_blob.SPOOL_FULL),
    (401, ingest_blob.AUTH), (403, ingest_blob.AUTH),
    (400, ingest_blob.PERMANENT), (413, ingest_blob.PERMANENT),
    (422, ingest_blob.PERMANENT),
])
def test_classify(status, verdict):
    assert ingest_blob.classify(status) == verdict


def test_is_permanent_status_is_shared_with_the_pusher():
    import rovimen_pusher
    assert rovimen_pusher.is_permanent_status is ingest_blob.is_permanent_status


# ---------------------------------------------------------------------------
# Status handling in the sender
# ---------------------------------------------------------------------------

def test_408_is_retried_not_parked(slept, clock):
    t = _Transport(408, 408, 200)
    sender = BlobSender(t)
    assert _send(sender) is True
    assert len(t.calls) == 3
    assert sender.quarantined == {}


@pytest.mark.parametrize("status", [502, 0])
def test_502_and_no_response_are_transient(slept, clock, status):
    t = _Transport(status)
    sender = BlobSender(t)
    assert _send(sender) is False
    assert len(t.calls) == ingest_blob.MAX_ATTEMPTS
    assert sender.quarantined == {}


@pytest.mark.parametrize("status", [401, 403])
def test_rejected_key_pauses_the_station_not_the_file(slept, clock, status):
    t = _Transport(status, 200)
    sender = BlobSender(t)
    assert _send(sender) is False
    assert len(t.calls) == 1
    assert sender.quarantined == {}
    # Every other file waits too: re-sending bodies can't fix a key.
    assert _send(sender, URL.replace("x_stack", "y_stack")) is False
    assert len(t.calls) == 1
    clock["t"] += ingest_blob.AUTH_PAUSE_S + 1
    assert _send(sender) is True


def test_413_parks_the_file_and_says_it_is_too_large(slept, clock, caplog):
    t = _Transport(413)
    sender = BlobSender(t)
    with caplog.at_level("WARNING", logger=ingest_blob.logger.name):
        assert _send(sender) is False
        assert _send(sender) is False
    assert len(t.calls) == 1
    assert any("413" in r.getMessage() and "body limit" in r.getMessage()
               for r in caplog.records)


def test_retry_after_sets_the_rate_limit_wait(slept, clock):
    t = _Transport(BlobStatus(429, 7.0), 200)
    assert _send(BlobSender(t)) is True
    assert slept == [7.0]


def test_retry_after_is_capped(slept, clock):
    t = _Transport(BlobStatus(429, 99999.0), 200)
    assert _send(BlobSender(t)) is True
    assert slept == [ingest_blob.RATE_LIMIT_WAIT_CAP_S]


def test_retry_after_sets_the_spool_full_pause(slept, clock):
    t = _Transport(BlobStatus(507, 30.0), 200)
    sender = BlobSender(t)
    assert _send(sender) is False
    clock["t"] += 29
    assert sender.paused() is True
    clock["t"] += 2
    assert sender.paused() is False
    assert _send(sender) is True


def test_post_blob_status_carries_retry_after(monkeypatch):
    def seconds(*a, **k):
        raise urllib.error.HTTPError("http://x", 429, "slow down", {"Retry-After": "12"}, None)

    monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", seconds)
    status = ingest_blob.post_blob_status("http://x", "k", b"d")
    assert status == 429 and status.retry_after == 12.0

    when = email.utils.formatdate(time.time() + 120, usegmt=True)

    def http_date(*a, **k):
        raise urllib.error.HTTPError("http://x", 507, "full", {"Retry-After": when}, None)

    monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", http_date)
    status = ingest_blob.post_blob_status("http://x", "k", b"d")
    assert status == 507 and 100 < status.retry_after <= 121


def test_post_blob_status_without_retry_after(monkeypatch):
    def boom(*a, **k):
        raise urllib.error.HTTPError("http://x", 503, "down", {}, None)

    monkeypatch.setattr(ingest_blob.urllib.request, "urlopen", boom)
    assert ingest_blob.post_blob_status("http://x", "k", b"d").retry_after is None


# ---------------------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------------------

def test_breaker_opens_after_consecutive_failures_across_files(slept, clock):
    t = _Transport(0)
    sender = BlobSender(t)
    urls = [URL.replace("x_stack", f"s{i}_stack") for i in range(50)]
    for url in urls:
        _send(sender, url)
    assert len(t.calls) == ingest_blob.BREAKER_FAILURES
    assert sender.paused() is True
    assert "looks down" in sender.pause_reason


def test_breaker_probes_once_after_the_pause_and_reopens(slept, clock):
    t = _Transport(0)
    sender = BlobSender(t)
    for i in range(3):
        _send(sender, URL.replace("x_stack", f"s{i}_stack"))
    n = len(t.calls)
    clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
    assert _send(sender) is False
    assert len(t.calls) == n + 1, "one probe, then the breaker is open again"
    assert sender.paused() is True


def test_breaker_resets_on_success(slept, clock):
    t = _Transport(0, 0, 0, 200, 0, 0, 0)
    sender = BlobSender(t)
    _send(sender)                 # 3 failures
    _send(sender)                 # success resets
    _send(sender)                 # 3 failures, still under the threshold
    assert sender.paused() is False


def test_an_answering_vps_never_trips_the_breaker(slept, clock):
    t = _Transport(422)
    sender = BlobSender(t)
    for i in range(20):
        _send(sender, URL.replace("x_stack", f"s{i}_stack"))
    assert sender.paused() is False


# ---------------------------------------------------------------------------
# Aborts
# ---------------------------------------------------------------------------

def test_abort_before_the_first_attempt_posts_nothing(slept, clock):
    t = _Transport(200)
    assert _send(BlobSender(t), should_abort=lambda: True) is False
    assert t.calls == []


def test_abort_during_a_rate_limit_wait_returns_promptly(monkeypatch, clock):
    naps: list[float] = []
    monkeypatch.setattr(ingest_blob.time, "sleep", naps.append)
    t = _Transport(429, 200)
    state = {"n": 0}

    def should_abort() -> bool:
        state["n"] += 1
        return state["n"] > 3   # fires a few seconds into the 60 s wait

    assert _send(BlobSender(t), should_abort=should_abort) is False
    assert len(t.calls) == 1
    assert sum(naps) < 5


# ---------------------------------------------------------------------------
# The uploader: an outage or a deadline must not stall dawn
# ---------------------------------------------------------------------------

def _night_with_stacks(tmp_path: Path, n: int) -> dict:
    capture = tmp_path / "color_capture"
    stacks = capture / "C" / "20260920" / "stacks"
    stacks.mkdir(parents=True)
    for i in range(n):
        (stacks / f"C_20260920_21{i:04d}_stack.webp").write_bytes(b"webp")
    state = flags_manager._empty_state("C", "20260920")
    (stacks.parent / "state.json").write_text(json.dumps(state))
    return {
        "videocapture_path": str(capture),
        "stations": {"C": {}},
        "archive": {"enabled": True, "upload_stacks": True, "upload_meteors": True,
                    "upload_timelapses": True, "upload_rms": False},
    }


def _ready(monkeypatch) -> None:
    monkeypatch.setattr(archive_upload, "_resolve_base_url", lambda cfg: "https://vps.example")
    monkeypatch.setattr(archive_upload, "_resolve_ingest_key", lambda: "SECRET")
    monkeypatch.setattr(archive_upload, "_resolve_host_key", lambda cfg: "gmnro03")
    monkeypatch.setattr(archive_upload, "stagger_hold_s", lambda *a, **k: 0.0)


def test_vps_outage_costs_a_handful_of_posts_not_the_whole_night(tmp_path, monkeypatch, slept):
    cfg = _night_with_stacks(tmp_path, 200)
    _ready(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(archive_upload, "post_blob_status",
                        lambda url, *a, **k: (calls.append(url), 0)[1])
    assert archive_upload.run_night("C", "20260920", cfg) == 0
    assert len(calls) == ingest_blob.BREAKER_FAILURES


def test_run_night_stops_at_the_next_file_once_aborted(tmp_path, monkeypatch):
    cfg = _night_with_stacks(tmp_path, 20)
    _ready(monkeypatch)
    calls: list[str] = []
    monkeypatch.setattr(archive_upload, "post_blob_status",
                        lambda url, *a, **k: (calls.append(url), 200)[1])
    stacks = lambda: [c for c in calls if "/stacks/" in c]  # noqa: E731
    assert archive_upload.run_night("C", "20260920", cfg,
                                    should_abort=lambda: len(stacks()) >= 3) == 3


def test_run_once_honours_a_stop_event(tmp_path, monkeypatch):
    import threading
    cfg = _night_with_stacks(tmp_path, 20)
    _ready(monkeypatch)
    stop = threading.Event()
    calls: list[str] = []

    def post(url, *a, **k):
        calls.append(url)
        if len([c for c in calls if "/stacks/" in c]) == 2:
            stop.set()
        return 200

    monkeypatch.setattr(archive_upload, "post_blob_status", post)
    assert archive_upload.run_once("C", "", cfg, stop_event=stop) == 2


def test_body_over_the_tunnel_cap_is_parked_without_a_request(slept, clock, caplog):
    """Cloudflare drops a body over 100 MiB mid-upload; sending it anyway fed
    the breaker and paused every other file on gmnde00 (Johannismühle)."""
    t = _Transport(200)
    sender = BlobSender(t)
    big = ingest_blob.TUNNEL_MAX_BODY_BYTES + 1
    with caplog.at_level("WARNING", logger=ingest_blob.logger.name):
        assert sender.send(URL, "KEY", b"x", big, "x_timelapse.mp4") is False
        assert sender.send(URL, "KEY", b"x", big, "x_timelapse.mp4") is False
    assert t.calls == []
    assert not sender.paused()
    assert sum("MiB" in r.getMessage() for r in caplog.records) == 1
    # Rebuilt smaller, it goes through.
    assert sender.send(URL, "KEY", b"x", 1, "x_timelapse.mp4") is True
    assert len(t.calls) == 1


# ---------------------------------------------------------------------------
# Fallback route and per-file deferral (gmnro01 (Ghirdoveni), 2026-10-14)
# ---------------------------------------------------------------------------

PRIMARY = "https://vps.example"
ALT = "https://vps.tailnet.example"
ALT_URL = URL.replace(PRIMARY, ALT)


class _RouteTransport:
    """Status per base URL (prefix match); records every POSTed URL."""

    def __init__(self, by_base: dict) -> None:
        self.by_base = by_base
        self.calls: list[str] = []

    def __call__(self, url: str, key: str, data: bytes, timeout: int) -> int:
        self.calls.append(url)
        for base, status in self.by_base.items():
            if url.startswith(base + "/"):
                return status
        raise AssertionError(url)


def test_a_cloudflare_524_is_retried_at_once_on_the_fallback(slept, clock):
    t = _RouteTransport({PRIMARY: ingest_blob.CLOUDFLARE_ORIGIN_TIMEOUT, ALT: 200})
    sender = BlobSender(t, fallback=(PRIMARY, ALT))
    assert _send(sender) is True
    assert t.calls == [URL, ALT_URL]
    assert slept == [], "a different route is tried without a backoff wait"
    assert sender._consecutive_failures == 0


def test_after_the_fallback_carried_a_file_it_is_tried_first(slept, clock):
    t = _RouteTransport({PRIMARY: 0, ALT: 200})
    sender = BlobSender(t, fallback=(PRIMARY, ALT))
    assert _send(sender) is True
    other = URL.replace("x_stack", "y_stack")
    assert _send(sender, other) is True
    assert t.calls[-1] == other.replace(PRIMARY, ALT)
    assert len(t.calls) == 3, "the second file went straight to the fallback"


def test_the_preference_lapses_and_the_primary_is_first_again(slept, clock):
    t = _RouteTransport({PRIMARY: 0, ALT: 200})
    sender = BlobSender(t, fallback=(PRIMARY, ALT))
    _send(sender)
    clock["t"] += ingest_blob.FALLBACK_PREFER_S + 1
    t.by_base[PRIMARY] = 200
    other = URL.replace("x_stack", "y_stack")
    assert _send(sender, other) is True
    assert t.calls[-1] == other


def test_a_failing_fallback_falls_back_to_the_primary(slept, clock):
    t = _RouteTransport({PRIMARY: 0, ALT: 200})
    sender = BlobSender(t, fallback=(PRIMARY, ALT))
    _send(sender)                                  # sets the preference
    t.by_base = {ALT: 0, PRIMARY: 200}
    other = URL.replace("x_stack", "y_stack")
    assert _send(sender, other) is True
    assert t.calls[-2:] == [other.replace(PRIMARY, ALT), other]
    assert sender._prefer_fallback_until == 0.0


def test_both_routes_down_still_trips_the_breaker(slept, clock):
    t = _RouteTransport({PRIMARY: 0, ALT: 0})
    sender = BlobSender(t, fallback=(PRIMARY, ALT))
    for i in range(10):
        _send(sender, URL.replace("x_stack", f"s{i}_stack"))
    assert sender.paused() is True


@pytest.mark.parametrize("fallback", [None, (PRIMARY, ""), (PRIMARY, PRIMARY + "/")])
def test_no_fallback_means_the_primary_only(slept, clock, fallback):
    t = _Transport(0)
    sender = BlobSender(t, fallback=fallback)
    _send(sender)
    assert set(t.calls) == {URL}


def test_a_file_failing_repeatedly_is_set_aside(slept, clock):
    t = _Transport(0)
    sender = BlobSender(t)
    for _ in range(ingest_blob.FILE_DEFER_AFTER):
        clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
        _send(sender)
    assert sender.blocked(URL, 5) is True
    n = len(t.calls)
    clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
    assert _send(sender) is False
    assert len(t.calls) == n, "a deferred file is not POSTed"
    clock["t"] += ingest_blob.FILE_DEFER_S
    assert sender.blocked(URL, 5) is False


def test_a_deferred_file_whose_size_changed_is_retried(slept, clock):
    sender = BlobSender(_Transport(0))
    for _ in range(ingest_blob.FILE_DEFER_AFTER):
        clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
        _send(sender)
    assert sender.is_deferred(URL, 5) is True
    assert sender.is_deferred(URL, 6) is False


def test_one_bad_file_no_longer_blocks_the_queue(slept, clock):
    """The breaker's probe after each pause used to hit the same head-of-line
    file forever; once it is deferred the next probe is another file."""
    big = URL.replace("x_stack", "big_clip")
    small = URL.replace("x_stack", "small_stack")

    def transport(url, key, data, timeout):
        return 0 if url == big else 200

    sender = BlobSender(transport)
    for _ in range(ingest_blob.BREAKER_FAILURES):     # open the breaker on big
        sender.send(big, "KEY", b"b", 1, "big")
    sent = []
    for _ in range(ingest_blob.FILE_DEFER_AFTER + 2):    # later passes, big first
        clock["t"] += ingest_blob.BREAKER_PAUSE_S + 1
        sender.send(big, "KEY", b"b", 1, "big")
        sent.append(sender.send(small, "KEY", b"s", 1, "small"))
    assert sent[-1] is True


def test_success_clears_a_files_failure_count(slept, clock):
    t = _Transport(0, 0, 0, 200)
    sender = BlobSender(t)
    _send(sender)
    _send(sender)
    assert URL not in sender._file_failures
    assert sender.is_deferred(URL, 5) is False
