# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Per-vendor API pricing — model rate cards + cost helpers.

Anthropic (T5.5.6) and OpenAI (TM8) each have a top-level
``<vendor>_rates.py`` with an identical shape (``RATE_HISTORY``,
``rates_at``, ``compute_cost_usd``). The unprefixed re-exports
(``compute_cost_usd``, ``rates_at``, ``CURRENT_RATES``, ``RATE_HISTORY``)
remain the **Anthropic** ones for backward compatibility — existing
callers import them straight from ``vargate_telemetry.pricing``.

OpenAI is exposed two ways:
  - the module itself (``from vargate_telemetry.pricing import openai_rates``),
  - vendor-prefixed aliases (``openai_compute_cost_usd``,
    ``openai_rates_at``, ``OPENAI_CURRENT_RATES``, ``OPENAI_RATE_HISTORY``)
    for call sites that want both vendors' helpers in scope.
"""

from vargate_telemetry.pricing import openai_rates
from vargate_telemetry.pricing.anthropic_rates import (
    CURRENT_RATES,
    ModelRates,
    RATE_HISTORY,
    compute_cost_usd,
    rates_at,
)
from vargate_telemetry.pricing.openai_rates import (
    CURRENT_RATES as OPENAI_CURRENT_RATES,
)
from vargate_telemetry.pricing.openai_rates import (
    RATE_HISTORY as OPENAI_RATE_HISTORY,
)
from vargate_telemetry.pricing.openai_rates import (
    compute_cost_usd as openai_compute_cost_usd,
)
from vargate_telemetry.pricing.openai_rates import (
    rates_at as openai_rates_at,
)

__all__ = [
    # Anthropic (unprefixed = backward-compatible default)
    "CURRENT_RATES",
    "ModelRates",
    "RATE_HISTORY",
    "compute_cost_usd",
    "rates_at",
    # OpenAI (TM8)
    "openai_rates",
    "openai_compute_cost_usd",
    "openai_rates_at",
    "OPENAI_CURRENT_RATES",
    "OPENAI_RATE_HISTORY",
]
