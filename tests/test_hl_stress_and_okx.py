from __future__ import annotations

import json

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import cex_perp_public as cex
from hl_quant import execution_stress as es
from hl_quant import funding_arb as fa

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def test_okx_symbol_and_collectors_parse_mocked_pages():
    assert cex.okx_symbol("BTC") == "BTC-USDT-SWAP"
    assert cex.okx_symbol("kPEPE") == "PEPE-USDT-SWAP"

    def handler(req: httpx.Request) -> httpx.Response:
        if "funding-rate-history" in str(req.url):
            return httpx.Response(
                200,
                text=json.dumps(
                    {
                        "code": "0",
                        "msg": "",
                        "data": [
                            {"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001", "fundingTime": str(T0 + 8 * H)},
                            {"instId": "BTC-USDT-SWAP", "fundingRate": "0.0002", "fundingTime": str(T0)},
                        ],
                    }
                ),
            )
        return httpx.Response(
            200,
            text=json.dumps(
                {
                    "code": "0",
                    "msg": "",
                    "data": [
                        [str(T0 + H), "101", "102", "100", "101.5", "10", "10", "1015", "1"],
                        [str(T0), "100", "101", "99", "101", "10", "10", "1010", "1"],
                    ],
                }
            ),
        )

    transport = httpx.MockTransport(handler)
    with cex.PublicGetClient(cex.OKX_API, transport=transport) as client:
        f = cex.collect_funding(client, "okx", "BTC-USDT-SWAP", T0, T0 + 10 * H, now_ms=T0 + 12 * H)
        k = cex.collect_klines_1h(client, "okx", "BTC-USDT-SWAP", T0, T0 + 2 * H, now_ms=T0 + 12 * H)
    assert len(f.rows) == 2
    assert f.rows["funding_rate"].tolist() == [0.0002, 0.0001]
    assert len(k.rows) == 2
    assert k.rows["close"].tolist() == [101.0, 101.5]


def test_execution_monte_carlo_monotonically_degrades_with_friction():
    idx = pd.Index(T0 + np.arange(500) * H, name="hour_ms")
    r = pd.Series(np.full(500, 1e-4), index=idx)
    dw = pd.Series(np.where(np.arange(500) % 12 == 0, 0.5, 0.0), index=idx)
    lv = pd.Series(np.full(500, 0.005), index=idx)

    mc = es.run_execution_monte_carlo(r, dw, lv, n_paths=100, seed=7)
    aprs = [x.median_apr for x in mc]
    assert aprs[0] > aprs[1] > aprs[2] > aprs[3]


def test_margin_buffer_audit_scales_with_leverage():
    idx = pd.Index(T0 + np.arange(200) * H, name="hour_ms")
    ret = pd.DataFrame({"A": np.full(200, -0.001)}, index=idx)
    w = pd.DataFrame({"A": np.full(200, 0.5)}, index=idx)
    rows = es.margin_buffer_audit(ret, w, leverages=(1.0, 2.0, 3.0))
    assert abs(rows[2]["worst_72h_single_leg_pct"]) > abs(rows[0]["worst_72h_single_leg_pct"])
    assert rows[2]["recommended_free_margin_pct"] >= rows[0]["recommended_free_margin_pct"]


def test_metamodel_stream_matches_sleeve_simulators_and_scales_cross_venue_turnover():
    rng = np.random.default_rng(3)
    n_hours, coins = 400, [f"C{i}" for i in range(12)]
    hours = np.arange(n_hours, dtype="int64") * 3_600_000
    rows = []
    for i, coin in enumerate(coins):
        px = 50.0 * np.exp(np.cumsum(rng.normal(0.0, 0.004, n_hours)))
        rate = (3e-4 if i < 3 else 0.0) + rng.normal(0.0, 1e-5, n_hours)
        rows.append(pd.DataFrame({
            "hour_ms": hours, "coin": coin, "hl_close": px, "hl_notional_usd": 1e6, "hl_fund_next": rate, "hl_rate_h": rate,
            "bybit_close": px * (1 + rng.normal(0, 3e-4, n_hours)), "bybit_notional_usd": 1e6, "bybit_fund_next": 0.0, "bybit_rate_h": 0.0,
        }))
    market = af.build_wide_market(pd.concat(rows, ignore_index=True))
    reg = {s.name: s for s in af.default_hypothesis_registry()}

    ret, turn, leg_vol, delay = es.metamodel_stream(market, {"xs_rev_L24_r12": 1.0})
    sim = af.simulate_hl_single_venue(market, af.build_xs_price_weights(market, reg["xs_rev_L24_r12"].params), one_way_cost_bps=1.5)
    np.testing.assert_allclose(ret.to_numpy(), sim["ret"].to_numpy())
    np.testing.assert_allclose(turn.to_numpy(), sim["turnover"].to_numpy())
    assert np.isfinite(leg_vol.iloc[48:]).all()
    assert (delay == 0.0).all()  # single-venue trades have no cross-venue leg to leave unhedged

    spec = reg["carry_bybit_L168_in5"]
    _, turn_c, _, delay_c = es.metamodel_stream(market, {spec.name: 1.0})
    sim_c = af.simulate_cross_venue(market, af.build_funding_carry_weights(market, spec.params)[0], one_way_cost_bps=3.5)
    assert sim_c["turnover"].sum() > 0
    # A cross-venue change dw trades dw * C / 2 on each of two legs -> dw units of capital;
    # one delayed entry leg exposes 0.25 * cex_turnover of total capital.
    np.testing.assert_allclose(turn_c.to_numpy(), sim_c["turnover"].to_numpy())
    np.testing.assert_allclose(delay_c.to_numpy(), 0.25 * sim_c["turnover"].to_numpy())


def test_leg_delay_shock_only_hits_cross_venue_exposure():
    idx = pd.RangeIndex(500)
    r = pd.Series(1e-4, index=idx)
    dw = pd.Series(0.5, index=idx)
    lv = pd.Series(0.01, index=idx)
    delay_only = (es.StressScenario("delay", miss_prob=0.0, taker_extra_bps=0.0, random_slip_max_bps=1e-9, unhedged_leg_delay_prob=1.0),)
    hl_only = es.run_execution_monte_carlo(r, dw, lv, scenarios=delay_only, n_paths=50, delay_exposure=pd.Series(0.0, index=idx))
    cross = es.run_execution_monte_carlo(r, dw, lv, scenarios=delay_only, n_paths=50, delay_exposure=dw)
    assert hl_only[0].median_apr > cross[0].median_apr
    assert hl_only[0].median_apr == pytest.approx(1e-4 * 8760, rel=1e-3)
