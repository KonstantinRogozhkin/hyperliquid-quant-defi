from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hl_quant import funding_arb as fa

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def _bars(n: int, prices=None) -> pd.DataFrame:
    px = np.full(n, 100.0) if prices is None else np.asarray(prices, dtype=float)
    return pd.DataFrame({"open_time_ms": T0 + np.arange(n) * H, "close": px})


def _funding(times, rates) -> pd.DataFrame:
    return pd.DataFrame({"time_ms": np.asarray(times, dtype="int64"), "funding_rate": np.asarray(rates, dtype=float)})


def _hl_funding(n: int, rate) -> pd.DataFrame:
    rates = np.full(n, rate) if np.isscalar(rate) else np.asarray(rate, dtype=float)
    # HL stamps events a few ms after the hour.
    return _funding(T0 + (np.arange(n) + 1) * H + 11, rates)


def _cex_funding(n: int, rate: float, every: int = 8) -> pd.DataFrame:
    hours = np.arange(every, n + 1, every)
    return _funding(T0 + hours * H + 3, np.full(len(hours), rate))


def _panel(n=200, hl_rate=1e-4, cex_rate=0.0, hl_px=None, cex_px=None):
    return fa.build_panel(_hl_funding(n, hl_rate), _cex_funding(n, cex_rate), _bars(n, hl_px), _bars(n, cex_px))


# The CEX rate is unknown until its first event (hour 8); the signal then needs
# `lookback` known hours, each known one hour after settlement.
NO_COST = fa.Costs(hl_fee_bps=0, cex_fee_bps=0, slippage_bps_per_leg=0)
P = fa.Params(lookback_h=8, entry_apr=0.05, exit_apr=0.025)
WARM = 8 + P.lookback_h


def test_positive_hl_funding_shorts_hl_and_earns_it():
    sim = fa.simulate(_panel(), P, NO_COST)
    assert (sim["position"].iloc[:WARM] == 0).all()
    held = sim["position"].iloc[WARM:-1]
    assert (held == 1).all()
    # Each held hour earns the HL hourly rate on half the capital (two legs, 1x).
    assert sim["funding_pnl"].iloc[WARM:-1].to_numpy() == pytest.approx(np.full(len(held), 1e-4 / 2))
    assert sim["basis_pnl"].abs().sum() == 0


def test_negative_spread_goes_long_hl_and_still_earns():
    sim = fa.simulate(_panel(hl_rate=-1e-4), P, NO_COST)
    assert (sim["position"].iloc[WARM:-1] == -1).all()
    assert sim["funding_pnl"].sum() > 0


def test_cex_funding_is_paid_on_the_long_leg():
    # HL pays more per hour than CEX, but CEX funding is a cost when long CEX.
    sim = fa.simulate(_panel(hl_rate=2e-4, cex_rate=8e-4), P, NO_COST)
    # Hourly equivalent: 2e-4 - 8e-4/8 = 1e-4 > 0 -> short HL / long CEX.
    assert (sim["position"].iloc[20:-1] == 1).all()
    cex_hours = sim.index[(sim.index - T0) % (8 * H) == 7 * H][3:-1]
    assert sim.loc[cex_hours, "funding_pnl"].to_numpy() == pytest.approx((2e-4 - 8e-4) / 2)


def test_basis_divergence_hits_short_hl_leg():
    n = 60
    hl_px = np.full(n, 100.0)
    hl_px[40:] = 101.0  # HL rallies 1% relative to CEX while we are short HL
    sim = fa.simulate(_panel(n=n, hl_px=hl_px), P, NO_COST)
    assert sim.loc[T0 + 39 * H, "position"] == 1
    assert sim.loc[T0 + 39 * H, "basis_pnl"] == pytest.approx(-0.01 / 2)


def test_costs_charge_both_legs_on_entry_and_exit():
    costs = fa.Costs(hl_fee_bps=4.5, cex_fee_bps=5.0, slippage_bps_per_leg=1.0)
    sim = fa.simulate(_panel(), P, costs)
    unit = (4.5 + 5.0 + 2.0) / 1e4 / 2
    assert sim["cost_pnl"].sum() == pytest.approx(-2 * unit)  # one entry, one forced exit


def test_flip_costs_two_units():
    rates = np.r_[np.full(60, 1e-4), np.full(60, -1e-4)]
    sim = fa.simulate(_panel(n=120, hl_rate=rates), fa.Params(1, 0.05, 0.025), NO_COST)
    pos = sim["position"].to_numpy()
    flips = np.where(np.abs(np.diff(pos)) == 2)[0]
    assert len(flips) == 1


def test_no_look_ahead_in_positions():
    n = 120
    base = np.full(n, 1e-4)
    changed = base.copy()
    m = 70
    changed[m:] = -5e-4  # events settling from hour m+1 onwards change
    a = fa.simulate(_panel(n=n, hl_rate=base), P, NO_COST)
    b = fa.simulate(_panel(n=n, hl_rate=changed), P, NO_COST)
    settle_idx = m + 1  # event index m settles at hour m+1
    # Position at h_k uses events settled at <= h_{k-1}: unchanged through k = settle_idx.
    assert (a["position"].iloc[: settle_idx + 1].to_numpy() == b["position"].iloc[: settle_idx + 1].to_numpy()).all()
    assert (a["position"].iloc[settle_idx + 2 :] != b["position"].iloc[settle_idx + 2 :]).any()


def test_hysteresis_holds_between_exit_and_entry():
    exp = np.array([0.12, 0.08, 0.06, 0.04, 0.08])
    assert fa.positions(exp, fa.Params(1, 0.10, 0.05)).tolist() == [1, 1, 1, 0, 0]


def test_evaluate_choice_ignores_test_period():
    n = 600
    rng = np.random.default_rng(7)
    rates = 1e-4 + rng.normal(0, 5e-5, n)
    panels_a = {"X": _panel(n=n, hl_rate=rates)}
    tail = rates.copy()
    tail[int(n * 0.8) :] = -1e-3  # very different data late in the test part
    panels_b = {"X": _panel(n=n, hl_rate=tail)}
    grid = [fa.Params(8, 0.05, 0.025), fa.Params(24, 0.20, 0.10), fa.Params(72, 0.40, 0.20)]
    ea = fa.evaluate(panels_a, grid=grid, train_fraction=0.6)
    eb = fa.evaluate(panels_b, grid=grid, train_fraction=0.6)
    assert ea.chosen == eb.chosen
    assert ea.chosen_train == eb.chosen_train
    assert ea.chosen_test != eb.chosen_test


def test_metrics_drawdown_and_portfolio_alignment():
    sim = pd.DataFrame(
        {"ret": [0.01, -0.02, 0.005], "position": [1, 1, 0], "funding_pnl": [0.0] * 3, "basis_pnl": [0.0] * 3, "cost_pnl": [0.0] * 3},
        index=[0, H, 2 * H],
    )
    m = fa.metrics(sim)
    assert m["max_drawdown"] == pytest.approx(-0.02)
    assert m["total_return"] == pytest.approx(-0.005)
    pooled = fa.portfolio({"a": sim, "b": sim.iloc[1:]})
    assert list(pooled.index) == [H, 2 * H]
