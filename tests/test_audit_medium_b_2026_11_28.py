"""Regression tests for the 2026-11-28 medium-severity audit batch (B)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hl_quant import hl_analytics_risk as ar
from hl_quant.metrics import render_prometheus_metrics


# --------------------------------------------------------------------------- 6. current drawdown gauge


def test_leaderboard_reports_current_drawdown_separately_from_historical_max() -> None:
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    dates = [(base + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(5)]
    # Deep historical drawdown (-10%) that has fully recovered to a new high
    row = ar.compute_series_performance(
        strategy_id="spot_perp", name="SP", category="x", dates=dates,
        gross_shares=[1.0, 0.9, 0.95, 1.02, 1.03], inception_share=1.0,
    )
    assert row["max_dd_gross_pct"] == 10.0
    assert row["current_dd_gross_pct"] == 0.0
    # Currently underwater by ~2.9% from the 1.03 peak
    row2 = ar.compute_series_performance(
        strategy_id="spot_perp", name="SP", category="x", dates=dates,
        gross_shares=[1.0, 0.9, 1.03, 1.01, 1.0], inception_share=1.0,
    )
    assert row2["current_dd_gross_pct"] == round((1.03 - 1.0) / 1.03 * 100.0, 2)


def test_metrics_emit_current_dd_gauge_from_leaderboard() -> None:
    overview = {
        "spot_perp_paper": {"starting_capital_usd": 100000.0, "equity_usd": 100100.0, "positions": {}},
    }
    analytics = {
        "leaderboard": [{"id": "spot_perp", "max_dd_gross_pct": 12.0, "current_dd_gross_pct": 0.4}],
    }
    text = render_prometheus_metrics(overview, analytics=analytics)
    assert 'hl_strategy_max_dd_pct{strategy="spot_perp_hlp"' in text
    cur = [ln for ln in text.splitlines() if ln.startswith("hl_strategy_current_dd_pct{strategy=\"spot_perp_hlp\"")]
    assert cur and cur[0].endswith(" 0.4")
