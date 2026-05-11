#!/usr/bin/env python3
# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""CI check for `openapi/ogma-api.yaml`.

Two responsibilities, both as small as possible:

  1. Parse the YAML and validate it against the OpenAPI 3.1 JSON
     Schema (via `openapi-spec-validator`).
  2. (Once FastAPI routes for the onboarding endpoints land in
     T4.2+) call `app.openapi()` on the running app and diff the
     runtime-generated spec against the committed YAML. Drift
     fails the script.

Today, only step 1 runs — onboarding routes don't exist yet, and
`app.openapi()` would produce a spec that's missing every path
we just committed. Step 2 is gated behind the env var
`OGMA_OPENAPI_DIFF_ROUTES=1` so it can be enabled per CI job once
the routes exist, without churning this file.

Exit codes:
  0  — contract is clean.
  1  — YAML failed to parse or failed schema validation.
  2  — runtime/spec drift (only reachable when OGMA_OPENAPI_DIFF_ROUTES=1
       and the FastAPI app exposes the same path set as the YAML).

Usage (from inside the celery-worker container):

    python scripts/validate_openapi.py

Usage (host-side dev loop):

    docker compose exec celery-worker python scripts/validate_openapi.py
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


SPEC_PATH = Path(__file__).resolve().parent.parent / "openapi" / "ogma-api.yaml"


def _validate_yaml_against_openapi_31() -> int:
    """Step 1: parse + schema-validate the committed YAML."""
    if not SPEC_PATH.exists():
        print(f"FAIL: spec file does not exist at {SPEC_PATH}", file=sys.stderr)
        return 1

    try:
        from openapi_spec_validator import validate
        from openapi_spec_validator.readers import read_from_filename
    except ImportError as exc:
        print(
            "FAIL: openapi-spec-validator not importable — was the image "
            f"rebuilt after the requirements.txt bump?\n  {exc}",
            file=sys.stderr,
        )
        return 1

    try:
        spec_dict, _base_uri = read_from_filename(str(SPEC_PATH))
    except Exception as exc:
        print(f"FAIL: could not read/parse {SPEC_PATH}: {exc}", file=sys.stderr)
        return 1

    version = spec_dict.get("openapi", "")
    if not isinstance(version, str) or not version.startswith("3.1"):
        print(
            f"FAIL: spec declares openapi version {version!r}; expected '3.1.x'",
            file=sys.stderr,
        )
        return 1

    try:
        validate(spec_dict)
    except Exception as exc:
        print(f"FAIL: OpenAPI 3.1 schema validation rejected the spec:\n  {exc}", file=sys.stderr)
        return 1

    paths_count = len(spec_dict.get("paths", {}))
    schemas_count = len((spec_dict.get("components") or {}).get("schemas", {}))
    print(
        f"OK: {SPEC_PATH.name} parses cleanly as OpenAPI {version} "
        f"({paths_count} paths, {schemas_count} schemas)."
    )
    return 0


def _diff_runtime_against_yaml() -> int:
    """Step 2: runtime FastAPI app.openapi() drift check (gated).

    Disabled by default until the onboarding routes land in T4.2+.
    Enable per-environment with `OGMA_OPENAPI_DIFF_ROUTES=1`. The
    real implementation will:

      - Import `vargate_telemetry.api` (or whatever the FastAPI
        app module ends up named) and call `app.openapi()`.
      - Compare path set, method set per path, and operationId set
        against the committed YAML.
      - Exit 2 on any difference, with a one-line summary of the
        drift category (path-missing / method-missing / etc.).

    Today, returning 0 means "nothing to check yet."
    """
    if os.environ.get("OGMA_OPENAPI_DIFF_ROUTES") != "1":
        print(
            "SKIP: runtime/spec diff disabled "
            "(set OGMA_OPENAPI_DIFF_ROUTES=1 to enable once routes exist)."
        )
        return 0

    # Placeholder for T4.2+ — fail loud rather than silently passing,
    # so a CI job that opts in but is misconfigured doesn't fake-pass.
    print(
        "FAIL: OGMA_OPENAPI_DIFF_ROUTES=1 was set but no FastAPI app is "
        "wired into this script yet (T4.2 hand-off).",
        file=sys.stderr,
    )
    return 2


def main() -> int:
    rc = _validate_yaml_against_openapi_31()
    if rc != 0:
        return rc

    rc = _diff_runtime_against_yaml()
    if rc != 0:
        return rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
