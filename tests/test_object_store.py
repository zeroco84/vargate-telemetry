# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Round-trip tests for the T5.0 object_store wrapper.

These tests run against the LIVE MinIO inside the dev compose — they
need the `tenant-content` bucket to exist (the `minio-bootstrap`
compose service creates it). pytest fixture state is scoped to a
unique per-test tenant_id so concurrent runs don't trample each other
and cleanup is bounded to one tenant's namespace.

What's NOT tested here:
  - the boto3 client wiring itself (`_build_client`) — env-shaped
    failures are tested implicitly by the live round-trip.
  - retry behaviour. Wrapping the live MinIO in a 503-fault-injection
    layer would expand the scope past T5.0; tenacity's policy is
    well-known and the wrapper's retry decorator is exercised in T5.1
    when the encrypt-and-store path lands.
"""

from __future__ import annotations

import uuid
from typing import Iterator

import pytest

from vargate_telemetry.storage import object_store


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def smoke_tenant_id() -> str:
    """A unique tenant_id per test so writes never collide and cleanup
    only nukes one test's namespace."""
    return f"test-tnt-{uuid.uuid4().hex[:16]}"


@pytest.fixture
def cleanup_after(smoke_tenant_id: str) -> Iterator[list[str]]:
    """Track keys created by the test and delete each on teardown.

    Yields a list the test appends to. Teardown calls
    `delete_content` on each — `delete_content` is idempotent, so
    teardown is safe even if the test never wrote the key.
    """
    keys: list[str] = []
    yield keys
    for k in keys:
        try:
            object_store.delete_content(smoke_tenant_id, k)
        except object_store.StorageError:  # pragma: no cover
            # Best-effort cleanup. If MinIO is unreachable during
            # teardown the test was already failing for a different
            # reason; don't shadow it.
            pass


# ───────────────────────────────────────────────────────────────────────────
# Baseline (spec-required) tests
# ───────────────────────────────────────────────────────────────────────────


def test_minio_reachable_via_object_store_client(
    smoke_tenant_id: str,
) -> None:
    """Smoke check the configured endpoint responds.

    Uses `exists()` against a known-missing key — that's a HEAD probe
    which round-trips the auth handshake without needing any data
    written first. A False return means MinIO is reachable AND the
    bucket exists AND the credentials work, which is the property we
    actually want to assert for "reachable."
    """
    assert object_store.exists(smoke_tenant_id, "no-such-key") is False


def test_put_get_round_trip(
    smoke_tenant_id: str, cleanup_after: list[str]
) -> None:
    """Bytes written via put_content read back byte-for-byte via
    get_content. The core invariant of the wrapper."""
    key = "roundtrip-test"
    cleanup_after.append(key)
    payload = b"\x00\x01\x02hello-T5.0\xfe\xff" * 16  # binary safety check

    object_store.put_content(smoke_tenant_id, key, payload)
    fetched = object_store.get_content(smoke_tenant_id, key)

    assert fetched == payload


def test_delete_removes_object(
    smoke_tenant_id: str, cleanup_after: list[str]
) -> None:
    """After delete_content, exists() returns False and get_content
    raises NotFound."""
    key = "delete-test"
    cleanup_after.append(key)
    object_store.put_content(smoke_tenant_id, key, b"to-be-deleted")
    assert object_store.exists(smoke_tenant_id, key) is True

    object_store.delete_content(smoke_tenant_id, key)

    assert object_store.exists(smoke_tenant_id, key) is False
    with pytest.raises(object_store.NotFound):
        object_store.get_content(smoke_tenant_id, key)


# ───────────────────────────────────────────────────────────────────────────
# Bonus tests (defensive contracts that callers will rely on)
# ───────────────────────────────────────────────────────────────────────────


