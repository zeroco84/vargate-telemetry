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
| `ui/`                      | React + Vite dashboard                         |
| `docs/adr/`                | Architecture decision records                  |
| `docs/architecture/`       | Architecture documents and diagrams            |
| `docs/legal/`              | Trademark and IP-assignment tracking           |
| `docs/perf/`               | Performance benchmarks (HSM envelope, etc.)    |
| `docs/sprints/`            | Sprint planning and completion notes           |
| `tests/`                   | Pytest suite                                   |
| `scripts/`                 | One-shot maintenance and benchmark scripts     |

## Architecture

The system architecture is documented in
[ADR-001](docs/adr/ADR-001-telemetry-architecture.md). The short version:
Telemetry runs as a per-region cell — Postgres, MinIO, Celery on Redis,
SoftHSM2 with envelope encryption, FastAPI gateway, React dashboard — with
a small global control plane for signup routing and billing rollups. No
content ever crosses regions.

## Running locally

The Telemetry stack is a Docker Compose deployment. Sprint T1 lands
infrastructure incrementally — Postgres in T1.1, MinIO in T1.2, Celery in
T1.3 — before the gateway code lands in T1.4+. The list of services in
`docker-compose.yml` will grow with each sprint.

Prerequisites: Docker Engine ≥ 24 with the Compose v2 plugin.

Bootstrap (Postgres only — current state of the stack). The block is
idempotent: re-running won't overwrite an existing `.env` or rotate a
working password.

```bash
# First-time only: create .env and insert a random password into both
# POSTGRES_PASSWORD and DATABASE_URL. No-op once .env exists.
[ -f .env ] || { cp .env.example .env && \
  sed -i "s/changeme/$(openssl rand -hex 32)/g" .env; }

docker compose up -d --wait postgres   # blocks until (healthy)
docker compose exec postgres psql -U vargate -d vargate_telemetry -c "SELECT 1"
```

> **If you re-ran the older non-idempotent bootstrap and your `.env`
> password drifted from what's in the data volume**, realign without
> destroying the volume:
>
> ```bash
> PW=$(grep ^POSTGRES_PASSWORD= .env | cut -d= -f2-)
> docker compose exec -T postgres psql -U vargate -d vargate_telemetry \
>   -c "ALTER USER vargate PASSWORD '${PW}';"
> ```

Tear down (keeps the data volume):

```bash
docker compose down
```

**Never run `docker compose down -v`** — it deletes the
`vargate-postgres-data` volume and any HSM keys we add later.

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
