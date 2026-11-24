"""Tests for the signed station update bundle pipeline.

Covers rovimen-scripts/bundle_verify.py (station verifier) and
deployment/sign_bundle.py (CI signer): signature checks, exact-set file
verification, channel binding, anti-rollback, warn/enforce mode handling and
the full sign -> verify round trip the deploy workflows + updater.sh perform.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import bundle_verify

# deployment/ is not on the conftest sys.path (station + dashboard code only).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deployment"))
import sign_bundle  # noqa: E402


# ── helpers ──────────────────────────────────────────────────────────────────


def _keypair() -> tuple[str, str, bytes]:
    """Return (private_pem, public_pem, raw_pub32)."""
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv_pem, pub_pem, raw


@pytest.fixture
def keypair():
    return _keypair()


@pytest.fixture
def bundle(tmp_path):
    """A staged bundle directory with a few files, including a subdirectory."""
    b = tmp_path / "staging"
    (b / "assets").mkdir(parents=True)
    (b / "station_api.py").write_text("print('api')\n")
    (b / "updater.sh").write_text("#!/bin/bash\necho update\n")
    (b / ".version").write_text("dev.42.2026.07.03\n")
    (b / "assets" / "logo.png").write_bytes(b"\x89PNG fake")
    return b


def _sign(bundle_dir: Path, priv_pem: str, *, channel="dev", version="dev.42.2026.07.03",
          build=42) -> None:
    sign_bundle.sign_bundle(bundle_dir, version=version, channel=channel,
                            build=build, private_pem=priv_pem)


# ── signer + core verification ───────────────────────────────────────────────


def test_sign_then_verify_happy_path(bundle, keypair):
    priv_pem, _pub_pem, raw = keypair
    _sign(bundle, priv_pem)
    assert (bundle / "MANIFEST").exists()
    assert (bundle / "MANIFEST.sig").exists()
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)
    assert result.ok, result.failures
    manifest = result.manifest
    assert manifest["schema"] == 1
    assert manifest["channel"] == "dev"
    assert manifest["build"] == 42
    # Manifest covers every file including the subdirectory, exempting itself.
    assert set(manifest["files"]) == {
        "station_api.py", "updater.sh", ".version", "assets/logo.png",
    }


def test_tampered_file_rejected(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    (bundle / "station_api.py").write_text("print('backdoor')\n")
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)
    assert not result.ok
    assert any("sha256 mismatch: station_api.py" in f for f in result.failures)


def test_tampered_manifest_rejected(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    manifest = json.loads((bundle / "MANIFEST").read_bytes())
    manifest["build"] = 9999
    (bundle / "MANIFEST").write_text(json.dumps(manifest, sort_keys=True,
                                                separators=(",", ":")) + "\n")
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)
    assert not result.ok
    assert any("does not verify" in f for f in result.failures)


def test_extra_file_rejected(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    (bundle / "implant.py").write_text("import os\n")
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)
    assert not result.ok
    assert any("extra file not in manifest: implant.py" in f for f in result.failures)


def test_missing_file_rejected(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    (bundle / "updater.sh").unlink()
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)
    assert not result.ok
    assert any("missing from staging: updater.sh" in f for f in result.failures)


def test_wrong_key_rejected(bundle, keypair):
    priv_pem, _, _ = keypair
    _sign(bundle, priv_pem)
    _, _, other_raw = _keypair()
    result = bundle_verify.verify_bundle(bundle, "dev", [other_raw], state=None)
    assert not result.ok
    assert any("does not verify" in f for f in result.failures)


def test_multi_pin_rotation_window(bundle, keypair, tmp_path):
    """During key rotation the pin file holds old + new keys; either signs."""
    old_priv, old_pub_pem, _old_raw = keypair
    new_priv, new_pub_pem, _new_raw = _keypair()
    pin_file = tmp_path / "pins.pem"
    pin_file.write_text(old_pub_pem + new_pub_pem)
    pins = bundle_verify.load_pinned_keys(pin_file)
    assert len(pins) == 2

    _sign(bundle, old_priv)
    assert bundle_verify.verify_bundle(bundle, "dev", pins, state=None).ok
    _sign(bundle, new_priv)
    assert bundle_verify.verify_bundle(bundle, "dev", pins, state=None).ok


def test_no_pins_is_a_failure(bundle, keypair):
    priv_pem, _, _ = keypair
    _sign(bundle, priv_pem)
    result = bundle_verify.verify_bundle(bundle, "dev", [], state=None)
    assert not result.ok
    assert any("no pinned signing key" in f for f in result.failures)


# ── channel binding + anti-rollback ──────────────────────────────────────────


def test_channel_mismatch_rejected(bundle, keypair):
    """A dev bundle can never be replayed onto a main-channel station."""
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev")
    result = bundle_verify.verify_bundle(bundle, "main", [raw], state=None)
    assert not result.ok
    assert any("channel mismatch" in f for f in result.failures)


def test_rollback_same_channel_rejected(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=42)
    state = {"channel": "dev", "build": 100}
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=state)
    assert not result.ok
    assert any("rollback rejected" in f for f in result.failures)


def test_equal_build_to_last_verified_is_a_retry_not_a_rollback(bundle, keypair):
    """A run that verified build 42 and died before installing it must be able
    to verify 42 again (gmnro05 (Bistrița) refused build 526 ten times from
    2026-11-06 with "526 <= last verified build 526")."""
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=42)
    state = {"channel": "dev", "build": 42}
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=state)
    assert result.ok, result.failures


def test_installed_build_is_the_floor_not_the_verified_one(bundle, keypair):
    """gmnro05's stuck shape: verified 42, installed 40. 42 must pass."""
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=42)
    state = {"channel": "dev", "build": 42, "last_verify": "ok"}
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=state,
                                         installed=("dev", 40))
    assert result.ok, result.failures


