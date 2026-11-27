# syntax=docker/dockerfile:1.7

FROM ghcr.io/astral-sh/uv:0.9.2 AS uv-bin

FROM python:3.11-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH"

WORKDIR /app

COPY --from=uv-bin /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock README.md ./

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

FROM base AS backend

ARG BUILD_SHA=dev
ARG BUILT_AT=""

ENV HL_BUILD_SHA=$BUILD_SHA \
    HL_BUILT_AT=$BUILT_AT \
    HL_DATA_DIR=/app/data \
    HL_DATA_ROOT=/app/data/paper \
    PYTHONPATH=/app/src

LABEL org.opencontainers.image.revision=$BUILD_SHA \
      org.opencontainers.image.created=$BUILT_AT \
      org.opencontainers.image.title="hyperliquid-quant-defi-backend"

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data/paper /app/data/raw \
    && chown -R appuser:appuser /app/data

COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser scripts ./scripts

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=4 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=3)"

CMD ["uvicorn", "hl_quant.api.app:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]

FROM base AS worker

ARG BUILD_SHA=dev
ARG BUILT_AT=""

ENV HL_BUILD_SHA=$BUILD_SHA \
    HL_BUILT_AT=$BUILT_AT \
    HL_DATA_DIR=/app/data \
    HL_DATA_ROOT=/app/data/paper \
    PYTHONPATH=/app/src

LABEL org.opencontainers.image.revision=$BUILD_SHA \
      org.opencontainers.image.created=$BUILT_AT \
      org.opencontainers.image.title="hyperliquid-quant-defi-worker"

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev --extra testnet

RUN useradd --create-home --uid 1000 appuser \
    && mkdir -p /app/data/paper /app/data/raw \
    && chown -R appuser:appuser /app/data

COPY --chown=appuser:appuser src ./src
COPY --chown=appuser:appuser config ./config
COPY --chown=appuser:appuser scripts ./scripts

USER appuser

HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import json, sys, datetime as d; s=json.load(open('/app/data/paper/hl_worker_status.json')); hb=d.datetime.strptime(s['heartbeat_utc'], '%Y-%m-%d %H:%M:%S UTC').replace(tzinfo=d.timezone.utc); sys.exit(0 if (d.datetime.now(d.timezone.utc) - hb).total_seconds() < 300 else 1)"

CMD ["python", "-m", "hl_quant.scheduler"]
