<p align="center">
  <img src="./assets/readme/hero.svg" width="100%" alt="hyperliquid-quant-defi — Hyperliquid quantitative trading, Avellaneda-Stoikov market making, HIP-3 Growth Mode arbitrage, 5-DEX funding carry, and HyperEVM Chain 999 on-chain DeFi stack">
</p>

<p align="center">
  <a href="https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki"><b>Full Technical Wiki (10 pages)</b></a> ·
  <a href="docs/DEPLOYMENT_DOKPLOY.md"><b>Docker &amp; Dokploy CI/CD Guide</b></a> ·
  <a href="docs/reports/2026-11-26/hl_defi_trader_portfolio_brief.md"><b>Portfolio &amp; Case Studies (EN + RU)</b></a> ·
  <a href="docs/reports/2026-11-26/hl_trading_tear_sheet.md"><b>Executive Tear-Sheet</b></a>
</p>

---

## Verified Live Results

Standalone full-stack quant suite: Python package **`hl_quant`** (`src/hl_quant/`, zero external monorepo dependencies, **186+ passing unit tests**, zero warnings), **FastAPI read-only desk API**, **React + Vite + TypeScript Live Desk SPA** (10 Vitest checks), **13-job Docker `worker` scheduler** (with cooldown-gated Telegram failure/recovery alerts), and automated **GHCR + Dokploy CI/CD** behind a protected PR-only `main` branch (`tests` + `frontend` required checks). Every number below comes from live Hyperliquid Mainnet feeds, Hyperliquid Testnet executions, HyperEVM Chain 999 RPC (`eth_call`), Pendle Boros Open API, and reproducible OOS studies.

| Module | Scope | Verified Result | Report / Proof |
|---|---|---|---|
| **6-Family Metamodel Audit** | 58 hypotheses + 60m live `bbo` ALO audit (19 alts) | Ideal `1.5 bps`: `+32.18% APR / Sh 3.11`; at **measured `5.0 bps` BBO cost** (`55%` fill, negative 30s markout): collapses to 1 carry layer, **Ex-`INIT` −0.37% / Sh −0.19** → honestly kept **observation-only** (`5.0 bps` paper) | [BBO ALO Audit](docs/reports/2026-11-26/hl_alo_fill_portfolio_coins.md) · [Real-Cost Metamodel](docs/reports/2026-11-26/hl_metamodel_real_costs.md) · [Slow Rev Test](docs/reports/2026-11-26/hl_xs_rev_slow_variants.md) |
| **Testnet `ALO` Executor & Attribution** | 4h `Alo` rebalancer + 09:00 UTC daily audit | **95.7% real `ALO` Fill Rate (`22/23`)**, VW Markout **`+2.33 bps`**, exact `Unified Account` sync, `onlyIsolated` guard, **isolated/cross `ADL Rank`** & **size-scaled / Growth `0.1×` API quota health** | [Testnet Execution Wiki](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki/Hyperliquid-Testnet-Execution-and-Attribution) |
| **Avellaneda–Stoikov MM v2** | Mainnet WS $\kappa$-fit + live `BTC+ETH` Testnet | `ETH κ=0.544`, `BTC κ=0.327`; **100% Maker, +$0.0165 net PnL, ETH +4.51 bps 5s markout**; **`p50 = 5.62 µs`**; explicit canonical `cancel` + pure `Alo` batch guard, `tid` fill dedup, `Standard` mempool check, **rolling VPIN** & **adaptive `1 req/$` (`0.1×` Growth) throttle** | [Calibration Report](docs/reports/2026-11-26/hl_mm_ws_calibration.md) |
| **5-DEX Scanner v2 + Cross-DEX Paper v2** | 810 perps + 4h **HL × Lighter × Extended** cron | Persistence Gate (`>=2` ticks), tight L2 cap (`<=5 bps`), venue-outage guard, **H1 Basis+Carry Combo** live on Extended (`NEAR +18.8% APR`, `+13.6 bps` exec basis, **2h payback**) | [5-DEX Scan](docs/reports/2026-11-26/hl_cross_dex_v2_scan.md) · [Paper v2 Log](docs/reports/2026-11-26/hl_cross_dex_paper_live.md) |
| **HIP-3 Builder-Perps (`xyz`/`io`/`para`)** | **10 builders, 178 markets, `$2.68B/24h`** + **Split-Margin Paper** & **Oracle Div Guard** | Zero-bridge same-chain arb in **Growth Mode (`0.90 bps` taker / `0.30 bps` maker, independent DEX margin)**: live **4h Split-Margin Paper Ledger** (`MU` `83.9% APR` isolated half-size `3.1h` payback, `NVDA` `41.3% APR` Cross `0.0 bps` oracle div `7.2h` payback), **`SKHX` (`⚠️ 30–46 bps` oracle div blocked!)**, gap- & flip-aware persistence | [HIP-3 Live](docs/reports/2026-11-26/hl_hip3_builder_arb_live.md) · [Frontier Persistence](docs/reports/2026-11-26/hl_frontier_persistence_live.md) |
| **Pendle Boros & HyperEVM Yield Stack** | **39 active Boros** (`17` HL/Spread) + `kHYPE/Felix/HyperLend` vs `HLP` | Boros locks **`HYPE +8.94%` / `BTC +6.69%`** (28d) & `HL-Gate ETH` spread `+6.36%` disloc; composable `kHYPE+Boros` loop nets **`+9.07%`** vs passive **`HLP +15.68%`** | [Boros Live](docs/reports/2026-11-26/hl_boros_fixed_funding_live.md) · [Yield Stack](docs/reports/2026-11-26/hl_hyperevm_yield_stack_live.md) |
| **HyperEVM (999) + HLP + H2/H4/H5/H6 Events** | `L1Read`, V3 depth, **leveraged whale H2**, **deposit-clean HLP PnL**, **3-regime H6** (15m cron) | `L1Read`: **0.00 bps** BTC/ETH drift; `USDT0` `+11.08 bps` peg adj; live **`ETH $52.0M`** liq wall (`4.5%`); **`INIT -110.9 bps` (`-840%` APR)** cleanly classified as `CROWDED_FUNDING_BASIS`; deposit-clean HLP `allTime.pnlHistory` + **4h alert cooldown** + **6h whale cache** | [HyperEVM Scan](docs/reports/2026-11-26/hl_hyperevm_defi_scan.md) · [H2–H6 Live](docs/reports/2026-11-26/hl_onchain_events_h2_h4_h5_live.md) |
| **52d `:00` Hourly Funding OOS Study** | **51 coins / 60,370** `:00` events (`52d 15m` + `17d 5m`) | `APR<=-25%`: fast `0–5m` unwind (`+3.84/+2.27 bps`) wired into MM/Executor; `APR>=+50%`: `naive_harvest_15m` yields **`+21.48 bps` Net Maker** Holdout (`t=+2.3`, `ex-XMR +33.6 bps`) | [17d `5m` Study](docs/reports/2026-11-26/hl_hourly_funding_settlement_study.md) · [52d `15m` Study](docs/reports/2026-11-26/hl_hourly_funding_settlement_52d_study.md) |

