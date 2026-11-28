"""Pass 4 Wave 1 regression suite: Testnet HWM/Risk, Executor, A-S MM, Micro-Exec, Attribution, Stress & CEX."""

from __future__ import annotations

import asyncio
from decimal import Decimal
import importlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import httpx
import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner

from hl_quant import alpha_families as af
from hl_quant import cex_perp_public as cex
from hl_quant import cross_usdc_micro_execution as micro
from hl_quant import execution_stress as es
from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_testnet_executor as ex
from hl_quant import hl_testnet_risk as risk

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_796_800_000_000


def _meta(*coins: str, sz_decimals: int = 2) -> dict[str, ex.AssetSpec]:
    return {c: ex.AssetSpec(c, sz_decimals=sz_decimals, max_leverage=10) for c in coins}


# --------------------------------------------------------------------------- 1. LIVE-01, EXEC-01, EXEC-05, OPS-04


def test_exec01_multiplicative_withdrawal_hwm_preserves_drawdown_pct():
    # Pre-withdrawal: HWM = 1,000, equity = 960 -> exact 4.0% drawdown (< 5% kill-switch).
    # Owner withdraws $600 -> post-withdrawal equity = 360.
    # Old additive math gave 1,000 - 600 = 400 -> (400 - 360)/400 = 10.0% FALSE drawdown breach!
    adj = ex.flow_adjusted_high_water_mark(
        1_000.0,
        prev_net_flow_usd=1_000.0,
        cur_net_flow_usd=400.0,
        current_equity_usd=360.0,
    )
    assert adj == pytest.approx(1_000.0 * (360.0 / 960.0))
    assert ex.drawdown_pct(360.0, adj) == pytest.approx(4.0)

    # Deposits remain additive dollar-for-dollar so fresh capital never masks drawdown as profit.
    adj_dep = ex.flow_adjusted_high_water_mark(
        1_000.0,
        prev_net_flow_usd=1_000.0,
        cur_net_flow_usd=1_500.0,
        current_equity_usd=1_460.0,
    )
    assert adj_dep == pytest.approx(1_500.0)


def test_live01_and_ops04_post_flatten_cooldown_reanchors_once_with_pid_safe_save(tmp_path: Path):
    hwm_path = tmp_path / "hl_testnet_hwm.json"
    audit_path = tmp_path / "hl_testnet_audit.jsonl"
    ex.save_high_water_mark(hwm_path, 1_000.0, net_external_flow_usd=1_000.0)
    assert not list(tmp_path.glob("*.tmp*"))

    t_start = NOW - int(4.5 * 3_600_000)
    records = [
        {"ts_ms": t_start - 3_600_000, "mode": "testnet_live", "status": "ok", "violations": []},
        {
            "ts_ms": t_start,
            "mode": "testnet_live",
            "status": "kill_switch_triggered",
            "derisk_mode": "flatten",
            "violations": ["drawdown_limit:45.00%>=5.00%"],
        },
        {
            "ts_ms": NOW - 1_800_000,
            "mode": "testnet_live",
            "status": "kill_switch_triggered",
            "derisk_mode": "flatten",
            "violations": ["drawdown_limit:45.00%>=5.00%"],
        },
    ]
    audit_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")

    # 1. Before cooldown (< 4h) -> no re-anchor
    h_early, did_early = ex.reanchor_hwm_after_flatten(
        hwm_path,
        audit_path,
        stored_hwm=1_000.0,
        oracle_equity_usd=550.0,
        gross_notional_usd=0.0,
        now_ms=t_start + int(3.5 * 3_600_000),
        net_external_flow_usd=1_000.0,
    )
    assert did_early is False and h_early == pytest.approx(1_000.0)

    # 2. Still holding non-dust positions -> no re-anchor
    h_open, did_open = ex.reanchor_hwm_after_flatten(
        hwm_path,
        audit_path,
        stored_hwm=1_000.0,
        oracle_equity_usd=550.0,
        gross_notional_usd=25.0,
        now_ms=NOW,
        net_external_flow_usd=1_000.0,
    )
    assert did_open is False and h_open == pytest.approx(1_000.0)

    # 3. Cooldown elapsed & flat -> re-anchors to 550.0 and records streak metadata
    h_new, did_reanchor = ex.reanchor_hwm_after_flatten(
        hwm_path,
        audit_path,
        stored_hwm=1_000.0,
        oracle_equity_usd=550.0,
        gross_notional_usd=0.0,
        now_ms=NOW,
        net_external_flow_usd=1_000.0,
        persist=True,
    )
    assert did_reanchor is True and h_new == pytest.approx(550.0)
    saved = json.loads(hwm_path.read_text(encoding="utf-8"))
    assert saved["high_water_mark_usd"] == pytest.approx(550.0)
    assert saved["pre_reanchor_hwm_usd"] == pytest.approx(1_000.0)
    assert saved["reanchored_streak_ms"] == t_start
    assert saved["net_external_flow_usd"] == pytest.approx(1_000.0)

    # Subsequent save_high_water_mark preserves reanchored_streak_ms so same streak cannot re-anchor twice
    ex.save_high_water_mark(hwm_path, 555.0, net_external_flow_usd=1_000.0)
    _, did_again = ex.reanchor_hwm_after_flatten(
        hwm_path,
        audit_path,
        stored_hwm=555.0,
        oracle_equity_usd=520.0,
        gross_notional_usd=0.0,
        now_ms=NOW + 60_000,
        net_external_flow_usd=1_000.0,
    )
    assert did_again is False


