#!/usr/bin/env python3
"""Run one read-only Neutral Autopilot Multi-Strategy Allocator + <=5% Crisis-CTA tick."""

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

from hl_quant import hl_autopilot as ap  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    data_root: Path = typer.Option(ROOT / "data/paper", help="Paper artifacts root directory"),
    state_dir: Path | None = typer.Option(None, help="Override autopilot output directory (defaults to <data_root>/hl_autopilot)"),
    rebalance_deadband_pct: float = typer.Option(3.0, help="Weight drift deadband (%) required before rebalancing"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    cfg = ap.AutopilotConfig(rebalance_deadband_pct=rebalance_deadband_pct)
    out_dir = state_dir or (data_root / "hl_autopilot")
    with httpx.Client(timeout=25.0) as client:
        res = ap.run_autopilot_tick(client, data_root=data_root, state_dir=out_dir, cfg=cfg)

    md = ap.format_autopilot_markdown(res)
    tmp_md = out_dir / "report.tmp.md"
    tmp_md.write_text(md, encoding="utf-8")
    tmp_md.replace(out_dir / "report.md")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env", ROOT.parent / "ResearchLAB" / "system" / ".env")
        )
        if token and chat_id:
            try:
                rich = ap.format_autopilot_telegram_rich_html(res)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
