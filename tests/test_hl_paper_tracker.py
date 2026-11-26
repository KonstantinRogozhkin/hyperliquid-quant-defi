from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from hl_quant import alpha_families as af
from hl_quant import funding_arb as fa
from hl_quant import paper_tracker as pt

H = fa.HOUR_MS
T0 = 1_790_000_000_000 - (1_790_000_000_000 % H)


def _synthetic_market(n_hours: int = 240, n_coins: int = 6) -> af.WideMarket:
    rng = np.random.default_rng(17)
    hours = T0 + np.arange(n_hours, dtype="int64") * H
    rows = []
    for i in range(n_coins):
        coin = f"C{i}"
        hl_px = 100.0 * np.exp(np.cumsum(rng.normal(0.0, 0.003, n_hours)))
        by_px = hl_px * (1.0 + rng.normal(0.0, 0.0005, n_hours))
        hl_rate = (2e-4 if i == 0 else -1e-4 if i == 1 else 0.0) + rng.normal(0.0, 1e-5, n_hours)
        rows.append(
            pd.DataFrame(
                {
                    "hour_ms": hours,
                    "coin": coin,
                    "hl_close": hl_px,
                    "hl_ret": pd.Series(hl_px).pct_change().to_numpy(),
                    "hl_notional_usd": np.full(n_hours, 500_000.0),
                    "hl_fund_next": hl_rate,
                    "hl_rate_h": hl_rate,
                    "bybit_close": by_px,
                    "bybit_ret": pd.Series(by_px).pct_change().to_numpy(),
                    "bybit_notional_usd": np.full(n_hours, 500_000.0),
                    "bybit_fund_next": np.zeros(n_hours),
                    "bybit_rate_h": np.zeros(n_hours),
                    "binance_close": by_px,
                    "binance_ret": pd.Series(by_px).pct_change().to_numpy(),
                    "binance_notional_usd": np.full(n_hours, 500_000.0),
                    "binance_fund_next": np.zeros(n_hours),
                    "binance_rate_h": np.zeros(n_hours),
                    "okx_close": by_px,
                    "okx_ret": pd.Series(by_px).pct_change().to_numpy(),
                    "okx_notional_usd": np.full(n_hours, 500_000.0),
                    "okx_fund_next": np.full(n_hours, -2e-4 if i == 0 else 0.0),
                    "okx_rate_h": np.full(n_hours, -2e-4 if i == 0 else 0.0),
                }
            )
        )
    return af.build_wide_market(pd.concat(rows, ignore_index=True))


