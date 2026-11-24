#!/usr/bin/env python3
"""sign_bundle.py — CI-side signer for station update bundles.

Run by the deploy workflows after the bundle is staged and ``.version`` is
written. Produces two files in the bundle directory:

  * ``MANIFEST`` — canonical JSON ``{schema, version, channel, build,
    built_at, files: {relpath: sha256}}`` covering every file in the bundle.
  * ``MANIFEST.sig`` — base64 ed25519 signature over the exact MANIFEST file
    bytes, made with the private key held ONLY in the GitHub secret
    ``BUNDLE_SIGNING_KEY`` (PEM text in the environment, never on disk).

Stations verify with ``rovimen-scripts/bundle_verify.py`` against the pinned
public key ``/etc/rovimen/bundle_signing_pub.pem`` (SSH-delivered). After
signing, this script self-verifies the staged bundle with the same verifier
code the stations run — CI fails on any mismatch, so a broken bundle never
reaches the VPS.

Usage (CI):

    BUNDLE_SIGNING_KEY="$PEM" python deployment/sign_bundle.py \\
        --bundle /tmp/station-bundle-dev --channel dev \\
        --version "$(cat /tmp/station-bundle-dev/.version)" --build "$RUN_NUMBER"
"""

from __future__ import annotations

import argparse
import json
import base64
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import the station verifier for hashing + self-verification (same code path
# the stations execute).
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "rovimen-scripts"))

import bundle_verify  # noqa: E402

KEY_ENV = "BUNDLE_SIGNING_KEY"


def build_manifest(bundle: Path, *, version: str, channel: str, build: int) -> dict:
    """Hash every file in the bundle (MANIFEST/MANIFEST.sig excluded).

    Bytecode is pruned first: anything that imports a bundle module in place
    leaves a ``__pycache__`` behind, and a .pyc that ships without a manifest
    entry makes every station reject the bundle outright (build 220 was refused
    fleet-wide for three days for exactly that reason). Pruning here means a
    stray import during staging cannot produce an unshippable bundle.
    """
    pruned = bundle_verify.prune_bytecode(bundle)
    if pruned:
        print(f"pruned {len(pruned)} bytecode file(s) before manifesting: {pruned}")
    files: dict[str, str] = {}
    for p in sorted(bundle.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(bundle).as_posix()
        if rel in bundle_verify.EXEMPT_FILES:
            continue
        files[rel] = bundle_verify.sha256_file(p)
    return {
        "schema": 1,
        "version": version,
        "channel": channel,
        "build": build,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "files": files,
    }


def sign_manifest_bytes(manifest_bytes: bytes, private_pem: str) -> tuple[str, bytes]:
    """Return (base64 signature, raw 32-byte public key) for the manifest."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = serialization.load_pem_private_key(private_pem.encode(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("BUNDLE_SIGNING_KEY is not an ed25519 private key")
    sig = base64.b64encode(key.sign(manifest_bytes)).decode("ascii")
    pub_raw = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return sig, pub_raw


def sign_bundle(bundle: Path, *, version: str, channel: str, build: int,
                private_pem: str) -> None:
    manifest = build_manifest(bundle, version=version, channel=channel, build=build)
    if not manifest["files"]:
        raise SystemExit(f"refusing to sign an empty bundle: {bundle}")

    manifest_bytes = (
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    sig_b64, pub_raw = sign_manifest_bytes(manifest_bytes, private_pem)

    (bundle / bundle_verify.MANIFEST_NAME).write_bytes(manifest_bytes)
    (bundle / bundle_verify.SIGNATURE_NAME).write_text(sig_b64 + "\n")

    # Self-verify with the exact code stations run. No state -> anti-rollback
    # is skipped here (CI has no station state); everything else is exercised.
    result = bundle_verify.verify_bundle(bundle, channel, [pub_raw], state=None)
    if not result.ok:
        for f in result.failures:
            print(f"self-verify FAIL: {f}", file=sys.stderr)
        raise SystemExit("bundle self-verification failed — not deploying")

    print(f"signed bundle: channel={channel} build={build} version={version} "
          f"files={len(manifest['files'])}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Sign a staged station update bundle.")
    ap.add_argument("--bundle", type=Path, required=True, help="staged bundle directory")
    ap.add_argument("--channel", required=True, choices=("main", "dev"),
                    help="update channel this bundle is built for")
    ap.add_argument("--version", required=True, help="bundle version string (.version)")
    ap.add_argument("--build", type=int, required=True,
                    help="monotonic build number (CI run number)")
    args = ap.parse_args(argv)

    private_pem = os.environ.get(KEY_ENV, "")
    if not private_pem.strip():
        print(f"{KEY_ENV} is empty — set the GitHub secret before deploying",
              file=sys.stderr)
        return 1
    if not args.bundle.is_dir():
        print(f"bundle directory not found: {args.bundle}", file=sys.stderr)
        return 1

    sign_bundle(args.bundle, version=args.version, channel=args.channel,
                build=args.build, private_pem=private_pem)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
