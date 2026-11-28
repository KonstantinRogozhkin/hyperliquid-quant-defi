"""Comprehensive regression tests for Wave 1 execution, MM, attribution & micro-execution fixes."""

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
import pandas as pd
import pytest
from typer.testing import CliRunner

from hl_quant import cross_usdc_micro_execution as micro
from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_testnet_executor as ex
from hl_quant import hl_testnet_risk as risk

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_796_500_000_000


def _meta(*coins: str, sz_decimals: int = 2) -> dict[str, ex.AssetSpec]:
    return {c: ex.AssetSpec(c, sz_decimals=sz_decimals, max_leverage=10) for c in coins}


# --------------------------------------------------------------------------- 1. Testnet Executor & Risk Probe


def test_c1_spot_clearinghouse_retries_429_and_raises_on_persistent_5xx():
    calls = {"spot": 0, "ua": 0}

    def recovering_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(
                200,
                json={
                    "marginSummary": {"accountValue": "154.0", "totalMarginUsed": "120.0"},
                    "withdrawable": "0.0",
                    "assetPositions": [],
                },
            )
        if t == "userAbstraction":
            calls["ua"] += 1
            if calls["ua"] == 1:
                return httpx.Response(429, json={"error": "rate_limit"})
            return httpx.Response(200, json="unifiedAccount")
        if t == "spotClearinghouseState":
            calls["spot"] += 1
            if calls["spot"] < 3:
                return httpx.Response(502, text="Bad Gateway")
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "983.5", "hold": "120.0"}]})
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(recovering_handler)) as client:
        snap = ex.fetch_testnet_account(client, "0xUNIFIED")
    assert snap.equity_usd == pytest.approx(983.5)
    assert snap.free_margin_usd == pytest.approx(863.5)
    assert calls["spot"] == 3 and calls["ua"] == 2

    def persistent_500_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "154.0"}, "withdrawable": "0.0"})
        if t == "userAbstraction":
            return httpx.Response(404, json={"error": "unsupported"})
        if t == "spotClearinghouseState":
            return httpx.Response(503, text="Service Unavailable")
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(persistent_500_handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            ex.fetch_testnet_account(client, "0xUNIFIED")


def test_c2_and_h34_orphan_close_px_oracle_bounds_partial_min_usd_and_slicing():
    # Missing or non-positive oracle must refuse to price off rogue quotes
    assert ex._orphan_close_px("sell", 10.0, 12.0, None, 2) is None
    assert ex._orphan_close_px("buy", 10.0, 12.0, 0.0, 2) is None

    # Rogue low ask ($10 vs $100 oracle) on ask-only book must NOT drag sell below $98
    assert ex._orphan_close_px("sell", None, 10.0, 100.0, 2) == pytest.approx(100.0)
    # Normal nearby ask ($99 >= $98) joins ask
    assert ex._orphan_close_px("sell", None, 99.0, 100.0, 2) == pytest.approx(99.0)
    # Favorable high bid ($105 > $100 oracle) prices passively above $105
    px_fav_sell = ex._orphan_close_px("sell", 105.0, None, 100.0, 2)
    assert px_fav_sell is not None and px_fav_sell > 105.0

    # Symmetric buy checks: rogue high bid ($500 vs $100 oracle) ignored; favorable low ask ($90) improved passively
    assert ex._orphan_close_px("buy", 500.0, None, 100.0, 2) == pytest.approx(100.0)
    px_fav_buy = ex._orphan_close_px("buy", None, 90.0, 100.0, 2)
    assert px_fav_buy is not None and px_fav_buy < 90.0

    # Partial trim < $15 min_order_usd is skipped, while full close >= $0.50 is placed
    acct_partial = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=900.0,
        positions_sz={"VVV": 1.0, "DUST": 2.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"VVV": 25.0, "DUST": 4.0},
    )
    limits = ex.RiskLimits(min_order_usd=15.0, max_child_usd=1_000.0)
    # VVV held $25 vs target 0.02 ($20) -> $5 partial trim (< $15) skipped; DUST $4 untargeted full close placed!
    ords = ex.plan_orphan_unwind_orders(
        acct_partial,
        _meta("VVV", "DUST"),
        {},
        {"VVV": (24.5, None, NOW), "DUST": (None, None, NOW)},
        {"VVV": 25.0, "DUST": 2.0},
        limits=limits,
        target_weights={"VVV": 0.02},
    )
    assert [(o.coin, o.sz) for o in ords] == [("DUST", 2.0)]

    # Large $2,500 orphan unwind slices into 3 child orders <= $1,000 and residual sums all slices
    acct_big = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=8_000.0,
        positions_sz={"BIG": 25.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"BIG": 2_500.0},
    )
    sliced = ex.plan_orphan_unwind_orders(
        acct_big, _meta("BIG"), {}, {"BIG": (None, None, NOW)}, {"BIG": 100.0}, limits=limits
    )
    assert len(sliced) == 3
    assert [o.child_index for o in sliced] == [0, 1, 2]
    assert all(o.notional_usd <= 1_000.0 for o in sliced)
    resid_conservative = ex.orphan_residual_notional_usd(
        acct_big, _meta("BIG"), {}, {"BIG": (None, None, NOW)}, {"BIG": 100.0}, limits=limits
    )
    assert resid_conservative["BIG"] == pytest.approx(2_500.0, abs=1.0)
    resid = ex.orphan_residual_notional_usd(
        acct_big, _meta("BIG"), {}, {"BIG": (None, None, NOW)}, {"BIG": 100.0}, limits=limits, assume_unwinds_fill=True
    )
    assert resid["BIG"] == pytest.approx(0.0, abs=1.0)


