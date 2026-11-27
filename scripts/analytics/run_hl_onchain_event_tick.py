#!/usr/bin/env python3
"""Run one read-only H2+H4+H5+H6 On-Chain Event Alpha tick and grade forward outcomes."""

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

from hl_quant import hl_onchain_event_alpha as oea  # noqa: E402
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
    state_dir: Path = typer.Option(ROOT / "data/paper/hl_onchain_events", help="Append-only event ledger directory"),
    top_whales: int = typer.Option(60, help="Active leveraged whales to inspect for H2"),
    report_path: Path | None = typer.Option(None, help="Optional dated markdown report output path"),
    telegram_on_alert: bool = typer.Option(False, help="Send Rich HTML alert to Telegram only if threshold triggered"),
    force_telegram: bool = typer.Option(False, help="Always send Rich HTML summary to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    cfg = oea.OnChainEventConfig(top_whales=top_whales)
    res = oea.run_onchain_event_tick(state_dir=state_dir, cfg=cfg)
    md = oea.format_onchain_events_markdown(res)
    _write_text_atomic(state_dir / "report.md", md)
    if report_path is not None:
        _write_text_atomic(report_path, md)

    should_send = force_telegram or (telegram_on_alert and res.get("should_notify", res["any_alert"]))
    if res.get("suppressed_alert_keys"):
        typer.echo(f"alerts_suppressed_by_cooldown={res['suppressed_alert_keys']}", err=True)
    if should_send:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich = oea.format_onchain_events_telegram_rich_html(res)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
