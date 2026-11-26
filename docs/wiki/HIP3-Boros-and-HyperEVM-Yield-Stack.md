# HIP-3 Builder-Perps, Pendle Boros и HyperEVM Yield Stack (2026)

Три передовых направления экосистемы Hyperliquid 2025–2026 годов, полностью покрытые read-only сканерами, живыми L2-проверками и unit-тестами.

---

## 1. HIP-3 Builder-Perps и внутрибиржевой кросс-билдер арбитраж (`hl_hip3_builder_arb.py`)

**Файлы:** `src/hl_quant/hl_hip3_builder_arb.py`, `scripts/analytics/run_hl_hip3_arb_scan.py`, `tests/test_hl_hip3_builder_arb.py`, отчёт `docs/reports/2026-11-26/hl_hip3_builder_arb_live.md`.

### Что делает модуль
1. Автоматически находит все сторонние HIP-3 перп-DEX через `{"type": "perpDexs"}` (`10` билдеров, `178` активных рынков, оборот **`$2.20B–$2.32B / 24ч`**) и загружает их контексты через `{"type": "metaAndAssetCtxs", "dex": "<name>"}`, включая live-флаг **`growthMode`** (`135/150` рынков `xyz`, `18/26` `io`, `27/40` `para`) и `deployerFeeScale`.
2. **Growth Mode (`-90%` к комиссиям и API-квоте), 30-дневный кулдаун & Oracle Divergence Guard:** автоматически применяет льготную шкалу `Growth Mode` (`0.45–0.90 б.п.` тейкер / `0.15–0.30 б.п.` мейкер, учитывая `-90%` вес объёма в L1 API rate-limit → `1 req / 10 USDC`), парсит `lastGrowthModeChangeTime` / `lastFeeScaleChangeTime` (остаток 30-дневного локапа) и выставляет **`oracle_risk_warning`** при расхождении оракулов `> 15 б.п.` (кейс SK Hynix `28 Jul 2026`: `$57.4M` ликвидаций).
3. **Независимое маржирование HIP-3 DEX (`independent_dex_margin=True`):** `xyz` и `io` имеют раздельный клиринг без автоматического кросс-неттинга (плюс право деплоера на `haltTrading` по своей `mark price`), поэтому в расчёт добавлен `net_apr_split_margin_pct` для раздельного буфера маржи на обеих ногах.

### Подтверждённый живой срез с Growth Mode и Oracle Guard (2026-11-26 20:41 UTC)

| Тикер | Ноги HIP-3 | Спред APR | Growth | Oracle Div | L2 Exec Basis `$5k` | Slip `$5k / $25k` | Окуп. Maker / Taker | Режим |
|---|---|---:|---|---:|---:|---:|---:|---|
| **`NVDA`** | `S xyz:NVDA / L io:NVDA` | **+77.5%** | ⚡ GM | ⚠️ 17.8 б.п. | −7.7 б.п. | **1.7 / 3.8 б.п.** | **5ч / 8ч** | `Cross` |
| **`SKHX`** | `S xyz:SKHX / L io:SKHX` | **+75.2%** | ⚡ GM | **⚠️ 46.0 б.п.** | −29.6 б.п. | 2.2 / 4.5 б.п. | 6ч / 9ч | `Cross` |
| **`DRAM`** | `S xyz:DRAM / L io:DRAM` | **+50.0%** | ⚡ GM | ⚠️ 21.8 б.п. | −8.5 б.п. | 3.4 / 7.5 б.п. | **14ч / 18ч** | `Cross` |
| **`SNDK`** | `S xyz:SNDK / L io:SNDK` | **+40.3%** | ⚡ GM | ⚠️ 19.7 б.п. | −8.3 б.п. | **0.7 / 2.3 б.п.** | **6ч / 11ч** | `Cross` |
| **`NBIS`** | `S xyz:NBIS / L io:NBIS` | **+180.0%** | ⚡ GM | ⚠️ 21.8 б.п. | +1.5 б.п. | 3.5 / 9.9 б.п. | 3ч / 4ч | `⚠️ Iso` |

---