---

## Core HyperCore Microstructure Safeguards

- **Mempool Priority Discipline:** Explicit canonical L1 `cancel` + pure `Alo`-only batches (`ValueError` on mixed `GTC`/`IOC`) + `tid`-keyed fill deduplication + startup `userAbstraction` audit (`Standard` mode required for HyperCore mempool prioritization; `Unified Account` / `Portfolio Margin` explicitly flagged).
- **Rate-Limit & Growth Mode Accounting:** Adaptive requote governor and post-trade quota audit scale both by HyperCore's `1 req / 1 USDC` budget (with **`-90%` / `0.1×` volume weight in HIP-3 Growth Mode → `1 req / 10 USDC`**) and by average order size (`1%` fill floor at `$100` vs `0.05%` at `$2,000`).
- **Execution-First Honesty:** Rejected a `+32% APR` paper alpha before committing live capital after a 60-minute live `bbo` recording proved real altcoin passive cost is `5.0 bps` (`55%` fill rate), then focused engineering on verified structural edges (HIP-3, Boros, Cross-DEX L2, `:00` overlays, MM).
- **True On-Chain Risk Hygiene:** Deposit-clean HLP PnL (`vaultDetails.portfolio.allTime.pnlHistory`), exact `isolated` (`marginUsed`) vs `cross` (`accountValue`) `ADL Rank` denominators, independent HIP-3 DEX margin accounting, `USDT0/USDC` peg adjustment, and fail-closed testnet-only execution guardrails.

---

## System Architecture

```mermaid
flowchart LR
    subgraph Feeds["Live Market & On-Chain Feeds"]
        HL["HyperCore L1<br/>WS bbo + l2Book + trades + REST"]
        HIP3["HIP-3 Builders<br/>xyz · io · para (Growth Mode)"]
        DEX5["Lighter · Extended<br/>dYdX v4 · Paradex L2"]
        EVM["HyperEVM Chain 999<br/>L1Read · V3 Pools · Boros · HLP"]
    end

    subgraph Worker["Docker Worker (hl_quant.scheduler)"]
        MM["Avellaneda-Stoikov MM v2<br/>κ-Fit · Pure ALO · VPIN · DMS"]
        EXEC["Testnet ALO Executor<br/>onlyIsolated · :55 Defer · Scoped Cancel"]
        CDEX["5-DEX Scanner &<br/>HL×Lighter×Extended Paper v2"]
        FRONT["HIP-3 + Pendle Boros +<br/>HyperEVM Yield Persistence"]
        EVENT["15m On-Chain Event Alpha<br/>H2 Whales · H4 EVM · H5 HLP · H6 Stress"]
    end

    subgraph Web["Dokploy Stack (GHCR Immutable SHA)"]
        VOL[("Shared Volume<br/>/app/data (Atomic Parquet/JSON)")]
        API["FastAPI Backend (:8000)<br/>Read-Only /api/v1/* · /healthz"]
        SPA["Nginx + React/Vite/TS SPA (:80)<br/>6-Tab Live Desk · /api/ Proxy"]
    end

    HL --> MM --> VOL
    HL --> EXEC --> VOL
    DEX5 --> CDEX --> VOL
    HIP3 --> FRONT --> VOL
    EVM --> FRONT
    EVM --> EVENT --> VOL
    HL --> EVENT
    VOL --> API --> SPA
```

