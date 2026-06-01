# Access control — tenant roles (TM4)

Ogma has a **lightweight two-role model** per tenant: every user is
either an **admin** or a **member**. It closes the TM3 §2.2 gap where
"tenant admin required for write" was specified but the auth layer had
no role distinction — every authenticated user was implicitly able to
write.

This is intentionally minimal (no per-resource ACLs, no custom roles).
It exists to separate *configuration/identity* actions from *viewing*.

## What each role can do

| Action | Member | Admin |
|---|:---:|:---:|
| View everything (usage, users, sessions, budgets, alerts, topics) | ✅ | ✅ |
| Acknowledge budget alerts | ✅ | ✅ |
| Run onboarding / select region (self-service) | ✅ | ✅ |
| Create / edit / delete **budgets** | — | ✅ |
| Map an identity to a user (**alias stitching**) | — | ✅ |
| Change another user's **role** | — | ✅ |

Everything not in the admin-only rows is open to any authenticated
member. Reads are never gated.

## How roles are assigned

- **Tenant provisioner → admin.** Whoever completes
  `POST /onboarding/select-region` (creating the tenant) is set to
  `admin` at creation. They own the tenant.
- **Backfill (migration 0022).** Existing tenants had the
  **earliest-created user per tenant** promoted to `admin`, everyone
  else set to `member`. Single-operator tenants (the common case today)
  therefore kept a working admin — no one was locked out.
- **New users default to `member`** (the `users.role` server default)
  until promoted.
- **Promote / demote** is done by an admin — from the dashboard
  (Users roster → the inline toggle on each row) or via
  `POST /api/users/{id}/role` with `{"role": "admin" | "member"}`.

> **Today every tenant has one user** (onboarding creates one tenant per
> user; there's no invite flow yet), so in practice everyone is the admin
> of their own tenant and the `member` role is forward-looking
> infrastructure for when multi-user tenants land. The gate still matters
> for single-user tenants as correct, defensible behavior.

## Last-admin guard

`POST /users/{id}/role` refuses to demote a tenant's **only** admin
(HTTP `409`, code `last_admin`). Promote a second user to admin first.
This prevents a tenant from locking itself out of budget + identity
configuration. Self-demotion is allowed as long as another admin
remains.

## Enforcement

- **Backend is the source of truth.** The `require_admin` FastAPI
  dependency (`vargate_telemetry/auth/roles.py`) gates the admin-only
  endpoints. It reads the role **fresh from the DB per request** (not
  from the JWT), so a promote/demote takes effect on the user's next
  call without forcing them to sign in again. A non-admin gets `403`
  with code `admin_required`; a tenant-less session gets `400`.
- **Frontend gating is advisory UX only.** The dashboard hides the
  admin-only controls (the budget create/delete buttons, the alias-map
  form) and the roster's promote/demote toggle for members, driven by
  `me.role` (exposed on `GET /me`) via the `useIsAdmin()` hook. Hiding a
  button is not security — the backend `require_admin` is.
- `users` has **no RLS** (it's read by the unauth'd SSO callback), so
  every role query scopes by `tenant_id` explicitly. Role lookups
  compare `id::text` so a malformed JWT subject resolves to
  "not an admin" rather than a 500.

## Where it lives

| Piece | Path |
|---|---|
| Role constants, `get_role`, `count_admins`, `require_admin` | `vargate_telemetry/auth/roles.py` |
| `users.role` column + backfill | `migrations/versions/0022_add_user_role.py` |
| Gated endpoints | `api/budgets.py` (create/update/delete), `api/users.py` (`POST /users/{id}/aliases`) |
| Role-change endpoint | `api/users.py` → `POST /users/{id}/role` |
| `/me` role + provisioner promotion | `api/auth.py`, `api/onboarding.py` |
| Dashboard | `vargate-frontend` → `apps/ogma-dashboard` (`lib/me.tsx` `useIsAdmin`, `Users.tsx` roster, `Budgets.tsx`/`BudgetDetail.tsx`, `UserDetail.tsx`) |
