# Docker, Dokploy & Full-Stack CI/CD

Архитектура production-развёртывания **`hyperliquid-quant-defi`** в Docker / Dokploy с неизменяемыми SHA-образами в GHCR и защищённой веткой `main`.

---

## 1. Топология контейнеров (`docker-compose.dokploy.yml`)

| Сервис | Образ (GHCR) | Монтирование / порты | Роль и изоляция секретов |
|---|---|---|---|
| **`frontend`** | `ghcr.io/konstantinrogozhkin/hl-frontend:sha-<SHA>` | `expose: 80` (через Dokploy Traefik HTTPS) | Nginx `1.27-alpine` + React/Vite/TS SPA, `/build-info.json`, `/nginx-health`, динамический Docker DNS `resolver 127.0.0.11` для прокси `/api/* → http://backend:8000`. Секретов не получает. |
| **`backend`** | `ghcr.io/konstantinrogozhkin/hl-backend:sha-<SHA>` | `hl-data:/app/data:ro`, internal `:8000` | Read-only `FastAPI` + `uvicorn` (`hl_quant.api.app`). Эндпоинты `/healthz`, `/build-info.json`, `/api/v1/overview`, `/api/v1/metamodel/positions`, `/api/v1/hip3/persistence`, `/api/v1/events/latest`, `/api/v1/attribution`, `/api/v1/scheduler/status`. Ключи кошелька и Telegram **не** прокидываются. |
| **`worker`** | `ghcr.io/konstantinrogozhkin/hl-worker:sha-<SHA>` | `hl-data:/app/data:rw`, без портов | Контейнерный UTC-планировщик `hl_quant.scheduler` (9 задач с catch-up пропущенных минут, per-job lock'ами, `SIGTERM`-пробросом в группы дочерних процессов и атомарным heartbeat `hl_worker_status.json`). `HL_ENABLE_TESTNET_EXECUTION=false` по умолчанию. |

---

## 2. CI/CD и защита `main`

- **GitHub Ruleset `Protect main (PR + CI)`:** прямой push, force-push и удаление `main` заблокированы; обязательны Pull Request и зелёные чеки **`tests`** (Python `ruff` + `pytest`) и **`frontend`** (`Vitest` component tests + TypeScript `tsc --noEmit` + `vite build`), все GitHub Actions на нативном Node 24.
- **Быстрый path-skip внутри джоб (`ci.yml`):** при docs/backend-only PR `frontend` завершается за ~4s (не подвешивая required checks); при frontend-only PR аналогично быстро завершается `tests`.
- **Авторелиз при мерже в `main` (`release.yml`):**
  1. `gate`: проверяет, что `HEAD == main` (не деплоит устаревший SHA при серии мержей).
  2. `build-and-push`: собирает `hl-backend`, `hl-worker`, `hl-frontend` с `BUILD_SHA=$GITHUB_SHA`, прогоняет **pre-push container smoke tests** (`/healthz`, `/build-info.json`, worker SDK & 9 jobs, standalone Nginx `/nginx-health`) и пушит immutable `:sha-<GITHUB_SHA>` + `:latest` в публичный GHCR (`ghcr.io`).
  3. `deploy-dokploy` (при `vars.DOKPLOY_DEPLOY_ENABLED == 'true'`): вызывает `scripts/ops/deploy_dokploy.py`, обновляет `DEPLOY_SHA` через Dokploy API (`compose.update`), запускает `compose.deploy`, дожидается `status == done` и проверяет совпадение `sha` на живых `/build-info.json` и `/api/v1/build-info.json`.

---

## 3. Быстрый запуск и откат

- **Локальный full-stack предпросмотр (`http://localhost:8080`):**
  ```bash
  make docker-up
  ```
- **Мгновенный откат в Dokploy:** поменять `DEPLOY_SHA` на нужный 40-символьный SHA предыдущего релиза в Environment и нажать **Deploy** — пересборка не требуется.
- **Безопасный cutover локальных macOS-кронов на Dokploy `worker`:**
  ```bash
  # Dry-run с проверкой живого heartbeat /api/v1/scheduler/status
  python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl.example.com
  # Применить (сохраняет бэкап в data/crontab_backups/ и комментирует только блоки hyperliquid-quant-defi)
  python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl.example.com --apply
  ```
- Полная пошаговая инструкция (переменные Dokploy, секреты GitHub, `--keep-local-testnet` и `--restore`): [docs/DEPLOYMENT_DOKPLOY.md](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/DEPLOYMENT_DOKPLOY.md).
