#!/usr/bin/env python3
"""ingest_blob.py — station-to-VPS blob transport over the HTTPS ingest API.

Every archival byte a station sends goes to one endpoint,
``<base>/api/ingest/v1/<host_key>/blob/<camera>/<date>/<relpath>``, authenticated
with the station's ingest key (the same key the pusher uses). This module owns
that POST and the rules for what an HTTP answer means:

* ``post_blob_status`` / ``post_blob``: one POST, never raises.
* ``is_permanent_status``: a 4xx that re-sending the same bytes cannot fix;
  shared with rovimen_pusher so both agents classify the VPS the same way.
* ``BlobSender``: the retry policy for a long upload pass. Transient failures
  retry with capped backoff, a 429 waits out the rate-limit window, a 507
  (VPS spool full) or a rejected key (401/403) pauses every upload, a run of
  failed POSTs (VPS or tunnel down) trips a breaker that pauses them too, a
  permanent rejection parks the file instead of re-sending it every cycle, a
  file that keeps failing on transport is set aside for an hour so it cannot
  block the queue, and a transport failure is retried once on the fallback
  base URL (the VPS on the tailnet) when one is configured.
  An abort callback (dawn's deadline, the daemon's stop) is honoured before
  every attempt and during every wait.

archive_upload drives ``BlobSender`` for the nightly archive; detection_indexer
(FF thumbnails), sky_dome_render and spectro_pipeline send single files with
``post_blob``. Standard library only, and no import of any other station
module: those three used to import archive_upload for this, which pulled in
encoder, overlay and flags_manager, so an import regression in any of them
silently switched the thumbnail, dome and spectro uploads off. Must import on
Python 3.8 (the archive-upload unit runs the system python3; gmnro05 (Bistrița)
and gmnro08 (Brașov) run RMS venvs on 3.8.10).
"""

from __future__ import annotations

import email.utils
import logging
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

AGENT_VERSION = "archive-upload/1"

# Generous: a colour MKV chunk can be tens of MB.
HTTP_TIMEOUT_S = 180
# post_blob_status sentinel for "no HTTP response at all" (DNS, TCP, timeout).
NO_RESPONSE = 0

# Transient network blips retry with backoff: the first pause, doubling,
# capped so a long outage can't stall a cycle.
MAX_ATTEMPTS = 3
BACKOFF_BASE_S = 1.0
BACKOFF_CAP_S = 30.0

# How long a permanently rejected blob stays parked before one more try. Long
# enough that a stuck file costs ~1 request/day instead of ~200, short enough
# that a file the station later regenerates still recovers on its own.
PERMANENT_RETRY_AFTER_S = 24 * 3600

# The Cloudflare tunnel in front of the VPS refuses a request body over 100 MiB:
# it answers 413, or drops the connection mid-upload, which reads as "no
# response" and feeds the breaker that pauses every other file. The VPS's own
# 256 MB ingest limit is never reached. Measured from the archive on
# 2026-09-28: 104,717,560 bytes got through, 105,684,466 never did.
TUNNEL_MAX_BODY_BYTES = 100 * 1024 * 1024

# A 429 is not a network blip and the generic backoff cannot clear it: the
# ingest blob limit is a per-station per-minute window, while base+cap backoff
# puts every attempt inside its first three seconds. Every attempt then 429s,
# the whole dawn stack flush fails, and the files wait for the next cycle
# (never marked uploaded, so nothing is lost, just late: that is why the
# fleet's morning stacks lagged). Wait out the window instead, without
# spending an attempt, so the retry lands in a fresh one.
RATE_LIMIT_STATUS = 429
RATE_LIMIT_WAIT_S = 60.0
# Bound on how long one file may wait on rate limiting before it is left for
# the next cycle: a station over its hourly budget must not stall the run.
RATE_LIMIT_MAX_WAITS = 2

# The VPS answers 507 when its ingest spool disk is below its free-space
# reserve (ingest_api._spool_full_response). Every file would get the same
# answer, each after re-sending its whole body three times, so the sender
# stops POSTing for the server's Retry-After (300 s) and leaves the rest for a
# later cycle. Nothing is marked uploaded, so nothing is lost.
SPOOL_FULL_STATUS = 507
SPOOL_FULL_PAUSE_S = 300.0

