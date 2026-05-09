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
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
         curl \
         ca-certificates \
         softhsm2 \
         build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY vargate_telemetry/ ./vargate_telemetry/
COPY tests/ ./tests/
COPY scripts/ ./scripts/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default command is a sanity import; compose services override per service.
CMD ["python", "-c", "import vargate_telemetry; print('vargate_telemetry image ready')"]
