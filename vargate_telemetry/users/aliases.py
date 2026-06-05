# Copyright (C) Twinlite Services Limited
# Licensed under the Business Source License 1.1
# See LICENSE for the full license text and the Change Date.
"""Alias reconciliation + email-equality auto-match (TM3 Phase C1).

The reconciler scans ``telemetry_records`` for distinct
``(source_api, actor identifier)`` tuples and ensures a
``user_aliases`` row exists for each. New identifiers that look
like an email get auto-matched against ``users.email`` (tenant-
scoped); everything else lands unmapped for manual linking.

Trigger points (TM3 §3.2 deviation, documented in the close
report): the spec frames auto-match as happening "when a record
lands", but hooking every ingest path (pull_code_analytics,
pull_compliance, mcp persist_event) is invasive and risky. Instead
the reconciler runs:

  1. As a 15-minute Celery beat task (steady-state), and
  2. Lazily at the top of ``GET /api/users`` (demo / activation
     readiness — a freshly-onboarded tenant sees stitched users
     on first load without waiting for a beat tick).

The UPSERT is idempotent and only touches still-unmapped auto rows,
so running it from two places can't double-count or clobber manual
links.
"""

from __future__ import annotations

import logging

from sqlalchemy import text as sql_text
from sqlalchemy.orm import Session

_log = logging.getLogger(__name__)


# Source APIs that carry an actor dimension (same set the /sessions
# endpoint aggregates). Anthropic admin usage is bucket-grain (no
# actor) so it's excluded — per-user attribution doesn't exist for it.
#
# TM8: OpenAI usage (``openai_admin_usage``) DOES carry a per-user
# dimension (the grouped result row's ``user_id``, which the pull task
# resolves to an email in ``metadata.user_email`` via the
# ``openai_users`` side table and also exposes raw as
# ``metadata.subject_user_id``). Both keys are in the ACTOR_KEY_SQL
# COALESCE below, so an OpenAI user with a resolvable email auto-matches
# an Ogma ``users`` row exactly like a Claude Code actor; one without a
# known email lands unmapped on the raw ``user_id`` (same as an
# Anthropic api-key actor). OpenAI costs / audit are NOT in this set —
# costs have no per-user grain, and audit attribution is best-effort and
# not part of the cross-vendor user rollup.
SESSION_SOURCE_APIS = (
    "code_analytics",
    "compliance_activities",
    "mcp",
    "openai_admin_usage",
)


# Actor-identifier extraction. KEEP IN SYNC with
# ``api/sessions.py``'s ``_ACTOR_KEY_SQL`` — both walk the same
# COALESCE priority so a person's identifier is identical whether
# read through the Sessions view or the alias reconciler. (The
# fragment is duplicated rather than shared to avoid coupling the
# users package to the sessions API module; same posture as the
# usage/budgets supersession-filter duplication.)
ACTOR_KEY_SQL = (
    "COALESCE("
    "  metadata->'actor'->>'email_address',"
    "  metadata->'actor'->>'api_key_name',"
    "  metadata->'actor'->>'user_id',"
    "  metadata->'actor'->>'api_key_id',"
    "  metadata->'actor'->>'type',"
    "  metadata->>'user_email',"
    "  metadata->>'subject_user_id'"
    ")"
)


# TM4 #3 — "effective surface" for display + aggregation. The MCP path
# captures a Claude-self-reported `surface` (claude_code / claude_web /
# claude_desktop / other); there is no server-side signal because
# Anthropic proxies Claude Code on the web identically to chat. For
# records logged before the field existed, fall back to the
# kind=tool_use heuristic (approximate — a tool-using chat turn also
# reads as tool_use), else the generic 'mcp' token. Non-mcp sources
# pass through unchanged so this is safe to apply to any source_api.
#
# `tr.`-qualified because `source_api` is ambiguous in queries that
# JOIN telemetry_records against user_aliases (which also has a
# source_api); every /users + /sessions query aliases the records
# table as `tr`. The frontend `sourceLabels.ts` maps the resulting
# tokens to display strings ("Claude Code" vs "Claude (chat)").
EFFECTIVE_SURFACE_SQL = (
    "CASE "
    "WHEN tr.source_api = 'mcp' THEN CASE "
    "WHEN NULLIF(tr.metadata->>'surface', '') IS NOT NULL "
    "THEN tr.metadata->>'surface' "
    "WHEN tr.metadata->>'kind' = 'tool_use' THEN 'claude_code' "
    "ELSE 'mcp' END "
    "ELSE tr.source_api END"
)


