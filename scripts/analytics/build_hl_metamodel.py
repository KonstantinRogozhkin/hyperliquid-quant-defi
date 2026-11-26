#!/usr/bin/env python3
"""Aggregate Colab hypothesis shards, fit Dev-only metamodel, and audit untouched holdout."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hl_quant import metamodel as mm  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shards-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-dev-sharpe", type=float, default=0.5)
    parser.add_argument("--min-stressed-sharpe", type=float, default=0.0)
    parser.add_argument("--min-pos-fold-ratio", type=float, default=0.80)
    parser.add_argument("--max-corr", type=float, default=0.50)
    parser.add_argument("--max-per-family", type=int, default=2)
    parser.add_argument("--max-total", type=int, default=8)
    parser.add_argument("--target-vol", type=float, default=0.12)
    parser.add_argument("--max-leverage", type=float, default=3.0)
    args = parser.parse_args()

    summary_files = sorted(args.shards_dir.glob("shard_*_summary.json"))
    if not summary_files:
        raise FileNotFoundError(f"No shard_*_summary.json found in {args.shards_dir}")

    all_summaries = []
    oof_frames = []
    stress_frames = []
    holdout_start_ms = None
    dev_start_ms = None

    for sf in summary_files:
        data = json.loads(sf.read_text())
        all_summaries.extend(data["summaries"])
        holdout_start_ms = data["holdout_start_ms"]
        dev_start_ms = data["dev_start_ms"]
        sid = data["shard_id"]
        oof_frames.append(pd.read_parquet(args.shards_dir / f"shard_{sid}_oof_returns.parquet"))
        stress_frames.append(pd.read_parquet(args.shards_dir / f"shard_{sid}_stressed_returns.parquet"))

    assert holdout_start_ms is not None
    oof_df = pd.concat(oof_frames, axis=1)
    stress_df = pd.concat(stress_frames, axis=1)

    res = mm.build_metamodel(
        all_summaries,
        oof_df,
        stress_df,
        holdout_start_ms=holdout_start_ms,
        min_dev_sharpe=args.min_dev_sharpe,
        min_stressed_sharpe=args.min_stressed_sharpe,
        min_pos_fold_ratio=args.min_pos_fold_ratio,
        max_corr=args.max_corr,
        max_per_family=args.max_per_family,
        max_total=args.max_total,
        target_vol_annual=args.target_vol,
        max_leverage=args.max_leverage,
    )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "dev_start_ms": dev_start_ms,
        "holdout_start_ms": holdout_start_ms,
        "total_hypotheses": len(all_summaries),
        "all_summaries": all_summaries,
        "metamodel": res.to_dict(),
    }
    (args.output_dir / "metamodel_summary.json").write_text(json.dumps(payload, indent=2))
    res.corr_matrix_dev.to_csv(args.output_dir / "dev_corr_matrix.csv")
    res.holdout_hourly.to_parquet(args.output_dir / "holdout_hourly.parquet")

    print(f"Total hypotheses evaluated: {len(all_summaries)}")
    print(f"Selected orthogonal winners ({len(res.selected_names)}): {res.selected_names}")
    print("\n=== METAMODEL RESULTS (Dev 75d vs Untouched Holdout 45d) ===")
    for method in res.dev_metrics:
        dm = res.dev_metrics[method]
        hm = res.holdout_metrics[method]
        hs = res.holdout_stressed_metrics[method]
        print(
            f"{method:24s} | DEV {dm['apr']*100:+6.2f}% / Sh {dm['sharpe']:+5.2f} / DD {dm['max_drawdown']*100:+6.2f}% | "
            f"HOLD {hm['apr']*100:+6.2f}% / Sh {hm['sharpe']:+5.2f} / DD {hm['max_drawdown']*100:+6.2f}% | "
            f"HOLD-STRESS {hs['apr']*100:+6.2f}% / Sh {hs['sharpe']:+5.2f}"
        )


if __name__ == "__main__":
    main()
