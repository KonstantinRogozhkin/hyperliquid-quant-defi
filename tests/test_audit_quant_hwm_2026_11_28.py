"""Regression tests for 2026-11-28: net-of-flows testnet HWM and quant-methodology fixes."""

from __future__ import annotations

import pytest

from hl_quant import hl_post_trade_attribution as pta
from hl_quant import hl_testnet_executor as ex

ME = "0xabc"


def test_withdrawal_lowers_hwm_instead_of_tripping_drawdown(tmp_path):
    # Peak 1,006.55; the owner withdraws $100 -> equity 906 is NOT a 10% trading drawdown.
    adj = ex.flow_adjusted_high_water_mark(1_006.55, prev_net_flow_usd=1_000.0, cur_net_flow_usd=900.0)
    assert adj == pytest.approx(906.55)
    assert ex.drawdown_pct(906.0, adj) < 0.1
    # Deposit raises the peak so fresh capital is not booked as profit.
    assert ex.flow_adjusted_high_water_mark(1_000.0, 1_000.0, 1_500.0) == pytest.approx(1_500.0)
    # Unknown flows -> unchanged.
    assert ex.flow_adjusted_high_water_mark(1_000.0, None, 900.0) == 1_000.0
    assert ex.flow_adjusted_high_water_mark(1_000.0, 1_000.0, None) == 1_000.0


def test_hwm_state_roundtrip_keeps_flow_baseline(tmp_path):
    p = tmp_path / "hwm.json"
    ex.save_high_water_mark(p, 1_006.5, net_external_flow_usd=1_000.0)
    assert ex.load_high_water_mark(p) == pytest.approx(1_006.5)
    assert ex.load_hwm_net_flow(p) == pytest.approx(1_000.0)
    ex.save_high_water_mark(p, 1_010.0)
    assert ex.load_hwm_net_flow(p) is None  # legacy file shape


def test_net_external_flow_is_signed_and_ignores_internal_transfers():
    ledger = [
        {"time": 1, "delta": {"type": "deposit", "usdc": "1000"}},
        {"time": 2, "delta": {"type": "accountClassTransfer", "usdc": "500", "toPerp": True}},
        {"time": 3, "delta": {"type": "withdraw", "usdc": "1200", "fee": "1"}},
    ]
    assert pta.net_external_usdc_flow_usd(ledger, ME) == pytest.approx(-201.0)
    assert pta.compute_net_deposited_capital_usd(ledger, ME) is None  # unchanged contract
    assert pta.net_external_usdc_flow_usd([], ME) is None


def test_benjamini_hochberg_rejects_lone_marginal_t_among_many_tests():
    from hl_quant import hl_funding_settlement_study as fss

    # 44 simultaneous cells: one t = 2.3 (p ~ 0.011) among noise is not a discovery at q = 0.10...
    t_vals = [2.3] + [0.0] * 43
    p = [fss.one_sided_p_value(t) for t in t_vals]
    assert fss.benjamini_hochberg(p, 0.10)[0] is False
    # ...but a strong t = 4.5 (p ~ 3e-6) survives.
    p2 = [fss.one_sided_p_value(4.5)] + p[1:]
    assert fss.benjamini_hochberg(p2, 0.10)[0] is True
    assert fss.one_sided_p_value(float("nan")) == 1.0
    assert fss.benjamini_hochberg([], 0.1) == []


def test_paper_funding_uses_realized_settlements_per_position_window():
    import json

    import httpx

    from hl_quant import hl_cross_dex_paper as cdp

    H = 3_600_000
    t_prev, t_now = 100 * H + 5 * 60_000, 104 * H + 5 * 60_000
    seen: list[tuple[str, int]] = []

    def handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        seen.append((body["coin"], body["startTime"]))
        # Spiky snapshot at tick time would be 1e-3/h; realized settlements sum to 4e-5.
        rows = [{"coin": body["coin"], "fundingRate": "0.00001", "time": (101 + k) * H} for k in range(4)]
        return httpx.Response(200, json=rows)

    positions = {
        "NEAR": {"coin": "NEAR"},
        "ETH": {"coin": "ETH", "last_accrual_ms": t_prev - 4 * H},  # skipped one tick (missing quote)
    }
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        out = cdp.settled_funding_by_position(c, positions, lambda p: [p.get("coin")], t_prev, t_now)
    assert out["NEAR"] == pytest.approx(4e-5)
    assert {coin: start for coin, start in seen} == {"NEAR": t_prev + 1, "ETH": t_prev - 4 * H + 1}
    assert cdp.settled_funding_by_position(None, positions, lambda p: [p.get("coin")], t_prev, t_now) == {}


