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

from datetime import datetime
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
    InsufficientScope,
    RateLimited,
)
from vargate_telemetry.anthropic.types import (
    Activity,
    Chat,
    ChatWithMessages,
    Member,
    UsageBucket,
    Workspace,
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
        self,
        path: str,
        params: Optional[Any] = None,
    ) -> httpx.Response:
        """One HTTP GET; raises typed exceptions on 403 / 429 / 5xx.

        - 403 → ``InsufficientScope`` (the supplied key lacks the
          scope this endpoint requires; T5.2 added this for the
          Compliance API content endpoints that only Compliance
          Access Keys can reach).
        - 429 → ``RateLimited`` with the ``Retry-After`` value.
        - 5xx → ``AnthropicAPIError`` (non-retryable here; the
          tenacity wrapper retries only RateLimited).
        - Other 4xx → ``httpx.HTTPStatusError`` from
          ``raise_for_status()``.
        """
        r = self._client.get(path, params=params)
        if r.status_code == 403:
            raise InsufficientScope(r.text)
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

    def _get(self, path: str, params: Optional[Any] = None) -> dict:
        """GET with tenacity-backed retry on 429. Returns parsed JSON.

        ``params`` accepts either ``dict`` (single-value keys; standard
        Admin API) or ``list[tuple[str, str]]`` (repeatable keys for
        the Compliance API's ``activity_types[]`` etc.).
        """
        retryer = self._retrying()
        response = retryer(self._raw_get, path, params)
        return response.json()

    def paginate(
        self, path: str, params: Optional[dict] = None
    ) -> Iterator[dict]:
        """Yield rows from `data` across pages — `next_page` cursor variant.

        Kept from T3.1 for any endpoint that uses a `next_page` cursor
        on the envelope. The Anthropic Admin API uses a different
        pattern (see `_paginate_admin`); use that for admin endpoints.
        """
        p = dict(params or {})
        while True:
            data = self._get(path, p)
            yield from data.get("data", [])
            cursor = data.get("next_page")
            if not cursor:
                return
            p["page"] = cursor

    def _paginate_admin(
        self, path: str, params: Optional[dict] = None
    ) -> Iterator[dict]:
        """Yield rows from `data` across pages — Anthropic Admin variant.

        Pagination contract: each response envelope carries `data`,
        `has_more`, `first_id`, and `last_id`. When `has_more` is true,
        the next request adds `after_id=<last_id>` to advance. Stops
        when `has_more` is false or `last_id` is missing.

        T3.2: shape is best-guess scaffolding. T3.x's first real
        cassette recording will confirm; mismatches show up here as
        either an early `return` (we stop before all pages) or an
        infinite loop (we fail to advance). Test
        `test_paginate_admin_advances_after_id` pins the contract.
        """
        p = dict(params or {})
        while True:
            envelope = self._get(path, p)
            yield from envelope.get("data", [])
            if not envelope.get("has_more"):
                return
            last_id = envelope.get("last_id")
            if last_id is None:
                return
            p["after_id"] = last_id

    def list_workspaces(self) -> Iterator[Workspace]:
        """Yield every workspace in the organization."""
        for raw in self._paginate_admin("/v1/organizations/workspaces"):
            yield Workspace.model_validate(raw)

    def list_members(self) -> Iterator[Member]:
        """Yield every member user in the organization."""
        for raw in self._paginate_admin("/v1/organizations/users"):
            yield Member.model_validate(raw)

    def list_usage(
        self,
        *,
        starting_at: datetime,
        ending_at: datetime,
        bucket_width: str = "1d",
    ) -> Iterator[UsageBucket]:
        """Yield time-bucketed usage rows for `[starting_at, ending_at)`.

        `bucket_width` is the per-bucket granularity. Common values:
        `1d` (daily, the default) and `1h` (hourly). T3.5's scheduled
        pull task fans these out per tenant into `telemetry_records`.
        """
        params = {
            "starting_at": starting_at.isoformat(),
            "ending_at": ending_at.isoformat(),
            "bucket_width": bucket_width,
        }
        for raw in self._paginate_admin(
            "/v1/organizations/usage_report/messages", params
        ):
            yield UsageBucket.model_validate(raw)

    # ───────────────────────────────────────────────────────────────────
    # Compliance API (T5.2) — Activity Feed + chat content
    #
    # Two endpoint families with different key requirements:
    #
    #   - Activity Feed (``/v1/compliance/activities``) — reachable by
    #     both Admin API keys (``sk-ant-admin01-...``) and Compliance
    #     Access Keys (``sk-ant-api01-...``) carrying the
    #     ``read:compliance_activities`` scope.
    #   - Content endpoints (``/v1/compliance/apps/chats/*``) — require
    #     a Compliance Access Key with ``read:compliance_user_data``.
    #     An Admin API key against these endpoints returns 403, which
    #     ``_raw_get`` surfaces as ``InsufficientScope``.
    #
    # Plan gating: Enterprise only. Pro / Team / individual plans
    # don't reach this surface at all.
    # ───────────────────────────────────────────────────────────────────

    def _build_compliance_query(
        self,
        *,
        created_at_gte: Optional[datetime] = None,
        created_at_gt: Optional[datetime] = None,
        created_at_lte: Optional[datetime] = None,
        created_at_lt: Optional[datetime] = None,
        activity_types: Optional[list[str]] = None,
        actor_ids: Optional[list[str]] = None,
        organization_ids: Optional[list[str]] = None,
        user_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> list[tuple[str, str]]:
        """Construct the query-param list for a Compliance endpoint.

        The Compliance API uses two non-standard query shapes:

          - **Dotted sub-parameters** for date ranges:
            ``created_at.gte=2026-04-01T00:00:00Z``. Pass datetime values
            as ISO 8601.
          - **Array-bracket syntax** for repeatable filters:
            ``activity_types[]=x&activity_types[]=y``. Each value gets
            its own (key, value) tuple.

        httpx accepts a list of ``(key, value)`` tuples in ``params=``
        and renders both shapes correctly. Returning a list (not a
        dict) preserves duplicate keys for array-bracket params.
        """
        params: list[tuple[str, str]] = []
        if created_at_gte is not None:
            params.append(("created_at.gte", created_at_gte.isoformat()))
        if created_at_gt is not None:
            params.append(("created_at.gt", created_at_gt.isoformat()))
        if created_at_lte is not None:
            params.append(("created_at.lte", created_at_lte.isoformat()))
        if created_at_lt is not None:
            params.append(("created_at.lt", created_at_lt.isoformat()))
        for v in activity_types or ():
            params.append(("activity_types[]", v))
        for v in actor_ids or ():
            params.append(("actor_ids[]", v))
        for v in organization_ids or ():
            params.append(("organization_ids[]", v))
        for v in user_ids or ():
            params.append(("user_ids[]", v))
        for v in project_ids or ():
            params.append(("project_ids[]", v))
        if limit is not None:
            params.append(("limit", str(limit)))
        return params

    def list_activities(
        self,
        *,
        created_at_gte: Optional[datetime] = None,
        created_at_gt: Optional[datetime] = None,
        created_at_lte: Optional[datetime] = None,
        created_at_lt: Optional[datetime] = None,
        activity_types: Optional[list[str]] = None,
        actor_ids: Optional[list[str]] = None,
        organization_ids: Optional[list[str]] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Activity]:
        """Yield activity records from ``GET /v1/compliance/activities``.

        Filters compose with AND. Date bounds are RFC 3339 ISO 8601;
        ``activity_types[]`` / ``actor_ids[]`` / ``organization_ids[]``
        are repeatable.

        Pagination is cursor-based (``after_id`` / ``has_more``) —
        same scheme as the Admin API, so ``_paginate_admin`` is
        reusable. Activities return newest-first; advancing with
        ``last_id`` walks backward through time.

        Available to both Admin API keys and Compliance Access Keys
        with ``read:compliance_activities`` scope. A key without the
        scope raises ``InsufficientScope``; a key with no plan access
        (Pro/Team/individual) returns 403 too.
        """
        params = self._build_compliance_query(
            created_at_gte=created_at_gte,
            created_at_gt=created_at_gt,
            created_at_lte=created_at_lte,
            created_at_lt=created_at_lt,
            activity_types=activity_types,
            actor_ids=actor_ids,
            organization_ids=organization_ids,
            limit=limit,
        )
        # `_paginate_admin` expects a dict (or None). httpx accepts a
        # list-of-tuples in `params=` for repeated keys — we go through
        # the lower-level pager to thread the list shape, since dicts
        # collapse duplicate keys.
        yield from self._paginate_compliance_typed(
            "/v1/compliance/activities", params, Activity
        )

    def list_chats(
        self,
        *,
        user_ids: list[str],
        organization_ids: Optional[list[str]] = None,
        project_ids: Optional[list[str]] = None,
        created_at_gte: Optional[datetime] = None,
        created_at_lte: Optional[datetime] = None,
        limit: Optional[int] = None,
    ) -> Iterator[Chat]:
        """Yield chat metadata records from
        ``GET /v1/compliance/apps/chats``.

        Requires a Compliance Access Key with
        ``read:compliance_user_data`` scope. Admin API keys raise
        ``InsufficientScope``.

        ``user_ids`` is required (the API rejects a list-chats call
        with no user filter — enumerate via ``list_members`` /
        ``list_organization_users`` first). Up to 10 user IDs per
        request per the docs.

        Returns metadata only — fetch message content via
        ``get_chat_messages(chat_id)``.
        """
        if not user_ids:
            raise ValueError(
                "user_ids is required by the Compliance API "
                "list-chats endpoint (enumerate via list_members first)"
            )
        params = self._build_compliance_query(
            user_ids=user_ids,
            organization_ids=organization_ids,
            project_ids=project_ids,
            created_at_gte=created_at_gte,
            created_at_lte=created_at_lte,
            limit=limit,
        )
        yield from self._paginate_compliance_typed(
            "/v1/compliance/apps/chats", params, Chat
        )

    def get_chat_messages(self, chat_id: str) -> ChatWithMessages:
        """Fetch a single chat with its full message content.

        Endpoint: ``GET /v1/compliance/apps/chats/{chat_id}/messages``.
        Returns the chat metadata + the ``chat_messages`` array with
        each message's content blocks, attached files, generated
        files, and artifacts.

        T5.3's ingestion path is the target caller: for each chat
        discovered via ``list_chats``, fetch the messages, extract the
        text content, encrypt under the tenant DEK via
        ``vargate_telemetry.storage.content.store_content``, and append
        a ``telemetry_record`` with the resulting ``content_ref`` and
        ``content_hash``.

        For very long chats the response can be paginated within the
        single chat via ``after_id`` / ``before_id``; the current
        signature returns whatever fits in one response (default limit
        per the docs). T5.x can add cursor-walking if a real chat
        exceeds the response cap.

        Requires a Compliance Access Key with
        ``read:compliance_user_data`` scope. Admin API keys raise
        ``InsufficientScope``.
        """
        if not chat_id:
            raise ValueError("chat_id required")
        raw = self._get(f"/v1/compliance/apps/chats/{chat_id}/messages")
        return ChatWithMessages.model_validate(raw)

    def _paginate_compliance_typed(
        self,
        path: str,
        params: list[tuple[str, str]],
        model_cls: type,
    ) -> Iterator[Any]:
        """Cursor-paginate the Compliance API and yield typed rows.

        Same ``after_id`` / ``has_more`` / ``last_id`` envelope shape
        as ``_paginate_admin``, but built on top of a list-of-tuples
        params list so array-bracket repeated keys survive (a dict
        would collapse them). Each ``data[]`` element is
        ``model_cls.model_validate``'d before yielding.
        """
        cursor: Optional[str] = None
        while True:
            page_params = list(params)
            if cursor is not None:
                page_params.append(("after_id", cursor))
            envelope = self._get(path, page_params)
            for raw in envelope.get("data", []):
                yield model_cls.model_validate(raw)
            if not envelope.get("has_more"):
                return
            cursor = envelope.get("last_id")
            if cursor is None:
                return
