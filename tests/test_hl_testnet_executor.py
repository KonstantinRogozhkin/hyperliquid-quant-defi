from __future__ import annotations

from pathlib import Path

import httpx

import pytest

from hl_quant import hl_testnet_executor as ex


def test_hl_price_and_size_rounding_post_only_safe():
    # BTC: szDecimals=5 -> max price decimals = 1, 5 sig figs
    assert ex.round_hl_price(60123.78, sz_decimals=5, side="buy") == pytest.approx(60123.0)
    assert ex.round_hl_price(60123.12, sz_decimals=5, side="sell") == pytest.approx(60124.0)
    # Small alt: szDecimals=0 -> max 6 decimals, 5 sig figs
    assert ex.round_hl_price(0.123456, sz_decimals=0, side="buy") == pytest.approx(0.12345)
    assert ex.round_hl_price(0.123451, sz_decimals=0, side="sell") == pytest.approx(0.12346)
    assert ex.round_hl_size(1.23456, sz_decimals=3) == pytest.approx(1.234)


def test_kill_switch_halts_on_drawdown_margin_and_stale_feed():
    now = 1_700_000_100_000
    acct = ex.AccountSnapshot(
        equity_usd=9_400.0,
        free_margin_usd=1_000.0,  # 10.6% < 20%
        positions_sz={},
        open_orders=[{"coin": "BTC", "oid": 1}],
        observed_at_ms=now,
    )
    bbo = {"BTC": (60_000.0, 60_005.0, now - 60_000)}  # 60s stale > 30s
    violations = ex.check_kill_switch(
        acct,
        {"BTC": 0.15},
        bbo,
        ex.RiskLimits(max_drawdown_pct=5.0, min_free_margin_pct=20.0, max_stale_feed_sec=30.0),
        high_water_mark_usd=10_000.0,
        now_ms=now,
    )
    assert any("drawdown_limit" in v for v in violations)
    assert any("low_free_margin" in v for v in violations)
    assert any("stale_feed" in v for v in violations)


def test_rebalance_slices_large_orders_into_alo_children_and_writes_audit(tmp_path: Path):
    now = 1_700_000_000_000
    acct = ex.AccountSnapshot(
        equity_usd=20_000.0,
        free_margin_usd=20_000.0,
        positions_sz={},
        open_orders=[{"coin": "ETH", "oid": 99}],
        observed_at_ms=now,
    )
    meta = {
        "BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40),
        "ETH": ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25),
    }
    bbo = {
        "BTC": (60_000.0, 60_002.0, now),
        "ETH": (2_500.0, 2_501.0, now),
    }
    adapter = ex.DryRunAdapter()
    audit_path = tmp_path / "audit.jsonl"
    res = ex.execute_rebalance_cycle(
        acct,
        {"BTC": 0.15, "ETH": -0.15},  # $3,000 each -> 3 x $1,000 child ALO orders per coin!
        meta,
        bbo,
        adapter,
        limits=ex.RiskLimits(max_child_usd=1_000.0),
        audit_log_path=audit_path,
        now_ms=now,
    )
    assert res["status"] == "ok"
    assert res["cancelled_count"] == 1
    assert res["placed_count"] == 6
    assert all(o["tif"] == "Alo" for o in adapter.placed)
    assert audit_path.is_file()

    # Verify dollar neutralization, HWM persistence, and Telegram Rich HTML formatting
    neut = ex.neutralize_target_weights({"A": -0.30, "B": -0.10, "C": 0.10}, max_gross=0.90)
    assert abs(sum(neut.values())) < 1e-4
    assert sum(abs(v) for v in neut.values()) <= 0.90 + 1e-6

    hwm_file = tmp_path / "hwm.json"
    ex.save_high_water_mark(hwm_file, max(ex.load_high_water_mark(hwm_file), 1000.0))
    ex.save_high_water_mark(hwm_file, max(ex.load_high_water_mark(hwm_file), 950.0))
    assert ex.load_high_water_mark(hwm_file) == 1000.0
    rich = ex.format_testnet_telegram_rich_html(acct, res, bbo, hwm_usd=20_000.0)
    assert '<table bordered striped>' in rich
    assert "Hyperliquid Testnet" in rich