def test_paper_tracker_idempotent_and_charges_l2_spread(tmp_path: Path):
    m1 = _synthetic_market(n_hours=400, n_coins=6)
    quotes = {
        c: pt.L2Quote(coin=c, best_bid=99.95, best_ask=100.05, mid_px=100.0, spread_bps=10.0, top5_bid_usd=500_000.0, top5_ask_usd=500_000.0)
        for c in m1.coins
    }
    r1 = pt.run_paper_tick(m1, quotes, state_dir=tmp_path, starting_capital_usd=100_000.0)
    assert r1["status"] == "ok"
    assert r1["active_coins"] > 0
    assert r1["cost_pnl_usd"] < 0.0  # charged maker fee + 0.5 * 10 bps L2 spread
    assert (tmp_path / "ticks.parquet").exists()
    assert (tmp_path / "trades.parquet").exists()

    # Running same hour again must be idempotent no-op
    r_dup = pt.run_paper_tick(m1, quotes, state_dir=tmp_path)
    assert r_dup["status"] == "duplicate_hour_skipped"

    # Advance 1 hour and verify funding + mark-to-market tick appends cleanly
    m2 = _synthetic_market(n_hours=401, n_coins=6)
    r2 = pt.run_paper_tick(m2, quotes, state_dir=tmp_path)
    assert r2["status"] == "ok"
    assert r2["fund_pnl_usd"] != 0.0
    ticks = pd.read_parquet(tmp_path / "ticks.parquet")
    assert len(ticks) == 2
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["tick_count"] == 2
    assert state["positions"]["C0"]["cex_venue"] == "okx"
    assert "cum_pnl_usd" in state["positions"]["C0"]

    msg = pt.format_telegram_summary(r2, state)
    assert "Hyperliquid Metamodel" in msg
    assert "<blockquote><pre>" in msg
    assert "Coin" in msg and "L2bp" in msg
    assert "Paper/shadow L2 mode" in msg

    sent = {}

    def handler(req: httpx.Request) -> httpx.Response:
        sent["payload"] = json.loads(req.content.decode())
        return httpx.Response(200, text=json.dumps({"ok": True, "result": {"message_id": 123}}))

    resp = pt.send_telegram_message(msg, bot_token="tok", chat_id="42", thread_id="7", transport=httpx.MockTransport(handler))
    assert resp == {"ok": True, "message_id": 123}
    assert sent["payload"]["chat_id"] == "42"
    assert sent["payload"]["message_thread_id"] == 7
    assert sent["payload"]["parse_mode"] == "HTML"

    rich_html = pt.format_telegram_rich_html(r2, state, ticks_df=ticks)
    assert '<table bordered striped>' in rich_html
    assert "<h1>📈 Hyperliquid" in rich_html
    assert "Что открыто" in rich_html
    assert "Сумма" in rich_html
    assert "PnL" in rich_html
    assert "OKX" in rich_html
    assert "24ч:" in rich_html and "7д:" in rich_html
    rich_sent = {}

    def rich_handler(req: httpx.Request) -> httpx.Response:
        rich_sent["url"] = str(req.url)
        rich_sent["payload"] = json.loads(req.content.decode())
        return httpx.Response(200, text=json.dumps({"ok": True, "result": {"message_id": 456}}))

    rresp = pt.send_telegram_rich_message(rich_html, bot_token="tok", chat_id="42", thread_id="7", transport=httpx.MockTransport(rich_handler))
    assert rresp == {"ok": True, "message_id": 456}
    assert rich_sent["url"].endswith("/sendRichMessage")
    assert rich_sent["payload"]["rich_message"]["html"] == rich_html
    assert rich_sent["payload"]["message_thread_id"] == 7


def _flat_quotes(market: af.WideMarket) -> dict[str, pt.L2Quote]:
    return {
        c: pt.L2Quote(coin=c, best_bid=99.95, best_ask=100.05, mid_px=100.0, spread_bps=10.0, top5_bid_usd=5e5, top5_ask_usd=5e5)
        for c in market.coins
    }


def test_paper_tracker_skips_tick_when_held_coin_is_missing(tmp_path: Path):
    m1 = _synthetic_market(n_hours=400, n_coins=6)
    assert pt.run_paper_tick(m1, _flat_quotes(m1), state_dir=tmp_path)["status"] == "ok"
    before = (tmp_path / "state.json").read_text()
    held = sorted(json.loads(before)["positions"])
    assert held

    # Next tick's market lost one held coin (e.g. a transient fetch failure).
    m2 = _synthetic_market(n_hours=401, n_coins=6)
    keep = [c for c in m2.coins if c != held[0]]
    m2_missing = af.WideMarket(
        hours=m2.hours,
        coins=keep,
        **{f: getattr(m2, f)[keep] for f in af.WideMarket.__dataclass_fields__ if f not in ("hours", "coins")},
    )
    res = pt.run_paper_tick(m2_missing, _flat_quotes(m2_missing), state_dir=tmp_path)
    assert res["status"] == "held_coins_missing"
    assert res["missing_coins"] == [held[0]]
    assert (tmp_path / "state.json").read_text() == before  # ledger untouched


def test_paper_tracker_keeps_separate_cex_legs_per_venue_and_never_uses_hl_price(tmp_path: Path):
    market = _synthetic_market(n_hours=400, n_coins=6)
    tick = int(market.hours[-1])
    # The last OKX close is missing: the leg must be marked with the last known OKX close.
    market.okx_close.loc[tick, "C0"] = np.nan
    spec = af.HypothesisSpec(
        name="carry_test", family="funding_carry_arb", venue_mode="cross_venue",
        params={"mode": "best_cex", "lookback_h": 24, "entry_apr": 0.05, "min_vol_usd": 1.0},
    )
    sleeves = [pt.MetamodelSleeve(spec=spec, weight=0.5), pt.MetamodelSleeve(spec=af.HypothesisSpec(
        name="basis_test", family="funding_carry_arb", venue_mode="cross_venue",
        params={"mode": "bybit", "lookback_h": 24, "entry_apr": 0.05, "min_vol_usd": 1.0},
    ), weight=0.5)]
    targets = pt.compute_latest_target_positions(market, sleeves)
    legs = targets["C0"]["cex_legs"]
    assert set(legs) == {"okx", "bybit"}  # two sleeves hedge C0 on different venues
    assert targets["C0"]["cex_weight"] == round(sum(legs.values()), 6)

    res = pt.run_paper_tick(market, _flat_quotes(market), state_dir=tmp_path, sleeves=sleeves)
    assert res["status"] == "ok"
    pos = json.loads((tmp_path / "state.json").read_text())["positions"]["C0"]
    expected_okx = float(market.okx_close["C0"].dropna().iloc[-1])
    assert pos["last_cex_px_by_venue"]["okx"] == expected_okx
    assert pos["last_cex_px_by_venue"]["bybit"] == float(market.bybit_close.loc[tick, "C0"])
    assert pos["last_hl_px"] == 100.0
    assert expected_okx != 100.0


