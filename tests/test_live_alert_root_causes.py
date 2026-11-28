"""Regression tests for the root causes of alerts that were firing on the live stack after `e8faca9`."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from hl_quant import hl_autopilot as ap
from hl_quant import hl_funding_settlement_study as fss
from hl_quant import hl_testnet_executor as ex
from hl_quant.metrics import render_prometheus_metrics
from hl_quant.scheduler import build_job_specs


def _closes(n: int = 50, drift: float = 0.005, start: float = 100.0) -> list[float]:
    return [round(start * ((1.0 + drift) ** i), 4) for i in range(n)]


def test_autopilot_reports_post_rebalance_drift_so_risk_alert_clears(tmp_path: Path):
    sp_dir = tmp_path / "hl_spot_perp_paper"
    cd_dir = tmp_path / "hl_cross_dex_paper"
    h3_dir = tmp_path / "hl_hip3_paper"
    for d, eq in [(sp_dir, 100_000.0), (cd_dir, 100_000.0), (h3_dir, 50_000.0)]:
        d.mkdir(parents=True)
        (d / "state.json").write_text(
            json.dumps({"equity_usd": eq, "starting_capital_usd": eq, "positions": {}, "hlp_apr_pct": 14.0}),
            encoding="utf-8",
        )
    closes = {c: _closes() for c in ap.CTA_UNIVERSE}
    cfg = ap.AutopilotConfig(starting_capital_usd=250_000.0, rebalance_deadband_pct=0.5, max_tick_nav_jump_pct=4.0)
    t0 = 1_795_700_000_000
    ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0, daily_closes=closes)

    # +3% on spot_perp drifts its sleeve weight past the 0.5% deadband -> rebalance this tick.
    (sp_dir / "state.json").write_text(
        json.dumps({"equity_usd": 103_000.0, "starting_capital_usd": 100_000.0, "positions": {}, "hlp_apr_pct": 14.0}),
        encoding="utf-8",
    )
    r2 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, daily_closes=closes)
    rt = r2["state"]["risk_telemetry"]
    assert rt["rebalanced_this_tick"] is True
    assert rt["pre_rebalance_max_drift_pct"] >= 0.5
    assert rt["rebalance_reason"].startswith("deadband_drift(")
    # After the rebalance the book sits on target: the alerting gauge must not keep the old drift.
    assert rt["max_weight_drift_pct"] == 0.0
    txt = render_prometheus_metrics({"autopilot": r2["state"]})
    assert "hl_autopilot_max_drift_pct 0" in txt


def test_illiquid_testnet_books_are_skipped_but_unreadable_books_still_fail_closed():
    now = 1_700_000_000_000
    targets = {"BTC": 0.15, "ETH": -0.15, "SOL": 0.1, "JUP": 0.05, "WLFI": -0.05}
    bbo = {"BTC": (60_000.0, 60_002.0, now), "ETH": (2_500.0, 2_501.0, now), "SOL": (100.0, 100.1, now)}

    # 2/5 = 40% > 35%: without book context this is (wrongly) treated as a feed outage.
    legacy, legacy_skipped = ex.filter_unquoted_targets(targets, bbo, max_missing_fraction=0.35)
    assert legacy_skipped == []
    assert legacy == targets

    # Books were read fine but are one-sided / empty (thin Testnet alts): skip, no kill-switch.
    tops = {
        "BTC": (60_000.0, 60_002.0, now),
        "ETH": (2_500.0, 2_501.0, now),
        "SOL": (100.0, 100.1, now),
        "JUP": (0.24, None, now),
        "WLFI": (None, None, now),
    }
    filtered, skipped = ex.filter_unquoted_targets(targets, bbo, max_missing_fraction=0.35, book_tops=tops)
    assert skipped == ["JUP", "WLFI"]
    assert set(filtered) == {"BTC", "ETH", "SOL"}
    acct = ex.AccountSnapshot(
        equity_usd=1_000.0, free_margin_usd=1_000.0, positions_sz={}, open_orders=[], observed_at_ms=now
    )
    assert ex.check_kill_switch(acct, filtered, bbo, ex.RiskLimits(), high_water_mark_usd=1_000.0, now_ms=now) == []

    # A malformed / unreadable l2Book (observed_ms == 0) is a genuine feed failure: keep fail-closed.
    tops_outage = {**tops, "JUP": (None, None, 0), "WLFI": (None, None, 0)}
    out_t, out_skipped = ex.filter_unquoted_targets(targets, bbo, max_missing_fraction=0.35, book_tops=tops_outage)
    assert out_skipped == []
    assert out_t == targets


def test_fetch_testnet_book_tops_marks_malformed_book_as_unreadable():
    def handler(req: httpx.Request) -> httpx.Response:
        coin = json.loads(req.content)["coin"]
        if coin == "EMPTY":
            return httpx.Response(200, json={"coin": coin, "time": 1_700_000_000_000, "levels": [[], []]})
        if coin == "BAD":
            return httpx.Response(200, json={"error": "unknown"})
        return httpx.Response(
            200,
            json={"coin": coin, "time": 1_700_000_000_000, "levels": [[{"px": "10", "sz": "1", "n": 1}], [{"px": "11", "sz": "1", "n": 1}]]},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        tops = ex.fetch_testnet_book_tops(client, ["OK", "EMPTY", "BAD"])
    assert tops["OK"] == (10.0, 11.0, 1_700_000_000_000)
    assert tops["EMPTY"] == (None, None, 1_700_000_000_000)
    assert tops["BAD"] == (None, None, 0)


def test_settlement_study_paces_requests_on_hyperliquid_weight_budget(monkeypatch):
    hour = fss.HOUR_MS
    m15 = fss.FIFTEEN_MIN_MS
    base = 1_795_000_000_000 - (1_795_000_000_000 % hour)
    candles = [{"t": base + h * hour + off, "o": "100.0", "c": "100.0"} for h in range(40) for off in (-m15, 0, m15)]
    funding = [{"time": base + h * hour, "fundingRate": "0.0001"} for h in range(40)]

    sleeps: list[float] = []

    async def fake_sleep(s: float) -> None:
        sleeps.append(float(s))

    monkeypatch.setattr(fss.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(fss.time, "monotonic", lambda: 0.0)

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        if body["type"] == "metaAndAssetCtxs":
            return httpx.Response(
                200,
                json=[
                    {"universe": [{"name": "BTC"}]},
                    [{"markPx": "60000", "openInterest": "1000", "dayNtlVlm": "50000000", "funding": "0.0001"}],
                ],
            )
        if body["type"] == "candleSnapshot":
            return httpx.Response(200, json=candles)  # 120 bars -> +2 weight
        return httpx.Response(200, json=funding)  # 40 rows -> +2 weight

    real_client = httpx.AsyncClient

    def patched(*a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **kw)

    monkeypatch.setattr(fss.httpx, "AsyncClient", patched)
    df, meta = asyncio.run(fss.fetch_settlement_dataset(bar_interval="15m", max_coins=1, weight_budget_per_min=600.0))
    assert meta["coins_collected"] == 1 and not df.empty
    # 600 weight/min = 10 weight/s: candle call (20) + its item surcharge (2) must elapse before the
    # funding call is sent -> a 2.2s wait, instead of the old fixed 0.25s (4 req/s ~ 4,800 weight/min).
    assert any(s == pytest.approx(2.2, abs=1e-6) for s in sleeps), sleeps


def test_settlement_job_timeout_covers_paced_collection():
    job = {j.name: j for j in build_job_specs()}["settlement_study"]
    assert job.timeout_s >= 1200
