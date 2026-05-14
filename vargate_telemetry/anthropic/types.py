# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic models for the Anthropic Admin API response shapes (T3.2).

**Status: best-guess scaffolding.** Field names and types here reflect
the public Anthropic Admin API conventions as of T3.2 authoring, but
no real cassette has been recorded against the live API yet. T3.x
will record real responses through `tests/_vcr_config.vcr_for_anthropic`
and any shape drift becomes a small one-shot edit: add a Field alias,
adjust an optional default, or extend `model_config.extra="allow"`
absorption coverage. The shape isn't load-bearing for T3.1's
transport tests — it's load-bearing for T3.5's pull task when the
normalized data flows into `telemetry_records`.

Every model sets `extra="allow"` so an unexpected field on the wire
doesn't crash parsing — the Anthropic API may add fields without a
versioned response shape change, and the conservative posture is to
absorb-and-log rather than refuse.

`UsageBreakdown.model` is the Anthropic model name (e.g.
`claude-3-5-sonnet-20250101`). Pydantic 2.x reserves the `model_`
namespace by default; we set `protected_namespaces=()` so the field
keeps its natural name from the wire.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class Member(BaseModel):
    """One organization-member user.

    Endpoint: `GET /v1/organizations/users`. Role values observed on
    the wire: `admin`, `developer`, `billing`, plus newer per-product
    roles like `claude_code_user`. We do not enumerate — `role` is a
    plain string so additions don't require a model bump.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "user"
    email: str
    name: Optional[str] = None
    role: str
    added_at: datetime


class Workspace(BaseModel):
    """One organization workspace.

    Endpoint: `GET /v1/organizations/workspaces`. `archived_at` is
    `None` for active workspaces. `display_color` is the workspace's
    UI swatch as a `#RRGGBB` hex string; absent if never customized.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "workspace"
    name: str
    created_at: datetime
    archived_at: Optional[datetime] = None
    display_color: Optional[str] = None


class ApiKey(BaseModel):
    """One Anthropic API key.

    Endpoint: ``GET /v1/organizations/api_keys``. The usage-report
    `api_key_id` field references rows of this shape — Anthropic does
    NOT return the key name in the usage report, only the id, so the
    UI's "API key — sera-production" rendering requires this fetch
    + an in-Ogma lookup map (TM3 Phase A4).

    `partial_key_hint` is a redacted prefix like "sk-...wxyz" — safe
    to log but useless for end-user display. The `name` field is the
    human-readable label the customer assigned.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "api_key"
    name: str
    status: str  # "active" | "inactive" | "archived" | "expired"
    created_at: datetime
    expires_at: Optional[datetime] = None
    workspace_id: Optional[str] = None
    partial_key_hint: Optional[str] = None


class UsageBreakdown(BaseModel):
    """Tokens used inside one (model, workspace, key, tier, context) slice.

    The Anthropic Admin usage report nests these inside each time
    bucket — one row per distinct (model, workspace_id, api_key_id,
    service_tier, context_window) combination. Optional fields are
    Optional because earlier API versions omitted them or because
    filtering can collapse some dimensions.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    # T3.7 smoke against the real Anthropic API revealed `model` is
    # `None` for some breakdown rows (likely aggregate / non-model-tagged
    # entries). Allowing None here matches the wire reality; consumers
    # of UsageBreakdown that need a string fall back to a sentinel.
    model: Optional[str] = None
    workspace_id: Optional[str] = None
    api_key_id: Optional[str] = None
    service_tier: Optional[str] = None
    context_window: Optional[str] = None
    input_tokens: int = Field(0, alias="uncached_input_tokens")
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


class UsageBucket(BaseModel):
    """One time-bucketed usage row.

    Endpoint: `GET /v1/organizations/usage_report/messages`. Each row
    covers `[starting_at, ending_at)` and carries `results` — the
    per-dimension breakdowns within that window. Buckets are typically
    daily (`bucket_width=1d`) or hourly (`bucket_width=1h`); finer
    granularities may exist.
    """

    model_config = ConfigDict(extra="allow")

    starting_at: datetime
    ending_at: datetime
    results: list[UsageBreakdown] = Field(default_factory=list)


