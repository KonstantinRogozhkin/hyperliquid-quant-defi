from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

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
from hl_quant import hl_hyperevm_yield_stack as hys
from hl_quant import hl_liquidation_and_funding_alpha as liq
from hl_quant import hl_microstructure as hm
from hl_quant import hl_onchain_event_alpha as oea
from hl_quant import hl_spot_perp_arb as spa
from hl_quant import hl_tear_sheet as ts
from hl_quant import metamodel as mm
from hl_quant import paper_tracker as pt
from hl_quant import walk_forward as wf

H = fa.HOUR_MS
DAY_MS = 24 * H
# Exact 00:00 UTC epoch boundary
UTC_MIDNIGHT = 1_795_824_000_000 - (1_795_824_000_000 % DAY_MS)


def test_c5_cross_sectional_weights_canonical_utc_boundaries_and_best_cex_notional():
    # Build 600 hours starting at 00:00 UTC with deterministic changing scores every hour
    n_h = 600
    hours = UTC_MIDNIGHT + np.arange(n_h, dtype="int64") * H
    coins = ["C0", "C1", "C2", "C3"]
    rng = np.random.default_rng(42)
    scores_mat = rng.normal(0.0, 1.0, size=(n_h, len(coins)))
    score_full = pd.DataFrame(scores_mat, index=hours, columns=coins)
    mask_full = pd.DataFrame(True, index=hours, columns=coins)

    # Slice two 480h windows ending at 07:00 UTC vs 11:00 UTC on the same day (no 12h/24h boundary crossed!)
    # End index 511 -> hour 511 % 24 == 7 (07:00 UTC); end index 515 -> hour 515 % 24 == 11 (11:00 UTC)
    w_07_r12 = af.cross_sectional_weights(
        score_full.iloc[512 - 480 : 512], mask_full.iloc[512 - 480 : 512], top_n=1, rebalance_h=12
    )
    w_11_r12 = af.cross_sectional_weights(
        score_full.iloc[516 - 480 : 516], mask_full.iloc[516 - 480 : 516], top_n=1, rebalance_h=12
    )
    # Neither crossed 12:00 UTC; both must hold exact 00:00 UTC (index 504) weights — zero 4h churn!
    pd.testing.assert_series_equal(w_07_r12.iloc[-1], w_11_r12.iloc[-1], check_names=False)

    # Crossing 12:00 UTC (end index 519 -> 15:00 UTC) updates immediately to index 516 (12:00 UTC)
    scores_mat[516] = [-10.0, -5.0, 5.0, 10.0]
    score_mod = pd.DataFrame(scores_mat, index=hours, columns=coins)
    w_15_r12 = af.cross_sectional_weights(
        score_mod.iloc[520 - 480 : 520], mask_full.iloc[520 - 480 : 520], top_n=1, rebalance_h=12
    )
    assert w_15_r12.iloc[-1]["C3"] == pytest.approx(0.5)
    assert w_15_r12.iloc[-1]["C0"] == pytest.approx(-0.5)

    # EMA < 1.0 also stays invariant to start_ms shifts
    ema_a = af.cross_sectional_weights(
        score_mod.iloc[520 - 480 : 520], mask_full.iloc[520 - 480 : 520], top_n=1, rebalance_h=12, ema_alpha=0.5
    )
    ema_b = af.cross_sectional_weights(
        score_mod.iloc[520 - 476 : 520], mask_full.iloc[520 - 476 : 520], top_n=1, rebalance_h=12, ema_alpha=0.5
    )
    pd.testing.assert_series_equal(ema_a.iloc[-1], ema_b.iloc[-1], check_names=False)

    # Small synthetic integer indices still fall back to i % rebalance_h == 0
    syn_scores = pd.DataFrame([[1.0, -1.0], [-1.0, 1.0], [-1.0, 1.0]], index=[0, 1, 2], columns=["A", "B"])
    syn_mask = pd.DataFrame(True, index=[0, 1, 2], columns=["A", "B"])
    syn_w = af.cross_sectional_weights(syn_scores, syn_mask, top_n=1, rebalance_h=2)
    assert syn_w.loc[0, "A"] == pytest.approx(0.5)
    assert syn_w.loc[1, "A"] == pytest.approx(0.5)
    assert syn_w.loc[2, "A"] == pytest.approx(-0.5)

    # M-12.4: best_cex refuses illiquid OKX/Binance (< min_vol_usd)
    rows = []
    for c_idx in range(2):
        c = f"C{c_idx}"
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours[:400],
                    "coin": c,
                    "hl_close": np.full(400, 100.0),
                    "hl_notional_usd": np.full(400, 500_000.0),
                    "hl_fund_next": np.full(400, 2e-4),
                    "hl_rate_h": np.full(400, 2e-4),
                    "bybit_close": np.full(400, 100.0),
                    "bybit_notional_usd": np.full(400, 500_000.0),
                    "bybit_fund_next": np.zeros(400),
                    "bybit_rate_h": np.zeros(400),
                    "binance_close": np.full(400, 100.0),
                    "binance_notional_usd": np.full(400, 1_000.0),  # $24k/day < $1M!
                    "binance_fund_next": np.full(400, -5e-4),
                    "binance_rate_h": np.full(400, -5e-4),
                    "okx_close": np.full(400, 100.0),
                    "okx_notional_usd": np.full(400, 1_000.0),  # $24k/day < $1M!
                    "okx_fund_next": np.full(400, -5e-4),
                    "okx_rate_h": np.full(400, -5e-4),
                }
            )
        )
    mkt = af.build_wide_market(pd.concat(rows, ignore_index=True))
    _, vc = af.build_funding_carry_weights(
        mkt, {"mode": "best_cex", "lookback_h": 24, "entry_apr": 0.05, "min_vol_usd": 1_000_000.0}
    )
    assert vc is not None
    assert (vc.iloc[-1] == "bybit").all()


