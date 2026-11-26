# Hyperliquid Quantitative & On-Chain DeFi — Portfolio & Case Studies (EN + RU)

> **Repository:** https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi
> **Technical Wiki (13 pages, architecture & live results):** https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki
> **Executive Tear-Sheet:** [hl_trading_tear_sheet.md](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_trading_tear_sheet.md)
> **Interactive 5-Tab Offline Desk Dashboard:** [hl_desk_dashboard.html](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_desk_dashboard.html)

---

## Part 1 — English Executive Summary & Case Studies

### System Overview
End-to-end quantitative trading, market making, and on-chain DeFi infrastructure engineered specifically for **Hyperliquid L1**, **HyperEVM (Chain 999)**, and multi-DEX perpetual markets. Every number below is backed by reproducible code, **91 passing unit tests** (`0` warnings), **5 active automated crons**, and dated live telemetry artifacts.

- **Execution-Realistic Alpha Research (PR #13–#19):** 6-family alpha factory (**58 hypotheses**, strict Dev 75d / Holdout 45d split) initially showed **+32.18% APR / Sharpe 3.11** at idealized `1.5 bps` maker fees (`+22.37%` ex-`INIT`). A 60-minute live `bbo + trades` audit across 19 portfolio altcoins revealed `55%` median ALO fill and **`5.0 bps` measured all-in HL cost**; re-running at `5.0 bps` (including 10 slowed reversal variants) collapsed Ex-`INIT` Holdout to **`−0.37% / Sh −0.19`** → strategy honestly downgraded to **observation-only** (`5.0 bps` paper cron) rather than deploying real capital on backtest illusion.
- **Live Testnet `ALO` Execution & Post-Trade Attribution:** Automated 4h `Alo` post-only rebalancer achieving **93.3% real `ALO` fill rate** on small clips, exact Unified Account equity tracking (`spot.USDC.total` verified to 6 decimals against `portfolio` API), automatic `onlyIsolated` risk filtering (`ZRO` closed cleanly, 0 liquidation alerts), and daily attribution telemetry + local dashboard refresh.
- **Avellaneda–Stoikov Market Maker v2 (WebSocket `bbo+l2Book`):** Empirically calibrated fill-intensity $\kappa$ on Mainnet WS (`ETH κ=0.544 R²=0.83`, `BTC κ=0.327 R²=0.98`), Binance Futures WS Lead-Lag toxic-side pull (`>= 2 bps`), atomic `batchModify`, `scheduleCancel` Dead Man's Switch, and **`p50 = 5.62 µs`** tick-to-quote hot path (`175k ticks/s`). Live `BTC+ETH` Testnet session: **100% Maker, +$0.0165 net PnL after fees, +4.51 bps ETH 5s markout**.
- **5-DEX Cross-DEX Arbitrage & Persistence Tracker:** Live L2 VWAP depth (`$10k / $50k`) across **810 perps** (Hyperliquid, Lighter, Extended, dYdX v4, Paradex) + automated 4h **HL × Lighter** dual-leg paper tracker (`5` L2-gated pairs averaging **+33.1% APR** at `−3.8 bps` blended entry cost).
- **HyperEVM (Chain 999) & L1 Microstructure Alpha:** Native `L1Read` precompiles (`0x0806/0x0807/0x0809`, `0.00 bps` BTC/ETH drift), live `USDT0/USDC` 1bp peg adjustment (`+11.08 bps` eliminating false arbs), exact V3 virtual depth (`$53.97M` on `WHYPE/USDC`), HLP vault telemetry (`$152.45M` TVL), 45-whale on-chain liquidation heatmap, and a **17-day / 17,874-event causal OOS study** of `:00` hourly funding settlements integrated directly into MM & executor filters.

---

### Verified System Map & Live Metrics (EN)

| Module | Live / Verification Scope | Key Verified Metrics | Proof (Commit / Report) |
|---|---|---|---|
| **6-Family Metamodel & BBO Cost Audit** | 58 hypotheses + 60m live `bbo` ALO check (19 alts) | `1.5 bps`: `+32.18% / Sh 3.11`; **measured `5.0 bps`** (`55%` fill): 1 carry layer, **Ex-`INIT` −0.37%** → **Observation-Only** (live paper set to `5.0 bps`) | [PR #16–#19 / `5b03260`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/5b03260) · [BBO ALO Report](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_alo_fill_portfolio_coins.md) · [Real-Cost Report](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_metamodel_real_costs.md) |
| **Live Testnet `ALO` Executor & Attribution** | 4h execution cron + 09:00 UTC daily attribution cron | **93.3% ALO Fill Rate (`14/15`)**, 0 liq alerts, `onlyIsolated` guard, Unified Account exact sync | [`3624e7a`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/3624e7a) · [`7984ca3`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/7984ca3) |
| **Avellaneda–Stoikov MM v2 (`bbo+l2Book`)** | Mainnet WS $\kappa$-calibration + live `BTC+ETH` Testnet | `ETH κ=0.544`, `BTC κ=0.327`; live **100% Maker, +$0.0165 net PnL, ETH +4.51 bps 5s markout**; **`p50 = 5.62 µs`** | [`2635b73`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/2635b73) · [`ced8b73`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/ced8b73) · [Calibration Report](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_mm_ws_calibration.md) |
| **5-DEX Scanner v2 + HL×Lighter Paper** | 810 perps (HL, Lighter, Extended, dYdX, Paradex) + 4h cron | L2 VWAP `$10k/$50k` separates real edge (`ETH/SOL < 2 bps`) from Paradex `88 bps` traps; **5 live HL×Lighter pairs @ +33.1% APR** | [`da88dc5`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/da88dc5) · [5-DEX Report](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_cross_dex_v2_scan.md) · [Paper Log](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_cross_dex_paper_live.md) |
| **HyperEVM (Chain 999) + `L1Read` + HLP** | Direct `eth_call` on Chain 999 RPC + HLP child vaults | `L1Read` (`0x0806/07/09`): **0.00 bps** BTC/ETH drift; `USDT0/USDC` peg **+11.08 bps**; `WHYPE/USDC` **$53.97M** depth; HLP **$152.45M** TVL (`+15.66% APR`) | [`f96a215`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/f96a215) · [`ced8b73`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/ced8b73) · [HyperEVM Report](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_hyperevm_defi_scan.md) |
| **L1 Whale Liq Map & 17d `:00` Funding OOS** | 45 active top whales + 43 coins / 17,874 `:00` settlements | Proved `naive_harvest_5m` loses **−5.85 bps** net while `post_unwind_fade_5m` delivers **+3.84 / +2.27 bps** gross (`t=+3.3 / +2.4`) → wired into MM & Executor | [`447f82c`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/447f82c) · [`da88dc5`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/da88dc5) · [OOS Study](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_hourly_funding_settlement_study.md) |
| **Ops, Telemetry & Showcase** | **6 active local crons**, Telegram Rich HTML, **97 unit tests** | Zero warnings, 100% pass rate, offline 6-tab interactive HTML desk dashboard (daily auto-refresh) | [`7ba3391`](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/commit/7ba3391) · [Dashboard HTML](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_desk_dashboard.html) |
| **HIP-3 (`xyz`/`io`/`para`), Pendle Boros & Yield Stack** | **10 HIP-3 builders (`$2.32B/24h`)**, **39 active Boros** (`17` HL/Spread), `kHYPE/Felix/HyperLend` | Zero-bridge **`xyz:NVDA / io:NVDA` `+39.0% APR` (`0.7 bps` L2 slip, Cross, 16h payback)**; Boros locks `HYPE +8.94%` / `BTC +6.69%`; `kHYPE+Boros` loop nets **`+9.07%`** vs **`HLP +15.68%`** | [HIP-3 Live](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_hip3_builder_arb_live.md) · [Boros Live](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_boros_fixed_funding_live.md) · [Yield Stack](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/blob/main/docs/reports/2026-11-26/hl_hyperevm_yield_stack_live.md) |

---

### Four Production Case Studies (Problem → Engineering → Honest Result)

#### Case 1 — Killing Our Own "+32% APR" Backtest with Real Top-of-Book `bbo` Fill Telemetry (PR #13–#19)
- **Hypothesis:** Multi-venue funding carry (`HL` vs `Bybit/Binance/OKX`) + short-term altcoin reversal (`xs_rev_L24_r12`) + momentum (`xs_mom`) delivered **+32.18% APR (Sh 3.11)** on 45d Holdout (**+22.37% / Sh 2.33** ex-`INIT`) at `1.5 bps` HL maker cost.
- **How we tested the key assumption:**
  1. Discovered that public `l2Book` pushes only every 7–12s and `trades` emits historical snapshots on connect. Fixed `hl_microstructure.py` (PR #16) to capture per-block `bbo` ticks and drop pre-subscription trades, then ran a **60-minute live capture** across all **19 active portfolio alts** (`180,873` BBO ticks, `43,513` trades, `$5,000` ALO order).
  2. Measured truth: **median ALO fill rate = 55%** (only `VVV 90%`, `ZEC 88%`, `HYPE 85%` cleared the `85%` threshold), 30s markout was negative across all 19 coins, and **all-in passive HL execution cost was `5.0 bps`** (nearly equal to taker `5.6 bps`).
  3. Re-ran the 48-hypothesis selection + 10 pre-registered slow reversal variants (`58` total, PR #17–#18) at `5.0 bps` HL / `7.0 bps` cross-venue: only `carry_best_cex_L168_in5` passed Dev, and **Ex-`INIT` Holdout dropped to `−0.37% / Sh −0.19`**; slowed `xs_rev` proved strongly regime-dependent (Dev folds 2–4 deep negative).
- **Production Decision:** Refused to overfit a regime filter on 180d history, updated `run_hl_metamodel_paper_tick.py` to charge **`5.0 bps` all-in** (PR #19), kept the metamodel strictly **observation-only**, and shifted capital focus to edges that survive real microstructure (Lead-Lag MM v2, L2-gated Cross-DEX HL×Lighter, HyperEVM `L1Read`, `:00` zero-fee overlays).

#### Case 2 — Live Avellaneda–Stoikov Market Maker with Binance Lead-Lag & Empirical $\kappa$
- **Problem:** Text-book A–S spreads fail on Hyperliquid if $\kappa$ is guessed, if quotes update on slow 20-level `l2Book` snapshots (~5–10s), or if Binance Futures moves first and sweeps resting HL quotes.
- **Engineering:**
  - Built an empirical Poisson fill-intensity calibrator on Mainnet WS (`ETH κ=0.544 R²=0.83`, `BTC κ=0.327 R²=0.98`).
  - Subscribed concurrently to Hyperliquid `bbo` (~114–200ms) + `l2Book` (for `OBI5`) + Binance Futures `bookTicker` WS (`LeadLagEstimator`: pulls the toxic side immediately when Binance mid leads HL by `>= 2 bps`).
  - Used atomic `bulk_modify_orders_new`, native `scheduleCancel` Dead Man's Switch (with graceful fallback below the `$1M` exchange volume threshold), strict Hyperliquid price rounding (`5 sig figs`, `6 - szDecimals`), and `--flatten-on-exit`.
- **Live Result:** `p50 = 5.62 µs` hot path (`175,088 ticks/s`), live Testnet `BTC+ETH` session logged `36 bbo` ticks, `16 batchModify`, `5` Binance Lead-Lag toxic pulls on ETH, **15 fills ($150.13 vol), 100% Maker, +$0.0165 net PnL after fees, +4.51 bps ETH 5s markout**, zero residual open orders.

#### Case 3 — Separating Real HyperEVM & Cross-DEX Edge from On-Chain Mirages
- **HyperEVM (Chain 999):** Raw spot vs HyperCore perp scans showed tempting `~11–13 bps` spreads in `USDT0` pools. Querying `PRJX V3 USDC/USDT0 1bp` on-chain proved `1 USDC = 1.001108 USDT0` (`+11.08 bps` discount). After exact peg normalization + exact V3 virtual depth (`$5k/$25k/$100k` slippage) + native `L1Read` precompiles (`0x0806/0x0807/0x0809`, `0.00 bps` drift on BTC/ETH), false arbs disappeared cleanly.
- **5-DEX Perps (810 markets):** Naive funding-rate scanning highlighted `BTC` HL vs Paradex (`12.5% APR`), but live L2 VWAP revealed **`88.0 bps` slippage on `$10k`** (`1,513h` payback — uninvestable). Filtering by real L2 depth surfaced executable **HL × Lighter** pairs (`BTC`, `SOL`, `NEAR`, `GRAM`, `XMR` averaging **+33.1% APR** at `−3.8 bps` entry cost), now tracked live every 4h to prove spread persistence over payback hours.

#### Case 4 — Causal 17-Day Study of Hyperliquid's `:00` Hourly Funding Settlement
- **Question:** Because HL settles funding hourly (`8×` more often than CEXs), can you harvest extreme negative funding or fade the post-settlement unwind?
- **Causal Proof (43 liquid coins, 17,874 settlements, Dev 65% vs Holdout 35%, zero lookahead using `f(T0 - 1h)`):**
  - `naive_harvest_5m` (`APR <= -25%`) **loses money reliably**: `−2.85 bps` gross / **`−5.85 bps` net Maker** in Holdout (`t = −2.2`).
  - `post_unwind_fade_5m` (short `T0 → T+5m` on `APR <= -25%`) shows statistically significant gross alpha in both splits: **+3.84 bps (`t = +3.3`) Dev** and **+2.27 bps (`t = +2.4`) Holdout** (`N = 2,025`) — too tight for standalone `3.0 bps` round-trip fees, **ideal as a zero-fee execution / MM skew overlay**.
- **Production Integration:** Wired directly into `hl_mm_avellaneda_stoikov.py` (`−2.5 bps` bearish quote skew in `:59 → :05` when `APR <= -25%`) and `hl_testnet_executor.py` (defers new negative-funding long entries in `:55 → :59` to avoid paying the unwind drop).

---

## Часть 2 — Краткое резюме проекта на русском

Репозиторий `s2m-btester` реализует полный цикл количественного трейдинга и DeFi-инфраструктуры для **Hyperliquid L1**, **HyperEVM (Chain 999)** и кросс-DEX перпов (**91 unit-тест**, 5 автоматических cron-контуров, живая телеметрия в Telegram и автономный HTML-дашборд):

- **Честный аудит альфа-метамодели (58 гипотез, PR #13–#19):** при идеальных `1.5 б.п.` модель давала `+32.18% APR / Sh 3.11`, но 60-минутный живой замер `bbo + trades` по 19 альтам портфеля показал медианный `ALO fill = 55%` и полную цену сделки **`5.0 б.п.`** (почти как у тейкера `5.6 б.п.`). Пересчёт на `5.0 б.п.` (включая 10 замедленных версий `xs_rev`) обнулил Ex-`INIT` Holdout (`−0.37% / Sh −0.19`), поэтому метамодель честно переведена в **observation-only** (`paper`-крон на `5.0 б.п.`) без риска реальным капиталом.
- **Живой Testnet `ALO` экзекутор и пост-трейд атрибуция:** 4ч ребаланс пассивными `Alo`-ордерами (**93.3% реальный ALO Fill Rate**), точный учёт `Unified Account` (`spot.USDC.total` до 6 знака), автозащита от `onlyIsolated` активов (0 предупреждений по ликвидации) и ежедневный аудит комиссий/markout.
- **Маркет-мейкер Avellaneda–Stoikov v2 (WebSocket `bbo+l2Book`):** эмпирическая калибровка $\kappa$ на Mainnet (`ETH κ=0.544`, `BTC κ=0.327`), защита от токсичного потока через Binance Futures WS Lead-Lag (`>= 2 б.п.`), атомарный `batchModify`, `scheduleCancel` Dead Man's Switch и горячий путь **`p50 = 5.62 мкс`** (`175k` тик/с). Живая сессия `BTC+ETH`: **100% Maker, +$0.0165 чистый PnL, ETH +4.51 б.п. markout +5с**.
- **Кросс-DEX v2 (5 DEX / 810 перпов) + живой Paper-трекер HL × Lighter:** расчёт реального L2 VWAP на `$10k/$50k` и 4ч проверка персистентности спредов (`5` активных пар со средним **+33.1% APR** при L2-входе `−3.8 б.п.`).
- **HyperEVM (Chain 999) + L1-микроструктура `:00`:** нативные прекомпилы `L1Read` (`0.00 б.п.` дрейф), поправка на реальный пег `USDT0/USDC` (`+11.08 б.п.`), V3-глубина пулов, аналитика HLP (`$152.45M` TVL), карта ликвидаций 45 топ-китов и каузальное 17-дневное OOS-исследование расчётов `:00` (`17 874` события), встроенное прямо в котирование MM и фильтр экзекутора.

Подробная документация по архитектуре и каждому модулю: https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki
