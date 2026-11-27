# Docker & Dokploy CI/CD Deployment Guide

This repository ships as a three-container Docker Compose stack behind Dokploy Traefik:

- **`frontend`** (`nginx:1.27-alpine` + React/Vite/TS SPA): serves the live quant desk UI, `/build-info.json`, and proxies `/api/*` → `http://backend:8000` (single public domain, zero CORS setup).
- **`backend`** (`python:3.11-slim` + `FastAPI`/`uvicorn`): read-only telemetry API mounted on `/app/data:ro`, never receives wallet private keys or Telegram tokens.
- **`worker`** (`python:3.11-slim` + `hl_quant.scheduler` + `--extra testnet`): runs all **13 UTC jobs** (the 7 core schedules plus 6 read-only dashboard/paper scanners: `hip3_paper`, `cross_dex_scan`, `hyperevm_defi_scan`, `liq_funding_alpha`, `mm_calibration`, and `settlement_study`), preserves job health across restarts, sends cooldown-gated Telegram alerts on job failure/recovery, writes atomically to shared volume `hl-data:/app/data:rw`, and seeds any missing read-only tab in the background on startup.

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

# Optional Telegram alerts (worker only; accepts TELEGRAM_* or TELEGRAM_SIGNAL_*)
HL_TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
TELEGRAM_THREAD_ID=

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

Once the Dokploy `worker` tab under **Scheduler & Ops** reports fresh heartbeats and green jobs, use the pre-flight cutover script (it checks live `/build-info.json`, `/api/v1/healthz`, and `/api/v1/scheduler/status`, saves a timestamped backup under `data/crontab_backups/`, and preserves all unrelated cron entries):

```bash
# 1. Dry-run (verifies Dokploy worker heartbeat + prints unified crontab diff)
python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl.example.com

# 2a. Cut over all 7 schedules (when Dokploy worker has HL_ENABLE_TESTNET_EXECUTION=true)
python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl.example.com --expect-testnet-worker --apply

# 2b. Or cut over the 6 data/paper/alert schedules while keeping local testnet_executor active
python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl.example.com --keep-local-testnet --apply

# 3. Instant rollback from backup if ever needed
python3 scripts/ops/cutover_local_crons_to_dokploy.py --restore data/crontab_backups/crontab_<timestamp>.txt --apply
```