# A server Retry-After is honoured up to these caps, so a bad header cannot
# park an upload pass for hours.
RATE_LIMIT_WAIT_CAP_S = 300.0
SPOOL_FULL_PAUSE_CAP_S = 3600.0

# 401/403: the VPS does not accept this station's ingest key. Every file would
# get the same answer after sending its whole body, so stop POSTing for a
# while instead of parking each file for a day (the key gets fixed on the VPS,
# not by re-sending bytes).
AUTH_STATUSES = (401, 403)
AUTH_PAUSE_S = 1800.0

# The breaker: this many failed attempts in a row with no response or a 5xx
# (VPS down, tunnel down, a silently dropped connection) pause every POST for
# BREAKER_PAUSE_S. Without it an outage cost every pending file three full
# attempts per pass, up to 3 x 180 s each on a dropped connection, and dawn
# waited for the whole queue before stacking. After the pause one attempt
# probes the link; a failure re-opens the breaker straight away.
BREAKER_FAILURES = 5
BREAKER_PAUSE_S = 300.0

# One file that keeps failing on transport must not hold the queue. Once the
# breaker is open every pass sends one probe, and the probe was always the
# file at the head of the queue: on 2026-10-14 gmnro01 (Ghirdoveni) probed the
# same 20.9 MB clip every 20 min from 04:57 to 11:43 and uploaded nothing else.
# After FILE_DEFER_AFTER failed sends a file waits FILE_DEFER_S, so the next
# probe tries another file. Nothing is lost: the file is not marked uploaded.
FILE_DEFER_AFTER = 2
FILE_DEFER_S = 3600.0

# Cloudflare answers 524 when the origin has not responded 100 s after the
# request reached it. Behind the tunnel the VPS answers only once the whole
# body is in, so on a ~150 kB/s uplink any blob over ~15 MB gets a 524 every
# time, however often it is retried (gmnro01 (Ghirdoveni), 2026-10-14). The
# fallback base URL (resolve_fallback_url in rovimen_pusher: the VPS on the
# tailnet, no 100 s limit) is tried after any transport failure on the primary.
CLOUDFLARE_ORIGIN_TIMEOUT = 524
# Once the fallback has carried a file that failed on the primary, sends start
# on the fallback for this long, so a slow station does not spend 100 s on a
# doomed primary attempt per large file; the primary stays the second try.
FALLBACK_PREFER_S = 3600.0

# What a status means for the sender.
OK = "ok"
TRANSIENT = "transient"
RATE_LIMITED = "rate_limited"
SPOOL_FULL = "spool_full"
AUTH = "auth"
PERMANENT = "permanent"


def is_permanent_status(status: Optional[int]) -> bool:
    """True for a 4xx that retrying the identical body cannot fix.

    408 (timeout) and 429 (rate limited) are excluded: those are transient by
    definition and the same body is expected to succeed later.
    """
    return status is not None and 400 <= status < 500 and status not in (408, 429)


def classify(status: int) -> str:
    """What ``status`` means for an upload: one of the verdict constants."""
    if 200 <= status < 300:
        return OK
    if status == RATE_LIMIT_STATUS:
        return RATE_LIMITED
    if status == SPOOL_FULL_STATUS:
        return SPOOL_FULL
    if status in AUTH_STATUSES:
        return AUTH
    if is_permanent_status(status):
        return PERMANENT
    # 5xx, 408, no response, and anything unexpected: worth another try later.
    return TRANSIENT


class BlobStatus(int):
    """An HTTP status that also carries the server's Retry-After (seconds).

    An ``int`` so every caller that compares or logs the status keeps working;
    ``retry_after`` is None when the server sent none.
    """

    retry_after: Optional[float]

    def __new__(cls, status: int, retry_after: Optional[float] = None) -> "BlobStatus":
        obj = int.__new__(cls, status)
        obj.retry_after = retry_after
        return obj


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - time.time())


