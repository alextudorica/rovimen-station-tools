"""Tests for `updater.sh --verify-only` — the enforce preflight.

The preflight answers one question: *would this station accept the bundle the
VPS is serving, if it were in enforce mode?* Operators run it before flipping a
station to enforce, before the first deploy after a signing-key rotation, and
when a station rejoins the fleet after weeks offline.

Its safety contract is what these tests pin down, because getting any of it
wrong is how a fleet locks itself out:

  * it NEVER writes bundle state (recording a verify that is not followed by an
    install strands the station on an anti-rollback failure — gmnro02, 2026-08);
  * it NEVER touches the live install or the real staging directory;
  * it reports on ENFORCE semantics regardless of the station's current mode,
    otherwise a warn-mode station is told PASS for a bundle it would reject;
  * it PASSES on a station already running the bundle the VPS serves, rather
    than raising a false anti-rollback alarm that talks an operator out of a
    perfectly healthy flip.

The updater is driven for real (`bash updater.sh --verify-only`) with `rsync`
stubbed on PATH to serve a locally signed bundle, so the shell logic itself is
under test, not a Python re-implementation of it.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import bundle_verify

# deployment/ is not on the conftest sys.path (station + dashboard code only).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "deployment"))
import sign_bundle  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
UPDATER = REPO_ROOT / "rovimen-scripts" / "updater.sh"
VERIFIER = REPO_ROOT / "rovimen-scripts" / "bundle_verify.py"

SENTINEL = "# live install — must survive every preflight\n"


def _keypair() -> tuple[str, str]:
    """Return (private_pem, public_pem)."""
    priv = Ed25519PrivateKey.generate()
    return (
        priv.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        priv.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


@pytest.fixture
def station(tmp_path):
    """A fake station: HOME with a live install, a signed bundle the stubbed
    rsync serves, a pinned pubkey, and a stub bin dir on PATH.

    Returns a small namespace of paths plus a ``run()`` helper.
    """
    priv_pem, pub_pem = _keypair()

    home = tmp_path / "home"
    live = home / "rovimen_scripts"
    live.mkdir(parents=True)
    (live / "bundle_verify.py").write_text(VERIFIER.read_text())
    (live / "config.json").write_text(
        json.dumps({"vps_host": "vps.invalid", "update_channel": "main"}) + "\n"
    )
    # A sentinel in the live install: any preflight that writes through to the
    # live directory changes this, and the test catches it.
    (live / "station_api.py").write_text(SENTINEL)

    # The bundle the VPS is "serving", signed with the matching private key.
    bundle = tmp_path / "vps-bundle"
    bundle.mkdir()
    (bundle / "station_api.py").write_text("print('new api')\n")
    (bundle / "updater.sh").write_text("#!/bin/bash\necho update\n")
    (bundle / ".version").write_text("240.2026.09.03\n")
    sign_bundle.sign_bundle(
        bundle, version="240.2026.09.03", channel="main", build=240,
        private_pem=priv_pem,
    )

    pin = tmp_path / "pin.pem"
    pin.write_text(pub_pem)

    # Stub rsync: ignore the remote spec, copy the fixture bundle into the
    # destination (the last argument), mirroring what the real rsync does.
    stubbin = tmp_path / "bin"
    stubbin.mkdir()
    rsync = stubbin / "rsync"
    rsync.write_text(
        '#!/usr/bin/env bash\n'
        'dst="${@: -1}"\n'
        'mkdir -p "$dst"\n'
        'cp -a "$STUB_BUNDLE_SRC"/. "$dst"/\n'
    )
    rsync.chmod(0o755)

    class Station:
        def __init__(self) -> None:
            self.home = home
            self.live = live
            self.bundle = bundle
            self.pin = pin
            self.state = live / ".bundle_state.json"

        def run(self, *, pubkey: Path | None = None) -> subprocess.CompletedProcess:
            env = dict(os.environ)
            env.update(
                HOME=str(home),
                PATH=f"{stubbin}{os.pathsep}{env['PATH']}",
                STUB_BUNDLE_SRC=str(bundle),
                ROVIMEN_BUNDLE_PUBKEY=str(pubkey or pin),
                ROVIMEN_BUNDLE_STATE=str(self.state),
            )
            return subprocess.run(
                ["bash", str(UPDATER), "--verify-only"],
                capture_output=True, text=True, env=env, timeout=120,
            )

    return Station()


# ── happy path ───────────────────────────────────────────────────────────────


def test_good_bundle_passes(station):
    r = station.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Result:   PASS" in r.stdout


def test_pass_is_reported_under_enforce_semantics_not_the_station_mode(station):
    """No mode file exists, so the station is effectively in warn. The preflight
    must still evaluate as enforce — that is the question being asked."""
    r = station.run()
    assert "verify OK (enforce)" in r.stdout + r.stderr


# ── the anti-rollback false alarm ────────────────────────────────────────────


def test_station_already_on_this_build_still_passes(station):
    """A healthy, fully up-to-date station must not fail its own preflight.

    Anti-rollback compares sequence numbers; it says nothing about whether this
    bundle is authentic. If the preflight honoured it, every station already
    running the served bundle would report FAIL and no operator would ever dare
    flip one to enforce."""
    station.state.write_text(json.dumps({
        "channel": "main", "build": 240, "version": "240.2026.09.03",
        "last_verify": "ok",
    }))
    r = station.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "Result:   PASS" in r.stdout
    assert "rollback" not in (r.stdout + r.stderr).lower()


# ── the no-state guarantee ───────────────────────────────────────────────────


def test_preflight_never_creates_bundle_state(station):
    """Recording a verify that is not followed by an install is how a station
    strands itself: the real update then reads as a rollback."""
    assert not station.state.exists()
    r = station.run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert not station.state.exists(), "preflight wrote bundle state"


def test_preflight_leaves_an_existing_counter_untouched(station):
    before = json.dumps({
        "channel": "main", "build": 238, "version": "238.2026.09.01",
        "last_verify": "ok",
    })
    station.state.write_text(before)
    station.run()
    assert station.state.read_text() == before


# ── the live install is never touched ────────────────────────────────────────


def test_live_install_untouched_on_pass(station):
    station.run()
    assert (station.live / "station_api.py").read_text() == SENTINEL
    assert (station.live / ".version").exists() is False


def test_live_install_untouched_on_fail(station, tmp_path):
    _, other_pub = _keypair()
    wrong = tmp_path / "wrong.pem"
    wrong.write_text(other_pub)
    station.run(pubkey=wrong)
    assert (station.live / "station_api.py").read_text() == SENTINEL


def test_staging_directory_is_discarded(station):
    station.run()
    assert not (station.home / "rovimen_scripts.verify").exists()


def test_preflight_does_not_use_the_real_staging_dir(station):
    """A timer-driven update racing a hand-run preflight must not have its
    staging directory pulled out from under it."""
    station.run()
    assert not (station.home / "rovimen_scripts.staging").exists()


# ── failure reporting ────────────────────────────────────────────────────────


def test_wrong_pin_fails_loudly(station, tmp_path):
    _, other_pub = _keypair()
    wrong = tmp_path / "wrong.pem"
    wrong.write_text(other_pub)
    r = station.run(pubkey=wrong)
    assert r.returncode == 1
    assert "Result:   FAIL" in r.stdout
    assert "Do NOT flip this station to enforce" in r.stdout


def test_tampered_bundle_fails(station):
    (station.bundle / "station_api.py").write_text("print('backdoor')\n")
    r = station.run()
    assert r.returncode == 1
    assert "Result:   FAIL" in r.stdout


def test_failure_carries_the_reject_marker(station, tmp_path):
    _, other_pub = _keypair()
    wrong = tmp_path / "wrong.pem"
    wrong.write_text(other_pub)
    r = station.run(pubkey=wrong)
    assert bundle_verify.REJECT_MARKER in r.stdout + r.stderr


def test_missing_verifier_reports_distinctly(station):
    """Exit 2, not 1: "this station cannot verify" is an operator problem to fix
    before flipping, not a verdict about the bundle."""
    (station.live / "bundle_verify.py").unlink()
    r = station.run()
    assert r.returncode == 2
    assert "NO VERIFIER" in r.stdout


# ── marker consistency across the two layers ─────────────────────────────────


def test_reject_marker_is_identical_in_updater_and_verifier():
    """One grep token must catch a rejection from either layer. If these drift,
    a fleet-wide log sweep silently misses half the rejections."""
    text = UPDATER.read_text()
    assert f'REJECT_MARKER="{bundle_verify.REJECT_MARKER}"' in text
