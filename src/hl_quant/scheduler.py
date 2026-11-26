"""Container-native UTC job scheduler replacing local macOS crons for Docker / Dokploy.

Runs the 7 scheduled Hyperliquid research/paper/testnet jobs plus 2 read-only dashboard scanners
in bounded subprocesses with non-overlapping locks, atomic ``hl_worker_status.json`` telemetry,
safe default gating (``HL_ENABLE_TESTNET_EXECUTION=false``), and graceful ``SIGTERM`` shutdown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any, Callable

import typer

ROOT_DIR = Path(__file__).resolve().parents[2]


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class JobSpec:
    name: str
    description: str
    cron_expr: str
    minutes: tuple[int, ...]
    hours: tuple[int, ...] | None  # None = every hour
    timeout_s: int
    readonly: bool
    bootstrap_eligible: bool
    build_command: Callable[[Path, Path, bool], list[str]]
    is_enabled: Callable[[], tuple[bool, str]]

    def should_run_at(self, dt_utc: datetime) -> bool:
        if dt_utc.minute not in self.minutes:
            return False
        if self.hours is not None and dt_utc.hour not in self.hours:
            return False
        return True

    def next_run_utc(self, after_utc: datetime) -> datetime:
        cursor = after_utc.replace(second=0, microsecond=0) + timedelta(minutes=1)
        for _ in range(60 * 24 + 2):
            if self.should_run_at(cursor):
                return cursor
            cursor += timedelta(minutes=1)
        return cursor


def _default_enabled() -> tuple[bool, str]:
    return True, "active"


def _testnet_exec_enabled() -> tuple[bool, str]:
    if not _env_flag("HL_ENABLE_TESTNET_EXECUTION", False):
        return False, "disabled (set HL_ENABLE_TESTNET_EXECUTION=true to enable)"
    if not os.environ.get("HL_TESTNET_PRIVATE_KEY", "").strip():
        return False, "disabled (missing HL_TESTNET_PRIVATE_KEY)"
    return True, "active"


def _testnet_attr_enabled() -> tuple[bool, str]:
    if not os.environ.get("HL_TESTNET_ACCOUNT_ADDRESS", "").strip():
        return False, "disabled (missing HL_TESTNET_ACCOUNT_ADDRESS)"
    return True, "active"


def build_job_specs() -> list[JobSpec]:
    """Return the canonical ordered list of scheduled Hyperliquid container jobs."""
    py = sys.executable

    def _onchain_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_onchain_event_tick.py"),
            "--state-dir",
            str(data_dir / "paper/hl_onchain_events"),
        ]
        if tg:
            cmd.append("--telegram-on-alert")
        return cmd

    def _snapshot_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        coins = os.environ.get(
            "HL_SNAPSHOT_COINS",
            "BTC,ETH,SOL,HYPE,DOGE,XRP,SUI,ENA,WIF,kPEPE,AVAX,LINK,TAO",
        )
        return [
            py,
            str(root / "scripts/analytics/collect_hyperliquid_market_data.py"),
            "snapshot",
            "--coins",
            coins,
            "--no-trades",
            "--output-root",
            str(data_dir / "raw/hyperliquid"),
        ]

    def _metamodel_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_metamodel_paper_tick.py"),
            "--state-dir",
            str(data_dir / "paper/hl_metamodel"),
            "--max-coins",
            os.environ.get("HL_PAPER_MAX_COINS", "35"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    def _testnet_exec_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_testnet_executor.py"),
            "--state-json",
            str(data_dir / "paper/hl_metamodel/state.json"),
            "--audit-log",
            str(data_dir / "paper/hl_testnet_audit.jsonl"),
            "--execute-testnet",
            "--max-child-usd",
            os.environ.get("HL_TESTNET_MAX_CHILD_USD", "250"),
            "--neutralize-net",
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    def _cross_dex_paper_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_cross_dex_paper_tick.py"),
            "--state-dir",
            str(data_dir / "paper/hl_cross_dex_paper"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    def _frontier_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_hl_frontier_persistence_tick.py"),
            "--data-root",
            str(data_dir / "paper"),
        ]

    def _cross_dex_scan_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_cross_dex_arb_scan.py"),
            "--output-dir",
            str(data_dir / "paper/hl_cross_dex_v2"),
        ]

    def _hyperevm_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_hl_hyperevm_defi_scan.py"),
            "--output-dir",
            str(data_dir / "paper/hl_hyperevm_defi"),
        ]

    def _attribution_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_testnet_attribution.py"),
            "--audit-log",
            str(data_dir / "paper/hl_testnet_audit.jsonl"),
            "--paper-dir",
            str(data_dir / "paper/hl_metamodel"),
            "--output-dir",
            str(data_dir / "paper/hl_testnet_attribution"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    four_hour = (0, 4, 8, 12, 16, 20)
    return [
        JobSpec(
            name="onchain_events",
            description="H2 Whale Liq + H4 HyperEVM + H6 Funding Regime + HLP PnL (15m)",
            cron_expr="*/15 * * * *",
            minutes=(0, 15, 30, 45),
            hours=None,
            timeout_s=300,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_onchain_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="market_snapshot",
            description="Hyperliquid L2 + predicted fundings snapshot (hourly :50)",
            cron_expr="50 * * * *",
            minutes=(50,),
            hours=None,
            timeout_s=180,
            readonly=True,
            bootstrap_eligible=False,
            build_command=_snapshot_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="metamodel_paper",
            description="HL x Bybit Metamodel L2 Paper Tick (4h :05)",
            cron_expr="5 */4 * * *",
            minutes=(5,),
            hours=four_hour,
            timeout_s=360,
            readonly=True,
            bootstrap_eligible=False,
            build_command=_metamodel_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="testnet_executor",
            description="Hyperliquid Testnet ALO Rebalance Executor (4h :10, opt-in)",
            cron_expr="10 */4 * * *",
            minutes=(10,),
            hours=four_hour,
            timeout_s=300,
            readonly=False,
            bootstrap_eligible=False,
            build_command=_testnet_exec_cmd,
            is_enabled=_testnet_exec_enabled,
        ),
        JobSpec(
            name="cross_dex_paper",
            description="HL x Lighter/Extended Dual-Leg Paper Tick (4h :15)",
            cron_expr="15 */4 * * *",
            minutes=(15,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_cross_dex_paper_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="frontier_persistence",
            description="HIP-3 + Pendle Boros + HyperEVM Yield Stack Persistence (4h :20)",
            cron_expr="20 */4 * * *",
            minutes=(20,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_frontier_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="cross_dex_scan",
            description="5-DEX Funding & L2 Depth Opportunity Scan (4h :25)",
            cron_expr="25 */4 * * *",
            minutes=(25,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_cross_dex_scan_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="hyperevm_defi_scan",
            description="HyperEVM Chain 999 L1Read + V3 Depth + HLP Scan (4h :30)",
            cron_expr="30 */4 * * *",
            minutes=(30,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_hyperevm_cmd,
            is_enabled=_default_enabled,
        ),
        JobSpec(
            name="testnet_attribution",
            description="Daily Testnet Post-Trade Attribution, ADL Rank & Quota Audit (06:00 UTC)",
            cron_expr="0 6 * * *",
            minutes=(0,),
            hours=(6,),
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=False,
            build_command=_attribution_cmd,
            is_enabled=_testnet_attr_enabled,
        ),
    ]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


class WorkerScheduler:
    """Single-process UTC scheduler executing isolated subprocess ticks and publishing atomic status."""

    def __init__(
        self,
        *,
        root_dir: Path = ROOT_DIR,
        data_dir: Path | None = None,
        jobs: list[JobSpec] | None = None,
        telegram_enabled: bool | None = None,
        runner: Callable[[list[str], dict[str, str], int], int] | None = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.data_dir = Path(data_dir if data_dir is not None else os.environ.get("HL_DATA_DIR", str(self.root_dir / "data")))
        self.paper_dir = self.data_dir / "paper"
        self.status_path = self.paper_dir / "hl_worker_status.json"
        self.jobs = jobs if jobs is not None else build_job_specs()
        self.telegram_enabled = (
            telegram_enabled
            if telegram_enabled is not None
            else (
                _env_flag("HL_TELEGRAM_ENABLED", True)
                and bool(os.environ.get("TELEGRAM_BOT_TOKEN", "").strip())
                and bool(os.environ.get("TELEGRAM_CHAT_ID", "").strip())
            )
        )
        self._runner = runner or self._subprocess_runner
        self.started_at_utc = datetime.now(tz=timezone.utc)
        self._stop_requested = False
        self._state: dict[str, dict[str, Any]] = {}
        self._last_slot_run: dict[str, str] = {}
        self._load_existing_state()

    def _load_existing_state(self) -> None:
        existing: dict[str, dict[str, Any]] = {}
        if self.status_path.is_file():
            try:
                raw = json.loads(self.status_path.read_text(encoding="utf-8"))
                for item in raw.get("jobs") or []:
                    if isinstance(item, dict) and item.get("name"):
                        existing[str(item["name"])] = item
            except Exception:
                pass
        now = datetime.now(tz=timezone.utc)
        for job in self.jobs:
            enabled, reason = job.is_enabled()
            prev = existing.get(job.name, {})
            self._state[job.name] = {
                "name": job.name,
                "description": job.description,
                "cron_expr": job.cron_expr,
                "readonly": job.readonly,
                "enabled": enabled,
                "status_reason": reason,
                "state": "idle" if enabled else "disabled",
                "last_started_at_utc": prev.get("last_started_at_utc"),
                "last_finished_at_utc": prev.get("last_finished_at_utc"),
                "last_exit_code": prev.get("last_exit_code"),
                "last_duration_s": prev.get("last_duration_s"),
                "runs_ok": int(prev.get("runs_ok", 0)),
                "runs_failed": int(prev.get("runs_failed", 0)),
                "next_run_utc": job.next_run_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
            }

    @staticmethod
    def _subprocess_runner(cmd: list[str], env: dict[str, str], timeout_s: int) -> int:
        try:
            proc = subprocess.run(cmd, env=env, timeout=timeout_s, check=False)
            return int(proc.returncode)
        except subprocess.TimeoutExpired:
            print(f"[hl-worker] job timed out after {timeout_s}s: {' '.join(cmd[:3])}", flush=True)
            return 124
        except Exception as exc:
            print(f"[hl-worker] job failed to spawn: {exc}", flush=True)
            return 1

    def publish_status(self, now_utc: datetime | None = None) -> dict[str, Any]:
        now = now_utc or datetime.now(tz=timezone.utc)
        for job in self.jobs:
            enabled, reason = job.is_enabled()
            entry = self._state[job.name]
            entry["enabled"] = enabled
            entry["status_reason"] = reason
            if not enabled and entry["state"] != "running":
                entry["state"] = "disabled"
            entry["next_run_utc"] = job.next_run_utc(now).strftime("%Y-%m-%d %H:%M UTC")
        payload = {
            "service": "hl-worker",
            "build_sha": os.environ.get("HL_BUILD_SHA", "dev"),
            "started_at_utc": self.started_at_utc.strftime("%Y-%m-%d %H:%M UTC"),
            "heartbeat_utc": now.strftime("%Y-%m-%d %H:%M:%S UTC"),
            "telegram_enabled": self.telegram_enabled,
            "testnet_execution_enabled": _env_flag("HL_ENABLE_TESTNET_EXECUTION", False),
            "jobs": [self._state[j.name] for j in self.jobs],
        }
        _write_json_atomic(self.status_path, payload)
        return payload

    def run_job(self, job: JobSpec, *, force: bool = False) -> int:
        enabled, reason = job.is_enabled()
        entry = self._state[job.name]
        if not enabled and not force:
            entry["enabled"] = False
            entry["status_reason"] = reason
            entry["state"] = "disabled"
            self.publish_status()
            return 0

        start_dt = datetime.now(tz=timezone.utc)
        t0 = time.monotonic()
        entry["state"] = "running"
        entry["last_started_at_utc"] = start_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        self.publish_status(start_dt)

        cmd = job.build_command(self.root_dir, self.data_dir, self.telegram_enabled)
        env = dict(os.environ)
        src_path = str(self.root_dir / "src")
        env["PYTHONPATH"] = f"{src_path}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_path
        env["PYTHONUNBUFFERED"] = "1"

        print(f"[hl-worker] starting {job.name} ({job.cron_expr})", flush=True)
        exit_code = self._runner(cmd, env, job.timeout_s)
        duration = round(time.monotonic() - t0, 2)
        end_dt = datetime.now(tz=timezone.utc)

        entry["state"] = "ok" if exit_code == 0 else "failed"
        entry["last_finished_at_utc"] = end_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
        entry["last_exit_code"] = exit_code
        entry["last_duration_s"] = duration
        if exit_code == 0:
            entry["runs_ok"] = int(entry["runs_ok"]) + 1
        else:
            entry["runs_failed"] = int(entry["runs_failed"]) + 1
        print(f"[hl-worker] finished {job.name} rc={exit_code} in {duration:.1f}s", flush=True)
        self.publish_status(end_dt)
        return exit_code

    def run_bootstrap_readonly(self) -> None:
        """Seed read-only snapshots on a fresh volume so the dashboard has live data immediately."""
        sentinel = self.paper_dir / "hl_frontier_history" / "latest_summary.json"
        if sentinel.is_file():
            return
        print("[hl-worker] fresh data volume detected; bootstrapping read-only scans...", flush=True)
        for job in self.jobs:
            if self._stop_requested:
                break
            if job.bootstrap_eligible and job.readonly:
                self.run_job(job)

    def tick_minute(self, now_utc: datetime) -> list[str]:
        """Run any scheduled jobs matching ``now_utc`` (at most once per UTC minute slot)."""
        slot_key = now_utc.strftime("%Y-%m-%d %H:%M")
        ran: list[str] = []
        for job in self.jobs:
            if self._stop_requested:
                break
            if job.should_run_at(now_utc) and self._last_slot_run.get(job.name) != slot_key:
                self._last_slot_run[job.name] = slot_key
                self.run_job(job)
                ran.append(job.name)
        self.publish_status(now_utc)
        return ran

    def run_forever(self, *, bootstrap: bool = True, poll_seconds: float = 15.0) -> None:
        def _handle_stop(signum: int, _frame: Any) -> None:
            print(f"[hl-worker] received signal {signum}; shutting down gracefully...", flush=True)
            self._stop_requested = True

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

        self.publish_status()
        if bootstrap:
            self.run_bootstrap_readonly()

        print(f"[hl-worker] scheduler active (data_dir={self.data_dir}, jobs={len(self.jobs)})", flush=True)
        while not self._stop_requested:
            now_utc = datetime.now(tz=timezone.utc)
            self.tick_minute(now_utc)
            # Sleep in short increments so SIGTERM exits promptly
            deadline = time.monotonic() + poll_seconds
            while not self._stop_requested and time.monotonic() < deadline:
                time.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
        self.publish_status()


app = typer.Typer(add_completion=False, help="Hyperliquid Quant DeFi container worker scheduler.")


@app.command()
def main(
    data_dir: Path | None = typer.Option(None, help="Override data directory (default: $HL_DATA_DIR or ./data)"),
    list_jobs: bool = typer.Option(False, "--list", help="List configured scheduled jobs and exit"),
    run_once: str | None = typer.Option(None, "--run-once", help="Execute one named job and exit"),
    bootstrap_only: bool = typer.Option(False, "--bootstrap-only", help="Run read-only bootstrap jobs and exit"),
    no_bootstrap: bool = typer.Option(False, "--no-bootstrap", help="Skip fresh-volume bootstrap on startup"),
) -> None:
    sched = WorkerScheduler(data_dir=data_dir)
    if list_jobs:
        for job in sched.jobs:
            enabled, reason = job.is_enabled()
            typer.echo(f"{job.name:22s} | {job.cron_expr:12s} | enabled={str(enabled):5s} | {reason}")
        raise typer.Exit(0)
    if run_once is not None:
        match = next((j for j in sched.jobs if j.name == run_once), None)
        if match is None:
            valid = ", ".join(j.name for j in sched.jobs)
            typer.echo(f"Unknown job '{run_once}'. Valid jobs: {valid}", err=True)
            raise typer.Exit(2)
        rc = sched.run_job(match, force=True)
        raise typer.Exit(rc)
    if bootstrap_only:
        sched.run_bootstrap_readonly()
        raise typer.Exit(0)

    bootstrap = (not no_bootstrap) and _env_flag("HL_WORKER_BOOTSTRAP_READONLY", True)
    sched.run_forever(bootstrap=bootstrap)


if __name__ == "__main__":
    app()