def test_h2_foreign_mm_coins_protected_even_when_present_in_target_weights():
    acct = ex.AccountSnapshot(
        equity_usd=2_000.0,
        free_margin_usd=1_800.0,
        positions_sz={"ETH": 0.2, "SOL": 0.0},
        open_orders=[{"coin": "ETH", "oid": 501}, {"coin": "SOL", "oid": 502}],
        observed_at_ms=NOW,
        position_notional_usd={"ETH": 500.0},
    )
    bbo = {"ETH": (2_500.0, 2_501.0, NOW), "SOL": (100.0, 100.1, NOW)}
    adapter = ex.DryRunAdapter()
    # ETH is in target_weights AND in foreign_coins: normal cycle must NOT cancel oid=501 or trade ETH!
    res = ex.execute_rebalance_cycle(
        acct,
        {"ETH": -0.15, "SOL": 0.15},
        _meta("ETH", "SOL", sz_decimals=4),
        bbo,
        adapter,
        high_water_mark_usd=2_000.0,
        now_ms=NOW,
        foreign_coins={"ETH"},
        foreign_order_oids={501},
    )
    assert res["status"] == "ok"
    assert res["foreign_coins"] == ["ETH"]
    assert [c["oid"] for c in adapter.cancelled] == [502]
    assert {o["coin"] for o in adapter.placed} == {"SOL"}


def test_h3_killswitch_orphan_gross_derisk_scaling_bulk_rejections_and_single_coin_cap(tmp_path: Path):
    # 1. Unquoted coin WITH non-zero target_weights still counts full held notional in gross check
    acct_gross = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=800.0,
        positions_sz={"VVV": 28.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"VVV": 700.0},
    )
    bbo = {"SOL": (100.0, 100.1, NOW)}
    v_gross = ex.check_kill_switch(
        acct_gross,
        {"SOL": 0.90, "VVV": 0.05},  # 0.90 + max(0.05, 0.70) = 1.60 > 1.50
        bbo,
        ex.RiskLimits(max_gross_leverage=1.50),
        high_water_mark_usd=1_000.0,
        now_ms=NOW,
    )
    assert any(v.startswith("gross_leverage:1.60x") for v in v_gross)

    # 2. Low free margin with held == target_weights STILL emits reduce_only de-risking orders
    acct_margin = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=80.0,  # 8% < 20%
        positions_sz={"SOL": 2.0},  # $200 == 0.20 weight
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 200.0},
    )
    adapter_m = ex.DryRunAdapter()
    res_m = ex.execute_rebalance_cycle(
        acct_margin,
        {"SOL": 0.20},
        _meta("SOL"),
        bbo,
        adapter_m,
        high_water_mark_usd=1_000.0,
        now_ms=NOW,
    )
    assert res_m["derisk_mode"] == "reduce_only"
    assert res_m["mode"] == "dry_run"
    assert len(adapter_m.placed) == 1 and adapter_m.placed[0]["reduce_only"] is True

    # 3. Rejected ALO orders & transport errors set status="order_rejected", placed_count=0, and still write audit log
    class _RejectingBulkAdapter:
        def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> dict:
            return {"status": "ok"}

        def cancel_all_open_orders(self, open_orders: list[dict]) -> list[dict]:
            return []

        def place_bulk_alo_orders(self, orders: list[ex.PlannedOrder]) -> list[dict]:
            return [
                {
                    "mode": "testnet_live",
                    "order": o.to_dict(),
                    "response": {"status": "ok", "response": {"data": {"statuses": [{"error": "Post only order would have immediately matched."}]}}},
                }
                for o in orders
            ]

    audit_rej = tmp_path / "rej_audit.jsonl"
    acct_ok = ex.AccountSnapshot(
        equity_usd=1_000.0, free_margin_usd=900.0, positions_sz={}, open_orders=[], observed_at_ms=NOW
    )
    res_rej = ex.execute_rebalance_cycle(
        acct_ok, {"SOL": 0.20}, _meta("SOL"), bbo, _RejectingBulkAdapter(), audit_log_path=audit_rej, now_ms=NOW  # type: ignore[arg-type]
    )
    assert res_rej["status"] == "order_rejected"
    assert res_rej["placed_count"] == 0
    assert res_rej["rejected_count"] == 1
    assert audit_rej.is_file()

    class _ThrowingAdapter(_RejectingBulkAdapter):
        def place_bulk_alo_orders(self, orders: list[ex.PlannedOrder]) -> list[dict]:
            raise httpx.ReadTimeout("socket timeout")

    audit_err = tmp_path / "err_audit.jsonl"
    res_err = ex.execute_rebalance_cycle(
        acct_ok, {"SOL": 0.20}, _meta("SOL"), bbo, _ThrowingAdapter(), audit_log_path=audit_err, now_ms=NOW  # type: ignore[arg-type]
    )
    assert res_err["status"] == "order_rejected"
    assert res_err["placed_count"] == 0 and res_err["rejected_count"] == 1
    assert audit_err.is_file()

    # 4. Single-coin neutralize_target_weights honors max_gross
    assert ex.neutralize_target_weights({"BTC": 0.20}, max_gross=0.08, max_single_coin_weight=0.25) == {"BTC": 0.08}
    assert ex.neutralize_target_weights({"BTC": 0.20}, max_gross=0.0, max_single_coin_weight=0.25) == {}


