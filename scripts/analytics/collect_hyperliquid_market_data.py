#!/usr/bin/env python3
"""Collect public Hyperliquid market data (read-only, research evidence).

Examples:
  collect_hyperliquid_market_data.py funding --coins BTC,ETH --start 2026-11-01 --end 2026-11-24
  collect_hyperliquid_market_data.py candles --coins BTC --interval 1h --start 2026-11-20 --end 2026-11-24
  collect_hyperliquid_market_data.py snapshot --coins BTC,ETH,SOL
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hyperliquid as hl  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)

# Unix epoch 1970-01-01 was a Thursday; crypto weekly candles open on Monday 00:00 UTC.
_MONDAY_OFFSET_MS = 4 * 86_400_000


def _output_root(value: Path | None) -> Path:
    if value is not None:
        return value
    return (ROOT / os.getenv("BACKTESTER_RAW_ROOT", "data/raw")).resolve() / "hyperliquid"


def _ms(value: str, *, end: bool = False) -> int:
    """ISO date/datetime (UTC) to epoch ms; a bare end date means end of that day."""
    text = value.strip()
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    ms = int(parsed.timestamp() * 1000)
    if end and len(text) == 10:
        ms += 86_400_000 - 1
    return ms


def _end_ms_funding(end: str, now: int) -> int:
    """Clamp to the latest elapsed hourly funding settlement (inclusive of :00:00)."""
    step = hl.FUNDING_STEP_MS
    latest_settlement = now - now % step
    return min(_ms(end, end=True), latest_settlement)


def _end_ms_candles(end: str, now: int, interval: str) -> int:
    """Clamp to the last millisecond of the latest fully closed candle."""
    step = hl.CANDLE_INTERVALS_MS[interval]
    if interval == "1w":
        boundary = ((now - _MONDAY_OFFSET_MS) // step) * step + _MONDAY_OFFSET_MS
    else:
        boundary = now - now % step
    return min(_ms(end, end=True), boundary - 1)


def _coins(raw: str) -> list[str]:
    values = [c.strip() for c in raw.split(",") if c.strip()]
    if not values:
        raise typer.BadParameter("at least one coin")
    return values


def _report(result: hl.PublishResult) -> None:
    typer.echo(
        json.dumps(
            {
                "run_id": result.run_id,
                "status": result.status,
                "rows": result.row_count,
                "reused": result.reused,
                "path": str(result.run_dir),
            }
        )
    )


def _base_url(testnet: bool) -> str:
    return hl.TESTNET_INFO_URL if testnet else hl.MAINNET_INFO_URL


@app.command()
def funding(
    coins: str = typer.Option(..., help="Comma-separated perp names, e.g. BTC,ETH"),
    start: str = typer.Option(..., help="UTC ISO date or datetime"),
    end: str = typer.Option(..., help="UTC ISO date or datetime (date = end of day)"),
    output_root: Path | None = typer.Option(None, help="Default: $BACKTESTER_RAW_ROOT/hyperliquid"),
    testnet: bool = typer.Option(False, help="Use the public testnet info endpoint"),
) -> None:
    """Hourly funding history."""
    base = _base_url(testnet)
    root = _output_root(output_root)
    now = hl.current_time_ms()
    start_ms = _ms(start)
    end_ms = _end_ms_funding(end, now)
    if end_ms < start_ms:
        raise typer.BadParameter("end is before start after clamping to the latest funding settlement")
    with hl.HyperliquidInfoClient(base_url=base) as client:
        for coin in _coins(coins):
            params = {"coin": coin, "start_ms": start_ms, "end_ms": end_ms}
            reused = hl.try_reuse(root, "funding", params, base_url=base, provider=hl.PROVIDER)
            if reused is not None:
                _report(reused)
                continue
            collected = hl.collect_funding(client, coin, start_ms, end_ms, now_ms=now)
            _report(hl.publish(collected, root, base_url=base))


@app.command()
def candles(
    coins: str = typer.Option(..., help="Comma-separated perp names"),
    interval: str = typer.Option("1h", help=f"One of {', '.join(hl.CANDLE_INTERVALS_MS)}"),
    start: str = typer.Option(..., help="UTC ISO date or datetime"),
    end: str = typer.Option(..., help="UTC ISO date or datetime (date = end of day)"),
    output_root: Path | None = typer.Option(None, help="Default: $BACKTESTER_RAW_ROOT/hyperliquid"),
    testnet: bool = typer.Option(False, help="Use the public testnet info endpoint"),
) -> None:
    """Closed OHLCV candles (provider keeps only the latest ~5000 per interval)."""
    base = _base_url(testnet)
    root = _output_root(output_root)
    if interval not in hl.CANDLE_INTERVALS_MS:
        raise typer.BadParameter(f"unknown interval {interval!r}")
    now = hl.current_time_ms()
    start_ms = _ms(start)
    end_ms = _end_ms_candles(end, now, interval)
    if end_ms < start_ms:
        raise typer.BadParameter("end is before start after clamping to the latest closed candle")
    with hl.HyperliquidInfoClient(base_url=base) as client:
        for coin in _coins(coins):
            params = {"coin": coin, "interval": interval, "start_ms": start_ms, "end_ms": end_ms}
            reused = hl.try_reuse(root, "candles", params, base_url=base, provider=hl.PROVIDER)
            if reused is not None:
                _report(reused)
                continue
            collected = hl.collect_candles(client, coin, interval, start_ms, end_ms, now_ms=now)
            _report(hl.publish(collected, root, base_url=base))


@app.command()
def snapshot(
    coins: str = typer.Option(..., help="Comma-separated perp names"),
    trades: bool = typer.Option(True, help="Include recent public trades"),
    output_root: Path | None = typer.Option(None, help="Default: $BACKTESTER_RAW_ROOT/hyperliquid"),
    testnet: bool = typer.Option(False, help="Use the public testnet info endpoint"),
) -> None:
    """Current asset contexts, cross-venue predicted funding, L2 books and recent trades."""
    base = _base_url(testnet)
    with hl.HyperliquidInfoClient(base_url=base) as client:
        collected = hl.collect_snapshot(client, _coins(coins), include_trades=trades)
        _report(hl.publish(collected, _output_root(output_root), base_url=base))


if __name__ == "__main__":
    app()
