# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the Stripe usage-dispatch hook on the metering flush (T2.4)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import text as sql_text


class StubDispatcher:
    """Captures every report_usage call. Optionally raises on each call."""

    def __init__(self, raise_on_call: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.raise_on_call = raise_on_call

    def report_usage(
        self,
        *,
        subscription_item_id: str,
        quantity: int,
        timestamp: int,
        idempotency_key: str,
    ) -> None:
        self.calls.append(
            {
                "subscription_item_id": subscription_item_id,
                "quantity": quantity,
                "timestamp": timestamp,
                "idempotency_key": idempotency_key,
            }
        )
        if self.raise_on_call:
            raise RuntimeError("stripe-down")


@pytest.fixture
def clean_billing_state() -> None:
    """Drop Redis meter keys, truncate billing + usage tables, reset dispatcher."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE usage_records, tenant_billing, billing_retry "
                "RESTART IDENTITY CASCADE"
            )
        )

    set_dispatcher_for_test(None)

    yield

    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "TRUNCATE TABLE usage_records, tenant_billing, billing_retry "
                "RESTART IDENTITY CASCADE"
            )
        )
    set_dispatcher_for_test(None)


def _provision_billing(tenant_id: str, subscription_item_id: str) -> None:
    """Insert a tenant_billing row. T4 will own this for real."""
    from vargate_telemetry.db import session_scope

    with session_scope(tenant_id) as s:
        s.execute(
            sql_text(
                "INSERT INTO tenant_billing (tenant_id, subscription_item_id) "
                "VALUES (:t, :s)"
            ),
            {"t": tenant_id, "s": subscription_item_id},
        )


def test_stripe_test_mode_dispatch(clean_billing_state: None) -> None:
    """100 increments → flush → one Stripe call with quantity=100."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.metering import flush, increment

    tenant = "test-billing-A"
    _provision_billing(tenant, "si_test_AAA")

    stub = StubDispatcher()
    set_dispatcher_for_test(stub)

    for _ in range(100):
        increment(tenant, "usage")

    processed = flush()
    assert processed == 1

    assert len(stub.calls) == 1
    call = stub.calls[0]
    assert call["subscription_item_id"] == "si_test_AAA"
    assert call["quantity"] == 100
    assert call["idempotency_key"].startswith(f"{tenant}:usage:")


def test_stripe_failure_does_not_block_flush(
    clean_billing_state: None,
) -> None:
    """Stripe error during flush → usage_records still commits, retry row written."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine, session_scope
    from vargate_telemetry.metering import flush, increment

    tenant = "test-billing-fail"
    _provision_billing(tenant, "si_test_FAIL")

    stub = StubDispatcher(raise_on_call=True)
    set_dispatcher_for_test(stub)

    for _ in range(7):
        increment(tenant, "usage")

    # Flush returns normally despite the Stripe error.
    assert flush() == 1

    # Stripe was attempted exactly once (one bucket).
    assert len(stub.calls) == 1

    # usage_records committed — durability of the count is non-negotiable.
    with session_scope(tenant) as s:
        usage_row = s.execute(
            sql_text(
                "SELECT record_count FROM usage_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).first()
        assert usage_row is not None
        assert usage_row.record_count == 7

        # billing_retry has the failed dispatch.
        retry_row = s.execute(
            sql_text(
                "SELECT record_type, quantity, last_error, attempts "
                "FROM billing_retry WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).first()
        assert retry_row is not None
        assert retry_row.record_type == "usage"
        assert retry_row.quantity == 7
        assert "stripe-down" in retry_row.last_error
        assert retry_row.attempts == 1

    # Both rows in the same tenant — no cross-tenant leakage via the
    # bootstrap connection either.
    with engine.connect() as conn:
        total_retries = conn.execute(
            sql_text("SELECT COUNT(*) FROM billing_retry")
        ).scalar()
    assert total_retries == 1


def test_no_billing_row_skips_dispatch(clean_billing_state: None) -> None:
    """Tenant without tenant_billing → flush succeeds, no Stripe call attempted."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.metering import flush, increment

    tenant = "test-billing-unprovisioned"
    # NOTE: deliberately not calling _provision_billing.

    class AssertNotCalledDispatcher:
        def report_usage(self, **kwargs: Any) -> None:
            raise AssertionError(
                "dispatcher should not be called for an unprovisioned tenant"
            )

    set_dispatcher_for_test(AssertNotCalledDispatcher())

    for _ in range(5):
        increment(tenant, "usage")

    # Flush commits the usage_records row but never touches Stripe.
    assert flush() == 1
