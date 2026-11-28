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


def test_load_high_water_mark_tolerates_null_json(tmp_path: Path) -> None:
    p = tmp_path / "hwm.json"
    p.write_text("null")
    assert hte.load_high_water_mark(p) == 0.0
    hte.save_high_water_mark(p, 123.0)
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


def test_markout_series_drops_unfinished_candle() -> None:
    now = 1_795_700_000_000
    bars = [{"T": now - 1, "c": "1.0"}, {"T": now + 30_000, "c": "2.0"}]
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=bars))
    with httpx.Client(transport=transport) as c:
        series = pta.fetch_markout_series(c, [{"coin": "X", "time": now - 10_000}], now_ms=now)
    assert series == {"X": [(now - 1, 1.0)]}


def test_fill_ownership_uses_full_audit_set_when_fill_window_capped(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    old_ts, new_ts = 1_000, 10_000_000
    rec = lambda ts, oid: json.dumps({"ts_ms": ts, "orders": [{"response": {"response": {"data": {"statuses": [{"resting": {"oid": oid}}]}}}}]})  # noqa: E731
    audit.write_text(rec(old_ts, 1) + "\n" + rec(new_ts, 2) + "\n")
    fills = [{"coin": "BTC", "px": "100", "sz": "1", "side": "B", "oid": 1, "time": new_ts + i, "fee": "0"} for i in range(1900)]
    fills.append({"coin": "BTC", "px": "100", "sz": "1", "side": "B", "oid": 2, "time": new_ts + 5000, "fee": "0"})
    snap = {"fills": fills, "all_mids": {}, "clearinghouse": {}, "spot": {}, "open_orders": [], "funding": []}
    attr = pta.compute_post_trade_attribution(snap, audit_log_path=audit)
    assert attr.audit_fill_volume_usd == pytest.approx(190_100.0)
    assert attr.mixed_account_warning == ""


def test_ops_lock_skips_when_held(tmp_path: Path) -> None:
    import fcntl
    import os
    import sys

    from hl_quant import ops_lock

    lock = tmp_path / "x.flock"
    marker = tmp_path / "ran"
    cmd = [sys.executable, "-c", f"open({str(marker)!r}, 'w').write('1')"]
    fd = os.open(lock, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert ops_lock.run_locked(lock, cmd) == 0
        assert not marker.exists()
    finally:
        os.close(fd)
    assert ops_lock.run_locked(lock, cmd) == 0
    assert marker.exists()


def test_ops_lock_survives_guard_death(tmp_path: Path) -> None:
    import fcntl
    import os
    import signal
    import subprocess
    import sys
    import time

    lock = tmp_path / "y.flock"
    src = str(Path(__file__).resolve().parents[1] / "src")
    guard = subprocess.Popen(
        [sys.executable, "-m", "hl_quant.ops_lock", str(lock), "--", sys.executable, "-c", "import time; time.sleep(3)"],
        env={**os.environ, "PYTHONPATH": src},
    )
    time.sleep(0.8)
    guard.send_signal(signal.SIGKILL)
    guard.wait()
    fd = os.open(lock, os.O_RDWR)
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # child still holds the lock
    finally:
        os.close(fd)


def test_cross_dex_long_hl_book_and_mark_basis_share_denominator() -> None:
    hb, ha = [(99.99, 1e3)], [(100.01, 1e3)]
    ob, oa = [(102.99, 1e3)], [(103.01, 1e3)]
    book = cds.compute_book_mid_basis_bps(hb, ha, ob, oa, short_hl=False)
    mark = (103.0 - 100.0) / 100.0 * 1e4
    assert book == pytest.approx(mark, abs=0.01)


def test_autopilot_nav_guard_does_not_poison_sleeve_or_cta_baselines(tmp_path: Path) -> None:
    from hl_quant import hl_autopilot as ap

    sp = tmp_path / "hl_spot_perp_paper"
    sp.mkdir()
    (sp / "state.json").write_text(
        json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}})
    )
    closes = {c: [100.0] * 50 for c in ap.CTA_UNIVERSE}
    cfg = ap.AutopilotConfig(max_tick_nav_jump_pct=3.0)
    t0 = 1_795_700_000_000
    r1 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0, daily_closes=closes)
    base_eq = r1["state"]["gross_equity_usd"]

    # Anomalous +50% upstream spike should trip NAV guard and NOT overwrite last_sleeve_equities or cta last_closes
    (sp / "state.json").write_text(
        json.dumps({"equity_usd": 150_000.0, "starting_capital_usd": 100_000.0, "positions": {}})
    )
    spike_closes = {c: [100.0] * 49 + [200.0] for c in ap.CTA_UNIVERSE}
    r2 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, daily_closes=spike_closes)
    assert r2["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r2["state"]["last_sleeve_equities"]["spot_perp_paper"] == pytest.approx(100_000.0)
    assert r2["state"]["cta_state"]["last_closes"]["BTC"] == pytest.approx(100.0)

    # Next tick when upstream feed returns to normal should NOT book a phantom crash
    (sp / "state.json").write_text(
        json.dumps({"equity_usd": 100_100.0, "starting_capital_usd": 100_000.0, "positions": {}})
    )
    r3 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 8 * 3_600_000, daily_closes=closes)
    assert r3["state"]["risk_telemetry"]["nav_guard_triggered"] is False
    assert r3["state"]["gross_equity_usd"] >= base_eq


