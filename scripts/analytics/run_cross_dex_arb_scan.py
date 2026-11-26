#!/usr/bin/env python3
"""Scan live cross-DEX funding & L2 depth-weighted basis across 5 Perp DEXs.

Venues: Hyperliquid × (Lighter, Extended, dYdX v4, Paradex). Read-only public APIs, zero orders.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import sys

import httpx
import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import cross_dex_scanner as cds  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    min_day_vol_usd: float = typer.Option(500_000.0, help="Min 24h USD volume on both DEXs"),
    top_n: int = typer.Option(15, help="Top opportunities to enrich with live L2 depth VWAP ($10k / $50k)"),
    with_l2_depth: bool = typer.Option(True, "--l2-depth/--no-l2-depth", help="Fetch live L2 books for top opportunities"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_cross_dex_v2", help="Directory for parquet/markdown artifacts"),
    report_path: Path | None = typer.Option(None, help="Optional markdown report path"),
) -> None:
    with httpx.Client(timeout=15.0) as client:
        hl = cds.fetch_hyperliquid_quotes(client)
        dex_fetchers = [
            ("lighter", cds.fetch_lighter_quotes),
            ("extended", cds.fetch_extended_quotes),
            ("dydx", cds.fetch_dydx_quotes),
            ("paradex", cds.fetch_paradex_quotes),
        ]
        frames: list[pd.DataFrame] = []
        counts: dict[str, int] = {"hyperliquid": len(hl)}
        for name, fn in dex_fetchers:
            try:
                q = fn(client)
                counts[name] = len(q)
                df_d = cds.scan_cross_dex_opportunities(hl, q, min_day_vol_usd=min_day_vol_usd)
                if not df_d.empty:
                    frames.append(df_d)
            except Exception as exc:
                counts[name] = 0
                typer.echo(f"warn: {name} fetch failed: {exc}", err=True)

        combined = (
            pd.concat(frames, ignore_index=True).sort_values(by=["spread_apr_pct", "favorable_basis_bps"], ascending=[False, False]).reset_index(drop=True)
            if frames
            else pd.DataFrame()
        )
        if with_l2_depth and not combined.empty:
            top_df = cds.enrich_opportunities_with_l2_depth(client, combined, top_n=top_n)
        else:
            top_df = combined.head(top_n)

    ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Кросс-DEX арбитраж v2 с L2-глубиной: Hyperliquid × Lighter × Extended × dYdX v4 × Paradex ({ts})",
        "",
        "- Охват живых перпов: " + ", ".join(f"**{k}** `{v}`" for k, v in counts.items()) + f" · фильтр объёма `>= ${min_day_vol_usd/1e6:.1f}M/24ч` · комиссии+мост `9.0 б.п.`",
        "",
    ]
    if top_df.empty:
        lines.append("Подходящих пар по текущему фильтру объёма не найдено.")
    else:
        lines += [
            "| Монета | Направление | HL APR | DEX APR | Спред APR | L2 Exec Basis $10k | Slip $10k / $50k | $50k OK | Окуп. $10k | Объём HL / DEX |",
            "|---|---|---:|---:|---:|---:|---:|---|---:|---:|",
        ]
        for r in top_df.itertuples(index=False):
            eb = f"{r.exec_basis_10k_bps:+.1f} б.п." if hasattr(r, "exec_basis_10k_bps") and pd.notna(r.exec_basis_10k_bps) else f"{r.favorable_basis_bps:+.1f}*"
            s10 = f"{r.l2_slip_10k_bps:.1f}" if hasattr(r, "l2_slip_10k_bps") and pd.notna(r.l2_slip_10k_bps) else "—"
            s50 = f"{r.l2_slip_50k_bps:.1f}" if hasattr(r, "l2_slip_50k_bps") and pd.notna(r.l2_slip_50k_bps) else "—"
            ok50 = "🟢" if getattr(r, "depth_50k_ok", False) else "⚪"
            beh = f"{r.depth_be_10k_h:.0f}ч" if hasattr(r, "depth_be_10k_h") and pd.notna(r.depth_be_10k_h) else f"{r.breakeven_hours:.0f}ч"
            lines.append(
                f"| **{r.coin}** | {r.direction} | {r.hl_apr_pct:+.1f}% | {r.other_apr_pct:+.1f}% | **{r.spread_apr_pct:.1f}%** | "
                f"{eb} | {s10} / {s50} б.п. | {ok50} | {beh} | ${r.hl_vol_m:,.1f}M / ${r.other_vol_m:,.1f}M |"
            )

    md = "\n".join(lines) + "\n"
    output_dir.mkdir(parents=True, exist_ok=True)
    if not top_df.empty:
        top_df.to_parquet(output_dir / "opportunities.parquet", index=False)
    (output_dir / "report.md").write_text(md)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md)
    typer.echo(md)


if __name__ == "__main__":
    app()
