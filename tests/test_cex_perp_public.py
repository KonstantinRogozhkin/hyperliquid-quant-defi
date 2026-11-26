from __future__ import annotations

import httpx
import pytest

from hl_quant import cex_perp_public as cex
from hl_quant import hyperliquid as hl

H = cex.HOUR_MS
NOW = 1_795_000_000_000 - (1_795_000_000_000 % H)


def _client(base, handler):
    return cex.PublicGetClient(base, transport=httpx.MockTransport(handler), sleep=lambda _s: None)


def _q(request, key, default=None):
    value = request.url.params.get(key)
    return default if value is None else int(value)


def test_binance_funding_paginates_ascending():
    times = [NOW - (2500 - i) * 8 * H for i in range(2500)]

    def handler(request):
        lo, hi, lim = _q(request, "startTime"), _q(request, "endTime"), _q(request, "limit")
        page = [t for t in times if lo <= t <= hi][:lim]
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingTime": t + 3, "fundingRate": "0.0001", "markPrice": "1"} for t in page])

    got = cex.collect_funding(_client(cex.BINANCE_FAPI, handler), "binance", "BTCUSDT", times[0], NOW, now_ms=NOW)
    assert got.notes["page_count"] == 3
    assert len(got.rows) == 2500
    assert got.provider == "binance" and got.dataset == "binance_funding"


def test_bybit_funding_pages_backwards():
    times = [NOW - i * 8 * H for i in range(450)]  # newest first, like the API

    def handler(request):
        lo, hi, lim = _q(request, "startTime"), _q(request, "endTime"), _q(request, "limit")
        page = [t for t in times if lo <= t <= hi][:lim]
        return httpx.Response(
            200,
            json={"retCode": 0, "retMsg": "OK", "result": {"list": [{"symbol": "BTCUSDT", "fundingRate": "0.00005", "fundingRateTimestamp": str(t)} for t in page]}},
        )

    got = cex.collect_funding(_client(cex.BYBIT_API, handler), "bybit", "BTCUSDT", times[-1], NOW, now_ms=NOW)
    assert got.notes["page_count"] == 3
    assert got.rows["time_ms"].is_monotonic_increasing
    assert len(got.rows) == 450


def test_bybit_error_code_is_terminal():
    def handler(request):
        return httpx.Response(200, json={"retCode": 10001, "retMsg": "params error", "result": {}})

    with pytest.raises(cex.CexDataError, match="10001"):
        cex.collect_funding(_client(cex.BYBIT_API, handler), "bybit", "BTCUSDT", NOW - H, NOW, now_ms=NOW)


def _kline_handler(venue):
    def handler(request):
        if venue == "binance":
            lo, hi, lim = _q(request, "startTime"), _q(request, "endTime"), _q(request, "limit")
            opens = list(range(lo - lo % H, hi + 1, H))[:lim]
            return httpx.Response(200, json=[[t, "1", "2", "0.5", "1.5", "10", t + H - 1] for t in opens])
        lo, hi, lim = _q(request, "start"), _q(request, "end"), _q(request, "limit")
        opens = sorted(range(lo - lo % H, hi + 1, H), reverse=True)[:lim]
        return httpx.Response(200, json={"retCode": 0, "result": {"list": [[str(t), "1", "2", "0.5", "1.5", "10", "15"] for t in opens]}})

    return handler


@pytest.mark.parametrize("venue,base", [("binance", cex.BINANCE_FAPI), ("bybit", cex.BYBIT_API)])
def test_klines_paginate_and_drop_open_bar(venue, base):
    start = NOW - 2000 * H
    observed = NOW + H // 2  # bar opening at NOW is still open
    got = cex.collect_klines_1h(_client(base, _kline_handler(venue)), venue, "BTCUSDT", start, NOW, now_ms=observed)
    assert len(got.rows) == 2000
    assert got.rows["open_time_ms"].is_monotonic_increasing
    assert int(got.rows["open_time_ms"].iloc[-1]) == NOW - H
    assert got.status == hl.STATUS_COMPLETE


def test_cex_run_publishes_with_its_provider(tmp_path):
    def handler(request):
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingTime": NOW - H, "fundingRate": "0.0001"}])

    got = cex.collect_funding(_client(cex.BINANCE_FAPI, handler), "binance", "BTCUSDT", NOW - 2 * H, NOW, now_ms=NOW)
    result = hl.publish(got, tmp_path, base_url=cex.BINANCE_FAPI)
    assert hl.verify_run(result.run_dir)["provider"] == "binance"
    assert result.run_dir.parent.name == "binance_funding"
