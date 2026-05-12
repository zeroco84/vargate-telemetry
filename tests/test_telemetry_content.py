# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the T5.1 content storage layer.

Composes T1.7 (tenant DEK seal) + T5.0 (MinIO object store) into the
end-to-end ingest path that T5.x's Compliance API ingestor will use.

Each test runs against the LIVE MinIO + the real HSM in compose. The
tenant_id is unique per test (uuid suffix) so DEK provisioning,
chain state, and MinIO writes don't trample concurrent runs.

What's NOT tested here:
  - the underlying object_store retry / error model (covered in T5.0
    test_object_store.py).
  - HSM unwrap correctness (covered in T1.7's test_telemetry_seal.py).
  This file exercises the *composition* and the new T5.1 invariants
  (plaintext-hash stability across DEK rotation, AES-GCM tag
  detection, chain integration).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def smoke_tenant_id() -> str:
    """Per-test tenant_id matching the T4.5 ``tnt_{region}_{16hex}``
    shape, so the validation guards see realistic input.
    """
    return f"tnt_t5_{uuid.uuid4().hex[:16]}"


@pytest.fixture
def provisioned_tenant(smoke_tenant_id: str) -> Iterator[str]:
    """A tenant with a provisioned DEK, cleaned up on teardown.

    Yields the tenant_id. Teardown deletes the DEK row + any
    telemetry_records the test wrote. MinIO blobs are cleaned by the
    separate ``cleanup_objects`` fixture (driven by the test
    appending to it).
    """
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine

    provision_tenant_dek(smoke_tenant_id)
    yield smoke_tenant_id

    # Cleanup: DEK row + telemetry_records for this tenant. Bypass RLS
    # via the bootstrap superuser; the per-test cleanup is fine to do
    # under that role since it only touches rows tied to a uuid-stamped
    # tenant_id that no other test can collide with.
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "DELETE FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": smoke_tenant_id},
        )
        conn.execute(
            sql_text("DELETE FROM tenant_deks WHERE tenant_id = :t"),
            {"t": smoke_tenant_id},
        )


@pytest.fixture
def cleanup_objects(provisioned_tenant: str) -> Iterator[list[str]]:
    """Track content_refs and best-effort delete each on teardown.

    Tests append content_refs to the yielded list as they write them.
    Teardown calls ``delete_content`` on each (idempotent). MinIO
    blobs that never had a content_ref appended just stay until the
    bucket-level cleanup runs (no concern at test scope).
    """
    from vargate_telemetry.storage import content as content_mod

    refs: list[str] = []
    yield refs
    for ref in refs:
        try:
            content_mod.delete_content(provisioned_tenant, ref)
        except Exception:  # pragma: no cover — best-effort
            pass


# ───────────────────────────────────────────────────────────────────────────
# Baseline (spec-required) tests
# ───────────────────────────────────────────────────────────────────────────


def test_store_content_writes_to_minio_and_returns_hash(
    provisioned_tenant: str, cleanup_objects: list[str]
) -> None:
    """store_content writes encrypted bytes to MinIO and returns
    (content_ref, content_hash) where content_hash matches
    sha256(plaintext) and the ciphertext on disk is NOT the plaintext."""
    from vargate_telemetry.storage import content as content_mod
    from vargate_telemetry.storage import object_store

    plaintext = (
        b"Audit-bearing prompt content: How do I drop the production DB?"
    )
    expected_hash = hashlib.sha256(plaintext).digest()

    content_ref, content_hash, content_size_bytes = content_mod.store_content(
        provisioned_tenant, plaintext
    )
    cleanup_objects.append(content_ref)

    # T5.3: size is the uncompressed plaintext length.
    assert content_size_bytes == len(plaintext)

    # Returned hash matches plaintext sha256 — the load-bearing
    # invariant (chain entry survives DEK rotation; see
    # test_content_hash_stable_across_encryption_with_new_dek).
    assert content_hash == expected_hash

    # The MinIO blob is opaque: definitely NOT the plaintext.
    raw_blob = object_store.get_content(provisioned_tenant, content_ref)
    assert raw_blob != plaintext
    # And longer than plaintext by at least IV(12) + GCM-tag(16) = 28.
    assert len(raw_blob) >= len(plaintext) + 28


