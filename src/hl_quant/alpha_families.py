"""Six orthogonal Hyperliquid alpha families with strictly causal walk-forward evaluation.

Research only; no orders placed. Every family obeys the same timing contract:
* Decisions at hour ``h_t`` use ONLY data known at or before ``h_{t-1}`` (``shift(1)``).
* Returns accrue over ``(h_t, h_{t+1}]`` from actual next-hour price returns and funding.
* Turnover costs are charged at ``h_t`` on ``|w_{t,c} - w_{t-1,c}|``.
* Cross-venue delta-neutral families collateralise two legs (``capital = 2 * notional``
  at 1x leverage, so ``scale = 0.5``).
* Single-venue HL market-neutral dollar-neutral families collateralise one account
  (``sum(|w|) <= 1.0`` at 1x leverage, so ``scale = 1.0``, ``sum(w) == 0``).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import pandas as pd

from hl_quant import funding_arb as fa

HOURS_PER_YEAR = 8760.0
DAY_MS = 24 * fa.HOUR_MS


@dataclass(frozen=True, slots=True)
class HypothesisSpec:
    name: str
    family: str
    venue_mode: str  # "cross_venue" or "hl_single_venue"
    params: dict[str, Any]


@dataclass(frozen=True, slots=True)
class HypothesisEval:
    spec: HypothesisSpec
    dev_metrics: dict[str, float]
    holdout_metrics: dict[str, float]
    dev_stressed_metrics: dict[str, float]
    holdout_stressed_metrics: dict[str, float]
    dev_fold_aprs: list[float]
    hourly_returns: pd.Series  # full OOS (dev + holdout), base maker costs
    stressed_returns: pd.Series  # full OOS (dev + holdout), stressed costs

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "name": self.spec.name,
            "family": self.spec.family,
            "venue_mode": self.spec.venue_mode,
            "params": self.spec.params,
            "dev_metrics": self.dev_metrics,
            "holdout_metrics": self.holdout_metrics,
            "dev_stressed_metrics": self.dev_stressed_metrics,
            "holdout_stressed_metrics": self.holdout_stressed_metrics,
            "dev_fold_aprs": self.dev_fold_aprs,
            "dev_pos_fold_ratio": float(np.mean([x > 0 for x in self.dev_fold_aprs])) if self.dev_fold_aprs else 0.0,
        }


# --------------------------------------------------------------------------- pivot helpers


@dataclass(slots=True)
class WideMarket:
    """Aligned ``(T x N)`` hourly matrices for fast vectorized causal simulation."""

    hours: np.ndarray  # int64 (T,)
    coins: list[str]  # length N
    hl_close: pd.DataFrame
    hl_ret_next: pd.DataFrame  # return over (h_t, h_{t+1}]
    hl_fund_next: pd.DataFrame  # funding accrued over (h_t, h_{t+1}]
    hl_rate_h: pd.DataFrame  # hourly rate known at h_t (shift(1) before use!)
    hl_notional: pd.DataFrame
    bybit_close: pd.DataFrame
    bybit_ret_next: pd.DataFrame
    bybit_fund_next: pd.DataFrame
    bybit_rate_h: pd.DataFrame
    bybit_notional: pd.DataFrame
    binance_close: pd.DataFrame
    binance_ret_next: pd.DataFrame
    binance_fund_next: pd.DataFrame
    binance_rate_h: pd.DataFrame
    binance_notional: pd.DataFrame
    okx_close: pd.DataFrame
    okx_ret_next: pd.DataFrame
    okx_fund_next: pd.DataFrame
    okx_rate_h: pd.DataFrame
    okx_notional: pd.DataFrame


def build_wide_market(bundle: pd.DataFrame) -> WideMarket:
    """Pivot long ``dataset_bundle.parquet`` into aligned ``(T x N)`` matrices."""
    hours = np.sort(bundle["hour_ms"].unique().astype("int64"))
    coins = sorted(bundle["coin"].unique().tolist())

    def piv(col: str) -> pd.DataFrame:
        if col not in bundle.columns:
            return pd.DataFrame(np.nan, index=hours, columns=coins)
        return bundle.pivot(index="hour_ms", columns="coin", values=col).reindex(index=hours, columns=coins).astype(float)

    hl_close = piv("hl_close")
    by_close = piv("bybit_close")
    bn_close = piv("binance_close")
    okx_close = piv("okx_close")

    # Close of bar opening at h_t is price at h_{t+1}; return over (h_t, h_{t+1}] is close_t / close_{t-1} - 1.
    hl_ret_next = (hl_close / hl_close.shift(1) - 1.0).fillna(0.0)
    by_ret_next = (by_close / by_close.shift(1) - 1.0).fillna(0.0)
    bn_ret_next = (bn_close / bn_close.shift(1) - 1.0).fillna(0.0)
    okx_ret_next = (okx_close / okx_close.shift(1) - 1.0).fillna(0.0)

    return WideMarket(
        hours=hours,
        coins=coins,
        hl_close=hl_close,
        hl_ret_next=hl_ret_next,
        hl_fund_next=piv("hl_fund_next").fillna(0.0),
        hl_rate_h=piv("hl_rate_h"),
        hl_notional=piv("hl_notional_usd"),
        bybit_close=by_close,
        bybit_ret_next=by_ret_next,
        bybit_fund_next=piv("bybit_fund_next").fillna(0.0),
        bybit_rate_h=piv("bybit_rate_h"),
        bybit_notional=piv("bybit_notional_usd"),
        binance_close=bn_close,
        binance_ret_next=bn_ret_next,
        binance_fund_next=piv("binance_fund_next").fillna(0.0),
        binance_rate_h=piv("binance_rate_h"),
        binance_notional=piv("binance_notional_usd"),
        okx_close=okx_close,
        okx_ret_next=okx_ret_next,
        okx_fund_next=piv("okx_fund_next").fillna(0.0),
        okx_rate_h=piv("okx_rate_h"),
        okx_notional=piv("okx_notional_usd"),
    )


def causal_liquid_mask(
    market: WideMarket,
    *,
    min_daily_vol_usd: float = 2_000_000.0,
    lookback_days: int = 30,
    require_bybit: bool = True,
    max_price_ratio_log_diff: float = 0.05,
) -> pd.DataFrame:
    """Strictly causal rolling liquidity & cross-venue price-parity mask (uses only ``shift(1)``)."""
    win = lookback_days * 24
    hl_daily = market.hl_notional.shift(1).rolling(win, min_periods=max(24, win // 2)).mean() * 24.0
    ok = (hl_daily >= min_daily_vol_usd) & market.hl_close.shift(1).notna()
    if require_bybit:
        by_daily = market.bybit_notional.shift(1).rolling(win, min_periods=max(24, win // 2)).mean() * 24.0
        # Reject mismatched-asset/scale pairs (e.g. HL PURR vs unrelated Bybit PURRUSDT) strictly causally
        log_ratio = np.log((market.hl_close.shift(1) / market.bybit_close.shift(1)).clip(lower=1e-9))
        med_abs_diff = log_ratio.abs().rolling(72, min_periods=12).median()
        parity_ok = (med_abs_diff <= max_price_ratio_log_diff) & (log_ratio.abs() <= 0.15)
        ok = ok & (by_daily >= min_daily_vol_usd) & market.bybit_close.shift(1).notna() & parity_ok
    return ok.fillna(False)


# --------------------------------------------------------------------------- simulation engine


def simulate_cross_venue(
    market: WideMarket,
    weights: pd.DataFrame,
    *,
    venue_choice: pd.DataFrame | None = None,
    one_way_cost_bps: float = 3.5,
) -> pd.DataFrame:
    """Simulate cross-venue delta-neutral weights (``w > 0`` = short HL / long CEX).

    ``capital = 2 * notional`` at 1x leverage -> ``scale = 0.5``.
    ``venue_choice`` (optional): boolean DataFrame where True = Binance, False = Bybit, or a
    string DataFrame of ``"bybit" | "binance" | "okx"``.

    ``one_way_cost_bps`` covers both legs of one unit of turnover. The HL leg pays it on
    ``|w_t - w_{t-1}|``; the CEX leg pays it on the same amount while the venue is unchanged,
    but on ``|w_t| + |w_{t-1}|`` when the hedge moves to another venue (old leg closed, new
    leg opened). Each leg is charged half of ``one_way_cost_bps``.
    """
    w = weights.reindex(index=market.hours, columns=market.coins).fillna(0.0).to_numpy(dtype=float, copy=True)
    if len(w) == 0:
        return pd.DataFrame(
            columns=["position", "turnover", "cex_turnover", "funding_pnl", "basis_pnl", "cost_pnl", "ret"],
            index=market.hours,
            dtype=float,
        )
    w[-1, :] = 0.0
    prev = np.vstack([np.zeros((1, w.shape[1])), w[:-1, :]])
    dw = np.abs(w - prev)

    hl_f = market.hl_fund_next.to_numpy(dtype=float)
    hl_r = market.hl_ret_next.to_numpy(dtype=float)
    by_f = market.bybit_fund_next.to_numpy(dtype=float)
    by_r = market.bybit_ret_next.to_numpy(dtype=float)
    dw_cex = dw

    if venue_choice is not None and len(market.coins) > 0:
        vc = venue_choice.reindex(index=market.hours, columns=market.coins)
        bn_f = market.binance_fund_next.to_numpy(dtype=float)
        bn_r = market.binance_ret_next.to_numpy(dtype=float)
        okx_f = market.okx_fund_next.to_numpy(dtype=float)
        okx_r = market.okx_ret_next.to_numpy(dtype=float)
        if vc.dtypes.iloc[0] == bool or str(vc.dtypes.iloc[0]) == "bool":
            use_bn = vc.fillna(False).to_numpy(dtype=bool)
            cex_f = np.where(use_bn, bn_f, by_f)
            cex_r = np.where(use_bn, bn_r, by_r)
            vc_arr = np.where(use_bn, "binance", "bybit").astype(object)
        else:
            vc_arr = vc.fillna("bybit").to_numpy(dtype=object)
            use_bn = vc_arr == "binance"
            use_okx = vc_arr == "okx"
            cex_f = np.where(use_okx, okx_f, np.where(use_bn, bn_f, by_f))
            cex_r = np.where(use_okx, okx_r, np.where(use_bn, bn_r, by_r))
        prev_vc = np.vstack([vc_arr[:1, :], vc_arr[:-1, :]])
        switched = vc_arr != prev_vc
        dw_cex = np.where(switched, np.abs(w) + np.abs(prev), dw)
    else:
        cex_f = by_f
        cex_r = by_r

    scale = 0.5
    fund = (w * (hl_f - cex_f)).sum(axis=1) * scale
    basis = (w * (cex_r - hl_r)).sum(axis=1) * scale
    cost = -(0.5 * (dw + dw_cex).sum(axis=1) * (one_way_cost_bps / 1e4)) * scale
    ret = fund + basis + cost
    return pd.DataFrame(
        {
            "position": np.abs(w).sum(axis=1),
            "turnover": dw.sum(axis=1),
            "cex_turnover": dw_cex.sum(axis=1),
            "funding_pnl": fund,
            "basis_pnl": basis,
            "cost_pnl": cost,
            "ret": ret,
        },
        index=market.hours,
    )


def simulate_hl_single_venue(
    market: WideMarket,
    weights: pd.DataFrame,
    *,
    one_way_cost_bps: float = 1.5,
) -> pd.DataFrame:
    """Simulate single-venue HL market-neutral weights (``w > 0`` = long HL, ``w < 0`` = short HL).

    Long pays HL funding (`-w * hl_fund_next`); short earns HL funding.
    Capital at 1x gross leverage = ``sum(|w|) <= 1.0`` -> ``scale = 1.0``.
    """
    w = weights.reindex(index=market.hours, columns=market.coins).fillna(0.0).to_numpy(dtype=float, copy=True)
    if len(w) == 0:
        return pd.DataFrame(
            columns=["position", "turnover", "price_pnl", "funding_pnl", "cost_pnl", "ret"],
            index=market.hours,
            dtype=float,
        )
    w[-1, :] = 0.0
    prev = np.vstack([np.zeros((1, w.shape[1])), w[:-1, :]])
    dw = np.abs(w - prev)

    hl_f = market.hl_fund_next.to_numpy(dtype=float)
    hl_r = market.hl_ret_next.to_numpy(dtype=float)

    price_pnl = (w * hl_r).sum(axis=1)
    fund_pnl = (-w * hl_f).sum(axis=1)
    cost_pnl = -(dw.sum(axis=1) * (one_way_cost_bps / 1e4))
    ret = price_pnl + fund_pnl + cost_pnl
    return pd.DataFrame(
        {
            "position": np.abs(w).sum(axis=1),
            "turnover": dw.sum(axis=1),
            "funding_pnl": fund_pnl,
            "basis_pnl": price_pnl,
            "cost_pnl": cost_pnl,
            "ret": ret,
        },
        index=market.hours,
    )


def series_metrics(ret: pd.Series, sim_df: pd.DataFrame | None = None) -> dict[str, float]:
    r = ret.to_numpy(dtype=float)
    n = len(r)
    if n == 0:
        return {"hours": 0, "apr": 0.0, "sharpe": 0.0, "max_drawdown": 0.0}
    eq = np.cumsum(r)
    dd = eq - np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:]
    std = float(r.std(ddof=1)) if n > 1 else 0.0
    out = {
        "hours": int(n),
        "total_return": float(eq[-1]),
        "apr": float(r.mean() * HOURS_PER_YEAR),
        "vol_annual": float(std * math.sqrt(HOURS_PER_YEAR)),
        "sharpe": float(r.mean() / std * math.sqrt(HOURS_PER_YEAR)) if std > 0 else 0.0,
        "max_drawdown": float(dd.min()),
    }
    if sim_df is not None:
        sub = sim_df.loc[ret.index]
        out["avg_gross"] = float(sub["position"].mean())
        out["total_turnover"] = float(sub["turnover"].sum())
        out["funding_pnl"] = float(sub["funding_pnl"].sum())
        out["price_or_basis_pnl"] = float(sub["basis_pnl"].sum())
        out["cost_pnl"] = float(sub["cost_pnl"].sum())
    return out


# --------------------------------------------------------------------------- Family 1: Funding Carry Arb


def _active_slots_from_signal(
    score_df: pd.DataFrame,
    mask_df: pd.DataFrame,
    *,
    entry_thr: float,
    exit_thr: float,
    max_slots: int = 5,
    max_weight: float = 0.25,
    basis_filter_df: pd.DataFrame | None = None,
    min_basis_z: float | None = None,
) -> pd.DataFrame:
    """Hysteresis + sticky top-M active-slot allocation (strictly causal)."""
    scores = score_df.to_numpy(dtype=float)
    mask = mask_df.to_numpy(dtype=bool)
    basis_z = basis_filter_df.to_numpy(dtype=float) if basis_filter_df is not None else None
    t_len, n_coins = scores.shape

    sig = np.zeros((t_len, n_coins), dtype=np.int8)
    prev = np.zeros(n_coins, dtype=np.int8)
    for i in range(t_len):
        s_row = scores[i]
        m_row = mask[i]
        b_row = basis_z[i] if basis_z is not None else None
        cur = np.zeros(n_coins, dtype=np.int8)
        for j in range(n_coins):
            e = s_row[j]
            if not m_row[j] or not math.isfinite(e):
                cur[j] = 0
                continue
            sgn = 1 if e > 0 else -1
            if prev[j] != 0 and sgn == prev[j] and abs(e) >= exit_thr:
                cur[j] = prev[j]
            elif abs(e) >= entry_thr:
                if b_row is not None and min_basis_z is not None and math.isfinite(b_row[j]):
                    # Only enter if basis is not strongly against our direction (sgn * basis_z >= min_basis_z)
                    if sgn * b_row[j] < min_basis_z:
                        cur[j] = 0
                        continue
                cur[j] = sgn
            else:
                cur[j] = 0
        sig[i] = cur
        prev = cur

    weights = np.zeros((t_len, n_coins), dtype=float)
    held: set[int] = set()
    for i in range(t_len):
        active = [j for j in range(n_coins) if sig[i, j] != 0]
        if not active:
            held = set()
            continue
        kept = [j for j in active if j in held]
        if len(kept) > max_slots:
            kept.sort(key=lambda j: -abs(scores[i, j]))
            chosen = kept[:max_slots]
        else:
            free = [j for j in active if j not in held]
            free.sort(key=lambda j: -abs(scores[i, j]))
            chosen = kept + free[: max_slots - len(kept)]
        held = set(chosen)
        w_each = min(max_weight, 1.0 / len(chosen))
        for j in chosen:
            weights[i, j] = float(sig[i, j]) * w_each
    return pd.DataFrame(weights, index=score_df.index, columns=score_df.columns)


def build_funding_carry_weights(market: WideMarket, params: dict[str, Any]) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    lb = int(params["lookback_h"])
    entry_apr = float(params["entry_apr"])
    exit_apr = float(params.get("exit_apr", entry_apr / 2.0))
    max_slots = int(params.get("max_slots", 5))
    max_weight = float(params.get("max_weight", 0.25))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    mode = str(params.get("mode", "bybit"))  # "bybit" or "best_cex"
    min_basis_z = params.get("min_basis_z")

    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=True)
    by_spread = (market.hl_rate_h - market.bybit_rate_h).shift(1).rolling(lb, min_periods=lb).mean() * HOURS_PER_YEAR
    venue_choice: pd.DataFrame | None = None

    if mode == "best_cex":
        bn_spread = (market.hl_rate_h - market.binance_rate_h).shift(1).rolling(lb, min_periods=lb).mean() * HOURS_PER_YEAR
        okx_spread = (market.hl_rate_h - market.okx_rate_h).shift(1).rolling(lb, min_periods=lb).mean() * HOURS_PER_YEAR
        hl_px = market.hl_close.shift(1)
        bn_px = market.binance_close.ffill(limit=2).shift(1)
        okx_px = market.okx_close.ffill(limit=2).shift(1)
        bn_parity = np.log((hl_px / bn_px).clip(lower=1e-9)).abs() <= 0.10
        okx_parity = np.log((hl_px / okx_px).clip(lower=1e-9)).abs() <= 0.10
        bn_ok = bn_px.notna() & bn_spread.notna() & bn_parity
        okx_ok = okx_px.notna() & okx_spread.notna() & okx_parity
        vol_win = 30 * 24
        vol_min_p = max(24, vol_win // 2)
        if market.binance_notional.notna().any().any():
            bn_daily = market.binance_notional.shift(1).rolling(vol_win, min_periods=vol_min_p).mean() * 24.0
            bn_daily = bn_daily.fillna(market.binance_notional.shift(1).expanding(min_periods=1).mean() * 24.0)
            bn_ok = bn_ok & (bn_daily >= min_vol)
        if market.okx_notional.notna().any().any():
            okx_daily = market.okx_notional.shift(1).rolling(vol_win, min_periods=vol_min_p).mean() * 24.0
            okx_daily = okx_daily.fillna(market.okx_notional.shift(1).expanding(min_periods=1).mean() * 24.0)
            okx_ok = okx_ok & (okx_daily >= min_vol)

        score = by_spread.copy()
        choice = pd.DataFrame("bybit", index=by_spread.index, columns=by_spread.columns, dtype=object)

        use_bn = bn_ok & (bn_spread.abs() > score.abs().fillna(-1.0))
        score = score.where(~use_bn, bn_spread)
        choice = choice.where(~use_bn, "binance")

        use_okx = okx_ok & (okx_spread.abs() > score.abs().fillna(-1.0))
        score = score.where(~use_okx, okx_spread)
        choice = choice.where(~use_okx, "okx")
        venue_choice = choice
    else:
        score = by_spread

    basis_z_df = None
    if min_basis_z is not None:
        raw_basis = np.log(market.hl_close.shift(1) / market.bybit_close.shift(1))
        b_mean = raw_basis.rolling(72, min_periods=24).mean()
        b_std = raw_basis.rolling(72, min_periods=24).std().replace(0.0, np.nan)
        basis_z_df = (raw_basis - b_mean) / b_std

    w = _active_slots_from_signal(
        score,
        mask,
        entry_thr=entry_apr,
        exit_thr=exit_apr,
        max_slots=max_slots,
        max_weight=max_weight,
        basis_filter_df=basis_z_df,
        min_basis_z=float(min_basis_z) if min_basis_z is not None else None,
    )
    return w, venue_choice


# --------------------------------------------------------------------------- Family 2: Basis Mean-Reversion


def build_basis_mr_weights(market: WideMarket, params: dict[str, Any]) -> pd.DataFrame:
    lb = int(params["lookback_h"])
    z_in = float(params["z_in"])
    z_out = float(params.get("z_out", 0.25))
    max_slots = int(params.get("max_slots", 5))
    max_weight = float(params.get("max_weight", 0.25))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    funding_aligned = bool(params.get("funding_aligned", False))

    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=True)
    # Strictly causal: basis known at h_t comes from close at h_{t-1}
    basis = np.log(market.hl_close.shift(1) / market.bybit_close.shift(1))
    mu = basis.rolling(lb, min_periods=max(12, lb // 2)).mean()
    sd = basis.rolling(lb, min_periods=max(12, lb // 2)).std().replace(0.0, np.nan)
    z = (basis - mu) / sd

    if funding_aligned:
        # Suppress entries where trailing 24h funding spread is strongly opposite to basis fade direction
        sp24 = (market.hl_rate_h - market.bybit_rate_h).shift(1).rolling(24, min_periods=12).mean() * HOURS_PER_YEAR
        adverse = (z > 0) & (sp24 < -0.15) | (z < 0) & (sp24 > 0.15)
        z = z.mask(adverse, 0.0)

    return _active_slots_from_signal(
        z,
        mask,
        entry_thr=z_in,
        exit_thr=z_out,
        max_slots=max_slots,
        max_weight=max_weight,
    )


# --------------------------------------------------------------------------- Families 3-5: Cross-Sectional HL


def cross_sectional_weights(
    score_df: pd.DataFrame,
    mask_df: pd.DataFrame,
    *,
    top_n: int = 5,
    rebalance_h: int = 8,
    ema_alpha: float = 1.0,
    vol_df: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Dollar-neutral top-N long / bottom-N short weights on HL (``sum(|w|) = 1.0``, ``sum(w) = 0``)."""
    scores = score_df.to_numpy(dtype=float)
    mask = mask_df.to_numpy(dtype=bool)
    vols = vol_df.to_numpy(dtype=float) if vol_df is not None else None
    t_len, n_coins = scores.shape
    weights = np.zeros((t_len, n_coins), dtype=float)
    cur_w = np.zeros(n_coins, dtype=float)

    idx_vals = score_df.index.to_numpy()
    use_epoch_grid = (
        rebalance_h > 1
        and t_len > 0
        and np.issubdtype(idx_vals.dtype, np.number)
        and float(np.min(idx_vals)) >= 100_000_000_000.0
    )

    for i in range(t_len):
        if rebalance_h <= 1:
            should_rebal = True
        elif use_epoch_grid:
            is_canonical = ((int(idx_vals[i]) // fa.HOUR_MS) % rebalance_h) == 0
            # Seed i == 0 only for non-EMA series before the first canonical UTC boundary; once any
            # canonical boundary hits, cur_w is 100% invariant to sliding-window start_ms shifts.
            should_rebal = is_canonical or (i == 0 and ema_alpha >= 1.0)
        else:
            should_rebal = (i % rebalance_h) == 0

        if should_rebal:
            row = scores[i]
            valid = [j for j in range(n_coins) if mask[i, j] and math.isfinite(row[j])]
            target = np.zeros(n_coins, dtype=float)
            k = min(top_n, len(valid) // 2)
            if k >= 1:
                valid.sort(key=lambda j: row[j])
                shorts = valid[:k]
                longs = valid[-k:]
                if vols is not None:
                    inv_l = np.array([1.0 / max(vols[i, j], 1e-4) if math.isfinite(vols[i, j]) else 1.0 for j in longs])
                    inv_s = np.array([1.0 / max(vols[i, j], 1e-4) if math.isfinite(vols[i, j]) else 1.0 for j in shorts])
                    inv_l = inv_l / inv_l.sum() * 0.5
                    inv_s = inv_s / inv_s.sum() * 0.5
                    for idx_pos, j in enumerate(longs):
                        target[j] = inv_l[idx_pos]
                    for idx_pos, j in enumerate(shorts):
                        target[j] = -inv_s[idx_pos]
                else:
                    for j in longs:
                        target[j] = 0.5 / k
                    for j in shorts:
                        target[j] = -0.5 / k
            if ema_alpha < 1.0:
                cur_w = ema_alpha * target + (1.0 - ema_alpha) * cur_w
                target_gross = float(np.abs(target).sum())
                gross = float(np.abs(cur_w).sum())
                if target_gross <= 1e-9:
                    # Target is flat: let positions decay cleanly to zero without re-inflating gross to 1.0.
                    cur_w = np.where(np.abs(cur_w) < 1e-3, 0.0, cur_w)
                elif gross > 1.0:
                    cur_w = cur_w / gross
            else:
                cur_w = target
        weights[i] = cur_w
    return pd.DataFrame(weights, index=score_df.index, columns=score_df.columns)


def build_xs_funding_contrarian_weights(market: WideMarket, params: dict[str, Any]) -> pd.DataFrame:
    lb = int(params["lookback_h"])
    top_n = int(params.get("top_n", 5))
    reb_h = int(params.get("rebalance_h", 8))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    vol_scale = bool(params.get("vol_scale", True))
    sign = float(params.get("sign", -1.0))  # -1 = short highest funding / long lowest funding (earns carry + unwind!)

    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=False)
    fund_avg = market.hl_rate_h.shift(1).rolling(lb, min_periods=max(4, lb // 2)).mean()
    score = sign * fund_avg
    vol_df = market.hl_close.pct_change(fill_method=None).shift(1).rolling(72, min_periods=24).std() if vol_scale else None
    return cross_sectional_weights(score, mask, top_n=top_n, rebalance_h=reb_h, vol_df=vol_df)


def build_xs_price_weights(market: WideMarket, params: dict[str, Any]) -> pd.DataFrame:
    lb = int(params["lookback_h"])
    top_n = int(params.get("top_n", 5))
    reb_h = int(params.get("rebalance_h", 8))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    sign = float(params["sign"])  # -1 = reversal, +1 = momentum
    blend_funding = float(params.get("blend_funding", 0.0))

    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=False)
    px_known = market.hl_close.shift(1)
    past_ret = np.log(px_known / px_known.shift(lb))
    vol = market.hl_close.pct_change(fill_method=None).shift(1).rolling(72, min_periods=24).std().replace(0.0, np.nan)
    score = sign * (past_ret / (vol * math.sqrt(lb)))

    if blend_funding != 0.0:
        f_rank = (-market.hl_rate_h.shift(1).rolling(24, min_periods=12).mean()).rank(axis=1, pct=True)
        p_rank = score.rank(axis=1, pct=True)
        score = (1.0 - abs(blend_funding)) * p_rank + blend_funding * f_rank

    ema_alpha = float(params.get("ema_alpha", 1.0))
    return cross_sectional_weights(score, mask, top_n=top_n, rebalance_h=reb_h, ema_alpha=ema_alpha, vol_df=vol)


def build_xs_velocity_divergence_weights(market: WideMarket, params: dict[str, Any]) -> pd.DataFrame:
    variant = str(params["variant"])
    top_n = int(params.get("top_n", 5))
    reb_h = int(params.get("rebalance_h", 8))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    sign = float(params.get("sign", -1.0))

    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=(variant == "hl_bybit_div"))
    vol = market.hl_close.pct_change(fill_method=None).shift(1).rolling(72, min_periods=24).std().replace(0.0, np.nan)

    if variant == "funding_vel":
        fast = market.hl_rate_h.shift(1).rolling(12, min_periods=6).mean()
        slow = market.hl_rate_h.shift(1).rolling(168, min_periods=48).mean()
        score = sign * (fast - slow)
    elif variant == "hl_bybit_div":
        sp = (market.hl_rate_h - market.bybit_rate_h).shift(1).rolling(24, min_periods=12).mean()
        score = sign * sp
    elif variant == "vol_shock_funding":
        v_fast = market.hl_notional.shift(1).rolling(12, min_periods=6).mean()
        v_slow = market.hl_notional.shift(1).rolling(168, min_periods=48).mean().replace(0.0, np.nan)
        v_ratio = np.log((v_fast / v_slow).clip(lower=1e-3))
        f24 = market.hl_rate_h.shift(1).rolling(24, min_periods=12).mean()
        score = sign * (f24.rank(axis=1, pct=True) - 0.5) * v_ratio.clip(lower=0.0)
    else:
        raise ValueError(f"unknown variant {variant!r}")

    return cross_sectional_weights(score, mask, top_n=top_n, rebalance_h=reb_h, vol_df=vol)


# --------------------------------------------------------------------------- Family 6: Walk-Forward ML Ranker


def build_ml_feature_panel(market: WideMarket) -> dict[str, pd.DataFrame]:
    """Build strictly causal ``(T x N)`` feature matrices (all inputs ``shift(1)``)."""
    px = market.hl_close.shift(1)
    r1 = px.pct_change(fill_method=None)
    vol24 = r1.rolling(24, min_periods=12).std().replace(0.0, np.nan)
    vol168 = r1.rolling(168, min_periods=48).std().replace(0.0, np.nan)
    hl_r = market.hl_rate_h.shift(1)
    by_r = market.bybit_rate_h.shift(1)
    basis = np.log(px / market.bybit_close.shift(1))
    b_z = (basis - basis.rolling(72, min_periods=24).mean()) / basis.rolling(72, min_periods=24).std().replace(0.0, np.nan)
    v_fast = market.hl_notional.shift(1).rolling(12, min_periods=6).mean()
    v_slow = market.hl_notional.shift(1).rolling(168, min_periods=48).mean().replace(0.0, np.nan)

    raw_feats = {
        "ret_4h": np.log(px / px.shift(4)) / (vol24 * 2.0),
        "ret_12h": np.log(px / px.shift(12)) / (vol24 * math.sqrt(12)),
        "ret_24h": np.log(px / px.shift(24)) / (vol24 * math.sqrt(24)),
        "ret_72h": np.log(px / px.shift(72)) / (vol168 * math.sqrt(72)),
        "ret_168h": np.log(px / px.shift(168)) / (vol168 * math.sqrt(168)),
        "vol_ratio": np.log((vol24 / vol168).clip(lower=1e-3)),
        "fund_8h": hl_r.rolling(8, min_periods=4).mean(),
        "fund_24h": hl_r.rolling(24, min_periods=12).mean(),
        "fund_168h": hl_r.rolling(168, min_periods=48).mean(),
        "fund_vel": hl_r.rolling(24, min_periods=12).mean() - hl_r.rolling(168, min_periods=48).mean(),
        "hl_bybit_sp24": (hl_r - by_r).rolling(24, min_periods=12).mean(),
        "hl_bybit_sp168": (hl_r - by_r).rolling(168, min_periods=48).mean(),
        "basis_z72": b_z,
        "notional_ratio": np.log((v_fast / v_slow).clip(lower=1e-3)),
    }
    # Cross-sectional rank-normalize each feature to [-0.5, +0.5] per hour (robust to crypto outliers!)
    return {k: df.rank(axis=1, pct=True) - 0.5 for k, df in raw_feats.items()}


def build_ml_walkforward_weights(
    market: WideMarket,
    params: dict[str, Any],
    *,
    fold_boundaries: list[tuple[int, int, int]],  # (train_start_idx, test_start_idx, test_end_idx)
    feature_cache: dict[str, pd.DataFrame] | None = None,
) -> pd.DataFrame:
    """Walk-forward Ridge / HistGradientBoosting cross-sectional alpha predictor (strictly purged!)."""
    horizon_h = int(params.get("horizon_h", 24))
    model_kind = str(params.get("model", "ridge"))
    top_n = int(params.get("top_n", 5))
    reb_h = int(params.get("rebalance_h", 12))
    min_vol = float(params.get("min_vol_usd", 2_000_000.0))
    alpha = float(params.get("alpha", 100.0))

    feats = feature_cache if feature_cache is not None else build_ml_feature_panel(market)
    feat_names = sorted(feats.keys())
    mask = causal_liquid_mask(market, min_daily_vol_usd=min_vol, require_bybit=False)
    vol = market.hl_close.pct_change(fill_method=None).shift(1).rolling(72, min_periods=24).std().replace(0.0, np.nan)

    # Forward total return (price + funding) over (h_t, h_{t+horizon_h}]
    # Close of bar opening at h_t is price at h_{t+1}, so price at h_t is hl_close.shift(1)
    # and price at h_{t+horizon_h} is hl_close.shift(-(horizon_h - 1)).
    # Note: we purge the last `horizon_h` hours of every train window so the target NEVER touches test!
    fwd_px = np.log(market.hl_close.shift(-(horizon_h - 1)) / market.hl_close.shift(1))
    fwd_fund = -market.hl_fund_next.rolling(horizon_h).sum().shift(-(horizon_h - 1))
    fwd_total = fwd_px + fwd_fund
    # Cross-sectional demeaned target
    target_df = fwd_total.sub(fwd_total.mean(axis=1), axis=0)

    # Stack feature cube (T, N, F)
    cube = np.stack([feats[k].to_numpy(dtype=float) for k in feat_names], axis=-1)
    y_mat = target_df.to_numpy(dtype=float)
    m_mat = mask.to_numpy(dtype=bool)
    t_len, n_coins = y_mat.shape

    pred_scores = np.full((t_len, n_coins), np.nan, dtype=float)

    for tr_s, te_s, te_e in fold_boundaries:
        # Strict purge: drop last `horizon_h` hours before `te_s`!
        tr_e_purged = te_s - horizon_h
        if tr_e_purged <= tr_s + 24:
            continue
        # Subsample every 4 hours on train to reduce serial correlation & speed up fitting
        tr_slice = slice(tr_s, tr_e_purged, 4)
        x_tr = cube[tr_slice].reshape(-1, len(feat_names))
        y_tr = y_mat[tr_slice].reshape(-1)
        m_tr = m_mat[tr_slice].reshape(-1)
        valid_tr = m_tr & np.isfinite(y_tr) & np.all(np.isfinite(x_tr), axis=1)
        if int(valid_tr.sum()) < 200:
            continue
        X_fit = x_tr[valid_tr]
        y_fit = np.clip(y_tr[valid_tr], -0.25, 0.25)

        x_te = cube[te_s:te_e].reshape(-1, len(feat_names))
        ok_te = np.all(np.isfinite(x_te), axis=1)
        p_te = np.full(len(x_te), np.nan, dtype=float)

        if model_kind == "ridge":
            x_mean = X_fit.mean(axis=0)
            y_mean = float(y_fit.mean())
            Xc = X_fit - x_mean
            yc = y_fit - y_mean
            coef = np.linalg.solve(Xc.T @ Xc + alpha * np.eye(Xc.shape[1]), Xc.T @ yc)
            if ok_te.any():
                p_te[ok_te] = (x_te[ok_te] - x_mean) @ coef + y_mean
        elif model_kind == "hgb":
            from sklearn.ensemble import HistGradientBoostingRegressor

            model = HistGradientBoostingRegressor(
                max_depth=3,
                max_leaf_nodes=7,
                min_samples_leaf=200,
                learning_rate=0.03,
                max_iter=80,
                l2_regularization=10.0,
                random_state=42,
            )
            model.fit(X_fit, y_fit)
            if ok_te.any():
                p_te[ok_te] = model.predict(x_te[ok_te])
        else:
            raise ValueError(f"unknown model_kind {model_kind!r}")
        pred_scores[te_s:te_e] = p_te.reshape(te_e - te_s, n_coins)

    score_df = pd.DataFrame(pred_scores, index=market.hours, columns=market.coins)
    return cross_sectional_weights(score_df, mask, top_n=top_n, rebalance_h=reb_h, vol_df=vol)


# --------------------------------------------------------------------------- Registry & Evaluation


def default_hypothesis_registry() -> list[HypothesisSpec]:
    """Curated, diverse registry of 48 hypotheses across 6 orthogonal families."""
    specs: list[HypothesisSpec] = []

    # Family 1: Cross-venue Funding Carry Arb (10 specs)
    for mode in ("bybit", "best_cex"):
        for lb, entry in ((72, 0.10), (168, 0.05), (168, 0.10), (168, 0.15)):
            specs.append(
                HypothesisSpec(
                    name=f"carry_{mode}_L{lb}_in{int(entry*100)}",
                    family="funding_carry_arb",
                    venue_mode="cross_venue",
                    params={"mode": mode, "lookback_h": lb, "entry_apr": entry, "max_slots": 5, "max_weight": 0.25, "min_vol_usd": 1_000_000.0},
                )
            )
    for min_bz in (0.0, 0.5):
        specs.append(
            HypothesisSpec(
                name=f"carry_bybit_L168_in10_bz{min_bz:.1f}",
                family="funding_carry_arb",
                venue_mode="cross_venue",
                params={"mode": "bybit", "lookback_h": 168, "entry_apr": 0.10, "max_slots": 5, "max_weight": 0.25, "min_vol_usd": 1_000_000.0, "min_basis_z": min_bz},
            )
        )

    # Family 2: Cross-venue Basis Mean-Reversion (8 specs)
    for lb in (24, 72, 168):
        for z_in in (1.75, 2.25):
            specs.append(
                HypothesisSpec(
                    name=f"basis_mr_L{lb}_z{z_in:.2f}",
                    family="basis_mean_reversion",
                    venue_mode="cross_venue",
                    params={"lookback_h": lb, "z_in": z_in, "z_out": 0.25, "max_slots": 5, "max_weight": 0.25, "min_vol_usd": 2_000_000.0, "funding_aligned": False},
                )
            )
    for lb in (24, 72):
        specs.append(
            HypothesisSpec(
                name=f"basis_mr_L{lb}_z2.00_falign",
                family="basis_mean_reversion",
                venue_mode="cross_venue",
                params={"lookback_h": lb, "z_in": 2.0, "z_out": 0.25, "max_slots": 5, "max_weight": 0.25, "min_vol_usd": 2_000_000.0, "funding_aligned": True},
            )
        )

    # Family 3: Single-venue HL Cross-Sectional Funding Contrarian + Carry (9 specs)
    for lb in (8, 24, 72, 168):
        for reb in (8, 24):
            specs.append(
                HypothesisSpec(
                    name=f"xs_fund_contra_L{lb}_r{reb}",
                    family="hl_xs_funding_contrarian",
                    venue_mode="hl_single_venue",
                    params={"lookback_h": lb, "rebalance_h": reb, "top_n": 5, "min_vol_usd": 2_000_000.0, "sign": -1.0, "vol_scale": True},
                )
            )
    specs.append(
        HypothesisSpec(
            name="xs_fund_contra_L24_r12_n8",
            family="hl_xs_funding_contrarian",
            venue_mode="hl_single_venue",
            params={"lookback_h": 24, "rebalance_h": 12, "top_n": 8, "min_vol_usd": 1_000_000.0, "sign": -1.0, "vol_scale": True},
        )
    )

    # Family 4: Single-venue HL Cross-Sectional Price Reversal & Momentum (10 specs)
    for lb, reb in ((4, 4), (12, 8), (24, 12), (48, 24)):
        specs.append(
            HypothesisSpec(
                name=f"xs_rev_L{lb}_r{reb}",
                family="hl_xs_price_reversal_mom",
                venue_mode="hl_single_venue",
                params={"lookback_h": lb, "rebalance_h": reb, "top_n": 5, "min_vol_usd": 2_000_000.0, "sign": -1.0},
            )
        )
    for lb, reb in ((72, 24), (168, 24), (336, 48)):
        specs.append(
            HypothesisSpec(
                name=f"xs_mom_L{lb}_r{reb}",
                family="hl_xs_price_reversal_mom",
                venue_mode="hl_single_venue",
                params={"lookback_h": lb, "rebalance_h": reb, "top_n": 5, "min_vol_usd": 2_000_000.0, "sign": 1.0},
            )
        )
    for lb, bf in ((12, 0.35), (24, 0.35), (168, 0.35)):
        sgn = -1.0 if lb <= 24 else 1.0
        tag = "rev" if sgn < 0 else "mom"
        specs.append(
            HypothesisSpec(
                name=f"xs_{tag}_fundblend_L{lb}",
                family="hl_xs_price_reversal_mom",
                venue_mode="hl_single_venue",
                params={"lookback_h": lb, "rebalance_h": 12, "top_n": 5, "min_vol_usd": 2_000_000.0, "sign": sgn, "blend_funding": bf},
            )
        )

    # Family 5: Funding Velocity & Cross-Venue Divergence on HL (5 specs)
    for variant, sgn, reb in (
        ("funding_vel", -1.0, 8),
        ("funding_vel", 1.0, 8),
        ("hl_bybit_div", -1.0, 12),
        ("hl_bybit_div", 1.0, 12),
        ("vol_shock_funding", -1.0, 12),
    ):
        tag = "contra" if sgn < 0 else "follow"
        specs.append(
            HypothesisSpec(
                name=f"xs_{variant}_{tag}_r{reb}",
                family="hl_xs_velocity_div",
                venue_mode="hl_single_venue",
                params={"variant": variant, "sign": sgn, "rebalance_h": reb, "top_n": 5, "min_vol_usd": 2_000_000.0},
            )
        )

    # Family 6: Walk-Forward ML Cross-Sectional Rankers (6 specs)
    for model_kind in ("ridge", "hgb"):
        for horizon, reb in ((12, 12), (24, 12), (24, 24)):
            specs.append(
                HypothesisSpec(
                    name=f"ml_{model_kind}_h{horizon}_r{reb}",
                    family="ml_walkforward_ranker",
                    venue_mode="hl_single_venue",
                    params={"model": model_kind, "horizon_h": horizon, "rebalance_h": reb, "top_n": 5, "min_vol_usd": 2_000_000.0, "alpha": 100.0},
                )
            )

    return specs


def slow_reversal_registry() -> list[HypothesisSpec]:
    """Pre-registered lower-turnover variants of the HL cross-sectional reversal (2026-11-26).

    At measured HL execution costs (~5 bps per trade) the hourly-rebalanced ``xs_rev_L24_r12`` and
    ``xs_rev_fundblend_L24`` lose fold stability because of turnover. These variants only slow the
    trading (longer rebalance, EMA-smoothed weights, more names) and keep the signal unchanged;
    the set is fixed up front so it is not tuned on the holdout.
    """
    base = {"top_n": 5, "min_vol_usd": 2_000_000.0, "sign": -1.0}
    blend = {**base, "blend_funding": 0.35}
    variants: list[tuple[str, dict[str, Any]]] = [
        ("xs_rev_L24_r24", {**base, "lookback_h": 24, "rebalance_h": 24}),
        ("xs_rev_L24_r48", {**base, "lookback_h": 24, "rebalance_h": 48}),
        ("xs_rev_L48_r48", {**base, "lookback_h": 48, "rebalance_h": 48}),
        ("xs_rev_L72_r48", {**base, "lookback_h": 72, "rebalance_h": 48}),
        ("xs_rev_L24_r12_ema05", {**base, "lookback_h": 24, "rebalance_h": 12, "ema_alpha": 0.5}),
        ("xs_rev_L24_r24_ema05", {**base, "lookback_h": 24, "rebalance_h": 24, "ema_alpha": 0.5}),
        ("xs_rev_L24_r24_n8", {**base, "lookback_h": 24, "rebalance_h": 24, "top_n": 8}),
        ("xs_rev_fundblend_L24_r24", {**blend, "lookback_h": 24, "rebalance_h": 24}),
        ("xs_rev_fundblend_L24_r48", {**blend, "lookback_h": 24, "rebalance_h": 48}),
        ("xs_rev_fundblend_L24_r24_ema05", {**blend, "lookback_h": 24, "rebalance_h": 24, "ema_alpha": 0.5}),
    ]
    return [
        HypothesisSpec(name=name, family="hl_xs_price_reversal_mom", venue_mode="hl_single_venue", params=params)
        for name, params in variants
    ]


def make_walk_forward_splits(
    n_hours: int,
    *,
    train_days: int = 60,
    fold_days: int = 15,
    holdout_days: int = 45,
) -> tuple[int, int, list[tuple[int, int, int]]]:
    """Return ``(dev_start_idx, holdout_start_idx, all_fold_boundaries)``."""
    train_h = train_days * 24
    fold_h = fold_days * 24
    holdout_h = holdout_days * 24
    holdout_start = n_hours - holdout_h
    if holdout_start <= train_h + fold_h:
        raise ValueError("not enough history for dev walk-forward + holdout")

    folds: list[tuple[int, int, int]] = []
    cur = train_h
    while cur < holdout_start:
        te_e = min(cur + fold_h, holdout_start)
        folds.append((cur - train_h, cur, te_e))
        cur = te_e
    # Holdout folds (re-trained strictly on rolling trailing 60d up to each holdout fold start)
    cur = holdout_start
    while cur < n_hours:
        te_e = min(cur + fold_h, n_hours)
        folds.append((cur - train_h, cur, te_e))
        cur = te_e
    return train_h, holdout_start, folds


@dataclass(frozen=True, slots=True)
class ExecutionCosts:
    """One-way costs per unit of turnover used by :func:`evaluate_hypothesis` (bps).

    ``cross_venue_bps`` covers both legs of a cross-venue trade (HL + CEX); ``hl_bps`` is a
    single-venue HL trade. Defaults are the original maker-fee assumptions (HL 1.5 + CEX 2.0).
    """

    cross_venue_bps: float = 3.5
    cross_venue_stress_bps: float = 5.5
    hl_bps: float = 1.5
    hl_stress_bps: float = 3.5


def evaluate_hypothesis(
    market: WideMarket,
    spec: HypothesisSpec,
    *,
    dev_start_idx: int,
    holdout_start_idx: int,
    fold_boundaries: list[tuple[int, int, int]],
    feature_cache: dict[str, pd.DataFrame] | None = None,
    costs: ExecutionCosts | None = None,
) -> HypothesisEval:
    costs = costs or ExecutionCosts()
    venue_choice: pd.DataFrame | None = None
    if spec.family == "funding_carry_arb":
        weights, venue_choice = build_funding_carry_weights(market, spec.params)
    elif spec.family == "basis_mean_reversion":
        weights = build_basis_mr_weights(market, spec.params)
    elif spec.family == "hl_xs_funding_contrarian":
        weights = build_xs_funding_contrarian_weights(market, spec.params)
    elif spec.family == "hl_xs_price_reversal_mom":
        weights = build_xs_price_weights(market, spec.params)
    elif spec.family == "hl_xs_velocity_div":
        weights = build_xs_velocity_divergence_weights(market, spec.params)
    elif spec.family == "ml_walkforward_ranker":
        weights = build_ml_walkforward_weights(market, spec.params, fold_boundaries=fold_boundaries, feature_cache=feature_cache)
    else:
        raise ValueError(f"unknown family {spec.family!r}")

    if spec.venue_mode == "cross_venue":
        sim_base = simulate_cross_venue(market, weights, venue_choice=venue_choice, one_way_cost_bps=costs.cross_venue_bps)
        sim_stress = simulate_cross_venue(market, weights, venue_choice=venue_choice, one_way_cost_bps=costs.cross_venue_stress_bps)
    else:
        sim_base = simulate_hl_single_venue(market, weights, one_way_cost_bps=costs.hl_bps)
        sim_stress = simulate_hl_single_venue(market, weights, one_way_cost_bps=costs.hl_stress_bps)

    dev_idx = market.hours[dev_start_idx:holdout_start_idx]
    hold_idx = market.hours[holdout_start_idx:]
    oos_idx = market.hours[dev_start_idx:]

    dev_fold_aprs: list[float] = []
    for _tr_s, te_s, te_e in fold_boundaries:
        if te_s >= holdout_start_idx:
            break
        f_idx = market.hours[te_s:te_e]
        dev_fold_aprs.append(float(sim_base.loc[f_idx, "ret"].mean() * HOURS_PER_YEAR))

    return HypothesisEval(
        spec=spec,
        dev_metrics=series_metrics(sim_base.loc[dev_idx, "ret"], sim_base),
        holdout_metrics=series_metrics(sim_base.loc[hold_idx, "ret"], sim_base),
        dev_stressed_metrics=series_metrics(sim_stress.loc[dev_idx, "ret"], sim_stress),
        holdout_stressed_metrics=series_metrics(sim_stress.loc[hold_idx, "ret"], sim_stress),
        dev_fold_aprs=dev_fold_aprs,
        hourly_returns=sim_base.loc[oos_idx, "ret"].rename(spec.name),
        stressed_returns=sim_stress.loc[oos_idx, "ret"].rename(spec.name),
    )
