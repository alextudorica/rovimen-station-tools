"""HTTP response caching — ETag, 304 Not Modified, content-addressed cache."""

import hashlib
import json
import threading
from collections import OrderedDict
from typing import Any

from flask import Response, request

# Bounded cache of already-serialized payloads, keyed by a CALLER-SUPPLIED key.
#
# This cache used to be keyed by the blake2b of the serialized body, which it
# could only compute by serializing first -- so it could never save the
# json.dumps it existed to avoid, and every request re-encoded the whole
# payload (a fleet night of detections is several MiB). An earlier version
# keyed on id(payload) instead, which did skip the dumps but could serve one
# payload's bytes for another after CPython recycled an object address.
#
# A caller-supplied key avoids both traps: the caller already knows what
# identifies its payload (endpoint + date + the build stamp of the upstream
# payload cache + whether the anonymous projection was applied), so a key
# collision means the CONTENT is genuinely the same. Callers that have no such
# key omit it and get the previous serialize-every-time behaviour.
#
# Bounded by entry count AND total bytes so a few large payloads can't grow
# the worker's RSS without limit (this process gets OOM-killed for less).
_JSON_CACHE_MAX_ENTRIES = 64
_JSON_CACHE_MAX_BYTES = 32 * 1024 * 1024
_json_cache: "OrderedDict[Any, tuple[str, bytes]]" = OrderedDict()
_json_cache_bytes = 0
_json_cache_lock = threading.Lock()


def _json_cache_get(cache_key: Any) -> tuple[str, bytes] | None:
    with _json_cache_lock:
        hit = _json_cache.get(cache_key)
        if hit is not None:
            _json_cache.move_to_end(cache_key)
        return hit


def _json_cache_put(cache_key: Any, etag: str, body: bytes) -> None:
    global _json_cache_bytes
    if len(body) > _JSON_CACHE_MAX_BYTES:
        return  # a single payload larger than the whole budget: don't cache it
    with _json_cache_lock:
        prev = _json_cache.pop(cache_key, None)
        if prev is not None:
            _json_cache_bytes -= len(prev[1])
        _json_cache[cache_key] = (etag, body)
        _json_cache_bytes += len(body)
        while _json_cache and (
            len(_json_cache) > _JSON_CACHE_MAX_ENTRIES
            or _json_cache_bytes > _JSON_CACHE_MAX_BYTES
        ):
            _, evicted = _json_cache.popitem(last=False)
            _json_cache_bytes -= len(evicted[1])


def _json_cache_clear() -> None:
    """Test hook — drop every memoized body."""
    global _json_cache_bytes
    with _json_cache_lock:
        _json_cache.clear()
        _json_cache_bytes = 0


def _json_cached(payload: Any, max_age: int, cache_key: Any = None) -> Response:
    """Return a Flask JSON response with ETag + Cache-Control, honouring
    If-None-Match for 304 short-circuits. The payload bytes are hashed so the
    ETag is stable across processes — restart the dashboard, browsers keep
    their cache. Use for read-mostly endpoints; do NOT use for endpoints that
    return a Set-Cookie or mutate session state.

    Uses Response.make_conditional rather than comparing the If-None-Match
    header literally: nginx, when it gzips a response that carries a strong
    ETag, rewrites it to a weak ETag (W/"…") because the wire bytes change.
    A direct == compare would miss the weak prefix on the round-trip;
    make_conditional treats weak and strong as equivalent for 304.

    ``cache_key``, when given, memoizes the serialized body so repeat requests
    for the same content skip the json.dumps entirely. It MUST change whenever
    the payload's content changes — see the module comment.
    """
    hit = _json_cache_get(cache_key) if cache_key is not None else None
    if hit is not None:
        etag_value, body_bytes = hit
    else:
        body_bytes = json.dumps(
            payload, separators=(",", ":"), default=str
        ).encode("utf-8")
        etag_value = hashlib.blake2b(body_bytes, digest_size=12).hexdigest()
        if cache_key is not None:
            _json_cache_put(cache_key, etag_value, body_bytes)

    resp = Response(body_bytes, mimetype="application/json")
    resp.set_etag(etag_value)  # type: ignore[arg-type]
    resp.headers["Cache-Control"] = f"private, max-age={max_age}"
    return resp.make_conditional(request)
