from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pandas as pd
import pytest

from hl_quant import hl_spot_perp_arb as spa


def _flat_book(mid: float, spread_bps: float = 2.0, depth_usd: float = 200_000.0) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    half = mid * (spread_bps / 2.0) / 1e4
    bid_px = mid - half
    ask_px = mid + half
    sz = depth_usd / mid
    return [(bid_px, sz)], [(ask_px, sz)]


def test_fetch_matched_spot_perp_joins_by_coin_name_not_index():
    """spotMetaAndAssetCtxs universe & ctxs have mismatched lengths — join MUST use ctx['coin']."""

    def handler(request: httpx.Request) -> httpx.Response:
        body = request.read().decode("utf-8")
        if "metaAndAssetCtxs" in body and "spotMeta" not in body:
            return httpx.Response(
                200,
                json=[
                    {
                        "universe": [
                            {"name": "BTC", "maxLeverage": 40, "onlyIsolated": False},
                            {"name": "HYPE", "maxLeverage": 10, "onlyIsolated": False},
                            {"name": "PAXG", "maxLeverage": 10, "onlyIsolated": False},
                        ]
                    },
                    [
                        {"markPx": "61440", "oraclePx": "61435", "funding": "0.0000125", "dayNtlVlm": "2000000000", "openInterest": "10000"},
                        {"markPx": "66.00", "oraclePx": "66.00", "funding": "0.0000150", "dayNtlVlm": "400000000", "openInterest": "5000000"},
                        {"markPx": "3620", "oraclePx": "3615", "funding": "0.0000125", "dayNtlVlm": "5000000", "openInterest": "1000"},
                    ],
                ],
            )
        if "spotMetaAndAssetCtxs" in body:
            return httpx.Response(
                200,
                json=[
                    {
                        "tokens": [
                            {"index": 0, "name": "USDC"},
                            {"index": 1, "name": "HYPE"},
                            {"index": 2, "name": "UBTC"},
                            {"index": 3, "name": "XAUT0"},
                            {"index": 4, "name": "JUNK"},
                        ],
                        "universe": [
                            {"name": "@1", "tokens": [4, 0]},
                            {"name": "@107", "tokens": [1, 0]},
                            {"name": "@142", "tokens": [2, 0]},
                            {"name": "@182", "tokens": [3, 0]},
                        ],
                    },
                    # Intentionally shifted order + extra delisted ctx entries before real ones!
                    [
                        {"coin": "@9999", "midPx": "0.01", "dayNtlVlm": "99999999"},
                        {"coin": "@182", "midPx": "3606.0", "dayNtlVlm": "2000000"},
                        {"coin": "@142", "midPx": "61380.0", "dayNtlVlm": "25000000"},
                        {"coin": "@107", "midPx": "66.02", "dayNtlVlm": "48000000"},
                    ],
                ],
            )
        raise AssertionError(f"Unexpected request: {body}")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        rows = spa.fetch_matched_spot_perp_markets(client)

    by_base = {r["base_symbol"]: r for r in rows}
    assert set(by_base.keys()) == {"BTC", "HYPE", "PAXG"}
    assert by_base["BTC"]["spot_pair"] == "@142"
    assert by_base["BTC"]["spot_token"] == "UBTC"
    assert by_base["BTC"]["is_proxy_hedge"] is False
    assert by_base["HYPE"]["spot_pair"] == "@107"
    assert by_base["PAXG"]["spot_pair"] == "@182"
    assert by_base["PAXG"]["spot_token"] == "XAUT0"
    assert by_base["PAXG"]["is_proxy_hedge"] is True


