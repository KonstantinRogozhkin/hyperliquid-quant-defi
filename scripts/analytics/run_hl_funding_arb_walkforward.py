#!/usr/bin/env python3
"""Rolling walk-forward funding arbitrage backtest on the Hyperliquid universe.

Discovers common HL x CEX perps, caches/reuses public funding & 1h bars, and runs
rolling walk-forward (train_days -> test_days) with either equal-weight or
active-slot concentrated allocation. Research only; no orders placed.

Example:
  run_hl_funding_arb_walkforward.py --venue bybit --days 180 --max-candidates 100 \\
    --min-train-daily-vol-usd 2000000 --allocation active_slots --max-active-slots 5 \\
    --output-dir /tmp/hl-wf
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
from hl_quant import universe_selection as us  # noqa: E402
from hl_quant import walk_forward as wf  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _fetch_or_reuse(root: Path, dataset: str, params: dict, *, base_url: str, provider: str, fetch_fn):
    reused = hl.try_reuse(root, dataset, params, base_url=base_url, provider=provider)
    if reused is not None:
        return reused
    return hl.publish(fetch_fn(), root, base_url=base_url)


def _load_rows(result: hl.PublishResult) -> pd.DataFrame | None:
    if result.status == hl.STATUS_EMPTY:
        return None
    return pd.read_parquet(result.run_dir / "rows.parquet")


def _report(ev: wf.WalkForwardEvaluation, *, venue: str, costs: fa.Costs, train_days: int, test_days: int, top_k: int, min_vol: float) -> str:
    m = ev.oos_metrics
    lines = [
        f"# Walk-forward funding-арбитраж: Hyperliquid против {venue}",
        "",
        f"- Схема: rolling `{train_days}d train -> {test_days}d test` ({len(ev.folds)} фолдов), "
        f"train-фильтр >= ${min_vol / 1e6:.1f}M/день, `top_k={top_k}`.",
        f"- Аллокация: `{ev.allocation}` (max_active_slots={ev.max_active_slots}, max_weight={ev.max_weight_per_coin:.2f}).",
        f"- Издержки: HL {costs.hl_fee_bps} б.п., {venue} {costs.cex_fee_bps} б.п., "
        f"проскальзывание {costs.slippage_bps_per_leg} б.п., плечо {costs.leverage:g}.",
        "",
        "## Сводка OOS (непрерывный склеенный test с учётом комиссий перебалансировки)",
        "",
        "| OOS часов | APR | Sharpe | Max DD | В позиции (gross) | Funding | Базис | Издержки |",
        "|---|---|---|---|---|---|---|---|",
        f"| {m['hours']} | {_fmt_pct(m['apr'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_drawdown'])} | "
        f"{_fmt_pct(m['time_in_market'])} | {_fmt_pct(m['funding_pnl'])} | {_fmt_pct(m['basis_pnl'])} | {_fmt_pct(m['cost_pnl'])} |",
        "",
        "## По фолдам",
        "",
        "| Fold | Test старт | Ликв. | Параметры | Train APR/Sh | Test APR/Sh | Test DD | Монеты (первые 5) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for f in ev.folds:
        ts = datetime.fromtimestamp(f.test_start_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
        lines.append(
            f"| {f.fold_index} | {ts} | {f.eligible_count} | `{f.chosen_params}` | "
            f"{_fmt_pct(f.train_apr)} / {f.train_sharpe:.2f} | {_fmt_pct(f.test_apr)} / {f.test_sharpe:.2f} | "
            f"{_fmt_pct(f.test_max_drawdown)} | {', '.join(f.selected_coins[:5])} |"
        )
    return "\n".join(lines) + "\n"


@app.command()
def main(
    venue: str = typer.Option("bybit", help="binance or bybit"),
    days: int = typer.Option(180, min=45, max=200),
    train_days: int = typer.Option(60, min=14, max=120),
    test_days: int = typer.Option(14, min=3, max=60),
    output_dir: Path = typer.Option(...),
    raw_root: Path | None = typer.Option(None),
    max_candidates: int = typer.Option(100, min=5, max=200),
    min_prefilter_vol_usd: float = typer.Option(500_000.0),
    min_train_daily_vol_usd: float = typer.Option(2_000_000.0),
    min_train_coverage: float = typer.Option(0.80),
    top_k: int = typer.Option(10, min=1, max=50),
    allocation: str = typer.Option("active_slots", help="equal_weight or active_slots"),
    max_active_slots: int = typer.Option(5, min=1, max=30),
    max_weight_per_coin: float = typer.Option(0.25, min=0.05, max=1.0),
    hl_fee_bps: float = typer.Option(1.5),
    cex_fee_bps: float = typer.Option(2.0),
    slippage_bps: float = typer.Option(0.0),
    leverage: float = typer.Option(1.0),
) -> None:
    if venue not in cex.VENUES:
        raise typer.BadParameter(f"venue must be one of {cex.VENUES}")
    if allocation not in ("equal_weight", "active_slots"):
        raise typer.BadParameter("allocation must be equal_weight or active_slots")

    raw = raw_root or (ROOT / os.getenv("BACKTESTER_RAW_ROOT", "data/raw")).resolve()
    now = hl.current_time_ms()
    end_ms = now - now % fa.HOUR_MS - 1
    start_ms = end_ms + 1 - days * 24 * fa.HOUR_MS
    costs = fa.Costs(hl_fee_bps=hl_fee_bps, cex_fee_bps=cex_fee_bps, slippage_bps_per_leg=slippage_bps, leverage=leverage)

    panels: dict[str, pd.DataFrame] = {}
    skipped: dict[str, str] = {}
    with hl.HyperliquidInfoClient(request_delay_s=0.04) as hl_client, cex.default_client(venue) as cex_client:
        candidates = us.discover_common_coins(
            hl_client,
            cex_client,
            venue,
            min_prefilter_vol_usd=min_prefilter_vol_usd,
            max_coins=max_candidates,
        )
        typer.echo(f"discovered={len(candidates)} venue={venue}", err=True)
        for idx, cand in enumerate(candidates, start=1):
            coin, sym = cand.coin, cand.cex_symbol
            try:
                hl_f = _fetch_or_reuse(
                    raw / "hyperliquid",
                    "funding",
                    {"coin": coin, "start_ms": start_ms, "end_ms": end_ms},
                    base_url=hl_client.base_url,
                    provider=hl.PROVIDER,
                    fetch_fn=lambda c=coin: hl.collect_funding(hl_client, c, start_ms, end_ms, now_ms=now),
                )
                hl_b = _fetch_or_reuse(
                    raw / "hyperliquid",
                    "candles",
                    {"coin": coin, "interval": "1h", "start_ms": start_ms, "end_ms": end_ms},
                    base_url=hl_client.base_url,
                    provider=hl.PROVIDER,
                    fetch_fn=lambda c=coin: hl.collect_candles(hl_client, c, "1h", start_ms, end_ms, now_ms=now),
                )
                cex_f = _fetch_or_reuse(
                    raw / venue,
                    f"{venue}_funding",
                    {"coin": sym, "start_ms": start_ms, "end_ms": end_ms},
                    base_url=cex_client.base_url,
                    provider=venue,
                    fetch_fn=lambda s=sym: cex.collect_funding(cex_client, venue, s, start_ms, end_ms, now_ms=now),
                )
                cex_b = _fetch_or_reuse(
                    raw / venue,
                    f"{venue}_klines",
                    {"coin": sym, "interval": "1h", "start_ms": start_ms, "end_ms": end_ms},
                    base_url=cex_client.base_url,
                    provider=venue,
                    fetch_fn=lambda s=sym: cex.collect_klines_1h(cex_client, venue, s, start_ms, end_ms, now_ms=now),
                )
                dfs = [_load_rows(hl_f), _load_rows(cex_f), _load_rows(hl_b), _load_rows(cex_b)]
                if any(df is None or df.empty for df in dfs):
                    skipped[coin] = "empty provider dataset"
                    continue
                panels[coin] = fa.build_panel(dfs[0], dfs[1], dfs[2], dfs[3])
            except Exception as exc:  # noqa: BLE001
                skipped[coin] = f"{type(exc).__name__}: {exc}"
            if idx % 10 == 0 or idx == len(candidates):
                typer.echo(f"progress={idx}/{len(candidates)} loaded={len(panels)} skipped={len(skipped)}", err=True)

    ev = wf.evaluate_walk_forward(
        panels,
        costs=costs,
        train_days=train_days,
        test_days=test_days,
        top_k=top_k,
        min_train_daily_vol_usd=min_train_daily_vol_usd,
        min_train_coverage=min_train_coverage,
        allocation=allocation,  # type: ignore[arg-type]
        max_active_slots=max_active_slots,
        max_weight_per_coin=max_weight_per_coin,
        window_start_ms=start_ms,
        window_end_ms=end_ms,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text(
        _report(ev, venue=venue, costs=costs, train_days=train_days, test_days=test_days, top_k=top_k, min_vol=min_train_daily_vol_usd)
    )
    (output_dir / "summary.json").write_text(
        json.dumps(
            {
                **ev.to_dict(),
                "venue": venue,
                "window": {"start_ms": start_ms, "end_ms": end_ms},
                "loaded_count": len(panels),
                "skipped_count": len(skipped),
                "costs": {"hl_fee_bps": hl_fee_bps, "cex_fee_bps": cex_fee_bps, "slippage_bps": slippage_bps, "leverage": leverage},
                "research_only": True,
            },
            indent=2,
            default=str,
        )
    )
    ev.oos_hourly.to_parquet(output_dir / "oos_hourly.parquet")
    typer.echo(json.dumps({"venue": venue, "folds": len(ev.folds), "oos": ev.oos_metrics, "output": str(output_dir)}, default=str))


if __name__ == "__main__":
    app()
