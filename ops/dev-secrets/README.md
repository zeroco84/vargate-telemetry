# ops/dev-secrets/

Local-only secrets for development. **Everything in this directory
is gitignored** (the parent `.gitignore` already covers `*.pem`
and `*.key`).

## Bridge JWT keypair (TM2 Phase A2)

`docker compose up` bind-mounts `bridge_jwt_private.pem` from this
directory into the gateway container at
`/run/secrets/bridge_jwt_private.pem`. Generate one before first
compose-up after a fresh clone:

```bash
python scripts/generate_bridge_jwt_keypair.py \
    --out ops/dev-secrets/bridge_jwt_private.pem
```

The generator prints a suggested `OGMA_BRIDGE_JWT_KID` value that
you should paste into `.env`.

For **production**, generate into `/home/vargate/secrets/` and set
`OGMA_BRIDGE_JWT_PRIVATE_KEY_HOST_PATH` in the host's `.env` to
that path — the docker-compose bind-mount falls through to that
override automatically. The dev path is the default only.

Rotation: move the existing PEM aside, regenerate with a new
`--out`, update `OGMA_BRIDGE_JWT_KID` in env, redeploy gateway +
mcp-server in that order.
