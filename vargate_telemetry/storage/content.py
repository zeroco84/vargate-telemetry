# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-tenant content storage (T5.1) — encrypt + ship to MinIO.

This is the high-level wrapper that T5.x ingest paths (Compliance API
prompt/response capture, Code Analytics artifacts) call when they
have a plaintext content blob to persist. It composes T1.7's tenant
DEK + AES-GCM with T5.0's MinIO object store:

    plaintext ──► [AES-256-GCM under tenant DEK with AAD] ──► IV||CT
                       │
                       └── content_hash = SHA-256(plaintext)
                       └── content_size_bytes = len(plaintext)

    IV||CT ──► [MinIO put_object at tenants/{tenant_id}/{content_ref}]

The MinIO blob layout is:

    bytes [0..11]   12-byte AES-GCM IV
    bytes [12..]    AES-GCM ciphertext, tag-suffixed by the cryptography
                    library convention

So ``retrieve_content`` slices the IV off the front, runs the AES-GCM
decrypt with the same AAD, and returns plaintext. Tag mismatches
surface as a typed ``IntegrityError`` (subclass of
``object_store.StorageError`` so callers can use a single ``except
StorageError`` for broad sweep, or ``except IntegrityError`` for the
specific tamper-detection path).

Why content_hash is SHA-256 of plaintext, not ciphertext
========================================================

The hash that lands in ``telemetry_records.content_hash`` (and thus
in the chain canonical bytes) is computed over the *plaintext*. This
is load-bearing: if a tenant rotates their DEK, the *ciphertext*
changes on the next re-encrypt — but the *plaintext* doesn't. Hashing
ciphertext would force a chain rewrite on every DEK rotation,
breaking the immutability invariant the chain exists to provide.
Hashing plaintext means the chain entry stays verifiable across
arbitrary key rotations.

Trade-off: the hash binds the *contents*, not the storage layer. If
a content blob is moved between buckets, replicated to a backup, or
re-encrypted under a new DEK, the chain entry continues to verify
against the same hash. That is the intended behaviour — auditability
is a property of the data, not the storage path.

What MinIO sees
===============

MinIO never sees plaintext. Every blob is AES-GCM-encrypted at this
layer before the ``put_content`` call. Per the working-memory rule
saved at T5.0:

  - **Do NOT enable MinIO server-side encryption.** Two layers
    obscure the audit story without adding strength.
  - **Crypto-shred is row-level.** Destroying the wrapped DEK in
    ``tenant_deks`` makes every blob for that tenant
    cryptographically inaccessible regardless of whether MinIO
    still has the bytes. ``delete_content`` is a secondary
    clean-up — it removes the now-unreadable bytes from storage.

Tenant-id validation
====================

``store_content``, ``retrieve_content``, and ``delete_content`` all
validate ``tenant_id`` at the top of the function per the
``tenant_id_input_validation_at_boundaries`` working-memory rule:
empty / slash-containing / leading-dot / null-byte / >64-char
``tenant_id`` is rejected with ``ValueError`` before any path
construction or DEK lookup. The underlying ``object_store`` also
validates (T5.0), so a malformed ``tenant_id`` is rejected at two
boundaries; defence in depth.
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag

from vargate_telemetry.crypto.dek import (
    decrypt_with_dek,
    encrypt_with_dek,
)
from vargate_telemetry.crypto.seal import get_tenant_dek
from vargate_telemetry.storage import object_store


_log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Public exception type
# ───────────────────────────────────────────────────────────────────────────


class IntegrityError(object_store.StorageError):
    """The retrieved ciphertext failed AES-GCM authentication.

    The ciphertext or its IV was modified after the put — either the
    MinIO blob was tampered with, the wrong DEK was used, or the
    AAD doesn't match (e.g., the content_ref was changed since the
    write). All three are tamper signals; the dashboard's content
    view should raise this all the way to a "verification failed"
    state, not retry.

    Subclass of ``StorageError`` so a broad
    ``except object_store.StorageError`` catches it alongside
    transport-level failures; callers that want to distinguish
    tamper-from-transient-error do ``except IntegrityError``.
    """


# ───────────────────────────────────────────────────────────────────────────
# tenant_id boundary validation (Memory: tenant_id_input_validation_at_boundaries)
# ───────────────────────────────────────────────────────────────────────────


