#!/usr/bin/env python3
"""Run live 100% read-only HyperEVM Delta-Neutral Yield Stack monitor (kHYPE + Felix/HyperLend + Boros + HLP)."""

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

from hl_quant import hl_hyperevm_yield_stack as hys  # noqa: E402
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
    perp_leverage: float = typer.Option(2.0, help="Short HYPE perp margin leverage (determines capital split & liq buffer)"),
    loop_ltv: float = typer.Option(0.50, help="Conservative CDP/lending LTV for composable kHYPE loop"),
    borrow_apr_pct: float = typer.Option(3.5, help="Assumed stablecoin borrow APR (%) on Felix/HyperLend"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_hyperevm_yield_stack"),
    report_path: Path | None = typer.Option(ROOT / "docs/reports/2026-11-26/hl_hyperevm_yield_stack_live.md"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    snap = hys.run_hyperevm_yield_stack_scan(
        perp_leverage=perp_leverage,
        loop_ltv=loop_ltv,
        assumed_stable_borrow_apr_pct=borrow_apr_pct,
    )
    md = hys.format_yield_stack_markdown(snap)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshot.json").write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(md, encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env", ROOT.parent / "ResearchLAB" / "system" / ".env")
        )
        if token and chat_id:
            try:
                rich = hys.format_yield_stack_telegram_rich_html(snap)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
