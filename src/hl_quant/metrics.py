"""Zero-dependency Prometheus text exposition exporter for ``hl-backend``.

Converts the live atomic telemetry artifacts under ``/app/data/paper`` into valid
Prometheus ``text/plain; version=0.0.4`` gauges and counters so Prometheus or
VictoriaMetrics can scrape ``GET /api/v1/metrics`` (or internal ``GET /metrics``)
and visualize the full Hyperliquid Quant DeFi stack in Grafana.
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


def render_prometheus_metrics(
    overview: dict[str, Any],
    *,
    now_utc: datetime | None = None,
) -> str:
    """Render a full ``/api/v1/overview`` payload as Prometheus text exposition format."""
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
        for job in ws.get("jobs") or []:
            if not isinstance(job, dict) or not job.get("name"):
                continue
            jname = str(job["name"])
            state = str(job.get("state") or "idle")
            lbl = {"job": jname}
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

    # 3. Testnet ALO execution & post-trade attribution
    attr = overview.get("attribution")
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

    audit_recent = overview.get("testnet_audit_recent")
    if isinstance(audit_recent, list) and audit_recent:
        latest_audit = audit_recent[0]
        if isinstance(latest_audit, dict):
            b.emit("hl_testnet_hwm_usd", latest_audit.get("high_water_mark_usd"), help_text="Testnet equity high-water mark USD")
            b.emit(
                "hl_testnet_last_cycle_ok",
                str(latest_audit.get("status")) == "ok",
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
    if isinstance(fh, dict):
        b.emit("hl_hlp_apr_pct", fh.get("hlp_apr_pct"), help_text="HLP Vault benchmark APR %")
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

    # 5. Cross-DEX Paper v2
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
        b.emit(
            "hl_cross_dex_paper_closed_pairs_total",
            cdp.get("closed_pairs_count"),
            metric_type="counter",
            help_text="Closed Cross-DEX paper pairs",
        )

    # 6. Metamodel Observation Paper (5 bps)
    paper = overview.get("paper")
    if isinstance(paper, dict):
        b.emit("hl_metamodel_paper_equity_usd", paper.get("equity_usd"), help_text="Metamodel observation paper equity USD (5 bps)")
        b.emit("hl_metamodel_paper_return_pct", paper.get("return_pct"), help_text="Metamodel observation paper return %")
        b.emit("hl_metamodel_paper_active_positions", paper.get("active_positions"), help_text="Active metamodel paper positions")
        b.emit("hl_metamodel_paper_ticks_total", paper.get("ticks"), metric_type="counter", help_text="Total metamodel paper ticks")

    # 7. On-Chain Events (H2/H4/H5/H6) & HLP Vault
    ev = overview.get("onchain_events")
    if isinstance(ev, dict):
        b.emit("hl_hlp_tvl_usd", ev.get("hlp_tvl_usd"), help_text="HLP Vault TVL USD")
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

    # 8. Avellaneda-Stoikov MM Calibration
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

    return b.render()
