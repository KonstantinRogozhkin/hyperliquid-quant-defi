"""Regression tests for 2026-11-28 medium audit fixes (batch A: execution, MM, scheduler, paper)."""

from __future__ import annotations

import pytest

from hl_quant import hl_testnet_executor as ex

NOW = 1_795_800_000_000


def _meta(*coins: str) -> dict[str, ex.AssetSpec]:
    return {c: ex.AssetSpec(c, sz_decimals=2, max_leverage=10) for c in coins}


def _acct(**kw) -> ex.AccountSnapshot:
    base = {"equity_usd": 1_000.0, "free_margin_usd": 900.0, "positions_sz": {}, "open_orders": [], "observed_at_ms": NOW}
    base.update(kw)
    return ex.AccountSnapshot(**base)


class _FailingCancelAdapter(ex.DryRunAdapter):
    def cancel_all_open_orders(self, open_orders, *, fast: bool = False):
        reqs = [{"coin": o["coin"], "oid": o["oid"]} for o in open_orders]
        statuses = [{"error": "rate limited"} if r["coin"] == "SOL" else "success" for r in reqs]
        return [{"mode": "testnet_live", "requests": reqs, "response": {"status": "ok", "response": {"type": "cancel", "data": {"statuses": statuses}}}}]


def test_failed_cancel_blocks_new_exposure_on_that_coin_only():
    acct = _acct(open_orders=[{"coin": "SOL", "oid": 1}, {"coin": "ETH", "oid": 2}])
    bbo = {"SOL": (100.0, 100.1, NOW), "ETH": (2_000.0, 2_001.0, NOW)}
    adapter = _FailingCancelAdapter()
    res = ex.execute_rebalance_cycle(acct, {"SOL": 0.10, "ETH": -0.10}, _meta("SOL", "ETH"), bbo, adapter, high_water_mark_usd=1_000.0, now_ms=NOW)
    assert res["cancel_failed_coins"] == ["SOL"]
    assert {o["coin"] for o in adapter.placed} == {"ETH"}


def test_cancel_failed_coins_parsing():
    gone = [{"mode": "testnet_live", "coin": "BTC", "oid": 5, "response": {"status": "ok", "response": {"data": {"statuses": [{"error": "Order was never placed, already canceled, or filled."}]}}}}]
    assert ex.cancel_failed_coins(gone, [{"coin": "BTC", "oid": 5}]) == set()
    err = [{"mode": "testnet_live", "coin": "BTC", "oid": 5, "response": {"status": "err", "response": "boom"}}]
    assert ex.cancel_failed_coins(err, [{"coin": "BTC", "oid": 5}]) == {"BTC"}
    assert ex.cancel_failed_coins([], [{"coin": "BTC", "oid": 5}]) == {"BTC"}


class _RaisingCancelAdapter(ex.DryRunAdapter):
    def cancel_all_open_orders(self, open_orders, *, fast: bool = False):
        raise TimeoutError("cancel timed out")


def test_cancel_transport_error_does_not_crash_and_blocks_entries():
    acct = _acct(open_orders=[{"coin": "SOL", "oid": 1}])
    adapter = _RaisingCancelAdapter()
    res = ex.execute_rebalance_cycle(acct, {"SOL": 0.10}, _meta("SOL"), {"SOL": (100.0, 100.1, NOW)}, adapter, high_water_mark_usd=1_000.0, now_ms=NOW)
    assert res["cancel_failed_coins"] == ["SOL"]
    assert adapter.placed == []


def test_oracle_band_blocks_entries_into_dislocated_books_but_allows_reduce_only():
    # INIT-like dislocation: book mid ~30% above the oracle.
    acct = _acct(positions_sz={"XMR": 0.5})
    bbo = {"INIT": (0.1160, 0.1162, NOW), "XMR": (330.0, 331.0, NOW)}
    refs = {"INIT": 0.0894, "XMR": 401.0}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, {"INIT": -0.03, "XMR": 0.0}, _meta("INIT", "XMR"), bbo, adapter,
        high_water_mark_usd=1_000.0, now_ms=NOW, reference_px=refs,
    )
    assert res["oracle_band_blocked_coins"] == ["INIT", "XMR"]
    assert [(o["coin"], o["reduce_only"]) for o in adapter.placed] == [("XMR", True)]


