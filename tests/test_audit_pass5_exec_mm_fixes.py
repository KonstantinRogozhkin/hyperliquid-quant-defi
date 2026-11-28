"""Pass 5 Group A regression suite: MM order-stacking/transport guards, post-withdrawal HWM,
oracle kill-switch, rotation asymmetry, attribution smoothness, Cross-USDC gates, MM recency/trigger
hygiene, OKX 1-token safety, and compounded execution stress drawdown.
"""

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

from hl_quant import alpha_families as af
from hl_quant import cex_perp_public as cex
from hl_quant import cross_usdc_micro_execution as micro
from hl_quant import execution_stress as es
from hl_quant import hl_mm_avellaneda_stoikov as mm
from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_testnet_executor as ex
from hl_quant import hl_testnet_risk as risk

ROOT = Path(__file__).resolve().parents[1]
NOW = 1_797_000_000_000


def _meta(*coins: str, sz_decimals: int = 2) -> dict[str, ex.AssetSpec]:
    return {c: ex.AssetSpec(c, sz_decimals=sz_decimals, max_leverage=10) for c in coins}


# --------------------------------------------------------------------------- Bug 1: Live MM Order-Stacking & 429/Timeout Guards


def test_bug1_mm_modify_fallback_never_stacks_on_failed_cancel_and_guards_transport_429():
    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(requote_threshold_bps=0.5)
    bk1 = mm.L2BookSnapshot("ETH", NOW, 2505.0, 2505.5, 5.0, 5.0, 0.0, 2505.25, 2505.25, 2.0)
    bk2 = mm.L2BookSnapshot("ETH", NOW + 500, 2510.0, 2510.5, 5.0, 5.0, 0.0, 2510.25, 2510.25, 2.0)
    dec1 = mm.compute_as_quotes(bk1, spec, inventory_sz=0.0, sigma_tau_bps=8.0, params=params)
    dec2 = mm.compute_as_quotes(bk2, spec, inventory_sz=0.0, sigma_tau_bps=8.0, params=params)

    # 1. Modify fails (or times out) AND fallback cancel hits 429/timeout -> prev_oid MUST remain in active_orders
    # so the next tick does NOT think side_k is empty and stack a duplicate live order!
    class _RateLimitedModifyCancelAdapter:
        def __init__(self) -> None:
            self.modify_calls: list[list[tuple[int, ex.PlannedOrder]]] = []
            self.cancel_calls: list[list[dict[str, Any]]] = []
            self.placed_calls: list[ex.PlannedOrder] = []

        def modify_bulk_alo_orders(self, modifications: list[tuple[int, ex.PlannedOrder]]) -> list[dict[str, Any]]:
            self.modify_calls.append(modifications)
            raise httpx.ReadTimeout("modify timed out")

        def cancel_all_open_orders(self, open_orders: list[dict[str, Any]], *, fast: bool = False) -> list[dict[str, Any]]:
            self.cancel_calls.append(open_orders)
            return [{"mode": "testnet_live", "response": {"status": "err", "response": "429 Too Many Requests"}}]

        def place_bulk_alo_orders(self, orders: list[ex.PlannedOrder]) -> list[dict[str, Any]]:
            self.placed_calls.extend(orders)
            raise httpx.ReadTimeout("bulk place timed out")

    rl_ad = _RateLimitedModifyCancelAdapter()
    active: dict[str, dict[str, Any]] = {
        "buy": {"oid": 701, "px": 2500.0, "sz": 0.012, "reduce_only": False},
        "sell": {"oid": 702, "px": 2501.0, "sz": 0.012, "reduce_only": False},
    }
    mm_oids: set[int] = {701, 702}

    pl1, md1, cn1, _ = asyncio.run(
        mm._sync_coin_quotes_atomic("ETH", dec1, active, mm_oids, rl_ad, mid_px=bk1.mid_px, params=params, recv_ms=NOW, audit_log_path=None)
    )
    assert (pl1, md1, cn1) == (0, 0, 0)
    assert active["buy"]["oid"] == 701 and active["sell"]["oid"] == 702
    assert rl_ad.placed_calls == []

    # Tick 2: because 701 & 702 were kept in active_orders, tick 2 retries modifying 701/702 instead of placing duplicates!
    pl2, md2, cn2, _ = asyncio.run(
        mm._sync_coin_quotes_atomic("ETH", dec2, active, mm_oids, rl_ad, mid_px=bk2.mid_px, params=params, recv_ms=NOW + 500, audit_log_path=None)
    )
    assert (pl2, md2, cn2) == (0, 0, 0)
    assert [oid for oid, _ in rl_ad.modify_calls[-1]] == [701, 702]
    assert rl_ad.placed_calls == []

    # Tick 3 from empty active_orders: place_bulk_alo_orders timeout is caught cleanly without crashing _handle_message
    empty_active: dict[str, dict[str, Any]] = {}
    pl3, _, _, _ = asyncio.run(
        mm._sync_coin_quotes_atomic("ETH", dec1, empty_active, set(), rl_ad, mid_px=bk1.mid_px, params=params, recv_ms=NOW + 1000, audit_log_path=None)
    )
    assert pl3 == 0 and empty_active == {}

    # 2. HTTP 429 JSON error dict on userFills + place_alo_order exception in run_live_mm_session & WS session
    def err429_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        if t == "l2Book":
            return httpx.Response(
                200,
                json={"coin": "ETH", "time": NOW, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]},
            )
        if t == "userFills":
            # Dict instead of list -> previously crashed `for fl in uf: fl.get(...)` with AttributeError on str key!
            return httpx.Response(429, json={"status": "err", "response": "rate limited"})
        if t == "clearinghouseState":
            return httpx.Response(429, json={"status": "err", "response": "rate limited"})
        return httpx.Response(200, json=[])

    class _FlakyRestAdapter(ex.DryRunAdapter):
        def __init__(self) -> None:
            super().__init__()
            self.calls = 0

        def place_alo_order(self, order: ex.PlannedOrder) -> dict[str, Any]:
            self.calls += 1
            if self.calls == 1:
                raise httpx.ReadTimeout("transient place timeout")
            rec = super().place_alo_order(order)
            rec.pop("mode", None)
            return rec

    with httpx.Client(transport=httpx.MockTransport(err429_handler)) as client:
        s_rest, _, _ = mm.run_live_mm_session(
            "ETH",
            _FlakyRestAdapter(),
            account_address="0xMM",
            execute_live=True,
            duration_sec=0.3,
            poll_interval_sec=0.2,
            client=client,
        )
        assert s_rest.ticks_observed >= 1

        ws_msgs = [
            {"channel": "l2Book", "data": {"coin": "ETH", "time": NOW, "levels": [[{"px": "2500.0", "sz": "5.0"}], [{"px": "2500.5", "sz": "5.0"}]]}}
        ]
        s_ws, _, _ = asyncio.run(
            mm.run_live_mm_ws_session("ETH", _FlakyRestAdapter(), account_address="0xMM", execute_live=True, client=client, ws_messages=ws_msgs)
        )
        assert s_ws.ticks_observed == 1


