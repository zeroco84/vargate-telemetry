# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Tests for user-alias reconciliation + auto-match (TM3 Phase C1).

Seeds telemetry records (code_analytics / mcp actor shapes) + users,
runs ``reconcile_aliases_for_tenant``, and asserts:
  - email-equality auto-match links the alias to the user
  - non-email identifiers (api_key_name) land unmapped
  - an email with no matching user lands unmapped
  - a later-arriving user gets linked on a re-run
  - manual links (auto_matched=false) are never overwritten
  - reconcile is idempotent (second run inserts nothing)
  - RLS keeps one tenant's aliases out of another's reconcile
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Iterator

import pytest
from sqlalchemy import text as sql_text

from vargate_telemetry.db import engine, session_scope
from vargate_telemetry.users import reconcile_aliases_for_tenant


@pytest.fixture
def clean_state() -> Iterator[None]:
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE user_aliases RESTART IDENTITY CASCADE")
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )
    yield
    with engine.begin() as conn:
        conn.execute(
            sql_text("TRUNCATE TABLE user_aliases RESTART IDENTITY CASCADE")
        )
        conn.execute(
            sql_text("TRUNCATE TABLE telemetry_records RESTART IDENTITY CASCADE")
        )


def _provision_tenant(tenant_id: str) -> None:
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


