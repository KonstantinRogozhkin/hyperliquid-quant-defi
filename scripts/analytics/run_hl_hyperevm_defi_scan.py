#!/usr/bin/env python3
"""Run read-only HyperEVM (Chain 999) on-chain DEX & HyperCore cross-layer DeFi scanner.

Reads live Uniswap-V3-style pools on HyperSwap V3 and Project X (PRJX V3) via JSON-RPC,
compares them against HyperCore Spot & Perp markets, checks HLP / child liquidator vaults,
and reports extreme funding dislocations. Optionally delivers Rich HTML table to Telegram.
"""

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

from hl_quant import hl_hyperevm_defi as defi  # noqa: E402
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
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_hyperevm_defi", help="Directory for JSON & markdown artifacts"),
    report_path: Path | None = typer.Option(None, help="Optional dated report path under docs/reports/"),
    telegram: bool = typer.Option(False, help="Send Rich HTML DeFi scan summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    snap = defi.run_hyperevm_defi_scan()
    md = defi.format_hyperevm_defi_markdown(snap)

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
            rich = defi.format_hyperevm_defi_telegram_rich_html(snap)
            tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
            typer.echo(f"telegram={json.dumps(tg)}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
