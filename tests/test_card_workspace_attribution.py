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
    """Empty telemetry_records + workspaces before AND after each test.

    Workspace rows are tenant-scoped and the tests use unique tenant
    ids, but truncating keeps the suite tidy and matches the plain
    ``TRUNCATE TABLE workspaces`` (no identity column) the usage tests
    use.
    """
    from vargate_telemetry.db import engine

    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE workspaces"))
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
        conn.execute(sql_text("TRUNCATE TABLE workspaces"))


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
