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

## The effective role MUST NOT be a superuser

PostgreSQL superusers BYPASS RLS regardless of ENABLE / FORCE settings.
The bootstrap user of the cluster (whoever `POSTGRES_USER` resolved to
when the volume was first initialized) is permanently a superuser — the
kernel rejects `ALTER USER ... NOSUPERUSER` on the bootstrap role with

```
permission denied to alter role
DETAIL:  The bootstrap user must have the SUPERUSER attribute.
```

— and that's by design: removing the attribute could leave the cluster
with no superusers at all.

The pattern that works around this:

1. **Leave the bootstrap user as a superuser.** Migrations run under
   it; DDL is unrestricted; emergencies are still recoverable.
2. **Create a separate non-superuser role** (`vargate_app`) and grant
   the bootstrap role membership in it. Migration `0002_create_app_role`
   does this once.
3. **Application code issues `SET LOCAL ROLE vargate_app`** at the
   start of every transaction. `vargate_telemetry.db.session_scope`
   does this automatically; raw `engine.connect()` callers must do it
   themselves (the RLS tests demonstrate the pattern).

The result: migrations operate under the bootstrap superuser (so DDL
is unrestricted), and application traffic operates under
`vargate_app` (so RLS applies). The mode switch is per-transaction,
reset on COMMIT/ROLLBACK, and is enforced by `session_scope` on every
session that backend code opens.

`vargate_app` has `ALL` privileges on every public-schema table and
sequence, plus an `ALTER DEFAULT PRIVILEGES` clause that auto-grants
the same on tables and sequences created by the bootstrap role in
future migrations. So new tables Just Work — no GRANT bookkeeping per
migration.

If you ever need to add a different non-super role, create it with
`NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE` and grant it only the
privileges it actually needs.

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
