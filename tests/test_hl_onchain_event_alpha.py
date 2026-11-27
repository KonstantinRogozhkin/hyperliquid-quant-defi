from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from hl_quant import hl_hyperevm_defi as evm
from hl_quant import hl_liquidation_and_funding_alpha as liq
from hl_quant import hl_onchain_event_alpha as oea


def test_h2_h4_h5_event_extraction_alerting_and_forward_outcome_grading(tmp_path: Path):
    t0 = 1_795_800_000_000
    cfg = oea.OnChainEventConfig(
        h2_alert_wall_dist_pct=2.5,
        h4_alert_peg_spread_bps=10.0,
        h5_alert_skew_usd=3_000_000.0,
        h5_alert_tvl_share_pct=2.0,
    )
    clusters = [
        liq.CoinLiquidationCluster(
            coin="BTC",
            mark_px=100_000.0,
            whales_count=5,
            long_liq_0_2_usd=3_000_000.0,
            long_liq_2_5_usd=1_500_000.0,
            long_liq_5_10_usd=3_500_000.0,
            short_liq_0_2_usd=0.0,
            short_liq_2_5_usd=500_000.0,
            short_liq_5_10_usd=1_500_000.0,
            total_0_10_usd=10_000_000.0,
            nearest_long_liq_px=98_200.0,
            nearest_long_dist_pct=1.8,
            nearest_short_liq_px=107_500.0,
            nearest_short_dist_pct=7.5,
            cascade_bias="Downside Cascade Magnet",
        )
    ]
    liq_snap = liq.LiquidationAndFundingAlphaSnapshot(
        observed_at_ms=t0,
        whales_scanned=45,
        whales_with_positions=30,
        liq_positions_count=10,
        clusters=clusters,
        funding_micro=[],
    )
    evm_snap = evm.HyperEVMDefiSnapshot(
        observed_at_ms=t0,
        chain=evm.HyperEVMChainHealth(
            chain_id=999,
            block_number=52_000_000,
            gas_price_gwei=0.1,
            rpc_latency_ms=80.0,
            l1_block_number=1_235_000_000,
            usdt0_per_usdc=1.0011,
            usdt0_peg_discount_bps=11.0,
            stable_peg_source="PRJX V3 USDC/USDT0 1bp",
        ),
        evm_arb=[
            evm.EvmPoolArbOpportunity(
                dex="PRJX V3",
                label="WHYPE/USDC 5bp",
                pool_address="0x6c9a",
                evm_price=35.50,
                core_ref_price=35.40,
                gross_spread_bps=28.25,
                peg_adjusted_spread_bps=28.25,
                pool_fee_bps=5.0,
                core_fee_bps=4.5,
                stable_leg_fee_bps=0.0,
                net_arb_bps=18.75,
                direction="Buy Core → Sell EVM",
                active_liquidity_raw=10**18,
                virtual_depth_usd=50_000_000.0,
                slippage_5k_bps=1.0,
                slippage_25k_bps=4.5,
                slippage_100k_bps=18.0,
                net_arb_25k_bps=14.25,
                executable=True,
            )
        ],
        l1_read_oracles=[],
        spot_perp_basis=[],
        vaults=[],
        extreme_funding=[],
    )
    hlp_children = [
        {
            "marginSummary": {"accountValue": "150000000.0"},
            "assetPositions": [
                {"position": {"coin": "ETH", "szi": "-2000.0", "positionValue": "6000000.0", "unrealizedPnl": "120000.0"}},
                {"position": {"coin": "SOL", "szi": "4000.0", "positionValue": "600000.0", "unrealizedPnl": "-10000.0"}},
            ],
        }
    ]
    perps_raw_1 = {
        "BTC": {"mark_px": 100_000.0, "mid_px": 100_010.0, "oracle_px": 100_000.0, "day_vol_usd": 5_000_000_000.0},
        "HYPE": {"mark_px": 30.00, "mid_px": 30.20, "oracle_px": 30.00, "day_vol_usd": 300_000_000.0},  # +66.7 bps BOOK_STRESS!
    }

    # Tick 1: logs all four hypotheses (H2/H4/H5/H6) and fires alerts on BTC (H2), WHYPE/USDC (H4), ETH (H5), HYPE (H6)
    r1 = oea.run_onchain_event_tick(
        state_dir=tmp_path,
        cfg=cfg,
        now_ms=t0,
        liq_snap=liq_snap,
        evm_snap=evm_snap,
        hlp_child_states=hlp_children,
        hlp_tvl_usd=150_000_000.0,
        mark_prices={"BTC": 100_000.0, "ETH": 3_000.0, "SOL": 150.0, "HYPE": 30.0},
        perps_raw=perps_raw_1,
    )
    assert r1["any_alert"] is True
    assert r1["alerts_count"] == {"h2": 1, "h4": 1, "h5": 1, "h6": 1}
    assert r1["h5_events"][0]["coin"] == "ETH"
    assert r1["h5_events"][0]["hlp_side"] == "HLP_SHORT"
    assert r1["h6_events"][0]["coin"] == "HYPE"
    assert r1["h6_events"][0]["stress_type"] == "CRASH_BOOK_STRESS"
    assert r1["hlp_mm_pnl"]["mm_pnl_regime"] == "CALM"

    # Tick 2 (+20 minutes): BTC dumps -100 bps while HLP equity JUMPS +$400k -> MECHANICAL_CASCADE_CANDIDATE + grades H2/H5/H6!
    hlp_children_2 = [
        {
            "marginSummary": {"accountValue": "150400000.0"},
            "assetPositions": hlp_children[0]["assetPositions"],
        }
    ]
    r2 = oea.run_onchain_event_tick(
        state_dir=tmp_path,
        cfg=cfg,
        now_ms=t0 + 20 * 60_000,
        liq_snap=liq_snap,
        evm_snap=evm_snap,
        hlp_child_states=hlp_children_2,
        hlp_tvl_usd=150_400_000.0,
        mark_prices={"BTC": 99_000.0, "ETH": 3_015.0, "SOL": 150.0, "HYPE": 30.30},
        perps_raw=perps_raw_1,
    )
    assert r2["graded_outcomes"]["h2"] == 1
    assert r2["graded_outcomes"]["h5"] == 2
    assert r2["graded_outcomes"]["h6"] == 1
    assert r2["hlp_mm_pnl"]["mm_pnl_regime"] == "MECHANICAL_CASCADE_CANDIDATE"
    assert r2["hlp_mm_pnl"]["hlp_pnl_delta_usd"] == pytest.approx(400_000.0)
    h2_df = pd.read_parquet(tmp_path / "h2_liq_events.parquet")
    assert h2_df.iloc[0]["ret_15m_bps"] == pytest.approx(-100.0, abs=0.1)
    h5_df = pd.read_parquet(tmp_path / "h5_hlp_skew.parquet")
    eth_row = h5_df[(h5_df["ts_ms"] == t0) & (h5_df["coin"] == "ETH")].iloc[0]
    assert eth_row["ret_15m_bps"] == pytest.approx(50.0, abs=0.1)
    h6_df = pd.read_parquet(tmp_path / "h6_mark_oracle_disloc.parquet")
    assert h6_df.iloc[0]["ret_15m_bps"] == pytest.approx(100.0, abs=0.1)

    md = oea.format_onchain_events_markdown(r2)
    rich = oea.format_onchain_events_telegram_rich_html(r2)
    assert "H2 — Стенки ликвидаций" in md
    assert "H5 — Перекос инвентаря HLP" in md
    assert "H6 — Стресс-дислокации" in md
    assert "MECHANICAL_CASCADE_CANDIDATE" in md
    assert '<table bordered striped>' in rich