def test_spot_perp_nav_guard_reverts_mutated_position_equity(tmp_path: Path) -> None:
    from hl_quant import hl_spot_perp_arb as spa

    markets = [
        {
            "base_symbol": "HYPE",
            "spot_pair": "@107",
            "spot_token": "HYPE",
            "quote_token": "USDC",
            "is_proxy_hedge": False,
            "spot_mid_px": 35.0,
            "spot_day_vol_usd": 10_000_000.0,
            "perp_mark_px": 20.0,
            "perp_oracle_px": 20.0,
            "funding_rate_h": 0.0002,
            "perp_day_vol_usd": 50_000_000.0,
            "perp_oi_usd": 20_000_000.0,
            "only_isolated": False,
            "max_leverage": 5,
        }
    ]

    def fake_book(coin_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        px = 35.0 if coin_key == "@107" else 20.0
        return [(px * 0.9999, 10_000.0)], [(px * 1.0001, 10_000.0)]

    state_dir = tmp_path / "sp"
    state_dir.mkdir()
    seed_state = {
        "starting_capital_usd": 100_000.0,
        "equity_usd": 100_000.0,
        "hlp_reserve_usd": 10_000.0,
        "free_cash_usd": 60_000.0,
        "hlp_apr_pct": 12.0,
        "last_tick_ms": 1_795_700_000_000,
        "ticks_count": 1,
        "positions": {
            "HYPE": {
                "base_symbol": "HYPE",
                "spot_pair": "@107",
                "qty": 1000.0,
                "entry_spot_px": 20.0,
                "entry_perp_px": 20.0,
                "last_spot_px": 20.0,
                "last_perp_px": 20.0,
                "spot_notional_usd": 20_000.0,
                "perp_notional_usd": 20_000.0,
                "perp_margin_usd": 10_000.0,
                "entry_cost_usd": 6.0,
                "cum_cost_usd": 6.0,
                "cum_funding_usd": 0.0,
                "cum_basis_mtm_usd": 0.0,
                "hours_held": 4.0,
            }
        },
    }
    (state_dir / "state.json").write_text(json.dumps(seed_state))

    # Corrupt spot price spike (20 -> 35 while perp stays 20 => +$15k / +15% basis jump > 3% guard)
    cfg = spa.SpotPerpPaperConfig(max_tick_nav_jump_pct=3.0)
    res = spa.run_spot_perp_paper_tick(
        state_dir=state_dir,
        cfg=cfg,
        now_ms=1_795_700_000_000 + 4 * 3_600_000,
        markets=markets,
        book_fetcher=fake_book,
        hlp_summary=(12.0, 250_000_000.0),
    )
    st = res["state"]
    assert st["risk_telemetry"]["nav_guard_triggered"] is True
    assert st["equity_usd"] == pytest.approx(100_000.0, abs=1.0)
    assert st["positions"]["HYPE"]["last_spot_px"] == pytest.approx(20.0)


def test_execution_stress_short_series_and_initial_dd() -> None:
    import pandas as pd

    from hl_quant import execution_stress as es

    idx = pd.Index([1, 2, 3], name="hour_ms")
    ret_df = pd.DataFrame({"BTC": [-0.10, -0.05, 0.02]}, index=idx)
    w_df = pd.DataFrame({"BTC": [1.0, 1.0, 1.0]}, index=idx)
    rows = es.margin_buffer_audit(ret_df, w_df, leverages=(1.0,))
    assert math.isfinite(rows[0]["worst_72h_single_leg_pct"])
    assert rows[0]["max_single_leg_dd_pct"] == pytest.approx(-15.0, abs=0.01)

    empty_mc = es.run_execution_monte_carlo(
        pd.Series(dtype=float),
        pd.Series(dtype=float),
        pd.Series(dtype=float),
        n_paths=8,
    )
    assert len(empty_mc) == len(es.DEFAULT_SCENARIOS)


def test_dashboard_and_tear_sheet_tolerate_null_fields() -> None:
    from hl_quant import hl_portfolio_dashboard as dash
    from hl_quant import hl_tear_sheet as ts

    payload = {
        "generated_at_utc": "2026-11-27 12:00 UTC",
        "attribution": {"equity_usd": None, "maker_volume_share_pct": None, "effective_fee_bps": None},
        "mm_session": {"summary": None},
        "mm_calibration": {"BTC": {"kappa_per_bp": None, "r2_fit": None}, "meta": "skip"},
        "boros": {"markets": [{"market_id": "1", "name": "HYPE", "best_bid_apr_pct": None, "best_ask_apr_pct": None}]},
        "paper_ticks": [{"equity_usd": None}, {"equity_usd": float("nan")}, {"equity_usd": 100_000.0}],
        "hyperevm": {"vaults": [{"tvl_usd": None, "apr_pct": None}]},
        "latency_bench": {"p50_us": None, "throughput_ticks_per_sec": None},
    }
    html_out = dash.render_portfolio_dashboard_html(payload)
    assert "nan,nan" not in html_out.lower()
    tg_out = ts.format_tear_sheet_telegram_rich_html(payload)
    assert "Maker —" in tg_out
    md_out = ts.format_tear_sheet_markdown(payload)
    assert "κ=0.000" in md_out


def test_cross_dex_and_hip3_paper_mtm_uses_fixed_coin_qty_round_trip(tmp_path: Path) -> None:
    """Round-trip price path (100 -> 200 / 180 -> 100) must net to 0 basis MTM under fixed coin qty."""
    from hl_quant import cross_dex_scanner as cds
    from hl_quant import hl_cross_dex_paper as cdp
    from hl_quant import hl_hip3_builder_arb as hip3
    from hl_quant import hl_hip3_paper as hp

    def q(dex: str, px: float, apr: float) -> cds.DexMarketQuote:
        return cds.DexMarketQuote(
            dex=dex,
            coin="SOL",
            raw_symbol="SOL",
            mark_px=px,
            funding_rate_h=apr / (cds.HOURS_PER_YEAR * 100.0),
            funding_apr_pct=apr,
            day_vol_usd=20_000_000.0,
            oi_usd=10_000_000.0,
            spread_bps=1.0,
        )

    t0 = 1_795_700_000_000
    cd_dir = tmp_path / "cd"
    cfg_cd = cdp.CrossDexPaperConfig(slot_notional_usd=5_000.0, min_persistence_ticks=1, entry_apr_pct=15.0)
    with httpx.Client() as client:
        cdp.run_cross_dex_paper_tick(
            client,
            state_dir=cd_dir,
            cfg=cfg_cd,
            now_ms=t0,
            hl_quotes={"SOL": q("hyperliquid", 100.0, 25.0)},
            lighter_quotes={"SOL": q("lighter", 100.0, 0.0)},
            book_fetcher=lambda _d, _s: ([(99.99, 500.0)], [(100.01, 500.0)]),
        )
        # Asymmetric surge (HL 100->200, Lighter 100->180)
        r_mid = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=cd_dir,
            cfg=cfg_cd,
            now_ms=t0 + 4 * 3_600_000,
            hl_quotes={"SOL": q("hyperliquid", 200.0, 25.0)},
            lighter_quotes={"SOL": q("lighter", 180.0, 0.0)},
            book_fetcher=lambda _d, _s: ([(189.9, 500.0)], [(190.1, 500.0)]),
        )
        # Short 50 SOL on HL (-$5,000) + Long 50 SOL on Lighter (+$4,000) => exact -$1,000 basis MTM
        assert r_mid["state"]["positions"]["SOL"]["cum_basis_mtm_usd"] == pytest.approx(-1_000.0, abs=0.01)
        # Return both legs to 100.0 -> cumulative basis MTM must return to exact 0.0!
        r_end = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=cd_dir,
            cfg=cfg_cd,
            now_ms=t0 + 8 * 3_600_000,
            hl_quotes={"SOL": q("hyperliquid", 100.0, 25.0)},
            lighter_quotes={"SOL": q("lighter", 100.0, 0.0)},
            book_fetcher=lambda _d, _s: ([(99.99, 500.0)], [(100.01, 500.0)]),
        )
        assert r_end["state"]["positions"]["SOL"]["cum_basis_mtm_usd"] == pytest.approx(0.0, abs=0.01)

    # Same invariant for HIP-3 paper tracker
    def mkt(dex: str, px: float, apr: float) -> hip3.Hip3BuilderMarket:
        return hip3.Hip3BuilderMarket(
            dex=dex,
            dex_full_name=dex,
            base_symbol="NVDA",
            full_symbol=f"{dex}:NVDA",
            mark_px=px,
            oracle_px=px,
            basis_vs_oracle_bps=0.0,
            funding_rate_h=apr / (hip3.HOURS_PER_YEAR * 100.0),
            funding_apr_pct=apr,
            day_vol_usd=5_000_000.0,
            oi_usd=2_000_000.0,
            max_leverage=10,
            only_isolated=False,
            growth_mode=True,
            deployer_fee_scale=0.2,
            est_maker_fee_bps=0.3,
            est_taker_fee_bps=0.9,
        )

    h3_dir = tmp_path / "h3"
    cfg_h3 = hp.Hip3PaperConfig(
        slot_notional_usd=5_000.0, min_clean_streak=1, entry_apr_pct=15.0, exit_oracle_div_bps=5000.0,
        max_payback_hours=48.0,  # exit slip is charged twice in payback since 2026-11-28
    )
    hp.run_hip3_paper_tick(
        state_dir=h3_dir,
        cfg=cfg_h3,
        now_ms=t0,
        markets=[mkt("io", 100.0, 40.0), mkt("xyz", 100.0, 5.0)],
        book_fetcher=lambda _s: ([(99.99, 500.0)], [(100.01, 500.0)]),
    )
    hp.run_hip3_paper_tick(
        state_dir=h3_dir,
        cfg=cfg_h3,
        now_ms=t0 + 4 * 3_600_000,
        markets=[mkt("io", 200.0, 40.0), mkt("xyz", 180.0, 5.0)],
        book_fetcher=lambda _s: ([(189.9, 500.0)], [(190.1, 500.0)]),
    )
    r_h3_end = hp.run_hip3_paper_tick(
        state_dir=h3_dir,
        cfg=cfg_h3,
        now_ms=t0 + 8 * 3_600_000,
        markets=[mkt("io", 100.0, 40.0), mkt("xyz", 100.0, 5.0)],
        book_fetcher=lambda _s: ([(99.99, 500.0)], [(100.01, 500.0)]),
    )
    assert r_h3_end["state"]["positions"]["NVDA"]["cum_basis_mtm_usd"] == pytest.approx(0.0, abs=0.01)


