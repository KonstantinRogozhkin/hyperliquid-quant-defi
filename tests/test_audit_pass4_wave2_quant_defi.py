"""Comprehensive Pass-4 Wave 2 regression suite for Quant Core, Ledgers, Autopilot, FDR, Microstructure & DeFi."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import cross_dex_scanner as cds
from hl_quant import funding_arb as fa
from hl_quant import hl_autopilot as ap
from hl_quant import hl_boros_fixed_funding as boros
from hl_quant import hl_cross_dex_paper as cdp
from hl_quant import hl_frontier_persistence as fp
from hl_quant import hl_funding_settlement_study as fss
from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import hl_hip3_paper as hp
from hl_quant import hl_hyperevm_defi as evm
from hl_quant import hl_hyperevm_yield_stack as hys
from hl_quant import hl_microstructure as hm
from hl_quant import hl_onchain_event_alpha as oea
from hl_quant import hl_spot_perp_arb as spa
from hl_quant import hl_tear_sheet as ts
from hl_quant import metamodel as mm
from hl_quant import paper_tracker as pt
from hl_quant import universe_selection as us
from hl_quant import walk_forward as wf

H_MS = 3_600_000
T0 = 1_795_800_000_000 - (1_795_800_000_000 % H_MS)


def _flat_book(mid: float, spread_bps: float = 1.0, depth_usd: float = 250_000.0):
    half = mid * (spread_bps / 2.0) / 1e4
    sz = depth_usd / mid
    return [(mid - half, sz)], [(mid + half, sz)]


# --------------------------------------------------------------------------- 1. QUANT-01 & QUANT-11


def test_quant_01_and_11_gap_returns_masked_xs_ranks_best_cex_okx_scale_and_compounded_dd():
    # QUANT-01: 1h missing/0.0 candle close gap must NOT erase a +20% 2-hour price move!
    hours = T0 + np.arange(6, dtype="int64") * H_MS
    hl_bars = pd.DataFrame(
        {
            "open_time_ms": [hours[0], hours[1], hours[3], hours[4], hours[5]],  # hours[2] missing!
            "close": [100.0, 100.0, 120.0, 120.0, 120.0],
            "volume": [1000.0] * 5,
        }
    )
    hl_bars.loc[1, "close"] = 0.0  # 0.0 corrupt close at hours[1] -> ffill(limit=2) bridges to hours[3]!
    cex_bars = pd.DataFrame(
        {
            "open_time_ms": list(hours),
            "close": [100.0, 100.0, 100.0, 120.0, 120.0, 120.0],
            "notional_usd": [1_000_000.0] * 6,
        }
    )
    hl_fund = pd.DataFrame({"time_ms": list(hours), "funding_rate": [0.0001] * 6})
    cex_fund = pd.DataFrame({"time_ms": list(hours), "funding_rate": [0.0] * 6})

    panel = fa.build_panel(hl_fund, cex_fund, hl_bars, cex_bars)
    # Compounded HL return over the window captures the full +20% move (100 -> 120), never 0.0!
    comp_hl = float((1.0 + panel["hl_ret"]).prod() - 1.0)
    assert comp_hl == pytest.approx(0.20, abs=1e-6)

    # Same gap-bridging inside alpha_families.build_wide_market + OKX 1000x k* scaling + best_cex without Bybit
    n_h = 400
    hrs = T0 + np.arange(n_h, dtype="int64") * H_MS
    rows = []
    for idx, coin in enumerate(["kPEPE", "BTC", "ETH", "SOL", "ILLIQ1", "ILLIQ2"]):
        is_illiq = coin.startswith("ILLIQ")
        hl_c = np.full(n_h, 10.0 if coin == "kPEPE" else 100.0 + idx * 10.0)
        if coin == "BTC":
            hl_c[10] = 0.0  # 0.0 glitch
            hl_c[11] = np.nan  # 1h missing bar
            hl_c[12:] = hl_c[9] * 1.25  # +25% jump across gap
        elif not is_illiq:
            hl_c = hl_c * np.linspace(1.0, 1.05 + 0.03 * idx, n_h)
        else:
            # Extreme illiquid outlier returns that previously polluted cross-sectional ranks!
            hl_c = hl_c * (2.5 if coin == "ILLIQ1" else 0.2) ** (np.arange(n_h) / n_h)
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hrs,
                    "coin": coin,
                    "hl_close": hl_c,
                    "hl_ret": np.zeros(n_h),
                    "hl_notional_usd": np.full(n_h, 1_000.0 if is_illiq else 5_000_000.0),
                    "hl_fund_next": np.full(n_h, 0.0003 if coin == "kPEPE" else 0.00001),
                    "hl_rate_h": np.full(n_h, 0.0003 if coin == "kPEPE" else 0.00001),
                    "bybit_close": np.full(n_h, np.nan if coin == "kPEPE" else 100.0),
                    "bybit_ret": np.zeros(n_h),
                    "bybit_notional_usd": np.full(n_h, np.nan if coin == "kPEPE" else 5_000_000.0),
                    "bybit_fund_next": np.zeros(n_h),
                    "bybit_rate_h": np.zeros(n_h),
                    "binance_close": np.full(n_h, np.nan),
                    "binance_ret": np.zeros(n_h),
                    "binance_notional_usd": np.full(n_h, np.nan),
                    "binance_fund_next": np.zeros(n_h),
                    "binance_rate_h": np.zeros(n_h),
                    # kPEPE unscaled OKX price 0.01 vs HL 10.0 (ratio 1000x) -> must be scaled x1000 and accepted!
                    "okx_close": np.full(n_h, 0.01 if coin == "kPEPE" else 100.0),
                    "okx_ret": np.zeros(n_h),
                    "okx_notional_usd": np.full(n_h, 5_000_000.0),
                    "okx_fund_next": np.zeros(n_h),
                    "okx_rate_h": np.zeros(n_h),
                }
            )
        )
    mkt = af.build_wide_market(pd.concat(rows, ignore_index=True))
    # BTC +25% move across [9 -> 12] gap is preserved in hl_ret_next
    btc_comp = float((1.0 + mkt.hl_ret_next["BTC"].iloc[:13]).prod() - 1.0)
    assert btc_comp == pytest.approx(0.25, abs=1e-6)

    # carry_best_cex trades kPEPE on OKX despite missing Bybit AND 1000x OKX price scale
    w_carry, vc = af.build_funding_carry_weights(
        mkt, {"mode": "best_cex", "lookback_h": 24, "entry_apr": 0.10, "min_vol_usd": 100_000.0}
    )
    assert float(w_carry["kPEPE"].iloc[-1]) > 0.0
    assert vc is not None and vc["kPEPE"].iloc[-1] == "okx"

    # QUANT-11: XS weights are identical with or without illiquid outlier coins in the universe
    mkt_liquid_only = af.build_wide_market(
        pd.concat([r for r in rows if not str(r["coin"].iloc[0]).startswith("ILLIQ")], ignore_index=True)
    )
    p_xs = {"lookback_h": 24, "rebalance_h": 12, "sign": 1.0, "top_n": 1, "min_vol_usd": 100_000.0}
    w_with_illiq = af.build_xs_price_weights(mkt, p_xs)
    w_clean = af.build_xs_price_weights(mkt_liquid_only, p_xs)
    for c in ("kPEPE", "BTC", "ETH", "SOL"):
        pd.testing.assert_series_equal(w_with_illiq[c], w_clean[c], check_names=False)
    assert (w_with_illiq["ILLIQ1"].abs().max() == 0.0) and (w_with_illiq["ILLIQ2"].abs().max() == 0.0)

    # Compounded drawdown in series_metrics & metamodel stressed vol-target cost
    s_crash = pd.Series([-0.50, -0.50], index=[hrs[0], hrs[1]])
    m_dd = af.series_metrics(s_crash)
    # Compounded: 1.0 -> 0.5 -> 0.25 => -75% max drawdown (additive cumsum would say -100% / -50%)
    assert m_dd["max_drawdown"] == pytest.approx(-0.75, abs=1e-6)
    s_vt = pd.Series([0.001, -0.001] * 20 + [0.02, -0.02] * 20)
    vt_zero = mm.apply_vol_target(s_vt, target_vol_annual=0.10, lookback_h=24, cost_bps=0.0)
    vt_stressed = mm.apply_vol_target(s_vt, target_vol_annual=0.10, lookback_h=24, cost_bps=5.0)
    assert float(vt_stressed.sum()) < float(vt_zero.sum())


# --------------------------------------------------------------------------- 2. QUANT-02


def test_quant_02_payback_gate_no_triple_counted_l2_slip_and_one_way_fee_credit_cap(tmp_path: Path):
    # Spot+Perp: one_way_fee = 5.5 bps (RT = 11.0 bps). With entry_slip = 4.0 bps and exec_basis = +20 bps:
    # basis_credit is capped at one_way_fee (5.5 bps), and net_hurdle = 11.0 + 1.0*4.0 - 5.5 = 9.5 bps (NOT +2*4.0!).
    fund_h = 1.0 / 1e4  # 1.0 bp/hour -> payback_hours equals net_hurdle_bps!
    mkt = [
        {
            "base_symbol": "BTC",
            "spot_pair": "@142",
            "spot_token": "UBTC",
            "quote_token": "USDC",
            "is_proxy_hedge": False,
            "spot_mid_px": 100.0,
            "spot_day_vol_usd": 10_000_000.0,
            "perp_mark_px": 100.24,  # +24 bps mark basis
            "perp_oracle_px": 100.0,
            "funding_rate_h": fund_h,
            "perp_day_vol_usd": 50_000_000.0,
            "perp_oi_usd": 20_000_000.0,
            "only_isolated": False,
            "max_leverage": 20,
        }
    ]

    def book_fn(k: str):
        if k == "BTC":
            # mid = 100.24, bid = 100.22 (~2 bps slip)
            return [(100.22, 1000.0)], [(100.26, 1000.0)]
        # spot mid = 100.00, ask = 100.02 (~2 bps slip) -> total entry_slip = 4.0 bps, exec_basis ~ +19.996 bps
        return [(99.98, 1000.0)], [(100.02, 1000.0)]

    snap = spa.scan_spot_perp_opportunities(
        cfg=spa.SpotPerpPaperConfig(use_maker_fees=True, max_book_mark_divergence_bps=15.0),
        markets=mkt,
        book_fetcher=book_fn,
        hlp_summary=(10.0, 1e8),
    )
    opp = snap.opportunities[0]
    assert opp.entry_l2_slip_bps == pytest.approx(4.0, abs=0.05)
    # 11.0 RT fee + 1.0 * 4.0 exit slip - 5.5 capped credit = 9.5 bps -> 9.5 hours (triple-counting gave 13.5h or 0h)
    assert opp.payback_hours == pytest.approx(9.5, abs=0.2)

    # Proxy hedge gets <= 0 credit on positive exec basis
    mkt_proxy = [dict(mkt[0], base_symbol="PAXG", spot_token="XAUT0", is_proxy_hedge=True)]
    snap_p = spa.scan_spot_perp_opportunities(
        cfg=spa.SpotPerpPaperConfig(use_maker_fees=True),
        markets=mkt_proxy,
        book_fetcher=lambda k: book_fn("BTC" if k == "PAXG" else "@142"),
        hlp_summary=(10.0, 1e8),
    )
    # 11.0 + 4.0 - 0.0 = 15.0h
    assert snap_p.opportunities[0].payback_hours == pytest.approx(15.0, abs=0.2)


# --------------------------------------------------------------------------- 3. QUANT-03


def test_quant_03_empty_metrics_keys_union_evaluate_and_delisted_survivorship():
    # Empty series returns ALL standard & alias keys so callers never hit KeyError: 'apr'
    m_empty = fa.metrics(pd.DataFrame())
    for req_key in ("hours", "total_return", "apr", "vol", "sharpe", "max_dd", "turnover", "active_frac"):
        assert req_key in m_empty and m_empty[req_key] == 0.0

    # Union panel evaluation: DELIST terminates at hour 10 while BTC runs 30 hours
    idx_btc = pd.Index([T0 + i * H_MS for i in range(30)], name="hour_ms")
    idx_delist = pd.Index([T0 + i * H_MS for i in range(10)], name="hour_ms")

    def _mk_panel(idx: pd.Index, spread_h: float) -> pd.DataFrame:
        n = len(idx)
        return pd.DataFrame(
            {
                "hl_close": np.full(n, 100.0),
                "cex_close": np.full(n, 100.0),
                "hl_ret": np.zeros(n),
                "cex_ret": np.zeros(n),
                "cex_notional_usd": np.full(n, 5_000_000.0),
                "hl_fund_next": np.full(n, spread_h),
                "cex_fund_next": np.zeros(n),
                "hl_rate_h": np.full(n, spread_h),
                "cex_rate_h": np.zeros(n),
                "cex_event_now": np.ones(n),
                "spread_h": np.full(n, spread_h),
            },
            index=idx,
        )

    costs = fa.Costs()
    res_eval = fa.evaluate(
        {"BTC": _mk_panel(idx_btc, 0.0005), "DELIST": _mk_panel(idx_delist, 0.0005)},
        grid=(fa.Params(lookback_h=3, entry_apr=0.10, exit_apr=0.02),),
        costs=costs,
        train_fraction=0.5,
    )
    # Full 30-hour union is preserved (15 train + 15 test), NOT truncated to 10 hours!
    assert res_eval.chosen_train["hours"] == 15
    assert res_eval.chosen_test["hours"] == 15
    # DELIST paid full round-trip entry+exit cost inside train (at T_delist = hour 9) and stayed flat in test
    sim_delist_train = fa._simulate_on_union(
        _mk_panel(idx_delist, 0.0005), idx_btc, fa.Params(lookback_h=3, entry_apr=0.10, exit_apr=0.02), costs
    )
    assert float(sim_delist_train.loc[idx_btc[9], "cost_pnl"]) < 0.0
    assert res_eval.per_coin_test["DELIST"]["cost_pnl"] == 0.0

    # Walk-forward also forces exit and charges exit cost when a coin delists mid-fold (even if target weight stayed 0.5)
    w_res, _ = wf.simulate_portfolio_weights(
        {"BTC": _mk_panel(idx_btc, 0.0005), "DELIST": _mk_panel(idx_delist, 0.0005)},
        pd.DataFrame({"BTC": np.full(30, 0.5), "DELIST": np.full(30, 0.5)}, index=idx_btc),
        costs,
    )
    assert float(w_res.loc[idx_btc[10], "cost_pnl"]) < 0.0

    # cex_symbol("kFloki") + Bybit Closed + delisted exemption from today's volume/cap
    assert us.cex_symbol("kFloki") == "1000FLOKIUSDT"

    class StubHL:
        def info(self, _payload: dict[str, Any]):
            return [
                {
                    "universe": [
                        {"name": "BTC"},
                        {"name": "ETH"},
                        {"name": "DEAD", "isDelisted": True},
                    ]
                },
                [
                    {"dayNtlVlm": "50000000", "openInterest": "1000", "markPx": "60000"},
                    {"dayNtlVlm": "40000000", "openInterest": "5000", "markPx": "3000"},
                    {"dayNtlVlm": "0", "openInterest": "0", "markPx": "0.1"},  # $0 today's volume!
                ],
            ]

    class StubBybit:
        def get(self, _path: str, params: dict[str, Any]):
            if params.get("status") == "Closed":
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"symbol": "DEADUSDT", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Closed"}],
                        "nextPageCursor": "",
                    },
                }
            return {
                "retCode": 0,
                "result": {
                    "list": [
                        {"symbol": "BTCUSDT", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Trading"},
                        {"symbol": "ETHUSDT", "quoteCoin": "USDT", "contractType": "LinearPerpetual", "status": "Trading"},
                    ],
                    "nextPageCursor": "",
                },
            }

    cands = us.discover_common_coins(
        StubHL(),  # type: ignore[arg-type]
        StubBybit(),  # type: ignore[arg-type]
        "bybit",
        min_prefilter_vol_usd=10_000_000.0,
        max_coins=1,
        include_delisted=True,
    )
    # max_coins=1 caps active coins to BTC, while delisted DEAD (0 today's vol, Closed on Bybit) is STILL included!
    assert {c.coin for c in cands} == {"BTC", "DEAD"}


# --------------------------------------------------------------------------- 4. QUANT-04, QUANT-07, QUANT-09, B-1


def test_quant_04_07_09_b1_carry_ledgers_liq_before_topup_split_margin_and_scanner_bbo(tmp_path: Path):
    # 1. Spot+Perp: perp_margin_liquidation MUST trigger BEFORE MARGIN_TOPUP (even when abundant free_cash exists!),
    #    and isolated perp limited liability floors perp_margin at 0.0.
    sp_dir = tmp_path / "sp_liq_topup"
    cfg_sp = spa.SpotPerpPaperConfig(
        starting_capital_usd=100_000.0,
        hlp_reserve_share=0.0,
        slot_equity_usd=20_000.0,
        isolated_slot_scale=1.0,
        target_perp_leverage=2.0,
        min_clean_streak=1,
        allow_only_isolated=True,
        max_tick_nav_jump_pct=99.0,
        require_hlp_hurdle=False,
    )
    m_iso = [
        {
            "base_symbol": "ALT",
            "spot_pair": "@200",
            "spot_token": "ALT",
            "quote_token": "USDC",
            "is_proxy_hedge": False,
            "spot_mid_px": 100.0,
            "spot_day_vol_usd": 5_000_000.0,
            "perp_mark_px": 100.0,
            "perp_oracle_px": 100.0,
            "funding_rate_h": 20.0 / 100.0 / spa.HOURS_PER_YEAR,
            "perp_day_vol_usd": 10_000_000.0,
            "perp_oi_usd": 5_000_000.0,
            "only_isolated": True,
            "max_leverage": 5,
        }
    ]
    r_open = spa.run_spot_perp_paper_tick(
        state_dir=sp_dir, cfg=cfg_sp, now_ms=T0, markets=m_iso, book_fetcher=lambda _k: _flat_book(100.0), hlp_summary=(10.0, 1e8)
    )
    assert r_open["state"]["positions"]["ALT"]["last_accrual_ms"] == T0
    # Same-hour duplicate retry is skipped
    r_dup = spa.run_spot_perp_paper_tick(
        state_dir=sp_dir, cfg=cfg_sp, now_ms=T0 + 30_000, markets=m_iso, book_fetcher=lambda _k: _flat_book(100.0), hlp_summary=(10.0, 1e8)
    )
    assert r_dup["status"] == "duplicate_hour_skipped"

    # +100% explosion (100 -> 200): short perp loses 100% (> 50% initial margin), while $80k free_cash sits idle.
    # Must NOT top up from free_cash; must liquidate with isolated limited liability (perp_margin capped at 0)!
    m_boom = [dict(m_iso[0], spot_mid_px=200.0, perp_mark_px=200.0)]
    r_liq = spa.run_spot_perp_paper_tick(
        state_dir=sp_dir, cfg=cfg_sp, now_ms=T0 + 4 * H_MS, markets=m_boom, book_fetcher=lambda _k: _flat_book(200.0), hlp_summary=(10.0, 1e8)
    )
    actions = [(t["action"], t["reason"]) for t in r_liq["trades_this_tick"]]
    assert not any(a == "MARGIN_TOPUP" for a, _ in actions)
    assert ("CLOSE", "perp_margin_liquidation") in actions
    # Spot doubled ($13.33k -> $26.67k, +$13.33k) while isolated perp margin lost at most its ~$6.66k margin -> net gain!
    assert r_liq["state"]["equity_usd"] > 105_000.0

    # fetch_settled_funding_sum: fails closed ({}) if page 2 returns HTTP 500, and caps start at 24h
    seen_starts: list[int] = []

    def bad_page2_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen_starts.append(int(body["startTime"]))
        if len(seen_starts) == 1:
            return httpx.Response(200, json=[{"time": int(body["startTime"]) + i, "fundingRate": "0.0001"} for i in range(500)])
        return httpx.Response(500, json={"error": "boom"})

    with httpx.Client(transport=httpx.MockTransport(bad_page2_handler)) as c_bad:
        partial = spa.fetch_settled_funding_sum(c_bad, ["BTC"], T0 - 72 * H_MS, T0)
    assert partial == {}  # Never return partial page-1 sum!
    assert seen_starts[0] == T0 - 24 * H_MS + 1

    # 2. HIP-3 split-margin: winning leg uPnL is NOT locked margin, and losing leg breach triggers split_margin_leg_liquidation
    h3_dir = tmp_path / "hip3_split_liq"
    cfg_h3 = hp.Hip3PaperConfig(min_clean_streak=1, per_leg_leverage=2.0, max_tick_nav_jump_pct=99.0)
    mkts_h3 = [
        hip3.Hip3BuilderMarket("io", "io", "io:TSLA", "TSLA", 100.0, 100.0, 0.0, 0.00005, 43.8, 5e6, 2e6, False, 5, True, 1.0, 0.9, 0.3),
        hip3.Hip3BuilderMarket("xyz", "xyz", "xyz:TSLA", "TSLA", 100.0, 100.0, 0.0, 0.0, 0.0, 5e6, 2e6, False, 5, True, 1.0, 0.9, 0.3),
    ]
    r_h1 = hp.run_hip3_paper_tick(state_dir=h3_dir, cfg=cfg_h3, now_ms=T0, markets=mkts_h3, book_fetcher=lambda _s: _flat_book(100.0))
    assert r_h1["state"]["positions"]["TSLA"]["last_accrual_ms"] == T0
    # +20% move (100 -> 120): locked margin is $3k + $3k = $6,000 (NOT $6,500!)
    mkts_h3_20 = [dataclasses.replace(m, mark_px=120.0, oracle_px=120.0) for m in mkts_h3]
    r_h2 = hp.run_hip3_paper_tick(state_dir=h3_dir, cfg=cfg_h3, now_ms=T0 + 4 * H_MS, markets=mkts_h3_20, book_fetcher=lambda _s: _flat_book(120.0))
    assert r_h2["state"]["margin_locked_usd"] == pytest.approx(6_000.0)
    # +40% move (100 -> 140): short leg equity = $2,500 - $2,000 = $500 <= $7,000 * 0.10 ($700 MMR) -> split_margin_leg_liquidation!
    mkts_h3_40 = [dataclasses.replace(m, mark_px=140.0, oracle_px=140.0) for m in mkts_h3]
    r_h3 = hp.run_hip3_paper_tick(state_dir=h3_dir, cfg=cfg_h3, now_ms=T0 + 8 * H_MS, markets=mkts_h3_40, book_fetcher=lambda _s: _flat_book(140.0))
    assert r_h3["state"]["positions"] == {}
    assert r_h3["trades"][0]["exit_reason"] == "split_margin_leg_liquidation"

    # 3. Cross-DEX: Lighter prefers two-sided BBO mid over stale last_trade_price; Paradex guards interval="0"; kFloki raw_hl_symbol preserved
    def scanner_handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "orderBookDetails" in u:
            return httpx.Response(
                200,
                json={
                    "order_book_details": [
                        {
                            "market_type": "perp",
                            "status": "active",
                            "symbol": "ETH",
                            "market_id": 1,
                            "last_trade_price": "1500.0",  # stale trade!
                            "best_bid_price": "2000.0",
                            "best_ask_price": "2002.0",
                            "daily_quote_token_volume": "10000000",
                            "open_interest": "1000",
                        }
                    ]
                },
            )
        if "funding-rates" in u:
            return httpx.Response(200, json={"funding_rates": [{"exchange": "lighter", "market_id": 1, "rate": "0.0008"}]})
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "symbol": "ETH-USD-PERP",
                        "mark_price": "2001.0",
                        "funding_interval_minutes": "0",  # must fall back to 480m (8h), NOT 1h!
                        "funding_rate": "0.0008",
                        "volume_24h": "5000000",
                        "open_interest": "500",
                    }
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(scanner_handler)) as c_sc:
        lt = cds.fetch_lighter_quotes(c_sc)
        pdx = cds.fetch_paradex_quotes(c_sc)
    assert lt["ETH"].mark_px == pytest.approx(2001.0)
    assert pdx["ETH"].funding_rate_h == pytest.approx(0.0008 / 8.0)

    # Cross-DEX paper stores raw_hl_symbol ("kFloki") and requires full_ok on CLOSE before trusting partial L2 slip
    cd_dir = tmp_path / "cd_kfloki"
    seen_books: list[tuple[str, str]] = []

    def cd_book(dex: str, sym: str):
        seen_books.append((dex, sym))
        # On CLOSE (second tick), return tiny 0.01 sz partial book (sl=0.0, full_ok=False) -> must fall back to >=2.0 bps!
        if len(seen_books) > 2:
            return [(0.10, 1.0)], [(0.10, 1.0)]
        return _flat_book(0.10, spread_bps=1.0)

    q_hl = cds.DexMarketQuote("hyperliquid", "kFLOKI", "kFloki", 0.10, 0.0003, 262.8, 5e6, 2e6, 1.0)
    q_lt = cds.DexMarketQuote("lighter", "kFLOKI", "77", 0.10, 0.0, 0.0, 5e6, 2e6, 1.0)
    with httpx.Client() as c_cd:
        r_c1 = cdp.run_cross_dex_paper_tick(
            c_cd, state_dir=cd_dir, cfg=cdp.CrossDexPaperConfig(min_persistence_ticks=1), now_ms=T0,
            hl_quotes={"kFLOKI": q_hl}, lighter_quotes={"kFLOKI": q_lt}, book_fetcher=cd_book,
        )
        assert r_c1["state"]["positions"]["kFLOKI"]["raw_hl_symbol"] == "kFloki"
        assert ("hyperliquid", "kFloki") in seen_books
        r_c2 = cdp.run_cross_dex_paper_tick(
            c_cd, state_dir=cd_dir, cfg=cdp.CrossDexPaperConfig(min_persistence_ticks=1), now_ms=T0 + 4 * H_MS,
            hl_quotes={"kFLOKI": dataclasses.replace(q_hl, funding_apr_pct=0.0, funding_rate_h=0.0)},
            lighter_quotes={"kFLOKI": q_lt}, book_fetcher=cd_book,
        )
        assert r_c2["trades"][0]["l2_slip_bps"] >= 2.0


# --------------------------------------------------------------------------- 5. QUANT-05, QUANT-08, QUANT-12


def test_quant_05_08_12_nav_guard_sign_mag_perf_fee_order_drifted_qty_roundtrip_and_irregular_vol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    # 1. Irregular-interval demeaned volatility in Autopilot: constant drift across mixed 1h / 24h intervals has ZERO vol!
    drift_h = 0.001  # +0.1% per hour -> +876% APR deterministic drift
    dts = [1.0, 24.0, 1.0, 24.0, 4.0, 12.0]
    rets = [drift_h * d for d in dts]
    sh, vol = ap._sleeve_sharpe_and_vol(rets, dt_hours=dts)
    # Without demeaning, std(r / sqrt(dt)) was ~0.16 (false 16% vol on a deterministic line!); demeaned resid == 0 -> floor 0.05
    assert sh == 0.0 and vol == pytest.approx(0.05)

    # 2. Autopilot NAV guard rejects opposite-sign spike on next tick AND applies confirmed loss BEFORE perf-fee crystallization!
    ap_root = tmp_path / "ap_perf"
    sp_dir = ap_root / "hl_spot_perp_paper"
    sp_dir.mkdir(parents=True)

    def _set_sp(eq: float) -> None:
        (sp_dir / "state.json").write_text(json.dumps({"equity_usd": eq, "starting_capital_usd": 100_000.0, "positions": {}}))

    _set_sp(100_000.0)
    closes = {c: [100.0] * 55 for c in ap.CTA_UNIVERSE}
    cfg_ap = ap.AutopilotConfig(starting_capital_usd=250_000.0, max_tick_nav_jump_pct=3.0)
    t_q3_a = int(datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp() * 1000)
    ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q3_a, daily_closes=closes)

    # Intra-Q3 gain (+2% master < 3% guard) accrues positive performance fee liability
    _set_sp(102_500.0)
    r_gain = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q3_a + 4 * H_MS, daily_closes=closes)
    assert r_gain["state"]["accrued_perf_fee_usd"] > 0.0

    # End of Q3 (20:00 UTC): genuine -20% sleeve crash (-16% master > 3% guard) gets clamped!
    _set_sp(82_000.0)
    r_clamp = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q3_a + 8 * H_MS, daily_closes=closes)
    assert r_clamp["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r_clamp["state"]["nav_guard_pending_ret_pct"] < -5.0

    # First tick of Q4 (2026-10-01 00:00 UTC): crash persists -> confirmed!
    # Must apply -16% loss BEFORE crystallizing Q3 perf fee -> $0.00 crystallized perf fee!
    t_q4 = int(datetime(2026, 10, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    r_q4 = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q4, daily_closes=closes)
    assert r_q4["state"]["risk_telemetry"]["nav_guard_triggered"] is False
    assert r_q4["state"]["cum_perf_fee_usd"] == pytest.approx(0.0, abs=1e-6)

    # Opposite-sign oscillation (+25% spike then -25% crash) MUST re-clamp instead of confirming!
    _set_sp(115_000.0)
    r_osc1 = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q4 + 4 * H_MS, daily_closes=closes)
    assert r_osc1["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    _set_sp(55_000.0)
    r_osc2 = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=t_q4 + 8 * H_MS, daily_closes=closes)
    assert r_osc2["state"]["risk_telemetry"]["nav_guard_triggered"] is True

    # 3. Crisis-CTA zero-trade price round-trip yields exact $0.00 MTM PnL!
    cta_root = tmp_path / "cta_rt"
    (cta_root / "hl_spot_perp_paper").mkdir(parents=True)
    (cta_root / "hl_spot_perp_paper" / "state.json").write_text(json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}}))
    cfg_cta = ap.AutopilotConfig(
        cta_weight_deadband=0.50,
        rebalance_deadband_pct=20.0,
        cta_funding_cost_apr_pct=0.0,
        default_hlp_apr_pct=0.0,
        mgmt_fee_annual_pct=0.0,
        perf_fee_pct=0.0,
        max_tick_nav_jump_pct=99.0,
    )
    up_100 = {"BTC": [50.0 * (1.015**i) for i in range(54)] + [100.0]}
    up_130 = {"BTC": up_100["BTC"][:-1] + [130.0]}
    r_cta0 = ap.run_autopilot_tick(data_root=cta_root, cfg=cfg_cta, now_ms=T0, daily_closes=up_100)
    r_cta1 = ap.run_autopilot_tick(data_root=cta_root, cfg=cfg_cta, now_ms=T0 + 4 * H_MS, daily_closes=up_130)
    assert r_cta1["state"]["cta_state"]["cum_pnl_usd"] > 50.0
    r_cta2 = ap.run_autopilot_tick(data_root=cta_root, cfg=cfg_cta, now_ms=T0 + 8 * H_MS, daily_closes=up_100)
    assert r_cta2["state"]["cta_state"]["cum_pnl_usd"] == pytest.approx(0.0, abs=1e-4)
    assert r_cta2["state"]["gross_equity_usd"] == pytest.approx(r_cta0["state"]["gross_equity_usd"], abs=0.05)

    # 4. Paper tracker zero-trade deadband round-trip yields exact $0.00 price PnL + FORCED_STALE_EXIT logs & charges cost
    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda _m, _s: {"BTC": {"hl_weight": 0.20, "cex_legs": {}, "cex_weight": 0.0, "cex_venue": ""}},
    )

    def _pt_mkt(n_h: int, coins: tuple[str, ...] = ("BTC", "ETH")) -> af.WideMarket:
        hrs_p = T0 + np.arange(n_h, dtype="int64") * H_MS
        dfs = []
        for c in coins:
            d: dict[str, Any] = {"hour_ms": hrs_p, "coin": c}
            for v in ("hl", "bybit", "binance", "okx"):
                d[f"{v}_close"] = np.full(n_h, 100.0)
                d[f"{v}_ret"] = np.zeros(n_h)
                d[f"{v}_notional_usd"] = np.full(n_h, 1e6)
                d[f"{v}_fund_next"] = np.zeros(n_h)
                d[f"{v}_rate_h"] = np.zeros(n_h)
            dfs.append(pd.DataFrame(d))
        return af.build_wide_market(pd.concat(dfs, ignore_index=True))

    def _q_pt(px: float) -> dict[str, pt.L2Quote]:
        return {c: pt.L2Quote(c, px - 0.01, px + 0.01, px, 2.0, 1e6, 1e6) for c in ("BTC", "ETH")}

    pt_dir = tmp_path / "pt_rt"
    r_p0 = pt.run_paper_tick(_pt_mkt(20), _q_pt(100.0), state_dir=pt_dir, sleeves=[], rebalance_deadband_weight=0.08)
    eq_after_open = r_p0["equity_usd"]
    # +15% price move -> drifted weight ~0.223 (drift 0.023 < 0.08 deadband -> ZERO trades!)
    r_p1 = pt.run_paper_tick(_pt_mkt(21), _q_pt(115.0), state_dir=pt_dir, sleeves=[], rebalance_deadband_weight=0.08)
    assert r_p1["trades_count"] == 0 and r_p1["price_pnl_usd"] > 2_500.0
    # Return to 100.0 -> cumulative price PnL returns to exact $0.00!
    r_p2 = pt.run_paper_tick(_pt_mkt(22), _q_pt(100.0), state_dir=pt_dir, sleeves=[], rebalance_deadband_weight=0.08)
    assert r_p2["trades_count"] == 0
    assert (r_p1["price_pnl_usd"] + r_p2["price_pnl_usd"]) == pytest.approx(0.0, abs=1e-6)
    assert r_p2["equity_usd"] == pytest.approx(eq_after_open, abs=1e-6)

    # Stale held coin missing for max_held_missing_ticks=2 logs FORCED_STALE_EXIT and charges exit cost!
    assert pt.run_paper_tick(_pt_mkt(23, coins=("ETH",)), {"ETH": _q_pt(100.0)["ETH"]}, state_dir=pt_dir, sleeves=[], max_held_missing_ticks=2)["status"] == "held_coins_missing"
    r_stale = pt.run_paper_tick(_pt_mkt(24, coins=("ETH",)), {"ETH": _q_pt(100.0)["ETH"]}, state_dir=pt_dir, sleeves=[], max_held_missing_ticks=2)
    assert r_stale["forced_stale_exits"] == ["BTC"]
    assert r_stale["cost_pnl_usd"] < 0.0
    trades_df = pd.read_parquet(pt_dir / "trades.parquet")
    assert "FORCED_STALE_EXIT" in set(trades_df["action"])


# --------------------------------------------------------------------------- 6. QUANT-06, QUANT-10, QUANT-13, DEFI-01..04


def test_quant_06_10_13_defi_01_04_settlement_net_fdr_micro_chase_boros_yield_evm_events_and_frontier(tmp_path: Path):
    # 1. Settlement study: gross +3.1 bps with tiny dispersion has t_gross >> 2, BUT net_maker (+0.1 bps) has net_t_stat < 2 -> NO-GO!
    m5 = fss.FIVE_MIN_MS
    candles, funding = [], []
    for h in range(24):
        t0 = T0 + h * H_MS
        funding.append({"time": t0, "fundingRate": "-0.0001"})
        if h >= 1:
            jit = 0.6 * ((h % 3) - 1)  # +/- 0.6 bps jitter around +3.1 bps fade
            c_p5 = 100.0 * (1.0 - (3.1 + jit) * 1e-4)
            candles.extend(
                [
                    {"t": t0 - 3 * m5, "T": t0 - 2 * m5, "o": "100.0", "c": "100.0"},
                    {"t": t0 - m5, "T": t0, "o": "100.0", "c": "100.0"},
                    {"t": t0, "T": t0 + m5, "o": "100.0", "c": f"{c_p5:.6f}"},
                    {"t": t0 + 2 * m5, "T": t0 + 3 * m5, "o": f"{c_p5:.6f}", "c": f"{c_p5:.6f}"},
                ]
            )
    # Unclosed candle (T > now_ms) and missing curr_rate hour must be excluded
    ev_trunc = fss.build_coin_settlement_events("TEST", candles, funding[:-1], now_ms=T0 + 22 * H_MS + m5)
    assert int(ev_trunc["t0_ms"].max()) <= T0 + 22 * H_MS

    ev_full = fss.build_coin_settlement_events("TEST", candles, funding)
    stats = fss.evaluate_settlement_strategies(ev_full, maker_rt_bps=3.0)
    row_fade = stats[(stats["split"] == "holdout") & (stats["strategy"] == "post_unwind_fade_5m") & (stats["bucket"] == "APR<=-25% (neg)")].iloc[0]
    assert row_fade["t_stat"] > 10.0  # gross t-stat is huge...
    assert row_fade["net_maker_bps"] == pytest.approx(0.1, abs=0.15)
    assert row_fade["net_t_stat"] < 2.0  # ...but net t-stat after 3.0 bps maker RT is < 2.0!
    meta = {"observed_at_ms": T0, "coins_collected": 1, "total_settlement_events": len(ev_full), "span_days": 1.0}
    md = fss.format_settlement_study_markdown(stats, meta)
    tg = fss.format_settlement_study_telegram_rich_html(stats, meta)
    assert "GO (Maker)" not in md
    assert "🟢" not in tg  # Telegram gate aligned with Markdown GO gate!

    # 2. Microstructure: relative tolerance fills high-priced BTC & unfilled timeout includes runaway chase drift
    l2_rows = []
    for s in range(100):
        ms = T0 + s * 1000
        # Price runs up +50 bps over 30s so passive buy never fills and must chase higher!
        mid = 100_000.0 * (1.0 + (0.0050 if s >= 5 else 0.0))
        l2_rows.append(
            {
                "recv_ms": ms,
                "book_time_ms": ms,
                "coin": "BTC",
                "best_bid": mid - 5.0,
                "best_ask": mid + 5.0,
                "mid_px": mid,
                "spread_bps": 1.0,
                "bid_sz1": 0.01,
                "ask_sz1": 0.01,
                "bid_usd1": 1000.0,
                "ask_usd1": 1000.0,
                "bid_usd5": 5000.0,
                "ask_usd5": 5000.0,
                "imbalance1": 0.0,
                "imbalance5": 0.0,
            }
        )
    # Sell print at t=1s with tiny 1e-8 float jitter at 100,000 price level fills passive buy on trial 0 only
    tr_rows = [
        {
            "recv_ms": T0 + 1000,
            "trade_time_ms": T0 + 1000,
            "coin": "BTC",
            "side": "sell",
            "px": 99_995.0 + 2e-8,  # > 1e-12 absolute, < 1e-9 * 100_000 relative!
            "sz": 1.0,
            "notional_usd": 100_000.0,
            "tid": 1,
        }
    ]
    rep = hm.simulate_passive_alo_coin(pd.DataFrame(l2_rows), pd.DataFrame(tr_rows), order_usd=1000.0, sample_interval_sec=30.0, timeout_sec=20.0)
    assert rep.alo_fill_rate > 0.0  # relative float tolerance allowed the BTC fill!

    # Pure runaway on passive buys (no sells, price drifts up +50 bps before timeout while sells fill immediately at t+1s)
    tr_sells_fill = [
        {"recv_ms": T0 + 1000, "trade_time_ms": T0 + 1000, "coin": "BTC", "side": "buy", "px": 100_005.0, "sz": 1.0, "notional_usd": 100_000.0, "tid": 2}
    ]
    rep_runaway = hm.simulate_passive_alo_coin(
        pd.DataFrame(l2_rows[:45]), pd.DataFrame(tr_sells_fill), order_usd=1000.0, sample_interval_sec=60.0, timeout_sec=20.0
    )
    # Unfilled buy had to chase +50 bps higher after timeout -> maker_eff_cost_bps reflects the ~-25 bps chase drag!
    assert rep_runaway.maker_eff_cost_bps < rep_runaway.taker_eff_cost_bps - 10.0

    # 3. Boros: unpriced coin-margined collateral (tokenId=1 WBTC without USD price) fails closed; negative lock checks floating
    m_unpriced = boros.parse_boros_market(
        {
            "marketId": 7,
            "tokenId": 1,
            "imData": {"name": "Hyperliquid BTC", "symbol": "HL-BTC", "maturity": 1798156800},
            "metadata": {"underlyingSymbol": "BTC", "isSpreadMarket": False},
            "data": {"timeToMaturity": 30 * 86400, "bestBid": 0.12, "bestAsk": 0.13, "floatingApr": 0.05, "notionalOI": 500_000, "volume24h": 50_000},
        },
        collateral_assets={},
    )
    assert m_unpriced is not None and m_unpriced.is_actionable is False and m_unpriced.notional_oi == 0.0

    m_bad_neg_lock = boros.parse_boros_market(
        {
            "marketId": 8,
            "tokenId": 3,
            "imData": {"name": "Hyperliquid ALT", "symbol": "HL-ALT", "maturity": 1798156800},
            "metadata": {"underlyingSymbol": "ALT", "isSpreadMarket": False},
            # Ask is -12% (receive 12% fixed on long perp), BUT live floating is -30% (receiving +30% floating is far better!)
            "data": {"timeToMaturity": 30 * 86400, "bestBid": -0.15, "bestAsk": -0.12, "floatingApr": -0.30, "notionalOI": 500_000, "volume24h": 50_000},
        }
    )
    assert m_bad_neg_lock is not None and "Long Perp + Long Boros" not in m_bad_neg_lock.recommended_action

    # 4. Yield Stack: negative Boros fixed rate NEVER overwrites positive floating HYPE carry
    y_inp = hys.HyperEvmYieldInputs(T0, 35.0, 12.0, "HL-HYPE", -5.0, 28.0, 2.0, 1e8, 2.0, 1e8, 4.0, 1e7, 1e8, 1e7, 9.0, 12.0, 1e8)
    y_strats = hys.build_hyperevm_yield_strategies(y_inp)
    assert not any(s.rate_type == "Fixed (Boros)" for s in y_strats)

    # 5. HyperEVM DeFi: isDelisted filtered while preserving L1Read asset_index
    def hl_delist_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "OLD", "isDelisted": True}, {"name": "BTC", "szDecimals": 5}]},
                    [{"markPx": "1.0"}, {"markPx": "60000.0", "funding": "0.0001", "openInterest": "100", "dayNtlVlm": "10000000"}],
                ],
            )
        return httpx.Response(200, json=[{"tokens": [], "universe": []}, []])

    with httpx.Client(transport=httpx.MockTransport(hl_delist_handler)) as c_del:
        perps_map, _ = evm.fetch_hypercore_markets(c_del)
    assert "OLD" not in perps_map and perps_map["BTC"]["asset_index"] == 1

    # 6. On-Chain Event Alpha: > top_n alerts preserved + 35m delayed cron grades +15m outcome + corrupt parquet tolerated
    cfg_ev = oea.OnChainEventConfig(h5_top_n=2, h5_alert_skew_usd=100_000.0)
    ch_many = [
        {
            "assetPositions": [
                {"position": {"coin": f"C{i}", "szi": "1000", "positionValue": "200000"}}
                for i in range(5)  # 5 alerts > h5_top_n=2!
            ]
        }
    ]
    h5_rows = oea.extract_h5_hlp_inventory_skew(ch_many, {f"C{i}": 200.0 for i in range(5)}, hlp_tvl_usd=1e8, ts_ms=T0, cfg=cfg_ev)
    assert len(h5_rows) == 5 and all(r["alert"] for r in h5_rows)

    corrupt_pq = tmp_path / "corrupt.parquet"
    corrupt_pq.write_bytes(b"not_a_parquet")
    assert oea.grade_forward_outcomes(corrupt_pq, {"BTC": 100.0}, now_ms=T0) == 0

    delayed_pq = tmp_path / "h2_delayed.parquet"
    pd.DataFrame([{"ts_ms": T0, "coin": "BTC", "mark_px": 100.0, "ret_15m_bps": np.nan}]).to_parquet(delayed_pq, index=False)
    assert oea.grade_forward_outcomes(delayed_pq, {"BTC": 101.0}, now_ms=T0 + 35 * 60_000) == 1
    assert pd.read_parquet(delayed_pq).iloc[0]["ret_15m_bps"] == pytest.approx(100.0)

    # 7. HIP-3 Builder Arb: evaluates L2 depth across ALL candidate builder pairs per symbol (picks deep pair over ghost pair)
    mkts_3b = [
        hip3.Hip3BuilderMarket("ghost", "ghost", "ghost:NVDA", "NVDA", 150.0, 150.0, 0.0, 0.0001, 87.6, 5e6, 2e6, False, 10),
        hip3.Hip3BuilderMarket("xyz", "xyz", "xyz:NVDA", "NVDA", 150.0, 150.0, 0.0, 0.00005, 43.8, 5e6, 2e6, False, 10),
        hip3.Hip3BuilderMarket("io", "io", "io:NVDA", "NVDA", 150.0, 150.0, 0.0, 0.00001, 8.76, 5e6, 2e6, False, 10),
    ]
    opps_3b = hip3.scan_hip3_cross_builder_arb(
        mkts_3b,
        book_fetcher=lambda sym: ([], []) if sym.startswith("ghost:") else _flat_book(150.0),
    )
    assert len(opps_3b) == 1
    assert opps_3b[0].short_symbol == "xyz:NVDA" and opps_3b[0].long_symbol == "io:NVDA" and opps_3b[0].depth_5k_ok is True

    # 8. Frontier Persistence duplicate-tick guard + Tear-sheet None maker share
    fp_dir = tmp_path / "fp_dup"
    s_h = hip3.Hip3ScannerSnapshot(T0, "NY_REGULAR", 2, 10, 1e8, [], [])
    s_b = boros.BorosScannerSnapshot(T0, 0, 0, 0, [])
    s_y = hys.HyperEvmYieldSnapshot(y_inp, [])
    fp.run_frontier_persistence_tick(data_root=fp_dir, now_ms=T0, hip3_snap=s_h, boros_snap=s_b, yield_snap=s_y)
    dup_fp = fp.run_frontier_persistence_tick(data_root=fp_dir, now_ms=T0 + 10 * 60_000, hip3_snap=s_h, boros_snap=s_b, yield_snap=s_y)
    assert dup_fp["status"] == "duplicate_tick_skipped"

    md_ts = ts.format_tear_sheet_markdown({"generated_at_utc": "now", "attribution": {"maker_volume_share_pct": None, "effective_fee_bps": None}})
    assert "Maker —" in md_ts and "Maker 100" not in md_ts
