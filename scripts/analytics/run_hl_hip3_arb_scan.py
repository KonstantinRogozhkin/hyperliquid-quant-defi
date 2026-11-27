#!/usr/bin/env python3
"""Run live 100% read-only Hyperliquid HIP-3 Builder-Perps & Cross-Builder L2 Arbitrage scan."""

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

from hl_quant import hl_hip3_builder_arb as hip3  # noqa: E402
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
    min_leg_vol_usd: float = typer.Option(200_000.0, help="Min 24h volume per HIP-3 leg USD"),
    min_spread_apr_pct: float = typer.Option(10.0, help="Min cross-builder funding spread APR (%)"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_hip3_arb"),
    report_path: Path | None = typer.Option(ROOT / "docs/reports/2026-11-26/hl_hip3_builder_arb_live.md"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    snap = hip3.run_hip3_builder_scan(min_leg_vol_usd=min_leg_vol_usd, min_spread_apr_pct=min_spread_apr_pct)
    md = hip3.format_hip3_arb_markdown(snap)

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "snapshot.json").write_text(json.dumps(snap.to_dict(), indent=2), encoding="utf-8")
    (output_dir / "report.md").write_text(md, encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = hip3.format_hip3_arb_telegram_rich_html(snap)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
