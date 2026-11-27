from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from typer.testing import CliRunner

from hl_quant.scheduler import WorkerScheduler, app, build_job_specs


def test_job_schedule_matching(tmp_path: Path):
    jobs = {j.name: j for j in build_job_specs()}
    assert len(jobs) == 13

    assert jobs["onchain_events"].should_run_at(datetime(2026, 11, 26, 14, 15, tzinfo=timezone.utc))
    assert not jobs["onchain_events"].should_run_at(datetime(2026, 11, 26, 14, 16, tzinfo=timezone.utc))

    assert jobs["market_snapshot"].should_run_at(datetime(2026, 11, 26, 7, 50, tzinfo=timezone.utc))
    assert not jobs["market_snapshot"].should_run_at(datetime(2026, 11, 26, 7, 51, tzinfo=timezone.utc))

    assert jobs["frontier_persistence"].should_run_at(datetime(2026, 11, 26, 8, 20, tzinfo=timezone.utc))
    assert not jobs["frontier_persistence"].should_run_at(datetime(2026, 11, 26, 9, 20, tzinfo=timezone.utc))

    assert jobs["hip3_paper"].should_run_at(datetime(2026, 11, 26, 8, 22, tzinfo=timezone.utc))
    assert not jobs["hip3_paper"].should_run_at(datetime(2026, 11, 26, 9, 22, tzinfo=timezone.utc))

    assert jobs["liq_funding_alpha"].should_run_at(datetime(2026, 11, 26, 8, 35, tzinfo=timezone.utc))
    assert not jobs["liq_funding_alpha"].should_run_at(datetime(2026, 11, 26, 9, 35, tzinfo=timezone.utc))

    assert jobs["mm_calibration"].should_run_at(datetime(2026, 11, 26, 5, 18, tzinfo=timezone.utc))
    assert not jobs["mm_calibration"].should_run_at(datetime(2026, 11, 26, 17, 18, tzinfo=timezone.utc))

    assert jobs["settlement_study"].should_run_at(datetime(2026, 11, 26, 5, 34, tzinfo=timezone.utc))
    assert not jobs["settlement_study"].should_run_at(datetime(2026, 11, 26, 17, 34, tzinfo=timezone.utc))

    assert jobs["testnet_attribution"].should_run_at(datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc))
    assert not jobs["testnet_attribution"].should_run_at(datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc))

    # Settlement study must write report under writable data_dir (never read-only /app/docs)
    settle_cmd = jobs["settlement_study"].build_command(Path("/app"), tmp_path, False)
    assert "--report-path" in settle_cmd
    assert str(tmp_path / "paper/hl_funding_settlement_study/report.md") in settle_cmd


def test_testnet_execution_disabled_by_default(monkeypatch):
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    monkeypatch.delenv("HL_TESTNET_PRIVATE_KEY", raising=False)
    jobs = {j.name: j for j in build_job_specs()}
    enabled, reason = jobs["testnet_executor"].is_enabled()
    assert enabled is False
    assert "HL_ENABLE_TESTNET_EXECUTION" in reason


def test_worker_scheduler_runs_slot_once_and_writes_atomic_status(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)
    executed: list[list[str]] = []

    def fake_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        executed.append(cmd)
        return 0

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=fake_runner)
    slot = datetime(2026, 11, 26, 8, 20, tzinfo=timezone.utc)
    ran1 = sched.tick_minute(slot)
    ran2 = sched.tick_minute(slot)  # Same UTC minute must not re-fire
    assert ran1 == ["frontier_persistence"]
    assert ran2 == []
    assert len(executed) == 1
    assert "--data-root" in executed[0]

    status_file = tmp_path / "paper" / "hl_worker_status.json"
    assert status_file.is_file()
    status = json.loads(status_file.read_text(encoding="utf-8"))
    by_name = {j["name"]: j for j in status["jobs"]}
    assert by_name["frontier_persistence"]["state"] == "ok"
    assert by_name["frontier_persistence"]["runs_ok"] == 1
    assert by_name["testnet_executor"]["state"] == "disabled"


def test_worker_bootstrap_runs_only_readonly_eligible_jobs_on_empty_volume(tmp_path: Path):
    executed: list[str] = []

    def fake_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        executed.append(Path(cmd[1]).name)
        return 0

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=fake_runner)
    sched.run_bootstrap_readonly()
    assert "run_hl_frontier_persistence_tick.py" in executed
    assert "run_hl_onchain_event_tick.py" in executed
    assert "run_hl_testnet_executor.py" not in executed


