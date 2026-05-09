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

    # AES-CBC-PAD: 16-byte IV + 16-byte-aligned ciphertext (PKCS#7 pad).
    # For a 32-byte DEK the wrapped form is 16 + 48 = 64 bytes. The exact
    # length matters less than the invariants: it differs from the DEK
    # in plaintext and is strictly longer than the DEK.
    assert wrapped != dek
    assert len(wrapped) > len(dek)

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


# --- T2.0: HMAC integrity-tag tampering tests --------------------------------


@pytest.fixture
def clean_secrets() -> None:
    """Empty tenant_deks and encrypted_secrets before/after each integrity test."""
    from vargate_telemetry.db import engine
    from sqlalchemy import text as sql_text

    with engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE encrypted_secrets RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE tenant_deks RESTART IDENTITY CASCADE"))
    yield
    with engine.begin() as conn:
        conn.execute(sql_text("TRUNCATE TABLE encrypted_secrets RESTART IDENTITY CASCADE"))
        conn.execute(sql_text("TRUNCATE TABLE tenant_deks RESTART IDENTITY CASCADE"))


def _flip_first_bit(b: bytes) -> bytes:
    """Toggle the low bit of the first byte of `b`."""
    return bytes([b[0] ^ 0x01]) + b[1:]


def test_unseal_rejects_tampered_ciphertext(clean_secrets: None) -> None:
    """A bit-flip in encrypted_secrets.ciphertext makes unseal raise IntegrityError."""
    from sqlalchemy import text as sql_text

    from vargate_telemetry.crypto import (
        IntegrityError,
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )
    from vargate_telemetry.db import engine

    provision_tenant_dek("tenant-T")
    seal_secret("tenant-T", "k", b"original")

    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT ciphertext FROM encrypted_secrets "
                "WHERE tenant_id = 'tenant-T' AND secret_name = 'k'"
            )
        ).first()
        tampered = _flip_first_bit(bytes(row[0]))
        conn.execute(
            sql_text(
                "UPDATE encrypted_secrets SET ciphertext = :ct "
                "WHERE tenant_id = 'tenant-T' AND secret_name = 'k'"
            ),
            {"ct": tampered},
        )

    with pytest.raises(IntegrityError):
        unseal_secret("tenant-T", "k")


def test_unseal_rejects_swapped_dek(clean_secrets: None) -> None:
    """Swapping tenant A's wrapped_dek into tenant B's row makes unseal raise."""
    from sqlalchemy import text as sql_text

    from vargate_telemetry.crypto import (
        IntegrityError,
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )
    from vargate_telemetry.db import engine

    provision_tenant_dek("tenant-A")
    provision_tenant_dek("tenant-B")
    seal_secret("tenant-B", "k", b"B-data")

    # Copy A's (wrapped_dek, integrity_tag) over B's, leaving A's
    # ciphertext entries alone and B's ciphertext intact. The tenant_id
    # binding in the HMAC catches the swap because A's tag was computed
    # with `'tenant-A' || ":" || A_wrapped_dek`, not with `'tenant-B'`.
    with engine.begin() as conn:
        a_row = conn.execute(
            sql_text(
                "SELECT wrapped_dek, integrity_tag FROM tenant_deks "
                "WHERE tenant_id = 'tenant-A'"
            )
        ).first()
        conn.execute(
            sql_text(
                "UPDATE tenant_deks SET wrapped_dek = :wd, integrity_tag = :tag "
                "WHERE tenant_id = 'tenant-B'"
            ),
            {"wd": bytes(a_row[0]), "tag": bytes(a_row[1])},
        )

    with pytest.raises(IntegrityError):
        unseal_secret("tenant-B", "k")


def test_unseal_rejects_tampered_tag(clean_secrets: None) -> None:
    """A bit-flip in encrypted_secrets.integrity_tag makes unseal raise."""
    from sqlalchemy import text as sql_text

    from vargate_telemetry.crypto import (
        IntegrityError,
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )
    from vargate_telemetry.db import engine

    provision_tenant_dek("tenant-T")
    seal_secret("tenant-T", "k", b"original")

    with engine.begin() as conn:
        row = conn.execute(
            sql_text(
                "SELECT integrity_tag FROM encrypted_secrets "
                "WHERE tenant_id = 'tenant-T' AND secret_name = 'k'"
            )
        ).first()
        tampered = _flip_first_bit(bytes(row[0]))
        conn.execute(
            sql_text(
                "UPDATE encrypted_secrets SET integrity_tag = :tag "
                "WHERE tenant_id = 'tenant-T' AND secret_name = 'k'"
            ),
            {"tag": tampered},
        )

    with pytest.raises(IntegrityError):
        unseal_secret("tenant-T", "k")


def test_t1_seal_unseal_still_passes(clean_secrets: None) -> None:
    """The T1.7 seal/unseal happy path is unaffected by the new integrity layer."""
    from vargate_telemetry.crypto import (
        provision_tenant_dek,
        seal_secret,
        unseal_secret,
    )

    provision_tenant_dek("tenant-Z")
    seal_secret("tenant-Z", "anthropic_admin_key", b"sk-ant-test-1234")
    assert unseal_secret("tenant-Z", "anthropic_admin_key") == b"sk-ant-test-1234"

    # Rotation path also still works (UPSERT updates iv + ciphertext + tag).
    seal_secret("tenant-Z", "anthropic_admin_key", b"sk-ant-rotated-5678")
    assert unseal_secret("tenant-Z", "anthropic_admin_key") == b"sk-ant-rotated-5678"
