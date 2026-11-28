"""Multi-Strategy "Autopilot" Master Portfolio Allocator with Deadband Rebalancing & <=5% Crisis-CTA Sleeve.

Inspired by Neutral Trade's *Neutral Autopilot* master vault:
- **Market-Neutral Core (80–85%):** aggregates live/paper desk sleeves (``spot_perp_paper``,
  ``cross_dex_paper``, ``hip3_paper``, plus Sharpe-gated ``metamodel_paper``) with shrinkage
  inverse-volatility risk-parity weighting and per-sleeve concentration caps (<= 40%).
- **Liquidity Reserve Floor (>= 10%):** HLP / USDC Savings buffer so margin swings never force
  emergency unwinds.
- **Directional Crisis-Alpha CTA Sleeve (hard cap <= 5.0%):** low-turnover multi-horizon
  time-series momentum (10d/20d/40d TSMOM) over HyperCore majors + Gold RWA (``BTC, ETH, SOL,
  HYPE, PAXG``) with volatility targeting and signal deadband — designed to deliver negative
  correlation during bear drawdowns when funding compresses.
- **Deadband Rebalancing:** avoids burning 5 bps execution drag on tiny weight drifts; only
  rebalances when ``max(|actual_w - target_w|) >= rebalance_deadband_pct`` or a hard risk limit
  is breached.
- **Institutional NAV Hygiene:** Gross vs Net-of-Fee High-Water Mark (HWM) share-price tracking
  plus a **Bounded NAV Sanity Guard** (circuit breaker against anomalous upstream ticks).

100% read-only against Hyperliquid public APIs; zero live orders placed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx
import numpy as np
import pandas as pd

from hl_quant import hl_spot_perp_arb as spa

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
NAV_GUARD_CONFIRM_MS = 3_600_000
HOURS_PER_YEAR = 8760.0
CTA_UNIVERSE: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE", "PAXG")

DEFAULT_BASE_WEIGHTS: dict[str, float] = {
    "spot_perp_paper": 0.35,
    "cross_dex_paper": 0.25,
    "hip3_paper": 0.20,
    "hlp_savings_reserve": 0.15,
    "crisis_cta": 0.05,
    "metamodel_paper": 0.00,
}

SLEEVE_CATEGORY: dict[str, str] = {
    "spot_perp_paper": "Market Neutral",
    "cross_dex_paper": "Market Neutral",
    "hip3_paper": "Market Neutral",
    "metamodel_paper": "Market Neutral (Obs)",
    "hlp_savings_reserve": "Savings / Reserve",
    "crisis_cta": "Directional CTA",
}


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


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _append_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    new_df = pd.DataFrame(rows)
    if path.is_file():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    tmp = path.with_suffix(".tmp.parquet")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)


@dataclass(frozen=True, slots=True)
class AutopilotConfig:
    starting_capital_usd: float = 250_000.0
    max_single_sleeve_weight: float = 0.40
    min_reserve_weight: float = 0.10
    target_reserve_weight: float = 0.15
    max_directional_weight: float = 0.05  # Neutral Autopilot hard cap: <= 5% Directional CTA
    rebalance_deadband_pct: float = 3.0  # Rebalance only if max sleeve drift >= 3.0%
    rebalance_cost_bps: float = 5.0  # Realistic HL reallocation cost
    min_metamodel_sharpe: float = 0.50  # Keep observation-only metamodel at 0% unless live Sharpe >= 0.50
    min_history_ticks_for_opt: int = 6
    cta_target_ann_vol: float = 0.15
    cta_max_leverage: float = 1.5
    cta_max_asset_weight: float = 0.45
    cta_weight_deadband: float = 0.10
    # A flat asset opens once its vol-scaled target reaches this size even if it is inside the
    # deadband (otherwise targets of 4-8% never trade from 0 and 3/5 assets stay dead forever);
    # a sign flip always trades.
    cta_min_open_weight: float = 0.02
    cta_funding_cost_apr_pct: float = 10.0  # conservative perp carry charged on |CTA gross| exposure
    cta_turnover_cost_bps: float = 5.0
    mgmt_fee_annual_pct: float = 1.0  # Model Neutral Autopilot 1% annual management fee
    perf_fee_pct: float = 10.0  # Model performance fee above HWM
    # Performance fee ACCRUES every tick (reversible liability in net NAV) and is CRYSTALLIZED
    # (paid, HWM reset) only once per period: "quarter" or "month".
    perf_fee_crystallization: str = "quarter"
    max_tick_nav_jump_pct: float = 4.0  # Bounded NAV sanity guard
    # A second invocation within this window (scheduler retry, manual re-run) is a no-op instead of
    # booking another tick with dt ~ 0 on top of an already persisted state.
    min_tick_spacing_min: float = 10.0
    # Longest interval time-based accruals (mgmt fee, HLP yield, CTA carry) cover after an outage.
    max_accrual_hours: float = 168.0
    default_hlp_apr_pct: float = 12.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _metamodel_position_gross(pos: dict[str, Any]) -> float:
    """|HL| + sum |per-venue CEX legs| (``cex_weight`` is a signed net; legacy fallback only)."""
    legs = pos.get("cex_legs")
    if isinstance(legs, dict) and legs:
        cex_gross = sum(abs(_f(w)) for w in legs.values())
    else:
        cex_gross = abs(_f(pos.get("cex_weight")))
    return abs(_f(pos.get("hl_weight"))) + cex_gross


# --------------------------------------------------------------------------- Crisis-Alpha CTA Sleeve


def _post_info_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.5,
) -> httpx.Response | None:
    """POST to Hyperliquid Info API with bounded retries on HTTP 429/5xx or transport errors."""
    last_resp: httpx.Response | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.post(url, json=payload)
            last_resp = resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
        except httpx.HTTPError:
            pass
        if attempt + 1 < max_attempts:
            time.sleep(base_delay_s * (attempt + 1))
    return last_resp


def fetch_hypercore_daily_closes_with_dates(
    client: httpx.Client,
    coins: tuple[str, ...] = CTA_UNIVERSE,
    *,
    lookback_days: int = 90,
    now_ms: int | None = None,
    info_url: str = HL_INFO_URL,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch daily ``[{date, close}]`` bars from HyperCore ``candleSnapshot`` for CTA & benchmarks."""
    end_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    start_ms = end_ms - (lookback_days + 5) * 86_400_000
    out: dict[str, list[dict[str, Any]]] = {}
    for coin in coins:
        try:
            r = _post_info_with_retry(
                client,
                info_url,
                {
                    "type": "candleSnapshot",
                    "req": {"coin": coin, "interval": "1d", "startTime": start_ms, "endTime": end_ms},
                },
            )
            if r is None or r.status_code != 200:
                continue
            bars = r.json() or []
            if not isinstance(bars, list):
                continue
            pts: list[dict[str, Any]] = []
            for b in bars:
                if not isinstance(b, dict):
                    continue
                c_px = _f(b.get("c"))
                close_ms = int(_f(b.get("T"))) if b.get("T") is not None else 0
                open_ms = int(_f(b.get("t")))
                # Drop today's still-forming 1d candle (`T > end_ms`) so 20d vol & TSMOM use completed UTC days.
                if close_ms > end_ms:
                    continue
                t_ms = close_ms or open_ms
                if c_px > 0 and t_ms > 0:
                    d_str = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
                    pts.append({"date": d_str, "close": c_px})
            if pts:
                out[coin] = pts[-lookback_days:]
        except Exception:
            continue
    return out


