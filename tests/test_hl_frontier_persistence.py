from __future__ import annotations

from pathlib import Path

import pandas as pd

from hl_quant import hl_boros_fixed_funding as boros_mod
from hl_quant import hl_frontier_persistence as fp
from hl_quant import hl_hip3_builder_arb as hip3_mod
from hl_quant import hl_hyperevm_yield_stack as ys_mod


def _fake_snaps(
    ts_ms: int,
    nvda_apr: float,
    nvda_oracle_div: float,
    *,
    short_symbol: str = "xyz:NVDA",
    long_symbol: str = "io:NVDA",
    short_dex: str = "xyz",
    long_dex: str = "io",
):
    hip3 = hip3_mod.Hip3ScannerSnapshot(
        observed_at_ms=ts_ms,
        ny_session="NY_REGULAR",
        builders_discovered=10,
        active_markets_count=178,
        total_hip3_day_vol_usd=2_200_000_000.0,
        cross_builder_opps=[
            hip3_mod.Hip3CrossBuilderOpp(
                base_symbol="NVDA",
                short_symbol=short_symbol,
                long_symbol=long_symbol,
                short_dex=short_dex,
                long_dex=long_dex,
                short_apr_pct=nvda_apr + 5.0,
                long_apr_pct=5.0,
                spread_apr_pct=nvda_apr,
                mark_basis_bps=2.0,
                exec_basis_5k_bps=-4.0,
                l2_slip_5k_bps=1.5,
                l2_slip_25k_bps=3.2,
                depth_5k_ok=True,
                depth_25k_ok=True,
                payback_hours_5k=6.0,
                any_only_isolated=False,
                short_vol_usd=180_000_000.0,
                long_vol_usd=14_000_000.0,
                short_oi_usd=90_000_000.0,
                long_oi_usd=6_000_000.0,
                both_growth_mode=True,
                oracle_divergence_bps=nvda_oracle_div,
                oracle_risk_warning=nvda_oracle_div > 15.0,
                est_rt_maker_fee_bps=1.2,
                est_rt_taker_fee_bps=3.6,
                payback_hours_5k_taker=9.0,
            )
        ],
        top_tradfi_markets=[],
    )
    boros = boros_mod.BorosScannerSnapshot(
        observed_at_ms=ts_ms,
        total_markets_scanned=249,
        active_markets_count=39,
        hl_and_spread_count=1,
        markets=[
            boros_mod.BorosActiveMarket(
                market_id=83,
                name="Hyperliquid HYPE 25 Dec 2026",
                symbol="HL-HYPE-25DEC26",
                underlying="HYPE",
                is_spread_market=False,
                is_hyperliquid=True,
                maturity_ts=1_798_156_800,
                days_to_maturity=28.0,
                mark_apr_pct=9.11,
                mid_apr_pct=9.11,
                best_bid_apr_pct=8.94,
                best_ask_apr_pct=9.28,
                floating_apr_pct=10.95,
                boros_bid_ask_apr_pct=0.34,
                maturity_spread_cost_bps=3.4,
                fixed_minus_float_bid_pct=-2.01,
                float_minus_fixed_ask_pct=1.67,
                notional_oi=392_000.0,
                volume_24h=50_000.0,
                recommended_action="Lock Fixed Carry",
            )
        ],
    )
    ys = ys_mod.HyperEvmYieldSnapshot(
        inputs=ys_mod.HyperEvmYieldInputs(
            observed_at_ms=ts_ms,
            hype_mark_px=35.0,
            hype_funding_apr_pct=10.95,
            boros_hype_market_name="Hyperliquid HYPE 25 Dec 2026",
            boros_hype_fixed_bid_apr_pct=8.94,
            boros_hype_days_to_maturity=28.0,
            khype_apy_pct=1.88,
            khype_tvl_usd=550_000_000.0,
            sthype_apy_pct=1.99,
            sthype_tvl_usd=135_000_000.0,
            morpho_feusd_apy_pct=5.2,
            morpho_feusd_tvl_usd=13_400_000.0,
            hyperlend_khype_tvl_usd=158_000_000.0,
            felix_whype_tvl_usd=15_900_000.0,
            liminal_xhype_apy_pct=10.0,
            hlp_apr_pct=15.68,
            hlp_tvl_usd=152_600_000.0,
        ),
        strategies=[
            ys_mod.YieldStackStrategyRow(
                name="Composable Loop kHYPE + Boros",
                rate_type="Fixed (Boros)",
                gross_stacked_apr_pct=10.82,
                capital_efficiency_pct=100.0,
                net_deposit_apr_pct=9.07,
                spread_vs_hlp_pct=-6.61,
                perp_leverage=2.0,
                liq_buffer_pct=36.0,
                key_risks="LST depeg + borrow spike",
            )
        ],
    )
    return hip3, boros, ys


