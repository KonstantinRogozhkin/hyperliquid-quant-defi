from __future__ import annotations

import json
from pathlib import Path
import sys

import httpx
import pytest
from typer.testing import CliRunner

from hl_quant import hl_testnet_executor as ex
from hl_quant import hl_testnet_risk as risk

ROOT = Path(__file__).resolve().parents[1]


def _ctx_payload() -> list:
    return [
        {"universe": [{"name": "XMR"}, {"name": "INIT"}, {"name": "SKY"}, {"name": "BAD"}]},
        [
            {"markPx": "328.0", "oraclePx": "401.5", "funding": "0.0000125"},
            {"markPx": "0.1197", "oraclePx": "0.0921", "funding": "0"},
            {"markPx": "0.04639", "oraclePx": "0.04682", "funding": "0"},
            {"markPx": None, "oraclePx": "1.0", "funding": "0"},
        ],
    ]


def _account() -> ex.AccountSnapshot:
    # Mirrors the 2026-11-27 Testnet incident: XMR long under water at mark, INIT short, SKY orphan.
    return ex.AccountSnapshot(
        equity_usd=992.67,
        free_margin_usd=847.5,
        positions_sz={"XMR": 0.16, "INIT": -281.0, "SKY": -975.0, "FLAT": 0.0},
        open_orders=[{"coin": "XMR", "oid": 1}],
        observed_at_ms=0,
        position_notional_usd={"XMR": 52.49, "INIT": -33.64, "SKY": -45.23},
        position_entry_px={"XMR": 462.5025, "INIT": 0.13561, "SKY": 0.046718},
        position_unrealized_pnl_usd={"XMR": -21.51, "INIT": 4.47, "SKY": 0.32},
    )


def test_parse_asset_ctxs_skips_unpriced_and_annualizes_funding():
    ctxs = risk.parse_asset_ctxs(_ctx_payload())
    assert set(ctxs) == {"XMR", "INIT", "SKY"}
    assert ctxs["XMR"].mark_px == pytest.approx(328.0)
    assert ctxs["XMR"].oracle_px == pytest.approx(401.5)
    assert ctxs["XMR"].funding_apr_pct == pytest.approx(0.0000125 * 24 * 365 * 100)
    assert risk.parse_asset_ctxs({"bad": 1}) == {}


def test_build_risk_snapshot_drawdown_orphans_and_mark_vs_oracle():
    snap = risk.build_risk_snapshot(
        _account(),
        high_water_mark_usd=1006.548472,
        asset_ctxs=risk.parse_asset_ctxs(_ctx_payload()),
        book_sides={"XMR": "two_sided", "INIT": "two_sided", "SKY": "ask_only"},
        now_ms=123,
    )
    assert snap["observed_at_ms"] == 123
    assert snap["drawdown_pct"] == pytest.approx((1 - 992.67 / 1006.548472) * 100, abs=1e-3)
    assert snap["drawdown_headroom_pct"] == pytest.approx(5.0 - snap["drawdown_pct"], abs=1e-3)
    assert snap["open_positions"] == 3  # zero-size FLAT is ignored
    assert snap["gross_notional_usd"] == pytest.approx(52.49 + 33.64 + 45.23)
    assert snap["unrealized_pnl_mark_usd"] == pytest.approx(-21.51 + 4.47 + 0.32)

    xmr_oracle = 0.16 * (401.5 - 462.5025)
    init_oracle = -281.0 * (0.0921 - 0.13561)
    sky_oracle = -975.0 * (0.04682 - 0.046718)
    assert snap["unrealized_pnl_oracle_usd"] == pytest.approx(xmr_oracle + init_oracle + sky_oracle, abs=1e-3)
    gap = snap["unrealized_pnl_mark_usd"] - snap["unrealized_pnl_oracle_usd"]
    assert snap["mark_oracle_pnl_gap_usd"] == pytest.approx(gap, abs=1e-3)
    assert snap["mark_oracle_pnl_gap_pct"] == pytest.approx(abs(gap) / 992.67 * 100, abs=1e-3)
    assert snap["max_mark_oracle_dev_coin"] == "INIT"
    assert snap["max_mark_oracle_dev_pct"] == pytest.approx((0.1197 / 0.0921 - 1) * 100, abs=1e-3)

    assert set(snap["orphan_positions"]) == {"SKY"}
    assert snap["orphan_positions"]["SKY"]["book"] == "ask_only"
    assert snap["orphan_notional_usd"] == pytest.approx(45.23)
    assert snap["orphan_unrealized_pnl_usd"] == pytest.approx(0.32)
    assert snap["open_orders"] == 1
    by_coin = {r["coin"]: r for r in snap["per_coin"]}
    assert by_coin["SKY"]["orphan"] is True and by_coin["XMR"]["orphan"] is False


