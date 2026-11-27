# Операции, 7 Cron-контуров и Runbook

Практическое руководство по запуску, автоматизации, проверке и безопасному обслуживанию **`hyperliquid-quant-defi`**.

---

## 1. Контракт безопасности

- Все боевые ордера жёстко привязаны к **`https://api.hyperliquid-testnet.xyz`** (мок-USDC, нулевой риск реальных средств).
- Секреты хранятся строго в локальном gitignored `.env` (см. шаблон `.env.example`):
  - `HL_TESTNET_PRIVATE_KEY` / `HL_TESTNET_ACCOUNT_ADDRESS` — API-кошелёк Testnet без права вывода;
  - `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` / `TELEGRAM_THREAD_ID` — уведомления Telegram Bot API 10.1+ (`sendRichMessage` с нативными HTML-таблицами).
- Все cron-обёртки используют lock-директории `.neira/state/*.lock` с проверкой живого PID — параллельные дубли невозможны.

---

## 2. Все 7 локальных Cron-контуров

| Расписание | Обёртка (`scripts/ops/`) | Назначение | Артефакты (`data/paper/`) |
|---|---|---|---|
| `50 * * * *` | `run_hyperliquid_snapshot_tick.sh` | Часовые снимки `predictedFundings` и состояния рынков | `data/raw/hyperliquid/snapshot/` |
| `5 */4 * * *` | `run_hl_metamodel_paper_tick.sh` | Observation-only shadow paper-трекер на измеренных `5.0 б.п.` | `data/paper/hl_metamodel/` |
| `10 */4 * * *` | `run_hl_testnet_executor_tick.sh` | Живой Testnet `ALO` ребалансер (`cross:3x`, `onlyIsolated` guard) | `data/paper/hl_testnet_audit.jsonl` |
| `15 */4 * * *` | `run_hl_cross_dex_paper_tick.sh` | Cross-DEX Paper v2 (`HL × Lighter × Extended`, Persistence + H1 Combo) | `data/paper/hl_cross_dex_paper/` |
| `20 */4 * * *` | `run_hl_frontier_persistence_tick.sh` | Frontier Persistence Tracker (`HIP-3` + `Pendle Boros` + `HyperEVM Yield Stack`) | `data/paper/hl_frontier_history/` |
| `*/15 * * * *` | `run_hl_onchain_event_tick.sh` | On-Chain Event Alpha (`H2/H4/H5/H6` + deposit-clean HLP PnL + 4ч cooldown + 6ч кэш китов) | `data/paper/hl_onchain_events/` |
| `0 6 * * *` (`09:00 UTC`) | `run_hl_testnet_attribution_daily.sh` | Пост-трейд атрибуция (`ALO Fill`, `ADL Rank`, `API Quota`) + авторефреш HTML-дашборда | `data/paper/hl_testnet_attribution/` |

### Единая установка / миграция всех 7 кронов

```bash
zsh scripts/ops/install_all_hl_crons.sh
```

Скрипт автоматически делает резервную копию текущего `crontab` в `.neira/temp/`, удаляет старые/дублирующиеся блоки Hyperliquid и устанавливает все 7 расписаний на текущий репозиторий (используя короткий временный путь `/tmp`, чтобы обойти 99-символьный лимит аргумента macOS BSD `crontab`).

---

## 3. Команды разработки и ручной диагностики

```bash
# Установка окружения
uv sync --extra dev --extra testnet

# Проверка линтера и всех unit-тестов (идентично GitHub Actions CI)
make lint
make test
make frontend-test
make frontend-build
make docker-up   # локальный запуск Nginx + React SPA + FastAPI на http://localhost:8080

# Безопасные 100% read-only живые сканы (без ключей API)
make scan-hip3
make scan-boros
make scan-evm
make bench-mm
.venv/bin/python scripts/analytics/run_cross_dex_arb_scan.py --min-day-vol-usd 500000 --top-n 10
.venv/bin/python scripts/analytics/run_hl_onchain_event_tick.py
.venv/bin/python scripts/analytics/run_hl_frontier_persistence_tick.py
.venv/bin/python scripts/analytics/run_hl_hip3_paper_tick.py

# Безопасный перенос локальных macOS-кронов на 13-джобовый Dokploy worker (с бэкапом)
python3 scripts/ops/cutover_local_crons_to_dokploy.py --public-url https://hl-quant.95-217-163-111.sslip.io --apply

# Пересборка автономного 6-вкладочного HTML-дашборда
make dashboard

# Синхронизация docs/wiki/*.md -> GitHub Wiki
zsh scripts/ops/publish_wiki.sh
```