def test_h4_oracle_adjusted_hwm_dry_run_cli_hygiene_and_risk_probe_seeding(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts" / "analytics"))
    exec_cli = importlib.import_module("run_hl_testnet_executor")
    probe_cli = importlib.import_module("run_hl_testnet_risk_probe")

    # +30% thin-book mark spike on XMR: mark equity is $1,300, but oracle-adjusted equity is $1,000!
    acct_spike = ex.AccountSnapshot(
        equity_usd=1_300.0,
        free_margin_usd=1_100.0,
        positions_sz={"XMR": 1.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_entry_px={"XMR": 400.0},
        position_unrealized_pnl_usd={"XMR": 300.0},  # mark=700 vs oracle=400 -> +$300 phantom mark uPnL
    )
    ctxs = {"XMR": risk.AssetCtx(mark_px=700.0, oracle_px=400.0, funding_apr_pct=0.0)}
    assert risk.oracle_adjusted_equity_usd(acct_spike, ctxs) == pytest.approx(1_000.0)

    # Probe seeds missing HWM file using oracle-adjusted equity ($1,000, NOT $1,300)
    def probe_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(
                200,
                json={
                    "marginSummary": {"accountValue": "1300.0", "totalMarginUsed": "100.0"},
                    "withdrawable": "1200.0",
                    "assetPositions": [
                        {
                            "position": {
                                "coin": "XMR",
                                "szi": "1.0",
                                "entryPx": "400.0",
                                "positionValue": "700.0",
                                "unrealizedPnl": "300.0",
                            }
                        }
                    ],
                },
            )
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if t == "metaAndAssetCtxs":
            return httpx.Response(
                200, json=[{"universe": [{"name": "XMR"}]}, [{"markPx": "700.0", "oraclePx": "400.0", "funding": "0"}]]
            )
        if t == "l2Book":
            return httpx.Response(503)
        return httpx.Response(200, json=[])

    real_client = httpx.Client
    monkeypatch.setattr(probe_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(probe_handler)))
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0xSPIKE")
    hwm_path = tmp_path / "seeded_hwm.json"
    risk_out = tmp_path / "risk" / "latest.json"
    r_probe = CliRunner().invoke(probe_cli.app, ["--hwm-state", str(hwm_path), "--output-json", str(risk_out)])
    assert r_probe.exit_code == 0, r_probe.output
    assert ex.load_high_water_mark(hwm_path) == pytest.approx(1_000.0)
    snap_json = json.loads(risk_out.read_text())
    assert snap_json["unknown_books"] == 1
    assert snap_json["orphan_positions"] == {}

    # Dry-run executor CLI must NOT overwrite HWM!
    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"positions": {"SOL": {"hl_weight": 0.15}, "ETH": {"hl_weight": -0.15}}}))

    def exec_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(
                200,
                json={"universe": [{"name": "SOL", "szDecimals": 2, "maxLeverage": 20}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]},
            )
        if t == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "5000.0", "totalMarginUsed": "0.0"}, "withdrawable": "5000.0", "assetPositions": []})
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if t == "l2Book":
            c = json.loads(req.content)["coin"]
            px = "100.0" if c == "SOL" else "2500.0"
            ask = "100.1" if c == "SOL" else "2500.5"
            return httpx.Response(200, json={"coin": c, "time": NOW, "levels": [[{"px": px, "sz": "10"}], [{"px": ask, "sz": "10"}]]})
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": []}, []])
        return httpx.Response(200, json=[])

    monkeypatch.setattr(exec_cli.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(exec_handler)))
    monkeypatch.setenv("HL_MM_COINS", "ETH")
    r_exec = CliRunner().invoke(exec_cli.app, ["--state-json", str(state_file), "--hwm-state", str(hwm_path)])
    assert r_exec.exit_code == 0, r_exec.output
    # Dry-run did not ratchet HWM from 1,000 up to 5,000!
    assert ex.load_high_water_mark(hwm_path) == pytest.approx(1_000.0)


# --------------------------------------------------------------------------- 2. Market Maker (C-3, H-2.3, M-7)


