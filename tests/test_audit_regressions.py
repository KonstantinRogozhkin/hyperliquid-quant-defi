"""Regression tests covering critical and medium audit findings across hl_quant."""

from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import cex_perp_public as cex
from hl_quant import cross_dex_scanner as cds
from hl_quant import cross_usdc_micro_execution as micro
from hl_quant import hl_boros_fixed_funding as boros
from hl_quant import hl_cross_dex_paper as cdp
from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import hl_hyperevm_yield_stack as hys
from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant import hl_onchain_event_alpha as oea
from hl_quant import hl_post_trade_attribution as attr
from hl_quant import hl_testnet_executor as ex
from hl_quant import paper_tracker as pt


def test_cross_usdc_mainnet_safety_guard_and_arrival_shortfall(monkeypatch):
    monkeypatch.delenv(micro.MAINNET_EXECUTION_ACK_ENV, raising=False)

    b_client = micro.BinanceClient("k", "s")
    with pytest.raises(micro.MicroExecutionError, match="mainnet_execution_disabled_by_safety_guard"):
        b_client.create_ioc(symbol="BTCUSDC", side="BUY", quantity=Decimal("0.001"), price=Decimal("60000"), client_id="id1")

    y_client = micro.BybitClient("k", "s")
    with pytest.raises(micro.MicroExecutionError, match="mainnet_execution_disabled_by_safety_guard"):
        y_client.create_ioc(
            symbol="BTCPERP", side="Sell", quantity=Decimal("0.001"), price=Decimal("60000"), client_id="id2", reduce_only=False
        )

    with pytest.raises(micro.MicroExecutionError, match="mainnet_execution_disabled_by_safety_guard"):
        micro.HyperliquidClient("0x0000000000000000000000000000000000000001", "0x" + "11" * 32, use_testnet=False)

    # Shortfall vs arrival price (buy filled at 100.03 vs arrival 100.00 => +3.0 bps adverse)
    rows = [{"symbol": "ETHUSDC", "isBuyer": True, "qty": "1", "price": "100.03", "commission": "0", "commissionAsset": "USDC"}]
    sf = micro._shortfall_bps(rows, venue="binance", symbol="ETHUSDC", side="buy", arrival_price=Decimal("100.00"))
    assert sf == pytest.approx(3.0)


def test_neutralize_target_weights_respects_single_coin_cap_and_passes_kill_switch():
    raw = {"A": 0.2, "B": 0.2, "C": 0.2, "D": -0.2}
    norm = ex.neutralize_target_weights(raw, max_gross=0.90, max_single_coin_weight=0.25)
    assert abs(sum(norm.values())) < 1e-4
    assert max(abs(w) for w in norm.values()) <= 0.25 + 1e-9
    assert sum(abs(w) for w in norm.values()) <= 0.90 + 1e-9

    now = 1_795_800_000_000
    acct = ex.AccountSnapshot(equity_usd=1000.0, free_margin_usd=950.0, positions_sz={}, open_orders=[], observed_at_ms=now)
    bbo = {c: (100.0, 100.1, now) for c in norm}
    assert ex.check_kill_switch(acct, norm, bbo, ex.RiskLimits(), high_water_mark_usd=1000.0, now_ms=now) == []


def test_hl_string_error_responses_never_raise_attribute_error(tmp_path: Path):
    err_resp = {"status": "err", "response": "Too many requests"}
    statuses, err_msg = ex.extract_hl_order_statuses(err_resp)
    assert statuses == [] and err_msg == "Too many requests"

    oid, state = mm._extract_order_oid({"mode": "testnet_live", "response": err_resp})
    assert oid is None and "Too many requests" in state

    class _ErrExchange:
        def bulk_orders(self, _reqs):
            return err_resp

        def bulk_modify_orders_new(self, _reqs):
            return err_resp

    adapter = object.__new__(ex.HyperliquidTestnetSDKAdapter)
    adapter._exchange = _ErrExchange()
    o1 = ex.PlannedOrder(coin="ETH", side="buy", limit_px=3000.0, sz=0.01, notional_usd=30.0, tif="Alo", reduce_only=False)
    o2 = ex.PlannedOrder(coin="BTC", side="sell", limit_px=60000.0, sz=0.001, notional_usd=60.0, tif="Alo", reduce_only=False)
    bulk_out = adapter.place_bulk_alo_orders([o1, o2])
    assert len(bulk_out) == 2 and bulk_out[0]["response"]["status"] == "err"
    mod_out = adapter.modify_bulk_alo_orders([(101, o1)])
    assert len(mod_out) == 1 and mod_out[0]["response"]["status"] == "err"

    acct = ex.AccountSnapshot(equity_usd=1000.0, free_margin_usd=950.0, positions_sz={}, open_orders=[], observed_at_ms=1)
    rich = ex.format_testnet_telegram_rich_html(acct, {"status": "ok", "orders": bulk_out}, {}, hwm_usd=1000.0)
    assert "Too many requests" in rich

    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(json.dumps({"ts_ms": 1000, "orders": bulk_out}) + "\n", encoding="utf-8")
    assert attr.extract_audit_oids(audit_path) == set()