# --------------------------------------------------------------------------- Bug 2: Post-Withdrawal Loss Masking & Immediate Probe Persistence


def test_bug2_post_withdrawal_loss_not_masked_and_probe_persists_flow_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    # Scenario A: Withdraw $10k during 3.0% drawdown (H0=20,000, E_pre=19,400 -> W=10,000 -> E_now=9,400)
    adj_a = ex.flow_adjusted_high_water_mark(
        20_000.0,
        prev_net_flow_usd=20_000.0,
        cur_net_flow_usd=10_000.0,
        current_equity_usd=9_400.0,
        pre_flow_equity_usd=19_400.0,
    )
    assert adj_a == pytest.approx(20_000.0 * (9_400.0 / 19_400.0))
    assert ex.drawdown_pct(9_400.0, adj_a) == pytest.approx(3.0)

    # Scenario B: At peak $10,000 (E_pre=10,000), withdraw $9,000 to $1,000, THEN lose -$450 (-45%) to $550!
    # Old formula gave 10000 * 550 / 9550 = $575.92 -> 4.5% (MASKED!). Exact pre_flow_equity_usd gives $1,000 -> 45.0% DD!
    adj_b = ex.flow_adjusted_high_water_mark(
        10_000.0,
        prev_net_flow_usd=10_000.0,
        cur_net_flow_usd=1_000.0,
        current_equity_usd=550.0,
        pre_flow_equity_usd=10_000.0,
    )
    assert adj_b == pytest.approx(1_000.0)
    assert ex.drawdown_pct(550.0, adj_b) == pytest.approx(45.0)

    acct_loss = ex.AccountSnapshot(
        equity_usd=550.0,
        free_margin_usd=450.0,
        positions_sz={"BTC": 0.005},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"BTC": 300.0},
    )
    res = ex.execute_rebalance_cycle(
        acct_loss,
        {"BTC": 0.10},
        _meta("BTC", sz_decimals=5),
        {"BTC": (60_000.0, 60_002.0, NOW)},
        ex.DryRunAdapter(),
        high_water_mark_usd=adj_b,
        now_ms=NOW,
    )
    assert res["status"] == "kill_switch_triggered"
    assert res["derisk_mode"] == "flatten"
    assert any(v.startswith("drawdown_limit:45.00%") for v in res["violations"])

    # Probe CLI: immediately persists updated HWM + cur_net_flow + last_equity_usd within <=15m of withdrawal
    sys.path.insert(0, str(ROOT / "scripts" / "analytics"))
    probe_cli = importlib.import_module("run_hl_testnet_risk_probe")
    hwm_file = tmp_path / "hl_testnet_hwm.json"
    out_file = tmp_path / "hl_testnet_risk" / "latest.json"
    hwm_file.write_text(
        json.dumps(
            {
                "high_water_mark_usd": 10_000.0,
                "net_external_flow_usd": 10_000.0,
                "last_equity_usd": 10_000.0,
                "reanchored_streak_ms": 123456,
            }
        ),
        encoding="utf-8",
    )

    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "1000.0", "totalMarginUsed": "0.0"}, "withdrawable": "1000.0", "assetPositions": []})
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if t == "userNonFundingLedgerUpdates":
            return httpx.Response(
                200,
                json=[
                    {"time": 1, "hash": "0x1", "delta": {"type": "deposit", "usdc": "10000.0"}},
                    {"time": 2, "hash": "0x2", "delta": {"type": "withdraw", "usdc": "9000.0", "fee": "0.0"}},
                ],
            )
        return httpx.Response(200, json=[])

    real_client = httpx.Client
    monkeypatch.setattr(probe_cli.httpx, "Client", lambda **_kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0xFLOW")
    inv = CliRunner().invoke(probe_cli.app, ["--hwm-state", str(hwm_file), "--output-json", str(out_file)])
    assert inv.exit_code == 0, inv.output
    persisted = json.loads(hwm_file.read_text(encoding="utf-8"))
    assert persisted["high_water_mark_usd"] == pytest.approx(1_000.0)
    assert persisted["net_external_flow_usd"] == pytest.approx(1_000.0)
    assert persisted["last_equity_usd"] == pytest.approx(1_000.0)
    assert persisted["reanchored_streak_ms"] == 123456


# --------------------------------------------------------------------------- Bug 3: Mark Spike Kill-Switch, Standard Leverage & Co-Occurring Flatten Re-Anchor


def test_bug3_oracle_equity_blocks_mark_spike_hide_std_leverage_and_cooccurring_reanchor(tmp_path: Path):
    # 3A: Mark spike inflates account.equity_usd to $1,200 while oracle_equity_usd is $920 (8% drawdown vs $1,000 HWM!)
    acct_spike = ex.AccountSnapshot(
        equity_usd=1_200.0,
        free_margin_usd=1_000.0,
        positions_sz={"XMR": 1.0, "BTC": -0.005},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"XMR": 680.0, "BTC": -300.0},
        position_entry_px={"XMR": 400.0, "BTC": 60_000.0},
        position_unrealized_pnl_usd={"XMR": 280.0, "BTC": 0.0},
    )
    res_spike = ex.execute_rebalance_cycle(
        acct_spike,
        {"BTC": -0.05},
        _meta("XMR", "BTC", sz_decimals=4),
        {"XMR": (679.0, 681.0, NOW), "BTC": (60_000.0, 60_002.0, NOW)},
        ex.DryRunAdapter(),
        high_water_mark_usd=1_000.0,
        oracle_equity_usd=920.0,
        now_ms=NOW,
    )
    assert res_spike["status"] == "kill_switch_triggered"
    assert res_spike["derisk_mode"] == "flatten"
    assert res_spike["drawdown_pct"] == pytest.approx(8.0)
    assert any(v.startswith("drawdown_limit:8.00%") for v in res_spike["violations"])

    # 3A (Risk Snapshot Leverage): Standard Account ($200 Perps + $800 idle Spot USDC) reports 300/200 = 1.50x
    acct_std = ex.AccountSnapshot(
        equity_usd=1_000.0,
        perps_equity_usd=200.0,
        free_margin_usd=100.0,
        positions_sz={"ETH": 0.15},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"ETH": 300.0},
        is_unified=False,
    )
    snap_std = risk.build_risk_snapshot(acct_std, high_water_mark_usd=1_000.0, asset_ctxs={}, book_sides={"ETH": "two_sided"})
    assert snap_std["gross_leverage"] == pytest.approx(1.5)

    # 3B: Co-occurring low_free_margin / gross_leverage on initial crash tick does NOT block post-flatten re-anchoring
    hwm_path = tmp_path / "hwm.json"
    audit_path = tmp_path / "audit.jsonl"
    ex.save_high_water_mark(hwm_path, 1_000.0, net_external_flow_usd=1_000.0, last_equity_usd=1_000.0)
    t_crash = NOW - int(4.5 * 3_600_000)
    records = [
        {
            "ts_ms": t_crash,
            "mode": "testnet_live",
            "status": "kill_switch_triggered",
            "derisk_mode": "flatten",
            "violations": ["drawdown_limit:12.00%>=5.00%", "low_free_margin:8.0%<20.0%", "gross_leverage:1.80x>1.50x"],
        },
        {
            "ts_ms": NOW - 1_800_000,
            "mode": "testnet_live",
            "status": "kill_switch_triggered",
            "derisk_mode": "flatten",
            "violations": ["drawdown_limit:12.00%>=5.00%"],
        },
    ]
    audit_path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    new_hwm, did_reanchor = ex.reanchor_hwm_after_flatten(
        hwm_path,
        audit_path,
        stored_hwm=1_000.0,
        oracle_equity_usd=880.0,
        gross_notional_usd=0.0,
        now_ms=NOW,
        net_external_flow_usd=1_000.0,
    )
    assert did_reanchor is True and new_hwm == pytest.approx(880.0)


