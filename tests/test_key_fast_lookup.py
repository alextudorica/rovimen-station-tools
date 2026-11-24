"""Key checks are one sha256 lookup once a key has verified, for both stores.

Both ``api_keys.yaml`` and ``station_keys.yaml`` hold a slow werkzeug hash
(scrypt) per key. Checking a wrong key used to cost one scrypt per enabled key
before any rate limit counted the request. A key's first successful slow check
now records ``secret_sha256`` on its row, next to the untouched slow hash, and
later checks look the sha256 up in a dict. Rows without one (not verified since
the change) still get the slow check, so no key can stop working.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest
import yaml
from werkzeug.security import generate_password_hash

import api_keys
import station_keys

DASHBOARD = Path(__file__).resolve().parent.parent / "dashboard"


@pytest.fixture(params=[api_keys, station_keys], ids=["api_keys", "station_keys"])
def mod(request) -> ModuleType:
    m = request.param
    m._invalidate_cache()
    m._last_good.clear()
    yield m
    m._invalidate_cache()
    m._last_good.clear()


@pytest.fixture
def path(tmp_path: Path) -> Path:
    return tmp_path / "keys.yaml"


def _sha(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _row(mod: ModuleType, key_id: str, secret: str, **extra) -> dict:
    """A row as minted before this change: slow hash only."""
    row = {"id": key_id, "secret_hash": generate_password_hash(secret), "label": key_id}
    if mod is station_keys:
        row["station"] = key_id
    row.update(extra)
    return row


def _write(path: Path, rows: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"keys": rows}, sort_keys=False))


def _rows(path: Path) -> list[dict]:
    return yaml.safe_load(path.read_text())["keys"]


class _SlowHashCounter:
    def __init__(self, real) -> None:
        self.real = real
        self.calls = 0

    def __call__(self, pwhash: str, password: str) -> bool:
        self.calls += 1
        return self.real(pwhash, password)


@pytest.fixture
def slow_calls(mod, monkeypatch) -> _SlowHashCounter:
    counter = _SlowHashCounter(mod.check_password_hash)
    monkeypatch.setattr(mod, "check_password_hash", counter)
    return counter


def _forbid_slow_hash(mod: ModuleType, monkeypatch) -> None:
    def _boom(*_a, **_k):
        raise AssertionError("slow hash called")
    monkeypatch.setattr(mod, "check_password_hash", _boom)


# ── migration of rows minted before the change ────────────────────────────


def test_slow_hash_only_row_verifies_and_gets_its_sha256(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha"), _row(mod, "beta", "s-beta")])
    hashes_before = [r["secret_hash"] for r in _rows(path)]

    key = mod.validate("s-alpha", path=path)

    assert key is not None and key.id == "alpha"
    rows = _rows(path)
    assert [r["secret_hash"] for r in rows] == hashes_before
    assert rows[0]["secret_sha256"] == _sha("s-alpha")
    assert rows[1].get("secret_sha256", "") == ""


def test_migrated_row_verifies_without_the_slow_hash(mod, path, monkeypatch):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    assert mod.validate("s-alpha", path=path) is not None
    mod._invalidate_cache()
    _forbid_slow_hash(mod, monkeypatch)

    key = mod.validate("s-alpha", path=path)

    assert key is not None and key.id == "alpha"


def test_wrong_key_costs_no_slow_hash_once_all_rows_are_migrated(mod, path, slow_calls):
    _write(path, [_row(mod, "alpha", "s-alpha"), _row(mod, "beta", "s-beta")])
    assert mod.validate("s-alpha", path=path) is not None
    assert mod.validate("s-beta", path=path) is not None
    slow_calls.calls = 0

    for _ in range(5):
        assert mod.validate("not-a-key", path=path) is None

    assert slow_calls.calls == 0


def test_wrong_key_still_checks_rows_without_a_sha256(mod, path, slow_calls):
    _write(path, [_row(mod, "alpha", "s-alpha"), _row(mod, "beta", "s-beta")])
    assert mod.validate("s-alpha", path=path) is not None
    slow_calls.calls = 0

    assert mod.validate("not-a-key", path=path) is None

    assert slow_calls.calls == 1  # beta only


def test_minted_key_carries_both_hashes_and_never_needs_the_slow_one(mod, path, monkeypatch):
    if mod is station_keys:
        _, secret = mod.add_key("gmn0002", path=path)
    else:
        _, secret = mod.add_key("astromania", path=path)
    [row] = _rows(path)
    assert row["secret_hash"].startswith(("scrypt:", "pbkdf2:"))
    assert row["secret_sha256"] == _sha(secret)
    _forbid_slow_hash(mod, monkeypatch)

    assert mod.validate(secret, path=path) is not None


def test_legacy_plaintext_row_gets_slow_hash_and_sha256(mod, path, monkeypatch):
    row = {"id": "legacy", "secret": "plain-secret"}
    if mod is station_keys:
        row["station"] = "legacy"
    _write(path, [row])

    assert mod.validate("plain-secret", path=path) is not None

    [stored] = _rows(path)
    assert stored["secret"] == ""
    assert stored["secret_hash"].startswith(("scrypt:", "pbkdf2:"))
    assert stored["secret_sha256"] == _sha("plain-secret")
    mod._invalidate_cache()
    _forbid_slow_hash(mod, monkeypatch)
    assert mod.validate("plain-secret", path=path) is not None


# ── disabled keys ─────────────────────────────────────────────────────────


def test_disabled_migrated_key_is_rejected(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    assert mod.validate("s-alpha", path=path) is not None
    assert mod.disable_key("alpha", path=path) is True

    assert mod.validate("s-alpha", path=path) is None
    assert _rows(path)[0]["secret_sha256"] == _sha("s-alpha")


def test_hand_disabled_row_with_sha256_is_rejected(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha", disabled=True, secret_sha256=_sha("s-alpha"))])

    assert mod.validate("s-alpha", path=path) is None


def test_disabled_row_is_never_migrated(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha", disabled=True)])

    assert mod.validate("s-alpha", path=path) is None
    assert _rows(path)[0].get("secret_sha256", "") == ""


# ── the migration writer ──────────────────────────────────────────────────


def test_writer_with_a_stale_snapshot_keeps_the_other_workers_row(mod, path):
    """Worker A read the file before worker B recorded beta's sha256; A's
    write for alpha must re-read under the lock and keep beta's."""
    _write(path, [_row(mod, "alpha", "s-alpha"), _row(mod, "beta", "s-beta")])
    stale = mod._get_indexes(path).by_id["alpha"]
    assert mod.validate("s-beta", path=path) is not None  # "worker B"

    mod._record_verified(path, stale, "s-alpha")  # "worker A"

    rows = {r["id"]: r for r in _rows(path)}
    assert rows["alpha"]["secret_sha256"] == _sha("s-alpha")
    assert rows["beta"]["secret_sha256"] == _sha("s-beta")


