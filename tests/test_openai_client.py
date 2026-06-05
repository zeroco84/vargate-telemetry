# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the OpenAI Admin API client (TM8 Phase B).

Driven entirely by ``httpx.MockTransport(handler)`` + ``min_wait=0`` so
runs are deterministic and wait-free — the same approach as
``test_anthropic_client.py``. Covers:

  - request shaping: Bearer auth header, repeated ``group_by`` params,
    Unix-epoch ``start_time``/``end_time`` serialization
  - BOTH pagination styles from the recon (§6): usage/costs
    ``has_more``+``next_page`` (→ ``page=``), and lists
    ``first_id``/``last_id``/``has_more`` (→ ``after=``)
  - exception mapping: 403 → InsufficientScope, 429 → RateLimited (with
    Retry-After), 5xx → OpenAIAPIError
  - retry: 429s are retried on the tenacity wrapper, then success or a
    terminal 5xx surfaces
  - typed parsing: the cost ``amount.value`` Decimal-via-str path, the
    usage token-field set
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx
import pytest

from vargate_telemetry.openai import (
    InsufficientScope,
    OpenAIAdminClient,
    OpenAIAPIError,
)
from vargate_telemetry.openai.exceptions import RateLimited

# A fixed tz-aware window so epoch serialization is deterministic.
# 2026-06-01T00:00:00Z = 1780272000 ; 2026-06-02T00:00:00Z = 1780358400.
START = datetime(2026, 6, 1, tzinfo=timezone.utc)
END = datetime(2026, 6, 2, tzinfo=timezone.utc)
START_EPOCH = str(int(START.timestamp()))
END_EPOCH = str(int(END.timestamp()))


def _zero_wait_client(handler, *, max_attempts: int = 5) -> OpenAIAdminClient:
    """Client wired with MockTransport + zero retry-wait (ms-fast runs)."""
    return OpenAIAdminClient(
        api_key="sk-admin-test",
        base_url="https://api.test/v1/organization",
        max_attempts=max_attempts,
        min_wait=0.0,
        transport=httpx.MockTransport(handler),
    )


# ── transport: auth, retry, exception mapping ───────────────────────────────


def test_constructor_rejects_empty_key() -> None:
    with pytest.raises(ValueError):
        OpenAIAdminClient(api_key="")


def test_auth_header_is_bearer() -> None:
    """Every request carries ``Authorization: Bearer <key>``."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _zero_wait_client(handler) as client:
        list(client.list_projects())

    assert seen["auth"] == "Bearer sk-admin-test"


def test_get_handles_429_with_backoff() -> None:
    """Two 429s then a 200 → parsed body after retries; retry-after honored."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(
                429,
                headers={"retry-after": "0"},
                json={"error": "rate limited"},
            )
        return httpx.Response(200, json={"data": [{"id": "p_1"}]})

    with _zero_wait_client(handler) as client:
        result = client._get("/projects")

    assert result == {"data": [{"id": "p_1"}]}
    assert call_count["n"] == 3


def test_429_without_retry_after_defaults() -> None:
    """A 429 with no Retry-After header parses to the default, still retryable."""
    captured: list[int] = []
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            # No retry-after header at all.
            return httpx.Response(429, json={"error": "slow down"})
        return httpx.Response(200, json={"data": [], "has_more": False})

    client = _zero_wait_client(handler)
    # Probe the raw layer once to confirm the default retry_after value.
    try:
        client._raw_get("/projects")
    except RateLimited as e:
        captured.append(e.retry_after)
    client.close()

    assert captured == [OpenAIAdminClient.DEFAULT_RETRY_AFTER_SECONDS]


def test_get_propagates_500_after_retries() -> None:
    """429, 429, 500 → OpenAIAPIError once the retry budget is consumed."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] < 3:
            return httpx.Response(
                429, headers={"retry-after": "0"}, json={"error": "rl"}
            )
        return httpx.Response(500, text="upstream exploded")

    with _zero_wait_client(handler) as client:
        with pytest.raises(OpenAIAPIError) as excinfo:
            client._get("/projects")

    assert excinfo.value.status_code == 500
    assert "upstream exploded" in excinfo.value.body
    assert call_count["n"] == 3


def test_403_maps_to_insufficient_scope() -> None:
    """403 → InsufficientScope (a subclass of OpenAIAPIError), NOT retried."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(403, text="missing scope")

    with _zero_wait_client(handler) as client:
        with pytest.raises(InsufficientScope) as excinfo:
            client._get("/audit_logs")

    # 403 is terminal — the tenacity wrapper only retries RateLimited.
    assert call_count["n"] == 1
    assert excinfo.value.status_code == 403
    assert isinstance(excinfo.value, OpenAIAPIError)


