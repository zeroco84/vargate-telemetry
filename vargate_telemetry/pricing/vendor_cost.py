# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cross-vendor record cost primitive (TM8 Phase D).

The single chokepoint that prices ONE captured ``telemetry_records``
row's ``metadata`` against the right vendor's rate card, given only the
record's ``source_api``. It is the cross-vendor analogue of the
per-row ``compute_cost_usd`` call that ``api/usage.py`` and
``budgets/spend.py`` make inline for Anthropic — pulled out into a
vendor-dispatching helper so the Insights cards (and any wave-2
consumer) can roll up spend across vendors without re-implementing
each vendor's token-field extraction.

Two functions:

- :func:`estimate_record_cost_usd` — dispatch by ``source_api`` to the
  vendor's ``compute_cost_usd`` after extracting that vendor's token
  fields from the record metadata. **Anthropic numbers are reproduced
  EXACTLY** (same field extraction, same rate helper) so this is a
  drop-in for the existing per-row pricing — regression-safe. Returns
  ``None`` for an unpriceable / unknown-source record (never fakes a
  number — same discipline as ``compute_cost_usd``).
- :func:`vendor_of` — ``source_api`` → display vendor name
  (``"Anthropic"`` | ``"OpenAI"`` | ``"Google"``).

Why ``metadata`` (not a SQL row)
================================

The existing Anthropic pricing reads token fields out of expanded
``metadata->'results'`` JSONB *in SQL*. This module instead takes the
already-deserialized ``metadata`` dict so it works the same whether the
caller is a SQL aggregation (passing ``row.metadata`` /
``jsonb``-loaded dict) or a Python-side iteration over records. The
extraction reproduces the SQL's COALESCE/NULLIF cache-creation handling
in Python (see :func:`_anthropic_breakdown_cost`).

Authoritative vs estimated
===========================

This primitive only produces **usage-token estimates** (Anthropic
always; OpenAI from the ``openai_admin_usage`` stream; Google/Vertex
from the ``vertex_token_usage`` stream). OpenAI also has
**authoritative billed spend** in the ``openai_admin_costs`` stream
(``amount.value``); that is NOT priced here — it is read directly by
:mod:`vargate_telemetry.insights.spend_data` (``openai_actual_spend``).
``estimate_record_cost_usd`` returns ``None`` for an
``openai_admin_costs`` record on purpose: a cost record carries no
token fields to estimate from, and double-counting it against the
usage estimate is the trap the per-vendor spend split exists to avoid.

Google/Vertex mirrors OpenAI exactly: usage (``vertex_token_usage``,
from Cloud Monitoring ``token_count``) is token-estimated here via
:mod:`vargate_telemetry.pricing.vertex_rates`, while costs
(``vertex_billing_costs``) are **authoritative** (the BigQuery billing
export, net of credits) and read separately by the spend rollups —
``estimate_record_cost_usd`` returns ``None`` for a
``vertex_billing_costs`` record for the same no-double-count reason.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from vargate_telemetry.pricing import anthropic_rates, openai_rates, vertex_rates

# ───────────────────────────────────────────────────────────────────────────
# source_api → vendor mapping
# ───────────────────────────────────────────────────────────────────────────
#
# Display vendor name per ingest stream. Anthropic streams are the
# established T3/T5 set (``admin`` is the usage stream this module
# prices; the rest are non-usage but still attribute to Anthropic).
# OpenAI streams are the TM8 set. Unknown sources fall through to
# ``Anthropic`` in vendor_of (the historical default — every pre-TM8
# stream is Anthropic), but estimate_record_cost_usd prices only the
# two usage streams and returns None for everything else.

VENDOR_ANTHROPIC = "Anthropic"
VENDOR_OPENAI = "OpenAI"
VENDOR_GOOGLE = "Google"

# Usage streams this primitive can price (token-derived estimate).
SOURCE_API_ANTHROPIC_USAGE = "admin"
SOURCE_API_OPENAI_USAGE = "openai_admin_usage"
# Vertex (TM9) usage stream — Cloud Monitoring token_count. Both Vertex
# source_api strings are <= 32 chars (the pull_state limit): 18 / 20.
SOURCE_API_VERTEX_USAGE = "vertex_token_usage"
# Vertex (TM9) cost stream — BigQuery billing export. Authoritative
# (net-of-credits) billed spend; NOT estimated here (returns None from
# estimate_record_cost_usd, read separately by the spend rollups).
SOURCE_API_VERTEX_COSTS = "vertex_billing_costs"

