"""Delta-neutral cross-venue funding arbitrage backtest: Hyperliquid perp vs a CEX perp.

Research only. Equal notional on both legs; position ``s`` per hour:

* ``s = +1``: short the Hyperliquid perp, long the CEX perp (earns HL funding,
  pays CEX funding);
* ``s = -1``: the reverse; ``s = 0``: flat.

Timing contract (hour grid ``h_k`` in UTC):

* ``s_k`` is decided at ``h_k`` from funding events settled at or before
  ``h_{k-1}`` only (an event stamped ``h_k`` arrives milliseconds after the
  hour, so it is not used until the next decision), and is held over
  ``(h_k, h_{k+1}]``;
* funding events settling at ``h_{k+1}`` (provider timestamps are floored to the
  hour) accrue to ``s_k``; the price leg uses the 1h close-to-close returns over
  the same interval;
* trading costs are charged at ``h_k`` on ``|s_k - s_{k-1}|`` for both legs.

PnL is per unit of one-leg notional; capital is ``2 / leverage`` notional
(both legs collateralised), so ``return = pnl * leverage / 2``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import itertools
import math
from typing import Iterable

import numpy as np
import pandas as pd

HOUR_MS = 3_600_000
HOURS_PER_YEAR = 8760.0


@dataclass(frozen=True, slots=True)
class Costs:
    hl_fee_bps: float = 4.5
    cex_fee_bps: float = 5.0
    slippage_bps_per_leg: float = 1.0
    leverage: float = 1.0

    @property
    def round_unit(self) -> float:
        """Cost of changing the position by 1 on both legs, as a fraction of notional."""
        return (self.hl_fee_bps + self.cex_fee_bps + 2 * self.slippage_bps_per_leg) / 1e4


@dataclass(frozen=True, slots=True)
class Params:
    lookback_h: int = 24
    entry_apr: float = 0.10
    exit_apr: float = 0.05

    def label(self) -> str:
        return f"L{self.lookback_h}_in{self.entry_apr:.2f}_out{self.exit_apr:.2f}"


DEFAULT_GRID: tuple[Params, ...] = tuple(
    Params(lookback_h=lb, entry_apr=e, exit_apr=e / 2)
    for lb, e in itertools.product((8, 24, 72, 168), (0.05, 0.10, 0.20, 0.40))
)


# --------------------------------------------------------------------------- panel


def build_panel(
    hl_funding: pd.DataFrame,
    cex_funding: pd.DataFrame,
    hl_bars: pd.DataFrame,
    cex_bars: pd.DataFrame,
) -> pd.DataFrame:
    """Hourly panel indexed by hour start ``h_k`` (ms) for one coin and one CEX.

    Inputs are the normalized collector rows (``time_ms``/``funding_rate`` or
    ``fundingRate``-derived ``funding_rate``; bars with ``open_time_ms``/``close``).
    """
    start = max(int(hl_bars["open_time_ms"].min()), int(cex_bars["open_time_ms"].min()))
    end = min(int(hl_bars["open_time_ms"].max()), int(cex_bars["open_time_ms"].max()))
    if end <= start:
        raise ValueError("price histories do not overlap")
    grid = np.arange(start, end + 1, HOUR_MS, dtype="int64")
    panel = pd.DataFrame(index=pd.Index(grid, name="hour_ms"))

    # Close of the bar opening at h_k is the price at h_{k+1}; the return over
    # (h_k, h_{k+1}] is close_k / close_{k-1} - 1.
    for name, bars in (("hl", hl_bars), ("cex", cex_bars)):
        close = bars.set_index("open_time_ms")["close"].astype(float)
        close = close[~close.index.duplicated(keep="last")].reindex(grid)
        panel[f"{name}_close"] = close
        panel[f"{name}_ret"] = close / close.shift(1) - 1.0
        if "volume" in bars.columns:
            vol = bars.set_index("open_time_ms")["volume"].astype(float)
            vol = vol[~vol.index.duplicated(keep="last")].reindex(grid)
            panel[f"{name}_notional_usd"] = close * vol

    for name, events in (("hl", hl_funding), ("cex", cex_funding)):
        settle = (events["time_ms"].astype("int64") // HOUR_MS) * HOUR_MS
        rates = pd.Series(events["funding_rate"].astype(float).to_numpy(), index=settle.to_numpy())
        rates = rates.groupby(level=0).sum()
        # Event settling at h_{k+1} is paid on the position held over (h_k, h_{k+1}].
        panel[f"{name}_fund_next"] = rates.reindex(grid + HOUR_MS).fillna(0.0).to_numpy()
        # Hourly-equivalent rate known at h_k: last settled rate / its interval length.
        known = rates[rates.index <= grid[-1]]
        diffs_h = np.diff(known.index.to_numpy()) / HOUR_MS if len(known) >= 2 else np.array([], dtype=float)
        pos_diffs = diffs_h[np.isfinite(diffs_h) & (diffs_h > 0)]
        default_interval = float(np.median(pos_diffs)) if len(pos_diffs) >= 1 else (1.0 if name == "hl" else 8.0)
        interval_h = pd.Series(np.diff(known.index.to_numpy(), prepend=np.nan) / HOUR_MS, index=known.index)
        interval_h = interval_h.fillna(default_interval).clip(lower=1.0)
        hourly_equiv = (known / interval_h).reindex(grid, method="ffill", limit=24)
        panel[f"{name}_rate_h"] = hourly_equiv.to_numpy()
        panel[f"{name}_event_now"] = rates.reindex(grid).notna().to_numpy()

    panel["spread_h"] = panel["hl_rate_h"] - panel["cex_rate_h"]
    return panel


# --------------------------------------------------------------------------- simulation


def positions(expected_apr: np.ndarray, params: Params) -> np.ndarray:
    """Hysteresis rule: enter at ``entry_apr``, hold while same sign and above ``exit_apr``."""
    out = np.zeros(len(expected_apr), dtype="int8")
    prev = 0
    for i, e in enumerate(expected_apr):
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
        out[i] = cur
        prev = cur
    return out


def simulate(panel: pd.DataFrame, params: Params, costs: Costs = Costs()) -> pd.DataFrame:
    """Hourly PnL breakdown for one coin (indexed like ``panel``)."""
    known = panel["spread_h"].shift(1)  # see the timing contract in the module docstring
    signal = known.rolling(params.lookback_h, min_periods=params.lookback_h).mean()
    expected_apr = (signal * HOURS_PER_YEAR).to_numpy()
    s = positions(expected_apr, params).astype(float)
    # The last hour has no next price/funding; flatten so nothing is left open unpriced.
    s[-1] = 0.0
    prev = np.concatenate([[0.0], s[:-1]])
    fund = s * (panel["hl_fund_next"].to_numpy() - panel["cex_fund_next"].to_numpy())
    # panel["*_ret"][k] = close_k / close_{k-1} - 1 is already the return over (h_k, h_{k+1}].
    next_hl = panel["hl_ret"].fillna(0.0).to_numpy()
    next_cex = panel["cex_ret"].fillna(0.0).to_numpy()
    basis = s * (next_cex - next_hl)
    cost = -np.abs(s - prev) * costs.round_unit
    pnl = fund + basis + cost
    scale = costs.leverage / 2.0
    return pd.DataFrame(
        {
            "expected_apr": expected_apr,
            "position": s,
            "funding_pnl": fund * scale,
            "basis_pnl": basis * scale,
            "cost_pnl": cost * scale,
            "ret": pnl * scale,
        },
        index=panel.index,
    )


# --------------------------------------------------------------------------- metrics


def metrics(sim: pd.DataFrame) -> dict[str, float]:
    ret = sim["ret"].to_numpy()
    n = len(ret)
    if n == 0:
        return {"hours": 0}
    equity = np.cumsum(ret)
    drawdown = equity - np.maximum.accumulate(np.concatenate([[0.0], equity]))[1:]
    std = ret.std(ddof=1) if n > 1 else 0.0
    changes = np.abs(np.diff(np.concatenate([[0.0], sim["position"].to_numpy()])))
    return {
        "hours": int(n),
        "total_return": float(equity[-1]),
        "apr": float(ret.mean() * HOURS_PER_YEAR),
        "vol_annual": float(std * math.sqrt(HOURS_PER_YEAR)),
        "sharpe": float(ret.mean() / std * math.sqrt(HOURS_PER_YEAR)) if std > 0 else 0.0,
        "max_drawdown": float(drawdown.min()),
        "time_in_market": float((sim["position"] != 0).mean()),
        "position_changes": float(changes.sum()),
        "funding_pnl": float(sim["funding_pnl"].sum()),
        "basis_pnl": float(sim["basis_pnl"].sum()),
        "cost_pnl": float(sim["cost_pnl"].sum()),
    }


# --------------------------------------------------------------------------- evaluation


@dataclass(frozen=True, slots=True)
class Evaluation:
    split_hour_ms: int
    chosen: Params
    train_grid: pd.DataFrame
    test_grid: pd.DataFrame
    chosen_train: dict[str, float]
    chosen_test: dict[str, float]
    per_coin_test: dict[str, dict[str, float]]
    test_hourly: pd.DataFrame

    def to_dict(self) -> dict:
        return {
            "split_hour_ms": self.split_hour_ms,
            "chosen": asdict(self.chosen),
            "chosen_train": self.chosen_train,
            "chosen_test": self.chosen_test,
            "per_coin_test": self.per_coin_test,
        }


def portfolio(sims: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Equal-weight average of per-coin hourly results on their common hours."""
    common = None
    for sim in sims.values():
        common = sim.index if common is None else common.intersection(sim.index)
    frames = [sim.loc[common] for sim in sims.values()]
    out = sum(f[["funding_pnl", "basis_pnl", "cost_pnl", "ret"]] for f in frames) / len(frames)
    out["position"] = sum((f["position"] != 0).astype(float) for f in frames) / len(frames)
    return out


