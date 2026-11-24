"""Per-station key store for the reversed-HTTP push ingest API.

Each station holds exactly one ingest key that authorises writing *its own*
telemetry (and nothing else) to ``/api/ingest/v1/<station>/*``. The key is bound
to a single ``host_key`` (``gmn0002``, ``gmnro10``, …) — the same identifier used
in ``dashboard_config.yaml``. A key for ``gmn0002`` cannot write ``gmnro10``'s
state; that binding is the property SSH-into-:7779 never had (see
``docs/reversed_http_push_design.md`` §2.2).

This mirrors ``api_keys.py`` (the public-API key store): 256-bit secrets minted
by an ops CLI (``tools/mint_station_key.py``), stored in ``station_keys.yaml``
(mode 0600) as a slow werkzeug hash (scrypt) plus ``secret_sha256``. A check is
one dict lookup by sha256; only an enabled row with no sha256 yet (minted before
it existed and not verified since) gets the slow check, and its first match
records the sha256 on the row. Secrets are 256-bit random, so the slow hash adds
nothing over sha256, and it is never rewritten, so a rollback keeps every key
valid. The sha256 is bound to the slow hash it was recorded against
(``secret_sha256_for``, a fingerprint of that ``secret_hash``): a row re-keyed
in place, by a hand edit or by code that does not know the field, is checked
with the slow hash again, so a stale sha256 never accepts the old secret.
Every station push is checked here, so this is the fleet's ingest gate.
The one structural difference is the ``station`` field binding a key to its
host_key.

Storage shape (``station_keys.yaml``)::

    keys:
      - id: gmn0002
        station: gmn0002
        secret_hash: "scrypt:32768:8:1$...."
        secret_sha256: "3f1c...e9"
        secret_sha256_for: "a94a8fe5ccb19ba6"
        label: "Vaslui push agent"
        created_at: "2026-07-01T22:00:00Z"
        disabled: false
        rate_limit_override: null

Legacy plaintext ``secret`` rows are accepted on read and migrated to a hash on
first successful verify, exactly like ``api_keys.py``.
"""

from __future__ import annotations

import fcntl
import hashlib
import logging
import os
import re
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, ValidationError
from werkzeug.security import check_password_hash, generate_password_hash

from keyfile_io import KeyFileError, KeyFileInvalid, LastGood, read_yaml_strict, write_preserving

logger = logging.getLogger(__name__)


DEFAULT_KEYS_PATH = Path(
    os.environ.get("ROVIMEN_STATION_KEYS_PATH", "/opt/rovimen/station_keys.yaml")
)
# When "0", ingest key verification is bypassed — local dev / integration
# tests only. NEVER set to "0" on a deployment reachable from the internet.
REQUIRE_KEY_ENV = "ROVIMEN_STATION_KEYS_REQUIRED"

# Secret prefix so a leaked token is recognisable and greppable in logs.
SECRET_PREFIX = "rvmn"


class StationKey(BaseModel):
    """One row in the station keys file.

    ``station`` is the single ``host_key`` this key authorises. ``id`` defaults
    to the same value (one key per station), but is kept distinct so a station
    can be re-keyed (``gmn0002`` → ``gmn0002-2``) without losing the old row's
    audit trail — only the enabled row for a given ``station`` is honoured.
    """

    id: str
    station: str
    secret_hash: str = ""
    secret: str = ""
    # Hex sha256 of the secret: the fast lookup key. Recorded at mint, or on
    # the first successful slow check of an older row.
    secret_sha256: str = ""
    # Fingerprint of the secret_hash the sha256 was recorded against; the
    # sha256 is only used while it matches the row's current secret_hash.
    secret_sha256_for: str = ""
    label: str = ""
    created_at: str = ""
    disabled: bool = False
    rate_limit_override: str | None = None


