"""Regression tests for all post-review audit fixes (Items 1.2 through 4.1)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from hl_quant import alpha_families as af
from hl_quant import funding_arb as fa
from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_funding_settlement_study as fss
from hl_quant import hl_portfolio_dashboard as pdash
from hl_quant import hl_testnet_executor as ex
from hl_quant import hl_testnet_risk as risk
from hl_quant import paper_tracker as pt
from hl_quant.metrics import render_prometheus_metrics
from hl_quant.scheduler import WorkerScheduler

ROOT = Path(__file__).resolve().parents[1]
H = fa.HOUR_MS
DAY_MS = 24 * H
UTC_MIDNIGHT = 1_795_824_000_000 - (1_795_824_000_000 % DAY_MS)


def _load_script(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_c5_live_00_05_and_12_05_utc_decision_hits_canonical_boundary_with_closed_23h_bar():
    """At 00:05 UTC, market.hours[-1] is 23:00 UTC; compute_latest_target_positions must evaluate
    at 00:00 UTC using the closed 23:00..00:00 bar and trigger the 24h / 12h UTC rebalance!
    """
    # Build 384 completed hourly bars (>= 360h causal_liquid_mask warmup) ending at 23:00 UTC
    n_h = 384
    hours = (UTC_MIDNIGHT - n_h * H) + np.arange(n_h, dtype="int64") * H
    assert (int(hours[-1]) + H) % DAY_MS == 0  # closed bar ends right at 00:00 UTC!

    rng = np.random.default_rng(7)
    rows = []
    # C0 rallies strongly ONLY in the final closed bar (23:00..00:00) while C1 drops strongly
    for _c_idx, coin in enumerate(["C0", "C1", "C2", "C3"]):
        px = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.002, n_h)))
        if coin == "C0":
            px[-1] = px[-2] * 1.60  # huge jump in the 23:00..00:00 bar!
        elif coin == "C1":
            px[-1] = px[-2] * 0.60  # huge drop in the 23:00..00:00 bar!
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours,
                    "coin": coin,
                    "hl_close": px,
                    "hl_notional_usd": np.full(n_h, 500_000.0),
                    "hl_fund_next": np.zeros(n_h),
                    "hl_rate_h": np.zeros(n_h),
                    "bybit_close": px,
                    "bybit_notional_usd": np.full(n_h, 500_000.0),
                    "bybit_fund_next": np.zeros(n_h),
                    "bybit_rate_h": np.zeros(n_h),
                }
            )
        )
    mkt_00_05 = af.build_wide_market(pd.concat(rows, ignore_index=True))
    mom_24 = pt.MetamodelSleeve(
        af.HypothesisSpec(
            name="xs_mom_24h",
            family="hl_xs_price_reversal_mom",
            venue_mode="hl_single_venue",
            params={"lookback_h": 24, "rebalance_h": 24, "top_n": 1, "sign": 1.0, "min_vol_usd": 1_000.0},
        ),
        weight=1.0,
    )
    targets_00_05 = pt.compute_latest_target_positions(mkt_00_05, [mom_24])
    # Must have rebalanced at 00:00 UTC AND seen the 23:00..00:00 breakout (long C0, short C1)
    assert targets_00_05["C0"]["hl_weight"] > 0.0
    assert targets_00_05["C1"]["hl_weight"] < 0.0

    # Advancing 4 hours to 04:05 UTC (market.hours[-1] = 03:00 UTC -> eval_h = 04:00 UTC) must NOT rebalance 24h sleeve!
    rows_04_05 = []
    hours_04 = (UTC_MIDNIGHT - n_h * H) + np.arange(n_h + 4, dtype="int64") * H
    for df_c in rows:
        coin = str(df_c["coin"].iloc[0])
        extra_px = 50.0 if coin == "C0" else (200.0 if coin == "C1" else 102.0)  # reverse move after 00:00 UTC
        ext = pd.DataFrame(
            {
                "hour_ms": hours_04[-4:],
                "coin": coin,
                "hl_close": np.full(4, extra_px),
                "hl_notional_usd": np.full(4, 500_000.0),
                "hl_fund_next": np.zeros(4),
                "hl_rate_h": np.zeros(4),
                "bybit_close": np.full(4, extra_px),
                "bybit_notional_usd": np.full(4, 500_000.0),
                "bybit_fund_next": np.zeros(4),
                "bybit_rate_h": np.zeros(4),
            }
        )
        rows_04_05.append(pd.concat([df_c, ext], ignore_index=True))
    mkt_04_05 = af.build_wide_market(pd.concat(rows_04_05, ignore_index=True))
    targets_04_05 = pt.compute_latest_target_positions(mkt_04_05, [mom_24])
    assert targets_04_05 == targets_00_05


def test_deploy_dokploy_rejects_mismatched_worker_sha_or_stale_heartbeat(monkeypatch):
    deploy = _load_script("deploy_dokploy_post", "scripts/ops/deploy_dokploy.py")
    sha = "f" * 40
    stale_hb = (datetime.now(tz=timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%d %H:%M:%S UTC")

    # 1. Mismatched worker_status.build_sha fails verification
    monkeypatch.setattr(
        deploy,
        "fetch_json",
        lambda url, timeout_s=10: (
            {"status": "ok"}
            if url.endswith("/healthz")
            else (
                {"build_info": {"sha": sha}, "worker_status": {"service": "hl-worker", "build_sha": "0" * 40}}
                if url.endswith("/scheduler/status")
                else {"sha": sha}
            )
        ),
    )
    with pytest.raises(RuntimeError, match="ws_sha="):
        deploy.verify_live_probes("https://hl.example.com", expected_sha=sha, verify_timeout_s=0.05, poll_interval_s=0.01)

    # 2. Stale worker heartbeat (>300s) fails verification even if build_sha matches
    monkeypatch.setattr(
        deploy,
        "fetch_json",
        lambda url, timeout_s=10: (
            {"status": "ok"}
            if url.endswith("/healthz")
            else (
                {
                    "build_info": {"sha": sha},
                    "worker_status": {"service": "hl-worker", "build_sha": sha, "heartbeat_utc": stale_hb},
                }
                if url.endswith("/scheduler/status")
                else {"sha": sha}
            )
        ),
    )
    with pytest.raises(RuntimeError, match="ws_hb_ok=False"):
        deploy.verify_live_probes("https://hl.example.com", expected_sha=sha, verify_timeout_s=0.05, poll_interval_s=0.01)


def test_relative_path_ops_run_script_execs_cleanly_from_outside_repo(tmp_path: Path):
    """Invoking zsh ../.../scripts/ops/run_*.sh via a relative path from another cwd must resolve SCRIPT_PATH."""
    if not shutil.which("zsh"):
        pytest.skip("zsh not available in minimal environment")
    rel_script = os.path.relpath(ROOT / "scripts/ops/run_hl_frontier_persistence_tick.sh", start=tmp_path)
    # Create a tiny stub python that prints args when running the analytics script, while using real .venv python for ops_lock
    real_py = ROOT / ".venv/bin/python"
    wrapper_py = tmp_path / "stub_python.sh"
    wrapper_py.write_text(
        f'#!/bin/sh\nif [ "$1" = "-m" ]; then exec "{real_py}" "$@"; else echo "RAN_ANALYTICS:$1"; fi\n',
        encoding="utf-8",
    )
    wrapper_py.chmod(0o755)
    env = {
        **os.environ,
        "PYTHON_BIN": str(wrapper_py),
        "BACKTESTER_LOG_DIR": str(tmp_path / "logs"),
        "HL_LOCK_FILE": str(tmp_path / "test.flock"),
    }
    proc = subprocess.run(["zsh", rel_script], cwd=tmp_path, env=env, capture_output=True, text=True, check=True)
    assert "RAN_ANALYTICS:" in proc.stdout
    assert "run_hl_frontier_persistence_tick.py" in proc.stdout


def test_settlement_study_does_not_censor_post_entry_outcomes_and_computes_causal_volume():
    t0 = UTC_MIDNIGHT + 2 * H
    m5 = fss.FIVE_MIN_MS
    funding = [{"time": t0 - H, "fundingRate": "0.0010"}, {"time": t0, "fundingRate": "0.0010"}]
    # Calm pre-entry (+0%), extreme post-entry +120% move (12,000 bps > 9,000 bps) -> must NOT be censored!
    candles = [
        {"t": t0 - 3 * m5, "o": "100.0", "c": "100.0", "v": "1000.0"},  # $100,000 prior vol
        {"t": t0 - m5, "o": "100.0", "c": "100.0", "v": "2500.0"},  # $250,000 prior vol
        {"t": t0, "o": "100.0", "c": "220.0", "v": "999999.0"},  # post-T0 volume MUST NOT leak into t0 day_vol_usd!
        {"t": t0 + 2 * m5, "o": "220.0", "c": "220.0", "v": "999999.0"},
    ]
    ev = fss.build_coin_settlement_events("EXPLODE", candles, funding, day_vol_usd=9_999_999.0)
    assert len(ev) == 1
    assert ev.iloc[0]["day_vol_usd"] == pytest.approx(350_000.0)


def test_parquet_caches_do_not_thrash_and_preserve_state_cum_funding(tmp_path: Path, monkeypatch):
    mm_dir = tmp_path / "hl_metamodel"
    mm_dir.mkdir(parents=True)
    (mm_dir / "state.json").write_text(
        json.dumps(
            {
                "starting_capital_usd": 100_000.0,
                "equity_usd": 101_000.0,
                "cum_funding_usd": 777.25,
                "cum_costs_usd": 42.5,
                "positions": {},
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {
            "tick_hour_ms": [UTC_MIDNIGHT - 2 * H, UTC_MIDNIGHT - H],
            "equity_usd": [100_100.0, 101_000.0],
            "fund_pnl_usd": [10.0, 20.0],  # partial window sum = 30.0 != authoritative 777.25!
            "cost_pnl_usd": [-1.0, -2.0],
            "gross_hl": [0.4, 0.5],
            "gross_cex": [0.4, 0.5],
        }
    ).to_parquet(mm_dir / "ticks.parquet", index=False)

    reads = 0
    orig_read = pd.read_parquet

    def counting_read(p, *a, **kw):
        nonlocal reads
        reads += 1
        return orig_read(p, *a, **kw)

    monkeypatch.setattr(pd, "read_parquet", counting_read)
    # _read_parquet_records shares one disk read across limit=720 and limit=None
    pdash._read_parquet_records(mm_dir / "ticks.parquet", limit=720)
    pdash._read_parquet_records(mm_dir / "ticks.parquet", limit=None)
    assert reads == 1

    p1 = pdash.collect_dashboard_payload(tmp_path)
    first_total_reads = reads
    assert p1["paper"]["cum_funding_usd"] == pytest.approx(777.25)
    assert p1["paper"]["cum_costs_usd"] == pytest.approx(42.5)

    # Second dashboard call without file changes reads 0 times from disk (both ts and pdash cached!)
    pdash.collect_dashboard_payload(tmp_path)
    assert reads == first_total_reads

    # Analytics cross-request cache also reads ticks.parquet at most once across 2 calls
    reads = 0
    ar.build_desk_analytics_payload(tmp_path)
    first_analytics_reads = reads
    ar.build_desk_analytics_payload(tmp_path)
    assert reads == first_analytics_reads


def test_scheduler_wait_budgets_mm_cli_guards_and_unmanaged_orphans(tmp_path: Path):
    # 1. Scheduler dependency wait budgets cover full upstream timeout + retry
    assert WorkerScheduler.DEFAULT_JOB_DEPENDENCIES["testnet_executor"] == ("metamodel_paper", 750.0)
    assert WorkerScheduler.DEFAULT_JOB_DEPENDENCIES["hip3_paper"] == ("frontier_persistence", 510.0)

    # 2. MM CLI multi-coin replay-l2 & --rest multi-coin guard
    mm_cli = _load_script("run_hl_mm_testnet_post", "scripts/analytics/run_hl_mm_testnet.py")
    l2_path = tmp_path / "l2.parquet"
    pd.DataFrame(
        [
            {"coin": "BTC", "recv_ms": 1_000, "best_bid": 90000.0, "best_ask": 90010.0, "bid_sz1": 1.0, "ask_sz1": 1.0},
            {"coin": "BTC", "recv_ms": 2_000, "best_bid": 90005.0, "best_ask": 90015.0, "bid_sz1": 1.0, "ask_sz1": 1.0},
            {"coin": "ETH", "recv_ms": 1_000, "best_bid": 3000.0, "best_ask": 3001.0, "bid_sz1": 5.0, "ask_sz1": 5.0},
            {"coin": "ETH", "recv_ms": 2_000, "best_bid": 3000.5, "best_ask": 3001.5, "bid_sz1": 5.0, "ask_sz1": 5.0},
        ]
    ).to_parquet(l2_path, index=False)
    out_mm = tmp_path / "mm_out"
    res_replay = CliRunner().invoke(
        mm_cli.app,
        ["--coins", "BTC,ETH", "--replay-l2", str(l2_path), "--output-dir", str(out_mm)],
    )
    assert res_replay.exit_code == 0, res_replay.output
    summary_json = json.loads((out_mm / "summary.json").read_text(encoding="utf-8"))
    assert set(summary_json["summaries"]) == {"BTC", "ETH"}

    res_rest_multi = CliRunner().invoke(
        mm_cli.app,
        ["--coins", "BTC,ETH", "--rest", "--duration-sec", "1"],
        env={"NO_COLOR": "1"},
    )
    assert res_rest_multi.exit_code != 0
    clean_out = re.sub(r"\x1b\[[0-9;]*m", "", res_rest_multi.output)
    assert "--ws" in clean_out

    # 3. Unmanaged orphan gating: retained target orphan & unwind-order-covered orphan do NOT count as unmanaged
    acct = ex.AccountSnapshot(
        equity_usd=1000.0,
        free_margin_usd=800.0,
        positions_sz={"KEEP": 10.0, "UNWINDING": -20.0, "STUCK": 15.0, "DUST": 1.0},
        open_orders=[{"coin": "UNWINDING", "oid": 101, "side": "B", "reduceOnly": True}],
        observed_at_ms=1_000,
        position_notional_usd={"KEEP": 50.0, "UNWINDING": -40.0, "STUCK": 35.0, "DUST": 3.0},
        position_entry_px={"KEEP": 5.0, "UNWINDING": 2.0, "STUCK": 2.33, "DUST": 3.0},
        position_unrealized_pnl_usd={"KEEP": 0.0, "UNWINDING": 0.0, "STUCK": 0.0, "DUST": 0.0},
    )
    snap = risk.build_risk_snapshot(
        acct,
        high_water_mark_usd=1000.0,
        asset_ctxs={},
        book_sides={"KEEP": "empty", "UNWINDING": "ask_only", "STUCK": "empty", "DUST": "empty"},
        target_weights={"KEEP": 0.05},  # same-side target -> retained!
    )
    assert len(snap["orphan_positions"]) == 4
    assert snap["unmanaged_orphan_positions"] == 1
    assert snap["orphan_positions"]["STUCK"]["unmanaged"] is True
    assert snap["orphan_positions"]["KEEP"]["unmanaged"] is False
    assert snap["orphan_positions"]["UNWINDING"]["unmanaged"] is False
    assert snap["orphan_positions"]["DUST"]["unmanaged"] is False

    prom_txt = render_prometheus_metrics({"testnet_risk": snap})
    assert "hl_testnet_live_orphan_positions 4" in prom_txt
    assert "hl_testnet_unmanaged_orphan_positions 1" in prom_txt
