# Postgres Row-Level Security (RLS) — convention

**Status:** Active as of T1.5.
**Audience:** anyone adding a new table to the Telemetry schema.

## The rule

**Every tenant-owned Telemetry table MUST have RLS enabled, FORCE'd, and
a tenant-isolation policy installed in the same migration that creates
the table.** No exceptions; the migration is rejected in code review if
the pattern is missing.

## Why

Application code already filters every query by `tenant_id`. RLS is a
**defense-in-depth** layer that catches the class of bug where a
developer forgets the filter — a missing `WHERE tenant_id = :t` would
otherwise expose every tenant's data. With RLS enabled and FORCE'd,
that bug returns zero rows instead of the world.

This also closes the SQL-injection corner where a malicious payload
escapes to the database with the connecting role's full privileges. The
role is the tables' owner, but FORCE makes RLS apply to the owner too,
so the policy still bites.

## The pattern

For a new table named `your_table` with a `tenant_id` column:

```sql
CREATE TABLE your_table (
    id BIGSERIAL PRIMARY KEY,
    tenant_id VARCHAR(64) NOT NULL,
    -- ...other columns...
);
CREATE INDEX your_table_tenant_idx ON your_table (tenant_id);

ALTER TABLE your_table ENABLE ROW LEVEL SECURITY;
ALTER TABLE your_table FORCE ROW LEVEL SECURITY;

CREATE POLICY tenant_isolation_your_table ON your_table
  USING       (tenant_id = current_setting('app.tenant_id', true))
  WITH CHECK  (tenant_id = current_setting('app.tenant_id', true));
```

Three details that look small but matter:

1. **`ENABLE` + `FORCE`.** `ENABLE` alone leaves table owners exempt.
   Since the connecting role is the owner of every Telemetry table,
   ENABLE without FORCE is effectively a no-op.
2. **`USING` AND `WITH CHECK`.** `USING` filters reads (and updates and
   deletes); `WITH CHECK` filters writes. Without `WITH CHECK`, an
   INSERT under tenant-A could plant a row tagged for tenant-B and the
   policy wouldn't catch it.
3. **`current_setting('app.tenant_id', true)`.** The `true` second
   argument tells Postgres to return NULL when the GUC is unset, rather
   than raising. `tenant_id = NULL` is NULL (not true), so an unset GUC
   yields zero rows — the desired fail-closed default. Drop the `true`
   and a missing GUC is a runtime crash.

## How the GUC gets set in production code

`vargate_telemetry.db.session_scope(tenant_id)` is the only blessed
entry point for opening a session. It executes

```python
SELECT set_config('app.tenant_id', :tenant_id, true)
```

at the start of every transaction it manages. Application code that
goes through `session_scope` therefore satisfies RLS automatically. The
RLS policy is the safety net for code that doesn't.

`session_scope` rejects empty/None tenant IDs at the entry. Combined
with FORCE'd RLS, this means there are exactly two ways for application
code to interact with a tenant-owned table:

- Through `session_scope(tenant_id)` with a non-empty tenant_id, in
  which case both the application filter and the RLS policy line up.
- Bypassing `session_scope` (e.g., raw `engine.connect()` or
  `SessionLocal()`), in which case the RLS policy fires the fail-closed
  branch and zero rows are visible.

Both ways are safe. The dangerous middle ground — application filter
silently wrong but database trusts you anyway — is the bug class RLS
exists to eliminate.

## When NOT to use this pattern

The only Telemetry tables that should not be tenant-owned are tables
that hold genuinely cross-tenant operational state — for example, an
infrastructure-level alembic_version row, or a global feature-flag
registry that doesn't read tenant-scoped data. These tables omit the
`tenant_id` column entirely and don't enable RLS.

If you find yourself reaching for "this table has tenant_id but RLS
should be off because…", stop. The answer is almost always wrong. Open
an ADR before creating an exception.

## Testing the pattern on a new table

The three properties checked by `tests/test_telemetry_rls.py` against
the canary table are the same three every new tenant-owned table must
satisfy. Copy that test file, adapt the table name, and add the test to
the same sprint that creates the table:

- With no `app.tenant_id` set, SELECT returns zero rows.
- With `app.tenant_id` = `tenant-A`, only tenant-A's rows are visible.
- With `app.tenant_id` = `tenant-B`, tenant-A's rows are invisible.

A migration that adds a tenant-owned table without these three tests
is incomplete.