# ───────────────────────────────────────────────────────────────────────────
# Compliance API (T5.2) — Activity Feed + chat content
#
# **Best-guess scaffolding** — refine against real cassettes during T5.3
# ingestion. Field shapes track the public Compliance API docs at
# https://platform.claude.com/docs/en/manage-claude/compliance-* as of
# T5.2 authoring (May 2026). Every model sets `extra="allow"` so the
# Anthropic team can add fields without breaking parsing.
#
# Two endpoint families are modeled here:
#
#   1. Activity Feed (`GET /v1/compliance/activities`). Reachable by
#      both Admin API keys and Compliance Access Keys. Returns event
#      metadata — NOT chat content. The `Activity.type` enum is open
#      (hundreds of values; type-specific extra fields like
#      `claude_chat_id` or `filename` ride along via `extra="allow"`).
#   2. Chat content (`GET /v1/compliance/apps/chats/*`). Requires a
#      Compliance Access Key with `read:compliance_user_data` scope.
#      Returns full chat history including prompt/response text. T5.x
#      onboarding must collect a Compliance Access Key in addition to
#      the Admin API key for this path to be usable.
#
# The actor union is modeled as a single `Actor` class rather than six
# discriminated subclasses — the field set across the six actor types is
# small enough that a flat all-Optional model is more readable than the
# discriminated-union machinery would be at this scope.
# ───────────────────────────────────────────────────────────────────────────


class Actor(BaseModel):
    """The principal that performed an activity or analytics-recorded action.

    Endpoint contexts:
      - ``Activity.actor`` (Compliance Activity Feed, T5.2).
      - ``CodeAnalyticsRecord.actor`` (Code Analytics, T5.4).

    The ``type`` field is the discriminator. Known values:

      - ``user_actor`` (Activity Feed: ``email_address`` + ``user_id`` +
        ``ip_address`` + ``user_agent``; Code Analytics: just
        ``email_address``).
      - ``api_actor`` (Activity Feed: ``api_key_id`` + ``ip_address`` +
        ``user_agent``; Code Analytics: ``api_key_name``).
      - ``admin_api_key_actor`` (Activity Feed only).
      - ``unauthenticated_user_actor`` (Activity Feed only).
      - ``anthropic_actor`` (Activity Feed only).
      - ``scim_directory_sync_actor`` (Activity Feed only).
      - ...plus anything Anthropic adds later via ``extra="allow"``.

    Field-set varies across the union (e.g., Code Analytics' api_actor
    uses ``api_key_name`` while Activity Feed's api_actor uses
    ``api_key_id``). All modeled here as Optional so a single parser
    handles every variant — the caller branches on ``.type``.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    # Common (user_actor, unauthenticated_user_actor, api_actor)
    email_address: Optional[str] = None
    ip_address: Optional[str] = None
    user_agent: Optional[str] = None
    # user_actor
    user_id: Optional[str] = None
    # api_actor (Activity Feed)
    api_key_id: Optional[str] = None
    # api_actor (Code Analytics — uses NAME, not ID, on this endpoint)
    api_key_name: Optional[str] = None
    # admin_api_key_actor
    admin_api_key_id: Optional[str] = None
    # unauthenticated_user_actor
    unauthenticated_email_address: Optional[str] = None
    # scim_directory_sync_actor
    workos_event_id: Optional[str] = None
    directory_id: Optional[str] = None
    idp_connection_type: Optional[str] = None


class Activity(BaseModel):
    """One Activity Feed record.

    Endpoint: ``GET /v1/compliance/activities``. The ``type`` field is
    an open enum — hundreds of values like ``claude_chat_created``,
    ``claude_file_uploaded``, ``sso_login_initiated``, etc. Type-specific
    extra fields (``claude_chat_id``, ``claude_project_id``,
    ``filename``, ``claude_artifact_id``, ...) ride along via
    ``extra="allow"`` rather than being modeled per-type — T5.3's
    ingestion pipeline can branch on ``type`` and read
    ``model_extra`` for the variant fields.

    ``organization_id`` / ``organization_uuid`` are nullable: events
    not tied to an organization (sign-in/out, Compliance API calls
    themselves) have both as ``None``.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    created_at: datetime
    organization_id: Optional[str] = None
    organization_uuid: Optional[str] = None
    actor: Actor
    type: str