def test_retrieve_content_decrypts_correctly(
    provisioned_tenant: str, cleanup_objects: list[str]
) -> None:
    """store + retrieve round-trips back to the original plaintext."""
    from vargate_telemetry.storage import content as content_mod

    plaintext = b"\x00\x01\x02 prompt with NULs and \xfe\xff bytes" * 8
    content_ref, _, _ = content_mod.store_content(
        provisioned_tenant, plaintext
    )
    cleanup_objects.append(content_ref)

    retrieved = content_mod.retrieve_content(
        provisioned_tenant, content_ref
    )
    assert retrieved == plaintext


def test_content_hash_stable_across_encryption_with_new_dek(
    smoke_tenant_id: str,
) -> None:
    """The chain's load-bearing invariant: rotating the tenant DEK
    must not change the content_hash for a given plaintext.

    If the hash changed across rotation, every chain entry referencing
    a content blob would need a chain rewrite on DEK rotation — which
    defeats the whole point of an append-only chain. content_hash is
    SHA-256(plaintext), so it's stable by construction; this test
    pins the invariant against future refactors that might be tempted
    to hash the ciphertext.
    """
    from vargate_telemetry.crypto.seal import provision_tenant_dek
    from vargate_telemetry.db import engine
    from vargate_telemetry.storage import content as content_mod
    from vargate_telemetry.storage import object_store

    provision_tenant_dek(smoke_tenant_id)
    refs_to_clean: list[str] = []

    try:
        plaintext = b"identical plaintext across rotations"

        # Round 1: store under DEK v1
        ref1, hash1, _ = content_mod.store_content(
            smoke_tenant_id, plaintext
        )
        refs_to_clean.append(ref1)
        ciphertext_v1 = object_store.get_content(smoke_tenant_id, ref1)

        # Rotate: delete the DEK row and re-provision. The new
        # provision picks a fresh random DEK, so any ciphertext
        # written under v1 is no longer readable. (We never call
        # retrieve_content(ref1) after this — the test isn't about
        # reading old ciphertext under a new key, it's about hash
        # stability across the rotation.)
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "DELETE FROM tenant_deks WHERE tenant_id = :t"
                ),
                {"t": smoke_tenant_id},
            )
        provision_tenant_dek(smoke_tenant_id)

        # Round 2: store SAME plaintext under DEK v2
        ref2, hash2, _ = content_mod.store_content(
            smoke_tenant_id, plaintext
        )
        refs_to_clean.append(ref2)
        ciphertext_v2 = object_store.get_content(smoke_tenant_id, ref2)

        # The ciphertext differs (different DEK + different content_ref
        # → different AAD → different output); the hash is identical.
        assert ciphertext_v1 != ciphertext_v2
        assert hash1 == hash2 == hashlib.sha256(plaintext).digest()

        # And retrieve under DEK v2 returns the plaintext.
        assert (
            content_mod.retrieve_content(smoke_tenant_id, ref2)
            == plaintext
        )
    finally:
        for r in refs_to_clean:
            try:
                content_mod.delete_content(smoke_tenant_id, r)
            except Exception:  # pragma: no cover
                pass
        with engine.begin() as conn:
            conn.execute(
                sql_text(
                    "DELETE FROM tenant_deks WHERE tenant_id = :t"
                ),
                {"t": smoke_tenant_id},
            )


