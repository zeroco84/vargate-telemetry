# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""HSM-backed Key Encryption Key (KEK) lifecycle and DEK wrapping (T1.6).

The Telemetry KEK is a single AES-256 secret-key object inside SoftHSM2
identified by `HSM_KEK_LABEL`. It is generated once (idempotently) by
`scripts/init_telemetry_kek.py` and never leaves the HSM. Every per-tenant
DEK is wrapped (AES-GCM encrypted) by the KEK; the wrapped form is what
lands in Postgres in `tenant_deks` from T1.7 onward.

Losing the KEK crypto-shreds every wrapped DEK. The HSM token volume
(`vargate-hsm-tokens` in dev) must be backed up alongside Postgres in any
deployment that wants to survive disk loss.
"""

from __future__ import annotations

import os
from typing import Any

import pkcs11
from pkcs11 import Attribute, KeyType, Mechanism, MechanismFlag, ObjectClass

# Lazy module-level singletons. PKCS#11 sessions are expensive to open and
# python-pkcs11 is process-local, so caching is safe — the worker process
# holds the session for its lifetime.
_lib: Any = None
_session: Any = None


def _hsm_session() -> Any:
    """Open (or reuse) a USER-authenticated PKCS#11 session against the token."""
    global _lib, _session
    if _session is None:
        _lib = pkcs11.lib(os.environ["PKCS11_MODULE"])
        token = _lib.get_token(token_label=os.environ["HSM_TOKEN_LABEL"])
        _session = token.open(user_pin=os.environ["HSM_PIN"], rw=True)
    return _session


def get_or_create_kek() -> Any:
    """Idempotently fetch (or create) the Telemetry KEK by label.

    The KEK is an AES-256 secret-key object with ENCRYPT/DECRYPT capability
    (we wrap DEK bytes via AES-GCM, which is the encrypt/decrypt mechanism,
    not the PKCS#11 wrap/unwrap mechanism).
    """
    s = _hsm_session()
    label = os.environ["HSM_KEK_LABEL"]

    keys = list(s.get_objects({
        Attribute.LABEL: label,
        Attribute.CLASS: ObjectClass.SECRET_KEY,
    }))
    if keys:
        return keys[0]

    return s.generate_key(
        KeyType.AES,
        256,
        label=label,
        store=True,
        capabilities=MechanismFlag.ENCRYPT | MechanismFlag.DECRYPT,
    )


def wrap_dek(dek_bytes: bytes) -> bytes:
    """AES-GCM-wrap a 32-byte DEK with the KEK; returns iv || ciphertext_with_tag."""
    if len(dek_bytes) != 32:
        raise ValueError(f"DEK must be 32 bytes (got {len(dek_bytes)})")
    kek = get_or_create_kek()
    iv = os.urandom(12)
    ciphertext = kek.encrypt(
        dek_bytes,
        mechanism=Mechanism.AES_GCM,
        mechanism_param=(iv, b"", 128),
    )
    return iv + ciphertext


def unwrap_dek(wrapped: bytes) -> bytes:
    """Inverse of `wrap_dek`. Returns the original 32-byte DEK."""
    if len(wrapped) < 12 + 16:
        raise ValueError(f"wrapped blob too short ({len(wrapped)} bytes)")
    kek = get_or_create_kek()
    iv, ct = wrapped[:12], wrapped[12:]
    return kek.decrypt(
        ct,
        mechanism=Mechanism.AES_GCM,
        mechanism_param=(iv, b"", 128),
    )