def test_residual_4xx_bubbles_as_httpx_error() -> None:
    """A 400 (not 403/429) propagates as httpx.HTTPStatusError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="bad request")

    with _zero_wait_client(handler) as client:
        with pytest.raises(httpx.HTTPStatusError):
            client._get("/projects")


# ── request shaping: group_by, epoch ────────────────────────────────────────


def test_usage_sends_repeated_group_by_and_epoch() -> None:
    """Usage request: repeated ``group_by=`` params + epoch start/end times."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        # multi_items preserves repeated keys; params.get collapses them.
        captured["group_by"] = request.url.params.get_list("group_by")
        captured["start_time"] = request.url.params.get("start_time")
        captured["end_time"] = request.url.params.get("end_time")
        captured["bucket_width"] = request.url.params.get("bucket_width")
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _zero_wait_client(handler) as client:
        list(
            client.list_usage(
                start_time=START,
                end_time=END,
                group_by=["model", "user_id", "api_key_id", "project_id"],
            )
        )

    assert captured["path"] == "/v1/organization/usage/completions"
    assert captured["group_by"] == [
        "model",
        "user_id",
        "api_key_id",
        "project_id",
    ]
    assert captured["start_time"] == START_EPOCH
    assert captured["end_time"] == END_EPOCH
    assert captured["bucket_width"] == "1d"


def test_usage_modality_selects_endpoint() -> None:
    """``modality='embeddings'`` hits /usage/embeddings."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _zero_wait_client(handler) as client:
        list(
            client.list_usage(
                modality="embeddings", start_time=START, end_time=END
            )
        )

    assert seen["path"] == "/v1/organization/usage/embeddings"


def test_costs_sends_group_by_and_epoch() -> None:
    """Costs request: repeated ``group_by=`` params + epoch window."""
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["path"] = request.url.path
        captured["group_by"] = request.url.params.get_list("group_by")
        captured["start_time"] = request.url.params.get("start_time")
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _zero_wait_client(handler) as client:
        list(
            client.list_costs(
                start_time=START,
                end_time=END,
                group_by=["project_id", "line_item"],
            )
        )

    assert captured["path"] == "/v1/organization/costs"
    assert captured["group_by"] == ["project_id", "line_item"]
    assert captured["start_time"] == START_EPOCH


def test_project_api_keys_requires_project_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [], "has_more": False})

    with _zero_wait_client(handler) as client:
        with pytest.raises(ValueError):
            list(client.list_project_api_keys(""))


# ── pagination style 1: usage/costs has_more + next_page → page= ─────────────


def test_usage_paginates_via_next_page() -> None:
    """Three usage pages with ``next_page`` cursors flatten into one stream."""
    pages = [
        {
            "object": "page",
            "data": [
                {"start_time": 1780272000, "end_time": 1780358400, "results": []}
            ],
            "has_more": True,
            "next_page": "cursor-2",
        },
        {
            "object": "page",
            "data": [
                {"start_time": 1780358400, "end_time": 1780444800, "results": []}
            ],
            "has_more": True,
            "next_page": "cursor-3",
        },
        {
            "object": "page",
            "data": [
                {"start_time": 1780444800, "end_time": 1780531200, "results": []}
            ],
            "has_more": False,
            "next_page": None,
        },
    ]
    idx = {"i": 0}
    seen_page_cursor: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_page_cursor.append(request.url.params.get("page"))
        body = pages[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json=body)

    with _zero_wait_client(handler) as client:
        buckets = list(client.list_usage(start_time=START, end_time=END))

    assert len(buckets) == 3
    # First call has no ``page``; subsequent calls carry the prior next_page.
    assert seen_page_cursor == [None, "cursor-2", "cursor-3"]


def test_next_page_stops_when_next_page_missing_despite_has_more() -> None:
    """A stray ``has_more=true`` with no ``next_page`` is treated as terminal.

    Guards against an infinite loop on a malformed envelope — we stop
    the moment the cursor we'd advance on is absent.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "object": "page",
                "data": [
                    {
                        "start_time": 1780272000,
                        "end_time": 1780358400,
                        "results": [],
                    }
                ],
                "has_more": True,  # claims more …
                "next_page": None,  # … but gives no cursor → stop.
            },
        )

    with _zero_wait_client(handler) as client:
        buckets = list(client.list_usage(start_time=START, end_time=END))

    assert len(buckets) == 1
    assert call_count["n"] == 1


# ── pagination style 2: lists first_id/last_id/has_more → after= ─────────────


def test_projects_paginate_via_after_cursor() -> None:
    """Lists advance with ``after=<last_id>`` until ``has_more`` is false."""
    pages = [
        {
            "object": "list",
            "data": [{"id": "proj_a"}, {"id": "proj_b"}],
            "first_id": "proj_a",
            "last_id": "proj_b",
            "has_more": True,
        },
        {
            "object": "list",
            "data": [{"id": "proj_c"}],
            "first_id": "proj_c",
            "last_id": "proj_c",
            "has_more": False,
        },
    ]
    idx = {"i": 0}
    seen_after: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_after.append(request.url.params.get("after"))
        body = pages[idx["i"]]
        idx["i"] += 1
        return httpx.Response(200, json=body)

    with _zero_wait_client(handler) as client:
        projects = list(client.list_projects())

    assert [p.id for p in projects] == ["proj_a", "proj_b", "proj_c"]
    # First page no ``after``; second carries the prior page's last_id.
    assert seen_after == [None, "proj_b"]


