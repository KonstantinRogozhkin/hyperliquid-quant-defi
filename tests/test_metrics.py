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
    assert 'hl_worker_job_ok{job="hip3_paper"} 1' in text
    assert 'hl_worker_job_failed{job="onchain_events"} 1' in text
    assert "hl_testnet_equity_usd 1004.16" in text
    assert "hl_testnet_last_cycle_skipped_unquoted 2" in text
    assert "hl_hip3_paper_equity_usd 49996.135" in text
    assert 'hl_hip3_paper_pair_live_apr_pct{symbol="NVDA",short_dex="io",long_dex="xyz",margin_mode="cross"} 41.26' in text
    assert 'hl_cross_dex_paper_pair_live_apr_pct{coin="NEAR",other_dex="extended"} 37.77' in text
    assert "hl_hlp_tvl_usd 152279944.79" in text
    assert 'hl_mm_calibration_kappa{coin="ETH"} 0.562' in text
    # Every emitted sample value must be a finite number (never NaN/Inf)
    import math

    sample_lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(sample_lines) >= 25
    for ln in sample_lines:
        val = float(ln.rsplit(" ", 1)[1])
        assert math.isfinite(val), ln
