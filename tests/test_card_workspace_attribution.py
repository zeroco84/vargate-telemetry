# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for the workspace cost-attribution card (TM7).

Exercises ``vargate_telemetry.insights.cards.workspace_attribution.build_card``
directly (no HTTP) against synthetic ``telemetry_records`` seeded
through a direct INSERT, mirroring ``test_card_model_mix.py`` /
``test_usage_api.py`` / ``test_budgets_api.py``.

The card ranks workspaces by their share of priceable Admin-API spend
over the window (via ``spend_data.workspace_spend``):

  - usage rows whose ``result.workspace_id`` is null carry no workspace
    dimension at all → ``workspace_spend`` returns ``[]`` → the card is
    ``idle`` with zero findings and an empty-state that points at
    enabling "workspace grouping" on the connector;
  - usage rows that DO carry a ``workspace_id`` (optionally resolved to
    a human name via the ``workspaces`` side table) → at least one
    finding, non-empty items, severity ``advisory``.

Both seeded rows use a known model (Sonnet 4.5) so the tokens price to
a non-zero USD cost — ``workspace_spend`` drops buckets that price to
``None`` (null/unknown model), so a costed model is required for the
populated branch to produce items.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text as sql_text

os.environ.setdefault(
    "JWT_SIGNING_KEY",
    "test-jwt-signing-key-only-used-inside-the-test-suite-32b",
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures + helpers
# ───────────────────────────────────────────────────────────────────────────


@pytest.fixture
def clean_records():
    """Empty telemetry_records + workspaces + openai_projects before AND
    after each test.

    Workspace / project rows are tenant-scoped and the tests use unique
    tenant ids, but truncating keeps the suite tidy and matches the plain
    ``TRUNCATE`` (no identity column) the usage tests use. ``openai_projects``
    is truncated for the cross-vendor (TM8 Phase D) cases; CASCADE on the
    telemetry truncate is harmless here.
    """
    from vargate_telemetry.db import engine

    def _truncate(conn) -> None:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE workspaces"))
        conn.execute(sql_text("TRUNCATE TABLE openai_projects"))

    with engine.begin() as conn:
        _truncate(conn)
    yield
    with engine.begin() as conn:
        _truncate(conn)


def _provision_tenant(tenant_id: str) -> None:
    """Insert a ``tenants`` row so the OpenAI side tables' FK is satisfied.

    The Anthropic-only cases don't need this (``telemetry_records`` has no
    tenants FK), but ``openai_projects`` references ``tenants(tenant_id)``,
    so the cross-vendor cases provision the tenant first.
    """
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO tenants (tenant_id, region, active, billing_status)
                VALUES (:t, 'us', TRUE, 'trial')
                ON CONFLICT (tenant_id) DO NOTHING
                """
            ),
            {"t": tenant_id},
        )


def _unique_tenant(name: str) -> str:
    return f"tnt_us_{name}_" + uuid.uuid4().hex[:8]


# Sonnet lives in the current (2026-05) rate card, so tokens seeded
# anywhere in the trailing week price to a non-zero USD cost.
_SONNET = "claude-sonnet-4-5-20250929"


def _seed_usage_record(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_tokens: int = 1_000_000,
    output_tokens: int = 200_000,
    workspace_id: str | None = None,
    api_key_id: str | None = None,
    model: str | None = _SONNET,
) -> None:
    """Insert one ``record_type='usage'`` / ``source_api='admin'`` row.

    Shape mirrors ``_seed_usage_record`` in ``test_budgets_api.py`` /
    ``test_card_model_mix.py``: a ``metadata.results`` array with a
    single per-model breakdown, chain seq = COALESCE(MAX+1),
    placeholder content/prev/self hashes via ``decode(hex)``.
    ``workspace_id`` is the dimension this card slices on; ``None``
    (the default) is the idle case.
    """
    from vargate_telemetry.db import engine

    results = [
        {
            "model": model,
            "workspace_id": workspace_id,
            "api_key_id": api_key_id,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
        }
    ]
    md = {
        "starting_at": occurred_at.isoformat(),
        "ending_at": occurred_at.isoformat(),
        "results": results,
    }
    eid = f"usage:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'admin', :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records
                      WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'),
                    decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _seed_workspace(tenant_id: str, workspace_id: str, name: str) -> None:
    """Insert a ``workspaces`` row so the card can resolve a human name."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO workspaces (tenant_id, workspace_id, name)
                VALUES (:t, :w, :n)
                ON CONFLICT (tenant_id, workspace_id)
                DO UPDATE SET name = EXCLUDED.name
                """
            ),
            {"t": tenant_id, "w": workspace_id, "n": name},
        )


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


# ───────────────────────────────────────────────────────────────────────────
# (a) workspace_id null → idle, zero findings, empty-state nudge
# ───────────────────────────────────────────────────────────────────────────


def test_null_workspace_id_is_idle(clean_records: None) -> None:
    """Usage rows that carry no ``workspace_id`` (the common
    Personal-plan reality) leave ``workspace_spend`` empty → the card
    is ``idle`` with zero findings, no items, and an empty-state that
    nudges the operator toward enabling workspace grouping."""
    from vargate_telemetry.insights.cards.workspace_attribution import build_card

    tenant = _unique_tenant("wsattr_idle")
    now = _now()

    # Two priceable usage rows in the current window, both workspace-less.
    _seed_usage_record(tenant, occurred_at=now - timedelta(hours=1))
    _seed_usage_record(tenant, occurred_at=now - timedelta(days=2))

    card = build_card(tenant, "7d")

    assert card.id == "workspace_attribution"
    assert card.severity == "idle"
    assert card.findings_count == 0
    assert card.items == []
    assert card.empty_state is not None
    assert "workspace grouping" in card.empty_state


# ───────────────────────────────────────────────────────────────────────────
# (b) workspace_id set → advisory with findings + items
# ───────────────────────────────────────────────────────────────────────────


def test_workspace_id_set_is_advisory_with_items(clean_records: None) -> None:
    """Usage rows that carry a ``workspace_id`` (with a resolved name
    from the ``workspaces`` side table) → at least one finding,
    non-empty items, severity ``advisory``."""
    from vargate_telemetry.insights.cards.workspace_attribution import build_card

    tenant = _unique_tenant("wsattr_pop")
    now = _now()

    # Name the workspace so the resolved label can surface on the item.
    _seed_workspace(tenant, "wrkspc_demo", "Demo Workspace")
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(hours=1),
        workspace_id="wrkspc_demo",
    )

    card = build_card(tenant, "7d")

    assert card.id == "workspace_attribution"
    assert card.severity == "advisory"
    assert card.findings_count >= 1
    assert len(card.items) >= 1
    # The single workspace owns 100% of spend, so it appears as an item
    # carrying its resolved name and a spend-share detail.
    assert any(it.label == "Demo Workspace" for it in card.items), [
        it.label for it in card.items
    ]
    assert any(
        it.detail is not None and "spend" in it.detail for it in card.items
    ), [it.detail for it in card.items]


def test_workspace_id_set_without_workspaces_row_falls_back_to_id(
    clean_records: None,
) -> None:
    """A ``workspace_id`` with no matching ``workspaces`` row still
    produces a populated, advisory card — the item label falls back to
    the raw id rather than vanishing."""
    from vargate_telemetry.insights.cards.workspace_attribution import build_card

    tenant = _unique_tenant("wsattr_noname")
    now = _now()

    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(hours=1),
        workspace_id="wrkspc_unnamed",
    )

    card = build_card(tenant, "7d")

    assert card.id == "workspace_attribution"
    assert card.severity == "advisory"
    assert card.findings_count >= 1
    assert len(card.items) >= 1
    # No workspaces row → label falls back to the raw workspace id.
    assert any(it.label == "wrkspc_unnamed" for it in card.items), [
        it.label for it in card.items
    ]


# ───────────────────────────────────────────────────────────────────────────
# Cross-vendor (TM8 Phase D) — OpenAI projects merge into the ranking
# ───────────────────────────────────────────────────────────────────────────

_GPT4O = "gpt-4o"


def _seed_openai_cost(
    tenant_id: str,
    *,
    occurred_at: datetime,
    amount_value: str,
    project_id: str = "proj_alpha",
    project_name: str | None = "Alpha",
    line_item: str = "gpt-4o-2024-08-06, input",
) -> None:
    """Insert an authoritative OpenAI cost record (pull_openai_costs shape).

    The card's authoritative path SUMs ``amount_value`` per ``project_id``,
    resolving the name from ``openai_projects`` (falling back to the cost
    record's own ``project_name``).
    """
    from vargate_telemetry.db import engine

    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "line_item": line_item,
        "project_id": project_id,
        "project_name": project_name,
        "amount_value": amount_value,
        "currency": "usd",
    }
    eid = f"openai_admin_costs:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'cost', 'openai_admin_costs', :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'),
                    decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _seed_openai_usage(
    tenant_id: str,
    *,
    occurred_at: datetime,
    input_uncached: int,
    input_cached: int = 0,
    output: int = 0,
    project_id: str = "proj_alpha",
    model: str = _GPT4O,
) -> None:
    """Insert an OpenAI usage record (pull_openai_usage shape).

    Used to exercise the per-project usage-estimate fallback (no /costs
    rows). ``project_id`` is top-level metadata (the grouped grain the
    usage pull writes); the token split lives under ``result``.
    """
    from vargate_telemetry.db import engine

    md = {
        "start_time": occurred_at.isoformat(),
        "end_time": occurred_at.isoformat(),
        "modality": "completions",
        "result": {
            "model": model,
            "input_tokens": input_uncached + input_cached,
            "input_uncached_tokens": input_uncached,
            "input_cached_tokens": input_cached,
            "output_tokens": output,
        },
        "model": model,
        "project_id": project_id,
        "subject_user_id": "user-oai",
    }
    eid = f"openai_admin_usage:{uuid.uuid4()}"
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, 'usage', 'openai_admin_usage', :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'),
                    decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "eid": eid,
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _seed_openai_project(
    tenant_id: str, project_id: str, name: str
) -> None:
    """Insert an ``openai_projects`` row so the card resolves a human name."""
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO openai_projects (tenant_id, project_id, name)
                VALUES (:t, :p, :n)
                ON CONFLICT (tenant_id, project_id)
                DO UPDATE SET name = EXCLUDED.name
                """
            ),
            {"t": tenant_id, "p": project_id, "n": name},
        )