def test_alert_cooldown_suppresses_repeats_and_fires_on_escalation_or_expiry():
    t0 = 1_795_800_000_000
    # 1. First sighting of INIT at 105 bps -> fires immediately
    new1, sup1, st1 = oea.evaluate_alert_cooldown({"H6:INIT": 105.0}, {}, now_ms=t0, cooldown_sec=4 * 3600, escalation_mult=1.5)
    assert new1 == ["H6:INIT"] and sup1 == []

    # 2. 15 minutes later, INIT still at 110 bps (< 1.5x) -> suppressed!
    new2, sup2, st2 = oea.evaluate_alert_cooldown({"H6:INIT": 110.0}, st1, now_ms=t0 + 15 * 60_000, cooldown_sec=4 * 3600, escalation_mult=1.5)
    assert new2 == [] and sup2 == ["H6:INIT"]

    # 3. 30 minutes later, INIT escalates to 170 bps (>= 1.5x of 105) -> bypasses cooldown and fires!
    new3, sup3, st3 = oea.evaluate_alert_cooldown({"H6:INIT": 170.0}, st2, now_ms=t0 + 30 * 60_000, cooldown_sec=4 * 3600, escalation_mult=1.5)
    assert new3 == ["H6:INIT"] and sup3 == []

    # 4. 4.5 hours later, same 170 bps -> cooldown expired -> fires again!
    new4, sup4, _ = oea.evaluate_alert_cooldown({"H6:INIT": 170.0}, st3, now_ms=t0 + 5 * 3600 * 1000, cooldown_sec=4 * 3600, escalation_mult=1.5)
    assert new4 == ["H6:INIT"] and sup4 == []