def test_build_below_installed_is_refused_even_if_state_is_lower(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=42)
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state={},
                                         installed=("dev", 43))
    assert not result.ok
    assert any("rollback rejected: bundle build 42 < installed build 43" in f
               for f in result.failures)


def test_reapplying_the_installed_build_is_allowed(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=42)
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state={},
                                         installed=("dev", 42))
    assert result.ok, result.failures


@pytest.mark.parametrize("text,expected", [
    ("549.2026.11.19\n", ("main", 549)),
    ("dev.276.2026.07.03", ("dev", 276)),
    ("none", None),
    ("", None),
])
def test_parse_version(text, expected):
    assert bundle_verify.parse_version(text) == expected


def test_channel_switch_resets_rollback_counter(bundle, keypair):
    """dev/prod CI run counters are independent — switching channel (an SSH-
    gated config edit) must accept the other channel's numbering."""
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem, channel="dev", build=3)
    state = {"channel": "main", "build": 500}
    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=state)
    assert result.ok, result.failures


# ── mode handling ────────────────────────────────────────────────────────────


def test_missing_mode_file_means_warn(tmp_path):
    assert bundle_verify.read_mode(tmp_path / "nope") == "warn"


def test_garbage_mode_means_warn(tmp_path):
    f = tmp_path / "mode"
    f.write_text("bananas\n")
    assert bundle_verify.read_mode(f) == "warn"


def test_enforce_mode_read(tmp_path):
    f = tmp_path / "mode"
    f.write_text("enforce\n")
    assert bundle_verify.read_mode(f) == "enforce"


# ── CLI behaviour (what updater.sh sees) ─────────────────────────────────────


def _cli(bundle, tmp_path, *, pin_text: str | None, mode: str | None,
         channel="dev", state: dict | None = None) -> tuple[int, dict]:
    pin = tmp_path / "pin.pem"
    if pin_text is not None:
        pin.write_text(pin_text)
    mode_file = tmp_path / "mode"
    if mode is not None:
        mode_file.write_text(mode)
    state_file = tmp_path / "state.json"
    if state is not None:
        state_file.write_text(json.dumps(state))
    rc = bundle_verify.main([
        "--staging", str(bundle), "--channel", channel,
        "--pubkey", str(pin), "--state", str(state_file),
        "--mode-file", str(mode_file),
    ])
    persisted = json.loads(state_file.read_text()) if state_file.exists() else {}
    return rc, persisted


def test_cli_pass_writes_state(bundle, keypair, tmp_path):
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem)
    rc, state = _cli(bundle, tmp_path, pin_text=pub_pem, mode="enforce")
    assert rc == 0
    assert state["last_verify"] == "ok"
    assert state["channel"] == "dev"
    assert state["build"] == 42
    assert state["mode"] == "enforce"


def test_cli_enforce_fail_exits_nonzero_and_keeps_old_build(bundle, keypair, tmp_path):
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem)
    (bundle / "station_api.py").write_text("tampered\n")
    rc, state = _cli(bundle, tmp_path, pin_text=pub_pem, mode="enforce",
                     state={"channel": "dev", "build": 40, "last_verify": "ok"})
    assert rc == 1
    assert state["last_verify"] == "fail"
    # Rejected bundle must not advance the anti-rollback baseline.
    assert state["build"] == 40