def test_c6_h6_paper_tracker_feed_integrity_hl_deadband_and_tear_sheet(tmp_path: Path):
    n_h = 400
    hours = UTC_MIDNIGHT + np.arange(n_h, dtype="int64") * H
    rows = []
    for i, c in enumerate(["BTC", "ETH"]):
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours,
                    "coin": c,
                    "hl_close": np.full(n_h, 100.0),
                    "hl_notional_usd": np.full(n_h, 500_000.0),
                    "hl_fund_next": np.full(n_h, 2e-4 if i == 0 else -2e-4),
                    "hl_rate_h": np.full(n_h, 2e-4 if i == 0 else -2e-4),
                    "bybit_close": np.full(n_h, 100.0),
                    "bybit_notional_usd": np.full(n_h, 500_000.0),
                    "bybit_fund_next": np.zeros(n_h),
                    "bybit_rate_h": np.zeros(n_h),
                }
            )
        )
    mkt1 = af.build_wide_market(pd.concat(rows, ignore_index=True))
    quotes = {
        c: pt.L2Quote(coin=c, best_bid=99.98, best_ask=100.02, mid_px=100.0, spread_bps=4.0, top5_bid_usd=1e6, top5_ask_usd=1e6)
        for c in mkt1.coins
    }
    carry_spec = af.HypothesisSpec(
        "carry_bybit", "funding_carry_arb", "cross_venue", {"mode": "bybit", "lookback_h": 24, "entry_apr": 0.05, "min_vol_usd": 1e6}
    )
    mm_dir = tmp_path / "hl_metamodel"
    r1 = pt.run_paper_tick(
        mkt1,
        quotes,
        state_dir=mm_dir,
        sleeves=[pt.MetamodelSleeve(carry_spec, 1.0)],
        cex_mid_prices={"bybit": {"BTC": 100.0, "ETH": 100.0}},
    )
    assert r1["status"] == "ok"
    st1_raw = (mm_dir / "state.json").read_text()
    st1 = json.loads(st1_raw)
    assert st1["cum_costs_usd"] > 0.0
    assert "cum_funding_usd" in st1 and "cum_price_pnl_usd" in st1 and "nav_guard_triggered" in st1

    # Advance 1h: missing live CEX mid price when cex_mid_prices is provided must block MTM without mutating state.json!
    rows2 = [df.assign(hour_ms=df["hour_ms"] + H) for df in rows]
    mkt2 = af.build_wide_market(pd.concat(rows2, ignore_index=True))
    r_miss_cex = pt.run_paper_tick(
        mkt2,
        quotes,
        state_dir=mm_dir,
        sleeves=[pt.MetamodelSleeve(carry_spec, 1.0)],
        cex_mid_prices={"bybit": {"ETH": 100.0}},  # BTC missing!
    )
    assert r_miss_cex["status"] == "held_coins_missing"
    assert "BTC" in r_miss_cex["missing_coins"]
    assert (mm_dir / "state.json").read_text() == st1_raw

    # Missing HL live quote for held dual-leg position in synchronous mode must also block!
    r_miss_hl = pt.run_paper_tick(
        mkt2,
        {"ETH": quotes["ETH"]},
        state_dir=mm_dir,
        sleeves=[pt.MetamodelSleeve(carry_spec, 1.0)],
        cex_mid_prices={"bybit": {"BTC": 100.0, "ETH": 100.0}},
    )
    assert r_miss_hl["status"] == "held_coins_missing"
    assert (mm_dir / "state.json").read_text() == st1_raw

    # Valid synchronous tick advances and populates tear sheet telemetry
    r2 = pt.run_paper_tick(
        mkt2,
        quotes,
        state_dir=mm_dir,
        sleeves=[pt.MetamodelSleeve(carry_spec, 1.0)],
        cex_mid_prices={"bybit": {"BTC": 100.0, "ETH": 100.0}},
    )
    assert r2["status"] == "ok"
    tear = ts.collect_tear_sheet_data(tmp_path)
    p_info = tear["paper"]
    assert p_info is not None
    assert p_info["cum_funding_usd"] > 0.0
    assert p_info["cum_costs_usd"] > 0.0
    assert p_info["gross_hl"] > 0.0 and p_info["gross_cex"] > 0.0
    assert p_info["nav_guard_triggered"] is False

    # H-6: Rebalance deadband suppresses micro-turnover for HL-only sleeves (cex_legs == {})
    hl_only_dir = tmp_path / "hl_only"
    hl_only_dir.mkdir()
    tick2_ms = int(mkt2.hours[-1])
    (hl_only_dir / "state.json").write_text(
        json.dumps(
            {
                "starting_capital_usd": 100_000.0,
                "equity_usd": 100_000.0,
                "last_tick_hour_ms": tick2_ms - 4 * H,
                "tick_count": 1,
                "positions": {"BTC": {"hl_weight": 0.25, "cex_legs": {}, "cex_weight": 0.0, "cex_venue": "", "last_hl_px": 100.0}},
            }
        )
    )
    orig_compute = pt.compute_latest_target_positions
    pt.compute_latest_target_positions = lambda m, s: {
        "BTC": {"hl_weight": 0.258, "cex_legs": {}, "cex_weight": 0.0, "cex_venue": ""}
    }
    try:
        r_db = pt.run_paper_tick(mkt2, quotes, state_dir=hl_only_dir, sleeves=[], rebalance_deadband_weight=0.015)
    finally:
        pt.compute_latest_target_positions = orig_compute
    assert r_db["trades_count"] == 0
    assert r_db["turnover"] == 0.0


