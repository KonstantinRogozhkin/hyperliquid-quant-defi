#!/usr/bin/env python3
"""Backtest delta-neutral funding arbitrage: Hyperliquid perp vs Binance/Bybit perp.

Collects (or reuses) public funding history and 1h closes for both venues,
picks one parameter set on the first part of history and reports it untouched
on the last part. Research only: nothing here places orders.

Example:
  run_hl_funding_arb_backtest.py --coins BTC,ETH,SOL --venue binance --days 180 --output-dir /tmp/hl-arb
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import cex_perp_public as cex  # noqa: E402
from hl_quant import funding_arb as fa  # noqa: E402
from hl_quant import hyperliquid as hl  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def cex_symbol(coin: str) -> str:
    """HL ``kPEPE``-style thousand-unit coins trade as ``1000PEPEUSDT`` on CEXs."""
    if coin.startswith("k") and coin[1:].isupper():
        return f"1000{coin[1:]}USDT"
    return f"{coin}USDT"


def _rows(result: hl.PublishResult) -> pd.DataFrame:
    if result.status == hl.STATUS_EMPTY:
        raise typer.BadParameter(f"{result.run_dir.name}: provider returned no rows")
    return pd.read_parquet(result.run_dir / "rows.parquet")


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _report(evaluation: fa.Evaluation, *, coins, venue, costs, statuses) -> str:
    split = datetime.fromtimestamp(evaluation.split_hour_ms / 1000, tz=timezone.utc).isoformat()
    tr, te = evaluation.chosen_train, evaluation.chosen_test
    lines = [
        f"# Funding-арбитраж Hyperliquid против {venue}",
        "",
        f"Монеты: {', '.join(coins)}. Равные веса, обе ноги с равным номиналом, плечо {costs.leverage:g}.",
        f"Издержки: HL {costs.hl_fee_bps} б.п., {venue} {costs.cex_fee_bps} б.п., проскальзывание {costs.slippage_bps_per_leg} б.п. на ногу.",
        f"Граница train/test: {split}. Параметры выбраны только на train: `{evaluation.chosen.label()}`.",
        "",
        "| | APR | Sharpe | Max DD | В позиции | Смен позиции | Funding | Базис | Издержки |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name, m in (("train", tr), ("test", te)):
        lines.append(
            f"| {name} | {_fmt_pct(m['apr'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_drawdown'])} | "
            f"{_fmt_pct(m['time_in_market'])} | {m['position_changes']:.1f} | {_fmt_pct(m['funding_pnl'])} | "
            f"{_fmt_pct(m['basis_pnl'])} | {_fmt_pct(m['cost_pnl'])} |"
        )
    lines += ["", "## Test по монетам", "", "| Монета | APR | Sharpe | Max DD | В позиции |", "|---|---|---|---|---|"]
    for coin, m in evaluation.per_coin_test.items():
        lines.append(
            f"| {coin} | {_fmt_pct(m['apr'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_drawdown'])} | {_fmt_pct(m['time_in_market'])} |"
        )
    positive = int((evaluation.test_grid["apr"] > 0).sum())
    lines += [
        "",
        "## Вся сетка на test (для контроля подгонки)",
        "",
        f"Положительный APR на test у {positive} из {len(evaluation.test_grid)} наборов параметров.",
        "",
        "## Данные",
        "",
        *[f"- {k}: {v}" for k, v in statuses.items()],
        "",
        "Ограничения: исторические закрытия 1h вместо исполнения по стакану; funding по фактическим событиям; "
        "ликвидации, маржинальные требования и перевод залога между биржами не моделируются.",
    ]
    return "\n".join(lines) + "\n"


@app.command()
def main(
    coins: str = typer.Option("BTC,ETH,SOL", help="Comma-separated Hyperliquid perp names"),
    venue: str = typer.Option("binance", help="binance or bybit"),
    days: int = typer.Option(180, min=14, max=200, help="History length; HL keeps ~5000 hourly candles"),
    output_dir: Path = typer.Option(..., help="Where to write report.md, summary.json and test_hourly.parquet"),
    raw_root: Path | None = typer.Option(None, help="Default: $BACKTESTER_RAW_ROOT"),
    train_fraction: float = typer.Option(0.6),
    hl_fee_bps: float = typer.Option(4.5),
    cex_fee_bps: float = typer.Option(5.0),
    slippage_bps: float = typer.Option(1.0),
    leverage: float = typer.Option(1.0),
) -> None:
    if venue not in cex.VENUES:
        raise typer.BadParameter(f"venue must be one of {cex.VENUES}")
    raw = raw_root or (ROOT / os.getenv("BACKTESTER_RAW_ROOT", "data/raw")).resolve()
    now = hl.current_time_ms()
    end_ms = now - now % fa.HOUR_MS - 1
    start_ms = end_ms + 1 - days * 24 * fa.HOUR_MS
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    costs = fa.Costs(hl_fee_bps=hl_fee_bps, cex_fee_bps=cex_fee_bps, slippage_bps_per_leg=slippage_bps, leverage=leverage)

    panels, statuses = {}, {}
    with hl.HyperliquidInfoClient() as hl_client, cex.default_client(venue) as cex_client:
        for coin in coin_list:
            symbol = cex_symbol(coin)
            runs = {
                "hl_funding": hl.publish(hl.collect_funding(hl_client, coin, start_ms, end_ms, now_ms=now), raw / "hyperliquid", base_url=hl_client.base_url),
                "hl_bars": hl.publish(hl.collect_candles(hl_client, coin, "1h", start_ms, end_ms, now_ms=now), raw / "hyperliquid", base_url=hl_client.base_url),
                "cex_funding": hl.publish(cex.collect_funding(cex_client, venue, symbol, start_ms, end_ms, now_ms=now), raw / venue, base_url=cex_client.base_url),
                "cex_bars": hl.publish(cex.collect_klines_1h(cex_client, venue, symbol, start_ms, end_ms, now_ms=now), raw / venue, base_url=cex_client.base_url),
            }
            for name, result in runs.items():
                statuses[f"{coin} {name}"] = f"{result.status}, {result.row_count} строк, run `{result.run_id}`"
            panels[coin] = fa.build_panel(
                _rows(runs["hl_funding"]), _rows(runs["cex_funding"]), _rows(runs["hl_bars"]), _rows(runs["cex_bars"])
            )

    evaluation = fa.evaluate(panels, costs=costs, train_fraction=train_fraction)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text(_report(evaluation, coins=coin_list, venue=venue, costs=costs, statuses=statuses))
    summary = {
        **evaluation.to_dict(),
        "coins": coin_list,
        "venue": venue,
        "window": {"start_ms": start_ms, "end_ms": end_ms},
        "costs": {"hl_fee_bps": hl_fee_bps, "cex_fee_bps": cex_fee_bps, "slippage_bps": slippage_bps, "leverage": leverage},
        "train_grid": evaluation.train_grid.to_dict(orient="records"),
        "test_grid": evaluation.test_grid.to_dict(orient="records"),
        "research_only": True,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    evaluation.test_hourly.to_parquet(output_dir / "test_hourly.parquet")
    typer.echo(json.dumps({"chosen": evaluation.chosen.label(), "test": evaluation.chosen_test, "output": str(output_dir)}, default=str))


if __name__ == "__main__":
    app()