def test_enforce_rejection_carries_the_grep_marker(bundle, keypair, tmp_path, caplog):
    """A rejection must be findable by a stable token, not by matching prose.

    The fleet sweep for "did any station refuse a bundle" is a grep across 20
    stations' updater.log; if it keys on wording, a later copy-edit silently
    turns that sweep into a no-op."""
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem)
    (bundle / "station_api.py").write_text("tampered\n")
    with caplog.at_level("ERROR", logger="rovimen_updater.bundle_verify"):
        rc, _ = _cli(bundle, tmp_path, pin_text=pub_pem, mode="enforce")
    assert rc == 1
    assert bundle_verify.REJECT_MARKER in caplog.text


def test_warn_mode_does_not_emit_the_reject_marker(bundle, keypair, tmp_path, caplog):
    """warn applies the bundle, so it must not look like a rejection in a sweep."""
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem)
    (bundle / "station_api.py").write_text("tampered\n")
    with caplog.at_level("WARNING", logger="rovimen_updater.bundle_verify"):
        rc, _ = _cli(bundle, tmp_path, pin_text=pub_pem, mode="warn")
    assert rc == 0
    assert bundle_verify.REJECT_MARKER not in caplog.text


def test_cli_warn_fail_proceeds(bundle, keypair, tmp_path):
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem)
    (bundle / "station_api.py").write_text("tampered\n")
    rc, state = _cli(bundle, tmp_path, pin_text=pub_pem, mode="warn")
    assert rc == 0  # warn mode: log + proceed
    assert state["last_verify"] == "fail"


def test_cli_missing_pin_warn_proceeds(bundle, keypair, tmp_path):
    priv_pem, _, _ = keypair
    _sign(bundle, priv_pem)
    rc, state = _cli(bundle, tmp_path, pin_text=None, mode="warn")
    assert rc == 0
    assert state["last_verify"] == "fail"
    assert any("no pinned signing key" in f for f in state["failures"])


def test_cli_missing_pin_enforce_fails(bundle, keypair, tmp_path):
    priv_pem, _, _ = keypair
    _sign(bundle, priv_pem)
    rc, _state = _cli(bundle, tmp_path, pin_text=None, mode="enforce")
    assert rc == 1


def test_cli_missing_mode_file_defaults_to_warn(bundle, keypair, tmp_path):
    priv_pem, _, _ = keypair
    _sign(bundle, priv_pem)
    (bundle / "implant.py").write_text("x\n")
    rc, state = _cli(bundle, tmp_path, pin_text=None, mode=None)
    assert rc == 0
    assert state["mode"] == "warn"


# ── signer CLI (what the deploy workflows run) ───────────────────────────────


def test_sign_bundle_cli_round_trip(bundle, keypair, tmp_path, monkeypatch):
    priv_pem, pub_pem, _ = keypair
    monkeypatch.setenv("BUNDLE_SIGNING_KEY", priv_pem)
    rc = sign_bundle.main([
        "--bundle", str(bundle), "--channel", "dev",
        "--version", "dev.42.2026.07.03", "--build", "42",
    ])
    assert rc == 0
    rc2, state = _cli(bundle, tmp_path, pin_text=pub_pem, mode="enforce")
    assert rc2 == 0
    assert state["last_verify"] == "ok"


def test_sign_bundle_cli_missing_key_fails(bundle, monkeypatch):
    monkeypatch.delenv("BUNDLE_SIGNING_KEY", raising=False)
    rc = sign_bundle.main([
        "--bundle", str(bundle), "--channel", "dev",
        "--version", "v", "--build", "1",
    ])
    assert rc == 1