def test_get_nonexistent_object_raises_not_found(
    smoke_tenant_id: str,
) -> None:
    """A clean NotFound rather than a 4xx ClientError surfacing up.
    The two-layer error model (NotFound subclass of StorageError) is
    the wrapper's headline contract; pin it explicitly so a future
    refactor that returns None or swallows 404s breaks loudly."""
    with pytest.raises(object_store.NotFound):
        object_store.get_content(smoke_tenant_id, "missing-key")


def test_put_overwrites_existing_object_silently(
    smoke_tenant_id: str, cleanup_after: list[str]
) -> None:
    """S3 semantics: a put against an existing key replaces the bytes
    silently. Document this here so a future refactor adding
    fail-on-exists doesn't quietly break callers that depend on
    overwrite idempotency (T5.1's re-ingest path will).
    """
    key = "overwrite-test"
    cleanup_after.append(key)

    object_store.put_content(smoke_tenant_id, key, b"first version")
    object_store.put_content(smoke_tenant_id, key, b"second version")
    assert object_store.get_content(smoke_tenant_id, key) == b"second version"


# ───────────────────────────────────────────────────────────────────────────
# Defensive key-construction tests (no MinIO round-trip required)
# ───────────────────────────────────────────────────────────────────────────


def test_put_rejects_empty_tenant_id() -> None:
    """Empty tenant_id is a programmer error — refuse loudly instead
    of writing to `tenants//key` (which would create an ambiguous
    cross-tenant blob)."""
    with pytest.raises(ValueError, match="tenant_id required"):
        object_store.put_content("", "k", b"x")


def test_put_rejects_tenant_id_with_slash() -> None:
    """Defensive: a tenant_id with a slash would re-shape the prefix
    and potentially allow cross-tenant reads via traversal. Per T4.5,
    tenant_id format is `tnt_{region}_{16hex}` — no slashes ever — but
    pin the guard against future API drift."""
    with pytest.raises(ValueError, match="may not contain"):
        object_store.put_content("tnt_us_/../other_tenant", "k", b"x")


# ───────────────────────────────────────────────────────────────────────────
# Credential resolution (TM6 T6.0) — scoped svc account preferred, root
# fallback. Pure env logic; no live MinIO.
# ───────────────────────────────────────────────────────────────────────────


def test_resolve_credentials_prefers_scoped_svcaccount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OGMA_MINIO_ACCESS_KEY", "scoped-ak")
    monkeypatch.setenv("OGMA_MINIO_SECRET_KEY", "scoped-sk")
    monkeypatch.setenv("MINIO_ROOT_USER", "root-ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "root-sk")
    assert object_store._resolve_credentials() == ("scoped-ak", "scoped-sk")


def test_resolve_credentials_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OGMA_MINIO_ACCESS_KEY", raising=False)
    monkeypatch.delenv("OGMA_MINIO_SECRET_KEY", raising=False)
    monkeypatch.setenv("MINIO_ROOT_USER", "root-ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "root-sk")
    assert object_store._resolve_credentials() == ("root-ak", "root-sk")


def test_resolve_credentials_empty_scoped_falls_back_to_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # compose passes ${OGMA_MINIO_ACCESS_KEY:-} → "" when unset in .env;
    # an empty string must fall through to root, not raise.
    monkeypatch.setenv("OGMA_MINIO_ACCESS_KEY", "")
    monkeypatch.setenv("OGMA_MINIO_SECRET_KEY", "")
    monkeypatch.setenv("MINIO_ROOT_USER", "root-ak")
    monkeypatch.setenv("MINIO_ROOT_PASSWORD", "root-sk")
    assert object_store._resolve_credentials() == ("root-ak", "root-sk")


def test_resolve_credentials_raises_when_none_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for var in (
        "OGMA_MINIO_ACCESS_KEY",
        "OGMA_MINIO_SECRET_KEY",
        "MINIO_ROOT_USER",
        "MINIO_ROOT_PASSWORD",
    ):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(object_store.StorageError):
        object_store._resolve_credentials()
