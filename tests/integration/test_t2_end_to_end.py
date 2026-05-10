# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""End-to-end T2 pipeline test (T2.5).

Provisions one tenant (DEK + Stripe subscription_item), ingests 1000
synthetic records across two record_types exercising the full seal +
chain + meter path, force-flushes the metering pipeline, and asserts
every accumulator agrees on 1000:

  - telemetry_records row count = 1000
  - usage_records record_count sum = 1000
  - encrypted_secrets row count = 1000 (per-record seal)
  - Stripe stub captured quantity sum = 1000
  - chain verification passes end-to-end
  - billing_retry empty (no Stripe failures)

This is the integration check that proves T1's infra (HSM, RLS, Redis)
and T2's record layer (chain, metering, billing) compose correctly. A
regression here is the first signal that some layer's contract drifted.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text as sql_text

from fixtures.mock_anthropic_records import generate_mock_records


class StubDispatcher:
    """Captures every report_usage call for assertion."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

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


_T2_TABLES = (
    "telemetry_records",
    "usage_records",
    "tenant_billing",
    "billing_retry",
    "encrypted_secrets",
    "tenant_deks",
)


@pytest.fixture
def clean_t2_state() -> None:
    """Empty every T2-touched table + Redis meter keys, reset Stripe stub."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)

    truncate_sql = (
        f"TRUNCATE TABLE {', '.join(_T2_TABLES)} "
        "RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))

    set_dispatcher_for_test(None)

    yield

    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(sql_text(truncate_sql))
    set_dispatcher_for_test(None)


def test_t2_pipeline_1000_records(clean_t2_state: None) -> None:
    """1000 records → chain verifies, all accumulators agree on 1000."""
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.chain import (
        append_telemetry_record,
        verify_telemetry_chain,
    )
    from vargate_telemetry.crypto.seal import (
        provision_tenant_dek,
        seal_secret,
    )
    from vargate_telemetry.db import engine, session_scope
    from vargate_telemetry.metering import flush, increment

    tenant = "test-t2-e2e"
    n_records = 1000

    # --- Provisioning ---
    provision_tenant_dek(tenant)
    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                "INSERT INTO tenant_billing "
                "(tenant_id, subscription_item_id) "
                "VALUES (:t, :s)"
            ),
            {"t": tenant, "s": "si_test_e2e"},
        )

    stub = StubDispatcher()
    set_dispatcher_for_test(stub)

    # --- Ingest 1000 records through the full pipeline ---
    for record in generate_mock_records(n_records):
        seal_secret(
            tenant,
            f"content:{record.external_id}",
            record.content,
        )
        append_telemetry_record(
            tenant,
            record_type=record.record_type,
            source_api=record.source_api,
            external_id=record.external_id,
            occurred_at=record.occurred_at,
            content_hash=record.content_hash,
            record_metadata=record.record_metadata,
        )
        increment(tenant, record.record_type)

    # --- Force the beat-scheduled flush right now ---
    processed = flush()
    # At minimum: one (tenant, bucket, record_type) row per record_type.
    # If the loop straddled a minute boundary, more — but never fewer.
    assert processed >= 2

    # --- Chain integrity end-to-end ---
    result = verify_telemetry_chain(tenant)
    assert result.valid, f"chain verification failed: {result!r}"
    assert result.record_count == n_records

    # --- Postgres accumulators agree ---
    with session_scope(tenant) as s:
        tr_count = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM telemetry_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar()
        usage_sum = s.execute(
            sql_text(
                "SELECT SUM(record_count) FROM usage_records "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar()
        secret_count = s.execute(
            sql_text(
                "SELECT COUNT(*) FROM encrypted_secrets "
                "WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar()

    assert tr_count == n_records
    assert usage_sum == n_records
    assert secret_count == n_records

    # --- Stripe stub captured every count exactly once ---
    assert sum(c["quantity"] for c in stub.calls) == n_records
    # Idempotency keys are unique per (record_type, bucket).
    assert len({c["idempotency_key"] for c in stub.calls}) == len(stub.calls)

    # --- No dispatch failures landed in the retry queue ---
    with engine.connect() as conn:
        retry_count = conn.execute(
            sql_text("SELECT COUNT(*) FROM billing_retry")
        ).scalar()
    assert retry_count == 0
