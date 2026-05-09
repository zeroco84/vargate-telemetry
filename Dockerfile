# Vargate Telemetry — Python image shared by celery-worker, celery-beat,
# and (from T1.4) the FastAPI gateway. Single image, per-service commands.

FROM python:3.12-slim

WORKDIR /app

# curl for in-container health probes; ca-certificates for HTTPS calls
# from inside the workers (Anthropic admin API, etc., from T3.1+).
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

COPY vargate_telemetry/ ./vargate_telemetry/

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Default command is a sanity import; compose services override per service.
CMD ["python", "-c", "import vargate_telemetry; print('vargate_telemetry image ready')"]