def test_c3_and_h23_mm_modify_failure_cancels_prev_oid_and_guards_stale_replace(tmp_path: Path):
    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(requote_threshold_bps=0.5)
    bk = mm.L2BookSnapshot("ETH", NOW, 2505.0, 2505.5, 5.0, 5.0, 0.0, 2505.25, 2505.25, 2.0)
    dec = mm.compute_as_quotes(bk, spec, inventory_sz=0.0, sigma_tau_bps=8.0, params=params)

    class _ModifyFailAdapter:
        def __init__(self, *, cancel_already_filled: bool) -> None:
            self.cancel_already_filled = cancel_already_filled
            self.cancels: list[list[dict]] = []
            self.placed: list[ex.PlannedOrder] = []

        def modify_bulk_alo_orders(self, modifications: list[tuple[int, ex.PlannedOrder]]) -> list[dict]:
            return [
                {
                    "mode": "testnet_live",
                    "prev_oid": oid,
                    "order": o.to_dict(),
                    "response": {"status": "ok", "response": {"data": {"statuses": [{"error": "Cannot modify order"}]}}},
                }
                for oid, o in modifications
            ]

        def cancel_all_open_orders(self, open_orders: list[dict], *, fast: bool = False) -> list[dict]:
            self.cancels.append(open_orders)
            if self.cancel_already_filled:
                return [
                    {
                        "mode": "testnet_live",
                        "response": {
                            "status": "ok",
                            "response": {"type": "cancel", "data": {"statuses": [{"error": "Order was never placed, already canceled, or filled."}]}},
                        },
                    }
                ]
            return [
                {
                    "mode": "testnet_live",
                    "response": {"status": "ok", "response": {"type": "cancel", "data": {"statuses": ["success"]}}},
                }
            ]

        def place_bulk_alo_orders(self, orders: list[ex.PlannedOrder]) -> list[dict]:
            self.placed.extend(orders)
            return [
                {
                    "mode": "testnet_live",
                    "order": o.to_dict(),
                    "response": {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 900 + i}}]}}},
                }
                for i, o in enumerate(orders)
            ]

    # Case A: prev_oid was still resting -> explicit cancel succeeds -> safe fresh replacement placed!
    ad_resting = _ModifyFailAdapter(cancel_already_filled=False)
    active_a: dict[str, dict[str, Any]] = {"buy": {"oid": 101, "px": 2500.0, "sz": 0.012}, "sell": {"oid": 102, "px": 2501.0, "sz": 0.012}}
    oids_a: set[int] = {101, 102}
    pl_a, md_a, cn_a, _ = asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec, active_a, oids_a, ad_resting, mid_px=bk.mid_px, params=params, recv_ms=NOW, audit_log_path=None
        )
    )
    assert md_a == 0 and cn_a == 2 and pl_a == 2
    assert len(ad_resting.cancels) == 2 and len(ad_resting.placed) == 2

    # Case B: cancel reports order already filled -> do NOT blindly re-place on stale pre-fill inventory!
    ad_filled = _ModifyFailAdapter(cancel_already_filled=True)
    active_b: dict[str, dict[str, Any]] = {"buy": {"oid": 201, "px": 2500.0, "sz": 0.012}, "sell": {"oid": 202, "px": 2501.0, "sz": 0.012}}
    oids_b: set[int] = {201, 202}
    pl_b, md_b, cn_b, _ = asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec, active_b, oids_b, ad_filled, mid_px=bk.mid_px, params=params, recv_ms=NOW, audit_log_path=None
        )
    )
    assert md_b == 0 and cn_b == 0 and pl_b == 0
    assert ad_filled.placed == []
    assert active_b == {}

    # Case C: DryRunAdapter audit records carry mode="dry_run" so load_logged_order_oids ignores them
    mm_audit = tmp_path / "mm_audit.jsonl"
    dry_ad = ex.DryRunAdapter()
    active_c: dict[str, dict[str, Any]] = {}
    oids_c: set[int] = set()
    asyncio.run(
        mm._sync_coin_quotes_atomic(
            "ETH", dec, active_c, oids_c, dry_ad, mid_px=bk.mid_px, params=params, recv_ms=NOW, audit_log_path=mm_audit
        )
    )
    assert ex.load_logged_order_oids(mm_audit) == set()


