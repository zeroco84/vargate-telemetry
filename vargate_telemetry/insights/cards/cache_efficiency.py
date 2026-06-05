# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cache-efficiency Insights card (TM7; cross-vendor TM8 Phase D).

Surfaces per-model prompt-caching recommendations across **both
vendors**, because the two clouds cache very differently:

Anthropic (unchanged from TM7)
------------------------------
Caching is **explicit**: you mark a prompt prefix with ``cache_control``
and pay a one-off **cache-creation** premium to write it, then a cheap
**cache-read** rate on every reuse. The failure mode is *writing cache
that's never read back* — high ``cache_creation`` + low ``cache_read``.
This card reuses the exact tiering helper
(:func:`vargate_telemetry.api.usage._cache_recommendation`) the
``/usage/cache-recommendations`` endpoint runs, so the card and that
page can never disagree about which Anthropic models are flagged or
their hit ratio. The "recoverable premium" figure is the cache-creation
spend that, at a healthy hit ratio, would have been served at the
cheaper read rate.

OpenAI (TM8 Phase D)
--------------------
Caching is **automatic, input-only, ~50% off, with NO cache-creation
charge** — and it only kicks in for prompts whose **stable prefix
exceeds ~1024 tokens** (OpenAI's automatic-caching minimum). There is
nothing to "write" and no creation premium to recover; the lever is
*structuring requests so the cacheable prefix is long enough and stable
enough to actually get cached*. The failure mode is therefore the
inverse of Anthropic's: a model with **high uncached input and almost
no cached input** is very likely sending prompts whose prefix is below
the 1024-token floor (or is varying the prefix so the cache never
hits). We flag those and recommend verifying / lengthening the prefix.
Because OpenAI cached input is billed at half the uncached rate, the
"recoverable" figure is what the currently-uncached input *would* cost
at the cached rate if it started hitting — an upper-bound saving.

One headline spans both vendors (e.g. "3 recommendations across both
vendors"); each :class:`InsightItem` is vendor-tagged in its ``detail``.
On a single-vendor tenant the card reads exactly as it did in TM7 for
that vendor.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text as sql_text

from vargate_telemetry.db import session_scope
from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import (
    Card,
    InsightCta,
    InsightItem,
    escalate,
    idle_card,
)
from vargate_telemetry.pricing import openai_rates, vendor_cost
from vargate_telemetry.pricing.anthropic_rates import rates_at

CARD_ID = "cache_efficiency"
CARD_TITLE = "Cache efficiency"

# A flagged Anthropic model whose hit ratio is below this is loud
# (severity "action"): cache is being written but almost never read
# back, which is close to pure waste.
_ACTION_HIT_RATE = 0.30

# OpenAI automatic-caching minimum prefix length (tokens). Prompts whose
# stable prefix is shorter than this never get cached, so a model with
# lots of uncached input and ~no cached input is the canonical "prefix
# too short / unstable" case. Sourced from the recon (OpenAI automatic
# prompt caching, 1024-token minimum).
_OPENAI_MIN_CACHE_PREFIX_TOKENS = 1024

# Below this OpenAI cached-input fraction we treat caching as "not
# really happening" and recommend verifying the prefix. Above the
# warn floor but below "healthy" is an advisory nudge.
_OPENAI_CACHE_WARN_FRACTION = 0.05
_OPENAI_CACHE_HEALTHY_FRACTION = 0.30

# Shared volume floor (matches api/usage._CACHE_VOLUME_FLOOR): below
# this much input there's too little signal to reason about caching.
_CACHE_VOLUME_FLOOR = 100_000


def _fmt_dollars(amount: Decimal) -> str:
    """Whole-dollar, thousands-separated string (e.g. ``$1,240``)."""
    return "$" + format(int(amount.quantize(Decimal("1"))), ",")


# ───────────────────────────────────────────────────────────────────────────
# Anthropic per-model token totals (unchanged from TM7).
# ───────────────────────────────────────────────────────────────────────────

_MODEL_TOKENS_SQL = sql_text(
    """
    SELECT
        COALESCE(r.result->>'model', '(unspecified)') AS model,
        COALESCE(SUM((r.result->>'input_tokens')::bigint), 0)
            AS uncached_input,
        COALESCE(SUM((r.result->>'cache_read_input_tokens')::bigint), 0)
            AS cache_read,
        COALESCE(SUM(COALESCE(
            NULLIF((r.result->>'cache_creation_input_tokens')::bigint, 0),
            ((r.result->'cache_creation')->>'ephemeral_5m_input_tokens')::bigint
            + ((r.result->'cache_creation')->>'ephemeral_1h_input_tokens')::bigint,
            0
        )), 0) AS cache_creation,
        COALESCE(SUM((r.result->>'output_tokens')::bigint), 0)
            AS output_tokens
    FROM telemetry_records tr,
         jsonb_array_elements(tr.metadata->'results') AS r(result)
    WHERE tr.tenant_id = current_setting('app.tenant_id')
      AND tr.record_type = 'usage'
      AND tr.source_api = 'admin'
      AND tr.occurred_at
          >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
    GROUP BY 1
    """
)


# ───────────────────────────────────────────────────────────────────────────
# OpenAI per-model token totals. One grouped result per record under
# the ``result`` key (the usage pull writes one grouped row per
# record); we sum the uncached / cached / output split per model.
# ───────────────────────────────────────────────────────────────────────────

_OPENAI_MODEL_TOKENS_SQL = sql_text(
    """
    SELECT
        COALESCE(tr.metadata->'result'->>'model', '(unspecified)') AS model,
        COALESCE(
            SUM((tr.metadata->'result'->>'input_uncached_tokens')::bigint), 0
        ) AS uncached_input,
        COALESCE(
            SUM((tr.metadata->'result'->>'input_cached_tokens')::bigint), 0
        ) AS cached_input,
        COALESCE(
            SUM((tr.metadata->'result'->>'output_tokens')::bigint), 0
        ) AS output_tokens
    FROM telemetry_records tr
    WHERE tr.tenant_id = current_setting('app.tenant_id')
      AND tr.record_type = 'usage'
      AND tr.source_api = :source_api
      AND tr.occurred_at
          >= (now() AT TIME ZONE 'UTC') - make_interval(days => :days)
    GROUP BY 1
    """
)


def _openai_cache_recommendation(
    uncached_input: int, cached_input: int
) -> tuple[str, Optional[float], str]:
    """Cache-efficiency verdict for one OpenAI model over the window.

    Returns ``(severity, cached_fraction, recommendation)`` shaped like
    the Anthropic helper, but with OpenAI semantics:

      - ``cached_fraction`` = ``cached / (cached + uncached)`` input —
        OpenAI caching is input-only and automatic, so this is the
        "fraction of input that hit the automatic cache". ``None`` when
        there is no input at all.
      - severity ``warn`` when input is above the volume floor and the
        cached fraction is essentially zero (the prefix is almost
        certainly below the ~1024-token automatic-caching minimum, or is
        varying so the cache never hits); ``info`` for a low-but-present
        fraction with room to improve; ``ok`` otherwise (healthy, or too
        little volume to judge).

    Unlike Anthropic there is no cache-*creation* signal: OpenAI never
    charges to write the cache, so "poor reuse of written cache" is not
    a thing — the only lever is getting the prefix long/stable enough to
    be cached in the first place.
    """
    total_input = uncached_input + cached_input
    cached_fraction = (
        (cached_input / total_input) if total_input > 0 else None
    )

    if total_input < _CACHE_VOLUME_FLOOR:
        return (
            "ok",
            cached_fraction,
            "Low input volume — automatic prompt caching has little impact "
            "at this scale yet.",
        )

    assert cached_fraction is not None  # total_input > 0 here

    if cached_fraction < _OPENAI_CACHE_WARN_FRACTION:
        return (
            "warn",
            cached_fraction,
            f"Almost no cached input on {total_input:,} input tokens. "
            "OpenAI caches automatically only when the stable prompt "
            f"prefix exceeds ~{_OPENAI_MIN_CACHE_PREFIX_TOKENS:,} tokens — "
            "verify your prefix clears that minimum and stays byte-stable "
            "so it starts hitting the 50%-off cached rate.",
        )
    if cached_fraction < _OPENAI_CACHE_HEALTHY_FRACTION:
        return (
            "info",
            cached_fraction,
            f"Only {round(cached_fraction * 100)}% of input is cached. "
            "Lengthening / stabilising the shared prompt prefix would let "
            "more of it land in OpenAI's automatic cache at half price.",
        )
    return (
        "ok",
        cached_fraction,
        f"Healthy automatic caching ({round(cached_fraction * 100)}% of "
        "input cached).",
    )


def _anthropic_items(
    tenant_id: str, days: int
) -> tuple[list[InsightItem], str, int, int, int]:
    """Build the Anthropic per-model cache findings (unchanged TM7 logic).

    Returns ``(items, severity, below_action, warn_count, info_count)``
    so the caller can fold these counts into the cross-vendor headline.
    Severity is the worst Anthropic-side tier (``idle`` if nothing
    flagged).
    """
    # Lazy import (not module-level): the tiering helper lives in
    # api/usage.py, and the api package __init__ imports api.app, which
    # mounts the insights router — importing it at module load creates an
    # insights -> api -> app -> insights cycle. By the time build_card
    # runs the app is fully loaded, so a call-time import is always safe
    # and lets `insights` be imported standalone (scripts, ad-hoc checks).
    from vargate_telemetry.api.usage import _cache_recommendation

    with session_scope(tenant_id) as s:
        rows = s.execute(_MODEL_TOKENS_SQL, {"days": days}).all()

    now = datetime.now(timezone.utc)
    rate_card = rates_at(now)

    items: list[InsightItem] = []
    severity = "idle"
    below_action = 0
    warn_count = 0
    info_count = 0

    for row in rows:
        uncached = int(row.uncached_input)
        cache_read = int(row.cache_read)
        cache_creation = int(row.cache_creation)

        sev, hit_rate, _text = _cache_recommendation(
            uncached, cache_read, cache_creation
        )

        # Only surface improvable models: warn/info AND a real hit ratio.
        # A None hit_rate (below the volume floor, or cache never
        # written) has no reuse number to show.
        if sev not in ("warn", "info") or hit_rate is None:
            continue

        if sev == "warn":
            warn_count += 1
        else:
            info_count += 1

        is_below_action = hit_rate < _ACTION_HIT_RATE
        if is_below_action:
            below_action += 1
            severity = escalate(severity, "action")
        elif sev == "warn":
            severity = escalate(severity, "warning")
        else:
            severity = escalate(severity, "advisory")

        detail = f"Anthropic · {round(hit_rate * 100)}% hit ratio"

        # Recoverable premium: only when we can price the model. The
        # delta between cache-creation and cache-read per-mtok is what
        # writing-without-reuse costs.
        value = None
        rates = rate_card.get(row.model)
        if rates is not None:
            premium = (
                Decimal(cache_creation)
                * (rates.cache_creation_per_mtok - rates.cache_read_per_mtok)
                / Decimal(1_000_000)
            )
            # Scale a sub-monthly window up to a monthly figure so the
            # number is comparable regardless of window.
            if days < 30:
                premium = premium * Decimal(30) / Decimal(days)
            if premium > 0:
                value = f"{_fmt_dollars(premium)}/mo est. recoverable"

        items.append(InsightItem(label=row.model, detail=detail, value=value))

    return items, severity, below_action, warn_count, info_count


def _openai_items(
    tenant_id: str, days: int
) -> tuple[list[InsightItem], str, int, int]:
    """Build the OpenAI per-model cache findings (TM8 Phase D).

    Returns ``(items, severity, warn_count, info_count)``. Severity is
    the worst OpenAI-side tier — ``warning`` for a "prefix below the
    automatic-caching minimum" finding, ``advisory`` for a "could cache
    more" nudge. OpenAI has no cache-creation waste, so there is no
    ``action`` tier here (nothing is being actively burned).
    """
    with session_scope(tenant_id) as s:
        rows = s.execute(
            _OPENAI_MODEL_TOKENS_SQL,
            {"days": days, "source_api": vendor_cost.SOURCE_API_OPENAI_USAGE},
        ).all()

    now = datetime.now(timezone.utc)
    rate_card = openai_rates.rates_at(now)

    items: list[InsightItem] = []
    severity = "idle"
    warn_count = 0
    info_count = 0

    for row in rows:
        uncached = int(row.uncached_input)
        cached = int(row.cached_input)

        sev, cached_fraction, _text = _openai_cache_recommendation(
            uncached, cached
        )

        # Surface only improvable models with a real fraction to show.
        if sev not in ("warn", "info") or cached_fraction is None:
            continue

        if sev == "warn":
            warn_count += 1
            severity = escalate(severity, "warning")
        else:
            info_count += 1
            severity = escalate(severity, "advisory")

        detail = (
            f"OpenAI · {round(cached_fraction * 100)}% of input cached"
        )

        # Upper-bound saving: the currently-uncached input, if it started
        # hitting the automatic cache, bills at the cached rate instead of
        # full — so the saving per token is (input - cache_read) per mtok.
        # Only when we can price the model.
        value = None
        rates = openai_rates._lookup_model(row.model, rate_card)
        if rates is not None:
            saving = (
                Decimal(uncached)
                * (rates.input_per_mtok - rates.cache_read_per_mtok)
                / Decimal(1_000_000)
            )
            if days < 30:
                saving = saving * Decimal(30) / Decimal(days)
            if saving > 0:
                value = f"{_fmt_dollars(saving)}/mo est. if cached"

        items.append(InsightItem(label=row.model, detail=detail, value=value))

    return items, severity, warn_count, info_count


def build_card(tenant_id: str, window: str) -> Card:
    """Build the cross-vendor cache-efficiency card over ``window``."""
    days = spend_data.window_to_days(window)

    (
        anthropic_items,
        anthropic_severity,
        below_action,
        a_warn,
        a_info,
    ) = _anthropic_items(tenant_id, days)

    openai_items, openai_severity, o_warn, o_info = _openai_items(
        tenant_id, days
    )

    items = anthropic_items + openai_items

    if not items:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Your cache hit ratios look healthy. We watch per-model "
                "cache reuse across Anthropic and OpenAI and flag workflows "
                "wasting spend — Anthropic cache that's written but never "
                "read, or OpenAI prompts whose prefix is too short to be "
                "automatically cached."
            ),
        )

    severity = escalate(anthropic_severity, openai_severity)

    # Cross-vendor headline. Prefer the loudest concrete signal; when
    # both vendors contribute, say so.
    both_vendors = bool(anthropic_items) and bool(openai_items)
    total_findings = len(items)

    if both_vendors:
        headline = (
            f"{total_findings} cache recommendation(s) across both vendors"
        )
    elif severity == "action" or severity == "warning":
        if below_action > 0:
            headline = f"Cache hit ratio below 30% on {below_action} model(s)"
        elif a_warn > 0:
            headline = f"Low cache reuse on {a_warn} model(s)"
        else:  # OpenAI-only warning: prefix below the caching minimum
            headline = (
                f"{o_warn} model(s) barely hitting OpenAI's automatic cache"
            )
    else:  # advisory
        improvable = a_info + o_info
        headline = f"{improvable} model(s) have improvable cache reuse"

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity=severity,
        findings_count=len(items),
        headline=headline,
        items=items,
        cta=InsightCta(label="See recommendations", href="/insights/cache"),
    )
