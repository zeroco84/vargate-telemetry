# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""HMAC-SHA256 integrity tags for envelope-encrypted records (T2.0).

T1.6's wrap mechanism (AES-CBC-PAD) does not authenticate. A tampered
wrapped DEK or a tampered ciphertext decrypts to garbage rather than
raising at unwrap time, which leaks integrity-failure detection to
downstream code. T2.0 closes that gap with HMAC-SHA256 tags computed
over `tenant_id || ":" || ciphertext` (the tenant_id binding catches
cross-tenant DEK swaps).

Key derivation:

  material = kek.encrypt(<fixed label>, AES-CBC-PAD, fixed-IV)
  hmac_key = HKDF-SHA256(material, salt=..., info=..., length=32)

The HMAC key is module-cached after first derivation. Same KEK input
yields the same HMAC key, so every seal verifies against the same key
the seal that wrote it used. Crypto-shredding the KEK invalidates every
existing tag because re-derivation under a new KEK produces a different
HMAC key — a feature, not a bug.

Verification is `hmac.compare_digest` (constant time) and MUST run
BEFORE any decryption attempt. Successful verify means the wrapped
record has not been tampered with and the HSM operations that follow
can be trusted.
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
from typing import Optional

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from pkcs11 import Mechanism

# Fixed inputs to the KEK-based key derivation. Changing any of these
# invalidates every existing integrity tag in the database; treat as
# load-bearing constants.
_HMAC_KEY_DERIVATION_LABEL = b"vargate.telemetry/hmac-key-derivation/v1"
_HMAC_KEY_DERIVATION_IV = b"\x00" * 16
_HKDF_SALT = b"vargate.telemetry/hmac-key-salt/v1"
_HKDF_INFO = b"vargate.telemetry/hmac-key/v1"

_hmac_key: Optional[bytes] = None


class IntegrityError(Exception):
    """Raised when an HMAC integrity tag does not match the recomputed value."""


def _get_hmac_key() -> bytes:
    """Derive (and cache) the HMAC key from the HSM KEK.

    The derivation goes through the working PKCS#11 encrypt path
    (AES-CBC-PAD with a fixed IV over a fixed label) to produce a
    deterministic chunk of secret material, then runs HKDF-SHA256 over
    that to produce a uniformly-distributed 32-byte HMAC key.
    """
    global _hmac_key
    if _hmac_key is None:
        # Lazy import: hsm.py opens a PKCS#11 session at module import,
        # which we don't want to trigger from package __init__.
        from vargate_telemetry.crypto.hsm import get_or_create_kek

        kek = get_or_create_kek()
        material = kek.encrypt(
            _HMAC_KEY_DERIVATION_LABEL,
            mechanism=Mechanism.AES_CBC_PAD,
            mechanism_param=_HMAC_KEY_DERIVATION_IV,
        )
        _hmac_key = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=_HKDF_SALT,
            info=_HKDF_INFO,
        ).derive(material)
    return _hmac_key


def compute_integrity_tag(tenant_id: str, ciphertext: bytes) -> bytes:
    """Return the 32-byte HMAC-SHA256 tag binding `ciphertext` to `tenant_id`."""
    if not tenant_id:
        raise ValueError("tenant_id required")

    key = _get_hmac_key()
    msg = tenant_id.encode("utf-8") + b":" + ciphertext
    return _hmac.new(key, msg, hashlib.sha256).digest()


def verify_integrity_tag(tenant_id: str, ciphertext: bytes, tag: bytes) -> None:
    """Constant-time verify; raise `IntegrityError` on mismatch.

    MUST be called BEFORE any decryption attempt. A successful verify
    means the bytes have not been tampered with and the HSM operations
    that follow can be trusted.
    """
    expected = compute_integrity_tag(tenant_id, ciphertext)
    if not _hmac.compare_digest(expected, tag):
        raise IntegrityError(
            f"integrity check failed for tenant {tenant_id!r}"
        )
