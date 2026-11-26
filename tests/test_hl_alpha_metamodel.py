from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import funding_arb as fa
from hl_quant import metamodel as mm

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def _synthetic_bundle(n_hours: int = 24 * 120, n_coins: int = 8, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    hours = T0 + np.arange(n_hours, dtype="int64") * H
    rows = []
    for c_idx in range(n_coins):
        coin = f"C{c_idx}"
        r_hl = rng.normal(0.0, 0.004, n_hours)
        hl_px = 100.0 * np.exp(np.cumsum(r_hl))
        basis = rng.normal(0.0, 0.001, n_hours)
        by_px = hl_px * np.exp(-basis)
        hl_rate = 1e-4 * (c_idx - n_coins / 2) / n_coins + rng.normal(0.0, 3e-5, n_hours)
        by_rate = rng.normal(0.0, 2e-5, n_hours)
        df = pd.DataFrame(
            {
                "hour_ms": hours,
                "coin": coin,
                "hl_close": hl_px,
                "hl_ret": pd.Series(hl_px).pct_change().to_numpy(),
                "hl_notional_usd": np.full(n_hours, 500_000.0),  # $12M/day
                "hl_fund_next": hl_rate,
                "hl_rate_h": hl_rate,
                "bybit_close": by_px,
                "bybit_ret": pd.Series(by_px).pct_change().to_numpy(),
                "bybit_notional_usd": np.full(n_hours, 500_000.0),
                "bybit_fund_next": by_rate,
                "bybit_rate_h": by_rate,
                "binance_close": by_px,
                "binance_ret": pd.Series(by_px).pct_change().to_numpy(),
                "binance_notional_usd": np.full(n_hours, 500_000.0),
                "binance_fund_next": by_rate,
                "binance_rate_h": by_rate,
            }
        )
        rows.append(df)
    return pd.concat(rows, ignore_index=True)


def test_all_six_families_zero_holdout_leakage():
    bundle_a = _synthetic_bundle(n_hours=24 * 120, n_coins=8, seed=13)
    market_a = af.build_wide_market(bundle_a)
    dev_s, hold_s, folds = af.make_walk_forward_splits(len(market_a.hours), train_days=40, fold_days=15, holdout_days=30)
    hold_hour_ms = int(market_a.hours[hold_s])

    # Mutate prices and funding aggressively strictly inside holdout (>= hold_hour_ms)
    bundle_b = bundle_a.copy()
    mask_hold = bundle_b["hour_ms"] >= hold_hour_ms
    bundle_b.loc[mask_hold, "hl_close"] *= 1.5
    bundle_b.loc[mask_hold, "hl_rate_h"] = -5e-3
    bundle_b.loc[mask_hold, "hl_fund_next"] = -5e-3
    market_b = af.build_wide_market(bundle_b)

    specs = [
        af.HypothesisSpec("f1", "funding_carry_arb", "cross_venue", {"mode": "bybit", "lookback_h": 24, "entry_apr": 0.05, "min_vol_usd": 1e6}),
        af.HypothesisSpec("f2", "basis_mean_reversion", "cross_venue", {"lookback_h": 24, "z_in": 1.5, "min_vol_usd": 1e6}),
        af.HypothesisSpec("f3", "hl_xs_funding_contrarian", "hl_single_venue", {"lookback_h": 24, "rebalance_h": 8, "top_n": 2, "min_vol_usd": 1e6}),
        af.HypothesisSpec("f4", "hl_xs_price_reversal_mom", "hl_single_venue", {"lookback_h": 12, "rebalance_h": 8, "top_n": 2, "sign": -1.0, "min_vol_usd": 1e6}),
        af.HypothesisSpec("f5", "hl_xs_velocity_div", "hl_single_venue", {"variant": "funding_vel", "rebalance_h": 8, "top_n": 2, "sign": -1.0, "min_vol_usd": 1e6}),
        af.HypothesisSpec("f6", "ml_walkforward_ranker", "hl_single_venue", {"model": "ridge", "horizon_h": 12, "rebalance_h": 12, "top_n": 2, "min_vol_usd": 1e6}),
    ]

    for spec in specs:
        ea = af.evaluate_hypothesis(market_a, spec, dev_start_idx=dev_s, holdout_start_idx=hold_s, fold_boundaries=folds)
        eb = af.evaluate_hypothesis(market_b, spec, dev_start_idx=dev_s, holdout_start_idx=hold_s, fold_boundaries=folds)
        # Dev returns strictly before holdout_start - 1 (last dev hour accrues return to holdout_start) must be identical!
        dev_strict = market_a.hours[dev_s : hold_s - 1]
        np.testing.assert_allclose(
            ea.hourly_returns.loc[dev_strict].to_numpy(),
            eb.hourly_returns.loc[dev_strict].to_numpy(),
            rtol=1e-12,
            atol=1e-12,
            err_msg=f"Leakage detected in family {spec.family}",
        )


def test_metamodel_ignores_holdout_when_selecting_and_fitting():
    n = 1000
    hours = T0 + np.arange(n, dtype="int64") * H
    hold_ms = int(hours[700])
    rng = np.random.default_rng(99)

    r1 = rng.normal(3e-4, 1e-3, n)
    r2 = rng.normal(2e-4, 1e-3, n)
    r3 = rng.normal(-1e-4, 1e-3, n)
    oof_a = pd.DataFrame({"h1": r1, "h2": r2, "h3": r3}, index=hours)
    stress_a = oof_a - 1e-5

    # Mutate holdout heavily: make h3 amazing and h1 terrible on holdout
    oof_b = oof_a.copy()
    oof_b.loc[hours >= hold_ms, "h1"] = -5e-3
    oof_b.loc[hours >= hold_ms, "h3"] = +5e-3
    stress_b = oof_b - 1e-5

    def make_summaries(df_base, df_stress):
        dev = df_base.index < hold_ms
        out = []
        for name, fam in (("h1", "famA"), ("h2", "famB"), ("h3", "famC")):
            dm = af.series_metrics(df_base.loc[dev, name])
            ds = af.series_metrics(df_stress.loc[dev, name])
            out.append(
                {
                    "name": name,
                    "family": fam,
                    "dev_metrics": dm,
                    "dev_stressed_metrics": ds,
                    "dev_pos_fold_ratio": 1.0 if dm["sharpe"] > 0 else 0.0,
                }
            )
        return out

    ma = mm.build_metamodel(make_summaries(oof_a, stress_a), oof_a, stress_a, holdout_start_ms=hold_ms)
    mb = mm.build_metamodel(make_summaries(oof_b, stress_b), oof_b, stress_b, holdout_start_ms=hold_ms)

    assert ma.selected_names == mb.selected_names == ["h1", "h2"]
    assert ma.weights_by_method == mb.weights_by_method
    assert ma.dev_metrics == mb.dev_metrics
    assert ma.holdout_metrics != mb.holdout_metrics


def test_causal_liquid_mask_rejects_mismatched_cross_venue_price_scale():
    bundle = _synthetic_bundle(n_hours=24 * 40, n_coins=3, seed=5)
    # Simulate mismatched asset like HL PURR vs Bybit PURRUSDT (100x price ratio)
    bundle.loc[bundle["coin"] == "C2", "bybit_close"] *= 100.0
    market = af.build_wide_market(bundle)
    mask = af.causal_liquid_mask(market, min_daily_vol_usd=1e6, lookback_days=10, require_bybit=True)
    assert mask["C0"].iloc[-1]
    assert mask["C1"].iloc[-1]
    assert not mask["C2"].any()



def test_cross_venue_sim_charges_round_trip_when_cex_venue_switches():
    hours = np.arange(4, dtype="int64") * 3_600_000
    bundle = pd.DataFrame(
        {
            "hour_ms": hours,
            "coin": "C0",
            "hl_close": 100.0,
            "bybit_close": 100.0,
            "binance_close": 100.0,
            "okx_close": 100.0,
        }
    )
    market = af.build_wide_market(bundle)
    w = pd.DataFrame({"C0": [0.5, 0.5, 0.5, 0.0]}, index=hours)
    same = pd.DataFrame({"C0": ["bybit"] * 4}, index=hours, dtype=object)
    moved = pd.DataFrame({"C0": ["bybit", "okx", "okx", "okx"]}, index=hours, dtype=object)

    sim_same = af.simulate_cross_venue(market, w, venue_choice=same, one_way_cost_bps=4.0)
    sim_moved = af.simulate_cross_venue(market, w, venue_choice=moved, one_way_cost_bps=4.0)
    # Same venue: open 0.5 and close 0.5 on both legs.
    assert sim_same["cost_pnl"].sum() == pytest.approx(-(1.0 * 4.0 / 1e4) * 0.5)
    # Moving the CEX hedge at hour 1 closes 0.5 on Bybit and opens 0.5 on OKX (CEX half of cost only).
    extra = -(0.5 * 1.0 * 4.0 / 1e4) * 0.5
    assert sim_moved["cost_pnl"].sum() == pytest.approx(sim_same["cost_pnl"].sum() + extra)
    assert sim_moved["cex_turnover"].iloc[1] == pytest.approx(1.0)


def test_evaluate_hypothesis_costs_default_to_maker_assumptions_and_scale_with_turnover():
    market = af.build_wide_market(_synthetic_bundle())
    dev_s, hold_s, folds = af.make_walk_forward_splits(len(market.hours), train_days=30, fold_days=15, holdout_days=30)
    spec = {s.name: s for s in af.default_hypothesis_registry()}["xs_rev_L24_r12"]
    kw = dict(dev_start_idx=dev_s, holdout_start_idx=hold_s, fold_boundaries=folds)
    base = af.evaluate_hypothesis(market, spec, **kw)
    same = af.evaluate_hypothesis(market, spec, costs=af.ExecutionCosts(), **kw)
    real = af.evaluate_hypothesis(market, spec, costs=af.ExecutionCosts(hl_bps=5.0, hl_stress_bps=7.0, cross_venue_bps=7.0, cross_venue_stress_bps=9.0), **kw)
    pd.testing.assert_series_equal(base.hourly_returns, same.hourly_returns)
    turnover_cost_gap = (base.hourly_returns - real.hourly_returns).sum()
    assert turnover_cost_gap > 0  # 3.5 bps more per unit of HL turnover
    assert real.dev_metrics["apr"] < base.dev_metrics["apr"]


def test_slow_reversal_registry_only_slows_trading():
    default = {s.name for s in af.default_hypothesis_registry()}
    slow = af.slow_reversal_registry()
    assert len(slow) == 10 and not ({s.name for s in slow} & default)
    assert all(s.family == "hl_xs_price_reversal_mom" and s.params["sign"] == -1.0 for s in slow)
    market = af.build_wide_market(_synthetic_bundle())
    fast = af.build_xs_price_weights(market, {"lookback_h": 24, "rebalance_h": 12, "top_n": 2, "sign": -1.0, "min_vol_usd": 1e6})
    smooth = af.build_xs_price_weights(market, {"lookback_h": 24, "rebalance_h": 12, "top_n": 2, "sign": -1.0, "min_vol_usd": 1e6, "ema_alpha": 0.5})
    assert smooth.diff().abs().sum().sum() < fast.diff().abs().sum().sum()
