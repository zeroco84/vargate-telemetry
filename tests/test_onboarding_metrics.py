# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for T4.7 onboarding Prometheus metrics.

Each test exercises a real onboarding code path (a route, a pull) and
asserts the matching Prometheus instrument observed a sample. Since
the default `prometheus_client.REGISTRY` is process-global, we read
the sample count for the relevant label both before and after the
action and assert it incremented — this lets the tests run in any
order without cross-pollution.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from sqlalchemy import text as sql_text


os.environ["JWT_SIGNING_KEY"] = (
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b"
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def client() -> TestClient:
    from vargate_telemetry.api.app import app

    return TestClient(app)


@pytest.fixture
def clean_metrics_state() -> Iterator[None]:
    """Reset Prometheus-adjacent state: the Redis SETNX guard keys that
    prevent the time-to-first-pull histogram from double-observing, and
    the DB tables the metric assertions read.

    Note: we do NOT reset the Prometheus REGISTRY counters — those are
    process-global, and the tests read before/after deltas so absolute
    values don't matter.
    """
    from vargate_telemetry.api.onboarding import (
        set_async_result_factory_for_test,
        set_client_factory_for_test,
        set_task_dispatcher_for_test,
        set_tenant_id_generator_for_test,
    )
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:metrics:onboarding:*"):
        r.delete(key)

    truncate_sql = sql_text(
        "TRUNCATE TABLE telemetry_records, encrypted_secrets, "
        "tenant_deks, sessions, users, tenants, pull_state "
        "RESTART IDENTITY CASCADE"
    )
    with engine.begin() as conn:
        conn.execute(truncate_sql)

    set_client_factory_for_test(None)
    set_tenant_id_generator_for_test(None)
    set_task_dispatcher_for_test(None)
    set_async_result_factory_for_test(None)

    yield

    for key in r.scan_iter("vargate:metrics:onboarding:*"):
        r.delete(key)
    with engine.begin() as conn:
        conn.execute(truncate_sql)
    set_client_factory_for_test(None)
    set_tenant_id_generator_for_test(None)
    set_task_dispatcher_for_test(None)
    set_async_result_factory_for_test(None)


# ───────────────────────────────────────────────────────────────────────────
# Test helpers
# ───────────────────────────────────────────────────────────────────────────


def _metric_sample(name: str, label_filter: dict[str, str] | None = None) -> float:
    """Return the current value of a Prometheus sample by name + labels.

    For histograms, pass `name="<base>_count"` to read total observation
    count, or `name="<base>_sum"` for the running sum, or
    `name="<base>_bucket"` plus a `le` label for individual buckets.

    Returns 0.0 if the sample isn't registered yet (e.g. metric exists
    but no observation has been made for that label combination).
    """
    needle = label_filter or {}
    for family in REGISTRY.collect():
        for sample in family.samples:
            if sample.name != name:
                continue
            if all(sample.labels.get(k) == v for k, v in needle.items()):
                return float(sample.value)
    return 0.0


def _bearer_token_for(
    user_id: uuid.UUID,
    email: str = "metrics@example.com",
    tenant_id: str | None = None,
) -> str:
    from vargate_telemetry.auth.jwt import issue_session_jwt

    return issue_session_jwt(
        user_id=str(user_id),
        email=email,
        sso_provider="google",
        tenant_id=tenant_id,
    )


def _create_test_user(
    email: str = "metrics@example.com",
    tenant_id: str | None = None,
    sso_sign_in_at: datetime | None = None,
) -> uuid.UUID:
    """INSERT a user row with optional sso_sign_in_at (T4.7)."""
    from vargate_telemetry.db import engine

    user_uuid = uuid.uuid4()
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, name,
                     tenant_id, sso_sign_in_at)
                VALUES
                    (:id, :email, 'google', :sub, 'Metrics Tester',
                     :tenant_id, :sso_sign_in_at)
                """
            ),
            {
                "id": str(user_uuid),
                "email": email,
                "sub": f"google-sub-{user_uuid.hex[:8]}",
                "tenant_id": tenant_id,
                "sso_sign_in_at": sso_sign_in_at,
            },
        )
    return user_uuid


def _provision_test_tenant(
    tenant_id: str,
    region: str = "us",
    initial_backfill_task_id: str | None = None,
) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants
                    (tenant_id, region, active, billing_status,
                     initial_backfill_task_id)
                VALUES
                    (:t, :r, TRUE, 'trial', :tid)
                """
            ),
            {"t": tenant_id, "r": region, "tid": initial_backfill_task_id},
        )


