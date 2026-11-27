"""Zero-dependency Prometheus text exposition exporter for ``hl-backend``.

Converts the live atomic telemetry artifacts under ``/app/data/paper`` (plus unified
``hl_analytics_risk`` leaderboard / risk outputs) into valid Prometheus
``text/plain; version=0.0.4`` gauges and counters so Prometheus or VictoriaMetrics can
scrape ``GET /api/v1/metrics`` (or internal ``GET /metrics``) and power institutional
Grafana dashboards and critical alerting rules.
"""

from __future__ import annotations

from datetime import datetime, timezone
import math
from typing import Any

PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


def _escape_label(value: object) -> str:
    s = str(value)
    return s.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_labels(labels: dict[str, object] | None) -> str:
    if not labels:
        return ""
    inner = ",".join(f'{k}="{_escape_label(v)}"' for k, v in labels.items())
    return "{" + inner + "}"


def _fmt_value(value: float) -> str:
    if value == 0.0:
        return "0"
    if float(int(value)) == value and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _parse_utc_timestamp(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


class _PromBuilder:
    def __init__(self) -> None:
        self._declared: set[str] = set()
        self._lines: list[str] = []

    def emit(
        self,
        name: str,
        value: Any,
        *,
        metric_type: str = "gauge",
        help_text: str = "",
        labels: dict[str, object] | None = None,
    ) -> None:
        num = float(value) if isinstance(value, bool) else _num(value)
        if num is None:
            return
        if name not in self._declared:
            self._declared.add(name)
            if help_text:
                self._lines.append(f"# HELP {name} {help_text}")
            self._lines.append(f"# TYPE {name} {metric_type}")
        self._lines.append(f"{name}{_fmt_labels(labels)} {_fmt_value(num)}")

    def render(self) -> str:
        return ("\n".join(self._lines) + "\n") if self._lines else ""


def _emit_strategy_row(
    b: _PromBuilder,
    *,
    strategy: str,
    category: str,
    mode: str,
    status_ok: bool,
    equity_usd: Any,
    starting_capital_usd: Any,
    return_pct: Any,
    net_return_pct: Any,
    net_pnl_usd: Any,
    funding_pnl_usd: Any,
    costs_usd: Any,
    live_apr_pct: Any,
    open_positions: Any,
    leverage: Any,
    max_dd_pct: Any,
    share_price: Any,
    sharpe_90d: Any = None,
    beta_btc: Any = None,
) -> None:
    """Emit standardized unified strategy-leaderboard gauges sharing identical labels."""
    lbl = {"strategy": strategy, "category": category, "mode": mode}
    b.emit("hl_strategy_status_ok", status_ok, help_text="1 if strategy is healthy, 0 on kill-switch/guard", labels=lbl)
    b.emit("hl_strategy_equity_usd", equity_usd, help_text="Current strategy equity USD", labels=lbl)
    b.emit("hl_strategy_starting_capital_usd", starting_capital_usd, help_text="Initial strategy capital USD", labels=lbl)
    b.emit("hl_strategy_return_pct", return_pct, help_text="Cumulative gross return %", labels=lbl)
    b.emit("hl_strategy_net_return_pct", net_return_pct, help_text="Cumulative net HWM return %", labels=lbl)
    b.emit("hl_strategy_net_pnl_usd", net_pnl_usd, help_text="Cumulative net PnL USD", labels=lbl)
    b.emit("hl_strategy_funding_pnl_usd", funding_pnl_usd, help_text="Cumulative funding / yield PnL USD", labels=lbl)
    b.emit("hl_strategy_costs_usd", costs_usd, help_text="Cumulative trading fees and slippage USD", labels=lbl)
    b.emit("hl_strategy_live_apr_pct", live_apr_pct, help_text="Live weighted portfolio carry/yield APR %", labels=lbl)
    b.emit("hl_strategy_open_positions", open_positions, help_text="Currently open positions or pairs", labels=lbl)
    b.emit("hl_strategy_leverage", leverage, help_text="Current gross leverage multiple", labels=lbl)
    b.emit("hl_strategy_max_dd_pct", max_dd_pct, help_text="Maximum historical drawdown %", labels=lbl)
    b.emit("hl_strategy_share_price", share_price, help_text="Normalized NAV share price ($1.00 inception)", labels=lbl)
    if sharpe_90d is not None:
        b.emit("hl_strategy_sharpe_90d", sharpe_90d, help_text="Trailing 90D Sharpe ratio (>=5 calendar days)", labels=lbl)
    if beta_btc is not None:
        b.emit("hl_strategy_beta_btc", beta_btc, help_text="OLS beta against BTC daily log returns", labels=lbl)


def render_prometheus_metrics(
    overview: dict[str, Any],
    *,
    analytics: dict[str, Any] | None = None,
    now_utc: datetime | None = None,
) -> str:
    """Render full ``/api/v1/overview`` (+ optional ``/api/v1/analytics``) as Prometheus exposition text."""
    now = now_utc or datetime.now(tz=timezone.utc)
    b = _PromBuilder()

    build = overview.get("build_info") or {}
    if isinstance(build, dict):
        b.emit(
            "hl_build_info",
            1.0,
            help_text="Build metadata for hl-backend",
            labels={
                "service": build.get("service", "hl-backend"),
                "sha": str(build.get("sha", "dev"))[:12],
            },
        )

    # 1. Worker scheduler health & jobs
    ws = overview.get("worker_status")
    if isinstance(ws, dict):
        hb_dt = _parse_utc_timestamp(ws.get("heartbeat_utc"))
        if hb_dt is not None:
            hb_age = max(0.0, (now - hb_dt).total_seconds())
            b.emit("hl_worker_heartbeat_age_seconds", hb_age, help_text="Seconds since last hl-worker heartbeat")
            b.emit("hl_worker_up", 1.0 if hb_age <= 300.0 else 0.0, help_text="1 if hl-worker heartbeat <= 300s")
        b.emit("hl_worker_telegram_enabled", bool(ws.get("telegram_enabled")), help_text="1 if worker Telegram alerts enabled")
        b.emit(
            "hl_worker_testnet_execution_enabled",
            bool(ws.get("testnet_execution_enabled")),
            help_text="1 if live Testnet ALO execution enabled in worker",
        )
        jobs = [j for j in (ws.get("jobs") or []) if isinstance(j, dict) and j.get("name")]
        enabled_jobs = [j for j in jobs if bool(j.get("enabled"))]
        ok_jobs = [j for j in enabled_jobs if str(j.get("state") or "") == "ok"]
        b.emit("hl_worker_jobs_total", len(jobs), help_text="Total scheduled worker jobs")
        if enabled_jobs:
            b.emit(
                "hl_worker_jobs_healthy_ratio",
                round(len(ok_jobs) / len(enabled_jobs), 4),
                help_text="Fraction of enabled worker jobs in ok state",
            )
        for job in jobs:
            jname = str(job["name"])
            state = str(job.get("state") or "idle")
            # Emit both `worker_job` (immune to Prometheus scrape `job` label collision) and `job` (backwards-compat)
            lbl = {"job": jname, "worker_job": jname}
            b.emit("hl_worker_job_enabled", bool(job.get("enabled")), help_text="1 if job is enabled", labels=lbl)
            b.emit("hl_worker_job_ok", state == "ok", help_text="1 if job last state is ok", labels=lbl)
            b.emit("hl_worker_job_failed", state == "failed", help_text="1 if job last state is failed", labels=lbl)
            b.emit(
                "hl_worker_job_runs_ok_total",
                job.get("runs_ok"),
                metric_type="counter",
                help_text="Cumulative successful runs per job",
                labels=lbl,
            )
            b.emit(
                "hl_worker_job_runs_failed_total",
                job.get("runs_failed"),
                metric_type="counter",
                help_text="Cumulative failed runs per job",
                labels=lbl,
            )
            b.emit(
                "hl_worker_job_last_duration_seconds",
                job.get("last_duration_s"),
                help_text="Last run duration in seconds",
                labels=lbl,
            )

    # 2. Artifact freshness
    freshness = overview.get("artifact_freshness")
    if isinstance(freshness, dict):
        for key, info in freshness.items():
            if not isinstance(info, dict):
                continue
            lbl = {"artifact": key}
            b.emit("hl_artifact_exists", bool(info.get("exists")), help_text="1 if telemetry artifact exists", labels=lbl)
            b.emit("hl_artifact_age_minutes", info.get("age_minutes"), help_text="Artifact age in minutes", labels=lbl)

    # 3. Testnet ALO execution & post-trade attribution (+ per-coin risk)
    attr = overview.get("attribution")
    testnet_last_ok = True
    if isinstance(attr, dict):
        b.emit("hl_testnet_equity_usd", attr.get("equity_usd"), help_text="Live Testnet Unified Account equity USD")
        b.emit("hl_testnet_perps_equity_usd", attr.get("perps_equity_usd"), help_text="Testnet Perps sub-account value USD")
        b.emit("hl_testnet_net_pnl_usd", attr.get("net_attributed_pnl_usd"), help_text="Net attributed Testnet PnL USD")
        b.emit("hl_testnet_realized_pnl_usd", attr.get("realized_price_pnl_usd"), help_text="Realized price PnL USD")
        b.emit("hl_testnet_unrealized_pnl_usd", attr.get("unrealized_price_pnl_usd"), help_text="Unrealized price PnL USD")
        b.emit("hl_testnet_funding_pnl_usd", attr.get("funding_pnl_usd"), help_text="Cumulative Testnet funding PnL USD")
        b.emit("hl_testnet_fees_usd", attr.get("fees_usd"), help_text="Cumulative Testnet fees USD")
        b.emit("hl_testnet_open_positions", attr.get("open_positions_count"), help_text="Open Testnet perp positions")
        b.emit("hl_testnet_total_fills", attr.get("total_fills"), metric_type="counter", help_text="Total Testnet fills")
        b.emit("hl_testnet_volume_usd", attr.get("total_volume_usd"), metric_type="counter", help_text="Total Testnet fill volume USD")
        b.emit("hl_testnet_maker_share_pct", attr.get("maker_volume_share_pct"), help_text="Maker volume share percentage")
        b.emit("hl_testnet_alo_fill_rate_pct", attr.get("real_alo_fill_rate_pct"), help_text="Real ALO post-only fill rate percentage")
        b.emit("hl_testnet_effective_fee_bps", attr.get("effective_fee_bps"), help_text="Effective fee in bps")
        b.emit("hl_testnet_vw_markout_bps", attr.get("vw_markout_bps"), help_text="Volume-weighted markout in bps")
        b.emit("hl_testnet_min_liq_distance_pct", attr.get("min_liq_distance_pct"), help_text="Minimum liquidation distance percentage")
        b.emit("hl_testnet_api_reqs_per_usd_vol", attr.get("api_reqs_per_usd_vol"), help_text="API requests per USD volume")
        b.emit("hl_testnet_liq_warnings", len(attr.get("liq_warnings") or []), help_text="Active liquidation warnings")
        b.emit("hl_testnet_adl_warnings", len(attr.get("adl_warnings") or []), help_text="Active ADL rank warnings")

        for crow in attr.get("per_coin") or []:
            if not isinstance(crow, dict) or not crow.get("coin"):
                continue
            clbl = {"coin": str(crow["coin"]), "leverage_mode": str(crow.get("leverage_mode") or "cross")}
            b.emit("hl_testnet_position_usd", crow.get("position_usd"), help_text="Testnet open position notional USD", labels=clbl)
            b.emit("hl_testnet_position_net_pnl_usd", crow.get("net_pnl_usd"), help_text="Testnet per-coin net PnL USD", labels=clbl)
            b.emit("hl_testnet_position_liq_distance_pct", crow.get("liq_distance_pct"), help_text="Testnet per-coin liquidation distance %", labels=clbl)
            b.emit("hl_testnet_position_adl_rank", crow.get("adl_rank_score"), help_text="Testnet per-coin ADL rank score", labels=clbl)
            b.emit("hl_testnet_position_maker_share_pct", crow.get("maker_share_pct"), help_text="Testnet per-coin maker fill share %", labels=clbl)

    audit_recent = overview.get("testnet_audit_recent")
    if isinstance(audit_recent, list) and audit_recent:
        latest_audit = audit_recent[0]
        if isinstance(latest_audit, dict):
            testnet_last_ok = str(latest_audit.get("status")) == "ok"
            b.emit("hl_testnet_hwm_usd", latest_audit.get("high_water_mark_usd"), help_text="Testnet equity high-water mark USD")
            b.emit(
                "hl_testnet_last_cycle_ok",
                testnet_last_ok,
                help_text="1 if latest Testnet rebalance cycle succeeded",
            )
            b.emit("hl_testnet_last_cycle_placed", latest_audit.get("placed_count"), help_text="ALO orders placed in latest cycle")
            b.emit(
                "hl_testnet_last_cycle_skipped_unquoted",
                len(latest_audit.get("skipped_unquoted_coins") or []),
                help_text="Unquoted Testnet coins skipped in latest cycle",
            )

    # 4. HIP-3 Split-Margin Paper Ledger & Persistence
    hp = overview.get("hip3_paper")
    if isinstance(hp, dict):
        b.emit("hl_hip3_paper_equity_usd", hp.get("equity_usd"), help_text="HIP-3 split-margin paper equity USD")
        b.emit("hl_hip3_paper_return_pct", hp.get("return_pct"), help_text="HIP-3 paper cumulative return percentage")
        b.emit("hl_hip3_paper_margin_locked_usd", hp.get("margin_locked_usd"), help_text="HIP-3 independent split-margin locked USD")
        b.emit(
            "hl_hip3_paper_margin_utilization_pct",
            hp.get("margin_utilization_pct"),
            help_text="HIP-3 split-margin utilization percentage",
        )
        b.emit("hl_hip3_paper_funding_usd", hp.get("cum_funding_usd"), help_text="HIP-3 cumulative paper funding PnL USD")
        b.emit("hl_hip3_paper_basis_mtm_usd", hp.get("cum_basis_mtm_usd"), help_text="HIP-3 cumulative paper basis MTM USD")
        b.emit("hl_hip3_paper_costs_usd", hp.get("cum_costs_usd"), help_text="HIP-3 cumulative paper fees & slippage USD")
        positions = hp.get("positions") or {}
        if isinstance(positions, dict):
            b.emit("hl_hip3_paper_open_pairs", len(positions), help_text="Active HIP-3 paper pairs")
            for sym, pos in positions.items():
                if not isinstance(pos, dict):
                    continue
                plbl = {
                    "symbol": sym,
                    "short_dex": pos.get("short_dex", ""),
                    "long_dex": pos.get("long_dex", ""),
                    "margin_mode": pos.get("margin_mode", "cross"),
                }
                b.emit("hl_hip3_paper_pair_live_apr_pct", pos.get("live_spread_apr_pct"), help_text="Live HIP-3 pair spread APR %", labels=plbl)
                b.emit("hl_hip3_paper_pair_oracle_div_bps", pos.get("live_oracle_div_bps"), help_text="Live HIP-3 pair oracle divergence bps", labels=plbl)
                b.emit("hl_hip3_paper_pair_net_pnl_usd", pos.get("net_pnl_usd"), help_text="Live HIP-3 pair net PnL USD", labels=plbl)
                b.emit("hl_hip3_paper_pair_notional_usd", pos.get("leg_notional_usd"), help_text="Live HIP-3 pair per-leg notional USD", labels=plbl)
        b.emit(
            "hl_hip3_paper_closed_pairs_total",
            hp.get("closed_pairs_count"),
            metric_type="counter",
            help_text="Closed HIP-3 paper pairs",
        )
        b.emit(
            "hl_hip3_paper_payback_achieved_total",
            hp.get("payback_achieved_count"),
            metric_type="counter",
            help_text="Closed HIP-3 pairs that achieved entry fee payback",
        )

    hip3_snap = overview.get("hip3")
    if isinstance(hip3_snap, dict):
        b.emit("hl_hip3_day_volume_usd", hip3_snap.get("total_hip3_day_vol_usd"), help_text="Total 24h volume across HIP-3 builders USD")
        b.emit("hl_hip3_active_markets", hip3_snap.get("active_markets_count"), help_text="Active HIP-3 builder markets")

    fh = overview.get("frontier_history")
    hlp_apr_val: float | None = None
    if isinstance(fh, dict):
        hlp_apr_val = _num(fh.get("hlp_apr_pct"))
        b.emit("hl_hlp_apr_pct", hlp_apr_val, help_text="HLP Vault benchmark APR %")
        best_ys = fh.get("best_yield_strategy")
        if isinstance(best_ys, dict):
            b.emit("hl_yield_stack_best_net_apr_pct", best_ys.get("net_apr_pct"), help_text="Best composable HyperEVM yield stack net APR %")
        for row in (fh.get("hip3_persistence") or [])[:10]:
            if not isinstance(row, dict) or not row.get("base_symbol"):
                continue
            rlbl = {"symbol": row["base_symbol"]}
            b.emit("hl_hip3_persistence_clean_streak", row.get("clean_streak"), help_text="Consecutive clean HIP-3 ticks", labels=rlbl)
            b.emit("hl_hip3_persistence_latest_apr_pct", row.get("latest_apr_pct"), help_text="Latest HIP-3 cross-builder APR %", labels=rlbl)
            b.emit(
                "hl_hip3_persistence_oracle_div_bps",
                row.get("median_oracle_div_bps"),
                help_text="Median HIP-3 builder oracle divergence bps",
                labels=rlbl,
            )

    # 5. Cross-DEX Paper v2 & 5-DEX Scanner
    cdp = overview.get("cross_dex_paper")
    if isinstance(cdp, dict):
        b.emit("hl_cross_dex_paper_equity_usd", cdp.get("equity_usd"), help_text="Cross-DEX Paper v2 equity USD")
        b.emit("hl_cross_dex_paper_funding_usd", cdp.get("cum_funding_usd"), help_text="Cross-DEX cumulative funding PnL USD")
        b.emit("hl_cross_dex_paper_basis_mtm_usd", cdp.get("cum_basis_mtm_usd"), help_text="Cross-DEX cumulative basis MTM USD")
        b.emit("hl_cross_dex_paper_costs_usd", cdp.get("cum_costs_usd"), help_text="Cross-DEX cumulative fees & slippage USD")
        cpos = cdp.get("positions") or {}
        if isinstance(cpos, dict):
            b.emit("hl_cross_dex_paper_open_pairs", len(cpos), help_text="Active Cross-DEX paper pairs")
            for coin, pos in cpos.items():
                if not isinstance(pos, dict):
                    continue
                clbl = {"coin": coin, "other_dex": pos.get("other_dex", "lighter")}
                b.emit("hl_cross_dex_paper_pair_live_apr_pct", pos.get("live_spread_apr_pct"), help_text="Live Cross-DEX pair spread APR %", labels=clbl)
                b.emit("hl_cross_dex_paper_pair_net_pnl_usd", pos.get("net_pnl_usd"), help_text="Live Cross-DEX pair net PnL USD", labels=clbl)
                b.emit("hl_cross_dex_paper_pair_notional_usd", pos.get("leg_notional_usd"), help_text="Live Cross-DEX pair leg notional USD", labels=clbl)
        b.emit(
            "hl_cross_dex_paper_closed_pairs_total",
            cdp.get("closed_pairs_count"),
            metric_type="counter",
            help_text="Closed Cross-DEX paper pairs",
        )

    cd_all = overview.get("cross_dex_all")
    if isinstance(cd_all, list) and cd_all:
        aprs = [_num(r.get("spread_apr_pct")) for r in cd_all if isinstance(r, dict)]
        valid_aprs = [x for x in aprs if x is not None]
        b.emit("hl_cross_dex_scan_opportunities_count", len(cd_all), help_text="Top 5-DEX scan opportunities count")
        if valid_aprs:
            b.emit("hl_cross_dex_scan_top_apr_pct", max(valid_aprs), help_text="Top 5-DEX live funding spread APR %")

    # 6. Metamodel Observation Paper (5 bps)
    paper = overview.get("paper")
    if isinstance(paper, dict):
        b.emit("hl_metamodel_paper_equity_usd", paper.get("equity_usd"), help_text="Metamodel observation paper equity USD (5 bps)")
        b.emit("hl_metamodel_paper_return_pct", paper.get("return_pct"), help_text="Metamodel observation paper return %")
        b.emit("hl_metamodel_paper_active_positions", paper.get("active_positions"), help_text="Active metamodel paper positions")
        b.emit("hl_metamodel_paper_ticks_total", paper.get("ticks"), metric_type="counter", help_text="Total metamodel paper ticks")

    # 7. On-Chain Events (H2/H4/H5/H6), HLP Vault, Boros Fixed Funding & HyperEVM Chain 999
    ev = overview.get("onchain_events")
    hlp_tvl_val: float | None = None
    if isinstance(ev, dict):
        hlp_tvl_val = _num(ev.get("hlp_tvl_usd"))
        b.emit("hl_hlp_tvl_usd", hlp_tvl_val, help_text="HLP Vault TVL USD")
        mm_pnl = ev.get("hlp_mm_pnl")
        if isinstance(mm_pnl, dict):
            b.emit("hl_hlp_cum_pnl_usd", mm_pnl.get("hlp_cum_pnl_usd"), help_text="Deposit-clean HLP cumulative all-time PnL USD")
            b.emit("hl_hlp_pnl_delta_usd", mm_pnl.get("hlp_pnl_delta_usd"), help_text="HLP PnL delta since previous 15m tick USD")
        alerts_cnt = ev.get("alerts_count")
        if isinstance(alerts_cnt, dict):
            for hyp, cnt in alerts_cnt.items():
                b.emit("hl_onchain_alerts_active", cnt, help_text="Active on-chain alerts by hypothesis", labels={"hypothesis": hyp})
        tot_logged = ev.get("total_logged")
        if isinstance(tot_logged, dict):
            for hyp, cnt in tot_logged.items():
                b.emit(
                    "hl_onchain_events_logged_total",
                    cnt,
                    metric_type="counter",
                    help_text="Total logged on-chain events by hypothesis",
                    labels={"hypothesis": hyp},
                )
        for row in ev.get("h2_events") or []:
            if not isinstance(row, dict) or not row.get("coin"):
                continue
            hlbl = {"coin": row["coin"]}
            b.emit("hl_onchain_h2_liq_0_5_usd", row.get("total_0_5_usd"), help_text="Whale liquidation wall 0-5% from mark USD", labels=hlbl)
            b.emit("hl_onchain_h2_liq_0_10_usd", row.get("total_0_10_usd"), help_text="Whale liquidation wall 0-10% from mark USD", labels=hlbl)
            b.emit("hl_onchain_h2_nearest_wall_pct", row.get("nearest_wall_pct"), help_text="Distance to nearest whale liquidation wall %", labels=hlbl)

    boros = overview.get("boros")
    if isinstance(boros, dict):
        b.emit("hl_boros_active_markets", boros.get("active_markets_count"), help_text="Active Pendle Boros fixed-funding markets")
        b.emit("hl_boros_hl_spread_markets", boros.get("hl_and_spread_count"), help_text="Active Hyperliquid & Spread Boros markets")
        for m in (boros.get("markets") or [])[:12]:
            if not isinstance(m, dict) or not m.get("symbol"):
                continue
            blbl = {"symbol": str(m["symbol"]), "underlying": str(m.get("underlying") or "")}
            b.emit("hl_boros_fixed_bid_apr_pct", m.get("best_bid_apr_pct"), help_text="Pendle Boros fixed-funding bid APR %", labels=blbl)
            b.emit("hl_boros_fixed_minus_float_pct", m.get("fixed_minus_float_bid_pct"), help_text="Pendle Boros fixed minus floating APR spread %", labels=blbl)
            b.emit("hl_boros_days_to_maturity", m.get("days_to_maturity"), help_text="Pendle Boros days to maturity", labels=blbl)

    hevm = overview.get("hyperevm")
    if isinstance(hevm, dict):
        ch = hevm.get("chain") or {}
        if isinstance(ch, dict):
            b.emit("hl_hyperevm_block_number", ch.get("block_number"), help_text="Latest HyperEVM Chain 999 block number")
            b.emit("hl_hyperevm_usdt0_peg_discount_bps", ch.get("usdt0_peg_discount_bps"), help_text="HyperEVM USDT0/USDC peg discount bps")
        l1_oracles = [x for x in (hevm.get("l1_read_oracles") or []) if isinstance(x, dict)]
        drifts = [abs(_num(x.get("drift_bps")) or 0.0) for x in l1_oracles if _num(x.get("drift_bps")) is not None]
        if drifts:
            b.emit("hl_hyperevm_l1_read_max_drift_bps", max(drifts), help_text="Max L1Read precompile vs HyperCore mark drift bps")
        evm_arbs = [x for x in (hevm.get("evm_arb") or []) if isinstance(x, dict)]
        b.emit(
            "hl_hyperevm_executable_arbs",
            sum(1 for x in evm_arbs if bool(x.get("executable"))),
            help_text="Executable peg-adjusted HyperEVM vs HyperCore arbs",
        )

    # 8. Avellaneda-Stoikov MM Calibration & Hot-Path Latency
    mm_cal = overview.get("mm_calibration")
    if isinstance(mm_cal, dict):
        for coin, cal in mm_cal.items():
            if not isinstance(cal, dict):
                continue
            mlbl = {"coin": coin}
            b.emit("hl_mm_calibration_kappa", cal.get("kappa_per_bp"), help_text="Empirical Poisson fill-intensity kappa per bp", labels=mlbl)
            b.emit("hl_mm_calibration_r2", cal.get("r2_fit"), help_text="Empirical kappa fit R^2", labels=mlbl)
            b.emit(
                "hl_mm_calibration_min_half_spread_bps",
                cal.get("implied_min_half_spread_bps"),
                help_text="Implied Avellaneda-Stoikov minimum half-spread bps",
                labels=mlbl,
            )
            b.emit("hl_mm_calibration_sigma_1s_bps", cal.get("empirical_sigma_1s_bps"), help_text="Empirical 1s volatility bps", labels=mlbl)

    lat = overview.get("latency_bench")
    if isinstance(lat, dict):
        b.emit("hl_mm_tick_to_quote_p50_us", lat.get("p50_us"), help_text="Avellaneda-Stoikov hot-path tick-to-quote p50 microseconds")
        b.emit("hl_mm_tick_to_quote_p99_us", lat.get("p99_us"), help_text="Avellaneda-Stoikov hot-path tick-to-quote p99 microseconds")
        b.emit("hl_mm_throughput_ticks_per_sec", lat.get("throughput_ticks_per_sec"), help_text="Avellaneda-Stoikov hot-path throughput ticks/sec")

    # 9. HyperCore Spot+Perp (+10% HLP) & Neutral Autopilot Master Vault (Deep Metrics)
    sp = overview.get("spot_perp_paper")
    sp_nav_guards = 0
    if isinstance(sp, dict):
        sp_nav_guards = int(sp.get("nav_guard_triggers", 0) or 0)
        b.emit("hl_spot_perp_equity_usd", sp.get("equity_usd"), help_text="HyperCore Spot+Perp paper equity USD")
        b.emit("hl_spot_perp_return_pct", sp.get("return_pct"), help_text="HyperCore Spot+Perp paper return %")
        b.emit("hl_spot_perp_blended_apr_pct", sp.get("blended_portfolio_apr_pct"), help_text="Spot+Perp + 10% HLP blended portfolio APR %")
        b.emit("hl_spot_perp_hlp_reserve_usd", sp.get("hlp_reserve_usd"), help_text="Spot+Perp HLP reserve allocation USD")
        b.emit("hl_spot_perp_hlp_reserve_share_pct", sp.get("hlp_reserve_share_pct"), help_text="Spot+Perp HLP reserve share %")
        b.emit("hl_spot_perp_free_cash_usd", sp.get("free_cash_usd"), help_text="Spot+Perp free USDC cash buffer USD")
        b.emit("hl_spot_perp_funding_usd", sp.get("cum_funding_usd"), help_text="Spot+Perp cumulative funding PnL USD")
        b.emit("hl_spot_perp_hlp_yield_usd", sp.get("cum_hlp_yield_usd"), help_text="Spot+Perp cumulative HLP reserve yield USD")
        b.emit("hl_spot_perp_basis_mtm_usd", sp.get("cum_basis_mtm_usd"), help_text="Spot+Perp cumulative basis MTM USD")
        b.emit("hl_spot_perp_costs_usd", sp.get("cum_costs_usd"), help_text="Spot+Perp cumulative fees & slippage USD")
        b.emit("hl_spot_perp_nav_guard_triggers", sp_nav_guards, metric_type="counter", help_text="Spot+Perp bounded NAV sanity guard triggers")
        sp_pos = sp.get("positions") or {}
        if isinstance(sp_pos, dict):
            b.emit("hl_spot_perp_open_positions", len(sp_pos), help_text="Open HyperCore Spot+Perp pairs")
            for sym, pos in sp_pos.items():
                if not isinstance(pos, dict):
                    continue
                slbl = {
                    "symbol": sym,
                    "spot_pair": str(pos.get("spot_pair") or ""),
                    "tier": str(pos.get("asset_tier") or ""),
                }
                b.emit("hl_spot_perp_position_notional_usd", pos.get("perp_notional_usd"), help_text="Open Spot+Perp hedged notional USD", labels=slbl)
                b.emit("hl_spot_perp_position_live_apr_pct", pos.get("live_funding_apr_pct"), help_text="Open Spot+Perp live funding APR %", labels=slbl)
                b.emit("hl_spot_perp_position_net_pnl_usd", pos.get("net_pnl_usd"), help_text="Open Spot+Perp net PnL USD", labels=slbl)
                b.emit("hl_spot_perp_position_perp_lev", pos.get("effective_perp_leverage"), help_text="Open Spot+Perp effective perp leverage", labels=slbl)
        sp_rt = sp.get("risk_telemetry")
        if isinstance(sp_rt, dict):
            b.emit("hl_spot_perp_gross_leverage", sp_rt.get("gross_leverage"), help_text="Spot+Perp current gross leverage")
            b.emit("hl_spot_perp_leverage_30d_max", sp_rt.get("leverage_30d_max"), help_text="Spot+Perp 30D max gross leverage")
            b.emit("hl_spot_perp_net_delta_pct", sp_rt.get("net_delta_pct"), help_text="Spot+Perp net delta % of equity")
            b.emit("hl_spot_perp_min_perp_liq_distance_pct", sp_rt.get("min_perp_liq_distance_pct"), help_text="Spot+Perp minimum short-perp liquidation distance %")

    sp_scan = overview.get("spot_perp_scan")
    if isinstance(sp_scan, dict):
        opps = [o for o in (sp_scan.get("opportunities") or []) if isinstance(o, dict)]
        elig = [o for o in opps if bool(o.get("eligible"))]
        b.emit("hl_spot_perp_scan_eligible_pairs", len(elig), help_text="L2-eligible HyperCore Spot+Perp carry pairs")
        if elig:
            top_apr = max(_num(o.get("funding_apr_pct")) or 0.0 for o in elig)
            b.emit("hl_spot_perp_scan_top_apr_pct", top_apr, help_text="Top L2-eligible HyperCore Spot+Perp funding APR %")

    ap_st = overview.get("autopilot")
    ap_nav_guards = 0
    if isinstance(ap_st, dict):
        ap_nav_guards = int(ap_st.get("nav_guard_triggers", 0) or 0)
        b.emit("hl_autopilot_gross_equity_usd", ap_st.get("gross_equity_usd"), help_text="Neutral Autopilot master gross equity USD")
        b.emit("hl_autopilot_net_equity_usd", ap_st.get("net_equity_usd"), help_text="Neutral Autopilot master net HWM equity USD")
        b.emit("hl_autopilot_gross_return_pct", ap_st.get("gross_return_pct"), help_text="Neutral Autopilot gross return %")
        b.emit("hl_autopilot_net_return_pct", ap_st.get("net_return_pct"), help_text="Neutral Autopilot net HWM return %")
        b.emit("hl_autopilot_gross_share_price", ap_st.get("gross_share_price"), help_text="Neutral Autopilot gross NAV share price")
        b.emit("hl_autopilot_net_share_price", ap_st.get("net_share_price"), help_text="Neutral Autopilot net HWM share price")
        b.emit("hl_autopilot_hwm_share_price", ap_st.get("hwm_net_share_price"), help_text="Neutral Autopilot high-water mark share price")
        b.emit("hl_autopilot_cum_mgmt_fee_usd", ap_st.get("cum_mgmt_fee_usd"), help_text="Neutral Autopilot cumulative management fee USD")
        b.emit("hl_autopilot_cum_perf_fee_usd", ap_st.get("cum_perf_fee_usd"), help_text="Neutral Autopilot cumulative HWM perf fee USD")
        b.emit("hl_autopilot_cum_rebalance_cost_usd", ap_st.get("cum_rebalance_cost_usd"), help_text="Neutral Autopilot cumulative rebalance cost USD")
        b.emit("hl_autopilot_rebalances_total", ap_st.get("rebalances_count"), metric_type="counter", help_text="Neutral Autopilot deadband rebalances executed")
        b.emit("hl_autopilot_nav_guard_triggers", ap_nav_guards, metric_type="counter", help_text="Neutral Autopilot bounded NAV guard triggers")
        ap_rt = ap_st.get("risk_telemetry")
        if isinstance(ap_rt, dict):
            b.emit("hl_autopilot_gross_leverage", ap_rt.get("gross_leverage"), help_text="Neutral Autopilot weighted gross leverage")
            b.emit("hl_autopilot_leverage_30d_max", ap_rt.get("leverage_30d_max"), help_text="Neutral Autopilot 30D max weighted leverage")
            b.emit("hl_autopilot_max_drift_pct", ap_rt.get("max_weight_drift_pct"), help_text="Neutral Autopilot max sleeve drift vs target %")
            b.emit("hl_autopilot_directional_exposure_pct", ap_rt.get("net_directional_exposure_pct"), help_text="Neutral Autopilot net directional exposure %")
            b.emit("hl_autopilot_directional_sleeve_pct", ap_rt.get("directional_sleeve_pct"), help_text="Neutral Autopilot Crisis-CTA sleeve weight %")
            b.emit("hl_autopilot_reserve_sleeve_pct", ap_rt.get("reserve_sleeve_pct"), help_text="Neutral Autopilot HLP/USDC reserve weight %")
        for sl in ap_st.get("sleeves") or []:
            if not isinstance(sl, dict) or not sl.get("name"):
                continue
            s_lbl = {"sleeve": str(sl["name"]), "category": str(sl.get("category") or "")}
            b.emit("hl_autopilot_sleeve_target_pct", sl.get("target_weight_pct"), help_text="Autopilot sleeve target weight %", labels=s_lbl)
            b.emit("hl_autopilot_sleeve_actual_pct", sl.get("actual_weight_pct"), help_text="Autopilot sleeve actual weight %", labels=s_lbl)
            b.emit("hl_autopilot_sleeve_drift_pct", sl.get("drift_pct"), help_text="Autopilot sleeve weight drift %", labels=s_lbl)
            b.emit("hl_autopilot_sleeve_allocation_usd", sl.get("allocation_usd"), help_text="Autopilot sleeve allocation USD", labels=s_lbl)
        cta_assets = (ap_st.get("cta_state") or {}).get("assets") or {}
        if isinstance(cta_assets, dict):
            for coin, ainfo in cta_assets.items():
                if not isinstance(ainfo, dict):
                    continue
                clbl = {"coin": str(coin)}
                b.emit("hl_autopilot_cta_signal", ainfo.get("tsmom_signal"), help_text="Crisis-CTA multi-horizon TSMOM signal [-1..+1]", labels=clbl)
                b.emit("hl_autopilot_cta_weight", ainfo.get("weight"), help_text="Crisis-CTA asset weight inside CTA sleeve", labels=clbl)
                b.emit("hl_autopilot_cta_ann_vol_pct", ainfo.get("ann_vol_pct"), help_text="Crisis-CTA 20d realized annualized volatility %", labels=clbl)

    # 10. Unified Strategy Leaderboard Table Metrics (hl_strategy_* + Benchmarks + Desk Totals)
    lb_by_id: dict[str, dict[str, Any]] = {}
    btc_beta_by_id: dict[str, float] = {}
    if isinstance(analytics, dict):
        for row in analytics.get("leaderboard") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            rid = str(row["id"])
            lb_by_id[rid] = row
            if bool(row.get("is_benchmark")):
                asset = rid.removeprefix("bench_").upper()
                blbl = {"asset": asset}
                b.emit("hl_benchmark_price_usd", row.get("spot_price_usd"), help_text="Live benchmark spot price USD", labels=blbl)
                b.emit("hl_benchmark_return_pct", (row.get("gross_returns") or {}).get("all_time"), help_text="Benchmark 90D cumulative return %", labels=blbl)
                b.emit("hl_benchmark_max_dd_pct", row.get("max_dd_gross_pct"), help_text="Benchmark 90D max drawdown %", labels=blbl)
        btc_exp = (analytics.get("market_exposure") or {}).get("BTC") or {}
        for erow in btc_exp.get("rows") or []:
            if isinstance(erow, dict) and erow.get("id") and _num(erow.get("beta")) is not None:
                btc_beta_by_id[str(erow["id"])] = float(erow["beta"])

    total_paper_eq = 0.0
    total_open_pos = 0

    def _lb_fields(lb_id: str, fallback_ret: float) -> tuple[float, float, float | None, float | None]:
        row = lb_by_id.get(lb_id) or {}
        net_r = _num((row.get("net_returns") or {}).get("all_time"))
        max_dd = _num(row.get("max_dd_gross_pct"))
        sh90 = _num(row.get("sharpe_gross"))
        beta = btc_beta_by_id.get(lb_id)
        return (
            net_r if net_r is not None else fallback_ret,
            max_dd if max_dd is not None else 0.0,
            sh90,
            beta,
        )

    if isinstance(ap_st, dict):
        eq = _num(ap_st.get("gross_equity_usd")) or 0.0
        sc = _num(ap_st.get("starting_capital_usd")) or 250_000.0
        ret = _num(ap_st.get("gross_return_pct")) or 0.0
        net_r_st = _num(ap_st.get("net_return_pct"))
        net_r, max_dd, sh90, beta = _lb_fields("autopilot", ret)
        ap_rt = ap_st.get("risk_telemetry") or {}
        active_sleeves = sum(1 for s in (ap_st.get("sleeves") or []) if isinstance(s, dict) and (_num(s.get("allocation_usd")) or 0.0) > 1.0)
        _emit_strategy_row(
            b,
            strategy="neutral_autopilot",
            category="Master Vault",
            mode="paper",
            status_ok=not bool(ap_rt.get("nav_guard_triggered")),
            equity_usd=eq,
            starting_capital_usd=sc,
            return_pct=ret,
            net_return_pct=net_r_st if net_r_st is not None else net_r,
            net_pnl_usd=round(eq - sc, 2),
            funding_pnl_usd=0.0,
            costs_usd=_num(ap_st.get("cum_rebalance_cost_usd")) or 0.0,
            live_apr_pct=_num(sp.get("blended_portfolio_apr_pct")) if isinstance(sp, dict) else 0.0,
            open_positions=active_sleeves,
            leverage=_num(ap_rt.get("gross_leverage")) or 0.0,
            max_dd_pct=max_dd,
            share_price=_num(ap_st.get("gross_share_price")) or (eq / sc if sc > 0 else 1.0),
            sharpe_90d=sh90,
            beta_btc=beta,
        )

    if isinstance(sp, dict):
        eq = _num(sp.get("equity_usd")) or 0.0
        sc = _num(sp.get("starting_capital_usd")) or 100_000.0
        ret = _num(sp.get("return_pct")) or 0.0
        net_r, max_dd, sh90, beta = _lb_fields("spot_perp", ret)
        sp_rt = sp.get("risk_telemetry") or {}
        pos_cnt = len(sp.get("positions") or {}) if isinstance(sp.get("positions"), dict) else 0
        total_paper_eq += eq
        total_open_pos += pos_cnt
        _emit_strategy_row(
            b,
            strategy="spot_perp_hlp",
            category="Market Neutral",
            mode="paper",
            status_ok=not bool(sp_rt.get("nav_guard_triggered")),
            equity_usd=eq,
            starting_capital_usd=sc,
            return_pct=ret,
            net_return_pct=net_r,
            net_pnl_usd=round(eq - sc, 2),
            funding_pnl_usd=round((_num(sp.get("cum_funding_usd")) or 0.0) + (_num(sp.get("cum_hlp_yield_usd")) or 0.0), 4),
            costs_usd=_num(sp.get("cum_costs_usd")) or 0.0,
            live_apr_pct=_num(sp.get("blended_portfolio_apr_pct")) or 0.0,
            open_positions=pos_cnt,
            leverage=_num(sp_rt.get("gross_leverage")) or 0.0,
            max_dd_pct=max_dd,
            share_price=_num(sp.get("share_price")) or (eq / sc if sc > 0 else 1.0),
            sharpe_90d=sh90,
            beta_btc=beta,
        )

    if isinstance(cdp, dict):
        eq = _num(cdp.get("equity_usd")) or 0.0
        sc = _num(cdp.get("starting_capital_usd")) or 50_000.0
        ret = round((eq / sc - 1.0) * 100.0, 4) if sc > 0 else 0.0
        net_r, max_dd, sh90, beta = _lb_fields("cross_dex", ret)
        cpos = cdp.get("positions") or {}
        pos_cnt = len(cpos) if isinstance(cpos, dict) else 0
        gross_ntl = sum(2.0 * (_num(p.get("leg_notional_usd")) or 0.0) for p in cpos.values() if isinstance(p, dict)) if isinstance(cpos, dict) else 0.0
        weighted_apr = (
            sum((_num(p.get("leg_notional_usd")) or 0.0) * (_num(p.get("live_spread_apr_pct")) or 0.0) for p in cpos.values() if isinstance(p, dict))
            / max(eq, 1.0)
            if isinstance(cpos, dict)
            else 0.0
        )
        total_paper_eq += eq
        total_open_pos += pos_cnt
        _emit_strategy_row(
            b,
            strategy="cross_dex_v2",
            category="Market Neutral",
            mode="paper",
            status_ok=True,
            equity_usd=eq,
            starting_capital_usd=sc,
            return_pct=ret,
            net_return_pct=net_r,
            net_pnl_usd=round(eq - sc, 2),
            funding_pnl_usd=_num(cdp.get("cum_funding_usd")) or 0.0,
            costs_usd=_num(cdp.get("cum_costs_usd")) or 0.0,
            live_apr_pct=round(weighted_apr, 2),
            open_positions=pos_cnt,
            leverage=round(gross_ntl / max(eq, 1.0), 3),
            max_dd_pct=max_dd,
            share_price=round(eq / sc, 6) if sc > 0 else 1.0,
            sharpe_90d=sh90,
            beta_btc=beta,
        )

    if isinstance(hp, dict):
        eq = _num(hp.get("equity_usd")) or 0.0
        sc = _num(hp.get("starting_capital_usd")) or 50_000.0
        ret = _num(hp.get("return_pct")) or 0.0
        net_r, max_dd, sh90, beta = _lb_fields("hip3_paper", ret)
        hpos = hp.get("positions") or {}
        pos_cnt = len(hpos) if isinstance(hpos, dict) else 0
        gross_ntl = sum(2.0 * (_num(p.get("leg_notional_usd")) or 0.0) for p in hpos.values() if isinstance(p, dict)) if isinstance(hpos, dict) else 0.0
        weighted_apr = (
            sum((_num(p.get("leg_notional_usd")) or 0.0) * (_num(p.get("live_spread_apr_pct")) or 0.0) for p in hpos.values() if isinstance(p, dict))
            / max(eq, 1.0)
            if isinstance(hpos, dict)
            else 0.0
        )
        max_ora_div = max((_num(p.get("live_oracle_div_bps")) or 0.0 for p in hpos.values() if isinstance(p, dict)), default=0.0) if isinstance(hpos, dict) else 0.0
        total_paper_eq += eq
        total_open_pos += pos_cnt
        _emit_strategy_row(
            b,
            strategy="hip3_split_margin",
            category="Market Neutral",
            mode="paper",
            status_ok=max_ora_div <= 30.0,
            equity_usd=eq,
            starting_capital_usd=sc,
            return_pct=ret,
            net_return_pct=net_r,
            net_pnl_usd=round(eq - sc, 2),
            funding_pnl_usd=_num(hp.get("cum_funding_usd")) or 0.0,
            costs_usd=_num(hp.get("cum_costs_usd")) or 0.0,
            live_apr_pct=round(weighted_apr, 2),
            open_positions=pos_cnt,
            leverage=round(gross_ntl / max(eq, 1.0), 3),
            max_dd_pct=max_dd,
            share_price=round(eq / sc, 6) if sc > 0 else 1.0,
            sharpe_90d=sh90,
            beta_btc=beta,
        )

    if isinstance(paper, dict):
        eq = _num(paper.get("equity_usd")) or 0.0
        sc = _num(paper.get("starting_capital_usd")) or 100_000.0
        ret = _num(paper.get("return_pct")) or 0.0
        net_r, max_dd, sh90, beta = _lb_fields("metamodel", ret)
        pos_cnt = int(_num(paper.get("active_positions")) or 0)
        total_paper_eq += eq
        total_open_pos += pos_cnt
        _emit_strategy_row(
            b,
            strategy="metamodel_5bps",
            category="Observation",
            mode="paper",
            status_ok=True,
            equity_usd=eq,
            starting_capital_usd=sc,
            return_pct=ret,
            net_return_pct=net_r,
            net_pnl_usd=round(eq - sc, 2),
            funding_pnl_usd=0.0,
            costs_usd=0.0,
            live_apr_pct=0.0,
            open_positions=pos_cnt,
            leverage=1.0 if pos_cnt > 0 else 0.0,
            max_dd_pct=max_dd,
            share_price=round(eq / sc, 6) if sc > 0 else 1.0,
            sharpe_90d=sh90,
            beta_btc=beta,
        )

    if isinstance(attr, dict):
        eq = _num(attr.get("equity_usd")) or 0.0
        net_pnl = _num(attr.get("net_attributed_pnl_usd")) or 0.0
        sc = max(eq - net_pnl, 1.0)
        ret = round(net_pnl / sc * 100.0, 4)
        pos_cnt = int(_num(attr.get("open_positions_count")) or 0)
        tot_notional = sum(abs(_num(c.get("position_usd")) or 0.0) for c in (attr.get("per_coin") or []) if isinstance(c, dict))
        liq_w = len(attr.get("liq_warnings") or [])
        adl_w = len(attr.get("adl_warnings") or [])
        total_open_pos += pos_cnt
        _emit_strategy_row(
            b,
            strategy="testnet_alo_live",
            category="Live Testnet",
            mode="testnet",
            status_ok=bool(testnet_last_ok and liq_w == 0 and adl_w == 0),
            equity_usd=eq,
            starting_capital_usd=round(sc, 2),
            return_pct=ret,
            net_return_pct=ret,
            net_pnl_usd=net_pnl,
            funding_pnl_usd=_num(attr.get("funding_pnl_usd")) or 0.0,
            costs_usd=abs(_num(attr.get("fees_usd")) or 0.0),
            live_apr_pct=0.0,
            open_positions=pos_cnt,
            leverage=round(tot_notional / max(eq, 1.0), 3),
            max_dd_pct=0.0,
            share_price=round(eq / sc, 6) if sc > 0 else 1.0,
        )

    if hlp_apr_val is not None or hlp_tvl_val is not None:
        _emit_strategy_row(
            b,
            strategy="hlp_benchmark",
            category="Benchmark",
            mode="mainnet",
            status_ok=True,
            equity_usd=hlp_tvl_val or 0.0,
            starting_capital_usd=hlp_tvl_val or 0.0,
            return_pct=hlp_apr_val or 0.0,
            net_return_pct=hlp_apr_val or 0.0,
            net_pnl_usd=0.0,
            funding_pnl_usd=0.0,
            costs_usd=0.0,
            live_apr_pct=hlp_apr_val or 0.0,
            open_positions=1,
            leverage=1.0,
            max_dd_pct=0.0,
            share_price=1.0,
        )

    b.emit("hl_desk_total_paper_equity_usd", round(total_paper_eq, 2), help_text="Sum of active paper strategy equities USD")
    b.emit("hl_desk_total_open_positions", total_open_pos, help_text="Total open positions across all active strategies")
    b.emit(
        "hl_desk_nav_guard_triggers_total",
        sp_nav_guards + ap_nav_guards,
        metric_type="counter",
        help_text="Total Bounded NAV Sanity Guard triggers across desk ledgers",
    )

    return b.render()
