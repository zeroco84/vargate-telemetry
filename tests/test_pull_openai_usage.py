# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the TM8 OpenAI Admin usage pull (``pull_openai_usage``).

Mirrors ``test_pull_code_analytics.py`` / ``test_pull_admin.py`` —
``httpx.MockTransport`` for deterministic responses, ``min_wait=0`` so
no real backoff, and a truncate fixture around each test:

  - happy path → grouped usage rows land as telemetry_records with the
    full token breakdown in metadata + a cost estimate that heeds the
    double-count trap;
  - 403 → ``InsufficientScope`` becomes a soft-skip dict (no rows, no
    cursor advance);
  - dedup-only second run → all rows dedup, cursor still advances;
  - empty bucket → one sentinel record per (bucket, modality) so the
    cursor advances and we don't re-pull an empty window forever;
  - cross-vendor attribution → a usage row's ``user_id`` resolves to an
    email (via the ``openai_users`` side table) in
    ``metadata.user_email`` so the alias reconciler can match it.

The OpenAI usage envelope (recon §2): ``{object:"page", data:[bucket],
has_more, next_page}``; each bucket ``{start_time, end_time,
results:[grouped row]}``; ``start_time``/``end_time`` are Unix-epoch
seconds.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable, Iterator

import httpx
import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.openai import OpenAIAdminClient

# A fixed bucket window: 2026-05-11 00:00 → 2026-05-12 00:00 UTC, as
# Unix-epoch seconds (the wire form).
_START = int(datetime(2026, 5, 11, tzinfo=timezone.utc).timestamp())
_END = int(datetime(2026, 5, 12, tzinfo=timezone.utc).timestamp())


def _usage_result(
    *,
    model: str = "gpt-4o-2024-08-06",
    user_id: str | None = "user-alice",
    api_key_id: str | None = "key_alpha",
    project_id: str | None = "proj_alpha",
    input_uncached: int = 80,
    input_cached: int = 20,
    output: int = 50,
) -> dict:
    """One grouped usage result row (recon §2 shape).

    ``input_tokens`` is deliberately the TOTAL (uncached + cached) — the
    double-count trap the normalize must NOT bill directly.
    """
    return {
        "object": "organization.usage.completions.result",
        "project_id": project_id,
        "user_id": user_id,
        "api_key_id": api_key_id,
        "model": model,
        "batch": None,
        "service_tier": None,
        "num_model_requests": 2,
        "input_tokens": input_uncached + input_cached,  # TOTAL
        "input_uncached_tokens": input_uncached,
        "input_cached_tokens": input_cached,
        "output_tokens": output,
        "input_text_tokens": input_uncached,
        "output_text_tokens": output,
        "input_cached_text_tokens": input_cached,
        "input_audio_tokens": 0,
        "input_cached_audio_tokens": 0,
        "output_audio_tokens": 0,
        "input_image_tokens": 0,
        "input_cached_image_tokens": 0,
        "output_image_tokens": 0,
    }


def _bucket(results: list[dict]) -> dict:
    return {
        "object": "bucket",
        "start_time": _START,
        "end_time": _END,
        "results": results,
    }


def _page(buckets: list[dict]) -> dict:
    return {
        "object": "page",
        "data": buckets,
        "has_more": False,
        "next_page": None,
    }