# Every OpenAI source_api prefix → OpenAI; every Vertex prefix → Google.
# Anthropic is the default for everything else (matches the pre-TM8 world
# where all streams were Anthropic). The prefixes are disjoint
# (``openai_`` vs ``vertex_``), so the test order is for readability only.
_OPENAI_SOURCE_PREFIX = "openai_"
_VERTEX_SOURCE_PREFIX = "vertex_"


def vendor_of(source_api: str) -> str:
    """Display vendor name for a ``source_api`` value.

    ``"vertex_token_usage"`` / ``"vertex_billing_costs"`` (any
    ``vertex_*`` stream) → ``"Google"``; ``"openai_admin_usage"`` /
    ``"openai_admin_costs"`` / ``"openai_audit_logs"`` (any ``openai_*``
    stream) → ``"OpenAI"``; everything else (the Anthropic streams
    ``admin`` / ``mcp`` / ``code_analytics`` / ``activity_feed`` /
    ``compliance_*``, and any future Anthropic stream) → ``"Anthropic"``.
    The OpenAI and Vertex tests are prefix matches so a new ``openai_*`` /
    ``vertex_*`` stream is classified correctly without touching this map;
    the two prefixes are disjoint, so the order is for readability only.
    """
    if source_api and source_api.startswith(_VERTEX_SOURCE_PREFIX):
        return VENDOR_GOOGLE
    if source_api and source_api.startswith(_OPENAI_SOURCE_PREFIX):
        return VENDOR_OPENAI
    return VENDOR_ANTHROPIC


# ───────────────────────────────────────────────────────────────────────────
# Anthropic — reproduce api/usage.py's per-breakdown token extraction
# ───────────────────────────────────────────────────────────────────────────


def _as_int(value: Any) -> int:
    """Coerce a JSONB-loaded token field to int, treating ``None``/missing
    as 0 — matches the SQL ``COALESCE(...::bigint, 0)`` in api/usage.py."""
    if value is None:
        return 0
    return int(value)


def _anthropic_cache_creation_tokens(breakdown: dict[str, Any]) -> int:
    """Cache-creation tokens for one Anthropic breakdown row.

    Reproduces the SQL COALESCE/NULLIF in ``api/usage.py`` /
    ``budgets/spend.py`` exactly:

        COALESCE(
            NULLIF(cache_creation_input_tokens, 0),
            ephemeral_5m_input_tokens + ephemeral_1h_input_tokens,
            0
        )

    i.e. prefer the flat ``cache_creation_input_tokens`` when it is
    present and non-zero; otherwise fall through to the nested
    ``cache_creation`` ephemeral sum (the group_by'd response shape
    drops the flat key entirely, defaulting it to 0, so without the
    NULLIF the flat 0 would mask the real nested value). The final 0 is
    the both-absent case.
    """
    flat = _as_int(breakdown.get("cache_creation_input_tokens"))
    if flat != 0:
        return flat

    nested = breakdown.get("cache_creation")
    if isinstance(nested, dict):
        return _as_int(
            nested.get("ephemeral_5m_input_tokens")
        ) + _as_int(nested.get("ephemeral_1h_input_tokens"))
    return 0


def _anthropic_breakdown_cost(
    breakdown: dict[str, Any], occurred_at: datetime
) -> Optional[Decimal]:
    """Price one Anthropic ``results[i]`` breakdown row.

    Extracts the SAME four token fields ``api/usage.py`` reads
    (``input_tokens`` / ``output_tokens`` / ``cache_read_input_tokens``
    / cache-creation via :func:`_anthropic_cache_creation_tokens`) and
    calls the Anthropic ``compute_cost_usd``. Returns ``None`` when the
    model is null/unknown — identical to the SQL path where such rows
    contribute zero.
    """
    return anthropic_rates.compute_cost_usd(
        breakdown.get("model"),
        input_tokens=_as_int(breakdown.get("input_tokens")),
        output_tokens=_as_int(breakdown.get("output_tokens")),
        cache_read_tokens=_as_int(breakdown.get("cache_read_input_tokens")),
        cache_creation_tokens=_anthropic_cache_creation_tokens(breakdown),
        occurred_at=occurred_at,
    )


