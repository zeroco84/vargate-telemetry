# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Cache-efficiency Insights card (TM7).

Surfaces the *existing* per-model cache analysis that
``api/usage.py`` already computes for the
``/usage/cache-recommendations`` endpoint, repackaged as an Insights
:class:`Card`. We reuse that endpoint's pure tiering helper
(:func:`vargate_telemetry.api.usage._cache_recommendation`) so the
card and the recommendations page can never disagree about which
models are flagged or what their hit ratio is.

For each model over the window we sum the four token totals from the
captured Admin-API usage records (``record_type='usage'``,
``source_api='admin'``; one ``metadata->'results'`` element per
CROSS JOIN row) and run the tiering helper. We flag the models the
helper calls ``warn`` or ``info`` *and* that have a real hit_rate —
a ``None`` hit_rate means there's no cache activity to reason about
(either below the volume floor, or cache was never written), so
there's no "reuse ratio" to show and we leave it off the card.

Recoverable estimate
---------------------
When the model is in the active rate card, the per-mtok delta between
cache-*creation* and cache-*read* pricing is the premium paid for
every token written to cache. ``cache_creation_tokens * (creation -
read) / 1e6`` is the spend on cache that — at a healthy hit ratio —
would mostly have been served at the cheaper read rate. We surface it
as a monthly figure (scaled up from a <30d window) so the operator
sees an annualisable "here's what poor reuse is costing you" number,
not a one-week sliver. Omitted when the model isn't in the rate card.
"""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import text as sql_text

from vargate_telemetry.api.usage import _cache_recommendation
from vargate_telemetry.db import session_scope
from vargate_telemetry.insights import spend_data
from vargate_telemetry.insights.models import (
    Card,
    InsightCta,
    InsightItem,
    escalate,
    idle_card,
)
from vargate_telemetry.pricing.anthropic_rates import rates_at

CARD_ID = "cache_efficiency"
CARD_TITLE = "Cache efficiency"

# A flagged model whose hit ratio is below this is loud (severity
# "action"): cache is being written but almost never read back, which
# is close to pure waste.
_ACTION_HIT_RATE = 0.30


def _fmt_dollars(amount: Decimal) -> str:
    """Whole-dollar, thousands-separated string (e.g. ``$1,240``)."""
    return "$" + format(int(amount.quantize(Decimal("1"))), ",")


# Per-model token totals over the window. Mirrors the source +
# supersession-free aggregation insights use elsewhere (a plain SUM
# per model; the cards are directional, not billing-grade — see
# spend_data's module note).
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


def build_card(tenant_id: str, window: str) -> Card:
    """Build the cache-efficiency card for ``tenant_id`` over ``window``."""
    days = spend_data.window_to_days(window)

    with session_scope(tenant_id) as s:
        rows = s.execute(_MODEL_TOKENS_SQL, {"days": days}).all()

    now = datetime.now(timezone.utc)
    rate_card = rates_at(now)

    items: list[InsightItem] = []
    severity = "idle"
    below_action = 0  # flagged models with hit_rate < 0.30
    warn_count = 0
    info_count = 0

    for row in rows:
        uncached = int(row.uncached_input)
        cache_read = int(row.cache_read)
        cache_creation = int(row.cache_creation)

        sev, hit_rate, _text = _cache_recommendation(
            uncached, cache_read, cache_creation
        )

        # Only surface improvable models: warn/info AND a real hit
        # ratio. A None hit_rate (below the volume floor, or cache
        # never written) has no reuse number to show.
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

        detail = f"{round(hit_rate * 100)}% hit ratio"

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

        items.append(
            InsightItem(label=row.model, detail=detail, value=value)
        )

    if not items:
        return idle_card(
            CARD_ID,
            CARD_TITLE,
            empty_state=(
                "Your cache hit ratios look healthy. We watch per-model "
                "cache reuse and flag workflows wasting spend re-creating "
                "cache that could be read."
            ),
        )

    if severity == "action" or severity == "warning":
        if below_action > 0:
            headline = (
                f"Cache hit ratio below 30% on {below_action} model(s)"
            )
        else:
            headline = f"Low cache reuse on {warn_count} model(s)"
    else:  # advisory
        headline = f"{info_count} model(s) have improvable cache reuse"

    return Card(
        id=CARD_ID,
        title=CARD_TITLE,
        severity=severity,
        findings_count=len(items),
        headline=headline,
        items=items,
        cta=InsightCta(label="See recommendations", href="/insights/cache"),
    )
