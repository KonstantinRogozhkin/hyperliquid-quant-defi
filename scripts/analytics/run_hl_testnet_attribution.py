#!/usr/bin/env python3
"""Run post-trade PnL attribution, maker fill-rate & markout audit for Hyperliquid Testnet.

Read-only analytics over live Testnet ``userFills``, ``userFunding``, positions,
L2 mids, and ``data/paper/hl_testnet_audit.jsonl``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import httpx
import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_post_trade_attribution as pta  # noqa: E402
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


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{path.suffix}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


@app.command()
def main(
    account_address: str | None = typer.Option(None, help="Default: $HL_TESTNET_ACCOUNT_ADDRESS"),
    audit_log: Path = typer.Option(ROOT / "data/paper/hl_testnet_audit.jsonl"),
    paper_dir: Path = typer.Option(ROOT / "data/paper/hl_metamodel"),
    output_dir: Path | None = typer.Option(ROOT / "data/paper/hl_testnet_attribution"),
    telegram: bool = typer.Option(False, help="Send Rich HTML attribution table to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    addr = (account_address or os.getenv("HL_TESTNET_ACCOUNT_ADDRESS", "")).strip()
    if not addr:
        raise typer.BadParameter("account_address or HL_TESTNET_ACCOUNT_ADDRESS is required")

    with httpx.Client(timeout=15.0) as client:
        snap = pta.fetch_post_trade_snapshot(client, addr)

    attr = pta.compute_post_trade_attribution(snap, audit_log_path=audit_log, paper_dir=paper_dir)
    md = pta.format_attribution_markdown(attr)

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(output_dir / "report.md", md)
        _atomic_write_text(output_dir / "attribution.json", json.dumps(attr.to_dict(), indent=2))
        if snap["fills"]:
            pd.DataFrame(snap["fills"]).to_parquet(output_dir / "fills.parquet", index=False)

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            rich = pta.format_attribution_telegram_rich_html(attr)
            tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
            typer.echo(f"telegram={json.dumps(tg)}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
