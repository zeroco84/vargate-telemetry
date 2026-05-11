# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for `openapi/ogma-api.yaml` — the API contract source of truth (T4.0).

Two checks, both fast (no Postgres, no Redis, no HSM):

  - `test_yaml_parses_as_valid_openapi_3_1` — parses the YAML and
    validates it against the OpenAPI 3.1 JSON Schema via
    `openapi-spec-validator`.

  - `test_required_endpoints_present` — pins the seven T4 onboarding
    endpoints by (method, path). If a future commit accidentally
    removes or renames one of them, this test fails before the
    frontend ever notices.

The matching CI-style script lives at `scripts/validate_openapi.py`.
Keeping the contract in the pytest suite means every PR's test run
also re-validates the contract.
"""

from __future__ import annotations

from pathlib import Path

import yaml


# Resolved at import time so test failures point at the actual file path.
SPEC_PATH = (
    Path(__file__).resolve().parent.parent / "openapi" / "ogma-api.yaml"
)


# The T4 onboarding contract, by (HTTP method, path) tuple. Every entry
# here MUST appear in the YAML — that's the property this module pins.
# Path values are written WITHOUT the `/api` server prefix; the YAML's
# `servers: [{url: /api}]` block provides that.
REQUIRED_ENDPOINTS: list[tuple[str, str]] = [
    ("post", "/auth/sso/google/callback"),
    ("post", "/auth/sso/microsoft/callback"),
    ("post", "/onboarding/validate-key"),
    ("post", "/onboarding/select-region"),
    ("post", "/onboarding/start-backfill"),
    ("get", "/onboarding/backfill-status/{task_id}"),
    ("get", "/me"),
]


def test_yaml_parses_as_valid_openapi_3_1() -> None:
    """The committed YAML is well-formed OpenAPI 3.1.x."""
    from openapi_spec_validator import validate
    from openapi_spec_validator.readers import read_from_filename

    assert SPEC_PATH.exists(), f"spec file missing at {SPEC_PATH}"

    spec_dict, _base_uri = read_from_filename(str(SPEC_PATH))

    version = spec_dict.get("openapi", "")
    assert isinstance(version, str), f"openapi version is not a string: {version!r}"
    assert version.startswith("3.1"), (
        f"expected OpenAPI 3.1.x, got {version!r}"
    )

    # Raises `OpenAPIValidationError` on schema violations.
    validate(spec_dict)


def test_required_endpoints_present() -> None:
    """Every T4 onboarding endpoint is defined in the contract."""
    with SPEC_PATH.open() as fh:
        spec = yaml.safe_load(fh)

    paths: dict = spec.get("paths") or {}

    missing: list[tuple[str, str]] = []
    for method, path in REQUIRED_ENDPOINTS:
        if path not in paths:
            missing.append((method, path))
            continue
        if method not in paths[path]:
            missing.append((method, path))

    assert not missing, (
        "required endpoints absent from openapi/ogma-api.yaml: " + ", ".join(
            f"{m.upper()} {p}" for m, p in missing
        )
    )


def test_every_path_carries_an_operation_id() -> None:
    """operationId is the stable handle for code-generated client methods.

    Frontend's `openapi-typescript` generator names methods after the
    operationId. Renaming or losing one silently changes the
    generated TypeScript surface. Pin that every defined operation
    has an explicit operationId so we notice rename-by-omission.
    """
    with SPEC_PATH.open() as fh:
        spec = yaml.safe_load(fh)

    missing: list[str] = []
    for path, methods in (spec.get("paths") or {}).items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(op, dict):
                continue
            if not op.get("operationId"):
                missing.append(f"{method.upper()} {path}")

    assert not missing, (
        "operations without an explicit operationId: " + ", ".join(missing)
    )
