"""Read-only 4-hour Persistence Tracker for Hyperliquid 2026 Frontier Edges (HIP-3 + Pendle Boros + HyperEVM Yield Stack).

Appends timestamped snapshots into Parquet ledgers under ``data/paper/hl_frontier_history/`` and
refreshes each sub-module's ``snapshot.json`` so the offline HTML desk dashboard stays fresh:
1. **HIP-3 Cross-Builder Arbitrage** (`hip3_history.parquet`): tracks live APR persistence,
   Growth Mode status, L2 VWAP slippage, and builder ``oracle_divergence_bps`` over time.
2. **Pendle Boros Fixed Funding & Spread Markets** (`boros_history.parquet`): tracks fixed bid/ask
   APR vs floating funding across Hyperliquid and cross-exchange spread maturities.
3. **HyperEVM Composable Delta-Neutral Yield Stack** (`yield_stack_history.parquet`): tracks net
   deposit APR of `kHYPE + Short HYPE + Boros` vs passive `HLP` benchmark over time.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any

import httpx
import pandas as pd

from hl_quant import hl_boros_fixed_funding as boros_mod
from hl_quant import hl_hip3_builder_arb as hip3_mod
from hl_quant import hl_hyperevm_yield_stack as ys_mod


def _write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.parquet")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _append_parquet(path: Path, rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.read_parquet(path) if path.is_file() else pd.DataFrame()
    new_df = pd.DataFrame(rows)
    if path.is_file():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    _write_parquet_atomic(path, combined)
    return combined


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def compute_hip3_persistence_summary(
    hip3_df: pd.DataFrame,
    *,
    min_apr_pct: float = 20.0,
    max_oracle_div_bps: float = 25.0,
) -> list[dict[str, Any]]:
    """Summarize multi-tick persistence of HIP-3 cross-builder spreads (clean vs oracle-divergent)."""
    if hip3_df.empty or "base_symbol" not in hip3_df.columns:
        return []
    out: list[dict[str, Any]] = []
    for sym, grp in hip3_df.sort_values("ts_ms").groupby("base_symbol", sort=False):
        aprs = grp["spread_apr_pct"].astype(float).tolist()
        odivs = grp["oracle_divergence_bps"].astype(float).tolist()
        qual = [
            (a >= min_apr_pct and d <= max_oracle_div_bps)
            for a, d in zip(aprs, odivs, strict=False)
        ]
        # Trailing consecutive ticks meeting APR >= min_apr_pct (raw) and clean (oracle div <= cap)
        raw_streak = 0
        for a in reversed(aprs):
            if a >= min_apr_pct:
                raw_streak += 1
            else:
                break
        clean_streak = 0
        for ok in reversed(qual):
            if ok:
                clean_streak += 1
            else:
                break
        last = grp.iloc[-1]
        out.append(
            {
                "base_symbol": str(sym),
                "short_symbol": str(last["short_symbol"]),
                "long_symbol": str(last["long_symbol"]),
                "ticks_seen": int(len(grp)),
                "raw_apr_streak": raw_streak,
                "clean_streak": clean_streak,
                "latest_apr_pct": round(float(last["spread_apr_pct"]), 2),
                "median_apr_pct": round(float(grp["spread_apr_pct"].median()), 2),
                "median_oracle_div_bps": round(float(grp["oracle_divergence_bps"].median()), 2),
                "both_growth_mode": bool(last["both_growth_mode"]),
                "any_only_isolated": bool(last["any_only_isolated"]),
            }
        )
    out.sort(key=lambda r: (-r["clean_streak"], -r["raw_apr_streak"], -r["median_apr_pct"]))
    return out


def run_frontier_persistence_tick(
    *,
    data_root: Path,
    now_ms: int | None = None,
    hip3_snap: hip3_mod.Hip3ScannerSnapshot | None = None,
    boros_snap: boros_mod.BorosScannerSnapshot | None = None,
    yield_snap: ys_mod.HyperEvmYieldSnapshot | None = None,
) -> dict[str, Any]:
    """Run one read-only HIP-3 + Pendle Boros + HyperEVM Yield Stack persistence logging tick."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    hist_dir = data_root / "hl_frontier_history"
    hist_dir.mkdir(parents=True, exist_ok=True)

    with httpx.Client(timeout=25.0) as client:
        if hip3_snap is None:
            hip3_snap = hip3_mod.run_hip3_builder_scan(client)
        if boros_snap is None:
            boros_snap = boros_mod.run_boros_fixed_funding_scan(client)
        if yield_snap is None:
            yield_snap = ys_mod.run_hyperevm_yield_stack_scan(client)

    # Refresh module snapshots used by HTML desk dashboard
    _write_json_atomic(data_root / "hl_hip3_arb" / "snapshot.json", hip3_snap.to_dict())
    _write_json_atomic(data_root / "hl_boros" / "snapshot.json", boros_snap.to_dict())
    _write_json_atomic(data_root / "hl_hyperevm_yield_stack" / "snapshot.json", yield_snap.to_dict())

    hip3_rows = [
        {
            "ts_ms": ts_ms,
            "ny_session": hip3_snap.ny_session,
            "base_symbol": o.base_symbol,
            "short_symbol": o.short_symbol,
            "long_symbol": o.long_symbol,
            "spread_apr_pct": o.spread_apr_pct,
            "both_growth_mode": o.both_growth_mode,
            "oracle_divergence_bps": o.oracle_divergence_bps,
            "oracle_risk_warning": o.oracle_risk_warning,
            "exec_basis_5k_bps": o.exec_basis_5k_bps,
            "l2_slip_5k_bps": o.l2_slip_5k_bps,
            "payback_hours_5k": o.payback_hours_5k,
            "payback_hours_5k_taker": o.payback_hours_5k_taker,
            "any_only_isolated": o.any_only_isolated,
        }
        for o in hip3_snap.cross_builder_opps[:15]
    ]
    boros_rows = [
        {
            "ts_ms": ts_ms,
            "market_id": m.market_id,
            "name": m.name,
            "symbol": m.symbol,
            "underlying": m.underlying,
            "is_spread_market": m.is_spread_market,
            "days_to_maturity": m.days_to_maturity,
            "best_bid_apr_pct": m.best_bid_apr_pct,
            "best_ask_apr_pct": m.best_ask_apr_pct,
            "floating_apr_pct": m.floating_apr_pct,
            "fixed_minus_float_bid_pct": m.fixed_minus_float_bid_pct,
            "maturity_spread_cost_bps": m.maturity_spread_cost_bps,
        }
        for m in boros_snap.markets[:20]
    ]
    ys_rows = [
        {
            "ts_ms": ts_ms,
            "strategy": s.name,
            "rate_type": s.rate_type,
            "gross_stacked_apr_pct": s.gross_stacked_apr_pct,
            "net_deposit_apr_pct": s.net_deposit_apr_pct,
            "spread_vs_hlp_pct": s.spread_vs_hlp_pct,
            "hype_funding_apr_pct": yield_snap.inputs.hype_funding_apr_pct,
            "hlp_apr_pct": yield_snap.inputs.hlp_apr_pct,
            "boros_hype_bid_apr_pct": yield_snap.inputs.boros_hype_fixed_bid_apr_pct,
        }
        for s in yield_snap.strategies
    ]

    hip3_df = _append_parquet(hist_dir / "hip3_history.parquet", hip3_rows)
    boros_df = _append_parquet(hist_dir / "boros_history.parquet", boros_rows)
    ys_df = _append_parquet(hist_dir / "yield_stack_history.parquet", ys_rows)

    persistence = compute_hip3_persistence_summary(hip3_df)
    summary = {
        "ts_ms": ts_ms,
        "observed_at_utc": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "ny_session": hip3_snap.ny_session,
        "total_logged": {
            "hip3_rows": int(len(hip3_df)),
            "boros_rows": int(len(boros_df)),
            "yield_rows": int(len(ys_df)),
        },
        "hip3_persistence": persistence[:10],
        "top_boros_count": len(boros_rows),
        "best_yield_strategy": (
            {"name": yield_snap.strategies[0].name, "net_apr_pct": yield_snap.strategies[0].net_deposit_apr_pct}
            if yield_snap.strategies
            else None
        ),
        "hlp_apr_pct": yield_snap.inputs.hlp_apr_pct,
    }
    _write_json_atomic(hist_dir / "latest_summary.json", summary)
    return summary