def _modality_handler(
    completions_results: list[dict],
    embeddings_results: list[dict] | None = None,
) -> Callable[[httpx.Request], httpx.Response]:
    """Route by the ``/usage/{modality}`` path so completions +
    embeddings (both pulled per the default modalities) get the right
    payload. Embeddings default to an empty page."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usage/embeddings"):
            return httpx.Response(
                200, json=_page([_bucket(embeddings_results or [])])
            )
        return httpx.Response(
            200, json=_page([_bucket(completions_results)])
        )

    return handler


def _stub_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> OpenAIAdminClient:
    return OpenAIAdminClient(
        api_key="sk-admin-test",
        base_url="https://api.test/v1/organization",
        min_wait=0.0,
        max_wait=0.0,
        wait_multiplier=0.0,
        transport=httpx.MockTransport(handler),
    )


@pytest.fixture
def clean_pull_state() -> Iterator[None]:
    """Empty ingest-touched tables + Redis meter keys around each test.

    Mirrors the ``test_pull_code_analytics`` fixture, plus the OpenAI
    ``openai_users`` side table (the usage attribution test seeds it).
    """
    from vargate_telemetry.billing import set_dispatcher_for_test
    from vargate_telemetry.db import engine
    from vargate_telemetry.metering import _redis

    r = _redis()
    for key in r.scan_iter("vargate:meter:*"):
        r.delete(key)

    truncate_sql = (
        "TRUNCATE TABLE telemetry_records, usage_records, "
        "pull_state, openai_users, user_aliases, users, tenants, "
        "billing_retry, tenant_billing RESTART IDENTITY CASCADE"
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


def _provision_tenant(tenant_id: str) -> None:
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES (:t, 'us', TRUE, 'trial') "
                "ON CONFLICT (tenant_id) DO NOTHING"
            ),
            {"t": tenant_id},
        )


# ───────────────────────────────────────────────────────────────────────────
# Happy path — grouped usage rows → telemetry_records
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_usage_writes_records(clean_pull_state: None) -> None:
    """Two grouped rows (one per modality? no — both on completions)
    land as telemetry_records with source_api='openai_admin_usage', the
    pinned external_id, the full token breakdown in metadata, and a cost
    estimate computed from the uncached+cached split (NOT the raw
    input_tokens total)."""
    from vargate_telemetry.chain import verify_telemetry_chain
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        SOURCE_API_OPENAI_USAGE,
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_write"
    _provision_tenant(tenant)

    results = [
        _usage_result(user_id="user-alice", api_key_id="key_a"),
        _usage_result(user_id="user-bob", api_key_id="key_b"),
    ]
    result = _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler(results))
    )

    assert result["status"] == "ok"
    # 2 completions rows + 1 embeddings sentinel (empty embeddings page).
    assert result["records_pulled"] == 3
    assert result["records_deduped"] == 0

    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                "SELECT external_id, record_type, source_api, "
                "subject_user_id, metadata::text AS meta_json "
                "FROM telemetry_records WHERE tenant_id = :t "
                "ORDER BY chain_seq"
            ),
            {"t": tenant},
        ).all()

    assert len(rows) == 3
    by_eid = {r.external_id: r for r in rows}

    alice_eid = (
        f"openai:{SOURCE_API_OPENAI_USAGE}:{_START}:{_END}:"
        f"gpt-4o-2024-08-06:proj_alpha:key_a:user-alice"
    )
    assert alice_eid in by_eid
    alice = by_eid[alice_eid]
    assert alice.record_type == "usage"
    assert alice.source_api == SOURCE_API_OPENAI_USAGE
    assert alice.subject_user_id == "user-alice"

    meta = json.loads(alice.meta_json)
    # Full token breakdown stored verbatim under `result`.
    assert meta["result"]["input_tokens"] == 100  # the TOTAL
    assert meta["result"]["input_uncached_tokens"] == 80
    assert meta["result"]["input_cached_tokens"] == 20
    assert meta["result"]["input_audio_tokens"] == 0

    # Cost heeds the double-count trap: gpt-4o is $2.50 in / $1.25 cached
    # / $10 out per Mtok. 80 uncached + 20 cached + 50 out:
    #   80/1e6*2.50 + 20/1e6*1.25 + 50/1e6*10 = 0.000725
    # The raw-total trap would give 100/1e6*2.50 + 20*1.25... ≠ this.
    expected = (
        Decimal(80) * Decimal("2.50")
        + Decimal(20) * Decimal("1.25")
        + Decimal(50) * Decimal("10.00")
    ) / Decimal(1_000_000)
    assert Decimal(meta["estimated_cost_usd"]) == expected.quantize(
        Decimal("0.000001")
    )

    # The embeddings sentinel row exists with the all-dash external_id.
    sentinel_eid = (
        f"openai:{SOURCE_API_OPENAI_USAGE}:{_START}:{_END}:-:-:-:-"
    )
    assert sentinel_eid in by_eid

    chain = verify_telemetry_chain(tenant)
    assert chain.valid is True
    assert chain.record_count == 3

    # Cursor advanced.
    with session_scope(tenant) as s:
        cur = s.execute(
            sql_text(
                "SELECT cursor, last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_USAGE},
        ).one()
    assert cur.last_status == "ok"
    assert cur.cursor is not None


def test_pull_openai_usage_cost_estimate_avoids_double_count(
    clean_pull_state: None,
) -> None:
    """Pin the double-count trap explicitly: a row with NO cached tokens
    and the same uncached count must cost LESS than a naive
    raw-``input_tokens`` × input-rate would, because the cached split is
    billed at the cheaper cached rate — never at the full input rate via
    the total."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_dblcount"
    _provision_tenant(tenant)

    # 100 total input = 0 uncached + 100 cached (an all-cache-hit row).
    # Correct cost: 0*2.50 + 100*1.25 + 0 out = 125/1e6 = 0.000125.
    # The trap (billing input_tokens=100 at full rate) would be
    # 100*2.50/1e6 = 0.000250 — exactly double-ish. Assert the cheap one.
    results = [
        _usage_result(
            user_id="user-cache",
            input_uncached=0,
            input_cached=100,
            output=0,
        )
    ]
    _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler(results))
    )

    with session_scope(tenant) as s:
        meta_json = s.execute(
            sql_text(
                "SELECT metadata::text FROM telemetry_records "
                "WHERE tenant_id = :t AND subject_user_id = 'user-cache'"
            ),
            {"t": tenant},
        ).scalar_one()
    cost = Decimal(json.loads(meta_json)["estimated_cost_usd"])
    assert cost == Decimal("0.000125")
    # And explicitly NOT the raw-total trap value.
    assert cost != Decimal("0.000250")


