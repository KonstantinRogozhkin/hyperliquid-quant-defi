from __future__ import annotations

import pytest

from hl_quant import hl_funding_settlement_study as fss


def test_causal_settlement_event_alignment_and_dev_holdout_stats():
    hour = fss.HOUR_MS
    m5 = fss.FIVE_MIN_MS
    base = 1_795_000_000_000 - (1_795_000_000_000 % hour)

    candles = []
    funding = []
    # Build 20 hourly settlements with strongly negative funding (-0.0001 = -87.6% APR -> receiver is LONG)
    # Pre-settlement T-5m..T0 rallies +4 bps (crowding), post-settlement T0..T+5m dumps -8 bps (fade wins +8 bps!)
    for h in range(22):
        t0 = base + h * hour
        funding.append({"time": t0, "fundingRate": "-0.0001"})
        if h >= 1:
            # 5m bars around t0 (small realistic dispersion so sample std > 0 -> finite t-stat)
            jitter = 0.005 * ((h % 3) - 1)
            c_post5 = 99.96 + jitter
            c_post15 = 99.94 + jitter
            candles.extend(
                [
                    {"t": t0 - 3 * m5, "o": "100.00", "c": "100.00"},
                    {"t": t0 - m5, "o": "100.00", "c": "100.04"},
                    {"t": t0, "o": "100.04", "c": f"{c_post5:.4f}"},  # T0->T+5m: ~-8 bps drop -> fade short earns ~+8 bps!
                    {"t": t0 + m5, "o": f"{c_post5:.4f}", "c": "99.95"},
                    {"t": t0 + 2 * m5, "o": "99.95", "c": f"{c_post15:.4f}"},  # T0->T+15m: ~-10 bps drop
                ]
            )

    ev = fss.build_coin_settlement_events("GRAM", candles, funding, day_vol_usd=5_000_000, oi_usd=10_000_000)
    assert len(ev) == 21
    assert ev["pre_crowding_5m"].mean() == pytest.approx(4.0, abs=0.1)
    assert ev["post_unwind_fade_5m"].mean() == pytest.approx(8.0, abs=0.2)
    assert ev["post_unwind_fade_15m"].mean() == pytest.approx(10.0, abs=0.2)
    # Naive harvest: T-5m(100.00) -> T+5m(~99.96) = -4 bps price + 1 bp funding payout = -3 bps!
    assert ev["naive_harvest_5m"].mean() == pytest.approx(-3.0, abs=0.2)

    stats = fss.evaluate_settlement_strategies(ev)
    hld_fade = stats[(stats["split"] == "holdout") & (stats["strategy"] == "post_unwind_fade_5m") & (stats["bucket"] == "APR<=-25% (neg)")]
    assert len(hld_fade) == 1
    assert hld_fade.iloc[0]["mean_gross_bps"] == pytest.approx(8.0, abs=0.5)
    assert hld_fade.iloc[0]["net_maker_bps"] == pytest.approx(5.0, abs=0.5)
    assert hld_fade.iloc[0]["t_stat"] >= 5.0

    # Zero-lookahead contract: pre_crowding_* and naive_harvest_* must NEVER be evaluated inside pre5m>0 bucket
    pre_bucket_strats = set(stats[stats["bucket"] == "APR<=-50% + pre5m>0"]["strategy"])
    assert pre_bucket_strats <= {"post_unwind_fade_5m", "post_unwind_fade_15m"}

    meta = {"observed_at_ms": base, "coins_collected": 1, "total_settlement_events": len(ev), "span_days": 1.0}
    md = fss.format_settlement_study_markdown(stats, meta)
    rich = fss.format_settlement_study_telegram_rich_html(stats, meta)
    assert "GO (Maker)" in md
    assert '<table bordered striped>' in rich


