#!/usr/bin/env python3
"""Run Hyperliquid On-Chain Whale Liquidation Heatmap & Hourly `:00` Funding Microstructure scan."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_liquidation_and_funding_alpha as lfa  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


@app.command()
def main(
    top_whales: int = typer.Option(45, help="Number of active top leaderboard whales to scan"),
    top_funding_coins: int = typer.Option(8, help="Number of extreme-funding coins for :00 microstructure"),
    lookback_hours: int = typer.Option(6, help="Hours of 1m candles for hourly settlement measurement"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_liq_funding_alpha", help="Directory for output artifacts"),
    report_path: Path | None = typer.Option(None, help="Optional dated report path under docs/reports/"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    snap = asyncio.run(
        lfa.run_liquidation_and_funding_alpha_scan(
            top_whales=top_whales,
            top_funding_coins=top_funding_coins,
            lookback_hours=lookback_hours,
        )
    )
    md = lfa.format_liq_funding_markdown(snap)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshot.json").write_text(json.dumps(snap.to_dict(), indent=2))
    (output_dir / "report.md").write_text(md)
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md)

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = lfa.format_liq_funding_telegram_rich_html(snap)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
