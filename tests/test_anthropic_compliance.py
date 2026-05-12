# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the typed Anthropic Compliance API methods (T5.2).

**Status: best-guess scaffolding tests.** The Pydantic shapes in
``vargate_telemetry/anthropic/types.py`` track the public Compliance
API docs at https://platform.claude.com/docs/en/manage-claude/compliance-*
as of T5.2 authoring, but no cassette has been recorded against the
live API yet. T5.3 ingestion is where real cassettes get recorded;
any drift surfaces here as a Pydantic ValidationError and is fixed by
extending the model or relaxing a type.

All tests use ``httpx.MockTransport`` for deterministic response
sequences — same pattern as ``test_anthropic_admin.py``.

Three endpoint families exercised:

  - ``GET /v1/compliance/activities`` (Activity Feed; both Admin API
    keys and Compliance Access Keys reach it).
  - ``GET /v1/compliance/apps/chats`` (Compliance Access Key only).
  - ``GET /v1/compliance/apps/chats/{chat_id}/messages`` (Compliance
    Access Key only).

The InsufficientScope test pins the 403-surfacing contract used by
the Admin-API-key calling a content endpoint path.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Callable

import httpx
import pytest

from vargate_telemetry.anthropic import (
    Activity,
    AnthropicAdminClient,
    Chat,
    ChatWithMessages,
    InsufficientScope,
)


def _zero_wait_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> AnthropicAdminClient:
    """Build a client wired with MockTransport + zero retry-wait.

    Same construction shape as test_anthropic_admin.py — only the
    handler closure changes per test.
    """
    return AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


# ───────────────────────────────────────────────────────────────────────────
# Sample response payloads (lifted verbatim from the public docs)
# ───────────────────────────────────────────────────────────────────────────


_ACTIVITY_PAGE_1 = {
    "data": [
        {
            "id": "activity_01XyDMpzjS89pFZXqSFUBDr6",
            "created_at": "2026-04-10T08:09:10Z",
            "organization_id": "org_01Wv6QeBcDfGhJkLmNpQrSt8",
            "organization_uuid": "abcdef01-2345-6789-abcd-ef0123456789",
            "actor": {
                "type": "user_actor",
                "email_address": "user@example.com",
                "user_id": "user_01TuVwXyZaBcDeFgH2JkLmN4",
                "ip_address": "192.0.2.34",
                "user_agent": "Mozilla/5.0...",
            },
            "type": "claude_chat_created",
            "claude_chat_id": "claude_chat_01XyDMpzjS89pFZXqSFUBDr6",
            "claude_project_id": "claude_proj_01KGp4eZNug9ri4kE35RSppq",
        },
        {
            "id": "activity_02ZyDMpzjS89pFZXqSFUBDr6",
            "created_at": "2026-04-10T08:09:11Z",
            "organization_id": None,  # sign-in events have null org
            "organization_uuid": None,
            "actor": {
                "type": "unauthenticated_user_actor",
                "unauthenticated_email_address": "newuser@example.com",
                "ip_address": "192.0.2.99",
                "user_agent": "Mozilla/5.0...",
            },
            "type": "sso_login_initiated",
        },
    ],
    "has_more": True,
    "first_id": "activity_01XyDMpzjS89pFZXqSFUBDr6",
    "last_id": "activity_02ZyDMpzjS89pFZXqSFUBDr6",
}


_ACTIVITY_PAGE_2 = {
    "data": [
        {
            "id": "activity_03AyDMpzjS89pFZXqSFUBDr6",
            "created_at": "2026-04-10T08:00:00Z",
            "organization_id": "org_01Wv6QeBcDfGhJkLmNpQrSt8",
            "organization_uuid": "abcdef01-2345-6789-abcd-ef0123456789",
            "actor": {
                "type": "api_actor",
                "api_key_id": "key_01ABCD...",
                "ip_address": "192.0.2.50",
                "user_agent": "MyApp/1.0",
            },
            "type": "claude_file_uploaded",
            "filename": "report.pdf",
        }
    ],
    "has_more": False,
    "first_id": "activity_03AyDMpzjS89pFZXqSFUBDr6",
    "last_id": "activity_03AyDMpzjS89pFZXqSFUBDr6",
}


