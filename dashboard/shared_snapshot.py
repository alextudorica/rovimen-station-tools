"""Cross-worker snapshot cache for expensive JSON payloads.

Why this exists: the dashboard runs ``GUNICORN_WORKERS`` (3 on prod) gthread
workers, and its hot payload caches were plain per-process dicts. So a payload
that costs seconds to build had to be built once *per worker*. Measured on
prod 2026-09-09 with ten identical back-to-back calls to
``/api/detections/<date>``: exactly three responses took 5.4-7.1 s (one per
worker, each warming its own copy) and the other seven took ~18 ms. Every TTL
expiry paid that 3x again, so an ordinary page load regularly landed on a cold
worker and felt slow even though the box was ~76% idle.

This module adds a shared second tier *behind* those per-process dicts:

* per-process dict (L1) — unchanged, still the fast path, still bounded.
* this store (L2) — one Redis key per cache entry, shared by every worker.

On an L1 miss a worker asks L2 before rebuilding, so the expensive build
happens once per fleet rather than once per worker. L1 stays in front because
an L2 hit still costs a Redis round-trip plus a JSON decode; L1 keeps the warm
path at dict-lookup speed.

Contract notes that matter to callers:

* **Fail-soft, always.** No Redis URL configured (dev, tests, single worker)
  means every :meth:`SharedSnapshotStore.get` returns ``None`` and every
  :meth:`set` is a no-op, i.e. exactly the historical per-worker behaviour. A
  Redis error at runtime degrades the same way — a cache tier must never turn
  a working request into a 500.
* **Strict JSON only.** Payloads are encoded with ``json.dumps`` and *no*
  ``default=`` coercion. A payload holding a non-JSON-native object (a
  ``datetime``, say) is simply not shared, rather than being silently
  round-tripped into a different shape than the one Flask's ``jsonify`` would
  have emitted. That keeps an L2 hit byte-identical to an L1 hit.
* **Wall clock, not monotonic.** Age and remaining lifetime cross process
  boundaries, so they are stamped with ``time.time()``. All workers share one
  host and therefore one clock.
* **Size-capped.** An entry larger than ``max_bytes`` is not shared, so one
  pathological payload cannot push Redis (``noeviction`` on prod, and holding
  the durable station mirror) toward its memory ceiling.

Related: :mod:`cache_backend` does the same job for the pushed status/vitals
last-value store; this module is deliberately separate because that one models
a last-value *store* keyed by station, while this one models a *cache* of
whole computed payloads with a TTL.
"""

from __future__ import annotations

import json
import logging
import math
import os
import time
from typing import Any

from cache_backend import REDIS_URL_ENV, connect_redis

logger = logging.getLogger(__name__)

# Shared key namespace. Kept aligned with cache_backend so one Redis instance
# holds every dashboard key under one recognisable prefix.
_REDIS_PREFIX = os.environ.get("ROVIMEN_REDIS_PREFIX", "rovimen")

# Envelope schema version. Bumped only when the envelope shape changes; a
# reader that sees an unknown version treats the entry as a miss, so a mixed
# fleet during a rolling deploy degrades to per-worker builds instead of
# mis-reading a payload.
_ENVELOPE_VERSION = 1

# Per-entry ceiling. A fleet night of detections is ~4 MiB of JSON, so 16 MiB
# leaves generous headroom while still refusing a runaway payload.
_MAX_BYTES = int(os.environ.get("ROVIMEN_SHARED_SNAPSHOT_MAX_BYTES", str(16 * 1024 * 1024)))

# Don't publish an entry that is about to expire anyway. Callers legitimately
# hand us very short TTLs -- the detections route drops to 10 s while the
# archive index is still warming up -- and pushing 4 MiB into Redis for a
# 10-second lifetime is pure churn: several MiB/min of traffic and a re-decode
# in every worker, for a window barely wide enough to be adopted. Observed on
# the dev tier 2026-09-09, where a cold archive index kept the shared entry at
# a 10 s TTL. L1 still caches those entries; only the sharing is skipped.
#
# Read at construction rather than import so ops (and tests) can set it without
# depending on module import order.
_MIN_TTL_ENV = "ROVIMEN_SHARED_SNAPSHOT_MIN_TTL"
_MIN_TTL_DEFAULT_S = 30.0


