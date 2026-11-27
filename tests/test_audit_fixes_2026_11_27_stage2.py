"""Regression tests for Stage-2 2026-11-27 comprehensive audit fixes."""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import cex_perp_public as cex
from hl_quant import funding_arb as fa
from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import hl_hip3_paper as hp
from hl_quant import hl_hyperevm_defi as defi
from hl_quant import hl_liquidation_and_funding_alpha as lfa
from hl_quant import hl_onchain_event_alpha as oea
from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_spot_perp_arb as spa
from hl_quant import hl_testnet_executor as ex
from hl_quant import metrics as prom_metrics
from hl_quant import paper_tracker as pt

H = fa.HOUR_MS
T0 = 1_795_000_000_000 - (1_795_000_000_000 % H)


class _DummyCexClient:
    def __init__(self, payload):
        self._payload = payload

    def get(self, path: str, params: dict | None = None):
        return self._payload


def test_cex_fetch_live_mid_prices_all_venues():
    bn = _DummyCexClient(
        [
            {"symbol": "BTCUSDT", "bidPrice": "90000.0", "askPrice": "90010.0"},
            {"symbol": "1000PEPEUSDT", "bidPrice": "0.0120", "askPrice": "0.0122"},
            {"symbol": "OTHERUSDT", "bidPrice": "1.0", "askPrice": "1.2"},
        ]
    )
    by = _DummyCexClient(
        {
            "retCode": 0,
            "result": {
                "list": [
                    {"symbol": "BTCUSDT", "bid1Price": "90002.0", "ask1Price": "90008.0"},
                    {"symbol": "ETHUSDT", "bid1Price": "", "ask1Price": "", "markPrice": "3100.5"},
                ]
            },
        }
    )
    ok = _DummyCexClient(
        {
            "code": "0",
            "data": [
                {"instId": "BTC-USDT-SWAP", "bidPx": "89999.0", "askPx": "90001.0"},
                {"instId": "PEPE-USDT-SWAP", "bidPx": "0.0000120", "askPx": "0.0000122"},
            ],
        }
    )
    assert cex.fetch_live_mid_prices(bn, "binance", {"BTCUSDT", "1000PEPEUSDT"}) == {
        "BTCUSDT": pytest.approx(90005.0),
        "1000PEPEUSDT": pytest.approx(0.0121),
    }
    assert cex.fetch_live_mid_prices(by, "bybit", {"BTCUSDT", "ETHUSDT"}) == {
        "BTCUSDT": pytest.approx(90005.0),
        "ETHUSDT": pytest.approx(3100.5),
    }
    assert cex.fetch_live_mid_prices(ok, "okx", {"BTC-USDT-SWAP"}) == {
        "BTC-USDT-SWAP": pytest.approx(90000.0),
    }


def _two_coin_market(n_hours: int = 40) -> af.WideMarket:
    hours = T0 + np.arange(n_hours, dtype="int64") * H
    rows = []
    for coin in ("BTC", "ETH"):
        px = np.full(n_hours, 100.0)
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours,
                    "coin": coin,
                    "hl_close": px,
                    "hl_ret": np.zeros(n_hours),
                    "hl_notional_usd": np.full(n_hours, 1e6),
                    "hl_fund_next": np.zeros(n_hours),
                    "hl_rate_h": np.zeros(n_hours),
                    "bybit_close": px,
                    "bybit_ret": np.zeros(n_hours),
                    "bybit_notional_usd": np.full(n_hours, 1e6),
                    "bybit_fund_next": np.zeros(n_hours),
                    "bybit_rate_h": np.zeros(n_hours),
                    "binance_close": px,
                    "binance_ret": np.zeros(n_hours),
                    "binance_notional_usd": np.full(n_hours, 1e6),
                    "binance_fund_next": np.zeros(n_hours),
                    "binance_rate_h": np.zeros(n_hours),
                    "okx_close": px,
                    "okx_ret": np.zeros(n_hours),
                    "okx_notional_usd": np.full(n_hours, 1e6),
                    "okx_fund_next": np.zeros(n_hours),
                    "okx_rate_h": np.zeros(n_hours),
                }
            )
        )
    return af.build_wide_market(pd.concat(rows, ignore_index=True))