def test_m7_mm_maker_rebate_sub10_flatten_leadlag_sub50ms_dms_warning_and_ws_reconnect(monkeypatch):
    # 1. Negative maker fee (rebate) increases net PnL
    lg = mm._InventoryLedger(maker_fee_bps=-0.2)
    lg.on_fill("buy", 2000.0, 1.0, fee_usd=-0.40)
    lg.on_fill("sell", 2000.0, 1.0, fee_usd=-0.40)
    assert lg.fees_usd == pytest.approx(-0.80)
    assert lg.net_pnl_usd(2000.0) == pytest.approx(0.80)

    # 2. LeadLagEstimator uses actual dt_sec (<50ms) instead of 50ms floor
    est_fast = mm.LeadLagEstimator(basis_half_life_sec=10.0)
    est_fast.update_cex(NOW, 2500.0)
    assert est_fast.impulse_bps(NOW, 2500.0) == pytest.approx(0.0)
    est_fast.update_cex(NOW + 5, 2502.5)  # +10 bps jump in 5ms
    _ = est_fast.impulse_bps(NOW + 5, 2500.0)
    # After only 5ms on a 10s half-life, EWMA basis should move < 0.01 bps (vs ~0.035 bps under old 50ms floor)
    assert est_fast._basis_bps is not None and est_fast._basis_bps < 0.01

    # 3. Sub-$10.50 ($5.00) full-account flatten_on_exit executes and counts in fills; DMS warning rendered
    def meta_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        return httpx.Response(200, json=[])

    ws_msgs = [
        {"channel": "l2Book", "data": {"coin": "ETH", "time": NOW, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}},
        {"channel": "trades", "data": [{"coin": "ETH", "side": "A", "px": "2490.0", "sz": "1.0", "time": NOW + 100}]},
    ]
    dry = ex.DryRunAdapter()
    params_small = mm.ASParams(base_order_usd=5.0, min_order_usd=4.0, max_inventory_usd=50.0)
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as client:
        summ, fills_df, _ = asyncio.run(
            mm.run_live_mm_ws_session(
                "ETH", dry, execute_live=False, flatten_on_exit=True, params=params_small, client=client, ws_messages=ws_msgs
            )
        )
    assert summ.flattened_exit_sz == pytest.approx(0.002)
    assert summ.fills_count == 2  # 1 passive buy + 1 flatten IOC sell
    assert len(fills_df) == 2

    armed_summ = mm.MMSessionSummary(**{**summ.to_dict(), "dms_left_armed": True})
    rich = mm.format_mm_telegram_rich_html(armed_summ, params_small)
    assert "scheduleCancel" in rich and "DMS оставлен активным" in rich

    # 4. Bounded WS reconnect recovers from mid-session ConnectionClosed
    import websockets

    conn_attempts = {"n": 0}

    class _FakeWS:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.sent: list[str] = []
            self._step = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

        async def recv(self) -> str:
            self._step += 1
            if self.idx == 1 and self._step == 2:
                raise ConnectionResetError("transient socket drop")
            if self._step == 1:
                return json.dumps(
                    {"channel": "bbo", "data": {"coin": "ETH", "time": NOW, "bbo": [{"px": "2500.0", "sz": "5"}, {"px": "2500.5", "sz": "5"}]}}
                )
            await asyncio.sleep(0.35)
            return json.dumps({"channel": "pong"})

    def fake_connect(*_a, **_kw):
        conn_attempts["n"] += 1
        return _FakeWS(conn_attempts["n"])

    monkeypatch.setattr(websockets, "connect", fake_connect)
    with httpx.Client(transport=httpx.MockTransport(meta_handler)) as client:
        rc_summ, _, _ = asyncio.run(
            mm.run_live_mm_ws_session(
                "ETH", ex.DryRunAdapter(), execute_live=False, duration_sec=1.0, enable_lead_lag=False, client=client
            )
        )
    assert conn_attempts["n"] >= 2
    assert rc_summ.ticks_observed >= 2
    assert any(h.startswith("ws_reconnect:1:") for h in rc_summ.halt_events)


# --------------------------------------------------------------------------- 3. Post-Trade Attribution (H-1, H-9)


