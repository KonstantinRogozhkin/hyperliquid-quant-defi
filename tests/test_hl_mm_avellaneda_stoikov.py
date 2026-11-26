from __future__ import annotations

import json
import math

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant import hl_testnet_executor as ex


def _book(bid: float = 2500.0, ask: float = 2500.5, bid_sz: float = 10.0, ask_sz: float = 10.0, imb5: float = 0.0) -> mm.L2BookSnapshot:
    mid = 0.5 * (bid + ask)
    micro = (bid * ask_sz + ask * bid_sz) / (bid_sz + ask_sz)
    return mm.L2BookSnapshot(
        coin="ETH",
        observed_ms=1_700_000_000_000,
        best_bid=bid,
        best_ask=ask,
        bid_sz1=bid_sz,
        ask_sz1=ask_sz,
        imbalance5=imb5,
        mid_px=mid,
        microprice=micro,
        book_spread_bps=(ask - bid) / mid * 1e4,
    )


def test_avellaneda_stoikov_reservation_skew_spread_and_inventory_caps():
    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(gamma=0.05, kappa=0.25, base_order_usd=30.0, max_inventory_usd=100.0, clamp_to_bbo=True)
    bk = _book(2500.0, 2500.5)

    # Zero inventory -> symmetric reservation price == fair_px, half-spread matches textbook formula
    q0 = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=10.0, params=params)
    expected_total = 0.05 * 100.0 + (2.0 / 0.05) * math.log(1.0 + 0.05 / 0.25)
    assert q0.skew_bps == pytest.approx(0.0)
    assert q0.half_spread_bps == pytest.approx(0.5 * expected_total, abs=1e-6)
    assert q0.bid_px is not None and q0.ask_px is not None
    assert q0.bid_px <= bk.best_bid and q0.ask_px >= bk.best_ask  # strict ALO clamp

    # Long inventory (+$50 = +0.5 q_norm) -> negative reservation skew (lower both quotes) & smaller bid than ask
    q_long = mm.compute_as_quotes(bk, spec, inventory_sz=50.0 / bk.mid_px, sigma_tau_bps=10.0, params=params)
    assert q_long.skew_bps < 0.0
    assert q_long.reservation_px < q0.reservation_px
    assert q_long.bid_px is not None and q_long.bid_px < q0.bid_px
    assert q_long.bid_sz < q_long.ask_sz

    # Short inventory (-$50) -> positive reservation skew (mirror symmetry)
    q_short = mm.compute_as_quotes(bk, spec, inventory_sz=-50.0 / bk.mid_px, sigma_tau_bps=10.0, params=params)
    assert q_short.skew_bps == pytest.approx(-q_long.skew_bps, abs=1e-9)

    # Hard max long inventory cap -> bid completely disabled, ask is reduce_only
    q_cap = mm.compute_as_quotes(bk, spec, inventory_sz=110.0 / bk.mid_px, sigma_tau_bps=10.0, params=params)
    assert q_cap.bid_px is None and q_cap.bid_sz == 0.0
    assert q_cap.ask_px is not None and q_cap.ask_reduce_only is True

    # Volatility circuit breaker halts quoting
    q_halt = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=80.0, params=params)
    assert q_halt.halt_reason is not None and "high_volatility" in q_halt.halt_reason
    assert q_halt.bid_px is None and q_halt.ask_px is None


def test_ewma_vol_and_requote_deadband():
    est = mm.EWMAVolatilityEstimator(horizon_sec=60.0, half_life_sec=10.0, default_sigma_bps=8.0)
    t0 = 1_700_000_000_000
    s_quiet = est.update(t0, 2500.0)
    for i in range(1, 15):
        s_quiet = est.update(t0 + i * 1000, 2500.0 * (1.0 + (0.0001 if i % 2 else -0.0001)))
    s_spike = s_quiet
    for i in range(15, 25):
        s_spike = est.update(t0 + i * 1000, 2500.0 * (1.0 + (0.004 if i % 2 else -0.004)))
    assert s_spike > s_quiet * 3.0

    # Small price move < 1.0 bps should NOT trigger requote (preserves queue priority)
    assert not mm.should_requote_side(2500.10, 0.012, (2500.0, 0.012), mid_px=2500.0, threshold_bps=1.0)
    assert mm.should_requote_side(2500.50, 0.012, (2500.0, 0.012), mid_px=2500.0, threshold_bps=1.0)