def test_metamodel_live_cex_marks_deadband_and_nav_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    m1 = _two_coin_market(40)
    quotes_1 = {
        "BTC": pt.L2Quote("BTC", 99.95, 100.05, 100.0, 10.0, 1e6, 1e6),
        "ETH": pt.L2Quote("ETH", 99.95, 100.05, 100.0, 10.0, 1e6, 1e6),
    }
    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda m, s: {
            "BTC": {"hl_weight": -0.20, "cex_legs": {"bybit": 0.20}, "cex_weight": 0.20, "cex_venue": "bybit"},
            "ETH": {"hl_weight": 0.15, "cex_legs": {"okx": -0.15}, "cex_weight": -0.15, "cex_venue": "okx"},
        },
    )
    r1 = pt.run_paper_tick(
        m1,
        quotes_1,
        state_dir=tmp_path,
        sleeves=[],
        cex_mid_prices={"bybit": {"BTC": 100.0}, "okx": {"ETH": 100.0}},
        rebalance_deadband_weight=0.02,
    )
    assert r1["status"] == "ok"

    # Tick 2: HL and live CEX both rally +3% together while `:00` kline closes stay at 100.0!
    # Without live CEX mids, -0.20 HL short + 0.05 net would fabricate a directional MTM loss/gain;
    # WITH synchronous `cex_mid_prices`, delta-neutral price_pnl_usd is ~0.0!
    # Also nudge BTC target by 0.008 (< 0.02 deadband) and close ETH (0.0 -> must NOT be blocked by deadband).
    m2 = _two_coin_market(41)
    quotes_2 = {
        "BTC": pt.L2Quote("BTC", 102.95, 103.05, 103.0, 10.0, 1e6, 1e6),
        "ETH": pt.L2Quote("ETH", 102.95, 103.05, 103.0, 10.0, 1e6, 1e6),
    }
    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda m, s: {
            "BTC": {"hl_weight": -0.208, "cex_legs": {"bybit": 0.208}, "cex_weight": 0.208, "cex_venue": "bybit"},
        },
    )
    r2 = pt.run_paper_tick(
        m2,
        quotes_2,
        state_dir=tmp_path,
        sleeves=[],
        cex_mid_prices={"bybit": {"BTC": 103.0}, "okx": {"ETH": 103.0}},
        rebalance_deadband_weight=0.02,
    )
    assert r2["price_pnl_usd"] == pytest.approx(0.0, abs=1e-6)
    # BTC micro-drift (-0.20 -> -0.208) suppressed; ETH full exit (0.15 -> 0.0) executed -> 1 trade, turnover=0.30!
    assert r2["trades_count"] == 1
    assert r2["turnover"] == pytest.approx(0.30, abs=1e-6)
    st2 = json.loads((tmp_path / "state.json").read_text())
    assert st2["positions"]["BTC"]["hl_weight"] == pytest.approx(-0.20)
    assert "ETH" not in st2["positions"]

    # Tick 3: Corrupt HL quote spikes +60% while CEX stays at 103 -> >8% NAV jump triggers Bounded NAV Sanity Guard!
    m3 = _two_coin_market(42)
    quotes_3 = {
        "BTC": pt.L2Quote("BTC", 164.0, 165.0, 164.5, 10.0, 1e6, 1e6),
        "ETH": pt.L2Quote("ETH", 102.95, 103.05, 103.0, 10.0, 1e6, 1e6),
    }
    r3 = pt.run_paper_tick(
        m3,
        quotes_3,
        state_dir=tmp_path,
        sleeves=[],
        cex_mid_prices={"bybit": {"BTC": 103.0}},
        max_tick_nav_jump_pct=8.0,
    )
    assert r3["nav_guard_triggered"] is True
    assert r3["equity_usd"] == pytest.approx(r2["equity_usd"])
    st3 = json.loads((tmp_path / "state.json").read_text())
    assert st3["nav_guard_triggers"] == 1