# --------------------------------------------------------------------------- Bug 4: Asymmetry Guard (cancel_failed + Portfolio Rotation Delta)


def test_bug4_asymmetry_guard_includes_cancel_failed_and_portfolio_rotation_delta():
    # 1. cancel_failed on SOL during 25%-per-side rebalance prevents opening 25% unhedged ETH short!
    class _SolCancelFailAdapter(ex.DryRunAdapter):
        def cancel_all_open_orders(self, open_orders, *, fast: bool = False):
            reqs = [{"coin": o["coin"], "oid": o["oid"]} for o in open_orders]
            return [
                {
                    "mode": "testnet_live",
                    "requests": reqs,
                    "response": {"status": "ok", "response": {"type": "cancel", "data": {"statuses": [{"error": "429"}]}}},
                }
            ]

    acct_cf = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=10_000.0,
        positions_sz={},
        open_orders=[{"coin": "SOL", "oid": 7}],
        observed_at_ms=NOW,
    )
    ad_cf = _SolCancelFailAdapter()
    res_cf = ex.execute_rebalance_cycle(
        acct_cf,
        {"SOL": 0.25, "ETH": -0.25},
        _meta("SOL", "ETH", sz_decimals=3),
        {"SOL": (100.0, 100.1, NOW), "ETH": (2_000.0, 2_001.0, NOW)},
        ad_cf,
        high_water_mark_usd=10_000.0,
        now_ms=NOW,
    )
    assert res_cf["cancel_failed_coins"] == ["SOL"]
    assert ad_cf.placed == []

    # 2. Portfolio rotation: currently Long +$2,000 SOL + Short -$2,000 ETH ($0 net on $10,000 equity).
    # Rotating SOL -> 0 (reduce_only=True) and BTC -> +$2,000 (reduce_only=False) where BTC is oracle-blocked!
    # SOL close MUST be deferred so Short -$2,000 ETH is NOT left 100% unhedged!
    acct_rot = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=8_000.0,
        positions_sz={"SOL": 20.0, "ETH": -1.0},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 2_000.0, "ETH": -2_000.0},
    )
    bbo_rot = {
        "SOL": (99.95, 100.05, NOW),
        "ETH": (1_999.5, 2_000.5, NOW),
        "BTC": (75_000.0, 75_010.0, NOW),  # +25% above $60,000 oracle -> oracle_blocked!
    }
    refs_rot = {"SOL": 100.0, "ETH": 2_000.0, "BTC": 60_000.0}
    ad_rot = ex.DryRunAdapter()
    res_rot = ex.execute_rebalance_cycle(
        acct_rot,
        {"BTC": 0.20, "ETH": -0.20, "SOL": 0.0},
        _meta("SOL", "ETH", "BTC", sz_decimals=4),
        bbo_rot,
        ad_rot,
        high_water_mark_usd=10_000.0,
        now_ms=NOW,
        reference_px=refs_rot,
    )
    assert res_rot["oracle_band_blocked_coins"] == ["BTC"]
    assert ad_rot.placed == []  # SOL close deferred; net delta stays $0!

    # Partial replacement exceeding 10% net_tol: rotating SOL +$2,500 -> AVAX +$1,000 (ok) + BTC +$1,500 (oracle_blocked)
    # leaves raw |post_net_usd| = $1,500 > $1,000 (0.10 * perp_eq) -> trims SOL close to ~$1,000 so net delta stays $0!
    acct_rot25 = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=8_000.0,
        positions_sz={"SOL": 25.0, "ETH": -1.25},
        open_orders=[],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 2_500.0, "ETH": -2_500.0},
    )
    bbo_half = {**bbo_rot, "AVAX": (24.99, 25.01, NOW)}
    refs_half = {**refs_rot, "AVAX": 25.0}
    ad_half = ex.DryRunAdapter()
    ex.execute_rebalance_cycle(
        acct_rot25,
        {"AVAX": 0.10, "BTC": 0.15, "ETH": -0.25, "SOL": 0.0},
        _meta("SOL", "ETH", "BTC", "AVAX", sz_decimals=4),
        bbo_half,
        ad_half,
        high_water_mark_usd=10_000.0,
        now_ms=NOW,
        reference_px=refs_half,
    )
    avax_buy_usd = sum(o["notional_usd"] for o in ad_half.placed if o["coin"] == "AVAX" and o["side"] == "buy")
    sol_sell_usd = sum(o["notional_usd"] for o in ad_half.placed if o["coin"] == "SOL" and o["side"] == "sell")
    assert avax_buy_usd == pytest.approx(1_000.0, rel=0.02)
    assert sol_sell_usd == pytest.approx(1_000.0, rel=0.02)


