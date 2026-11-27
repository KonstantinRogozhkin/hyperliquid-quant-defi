#!/usr/bin/env python3
"""100% read-only HyperCore Native Spot + Perp Funding Arb Scanner (+ HLP Reserve benchmark)."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_spot_perp_arb as spa  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    output_dir: Path = typer.Option(ROOT / "data/paper/hl_spot_perp_arb", help="Output snapshot directory"),
    clip_usd: float = typer.Option(9_333.0, help="Target hedged notional clip (USD) for L2 VWAP slippage"),
    perp_leverage: float = typer.Option(2.0, help="Target perp margin leverage"),
    fetch_boros: bool = typer.Option(True, help="Enrich with live Pendle Boros fixed-funding bid APRs"),
    taker_fees: bool = typer.Option(False, help="Use taker fees instead of maker ALO fees"),
) -> None:
    cfg = spa.SpotPerpPaperConfig(
        target_perp_leverage=perp_leverage,
        use_maker_fees=not taker_fees,
    )
    with httpx.Client(timeout=25.0) as client:
        snap = spa.scan_spot_perp_opportunities(
            client,
            cfg=cfg,
            clip_usd=clip_usd,
            fetch_l2=True,
            fetch_boros=fetch_boros,
        )

    output_dir.mkdir(parents=True, exist_ok=True)
    tmp_json = output_dir / "snapshot.tmp.json"
    tmp_json.write_text(json.dumps(snap.to_dict(), indent=2, default=str), encoding="utf-8")
    tmp_json.replace(output_dir / "snapshot.json")

    md = spa.format_spot_perp_scan_markdown(snap)
    tmp_md = output_dir / "report.tmp.md"
    tmp_md.write_text(md, encoding="utf-8")
    tmp_md.replace(output_dir / "report.md")
    typer.echo(md)


if __name__ == "__main__":
    app()