_MAX_TENANT_ID_LEN = 64


def _validate_tenant_id(tenant_id: str) -> None:
    """Reject malformed tenant_id before any path construction or DEK lookup.

    Rejection rules per the working-memory rule:
      - empty string
      - contains ``/`` or ``\\``
      - starts with ``.`` (traversal vectors)
      - contains a null byte
      - longer than 64 chars

    The standard format from T4.5 is ``tnt_{region}_{16hex}`` — 24
    chars total. The 64-char cap leaves headroom for region prefix
    changes without re-tuning. Rejection, never sanitization.
    """
    if not isinstance(tenant_id, str) or not tenant_id:
        raise ValueError("tenant_id required (non-empty string)")
    if len(tenant_id) > _MAX_TENANT_ID_LEN:
        raise ValueError(
            f"tenant_id too long ({len(tenant_id)} > {_MAX_TENANT_ID_LEN})"
        )
    if "/" in tenant_id or "\\" in tenant_id:
        raise ValueError(
            f"tenant_id may not contain '/' or '\\\\': got {tenant_id!r}"
        )
    if tenant_id.startswith("."):
        raise ValueError(
            f"tenant_id may not start with '.': got {tenant_id!r}"
        )
    if "\x00" in tenant_id:
        raise ValueError("tenant_id may not contain null bytes")


# ───────────────────────────────────────────────────────────────────────────
# content_ref construction
# ───────────────────────────────────────────────────────────────────────────


def _new_content_ref(*, now: datetime | None = None) -> str:
    """Build a fresh per-tenant content_ref.

    Format: ``YYYY/MM/DD/{uuid4}.enc`` — the date prefix gives
    natural sort order at day granularity (useful for ops:
    ``mc ls .../tenants/{tenant_id}/2026/05/`` enumerates a month's
    content). Random uuid4 inside the day prefix keeps writes
    collision-free without the ordering guarantees uuid7 would
    add — Python 3.12 doesn't ship uuid7, and pulling in a
    dependency for day-level sort isn't worth the cost today.

    ``now`` injectable so tests can pin the date.

    Returns the *per-tenant* portion of the key. The object_store
    wrapper prepends ``tenants/{tenant_id}/`` to produce the full
    S3 key — we do not include tenant_id in content_ref to avoid
    a redundant double-prefix in the final S3 path.
    """
    moment = now or datetime.now(timezone.utc)
    return (
        f"{moment.year:04d}/{moment.month:02d}/{moment.day:02d}/"
        f"{uuid.uuid4().hex}.enc"
    )


def _aad_for_content(tenant_id: str, content_ref: str) -> bytes:
    """AAD binding the ciphertext to (tenant_id, content_ref).

    Mirrors ``seal._aad_for_secret``. If a ciphertext blob is copied
    into a different tenant's namespace or to a different content_ref
    within the same tenant, decryption fails — even if the attacker
    has the right DEK. Belt-and-braces over the path-derived
    addressing.
    """
    return (
        f"vargate.telemetry/content/{tenant_id}/{content_ref}"
    ).encode("utf-8")


# ───────────────────────────────────────────────────────────────────────────
# Public verbs
# ───────────────────────────────────────────────────────────────────────────


_IV_LEN = 12  # AES-GCM 96-bit IV per NIST SP 800-38D