_CHAT_PAGE = {
    "data": [
        {
            "id": "claude_chat_01H5CWunD7RpVJ5bHa8RCkja",
            "name": "Product Requirements Discussion",
            "created_at": "2026-04-10T08:09:10Z",
            "updated_at": "2026-04-10T09:10:11Z",
            "deleted_at": None,
            "href": "https://claude.ai/chat/abcdef01-2345-6789-abcd-ef0123456789",
            "model": "claude-opus-4-7",
            "organization_id": "org_01Wv6QeBcDfGhJkLmNpQrSt8",
            "organization_uuid": "91012d09-e48b-438e-a489-1bebfd8fa6f9",
            "project_id": "claude_proj_01KGp4eZNug9ri4kE35RSppq",
            "user": {
                "id": "user_01XyDMpzjS89pFZXqSFUBDr6",
                "email_address": "user@example.com",
            },
        }
    ],
    "has_more": False,
    "first_id": "claude_chat_01H5CWunD7RpVJ5bHa8RCkja",
    "last_id": "claude_chat_01H5CWunD7RpVJ5bHa8RCkja",
}


_CHAT_WITH_MESSAGES = {
    "id": "claude_chat_01H5CWunD7RpVJ5bHa8RCkja",
    "name": "Product Requirements Discussion",
    "created_at": "2026-04-10T08:09:10Z",
    "updated_at": "2026-04-10T09:10:11Z",
    "deleted_at": None,
    "href": "https://claude.ai/chat/abcdef01-2345-6789-abcd-ef0123456789",
    "model": "claude-opus-4-7",
    "organization_id": "org_01Wv6QeBcDfGhJkLmNpQrSt8",
    "organization_uuid": "91012d09-e48b-438e-a489-1bebfd8fa6f9",
    "project_id": "claude_proj_01KGp4eZNug9ri4kE35RSppq",
    "user": {
        "id": "user_01XyDMpzjS89pFZXqSFUBDr6",
        "email_address": "user@example.com",
    },
    "chat_messages": [
        {
            "id": "claude_chat_msg_01VnBPkLmtj7YdW5QrXKEA8c",
            "role": "user",
            "created_at": "2026-04-10T08:09:10Z",
            "content": [
                {
                    "type": "text",
                    "text": "Can you help me draft requirements?",
                }
            ],
            "files": [
                {
                    "id": "claude_file_01UaT9wBcDfGhJkLmNpQrSv7",
                    "filename": "dashboard_mockup_v1.pdf",
                    "mime_type": "application/pdf",
                }
            ],
        },
        {
            "id": "claude_chat_msg_01M8tFcHwbQ2kY6NpEjRZv4D",
            "role": "assistant",
            "created_at": "2026-04-10T08:09:11Z",
            "content": [
                {
                    "type": "text",
                    "text": "I'd be happy to help you draft requirements...",
                }
            ],
            "generated_files": None,
            "artifacts": [
                {
                    "id": "claude_artifact_01HqRsTuVwXyZa2BcDeFgH4J",
                    "version_id": "claude_artifact_version_01KmNpQrSt3UvWxYz5AbCdEfG",
                    "title": "Dashboard Requirements Draft",
                    "artifact_type": "text/markdown",
                }
            ],
        },
    ],
    "has_more": False,
    "first_id": "eyJtc2dfdXVpZCI6ICIwZjcwYjA2Ni0uLi4ifQ==",
    "last_id": "eyJtc2dfdXVpZCI6ICJhNGUwYjE3Mi0uLi4ifQ==",
}


# ───────────────────────────────────────────────────────────────────────────
# 1. list_activities returns typed Activity records
# ───────────────────────────────────────────────────────────────────────────


