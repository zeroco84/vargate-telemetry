# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Exceptions raised by the OpenAI Admin API client (TM8 Phase B).

Mirrors ``anthropic/exceptions.py`` one-for-one so the parallel pull
tasks can ``except OpenAIAPIError`` / ``except InsufficientScope`` /
``except RateLimited`` with the same control flow they use for
Anthropic. Two axes matter:

  - ``RateLimited`` carries a ``retry_after`` seconds value parsed from
    the ``Retry-After`` header. Retryable — the client's tenacity
    wrapper catches it and re-issues with exponential backoff.
  - ``OpenAIAPIError`` is the non-retryable 5xx (and any other API
    failure once retries are exhausted). Carries the HTTP status code
    plus the truncated response body for logging.

``InsufficientScope`` (403) is the soft-skip signal: the OpenAI
``audit_logs`` / per-user breakdown surfaces can 403 on org tiers that
don't expose them, and the recon (TM8 Phase A) confirmed each endpoint
family returns 403 rather than a silently-empty 200 when access is
denied. The pull tasks catch it and return a status dict instead of
failing the whole dispatch.

Native httpx errors (4xx other than 403/429, network failures) bubble
up unwrapped — the client's ``_get`` calls ``raise_for_status()`` for
the residual 4xx case and lets ``httpx.HTTPStatusError`` propagate.
"""

from __future__ import annotations


class OpenAIAPIError(Exception):
    """Non-retryable API failure. 5xx responses raise this immediately."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"OpenAI API error {status_code}: {body[:200]}")
        self.status_code = status_code
        self.body = body


class RateLimited(Exception):
    """429 Too Many Requests. Retryable; carries the server's retry-after hint."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after


class InsufficientScope(OpenAIAPIError):
    """403 Forbidden — the supplied key lacks access to this endpoint.

    TM8 Phase A recon: every OpenAI org endpoint returned 200 with a
    read-only Admin key on a Pay-as-you-go org, so 403 in production
    means a genuinely scope-limited key (or an org tier that gates the
    surface — e.g. audit_logs on non-Enterprise). Surfacing this as a
    typed exception lets the per-tenant pull tasks soft-skip ("no
    OpenAI audit access → advance cursor, status no_audit_access")
    without parsing error bodies.

    Subclass of ``OpenAIAPIError`` so a broad ``except OpenAIAPIError``
    catches it; callers that want to distinguish do ``except
    InsufficientScope``. Mirrors ``anthropic.exceptions.InsufficientScope``.
    """

    def __init__(self, body: str, required_scope: str | None = None) -> None:
        super().__init__(403, body)
        self.required_scope = required_scope
