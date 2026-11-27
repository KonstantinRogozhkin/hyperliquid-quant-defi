from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "cutover_local_crons_to_dokploy.py"
_SPEC = importlib.util.spec_from_file_location("cutover_local_crons_to_dokploy", SCRIPT_PATH)
assert _SPEC and _SPEC.loader
cutover = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = cutover
_SPEC.loader.exec_module(cutover)


SAMPLE_CRONTAB = """\
*/30 * * * * /Users/konstantin/Projects/API-TESTER/scripts/cron_monitor.sh
# telegram-mcp paid_members daily sync
0 12 * * * /Users/konstantin/Projects/tg-mcp/telegram-mcp/scripts/ops/sync_paid_members.sh
# BEGIN hyperliquid-quant-defi snapshot schedule
50 * * * * zsh /Users/konstantin/AllProjects/Spy2Money/hyperliquid-quant-defi/scripts/ops/run_hyperliquid_snapshot_tick.sh
# END hyperliquid-quant-defi snapshot schedule
# BEGIN hyperliquid-quant-defi metamodel paper schedule
5 */4 * * * zsh /Users/konstantin/AllProjects/Spy2Money/hyperliquid-quant-defi/scripts/ops/run_hl_metamodel_paper_tick.sh
# END hyperliquid-quant-defi metamodel paper schedule
# BEGIN hyperliquid-quant-defi testnet executor schedule
10 */4 * * * zsh /Users/konstantin/AllProjects/Spy2Money/hyperliquid-quant-defi/scripts/ops/run_hl_testnet_executor_tick.sh
# END hyperliquid-quant-defi testnet executor schedule
# BEGIN hyperliquid-quant-defi onchain event alpha schedule
*/15 * * * * zsh /Users/konstantin/AllProjects/Spy2Money/hyperliquid-quant-defi/scripts/ops/run_hl_onchain_event_tick.sh
# END hyperliquid-quant-defi onchain event alpha schedule
"""


def _make_fetcher(
    *,
    fe_sha: str = "a" * 40,
    be_sha: str = "a" * 40,
    worker_sha: str = "a" * 40,
    heartbeat_age_sec: float = 15.0,
    health_status: str = "ok",
    worker_missing: bool = False,
    testnet_enabled: bool = False,
    failed_job: str | None = None,
    now_utc: datetime | None = None,
):
    now = now_utc or datetime(2026, 11, 26, 22, 0, 0, tzinfo=timezone.utc)
    hb = (now - timedelta(seconds=heartbeat_age_sec)).strftime("%Y-%m-%d %H:%M:%S UTC")

    jobs = []
    for name in cutover.DEFAULT_REQUIRED_JOBS:
        jobs.append(
            {
                "name": name,
                "enabled": True,
                "status_reason": "active",
                "state": "failed" if name == failed_job else "ok",
                "last_exit_code": 1 if name == failed_job else 0,
            }
        )
    jobs.append(
        {
            "name": "testnet_executor",
            "enabled": testnet_enabled,
            "status_reason": "active" if testnet_enabled else "disabled",
            "state": "ok" if testnet_enabled else "disabled",
            "last_exit_code": 0 if testnet_enabled else None,
        }
    )

    def _fetch(url: str) -> dict[str, Any]:
        if url.endswith("/api/v1/healthz"):
            return {"status": health_status, "service": "hl-backend", "sha": be_sha}
        if url.endswith("/api/v1/build-info.json"):
            return {"service": "hl-backend", "sha": be_sha, "built_at": hb}
        if url.endswith("/build-info.json"):
            return {"service": "hl-frontend", "sha": fe_sha, "built_at": hb}
        if url.endswith("/api/v1/scheduler/status"):
            return {
                "build_info": {"service": "hl-backend", "sha": be_sha},
                "worker_status": None
                if worker_missing
                else {
                    "service": "hl-worker",
                    "build_sha": worker_sha,
                    "heartbeat_utc": hb,
                    "telegram_enabled": True,
                    "testnet_execution_enabled": testnet_enabled,
                    "jobs": jobs,
                },
            }
        raise AssertionError(f"Unexpected URL: {url}")

    return _fetch, now


def test_verify_dokploy_stack_passes_on_healthy_live_worker() -> None:
    fetcher, now = _make_fetcher(testnet_enabled=True)
    res = cutover.verify_dokploy_stack(
        "https://hl.example.com/",
        expected_sha="a" * 40,
        expect_testnet_worker=True,
        fetcher=fetcher,
        now_utc=now,
    )
    assert res.public_url == "https://hl.example.com"
    assert res.backend_sha == "a" * 40
    assert res.testnet_execution_enabled is True
    assert res.warnings == []


def test_verify_dokploy_stack_rejects_stale_heartbeat_or_failed_job() -> None:
    stale_fetcher, now = _make_fetcher(heartbeat_age_sec=601.0)
    with pytest.raises(RuntimeError, match="heartbeat is stale"):
        cutover.verify_dokploy_stack("https://hl.example.com", max_heartbeat_age_sec=300.0, fetcher=stale_fetcher, now_utc=now)

    failed_fetcher, now = _make_fetcher(failed_job="onchain_events")
    with pytest.raises(RuntimeError, match="onchain_events.*failed"):
        cutover.verify_dokploy_stack("https://hl.example.com", fetcher=failed_fetcher, now_utc=now)

    missing_worker_fetcher, now = _make_fetcher(worker_missing=True)
    with pytest.raises(RuntimeError, match="no worker_status"):
        cutover.verify_dokploy_stack("https://hl.example.com", fetcher=missing_worker_fetcher, now_utc=now)


