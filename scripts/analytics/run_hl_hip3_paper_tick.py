#!/usr/bin/env python3
"""Run one read-only HIP-3 Cross-Builder Split-Margin Paper tick (xyz × io × para)."""

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

from hl_quant import hl_hip3_paper as hp  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    state_dir: Path = typer.Option(ROOT / "data/paper/hl_hip3_paper", help="HIP-3 paper ledger directory"),
    frontier_summary: Path = typer.Option(
        ROOT / "data/paper/hl_frontier_history/latest_summary.json",
        help="Optional frontier persistence summary for clean_streak continuity",
    ),
    allow_only_isolated: bool = typer.Option(True, help="Allow half-sized entries on onlyIsolated builder legs"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    cfg = hp.Hip3PaperConfig(allow_only_isolated=allow_only_isolated)
    with httpx.Client(timeout=25.0) as client:
        res = hp.run_hip3_paper_tick(
            client,
            state_dir=state_dir,
            cfg=cfg,
            frontier_summary_path=frontier_summary,
        )

    if res.get("status") == "duplicate_hour_skipped":
        typer.echo(json.dumps({"status": "duplicate_hour_skipped", "ts_ms": res["tick"]["ts_ms"]}))
        return

    md = hp.format_hip3_paper_markdown(res)
    tmp_md = state_dir / "report.tmp.md"
    tmp_md.write_text(md, encoding="utf-8")
    tmp_md.replace(state_dir / "report.md")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = hp.format_hip3_paper_telegram_rich_html(res)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
