"""Regression tests for the third 2026-11-27 live-dump audit pass (stale-prod findings re-verified on main)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

from hl_quant import cross_dex_scanner as cds
from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_cross_dex_paper as cdp
from hl_quant import hl_funding_settlement_study as fss
from hl_quant import hl_hyperevm_defi as evm
from hl_quant import hl_liquidation_and_funding_alpha as lfa
from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant.api.app import sanitize_json
from hl_quant.metrics import _PromBuilder


def _q(dex: str, coin: str, px: float, apr: float) -> cds.DexMarketQuote:
    return cds.DexMarketQuote(
        dex=dex,
        coin=coin,
        raw_symbol=coin,
        mark_px=px,
        funding_rate_h=apr / (cds.HOURS_PER_YEAR * 100.0),
        funding_apr_pct=apr,
        day_vol_usd=10_000_000.0,
        oi_usd=5_000_000.0,
        spread_bps=1.0,
    )


def _near_book(dex: str, sym: str):
    if dex == "hyperliquid":
        return ([(2.9999, 10_000.0)], [(3.0000, 10_000.0)])
    return ([(3.0060, 10_000.0)], [(3.0061, 10_000.0)])


# --------------------------------------------------------------------------- cross_dex_paper


def test_h1_combo_requires_its_own_persistence_by_default(tmp_path: Path):
    """A one-tick basis+carry spike must not open an H1 combo pair (default 2 spaced ticks)."""
    t0 = 1_796_000_000_000
    quotes = dict(
        hl_quotes={"NEAR": _q("hyperliquid", "NEAR", 3.0000, -13.0)},  # 13% < 15% carry gate, >= 12% combo
        lighter_quotes={},
        extended_quotes={"NEAR": _q("extended", "NEAR", 3.0060, 0.0)},  # +20 bps mark basis
        book_fetcher=_near_book,
    )
    cfg = cdp.CrossDexPaperConfig()
    with httpx.Client() as client:
        r1 = cdp.run_cross_dex_paper_tick(client, state_dir=tmp_path, cfg=cfg, now_ms=t0, **quotes)
        assert r1["state"]["positions"] == {}
        assert r1["gated_stats"]["skipped_persistence"] >= 1
        # A re-run inside the same cron window does not advance the combo streak either.
        r1b = cdp.run_cross_dex_paper_tick(client, state_dir=tmp_path, cfg=cfg, now_ms=t0 + 600_000, **quotes)
        assert r1b["state"]["positions"] == {}
        r2 = cdp.run_cross_dex_paper_tick(client, state_dir=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, **quotes)
    pos = r2["state"]["positions"]["NEAR"]
    assert pos["entry_type"] == "basis_carry_combo"
    assert pos["combo_persistence_streak"] == 2


def test_cross_dex_open_pair_reports_exit_cost_and_liquidation_nav(tmp_path: Path):
    t0 = 1_796_100_000_000
    cfg = cdp.CrossDexPaperConfig(combo_min_persistence_ticks=1)
    with httpx.Client() as client:
        r = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0,
            hl_quotes={"NEAR": _q("hyperliquid", "NEAR", 3.0000, -20.0)},
            lighter_quotes={},
            extended_quotes={"NEAR": _q("extended", "NEAR", 3.0060, 0.0)},
            book_fetcher=_near_book,
        )
    st = r["state"]
    pos = st["positions"]["NEAR"]
    expected_exit = 5_000.0 * (cfg.hl_fee_bps + cfg.extended_fee_bps + pos["entry_l2_slip_bps"]) * 1e-4
    assert pos["est_exit_cost_usd"] == pytest.approx(expected_exit, abs=1e-3)
    assert pos["liquidation_net_pnl_usd"] == pytest.approx(pos["net_pnl_usd"] - expected_exit, abs=1e-3)
    assert st["liquidation_equity_usd"] == pytest.approx(st["equity_usd"] - expected_exit, abs=1e-3)
    # Legacy Lighter-only aliases are not written for an Extended leg.
    assert "entry_lt_px" not in pos and "last_lt_px" not in pos


# --------------------------------------------------------------------------- analytics


def _dt(days: list[int]) -> list[datetime]:
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return [base + timedelta(days=d) for d in days]


def test_negative_apy_is_null_not_raw_period_return():
    assert ar._annualize_return_pct(-25.7, 30.0) is None
    assert ar._annualize_return_pct(5.0, 30.0) is not None


def test_drawdown_splits_completed_recovery_from_open_underwater():
    # Peak day 1, trough day 3, never recovered by day 10.
    dates = _dt([0, 1, 2, 3, 10])
    _, rec, under = ar._compute_drawdown_stats(dates, [1.0, 1.1, 1.05, 0.9, 1.0])
    assert rec is None
    assert under == pytest.approx(9.0)
    # Recovered within 0.5 day -> reported as 0.5, not rounded up to a full day.
    d2 = [datetime(2026, 9, 1, tzinfo=timezone.utc) + timedelta(hours=h) for h in (0, 6, 12)]
    _, rec2, under2 = ar._compute_drawdown_stats(d2, [1.0, 0.99, 1.01])
    assert rec2 == pytest.approx(0.5)
    assert under2 == 0.0


def test_monthly_ytd_uses_unrounded_closes_and_is_null_without_year_start():
    dates = _dt([0, 29, 59, 89])  # Sep 1 .. Nov 30, no Jan-1 history
    shares = [1.0, 0.93333, 0.87777, 0.80055]
    rows = ar.compute_monthly_returns(dates, shares)
    assert rows[0]["ytd"] is None
    # With an inception anchor YTD equals the exact all-time return (no compounding of rounded months).
    rows_a = ar.compute_monthly_returns(dates, shares, inception_share=1.0)
    assert rows_a[0]["ytd"] == round((shares[-1] - 1.0) * 100.0, 2)


def test_curve_inception_anchor_prepended():
    d, g, n = ar._with_inception_anchor(["2026-11-27T14:39:36Z", "2026-11-27T18:39:36Z"], [0.999923, 0.999991], None)
    assert g[0] == 1.0 and d[0] == "2026-11-27T14:39:35Z" and n is None
    d2, g2, _ = ar._with_inception_anchor(["2026-11-27", "2026-11-28"], [0.99963, 0.9991], [0.99963, 0.9990])
    assert d2[0] == "2026-11-26" and g2[:2] == [1.0, 0.99963]


def test_sanitize_json_normalizes_negative_zero():
    out = sanitize_json({"ytd": -0.0, "months": {"11": -0.0}})
    assert json.dumps(out) == '{"ytd": 0.0, "months": {"11": 0.0}}'


def test_risk_exposure_leverage_history_metamodel_and_canonical_tiers(tmp_path: Path):
    t0 = 1_796_200_000_000
    cd_dir = tmp_path / "hl_cross_dex_paper"
    cd_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "ts_ms": [t0, t0 + 4 * 3_600_000, t0 + 8 * 3_600_000],
            "equity_usd": [50_000.0, 50_000.0, 50_000.0],
            "gross_notional_usd": [10_000.0, 40_000.0, 50_000.0],
        }
    ).to_parquet(cd_dir / "ticks.parquet", index=False)
    pos = {f"C{i}": {"leg_notional_usd": 5_000.0} for i in range(5)}
    (cd_dir / "state.json").write_text(json.dumps({"equity_usd": 50_000.0, "positions": pos}), encoding="utf-8")

    mm_dir = tmp_path / "hl_metamodel"
    mm_dir.mkdir(parents=True)
    (mm_dir / "state.json").write_text(
        json.dumps({"equity_usd": 100_000.0, "positions": {"BTC": {"hl_weight": 0.3, "cex_weight": -0.3}}}),
        encoding="utf-8",
    )

    ap_dir = tmp_path / "hl_autopilot"
    ap_dir.mkdir(parents=True)
    (ap_dir / "state.json").write_text(
        json.dumps(
            {
                "sleeve_allocations_usd": {"hlp_savings_reserve": 37_500.0, "crisis_cta": 12_500.0},
                "cta_state": {"weights": {"PAXG": -0.16, "BTC": -0.11}},
                "risk_telemetry": {"venue_breakdown_usd": {"HyperCore": 50_000.0}},
            }
        ),
        encoding="utf-8",
    )
    sp_dir = tmp_path / "hl_spot_perp_paper"
    sp_dir.mkdir(parents=True)
    (sp_dir / "state.json").write_text(
        json.dumps({"equity_usd": 100_000.0, "risk_telemetry": {"asset_tier_breakdown_usd": {"HLP Vault Reserve": 1.0}}}),
        encoding="utf-8",
    )

    rx = ar.build_consolidated_risk_exposure(tmp_path)
    rows = {r["strategy"]: r for r in rx["leverage_by_strategy"]}
    cd_row = rows["Cross-DEX (HL×Lighter×Ext)"]
    assert cd_row["current_leverage"] == 1.0
    assert cd_row["leverage_30d_avg"] < 1.0  # 0.2x / 0.8x / 1.0x history, not a copy of current
    assert cd_row["leverage_30d_max"] == 1.0
    assert rows["Metamodel (HL x CEX)"]["current_leverage"] == 0.6

    tiers = {r["label"]: r["usd"] for r in rx["asset_tier_concentration"]}
    assert "HLP / USDC Reserve" not in tiers and "HLP Vault Reserve" not in tiers
    assert tiers["HLP Vault"] == pytest.approx(37_500.0)
    assert tiers["Gold RWA"] == pytest.approx(12_500.0 * 0.16)
    assert tiers["Crypto - Directional CTA"] == pytest.approx(12_500.0 * 0.11)
    assert tiers["Cash / USDC"] == pytest.approx(12_500.0 * (1 - 0.27))


# --------------------------------------------------------------------------- metrics exporter


def test_prom_builder_groups_families_and_drops_duplicate_series():
    b = _PromBuilder()
    b.emit("hl_pos_sz", 1.0, help_text="size", labels={"coin": "AAVE"})
    b.emit("hl_pos_liq", None, help_text="liq", labels={"coin": "AAVE"})  # null on first coin
    b.emit("hl_pos_sz", 2.0, help_text="size", labels={"coin": "INIT"})
    b.emit("hl_pos_liq", 5.0, help_text="liq", labels={"coin": "INIT"})
    b.emit("hl_pos_sz", 9.0, help_text="size", labels={"coin": "INIT"})  # duplicate label set
    lines = b.render().strip().splitlines()
    assert lines == [
        "# HELP hl_pos_sz size",
        "# TYPE hl_pos_sz gauge",
        'hl_pos_sz{coin="AAVE"} 1',
        'hl_pos_sz{coin="INIT"} 2',
        "# HELP hl_pos_liq liq",
        "# TYPE hl_pos_liq gauge",
        'hl_pos_liq{coin="INIT"} 5',
    ]
    assert b.duplicates_dropped == 1


# --------------------------------------------------------------------------- scanners & studies


def test_cascade_bias_counts_5_10pct_walls_and_is_machine_enum():
    """XMR-style wall: $6M of long liquidations at 5-10% only -> downside, not "balanced"."""
    marks = {"XMR": 400.0}
    ch = {
        "assetPositions": [
            {
                "position": {
                    "coin": "XMR",
                    "szi": "15000.0",
                    "positionValue": "6000000.0",
                    "entryPx": "420.0",
                    "liquidationPx": "372.0",  # -7% -> long 5-10% band
                    "leverage": {"type": "cross", "value": 5},
                }
            }
        ]
    }
    positions = lfa.extract_whale_liquidation_positions("0xW", ch, marks, max_dist_pct=25.0)
    xmr = lfa.aggregate_liquidation_clusters(positions, marks)[0]
    assert xmr.long_liq_5_10_usd == pytest.approx(6_000_000.0)
    assert xmr.cascade_bias == lfa.CASCADE_DOWNSIDE == "downside_cascade"
    assert lfa.cascade_bias_label(lfa.CASCADE_BALANCED) == "Сбалансировано"


def test_spot_perp_basis_skips_illiquid_spot_books():
    perps = {"AZTEC": {"mark_px": 1.2868, "funding_hourly": 0.0, "oi_usd": 1e6}}
    spots = {"AZTEC": {"token": "AZTEC", "pair": "AZTEC/USDC", "mid_px": 1.0, "day_vol_usd": 252.0}}
    assert evm.compute_spot_perp_basis(perps, spots) == []
    spots["AZTEC"]["day_vol_usd"] = 1_000_000.0
    rows = evm.compute_spot_perp_basis(perps, spots)
    assert rows and rows[0].strategy_hint == evm.HINT_NEUTRAL


def test_settlement_clustered_t_stat_collapses_simultaneous_events():
    # 10 settlement hours x 20 perfectly correlated coins: naive t sees 200 draws, clustered sees 10.
    rng = np.random.default_rng(0)
    hour_effect = rng.normal(1.0, 3.0, size=10)
    rows = [
        {"t0_ms": h * 3_600_000, "coin": f"C{c}", "x": float(hour_effect[h])} for h in range(10) for c in range(20)
    ]
    df = pd.DataFrame(rows)
    t_cl, n_cl = fss._clustered_t_stat(df, "x")
    naive = df["x"].mean() / (df["x"].std(ddof=1) / np.sqrt(len(df)))
    assert n_cl == 10
    assert abs(t_cl) < abs(naive) / 3.0


def test_mm_calibration_uses_non_overlapping_windows_and_fee_aware_half_spread():
    n = 400
    ts = np.arange(n, dtype=np.int64) * 250 + 1_796_000_000_000  # 100s of 4Hz L2
    mid = 2000.0 * (1.0 + 1e-4 * np.sin(np.arange(n) / 7.0))
    l2 = pd.DataFrame({"recv_ms": ts, "best_bid": mid - 0.05, "best_ask": mid + 0.05})
    cal = mm.calibrate_as_arrival_intensity(l2, None, coin="ETH", window_sec=5.0, maker_fee_bps=1.5)
    assert cal.n_windows <= 20  # 100s / 5s non-overlapping (was ~39 with 50% overlap)
    assert cal.implied_min_half_spread_bps == pytest.approx(cal.adverse_term_bps + 1.5, abs=0.011)
    assert cal.calibration_ok is False  # far below min_windows_ok


# --------------------------------------------------------------------------- PR #24 review follow-ups


def _ap_with_cta(tmp_path: Path, weights: dict[str, float]) -> Path:
    root = tmp_path / "root"
    ap_dir = root / "hl_autopilot"
    ap_dir.mkdir(parents=True)
    (ap_dir / "state.json").write_text(
        json.dumps({"sleeve_allocations_usd": {"crisis_cta": 10_000.0}, "cta_state": {"weights": weights}}),
        encoding="utf-8",
    )
    return root


def test_levered_cta_tier_mix_is_order_independent(tmp_path: Path):
    w = {"BTC": 0.4, "ETH": 0.4, "HYPE": 0.3, "PAXG": -0.3}  # 1.4x gross
    fwd = ar.build_consolidated_risk_exposure(_ap_with_cta(tmp_path / "a", w))
    rev = ar.build_consolidated_risk_exposure(_ap_with_cta(tmp_path / "b", dict(reversed(list(w.items())))))
    t_fwd = {r["label"]: r["usd"] for r in fwd["asset_tier_concentration"]}
    t_rev = {r["label"]: r["usd"] for r in rev["asset_tier_concentration"]}
    assert t_fwd == t_rev
    assert t_fwd["Gold RWA"] == pytest.approx(10_000.0 * 0.3 / 1.4, abs=0.01)
    assert sum(t_fwd.values()) == pytest.approx(10_000.0, abs=0.05)


def test_metamodel_gross_uses_per_venue_legs_and_tick_history(tmp_path: Path):
    pos = {"hl_weight": 0.1, "cex_weight": -0.1, "cex_legs": {"bybit": 0.2, "binance": -0.3}}
    assert ar.metamodel_position_gross(pos) == pytest.approx(0.6)
    assert ar.metamodel_position_gross({"hl_weight": 0.1, "cex_weight": -0.2}) == pytest.approx(0.3)

    mm_dir = tmp_path / "hl_metamodel"
    mm_dir.mkdir(parents=True)
    (mm_dir / "state.json").write_text(json.dumps({"equity_usd": 100_000.0, "positions": {"BTC": pos}}), encoding="utf-8")
    t0 = 1_796_300_000_000
    pd.DataFrame(
        {"tick_hour_ms": [t0, t0 + 3_600_000], "gross_hl": [0.9, 0.1], "gross_cex": [0.9, 0.5]}
    ).to_parquet(mm_dir / "ticks.parquet", index=False)
    row = next(
        r for r in ar.build_consolidated_risk_exposure(tmp_path)["leverage_by_strategy"] if r["strategy"].startswith("Metamodel")
    )
    assert row["current_leverage"] == pytest.approx(0.6)
    assert row["leverage_30d_max"] == pytest.approx(1.8)  # historical peak, not a copy of current


def test_degraded_settlement_run_keeps_published_meta(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib.util

    from typer.testing import CliRunner

    spec = importlib.util.spec_from_file_location(
        "run_settlement_study", Path(__file__).resolve().parents[1] / "scripts/analytics/run_hl_funding_settlement_study.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)

    out = tmp_path / "study"
    out.mkdir()
    published = {"coins_requested": 35, "coins_collected": 35, "total_settlement_events": 18_496, "observed_at_ms": 1}
    (out / "meta.json").write_text(json.dumps(published), encoding="utf-8")
    pd.DataFrame({"x": [1]}).to_parquet(out / "stats.parquet", index=False)
    attempt = {
        "observed_at_ms": 1_796_000_000_000,
        "bar_interval": "5m",
        "coins_requested": 35,
        "coins_collected": 5,
        "coins_failed": [],
        "total_settlement_events": 900,
        "span_days": 17.0,
        "collection_ratio": 0.1429,
        "degraded": True,
    }

    async def fake_fetch(**_kw):
        return pd.DataFrame(), attempt

    monkeypatch.setattr(mod.fss, "fetch_settlement_dataset", fake_fetch)
    res = CliRunner().invoke(
        mod.app, ["--output-dir", str(out), "--report-path", str(tmp_path / "r.md")], catch_exceptions=False
    )
    assert res.exit_code == 2
    assert json.loads((out / "meta.json").read_text()) == published
    assert json.loads((out / "last_attempt.json").read_text())["coins_collected"] == 5
    assert (out / "stats.parquet").is_file()