class _RejectingAdapter(ex.DryRunAdapter):
    def place_alo_order(self, order):
        rec = super().place_alo_order(order)
        if order.coin == "ETH":
            rec["response"] = {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only order would have immediately matched"}]}}}
        return rec


def test_total_notional_counts_only_accepted_orders():
    bbo = {"SOL": (100.0, 100.1, NOW), "ETH": (2_000.0, 2_001.0, NOW)}
    adapter = _RejectingAdapter()
    res = ex.execute_rebalance_cycle(_acct(), {"SOL": 0.10, "ETH": -0.10}, _meta("SOL", "ETH"), bbo, adapter, high_water_mark_usd=1_000.0, now_ms=NOW)
    sol_ntl = sum(r["notional_usd"] for r in adapter.placed if r["coin"] == "SOL")
    assert res["total_notional_usd"] == pytest.approx(sol_ntl)
    assert res["rejected_count"] >= 1


def test_mm_open_orders_helper_rejects_error_payloads():
    import httpx

    from hl_quant import hl_mm_avellaneda_stoikov as mm

    def client(payload, status=200):
        return httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload)))

    with client([{"coin": "ETH", "oid": 1}, "junk"]) as c:
        assert mm.fetch_open_orders_checked(c, "0xabc") == [{"coin": "ETH", "oid": 1}]
    with client({"status": "err", "response": "rate limited"}) as c:
        assert mm.fetch_open_orders_checked(c, "0xabc") is None
    with client(None) as c:
        assert mm.fetch_open_orders_checked(c, "0xabc") is None
    with client([], status=429) as c:
        assert mm.fetch_open_orders_checked(c, "0xabc") is None


# ----------------------------------------------------------------- scheduler


def _job(name: str, minute: int, hours: tuple[int, ...] | None = None):

    from hl_quant.scheduler import JobSpec

    return JobSpec(
        name=name, description=name, cron_expr=f"{minute} * * * *", minutes=(minute,), hours=hours,
        timeout_s=60, readonly=True, bootstrap_eligible=False,
        build_command=lambda root, data, tg: ["python", f"{name}.py"],
        is_enabled=lambda: (True, "active"), has_required_credentials=lambda: (True, "ok"),
    )


def _seed_status(tmp_path, jobs: dict[str, str]) -> None:
    import json

    p = tmp_path / "paper" / "hl_worker_status.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"jobs": [
        {"name": n, "state": "ok", "last_exit_code": 0, "last_started_at_utc": ts} for n, ts in jobs.items()
    ]}))


def test_scheduler_restart_catches_up_missed_slot_but_not_started_ones(tmp_path):
    from datetime import datetime, timezone

    from hl_quant.scheduler import WorkerScheduler

    jobs = [_job("autopilot", 37), _job("executor", 10), _job("delayed", 20)]
    # autopilot last ran 4h ago (missed 20:37 during the deploy); executor already started at
    # 20:10; "delayed" started at 20:21:30 for its 20:20 slot (dependency wait).
    _seed_status(tmp_path, {
        "autopilot": "2026-11-27 16:37:05 UTC",
        "executor": "2026-11-27 20:10:02 UTC",
        "delayed": "2026-11-27 20:21:30 UTC",
    })
    ran: list[str] = []
    sched = WorkerScheduler(
        data_dir=tmp_path, jobs=jobs, telegram_enabled=False, startup_catchup_minutes=30,
        runner=lambda cmd, env, t: ran.append(cmd[1]) or 0,
    )
    sched.tick_minute(datetime(2026, 11, 27, 20, 40, tzinfo=timezone.utc))
    assert ran == ["autopilot.py"]


def test_scheduler_marks_shutdown_kill_as_interrupted_not_failed(tmp_path):
    from hl_quant.scheduler import WorkerScheduler

    job = _job("paper", 5)
    sched = WorkerScheduler(data_dir=tmp_path, jobs=[job], telegram_enabled=False, runner=lambda c, e, t: -15)
    sched._stop_event.set()
    sched.run_job(job, force=True)
    entry = sched._state["paper"]
    assert entry["state"] == "interrupted"
    assert entry["runs_failed"] == 0


def test_scheduler_process_lock_blocks_concurrent_run_once(tmp_path):
    import fcntl
    import os

    from hl_quant.scheduler import WorkerScheduler

    job = _job("testnet_executor", 10)
    ran: list[str] = []
    sched = WorkerScheduler(data_dir=tmp_path, jobs=[job], telegram_enabled=False, runner=lambda c, e, t: ran.append("x") or 0)
    lock_dir = tmp_path / "paper" / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_dir / "testnet_executor.lock", os.O_RDWR | os.O_CREAT, 0o644)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another process (e.g. docker exec --run-once)
    try:
        assert sched.run_job(job, force=True) == 0
        assert ran == []
    finally:
        os.close(fd)
    sched.run_job(job, force=True)
    assert ran == ["x"]


# ----------------------------------------------------------------- paper / autopilot


