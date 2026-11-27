from __future__ import annotations

import json
from pathlib import Path
import re

import yaml

from hl_quant.metrics import render_prometheus_metrics

ROOT = Path(__file__).resolve().parents[1]
DASH_PATH = ROOT / "docs" / "grafana" / "hyperliquid_quant_defi_dashboard.json"
PROM_ALERTS_PATH = ROOT / "docs" / "grafana" / "prometheus_alerts.yml"
GRAFANA_ALERTS_PATH = ROOT / "docs" / "grafana" / "grafana_alert_rules.yml"


def _sample_full_metrics_names() -> set[str]:
    """Render metrics with a maximally populated synthetic payload and collect every emitted metric name."""
    from datetime import datetime, timezone

    overview = {
        "build_info": {"service": "hl-backend", "sha": "cf7ab78"},
        "worker_status": {
            "heartbeat_utc": "2026-11-27 14:00:00 UTC",
            "telegram_enabled": True,
            "testnet_execution_enabled": True,
            "jobs": [{"name": "spot_perp_paper", "enabled": True, "state": "ok", "runs_ok": 3, "runs_failed": 0, "last_duration_s": 5.0}],
        },
        "artifact_freshness": {"spot_perp_paper": {"exists": True, "age_minutes": 1.0}},
        "attribution": {
            "equity_usd": 1004.0,
            "perps_equity_usd": 160.0,
            "net_attributed_pnl_usd": 4.8,
            "realized_price_pnl_usd": 5.0,
            "unrealized_price_pnl_usd": 0.4,
            "funding_pnl_usd": -0.3,
            "fees_usd": -0.3,
            "open_positions_count": 10,
            "total_fills": 100,
            "total_volume_usd": 2000.0,
            "maker_volume_share_pct": 93.0,
            "real_alo_fill_rate_pct": 90.0,
            "effective_fee_bps": 1.7,
            "vw_markout_bps": 26.0,
            "min_liq_distance_pct": 500.0,
            "api_reqs_per_usd_vol": 0.005,
            "liq_warnings": [],
            "adl_warnings": [],
            "per_coin": [{"coin": "BTC", "leverage_mode": "cross", "position_usd": 120.0, "net_pnl_usd": 1.2, "liq_distance_pct": 400.0, "adl_rank_score": 0.5, "maker_share_pct": 100.0}],
        },
        "testnet_audit_recent": [{"status": "ok", "high_water_mark_usd": 1006.0, "placed_count": 5, "skipped_unquoted_coins": [], "orphan_positions": {}}],
        "testnet_risk": {
            "observed_at_ms": 1795806081453,
            "equity_usd": 992.0,
            "free_margin_usd": 847.0,
            "drawdown_pct": 1.4,
            "drawdown_headroom_pct": 3.6,
            "gross_leverage": 0.37,
            "open_positions": 1,
            "unrealized_pnl_mark_usd": -13.0,
            "unrealized_pnl_oracle_usd": -7.0,
            "mark_oracle_pnl_gap_pct": 0.6,
            "max_mark_oracle_dev_pct": 29.7,
            "orphan_positions": {"SKY": {"szi": -975.0}},
            "orphan_notional_usd": 45.0,
            "orphan_unrealized_pnl_usd": 0.03,
            "per_coin": [{"coin": "SKY", "book": "ask_only", "notional_usd": -45.0, "unrealized_pnl_mark_usd": 0.03,
                          "unrealized_pnl_oracle_usd": -0.1, "mark_oracle_dev_pct": -0.9, "orphan": True}],
        },
        "hip3_paper": {
            "starting_capital_usd": 50000.0,
            "equity_usd": 50010.0,
            "return_pct": 0.02,
            "margin_locked_usd": 5000.0,
            "margin_utilization_pct": 10.0,
            "cum_funding_usd": 15.0,
            "cum_basis_mtm_usd": 0.0,
            "cum_costs_usd": 5.0,
            "closed_pairs_count": 0,
            "payback_achieved_count": 0,
            "positions": {"NVDA": {"short_dex": "io", "long_dex": "xyz", "margin_mode": "cross", "leg_notional_usd": 5000.0, "live_spread_apr_pct": 40.0, "live_oracle_div_bps": 1.0, "net_pnl_usd": 5.0}},
        },
        "hip3": {"total_hip3_day_vol_usd": 2.5e9, "active_markets_count": 170},
        "frontier_history": {
            "hlp_apr_pct": 14.3,
            "best_yield_strategy": {"net_apr_pct": 9.1},
            "hip3_persistence": [{"base_symbol": "NVDA", "clean_streak": 4, "latest_apr_pct": 40.0, "median_oracle_div_bps": 1.0}],
        },
        "cross_dex_paper": {
            "starting_capital_usd": 50000.0,
            "equity_usd": 49990.0,
            "cum_funding_usd": 5.0,
            "cum_basis_mtm_usd": 0.0,
            "cum_costs_usd": 15.0,
            "closed_pairs_count": 0,
            "positions": {"BTC": {"other_dex": "extended", "leg_notional_usd": 5000.0, "live_spread_apr_pct": 30.0, "net_pnl_usd": -2.0}},
        },
        "cross_dex_all": [{"coin": "BTC", "spread_apr_pct": 35.0}],
        "paper": {"starting_capital_usd": 100000.0, "equity_usd": 99500.0, "return_pct": -0.5, "active_positions": 12, "ticks": 4},
        "settlement_meta": {"degraded": False, "collection_ratio": 1.0},
        "onchain_events": {
            "hlp_tvl_usd": 152e6,
            "hlp_mm_pnl": {"hlp_cum_pnl_usd": 141e6, "hlp_pnl_delta_usd": 200.0},
            "alerts_count": {"h2": 2},
            "total_logged": {"h2": 100},
            "h2_events": [{"coin": "BTC", "total_0_5_usd": 100e6, "total_0_10_usd": 150e6, "nearest_wall_pct": 3.0}],
        },
        "boros": {
            "active_markets_count": 39,
            "hl_and_spread_count": 17,
            "markets": [{"symbol": "HL-BTC-28D", "underlying": "BTC", "best_bid_apr_pct": 7.4, "fixed_minus_float_bid_pct": -3.5, "days_to_maturity": 28.0}],
        },
        "hyperevm": {
            "chain": {"block_number": 1800000, "usdt0_peg_discount_bps": 11.0},
            "l1_read_oracles": [{"drift_bps": 0.0}],
            "evm_arb": [{"executable": False}],
        },
        "mm_calibration": {"BTC": {"kappa_per_bp": 0.5, "r2_fit": 0.9, "implied_min_half_spread_bps": 1.8, "empirical_sigma_1s_bps": 1.0}},
        "latency_bench": {"p50_us": 5.5, "p99_us": 12.0, "throughput_ticks_per_sec": 170000.0},
        "spot_perp_paper": {
            "starting_capital_usd": 100000.0,
            "equity_usd": 100100.0,
            "share_price": 1.001,
            "return_pct": 0.1,
            "blended_portfolio_apr_pct": 8.5,
            "hlp_reserve_usd": 10000.0,
            "hlp_reserve_share_pct": 10.0,
            "free_cash_usd": 8000.0,
            "cum_funding_usd": 120.0,
            "cum_hlp_yield_usd": 20.0,
            "cum_basis_mtm_usd": 5.0,
            "cum_costs_usd": 45.0,
            "nav_guard_triggers": 0,
            "positions": {"BTC": {"spot_pair": "@142", "asset_tier": "Crypto - Large Cap", "perp_notional_usd": 9333.0, "live_funding_apr_pct": 10.95, "net_pnl_usd": 20.0, "effective_perp_leverage": 2.0}},
            "risk_telemetry": {"gross_leverage": 1.3, "leverage_30d_max": 1.4, "net_delta_pct": 0.01, "min_perp_liq_distance_pct": 45.0, "nav_guard_triggered": False},
        },
        "spot_perp_scan": {"opportunities": [{"base_symbol": "BTC", "eligible": True, "funding_apr_pct": 10.95}]},
        "autopilot": {
            "starting_capital_usd": 250000.0,
            "gross_equity_usd": 250200.0,
            "net_equity_usd": 250150.0,
            "gross_return_pct": 0.08,
            "net_return_pct": 0.06,
            "gross_share_price": 1.0008,
            "net_share_price": 1.0006,
            "hwm_net_share_price": 1.0006,
            "cum_mgmt_fee_usd": 10.0,
            "cum_perf_fee_usd": 40.0,
            "cum_rebalance_cost_usd": 0.0,
            "rebalances_count": 0,
            "nav_guard_triggers": 0,
            "sleeves": [{"name": "spot_perp_paper", "category": "Market Neutral", "target_weight_pct": 35.0, "actual_weight_pct": 35.0, "drift_pct": 0.0, "allocation_usd": 87500.0}],
            "cta_state": {"assets": {"BTC": {"tsmom_signal": -0.7, "weight": -0.1, "ann_vol_pct": 45.0}}},
            "risk_telemetry": {"gross_leverage": 1.2, "leverage_30d_max": 1.3, "max_weight_drift_pct": 0.2, "net_directional_exposure_pct": -1.0, "directional_sleeve_pct": 5.0, "reserve_sleeve_pct": 15.0, "nav_guard_triggered": False},
        },
    }
    analytics = {
        "leaderboard": [
            {"id": "bench_btc", "is_benchmark": True, "spot_price_usd": 61400.0, "gross_returns": {"all_time": -22.0}, "max_dd_gross_pct": 32.0},
            {"id": "spot_perp", "is_benchmark": False, "net_returns": {"all_time": 0.08}, "max_dd_gross_pct": 0.1, "sharpe_gross": 4.2},
        ],
        "market_exposure": {"BTC": {"rows": [{"id": "spot_perp", "beta": 0.002}]}},
    }
    text = render_prometheus_metrics(overview, analytics=analytics, now_utc=datetime(2026, 11, 27, 14, 0, 30, tzinfo=timezone.utc))
    names = set()
    for ln in text.splitlines():
        if ln and not ln.startswith("#"):
            names.add(ln.split("{", 1)[0].split(" ", 1)[0])
    return names


