# Hyperliquid Quantitative & On-Chain DeFi — Executive Trading Tear-Sheet (2026-11-26 18:24 UTC)

## 1. Сводная карта контура

| Модуль | Статус | Ключевые подтверждённые метрики |
|---|---|---|
| **6-Family OOF Metamodel** (58 гипотез, PR #16–#19) | 🟡 Observation-Only | При `1.5 б.п.`: +32.18% / Sh 3.11; при **измеренных `5.0 б.п.` BBO ALO** (fill 55%, 19 альтов): 1 слой `carry`, Ex-`INIT` **−0.37% / Sh −0.19** → paper-наблюдение на `5.0 б.п.`, без live-капитала |
| **Live Testnet ALO Execution & Attribution** | ✅ Cron 4h + Daily 09:00 UTC | Баланс **$997.71**, сделок 68, оборот $1,063.9, **Maker 86.8%**, fee **1.90 б.п.**, ALO Fill **93.3% (14/15)**, Markout -10.57 б.п., Liq warnings: `0` |
| **Live Paper L2 Shadow Tracker** (HL × Bybit/Binance/OKX) | ✅ Cron 4h | Тиков: 7, активных позиций: 16, Equity `$99,653.89` (`-0.346%`), динамический ALO-throttle |
| **Avellaneda–Stoikov MM v2** (WS `bbo+l2Book`, Lead-Lag, `batchModify`, `scheduleCancel`) | ✅ Live Testnet | Монеты `BTC, ETH` · `bbo`-тиков 36, `batchModify` 16, сделок 15, оборот **$150.13**, Net PnL **+0.0165$**, 100% Maker |
| **Эмпирическая калибровка λ(δ)=Ae^(−κδ)** (Mainnet WS) | ✅ Verified | ETH: κ=0.544 (R²=0.83), BTC: κ=0.327 (R²=0.98) |
| **HyperEVM Chain 999 DeFi & `L1Read` Precompiles** | ✅ Live On-Chain | Блок `#52,312,546`, L1 `#1,235,680,830`, пег USDT0 `+11.1 б.п.`, `L1Read` оракулов: 4, чистых peg-adj арб: 0, HLP TVL **$152.4M** (`+15.66% APR`) |
| **L1 Liquidation Heatmap & Hourly `:00` Funding Alpha** | ✅ Live Mainnet | Китов: 45, зон ликвидации: 6 (лидер BTC ($12.16M в 0–10%)), 1m-микроструктура расчёта `:00` |
| **5-DEX Cross-DEX Arbitrage v2** (HL × Lighter × Extended × dYdX × Paradex) | ✅ Live L2 Depth | Лидер **GRAM** (Long HL / Short lighter): спред **56.5% APR**, L2 slip $10k `8.19 б.п.` |

## 2. Бенчмарк горячего пути котирования (`tick-to-quote`)

- Прогон `3,000` тиков (`parse_bbo -> EWMA -> LeadLag -> compute_as_quotes`, pure Python): **p50 = `10.75 мкс`**, **p95 = `14.13 мкс`**, **p99 = `163.78 мкс`**, пропускная способность **`14,854` тиков/сек**.

## 3. Архитектурные гарантии и риск-контроль

- **Zero Taker Crossing:** жёсткое округление Hyperliquid (`5 sig figs`, `6 - szDecimals`) + BBO-кламп (`Alo`), подтверждено **100.0% Maker** на живых Testnet-филлах.
- **Изоляция маржи и ликвидаций:** автоматическая фильтрация `onlyIsolated`/`strictIsolated` активов + `update_leverage` (`cross:3x`) + ежедневный аудит дистанции до `liquidationPx`.
- **Защита от токсичного потока и обрыва связи:** Binance Futures WS Lead-Lag toxic-side pull (`>= 2 б.п.`), атомарный `batchModify`, нативный `scheduleCancel` (Dead Man's Switch) и `--flatten-on-exit`.
- **Прозрачный ончейн-учёт:** поправка на реальный пег `USDT0/USDC` (1bp пул), расчёт проскальзывания по V3-ликвидности $L$ на клипах `$5k/$25k/$100k` и проверка консенсуса через нативные `L1Read` прекомпилы.
