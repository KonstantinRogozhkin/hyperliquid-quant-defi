#!/usr/bin/env python3
"""Build a self-contained interactive HTML portfolio dashboard from all live Hyperliquid artifacts."""

from __future__ import annotations

import os
from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_portfolio_dashboard as dash  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


@app.command()
def main(
    data_root: Path = typer.Option(ROOT / "data/paper", help="Directory containing live Hyperliquid module outputs"),
    output_html: Path = typer.Option(
        ROOT / "docs/reports/2026-11-26/hl_desk_dashboard.html",
        help="Output standalone interactive HTML dashboard path",
    ),
) -> None:
    payload = dash.collect_dashboard_payload(data_root)
    html_str = dash.render_portfolio_dashboard_html(payload)
    _atomic_write_text(output_html, html_str)
    # Also write a copy inside data_root for local quick access
    data_copy = data_root / "hl_desk_dashboard.html"
    if data_copy.resolve() != output_html.resolve():
        _atomic_write_text(data_copy, html_str)
    typer.echo(f"wrote {output_html} ({len(html_str):,} bytes) and {data_copy}")


if __name__ == "__main__":
    app()