def _acct(equity: float) -> ex.AccountSnapshot:
    return ex.AccountSnapshot(equity_usd=equity, free_margin_usd=equity, positions_sz={}, open_orders=[], observed_at_ms=0)


def test_drawdown_kill_switch_uses_persisted_high_water_mark(tmp_path: Path):
    now = 1_700_000_000_000
    meta = {"BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40)}
    bbo = {"BTC": (60_000.0, 60_002.0, now)}
    hwm_path = tmp_path / "hwm.json"
    assert ex.load_high_water_mark(hwm_path) == 0.0
    ex.save_high_water_mark(hwm_path, 10_000.0)

    res = ex.execute_rebalance_cycle(
        _acct(9_000.0), {"BTC": 0.1}, meta, bbo, ex.DryRunAdapter(),
        high_water_mark_usd=ex.load_high_water_mark(hwm_path), now_ms=now,
    )
    assert res["status"] == "kill_switch_triggered"
    assert any(v.startswith("drawdown_limit") for v in res["violations"])
    assert res["equity_usd"] == pytest.approx(9_000.0)
    assert res["drawdown_pct"] == pytest.approx(10.0)
    assert res["orphan_notional_usd"] == 0.0
    assert ex.drawdown_pct(11_000.0, 10_000.0) == 0.0
    assert ex.drawdown_pct(9_500.0, 0.0) == 0.0

    # Without a stored mark the gate can only compare equity with itself.
    res_none = ex.execute_rebalance_cycle(_acct(9_000.0), {"BTC": 0.1}, meta, bbo, ex.DryRunAdapter(), now_ms=now)
    assert res_none["status"] == "ok"


def test_missing_quote_for_target_coin_halts_entries():
    violations = ex.check_kill_switch(
        _acct(10_000.0), {"BTC": 0.1, "ETH": 0.0}, {}, ex.RiskLimits(), high_water_mark_usd=10_000.0, now_ms=0
    )
    assert violations == ["missing_quote:BTC"]


def test_bbo_age_comes_from_exchange_book_time():
    book_ms = 1_700_000_000_000

    def handler(req: httpx.Request) -> httpx.Response:
        body = {"coin": "BTC", "time": book_ms, "levels": [[{"px": "60000", "sz": "1", "n": 1}], [{"px": "60001", "sz": "1", "n": 1}]]}
        return httpx.Response(200, json=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        bbo = ex.fetch_testnet_bbo(client, ["BTC"])
    assert bbo["BTC"] == (60_000.0, 60_001.0, book_ms)
    stale = ex.check_kill_switch(
        _acct(10_000.0), {"BTC": 0.1}, bbo, ex.RiskLimits(max_stale_feed_sec=30.0), high_water_mark_usd=10_000.0, now_ms=book_ms + 60_000
    )
    assert any(v.startswith("stale_feed:BTC") for v in stale)


def test_executor_cli_refuses_to_trade_without_targets(tmp_path: Path):
    import typer

    from scripts.analytics import run_hl_testnet_executor as cli

    with pytest.raises(typer.BadParameter):
        cli._load_targets(tmp_path / "missing_state.json", None)
    assert cli._load_targets(tmp_path / "missing_state.json", "BTC=0.1,ETH=-0.1") == {"BTC": 0.1, "ETH": -0.1}


def test_fetch_testnet_account_unified_vs_standard_equity():
    import json

    def unified_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(200, json={
                "marginSummary": {"accountValue": "61.23", "totalMarginUsed": "51.46"},
                "withdrawable": "0.0",
                "assetPositions": [{"position": {"coin": "ETH", "szi": "-0.02", "unrealizedPnl": "-0.26"}}],
            })
        if t == "userAbstraction":
            return httpx.Response(200, json="unifiedAccount")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "998.71", "hold": "61.35"}]})
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(unified_handler)) as client:
        snap = ex.fetch_testnet_account(client, "0xUNIFIED")
    assert snap.equity_usd == pytest.approx(998.71)
    assert snap.free_margin_usd == pytest.approx(998.71 - 61.35)
    assert snap.position_unrealized_pnl_usd == {"ETH": pytest.approx(-0.26)}
    assert snap.position_entry_px == {}  # no entryPx in payload -> omitted, never 0

    def standard_handler(req: httpx.Request) -> httpx.Response:
        t = json.loads(req.content)["type"]
        if t == "clearinghouseState":
            return httpx.Response(200, json={
                "marginSummary": {"accountValue": "400.0", "totalMarginUsed": "100.0"},
                "withdrawable": "300.0",
                "assetPositions": [],
            })
        if t == "userAbstraction":
            return httpx.Response(200, json="disabled")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "600.0", "hold": "0.0"}]})
        return httpx.Response(200, json=[])

    with httpx.Client(transport=httpx.MockTransport(standard_handler)) as client:
        snap_std = ex.fetch_testnet_account(client, "0xSTD")
    assert snap_std.equity_usd == pytest.approx(1000.0)
    # Standard Account: Spot USDC ($600) cannot margin Perps without usdClassTransfer -> free_margin == 400 - 100 = 300.
    assert snap_std.free_margin_usd == pytest.approx(300.0)


