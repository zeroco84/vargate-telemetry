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

    With T4.2 the gateway exists, so we can actually compare. Enabled
    per-environment with `OGMA_OPENAPI_DIFF_ROUTES=1`. T4.2's
    contract surface is the three SSO/me operations; later sprints
    flesh out the rest. The diff:

      - Compares the path set in the YAML to the path set in
        `app.openapi()["paths"]`. Paths declared in YAML but not
        implemented are NOT yet a failure (T4 ships incrementally;
        the onboarding endpoints come online sprint-by-sprint).
        Paths IMPLEMENTED but not in YAML ARE a failure — that's
        drift.
      - For every operation present in BOTH, checks that the
        operationId matches. Misalignment here breaks the
        frontend's generated types.
    """
    if os.environ.get("OGMA_OPENAPI_DIFF_ROUTES") != "1":
        print(
            "SKIP: runtime/spec diff disabled "
            "(set OGMA_OPENAPI_DIFF_ROUTES=1 to enable per env)."
        )
        return 0

    try:
        import yaml
    except ImportError:
        print("FAIL: PyYAML not importable", file=sys.stderr)
        return 1

    try:
        from vargate_telemetry.api.app import app
    except Exception as exc:
        print(
            f"FAIL: could not import the FastAPI app — {exc}",
            file=sys.stderr,
        )
        return 2

    runtime = app.openapi()
    runtime_paths: dict = runtime.get("paths") or {}

    with SPEC_PATH.open() as fh:
        committed = yaml.safe_load(fh)
    committed_paths: dict = committed.get("paths") or {}

    # Paths implemented but missing from the YAML — drift we must
    # never tolerate. The contract is the source of truth.
    undeclared: list[str] = []
    for path, methods in runtime_paths.items():
        if path not in committed_paths:
            method_list = ", ".join(
                m.upper()
                for m in methods
                if m in ("get", "post", "put", "patch", "delete")
            )
            undeclared.append(f"{method_list} {path}")
            continue

    if undeclared:
        print(
            "FAIL: routes implemented but absent from the YAML contract:",
            file=sys.stderr,
        )
        for u in undeclared:
            print(f"  - {u}", file=sys.stderr)
        return 2

    # operationId drift on operations present in both.
    op_drift: list[str] = []
    for path, methods in runtime_paths.items():
        for method, op in methods.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            committed_op = (committed_paths[path] or {}).get(method)
            if committed_op is None:
                op_drift.append(
                    f"{method.upper()} {path}: method implemented but not in YAML"
                )
                continue
            runtime_id = op.get("operationId")
            yaml_id = committed_op.get("operationId")
            if runtime_id and yaml_id and runtime_id != yaml_id:
                op_drift.append(
                    f"{method.upper()} {path}: operationId "
                    f"{runtime_id!r} (runtime) vs {yaml_id!r} (YAML)"
                )

    if op_drift:
        print("FAIL: operationId drift:", file=sys.stderr)
        for d in op_drift:
            print(f"  - {d}", file=sys.stderr)
        return 2

    # Count how many YAML-declared paths are NOT yet implemented.
    # This is information, not failure — sprints fill these in over
    # time. Helps planning to see the gap.
    not_yet_implemented = sorted(
        path for path in committed_paths if path not in runtime_paths
    )
    print(
        f"OK: runtime has {len(runtime_paths)} paths, YAML has "
        f"{len(committed_paths)} paths "
        f"({len(not_yet_implemented)} contract paths not yet implemented)."
    )
    if not_yet_implemented:
        print("  Not yet implemented (informational):")
        for path in not_yet_implemented:
            print(f"    - {path}")
    return 0


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