# Stub admin client that yields a single workspace + a single member,
# enough for validate-key to succeed cleanly.
class _StubWorkspace:
    def __init__(self, name: str) -> None:
        self.name = name


class _StubMember:
    def __init__(self, id: str) -> None:
        self.id = id


class _StubAdminClient:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def list_workspaces(self) -> Iterator[_StubWorkspace]:
        self.calls.append("list_workspaces")
        return iter([_StubWorkspace("MetricsCo")])

    def list_members(self) -> Iterator[_StubMember]:
        self.calls.append("list_members")
        return iter([_StubMember("u1")])


class _StubDispatch:
    def __init__(self, task_id: str) -> None:
        self.id = task_id

    def __call__(self, tenant_id: str, days: int) -> "_StubDispatch":
        return self


# ───────────────────────────────────────────────────────────────────────────
# 1. /metrics endpoint surfaces all three onboarding instruments.
# ───────────────────────────────────────────────────────────────────────────


def test_metrics_endpoint_exposes_onboarding_instruments(
    clean_metrics_state: None,
    client: TestClient,
) -> None:
    """Smoke test: /metrics returns text/plain Prometheus exposition
    and all three onboarding metric names are present (even with zero
    observations, because they were pre-touched at module import
    time)."""
    response = client.get("/metrics")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    body = response.text
    assert "vargate_onboarding_step_seconds" in body
    assert "vargate_onboarding_time_to_first_pull_seconds" in body
    assert "vargate_onboarding_completion_total" in body


# ───────────────────────────────────────────────────────────────────────────
# 2. test_step_duration_recorded_on_successful_route
# ───────────────────────────────────────────────────────────────────────────


def test_step_duration_recorded_on_successful_route(
    clean_metrics_state: None,
    client: TestClient,
) -> None:
    """Three of the four onboarding routes are exercised end-to-end
    (sso is covered by the auth tests). After each call, the matching
    `step` label's histogram count must have incremented by 1."""
    from vargate_telemetry.api.onboarding import (
        set_client_factory_for_test,
        set_task_dispatcher_for_test,
        set_tenant_id_generator_for_test,
    )

    # ── validate-key ────────────────────────────────────────────────
    before = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "validate-key"}
    )
    set_client_factory_for_test(lambda _k: _StubAdminClient())
    user_uuid = _create_test_user(email="step-validate@example.com")
    response = client.post(
        "/onboarding/validate-key",
        json={"admin_key": "sk-ant-admin01-metrics-key-XXXXXXXXXXXX"},
        headers={"Authorization": f"Bearer {_bearer_token_for(user_uuid)}"},
    )
    assert response.status_code == 200, response.text
    after = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "validate-key"}
    )
    assert after == before + 1, (
        f"validate-key histogram did not advance: {before} → {after}"
    )

    # ── select-region ──────────────────────────────────────────────
    before = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "select-region"}
    )
    fixed_tenant = "tnt_us_metrics_step_001"
    set_tenant_id_generator_for_test(lambda _r: fixed_tenant)
    response = client.post(
        "/onboarding/select-region",
        json={
            "region": "us",
            "admin_key": "sk-ant-admin01-metrics-key-XXXXXXXXXXXX",
        },
        headers={"Authorization": f"Bearer {_bearer_token_for(user_uuid)}"},
    )
    assert response.status_code == 200, response.text
    after = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "select-region"}
    )
    assert after == before + 1, (
        f"select-region histogram did not advance: {before} → {after}"
    )

    # ── start-backfill ─────────────────────────────────────────────
    # The select-region call above set users.tenant_id and reissued
    # the JWT; the *next* request must carry a bearer that has the
    # bound tenant_id claim. We don't have the new cookie here, so
    # re-mint a token manually.
    bearer_with_tenant = _bearer_token_for(
        user_uuid, tenant_id=fixed_tenant
    )
    set_task_dispatcher_for_test(_StubDispatch("celery-task-step-001"))

    before_step = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "start-backfill"}
    )
    before_completed = _metric_sample(
        "vargate_onboarding_completion_total", {"outcome": "completed"}
    )
    response = client.post(
        "/onboarding/start-backfill",
        json={"tenant_id": fixed_tenant, "days": 90},
        headers={"Authorization": f"Bearer {bearer_with_tenant}"},
    )
    assert response.status_code == 200, response.text
    after_step = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "start-backfill"}
    )
    after_completed = _metric_sample(
        "vargate_onboarding_completion_total", {"outcome": "completed"}
    )
    assert after_step == before_step + 1, (
        f"start-backfill histogram did not advance: "
        f"{before_step} → {after_step}"
    )
    assert after_completed == before_completed + 1, (
        f"completion counter did not advance on successful start-backfill: "
        f"{before_completed} → {after_completed}"
    )

    # ── /metrics surfaces the new samples ──────────────────────────
    body = client.get("/metrics").text
    assert 'step="validate-key"' in body
    assert 'step="select-region"' in body
    assert 'step="start-backfill"' in body