def test_15m_settlement_events_and_ex_top1_coin_audit():
    hour = fss.HOUR_MS
    m15 = fss.FIFTEEN_MIN_MS
    base = 1_795_000_000_000 - (1_795_000_000_000 % hour)

    frames = []
    # Two coins with positive funding (+0.0001 = +87.6% APR -> receiver SHORT):
    # OUTLIER coin gives huge +80 bps on naive_harvest_15m; STEADY coin gives +2 bps (below 3 bp fee) -> Ex-Top1 catches it!
    for coin, drop_bps in (("OUTLIER", 79.0), ("STEADY", 1.0)):
        candles, funding = [], []
        for h in range(18):
            t0 = base + h * hour
            funding.append({"time": t0, "fundingRate": "0.0001"})
            if h >= 1:
                p_end = 100.0 * (1.0 - drop_bps * 1e-4)
                candles.extend(
                    [
                        {"t": t0 - m15, "o": "100.00", "c": "100.00"},
                        {"t": t0, "o": "100.00", "c": f"{p_end:.4f}"},
                        {"t": t0 + m15, "o": f"{p_end:.4f}", "c": f"{p_end:.4f}"},
                    ]
                )
        frames.append(fss.build_coin_settlement_events_15m(coin, candles, funding))

    import pandas as pd

    ev15 = pd.concat(frames, ignore_index=True)
    stats = fss.evaluate_settlement_strategies(ev15)
    row = stats[(stats["split"] == "holdout") & (stats["bucket"] == "APR>=+25% (pos)") & (stats["strategy"] == "naive_harvest_15m")].iloc[0]
    assert row["top1_coin"] == "OUTLIER"
    assert row["mean_gross_bps"] > 30.0  # inflated by OUTLIER
    assert row["ex_top1_gross_bps"] == pytest.approx(2.0, abs=0.2)  # Ex-Top1 reveals STEADY is only +2 bps!


def test_fetch_settlement_dataset_retries_429_and_tracks_failed_coins(monkeypatch):
    import asyncio
    import json

    import httpx

    hour = fss.HOUR_MS
    m15 = fss.FIFTEEN_MIN_MS
    base = 1_795_000_000_000 - (1_795_000_000_000 % hour)
    candles_ok = [
        {"t": base + h * hour + off, "o": "100.0", "c": "100.0"}
        for h in range(4)
        for off in (-m15, 0, m15)
    ]
    fund_ok = [{"time": base + h * hour, "fundingRate": "0.0001"} for h in range(4)]

    calls: dict[str, int] = {}

    async def fake_sleep(_s: float) -> None:
        return None

    monkeypatch.setattr(fss.asyncio, "sleep", fake_sleep)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        rtype = body.get("type")
        if rtype == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC"}, {"name": "ETH"}]},
                    [
                        {"markPx": "60000", "openInterest": "1000", "dayNtlVlm": "50000000", "funding": "0.0001"},
                        {"markPx": "2000", "openInterest": "10000", "dayNtlVlm": "20000000", "funding": "0.0001"},
                    ],
                ],
            )
        if rtype == "candleSnapshot":
            coin = body["req"]["coin"]
            key = f"candle:{coin}"
            calls[key] = calls.get(key, 0) + 1
            if coin == "BTC" and calls[key] <= 2:
                return httpx.Response(429, headers={"Retry-After": "0.1"}, json={"error": "rate_limit"})
            if coin == "ETH":
                return httpx.Response(400, json={"error": "bad_coin"})
            return httpx.Response(200, json=candles_ok)
        if rtype == "fundingHistory":
            coin = body["coin"]
            key = f"fund:{coin}"
            calls[key] = calls.get(key, 0) + 1
            if calls[key] == 1:
                return httpx.Response(429, json={"error": "rate_limit"})
            return httpx.Response(200, json=fund_ok)
        return httpx.Response(404)

    real_async_client = httpx.AsyncClient

    def _patched_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fss.httpx, "AsyncClient", _patched_client)

    df, meta = asyncio.run(fss.fetch_settlement_dataset(bar_interval="15m", max_coins=2))
    assert not df.empty
    assert meta["coins_requested"] == 2
    assert meta["coins_collected"] == 1
    assert meta["coins_failed"] == ["ETH"]
    assert calls["candle:BTC"] == 3
    assert calls["fund:BTC"] == 2