def test_sign_bundle_self_verify_catches_post_sign_tamper(bundle, keypair):
    """The signer's self-verification runs the real verifier — a bundle whose
    signature does not match its own contents must never leave CI."""
    priv_pem, _, _ = keypair
    other_priv, _, _ = _keypair()
    manifest = sign_bundle.build_manifest(bundle, version="v", channel="dev", build=1)
    manifest_bytes = (json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n").encode()
    # Sign with a DIFFERENT key than the one self-verify derives — mimics any
    # sign/verify divergence.
    sig, _pub = sign_bundle.sign_manifest_bytes(manifest_bytes, other_priv)
    (bundle / "MANIFEST").write_bytes(manifest_bytes)
    (bundle / "MANIFEST.sig").write_text(sig + "\n")
    _, _, raw = _keypair()
    assert not bundle_verify.verify_bundle(bundle, "dev", [raw], state=None).ok


# ── pure-python fallback verifier ────────────────────────────────────────────


def test_vendored_ed25519_agrees_with_cryptography(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    manifest_bytes = (bundle / "MANIFEST").read_bytes()
    sig_b64 = (bundle / "MANIFEST.sig").read_text().strip()
    sig = base64.b64decode(sig_b64)
    assert bundle_verify._ed25519_verify(raw, manifest_bytes, sig)
    assert not bundle_verify._ed25519_verify(raw, manifest_bytes + b"x", sig)


# ── bytecode pruning (the fleet-freeze regression) ───────────────────────────
#
# Build 220 shipped the overlay-logo fix on 2026-08-17 and every station refused
# it four times a day for three days: two root-owned .pyc files under
# /opt/rovimen/station-bundle/__pycache__ (created by a python run inside the
# bundle dir two minutes after the deploy) had no manifest entry, so the
# exact-set check failed and updater.sh discarded the bundle. The fleet stayed on
# 218 and kept rendering the old oversized logo. These tests pin both halves of
# the fix: staging bytecode is pruned rather than fatal, and the signer can never
# manifest it in the first place.


def test_stray_bytecode_in_staging_does_not_reject_bundle(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    cache = bundle / "__pycache__"
    cache.mkdir()
    (cache / "overlay.cpython-312.pyc").write_bytes(b"\x00\x00fake bytecode")
    (cache / "config_migrate.cpython-312.pyc").write_bytes(b"\x00\x00more")

    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)

    assert result.ok, result.failures
    # Pruned, not merely ignored: nothing unverified survives to the install step.
    assert not cache.exists()


def test_nested_bytecode_pruned(bundle, keypair):
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    nested = bundle / "assets" / "__pycache__"
    nested.mkdir()
    (nested / "thing.cpython-311.pyc").write_bytes(b"x")

    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)

    assert result.ok, result.failures
    assert not nested.exists()


def test_prune_bytecode_reports_what_it_removed(bundle):
    cache = bundle / "__pycache__"
    cache.mkdir()
    (cache / "a.cpython-312.pyc").write_bytes(b"x")
    (cache / "b.cpython-312.pyc").write_bytes(b"y")

    removed = bundle_verify.prune_bytecode(bundle)

    assert sorted(removed) == [
        "__pycache__/a.cpython-312.pyc",
        "__pycache__/b.cpython-312.pyc",
    ]
    assert bundle_verify.prune_bytecode(bundle) == []


def test_non_bytecode_extra_file_still_rejected(bundle, keypair):
    """Pruning bytecode must not weaken the exact-set check for anything else."""
    priv_pem, _, raw = keypair
    _sign(bundle, priv_pem)
    (bundle / "backdoor.py").write_text("import os\n")

    result = bundle_verify.verify_bundle(bundle, "dev", [raw], state=None)

    assert not result.ok
    assert any("extra file not in manifest: backdoor.py" in f for f in result.failures)


def test_signer_never_manifests_bytecode(bundle, keypair):
    """A stray import during CI staging cannot produce an unshippable bundle."""
    priv_pem, _, raw = keypair
    cache = bundle / "__pycache__"
    cache.mkdir()
    (cache / "overlay.cpython-312.pyc").write_bytes(b"\x00\x00fake")

    _sign(bundle, priv_pem)

    manifest = json.loads((bundle / "MANIFEST").read_bytes())
    assert not any("__pycache__" in rel for rel in manifest["files"])
    assert bundle_verify.verify_bundle(bundle, "dev", [raw], state=None).ok


# ── --no-state (the anti-rollback self-poisoning trap) ───────────────────────
#
# Before the installed-build floor, a successful verify advanced the
# anti-rollback counter: verifying a staged bundle by hand on gmnro02 recorded
# build 220 while the live install stayed on 218, and the next real update
# failed with "rollback rejected: bundle build 220 <= last verified build 220".
# A preflight still must not record a bundle it does not install.


def test_no_state_does_not_record_a_successful_verify(bundle, keypair, tmp_path, capsys):
    priv_pem, pub_pem, _raw = keypair
    _sign(bundle, priv_pem)
    pub = tmp_path / "pin.pem"
    pub.write_text(pub_pem)
    state = tmp_path / "state.json"
    mode = tmp_path / "mode"
    mode.write_text("enforce")

    rc = bundle_verify.main([
        "--staging", str(bundle), "--channel", "dev",
        "--pubkey", str(pub), "--state", str(state),
        "--mode-file", str(mode), "--no-state",
    ])

    assert rc == 0
    assert not state.exists(), "a stateless check must not touch the counter"


def test_default_run_still_records_state(bundle, keypair, tmp_path):
    priv_pem, pub_pem, _raw = keypair
    _sign(bundle, priv_pem)
    pub = tmp_path / "pin.pem"
    pub.write_text(pub_pem)
    state = tmp_path / "state.json"
    mode = tmp_path / "mode"
    mode.write_text("enforce")

    rc = bundle_verify.main([
        "--staging", str(bundle), "--channel", "dev",
        "--pubkey", str(pub), "--state", str(state), "--mode-file", str(mode),
    ])

    assert rc == 0
    assert json.loads(state.read_text())["build"] == 42


def test_no_state_leaves_an_existing_counter_untouched(bundle, keypair, tmp_path):
    """The poisoning scenario: verify 42 by hand while 41 stays installed."""
    priv_pem, pub_pem, _raw = keypair
    _sign(bundle, priv_pem)
    pub = tmp_path / "pin.pem"
    pub.write_text(pub_pem)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"channel": "dev", "build": 41,
                                 "version": "dev.41", "last_verify": "ok"}))
    mode = tmp_path / "mode"
    mode.write_text("enforce")

    bundle_verify.main([
        "--staging", str(bundle), "--channel", "dev",
        "--pubkey", str(pub), "--state", str(state),
        "--mode-file", str(mode), "--no-state",
    ])

    assert json.loads(state.read_text())["build"] == 41