def test_h7_m8_autopilot_retries_benchmark_merge_cta_missing_coin_and_zero_pos_guard(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(ap.time, "sleep", lambda _s: None)
    # Pre-populate cached SOL benchmark history so a single-coin failure on PAXG or SOL never wipes cache
    bench_dir = tmp_path / "hl_benchmarks"
    bench_dir.mkdir(parents=True)
    (bench_dir / "daily_closes.json").write_text(
        json.dumps({"updated_at_utc": "old", "benchmarks": {"SOL": [{"date": "2026-11-01", "close": 180.0}]}}),
        encoding="utf-8",
    )

    calls: dict[str, int] = {}
    t0 = 1_795_800_000_000

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body.get("type") == "allMids":
            calls["mids"] = calls.get("mids", 0) + 1
            if calls["mids"] == 1:
                return httpx.Response(429, json={"error": "rate_limit"})
            return httpx.Response(200, json={"BTC": "95000", "PAXG": "3120"})
        coin = body["req"]["coin"]
        calls[coin] = calls.get(coin, 0) + 1
        if coin == "BTC" and calls[coin] == 1:
            return httpx.Response(503, json={"error": "busy"})
        if coin == "SOL":
            return httpx.Response(500, json={"error": "down"})
        bars = [
            {"t": t0 - (45 - i) * 86_400_000, "T": t0 - (44 - i) * 86_400_000, "c": str(100.0 * (1.01**i))}
            for i in range(44)
        ]
        return httpx.Response(200, json=bars)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        r1 = ap.run_autopilot_tick(client, data_root=tmp_path, now_ms=t0)

    assert calls["BTC"] == 2 and calls["mids"] == 2
    saved_bench = json.loads((bench_dir / "daily_closes.json").read_text())["benchmarks"]
    assert "SOL" in saved_bench and "BTC" in saved_bench  # SOL cache preserved despite 500s!
    assert r1["state"]["cta_state"]["assets"]["BTC"]["mark_px"] == pytest.approx(95_000.0)

    # Missing daily bars for held PAXG preserves weight (zero turnover) and still marks MTM via live_marks
    snap_missing = ap.compute_crisis_cta_weights(
        {"BTC": [100.0 * (1.01**i) for i in range(45)]},
        prev_weights={"BTC": 0.30, "PAXG": 0.25},
        cfg=ap.AutopilotConfig(cta_weight_deadband=0.50),
    )
    assert snap_missing["weights"]["PAXG"] == pytest.approx(0.25)
    assert snap_missing["turnover"] == pytest.approx(0.0)

    # _read_sleeve_tick_returns drops clamped rows, dedups timestamps, and 0-position HLP sleeve does not get inv_vol=50
    sp_dir = tmp_path / "hl_spot_perp_paper"
    cd_dir = tmp_path / "hl_cross_dex_paper"
    sp_dir.mkdir(parents=True)
    cd_dir.mkdir(parents=True)
    (sp_dir / "state.json").write_text(json.dumps({"equity_usd": 100_060, "starting_capital_usd": 100_000, "positions": {}}))
    (cd_dir / "state.json").write_text(json.dumps({"equity_usd": 100_300, "starting_capital_usd": 100_000, "positions": {"ETH": {"leg_notional_usd": 5000}}}))
    # Constant deterministic HLP increments on spot_perp (0 positions!) + a clamped row + duplicate timestamp
    pd.DataFrame(
        {
            "tick_ms": [t0 + i * 14_400_000 for i in [0, 1, 1, 2, 3, 4, 5, 6]],
            "equity_usd": [100_000, 100_010, 100_010, 100_020, 100_020, 100_030, 100_040, 100_050],
            "nav_guard_triggered": [False, False, False, False, True, False, False, False],
        }
    ).to_parquet(sp_dir / "ticks.parquet", index=False)
    pd.DataFrame(
        {
            "ts_ms": [t0 + i * 14_400_000 for i in range(7)],
            "equity_usd": [100_000, 100_080, 100_020, 100_140, 100_190, 100_250, 100_300],
        }
    ).to_parquet(cd_dir / "ticks.parquet", index=False)

    rets_sp = ap._read_sleeve_tick_returns(sp_dir / "ticks.parquet")
    assert len(rets_sp) == 5  # 8 rows -> -1 clamped -> -1 duplicate -> 6 points -> 5 returns
    sleeves = ap.collect_desk_sleeve_metrics(tmp_path)
    tw = ap.compute_autopilot_target_weights(sleeves, cta_active=True)
    # Active cross_dex sleeve with real positions & high Sharpe is not crushed by 0-position constant HLP sleeve
    assert tw["cross_dex_paper"] >= tw["spot_perp_paper"]


def test_h8_h14_settlement_study_keeps_genuine_squeezes_and_clears_last_attempt(tmp_path: Path, monkeypatch):
    t0 = UTC_MIDNIGHT + H
    m5 = fss.FIVE_MIN_MS
    funding = [{"time": UTC_MIDNIGHT, "fundingRate": "-0.0010"}, {"time": t0, "fundingRate": "-0.0010"}]
    # Genuine -12% (-1200 bps) post-settlement squeeze unwind + corrupt >90% glitch at next hour
    candles = [
        {"t": t0 - 3 * m5, "o": "100.0", "c": "100.0"},
        {"t": t0 - m5, "o": "100.0", "c": "101.0"},
        {"t": t0, "o": "101.0", "c": "88.88"},  # -1200 bps drop -> +1200 bps fade!
        {"t": t0 + 2 * m5, "o": "88.88", "c": "87.87"},
    ]
    ev = fss.build_coin_settlement_events("SQUEEZE", candles, funding)
    assert len(ev) == 1
    assert ev.iloc[0]["post_unwind_fade_5m"] == pytest.approx(1200.0, abs=5.0)

    # CLI healthy run sets degraded=False and deletes stale last_attempt.json
    import scripts.analytics.run_hl_funding_settlement_study as cli_fss

    out_dir = tmp_path / "study_out"
    out_dir.mkdir()
    (out_dir / "last_attempt.json").write_text(json.dumps({"degraded": True}), encoding="utf-8")

    async def _fake_fetch(**_kwargs):
        return ev, {
            "observed_at_ms": t0,
            "bar_interval": "5m",
            "coins_requested": 1,
            "coins_collected": 1,
            "coins_failed": [],
            "total_settlement_events": 1,
            "span_days": 1.0,
            "collection_ratio": 1.0,
            "degraded": False,
        }

    monkeypatch.setattr(fss, "fetch_settlement_dataset", _fake_fetch)
    res = CliRunner().invoke(cli_fss.app, ["--output-dir", str(out_dir), "--report-path", str(tmp_path / "rep.md")])
    assert res.exit_code == 0
    assert not (out_dir / "last_attempt.json").exists()
    meta_disk = json.loads((out_dir / "meta.json").read_text())
    assert meta_disk["degraded"] is False


def test_h9_hip3_standard_fee_formula_and_multi_builder_pair_search():
    assert hip3.compute_hip3_builder_fees_bps(is_growth=False, fee_scale=0.0) == (4.5, 1.5)
    assert hip3.compute_hip3_builder_fees_bps(is_growth=False, fee_scale=0.5) == (6.75, 2.25)
    assert hip3.compute_hip3_builder_fees_bps(is_growth=False, fee_scale=1.0) == (9.0, 3.0)
    assert hip3.compute_hip3_builder_fees_bps(is_growth=False, fee_scale=2.0) == (18.0, 6.0)
    assert hip3.compute_hip3_builder_fees_bps(is_growth=True, fee_scale=1.0) == (0.9, 0.3)

    # 3 builders on NVDA: rogue builder has colliding price 15.0 (10x off) and highest APR;
    # xyz (40% APR, 150.0) vs io (5% APR, 150.0) must still be discovered!
    mkts = [
        hip3.Hip3BuilderMarket("rogue", "Rogue", "rogue:NVDA", "NVDA", 15.0, 15.0, 0.0, 0.001, 876.0, 1e6, 1e6, True, 5),
        hip3.Hip3BuilderMarket("xyz", "XYZ", "xyz:NVDA", "NVDA", 150.0, 150.0, 0.0, 0.000045, 40.0, 1e7, 5e6, False, 20),
        hip3.Hip3BuilderMarket("io", "IO", "io:NVDA", "NVDA", 150.0, 150.0, 0.0, 0.000005, 5.0, 5e6, 2e6, False, 10),
    ]
    opps = hip3.scan_hip3_cross_builder_arb(mkts)
    assert len(opps) == 1
    assert opps[0].short_symbol == "xyz:NVDA" and opps[0].long_symbol == "io:NVDA"


def test_h10_spot_perp_held_depeg_nav_guard_hygiene_pagination_and_perp_liq(tmp_path: Path):
    # 1. include_bases retains held PAXG when XAUT0 depegs -15% (ratio 0.85 < 0.90) so proxy_basis_break closes it!
    def depeg_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t = body.get("type")
        if t == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "PAXG", "maxLeverage": 10, "onlyIsolated": False}]},
                    [{"markPx": "3600", "oraclePx": "3600", "funding": "0.00002", "dayNtlVlm": "5000000", "openInterest": "1000"}],
                ],
            )
        if t == "spotMetaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"tokens": [{"index": 0, "name": "USDC"}, {"index": 1, "name": "XAUT0"}], "universe": [{"name": "@182", "tokens": [1, 0]}]},
                    [{"coin": "@182", "midPx": "3060.0", "dayNtlVlm": "1000"}],  # -15% depeg & tiny vol!
                ],
            )
        if t == "fundingHistory":
            start_t = int(body["startTime"])
            if start_t == 1000:
                rows = [{"time": 1000 + i, "fundingRate": "0.0001"} for i in range(500)]
                return httpx.Response(200, json=rows)
            return httpx.Response(200, json=[{"time": 1600, "fundingRate": "0.0002"}])
        return httpx.Response(200, json={"levels": [[{"px": "3059", "sz": "100"}], [{"px": "3061", "sz": "100"}]]})

    with httpx.Client(transport=httpx.MockTransport(depeg_handler)) as client:
        assert spa.fetch_matched_spot_perp_markets(client) == []  # filtered when not held
        kept = spa.fetch_matched_spot_perp_markets(client, include_bases={"PAXG"})
        assert len(kept) == 1 and kept[0]["base_symbol"] == "PAXG"
        # Paginated funding sum collects 500 * 0.0001 + 0.0002 = 0.0502
        f_sum = spa.fetch_settled_funding_sum(client, ["PAXG"], 1000, 2000)
        assert f_sum["PAXG"] == pytest.approx(0.0502)

    # 2. Entry basis credit capped at mark_basis_bps (async L2 spike cannot fake fast payback)
    mkt_async = [
        {
            "base_symbol": "BTC",
            "spot_pair": "@142",
            "spot_token": "UBTC",
            "quote_token": "USDC",
            "is_proxy_hedge": False,
            "spot_mid_px": 60_000.0,
            "spot_day_vol_usd": 20_000_000.0,
            "perp_mark_px": 59_880.0,  # -20 bps mark basis!
            "perp_oracle_px": 60_000.0,
            "funding_rate_h": 10.0 / 100.0 / spa.HOURS_PER_YEAR,
            "perp_day_vol_usd": 500_000_000.0,
            "perp_oi_usd": 100_000_000.0,
            "only_isolated": False,
            "max_leverage": 40,
        }
    ]
    # Stale L2 book pretends +30 bps basis, but mark_basis_bps is -20 bps (< min_exec_basis_bps=-12) -> rejected!
    snap_async = spa.scan_spot_perp_opportunities(
        cfg=spa.SpotPerpPaperConfig(min_exec_basis_bps=-12.0),
        markets=mkt_async,
        book_fetcher=lambda k: ([(60_180.0, 10.0)], [(60_190.0, 10.0)]) if k == "BTC" else ([(59_995.0, 10.0)], [(60_000.0, 10.0)]),
        hlp_summary=(10.0, 1e8),
    )
    assert snap_async.opportunities[0].eligible is False
    assert "neg_basis" in snap_async.opportunities[0].reject_reason

    # 3. Depleted short perp after MARGIN_TOPUP force-closes with perp_margin_liquidation
    liq_dir = tmp_path / "sp_liq"
    cfg_liq = spa.SpotPerpPaperConfig(
        starting_capital_usd=20_000.0,
        hlp_reserve_share=0.0,
        min_cash_buffer_share=0.0,
        slot_equity_usd=20_000.0,
        target_perp_leverage=2.0,
        min_clean_streak=1,
        max_tick_nav_jump_pct=99.0,
        require_hlp_hurdle=False,
    )
    m_open = [dict(mkt_async[0], perp_mark_px=60_010.0, max_leverage=3)]
    t0 = 1_795_800_000_000
    r_open = spa.run_spot_perp_paper_tick(
        state_dir=liq_dir,
        cfg=cfg_liq,
        now_ms=t0,
        markets=m_open,
        book_fetcher=lambda _k: ([(60_000.0, 10.0)], [(60_010.0, 10.0)]),
        hlp_summary=(10.0, 1e8),
    )
    assert "BTC" in r_open["state"]["positions"]
    # Perp explodes +80% (free_cash == 0 so MARGIN_TOPUP cannot rescue it) -> perp_margin_liquidation!
    m_blowup = [dict(m_open[0], spot_mid_px=108_000.0, perp_mark_px=108_000.0)]
    r_liq = spa.run_spot_perp_paper_tick(
        state_dir=liq_dir,
        cfg=cfg_liq,
        now_ms=t0 + 4 * H,
        markets=m_blowup,
        book_fetcher=lambda _k: ([(107_990.0, 10.0)], [(108_010.0, 10.0)]),
        hlp_summary=(10.0, 1e8),
    )
    assert "BTC" not in r_liq["state"]["positions"]
    assert any(t["action"] == "CLOSE" and t["reason"] == "perp_margin_liquidation" for t in r_liq["trades_this_tick"])