def test_spot_perp_settled_funding_real_mmr_and_liquidation_nav(tmp_path: Path):
    t0 = T0 + 5 * 60_000  # 00:05 UTC (strictly 5m past hour boundary)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t = body.get("type")
        if t == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40, "onlyIsolated": False}]},
                    [
                        {
                            "markPx": "100000.0",
                            "midPx": "100000.0",
                            "oraclePx": "100000.0",
                            "funding": "0.0005",  # +438% APR instantaneous at snapshot time
                            "openInterest": "1000.0",
                            "dayNtlVlm": "500000000.0",
                        }
                    ],
                ],
            )
        if t == "spotMetaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {
                        "tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "UBTC"}],
                        "universe": [{"name": "@1", "tokens": [1, 0]}],
                    },
                    [{"coin": "@1", "midPx": "99980.0", "markPx": "99980.0", "dayNtlVlm": "50000000.0"}],
                ],
            )
        if t == "fundingHistory":
            # Realized 4 hourly settlements averaged 0.0001/h (total = 0.0004), NOT 4 * 0.0005 = 0.0020!
            start_t = int(body["startTime"])
            end_t = int(body["endTime"])
            events = [
                {"coin": "BTC", "fundingRate": "0.0001", "time": (t0 // H + k) * H}
                for k in (1, 2, 3, 4)
            ]
            return httpx.Response(200, json=[e for e in events if start_t <= e["time"] <= end_t])
        return httpx.Response(200, json={})

    book = lambda _sym: ([(99_979.0, 10.0)], [(99_981.0, 10.0)])
    cfg = spa.SpotPerpPaperConfig(min_clean_streak=1)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r1 = spa.run_spot_perp_paper_tick(
            client, state_dir=tmp_path, cfg=cfg, now_ms=t0, book_fetcher=book, hlp_summary=(10.0, 1e8)
        )
        pos1 = r1["state"]["positions"]["BTC"]
        assert pos1["max_leverage"] == 40
        # 40x max leverage -> MMR = 0.5 / 40 = 1.25% (not hardcoded 5.0%!)
        expected_liq_dist = (pos1["perp_margin_usd"] / pos1["perp_notional_usd"] - 0.0125) * 100.0
        assert pos1["perp_liq_distance_pct"] == pytest.approx(expected_liq_dist, abs=0.05)
        assert r1["state"]["liquidation_equity_usd"] < r1["state"]["equity_usd"]

        # Sub-hour re-run at 00:25 UTC (zero `:00` boundaries crossed) -> 0 settled funding!
        r_sub = spa.run_spot_perp_paper_tick(
            client, state_dir=tmp_path, cfg=cfg, now_ms=t0 + 20 * 60_000, book_fetcher=book, hlp_summary=(10.0, 1e8)
        )
        assert r_sub["state"]["cum_funding_usd"] == pytest.approx(0.0, abs=1e-6)

        # 4 hours later at 04:05 UTC (4 hourly `:00` settlements crossed -> sum = 0.0004)
        r2 = spa.run_spot_perp_paper_tick(
            client, state_dir=tmp_path, cfg=cfg, now_ms=t0 + 4 * H, book_fetcher=book, hlp_summary=(10.0, 1e8)
        )
        perp_ntl = r2["state"]["positions"]["BTC"]["perp_notional_usd"]
        assert r2["state"]["cum_funding_usd"] == pytest.approx(perp_ntl * 0.0004, rel=1e-3)


def test_hip3_paper_tracks_exit_cost_and_liquidation_equity(tmp_path: Path):
    t0 = 1_796_000_000_000
    mkts = [
        hip3.Hip3BuilderMarket(
            dex="xyz",
            dex_full_name="XYZ",
            full_symbol="xyz:NVDA",
            base_symbol="NVDA",
            mark_px=140.0,
            oracle_px=140.0,
            basis_vs_oracle_bps=0.0,
            funding_rate_h=0.0004,
            funding_apr_pct=350.4,
            day_vol_usd=20_000_000.0,
            oi_usd=10_000_000.0,
            only_isolated=False,
            max_leverage=10,
            growth_mode=True,
            est_taker_fee_bps=0.9,
            est_maker_fee_bps=0.3,
        ),
        hip3.Hip3BuilderMarket(
            dex="flx",
            dex_full_name="FLX",
            full_symbol="flx:NVDA",
            base_symbol="NVDA",
            mark_px=139.9,
            oracle_px=140.0,
            basis_vs_oracle_bps=-7.14,
            funding_rate_h=0.0,
            funding_apr_pct=0.0,
            day_vol_usd=20_000_000.0,
            oi_usd=10_000_000.0,
            only_isolated=False,
            max_leverage=10,
            growth_mode=True,
            est_taker_fee_bps=0.9,
            est_maker_fee_bps=0.3,
        ),
    ]
    book = lambda sym: ([(140.0, 1000.0)], [(140.01, 1000.0)]) if sym.startswith("xyz:") else ([(139.89, 1000.0)], [(139.90, 1000.0)])
    cfg = hp.Hip3PaperConfig(min_clean_streak=1)
    res = hp.run_hip3_paper_tick(state_dir=tmp_path, cfg=cfg, now_ms=t0, markets=mkts, book_fetcher=book)
    st = res["state"]
    pos = st["positions"]["NVDA"]
    assert pos["est_exit_cost_usd"] > 0.0
    assert pos["liquidation_net_pnl_usd"] == pytest.approx(pos["net_pnl_usd"] - pos["est_exit_cost_usd"], abs=1e-3)
    assert st["liquidation_equity_usd"] == pytest.approx(st["equity_usd"] - pos["est_exit_cost_usd"], abs=1e-3)


def test_analytics_consecutive_day_gap_guard_and_autopilot_look_through(tmp_path: Path):
    # BTC rises +1% every day for 10 consecutive days, except on day 6 it drops -10%.
    dates_all = pd.date_range("2026-09-01", periods=10, freq="D").strftime("%Y-%m-%d").tolist()
    btc_vals = [100.0, 101.0, 102.0, 103.0, 104.0, 105.0, 94.5, 95.5, 96.5, 97.5]
    btc_ser = pd.Series(btc_vals, index=dates_all)

    # Market-neutral strategy grows steadily +0.05%/day, EXCEPT days 2026-09-04..09-06 were missed (worker outage),
    # so 2026-09-07 has a 4-day cumulative +0.20% gain right on the day BTC fell -10%!
    strat_idx = [d for d in dates_all if d not in {"2026-09-04", "2026-09-05", "2026-09-06"}]
    strat_vals = [1.0 * (1.0005 ** i) for i in (0, 1, 2, 6, 7, 8, 9)]
    strat_ser = pd.Series(strat_vals, index=strat_idx)

    ols = ar.compute_ols_market_exposure({"sp": ("Spot+Perp", strat_ser)}, btc_ser, factor_name="BTC")
    row = ols["rows"][0]
    # 2026-09-07 is excluded because 09-03 -> 09-07 is a 4-day gap, leaving 5 true consecutive 1-day returns with 0 variance -> beta=0!
    assert row["days"] == 5
    assert row["beta"] == pytest.approx(0.0, abs=1e-4)

    # Verify Autopilot look-through routes idle Cross-DEX & Metamodel cash to Cash / USDC and sums mm nav_guard_triggers
    for sub, payload in (
        (
            "hl_autopilot",
            {
                "sleeve_allocations_usd": {"cross_dex_paper": 40_000.0, "metamodel_paper": 60_000.0},
                "risk_telemetry": {"venue_breakdown_usd": {"HyperCore": 100_000.0}},
            },
        ),
        ("hl_cross_dex_paper", {"equity_usd": 100_000.0, "positions": {"NEAR": {"leg_notional_usd": 25_000.0}}}),
        ("hl_metamodel", {"equity_usd": 100_000.0, "nav_guard_triggers": 2, "positions": {"BTC": {"hl_weight": 0.25, "cex_legs": {"bybit": -0.25}}}}),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps(payload), encoding="utf-8")

    rx = ar.build_consolidated_risk_exposure(tmp_path)
    assert rx["total_nav_guard_triggers"] == 2
    tiers = {r["label"]: r["usd"] for r in rx["asset_tier_concentration"]}
    # Cross-DEX: 25k locked of 100k = 25% of 40k = $10k deployed, $30k cash.
    # Metamodel: 0.50 gross / 2 = 25% of 60k = $15k deployed, $45k cash. Total Cash / USDC = $75k!
    assert tiers["Crypto - Cross-DEX Neutral"] == pytest.approx(10_000.0)
    assert tiers["Crypto - Multi-Factor Neutral"] == pytest.approx(15_000.0)
    assert tiers["Cash / USDC"] == pytest.approx(75_000.0)


def test_testnet_executor_raises_under_levered_cross_position_and_non_circular_deposited_return(tmp_path: Path):
    now = 1_796_000_000_000
    acct = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=900.0,
        positions_sz={"BTC": 0.001},
        open_orders=[],
        observed_at_ms=now,
        position_leverage={"BTC": ("cross", 1)},  # Stuck at 1x while default_cross_leverage=3!
    )
    meta = {"BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40)}
    bbo = {"BTC": (90_000.0, 90_002.0, now)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct,
        {"BTC": 0.25},
        meta,
        bbo,
        adapter,
        limits=ex.RiskLimits(default_cross_leverage=3),
        now_ms=now,
    )
    assert res["status"] == "ok"
    assert adapter.leverage_updates == [{"mode": "dry_run", "status": "ok", "coin": "BTC", "leverage": 3, "is_cross": True}]

    # Non-circular Testnet return from userNonFundingLedgerUpdates
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "state.json").write_text(json.dumps({"starting_capital_usd": 100_000.0}), encoding="utf-8")
    pd.DataFrame({"tick_hour_ms": [now], "equity_usd": [100_500.0]}).to_parquet(paper_dir / "ticks.parquet", index=False)

    snap = {
        "account_address": "0x6b9c000000000000000000000000000000000001",
        "observed_at_ms": now,
        "user_abstraction": "unifiedAccount",
        "ledger_updates": [
            {"time": now - 10_000, "delta": {"type": "internalTransfer", "token": "USDC", "usdc": "1000.0", "fee": "1.0", "user": "0xfaucet", "destination": "0x6b9c000000000000000000000000000000000001"}},
            {"time": now - 5_000, "delta": {"type": "accountClassTransfer", "usdc": "500.0", "toPerp": True}},
        ],
        "fills": [],
        "funding": [],
        "clearinghouse": {"marginSummary": {"accountValue": "100.0"}, "withdrawable": "0.0", "assetPositions": []},
        "spot": {"balances": [{"coin": "USDC", "total": "995.0", "hold": "0.0"}]},
        "open_orders": [],
        "all_mids": {},
    }
    attr = pta.compute_post_trade_attribution(snap, paper_dir=paper_dir)
    assert attr.deposited_capital_usd == pytest.approx(1_000.0)
    assert attr.account_return_pct == pytest.approx(-0.5)  # ($995 - $1,000) / $1,000 = -0.5% (even with 0 fills in window!)
    assert attr.paper_comparison is not None
    assert attr.paper_comparison["testnet_deposited_usd"] == pytest.approx(1_000.0)

    prom_txt = prom_metrics.render_prometheus_metrics({"attribution": attr.to_dict()})
    assert 'hl_strategy_starting_capital_usd{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 1000' in prom_txt
    assert 'hl_strategy_return_pct{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} -0.5' in prom_txt


def test_hyperevm_spot_and_cross_pool_fees_and_slippage_gated_executable():
    assert lfa.DEFAULT_TOP_WHALES == oea.OnChainEventConfig().top_whales == 60

    cross_pool = defi.PoolSpec(
        dex="PRJX V3",
        label="UBTC/WHYPE 30bp",
        pool_address="0x0d6ecb912b6ee160e95bc198b618acc1bcb92525",
        base_symbol="UBTC",
        quote_symbol="WHYPE",
        core_base_perp="BTC",
        core_quote_perp="HYPE",
        fee_bps=30.0,
    )
    # Core ref = 60,000 / 60 = 1,000 WHYPE per UBTC; set EVM price = 1,005.0 (+50 bps gross spread)
    # Pool fee = 30 bps; TWO liquid HyperCore Spot legs = 7 + 7 = 14 bps -> zero-size net_arb_bps = +6.0 bps!
    # Thin liquidity ($2,000 virtual depth) -> $5k slippage = 5000 / 7000 * 1e4 = 7,142.86 bps -> NOT executable!
    sqrt_ubtc_whype = int(math.sqrt(1005.0 * 1e10) * (1 << 96))
    slot0 = "0x" + hex(sqrt_ubtc_whype)[2:].rjust(64, "0") + "0" * 64
    t0_ubtc = "0x" + defi.HYPEREVM_TOKENS["UBTC"][0][2:].rjust(64, "0")
    t1_whype = "0x" + defi.HYPEREVM_TOKENS["WHYPE"][0][2:].rjust(64, "0")
    sqrt_p = float(sqrt_ubtc_whype) / float(1 << 96)
    thin_liq = max(1, int((2_000.0 / 60_000.0) * (10**8) * sqrt_p))

    def rpc_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        data = body["params"][0]["data"]
        if data == defi.SEL_SLOT0:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": slot0})
        if data == defi.SEL_LIQUIDITY:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(thin_liq)})
        if data == defi.SEL_TOKEN0:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": t0_ubtc})
        if data == defi.SEL_TOKEN1:
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": t1_whype})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0"})

    perps = {
        "BTC": {"mid_px": 60_000.0, "mark_px": 60_000.0},
        "HYPE": {"mid_px": 60.0, "mark_px": 60.0},
    }
    spots = {
        "UBTC": {"token": "UBTC", "pair": "@1", "mid_px": 60_000.0, "day_vol_usd": 5_000_000.0},
        "HYPE": {"token": "HYPE", "pair": "@107", "mid_px": 60.0, "day_vol_usd": 20_000_000.0},
    }
    with httpx.Client(transport=httpx.MockTransport(rpc_handler)) as client:
        rows = defi.scan_hyperevm_vs_hypercore_arb(client, perps, spots, pools=(cross_pool,))
    assert len(rows) == 1
    assert rows[0].core_fee_bps == pytest.approx(14.0)  # 7 bps Spot UBTC + 7 bps Spot HYPE
    assert rows[0].net_arb_bps == pytest.approx(6.0, abs=0.2)
    assert rows[0].executable is False  # blocked by V3 depth slippage on $5k clip!
