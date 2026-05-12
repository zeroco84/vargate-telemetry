# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Object-store package (T5.0 + T5.1).

Submodules:

  - ``object_store`` (T5.0) — typed wrapper around boto3 for the MinIO
    tenant-content bucket. PUT / GET / DELETE / HEAD with retries,
    tenant-prefixed keys, and a small set of typed exceptions
    (``NotFound``, ``StorageError``) that callers can match on without
    pulling in botocore exception types.
  - ``content`` (T5.1) — high-level wrapper that composes ``object_store``
    with the tenant DEK + AES-GCM seal. Public surface:
    ``store_content``, ``retrieve_content``, ``delete_content``, plus
    the ``IntegrityError`` typed exception (subclass of
    ``StorageError``) for AES-GCM-tag failures.

Future submodules can land here as additional storage shapes appear
(e.g., backup-archive store, audit-export bucket).
"""

# T5.1 content layer is the primary import target for ingest callers.
# T5.0 object_store stays exported for callers that need raw byte I/O
# (the smoke script, future audit-bundle exporters).
from vargate_telemetry.storage import content, object_store
from vargate_telemetry.storage.content import IntegrityError
from vargate_telemetry.storage.object_store import (
    NotFound,
    StorageError,
    delete_content,
    exists,
    get_content,
    put_content,
)

__all__ = [
    "IntegrityError",
    "NotFound",
    "StorageError",
    "content",
    "delete_content",
    "exists",
    "get_content",
    "object_store",
    "put_content",
]
