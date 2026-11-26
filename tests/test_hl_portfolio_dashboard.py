from __future__ import annotations

import json
from pathlib import Path

from hl_quant import hl_portfolio_dashboard as dash


def test_portfolio_dashboard_renders_empty_and_populated_artifacts(tmp_path: Path):
    # 1. Graceful rendering on completely empty data directory
    empty_payload = dash.collect_dashboard_payload(tmp_path)
    empty_html = dash.render_portfolio_dashboard_html(empty_payload)
    assert "<!DOCTYPE html>" in empty_html
    assert "Hyperliquid Quantitative" in empty_html
    assert "function openTab" in empty_html

    # 2. Populated artifacts render all 5 interactive tabs and inline SVG calibration curve
    (tmp_path / "hl_testnet_attribution").mkdir(parents=True)
    (tmp_path / "hl_testnet_attribution" / "attribution.json").write_text(
        json.dumps(
            {
                "account_address": "0xTEST",
                "equity_usd": 998.5,
                "total_fills": 20,
                "total_volume_usd": 450.0,
                "maker_volume_share_pct": 100.0,
                "effective_fee_bps": 1.5,
                "real_alo_fill_rate_pct": 93.3,
                "liq_warnings": [],
                "adl_warnings": [],
                "api_quota_health": "OK",
                "api_reqs_per_usd_vol": 0.02,
                "per_coin": [
                    {
                        "coin": "ETH",
                        "position_sz": 0.05,
                        "position_usd": 85.0,
                        "leverage_mode": "cross:3",
                        "fills_count": 4,
                        "maker_share_pct": 100.0,
                        "net_pnl_usd": 1.25,
                        "vw_markout_bps": 2.4,
                        "liq_distance_pct": 2500.0,
                        "adl_rank_score": 0.08,
                    }
                ],
            }
        )
    )
    (tmp_path / "hl_mm_calibration").mkdir(parents=True)
    (tmp_path / "hl_mm_calibration" / "calibration.json").write_text(
        json.dumps(
            {
                "ETH": {
                    "kappa_per_bp": 0.544,
                    "arrival_a_per_sec": 0.08,
                    "r2_fit": 0.83,
                    "empirical_sigma_tau_bps": 7.7,
                    "depth_curve": [
                        {"delta_bps": 1.0, "lambda_per_sec": 0.05, "fitted_lambda": 0.046},
                        {"delta_bps": 4.0, "lambda_per_sec": 0.01, "fitted_lambda": 0.009},
                    ],
                }
            }
        )
    )
    (tmp_path / "hl_cross_dex_paper").mkdir(parents=True)
    (tmp_path / "hl_cross_dex_paper" / "state.json").write_text(
        json.dumps(
            {
                "equity_usd": 49980.81,
                "cum_funding_usd": 0.0,
                "cum_costs_usd": 19.19,
                "positions": {
                    "XMR": {
                        "coin": "XMR",
                        "direction": "Long HL / Short lighter",
                        "leg_notional_usd": 5000.0,
                        "live_spread_apr_pct": 62.6,
                        "entry_l2_slip_bps": 11.6,
                        "entry_breakeven_hours": 39.0,
                        "hours_held": 0.0,
                        "net_pnl_usd": -7.28,
                    }
                },
            }
        )
    )

    pop_payload = dash.collect_dashboard_payload(tmp_path)
    pop_html = dash.render_portfolio_dashboard_html(pop_payload)
    assert "0xTEST" in pop_html
    assert "93.3%" in pop_html
    assert "62.6%" in pop_html
    assert "<svg viewBox=" in pop_html
    assert "tab-alpha" in pop_html
    assert "tab-frontier" in pop_html
    assert "ADL Rank" in pop_html
    assert "H6 Стресс BookMid" in pop_html
    assert pop_html.count("Простыми словами:") == 6
