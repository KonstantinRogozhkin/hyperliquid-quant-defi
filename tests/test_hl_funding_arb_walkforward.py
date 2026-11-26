from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from hl_quant import funding_arb as fa
from hl_quant import walk_forward as wf

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def _panel(n: int = 1200, *, hl_rate=2e-4, hourly_vol: float = 5_000.0) -> pd.DataFrame:
    idx = np.arange(n)
    rates = np.full(n, hl_rate, dtype=float) if np.isscalar(hl_rate) else np.asarray(hl_rate, dtype=float)
    bars = pd.DataFrame({"open_time_ms": T0 + idx * H, "close": np.full(n, 100.0), "volume": np.full(n, hourly_vol)})
    hl_f = pd.DataFrame({"time_ms": T0 + (idx + 1) * H + 11, "funding_rate": rates})
    cex_hours = np.arange(8, n + 1, 8)
    cex_f = pd.DataFrame({"time_ms": T0 + cex_hours * H + 3, "funding_rate": np.zeros(len(cex_hours))})
    return fa.build_panel(hl_f, cex_f, bars, bars)


def test_active_slots_respects_max_weight_and_concentrates_capital():
    # Coin A has strong funding, B/C/D are flat -> active_slots should give A max_weight (0.25),
    # whereas equal_weight over 4 coins gives 0.25 too; when 2 coins active, active_slots gives 0.25 each (capped)
    # or 0.50 if max_weight=0.50!
    panels = {
        "A": _panel(n=200, hl_rate=3e-4),
        "B": _panel(n=200, hl_rate=2e-4),
        "C": _panel(n=200, hl_rate=0.0),
        "D": _panel(n=200, hl_rate=0.0),
    }
    hours = panels["A"].index[40:]
    p = fa.Params(lookback_h=8, entry_apr=0.05, exit_apr=0.025)
    w_active, _ = wf.compute_fold_weights(
        panels,
        ["A", "B", "C", "D"],
        p,
        hours,
        allocation="active_slots",
        max_active_slots=2,
        max_weight_per_coin=0.5,
    )
    assert (w_active["A"] == 0.5).all()
    assert (w_active["B"] == 0.5).all()
    assert (w_active["C"] == 0.0).all()
    assert (w_active["D"] == 0.0).all()


def test_walk_forward_fold_zero_lookahead_and_boundary_costs():
    n = 24 * 50  # 50 days: train=20d, test=10d -> 3 OOS folds
    rng = np.random.default_rng(11)
    r_a = 3e-4 + rng.normal(0, 2e-5, n)
    r_b = 1.5e-4 + rng.normal(0, 2e-5, n)
    r_c = 0.0 + rng.normal(0, 1e-5, n)

    panels_a = {"A": _panel(n=n, hl_rate=r_a), "B": _panel(n=n, hl_rate=r_b), "C": _panel(n=n, hl_rate=r_c)}
    # Mutate C only in the LAST fold (days 40..50): fold 0 and fold 1 selections must be identical!
    r_c_mut = r_c.copy()
    r_c_mut[24 * 40 :] = 5e-3
    panels_b = {**panels_a, "C": _panel(n=n, hl_rate=r_c_mut)}

    grid = [fa.Params(8, 0.05, 0.025), fa.Params(24, 0.10, 0.05)]
    costs = fa.Costs(hl_fee_bps=1.5, cex_fee_bps=2.0, slippage_bps_per_leg=0.0)

    ea = wf.evaluate_walk_forward(
        panels_a, costs=costs, grid=grid, train_days=20, test_days=10, top_k=2, allocation="active_slots", max_active_slots=2, max_weight_per_coin=0.5
    )
    eb = wf.evaluate_walk_forward(
        panels_b, costs=costs, grid=grid, train_days=20, test_days=10, top_k=2, allocation="active_slots", max_active_slots=2, max_weight_per_coin=0.5
    )
    assert len(ea.folds) == 3
    assert ea.folds[0].selected_coins == eb.folds[0].selected_coins
    assert ea.folds[1].selected_coins == eb.folds[1].selected_coins
    assert ea.folds[0].chosen_params == eb.folds[0].chosen_params
    assert ea.oos_metrics["cost_pnl"] < 0.0
    assert ea.oos_metrics["funding_pnl"] > 0.0
    assert ea.oos_metrics["apr"] == pytest.approx(eb.oos_metrics["apr"])  # C wasn't selected in fold 2 either
