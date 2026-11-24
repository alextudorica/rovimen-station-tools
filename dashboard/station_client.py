"""HTTP session pool, station API client, and polling cache."""

import json
import logging
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any

import requests
import urllib3.exceptions
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from cache_backend import (
    StationStateBackend,
    make_station_state_backend,
)
from models import DashboardConfig
from segment_health import station_segment_alarms, tag_segment_alarms
from tunnels import _TunnelDown  # noqa: F401 -- re-exported for backward compat

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pooled HTTP sessions for the 30+ outbound station calls
# ---------------------------------------------------------------------------
#
# Every page on the dashboard fans out to several stations over Tailscale
# (~70 ms RTT to Romania). A fresh `requests.get` builds a new TCP+TLS
# connection for every call; with keep-alive plus an HTTPAdapter we save
# 1x round-trip + TLS handshake per call on every cache miss. One Session
# per station base URL means each station's connection pool is independent
# -- a slow station can't starve a fast one.
_station_sessions: dict[str, requests.Session] = {}
_station_sessions_lock = threading.Lock()


def _get_station_session(base_url: str) -> requests.Session:
    """Return a pooled requests.Session for a given station base URL.

    Pool sizing: 30 reused connections, 80 hard cap, non-blocking -- so a
    burst (e.g. overview fan-out across all cameras of one station, or
    parallel /api/vitals + /api/status + thumbnail prefetch) spawns a
    few new sockets rather than blocking workers. `Retry` covers
    transient 5xx blips from station nginx restarts; the backoff (0.2 s)
    is short enough that timeouts still trip on truly dead hosts.
    """
    sess = _station_sessions.get(base_url)
    if sess is not None:
        return sess
    with _station_sessions_lock:
        sess = _station_sessions.get(base_url)
        if sess is not None:
            return sess
        sess = requests.Session()
        # Connect-failure retries are explicitly disabled: when a station is
        # offline, urllib3's default of falling through to `total` would
        # turn a configured 10s timeout into ~30s of wall time (initial +
        # 2 retries with backoff). That dominated /api/config/all and
        # /api/settings/<offline-host> latency. Read retries are also off
        # for the same reason -- a dead station should fail fast, not stall.
        # We only retry on transient 5xx (nginx restart, etc.).
        retry = Retry(
            total=2,
            connect=0,
            read=0,
            backoff_factor=0.2,
            status_forcelist=(502, 503, 504),
            allowed_methods=frozenset({"GET", "HEAD", "POST", "PATCH", "PUT", "DELETE"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(
            pool_connections=30,
            pool_maxsize=80,
            max_retries=retry,
            pool_block=False,
        )
        sess.mount("http://", adapter)
        sess.mount("https://", adapter)
        _station_sessions[base_url] = sess
        return sess


def _base_of_url(url: str) -> str:
    """``scheme://host:port`` of a full URL (the whole string if it has no path)."""
    try:
        scheme_end = url.index("://") + 3
        path_start = url.find("/", scheme_end)
    except ValueError:
        return url
    return url if path_start < 0 else url[:path_start]


def _raw_session_for_url(url: str) -> requests.Session:
    """The pooled ``requests.Session`` for a URL's ``scheme://host:port``, with
    no reachability breaker in front of it. Only the pollers' probe
    (``station_get_status``) should use this directly."""
    return _get_station_session(_base_of_url(url))


def _session_for_url(url: str) -> "_BreakerSession":
    """Resolve `http(s)://host:port` prefix from a full URL so the same
    Session is reused for /api/foo and /color_capture/... on one station.

    Returns the pooled session wrapped in the station reachability breaker
    (see below): every ``get``/``post``/``head`` through it fails fast while
    the station's breaker is open, gets the connect-timeout cap, and feeds the
    breaker with its outcome. The station is recognised from the URL's base,
    which ``station_url`` records for every base it hands out, so the ~40
    media/proxy call sites that build a station URL and fetch it themselves
    are covered without each having to know about the breaker.
    """
    base = _base_of_url(url)
    host_key = _host_for_base(base)
    return _BreakerSession(_get_station_session(base), base if host_key else None, host_key)


def _evict_station_session(base_url: str) -> None:
    """Remove and close the pooled Session for a base URL (e.g. after a tunnel
    reconnects on a new port). Without this, orphaned Sessions keyed by dead
    ``127.0.0.1:<old_port>`` accumulate connection pools that never get reused."""
    with _station_sessions_lock:
        sess = _station_sessions.pop(base_url, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def _streaming_proxy(resp, chunk_size: int = 65536):
    """Yield chunks from a ``requests.Response`` opened with ``stream=True``,
    guaranteeing the upstream connection is closed when the generator exits
    (including on client disconnect / GeneratorExit). Without this wrapper,
    abandoned ``iter_content()`` generators leave the Response object -- and its
    socket buffer -- alive until GC collects it, leaking ~32 KB-several MB per
    abandoned request."""
    try:
        for chunk in resp.iter_content(chunk_size=chunk_size):
            yield chunk
    except GeneratorExit:
        pass
    finally:
        try:
            resp.close()
        except Exception:
            pass


_sshfs_leak_count = 0
_sshfs_leak_lock = threading.Lock()
_SSHFS_CIRCUIT_BREAKER_THRESHOLD = 15

# Global concurrency bound on storage-box ops, independent of the leak breaker.
# Even when the mount is merely SLOW (not hung), a fan-out burst -- e.g. the
# highlights range = up to 10 dates x ~15 cameras -- would otherwise spawn ~150
# threads all hammering the single SSHFS mount AND consuming the worker's thread
# pool, saturating both -> 504s. At most _SSHFS_MAX_CONCURRENCY run at once;
# excess callers fail fast to *default* (degrade to empty/cached) rather than
# queueing. A permit is held until the op ACTUALLY finishes (released in the
# runner), so a stuck/leaked op keeps counting against the mount's real load.
_SSHFS_MAX_CONCURRENCY = int(os.environ.get("ROVIMEN_SSHFS_MAX_CONCURRENCY", "6"))
_SSHFS_ACQUIRE_TIMEOUT = float(os.environ.get("ROVIMEN_SSHFS_ACQUIRE_TIMEOUT", "2.0"))
_sshfs_semaphore = threading.BoundedSemaphore(_SSHFS_MAX_CONCURRENCY)

# Reusable worker pool for guarded ops. Previously every call spawned a fresh
# OS thread (create/destroy churn on the hot path under load); the pool reuses
# threads instead. Sized ABOVE the concurrency cap so a permitted submit always
# finds a free worker even while timed-out ("leaked") ops keep their thread busy
# until the underlying syscall finally returns — the semaphore, not the pool, is
# the real concurrency bound.
_SSHFS_POOL = ThreadPoolExecutor(
    max_workers=_SSHFS_MAX_CONCURRENCY + 4, thread_name_prefix="sshfs-guard"
)

# Who holds the read-lane permits right now: token -> (call site, start). The
# "limit reached" warning used to say only that the lane was full, which left
# 12,600 sheds/day on 2026-09-22 with no way to tell which route or job was
# holding it. The warning now names the holders, at most every 10 s, with the
# count of sheds it suppressed in between.
_sshfs_inflight: dict[int, tuple[str, float]] = {}
_sshfs_inflight_lock = threading.Lock()
_sshfs_shed_log_interval_s = 10.0
_sshfs_shed_last_log = float("-inf")
_sshfs_shed_suppressed = 0


def _sshfs_call_site() -> str:
    """``function@file:line`` of the code that called the guard."""
    try:
        f = sys._getframe(2)
    except ValueError:
        return "?"
    return f"{f.f_code.co_name}@{os.path.basename(f.f_code.co_filename)}:{f.f_lineno}"


def _log_sshfs_shed(site: str) -> None:
    global _sshfs_shed_last_log, _sshfs_shed_suppressed
    now = time.monotonic()
    with _sshfs_inflight_lock:
        if now - _sshfs_shed_last_log < _sshfs_shed_log_interval_s:
            _sshfs_shed_suppressed += 1
            return
        suppressed = _sshfs_shed_suppressed
        _sshfs_shed_suppressed = 0
        _sshfs_shed_last_log = now
        holders = sorted(_sshfs_inflight.values(), key=lambda h: h[1])
    held = ", ".join(f"{name} {now - start:.1f}s" for name, start in holders)
    logger.warning(
        "SSHFS concurrency limit (%d) reached; returning default to %s "
        "(+%d shed since last report); holders: %s",
        _SSHFS_MAX_CONCURRENCY, site, suppressed, held or "none",
    )


def _with_sshfs_timeout(fn, timeout: float = 5.0, default=None):
    """Run *fn* in a worker thread; if it doesn't return within *timeout*
    seconds, return *default* and log a warning.

    SSHFS reads against the Hetzner Storage Box can hang for minutes when
    the mount is being remounted (or the box is rebooting). Wrapping
    metadata walks in this guard keeps a stuck mount from blocking Flask
    threads indefinitely -- the worker thread is left to finish in the
    background and gets garbage-collected when the function eventually
    returns.

    A circuit breaker trips after ``_SSHFS_CIRCUIT_BREAKER_THRESHOLD``
    *currently active* leaked threads -- at that point every call returns
    *default* immediately without spawning a new thread.  When a leaked
    thread eventually completes, the counter decrements so the breaker
    can recover without a restart.
    """
    global _sshfs_leak_count
    with _sshfs_leak_lock:
        if _sshfs_leak_count >= _SSHFS_CIRCUIT_BREAKER_THRESHOLD:
            logger.critical(
                "SSHFS circuit breaker: %d leaked threads, returning default",
                _sshfs_leak_count,
            )
            return default

    # Bound concurrent storage-box ops. Fail fast to *default* if no permit
    # frees up quickly, so a fan-out burst degrades instead of piling up.
    site = _sshfs_call_site()
    if not _sshfs_semaphore.acquire(timeout=_SSHFS_ACQUIRE_TIMEOUT):
        _log_sshfs_shed(site)
        return default

    token = object()
    with _sshfs_inflight_lock:
        _sshfs_inflight[id(token)] = (site, time.monotonic())
    leaked = False
    done = threading.Event()
    box: dict[str, Any] = {"value": default}

    def _runner() -> None:
        global _sshfs_leak_count
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = default
        finally:
            done.set()
            with _sshfs_inflight_lock:
                _sshfs_inflight.pop(id(token), None)
            # Release only when the op ACTUALLY finished (even if it already
            # timed out and returned default) — a stuck op keeps its permit so
            # the concurrency bound reflects real mount load.
            try:
                _sshfs_semaphore.release()
            except ValueError:
                pass
            if leaked:
                with _sshfs_leak_lock:
                    _sshfs_leak_count -= 1
                logger.info(
                    "SSHFS leaked thread recovered (active: %d)",
                    _sshfs_leak_count,
                )

    # Run on the reusable pool. A permit is held, and the pool is sized above the
    # permit cap, so this submit never queues behind a busy worker.
    _SSHFS_POOL.submit(_runner)
    if not done.wait(timeout):
        leaked = True
        with _sshfs_leak_lock:
            _sshfs_leak_count += 1
        logger.warning(
            "SSHFS operation timed out after %.1fs at %s (leaked threads: %d)",
            timeout, site, _sshfs_leak_count,
        )
        return default
    return box["value"]


# ---------------------------------------------------------------------------
# Dedicated flush lane
# ---------------------------------------------------------------------------
# The blob-spool flusher archives pushed clips/stacks to the Storage Box. It
# MUST NOT share the read lane's permits above: under a read-traffic burst it
# would never win one of the 6 slots, bounce every file back to the spool, and
# livelock — a single slow station's backlog then accumulates and fills the VPS
# root disk (observed: gmnro02, ~16k files / 4.2 GB stuck for two weeks, nightly
# disk-full cascade). A separate lane isolates flush writes from interactive
# reads. It also WAITS for a permit instead of failing fast: a background writer
# can afford to queue, so it drains steadily rather than shedding to the spool.
# Same leak/circuit-breaker semantics so a genuinely hung mount can't pile up
# flush threads unbounded.
_FLUSH_SSHFS_MAX_CONCURRENCY = int(
    os.environ.get("ROVIMEN_SSHFS_FLUSH_CONCURRENCY", "3")
)
_FLUSH_SSHFS_ACQUIRE_TIMEOUT = float(
    os.environ.get("ROVIMEN_SSHFS_FLUSH_ACQUIRE_TIMEOUT", "30.0")
)
_flush_sshfs_semaphore = threading.BoundedSemaphore(_FLUSH_SSHFS_MAX_CONCURRENCY)
_FLUSH_SSHFS_POOL = ThreadPoolExecutor(
    max_workers=_FLUSH_SSHFS_MAX_CONCURRENCY + 2, thread_name_prefix="sshfs-flush"
)
_flush_sshfs_leak_count = 0
_flush_sshfs_leak_lock = threading.Lock()


def with_sshfs_flush_timeout(fn, timeout: float = 60.0, default=None):
    """Guard for blob-spool flush writes — a lane SEPARATE from the read guard
    so a read-traffic burst can't starve the flusher into a spool livelock.

    Unlike ``_with_sshfs_timeout`` it WAITS up to ``_FLUSH_SSHFS_ACQUIRE_TIMEOUT``
    for a permit (a background writer can queue) rather than degrading on the
    first busy moment. A permit is held until the op truly finishes, and the
    same circuit breaker trips after ``_SSHFS_CIRCUIT_BREAKER_THRESHOLD`` leaked
    threads so a hung mount can't spawn flush threads without bound.
    """
    global _flush_sshfs_leak_count
    with _flush_sshfs_leak_lock:
        if _flush_sshfs_leak_count >= _SSHFS_CIRCUIT_BREAKER_THRESHOLD:
            logger.critical(
                "SSHFS flush circuit breaker: %d leaked threads, deferring",
                _flush_sshfs_leak_count,
            )
            return default

    if not _flush_sshfs_semaphore.acquire(timeout=_FLUSH_SSHFS_ACQUIRE_TIMEOUT):
        logger.warning(
            "SSHFS flush lane saturated (%d); deferring file to next pass",
            _FLUSH_SSHFS_MAX_CONCURRENCY,
        )
        return default

    leaked = False
    done = threading.Event()
    box: dict[str, Any] = {"value": default}

    def _runner() -> None:
        global _flush_sshfs_leak_count
        try:
            box["value"] = fn()
        except Exception:
            box["value"] = default
        finally:
            done.set()
            try:
                _flush_sshfs_semaphore.release()
            except ValueError:
                pass
            if leaked:
                with _flush_sshfs_leak_lock:
                    _flush_sshfs_leak_count -= 1
                logger.info(
                    "SSHFS flush leaked thread recovered (active: %d)",
                    _flush_sshfs_leak_count,
                )

    _FLUSH_SSHFS_POOL.submit(_runner)
    if not done.wait(timeout):
        leaked = True
        with _flush_sshfs_leak_lock:
            _flush_sshfs_leak_count += 1
        logger.warning(
            "SSHFS flush op timed out after %.1fs (leaked threads: %d)",
            timeout, _flush_sshfs_leak_count,
        )
        return default
    return box["value"]


# ---------------------------------------------------------------------------
# Station API client
# ---------------------------------------------------------------------------


def station_url(
    config: DashboardConfig, tunnels, host_key: str, path: str
) -> str:
    # ``path`` is handed to get_api_base as well as appended: for a station
    # split across two boxes it selects which box serves this camera-scoped
    # request (StationConfig.host_for_path). Single-box stations are
    # unaffected.
    base = tunnels.get_api_base(host_key, path)
    if base is None:
        raise _TunnelDown(host_key)
    _remember_base(base, host_key)
    return f"{base}{path}"


# ── Station reachability breaker ─────────────────────────────────────────
#
# A station the VPS cannot reach over Tailscale drops every SYN, so each call
# to it costs the full connect timeout (10 s on most call sites) and holds a
# gthread for that long. Nothing remembered the failure: an offline station
# was re-probed by every request that touched it — the calibration page alone
# fires ~45 camera requests, the RMS tab one per night — so a single dead
# peer could pin all 32 threads of a worker for 10 s+ at a time while cheap
# endpoints queued behind it and 504ed at nginx. This breaker remembers a
# connection-level failure per station and makes request-path calls fail
# fast (no network) until the window expires; the window doubles while the
# station stays dark and clears on the first success. Read timeouts and HTTP
# errors do NOT trip it: the station answered, it is merely slow or unhappy.
#
# The status/vitals pollers keep probing on their own cadence regardless of
# the breaker (they are the cheap, bounded, thread-pooled path that notices
# recovery) and their results feed it, so a station that comes back is seen
# within one poll cycle rather than one breaker window.

_BREAKER_BASE_S = float(os.environ.get("ROVIMEN_STATION_BREAKER_BASE_S", "60"))
_BREAKER_MAX_S = float(os.environ.get("ROVIMEN_STATION_BREAKER_MAX_S", "480"))
# Connect timeout applied to every station call. Tailscale peers answer a SYN
# in well under a second (direct or DERP-relayed); a scalar ``timeout=10``
# spent the full 10 s on a dead peer. The read half keeps the caller's value.
_STATION_CONNECT_TIMEOUT_S = float(os.environ.get("ROVIMEN_STATION_CONNECT_TIMEOUT_S", "5"))

_breaker_lock = threading.Lock()
# Keyed by the station BASE URL (scheme://host:port), i.e. one physical box,
# not by host_key: a dashboard station can span two boxes (per-camera ``host``
# override), and a dead second box must not blackhole the first.
# base -> (open_until_monotonic, consecutive_failures)
_breaker_state: dict[str, tuple[float, int]] = {}


class StationUnreachable(requests.exceptions.ConnectionError):
    """Raised without touching the network while a station's breaker is open.

    Subclasses ``ConnectionError`` so every existing ``except
    requests.RequestException`` / ``except Exception`` around a station call
    keeps working unchanged.
    """


def _station_timeout(timeout: float | tuple) -> float | tuple[float, float]:
    """Cap the connect half of a timeout; leave tuples the caller chose alone."""
    if isinstance(timeout, tuple):
        return timeout
    try:
        read = float(timeout)
    except (TypeError, ValueError):
        return timeout
    return (min(_STATION_CONNECT_TIMEOUT_S, read), read)


def station_breaker_open(base: str) -> bool:
    """True while request-path calls to the box at ``base`` should fail fast."""
    with _breaker_lock:
        entry = _breaker_state.get(base)
    return bool(entry) and time.monotonic() < entry[0]


def station_breaker_trip(base: str, exc: BaseException | None = None, label: str | None = None) -> None:
    """Record a connection-level failure; open (or lengthen) the window."""
    with _breaker_lock:
        _, failures = _breaker_state.get(base, (0.0, 0))
        failures += 1
        # Cap the exponent: a station that stays dark for weeks trips this once
        # per poll cycle, and 2 ** 1024 overflows a float. The window saturates
        # at _BREAKER_MAX_S long before that anyway.
        window = min(_BREAKER_BASE_S * (2 ** min(failures - 1, 16)), _BREAKER_MAX_S)
        _breaker_state[base] = (time.monotonic() + window, failures)
    if failures == 1:
        logger.warning(
            "station %s (%s) unreachable (%s) — failing fast for %.0fs",
            label or _host_for_base(base) or "?", base,
            exc.__class__.__name__ if exc else "connect", window,
        )


def station_breaker_reset(base: str, label: str | None = None) -> None:
    """A successful connection: forget the failure history."""
    with _breaker_lock:
        if _breaker_state.pop(base, None) is not None:
            logger.info("station %s (%s) reachable again — breaker cleared",
                        label or _host_for_base(base) or "?", base)


def _breaker_check(base: str, label: str | None = None) -> None:
    if station_breaker_open(base):
        raise StationUnreachable(f"{label or base}: breaker open, station unreachable")


def _is_connect_failure(exc: BaseException) -> bool:
    """True only for "could not reach the box" failures.

    requests wraps a refused/unroutable connection as ``ConnectionError``
    carrying urllib3's ``MaxRetryError`` (or as ``ConnectTimeout``). A reset
    or EOF in the middle of an answered request arrives as ``ConnectionError``
    carrying a ``ProtocolError`` — the box was reachable, its API merely
    restarted or dropped one request — and must not open a fail-fast window
    for every route. ``ReadTimeout`` (only a ``Timeout``) and TLS/proxy errors
    are likewise answers, not silence.
    """
    if isinstance(exc, StationUnreachable):
        return False
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    if isinstance(exc, (requests.exceptions.SSLError, requests.exceptions.ProxyError)):
        return False
    if isinstance(exc, requests.exceptions.ConnectionError):
        cause = exc.args[0] if exc.args else None
        return isinstance(cause, urllib3.exceptions.MaxRetryError)
    return False


# scheme://host:port -> host_key, filled by station_url() so a plain URL can be
# attributed to its station. Bases are per station (its Tailscale IP or its
# tunnel's local port), so the mapping is unambiguous; a tunnel that moves to
# a new port simply records the new base on its next station_url() call.
_base_to_host: dict[str, str] = {}
_base_to_host_lock = threading.Lock()


def _remember_base(base: str, host_key: str) -> None:
    with _base_to_host_lock:
        if _base_to_host.get(base) != host_key:
            _base_to_host[base] = host_key


def _host_for_base(base: str) -> str | None:
    with _base_to_host_lock:
        return _base_to_host.get(base)


class _BreakerSession:
    """A ``requests.Session`` front that applies the reachability breaker.

    Every HTTP verb goes through :meth:`request`: it raises
    :class:`StationUnreachable` without touching the network while the
    station's breaker is open, caps the connect half of a scalar timeout,
    trips the breaker on a connection-level failure and clears it on any
    answer. A URL whose base no station has claimed passes straight through
    (only the connect cap applies). Everything else (``close()``, ``headers``,
    ...) is delegated to the wrapped session.
    """

    __slots__ = ("_sess", "base", "host_key")

    def __init__(self, sess: requests.Session, base: str | None, host_key: str | None) -> None:
        self._sess = sess
        self.base = base            # breaker key; None = no station claimed this base
        self.host_key = host_key    # label for logs

    def request(self, method: str, url: str, **kwargs: Any):
        if self.base:
            _breaker_check(self.base, self.host_key)
        if "timeout" in kwargs and kwargs["timeout"] is not None:
            kwargs["timeout"] = _station_timeout(kwargs["timeout"])
        # Delegate to the verb method (not Session.request) so requests' own
        # per-verb defaults keep applying — e.g. HEAD does not follow redirects.
        verb = getattr(self._sess, method.lower())
        try:
            resp = verb(url, **kwargs)
        except Exception as exc:
            if self.base and _is_connect_failure(exc):
                station_breaker_trip(self.base, exc, self.host_key)
            raise
        if self.base:
            station_breaker_reset(self.base, self.host_key)
        return resp

    def get(self, url: str, **kwargs: Any):
        return self.request("GET", url, **kwargs)

    def head(self, url: str, **kwargs: Any):
        return self.request("HEAD", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self.request("POST", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self.request("PUT", url, **kwargs)

    def patch(self, url: str, **kwargs: Any):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self.request("DELETE", url, **kwargs)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sess, name)


def station_get_status(
    config: DashboardConfig,
    tunnels,
    host_key: str,
    path: str,
    timeout: int = 10,
) -> dict[str, Any]:
    """Fetch a status/vitals endpoint that returns a dict. Adds online/timestamp metadata.

    This is the pollers' probe: it is never blocked by an open breaker (so
    recovery is noticed on the poll cadence), but its outcome feeds it.
    """
    base = None
    try:
        url = station_url(config, tunnels, host_key, path)
        base = _base_of_url(url)
        # Raw session on purpose: the probe must run while the breaker is open.
        sess = _raw_session_for_url(url)
        resp = sess.get(url, timeout=_station_timeout(timeout))
        resp.raise_for_status()
        data = resp.json()
        station_breaker_reset(base, host_key)
        data["online"] = True
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        return data
    except Exception as exc:
        if base and _is_connect_failure(exc):
            station_breaker_trip(base, exc, host_key)
        return {
            "online": False,
            "error": str(exc),
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }


def station_get_raw(
    config: DashboardConfig,
    tunnels,
    host_key: str,
    path: str,
    timeout: int = 10,
) -> Any:
    """Fetch a station endpoint and return the raw JSON (list or dict).

    Raises :class:`StationUnreachable` immediately (no network) while the
    station's breaker is open; otherwise one attempt whose connect timeout is
    capped at ``_STATION_CONNECT_TIMEOUT_S``.
    """
    url = station_url(config, tunnels, host_key, path)
    resp = _BreakerSession(_raw_session_for_url(url), _base_of_url(url), host_key).get(
        url, timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def station_session_get(
    host_key: str, url: str, *, timeout: float | tuple = 10, **kwargs: Any
):
    """``_session_for_url(url).get(url, ...)`` with the breaker and the connect
    cap applied — for the routes that stream or need the raw ``Response``.

    Raises :class:`StationUnreachable` while the breaker is open; the caller's
    existing ``except`` handles it like any other connection failure.
    """
    return _BreakerSession(_raw_session_for_url(url), _base_of_url(url), host_key).get(
        url, timeout=timeout, **kwargs,
    )


# ---------------------------------------------------------------------------
# Cache and background polling
# ---------------------------------------------------------------------------


# Cap on simultaneous SSE subscribers. Each connection holds a thread (Flask's
# threaded WSGI) plus a bounded queue, so the memory cost is modest, but a
# runaway reconnect storm from a buggy client could pin the dashboard. 32 is
# far above any realistic legitimate ceiling (we have <10 humans on the team).
_MAX_STATUS_LISTENERS = 32

# Per-listener queue depth. Status events are infrequent (a station changes
# online/services state seconds-rarely, hours-typically), so even 16 would be
# plenty; 64 leaves room for a brief consumer stall without dropping events.
_STATUS_LISTENER_QUEUE_MAX = 64

# A push station whose newest heartbeat is older than this is presented as
# offline. Pushes are the ONLY thing that refreshes a push station's cached
# status (the pollers never contact it), so without this gate a dead station
# keeps its last "online": true snapshot forever — gmnro13 showed online for
# 11 h after it went dark (2026-07-10). Heartbeats arrive every ~45 s
# (rovimen_pusher HEARTBEAT_INTERVAL), so 300 s ≈ 6 missed beats: late enough
# to ride out ingest hiccups, early enough that the dashboard is honest.
PUSH_STALE_SECS = int(os.environ.get("ROVIMEN_PUSH_STALE_SECS", "300"))

# Sentinel fields used to decide whether two status payloads are "meaningfully"
# different. We deliberately ignore wall-clock timestamps the station API
# stamps on every reply -- comparing those would push an event every poll cycle
# even when nothing changed, defeating the point of SSE.
_STATUS_DIFF_FIELDS = (
    "online",
    "services",
    "disk",
    "storage",
    "extra_disks",
    "cameras",
    "color_capture_running",
    "rms_running",
    # Per-camera colour-segment cadence. A clip storm develops inside a single
    # poll cycle, so the SSE fan-out has to notice it changing.
    "segment_health",
    # Storage-card banners: a push station's janitor pressure episode and its
    # colour-capture fallback have to reach open pages when they start.
    "disk_pressure",
    "capture_fallback",
)

# Fields whose raw value churns on every poll -- a moving newest-segment
# timestamp, a growing segment count -- while only their derived alarm state
# reaches the UI. Reduced to that state before entering the signature, so
# adding such a field keeps SSE event-on-change instead of event-per-poll.
_STATUS_DIFF_REDUCERS: dict[str, Any] = {
    "segment_health": station_segment_alarms,
}


def _status_diff_signature(data: dict[str, Any] | None) -> str:
    """Compact signature for change-detection. Stable json.dumps over the
    fields we actually surface in the UI; anything outside `_STATUS_DIFF_FIELDS`
    (e.g. server-stamped `updated_at`) is ignored to avoid spurious events."""
    if not data:
        return ""
    snapshot: dict[str, Any] = {}
    for f in _STATUS_DIFF_FIELDS:
        if f not in data:
            continue
        reduce_fn = _STATUS_DIFF_REDUCERS.get(f)
        snapshot[f] = reduce_fn(data[f]) if reduce_fn else data[f]
    return json.dumps(snapshot, sort_keys=True, default=str, separators=(",", ":"))


def _push_status_age_seconds(status: dict[str, Any]) -> float | None:
    """Age of a pushed status snapshot in seconds, or None when it carries no
    usable timestamp. Prefers the ingest-stamped ``received_at`` (server clock,
    immune to station clock skew); falls back to ``last_updated`` (the
    station-stamped ``sent_at``) for entries cached before received_at existed."""
    raw = status.get("received_at") or status.get("last_updated")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - ts).total_seconds()


class StationCache:
    def __init__(self, backend: StationStateBackend | None = None) -> None:
        # The raw last-value store for pushed/polled status + vitals. Defaults
        # to the per-process in-memory backend (unchanged single-worker
        # behaviour); when ROVIMEN_REDIS_URL is set, make_station_state_backend
        # returns a shared Redis backend so every gunicorn worker reads/writes
        # the same telemetry. No redis types cross this boundary.
        self._backend: StationStateBackend = (
            backend if backend is not None else make_station_state_backend()
        )
        # Last signature per host -- used by set_status to suppress no-op
        # broadcasts. Kept inside the lock so writers see a consistent view.
        # This stays per-process: it only gates the local SSE fan-out, and a
        # spurious broadcast on a worker that hasn't seen the prior value is
        # harmless (the payload is identical, clients dedup on content).
        self._status_sig: dict[str, str] = {}
        self._lock = threading.Lock()
        # Pub/sub for /api/events/status. Listeners are bounded Queues of
        # pre-encoded SSE event bytes (see _broadcast_status). On a full queue
        # we drop the event (slow-consumer policy) rather than block the
        # polling thread that owns set_status.
        self._status_listeners: set[queue.Queue[bytes]] = set()
        self._status_listeners_lock = threading.Lock()
        # Monotonic version bumped on any status write. Snapshot encoders share
        # cached bytes across listeners that arrive within the same version.
        self._cache_version: int = 0
        self._snapshot_bytes: tuple[int, bytes] | None = None

    def get_status(self, host_key: str) -> dict[str, Any] | None:
        return self._backend.get_status(host_key)

    def set_status(self, host_key: str, data: dict[str, Any]) -> None:
        # Stamp the segment-cadence verdict onto each camera's block once, on
        # the way in, so every reader of a cached status -- the SSE stream,
        # /api/status/<host>, /api/status/all -- sees the same decision and no
        # threshold gets re-implemented in the frontend. No-op on a payload
        # without the field (a station bundle predating it).
        data = tag_segment_alarms(data)
        with self._lock:
            new_sig = _status_diff_signature(data)
            prev_sig = self._status_sig.get(host_key)
            self._status_sig[host_key] = new_sig
            self._cache_version += 1
            self._snapshot_bytes = None
            changed = new_sig != prev_sig
        # Write to the (possibly shared) backend outside the local lock — the
        # backend has its own synchronisation and may do network I/O (Redis).
        self._backend.set_status(host_key, data)
        if changed:
            payload = json.dumps(
                {"host": host_key, "status": data}, default=str
            )
            event = f"event: status\ndata: {payload}\n\n".encode("utf-8")
            self._broadcast_status(event)

    def get_vitals(self, host_key: str) -> dict[str, Any] | None:
        return self._backend.get_vitals(host_key)

    def set_vitals(self, host_key: str, data: dict[str, Any]) -> None:
        self._backend.set_vitals(host_key, data)

    # -- live thumbnails (pushed newest-FF maxpixel WebP) ------------------
    def set_live_thumb(
        self,
        host_key: str,
        cam: str,
        data: bytes,
        *,
        content_type: str = "image/webp",
        ff_timestamp: str | None = None,
    ) -> None:
        """Store the latest pushed live thumbnail for a (station, cam).

        Last-writer-wins, keyed by camera. Delegates to the backend so the
        thumbnail is shared across gunicorn workers when a Redis backend is
        configured, and behaves exactly as the historical per-process dict when
        it isn't. ``received_at`` is stamped by the backend on write."""
        self._backend.set_live_thumb(
            host_key, cam, data, content_type, ff_timestamp
        )

    def get_live_thumb(self, host_key: str, cam: str) -> dict[str, Any] | None:
        """Return the latest pushed live thumbnail for a (station, cam), or None."""
        return self._backend.get_live_thumb(host_key, cam)

    # -- SSE pub/sub plumbing ----------------------------------------------
    def snapshot_bytes(self, host_keys: tuple[str, ...]) -> bytes:
        """Build the SSE `snapshot` event bytes for the given station ordering.
        Cached across concurrent registrations within the same cache version so
        a reconnect storm doesn't re-encode the same payload per client."""
        with self._lock:
            cached = self._snapshot_bytes
            version = self._cache_version
            if cached is not None and cached[0] == version:
                return cached[1]
        # Read the (possibly shared) backend outside the local lock — it may
        # do network I/O. Re-check the version cache after building in case a
        # concurrent write bumped it; worst case we build one extra snapshot.
        snapshot = self._backend.all_status(tuple(host_keys))
        payload = json.dumps(snapshot, default=str)
        event = f"event: snapshot\ndata: {payload}\n\n".encode("utf-8")
        with self._lock:
            self._snapshot_bytes = (version, event)
            return event

    def can_register_status_listener(self) -> bool:
        """Cheap, lock-free-ish probe used to gate the SSE route's 503 response
        before we enter the generator. The actual register call still re-checks
        the cap under the lock (the count may have grown between the probe and
        the registration), so this is purely an early-return optimisation that
        keeps the 503 path out of the streaming response path."""
        with self._status_listeners_lock:
            return len(self._status_listeners) < _MAX_STATUS_LISTENERS

    def register_status_listener(self) -> queue.Queue[bytes] | None:
        """Register a new SSE listener. Returns the queue to consume from,
        or None if the per-process listener cap has been hit (caller must
        respond with 503 -- the client's EventSource will retry on its own
        backoff, which is the desired behaviour during a transient storm)."""
        q: queue.Queue[bytes] = queue.Queue(maxsize=_STATUS_LISTENER_QUEUE_MAX)
        with self._status_listeners_lock:
            if len(self._status_listeners) >= _MAX_STATUS_LISTENERS:
                logger.warning(
                    "status SSE listener cap (%d) reached -- rejecting new subscriber",
                    _MAX_STATUS_LISTENERS,
                )
                return None
            self._status_listeners.add(q)
        return q

    def unregister_status_listener(self, q: queue.Queue[bytes]) -> None:
        with self._status_listeners_lock:
            self._status_listeners.discard(q)

    def _broadcast_status(self, event: bytes) -> None:
        """Fan out a pre-encoded SSE event to every subscribed queue. Snapshot
        the listener set under its lock so the iteration can't trip on a
        concurrent register/unregister. Drop on full -- never block the polling
        thread."""
        with self._status_listeners_lock:
            listeners = tuple(self._status_listeners)
        for q in listeners:
            try:
                q.put_nowait(event)
            except queue.Full:
                # Slow consumer -- they'll resync on the next change or via
                # the periodic fallback fetch from the client.
                logger.debug("status SSE listener queue full -- dropping event")


def start_polling(
    config: DashboardConfig, tunnels, cache: StationCache
) -> None:
    # Pollers fan out across stations in parallel. A single offline station
    # used to stall the loop because the 90-second per-station timeout
    # blocked all subsequent stations in the same iteration; with N=8 and
    # 1 offline station the rest would lag by 90 s+. ThreadPoolExecutor +
    # tight per-station timeouts keeps each cycle bounded to ~15 s regardless
    # of how many stations are dead.
    _offline_streak: dict[str, int] = {}

    def _skip_poll(key: str) -> bool:
        """True when the poller must NOT contact this station:

        - it's on the push path (its status/vitals cache is owned by the
          ingest API), or
        - it's not contactable at all — commissioning / no cameras / no IP
          (see ``StationConfig.should_contact``). Polling a commissioning box
          whose IP is a dead Tailscale peer wedges the tailnet path.

        Fail-safe: an unknown/malformed entry defaults to skipping.
        """
        st = config.stations.get(key)
        if st is None:
            return True
        if bool(getattr(st, "push_enabled", False)):
            return True
        return not st.should_contact

    def _demote_stale_push(key: str) -> None:
        """Present a push station as offline once its heartbeats stop.

        Ingest owns the cache entry for push stations, but ingest only writes
        on ARRIVING pushes — when a station dies, nothing ever flips its frozen
        ``"online": true`` snapshot (gmnro13 showed online for 11 h after going
        dark). This runs every status cycle and demotes the cached entry once
        it is older than PUSH_STALE_SECS. The set_status diff/fan-out then
        broadcasts the transition to SSE clients exactly once — the demoted
        payload flips ``online`` (a diff field), and later cycles no-op because
        an already-offline entry is skipped below. The original timestamps are
        kept so the UI can show when the station was last seen; the next
        accepted heartbeat overwrites the whole entry and the station comes
        back online with no extra bookkeeping."""
        status = cache.get_status(key)
        if not status:
            return
        if not status.get("online"):
            if status.get("stale"):
                # Already demoted (perhaps by another worker, or before a
                # restart): make sure the event history has the outage too.
                # mark_offline is idempotent and memoised per process.
                _record_push_offline(key, status)
            return
        age = _push_status_age_seconds(status)
        if age is None or age < PUSH_STALE_SECS:
            return
        demoted = dict(status)
        demoted["online"] = False
        demoted["stale"] = True
        demoted["error"] = (
            "no push received since "
            f"{status.get('received_at') or status.get('last_updated')}"
        )
        cache.set_status(key, demoted)
        logger.warning(
            "%s: push heartbeats stale (%ds > %ds) — marked offline",
            key, int(age), PUSH_STALE_SECS,
        )
        _record_push_offline(key, status)

    def _record_push_offline(key: str, status: dict[str, Any]) -> None:
        """Append the ``offline`` event to the station's history (once per
        outage across all workers; see station_events.mark_offline)."""
        try:
            import station_events

            station_events.mark_offline(
                key, status.get("received_at") or status.get("last_updated"))
        except Exception:
            logger.exception("%s: could not record offline event", key)

    def _poll_one_status(key: str) -> None:
        st = config.stations.get(key)
        if st is not None and bool(getattr(st, "push_enabled", False)):
            # Never contact a push station — ingest owns its cache entry —
            # but do verify its pushes are still arriving.
            _demote_stale_push(key)
            return
        if _skip_poll(key):
            # Commissioning/no-camera station we must never call. Leave the
            # cache untouched.
            return
        try:
            data = station_get_status(
                config, tunnels, key, "/api/status", timeout=8
            )
            if data.get("online"):
                _offline_streak[key] = 0
                cache.set_status(key, data)
            else:
                _offline_streak[key] = _offline_streak.get(key, 0) + 1
                prev = cache.get_status(key)
                # last_updated means the last successful contact: a failed
                # poll keeps the previous one rather than stamping now, so a
                # dead station reads "5d ago", not "13s ago".
                data["last_updated"] = (prev or {}).get("last_updated")
                if _offline_streak[key] >= 3:
                    cache.set_status(key, data)
                elif not (prev and prev.get("online")):
                    cache.set_status(key, data)
        except Exception:
            logger.exception("status poll failed for %s", key)

    def _poll_one_vitals(key: str) -> None:
        if _skip_poll(key):
            # Vitals arrive via the ingest for push stations; commissioning /
            # no-camera stations are never contacted.
            return
        try:
            data = station_get_status(
                config, tunnels, key, "/api/vitals", timeout=10
            )
            cache.set_vitals(key, data)
        except Exception:
            logger.exception("vitals poll failed for %s", key)

    def poll_status() -> None:
        n = max(len(config.stations), 1)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="status-poll") as pool:
            while True:
                try:
                    list(pool.map(_poll_one_status, list(config.stations)))
                except Exception:
                    logger.exception("status poll cycle failed")
                time.sleep(60)

    def poll_vitals() -> None:
        n = max(len(config.stations), 1)
        with ThreadPoolExecutor(max_workers=n, thread_name_prefix="vitals-poll") as pool:
            while True:
                try:
                    list(pool.map(_poll_one_vitals, list(config.stations)))
                except Exception:
                    logger.exception("vitals poll cycle failed")
                time.sleep(30)

    threading.Thread(target=poll_status, daemon=True).start()
    threading.Thread(target=poll_vitals, daemon=True).start()