def test_spot_perp_paper_full_lifecycle_hlp_accrual_and_flip_exit(tmp_path: Path):
    cfg = spa.SpotPerpPaperConfig(
        starting_capital_usd=100_000.0,
        hlp_reserve_share=0.10,
        slot_equity_usd=15_000.0,
        target_perp_leverage=2.0,
        min_clean_streak=2,
        entry_apr_pct=8.0,
        exit_apr_pct=2.0,
        hard_flip_apr_pct=-5.0,
        require_hlp_hurdle=False,  # lifecycle mechanics test; hurdle covered separately
    )

    def make_markets(btc_px: float, btc_apr: float, paxg_px: float = 3600.0) -> list[dict[str, Any]]:
        return [
            {
                "base_symbol": "BTC",
                "spot_pair": "@142",
                "spot_token": "UBTC",
                "quote_token": "USDC",
                "is_proxy_hedge": False,
                "spot_mid_px": btc_px,
                "spot_day_vol_usd": 25_000_000.0,
                "perp_mark_px": btc_px * 1.0005,  # +5 bps favorable basis
                "perp_oracle_px": btc_px,
                "funding_rate_h": btc_apr / 100.0 / spa.HOURS_PER_YEAR,
                "perp_day_vol_usd": 2_000_000_000.0,
                "perp_oi_usd": 500_000_000.0,
                "only_isolated": False,
                "max_leverage": 40,
            },
            {
                "base_symbol": "PAXG",
                "spot_pair": "@182",
                "spot_token": "XAUT0",
                "quote_token": "USDC",
                "is_proxy_hedge": True,
                "spot_mid_px": paxg_px,
                "spot_day_vol_usd": 2_000_000.0,
                "perp_mark_px": paxg_px * 1.001,
                "perp_oracle_px": paxg_px,
                "funding_rate_h": 11.0 / 100.0 / spa.HOURS_PER_YEAR,
                "perp_day_vol_usd": 5_000_000.0,
                "perp_oi_usd": 3_000_000.0,
                "only_isolated": False,
                "max_leverage": 10,
            },
        ]

    books_state = {"btc": 60_000.0, "paxg": 3600.0}

    def fake_book(coin_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        if coin_key == "@142":
            return _flat_book(books_state["btc"], spread_bps=1.0)
        if coin_key == "BTC":
            return _flat_book(books_state["btc"] * 1.0005, spread_bps=1.0)
        if coin_key == "@182":
            return _flat_book(books_state["paxg"], spread_bps=2.0)
        if coin_key == "PAXG":
            return _flat_book(books_state["paxg"] * 1.001, spread_bps=2.0)
        return [], []

    t0 = 1_795_700_000_000
    # Tick 1: streak=1 (< min_clean_streak=2) -> no entries yet, HLP reserve holds 10% ($10k)
    r1 = spa.run_spot_perp_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0, markets=make_markets(60_000.0, 12.0), book_fetcher=fake_book, hlp_summary=(15.0, 150e6)
    )
    assert r1["state"]["positions"] == {}
    assert r1["state"]["hlp_reserve_usd"] == pytest.approx(10_000.0, abs=1.0)

    # Tick 2 (+4h): streak=2 -> opens BTC (full $15k -> $10k notional) and PAXG (proxy half-size $7.5k -> $5k notional)
    r2 = spa.run_spot_perp_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, markets=make_markets(60_000.0, 12.0), book_fetcher=fake_book, hlp_summary=(15.0, 150e6)
    )
    pos2 = r2["state"]["positions"]
    assert set(pos2.keys()) == {"BTC", "PAXG"}
    assert pos2["BTC"]["spot_notional_usd"] == pytest.approx(10_000.0, rel=0.01)
    assert pos2["PAXG"]["spot_notional_usd"] == pytest.approx(5_000.0, rel=0.01)  # half-sized proxy
    assert r2["state"]["cum_hlp_yield_usd"] > 0.0
    assert r2["state"]["risk_telemetry"]["asset_tier_breakdown_usd"]["Gold RWA"] > 0
    assert abs(r2["state"]["risk_telemetry"]["net_delta_pct"]) < 0.05

    # Tick 3 (+8h): BTC rallies +20% (spot and perp move together -> delta-neutral!), funding + HLP accrue cleanly
    books_state["btc"] = 72_000.0
    r3 = spa.run_spot_perp_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 8 * 3_600_000, markets=make_markets(72_000.0, 12.0), book_fetcher=fake_book, hlp_summary=(15.0, 150e6)
    )
    assert r3["state"]["cum_funding_usd"] > 0.0
    # Delta-neutral check: despite +20% BTC jump, basis MTM must stay tiny (< $15)
    assert abs(r3["state"]["cum_basis_mtm_usd"]) < 15.0
    # Auto margin rebalance should have triggered because +20% move eroded 2x short perp margin
    assert r3["state"]["rebalances_count"] >= 1
    assert r3["state"]["positions"]["BTC"]["effective_perp_leverage"] <= 2.5

    # Tick 4 (+12h): BTC funding flips hard negative (-10% APR) -> immediate hard_funding_flip exit!
    r4 = spa.run_spot_perp_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 12 * 3_600_000, markets=make_markets(72_000.0, -10.0), book_fetcher=fake_book, hlp_summary=(15.0, 150e6)
    )
    assert "BTC" not in r4["state"]["positions"]
    assert "PAXG" in r4["state"]["positions"]
    assert r4["state"]["closed_positions_count"] == 1
    assert any(t["action"] == "CLOSE" and "hard_funding_flip" in t["reason"] for t in r4["trades_this_tick"])

    ticks_df = pd.read_parquet(tmp_path / "ticks.parquet")
    assert len(ticks_df) == 4
    assert r4["state"]["risk_telemetry"]["leverage_30d_max"] >= r4["state"]["risk_telemetry"]["leverage_30d_avg"]


def test_bounded_nav_sanity_guard_blocks_corrupt_basis_spike(tmp_path: Path):
    cfg = spa.SpotPerpPaperConfig(
        min_clean_streak=1, max_tick_nav_jump_pct=3.0, slot_equity_usd=45_000.0, require_hlp_hurdle=False
    )
    m_normal = [
        {
            "base_symbol": "BTC",
            "spot_pair": "@142",
            "spot_token": "UBTC",
            "quote_token": "USDC",
            "is_proxy_hedge": False,
            "spot_mid_px": 60_000.0,
            "spot_day_vol_usd": 25_000_000.0,
            "perp_mark_px": 60_020.0,
            "perp_oracle_px": 60_000.0,
            "funding_rate_h": 15.0 / 100.0 / spa.HOURS_PER_YEAR,
            "perp_day_vol_usd": 2_000_000_000.0,
            "perp_oi_usd": 500_000_000.0,
            "only_isolated": False,
            "max_leverage": 40,
        }
    ]
    t0 = 1_795_700_000_000
    spa.run_spot_perp_paper_tick(
        state_dir=tmp_path,
        cfg=cfg,
        now_ms=t0,
        markets=m_normal,
        book_fetcher=lambda _k: _flat_book(60_000.0, 1.0),
        hlp_summary=(12.0, 150e6),
    )

    # Corrupt quote where spot doubles while perp stays flat -> massive artificial +30% basis jump
    m_corrupt = [dict(m_normal[0], spot_mid_px=120_000.0)]
    r_bad = spa.run_spot_perp_paper_tick(
        state_dir=tmp_path,
        cfg=cfg,
        now_ms=t0 + 4 * 3_600_000,
        markets=m_corrupt,
        book_fetcher=lambda _k: _flat_book(60_000.0, 1.0),
        hlp_summary=(12.0, 150e6),
    )
    assert r_bad["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r_bad["state"]["nav_guard_triggers"] == 1