def test_only_isolated_coins_are_skipped_for_entry_and_unwound_if_held():
    now = 1_700_000_000_000
    meta = {
        "BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40, only_isolated=False),
        "ZRO": ex.AssetSpec("ZRO", sz_decimals=1, max_leverage=10, only_isolated=True, margin_mode="strictIsolated"),
    }
    bbo = {"BTC": (60_000.0, 60_002.0, now), "ZRO": (1.07, 1.08, now)}
    # Account currently holds 20 ZRO (~$21.5) in isolated:10 and target still asks for ZRO=+0.10
    acct = ex.AccountSnapshot(
        equity_usd=1_000.0,
        free_margin_usd=950.0,
        positions_sz={"ZRO": 20.0},
        open_orders=[],
        observed_at_ms=now,
        position_leverage={"ZRO": ("isolated", 10)},
    )
    adapter = ex.DryRunAdapter()
    res = ex.execute_rebalance_cycle(
        acct,
        {"BTC": 0.10, "ZRO": 0.10},
        meta,
        bbo,
        adapter,
        limits=ex.RiskLimits(allow_only_isolated=False, default_cross_leverage=3),
        now_ms=now,
    )
    assert res["status"] == "ok"
    # ZRO must be unwound with reduce_only=True sell; BTC opens at cross:3
    zro_orders = [o for o in adapter.placed if o["coin"] == "ZRO"]
    assert len(zro_orders) == 1
    assert zro_orders[0]["side"] == "sell"
    assert zro_orders[0]["reduce_only"] is True
    assert any(u["coin"] == "BTC" and u["leverage"] == 3 and u["is_cross"] is True for u in adapter.leverage_updates)
    assert all(u["coin"] != "ZRO" for u in adapter.leverage_updates)

    # When allow_only_isolated=True, ZRO is capped to default_isolated_leverage=2
    acct_flat = ex.AccountSnapshot(equity_usd=1_000.0, free_margin_usd=1_000.0, positions_sz={}, open_orders=[], observed_at_ms=now)
    adapter2 = ex.DryRunAdapter()
    res2 = ex.execute_rebalance_cycle(
        acct_flat,
        {"ZRO": 0.10},
        meta,
        bbo,
        adapter2,
        limits=ex.RiskLimits(allow_only_isolated=True, default_isolated_leverage=2),
        now_ms=now,
    )
    assert res2["status"] == "ok"
    assert adapter2.leverage_updates == [{"mode": "dry_run", "status": "ok", "coin": "ZRO", "leverage": 2, "is_cross": False}]
    assert len(adapter2.placed) == 1 and adapter2.placed[0]["side"] == "buy"

    # If set_leverage fails for an isolated-only coin, new entry must be blocked (never fall back to 10x)
    class FailingLevAdapter(ex.DryRunAdapter):
        def set_leverage(self, coin: str, leverage: int, *, is_cross: bool):
            raise RuntimeError("exchange rejected leverage change")

    adapter3 = FailingLevAdapter()
    res3 = ex.execute_rebalance_cycle(
        acct_flat,
        {"ZRO": 0.10},
        meta,
        bbo,
        adapter3,
        limits=ex.RiskLimits(allow_only_isolated=True, default_isolated_leverage=2),
        now_ms=now,
    )
    assert res3["blocked_isolated_coins"] == ["ZRO"]
    assert res3["placed_count"] == 0


