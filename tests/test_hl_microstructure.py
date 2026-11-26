from __future__ import annotations

import pandas as pd
import pytest

from hl_quant import hl_microstructure as hm


def test_parse_l2_and_trades_messages():
    l2 = hm.parse_l2_message(
        {
            "coin": "BTC",
            "time": 1_700_000_000_000,
            "levels": [
                [{"px": "100000.0", "sz": "2.0"}, {"px": "99990.0", "sz": "3.0"}],
                [{"px": "100010.0", "sz": "1.0"}, {"px": "100020.0", "sz": "4.0"}],
            ],
        },
        recv_ms=1_700_000_000_050,
    )
    assert l2 is not None
    assert l2["coin"] == "BTC"
    assert l2["spread_bps"] == pytest.approx(10.0 / 100005.0 * 1e4)
    assert l2["imbalance1"] == pytest.approx((2.0 - 1.0) / 3.0)

    tr = hm.parse_trades_message(
        [{"coin": "BTC", "side": "A", "px": "100000.0", "sz": "0.5", "time": 1_700_000_000_100, "tid": 99}],
        recv_ms=1_700_000_000_120,
    )
    assert len(tr) == 1
    assert tr[0]["side"] == "sell"
    assert tr[0]["notional_usd"] == pytest.approx(50_000.0)


def test_simulate_passive_alo_queue_fill_and_adverse_markout():
    # L2 book sits at bid=100.00 (queue $10k) / ask=100.02 (queue $10k) for 0..90s,
    # then at t=10s a $20k sell hits 100.00 (fills our $5k buy at 100.00!)
    # and mid drops to 99.90 at t=15s..90s -> buy markout at +5s/+30s is -10 bps (adverse selection!).
    t0 = 1_700_000_000_000
    l2_rows = []
    for sec in range(0, 95):
        ms = t0 + sec * 1000
        if sec < 12:
            bid, ask = 100.00, 100.02
        else:
            bid, ask = 99.89, 99.91
        l2_rows.append(
            {
                "recv_ms": ms,
                "book_time_ms": ms,
                "coin": "TEST",
                "best_bid": bid,
                "best_ask": ask,
                "mid_px": 0.5 * (bid + ask),
                "spread_bps": (ask - bid) / (0.5 * (bid + ask)) * 1e4,
                "bid_sz1": 100.0,
                "ask_sz1": 100.0,
                "bid_usd1": 10_000.0,
                "ask_usd1": 10_000.0,
                "bid_usd5": 50_000.0,
                "ask_usd5": 50_000.0,
                "imbalance1": 0.0,
                "imbalance5": 0.0,
            }
        )
    tr_rows = [
        {"recv_ms": t0 + 10_000, "trade_time_ms": t0 + 10_000, "coin": "TEST", "side": "sell", "px": 100.00, "sz": 200.0, "notional_usd": 20_000.0, "tid": 1}
    ]
    rep = hm.simulate_passive_alo_coin(
        pd.DataFrame(l2_rows),
        pd.DataFrame(tr_rows),
        order_usd=5_000.0,
        sample_interval_sec=10.0,
        timeout_sec=30.0,
    )
    assert rep.alo_trials >= 2
    assert rep.alo_fill_rate > 0.0
    # Buy filled at 100.00, mid 5s later is 99.90 -> -10 bps adverse selection!
    assert rep.markout_5s_bps == pytest.approx(-10.0, abs=0.2)
    assert rep.markout_30s_bps == pytest.approx(-10.0, abs=0.2)


def test_bbo_ticks_carry_depth_and_drive_the_analyzer(tmp_path):
    import pandas as pd

    depth = hm.parse_l2_message(
        {"coin": "HYPE", "time": 1, "levels": [[{"px": "10.0", "sz": "100", "n": 1}] * 5, [{"px": "10.1", "sz": "50", "n": 1}] * 5]},
        recv_ms=1,
    )
    row = hm.parse_bbo_message(
        {"coin": "HYPE", "time": 2, "bbo": [{"px": "10.0", "sz": "3", "n": 1}, {"px": "10.01", "sz": "4", "n": 1}]}, recv_ms=2, depth=depth
    )
    assert row["source"] == "bbo"
    assert row["bid_usd1"] == 30.0 and row["ask_usd1"] == 40.04
    assert row["bid_usd5"] == depth["bid_usd5"] and row["imbalance5"] == depth["imbalance5"]
    assert hm.parse_bbo_message({"coin": "HYPE", "bbo": [None, {"px": "1", "sz": "1"}]}, recv_ms=3) is None

    # Stale l2Book row says bid 9.0; bbo ticks say 10.0. The analyzer must use bbo for the limit price.
    t0 = 1_000_000
    stale = {**depth, "recv_ms": t0, "best_bid": 9.0, "best_ask": 9.1, "mid_px": 9.05, "source": "l2Book"}
    ticks = [stale] + [
        {**hm.parse_bbo_message({"coin": "HYPE", "bbo": [{"px": "10.0", "sz": "1", "n": 1}, {"px": "10.01", "sz": "1", "n": 1}]}, recv_ms=t0 + i * 1000, depth=depth)}
        for i in range(120)
    ]
    pd.DataFrame(ticks).to_parquet(tmp_path / "l2_ticks.parquet", index=False)
    trades = [
        {"recv_ms": t0 + i * 1000 + 500, "trade_time_ms": t0 + i * 1000 + 500, "coin": "HYPE", "side": "sell", "px": 10.0, "sz": 1000.0, "notional_usd": 10_000.0, "tid": i}
        for i in range(120)
    ]
    pd.DataFrame(trades).to_parquet(tmp_path / "trades.parquet", index=False)
    rep = hm.analyze_recording(tmp_path, order_usd=1_000.0, timeout_sec=10.0)[0]
    assert rep.l2_updates == 120  # only bbo ticks drive top of book
    assert rep.alo_fill_rate == 0.5  # passive buys at 10.0 fill on sells at 10.0; passive sells at 10.01 never do


def test_analyzer_drops_trades_replayed_from_before_the_recording(tmp_path):
    import pandas as pd

    t0 = 5_000_000
    tick = {"coin": "X", "best_bid": 10.0, "best_ask": 10.01, "mid_px": 10.005, "spread_bps": 10.0, "bid_sz1": 1.0, "ask_sz1": 1.0,
            "bid_usd1": 10.0, "ask_usd1": 10.01, "bid_usd5": 50.0, "ask_usd5": 50.0, "imbalance1": 0.0, "imbalance5": 0.0, "source": "bbo"}
    pd.DataFrame([{**tick, "recv_ms": t0 + i * 1000, "book_time_ms": t0 + i * 1000} for i in range(120)]).to_parquet(tmp_path / "l2_ticks.parquet", index=False)
    # Historical prints delivered at subscribe time: recv_ms inside the recording, trade_time_ms an hour earlier.
    replay = [{"recv_ms": t0 + 10, "trade_time_ms": t0 - 3_600_000, "coin": "X", "side": "sell", "px": 9.0, "sz": 1.0, "notional_usd": 9.0, "tid": i} for i in range(30)]
    pd.DataFrame(replay).to_parquet(tmp_path / "trades.parquet", index=False)
    rep = hm.analyze_recording(tmp_path, order_usd=100.0, timeout_sec=10.0)[0]
    assert rep.trades_count == 0
    assert rep.alo_fill_rate == 0.0
