from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from hl_quant import cross_dex_scanner as cds
from hl_quant import hl_cross_dex_paper as cdp


def _q(dex: str, coin: str, px: float, apr: float, vol: float = 10_000_000.0) -> cds.DexMarketQuote:
    return cds.DexMarketQuote(
        dex=dex,
        coin=coin,
        raw_symbol=coin,
        mark_px=px,
        funding_rate_h=apr / (cds.HOURS_PER_YEAR * 100.0),
        funding_apr_pct=apr,
        day_vol_usd=vol,
        oi_usd=5_000_000.0,
        spread_bps=1.0,
    )


def test_cross_dex_paper_lifecycle_open_accrue_funding_and_hysteresis_exit(tmp_path: Path):
    def fake_book(dex: str, sym: str):
        if sym == "ETH":
            return ([(2000.0, 20.0)], [(2000.4, 20.0)])  # ~1 bp half-spread, deep book
        return ([(10.0, 100.0)], [(10.5, 100.0)])  # wide illiquid book -> should be rejected!

    t0 = 1_795_700_000_000
    cfg = cdp.CrossDexPaperConfig(
        starting_capital_usd=50_000.0,
        slot_notional_usd=5_000.0,
        entry_apr_pct=15.0,
        exit_apr_pct=5.0,
        min_persistence_ticks=1,
    )
    with httpx.Client() as client:
        res1 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0,
            hl_quotes={"ETH": _q("hyperliquid", "ETH", 2000.0, 15.0), "THIN": _q("hyperliquid", "THIN", 10.2, 30.0)},
            lighter_quotes={"ETH": _q("lighter", "ETH", 2000.0, -10.0), "THIN": _q("lighter", "THIN", 10.2, 0.0)},
            book_fetcher=fake_book,
        )
        assert list(res1["state"]["positions"].keys()) == ["ETH"]
        assert res1["state"]["positions"]["ETH"]["hl_sign"] == -1.0  # Short HL / Long Lighter

        res2 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0 + 10 * 3_600_000,
            hl_quotes={"ETH": _q("hyperliquid", "ETH", 2020.0, 15.0)},
            lighter_quotes={"ETH": _q("lighter", "ETH", 2020.0, -10.0)},
            book_fetcher=fake_book,
        )
        assert "ETH" in res2["state"]["positions"]
        assert res2["state"]["positions"]["ETH"]["hours_held"] == pytest.approx(10.0)
        expected_10h_fund = 5_000.0 * (25.0 / 100.0) * (10.0 / 8760.0)
        assert res2["state"]["positions"]["ETH"]["cum_funding_usd"] == pytest.approx(expected_10h_fund, abs=0.05)
        assert res2["state"]["positions"]["ETH"]["cum_basis_mtm_usd"] == pytest.approx(0.0, abs=0.01)

        res3 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0 + 14 * 3_600_000,
            hl_quotes={"ETH": _q("hyperliquid", "ETH", 2020.0, 2.0)},
            lighter_quotes={"ETH": _q("lighter", "ETH", 2020.0, 0.0)},
            book_fetcher=fake_book,
        )
        assert res3["state"]["positions"] == {}
        assert res3["state"]["closed_pairs_count"] == 1
        assert res3["trades"][0]["action"] == "CLOSE"

    md = cdp.format_cross_dex_paper_markdown(res2)
    rich = cdp.format_cross_dex_paper_telegram_rich_html(res2)
    assert "Hyperliquid × Lighter × Extended" in md
    assert '<table bordered striped>' in rich


def test_cross_dex_paper_v2_persistence_gate_tight_l2_and_h1_basis_carry_combo(tmp_path: Path):
    def fake_book(dex: str, sym: str):
        if sym == "SOL":
            return ([(100.0, 200.0)], [(100.02, 200.0)])  # tight 2bp spread
        if sym == "PUMP":
            # Favorable +15 bps basis for Long HL (ask 1.0000) / Short Extended (bid 1.0015)!
            if dex == "hyperliquid":
                return ([(0.9999, 20_000.0)], [(1.0000, 20_000.0)])
            return ([(1.0015, 20_000.0)], [(1.0016, 20_000.0)])
        return ([(10.0, 1000.0)], [(10.12, 1000.0)])  # ~120 bp slip -> rejected by 5 bp cap!

    t0 = 1_795_800_000_000
    cfg = cdp.CrossDexPaperConfig(
        max_l2_slip_bps=5.0,
        min_persistence_ticks=2,
        enable_extended=True,
        enable_basis_carry_combo=True,
        combo_min_exec_basis_bps=8.0,
        combo_min_apr_pct=8.0,
    )
    with httpx.Client() as client:
        # Tick 1: SOL has 20% carry on Lighter (streak=1 < 2 -> held back by Persistence Gate!),
        # PUMP has 11% carry + +15 bps L2 exec basis on Extended -> opens IMMEDIATELY via H1 Combo!
        r1 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0,
            hl_quotes={
                "SOL": _q("hyperliquid", "SOL", 100.0, -20.0),
                "PUMP": _q("hyperliquid", "PUMP", 1.0000, -11.0),
                "XMR": _q("hyperliquid", "XMR", 10.06, -50.0),
            },
            lighter_quotes={
                "SOL": _q("lighter", "SOL", 100.0, 0.0),
                "XMR": _q("lighter", "XMR", 10.06, 0.0),
            },
            extended_quotes={
                "PUMP": _q("extended", "PUMP", 1.0015, 0.0),
            },
            book_fetcher=fake_book,
        )
        assert "SOL" not in r1["state"]["positions"]  # blocked on tick 1 by Persistence Gate!
        assert "XMR" not in r1["state"]["positions"]
        assert "PUMP" in r1["state"]["positions"]  # opened via H1 Basis+Carry Combo on Extended!
        assert r1["state"]["positions"]["PUMP"]["other_dex"] == "extended"
        assert r1["state"]["positions"]["PUMP"]["entry_type"] == "basis_carry_combo"

        # Tick 2: SOL persists at 20% APR (streak=2 >= 2) -> now opens! XMR still blocked by L2 slip > 5 bps!
        r2 = cdp.run_cross_dex_paper_tick(
            client,
            state_dir=tmp_path,
            cfg=cfg,
            now_ms=t0 + 4 * 3_600_000,
            hl_quotes={
                "SOL": _q("hyperliquid", "SOL", 100.0, -20.0),
                "PUMP": _q("hyperliquid", "PUMP", 1.0010, -11.0),
                "XMR": _q("hyperliquid", "XMR", 10.06, -50.0),
            },
            lighter_quotes={
                "SOL": _q("lighter", "SOL", 100.0, 0.0),
                "XMR": _q("lighter", "XMR", 10.06, 0.0),
            },
            extended_quotes={
                "PUMP": _q("extended", "PUMP", 1.0015, 0.0),
            },
            book_fetcher=fake_book,
        )
        assert "SOL" in r2["state"]["positions"]
        assert r2["state"]["positions"]["SOL"]["entry_type"] == "carry_persistent"
        assert "XMR" not in r2["state"]["positions"]
        assert r2["gated_stats"]["skipped_l2_slip"] >= 1