# ----------------------------------------------------------------- PR #36 review follow-ups


def test_hwm_not_persisted_during_ledger_outage_once_flow_tracking_is_active():
    assert ex.hwm_persist_allowed(None, 1_000.0) is False  # outage + baseline -> keep old file
    assert ex.hwm_persist_allowed(1_500.0, 1_000.0) is True
    assert ex.hwm_persist_allowed(None, None) is True  # legacy file without flow tracking


def test_realized_funding_window_is_capped_like_the_fallback_leg():
    import json

    import httpx

    from hl_quant import hl_cross_dex_paper as cdp

    H = 3_600_000
    t_now = 200 * H + 5 * 60_000
    starts: list[int] = []

    def handler(req: httpx.Request) -> httpx.Response:
        starts.append(json.loads(req.content)["startTime"])
        return httpx.Response(200, json=[])

    positions = {"NEAR": {"coin": "NEAR", "last_accrual_ms": t_now - 48 * H}}
    with httpx.Client(transport=httpx.MockTransport(handler)) as c:
        cdp.settled_funding_by_position(c, positions, lambda p: [p.get("coin")], t_now - 48 * H, t_now, cap_hours=24.0)
    assert starts == [t_now - 24 * H + 1]


def test_sleeve_sharpe_does_not_depend_on_tick_cadence():
    import numpy as np

    from hl_quant import hl_autopilot as ap

    rng = np.random.default_rng(7)
    drift_h, vol_h = 2e-5, 4e-4
    r4 = list(drift_h * 4 + vol_h * 2 * rng.standard_normal(4000))
    r16 = list(drift_h * 16 + vol_h * 4 * rng.standard_normal(1000))
    sh4, v4 = ap._sleeve_sharpe_and_vol(r4, dt_hours=[4.0] * len(r4))
    sh16, v16 = ap._sleeve_sharpe_and_vol(r16, dt_hours=[16.0] * len(r16))
    assert v16 == pytest.approx(v4, rel=0.1)
    assert abs(sh16 - sh4) < 1.5  # same process -> same annualized Sharpe (not 2x)


def test_offline_cta_ticks_mark_with_supplied_daily_closes(tmp_path):
    import json

    from hl_quant import hl_autopilot as ap

    sp = tmp_path / "hl_spot_perp_paper"
    sp.mkdir()
    (sp / "state.json").write_text(json.dumps({"equity_usd": 100_000.0, "starting_capital_usd": 100_000.0, "positions": {}}))
    down = {c: [100.0 - 0.5 * i for i in range(60)] for c in ap.CTA_UNIVERSE}  # downtrend -> short CTA
    t0 = 1_795_700_000_000
    r1 = ap.run_autopilot_tick(data_root=tmp_path, now_ms=t0, daily_closes=down)
    last = {c: v[-1] for c, v in down.items()}
    moved = {c: v[:-1] + [v[-1] * 0.9] for c, v in down.items()}
    r2 = ap.run_autopilot_tick(data_root=tmp_path, now_ms=t0 + 24 * H_MS, daily_closes=moved)
    marks = r2["state"]["cta_state"]["last_closes"]
    assert all(marks[c] == pytest.approx(last[c] * 0.9) for c in marks)
    assert r1["state"]["cta_state"]["last_closes"]["BTC"] == pytest.approx(last["BTC"])


H_MS = 3_600_000


def test_fdr_p_values_use_student_t_on_few_clusters():
    from hl_quant import hl_funding_settlement_study as fss

    row = {"t_stat": 4.5, "t_stat_clustered": 4.5, "events": 6, "clusters": 3}
    p = fss.conservative_holdout_p_value(row)
    assert 0.01 < p < 0.05  # Student-t, df = 2 (normal tail would give ~3e-6)
    # A lone such cell among 27 tests is not a BH discovery at q = 0.10.
    assert fss.benjamini_hochberg([p] + [0.5] * 26, 0.10)[0] is False
