#!/usr/bin/env python3
"""Run one read-only 4-hour HIP-3 + Pendle Boros + HyperEVM Yield Stack persistence logging tick."""

from __future__ import annotations

from pathlib import Path
import sys

import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_frontier_persistence as fp  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


@app.command()
def main(
    data_root: Path = typer.Option(ROOT / "data/paper", help="Paper data root directory"),
    report_path: Path | None = typer.Option(None, help="Optional markdown report output path"),
) -> None:
    res = fp.run_frontier_persistence_tick(data_root=data_root)
    md = fp.format_frontier_persistence_markdown(res)
    (data_root / "hl_frontier_history" / "report.md").write_text(md, encoding="utf-8")
    if report_path is not None:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(md, encoding="utf-8")
    typer.echo(md)


if __name__ == "__main__":
    app()
