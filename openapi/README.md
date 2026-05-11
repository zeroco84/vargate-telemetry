# Ogma API contract

`ogma-api.yaml` is the **contract source of truth** for the Ogma
HTTP API. The backend (this repo) implements against it; the
frontend (`vargate-frontend/apps/ogma-dashboard/`) generates
TypeScript types from it.

Both sides stay in sync because both consume this one file.

## The change order

When you need to add a new endpoint, change a request shape, or
adjust an error code:

1. **Contract change first.** Edit `ogma-api.yaml` here in
   `vargate-telemetry`. Run `python scripts/validate_openapi.py`
   and `pytest tests/test_openapi_spec.py`. Commit with a
   `Sprint TX.Y: <description>` message. Push.
2. **Backend lands second.** Implementation matches the contract.
   Backend tests verify request/response shapes against this YAML.
   The new endpoint can be feature-flagged off if the frontend
   isn't ready yet.
3. **Frontend lands third.** `pnpm dev` (or `pnpm build`) runs
   `openapi-typescript ../../../vargate-telemetry/openapi/ogma-api.yaml -o
   src/types/ogma-api.d.ts` as a pre-step. The generated types
   regenerate every build; the file is gitignored. Wire the UI to
   the new endpoint using the freshly generated types.
4. **Flag flip is its own commit.** Turning a feature-flagged
   endpoint on is small, reviewable, and revertable. Don't roll
   it into the implementation commit.

The discipline matters from sprint one even though prod customers
don't exist yet. Drift is cheap to prevent and expensive to fix.

## Backwards compatibility during transitions

The backend must support the **old contract** until every caller
has switched to the new shape. For now "every caller" is just the
frontend; later it'll include programmatic API users.

Patterns:

- **Additive changes** (new endpoint, new optional field, new
  enum variant) — land in any order.
- **Removing a field / renaming an endpoint / tightening a type** —
  add the new shape, ship in parallel, deprecate the old shape
  with a `deprecated: true` flag in the YAML, then remove after
  the deprecation window (one full sprint, minimum).
- **Breaking a runtime contract** (e.g., changing the meaning of
  an existing field) — fork the field name. Old field stays valid
  for the deprecation window.

## Validation

Two checks run today; more land as the API matures:

1. **`scripts/validate_openapi.py`** — parses the YAML, validates
   against the OpenAPI 3.1 JSON Schema. Once the FastAPI routes
   land (T4.2+), it'll also diff `app.openapi()` against the
   committed YAML and fail on drift.
2. **`tests/test_openapi_spec.py`** — pytest equivalents of the
   script's parse + required-endpoint checks, so the contract
   stays in the regular test suite.

Run them both before pushing any contract change:

```bash
docker compose exec celery-worker python scripts/validate_openapi.py
docker compose exec celery-worker pytest tests/test_openapi_spec.py -v
```

## Why YAML over JSON

OpenAPI tooling accepts both. We picked YAML because:

- Multi-line strings (the `description:` blocks) read cleanly
  without escape goo.
- Comments. YAML allows them; JSON doesn't.
- Diff readability. PRs that touch the contract are easier to
  review with YAML's hierarchical indentation than with JSON's
  brace soup.

The committed file is the contract. The runtime-generated form
(`app.openapi()` once routes exist) is treated as derived.

## File layout

```
openapi/
  ogma-api.yaml       # the contract
  README.md           # this doc
```

That's it. No fragments, no `$ref` to other local files. One file
keeps the contract diff-reviewable in a single PR comment thread.
If the file ever grows past ~2000 lines we revisit splitting.
