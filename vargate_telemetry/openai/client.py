# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""OpenAI Admin API client (TM8 Phase B).

Single class — ``OpenAIAdminClient`` — that mirrors
``anthropic/client.py`` so the parallel pull tasks share one mental
model:

  - Bearer auth header (``Authorization: Bearer sk-admin-…``)
  - Tenacity-backed retry on ``RateLimited`` (429), exponential backoff
    honoring the server's ``Retry-After`` hint (recon §6: OpenAI emits no
    ``x-ratelimit-*`` headers, so retry-after defaults when absent)
  - Immediate ``OpenAIAPIError`` on 5xx, ``InsufficientScope`` on 403
  - BOTH pagination styles the recon found (§6):
      * usage / costs → top-level ``has_more`` + opaque ``next_page``
        cursor, advanced as ``page=<next_page>``
      * lists (audit_logs / projects / users / project api_keys) →
        ``first_id`` / ``last_id`` / ``has_more``, advanced as
        ``after=<last_id>``

``base_url`` defaults to ``https://api.openai.com/v1/organization`` so
the typed methods pass short relative paths (``/usage/completions``,
``/costs``, ``/audit_logs``, …). ``transport=`` and ``min_wait=`` are
constructor-injectable exactly as on the Anthropic client so tests pass
``httpx.MockTransport(handler)`` + ``min_wait=0`` for deterministic,
wait-free runs.

``group_by`` is sent as repeated query params (``group_by=a&group_by=b``)
— recon §7 / §2 require ``group_by=model,user_id,api_key_id,project_id``
on every usage pull to get the per-row grain we normalize on.
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

from vargate_telemetry.openai.exceptions import (
    InsufficientScope,
    OpenAIAPIError,
    RateLimited,
)
from vargate_telemetry.openai.types import (
    AuditLogEntry,
    CostBucket,
    OrgUser,
    Project,
    ProjectApiKey,
    UsageBucket,
)


