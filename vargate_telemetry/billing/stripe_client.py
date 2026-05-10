# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Stripe usage-event dispatch + retry-queue fallback (T2.4).

Module-level dispatcher cache so the metering flush doesn't pay for
`stripe.api_key` setup on every per-tenant call. `StripeDispatcher`
is the real client; tests substitute a stub via `set_dispatcher_for_test`.

`report_usage()` is the only entry point the flush path needs:

  - Reads the tenant's `subscription_item_id` from `tenant_billing`
    under the caller-supplied session (RLS-scoped to the same tenant).
  - If the row is missing (tenant not yet billing-onboarded), the call
    is a silent no-op — T4 is the producer for those rows.
  - For each `(record_type, bucket_start, quantity)` tuple, dispatches
    one `SubscriptionItem.create_usage_record` with an idempotency key
    of `<tenant>:<record_type>:<bucket_iso>` so a flush replay after a
    worker crash never double-charges.
  - On any exception, writes the failed item to `billing_retry` under
    the same session and continues. The flush itself never raises on
    Stripe failure.

Idempotency-key construction is the contract that protects against the
worker-crash-after-Stripe-success-but-before-Postgres-commit race. The
key is deterministic from `(tenant_id, record_type, bucket_start)`, so
the same flush replayed produces the same key, and Stripe returns the
prior result instead of double-creating.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Iterable, Optional, Protocol

from sqlalchemy import text
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


class UsageDispatcher(Protocol):
    """Pluggable Stripe-shaped client. Tests substitute a stub via
    `set_dispatcher_for_test`; production uses `StripeDispatcher`.

    Implementations MUST raise on any failure they can't recover from —
    `report_usage()` catches the exception, writes a `billing_retry`
    row, and continues to the next item.
    """

    def report_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: int,
        timestamp: int,
        idempotency_key: str,
    ) -> None: ...


class StripeDispatcher:
    """Real Stripe dispatcher. Lazy-imports `stripe` so that test
    environments without the package can still import this module.
    """

    def __init__(self, api_key: str) -> None:
        import stripe

        self._stripe = stripe
        self._stripe.api_key = api_key

    def report_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: int,
        timestamp: int,
        idempotency_key: str,
    ) -> None:
        self._stripe.SubscriptionItem.create_usage_record(
            subscription_item_id,
            quantity=quantity,
            timestamp=timestamp,
            action="increment",
            idempotency_key=idempotency_key,
        )


_dispatcher: Optional[UsageDispatcher] = None


def _get_dispatcher() -> UsageDispatcher:
    """Return the cached dispatcher, creating a StripeDispatcher on
    first use. Test-mode key only — production wiring lands in a later
    sprint.
    """
    global _dispatcher
    if _dispatcher is None:
        api_key = os.environ.get("STRIPE_API_KEY_TEST")
        if not api_key:
            raise RuntimeError(
                "STRIPE_API_KEY_TEST is not set; "
                "call set_dispatcher_for_test() or configure the env"
            )
        _dispatcher = StripeDispatcher(api_key=api_key)
    return _dispatcher


def set_dispatcher_for_test(d: Optional[UsageDispatcher]) -> None:
    """Substitute a stub dispatcher. Pass `None` to reset to lazy init."""
    global _dispatcher
    _dispatcher = d


def _idempotency_key(
    tenant_id: str, record_type: str, bucket_start: datetime
) -> str:
    return f"{tenant_id}:{record_type}:{bucket_start.isoformat()}"


def report_usage(
    session: Session,
    tenant_id: str,
    items: Iterable[tuple[str, datetime, int]],
) -> None:
    """Push each (record_type, bucket_start, quantity) to Stripe.

    Silent no-op if the tenant has no `tenant_billing` row. Each
    dispatch failure is caught and persisted to `billing_retry` under
    the caller's session — the flush never raises on Stripe trouble.
    """
    items_list = list(items)
    if not items_list:
        return

    row = session.execute(
        text(
            "SELECT subscription_item_id FROM tenant_billing "
            "WHERE tenant_id = :t"
        ),
        {"t": tenant_id},
    ).first()
    if row is None:
        _log.info(
            "billing: no tenant_billing row for %s — skipping Stripe dispatch",
            tenant_id,
        )
        return

    subscription_item_id = row.subscription_item_id
    dispatcher = _get_dispatcher()

    for record_type, bucket_start, quantity in items_list:
        try:
            dispatcher.report_usage(
                subscription_item_id=subscription_item_id,
                quantity=quantity,
                timestamp=int(bucket_start.timestamp()),
                idempotency_key=_idempotency_key(
                    tenant_id, record_type, bucket_start
                ),
            )
        except Exception as exc:
            _log.warning(
                "billing: Stripe dispatch failed for %s/%s@%s: %s",
                tenant_id,
                record_type,
                bucket_start.isoformat(),
                exc,
            )
            session.execute(
                text(
                    "INSERT INTO billing_retry "
                    "(tenant_id, record_type, bucket_start, quantity, "
                    "last_error, attempts) "
                    "VALUES (:tenant_id, :record_type, :bucket_start, "
                    ":quantity, :err, 1)"
                ),
                {
                    "tenant_id": tenant_id,
                    "record_type": record_type,
                    "bucket_start": bucket_start,
                    "quantity": quantity,
                    "err": str(exc)[:1000],
                },
            )
