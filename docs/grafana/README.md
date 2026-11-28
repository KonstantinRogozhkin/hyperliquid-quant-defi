# Grafana, Prometheus & Critical Alerting Integration

`hl-backend` exposes a zero-dependency Prometheus text exposition endpoint at:
- Public (via Dokploy Nginx): `https://<your-domain>/api/v1/metrics`
- Internal Docker network: `http://backend:8000/metrics`

## 1. Prometheus / VictoriaMetrics Scrape Config

```yaml
scrape_configs:
  - job_name: "hyperliquid-quant-defi"
    scrape_interval: 60s
    scrape_timeout: 15s
    metrics_path: /api/v1/metrics
    scheme: https
    static_configs:
      - targets:
          - hl-quant.example.com
```

## 2. Dashboard v2 (`hyperliquid_quant_defi_dashboard.json`)

Regenerate cleanly anytime via:
```bash
.venv/bin/python scripts/ops/build_grafana_dashboard.py
```

The v2 dashboard (`uid: hl-quant-defi-overview`, 8 rows) includes:
1. **Executive Desk & Worker Health** (`hl_worker_up`, heartbeat age, healthy jobs ratio across 16 UTC jobs, failed jobs, Testnet kill-switch, active Bounded NAV Guard status, total open positions, HLP TVL).
2. **All Strategies — Live Leaderboard Table & NAV Comparison** (`hl_strategy_*` unified across `neutral_autopilot`, `spot_perp_hlp`, `cross_dex_v2`, `hip3_split_margin`, `metamodel_5bps`, `testnet_alo_live`, `hlp_benchmark` — Equity, Gross/Net HWM Return %, Net PnL, Funding/Yield, Costs, Live APR %, Positions, Leverage, Max DD %, Share Price + trailing 1Y / 370D crypto/gold benchmarks).
3. **Neutral Autopilot & Crisis-Alpha CTA (≤5% Cap)** — sleeve target vs actual vs deadband drift table + multi-horizon TSMOM signals/weights.
4. **HyperCore Spot+Perp (+10% HLP), HIP-3 & Cross-DEX Carry** — blended APR, 10% HLP reserve, net delta %, min perp liq distance, open Spot+Perp table, HIP-3 oracle divergence guardrails.
5. **Live Testnet ALO Execution & Per-Coin Risk** — Equity/HWM, instant stat KPIs (Net PnL, ALO Fill %, Maker %, VW Markout, Min Liq Distance, API req/\$ vol), and per-coin ADL/Liquidation table.
6. **Frontier Yield (Pendle Boros, HyperEVM Chain 999 & HLP)** — HLP vs Composable Loop APR, Boros live fixed-bid APR table, `USDT0` peg discount, `L1Read` max drift.
7. **On-Chain Alpha (H2/H4/H5/H6) & Avellaneda-Stoikov MM** — whale liquidation walls, empirical $\kappa$ & $R^2$, `p50/p99` tick-to-quote latency.
8. **16 UTC Worker Jobs Table & Artifact Freshness**.

## 3. Critical & Warning Alert Rules (20 Rules — Exact Parity)

Two equivalent alert rule formats are provided (11 critical + 9 warning = 20 rules each):
- **Prometheus native rules:** `docs/grafana/prometheus_alerts.yml` (include in `/etc/prometheus/alerts.yml` on `Prometheus 85`).
- **Grafana Unified Alerting provisioning:** `docs/grafana/grafana_alert_rules.yml` (or configured directly in Grafana Alerting UI under folder **Hyperliquid Quant DeFi**).

Critical alerts covered (11):
- `HLWorkerDown`, `HLWorkerJobFailed`
- `HLTestnetKillSwitchTriggered`, `HLTestnetLiquidationRisk`, `HLTestnetADLRisk`, `HLTestnetDrawdownNearKillSwitch`
- `HLBoundedNavGuardTriggered` (`hl_desk_nav_guard_active > 0`)
- `HLHIP3OracleDivergenceCritical` (`> 30 bps`)
- `HLSpotPerpDeltaOrMarginCritical` (`|Δ| > 2%` or perp liq distance `< 15%`)
- `HLAutopilotRiskLimitBreach` (CTA sleeve `> 6%` or drift `> 6%`)
- `HLStrategyDrawdownOrApiQuotaCritical` (`Max DD > 5%` or API `> 0.85 req/$`)

Warning alerts covered (9):
- `HLArtifactStale`, `HLSettlementStudyDegraded`, `HLTestnetALOFillRateLow`
- `HLTestnetDrawdownWarning`, `HLTestnetOrphanPositions`, `HLTestnetMarkOracleDislocation`, `HLTestnetCoinMarkOracleDislocation`
- `HLTestnetRiskProbeStale`, `HLTestnetAttributionStale`

> **Note on Telegram delivery in Grafana:** Ensure either Grafana's built-in **Contact points** has a Telegram bot token + `chat_id`, or fix `telegram_configs[].chat_id` on external `Alertmanager Telegram` (`http://host.docker.internal:19093`).
