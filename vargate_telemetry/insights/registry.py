# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insight card registry (TM7) — the display order.

``CARD_MODULES`` is the single source of truth for which cards exist
and in what order the Insights page renders them. Each entry is a
module exposing ``CARD_ID`` / ``CARD_TITLE`` / ``build_card``; the
aggregator walks this list in order.

The six card modules are authored by a parallel task and may not
exist yet at the moment this file lands — that is fine. This module
is only imported at runtime (by the aggregator / API route), never at
package-import time, so a missing card module surfaces as an
ImportError when the Insights endpoint is first hit, not at startup.
"""

from __future__ import annotations

from vargate_telemetry.insights.cards import (
    activity_categorization,
    anomaly_detection,
    cache_efficiency,
    cost_forecasting,
    model_mix,
    workspace_attribution,
)

# Display order, top-to-bottom on the Insights page.
CARD_MODULES = [
    cache_efficiency,
    cost_forecasting,
    anomaly_detection,
    activity_categorization,
    model_mix,
    workspace_attribution,
]