def test_exec05_risk_snapshot_oracle_hwm_and_probe_sync(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    sys.path.insert(0, str(ROOT / "scripts" / "analytics"))
    probe_cli = importlib.import_module("run_hl_testnet_risk_probe")

    # Mark spike pushes equity_usd to $1,300 while oracle_eq is $980 (< $1,000 HWM => 2.0% drawdown!)
    acct = ex.AccountSnapshot(
        equity_usd=1_300.0,
        free_margin_usd=1_100.0,
        positions_sz={"XMR": 1.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"XMR": 720.0},
        position_entry_px={"XMR": 400.0},
        position_unrealized_pnl_usd={"XMR": 320.0},
    )
    ctxs = {"XMR": risk.AssetCtx(mark_px=720.0, oracle_px=400.0, funding_apr_pct=0.0)}
    snap = risk.build_risk_snapshot(acct, high_water_mark_usd=1_000.0, asset_ctxs=ctxs, book_sides={"XMR": "two_sided"})
    assert snap["equity_usd"] == pytest.approx(1_300.0)
    assert snap["oracle_adjusted_equity_usd"] == pytest.approx(980.0)
    assert snap["high_water_mark_usd"] == pytest.approx(1_000.0)
    assert snap["drawdown_pct"] == pytest.approx(2.0)

    # Probe CLI: cold-start saves net_external_flow_usd, auto-resolves sibling hl_metamodel/state.json,
    # strips own resting order from l2Book, and treats HL_MM_COINS as MM-managed.
    paper_dir = tmp_path / "paper"
    (paper_dir / "hl_metamodel").mkdir(parents=True)
    (paper_dir / "hl_metamodel" / "state.json").write_text(
        json.dumps({"positions": {"BTC": {"hl_weight": 0.20}, "ZRO": {"hl_weight": 0.20}}}),
        encoding="utf-8",
    )
    hwm_file = paper_dir / "hl_testnet_hwm.json"
    out_file = paper_dir / "hl_testnet_risk" / "latest.json"

    def handler(req: httpx.Request) -> httpx.Response:
        b = json.loads(req.content)
        t = b["type"]
        if t == "clearinghouseState":
            return httpx.Response(
                200,
                json={
                    "marginSummary": {"accountValue": "500.0", "totalMarginUsed": "50.0"},
                    "withdrawable": "450.0",
                    "assetPositions": [
                        {"position": {"coin": "ETH", "szi": "0.1", "entryPx": "2000", "positionValue": "200", "unrealizedPnl": "0"}},
                        {"position": {"coin": "SKY", "szi": "500", "entryPx": "0.05", "positionValue": "25", "unrealizedPnl": "0"}},
                    ],
                },
            )
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if t == "frontendOpenOrders":
            # SKY has a non-reduceOnly sell resting at 0.05 -> our own order is the ONLY ask on SKY!
            return httpx.Response(
                200,
                json=[{"coin": "SKY", "oid": 91, "side": "A", "limitPx": "0.05", "sz": "500", "reduceOnly": False}],
            )
        if t == "meta":
            return httpx.Response(
                200,
                json={
                    "universe": [
                        {"name": "BTC", "szDecimals": 5, "maxLeverage": 40},
                        {"name": "ETH", "szDecimals": 4, "maxLeverage": 25},
                        {"name": "SKY", "szDecimals": 0, "maxLeverage": 5},
                        {"name": "ZRO", "szDecimals": 1, "maxLeverage": 5, "onlyIsolated": True},
                    ]
                },
            )
        if t == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "ETH"}, {"name": "SKY"}]},
                    [{"markPx": "2000", "oraclePx": "2000", "funding": "0"}, {"markPx": "0.05", "oraclePx": "0.05", "funding": "0"}],
                ],
            )
        if t == "l2Book":
            c = b["coin"]
            if c == "SKY":
                return httpx.Response(
                    200,
                    json={"coin": "SKY", "levels": [[{"px": "0.048", "sz": "1000", "n": 2}], [{"px": "0.05", "sz": "500", "n": 1}]]},
                )
            return httpx.Response(200, json={"coin": c, "levels": [[], []]})
        if t == "userNonFundingLedgerUpdates":
            return httpx.Response(200, json=[{"time": 1, "hash": "0x1", "delta": {"type": "deposit", "usdc": "500.0"}}])
        return httpx.Response(200, json=[])

    real_client = httpx.Client
    monkeypatch.setattr(probe_cli.httpx, "Client", lambda **_kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0xPROBE")
    monkeypatch.setenv("HL_MM_COINS", "ETH")
    res = CliRunner().invoke(
        probe_cli.app,
        ["--hwm-state", str(hwm_file), "--metamodel-state", str(tmp_path / "nonexistent.json"), "--output-json", str(out_file)],
    )
    assert res.exit_code == 0, res.output
    assert ex.load_hwm_net_flow(hwm_file) == pytest.approx(500.0)
    out = json.loads(out_file.read_text(encoding="utf-8"))
    # SKY's sole ask was our own resting order (reduceOnly=False) -> stripped to bid_only & flagged unmanaged!
    assert out["orphan_positions"]["SKY"]["book"] == "bid_only"
    assert out["orphan_positions"]["SKY"]["unmanaged"] is True
    # ETH is in HL_MM_COINS -> NOT flagged unmanaged despite empty book!
    assert out["orphan_positions"]["ETH"]["unmanaged"] is False


# --------------------------------------------------------------------------- 2. LIVE-02, EXEC-06, EXEC-09, EXEC-10


def test_live02_sub50c_dust_full_close_and_exact_final_slice_remainder():
    # $0.26 MON dust position closes cleanly without $0.50 floor blocking it
    acct_dust = ex.AccountSnapshot(
        equity_usd=500.0,
        free_margin_usd=490.0,
        positions_sz={"MON": -2.6},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"MON": -0.26},
    )
    meta = {"MON": ex.AssetSpec("MON", sz_decimals=1, max_leverage=5)}
    ords = ex.plan_rebalance_orders(acct_dust, {"MON": 0.0}, meta, {"MON": (0.099, 0.100, NOW)}, ex.RiskLimits())
    assert len(ords) == 1 and ords[0].reduce_only is True and ords[0].sz == pytest.approx(2.6)

    orphan_ords = ex.plan_orphan_unwind_orders(acct_dust, meta, {}, {"MON": (None, None, NOW)}, {"MON": 0.10})
    assert len(orphan_ords) == 1 and orphan_ords[0].sz == pytest.approx(2.6)

    # Multi-slice full close of 25.00 units across 3 slices leaves 0.00 remainder (8.33 + 8.33 + 8.34 == 25.00)
    acct_slice = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=9_000.0,
        positions_sz={"SOL": 25.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 2_500.0},
    )
    sliced = ex.plan_rebalance_orders(
        acct_slice,
        {"SOL": 0.0},
        _meta("SOL", sz_decimals=2),
        {"SOL": (99.9, 100.0, NOW)},
        ex.RiskLimits(max_child_usd=1_000.0),
    )
    assert len(sliced) == 3
    assert sum(o.sz for o in sliced) == pytest.approx(25.0)
    assert all(o.notional_usd <= 1_000.0 for o in sliced)


