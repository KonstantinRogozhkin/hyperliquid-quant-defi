"""Regression tests for the 2026-11-28 medium-severity audit batch (B)."""

from __future__ import annotations

import os
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from hl_quant import hl_analytics_risk as ar
from hl_quant.metrics import render_prometheus_metrics

ROOT = Path(__file__).resolve().parents[1]


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


# --------------------------------------------------------------------------- 7. installers vs Dokploy cutover

_INSTALLERS = sorted((ROOT / "scripts/ops").glob("install_local_*.sh")) + [ROOT / "scripts/ops/install_all_hl_crons.sh"]


def _fake_crontab(tmp_path: Path, content: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    store = tmp_path / "cron_store.txt"
    store.write_text(content, encoding="utf-8")
    fake = fake_bin / "crontab"
    fake.write_text(
        f"#!/bin/sh\nif [ \"$1\" = \"-l\" ]; then cat \"{store}\"; else cp \"$1\" \"{store}\"; fi\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    env.pop("HL_FORCE_LOCAL_CRONS", None)
    return env, store


def test_installers_have_cutover_guard() -> None:
    assert len(_INSTALLERS) == 8
    for script in _INSTALLERS:
        text = script.read_text(encoding="utf-8")
        assert "(cutover to Dokploy worker)" in text, script.name
        assert "HL_FORCE_LOCAL_CRONS" in text, script.name


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh not available")
def test_installers_refuse_to_reactivate_cutover_blocks_unless_forced(tmp_path: Path) -> None:
    cutover_cron = (
        "0 * * * * /usr/local/bin/unrelated.sh\n"
        "# BEGIN hyperliquid-quant-defi cross-dex paper schedule\n"
        "# Disabled 2026-11-27 (cutover to Dokploy worker): 15 */4 * * * zsh /repo/scripts/ops/run_hl_cross_dex_paper_tick.sh\n"
        "# END hyperliquid-quant-defi cross-dex paper schedule\n"
    )
    for script in _INSTALLERS:
        env, store = _fake_crontab(tmp_path, cutover_cron)
        proc = subprocess.run(["zsh", str(script)], env=env, capture_output=True, text=True)
        assert proc.returncode == 3, (script.name, proc.stdout, proc.stderr)
        assert "HL_FORCE_LOCAL_CRONS=1" in proc.stderr
        assert store.read_text(encoding="utf-8") == cutover_cron, script.name

    env, store = _fake_crontab(tmp_path, cutover_cron)
    env["HL_FORCE_LOCAL_CRONS"] = "1"
    proc = subprocess.run(
        ["zsh", str(ROOT / "scripts/ops/install_local_hl_cross_dex_cron.sh")],
        env=env, capture_output=True, text=True, check=True,
    )
    assert "installed_crontab=" in proc.stdout
    installed = store.read_text(encoding="utf-8")
    assert "unrelated.sh" in installed
    assert "(cutover to Dokploy worker)" not in installed


# --------------------------------------------------------------------------- 8. stable repo-local lock dir


def test_run_scripts_use_repo_local_lock_dir_not_tmpdir() -> None:
    scripts = [p for p in sorted((ROOT / "scripts/ops").glob("run_*.sh")) if "ops_lock" in p.read_text(encoding="utf-8")]
    assert len(scripts) >= 9
    for p in scripts:
        text = p.read_text(encoding="utf-8")
        assert "TMPDIR" not in text, p.name
        assert "/.neira/state/locks/" in text, p.name