def test_verify_dokploy_stack_rejects_sha_mismatch_and_readonly_testnet_expectation() -> None:
    mismatch_fetcher, now = _make_fetcher(fe_sha="a" * 40, be_sha="b" * 40)
    with pytest.raises(RuntimeError, match="SHA mismatch"):
        cutover.verify_dokploy_stack("https://hl.example.com", fetcher=mismatch_fetcher, now_utc=now)

    readonly_fetcher, now = _make_fetcher(testnet_enabled=False)
    with pytest.raises(RuntimeError, match="testnet_execution_enabled=false"):
        cutover.verify_dokploy_stack(
            "https://hl.example.com",
            expect_testnet_worker=True,
            fetcher=readonly_fetcher,
            now_utc=now,
        )


def test_transform_crontab_comment_mode_is_idempotent_and_preserves_unrelated_jobs() -> None:
    plan = cutover.transform_crontab(SAMPLE_CRONTAB, mode="comment", date_tag="2026-11-26")
    assert plan.changed_count == 4
    assert plan.preserved_testnet_local is False
    # Unrelated crons untouched
    assert "*/30 * * * * /Users/konstantin/Projects/API-TESTER/scripts/cron_monitor.sh" in plan.updated_text
    assert "0 12 * * * /Users/konstantin/Projects/tg-mcp/telegram-mcp/scripts/ops/sync_paid_members.sh" in plan.updated_text
    # All HL lines commented
    assert "# Disabled 2026-11-26 (cutover to Dokploy worker): 50 * * * *" in plan.updated_text
    assert "# Disabled 2026-11-26 (cutover to Dokploy worker): 10 */4 * * *" in plan.updated_text

    # Re-running is strictly a no-op (never double-comments)
    second = cutover.transform_crontab(plan.updated_text, mode="comment", date_tag="2026-11-26")
    assert second.changed_count == 0
    assert second.updated_text == plan.updated_text


def test_transform_crontab_keep_local_testnet_and_remove_mode() -> None:
    plan = cutover.transform_crontab(SAMPLE_CRONTAB, mode="remove", keep_local_testnet=True)
    assert plan.changed_count == 3
    assert plan.preserved_testnet_local is True
    assert "run_hl_testnet_executor_tick.sh" in plan.updated_text
    assert "run_hyperliquid_snapshot_tick.sh" not in plan.updated_text
    assert "run_hl_metamodel_paper_tick.sh" not in plan.updated_text
    assert "run_hl_onchain_event_tick.sh" not in plan.updated_text
    assert "cron_monitor.sh" in plan.updated_text


def test_cli_dry_run_apply_and_restore_roundtrip(tmp_path: Path) -> None:
    state = {"crontab": SAMPLE_CRONTAB}
    fetcher, _ = _make_fetcher(testnet_enabled=True, now_utc=datetime.now(timezone.utc))

    def _read() -> str:
        return state["crontab"]

    def _write(new_text: str) -> None:
        state["crontab"] = new_text

    # 1. Dry-run does not modify crontab
    rc = cutover.main(
        ["--public-url", "https://hl.example.com", "--backup-dir", str(tmp_path)],
        crontab_reader=_read,
        crontab_writer=_write,
        fetcher=fetcher,
    )
    assert rc == 0
    assert state["crontab"] == SAMPLE_CRONTAB
    assert list(tmp_path.glob("crontab_*.txt")) == []

    # 2. Apply creates backup and comments out all 4 HL crons
    rc = cutover.main(
        ["--public-url", "https://hl.example.com", "--backup-dir", str(tmp_path), "--apply"],
        crontab_reader=_read,
        crontab_writer=_write,
        fetcher=fetcher,
    )
    assert rc == 0
    backups = list(tmp_path.glob("crontab_*.txt"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == SAMPLE_CRONTAB
    assert "# Disabled " in state["crontab"]

    # 3. Restore --apply restores exact original crontab
    rc = cutover.main(
        ["--restore", str(backups[0]), "--backup-dir", str(tmp_path), "--apply"],
        crontab_reader=_read,
        crontab_writer=_write,
        fetcher=fetcher,
    )
    assert rc == 0
    assert state["crontab"] == SAMPLE_CRONTAB


def test_write_local_crontab_uses_temp_file_and_handles_tcc_timeout(monkeypatch) -> None:
    import subprocess

    seen_paths: list[Path] = []

    def fake_run(cmd, **_kwargs):
        p = Path(cmd[1])
        seen_paths.append(p)
        assert p.is_file()
        assert p.read_text(encoding="utf-8") == "0 * * * * echo hi\n"
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(cutover.subprocess, "run", fake_run)
    cutover.write_local_crontab("0 * * * * echo hi\n", timeout_s=5.0)
    assert len(seen_paths) == 1
    assert not seen_paths[0].exists()  # temp file cleaned up

    def fake_timeout(cmd, **_kwargs):
        raise subprocess.TimeoutExpired(cmd, 5.0)

    monkeypatch.setattr(cutover.subprocess, "run", fake_timeout)
    with pytest.raises(RuntimeError, match="TCC"):
        cutover.write_local_crontab("0 * * * * echo hi\n", timeout_s=5.0)