def test_writer_skips_a_row_whose_slow_hash_changed_since_the_check(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    verified = mod._get_indexes(path).by_id["alpha"]
    _write(path, [_row(mod, "alpha", "s-rotated")])
    before = path.read_bytes()

    mod._record_verified(path, verified, "s-alpha")

    assert path.read_bytes() == before


def test_writer_skips_a_row_disabled_since_the_check(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    verified = mod._get_indexes(path).by_id["alpha"]
    mod.disable_key("alpha", path=path)
    before = path.read_bytes()

    mod._record_verified(path, verified, "s-alpha")

    assert path.read_bytes() == before


def test_writer_refuses_an_unparseable_file_and_leaves_it_untouched(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    verified = mod._get_indexes(path).by_id["alpha"]
    path.write_text("keys: [unclosed\n")
    before = path.read_bytes()

    mod._record_verified(path, verified, "s-alpha")

    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", ["keys: [unclosed\n", "keys:\n  - label: no id\n"])
def test_every_writer_refuses_a_malformed_file_and_leaves_it_untouched(mod, path, bad):
    """A mint after one bad hand edit must not write back only the new key."""
    path.write_text(bad)
    before = path.read_bytes()

    with pytest.raises(mod.KeyFileError):
        mod.add_key("gmnro14", path=path)
    with pytest.raises(mod.KeyFileError):
        mod.disable_key("gmnro14", path=path)

    assert path.read_bytes() == before


def test_failed_write_still_verifies_and_does_not_redo_the_slow_hash(mod, path, monkeypatch, slow_calls):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    before = path.read_bytes()

    def _fail(*_a, **_k):
        raise mod.KeyFileError("cannot keep the owner")
    monkeypatch.setattr(mod, "write_preserving", _fail)

    for _ in range(3):
        assert mod.validate("s-alpha", path=path) is not None

    assert slow_calls.calls == 1
    assert path.read_bytes() == before


def test_writer_keeps_file_mode(mod, path):
    _write(path, [_row(mod, "alpha", "s-alpha")])
    os.chmod(path, 0o640)

    assert mod.validate("s-alpha", path=path) is not None

    assert _rows(path)[0]["secret_sha256"] == _sha("s-alpha")
    assert (path.stat().st_mode & 0o777) == 0o640


_WORKER = """
import sys, os, time
from pathlib import Path
sys.path.insert(0, {dashboard!r})
import importlib
mod = importlib.import_module(sys.argv[1])
path, secret, go = Path(sys.argv[2]), sys.argv[3], Path(sys.argv[4])
mod._get_indexes(path)  # every worker holds the pre-migration file
while not go.exists():
    time.sleep(0.002)
key = mod.validate(secret, path=path)
print(key.id if key else "NONE")
"""


def test_concurrent_migration_from_separate_processes_keeps_every_row(mod, path, tmp_path):
    secrets_by_id = {f"k{i}": f"s-{i}" for i in range(4)}
    _write(path, [_row(mod, kid, s) for kid, s in secrets_by_id.items()])
    hashes_before = {r["id"]: r["secret_hash"] for r in _rows(path)}
    script = tmp_path / "worker.py"
    script.write_text(_WORKER.format(dashboard=str(DASHBOARD)))
    go = tmp_path / "go"

    procs = [
        subprocess.Popen(
            [sys.executable, str(script), mod.__name__, str(path), s, str(go)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for s in secrets_by_id.values()
    ]
    go.touch()
    outs = [p.communicate(timeout=120) for p in procs]

    assert [o[0].strip() for o in outs] == list(secrets_by_id), outs
    rows = {r["id"]: r for r in _rows(path)}
    assert set(rows) == set(secrets_by_id)
    for kid, secret in secrets_by_id.items():
        assert rows[kid]["secret_hash"] == hashes_before[kid]
        assert rows[kid]["secret_sha256"] == _sha(secret)


# ── station binding ───────────────────────────────────────────────────────


def test_station_key_after_migration_maps_to_its_station(path, monkeypatch):
    station_keys._invalidate_cache()
    _write(path, [
        _row(station_keys, "gmn0002", "s-vaslui"),
        _row(station_keys, "gmnro10", "s-roit"),
    ])
    assert station_keys.validate("s-vaslui", path=path).station == "gmn0002"
    assert station_keys.validate("s-roit", path=path).station == "gmnro10"
    station_keys._invalidate_cache()
    _forbid_slow_hash(station_keys, monkeypatch)

    assert station_keys.validate("s-roit", path=path).station == "gmnro10"
    assert station_keys.authorizes("s-vaslui", "gmn0002", path=path) is True
    assert station_keys.authorizes("s-vaslui", "gmnro10", path=path) is False
    station_keys._invalidate_cache()


def test_rotated_station_id_maps_to_the_enabled_row(path):
    """Re-keying reuses the id after disabling the old row: the old secret's
    sha256 stays on the disabled row and must not authorise."""
    station_keys._invalidate_cache()
    _, old = station_keys.add_key("gmn0002", path=path)
    station_keys.disable_key("gmn0002", path=path)
    _, new = station_keys.add_key("gmn0002", path=path)

    assert station_keys.validate(old, path=path) is None
    assert station_keys.validate(new, path=path).station == "gmn0002"
    station_keys._invalidate_cache()


class TestAuthorizesFast:
    """The ingest rate limiter's check: no slow hash, None when undecidable."""

    @pytest.fixture(autouse=True)
    def _fresh(self):
        station_keys._invalidate_cache()
        yield
        station_keys._invalidate_cache()

    def test_migrated_key_for_its_station(self, path, monkeypatch):
        _write(path, [_row(station_keys, "gmn0002", "s-vaslui")])
        station_keys.validate("s-vaslui", path=path)
        _forbid_slow_hash(station_keys, monkeypatch)

        assert station_keys.authorizes_fast("s-vaslui", "gmn0002", path=path) is True
        assert station_keys.authorizes_fast("s-vaslui", "gmnro10", path=path) is False
        assert station_keys.authorizes_fast("junk", "gmn0002", path=path) is False
        assert station_keys.authorizes_fast("", "gmn0002", path=path) is False

    def test_undecidable_while_the_station_row_has_no_sha256(self, path, monkeypatch):
        _write(path, [_row(station_keys, "gmn0002", "s-vaslui"), _row(station_keys, "gmnro10", "s-roit")])
        station_keys.validate("s-roit", path=path)
        _forbid_slow_hash(station_keys, monkeypatch)

        assert station_keys.authorizes_fast("junk", "gmn0002", path=path) is None
        assert station_keys.authorizes_fast("s-vaslui", "gmn0002", path=path) is None
        assert station_keys.authorizes_fast("junk", "gmnro10", path=path) is False

    def test_disabled_key_is_false(self, path):
        _, secret = station_keys.add_key("gmn0002", path=path)
        station_keys.disable_key("gmn0002", path=path)

        assert station_keys.authorizes_fast(secret, "gmn0002", path=path) is False

    def test_key_verified_in_memory_after_a_failed_write(self, path, monkeypatch):
        _write(path, [_row(station_keys, "gmn0002", "s-vaslui")])

        def _fail(*_a, **_k):
            raise station_keys.KeyFileError("cannot keep the owner")
        monkeypatch.setattr(station_keys, "write_preserving", _fail)
        assert station_keys.validate("s-vaslui", path=path) is not None

        assert station_keys.authorizes_fast("s-vaslui", "gmn0002", path=path) is True
        assert station_keys.authorizes_fast("junk", "gmn0002", path=path) is None


def test_rows_with_fields_this_code_does_not_know_still_load(mod, path):
    """Rollback safety: the code before secret_sha256 read files carrying it
    because the models ignore unknown fields. Forbidding them would make a
    file written by newer code invalid, which fails closed for every key."""
    _write(path, [_row(mod, "alpha", "s-alpha", some_future_field="x")])

    assert mod.validate("s-alpha", path=path) is not None


# ── the sha256 is bound to the slow hash it was recorded against ─────────


def _fp(secret_hash: str) -> str:
    return hashlib.sha256(secret_hash.encode()).hexdigest()[:16]


def _rotate_in_place(path: Path, key_id: str, new_secret: str) -> None:
    """Replace a row's slow hash the way a hand edit or an older writer would,
    leaving its secret_sha256 as it was."""
    rows = _rows(path)
    for r in rows:
        if r["id"] == key_id:
            r["secret_hash"] = generate_password_hash(new_secret)
    _write(path, rows)


def test_in_place_rotation_rejects_the_old_secret_and_accepts_the_new(mod, path):
    _write(path, [_row(mod, "alpha", "s-old")])
    assert mod.validate("s-old", path=path) is not None
    assert _rows(path)[0]["secret_sha256"] == _sha("s-old")

    _rotate_in_place(path, "alpha", "s-new")

    assert mod.validate("s-old", path=path) is None
    assert mod.validate("s-new", path=path) is not None
    [row] = _rows(path)
    assert row["secret_sha256"] == _sha("s-new")
    assert row["secret_sha256_for"] == _fp(row["secret_hash"])
    mod._invalidate_cache()
    assert mod.validate("s-old", path=path) is None
    assert mod.validate("s-new", path=path) is not None


def test_in_place_rotation_of_a_minted_key(mod, path):
    if mod is station_keys:
        _, old = mod.add_key("gmn0002", path=path)
    else:
        _, old = mod.add_key("astromania", path=path)
    key_id = _rows(path)[0]["id"]

    _rotate_in_place(path, key_id, "s-new")

    assert mod.validate(old, path=path) is None
    assert mod.validate("s-new", path=path) is not None


def test_in_place_rotation_is_not_fast_authorised_for_the_station(path):
    station_keys._invalidate_cache()
    _, old = station_keys.add_key("gmn0002", path=path)
    _rotate_in_place(path, "gmn0002", "s-new")

    assert station_keys.authorizes_fast(old, "gmn0002", path=path) is not True
    assert station_keys.authorizes(old, "gmn0002", path=path) is False
    assert station_keys.authorizes("s-new", "gmn0002", path=path) is True
    assert station_keys.authorizes_fast("s-new", "gmn0002", path=path) is True
    station_keys._invalidate_cache()


def test_every_writer_that_sets_a_slow_hash_binds_the_sha256_to_it(mod, path):
    legacy = {"id": "legacy", "secret": "plain-secret"}
    if mod is station_keys:
        legacy["station"] = "legacy"
    _write(path, [legacy])
    assert mod.validate("plain-secret", path=path) is not None
    if mod is station_keys:
        mod.add_key("gmn0002", path=path)
    else:
        mod.add_key("astromania", path=path)

    for row in _rows(path):
        assert row["secret_hash"]
        assert row["secret_sha256_for"] == _fp(row["secret_hash"]), row["id"]


def test_unbound_sha256_gets_the_slow_check_and_is_bound(mod, path, slow_calls):
    _write(path, [_row(mod, "alpha", "s-alpha", secret_sha256=_sha("s-alpha"))])

    assert mod.validate("s-alpha", path=path) is not None

    assert slow_calls.calls == 1
    [row] = _rows(path)
    assert row["secret_sha256_for"] == _fp(row["secret_hash"])
