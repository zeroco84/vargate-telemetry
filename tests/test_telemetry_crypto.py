# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Crypto smoke tests for T1.6.

Module-scoped autouse fixture runs the KEK init script first, so tests in
this file work cold against a fresh `vargate-hsm-tokens` volume. Tests in
other files don't touch HSM and aren't affected by this fixture.
"""

from __future__ import annotations

import os
import subprocess

import pytest


@pytest.fixture(scope="module", autouse=True)
def hsm_initialized() -> None:
    """Ensure the SoftHSM2 token + Telemetry KEK exist."""
    subprocess.run(
        ["python", "/app/scripts/init_telemetry_kek.py"],
        check=True,
    )


def test_kek_idempotent_init() -> None:
    """`get_or_create_kek` does not create a second key on repeated calls."""
    import pkcs11
    from pkcs11 import Attribute, ObjectClass

    from vargate_telemetry.crypto.hsm import _hsm_session, get_or_create_kek

    get_or_create_kek()
    get_or_create_kek()

    s = _hsm_session()
    label = os.environ["HSM_KEK_LABEL"]
    keys = list(
        s.get_objects(
            {
                Attribute.LABEL: label,
                Attribute.CLASS: ObjectClass.SECRET_KEY,
            }
        )
    )
    assert len(keys) == 1, (
        f"Expected exactly one KEK with label {label!r}, found {len(keys)}"
    )


def test_dek_wrap_unwrap_roundtrip() -> None:
    """Generate a DEK, wrap with KEK, unwrap, and assert byte-equality."""
    from vargate_telemetry.crypto.dek import generate_dek
    from vargate_telemetry.crypto.hsm import unwrap_dek, wrap_dek

    dek = generate_dek()
    wrapped = wrap_dek(dek)

    # AES-KEY-WRAP-PAD adds integrity-checked padding; for a 32-byte DEK
    # the wrapped form is 40 bytes. The exact length matters less than
    # the invariants: it differs from the DEK in plaintext and is at
    # least as long.
    assert wrapped != dek
    assert len(wrapped) >= len(dek)

    assert unwrap_dek(wrapped) == dek


def test_aesgcm_encrypt_decrypt() -> None:
    """`encrypt_with_dek` / `decrypt_with_dek` round-trip arbitrary plaintext."""
    from vargate_telemetry.crypto.dek import (
        decrypt_with_dek,
        encrypt_with_dek,
        generate_dek,
    )

    dek = generate_dek()
    plaintext = b"the quick brown fox jumps over the lazy dog" * 16

    iv, ciphertext = encrypt_with_dek(dek, plaintext)
    assert decrypt_with_dek(dek, iv, ciphertext) == plaintext


def test_aesgcm_aad_required() -> None:
    """Decrypting with the wrong AAD must raise."""
    from cryptography.exceptions import InvalidTag

    from vargate_telemetry.crypto.dek import (
        decrypt_with_dek,
        encrypt_with_dek,
        generate_dek,
    )

    dek = generate_dek()
    iv, ciphertext = encrypt_with_dek(dek, b"hello", aad=b"context-A")

    with pytest.raises(InvalidTag):
        decrypt_with_dek(dek, iv, ciphertext, aad=b"context-B")
