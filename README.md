# Vargate Telemetry

Vargate Telemetry tracks AI-model usage across an organization, monitors
prompts and responses for compliance signals, and gives buyers actionable
insight into how their teams are using AI.

It is the read-only, pull-based sibling of [Vargate Pro][pro] — Pro
intercepts and governs autonomous-agent tool calls inline; Telemetry pulls
the human side of AI usage from the AI vendor's admin and compliance APIs,
runs analytics on it, and produces enterprise-grade audit and alerting on
top.

[pro]: https://github.com/zeroco84/vargate-proxy

> **Status:** v0 bootstrap. The repo scaffolding and licensing artifacts are
> in place; product code lands in subsequent T1+ sprints.

## License

Vargate Telemetry is licensed under the **Business Source License 1.1
(BSL 1.1)**, with **Twinlite Services Limited** as Licensor. The full legal
text — including the Additional Use Grant that permits production use, the
Change Date, and the Change License — lives in [LICENSE](LICENSE).

The plain-English version: **you can run Vargate Telemetry in production for
your own organization, including self-hosting it for internal use.** The only
restriction is offering it as a hosted or managed service that competes with
Vargate's commercial offerings. On 2030-05-08, this version automatically
converts to Apache License 2.0 and even that restriction goes away.

For the predictable questions ("Is this open source?", "Why BSL not Apache?",
"Can I deploy this for my company?"), see [LICENSING-FAQ.md](LICENSING-FAQ.md).
For the decision record behind the choice, see
[docs/adr/ADR-002-licensing.md](docs/adr/ADR-002-licensing.md).

If your situation falls in a grey area, write to legal@vargate.ai.

## What's in this repo

| Path                       | What it is                                     |
| -------------------------- | ---------------------------------------------- |
| `vargate_telemetry/`       | Python package — gateway, ingest, analyzers    |
| `docs/adr/`                | Architecture decision records                  |
| `docs/architecture/`       | Architecture documents and diagrams            |
| `docs/legal/`              | Trademark and IP-assignment tracking           |
| `docs/perf/`               | Performance benchmarks (HSM envelope, etc.)    |
| `docs/sprints/`            | Sprint planning and completion notes           |
| `tests/`                   | Pytest suite                                   |
| `scripts/`                 | One-shot maintenance and benchmark scripts     |

This repository ships **backend only**. The Ogma dashboard UI lives
in the proprietary `vargate-frontend` repo at
`apps/ogma-dashboard/`, separate from this BSL-licensed source. See
the working-memory rule `ui_lives_in_vargate_frontend.md` for the
reasoning.

## Architecture

The system architecture is documented in
[ADR-001](docs/adr/ADR-001-telemetry-architecture.md). The short version:
Telemetry runs as a per-region cell — Postgres, MinIO, Celery on Redis,
SoftHSM2 with envelope encryption, FastAPI gateway — with a small global
control plane for signup routing and billing rollups. No content ever
crosses regions. The dashboard UI is a separate React app in
`vargate-frontend/apps/ogma-dashboard/` that consumes the gateway over
HTTP.

## Running locally

The Telemetry stack is a Docker Compose deployment. Sprint T1 lands
infrastructure incrementally — Postgres in T1.1, MinIO in T1.2, Celery in
T1.3 — before the gateway code lands in T1.4+. The list of services in
`docker-compose.yml` will grow with each sprint.

Prerequisites: Docker Engine ≥ 24 with the Compose v2 plugin.

Bootstrap (Postgres + MinIO + Redis + Celery + SoftHSM2 — current state
of the stack). The block is idempotent: re-running won't overwrite an
existing `.env` or rotate a working password.

```bash
# First-time only: create .env and insert distinct random secrets for
# each service. POSTGRES_PASSWORD and DATABASE_URL share CHANGEME_PG so
# they stay in sync; the others get their own. No-op once .env exists.
[ -f .env ] || { cp .env.example .env \
  && sed -i "s/CHANGEME_PG/$(openssl rand -hex 32)/g"        .env \
  && sed -i "s/CHANGEME_MINIO/$(openssl rand -hex 32)/g"     .env \
  && sed -i "s/CHANGEME_HSM_SO/$(openssl rand -hex 32)/g"    .env \
  && sed -i "s/CHANGEME_HSM_USER/$(openssl rand -hex 32)/g"  .env; }

# `--build` so the celery images rebuild when vargate_telemetry/ changes;
# `--wait` blocks until every service with a healthcheck is healthy.
docker compose up -d --build --wait \
  postgres minio redis celery-worker celery-beat

# Apply migrations and initialize the HSM token + KEK. Both are idempotent.
docker compose run --rm celery-worker alembic upgrade head
docker compose run --rm celery-worker python scripts/init_telemetry_kek.py

# Postgres round-trip
docker compose exec postgres psql -U vargate -d vargate_telemetry -c "SELECT 1"

# MinIO smoke test (alias, create bucket, remove bucket)
docker compose exec minio sh -c '
  mc alias set local http://localhost:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" \
  && mc mb local/vargate-test \
  && mc rb local/vargate-test --force'

# Redis ping
docker compose exec redis redis-cli ping   # expect: PONG

# Celery worker should log "celery@<host> ready." within a few seconds.
docker compose logs --tail=30 celery-worker | grep -E "ready\\.|Connected"

# Celery beat should be sending the (currently empty) schedule heartbeat.
docker compose logs --tail=20 celery-beat | grep -E "Scheduler|beat: Starting"

# HSM should show the Telemetry token + an AES-256 KEK by label.
docker compose exec celery-worker softhsm2-util --show-slots \
  | grep -E "Label:|Initialized:"
```