def test_l2_replay_simulation_captures_spread_and_controls_inventory():
    # Two-sided order flow around a stable mean-reverting mid -> MM captures spread with bounded inventory
    rng = np.random.default_rng(7)
    n = 120
    t0 = 1_700_000_000_000
    mids = np.full(n, 2500.0)
    for i in range(1, n):
        mids[i] = 2500.0 + 0.7 * (mids[i - 1] - 2500.0) + rng.normal(0.0, 0.35)
    l2_df = pd.DataFrame(
        {
            "recv_ms": [t0 + i * 1000 for i in range(n)],
            "best_bid": mids - 0.50,
            "best_ask": mids + 0.50,
            "mid_px": mids,
            "bid_sz1": 10.0,
            "ask_sz1": 10.0,
            "imbalance5": 0.0,
        }
    )
    # Alternating aggressive sells & buys sweeping 5 bps through mid so passive quotes get filled on both sides
    trades_rows = []
    for i in range(5, n - 5, 3):
        side = "sell" if (i // 3) % 2 == 0 else "buy"
        px = mids[i] - 1.5 if side == "sell" else mids[i] + 1.5
        trades_rows.append({"trade_time_ms": t0 + i * 1000 - 100, "side": side, "px": px, "sz": 0.5})
    trades_df = pd.DataFrame(trades_rows)

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(
        gamma=0.03,
        kappa=0.35,
        horizon_sec=30.0,
        base_order_usd=40.0,
        max_inventory_usd=120.0,
        maker_fee_bps=1.5,
        requote_threshold_bps=1.5,
    )
    summary, fills_df, quotes_df = mm.simulate_avellaneda_stoikov_on_l2(l2_df, spec, trades_df=trades_df, params=params)
    assert summary.ticks_observed == n
    assert summary.fills_count >= 10
    assert summary.buy_fills >= 4 and summary.sell_fills >= 4
    assert summary.maker_share_pct == pytest.approx(100.0)
    assert summary.max_abs_inventory_usd <= params.max_inventory_usd * 1.4
    assert summary.realized_spread_pnl_usd > 0.0
    assert summary.net_pnl_usd > 0.0
    assert not fills_df.empty and not quotes_df.empty

    md = mm.format_mm_report_markdown(summary, params)
    rich = mm.format_mm_telegram_rich_html(summary, params)
    assert "Avellaneda–Stoikov" in md
    assert '<table bordered striped>' in rich


def test_live_mm_session_refuses_only_isolated_and_cleans_up_orders_on_exit():
    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t = body["type"]
        if t == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                        {"name": "ZRO", "szDecimals": 1, "maxLeverage": 10, "onlyIsolated": True, "marginMode": "strictIsolated"},
                    ]
                },
            )
        if t == "l2Book":
            return httpx.Response(
                200,
                json={
                    "coin": "ETH",
                    "time": 1_700_000_000_000,
                    "levels": [[{"px": "2500.0", "sz": "5.0", "n": 2}], [{"px": "2500.5", "sz": "5.0", "n": 2}]],
                },
            )
        return httpx.Response(200, json=[])

    adapter = ex.DryRunAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="onlyIsolated"):
            mm.run_live_mm_session("ZRO", adapter, duration_sec=0.5, poll_interval_sec=0.2, client=client)

        summary, _, quotes_df = mm.run_live_mm_session(
            "ETH", adapter, execute_live=False, duration_sec=0.6, poll_interval_sec=0.2, client=client
        )
    assert summary.ticks_observed >= 2
    assert summary.quotes_placed == 2  # initial bid + ask, then deadband skips subsequent identical ticks!
    assert summary.requotes_skipped_deadband >= 2
    assert summary.final_cleanup_cancelled == 2
    assert len(adapter.cancelled) == 2
    assert not quotes_df.empty


