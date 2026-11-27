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
        l2_df = pd.read_parquet(replay_l2)
        target_coin = coin_list[0]
        if "coin" in l2_df.columns:
            l2_df = l2_df[l2_df["coin"] == target_coin].copy()
        spec = ex.AssetSpec(coin=target_coin, sz_decimals=4 if target_coin == "ETH" else 5, max_leverage=25)
        summary, fills_df, quotes_df = mm.simulate_avellaneda_stoikov_on_l2(l2_df, spec, params=params)
        summaries = {target_coin: summary}
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
            summary, fills_df, quotes_df = mm.run_live_mm_session(
                coin=coin_list[0],
                adapter=adapter,
                account_address=account_addr or None,
                execute_live=execute_testnet,
                duration_sec=duration_sec,
                poll_interval_sec=poll_interval_sec,
                params=params,
                audit_log_path=output_dir / "mm_audit.jsonl",
            )
            summaries = {coin_list[0]: summary}

    output_dir.mkdir(parents=True, exist_ok=True)
    md_parts = [mm.format_mm_report_markdown(s, params) for s in summaries.values()]
    md = "\n---\n\n".join(md_parts)
    (output_dir / "report.md").write_text(md)
    (output_dir / "summary.json").write_text(
        json.dumps({"params": params.to_dict(), "summaries": {k: v.to_dict() for k, v in summaries.items()}}, indent=2)
    )
    if not quotes_df.empty:
        quotes_df.to_parquet(output_dir / "quotes.parquet", index=False)
    if not fills_df.empty:
        fills_df.to_parquet(output_dir / "fills.parquet", index=False)

    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            for s in summaries.values():
                rich = mm.format_mm_telegram_rich_html(s, params)
                tg = pt.send_telegram_rich_message(rich, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                typer.echo(f"telegram({s.coin})={json.dumps(tg)}", err=True)

    typer.echo(md)


if __name__ == "__main__":
    app()
