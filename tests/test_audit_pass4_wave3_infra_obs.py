"""Pass-4 Wave 3 regression tests for Analytics, API/DoS, Nginx, Metrics, Alerts, Scheduler, Ops & CI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import inspect
import json
import math
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

from fastapi.testclient import TestClient
import numpy as np
import pandas as pd
import pytest
from typer.testing import CliRunner
import yaml

from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_portfolio_dashboard as pdash
from hl_quant.api import app as api_app
from hl_quant.metrics import render_prometheus_metrics
from hl_quant.scheduler import WorkerScheduler, app as scheduler_cli, build_job_specs

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- 1. LIVE-03 (Analytics Streaks & Benchmark Alignment)


def test_live03_streaks_and_sharpe_exclude_synthetic_pre_inception_day():
    # Strategy has 2 real UTC calendar days (elapsed_days = 1.0): Day 1 ends at 0.9994 after entry
    # fees, Day 2 rises to 0.9998 (still below inception 1.0).
    # Real observed day-over-day return is POSITIVE (+0.04%), never a 2-day loss streak!
    row_up = ar.compute_series_performance(
        strategy_id="spot_perp",
        name="SP",
        category="Market Neutral",
        dates=["2026-11-27T12:00:00Z", "2026-11-28T12:00:00Z"],
        gross_shares=[0.9994, 0.9998],
        inception_share=1.0,
    )
    assert row_up["elapsed_days"] == pytest.approx(1.0)
    assert row_up["days_tracked"] == 2
    assert row_up["longest_loss_streak"] == 0
    assert row_up["longest_win_streak"] == 1
    assert row_up["positive_days_pct"] == 100.0
    # Meanwhile all-time return and drawdown remain anchored to inception_share = 1.0
    assert (row_up["gross_returns"]["all_time"] or 0.0) < 0.0
    assert row_up["max_dd_gross_pct"] > 0.0

    # If Day 2 drops below Day 1 (0.9994 -> 0.9990), longest_loss_streak is 1 (1 real transition), never 2!
    row_down = ar.compute_series_performance(
        strategy_id="spot_perp",
        name="SP",
        category="Market Neutral",
        dates=["2026-11-27T12:00:00Z", "2026-11-28T12:00:00Z"],
        gross_shares=[0.9994, 0.9990],
        inception_share=1.0,
    )
    assert row_down["longest_loss_streak"] == 1
    assert row_down["longest_win_streak"] == 0

    # 5 calendar days where Day 1 starts at 0.95 (huge initial entry dip) then rises +0.10%/day steadily:
    # Sharpe must reflect only the 4 real steady daily gains, not a synthetic -5% day-0 crash.
    steady_dates = [f"2026-11-{20 + i:02d}" for i in range(5)]
    steady_shares = [0.95 * ((1.001) ** i) for i in range(5)]
    row_5d = ar.compute_series_performance(
        strategy_id="spot_perp",
        name="SP",
        category="Market Neutral",
        dates=steady_dates,
        gross_shares=steady_shares,
        inception_share=1.0,
    )
    assert row_5d["longest_loss_streak"] == 0
    assert row_5d["longest_win_streak"] == 4


def test_live03_benchmarks_extend_to_latest_strategy_day_from_autopilot_live_marks(tmp_path: Path):
    base_dt = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)
    strat_days = [base_dt + timedelta(days=i) for i in range(15)]  # 2026-11-14 .. 2026-11-28 (15 days)
    bench_days = strat_days[:-1]  # daily_closes.json ends yesterday (2026-11-27)

    rng = np.random.default_rng(11)
    btc_px = (90_000.0 * np.exp(np.cumsum(rng.normal(0.002, 0.02, size=len(bench_days))))).tolist()
    live_btc_today = btc_px[-1] * 1.03  # +3% move on 2026-11-28

    (tmp_path / "hl_benchmarks").mkdir()
    (tmp_path / "hl_benchmarks" / "daily_closes.json").write_text(
        json.dumps(
            {
                "updated_at_utc": "2026-11-28 12:00 UTC",
                "benchmarks": {
                    "BTC": [
                        {"date": d.strftime("%Y-%m-%d"), "close": px}
                        for d, px in zip(bench_days, btc_px, strict=False)
                    ]
                },
            }
        ),
        encoding="utf-8",
    )

    ap_dir = tmp_path / "hl_autopilot"
    ap_dir.mkdir()
    ap_eq = [250_000.0 * ((1.0005) ** i) for i in range(len(strat_days))]
    (ap_dir / "state.json").write_text(
        json.dumps(
            {
                "starting_capital_usd": 250_000.0,
                "gross_equity_usd": ap_eq[-1],
                "equity_usd": ap_eq[-1],
                "last_tick_ms": int(strat_days[-1].timestamp() * 1000),
                "cta_state": {"assets": {"BTC": {"mark_px": live_btc_today, "last_close": btc_px[-1]}}},
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame(
        {"tick_ms": [int(d.timestamp() * 1000) for d in strat_days], "equity_usd": ap_eq}
    ).to_parquet(ap_dir / "ticks.parquet", index=False)

    payload = ar.build_desk_analytics_payload(tmp_path)
    assert payload["curves"]["autopilot"]["dates"][-1] == "2026-11-28"
    assert payload["curves"]["bench_btc"]["dates"][-1] == "2026-11-28"
    btc_lb = next(r for r in payload["leaderboard"] if r["id"] == "bench_btc")
    assert btc_lb["spot_price_usd"] == pytest.approx(round(live_btc_today, 4))
    ols_row = payload["market_exposure"]["BTC"]["rows"][0]
    assert ols_row["scatter"][-1]["date"] == "2026-11-28"


# --------------------------------------------------------------------------- 2. LIVE-04 & N-1 (API Auth, Docs Gate, O(1) Tail, Cache, Nginx)


def test_live04_metrics_optional_bearer_auth_and_docs_disabled_by_default(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_METRICS_BEARER_TOKEN", raising=False)
    monkeypatch.delenv("HL_ENABLE_API_DOCS", raising=False)

    client_default = TestClient(api_app.create_app(data_root=tmp_path, build_sha="sha1"))
    assert client_default.get("/api/docs").status_code == 404
    assert client_default.get("/api/openapi.json").status_code == 404
    assert client_default.get("/metrics").status_code == 200
    assert client_default.get("/api/v1/metrics").status_code == 200

    # Enable bearer token on /metrics & /api/v1/metrics
    monkeypatch.setenv("HL_METRICS_BEARER_TOKEN", "prom-secret-token")
    for path in ("/metrics", "/api/v1/metrics"):
        r_unauth = client_default.get(path)
        assert r_unauth.status_code == 401
        assert r_unauth.headers.get("www-authenticate") == "Bearer"
        r_wrong = client_default.get(path, headers={"Authorization": "Bearer wrong"})
        assert r_wrong.status_code == 401
        r_ok = client_default.get(path, headers={"Authorization": "Bearer prom-secret-token"})
        assert r_ok.status_code == 200
        assert "hl_build_info" in r_ok.text

    # Enable OpenAPI docs explicitly
    monkeypatch.setenv("HL_ENABLE_API_DOCS", "true")
    client_docs = TestClient(api_app.create_app(data_root=tmp_path, build_sha="sha1"))
    assert client_docs.get("/api/docs").status_code == 200
    assert client_docs.get("/api/openapi.json").status_code == 200


def test_n1_audit_tail_reverse_block_seek_and_app_fingerprint_cache(tmp_path: Path, monkeypatch):
    audit_path = tmp_path / "hl_testnet_audit.jsonl"
    # Write 12,000 JSONL lines (~1.6 MB > 25x 64KB blocks) plus a trailing corrupt line
    with audit_path.open("w", encoding="utf-8") as fh:
        for idx in range(12_000):
            fh.write(
                json.dumps(
                    {
                        "ts_ms": 1_795_000_000_000 + idx * 1000,
                        "mode": "testnet_live",
                        "status": "ok",
                        "placed_count": idx % 10,
                        "violations": [f"note-{idx}-б.п."],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        fh.write("{corrupt-trailing-line\n\n")

    assert audit_path.stat().st_size > 1_000_000
    tail = api_app._read_testnet_audit_tail(tmp_path, limit=15)
    assert len(tail) == 15
    assert tail[0]["ts_ms"] == 1_795_000_000_000 + 11_999 * 1000
    assert tail[-1]["ts_ms"] == 1_795_000_000_000 + 11_985 * 1000

    # Verify 2s + mtime-fingerprint cache avoids recomputing unchanged payloads, yet invalidates immediately on write
    calls = {"overview": 0, "analytics": 0}
    orig_collect = api_app.collect_dashboard_payload
    orig_analytics = api_app.build_desk_analytics_payload

    def counting_collect(root: Path) -> dict[str, Any]:
        calls["overview"] += 1
        return orig_collect(root)

    def counting_analytics(root: Path) -> dict[str, Any]:
        calls["analytics"] += 1
        return orig_analytics(root)

    monkeypatch.setattr(api_app, "collect_dashboard_payload", counting_collect)
    monkeypatch.setattr(api_app, "build_desk_analytics_payload", counting_analytics)
    monkeypatch.setattr(api_app.time, "monotonic", lambda: 100.1)

    client = TestClient(api_app.create_app(data_root=tmp_path, build_sha="cachetest"))
    assert client.get("/api/v1/overview").status_code == 200
    assert client.get("/api/v1/overview").status_code == 200
    assert client.get("/api/v1/analytics").status_code == 200
    assert client.get("/api/v1/analytics").status_code == 200
    assert calls == {"overview": 1, "analytics": 1}

    # Touching a watched artifact invalidates immediately within the same 2s monotonic bucket
    (tmp_path / "hl_worker_status.json").write_text(
        json.dumps({"service": "hl-worker", "heartbeat_utc": "2026-11-28 12:00:00 UTC", "jobs": []}),
        encoding="utf-8",
    )
    r_after = client.get("/api/v1/overview")
    assert r_after.status_code == 200
    assert r_after.json()["worker_status"]["service"] == "hl-worker"
    assert calls["overview"] == 2


def test_n1_nginx_rate_limits_and_csp_permissions_headers():
    nginx_conf = (ROOT / "docker/nginx.conf").read_text(encoding="utf-8")
    assert "listen 80;" in nginx_conf
    assert "limit_req_zone $binary_remote_addr zone=hl_api:10m rate=10r/s;" in nginx_conf
    for proxied_loc in ("location = /metrics", "location /api/"):
        _, _, after = nginx_conf.partition(proxied_loc)
        block = after.split("}", 1)[0]
        assert "limit_req zone=hl_api burst=30 nodelay;" in block, proxied_loc
    for loc in (
        "location = /nginx-health",
        "location = /healthz",
        "location = /metrics",
        "location = /build-info.json",
        "location = /index.html",
        "location /assets/",
        "location /api/",
        "location /",
    ):
        _, _, after = nginx_conf.partition(loc)
        block = after.split("}", 1)[0]
        assert "add_header Content-Security-Policy" in block, loc
        assert "add_header Permissions-Policy" in block, loc


# --------------------------------------------------------------------------- 3. LIVE-01 & OBS-01..OBS-06 (Metrics, Grafana & Alerts)


def test_live01_and_obs01_obs02_obs05_prometheus_metrics():
    overview = {
        "worker_status": {
            "heartbeat_utc": "2026-11-28 14:00:00 UTC",
            "telegram_enabled": True,
            "testnet_execution_enabled": False,
            "jobs": [
                {"name": "spot_perp_paper", "enabled": True, "state": "ok", "runs_ok": 5, "runs_failed": 0},
                {"name": "cross_dex_paper", "enabled": True, "state": "running", "runs_ok": 4, "runs_failed": 0},
                {"name": "hip3_paper", "enabled": True, "state": "idle", "runs_ok": 0, "runs_failed": 0},
                {"name": "autopilot_allocator", "enabled": True, "state": "interrupted", "runs_ok": 3, "runs_failed": 0},
                {"name": "testnet_risk_probe", "enabled": False, "state": "disabled", "runs_ok": 0, "runs_failed": 0},
            ],
        },
        "attribution": {
            "observed_at_ms": 1_795_800_000_000,
            "equity_usd": 948.5,
            "withdrawable_usd": 948.5,
            "deposited_capital_usd": 1_000.0,
            "liq_warnings": [],
            "adl_warnings": [],
        },
        "testnet_audit_recent": [
            {
                "ts_ms": 1_795_800_100_000,
                "mode": "testnet_live",
                "status": "kill_switch_triggered",
                "derisk_mode": "flatten",
                "violations": ["drawdown_limit:5.15%>5.00%"],
                "high_water_mark_usd": 1_000.0,
                "placed_count": 0,
            }
        ],
        "testnet_risk": {
            "observed_at_ms": 1_795_800_900_000,  # newer than flatten audit
            "equity_usd": 948.5,
            "high_water_mark_usd": 948.5,  # re-anchored after flatten cooldown
            "gross_leverage": 0.0,
            "unmanaged_orphan_positions": 0,
            "drawdown_pct": 6.2,  #Wait: test < kill_switch below!
            "kill_switch_drawdown_pct": 5.0,
        },
        "hip3_paper": {
            "equity_usd": 50_000.0,
            "positions": {
                "NVDA": {"short_dex": "io", "long_dex": "xyz", "leg_notional_usd": 5_000.0, "live_spread_apr_pct": 40.0},
                "TSLA": {"short_dex": "io", "long_dex": "xyz", "leg_notional_usd": 5_000.0, "live_spread_apr_pct": 20.0},
            },
        },
        "cross_dex_paper": {
            "equity_usd": 50_000.0,
            "positions": {
                "SOL": {"other_dex": "lighter", "leg_notional_usd": 5_000.0, "live_spread_apr_pct": 30.0},
                "ARB": {"other_dex": "extended", "leg_notional_usd": 5_000.0, "live_spread_apr_pct": 18.0},
            },
        },
    }
    # 1. While drawdown_pct >= kill_switch_drawdown_pct (6.2 >= 5.0), flatten kill-switch stays latched (=0),
    #    yet disabled Testnet emits current_dd_pct=0 on testnet_alo_live!
    txt_still_dd = render_prometheus_metrics(
        overview,
        now_utc=datetime(2026, 11, 28, 14, 0, 10, tzinfo=timezone.utc),
    )
    assert "hl_testnet_last_cycle_ok 0" in txt_still_dd
    assert 'hl_strategy_current_dd_pct{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 0' in txt_still_dd
    assert "# HELP hl_testnet_perps_withdrawable_usd Testnet account withdrawable USDC" in txt_still_dd

    # OBS-02: 3 of 4 enabled jobs (ok, running, idle) are healthy; 1 interrupted is unhealthy -> 0.75
    assert "hl_worker_jobs_healthy_ratio 0.75" in txt_still_dd
    assert 'hl_worker_job_interrupted{worker_job="autopilot_allocator"} 1' in txt_still_dd
    assert 'hl_worker_job_interrupted{worker_job="spot_perp_paper"} 0' in txt_still_dd

    # OBS-05: unweighted avg open-pair APR emitted alongside capital-weighted hl_strategy_live_apr_pct
    assert "hl_hip3_paper_avg_pair_apr_pct 30" in txt_still_dd
    assert "hl_cross_dex_paper_avg_pair_apr_pct 24" in txt_still_dd

    # 2. Once HWM re-anchors / drawdown recovers (< 5.0%) on flat book, LIVE-01 clears hl_testnet_last_cycle_ok -> 1!
    overview["testnet_risk"]["drawdown_pct"] = 0.0
    txt_cleared = render_prometheus_metrics(
        overview,
        now_utc=datetime(2026, 11, 28, 14, 0, 10, tzinfo=timezone.utc),
    )
    assert "hl_testnet_last_cycle_ok 1" in txt_cleared
    assert "hl_testnet_hwm_usd 948.5" in txt_cleared


def test_obs01_obs02_obs03_obs04_alerts_and_dashboard_panels():
    prom = yaml.safe_load((ROOT / "docs/grafana/prometheus_alerts.yml").read_text(encoding="utf-8"))
    graf = yaml.safe_load((ROOT / "docs/grafana/grafana_alert_rules.yml").read_text(encoding="utf-8"))
    prom_by_name = {r["alert"]: r["expr"] for g in prom["groups"] for r in g["rules"]}
    graf_by_uid = {r["uid"]: r["data"][0]["model"]["expr"] for g in graf["groups"] for r in g["rules"]}

    expected_guards = [
        ("HLTestnetKillSwitchTriggered", "hl-crit-testnet-killswitch", "hl_worker_testnet_execution_enabled"),
        ("HLTestnetLiquidationRisk", "hl-crit-liq-risk", 'worker_job="testnet_attribution"'),
        ("HLTestnetADLRisk", "hl-crit-adl-risk", 'worker_job="testnet_attribution"'),
        ("HLTestnetDrawdownNearKillSwitch", "hl-crit-testnet-drawdown", 'worker_job="testnet_risk_probe"'),
        ("HLTestnetALOFillRateLow", "hl-warn-alo-fill-low", 'worker_job="testnet_attribution"'),
        ("HLTestnetDrawdownWarning", "hl-warn-testnet-drawdown", 'worker_job="testnet_risk_probe"'),
        ("HLTestnetOrphanPositions", "hl-warn-testnet-orphans", 'worker_job="testnet_risk_probe"'),
        ("HLTestnetMarkOracleDislocation", "hl-warn-testnet-mark-oracle", 'worker_job="testnet_risk_probe"'),
        ("HLTestnetCoinMarkOracleDislocation", "hl-warn-coin-mark-oracle", 'worker_job="testnet_risk_probe"'),
    ]
    for p_name, g_uid, guard_substr in expected_guards:
        assert guard_substr in prom_by_name[p_name], p_name
        assert guard_substr in graf_by_uid[g_uid], g_uid

    assert "1620" in prom_by_name["HLDailyArtifactStale"]
    assert "mm_calibration|settlement_study" in prom_by_name["HLDailyArtifactStale"]
    assert "1620" in graf_by_uid["hl-warn-daily-artifact-stale"]

    dash = json.loads((ROOT / "docs/grafana/hyperliquid_quant_defi_dashboard.json").read_text(encoding="utf-8"))
    by_id = {p["id"]: p for p in dash["panels"]}
    # Panel 31 wraps optional sleeve sums in `or vector(0)`
    p31_exprs = [t["expr"] for t in by_id[31]["targets"]]
    assert all("or vector(0)" in e for e in (p31_exprs[0], p31_exprs[3]))
    # Panel 65 maps INTERRUPTED distinctly
    p65 = by_id[65]
    assert "hl_worker_job_interrupted" in p65["targets"][0]["expr"]
    assert "INTERRUPTED" in json.dumps(p65["fieldConfig"]["overrides"])


def test_obs06_dashboard_settlement_nan_clustered_t_and_bh_fdr():
    # Case 1: t_stat_clustered is NaN or None -> treated as -inf (NEVER falls back to t_stat=5.0 -> NO GO!)
    nan_stats = [
        {"bucket": "b1", "strategy": "s1", "split": "dev", "events": 80, "mean_gross_bps": 9.0, "net_maker_bps": 6.0, "t_stat": 5.0},
        {
            "bucket": "b1",
            "strategy": "s1",
            "split": "holdout",
            "events": 80,
            "clusters": 40,
            "mean_gross_bps": 9.0,
            "net_maker_bps": 6.0,
            "t_stat": 5.0,
            "t_stat_clustered": math.nan,
            "ex_top1_gross_bps": 8.0,
        },
    ]
    html_nan = pdash.render_portfolio_dashboard_html({"generated_at_utc": "now", "settlement_stats": nan_stats})
    assert '<span class="pill green">GO</span>' not in html_nan

    # Case 2: 30 simultaneous bucket-strategy cells where 1 cell has marginal net_t_stat=2.1 among 29 noise cells -> rejected by BH-FDR q=0.10!
    many_rows: list[dict[str, Any]] = [
        {"bucket": "b0", "strategy": "s0", "split": "dev", "events": 60, "mean_gross_bps": 7.0, "net_maker_bps": 4.0, "t_stat": 3.5},
        {
            "bucket": "b0",
            "strategy": "s0",
            "split": "holdout",
            "events": 60,
            "clusters": 30,
            "mean_gross_bps": 5.1,
            "net_maker_bps": 2.1,
            "t_stat": 5.1,
            "net_t_stat": 2.1,  # p ~ 0.020 > (1/30)*0.10 = 0.0033 -> fails BH-FDR!
            "t_stat_clustered": 2.2,
            "ex_top1_gross_bps": 4.8,
        },
    ]
    for k in range(1, 30):
        many_rows.append({"bucket": f"b{k}", "strategy": "s0", "split": "dev", "events": 60, "mean_gross_bps": 0.0, "net_maker_bps": -3.0, "t_stat": 0.0})
        many_rows.append(
            {
                "bucket": f"b{k}",
                "strategy": "s0",
                "split": "holdout",
                "events": 60,
                "clusters": 30,
                "mean_gross_bps": 0.0,
                "net_maker_bps": -3.0,
                "t_stat": 0.0,
                "t_stat_clustered": 0.0,
            }
        )
    html_fdr = pdash.render_portfolio_dashboard_html({"generated_at_utc": "now", "settlement_stats": many_rows})
    assert '<span class="pill green">GO</span>' not in html_fdr

    # Explicit fdr_pass=True with valid clustered & net t-stats produces GO
    many_rows[1]["fdr_pass"] = True
    html_explicit = pdash.render_portfolio_dashboard_html({"generated_at_utc": "now", "settlement_stats": many_rows})
    assert '<span class="pill green">GO</span>' in html_explicit


# --------------------------------------------------------------------------- 4. OPS-01..OPS-10 (Scheduler, Lock, Deploy, Crons, Compose, CI)


def test_ops01_ops02_ops03_ops05_ops09_scheduler_hardening(tmp_path: Path, monkeypatch):
    # OPS-03: dynamic mm_calibration timeout, risk_probe --metamodel-state, settlement_study retries=0
    monkeypatch.setenv("HL_MM_CALIBRATION_SEC", "600")
    specs = {j.name: j for j in build_job_specs()}
    assert specs["mm_calibration"].timeout_s == 780
    assert specs["settlement_study"].retries == 0
    probe_cmd = specs["testnet_risk_probe"].build_command(Path("/repo"), tmp_path, False)
    assert "--metamodel-state" in probe_cmd
    assert str(tmp_path / "paper/hl_metamodel/state.json") in probe_cmd

    # OPS-03: settlement_study exit_code=2 -> state="ok", last_error="degraded_retained_prev", 0 retries, no failure alert;
    # and _bootstrap_needs_refresh checks last_attempt.json!
    calls: list[str] = []
    alerts: list[str] = []
    sched = WorkerScheduler(
        data_dir=tmp_path,
        telegram_enabled=True,
        readonly_retries=2,
        readonly_retry_delay_s=0.0,
        runner=lambda cmd, _env, _t: (calls.append(Path(cmd[1]).name) or 2),
        alert_sender=alerts.append,
    )
    assert sched.run_job(specs["settlement_study"]) == 2
    assert calls == ["run_hl_funding_settlement_study.py"]  # retries=0 -> 1 attempt only!
    assert sched._state["settlement_study"]["state"] == "ok"
    assert sched._state["settlement_study"]["last_error"] == "degraded_retained_prev"
    assert alerts == []

    study_dir = tmp_path / "paper" / "hl_funding_settlement_study"
    study_dir.mkdir(parents=True, exist_ok=True)
    sentinel = study_dir / "stats.parquet"
    sentinel.write_bytes(b"PAR1")
    (study_dir / "meta.json").write_text(json.dumps({"degraded": False, "coins_requested": 35, "coins_collected": 35}), encoding="utf-8")
    assert sched._bootstrap_needs_refresh("settlement_study", sentinel) is False
    (study_dir / "last_attempt.json").write_text(json.dumps({"degraded": True, "coins_requested": 35, "coins_collected": 10}), encoding="utf-8")
    assert sched._bootstrap_needs_refresh("settlement_study", sentinel) is True

    # OPS-02: --run-once merges ONLY target job without clobbering daemon heartbeat_utc or other jobs' counters
    daemon_hb = "2026-11-28 14:30:00 UTC"
    status_path = tmp_path / "paper" / "hl_worker_status.json"
    status_path.write_text(
        json.dumps(
            {
                "service": "hl-worker",
                "build_sha": "daemon-sha",
                "heartbeat_utc": daemon_hb,
                "jobs": [
                    {"name": "spot_perp_paper", "enabled": True, "state": "ok", "runs_ok": 42, "runs_failed": 0},
                    {"name": "onchain_events", "enabled": True, "state": "ok", "runs_ok": 10, "runs_failed": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    once_sched = WorkerScheduler(
        data_dir=tmp_path,
        telegram_enabled=False,
        single_job_mode="onchain_events",
        runner=lambda *_a: 0,
    )
    assert once_sched.run_job(specs["onchain_events"]) == 0
    merged = json.loads(status_path.read_text(encoding="utf-8"))
    assert merged["heartbeat_utc"] == daemon_hb
    assert merged["build_sha"] == "daemon-sha"
    merged_jobs = {j["name"]: j for j in merged["jobs"]}
    assert merged_jobs["spot_perp_paper"]["runs_ok"] == 42
    assert merged_jobs["onchain_events"]["runs_ok"] == 11
    assert not list((tmp_path / "paper").glob(".*.tmp"))

    # OPS-05: _queued_or_running marks queued upstream dependencies as running, and autopilot_allocator waits on all 4 sleeves
    deps = WorkerScheduler.DEFAULT_JOB_DEPENDENCIES["autopilot_allocator"][0]
    assert set(deps) == {"spot_perp_paper", "cross_dex_paper", "hip3_paper", "metamodel_paper"}
    sched._mark_job_queued("spot_perp_paper")
    assert sched._is_job_running("spot_perp_paper") is True
    sched._unmark_job_queued("spot_perp_paper")
    assert sched._is_job_running("spot_perp_paper") is False

    # OPS-09: failed _dispatch_alert_html does NOT burn cooldown or mark alerted_failed; successful alert persists last_failure_alert_utc across restarts!
    send_attempts = {"n": 0}
    delivered: list[str] = []

    def flaky_sender(html_msg: str) -> None:
        send_attempts["n"] += 1
        if send_attempts["n"] == 1:
            raise RuntimeError("telegram 502")
        delivered.append(html_msg)

    alert_dir = tmp_path / "alert_test"
    sched_al = WorkerScheduler(
        data_dir=alert_dir,
        telegram_enabled=True,
        readonly_retries=0,
        alert_cooldown_s=3600.0,
        runner=lambda *_a: 1,
        alert_sender=flaky_sender,
    )
    j_snap = next(j for j in sched_al.jobs if j.name == "market_snapshot")
    assert sched_al.run_job(j_snap) == 1
    assert len(delivered) == 0
    assert sched_al._state["market_snapshot"]["alerted_failed"] is False  # delivery failed -> not marked!

    # Immediate next failure retries Telegram and succeeds (was NOT suppressed by 1h cooldown!)
    assert sched_al.run_job(j_snap) == 1
    assert len(delivered) == 1
    persisted_alert_utc = sched_al._state["market_snapshot"]["last_failure_alert_utc"]
    assert isinstance(persisted_alert_utc, str) and persisted_alert_utc.endswith("UTC")

    # Restarting WorkerScheduler hydrates last_failure_alert_utc and suppresses duplicate alert within 1h cooldown!
    restarted_delivered: list[str] = []
    sched_restarted = WorkerScheduler(
        data_dir=alert_dir,
        telegram_enabled=True,
        readonly_retries=0,
        alert_cooldown_s=3600.0,
        runner=lambda *_a: 1,
        alert_sender=restarted_delivered.append,
    )
    assert sched_restarted.run_job(j_snap) == 1
    assert restarted_delivered == []


def test_ops06_ops07_ops08_ops10_crons_ops_lock_deploy_cli_defaults_and_ci(tmp_path: Path):
    # OPS-08: deploy_dokploy rejects missing/empty heartbeat_utc
    deploy = _load_script_module("deploy_pass4", "scripts/ops/deploy_dokploy.py")
    assert deploy._worker_heartbeat_is_fresh(None) is False
    assert deploy._worker_heartbeat_is_fresh("") is False
    assert deploy._worker_heartbeat_is_fresh("   ") is False

    # OPS-10: 6 standalone analytics CLIs default to gitignored data/reports/ (never docs/reports/)
    for rel_cli in (
        "scripts/analytics/run_hl_funding_settlement_study.py",
        "scripts/analytics/build_hl_portfolio_dashboard.py",
        "scripts/analytics/build_hl_tear_sheet.py",
        "scripts/analytics/run_hl_boros_scan.py",
        "scripts/analytics/run_hl_hip3_arb_scan.py",
        "scripts/analytics/run_hl_hyperevm_yield_stack.py",
    ):
        cli_mod = _load_script_module(Path(rel_cli).stem + "_p4", rel_cli)
        sig = inspect.signature(cli_mod.main)
        defaults_str = " ".join(str(p.default.default) for p in sig.parameters.values() if hasattr(p.default, "default"))
        assert "docs/reports" not in defaults_str, rel_cli
        assert "data/reports/" in defaults_str, rel_cli

    # OPS-01 & OPS-10: Compose stop_grace_period + CI/Release workflow checks
    for cmp_name in ("docker-compose.yml", "docker-compose.dokploy.yml"):
        cmp_doc = yaml.safe_load((ROOT / cmp_name).read_text(encoding="utf-8"))
        assert cmp_doc["services"]["backend"]["stop_grace_period"] == "15s"
        assert cmp_doc["services"]["worker"]["stop_grace_period"] == "30s"
        assert "HL_WORKER_STARTUP_CATCHUP_MIN" in cmp_doc["services"]["worker"]["environment"]

    ci_txt = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    rel_txt = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    assert '"Makefile"' in ci_txt
    assert "--extra dev --extra testnet" in ci_txt
    assert "--extra dev --extra testnet" in rel_txt
    assert "PR Docker build & Compose smoke test" in ci_txt

    # OPS-07: ops_lock forwards SIGTERM to child process group cleanly
    lock_file = tmp_path / "sigterm.flock"
    ready_file = tmp_path / "child_ready"
    child_code = (
        f"import pathlib, signal, sys, time; "
        f"signal.signal(signal.SIGTERM, lambda *_: sys.exit(0)); "
        f"pathlib.Path({str(ready_file)!r}).write_text('1'); "
        f"time.sleep(30)"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "hl_quant.ops_lock", str(lock_file), "--", sys.executable, "-c", child_code],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
    )
    deadline = time.monotonic() + 5.0
    while not ready_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert ready_file.exists()
    proc.send_signal(signal.SIGTERM)
    rc = proc.wait(timeout=5.0)
    assert rc == 128 + signal.SIGTERM

    # OPS-06: cron installers use :17 for cross-dex, TZ=UTC, and --attribution-json / --telegram parity in wrappers
    mm_sh = (ROOT / "scripts/ops/run_hl_metamodel_paper_tick.sh").read_text(encoding="utf-8")
    ap_sh = (ROOT / "scripts/ops/run_hl_autopilot_tick.sh").read_text(encoding="utf-8")
    sp_sh = (ROOT / "scripts/ops/run_hl_spot_perp_paper_tick.sh").read_text(encoding="utf-8")
    assert "--attribution-json" in mm_sh
    assert "--telegram" in ap_sh and "tee -a" in ap_sh
    assert "--telegram" in sp_sh and "tee -a" in sp_sh

    if shutil.which("zsh"):
        fake_bin = tmp_path / "bin"
        fake_bin.mkdir()
        store = tmp_path / "crontab.txt"
        store.write_text("", encoding="utf-8")
        fake_crontab = fake_bin / "crontab"
        fake_crontab.write_text(
            f'#!/bin/sh\nif [ "$1" = "-l" ]; then cat "{store}"; else cp "$1" "{store}"; fi\n',
            encoding="utf-8",
        )
        fake_crontab.chmod(0o755)
        env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}", "TZ": "UTC"}
        subprocess.run(["zsh", str(ROOT / "scripts/ops/install_all_hl_crons.sh")], env=env, check=True, capture_output=True)
        installed = store.read_text(encoding="utf-8")
        assert "17 0,4,8,12,16,20 * * * TZ=UTC zsh" in installed
        assert "0 9 * * * TZ=UTC zsh" in installed
        assert "CRON_TZ=UTC" in installed

    # CLI --run-once smoke check via CliRunner
    res = CliRunner().invoke(scheduler_cli, ["--data-dir", str(tmp_path / "cli_d"), "--list"])
    assert res.exit_code == 0
    assert "settlement_study" in res.output
