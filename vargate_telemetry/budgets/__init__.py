# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budget evaluation primitives (TM3 Phase B).

This module is the shared core between two callers:

1. ``api.budgets`` — the CRUD endpoints. The detail endpoint needs
   current-period spend + ratio for each budget so the UI can
   render a progress bar without re-implementing the math.

2. ``tasks.evaluate_budgets`` — the 15-minute Celery beat task that
   walks every tenant's live budgets, computes current spend, and
   fires alerts when the 70 / 85 / 100 % thresholds are crossed.

Putting the period math + spend SQL in one module guarantees the
two surfaces never disagree.
"""

from vargate_telemetry.budgets.period import (
    Period,
    PeriodWindow,
    current_period_window,
)
from vargate_telemetry.budgets.spend import (
    ALERT_THRESHOLDS,
    compute_spend_in_window,
)

__all__ = [
    "ALERT_THRESHOLDS",
    "Period",
    "PeriodWindow",
    "compute_spend_in_window",
    "current_period_window",
]