# ───────────────────────────────────────────────────────────────────────────
# 403 soft-skip
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_usage_skips_when_403(clean_pull_state: None) -> None:
    """A scope-limited key 403s → soft-skip dict, no rows, cursor
    untouched."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        SOURCE_API_OPENAI_USAGE,
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_403"
    _provision_tenant(tenant)

    def handler_403(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403, json={"error": {"message": "insufficient permissions"}}
        )

    result = _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(handler_403)
    )
    assert result["status"] == "no_openai_usage_access"
    assert result["records_pulled"] == 0
    assert result["records_deduped"] == 0

    with session_scope(tenant) as s:
        count = s.execute(
            sql_text(
                "SELECT count(*) FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).scalar_one()
        cur = s.execute(
            sql_text(
                "SELECT count(*) FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_USAGE},
        ).scalar_one()
    assert count == 0
    # No cursor row written on the soft-skip (window stays re-pullable).
    assert cur == 0


# ───────────────────────────────────────────────────────────────────────────
# Dedup-only second run → cursor still advances
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_usage_dedup_only_advances_cursor(
    clean_pull_state: None,
) -> None:
    """Second pull of the same window dedups every row but still reports
    status='ok' and advances the cursor (a re-pulled window is a
    fully-ingested window)."""
    from vargate_telemetry.db import engine
    from vargate_telemetry.tasks.pull_openai_usage import (
        SOURCE_API_OPENAI_USAGE,
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_dedup"
    _provision_tenant(tenant)
    results = [_usage_result(user_id="user-alice")]

    first = _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler(results))
    )
    # 1 completions row + 1 embeddings sentinel.
    assert first["records_pulled"] == 2
    assert first["records_deduped"] == 0

    with engine.begin() as conn:
        cursor_after_first = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_USAGE},
        ).scalar_one()

    second = _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler(results))
    )
    assert second["status"] == "ok"
    assert second["records_pulled"] == 0
    assert second["records_deduped"] == 2

    with engine.begin() as conn:
        cursor_after_second = conn.execute(
            sql_text(
                "SELECT cursor FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_USAGE},
        ).scalar_one()

    # Cursor moved forward (each run stamps pull_started = now()).
    assert datetime.fromisoformat(
        cursor_after_second
    ) >= datetime.fromisoformat(cursor_after_first)


# ───────────────────────────────────────────────────────────────────────────
# Empty bucket → sentinel record, cursor advances
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_usage_empty_bucket_emits_sentinel(
    clean_pull_state: None,
) -> None:
    """A bucket with no results (a day with zero usage) still emits a
    sentinel record so the cursor advances and we don't re-pull the
    empty window forever.

    Both modalities cover the SAME window, and the pinned external_id
    omits the modality segment (``…:{start}:{end}:-:-:-:-``), so the two
    sentinels share one external_id: the completions sentinel inserts,
    the embeddings sentinel dedups. That's intentional — one sentinel
    per window is enough to advance the (per-source_api, modality-shared)
    cursor."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        SOURCE_API_OPENAI_USAGE,
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_empty"
    _provision_tenant(tenant)

    # Both modalities return an empty-results bucket for the same window.
    result = _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler([], []))
    )
    assert result["status"] == "ok"
    # One sentinel inserts; the second (same external_id) dedups.
    assert result["records_pulled"] == 1
    assert result["records_deduped"] == 1

    with session_scope(tenant) as s:
        rows = s.execute(
            sql_text(
                "SELECT external_id, metadata::text AS meta "
                "FROM telemetry_records WHERE tenant_id = :t"
            ),
            {"t": tenant},
        ).all()
    assert len(rows) == 1
    sentinel_eid = (
        f"openai:{SOURCE_API_OPENAI_USAGE}:{_START}:{_END}:-:-:-:-"
    )
    assert rows[0].external_id == sentinel_eid
    # Sentinel carries a null result + the modality of whichever pull
    # won the insert (completions, since it's pulled first).
    meta = json.loads(rows[0].meta)
    assert meta["result"] is None
    assert meta["modality"] == "completions"

    # Cursor advanced despite zero real usage.
    with session_scope(tenant) as s:
        status = s.execute(
            sql_text(
                "SELECT last_status FROM pull_state "
                "WHERE tenant_id = :t AND source_api = :s"
            ),
            {"t": tenant, "s": SOURCE_API_OPENAI_USAGE},
        ).scalar_one()
    assert status == "ok"


