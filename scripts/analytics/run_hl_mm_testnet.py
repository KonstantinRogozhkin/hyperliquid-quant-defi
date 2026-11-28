#!/usr/bin/env python3
"""Run Avellaneda–Stoikov Market Making v2 on Hyperliquid Testnet (dry-run by default).

Features:
* Sub-second ``bbo + l2Book`` WebSocket stream + concurrent Binance Futures Lead-Lag fair price;
* Atomic ``bulk_orders`` / ``batchModify`` + native Hyperliquid ``scheduleCancel`` Dead Man's Switch;
* Multi-coin beta-neutral quoting (``--coins BTC,ETH,SOL``) or single-coin (``--coin ETH``);
* Guaranteed ``finally:`` cancel-all + optional ``--flatten-on-exit`` (``reduce_only IOC``);
* Hot-path ``--benchmark-latency`` tick-to-quote microsecond profiler.
"""

from __future__ import annotations

import asyncio
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

from hl_quant import hl_mm_avellaneda_stoikov as mm  # noqa: E402
from hl_quant import hl_testnet_executor as ex  # noqa: E402
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


_DEFAULT_SZ_DECIMALS: dict[str, int] = {
    "BTC": 5,
    "ETH": 4,
    "BNB": 3,
    "SOL": 2,
    "HYPE": 2,
    "AVAX": 2,
    "TAO": 2,
    "AAVE": 2,
    "LTC": 2,
    "APT": 2,
    "LINK": 1,
    "SUI": 1,
    "NEAR": 1,
    "ARB": 1,
    "OP": 1,
    "TIA": 1,
    "UNI": 1,
    "DOGE": 0,
    "XRP": 0,
    "ADA": 0,
    "TRX": 0,
    "ENA": 0,
    "WIF": 0,
    "ONDO": 0,
    "JUP": 0,
    "PENDLE": 0,
    "SEI": 0,
    "FET": 0,
}


def _replay_sz_decimals(coin: str, l2_df: pd.DataFrame) -> int:
    if "sz_decimals" in l2_df.columns and not l2_df["sz_decimals"].dropna().empty:
        return int(l2_df["sz_decimals"].dropna().iloc[0])
    if coin in _DEFAULT_SZ_DECIMALS:
        return _DEFAULT_SZ_DECIMALS[coin]
    if len(coin) > 1 and coin.startswith("k") and coin[1].isupper():
        return 0
    mid = (
        float(l2_df["mid_px"].median())
        if "mid_px" in l2_df.columns
        else 0.5 * float((l2_df["best_bid"] + l2_df["best_ask"]).median())
    )
    if mid >= 10_000.0:
        return 5
    if mid >= 1_000.0:
        return 4
    if mid >= 10.0:
        return 2
    if mid >= 1.0:
        return 1
    return 0


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f".tmp{path.suffix}")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