def format_frontier_persistence_markdown(summary: dict[str, Any]) -> str:
    lines = [
        f"# Frontier Persistence Tracker · HIP-3 + Boros + HyperEVM Yield ({summary['observed_at_utc']})",
        "",
        f"- **Сессия NY:** `{summary['ny_session']}` · **HLP Benchmark:** `{summary['hlp_apr_pct']:+.2f}% APR`",
        f"- **Накоплено записей:** HIP-3 `{summary['total_logged']['hip3_rows']}`, Boros `{summary['total_logged']['boros_rows']}`, Yield Stack `{summary['total_logged']['yield_rows']}`",
        "",
        "## Персистентность кросс-билдер спредов HIP-3 (порог ≥ 20% APR, чистый Oracle Div ≤ 25 б.п.)",
        "",
        "| Тикер | Ноги | Тиков | Серия ≥20% | Чистая серия | Текущий APR | Медиана APR | Мед. Oracle Div | Growth |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for r in summary.get("hip3_persistence") or []:
        gm = "⚡ GM" if r["both_growth_mode"] else "Std"
        lines.append(
            f"| **{r['base_symbol']}** | `S {r['short_symbol']} / L {r['long_symbol']}` | {r['ticks_seen']} | "
            f"{r['raw_apr_streak']} | **{r['clean_streak']}** | {r['latest_apr_pct']:+.1f}% | "
            f"{r['median_apr_pct']:+.1f}% | {r['median_oracle_div_bps']:.1f} б.п. | {gm} |"
        )
    return "\n".join(lines) + "\n"