# ───────────────────────────────────────────────────────────────────────────
# Cross-vendor attribution — user_id resolves to email via openai_users
# ───────────────────────────────────────────────────────────────────────────


def test_pull_openai_usage_resolves_email_for_attribution(
    clean_pull_state: None,
) -> None:
    """When the ``openai_users`` side table maps the row's ``user_id`` to
    an email, the usage record exposes ``metadata.user_email`` (the
    cross-vendor alias-reconciler match key). Without a mapping it
    carries only the raw user_id as ``subject_user_id``."""
    from vargate_telemetry.db import session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        _pull_openai_usage_for_tenant,
    )

    tenant = "tnt_us_oai_usage_attr"
    _provision_tenant(tenant)

    # Seed openai_users so user-alice → alice@example.com (user-bob has
    # no mapping).
    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                "INSERT INTO openai_users "
                "(tenant_id, openai_user_id, email) "
                "VALUES (:t, 'user-alice', 'alice@example.com')"
            ),
            {"t": tenant},
        )

    results = [
        _usage_result(user_id="user-alice", api_key_id="key_a"),
        _usage_result(user_id="user-bob", api_key_id="key_b"),
    ]
    _pull_openai_usage_for_tenant(
        tenant, client=_stub_client(_modality_handler(results))
    )

    with session_scope(tenant) as s:
        alice_meta = s.execute(
            sql_text(
                "SELECT metadata::text FROM telemetry_records "
                "WHERE tenant_id = :t AND subject_user_id = 'user-alice'"
            ),
            {"t": tenant},
        ).scalar_one()
        bob_meta = s.execute(
            sql_text(
                "SELECT metadata::text FROM telemetry_records "
                "WHERE tenant_id = :t AND subject_user_id = 'user-bob'"
            ),
            {"t": tenant},
        ).scalar_one()

    assert json.loads(alice_meta)["user_email"] == "alice@example.com"
    # Bob has no mapping → no user_email key, only the raw user_id.
    assert "user_email" not in json.loads(bob_meta)