def test_build_risk_snapshot_without_oracle_falls_back_to_mark_and_new_peak_has_zero_dd():
    snap = risk.build_risk_snapshot(
        _account(),
        high_water_mark_usd=0.0,
        asset_ctxs={},
        book_sides={},
    )
    assert snap["high_water_mark_usd"] == pytest.approx(992.67)
    assert snap["drawdown_pct"] == 0.0
    assert snap["unrealized_pnl_oracle_usd"] == pytest.approx(snap["unrealized_pnl_mark_usd"])
    # Unknown book state is treated as unmanageable (fail-closed visibility).
    assert set(snap["orphan_positions"]) == {"XMR", "INIT", "SKY"}


def test_fetch_book_sides_classifies_one_sided_and_empty_books():
    books = {
        "A": [[{"px": "1", "sz": "1", "n": 1}], [{"px": "2", "sz": "1", "n": 1}]],
        "B": [[{"px": "1", "sz": "1", "n": 1}], []],
        "C": [[], [{"px": "2", "sz": "1", "n": 1}]],
        "D": [[], []],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        coin = json.loads(req.content)["coin"]
        if coin == "E":
            return httpx.Response(500)
        return httpx.Response(200, json={"coin": coin, "levels": books[coin], "time": 1})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        sides = risk.fetch_book_sides(client, ["A", "B", "C", "D", "E"])
    assert sides == {"A": "two_sided", "B": "bid_only", "C": "ask_only", "D": "empty", "E": "unknown"}


def test_risk_probe_cli_writes_snapshot_read_only(tmp_path: Path, monkeypatch):
    sys.path.insert(0, str(ROOT / "scripts" / "analytics"))
    import importlib

    probe = importlib.import_module("run_hl_testnet_risk_probe")
    seen_types: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        assert req.url.host == "api.hyperliquid-testnet.xyz"
        assert req.url.path == "/info"  # never /exchange
        body = json.loads(req.content)
        seen_types.append(body["type"])
        t = body["type"]
        if t == "clearinghouseState":
            return httpx.Response(200, json={
                "marginSummary": {"accountValue": "150.0", "totalMarginUsed": "110.0"},
                "withdrawable": "3.0",
                "assetPositions": [{"position": {"coin": "XMR", "szi": "0.16", "entryPx": "462.5", "positionValue": "52.5", "unrealizedPnl": "-21.5"}}],
            })
        if t == "userAbstraction":
            return httpx.Response(200, json="unifiedAccount")
        if t == "spotClearinghouseState":
            return httpx.Response(200, json={"balances": [{"coin": "USDC", "total": "992.67", "hold": "145.0"}]})
        if t == "metaAndAssetCtxs":
            return httpx.Response(200, json=[{"universe": [{"name": "XMR"}]}, [{"markPx": "328", "oraclePx": "401.5", "funding": "0"}]])
        if t == "l2Book":
            return httpx.Response(200, json={"levels": [[{"px": "310", "sz": "1", "n": 1}], [{"px": "363", "sz": "1", "n": 1}]]})
        return httpx.Response(200, json=[])

    real_client = httpx.Client
    monkeypatch.setattr(probe.httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler)))
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0xTEST")
    hwm = tmp_path / "hwm.json"
    ex.save_high_water_mark(hwm, 1006.548472)
    out = tmp_path / "risk" / "latest.json"
    res = CliRunner().invoke(probe.app, ["--hwm-state", str(hwm), "--output-json", str(out)])
    assert res.exit_code == 0, res.output
    snap = json.loads(out.read_text())
    assert snap["equity_usd"] == pytest.approx(992.67)
    assert snap["drawdown_pct"] == pytest.approx(1.3788, abs=1e-3)
    assert snap["orphan_positions"] == {}
    # The probe must leave the executor-owned HWM untouched.
    assert ex.load_high_water_mark(hwm) == pytest.approx(1006.548472)
    assert "order" not in seen_types and "cancel" not in seen_types
