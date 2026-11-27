from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hl_quant import hl_autopilot as ap


def _make_trend_closes(n: int = 55, drift: float = 0.015, start: float = 100.0) -> list[float]:
    return [round(start * ((1.0 + drift) ** i), 4) for i in range(n)]


def test_crisis_cta_tsmom_goes_short_in_bear_and_long_gold_with_deadband():
    closes = {
        "BTC": _make_trend_closes(55, drift=-0.012, start=90_000.0),
        "ETH": _make_trend_closes(55, drift=-0.010, start=3_000.0),
        "SOL": _make_trend_closes(55, drift=-0.018, start=200.0),
        "HYPE": _make_trend_closes(55, drift=-0.008, start=80.0),
        "PAXG": _make_trend_closes(55, drift=+0.006, start=3_000.0),
    }
    snap = ap.compute_crisis_cta_weights(closes)
    w = snap["weights"]
    assert w["BTC"] < 0 and w["ETH"] < 0 and w["SOL"] < 0
    assert w["PAXG"] > 0  # Gold safe-haven trend long in crypto drawdown
    assert snap["gross_leverage"] <= ap.AutopilotConfig().cta_max_leverage + 1e-6

    # Minor wiggle within deadband must produce zero turnover
    nudged = {k: v[:-1] + [v[-1] * 1.001] for k, v in closes.items()}
    snap2 = ap.compute_crisis_cta_weights(nudged, prev_weights=w)
    assert snap2["turnover"] == 0.0
    assert snap2["weights"] == w


def test_autopilot_target_weights_respect_directional_cap_reserve_floor_and_obs_gate(tmp_path: Path):
    # Populate 4 sleeves (metamodel with negative Sharpe -> must stay 0%)
    for folder, eq_series in [
        ("hl_spot_perp_paper", [100_000, 100_050, 100_110, 100_160, 100_220, 100_280, 100_340]),
        ("hl_cross_dex_paper", [100_000, 100_080, 100_030, 100_150, 100_200, 100_260, 100_310]),
        ("hl_hip3_paper", [50_000, 50_060, 50_120, 50_180, 50_250, 50_310, 50_390]),
        ("hl_metamodel", [100_000, 99_900, 99_800, 99_700, 99_600, 99_500, 99_400, 99_300, 99_200, 99_100, 99_000, 98_900, 98_800]),
    ]:
        d = tmp_path / folder
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"equity_usd": eq_series[-1], "starting_capital_usd": eq_series[0], "positions": {}}), encoding="utf-8")
        pd.DataFrame({"tick_ms": [1_700_000_000_000 + i * 14_400_000 for i in range(len(eq_series))], "equity_usd": eq_series}).to_parquet(
            d / "ticks.parquet", index=False
        )

    sleeves = ap.collect_desk_sleeve_metrics(tmp_path)
    cfg = ap.AutopilotConfig()
    w = ap.compute_autopilot_target_weights(sleeves, cta_active=True, cfg=cfg)

    assert sum(w.values()) == pytest.approx(1.0, abs=1e-3)
    assert w["crisis_cta"] <= cfg.max_directional_weight + 1e-6
    assert w["hlp_savings_reserve"] >= cfg.min_reserve_weight - 1e-6
    assert w["metamodel_paper"] == 0.0  # observation-only negative Sharpe excluded
    for mn in ("spot_perp_paper", "cross_dex_paper", "hip3_paper"):
        assert 0.0 < w[mn] <= cfg.max_single_sleeve_weight + 1e-6


def test_autopilot_deadband_rebalance_hwm_fees_and_nav_guard(tmp_path: Path):
    sp_dir = tmp_path / "hl_spot_perp_paper"
    cd_dir = tmp_path / "hl_cross_dex_paper"
    h3_dir = tmp_path / "hl_hip3_paper"
    for d, eq in [(sp_dir, 100_000.0), (cd_dir, 100_000.0), (h3_dir, 50_000.0)]:
        d.mkdir(parents=True)
        (d / "state.json").write_text(json.dumps({"equity_usd": eq, "starting_capital_usd": eq, "positions": {}, "hlp_apr_pct": 14.0}), encoding="utf-8")

    closes_t0 = {c: _make_trend_closes(50, drift=0.005, start=100.0) for c in ap.CTA_UNIVERSE}
    cfg = ap.AutopilotConfig(starting_capital_usd=250_000.0, rebalance_deadband_pct=3.0, max_tick_nav_jump_pct=4.0)
    t0 = 1_795_700_000_000

    # Tick 1: initial bootstrap allocation
    r1 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0, daily_closes=closes_t0)
    # Bootstrap charges the CTA book's entry turnover cost (5% sleeve x gross lev x 5 bps)
    assert r1["state"]["cta_state"]["cum_cost_usd"] > 0.0
    assert r1["state"]["gross_equity_usd"] == pytest.approx(
        250_000.0 - r1["state"]["cta_state"]["cum_cost_usd"], abs=1.0
    )
    assert r1["state"]["risk_telemetry"]["rebalanced_this_tick"] is False

    # Tick 2 (+4h): small +0.5% gain on spot_perp -> within 3% deadband (no rebalance!), HWM perf fee applies cleanly
    (sp_dir / "state.json").write_text(json.dumps({"equity_usd": 100_500.0, "starting_capital_usd": 100_000.0, "positions": {}, "hlp_apr_pct": 14.0}), encoding="utf-8")
    r2 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, daily_closes=closes_t0)
    assert r2["state"]["risk_telemetry"]["rebalanced_this_tick"] is False
    assert r2["state"]["gross_equity_usd"] > 250_000.0
    assert r2["state"]["net_equity_usd"] < r2["state"]["gross_equity_usd"]
    # Perf fee only ACCRUES intra-period (reversible liability); nothing is crystallized yet
    assert r2["state"]["accrued_perf_fee_usd"] > 0.0
    assert r2["state"]["cum_perf_fee_usd"] == 0.0
    assert r2["state"]["hwm_net_share_price"] == 1.0

    # Tick 3 (+8h): corrupt +50% jump on spot_perp -> Bounded NAV Sanity Guard triggers!
    (sp_dir / "state.json").write_text(json.dumps({"equity_usd": 150_750.0, "starting_capital_usd": 100_000.0, "positions": {}, "hlp_apr_pct": 14.0}), encoding="utf-8")
    r3 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 8 * 3_600_000, daily_closes=closes_t0)
    assert r3["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r3["state"]["nav_guard_triggers"] == 1
    assert r3["state"]["gross_equity_usd"] == pytest.approx(r2["state"]["gross_equity_usd"], abs=0.5)
    assert np.isfinite(r3["state"]["net_share_price"])
