#!/usr/bin/env python3
"""Read-only 15-minute live risk probe for the Hyperliquid Testnet execution account.

Reads ``clearinghouseState`` / ``spotClearinghouseState`` / ``openOrders`` / ``metaAndAssetCtxs``
and per-position ``l2Book`` from the public Testnet ``info`` API (never signs or places orders),
compares equity with the executor's persisted high-water mark (read-only; the executor owns it),
and atomically writes ``hl_testnet_risk/latest.json`` for ``/api/v1/overview`` and ``/metrics``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

import httpx
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_testnet_executor as ex  # noqa: E402
from hl_quant import hl_testnet_risk as risk  # noqa: E402

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
    hwm_state: Path = typer.Option(ROOT / "data/paper/hl_testnet_hwm.json", help="Executor-owned HWM state (read-only)"),
    output_json: Path = typer.Option(ROOT / "data/paper/hl_testnet_risk/latest.json", help="Atomic risk snapshot output"),
) -> None:
    _load_dotenv(ROOT / ".env")
    account_addr = os.getenv("HL_TESTNET_ACCOUNT_ADDRESS", "").strip()
    if not account_addr:
        raise typer.BadParameter("HL_TESTNET_ACCOUNT_ADDRESS is required for the Testnet risk probe")

    with httpx.Client(timeout=15.0) as client:
        account = ex.fetch_testnet_account(client, account_addr)
        asset_ctxs = risk.fetch_testnet_asset_ctxs(client)
        book_sides = risk.fetch_book_sides(client, list(account.positions_sz))

    snapshot = risk.build_risk_snapshot(
        account,
        high_water_mark_usd=ex.load_high_water_mark(hwm_state),
        asset_ctxs=asset_ctxs,
        book_sides=book_sides,
    )
    output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_json.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
    tmp.replace(output_json)
    typer.echo(json.dumps({k: v for k, v in snapshot.items() if k != "per_coin"}, indent=2, default=str))


if __name__ == "__main__":
    app()