def test_grafana_dashboard_json_valid_and_all_metrics_exist():
    dash = json.loads(DASH_PATH.read_text(encoding="utf-8"))
    assert dash["uid"] == "hl-quant-defi-overview"
    panels = dash["panels"]
    assert len(panels) >= 35

    ids = [p["id"] for p in panels]
    assert len(ids) == len(set(ids)), "Duplicate panel IDs in Grafana dashboard"

    emitted = _sample_full_metrics_names()
    metric_re = re.compile(r"\b(hl_[a-z0-9_]+)\b")

    for p in panels:
        for t in p.get("targets") or []:
            expr = str(t.get("expr") or "")
            for m in metric_re.findall(expr):
                assert m in emitted, f"Panel '{p['title']}' references unknown metric '{m}'"
            if p["type"] in ("stat", "table", "bargauge"):
                assert t.get("instant") is True, f"Panel '{p['title']}' ({p['type']}) must set instant=true"


def test_prometheus_and_grafana_alert_rules_valid_and_reference_emitted_metrics():
    emitted = _sample_full_metrics_names()
    metric_re = re.compile(r"\b(hl_[a-z0-9_]+)\b")

    prom = yaml.safe_load(PROM_ALERTS_PATH.read_text(encoding="utf-8"))
    groups = prom["groups"]
    assert len(groups) == 2
    crit_rules = groups[0]["rules"]
    assert len(crit_rules) >= 10
    for grp in groups:
        for rule in grp["rules"]:
            assert rule.get("alert") and rule.get("expr")
            for m in metric_re.findall(str(rule["expr"])):
                assert m in emitted, f"Prometheus alert '{rule['alert']}' references unknown metric '{m}'"

    graf = yaml.safe_load(GRAFANA_ALERTS_PATH.read_text(encoding="utf-8"))
    assert len(graf["groups"][0]["rules"]) >= 9
    g_rules = [r for grp in graf["groups"] for r in grp["rules"]]
    uids = [r["uid"] for r in g_rules]
    assert len(uids) == len(set(uids)), "Grafana alert rule uids must be unique"
    for required in ("hl-crit-testnet-drawdown", "hl-warn-testnet-orphans", "hl-warn-testnet-risk-stale", "hl-warn-testnet-attr-stale"):
        assert required in uids
    prom_names = {r["alert"] for grp in groups for r in grp["rules"]}
    for required in ("HLTestnetDrawdownNearKillSwitch", "HLTestnetOrphanPositions", "HLTestnetRiskProbeStale", "HLTestnetAttributionStale"):
        assert required in prom_names
    for rule in g_rules:
        expr = str(rule["data"][0]["model"]["expr"])
        assert "bool(" not in expr, f"Invalid PromQL bool(...) function in '{rule['title']}'; use '< bool' / '> bool'"
        for m in metric_re.findall(expr):
            assert m in emitted, f"Grafana alert '{rule['title']}' references unknown metric '{m}'"


def test_grafana_dashboard_json_is_in_sync_with_builder():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "build_grafana_dashboard", ROOT / "scripts" / "ops" / "build_grafana_dashboard.py"
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    expected = mod.build_dashboard()
    actual = json.loads(DASH_PATH.read_text(encoding="utf-8"))
    assert actual == expected, "Run `python3 scripts/ops/build_grafana_dashboard.py` to sync dashboard JSON"
