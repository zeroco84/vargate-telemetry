# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Pydantic models for the OpenAI Admin API response shapes (TM8 Phase B).

Shapes are pinned to the live-probe recon in
``docs/sprints/TM8-openai-recon.md`` (sections 2-4), recorded against a
real Pay-as-you-go org with a read-only Admin key. Where the recon's
field list exceeds OpenAI's published docs (the audio/image/text token
sub-splits), the recon wins — those fields are real on the wire.

Per the multi-vendor convention (CLAUDE.md "TM8 conventions") and the
``flat_model_with_extra_allow_over_discriminated_union`` rule, every
model is a **flat** Pydantic model with ``ConfigDict(extra="allow")``:
OpenAI can add fields without a versioned response-shape change, so the
conservative posture is absorb-and-keep (the unmodeled field lands in
``model_extra``) rather than refuse to parse.

Pydantic 2.x reserves the ``model_`` namespace; any model with a field
literally named ``model`` sets ``protected_namespaces=()`` so the field
keeps its natural wire name.

Money note: ``CostAmount.value`` arrives as a JSON number that is
sometimes scientific notation (e.g. ``1.29e-05``). It is parsed via
``Decimal(str(value))`` in a validator so we never route billed spend
through a binary float. Token counts stay ``int``.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, field_validator


# ───────────────────────────────────────────────────────────────────────────
# Usage — /v1/organization/usage/completions  (recon §2)
#
# Envelope: {object:"page", data:[bucket], has_more, next_page}. Each
# bucket is {object:"bucket", start_time, end_time, results:[result]}.
# A result row carries the FULL token-field set from recon §2 plus the
# group_by dimensions (null unless that dim was requested via group_by[]).
# ───────────────────────────────────────────────────────────────────────────


class UsageCompletionsResult(BaseModel):
    """One grouped usage result row (``organization.usage.completions.result``).

    Recon §2: with ``group_by=model,user_id,api_key_id,project_id`` the
    API emits one row per distinct dimension tuple within each bucket;
    without ``group_by`` it emits a single aggregate row per bucket with
    every dimension ``null`` and tokens summed.

    ⚠ Billing trap (recon §2.1): ``input_tokens`` is the TOTAL input and
    EQUALS ``input_uncached_tokens + input_cached_tokens``. Cost is
    derived from the uncached + cached split — NEVER from the raw
    ``input_tokens`` field, or cached usage is billed twice. OpenAI has
    no cache-creation charge, so the canonical ``cache_creation`` is
    always 0 for OpenAI.

    The audio/image/text token sub-splits are stored verbatim in
    ``record_metadata`` by the pull task; only the four billing fields
    (``input_uncached_tokens``, ``input_cached_tokens``,
    ``output_tokens``, plus the always-0 cache-creation) drive cost.
    """

    model_config = ConfigDict(extra="allow", protected_namespaces=())

    # ── group_by dimensions (null unless requested) ──
    project_id: Optional[str] = None
    user_id: Optional[str] = None
    api_key_id: Optional[str] = None
    # Date-stamped on the wire (``gpt-4o-2024-08-06``) → longest-prefix
    # match for pricing. Null unless ``group_by=model`` requested.
    model: Optional[str] = None
    batch: Optional[bool] = None
    service_tier: Optional[str] = None

    # ── request count ──
    num_model_requests: int = 0

    # ── billing-relevant token totals ──
    # TOTAL input (INCLUDES cached) — do NOT bill directly. See §2.1.
    input_tokens: int = 0
    # Full-rate portion.
    input_uncached_tokens: int = 0
    # Half-rate (cached) portion → maps to the canonical cache_read.
    input_cached_tokens: int = 0
    output_tokens: int = 0

    # ── text / audio / image sub-splits (recon §2; stored, not billed) ──
    input_text_tokens: int = 0
    output_text_tokens: int = 0
    input_cached_text_tokens: int = 0
    input_audio_tokens: int = 0
    input_cached_audio_tokens: int = 0
    output_audio_tokens: int = 0
    input_image_tokens: int = 0
    input_cached_image_tokens: int = 0
    output_image_tokens: int = 0