def test_openai_project_appears_vendor_tagged(clean_records: None) -> None:
    """An OpenAI project with authoritative /costs spend (and a resolved
    name) appears as a populated, vendor-tagged item — the card title is
    the cross-vendor 'Project / workspace attribution'."""
    from vargate_telemetry.insights.cards.workspace_attribution import (
        build_card,
    )

    tenant = _unique_tenant("oai_proj")
    _provision_tenant(tenant)
    now = _now()

    _seed_openai_project(tenant, "proj_alpha", "Alpha Project")
    _seed_openai_cost(
        tenant,
        occurred_at=now - timedelta(hours=1),
        amount_value="42.00",
        project_id="proj_alpha",
    )

    card = build_card(tenant, "7d")

    assert card.id == "workspace_attribution"
    assert card.title == "Project / workspace attribution"
    assert card.severity == "advisory"
    assert card.findings_count >= 1
    alpha = [it for it in card.items if it.label == "Alpha Project"]
    assert alpha, [it.label for it in card.items]
    # Vendor is surfaced in the detail so two clouds don't blur together.
    assert alpha[0].detail is not None
    assert "OpenAI" in alpha[0].detail
    assert "spend" in alpha[0].detail


def test_both_vendors_merge_into_one_ranking(clean_records: None) -> None:
    """Anthropic workspaces and OpenAI projects merge into ONE ranking,
    each vendor-tagged, ordered by cost — the bigger spender leads and
    drives the concentration headline."""
    from vargate_telemetry.insights.cards.workspace_attribution import (
        build_card,
    )

    tenant = _unique_tenant("xvendor")
    _provision_tenant(tenant)
    now = _now()

    # Anthropic workspace: 1M Sonnet input + 200k output. Sonnet input is
    # $3/MTok, output $15/MTok -> $3.00 + $3.00 = $6.00.
    _seed_workspace(tenant, "wrkspc_eng", "Engineering")
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(hours=2),
        workspace_id="wrkspc_eng",
    )
    # OpenAI project: authoritative $50.00 — the clear leader.
    _seed_openai_project(tenant, "proj_data", "Data Science")
    _seed_openai_cost(
        tenant,
        occurred_at=now - timedelta(hours=1),
        amount_value="50.00",
        project_id="proj_data",
    )

    card = build_card(tenant, "7d")

    labels = [it.label for it in card.items]
    details = [it.detail or "" for it in card.items]
    # Both cost centres present.
    assert "Data Science" in labels, labels
    assert "Engineering" in labels, labels
    # Ranked by cost: the $50 OpenAI project leads the $6 workspace.
    assert labels.index("Data Science") < labels.index("Engineering")
    # Each item is vendor-tagged with the right vendor.
    data_item = next(it for it in card.items if it.label == "Data Science")
    eng_item = next(it for it in card.items if it.label == "Engineering")
    assert "OpenAI" in (data_item.detail or "")
    assert "Anthropic" in (eng_item.detail or "")
    # The OpenAI project dominates (>60%) → it owns the concentration
    # headline.
    assert "Data Science" in card.headline
    assert card.findings_count == 2