def test_h1_and_h9_attribution_unified_withdrawable_mark_liq_pagination_and_dry_run_skip(tmp_path: Path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        "\n".join(
            [
                json.dumps({"ts_ms": NOW, "mode": "dry_run", "cancelled_count": 5, "orders": [{"oid": 100_001, "response": {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 100_001}}]}}}}]}),
                json.dumps({"ts_ms": NOW, "mode": "testnet_live", "cancelled_count": 1, "orders": [{"mode": "testnet_live", "response": {"status": "ok", "response": {"data": {"statuses": [{"filled": {"oid": 42}}]}}}}]}),
            ]
        )
        + "\n"
    )
    assert pta.extract_audit_oids(audit_path) == {42}
    assert pta.count_audit_actions(audit_path) == 2

    # Paginate >2000 fills via userFillsByTime and >500 ledger updates via userNonFundingLedgerUpdates
    all_fills = [
        {"tid": i + 1, "coin": "BTC", "px": "60000", "sz": "0.001", "side": "B", "oid": 42 if i == 0 else 999, "time": 1_000 + i, "fee": "-0.006", "closedPnl": "0"}
        for i in range(5)
    ]
    all_ledger = [
        {"time": 500 + i, "hash": f"0xh{i}", "delta": {"type": "deposit", "usdc": "200.0"}}
        for i in range(4)
    ]

    def info_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        t = body["type"]
        if t == "userFills":
            return httpx.Response(200, json=all_fills[-3:])
        if t == "userFillsByTime":
            st = int(body.get("startTime", 0))
            return httpx.Response(200, json=[r for r in all_fills if r["time"] >= st][:3])
        if t == "userNonFundingLedgerUpdates":
            st = int(body.get("startTime", 0))
            return httpx.Response(200, json=[r for r in all_ledger if r["time"] >= st][:2])
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": [{"name": "BTC"}, {"name": "NOFILL"}]}, [{"markPx": "58000.0"}, {"markPx": "10.0"}]])
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(info_handler)) as client:
        fills_out, fills_complete = pta.fetch_all_user_fills(client, "0xU", page_cap=3)
        ledger_out = pta.fetch_all_user_ledger_updates(client, "0xU", page_cap=2)
    assert len(fills_out) == 5 and fills_complete is True
    assert len(ledger_out) == 4

    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    (paper_dir / "state.json").write_text(json.dumps({"starting_capital_usd": 100_000.0}))
    pd.DataFrame({"tick_hour_ms": [NOW], "equity_usd": [101_000.0]}).to_parquet(paper_dir / "ticks.parquet", index=False)

    snap = {
        "account_address": "0xU",
        "observed_at_ms": NOW,
        "user_abstraction": "unifiedAccount",
        "ledger_updates": ledger_out,  # 4 * $200 = $800 deposited
        "fills": fills_out,
        "fills_complete": True,
        "funding": [{"delta": {"coin": "NOFILL", "usdc": "0.50"}}],
        "clearinghouse": {
            "marginSummary": {"accountValue": "120.0"},
            "withdrawable": "0.0",
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.005",
                        "entryPx": "60000",
                        "positionValue": "290.0",
                        "unrealizedPnl": "-10.0",
                        "liquidationPx": "55100.0",  # 5.0% below mark 58,000 (even though mid is 70,000!)
                        "leverage": {"type": "cross", "value": 5},
                    }
                }
            ],
        },
        "spot": {"balances": [{"coin": "USDC", "total": "808.0", "hold": "120.0"}]},
        "open_orders": [],
        "all_mids": {"BTC": 70_000.0, "NOFILL": 10.0},
        "mark_prices": {"BTC": 58_000.0, "NOFILL": 10.0},
    }
    attr = pta.compute_post_trade_attribution(snap, audit_log_path=audit_path, paper_dir=paper_dir)
    assert attr.withdrawable_usd == pytest.approx(688.0)  # 808 - 120 on Unified Account
    btc_row = next(c for c in attr.per_coin if c.coin == "BTC")
    nofill_row = next(c for c in attr.per_coin if c.coin == "NOFILL")
    # Liq distance uses mark_px (58,000), NOT distorted mid (70,000): (58000 - 55100)/58000 = 5.0%
    assert btc_row.liq_distance_pct == pytest.approx(5.0, abs=0.05)
    assert nofill_row.maker_share_pct == 0.0  # 0 fills -> 0.0%, never 100.0%
    assert attr.paper_comparison is not None
    # Deposited $800, current equity $808 -> +1.0% matches account_return_pct
    assert attr.account_return_pct == pytest.approx(1.0)
    assert attr.paper_comparison["testnet_return_pct"] == pytest.approx(1.0)
    assert "attributed_return_pct" in attr.paper_comparison

    # Standard Account cross ADL uses perps_eq (not perps_eq + idle spot_usdc)
    snap_std_adl = {
        "account_address": "0xSTD",
        "observed_at_ms": NOW,
        "user_abstraction": "disabled",
        "fills": [],
        "funding": [],
        "clearinghouse": {
            "marginSummary": {"accountValue": "500.0"},
            "withdrawable": "200.0",
            "assetPositions": [
                {
                    "position": {
                        "coin": "SOL",
                        "szi": "-15.0",
                        "entryPx": "120.0",
                        "positionValue": "1500.0",
                        "unrealizedPnl": "300.0",
                        "leverage": {"type": "cross", "value": 5},
                    }
                }
            ],
        },
        "spot": {"balances": [{"coin": "USDC", "total": "9500.0", "hold": "0.0"}]},
        "open_orders": [],
        "all_mids": {"SOL": 100.0},
        "mark_prices": {"SOL": 100.0},
    }
    attr_std = pta.compute_post_trade_attribution(snap_std_adl)
    assert attr_std.per_coin[0].adl_rank_score == pytest.approx((120.0 / 100.0) * (1500.0 / 500.0), abs=0.01)


# --------------------------------------------------------------------------- 4. Cross-USDC Micro Execution (C-4, H-5.2, M-8)


