# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for budget period window math (TM3 Phase B2).

Pure-Python — no DB. Pins the boundary cases:
  - daily window covers exactly one UTC day
  - weekly window covers Monday-to-Monday (ISO weeks)
  - monthly window covers 1st-to-1st even across year boundary
  - naive datetimes are rejected
  - unknown periods are rejected
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from vargate_telemetry.budgets.period import (
    current_period_window,
    days_in_period,
)


def test_daily_window_covers_one_utc_day() -> None:
    now = datetime(2026, 5, 14, 18, 32, 7, tzinfo=timezone.utc)
    w = current_period_window("daily", now=now)
    assert w.start == datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)
    assert w.start_date == date(2026, 5, 14)
    assert days_in_period(w) == 1


def test_daily_window_at_utc_midnight_belongs_to_the_starting_day() -> None:
    # End is exclusive; the very-first-instant of a day is INSIDE that
    # day's window, not the previous day's. Pins the half-open contract.
    now = datetime(2026, 5, 14, 0, 0, 0, tzinfo=timezone.utc)
    w = current_period_window("daily", now=now)
    assert w.start == now
    assert w.end == datetime(2026, 5, 15, 0, 0, 0, tzinfo=timezone.utc)


def test_weekly_window_starts_monday_ends_next_monday() -> None:
    # 2026-05-14 is a Thursday; the Monday at-or-before is 2026-05-11.
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    w = current_period_window("weekly", now=now)
    assert w.start == datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 5, 18, 0, 0, 0, tzinfo=timezone.utc)
    assert w.start_date == date(2026, 5, 11)
    assert days_in_period(w) == 7


def test_weekly_window_on_a_monday_belongs_to_that_monday_not_the_previous() -> None:
    now = datetime(2026, 5, 11, 9, 0, 0, tzinfo=timezone.utc)
    w = current_period_window("weekly", now=now)
    assert w.start == datetime(2026, 5, 11, 0, 0, 0, tzinfo=timezone.utc)


def test_monthly_window_spans_the_full_calendar_month() -> None:
    now = datetime(2026, 5, 14, 18, 0, 0, tzinfo=timezone.utc)
    w = current_period_window("monthly", now=now)
    assert w.start == datetime(2026, 5, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2026, 6, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert days_in_period(w) == 31


def test_monthly_window_in_december_rolls_to_january_next_year() -> None:
    # Year boundary — easy to off-by-one.
    now = datetime(2026, 12, 31, 23, 59, 59, tzinfo=timezone.utc)
    w = current_period_window("monthly", now=now)
    assert w.start == datetime(2026, 12, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)


def test_monthly_window_in_february_handles_leap_year_correctly() -> None:
    # Feb 2028 is a leap year (29 days). The window must end on Mar 1.
    now = datetime(2028, 2, 15, 12, 0, 0, tzinfo=timezone.utc)
    w = current_period_window("monthly", now=now)
    assert w.start == datetime(2028, 2, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert w.end == datetime(2028, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    assert days_in_period(w) == 29


def test_naive_datetime_is_rejected() -> None:
    naive = datetime(2026, 5, 14, 12, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="tz-aware"):
        current_period_window("daily", now=naive)


def test_unknown_period_is_rejected() -> None:
    now = datetime(2026, 5, 14, 12, 0, 0, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="unknown period"):
        current_period_window("quarterly", now=now)  # type: ignore[arg-type]


def test_default_now_uses_current_utc_time() -> None:
    # No explicit `now` — should still produce a valid daily window
    # containing the actual now. Loose check; just confirms no crash.
    w = current_period_window("daily")
    now = datetime.now(tz=timezone.utc)
    assert w.start <= now < w.end
