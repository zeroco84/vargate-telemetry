# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Exceptions raised by the Anthropic Admin API client (T3.1).

Two exception types, distinguished by whether the caller should retry:

  - `RateLimited` carries a `retry_after` seconds value parsed from the
    `Retry-After` header. Retryable — the client's tenacity wrapper
    catches it and re-issues the request with exponential backoff.
  - `AnthropicAPIError` is the non-retryable 5xx (and any other API
    failure once retries are exhausted). Carries the HTTP status code
    plus the truncated response body for logging.

Native httpx errors (4xx other than 429, network failures) bubble up
unwrapped — the client's `_get` calls `response.raise_for_status()`
for the 4xx case and lets `httpx.HTTPStatusError` propagate. Callers
catch the specific exception type they care about.
"""

from __future__ import annotations


class AnthropicAPIError(Exception):
    """Non-retryable API failure. 5xx responses raise this immediately."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(
            f"Anthropic API error {status_code}: {body[:200]}"
        )
        self.status_code = status_code
        self.body = body


class RateLimited(Exception):
    """429 Too Many Requests. Retryable; carries the server's retry-after hint."""

    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited; retry after {retry_after}s")
        self.retry_after = retry_after