# --------------------------------------------------------------------------- Bug 5: Attribution Singularity & Vault Round-Trip


def test_bug5_attribution_smooth_partial_withdrawal_return_and_no_vault_roundtrip_inflation(tmp_path: Path):
    # 1. Cycling $1,000 external deposit 4x into/out of a vault must NOT inflate gross_dep to $5,000!
    vault_ledger = [{"time": 1, "delta": {"type": "deposit", "usdc": "1000.0"}}]
    for k in range(4):
        vault_ledger.append({"time": 2 + 2 * k, "delta": {"type": "vaultDeposit", "usdc": "1000.0"}})
        vault_ledger.append({"time": 3 + 2 * k, "delta": {"type": "vaultWithdraw", "netWithdrawnUsd": "1000.0"}})
    assert pta.compute_gross_deposited_capital_usd(vault_ledger, "0xV") == pytest.approx(1_000.0)
    assert pta.net_external_usdc_flow_usd(vault_ledger, "0xV") == pytest.approx(1_000.0)

    # 2. Withdraw $999 of a $1,000 deposit after +$500 profit -> net_flow=$1, equity=$501 -> +50.0% (NOT +50,000%!)
    paper_dir = tmp_path / "paper"
    paper_dir.mkdir()
    pd.DataFrame({"ts_ms": [1, 2], "equity_usd": [100_000.0, 150_000.0]}).to_parquet(paper_dir / "ticks.parquet")
    (paper_dir / "state.json").write_text(json.dumps({"starting_capital_usd": 100_000.0}), encoding="utf-8")

    snap_999 = {
        "account_address": "0xSMOOTH",
        "observed_at_ms": NOW,
        "ledger_updates": [
            *vault_ledger,
            {"time": 20, "delta": {"type": "withdraw", "usdc": "999.0", "fee": "0.0"}},
        ],
        "fills": [{"coin": "BTC", "px": "60000", "sz": "0.01", "side": "B", "fee": "0.0", "closedPnl": "500.0"}],
        "funding": [],
        "clearinghouse": {"marginSummary": {"accountValue": "501.0"}, "withdrawable": "501.0", "assetPositions": []},
        "spot": {"balances": []},
        "open_orders": [],
        "all_mids": {"BTC": 60_000.0},
    }
    attr = pta.compute_post_trade_attribution(snap_999, paper_dir=paper_dir)
    assert attr.deposited_capital_usd == pytest.approx(1_000.0)
    assert attr.account_return_pct == pytest.approx(50.0)
    assert attr.paper_comparison is not None
    assert attr.paper_comparison["testnet_return_pct"] == pytest.approx(50.0)
    assert attr.paper_comparison["tracking_diff_pct"] == pytest.approx(0.0)


