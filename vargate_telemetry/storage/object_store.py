# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Typed wrapper around the MinIO tenant-content bucket (T5.0).

Layout
======

One bucket — ``MINIO_TENANT_CONTENT_BUCKET`` (default ``tenant-content``)
— holds every tenant's content blobs. Keys are namespaced:

    tenants/{tenant_id}/{caller-supplied key}

The ``tenants/`` prefix keeps the bucket root unambiguous (no top-level
collision with bookkeeping objects if we ever add some) and makes
``mc ls ogma/tenant-content/tenants/`` a clean per-tenant enumeration
during ops work. The caller-supplied key part is up to the writer —
T5.1's content-blob ingestor uses
``{usage_record_id}-{prompt|response}.aesgcm``.

What MinIO sees
===============

**Ciphertext only.** T5.1 wraps every blob in the tenant DEK envelope
(AES-256-GCM with a per-tenant DEK that's itself wrapped at the HSM)
BEFORE calling ``put_content``. MinIO never holds plaintext. Two
consequences:

1. **Do NOT enable MinIO server-side encryption.** Two layers of
   crypto would only obscure the audit story without adding strength
   — and putting a SSE key in MinIO's config is the kind of "where
   does the key actually live" foot-gun we got into envelope
   encryption to avoid.
2. **Crypto-shred is row-level.** Destroying the wrapped DEK in
   ``tenant_deks`` makes every blob for that tenant
   cryptographically inaccessible regardless of whether MinIO still
   has the bytes. Bucket deletion is a secondary clean-up, not a
   prerequisite for the security claim.

Exceptions
==========

Callers see a tight set of typed errors so they don't have to import
``botocore.exceptions``:

  - ``NotFound`` — the key does not exist (HEAD or GET against a
    missing object).
  - ``StorageError`` — every other error path (network failure,
    bucket missing, auth rejected, request timeout, anything boto3
    raises that isn't a clean 404).

Retries
=======

Transient errors (5xx, connect/read timeouts) are retried up to 3
times with exponential backoff via ``tenacity``. boto3 has its own
internal retry layer too — kept on default — so the visible
``StorageError`` only fires after both layers give up.
"""

from __future__ import annotations

import functools
import logging
import os
import threading
from typing import Any, Optional

import boto3
from botocore.client import Config as BotoConfig
from botocore.exceptions import (
    BotoCoreError,
    ClientError,
    EndpointConnectionError,
)
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)


_log = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Public exception types
# ───────────────────────────────────────────────────────────────────────────


class StorageError(Exception):
    """Generic object-store failure (network, auth, server 5xx, etc.).

    Wraps the underlying boto3 / botocore exception in ``__cause__`` so
    diagnostic traces are preserved without leaking the AWS-shaped
    exception class up the stack.
    """


class NotFound(StorageError):
    """The requested key does not exist.

    A subclass of StorageError so a broad ``except StorageError`` block
    catches both; callers that want to distinguish do
    ``except NotFound``.
    """


# ───────────────────────────────────────────────────────────────────────────
# Client construction — lazy, thread-safe singleton
# ───────────────────────────────────────────────────────────────────────────


# boto3 clients are thread-safe per their docs — one per process is enough.
# Lazy build so import-time failures (env var missing in a script context)
# don't poison the module load.
_client: Optional[Any] = None
_client_lock = threading.Lock()


def _resolve_credentials() -> tuple[str, str]:
    """Resolve the MinIO (access_key, secret_key) for the boto3 client.

    TM6 T6.0: prefer the scoped service account
    (``OGMA_MINIO_ACCESS_KEY`` / ``OGMA_MINIO_SECRET_KEY`` — least
    privilege: Put/Get/Delete on ``tenant-content/*`` only). Fall back to
    root creds (``MINIO_ROOT_USER`` / ``MINIO_ROOT_PASSWORD``) when the
    scoped vars are unset (dev / pre-migration) so existing envs keep
    working. The scoped account shrinks the blast radius of a
    gateway/worker compromise from "all of MinIO" to "the content bucket".
    """
    access_key = os.environ.get("OGMA_MINIO_ACCESS_KEY") or os.environ.get(
        "MINIO_ROOT_USER"
    )
    secret_key = os.environ.get("OGMA_MINIO_SECRET_KEY") or os.environ.get(
        "MINIO_ROOT_PASSWORD"
    )
    if not access_key or not secret_key:
        raise StorageError(
            "No MinIO credentials set. Set OGMA_MINIO_ACCESS_KEY / "
            "OGMA_MINIO_SECRET_KEY (the scoped service account) — or, for "
            "dev / bootstrap, MINIO_ROOT_USER / MINIO_ROOT_PASSWORD."
        )
    return access_key, secret_key


def _build_client() -> Any:
    """Build the boto3 S3 client configured for MinIO.

    Reads from env on first call. Subsequent calls reuse the cached
    instance unless ``_reset_client_for_test`` has been called.

    Kept private — callers go through the verb functions below. The
    only reason this isn't fully private is that the test suite needs
    to reset it across tests that change env state.
    """
    endpoint = os.environ.get("MINIO_ENDPOINT")
    if not endpoint:
        raise StorageError(
            "MINIO_ENDPOINT is not set. The object-store wrapper "
            "requires it to construct the boto3 client."
        )
    access_key, secret_key = _resolve_credentials()

    # `addressing_style="path"` is required for MinIO — it doesn't do
    # virtual-hosted-style buckets out of the box. Same setting works
    # against AWS S3 in path mode (slightly less efficient than
    # virtual-hosted, but functionally identical).
    #
    # `signature_version="s3v4"` is the only signature MinIO supports.
    #
    # `connect_timeout=5, read_timeout=15` keeps a hung MinIO from
    # blocking a celery task indefinitely. The tenacity retry layer
    # absorbs transient timeouts; permanent ones surface as
    # StorageError.
    config = BotoConfig(
        signature_version="s3v4",
        s3={"addressing_style": "path"},
        connect_timeout=5,
        read_timeout=15,
        retries={"max_attempts": 1},  # tenacity owns retries; disable boto's
    )

    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        # MinIO ignores region but boto3 requires one syntactically.
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        config=config,
    )


def _client_singleton() -> Any:
    """Return the cached boto3 client, building it under a lock once."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = _build_client()
    return _client


def _reset_client_for_test() -> None:
    """Drop the cached client so the next call rebuilds it.

    Tests that mutate ``MINIO_ENDPOINT`` / credentials need this; the
    public API doesn't otherwise expose the cache.
    """
    global _client
    with _client_lock:
        _client = None


# ───────────────────────────────────────────────────────────────────────────
# Key construction
# ───────────────────────────────────────────────────────────────────────────


_TENANT_PREFIX = "tenants/"


def _full_key(tenant_id: str, key: str) -> str:
    """Build the S3 key for a (tenant_id, key) pair.

    Refuses empty tenant_id or key — both must be present for the
    namespace to make sense. We do NOT validate the *shape* of
    tenant_id (the wrapper doesn't know what tenant_id should look
    like — that's the caller's invariant); we just require it
    non-empty.
    """
    if not tenant_id:
        raise ValueError("tenant_id required")
    if not key:
        raise ValueError("key required")
    if "/" in tenant_id:
        # Defensive: a tenant_id with a slash would silently re-shape
        # the prefix and could allow cross-tenant reads via traversal.
        # Tenant_id format is `tnt_{region}_{16hex}` per T4.5 — no
        # slashes ever. This guards against future API drift.
        raise ValueError(
            f"tenant_id may not contain '/': got {tenant_id!r}"
        )
    return f"{_TENANT_PREFIX}{tenant_id}/{key}"


def _bucket() -> str:
    """Return the configured bucket name.

    Reads on every call (cheap, env can be overridden in tests) rather
    than caching, since the wrapper is small enough that one extra
    os.environ.get per call doesn't matter.
    """
    return os.environ.get("MINIO_TENANT_CONTENT_BUCKET", "tenant-content")


# ───────────────────────────────────────────────────────────────────────────
# tenacity-decorated low-level ops
# ───────────────────────────────────────────────────────────────────────────


# Retry on transport-level transient failures. ClientError covers
# everything boto3 wraps a non-2xx HTTP into; we filter inside the
# wrapper to NOT retry on 4xx (NoSuchKey, AccessDenied — those won't
# get better on a second try).
_TRANSIENT_EXCEPTIONS: tuple[type[BaseException], ...] = (
    EndpointConnectionError,
    BotoCoreError,
)


def _retry(fn):
    """Apply the tenacity policy used by every public verb."""
    return retry(
        retry=retry_if_exception_type(_TRANSIENT_EXCEPTIONS),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.5, min=0.5, max=4.0),
        reraise=True,
    )(fn)


def _is_not_found_error(exc: ClientError) -> bool:
    """True iff the ClientError is a 404 / NoSuchKey."""
    code = exc.response.get("Error", {}).get("Code", "")
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    # boto3 surfaces missing-key responses as `Error.Code = 'NoSuchKey'`
    # for GET and `'404'` for HEAD (yes, really, it's a string of digits
    # for HEAD). Cover both.
    return code in ("NoSuchKey", "404") or status == 404


# ───────────────────────────────────────────────────────────────────────────
# Public verbs
# ───────────────────────────────────────────────────────────────────────────


@_retry
def put_content(tenant_id: str, key: str, encrypted_bytes: bytes) -> None:
    """Write ``encrypted_bytes`` at ``tenants/{tenant_id}/{key}``.

    Caller MUST pass already-encrypted bytes; this wrapper does no
    crypto. T5.1's ingest path is the call site that wraps in the
    tenant DEK before calling here.

    Overwrites silently if the key already exists — standard S3
    semantics. The choice is deliberate: T5.1's content-blob keys are
    immutable-by-construction (they include the usage_record_id),
    so an overwrite means the same object was re-uploaded, which is
    safe and idempotent.
    """
    if not isinstance(encrypted_bytes, (bytes, bytearray, memoryview)):
        raise TypeError(
            f"encrypted_bytes must be bytes-like, got "
            f"{type(encrypted_bytes).__name__}"
        )
    full = _full_key(tenant_id, key)
    try:
        _client_singleton().put_object(
            Bucket=_bucket(),
            Key=full,
            Body=bytes(encrypted_bytes),
        )
    except ClientError as exc:
        raise StorageError(
            f"put_content failed for tenant={tenant_id} key={key}"
        ) from exc


@_retry
def get_content(tenant_id: str, key: str) -> bytes:
    """Read the bytes at ``tenants/{tenant_id}/{key}``.

    Raises ``NotFound`` if the key is missing — distinct from
    ``StorageError`` so callers can do an explicit lookup-or-default
    pattern without parsing exception messages.
    """
    full = _full_key(tenant_id, key)
    try:
        resp = _client_singleton().get_object(Bucket=_bucket(), Key=full)
    except ClientError as exc:
        if _is_not_found_error(exc):
            raise NotFound(
                f"no object at tenant={tenant_id} key={key}"
            ) from exc
        raise StorageError(
            f"get_content failed for tenant={tenant_id} key={key}"
        ) from exc

    # `Body` is a StreamingBody; read() pulls the whole payload. The
    # tenant-content blobs are bounded by the Compliance API response
    # size (T5.1 — typically <100 KB per blob), so eager read is fine.
    # Switch to streaming if a future caller needs to chunk-decrypt.
    body = resp["Body"]
    try:
        return body.read()
    finally:
        # Defensive close — boto3 normally closes the stream on
        # garbage collection but explicit is cheap insurance.
        try:
            body.close()
        except Exception:  # pragma: no cover
            pass


@_retry
def delete_content(tenant_id: str, key: str) -> None:
    """Delete the object at ``tenants/{tenant_id}/{key}``.

    Idempotent — deleting a missing key is a no-op (S3 semantics).
    Callers that need to distinguish "deleted now" from "never
    existed" should HEAD with ``exists()`` first.
    """
    full = _full_key(tenant_id, key)
    try:
        _client_singleton().delete_object(Bucket=_bucket(), Key=full)
    except ClientError as exc:
        raise StorageError(
            f"delete_content failed for tenant={tenant_id} key={key}"
        ) from exc


@_retry
def exists(tenant_id: str, key: str) -> bool:
    """HEAD probe for the key. Returns ``True`` iff the object is
    present, ``False`` if it isn't.

    Cheap — no body transfer. Used by the test suite's cleanup
    fixture and by T5.1's idempotency check before a re-upload.
    Raises ``StorageError`` for non-404 errors so a caller treating
    False as "missing" doesn't silently swallow a network outage.
    """
    full = _full_key(tenant_id, key)
    try:
        _client_singleton().head_object(Bucket=_bucket(), Key=full)
        return True
    except ClientError as exc:
        if _is_not_found_error(exc):
            return False
        raise StorageError(
            f"exists check failed for tenant={tenant_id} key={key}"
        ) from exc


# ───────────────────────────────────────────────────────────────────────────
# Test seam — re-export for fixtures that need to reset the client cache
# ───────────────────────────────────────────────────────────────────────────


__all__ = [
    "NotFound",
    "StorageError",
    "delete_content",
    "exists",
    "get_content",
    "put_content",
    "_reset_client_for_test",
]
