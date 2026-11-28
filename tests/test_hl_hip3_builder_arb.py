from __future__ import annotations

from datetime import datetime, timezone

import pytest

from hl_quant import hl_hip3_builder_arb as hip3


def test_hip3_cross_builder_matching_l2_depth_and_ny_session_classification():
    markets = [
        hip3.Hip3BuilderMarket(
            dex="xyz",
            dex_full_name="XYZ",
            full_symbol="xyz:NVDA",
            base_symbol="NVDA",
            mark_px=151.10,
            oracle_px=150.90,
            basis_vs_oracle_bps=13.25,
            funding_rate_h=38.0 / (8760.0 * 100.0),
            funding_apr_pct=38.0,
            day_vol_usd=180_000_000.0,
            oi_usd=90_000_000.0,
            only_isolated=False,
            max_leverage=20,
        ),
        hip3.Hip3BuilderMarket(
            dex="io",
            dex_full_name="EntropyIO",
            full_symbol="io:NVDA",
            base_symbol="NVDA",
            mark_px=151.05,
            oracle_px=150.90,
            basis_vs_oracle_bps=9.94,
            funding_rate_h=5.5 / (8760.0 * 100.0),
            funding_apr_pct=5.5,
            day_vol_usd=14_500_000.0,
            oi_usd=6_000_000.0,
            only_isolated=True,
            max_leverage=10,
        ),
        # Price collision guard: 10x price mismatch must be skipped
        hip3.Hip3BuilderMarket(
            dex="para",
            dex_full_name="Paragon",
            full_symbol="para:FAKE",
            base_symbol="FAKE",
            mark_px=10.0,
            oracle_px=10.0,
            basis_vs_oracle_bps=0.0,
            funding_rate_h=0.0001,
            funding_apr_pct=87.6,
            day_vol_usd=1_000_000.0,
            oi_usd=500_000.0,
            only_isolated=False,
            max_leverage=5,
        ),
        hip3.Hip3BuilderMarket(
            dex="xyz",
            dex_full_name="XYZ",
            full_symbol="xyz:FAKE",
            base_symbol="FAKE",
            mark_px=100.0,
            oracle_px=100.0,
            basis_vs_oracle_bps=0.0,
            funding_rate_h=0.0,
            funding_apr_pct=0.0,
            day_vol_usd=1_000_000.0,
            oi_usd=500_000.0,
            only_isolated=False,
            max_leverage=5,
        ),
    ]

    def fake_book(sym: str):
        if sym == "xyz:NVDA":
            return ([(151.09, 500.0)], [(151.11, 500.0)])
        return ([(151.04, 500.0)], [(151.06, 500.0)])

    opps = hip3.scan_hip3_cross_builder_arb(markets, book_fetcher=fake_book)
    assert len(opps) == 1
    nvda = opps[0]
    assert nvda.base_symbol == "NVDA"
    assert nvda.short_symbol == "xyz:NVDA"
    assert nvda.long_symbol == "io:NVDA"
    assert nvda.spread_apr_pct == pytest.approx(32.5)
    assert nvda.any_only_isolated is True
    assert nvda.depth_5k_ok is True
    assert nvda.depth_25k_ok is True
    assert nvda.exec_basis_5k_bps is not None and nvda.exec_basis_5k_bps > 0.0
    assert nvda.payback_hours_5k is not None and nvda.payback_hours_5k < 30.0

    # Verify NY TradFi session classifier (Saturday UTC -> US_WEEKEND, Thursday 15:00 UTC -> NY_REGULAR)
    sat_ms = int(datetime(2026, 11, 28, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    thu_ms = int(datetime(2026, 11, 26, 15, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert hip3.classify_ny_tradfi_session(sat_ms) == "US_WEEKEND"
    assert hip3.classify_ny_tradfi_session(thu_ms) == "NY_REGULAR"

    snap = hip3.Hip3ScannerSnapshot(
        observed_at_ms=thu_ms,
        ny_session="NY_REGULAR",
        builders_discovered=3,
        active_markets_count=4,
        total_hip3_day_vol_usd=196_500_000.0,
        cross_builder_opps=opps,
        top_tradfi_markets=markets[:2],
    )
    md = hip3.format_hip3_arb_markdown(snap)
    rich = hip3.format_hip3_arb_telegram_rich_html(snap)
    assert "xyz:NVDA" in md
    assert "io:NVDA" in md
    assert '<table bordered striped>' in rich


def test_hip3_growth_mode_fee_schedule_and_oracle_divergence_guard():
    # Two builders with Growth Mode enabled AND > 15 bps oracle divergence -> triggers oracle_risk_warning
    m1 = hip3.Hip3BuilderMarket(
        dex="xyz",
        dex_full_name="XYZ",
        full_symbol="xyz:NVDA",
        base_symbol="NVDA",
        mark_px=151.10,
        oracle_px=151.00,
        basis_vs_oracle_bps=6.62,
        funding_rate_h=40.0 / (8760.0 * 100.0),
        funding_apr_pct=40.0,
        day_vol_usd=100_000_000.0,
        oi_usd=50_000_000.0,
        only_isolated=False,
        max_leverage=20,
        growth_mode=True,
        deployer_fee_scale=1.0,
        est_taker_fee_bps=0.90,
        est_maker_fee_bps=0.30,
    )
    m2 = hip3.Hip3BuilderMarket(
        dex="io",
        dex_full_name="EntropyIO",
        full_symbol="io:NVDA",
        base_symbol="NVDA",
        mark_px=151.35,
        oracle_px=151.40,  # ~26.4 bps away from xyz oracle 151.00!
        basis_vs_oracle_bps=-3.30,
        funding_rate_h=5.0 / (8760.0 * 100.0),
        funding_apr_pct=5.0,
        day_vol_usd=10_000_000.0,
        oi_usd=5_000_000.0,
        only_isolated=False,
        max_leverage=10,
        growth_mode=True,
        deployer_fee_scale=1.0,
        est_taker_fee_bps=0.90,
        est_maker_fee_bps=0.30,
    )

    def fake_book(sym: str):
        px = 151.10 if sym == "xyz:NVDA" else 151.35
        return ([(px - 0.01, 500.0)], [(px + 0.01, 500.0)])

    opps = hip3.scan_hip3_cross_builder_arb([m1, m2], book_fetcher=fake_book)
    assert len(opps) == 1
    opp = opps[0]
    assert opp.both_growth_mode is True
    assert opp.est_rt_maker_fee_bps == pytest.approx(1.20)
    assert opp.est_rt_taker_fee_bps == pytest.approx(3.60)
    assert opp.oracle_divergence_bps > 20.0
    assert opp.oracle_risk_warning is True
    assert opp.payback_hours_5k_taker is not None and opp.payback_hours_5k_taker > 0.0
    assert opp.independent_dex_margin is True
    # Return on split margin at 2x per leg = spread * 2 / 2 (was understated 2x as spread * 0.5).
    assert opp.net_apr_split_margin_pct == pytest.approx(35.0)

    # 30-day Growth Mode cooldown parser handles nanosecond ISO timestamps cleanly
    now_ms = int(datetime(2026, 11, 26, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)
    assert hip3.compute_growth_mode_lock_days_left("2026-11-16T12:00:00.123456789", now_ms=now_ms) == pytest.approx(20.0, abs=0.1)
    assert hip3.compute_growth_mode_lock_days_left("2025-11-23T17:37:10.033211662", now_ms=now_ms) == pytest.approx(0.0)
