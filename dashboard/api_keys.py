"""API key store for the public API.

Manual provisioning model: an operator runs ``tools/mint_api_key.py`` to
mint a 256-bit secret, the secret hash is appended to ``api_keys.yaml``
on the VPS, the operator hands the plaintext secret to the consumer
once, and from then on every request to ``/api/public/v1/*`` must carry
that secret as ``X-API-Key: <secret>`` (or ``Authorization: Bearer
<secret>``).

Storage shape (``api_keys.yaml``) — current (hashed) format:

    keys:
      - id: astromania-prod
        secret_hash: "scrypt:32768:8:1$...."   # werkzeug password hash
        secret_sha256: "3f1c...e9"             # hex sha256 of the secret
        secret_sha256_for: "a94a8fe5ccb19ba6"  # fingerprint of secret_hash
        label: "astromania.org WordPress plugin"
        created_at: "2026-05-24T13:30:00Z"
        disabled: false
        rate_limit_override: null

Storage shape (legacy, plaintext) — accepted on read for backward
compatibility, migrated to hashed form on first successful verify:

    keys:
      - id: astromania-prod
        secret: 9f7a2b91c4e3d6f5...    # 64 hex chars (256-bit random)
        ...

Every row keeps the slow ``werkzeug.security.generate_password_hash``
hash it was minted with (scrypt). Checking a key against those costs one
slow hash per enabled key, and a wrong key pays it for every key, so a key's
first successful slow check records ``secret_sha256`` on its row and later
checks are one dict lookup by sha256. Secrets are 256-bit random, so the slow
hash adds nothing over sha256. ``add_key`` writes both. Rows without a
sha256 (not verified since it was introduced) still get the slow check, so
no key stops working; the slow hash is never rewritten, so a rollback to code
that ignores ``secret_sha256`` keeps every key valid.

The sha256 is bound to the slow hash it was recorded against:
``secret_sha256_for`` is a fingerprint of that ``secret_hash``, and a row
whose fingerprint does not match its current ``secret_hash`` (re-keyed in
place by a hand edit, or by code that does not know the field) is checked
with the slow hash again, so a stale sha256 can never accept the old
secret. Every writer that sets a slow hash sets the sha256 and fingerprint
with it (:func:`_secret_fields`).

Writes go through an exclusive ``fcntl.flock`` on a sidecar ``.lock``
file plus a same-process ``threading.Lock``, write-to-tmp + ``os.replace``
for atomicity — matching the ``_save_config`` / ``_save_users`` idiom in
``rovimen_dashboard.py``.
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
    os.environ.get("ROVIMEN_API_KEYS_PATH", "/opt/rovimen/api_keys.yaml")
)
# When set to "1" (the production default), every public JSON endpoint
# requires a valid key. When "0", the requirement is bypassed — useful
# for local development and integration tests where minting a key adds
# friction with no value. NEVER set this to "0" on a public-facing
# deployment.
REQUIRE_KEY_ENV = "ROVIMEN_API_KEYS_REQUIRED"


class ApiKey(BaseModel):
    """One row in the keys file.

    Either ``secret_hash`` (preferred, werkzeug slow hash) or
    ``secret`` (legacy plaintext, migrated on read) is populated. New
    rows minted by :func:`add_key` set ``secret_hash`` and
    ``secret_sha256``, never ``secret``.
    """

    id: str
    # New rows: hashed form only. Legacy rows may still carry a plaintext
    # ``secret`` until the first successful verify migrates them.
    secret_hash: str = ""
    secret: str = ""
    # Hex sha256 of the secret: the fast lookup key. Recorded at mint, or
    # on the first successful slow check of an older row.
    secret_sha256: str = ""
    # Fingerprint of the secret_hash the sha256 was recorded against; the
    # sha256 is only used while it matches the row's current secret_hash.
    secret_sha256_for: str = ""
    label: str = ""
    created_at: str = ""
    disabled: bool = False
    # Optional per-key Flask-Limiter override (string in the
    # ``"N/period;M/period"`` shape). When None the route's default
    # bucket applies — but keyed against the key id, not the IP, so
    # legitimate consumers get their own quota independent of where
    # their requests originate.
    rate_limit_override: str | None = None


class _KeysFile(BaseModel):
    keys: list[ApiKey] = Field(default_factory=list)


_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class _Index:
    """Enabled keys by id and by recorded sha256. ``slow`` holds the enabled
    rows with no usable sha256 (none yet, or one recorded against a slow
    hash the row no longer has): only their slow hash (or legacy
    plaintext) can check a secret against them."""

    by_id: dict[str, ApiKey] = field(default_factory=dict)
    by_sha256: dict[str, ApiKey] = field(default_factory=dict)
    slow: tuple[ApiKey, ...] = ()


_EMPTY_INDEX = _Index()


# ── Cache ──────────────────────────────────────────────────────────
# Re-reading + parsing the YAML on every request is wasteful. We keep
# a cache keyed on the file's mtime, inode and size, so a fresh-minted
# key (every write replaces the file) is picked up on the next request
# without a service restart.
_cache_lock = threading.Lock()
_cache: tuple[tuple[int, int, int], _Index] | None = None
# Slow hash -> sha256 of the secret that verified against it, in this
# process. Only successful checks land here (at most one per row), so a
# key whose sha256 could not be written to the file still verifies in
# O(1) after its first check. Cleared with the cache.
_verified_digests: dict[str, str] = {}
# Last successfully loaded keys per file. An unreadable file (OSError) is
# bridged with them for keyfile_io.STALE_KEYS_WINDOW_S from the first failed
# reload; an invalid file, or a longer outage, fails closed.
_last_good: LastGood[ApiKey] = LastGood("api_keys")

# Same-process serialisation for writers (load → mutate → save):
# pairs with the on-disk fcntl.flock for cross-process safety.
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


def _has_bound_sha256(k: ApiKey) -> bool:
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


def _build_index(by_id: dict[str, ApiKey]) -> _Index:
    by_sha256: dict[str, ApiKey] = {}
    slow: list[ApiKey] = []
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
    and :class:`KeyFileInvalid` when it cannot be parsed or validated (see
    ``keyfile_io``).
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
    by_id: dict[str, ApiKey] = {}
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
    """Serialise ``parsed`` to ``path`` inside an already-held flock context,
    keeping the file's owner, group and mode (see ``keyfile_io``)."""
    payload = yaml.safe_dump(parsed.model_dump(), sort_keys=False, allow_unicode=True)
    write_preserving(path, payload)