def fetch_hypercore_daily_closes(
    client: httpx.Client,
    coins: tuple[str, ...] = CTA_UNIVERSE,
    *,
    lookback_days: int = 60,
    now_ms: int | None = None,
    info_url: str = HL_INFO_URL,
) -> dict[str, list[float]]:
    """Fetch strictly completed daily closes (up to ``lookback_days``) from HyperCore ``candleSnapshot``."""
    dated = fetch_hypercore_daily_closes_with_dates(
        client, coins, lookback_days=lookback_days, now_ms=now_ms, info_url=info_url
    )
    return {c: [float(p["close"]) for p in pts] for c, pts in dated.items() if pts}


def compute_tsmom_signal(closes: list[float]) -> tuple[float, float]:
    """Return ``(signal_in_minus1_plus1, ann_realized_vol)`` from causal 10d/20d/40d TSMOM."""
    arr = np.asarray([c for c in closes if math.isfinite(c) and c > 0], dtype=float)
    if len(arr) < 12:
        return 0.0, 0.50
    rets = np.diff(np.log(arr))
    vol_win = min(20, len(rets))
    daily_vol = float(np.std(rets[-vol_win:], ddof=1)) if vol_win >= 2 else 0.03
    daily_vol = max(daily_vol, 0.005)
    ann_vol = daily_vol * math.sqrt(365.0)

    horizons = [h for h in (10, 20, 40) if len(arr) > h]
    scores: list[float] = []
    for h in horizons:
        log_r = math.log(arr[-1] / arr[-1 - h])
        # Risk-adjusted momentum z-score mapped smoothly via tanh to [-1, +1]
        z = log_r / (daily_vol * math.sqrt(float(h)))
        scores.append(math.tanh(0.8 * z))
    sig = float(np.mean(scores)) if scores else 0.0
    return round(sig, 4), round(ann_vol, 4)


def compute_crisis_cta_weights(
    daily_closes: dict[str, list[float]],
    *,
    prev_weights: dict[str, float] | None = None,
    cfg: AutopilotConfig = AutopilotConfig(),
) -> dict[str, Any]:
    """Compute volatility-targeted low-turnover TSMOM CTA weights with signal deadband."""
    prev = dict(prev_weights or {})
    raw_weights: dict[str, float] = {}
    asset_details: dict[str, dict[str, Any]] = {}
    n_valid = 0

    for coin, closes in sorted(daily_closes.items()):
        if not closes:
            continue
        sig, ann_vol = compute_tsmom_signal(closes)
        # Per-asset risk-parity contribution scaled toward cta_target_ann_vol
        raw_w = sig * (cfg.cta_target_ann_vol / max(ann_vol, 0.08))
        raw_weights[coin] = raw_w
        asset_details[coin] = {
            "coin": coin,
            "last_close": round(float(closes[-1]), 6),
            "tsmom_signal": sig,
            "ann_vol_pct": round(ann_vol * 100.0, 2),
        }
        n_valid += 1

    # Preserve any currently held CTA coin whose daily bars transiently failed to fetch
    held_missing = sorted(c for c, pw in prev.items() if abs(float(pw)) > 1e-9 and c not in raw_weights)
    n_valid_eff = max(n_valid, len(set(raw_weights) | set(held_missing)))

    if n_valid_eff == 0:
        return {
            "weights": {},
            "gross_leverage": 0.0,
            "net_exposure": 0.0,
            "turnover": 0.0,
            "assets": {},
        }

    # Normalize across universe so equal-risk sum targets cta_target_ann_vol, capped cleanly
    scale = 1.0 / math.sqrt(float(n_valid_eff))
    target_w: dict[str, float] = {}
    for coin, rw in raw_weights.items():
        w = max(-cfg.cta_max_asset_weight, min(cfg.cta_max_asset_weight, rw * scale))
        target_w[coin] = w

    gross = sum(abs(w) for w in target_w.values()) + sum(abs(float(prev[c])) for c in held_missing)
    if gross > cfg.cta_max_leverage and gross > 0 and target_w:
        avail_gross = max(0.0, cfg.cta_max_leverage - sum(abs(float(prev[c])) for c in held_missing))
        cur_tw_gross = sum(abs(w) for w in target_w.values())
        if cur_tw_gross > avail_gross and cur_tw_gross > 0:
            f = avail_gross / cur_tw_gross
            target_w = {c: w * f for c, w in target_w.items()}

    # Apply CTA deadband vs previous weights to suppress micro-churn
    final_w: dict[str, float] = {}
    turnover = 0.0
    for coin, tw in target_w.items():
        pw = float(prev.get(coin, 0.0))
        opens_flat = abs(pw) < 1e-9 and abs(tw) >= cfg.cta_min_open_weight
        flips_sign = pw * tw < 0.0
        if abs(tw - pw) < cfg.cta_weight_deadband and not opens_flat and not flips_sign:
            fw = pw
        else:
            fw = tw
        fw = round(fw, 4)
        final_w[coin] = fw

    for coin in held_missing:
        pw = round(float(prev[coin]), 4)
        final_w[coin] = pw
        target_w[coin] = pw
        asset_details[coin] = {
            "coin": coin,
            "last_close": 0.0,
            "tsmom_signal": 0.0,
            "ann_vol_pct": 0.0,
        }

    # Deadband can keep stale (larger) weights alongside new ones; re-enforce the leverage cap.
    gross_db = sum(abs(w) for w in final_w.values())
    if gross_db > cfg.cta_max_leverage + 1e-9:
        f = cfg.cta_max_leverage / gross_db
        final_w = {c: round(w * f, 4) for c, w in final_w.items()}

    for coin, fw in final_w.items():
        pw = float(prev.get(coin, 0.0))
        turnover += abs(fw - pw)
        asset_details[coin]["weight"] = fw
        asset_details[coin]["target_weight"] = round(target_w[coin], 4)

    gross_final = round(sum(abs(w) for w in final_w.values()), 4)
    net_final = round(sum(final_w.values()), 4)
    return {
        "weights": final_w,
        "gross_leverage": gross_final,
        "net_exposure": net_final,
        "turnover": round(turnover, 4),
        "assets": asset_details,
    }


# --------------------------------------------------------------------------- Sleeve Readers & Weight Optimizer