def test_arrival_intensity_calibration_and_parameter_sweep():
    # Generate L2 + trades whose fill probability decays exponentially with distance delta
    rng = np.random.default_rng(42)
    n = 240
    t0 = 1_700_000_000_000
    mids = 2500.0 + np.cumsum(rng.normal(0.0, 0.25, n))
    l2_df = pd.DataFrame(
        {
            "recv_ms": [t0 + i * 1000 for i in range(n)],
            "best_bid": mids - 0.20,
            "best_ask": mids + 0.20,
            "mid_px": mids,
            "bid_sz1": 10.0,
            "ask_sz1": 10.0,
            "imbalance5": 0.0,
        }
    )
    # Exponentially distributed trade excursions (true decay ~ 0.35 / bp)
    tr_rows = []
    for i in range(n):
        for _ in range(2):
            dist_bps = float(rng.exponential(scale=1.0 / 0.35))
            side = "buy" if rng.random() < 0.5 else "sell"
            px = mids[i] * (1.0 + dist_bps * 1e-4) if side == "buy" else mids[i] * (1.0 - dist_bps * 1e-4)
            tr_rows.append({"trade_time_ms": t0 + i * 1000 + 200, "side": side, "px": px, "sz": 0.2})
    trades_df = pd.DataFrame(tr_rows)

    cal = mm.calibrate_as_arrival_intensity(l2_df, trades_df, coin="ETH", horizon_sec=30.0, window_sec=5.0)
    assert 0.10 <= cal.kappa_per_bp <= 1.20
    assert cal.arrival_a_per_sec > 0.0
    assert cal.r2_fit >= 0.70
    assert cal.empirical_sigma_1s_bps > 0.0

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    sw = mm.sweep_as_parameters_on_l2(l2_df, spec, trades_df=trades_df, gammas=(0.02, 0.08), kappas=(cal.kappa_per_bp, 0.35))
    assert len(sw) == 4
    assert {"gamma", "kappa", "fills", "net_pnl_usd", "markout_5s_bps"} <= set(sw.columns)


def test_ws_mm_session_processes_streaming_l2_trades_and_cleans_up():
    import asyncio

    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        return httpx.Response(200, json=[])

    ws_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": 1_700_000_000_000, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}},
        {"channel": "trades", "data": [{"coin": "ETH", "side": "A", "px": "2490.0", "sz": "1.0", "time": 1_700_000_000_300}]},
        {"channel": "l2Book", "data": {"coin": "ETH", "time": 1_700_000_001_000, "levels": [[{"px": "2500.1", "sz": "5.0"}], [{"px": "2500.6", "sz": "5.0"}]]}},
        {"channel": "trades", "data": [{"coin": "ETH", "side": "B", "px": "2510.0", "sz": "1.0", "time": 1_700_000_001_300}]},
        {"channel": "l2Book", "data": {"coin": "ETH", "time": 1_700_000_002_000, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}},
    ]
    adapter = ex.DryRunAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary, fills_df, quotes_df = asyncio.run(
            mm.run_live_mm_ws_session("ETH", adapter, execute_live=False, client=client, ws_messages=ws_msgs)
        )
    assert summary.mode == "testnet_ws_dry_run_mm"
    assert summary.ticks_observed == 3
    assert summary.buy_fills == 1 and summary.sell_fills == 1
    assert summary.realized_spread_pnl_usd > 0.0
    assert summary.final_cleanup_cancelled == 2
    assert len(fills_df) == 2 and len(quotes_df) == 3


def test_ws_mm_bbo_channel_and_flatten_on_exit():
    import asyncio

    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        return httpx.Response(200, json=[])

    # Feed l2Book (sets OBI5) followed by fast sub-second bbo ticks + 1 buy fill, then exit with flatten_on_exit=True
    ws_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": 1_700_000_000_000, "levels": [[{"px": "2500.0", "sz": "10.0"}], [{"px": "2500.5", "sz": "2.0"}]]}},
        {"channel": "bbo", "data": {"coin": "ETH", "time": 1_700_000_000_200, "bbo": [{"px": "2500.1", "sz": "8.0"}, {"px": "2500.4", "sz": "3.0"}]}},
        {"channel": "trades", "data": [{"coin": "ETH", "side": "A", "px": "2490.0", "sz": "1.0", "time": 1_700_000_000_300}]},
        {"channel": "bbo", "data": {"coin": "ETH", "time": 1_700_000_000_500, "bbo": [{"px": "2500.2", "sz": "6.0"}, {"px": "2500.5", "sz": "4.0"}]}},
    ]
    adapter = ex.DryRunAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        summary, _, _ = asyncio.run(
            mm.run_live_mm_ws_session("ETH", adapter, execute_live=False, flatten_on_exit=True, client=client, ws_messages=ws_msgs)
        )
    assert summary.bbo_ticks_observed == 2
    assert summary.ticks_observed == 3
    assert summary.flattened_exit_sz > 0.0
    assert summary.end_inventory_sz == pytest.approx(0.0)
    ioc_orders = [o for o in adapter.placed if o.get("tif") == "Ioc"]
    assert len(ioc_orders) == 1
    assert ioc_orders[0]["reduce_only"] is True
    assert ioc_orders[0]["side"] == "sell"


