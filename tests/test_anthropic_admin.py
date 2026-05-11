# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the typed Anthropic Admin API methods (T3.2).

Best-guess response shapes per `vargate_telemetry/anthropic/types.py`.
Real cassettes against a live test org land in T3.x — any drift then
surfaces as a Pydantic validation failure here, fixed by updating the
type and re-running.

Pagination test pins the `has_more` + `last_id` → `after_id` contract
specifically; the three list_* tests verify that response parsing
emerges with the expected typed objects.

All tests use `httpx.MockTransport` for deterministic response
sequences (same pattern as `test_anthropic_client.py`).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

import httpx

from vargate_telemetry.anthropic import (
    AnthropicAdminClient,
    Member,
    UsageBucket,
    Workspace,
)


def _zero_wait_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AnthropicAdminClient:
    """Build a client wired with MockTransport + zero retry-wait."""
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(handler),
    )


def test_list_workspaces_parses_response() -> None:
    """A two-workspace single-page response parses into Workspace objects."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/organizations/workspaces"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "wrkspc_a",
                        "type": "workspace",
                        "name": "Prod",
                        "created_at": "2026-01-01T00:00:00Z",
                        "archived_at": None,
                        "display_color": "#FF0000",
                    },
                    {
                        "id": "wrkspc_b",
                        "type": "workspace",
                        "name": "Staging",
                        "created_at": "2026-02-01T00:00:00Z",
                        "archived_at": None,
                        "display_color": None,
                    },
                ],
                "has_more": False,
                "first_id": "wrkspc_a",
                "last_id": "wrkspc_b",
            },
        )

    with _zero_wait_client(handler) as client:
        workspaces = list(client.list_workspaces())

    assert len(workspaces) == 2
    assert all(isinstance(w, Workspace) for w in workspaces)
    assert workspaces[0].id == "wrkspc_a"
    assert workspaces[0].name == "Prod"
    assert workspaces[0].display_color == "#FF0000"
    assert workspaces[1].name == "Staging"
    assert workspaces[1].display_color is None


def test_list_members_parses_response() -> None:
    """A member list parses into Member objects with role and added_at."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/organizations/users"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "user_alice",
                        "type": "user",
                        "email": "alice@example.com",
                        "name": "Alice Smith",
                        "role": "admin",
                        "added_at": "2025-09-15T10:00:00Z",
                    },
                    {
                        "id": "user_bob",
                        "type": "user",
                        "email": "bob@example.com",
                        "name": None,
                        "role": "developer",
                        "added_at": "2026-01-04T16:30:00Z",
                    },
                ],
                "has_more": False,
                "first_id": "user_alice",
                "last_id": "user_bob",
            },
        )

    with _zero_wait_client(handler) as client:
        members = list(client.list_members())

    assert len(members) == 2
    assert all(isinstance(m, Member) for m in members)
    assert members[0].email == "alice@example.com"
    assert members[0].role == "admin"
    assert members[1].name is None
    assert members[1].role == "developer"


def test_list_usage_parses_nested_results() -> None:
    """Usage buckets parse with nested `results` breakdown entries."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert (
            request.url.path
            == "/v1/organizations/usage_report/messages"
        )
        assert request.url.params["starting_at"] == "2026-05-01T00:00:00+00:00"
        assert request.url.params["bucket_width"] == "1d"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": "2026-05-01T00:00:00Z",
                        "ending_at": "2026-05-02T00:00:00Z",
                        "results": [
                            {
                                "model": "claude-sonnet-4-6",
                                "workspace_id": "wrkspc_a",
                                "service_tier": "standard",
                                "context_window": "0-200k",
                                "uncached_input_tokens": 12_000,
                                "output_tokens": 4_500,
                                "cache_creation_input_tokens": 800,
                                "cache_read_input_tokens": 300,
                            },
                            {
                                "model": "claude-opus-4-7",
                                "workspace_id": "wrkspc_b",
                                "service_tier": "priority",
                                "context_window": "0-1m",
                                "uncached_input_tokens": 5_000,
                                "output_tokens": 1_200,
                                "cache_creation_input_tokens": 0,
                                "cache_read_input_tokens": 0,
                            },
                        ],
                    },
                ],
                "has_more": False,
            },
        )

    with _zero_wait_client(handler) as client:
        buckets = list(
            client.list_usage(
                starting_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
                ending_at=datetime(2026, 5, 11, tzinfo=timezone.utc),
            )
        )

    assert len(buckets) == 1
    assert all(isinstance(b, UsageBucket) for b in buckets)
    assert len(buckets[0].results) == 2
    sonnet = buckets[0].results[0]
    assert sonnet.model == "claude-sonnet-4-6"
    assert sonnet.input_tokens == 12_000  # aliased from uncached_input_tokens
    assert sonnet.output_tokens == 4_500
    opus = buckets[0].results[1]
    assert opus.workspace_id == "wrkspc_b"
    assert opus.service_tier == "priority"


def test_paginate_admin_advances_after_id() -> None:
    """has_more + last_id drives the next request's after_id parameter."""
    pages = [
        {
            "data": [{"id": "u_1"}, {"id": "u_2"}],
            "has_more": True,
            "first_id": "u_1",
            "last_id": "u_2",
        },
        {
            "data": [{"id": "u_3"}],
            "has_more": False,
            "first_id": "u_3",
            "last_id": "u_3",
        },
    ]
    seen_after_ids: list[str | None] = []
    page_idx = {"i": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_after_ids.append(request.url.params.get("after_id"))
        body = pages[page_idx["i"]]
        page_idx["i"] += 1
        return httpx.Response(200, json=body)

    with _zero_wait_client(handler) as client:
        rows = list(client._paginate_admin("/v1/organizations/users"))

    assert [r["id"] for r in rows] == ["u_1", "u_2", "u_3"]
    # First request has no after_id; second uses last_id from page 1.
    assert seen_after_ids == [None, "u_2"]