def _read_sleeve_tick_returns(ticks_path: Path, eq_col: str = "equity_usd") -> list[float]:
    if not ticks_path.is_file():
        return []
    try:
        df = pd.read_parquet(ticks_path)
        if df.empty or eq_col not in df.columns:
            return []
        work = df.copy()
        if "nav_guard_triggered" in work.columns:
            clamped = work["nav_guard_triggered"].fillna(False).astype(bool)
            work = work[~clamped]
        ts_col = next((c for c in ("tick_ms", "ts_ms", "tick_hour_ms") if c in work.columns), None)
        if ts_col is not None:
            work["_ts"] = pd.to_numeric(work[ts_col], errors="coerce")
            work = work.dropna(subset=["_ts"]).sort_values("_ts", kind="stable")
            work = work.drop_duplicates(subset=["_ts"], keep="last")
        work["_eq"] = pd.to_numeric(work[eq_col], errors="coerce")
        work = work.dropna(subset=["_eq"])
        work = work[work["_eq"] > 0]
        if len(work) < 2:
            return []
        eq = work["_eq"].to_numpy(dtype=float)
        rets = np.diff(eq) / eq[:-1]
        if ts_col is not None:
            ts_arr = work["_ts"].to_numpy(dtype=float)
            dt_h = np.clip(np.diff(ts_arr) / 3_600_000.0, 0.5, 48.0)
            rets = rets * (4.0 / dt_h)
        return [float(r) for r in rets if math.isfinite(r)]
    except Exception:
        return []


def collect_desk_sleeve_metrics(data_root: Path, *, default_hlp_apr_pct: float = 12.0) -> dict[str, dict[str, Any]]:
    """Read live state, equity, leverage, and return history for every desk sleeve under ``data_root``."""
    out: dict[str, dict[str, Any]] = {}

    # 1. Spot + Perp (+ 10% HLP)
    sp_st = _read_json(data_root / "hl_spot_perp_paper" / "state.json")
    sp_rets = _read_sleeve_tick_returns(data_root / "hl_spot_perp_paper" / "ticks.parquet")
    if sp_st:
        rt = sp_st.get("risk_telemetry") or {}
        sp_eq = max(_f(sp_st.get("equity_usd"), 100_000.0), 1.0)
        sp_free = max(0.0, _f(sp_st.get("free_cash_usd"), sp_eq if not (sp_st.get("positions") or {}) else 0.0))
        out["spot_perp_paper"] = {
            "active": True,
            "equity_usd": sp_eq,
            "starting_capital_usd": _f(sp_st.get("starting_capital_usd"), 100_000.0),
            "leverage": _f(rt.get("gross_leverage"), 1.25),
            "net_exposure_frac": _f(rt.get("net_delta_pct")) / 100.0,
            "positions_count": len(sp_st.get("positions") or {}),
            "deployed_frac": round(min(1.0, max(0.0, 1.0 - sp_free / sp_eq)), 6),
            "venue_breakdown_usd": dict(rt.get("venue_breakdown_usd") or {}),
            "returns": sp_rets,
            "venue": "HyperCore",
        }

    # 2. Cross-DEX Paper v2 (HL x Lighter x Extended)
    cd_st = _read_json(data_root / "hl_cross_dex_paper" / "state.json")
    cd_rets = _read_sleeve_tick_returns(data_root / "hl_cross_dex_paper" / "ticks.parquet")
    if cd_st:
        cd_pos = cd_st.get("positions") or {}
        cd_eq = max(_f(cd_st.get("equity_usd"), 100_000.0), 1.0)
        cd_gross = sum(2.0 * _f(p.get("leg_notional_usd")) for p in cd_pos.values() if isinstance(p, dict))
        cd_locked = sum(
            _f(p.get("margin_locked_usd"))
            if p.get("margin_locked_usd") is not None
            else (2.0 * _f(p.get("leg_notional_usd"))) / max(_f(p.get("per_leg_leverage"), 2.0), 1.0)
            for p in cd_pos.values()
            if isinstance(p, dict)
        )
        out["cross_dex_paper"] = {
            "active": True,
            "equity_usd": cd_eq,
            "starting_capital_usd": _f(cd_st.get("starting_capital_usd"), 100_000.0),
            "leverage": round(cd_gross / cd_eq, 3),
            "positions_count": len(cd_pos),
            "deployed_frac": round(min(1.0, max(0.0, cd_locked / cd_eq)), 6),
            "returns": cd_rets,
            "venue": "HL x Lighter/Extended",
        }

    # 3. HIP-3 Cross-Builder Split-Margin Paper
    h3_st = _read_json(data_root / "hl_hip3_paper" / "state.json")
    h3_rets = _read_sleeve_tick_returns(data_root / "hl_hip3_paper" / "ticks.parquet")
    if h3_st:
        h3_pos = h3_st.get("positions") or {}
        h3_eq = max(_f(h3_st.get("equity_usd"), 50_000.0), 1.0)
        h3_gross = sum(2.0 * _f(p.get("leg_notional_usd")) for p in h3_pos.values() if isinstance(p, dict))
        h3_locked = sum(_f(p.get("margin_locked_usd")) for p in h3_pos.values() if isinstance(p, dict))
        out["hip3_paper"] = {
            "active": True,
            "equity_usd": h3_eq,
            "starting_capital_usd": _f(h3_st.get("starting_capital_usd"), 50_000.0),
            "leverage": round(h3_gross / h3_eq, 3),
            "positions_count": len(h3_pos),
            "deployed_frac": round(min(1.0, max(0.0, h3_locked / h3_eq)), 6),
            "returns": h3_rets,
            "venue": "HIP-3 Builders",
        }

    # 4. Metamodel Paper (Observation-only unless positive live OOS Sharpe)
    mm_st = _read_json(data_root / "hl_metamodel" / "state.json")
    mm_rets = _read_sleeve_tick_returns(data_root / "hl_metamodel" / "ticks.parquet")
    if mm_st:
        mm_pos = mm_st.get("positions") or {}
        mm_lev = sum(_metamodel_position_gross(p) for p in mm_pos.values() if isinstance(p, dict))
        out["metamodel_paper"] = {
            "active": True,
            "equity_usd": _f(mm_st.get("equity_usd"), 100_000.0),
            "starting_capital_usd": _f(mm_st.get("starting_capital_usd"), 100_000.0),
            "leverage": round(mm_lev, 3),
            "net_exposure_frac": round(
                sum(_f(p.get("hl_weight")) + _f(p.get("cex_weight")) for p in mm_pos.values() if isinstance(p, dict)), 6
            ),
            "positions_count": len(mm_pos),
            "deployed_frac": round(min(1.0, max(0.0, mm_lev / 2.0)), 6),
            "returns": mm_rets,
            "venue": "HL x CEX",
        }

    # 5. Live HLP / Savings Reserve benchmark APR
    hlp_apr = default_hlp_apr_pct
    if sp_st and _f(sp_st.get("hlp_apr_pct")) != 0.0:
        hlp_apr = _f(sp_st.get("hlp_apr_pct"))
    else:
        fh = _read_json(data_root / "hl_frontier_history" / "latest_summary.json")
        if fh and _f(fh.get("hlp_apr_pct")) != 0.0:
            hlp_apr = _f(fh.get("hlp_apr_pct"))
    out["hlp_savings_reserve"] = {
        "active": True,
        "apr_pct": round(hlp_apr, 2),
        "leverage": 1.0,
        "deployed_frac": 1.0,
        "returns": [],
        "venue": "HyperCore HLP Vault",
    }
    return out


