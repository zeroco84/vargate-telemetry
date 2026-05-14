# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""TM2 — One-shot generator for the bridge JWT keypair.

Usage:

    python scripts/generate_bridge_jwt_keypair.py \\
        --out /home/vargate/secrets/ogma_bridge_jwt_private.pem

Generates a fresh ECDSA P-256 private key, writes it to the
target path as a PEM (PKCS#8, unencrypted), prints the kid you
should set in env.

Refuses to overwrite an existing file. To rotate, move the old
file aside, re-run, then redeploy. The CLAUDE.md rule "Bridge
JWT keypair is file-mounted ECDSA P-256" documents the rotation
posture.

Dev usage: target ``ops/dev-secrets/bridge_jwt_private.pem``
(gitignored) so a fresh clone + ``docker compose up`` works.
"""

from __future__ import annotations

import argparse
import os
import secrets
import stat
import sys
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec


def _default_kid() -> str:
    """Generate a kid the operator can override.

    Format: ``ogma-bridge-<8-hex>`` — short enough to log, random
    enough that two operators rotating independently won't collide.
    """
    return f"ogma-bridge-{secrets.token_hex(4)}"


def generate_keypair(out_path: Path, *, force: bool) -> None:
    if out_path.exists() and not force:
        print(
            f"refusing to overwrite existing key at {out_path}\n"
            "Move it aside or pass --force if you really mean to rotate "
            "in-place.",
            file=sys.stderr,
        )
        sys.exit(2)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    key = ec.generate_private_key(ec.SECP256R1())
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    out_path.write_bytes(pem)
    # 0600: only the file owner reads. Docker bind-mount inherits
    # the host file's permissions, so this matters for prod.
    out_path.chmod(stat.S_IRUSR | stat.S_IWUSR)

    suggested_kid = _default_kid()
    print(f"Wrote ES256 (P-256) private key to {out_path}")
    print(f"Suggested kid: {suggested_kid}")
    print(
        "Set in env:\n"
        f"  OGMA_BRIDGE_JWT_PRIVATE_KEY_PATH={out_path}\n"
        f"  OGMA_BRIDGE_JWT_KID={suggested_kid}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Generate an ECDSA P-256 keypair for the TM2 bridge JWT."
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Path to write the PEM (parent dirs are auto-created).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Overwrite an existing key at --out (rotation). Without "
            "this flag the script refuses to clobber existing material."
        ),
    )
    args = parser.parse_args()
    generate_keypair(args.out, force=args.force)


if __name__ == "__main__":
    main()
