# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Anthropic Admin API client base (T3.1).

Retry, pagination, and 5xx propagation use `httpx.MockTransport` to
drive deterministic response sequences — VCR with hand-crafted
cassettes was considered and rejected because it couples test
deterministic-ness to vcrpy's httpx integration story (still
experimental in vcrpy 6.x). The VCR config itself is exercised in
`test_vcr_config_redacts_auth_header`, which is the contract that
matters: any cassette T3.2+ records through `vcr_for_anthropic` will
have the API key filtered out.
"""

from __future__ import annotations

import httpx
import pytest

from vargate_telemetry.anthropic import (
    AnthropicAdminClient,
    AnthropicAPIError,
)


def _zero_wait_client(handler, *, max_attempts: int = 5) -> AnthropicAdminClient:
    """Build a client wired with MockTransport and zero retry-wait.

    `min_wait=0` selects `wait_none()` inside the client so tests run
    in milliseconds; `max_attempts` is the tenacity stop bound.
    """
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        max_attempts=max_attempts,
        min_wait=0.0,
        transport=httpx.MockTransport(handler),
    )


def test_get_handles_429_with_backoff() -> None:
    """Two 429s then a 200 → client returns the parsed body after retries."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": "rate limited"},
            )
        return httpx.Response(200, json={"data": [{"id": "u_1"}]})

    with _zero_wait_client(handler) as client:
        result = client._get("/v1/usage")

    assert result == {"data": [{"id": "u_1"}]}
    assert call_count["n"] == 3


def test_get_propagates_500_after_retries(
) -> None:
    """429, 429, 500 → AnthropicAPIError after the retry budget is consumed."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": "rate limited"},
            )
        return httpx.Response(500, text="upstream exploded")

    with _zero_wait_client(handler) as client:
        with pytest.raises(AnthropicAPIError) as excinfo:
            client._get("/v1/usage")

    assert excinfo.value.status_code == 500
    assert "upstream exploded" in excinfo.value.body
    assert call_count["n"] == 3


def test_paginate_yields_all_pages() -> None:
    """Three pages of data with cursor advancement → flat iterator of all items."""
    pages = [
        {"data": [{"id": "a"}, {"id": "b"}], "next_page": "p2"},
        {"data": [{"id": "c"}, {"id": "d"}], "next_page": "p3"},
        {"data": [{"id": "e"}]},  # last page — no next_page
    ]
    page_index = {"i": 0}
    seen_cursors: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cursors.append(request.url.params.get("page"))
        body = pages[page_index["i"]]
        page_index["i"] += 1
        return httpx.Response(200, json=body)

    with _zero_wait_client(handler) as client:
        rows = list(client.paginate("/v1/usage"))

    assert [r["id"] for r in rows] == ["a", "b", "c", "d", "e"]
    # First call has no `page`; subsequent calls carry the prior next_page.
    assert seen_cursors == [None, "p2", "p3"]


def test_vcr_config_redacts_auth_header() -> None:
    """vcr_for_anthropic() configures x-api-key → REDACTED in every cassette."""
    from _vcr_config import vcr_for_anthropic

    v = vcr_for_anthropic()

    # filter_headers is the list passed into vcr.VCR; entries are either
    # plain names (delete) or (name, replacement) tuples (rewrite).
    redactions = {
        h[0].lower(): h[1]
        for h in v.filter_headers
        if isinstance(h, tuple)
    }

    assert "x-api-key" in redactions, (
        "VCR config must filter the x-api-key header"
    )
    assert redactions["x-api-key"] == "REDACTED", (
        "x-api-key must be rewritten to the literal 'REDACTED' string, "
        f"not removed or replaced with something else: got {redactions['x-api-key']!r}"
    )
