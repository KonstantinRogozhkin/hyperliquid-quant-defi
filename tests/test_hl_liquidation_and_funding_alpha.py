from __future__ import annotations

import pytest

from hl_quant import hl_liquidation_and_funding_alpha as lfa


def test_liquidation_heatmap_extraction_bucketing_and_bias():
    marks = {"BTC": 60_000.0, "ETH": 2_000.0}
    ch = {
        "assetPositions": [
            {
                "position": {
                    "coin": "BTC",
                    "szi": "50.0",
                    "positionValue": "3000000.0",
                    "entryPx": "60200.0",
                    "liquidationPx": "59100.0",  # -1.5% below mark -> long 0-2% bucket!
                    "leverage": {"type": "cross", "value": 20},
                }
            },
            {
                "position": {
                    "coin": "BTC",
                    "szi": "-10.0",
                    "positionValue": "600000.0",
                    "entryPx": "59800.0",
                    "liquidationPx": "62400.0",  # +4.0% above mark -> short 2-5% bucket
                    "leverage": {"type": "isolated", "value": 10},
                }
            },
            {
                "position": {
                    "coin": "ETH",
                    "szi": "100.0",
                    "positionValue": "200000.0",
                    "entryPx": "2000.0",
                    "liquidationPx": "1000.0",  # -50% away -> filtered (> 25%)
                    "leverage": {"type": "cross", "value": 2},
                }
            },
        ]
    }
    positions = lfa.extract_whale_liquidation_positions("0xWHALE1", ch, marks, max_dist_pct=25.0)
    assert len(positions) == 2
    clusters = lfa.aggregate_liquidation_clusters(positions, marks)
    assert len(clusters) == 1
    btc = clusters[0]
    assert btc.coin == "BTC"
    assert btc.long_liq_0_2_usd == pytest.approx(3_000_000.0)
    assert btc.short_liq_2_5_usd == pytest.approx(600_000.0)
    assert btc.nearest_long_dist_pct == pytest.approx(1.5, abs=0.05)
    assert btc.cascade_bias == "downside_cascade"


def test_hourly_funding_settlement_microstructure_math():
    # Construct 3 hourly :00 settlements where funding is +0.0005 (+5 bps/hr -> receiver is SHORT)
    # Price rises +2 bps in T-5m..T0, then snaps back -4 bps in T0..T+5m (good for SHORT receiver!)
    hour = 3_600_000
    minute = 60_000
    base_hour = 1_795_700_000_000 - (1_795_700_000_000 % hour)
    candles = []
    for h in range(3):
        t_settle = base_hour + (h + 1) * hour
        for m in range(-10, 10):
            ts = t_settle + m * minute
            if m == -5:
                o, c = 100.00, 100.01
            elif m == 0:
                o, c = 100.02, 100.00  # T+1m: -2 bps price drop -> +2 bps for Short!
            elif m == 4:
                o, c = 99.99, 99.98  # T+5m: -4 bps price drop -> +4 bps for Short!
            else:
                o, c = 100.00, 100.00
            candles.append({"t": ts, "o": str(o), "c": str(c)})

    row = lfa.compute_hourly_settlement_microstructure(
        candles, coin="TEST", mark_px=100.0, open_interest_usd=5_000_000.0, funding_hourly=0.0005
    )
    assert row is not None
    assert row.settlements_measured == 3
    assert row.receiver_side == "short"
    assert row.funding_hourly_bps == pytest.approx(5.0)
    assert row.avg_post_1m_bps == pytest.approx(2.0, abs=0.05)
    assert row.avg_post_5m_bps == pytest.approx(4.0, abs=0.05)
    # Holding through T0 means entering at T-5m: the short pays the -2 bps pre-settlement drift.
    # Net capture 5m = 5.0 funding - 2.0 pre-drift + 4.0 snapback - 3.0 maker round-trip = +4.0 bps.
    assert row.avg_pre_5m_bps == pytest.approx(-2.0, abs=0.05)
    assert row.net_capture_5m_bps == pytest.approx(4.0, abs=0.1)
    assert row.settlements_with_history == 0

    # Historical rates override the current one per settlement: if every past settlement was
    # NEGATIVE (longs received), the same candles are measured from the long side instead.
    hist = {base_hour + (h + 1) * hour: -0.0002 for h in range(3)}
    row_hist = lfa.compute_hourly_settlement_microstructure(
        candles, coin="TEST", mark_px=100.0, open_interest_usd=5_000_000.0, funding_hourly=0.0005,
        funding_history=hist,
    )
    assert row_hist is not None and row_hist.settlements_with_history == 3
    assert row_hist.receiver_side == "short"  # label still reflects the current rate
    # Long: 2.0 funding + 2.0 pre-drift - 4.0 post (price fell) - 3.0 = -3.0 bps.
    assert row_hist.net_capture_5m_bps == pytest.approx(-3.0, abs=0.1)
    assert lfa.parse_funding_history(
        [{"time": base_hour + hour + 59, "fundingRate": "-0.0002"}, {"time": "x"}, "junk"]
    ) == {base_hour + hour: -0.0002}

    snap = lfa.LiquidationAndFundingAlphaSnapshot(
        observed_at_ms=1_795_710_000_000,
        whales_scanned=40,
        whales_with_positions=25,
        liq_positions_count=12,
        clusters=[],
        funding_micro=[row],
    )
    assert "Карта ликвидаций" in lfa.format_liq_funding_markdown(snap)
    assert '<table bordered striped>' in lfa.format_liq_funding_telegram_rich_html(snap)


def test_select_active_whale_wallets_prioritizes_leveraged_directional_traders():
    rows = [
        # Huge low-ROI HFT market maker ($80M equity, $500M day vol, 0.001 ROI)
        {
            "ethAddress": "0xHFT_MM",
            "accountValue": "80000000",
            "windowPerformances": [["day", {"vlm": "500000000", "roi": "0.001"}], ["week", {"vlm": "3000000000", "roi": "0.002"}]],
        },
        # Directional leveraged whale ($2M equity, $5M day vol, 0.45 day ROI!)
        {
            "ethAddress": "0xDIR_WHALE_1",
            "accountValue": "2000000",
            "windowPerformances": [["day", {"vlm": "5000000", "roi": "0.45"}], ["week", {"vlm": "20000000", "roi": "0.90"}]],
        },
        # Another directional whale ($800k equity, -0.35 day ROI -> high abs ROI!)
        {
            "ethAddress": "0xDIR_WHALE_2",
            "accountValue": "800000",
            "windowPerformances": [["day", {"vlm": "3000000", "roi": "-0.35"}], ["week", {"vlm": "10000000", "roi": "-0.50"}]],
        },
    ]
    picked = lfa.select_active_whale_wallets(rows, top_n=3)
    assert picked[0] == "0xDIR_WHALE_1"
    assert picked[1] == "0xDIR_WHALE_2"
    assert "0xHFT_MM" in picked  # kept in the 25% high-volume sleeve without crowding out directional whales