# --------------------------------------------------------------------------- Bug 6: Cross-USDC Protected Limit, Pre-Perp Check, BNB & Spread Tolerance


def test_bug6_cross_usdc_protected_limit_budget_pre_perp_usdc_gate_bnb_and_spread_tolerance():
    cfg = micro.build_contract(ROOT / "config/cross_usdc_micro_execution.yml", run_id="pass5-micro")

    # 1. BNB fee coverage & 0-fee promo skip base-asset gross-up
    assert (
        micro.plan_spot_buy_quantity_for_base_fee(
            Decimal("0.0500"), step=Decimal("0.0001"), fee_rate=Decimal("0.001"), can_pay_fee_in_bnb=True
        )
        == Decimal("0.0500")
    )
    acct_with_bnb = {"takerCommission": 10, "balances": [{"asset": "BNB", "free": "0.25", "locked": "0"}]}
    assert micro._binance_effective_base_fee_rate(acct_with_bnb) == Decimal("0")
    assert micro._binance_effective_base_fee_rate({"takerCommission": 0, "balances": []}) == Decimal("0")

    # 2. +0.5 bps normal spot-perp ask-bid spread ($2000.10 spot ask vs $2000.00 perps) passes plan & preflight!
    plan_spread = micro.plan_entry_quantities(
        cfg,
        binance_price=Decimal("2000.10"),
        bybit_price=Decimal("2000.00"),
        hyperliquid_price=Decimal("2000.00"),
        binance_step=Decimal("0.0001"),
        bybit_step=Decimal("0.01"),
        hyperliquid_step=Decimal("0.001"),
        binance_base_fee_rate=Decimal("0"),
    )
    assert plan_spread["binance"] == Decimal("0.0500")
    assert plan_spread["delta_fraction"] == Decimal("0")

    # 3. Preflight rejects when free USDC ($100.02) covers ask ($100.005) but NOT +5 bps protected limit ($100.055),
    # and execute_asset blocks BEFORE placing any Bybit or Hyperliquid perp short IOC!
    perp_orders_sent: list[str] = []

    class _BNTight:
        def __init__(self, usdc_free: str) -> None:
            self.usdc_free = usdc_free

        def account(self):
            return {"canTrade": True, "takerCommission": 0, "balances": [{"asset": "USDC", "free": self.usdc_free, "locked": "0"}]}

        def instrument(self, _s):
            return {"filters": [{"filterType": "LOT_SIZE", "stepSize": "0.0001", "minQty": "0.0001"}, {"filterType": "MIN_NOTIONAL", "minNotional": "5"}, {"filterType": "PRICE_FILTER", "tickSize": "0.01"}]}

        def book(self, _s):
            return {"bids": [["2000.00", "5"]], "asks": [["2000.10", "5"]]}

    class _BYMock:
        def query_api(self):
            return {"result": {"permissions": ["ContractTrade"]}}

        def wallet(self):
            return {"result": {"list": [{"totalAvailableBalance": "500"}]}}

        def account_info(self):
            return {"result": {"marginMode": "REGULAR_MARGIN"}}

        def instrument(self, _s):
            return {"lotSizeFilter": {"qtyStep": "0.01", "minOrderQty": "0.01", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.01"}, "leverageFilter": {"maxLeverage": "50"}}

        def positions(self, _s):
            return []

        def fee_rate(self, _s):
            return {"result": {"list": [{"takerFeeRate": "0.00055"}]}}

        def book(self, _s):
            return {"b": [["2000.00", "5"]], "a": [["2000.10", "5"]]}

        def set_leverage(self, *_a):
            perp_orders_sent.append("bybit_lev")
            return {"retCode": 0}

        def create_ioc(self, **_kw):
            perp_orders_sent.append("bybit_ioc")
            return {"result": {"orderId": "1"}}

    class _HLMock:
        api_url = micro.HYPERLIQUID
        api_wallet_address = "0xagent"

        def state(self):
            return {"withdrawable": "500", "assetPositions": []}

        def spot_state(self):
            return {"balances": []}

        def account_mode(self):
            return "standard"

        def extra_agents(self):
            return [{"address": "0xagent", "validUntil": None}]

        def user_fees(self):
            return {"userAddRate": "0.00015"}

        def meta(self):
            return {"universe": [{"name": "ETH", "szDecimals": 3, "maxLeverage": 25}, {"name": "BTC", "szDecimals": 5, "maxLeverage": 40}]}

        def mark_prices(self):
            return {"ETH": "2000.0", "BTC": "60000.0"}

        def book(self, _a):
            return {"levels": [[{"px": "2000.00", "sz": "5"}], [{"px": "2000.10", "sz": "5"}]]}

        def set_leverage(self, *_a, **_kw):
            perp_orders_sent.append("hl_lev")
            return {"status": "ok"}

        def create_ioc(self, **_kw):
            perp_orders_sent.append("hl_ioc")
            return {}

    # $100.02 < 0.0500 * 2001.10 ($100.055) -> preflight fails binance_spot_balance_passed!
    runner_tight = micro.MicroExecutionRunner(cfg, binance=_BNTight("100.02"), bybit=_BYMock(), hyperliquid=_HLMock())  # type: ignore[arg-type]
    pf_tight = runner_tight.preflight(assets=("ETH",))
    assert pf_tight["status"] == "blocked"
    assert pf_tight["assets"]["ETH"]["binance_spot_balance_passed"] is False

    # With $100.10 during preflight (passes +5 bps), then balance drops to $100.02 right before execute_asset ->
    # execute_asset aborts with PreflightBlocked("insufficient_binance_spot_usdc") BEFORE any perp IOC!
    bn_drop = _BNTight("100.10")
    runner_ok = micro.MicroExecutionRunner(cfg, binance=bn_drop, bybit=_BYMock(), hyperliquid=_HLMock())  # type: ignore[arg-type]
    pf_ok = runner_ok.preflight(assets=("ETH",))
    assert pf_ok["status"] == "passed"
    assert pf_ok["assets"]["ETH"]["actual_gross_within_contract"] is True

    bn_drop.usdc_free = "100.02"
    with pytest.raises(micro.PreflightBlocked, match="insufficient_binance_spot_usdc"):
        runner_ok.execute_asset("ETH", confirmation="pass5-micro")
    assert perp_orders_sent == []


# --------------------------------------------------------------------------- Bug 7: mm_coin_set Recency, Empty frontendOpenOrders & isTrigger Filter


def test_bug7_mm_coin_set_recency_cutoff_empty_frontend_open_orders_and_is_trigger_skip(tmp_path: Path):
    # 1. load_logged_order_coins enforces recency cutoff so weeks-old MM tests don't mask orphans forever
    mm_log = tmp_path / "mm_audit.jsonl"
    mm_log.write_text(
        "\n".join(
            [
                json.dumps({"ts_ms": NOW - 10 * 86_400_000, "mode": "ws_bulk", "coin": "STALE_COIN", "oid": 1}),
                json.dumps({"ts_ms": NOW - 30 * 60_000, "mode": "ws_bulk", "coin": "FRESH_COIN", "oid": 2}),
                json.dumps({"ts_ms": NOW - 10 * 60_000, "mode": "dry_run", "coin": "DRY_COIN", "oid": 3}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    assert ex.load_logged_order_coins(mm_log, now_ms=NOW) == {"FRESH_COIN"}
    assert ex.load_logged_order_coins(mm_log, min_ts_ms=NOW - 6 * 3_600_000) == {"FRESH_COIN"}

    # 2. fetch_testnet_account accepts empty list [] from frontendOpenOrders without redundant openOrders POST
    seen_calls: list[str] = []

    def acct_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        seen_calls.append(t)
        if t == "clearinghouseState":
            return httpx.Response(200, json={"marginSummary": {"accountValue": "500"}, "withdrawable": "500", "assetPositions": []})
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": []})
        if t == "frontendOpenOrders":
            return httpx.Response(200, json=[])
        if t == "openOrders":
            raise AssertionError("redundant openOrders POST must not fire when frontendOpenOrders returned []")
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(acct_handler)) as client:
        snap_acct = ex.fetch_testnet_account(client, "0xEMPTY_OO")
    assert snap_acct.open_orders == []
    assert "openOrders" not in seen_calls

    # 3. Untriggered trigger orders (isTrigger=True) are ignored in strip_own_orders_from_l2_levels & resting_unwind_coins
    levels = [[{"px": "100.0", "sz": "5.0", "n": 1}], [{"px": "101.0", "sz": "5.0", "n": 1}]]
    trig_order = {"coin": "SOL", "oid": 50, "side": "A", "limitPx": "101.0", "sz": "5.0", "reduceOnly": True, "isTrigger": True}
    stripped = ex.strip_own_orders_from_l2_levels(levels, [trig_order], "SOL")
    assert len(stripped[1]) == 1 and float(stripped[1][0]["sz"]) == pytest.approx(5.0)

    acct_trig = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=900.0,
        positions_sz={"SOL": 5.0},
        open_orders=[trig_order],
        observed_at_ms=NOW,
        position_notional_usd={"SOL": 500.0},
    )
    r_snap = risk.build_risk_snapshot(acct_trig, high_water_mark_usd=1_000.0, asset_ctxs={}, book_sides={"SOL": "bid_only"})
    assert r_snap["orphan_positions"]["SOL"]["unmanaged"] is True
    assert r_snap["unmanaged_orphan_positions"] == 1


# --------------------------------------------------------------------------- Bug 8: OKX 1-Token Safety & Compounded Stressed Drawdown


def test_bug8_okx_1token_coins_never_scaled_1000x_and_compounded_stress_drawdown():
    # 1. OKX_KILO_BASES is immutable frozenset; 1-token HL coins CAT & MOG stay 1.0x while kPEPE scales 1000x!
    assert isinstance(cex.OKX_KILO_BASES, frozenset)
    assert cex.okx_symbol("kCAT") == "CAT-USDT-SWAP"
    assert "CAT" not in cex.OKX_KILO_BASES
    assert cex.okx_price_scale("CAT-USDT-SWAP") == pytest.approx(1.0)
    assert cex.okx_price_scale("MOG-USDT-SWAP") == pytest.approx(1.0)
    assert cex.okx_price_scale("CAT") == pytest.approx(1.0)
    assert cex.okx_price_scale("kPEPE") == pytest.approx(1_000.0)
    assert cex.okx_price_scale("CAT-USDT-SWAP", hl_coin="CAT") == pytest.approx(1.0)
    assert cex.okx_price_scale("CAT-USDT-SWAP", hl_coin="kCAT") == pytest.approx(1_000.0)

    okx_client = SimpleNamespace(
        get=lambda _p, _q: {
            "code": "0",
            "data": [
                {"instId": "CAT-USDT-SWAP", "bidPx": "0.000030", "askPx": "0.000032"},
                {"instId": "PEPE-USDT-SWAP", "bidPx": "0.000010", "askPx": "0.000012"},
            ],
        }
    )
    mids = cex.fetch_live_mid_prices(okx_client, "okx", {"CAT": "CAT-USDT-SWAP", "kPEPE": "PEPE-USDT-SWAP"})
    assert mids["CAT-USDT-SWAP"] == pytest.approx(0.000031)
    assert mids["PEPE-USDT-SWAP"] == pytest.approx(0.011)

    h = cex.HOUR_MS
    t0 = 1_790_000_000_000 - (1_790_000_000_000 % h)
    with cex.PublicGetClient(
        cex.OKX_API,
        transport=httpx.MockTransport(
            lambda _r: httpx.Response(
                200, json={"code": "0", "msg": "", "data": [[str(t0), "0.00003", "0.000032", "0.000029", "0.000031", "10", "100000", "3.1", "1"]]}
            )
        ),
    ) as c_okx:
        kl_cat = cex.collect_klines_1h(c_okx, "okx", "CAT-USDT-SWAP", t0, t0 + h, now_ms=t0 + 2 * h, hl_coin="CAT")
    assert kl_cat.rows["close"].iloc[0] == pytest.approx(0.000031)

    # 2. Compounded stressed drawdown matches af.series_metrics and NEVER exceeds -100% (-1.0)
    idx = pd.RangeIndex(120)
    r_crash = pd.Series(-0.02, index=idx)  # additive cumsum would be -2.40 (-240%!), compounded is ~ -0.91!
    dw = pd.Series(1.0, index=idx)
    lv = pd.Series(0.02, index=idx)
    summaries = es.run_execution_monte_carlo(r_crash, dw, lv, n_paths=50, seed=11)
    base_dd = af.series_metrics(r_crash)["max_drawdown"]
    assert summaries[0].median_max_dd == pytest.approx(base_dd)
    for sc in summaries:
        assert -1.0 <= sc.p05_max_dd <= sc.median_max_dd <= 0.0
