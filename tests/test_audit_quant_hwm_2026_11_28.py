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