def _sleeve_sharpe_and_vol(rets: list[float], *, periods_per_year: float = 2190.0) -> tuple[float, float]:
    """Compute annualized Sharpe and volatility from 4h tick returns (2190 = 365 * 6)."""
    if len(rets) < 3:
        return 0.0, 0.05
    arr = np.asarray(rets, dtype=float)
    vol = float(np.std(arr, ddof=1)) * math.sqrt(periods_per_year)
    mean = float(np.mean(arr)) * periods_per_year
    if vol <= 1e-8:
        return 0.0, 0.05
    return mean / vol, max(vol, 0.01)


def compute_autopilot_target_weights(
    sleeves: dict[str, dict[str, Any]],
    *,
    cta_active: bool = True,
    cfg: AutopilotConfig = AutopilotConfig(),
) -> dict[str, float]:
    """Compute Neutral-Autopilot-style target weights honoring hard risk & diversification bounds."""
    cta_w = cfg.max_directional_weight if cta_active else 0.0
    mn_candidates = ["spot_perp_paper", "cross_dex_paper", "hip3_paper", "metamodel_paper"]

    eligible_mn: list[str] = []
    sharpe_map: dict[str, float] = {}
    vol_map: dict[str, float] = {}

    for name in mn_candidates:
        info = sleeves.get(name)
        if not info or not info.get("active"):
            continue
        rets = list(info.get("returns") or [])
        sh, vol = _sleeve_sharpe_and_vol(rets)
        sharpe_map[name] = sh
        vol_map[name] = vol
        if name == "metamodel_paper":
            # Observation-only safeguard: allocate only if proven live positive Sharpe over >= 2x opt window
            if len(rets) < max(12, cfg.min_history_ticks_for_opt) or sh < cfg.min_metamodel_sharpe:
                continue
        eligible_mn.append(name)

    if not eligible_mn:
        return {
            "spot_perp_paper": 0.0,
            "cross_dex_paper": 0.0,
            "hip3_paper": 0.0,
            "metamodel_paper": 0.0,
            "hlp_savings_reserve": round(1.0 - cta_w, 4),
            "crisis_cta": round(cta_w, 4),
        }

    # Reserve share: at least target_reserve_weight (and never below min_reserve_weight),
    # growing automatically if few market-neutral sleeves are active so single-sleeve cap holds.
    max_mn_total = min(1.0 - cta_w - cfg.target_reserve_weight, len(eligible_mn) * cfg.max_single_sleeve_weight)
    mn_budget = max(0.0, max_mn_total)
    reserve_w = max(cfg.min_reserve_weight, 1.0 - cta_w - mn_budget)

    # Check if >= 2 sleeves have enough live tick history for shrinkage risk-parity optimization
    with_hist = [n for n in eligible_mn if len(sleeves[n].get("returns") or []) >= cfg.min_history_ticks_for_opt]
    raw: dict[str, float] = {}
    if len(with_hist) >= 2:
        for n in eligible_mn:
            prior = max(DEFAULT_BASE_WEIGHTS.get(n, 0.15), 0.05)
            pos_cnt = int(sleeves[n].get("positions_count", 0) or 0)
            if n in with_hist and pos_cnt > 0:
                inv_vol = min(1.0 / max(vol_map[n], 0.02), 25.0)
                # Positive Sharpe multiplier (0.25 floor if slightly negative so diversification survives noise)
                tilt = max(0.25, min(2.5, 1.0 + 0.25 * sharpe_map[n]))
                raw[n] = 0.5 * prior + 0.5 * (inv_vol * tilt / 20.0)
            else:
                raw[n] = prior
    else:
        for n in eligible_mn:
            raw[n] = max(DEFAULT_BASE_WEIGHTS.get(n, 0.15), 0.05)

    # Normalize & iteratively clip to max_single_sleeve_weight
    tot = sum(raw.values()) or 1.0
    mn_weights = {n: raw[n] / tot * mn_budget for n in eligible_mn}
    for _ in range(10):
        over = [n for n, w in mn_weights.items() if w > cfg.max_single_sleeve_weight + 1e-9]
        if not over:
            break
        excess = sum(mn_weights[n] - cfg.max_single_sleeve_weight for n in over)
        for n in over:
            mn_weights[n] = cfg.max_single_sleeve_weight
        under = [n for n in mn_weights if n not in over]
        if not under:
            reserve_w += excess
            break
        u_sum = sum(mn_weights[n] for n in under) or float(len(under))
        for n in under:
            share = (mn_weights[n] / u_sum) if sum(mn_weights[x] for x in under) > 0 else (1.0 / len(under))
            mn_weights[n] += excess * share

    final = {
        "spot_perp_paper": 0.0,
        "cross_dex_paper": 0.0,
        "hip3_paper": 0.0,
        "metamodel_paper": 0.0,
        "hlp_savings_reserve": reserve_w,
        "crisis_cta": cta_w,
    }
    for n, w in mn_weights.items():
        final[n] = w

    # Clean final sum == 1.0
    s_tot = sum(final.values()) or 1.0
    final = {k: round(v / s_tot, 4) for k, v in final.items()}
    # Fix tiny rounding residual on reserve
    diff = round(1.0 - sum(final.values()), 4)
    final["hlp_savings_reserve"] = round(final["hlp_savings_reserve"] + diff, 4)
    return final


# --------------------------------------------------------------------------- Master Tick Engine


def _fee_period_key(ts_ms: int, mode: str) -> str:
    d = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc)
    if mode == "month":
        return f"{d.year}-{d.month:02d}"
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def _load_or_init_autopilot_state(state_path: Path, cfg: AutopilotConfig) -> dict[str, Any]:
    if state_path.is_file():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st.setdefault("sleeve_allocations_usd", {})
        st.setdefault("last_sleeve_equities", {})
        st.setdefault("cta_state", {"weights": {}, "last_closes": {}, "cum_pnl_usd": 0.0, "cum_cost_usd": 0.0})
        st.setdefault("rebalances_count", 0)
        st.setdefault("nav_guard_triggers", 0)
        return st
    return {
        "starting_capital_usd": cfg.starting_capital_usd,
        "gross_equity_usd": cfg.starting_capital_usd,
        "net_equity_usd": cfg.starting_capital_usd,
        "gross_share_price": 1.0,
        "net_share_price": 1.0,
        "hwm_net_share_price": 1.0,
        "gross_return_pct": 0.0,
        "net_return_pct": 0.0,
        "cum_mgmt_fee_usd": 0.0,
        "cum_perf_fee_usd": 0.0,
        "cum_rebalance_cost_usd": 0.0,
        "last_tick_ms": 0,
        "sleeve_allocations_usd": {},
        "last_sleeve_equities": {},
        "cta_state": {
            "weights": {},
            "last_closes": {},
            "cum_pnl_usd": 0.0,
            "cum_cost_usd": 0.0,
        },
        "rebalances_count": 0,
        "nav_guard_triggers": 0,
    }