def test_chain_integrity_holds_when_content_added(
    provisioned_tenant: str, cleanup_objects: list[str]
) -> None:
    """The chain verifies clean when we append a record carrying a
    content_ref + content_hash from store_content. Mixed chains
    (some records with content, some without) verify too.

    Tampering with the row's content_hash in the DB (the integrity
    surface the chain owns) breaks verification at that record. The
    MinIO blob is NOT part of the chain hash input — tampering with
    the blob is detected by AES-GCM at retrieve time, not by chain
    verify (see test_integrity_error_on_tampered_ciphertext).
    """
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )
    from vargate_telemetry.db import engine
    from vargate_telemetry.storage import content as content_mod

    now = datetime.now(timezone.utc)

    # Record 1: with content.
    plaintext = b"Compliance API captured prompt -- sensitive."
    ref, content_hash, _ = content_mod.store_content(
        provisioned_tenant, plaintext
    )
    cleanup_objects.append(ref)

    r1 = append_telemetry_record(
        tenant_id=provisioned_tenant,
        record_type="prompt",
        source_api="compliance",
        external_id="ext-content-1",
        occurred_at=now,
        content_hash=content_hash,
        content_ref=ref,
        record_metadata={"model": "claude-opus-4-7"},
    )

    # Record 2: no content (Admin API usage bucket shape — content_hash
    # is metadata-derived).
    r2 = append_telemetry_record(
        tenant_id=provisioned_tenant,
        record_type="usage",
        source_api="admin",
        external_id="ext-content-2",
        occurred_at=now,
        content_hash=b"\xaa" * 32,
        record_metadata={"tokens": 200},
    )

    # Mixed chain verifies clean.
    pre = verify_telemetry_chain(provisioned_tenant)
    assert pre.valid is True
    assert pre.record_count == 2

    # Tamper with the content_hash on record 1 directly in the DB.
    # The MinIO blob is unchanged — the chain doesn't read it; the
    # tamper surface here is the row's content_hash field.
    tampered_hash = b"\xff" * 32
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "UPDATE telemetry_records "
                "SET content_hash = :h "
                "WHERE id = :rid"
            ),
            {"h": tampered_hash, "rid": str(r1.id)},
        )

    post = verify_telemetry_chain(provisioned_tenant)
    assert post.valid is False
    assert post.failed_at_index == 0
    assert "record_hash mismatch" in (post.failure_reason or "")

    # The second record's hash math would have been valid against
    # the un-tampered first record; with the tamper on row 1, the
    # chain breaks at index 0 and stops. r2's row data is unchanged.
    _ = r2  # silence the unused warning; r2 is here for the mixed
    # chain shape, not for an additional assertion below.


def test_integrity_error_on_tampered_ciphertext(
    provisioned_tenant: str, cleanup_objects: list[str]
) -> None:
    """Corrupting one byte of the MinIO ciphertext makes AES-GCM
    auth-tag verification fail, surfacing as IntegrityError (NOT a
    generic StorageError or a cryptic InvalidTag).

    This is the tamper-detection story for the storage layer itself:
    the chain protects the (content_ref, content_hash) binding, and
    AES-GCM protects the bytes at content_ref. Together they detect
    any attack short of crypto-shred (which would require the
    wrapped DEK in tenant_deks, gated by the HSM).
    """
    from vargate_telemetry.storage import content as content_mod
    from vargate_telemetry.storage import object_store

    plaintext = b"original-content-T5.1"
    ref, _, _ = content_mod.store_content(provisioned_tenant, plaintext)
    cleanup_objects.append(ref)

    # Flip a byte deep in the ciphertext (well past the IV). The
    # AES-GCM tag is at the tail, so flipping a mid-body byte
    # invalidates the tag without touching it directly — proves the
    # tag covers the whole ciphertext.
    blob = object_store.get_content(provisioned_tenant, ref)
    # Pick a byte at position 20 (past the 12-byte IV) so we're in
    # the encrypted-payload region.
    assert len(blob) > 22
    tampered = bytes(blob[:20]) + bytes(
        [blob[20] ^ 0x01]
    ) + bytes(blob[21:])
    object_store.put_content(provisioned_tenant, ref, tampered)

    with pytest.raises(content_mod.IntegrityError):
        content_mod.retrieve_content(provisioned_tenant, ref)


