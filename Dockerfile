# syntax=docker/dockerfile:1.4
# Vargate Telemetry — Python image shared by celery-worker, celery-beat,
# and (from T1.4) the FastAPI gateway. Single image, per-service commands.

FROM python:3.12-slim

WORKDIR /app

# System dependencies:
#   curl + ca-certificates — in-container health probes and HTTPS to
#       Anthropic from the workers (T3.1+).
#   softhsm2 — PKCS#11 software HSM. Provides /usr/lib/softhsm/libsofthsm2.so
#       and the softhsm2-util token-management CLI used by the T1.6 KEK
#       init script.
#   build-essential — fallback for any C-extension wheels that pip can't
#       resolve as binaries (notably python-pkcs11 on uncommon archs).
#   git + openssh-client — required to pip install vargate-audit-chain
#       from the vargate-proxy repo over Git+SSH (T2.2). Build host must
#       supply an SSH agent that has read access to the proxy repo:
#           docker compose build --ssh default celery-worker
#       (or wherever the agent socket lives).
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl \
         ca-certificates \
         softhsm2 \
         build-essential \
         git \
         openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Trust github.com's host key non-interactively so the SSH-mounted pip
# install below doesn't fail with "Host key verification failed".
RUN mkdir -p /root/.ssh \
    && ssh-keyscan github.com >> /root/.ssh/known_hosts

COPY requirements.txt ./
# BuildKit SSH agent forwarding: --mount=type=ssh exposes the host's
# SSH agent to this RUN step so pip can clone the private vargate-proxy
# repo to install vargate-audit-chain. The mount is ephemeral and never
# baked into image layers.
RUN --mount=type=ssh \
    pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
# pyproject.toml carries the [tool.pytest.ini_options] block (testpaths,
# pythonpath, etc.). Without it in the image, pytest can't find shared
# test helpers like tests/fixtures/. Lands after the pip install layer
# so changes to project metadata don't invalidate the dependency cache.
COPY pyproject.toml ./
COPY vargate_telemetry/ ./vargate_telemetry/
# TM1 — mcp_server/ ships in the same image as the gateway. The
# `mcp-server` compose service runs `uvicorn mcp_server.main:app`
# off this same image. Apache-2.0 vs BSL-1.1 boundary lives in the
# per-file headers, not the filesystem.
COPY mcp_server/ ./mcp_server/
COPY tests/ ./tests/
COPY scripts/ ./scripts/
# openapi/ogma-api.yaml is the contract source of truth (T4.0).
# Validated by scripts/validate_openapi.py and tests/test_openapi_spec.py;
# both expect the file at /app/openapi/ inside the running container.
COPY openapi/ ./openapi/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default command is a sanity import; compose services override per service.
CMD ["python", "-c", "import vargate_telemetry; print('vargate_telemetry image ready')"]