def test_exec06_standard_account_uses_perps_equity_for_sizing_and_kill_switch():
    # Standard Account: $200 Perps USDC + $800 idle Spot USDC (total $1,000, is_unified=False)
    acct_std = ex.AccountSnapshot(
        equity_usd=1_000.0,
        perps_equity_usd=200.0,
        free_margin_usd=30.0,  # 15% of $200 Perps (< 20%), even though 30/1000 = 3%
        positions_sz={"SOL": 2.5},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 250.0},  # 1.25x of $200 Perps (vs 0.25x of $1,000)
        is_unified=False,
    )
    bbo = {"SOL": (100.0, 100.1, NOW), "ETH": (1_999.0, 2_001.0, NOW)}
    # Target ETH=-0.40 + unquoted/retained or SOL=+1.20 -> gross checked vs $200 Perps equity
    ords = ex.plan_rebalance_orders(acct_std, {"ETH": -0.20, "SOL": 1.25}, _meta("SOL", "ETH", sz_decimals=3), bbo, ex.RiskLimits())
    eth_ord = next(o for o in ords if o.coin == "ETH")
    # 20% of $200 Perps = $40 (0.020 ETH at mid 2000), NOT 20% of $1,000 = $200 (0.100 ETH)!
    assert eth_ord.sz == pytest.approx(0.020)

    v = ex.check_kill_switch(
        acct_std,
        {"ETH": -0.40},
        {"ETH": (2_000.0, 2_001.0, NOW)},  # SOL unquoted -> 250/200 = 1.25x + 0.40x = 1.65x > 1.50x!
        ex.RiskLimits(max_gross_leverage=1.50, min_free_margin_pct=20.0),
        high_water_mark_usd=1_000.0,
        now_ms=NOW,
    )
    assert any(r.startswith("gross_leverage:1.65x") for r in v)
    assert any(r.startswith("low_free_margin:15.0%") for r in v)


