"""Regression tests for post-PR-#34 audit fixes (Boros collateral USD, interrupted catch-up, venue-aware feed ok, cancel counts)."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path

from fastapi.testclient import TestClient
import httpx
import pytest

from hl_quant import hl_boros_fixed_funding as boros
from hl_quant import hl_testnet_executor as ex
from hl_quant import paper_tracker as pt
from hl_quant.api.app import _audit_record, create_app
from hl_quant.scheduler import JobSpec, WorkerScheduler


def test_boros_coin_margined_markets_convert_oi_and_volume_to_usd() -> None:
    """WBTC (tokenId=1) and WETH (tokenId=2) markets report OI/volume in coin units, not USD."""
    btc_raw = {
        "marketId": 229,
        "tokenId": 1,
        "imData": {"name": "Hyperliquid BTC 25 Dec 2026", "symbol": "HYPERLIQUID-BTC-25DEC2026", "maturity": 1798156800},
        "metadata": {"underlyingSymbol": "BTC", "fundingRateSymbol": "hyperliquid-btc", "isSpreadMarket": False},
        "data": {
            "timeToMaturity": 28.0 * 86_400,
            "markApr": 0.089,
            "midApr": 0.089,
            "bestBid": 0.0894,
            "bestAsk": 0.0930,
            "floatingApr": 0.0650,
            "notionalOI": 52.463,  # 52.463 BTC (~$3.15M), NOT $52!
            "volume24h": 7.097,  # 7.097 BTC (~$426k), NOT $7!
            "assetMarkPrice": 60_000.0,
        },
    }
    parsed = boros.parse_boros_market(
        btc_raw,
        collateral_assets={1: ("WBTC", 60_000.0), 2: ("WETH", 1_700.0), 3: ("USDT0", 1.0)},
    )
    assert parsed is not None
    assert parsed.collateral_symbol == "WBTC"
    assert parsed.notional_oi == pytest.approx(52.463 * 60_000.0, rel=1e-4)
    assert parsed.volume_24h == pytest.approx(7.097 * 60_000.0, rel=1e-4)
    assert parsed.is_actionable is True
    assert parsed.recommended_action == "Short Perp + Short Boros (Lock +8.94%)"

    # Fallback to data.assetMarkPrice when collateral_assets map is unavailable
    parsed_fb = boros.parse_boros_market(btc_raw)
    assert parsed_fb is not None
    assert parsed_fb.collateral_symbol == "WBTC"
    assert parsed_fb.is_actionable is True
    assert parsed_fb.notional_oi == pytest.approx(52.463 * 60_000.0, rel=1e-4)


def test_boros_frontier_bootstrap_refreshes_legacy_snapshot_without_collateral_symbol(tmp_path: Path) -> None:
    frontier_sentinel = tmp_path / "paper" / "hl_frontier_history" / "latest_summary.json"
    frontier_sentinel.parent.mkdir(parents=True, exist_ok=True)
    frontier_sentinel.write_text("{}", encoding="utf-8")

    boros_snap = tmp_path / "paper" / "hl_boros" / "snapshot.json"
    boros_snap.parent.mkdir(parents=True, exist_ok=True)
    boros_snap.write_text(json.dumps({"markets": [{"market_id": 229, "name": "Hyperliquid BTC 25 Dec 2026"}]}), encoding="utf-8")

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False)
    assert sched._bootstrap_needs_refresh("frontier_persistence", frontier_sentinel) is True

    boros_snap.write_text(
        json.dumps({"markets": [{"market_id": 229, "name": "Hyperliquid BTC 25 Dec 2026", "collateral_symbol": "WBTC"}]}),
        encoding="utf-8",
    )
    assert sched._bootstrap_needs_refresh("frontier_persistence", frontier_sentinel) is False


def test_boros_scan_sorts_actionable_markets_ahead_of_zero_volume_books() -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/assets"):
            return httpx.Response(
                200,
                json={"results": [{"tokenId": 1, "symbol": "WBTC", "usdPrice": "60000"}, {"tokenId": 3, "symbol": "USDT0", "usdPrice": "1.0"}]},
            )
        return httpx.Response(
            200,
            json={
                "results": [
                    {
                        "marketId": 241,
                        "tokenId": 3,
                        "imData": {"name": "Hyperliquid BTC 25 Dec 2026", "symbol": "HL-BTC-USDT", "maturity": 1798156800},
                        "metadata": {"underlyingSymbol": "BTC", "fundingRateSymbol": "hyperliquid-btc", "isSpreadMarket": False},
                        "data": {"timeToMaturity": 28 * 86400, "markApr": 0.08, "midApr": 0.08, "bestBid": 0.08, "bestAsk": 0.09, "floatingApr": 0.05, "notionalOI": 5_200_000.0, "volume24h": 0.0},
                    },
                    {
                        "marketId": 229,
                        "tokenId": 1,
                        "imData": {"name": "Hyperliquid BTC 25 Dec 2026", "symbol": "HL-BTC-WBTC", "maturity": 1798156800},
                        "metadata": {"underlyingSymbol": "BTC", "fundingRateSymbol": "hyperliquid-btc", "isSpreadMarket": False},
                        "data": {"timeToMaturity": 28 * 86400, "markApr": 0.089, "midApr": 0.089, "bestBid": 0.089, "bestAsk": 0.093, "floatingApr": 0.05, "notionalOI": 52.0, "volume24h": 7.0},
                    },
                ],
                "resumeToken": None,
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        snap = boros.run_boros_fixed_funding_scan(c, include_lighter_comparison=False)
    assert [m.market_id for m in snap.markets] == [229, 241]
    assert snap.markets[0].is_actionable is True
    assert snap.markets[1].is_actionable is False


def test_scheduler_catches_up_interrupted_readonly_job_and_preserves_recovery_alert(tmp_path: Path) -> None:
    def _mk_job(name: str, minute: int, *, readonly: bool = True) -> JobSpec:
        return JobSpec(
            name=name,
            description=name,
            cron_expr=f"{minute} * * * *",
            minutes=(minute,),
            hours=None,
            timeout_s=60,
            readonly=readonly,
            bootstrap_eligible=False,
            build_command=lambda root, data, tg: ["python", f"{name}.py"],
            is_enabled=lambda: (True, "active"),
            has_required_credentials=lambda: (True, "ok"),
        )

    status_path = tmp_path / "paper" / "hl_worker_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    # autopilot started at 20:37:02, was killed by deploy at 20:37:10 (state="interrupted"), and had
    # previously failed (`alerted_failed=True`). Live `testnet_executor` (readonly=False) was also
    # interrupted at 20:10:02 and must NOT auto-replay in the same slot.
    status_path.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "name": "autopilot",
                        "state": "interrupted",
                        "last_exit_code": -15,
                        "last_started_at_utc": "2026-11-27 20:37:02 UTC",
                        "alerted_failed": True,
                    },
                    {
                        "name": "testnet_executor",
                        "state": "interrupted",
                        "last_exit_code": -15,
                        "last_started_at_utc": "2026-11-27 20:10:02 UTC",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    ran: list[str] = []
    alerts: list[str] = []
    sched = WorkerScheduler(
        data_dir=tmp_path,
        jobs=[_mk_job("autopilot", 37, readonly=True), _mk_job("testnet_executor", 10, readonly=False)],
        telegram_enabled=True,
        startup_catchup_minutes=30,
        runner=lambda cmd, env, t: ran.append(cmd[1]) or 0,
        alert_sender=alerts.append,
    )
    sched.tick_minute(datetime(2026, 11, 27, 20, 38, tzinfo=timezone.utc))
    assert ran == ["autopilot.py"]
    assert sched._state["autopilot"]["state"] == "ok"
    assert sched._state["autopilot"]["alerted_failed"] is False
    assert len(alerts) == 1 and "ВОССТАНОВЛЕН" in alerts[0].upper()


def test_metamodel_stale_excluded_coin_checks_required_cex_venue_not_any_venue(tmp_path: Path, monkeypatch) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from test_paper_and_alpha_high_fixes import _market, _quotes
    finally:
        sys.path.pop(0)

    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda m, s: {
            "BTC": {"hl_weight": -0.20, "cex_legs": {"bybit": 0.20}, "cex_weight": 0.20, "cex_venue": "bybit"},
            "ETH": {"hl_weight": 0.10, "cex_legs": {}, "cex_weight": 0.0, "cex_venue": ""},
        },
    )
    kw = {"state_dir": tmp_path, "sleeves": [], "max_held_missing_ticks": 3}
    # Initial entry with Bybit BTC live
    assert pt.run_paper_tick(_market(20), _quotes(), cex_mid_prices={"bybit": {"BTC": 100.0}, "binance": {"BTC": 100.0}}, **kw)["status"] == "ok"
    # Bybit BTC feed dies while Binance BTC stays alive -> after 3 blocked ticks BTC is written off
    dead_bybit = {"bybit": {}, "binance": {"BTC": 100.0}}
    for n in (21, 22):
        assert pt.run_paper_tick(_market(n), _quotes(), cex_mid_prices=dead_bybit, **kw)["status"] == "held_coins_missing"
    r3 = pt.run_paper_tick(_market(23), _quotes(), cex_mid_prices=dead_bybit, **kw)
    assert r3["status"] == "ok" and r3["forced_stale_exits"] == ["BTC"]

    # Tick 24: Binance STILL quotes BTC, Bybit is STILL dead -> BTC must STAY excluded (no re-entry on Bybit!)
    r4 = pt.run_paper_tick(_market(24), _quotes(), cex_mid_prices=dead_bybit, **kw)
    assert r4["status"] == "ok"
    st4 = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    assert "BTC" not in st4["positions"]
    assert st4["stale_excluded_coins"] == ["BTC"]
    # Meanwhile HL-only ETH remains held cleanly even with empty CEX maps
    assert "ETH" in st4["positions"]


def test_cancelled_orders_count_and_audit_record_fields(tmp_path: Path) -> None:
    now = 1_795_800_000_000
    # Transport failure -> 0 cancelled orders (not 1 wrapper dict!)
    err_rec = [{"mode": "testnet_live", "error": "TimeoutError: boom", "requests": [{"coin": "SOL", "oid": 1}, {"coin": "ETH", "oid": 2}]}]
    assert ex.cancelled_orders_count(err_rec) == 0

    # Bulk cancel of 3 orders: 1 success, 1 already filled, 1 rate-limited -> 2 cancelled
    bulk_rec = [
        {
            "mode": "testnet_live",
            "requests": [{"coin": "SOL", "oid": 1}, {"coin": "ETH", "oid": 2}, {"coin": "BTC", "oid": 3}],
            "response": {
                "status": "ok",
                "response": {
                    "type": "cancel",
                    "data": {
                        "statuses": [
                            "success",
                            {"error": "Order was never placed, already canceled, or filled."},
                            {"error": "rate limited"},
                        ]
                    },
                },
            },
        }
    ]
    assert ex.cancelled_orders_count(bulk_rec) == 2

    # API _audit_record preserves cancel_failed_coins, oracle_band_blocked_coins, and rejected_count
    mapped = _audit_record(
        {
            "ts_ms": now,
            "status": "ok",
            "cancelled_count": 2,
            "placed_count": 1,
            "rejected_count": 1,
            "cancel_failed_coins": ["BTC"],
            "oracle_band_blocked_coins": ["INIT"],
        }
    )
    assert mapped["cancelled_count"] == 2
    assert mapped["rejected_count"] == 1
    assert mapped["cancel_failed_coins"] == ["BTC"]
    assert mapped["oracle_band_blocked_coins"] == ["INIT"]

    # Smoke-check via FastAPI /api/v1/overview
    (tmp_path / "hl_testnet_audit.jsonl").write_text(json.dumps(mapped) + "\n", encoding="utf-8")
    client = TestClient(create_app(data_root=tmp_path, build_sha="abc"))
    rec = client.get("/api/v1/overview").json()["testnet_audit_recent"][0]
    assert rec["cancel_failed_coins"] == ["BTC"]
    assert rec["oracle_band_blocked_coins"] == ["INIT"]
