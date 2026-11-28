#!/usr/bin/env python3
"""Run live 100% read-only Pendle Boros Fixed-Rate Funding & Spread Markets scan for Hyperliquid."""

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

from hl_quant import hl_boros_fixed_funding as boros  # noqa: E402
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


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


@app.command()
def main(
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_boros"),
    report_path: Path | None = typer.Option(ROOT / "docs/reports/2026-11-26/hl_boros_fixed_funding_live.md"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    snap = boros.run_boros_fixed_funding_scan()
    md = boros.format_boros_markdown(snap)

    output_dir.mkdir(parents=True, exist_ok=True)
    _write_text_atomic(output_dir / "snapshot.json", json.dumps(snap.to_dict(), indent=2))
    _write_text_atomic(output_dir / "report.md", md)
    if report_path is not None:
        _write_text_atomic(report_path, md)

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = boros.format_boros_telegram_rich_html(snap)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
