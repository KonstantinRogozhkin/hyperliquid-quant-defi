from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import hl_hip3_paper as hp


def _mkt(
    dex: str,
    base: str,
    mark: float,
    apr: float,
    *,
    oracle: float | None = None,
    vol: float = 10_000_000.0,
    only_isolated: bool = False,
    growth_mode: bool = True,
) -> hip3.Hip3BuilderMarket:
    ora = oracle if oracle is not None else mark
    return hip3.Hip3BuilderMarket(
        dex=dex,
        dex_full_name=dex,
        full_symbol=f"{dex}:{base}",
        base_symbol=base,
        mark_px=mark,
        oracle_px=ora,
        basis_vs_oracle_bps=round((mark - ora) / ora * 1e4, 2) if ora > 0 else 0.0,
        funding_rate_h=apr / (hip3.HOURS_PER_YEAR * 100.0),
        funding_apr_pct=apr,
        day_vol_usd=vol,
        oi_usd=5_000_000.0,
        only_isolated=only_isolated,
        max_leverage=10,
        growth_mode=growth_mode,
        deployer_fee_scale=1.0,
        est_taker_fee_bps=0.9 if growth_mode else 4.5,
        est_maker_fee_bps=0.3 if growth_mode else 1.5,
    )


def _tight_book(px: float):
    half = px * 0.0001  # 1 bp half-spread, deep size
    return ([(px - half, 500.0)], [(px + half, 500.0)])


def test_hip3_paper_lifecycle_streak_split_margin_funding_and_decay_exit(tmp_path: Path):
    def fake_book(sym: str):
        if "NVDA" in sym:
            return _tight_book(150.0)
        return _tight_book(100.0)

    t0 = 1_795_800_000_000
    cfg = hp.Hip3PaperConfig(
        starting_capital_usd=50_000.0,
        slot_notional_usd=5_000.0,
        per_leg_leverage=2.0,
        min_clean_streak=2,
        entry_apr_pct=20.0,
        exit_apr_pct=6.0,
    )
    mkts_open = [
        _mkt("io", "NVDA", 150.0, 40.0, only_isolated=False, growth_mode=True),
        _mkt("xyz", "NVDA", 150.0, 5.0, only_isolated=False, growth_mode=True),  # +35% spread
    ]

    # Tick 1: streak=1 < 2 -> blocked by clean_streak gate
    r1 = hp.run_hip3_paper_tick(state_dir=tmp_path, cfg=cfg, now_ms=t0, markets=mkts_open, book_fetcher=fake_book)
    assert r1["state"]["positions"] == {}
    assert r1["gated_stats"]["skipped_streak"] == 1

    # Tick 2 (+4h): streak=2 >= 2 -> opens Cross NVDA ($5,000/leg -> $5,000 split margin at 2x)
    r2 = hp.run_hip3_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 4 * 3_600_000, markets=mkts_open, book_fetcher=fake_book
    )
    assert list(r2["state"]["positions"].keys()) == ["NVDA"]
    pos = r2["state"]["positions"]["NVDA"]
    assert pos["short_symbol"] == "io:NVDA"
    assert pos["long_symbol"] == "xyz:NVDA"
    assert pos["leg_notional_usd"] == pytest.approx(5_000.0)
    assert pos["margin_locked_usd"] == pytest.approx(5_000.0)
    assert pos["margin_mode"] == "cross"
    assert pos["both_growth_mode"] is True
    assert pos["entry_fee_bps"] == pytest.approx(1.8)  # 0.9 + 0.9 bps Growth Mode taker
    assert r2["state"]["margin_locked_usd"] == pytest.approx(5_000.0)

    # Tick 3 (+10h hold): accrues 10h of +35% APR funding (~$1.997) with zero basis drift
    r3 = hp.run_hip3_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 14 * 3_600_000, markets=mkts_open, book_fetcher=fake_book
    )
    pos3 = r3["state"]["positions"]["NVDA"]
    expected_10h_fund = 5_000.0 * (35.0 / 100.0) * (10.0 / 8760.0)
    assert pos3["hours_held"] == pytest.approx(10.0)
    assert pos3["cum_funding_usd"] == pytest.approx(expected_10h_fund, abs=0.05)
    assert pos3["cum_basis_mtm_usd"] == pytest.approx(0.0, abs=0.01)

    # Tick 4 (+4h): spread collapses to 3% (< 6% exit_apr_pct) -> closes cleanly!
    mkts_decay = [
        _mkt("io", "NVDA", 150.0, 8.0, only_isolated=False, growth_mode=True),
        _mkt("xyz", "NVDA", 150.0, 5.0, only_isolated=False, growth_mode=True),
    ]
    r4 = hp.run_hip3_paper_tick(
        state_dir=tmp_path, cfg=cfg, now_ms=t0 + 18 * 3_600_000, markets=mkts_decay, book_fetcher=fake_book
    )
    assert r4["state"]["positions"] == {}
    assert r4["state"]["margin_locked_usd"] == pytest.approx(0.0)
    assert r4["state"]["closed_pairs_count"] == 1
    assert r4["trades"][0]["action"] == "CLOSE"
    assert r4["trades"][0]["exit_reason"] == "apr_decay"

    md = hp.format_hip3_paper_markdown(r3)
    rich = hp.format_hip3_paper_telegram_rich_html(r3)
    assert "HIP-3 Cross-Builder Split-Margin Paper Tracker" in md
    assert "NVDA" in md
    assert "<table bordered striped>" in rich