def test_exec09_strip_own_orders_and_exec10_sign_flip_orphan_keep_and_asymmetry():
    # 1. Self-quote strip removes our own resting order when it's the sole quote on a side
    lv = [[{"px": "0.24", "sz": "500", "n": 2}], [{"px": "0.25", "sz": "30", "n": 1}]]
    cleaned = ex.strip_own_orders_from_l2_levels(
        lv, [{"coin": "JUP", "side": "A", "limitPx": "0.25", "sz": "30"}], "JUP"
    )
    assert len(cleaned[0]) == 1 and cleaned[1] == []

    # 2. Sign-flip split: short -100 GRAM -> target +0.10 (+100 GRAM) at :57 UTC with -60% funding APR!
    # The reduce_only=True close leg (-100 -> 0) MUST still execute despite the funding trap blocking the new long!
    base_h = NOW - (NOW % 3_600_000)
    t_57 = base_h + 57 * 60_000
    acct_flip = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=900.0,
        positions_sz={"GRAM": -100.0},
        open_orders=[],
        observed_at_ms=t_57,
        position_notional_usd={"GRAM": -100.0},
    )
    flip_ords = ex.plan_rebalance_orders(
        acct_flip,
        {"GRAM": 0.10},
        {"GRAM": ex.AssetSpec("GRAM", sz_decimals=1, max_leverage=5)},
        {"GRAM": (1.00, 1.001, t_57)},
        ex.RiskLimits(),
        funding_apr_by_coin={"GRAM": -60.0},
        now_ms=t_57,
    )
    assert len(flip_ords) == 1
    assert flip_ords[0].reduce_only is True and flip_ords[0].side == "buy" and flip_ords[0].sz == pytest.approx(100.0)

    # Outside funding trap: emits 2 legs (close 100 reduce_only=True + open ~100 reduce_only=False)
    t_10 = base_h + 10 * 60_000
    flip_both = ex.plan_rebalance_orders(
        acct_flip,
        {"GRAM": 0.10},
        {"GRAM": ex.AssetSpec("GRAM", sz_decimals=1, max_leverage=5)},
        {"GRAM": (0.999, 1.001, t_10)},
        ex.RiskLimits(),
        funding_apr_by_coin={"GRAM": -60.0},
        now_ms=t_10,
    )
    assert [(o.reduce_only, o.sz) for o in flip_both] == [(True, 100.0), (False, 100.0)]

    # 3. orphan_residual_notional_usd keeps keep_frac=1.0 on unquoted/empty books
    acct_orph = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=800.0,
        positions_sz={"VVV": 20.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"VVV": 500.0},
    )
    resid = ex.orphan_residual_notional_usd(acct_orph, _meta("VVV"), {}, {"VVV": (None, None, NOW)}, {"VVV": 25.0})
    assert resid["VVV"] == pytest.approx(500.0)

    # 4. Asymmetry guard: 2 longs (+$200 BTC, +$200 ETH) vs 2 shorts (-$200 SOL, -$200 INIT) where INIT is oracle-blocked
    acct_flat = ex.AccountSnapshot(equity_usd=1_000.0, free_margin_usd=1_000.0, positions_sz={}, open_orders=[], observed_at_ms=NOW)
    bbo4 = {
        "BTC": (60_000.0, 60_002.0, NOW),
        "ETH": (2_000.0, 2_001.0, NOW),
        "SOL": (100.0, 100.1, NOW),
        "INIT": (0.13, 0.131, NOW),  # +44% above $0.09 oracle -> oracle_blocked!
    }
    refs4 = {"BTC": 60_000.0, "ETH": 2_000.0, "SOL": 100.0, "INIT": 0.09}
    ad4 = ex.DryRunAdapter()
    res4 = ex.execute_rebalance_cycle(
        acct_flat,
        {"BTC": 0.20, "ETH": 0.20, "SOL": -0.20, "INIT": -0.20},
        _meta("BTC", "ETH", "SOL", "INIT", sz_decimals=4),
        bbo4,
        ad4,
        high_water_mark_usd=1_000.0,
        now_ms=NOW,
        reference_px=refs4,
    )
    assert res4["oracle_band_blocked_coins"] == ["INIT"]
    buy_ntl = sum(o["notional_usd"] for o in ad4.placed if o["side"] == "buy")
    sell_ntl = sum(o["notional_usd"] for o in ad4.placed if o["side"] == "sell")
    # Half of shorts dropped -> longs scaled down 50% (~$200 long vs ~$200 short)!
    assert buy_ntl == pytest.approx(sell_ntl, rel=0.05)


# --------------------------------------------------------------------------- 3. EXEC-02, EXEC-08, EXEC-11, EXEC-12, EXEC-13


