from __future__ import annotations

from datetime import datetime, timezone

from hl_quant.metrics import PROMETHEUS_CONTENT_TYPE, render_prometheus_metrics


def test_render_prometheus_metrics_emits_valid_exposition_lines():
    now = datetime(2026, 11, 27, 14, 0, 30, tzinfo=timezone.utc)
    payload = {
        "build_info": {"service": "hl-backend", "sha": "cc0dfbc08495f727"},
        "worker_status": {
            "heartbeat_utc": "2026-11-27 14:00:00 UTC",
            "telegram_enabled": True,
            "testnet_execution_enabled": True,
            "jobs": [
                {"name": "hip3_paper", "enabled": True, "state": "ok", "runs_ok": 4, "runs_failed": 0, "last_duration_s": 5.2},
                {"name": "onchain_events", "enabled": True, "state": "failed", "runs_ok": 49, "runs_failed": 1, "last_duration_s": 29.1},
            ],
        },
        "artifact_freshness": {"hip3_paper": {"exists": True, "age_minutes": 12.5}},
        "attribution": {
            "equity_usd": 1004.16,
            "net_attributed_pnl_usd": 4.85,
            "maker_volume_share_pct": 93.17,
            "real_alo_fill_rate_pct": 90.0,
            "vw_markout_bps": 26.13,
            "min_liq_distance_pct": 1375.13,
            "liq_warnings": [],
            "adl_warnings": [],
        },
        "testnet_audit_recent": [
            {"status": "ok", "high_water_mark_usd": 1006.55, "placed_count": 8, "skipped_unquoted_coins": ["JUP", "PONS"]}
        ],
        "testnet_risk": {
            "observed_at_ms": 1795806081453,
            "equity_usd": 992.76,
            "free_margin_usd": 847.51,
            "drawdown_pct": 1.3698,
            "drawdown_headroom_pct": 3.6302,
            "gross_leverage": 0.3741,
            "open_positions": 13,
            "unrealized_pnl_mark_usd": -13.48,
            "unrealized_pnl_oracle_usd": -6.96,
            "mark_oracle_pnl_gap_pct": 0.6569,
            "max_mark_oracle_dev_pct": 29.74,
            "orphan_positions": {"SKY": {"szi": -975.0}, "VVV": {"szi": 1.71}},
            "orphan_notional_usd": 89.41,
            "orphan_unrealized_pnl_usd": -2.73,
            "per_coin": [
                {"coin": "SKY", "book": "ask_only", "notional_usd": -45.52, "unrealized_pnl_mark_usd": 0.03,
                 "unrealized_pnl_oracle_usd": -0.1, "mark_oracle_dev_pct": -0.9, "orphan": True},
                {"coin": "XMR", "book": "two_sided", "notional_usd": 52.29, "unrealized_pnl_mark_usd": -21.71,
                 "unrealized_pnl_oracle_usd": -9.76, "mark_oracle_dev_pct": -18.3, "orphan": False},
            ],
        },
        "hip3_paper": {
            "equity_usd": 49996.135,
            "return_pct": -0.0077,
            "margin_locked_usd": 7500.0,
            "margin_utilization_pct": 15.0,
            "cum_funding_usd": 12.4,
            "cum_basis_mtm_usd": 1.2,
            "cum_costs_usd": 3.865,
            "closed_pairs_count": 1,
            "payback_achieved_count": 1,
            "positions": {
                "NVDA": {
                    "short_dex": "io",
                    "long_dex": "xyz",
                    "margin_mode": "cross",
                    "live_spread_apr_pct": 41.26,
                    "live_oracle_div_bps": 0.0,
                    "net_pnl_usd": 5.2,
                }
            },
        },
        "cross_dex_paper": {
            "equity_usd": 49985.25,
            "cum_funding_usd": 2.08,
            "cum_basis_mtm_usd": 1.43,
            "cum_costs_usd": 18.26,
            "closed_pairs_count": 0,
            "positions": {"NEAR": {"other_dex": "extended", "live_spread_apr_pct": 37.77, "net_pnl_usd": -4.76}},
        },
        "paper": {"equity_usd": 99237.05, "return_pct": -0.763, "active_positions": 16, "ticks": 4},
        "onchain_events": {
            "hlp_tvl_usd": 152_279_944.79,
            "hlp_mm_pnl": {"hlp_cum_pnl_usd": 141_184_012.13, "hlp_pnl_delta_usd": 172.53},
            "alerts_count": {"h2": 3, "h4": 0, "h5": 0, "h6": 0},
            "total_logged": {"h2": 137},
            "h2_events": [{"coin": "BTC", "total_0_5_usd": 124_610_510.0, "total_0_10_usd": 189_827_752.0, "nearest_wall_pct": 2.97}],
        },
        "mm_calibration": {
            "ETH": {"kappa_per_bp": 0.562, "r2_fit": 0.9142, "implied_min_half_spread_bps": 1.78, "empirical_sigma_1s_bps": 0.778}
        },
    }

    text = render_prometheus_metrics(payload, now_utc=now)
    assert PROMETHEUS_CONTENT_TYPE.startswith("text/plain")
    assert 'hl_build_info{service="hl-backend",sha="cc0dfbc08495"} 1' in text
    assert "hl_worker_heartbeat_age_seconds 30" in text
    assert "hl_worker_up 1" in text
    assert 'hl_worker_job_ok{worker_job="hip3_paper"} 1' in text
    assert 'hl_worker_job_failed{worker_job="onchain_events"} 1' in text
    assert "hl_testnet_equity_usd 1004.16" in text
    assert "hl_testnet_last_cycle_skipped_unquoted 2" in text
    assert "hl_testnet_live_equity_usd 992.76" in text
    assert "hl_testnet_drawdown_pct 1.3698" in text
    assert "hl_testnet_live_orphan_positions 2" in text
    assert "hl_testnet_live_orphan_notional_usd 89.41" in text
    assert "hl_testnet_unrealized_pnl_oracle_usd -6.96" in text
    assert "hl_testnet_mark_oracle_pnl_gap_pct 0.6569" in text
    assert "hl_testnet_risk_observed_ts_seconds 1795806081.453" in text
    assert 'hl_testnet_live_position_orphan{coin="SKY",book="ask_only"} 1' in text
    assert 'hl_testnet_live_position_orphan{coin="XMR",book="two_sided"} 0' in text
    assert 'hl_testnet_live_position_mark_oracle_dev_pct{coin="XMR",book="two_sided"} -18.3' in text
    assert "hl_hip3_paper_equity_usd 49996.135" in text
    assert 'hl_hip3_paper_pair_live_apr_pct{symbol="NVDA",short_dex="io",long_dex="xyz",margin_mode="cross"} 41.26' in text
    assert 'hl_cross_dex_paper_pair_live_apr_pct{coin="NEAR",other_dex="extended"} 37.77' in text
    assert "hl_hlp_tvl_usd 152279944.79" in text
    assert 'hl_mm_calibration_kappa{coin="ETH"} 0.562' in text
    # Unified strategy leaderboard metrics emitted across all active strategies (15m testnet_risk overrides older 4h attribution)
    assert 'hl_strategy_equity_usd{strategy="hip3_split_margin",category="Market Neutral",mode="paper"} 49996.135' in text
    assert 'hl_strategy_equity_usd{strategy="cross_dex_v2",category="Market Neutral",mode="paper"} 49985.25' in text
    assert 'hl_strategy_equity_usd{strategy="metamodel_5bps",category="Observation",mode="paper"} 99237.05' in text
    assert 'hl_strategy_equity_usd{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 992.76' in text
    assert "hl_desk_total_open_positions" in text
    assert "hl_desk_nav_guard_active 0" in text
    assert "hl_desk_nav_guard_triggers_total 0" in text
    # Every emitted sample value must be a finite number (never NaN/Inf)
    import math

    sample_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(sample_lines) >= 25
    for ln in sample_lines:
        val = float(ln.rsplit(" ", 1)[1])
        assert math.isfinite(val), ln

    # Strict OpenMetrics / `promtool check metrics` type & suffix validation + live drawdown check
    assert 'hl_strategy_max_dd_pct{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 1.3698' in text
    types_by_name: dict[str, str] = {}
    seen_families: set[str] = set()
    current_family: str | None = None
    for ln in text.splitlines():
        if not ln:
            continue
        if ln.startswith("# TYPE "):
            _, _, rest = ln.partition("# TYPE ")
            fam, mtype = rest.split(" ", 1)
            assert fam not in seen_families, f"Interleaved/duplicate TYPE header for {fam}"
            seen_families.add(fam)
            types_by_name[fam] = mtype
            current_family = fam
            if mtype == "counter":
                assert fam.endswith("_total"), f"Counter {fam} must end with _total (promtool check metrics)"
            else:
                assert not fam.endswith("_total"), f"Non-counter {fam} ({mtype}) must not end with _total"
        elif not ln.startswith("#"):
            fam = ln.split("{", 1)[0].split(" ", 1)[0]
            assert fam == current_family, f"Sample {fam} interleaved outside family {current_family}"