class OpenAIAdminClient:
    """HTTP client for the OpenAI Admin (organization) API.

    Thread-safe under httpx's connection-pool semantics. Caller owns
    ``close()`` (or a ``with`` block) so the underlying ``httpx.Client``
    releases its pool and the plaintext key on its headers.
    """

    DEFAULT_BASE_URL = "https://api.openai.com/v1/organization"
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
                "Authorization": f"Bearer {api_key}",
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

    def __enter__(self) -> "OpenAIAdminClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    # ── transport ──────────────────────────────────────────────────────

    def _retrying(self) -> Retrying:
        """Build a per-call Retrying instance from the policy params.

        Fresh instance per call so successive ``_get`` invocations get
        independent attempt budgets. ``wait_none()`` short-circuits the
        backoff entirely when ``min_wait=0`` — used by tests.
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

        - 403 → ``InsufficientScope`` (key lacks access; the pull task
          soft-skips).
        - 429 → ``RateLimited`` with the ``Retry-After`` value (defaults
          to ``DEFAULT_RETRY_AFTER_SECONDS`` when the header is absent —
          OpenAI does not always send one, recon §6).
        - 5xx → ``OpenAIAPIError`` (non-retryable here; the tenacity
          wrapper retries only ``RateLimited``).
        - Other 4xx → ``httpx.HTTPStatusError`` from ``raise_for_status()``.
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
            raise OpenAIAPIError(r.status_code, r.text)
        r.raise_for_status()
        return r

    def _get(self, path: str, params: Optional[Any] = None) -> dict:
        """GET with tenacity-backed retry on 429. Returns parsed JSON.

        ``params`` accepts either ``dict`` (single-value keys) or
        ``list[tuple[str, str]]`` (repeated keys for ``group_by``). httpx
        renders the list-of-tuples as repeated query params, which is
        OpenAI's ``group_by=a&group_by=b`` form.
        """
        retryer = self._retrying()
        response = retryer(self._raw_get, path, params)
        return response.json()

    # ── pagination ─────────────────────────────────────────────────────

    def _paginate_page(
        self,
        path: str,
        params: Optional[list[tuple[str, str]]] = None,
        *,
        initial_page: Optional[str] = None,
    ) -> Iterator[dict]:
        """Yield ``data`` rows across pages — usage/costs ``next_page`` style.

        Recon §6: usage and costs return ``{has_more, next_page}`` on the
        envelope. When ``has_more`` is true (or ``next_page`` is present),
        the next request carries ``page=<next_page>``. Built on a
        list-of-tuples params list so repeated ``group_by`` keys survive
        (a dict would collapse them).

        Stops when ``has_more`` is false / absent and ``next_page`` is
        missing. We treat an absent ``next_page`` as terminal even if a
        stray ``has_more=true`` slips through, so a malformed envelope
        can't spin forever.
        """
        base = list(params or [])
        page_cursor: Optional[str] = initial_page
        while True:
            page_params = list(base)
            if page_cursor is not None:
                page_params.append(("page", page_cursor))
            envelope = self._get(path, page_params)
            yield from envelope.get("data", [])
            next_page = envelope.get("next_page")
            if not envelope.get("has_more") or not next_page:
                return
            page_cursor = next_page

    def _paginate_list(
        self, path: str, params: Optional[list[tuple[str, str]]] = None
    ) -> Iterator[dict]:
        """Yield ``data`` rows across pages — list ``after``/``last_id`` style.

        Recon §6: audit_logs / projects / users / project api_keys return
        ``{first_id, last_id, has_more}``. When ``has_more`` is true, the
        next request carries ``after=<last_id>``. Stops when ``has_more``
        is false or ``last_id`` is missing (the latter guards against an
        infinite loop on a malformed envelope).
        """
        base = list(params or [])
        after: Optional[str] = None
        while True:
            page_params = list(base)
            if after is not None:
                page_params.append(("after", after))
            envelope = self._get(path, page_params)
            yield from envelope.get("data", [])
            if not envelope.get("has_more"):
                return
            last_id = envelope.get("last_id")
            if last_id is None:
                return
            after = last_id

    # ── usage / costs ──────────────────────────────────────────────────

    def list_usage(
        self,
        *,
        modality: str = "completions",
        start_time: datetime,
        end_time: datetime,
        bucket_width: str = "1d",
        group_by: Optional[list[str]] = None,
        limit: int = 31,
        page: Optional[str] = None,
    ) -> Iterator[UsageBucket]:
        """Yield time-bucketed usage rows for ``[start_time, end_time)``.

        ``modality`` selects the endpoint family — ``"completions"``
        (default) or ``"embeddings"`` (recon §1: structurally identical
        bucket/result envelope). Any future ``/usage/<modality>`` works
        without a code change.

        ``start_time`` / ``end_time`` are sent as Unix-epoch seconds (the
        API's documented form). ``group_by`` is sent as repeated
        ``group_by=`` params; pass
        ``["model", "user_id", "api_key_id", "project_id"]`` to get the
        per-row grain Ogma normalizes on (recon §7). Passing ``None``
        sends no grouping (one aggregate row per bucket).

        ``limit`` is the bucket count per page (recon §6). ``page`` seeds
        the first request's cursor for resumed pulls; normal callers
        leave it ``None`` and let the paginator walk ``next_page``.

        Pagination: top-level ``has_more`` + ``next_page`` → ``page=``.
        403 → ``InsufficientScope``; 429 retried; 5xx → ``OpenAIAPIError``.
        """
        params: list[tuple[str, str]] = [
            ("start_time", _epoch(start_time)),
            ("end_time", _epoch(end_time)),
            ("bucket_width", bucket_width),
            ("limit", str(limit)),
        ]
        for dim in group_by or ():
            params.append(("group_by", dim))
        for raw in self._paginate_page(
            f"/usage/{modality}", params, initial_page=page
        ):
            yield UsageBucket.model_validate(raw)

    def list_costs(
        self,
        *,
        start_time: datetime,
        end_time: datetime,
        group_by: Optional[list[str]] = None,
        bucket_width: str = "1d",
        limit: int = 31,
        page: Optional[str] = None,
    ) -> Iterator[CostBucket]:
        """Yield time-bucketed cost rows for ``[start_time, end_time)``.

        Recon §3: authoritative billed spend at ``project_id`` /
        ``line_item`` grain — **no ``user_id``**. ``bucket_width`` is
        ``1d`` only (recon §6); kept as a kwarg for symmetry but other
        values are rejected by the API. The ``/costs`` endpoint is slow
        (~5 s observed, recon §5) — the pull task sizes its timeout
        accordingly.

        ``group_by`` (e.g. ``["project_id", "line_item"]``) is sent as
        repeated ``group_by=`` params. Pagination: ``has_more`` +
        ``next_page`` → ``page=`` (same as usage).
        """
        params: list[tuple[str, str]] = [
            ("start_time", _epoch(start_time)),
            ("end_time", _epoch(end_time)),
            ("bucket_width", bucket_width),
            ("limit", str(limit)),
        ]
        for dim in group_by or ():
            params.append(("group_by", dim))
        for raw in self._paginate_page(
            "/costs", params, initial_page=page
        ):
            yield CostBucket.model_validate(raw)

    # ── lists (after/last_id cursor) ─────────────────────────────────────

    def list_audit_logs(
        self,
        *,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> Iterator[AuditLogEntry]:
        """Yield audit-log entries (``GET /audit_logs``).

        Recon §1/§8: returns ``200`` but is **empty below Enterprise**
        (accessible ≠ populated) — the pull task treats an empty result
        as normal and advances its cursor. Pagination: ``first_id`` /
        ``last_id`` / ``has_more`` → ``after=<last_id>``. ``after`` seeds
        a resumed pull from a stored cursor.
        """
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if after is not None:
            params.append(("after", after))
        for raw in self._paginate_list("/audit_logs", params):
            yield AuditLogEntry.model_validate(raw)

    def list_projects(
        self,
        *,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> Iterator[Project]:
        """Yield organization projects (``GET /projects``).

        Feeds the ``openai_projects`` side table. Pagination:
        ``after=<last_id>`` (recon §4).
        """
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if after is not None:
            params.append(("after", after))
        for raw in self._paginate_list("/projects", params):
            yield Project.model_validate(raw)

    def list_users(
        self,
        *,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> Iterator[OrgUser]:
        """Yield organization users (``GET /users``).

        **Exposes ``email`` (PII)** — the cross-vendor user-match key.
        Feeds the ``openai_users`` side table. Pagination:
        ``after=<last_id>`` (recon §4).
        """
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if after is not None:
            params.append(("after", after))
        for raw in self._paginate_list("/users", params):
            yield OrgUser.model_validate(raw)

    def list_project_api_keys(
        self,
        project_id: str,
        *,
        after: Optional[str] = None,
        limit: int = 100,
    ) -> Iterator[ProjectApiKey]:
        """Yield API keys for one project (``GET /projects/{id}/api_keys``).

        Feeds the ``openai_api_keys`` side table; ``redacted_value`` is
        masked by OpenAI. Pagination: ``after=<last_id>`` (recon §4).
        """
        if not project_id:
            raise ValueError("project_id required")
        params: list[tuple[str, str]] = [("limit", str(limit))]
        if after is not None:
            params.append(("after", after))
        for raw in self._paginate_list(
            f"/projects/{project_id}/api_keys", params
        ):
            yield ProjectApiKey.model_validate(raw)


def _epoch(dt: datetime) -> str:
    """Serialize a datetime to a Unix-epoch-second string for the query.

    OpenAI's usage/costs endpoints take ``start_time`` / ``end_time`` as
    integer Unix seconds (recon §1). ``int(dt.timestamp())`` honors the
    datetime's tzinfo; a naive datetime is interpreted in local time by
    ``.timestamp()`` — callers pass tz-aware UTC datetimes (the pull
    tasks build them with ``timezone.utc``).
    """
    return str(int(dt.timestamp()))