## 2. Pendle Boros — Фиксация ставок фандинга и Spread Markets (`hl_boros_fixed_funding.py`)

**Файлы:** `src/hl_quant/hl_boros_fixed_funding.py`, `scripts/analytics/run_hl_boros_scan.py`, `tests/test_hl_boros_fixed_funding.py`, отчёт `docs/reports/2026-11-26/hl_boros_fixed_funding_live.md`.

Проходит по всем рынкам `https://api-boros.pendle.finance/apis/v1/markets` через курсорную пагинацию `resumeToken` (`249` рынков → `39` активных → `17` Hyperliquid & Spread):
- **`Hyperliquid HYPE 25 Dec 2026`** (`28.2д`, `OI 392k`): фиксация **`+8.94%` APR** (`bestBid`, цена спреда `3.4 б.п.`).
- **`Hyperliquid BTC 25 Dec 2026`** (`28.2д`, `OI 5.2M`): фиксация **`+6.69%` APR** (`3.8 б.п.`).
- **`Hyperliquid xyz:BRENTOIL 18 Dec 2026`** (`21.2д`): фиксация **`+20.00%` APR** по HIP-3 нефти (`float −28.07%`).
- **`Hyperliquid ETH − Gate ETHUSDT 25 Dec 2026 [SPREAD]`**: `bestBid +2.28%` против `float −4.08%` — дислокация **`+6.36%` APR** при спреде всего `0.2 б.п.`.

---

## 3. HyperEVM Composable Delta-Neutral Yield Stack (`hl_hyperevm_yield_stack.py`)

**Файлы:** `src/hl_quant/hl_hyperevm_yield_stack.py`, `scripts/analytics/run_hl_hyperevm_yield_stack.py`, `tests/test_hl_hyperevm_yield_stack.py`, отчёт `docs/reports/2026-11-26/hl_hyperevm_yield_stack_live.md`.

Собирает живые ставки DeFiLlama (`kHYPE $554.7M @ 1.88%`, `stHYPE $135.6M @ 1.99%`, `HyperLend kHYPE $158.5M`, `Felix WHYPE $15.9M`, `Morpho FEUSDCV2 $13.4M @ 5.20%`, `HLP $152.6M @ 15.68%`), фандинг `HYPE` (`+10.95%`) и фиксированный бид Boros (`+8.94%`):

| Связка | Тип | Gross | Эфф. капитала | **Net APR на депозит** | vs HLP | Буфер ликв. |
|---|---|---:|---:|---:|---:|---:|
| **Composable Loop (`kHYPE → Felix/HyperLend 50% LTV → Short HYPE + Boros`)** | `Fixed (Boros)` | +10.82% | 100% | **+9.07%** | −6.61% | 36% |
| `kHYPE + Short HYPE Perp` | `Floating` | +12.83% | 66.7% | **+8.55%** | −7.13% | 42% |
| `kHYPE + Short HYPE + Boros Fix (28d)` | `Fixed (Boros)` | +10.82% | 66.7% | **+7.21%** | −8.47% | 42% |
| **Benchmark: HLP Vault** | `Benchmark` | +15.68% | 100% | **+15.68%** | 0.00% | 100% |

> **Честный вывод:** при текущем фандинге `HYPE +10.95%` пассивный **HLP (`+15.68%`)** обыгрывает дельта-нейтральные связки `kHYPE` на `+6.6%…+8.5%` APR. Лупинг через Felix/HyperLend + Boros становится выгоднее HLP, когда фандинг `HYPE` (или фиксированный бид Boros) превышает `~18% APR`.

---

## 4. 4-часовой Frontier Persistence Tracker (`hl_frontier_persistence.py`, cron `20 */4 * * *`)

Каждые 4 часа в 100% read-only режиме собирает единый срез **HIP-3 + Pendle Boros + HyperEVM Yield Stack**, обновляет `snapshot.json` всех трёх модулей для HTML-дашборда и накапливает append-only Parquet-историю (`data/paper/hl_frontier_history/`) для измерения реальной живучести (`clean_streak` при `APR >= 20%` и `Oracle Div <= 25 б.п.`) и динамики ставок Boros vs HLP.
