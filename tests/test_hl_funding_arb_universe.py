from __future__ import annotations

import json

import httpx
import numpy as np
import pandas as pd

from hl_quant import cex_perp_public as cex
from hl_quant import funding_arb as fa
from hl_quant import hyperliquid as hl
from hl_quant import universe_selection as us

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def _bars(n: int, *, start_idx: int = 0, price: float = 100.0, hourly_vol: float = 5_000.0) -> pd.DataFrame:
    idx = np.arange(start_idx, start_idx + n)
    vol = np.full(n, hourly_vol, dtype=float) if np.isscalar(hourly_vol) else np.asarray(hourly_vol, dtype=float)
    return pd.DataFrame(
        {
            "open_time_ms": T0 + idx * H,
            "close": np.full(n, price, dtype=float),
            "volume": vol,
        }
    )


def _hl_funding(n: int, rate, *, start_idx: int = 0) -> pd.DataFrame:
    idx = np.arange(start_idx, start_idx + n)
    rates = np.full(n, rate, dtype=float) if np.isscalar(rate) else np.asarray(rate, dtype=float)
    return pd.DataFrame({"time_ms": T0 + (idx + 1) * H + 11, "funding_rate": rates})


def _cex_funding(n: int, rate: float = 0.0, *, start_idx: int = 0, every: int = 8) -> pd.DataFrame:
    hours = np.arange(start_idx + every, start_idx + n + 1, every)
    return pd.DataFrame({"time_ms": T0 + hours * H + 3, "funding_rate": np.full(len(hours), rate, dtype=float)})


def _panel(
    n: int = 600,
    *,
    start_idx: int = 0,
    hl_rate=1e-4,
    cex_rate: float = 0.0,
    hl_hourly_vol=5_000.0,
    cex_hourly_vol=5_000.0,
) -> pd.DataFrame:
    return fa.build_panel(
        _hl_funding(n, hl_rate, start_idx=start_idx),
        _cex_funding(n, cex_rate, start_idx=start_idx),
        _bars(n, start_idx=start_idx, hourly_vol=hl_hourly_vol),
        _bars(n, start_idx=start_idx, hourly_vol=cex_hourly_vol),
    )


def test_cex_symbol_maps_k_prefix():
    assert us.cex_symbol("BTC") == "BTCUSDT"
    assert us.cex_symbol("kPEPE") == "1000PEPEUSDT"
    assert us.cex_symbol("kSHIB") == "1000SHIBUSDT"


def test_discover_common_coins_filters_delisted_and_intersects_binance():
    meta_ctx = [
        {
            "universe": [
                {"name": "BTC", "isDelisted": False},
                {"name": "kPEPE", "isDelisted": False},
                {"name": "DEAD", "isDelisted": True},
                {"name": "HLONLY", "isDelisted": False},
                {"name": "TINY", "isDelisted": False},
            ]
        },
        [
            {"dayNtlVlm": "500000000", "openInterest": "1000", "markPx": "100000"},
            {"dayNtlVlm": "20000000", "openInterest": "500000", "markPx": "0.01"},
            {"dayNtlVlm": "99000000", "openInterest": "10", "markPx": "10"},
            {"dayNtlVlm": "50000000", "openInterest": "100", "markPx": "50"},
            {"dayNtlVlm": "100000", "openInterest": "10", "markPx": "1"},
        ],
    ]
    binance_info = {
        "symbols": [
            {"symbol": "BTCUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "1000PEPEUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "DEADUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
            {"symbol": "TINYUSDT", "status": "TRADING", "contractType": "PERPETUAL", "quoteAsset": "USDT"},
        ]
    }
    hl_transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=json.dumps(meta_ctx)))
    cex_transport = httpx.MockTransport(lambda _req: httpx.Response(200, text=json.dumps(binance_info)))

    with (
        hl.HyperliquidInfoClient(transport=hl_transport) as hl_client,
        cex.PublicGetClient(cex.BINANCE_FAPI, transport=cex_transport) as cex_client,
    ):
        found = us.discover_common_coins(hl_client, cex_client, "binance", min_prefilter_vol_usd=1_000_000.0)
        # Backtests: keep the delisted perp (survivorship-free), beyond the max_coins cap.
        with_dead = us.discover_common_coins(
            hl_client, cex_client, "binance", min_prefilter_vol_usd=1_000_000.0, max_coins=1, include_delisted=True
        )
    assert [c.coin for c in found] == ["BTC", "kPEPE"]
    assert found[1].cex_symbol == "1000PEPEUSDT"
    assert [c.coin for c in with_dead] == ["BTC", "DEAD"]
    assert with_dead[1].is_delisted is True