def test_mm_fill_dedup_key_preserves_same_size_partial_fills_via_tid():
    f1 = {"oid": 555, "side": "B", "sz": "0.01", "px": "3000", "time": 1000, "tid": 9001}
    f2 = {"oid": 555, "side": "B", "sz": "0.01", "px": "3000", "time": 1000, "tid": 9002}
    k1 = mm._fill_dedup_key(f1, "ETH", 555, "buy", 0.01, 1000)
    k2 = mm._fill_dedup_key(f2, "ETH", 555, "buy", 0.01, 1000)
    assert k1 != k2
    assert k1 == mm._fill_dedup_key(f1, "ETH", 555, "buy", 0.01, 1000)


def test_rebalance_preserves_unmanaged_mm_orders_unless_kill_switch_fires():
    now = 1_795_800_000_000
    meta = {"SOL": ex.AssetSpec("SOL", sz_decimals=2, max_leverage=20, only_isolated=False)}
    bbo = {"SOL": (100.0, 100.1, now)}
    open_orders = [
        {"coin": "SOL", "oid": 1},  # managed by rebalance
        {"coin": "ETH", "oid": 2},  # unmanaged concurrent MM quote
    ]
    acct = ex.AccountSnapshot(equity_usd=1000.0, free_margin_usd=950.0, positions_sz={}, open_orders=open_orders, observed_at_ms=now)
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(acct, {"SOL": 0.10}, meta, bbo, adapter, high_water_mark_usd=1000.0, now_ms=now)
    assert res["status"] == "ok"
    assert [c["oid"] for c in adapter.cancelled] == [1]

    # Kill-switch emergency (drawdown > limit) MUST cancel ALL orders including unmanaged ETH
    adapter2 = ex.DryRunAdapter()
    res_ks = ex.execute_rebalance_cycle(acct, {"SOL": 0.10}, meta, bbo, adapter2, high_water_mark_usd=2000.0, now_ms=now)
    assert res_ks["status"] == "kill_switch_triggered"
    assert {c["oid"] for c in adapter2.cancelled} == {1, 2}


def test_alpha_families_return_alignment_and_ema_closes_when_flat():
    t0 = 1_790_000_000_000
    bundle = pd.DataFrame(
        [
            {"hour_ms": t0, "coin": "BTC", "hl_close": 100.0, "bybit_close": 100.0, "hl_fund_next": 0.0, "bybit_fund_next": 0.0, "hl_rate_h": 0.0, "bybit_rate_h": 0.0, "hl_notional_usd": 1e7, "bybit_notional_usd": 1e7},
            {"hour_ms": t0 + 3_600_000, "coin": "BTC", "hl_close": 105.0, "bybit_close": 100.0, "hl_fund_next": 0.0, "bybit_fund_next": 0.0, "hl_rate_h": 0.0, "bybit_rate_h": 0.0, "hl_notional_usd": 1e7, "bybit_notional_usd": 1e7},
            {"hour_ms": t0 + 7_200_000, "coin": "BTC", "hl_close": 105.0, "bybit_close": 100.0, "hl_fund_next": 0.0, "bybit_fund_next": 0.0, "hl_rate_h": 0.0, "bybit_rate_h": 0.0, "hl_notional_usd": 1e7, "bybit_notional_usd": 1e7},
        ]
    )
    mkt = af.build_wide_market(bundle)
    assert mkt.hl_ret_next.loc[t0 + 3_600_000, "BTC"] == pytest.approx(0.05)
    assert mkt.hl_ret_next.loc[t0, "BTC"] == pytest.approx(0.0)

    idx = pd.RangeIndex(12)
    scores = pd.DataFrame({"A": [1.0] * 12, "B": [-1.0] * 12}, index=idx)
    mask = pd.DataFrame({"A": [True, True] + [False] * 10, "B": [True, True] + [False] * 10}, index=idx)
    w = af.cross_sectional_weights(scores, mask, top_n=1, rebalance_h=1, ema_alpha=0.5)
    assert float(w.iloc[0].abs().sum()) > 0.0
    # After 10 consecutive flat targets with ema_alpha=0.5, gross must decay to 0 (not stay pinned at 1.0!)
    assert float(w.iloc[-1].abs().sum()) == pytest.approx(0.0)