### Migrations

Alembic migrations run inside the celery-worker image (it has the same
dependencies as the would-be gateway). The first run creates the
`alembic_version` row from the empty initial migration; subsequent
sprints add real migrations.

```bash
# Apply all migrations to the dev Postgres
docker compose run --rm celery-worker alembic upgrade head

# Verify the latest version is applied
docker compose exec postgres psql -U vargate -d vargate_telemetry \
  -c "SELECT version_num FROM alembic_version;"
```

### HSM (T1.6)

SoftHSM2 is installed in the Telemetry image; the token directory is
mounted from the `vargate-hsm-tokens` named volume on celery-worker
and celery-beat. The Telemetry KEK lives inside that token and is
generated once via:

```bash
docker compose run --rm celery-worker python scripts/init_telemetry_kek.py
```

The init script is idempotent. Re-running prints "Token already
initialized" and "KEK ready" and exits 0.

### Tests

```bash
# All tests against the live stack
docker compose run --rm celery-worker pytest tests/

# Just the T1.4 infra smoke tests
docker compose run --rm celery-worker pytest tests/test_telemetry_infra.py

# The Celery round-trip test is skipped by default; opt in explicitly:
docker compose run --rm -e CELERY_TEST_LIVE=1 celery-worker \
  pytest tests/test_telemetry_infra.py::test_celery_worker_responsive
```

> **Postgres password drift recovery.** Postgres bakes the initial password
> into its data volume on first boot and ignores `.env` changes after that.
> If your `.env` and the volume disagree, realign without destroying the
> volume:
>
> ```bash
> PW=$(grep ^POSTGRES_PASSWORD= .env | cut -d= -f2-)
> docker compose exec -T postgres psql -U vargate -d vargate_telemetry \
>   -c "ALTER USER vargate PASSWORD '${PW}';"
> ```
>
> MinIO doesn't have this problem — it reads `MINIO_ROOT_PASSWORD` from env
> on every start, so editing `.env` and running `docker compose up -d
> --force-recreate minio` is enough.

Tear down (keeps the data volumes):

```bash
docker compose down
```

**Never run `docker compose down -v`** — it deletes the
`vargate-postgres-data`, `vargate-minio-data`, `vargate-redis-data`,
and `vargate-hsm-tokens` volumes. The HSM volume holds the KEK that
wraps every per-tenant DEK; losing it crypto-shreds every encrypted
blob in the system.

### Production overlay

`docker-compose.prod.yml` layers per-service memory limits on the
existing internal-only stack. Apply both files together:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Memory caps: Postgres 2 GB, MinIO 1 GB, Redis 1 GB, Celery worker 1 GB,
Celery beat 512 MB. Reservations are half of the limit on each service.

Verify nothing is exposed to the host (the only LISTEN sockets should
be loopback-bound, e.g., dockerd / containerd internals):

```bash
ss -tlnp 2>/dev/null | grep -vE '(127\.0\.0\.1|\[::1\]|State)'
# expect: empty output
```

T4+ adds the FastAPI gateway and an nginx ingress in front of it; until
then this stack has zero externally-reachable surface.

## Contributing

Contributions are welcome. Substantial code contributions require a
Contributor License Agreement (CLA) so that your contribution can be
relicensed at the Change Date when the codebase converts to Apache 2.0.
Small fixes and docs changes are accepted under BSL terms without a CLA.

By contributing you agree to abide by the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Working on the code

The full sprint-by-sprint task plan lives in
[`docs/sprints/`](docs/sprints/). Each sprint task is sized to one
implementer session and one commit. Read the relevant task block before
starting.

Conventions used in this repo:

- Every new `*.py` file under `vargate_telemetry/` carries the license
  header from `.github/LICENSE_HEADER.txt`. CI enforces this.
- Per-tenant isolation is sacred. Every database query scopes by
  `tenant_id`. Every per-tenant DEK is wrapped by the HSM-held KEK.
- Stage files explicitly by name; never `git add -A` or `git add .`.
- Never run `docker compose down -v` — it destroys HSM keys.

## Reporting security issues

Please do not file public issues for security reports. Email
security@vargate.ai with a description and reproduction steps. We
acknowledge within two business days.