def test_frontier_persistence_appends_parquets_and_tracks_clean_streak(tmp_path: Path):
    t0 = 1_795_800_000_000
    h1, b1, y1 = _fake_snaps(t0, nvda_apr=45.0, nvda_oracle_div=12.0)
    r1 = fp.run_frontier_persistence_tick(data_root=tmp_path, now_ms=t0, hip3_snap=h1, boros_snap=b1, yield_snap=y1)
    assert r1["total_logged"] == {"hip3_rows": 1, "boros_rows": 1, "yield_rows": 1}
    assert r1["hip3_persistence"][0]["clean_streak"] == 1

    # Tick 2: APR still 55% AND clean oracle div 18 bps <= 25 bps -> clean_streak reaches 2!
    h2, b2, y2 = _fake_snaps(t0 + 4 * 3600_000, nvda_apr=55.0, nvda_oracle_div=18.0)
    r2 = fp.run_frontier_persistence_tick(
        data_root=tmp_path, now_ms=t0 + 4 * 3600_000, hip3_snap=h2, boros_snap=b2, yield_snap=y2
    )
    assert r2["total_logged"] == {"hip3_rows": 2, "boros_rows": 2, "yield_rows": 2}
    assert r2["hip3_persistence"][0]["raw_apr_streak"] == 2
    assert r2["hip3_persistence"][0]["clean_streak"] == 2
    assert r2["hip3_persistence"][0]["direction_flips"] == 0
    assert (tmp_path / "hl_hip3_arb" / "snapshot.json").is_file()
    assert (tmp_path / "hl_boros" / "snapshot.json").is_file()
    assert (tmp_path / "hl_hyperevm_yield_stack" / "snapshot.json").is_file()
    assert len(pd.read_parquet(tmp_path / "hl_frontier_history" / "hip3_history.parquet")) == 2

    # Tick 3: oracle blows out to 45 bps -> raw_apr_streak=3 but clean_streak resets to 0!
    h3, b3, y3 = _fake_snaps(t0 + 8 * 3600_000, nvda_apr=75.0, nvda_oracle_div=45.0)
    r3 = fp.run_frontier_persistence_tick(
        data_root=tmp_path, now_ms=t0 + 8 * 3600_000, hip3_snap=h3, boros_snap=b3, yield_snap=y3
    )
    assert r3["hip3_persistence"][0]["raw_apr_streak"] == 3
    assert r3["hip3_persistence"][0]["clean_streak"] == 0
    assert r3["hip3_persistence"][0]["direction_flips"] == 0
    md = fp.format_frontier_persistence_markdown(r3)
    assert "NVDA" in md and "⚡ GM" in md and "Перевор." in md


def test_frontier_persistence_resets_clean_streak_on_direction_flip(tmp_path: Path):
    t0 = 1_795_800_000_000
    # Ticks 1 & 2: steady S xyz:NVDA / L io:NVDA -> clean_streak=2, direction_flips=0
    h1, b1, y1 = _fake_snaps(t0, nvda_apr=45.0, nvda_oracle_div=8.0)
    fp.run_frontier_persistence_tick(data_root=tmp_path, now_ms=t0, hip3_snap=h1, boros_snap=b1, yield_snap=y1)

    h2, b2, y2 = _fake_snaps(t0 + 4 * 3600_000, nvda_apr=50.0, nvda_oracle_div=9.0)
    r2 = fp.run_frontier_persistence_tick(
        data_root=tmp_path, now_ms=t0 + 4 * 3600_000, hip3_snap=h2, boros_snap=b2, yield_snap=y2
    )
    assert r2["hip3_persistence"][0]["clean_streak"] == 2
    assert r2["hip3_persistence"][0]["direction_flips"] == 0

    # Tick 3: direction flips to S io:NVDA / L xyz:NVDA despite high APR & low oracle div
    h3, b3, y3 = _fake_snaps(
        t0 + 8 * 3600_000,
        nvda_apr=60.0,
        nvda_oracle_div=7.0,
        short_symbol="io:NVDA",
        long_symbol="xyz:NVDA",
        short_dex="io",
        long_dex="xyz",
    )
    r3 = fp.run_frontier_persistence_tick(
        data_root=tmp_path, now_ms=t0 + 8 * 3600_000, hip3_snap=h3, boros_snap=b3, yield_snap=y3
    )
    row3 = r3["hip3_persistence"][0]
    assert row3["raw_apr_streak"] == 3
    assert row3["clean_streak"] == 1
    assert row3["direction_flips"] == 1
    assert row3["short_symbol"] == "io:NVDA"
    assert row3["long_symbol"] == "xyz:NVDA"

    # Tick 4: flips back to S xyz:NVDA / L io:NVDA -> clean_streak remains 1, flips=2
    h4, b4, y4 = _fake_snaps(t0 + 12 * 3600_000, nvda_apr=42.0, nvda_oracle_div=6.0)
    r4 = fp.run_frontier_persistence_tick(
        data_root=tmp_path, now_ms=t0 + 12 * 3600_000, hip3_snap=h4, boros_snap=b4, yield_snap=y4
    )
    row4 = r4["hip3_persistence"][0]
    assert row4["raw_apr_streak"] == 4
    assert row4["clean_streak"] == 1
    assert row4["direction_flips"] == 2