class UsageBucket(BaseModel):
    """One time-bucketed usage row (``object="bucket"``).

    Endpoint: ``GET /v1/organization/usage/completions`` (and the
    structurally identical ``/usage/embeddings``). Each bucket covers
    ``[start_time, end_time)`` (Unix-second integers) and carries
    ``results`` — the per-dimension breakdown rows for that window.
    ``start_time_iso`` / ``end_time_iso`` ride along via ``extra="allow"``
    when present.

    ``start_time`` / ``end_time`` are Unix-epoch seconds (ints) on the
    wire; Pydantic coerces them to ``datetime``. The external_id (see
    the pull task) uses the integer epoch window so dedup is stable.
    """

    model_config = ConfigDict(extra="allow")

    start_time: datetime
    end_time: datetime
    results: list[UsageCompletionsResult] = []


# ───────────────────────────────────────────────────────────────────────────
# Costs — /v1/organization/costs  (recon §3)
#
# Envelope: {object:"page", data:[bucket], has_more, next_page}. Same
# bucket shape as usage, but result rows are cost rows. NO user_id —
# costs group only by project_id / line_item. bucket_width=1d only.
# ───────────────────────────────────────────────────────────────────────────


class CostAmount(BaseModel):
    """The ``amount`` sub-object on a cost result row: ``{value, currency}``.

    Recon §3: ``value`` is a JSON number that is sometimes scientific
    notation (``1.29e-05``). Parsed via ``Decimal(str(value))`` so billed
    spend never passes through a binary float. ``currency`` is a plain
    string (``"usd"`` observed).
    """

    model_config = ConfigDict(extra="allow")

    value: Decimal = Decimal("0")
    currency: Optional[str] = None

    @field_validator("value", mode="before")
    @classmethod
    def _value_via_str_decimal(cls, v: Any) -> Any:
        """Decimal(str(value)) to survive scientific notation + avoid float.

        ``Decimal(0.0002225)`` would inherit the float's binary
        imprecision; ``Decimal(str(0.0002225))`` is exact. A float in
        sci-notation (``1.29e-05``) stringifies to ``"1.29e-05"`` which
        ``Decimal`` parses natively. ``None`` / already-``Decimal`` /
        ``str`` pass through to the normal coercion.
        """
        if v is None:
            return v
        if isinstance(v, Decimal):
            return v
        if isinstance(v, float):
            return Decimal(str(v))
        # ints and numeric strings: str() is safe and exact.
        return Decimal(str(v))


class CostResult(BaseModel):
    """One cost result row (``organization.costs.result``).

    Recon §3: ``line_item`` carries both per-model token costs
    (``"<model>, input"`` / ``"<model>, output"``) and non-token billed
    items (``"ft-… training"``, etc.) that a tokens×pricing estimate can
    never reproduce — which is why ``/costs`` is the authoritative source
    for total/project spend while ``/usage`` drives per-user estimates.

    No ``user_id`` field — costs group only by ``project_id`` /
    ``line_item``. ``project_name`` / ``organization_name`` are returned
    here (uniquely among the endpoints) and ride the model directly.
    """

    model_config = ConfigDict(extra="allow")

    amount: CostAmount = CostAmount()
    line_item: Optional[str] = None
    quantity: Optional[float] = None
    project_id: Optional[str] = None
    project_name: Optional[str] = None
    organization_id: Optional[str] = None
    organization_name: Optional[str] = None


class CostBucket(BaseModel):
    """One time-bucketed cost row (``object="bucket"``).

    Endpoint: ``GET /v1/organization/costs``. Same bucket envelope as
    usage; ``results`` holds :class:`CostResult` rows. ``bucket_width``
    is ``1d`` only for costs (recon §6).
    """

    model_config = ConfigDict(extra="allow")

    start_time: datetime
    end_time: datetime
    results: list[CostResult] = []


# ───────────────────────────────────────────────────────────────────────────
# Audit logs — /v1/organization/audit_logs  (recon §1, §8)
#
# Envelope: {object:"list", data:[entry], first_id, last_id, has_more}.
# Empty (200, data:[]) below Enterprise — accessible ≠ populated. The
# populated entry shape (id, type, effective_at, actor, project) is
# modeled from OpenAI's docs; event-specific detail rides extra="allow".
# ───────────────────────────────────────────────────────────────────────────