def test_mm_snapshot_fills_urgent_cancel_guard_locked_book_ewma_and_clis(tmp_path: Path):
    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)

    # 1. Locked 0-tick book (best_bid == best_ask == 2500.0) steps BOTH bid down and ask up
    locked = mm.L2BookSnapshot("ETH", NOW, 2500.0, 2500.0, 5.0, 5.0, 0.0, 2500.0, 2500.0, 0.0)
    dec_locked = mm.compute_as_quotes(locked, spec, inventory_sz=0.0, sigma_tau_bps=5.0)
    assert dec_locked.bid_px is not None and dec_locked.ask_px is not None
    assert dec_locked.bid_px < 2500.0 and dec_locked.ask_px > 2500.0

    # 2. should_requote_side triggers when reduce_only flips even at identical price/size
    assert mm.should_requote_side(
        2500.0, 0.02, (2500.0, 0.02, False), mid_px=2500.0, threshold_bps=2.0, desired_reduce_only=True
    )

    # 3. EWMAVolatilityEstimator skips non-increasing ts_ms and sub-second unchanged-mid ticks
    est = mm.EWMAVolatilityEstimator(horizon_sec=30.0, half_life_sec=30.0, default_sigma_bps=10.0)
    est.update(NOW, 2500.0)
    s_after_move = est.update(NOW + 1_000, 2505.0)
    assert est.update(NOW + 900, 2490.0) == pytest.approx(s_after_move)  # out-of-order skipped
    for k in range(1, 40):
        assert est.update(NOW + 1_000 + k * 10, 2505.0) == pytest.approx(s_after_move)  # unchanged mid <1s -> zero decay

    # 4. Out-of-order mids_series keeps monotonic timestamps & Binance k* symbol mapping
    mids: list[tuple[int, float]] = []
    mm._append_monotonic_mid(mids, 2_000, 100.0)
    mm._append_monotonic_mid(mids, 1_500, 99.0)
    mm._append_monotonic_mid(mids, 2_000, 101.0)
    assert mids == [(2_000, 101.0)]
    assert mm.binance_futures_symbol("kPEPE") == "1000PEPEUSDT"
    assert mm.binance_futures_symbol("kFloki") == "1000FLOKIUSDT"

    # 5. EXEC-08: Urgent cancel exception keeps active_orders intact and never stacks replacement orders
    class _FailingUrgentAdapter(ex.DryRunAdapter):
        def cancel_all_open_orders(self, open_orders, *, fast: bool = False):
            raise TimeoutError("urgent cancel timed out")

    fail_ad = _FailingUrgentAdapter()
    active_u: dict[str, dict[str, Any]] = {"buy": {"oid": 11, "px": 2499.0, "sz": 0.02, "reduce_only": False}}
    oids_u: set[int] = {11}
    bk2 = mm.L2BookSnapshot("ETH", NOW, 2505.0, 2505.5, 5.0, 5.0, 0.0, 2505.25, 2505.25, 2.0)
    dec2 = mm.compute_as_quotes(bk2, spec, inventory_sz=0.0, sigma_tau_bps=8.0)
    pl, md, cn, _ = asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec2, active_u, oids_u, fail_ad, mid_px=bk2.mid_px, params=mm.ASParams(), recv_ms=NOW, audit_log_path=None, prefer_explicit_cancel=True
        )
    )
    assert cn == 0 and active_u["buy"]["oid"] == 11
    # Only the unquoted sell side was placed; buy side was NOT duplicated!
    assert [o["side"] for o in fail_ad.placed] == ["sell"]

    # 6. EXEC-02 & EXEC-13: WS isSnapshot=True fill belonging to all_mm_oids is ingested; dry-run WS flatten logs mode="dry_run"
    class _LiveSnapAdapter(ex.DryRunAdapter):
        def place_alo_order(self, order):
            rec = super().place_alo_order(order)
            rec.pop("mode", None)
            return rec

    def meta_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        if t == "clearinghouseState":
            return httpx.Response(200, json={"assetPositions": []})
        return httpx.Response(200, json=[])

    snap_ad = _LiveSnapAdapter()
    ws_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": NOW, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}},
        {
            "channel": "userFills",
            "data": {
                "isSnapshot": True,
                "fills": [
                    {"tid": 77, "coin": "ETH", "oid": 100_001, "side": "B", "px": "2500.0", "sz": "0.012", "fee": "0.0", "time": NOW + 50},
                    {"tid": 78, "coin": "ETH", "oid": 999_999, "side": "B", "px": "2500.0", "sz": "1.0", "fee": "0.0", "time": NOW + 50},
                ],
            },
        },
    ]
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as client:
        s_live, _, _ = asyncio.run(
            mm.run_live_mm_ws_session("ETH", snap_ad, account_address="0xMM", execute_live=True, client=client, ws_messages=ws_msgs)
        )
    assert s_live.fills_count == 1 and s_live.buy_fills == 1

    # Dry-run WS flatten audit carries mode="dry_run" so load_logged_order_oids ignores it
    dry_audit = tmp_path / "mm_dry_audit.jsonl"
    dry_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": NOW, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}},
        {"channel": "trades", "data": [{"coin": "ETH", "side": "A", "px": "2490.0", "sz": "1.0", "time": NOW + 100}]},
    ]
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as client:
        asyncio.run(
            mm.run_live_mm_ws_session(
                "ETH", ex.DryRunAdapter(), execute_live=False, flatten_on_exit=True, audit_log_path=dry_audit, client=client, ws_messages=dry_msgs
            )
        )
    assert ex.load_logged_order_oids(dry_audit) == set()

    # 7. MM CLI --replay-l2 sz_decimals & calibration report header
    sys.path.insert(0, str(ROOT / "scripts" / "analytics"))
    mm_cli = importlib.import_module("run_hl_mm_testnet")
    cal_src = (ROOT / "scripts" / "analytics" / "run_hl_mm_calibration.py").read_text(encoding="utf-8")
    assert mm_cli._replay_sz_decimals("SOL", pd.DataFrame({"mid_px": [140.0]})) == 2
    assert mm_cli._replay_sz_decimals("DOGE", pd.DataFrame({"mid_px": [0.15]})) == 0
    assert "1/κ+fee" in cal_src


