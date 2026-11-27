"""Regression tests for the 2026-11-27 comprehensive audit fixes."""

from __future__ import annotations

import json
import math
from pathlib import Path

import httpx
import numpy as np
import pytest

from hl_quant import hl_testnet_executor as hte
from hl_quant import metamodel, paper_tracker, publication
from hl_quant.scheduler import WorkerScheduler


def test_run_id_is_single_path_component_for_spot_symbols() -> None:
    rid = publication.run_id_for_params(
        "candles", {"symbol": "HYPE/USDC", "interval": "1h", "start_ms": 1, "end_ms": 2}, base_url="x"
    )
    assert "/" not in rid and ".." not in rid
    assert rid.startswith("HYPE-USDC_1h_1_2_")


def test_verify_run_rejects_manifest_paths_escaping_run_dir(tmp_path: Path) -> None:
    outside = tmp_path / "secret.parquet"
    outside.write_bytes(b"x")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    manifest = {"files": {"../secret.parquet": publication.file_sha256(outside)}}
    (run_dir / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(publication.ManifestCorruptError, match="escapes"):
        publication.verify_run(run_dir)


def test_normalize_weights_infeasible_cap_falls_back_to_equal_weight() -> None:
    w = metamodel._normalize_weights(np.array([0.9, 0.1]), max_weight=0.40)
    assert np.allclose(w, [0.5, 0.5])
    w_nan = metamodel._normalize_weights(np.array([np.nan, 1.0, 1.0]), max_weight=0.40)
    assert np.all(np.isfinite(w_nan)) and math.isclose(float(w_nan.sum()), 1.0)


def test_kill_switch_trips_on_non_finite_snapshot() -> None:
    acct = hte.AccountSnapshot(
        equity_usd=float("nan"), free_margin_usd=100.0, positions_sz={}, open_orders=[], observed_at_ms=0
    )
    reasons = hte.check_kill_switch(acct, {}, {}, hte.RiskLimits(), high_water_mark_usd=1_000.0, now_ms=0)
    assert reasons == ["non_finite_account_snapshot"]


def test_update_high_water_mark_tolerates_null_json(tmp_path: Path) -> None:
    p = tmp_path / "hwm.json"
    p.write_text("null")
    assert hte.update_high_water_mark(p, 123.0) == 123.0
    assert json.loads(p.read_text())["high_water_mark_usd"] == 123.0


def test_telegram_http_error_does_not_leak_bot_token() -> None:
    transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"ok": False}))
    with pytest.raises(RuntimeError) as exc:
        paper_tracker.send_telegram_message("hi", bot_token="SECRET123:abc", chat_id="1", transport=transport)
    assert "SECRET123" not in str(exc.value)


def test_bootstrap_sentinels_cover_spot_perp_and_autopilot(tmp_path: Path) -> None:
    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=lambda *a, **k: 0)
    assert sched._bootstrap_sentinel_for("spot_perp_paper") is not None
    assert sched._bootstrap_sentinel_for("autopilot_allocator") is not None


def test_recovery_alert_fires_for_job_restored_in_failed_state(tmp_path: Path) -> None:
    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=lambda *a, **k: 0)
    job = next(j for j in sched.jobs if j.readonly and j.is_enabled()[0])
    sched._state[job.name]["state"] = "failed"
    seen: list[bool] = []
    sched._maybe_send_job_alert = lambda **kw: seen.append(kw["was_failed"])  # type: ignore[method-assign]
    assert sched.run_job(job) == 0
    assert seen == [True]


# --------------------------------------------------------------------------- Live-dump audit (2026-11-27, part 2)

from datetime import datetime, timedelta, timezone  # noqa: E402

from hl_quant import cross_dex_scanner as cds  # noqa: E402
from hl_quant import hl_analytics_risk as ar  # noqa: E402
from hl_quant import hl_autopilot as ap  # noqa: E402
from hl_quant import hl_post_trade_attribution as pta  # noqa: E402
from hl_quant.persistence_clock import advance_streak, streak_count, update_streak_map  # noqa: E402


