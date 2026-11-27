#!/usr/bin/env python3
"""Run one read-only HyperCore Spot + Perp Funding Arb (+ 10% HLP Reserve) paper tick."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_spot_perp_arb as spa  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    state_dir: Path = typer.Option(ROOT / "data/paper/hl_spot_perp_paper", help="Spot+Perp paper ledger directory"),
    min_clean_streak: int = typer.Option(2, help="Consecutive eligible ticks required to open a slot"),
    hlp_reserve_share: float = typer.Option(0.10, help="Target USDC equity share in HLP reserve (0.0-0.15)"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    cfg = spa.SpotPerpPaperConfig(
        min_clean_streak=min_clean_streak,
        hlp_reserve_share=hlp_reserve_share,
    )
    with httpx.Client(timeout=25.0) as client:
        res = spa.run_spot_perp_paper_tick(client, state_dir=state_dir, cfg=cfg)

    md = spa.format_spot_perp_paper_markdown(res)
    tmp_md = state_dir / "report.tmp.md"
    tmp_md.write_text(md, encoding="utf-8")
    tmp_md.replace(state_dir / "report.md")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env", ROOT.parent / "ResearchLAB" / "system" / ".env")
        )
        if token and chat_id:
            try:
                rich = spa.format_spot_perp_paper_telegram_rich_html(res)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
