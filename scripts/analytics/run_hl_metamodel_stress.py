#!/usr/bin/env python3
"""Execution Monte Carlo for frozen HL metamodel sleeve weights on the untouched holdout.

Reads a frozen ``dataset_bundle.parquet`` (same input as ``run_hl_colab_shard.py``), rebuilds the
sleeve-weighted return / turnover stream (``hl_quant.execution_stress``) and
prints one JSON list of scenario summaries for the holdout slice. Research only, no orders.

Example:
  run_hl_metamodel_stress.py --bundle dataset_bundle.parquet \
    --weights '{"xs_rev_L24_r12": 0.379, "carry_best_cex_L168_in5": 0.4, "xs_mom_L72_r24": 0.221}'
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from hl_quant import alpha_families as af  # noqa: E402
from hl_quant import execution_stress as es  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--weights", required=True, help="JSON object {hypothesis_name: weight}")
    parser.add_argument("--exclude-coins", default="", help="Comma-separated coins to drop (e.g. INIT)")
    parser.add_argument("--paths", type=int, default=500)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--holdout-days", type=int, default=45)
    parser.add_argument("--output", type=Path, default=None)
    args = parser.parse_args()

    bundle = pd.read_parquet(args.bundle)
    excluded = {c.strip() for c in args.exclude_coins.split(",") if c.strip()}
    if excluded:
        bundle = bundle[~bundle["coin"].isin(excluded)]
    market = af.build_wide_market(bundle)
    _, holdout_idx, _ = af.make_walk_forward_splits(len(market.hours), holdout_days=args.holdout_days)
    ret, turnover, leg_vol, delay_exposure = es.metamodel_stream(market, json.loads(args.weights))
    hold = market.hours[holdout_idx:]
    results = es.run_execution_monte_carlo(
        ret.loc[hold],
        turnover.loc[hold],
        leg_vol.loc[hold],
        n_paths=args.paths,
        seed=args.seed,
        delay_exposure=delay_exposure.loc[hold],
    )
    payload = json.dumps([r.to_dict() for r in results], indent=2)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload)
    print(payload)


if __name__ == "__main__":
    main()
