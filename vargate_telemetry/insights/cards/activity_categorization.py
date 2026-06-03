# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Activity categorization card (TM7) — placeholder stub.

This card will classify MCP-captured turns into topic areas (code,
writing, research, ops) and surface both on each user detail page and
in aggregate here. The classification engine that produces those
labels ships in TM5, so until then this card has nothing real to
analyse.

It therefore returns a finding-free :func:`idle_card`: ``findings_count``
is 0, so the UI renders the ``empty_state`` (and ignores the
``headline``). No DB queries are issued — there is no captured-turn
classification to read yet. When TM5 lands the classifier, this module
grows a real ``build_card`` that queries the labelled turns; the
``CARD_ID`` / ``CARD_TITLE`` contract stays unchanged so the registry
slot does not move.
"""

from __future__ import annotations

from vargate_telemetry.insights.models import Card, idle_card

CARD_ID = "activity_categorization"
CARD_TITLE = "Activity categorization"


def build_card(tenant_id: str, window: str) -> Card:
    """Return the placeholder idle card for activity categorization.

    A pure stub until the TM5 classification engine exists: no spend
    or telemetry is read. ``findings_count`` is 0, so the frontend
    shows the ``empty_state`` describing what this card will become.
    """
    return idle_card(
        CARD_ID,
        CARD_TITLE,
        empty_state=(
            "Classification of MCP-captured turns into topic areas "
            "(code, writing, research, ops). It will surface on each "
            "user detail page and aggregate here. The classification "
            "engine ships in TM5."
        ),
        headline="Coming next release",
    )