---

## Quick Start

Requires **Python >= 3.11.5** (and **Node >= 22** or **Docker** for the live web desk).

```bash
# 1. Install Python environment, dev tools & optional Testnet SDK
uv sync --all-extras

# 2. Run lint and the full 186+ unit test suite
make lint
make test

# 3. Preview full stack (Nginx + React SPA + FastAPI) on http://localhost:8080
make docker-up

# 4. Safe 100% read-only live scans (no API keys required)
make scan-hip3
make scan-boros
make scan-evm
make bench-mm
```

For production Dokploy setup with GHCR immutable `:sha-<commit>` images and post-deploy `/build-info.json` verification, see **[docs/DEPLOYMENT_DOKPLOY.md](docs/DEPLOYMENT_DOKPLOY.md)** and **[Docker & Dokploy CI/CD Wiki](https://github.com/KonstantinRogozhkin/hyperliquid-quant-defi/wiki/Docker-Dokploy-Fullstack-CICD)**.

---

## Automated Operations & Safety Guardrails

All live execution code is hard-pinned to **`https://api.hyperliquid-testnet.xyz`** (zero Mainnet private keys or withdrawal permissions). Copy `.env.example` to `.env` for optional Testnet/Telegram keys.

| Cadence | Wrapper Script | Role |
|---|---|---|
| Hourly `:50` | `scripts/ops/run_hyperliquid_snapshot_tick.sh` | Live `predictedFundings` & market snapshots |
| Every 4h `:05` | `scripts/ops/run_hl_metamodel_paper_tick.sh` | Observation-only shadow paper tracker at measured `5.0 bps` HL cost |
| Every 4h `:10` | `scripts/ops/run_hl_testnet_executor_tick.sh` | Live Testnet `ALO` rebalancer (`cross:3x`, `onlyIsolated` guard, kill-switch) |
| Every 4h `:15` | `scripts/ops/run_hl_cross_dex_paper_tick.sh` | Cross-DEX Paper v2 (**HL × Lighter × Extended**, Persistence + Slip `<=5 bps` + H1 Combo) |
| Every 4h `:20` | `scripts/ops/run_hl_frontier_persistence_tick.sh` | Frontier Persistence Tracker (**HIP-3** Growth/Oracle + **Pendle Boros** + **HyperEVM Yield Stack**) |
| Every 15m (`*/15`) | `scripts/ops/run_hl_onchain_event_tick.sh` | On-Chain Event Alpha (**H2** Leveraged Whales + **H4** EVM + **H5** Deposit-Clean HLP + **H6** 3-Regime Stress + **4h Cooldown**) |
| Daily `09:00 UTC` | `scripts/ops/run_hl_testnet_attribution_daily.sh` | Post-trade PnL, Maker %, `ALO` Fill Rate, `ADL Rank`, API quota + quiet HTML dashboard refresh |

Install or migrate all 7 local crons cleanly in one command:
```bash
zsh scripts/ops/install_all_hl_crons.sh
```

---

## Repository Layout

- `src/hl_quant/` — 31 reusable modules (research, MM, execution, 5-DEX, HIP-3 + Split-Margin Paper, Boros, HyperEVM, attribution, FastAPI `api/`, and 13-job container `scheduler.py`).
- `frontend/` — React + Vite + TypeScript 6-tab live quant desk SPA (with Vitest suite).
- `docker/` & `docker-compose*.yml` — multi-stage `backend`/`worker` (Python + `uv`) and `frontend` (Node + Nginx) containers for Dokploy & local preview.
- `scripts/analytics/` & `scripts/ops/` — thin CLI entrypoints, lock-aware cron wrappers, Dokploy cron cutover tool, and SHA release script (`deploy_dokploy.py`).
- `tests/` — 30 test modules (**186+ unit tests**), enforced on GitHub Actions (`tests` + `frontend` required PR checks).
- `docs/` — deployment runbook (`DEPLOYMENT_DOKPLOY.md`), 10-page Wiki mirror (`docs/wiki/`), and dated research reports (`docs/reports/`).