class AuditLogActor(BaseModel):
    """The principal on an audit-log entry.

    Recon §8: this org's audit feed was empty, so the actor shape is
    modeled from OpenAI's docs (``session``/``api_key`` sub-objects,
    ``type`` discriminator). All-Optional + ``extra="allow"`` absorbs
    the variants; the pull task stores the raw entry in
    ``record_metadata`` and reads what it needs.
    """

    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None
    session: Optional[dict] = None
    api_key: Optional[dict] = None


class AuditLogProject(BaseModel):
    """The project context on an audit-log entry (id + name)."""

    model_config = ConfigDict(extra="allow")

    id: Optional[str] = None
    name: Optional[str] = None


class AuditLogEntry(BaseModel):
    """One audit-log entry (``organization.audit_log``).

    Endpoint: ``GET /v1/organization/audit_logs``. ``type`` is an open
    enum (``api_key.created``, ``login.succeeded``, ``project.archived``,
    …); the type-specific detail object (named after the event type)
    rides along via ``extra="allow"`` rather than per-type subclasses.

    ``effective_at`` is Unix-epoch seconds on the wire → coerced to
    ``datetime``. ``id`` is the stable event id used in the external_id
    (``openai:openai_audit_logs:{event_id}``) for dedup.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    type: str
    effective_at: datetime
    actor: Optional[AuditLogActor] = None
    project: Optional[AuditLogProject] = None


# ───────────────────────────────────────────────────────────────────────────
# Lists — projects / users / project api_keys  (recon §4)
#
# Envelope: {object:"list", data:[row], first_id, last_id, has_more}.
# Cursor via after=<last_id>.
# ───────────────────────────────────────────────────────────────────────────


class Project(BaseModel):
    """One organization project (``organization.project``).

    Endpoint: ``GET /v1/organization/projects``. Feeds the
    ``openai_projects`` side table (migration 0025). ``archived_at`` is
    null for ``status="active"`` projects. ``created_at`` /
    ``archived_at`` are Unix-epoch seconds → coerced to ``datetime``.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[datetime] = None
    archived_at: Optional[datetime] = None


class OrgUser(BaseModel):
    """One organization user (``organization.user``).

    Endpoint: ``GET /v1/organization/users``. **Exposes ``email`` (PII)**
    — this is the value ``user_aliases`` matches on for cross-vendor user
    unification (the OpenAI user's email auto-matches ``users.email``).
    Feeds the ``openai_users`` side table. ``added_at`` is Unix-epoch
    seconds → coerced to ``datetime``.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    email: Optional[str] = None
    name: Optional[str] = None
    role: Optional[str] = None
    added_at: Optional[datetime] = None


class ProjectApiKeyOwner(BaseModel):
    """The nested ``owner`` on a project API key (recon §4).

    ``type`` is ``"user"`` or ``"service_account"``; the matching nested
    object (``user`` / ``service_account``) rides via ``extra="allow"``.
    Informational only — the side table stores the key id/name, not the
    owner graph.
    """

    model_config = ConfigDict(extra="allow")

    type: Optional[str] = None
    user: Optional[dict] = None
    service_account: Optional[dict] = None


class ProjectApiKey(BaseModel):
    """One project API key (``organization.project.api_key``).

    Endpoint: ``GET /v1/organization/projects/{project_id}/api_keys``.
    Feeds the ``openai_api_keys`` side table — the usage report's
    ``api_key_id`` resolves to ``name`` here for "API key — <name>"
    rendering. ``redacted_value`` is masked by OpenAI (``sk-proj-****…``)
    — safe to store but useless for auth. ``created_at`` / ``last_used_at``
    are Unix-epoch seconds → coerced to ``datetime``; ``last_used_at`` is
    null for never-used keys.
    """

    model_config = ConfigDict(extra="allow")

    id: str
    name: Optional[str] = None
    created_at: Optional[datetime] = None
    last_used_at: Optional[datetime] = None
    owner: Optional[ProjectApiKeyOwner] = None
    owner_project_access: Optional[str] = None
    redacted_value: Optional[str] = None