def test_pre_settlement_negative_funding_guard_defers_naive_long_entries():
    base_hour = 1_795_700_400_000 - (1_795_700_400_000 % 3_600_000)
    t_5700 = base_hour + 57 * 60 * 1000  # T-3m before :00 -> inside trap window!
    t_1000 = base_hour + 10 * 60 * 1000  # :10 -> safe outside window!

    meta = {"GRAM": ex.AssetSpec("GRAM", sz_decimals=1, max_leverage=5)}
    bbo_57 = {"GRAM": (1.00, 1.001, t_5700)}
    bbo_10 = {"GRAM": (1.00, 1.001, t_1000)}
    acct = ex.AccountSnapshot(equity_usd=1_000.0, free_margin_usd=1_000.0, positions_sz={}, open_orders=[], observed_at_ms=t_5700)
    limits = ex.RiskLimits(avoid_pre_settlement_neg_funding=True, neg_funding_apr_threshold=-25.0)

    # Opening a new LONG on GRAM (-60% APR) at :57 UTC must be deferred!
    ords_trap = ex.plan_rebalance_orders(acct, {"GRAM": 0.10}, meta, bbo_57, limits, funding_apr_by_coin={"GRAM": -60.0}, now_ms=t_5700)
    assert ords_trap == []

    # Same LONG at :10 UTC proceeds normally!
    ords_safe = ex.plan_rebalance_orders(acct, {"GRAM": 0.10}, meta, bbo_10, limits, funding_apr_by_coin={"GRAM": -60.0}, now_ms=t_1000)
    assert len(ords_safe) == 1 and ords_safe[0].side == "buy"

    # Closing an existing SHORT (reduce_only buy) at :57 UTC is STILL allowed!
    acct_short = ex.AccountSnapshot(equity_usd=1_000.0, free_margin_usd=1_000.0, positions_sz={"GRAM": -100.0}, open_orders=[], observed_at_ms=t_5700)
    ords_close = ex.plan_rebalance_orders(acct_short, {"GRAM": 0.0}, meta, bbo_57, limits, funding_apr_by_coin={"GRAM": -60.0}, now_ms=t_5700)
    assert len(ords_close) == 1 and ords_close[0].reduce_only is True


