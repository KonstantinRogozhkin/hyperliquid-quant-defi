"""Institutional Analytics & Risk Engine modeled on Neutral Trade's `/analytics` standard.

Computes unified, auditable quantitative analytics directly from persisted atomic paper/live
artifacts under ``HL_DATA_ROOT`` (100% local read-only, zero blocking network calls):
- **Strategy vs Buy-&-Hold Leaderboard:** 7D / 30D / YTD / All-Time Gross & Net (HWM) returns
  and APY (never annualizing negative returns), trailing 90D Sharpe & Sortino, Max Drawdown,
  Drawdown Recovery days, Positive Days %, Longest Win/Loss Streaks, and NAV Share Price.
- **Lower-Triangular Pearson Correlation Matrix (ρ):** pairwise daily log-return correlations.
- **OLS Market Exposure Regression:** ``β``, ``R²``, ``α (ann.)``, and daily scatter points
  against live benchmarks (``BTC``, ``ETH``, ``HYPE``).
- **Monthly Historical Returns Matrix:** Year × Month grid (Gross & Net).
- **Consolidated Live Risk & Exposure Telemetry:** Current / 30D Avg / 30D Max Leverage,
  Venue Concentration, Market-Cap / Asset Tier breakdown, and Bounded NAV Guard audit.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

TRADING_DAYS_PER_YEAR = 365.0
MS_PER_DAY = 86_400_000


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


# --------------------------------------------------------------------------- Core Quant Math


def simulate_hwm_net_series(
    dates: list[str],
    gross_shares: list[float],
    *,
    mgmt_fee_annual_pct: float = 1.0,
    perf_fee_pct: float = 20.0,
) -> list[float]:
    """Convert a Gross NAV share-price series into an exact Net-of-Fee High-Water Mark series."""
    if not gross_shares:
        return []
    if mgmt_fee_annual_pct <= 0.0 and perf_fee_pct <= 0.0:
        return [round(float(x), 6) for x in gross_shares]

    net: list[float] = [float(gross_shares[0])]
    hwm = float(gross_shares[0])
    for i in range(1, len(gross_shares)):
        prev_g = float(gross_shares[i - 1])
        cur_g = float(gross_shares[i])
        gross_r = (cur_g / prev_g - 1.0) if prev_g > 0 else 0.0
        try:
            d0 = datetime.fromisoformat(dates[i - 1])
            d1 = datetime.fromisoformat(dates[i])
            dt_days = max(0.0, (d1 - d0).total_seconds() / 86_400.0)
        except Exception:
            dt_days = 1.0

        pre_fee = net[-1] * (1.0 + gross_r)
        mgmt = pre_fee * (mgmt_fee_annual_pct / 100.0) * (dt_days / TRADING_DAYS_PER_YEAR)
        after_mgmt = max(pre_fee - mgmt, 1e-6)
        if after_mgmt > hwm and perf_fee_pct > 0.0:
            perf = (after_mgmt - hwm) * (perf_fee_pct / 100.0)
            after_all = max(after_mgmt - perf, 1e-6)
            hwm = max(hwm, after_all)
        else:
            after_all = after_mgmt
        net.append(round(after_all, 6))
    return net


def _annualize_return_pct(ret_pct: float, days: float) -> float:
    """Neutral Trade methodology: annualize positive returns cleanly; never annualize negative returns."""
    if ret_pct < 0.0 or days <= 0.0:
        return round(ret_pct, 2)
    eff_days = max(days, 1.0)
    base = 1.0 + ret_pct / 100.0
    if base <= 0.0:
        return round(ret_pct, 2)
    # Cap exponent so 1-day spikes don't explode to Inf
    ann = (base ** min(TRADING_DAYS_PER_YEAR / eff_days, 365.0) - 1.0) * 100.0
    return round(min(ann, 9999.0), 2)


def _window_return(
    dates_dt: list[datetime],
    shares: list[float],
    *,
    days_back: int | None = None,
    ytd: bool = False,
) -> tuple[float | None, float]:
    """Return ``(ret_pct, elapsed_days)`` over trailing window or YTD."""
    if len(shares) < 2 or not dates_dt:
        return (0.0 if shares else None), 0.0
    end_dt = dates_dt[-1]
    end_val = shares[-1]

    if ytd:
        target = datetime(end_dt.year, 1, 1, tzinfo=end_dt.tzinfo)
    elif days_back is not None:
        target = datetime.fromtimestamp(end_dt.timestamp() - days_back * 86_400.0, tz=end_dt.tzinfo)
    else:
        start_val = shares[0]
        span = max((end_dt - dates_dt[0]).total_seconds() / 86_400.0, 0.0)
        if start_val <= 0:
            return None, span
        return round((end_val / start_val - 1.0) * 100.0, 2), span

    # Find latest observation <= target (or earliest available if history is shorter than requested window)
    idx = 0
    for i, d in enumerate(dates_dt):
        if d <= target:
            idx = i
        else:
            break
    if days_back is not None and dates_dt[0] > target:
        # Not enough history to cover the requested lookback window
        return None, 0.0
    start_val = shares[idx]
    span = max((end_dt - dates_dt[idx]).total_seconds() / 86_400.0, 0.0)
    if start_val <= 0:
        return None, span
    return round((end_val / start_val - 1.0) * 100.0, 2), span


def _compute_drawdown_stats(dates_dt: list[datetime], shares: list[float]) -> tuple[float, int]:
    """Return ``(max_dd_pct_positive, max_recovery_days)``."""
    if len(shares) < 2:
        return 0.0, 0
    peak = shares[0]
    peak_dt = dates_dt[0]
    max_dd = 0.0
    max_rec_days = 0
    in_dd = False
    dd_start_dt = dates_dt[0]

    for dt, val in zip(dates_dt, shares, strict=False):
        if val >= peak:
            if in_dd:
                rec_days = int(round((dt - dd_start_dt).total_seconds() / 86_400.0))
                max_rec_days = max(max_rec_days, rec_days)
                in_dd = False
            peak = val
            peak_dt = dt
        else:
            dd = (peak - val) / peak * 100.0 if peak > 0 else 0.0
            if dd > max_dd:
                max_dd = dd
            if not in_dd:
                in_dd = True
                dd_start_dt = peak_dt

    if in_dd:
        ongoing = int(round((dates_dt[-1] - dd_start_dt).total_seconds() / 86_400.0))
        max_rec_days = max(max_rec_days, ongoing)
    return round(max_dd, 2), max_rec_days


def _compute_streaks(log_rets: np.ndarray) -> tuple[float | None, int, int]:
    """Return ``(positive_days_pct, longest_win_streak, longest_loss_streak)``."""
    if len(log_rets) == 0:
        return None, 0, 0
    pos_cnt = int(np.sum(log_rets > 0.0))
    pos_pct = round(pos_cnt / len(log_rets) * 100.0, 1)
    max_w = max_l = cur_w = cur_l = 0
    for r in log_rets:
        if r > 0.0:
            cur_w += 1
            cur_l = 0
            max_w = max(max_w, cur_w)
        elif r < 0.0:
            cur_l += 1
            cur_w = 0
            max_l = max(max_l, cur_l)
        else:
            cur_w = 0
            cur_l = 0
    return pos_pct, max_w, max_l


def compute_series_performance(
    *,
    strategy_id: str,
    name: str,
    category: str,
    dates: list[str],
    gross_shares: list[float],
    net_shares: list[float] | None = None,
    equity_usd: float | None = None,
    mgmt_fee_pct: float = 1.0,
    perf_fee_pct: float = 20.0,
    is_benchmark: bool = False,
) -> dict[str, Any]:
    """Compute full Neutral-Trade-grade performance metrics for one strategy or benchmark."""
    if net_shares is None:
        if is_benchmark:
            net_s = list(gross_shares)
        else:
            net_s = simulate_hwm_net_series(
                dates,
                gross_shares,
                mgmt_fee_annual_pct=mgmt_fee_pct,
                perf_fee_pct=perf_fee_pct,
            )
    else:
        net_s = list(net_shares)

    dates_dt: list[datetime] = []
    for ds in dates:
        try:
            d = datetime.fromisoformat(ds)
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            dates_dt.append(d)
        except Exception:
            dates_dt.append(datetime.now(tz=timezone.utc))

    def _pack_horizons(series: list[float]) -> tuple[dict[str, float | None], dict[str, float | None]]:
        r7, d7 = _window_return(dates_dt, series, days_back=7)
        r30, d30 = _window_return(dates_dt, series, days_back=30)
        rytd, dytd = _window_return(dates_dt, series, ytd=True)
        rall, dall = _window_return(dates_dt, series)
        rets_map = {"7d": r7, "30d": r30, "ytd": rytd, "all_time": rall}
        apy_map = {
            "7d": _annualize_return_pct(r7, d7) if r7 is not None else None,
            "30d": _annualize_return_pct(r30, d30) if r30 is not None else None,
            "ytd": _annualize_return_pct(rytd, dytd) if rytd is not None else None,
            "all_time": _annualize_return_pct(rall, dall) if rall is not None else None,
        }
        return rets_map, apy_map

    gross_ret, gross_apy = _pack_horizons(gross_shares)
    net_ret, net_apy = _pack_horizons(net_s)

    # Trailing 90-day daily log returns for Sharpe & Sortino
    sharpe_g = sortino_g = sharpe_n = sortino_n = None
    pos_days_pct = None
    win_streak = loss_streak = 0

    def _ratios(series: list[float]) -> tuple[float | None, float | None]:
        if len(series) < 3:
            return None, None
        sub = np.asarray(series[-91:], dtype=float)
        sub = sub[sub > 0]
        if len(sub) < 3:
            return None, None
        lr = np.diff(np.log(sub))
        if len(lr) < 2:
            return None, None
        mean_d = float(np.mean(lr))
        std_d = float(np.std(lr, ddof=1))
        sh = round((mean_d / std_d) * math.sqrt(TRADING_DAYS_PER_YEAR), 2) if std_d > 1e-10 else None
        down = lr[lr < 0.0]
        if len(down) >= 2:
            d_std = float(np.sqrt(np.mean(down * down)))
            so = round((mean_d / d_std) * math.sqrt(TRADING_DAYS_PER_YEAR), 2) if d_std > 1e-10 else None
        elif len(down) == 0 and mean_d > 0:
            so = 99.0
        else:
            so = None
        return sh, so

    if len(gross_shares) >= 2:
        arr_g = np.asarray(gross_shares, dtype=float)
        valid = arr_g > 0
        if np.sum(valid) >= 2:
            lr_all = np.diff(np.log(arr_g[valid]))
            pos_days_pct, win_streak, loss_streak = _compute_streaks(lr_all)
        sharpe_g, sortino_g = _ratios(gross_shares)
        sharpe_n, sortino_n = _ratios(net_s)

    max_dd_g, dd_rec_days = _compute_drawdown_stats(dates_dt, gross_shares)
    max_dd_n, _ = _compute_drawdown_stats(dates_dt, net_s)

    return {
        "id": strategy_id,
        "name": name,
        "category": category,
        "is_benchmark": is_benchmark,
        "fee_schedule": "No fees" if is_benchmark else f"{mgmt_fee_pct:.0f}% mgmt / {perf_fee_pct:.0f}% HWM",
        "equity_usd": round(equity_usd, 2) if equity_usd is not None else None,
        "share_price_gross": round(gross_shares[-1], 4) if gross_shares else None,
        "share_price_net": round(net_s[-1], 4) if net_s else None,
        "days_tracked": len(dates),
        "gross_returns": gross_ret,
        "gross_apy": gross_apy,
        "net_returns": net_ret,
        "net_apy": net_apy,
        "sharpe_gross": sharpe_g,
        "sortino_gross": sortino_g,
        "sharpe_net": sharpe_n,
        "sortino_net": sortino_n,
        "max_dd_gross_pct": max_dd_g,
        "max_dd_net_pct": max_dd_n,
        "dd_recovery_days": dd_rec_days,
        "positive_days_pct": pos_days_pct,
        "longest_win_streak": win_streak,
        "longest_loss_streak": loss_streak,
        "monthly_gross": compute_monthly_returns(dates_dt, gross_shares),
        "monthly_net": compute_monthly_returns(dates_dt, net_s),
    }


def compute_monthly_returns(dates_dt: list[datetime], shares: list[float]) -> list[dict[str, Any]]:
    """Compute Year × Month return table (``jan..dec`` + ``ytd``) from share-price observations."""
    if len(dates_dt) < 2 or len(dates_dt) != len(shares):
        return []
    df = pd.DataFrame({"dt": dates_dt, "share": shares})
    df["year"] = [d.year for d in dates_dt]
    df["month"] = [d.month for d in dates_dt]
    month_end = df.groupby(["year", "month"], as_index=False).last().sort_values(["year", "month"])

    rows_by_year: dict[int, dict[str, Any]] = {}
    prev_val = float(shares[0])
    for _, rec in month_end.iterrows():
        yr = int(rec["year"])
        mo = int(rec["month"])
        cur_val = float(rec["share"])
        r_pct = round((cur_val / prev_val - 1.0) * 100.0, 2) if prev_val > 0 else 0.0
        prev_val = cur_val
        if yr not in rows_by_year:
            rows_by_year[yr] = {"year": yr, "months": {}, "ytd": None}
        rows_by_year[yr]["months"][str(mo)] = r_pct

    # Compute YTD compound per year
    for yr, entry in rows_by_year.items():
        comp = 1.0
        for v in entry["months"].values():
            comp *= 1.0 + v / 100.0
        entry["ytd"] = round((comp - 1.0) * 100.0, 2)

    return [rows_by_year[y] for y in sorted(rows_by_year.keys(), reverse=True)]


def compute_correlation_matrix(
    series_map: dict[str, pd.Series],
    *,
    min_overlap: int = 3,
) -> dict[str, Any]:
    """Compute lower-triangular Pearson correlation matrix (ρ) of daily log returns."""
    ids = list(series_map.keys())
    if not ids:
        return {"ids": [], "matrix": [], "overlap_days": 0}

    df = pd.DataFrame(series_map).sort_index()
    log_rets = np.log(df.where(df > 0)).diff()

    matrix: list[list[float | None]] = []
    max_overlap = 0
    for i, id_i in enumerate(ids):
        row: list[float | None] = []
        for j, id_j in enumerate(ids):
            if i == j:
                row.append(1.0)
            elif j > i:
                row.append(None)  # Lower-triangular layout (Neutral Trade style)
            else:
                pair = log_rets[[id_i, id_j]].dropna()
                n = len(pair)
                max_overlap = max(max_overlap, n)
                if n < min_overlap:
                    row.append(None)
                else:
                    s1 = pair[id_i].to_numpy(dtype=float)
                    s2 = pair[id_j].to_numpy(dtype=float)
                    if np.std(s1) <= 1e-12 or np.std(s2) <= 1e-12:
                        row.append(0.0)
                    else:
                        c = float(np.corrcoef(s1, s2)[0, 1])
                        row.append(round(c, 3) if math.isfinite(c) else None)
        matrix.append(row)

    return {"ids": ids, "matrix": matrix, "overlap_days": max_overlap}


def compute_ols_market_exposure(
    strategy_series: dict[str, tuple[str, pd.Series]],
    factor_series: pd.Series,
    *,
    factor_name: str,
    min_overlap: int = 3,
    max_scatter: int = 120,
) -> dict[str, Any]:
    """OLS regression of strategy daily log returns against a market benchmark factor."""
    f_clean = np.log(factor_series.where(factor_series > 0)).diff().dropna()
    rows: list[dict[str, Any]] = []

    for sid, (sname, s_ser) in strategy_series.items():
        s_ret = np.log(s_ser.where(s_ser > 0)).diff().dropna()
        aligned = pd.concat([f_clean.rename("x"), s_ret.rename("y")], axis=1).dropna()
        n = len(aligned)
        if n < min_overlap:
            rows.append(
                {
                    "id": sid,
                    "name": sname,
                    "beta": None,
                    "r2_pct": None,
                    "alpha_ann_pct": None,
                    "days": n,
                    "scatter": [],
                }
            )
            continue

        x = aligned["x"].to_numpy(dtype=float)
        y = aligned["y"].to_numpy(dtype=float)
        x_mean = float(np.mean(x))
        y_mean = float(np.mean(y))
        var_x = float(np.sum((x - x_mean) ** 2))
        if var_x <= 1e-14:
            beta = 0.0
            alpha_d = y_mean
            r2 = 0.0
        else:
            cov_xy = float(np.sum((x - x_mean) * (y - y_mean)))
            beta = cov_xy / var_x
            alpha_d = y_mean - beta * x_mean
            y_hat = alpha_d + beta * x
            ss_tot = float(np.sum((y - y_mean) ** 2))
            ss_res = float(np.sum((y - y_hat) ** 2))
            r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-14 else 0.0

        tail = aligned.tail(max_scatter)
        scatter = [
            {
                "date": str(idx),
                "factor_ret_pct": round(float(r["x"]) * 100.0, 3),
                "strat_ret_pct": round(float(r["y"]) * 100.0, 3),
            }
            for idx, r in tail.iterrows()
        ]
        rows.append(
            {
                "id": sid,
                "name": sname,
                "beta": round(beta, 4),
                "r2_pct": round(r2 * 100.0, 2),
                "alpha_ann_pct": round(alpha_d * TRADING_DAYS_PER_YEAR * 100.0, 2),
                "days": n,
                "scatter": scatter,
            }
        )

    return {
        "factor": factor_name,
        "factor_days": int(len(f_clean)),
        "rows": rows,
    }


# --------------------------------------------------------------------------- Artifact Loaders


def _load_daily_share_series_from_ticks(
    ticks_path: Path,
    state_path: Path,
    *,
    ts_col_candidates: tuple[str, ...] = ("tick_ms", "tick_hour_ms", "ts_ms"),
    eq_col: str = "equity_usd",
    net_col: str | None = None,
) -> tuple[list[str], list[float], list[float] | None, float | None]:
    """Load daily-closed share price series from a strategy's ``ticks.parquet`` + ``state.json``."""
    st = _read_json(state_path) or {}
    start_cap = _f(st.get("starting_capital_usd"), 100_000.0)
    cur_eq = _f(st.get("equity_usd"), _f(st.get("gross_equity_usd"), 0.0)) or None

    if not ticks_path.is_file():
        if cur_eq is not None and start_cap > 0:
            today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
            sp = round(cur_eq / start_cap, 6)
            return [today], [sp], None, cur_eq
        return [], [], None, cur_eq

    try:
        df = pd.read_parquet(ticks_path)
    except Exception:
        return [], [], None, cur_eq
    if df.empty or eq_col not in df.columns:
        return [], [], None, cur_eq

    ts_col = next((c for c in ts_col_candidates if c in df.columns), None)
    if ts_col is None:
        return [], [], None, cur_eq

    work = df.copy()
    work["_ts"] = pd.to_numeric(work[ts_col], errors="coerce")
    work["_eq"] = pd.to_numeric(work[eq_col], errors="coerce")
    work = work.dropna(subset=["_ts", "_eq"])
    work = work[work["_eq"] > 0].sort_values("_ts")
    if work.empty:
        return [], [], None, cur_eq

    base_cap = start_cap if start_cap > 0 else float(work["_eq"].iloc[0])
    work["_date"] = pd.to_datetime(work["_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%d")

    # Group to UTC daily close; if all ticks fall on 1 calendar day, keep them indexed by ISO timestamp so charts still render!
    if work["_date"].nunique() >= 2:
        daily = work.groupby("_date", as_index=False).last().sort_values("_date")
        dates = daily["_date"].astype(str).tolist()
        gross_s = [round(float(x) / base_cap, 6) for x in daily["_eq"].tolist()]
        net_s: list[float] | None = None
        if net_col and net_col in daily.columns:
            net_vals = pd.to_numeric(daily[net_col], errors="coerce")
            if net_vals.notna().all() and (net_vals > 0).all():
                net_s = [round(float(x) / base_cap, 6) for x in net_vals.tolist()]
        last_eq = float(daily["_eq"].iloc[-1])
        return dates, gross_s, net_s, last_eq

    # Intraday-only bootstrap history (< 2 calendar days yet)
    iso_ts = pd.to_datetime(work["_ts"], unit="ms", utc=True).dt.strftime("%Y-%m-%dT%H:%M:%SZ").tolist()
    gross_s = [round(float(x) / base_cap, 6) for x in work["_eq"].tolist()]
    net_s = None
    if net_col and net_col in work.columns:
        net_vals = pd.to_numeric(work[net_col], errors="coerce")
        if net_vals.notna().all() and (net_vals > 0).all():
            net_s = [round(float(x) / base_cap, 6) for x in net_vals.tolist()]
    return iso_ts, gross_s, net_s, float(work["_eq"].iloc[-1])


def _load_benchmarks(data_root: Path) -> dict[str, tuple[list[str], list[float]]]:
    """Load cached daily benchmark closes from ``hl_benchmarks/daily_closes.json``."""
    raw = _read_json(data_root / "hl_benchmarks" / "daily_closes.json")
    if not raw:
        return {}
    out: dict[str, tuple[list[str], list[float]]] = {}
    series = raw.get("benchmarks") or {}
    if not isinstance(series, dict):
        return {}
    for sym, pts in series.items():
        if not isinstance(pts, list) or len(pts) < 2:
            continue
        dates = [str(p["date"]) for p in pts if isinstance(p, dict) and p.get("date") and _f(p.get("close")) > 0]
        closes = [_f(p["close"]) for p in pts if isinstance(p, dict) and p.get("date") and _f(p.get("close")) > 0]
        if len(dates) >= 2 and closes[0] > 0:
            out[str(sym).upper()] = (dates, closes)
    return out


def build_consolidated_risk_exposure(data_root: Path) -> dict[str, Any]:
    """Build Neutral-style consolidated Live Risk & Exposure telemetry across all active desk engines."""
    ap = _read_json(data_root / "hl_autopilot" / "state.json")
    sp = _read_json(data_root / "hl_spot_perp_paper" / "state.json")
    cd = _read_json(data_root / "hl_cross_dex_paper" / "state.json")
    h3 = _read_json(data_root / "hl_hip3_paper" / "state.json")

    venue_usd: dict[str, float] = {}
    tier_usd: dict[str, float] = {}
    leverage_rows: list[dict[str, Any]] = []
    total_nav_guard_triggers = 0

    if ap:
        rt = ap.get("risk_telemetry") or {}
        total_nav_guard_triggers += int(ap.get("nav_guard_triggers", 0) or 0)
        leverage_rows.append(
            {
                "strategy": "Neutral Autopilot",
                "current_leverage": _f(rt.get("gross_leverage")),
                "leverage_30d_avg": _f(rt.get("leverage_30d_avg")),
                "leverage_30d_max": _f(rt.get("leverage_30d_max")),
                "net_exposure_pct": _f(rt.get("net_directional_exposure_pct")),
                "nav_guard_triggers": int(ap.get("nav_guard_triggers", 0) or 0),
            }
        )
        for v, amt in (rt.get("venue_breakdown_usd") or {}).items():
            venue_usd[str(v)] = round(venue_usd.get(str(v), 0.0) + _f(amt), 2)

    if sp:
        rt = sp.get("risk_telemetry") or {}
        total_nav_guard_triggers += int(sp.get("nav_guard_triggers", 0) or 0)
        leverage_rows.append(
            {
                "strategy": "HyperCore Spot+Perp (+10% HLP)",
                "current_leverage": _f(rt.get("gross_leverage")),
                "leverage_30d_avg": _f(rt.get("leverage_30d_avg")),
                "leverage_30d_max": _f(rt.get("leverage_30d_max")),
                "net_exposure_pct": _f(rt.get("net_delta_pct")),
                "nav_guard_triggers": int(sp.get("nav_guard_triggers", 0) or 0),
            }
        )
        for t, amt in (rt.get("asset_tier_breakdown_usd") or {}).items():
            tier_usd[str(t)] = round(tier_usd.get(str(t), 0.0) + _f(amt), 2)
        if not ap:
            for v, amt in (rt.get("venue_breakdown_usd") or {}).items():
                venue_usd[str(v)] = round(venue_usd.get(str(v), 0.0) + _f(amt), 2)

    if cd:
        cd_pos = cd.get("positions") or {}
        cd_eq = max(_f(cd.get("equity_usd"), 100_000.0), 1.0)
        cd_gross = sum(2.0 * _f(p.get("leg_notional_usd")) for p in cd_pos.values() if isinstance(p, dict))
        lev = round(cd_gross / cd_eq, 3)
        leverage_rows.append(
            {
                "strategy": "Cross-DEX (HL×Lighter×Ext)",
                "current_leverage": lev,
                "leverage_30d_avg": lev,
                "leverage_30d_max": lev,
                "net_exposure_pct": 0.0,
                "nav_guard_triggers": 0,
            }
        )
        if not ap:
            venue_usd["HL × Lighter/Extended"] = round(venue_usd.get("HL × Lighter/Extended", 0.0) + cd_eq, 2)

    if h3:
        h3_pos = h3.get("positions") or {}
        h3_eq = max(_f(h3.get("equity_usd"), 50_000.0), 1.0)
        h3_gross = sum(2.0 * _f(p.get("leg_notional_usd")) for p in h3_pos.values() if isinstance(p, dict))
        lev = round(h3_gross / h3_eq, 3)
        leverage_rows.append(
            {
                "strategy": "HIP-3 Split-Margin",
                "current_leverage": lev,
                "leverage_30d_avg": lev,
                "leverage_30d_max": lev,
                "net_exposure_pct": 0.0,
                "nav_guard_triggers": 0,
            }
        )
        eq_deployed = sum(_f(p.get("margin_locked_usd")) for p in h3_pos.values() if isinstance(p, dict))
        if eq_deployed > 0:
            tier_usd["US Equities / HIP-3 Builders"] = round(tier_usd.get("US Equities / HIP-3 Builders", 0.0) + eq_deployed, 2)
        if not ap:
            venue_usd["HIP-3 Builders"] = round(venue_usd.get("HIP-3 Builders", 0.0) + h3_eq, 2)

    def _to_pct_rows(raw_map: dict[str, float]) -> list[dict[str, Any]]:
        tot = sum(v for v in raw_map.values() if v > 0)
        if tot <= 0:
            return []
        rows = [
            {"label": k, "usd": round(v, 2), "share_pct": round(v / tot * 100.0, 2)}
            for k, v in raw_map.items()
            if v > 0
        ]
        rows.sort(key=lambda r: -r["usd"])
        return rows

    return {
        "leverage_by_strategy": leverage_rows,
        "venue_concentration": _to_pct_rows(venue_usd),
        "asset_tier_concentration": _to_pct_rows(tier_usd),
        "total_nav_guard_triggers": total_nav_guard_triggers,
    }


def build_desk_analytics_payload(data_root: Path) -> dict[str, Any]:
    """Assemble the complete Neutral-Trade-style ``/api/v1/analytics`` JSON payload."""
    strategy_specs = [
        {
            "id": "autopilot",
            "name": "Neutral Autopilot",
            "category": "Master Vault",
            "ticks": data_root / "hl_autopilot" / "ticks.parquet",
            "state": data_root / "hl_autopilot" / "state.json",
            "net_col": "net_equity_usd",
            "mgmt": 1.0,
            "perf": 10.0,
        },
        {
            "id": "spot_perp",
            "name": "HyperCore Spot+Perp (+10% HLP)",
            "category": "Market Neutral",
            "ticks": data_root / "hl_spot_perp_paper" / "ticks.parquet",
            "state": data_root / "hl_spot_perp_paper" / "state.json",
            "net_col": None,
            "mgmt": 1.0,
            "perf": 20.0,
        },
        {
            "id": "cross_dex",
            "name": "Cross-DEX (HL×Lighter×Ext)",
            "category": "Market Neutral",
            "ticks": data_root / "hl_cross_dex_paper" / "ticks.parquet",
            "state": data_root / "hl_cross_dex_paper" / "state.json",
            "net_col": None,
            "mgmt": 1.0,
            "perf": 20.0,
        },
        {
            "id": "hip3_paper",
            "name": "HIP-3 Growth Split-Margin",
            "category": "Market Neutral",
            "ticks": data_root / "hl_hip3_paper" / "ticks.parquet",
            "state": data_root / "hl_hip3_paper" / "state.json",
            "net_col": None,
            "mgmt": 1.0,
            "perf": 20.0,
        },
        {
            "id": "metamodel",
            "name": "6-Family Metamodel (Obs)",
            "category": "Market Neutral (Obs)",
            "ticks": data_root / "hl_metamodel" / "ticks.parquet",
            "state": data_root / "hl_metamodel" / "state.json",
            "net_col": None,
            "mgmt": 1.0,
            "perf": 20.0,
        },
    ]

    leaderboard: list[dict[str, Any]] = []
    curves: dict[str, dict[str, Any]] = {}
    corr_inputs: dict[str, pd.Series] = {}
    ols_strat_inputs: dict[str, tuple[str, pd.Series]] = {}

    for spec in strategy_specs:
        dates, gross_s, net_s, cur_eq = _load_daily_share_series_from_ticks(
            spec["ticks"], spec["state"], net_col=spec["net_col"]
        )
        if not dates:
            continue
        if net_s is None:
            net_s = simulate_hwm_net_series(
                dates, gross_s, mgmt_fee_annual_pct=spec["mgmt"], perf_fee_pct=spec["perf"]
            )
        row = compute_series_performance(
            strategy_id=spec["id"],
            name=spec["name"],
            category=spec["category"],
            dates=dates,
            gross_shares=gross_s,
            net_shares=net_s,
            equity_usd=cur_eq,
            mgmt_fee_pct=spec["mgmt"],
            perf_fee_pct=spec["perf"],
            is_benchmark=False,
        )
        leaderboard.append(row)
        curves[spec["id"]] = {
            "id": spec["id"],
            "name": spec["name"],
            "category": spec["category"],
            "is_benchmark": False,
            "dates": dates,
            "gross_shares": gross_s,
            "net_shares": net_s,
        }
        # Normalize date index to YYYY-MM-DD for cross-strategy correlation & benchmark OLS
        day_keys = [d[:10] for d in dates]
        ser = pd.Series(gross_s, index=day_keys).groupby(level=0).last()
        corr_inputs[spec["id"]] = ser
        ols_strat_inputs[spec["id"]] = (spec["name"], ser)

    # Buy & Hold benchmarks (no fees)
    benchmarks = _load_benchmarks(data_root)
    market_exposure: dict[str, Any] = {}
    for sym, (b_dates, b_closes) in benchmarks.items():
        b_id = f"bench_{sym.lower()}"
        base_px = b_closes[0]
        b_shares = [round(px / base_px, 6) for px in b_closes]
        b_row = compute_series_performance(
            strategy_id=b_id,
            name=f"{sym} (Buy & Hold)",
            category="Benchmark",
            dates=b_dates,
            gross_shares=b_shares,
            net_shares=b_shares,
            equity_usd=b_closes[-1],
            mgmt_fee_pct=0.0,
            perf_fee_pct=0.0,
            is_benchmark=True,
        )
        b_row["spot_price_usd"] = round(b_closes[-1], 4)
        leaderboard.append(b_row)
        curves[b_id] = {
            "id": b_id,
            "name": f"{sym} (Buy & Hold)",
            "category": "Benchmark",
            "is_benchmark": True,
            "dates": b_dates,
            "gross_shares": b_shares,
            "net_shares": b_shares,
        }
        if sym in ("BTC", "ETH", "HYPE", "PAXG"):
            f_ser = pd.Series(b_shares, index=[d[:10] for d in b_dates]).groupby(level=0).last()
            market_exposure[sym] = compute_ols_market_exposure(
                ols_strat_inputs, f_ser, factor_name=sym
            )

    correlation = compute_correlation_matrix(corr_inputs)
    correlation["labels"] = {row["id"]: row["name"] for row in leaderboard if row["id"] in corr_inputs}

    return {
        "generated_at_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "methodology": {
            "sharpe_sortino_window_days": 90,
            "annualization_basis_days": 365,
            "negative_returns_annualized": False,
            "net_fee_model": "Continuous annual management fee + High-Water Mark (HWM) performance fee on net NAV",
            "correlation_method": "Pearson rho on overlapping UTC daily log returns (lower-triangular)",
            "ols_method": "Daily log-return OLS y = alpha + beta * factor (annualized alpha = 365 * alpha_daily)",
        },
        "leaderboard": leaderboard,
        "curves": curves,
        "correlation": correlation,
        "market_exposure": market_exposure,
        "risk_exposure": build_consolidated_risk_exposure(data_root),
    }
