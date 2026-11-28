#!/usr/bin/env python3
"""Generate the institutional 8-row Grafana v2 dashboard JSON for Hyperliquid Quant DeFi.

Produces ``docs/grafana/hyperliquid_quant_defi_dashboard.json`` with:
- Zero overlapping grid coordinates
- ``instant: true`` on all Stat / Table / BarGauge queries (eliminating single-scrape 'No data')
- ``max by (...)`` Prometheus table aggregations + ``merge`` transformations so all multi-metric
  tables (All-Strategies Leaderboard, Autopilot Sleeves, Testnet Positions, Boros Fixed Funding,
  Worker Jobs) join cleanly into one row per entity.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

DS = {"type": "prometheus", "uid": "${datasource}"}


def _target(
    ref_id: str,
    expr: str,
    *,
    legend: str = "",
    instant: bool = False,
    table: bool = False,
) -> dict[str, Any]:
    t: dict[str, Any] = {
        "refId": ref_id,
        "expr": expr,
        "datasource": DS,
    }
    if legend:
        t["legendFormat"] = legend
    if instant:
        t["instant"] = True
        t["range"] = False
    if table:
        t["format"] = "table"
    return t


def _stat(
    pid: int,
    title: str,
    expr: str,
    x: int,
    y: int,
    w: int,
    h: int = 4,
    *,
    unit: str | None = None,
    decimals: int | None = None,
    mappings: list[dict[str, Any]] | None = None,
    thresholds: list[dict[str, Any]] | None = None,
    color_mode: str = "value",
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if mappings:
        defaults["mappings"] = mappings
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return {
        "id": pid,
        "type": "stat",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": [_target("A", expr, instant=True)],
        "options": {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": color_mode,
            "graphMode": "none",
            "justifyMode": "auto",
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


def _row(pid: int, title: str, y: int) -> dict[str, Any]:
    return {
        "id": pid,
        "type": "row",
        "title": title,
        "collapsed": False,
        "gridPos": {"h": 1, "w": 24, "x": 0, "y": y},
    }


def _timeseries(
    pid: int,
    title: str,
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    w: int,
    h: int = 7,
    *,
    description: str | None = None,
    unit: str | None = None,
    decimals: int | None = None,
    thresholds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "custom": {
            "drawStyle": "line",
            "lineInterpolation": "smooth",
            "lineWidth": 2,
            "fillOpacity": 10,
            "showPoints": "auto",
            "spanNulls": True,
        }
    }
    if unit:
        defaults["unit"] = unit
    if decimals is not None:
        defaults["decimals"] = decimals
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    panel: dict[str, Any] = {
        "id": pid,
        "type": "timeseries",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "options": {
            "legend": {"displayMode": "table", "placement": "bottom", "calcs": ["lastNotNull", "min", "max"]},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }
    if description:
        panel["description"] = description
    return panel


def _bargauge(
    pid: int,
    title: str,
    targets: list[dict[str, Any]],
    x: int,
    y: int,
    w: int,
    h: int = 7,
    *,
    unit: str | None = None,
    thresholds: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    if unit:
        defaults["unit"] = unit
    if thresholds:
        defaults["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return {
        "id": pid,
        "type": "bargauge",
        "title": title,
        "datasource": DS,
        "gridPos": {"h": h, "w": w, "x": x, "y": y},
        "targets": targets,
        "options": {
            "reduceOptions": {"values": False, "calcs": ["lastNotNull"], "fields": ""},
            "orientation": "horizontal",
            "displayMode": "gradient",
            "showUnfilled": True,
        },
        "fieldConfig": {"defaults": defaults, "overrides": []},
    }


def _override(field_name: str, props: list[dict[str, Any]]) -> dict[str, Any]:
    return {"matcher": {"id": "byName", "options": field_name}, "properties": props}


def build_dashboard() -> dict[str, Any]:
    panels: list[dict[str, Any]] = []
    pid = 0

    def next_id() -> int:
        nonlocal pid
        pid += 1
        return pid

    # =========================================================================
    # ROW 1: Executive Desk & Worker Health (y=0..4)
    # =========================================================================
    panels.append(_row(next_id(), "1. Executive Desk & Worker Health", 0))
    panels.append(
        _stat(
            next_id(),
            "Worker Status",
            "hl_worker_up",
            0,
            1,
            3,
            mappings=[{"type": "value", "options": {"1": {"text": "ONLINE", "color": "green"}, "0": {"text": "DOWN", "color": "red"}}}],
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            color_mode="background",
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Heartbeat Age",
            "hl_worker_heartbeat_age_seconds",
            3,
            1,
            3,
            unit="s",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 120}, {"color": "red", "value": 300}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Healthy Jobs",
            "hl_worker_jobs_healthy_ratio",
            6,
            1,
            3,
            unit="percentunit",
            decimals=0,
            thresholds=[{"color": "blue", "value": None}, {"color": "green", "value": 0.9}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Failed Jobs",
            "sum(hl_worker_job_failed)",
            9,
            1,
            3,
            decimals=0,
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            color_mode="background",
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Testnet Last Cycle",
            "hl_testnet_last_cycle_ok",
            12,
            1,
            3,
            mappings=[{"type": "value", "options": {"1": {"text": "OK", "color": "green"}, "0": {"text": "KILL-SWITCH", "color": "red"}}}],
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 1}],
            color_mode="background",
        )
    )
    panels.append(
        _stat(
            next_id(),
            "NAV Guard Active",
            "hl_desk_nav_guard_active",
            15,
            1,
            3,
            decimals=0,
            mappings=[{"type": "value", "options": {"0": {"text": "OK", "color": "green"}, "1": {"text": "ACTIVE", "color": "red"}}}],
            thresholds=[{"color": "green", "value": None}, {"color": "red", "value": 1}],
            color_mode="background",
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Open Positions (All)",
            "hl_desk_total_open_positions",
            18,
            1,
            3,
            decimals=0,
        )
    )
    panels.append(
        _stat(
            next_id(),
            "HLP Vault TVL",
            "hl_hlp_tvl_usd",
            21,
            1,
            3,
            unit="currencyUSD",
        )
    )

    # =========================================================================
    # ROW 2: ALL STRATEGIES LEADERBOARD & LIVE PERFORMANCE TRACKING (y=5..22)
    # =========================================================================
    panels.append(_row(next_id(), "2. All Strategies — Live Leaderboard & Performance Tracking", 5))
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "All Strategies — Live Results, Capital-Weighted Live Carry APR %, Leverage & Risk Leaderboard",
            "description": "Unified live ledger across Neutral Autopilot, HyperCore Spot+Perp (+10% HLP), Cross-DEX v2, HIP-3 Split-Margin, Metamodel (Obs), Live Testnet ALO, and HLP Benchmark. Capital-Weighted Live Carry APR % scales active pair spread APR by deployed leg notional / strategy equity (see hl_hip3_paper_avg_pair_apr_pct & hl_cross_dex_paper_avg_pair_apr_pct for unweighted open-pair averages).",
            "datasource": DS,
            "gridPos": {"h": 9, "w": 24, "x": 0, "y": 6},
            "targets": [
                _target("A", "max by (strategy, category, mode) (hl_strategy_status_ok)", instant=True, table=True),
                _target("B", "max by (strategy, category, mode) (hl_strategy_equity_usd)", instant=True, table=True),
                _target("C", "max by (strategy, category, mode) (hl_strategy_return_pct)", instant=True, table=True),
                _target("D", "max by (strategy, category, mode) (hl_strategy_net_return_pct)", instant=True, table=True),
                _target("E", "max by (strategy, category, mode) (hl_strategy_net_pnl_usd)", instant=True, table=True),
                _target("F", "max by (strategy, category, mode) (hl_strategy_funding_pnl_usd)", instant=True, table=True),
                _target("G", "max by (strategy, category, mode) (hl_strategy_costs_usd)", instant=True, table=True),
                _target("H", "max by (strategy, category, mode) (hl_strategy_live_apr_pct)", instant=True, table=True),
                _target("I", "max by (strategy, category, mode) (hl_strategy_open_positions)", instant=True, table=True),
                _target("J", "max by (strategy, category, mode) (hl_strategy_leverage)", instant=True, table=True),
                _target("K", "max by (strategy, category, mode) (hl_strategy_max_dd_pct)", instant=True, table=True),
                _target("L", "max by (strategy, category, mode) (hl_strategy_share_price)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "strategy": "Strategy",
                            "category": "Category",
                            "mode": "Mode",
                            "Value #A": "Status",
                            "Value #B": "Equity ($)",
                            "Value #C": "Gross Ret %",
                            "Value #D": "Net HWM %",
                            "Value #E": "Net PnL ($)",
                            "Value #F": "Funding/Yield ($)",
                            "Value #G": "Costs ($)",
                            "Value #H": "Capital-Weighted Live Carry APR %",
                            "Value #I": "Positions",
                            "Value #J": "Leverage",
                            "Value #K": "Max DD %",
                            "Value #L": "Share Px",
                        },
                    },
                },
            ],
            "options": {"showHeader": True, "sortBy": [{"displayName": "Capital-Weighted Live Carry APR %", "desc": True}]},
            "fieldConfig": {
                "defaults": {"custom": {"align": "auto", "cellOptions": {"type": "auto"}}},
                "overrides": [
                    _override(
                        "Status",
                        [
                            {
                                "id": "mappings",
                                "value": [
                                    {
                                        "type": "value",
                                        "options": {
                                            "1": {"text": "OK", "color": "green"},
                                            "0": {"text": "ALERT", "color": "red"},
                                        },
                                    }
                                ],
                            },
                            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 1}]}},
                        ],
                    ),
                    _override("Equity ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                    _override(
                        "Gross Ret %",
                        [
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 3},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0}]}},
                        ],
                    ),
                    _override(
                        "Net HWM %",
                        [
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 3},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0}]}},
                        ],
                    ),
                    _override(
                        "Net PnL ($)",
                        [
                            {"id": "unit", "value": "currencyUSD"},
                            {"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0}]}},
                        ],
                    ),
                    _override("Funding/Yield ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                    _override("Costs ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                    _override(
                        "Capital-Weighted Live Carry APR %",
                        [
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "text", "value": None}, {"color": "green", "value": 5}]}},
                        ],
                    ),
                    _override("Positions", [{"id": "decimals", "value": 0}]),
                    _override(
                        "Leverage",
                        [
                            {"id": "decimals", "value": 2},
                            {"id": "unit", "value": "suffix:x"},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 3.0}]}},
                        ],
                    ),
                    _override(
                        "Max DD %",
                        [
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 2},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 5.0}]}},
                        ],
                    ),
                    _override("Share Px", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 4}]),
                ],
            },
        }
    )

    panels.append(
        _timeseries(
            next_id(),
            "All Strategies — Normalized NAV Share Price ($1.00 = Inception)",
            [
                _target("A", 'hl_strategy_share_price{category!="Benchmark"}', legend="{{strategy}} ({{category}})"),
            ],
            0,
            15,
            12,
            h=8,
            unit="currencyUSD",
            decimals=4,
        )
    )
    panels.append(
        _timeseries(
            next_id(),
            "Cumulative Return % — Our Strategies vs Crypto & Gold Benchmarks (1Y)",
            [
                _target("A", 'hl_strategy_return_pct{category!="Benchmark"}', legend="{{strategy}}"),
                _target("B", "hl_benchmark_return_pct", legend="Benchmark {{asset}} (1Y)"),
            ],
            12,
            15,
            12,
            h=8,
            unit="percent",
            decimals=2,
        )
    )

    # =========================================================================
    # ROW 3: Neutral Autopilot Master Vault & Crisis-Alpha CTA (y=23..35)
    # =========================================================================
    panels.append(_row(next_id(), "3. Neutral Autopilot Master Vault & Crisis-Alpha CTA (<=5% Cap)", 23))
    panels.append(_stat(next_id(), "Autopilot Gross Equity", "hl_autopilot_gross_equity_usd", 0, 24, 4, unit="currencyUSD"))
    panels.append(_stat(next_id(), "Autopilot Net (HWM)", "hl_autopilot_net_equity_usd", 4, 24, 4, unit="currencyUSD"))
    panels.append(
        _stat(
            next_id(),
            "Autopilot Leverage",
            "hl_autopilot_gross_leverage",
            8,
            24,
            4,
            decimals=2,
            unit="suffix:x",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 3.0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Max Drift (Deadband 3%)",
            "hl_autopilot_max_drift_pct",
            12,
            24,
            4,
            unit="percent",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2.5}, {"color": "red", "value": 5.0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "CTA Sleeve % (Cap <=5%)",
            "hl_autopilot_directional_sleeve_pct",
            16,
            24,
            4,
            unit="percent",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 5.1}, {"color": "red", "value": 6.0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Net Directional Exposure",
            "hl_autopilot_directional_exposure_pct",
            20,
            24,
            4,
            unit="percent",
            decimals=2,
        )
    )
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "Autopilot Sleeve Allocations — Target vs Actual vs Deadband Drift",
            "datasource": DS,
            "gridPos": {"h": 7, "w": 12, "x": 0, "y": 28},
            "targets": [
                _target("A", "max by (sleeve, category) (hl_autopilot_sleeve_target_pct)", instant=True, table=True),
                _target("B", "max by (sleeve, category) (hl_autopilot_sleeve_actual_pct)", instant=True, table=True),
                _target("C", "max by (sleeve, category) (hl_autopilot_sleeve_drift_pct)", instant=True, table=True),
                _target("D", "max by (sleeve, category) (hl_autopilot_sleeve_allocation_usd)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "sleeve": "Sleeve",
                            "category": "Category",
                            "Value #A": "Target %",
                            "Value #B": "Actual %",
                            "Value #C": "Drift %",
                            "Value #D": "Allocation ($)",
                        },
                    },
                },
            ],
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override("Target %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}]),
                    _override("Actual %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}]),
                    _override("Drift %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]),
                    _override("Allocation ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 0}]),
                ],
            },
        }
    )
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "Crisis-Alpha CTA Sleeve — Multi-Horizon TSMOM Signals & Weights",
            "datasource": DS,
            "gridPos": {"h": 7, "w": 12, "x": 12, "y": 28},
            "targets": [
                _target("A", "max by (coin) (hl_autopilot_cta_signal)", instant=True, table=True),
                _target("B", "max by (coin) (hl_autopilot_cta_weight)", instant=True, table=True),
                _target("C", "max by (coin) (hl_autopilot_cta_ann_vol_pct)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "coin": "Asset",
                            "Value #A": "TSMOM Signal",
                            "Value #B": "CTA Weight",
                            "Value #C": "20D Ann Vol %",
                        },
                    },
                },
            ],
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override(
                        "TSMOM Signal",
                        [
                            {"id": "decimals", "value": 3},
                            {"id": "custom.cellOptions", "value": {"type": "color-text"}},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "green", "value": 0}]}},
                        ],
                    ),
                    _override("CTA Weight", [{"id": "decimals", "value": 3}]),
                    _override("20D Ann Vol %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}]),
                ],
            },
        }
    )

    # =========================================================================
    # ROW 4: HyperCore Spot+Perp (+10% HLP), HIP-3 & Cross-DEX Carry (y=35..54)
    # =========================================================================
    panels.append(_row(next_id(), "4. HyperCore Spot+Perp (+10% HLP), HIP-3 Split-Margin & Cross-DEX v2", 35))
    panels.append(_stat(next_id(), "Spot+Perp Equity", "hl_spot_perp_equity_usd", 0, 36, 4, unit="currencyUSD"))
    panels.append(_stat(next_id(), "Spot+Perp Blended APR", "hl_spot_perp_blended_apr_pct", 4, 36, 4, unit="percent"))
    panels.append(_stat(next_id(), "10% HLP Reserve", "hl_spot_perp_hlp_reserve_usd", 8, 36, 4, unit="currencyUSD"))
    panels.append(
        _stat(
            next_id(),
            "Spot+Perp Net Delta %",
            "hl_spot_perp_net_delta_pct",
            12,
            36,
            4,
            unit="percent",
            decimals=3,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": -1.0}, {"color": "red", "value": 1.0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Spot+Perp Min Liq Dist",
            "hl_spot_perp_min_perp_liq_distance_pct",
            16,
            36,
            4,
            unit="percent",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 20}, {"color": "green", "value": 35}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "HIP-3 Margin Utilization",
            "hl_hip3_paper_margin_utilization_pct",
            20,
            36,
            4,
            unit="percent",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 65}, {"color": "red", "value": 80}],
        )
    )
    panels.append(
        _timeseries(
            next_id(),
            "Capital-Weighted Live Carry APR % vs Open-Pair Spread APR — Spot+Perp, HIP-3 & Cross-DEX",
            [
                _target("A", 'hl_strategy_live_apr_pct{strategy=~"spot_perp_hlp|hip3_split_margin|cross_dex_v2"}', legend="Capital-Weighted {{strategy}}"),
                _target("B", "hl_hip3_paper_avg_pair_apr_pct", legend="HIP-3 Avg Open Pair APR"),
                _target("C", "hl_cross_dex_paper_avg_pair_apr_pct", legend="Cross-DEX Avg Open Pair APR"),
                _target("D", "hl_spot_perp_position_live_apr_pct", legend="Spot+Perp {{symbol}}"),
                _target("E", "hl_hip3_paper_pair_live_apr_pct", legend="HIP-3 {{symbol}} ({{short_dex}}/{{long_dex}})"),
                _target("F", "hl_cross_dex_paper_pair_live_apr_pct", legend="Cross-DEX {{coin}} ({{other_dex}})"),
            ],
            0,
            40,
            12,
            h=7,
            description="Compares Capital-Weighted Live Carry APR % (deployed leg notional * spread APR / strategy equity, accounting for idle cash buffers) against unweighted open-pair average APR (hl_hip3_paper_avg_pair_apr_pct, hl_cross_dex_paper_avg_pair_apr_pct) and per-pair spread APRs.",
            unit="percent",
        )
    )
    panels.append(
        _timeseries(
            next_id(),
            "Carry PnL Attribution — Cumulative Funding vs Basis MTM vs Costs (USD)",
            [
                _target("A", "(hl_spot_perp_funding_usd or vector(0)) + (hl_spot_perp_hlp_yield_usd or vector(0))", legend="Spot+Perp Funding+HLP"),
                _target("B", "hl_hip3_paper_funding_usd", legend="HIP-3 Funding"),
                _target("C", "hl_cross_dex_paper_funding_usd", legend="Cross-DEX Funding"),
                _target(
                    "D",
                    "-((hl_spot_perp_costs_usd or vector(0)) + (hl_hip3_paper_costs_usd or vector(0)) + (hl_cross_dex_paper_costs_usd or vector(0)))",
                    legend="Total Carry Costs",
                ),
            ],
            12,
            40,
            12,
            h=7,
            unit="currencyUSD",
        )
    )
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "Open HyperCore Spot+Perp Carry Positions",
            "datasource": DS,
            "gridPos": {"h": 7, "w": 12, "x": 0, "y": 47},
            "targets": [
                _target("A", "max by (symbol, spot_pair, tier) (hl_spot_perp_position_notional_usd)", instant=True, table=True),
                _target("B", "max by (symbol, spot_pair, tier) (hl_spot_perp_position_live_apr_pct)", instant=True, table=True),
                _target("C", "max by (symbol, spot_pair, tier) (hl_spot_perp_position_perp_lev)", instant=True, table=True),
                _target("D", "max by (symbol, spot_pair, tier) (hl_spot_perp_position_net_pnl_usd)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "symbol": "Asset",
                            "spot_pair": "Spot Pair",
                            "tier": "Tier",
                            "Value #A": "Notional ($)",
                            "Value #B": "Live APR %",
                            "Value #C": "Perp Lev",
                            "Value #D": "Net PnL ($)",
                        },
                    },
                },
            ],
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override("Notional ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 0}]),
                    _override("Live APR %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]),
                    _override("Perp Lev", [{"id": "unit", "value": "suffix:x"}, {"id": "decimals", "value": 2}]),
                    _override("Net PnL ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                ],
            },
        }
    )
    panels.append(
        _timeseries(
            next_id(),
            "HIP-3 Oracle Divergence (bps) — Active & Watchlist Pairs (Guard: 15 / 35 bps)",
            [
                _target("A", "hl_hip3_paper_pair_oracle_div_bps", legend="Open {{symbol}}"),
                _target("B", "hl_hip3_persistence_oracle_div_bps", legend="Watch {{symbol}}"),
            ],
            12,
            47,
            12,
            h=7,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 15}, {"color": "red", "value": 30}],
        )
    )

    # =========================================================================
    # ROW 5: Live Testnet ALO Execution & Per-Coin Risk Hygiene (y=54..69)
    # =========================================================================
    panels.append(_row(next_id(), "5. Live Testnet ALO Execution & Per-Coin Risk Hygiene", 54))
    panels.append(
        _timeseries(
            next_id(),
            "Testnet Equity & High-Water Mark (USD)",
            [
                _target("A", "hl_testnet_equity_usd", legend="Equity USD (4h attribution)"),
                _target("B", "hl_testnet_hwm_usd", legend="HWM USD"),
                _target("C", "hl_testnet_live_equity_usd", legend="Live Equity USD (15m probe)"),
            ],
            0,
            55,
            8,
            h=7,
            unit="currencyUSD",
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Live Drawdown vs HWM",
            "hl_testnet_drawdown_pct",
            8,
            55,
            4,
            h=3,
            unit="percent",
            decimals=2,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 3.5}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Orphan Notional (no 2-sided book)",
            "hl_testnet_live_orphan_notional_usd",
            8,
            58,
            4,
            h=4,
            unit="currencyUSD",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 1}, {"color": "red", "value": 150}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Testnet Net PnL",
            "hl_testnet_net_pnl_usd",
            12,
            55,
            3,
            h=3,
            unit="currencyUSD",
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "ALO Fill Rate",
            "hl_testnet_alo_fill_rate_pct",
            15,
            55,
            3,
            h=3,
            unit="percent",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 70}, {"color": "green", "value": 85}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Maker Share",
            "hl_testnet_maker_share_pct",
            18,
            55,
            3,
            h=3,
            unit="percent",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 80}, {"color": "green", "value": 90}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "VW Markout (bps)",
            "hl_testnet_vw_markout_bps",
            21,
            55,
            3,
            h=3,
            decimals=2,
            thresholds=[{"color": "red", "value": None}, {"color": "green", "value": 0}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Min Liq Distance",
            "hl_testnet_min_liq_distance_pct",
            12,
            58,
            4,
            h=4,
            unit="percent",
            thresholds=[{"color": "red", "value": None}, {"color": "yellow", "value": 30}, {"color": "green", "value": 50}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "API Reqs / $ Vol",
            "hl_testnet_api_reqs_per_usd_vol",
            16,
            58,
            4,
            h=4,
            decimals=4,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 0.5}, {"color": "red", "value": 0.85}],
        )
    )
    panels.append(
        _stat(
            next_id(),
            "Skipped Unquoted",
            "hl_testnet_last_cycle_skipped_unquoted",
            20,
            58,
            4,
            h=4,
            decimals=0,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 3}, {"color": "red", "value": 8}],
        )
    )
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "Testnet Per-Coin Positions, Net PnL, Maker %, Liq Distance & ADL Rank",
            "datasource": DS,
            "gridPos": {"h": 7, "w": 24, "x": 0, "y": 62},
            "targets": [
                _target("A", "max by (coin, leverage_mode) (hl_testnet_position_usd)", instant=True, table=True),
                _target("B", "max by (coin, leverage_mode) (hl_testnet_position_net_pnl_usd)", instant=True, table=True),
                _target("C", "max by (coin, leverage_mode) (hl_testnet_position_maker_share_pct)", instant=True, table=True),
                _target("D", "max by (coin, leverage_mode) (hl_testnet_position_liq_distance_pct)", instant=True, table=True),
                _target("E", "max by (coin, leverage_mode) (hl_testnet_position_adl_rank)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "coin": "Coin",
                            "leverage_mode": "Margin Mode",
                            "Value #A": "Position ($)",
                            "Value #B": "Net PnL ($)",
                            "Value #C": "Maker %",
                            "Value #D": "Liq Dist %",
                            "Value #E": "ADL Rank",
                        },
                    },
                },
            ],
            "options": {"showHeader": True, "sortBy": [{"displayName": "Position ($)", "desc": True}]},
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override("Position ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 2}]),
                    _override("Net PnL ($)", [{"id": "unit", "value": "currencyUSD"}, {"id": "decimals", "value": 3}]),
                    _override("Maker %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 1}]),
                    _override(
                        "Liq Dist %",
                        [
                            {"id": "unit", "value": "percent"},
                            {"id": "decimals", "value": 1},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "red", "value": None}, {"color": "yellow", "value": 25}, {"color": "green", "value": 50}]}},
                        ],
                    ),
                    _override(
                        "ADL Rank",
                        [
                            {"id": "decimals", "value": 2},
                            {"id": "thresholds", "value": {"mode": "absolute", "steps": [{"color": "green", "value": None}, {"color": "yellow", "value": 2.0}, {"color": "red", "value": 4.0}]}},
                        ],
                    ),
                ],
            },
        }
    )

    # =========================================================================
    # ROW 6: Frontier Yield — Pendle Boros, HyperEVM Chain 999 & HLP (y=69..81)
    # =========================================================================
    panels.append(_row(next_id(), "6. Frontier Yield: Pendle Boros Fixed Funding, HyperEVM (999) & HLP", 69))
    panels.append(_stat(next_id(), "HLP Benchmark APR", "hl_hlp_apr_pct", 0, 70, 4, unit="percent"))
    panels.append(_stat(next_id(), "Best Composable Loop", "hl_yield_stack_best_net_apr_pct", 4, 70, 4, unit="percent"))
    panels.append(_stat(next_id(), "HLP All-Time MM PnL", "hl_hlp_cum_pnl_usd", 8, 70, 4, unit="currencyUSD"))
    panels.append(_stat(next_id(), "Boros Active HL Markets", "hl_boros_hl_spread_markets", 12, 70, 4, decimals=0))
    panels.append(_stat(next_id(), "USDT0 Peg Discount (bps)", "hl_hyperevm_usdt0_peg_discount_bps", 16, 70, 4, decimals=2))
    panels.append(
        _stat(
            next_id(),
            "L1Read Max Drift (bps)",
            "hl_hyperevm_l1_read_max_drift_bps",
            20,
            70,
            4,
            decimals=2,
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 5}, {"color": "red", "value": 20}],
        )
    )
    panels.append(
        _timeseries(
            next_id(),
            "HLP APR vs Best HyperEVM Composable Yield Stack (%)",
            [
                _target("A", "hl_hlp_apr_pct", legend="HLP Benchmark APR"),
                _target("B", "hl_yield_stack_best_net_apr_pct", legend="Best Composable Loop APR"),
                _target("C", "hl_spot_perp_blended_apr_pct", legend="Spot+Perp (+10% HLP) Blended APR"),
            ],
            0,
            74,
            12,
            h=7,
            unit="percent",
        )
    )
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "Pendle Boros — Live Fixed-Funding Bid APR vs Floating Spread",
            "datasource": DS,
            "gridPos": {"h": 7, "w": 12, "x": 12, "y": 74},
            "targets": [
                _target("A", "max by (market_id, symbol, underlying) (hl_boros_fixed_bid_apr_pct)", instant=True, table=True),
                _target("B", "max by (market_id, symbol, underlying) (hl_boros_fixed_minus_float_pct)", instant=True, table=True),
                _target("C", "max by (market_id, symbol, underlying) (hl_boros_days_to_maturity)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "symbol": "Boros Market",
                            "underlying": "Asset",
                            "Value #A": "Fixed Bid APR %",
                            "Value #B": "Fix − Float %",
                            "Value #C": "Days to Mat",
                        },
                    },
                },
            ],
            "options": {"showHeader": True, "sortBy": [{"displayName": "Fixed Bid APR %", "desc": True}]},
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override("Fixed Bid APR %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]),
                    _override("Fix − Float %", [{"id": "unit", "value": "percent"}, {"id": "decimals", "value": 2}]),
                    _override("Days to Mat", [{"id": "decimals", "value": 1}]),
                ],
            },
        }
    )

    # =========================================================================
    # ROW 7: On-Chain Alpha (H2/H4/H5/H6) & Avellaneda-Stoikov MM (y=81..93)
    # =========================================================================
    panels.append(_row(next_id(), "7. On-Chain Alpha (H2/H4/H5/H6) & Avellaneda-Stoikov MM Calibration", 81))
    panels.append(
        _timeseries(
            next_id(),
            "H2 Whale Liquidation Walls (0–5% from Mark, USD)",
            [
                _target("A", "hl_onchain_h2_liq_0_5_usd", legend="{{coin}} (0–5%)"),
            ],
            0,
            82,
            12,
            h=7,
            unit="currencyUSD",
        )
    )
    panels.append(
        _timeseries(
            next_id(),
            "Avellaneda-Stoikov Empirical Kappa (/bp) & R²",
            [
                _target("A", "hl_mm_calibration_kappa", legend="{{coin}} kappa"),
                _target("B", "hl_mm_calibration_r2", legend="{{coin}} R²"),
            ],
            12,
            82,
            12,
            h=7,
        )
    )
    panels.append(_stat(next_id(), "AS-MM Latency p50", "hl_mm_tick_to_quote_p50_us", 0, 89, 4, unit="µs", decimals=2))
    panels.append(_stat(next_id(), "AS-MM Latency p99", "hl_mm_tick_to_quote_p99_us", 4, 89, 4, unit="µs", decimals=2))
    panels.append(_stat(next_id(), "AS-MM Throughput", "hl_mm_throughput_ticks_per_sec", 8, 89, 4, decimals=0, unit="suffix: tps"))
    panels.append(_stat(next_id(), "HIP-3 24h Builder Vol", "hl_hip3_day_volume_usd", 12, 89, 4, unit="currencyUSD"))
    panels.append(_stat(next_id(), "5-DEX Top Spread APR", "hl_cross_dex_scan_top_apr_pct", 16, 89, 4, unit="percent"))
    panels.append(_stat(next_id(), "Spot+Perp L2 Eligible", "hl_spot_perp_scan_eligible_pairs", 20, 89, 4, decimals=0))

    # =========================================================================
    # ROW 8: Scheduler Jobs & Artifact Freshness Audit (y=93..102)
    # =========================================================================
    panels.append(_row(next_id(), "8. Scheduler Jobs (16 UTC Crons) & Artifact Freshness", 93))
    panels.append(
        {
            "id": next_id(),
            "type": "table",
            "title": "All 16 UTC Worker Jobs — Live Status, Runs & Duration",
            "datasource": DS,
            "gridPos": {"h": 8, "w": 12, "x": 0, "y": 94},
            "targets": [
                _target(
                    "A",
                    "max by (worker_job) (hl_worker_job_enabled * (1 + hl_worker_job_ok + 2 * hl_worker_job_interrupted + 3 * hl_worker_job_failed))",
                    instant=True,
                    table=True,
                ),
                _target("B", "max by (worker_job) (hl_worker_job_enabled)", instant=True, table=True),
                _target("C", "max by (worker_job) (hl_worker_job_runs_ok_total)", instant=True, table=True),
                _target("D", "max by (worker_job) (hl_worker_job_runs_failed_total)", instant=True, table=True),
                _target("E", "max by (worker_job) (hl_worker_job_last_duration_seconds)", instant=True, table=True),
            ],
            "transformations": [
                {"id": "merge", "options": {}},
                {
                    "id": "organize",
                    "options": {
                        "excludeByName": {"Time": True},
                        "renameByName": {
                            "worker_job": "Job",
                            "Value #A": "Status",
                            "Value #B": "Enabled",
                            "Value #C": "Total OK",
                            "Value #D": "Total Fail",
                            "Value #E": "Duration (s)",
                        },
                    },
                },
            ],
            "fieldConfig": {
                "defaults": {},
                "overrides": [
                    _override(
                        "Status",
                        [
                            {
                                "id": "mappings",
                                "value": [
                                    {
                                        "type": "value",
                                        "options": {
                                            "0": {"text": "DISABLED", "color": "semi-dark-blue"},
                                            "1": {"text": "IDLE / RUN", "color": "blue"},
                                            "2": {"text": "OK", "color": "green"},
                                            "3": {"text": "INTERRUPTED", "color": "yellow"},
                                            "4": {"text": "FAIL", "color": "red"},
                                        },
                                    }
                                ],
                            },
                            {"id": "custom.cellOptions", "value": {"type": "color-background"}},
                            {
                                "id": "thresholds",
                                "value": {
                                    "mode": "absolute",
                                    "steps": [
                                        {"color": "semi-dark-blue", "value": None},
                                        {"color": "blue", "value": 1},
                                        {"color": "green", "value": 2},
                                        {"color": "yellow", "value": 3},
                                        {"color": "red", "value": 4},
                                    ],
                                },
                            },
                        ],
                    ),
                    _override("Duration (s)", [{"id": "unit", "value": "s"}, {"id": "decimals", "value": 1}]),
                ],
            },
        }
    )
    panels.append(
        _bargauge(
            next_id(),
            "Artifact Freshness Age (min)",
            [_target("A", "hl_artifact_age_minutes", legend="{{artifact}}", instant=True)],
            12,
            94,
            12,
            h=8,
            unit="m",
            thresholds=[{"color": "green", "value": None}, {"color": "yellow", "value": 300}, {"color": "red", "value": 1500}],
        )
    )

    return {
        "uid": "hl-quant-defi-overview",
        "title": "Hyperliquid Quant DeFi — Live Telemetry",
        "description": "Institutional 24/7 Hyperliquid Quant DeFi desk telemetry: Unified All-Strategies Leaderboard, Neutral Autopilot (+ <=5% Crisis-CTA), HyperCore Spot+Perp (+10% HLP), HIP-3 Split-Margin, Cross-DEX v2, Testnet ALO Execution, Pendle Boros, HyperEVM Chain 999, On-Chain Alpha & Avellaneda-Stoikov MM.",
        "tags": ["hyperliquid", "quant", "defi", "hip3", "testnet", "autopilot", "spot-perp"],
        "timezone": "utc",
        "schemaVersion": 39,
        "version": 2,
        "refresh": "1m",
        "editable": True,
        "time": {"from": "now-24h", "to": "now"},
        "templating": {
            "list": [
                {
                    "name": "datasource",
                    "label": "Prometheus / VictoriaMetrics",
                    "type": "datasource",
                    "query": "prometheus",
                    "refresh": 1,
                    "hide": 0,
                }
            ]
        },
        "panels": panels,
    }


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    out = root / "docs" / "grafana" / "hyperliquid_quant_defi_dashboard.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    dash = build_dashboard()
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(dash, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(out)
    print(f"Wrote {out} ({len(dash['panels'])} panels)")


if __name__ == "__main__":
    main()
