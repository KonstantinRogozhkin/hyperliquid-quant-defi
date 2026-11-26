#!/usr/bin/env python3
"""Record Hyperliquid WebSocket L2 + trades and run passive ALO fill & markout analysis.

Example:
  run_hl_microstructure_study.py --coins BTC,ETH,SOL,HYPE,XMR,DOGE --duration-sec 180 --output-dir /tmp/hl-micro
"""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_microstructure as hm  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _fmt(x: float, fmt: str = ".2f") -> str:
    return format(x, fmt) if math.isfinite(x) else "—"


def _markdown_report(reports: list[hm.CoinMicrostructureReport], *, order_usd: float, timeout_sec: float) -> str:
    lines = [
        "# Микроструктура Hyperliquid: живой L2 + лента сделок, пассивные ALO-ордера и markout",
        "",
        f"Параметры симулятора: пассивный лимитный ордер `ALO` (post-only) размером **${order_usd:,.0f}** на top-of-book "
        f"(с учётом очереди `bid_usd1/ask_usd1` перед нами), таймаут **{timeout_sec:.0f}с** (при неисполнении — добор тейкером 4.5 б.п.).",
        "",
        "| Монета | L2 тиков | Сделок | Спред med/p90 | Top-1 / Top-5 | OBI5->5s | ALO Fill% | Fill (с) | Markout 5s | Markout 30s | ALO net | Taker | Экономия ALO |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in reports:
        lines.append(
            f"| **{r.coin}** | {r.l2_updates} | {r.trades_count} | "
            f"{r.median_spread_bps:.2f} / {r.p90_spread_bps:.2f} б.п. | "
            f"${r.median_top1_usd/1e3:.0f}k / ${r.median_top5_usd/1e3:.0f}k | "
            f"{r.obi5_corr_5s:+.2f} | {r.alo_fill_rate*100:.1f}% | {_fmt(r.alo_median_fill_sec, '.1f')} | "
            f"{_fmt(r.markout_5s_bps, '+.2f')} | {_fmt(r.markout_30s_bps, '+.2f')} | "
            f"{r.maker_eff_cost_bps:+.2f} | {r.taker_eff_cost_bps:+.2f} | **{r.alo_savings_bps:+.2f} б.п.** |"
        )
    return "\n".join(lines) + "\n"


@app.command()
def main(
    coins: str = typer.Option("BTC,ETH,SOL,HYPE,XMR,DOGE", help="Comma-separated HL perp names"),
    duration_sec: float = typer.Option(180.0, min=15.0, max=3600.0, help="Recording duration in seconds"),
    output_dir: Path = typer.Option(..., help="Where to save parquet ticks, summary.json and report.md"),
    order_usd: float = typer.Option(5_000.0, help="Simulated passive ALO order size in USD"),
    timeout_sec: float = typer.Option(60.0, help="Passive ALO wait timeout before taker fallback"),
    skip_record: bool = typer.Option(False, help="Re-analyze existing l2_ticks.parquet/trades.parquet in output_dir"),
) -> None:
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    if not skip_record:
        meta = asyncio.run(hm.record_hl_microstructure(coin_list, duration_sec=duration_sec, output_dir=output_dir))
        typer.echo(f"recorded={json.dumps(meta)}", err=True)

    reports = hm.analyze_recording(output_dir, order_usd=order_usd, timeout_sec=timeout_sec)
    md = _markdown_report(reports, order_usd=order_usd, timeout_sec=timeout_sec)
    (output_dir / "report.md").write_text(md)
    (output_dir / "summary.json").write_text(json.dumps([r.to_dict() for r in reports], indent=2))
    typer.echo(md)


if __name__ == "__main__":
    app()
