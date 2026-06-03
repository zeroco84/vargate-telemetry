# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Insight card modules (TM7).

One module per card. Each exposes module-level ``CARD_ID`` and
``CARD_TITLE`` plus ``build_card(tenant_id, window) -> Card``. The
display order is owned by :mod:`vargate_telemetry.insights.registry`,
not by import order here.
"""