class ChatUser(BaseModel):
    """The embedded user reference on a Chat.

    Always present (chats have an owner). Stripped down compared to the
    org users endpoint — just id + email here.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    email_address: str


class Chat(BaseModel):
    """One chat metadata record from the list-chats endpoint.

    Endpoint: ``GET /v1/compliance/apps/chats?user_ids[]=...``.
    Returns metadata only — message content is fetched separately via
    ``GET /v1/compliance/apps/chats/{chat_id}/messages`` (modeled as
    ``ChatWithMessages`` below).

    ``deleted_at`` is non-null for chats soft-deleted in claude.ai but
    still visible via the Compliance API. Hard-deleted chats stop
    appearing in the list entirely.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    id: str
    name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    href: Optional[str] = None
    # Pydantic 2.x reserves the `model_` namespace; the wire field is
    # literally `model` (the claude model name like
    # `claude-opus-4-7`), so we need `protected_namespaces=()`.
    model: Optional[str] = None
    organization_id: Optional[str] = None
    organization_uuid: Optional[str] = None
    project_id: Optional[str] = None
    user: ChatUser


class MessageContentBlock(BaseModel):
    """One content block on a chat message.

    Today the only documented block type is ``text`` (with a ``text``
    field carrying the plaintext). Future types — tool_use, tool_result,
    image attachments, structured content — ride along via
    ``extra="allow"``; T5.3 will refine when real cassettes surface
    additional shapes.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    text: Optional[str] = None


class MessageFile(BaseModel):
    """A user-uploaded file attached to a chat message.

    ``id`` starts with ``claude_file_``. Download bytes via
    ``GET /v1/compliance/apps/chats/files/{id}/content`` (not modeled
    yet — that's a streaming binary endpoint, T5.3 may add when needed).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    filename: Optional[str] = None
    mime_type: Optional[str] = None


class MessageGeneratedFile(BaseModel):
    """A Claude-generated file produced via tool use on a chat message.

    ``id`` starts with ``claude_gen_file_``. Download bytes via
    ``GET /v1/compliance/apps/chats/generated_files/{id}/content``.
    Distinct from ``MessageFile`` (which is a user upload).
    """

    model_config = ConfigDict(extra="allow")

    id: str
    filename: Optional[str] = None
    mime_type: Optional[str] = None


class MessageArtifact(BaseModel):
    """A Claude-generated artifact (structured doc inside a chat).

    Pass ``version_id`` — not ``id`` — to the artifact content endpoint
    to fetch the version's bytes. Each artifact version is immutable;
    a new version_id is minted when the artifact is updated.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    version_id: str
    title: Optional[str] = None
    artifact_type: Optional[str] = None


class ChatMessage(BaseModel):
    """One message in a chat.

    ``role`` is ``user`` or ``assistant`` (system messages don't appear
    here — they're carried as project instructions or model defaults).
    For user messages, ``created_at`` is when the user sent the message;
    for assistant messages, when Claude finished generating it.

    ``content`` is always an array of blocks (even for a single-block
    text response — the array shape gives forward compat with multi-block
    responses, tool use, etc.).

    ``files`` / ``generated_files`` / ``artifacts`` are nullable on a
    given message; the spec uses `null` not `[]` for the empty case.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    role: str
    created_at: datetime
    content: list[MessageContentBlock] = Field(default_factory=list)
    files: Optional[list[MessageFile]] = None
    generated_files: Optional[list[MessageGeneratedFile]] = None
    artifacts: Optional[list[MessageArtifact]] = None


class ChatWithMessages(BaseModel):
    """The envelope returned by ``GET /v1/compliance/apps/chats/{id}/messages``.

    Same top-level chat metadata as ``Chat``, plus a ``chat_messages``
    array and pagination cursors for very long chats (``after_id`` /
    ``before_id`` advance through messages within one chat).
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    id: str
    name: Optional[str] = None
    created_at: datetime
    updated_at: Optional[datetime] = None
    deleted_at: Optional[datetime] = None
    href: Optional[str] = None
    model: Optional[str] = None
    organization_id: Optional[str] = None
    organization_uuid: Optional[str] = None
    project_id: Optional[str] = None
    user: ChatUser
    chat_messages: list[ChatMessage] = Field(default_factory=list)
    has_more: bool = False
    first_id: Optional[str] = None
    last_id: Optional[str] = None


# ───────────────────────────────────────────────────────────────────────────
# Claude Code Analytics API (T5.4) — daily per-user metrics
#
# **Best-guess scaffolding** — refine against real cassettes / probes
# during T5.4+ ingest. Field shapes track the public docs at
# https://platform.claude.com/docs/en/build-with-claude/claude-code-analytics-api
# as of T5.4 authoring (May 2026).
#
# Endpoint: ``GET /v1/organizations/usage_report/claude_code``. Lives in
# the Admin API namespace (NOT Compliance API) — reachable by the same
# Admin API key our tenants already provision. Per the docs: "free to
# use for all organizations with access to the Admin API" — NOT plan-
# gated like Activity Feed.
#
# Pagination: opaque ``page`` token (NOT after_id cursors). Response
# carries ``data``, ``has_more``, ``next_page``. The existing
# ``client.paginate()`` already implements this scheme; T5.4 reuses it.
#
# Shape decisions (per the flat-model-with-extra-allow rule):
#   - Top-level ``CodeAnalyticsRecord`` models the documented fields;
#     nested objects (``core_metrics``, ``tool_actions``,
#     ``model_breakdown``) ride along as raw dicts via ``extra="allow"``
#     rather than per-class subtypes. T5.5+ dashboards branch into
#     these dicts directly; nothing in ingest reads the nested shape.
#   - That keeps "new metric type" friendly: a future
#     ``review_tool.accepted/rejected`` lands in the existing
#     ``tool_actions`` dict without a parser change.
# ───────────────────────────────────────────────────────────────────────────


class CodeAnalyticsRecord(BaseModel):
    """One (actor, day) record from the Code Analytics endpoint.

    Each record represents one user's (or one API key's) activity for
    the day specified by the request's ``starting_at``. Daily
    aggregation — no event-stream granularity.

    Documented top-level fields are modeled here. The nested objects
    (``core_metrics``, ``tool_actions``, ``model_breakdown``) ride
    along via ``extra="allow"`` so per-tool-type and per-model
    additions absorb without a parser change.
    """

    model_config = ConfigDict(extra="allow")

    # `date` is the day's UTC midnight timestamp (RFC 3339).
    date: datetime
    # The principal — same flat Actor model as the Compliance Activity
    # Feed. For Code Analytics the union has only `user_actor` and
    # `api_actor`; for the latter the field is `api_key_name` (NOT
    # `api_key_id` like Activity Feed).
    actor: Actor
    organization_id: Optional[str] = None
    # `api` for pay-as-you-go API customers, `subscription` for
    # Pro/Team plans. Useful for billing-side breakdowns.
    customer_type: Optional[str] = None
    # Terminal/editor identifier (`vscode`, `iTerm.app`, `tmux`, ...).
    # Open enum — new editors absorb via the string type.
    terminal_type: Optional[str] = None
    # The three nested metric objects live in `model_extra` via
    # `extra="allow"`. T5.4 ingest stores the full record JSON in
    # `telemetry_records.record_metadata`; T5.5 dashboards read the
    # nested structure from there.