def test_pull_openai_usage_email_match_links_alias(
    clean_pull_state: None,
) -> None:
    """End-to-end cross-vendor stitch: an OpenAI usage record whose
    user_id resolves to an email matching an Ogma ``users`` row gets an
    auto-matched ``user_aliases`` link from the reconciler — proving the
    SESSION_SOURCE_APIS extension wired openai_admin_usage in."""
    from vargate_telemetry.db import engine, session_scope
    from vargate_telemetry.tasks.pull_openai_usage import (
        _pull_openai_usage_for_tenant,
    )
    from vargate_telemetry.users import reconcile_aliases_for_tenant

    tenant = "tnt_us_oai_usage_link"
    _provision_tenant(tenant)

    # An Ogma user with the email + the openai_users mapping to it.
    # users has NOT NULL sso_provider / sso_subject_id (natural key);
    # mirror test_user_aliases._provision_user.
    user_uuid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO users "
                "(id, email, sso_provider, sso_subject_id, tenant_id) "
                "VALUES (:id, 'dev@example.com', 'google', :sub, :t)"
            ),
            {"id": user_uuid, "sub": f"sub-{user_uuid}", "t": tenant},
        )
    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                "INSERT INTO openai_users "
                "(tenant_id, openai_user_id, email) "
                "VALUES (:t, 'user-dev', 'dev@example.com')"
            ),
            {"t": tenant},
        )

    _pull_openai_usage_for_tenant(
        tenant,
        client=_stub_client(
            _modality_handler([_usage_result(user_id="user-dev")])
        ),
    )

    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)
        link = s.execute(
            sql_text(
                "SELECT ua.user_id IS NOT NULL AS linked, ua.auto_matched "
                "FROM user_aliases ua "
                "WHERE ua.source_api = 'openai_admin_usage' "
                "  AND ua.source_identifier = 'dev@example.com'"
            )
        ).one()
    assert link.linked is True
    assert link.auto_matched is True


# ───────────────────────────────────────────────────────────────────────────
# Dispatcher (beat fan-out) — region gap fix (TM5 T5.0)
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def dispatch_tenants() -> Iterator[dict]:
    """Unique-id tenants (us+eu active, us inactive); scoped DELETE
    teardown (never a global TRUNCATE tenants)."""
    import uuid as _uuid

    from vargate_telemetry.db import engine

    sfx = _uuid.uuid4().hex[:8]
    ids = {
        "us_active": f"t-oaiu-us-{sfx}",
        "eu_active": f"t-oaiu-eu-{sfx}",
        "us_inactive": f"t-oaiu-ui-{sfx}",
    }
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                "INSERT INTO tenants (tenant_id, region, active, "
                "billing_status) VALUES "
                "(:ua, 'us', true, 'paying'), "
                "(:ea, 'eu', true, 'paying'), "
                "(:ui, 'us', false, 'cancelled')"
            ),
            {
                "ua": ids["us_active"],
                "ea": ids["eu_active"],
                "ui": ids["us_inactive"],
            },
        )
    yield ids
    with engine.begin() as conn:
        conn.execute(
            sql_text("DELETE FROM tenants WHERE tenant_id = ANY(:ids)"),
            {"ids": list(ids.values())},
        )


def test_dispatch_openai_usage_default_all_regions(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_usage

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_usage.pull_openai_usage_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_usage.dispatch_openai_usage_pulls()
    ds = set(dispatched)
    assert {
        dispatch_tenants["us_active"],
        dispatch_tenants["eu_active"],
    } <= ds
    assert dispatch_tenants["us_inactive"] not in ds


def test_dispatch_openai_usage_explicit_region_filters(
    dispatch_tenants: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vargate_telemetry.tasks import pull_openai_usage

    dispatched: list[str] = []
    monkeypatch.setattr(
        pull_openai_usage.pull_openai_usage_for_tenant,
        "delay",
        lambda tenant_id: dispatched.append(tenant_id),
    )
    pull_openai_usage.dispatch_openai_usage_pulls(region="eu")
    ds = set(dispatched)
    assert dispatch_tenants["eu_active"] in ds
    assert dispatch_tenants["us_active"] not in ds