# ───────────────────────────────────────────────────────────────────────────
# 3. test_step_duration_not_recorded_on_failure_path
#    (Defensive bonus: a 4xx must NOT increment the histogram.)
# ───────────────────────────────────────────────────────────────────────────


def test_step_duration_not_recorded_on_failure_path(
    clean_metrics_state: None,
    client: TestClient,
) -> None:
    """An invalid-key 400 must skip the observation — the brief says
    'record their step duration on success' and the context manager's
    `else` clause is what enforces that. This test locks the contract."""
    from vargate_telemetry.api.onboarding import set_client_factory_for_test

    class _AuthFailClient:
        def list_workspaces(self):
            request = httpx.Request("GET", "https://api.anthropic.com/x")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError(
                "401", request=request, response=response
            )

        def list_members(self):
            return iter([])

    set_client_factory_for_test(lambda _k: _AuthFailClient())
    user_uuid = _create_test_user(email="step-fail@example.com")

    before = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "validate-key"}
    )
    response = client.post(
        "/onboarding/validate-key",
        json={"admin_key": "sk-ant-admin01-invalid-XXXXXXXXXXXXXXXX"},
        headers={"Authorization": f"Bearer {_bearer_token_for(user_uuid)}"},
    )
    assert response.status_code == 400, response.text
    after = _metric_sample(
        "vargate_onboarding_step_seconds_count", {"step": "validate-key"}
    )
    assert after == before, (
        f"failure path should NOT have advanced the histogram: "
        f"{before} → {after}"
    )


# ───────────────────────────────────────────────────────────────────────────
# 4. test_time_to_first_pull_measured_correctly
# ───────────────────────────────────────────────────────────────────────────


