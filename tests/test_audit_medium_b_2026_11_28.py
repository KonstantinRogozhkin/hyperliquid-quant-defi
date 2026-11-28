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


# --------------------------------------------------------------------------- 13-16. analytics


def _dts(days: list[str]) -> list[datetime]:
    return [datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc) for d in days]


def test_ytd_base_is_last_close_strictly_before_jan_1_and_matches_monthly_table() -> None:
    dates = _dts(["2025-12-30", "2025-12-31", "2026-01-01", "2026-01-02"])
    shares = [1.0, 1.1, 1.21, 1.331]
    ytd, _span = ar._window_return(dates, shares, ytd=True)
    assert ytd is not None
    assert round(ytd, 2) == round((1.331 / 1.1 - 1.0) * 100.0, 2)  # Dec-31 close, not the Jan-1 close
    monthly = ar.compute_monthly_returns(dates, shares)
    row_2026 = next(r for r in monthly if r["year"] == 2026)
    assert row_2026["ytd"] == round(ytd, 2)
    # Un-anchored series starting exactly on Jan 1 still has a YTD (based on its first close), like the table
    ytd_jan1, _ = ar._window_return(_dts(["2026-01-01", "2026-01-02"]), [1.0, 1.05], ytd=True)
    assert ytd_jan1 is not None and round(ytd_jan1, 2) == 5.0
    # Starting after Jan 1 without an inception anchor -> no YTD
    assert ar._window_return(_dts(["2026-01-05", "2026-01-06"]), [1.0, 1.05], ytd=True)[0] is None


def test_correlation_none_for_constant_series_and_min_overlap_10() -> None:
    import pandas as pd

    idx = pd.date_range("2026-09-01", periods=15, freq="D").strftime("%Y-%m-%d")
    rising = pd.Series([1.0 * (1.01 ** i) * (1.0 + 0.003 * ((-1) ** i)) for i in range(15)], index=idx)
    flat = pd.Series([1.0] * 15, index=idx)
    other = pd.Series([1.0 * (1.02 ** i) * (1.0 - 0.002 * ((-1) ** i)) for i in range(15)], index=idx)
    corr = ar.compute_correlation_matrix({"a": rising, "flat": flat, "b": other})
    assert corr["matrix"][1][0] is None  # constant series -> undefined, not 0.0
    assert isinstance(corr["matrix"][2][0], float)

    short = ar.compute_correlation_matrix({"a": rising.iloc[:6], "b": other.iloc[:6]})
    assert short["matrix"][1][0] is None and short["overlap_days"] == 5
    ols = ar.compute_ols_market_exposure({"a": ("A", rising.iloc[:6])}, other.iloc[:6], factor_name="BTC")
    assert ols["rows"][0]["beta"] is None and ols["rows"][0]["days"] == 5
    assert ar.MIN_STAT_OVERLAP_DAYS == 10


def test_risk_exposure_leverage_rows_carry_leaderboard_ids_and_names(tmp_path: Path) -> None:
    import json

    for sub, payload in (
        ("hl_hip3_paper", {"equity_usd": 50_000.0, "positions": {}}),
        ("hl_metamodel", {"equity_usd": 100_000.0, "positions": {}}),
        ("hl_cross_dex_paper", {"equity_usd": 50_000.0, "positions": {}}),
    ):
        (tmp_path / sub).mkdir(parents=True)
        (tmp_path / sub / "state.json").write_text(json.dumps(payload), encoding="utf-8")
    rows = ar.build_consolidated_risk_exposure(tmp_path)["leverage_by_strategy"]
    by_id = {r["id"]: r["strategy"] for r in rows}
    assert by_id == {k: ar.STRATEGY_NAMES[k] for k in ("cross_dex", "hip3_paper", "metamodel")}
    assert by_id["hip3_paper"] == "HIP-3 Growth Split-Margin"
    assert by_id["metamodel"] == "6-Family Metamodel (Obs)"


def test_monthly_returns_flag_partial_first_month() -> None:
    mid = ar.compute_monthly_returns(_dts(["2026-09-15", "2026-09-30", "2026-10-31"]), [1.0, 1.01, 1.02])
    assert mid[0]["partial_first_month"] == "9"
    assert set(mid[0]["months"]) == {"9", "10"}  # values still reported (backward compatible)
    full = ar.compute_monthly_returns(_dts(["2026-09-01", "2026-09-30", "2026-10-31"]), [1.0, 1.01, 1.02])
    assert full[0]["partial_first_month"] is None
    multi = ar.compute_monthly_returns(_dts(["2025-12-10", "2025-12-31", "2026-01-31"]), [1.0, 1.01, 1.02])
    by_year = {r["year"]: r for r in multi}
    assert by_year[2025]["partial_first_month"] == "12"
    assert by_year[2026]["partial_first_month"] is None


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