def reconcile_aliases_for_tenant(session: Session, tenant_id: str) -> dict:
    """Ensure a ``user_aliases`` row exists for every distinct actor.

    The caller has opened ``session_scope(tenant_id)`` — RLS gates
    every read + write here. ``users`` has NO RLS, so the
    email-match query filters by ``tenant_id`` explicitly to avoid
    linking across tenants.

    Returns a small dict of counts for logging / the beat task's
    return value.

    Idempotent: the UPSERT's ``DO UPDATE ... WHERE`` clause only
    re-touches rows that are still ``auto_matched = true AND
    user_id IS NULL`` and now have a match — so:
      - manual links (auto_matched = false) are never overwritten,
      - already-mapped auto rows are left alone,
      - newly-matchable unmapped rows get linked on a later run.
    """
    # 1. Distinct (source_api, identifier) tuples currently present
    #    in telemetry for the actor-bearing streams.
    distinct_sql = sql_text(
        f"""
        SELECT DISTINCT
            source_api,
            {ACTOR_KEY_SQL} AS identifier
        FROM telemetry_records
        WHERE tenant_id = current_setting('app.tenant_id')
          AND source_api = ANY(:source_apis)
          AND {ACTOR_KEY_SQL} IS NOT NULL
        """
    )
    rows = session.execute(
        distinct_sql, {"source_apis": list(SESSION_SOURCE_APIS)}
    ).all()

    inserted = 0
    linked = 0
    for row in rows:
        source_api = row.source_api
        identifier = row.identifier
        if not identifier:
            continue

        # Email-equality auto-match. Only attempt when the identifier
        # looks like an email — an api_key_name ("sera-production")
        # would never equal a users.email, but the LIKE guard makes
        # the intent explicit and avoids a needless users scan.
        matched_user_id = None
        if "@" in identifier:
            match = session.execute(
                sql_text(
                    """
                    SELECT id::text AS id
                    FROM users
                    WHERE email = :identifier
                      AND tenant_id = :tenant_id
                    """
                ),
                {"identifier": identifier, "tenant_id": tenant_id},
            ).all()
            # Single unambiguous match → link. Multiple matches
            # (shouldn't happen within one tenant) → leave unmapped
            # for the admin to disambiguate manually.
            if len(match) == 1:
                matched_user_id = match[0].id

        result = session.execute(
            sql_text(
                """
                INSERT INTO user_aliases (
                    tenant_id, user_id, source_api,
                    source_identifier, auto_matched
                )
                VALUES (
                    current_setting('app.tenant_id'),
                    :user_id, :source_api, :identifier, true
                )
                ON CONFLICT (tenant_id, source_api, source_identifier)
                DO UPDATE SET
                    user_id = EXCLUDED.user_id,
                    updated_at = now()
                WHERE user_aliases.auto_matched = true
                  AND user_aliases.user_id IS NULL
                  AND EXCLUDED.user_id IS NOT NULL
                RETURNING (xmax = 0) AS was_insert
                """
            ),
            {
                "user_id": matched_user_id,
                "source_api": source_api,
                "identifier": identifier,
            },
        ).first()

        # RETURNING fires on INSERT and on the conditional UPDATE.
        # `xmax = 0` is Postgres's idiom for "this row was just
        # INSERTed" (vs. updated). When the ON CONFLICT WHERE clause
        # suppresses the update, RETURNING yields nothing → None.
        if result is None:
            continue
        if result.was_insert:
            inserted += 1
            if matched_user_id is not None:
                linked += 1
        else:
            # An unmapped auto row just got linked.
            linked += 1

    _log.info(
        "reconcile_aliases_for_tenant(%s): %d distinct identifiers, "
        "%d new aliases, %d newly linked",
        tenant_id,
        len(rows),
        inserted,
        linked,
    )
    return {
        "tenant_id": tenant_id,
        "distinct_identifiers": len(rows),
        "aliases_inserted": inserted,
        "aliases_linked": linked,
    }
