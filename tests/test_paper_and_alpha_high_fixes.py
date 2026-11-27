"""Regression tests for the 2026-11-27 high-severity paper / alpha / ops fixes (audit items 7-11)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from hl_quant import alpha_families as af
from hl_quant import funding_arb as fa
from hl_quant import paper_tracker as pt

H = fa.HOUR_MS
T0 = 1_795_000_000_000 - (1_795_000_000_000 % H)


def _market(n_hours: int, *, hl_fund: dict[int, float] | None = None, bybit_fund: dict[int, float] | None = None) -> af.WideMarket:
    hours = T0 + np.arange(n_hours, dtype="int64") * H
    rows = []
    for coin in ("BTC", "ETH"):
        hl_f = np.zeros(n_hours)
        by_f = np.zeros(n_hours)
        for idx, v in (hl_fund or {}).items():
            if idx < n_hours:
                hl_f[idx] = v
        for idx, v in (bybit_fund or {}).items():
            if idx < n_hours:
                by_f[idx] = v
        px = np.full(n_hours, 100.0)
        cols = {"hour_ms": hours, "coin": coin}
        for venue, fund in (("hl", hl_f), ("bybit", by_f), ("binance", np.zeros(n_hours)), ("okx", np.zeros(n_hours))):
            cols.update(
                {
                    f"{venue}_close": px,
                    f"{venue}_ret": np.zeros(n_hours),
                    f"{venue}_notional_usd": np.full(n_hours, 1e6),
                    f"{venue}_fund_next": fund,
                    f"{venue}_rate_h": fund,
                }
            )
        rows.append(pd.DataFrame(cols))
    return af.build_wide_market(pd.concat(rows, ignore_index=True))


def _quotes(px: float = 100.0) -> dict[str, pt.L2Quote]:
    return {c: pt.L2Quote(c, px - 0.05, px + 0.05, px, 10.0, 1e6, 1e6) for c in ("BTC", "ETH")}


def _targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pt,
        "compute_latest_target_positions",
        lambda m, s: {
            "BTC": {"hl_weight": -0.20, "cex_legs": {"bybit": 0.20}, "cex_weight": 0.20, "cex_venue": "bybit"},
        },
    )


# ----------------------------------------------------------------- 7. metamodel funding window


def test_funding_interval_covers_settlements_held_since_previous_tick():
    hours = T0 + np.arange(24, dtype="int64") * H
    # Previous tick ran at 16:05 (tick_hour 15:00), this one at 20:05 (tick_hour 19:00): the held
    # settlements are 17:00..20:00 -> fund_next indices 16, 17, 18, 19.
    got = pt.funding_interval_hours(hours, int(hours[15]), int(hours[19]))
    assert list(got) == list(hours[16:20])


def test_paper_tick_credits_cex_settlement_to_the_portfolio_that_held_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _targets(monkeypatch)
    # Tick 1 at index 15 (positions opened "16:05"); 8h Bybit settlement paid at 16:00 lives at
    # index 15 (before entry), and the one paid at 20:00 lives at index 19 (held -> must count).
    pt.run_paper_tick(_market(16), _quotes(), state_dir=tmp_path, sleeves=[], cex_mid_prices={"bybit": {"BTC": 100.0}})
    r2 = pt.run_paper_tick(
        _market(20, bybit_fund={15: 1e-3, 19: 2e-3}),
        _quotes(),
        state_dir=tmp_path,
        sleeves=[],
        cex_mid_prices={"bybit": {"BTC": 100.0}},
    )
    # Long 0.20 Bybit leg pays positive funding: -0.20 * 2e-3 of equity (only the 20:00 settlement).
    ticks = pd.read_parquet(tmp_path / "ticks.parquet")
    equity_before = float(ticks.iloc[0]["equity_usd"])
    assert r2["fund_pnl_usd"] == pytest.approx(-0.20 * 2e-3 * equity_before, rel=1e-9)


# ----------------------------------------------------------------- 8. NAV guard confirms instead of freezing


def test_paper_nav_guard_accepts_a_persistent_jump_on_the_next_tick(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _targets(monkeypatch)
    kw = {"state_dir": tmp_path, "sleeves": [], "max_tick_nav_jump_pct": 8.0}
    pt.run_paper_tick(_market(20), _quotes(), cex_mid_prices={"bybit": {"BTC": 100.0}}, **kw)
    # Genuine HL rally +60% vs CEX hedge flat: -0.20 HL short loses 12% of NAV.
    spike = _quotes(160.0)
    r2 = pt.run_paper_tick(_market(21, hl_fund={20: 1e-4}), spike, cex_mid_prices={"bybit": {"BTC": 100.0}}, **kw)
    assert r2["nav_guard_triggered"] is True
    st2 = json.loads((tmp_path / "state.json").read_text())
    assert st2["last_tick_hour_ms"] == T0 + 19 * H  # clock not advanced on a clamp
    assert st2["nav_guard_pending_tick_hour_ms"] == T0 + 20 * H

    # Same hour re-run cannot self-confirm.
    r2b = pt.run_paper_tick(_market(21, hl_fund={20: 1e-4}), spike, cex_mid_prices={"bybit": {"BTC": 100.0}}, **kw)
    assert r2b["nav_guard_triggered"] is True

    # Next hour the move persists -> accepted, including the funding of the clamped hour.
    r3 = pt.run_paper_tick(_market(22, hl_fund={20: 1e-4}), spike, cex_mid_prices={"bybit": {"BTC": 100.0}}, **kw)
    assert r3["nav_guard_triggered"] is False
    assert r3["price_pnl_usd"] < -10_000.0
    assert r3["fund_pnl_usd"] == pytest.approx(0.20 * 1e-4 * 100_000.0, rel=0.01)
    st3 = json.loads((tmp_path / "state.json").read_text())
    assert st3["nav_guard_pending_tick_hour_ms"] is None
    assert st3["last_tick_hour_ms"] == T0 + 21 * H


def test_autopilot_nav_guard_accepts_persistent_sleeve_jump(tmp_path: Path) -> None:
    from hl_quant import hl_autopilot as ap

    sp = tmp_path / "hl_spot_perp_paper"
    sp.mkdir()

    def _sleeve(eq: float) -> None:
        (sp / "state.json").write_text(json.dumps({"equity_usd": eq, "starting_capital_usd": 100_000.0, "positions": {}}))

    _sleeve(100_000.0)
    closes = {c: [100.0] * 50 for c in ap.CTA_UNIVERSE}
    cfg = ap.AutopilotConfig(max_tick_nav_jump_pct=3.0)
    t0 = 1_795_700_000_000
    ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0, daily_closes=closes)
    _sleeve(130_000.0)  # genuine +30% sleeve move (> 3% master NAV guard)
    r2 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 4 * H, daily_closes=closes)
    assert r2["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r2["state"]["nav_guard_deferred_hours"] == pytest.approx(4.0)
    eq2 = r2["state"]["gross_equity_usd"]
    r3 = ap.run_autopilot_tick(data_root=tmp_path, cfg=cfg, now_ms=t0 + 8 * H, daily_closes=closes)
    assert r3["state"]["risk_telemetry"]["nav_guard_triggered"] is False
    assert r3["state"]["gross_equity_usd"] > eq2 * 1.05  # the persistent move is finally credited
    assert r3["state"]["nav_guard_pending_ms"] is None
    assert r3["state"]["nav_guard_deferred_hours"] == 0.0


# ----------------------------------------------------------------- 9. H2 wall alert weights distance


def test_h2_far_wall_needs_distance_weighted_size():
    from hl_quant import hl_liquidation_and_funding_alpha as liq
    from hl_quant import hl_onchain_event_alpha as oea

    def _cluster(coin: str, **bands: float) -> liq.CoinLiquidationCluster:
        vals = {k: bands.get(k, 0.0) for k in (
            "long_liq_0_2_usd", "long_liq_2_5_usd", "long_liq_5_10_usd",
            "short_liq_0_2_usd", "short_liq_2_5_usd", "short_liq_5_10_usd",
        )}
        return liq.CoinLiquidationCluster(
            coin=coin, mark_px=100.0, whales_count=1, total_0_10_usd=sum(vals.values()),
            nearest_long_liq_px=None, nearest_long_dist_pct=9.2 if vals["long_liq_5_10_usd"] else None,
            nearest_short_liq_px=None, nearest_short_dist_pct=None, cascade_bias="downside_cascade", **vals,
        )

    cfg = oea.OnChainEventConfig()
    # Live 2026-11-27 XMR: a single $6.1M long wall 9.2% away (nothing inside 5%) -> weighs $1.8M.
    xmr, huge = oea.extract_h2_liq_events(
        [_cluster("XMR", long_liq_5_10_usd=6_107_108.0), _cluster("BTC", short_liq_5_10_usd=60_800_000.0)],
        ts_ms=0,
        cfg=cfg,
    )[::-1]
    assert xmr["coin"] == "XMR" and xmr["alert"] is False
    assert xmr["weighted_near_usd"] == pytest.approx(6_107_108.0 * liq.BIAS_W_5_10, rel=1e-6)
    assert huge["coin"] == "BTC" and huge["alert"] is True  # $60.8M at 5-10% still weighs $18M


# ----------------------------------------------------------------- 11. cutover never runs two executors


def test_cutover_refuses_dual_or_missing_testnet_executor(tmp_path: Path):
    import importlib.util
    from datetime import datetime, timezone
    import sys

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("cutover_mod", root / "scripts/ops/cutover_local_crons_to_dokploy.py")
    assert spec is not None and spec.loader is not None
    cutover = importlib.util.module_from_spec(spec)
    sys.modules["cutover_mod"] = cutover
    spec.loader.exec_module(cutover)
    sys.path.insert(0, str(root / "tests"))
    try:
        from test_cutover_local_crons import SAMPLE_CRONTAB, _make_fetcher
    finally:
        sys.path.pop(0)

    def run(testnet_enabled: bool, *extra: str) -> tuple[int, str]:
        state = {"crontab": SAMPLE_CRONTAB}
        fetcher, _ = _make_fetcher(testnet_enabled=testnet_enabled, now_utc=datetime.now(timezone.utc))

        def _write(t: str) -> None:
            state["crontab"] = t

        rc = cutover.main(
            ["--public-url", "https://hl.example.com", "--backup-dir", str(tmp_path), "--apply", *extra],
            crontab_reader=lambda: state["crontab"],
            crontab_writer=_write,
            fetcher=fetcher,
        )
        return rc, state["crontab"]

    # Worker executes on Testnet + keep local executor -> two executors -> refused, crontab untouched.
    rc, tab = run(True, "--keep-local-testnet")
    assert rc == 2 and tab == SAMPLE_CRONTAB
    assert run(True, "--keep-local-testnet", "--force-dual-executor")[0] == 0
    # Worker read-only + disabling the local executor -> no executor anywhere -> refused.
    rc, tab = run(False)
    assert rc == 2 and tab == SAMPLE_CRONTAB
    assert run(False, "--allow-no-testnet-executor")[0] == 0
    assert run(False, "--keep-local-testnet")[0] == 0
    # Unverifiable (offline) keep-local is refused too.
    assert run(True, "--keep-local-testnet", "--skip-remote-check")[0] == 2


def test_spot_perp_nav_guard_confirms_and_keeps_funding_clock(tmp_path: Path) -> None:
    from hl_quant import hl_spot_perp_arb as spa

    markets = [
        {
            "base_symbol": "HYPE", "spot_pair": "@107", "spot_token": "HYPE", "quote_token": "USDC",
            "is_proxy_hedge": False, "spot_mid_px": 35.0, "spot_day_vol_usd": 10_000_000.0,
            "perp_mark_px": 20.0, "perp_oracle_px": 20.0, "funding_rate_h": 0.0002,
            "perp_day_vol_usd": 50_000_000.0, "perp_oi_usd": 20_000_000.0, "only_isolated": False, "max_leverage": 5,
        }
    ]

    def fake_book(coin_key: str):
        px = 35.0 if coin_key == "@107" else 20.0
        return [(px * 0.9999, 10_000.0)], [(px * 1.0001, 10_000.0)]

    t0 = 1_795_700_000_000
    state_dir = tmp_path / "sp"
    state_dir.mkdir()
    pos = {
        "base_symbol": "HYPE", "spot_pair": "@107", "qty": 1000.0, "entry_spot_px": 20.0, "entry_perp_px": 20.0,
        "last_spot_px": 20.0, "last_perp_px": 20.0, "spot_notional_usd": 20_000.0, "perp_notional_usd": 20_000.0,
        "perp_margin_usd": 10_000.0, "entry_cost_usd": 6.0, "cum_cost_usd": 6.0, "cum_funding_usd": 0.0,
        "cum_basis_mtm_usd": 0.0, "hours_held": 4.0,
    }
    (state_dir / "state.json").write_text(json.dumps({
        "starting_capital_usd": 100_000.0, "equity_usd": 100_000.0, "hlp_reserve_usd": 10_000.0,
        "free_cash_usd": 60_000.0, "hlp_apr_pct": 12.0, "last_tick_ms": t0, "ticks_count": 1, "positions": {"HYPE": pos},
    }))
    cfg = spa.SpotPerpPaperConfig(max_tick_nav_jump_pct=3.0)
    kw = {"state_dir": state_dir, "cfg": cfg, "markets": markets, "book_fetcher": fake_book, "hlp_summary": (12.0, 250_000_000.0)}
    r1 = spa.run_spot_perp_paper_tick(now_ms=t0 + 4 * H, **kw)
    assert r1["state"]["risk_telemetry"]["nav_guard_triggered"] is True
    assert r1["state"]["last_tick_ms"] == t0  # clock held on a clamp
    assert r1["state"]["nav_guard_pending_ms"] == t0 + 4 * H
    r2 = spa.run_spot_perp_paper_tick(now_ms=t0 + 8 * H, **kw)
    st2 = r2["state"]
    assert st2["risk_telemetry"]["nav_guard_triggered"] is False
    assert st2["nav_guard_pending_ms"] is None
    assert st2["last_tick_ms"] == t0 + 8 * H
    # The persistent +$15k basis move is accepted and 8h (not 4h) of HLP yield accrued.
    assert st2["cum_basis_mtm_usd"] > 10_000.0
    assert st2["cum_hlp_yield_usd"] == pytest.approx(10_000.0 * 0.12 / 8760.0 * 8.0, rel=0.02)
