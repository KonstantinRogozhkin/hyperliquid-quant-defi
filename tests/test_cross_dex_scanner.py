from __future__ import annotations

import json

import httpx
import pytest

from hl_quant import cross_dex_scanner as cds


def test_cross_dex_parsers_and_scanner_with_price_parity_guard():
    hl_payload = [
        {"universe": [{"name": "BTC", "isDelisted": False}, {"name": "PURR", "isDelisted": False}]},
        [
            {"markPx": "60000", "funding": "0.0001", "openInterest": "1000", "dayNtlVlm": "50000000", "impactPxs": ["59995", "60005"]},
            {"markPx": "0.10", "funding": "0.0005", "openInterest": "100000", "dayNtlVlm": "5000000"},
        ],
    ]
    dydx_payload = {
        "markets": {
            "BTC-USD": {"status": "ACTIVE", "oraclePrice": "59980", "nextFundingRate": "0.00002", "volume24H": "10000000", "openInterest": "200"},
            "PURR-USD": {"status": "ACTIVE", "oraclePrice": "10.0", "nextFundingRate": "0.0", "volume24H": "5000000", "openInterest": "100"},
        }
    }
    paradex_payload = {
        "results": [
            {
                "symbol": "BTC-USD-PERP",
                "mark_price": "59990",
                "funding_rate": "-0.0008",
                "funding_interval_minutes": 480,  # -0.0001 per hour!
                "volume_24h": "3000000",
                "open_interest": "150",
                "bid": "59985",
                "ask": "59995",
            }
        ]
    }

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "hyperliquid" in u:
            return httpx.Response(200, text=json.dumps(hl_payload))
        if "dydx" in u:
            return httpx.Response(200, text=json.dumps(dydx_payload))
        return httpx.Response(200, text=json.dumps(paradex_payload))

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        hl_q = cds.fetch_hyperliquid_quotes(client)
        dydx_q = cds.fetch_dydx_quotes(client)
        pdx_q = cds.fetch_paradex_quotes(client)

    assert pdx_q["BTC"].funding_rate_h == pytest.approx(-0.0001)
    df_dydx = cds.scan_cross_dex_opportunities(hl_q, dydx_q, min_day_vol_usd=1_000_000.0)
    # PURR has 100x price mismatch -> must be rejected by parity guard!
    assert df_dydx["coin"].tolist() == ["BTC"]
    assert df_dydx.iloc[0]["direction"] == "Short HL / Long dydx"

    df_pdx = cds.scan_cross_dex_opportunities(hl_q, pdx_q, min_day_vol_usd=1_000_000.0)
    assert df_pdx.iloc[0]["spread_apr_pct"] == pytest.approx(0.0002 * 8760 * 100.0, abs=0.05)


def test_cross_dex_v2_l2_depth_vwap_and_lighter_extended_parsing():
    # VWAP across 2 levels: $6k at 100.0 + $4k at 100.5 -> weighted price = 10000 / (60 + 39.801) = 100.1994
    vwap, filled = cds.compute_book_vwap([(100.0, 60.0), (100.5, 100.0)], 10_000.0)
    assert filled == pytest.approx(10_000.0)
    assert vwap == pytest.approx(100.1994, abs=0.01)

    eb, sl, ok = cds.compute_executable_cross_dex_entry_bps(
        hl_bids=[(100.20, 200.0)],
        hl_asks=[(100.22, 200.0)],
        other_bids=[(99.98, 200.0)],
        other_asks=[(100.00, 200.0)],
        short_hl=True,
        clip_usd=10_000.0,
    )
    assert ok is True
    assert eb == pytest.approx(20.0, abs=0.2)
    assert sl == pytest.approx(2.0, abs=0.3)

    def handler(req: httpx.Request) -> httpx.Response:
        u = str(req.url)
        if "orderBookDetails" in u:
            return httpx.Response(200, json={"order_book_details": [{"symbol": "BTC", "market_id": 1, "market_type": "perp", "status": "active", "last_trade_price": 60000.0, "daily_quote_token_volume": 5e8, "open_interest": 1000.0}]})
        if "funding-rates" in u:
            return httpx.Response(200, json={"funding_rates": [{"exchange": "lighter", "market_id": 1, "symbol": "BTC", "rate": 0.0008}]})
        if "extended.exchange" in u:
            return httpx.Response(200, json={"data": [{"name": "BTC-USD", "type": "PERPETUAL", "marketStats": {"markPrice": "60010", "fundingRate": "0.00002", "dailyVolume": "25000000", "openInterest": "15000000", "bidPrice": "60005", "askPrice": "60010"}}]})
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        lq = cds.fetch_lighter_quotes(client)
        eq = cds.fetch_extended_quotes(client)
    assert lq["BTC"].funding_rate_h == pytest.approx(0.0001)  # 0.0008 / 8h
    assert eq["BTC"].funding_rate_h == pytest.approx(0.00002)
    assert eq["BTC"].spread_bps == pytest.approx(5.0 / 60007.5 * 1e4, abs=0.05)