def test_train_liquidity_ignores_test_volume_spikes():
    n = 480  # 20 days
    split = T0 + 288 * H  # first 12 days train, last 8 days test
    # Low train volume ($100 * 100 * 24 = $240k/day), huge test volume ($240M/day)
    vol_a = np.r_[np.full(288, 100.0), np.full(192, 100.0)]
    vol_b = np.r_[np.full(288, 100.0), np.full(192, 100_000.0)]
    pa = _panel(n=n, hl_hourly_vol=vol_a, cex_hourly_vol=vol_a)
    pb = _panel(n=n, hl_hourly_vol=vol_b, cex_hourly_vol=vol_b)

    sa = us.train_liquidity_stats("LOW", pa, split_hour_ms=split, expected_train_hours=288, min_train_daily_vol_usd=1_000_000.0)
    sb = us.train_liquidity_stats("LOW", pb, split_hour_ms=split, expected_train_hours=288, min_train_daily_vol_usd=1_000_000.0)
    assert sa == sb
    assert not sa.eligible
    assert "min_daily_vol" in sa.reject_reason


def test_evaluate_universe_zero_test_leakage_and_coverage_filter():
    n = 600
    rng = np.random.default_rng(42)
    split_idx = int(n * 0.6)
    grid = [fa.Params(8, 0.05, 0.025), fa.Params(24, 0.10, 0.05)]
    costs = fa.Costs(hl_fee_bps=1.5, cex_fee_bps=2.0, slippage_bps_per_leg=0.0)

    # Strong coin A, moderate coin B, weak coin C, illiquid coin D, late-listed coin E
    r_a = 3e-4 + rng.normal(0, 2e-5, n)
    r_b = 1.5e-4 + rng.normal(0, 2e-5, n)
    r_c = -1e-5 + rng.normal(0, 2e-5, n)

    panels_base = {
        "A": _panel(n=n, hl_rate=r_a, hl_hourly_vol=5_000.0, cex_hourly_vol=5_000.0),  # $12M/day
        "B": _panel(n=n, hl_rate=r_b, hl_hourly_vol=5_000.0, cex_hourly_vol=5_000.0),
        "C": _panel(n=n, hl_rate=r_c, hl_hourly_vol=5_000.0, cex_hourly_vol=5_000.0),
        "ILLIQ": _panel(n=n, hl_rate=r_a * 2, hl_hourly_vol=50.0, cex_hourly_vol=5_000.0),  # $120k/day on HL
        "NEW": _panel(n=200, start_idx=400, hl_rate=r_a[:200], hl_hourly_vol=5_000.0, cex_hourly_vol=5_000.0),
    }

    # Mutate test period heavily for C (huge funding) and ILLIQ (huge volume)
    r_c_mut = r_c.copy()
    r_c_mut[split_idx + 10 :] = 2e-3
    vol_illiq_mut = np.r_[np.full(split_idx, 50.0), np.full(n - split_idx, 50_000.0)]
    panels_mut = {
        **panels_base,
        "C": _panel(n=n, hl_rate=r_c_mut, hl_hourly_vol=5_000.0, cex_hourly_vol=5_000.0),
        "ILLIQ": _panel(n=n, hl_rate=r_a * 2, hl_hourly_vol=vol_illiq_mut, cex_hourly_vol=5_000.0),
    }

    ea = us.evaluate_universe(panels_base, costs=costs, grid=grid, top_k=2, min_train_daily_vol_usd=5_000_000.0)
    eb = us.evaluate_universe(panels_mut, costs=costs, grid=grid, top_k=2, min_train_daily_vol_usd=5_000_000.0)

    assert ea.eligible_coins == ["A", "B", "C"]
    assert eb.eligible_coins == ["A", "B", "C"]
    assert ea.selected_coins == ["A", "B"]
    assert eb.selected_coins == ["A", "B"]
    assert ea.selected_eval.chosen == eb.selected_eval.chosen
    assert ea.train_ranking.equals(eb.train_ranking)
    assert ea.all_eligible_eval is not None
    assert eb.all_eligible_eval is not None
    assert ea.all_eligible_eval.chosen_test != eb.all_eligible_eval.chosen_test