def run_autopilot_tick(
    client: httpx.Client | None = None,
    *,
    data_root: Path,
    state_dir: Path | None = None,
    cfg: AutopilotConfig = AutopilotConfig(),
    now_ms: int | None = None,
    daily_closes: dict[str, list[float]] | None = None,
    info_url: str = HL_INFO_URL,
) -> dict[str, Any]:
    """Run one idempotent Neutral-Autopilot multi-strategy portfolio tick with deadband rebalancing."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    obs_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out_dir = state_dir or (data_root / "hl_autopilot")
    out_dir.mkdir(parents=True, exist_ok=True)
    state_path = out_dir / "state.json"
    ticks_path = out_dir / "ticks.parquet"
    rebal_path = out_dir / "rebalances.parquet"

    state = _load_or_init_autopilot_state(state_path, cfg)
    last_ms = int(state.get("last_tick_ms", 0) or 0)
    if last_ms > 0 and cfg.min_tick_spacing_min > 0 and 0 <= ts_ms - last_ms < cfg.min_tick_spacing_min * 60_000:
        return {"state": state, "status": "duplicate_tick_skipped"}
    sleeves = collect_desk_sleeve_metrics(data_root, default_hlp_apr_pct=cfg.default_hlp_apr_pct)

    own_client = client is None and daily_closes is None
    http_client = client or (httpx.Client(timeout=20.0) if own_client else None)
    live_marks: dict[str, float] = {}
    try:
        if daily_closes is None:
            assert http_client is not None
            # Fetch >=370 completed UTC days so `hl_benchmarks/daily_closes.json` covers full YTD & 1Y,
            # while CTA TSMOM slices the trailing 60 completed daily closes below.
            dated_bars = fetch_hypercore_daily_closes_with_dates(
                http_client, CTA_UNIVERSE, lookback_days=370, now_ms=ts_ms, info_url=info_url
            )
            if dated_bars:
                bench_path = data_root / "hl_benchmarks" / "daily_closes.json"
                existing_bench_doc = _read_json(bench_path) or {}
                existing_benchmarks = (
                    existing_bench_doc.get("benchmarks")
                    if isinstance(existing_bench_doc.get("benchmarks"), dict)
                    else {}
                )
                merged_benchmarks = {**existing_benchmarks, **dated_bars}
                _write_json_atomic(
                    bench_path,
                    {"updated_at_utc": obs_utc, "benchmarks": merged_benchmarks},
                )
            closes_map = {c: [float(p["close"]) for p in pts[-60:]] for c, pts in dated_bars.items() if pts}
            try:
                m_resp = _post_info_with_retry(http_client, info_url, {"type": "allMids"})
                if m_resp is not None and m_resp.status_code == 200 and isinstance(m_resp.json(), dict):
                    for k, v in m_resp.json().items():
                        px = _f(v)
                        if px > 0:
                            live_marks[str(k)] = px
            except Exception:
                pass
        else:
            closes_map = dict(daily_closes)
    finally:
        if own_client and http_client is not None:
            http_client.close()

    prev_ms = int(state.get("last_tick_ms", 0))
    dt_hours = max(0.0, min(cfg.max_accrual_hours, (ts_ms - prev_ms) / 3_600_000.0)) if prev_ms > 0 else 0.0
    # Time-based accruals (HLP reserve yield, CTA perp carry) skipped by a NAV-guard clamp are
    # carried forward and applied on the next accepted tick instead of being lost.
    accrual_hours = min(cfg.max_accrual_hours, dt_hours + max(0.0, _f(state.get("nav_guard_deferred_hours"))))
    prev_gross = float(state.get("gross_equity_usd", cfg.starting_capital_usd))
    prev_net = float(state.get("net_equity_usd", cfg.starting_capital_usd))
    start_cap = float(state.get("starting_capital_usd", cfg.starting_capital_usd))

    # 1. Update Crisis-Alpha CTA sleeve MTM & target weights
    cta_state = dict(state.get("cta_state") or {})
    prev_cta_w: dict[str, float] = dict(cta_state.get("weights") or {})
    prev_cta_px: dict[str, float] = dict(cta_state.get("last_closes") or {})

    cta_mark_map = {
        c: float(live_marks.get(c) or vals[-1])
        for c, vals in closes_map.items()
        if vals
    }
    for coin, w in prev_cta_w.items():
        if abs(float(w)) > 1e-9 and coin not in cta_mark_map and _f(live_marks.get(coin)) > 0:
            cta_mark_map[coin] = float(live_marks[coin])

    cta_tick_ret = 0.0
    if prev_cta_w and prev_ms > 0:
        for coin, w in prev_cta_w.items():
            p1 = cta_mark_map.get(coin, 0.0)
            p0 = _f(prev_cta_px.get(coin))
            if p1 > 0 and p0 > 0:
                cta_tick_ret += w * ((p1 - p0) / p0)

    cta_snap = compute_crisis_cta_weights(closes_map, prev_weights=prev_cta_w, cfg=cfg)
    cta_turnover_cost_ret = cta_snap["turnover"] * (cfg.cta_turnover_cost_bps / 1e4)
    # Perp carry on the exposure held over the elapsed interval (previous weights)
    prev_cta_gross = sum(abs(float(w)) for w in prev_cta_w.values())
    cta_funding_ret = prev_cta_gross * (cfg.cta_funding_cost_apr_pct / 100.0 / HOURS_PER_YEAR) * accrual_hours
    cta_cost_ret = cta_turnover_cost_ret + cta_funding_ret
    cta_net_ret = cta_tick_ret - cta_cost_ret
    new_last_closes = dict(cta_mark_map)
    for coin, w in prev_cta_w.items():
        if abs(float(w)) > 1e-9 and coin not in new_last_closes:
            preserved_px = _f(live_marks.get(coin)) or _f(prev_cta_px.get(coin))
            if preserved_px > 0:
                new_last_closes[coin] = preserved_px
    for coin, a_det in cta_snap["assets"].items():
        sig_close = _f(a_det.get("last_close")) or _f(prev_cta_px.get(coin)) or _f(new_last_closes.get(coin))
        if _f(a_det.get("last_close")) <= 0 and sig_close > 0:
            a_det["last_close"] = round(sig_close, 6)
        mark_val = _f(cta_mark_map.get(coin)) or _f(new_last_closes.get(coin)) or sig_close
        a_det["mark_px"] = round(mark_val, 6)

    # 2. Compute target master weights
    target_weights = compute_autopilot_target_weights(
        sleeves,
        cta_active=bool(cta_snap["weights"]),
        cfg=cfg,
    )

    alloc_usd: dict[str, float] = dict(state.get("sleeve_allocations_usd") or {})
    last_eq_map: dict[str, float] = dict(state.get("last_sleeve_equities") or {})
    is_initial = prev_ms == 0 or not alloc_usd

    sleeve_tick_pnl: dict[str, float] = {}
    nav_guard_triggered = False
    nav_guard_reason = ""

    if is_initial:
        alloc_usd = {k: round(prev_gross * w, 2) for k, w in target_weights.items()}
        # Opening the CTA book is not free: charge its entry turnover cost on the bootstrap tick.
        cta_entry_cost = round(alloc_usd.get("crisis_cta", 0.0) * cta_turnover_cost_ret, 4)
        if cta_entry_cost > 0:
            alloc_usd["crisis_cta"] = round(alloc_usd["crisis_cta"] - cta_entry_cost, 4)
            cta_state["cum_cost_usd"] = round(float(cta_state.get("cum_cost_usd", 0.0)) + cta_entry_cost, 4)
        for name in ("spot_perp_paper", "cross_dex_paper", "hip3_paper", "metamodel_paper"):
            if name in sleeves:
                last_eq_map[name] = float(sleeves[name]["equity_usd"])
    else:
        # Accrue sleeve-level returns onto existing allocations
        raw_pnl_total = 0.0
        provisional_alloc = dict(alloc_usd)
        provisional_last_eq_map = dict(last_eq_map)
        for name, cur_alloc in alloc_usd.items():
            if cur_alloc <= 0:
                sleeve_tick_pnl[name] = 0.0
                continue
            r = 0.0
            if name == "hlp_savings_reserve":
                hlp_apr = _f(sleeves["hlp_savings_reserve"].get("apr_pct"), cfg.default_hlp_apr_pct)
                r = (hlp_apr / 100.0 / HOURS_PER_YEAR) * accrual_hours
            elif name == "crisis_cta":
                r = cta_net_ret
            elif name in sleeves:
                cur_eq = float(sleeves[name]["equity_usd"])
                prev_s_eq = _f(last_eq_map.get(name), cur_eq)
                r = (cur_eq - prev_s_eq) / prev_s_eq if prev_s_eq > 0 else 0.0
                provisional_last_eq_map[name] = cur_eq
            pnl = cur_alloc * r
            sleeve_tick_pnl[name] = round(pnl, 4)
            provisional_alloc[name] = max(0.0, cur_alloc + pnl)
            raw_pnl_total += pnl

        # Keep equity baselines fresh for zero-allocated sleeves too, so a later allocation
        # into them does not credit Autopilot with returns earned before it held the sleeve.
        for name in ("spot_perp_paper", "cross_dex_paper", "hip3_paper", "metamodel_paper"):
            if name in sleeves and _f(alloc_usd.get(name)) <= 0:
                provisional_last_eq_map[name] = float(sleeves[name]["equity_usd"])
        # Bounded NAV Sanity Guard: clamp a jump once; if it persists on a tick >= 1h later, accept it.
        # Baselines (sleeve equities, CTA closes) are not advanced on a clamp, so the accepting tick
        # credits the full move; without confirmation a genuine jump froze NAV and rebalancing forever.
        pending_raw = state.get("nav_guard_pending_ms")
        nav_guard_pending: int | None = int(pending_raw) if pending_raw else None
        jump_exceeded = prev_gross > 0 and abs(raw_pnl_total) / prev_gross * 100.0 > cfg.max_tick_nav_jump_pct
        if jump_exceeded and nav_guard_pending is not None and ts_ms - nav_guard_pending >= NAV_GUARD_CONFIRM_MS:
            nav_guard_reason = (
                f"Master NAV jump {raw_pnl_total / prev_gross * 100.0:+.2f}% persisted since the clamped tick; "
                "accepted as genuine"
            )
            jump_exceeded = False
            nav_guard_pending = None
        elif not jump_exceeded:
            nav_guard_pending = None
        state["nav_guard_pending_ms"] = (
            (nav_guard_pending if nav_guard_pending is not None else ts_ms) if jump_exceeded else None
        )
        state["nav_guard_deferred_hours"] = round(accrual_hours, 6) if jump_exceeded else 0.0
        if jump_exceeded:
            nav_guard_triggered = True
            nav_guard_reason = (
                f"Master NAV jump {raw_pnl_total / prev_gross * 100.0:+.2f}% exceeded "
                f"+/-{cfg.max_tick_nav_jump_pct:.1f}% guard; skipping MTM & rebalance"
            )
            state["nav_guard_triggers"] = int(state.get("nav_guard_triggers", 0)) + 1
            sleeve_tick_pnl = {k: 0.0 for k in alloc_usd}
        else:
            alloc_usd = provisional_alloc
            last_eq_map = provisional_last_eq_map
            cta_alloc_prev = float(state.get("sleeve_allocations_usd", {}).get("crisis_cta", 0.0))
            cta_state["cum_pnl_usd"] = round(float(cta_state.get("cum_pnl_usd", 0.0)) + cta_alloc_prev * cta_tick_ret, 4)
            cta_state["cum_cost_usd"] = round(float(cta_state.get("cum_cost_usd", 0.0)) + cta_alloc_prev * cta_cost_ret, 4)

    # 3. Deadband Rebalance check
    gross_before_rebal = sum(alloc_usd.values())
    actual_weights = {
        k: round(alloc_usd.get(k, 0.0) / max(gross_before_rebal, 1.0), 4) for k in target_weights
    }
    max_drift_pct = round(
        max((abs(actual_weights.get(k, 0.0) - target_weights[k]) * 100.0 for k in target_weights), default=0.0), 3
    )

    hard_breach = (
        actual_weights.get("crisis_cta", 0.0) > cfg.max_directional_weight + 0.005
        or actual_weights.get("hlp_savings_reserve", 0.0) < cfg.min_reserve_weight - 0.02
    )
    should_rebalance = (not nav_guard_triggered) and (
        is_initial or max_drift_pct >= cfg.rebalance_deadband_pct or hard_breach
    )

    rebal_cost_usd = 0.0
    rebal_turnover = 0.0
    rebal_reason = ""
    if should_rebalance:
        rebal_turnover = 0.5 * sum(abs(target_weights[k] - actual_weights.get(k, 0.0)) for k in target_weights)
        if is_initial:
            # Bootstrap = 100% turnover into the trading sleeves (sized off full initial gross capital).
            # HLP is a vault deposit; the CTA entry cost was already charged above.
            fundable = sum(
                prev_gross * w
                for k, w in target_weights.items()
                if k not in ("hlp_savings_reserve", "crisis_cta")
            )
            rebal_cost_usd = round(fundable * (cfg.rebalance_cost_bps / 1e4), 4)
        else:
            rebal_cost_usd = round(gross_before_rebal * rebal_turnover * (cfg.rebalance_cost_bps / 1e4), 4)
        post_eq = max(gross_before_rebal - rebal_cost_usd, 1.0)
        alloc_usd = {k: round(post_eq * w, 2) for k, w in target_weights.items()}
        actual_weights = dict(target_weights)
        if not is_initial:
            state["rebalances_count"] = int(state.get("rebalances_count", 0)) + 1
            rebal_reason = "hard_limit_breach" if hard_breach else f"deadband_drift({max_drift_pct:.2f}%)"
            _append_parquet(
                rebal_path,
                [
                    {
                        "tick_ms": ts_ms,
                        "observed_at_utc": obs_utc,
                        "reason": rebal_reason,
                        "max_drift_pct": max_drift_pct,
                        "turnover_pct": round(rebal_turnover * 100.0, 2),
                        "cost_usd": rebal_cost_usd,
                        "gross_equity_usd": round(post_eq, 2),
                    }
                ],
            )

    gross_equity = round(sum(alloc_usd.values()), 2)
    gross_share = round(gross_equity / start_cap, 6) if start_cap > 0 else 1.0
    gross_ret_pct = round((gross_share - 1.0) * 100.0, 4)
    # ``max_drift_pct`` above is the PRE-rebalance drift that drove the decision; telemetry and the
    # HLAutopilotRiskLimitBreach alert must see the drift that remains AFTER this tick's rebalance.
    pre_rebalance_drift_pct = max_drift_pct
    post_drift_pct = round(
        max((abs(actual_weights.get(k, 0.0) - target_weights[k]) * 100.0 for k in target_weights), default=0.0), 3
    )

    # 4. Gross vs Net-of-Fee HWM Share Price Accounting
    # ``pre_perf_net`` = net NAV after management fees but before the (accrued) performance fee.
    # The perf fee accrues as a reversible liability each tick and is crystallized (paid, HWM reset)
    # only at the first tick of a new fee period, using the previous period-end accrual.
    tick_gross_ret = (gross_equity / prev_gross - 1.0) if prev_gross > 0 else 0.0
    hwm_share = float(state.get("hwm_net_share_price", 1.0))
    prev_accrued = float(state.get("accrued_perf_fee_usd", 0.0))
    prev_pre_perf = float(state.get("pre_perf_net_equity_usd", prev_net + prev_accrued))
    period_key = _fee_period_key(ts_ms, cfg.perf_fee_crystallization)
    prev_period = str(state.get("perf_fee_period") or period_key)
    perf_fee_usd = 0.0  # crystallized this tick
    if prev_period != period_key and prev_accrued > 0 and start_cap > 0:
        perf_fee_usd = round(prev_accrued, 4)
        prev_pre_perf = prev_pre_perf - perf_fee_usd
        hwm_share = max(hwm_share, round(prev_pre_perf / start_cap, 6))

    pre_fee = prev_pre_perf * (1.0 + tick_gross_ret)
    mgmt_fee_usd = round(pre_fee * (cfg.mgmt_fee_annual_pct / 100.0 / HOURS_PER_YEAR) * dt_hours, 4) if dt_hours > 0 else 0.0
    pre_perf_net = max(pre_fee - mgmt_fee_usd, 1.0)

    accrued_perf_fee_usd = 0.0
    if cfg.perf_fee_pct > 0 and start_cap > 0:
        cand_share = pre_perf_net / start_cap
        if cand_share > hwm_share:
            accrued_perf_fee_usd = round((cand_share - hwm_share) * start_cap * (cfg.perf_fee_pct / 100.0), 4)
    net_equity = round(pre_perf_net - accrued_perf_fee_usd, 2)

    net_share = round(net_equity / start_cap, 6) if start_cap > 0 else 1.0
    net_ret_pct = round((net_share - 1.0) * 100.0, 4)

    # 5. Consolidated Risk Telemetry (weighted leverage, 30D avg/max, look-through venue breakdown)
    weighted_lev = 0.0
    venue_breakdown: dict[str, float] = {}

    def _add_venue(label: str, amount_usd: float) -> None:
        if amount_usd <= 0:
            return
        canon = "Cash / USDC" if label in ("Free USDC Reserve", "Free USDC Buffer") else label
        venue_breakdown[canon] = round(venue_breakdown.get(canon, 0.0) + amount_usd, 2)

    for k, usd in alloc_usd.items():
        if usd <= 0:
            continue
        w = usd / max(gross_equity, 1.0)
        if k == "crisis_cta":
            lev_k = float(cta_snap["gross_leverage"])
            dep_frac = min(1.0, max(0.0, lev_k))
            _add_venue("HyperCore Perp (CTA)", usd * dep_frac)
            _add_venue("Cash / USDC", usd * (1.0 - dep_frac))
        elif k in sleeves:
            s_info = sleeves[k]
            lev_k = float(s_info.get("leverage", 1.0))
            venue_k = str(s_info.get("venue", "HyperCore"))
            sub_venues = s_info.get("venue_breakdown_usd")
            s_eq = _f(s_info.get("equity_usd"))
            if k == "spot_perp_paper" and isinstance(sub_venues, dict) and sub_venues and s_eq > 0:
                scale = usd / s_eq
                used = 0.0
                for sv_lbl, sv_amt in sub_venues.items():
                    part = max(0.0, _f(sv_amt)) * scale
                    used += part
                    _add_venue(str(sv_lbl), part)
                _add_venue("Cash / USDC", usd - used)
            else:
                dep_frac = min(1.0, max(0.0, _f(s_info.get("deployed_frac"), 1.0)))
                _add_venue(venue_k, usd * dep_frac)
                _add_venue("Cash / USDC", usd * (1.0 - dep_frac))
        else:
            lev_k = 1.0
            _add_venue("HyperCore", usd)
        weighted_lev += w * lev_k

    weighted_lev = round(weighted_lev, 3)
    lev_30d_avg, lev_30d_max = spa._compute_30d_leverage_stats(ticks_path, weighted_lev, ts_ms)
    cta_dir_pct = round(actual_weights.get("crisis_cta", 0.0) * float(cta_snap["net_exposure"]) * 100.0, 3)
    # Residual delta of the "neutral" sleeves (e.g. spot_perp spot-vs-perp drift) is directional too.
    sleeve_dir_pct = sum(
        actual_weights.get(k, 0.0) * _f(sleeves[k].get("net_exposure_frac"))
        for k in sleeves
        if k not in ("crisis_cta", "hlp_savings_reserve")
    ) * 100.0
    net_dir_pct = round(cta_dir_pct + sleeve_dir_pct, 3)

    if not nav_guard_triggered:
        cta_state["weights"] = cta_snap["weights"]
        cta_state["last_closes"] = new_last_closes
        cta_state["gross_leverage"] = cta_snap["gross_leverage"]
        cta_state["net_exposure"] = cta_snap["net_exposure"]
        cta_state["assets"] = cta_snap["assets"]

    sleeve_summary = []
    for k in target_weights:
        sleeve_summary.append(
            {
                "name": k,
                "category": SLEEVE_CATEGORY.get(k, "Other"),
                "target_weight_pct": round(target_weights[k] * 100.0, 2),
                "actual_weight_pct": round(actual_weights.get(k, 0.0) * 100.0, 2),
                "drift_pct": round((actual_weights.get(k, 0.0) - target_weights[k]) * 100.0, 2),
                "allocation_usd": round(alloc_usd.get(k, 0.0), 2),
                "tick_pnl_usd": round(sleeve_tick_pnl.get(k, 0.0), 4),
            }
        )

    state.update(
        {
            "gross_equity_usd": gross_equity,
            "equity_usd": gross_equity,  # Standard key for downstream analytics readers
            "net_equity_usd": net_equity,
            "gross_share_price": gross_share,
            "net_share_price": net_share,
            "hwm_net_share_price": hwm_share,
            "pre_perf_net_equity_usd": round(pre_perf_net, 4),
            "accrued_perf_fee_usd": accrued_perf_fee_usd,
            "perf_fee_period": period_key,
            "perf_fee_crystallization": cfg.perf_fee_crystallization,
            "gross_return_pct": gross_ret_pct,
            "net_return_pct": net_ret_pct,
            "cum_mgmt_fee_usd": round(float(state.get("cum_mgmt_fee_usd", 0.0)) + mgmt_fee_usd, 4),
            "cum_perf_fee_usd": round(float(state.get("cum_perf_fee_usd", 0.0)) + perf_fee_usd, 4),
            "cum_rebalance_cost_usd": round(float(state.get("cum_rebalance_cost_usd", 0.0)) + rebal_cost_usd, 4),
            "last_tick_ms": ts_ms,
            "observed_at_utc": obs_utc,
            "target_weights": target_weights,
            "actual_weights": actual_weights,
            "sleeve_allocations_usd": alloc_usd,
            "last_sleeve_equities": last_eq_map,
            "sleeves": sleeve_summary,
            "cta_state": cta_state,
            "risk_telemetry": {
                "gross_leverage": weighted_lev,
                "leverage_30d_avg": lev_30d_avg,
                "leverage_30d_max": lev_30d_max,
                "net_directional_exposure_pct": net_dir_pct,
                "cta_directional_exposure_pct": cta_dir_pct,
                "directional_sleeve_pct": round(actual_weights.get("crisis_cta", 0.0) * 100.0, 2),
                "reserve_sleeve_pct": round(actual_weights.get("hlp_savings_reserve", 0.0) * 100.0, 2),
                "max_weight_drift_pct": post_drift_pct,
                "pre_rebalance_max_drift_pct": pre_rebalance_drift_pct,
                "rebalanced_this_tick": bool(should_rebalance and not is_initial),
                "rebalance_reason": rebal_reason,
                "venue_breakdown_usd": venue_breakdown,
                "nav_guard_triggered": nav_guard_triggered,
                "nav_guard_reason": nav_guard_reason,
            },
        }
    )

    # Append the tick row BEFORE persisting state: if anything fails in between, the retry re-runs
    # the same interval from the old state instead of leaving state ahead of ticks.parquet.
    _append_parquet(
        ticks_path,
        [
            {
                "tick_ms": ts_ms,
                "observed_at_utc": obs_utc,
                "equity_usd": gross_equity,
                "net_equity_usd": net_equity,
                "gross_share_price": gross_share,
                "net_share_price": net_share,
                "gross_return_pct": gross_ret_pct,
                "net_return_pct": net_ret_pct,
                "gross_leverage": weighted_lev,
                "net_directional_exposure_pct": net_dir_pct,
                "cta_directional_exposure_pct": cta_dir_pct,
                "max_weight_drift_pct": post_drift_pct,
                "pre_rebalance_max_drift_pct": pre_rebalance_drift_pct,
                "rebalanced": bool(should_rebalance and not is_initial),
                "rebalance_cost_usd": rebal_cost_usd,
                "nav_guard_triggered": nav_guard_triggered,
            }
        ],
    )
    _write_json_atomic(state_path, state)
    return {"state": state}


def format_autopilot_markdown(result: dict[str, Any]) -> str:
    st = result["state"]
    rt = st.get("risk_telemetry") or {}
    lines = [
        f"# Hyperliquid Neutral Autopilot — Master Portfolio ({st.get('observed_at_utc', '')})",
        "",
        f"- **Gross Equity:** `${st['gross_equity_usd']:,.2f}` (`{st['gross_return_pct']:+.4f}%` · Share `${st['gross_share_price']:.4f}`)",
        f"- **Net Equity (1% mgmt / 10% HWM perf):** `${st['net_equity_usd']:,.2f}` (`{st['net_return_pct']:+.4f}%` · Share `${st['net_share_price']:.4f}`)",
        f"- **Risk:** Leverage `{rt.get('gross_leverage', 0):.2f}x` (30D avg `{rt.get('leverage_30d_avg', 0):.2f}x` / max `{rt.get('leverage_30d_max', 0):.2f}x`) · "
        f"CTA Cap `{rt.get('directional_sleeve_pct', 0):.2f}%` (Net Dir `{rt.get('net_directional_exposure_pct', 0):+.2f}%`) · "
        f"Reserve `{rt.get('reserve_sleeve_pct', 0):.2f}%` · Max Drift `{rt.get('max_weight_drift_pct', 0):.2f}%`",
        "",
        "| Sleeve | Category | Target % | Actual % | Drift % | Allocation | Tick PnL |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for s in st.get("sleeves") or []:
        lines.append(
            f"| **{s['name']}** | {s['category']} | {s['target_weight_pct']:.2f}% | {s['actual_weight_pct']:.2f}% | "
            f"{s['drift_pct']:+.2f}% | ${s['allocation_usd']:,.2f} | ${s['tick_pnl_usd']:+.2f} |"
        )
    cta_assets = (st.get("cta_state") or {}).get("assets") or {}
    if cta_assets:
        lines += [
            "",
            "## Crisis-Alpha Directional CTA Sleeve (TSMOM 10d/20d/40d, <= 5% Cap)",
            "",
            "| Asset | Close | TSMOM Signal | Ann Vol | Weight |",
            "|---|---:|---:|---:|---:|",
        ]
        for c, a in sorted(cta_assets.items()):
            px_s = f"${a['last_close']:,.2f}" if a["last_close"] >= 10.0 else f"${a['last_close']:.4f}"
            lines.append(
                f"| **{c}** | {px_s} | {a['tsmom_signal']:+.3f} | "
                f"{a['ann_vol_pct']:.1f}% | {a['weight']:+.2f} |"
            )
    return "\n".join(lines) + "\n"


def format_autopilot_telegram_rich_html(result: dict[str, Any]) -> str:
    st = result["state"]
    rt = st.get("risk_telemetry") or {}
    return (
        f"<h1>🧭 Neutral Autopilot Master · {html.escape(str(st.get('observed_at_utc', '')))}</h1>"
        f"<p>Gross: <b>${st['gross_equity_usd']:,.2f}</b> ({st['gross_return_pct']:+.3f}%) · "
        f"Net HWM: <b>${st['net_equity_usd']:,.2f}</b> ({st['net_return_pct']:+.3f}%)</p>"
        f"<p>Lev: <b>{rt.get('gross_leverage', 0):.2f}x</b> · CTA: <b>{rt.get('directional_sleeve_pct', 0):.1f}%</b> "
        f"(dir {rt.get('net_directional_exposure_pct', 0):+.2f}%) · Reserve: <b>{rt.get('reserve_sleeve_pct', 0):.1f}%</b> · "
        f"Drift: {rt.get('max_weight_drift_pct', 0):.2f}%</p>"
    )