def test_time_to_first_pull_measured_correctly(
    clean_metrics_state: None,
) -> None:
    """Fixture: a tenant + a user with sso_sign_in_at = now - 90s.
    Run the pull with a stub Anthropic client that returns one usage
    bucket. The first-pull histogram count goes from N → N+1 AND a
    bucket in the [60, 300] range gets the observation."""
    from vargate_telemetry.anthropic import AnthropicAdminClient
    from vargate_telemetry.tasks.pull_admin import _pull_admin_for_tenant

    tenant_id = "tnt_us_first_pull_001"
    sign_in_at = datetime.now(timezone.utc) - timedelta(seconds=90)
    _provision_test_tenant(tenant_id)
    _create_test_user(
        email="ttfp@example.com",
        tenant_id=tenant_id,
        sso_sign_in_at=sign_in_at,
    )

    def _handler(request: httpx.Request) -> httpx.Response:
        # Single bucket: returns one UsageBucket the chain will accept.
        # Window straddles a 30-second slice ending 30s ago.
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "starting_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=2)
                        ).isoformat(),
                        "ending_at": (
                            datetime.now(timezone.utc) - timedelta(minutes=1)
                        ).isoformat(),
                        "results": [
                            {
                                "uncached_input_tokens": 100,
                                "cached_input_tokens": 0,
                                "cache_creation_input_tokens": 0,
                                "output_tokens": 25,
                                "context_window": "0-200k",
                                "service_tier": "standard",
                            }
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    stub_client = AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(_handler),
    )

    before_count = _metric_sample(
        "vargate_onboarding_time_to_first_pull_seconds_count"
    )

    result = _pull_admin_for_tenant(tenant_id, client=stub_client)
    assert result["inserted"] >= 1, (
        f"expected at least one telemetry row, got {result}"
    )

    after_count = _metric_sample(
        "vargate_onboarding_time_to_first_pull_seconds_count"
    )
    assert after_count == before_count + 1, (
        f"time-to-first-pull histogram did not advance: "
        f"{before_count} → {after_count}"
    )

    # The observation was ~90 seconds, so it should land in the
    # le="120" bucket cumulative count (which covers everything ≤ 120s
    # since the buckets are: 30, 60, 90, 120, 300, 600).
    bucket_120 = _metric_sample(
        "vargate_onboarding_time_to_first_pull_seconds_bucket",
        {"le": "120.0"},
    )
    assert bucket_120 >= 1, (
        f"observation should be ≤120s, but le=120 bucket count is {bucket_120}"
    )


# ───────────────────────────────────────────────────────────────────────────
# 5. test_first_pull_is_observed_only_once_per_tenant
# ───────────────────────────────────────────────────────────────────────────


def test_first_pull_is_observed_only_once_per_tenant(
    clean_metrics_state: None,
) -> None:
    """Calling the pull twice for the same tenant must NOT double-
    observe. The Redis SETNX guard inside `observe_first_pull_if_first`
    is what enforces this; without it every successful pull would
    contribute a noisy sample."""
    from vargate_telemetry.anthropic import AnthropicAdminClient
    from vargate_telemetry.tasks.pull_admin import _pull_admin_for_tenant

    tenant_id = "tnt_us_first_pull_once"
    sign_in_at = datetime.now(timezone.utc) - timedelta(seconds=45)
    _provision_test_tenant(tenant_id)
    _create_test_user(
        email="once@example.com",
        tenant_id=tenant_id,
        sso_sign_in_at=sign_in_at,
    )

    # First call returns a row; the second returns no rows (so we
    # only observe once even though we ran two pulls).
    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "starting_at": (
                                datetime.now(timezone.utc)
                                - timedelta(minutes=2)
                            ).isoformat(),
                            "ending_at": (
                                datetime.now(timezone.utc)
                                - timedelta(minutes=1)
                            ).isoformat(),
                            "results": [
                                {
                                    "uncached_input_tokens": 50,
                                    "cached_input_tokens": 0,
                                    "cache_creation_input_tokens": 0,
                                    "output_tokens": 12,
                                    "context_window": "0-200k",
                                    "service_tier": "standard",
                                }
                            ],
                        }
                    ],
                    "has_more": False,
                },
            )
        return httpx.Response(200, json={"data": [], "has_more": False})

    before = _metric_sample(
        "vargate_onboarding_time_to_first_pull_seconds_count"
    )

    stub = AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(_handler),
    )
    _pull_admin_for_tenant(tenant_id, client=stub)

    # Second pull, fresh client. Some rows may dedup; either way the
    # metric must not re-observe.
    stub2 = AnthropicAdminClient(
        api_key="test-key",
        base_url="https://api.test",
        min_wait=0.0,
        transport=httpx.MockTransport(_handler),
    )
    _pull_admin_for_tenant(tenant_id, client=stub2)

    after = _metric_sample(
        "vargate_onboarding_time_to_first_pull_seconds_count"
    )
    assert after == before + 1, (
        f"first-pull histogram should have observed exactly once "
        f"across two pulls, but got: {before} → {after}"
    )
