"""Monte Carlo execution-friction and margin-buffer stress tests for the HL metamodel.

Research only. Stress-tests frozen metamodel sleeves under realistic maker execution risks:
* Partial / missed limit fills that cross the spread at taker fees + adverse selection;
* Random slippage shocks;
* 1-hour unhedged leg delay on missed cross-venue legs;
* Single-leg worst-case adverse excursion for 1x / 2x / 3x margin buffer sizing.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

import numpy as np
import pandas as pd

from hl_quant import alpha_families as af


@dataclass(frozen=True, slots=True)
class StressScenario:
    name: str
    miss_prob: float  # probability a limit order misses and crosses as taker
    taker_extra_bps: float  # extra fee + adverse selection when a limit order misses
    random_slip_max_bps: float  # uniform [0, max] extra bps on every trade
    unhedged_leg_delay_prob: float  # probability a cross-venue entry suffers 1h unhedged leg risk


DEFAULT_SCENARIOS: tuple[StressScenario, ...] = (
    StressScenario("base_maker", miss_prob=0.0, taker_extra_bps=0.0, random_slip_max_bps=0.0, unhedged_leg_delay_prob=0.0),
    StressScenario("mild_l2_friction", miss_prob=0.15, taker_extra_bps=4.0, random_slip_max_bps=2.0, unhedged_leg_delay_prob=0.05),
    StressScenario("harsh_limit_misses", miss_prob=0.30, taker_extra_bps=7.0, random_slip_max_bps=4.0, unhedged_leg_delay_prob=0.15),
    StressScenario("extreme_stress", miss_prob=0.50, taker_extra_bps=12.0, random_slip_max_bps=6.0, unhedged_leg_delay_prob=0.25),
)


@dataclass(frozen=True, slots=True)
class StressSummary:
    scenario: str
    paths: int
    median_apr: float
    p05_apr: float
    p95_apr: float
    median_sharpe: float
    p05_sharpe: float
    prob_positive_apr: float
    median_max_dd: float
    p05_max_dd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_execution_monte_carlo(
    base_returns: pd.Series,
    turnover: pd.Series,
    leg_vol: pd.Series,
    *,
    scenarios: tuple[StressScenario, ...] = DEFAULT_SCENARIOS,
    n_paths: int = 200,
    seed: int = 42,
    delay_exposure: pd.Series | None = None,
) -> list[StressSummary]:
    """Run vectorized Monte Carlo execution-friction paths over a return & turnover stream.

    ``turnover`` is traded notional in units of capital; miss / slippage bps apply to it.
    ``delay_exposure`` is the cross-venue leg notional (units of capital) that a 1h hedge delay
    leaves unhedged; single-venue trades have none. Defaults to ``turnover`` for callers that
    only pass one stream.
    """
    rng = np.random.default_rng(seed)
    r_base = base_returns.to_numpy(dtype=float)
    dw = turnover.reindex(base_returns.index).fillna(0.0).to_numpy(dtype=float)
    exposure_src = turnover if delay_exposure is None else delay_exposure
    dx = exposure_src.reindex(base_returns.index).fillna(0.0).to_numpy(dtype=float)
    lv = leg_vol.reindex(base_returns.index).fillna(0.005).to_numpy(dtype=float)
    n = len(r_base)
    results: list[StressSummary] = []

    for sc in scenarios:
        if sc.miss_prob == 0.0 and sc.random_slip_max_bps == 0.0 and sc.unhedged_leg_delay_prob == 0.0:
            m = af.series_metrics(base_returns)
            results.append(
                StressSummary(
                    scenario=sc.name,
                    paths=1,
                    median_apr=m["apr"],
                    p05_apr=m["apr"],
                    p95_apr=m["apr"],
                    median_sharpe=m["sharpe"],
                    p05_sharpe=m["sharpe"],
                    prob_positive_apr=1.0 if m["apr"] > 0 else 0.0,
                    median_max_dd=m["max_drawdown"],
                    p05_max_dd=m["max_drawdown"],
                )
            )
            continue

        # Broadcast (n_paths, n)
        trade_mask = dw > 1e-8
        missed = (rng.random((n_paths, n)) < sc.miss_prob) & trade_mask
        rand_slip = rng.uniform(0.0, sc.random_slip_max_bps, size=(n_paths, n)) * trade_mask
        extra_cost = -(dw[None, :] * ((missed * sc.taker_extra_bps + rand_slip) / 1e4))

        # Unhedged 1h leg delay shock: adverse move proportional to realized 1h leg volatility
        delayed = (rng.random((n_paths, n)) < sc.unhedged_leg_delay_prob) & (dx > 1e-8)
        leg_shock = -np.abs(rng.normal(0.0, 1.0, size=(n_paths, n))) * (dx[None, :] * lv[None, :]) * delayed

        paths_r = r_base[None, :] + extra_cost + leg_shock
        aprs = paths_r.mean(axis=1) * af.HOURS_PER_YEAR
        stds = paths_r.std(axis=1, ddof=1)
        sharpes = np.where(stds > 0, paths_r.mean(axis=1) / stds * math.sqrt(af.HOURS_PER_YEAR), 0.0)
        eq = np.cumsum(paths_r, axis=1)
        peak = np.maximum.accumulate(np.hstack([np.zeros((n_paths, 1)), eq]), axis=1)[:, 1:]
        max_dds = (eq - peak).min(axis=1)

        results.append(
            StressSummary(
                scenario=sc.name,
                paths=n_paths,
                median_apr=float(np.median(aprs)),
                p05_apr=float(np.percentile(aprs, 5)),
                p95_apr=float(np.percentile(aprs, 95)),
                median_sharpe=float(np.median(sharpes)),
                p05_sharpe=float(np.percentile(sharpes, 5)),
                prob_positive_apr=float(np.mean(aprs > 0)),
                median_max_dd=float(np.median(max_dds)),
                p05_max_dd=float(np.percentile(max_dds, 5)),
            )
        )
    return results


def margin_buffer_audit(
    hl_ret_next: pd.DataFrame,
    hl_weights: pd.DataFrame,
    *,
    leverages: tuple[float, ...] = (1.0, 2.0, 3.0),
) -> list[dict[str, float]]:
    """Audit worst-case single-leg HL drawdown (for cross-exchange collateral transfer sizing)."""
    # Single-leg HL mark-to-market return without the CEX hedge (worst case for margin on HL alone)
    w = hl_weights.reindex_like(hl_ret_next).fillna(0.0)
    hl_only_ret = (w * hl_ret_next).sum(axis=1)
    # 24h and 72h worst single-leg cumulative drop (before collateral can be rebalanced)
    w24 = float(hl_only_ret.rolling(24).sum().min())
    w72 = float(hl_only_ret.rolling(72).sum().min())
    eq = hl_only_ret.cumsum()
    max_single_leg_dd = float((eq - eq.cummax()).min())

    rows = []
    for lev in leverages:
        rows.append(
            {
                "leverage": lev,
                "worst_24h_single_leg_pct": w24 * lev * 100.0,
                "worst_72h_single_leg_pct": w72 * lev * 100.0,
                "max_single_leg_dd_pct": max_single_leg_dd * lev * 100.0,
                "recommended_free_margin_pct": max(15.0, abs(w72 * lev * 100.0) * 2.5),
            }
        )
    return rows


def metamodel_stream(
    market: af.WideMarket,
    sleeve_weights: dict[str, float],
    *,
    cross_venue_cost_bps: float = 3.5,
    hl_cost_bps: float = 1.5,
    leg_vol_window_h: int = 24,
) -> tuple[pd.Series, pd.Series, pd.Series, pd.Series]:
    """Hourly inputs for :func:`run_execution_monte_carlo` from frozen metamodel sleeve weights.

    Returns ``(returns, capital_turnover, leg_vol, delay_exposure)`` over ``market.hours``:
    * ``returns``: sleeve-weighted base-cost hourly returns (same simulators as the alpha factory);
    * ``capital_turnover``: sleeve-weighted traded notional in units of capital, the base the
      friction bps apply to. A cross-venue change ``dw`` trades ``dw * C / 2`` on each of two legs
      (capital ``C = 2 x`` leg notional), so it counts as ``(hl_turnover + cex_turnover) / 2``;
      a single-venue change ``dw`` counts as ``dw``;
    * ``leg_vol``: cross-sectional median of the trailing ``leg_vol_window_h`` std of HL 1h returns
      (known before the hour), the scale of a 1h unhedged-leg shock;
    * ``delay_exposure``: notional of one cross-venue leg traded (``cex_turnover / 2``, units of
      capital) that a delayed hedge leaves unhedged; zero for single-venue sleeves.
    """
    registry = {s.name: s for s in af.default_hypothesis_registry()}
    ret = pd.Series(0.0, index=market.hours)
    turnover = pd.Series(0.0, index=market.hours)
    delay_exposure = pd.Series(0.0, index=market.hours)
    for name, weight in sleeve_weights.items():
        spec = registry[name]
        if spec.family == "funding_carry_arb":
            w_df, venue_choice = af.build_funding_carry_weights(market, spec.params)
            sim = af.simulate_cross_venue(market, w_df, venue_choice=venue_choice, one_way_cost_bps=cross_venue_cost_bps)
            cex_turn = sim.get("cex_turnover", sim["turnover"])
            capital_turn = 0.5 * (sim["turnover"] + cex_turn)
            leg_exposure = 0.5 * cex_turn
        elif spec.family == "basis_mean_reversion":
            sim = af.simulate_cross_venue(market, af.build_basis_mr_weights(market, spec.params), one_way_cost_bps=cross_venue_cost_bps)
            capital_turn = 0.5 * (sim["turnover"] + sim["cex_turnover"])
            leg_exposure = 0.5 * sim["cex_turnover"]
        elif spec.family in ("hl_xs_funding_contrarian", "hl_xs_price_reversal_mom", "hl_xs_velocity_div"):
            builder = {
                "hl_xs_funding_contrarian": af.build_xs_funding_contrarian_weights,
                "hl_xs_price_reversal_mom": af.build_xs_price_weights,
                "hl_xs_velocity_div": af.build_xs_velocity_divergence_weights,
            }[spec.family]
            sim = af.simulate_hl_single_venue(market, builder(market, spec.params), one_way_cost_bps=hl_cost_bps)
            capital_turn = sim["turnover"]
            leg_exposure = pd.Series(0.0, index=market.hours)
        else:
            raise ValueError(f"unsupported sleeve family for stress stream: {spec.family!r}")
        ret = ret + weight * sim["ret"]
        turnover = turnover + weight * capital_turn
        delay_exposure = delay_exposure + weight * leg_exposure
    leg_vol = market.hl_ret_next.shift(1).rolling(leg_vol_window_h, min_periods=6).std().median(axis=1)
    return ret, turnover, pd.Series(leg_vol.to_numpy(dtype=float), index=market.hours), delay_exposure
