# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Object-store package (T5.0).

Submodules:

  - ``object_store`` — typed wrapper around boto3 for the MinIO
    tenant-content bucket. PUT / GET / DELETE / HEAD with retries,
    tenant-prefixed keys, and a small set of typed exceptions
    (``NotFound``, ``StorageError``) that callers can match on without
    pulling in botocore exception types.

Future submodules can land here as additional storage shapes appear
(e.g., backup-archive store, audit-export bucket).
"""

from vargate_telemetry.storage.object_store import (
    NotFound,
    StorageError,
    delete_content,
    exists,
    get_content,
    put_content,
)

__all__ = [
    "NotFound",
    "StorageError",
    "delete_content",
    "exists",
    "get_content",
    "put_content",
]