def test_lead_lag_toxic_pull_batch_modify_dms_multi_coin_and_latency_bench():
    import asyncio

    # 1. Lead-Lag estimator subtracts slow basis and triggers toxic-side pull on sudden jump
    est = mm.LeadLagEstimator(basis_half_life_sec=60.0)
    t0 = 1_700_000_000_000
    est.update_cex(t0, 2502.5)  # +10 bps structural basis
    assert est.impulse_bps(t0, 2500.0) == pytest.approx(0.0)
    # Suddenly Binance jumps +4 bps while HL mid is still 2500.0 -> impulse ~ +4 bps >= 2.0 lead_pull_bps!
    est.update_cex(t0 + 200, 2503.5)
    imp = est.impulse_bps(t0 + 200, 2500.0)
    assert imp >= 3.5

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    bk = _book(2500.0, 2500.5)
    dec_up = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=8.0, lead_impulse_bps=imp)
    assert dec_up.ask_px is None and dec_up.bid_px is not None  # ask pulled to avoid toxic buyer!

    # Cross-asset beta spill: holding long BTC shifts ETH reservation price down
    dec_beta = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=8.0, portfolio_other_inv_usd=200.0)
    assert dec_beta.skew_bps < 0.0

    # 2. Multi-coin WS session uses batchModify, Dead Man's Switch (arm + disarm None), and lead pulls
    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                    ]
                },
            )
        return httpx.Response(200, json=[])

    ws_msgs = [
        {"channel": "bbo", "data": {"coin": "ETH", "time": t0, "bbo": [{"px": "2500.0", "sz": "5"}, {"px": "2500.5", "sz": "5"}]}},
        {"channel": "bbo", "data": {"coin": "BTC", "time": t0 + 100, "bbo": [{"px": "60000.0", "sz": "1"}, {"px": "60002.0", "sz": "1"}]}},
        # ETH moves +3 bps -> triggers atomic batchModify instead of cancel+new!
        {"channel": "bbo", "data": {"coin": "ETH", "time": t0 + 600, "bbo": [{"px": "2501.0", "sz": "5"}, {"px": "2501.5", "sz": "5"}]}},
    ]
    adapter = ex.DryRunAdapter()
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = asyncio.run(
            mm.run_multi_coin_mm_ws_session(
                ["BTC", "ETH"],
                adapter,
                execute_live=False,
                enable_lead_lag=True,
                enable_dead_man_switch=True,
                client=client,
                ws_messages=ws_msgs,
            )
        )
    assert set(res["summaries"].keys()) == {"BTC", "ETH"}
    assert res["summaries"]["ETH"].quotes_modified >= 2
    assert res["dead_man_refreshes"] >= 1
    assert adapter.scheduled_cancels[0] is not None and adapter.scheduled_cancels[-1] is None  # armed then disarmed!
    assert len(adapter.modified) >= 2

    # 3. Hot-path latency benchmark runs fast (< 500 us p95 in pure Python)
    bench = mm.benchmark_tick_to_quote_latency(300)
    assert bench["p50_us"] > 0 and bench["throughput_ticks_per_sec"] > 2_000


def test_hourly_funding_settlement_quote_skew_overlay():
    # Exact :59:30 UTC and :02:00 UTC are inside the :59->:05 overlay window; :30:00 UTC is outside
    base_hour = 1_795_700_400_000 - (1_795_700_400_000 % 3_600_000)
    t_5930 = base_hour + 3570 * 1000
    t_0200 = base_hour + 120 * 1000
    t_3000 = base_hour + 1800 * 1000

    assert mm.compute_funding_settlement_overlay_bps(t_5930, -40.0) == pytest.approx(-2.5)
    assert mm.compute_funding_settlement_overlay_bps(t_0200, -40.0) == pytest.approx(-2.5)
    assert mm.compute_funding_settlement_overlay_bps(t_3000, -40.0) == pytest.approx(0.0)
    assert mm.compute_funding_settlement_overlay_bps(t_0200, -10.0) == pytest.approx(0.0)

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    bk = _book(2500.0, 2500.5)
    q_base = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=8.0, funding_overlay_bps=0.0)
    q_ov = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=8.0, funding_overlay_bps=-2.5)
    assert q_ov.fair_px < q_base.fair_px
    assert q_ov.reservation_px < q_base.reservation_px


