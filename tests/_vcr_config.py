# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""VCR configuration for Anthropic Admin API test cassettes (T3.1+).

`vcr_for_anthropic()` returns a `vcr.VCR` instance wired with the
redaction policy every cassette we ship MUST honor: the `x-api-key`
header is replaced with the literal string `REDACTED` so a recorded
cassette never leaks a real admin key, even if it was committed by
accident.

Importable from `pythonpath = ["tests"]` (set in pyproject.toml since
T2.5) — test modules do `from _vcr_config import vcr_for_anthropic`.

Default `record_mode="none"` so CI never accidentally hits the real
API; recording sessions opt in with `"once"` or `"new_episodes"` at
the call site.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

import vcr


def vcr_for_anthropic(
    cassette_library_dir: Union[str, Path, None] = None,
    record_mode: str = "none",
) -> vcr.VCR:
    """Return a vcr.VCR configured for Anthropic Admin API tests.

    - Filters `x-api-key` to `REDACTED` on every recorded request.
    - Default cassette dir is `tests/fixtures/cassettes/`.
    - Default record_mode is `none` (replay-only, raises if missing).
    """
    if cassette_library_dir is None:
        cassette_library_dir = (
            Path(__file__).parent / "fixtures" / "cassettes"
        )
    return vcr.VCR(
        cassette_library_dir=str(cassette_library_dir),
        record_mode=record_mode,
        filter_headers=[("x-api-key", "REDACTED")],
        match_on=["method", "scheme", "host", "path", "query"],
    )
