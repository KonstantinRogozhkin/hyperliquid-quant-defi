"""Regression tests for the 2026-11-27 high-severity Testnet execution fixes."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_quant import hl_testnet_executor as ex

NOW = 1_795_800_000_000


def _meta(*coins: str) -> dict[str, ex.AssetSpec]:
    return {c: ex.AssetSpec(c, sz_decimals=2, max_leverage=10) for c in coins}


def _acct(
    *,
    equity: float = 1_000.0,
    free: float = 900.0,
    positions: dict[str, float] | None = None,
    notional: dict[str, float] | None = None,
    open_orders: list[dict] | None = None,
) -> ex.AccountSnapshot:
    return ex.AccountSnapshot(
        equity_usd=equity,
        free_margin_usd=free,
        positions_sz=positions or {},
        open_orders=open_orders or [],
        observed_at_ms=NOW,
        position_notional_usd=notional or {},
    )


# ----------------------------------------------------------------- 1. kill-switch de-risks


def test_drawdown_kill_switch_flattens_positions_with_reduce_only_orders():
    acct = _acct(equity=900.0, positions={"SOL": 1.0, "ETH": -0.02}, notional={"SOL": 100.0, "ETH": -40.0})
    bbo = {"SOL": (100.0, 100.1, NOW), "ETH": (2_000.0, 2_001.0, NOW)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, {"SOL": 0.10, "ETH": -0.04}, _meta("SOL", "ETH"), bbo, adapter, high_water_mark_usd=1_000.0, now_ms=NOW
    )
    assert res["status"] == "kill_switch_triggered"
    assert res["derisk_mode"] == "flatten"
    placed = {(o["coin"], o["side"]) for o in adapter.placed}
    assert placed == {("SOL", "sell"), ("ETH", "buy")}
    assert all(o["reduce_only"] for o in adapter.placed)
    assert res["placed_count"] == 2


def test_low_margin_kill_switch_only_reduces_toward_targets():
    # SOL held 2.0 (=$200) vs target 0.10 ($100): the reduce-only half is allowed; no new BTC entry.
    acct = _acct(free=100.0, positions={"SOL": 2.0}, notional={"SOL": 200.0})
    bbo = {"SOL": (100.0, 100.1, NOW), "BTC": (60_000.0, 60_001.0, NOW)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, {"SOL": 0.10, "BTC": -0.10}, _meta("SOL", "BTC"), bbo, adapter, high_water_mark_usd=1_000.0, now_ms=NOW
    )
    assert res["status"] == "kill_switch_triggered"
    assert any(v.startswith("low_free_margin") for v in res["violations"])
    assert res["derisk_mode"] == "reduce_only"
    assert [(o["coin"], o["side"], o["reduce_only"]) for o in adapter.placed] == [("SOL", "sell", True)]


def test_data_integrity_kill_switch_places_nothing_and_spares_mm_orders():
    acct = _acct(
        positions={"SOL": 1.0},
        notional={"SOL": 100.0},
        open_orders=[{"coin": "SOL", "oid": 1}, {"coin": "BTC", "oid": 2}],
    )
    stale = {"SOL": (100.0, 100.1, NOW - 120_000)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, {"SOL": 0.10}, _meta("SOL", "BTC"), stale, adapter, high_water_mark_usd=1_000.0, now_ms=NOW,
        foreign_order_oids={2},
    )
    assert res["status"] == "kill_switch_triggered"
    assert res["derisk_mode"] == "none"
    assert adapter.placed == []
    assert [c["oid"] for c in adapter.cancelled] == [1]


# ----------------------------------------------------------------- 2. orphan unwind


@pytest.mark.parametrize(
    ("sz", "top", "ref", "side", "expected_px"),
    [
        (30.0, (0.24, None, NOW), 0.25, "sell", 0.25),  # long, bid-only book: sell at oracle above bid
        (30.0, (0.26, None, NOW), 0.25, "sell", 0.2601),  # oracle below bid: one price step (4 dp) above the bid
        (-975.0, (None, 0.0495, NOW), 0.0490, "buy", 0.049),  # short, ask-only: buy at oracle below ask
        (-30.0, (None, None, NOW), 0.9925, "buy", 0.9925),  # empty book: oracle
    ],
)
def test_orphan_unwind_prices_passively(sz, top, ref, side, expected_px):
    acct = _acct(positions={"X": sz})
    orders = ex.plan_orphan_unwind_orders(acct, _meta("X"), {}, {"X": top}, {"X": ref})
    assert len(orders) == 1
    o = orders[0]
    assert (o.side, o.reduce_only, o.tif) == (side, True, "Alo")
    assert o.sz == pytest.approx(abs(sz))
    assert o.limit_px == pytest.approx(expected_px)
    bid, ask, _ = top
    if side == "sell" and bid is not None:
        assert o.limit_px > bid
    if side == "buy" and ask is not None:
        assert o.limit_px < ask


def test_normal_cycle_unwinds_orphans_and_skips_foreign_mm_coins():
    acct = _acct(
        positions={"SOL": 1.0, "JUP": 30.0, "BTC": 0.001},
        notional={"SOL": 100.0, "JUP": 7.4, "BTC": 60.0},
        open_orders=[{"coin": "BTC", "oid": 77}],
    )
    bbo = {"SOL": (100.0, 100.1, NOW), "BTC": (60_000.0, 60_001.0, NOW)}
    tops = {**bbo, "JUP": (0.24, None, NOW)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, {"SOL": 0.10}, _meta("SOL", "JUP", "BTC"), bbo, adapter, high_water_mark_usd=1_000.0, now_ms=NOW,
        book_tops=tops, reference_px={"JUP": 0.25}, foreign_order_oids={77},
    )
    assert res["status"] == "ok"
    assert res["orphan_unwind_count"] == 1
    assert res["foreign_coins"] == ["BTC"]
    coins = {o["coin"] for o in adapter.placed}
    assert "JUP" in coins  # orphan closed
    assert "BTC" not in coins  # MM inventory left alone
    assert adapter.cancelled == []  # MM order 77 preserved


def test_kill_switch_still_cancels_everything_on_account_risk():
    acct = _acct(equity=900.0, open_orders=[{"coin": "BTC", "oid": 77}, {"coin": "SOL", "oid": 1}])
    adapter = ex.DryRunAdapter()
    ex.execute_rebalance_cycle(
        acct, {"SOL": 0.10}, _meta("SOL"), {"SOL": (100.0, 100.1, NOW)}, adapter,
        high_water_mark_usd=1_000.0, now_ms=NOW, foreign_order_oids={77},
    )
    assert {c["oid"] for c in adapter.cancelled} == {1, 77}


# ----------------------------------------------------------------- 3. leverage failures block entries


class _FailingLeverageAdapter(ex.DryRunAdapter):
    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> dict:
        return {"mode": "testnet_live", "coin": coin, "response": {"status": "err", "response": "leverage rejected"}}


def test_failed_cross_leverage_update_blocks_new_exposure():
    adapter = _FailingLeverageAdapter()
    res = ex.execute_rebalance_cycle(
        _acct(), {"SOL": 0.10}, _meta("SOL"), {"SOL": (100.0, 100.1, NOW)}, adapter,
        high_water_mark_usd=1_000.0, now_ms=NOW,
    )
    assert res["status"] == "ok"
    assert res["blocked_leverage_coins"] == ["SOL"]
    assert adapter.placed == []


# ----------------------------------------------------------------- helpers


def test_load_logged_order_oids_reads_mm_and_executor_logs_and_skips_dry_run(tmp_path: Path):
    log = tmp_path / "audit.jsonl"
    rows = [
        {"ts_ms": 1, "coin": "BTC", "status": "resting", "oid": 11, "order": {}},
        {"ts_ms": 2, "orders": [{"mode": "testnet_live", "response": {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": 22}}]}}}}]},
        {"ts_ms": 3, "orders": [{"mode": "dry_run", "oid": 100_001}]},
        {"ts_ms": 4, "mode": "dry_run", "oid": 100_002},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows) + "\nnot-json\n")
    assert ex.load_logged_order_oids(log) == {11, 22}
    assert ex.load_logged_order_oids(tmp_path / "missing.jsonl") == set()


def test_two_sided_bbo_drops_one_sided_books():
    tops = {"A": (1.0, 1.1, NOW), "B": (1.0, None, NOW), "C": (None, None, NOW)}
    assert ex.two_sided_bbo(tops) == {"A": (1.0, 1.1, NOW)}


# ----------------------------------------------------------------- 4/5. MM shared-address safety


def _mm_book(bid: float = 2500.0, ask: float = 2500.5):
    from hl_quant import hl_mm_avellaneda_stoikov as mm

    mid = 0.5 * (bid + ask)
    return mm.L2BookSnapshot(
        coin="ETH", observed_ms=NOW, best_bid=bid, best_ask=ask, bid_sz1=10.0, ask_sz1=10.0,
        imbalance5=0.0, mid_px=mid, microprice=mid, book_spread_bps=(ask - bid) / mid * 1e4,
    )


def test_mm_reduce_only_follows_real_account_position():
    from hl_quant import hl_mm_avellaneda_stoikov as mm

    spec = ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = mm.ASParams(base_order_usd=30.0, max_inventory_usd=100.0)
    bk = _mm_book()
    inv = 110.0 / bk.mid_px  # session long beyond the cap -> bid off, ask unwinds
    # Session-only view (legacy): ask flagged reduce-only.
    assert mm.compute_as_quotes(bk, spec, inventory_sz=inv, sigma_tau_bps=10.0, params=params).ask_reduce_only
    # Account is net short (another engine holds -0.2 ETH): selling would grow the short, so the
    # unwind must NOT be reduce-only or Hyperliquid rejects it and the MM is stuck.
    dec = mm.compute_as_quotes(
        bk, spec, inventory_sz=inv, sigma_tau_bps=10.0, params=params, account_position_sz=inv - 0.2
    )
    assert dec.bid_px is None and dec.ask_px is not None
    assert dec.ask_reduce_only is False
    # Account long enough to cover the unwind: reduce-only stays on.
    dec_ok = mm.compute_as_quotes(bk, spec, inventory_sz=inv, sigma_tau_bps=10.0, params=params, account_position_sz=inv)
    assert dec_ok.ask_reduce_only is True
    assert mm.reduce_only_is_valid("buy", 0.1, -0.1) and not mm.reduce_only_is_valid("buy", 0.1, 0.05)


def test_mm_ws_session_keeps_dead_man_switch_armed_when_cancels_do_not_clear():
    import asyncio

    import httpx

    from hl_quant import hl_mm_avellaneda_stoikov as mm

    adapter = ex.DryRunAdapter()

    def handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "meta":
            return httpx.Response(200, json={"universe": [{"name": "ETH", "szDecimals": 4, "maxLeverage": 25}]})
        if t == "openOrders":
            # Cancels never take effect: every order the MM placed is still resting.
            live = [r["oid"] for r in adapter.placed + adapter.modified if r.get("oid") is not None]
            return httpx.Response(200, json=[{"coin": "ETH", "oid": oid} for oid in live])
        if t == "clearinghouseState":
            return httpx.Response(200, json={"assetPositions": []})
        return httpx.Response(200, json=[])

    t0 = NOW
    ws_msgs = [
        {"channel": "bbo", "data": {"coin": "ETH", "time": t0, "bbo": [{"px": "2500.0", "sz": "5"}, {"px": "2500.5", "sz": "5"}]}},
        {"channel": "bbo", "data": {"coin": "ETH", "time": t0 + 600, "bbo": [{"px": "2501.0", "sz": "5"}, {"px": "2501.5", "sz": "5"}]}},
    ]
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        res = asyncio.run(
            mm.run_multi_coin_mm_ws_session(
                ["ETH"], adapter, account_address="0xabc", execute_live=True,
                enable_lead_lag=False, enable_dead_man_switch=True, client=client, ws_messages=ws_msgs,
            )
        )
    assert adapter.placed, "session should have quoted"
    assert adapter.scheduled_cancels and adapter.scheduled_cancels[0] is not None
    assert None not in adapter.scheduled_cancels  # never disarmed while orders may be resting
    assert res["summaries"]["ETH"].dms_left_armed is True


def test_feed_gap_kill_switch_still_unwinds_untargeted_orphans_only():
    # Live 2026-11-27 shape: >35% of targets unquoted -> missing_quote halt. GRAM (not targeted,
    # empty book) must still be closed; JUP (targeted long, held within target) is kept.
    acct = _acct(
        positions={"GRAM": -30.0, "JUP": 30.0},
        notional={"GRAM": -29.7, "JUP": 7.4},
    )
    targets = {"JUP": 0.01, "SKY": -0.04, "SOL": 0.03}  # JUP 1% of $1000 = $10 > held $7.4
    tops = {"GRAM": (None, None, NOW), "JUP": (0.24, None, NOW), "SKY": (None, 0.05, NOW)}
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct, targets, _meta("GRAM", "JUP", "SKY", "SOL"), {"SOL": (100.0, 100.1, NOW)}, adapter,
        high_water_mark_usd=1_000.0, now_ms=NOW, book_tops=tops, reference_px={"GRAM": 0.99, "JUP": 0.25},
    )
    assert res["status"] == "kill_switch_triggered"
    assert any(v.startswith("missing_quote") for v in res["violations"])
    assert res["derisk_mode"] == "orphan_unwind_only"
    assert [(o["coin"], o["side"], o["sz"], o["reduce_only"]) for o in adapter.placed] == [("GRAM", "buy", 30.0, True)]


def test_orphan_unwind_trims_only_the_excess_over_target():
    acct = _acct(positions={"VVV": 2.0})  # $50 at oracle 25
    tops = {"VVV": (24.0, None, NOW)}
    orders = ex.plan_orphan_unwind_orders(acct, _meta("VVV"), {}, tops, {"VVV": 25.0}, target_weights={"VVV": 0.02})
    assert len(orders) == 1 and orders[0].side == "sell"
    assert orders[0].sz == pytest.approx(1.2)  # keep $20 (0.8 VVV), trim 1.2
    flipped = ex.plan_orphan_unwind_orders(acct, _meta("VVV"), {}, tops, {"VVV": 25.0}, target_weights={"VVV": -0.02})
    assert flipped[0].sz == pytest.approx(2.0)  # opposite-sign intent -> close fully (never flip)
