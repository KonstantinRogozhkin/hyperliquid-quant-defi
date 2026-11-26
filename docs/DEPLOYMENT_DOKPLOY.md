# Docker & Dokploy CI/CD Deployment Guide

This repository ships as a three-container Docker Compose stack behind Dokploy Traefik:

- **`frontend`** (`nginx:1.27-alpine` + React/Vite/TS SPA): serves the live quant desk UI, `/build-info.json`, and proxies `/api/*` → `http://backend:8000` (single public domain, zero CORS setup).
- **`backend`** (`python:3.11-slim` + `FastAPI`/`uvicorn`): read-only telemetry API mounted on `/app/data:ro`, never receives wallet private keys or Telegram tokens.
- **`worker`** (`python:3.11-slim` + `hl_quant.scheduler` + `--extra testnet`): replaces all 7 local macOS crons plus 2 dashboard scanners, writes atomically to shared volume `hl-data:/app/data:rw`.

---

## 1. Local Docker Preview

Preview the full stack locally against your existing `./data/paper` folder at `http://localhost:8080`:

```bash
docker compose up --build
```

To also run the background `worker` locally (keep local macOS crons disabled first to avoid duplicate ticks):

```bash
docker compose --profile worker up --build
```

---

## 2. One-Time Dokploy Setup

1. In Dokploy, create a new **Compose** service connected to GitHub repository `KonstantinRogozhkin/hyperliquid-quant-defi`:
   - **Branch:** `main`
   - **Compose Path:** `./docker-compose.dokploy.yml`
   - **Auto Deploy:** **OFF** (GitHub Actions controls releases strictly after CI & container smoke checks pass).
2. In the Dokploy Compose **Environment** tab, configure:

```dotenv
IMAGE_REGISTRY=ghcr.io
IMAGE_NAMESPACE=konstantinrogozhkin
DEPLOY_SHA=<initial-40-char-sha-from-main>

# Worker behavior (read-only bootstrap populates all tabs within ~60s on first start)
HL_WORKER_BOOTSTRAP_READONLY=true

# Optional Telegram alerts (worker only)
HL_TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=

# Optional Hyperliquid Testnet execution (strictly OFF by default)
HL_ENABLE_TESTNET_EXECUTION=false
HL_TESTNET_PRIVATE_KEY=
HL_TESTNET_ACCOUNT_ADDRESS=
HL_TESTNET_MAX_CHILD_USD=250
```

3. In the Dokploy **Domains** tab, attach your HTTPS domain (e.g. `hl.example.com`) to:
   - **Service:** `frontend`
   - **Container Port:** `80`
   - **HTTPS:** enabled (Let's Encrypt via Dokploy Traefik).
4. Make sure the GHCR packages `hl-backend`, `hl-worker`, and `hl-frontend` are **Public** (or add GHCR registry credentials in Dokploy if you switch them to Private).

---

## 3. One-Time GitHub Repository Secrets & Variables

Under **GitHub → Settings → Secrets and variables → Actions**:

### Repository Secrets
| Name | Example / Description |
|---|---|
| `DOKPLOY_API_URL` | `https://your-dokploy-host/api` |
| `DOKPLOY_API_KEY` | Dokploy API key (`Settings → Profile → API Keys`) |
| `DOKPLOY_COMPOSE_ID` | Target Compose ID from Dokploy URL |

### Repository Variables
| Name | Example / Description |
|---|---|
| `DOKPLOY_DEPLOY_ENABLED` | `true` (leave unset or `false` to build & push GHCR images only) |
| `DOKPLOY_PUBLIC_URL` | `https://hl.example.com` (used for live `/build-info.json` and `/api/v1/healthz` verification) |

---

## 4. Release & Rollback Flow

1. Every change enters `main` via a **Pull Request** gated by `ci.yml` (`tests` + `frontend`).
2. Merging to `main` triggers `.github/workflows/release.yml`:
   - Builds `hl-backend`, `hl-worker`, `hl-frontend` with `BUILD_SHA=$GITHUB_SHA`.
   - Runs pre-push container smoke tests (`/healthz`, `/build-info.json`, worker scheduler & SDK check).
   - Pushes immutable `ghcr.io/<owner>/hl-*:sha-<GITHUB_SHA>` tags.
   - Calls `scripts/ops/deploy_dokploy.py` to pin `DEPLOY_SHA=<GITHUB_SHA>` in Dokploy, trigger `compose.deploy`, wait for `status == done`, and assert both frontend and backend serve the exact `GITHUB_SHA`.
3. **Instant Rollback:** set `DEPLOY_SHA=<previous-40-char-sha>` in Dokploy Environment and click **Deploy** (no rebuild needed; every historical SHA image remains immutable in GHCR).

---

## 5. Cutting Over from Local macOS Crons

Once the Dokploy `worker` tab under **Scheduler & Ops** reports fresh heartbeats and green jobs, remove the 7 local macOS crons so Telegram alerts and Testnet ticks are not duplicated:

```bash
crontab -l | awk '/# BEGIN hyperliquid-quant-defi /{s=1;next} /# END hyperliquid-quant-defi /{s=0;next} !s' | crontab -
```