def _intraday(n: int, minutes: int = 25) -> list[str]:
    t0 = datetime(2026, 11, 27, 14, 30, tzinfo=timezone.utc)
    return [(t0 + timedelta(minutes=minutes * i)).strftime("%Y-%m-%dT%H:%M:%SZ") for i in range(n)]


def test_days_tracked_counts_calendar_days_not_ticks() -> None:
    row = ar.compute_series_performance(
        strategy_id="autopilot", name="AP", category="x", dates=_intraday(5),
        gross_shares=[0.9999, 1.0001, 0.9998, 0.9997, 0.9996], inception_share=1.0,
    )
    assert row["days_tracked"] == 1
    assert row["observations"] == 5
    # Intraday ticks must never produce positive-days % or loss streaks
    assert row["positive_days_pct"] is None
    assert row["longest_loss_streak"] == 0


def test_all_time_anchored_to_inception_and_no_apy_on_short_history() -> None:
    # hip3_paper: first persisted tick is already after entry fees (0.999923); real equity < start
    row = ar.compute_series_performance(
        strategy_id="hip3_paper", name="H", category="x", dates=_intraday(2, minutes=145),
        gross_shares=[0.999923, 0.999991], inception_share=1.0,
    )
    assert row["gross_returns"]["all_time"] == pytest.approx(-0.0, abs=0.005)
    assert row["gross_returns"]["all_time"] <= 0.0
    assert row["gross_apy"]["all_time"] is None  # < MIN_APY_DAYS -> no annualization
    assert row["net_apy"]["all_time"] is None
    assert row["max_dd_gross_pct"] > 0.0  # drawdown vs inception 1.0 is visible


def test_benchmark_ytd_is_none_without_jan_1_history_and_7d_apy_suppressed() -> None:
    base = datetime(2026, 8, 30, tzinfo=timezone.utc)
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(90)]
    shares = [1.0 + 0.002 * i for i in range(90)]
    row = ar.compute_series_performance(
        strategy_id="bench_hype", name="HYPE", category="Benchmark", dates=dates,
        gross_shares=shares, net_shares=shares, is_benchmark=True, mgmt_fee_pct=0.0, perf_fee_pct=0.0,
    )
    assert row["gross_returns"]["ytd"] is None
    assert row["gross_apy"]["7d"] is None
    assert row["gross_apy"]["30d"] is not None


def test_asset_tier_concentration_uses_autopilot_portfolio(tmp_path: Path) -> None:
    (tmp_path / "hl_autopilot").mkdir()
    (tmp_path / "hl_spot_perp_paper").mkdir()
    (tmp_path / "hl_autopilot" / "state.json").write_text(json.dumps({
        "sleeve_allocations_usd": {"spot_perp_paper": 87_500.0, "hlp_savings_reserve": 37_500.0,
                                   "cross_dex_paper": 62_500.0, "hip3_paper": 50_000.0, "crisis_cta": 12_500.0},
        "risk_telemetry": {"venue_breakdown_usd": {"HyperCore": 250_000.0}},
    }))
    (tmp_path / "hl_spot_perp_paper" / "state.json").write_text(json.dumps({
        "equity_usd": 100_000.0,
        "risk_telemetry": {"asset_tier_breakdown_usd": {"Crypto - Large Cap": 40_000.0, "Gold RWA": 10_000.0}},
    }))
    risk = ar.build_consolidated_risk_exposure(tmp_path)
    tier_total = sum(r["usd"] for r in risk["asset_tier_concentration"])
    venue_total = sum(r["usd"] for r in risk["venue_concentration"])
    assert tier_total == pytest.approx(250_000.0, abs=1.0)
    assert tier_total == pytest.approx(venue_total, abs=1.0)