def test_ws_mm_live_partial_fill_adverse_halt_and_live_flatten_ledger() -> None:
    import asyncio

    from hl_quant import hl_mm_avellaneda_stoikov as mm

    class LiveMockAdapter:
        def __init__(self) -> None:
            self.next_oid = 100
            self.cancelled: list[dict] = []
            self.ioc_orders: list[mm.PlannedOrder] = []

        def set_leverage(self, coin: str, leverage: int, *, is_cross: bool = True) -> dict:
            return {"status": "ok"}

        def place_alo_order(self, order: mm.PlannedOrder) -> dict:
            self.next_oid += 1
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"resting": {"oid": self.next_oid}}]}}}

        def cancel_all_open_orders(self, open_orders: list[dict], **_kw) -> int:
            self.cancelled.extend(open_orders)
            return len(open_orders)

        def place_ioc_order(self, order: mm.PlannedOrder) -> dict:
            self.ioc_orders.append(order)
            return {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {"statuses": [{"filled": {"totalSz": str(order.sz), "avgPx": str(order.limit_px), "oid": 999}}]},
                },
            }

    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        if t == "clearinghouseState":
            return httpx.Response(200, json={"assetPositions": []})
        return httpx.Response(200, json=[])

    t0 = 1_700_000_000_000
    # 1. l2Book places resting buy oid=101, sell oid=102
    # 2. orderUpdates partial fill reduces buy oid=101 remaining sz to 0.01 + 3 toxic buy fills at 2500
    # 3. 6 seconds later, mid crashes to 2490 (-40 bps <= -20 bps adverse_markout_halt_bps) -> halts & cancels!
    # 4. flatten_on_exit=True submits live IOC sell and updates ledger to 0 inventory!
    ws_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": t0, "levels": [[{"px": "2500.0", "sz": "10.0"}], [{"px": "2500.4", "sz": "10.0"}]]}},
        {"channel": "orderUpdates", "data": [{"status": "open", "order": {"coin": "ETH", "oid": 101, "sz": "0.01"}}]},
        {
            "channel": "userFills",
            "data": {
                "isSnapshot": False,
                "fills": [
                    {"tid": 1, "coin": "ETH", "oid": 101, "side": "B", "px": "2500.0", "sz": "0.01", "fee": "0.0", "time": t0 + 100},
                    {"tid": 2, "coin": "ETH", "oid": 101, "side": "B", "px": "2500.0", "sz": "0.01", "fee": "0.0", "time": t0 + 200},
                    {"tid": 3, "coin": "ETH", "oid": 101, "side": "B", "px": "2500.0", "sz": "0.01", "fee": "0.0", "time": t0 + 300},
                ],
            },
        },
        *[
            {"channel": "bbo", "data": {"coin": "ETH", "time": t0 + 1_000 + k * 500, "bbo": [{"px": "2489.8", "sz": "10.0"}, {"px": "2490.2", "sz": "10.0"}]}}
            for k in range(12)
        ],
    ]
    adapter = LiveMockAdapter()
    mp = pytest.MonkeyPatch()
    mp.setattr(mm.time, "time", lambda: t0 / 1000.0)
    try:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            summary, _, _ = asyncio.run(
                mm.run_live_mm_ws_session(
                    "ETH",
                    adapter,
                    account_address="0xabc",
                    execute_live=True,
                    flatten_on_exit=True,
                    params=mm.ASParams(base_order_usd=50.0, max_inventory_usd=500.0, adverse_markout_halt_bps=-20.0),
                    client=client,
                    ws_messages=ws_msgs,
                )
            )
    finally:
        mp.undo()

    assert any(h.startswith("adverse_markout_halt:") for h in summary.halt_events)
    assert summary.flattened_exit_sz == pytest.approx(0.03)
    assert summary.end_inventory_sz == pytest.approx(0.0)