def test_hip3_persistence_sorting_prefers_steady_over_flipped():
    df = pd.DataFrame(
        [
            # AAPL: steady direction across 2 ticks, lower APR (30%)
            {
                "ts_ms": 1,
                "base_symbol": "AAPL",
                "short_symbol": "xyz:AAPL",
                "long_symbol": "io:AAPL",
                "spread_apr_pct": 30.0,
                "oracle_divergence_bps": 4.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            {
                "ts_ms": 2,
                "base_symbol": "AAPL",
                "short_symbol": "xyz:AAPL",
                "long_symbol": "io:AAPL",
                "spread_apr_pct": 30.0,
                "oracle_divergence_bps": 4.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            # NVDA: flipped direction, higher APR (60%) -> clean_streak=1, flips=1
            {
                "ts_ms": 1,
                "base_symbol": "NVDA",
                "short_symbol": "xyz:NVDA",
                "long_symbol": "io:NVDA",
                "spread_apr_pct": 60.0,
                "oracle_divergence_bps": 5.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            {
                "ts_ms": 2,
                "base_symbol": "NVDA",
                "short_symbol": "io:NVDA",
                "long_symbol": "xyz:NVDA",
                "spread_apr_pct": 60.0,
                "oracle_divergence_bps": 5.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            # MSFT: single clean tick (clean_streak=1, flips=0, lower APR 25%) -> beats flipped NVDA on flips tie-breaker
            {
                "ts_ms": 2,
                "base_symbol": "MSFT",
                "short_symbol": "xyz:MSFT",
                "long_symbol": "io:MSFT",
                "spread_apr_pct": 25.0,
                "oracle_divergence_bps": 5.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
        ]
    )
    summary = fp.compute_hip3_persistence_summary(df)
    assert [r["base_symbol"] for r in summary] == ["AAPL", "MSFT", "NVDA"]
    assert summary[0]["clean_streak"] == 2 and summary[0]["direction_flips"] == 0
    assert summary[1]["clean_streak"] == 1 and summary[1]["direction_flips"] == 0
    assert summary[2]["clean_streak"] == 1 and summary[2]["direction_flips"] == 1


def test_hip3_persistence_breaks_streak_when_symbol_disappears():
    df = pd.DataFrame(
        [
            # STALE appeared in ticks 1 and 2 with huge APR, then vanished on tick 3
            {
                "ts_ms": 1,
                "base_symbol": "STALE",
                "short_symbol": "xyz:STALE",
                "long_symbol": "io:STALE",
                "spread_apr_pct": 120.0,
                "oracle_divergence_bps": 2.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            {
                "ts_ms": 2,
                "base_symbol": "STALE",
                "short_symbol": "xyz:STALE",
                "long_symbol": "io:STALE",
                "spread_apr_pct": 120.0,
                "oracle_divergence_bps": 2.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            # AAPL is present in tick 3 (and missed tick 2 -> gap breaks streak at 1)
            {
                "ts_ms": 1,
                "base_symbol": "AAPL",
                "short_symbol": "xyz:AAPL",
                "long_symbol": "io:AAPL",
                "spread_apr_pct": 30.0,
                "oracle_divergence_bps": 4.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
            {
                "ts_ms": 3,
                "base_symbol": "AAPL",
                "short_symbol": "xyz:AAPL",
                "long_symbol": "io:AAPL",
                "spread_apr_pct": 30.0,
                "oracle_divergence_bps": 4.0,
                "both_growth_mode": True,
                "any_only_isolated": False,
            },
        ]
    )
    summary = fp.compute_hip3_persistence_summary(df)
    assert [r["base_symbol"] for r in summary] == ["AAPL", "STALE"]
    aapl, stale = summary[0], summary[1]
    assert aapl["active_in_latest_tick"] is True and aapl["clean_streak"] == 1 and aapl["raw_apr_streak"] == 1
    assert stale["active_in_latest_tick"] is False and stale["clean_streak"] == 0 and stale["raw_apr_streak"] == 0
    assert stale["ticks_since_last_seen"] == 1