def store_content(tenant_id: str, plaintext: bytes) -> tuple[str, bytes]:
    """Encrypt ``plaintext`` under the tenant DEK and write to MinIO.

    Returns ``(content_ref, content_hash)`` for the caller to persist
    in ``telemetry_records.content_ref`` / ``.content_hash``.
    ``content_hash`` is SHA-256 of the *plaintext* (see module
    docstring on why this is load-bearing for DEK rotation).

    Raises ``ValueError`` for a malformed tenant_id (per the
    boundary-validation rule). Raises
    ``vargate_telemetry.storage.object_store.StorageError`` if the
    MinIO put fails. The tenant DEK lookup raises ``LookupError`` if
    no DEK has been provisioned — caller must
    ``provision_tenant_dek(tenant_id)`` first (or arrive via the
    T4.5 select-region path, which provisions on tenant creation).
    """
    _validate_tenant_id(tenant_id)
    if not isinstance(plaintext, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"plaintext must be bytes-like, got {type(plaintext).__name__}"
        )

    plaintext_bytes = bytes(plaintext)
    content_hash = hashlib.sha256(plaintext_bytes).digest()
    content_ref = _new_content_ref()

    # Pull the DEK once, AES-GCM-encrypt, then drop the in-memory
    # plaintext key on return (the local variable goes out of scope;
    # Python's GC will reclaim the buffer). The DEK is unwrapped
    # from the HSM per call — no caching. T1.8 benchmarked this as
    # acceptable for the per-record latency surface; if T5.x's
    # batch ingest path hits HSM throughput limits, an LRU cache
    # lands then.
    dek = get_tenant_dek(tenant_id)
    aad = _aad_for_content(tenant_id, content_ref)
    iv, ciphertext = encrypt_with_dek(dek, plaintext_bytes, aad=aad)
    if len(iv) != _IV_LEN:
        # Defensive: encrypt_with_dek always returns a 12-byte IV per
        # NIST 800-38D, but if that ever changes our slice in
        # retrieve_content would silently corrupt — fail loud here.
        raise RuntimeError(
            f"AES-GCM IV length {_IV_LEN} expected, got {len(iv)}"
        )

    blob = iv + ciphertext
    object_store.put_content(tenant_id, content_ref, blob)
    return content_ref, content_hash


def retrieve_content(tenant_id: str, content_ref: str) -> bytes:
    """Fetch + decrypt the blob at ``content_ref`` for ``tenant_id``.

    Raises:
      - ``ValueError`` for malformed tenant_id / empty content_ref.
      - ``object_store.NotFound`` if the MinIO object is missing.
      - ``IntegrityError`` if the AES-GCM auth tag fails — tamper,
        wrong DEK, or AAD mismatch (e.g., the content_ref was
        renamed since the put).
      - ``object_store.StorageError`` for transport errors.

    The function is read-only — no DB writes, no chain state change.
    Safe to call from any tenant-scoped context.
    """
    _validate_tenant_id(tenant_id)
    if not content_ref:
        raise ValueError("content_ref required")

    blob = object_store.get_content(tenant_id, content_ref)
    if len(blob) < _IV_LEN:
        # An object shorter than the IV is malformed by construction
        # — it can't be one of our writes. Surface as an integrity
        # error rather than a cryptic InvalidTag on decrypt.
        raise IntegrityError(
            f"content blob too short ({len(blob)} bytes) at "
            f"tenant={tenant_id} content_ref={content_ref}; "
            f"expected at least {_IV_LEN} bytes of IV"
        )

    iv = blob[:_IV_LEN]
    ciphertext = blob[_IV_LEN:]
    dek = get_tenant_dek(tenant_id)
    aad = _aad_for_content(tenant_id, content_ref)

    try:
        return decrypt_with_dek(dek, iv, ciphertext, aad=aad)
    except InvalidTag as exc:
        raise IntegrityError(
            f"AES-GCM auth tag failed at tenant={tenant_id} "
            f"content_ref={content_ref}; possible tamper or "
            f"AAD mismatch"
        ) from exc


def delete_content(tenant_id: str, content_ref: str) -> None:
    """Delete the MinIO blob at ``content_ref``.

    Idempotent — deleting a missing object is a no-op (S3 semantics).
    Crypto-shred is a separate operation on the DEK itself
    (destroy the wrapped DEK in ``tenant_deks`` → all blobs for that
    tenant become cryptographically inaccessible regardless of MinIO
    state). This function just removes the now-unreadable bytes from
    the bucket.

    The chain entry in ``telemetry_records`` is unaffected:
    ``content_hash`` stays bound to the chain and the row is the
    permanent audit record. After ``delete_content``,
    ``retrieve_content`` raises ``NotFound`` but the chain still
    verifies (the chain doesn't read the blob — it reads the row's
    ``content_hash``, which is unchanged).
    """
    _validate_tenant_id(tenant_id)
    if not content_ref:
        raise ValueError("content_ref required")

    object_store.delete_content(tenant_id, content_ref)


__all__ = [
    "IntegrityError",
    "delete_content",
    "retrieve_content",
    "store_content",
]