def test_openai_usage_estimate_fallback_when_no_costs(
    clean_records: None,
) -> None:
    """With no /costs rows, an OpenAI project's spend is the usage-token
    estimate (double-count-safe) and still ranks + vendor-tags."""
    from vargate_telemetry.insights.cards.workspace_attribution import (
        build_card,
    )

    tenant = _unique_tenant("oai_est")
    _provision_tenant(tenant)
    now = _now()

    _seed_openai_project(tenant, "proj_alpha", "Alpha")
    # 1M uncached gpt-4o input @ $2.50/MTok -> $2.50 estimate, no /costs.
    _seed_openai_usage(
        tenant,
        occurred_at=now - timedelta(hours=1),
        input_uncached=1_000_000,
        project_id="proj_alpha",
    )

    card = build_card(tenant, "7d")

    alpha = [it for it in card.items if it.label == "Alpha"]
    assert alpha, [it.label for it in card.items]
    assert "OpenAI" in (alpha[0].detail or "")
    # $2.50 truncates to $2 in the whole-dollar value.
    assert alpha[0].value == "$2"


def test_openai_costs_preferred_over_usage_estimate(
    clean_records: None,
) -> None:
    """When BOTH an authoritative /costs row and usage exist for a project,
    the card uses the authoritative billed amount (not the usage estimate)."""
    from vargate_telemetry.insights.cards.workspace_attribution import (
        build_card,
    )

    tenant = _unique_tenant("oai_pref")
    _provision_tenant(tenant)
    now = _now()

    _seed_openai_project(tenant, "proj_alpha", "Alpha")
    # Usage would estimate $2.50…
    _seed_openai_usage(
        tenant,
        occurred_at=now - timedelta(hours=2),
        input_uncached=1_000_000,
        project_id="proj_alpha",
    )
    # …but an authoritative /costs row bills $99.00.
    _seed_openai_cost(
        tenant,
        occurred_at=now - timedelta(hours=1),
        amount_value="99.00",
        project_id="proj_alpha",
    )

    card = build_card(tenant, "7d")

    alpha = [it for it in card.items if it.label == "Alpha"]
    assert alpha, [it.label for it in card.items]
    # $99 authoritative, NOT the $2 usage estimate.
    assert alpha[0].value == "$99"


def test_anthropic_only_card_is_unchanged_except_title(
    clean_records: None,
) -> None:
    """An Anthropic-only tenant ranks exactly as in TM7: a single named
    workspace owns 100% of spend with a spend-share detail. The only TM8
    change is the cross-vendor title."""
    from vargate_telemetry.insights.cards.workspace_attribution import (
        build_card,
    )

    tenant = _unique_tenant("anthropic_only")
    now = _now()

    _seed_workspace(tenant, "wrkspc_demo", "Demo Workspace")
    _seed_usage_record(
        tenant,
        occurred_at=now - timedelta(hours=1),
        workspace_id="wrkspc_demo",
    )

    card = build_card(tenant, "7d")

    assert card.title == "Project / workspace attribution"
    assert card.severity == "advisory"
    assert card.findings_count == 1
    item = card.items[0]
    assert item.label == "Demo Workspace"
    # Anthropic-vendor-tagged, sole workspace -> 100% of spend.
    assert item.detail == "Anthropic · 100% of spend"
    assert "Demo Workspace" in card.headline
