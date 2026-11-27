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


def test_degraded_settlement_run_suppresses_corrupted_telegram_push(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import importlib.util

    from typer.testing import CliRunner

    spec = importlib.util.spec_from_file_location(
        "run_settlement_study_tg", Path(__file__).resolve().parents[1] / "scripts/analytics/run_hl_funding_settlement_study.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(mod)

    sent: list[str] = []
    monkeypatch.setattr(mod.pt, "load_telegram_credentials", lambda *_a, **_kw: ("tok", "chat", None))
    monkeypatch.setattr(mod.pt, "send_telegram_rich_message", lambda html, **_kw: sent.append(html) or {"ok": True})

    async def fake_fetch(**_kw):
        return pd.DataFrame(), {
            "observed_at_ms": 1_796_000_000_000,
            "bar_interval": "15m",
            "coins_requested": 35,
            "coins_collected": 15,
            "coins_failed": ["X"],
            "total_settlement_events": 1000,
            "span_days": 52.0,
            "collection_ratio": 15 / 35,
            "degraded": True,
        }

    monkeypatch.setattr(mod.fss, "fetch_settlement_dataset", fake_fetch)
    res = CliRunner().invoke(
        mod.app,
        ["--output-dir", str(tmp_path / "s"), "--report-path", str(tmp_path / "r.md"), "--telegram"],
        catch_exceptions=False,
    )
    assert res.exit_code == 2
    assert sent == []  # never broadcast unrepresentative degraded stats to Telegram


def test_testnet_orphan_neutralization_killswitch_and_sub10_reduce_only_close():
    from hl_quant import hl_testnet_executor as ex
    from hl_quant.api.app import _audit_record

    # 1. Dollar neutralization offsets signed orphan exposure (+0.07 net long orphan -> quoted sum == -0.07)
    neut = ex.neutralize_target_weights({"BTC": 0.20, "ETH": -0.10, "SOL": -0.05}, max_gross=0.80, fixed_net_weight=0.07)
    assert sum(neut.values()) + 0.07 == pytest.approx(0.0, abs=1e-4)

    # 2. Unquoted orphan notional counts toward gross leverage kill-switch
    now = 1_796_000_000_000
    acct = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=800.0,
        positions_sz={"VVV": 1.7, "JUP": 30.0},
        open_orders=[],
        observed_at_ms=now,
        position_notional_usd={"VVV": 700.0, "JUP": 7.5},
    )
    bbo = {"BTC": (60_000.0, 60_002.0, now), "JUP": (0.249, 0.251, now)}  # VVV unquoted!
    violations = ex.check_kill_switch(
        acct, {"BTC": 0.90}, bbo, ex.RiskLimits(max_gross_leverage=1.50), high_water_mark_usd=1_000.0, now_ms=now
    )
    assert any(v.startswith("gross_leverage:") for v in violations)  # 0.90 + 0.70 = 1.60x > 1.50x

    # 3. Sub-$10 dust position (`JUP` $7.50) with target_w == 0 is closed via reduce_only=True,
    #    and unquoted held `VVV` (target_w == 0) is surfaced in `orphan_positions` + preserved by API `_audit_record`.
    meta = {
        "BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40),
        "JUP": ex.AssetSpec("JUP", sz_decimals=0, max_leverage=10),
        "VVV": ex.AssetSpec("VVV", sz_decimals=2, max_leverage=5),
    }
    adapter = ex.DryRunAdapter()
    cyc = ex.execute_rebalance_cycle(acct, {"BTC": 0.10}, meta, bbo, adapter, now_ms=now)
    assert cyc["status"] == "ok"
    assert cyc["orphan_positions"] == {"VVV": 1.7}
    jup_orders = [o for o in adapter.placed if o["coin"] == "JUP"]
    assert len(jup_orders) == 1 and jup_orders[0]["reduce_only"] is True and jup_orders[0]["side"] == "sell"
    api_rec = _audit_record({"ts_ms": now, **cyc})
    assert api_rec["orphan_positions"] == {"VVV": 1.7}


def test_autopilot_drops_unclosed_intraday_daily_candle():
    from hl_quant import hl_autopilot as ap

    now = 1_796_040_000_000
    bars = [
        {"t": now - 2 * 86_400_000, "T": now - 86_400_000 - 1, "c": "60000"},
        {"t": now - 86_400_000, "T": now - 1, "c": "61000"},
        {"t": now, "T": now + 40_000_000, "c": "99999"},  # still-forming intraday 1d candle!
    ]
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, json=bars))
    with httpx.Client(transport=transport) as client:
        out = ap.fetch_hypercore_daily_closes_with_dates(client, ("BTC",), lookback_days=10, now_ms=now)
    assert [r["close"] for r in out["BTC"]] == [60000.0, 61000.0]


