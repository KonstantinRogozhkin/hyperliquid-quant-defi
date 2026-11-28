from __future__ import annotations

import dataclasses

import pytest

from hl_quant import hl_hyperevm_yield_stack as hys


def test_hyperevm_delta_neutral_yield_stack_math_and_reporting():
    inp = hys.HyperEvmYieldInputs(
        observed_at_ms=1_795_800_000_000,
        hype_mark_px=35.0,
        hype_funding_apr_pct=10.95,
        boros_hype_market_name="Hyperliquid HYPE 25 Dec 2026",
        boros_hype_fixed_bid_apr_pct=8.94,
        boros_hype_days_to_maturity=28.2,
        khype_apy_pct=1.88,
        khype_tvl_usd=554_700_000.0,
        sthype_apy_pct=1.99,
        sthype_tvl_usd=135_600_000.0,
        morpho_feusd_apy_pct=5.20,
        morpho_feusd_tvl_usd=13_400_000.0,
        hyperlend_khype_tvl_usd=158_500_000.0,
        felix_whype_tvl_usd=15_900_000.0,
        liminal_xhype_apy_pct=9.07,
        hlp_apr_pct=15.68,
        hlp_tvl_usd=152_600_000.0,
    )
    strats = hys.build_hyperevm_yield_strategies(inp, perp_leverage=2.0, loop_ltv=0.50, assumed_stable_borrow_apr_pct=3.5)
    by_name = {s.name: s for s in strats}

    # Check fixed-rate Boros delta-neutral math: (1.88 + 8.94) * (2/3) = 7.21% net on total deposit
    fix = next(s for s in strats if s.name.startswith("kHYPE + Short HYPE + Boros Fix"))
    assert fix.gross_stacked_apr_pct == pytest.approx(10.82)
    assert fix.capital_efficiency_pct == pytest.approx(66.7, abs=0.1)
    assert fix.net_deposit_apr_pct == pytest.approx(7.21, abs=0.05)

    # Composable loop: borrow cost is floored by the live stable supply APY / utilization
    # (5.20 / 0.80 = 6.50% > the 3.5% assumption): 1.88 + 8.94 - 0.50 * 6.50 = 7.57% net.
    loop = next(s for s in strats if s.name.startswith("Composable Loop:"))
    assert loop.capital_efficiency_pct == pytest.approx(100.0)
    assert loop.net_deposit_apr_pct == pytest.approx(7.57, abs=0.05)
    # With no live supply rate the explicit assumption still applies.
    no_supply = hys.build_hyperevm_yield_strategies(
        dataclasses.replace(inp, morpho_feusd_apy_pct=0.0),
        perp_leverage=2.0, loop_ltv=0.50, assumed_stable_borrow_apr_pct=3.5,
    )
    loop0 = next(s for s in no_supply if s.name.startswith("Composable Loop:"))
    assert loop0.net_deposit_apr_pct == pytest.approx(9.07, abs=0.05)

    assert "Benchmark: HLP Vault (Hyperliquidity Provider)" in by_name
    snap = hys.HyperEvmYieldSnapshot(inputs=inp, strategies=strats)
    md = hys.format_yield_stack_markdown(snap)
    rich = hys.format_yield_stack_telegram_rich_html(snap)
    assert "kHYPE" in md
    assert "Boros Fix" in md
    assert '<table bordered striped>' in rich
