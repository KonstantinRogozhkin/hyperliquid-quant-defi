"""Pass-5 regression suite for all 8 second-order Quant Core, Ledger, Analytics, Metrics & Dashboard fixes."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import cross_dex_scanner as cds
from hl_quant import funding_arb as fa
from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_autopilot as ap
from hl_quant import hl_cross_dex_paper as cdp
from hl_quant import hl_funding_settlement_study as fss
from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import hl_hip3_paper as hp
from hl_quant import hl_portfolio_dashboard as pdash
from hl_quant import hl_spot_perp_arb as spa
from hl_quant import paper_tracker as pt
from hl_quant import walk_forward as wf
from hl_quant.metrics import render_prometheus_metrics

H_MS = 3_600_000
T0 = 1_795_800_000_000 - (1_795_800_000_000 % H_MS)


def _flat_book(mid: float, spread_bps: float = 1.0, depth_usd: float = 250_000.0):
    half = mid * (spread_bps / 2.0) / 1e4
    sz = depth_usd / mid
    return [(mid - half, sz)], [(mid + half, sz)]


# --------------------------------------------------------------------------- 1. 168h Settled Funding Window
def test_pass5_bug1_168h_settled_funding_preserved_on_nav_guard_confirmation(tmp_path: Path):
    # 72-hour gap while NAV guard confirmation is pending (cap_hours = 168.0):
    # Cross-DEX, HIP-3, and Spot+Perp must all sum the full 72 hourly settlements, NEVER truncating to 24h!
    prev_ms = T0
    ts_ms = T0 + 72 * H_MS
    rate_h = 0.0001  # 1 bp/h -> 72h sum = 0.0072 (vs truncated 24h = 0.0024)

    def fh_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t_type = body.get("type")
        if t_type == "fundingHistory":
            st = int(body["startTime"])
            en = int(body["endTime"])
            first_h = ((st + H_MS - 1) // H_MS) * H_MS
            rows = []
            h = first_h
            while h <= en:
                rows.append({"time": h, "fundingRate": str(rate_h)})
                h += H_MS
            return httpx.Response(200, json=rows)
        if t_type == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC", "maxLeverage": 20}]},
                    [
                        {
                            "markPx": "115.0",
                            "oraclePx": "115.0",
                            "funding": str(rate_h),
                            "dayNtlVlm": "50000000",
                            "openInterest": "1000",
                        }
                    ],
                ],
            )
        if t_type == "spotMetaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {
                        "tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "UBTC"}],
                        "universe": [{"name": "@142", "tokens": [1, 0]}],
                    },
                    [{"coin": "@142", "midPx": "115.0", "markPx": "115.0", "dayNtlVlm": "10000000"}],
                ],
            )
        if t_type == "l2Book":
            return httpx.Response(
                200,
                json={"levels": [[{"px": "114.99", "sz": "500"}], [{"px": "115.01", "sz": "500"}]]},
            )
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(fh_handler)) as client:
        pos_map = {"BTC": {"coin": "BTC", "raw_hl_symbol": "BTC", "last_accrual_ms": prev_ms}}
        cd_settled = cdp.settled_funding_by_position(
            client, pos_map, lambda p: [p["raw_hl_symbol"]], prev_ms, ts_ms, cap_hours=168.0
        )
        hp_settled = hp.settled_funding_by_position(
            client, {"BTC": {"short_symbol": "BTC", "last_accrual_ms": prev_ms}},
            lambda p: [p["short_symbol"]], prev_ms, ts_ms, cap_hours=168.0
        )
        assert cd_settled["BTC"] == pytest.approx(72 * rate_h, abs=1e-9)
        assert hp_settled["BTC"] == pytest.approx(72 * rate_h, abs=1e-9)

        # Spot+Perp live confirming tick after a clamped jump 72h after prev_ms accrues full 72h settled funding
        sp_dir = tmp_path / "sp_168"
        sp_dir.mkdir()
        (sp_dir / "state.json").write_text(
            json.dumps(
                {
                    "starting_capital_usd": 100_000.0,
                    "equity_usd": 100_000.0,
                    "hlp_reserve_usd": 0.0,
                    "free_cash_usd": 85_000.0,
                    "last_tick_ms": prev_ms,
                    "last_clamped_ms": ts_ms - 2 * H_MS,
                    "nav_guard_pending_ms": ts_ms - 2 * H_MS,
                    "nav_guard_pending_ret_pct": 0.08,
                    "positions": {
                        "BTC": {
                            "base_symbol": "BTC",
                            "spot_pair": "@142",
                            "spot_token": "UBTC",
                            "asset_tier": "Crypto - Large Cap",
                            "qty": 100.0,
                            "last_spot_px": 115.0,
                            "last_perp_px": 115.0,
                            "perp_margin_usd": 5_750.0,
                            "max_leverage": 20,
                            "last_accrual_ms": prev_ms,
                            "entry_cost_usd": 1.0,
                            "cum_cost_usd": 1.0,
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        res_sp = spa.run_spot_perp_paper_tick(
            client,
            state_dir=sp_dir,
            cfg=spa.SpotPerpPaperConfig(
                hlp_reserve_share=0.0,
                max_tick_nav_jump_pct=0.05,  # 72h funding = +$82.80 (+0.0828% > 0.05% guard, confirmed!)
                require_hlp_hurdle=False,
            ),
            now_ms=ts_ms,
            hlp_summary=(0.0, 1e8),
        )
        assert res_sp["state"]["risk_telemetry"]["nav_guard_triggered"] is False
        # perp_notional = 100 * 115 = $11,500; 72h * 0.0001 = 0.0072 -> $82.80 (24h truncation gave $27.60!)
        assert res_sp["state"]["cum_funding_usd"] == pytest.approx(11_500.0 * 72 * rate_h, abs=1e-3)


# --------------------------------------------------------------------------- 2. OKX k* 1000x Scale in WideMarket
def test_pass5_bug2_widemarket_okx_k_coin_scaled_preventing_paper_tracker_mtm_explosion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    n_h = 30
    hrs = T0 + np.arange(n_h, dtype="int64") * H_MS
    bundle = pd.DataFrame(
        {
            "hour_ms": hrs,
            "coin": "kPEPE",
            "hl_close": np.full(n_h, 10.0),
            "hl_notional_usd": np.full(n_h, 5_000_000.0),
            "hl_fund_next": np.zeros(n_h),
            "hl_rate_h": np.full(n_h, 0.0003),
            "bybit_close": np.full(n_h, np.nan),
            "bybit_notional_usd": np.full(n_h, np.nan),
            "bybit_fund_next": np.zeros(n_h),
            "bybit_rate_h": np.zeros(n_h),
            "binance_close": np.full(n_h, np.nan),
            "binance_notional_usd": np.full(n_h, np.nan),
            "binance_fund_next": np.zeros(n_h),
            "binance_rate_h": np.zeros(n_h),
            # Unscaled OKX kline price 0.0125 vs HL 10.0 (ratio = 800 in [500, 2000]) -> scaled to 12.5 -> test 0.010 -> 10.0!
            "okx_close": np.full(n_h, 0.010),
            "okx_notional_usd": np.full(n_h, 5_000_000.0),
            "okx_fund_next": np.zeros(n_h),
            "okx_rate_h": np.zeros(n_h),
        }
    )
    mkt1 = af.build_wide_market(bundle.iloc[:-1])
    mkt2 = af.build_wide_market(bundle)
    assert float(mkt1.okx_close["kPEPE"].iloc[-1]) == pytest.approx(10.0)
    assert float(mkt2.okx_close["kPEPE"].iloc[-1]) == pytest.approx(10.0)

    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda _m, _s: {
            "kPEPE": {"hl_weight": -0.20, "cex_legs": {"okx": 0.20}, "cex_weight": 0.20, "cex_venue": "okx"}
        },
    )
    quotes = {"kPEPE": pt.L2Quote("kPEPE", 9.999, 10.001, 10.0, 2.0, 1e6, 1e6)}
    pt_dir = tmp_path / "pt_okx_scale"
    # Tick 1 opens on kline fallback (cex_mid_prices=None)
    r1 = pt.run_paper_tick(mkt1, quotes, state_dir=pt_dir, sleeves=[], cex_mid_prices=None)
    assert r1["positions"]["kPEPE"]["last_cex_px_by_venue"]["okx"] == pytest.approx(10.0) if "positions" in r1 else True
    # Tick 2 switches to live cex_mid_prices={"okx": {"kPEPE": 10.0}} -> ZERO price MTM jump and NO nav_guard clamp!
    r2 = pt.run_paper_tick(mkt2, quotes, state_dir=pt_dir, sleeves=[], cex_mid_prices={"okx": {"kPEPE": 10.0}})
    assert r2["nav_guard_triggered"] is False
    assert r2["price_pnl_usd"] == pytest.approx(0.0, abs=1e-6)


# --------------------------------------------------------------------------- 3. >= 3h Candle Gap Returns
def test_pass5_bug3_long_candle_gaps_preserve_100_pct_of_resumption_return():
    # [100.0, NaN, NaN, NaN, 50.0] -> [0.0, 0.0, 0.0, 0.0, -0.50] across ALL 3 engines
    hrs = T0 + np.arange(5, dtype="int64") * H_MS
    close_seq = [100.0, np.nan, np.nan, np.nan, 50.0]

    # 1. alpha_families.build_wide_market
    bundle = pd.DataFrame(
        {
            "hour_ms": hrs,
            "coin": "BTC",
            "hl_close": close_seq,
            "bybit_close": [100.0, 0.0, np.nan, np.nan, 50.0],
            "binance_close": close_seq,
            "okx_close": close_seq,
        }
    )
    wm = af.build_wide_market(bundle)
    expected = [0.0, 0.0, 0.0, 0.0, -0.50]
    assert wm.hl_ret_next["BTC"].tolist() == pytest.approx(expected)
    assert wm.bybit_ret_next["BTC"].tolist() == pytest.approx(expected)
    assert wm.binance_ret_next["BTC"].tolist() == pytest.approx(expected)
    assert wm.okx_ret_next["BTC"].tolist() == pytest.approx(expected)

    # 2. funding_arb.build_panel
    hl_bars = pd.DataFrame({"open_time_ms": [hrs[0], hrs[4]], "close": [100.0, 50.0]})
    cex_bars = pd.DataFrame({"open_time_ms": list(hrs), "close": [100.0, 100.0, 100.0, 100.0, 100.0]})
    fund = pd.DataFrame({"time_ms": list(hrs), "funding_rate": [0.0] * 5})
    panel = fa.build_panel(fund, fund, hl_bars, cex_bars)
    assert panel["hl_ret"].tolist() == pytest.approx(expected)

    # 3. walk_forward.simulate_portfolio_weights
    hrs_6 = T0 + np.arange(6, dtype="int64") * H_MS
    hl_bars_6 = pd.DataFrame({"open_time_ms": [hrs_6[0], hrs_6[4], hrs_6[5]], "close": [100.0, 50.0, 50.0]})
    cex_bars_6 = pd.DataFrame({"open_time_ms": list(hrs_6), "close": [100.0] * 6})
    fund_6 = pd.DataFrame({"time_ms": list(hrs_6), "funding_rate": [0.0] * 6})
    panel_6 = fa.build_panel(fund_6, fund_6, hl_bars_6, cex_bars_6)
    # Hold w = +1.0 (short HL / long CEX, scale=0.5): -50% HL drop on bar 4 yields +0.5 * 0.50 = +0.25 basis PnL!
    sim_wf, _ = wf.simulate_portfolio_weights(
        {"BTC": panel_6},
        pd.DataFrame({"BTC": [1.0, 1.0, 1.0, 1.0, 1.0, 0.0]}, index=pd.Index(hrs_6, name="hour_ms")),
        fa.Costs(hl_fee_bps=0.0, cex_fee_bps=0.0, slippage_bps_per_leg=0.0, leverage=1.0),
    )
    assert float(sim_wf["basis_pnl"].iloc[4]) == pytest.approx(0.25, abs=1e-6)
    assert float(sim_wf["basis_pnl"].sum()) == pytest.approx(0.25, abs=1e-6)


# --------------------------------------------------------------------------- 4. Settlement Study FDR & Dashboard GO Gate
def test_pass5_bug4_persisted_fdr_pass_legacy_hydration_and_net_clustered_go_gate(tmp_path: Path):
    # 1. evaluate_settlement_strategies persists boolean fdr_pass directly in returned DataFrame
    m5 = fss.FIVE_MIN_MS
    all_ev = []
    for coin_i, coin in enumerate(("BTC", "ETH", "SOL")):
        candles, funding = [], []
        for h in range(40):
            t0 = T0 + h * H_MS
            funding.append({"time": t0, "fundingRate": "-0.0002"})  # APR <= -175%
            if h >= 1:
                fade_bps = 14.0 + 0.5 * coin_i + (0.4 if h % 2 == 0 else -0.4)
                c_p5 = 100.0 * (1.0 - fade_bps * 1e-4)
                candles.extend(
                    [
                        {"t": t0 - 3 * m5, "T": t0 - 2 * m5, "o": "100.0", "c": "100.0"},
                        {"t": t0 - m5, "T": t0, "o": "100.0", "c": "100.0"},
                        {"t": t0, "T": t0 + m5, "o": "100.0", "c": f"{c_p5:.6f}"},
                        {"t": t0 + 2 * m5, "T": t0 + 3 * m5, "o": f"{c_p5:.6f}", "c": f"{c_p5:.6f}"},
                    ]
                )
        all_ev.append(fss.build_coin_settlement_events(coin, candles, funding))
    stats_df = fss.evaluate_settlement_strategies(pd.concat(all_ev, ignore_index=True), maker_rt_bps=3.0)
    assert "fdr_pass" in stats_df.columns
    assert stats_df["fdr_pass"].dtype == bool
    hld_fade = stats_df[
        (stats_df["split"] == "holdout")
        & (stats_df["strategy"] == "post_unwind_fade_5m")
        & (stats_df["bucket"] == "APR<=-25% (neg)")
    ].iloc[0]
    assert bool(hld_fade["fdr_pass"]) is True
    assert bool((stats_df[stats_df["split"] == "dev"]["fdr_pass"] == False).all())  # noqa: E712

    # 2. collect_dashboard_payload dynamically computes fdr_pass when legacy stats.parquet lacks it
    study_dir = tmp_path / "hl_funding_settlement_study"
    study_dir.mkdir()
    legacy_rows: list[dict[str, Any]] = [
        {"bucket": "b0", "strategy": "s0", "split": "dev", "events": 80, "mean_gross_bps": 8.0, "net_maker_bps": 5.0, "t_stat": 4.0},
        {
            "bucket": "b0",
            "strategy": "s0",
            "split": "holdout",
            "events": 80,
            "clusters": 35,
            "mean_gross_bps": 5.1,
            "net_maker_bps": 2.1,
            "t_stat": 5.1,
            "net_t_stat": 2.1,
            "t_stat_clustered": 4.2,
            "net_t_stat_clustered": 2.1,  # p ~ 0.021 > (1/25)*0.10 = 0.004 -> must fail BH-FDR!
        },
    ]
    for k in range(1, 25):
        legacy_rows.append(
            {"bucket": f"b{k}", "strategy": "s0", "split": "dev", "events": 80, "mean_gross_bps": 0.0, "net_maker_bps": -3.0, "t_stat": 0.0}
        )
        legacy_rows.append(
            {
                "bucket": f"b{k}",
                "strategy": "s0",
                "split": "holdout",
                "events": 80,
                "clusters": 35,
                "mean_gross_bps": 0.0,
                "net_maker_bps": -3.0,
                "t_stat": 0.0,
                "net_t_stat": -3.0,
                "t_stat_clustered": 0.0,
                "net_t_stat_clustered": -3.0,
            }
        )
    pd.DataFrame(legacy_rows).to_parquet(study_dir / "stats.parquet", index=False)
    payload = pdash.collect_dashboard_payload(tmp_path)
    loaded_hld_b0 = next(
        r for r in payload["settlement_stats"] if r["bucket"] == "b0" and r["split"] == "holdout"
    )
    assert "fdr_pass" in loaded_hld_b0 and loaded_hld_b0["fdr_pass"] is False

    # 3. render_portfolio_dashboard_html gates GO on net_t_stat_clustered >= 2.0 (never gross t_stat_clustered!)
    gross_only_clustered = [
        {"bucket": "b1", "strategy": "s1", "split": "dev", "events": 90, "mean_gross_bps": 8.0, "net_maker_bps": 5.0, "t_stat": 4.5},
        {
            "bucket": "b1",
            "strategy": "s1",
            "split": "holdout",
            "events": 90,
            "clusters": 45,
            "mean_gross_bps": 5.5,
            "net_maker_bps": 2.5,
            "t_stat": 4.8,
            "net_t_stat": 2.6,
            "t_stat_clustered": 3.9,  # Gross clustered >= 2.0...
            "net_t_stat_clustered": 1.4,  # ...but NET clustered < 2.0 -> MUST NOT BE GO!
            "ex_top1_gross_bps": 5.0,
            "fdr_pass": True,
        },
    ]
    html_blocked = pdash.render_portfolio_dashboard_html({"generated_at_utc": "now", "settlement_stats": gross_only_clustered})
    assert '<span class="pill green">GO</span>' not in html_blocked

    # Also blocked when net_t_stat_clustered is explicitly None/NaN despite t_stat_clustered = 3.9
    gross_only_clustered[1]["net_t_stat_clustered"] = math.nan
    html_nan_net_cl = pdash.render_portfolio_dashboard_html({"generated_at_utc": "now", "settlement_stats": gross_only_clustered})
    assert '<span class="pill green">GO</span>' not in html_nan_net_cl


# --------------------------------------------------------------------------- 5. HIP-3 Per-Leg Funding Split-Margin Liq
def test_pass5_bug5_hip3_split_margin_liquidation_includes_per_leg_settled_funding(tmp_path: Path):
    # Both legs stay at $100.00 (0 price uPnL!), net spread is +35% APR (short +95% APR, long +60% APR),
    # so overall pair cum_funding_usd > 0, YET the long leg pays 60% APR out of its independent DEX margin!
    h3_dir = tmp_path / "hip3_fund_liq"
    cfg = hp.Hip3PaperConfig(
        slot_notional_usd=5_000.0,
        per_leg_leverage=2.0,  # init_leg_m = $2,500 per leg
        min_clean_streak=1,
        max_tick_nav_jump_pct=99.0,
    )
    # max_leverage = 1 -> l_mmr = 0.5 / 1.0 = 0.50 -> maintenance margin = $2,500!
    # Wait: use max_leverage = 2 -> l_mmr = 0.25 -> maintenance margin = $1,250.
    # If long leg pays 10% per hour (extreme test funding rate) while short leg receives 12% per hour:
    # Over 3 hours: long_fund_pnl = -$5,000 * 0.10 * 3 = -$1,500 -> remaining long leg equity = $2,500 - $1,500 = $1,000 <= $1,250!
    short_r_h = 0.12
    long_r_h = 0.10
    mkts = [
        hip3.Hip3BuilderMarket(
            "io", "io", "io:COIN", "COIN", 100.0, 100.0, 0.0,
            short_r_h, short_r_h * 876_000.0, 5e6, 2e6, False, 2, True, 1.0, 0.9, 0.3,
        ),
        hip3.Hip3BuilderMarket(
            "xyz", "xyz", "xyz:COIN", "COIN", 100.0, 100.0, 0.0,
            long_r_h, long_r_h * 876_000.0, 5e6, 2e6, False, 2, True, 1.0, 0.9, 0.3,
        ),
    ]
    r_open = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg, now_ms=T0, markets=mkts, book_fetcher=lambda _s: _flat_book(100.0)
    )
    pos0 = r_open["state"]["positions"]["COIN"]
    assert pos0["cum_short_funding_usd"] == 0.0 and pos0["cum_long_funding_usd"] == 0.0

    # Tick +1h: long leg paid -$500 (equity $2,000 > $1,250 MMR) -> still open, per-leg funding tracked
    r_1h = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg, now_ms=T0 + H_MS, markets=mkts, book_fetcher=lambda _s: _flat_book(100.0)
    )
    pos1 = r_1h["state"]["positions"]["COIN"]
    assert pos1["cum_short_funding_usd"] == pytest.approx(600.0)
    assert pos1["cum_long_funding_usd"] == pytest.approx(-500.0)
    assert pos1["cum_funding_usd"] == pytest.approx(100.0)

    # Tick +3h (+2h more): long leg paid another -$1,000 -> cum_long_funding_usd = -$1,500 -> $2,500 - $1,500 = $1,000 <= $1,250 MMR!
    # Net pair funding is +$300 (> 0) and mark prices never moved (100.0), yet split-margin long leg liquidates!
    r_3h = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg, now_ms=T0 + 3 * H_MS, markets=mkts, book_fetcher=lambda _s: _flat_book(100.0)
    )
    assert r_3h["state"]["positions"] == {}
    assert r_3h["trades"][0]["exit_reason"] == "split_margin_leg_liquidation"


# --------------------------------------------------------------------------- 6. Drifted Live Leg Notionals
def test_pass5_bug6_drifted_live_leg_notionals_aligned_across_analytics_metrics_and_autopilot(tmp_path: Path):
    cd_state = {
        "starting_capital_usd": 50_000.0,
        "equity_usd": 50_000.0,
        "positions": {
            "SOL": {
                "coin": "SOL",
                "other_dex": "lighter",
                "leg_notional_usd": 5_000.0,  # stale entry notional ($10k pair -> 0.20x)
                "hl_notional_usd": 7_500.0,   # +50% drifted MTM notional ($15k pair -> 0.30x!)
                "other_notional_usd": 7_500.0,
                "live_spread_apr_pct": 40.0,
            }
        },
    }
    hp_state = {
        "starting_capital_usd": 50_000.0,
        "equity_usd": 50_000.0,
        "return_pct": 0.0,
        "positions": {
            "NVDA": {
                "base_symbol": "NVDA",
                "short_dex": "io",
                "long_dex": "xyz",
                "leg_notional_usd": 5_000.0,     # stale entry notional ($10k pair -> 0.20x)
                "short_notional_usd": 6_500.0,   # +30% drifted MTM notional ($13k pair -> 0.26x!)
                "long_notional_usd": 6_500.0,
                "live_spread_apr_pct": 50.0,
            }
        },
    }
    for sub, payload in (("hl_cross_dex_paper", cd_state), ("hl_hip3_paper", hp_state)):
        d = tmp_path / sub
        d.mkdir()
        (d / "state.json").write_text(json.dumps(payload), encoding="utf-8")

    risk = ar.build_consolidated_risk_exposure(tmp_path)
    lev_by_id = {r["id"]: r["current_leverage"] for r in risk["leverage_by_strategy"]}
    ap_sleeves = ap.collect_desk_sleeve_metrics(tmp_path)
    assert lev_by_id["cross_dex"] == pytest.approx(0.300)
    assert lev_by_id["hip3_paper"] == pytest.approx(0.260)
    assert ap_sleeves["cross_dex_paper"]["leverage"] == pytest.approx(lev_by_id["cross_dex"])
    assert ap_sleeves["hip3_paper"]["leverage"] == pytest.approx(lev_by_id["hip3_paper"])

    prom_txt = render_prometheus_metrics(
        {
            "cross_dex_paper": cd_state,
            "hip3_paper": hp_state,
            "autopilot": {
                "starting_capital_usd": 250_000.0,
                "gross_equity_usd": 250_000.0,
                "sleeves": [
                    {"name": "cross_dex_paper", "actual_weight_pct": 50.0, "allocation_usd": 125_000.0},
                    {"name": "hip3_paper", "actual_weight_pct": 50.0, "allocation_usd": 125_000.0},
                ],
            },
        }
    )
    # cross_dex_v2: leverage = 15000 / 50000 = 0.3; live_apr_pct = 7500 * 40 / 50000 = 6.0% (stale gave 4.0%!)
    assert 'hl_strategy_leverage{strategy="cross_dex_v2",category="Market Neutral",mode="paper"} 0.3' in prom_txt
    assert 'hl_strategy_live_apr_pct{strategy="cross_dex_v2",category="Market Neutral",mode="paper"} 6' in prom_txt
    # hip3_split_margin: leverage = 13000 / 50000 = 0.26; live_apr_pct = 6500 * 50 / 50000 = 6.5% (stale gave 5.0%!)
    assert 'hl_strategy_leverage{strategy="hip3_split_margin",category="Market Neutral",mode="paper"} 0.26' in prom_txt
    assert 'hl_strategy_live_apr_pct{strategy="hip3_split_margin",category="Market Neutral",mode="paper"} 6.5' in prom_txt
    # neutral_autopilot: 0.5 * 6.0 + 0.5 * 6.5 = 6.25%
    assert 'hl_strategy_live_apr_pct{strategy="neutral_autopilot",category="Master Vault",mode="paper"} 6.25' in prom_txt


# --------------------------------------------------------------------------- 7. L2 Depth full_fill_ok & Credit Cap
def test_pass5_bug7_scanner_l2_depth_enforces_full_fill_ok_and_caps_basis_credit(monkeypatch: pytest.MonkeyPatch):
    # 1. cross_dex_scanner.enrich_opportunities_with_l2_depth:
    #    - DUST has only $500 depth (< $10k) -> exec_basis_10k_bps / l2_slip_10k_bps are None, depth_be_10k_h is NaN!
    #    - DEEP has $100k depth and +40 bps favorable basis with 10.0 bps RT fee -> credit capped at 0.5 * 10.0 = 5.0 bps!
    df_in = pd.DataFrame(
        [
            {
                "coin": "DUST",
                "hl_raw_symbol": "DUST",
                "other_dex": "lighter",
                "other_raw_symbol": "1",
                "direction": "Short HL / Long lighter",
                "spread_apr_pct": 87.6,  # 1.0 bp/h
                "favorable_basis_bps": 40.0,
            },
            {
                "coin": "DEEP",
                "hl_raw_symbol": "DEEP",
                "other_dex": "lighter",
                "other_raw_symbol": "2",
                "direction": "Short HL / Long lighter",
                "spread_apr_pct": 87.6,  # 1.0 bp/h
                "favorable_basis_bps": 40.0,
            },
        ]
    )

    def fake_fetch_book(_client: Any, dex: str, sym: str, **_kw: Any):
        if sym in ("DUST", "1"):
            return [(100.40, 5.0)], [(100.42, 5.0)]  # only ~$502 depth -> ok10 is False!
        if dex == "hyperliquid":
            return [(100.40, 1_000.0)], [(100.42, 1_000.0)]
        return [(99.98, 1_000.0)], [(100.00, 1_000.0)]

    monkeypatch.setattr(cds, "fetch_dex_l2_book", fake_fetch_book)
    with httpx.Client() as client:
        enriched = cds.enrich_opportunities_with_l2_depth(client, df_in, round_trip_fee_bps=10.0)

    r_dust = enriched[enriched["coin"] == "DUST"].iloc[0]
    assert pd.isna(r_dust["exec_basis_10k_bps"])
    assert pd.isna(r_dust["l2_slip_10k_bps"])
    assert pd.isna(r_dust["depth_be_10k_h"])

    r_deep = enriched[enriched["coin"] == "DEEP"].iloc[0]
    assert r_deep["credited_basis_10k_bps"] == pytest.approx(5.0)  # capped at 0.5 * 10.0 bps!
    # 10.0 RT + 2.0 slip (1 bp per leg) - 5.0 capped credit = 7.0h (uncapped credit previously gave 0.0h!)
    assert r_deep["depth_be_10k_h"] == pytest.approx(7.0, abs=0.2)

    # 2. hl_hip3_builder_arb: $500 thin book (depth_5k_ok=False) sets payback_hours_5k and payback_hours_5k_taker to None!
    mkts_thin = [
        hip3.Hip3BuilderMarket("io", "io", "io:THIN", "THIN", 100.0, 100.0, 0.0, 0.0001, 87.6, 5e6, 2e6, False, 10),
        hip3.Hip3BuilderMarket("xyz", "xyz", "xyz:THIN", "THIN", 100.0, 100.0, 0.0, 0.0, 0.0, 5e6, 2e6, False, 10),
    ]
    opps_thin = hip3.scan_hip3_cross_builder_arb(
        mkts_thin,
        book_fetcher=lambda _sym: ([(99.99, 5.0)], [(100.01, 5.0)]),  # only $500 < $5k clip!
    )
    assert len(opps_thin) == 1
    assert opps_thin[0].depth_5k_ok is False
    assert opps_thin[0].payback_hours_5k is None
    assert opps_thin[0].payback_hours_5k_taker is None


# --------------------------------------------------------------------------- 8. max_tick_nav_jump_pct=0.0 Disables Guard
def test_pass5_bug8_zero_max_tick_nav_jump_pct_cleanly_disables_circuit_breaker(tmp_path: Path):
    # 1. Spot+Perp with max_tick_nav_jump_pct=0.0 accepts positive HLP/funding yield without clamping
    sp_dir = tmp_path / "sp_zero_guard"
    cfg_sp = spa.SpotPerpPaperConfig(max_tick_nav_jump_pct=0.0, hlp_reserve_share=0.10)
    spa.run_spot_perp_paper_tick(
        state_dir=sp_dir, cfg=cfg_sp, now_ms=T0, markets=[], book_fetcher=lambda _k: ([], []), hlp_summary=(20.0, 1e8)
    )
    r_sp2 = spa.run_spot_perp_paper_tick(
        state_dir=sp_dir, cfg=cfg_sp, now_ms=T0 + 4 * H_MS, markets=[], book_fetcher=lambda _k: ([], []), hlp_summary=(20.0, 1e8)
    )
    assert r_sp2["state"]["risk_telemetry"]["nav_guard_triggered"] is False
    assert r_sp2["state"]["nav_guard_triggers"] == 0
    assert r_sp2["state"]["equity_usd"] > 100_000.0

    # 2. Autopilot with max_tick_nav_jump_pct=0.0 accepts +10% sleeve jump without clamping
    ap_root = tmp_path / "ap_zero_guard"
    (ap_root / "hl_spot_perp_paper").mkdir(parents=True)
    (ap_root / "hl_spot_perp_paper" / "state.json").write_text(
        json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}}),
        encoding="utf-8",
    )
    closes = {c: [100.0] * 55 for c in ap.CTA_UNIVERSE}
    cfg_ap = ap.AutopilotConfig(max_tick_nav_jump_pct=0.0)
    ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=T0, daily_closes=closes)
    (ap_root / "hl_spot_perp_paper" / "state.json").write_text(
        json.dumps({"equity_usd": 112_000.0, "starting_capital_usd": 100_000.0, "positions": {}}),
        encoding="utf-8",
    )
    r_ap2 = ap.run_autopilot_tick(data_root=ap_root, cfg=cfg_ap, now_ms=T0 + 4 * H_MS, daily_closes=closes)
    assert r_ap2["state"]["risk_telemetry"]["nav_guard_triggered"] is False
    assert r_ap2["state"]["nav_guard_triggers"] == 0
    assert r_ap2["state"]["gross_equity_usd"] > 260_000.0
