from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import time

from typer.testing import CliRunner

from hl_quant.scheduler import WorkerScheduler, app, build_job_specs


def test_job_schedule_matching():
    jobs = {j.name: j for j in build_job_specs()}

    assert jobs["onchain_events"].should_run_at(datetime(2026, 11, 26, 14, 15, tzinfo=timezone.utc))
    assert not jobs["onchain_events"].should_run_at(datetime(2026, 11, 26, 14, 16, tzinfo=timezone.utc))

    assert jobs["market_snapshot"].should_run_at(datetime(2026, 11, 26, 7, 50, tzinfo=timezone.utc))
    assert not jobs["market_snapshot"].should_run_at(datetime(2026, 11, 26, 7, 51, tzinfo=timezone.utc))

    assert jobs["frontier_persistence"].should_run_at(datetime(2026, 11, 26, 8, 20, tzinfo=timezone.utc))
    assert not jobs["frontier_persistence"].should_run_at(datetime(2026, 11, 26, 9, 20, tzinfo=timezone.utc))

    assert jobs["testnet_attribution"].should_run_at(datetime(2026, 11, 26, 6, 0, tzinfo=timezone.utc))
    assert not jobs["testnet_attribution"].should_run_at(datetime(2026, 11, 26, 18, 0, tzinfo=timezone.utc))


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
    # Next poll arrives late at 08:16 (simulating a long job): must catch 08:05, 08:10, 08:15!
    caught = sched.tick_minute(datetime(2026, 11, 26, 8, 16, tzinfo=timezone.utc))
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
