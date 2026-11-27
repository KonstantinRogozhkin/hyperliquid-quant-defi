"""Comprehensive regression tests for Wave 3 infrastructure, metrics, analytics, and ops fixes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from typing import Any
from urllib.error import HTTPError

from fastapi.testclient import TestClient
import pandas as pd
import pytest
import yaml

from hl_quant import hl_analytics_risk as ar
from hl_quant import hl_portfolio_dashboard as pdash
from hl_quant.api.app import ARTIFACT_FILES, create_app
from hl_quant.metrics import render_prometheus_metrics
from hl_quant.scheduler import WorkerScheduler, build_job_specs

ROOT = Path(__file__).resolve().parents[1]


def _load_script_module(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --------------------------------------------------------------------------- 1. Metrics & Grafana / Alerts (C-7, M-1, M-2, H-14)


def test_c7_nav_guard_active_gauge_vs_lifetime_triggers_counter():
    # Historical confirmed trigger (triggers=3, but nav_guard_triggered=False) must NOT set active=1
    calm_overview = {
        "spot_perp_paper": {"equity_usd": 100_000.0, "nav_guard_triggers": 1, "risk_telemetry": {"nav_guard_triggered": False}},
        "autopilot": {"gross_equity_usd": 250_000.0, "nav_guard_triggers": 1, "risk_telemetry": {"nav_guard_triggered": False}},
        "cross_dex_paper": {"equity_usd": 50_000.0, "nav_guard_triggers": 2, "nav_guard_triggered": False},
        "hip3_paper": {"equity_usd": 50_000.0, "nav_guard_triggers": 3, "nav_guard_triggered": False},
        "paper": {"equity_usd": 100_000.0, "nav_guard_triggers": 4, "nav_guard_triggered": False},
    }
    txt_calm = render_prometheus_metrics(calm_overview)
    assert "hl_desk_nav_guard_active 0" in txt_calm
    assert "hl_desk_nav_guard_triggers_total 11" in txt_calm
    assert "hl_metamodel_nav_guard_triggers_total 4" in txt_calm

    # Active guard on Cross-DEX flips hl_desk_nav_guard_active to 1 and cross_dex_v2 status_ok to 0
    tripped_overview = {
        **calm_overview,
        "cross_dex_paper": {"equity_usd": 50_000.0, "nav_guard_triggers": 2, "nav_guard_triggered": True},
    }
    txt_tripped = render_prometheus_metrics(tripped_overview)
    assert "hl_desk_nav_guard_active 1" in txt_tripped
    assert 'hl_strategy_status_ok{strategy="cross_dex_v2",category="Market Neutral",mode="paper"} 0' in txt_tripped


def test_m1_h14_metamodel_leverage_testnet_live_risk_audit_and_settlement_degraded():
    overview = {
        "paper": {
            "starting_capital_usd": 100_000.0,
            "equity_usd": 100_250.0,
            "return_pct": 0.25,
            "active_positions": 2,
            "ticks": 10,
            "cum_funding_usd": 420.5,
            "cum_costs_usd": -115.25,
            "nav_guard_triggered": True,
            "nav_guard_triggers": 1,
        },
        "paper_positions": [
            {"coin": "BTC", "hl_weight": 0.30, "cex_legs": {"bybit": -0.20, "binance": -0.10}},
            {"coin": "ETH", "hl_weight": -0.15, "cex_legs": {"okx": 0.15}},
        ],
        "hip3_paper": {
            "starting_capital_usd": 50_000.0,
            "equity_usd": 50_100.0,
            "return_pct": 0.2,
            "nav_guard_triggered": True,
            "positions": {},
        },
        "attribution": {
            "observed_at_ms": 1_795_800_000_000,
            "equity_usd": 1_010.0,
            "deposited_capital_usd": 1_000.0,
            "account_return_pct": 1.0,
            "net_attributed_pnl_usd": 12.0,
            "funding_pnl_usd": 3.0,
            "fees_usd": -1.5,
            "open_positions_count": 2,
            "liq_warnings": [],
            "adl_warnings": [],
            "per_coin": [
                {"coin": "BTC", "leverage_mode": "cross", "position_sz": 0.002, "position_usd": 180.0, "net_pnl_usd": 2.0},
                {"coin": "CLOSED", "leverage_mode": "cross", "position_sz": 0.0, "position_usd": 0.0, "net_pnl_usd": 5.0},
            ],
        },
        "testnet_audit_recent": [
            {"mode": "dry_run", "status": "kill_switch", "high_water_mark_usd": 99_999.0, "placed_count": 99},
            {"mode": "testnet_live", "status": "ok", "high_water_mark_usd": 1_020.0, "placed_count": 4},
        ],
        "testnet_risk": {
            "observed_at_ms": 1_795_800_900_000,  # Newer than attribution
            "equity_usd": 985.5,
            "gross_leverage": 0.4125,
            "drawdown_pct": 3.3824,
            "open_positions": 1,
        },
        "settlement_meta": {"coins_requested": 35, "coins_collected": 15},  # 15 < ceil(0.8*35)=28 -> degraded!
    }
    analytics = {
        "leaderboard": [
            {"id": "bench_btc", "is_benchmark": True, "spot_price_usd": 90_000.0, "gross_returns": {"all_time": 42.0}, "max_dd_gross_pct": 24.0}
        ]
    }
    txt = render_prometheus_metrics(overview, analytics=analytics)

    # Metamodel real leverage (0.30+0.30 + 0.15+0.15 = 0.9), funding, costs, status_ok
    assert 'hl_strategy_leverage{strategy="metamodel_5bps",category="Observation",mode="paper"} 0.9' in txt
    assert 'hl_strategy_funding_pnl_usd{strategy="metamodel_5bps",category="Observation",mode="paper"} 420.5' in txt
    assert 'hl_strategy_costs_usd{strategy="metamodel_5bps",category="Observation",mode="paper"} 115.25' in txt
    assert 'hl_strategy_status_ok{strategy="metamodel_5bps",category="Observation",mode="paper"} 0' in txt
    assert 'hl_strategy_status_ok{strategy="hip3_split_margin",category="Market Neutral",mode="paper"} 0' in txt

    # Closed coin skipped in hl_testnet_position_*
    assert 'coin="BTC"' in txt
    assert 'coin="CLOSED"' not in txt

    # dry_run audit ignored in favor of latest testnet_live audit
    assert "hl_testnet_hwm_usd 1020" in txt
    assert "hl_testnet_last_cycle_ok 1" in txt
    assert "hl_testnet_last_cycle_placed 4" in txt

    # Newer 15m testnet_risk drives testnet_alo_live equity, leverage, drawdown, and reconciled net_pnl_usd
    assert 'hl_strategy_equity_usd{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 985.5' in txt
    assert 'hl_strategy_starting_capital_usd{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 1000' in txt
    assert 'hl_strategy_net_pnl_usd{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} -14.5' in txt
    assert 'hl_strategy_leverage{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 0.4125' in txt
    assert 'hl_strategy_max_dd_pct{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 3.3824' in txt

    # Settlement study degraded (15/35) and benchmark 1Y help text
    assert "hl_settlement_study_degraded 1" in txt
    assert "# HELP hl_benchmark_return_pct Benchmark trailing 1Y (370D) cumulative return %" in txt
    assert "# HELP hl_benchmark_max_dd_pct Benchmark trailing 1Y (370D) max drawdown %" in txt

    # Zero equity with positive HWM reports 100% drawdown (never 0%)
    wiped_overview = {
        "attribution": {"observed_at_ms": 2_000, "equity_usd": 0.0},
        "testnet_audit_recent": [{"mode": "testnet_live", "status": "ok", "high_water_mark_usd": 1_000.0}],
    }
    txt_wiped = render_prometheus_metrics(wiped_overview)
    assert 'hl_strategy_max_dd_pct{strategy="testnet_alo_live",category="Live Testnet",mode="testnet"} 100' in txt_wiped


def test_m2_grafana_16_jobs_and_prometheus_grafana_alert_parity():
    dash = json.loads((ROOT / "docs/grafana/hyperliquid_quant_defi_dashboard.json").read_text(encoding="utf-8"))
    by_title = {p["title"]: p for p in dash["panels"]}
    assert "All 16 UTC Worker Jobs — Live Status, Runs & Duration" in by_title
    assert by_title["NAV Guard Active"]["targets"][0]["expr"] == "hl_desk_nav_guard_active"
    assert by_title["Healthy Jobs"]["targets"][0]["expr"] == "hl_worker_jobs_healthy_ratio"

    prom = yaml.safe_load((ROOT / "docs/grafana/prometheus_alerts.yml").read_text(encoding="utf-8"))
    graf = yaml.safe_load((ROOT / "docs/grafana/grafana_alert_rules.yml").read_text(encoding="utf-8"))
    prom_by_name = {r["alert"]: r for g in prom["groups"] for r in g["rules"]}
    graf_by_uid = {r["uid"]: r for g in graf["groups"] for r in g["rules"]}

    assert len(prom_by_name) == len(graf_by_uid) == 20
    assert prom_by_name["HLBoundedNavGuardTriggered"]["expr"] == "hl_desk_nav_guard_active > 0"
    assert graf_by_uid["hl-crit-nav-guard"]["data"][0]["model"]["expr"] == "hl_desk_nav_guard_active"
    assert "hl_artifact_exists" in prom_by_name["HLArtifactStale"]["expr"]

    for name in ("HLTestnetRiskProbeStale", "HLTestnetAttributionStale"):
        expr = prom_by_name[name]["expr"]
        assert "on()" not in expr
        assert expr.count("ignoring(artifact, worker_job)") == 2
    for uid in ("hl-warn-testnet-risk-stale", "hl-warn-testnet-attr-stale"):
        expr = graf_by_uid[uid]["data"][0]["model"]["expr"]
        assert "on()" not in expr
        assert expr.count("ignoring(artifact, worker_job)") == 2


# --------------------------------------------------------------------------- 2. Scheduler & Dokploy Cutover (H-11, M-3)


def test_h11_scheduler_publish_status_mm_audit_max_workers_and_dependency_wait(tmp_path: Path, monkeypatch):
    # Flag true WITHOUT credentials must report testnet_execution_enabled=False
    monkeypatch.setenv("HL_ENABLE_TESTNET_EXECUTION", "true")
    monkeypatch.delenv("HL_TESTNET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, dependency_poll_s=0.02)
    assert sched.max_workers == 6
    status = sched.publish_status()
    assert status["testnet_execution_enabled"] is False

    jobs = {j.name: j for j in build_job_specs()}
    exec_cmd = jobs["testnet_executor"].build_command(Path("/repo"), tmp_path, False)
    assert "--mm-audit-log" in exec_cmd
    idx = exec_cmd.index("--mm-audit-log")
    assert exec_cmd[idx + 1] == str(tmp_path / "paper/hl_mm_testnet/mm_audit.jsonl")

    # hip3_paper waits for running frontier_persistence before starting
    events: list[tuple[str, float]] = []

    def concurrent_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        script = Path(cmd[1]).name
        if script == "run_hl_frontier_persistence_tick.py":
            time.sleep(0.12)
            events.append(("frontier_done", time.monotonic()))
        elif script == "run_hl_hip3_paper_tick.py":
            events.append(("hip3_start", time.monotonic()))
        return 0

    sched2 = WorkerScheduler(
        data_dir=tmp_path,
        telegram_enabled=False,
        runner=concurrent_runner,
        dependency_poll_s=0.01,
    )
    j_map = {j.name: j for j in sched2.jobs}
    t_front = threading.Thread(target=sched2.run_job, args=(j_map["frontier_persistence"],))
    t_front.start()
    deadline = time.monotonic() + 2.0
    while not sched2._is_job_running("frontier_persistence") and time.monotonic() < deadline:
        time.sleep(0.005)
    t_hip3 = threading.Thread(target=sched2.run_job, args=(j_map["hip3_paper"],))
    t_hip3.start()
    t_front.join(timeout=3.0)
    t_hip3.join(timeout=3.0)
    ev_map = dict(events)
    assert ev_map["hip3_start"] >= ev_map["frontier_done"]


def test_m3_cutover_required_jobs_testnet_executor_verification_and_stray_python_lines():
    cutover = _load_script_module("cutover_w3", "scripts/ops/cutover_local_crons_to_dokploy.py")
    assert "cross_dex_paper" in cutover.DEFAULT_REQUIRED_JOBS

    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=timezone.utc)
    hb = (now - timedelta(seconds=10)).strftime("%Y-%m-%d %H:%M:%S UTC")

    def _build_fetcher(*, te_enabled: bool, te_state: str):
        jobs = [
            {"name": name, "enabled": True, "status_reason": "active", "state": "ok", "last_exit_code": 0}
            for name in cutover.DEFAULT_REQUIRED_JOBS
        ]
        jobs.append(
            {
                "name": "testnet_executor",
                "enabled": te_enabled,
                "status_reason": "missing_key" if not te_enabled else "active",
                "state": te_state,
                "last_exit_code": 1 if te_state == "failed" else 0,
            }
        )

        def _fetch(url: str) -> dict[str, Any]:
            if url.endswith("/api/v1/healthz"):
                return {"status": "ok"}
            if url.endswith("build-info.json"):
                return {"sha": "a" * 40}
            return {
                "build_info": {"sha": "a" * 40},
                "worker_status": {
                    "service": "hl-worker",
                    "build_sha": "a" * 40,
                    "heartbeat_utc": hb,
                    "telegram_enabled": True,
                    "testnet_execution_enabled": True,
                    "jobs": jobs,
                },
            }

        return _fetch

    # Worker claims testnet_execution_enabled=true, but testnet_executor job is disabled -> downgraded to False
    res = cutover.verify_dokploy_stack("https://hl.example.com", fetcher=_build_fetcher(te_enabled=False, te_state="disabled"), now_utc=now)
    assert res.testnet_execution_enabled is False
    with pytest.raises(RuntimeError, match="testnet_executor disabled"):
        cutover.verify_dokploy_stack(
            "https://hl.example.com",
            expect_testnet_worker=True,
            fetcher=_build_fetcher(te_enabled=False, te_state="disabled"),
            now_utc=now,
        )
    with pytest.raises(RuntimeError, match="testnet_executor.*failed"):
        cutover.verify_dokploy_stack(
            "https://hl.example.com",
            fetcher=_build_fetcher(te_enabled=True, te_state="failed"),
            now_utc=now,
        )

    # Stray direct scripts/analytics/*.py cron lines are caught
    stray_cron = "15 */4 * * * /opt/hl/.venv/bin/python /opt/hl/scripts/analytics/run_hl_cross_dex_paper_tick.py\n"
    plan = cutover.transform_crontab(stray_cron, mode="comment", date_tag="2026-11-27")
    assert plan.changed_count == 1
    assert plan.updated_text.startswith("# Disabled 2026-11-27")


# --------------------------------------------------------------------------- 3. Cron Installers & Ops Lock Scripts (H-12)


def test_h12_install_local_cron_scripts_preserve_lines_after_legacy_blocks(tmp_path: Path):
    install_scripts = sorted((ROOT / "scripts/ops").glob("install_local_*.sh"))
    assert len(install_scripts) == 7

    for script in install_scripts:
        content = script.read_text(encoding="utf-8")
        assert "crontab <" not in content
        assert "mktemp" in content
        lbegin = next(ln.split("=", 1)[1].strip('"') for ln in content.splitlines() if ln.startswith("LEGACY_BEGIN_MARKER="))
        lend = next(ln.split("=", 1)[1].strip('"') for ln in content.splitlines() if ln.startswith("LEGACY_END_MARKER="))
        assert lbegin.removeprefix("# BEGIN ") == lend.removeprefix("# END "), script.name

    # End-to-end check with fake `crontab` binary: lines AFTER a legacy block must NEVER be wiped!
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    store = tmp_path / "cron_store.txt"
    store.write_text(
        "0 * * * * /usr/local/bin/before_job.sh\n"
        "# BEGIN Spy2Money BackTester hl cross-dex paper schedule\n"
        "15 */4 * * * zsh /old/run_hl_cross_dex_paper_tick.sh\n"
        "# END Spy2Money BackTester hl cross-dex paper schedule\n"
        "30 * * * * /usr/local/bin/after_job_must_survive.sh\n",
        encoding="utf-8",
    )
    fake_crontab = fake_bin / "crontab"
    fake_crontab.write_text(
        f"#!/bin/sh\nif [ \"$1\" = \"-l\" ]; then cat \"{store}\"; else cp \"$1\" \"{store}\"; fi\n",
        encoding="utf-8",
    )
    fake_crontab.chmod(0o755)

    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ.get('PATH', '')}"}
    proc = subprocess.run(
        ["zsh", str(ROOT / "scripts/ops/install_local_hl_cross_dex_cron.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert "installed_crontab=" in proc.stdout
    installed = store.read_text(encoding="utf-8")
    assert "before_job.sh" in installed
    assert "after_job_must_survive.sh" in installed
    assert "# BEGIN Spy2Money BackTester" not in installed
    assert "# BEGIN hyperliquid-quant-defi cross-dex paper schedule" in installed


def test_h12_run_tick_scripts_use_atomic_ops_lock():
    target_scripts = [
        "run_hl_cross_dex_paper_tick.sh",
        "run_hl_frontier_persistence_tick.sh",
        "run_hl_metamodel_paper_tick.sh",
        "run_hl_onchain_event_tick.sh",
        "run_hl_testnet_attribution_daily.sh",
        "run_hl_testnet_executor_tick.sh",
        "run_hyperliquid_snapshot_tick.sh",
    ]
    for name in target_scripts:
        path = ROOT / "scripts/ops" / name
        text = path.read_text(encoding="utf-8")
        assert "hl_quant.ops_lock" in text, name
        assert 'mkdir "${LOCK_DIR}"' not in text, name
        assert 'rm -rf "${LOCK_DIR}"' not in text, name
        proc = subprocess.run(["zsh", str(path), "--help"], capture_output=True, text=True, check=True)
        assert "Usage:" in proc.stdout


# --------------------------------------------------------------------------- 4. Dokploy Deploy Hardening (H-13)


def test_h13_deploy_dokploy_env_comments_http_error_scheduler_probe_and_rollback_redeploy(monkeypatch):
    deploy = _load_script_module("deploy_dokploy_w3", "scripts/ops/deploy_dokploy.py")

    # 1. Inline # comments and quotes stripped cleanly
    env_txt = 'DEPLOY_SHA="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" # prod pin\nOTHER=val # trailing\n'
    assert deploy.extract_env_var(env_txt, "DEPLOY_SHA") == "a" * 40
    assert deploy.extract_env_var(env_txt, "OTHER") == "val"

    # 2. HTTPError surfaces HTTP code + body snippet without echoing secret payload
    def broken_urlopen(_req, timeout=20):
        raise HTTPError("https://dokploy.example.com/api/compose.update", 401, "Unauthorized", {}, io.BytesIO(b'{"message":"invalid api key"}'))

    monkeypatch.setattr(deploy, "urlopen", broken_urlopen)
    dc = deploy.DokployClient("https://dokploy.example.com/api", "super-secret-key")
    with pytest.raises(RuntimeError) as exc_info:
        dc.update_compose_env("cmp-1", "PRIVATE_KEY=0xdeadbeef")
    msg = str(exc_info.value)
    assert "HTTP 401" in msg and "invalid api key" in msg
    assert "0xdeadbeef" not in msg

    # 3. Post-deploy verification failure AFTER compose.deploy triggers rollback redeploy + wait
    prev_sha = "1" * 40
    bad_sha = "2" * 40
    triggered_titles: list[str] = []

    class _RollbackTrackingClient:
        def __init__(self) -> None:
            self.env = f"DEPLOY_SHA={prev_sha} # previous\n"
            self.deployments: list[dict[str, Any]] = []

        def get_compose(self, compose_id: str) -> dict[str, Any]:
            return {"composeId": compose_id, "autoDeploy": False, "env": self.env}

        def update_compose_env(self, _compose_id: str, env_text: str) -> None:
            self.env = env_text

        def list_deployments(self, _compose_id: str) -> list[dict[str, Any]]:
            for d in self.deployments:
                d["status"] = "done"
            return list(self.deployments)

        def trigger_deploy(self, _compose_id: str, title: str = "") -> None:
            triggered_titles.append(title)
            self.deployments.append({"deploymentId": f"dep-{len(self.deployments) + 1}", "status": "running", "title": title})

    # Scheduler status returning wrong SHA fails verify_live_probes and triggers full container rollback
    def bad_probe_fetch(url: str, *, timeout_s: int = 10) -> dict[str, Any]:
        if url.endswith("/api/v1/healthz"):
            return {"status": "ok"}
        if url.endswith("/api/v1/scheduler/status"):
            return {"build_info": {"sha": prev_sha}, "worker_status": "not-a-dict"}
        return {"sha": bad_sha}

    monkeypatch.setattr(deploy, "fetch_json", bad_probe_fetch)
    rb_client = _RollbackTrackingClient()
    with pytest.raises(RuntimeError, match="Post-deploy verification failed"):
        deploy.deploy_sha_to_dokploy(
            rb_client,
            compose_id="cmp-rb",
            sha=bad_sha,
            public_url="https://hl.example.com",
            verify_timeout_s=0,
            poll_interval_s=0.01,
        )
    assert deploy.extract_env_var(rb_client.env, "DEPLOY_SHA") == prev_sha
    assert len(triggered_titles) == 2
    assert "Rollback" in triggered_titles[1]


# --------------------------------------------------------------------------- 5. API, Portfolio Dashboard & Risk Analytics (H-14.1, H-19, H-22.2, M-18, L-4)


def test_api_audit_fields_cex_legs_and_artifact_files(tmp_path: Path):
    assert ARTIFACT_FILES["benchmarks"] == "hl_benchmarks/daily_closes.json"
    assert ARTIFACT_FILES["spot_perp_scan"] == "hl_spot_perp_paper/snapshot.json"

    (tmp_path / "hl_metamodel").mkdir()
    (tmp_path / "hl_metamodel" / "state.json").write_text(
        json.dumps(
            {
                "positions": {
                    "SOL": {
                        "hl_weight": 0.2,
                        "cex_weight": -0.2,
                        "cex_venue": "bybit",
                        "cex_legs": {"bybit": -0.12, "okx": -0.08},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "hl_testnet_audit.jsonl").write_text(
        json.dumps(
            {
                "ts_ms": 1_795_800_000_000,
                "mode": "dry_run",
                "status": "ok",
                "derisk_mode": "reduce_only",
                "orphan_unwind_count": 2,
                "blocked_leverage_coins": ["TRUMP"],
                "foreign_coins": ["HYPE"],
                "free_margin_usd": 745.25,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    client = TestClient(create_app(data_root=tmp_path, build_sha="deadbeef"))
    pos = client.get("/api/v1/metamodel/positions").json()["positions"][0]
    assert pos["cex_legs"] == {"bybit": -0.12, "okx": -0.08}

    aud = client.get("/api/v1/attribution").json()["testnet_audit_recent"][0]
    assert aud["mode"] == "dry_run"
    assert aud["derisk_mode"] == "reduce_only"
    assert aud["orphan_unwind_count"] == 2
    assert aud["blocked_leverage_coins"] == ["TRUMP"]
    assert aud["foreign_coins"] == ["HYPE"]
    assert aud["free_margin_usd"] == pytest.approx(745.25)


def test_portfolio_dashboard_study_selection_combo_badge_go_verdict_and_cache_copy(tmp_path: Path):
    # 1. Live hl_funding_settlement_study preferred over older hl_funding_settlement_study_52d
    old_dir = tmp_path / "hl_funding_settlement_study_52d"
    live_dir = tmp_path / "hl_funding_settlement_study"
    old_dir.mkdir()
    live_dir.mkdir()
    pd.DataFrame([{"bucket": "extreme_pos", "strategy": "carry", "split": "dev", "events": 10, "mean_gross_bps": 5.0, "net_maker_bps": 2.0, "t_stat": 3.0}]).to_parquet(
        old_dir / "stats.parquet", index=False
    )
    (old_dir / "meta.json").write_text(json.dumps({"total_settlement_events": 111}), encoding="utf-8")
    old_ts = time.time() - 3600
    os.utime(old_dir / "stats.parquet", (old_ts, old_ts))

    pd.DataFrame(
        [
            {"bucket": "extreme_pos", "strategy": "carry", "split": "dev", "events": 50, "mean_gross_bps": 8.0, "net_maker_bps": 5.0, "t_stat": 4.0},
            {
                "bucket": "extreme_pos",
                "strategy": "carry",
                "split": "holdout",
                "events": 50,
                "mean_gross_bps": 6.0,
                "net_maker_bps": 3.0,  # implied cost = 3.0 bps
                "t_stat": 3.5,
                "t_stat_clustered": 1.4,  # < 2.0 -> must NOT get GO verdict!
                "ex_top1_gross_bps": 5.0,
            },
            {"bucket": "outlier_only", "strategy": "carry", "split": "dev", "events": 50, "mean_gross_bps": 8.0, "net_maker_bps": 5.0, "t_stat": 4.0},
            {
                "bucket": "outlier_only",
                "strategy": "carry",
                "split": "holdout",
                "events": 50,
                "mean_gross_bps": 6.0,
                "net_maker_bps": 3.0,  # implied cost = 3.0 bps
                "t_stat": 3.5,
                "t_stat_clustered": 2.8,
                "ex_top1_gross_bps": 2.5,  # 2.5 - 3.0 = -0.5 <= 0 -> dominated by 1 outlier, NOT GO!
            },
        ]
    ).to_parquet(live_dir / "stats.parquet", index=False)
    (live_dir / "meta.json").write_text(json.dumps({"total_settlement_events": 999}), encoding="utf-8")

    # 2. H1-Combo badge checks entry_type == 'basis_carry_combo' rather than raw entry_exec_basis_bps >= 8
    (tmp_path / "hl_cross_dex_paper").mkdir()
    (tmp_path / "hl_cross_dex_paper" / "state.json").write_text(
        json.dumps(
            {
                "equity_usd": 50_000.0,
                "positions": {
                    "SOL": {"coin": "SOL", "entry_type": "carry_persistent", "entry_exec_basis_bps": 12.0, "live_spread_apr_pct": 30.0},
                    "ARB": {"coin": "ARB", "entry_type": "basis_carry_combo", "entry_exec_basis_bps": 4.0, "live_spread_apr_pct": 25.0},
                },
            }
        ),
        encoding="utf-8",
    )

    payload = pdash.collect_dashboard_payload(tmp_path)
    assert payload["settlement_meta"]["total_settlement_events"] == 999

    # Mutating returned parquet records must not corrupt cached dicts
    payload["settlement_stats"][0]["bucket"] = "MUTATED"
    fresh_records = pdash._read_parquet_records(live_dir / "stats.parquet")
    assert fresh_records[0]["bucket"] == "extreme_pos"

    rendered = pdash.render_portfolio_dashboard_html(payload)
    assert '<span class="pill green">GO</span>' not in rendered
    assert rendered.count(">H1-Combo<") == 1
    assert rendered.count(">Carry<") == 1


def test_h19_m18_l4_analytics_net_streaks_risk_exposure_and_single_parquet_read(tmp_path: Path, monkeypatch):
    base_dt = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
    days = [base_dt + timedelta(days=i) for i in range(10)]
    # Tiny gross daily gains (+0.001%/day) smaller than 3.65% annual mgmt fee (-0.01%/day) -> gross positive, net negative!
    gross_eq = [100_000.0 * ((1.00001) ** (i + 1)) for i in range(10)]

    for sub, st_extra in (
        (
            "hl_cross_dex_paper",
            {
                "nav_guard_triggers": 3,
                "positions": {
                    "BTC": {
                        "hl_sign": -1.0,
                        "leg_notional_usd": 20_000.0,
                        "hl_notional_usd": 20_500.0,
                        "other_notional_usd": 19_500.0,
                    }
                },
            },
        ),
        (
            "hl_hip3_paper",
            {
                "nav_guard_triggers": 2,
                "positions": {
                    "NVDA": {
                        "leg_notional_usd": 10_000.0,
                        "long_notional_usd": 10_400.0,
                        "short_notional_usd": 9_900.0,
                        "margin_locked_usd": 5_000.0,
                    }
                },
            },
        ),
        (
            "hl_metamodel",
            {
                "nav_guard_triggers": 1,
                "positions": {"ETH": {"hl_weight": 0.20, "cex_weight": -0.20, "cex_legs": {"bybit": -0.20}}},
            },
        ),
    ):
        d = tmp_path / sub
        d.mkdir()
        st = {"starting_capital_usd": 100_000.0, "equity_usd": gross_eq[-1], **st_extra}
        (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
        pd.DataFrame(
            {
                "tick_ms": [int(dt.timestamp() * 1000) for dt in days],
                "equity_usd": gross_eq,
                "gross_notional_usd": [40_000.0] * len(days),
            }
        ).to_parquet(d / "ticks.parquet", index=False)

    read_counts: dict[Path, int] = {}
    orig_read_parquet = pd.read_parquet

    def counting_read_parquet(path, *args, **kwargs):
        p = Path(path).resolve()
        read_counts[p] = read_counts.get(p, 0) + 1
        return orig_read_parquet(path, *args, **kwargs)

    monkeypatch.setattr(pd, "read_parquet", counting_read_parquet)
    payload = ar.build_desk_analytics_payload(tmp_path)

    # M-18: each ticks.parquet read strictly ONCE across leaderboard + risk_exposure
    assert len(read_counts) == 3
    assert all(cnt == 1 for cnt in read_counts.values()), read_counts

    # H-19: net streaks computed alongside gross streaks
    cd_lb = next(r for r in payload["leaderboard"] if r["id"] == "cross_dex")
    assert cd_lb["positive_days_pct"] == 100.0
    assert cd_lb["longest_win_streak"] == 10
    assert cd_lb["positive_days_net_pct"] == 0.0
    assert cd_lb["longest_loss_streak_net"] == 10
    assert cd_lb["longest_win_streak_net"] == 0

    # L-4: real net_exposure_pct, nav_guard_triggers, and standalone tier_usd when Autopilot is absent
    rx = payload["risk_exposure"]
    assert rx["total_nav_guard_triggers"] == 6
    by_strat = {r["strategy"]: r for r in rx["leverage_by_strategy"]}
    # Cross-DEX: hl_sgn=-1 * 20500 - (-1) * 19500 = -1000 USD on ~100,010 eq -> ~ -1.0%
    assert by_strat["Cross-DEX (HL×Lighter×Ext)"]["net_exposure_pct"] == pytest.approx(-1.0, abs=0.01)
    assert by_strat["Cross-DEX (HL×Lighter×Ext)"]["nav_guard_triggers"] == 3
    # HIP-3: 10400 - 9900 = +500 USD on ~100,010 eq -> ~ +0.5%
    assert by_strat["HIP-3 Split-Margin"]["net_exposure_pct"] == pytest.approx(0.5, abs=0.01)
    assert by_strat["HIP-3 Split-Margin"]["nav_guard_triggers"] == 2

    tiers = {r["label"]: r["usd"] for r in rx["asset_tier_concentration"]}
    assert "Crypto - Cross-DEX Neutral" in tiers
    assert "Crypto - Multi-Factor Neutral" in tiers
    assert "US Equities / HIP-3 Builders" in tiers


# --------------------------------------------------------------------------- 6. Nginx, Compose & CI/CD Workflows (M-4, M-5, M-6)


def test_m4_m5_m6_nginx_security_headers_compose_env_and_workflows():
    nginx_conf = (ROOT / "docker/nginx.conf").read_text(encoding="utf-8")
    for loc in ("location = /nginx-health", "location = /build-info.json", "location = /index.html", "location /assets/"):
        _, _, after = nginx_conf.partition(loc)
        block = after.split("}", 1)[0]
        assert 'add_header X-Content-Type-Options "nosniff" always;' in block, loc
        assert 'add_header X-Frame-Options "SAMEORIGIN" always;' in block, loc
        assert 'add_header Referrer-Policy "strict-origin-when-cross-origin" always;' in block, loc

    compose_local = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    compose_dok = yaml.safe_load((ROOT / "docker-compose.dokploy.yml").read_text(encoding="utf-8"))
    assert "HL_CORS_ORIGINS" in compose_local["services"]["backend"]["environment"]
    assert "HL_CORS_ORIGINS" in compose_dok["services"]["backend"]["environment"]
    worker_env = compose_dok["services"]["worker"]["environment"]
    for var in (
        "HL_MM_COINS",
        "HL_LIQ_TOP_WHALES",
        "HL_LIQ_TOP_FUNDING_COINS",
        "HL_MM_CALIBRATION_COINS",
        "HL_MM_CALIBRATION_SEC",
        "HL_SETTLEMENT_INTERVAL",
        "HL_SETTLEMENT_MAX_COINS",
    ):
        assert var in worker_env, var

    ci_wf = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    on_block = ci_wf.get("on") or ci_wf.get(True)
    push_paths = on_block["push"]["paths"]
    for p in ("scripts/**", "docker-compose.yml", "docker-compose.dokploy.yml", ".github/workflows/release.yml"):
        assert p in push_paths

    rel_text = (ROOT / ".github/workflows/release.yml").read_text(encoding="utf-8")
    rel_wf = yaml.safe_load(rel_text)
    assert 'grep -qi "x-content-type-options: nosniff"' in rel_text
    push_step = next(s for s in rel_wf["jobs"]["build-and-push"]["steps"] if "Push verified" in s.get("name", ""))
    assert 'if [ "$current_sha" = "$SHA" ]' in push_step["run"]
    tip_step = next(s for s in rel_wf["jobs"]["deploy-dokploy"]["steps"] if "Confirm SHA" in s.get("name", ""))
    assert "::notice" in tip_step["run"] and "exit 0" in tip_step["run"]