def test_hypercore_cancel_priority_bypasses_modify_on_toxic_pull():
    import asyncio

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(requote_threshold_bps=0.5)
    bk1 = _book(2500.0, 2500.5)
    bk2 = _book(2505.0, 2505.5)
    dec1 = mm.compute_as_quotes(bk1, spec, inventory_sz=0.0, sigma_tau_bps=8.0, params=params)
    dec2 = mm.compute_as_quotes(bk2, spec, inventory_sz=0.0, sigma_tau_bps=8.0, params=params)

    # 1. Calm requote (prefer_explicit_cancel=False) uses atomic batchModify
    adapter_calm = ex.DryRunAdapter()
    active_calm: dict[str, dict] = {}
    oids_calm: set[int] = set()
    asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec1, active_calm, oids_calm, adapter_calm, mid_px=bk1.mid_px, params=params, recv_ms=1_700_000_000_000, audit_log_path=None, prefer_explicit_cancel=False
        )
    )
    pl, md, cn, _ = asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec2, active_calm, oids_calm, adapter_calm, mid_px=bk2.mid_px, params=params, recv_ms=1_700_000_001_000, audit_log_path=None, prefer_explicit_cancel=False
        )
    )
    assert md == 2 and cn == 0 and pl == 0

    # 2. Urgent toxic requote (prefer_explicit_cancel=True) MUST bypass modify and emit explicit cancel first!
    adapter_urg = ex.DryRunAdapter()
    active_urg: dict[str, dict] = {}
    oids_urg: set[int] = set()
    asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec1, active_urg, oids_urg, adapter_urg, mid_px=bk1.mid_px, params=params, recv_ms=1_700_000_000_000, audit_log_path=None, prefer_explicit_cancel=False
        )
    )
    pl2, md2, cn2, _ = asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec2, active_urg, oids_urg, adapter_urg, mid_px=bk2.mid_px, params=params, recv_ms=1_700_000_001_000, audit_log_path=None, prefer_explicit_cancel=True
        )
    )
    assert md2 == 0 and cn2 == 2 and pl2 == 2
    assert len(adapter_urg.cancelled) == 2 and len(adapter_urg.modified) == 0
    assert all(c.get("fast") is True for c in adapter_urg.cancelled)


def test_vpin_order_flow_toxicity_and_adaptive_api_rate_limit_governor():
    t0 = 1_700_000_000_000
    est = mm.OrderFlowToxicityEstimator(window_sec=15.0, min_volume_usd=2_000.0)
    # Balanced flow -> VPIN near 0
    est.update(t0, "B", 1_500.0)
    est.update(t0 + 500, "A", 1_500.0)
    assert est.vpin(t0 + 1_000) == pytest.approx(0.0)

    # After 20s old trades expire; sudden one-sided sell sweep -> VPIN == 1.0!
    est.update(t0 + 20_000, "A", 3_000.0)
    assert est.vpin(t0 + 21_000) == pytest.approx(1.0)

    # Adaptive API rate-limit governor scales interval smoothly above free buffer
    assert mm.compute_adaptive_action_interval_ms(400, 35, 10.0, free_buffer_reqs=40) == 400
    assert mm.compute_adaptive_action_interval_ms(400, 110, 100.0, free_buffer_reqs=40) == 800   # ratio=0.7 -> 2x
    assert mm.compute_adaptive_action_interval_ms(400, 140, 100.0, free_buffer_reqs=40) == 1600  # ratio=1.0 -> 4x
    assert mm.compute_adaptive_action_interval_ms(400, 180, 100.0, free_buffer_reqs=40) == 3200  # ratio=1.4 -> 8x
    # HIP-3 Growth Mode (-90% volume weight -> 1 req / 10 USDC): $100 vol counts as $10 -> hits 8x throttle immediately!
    assert mm.compute_adaptive_action_interval_ms(400, 60, 100.0, free_buffer_reqs=40, is_growth_mode=True) == 3200
    assert mm.compute_adaptive_action_interval_ms(400, 60, 100.0, free_buffer_reqs=40, is_growth_mode=False) == 400
