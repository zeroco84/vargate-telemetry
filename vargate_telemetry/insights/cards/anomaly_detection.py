# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Anomaly detection insight card (TM7) — placeholder.

A deliberate stub: the detection engine (off-hours bursts, per-actor
volume spikes, and model-mix shifts versus a rolling 14-day baseline)
ships in TM5. Until then this card always renders as an idle, finding-
free card so the slot is present on the Insights page and the copy
tells the operator what is coming, without ever issuing a DB query.

``build_card`` ignores ``window`` on purpose — there is nothing to
compute yet, so every window resolves to the same idle card.
"""

from __future__ import annotations

from vargate_telemetry.insights.models import Card, idle_card

CARD_ID = "anomaly_detection"
CARD_TITLE = "Anomaly detection"

_EMPTY_STATE = (
    "We are watching for off-hours bursts, per-actor volume spikes, "
    "and model-mix shifts vs your rolling 14-day baseline. The "
    "detection engine ships in TM5."
)
_HEADLINE = "No anomalies in the last 7 days"


def build_card(tenant_id: str, window: str) -> Card:
    """Return the idle placeholder card.

    No analysis runs yet (the engine lands in TM5), so this always
    returns a finding-free idle card regardless of ``tenant_id`` or
    ``window`` — the UI shows ``empty_state``.
    """
    return idle_card(
        CARD_ID,
        CARD_TITLE,
        empty_state=_EMPTY_STATE,
        headline=_HEADLINE,
    )