def test_filter_unquoted_targets_skips_thin_testnet_books_and_fails_closed_on_outage(tmp_path: Path):
    now = 1_700_000_000_000
    raw_targets = {"BTC": 0.12, "ETH": 0.08, "SOL": -0.06, "INIT": -0.07, "JUP": 0.05, "PONS": -0.12}
    # 2/6 coins (33.3% <= 35%) lack a two-sided Testnet book (like JUP/PONS empty ask/book)
    bbo = {
        "BTC": (60_000.0, 60_002.0, now),
        "ETH": (2_500.0, 2_501.0, now),
        "SOL": (100.0, 100.1, now),
        "INIT": (0.12, 0.121, now),
    }
    filtered, skipped = ex.filter_unquoted_targets(raw_targets, bbo, max_missing_fraction=0.35)
    assert skipped == ["JUP", "PONS"]
    assert set(filtered.keys()) == {"BTC", "ETH", "SOL", "INIT"}

    # Re-neutralizing after dropping unquoted coins restores dollar-neutrality
    neut = ex.neutralize_target_weights(filtered, max_gross=0.90)
    assert abs(sum(neut.values())) < 1e-4

    # Rebalance cycle proceeds on the 4 quoted coins, cancels stale resting order on skipped JUP,
    # leaves unmanaged MM order on HYPE untouched, and logs + formats skipped_unquoted_coins.
    meta = {
        "BTC": ex.AssetSpec("BTC", sz_decimals=5, max_leverage=40),
        "ETH": ex.AssetSpec("ETH", sz_decimals=4, max_leverage=25),
        "SOL": ex.AssetSpec("SOL", sz_decimals=2, max_leverage=20),
        "INIT": ex.AssetSpec("INIT", sz_decimals=0, max_leverage=5),
    }
    acct = ex.AccountSnapshot(
        equity_usd=10_000.0,
        free_margin_usd=10_000.0,
        positions_sz={},
        open_orders=[{"coin": "JUP", "oid": 77}, {"coin": "HYPE", "oid": 88}],
        observed_at_ms=now,
    )
    adapter = ex.DryRunAdapter()
    audit_path = tmp_path / "audit.jsonl"
    res = ex.execute_rebalance_cycle(
        acct,
        neut,
        meta,
        bbo,
        adapter,
        audit_log_path=audit_path,
        now_ms=now,
        skipped_unquoted_coins=skipped,
    )
    assert res["status"] == "ok"
    assert res["skipped_unquoted_coins"] == ["JUP", "PONS"]
    assert res["cancelled_count"] == 1
    assert [c["coin"] for c in adapter.cancelled] == ["JUP"]
    assert {o["coin"] for o in adapter.placed} == {"BTC", "ETH", "SOL", "INIT"}
    audit_lines = [line for line in audit_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert '"skipped_unquoted_coins": ["JUP", "PONS"]' in audit_lines[-1]
    tg_html = ex.format_testnet_telegram_rich_html(acct, res, bbo, hwm_usd=10_000.0)
    assert "JUP, PONS" in tg_html

    # Fail-closed safeguard: if >35% of the basket is unquoted (e.g. 3/6 = 50% systemic outage),
    # filter_unquoted_targets refuses to drop coins so check_kill_switch halts the cycle.
    bbo_outage = {"BTC": (60_000.0, 60_002.0, now), "ETH": (2_500.0, 2_501.0, now), "SOL": (100.0, 100.1, now)}
    outage_targets, outage_skipped = ex.filter_unquoted_targets(raw_targets, bbo_outage, max_missing_fraction=0.35)
    assert outage_skipped == []
    assert outage_targets == raw_targets
    res_outage = ex.execute_rebalance_cycle(acct, outage_targets, meta, bbo_outage, ex.DryRunAdapter(), now_ms=now)
    assert res_outage["status"] == "kill_switch_triggered"
    assert sorted(res_outage["violations"]) == ["missing_quote:INIT", "missing_quote:JUP", "missing_quote:PONS"]

    # Also fail-closed when a 2-coin basket loses 1 coin (leaving <2 quoted coins)
    pair_targets, pair_skipped = ex.filter_unquoted_targets({"BTC": 0.1, "ETH": -0.1}, {"BTC": (60_000.0, 60_002.0, now)}, max_missing_fraction=0.6)
    assert pair_skipped == []
    assert pair_targets == {"BTC": 0.1, "ETH": -0.1}
