#!/usr/bin/env python3
"""Run one read-only Cross-DEX Paper v2 tick (Hyperliquid × Lighter × Extended with L2 & Persistence Gates)."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import httpx
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_cross_dex_paper as cdp  # noqa: E402
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
    state_dir: Path = typer.Option(ROOT / "data/paper/hl_cross_dex_paper", help="Append-only paper ledger directory"),
    max_slots: int = typer.Option(5, help="Max concurrent delta-neutral pairs"),
    slot_notional_usd: float = typer.Option(5_000.0, help="Notional per leg (2x = gross per pair)"),
    entry_apr_pct: float = typer.Option(15.0, help="Minimum persistent funding spread APR (%)"),
    exit_apr_pct: float = typer.Option(5.0, help="Exit pair when live directional APR drops below this"),
    max_l2_slip_bps: float = typer.Option(5.0, help="Tight L2 combined entry slippage cap (bps)"),
    min_exec_basis_bps: float = typer.Option(-3.0, help="Minimum acceptable L2 execution basis on entry (bps)"),
    min_persistence_ticks: int = typer.Option(2, help="Consecutive ticks >= entry_apr_pct before opening pure carry"),
    enable_extended: bool = typer.Option(True, help="Include Extended perp DEX alongside Lighter"),
    enable_basis_carry_combo: bool = typer.Option(True, help="Enable H1 Basis+Carry immediate entry mode"),
    report_path: Path | None = typer.Option(None, help="Optional markdown report path"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    cfg = cdp.CrossDexPaperConfig(
        max_slots=max_slots,
        slot_notional_usd=slot_notional_usd,
        entry_apr_pct=entry_apr_pct,
        exit_apr_pct=exit_apr_pct,
        max_l2_slip_bps=max_l2_slip_bps,
        min_exec_basis_bps=min_exec_basis_bps,
        min_persistence_ticks=min_persistence_ticks,
        enable_extended=enable_extended,
        enable_basis_carry_combo=enable_basis_carry_combo,
    )
    with httpx.Client(timeout=20.0) as client:
        res = cdp.run_cross_dex_paper_tick(client, state_dir=state_dir, cfg=cfg)

    md = cdp.format_cross_dex_paper_markdown(res)
    _write_text_atomic(state_dir / "report.md", md)
    if report_path is not None:
        _write_text_atomic(report_path, md)

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = cdp.format_cross_dex_paper_telegram_rich_html(res)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