def test_testnet_exec_requires_both_key_and_address_and_anchors_paths(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HL_ENABLE_TESTNET_EXECUTION", "true")
    monkeypatch.setenv("HL_TESTNET_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)

    jobs = {j.name: j for j in build_job_specs()}
    enabled, reason = jobs["testnet_executor"].is_enabled()
    assert enabled is False
    assert "HL_TESTNET_ACCOUNT_ADDRESS" in reason

    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1234")
    enabled2, _ = jobs["testnet_executor"].is_enabled()
    assert enabled2 is True

    exec_cmd = jobs["testnet_executor"].build_command(Path("/repo"), tmp_path, False)
    assert "--hwm-state" in exec_cmd
    assert str(tmp_path / "paper/hl_testnet_hwm.json") in exec_cmd

    mm_cmd = jobs["metamodel_paper"].build_command(Path("/repo"), tmp_path, False)
    assert "--attribution-json" in mm_cmd
    assert str(tmp_path / "paper/hl_testnet_attribution/attribution.json") in mm_cmd


def test_scheduler_catches_up_missed_slots_without_dropping_jobs(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    ran: list[str] = []

    def fake_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        ran.append(Path(cmd[1]).name)
        return 0

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=fake_runner)
    # Initial check at 08:04 (nothing due)
    assert sched.tick_minute(datetime(2026, 11, 26, 8, 4, tzinfo=timezone.utc)) == []
    # Next poll arrives late at 08:18 (simulating a long job): must catch 08:05, 08:10, 08:15, 08:17!
    caught = sched.tick_minute(datetime(2026, 11, 26, 8, 18, tzinfo=timezone.utc))
    assert "metamodel_paper" in caught
    assert "cross_dex_paper" in caught
    assert "onchain_events" in caught


def test_run_once_cli_respects_testnet_opt_in_gate(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    monkeypatch.delenv("HL_TESTNET_PRIVATE_KEY", raising=False)
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)
    runner = CliRunner()
    res = runner.invoke(app, ["--data-dir", str(tmp_path), "--run-once", "testnet_executor"])
    assert res.exit_code == 3
    assert "Refusing to run 'testnet_executor'" in res.output


def test_terminate_active_children_propagates_sigterm(tmp_path: Path):
    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False)
    import threading

    rc_holder: list[int] = []

    def _run() -> None:
        rc_holder.append(sched._subprocess_runner([sys.executable, "-c", "import time; time.sleep(30)"], {}, 60))

    t = threading.Thread(target=_run)
    t.start()
    deadline = time.monotonic() + 5.0
    while not sched._active_procs and time.monotonic() < deadline:
        time.sleep(0.05)
    assert len(sched._active_procs) == 1
    sched._stop_event.set()
    sched.terminate_active_children(grace_sec=3.0)
    t.join(timeout=5.0)
    assert not t.is_alive()
    assert rc_holder and rc_holder[0] != 0


def test_telegram_credentials_accept_both_standard_and_signal_env_names(tmp_path: Path, monkeypatch):
    from hl_quant.paper_tracker import load_telegram_credentials

    for k in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "TELEGRAM_THREAD_ID",
        "TELEGRAM_SIGNAL_BOT_TOKEN",
        "TELEGRAM_SIGNAL_CHAT_ID",
        "TELEGRAM_SIGNAL_THREAD_ID",
        "HL_TELEGRAM_ENABLED",
    ):
        monkeypatch.delenv(k, raising=False)

    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "bot123")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-10099")
    monkeypatch.setenv("TELEGRAM_THREAD_ID", "42")

    tok, chat, thread = load_telegram_credentials()
    assert (tok, chat, thread) == ("bot123", "-10099", "42")

    sched = WorkerScheduler(data_dir=tmp_path)
    assert sched.telegram_enabled is True

    # TELEGRAM_SIGNAL_* overrides TELEGRAM_* and also enables WorkerScheduler
    monkeypatch.setenv("TELEGRAM_SIGNAL_BOT_TOKEN", "sig999")
    monkeypatch.setenv("TELEGRAM_SIGNAL_CHAT_ID", "-10077")
    monkeypatch.setenv("TELEGRAM_SIGNAL_THREAD_ID", "7")
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    assert load_telegram_credentials() == ("sig999", "-10077", "7")
    sched2 = WorkerScheduler(data_dir=tmp_path)
    assert sched2.telegram_enabled is True


def test_bootstrap_seeds_only_missing_readonly_artifacts(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)
    # Pre-create frontier_history sentinel; metamodel_paper is still missing and must be seeded!
    frontier = tmp_path / "paper" / "hl_frontier_history" / "latest_summary.json"
    frontier.parent.mkdir(parents=True, exist_ok=True)
    frontier.write_text("{}", encoding="utf-8")

    executed: list[str] = []

    def fake_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        executed.append(Path(cmd[1]).name)
        return 0

    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False, runner=fake_runner)
    sched.run_bootstrap_readonly()
    assert "run_hl_frontier_persistence_tick.py" not in executed
    assert "run_hl_hip3_paper_tick.py" in executed
    assert "run_hl_metamodel_paper_tick.py" in executed
    assert "run_hl_liquidation_and_funding_alpha.py" in executed
    assert "run_hl_mm_calibration.py" in executed
    assert "run_hl_funding_settlement_study.py" in executed
    assert "run_hl_testnet_attribution.py" not in executed  # disabled without address
    assert "run_hl_testnet_executor.py" not in executed


