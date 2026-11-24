#!/usr/bin/env python3
"""gen_bundle_signing_key.py — mint the ed25519 bundle-signing keypair.

CI signs every station update bundle (``deployment/sign_bundle.py``) with a
private key held ONLY as the GitHub secret ``BUNDLE_SIGNING_KEY``; stations
verify against the public pin ``/etc/rovimen/bundle_signing_pub.pem``
(SSH-delivered, sourced from ``deployment/bundle_signing_pub.pem``).

The private key is written to **stdout only** so it never touches disk —
pipe it straight into the GitHub secret:

    uv run tools/gen_bundle_signing_key.py | gh secret set BUNDLE_SIGNING_KEY

The public half is written to ``deployment/bundle_signing_pub.pem`` (commit
it — it is the pin source for the installer and the SSH pin round).

Rotation (dual-pin window): mint a new pair with ``--force``, append the NEW
public pem to each station's ``/etc/rovimen/bundle_signing_pub.pem`` over SSH
(the verifier accepts multiple concatenated PEM blocks), update the GitHub
secret, then remove the old block after the next verified update on every
station. See ``docs/signed_bundles.md``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PUBLIC = REPO_ROOT / "deployment" / "bundle_signing_pub.pem"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Mint the bundle-signing ed25519 keypair.")
    ap.add_argument("--public", type=Path, default=DEFAULT_PUBLIC,
                    help=f"public PEM output path (default {DEFAULT_PUBLIC})")
    ap.add_argument("--force", action="store_true",
                    help="overwrite an existing public key (rotation)")
    args = ap.parse_args(argv)

    if args.public.exists() and not args.force:
        print(f"refusing to overwrite existing public key {args.public} "
              f"(use --force to rotate)", file=sys.stderr)
        return 1

    if sys.stdout.isatty():
        print("WARNING: stdout is a terminal — the PRIVATE key is about to be "
              "displayed. Pipe it instead:\n"
              "  uv run tools/gen_bundle_signing_key.py | gh secret set BUNDLE_SIGNING_KEY",
              file=sys.stderr)

    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("ascii")

    args.public.parent.mkdir(parents=True, exist_ok=True)
    args.public.write_text(pub_pem)

    # Private key to stdout ONLY (pipe to `gh secret set`); everything else
    # to stderr so the pipe stays clean.
    sys.stdout.write(priv_pem)
    print(f"public key written: {args.public}", file=sys.stderr)
    print("next: commit the public pem; the private key exists only in the "
          "GitHub secret and this pipe.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