# --------------------------------------------------------------------------- 9-11. API healthz / audit / metrics


def test_healthz_reports_degraded_when_data_root_missing(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from hl_quant.api.app import create_app

    client = TestClient(create_app(data_root=tmp_path / "missing", build_sha="abc"))
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json()["status"] == "degraded"
    assert r.json()["data_root_exists"] is False
    assert TestClient(create_app(data_root=tmp_path, build_sha="abc")).get("/healthz").json()["status"] == "ok"


def test_audit_record_orphan_positions_none_when_field_absent() -> None:
    from hl_quant.api.app import _audit_record

    assert _audit_record({"ts_ms": 1, "status": "ok"})["orphan_positions"] is None
    assert _audit_record({"ts_ms": 1, "status": "ok", "orphan_positions": {}})["orphan_positions"] == {}
    assert _audit_record({"ts_ms": 1, "orphan_positions": {"SKY": -975.0}})["orphan_positions"] == {"SKY": -975.0}


def test_metrics_survive_analytics_failure_and_export_error_gauge(tmp_path: Path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from hl_quant.api import app as api_app

    (tmp_path / "hl_worker_status.json").write_text(
        '{"service": "hl-worker", "heartbeat_utc": "2026-11-27 14:00:00 UTC", "jobs": []}', encoding="utf-8"
    )
    client = TestClient(api_app.create_app(data_root=tmp_path, build_sha="abc"))
    ok = client.get("/metrics")
    assert ok.status_code == 200
    assert "hl_metrics_render_errors 0" in ok.text

    def _boom(_root: Path) -> dict:
        raise RuntimeError("analytics exploded")

    monkeypatch.setattr(api_app, "build_desk_analytics_payload", _boom)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "hl_metrics_render_errors 1" in r.text
    assert "hl_worker_up" in r.text
    assert "hl_artifact_exists" in r.text


# --------------------------------------------------------------------------- 12. cutover requires testnet jobs


def _load_cutover():
    import importlib.util

    spec = importlib.util.spec_from_file_location("cutover_b", ROOT / "scripts/ops/cutover_local_crons_to_dokploy.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    import sys

    sys.modules["cutover_b"] = mod  # dataclasses resolve their module via sys.modules
    spec.loader.exec_module(mod)
    return mod


def test_cutover_requires_testnet_executor_and_risk_probe_when_testnet_enabled() -> None:
    cutover = _load_cutover()
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    hb = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S UTC")

    def fetcher_for(testnet_jobs: list[dict], testnet_enabled: bool = True):
        jobs = [{"name": n, "enabled": True, "state": "ok"} for n in cutover.DEFAULT_REQUIRED_JOBS] + testnet_jobs

        def _fetch(url: str) -> dict:
            if url.endswith("/api/v1/healthz"):
                return {"status": "ok"}
            if url.endswith("build-info.json"):
                return {"sha": "a" * 40}
            return {
                "worker_status": {
                    "build_sha": "a" * 40,
                    "heartbeat_utc": hb,
                    "testnet_execution_enabled": testnet_enabled,
                    "jobs": jobs,
                }
            }

        return _fetch

    ex_ok = {"name": "testnet_executor", "enabled": True, "state": "ok"}
    rp_ok = {"name": "testnet_risk_probe", "enabled": True, "state": "ok"}
    res = cutover.verify_dokploy_stack("https://hl.example.com", fetcher=fetcher_for([ex_ok, rp_ok]), now_utc=now)
    assert res.testnet_execution_enabled is True

    with pytest.raises(RuntimeError, match="testnet_risk_probe' is missing"):
        cutover.verify_dokploy_stack("https://hl.example.com", fetcher=fetcher_for([ex_ok]), now_utc=now)
    with pytest.raises(RuntimeError, match="testnet_executor' is missing"):
        cutover.verify_dokploy_stack("https://hl.example.com", fetcher=fetcher_for([rp_ok]), now_utc=now)
    with pytest.raises(RuntimeError, match="testnet_risk_probe disabled"):
        cutover.verify_dokploy_stack(
            "https://hl.example.com",
            fetcher=fetcher_for([ex_ok, {"name": "testnet_risk_probe", "enabled": False, "state": "disabled"}]),
            now_utc=now,
        )
    with pytest.raises(RuntimeError, match="testnet_risk_probe' is in failed state"):
        cutover.verify_dokploy_stack(
            "https://hl.example.com",
            fetcher=fetcher_for([ex_ok, {"name": "testnet_risk_probe", "enabled": True, "state": "failed"}]),
            now_utc=now,
        )
    # Read-only worker: testnet jobs are not required.
    res_ro = cutover.verify_dokploy_stack(
        "https://hl.example.com", fetcher=fetcher_for([], testnet_enabled=False), now_utc=now
    )
    assert res_ro.testnet_execution_enabled is False
