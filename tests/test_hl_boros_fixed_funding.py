from __future__ import annotations

import pytest

from hl_quant import cross_dex_scanner as cds
from hl_quant import hl_boros_fixed_funding as boros


def test_boros_market_parsing_fixed_lock_and_spread_market_detection():
    expired = {"marketId": 1, "imData": {"name": "Expired BTC"}, "data": {"timeToMaturity": 0}}
    assert boros.parse_boros_market(expired) is None

    raw_hype = {
        "marketId": 245,
        "imData": {"name": "Hyperliquid HYPE 25 Dec 2026", "symbol": "HL-HYPE-25DEC2026", "maturity": 1798156800},
        "metadata": {"underlyingSymbol": "HYPE", "fundingRateSymbol": "hyperliquid-hype", "isSpreadMarket": False},
        "data": {
            "timeToMaturity": 28.0 * 86400,
            "markApr": 0.0920,
            "midApr": 0.0915,
            "bestBid": 0.0894,
            "bestAsk": 0.0937,
            "floatingApr": 0.1095,
            "notionalOI": 391776.9,
            "volume24h": 50000.0,
        },
    }
    raw_spread = {
        "marketId": 242,
        "imData": {"name": "Hyperliquid ETH - Gate ETHUSDT 25 Dec 2026", "symbol": "SPR-HL-GATE-ETH", "maturity": 1798156800},
        "metadata": {"underlyingSymbol": "ETH", "fundingRateSymbol": "hl-gate-eth", "isSpreadMarket": True},
        "data": {
            "timeToMaturity": 28.0 * 86400,
            "markApr": 0.0229,
            "midApr": 0.0229,
            "bestBid": 0.0228,
            "bestAsk": 0.0230,
            "floatingApr": -0.0384,
            "notionalOI": 9958.0,
            "volume24h": 1200.0,
        },
    }
    lt_q = {
        "HYPE": cds.DexMarketQuote(
            dex="lighter",
            coin="HYPE",
            raw_symbol="99",
            mark_px=35.0,
            funding_rate_h=0.0,
            funding_apr_pct=3.0,
            day_vol_usd=10_000_000.0,
            oi_usd=5_000_000.0,
            spread_bps=1.0,
        )
    }

    m_hype = boros.parse_boros_market(raw_hype, lighter_quotes=lt_q)
    assert m_hype is not None
    assert m_hype.is_hyperliquid is True
    assert m_hype.best_bid_apr_pct == pytest.approx(8.94)
    assert m_hype.days_to_maturity == pytest.approx(28.0)
    assert m_hype.locked_hl_minus_lt_apr_pct == pytest.approx(5.94)
    assert "Lock +8.94%" in m_hype.recommended_action

    m_spr = boros.parse_boros_market(raw_spread)
    assert m_spr is not None
    assert m_spr.is_spread_market is True
    assert m_spr.fixed_minus_float_bid_pct == pytest.approx(6.12)
    assert "Short Boros Spread" in m_spr.recommended_action

    snap = boros.BorosScannerSnapshot(
        observed_at_ms=1_795_800_000_000,
        total_markets_scanned=249,
        active_markets_count=39,
        hl_and_spread_count=2,
        markets=[m_hype, m_spr],
    )
    md = boros.format_boros_markdown(snap)
    rich = boros.format_boros_telegram_rich_html(snap)
    assert "Hyperliquid HYPE 25 Dec 2026" in md
    assert "[SPREAD]" in md
    assert '<table bordered striped>' in rich