def test_streak_restarts_after_a_gap_of_missed_ticks():
    from hl_quant import persistence_clock as pc

    h = pc.MS_PER_HOUR
    s1 = pc.advance_streak(None, qualifies=True, now_ms=10 * h)
    s2 = pc.advance_streak(s1, qualifies=True, now_ms=14 * h)
    assert s2["n"] == 2
    # Worker down for 2 days: the next qualifying tick is not "consecutive".
    s3 = pc.advance_streak(s2, qualifies=True, now_ms=62 * h)
    assert s3 == {"n": 1, "first_ms": 62 * h, "last_inc_ms": 62 * h}
    # Legacy migrated values (last_inc_ms=0) are not treated as gaps.
    assert pc.advance_streak(3, qualifies=True, now_ms=62 * h)["n"] == 4


def test_position_accrual_clock_covers_skipped_ticks():
    from hl_quant import persistence_clock as pc

    h = pc.MS_PER_HOUR
    assert pc.settled_hours_between(16 * h + 5, 24 * h + 5, cap_hours=24) == 8.0
    assert pc.settled_hours_between(0, 24 * h, cap_hours=24) == 0.0
    assert pc.position_accrual_start_ms({"last_accrual_ms": 16 * h}, 20 * h) == 16 * h
    assert pc.position_accrual_start_ms({}, 20 * h) == 20 * h


def test_autopilot_retry_within_spacing_is_a_noop(tmp_path):
    import json

    from hl_quant import hl_autopilot as ap

    sp = tmp_path / "hl_spot_perp_paper"
    sp.mkdir()
    (sp / "state.json").write_text(json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}}))
    closes = {c: [100.0] * 50 for c in ap.CTA_UNIVERSE}
    t0 = 1_795_700_000_000
    ap.run_autopilot_tick(data_root=tmp_path, now_ms=t0, daily_closes=closes)
    before = (tmp_path / "hl_autopilot" / "state.json").read_text()
    res = ap.run_autopilot_tick(data_root=tmp_path, now_ms=t0 + 15_000, daily_closes=closes)
    assert res["status"] == "duplicate_tick_skipped"
    assert (tmp_path / "hl_autopilot" / "state.json").read_text() == before


def test_metamodel_stale_held_coin_is_written_off_after_max_blocked_ticks(tmp_path):
    import json
    import sys
    from pathlib import Path

    from hl_quant import alpha_families as af
    from hl_quant import paper_tracker as pt

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from test_hl_paper_tracker import _flat_quotes, _synthetic_market
    finally:
        sys.path.pop(0)

    m1 = _synthetic_market(n_hours=400, n_coins=6)
    assert pt.run_paper_tick(m1, _flat_quotes(m1), state_dir=tmp_path)["status"] == "ok"
    held = sorted(json.loads((tmp_path / "state.json").read_text())["positions"])
    dead = held[0]

    def _without(n_hours: int) -> af.WideMarket:
        m = _synthetic_market(n_hours=n_hours, n_coins=6)
        keep = [c for c in m.coins if c != dead]
        return af.WideMarket(
            hours=m.hours, coins=keep,
            **{f: getattr(m, f)[keep] for f in af.WideMarket.__dataclass_fields__ if f not in ("hours", "coins")},
        )

    for n in (401, 402):
        m = _without(n)
        assert pt.run_paper_tick(m, _flat_quotes(m), state_dir=tmp_path, max_held_missing_ticks=3)["status"] == "held_coins_missing"
    m3 = _without(403)
    res = pt.run_paper_tick(m3, _flat_quotes(m3), state_dir=tmp_path, max_held_missing_ticks=3)
    assert res["status"] == "ok"
    assert res["forced_stale_exits"] == [dead]
    assert not (tmp_path / "held_missing.json").exists()


def _mm_market_helpers():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from test_paper_and_alpha_high_fixes import H, T0, _market, _quotes
    finally:
        sys.path.pop(0)
    return H, T0, _market, _quotes


def _patch_btc_eth_targets(monkeypatch):
    from hl_quant import paper_tracker as pt

    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda m, s: {
            "BTC": {"hl_weight": -0.20, "cex_legs": {"bybit": 0.20}, "cex_weight": 0.20, "cex_venue": "bybit"},
            "ETH": {"hl_weight": 0.10, "cex_legs": {"bybit": -0.10}, "cex_weight": -0.10, "cex_venue": "bybit"},
        },
    )


def test_metamodel_missing_feed_grace_period_is_per_coin(tmp_path, monkeypatch):
    from hl_quant import paper_tracker as pt

    _, _, _market, _quotes = _mm_market_helpers()
    _patch_btc_eth_targets(monkeypatch)
    kw = {"state_dir": tmp_path, "sleeves": [], "max_held_missing_ticks": 3}
    both = {"bybit": {"BTC": 100.0, "ETH": 100.0}}
    assert pt.run_paper_tick(_market(20), _quotes(), cex_mid_prices=both, **kw)["status"] == "ok"
    # BTC's CEX feed dies for two tick hours...
    for n in (21, 22):
        assert pt.run_paper_tick(_market(n), _quotes(), cex_mid_prices={"bybit": {"ETH": 100.0}}, **kw)["status"] == "held_coins_missing"
    # ...then recovers while ETH's dies: ETH has missed ONE observation and must not be written off.
    r = pt.run_paper_tick(_market(23), _quotes(), cex_mid_prices={"bybit": {"BTC": 100.0}}, **kw)
    assert r["status"] == "held_coins_missing"
    assert r["blocked_ticks"] == {"ETH": 1}


