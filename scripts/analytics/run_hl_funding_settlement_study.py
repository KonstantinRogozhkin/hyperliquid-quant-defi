#!/usr/bin/env python3
"""Run causal multi-week/multi-month Dev/Holdout backtest of Hyperliquid `:00` funding settlements."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_funding_settlement_study as fss  # noqa: E402
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
    interval: str = typer.Option("5m", help="Candle interval: '5m' (~17.4d) or '15m' (~52.1d)"),
    max_coins: int = typer.Option(45, help="Number of active HL perpetuals to study"),
    min_oi_usd: float = typer.Option(2_000_000.0, help="Minimum open interest USD"),
    min_vol_usd: float = typer.Option(500_000.0, help="Minimum 24h volume USD"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_funding_settlement_study"),
    report_path: Path = typer.Option(ROOT / "docs/reports/2026-11-26/hl_hourly_funding_settlement_study.md"),
    telegram: bool = typer.Option(False, help="Send Rich HTML OOS verdict table to Telegram"),
) -> None:
    _load_dotenv(ROOT / ".env")
    events_df, meta = asyncio.run(
        fss.fetch_settlement_dataset(
            bar_interval=interval,
            max_coins=max_coins,
            min_oi_usd=min_oi_usd,
            min_vol_usd=min_vol_usd,
        )
    )
    stats_df = fss.evaluate_settlement_strategies(events_df)
    md = fss.format_settlement_study_markdown(stats_df, meta)

    output_dir.mkdir(parents=True, exist_ok=True)
    degraded = bool(meta.get("degraded", False))
    meta["degraded"] = degraded
    if degraded:
        # Keep the last representative events/stats; only publish meta + report flagged DEGRADED.
        typer.echo(
            f"settlement_study_degraded coins={meta.get('coins_collected')}/{meta.get('coins_requested')}",
            err=True,
        )
    if not events_df.empty and not degraded:
        tmp_ev = output_dir / "events.tmp.parquet"
        events_df.to_parquet(tmp_ev, index=False)
        tmp_ev.replace(output_dir / "events.parquet")
    if not stats_df.empty and not degraded:
        tmp_st = output_dir / "stats.tmp.parquet"
        stats_df.to_parquet(tmp_st, index=False)
        tmp_st.replace(output_dir / "stats.parquet")
    # meta.json describes the PUBLISHED dataset (events/stats). A degraded attempt must not relabel
    # the retained results, so its metadata goes to last_attempt.json instead.
    meta_name = "last_attempt.json" if degraded else "meta.json"
    tmp_meta = output_dir / f"{meta_name}.tmp"
    tmp_meta.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    tmp_meta.replace(output_dir / meta_name)
    if not degraded:
        (output_dir / "last_attempt.json").unlink(missing_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_rep = report_path.with_suffix(".tmp.md")
    tmp_rep.write_text(md, encoding="utf-8")
    tmp_rep.replace(report_path)

    if telegram and not degraded:
        token, chat_id, thread_id = pt.load_telegram_credentials((ROOT / ".env",))
        if token and chat_id:
            try:
                rich = fss.format_settlement_study_telegram_rich_html(stats_df, meta)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram={json.dumps(tg)}", err=True)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)

    typer.echo(md)
    if degraded:
        raise typer.Exit(code=2)  # surfaces as a failed worker job instead of a silent "ok"


if __name__ == "__main__":
    app()