def test_list_stops_when_last_id_missing() -> None:
    """``has_more=true`` but no ``last_id`` → terminate (no infinite loop)."""
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [{"id": "u_1"}],
                "has_more": True,
                "last_id": None,
            },
        )

    with _zero_wait_client(handler) as client:
        users = list(client.list_users())

    assert len(users) == 1
    assert call_count["n"] == 1


def test_audit_logs_empty_is_normal() -> None:
    """An empty audit feed (200, data:[]) yields nothing — accessible≠populated."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [],
                "first_id": None,
                "last_id": None,
                "has_more": False,
            },
        )

    with _zero_wait_client(handler) as client:
        entries = list(client.list_audit_logs())

    assert entries == []


def test_audit_logs_403_soft_skips_via_exception() -> None:
    """A scope-gated audit feed raises InsufficientScope for the pull task."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="audit not available on this tier")

    with _zero_wait_client(handler) as client:
        with pytest.raises(InsufficientScope):
            list(client.list_audit_logs())


# ── typed parsing ───────────────────────────────────────────────────────────


def test_usage_result_parses_full_token_fields() -> None:
    """A grouped usage row parses every recon §2 token field + dims."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "page",
                "has_more": False,
                "next_page": None,
                "data": [
                    {
                        "object": "bucket",
                        "start_time": 1780272000,
                        "end_time": 1780358400,
                        "results": [
                            {
                                "object": "organization.usage.completions.result",
                                "project_id": "proj_X",
                                "user_id": "user-X",
                                "api_key_id": "key_X",
                                "model": "gpt-4o-2024-08-06",
                                "batch": None,
                                "service_tier": None,
                                "num_model_requests": 2,
                                "input_tokens": 89,
                                "input_uncached_tokens": 80,
                                "input_cached_tokens": 9,
                                "output_tokens": 123,
                                "input_text_tokens": 89,
                                "output_text_tokens": 123,
                                "input_cached_text_tokens": 9,
                                "input_audio_tokens": 0,
                                "input_cached_audio_tokens": 0,
                                "output_audio_tokens": 0,
                                "input_image_tokens": 0,
                                "input_cached_image_tokens": 0,
                                "output_image_tokens": 0,
                            }
                        ],
                    }
                ],
            },
        )

    with _zero_wait_client(handler) as client:
        buckets = list(
            client.list_usage(
                start_time=START,
                end_time=END,
                group_by=["model", "user_id", "api_key_id", "project_id"],
            )
        )

    assert len(buckets) == 1
    row = buckets[0].results[0]
    assert row.model == "gpt-4o-2024-08-06"
    assert row.user_id == "user-X"
    assert row.api_key_id == "key_X"
    assert row.project_id == "proj_X"
    # The billing split (recon §2.1): uncached + cached == total input.
    assert row.input_uncached_tokens == 80
    assert row.input_cached_tokens == 9
    assert row.input_tokens == row.input_uncached_tokens + row.input_cached_tokens
    assert row.output_tokens == 123
    assert row.num_model_requests == 2


def test_cost_amount_value_parses_scientific_notation_as_decimal() -> None:
    """``amount.value`` in sci-notation parses to an exact Decimal (no float)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "page",
                "has_more": False,
                "next_page": None,
                "data": [
                    {
                        "object": "bucket",
                        "start_time": 1780272000,
                        "end_time": 1780358400,
                        "results": [
                            {
                                "object": "organization.costs.result",
                                "amount": {"value": 1.29e-05, "currency": "usd"},
                                "line_item": "gpt-4o-2024-08-06, input",
                                "project_id": "proj_X",
                                "project_name": "demo",
                            }
                        ],
                    }
                ],
            },
        )

    with _zero_wait_client(handler) as client:
        buckets = list(client.list_costs(start_time=START, end_time=END))

    cost = buckets[0].results[0]
    assert isinstance(cost.amount.value, Decimal)
    # Decimal(str(1.29e-05)) is exact; Decimal(1.29e-05) would carry
    # binary-float noise. Confirm we took the str() path.
    assert cost.amount.value == Decimal("0.0000129")
    assert cost.amount.currency == "usd"
    assert cost.line_item == "gpt-4o-2024-08-06, input"
    assert cost.project_name == "demo"


def test_unknown_fields_absorbed_via_extra_allow() -> None:
    """An unmodeled wire field lands in model_extra rather than crashing parse."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "object": "list",
                "data": [
                    {
                        "id": "proj_a",
                        "name": "demo",
                        "status": "active",
                        "some_future_field": "absorb-me",
                    }
                ],
                "has_more": False,
            },
        )

    with _zero_wait_client(handler) as client:
        projects = list(client.list_projects())

    assert projects[0].id == "proj_a"
    assert projects[0].model_extra.get("some_future_field") == "absorb-me"
