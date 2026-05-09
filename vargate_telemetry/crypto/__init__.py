# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Crypto primitives for the Telemetry envelope-encryption pattern (T1.6).

Public surface:

    from vargate_telemetry.crypto import (
        get_or_create_kek, wrap_dek, unwrap_dek,    # HSM-backed KEK ops
        generate_dek, encrypt_with_dek, decrypt_with_dek,
    )
"""

from vargate_telemetry.crypto.dek import (
    decrypt_with_dek,
    encrypt_with_dek,
    generate_dek,
)
from vargate_telemetry.crypto.hsm import (
    get_or_create_kek,
    unwrap_dek,
    wrap_dek,
)
from vargate_telemetry.crypto.integrity import (
    IntegrityError,
    compute_integrity_tag,
    verify_integrity_tag,
)
from vargate_telemetry.crypto.seal import (
    get_tenant_dek,
    provision_tenant_dek,
    seal_secret,
    unseal_secret,
)

__all__ = [
    "IntegrityError",
    "compute_integrity_tag",
    "decrypt_with_dek",
    "encrypt_with_dek",
    "generate_dek",
    "get_or_create_kek",
    "get_tenant_dek",
    "provision_tenant_dek",
    "seal_secret",
    "unseal_secret",
    "unwrap_dek",
    "verify_integrity_tag",
    "wrap_dek",
]