def test_h6_separates_crowded_funding_basis_from_crash_and_hlp_pnl_ignores_lp_deposits():
    cfg = oea.OnChainEventConfig()
    perps = {
        # INIT: -108 bps mark vs oracle, BUT -494% funding APR & calm BookMid -> CROWDED_FUNDING_BASIS (no crash alert!)
        "INIT": {
            "mark_px": 0.500,
            "mid_px": 0.5001,
            "oracle_px": 0.5054,
            "funding_hourly": -0.000564,
            "day_vol_usd": 6_000_000.0,
        },
        # ETH: -180 bps BookMid vs Mark -> acute CRASH_BOOK_STRESS (must alert!)
        "ETH": {
            "mark_px": 2000.0,
            "mid_px": 1964.0,
            "oracle_px": 2001.0,
            "funding_hourly": 0.00001,
            "day_vol_usd": 2_000_000_000.0,
        },
        # SOL: -70 bps Mark vs Oracle under normal +10% funding -> ORACLE_DECOUPLING (must alert!)
        "SOL": {
            "mark_px": 100.0,
            "mid_px": 100.02,
            "oracle_px": 100.70,
            "funding_hourly": 0.000011,
            "day_vol_usd": 500_000_000.0,
        },
    }
    rows = oea.extract_h6_book_mark_oracle_dislocations(perps, ts_ms=1_795_800_000_000, cfg=cfg)
    by_coin = {r["coin"]: r for r in rows}
    assert by_coin["ETH"]["stress_type"] == "CRASH_BOOK_STRESS" and by_coin["ETH"]["alert"] is True
    assert by_coin["SOL"]["stress_type"] == "ORACLE_DECOUPLING" and by_coin["SOL"]["alert"] is True
    assert by_coin["INIT"]["stress_type"] == "CROWDED_FUNDING_BASIS" and by_coin["INIT"]["alert"] is False

    # Deposit-clean HLP PnL: LP deposits +$5M into HLP (child equity jumps +$5M) while BTC dumps -100 bps,
    # BUT deposit-clean allTime.pnlHistory moved only +$12k -> regime MUST stay CALM (no false cascade signal)!
    prev = {
        "hlp_mm_pnl": {
            "hlp_cum_pnl_usd": 141_000_000.0,
            "hlp_child_account_value_usd": 39_000_000.0,
            "btc_mark_px": 60_000.0,
        }
    }
    children_after_deposit = [{"marginSummary": {"accountValue": "44000000.0"}, "assetPositions": []}]
    mm_res = oea.summarize_hlp_mm_pnl_regime(
        children_after_deposit,
        mark_prices={"BTC": 59_400.0},
        prev_snapshot=prev,
        hlp_cum_pnl_usd=141_012_000.0,
    )
    assert mm_res["pnl_source"] == "vault_pnlHistory_deposit_clean"
    assert mm_res["hlp_pnl_delta_usd"] == pytest.approx(12_000.0)
    assert mm_res["mm_pnl_regime"] == "CALM"


def test_fetch_hlp_vault_telemetry_retries_transient_429_without_dropping_children(monkeypatch):
    import json

    import httpx

    monkeypatch.setattr(oea.time, "sleep", lambda _s: None)
    calls: dict[str, int] = {}

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        key = f"{body.get('type')}:{body.get('user') or body.get('vaultAddress')}"
        calls[key] = calls.get(key, 0) + 1
        if calls[key] == 1:
            return httpx.Response(429, json={"error": "rate_limited"})
        if body.get("type") == "vaultDetails":
            return httpx.Response(
                200,
                json={
                    "portfolio": [["allTime", {"accountValueHistory": [[1, "150000000.0"]], "pnlHistory": [[1, "141000000.0"]]}]],
                    "relationship": {"data": {"childAddresses": ["0xchild1"]}},
                },
            )
        return httpx.Response(200, json={"marginSummary": {"accountValue": "40000000.0"}, "assetPositions": []})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        states, tvl, cum_pnl = oea.fetch_hlp_vault_telemetry(client, vault_address="0xhlp")
    assert tvl == pytest.approx(150_000_000.0)
    assert cum_pnl == pytest.approx(141_000_000.0)
    assert len(states) == 1
    assert calls["vaultDetails:0xhlp"] == 2
    assert calls["clearinghouseState:0xchild1"] == 2
