# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Billing dispatch — Stripe usage events from the metering flush (T2.4).

The flush task calls `report_usage(session, tenant_id, items)` after
each per-tenant UPSERT. We push one `SubscriptionItem.create_usage_record`
call per `(record_type, bucket_start)` entry and fall back to the
`billing_retry` queue on any exception, all under the caller-supplied
session so the bookkeeping is atomic with the usage_records write.
"""

from vargate_telemetry.billing.stripe_client import (
    StripeDispatcher,
    UsageDispatcher,
    report_usage,
    set_dispatcher_for_test,
)

__all__ = [
    "StripeDispatcher",
    "UsageDispatcher",
    "report_usage",
    "set_dispatcher_for_test",
]
