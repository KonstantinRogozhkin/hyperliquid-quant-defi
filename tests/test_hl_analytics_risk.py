from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hl_quant import hl_analytics_risk as ar


def _write_ticks(path: Path, dates: list[datetime], equities: list[float], extra: dict | None = None) -> None:
    path.mkdir(parents=True, exist_ok=True)
    st = {"starting_capital_usd": equities[0], "equity_usd": equities[-1], "positions": {}}
    if extra:
        st.update(extra)
    (path / "state.json").write_text(json.dumps(st), encoding="utf-8")
    pd.DataFrame(
        {"tick_ms": [int(d.timestamp() * 1000) for d in dates], "equity_usd": equities}
    ).to_parquet(path / "ticks.parquet", index=False)


def test_hwm_net_series_charges_mgmt_and_perf_strictly_above_high_water_mark():
    dates = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    gross = [1.0, 1.10, 1.05, 1.08]  # Peak 1.10 -> dip 1.05 -> partial recovery 1.08 (still below HWM!)
    net = ar.simulate_hwm_net_series(dates, gross, mgmt_fee_annual_pct=0.0, perf_fee_pct=20.0)
    assert net[0] == 1.0
    assert net[1] == pytest.approx(1.08, abs=1e-4)  # 20% perf fee on +0.10 gain
    # Dip from 1.10 -> 1.05 (-4.545%) and recovery 1.05 -> 1.08 (+2.857%) stay below HWM=1.08 -> zero extra perf fee!
    expected_2 = net[1] * (1.05 / 1.10)
    expected_3 = expected_2 * (1.08 / 1.05)
    assert net[2] == pytest.approx(expected_2, abs=1e-4)
    assert net[3] == pytest.approx(expected_3, abs=1e-4)


def test_full_analytics_payload_leaderboard_correlation_ols_and_risk_exposure(tmp_path: Path):
    base_dt = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    days = [base_dt + timedelta(days=i) for i in range(45)]
    rng = np.random.default_rng(42)

    # BTC drops -25% with high volatility
    btc_rets = rng.normal(-0.006, 0.03, size=45)
    btc_px = 80_000.0 * np.exp(np.cumsum(btc_rets))
    bench_dir = tmp_path / "hl_benchmarks"
    bench_dir.mkdir(parents=True)
    (bench_dir / "daily_closes.json").write_text(
        json.dumps(
            {
                "updated_at_utc": "2026-10-16 12:00 UTC",
                "benchmarks": {
                    "BTC": [{"date": d.strftime("%Y-%m-%d"), "close": float(px)} for d, px in zip(days, btc_px, strict=False)]
                },
            }
        ),
        encoding="utf-8",
    )

    # Spot+Perp: steady market-neutral upward carry (~0 beta to BTC)
    sp_eq = 100_000.0 * np.cumprod(1.0 + rng.normal(0.0004, 0.0003, size=45))
    _write_ticks(
        tmp_path / "hl_spot_perp_paper",
        days,
        sp_eq.tolist(),
        extra={
            "risk_telemetry": {
                "gross_leverage": 1.32,
                "leverage_30d_avg": 1.38,
                "leverage_30d_max": 1.55,
                "net_delta_pct": 0.01,
                "venue_breakdown_usd": {"HyperCore Spot": 55_000.0, "HyperCore HLP Vault": 10_000.0},
                "asset_tier_breakdown_usd": {"Crypto - Large Cap": 60_000.0, "Gold RWA": 15_000.0},
            }
        },
    )

    # Cross-DEX: orthogonal market-neutral series
    cd_eq = 100_000.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.0006, size=45))
    _write_ticks(tmp_path / "hl_cross_dex_paper", days, cd_eq.tolist())

    # Autopilot: combined master series
    ap_eq = 250_000.0 * np.cumprod(1.0 + rng.normal(0.0005, 0.0004, size=45))
    _write_ticks(
        tmp_path / "hl_autopilot",
        days,
        ap_eq.tolist(),
        extra={
            "gross_equity_usd": float(ap_eq[-1]),
            "risk_telemetry": {
                "gross_leverage": 1.25,
                "leverage_30d_avg": 1.22,
                "leverage_30d_max": 1.40,
                "net_directional_exposure_pct": -0.5,
                "venue_breakdown_usd": {"HyperCore": 180_000.0, "HL x Lighter/Extended": 70_000.0},
            },
        },
    )

    payload = ar.build_desk_analytics_payload(tmp_path)
    lb = {r["id"]: r for r in payload["leaderboard"]}
    assert {"autopilot", "spot_perp", "cross_dex", "bench_btc"}.issubset(lb.keys())

    # Neutral Trade rule: negative benchmark returns must NOT be annualized under APY
    btc_row = lb["bench_btc"]
    if (btc_row["gross_returns"]["all_time"] or 0.0) < 0:
        assert btc_row["gross_apy"]["all_time"] == btc_row["gross_returns"]["all_time"]

    # Spot+Perp positive steady carry -> high Sharpe, net return <= gross return, monthly matrix present
    sp_row = lb["spot_perp"]
    assert (sp_row["sharpe_gross"] or 0.0) > 2.0
    assert (sp_row["net_returns"]["all_time"] or 0.0) <= (sp_row["gross_returns"]["all_time"] or 0.0)
    assert len(sp_row["monthly_gross"]) >= 1

    # Lower-triangular Pearson correlation matrix
    corr = payload["correlation"]
    assert len(corr["ids"]) == 3
    assert corr["matrix"][0][0] == 1.0
    assert corr["matrix"][0][1] is None  # upper triangle masked
    assert corr["matrix"][1][0] is not None  # lower triangle populated

    # OLS Market Exposure vs BTC -> market-neutral spot_perp has near-zero beta
    ols_btc = payload["market_exposure"]["BTC"]
    sp_ols = next(r for r in ols_btc["rows"] if r["id"] == "spot_perp")
    assert sp_ols["days"] >= 40
    assert abs(sp_ols["beta"]) < 0.05
    assert len(sp_ols["scatter"]) >= 40

    # Consolidated Risk Exposure telemetry
    risk = payload["risk_exposure"]
    assert len(risk["leverage_by_strategy"]) == 3
    assert len(risk["venue_concentration"]) >= 2
    assert len(risk["asset_tier_concentration"]) >= 2