def test_metamodel_written_off_coin_is_not_reopened_until_feed_recovers(tmp_path, monkeypatch):
    import json

    from hl_quant import paper_tracker as pt

    _, _, _market, _quotes = _mm_market_helpers()
    _patch_btc_eth_targets(monkeypatch)
    kw = {"state_dir": tmp_path, "sleeves": [], "max_held_missing_ticks": 3}
    eth_only = {"bybit": {"ETH": 100.0}}
    pt.run_paper_tick(_market(20), _quotes(), cex_mid_prices={"bybit": {"BTC": 100.0, "ETH": 100.0}}, **kw)
    for n in (21, 22):
        pt.run_paper_tick(_market(n), _quotes(), cex_mid_prices=eth_only, **kw)
    r3 = pt.run_paper_tick(_market(23), _quotes(), cex_mid_prices=eth_only, **kw)
    assert r3["status"] == "ok" and r3["forced_stale_exits"] == ["BTC"]
    st = json.loads((tmp_path / "state.json").read_text())
    assert "BTC" not in st["positions"] and st["stale_excluded_coins"] == ["BTC"]
    # Feed still dead: BTC stays out (no re-entry from historical fallbacks, no new block).
    r4 = pt.run_paper_tick(_market(24), _quotes(), cex_mid_prices=eth_only, **kw)
    assert r4["status"] == "ok"
    assert "BTC" not in json.loads((tmp_path / "state.json").read_text())["positions"]
    # Feed recovers: BTC is eligible again.
    pt.run_paper_tick(_market(25), _quotes(), cex_mid_prices={"bybit": {"BTC": 100.0, "ETH": 100.0}}, **kw)
    st5 = json.loads((tmp_path / "state.json").read_text())
    assert "BTC" in st5["positions"] and st5["stale_excluded_coins"] == []


def test_spot_perp_position_accrual_uses_extended_cap_while_nav_guard_pending(tmp_path):
    import json

    from hl_quant import hl_spot_perp_arb as spa

    H = 3_600_000
    t0 = 1_795_700_000_000
    markets = [{
        "base_symbol": "HYPE", "spot_pair": "@107", "spot_token": "HYPE", "quote_token": "USDC",
        "is_proxy_hedge": False, "spot_mid_px": 20.0, "spot_day_vol_usd": 10_000_000.0,
        "perp_mark_px": 20.0, "perp_oracle_px": 20.0, "funding_rate_h": 0.0001,
        "perp_day_vol_usd": 50_000_000.0, "perp_oi_usd": 20_000_000.0, "only_isolated": False, "max_leverage": 5,
    }]

    def book(coin_key: str):
        return [(20.0 * 0.9999, 10_000.0)], [(20.0 * 1.0001, 10_000.0)]

    state_dir = tmp_path / "sp"
    state_dir.mkdir()
    pos = {
        "base_symbol": "HYPE", "spot_pair": "@107", "qty": 1000.0, "entry_spot_px": 20.0, "entry_perp_px": 20.0,
        "last_spot_px": 20.0, "last_perp_px": 20.0, "spot_notional_usd": 20_000.0, "perp_notional_usd": 20_000.0,
        "perp_margin_usd": 10_000.0, "entry_cost_usd": 6.0, "cum_cost_usd": 6.0, "cum_funding_usd": 0.0,
        "cum_basis_mtm_usd": 0.0, "hours_held": 4.0,
    }
    (state_dir / "state.json").write_text(json.dumps({
        "starting_capital_usd": 100_000.0, "equity_usd": 100_000.0, "hlp_reserve_usd": 10_000.0,
        "free_cash_usd": 60_000.0, "hlp_apr_pct": 12.0, "last_tick_ms": t0, "ticks_count": 1,
        "nav_guard_pending_ms": t0 + H, "positions": {"HYPE": pos},
    }))
    res = spa.run_spot_perp_paper_tick(
        state_dir=state_dir, cfg=spa.SpotPerpPaperConfig(max_tick_nav_jump_pct=50.0), now_ms=t0 + 48 * H,
        markets=markets, book_fetcher=book, hlp_summary=(12.0, 250_000_000.0),
    )
    hype = res["state"]["positions"]["HYPE"]
    # 48h of fallback funding on the $20k perp (not a 24h truncation).
    assert hype["cum_funding_usd"] == pytest.approx(20_000.0 * 0.0001 * 48.0, rel=1e-6)
