"""Rolling walk-forward and active-slot portfolio allocation for HL funding arbitrage.

Research only. At every rebalance boundary ``t_k``:
* Liquidity filtering, coin selection, and parameter choice read strictly from
  ``[t_k - train_hours, t_k)``.
* The chosen coins and parameters are frozen for the out-of-sample test fold
  ``[t_k, t_k + test_hours)``.
* Positions are simulated continuously across fold boundaries so exiting a
  dropped coin or entering a new coin pays full trading costs.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from hl_quant import funding_arb as fa
from hl_quant import universe_selection as us

AllocationMode = Literal["equal_weight", "active_slots"]


@dataclass(frozen=True, slots=True)
class FoldResult:
    fold_index: int
    train_start_ms: int
    test_start_ms: int
    test_end_ms: int
    eligible_count: int
    selected_coins: list[str]
    chosen_params: str
    train_apr: float
    train_sharpe: float
    test_apr: float
    test_sharpe: float
    test_max_drawdown: float
    test_funding_pnl: float
    test_cost_pnl: float


@dataclass(frozen=True, slots=True)
class WalkForwardEvaluation:
    allocation: AllocationMode
    max_active_slots: int
    max_weight_per_coin: float
    folds: list[FoldResult]
    oos_hourly: pd.DataFrame
    oos_metrics: dict[str, float]
    per_coin_oos: dict[str, dict[str, float]]

    def to_dict(self) -> dict:
        return {
            "allocation": self.allocation,
            "max_active_slots": self.max_active_slots,
            "max_weight_per_coin": self.max_weight_per_coin,
            "fold_count": len(self.folds),
            "oos_metrics": self.oos_metrics,
            "folds": [asdict(f) for f in self.folds],
            "per_coin_oos": self.per_coin_oos,
        }


def simulate_portfolio_weights(
    panels: dict[str, pd.DataFrame],
    weights_by_coin: pd.DataFrame,
    costs: fa.Costs,
) -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    """Simulate a multi-coin portfolio given target signed weights ``w_{t,c}`` per hour.

    ``weights_by_coin`` is indexed by ``hour_ms`` with columns = coins, where
    ``w_{t,c}`` is the signed notional weight on coin ``c`` held over ``(t, t+1]``
    (``sum_c |w_{t,c}| <= 1.0`` at 1x leverage; total capital = ``2 / leverage``).
    Turnover costs are charged at ``t`` on ``|w_{t,c} - w_{t-1,c}|`` for each coin.
    """
    hours = weights_by_coin.index
    n = len(hours)
    scale = costs.leverage / 2.0
    total_fund = np.zeros(n, dtype=float)
    total_basis = np.zeros(n, dtype=float)
    total_cost = np.zeros(n, dtype=float)
    total_gross = np.zeros(n, dtype=float)
    total_turnover = np.zeros(n, dtype=float)
    per_coin: dict[str, pd.DataFrame] = {}

    for coin in weights_by_coin.columns:
        panel = panels[coin].reindex(hours)
        w = weights_by_coin[coin].to_numpy(dtype=float, copy=True)
        w = np.where(np.isfinite(w), w, 0.0)
        if n > 0:
            w[-1] = 0.0  # flatten final unpriced hour
        prev = np.concatenate([[0.0], w[:-1]])
        dw = np.abs(w - prev)

        hl_f = panel["hl_fund_next"].fillna(0.0).to_numpy(dtype=float)
        cex_f = panel["cex_fund_next"].fillna(0.0).to_numpy(dtype=float)
        next_hl = panel["hl_ret"].shift(-1).fillna(0.0).to_numpy(dtype=float)
        next_cex = panel["cex_ret"].shift(-1).fillna(0.0).to_numpy(dtype=float)

        fund = w * (hl_f - cex_f) * scale
        basis = w * (next_cex - next_hl) * scale
        cost = -dw * costs.round_unit * scale
        ret = fund + basis + cost

        total_fund += fund
        total_basis += basis
        total_cost += cost
        total_gross += np.abs(w)
        total_turnover += dw

        per_coin[coin] = pd.DataFrame(
            {
                "position": w,
                "funding_pnl": fund,
                "basis_pnl": basis,
                "cost_pnl": cost,
                "ret": ret,
            },
            index=hours,
        )

    out = pd.DataFrame(
        {
            "position": total_gross,
            "turnover": total_turnover,
            "funding_pnl": total_fund,
            "basis_pnl": total_basis,
            "cost_pnl": total_cost,
            "ret": total_fund + total_basis + total_cost,
        },
        index=hours,
    )
    return out, per_coin


def _expected_apr_series(panel: pd.DataFrame, lookback_h: int) -> pd.Series:
    known = panel["spread_h"].shift(1)
    return known.rolling(lookback_h, min_periods=lookback_h).mean() * fa.HOURS_PER_YEAR


def compute_fold_weights(
    panels: dict[str, pd.DataFrame],
    selected_coins: list[str],
    params: fa.Params,
    fold_hours: pd.Index,
    *,
    allocation: AllocationMode = "equal_weight",
    max_active_slots: int = 5,
    max_weight_per_coin: float = 0.25,
    prev_signals: dict[str, int] | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """Compute signed per-coin weights for ``fold_hours`` using only past signals.

    * ``equal_weight``: each selected coin runs hysteresis independently with weight ``1 / len(selected_coins)``.
    * ``active_slots``: each selected coin runs hysteresis independently; among currently
      active coins at hour ``t``, at most ``max_active_slots`` with the largest
      ``|expected_apr|`` are held (with stickiness for already-held coins so we do not
      churn on tiny rank changes), each receiving ``min(max_weight_per_coin, 1 / n_active)``.
    """
    n = len(fold_hours)
    if n == 0 or not selected_coins:
        return pd.DataFrame(index=fold_hours, dtype=float), {}

    exp_map: dict[str, np.ndarray] = {}
    sig_map: dict[str, np.ndarray] = {}
    end_signals: dict[str, int] = {}

    for coin in selected_coins:
        panel = panels[coin]
        # Compute rolling signal on full history up to fold_hours[-1] (strictly backward-looking)
        upto = panel[panel.index <= int(fold_hours[-1])]
        exp_full = _expected_apr_series(upto, params.lookback_h)
        exp_fold = exp_full.reindex(fold_hours).to_numpy(dtype=float)
        exp_map[coin] = exp_fold

        prev = int((prev_signals or {}).get(coin, 0))
        sig = np.zeros(n, dtype="int8")
        for i, e in enumerate(exp_fold):
            if not math.isfinite(e):
                cur = 0
            else:
                sign = 1 if e > 0 else -1
                if prev != 0 and sign == prev and abs(e) >= params.exit_apr:
                    cur = prev
                elif abs(e) >= params.entry_apr:
                    cur = sign
                else:
                    cur = 0
            sig[i] = cur
            prev = cur
        sig_map[coin] = sig
        end_signals[coin] = int(sig[-1]) if n > 0 else 0

    weights = np.zeros((n, len(selected_coins)), dtype=float)
    if allocation == "equal_weight":
        w_unit = 1.0 / len(selected_coins)
        for j, coin in enumerate(selected_coins):
            weights[:, j] = sig_map[coin].astype(float) * w_unit
        return pd.DataFrame(weights, index=fold_hours, columns=selected_coins), end_signals

    # active_slots: concentrate capital into currently active coins, capped by max_active_slots & max_weight_per_coin
    held_slots: set[int] = set()
    slots = max(1, min(max_active_slots, len(selected_coins)))
    for i in range(n):
        active_idxs = [j for j, c in enumerate(selected_coins) if sig_map[c][i] != 0]
        if not active_idxs:
            held_slots = set()
            continue
        # Keep still-active held coins first (rank stickiness prevents churn); fill remaining slots by |expected_apr|
        kept = [j for j in active_idxs if j in held_slots]
        if len(kept) > slots:
            kept.sort(key=lambda j: -abs(exp_map[selected_coins[j]][i]))
            chosen = kept[:slots]
        else:
            free = [j for j in active_idxs if j not in held_slots]
            free.sort(key=lambda j: -abs(exp_map[selected_coins[j]][i]))
            chosen = kept + free[: slots - len(kept)]
        held_slots = set(chosen)
        w_each = min(max_weight_per_coin, 1.0 / len(chosen))
        for j in chosen:
            weights[i, j] = float(sig_map[selected_coins[j]][i]) * w_each

    return pd.DataFrame(weights, index=fold_hours, columns=selected_coins), end_signals


def _select_params_on_train(
    panels: dict[str, pd.DataFrame],
    selected_coins: list[str],
    train_hours: pd.Index,
    *,
    grid: tuple[fa.Params, ...],
    costs: fa.Costs,
    allocation: AllocationMode,
    max_active_slots: int,
    max_weight_per_coin: float,
) -> tuple[fa.Params, dict[str, float]]:
    best_params = grid[0]
    best_metrics: dict[str, float] | None = None
    best_key = (float("inf"), float("inf"))

    for params in grid:
        w_df, _ = compute_fold_weights(
            panels,
            selected_coins,
            params,
            train_hours,
            allocation=allocation,
            max_active_slots=max_active_slots,
            max_weight_per_coin=max_weight_per_coin,
        )
        sim, _ = simulate_portfolio_weights(panels, w_df, costs)
        m = fa.metrics(sim)
        turnover = float(sim["turnover"].sum())
        key = (-m["sharpe"], turnover)
        if key < best_key:
            best_key = key
            best_params = params
            best_metrics = m
    assert best_metrics is not None
    return best_params, best_metrics


def evaluate_walk_forward(
    panels: dict[str, pd.DataFrame],
    *,
    costs: fa.Costs = fa.Costs(hl_fee_bps=1.5, cex_fee_bps=2.0, slippage_bps_per_leg=0.0),
    grid: Iterable[fa.Params] = fa.DEFAULT_GRID,
    train_days: int = 60,
    test_days: int = 14,
    top_k: int = 10,
    min_train_daily_vol_usd: float = 2_000_000.0,
    min_train_coverage: float = 0.80,
    min_train_tim: float = 0.05,
    rank_by: us.RankMetric = "best_sharpe",
    allocation: AllocationMode = "active_slots",
    max_active_slots: int = 5,
    max_weight_per_coin: float = 0.25,
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> WalkForwardEvaluation:
    """Run rolling walk-forward coin & parameter selection across the full window."""
    if not panels:
        raise ValueError("panels must not be empty")
    grid = tuple(grid)
    train_h = train_days * 24
    test_h = test_days * 24
    if train_h < 2 * max(p.lookback_h for p in grid):
        raise ValueError("train_days too short for grid lookbacks")

    start_ms = int(window_start_ms) if window_start_ms is not None else min(int(p.index.min()) for p in panels.values())
    end_ms = int(window_end_ms) if window_end_ms is not None else max(int(p.index.max()) for p in panels.values())
    all_hours = pd.Index(np.arange(start_ms, end_ms + 1, fa.HOUR_MS, dtype="int64"), name="hour_ms")

    if len(all_hours) < train_h + test_h:
        raise ValueError("not enough history for even one walk-forward fold")

    fold_frames: list[pd.DataFrame] = []
    fold_meta: list[tuple[int, int, int, int, int, list[str], fa.Params, dict[str, float]]] = []
    prev_signals: dict[str, int] = {}

    fold_idx = 0
    cursor = train_h
    while cursor < len(all_hours):
        test_end_idx = min(cursor + test_h, len(all_hours))
        if test_end_idx - cursor < 24:
            break
        train_hours = all_hours[cursor - train_h : cursor]
        test_hours = all_hours[cursor:test_end_idx]
        train_start = int(train_hours[0])
        test_start = int(test_hours[0])
        test_end = int(test_hours[-1])

        # Slice panels strictly to [train_start, test_start) for liquidity & ranking
        train_only_panels: dict[str, pd.DataFrame] = {}
        eligible: list[str] = []
        for coin, panel in sorted(panels.items()):
            tr = panel[(panel.index >= train_start) & (panel.index < test_start)]
            if tr.empty:
                continue
            liq = us.train_liquidity_stats(
                coin,
                tr,
                split_hour_ms=test_start,
                expected_train_hours=len(train_hours),
                min_train_daily_vol_usd=min_train_daily_vol_usd,
                min_train_coverage=min_train_coverage,
            )
            if liq.eligible:
                eligible.append(coin)
                train_only_panels[coin] = tr

        if not eligible:
            fold_frames.append(pd.DataFrame(index=test_hours, dtype=float))
            prev_signals = {}
            cursor = test_end_idx
            fold_idx += 1
            continue

        ranking = us.rank_coins_on_train(
            train_only_panels,
            split_hour_ms=test_start,
            costs=costs,
            grid=grid,
            rank_by=rank_by,
            min_train_tim=min_train_tim,
        )
        pos_ranked = ranking[ranking["rank_score"] > 0]
        picked_df = pos_ranked if not pos_ranked.empty else ranking
        selected = picked_df["coin"].head(top_k).tolist()

        chosen_params, train_m = _select_params_on_train(
            train_only_panels,
            selected,
            train_hours,
            grid=grid,
            costs=costs,
            allocation=allocation,
            max_active_slots=max_active_slots,
            max_weight_per_coin=max_weight_per_coin,
        )

        # Compute OOS weights for this fold (carry forward signals only for coins kept from previous fold)
        fold_w, end_sig = compute_fold_weights(
            panels,
            selected,
            chosen_params,
            test_hours,
            allocation=allocation,
            max_active_slots=max_active_slots,
            max_weight_per_coin=max_weight_per_coin,
            prev_signals={c: prev_signals.get(c, 0) for c in selected},
        )
        fold_frames.append(fold_w)
        fold_meta.append((fold_idx, train_start, test_start, test_end, len(eligible), selected, chosen_params, train_m))
        prev_signals = end_sig
        cursor = test_end_idx
        fold_idx += 1

    if not fold_frames:
        raise ValueError("no walk-forward folds produced")

    # Concatenate all OOS fold weight matrices into one continuous hourly matrix
    # (coins dropped between folds go to 0.0 and pay real exit costs on the boundary!)
    oos_weights = pd.concat(fold_frames, axis=0).fillna(0.0)
    oos_hourly, per_coin_frames = simulate_portfolio_weights(panels, oos_weights, costs)
    oos_metrics = fa.metrics(oos_hourly)
    oos_metrics["total_turnover"] = float(oos_hourly["turnover"].sum())

    fold_results: list[FoldResult] = []
    for f_idx, tr_s, te_s, te_e, elig_n, sel, params, tr_m in fold_meta:
        sub = oos_hourly[(oos_hourly.index >= te_s) & (oos_hourly.index <= te_e)]
        te_m = fa.metrics(sub)
        fold_results.append(
            FoldResult(
                fold_index=f_idx,
                train_start_ms=tr_s,
                test_start_ms=te_s,
                test_end_ms=te_e,
                eligible_count=elig_n,
                selected_coins=sel,
                chosen_params=params.label(),
                train_apr=tr_m["apr"],
                train_sharpe=tr_m["sharpe"],
                test_apr=te_m["apr"],
                test_sharpe=te_m["sharpe"],
                test_max_drawdown=te_m["max_drawdown"],
                test_funding_pnl=te_m["funding_pnl"],
                test_cost_pnl=te_m["cost_pnl"],
            )
        )

    per_coin_oos = {coin: fa.metrics(df) for coin, df in sorted(per_coin_frames.items())}
    return WalkForwardEvaluation(
        allocation=allocation,
        max_active_slots=max_active_slots,
        max_weight_per_coin=max_weight_per_coin,
        folds=fold_results,
        oos_hourly=oos_hourly,
        oos_metrics=oos_metrics,
        per_coin_oos=per_coin_oos,
    )