class SharedSnapshotStore:
    """A TTL'd, Redis-backed cache of whole JSON payloads for one namespace.

    ``client`` of ``None`` yields a permanently disabled store whose ``get``
    always misses and whose ``set`` is a no-op — the default everywhere Redis
    is not configured. Use :func:`make_shared_snapshot_store` to build one
    from the environment.
    """

    def __init__(
        self,
        client: Any | None,
        *,
        namespace: str,
        prefix: str = _REDIS_PREFIX,
        version: int = _ENVELOPE_VERSION,
        max_bytes: int = _MAX_BYTES,
        min_ttl_s: float | None = None,
    ) -> None:
        self._r = client
        self._namespace = namespace
        self._prefix = prefix
        self._version = version
        self._max_bytes = max_bytes
        self._min_ttl_s = (
            float(os.environ.get(_MIN_TTL_ENV, _MIN_TTL_DEFAULT_S))
            if min_ttl_s is None else min_ttl_s
        )
        # One-shot log guards. These paths sit on every request of a hot route,
        # so a persistent fault (Redis down, unserialisable payload) must cost
        # one log line, not one per request.
        self._warned: set[str] = set()

    @property
    def enabled(self) -> bool:
        """True when this store actually talks to Redis."""
        return self._r is not None

    def _key(self, key: str) -> str:
        return f"{self._prefix}:snapshot:{self._namespace}:{key}"

    def _warn_once(self, tag: str, msg: str, *args: Any) -> None:
        if tag in self._warned:
            return
        self._warned.add(tag)
        logger.warning(msg, *args)

    def get(self, key: str) -> tuple[Any, float, float] | None:
        """Return ``(payload, age_s, remaining_s)`` for ``key``, or ``None``.

        ``age_s`` is how long ago the payload was built, ``remaining_s`` how
        much of its TTL is left. An entry whose TTL has run out reads as a miss
        even if Redis has not expired it yet, so a caller can never adopt an
        entry it would immediately have to rebuild.
        """
        if self._r is None:
            return None
        try:
            raw = self._r.get(self._key(key))
        except Exception:
            self._warn_once(
                "get", "shared snapshot %s: Redis read failed, falling back to "
                "per-worker builds", self._namespace,
            )
            return None
        if raw is None:
            return None
        try:
            envelope = json.loads(raw)
            if envelope.get("v") != self._version:
                return None
            built_at = float(envelope["built_at"])
            expires_at = float(envelope["exp"])
            payload = envelope["payload"]
        except (ValueError, TypeError, KeyError, AttributeError):
            self._warn_once(
                "malformed", "shared snapshot %s/%s is malformed; ignoring",
                self._namespace, key,
            )
            return None
        now = time.time()
        remaining = expires_at - now
        if remaining <= 0:
            return None
        # A clock that jumped backwards would otherwise yield a negative age
        # and read as "fresher than fresh"; clamp instead.
        return payload, max(0.0, now - built_at), remaining

    def set(self, key: str, payload: Any, ttl: float) -> None:
        """Publish ``payload`` under ``key`` for ``ttl`` seconds.

        Silently does nothing when the store is disabled, when ``ttl`` is not
        positive or is below ``min_ttl_s``, when the payload is not strictly
        JSON-encodable, or when the encoded entry exceeds the size cap.
        """
        if self._r is None or ttl <= 0 or ttl < self._min_ttl_s:
            return
        now = time.time()
        envelope = {
            "v": self._version,
            "built_at": now,
            "exp": now + ttl,
            "payload": payload,
        }
        try:
            # No default= on purpose: see the module docstring. A payload that
            # is not already JSON-native is skipped rather than coerced.
            blob = json.dumps(envelope, allow_nan=False)
        except (TypeError, ValueError):
            self._warn_once(
                "encode", "shared snapshot %s: payload is not strictly "
                "JSON-encodable; not sharing it across workers", self._namespace,
            )
            return
        if len(blob) > self._max_bytes:
            self._warn_once(
                "toobig", "shared snapshot %s: entry is %d bytes (cap %d); not "
                "sharing it across workers", self._namespace, len(blob), self._max_bytes,
            )
            return
        try:
            # Redis EX takes whole seconds and floors, so ceil to guarantee the
            # key outlives the TTL a reader was handed.
            self._r.set(self._key(key), blob, ex=max(1, math.ceil(ttl)))
        except Exception:
            self._warn_once(
                "set", "shared snapshot %s: Redis write failed, staying "
                "per-worker", self._namespace,
            )

    def invalidate(self, key: str) -> None:
        """Drop ``key`` from the shared tier (best effort)."""
        if self._r is None:
            return
        try:
            self._r.delete(self._key(key))
        except Exception:
            self._warn_once(
                "del", "shared snapshot %s: Redis delete failed", self._namespace,
            )


def make_shared_snapshot_store(
    namespace: str,
    redis_url: str | None = None,
    **kwargs: Any,
) -> SharedSnapshotStore:
    """Build a :class:`SharedSnapshotStore` for ``namespace`` from the env.

    Returns a disabled (no-op) store when ``ROVIMEN_REDIS_URL`` is unset or
    the client cannot be built, so the dashboard boots and serves either way —
    it just rebuilds payloads per worker, exactly as before this tier existed.
    A Redis that is down at boot keeps its client: reads miss and writes no-op
    until it recovers (see ``cache_backend.connect_redis``).
    """
    url = redis_url if redis_url is not None else os.environ.get(REDIS_URL_ENV)
    if not url:
        return SharedSnapshotStore(None, namespace=namespace, **kwargs)
    client = connect_redis(url, f"shared snapshot {namespace}")
    return SharedSnapshotStore(client, namespace=namespace, **kwargs)