# ───────────────────────────────────────────────────────────────────────────
# Bonus tests (defensive contracts that callers will rely on)
# ───────────────────────────────────────────────────────────────────────────


def test_delete_content_removes_object_but_preserves_hash_in_chain(
    provisioned_tenant: str,
) -> None:
    """Crypto-shred semantics: deleting the MinIO blob makes the
    content unreadable (NotFound at retrieve), but the chain entry
    in telemetry_records still verifies cleanly — the row's
    content_hash is the audit anchor, not the blob.

    The point: an attacker who deletes the MinIO blob removes the
    *content* but cannot remove the *fact that the content existed*
    from the audit chain. Discoverability of past activity is
    preserved even when the activity's payload is no longer
    available.
    """
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )
    from vargate_telemetry.storage import content as content_mod
    from vargate_telemetry.storage import object_store

    plaintext = b"to-be-shredded"
    ref, content_hash, _ = content_mod.store_content(
        provisioned_tenant, plaintext
    )
    append_telemetry_record(
        tenant_id=provisioned_tenant,
        record_type="prompt",
        source_api="compliance",
        external_id="shred-test",
        occurred_at=datetime.now(timezone.utc),
        content_hash=content_hash,
        content_ref=ref,
        record_metadata={"shredded": True},
    )

    # Delete the blob.
    content_mod.delete_content(provisioned_tenant, ref)

    # Blob gone.
    assert object_store.exists(provisioned_tenant, ref) is False
    with pytest.raises(object_store.NotFound):
        content_mod.retrieve_content(provisioned_tenant, ref)

    # But the chain entry persists and verifies — the row's
    # content_hash is unaltered.
    result = verify_telemetry_chain(provisioned_tenant)
    assert result.valid is True
    assert result.record_count == 1


def test_store_with_empty_plaintext_creates_zero_byte_content(
    provisioned_tenant: str, cleanup_objects: list[str]
) -> None:
    """Edge case: empty plaintext is a valid input — the content is
    just zero bytes. The hash is the SHA-256 of the empty string
    (well-known constant). store + retrieve round-trip preserves
    this.

    We could choose to reject empty input, but the dashboard's
    audit-export flow includes records whose content was an empty
    response — preserving "the response was empty" as a first-class
    auditable fact is more honest than refusing to record it.
    """
    from vargate_telemetry.storage import content as content_mod

    # Empty bytes
    plaintext = b""
    expected_hash = hashlib.sha256(plaintext).digest()
    # The well-known empty-SHA-256 constant, sanity-checked here:
    assert (
        expected_hash.hex()
        == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
    )

    ref, content_hash, content_size_bytes = content_mod.store_content(
        provisioned_tenant, plaintext
    )
    cleanup_objects.append(ref)
    assert content_hash == expected_hash
    assert content_size_bytes == 0  # zero-byte plaintext → zero-byte size

    retrieved = content_mod.retrieve_content(provisioned_tenant, ref)
    assert retrieved == b""


@pytest.mark.parametrize(
    "bad_tenant_id, reason_fragment",
    [
        ("", "required"),
        ("tnt_us_/../other", "may not contain"),
        ("tnt\\us_evasion", "may not contain"),
        (".hidden-tenant", "may not start with"),
        ("tnt_us_\x00null", "null bytes"),
        ("a" * 65, "too long"),
    ],
)
def test_store_rejects_invalid_tenant_id(
    bad_tenant_id: str, reason_fragment: str
) -> None:
    """Per the ``tenant_id_input_validation_at_boundaries`` working-
    memory rule: reject empty / slash / leading-dot / null-byte /
    over-long tenant_ids at the top of store_content, before any
    path construction or DEK lookup.

    Rejection, not sanitization — silently sanitizing would turn an
    attacker's probing into a legitimate write to an adjacent
    namespace.
    """
    from vargate_telemetry.storage import content as content_mod

    with pytest.raises(ValueError, match=reason_fragment):
        content_mod.store_content(bad_tenant_id, b"payload")