def evaluate(
    panels: dict[str, pd.DataFrame],
    *,
    grid: Iterable[Params] = DEFAULT_GRID,
    costs: Costs = Costs(),
    train_fraction: float = 0.6,
    split_hour_ms: int | None = None,
) -> Evaluation:
    """Pick one parameter set on the pooled train part; report it untouched on the test part.

    Signals are computed on the full history (they only look back), so the test
    part starts with a warm lookback window and no information from the future.
    """
    if not 0.1 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be within [0.1, 0.9]")
    grid = tuple(grid)
    common = None
    for panel in panels.values():
        common = panel.index if common is None else common.intersection(panel.index)
    if common is None or len(common) < 2 * max(p.lookback_h for p in grid):
        raise ValueError("not enough common history for the grid lookbacks")
    split = int(split_hour_ms) if split_hour_ms is not None else int(common[int(len(common) * train_fraction)])

    train_rows, test_rows, sims_by_params = [], [], {}
    for params in grid:
        sims = {coin: simulate(panel.loc[common], params, costs) for coin, panel in panels.items()}
        pooled = portfolio(sims)
        sims_by_params[params] = (sims, pooled)
        train_rows.append({"params": params.label(), **metrics(pooled[pooled.index < split])})
        test_rows.append({"params": params.label(), **metrics(pooled[pooled.index >= split])})
    train_grid = pd.DataFrame(train_rows)
    test_grid = pd.DataFrame(test_rows)

    # Highest train Sharpe; ties go to fewer position changes.
    order = sorted(
        range(len(grid)),
        key=lambda i: (-train_grid.loc[i, "sharpe"], train_grid.loc[i, "position_changes"]),
    )
    chosen = grid[order[0]]
    sims, pooled = sims_by_params[chosen]
    test_mask = pooled.index >= split
    return Evaluation(
        split_hour_ms=split,
        chosen=chosen,
        train_grid=train_grid,
        test_grid=test_grid,
        chosen_train=metrics(pooled[~test_mask]),
        chosen_test=metrics(pooled[test_mask]),
        per_coin_test={coin: metrics(sim[sim.index >= split]) for coin, sim in sims.items()},
        test_hourly=pooled[test_mask],
    )
