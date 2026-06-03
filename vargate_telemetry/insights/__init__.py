# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insights package (TM7) — the dashboard's "what's going on" surface.

The Insights page renders a column of cards, each a self-contained
analysis over a tenant's captured Admin-API usage. A card has a
severity (idle → advisory → warning → action), a finding count, a
short headline, and a few line items; when it has nothing to say it
shows an empty-state instead.

Layering
--------

- :mod:`vargate_telemetry.insights.models` — the wire shapes (Card,
  InsightItem, InsightsResponse) plus the severity ladder helpers.
- :mod:`vargate_telemetry.insights.spend_data` — the shared SQL /
  cost primitives every card draws from (daily spend, model share,
  workspace attribution, month-end forecast).
- :mod:`vargate_telemetry.insights.cards` — one module per card,
  each exposing ``CARD_ID`` / ``CARD_TITLE`` / ``build_card``.
- :mod:`vargate_telemetry.insights.registry` — the ordered list of
  card modules (display order).
- :mod:`vargate_telemetry.insights.aggregator` — runs every card,
  isolating failures so one bad card can never 500 the page.

Imports here are intentionally side-effect-free; the registry pulls
the card modules in at runtime so this package import stays cheap.
"""