def test_analytics_daily_resample_ratios_single_down_sortino_and_net_dd():
    # 6 calendar days x 6 intraday 4h ticks = 36 points, with exactly 1 losing day
    base = datetime(2026, 11, 1, 0, 0, tzinfo=timezone.utc)
    dates: list[str] = []
    shares: list[float] = []
    day_closes = [1.002, 1.004, 1.003, 1.006, 1.009, 1.008]  # day 3 dips, day 6 slightly below day 5 peak
    for d_idx, dc in enumerate(day_closes):
        for h in (0, 4, 8, 12, 16, 20):
            dates.append((base + timedelta(days=d_idx, hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ"))
            shares.append(dc)
    row = ar.compute_series_performance(
        strategy_id="sp",
        name="Spot+Perp",
        category="Market Neutral",
        dates=dates,
        gross_shares=shares,
        mgmt_fee_pct=2.0,
        perf_fee_pct=20.0,
        inception_share=1.0,
    )
    assert row["sortino_gross"] is not None and row["sortino_gross"] != 99.0
    assert row["current_underwater_net_days"] is not None
    # Day-1 management fee is charged on the inception -> first tick interval
    net_series = ar.simulate_net_series_from_inception(
        ["2026-11-01", "2026-11-02"], [1.0, 1.0], inception_share=1.0, mgmt_fee_annual_pct=3.65, perf_fee_pct=0.0
    )
    assert net_series[0] < 1.0


def test_dashboard_parquet_reader_returns_latest_tail_and_invalidates_cache(tmp_path: Path):
    from hl_quant import hl_portfolio_dashboard as pdash

    p = tmp_path / "quotes.parquet"
    pd.DataFrame({"seq": [1, 2, 3, 4, 5]}).to_parquet(p, index=False)
    tail2 = pdash._read_parquet_records(p, limit=2)
    assert [r["seq"] for r in tail2] == [4, 5]
    head2 = pdash._read_parquet_records(p, limit=2, head=True)
    assert [r["seq"] for r in head2] == [1, 2]

    pd.DataFrame({"seq": [1, 2, 3, 4, 5, 6]}).to_parquet(p, index=False)
    tail2_updated = pdash._read_parquet_records(p, limit=2)
    assert [r["seq"] for r in tail2_updated] == [5, 6]


def test_metrics_autopilot_weighted_apr_hlp_zero_ret_and_scheduler_restart_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from hl_quant.metrics import render_prometheus_metrics
    from hl_quant.scheduler import WorkerScheduler

    overview = {
        "frontier_history": {"hlp_apr_pct": 14.36},
        "onchain_events": {"hlp_tvl_usd": 152_000_000.0},
        "spot_perp_paper": {"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "blended_portfolio_apr_pct": 4.0},
        "cross_dex_paper": {
            "equity_usd": 50_000.0,
            "starting_capital_usd": 50_000.0,
            "positions": {"A": {"leg_notional_usd": 25_000.0, "live_spread_apr_pct": 42.12}},  # 21.06% on $50k
        },
        "hip3_paper": {
            "equity_usd": 50_000.0,
            "starting_capital_usd": 50_000.0,
            "positions": {"NVDA": {"leg_notional_usd": 25_000.0, "live_spread_apr_pct": 48.68}},  # 24.34% on $50k
        },
        "autopilot": {
            "gross_equity_usd": 250_000.0,
            "net_equity_usd": 250_000.0,
            "starting_capital_usd": 250_000.0,
            "sleeves": [
                {"name": "spot_perp_paper", "category": "MN", "actual_weight_pct": 35.0, "allocation_usd": 87_500.0},
                {"name": "cross_dex_paper", "category": "MN", "actual_weight_pct": 25.0, "allocation_usd": 62_500.0},
                {"name": "hip3_paper", "category": "MN", "actual_weight_pct": 20.0, "allocation_usd": 50_000.0},
                {"name": "hlp_savings_reserve", "category": "Res", "actual_weight_pct": 15.0, "allocation_usd": 37_500.0},
                {"name": "crisis_cta", "category": "CTA", "actual_weight_pct": 5.0, "allocation_usd": 12_500.0},
            ],
        },
    }
    txt = render_prometheus_metrics(overview)
    # 0.35*4.0 + 0.25*21.06 + 0.20*24.34 + 0.15*14.36 = 13.687 -> 13.69% (never 4.0%!)
    assert 'hl_strategy_live_apr_pct{strategy="neutral_autopilot",category="Master Vault",mode="paper"} 13.69' in txt
    assert 'hl_strategy_return_pct{strategy="hlp_benchmark",category="Benchmark",mode="mainnet"} 0' in txt

    # Worker restart within the same UTC minute (`08:20:45` after starting at `08:20:05`) must not re-fire the job
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    status_path = tmp_path / "paper" / "hl_worker_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "service": "hl-worker",
                "jobs": [
                    {
                        "name": "frontier_persistence",
                        "state": "ok",
                        "last_started_at_utc": "2026-11-26 08:20:05 UTC",
                        "last_exit_code": 0,
                        "runs_ok": 1,
                        "runs_failed": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    ran: list[list[str]] = []
    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=lambda c, _e, _t: ran.append(c) or 0)
    assert sched.tick_minute(datetime(2026, 11, 26, 8, 20, 45, tzinfo=timezone.utc)) == []
    assert ran == []


def test_zero_external_monorepo_env_paths_in_src_and_analytics_scripts():
    repo_root = Path(__file__).resolve().parents[1]
    for folder in (repo_root / "src", repo_root / "scripts" / "analytics"):
        for py in folder.rglob("*.py"):
            content = py.read_text(encoding="utf-8")
            assert "ResearchLAB" not in content, f"Forbidden ResearchLAB dependency in {py}"
            assert "BackTesterv2" not in content, f"Forbidden BackTesterv2 dependency in {py}"