def test_list_activities_returns_typed_results() -> None:
    """Happy path: stub a single-page Activity Feed response, iterate,
    assert each yielded row is a parsed Activity with the right fields
    + the discriminated actor union sub-type."""
    received_requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        return httpx.Response(
            200,
            content=json.dumps(_ACTIVITY_PAGE_2).encode(),
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        activities = list(
            client.list_activities(
                created_at_gte=datetime(2026, 4, 1, tzinfo=timezone.utc),
                activity_types=["claude_chat_created", "claude_file_uploaded"],
                limit=100,
            )
        )
    finally:
        client.close()

    # One page, one row.
    assert len(activities) == 1
    a = activities[0]
    assert isinstance(a, Activity)
    assert a.id == "activity_03AyDMpzjS89pFZXqSFUBDr6"
    assert a.type == "claude_file_uploaded"
    assert a.actor.type == "api_actor"
    assert a.actor.api_key_id == "key_01ABCD..."
    # Type-specific extra field rides along via extra="allow".
    assert a.model_extra is not None
    assert a.model_extra.get("filename") == "report.pdf"

    # The request shape: dotted sub-params + array-bracket repeats.
    assert len(received_requests) == 1
    req = received_requests[0]
    assert str(req.url).startswith("https://api.test/v1/compliance/activities")
    qs = req.url.params
    # Multi-value param round-trips as repeated keys.
    assert sorted(qs.get_list("activity_types[]")) == [
        "claude_chat_created",
        "claude_file_uploaded",
    ]
    assert qs.get("created_at.gte") == "2026-04-01T00:00:00+00:00"
    assert qs.get("limit") == "100"


# ───────────────────────────────────────────────────────────────────────────
# 2. list_activities follows the cursor across pages
# ───────────────────────────────────────────────────────────────────────────


def test_list_activities_paginates_correctly() -> None:
    """Stub a two-page response (page 1: has_more=True; page 2:
    has_more=False). Iterating should yield all rows across both
    pages, and the second request must carry ``after_id=<last_id>``
    from the first page.
    """
    received_requests: list[httpx.Request] = []
    page_responses = [_ACTIVITY_PAGE_1, _ACTIVITY_PAGE_2]

    def handler(request: httpx.Request) -> httpx.Response:
        received_requests.append(request)
        payload = page_responses[len(received_requests) - 1]
        return httpx.Response(
            200,
            content=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        activities = list(client.list_activities())
    finally:
        client.close()

    # Yielded across both pages: 2 + 1 rows.
    assert [a.id for a in activities] == [
        "activity_01XyDMpzjS89pFZXqSFUBDr6",
        "activity_02ZyDMpzjS89pFZXqSFUBDr6",
        "activity_03AyDMpzjS89pFZXqSFUBDr6",
    ]

    # Exactly two HTTP requests, and the second carries the cursor
    # from the first page's last_id.
    assert len(received_requests) == 2
    assert received_requests[0].url.params.get("after_id") is None
    assert (
        received_requests[1].url.params.get("after_id")
        == "activity_02ZyDMpzjS89pFZXqSFUBDr6"
    )


# ───────────────────────────────────────────────────────────────────────────
# 3. list_activities retries on 429 (rate limit)
# ───────────────────────────────────────────────────────────────────────────


def test_list_activities_handles_rate_limit() -> None:
    """First response is 429 with Retry-After=1; second is 200. The
    iterator should yield the second response's rows after the
    tenacity wrapper absorbs the retry.

    `_zero_wait_client` sets min_wait=0 so the tenacity wait is
    short-circuited — the test doesn't actually sleep.
    """
    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                429,
                content=b'{"error": "rate limited"}',
                headers={"retry-after": "1"},
            )
        return httpx.Response(
            200,
            content=json.dumps(_ACTIVITY_PAGE_2).encode(),
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        activities = list(client.list_activities())
    finally:
        client.close()

    assert call_count["n"] == 2  # one retry happened
    assert len(activities) == 1
    assert activities[0].type == "claude_file_uploaded"


# ───────────────────────────────────────────────────────────────────────────
# 4. get_chat_messages returns the typed chat envelope with content
# ───────────────────────────────────────────────────────────────────────────


def test_get_chat_messages_returns_typed_entry() -> None:
    """Happy path for the content endpoint: parse the chat envelope,
    assert the chat metadata is intact and the message list yields
    parsed messages with text content + attachments."""

    def handler(request: httpx.Request) -> httpx.Response:
        # Pin the URL shape: GET /v1/compliance/apps/chats/{id}/messages
        assert request.url.path == (
            "/v1/compliance/apps/chats/"
            "claude_chat_01H5CWunD7RpVJ5bHa8RCkja/messages"
        )
        return httpx.Response(
            200,
            content=json.dumps(_CHAT_WITH_MESSAGES).encode(),
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        chat = client.get_chat_messages(
            "claude_chat_01H5CWunD7RpVJ5bHa8RCkja"
        )
    finally:
        client.close()

    assert isinstance(chat, ChatWithMessages)
    assert chat.id == "claude_chat_01H5CWunD7RpVJ5bHa8RCkja"
    assert chat.model == "claude-opus-4-7"
    assert len(chat.chat_messages) == 2

    user_msg = chat.chat_messages[0]
    assert user_msg.role == "user"
    assert user_msg.content[0].type == "text"
    assert user_msg.content[0].text == (
        "Can you help me draft requirements?"
    )
    assert user_msg.files is not None
    assert user_msg.files[0].filename == "dashboard_mockup_v1.pdf"

    asst_msg = chat.chat_messages[1]
    assert asst_msg.role == "assistant"
    assert asst_msg.generated_files is None  # nullable; spec uses null
    assert asst_msg.artifacts is not None
    assert (
        asst_msg.artifacts[0].version_id
        == "claude_artifact_version_01KmNpQrSt3UvWxYz5AbCdEfG"
    )


# ───────────────────────────────────────────────────────────────────────────
# Bonus 5: list_chats returns typed results (Compliance Access Key path)
# ───────────────────────────────────────────────────────────────────────────


def test_list_chats_returns_typed_results() -> None:
    """The list-chats endpoint requires `user_ids[]`. Stub a happy
    response and verify the request carries the user-id filter and
    the response parses into Chat objects."""
    received: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        received.append(request)
        return httpx.Response(
            200,
            content=json.dumps(_CHAT_PAGE).encode(),
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        chats = list(
            client.list_chats(
                user_ids=["user_01XyDMpzjS89pFZXqSFUBDr6"],
                created_at_gte=datetime(2025, 6, 1, tzinfo=timezone.utc),
                limit=100,
            )
        )
    finally:
        client.close()

    assert len(chats) == 1
    c = chats[0]
    assert isinstance(c, Chat)
    assert c.model == "claude-opus-4-7"
    assert c.user.email_address == "user@example.com"

    # Request shape: user_ids[] is required and goes through
    # array-bracket syntax.
    req = received[0]
    assert req.url.params.get_list("user_ids[]") == [
        "user_01XyDMpzjS89pFZXqSFUBDr6"
    ]
    assert req.url.params.get("created_at.gte") == "2025-06-01T00:00:00+00:00"


def test_list_chats_rejects_empty_user_ids() -> None:
    """The API requires at least one `user_ids[]` value; our wrapper
    enforces that client-side rather than letting the API return a
    400 with a less-helpful message."""
    client = _zero_wait_client(
        lambda req: httpx.Response(500, content=b"unreachable")
    )
    try:
        with pytest.raises(ValueError, match="user_ids is required"):
            list(client.list_chats(user_ids=[]))
    finally:
        client.close()


# ───────────────────────────────────────────────────────────────────────────
# Bonus 6: 403 surfaces as InsufficientScope (watchpoint 3)
# ───────────────────────────────────────────────────────────────────────────


def test_compliance_endpoint_raises_insufficient_scope_on_403() -> None:
    """Calling a content endpoint with an Admin API key returns 403
    per the docs. The client's `_raw_get` translates this into
    `InsufficientScope` (subclass of `AnthropicAPIError`) so T5.3
    ingestion can branch on the typed exception and skip content
    capture without parsing error bodies.

    Also pins: InsufficientScope is NOT a RateLimited, so the
    tenacity retry policy doesn't loop forever on a 403.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            content=b'{"error": {"type": "permission_error", '
            b'"message": "scope read:compliance_user_data required"}}',
            headers={"content-type": "application/json"},
        )

    client = _zero_wait_client(handler)
    try:
        with pytest.raises(InsufficientScope) as exc_info:
            client.get_chat_messages(
                "claude_chat_01H5CWunD7RpVJ5bHa8RCkja"
            )
    finally:
        client.close()

    # Carries the 403 status code and the response body for triage.
    assert exc_info.value.status_code == 403
    assert "permission_error" in exc_info.value.body