def test_streak_does_not_advance_on_rapid_reruns() -> None:
    t0 = 1_795_700_000_000
    v = advance_streak(None, qualifies=True, now_ms=t0)
    for k in range(1, 6):  # 5 manual re-runs within 30 minutes
        v = advance_streak(v, qualifies=True, now_ms=t0 + k * 6 * 60_000)
    assert streak_count(v) == 1
    v = advance_streak(v, qualifies=True, now_ms=t0 + 4 * 3_600_000)
    assert streak_count(v) == 2
    m = update_streak_map({"A": 3}, ["A"], now_ms=t0)  # legacy int migrates
    assert streak_count(m["A"]) == 4


def test_credited_basis_never_exceeds_mark_basis() -> None:
    # HYPE@extended: mark basis -3.57 bps, book claims +25.92 bps -> credit is the mark basis
    assert cds.credited_entry_basis_bps(25.92, -3.57) == pytest.approx(-3.57)
    assert cds.credited_entry_basis_bps(5.0, 9.0) == pytest.approx(5.0)
    hb, ha = [(100.0, 1e3)], [(100.02, 1e3)]
    ob, oa = [(99.98, 1e3)], [(100.0, 1e3)]
    assert cds.compute_book_mid_basis_bps(hb, ha, ob, oa, short_hl=True) == pytest.approx(2.0, abs=0.01)


def test_cta_opens_small_targets_and_charges_entry_cost(tmp_path: Path) -> None:
    snap = ap.compute_crisis_cta_weights(
        {"ETH": [100.0 * (0.99 ** i) for i in range(55)]}, prev_weights={"ETH": 0.0}
    )
    assert snap["weights"]["ETH"] < 0.0  # target ~-0.08 opens despite 0.10 deadband
    assert snap["turnover"] > 0.0


def test_perf_fee_accrues_intra_quarter_and_crystallizes_next_quarter(tmp_path: Path) -> None:
    sp = tmp_path / "hl_spot_perp_paper"
    sp.mkdir()
    (sp / "state.json").write_text(json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}}))
    closes = {c: [100.0] * 50 for c in ap.CTA_UNIVERSE}
    cfg = ap.AutopilotConfig()
    t0 = int(datetime(2026, 9, 30, 12, tzinfo=timezone.utc).timestamp() * 1000)
    ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0, daily_closes=closes)
    (sp / "state.json").write_text(json.dumps({"equity_usd": 101_000.0, "starting_capital_usd": 100_000.0, "positions": {}}))
    r2 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, daily_closes=closes)
    assert r2["state"]["cum_perf_fee_usd"] == 0.0 and r2["state"]["accrued_perf_fee_usd"] > 0.0
    t_q4 = int(datetime(2026, 10, 1, 4, tzinfo=timezone.utc).timestamp() * 1000)
    r3 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t_q4, daily_closes=closes)
    assert r3["state"]["cum_perf_fee_usd"] == pytest.approx(r2["state"]["accrued_perf_fee_usd"], abs=1e-3)
    assert r3["state"]["hwm_net_share_price"] > 1.0


def test_markout_uses_fixed_horizon_not_current_mid() -> None:
    fill_ms = 1_795_700_000_000
    snapshot = {
        "fills": [{"coin": "INIT", "px": "0.13561", "sz": "460", "side": "A", "oid": 1, "time": fill_ms, "fee": "0"}],
        "all_mids": {"INIT": 0.129825},  # hours later: big directional drift
        "markout_series": {"INIT": [(fill_ms + 60_000, 0.13560), (fill_ms + 120_000, 0.13559)]},
        "clearinghouse": {}, "spot": {}, "open_orders": [], "funding": [],
    }
    attr = pta.compute_post_trade_attribution(snapshot)
    assert attr.vw_mark_drift_bps == pytest.approx(426.59, abs=0.05)
    assert attr.vw_markout_bps == pytest.approx(0.74, abs=0.05)  # true 60s markout is tiny