def test_readonly_job_retries_once_while_testnet_executor_never_retries(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HL_ENABLE_TESTNET_EXECUTION", "true")
    monkeypatch.setenv("HL_TESTNET_PRIVATE_KEY", "0xdeadbeef")
    monkeypatch.setenv("HL_TESTNET_ACCOUNT_ADDRESS", "0x1234")

    attempts: dict[str, int] = {}

    def flaky_runner(cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        script = Path(cmd[1]).name
        attempts[script] = attempts.get(script, 0) + 1
        if script == "run_hl_onchain_event_tick.py":
            return 1 if attempts[script] == 1 else 0
        return 1  # testnet_executor fails on attempt 1 and must NOT retry

    sched = WorkerScheduler(
        data_dir=tmp_path,
        telegram_enabled=False,
        runner=flaky_runner,
        readonly_retries=1,
        readonly_retry_delay_s=0.0,
    )
    jobs = {j.name: j for j in sched.jobs}
    assert sched.run_job(jobs["onchain_events"]) == 0
    assert attempts["run_hl_onchain_event_tick.py"] == 2

    assert sched.run_job(jobs["testnet_executor"]) == 1
    assert attempts["run_hl_testnet_executor.py"] == 1
    status = json.loads((tmp_path / "paper" / "hl_worker_status.json").read_text(encoding="utf-8"))
    by_name = {j["name"]: j for j in status["jobs"]}
    assert by_name["onchain_events"]["last_error"] is None
    assert by_name["testnet_executor"]["last_error"] == "exit_code=1"


def test_worker_preserves_last_known_job_state_across_restarts(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("HL_ENABLE_TESTNET_EXECUTION", raising=False)
    monkeypatch.delenv("HL_TESTNET_ACCOUNT_ADDRESS", raising=False)
    status_path = tmp_path / "paper" / "hl_worker_status.json"
    status_path.parent.mkdir(parents=True, exist_ok=True)
    status_path.write_text(
        json.dumps(
            {
                "service": "hl-worker",
                "jobs": [
                    {"name": "market_snapshot", "state": "ok", "last_exit_code": 0, "runs_ok": 12, "runs_failed": 0},
                    {"name": "onchain_events", "state": "failed", "last_exit_code": 1, "runs_ok": 5, "runs_failed": 1, "last_error": "boom"},
                    {"name": "metamodel_paper", "state": "running", "last_exit_code": 0, "runs_ok": 3, "runs_failed": 0},
                ],
            }
        ),
        encoding="utf-8",
    )
    sched = WorkerScheduler(data_dir=tmp_path, telegram_enabled=False)
    published = sched.publish_status()
    by_name = {j["name"]: j for j in published["jobs"]}
    assert by_name["market_snapshot"]["state"] == "ok"
    assert by_name["onchain_events"]["state"] == "failed"
    assert by_name["metamodel_paper"]["state"] == "ok"
    assert by_name["cross_dex_paper"]["state"] == "idle"
    assert by_name["testnet_executor"]["state"] == "disabled"


def test_worker_sends_cooldown_gated_failure_and_recovery_alerts(tmp_path: Path):
    outcomes = [1, 1, 0, 0]  # fail #1 (alert), fail #2 within cooldown (suppressed), ok #3 (recovery), ok #4 (quiet)
    sent_alerts: list[str] = []

    def scripted_runner(_cmd: list[str], _env: dict[str, str], _timeout: int) -> int:
        return outcomes.pop(0)

    sched = WorkerScheduler(
        data_dir=tmp_path,
        telegram_enabled=True,
        runner=scripted_runner,
        readonly_retries=0,
        alert_cooldown_s=3600.0,
        alert_sender=sent_alerts.append,
    )
    job = next(j for j in sched.jobs if j.name == "onchain_events")

    assert sched.run_job(job) == 1
    assert len(sent_alerts) == 1
    assert "сбой onchain_events" in sent_alerts[0]

    # Second consecutive failure within 1h cooldown is suppressed
    assert sched.run_job(job) == 1
    assert len(sent_alerts) == 1

    # Recovery sends a single green recovery alert
    assert sched.run_job(job) == 0
    assert len(sent_alerts) == 2
    assert "восстановлен onchain_events" in sent_alerts[1]

    # Subsequent healthy run sends no alert
    assert sched.run_job(job) == 0
    assert len(sent_alerts) == 2
