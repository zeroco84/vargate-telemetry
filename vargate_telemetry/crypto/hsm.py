# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""HSM-backed Key Encryption Key (KEK) lifecycle and DEK wrapping (T1.6).

The Telemetry KEK is a single AES-256 secret-key object inside SoftHSM2,
identified by `HSM_KEK_LABEL`. It is generated once (idempotently) by
`scripts/init_telemetry_kek.py` and never leaves the HSM. Every per-tenant
DEK is encrypted by the KEK with AES-CBC-PAD; the resulting blob is what
lands in Postgres in `tenant_deks` from T1.7 onward.

Mechanism choice — short version: AES-CBC-PAD is what python-pkcs11
supports reliably for symmetric key wrapping in 0.7.x. AES-GCM has
broken parameter-packing in the Cython binding (raises a bare
TypeError); AES-KEY-WRAP-PAD via wrap_key/unwrap_key isn't reachable
from the SecretKey instance python-pkcs11 returns from get_objects.
CBC-PAD is the well-trodden path that works against this library.

What CBC-PAD costs us: it is **not authenticated**. A tampered wrapped
blob decrypts to garbage (which the per-tenant DEK consumer will then
fail on at the next AES-GCM operation) rather than raising at unwrap
time. Defense-in-depth is provided by:

  - RLS prevents cross-tenant writes to `tenant_deks`.
  - `seal_secret` / `provision_tenant_dek` (T1.7+) are the only call
    sites that write to `tenant_deks`; user input never reaches it.
  - The threat model in scope for T1.6 is "attacker steals a Postgres
    backup," not "attacker has authenticated row-write access." Under
    the former, CBC-PAD ciphertext is opaque without the HSM-bound KEK.

T1.7 may revisit and layer an HMAC over `(tenant_id, wrapped_dek)` if
deeper hardening is required.

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

    The KEK is an AES-256 secret-key object with ENCRYPT/DECRYPT capability;
    that's what `kek.encrypt(...)` / `kek.decrypt(...)` (with AES-CBC-PAD)
    require. Capabilities cannot be retrofitted onto an existing key — if
    you have a KEK from an earlier T1.6 attempt with different capabilities,
    the HSM volume must be re-initialized.
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
    """Encrypt a 32-byte DEK with the KEK using AES-CBC-PAD.

    Returns 16-byte IV followed by ciphertext (32 + 16 PKCS#7 padding =
    48 bytes of ciphertext, so 64 bytes total for a 32-byte DEK).
    """
    if len(dek_bytes) != 32:
        raise ValueError(f"DEK must be 32 bytes (got {len(dek_bytes)})")

    kek = get_or_create_kek()
    iv = os.urandom(16)
    ciphertext = kek.encrypt(
        dek_bytes,
        mechanism=Mechanism.AES_CBC_PAD,
        mechanism_param=iv,
    )
    return iv + ciphertext


def unwrap_dek(wrapped: bytes) -> bytes:
    """Inverse of `wrap_dek`. Returns the original 32-byte DEK.

    NOTE: AES-CBC-PAD does not authenticate. A tampered blob decrypts
    to garbage rather than raising; the consumer (tenant DEK user) will
    fail on the next AES-GCM operation against malformed key bytes.
    """
    if len(wrapped) < 16 + 16:
        raise ValueError(f"wrapped blob too short ({len(wrapped)} bytes)")

    kek = get_or_create_kek()
    iv = wrapped[:16]
    ciphertext = wrapped[16:]
    return kek.decrypt(
        ciphertext,
        mechanism=Mechanism.AES_CBC_PAD,
        mechanism_param=iv,
    )