@app.command()
def main(
    coin: str = typer.Option("ETH", help="Single cross-margin Testnet asset to market-make"),
    coins: str | None = typer.Option(None, help="Multi-coin comma list (e.g. BTC,ETH,SOL) for beta-neutral MM"),
    execute_testnet: bool = typer.Option(False, help="Submit real ALO MM quotes to Hyperliquid Testnet"),
    use_ws: bool = typer.Option(True, "--ws/--rest", help="Use sub-second WebSocket stream vs REST polling"),
    duration_sec: float = typer.Option(30.0, help="Quoting session duration in seconds"),
    poll_interval_sec: float = typer.Option(2.0, help="Seconds between REST L2 refreshes (--rest only)"),
    min_action_interval_ms: int = typer.Option(400, help="Minimum ms between order actions in --ws mode"),
    lead_lag: bool = typer.Option(True, "--lead-lag/--no-lead-lag", help="Enable Binance Futures WS Lead-Lag fair price & toxic pull"),
    dead_man_switch: bool = typer.Option(True, "--dead-man-switch/--no-dead-man-switch", help="Enable native HL scheduleCancel Dead Man's Switch"),
    flatten_on_exit: bool = typer.Option(False, help="Unwind session delta inventory with reduce_only IOC on exit"),
    gamma: float = typer.Option(0.03, help="Avellaneda–Stoikov inventory risk aversion (1/bp)"),
    kappa: float = typer.Option(0.35, help="Order arrival intensity decay (1/bp)"),
    base_order_usd: float = typer.Option(30.0, help="Base order size per side in USD"),
    max_inventory_usd: float = typer.Option(100.0, help="Hard per-coin inventory limit in USD"),
    min_half_spread_bps: float = typer.Option(2.5, help="Floor on half-spread in bps (>= maker fee + edge)"),
    requote_threshold_bps: float = typer.Option(1.0, help="Price move threshold in bps before modify/replace"),
    replay_l2: Path | None = typer.Option(None, help="Optional local L2 parquet file for offline replay"),
    benchmark_latency: bool = typer.Option(False, help="Run 5,000-tick hot-path tick-to-quote latency benchmark and exit"),
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_mm_testnet", help="Directory for MM session artifacts"),
    telegram: bool = typer.Option(False, help="Send Rich HTML MM session report to Telegram"),
) -> None:
    if benchmark_latency:
        bench = mm.benchmark_tick_to_quote_latency(5_000)
        typer.echo(json.dumps(bench, indent=2))
        return

    _load_dotenv(ROOT / ".env")
    params = mm.ASParams(
        gamma=gamma,
        kappa=kappa,
        base_order_usd=base_order_usd,
        max_inventory_usd=max_inventory_usd,
        min_half_spread_bps=min_half_spread_bps,
        requote_threshold_bps=requote_threshold_bps,
    )
    coin_list = [c.strip() for c in coins.split(",") if c.strip()] if coins else [coin]

    if replay_l2 is not None:
        l2_all = pd.read_parquet(replay_l2)
        summaries = {}
        fill_frames: list[pd.DataFrame] = []
        quote_frames: list[pd.DataFrame] = []
        for target_coin in coin_list:
            l2_df = l2_all[l2_all["coin"] == target_coin].copy() if "coin" in l2_all.columns else l2_all.copy()
            if l2_df.empty:
                continue
            spec = ex.AssetSpec(coin=target_coin, sz_decimals=_replay_sz_decimals(target_coin, l2_df), max_leverage=25)
            s_one, f_one, q_one = mm.simulate_avellaneda_stoikov_on_l2(l2_df, spec, params=params)
            summaries[target_coin] = s_one
            if not f_one.empty:
                fill_frames.append(f_one.assign(coin=target_coin) if "coin" not in f_one.columns else f_one)
            if not q_one.empty:
                quote_frames.append(q_one.assign(coin=target_coin) if "coin" not in q_one.columns else q_one)
        fills_df = pd.concat(fill_frames, ignore_index=True) if fill_frames else pd.DataFrame()
        quotes_df = pd.concat(quote_frames, ignore_index=True) if quote_frames else pd.DataFrame()
    else:
        account_addr = os.getenv("HL_TESTNET_ACCOUNT_ADDRESS", "").strip()
        priv_key = os.getenv("HL_TESTNET_PRIVATE_KEY", "").strip()
        adapter: ex.ExchangeAdapter
        if execute_testnet:
            if not (priv_key and account_addr):
                raise typer.BadParameter("HL_TESTNET_PRIVATE_KEY and HL_TESTNET_ACCOUNT_ADDRESS are required for --execute-testnet")
            adapter = ex.HyperliquidTestnetSDKAdapter(priv_key, account_addr)
        else:
            adapter = ex.DryRunAdapter()
        if use_ws:
            res = asyncio.run(
                mm.run_multi_coin_mm_ws_session(
                    coin_list,
                    adapter=adapter,
                    account_address=account_addr or None,
                    execute_live=execute_testnet,
                    duration_sec=duration_sec,
                    min_action_interval_ms=min_action_interval_ms,
                    flatten_on_exit=flatten_on_exit,
                    enable_lead_lag=lead_lag,
                    enable_dead_man_switch=dead_man_switch,
                    params=params,
                    audit_log_path=output_dir / "mm_audit.jsonl",
                )
            )
            summaries, fills_df, quotes_df = res["summaries"], res["fills_df"], res["quotes_df"]
        else:
            if len(coin_list) > 1:
                raise typer.BadParameter("--rest supports a single coin; use --ws for concurrent multi-coin quoting")
            summary, fills_df, quotes_df = mm.run_live_mm_session(
                coin=coin_list[0],
                adapter=adapter,
                account_address=account_addr or None,
                execute_live=execute_testnet,
                duration_sec=duration_sec,
                poll_interval_sec=poll_interval_sec,
                enable_dead_man_switch=dead_man_switch,
                flatten_on_exit=flatten_on_exit,
                params=params,
                audit_log_path=output_dir / "mm_audit.jsonl",
            )
            summaries = {coin_list[0]: summary}

    output_dir.mkdir(parents=True, exist_ok=True)
    md_parts = [mm.format_mm_report_markdown(s, params) for s in summaries.values()]
    md = "\n---\n\n".join(md_parts)
    _atomic_write_text(output_dir / "report.md", md)
    _atomic_write_text(
        output_dir / "summary.json",
        json.dumps({"params": params.to_dict(), "summaries": {k: v.to_dict() for k, v in summaries.items()}}, indent=2),
    )
    if not quotes_df.empty:
        quotes_tmp = output_dir / "quotes.tmp.parquet"
        quotes_df.to_parquet(quotes_tmp, index=False)
        quotes_tmp.replace(output_dir / "quotes.parquet")
    if not fills_df.empty:
        fills_tmp = output_dir / "fills.tmp.parquet"
        fills_df.to_parquet(fills_tmp, index=False)
        fills_tmp.replace(output_dir / "fills.parquet")

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            for s in summaries.values():
                try:
                    rich = mm.format_mm_telegram_rich_html(s, params)
                    tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                    typer.echo(f"telegram({s.coin})={json.dumps(tg)}", err=True)
                except Exception as exc:
                    typer.echo(f"telegram_warn({s.coin})={exc}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
