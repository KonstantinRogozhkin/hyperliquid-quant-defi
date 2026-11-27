#!/usr/bin/env python3
"""Run one read-only live L2 Paper / Shadow tick for the Hyperliquid metamodel.

Fetches trailing 10-day 1h bars & funding for top liquid HL x Bybit/Binance coins,
pulls live HL L2 orderbooks for all active/traded coins, updates the paper ledger,
and prints a concise JSON status line. Zero orders placed.
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import alpha_families as af  # noqa: E402
from hl_quant import cex_perp_public as cex  # noqa: E402
from hl_quant import funding_arb as fa  # noqa: E402
from hl_quant import hyperliquid as hl  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402
from hl_quant import universe_selection as us  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    state_dir: Path = typer.Option(ROOT / "data/paper/hl_metamodel", help="Paper ledger directory"),
    max_coins: int = typer.Option(35, min=10, max=100, help="Top liquid HL x Bybit coins to track"),
    lookback_days: int = typer.Option(20, min=16, max=40, help="Trailing days (>=16 for 30d liquidity min_periods)"),
    starting_capital_usd: float = typer.Option(100_000.0),
    hl_fee_bps: float = typer.Option(
        5.0,
        help="All-in cost of one HL trade (bps). 5.0 = median measured passive ALO cost on portfolio coins "
        "(fill rate, adverse selection, taker fallback): docs/reports/2026-11-26/hl_alo_fill_portfolio_coins.md",
    ),
    hl_spread_fraction: float = typer.Option(0.0, help="Extra share of the live HL spread on top of --hl-fee-bps"),
    cex_fee_bps: float = typer.Option(2.0, help="CEX maker fee (bps); CEX execution was not measured"),
    cex_spread_fraction: float = typer.Option(0.5, help="Share of the live HL spread charged on the CEX leg"),
    rebalance_deadband: float = typer.Option(
        0.015,
        min=0.0,
        max=0.10,
        help="Minimum single-leg weight change required to rebalance an existing same-direction position (suppresses micro-turnover fee bleed)",
    ),
    max_tick_nav_jump_pct: float = typer.Option(
        8.0,
        min=0.0,
        max=50.0,
        help="Bounded NAV Sanity Guard single-tick MTM jump threshold (%)",
    ),
    attribution_json: Path = typer.Option(
        ROOT / "data/paper/hl_testnet_attribution/attribution.json",
        help="Optional live Testnet attribution JSON used for dynamic ALO fill-rate throttling",
    ),
    telegram: bool = typer.Option(False, help="Send HTML summary to Telegram on new tick"),
) -> None:
    now = hl.current_time_ms()
    floor_hour_ms = now - now % fa.HOUR_MS
    end_ms = floor_hour_ms - 1
    # Funding settlements occur ON the `:00:00.000` boundary (`floor_hour_ms`), whereas `end_ms` is
    # `XX:59:59.999` for completed candle opens; pass `floor_hour_ms` to funding collectors so the
    # just-settled `:00` rate is populated into `*_fund_next` of the latest completed hour bar.
    fund_end_ms = floor_hour_ms
    start_ms = end_ms + 1 - lookback_days * 24 * fa.HOUR_MS

    frames: list[pd.DataFrame] = []
    with (
        hl.HyperliquidInfoClient(request_delay_s=0.03) as hl_client,
        cex.default_client("bybit") as by_client,
        cex.default_client("binance") as bn_client,
        cex.default_client("okx") as okx_client,
    ):
        cands = us.discover_common_coins(hl_client, by_client, "bybit", min_prefilter_vol_usd=1_000_000.0, max_coins=max_coins)
        # Coins already held must always be refreshed, even after they drop out of the
        # current top-N by volume; otherwise the tracker cannot mark them to market.
        state_path = state_dir / "state.json"
        held = sorted(json.loads(state_path.read_text()).get("positions", {})) if state_path.exists() else []
        known = {c.coin for c in cands}
        cands = cands + [
            us.CoinCandidate(coin=c, cex_symbol=us.cex_symbol(c), hl_day_notional_usd=0.0, hl_oi_usd=0.0)
            for c in held
            if c not in known
        ]
        bn_syms = us._binance_perp_symbols(bn_client)
        tracked_coins: list[str] = []
        for cand in cands:
            c, sym = cand.coin, cand.cex_symbol
            try:
                hl_f = hl.collect_funding(hl_client, c, start_ms, fund_end_ms, now_ms=now).rows
                hl_b = hl.collect_candles(hl_client, c, "1h", start_ms, end_ms, now_ms=now).rows
                by_f = cex.collect_funding(by_client, "bybit", sym, start_ms, fund_end_ms, now_ms=now).rows
                by_b = cex.collect_klines_1h(by_client, "bybit", sym, start_ms, end_ms, now_ms=now).rows
                if any(df.empty for df in (hl_f, hl_b, by_f, by_b)):
                    continue
                panel = fa.build_panel(hl_f, by_f, hl_b, by_b).rename(
                    columns={
                        "cex_close": "bybit_close",
                        "cex_ret": "bybit_ret",
                        "cex_notional_usd": "bybit_notional_usd",
                        "cex_fund_next": "bybit_fund_next",
                        "cex_rate_h": "bybit_rate_h",
                        "cex_event_now": "bybit_event_now",
                        "spread_h": "bybit_spread_h",
                    }
                )
                if sym in bn_syms:
                    bn_f = cex.collect_funding(bn_client, "binance", sym, start_ms, fund_end_ms, now_ms=now).rows
                    bn_b = cex.collect_klines_1h(bn_client, "binance", sym, start_ms, end_ms, now_ms=now).rows
                    if not bn_f.empty and not bn_b.empty:
                        p_bn = fa.build_panel(hl_f, bn_f, hl_b, bn_b)[
                            ["cex_close", "cex_ret", "cex_notional_usd", "cex_fund_next", "cex_rate_h", "spread_h"]
                        ].rename(
                            columns={
                                "cex_close": "binance_close",
                                "cex_ret": "binance_ret",
                                "cex_notional_usd": "binance_notional_usd",
                                "cex_fund_next": "binance_fund_next",
                                "cex_rate_h": "binance_rate_h",
                                "spread_h": "binance_spread_h",
                            }
                        )
                        panel = panel.join(p_bn, how="left")
                try:
                    okx_sym = cex.okx_symbol(c)
                    okx_f = cex.collect_funding(okx_client, "okx", okx_sym, start_ms, fund_end_ms, now_ms=now).rows
                    okx_b = cex.collect_klines_1h(okx_client, "okx", okx_sym, start_ms, end_ms, now_ms=now).rows
                    if not okx_f.empty and not okx_b.empty:
                        p_okx = fa.build_panel(hl_f, okx_f, hl_b, okx_b)[
                            ["cex_close", "cex_ret", "cex_notional_usd", "cex_fund_next", "cex_rate_h", "spread_h"]
                        ].rename(
                            columns={
                                "cex_close": "okx_close",
                                "cex_ret": "okx_ret",
                                "cex_notional_usd": "okx_notional_usd",
                                "cex_fund_next": "okx_fund_next",
                                "cex_rate_h": "okx_rate_h",
                                "spread_h": "okx_spread_h",
                            }
                        )
                        panel = panel.join(p_okx, how="left")
                except Exception:  # noqa: BLE001
                    pass
                for prefix in ("binance", "okx"):
                    for suffix in ("close", "ret", "notional_usd", "fund_next", "rate_h", "spread_h"):
                        col = f"{prefix}_{suffix}"
                        if col not in panel.columns:
                            panel[col] = np.nan
                panel["coin"] = c
                frames.append(panel.reset_index())
                tracked_coins.append(c)
            except Exception:  # noqa: BLE001
                continue

        if not frames:
            raise typer.Exit(code=1)
        bundle = pd.concat(frames, ignore_index=True)
        market = af.build_wide_market(bundle)
        quotes = pt.fetch_l2_quotes(hl_client, tracked_coins)

        # Fetch synchronous live CEX BBO mid prices at the exact same wall-clock instant as HL L2 quotes
        # so delta-neutral MTM does not mix `:07` HL prices with `:00` CEX candle closes.
        cex_sym_by_coin = {c: us.cex_symbol(c) for c in tracked_coins}
        okx_sym_by_coin = {c: cex.okx_symbol(c) for c in tracked_coins}
        cex_mid_prices: dict[str, dict[str, float]] = {"bybit": {}, "binance": {}, "okx": {}}
        for venue_name, v_client, sym_map in (
            ("bybit", by_client, cex_sym_by_coin),
            ("binance", bn_client, cex_sym_by_coin),
            ("okx", okx_client, okx_sym_by_coin),
        ):
            try:
                raw_mids = cex.fetch_live_mid_prices(v_client, venue_name, set(sym_map.values()))
                cex_mid_prices[venue_name] = {c: raw_mids[s] for c, s in sym_map.items() if s in raw_mids}
            except Exception:  # noqa: BLE001
                cex_mid_prices[venue_name] = {}

    sleeves = pt.default_frozen_metamodel()
    if attribution_json.is_file():
        try:
            obs_fill = json.loads(attribution_json.read_text()).get("real_alo_fill_rate_pct")
            if obs_fill is not None:
                sleeves = pt.apply_alo_fill_rate_throttle(sleeves, float(obs_fill))
        except Exception:  # noqa: BLE001
            pass

    result = pt.run_paper_tick(
        market,
        quotes,
        state_dir=state_dir,
        sleeves=sleeves,
        starting_capital_usd=starting_capital_usd,
        hl_fee_bps=hl_fee_bps,
        cex_fee_bps=cex_fee_bps,
        l2_spread_fraction=cex_spread_fraction,
        hl_spread_fraction=hl_spread_fraction,
        cex_mid_prices=cex_mid_prices,
        rebalance_deadband_weight=rebalance_deadband,
        max_tick_nav_jump_pct=max_tick_nav_jump_pct,
    )
    if telegram and result.get("status") == "ok":
        state = json.loads((state_dir / "state.json").read_text())
        ticks_path = state_dir / "ticks.parquet"
        ticks_df = pd.read_parquet(ticks_path) if ticks_path.exists() else None
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich_html = pt.format_telegram_rich_html(result, state, ticks_df=ticks_df)
                tg = pt.send_telegram_rich_message(rich_html, bot_token=token, chat_id=chat_id, thread_id=thread_id)
                result["telegram"] = tg
            except Exception as exc:  # noqa: BLE001
                result["telegram"] = {"ok": False, "error": str(exc)}
        else:
            result["telegram"] = {"ok": False, "reason": "missing_credentials"}
    typer.echo(json.dumps(result, default=str))
    if result.get("status") == "held_coins_missing":
        # Ledger left untouched; a non-zero exit makes the skipped tick visible in cron logs.
        raise typer.Exit(code=2)


if __name__ == "__main__":
    app()
