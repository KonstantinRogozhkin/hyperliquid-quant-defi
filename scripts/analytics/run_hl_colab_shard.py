#!/usr/bin/env python3
"""Run one shard of the Hyperliquid 6-family alpha hypothesis registry.

Works both locally and on Google Colab VMs (reads a frozen ``dataset_bundle.parquet``).
Writes:
  - ``shard_<id>_summary.json``
  - ``shard_<id>_oof_returns.parquet``
  - ``shard_<id>_stressed_returns.parquet``
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

import pandas as pd

# Support both repo layout (scripts/analytics/...) and Colab flat upload (/content/...)
HERE = Path(__file__).resolve()
for candidate in (HERE.parents[2] / "src" if len(HERE.parents) >= 3 else Path("."), HERE.parent, Path("/content")):
    if (candidate / "overnight").exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from hl_quant import alpha_families as af  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--shard-id", type=int, default=0)
    parser.add_argument("--num-shards", type=int, default=1)
    parser.add_argument("--train-days", type=int, default=60)
    parser.add_argument("--fold-days", type=int, default=15)
    parser.add_argument("--holdout-days", type=int, default=45)
    parser.add_argument("--cross-venue-bps", type=float, default=3.5, help="One-way cost per unit turnover, both legs (HL + CEX)")
    parser.add_argument("--cross-venue-stress-bps", type=float, default=5.5)
    parser.add_argument("--hl-bps", type=float, default=1.5, help="One-way cost of a single-venue HL trade")
    parser.add_argument("--hl-stress-bps", type=float, default=3.5)
    parser.add_argument(
        "--registry",
        choices=("default", "slow", "all"),
        default="default",
        help="default = 48 original hypotheses; slow = low-turnover reversal variants; all = both",
    )
    args = parser.parse_args()
    costs = af.ExecutionCosts(
        cross_venue_bps=args.cross_venue_bps,
        cross_venue_stress_bps=args.cross_venue_stress_bps,
        hl_bps=args.hl_bps,
        hl_stress_bps=args.hl_stress_bps,
    )

    t0 = time.time()
    bundle = pd.read_parquet(args.bundle)
    market = af.build_wide_market(bundle)
    dev_start_idx, holdout_start_idx, folds = af.make_walk_forward_splits(
        len(market.hours),
        train_days=args.train_days,
        fold_days=args.fold_days,
        holdout_days=args.holdout_days,
    )
    registry = {
        "default": af.default_hypothesis_registry,
        "slow": af.slow_reversal_registry,
        "all": lambda: af.default_hypothesis_registry() + af.slow_reversal_registry(),
    }[args.registry]()
    my_specs = [spec for i, spec in enumerate(registry) if i % args.num_shards == args.shard_id]

    needs_ml = any(s.family == "ml_walkforward_ranker" for s in my_specs)
    feat_cache = af.build_ml_feature_panel(market) if needs_ml else None

    summaries = []
    oof_cols: dict[str, pd.Series] = {}
    stress_cols: dict[str, pd.Series] = {}

    for idx, spec in enumerate(my_specs, start=1):
        ts = time.time()
        ev = af.evaluate_hypothesis(
            market,
            spec,
            dev_start_idx=dev_start_idx,
            holdout_start_idx=holdout_start_idx,
            fold_boundaries=folds,
            feature_cache=feat_cache,
            costs=costs,
        )
        summaries.append(ev.to_summary_dict())
        oof_cols[spec.name] = ev.hourly_returns
        stress_cols[spec.name] = ev.stressed_returns
        dm, ds = ev.dev_metrics, ev.dev_stressed_metrics
        print(
            f"[shard {args.shard_id} {idx}/{len(my_specs)}] {spec.name:30s} ({spec.family:24s}) | "
            f"dev {dm['apr']*100:+6.2f}% / Sh {dm['sharpe']:+5.2f} | "
            f"stress Sh {ds['sharpe']:+5.2f} | {time.time() - ts:.1f}s",
            flush=True,
        )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "shard_id": args.shard_id,
        "num_shards": args.num_shards,
        "dev_start_ms": int(market.hours[dev_start_idx]),
        "holdout_start_ms": int(market.hours[holdout_start_idx]),
        "end_ms": int(market.hours[-1]),
        "elapsed_s": round(time.time() - t0, 2),
        "registry": args.registry,
        "costs_bps": {
            "cross_venue": costs.cross_venue_bps,
            "cross_venue_stress": costs.cross_venue_stress_bps,
            "hl": costs.hl_bps,
            "hl_stress": costs.hl_stress_bps,
        },
        "summaries": summaries,
    }
    (args.output_dir / f"shard_{args.shard_id}_summary.json").write_text(json.dumps(meta, indent=2))
    pd.DataFrame(oof_cols).to_parquet(args.output_dir / f"shard_{args.shard_id}_oof_returns.parquet")
    pd.DataFrame(stress_cols).to_parquet(args.output_dir / f"shard_{args.shard_id}_stressed_returns.parquet")
    print(f"[shard {args.shard_id}] DONE {len(my_specs)} hypotheses in {time.time() - t0:.1f}s", flush=True)


if __name__ == "__main__":
    main()
