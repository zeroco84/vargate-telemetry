#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""One-shot init for the SoftHSM2 token + Telemetry KEK (T1.6).

Idempotent — running it twice is a no-op. Run inside the celery-worker
container:

    docker compose run --rm celery-worker python scripts/init_telemetry_kek.py

The conftest auto-runs this before the crypto tests, so manual invocation
is only needed for production-style bootstrap or the bench in T1.8.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys


def ensure_token() -> None:
    """Initialize the SoftHSM2 token if not already present.

    Token init goes through `softhsm2-util` rather than python-pkcs11
    because PKCS#11 itself does not expose a token-init operation; the
    spec leaves that to vendor tooling.
    """
    label = os.environ["HSM_TOKEN_LABEL"]

    result = subprocess.run(
        ["softhsm2-util", "--show-slots"],
        capture_output=True,
        text=True,
        check=True,
    )

    label_pattern = re.compile(
        r"^\s*Label:\s+" + re.escape(label) + r"\s*$",
        re.MULTILINE,
    )
    if label_pattern.search(result.stdout):
        print(f"Token '{label}' already initialized.", file=sys.stderr)
        return

    print(f"Initializing token '{label}'...", file=sys.stderr)
    subprocess.run(
        [
            "softhsm2-util",
            "--init-token",
            "--free",
            "--label",
            label,
            "--so-pin",
            os.environ["HSM_SO_PIN"],
            "--pin",
            os.environ["HSM_PIN"],
        ],
        check=True,
    )
    print(f"Token '{label}' initialized.", file=sys.stderr)


def ensure_kek() -> None:
    """Create the Telemetry KEK if not already present."""
    # Imported here so token init runs before any python-pkcs11 lib is
    # opened; the lib caches slot info and we want it to see the freshly
    # initialized token cleanly.
    from vargate_telemetry.crypto.hsm import get_or_create_kek

    get_or_create_kek()
    print(
        f"KEK ready: label={os.environ['HSM_KEK_LABEL']}",
        file=sys.stderr,
    )


def main() -> int:
    ensure_token()
    ensure_kek()
    return 0


if __name__ == "__main__":
    sys.exit(main())