class _KeysFile(BaseModel):
    keys: list[StationKey] = Field(default_factory=list)


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Index:
    """Enabled keys by id and by recorded sha256. ``slow`` holds the enabled
    rows with no usable sha256 (none yet, or one recorded against a slow
    hash the row no longer has): only their slow hash (or legacy
    plaintext) can check a secret against them."""

    by_id: dict[str, StationKey] = field(default_factory=dict)
    by_sha256: dict[str, StationKey] = field(default_factory=dict)
    slow: tuple[StationKey, ...] = ()


_EMPTY_INDEX = _Index()


# ── Cache ──────────────────────────────────────────────────────────
# Keyed on the file's mtime, inode and size: every write replaces the file.
_cache_lock = threading.Lock()
_cache: tuple[tuple[int, int, int], _Index] | None = None
# Slow hash -> sha256 of the secret that verified against it, in this process.
# Only successful checks land here (at most one per row), so a key whose sha256
# could not be written to the file still verifies in O(1) after its first
# check. Cleared with the cache.
_verified_digests: dict[str, str] = {}
# Last successfully loaded keys per file. An unreadable file (OSError) is
# bridged with them for keyfile_io.STALE_KEYS_WINDOW_S from the first failed
# reload; an invalid file, or a longer outage, fails closed.
_last_good: LastGood[StationKey] = LastGood("station_keys")
_write_lock = threading.Lock()