def test_boros_long_action_uses_ask_not_bid():
    bad_spread = boros.parse_boros_market(
        {
            "marketId": 1,
            "imData": {"name": "HL BTC", "symbol": "HL-BTC", "maturity": 1_800_000_000},
            "metadata": {"underlyingSymbol": "BTC", "isSpreadMarket": False},
            "data": {"timeToMaturity": 20 * 86400, "markApr": -0.05, "midApr": -0.045, "bestBid": -0.11, "bestAsk": 0.02, "floatingApr": -0.05},
        }
    )
    assert bad_spread is not None
    assert "Long Perp + Long Boros" not in bad_spread.recommended_action

    true_neg_ask = boros.parse_boros_market(
        {
            "marketId": 2,
            "imData": {"name": "HL BTC", "symbol": "HL-BTC", "maturity": 1_800_000_000},
            "metadata": {"underlyingSymbol": "BTC", "isSpreadMarket": False},
            "data": {"timeToMaturity": 20 * 86400, "markApr": -0.12, "midApr": -0.12, "bestBid": -0.14, "bestAsk": -0.11, "floatingApr": -0.12},
        }
    )
    assert true_neg_ask is not None
    assert "Lock +11.00%" in true_neg_ask.recommended_action


def test_cross_dex_paper_holds_positions_during_venue_outage(tmp_path: Path):
    def _q(dex: str, coin: str, px: float, apr: float) -> cds.DexMarketQuote:
        return cds.DexMarketQuote(dex, coin, coin, px, apr / (8760.0 * 100.0), apr, 10_000_000.0, 5_000_000.0, 1.0)

    def fake_book(_dex: str, _sym: str):
        return ([(100.0, 500.0)], [(100.01, 500.0)])

    t0 = 1_795_800_000_000
    cfg = cdp.CrossDexPaperConfig(min_persistence_ticks=1, max_l2_slip_bps=5.0, max_missing_ticks=3)
    with httpx.Client() as client:
        r1 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0,
            hl_quotes={"SOL": _q("hyperliquid", "SOL", 100.0, 40.0)},
            lighter_quotes={"SOL": _q("lighter", "SOL", 100.0, 0.0)},
            extended_quotes={},
            book_fetcher=fake_book,
        )
        assert "SOL" in r1["state"]["positions"]

        # Lighter API outage (empty dict) MUST NOT force-close SOL!
        r2 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0 + 4 * 3_600_000,
            hl_quotes={"SOL": _q("hyperliquid", "SOL", 100.0, 40.0)},
            lighter_quotes={},
            extended_quotes={},
            book_fetcher=fake_book,
        )
        assert "SOL" in r2["state"]["positions"]
        assert r2["state"]["closed_pairs_count"] == 0


def test_hip3_payback_uses_deployer_fee_scale_and_single_exit_slippage():
    m1 = hip3.Hip3BuilderMarket(
        dex="xyz", dex_full_name="XYZ", full_symbol="xyz:FOO", base_symbol="FOO",
        mark_px=100.0, oracle_px=100.0, basis_vs_oracle_bps=0.0,
        funding_rate_h=40.0 / (8760.0 * 100.0), funding_apr_pct=40.0,
        day_vol_usd=10_000_000.0, oi_usd=5_000_000.0, only_isolated=False, max_leverage=10,
        growth_mode=False, deployer_fee_scale=2.0, est_taker_fee_bps=9.0, est_maker_fee_bps=3.0,
    )
    m2 = hip3.Hip3BuilderMarket(
        dex="io", dex_full_name="IO", full_symbol="io:FOO", base_symbol="FOO",
        mark_px=100.0, oracle_px=100.0, basis_vs_oracle_bps=0.0,
        funding_rate_h=0.0, funding_apr_pct=0.0,
        day_vol_usd=10_000_000.0, oi_usd=5_000_000.0, only_isolated=False, max_leverage=10,
        growth_mode=False, deployer_fee_scale=1.0, est_taker_fee_bps=4.5, est_maker_fee_bps=1.5,
    )
    # 2 bps spread around 100 -> sl5 = 2.0 bps, eb5 = -2.0 bps; rt_maker = 2*(3.0 + 1.5) = 9.0 bps
    # Exact RT cost = rt_maker (9) + sl5 (2) - eb5 (-2) = 13.0 bps (vs old 6 + 4 - 0 = 10 bps ignoring fee scale!)
    opps = hip3.scan_hip3_cross_builder_arb(
        [m1, m2],
        book_fetcher=lambda _s: ([(99.99, 500.0)], [(100.01, 500.0)]),
    )
    assert len(opps) == 1
    hourly_bps = 40.0 / (8760.0 * 100.0) * 1e4
    expected_be = round((9.0 + 2.0 - (-2.0)) / hourly_bps, 1)
    assert opps[0].payback_hours_5k == pytest.approx(expected_be, abs=0.2)


