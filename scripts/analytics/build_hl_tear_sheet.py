#!/usr/bin/env python3
"""Build an Executive Trading Tear-Sheet from live Hyperliquid Testnet, Paper, MM, HyperEVM & Cross-DEX outputs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_tear_sheet as ts  # noqa: E402
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
    data_root: Path = typer.Option(ROOT / "data/paper", help="Root folder containing live HL module outputs"),
    report_path: Path = typer.Option(ROOT / "docs/reports/2026-11-26/hl_trading_tear_sheet.md", help="Markdown tear-sheet output"),
    telegram: bool = typer.Option(False, help="Send executive Rich HTML card to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    data = ts.collect_tear_sheet_data(data_root)
    md = ts.format_tear_sheet_markdown(data)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(md)
    (data_root / "hl_tear_sheet.json").write_text(json.dumps(data, indent=2, default=str))

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = ts.format_tear_sheet_telegram_rich_html(data)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