def test_paper_tracker_charges_both_legs_when_hedge_moves_venue(tmp_path: Path):
    market = _synthetic_market(n_hours=400, n_coins=6)
    tick = int(market.hours[-1])
    state = {
        "starting_capital_usd": 100_000.0,
        "equity_usd": 100_000.0,
        "last_tick_hour_ms": tick - 4 * H,
        "tick_count": 1,
        # Legacy single-venue format: C0 hedged on Bybit with the same weight the new target uses.
        "positions": {"C0": {"hl_weight": -0.1, "cex_weight": 0.1, "cex_venue": "bybit", "last_hl_px": 100.0, "last_cex_px": 100.0}},
    }
    (tmp_path / "state.json").write_text(json.dumps(state))
    # A sleeve stub returning a fixed OKX hedge for C0.
    orig = pt.compute_latest_target_positions
    pt.compute_latest_target_positions = lambda m, s: {
        "C0": {"hl_weight": -0.1, "cex_legs": {"okx": 0.1}, "cex_weight": 0.1, "cex_venue": "okx"}
    }
    try:
        res = pt.run_paper_tick(market, _flat_quotes(market), state_dir=tmp_path, sleeves=[], hl_fee_bps=0.0, cex_fee_bps=2.0, l2_spread_fraction=0.0)
    finally:
        pt.compute_latest_target_positions = orig
    # HL leg unchanged; closing the Bybit leg (0.1) and opening OKX (0.1) costs 0.2 * 2 bps.
    assert res["cost_pnl_usd"] == -100_000.0 * 0.2 * 2.0 / 1e4


def test_default_frozen_metamodel_is_post_fix_dev_selection():
    sleeves = {s.spec.name: s.weight for s in pt.default_frozen_metamodel()}
    assert sleeves == {"carry_best_cex_L168_in5": 0.400, "xs_rev_L24_r12": 0.379, "xs_mom_L72_r24": 0.221}
    assert abs(sum(sleeves.values()) - 1.0) < 1e-9


def test_paper_tracker_hl_cost_can_be_all_in_without_spread(tmp_path: Path):
    market = _synthetic_market(n_hours=400, n_coins=6)
    q = _flat_quotes(market)  # 10 bps spread
    a = pt.run_paper_tick(market, q, state_dir=tmp_path / "a", hl_fee_bps=5.0, cex_fee_bps=0.0, l2_spread_fraction=0.0)
    b = pt.run_paper_tick(market, q, state_dir=tmp_path / "b", hl_fee_bps=5.0, cex_fee_bps=0.0, l2_spread_fraction=0.0, hl_spread_fraction=0.0)
    c = pt.run_paper_tick(market, q, state_dir=tmp_path / "c", hl_fee_bps=5.0, cex_fee_bps=0.0, l2_spread_fraction=0.5, hl_spread_fraction=0.0)
    assert a["cost_pnl_usd"] == b["cost_pnl_usd"]  # default: HL fraction follows l2_spread_fraction
    hl_turn = sum(abs(p["hl_weight"]) for p in json.loads((tmp_path / "a" / "state.json").read_text())["positions"].values())
    assert abs(a["cost_pnl_usd"] + 100_000.0 * hl_turn * 5.0 / 1e4) < 1e-6  # HL legs pay exactly 5 bps, no spread
    assert c["cost_pnl_usd"] < a["cost_pnl_usd"]  # CEX leg still pays half the spread