def _provision_user(tenant_id: str, email: str) -> str:
    user_uuid = str(uuid.uuid4())
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO users
                    (id, email, sso_provider, sso_subject_id, tenant_id)
                VALUES (:id, :email, 'google', :sub, :t)
                """
            ),
            {
                "id": user_uuid,
                "email": email,
                "sub": f"sub-{user_uuid}",
                "t": tenant_id,
            },
        )
    return user_uuid


def _seed_code_analytics(
    tenant_id: str, *, email: str, occurred_at: datetime | None = None
) -> None:
    """Code Analytics user_actor record — nested metadata.actor.email_address."""
    md = {
        "actor": {"type": "user_actor", "email_address": email},
    }
    _insert(tenant_id, "code_analytics", md, occurred_at)


def _seed_code_analytics_apikey(
    tenant_id: str, *, key_name: str
) -> None:
    """Code Analytics api_actor record — api_key_name, no email."""
    md = {
        "actor": {"type": "api_actor", "api_key_name": key_name},
    }
    _insert(tenant_id, "code_analytics", md, None)


def _seed_mcp(tenant_id: str, *, email: str, user_id: str) -> None:
    """MCP record — flat metadata.user_email + subject_user_id."""
    md = {
        "kind": "chat",
        "user_email": email,
        "subject_user_id": user_id,
    }
    _insert(tenant_id, "mcp", md, None)


def _insert(
    tenant_id: str,
    source_api: str,
    md: dict,
    occurred_at: datetime | None,
) -> None:
    occurred_at = occurred_at or datetime.now(tz=timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            sql_text(
                """
                INSERT INTO telemetry_records (
                    tenant_id, record_type, source_api, external_id,
                    occurred_at, content_hash, metadata,
                    chain_seq, chain_prev_hash, chain_self_hash
                ) VALUES (
                    :t, :source_api, :source_api, :eid,
                    :occurred_at, decode(:zero32, 'hex'),
                    :metadata,
                    (SELECT COALESCE(MAX(chain_seq), 0) + 1
                       FROM telemetry_records WHERE tenant_id = :t_lookup),
                    decode(:zero32, 'hex'), decode(:one32, 'hex')
                )
                """
            ),
            {
                "t": tenant_id,
                "t_lookup": tenant_id,
                "source_api": source_api,
                "eid": f"{source_api}:{uuid.uuid4()}",
                "occurred_at": occurred_at,
                "metadata": json.dumps(md),
                "zero32": "00" * 32,
                "one32": "11" * 32,
            },
        )


def _aliases(tenant_id: str) -> list:
    with session_scope(tenant_id) as s:
        return s.execute(
            sql_text(
                """
                SELECT source_api, source_identifier,
                       user_id::text AS user_id, auto_matched
                FROM user_aliases
                ORDER BY source_api, source_identifier
                """
            )
        ).all()


# ───────────────────────────────────────────────────────────────────────────
# Cases
# ───────────────────────────────────────────────────────────────────────────


def test_email_actor_auto_matches_existing_user(clean_state: None) -> None:
    tenant = "tnt_us_alias_match"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "dev@example.com")
    _seed_code_analytics(tenant, email="dev@example.com")

    with session_scope(tenant) as s:
        result = reconcile_aliases_for_tenant(s, tenant)

    assert result["aliases_inserted"] == 1
    assert result["aliases_linked"] == 1
    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].source_identifier == "dev@example.com"
    assert aliases[0].user_id == uid
    assert aliases[0].auto_matched is True


def test_mcp_flat_email_auto_matches(clean_state: None) -> None:
    tenant = "tnt_us_alias_mcp"
    _provision_tenant(tenant)
    uid = _provision_user(tenant, "rick@vargate.ai")
    _seed_mcp(tenant, email="rick@vargate.ai", user_id=uid)

    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)

    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].source_api == "mcp"
    assert aliases[0].source_identifier == "rick@vargate.ai"
    assert aliases[0].user_id == uid


def test_api_key_name_lands_unmapped(clean_state: None) -> None:
    tenant = "tnt_us_alias_apikey"
    _provision_tenant(tenant)
    # No user with this "email" — it's an api_key_name, not an email.
    _seed_code_analytics_apikey(tenant, key_name="sera-production")

    with session_scope(tenant) as s:
        result = reconcile_aliases_for_tenant(s, tenant)

    assert result["aliases_inserted"] == 1
    assert result["aliases_linked"] == 0
    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].source_identifier == "sera-production"
    assert aliases[0].user_id is None  # Unmapped.


def test_email_with_no_matching_user_lands_unmapped(
    clean_state: None,
) -> None:
    tenant = "tnt_us_alias_nomatch"
    _provision_tenant(tenant)
    # Email actor, but no Ogma user with that email.
    _seed_code_analytics(tenant, email="stranger@example.com")

    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)

    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].user_id is None


def test_later_arriving_user_gets_linked_on_rerun(
    clean_state: None,
) -> None:
    """An unmapped (auto) alias should link when the matching user
    signs up later and the reconciler re-runs."""
    tenant = "tnt_us_alias_later"
    _provision_tenant(tenant)
    _seed_code_analytics(tenant, email="late@example.com")

    # First run: no user yet → unmapped.
    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)
    assert _aliases(tenant)[0].user_id is None

    # User signs up. Second run links the existing unmapped alias.
    uid = _provision_user(tenant, "late@example.com")
    with session_scope(tenant) as s:
        result = reconcile_aliases_for_tenant(s, tenant)

    assert result["aliases_linked"] == 1
    aliases = _aliases(tenant)
    assert len(aliases) == 1  # Still one row — updated, not duplicated.
    assert aliases[0].user_id == uid


def test_manual_link_is_not_overwritten(clean_state: None) -> None:
    """A manually-linked alias (auto_matched=false) must survive a
    reconcile even if auto-match would disagree."""
    tenant = "tnt_us_alias_manual"
    _provision_tenant(tenant)
    uid_real = _provision_user(tenant, "real@example.com")
    uid_other = _provision_user(tenant, "other@example.com")
    # Telemetry actor email is "other@example.com" — auto-match would
    # link to uid_other. But the admin manually linked it to uid_real.
    _seed_code_analytics(tenant, email="other@example.com")
    with session_scope(tenant) as s:
        s.execute(
            sql_text(
                """
                INSERT INTO user_aliases (
                    tenant_id, user_id, source_api,
                    source_identifier, auto_matched
                ) VALUES (
                    current_setting('app.tenant_id'),
                    :uid, 'code_analytics', 'other@example.com', false
                )
                """
            ),
            {"uid": uid_real},
        )

    # Reconcile must NOT flip it to uid_other.
    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)

    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].user_id == uid_real  # Unchanged.
    assert aliases[0].auto_matched is False


def test_reconcile_is_idempotent(clean_state: None) -> None:
    tenant = "tnt_us_alias_idem"
    _provision_tenant(tenant)
    _provision_user(tenant, "dev@example.com")
    _seed_code_analytics(tenant, email="dev@example.com")
    _seed_code_analytics_apikey(tenant, key_name="ci-key")

    with session_scope(tenant) as s:
        first = reconcile_aliases_for_tenant(s, tenant)
    assert first["aliases_inserted"] == 2

    # Second run: nothing new.
    with session_scope(tenant) as s:
        second = reconcile_aliases_for_tenant(s, tenant)
    assert second["aliases_inserted"] == 0
    assert second["aliases_linked"] == 0
    assert len(_aliases(tenant)) == 2


def test_reconcile_is_rls_isolated(clean_state: None) -> None:
    """Tenant A's reconcile must only see tenant A's telemetry."""
    tenant_a = "tnt_us_alias_rls_a"
    tenant_b = "tnt_us_alias_rls_b"
    _provision_tenant(tenant_a)
    _provision_tenant(tenant_b)
    _seed_code_analytics(tenant_a, email="a@example.com")
    _seed_code_analytics(tenant_b, email="b@example.com")

    with session_scope(tenant_a) as s:
        reconcile_aliases_for_tenant(s, tenant_a)

    aliases_a = _aliases(tenant_a)
    aliases_b = _aliases(tenant_b)
    assert len(aliases_a) == 1
    assert aliases_a[0].source_identifier == "a@example.com"
    # Tenant B's reconcile never ran + RLS hides A's rows from B.
    assert len(aliases_b) == 0


def test_multiple_users_same_email_leaves_unmapped(
    clean_state: None,
) -> None:
    """Defensive: if two users somehow share an email in one tenant,
    auto-match must NOT guess — leave unmapped for manual resolution."""
    tenant = "tnt_us_alias_dup"
    _provision_tenant(tenant)
    _provision_user(tenant, "dup@example.com")
    _provision_user(tenant, "dup@example.com")
    _seed_code_analytics(tenant, email="dup@example.com")

    with session_scope(tenant) as s:
        reconcile_aliases_for_tenant(s, tenant)

    aliases = _aliases(tenant)
    assert len(aliases) == 1
    assert aliases[0].user_id is None  # Ambiguous → unmapped.