def _sha256_hex(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def _check_slow_hash(secret_hash: str, secret: str) -> bool:
    try:
        return check_password_hash(secret_hash, secret)
    except Exception:
        return False


def _hash_fingerprint(secret_hash: str) -> str:
    return hashlib.sha256(secret_hash.encode("utf-8")).hexdigest()[:16]


def _has_bound_sha256(k: StationKey) -> bool:
    """True when the row's sha256 was recorded against its current slow hash."""
    return (
        bool(k.secret_hash)
        and bool(_SHA256_HEX.match(k.secret_sha256))
        and k.secret_sha256_for == _hash_fingerprint(k.secret_hash)
    )


def _secret_fields(secret: str) -> dict[str, str]:
    """Stored form of a new secret: its slow hash, and its sha256 bound to
    that hash. Every writer that sets a slow hash takes all of these."""
    secret_hash = generate_password_hash(secret)
    return {
        "secret_hash": secret_hash,
        "secret": "",
        "secret_sha256": _sha256_hex(secret),
        "secret_sha256_for": _hash_fingerprint(secret_hash),
    }


def _build_index(by_id: dict[str, StationKey]) -> _Index:
    by_sha256: dict[str, StationKey] = {}
    slow: list[StationKey] = []
    for k in by_id.values():
        if _has_bound_sha256(k):
            by_sha256[k.secret_sha256] = k
        else:
            slow.append(k)
    return _Index(by_id=by_id, by_sha256=by_sha256, slow=tuple(slow))


def _file_signature(path: Path) -> tuple[int, int, int] | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_ino, st.st_size


def _load_raw(path: Path) -> _KeysFile:
    """Parse the keys file; an absent file is an empty store.

    Raises :class:`KeyFileUnreadable` when the file exists but cannot be read
    and :class:`KeyFileInvalid` when it cannot be parsed or validated, so
    neither the auth path nor a writer mistakes "unreadable" for "no keys"
    (the 2026-11-20 outage).
    """
    raw = read_yaml_strict(path)
    try:
        return _KeysFile.model_validate(raw)
    except ValidationError as exc:
        raise KeyFileInvalid(f"cannot validate {path}: {exc}") from exc


def _refresh_cache(path: Path) -> _Index:
    signature = _file_signature(path)
    if signature is None:
        return _EMPTY_INDEX
    try:
        parsed = _load_raw(path)
    except KeyFileError as exc:
        # Unreadable: last good keys for a bounded window. Invalid: none.
        # Nothing is cached, so the next request retries the read.
        return _build_index(_last_good.failed(path, exc))
    by_id: dict[str, StationKey] = {}
    for k in parsed.keys:
        if k.disabled:
            continue
        if not k.secret_hash and not k.secret:
            continue
        by_id[k.id] = k
    index = _build_index(by_id)
    with _cache_lock:
        global _cache
        _cache = (signature, index)
    _last_good.loaded(path, by_id)
    return index


def _get_indexes(path: Path) -> _Index:
    """Return the index of the currently-enabled keys."""
    signature = _file_signature(path)
    if signature is None:
        return _EMPTY_INDEX
    with _cache_lock:
        if _cache and _cache[0] == signature:
            return _cache[1]
    return _refresh_cache(path)


def _invalidate_cache() -> None:
    with _cache_lock:
        global _cache
        _cache = None
    _verified_digests.clear()


def _lock_path(path: Path) -> Path:
    return path.with_suffix(path.suffix + ".lock")


def _write_file(path: Path, parsed: _KeysFile) -> None:
    """Serialise ``parsed`` to ``path`` inside an already-held flock context.

    Keeps the file's owner, group and mode (see ``keyfile_io``), so a mint run
    as root leaves the file readable for the dashboard user.
    """
    payload = yaml.safe_dump(parsed.model_dump(), sort_keys=False, allow_unicode=True)
    write_preserving(path, payload)


def _same_row(row: StationKey, verified: StationKey) -> bool:
    return (
        not row.disabled
        and row.id == verified.id
        and row.station == verified.station
        and row.secret_hash == verified.secret_hash
        and row.secret == verified.secret
    )


def _record_verified(path: Path, verified: StationKey, secret: str) -> None:
    """Record ``sha256(secret)`` on the row ``verified`` was read from,
    bound to the row's slow hash, so the next check of this key is one dict
    lookup. A legacy plaintext row also gets its slow hash and loses the
    plaintext (:func:`_secret_fields`). An existing slow hash is never
    changed.

    Best effort: a failure is logged and the key still verifies. Only an
    enabled row with the same id, station and hashes as the one checked is
    touched, so a row disabled or re-keyed since the check is left alone.
    Writers in other workers are serialised by the file lock and each re-reads
    the file under it (strictly: an unreadable or invalid file is never
    rewritten), so none drops another's update.
    """
    digest = _sha256_hex(secret)
    try:
        with _write_lock:
            lock_path = _lock_path(path)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            with open(lock_path, "a+") as lf:
                fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
                parsed = _load_raw(path)
                row = next((k for k in parsed.keys if _same_row(k, verified)), None)
                if row is None or (_has_bound_sha256(row) and row.secret_sha256 == digest):
                    return
                if row.secret and not row.secret_hash:
                    for name, value in _secret_fields(secret).items():
                        setattr(row, name, value)
                else:
                    row.secret_sha256 = digest
                    row.secret_sha256_for = _hash_fingerprint(row.secret_hash)
                _write_file(path, parsed)
        _invalidate_cache()
        logger.info("station_keys: recorded the sha256 of key %r (%s)", verified.id, verified.station)
    except Exception:
        logger.exception("station_keys: could not record the sha256 of key %r", verified.id)


def validate(secret: str, path: Path = DEFAULT_KEYS_PATH) -> StationKey | None:
    """Return the enabled StationKey matching ``secret``, or None.

    One sha256 lookup decides it for every row that has ``secret_sha256``.
    Only enabled rows without one get the slow check (werkzeug's
    ``check_password_hash``); a match records the sha256 on the row (see
    :func:`_record_verified`). Legacy plaintext rows are compared with
    ``secrets.compare_digest`` and migrated the same way. Nothing about a
    mismatch is disclosed.
    """
    if not secret:
        return None
    digest = _sha256_hex(secret)
    index = _get_indexes(path)
    key = index.by_sha256.get(digest)
    if key is not None:
        return key
    for key in index.slow:
        if key.secret_hash:
            if _verified_digests.get(key.secret_hash) == digest:
                return key
            if _check_slow_hash(key.secret_hash, secret):
                _verified_digests[key.secret_hash] = digest
                _record_verified(path, key, secret)
                return key
        elif key.secret and secrets.compare_digest(key.secret, secret):
            _record_verified(path, key, secret)
            # The migrated row, so the caller doesn't see the plaintext.
            return _get_indexes(path).by_id.get(key.id) or key
    return None


def authorizes(secret: str, station: str, path: Path = DEFAULT_KEYS_PATH) -> bool:
    """True iff ``secret`` is a valid enabled key bound to ``station``.

    This is the single authorisation check the ingest handler makes: a valid
    key that belongs to a *different* station returns False (the caller maps
    that to 403 — authenticated but wrong station — vs 401 for no/invalid key).
    """
    key = validate(secret, path=path)
    return key is not None and key.station == station


def authorizes_fast(secret: str, station: str, path: Path = DEFAULT_KEYS_PATH) -> bool | None:
    """:func:`authorizes` without the slow hash, for the ingest rate limiter.

    True or False when a sha256 lookup decides it. None when ``station`` still
    has an enabled row with no usable sha256 (and no in-process match), so only
    :func:`validate` can tell; the caller then treats the request as it did
    before this check existed.
    """
    if not secret:
        return False
    digest = _sha256_hex(secret)
    index = _get_indexes(path)
    key = index.by_sha256.get(digest)
    if key is not None:
        return key.station == station
    undecided = False
    for key in index.slow:
        if key.station != station:
            continue
        if key.secret_hash and _verified_digests.get(key.secret_hash) == digest:
            return True
        undecided = True
    return None if undecided else False


def is_required() -> bool:
    """Whether ingest endpoints should reject unkeyed requests. Defaults True."""
    return os.environ.get(REQUIRE_KEY_ENV, "1") != "0"


# ── Provisioning ───────────────────────────────────────────────────


def mint_secret(station: str) -> str:
    """A prefixed, station-tagged, 256-bit secret: ``rvmn_<station>_<hex>``.

    The prefix makes a leaked token recognisable; the station tag is advisory
    only — authorisation is decided by the stored ``station`` binding, never by
    parsing the token."""
    return f"{SECRET_PREFIX}_{station}_{secrets.token_hex(32)}"


def add_key(
    station: str,
    *,
    id: str | None = None,
    label: str = "",
    path: Path = DEFAULT_KEYS_PATH,
    rate_limit_override: str | None = None,
) -> tuple[StationKey, str]:
    """Mint a new key bound to ``station``, persist it, return
    ``(StationKey, plaintext_secret)``.

    ``id`` defaults to ``station``. Raises ValueError if an **enabled** key
    already exists for that id — disable the old one first (or pass a distinct
    ``id`` to rotate). Raises :class:`KeyFileError`, writing nothing, when the
    existing file cannot be read or parsed: writing back only the new key
    would drop every other station's key. The plaintext is returned once and
    never stored.
    """
    key_id = id or station
    with _write_lock:
        lock_path = _lock_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            for k in parsed.keys:
                if k.id == key_id and not k.disabled:
                    raise ValueError(f"station key id already exists: {key_id!r}")

            plaintext = mint_secret(station)
            new = StationKey(
                id=key_id,
                station=station,
                **_secret_fields(plaintext),
                label=label,
                created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                rate_limit_override=rate_limit_override,
            )
            parsed.keys.append(new)
            _write_file(path, parsed)
    _invalidate_cache()
    return new, plaintext


def disable_key(id: str, *, path: Path = DEFAULT_KEYS_PATH) -> bool:
    """Mark a key disabled in-place. Returns True if found."""
    with _write_lock:
        lock_path = _lock_path(path)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            found = False
            for k in parsed.keys:
                if k.id == id:
                    k.disabled = True
                    found = True
            if not found:
                return False
            _write_file(path, parsed)
    _invalidate_cache()
    return True


def list_keys(path: Path = DEFAULT_KEYS_PATH) -> list[dict[str, Any]]:
    """Return a redacted listing — id / station / label / created_at / disabled,
    never the secret. Raises :class:`KeyFileError` when the file is unreadable."""
    parsed = _load_raw(path)
    return [
        {
            "id": k.id,
            "station": k.station,
            "label": k.label,
            "created_at": k.created_at,
            "disabled": k.disabled,
            "rate_limit_override": k.rate_limit_override,
        }
        for k in parsed.keys
    ]