def test_m9_m10_m11_m12_cross_dex_hip3_frontier_boros_and_yield_stack(tmp_path: Path):
    # 1. Cross-DEX _normalize_coin handles kPEPE/KPEPE/1000PEPE without corrupting KAVA/KAITO
    assert cds._normalize_coin("kPEPE") == "kPEPE"
    assert cds._normalize_coin("KPEPE") == "kPEPE"
    assert cds._normalize_coin("1000PEPE") == "kPEPE"
    assert cds._normalize_coin("KBONK") == "kBONK"
    assert cds._normalize_coin("KAVA") == "KAVA"
    assert cds._normalize_coin("KAITO") == "KAITO"
    assert cds.default_round_trip_fee_bps("lighter", bridge_drag_bps=1.5) == pytest.approx(10.5)
    assert cds.default_round_trip_fee_bps("paradex", bridge_drag_bps=1.5) == pytest.approx(16.5)

    # 2. HIP-3 paper dynamic margin_locked_usd, sub-hour gate, and NAV sanity guard
    t0 = 1_795_800_000_000
    h3_dir = tmp_path / "hip3_p"
    cfg_h3 = hp.Hip3PaperConfig(min_clean_streak=1, max_tick_nav_jump_pct=5.0)
    mkts_1 = [
        hip3.Hip3BuilderMarket("io", "io", "io:NVDA", "NVDA", 100.0, 100.0, 0.0, 0.00005, 43.8, 5e6, 2e6, False, 10, True, 1.0, 0.9, 0.3),
        hip3.Hip3BuilderMarket("xyz", "xyz", "xyz:NVDA", "NVDA", 100.0, 100.0, 0.0, 0.0, 0.0, 5e6, 2e6, False, 10, True, 1.0, 0.9, 0.3),
    ]
    r1 = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg_h3, now_ms=t0, markets=mkts_1, book_fetcher=lambda _s: ([(99.99, 500.0)], [(100.01, 500.0)])
    )
    assert r1["state"]["margin_locked_usd"] == pytest.approx(5_000.0)
    # Same-hour re-run +10m later is gated
    r_dup = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg_h3, now_ms=t0 + 600_000, markets=mkts_1, book_fetcher=lambda _s: ([(99.99, 500.0)], [(100.01, 500.0)])
    )
    assert r_dup["status"] == "duplicate_hour_skipped"

    # +20% synchronized rally (100 -> 120): net PnL ~0, but split-margin locked grows to $6,500!
    mkts_rally = [
        hip3.Hip3BuilderMarket("io", "io", "io:NVDA", "NVDA", 120.0, 120.0, 0.0, 0.00005, 43.8, 5e6, 2e6, False, 10, True, 1.0, 0.9, 0.3),
        hip3.Hip3BuilderMarket("xyz", "xyz", "xyz:NVDA", "NVDA", 120.0, 120.0, 0.0, 0.0, 0.0, 5e6, 2e6, False, 10, True, 1.0, 0.9, 0.3),
    ]
    r2 = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg_h3, now_ms=t0 + 4 * H, markets=mkts_rally, book_fetcher=lambda _s: ([(119.99, 500.0)], [(120.01, 500.0)])
    )
    assert r2["state"]["margin_locked_usd"] == pytest.approx(6_500.0)

    # Corrupt single-leg spike triggers HIP-3 NAV Sanity Guard
    mkts_corrupt = [
        hip3.Hip3BuilderMarket("io", "io", "io:NVDA", "NVDA", 250.0, 120.0, 0.0, 0.00005, 43.8, 5e6, 2e6, False, 10, True, 1.0, 0.9, 0.3),
        mkts_rally[1],
    ]
    r_guard = hp.run_hip3_paper_tick(
        state_dir=h3_dir, cfg=cfg_h3, now_ms=t0 + 8 * H, markets=mkts_corrupt, book_fetcher=lambda _s: ([(119.99, 500.0)], [(120.01, 500.0)])
    )
    assert r_guard["state"]["nav_guard_triggered"] is True
    assert r_guard["state"]["nav_guard_triggers"] == 1

    # 3. Frontier persistence resets clean_streak to 0 when latest tick has 0 HIP-3 pairs
    fp_dir = tmp_path / "fp"
    empty_boros = boros.BorosScannerSnapshot(t0, 0, 0, 0, [])
    empty_ys = hys.HyperEvmYieldSnapshot(
        hys.HyperEvmYieldInputs(t0, 35.0, 10.0, "—", None, 0.0, 2.0, 1e8, 2.0, 1e8, 5.0, 1e7, 1e8, 1e7, 9.0, 12.0, 1e8), []
    )
    snap_with_20 = hip3.Hip3ScannerSnapshot(
        t0,
        "NY_REGULAR",
        2,
        20,
        1e8,
        [
            hip3.Hip3CrossBuilderOpp(
                f"SYM{i}", f"xyz:SYM{i}", f"io:SYM{i}", "xyz", "io", 30.0, 5.0, 25.0, 0.0, 1e6, 1e6, 1e6, 1e6, False, True, 2.0, False
            )
            for i in range(18)  # 18 > 15: verifies [:15] truncation is gone!
        ],
        [],
    )
    s1 = fp.run_frontier_persistence_tick(data_root=fp_dir, now_ms=t0, hip3_snap=snap_with_20, boros_snap=empty_boros, yield_snap=empty_ys)
    assert s1["total_logged"]["hip3_rows"] == 18
    snap_empty = hip3.Hip3ScannerSnapshot(t0 + 4 * H, "NY_REGULAR", 2, 20, 1e8, [], [])
    s2 = fp.run_frontier_persistence_tick(data_root=fp_dir, now_ms=t0 + 4 * H, hip3_snap=snap_empty, boros_snap=empty_boros, yield_snap=empty_ys)
    assert all(r["active_in_latest_tick"] is False and r["clean_streak"] == 0 for r in s2["hip3_persistence"])

    # 4. Boros None bestBid/bestAsk preserved (not faked from midApr) + DeFiLlama max-TVL pool selection
    m_nobook = boros.parse_boros_market(
        {
            "marketId": 99,
            "imData": {"name": "Hyperliquid HYPE", "symbol": "HL-HYPE", "maturity": 1798156800},
            "metadata": {"underlyingSymbol": "HYPE", "isSpreadMarket": False},
            "data": {"timeToMaturity": 864000, "markApr": 0.12, "midApr": 0.12, "bestBid": None, "bestAsk": None, "notionalOI": 50000, "volume24h": 5000},
        }
    )
    assert m_nobook is not None
    assert m_nobook.best_bid_apr_pct is None and m_nobook.best_ask_apr_pct is None
    assert m_nobook.is_actionable is False

    def llama_handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "hyperliquid" in u:
            return httpx.Response(200, json=[{"universe": [{"name": "HYPE"}]}, [{"markPx": "35.0", "funding": "0.0001"}]])
        if "boros" in u:
            return httpx.Response(200, json={"results": []})
        return httpx.Response(
            200,
            json={
                "data": [
                    {"project": "kinetiq-khype", "symbol": "KHYPE", "chain": "HyperEVM", "tvlUsd": 500_000_000.0, "apy": 2.15},
                    {"project": "kinetiq-khype", "symbol": "KHYPE", "chain": "HyperEVM", "tvlUsd": 500.0, "apy": 0.01},  # tiny dust sub-pool!
                    {"project": "hyperliquid-hlp", "symbol": "USDC", "chain": "Hyperliquid", "tvlUsd": 150_000_000.0, "apy": 14.5},
                    {"project": "hyperliquid-hlp", "symbol": "USDC", "chain": "Hyperliquid", "tvlUsd": 100.0, "apy": 0.5},
                ]
            },
        )

    with httpx.Client(transport=httpx.MockTransport(llama_handler)) as client:
        y_inp = hys.fetch_hyperevm_yield_inputs(client)
    assert y_inp.khype_apy_pct == pytest.approx(2.15)
    assert y_inp.hlp_apr_pct == pytest.approx(14.5)

    # 5. Cross-DEX paper: H1-combo beyond top-30 by spread is NOT truncated, sub-hour gate, & NAV sanity guard
    cd_dir = tmp_path / "cd_combo"
    hl_q_map = {
        f"CARRY{i}": cds.DexMarketQuote("hyperliquid", f"CARRY{i}", f"CARRY{i}", 100.0, 0.0005, 438.0, 5e6, 2e6, 1.0)
        for i in range(35)  # 35 pure-carry pairs with streak=1 (< min_persistence_ticks=2)
    }
    lt_q_map = {
        f"CARRY{i}": cds.DexMarketQuote("lighter", f"CARRY{i}", f"CARRY{i}", 100.0, 0.0, 0.0, 5e6, 2e6, 1.0)
        for i in range(35)
    }
    # Rank #36 by spread (14% APR < 438%), BUT +15 bps favorable basis -> must be included via H1 combo union!
    hl_q_map["COMBO36"] = cds.DexMarketQuote("hyperliquid", "COMBO36", "COMBO36", 100.15, 0.000016, 14.0, 5e6, 2e6, 1.0)
    lt_q_map["COMBO36"] = cds.DexMarketQuote("lighter", "COMBO36", "COMBO36", 100.00, 0.0, 0.0, 5e6, 2e6, 1.0)

    def cd_book(dex: str, sym: str):
        if sym == "COMBO36":
            return ([(100.145, 500.0)], [(100.155, 500.0)]) if dex == "hyperliquid" else ([(99.995, 500.0)], [(100.005, 500.0)])
        return ([(99.99, 500.0)], [(100.01, 500.0)])

    cfg_cd = cdp.CrossDexPaperConfig(
        min_persistence_ticks=2,
        combo_min_persistence_ticks=1,
        combo_min_apr_pct=12.0,
        combo_min_exec_basis_bps=8.0,
        max_tick_nav_jump_pct=5.0,
    )
    with httpx.Client() as client:
        rc1 = cdp.run_cross_dex_paper_tick(
            client, state_dir=cd_dir, cfg=cfg_cd, now_ms=t0, hl_quotes=hl_q_map, lighter_quotes=lt_q_map, book_fetcher=cd_book
        )
        assert "COMBO36" in rc1["state"]["positions"]
        rc_dup = cdp.run_cross_dex_paper_tick(
            client, state_dir=cd_dir, cfg=cfg_cd, now_ms=t0 + 300_000, hl_quotes=hl_q_map, lighter_quotes=lt_q_map, book_fetcher=cd_book
        )
        assert rc_dup["status"] == "duplicate_hour_skipped"
        # Corrupt single-leg price jump triggers Cross-DEX NAV sanity guard
        hl_bad = dict(hl_q_map, COMBO36=cds.DexMarketQuote("hyperliquid", "COMBO36", "COMBO36", 220.0, 0.000016, 14.0, 5e6, 2e6, 1.0))
        rc_guard = cdp.run_cross_dex_paper_tick(
            client, state_dir=cd_dir, cfg=cfg_cd, now_ms=t0 + 4 * H, hl_quotes=hl_bad, lighter_quotes=lt_q_map, book_fetcher=cd_book
        )
        assert rc_guard["state"]["nav_guard_triggered"] is True
        assert rc_guard["state"]["nav_guard_triggers"] == 1


