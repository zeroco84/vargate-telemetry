# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Budget period window math (TM3 Phase B).

A budget's spend ratio is always evaluated against the **current
period** the budget happens to be in. "Daily" buckets reset every
UTC midnight; "weekly" every UTC Monday; "monthly" on the 1st UTC.

The end-of-window is exclusive — the evaluator queries
``occurred_at >= start AND occurred_at < end`` so a record at
``end`` belongs to the next period, not this one. This matches the
half-open convention every other date-range query in the codebase
uses.

UTC-only. We never localize period boundaries to a tenant's
timezone — the alert "your monthly budget hit 70%" tracks the
billing calendar Anthropic's spending is denominated in (USD,
midnight UTC bucket starts), and split-timezone counting would
make the threshold-fired moment depend on observer.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Literal

# String enum for the SQL CHECK constraint. Keep these literal
# strings in sync with the ``ck_budgets_period`` constraint in
# migration 0019.
Period = Literal["daily", "weekly", "monthly"]


@dataclass(frozen=True)
class PeriodWindow:
    """A half-open ``[start, end)`` window in UTC.

    ``start_date`` is the calendar date the window opens on
    (00:00:00 UTC of that date == ``start``); the period row is
    indexed by this date in the dedup constraint
    ``UNIQUE (budget_id, period_start, threshold_crossed)``.
    """

    start: datetime
    end: datetime

    @property
    def start_date(self) -> date:
        return self.start.date()


def _utc_midnight(d: date) -> datetime:
    """00:00:00 UTC on the given date, tz-aware."""
    return datetime.combine(d, time(0, 0, 0), tzinfo=timezone.utc)


def _start_of_month(d: date) -> date:
    return date(d.year, d.month, 1)


def _start_of_next_month(d: date) -> date:
    if d.month == 12:
        return date(d.year + 1, 1, 1)
    return date(d.year, d.month + 1, 1)


def _start_of_iso_week(d: date) -> date:
    """The Monday at-or-before ``d``.

    ``d.weekday()`` is 0 for Monday → 6 for Sunday, so subtracting
    that many days lands on Monday. ISO weeks start Monday
    everywhere we care about; matching Python's stdlib default.
    """
    return d - timedelta(days=d.weekday())


def current_period_window(
    period: Period, now: datetime | None = None
) -> PeriodWindow:
    """Return the half-open window the given period is currently in.

    Parameters
    ----------
    period:
        ``"daily"`` | ``"weekly"`` | ``"monthly"`` — matches the
        ``ck_budgets_period`` CHECK constraint.
    now:
        UTC timestamp used as "current". Defaults to ``datetime.now
        (tz=utc)``. Tests pass this explicitly to pin against a
        known moment.

    Returns
    -------
    PeriodWindow with tz-aware UTC ``start`` and ``end`` such that
    ``start <= now < end``.

    Raises
    ------
    ValueError if ``period`` is not one of the three literal values.
    """
    if now is None:
        now = datetime.now(tz=timezone.utc)
    elif now.tzinfo is None:
        # Reject naive datetimes — the rest of the codebase is
        # tz-aware and a silent comparison-bug would skew period
        # boundaries by the caller's local TZ. Fail loud.
        raise ValueError(
            "current_period_window requires a tz-aware 'now'; got naive."
        )

    today = now.date()

    if period == "daily":
        return PeriodWindow(
            start=_utc_midnight(today),
            end=_utc_midnight(today + timedelta(days=1)),
        )

    if period == "weekly":
        monday = _start_of_iso_week(today)
        return PeriodWindow(
            start=_utc_midnight(monday),
            end=_utc_midnight(monday + timedelta(days=7)),
        )

    if period == "monthly":
        first = _start_of_month(today)
        next_first = _start_of_next_month(today)
        return PeriodWindow(
            start=_utc_midnight(first),
            end=_utc_midnight(next_first),
        )

    raise ValueError(
        f"unknown period {period!r}; "
        "expected one of 'daily', 'weekly', 'monthly'"
    )


def days_in_period(window: PeriodWindow) -> int:
    """Length of the window in whole days.

    Used for the budget-pacing display (e.g., "day 14 of 30") in
    the detail view. The end is exclusive so a 30-day monthly
    window in November returns 30, not 31.
    """
    delta = window.end - window.start
    return delta.days