# ── installed-build floor through the CLI (state written by older verifiers) ──
#
# Stations carry a .bundle_state.json in the pre-floor format ({channel, build,
# version, last_verify, ...}) where `build` is the last VERIFIED bundle. The new
# verifier reads the floor from the .version next to the state file.


def _cli_with_installed(bundle, tmp_path, *, installed: str | None, state: dict,
                        build: int, keypair) -> tuple[int, dict]:
    priv_pem, pub_pem, _ = keypair
    _sign(bundle, priv_pem, channel="main", version=f"{build}.2026.11.21", build=build)
    if installed is not None:
        (tmp_path / ".version").write_text(installed + "\n")
    return _cli(bundle, tmp_path, pin_text=pub_pem, mode="enforce",
                channel="main", state=state)


def test_old_format_state_in_sync_accepts_the_next_build(bundle, keypair, tmp_path):
    rc, state = _cli_with_installed(
        bundle, tmp_path, installed="549.2026.11.19", build=550, keypair=keypair,
        state={"channel": "main", "build": 549, "version": "549.2026.11.19",
               "last_verify": "ok", "mode": "enforce", "failures": []})
    assert rc == 0
    assert state["build"] == 550
    assert state["update"]["status"] == "started"


def test_gmnro05_stuck_shape_heals_on_the_first_run(bundle, keypair, tmp_path):
    """Verified 526, installed 522 (the cp -a backup died in between)."""
    rc, state = _cli_with_installed(
        bundle, tmp_path, installed="522.2026.11.04", build=526, keypair=keypair,
        state={"channel": "main", "build": 526, "version": "526.2026.11.06",
               "last_verify": "ok", "mode": "enforce", "failures": []})
    assert rc == 0, state
    assert state["last_verify"] == "ok"


def test_cli_build_below_installed_still_refused(bundle, keypair, tmp_path):
    rc, state = _cli_with_installed(
        bundle, tmp_path, installed="549.2026.11.19", build=548, keypair=keypair,
        state={"channel": "main", "build": 549, "last_verify": "ok"})
    assert rc == 1
    assert state["last_verify"] == "fail"
    assert state["build"] == 549
    assert any("< installed build 549" in f for f in state["failures"])


def test_missing_version_file_falls_back_to_verified_build(bundle, keypair, tmp_path):
    rc, _ = _cli_with_installed(
        bundle, tmp_path, installed=None, build=548, keypair=keypair,
        state={"channel": "main", "build": 549, "last_verify": "ok"})
    assert rc == 1


def test_record_update_merges_into_state(tmp_path):
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({
        "channel": "main", "build": 550, "last_verify": "ok",
        "update": {"status": "started", "version": "550.2026.11.21"}}))
    rc = bundle_verify.main(["--state", str(state_file), "--record-update", "unhealthy",
                             "--failed-services", "color-capture rovimen-pusher"])
    assert rc == 0
    state = json.loads(state_file.read_text())
    assert state["build"] == 550 and state["last_verify"] == "ok"
    assert state["update"]["status"] == "unhealthy"
    assert state["update"]["version"] == "550.2026.11.21"
    assert state["update"]["failed_services"] == ["color-capture", "rovimen-pusher"]


def test_verify_requires_staging_without_record_update(tmp_path):
    with pytest.raises(SystemExit):
        bundle_verify.main(["--state", str(tmp_path / "s.json")])