def test_m13_m14_event_escalation_walkforward_vol_target_funding_ffill_and_ws_replay(tmp_path: Path):
    # 1. M-13: H2 whale wall moving from 8% to 0.5% escalates immediately via weighted_near_usd
    t0 = 1_795_800_000_000
    cfg_ev = oea.OnChainEventConfig(h2_alert_weighted_usd=1_500_000.0)
    far_cluster = [
        liq.CoinLiquidationCluster("BTC", 100_000.0, 2, 0.0, 0.0, 6_000_000.0, 0.0, 0.0, 0.0, 6_000_000.0, 92_000.0, 8.0, None, None, "Down")
    ]
    near_cluster = [
        liq.CoinLiquidationCluster("BTC", 100_000.0, 2, 6_000_000.0, 0.0, 0.0, 0.0, 0.0, 0.0, 6_000_000.0, 99_500.0, 0.5, None, None, "Down")
    ]
    evm_empty = oea.evm.HyperEVMDefiSnapshot(
        t0, oea.evm.HyperEVMChainHealth(999, 1, 0.1, 10.0, 1, 1.0, 0.0, "ok"), [], [], [], [], []
    )
    r_far = oea.run_onchain_event_tick(
        state_dir=tmp_path / "ev",
        cfg=cfg_ev,
        now_ms=t0,
        liq_snap=liq.LiquidationAndFundingAlphaSnapshot(t0, 10, 5, 2, far_cluster, []),
        evm_snap=evm_empty,
        hlp_child_states=[],
        hlp_tvl_usd=150e6,
        mark_prices={"BTC": 100_000.0},
        perps_raw={},
    )
    assert "H2:BTC" in r_far["new_alert_keys"]
    r_near = oea.run_onchain_event_tick(
        state_dir=tmp_path / "ev",
        cfg=cfg_ev,
        now_ms=t0 + 15 * 60_000,
        liq_snap=liq.LiquidationAndFundingAlphaSnapshot(t0 + 15 * 60_000, 10, 5, 2, near_cluster, []),
        evm_snap=evm_empty,
        hlp_child_states=[],
        hlp_tvl_usd=150e6,
        mark_prices={"BTC": 100_000.0},
        perps_raw={},
    )
    assert "H2:BTC" in r_near["new_alert_keys"]  # weighted jumped $1.8M -> $6.0M (3.33x >= 1.5x)!

    # 2. M-14.1: metamodel apply_vol_target deducts turnover cost when cost_bps > 0
    s_ret = pd.Series(np.tile([0.002, -0.001, 0.003, -0.002], 25), name="m")
    vt_free = mm.apply_vol_target(s_ret, cost_bps=0.0)
    vt_cost = mm.apply_vol_target(s_ret, cost_bps=5.0)
    assert vt_cost.sum() < vt_free.sum()

    # 3. M-14.1: funding_arb 4h settlement inference + 24h ffill cap
    bars = pd.DataFrame({"open_time_ms": UTC_MIDNIGHT + np.arange(50) * H, "close": np.full(50, 100.0)})
    hl_f = pd.DataFrame({"time_ms": UTC_MIDNIGHT + (np.arange(50) + 1) * H, "funding_rate": np.full(50, 1e-4)})
    # CEX settles at hour 4 and hour 8 (4h interval!), then goes dead for 40 hours
    cex_f = pd.DataFrame({"time_ms": [UTC_MIDNIGHT + 4 * H, UTC_MIDNIGHT + 8 * H], "funding_rate": [4e-4, 4e-4]})
    panel = fa.build_panel(hl_f, cex_f, bars, bars)
    # Initial settlement at hour 4 inferred 4.0h interval -> 4e-4 / 4 = 1e-4 (not 4e-4 / 8!)
    assert panel.loc[UTC_MIDNIGHT + 4 * H, "cex_rate_h"] == pytest.approx(1e-4)
    # After hour 8 + 24 = hour 32, stale CEX funding stops forward-filling -> NaN at hour 35!
    assert np.isnan(panel.loc[UTC_MIDNIGHT + 35 * H, "cex_rate_h"])

    # 4. M-14.1: walk_forward carries held_slots across contiguous folds without churn
    idx = pd.Index(UTC_MIDNIGHT + np.arange(100) * H, name="hour_ms")
    p_base = {
        c: pd.DataFrame(
            {
                "spread_h": np.full(100, rate),
                "hl_fund_next": np.full(100, rate),
                "cex_fund_next": np.zeros(100),
                "hl_ret": np.zeros(100),
                "cex_ret": np.zeros(100),
            },
            index=idx,
        )
        for c, rate in (("A", 2e-4), ("B", 3e-4))
    }
    # If A was already held in the single slot, carrying prev_held_coins={"A"} keeps A instead of churning to B!
    w_carried, _ = wf.compute_fold_weights(
        p_base,
        ["A", "B"],
        fa.Params(lookback_h=8, entry_apr=0.05, exit_apr=0.025),
        idx[20:40],
        allocation="active_slots",
        max_active_slots=1,
        max_weight_per_coin=1.0,
        prev_signals={"A": 1, "B": 1},
        prev_held_coins={"A"},
    )
    assert (w_carried["A"] == 1.0).all() and (w_carried["B"] == 0.0).all()

    # 5. M-14.1: hl_microstructure drops WS trade replay prints with recv_ms < first_book_ms
    l2_df = pd.DataFrame(
        [
            {
                "recv_ms": 10_000 + i * 1000,
                "book_time_ms": 10_000 + i * 1000,
                "coin": "BTC",
                "best_bid": 100.0,
                "best_ask": 100.02,
                "mid_px": 100.01,
                "spread_bps": 2.0,
                "bid_sz1": 1.0,
                "ask_sz1": 1.0,
                "bid_usd1": 100.0,
                "ask_usd1": 100.02,
                "bid_usd5": 500.0,
                "ask_usd5": 500.0,
                "imbalance1": 0.0,
                "imbalance5": 0.0,
            }
            for i in range(30)
        ]
    )
    tr_df = pd.DataFrame(
        [
            {"recv_ms": 9_500, "trade_time_ms": 9_500, "coin": "BTC", "side": "sell", "px": 99.0, "sz": 50.0, "notional_usd": 4950.0, "tid": 1}
        ]
    )
    rep = hm.simulate_passive_alo_coin(l2_df, tr_df, order_usd=100.0, timeout_sec=5.0)
    assert rep.trades_count == 0 and rep.alo_fill_rate == 0.0
