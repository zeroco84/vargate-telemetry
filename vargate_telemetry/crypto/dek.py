# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant Data Encryption Keys (DEKs) — generation, encrypt, decrypt.

DEKs are 32 random bytes (AES-256). They are generated in-memory only,
wrapped by the HSM-held KEK at provisioning time (T1.7), and persisted
in their wrapped form in `tenant_deks`. The plaintext DEK exists only
for the duration of an unwrap → encrypt/decrypt round trip; we never
write it to disk.

AAD (additional authenticated data) is the AES-GCM mechanism for binding
ciphertext to a context. T1.7+ callers pass the tenant_id and a stable
purpose string ("compliance-content", "secret/anthropic-admin-key",
etc.) as AAD so a ciphertext intended for one purpose cannot be
silently decrypted under another.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def generate_dek() -> bytes:
    """Return a fresh 32-byte AES-256 key drawn from os.urandom."""
    return os.urandom(32)


def encrypt_with_dek(dek: bytes, plaintext: bytes, aad: bytes = b"") -> tuple[bytes, bytes]:
    """AES-GCM encrypt; returns (iv, ciphertext_with_tag)."""
    iv = os.urandom(12)
    ct = AESGCM(dek).encrypt(iv, plaintext, aad)
    return iv, ct


def decrypt_with_dek(
    dek: bytes,
    iv: bytes,
    ciphertext: bytes,
    aad: bytes = b"",
) -> bytes:
    """AES-GCM decrypt. Raises `cryptography.exceptions.InvalidTag` if AAD or tag mismatch."""
    return AESGCM(dek).decrypt(iv, ciphertext, aad)
