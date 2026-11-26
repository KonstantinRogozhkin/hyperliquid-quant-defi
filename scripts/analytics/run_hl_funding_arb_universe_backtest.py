#!/usr/bin/env python3
"""Universe-wide train-only coin selection backtest: Hyperliquid perp vs Binance/Bybit perp.

Discovers active HL perps that also trade on the target CEX, downloads (or reuses)
180d public funding + 1h candles, filters by train-only median daily notional volume
and coverage, selects top-K coins strictly on train, and evaluates untouched on test.
Research only: nothing here places orders.

Example:
  run_hl_funding_arb_universe_backtest.py --venue binance --days 180 --top-k 10 \\
    --hl-fee-bps 1.5 --cex-fee-bps 2.0 --slippage-bps 0.0 --output-dir /tmp/hl-univ
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

app = typer.Typer(add_completion=False, help=__doc__)


def _fmt_pct(x: float) -> str:
    return f"{x * 100:.2f}%"


def _fmt_usd_m(x: float) -> str:
    return f"${x / 1e6:.1f}M"


def _fetch_or_reuse(root: Path, dataset: str, params: dict, *, base_url: str, provider: str, fetch_fn):
    reused = hl.try_reuse(root, dataset, params, base_url=base_url, provider=provider)
    if reused is not None:
        return reused
    return hl.publish(fetch_fn(), root, base_url=base_url)


def _load_rows(result: hl.PublishResult) -> pd.DataFrame | None:
    if result.status == hl.STATUS_EMPTY:
        return None
    return pd.read_parquet(result.run_dir / "rows.parquet")


def _report(
    u_eval: us.UniverseEvaluation,
    *,
    venue: str,
    costs: fa.Costs,
    discovered_count: int,
    loaded_count: int,
    min_prefilter_vol_usd: float,
    min_train_daily_vol_usd: float,
    min_train_coverage: float,
    top_k: int,
    rank_by: str,
    skipped: dict[str, str],
) -> str:
    split = datetime.fromtimestamp(u_eval.split_hour_ms / 1000, tz=timezone.utc).isoformat()
    se = u_eval.selected_eval
    lines = [
        f"# Отбор монет по train: Hyperliquid против {venue}",
        "",
        f"- Вселенная: найдено {discovered_count} общих перпов (pre-filter HL 24h vol >= {_fmt_usd_m(min_prefilter_vol_usd)}), "
        f"загружено панелей: {loaded_count}, прошли train-фильтр ликвидности (>= {_fmt_usd_m(min_train_daily_vol_usd)}/день "
        f"на обеих биржах, покрытие >= {min_train_coverage * 100:.0f}%): **{len(u_eval.eligible_coins)}**.",
        f"- Издержки: HL {costs.hl_fee_bps} б.п., {venue} {costs.cex_fee_bps} б.п., проскальзывание {costs.slippage_bps_per_leg} б.п. на ногу, плечо {costs.leverage:g}.",
        f"- Граница train/test: `{split}`. Ранжирование на train: `{rank_by}`, топ-{top_k}.",
        f"- Выбранная корзина ({len(u_eval.selected_coins)}): {', '.join(u_eval.selected_coins)}.",
        f"- Параметры, выбранные на train: `{se.chosen.label()}`.",
        "",
        "## Портфель: выбранные топ-K против всей ликвидной вселенной",
        "",
        "| Портфель | Срез | Параметры | APR | Sharpe | Max DD | В позиции | Funding | Базис | Издержки | Сетка > 0 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    rows_to_show = [("Top-K (train)", se)]
    if u_eval.all_eligible_eval is not None:
        rows_to_show.append((f"Вся ликв. ({len(u_eval.eligible_coins)})", u_eval.all_eligible_eval))
    for label, ev in rows_to_show:
        pos = int((ev.test_grid["apr"] > 0).sum())
        for split_name, m in (("train", ev.chosen_train), ("test", ev.chosen_test)):
            grid_txt = f"{pos}/{len(ev.test_grid)}" if split_name == "test" else "—"
            lines.append(
                f"| {label} | {split_name} | `{ev.chosen.label()}` | {_fmt_pct(m['apr'])} | {m['sharpe']:.2f} | "
                f"{_fmt_pct(m['max_drawdown'])} | {_fmt_pct(m['time_in_market'])} | {_fmt_pct(m['funding_pnl'])} | "
                f"{_fmt_pct(m['basis_pnl'])} | {_fmt_pct(m['cost_pnl'])} | {grid_txt} |"
            )

    lines += [
        "",
        "## Выбранные монеты на test",
        "",
        "| Монета | Train score | Train med vol (min) | Test APR | Test Sharpe | Test Max DD | В позиции | Funding | Издержки |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    rank_map = u_eval.train_ranking.set_index("coin").to_dict(orient="index")
    liq_map = u_eval.liquidity.set_index("coin").to_dict(orient="index")
    for coin in u_eval.selected_coins:
        m = se.per_coin_test[coin]
        rk = rank_map[coin]
        lq = liq_map[coin]
        lines.append(
            f"| {coin} | {rk['rank_score']:.2f} | {_fmt_usd_m(lq['min_venue_median_daily_vol_usd'])} | "
            f"{_fmt_pct(m['apr'])} | {m['sharpe']:.2f} | {_fmt_pct(m['max_drawdown'])} | "
            f"{_fmt_pct(m['time_in_market'])} | {_fmt_pct(m['funding_pnl'])} | {_fmt_pct(m['cost_pnl'])} |"
        )

    if skipped:
        lines += ["", "## Пропущенные при загрузке", ""]
        for coin, reason in sorted(skipped.items()):
            lines.append(f"- `{coin}`: {reason}")

    lines += [
        "",
        "Ограничения: survivorship bias (отбираются только ныне активные листинги HL/CEX); "
        "закрытия 1h вместо исполнения по стакану; маржа, ликвидации и перевод залога не моделируются.",
    ]
    return "\n".join(lines) + "\n"


@app.command()
def main(
    venue: str = typer.Option("binance", help="binance or bybit"),
    days: int = typer.Option(180, min=14, max=200, help="History length; HL keeps ~5000 hourly candles"),
    output_dir: Path = typer.Option(..., help="Where to write report.md, summary.json and parquet tables"),
    raw_root: Path | None = typer.Option(None, help="Default: $BACKTESTER_RAW_ROOT"),
    top_k: int = typer.Option(10, min=1, max=50, help="Number of coins to pick strictly on train"),
    max_candidates: int = typer.Option(40, min=5, max=200, help="Cap downloaded candidates by current HL 24h volume"),
    min_prefilter_vol_usd: float = typer.Option(2_000_000.0, help="Download pre-filter on current HL 24h notional volume"),
    min_train_daily_vol_usd: float = typer.Option(5_000_000.0, help="Strict train-only median daily volume floor (both venues)"),
    min_train_coverage: float = typer.Option(0.80, min=0.1, max=1.0, help="Minimum fraction of train hours with prices"),
    rank_by: str = typer.Option("best_sharpe", help="best_sharpe, mean_sharpe, or mean_apr"),
    train_fraction: float = typer.Option(0.6),
    hl_fee_bps: float = typer.Option(1.5),
    cex_fee_bps: float = typer.Option(2.0),
    slippage_bps: float = typer.Option(0.0),
    leverage: float = typer.Option(1.0),
) -> None:
    if venue not in cex.VENUES:
        raise typer.BadParameter(f"venue must be one of {cex.VENUES}")
    if rank_by not in ("best_sharpe", "mean_sharpe", "mean_apr"):
        raise typer.BadParameter("rank_by must be best_sharpe, mean_sharpe, or mean_apr")

    raw = raw_root or (ROOT / os.getenv("BACKTESTER_RAW_ROOT", "data/raw")).resolve()
    now = hl.current_time_ms()
    end_ms = now - now % fa.HOUR_MS - 1
    start_ms = end_ms + 1 - days * 24 * fa.HOUR_MS
    costs = fa.Costs(hl_fee_bps=hl_fee_bps, cex_fee_bps=cex_fee_bps, slippage_bps_per_leg=slippage_bps, leverage=leverage)

    panels: dict[str, pd.DataFrame] = {}
    skipped: dict[str, str] = {}
    with hl.HyperliquidInfoClient(request_delay_s=0.05) as hl_client, cex.default_client(venue) as cex_client:
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

    u_eval = us.evaluate_universe(
        panels,
        costs=costs,
        train_fraction=train_fraction,
        top_k=top_k,
        min_train_daily_vol_usd=min_train_daily_vol_usd,
        min_train_coverage=min_train_coverage,
        rank_by=rank_by,  # type: ignore[arg-type]
        window_start_ms=start_ms,
        window_end_ms=end_ms,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.md").write_text(
        _report(
            u_eval,
            venue=venue,
            costs=costs,
            discovered_count=len(candidates),
            loaded_count=len(panels),
            min_prefilter_vol_usd=min_prefilter_vol_usd,
            min_train_daily_vol_usd=min_train_daily_vol_usd,
            min_train_coverage=min_train_coverage,
            top_k=top_k,
            rank_by=rank_by,
            skipped=skipped,
        )
    )
    summary = {
        **u_eval.to_dict(),
        "venue": venue,
        "window": {"start_ms": start_ms, "end_ms": end_ms},
        "costs": {"hl_fee_bps": hl_fee_bps, "cex_fee_bps": cex_fee_bps, "slippage_bps": slippage_bps, "leverage": leverage},
        "filters": {
            "max_candidates": max_candidates,
            "min_prefilter_vol_usd": min_prefilter_vol_usd,
            "min_train_daily_vol_usd": min_train_daily_vol_usd,
            "min_train_coverage": min_train_coverage,
            "top_k": top_k,
            "rank_by": rank_by,
        },
        "skipped": skipped,
        "research_only": True,
    }
    (output_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    u_eval.liquidity.to_parquet(output_dir / "liquidity.parquet", index=False)
    u_eval.train_ranking.to_parquet(output_dir / "train_ranking.parquet", index=False)
    u_eval.selected_eval.test_hourly.to_parquet(output_dir / "selected_test_hourly.parquet")

    typer.echo(
        json.dumps(
            {
                "venue": venue,
                "eligible": len(u_eval.eligible_coins),
                "selected": u_eval.selected_coins,
                "chosen": u_eval.selected_eval.chosen.label(),
                "selected_test": u_eval.selected_eval.chosen_test,
                "all_eligible_test": u_eval.all_eligible_eval.chosen_test if u_eval.all_eligible_eval else None,
                "output": str(output_dir),
            },
            default=str,
        )
    )


if __name__ == "__main__":
    app()
