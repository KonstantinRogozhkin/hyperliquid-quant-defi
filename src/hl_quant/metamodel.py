"""Strict Dev-only metamodel stacking over orthogonal OOF hypothesis return streams.

Research only. All hypothesis selection, orthogonal pruning, and combiner weight
fitting read strictly from ``hour_ms < holdout_start_ms`` (Dev OOF). The untouched
holdout slice ``hour_ms >= holdout_start_ms`` is evaluated once with frozen weights.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any

import numpy as np
import pandas as pd

from hl_quant.alpha_families import HOURS_PER_YEAR, series_metrics


@dataclass(frozen=True, slots=True)
class MetamodelResult:
    selected_names: list[str]
    selected_families: dict[str, str]
    weights_by_method: dict[str, dict[str, float]]
    dev_metrics: dict[str, dict[str, float]]
    holdout_metrics: dict[str, dict[str, float]]
    holdout_stressed_metrics: dict[str, dict[str, float]]
    corr_matrix_dev: pd.DataFrame
    holdout_hourly: pd.DataFrame

    def to_dict(self) -> dict[str, Any]:
        return {
            "selected_names": self.selected_names,
            "selected_families": self.selected_families,
            "weights_by_method": self.weights_by_method,
            "dev_metrics": self.dev_metrics,
            "holdout_metrics": self.holdout_metrics,
            "holdout_stressed_metrics": self.holdout_stressed_metrics,
        }


def select_orthogonal_hypotheses(
    summaries: list[dict[str, Any]],
    dev_returns: pd.DataFrame,
    *,
    min_dev_sharpe: float = 0.5,
    min_stressed_sharpe: float = 0.0,
    min_pos_fold_ratio: float = 0.60,
    max_corr: float = 0.50,
    max_per_family: int = 2,
    max_total: int = 8,
) -> tuple[list[str], dict[str, str]]:
    """Filter strictly on Dev OOF quality + stress survival, then greedily prune correlated clones."""
    survivors = [
        s
        for s in summaries
        if s["name"] in dev_returns.columns
        and s["dev_metrics"]["sharpe"] >= min_dev_sharpe
        and s["dev_stressed_metrics"]["sharpe"] >= min_stressed_sharpe
        and s["dev_pos_fold_ratio"] >= min_pos_fold_ratio
    ]
    # Rank by blended Dev base Sharpe + stressed Sharpe so low-turnover robust alphas win
    survivors.sort(
        key=lambda s: -(0.5 * s["dev_metrics"]["sharpe"] + 0.5 * s["dev_stressed_metrics"]["sharpe"]),
    )

    selected: list[str] = []
    fam_counts: dict[str, int] = {}
    fam_map: dict[str, str] = {}

    for s in survivors:
        if len(selected) >= max_total:
            break
        name = s["name"]
        fam = s["family"]
        if fam_counts.get(fam, 0) >= max_per_family:
            continue
        if selected:
            corrs = [abs(float(dev_returns[name].corr(dev_returns[prev]))) for prev in selected]
            if any(math.isfinite(c) and c > max_corr for c in corrs):
                continue
        selected.append(name)
        fam_counts[fam] = fam_counts.get(fam, 0) + 1
        fam_map[name] = fam

    return selected, fam_map


def _normalize_weights(w: np.ndarray, *, max_weight: float = 0.40) -> np.ndarray:
    w = np.asarray(w, dtype=float)
    w = np.where(np.isfinite(w), np.maximum(w, 0.0), 0.0)
    if len(w) == 0:
        return w
    if w.sum() <= 0:
        return np.full(len(w), 1.0 / len(w))
    # A cap below 1/n is infeasible (weights must sum to 1); relax it to equal weight.
    max_weight = max(max_weight, 1.0 / len(w))
    w = w / w.sum()
    # Iteratively clip to max_weight and redistribute
    for _ in range(10):
        over = w > max_weight
        if not over.any():
            break
        excess = float((w[over] - max_weight).sum())
        w[over] = max_weight
        under = ~over
        if under.any() and w[under].sum() > 0:
            w[under] += excess * (w[under] / w[under].sum())
        else:
            break
    return w / w.sum()


def fit_combiner_weights(dev_df: pd.DataFrame, *, max_weight: float = 0.40) -> dict[str, np.ndarray]:
    """Fit 3 conservative, strictly Dev-only weight vectors."""
    n = dev_df.shape[1]
    if n == 1:
        one = np.array([1.0])
        return {"equal_weight": one, "inverse_vol": one, "shrinkage_sharpe": one}

    stds = dev_df.std(ddof=1).to_numpy(dtype=float)
    stds = np.where(stds > 1e-8, stds, 1e-4)
    means = dev_df.mean().to_numpy(dtype=float)

    # 1. Equal weight
    w_eq = np.full(n, 1.0 / n)

    # 2. Inverse-volatility risk parity
    w_iv = _normalize_weights(1.0 / stds, max_weight=max_weight)

    # 3. Correlation-penalized shrinkage max-Sharpe (regularized cov -> closed-form non-neg)
    cov = dev_df.cov().to_numpy(dtype=float)
    diag = np.diag(np.diag(cov))
    cov_reg = 0.5 * cov + 0.5 * diag + np.eye(n) * 1e-10
    mu_pos = np.maximum(means, 0.0)
    try:
        raw = np.linalg.solve(cov_reg, mu_pos)
    except np.linalg.LinAlgError:
        raw = 1.0 / stds
    w_sh = _normalize_weights(raw, max_weight=max_weight)

    return {
        "equal_weight": w_eq,
        "inverse_vol": w_iv,
        "shrinkage_sharpe": w_sh,
    }


def apply_vol_target(
    ret: pd.Series,
    *,
    target_vol_annual: float = 0.12,
    lookback_h: int = 168,
    max_leverage: float = 3.0,
) -> pd.Series:
    """Strictly causal rolling volatility targeting (uses ``shift(1)`` realized vol)."""
    realized = ret.shift(1).rolling(lookback_h, min_periods=24).std() * math.sqrt(HOURS_PER_YEAR)
    scale = (target_vol_annual / realized.replace(0.0, np.nan)).clip(lower=0.25, upper=max_leverage).fillna(1.0)
    return (ret * scale).rename(ret.name)


def build_metamodel(
    summaries: list[dict[str, Any]],
    oof_returns: pd.DataFrame,
    stressed_returns: pd.DataFrame,
    *,
    holdout_start_ms: int,
    min_dev_sharpe: float = 0.5,
    min_stressed_sharpe: float = 0.0,
    min_pos_fold_ratio: float = 0.60,
    max_corr: float = 0.50,
    max_per_family: int = 2,
    max_total: int = 8,
    target_vol_annual: float = 0.12,
    max_leverage: float = 3.0,
) -> MetamodelResult:
    """Select orthogonal alphas & fit weights strictly on Dev; audit once on untouched Holdout."""
    dev_mask = oof_returns.index < holdout_start_ms
    hold_mask = oof_returns.index >= holdout_start_ms
    dev_ret = oof_returns.loc[dev_mask]

    selected, fam_map = select_orthogonal_hypotheses(
        summaries,
        dev_ret,
        min_dev_sharpe=min_dev_sharpe,
        min_stressed_sharpe=min_stressed_sharpe,
        min_pos_fold_ratio=min_pos_fold_ratio,
        max_corr=max_corr,
        max_per_family=max_per_family,
        max_total=max_total,
    )
    if not selected:
        raise ValueError("no hypotheses passed Dev OOF + stress selection gate")

    dev_sel = dev_ret[selected]
    full_sel = oof_returns[selected]
    full_stress_sel = stressed_returns[selected]

    weight_vecs = fit_combiner_weights(dev_sel)
    weights_by_method = {
        method: {name: float(w) for name, w in zip(selected, vec)}
        for method, vec in weight_vecs.items()
    }

    dev_metrics: dict[str, dict[str, float]] = {}
    holdout_metrics: dict[str, dict[str, float]] = {}
    holdout_stressed_metrics: dict[str, dict[str, float]] = {}
    holdout_cols: dict[str, pd.Series] = {}

    for method, vec in weight_vecs.items():
        full_r = pd.Series(full_sel.to_numpy(dtype=float) @ vec, index=full_sel.index, name=method)
        full_s = pd.Series(full_stress_sel.to_numpy(dtype=float) @ vec, index=full_stress_sel.index, name=method)

        dev_metrics[method] = series_metrics(full_r.loc[dev_mask])
        holdout_metrics[method] = series_metrics(full_r.loc[hold_mask])
        holdout_stressed_metrics[method] = series_metrics(full_s.loc[hold_mask])
        holdout_cols[method] = full_r.loc[hold_mask]

        # Also evaluate causal vol-targeted version (capped at max_leverage)
        vt_name = f"{method}_vt{int(target_vol_annual * 100)}"
        full_vt = apply_vol_target(full_r, target_vol_annual=target_vol_annual, max_leverage=max_leverage)
        full_vt_s = apply_vol_target(full_s, target_vol_annual=target_vol_annual, max_leverage=max_leverage)
        dev_metrics[vt_name] = series_metrics(full_vt.loc[dev_mask])
        holdout_metrics[vt_name] = series_metrics(full_vt.loc[hold_mask])
        holdout_stressed_metrics[vt_name] = series_metrics(full_vt_s.loc[hold_mask])
        holdout_cols[vt_name] = full_vt.loc[hold_mask]

    return MetamodelResult(
        selected_names=selected,
        selected_families=fam_map,
        weights_by_method=weights_by_method,
        dev_metrics=dev_metrics,
        holdout_metrics=holdout_metrics,
        holdout_stressed_metrics=holdout_stressed_metrics,
        corr_matrix_dev=dev_sel.corr(),
        holdout_hourly=pd.DataFrame(holdout_cols),
    )