def post_blob_status(url: str, key: str, data: bytes,
                     timeout: int = HTTP_TIMEOUT_S) -> int:
    """POST raw file bytes to the ingest blob endpoint; return the HTTP status.

    The result is a ``BlobStatus`` carrying the server's Retry-After, if any.
    Returns ``NO_RESPONSE`` (0) when the request never produced a response
    (DNS failure, refused connection, timeout): that is a transport fault, not a
    verdict on the bytes. Never raises; callers decide what a status means.
    """
    try:
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("X-Station-Key", key)
        req.add_header("User-Agent", AGENT_VERSION)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return BlobStatus(int(resp.status))
    except urllib.error.HTTPError as exc:
        logger.warning("blob POST %s -> HTTP %s", url, exc.code)
        headers = exc.headers
        retry_after = _parse_retry_after(headers.get("Retry-After") if headers else None)
        return BlobStatus(int(exc.code), retry_after)
    except Exception as exc:
        logger.warning("blob POST %s failed: %s", url, exc)
        return BlobStatus(NO_RESPONSE)


def post_blob(url: str, key: str, data: bytes, timeout: int = HTTP_TIMEOUT_S) -> bool:
    """POST raw file bytes to the ingest blob endpoint. Returns True on 2xx.

    A 200 with ``skipped`` (idempotent same-size re-POST) also counts as
    success. Never raises: network and HTTP errors are logged and reported as
    False so the caller leaves the file un-marked and retries next cycle.
    """
    return 200 <= post_blob_status(url, key, data, timeout) < 300


Transport = Callable[[str, str, bytes, int], int]
Abort = Callable[[], bool]


def _sleep(seconds: float, should_abort: Optional[Abort]) -> bool:
    """Sleep ``seconds``; False (early) if ``should_abort`` fires meanwhile.

    Slept in 1 s slices when an abort callback is given, so a daemon stop or a
    dawn deadline is seen within a second instead of after a 60 s rate-limit
    wait.
    """
    if should_abort is None:
        time.sleep(seconds)
        return True
    remaining = seconds
    while remaining > 0:
        if should_abort():
            return False
        step = min(remaining, 1.0)
        time.sleep(step)
        remaining -= step
    return not should_abort()


def _base_of(url: str, bases: Optional[Tuple[str, str]]) -> str:
    """Which of ``bases`` ``url`` sits under, for log lines."""
    for base in bases or ():
        if url.startswith(base + "/"):
            return base
    return url