# --------------------------------------------------------------------------- 4. EXEC-03 & EXEC-04


def test_exec03_and_exec04_spot_base_fee_grossup_expires_after_reset_and_exit_books_unwind(monkeypatch: pytest.MonkeyPatch):
    # 1. ETH $100 order (~0.0500 ETH, step 0.0001) with 10 bps base fee grosses up to 0.0501 ETH -> post-fee delta < 0.001!
    gross_q = micro.plan_spot_buy_quantity_for_base_fee(
        Decimal("0.0500"),
        step=Decimal("0.0001"),
        fee_rate=Decimal("0.001"),
        spot_price=Decimal("1919"),
        max_spot_notional=Decimal("104"),
        max_delta_fraction=Decimal("0.001"),
    )
    assert gross_q == Decimal("0.0501")
    net_after_fee = gross_q * Decimal("0.999")
    assert abs(net_after_fee - Decimal("0.0500")) / max(net_after_fee, Decimal("0.0500")) <= Decimal("0.001")

    # 2. HyperliquidClient.create_ioc resets expires_after = None inside finally: even when order raises
    hl_dummy = object.__new__(micro.HyperliquidClient)
    hl_dummy.use_testnet = True
    fake_ex = SimpleNamespace(expires_after=None, order=lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("boom")))
    hl_dummy._exchange = fake_ex
    with pytest.raises(RuntimeError, match="boom"):
        hl_dummy.create_ioc(asset="BTC", is_buy=True, quantity=Decimal("0.001"), price=Decimal("60000"), client_id="x", reduce_only=False)
    assert fake_ex.expires_after is None

    # 3. Exception during exit leg passes fresh exit_books (not stale entry books) to _safe_unwind
    cfg = micro.build_contract(ROOT / "config/cross_usdc_micro_execution.yml", run_id="pass4-micro-exit")
    monkeypatch.setattr(micro.time, "sleep", lambda _s: None)
    seen_unwind_books: list[dict[str, Any]] = []

    class _ExitFailRunner(micro.MicroExecutionRunner):
        def __init__(self, *a: Any, **kw: Any) -> None:
            super().__init__(*a, **kw)
            self._snap_calls = 0

        def preflight(self, *, assets=None):
            self._last_preflight_status = "passed"
            self._preflight_passed_at = micro.time.monotonic()
            self._preflight_assets = {"ETH"}
            return {"status": "passed"}

        def _market_snapshot(self, asset: str):
            self._snap_calls += 1
            px = "1919" if self._snap_calls == 1 else "2100"
            return {
                "binance": {"bids": [[px, "5"]], "asks": [[px, "5"]]},
                "bybit": {"b": [[px, "5"]], "a": [[px, "5"]]},
                "hyperliquid": {"levels": [[{"px": px, "sz": "5"}], [{"px": px, "sz": "5"}]]},
            }, 50

        def _account_snapshot(self, asset: str):
            return {"bybit": {}, "hyperliquid": {}, "liquidation_distance_passed": True}

        def _safe_unwind(self, asset: str, *, filled=None, books, client_tag: str) -> bool:
            seen_unwind_books.append(dict(books))
            return True

    class _BN:
        def __init__(self) -> None:
            self.buy_qty = Decimal("0.0501")

        def account(self):
            return {"canTrade": True, "takerCommission": 10, "balances": [{"asset": "USDC", "free": "500", "locked": "0"}, {"asset": "ETH", "free": "0.0500499", "locked": "0"}]}

        def instrument(self, _s):
            return {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"}, {"filterType": "MIN_NOTIONAL", "minNotional": "5"}, {"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}

        def create_ioc(self, **kw):
            if kw["side"] == "SELL":
                raise micro.MicroExecutionError("exit_rejected")
            self.buy_qty = kw["quantity"]
            return {"orderId": 11}

        def fills(self, **_kw):
            fee = self.buy_qty * Decimal("0.001")
            return [{"symbol": "ETHUSDC", "isBuyer": True, "qty": str(self.buy_qty), "price": "1919", "commission": str(fee), "commissionAsset": "ETH", "orderId": 11}]

    class _BY:
        def instrument(self, _s):
            return {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.01"}}

        def set_leverage(self, *_a):
            return {"retCode": 0}

        def create_ioc(self, **_kw):
            return {"result": {"orderId": "by-1"}}

        def fills(self, **_kw):
            return [{"symbol": "ETHPERP", "side": "Sell", "execQty": "0.04", "execPrice": "1919", "execFee": "0.04", "orderId": "by-1"}]

    class _HL:
        api_url = micro.HYPERLIQUID

        def meta(self):
            return {"universe": [{"name": "ETH", "szDecimals": 3}]}

        def set_leverage(self, *_a, **_kw):
            return {"status": "ok"}

        def create_ioc(self, **_kw):
            return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": "0.01", "oid": 55}}]}}}

        def fills(self, **_kw):
            return [{"coin": "ETH", "side": "A", "sz": "0.01", "px": "1919", "fee": "0.01", "oid": 55, "crossed": True}]

    bn_mock = _BN()
    runner = _ExitFailRunner(cfg, binance=bn_mock, bybit=_BY(), hyperliquid=_HL())  # type: ignore[arg-type]
    out = runner.execute_asset("ETH", confirmation="pass4-micro-exit")
    assert bn_mock.buy_qty == Decimal("0.0501")  # grossed up from 0.0500 to cover 10 bps ETH commission!
    assert out["status"] == "failed_safe_unwind"
    assert seen_unwind_books and seen_unwind_books[-1]["binance"]["bids"][0][0] == "2100"


# --------------------------------------------------------------------------- 5. EXEC-07, EXEC-14, EXEC-15, EXEC-16


def test_exec07_and_exec14_pagination_spot_exclusion_house_money_and_low_lev_isolated_adl():
    # 1. Same-millisecond boundary events are NOT dropped across pages (`cursor_ms = max_time`)
    events = [
        {"time": 1_000, "hash": "0x1", "delta": {"coin": "BTC", "usdc": "1.0"}},
        {"time": 2_000, "hash": "0x2", "delta": {"coin": "BTC", "usdc": "2.0"}},
        {"time": 2_000, "hash": "0x3", "delta": {"coin": "ETH", "usdc": "3.0"}},  # shares t=2000 across page boundary!
        {"time": 3_000, "hash": "0x4", "delta": {"coin": "SOL", "usdc": "4.0"}},
    ]

    def fund_handler(req: httpx.Request) -> httpx.Response:
        st = int(json.loads(req.content).get("startTime", 0))
        return httpx.Response(200, json=[e for e in events if e["time"] >= st][:2])

    with httpx.Client(transport=httpx.MockTransport(fund_handler)) as client:
        got = pta.fetch_all_user_funding(client, "0xA", page_cap=2)
    assert len(got) == 4

    # start_time_ms filters initial userFills even when < page_cap
    def fills_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"tid": 1, "coin": "BTC", "time": 500}, {"tid": 2, "coin": "BTC", "time": 1_500}])

    with httpx.Client(transport=httpx.MockTransport(fills_handler)) as client:
        fl_sub, fl_comp = pta.fetch_all_user_fills(client, "0xA", start_time_ms=1_000, page_cap=2_000)
    assert [r["tid"] for r in fl_sub] == [2] and fl_comp is False

    # 2. Spot fills (@107, PURR/USDC) excluded; vault flows tracked; house-money account bounded; 2x isolated ADL quiet
    ledger = [
        {"time": 1, "delta": {"type": "deposit", "usdc": "1000.0"}},
        {"time": 2, "delta": {"type": "withdraw", "usdc": "1000.0", "fee": "0.0"}},  # net_flow == 0.0 (house money!)
    ]
    assert pta.net_external_usdc_flow_usd(
        [{"time": 1, "delta": {"type": "deposit", "usdc": "1000"}}, {"time": 2, "delta": {"type": "vaultDeposit", "usdc": "400"}}],
        "0xHM",
    ) == pytest.approx(600.0)

    snap = {
        "account_address": "0xHM",
        "observed_at_ms": NOW,
        "ledger_updates": ledger,
        "fills": [
            {"coin": "@107", "px": "25.0", "sz": "10.0", "side": "B", "fee": "0.5", "feeToken": "HYPE", "closedPnl": "0"},
            {"coin": "PURR/USDC", "px": "0.2", "sz": "100", "side": "A", "fee": "0.1", "closedPnl": "5.0"},
            {"coin": "BTC", "px": "60000", "sz": "0.01", "side": "B", "fee": "0.09", "closedPnl": "500.0"},
        ],
        "funding": [],
        "clearinghouse": {
            "marginSummary": {"accountValue": "500.0"},
            "withdrawable": "400.0",
            "assetPositions": [
                {
                    "position": {
                        "coin": "ZRO",
                        "szi": "50.0",
                        "entryPx": "2.00",
                        "positionValue": "104.0",
                        "marginUsed": "50.0",
                        "unrealizedPnl": "4.0",  # +4% gain at 2x isolated -> adl_rank = 1.04 * 2.08 = 2.163 <= 2.50!
                        "leverage": {"type": "isolated", "value": 2},
                    }
                }
            ],
        },
        "spot": {"balances": []},
        "open_orders": [],
        "all_mids": {"BTC": 60_000.0, "ZRO": 2.08},
        "mark_prices": {"BTC": 60_000.0, "ZRO": 2.08},
    }
    attr = pta.compute_post_trade_attribution(snap)
    assert attr.total_fills == 1  # Spot fills excluded!
    assert {c.coin for c in attr.per_coin} == {"BTC", "ZRO"}
    # House-money return = ($500 - $0) / $1,000 gross deposited = +50.0% (NOT +50,000%!)
    assert attr.deposited_capital_usd == pytest.approx(1_000.0)
    assert attr.account_return_pct == pytest.approx(50.0)
    assert attr.adl_warnings == []  # 2x isolated +4% gain does not trigger false ADL warning!