def _estimate_anthropic_usage(
    metadata: dict[str, Any], occurred_at: datetime
) -> Optional[Decimal]:
    """Sum the priceable cost across an Anthropic usage record's
    ``metadata['results']`` breakdown rows.

    Post-T5.5.6 a single admin usage record carries exactly one
    breakdown in ``results`` (``_normalize_usage`` splits per breakdown),
    but a pre-T5.5.6 record can carry many — summing handles both. A
    breakdown whose model is null/unknown prices to ``None`` and
    contributes nothing (matches the SQL ``compute_cost_usd``-returns-
    ``None``-skips behavior).

    Returns ``None`` when NO breakdown in the record was priceable, so
    the caller can distinguish "this record adds nothing" from "this
    record adds $0.00" the same way the per-row Anthropic path does.
    """
    results = metadata.get("results")
    if not isinstance(results, list):
        return None

    total: Optional[Decimal] = None
    for breakdown in results:
        if not isinstance(breakdown, dict):
            continue
        cost = _anthropic_breakdown_cost(breakdown, occurred_at)
        if cost is None:
            continue
        total = cost if total is None else total + cost
    return total


# ───────────────────────────────────────────────────────────────────────────
# OpenAI — double-count-safe usage extraction
# ───────────────────────────────────────────────────────────────────────────


def _estimate_openai_usage(
    metadata: dict[str, Any], occurred_at: datetime
) -> Optional[Decimal]:
    """Price one OpenAI usage record's ``metadata['result']`` row.

    ``pull_openai_usage`` writes ONE grouped result per record under the
    ``result`` key (the full ``UsageCompletionsResult.model_dump``).

    ⚠ Double-count trap (recon §2.1): the wire ``input_tokens`` is the
    TOTAL input and equals ``input_uncached_tokens + input_cached_tokens``.
    Cost is derived from the split, NEVER the raw total — pass
    ``input_tokens=input_uncached_tokens`` and
    ``cache_read_tokens=input_cached_tokens`` (and ``cache_creation=0``,
    OpenAI has no cache-write charge). Every OpenAI
    ``cache_creation_per_mtok`` is 0 anyway, so the no-cache-write rule
    holds by construction.

    Returns ``None`` when the record has no ``result`` (an empty-bucket
    sentinel) or the model is null/unknown.
    """
    result = metadata.get("result")
    if not isinstance(result, dict):
        return None

    return openai_rates.compute_cost_usd(
        result.get("model"),
        input_tokens=_as_int(result.get("input_uncached_tokens")),
        output_tokens=_as_int(result.get("output_tokens")),
        cache_read_tokens=_as_int(result.get("input_cached_tokens")),
        cache_creation_tokens=0,
        occurred_at=occurred_at,
    )


# ───────────────────────────────────────────────────────────────────────────
# Google / Vertex — per-monitoring-point token estimate (project/team only)
# ───────────────────────────────────────────────────────────────────────────