def test_fetch_all_user_funding_paginates_and_retries_429() -> None:
    from hl_quant import hl_post_trade_attribution as pta

    calls = {"n": 0}
    all_events = [
        {"time": 1_000 + i * 1_000, "hash": f"0x{i}", "delta": {"coin": "BTC", "usdc": "1.25"}}
        for i in range(7)
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        assert body["type"] == "userFunding"
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(429, json={"error": "rate_limited"})
        start_t = int(body.get("startTime", 0))
        slice_rows = [e for e in all_events if e["time"] >= start_t][:3]
        return httpx.Response(200, json=slice_rows)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = pta.fetch_all_user_funding(client, "0x123", page_cap=3)
    assert len(rows) == 7
    assert [r["time"] for r in rows] == [1_000 + i * 1_000 for i in range(7)]


def test_ml_walkforward_target_aligns_1h_price_with_1h_funding() -> None:
    import pandas as pd

    from hl_quant import alpha_families as af

    # Build a synthetic 260-hour WideMarket where feature X_t positively predicts return over (h_t, h_{t+1}],
    # while return over (h_{t+1}, h_{t+2}] is pure opposite noise (-X_t).
    # With aligned fwd_px (horizon_h=1), Ridge learns a positive weight and goes LONG high-X coins!
    rng = np.random.default_rng(7)
    n_h = 560
    coins = [f"C{i}" for i in range(6)]
    hours = np.arange(n_h, dtype="int64") * 3_600_000
    sig = rng.normal(0.0, 0.01, size=(n_h, len(coins)))
    # Cumulative log price so log(hl_close[t] / hl_close[t-1]) == +sig[t] over (h_t, h_{t+1}]
    px_arr = 100.0 * np.exp(np.cumsum(sig, axis=0))
    rows = []
    for ti, h_ms in enumerate(hours):
        for ci, c in enumerate(coins):
            rows.append(
                {
                    "hour_ms": int(h_ms),
                    "coin": c,
                    "hl_close": float(px_arr[ti, ci]),
                    "bybit_close": float(px_arr[ti, ci]),
                    "hl_fund_next": 0.0,
                    "hl_rate_h": 0.0,
                    "bybit_rate_h": 0.0,
                    "hl_notional_usd": 5_000_000.0,
                    "bybit_notional_usd": 5_000_000.0,
                }
            )
    mkt = af.build_wide_market(pd.DataFrame(rows))
    feat_cache = {"pred": pd.DataFrame(sig, index=hours, columns=coins)}
    w = af.build_ml_walkforward_weights(
        mkt,
        {"horizon_h": 1, "model": "ridge", "top_n": 2, "rebalance_h": 1, "min_vol_usd": 1_000.0, "alpha": 1.0},
        fold_boundaries=[(370, 520, 555)],
        feature_cache=feat_cache,
    )
    sim = af.simulate_hl_single_venue(mkt, w.loc[hours[520:555]], one_way_cost_bps=0.0)
    assert float(sim["ret"].sum()) > 0.0