def test_hip3_paper_oracle_guard_isolated_half_sizing_and_external_streak(tmp_path: Path):
    def fake_book(sym: str):
        return _tight_book(100.0)

    t0 = 1_795_900_000_000
    # Seed external frontier persistence summary with clean_streak=3 for MU and clean_streak=2 for SKHX
    frontier_summary = tmp_path / "latest_summary.json"
    frontier_summary.write_text(
        json.dumps(
            {
                "hip3_persistence": [
                    {
                        "base_symbol": "MU",
                        "short_symbol": "io:MU",
                        "long_symbol": "xyz:MU",
                        "active_in_latest_tick": True,
                        "clean_streak": 3,
                        "direction_flips": 0,
                    },
                    {
                        "base_symbol": "SKHX",
                        "short_symbol": "io:SKHX",
                        "long_symbol": "xyz:SKHX",
                        "active_in_latest_tick": True,
                        "clean_streak": 2,
                        "direction_flips": 0,
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    # MU is clean (oracle div 3 bps) & onlyIsolated; SKHX has 30 bps oracle divergence -> MUST be rejected!
    mkts = [
        _mkt("io", "MU", 100.0, 90.0, oracle=100.03, only_isolated=True, growth_mode=True),
        _mkt("xyz", "MU", 100.0, 10.0, oracle=100.00, only_isolated=False, growth_mode=True),
        _mkt("io", "SKHX", 100.0, 180.0, oracle=100.30, only_isolated=False, growth_mode=True),
        _mkt("xyz", "SKHX", 100.0, 10.0, oracle=100.00, only_isolated=False, growth_mode=True),
    ]

    # 1. With allow_only_isolated=True, MU opens immediately on tick 1 via external streak at HALF notional ($2,500)
    r1 = hp.run_hip3_paper_tick(
        state_dir=tmp_path / "run_iso",
        cfg=hp.Hip3PaperConfig(allow_only_isolated=True, slot_notional_usd=5_000.0, isolated_slot_scale=0.5),
        now_ms=t0,
        markets=mkts,
        book_fetcher=fake_book,
        frontier_summary_path=frontier_summary,
    )
    assert list(r1["state"]["positions"].keys()) == ["MU"]
    assert r1["state"]["positions"]["MU"]["leg_notional_usd"] == pytest.approx(2_500.0)
    assert r1["state"]["positions"]["MU"]["margin_locked_usd"] == pytest.approx(2_500.0)
    assert r1["state"]["positions"]["MU"]["margin_mode"] == "isolated"
    assert r1["gated_stats"]["skipped_oracle"] == 1  # SKHX rejected!

    # 2. If MU's builder oracles suddenly diverge by 45 bps (> 35 bps exit threshold) -> emergency exit!
    mkts_oracle_shock = [
        _mkt("io", "MU", 100.0, 90.0, oracle=100.45, only_isolated=True, growth_mode=True),
        _mkt("xyz", "MU", 100.0, 10.0, oracle=100.00, only_isolated=False, growth_mode=True),
    ]
    r2 = hp.run_hip3_paper_tick(
        state_dir=tmp_path / "run_iso",
        cfg=hp.Hip3PaperConfig(allow_only_isolated=True, exit_oracle_div_bps=35.0),
        now_ms=t0 + 4 * 3_600_000,
        markets=mkts_oracle_shock,
        book_fetcher=fake_book,
    )
    assert r2["state"]["positions"] == {}
    assert r2["trades"][0]["action"] == "CLOSE"
    assert r2["trades"][0]["exit_reason"] == "oracle_divergence"

    # 3. With allow_only_isolated=False, MU is skipped cleanly
    r_no_iso = hp.run_hip3_paper_tick(
        state_dir=tmp_path / "run_cross_only",
        cfg=hp.Hip3PaperConfig(allow_only_isolated=False),
        now_ms=t0,
        markets=mkts,
        book_fetcher=fake_book,
        frontier_summary_path=frontier_summary,
    )
    assert r_no_iso["state"]["positions"] == {}
    assert r_no_iso["gated_stats"]["skipped_isolated"] == 1
    assert r_no_iso["gated_stats"]["skipped_oracle"] == 1