def _estimate_vertex_usage(
    metadata: dict[str, Any], occurred_at: datetime
) -> Optional[Decimal]:
    """Price one Vertex usage record's token total.

    Vertex usage is sourced from Cloud Monitoring ``token_count`` (the
    metric splits by a ``type`` label = input|output), per model /
    project. ``pull_vertex_usage._normalize_usage`` writes ONE record per
    monitoring point carrying that single side's count, with the relevant
    fields hoisted to the metadata top level: ``model`` (the Gemini model
    string), ``token_type`` (the raw ``input`` / ``output`` label), and
    ``token_count`` (that side's tokens). The full point dump is also
    nested under ``result``, but the top-level fields are the contract
    this reads.

    Because a point carries EITHER an input OR an output count (never
    both), the count is routed to the matching side and 0 is passed for
    the other — the same per-point posture ``_estimate_point_cost`` uses
    inside ``pull_vertex_usage``. There is no cache-read split and no
    cache-write charge in this metric, so ``cache_read_tokens`` and
    ``cache_creation_tokens`` are both 0; for the context-length-tiered
    model (Gemini 2.5 Pro) the input-token count selects the tier (see
    ``vertex_rates._resolve_rates``).

    Returns ``None`` when the model is null/unknown to the rate card
    (``vertex_rates.compute_cost_usd`` never fakes a number) or the
    point's ``token_type`` is neither ``input`` nor ``output``.

    Google has NO per-user-email attribution — this estimate is per
    token-and-model regardless of who ran the request (the project / team
    dims live in the record metadata, not in pricing).

    # TODO(TM9 Phase A): re-confirm these metadata keys (``model`` /
    # ``token_type`` / ``token_count``) against the frozen
    # pull_vertex_usage record shape once a live GCP project exists — in
    # particular whether a cached-input ``token_type`` appears (which
    # would route to ``cache_read_tokens`` here and add a branch below).
    """
    model = metadata.get("model")
    token_type_raw = metadata.get("token_type")
    token_type = (
        str(token_type_raw).lower() if token_type_raw else ""
    )
    token_count = _as_int(metadata.get("token_count"))

    if token_type == "input":
        return vertex_rates.compute_cost_usd(
            model,
            input_tokens=token_count,
            output_tokens=0,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            occurred_at=occurred_at,
        )
    if token_type == "output":
        return vertex_rates.compute_cost_usd(
            model,
            input_tokens=0,
            output_tokens=token_count,
            cache_read_tokens=0,
            cache_creation_tokens=0,
            occurred_at=occurred_at,
        )
    # Unknown / missing token_type — surface the gap, don't fake a number.
    return None


# ───────────────────────────────────────────────────────────────────────────
# Public dispatch
# ───────────────────────────────────────────────────────────────────────────


def estimate_record_cost_usd(
    source_api: str,
    metadata: dict[str, Any],
    occurred_at: datetime,
) -> Optional[Decimal]:
    """Estimate one captured record's cost in USD, dispatched by vendor.

    Parameters
    ----------
    source_api:
        The record's ``source_api``. Determines which vendor's rate
        card + token-field extraction applies.
    metadata:
        The record's deserialized ``metadata`` JSONB. For Anthropic
        usage (``"admin"``) this is ``{starting_at, ending_at, results:
        [...]}``; for OpenAI usage (``"openai_admin_usage"``) it is the
        per-row wrapper ``{start_time, end_time, modality, result:
        {...}, ...}``.
    occurred_at:
        Bucket timestamp, used to pick the rate card active then. Naive
        datetimes are treated as UTC (the ingest path always stores
        UTC).

    Returns
    -------
    A :class:`Decimal` (six-decimal precision from the underlying
    ``compute_cost_usd``) or ``None`` when the record is unpriceable:
      - ``source_api`` is not a usage stream (``openai_admin_costs`` /
        ``openai_audit_logs`` / ``vertex_billing_costs`` / any non-usage
        Anthropic stream) — a cost record's authoritative spend is read
        elsewhere, not estimated here;
      - the metadata carries no priceable breakdown (empty-bucket
        sentinel);
      - the model is null or unknown to the rate card.

    **Never fakes a number** — same discipline as ``compute_cost_usd``.
    Aggregate the per-record Decimals and round the TOTAL to two
    decimals; rounding each independently would compound error.
    """
    if not isinstance(metadata, dict):
        return None

    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)

    if source_api == SOURCE_API_ANTHROPIC_USAGE:
        return _estimate_anthropic_usage(metadata, occurred_at)
    if source_api == SOURCE_API_OPENAI_USAGE:
        return _estimate_openai_usage(metadata, occurred_at)
    if source_api == SOURCE_API_VERTEX_USAGE:
        return _estimate_vertex_usage(metadata, occurred_at)

    # openai_admin_costs / openai_audit_logs / vertex_billing_costs /
    # non-usage Anthropic streams: not token-estimable here.
    # vertex_billing_costs is authoritative (BigQuery billing export, net
    # of credits) and read separately by the spend rollups — pricing it
    # here would double-count against the vertex_token_usage estimate.
    return None


__all__ = [
    "SOURCE_API_ANTHROPIC_USAGE",
    "SOURCE_API_OPENAI_USAGE",
    "SOURCE_API_VERTEX_COSTS",
    "SOURCE_API_VERTEX_USAGE",
    "VENDOR_ANTHROPIC",
    "VENDOR_GOOGLE",
    "VENDOR_OPENAI",
    "estimate_record_cost_usd",
    "vendor_of",
]