def test_exec15_and_exec16_stress_unlisted_nan_vol_margin_cap_and_okx_kilo_scaling():
    # 1. Unlisted NaN coins (>50% of universe) must NOT collapse leg_vol median to 0.0!
    n_h = 80
    hours = np.arange(n_h, dtype="int64") * 3_600_000
    rng = np.random.default_rng(11)
    rows = []
    for idx in range(5):
        coin = f"C{idx}"
        # Only 2 of 5 coins are listed; 3 of 5 are all-NaN unlisted placeholders!
        px = (100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.01, n_h)))) if idx < 2 else np.full(n_h, np.nan)
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours,
                    "coin": coin,
                    "hl_close": px,
                    "hl_notional_usd": 1e6,
                    "hl_fund_next": 0.0,
                    "hl_rate_h": 0.0,
                    "bybit_close": px,
                    "bybit_notional_usd": 1e6,
                    "bybit_fund_next": 0.0,
                    "bybit_rate_h": 0.0,
                }
            )
        )
    mkt = af.build_wide_market(pd.concat(rows, ignore_index=True))
    _, _, leg_vol, _ = es.metamodel_stream(mkt, {"xs_rev_L24_r12": 1.0})
    assert float(leg_vol.iloc[-1]) > 0.005  # true ~1% vol, NOT 0.0!

    # Margin buffer audit caps all percentages < 100%
    crash_idx = pd.RangeIndex(72)
    crash_ret = pd.DataFrame({"A": np.full(72, -0.02)}, index=crash_idx)
    crash_w = pd.DataFrame({"A": np.full(72, 1.0)}, index=crash_idx)
    mb = es.margin_buffer_audit(crash_ret, crash_w, leverages=(3.0,))
    assert mb[0]["recommended_free_margin_pct"] < 100.0
    assert abs(mb[0]["worst_72h_single_leg_pct"]) < 100.0
    assert abs(mb[0]["max_single_leg_dd_pct"]) < 100.0

    # 2. OKX kFloki symbol, 1000x price scaling on k* swaps, and crossed-book rejection
    assert cex.okx_symbol("kFloki") == "FLOKI-USDT-SWAP"
    assert cex.okx_symbol("kPEPE") == "PEPE-USDT-SWAP"

    okx_client = SimpleNamespace(
        get=lambda _p, _q: {
            "code": "0",
            "data": [
                {"instId": "PEPE-USDT-SWAP", "bidPx": "0.0000100", "askPx": "0.0000102"},
                {"instId": "BTC-USDT-SWAP", "bidPx": "90100.0", "askPx": "90000.0"},  # crossed ask < bid -> rejected!
            ],
        }
    )
    mids = cex.fetch_live_mid_prices(okx_client, "okx")
    assert mids == {"PEPE-USDT-SWAP": pytest.approx(0.0101)}

    h = cex.HOUR_MS
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % h)

    def okx_kline_handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"code": "0", "msg": "", "data": [[str(t0), "0.00001", "0.000012", "0.000009", "0.000011", "100", "1000000", "11", "1"]]},
        )

    with cex.PublicGetClient(cex.OKX_API, transport=httpx.MockTransport(okx_kline_handler)) as c_okx:
        kl = cex.collect_klines_1h(c_okx, "okx", "PEPE-USDT-SWAP", t0, t0 + h, now_ms=t0 + 2 * h)
    assert kl.rows["close"].iloc[0] == pytest.approx(0.011)
    # close * volume preserves exact USD notional ($11.0)!
    assert (kl.rows["close"].iloc[0] * kl.rows["volume"].iloc[0]) == pytest.approx(11.0)