class _MockBinance:
    def __init__(self, *, free_usdc: str = "500", free_btc: str = "0") -> None:
        self.free_usdc = Decimal(free_usdc)
        self.free_btc = Decimal(free_btc)
        self.orders: list[dict[str, Any]] = []

    def account(self) -> dict[str, Any]:
        return {
            "canTrade": True,
            "makerCommission": 10,
            "takerCommission": 10,
            "balances": [
                {"asset": "USDC", "free": str(self.free_usdc), "locked": "0"},
                {"asset": "BTC", "free": str(self.free_btc), "locked": "0"},
                {"asset": "ETH", "free": "0", "locked": "0"},
            ],
        }

    def instrument(self, symbol: str) -> dict[str, Any]:
        step = "0.00001" if symbol.startswith("BTC") else "0.0001"
        return {
            "filters": [
                {"filterType": "LOT_SIZE", "stepSize": step, "minQty": step},
                {"filterType": "MIN_NOTIONAL", "minNotional": "5"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
            ]
        }

    def book(self, symbol: str) -> dict[str, Any]:
        px = "80000" if symbol.startswith("BTC") else "2000"
        return {"bids": [[px, "5"]], "asks": [[px, "5"]]}

    def create_ioc(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        if kwargs["side"] == "SELL":
            self.free_btc = max(Decimal("0"), self.free_btc - kwargs["quantity"])
        else:
            self.free_btc += kwargs["quantity"]
        return {"orderId": 7001}

    def fills(self, *, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return []


class _MockBybit:
    def __init__(self, *, pos_size: str = "0") -> None:
        self.pos_size = Decimal(pos_size)
        self.leverages_set: list[tuple[str, Decimal]] = []
        self.orders: list[dict[str, Any]] = []
        self.fail_unwind = False

    def query_api(self) -> dict[str, Any]:
        return {"retCode": 0, "result": {"permissions": ["ContractTrade"]}}

    def wallet(self) -> dict[str, Any]:
        return {"result": {"list": [{"totalAvailableBalance": "500"}]}}

    def account_info(self) -> dict[str, Any]:
        return {"result": {"marginMode": "REGULAR_MARGIN"}}

    def fee_rate(self, symbol: str) -> dict[str, Any]:
        return {"result": {"list": [{"takerFeeRate": "0.00055"}]}}

    def positions(self, symbol: str) -> list[dict[str, Any]]:
        if self.pos_size <= 0:
            return []
        return [{"size": str(self.pos_size), "side": "Sell", "positionIM": "25", "positionMM": "2", "positionValue": "50", "liqPrice": "90000", "markPrice": "50000"}]

    def instrument(self, symbol: str) -> dict[str, Any]:
        return {
            "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001", "minNotionalValue": "5"},
            "priceFilter": {"tickSize": "0.1"},
            "leverageFilter": {"maxLeverage": "50"},
        }

    def book(self, symbol: str) -> dict[str, Any]:
        px = "80000" if symbol.startswith("BTC") else "2000"
        return {"b": [[px, "5"]], "a": [[px, "5"]]}

    def set_leverage(self, symbol: str, leverage: Decimal) -> dict[str, Any]:
        self.leverages_set.append((symbol, leverage))
        return {"retCode": 0}

    def create_ioc(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        if kwargs.get("reduce_only") and self.fail_unwind:
            raise micro.MicroExecutionError("bybit_unwind_rejected")
        if kwargs["side"] == "Sell":
            self.pos_size += kwargs["quantity"]
        else:
            self.pos_size = max(Decimal("0"), self.pos_size - kwargs["quantity"])
        return {"result": {"orderId": "bybit-101"}}

    def fills(self, *, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return [
            # Older unrelated fill in 5s skew window with different orderId -> must be filtered out!
            {"symbol": symbol, "side": "Sell", "execQty": "0.999", "execPrice": "50000", "execFee": "0.1", "orderId": "stale-oid"},
            {"symbol": symbol, "side": "Sell", "execQty": "0.001", "execPrice": "50000", "execFee": "0.02", "orderId": "bybit-101"},
        ]


class _MockHL:
    api_url = micro.HYPERLIQUID
    api_wallet_address = "0xagent"

    def __init__(self, *, mode: str = "disabled", szi: str = "0") -> None:
        self.mode = mode
        self.szi = Decimal(szi)
        self.state_calls = 0
        self.leverages_set: list[tuple[str, int]] = []
        self.orders: list[dict[str, Any]] = []

    def state(self) -> dict[str, Any]:
        self.state_calls += 1
        pos = []
        if self.szi != 0:
            pos.append({"position": {"coin": "BTC", "szi": str(self.szi), "positionValue": "50", "marginUsed": "25", "liquidationPx": "90000"}})
        return {"withdrawable": "500", "assetPositions": pos}

    def spot_state(self) -> dict[str, Any]:
        return {"balances": [{"coin": "USDC", "total": "500", "hold": "0"}]}

    def account_mode(self) -> str:
        return self.mode

    def extra_agents(self) -> list[dict[str, Any]]:
        return [{"address": "0xagent", "validUntil": 0}]

    def user_fees(self) -> dict[str, Any]:
        return {"userCrossRate": "0.00045"}

    def meta(self) -> dict[str, Any]:
        return {"universe": [{"name": "BTC", "szDecimals": 5, "maxLeverage": 40}, {"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]}

    def mark_prices(self) -> dict[str, Any]:
        return {"BTC": "80000", "ETH": "2000"}

    def book(self, asset: str) -> dict[str, Any]:
        px = "80000" if asset == "BTC" else "2000"
        return {"levels": [[{"px": px, "sz": "5"}], [{"px": px, "sz": "5"}]]}

    def set_leverage(self, asset: str, leverage: int, *, is_cross: bool = True) -> dict[str, Any]:
        self.leverages_set.append((asset, leverage))
        return {"status": "ok"}

    def create_ioc(self, **kwargs: Any) -> dict[str, Any]:
        self.orders.append(kwargs)
        qty = kwargs["quantity"]
        self.szi = self.szi + qty if kwargs["is_buy"] else self.szi - qty
        return {"status": "ok", "response": {"type": "order", "data": {"statuses": [{"filled": {"totalSz": str(qty), "oid": 8801}}]}}}

    def fills(self, *, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return [{"coin": "BTC", "side": "A", "sz": "0.001", "px": "50000", "fee": "0.02", "oid": 8801, "crossed": True}]


def test_c4_h52_m8_micro_execution_preflight_gate_modes_order_id_filter_and_bounded_unwind():
    cfg = micro.build_contract(ROOT / "config/cross_usdc_micro_execution.yml", run_id="wave1-micro-run")

    # 1. Preflight accepts HL "disabled" & "portfolioMargin", hoists state() to 1 call, and uses _safe_error_code
    bn = _MockBinance()
    by = _MockBybit()
    hl = _MockHL(mode="disabled")
    runner = micro.MicroExecutionRunner(cfg, binance=bn, bybit=by, hyperliquid=hl)  # type: ignore[arg-type]
    pf = runner.preflight()
    assert pf["status"] == "passed"
    assert hl.state_calls == 1  # hoisted outside per-asset loop!

    hl_pm = _MockHL(mode="portfolioMargin")
    runner_pm = micro.MicroExecutionRunner(cfg, binance=bn, bybit=by, hyperliquid=hl_pm)  # type: ignore[arg-type]
    assert runner_pm.preflight()["status"] == "passed"

    # Safe error code preserved in outer preflight except
    bad_bn = SimpleNamespace(account=lambda: (_ for _ in ()).throw(micro.MicroExecutionError("binance_key_ip_or_permission_rejected")))
    runner_err = micro.MicroExecutionRunner(cfg, binance=bad_bn, bybit=by, hyperliquid=hl)  # type: ignore[arg-type]
    pf_err = runner_err.preflight()
    assert pf_err["status"] == "blocked"
    assert pf_err["safe_error"] == "binance_key_ip_or_permission_rejected"
    # Subsequent execute_asset MUST reject immediately because preflight blocked!
    with pytest.raises(micro.PreflightBlocked, match="preflight_blocked"):
        runner_err.execute_asset("BTC", confirmation="wave1-micro-run")

    # 2. Calling execute_asset WITHOUT prior preflight auto-runs preflight & blocks if balance is insufficient!
    runner_low_bal = micro.MicroExecutionRunner(
        cfg, binance=_MockBinance(free_usdc="1"), bybit=_MockBybit(), hyperliquid=_MockHL(mode="disabled")  # type: ignore[arg-type]
    )
    with pytest.raises(micro.PreflightBlocked, match="preflight_blocked"):
        runner_low_bal.execute_asset("BTC", confirmation="wave1-micro-run")

    # 3. _wait_fills filters out stale orderId in 5s skew window and waits until >=99.9% target qty
    seen_starts: list[int] = []

    class _TwoPartBybit(_MockBybit):
        def __init__(self) -> None:
            super().__init__()
            self.poll_n = 0

        def fills(self, *, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
            seen_starts.append(start_ms)
            self.poll_n += 1
            rows = [
                {"symbol": symbol, "side": "Sell", "execQty": "0.500", "execPrice": "50000", "execFee": "0.1", "orderId": "other-order"},
                {"symbol": symbol, "side": "Sell", "execQty": "0.0005", "execPrice": "50000", "execFee": "0.01", "orderId": "bybit-101"},
            ]
            if self.poll_n >= 2:
                rows.append({"symbol": symbol, "side": "Sell", "execQty": "0.0005", "execPrice": "50000", "execFee": "0.01", "orderId": "bybit-101"})
            return rows

    by_parts = _TwoPartBybit()
    runner_wf = micro.MicroExecutionRunner(cfg, binance=_MockBinance(), bybit=by_parts, hyperliquid=_MockHL())  # type: ignore[arg-type]
    got_fills = runner_wf._wait_fills(
        "BTC",
        start_ms=10_000,
        end_ms=10_100,
        expected_venues=("bybit",),
        expected_quantities={"bybit": Decimal("0.001")},
        sides={"bybit": "sell"},
        order_ids={"bybit": "bybit-101"},
    )
    assert seen_starts[0] == 5_000  # 10,000 - 5,000 clock-skew margin
    assert by_parts.poll_n == 2  # did not early-return on 50% partial fill!
    agg_q, _ = micro._aggregate_fills(got_fills["bybit"], venue="bybit", side="sell", symbol="BTCPERP")
    assert agg_q == Decimal("0.001")

    # 4. _safe_unwind unwinds ONLY this run's filled qty (leaving pre-existing holdings alone),
    #    uses >=50 bps emergency limit, catches single-leg errors, and still unwinds the remaining legs!
    bn_shared = _MockBinance(free_btc="0.0500")  # pre-existing 0.05 BTC spot balance, 0 bought by this run!
    by_shared = _MockBybit(pos_size="0.010")  # 0.010 total short, 0.001 from this run; simulate Bybit unwind error
    by_shared.fail_unwind = True
    hl_shared = _MockHL(szi="-0.0050")  # -0.0050 total short, 0.0010 from this run
    runner_uw = micro.MicroExecutionRunner(cfg, binance=bn_shared, bybit=by_shared, hyperliquid=hl_shared)  # type: ignore[arg-type]
    books, _ = runner_uw._market_snapshot("BTC")
    unwound = runner_uw._safe_unwind(
        "BTC",
        filled={"binance": Decimal("0"), "bybit": Decimal("0.001"), "hyperliquid": Decimal("0.0010")},
        books=books,
        client_tag="uwtest",
    )
    assert unwound is False  # Bybit leg failed -> verified non-flat -> returns False cleanly
    assert bn_shared.orders == []  # pre-existing 0.05 BTC on Binance untouched because filled["binance"] == 0!
    assert len(hl_shared.orders) == 1  # HL leg STILL unwound despite Bybit raising!
    assert hl_shared.orders[0]["quantity"] == Decimal("0.0010")  # unwound ONLY 0.0010, leaving pre-existing -0.0040!
    # Emergency unwind price allows 50 bps (> 5 bps entry limit): 80,000 * 1.005 = 80,400
    assert hl_shared.orders[0]["price"] >= Decimal("80300")
