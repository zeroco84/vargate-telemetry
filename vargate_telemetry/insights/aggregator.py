# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insights aggregator (TM7) — run every card, isolate failures.

:func:`build_insights` walks :data:`CARD_MODULES` in display order and
calls each module's ``build_card(tenant_id, window)``. A card that
raises must never 500 the whole page — the failure is logged and that
card degrades to a graceful idle card (built from the module's
``CARD_ID`` / ``CARD_TITLE``) so the rest of the column still renders.

This is the one place that imports the registry (and therefore the
card modules), so a missing card module surfaces here at first call
rather than at app startup.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from vargate_telemetry.insights.models import (
    Card,
    InsightsResponse,
    idle_card,
)
from vargate_telemetry.insights.registry import CARD_MODULES

_log = logging.getLogger(__name__)


def build_insights(tenant_id: str, window: str) -> InsightsResponse:
    """Build the full Insights payload for a tenant + window.

    Each card is built independently; an exception in one card is
    logged and replaced with an idle "temporarily unavailable" card
    so a single failing analysis can't blank the page. ``refreshed_at``
    is timezone-aware UTC.
    """
    cards: list[Card] = []
    for mod in CARD_MODULES:
        try:
            cards.append(mod.build_card(tenant_id, window))
        except Exception:
            _log.exception(
                "insight card %r failed to build for tenant %r (window=%r)",
                getattr(mod, "CARD_ID", mod.__name__),
                tenant_id,
                window,
            )
            cards.append(
                idle_card(
                    mod.CARD_ID,
                    mod.CARD_TITLE,
                    empty_state="This analysis is temporarily unavailable.",
                )
            )

    return InsightsResponse(
        window=window,
        refreshed_at=datetime.now(timezone.utc),
        cards=cards,
    )
