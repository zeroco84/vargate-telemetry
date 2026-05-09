# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""HSM-backed Key Encryption Key (KEK) lifecycle and DEK wrapping (T1.6).

The Telemetry KEK is a single AES-256 secret-key object inside SoftHSM2,
identified by `HSM_KEK_LABEL`. It is generated once (idempotently) by
`scripts/init_telemetry_kek.py` and never leaves the HSM. Every per-tenant
DEK is wrapped (AES-KEY-WRAP-PAD, RFC 5649) by the KEK; the wrapped form
is what lands in Postgres in `tenant_deks` from T1.7 onward.

Why AES_KEY_WRAP_PAD and not AES_GCM via the HSM? Two reasons:

  - PKCS#11 has wrap_key/unwrap_key as the canonical "wrap a key with a
    KEK" operation; AES_KEY_WRAP_PAD is the standard mechanism for
    arbitrary-length keys (RFC 5649). Using it makes the intent explicit.
  - python-pkcs11's AES_GCM parameter packing is broken in 0.7.x — the
    Cython binding raises a bare TypeError when packing the GCM params
    struct. AES_KEY_WRAP_PAD takes no mechanism parameters and works
    cleanly.

The wrapped form is 40 bytes for a 32-byte DEK (8 bytes of integrity-
checked padding per RFC 5649). The HSM verifies the integrity check on
unwrap; a tampered wrapped blob causes unwrap_key to raise.

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
    """Open (or reuse) a USER-authenticated read-write PKCS#11 session."""
    global _lib, _session
    if _session is None:
        _lib = pkcs11.lib(os.environ["PKCS11_MODULE"])
        token = _lib.get_token(token_label=os.environ["HSM_TOKEN_LABEL"])
        _session = token.open(user_pin=os.environ["HSM_PIN"], rw=True)
    return _session


def get_or_create_kek() -> Any:
    """Idempotently fetch (or create) the Telemetry KEK by label.

    The KEK is an AES-256 secret-key object with WRAP/UNWRAP capability;
    that's what the wrap_key/unwrap_key operations require. Note: keys
    created with different capabilities cannot be retrofitted — if you
    have an old KEK with only ENCRYPT/DECRYPT, the HSM volume must be
    re-initialized.
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
        capabilities=MechanismFlag.WRAP | MechanismFlag.UNWRAP,
    )


def wrap_dek(dek_bytes: bytes) -> bytes:
    """Wrap a 32-byte DEK with the KEK using AES-KEY-WRAP-PAD.

    Returns ~40 bytes of ciphertext (RFC 5649 fixes the format).
    """
    if len(dek_bytes) != 32:
        raise ValueError(f"DEK must be 32 bytes (got {len(dek_bytes)})")

    s = _hsm_session()
    kek = get_or_create_kek()

    # Stage the DEK as a transient session key object — wrap_key takes a
    # key object, not raw bytes. SENSITIVE=False / EXTRACTABLE=True only
    # persist for the micro-window between create_object and destroy()
    # below; the key never leaves this function as plaintext.
    dek_obj = s.create_object({
        Attribute.CLASS: ObjectClass.SECRET_KEY,
        Attribute.KEY_TYPE: KeyType.AES,
        Attribute.VALUE: dek_bytes,
        Attribute.TOKEN: False,
        Attribute.EXTRACTABLE: True,
        Attribute.SENSITIVE: False,
    })
    try:
        return kek.wrap_key(dek_obj, mechanism=Mechanism.AES_KEY_WRAP_PAD)
    finally:
        dek_obj.destroy()


def unwrap_dek(wrapped: bytes) -> bytes:
    """Inverse of `wrap_dek`. Returns the original 32-byte DEK.

    AES-KEY-WRAP-PAD is integrity-checked; a tampered wrapped blob causes
    the HSM to raise.
    """
    s = _hsm_session()
    kek = get_or_create_kek()

    dek_obj = kek.unwrap_key(
        ObjectClass.SECRET_KEY,
        KeyType.AES,
        wrapped,
        mechanism=Mechanism.AES_KEY_WRAP_PAD,
        template={
            Attribute.TOKEN: False,
            Attribute.EXTRACTABLE: True,
            Attribute.SENSITIVE: False,
        },
    )
    try:
        return dek_obj[Attribute.VALUE]
    finally:
        dek_obj.destroy()