class BlobSender:
    """Retry policy shared by every POST of one upload pass (or daemon).

    ``transport`` is ``post_blob_status`` in production; callers pass their own
    seam so tests can stand in for the network.
    """

    def __init__(self, transport: Optional[Transport] = None,
                 fallback: Optional[Tuple[str, str]] = None) -> None:
        self._transport: Transport = transport or post_blob_status
        # (primary_base, fallback_base): a URL under primary_base is re-tried
        # under fallback_base after a transport failure (see
        # CLOUDFLARE_ORIGIN_TIMEOUT). None, or equal bases, means no fallback.
        self._fallback: Optional[Tuple[str, str]] = None
        if fallback and fallback[0] and fallback[1]:
            primary, alt = fallback[0].rstrip("/"), fallback[1].rstrip("/")
            if primary != alt:
                self._fallback = (primary, alt)
        # url -> (retry_not_before_monotonic, source_size) for files set aside
        # after FILE_DEFER_AFTER failed sends, and url -> failed sends so far.
        self.deferred: Dict[str, Tuple[float, int]] = {}
        self._file_failures: Dict[str, int] = {}
        # Monotonic time until which sends try the fallback first.
        self._prefer_fallback_until = 0.0
        # url -> (retry_not_before_monotonic, reason, source_size). Nothing else
        # stops a rejected file: the uploader only marks a file uploaded on
        # success, so every cycle re-scans and re-POSTs it forever. In-process
        # is the right scope: the daemon restarts nightly, which is the natural
        # re-check.
        self.quarantined: Dict[str, Tuple[float, str, int]] = {}
        # Monotonic time before which no blob is POSTed (spool full, key
        # rejected, or the breaker is open), and why.
        self._paused_until = 0.0
        self._pause_reason = ""
        # Failed attempts in a row with no response or a 5xx (the breaker).
        self._consecutive_failures = 0

    def is_quarantined(self, url: str, size: int) -> bool:
        """True while ``url`` is parked after a permanent rejection.

        The entry is dropped, and the upload retried, as soon as the source
        file changes size (different bytes deserve a fresh verdict) or the
        retry window lapses.
        """
        entry = self.quarantined.get(url)
        if entry is None:
            return False
        expires_at, _reason, seen_size = entry
        if size != seen_size or time.monotonic() >= expires_at:
            del self.quarantined[url]
            return False
        return True

    def quarantine(self, url: str, size: int, reason: str) -> None:
        """Stop retrying ``url``, recording why, and log the reason once.

        Re-logging on every cycle is what turned this failure mode into 11k log
        lines on gmnro10 (Roit), so the log fires only when the file is newly
        parked or its size or reason changed.
        """
        prior = self.quarantined.get(url)
        self.quarantined[url] = (time.monotonic() + PERMANENT_RETRY_AFTER_S, reason, size)
        if prior is None or prior[1] != reason or prior[2] != size:
            logger.warning(
                "archive upload: giving up on %s (%d bytes) — %s; not retried for %.0fh",
                url, size, reason, PERMANENT_RETRY_AFTER_S / 3600)

    def paused(self) -> bool:
        """True while every POST is on hold (spool full, key rejected, breaker)."""
        return time.monotonic() < self._paused_until

    @property
    def pause_reason(self) -> str:
        return self._pause_reason if self.paused() else ""

    def _pause(self, seconds: float, reason: str, label: str) -> None:
        """Hold every POST for ``seconds``; log once per pause, not per file."""
        already = self.paused()
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)
        self._pause_reason = reason
        if not already:
            logger.warning("archive upload: %s — pausing uploads for %.0fs; "
                           "%s and the rest wait for a later pass", reason, seconds, label)

    def is_deferred(self, url: str, size: int) -> bool:
        """True while ``url`` is set aside after repeated transport failures.

        A changed size or a lapsed window ends the deferral, like quarantine.
        """
        entry = self.deferred.get(url)
        if entry is None:
            return False
        until, seen_size = entry
        if size != seen_size or time.monotonic() >= until:
            del self.deferred[url]
            return False
        return True

    def _transport_failed(self, url: str, size: int, label: str) -> bool:
        """Count a send of ``url`` that ended on transport failures; defer the
        file once it reaches FILE_DEFER_AFTER. Always returns False."""
        failures = self._file_failures.get(url, 0) + 1
        self._file_failures[url] = failures
        if failures >= FILE_DEFER_AFTER:
            self._file_failures.pop(url, None)
            self.deferred[url] = (time.monotonic() + FILE_DEFER_S, size)
            logger.warning(
                "archive upload: %s (%d bytes) failed %d sends in a row — set aside "
                "for %.0f min so the rest of the queue can move", label, size,
                failures, FILE_DEFER_S / 60)
        return False

    def _fallback_url(self, url: str) -> Optional[str]:
        """``url`` under the fallback base, or None when there is no fallback."""
        if self._fallback is None:
            return None
        primary, alt = self._fallback
        if url.startswith(primary + "/"):
            return alt + url[len(primary):]
        return None

    def blocked(self, url: str, size: int) -> bool:
        """True when ``url`` must not be POSTed right now."""
        return self.is_quarantined(url, size) or self.is_deferred(url, size) or self.paused()

    def send(self, url: str, key: str, data: bytes, size: int, label: str,
             should_abort: Optional[Abort] = None) -> bool:
        """POST ``data`` to ``url`` under the retry policy. True on success.

        Transient failures (5xx, 408, no response) retry with capped
        exponential backoff and feed the breaker; a 429 waits out the
        rate-limit window without spending an attempt; a 507 or a rejected key
        pauses every upload; any other 4xx parks the file: nothing about
        re-sending identical bytes can change the answer, and a failed file is
        never marked, so without the park the next cycle picks it straight back
        up. gmnro10 (Roit) rode that loop for a week, re-POSTing five zero-byte
        stack WebPs ~186 times each per day. A body over the tunnel's cap is
        parked the same way, without a request. A transport failure is tried
        once on the other route (primary or fallback base) straight away; a file
        whose sends keep failing on transport is deferred (FILE_DEFER_AFTER).
        ``should_abort`` is checked before every attempt and during every wait.
        """
        if self.blocked(url, size):
            return False
        if size > TUNNEL_MAX_BODY_BYTES:
            self.quarantine(
                url, size,
                f"larger than the {TUNNEL_MAX_BODY_BYTES // 2**20} MiB the tunnel "
                "accepts; the station must rebuild it smaller")
            return False
        delay = BACKOFF_BASE_S
        attempt = 0
        rate_limit_waits = 0
        alt = self._fallback_url(url)
        first, second = url, alt
        if alt is not None and time.monotonic() < self._prefer_fallback_until:
            first, second = alt, url
        target = first
        while attempt < MAX_ATTEMPTS:
            if should_abort is not None and should_abort():
                return False
            status = self._transport(target, key, data, HTTP_TIMEOUT_S)
            retry_after = getattr(status, "retry_after", None)
            verdict = classify(status)
            if verdict == OK:
                self.quarantined.pop(url, None)
                self._file_failures.pop(url, None)
                self._consecutive_failures = 0
                if target != first:
                    if target == alt:
                        self._prefer_fallback_until = time.monotonic() + FALLBACK_PREFER_S
                        logger.info("archive upload: %s went through the fallback %s; "
                                    "trying it first for %.0f min", label,
                                    self._fallback[1], FALLBACK_PREFER_S / 60)
                    else:
                        self._prefer_fallback_until = 0.0
                return True
            if verdict != TRANSIENT:
                # The VPS answered: the link works, whatever it thought of us.
                self._consecutive_failures = 0
            if verdict == RATE_LIMITED:
                if rate_limit_waits >= RATE_LIMIT_MAX_WAITS:
                    logger.warning(
                        "archive upload: %s still rate limited after %d waits — "
                        "leaving it for the next cycle", url, rate_limit_waits)
                    return False
                rate_limit_waits += 1
                wait = RATE_LIMIT_WAIT_S if retry_after is None else min(
                    retry_after, RATE_LIMIT_WAIT_CAP_S)
                if not _sleep(wait, should_abort):
                    return False
                continue
            if verdict == SPOOL_FULL:
                pause = SPOOL_FULL_PAUSE_S if retry_after is None else min(
                    max(retry_after, 1.0), SPOOL_FULL_PAUSE_CAP_S)
                self._pause(pause, "VPS ingest spool full (HTTP 507)", label)
                return False
            if verdict == AUTH:
                self._pause(AUTH_PAUSE_S,
                            f"the VPS rejected this station's ingest key (HTTP {status}); "
                            "check ingest.env and the station key on the VPS", label)
                return False
            if verdict == PERMANENT:
                if status == 413:
                    reason = (f"rejected with HTTP 413 — {size / 1024 ** 2:.0f} MB is over "
                              "the ingest body limit (VPS or tunnel)")
                else:
                    reason = f"rejected with HTTP {status} — permanent for these bytes"
                self.quarantine(url, size, reason)
                return False
            if second is not None and target == first:
                # A different route, not a repeat of the failed one: try it at
                # once, before the failure can feed the breaker.
                last = f"HTTP {status}" if status != NO_RESPONSE else "no response"
                logger.warning("archive upload: %s failed via %s (%s) — trying %s",
                               label, _base_of(first, self._fallback), last,
                               _base_of(second, self._fallback))
                target = second
                continue
            self._consecutive_failures += 1
            if self._consecutive_failures >= BREAKER_FAILURES:
                last = f"HTTP {status}" if status != NO_RESPONSE else "no response"
                self._pause(BREAKER_PAUSE_S,
                            f"{self._consecutive_failures} blob POSTs in a row failed "
                            f"(last: {last}); the VPS or its tunnel looks down", label)
                return self._transport_failed(url, size, label)
            attempt += 1
            if attempt < MAX_ATTEMPTS:
                wait = delay if retry_after is None else min(retry_after, BACKOFF_CAP_S)
                if not _sleep(wait, should_abort):
                    return False
                delay = min(delay * 2, BACKOFF_CAP_S)
        return self._transport_failed(url, size, label)