def test_attribution_window_aligns_audit_oids_when_fills_hit_2000_cap(tmp_path: Path):
    audit_path = tmp_path / "audit.jsonl"
    # Old order at ts=1000 (evicted from 2000-fill window) + recent order at ts=5000 (filled)
    old_rec = {"ts_ms": 1000, "orders": [{"response": {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 1}}]}}}}]}
    new_rec = {"ts_ms": 5000, "orders": [{"response": {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 2}}]}}}}]}
    audit_path.write_text(json.dumps(old_rec) + "\n" + json.dumps(new_rec) + "\n", encoding="utf-8")

    # Simulate 2000 fills starting at ts=4000 (so oid=1 is evicted, oid=2 is present)
    fills = [{"coin": "ETH", "px": "3000", "sz": "0.01", "fee": "0.01", "closedPnl": "0", "crossed": False, "oid": 2, "time": 4000 + i} for i in range(2000)]
    snap = {"account_address": "0x1", "observed_at_ms": 9999, "fills": fills, "funding": [], "clearinghouse": {"marginSummary": {"accountValue": "1000"}}, "spot": {}, "open_orders": [], "all_mids": {"ETH": 3000.0}}
    res = attr.compute_post_trade_attribution(snap, audit_log_path=audit_path)
    assert res.audit_orders_placed == 1
    assert res.real_alo_fill_rate_pct == pytest.approx(100.0)


def test_paper_tracker_append_parquet_deduplicates_on_retry(tmp_path: Path):
    p = tmp_path / "ticks.parquet"
    pt._append_parquet(p, pd.DataFrame([{"tick_hour_ms": 100, "equity_usd": 1000.0}]), dedup_subset=["tick_hour_ms"])
    pt._append_parquet(p, pd.DataFrame([{"tick_hour_ms": 100, "equity_usd": 1005.0}]), dedup_subset=["tick_hour_ms"])
    df = pd.read_parquet(p)
    assert len(df) == 1
    assert float(df.iloc[0]["equity_usd"]) == pytest.approx(1005.0)


def test_hyperevm_yield_inputs_raises_on_defillama_error():
    def handler(req: httpx.Request) -> httpx.Response:
        if "llama.fi" in str(req.url):
            return httpx.Response(502, text="Bad Gateway")
        if "boros" in str(req.url):
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(200, json=[{"universe": [{"name": "HYPE"}]}, [{"markPx": "35.0", "funding": "0.0001"}]])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(RuntimeError, match="DeFiLlama pools fetch failed"):
            hys.fetch_hyperevm_yield_inputs(client)


def test_cex_funding_marks_truncated_history_as_partial():
    t0 = 1_700_000_000_000
    t1 = t0 + 90 * 24 * cex.HOUR_MS

    class _FakeOkxClient:
        def get(self, _path, _params):
            # Returns only recent 5 days, missing the first 85 days after start_ms
            return {
                "code": "0",
                "data": [{"instId": "BTC-USDT-SWAP", "fundingTime": str(t1 - i * 8 * cex.HOUR_MS), "fundingRate": "0.0001"} for i in range(15)],
            }

    collected = cex.collect_funding(_FakeOkxClient(), "okx", "BTC-USDT-SWAP", t0, t1, now_ms=t1 + 60_000)
    assert collected.status == cex.STATUS_PARTIAL_HISTORY
    assert collected.notes["truncated_start"] is True


def test_onchain_grade_forward_outcomes_enforces_tight_horizon_windows(tmp_path: Path):
    p = tmp_path / "h2.parquet"
    t0 = 1_795_800_000_000
    pd.DataFrame(
        [{"ts_ms": t0, "coin": "BTC", "mark_px": 100_000.0, "ret_15m_bps": np.nan, "ret_1h_bps": np.nan, "ret_4h_bps": np.nan}]
    ).to_parquet(p, index=False)

    # At +100 min (outside 55..75m 1h window), ret_1h_bps must NOT be stamped with a 100m return!
    assert oea.grade_forward_outcomes(p, {"BTC": 101_000.0}, now_ms=t0 + 100 * 60_000) == 0
    # At +62 min (inside 55..75m 1h window), ret_1h_bps IS stamped cleanly!
    assert oea.grade_forward_outcomes(p, {"BTC": 101_000.0}, now_ms=t0 + 62 * 60_000) == 1