def _same_row(row: ApiKey, verified: ApiKey) -> bool:
    return (
        not row.disabled
        and row.id == verified.id
        and row.secret_hash == verified.secret_hash
        and row.secret == verified.secret
    )


def _record_verified(path: Path, verified: ApiKey, secret: str) -> None:
    """Record ``sha256(secret)`` on the row ``verified`` was read from,
    bound to the row's slow hash, so the next check of this key is one dict
    lookup. A legacy plaintext row also gets its slow hash and loses the
    plaintext (:func:`_secret_fields`). An existing slow hash is never
    changed.

    Best effort: a failure is logged and the key still verifies. Only an
    enabled row with the same id and hashes as the one checked is touched,
    so a row disabled or re-keyed since the check is left alone. Writers in
    other workers are serialised by the file lock and each re-reads the
    file under it (strictly: an unreadable or invalid file is never
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
        logger.info("api_keys: recorded the sha256 of key %r", verified.id)
    except Exception:
        logger.exception("api_keys: could not record the sha256 of key %r", verified.id)


def validate(secret: str, path: Path = DEFAULT_KEYS_PATH) -> ApiKey | None:
    """Return the enabled ApiKey matching ``secret``, or None.

    One sha256 lookup decides it for every row that has ``secret_sha256``.
    Only enabled rows without one get the slow check; a match records the
    sha256 on the row (see :func:`_record_verified`). Legacy plaintext rows
    are compared in constant time and migrated the same way.
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


def is_required() -> bool:
    """Whether public JSON endpoints should reject unkeyed requests.

    Defaults to True — production-safe. Operators set
    ``ROVIMEN_API_KEYS_REQUIRED=0`` on local / dev boxes only.
    """
    return os.environ.get(REQUIRE_KEY_ENV, "1") != "0"


# ── Provisioning ───────────────────────────────────────────────────


def mint_secret() -> str:
    """256 bits of randomness, hex-encoded. Same shape as the session
    signing key (see ``rovimen_dashboard._load_or_create_secret_key``)."""
    return secrets.token_hex(32)


def add_key(
    id: str,
    label: str = "",
    *,
    path: Path = DEFAULT_KEYS_PATH,
    rate_limit_override: str | None = None,
) -> tuple[ApiKey, str]:
    """Mint a new key, persist it, return ``(ApiKey, plaintext_secret)``.

    Caller is responsible for showing the plaintext exactly once — it is
    not stored anywhere after this function returns.

    Raises ValueError on duplicate id. Idempotent on missing parent
    directory (creates it) but not on missing file (creates it too).
    """
    with _write_lock:
        lock_path = _lock_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with open(lock_path, "a+") as lf:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            parsed = _load_raw(path)
            if any(k.id == id for k in parsed.keys):
                raise ValueError(f"api key id already exists: {id!r}")

            plaintext = mint_secret()
            new = ApiKey(
                id=id,
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
    """Mark a key disabled in-place. Returns True if found, False otherwise.

    Wrapped in the same flock + write-tmp + replace dance as
    :func:`add_key` so a concurrent ``add_key`` / ``disable_key`` /
    ``add_key`` sequence never races on the file.
    """
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
    """Return a redacted listing — id / label / created_at / disabled,
    but NOT the secret. Safe to surface in an admin log line or a
    sysadmin-only API."""
    parsed = _load_raw(path)
    return [
        {
            "id": k.id,
            "label": k.label,
            "created_at": k.created_at,
            "disabled": k.disabled,
            "rate_limit_override": k.rate_limit_override,
        }
        for k in parsed.keys
    ]
