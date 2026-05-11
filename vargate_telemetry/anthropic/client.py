# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anthropic Admin API client base (T3.1).

Single class — `AnthropicAdminClient` — that wraps `httpx.Client` with:

  - Standard auth headers (`x-api-key`, `anthropic-version`)
  - Tenacity-backed retry on `RateLimited` (429), exponential backoff
    honoring the server's `Retry-After` hint
  - Immediate `AnthropicAPIError` propagation on 5xx
  - Cursor-based pagination via the `paginate()` generator

Retry policy is constructor-injectable so tests can pass `min_wait=0`
to skip waits entirely. Production uses the spec defaults (2 s min,
60 s max, 5 attempts, multiplier 1).

The `transport` kwarg is also injectable so tests can pass
`httpx.MockTransport(handler)` to drive deterministic response
sequences without monkey-patching httpx internals.

Typed endpoints (list_usage, list_members, list_workspaces) land in
T3.2 — this sprint is just the transport layer.
"""

from __future__ import annotations

from typing import Any, Iterator, Optional

import httpx
from tenacity import (
    Retrying,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
    wait_none,
)

from vargate_telemetry.anthropic.exceptions import (
    AnthropicAPIError,
    RateLimited,
)


class AnthropicAdminClient:
    """HTTP client for the Anthropic Admin API.

    Thread-safe under httpx's connection-pool semantics. Caller is
    responsible for `close()` (or `with` block) so the underlying
    `httpx.Client` releases its pool.
    """

    DEFAULT_BASE_URL = "https://api.anthropic.com"
    DEFAULT_ANTHROPIC_VERSION = "2023-06-01"
    DEFAULT_RETRY_AFTER_SECONDS = 10

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        max_attempts: int = 5,
        min_wait: float = 2.0,
        max_wait: float = 60.0,
        wait_multiplier: float = 1.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        if not api_key:
            raise ValueError("api_key required")

        client_kwargs: dict[str, Any] = {
            "base_url": base_url,
            "headers": {
                "x-api-key": api_key,
                "anthropic-version": self.DEFAULT_ANTHROPIC_VERSION,
            },
            "timeout": timeout,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

        self._max_attempts = max_attempts
        self._min_wait = min_wait
        self._max_wait = max_wait
        self._wait_multiplier = wait_multiplier

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "AnthropicAdminClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _retrying(self) -> Retrying:
        """Build a per-call Retrying instance from the policy params.

        Fresh instance per call so successive `_get` invocations get
        independent attempt budgets. `wait_none()` short-circuits the
        backoff entirely when `min_wait=0` — useful for tests.
        """
        wait = (
            wait_exponential(
                multiplier=self._wait_multiplier,
                min=self._min_wait,
                max=self._max_wait,
            )
            if self._min_wait > 0
            else wait_none()
        )
        return Retrying(
            retry=retry_if_exception_type(RateLimited),
            wait=wait,
            stop=stop_after_attempt(self._max_attempts),
            reraise=True,
        )

    def _raw_get(
        self, path: str, params: Optional[dict] = None
    ) -> httpx.Response:
        """One HTTP GET; raises RateLimited on 429 and AnthropicAPIError on 5xx."""
        r = self._client.get(path, params=params)
        if r.status_code == 429:
            retry_after = int(
                r.headers.get(
                    "retry-after", str(self.DEFAULT_RETRY_AFTER_SECONDS)
                )
            )
            raise RateLimited(retry_after=retry_after)
        if r.status_code >= 500:
            raise AnthropicAPIError(r.status_code, r.text)
        r.raise_for_status()
        return r

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        """GET with tenacity-backed retry on 429. Returns parsed JSON."""
        retryer = self._retrying()
        response = retryer(self._raw_get, path, params)
        return response.json()

    def paginate(
        self, path: str, params: Optional[dict] = None
    ) -> Iterator[dict]:
        """Yield rows from `data` across all cursor pages.

        Anthropic's Admin endpoints page via a `next_page` cursor token
        on the response envelope. The generator stops when `next_page`
        is absent or empty.
        """
        p = dict(params or {})
        while True:
            data = self._get(path, p)
            yield from data.get("data", [])
            cursor = data.get("next_page")
            if not cursor:
                return
            p["page"] = cursor
