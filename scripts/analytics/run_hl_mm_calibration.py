#!/usr/bin/env python3
"""Record real Hyperliquid WebSocket L2 + trades, calibrate Avellaneda–Stoikov (kappa, A, sigma),
and run a (gamma, kappa) parameter sweep on the captured tick stream.

Read-only public market data (default: Mainnet WS for deep liquidity calibration); zero orders sent.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_microstructure as ms  # noqa: E402
from hl_quant import hl_mm_avellaneda_stoikov as mm  # noqa: E402
from hl_quant import hl_testnet_executor as ex  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{path.suffix}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


@app.command()
def main(
    coins: str = typer.Option("ETH,BTC", help="Comma-separated coins to record & calibrate"),
    duration_sec: float = typer.Option(60.0, help="Seconds of live WebSocket L2 + trades to capture"),
    ws_network: str = typer.Option("mainnet", help="Public read-only WS source: 'mainnet' (deeper flow) or 'testnet'"),
    horizon_sec: float = typer.Option(30.0, help="Avellaneda–Stoikov inventory horizon tau (seconds)"),
    window_sec: float = typer.Option(5.0, help="Poisson hazard window length (seconds) for kappa fit"),
    capture_dir: Path = typer.Option(ROOT / "data/paper/hl_mm_calibration", help="Directory for raw ticks & calibration outputs"),
    reuse_existing: bool = typer.Option(False, help="Skip WS recording and calibrate from existing parquet files in capture_dir"),
    report_path: Path | None = typer.Option(None, help="Optional markdown report path (e.g. docs/reports/YYYY-MM-DD/...)"),
) -> None:
    coin_list = [c.strip() for c in coins.split(",") if c.strip()]
    if not coin_list:
        raise typer.BadParameter("coins must be non-empty")

    l2_path = capture_dir / "l2_ticks.parquet"
    trades_path = capture_dir / "trades.parquet"
    if not (reuse_existing and l2_path.is_file()):
        ws_url = ms.MAINNET_WS_URL if ws_network.lower() == "mainnet" else ms.TESTNET_WS_URL
        typer.echo(f"Recording {duration_sec:.0f}s of public HL {ws_network} WS L2 + trades for {coin_list}...", err=True)
        meta = asyncio.run(ms.record_hl_microstructure(coin_list, duration_sec=duration_sec, output_dir=capture_dir, ws_url=ws_url))
        typer.echo(f"Captured: {json.dumps(meta)}", err=True)

    l2_all = pd.read_parquet(l2_path)
    trades_all = pd.read_parquet(trades_path) if trades_path.is_file() else pd.DataFrame()

    calibrations: dict[str, dict] = {}
    md_lines = [
        "# Калибровка Avellaneda–Stoikov (γ, κ) на реальных тиках Hyperliquid WebSocket",
        "",
        f"- Источник тиков: публичный read-only WebSocket `{ws_network}` · окно оценки λ(δ): `{window_sec:.0f}с`, горизонт τ: `{horizon_sec:.0f}с`",
        "",
        "## Эмпирическая калибровка интенсивности прихода ордеров λ(δ) = A · exp(-κ · δ)",
        "",
        "| Монета | Тиков L2 | Сделок | Мед. спред | σ_1s | σ_τ | κ (1/б.п.) | A (1/с) | R² | Мин. полуспред 1/κ+fee (б.п.) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]

    sweep_blocks: list[str] = []
    for coin in coin_list:
        l2_c = l2_all[l2_all["coin"] == coin].copy() if "coin" in l2_all.columns else l2_all.copy()
        tr_c = trades_all[trades_all["coin"] == coin].copy() if (not trades_all.empty and "coin" in trades_all.columns) else pd.DataFrame()
        if l2_c.empty:
            continue
        cal = mm.calibrate_as_arrival_intensity(l2_c, tr_c, coin=coin, horizon_sec=horizon_sec, window_sec=window_sec)
        calibrations[coin] = cal.to_dict()
        md_lines.append(
            f"| **{coin}** | {cal.l2_ticks} | {cal.trades_count} | {cal.median_book_spread_bps:.2f} б.п. | "
            f"{cal.empirical_sigma_1s_bps:.2f} б.п. | {cal.empirical_sigma_tau_bps:.1f} б.п. | "
            f"**{cal.kappa_per_bp:.3f}** | {cal.arrival_a_per_sec:.3f} | {cal.r2_fit:.2f} | {cal.implied_min_half_spread_bps:.2f} |"
        )

        spec = ex.AssetSpec(coin=coin, sz_decimals=4 if coin == "ETH" else (5 if coin == "BTC" else 2), max_leverage=25)
        k_grid = tuple(sorted({round(cal.kappa_per_bp, 3), round(cal.kappa_per_bp * 0.5, 3), round(cal.kappa_per_bp * 1.5, 3), 0.35}))
        base_p = mm.ASParams(
            horizon_sec=horizon_sec,
            default_sigma_bps=max(4.0, cal.empirical_sigma_tau_bps),
            base_order_usd=40.0,
            max_inventory_usd=120.0,
            maker_fee_bps=1.5,
            min_half_spread_bps=2.0,
            requote_threshold_bps=1.0,
        )
        sw = mm.sweep_as_parameters_on_l2(l2_c, spec, trades_df=tr_c, gammas=(0.01, 0.03, 0.08, 0.15), kappas=k_grid, base_params=base_p)
        capture_dir.mkdir(parents=True, exist_ok=True)
        sw_tmp = capture_dir / f"sweep_{coin}.tmp.parquet"
        sw.to_parquet(sw_tmp, index=False)
        sw_tmp.replace(capture_dir / f"sweep_{coin}.parquet")

        sweep_blocks += [
            "",
            f"## Реплей-сетка (γ, κ) на реальных тиках · {coin} (топ-8 по Net PnL)",
            "",
            "| γ | κ | Сделок (B/S) | Оборот ($) | Полуспред | Пик инв. ($) | Spread PnL | MTM | Комиссия | Net PnL | Markout +5s |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
        for r in sw.head(8).itertuples(index=False):
            mk5 = f"{r.markout_5s_bps:+.2f} б.п." if pd.notna(r.markout_5s_bps) else "—"
            sweep_blocks.append(
                f"| {r.gamma:.2f} | {r.kappa:.3f} | {r.fills} ({r.buy_fills}/{r.sell_fills}) | ${r.volume_usd:,.0f} | "
                f"{r.avg_half_spread_bps:.2f} б.п. | ${r.max_abs_inv_usd:,.0f} | {r.spread_pnl_usd:+.4f}$ | "
                f"{r.mtm_pnl_usd:+.4f}$ | {r.fees_usd:+.4f}$ | **{r.net_pnl_usd:+.4f}$** | {mk5} |"
            )

    md = "\n".join(md_lines + sweep_blocks) + "\n"
    capture_dir.mkdir(parents=True, exist_ok=True)
    _atomic_write_text(capture_dir / "calibration.json", json.dumps(calibrations, indent=2))
    _atomic_write_text(capture_dir / "calibration_report.md", md)
    if report_path is not None:
        _atomic_write_text(report_path, md)
    typer.echo(md)


if __name__ == "__main__":
    app()
