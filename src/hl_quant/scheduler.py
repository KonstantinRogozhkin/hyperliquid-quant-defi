"""Container-native UTC job scheduler replacing local macOS crons for Docker / Dokploy.

Runs the 10 core Hyperliquid research/paper/testnet jobs plus 6 read-only dashboard/analytics
scanners (16 UTC jobs total) in isolated subprocesses with:
- Catch-up slot evaluation + per-job non-overlapping worker threads (so a slow job at :05 never
  skips another job scheduled at :10)
- Atomic ``hl_worker_status.json`` telemetry
- Strict opt-in gating (``HL_ENABLE_TESTNET_EXECUTION=true`` + both key and address required)
- All state/HWM/attribution/report paths anchored under ``HL_DATA_DIR``
- Graceful ``SIGTERM`` propagation to active child process groups on container shutdown
"""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import html
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable

import typer

from hl_quant import paper_tracker as pt

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
    has_required_credentials: Callable[[], tuple[bool, str]]

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


def _testnet_exec_creds() -> tuple[bool, str]:
    if not os.environ.get("HL_TESTNET_PRIVATE_KEY", "").strip():
        return False, "disabled (missing HL_TESTNET_PRIVATE_KEY)"
    if not os.environ.get("HL_TESTNET_ACCOUNT_ADDRESS", "").strip():
        return False, "disabled (missing HL_TESTNET_ACCOUNT_ADDRESS)"
    return True, "credentials_ok"


def _testnet_exec_enabled() -> tuple[bool, str]:
    if not _env_flag("HL_ENABLE_TESTNET_EXECUTION", False):
        return False, "disabled (set HL_ENABLE_TESTNET_EXECUTION=true to enable)"
    creds_ok, reason = _testnet_exec_creds()
    if not creds_ok:
        return False, reason
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
            "--attribution-json",
            str(data_dir / "paper/hl_testnet_attribution/attribution.json"),
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
            "--hwm-state",
            str(data_dir / "paper/hl_testnet_hwm.json"),
            "--mm-audit-log",
            str(data_dir / "paper/hl_mm_testnet/mm_audit.jsonl"),
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

    def _hip3_paper_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_hip3_paper_tick.py"),
            "--state-dir",
            str(data_dir / "paper/hl_hip3_paper"),
            "--frontier-summary",
            str(data_dir / "paper/hl_frontier_history/latest_summary.json"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    def _spot_perp_paper_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_spot_perp_paper_tick.py"),
            "--state-dir",
            str(data_dir / "paper/hl_spot_perp_paper"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

    def _autopilot_cmd(root: Path, data_dir: Path, tg: bool) -> list[str]:
        cmd = [
            py,
            str(root / "scripts/analytics/run_hl_autopilot_tick.py"),
            "--data-root",
            str(data_dir / "paper"),
        ]
        if tg:
            cmd.append("--telegram")
        return cmd

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

    def _liq_funding_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_hl_liquidation_and_funding_alpha.py"),
            "--top-whales",
            os.environ.get("HL_LIQ_TOP_WHALES", "60"),
            "--top-funding-coins",
            os.environ.get("HL_LIQ_TOP_FUNDING_COINS", "8"),
            "--lookback-hours",
            "6",
            "--output-dir",
            str(data_dir / "paper/hl_liq_funding_alpha"),
        ]

    def _mm_calibration_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_hl_mm_calibration.py"),
            "--coins",
            os.environ.get("HL_MM_CALIBRATION_COINS", "BTC,ETH"),
            "--duration-sec",
            # >= 300s: 45s gave ~9 independent 5s windows per side, too few for a stable kappa fit.
            os.environ.get("HL_MM_CALIBRATION_SEC", "300"),
            "--ws-network",
            "mainnet",
            "--capture-dir",
            str(data_dir / "paper/hl_mm_calibration"),
        ]

    def _settlement_study_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        out_dir = data_dir / "paper/hl_funding_settlement_study"
        return [
            py,
            str(root / "scripts/analytics/run_hl_funding_settlement_study.py"),
            "--interval",
            os.environ.get("HL_SETTLEMENT_INTERVAL", "15m"),
            "--max-coins",
            os.environ.get("HL_SETTLEMENT_MAX_COINS", "35"),
            "--output-dir",
            str(out_dir),
            "--report-path",
            str(out_dir / "report.md"),
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

    def _risk_probe_cmd(root: Path, data_dir: Path, _tg: bool) -> list[str]:
        return [
            py,
            str(root / "scripts/analytics/run_hl_testnet_risk_probe.py"),
            "--hwm-state",
            str(data_dir / "paper/hl_testnet_hwm.json"),
            "--output-json",
            str(data_dir / "paper/hl_testnet_risk/latest.json"),
        ]

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
            has_required_credentials=_default_enabled,
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
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="metamodel_paper",
            description="HL x Bybit Metamodel L2 Paper Tick (4h :05)",
            cron_expr="5 */4 * * *",
            minutes=(5,),
            hours=four_hour,
            timeout_s=360,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_metamodel_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
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
            has_required_credentials=_testnet_exec_creds,
        ),
        JobSpec(
            name="cross_dex_paper",
            description="HL x Lighter/Extended Dual-Leg Paper Tick (4h :17)",
            cron_expr="17 */4 * * *",
            minutes=(17,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_cross_dex_paper_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
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
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="hip3_paper",
            description="HIP-3 Cross-Builder Split-Margin L2 Paper Tracker (4h :22)",
            cron_expr="22 */4 * * *",
            minutes=(22,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_hip3_paper_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
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
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="spot_perp_paper",
            description="HyperCore Native Spot+Perp Funding Arb + 10% HLP Paper Tick (4h :27)",
            cron_expr="27 */4 * * *",
            minutes=(27,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_spot_perp_paper_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="hyperevm_defi_scan",
            description="HyperEVM Chain 999 L1Read + V3 Depth + HLP Scan (4h :32)",
            cron_expr="32 */4 * * *",
            minutes=(32,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_hyperevm_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="liq_funding_alpha",
            description="H2 Whale Liquidation Clusters & :00 Funding Microstructure (4h :35)",
            cron_expr="35 */4 * * *",
            minutes=(35,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_liq_funding_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="autopilot_allocator",
            description="Neutral Autopilot Multi-Strategy Allocator + <=5% Crisis-CTA (4h :37)",
            cron_expr="37 */4 * * *",
            minutes=(37,),
            hours=four_hour,
            timeout_s=180,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_autopilot_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="mm_calibration",
            description="Avellaneda-Stoikov Empirical Kappa WS Calibration (daily 05:18 UTC)",
            cron_expr="18 5 * * *",
            minutes=(18,),
            hours=(5,),
            timeout_s=480,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_mm_calibration_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="settlement_study",
            description=":00 Hourly Funding Settlement OOS Study Refresh (daily 05:34 UTC)",
            cron_expr="34 5 * * *",
            minutes=(34,),
            hours=(5,),
            # ~35 coins x (5,000-bar candles + ~3 funding pages) ~ 8.5k info weight, paced at
            # 900 weight/min to stay under Hyperliquid's per-IP limit -> ~10 min of collection.
            timeout_s=1500,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_settlement_study_cmd,
            is_enabled=_default_enabled,
            has_required_credentials=_default_enabled,
        ),
        JobSpec(
            name="testnet_attribution",
            description="4h Testnet Post-Trade Attribution, Margin/Liq & Quota Audit (4h :22)",
            cron_expr="22 */4 * * *",
            minutes=(22,),
            hours=four_hour,
            timeout_s=240,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_attribution_cmd,
            is_enabled=_testnet_attr_enabled,
            has_required_credentials=_testnet_attr_enabled,
        ),
        JobSpec(
            name="testnet_risk_probe",
            description="Read-only Testnet live equity/drawdown/orphan & mark-vs-oracle risk probe (15m :02)",
            cron_expr="2-59/15 * * * *",
            minutes=(2, 17, 32, 47),
            hours=None,
            timeout_s=90,
            readonly=True,
            bootstrap_eligible=True,
            build_command=_risk_probe_cmd,
            is_enabled=_testnet_attr_enabled,
            has_required_credentials=_testnet_attr_enabled,
        ),
    ]


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def format_worker_alert_rich_html(
    *,
    job: JobSpec,
    recovered: bool,
    exit_code: int,
    duration_s: float,
    attempts: int,
    last_error: str | None,
    build_sha: str,
    observed_at_utc: str,
) -> str:
    """Format a concise Telegram Rich HTML operational alert for worker job failure or recovery."""
    icon = "🟢" if recovered else "🚨"
    title = f"hl-worker: восстановлен {job.name}" if recovered else f"hl-worker: сбой {job.name}"
    err_html = (
        f"<p><b>Ошибка:</b> <code>{html.escape(last_error[:240])}</code></p>"
        if (not recovered and last_error)
        else ""
    )
    return (
        f"<h1>{icon} {html.escape(title)}</h1>"
        f"<p><b>Время:</b> {html.escape(observed_at_utc)} · <b>SHA:</b> <code>{html.escape(build_sha[:12])}</code><br>"
        f"<b>Расписание:</b> <code>{html.escape(job.cron_expr)}</code> · "
        f"<b>Exit code:</b> <code>{exit_code}</code> · "
        f"<b>Попыток:</b> {attempts} · <b>Время:</b> {duration_s:.1f}s</p>"
        f"{err_html}"
        "<footer>Автоматический watchdog hl_quant.scheduler (1ч cooldown на повторные алерты)</footer>"
    )


class WorkerScheduler:
    """UTC scheduler executing isolated subprocess ticks with non-overlapping per-job concurrency."""

    DEFAULT_JOB_DEPENDENCIES: dict[str, tuple[str, float]] = {
        "testnet_executor": ("metamodel_paper", 750.0),
        "hip3_paper": ("frontier_persistence", 510.0),
    }

    def __init__(
        self,
        *,
        root_dir: Path = ROOT_DIR,
        data_dir: Path | None = None,
        jobs: list[JobSpec] | None = None,
        telegram_enabled: bool | None = None,
        runner: Callable[[list[str], dict[str, str], int], int] | None = None,
        max_workers: int = 6,
        readonly_retries: int = 1,
        readonly_retry_delay_s: float = 15.0,
        alert_cooldown_s: float = 3600.0,
        alert_sender: Callable[[str], Any] | None = None,
        dependency_wait_s: dict[str, tuple[str, float]] | None = None,
        dependency_poll_s: float = 0.25,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.data_dir = Path(
            data_dir if data_dir is not None else os.environ.get("HL_DATA_DIR", str(self.root_dir / "data"))
        )
        self.paper_dir = self.data_dir / "paper"
        self.status_path = self.paper_dir / "hl_worker_status.json"
        self.jobs = jobs if jobs is not None else build_job_specs()
        self.max_workers = max(1, int(max_workers))
        self.readonly_retries = max(0, int(readonly_retries))
        self.readonly_retry_delay_s = max(0.0, float(readonly_retry_delay_s))
        self.alert_cooldown_s = max(0.0, float(alert_cooldown_s))
        self._alert_sender = alert_sender
        self._job_dependencies = (
            dict(dependency_wait_s) if dependency_wait_s is not None else dict(self.DEFAULT_JOB_DEPENDENCIES)
        )
        self._dependency_poll_s = max(0.01, float(dependency_poll_s))
        self._last_failure_alert_mono: dict[str, float] = {}
        self._alerted_failed_jobs: set[str] = set()
        self._thread_local = threading.local()
        has_tg_token = bool(
            os.environ.get("TELEGRAM_SIGNAL_BOT_TOKEN", "").strip()
            or os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        )
        has_tg_chat = bool(
            os.environ.get("TELEGRAM_SIGNAL_CHAT_ID", "").strip()
            or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
        )
        self.telegram_enabled = (
            telegram_enabled
            if telegram_enabled is not None
            else (_env_flag("HL_TELEGRAM_ENABLED", True) and has_tg_token and has_tg_chat)
        )
        self._runner = runner or self._subprocess_runner
        self.started_at_utc = datetime.now(tz=timezone.utc)
        self._stop_event = threading.Event()
        self._status_lock = threading.Lock()
        self._procs_lock = threading.Lock()
        self._active_procs: set[subprocess.Popen[Any]] = set()
        self._job_locks: dict[str, threading.Lock] = {j.name: threading.Lock() for j in self.jobs}
        self._state: dict[str, dict[str, Any]] = {}
        self._last_slot_run: dict[str, str] = {}
        self._last_checked_minute: datetime | None = None
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
            prev_state = str(prev.get("state") or "")
            prev_rc = prev.get("last_exit_code")
            if not enabled:
                restored_state = "disabled"
            elif prev_state in {"ok", "failed"}:
                restored_state = prev_state
            elif prev_rc == 0:
                restored_state = "ok"
            elif prev_rc is not None:
                restored_state = "failed"
            else:
                restored_state = "idle"
            last_started = prev.get("last_started_at_utc")
            if isinstance(last_started, str) and len(last_started) >= 16:
                # Hydrate in-memory slot guard (`YYYY-MM-DD HH:MM`) so a container restart inside a scheduled
                # UTC minute never executes `testnet_executor` or paper ledgers a second time.
                self._last_slot_run[job.name] = last_started[:16]
            self._state[job.name] = {
                "name": job.name,
                "description": job.description,
                "cron_expr": job.cron_expr,
                "readonly": job.readonly,
                "enabled": enabled,
                "status_reason": reason,
                "state": restored_state,
                "last_started_at_utc": prev.get("last_started_at_utc"),
                "last_finished_at_utc": prev.get("last_finished_at_utc"),
                "last_exit_code": prev.get("last_exit_code"),
                "last_duration_s": prev.get("last_duration_s"),
                "last_error": prev.get("last_error"),
                "runs_ok": int(prev.get("runs_ok", 0)),
                "runs_failed": int(prev.get("runs_failed", 0)),
                "next_run_utc": job.next_run_utc(now).strftime("%Y-%m-%d %H:%M UTC"),
            }

    @staticmethod
    def _signal_process_group(proc: subprocess.Popen[Any], sig: int) -> None:
        if proc.poll() is not None:
            return
        try:
            if hasattr(os, "killpg"):
                os.killpg(proc.pid, sig)
            else:
                proc.send_signal(sig)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                proc.terminate() if sig == signal.SIGTERM else proc.kill()
            except OSError:
                pass

    def terminate_active_children(self, *, grace_sec: float = 15.0) -> None:
        """Forward SIGTERM to every running child process group and wait cleanly before SIGKILL."""
        with self._procs_lock:
            procs = list(self._active_procs)
        if not procs:
            return
        for proc in procs:
            self._signal_process_group(proc, signal.SIGTERM)
        deadline = time.monotonic() + max(0.5, grace_sec)
        for proc in procs:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                proc.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                self._signal_process_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    pass

    def _subprocess_runner(self, cmd: list[str], env: dict[str, str], timeout_s: int) -> int:
        self._thread_local.last_stderr = None
        err_file = tempfile.TemporaryFile(mode="w+b")
        try:
            proc = subprocess.Popen(cmd, env=env, stderr=err_file, start_new_session=True)
        except Exception as exc:
            err_file.close()
            self._thread_local.last_stderr = f"spawn_failed: {exc}"[:240]
            print(f"[hl-worker] job failed to spawn: {exc}", flush=True)
            return 1
        with self._procs_lock:
            self._active_procs.add(proc)
        rc_final = 1
        try:
            deadline = time.monotonic() + float(timeout_s)
            while True:
                rc = proc.poll()
                if rc is not None:
                    rc_final = int(rc)
                    break
                if self._stop_event.is_set():
                    self._signal_process_group(proc, signal.SIGTERM)
                    try:
                        rc_final = int(proc.wait(timeout=15.0))
                    except subprocess.TimeoutExpired:
                        self._signal_process_group(proc, signal.SIGKILL)
                        try:
                            rc_final = int(proc.wait(timeout=2.0))
                        except subprocess.TimeoutExpired:
                            rc_final = 137
                    break
                if time.monotonic() >= deadline:
                    print(f"[hl-worker] job timed out after {timeout_s}s: {' '.join(cmd[:3])}", flush=True)
                    self._signal_process_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=5.0)
                    except subprocess.TimeoutExpired:
                        self._signal_process_group(proc, signal.SIGKILL)
                        try:
                            proc.wait(timeout=2.0)
                        except subprocess.TimeoutExpired:
                            pass
                    rc_final = 124
                    self._thread_local.last_stderr = f"timed out after {timeout_s}s"
                    break
                time.sleep(0.25)
        finally:
            with self._procs_lock:
                self._active_procs.discard(proc)
            try:
                err_file.seek(0)
                raw_err = err_file.read().decode("utf-8", errors="replace")
                if raw_err:
                    sys.stderr.write(raw_err)
                    sys.stderr.flush()
                    if rc_final != 0:
                        lines = [ln.strip() for ln in raw_err.splitlines() if ln.strip()]
                        if lines:
                            self._thread_local.last_stderr = lines[-1][:240]
            finally:
                err_file.close()
        return rc_final

    def publish_status(self, now_utc: datetime | None = None) -> dict[str, Any]:
        now = now_utc or datetime.now(tz=timezone.utc)
        with self._status_lock:
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
                "testnet_execution_enabled": _testnet_exec_enabled()[0],
                "jobs": [dict(self._state[j.name]) for j in self.jobs],
            }
            _write_json_atomic(self.status_path, payload)
            return payload

    def _is_job_running(self, dep_name: str) -> bool:
        dep_lock = self._job_locks.get(dep_name)
        if dep_lock is not None and dep_lock.locked():
            return True
        with self._status_lock:
            return str((self._state.get(dep_name) or {}).get("state") or "") == "running"

    def _wait_for_dependency(self, job_name: str) -> None:
        dep = self._job_dependencies.get(job_name)
        if dep is None:
            return
        dep_name, max_wait_s = dep
        if max_wait_s <= 0 or not self._is_job_running(dep_name):
            return
        print(
            f"[hl-worker] {job_name} waiting up to {max_wait_s:.0f}s for upstream {dep_name} to finish...",
            flush=True,
        )
        deadline = time.monotonic() + float(max_wait_s)
        while time.monotonic() < deadline and not self._stop_event.is_set():
            if not self._is_job_running(dep_name):
                break
            self._stop_event.wait(timeout=min(self._dependency_poll_s, max(0.01, deadline - time.monotonic())))

    def run_job(self, job: JobSpec, *, force: bool = False) -> int:
        enabled, reason = job.is_enabled()
        if not enabled:
            if not force:
                with self._status_lock:
                    entry = self._state[job.name]
                    entry["enabled"] = False
                    entry["status_reason"] = reason
                    entry["state"] = "disabled"
                self.publish_status()
                return 0
            creds_ok, cred_reason = job.has_required_credentials()
            if not creds_ok:
                print(f"[hl-worker] refusing forced run of {job.name}: {cred_reason}", file=sys.stderr, flush=True)
                return 2

        lock = self._job_locks.setdefault(job.name, threading.Lock())
        if not lock.acquire(blocking=False):
            print(f"[hl-worker] skipping overlapping run of {job.name}", flush=True)
            return 0
        try:
            self._wait_for_dependency(job.name)
            start_dt = datetime.now(tz=timezone.utc)
            t0 = time.monotonic()
            with self._status_lock:
                entry = self._state[job.name]
                prev_failed = entry.get("state") == "failed"
                entry["state"] = "running"
                entry["last_started_at_utc"] = start_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
            self.publish_status(start_dt)

            cmd = job.build_command(self.root_dir, self.data_dir, self.telegram_enabled)
            env = dict(os.environ)
            src_path = str(self.root_dir / "src")
            env["PYTHONPATH"] = f"{src_path}:{env['PYTHONPATH']}" if env.get("PYTHONPATH") else src_path
            env["PYTHONUNBUFFERED"] = "1"

            print(f"[hl-worker] starting {job.name} ({job.cron_expr})", flush=True)
            max_attempts = (1 + self.readonly_retries) if job.readonly else 1
            exit_code = 1
            attempts_used = 0
            last_err: str | None = None
            for attempt in range(1, max_attempts + 1):
                attempts_used = attempt
                self._thread_local.last_stderr = None
                exit_code = self._runner(cmd, env, job.timeout_s)
                last_err = getattr(self._thread_local, "last_stderr", None)
                if exit_code == 0 or attempt >= max_attempts or self._stop_event.is_set():
                    break
                print(
                    f"[hl-worker] readonly job {job.name} failed (rc={exit_code}); "
                    f"retrying in {self.readonly_retry_delay_s:.0f}s (attempt {attempt + 1}/{max_attempts})",
                    flush=True,
                )
                if self._stop_event.wait(timeout=self.readonly_retry_delay_s):
                    break
            duration = round(time.monotonic() - t0, 2)
            end_dt = datetime.now(tz=timezone.utc)
            end_utc_str = end_dt.strftime("%Y-%m-%d %H:%M:%S UTC")

            with self._status_lock:
                entry = self._state[job.name]
                was_failed = prev_failed or (job.name in self._alerted_failed_jobs)
                entry["state"] = "ok" if exit_code == 0 else "failed"
                entry["last_finished_at_utc"] = end_utc_str
                entry["last_exit_code"] = exit_code
                entry["last_duration_s"] = duration
                entry["last_error"] = None if exit_code == 0 else (last_err or f"exit_code={exit_code}")
                if exit_code == 0:
                    entry["runs_ok"] = int(entry["runs_ok"]) + 1
                else:
                    entry["runs_failed"] = int(entry["runs_failed"]) + 1
                final_err = entry["last_error"]
            print(f"[hl-worker] finished {job.name} rc={exit_code} in {duration:.1f}s", flush=True)
            self.publish_status(end_dt)
            self._maybe_send_job_alert(
                job=job,
                exit_code=exit_code,
                was_failed=was_failed,
                duration_s=duration,
                attempts=attempts_used,
                last_error=final_err,
                observed_at_utc=end_utc_str,
            )
            return exit_code
        finally:
            lock.release()

    def _dispatch_alert_html(self, rich_html: str) -> None:
        if self._alert_sender is not None:
            self._alert_sender(rich_html)
            return
        token, chat_id, thread_id = pt.load_telegram_credentials((self.root_dir / ".env",))
        if token and chat_id:
            pt.send_telegram_rich_message(rich_html, bot_token=token, chat_id=chat_id, thread_id=thread_id)

    def _maybe_send_job_alert(
        self,
        *,
        job: JobSpec,
        exit_code: int,
        was_failed: bool,
        duration_s: float,
        attempts: int,
        last_error: str | None,
        observed_at_utc: str,
    ) -> None:
        if not self.telegram_enabled or self._stop_event.is_set():
            return
        now_mono = time.monotonic()
        should_send = False
        recovered = False
        with self._status_lock:
            if exit_code != 0:
                last_sent = self._last_failure_alert_mono.get(job.name)
                if last_sent is None or (now_mono - last_sent) >= self.alert_cooldown_s:
                    self._last_failure_alert_mono[job.name] = now_mono
                    self._alerted_failed_jobs.add(job.name)
                    should_send = True
            elif was_failed:
                self._alerted_failed_jobs.discard(job.name)
                self._last_failure_alert_mono.pop(job.name, None)
                should_send = True
                recovered = True
        if not should_send:
            return
        try:
            rich = format_worker_alert_rich_html(
                job=job,
                recovered=recovered,
                exit_code=exit_code,
                duration_s=duration_s,
                attempts=attempts,
                last_error=last_error,
                build_sha=os.environ.get("HL_BUILD_SHA", "dev"),
                observed_at_utc=observed_at_utc,
            )
            self._dispatch_alert_html(rich)
        except Exception as exc:
            print(f"[hl-worker] failed to send Telegram alert for {job.name}: {exc}", file=sys.stderr, flush=True)

    def _bootstrap_sentinel_for(self, job_name: str) -> Path | None:
        sentinels = {
            "onchain_events": self.paper_dir / "hl_onchain_events" / "latest_snapshot.json",
            "metamodel_paper": self.paper_dir / "hl_metamodel" / "state.json",
            "cross_dex_paper": self.paper_dir / "hl_cross_dex_paper" / "state.json",
            "frontier_persistence": self.paper_dir / "hl_frontier_history" / "latest_summary.json",
            "hip3_paper": self.paper_dir / "hl_hip3_paper" / "state.json",
            "spot_perp_paper": self.paper_dir / "hl_spot_perp_paper" / "state.json",
            "autopilot_allocator": self.paper_dir / "hl_autopilot" / "state.json",
            "cross_dex_scan": self.paper_dir / "hl_cross_dex_v2" / "opportunities.parquet",
            "hyperevm_defi_scan": self.paper_dir / "hl_hyperevm_defi" / "snapshot.json",
            "liq_funding_alpha": self.paper_dir / "hl_liq_funding_alpha" / "snapshot.json",
            "mm_calibration": self.paper_dir / "hl_mm_calibration" / "calibration.json",
            "settlement_study": self.paper_dir / "hl_funding_settlement_study" / "stats.parquet",
            "testnet_attribution": self.paper_dir / "hl_testnet_attribution" / "attribution.json",
            "testnet_risk_probe": self.paper_dir / "hl_testnet_risk" / "latest.json",
        }
        return sentinels.get(job_name)

    def run_bootstrap_readonly(self) -> None:
        """Seed missing read-only snapshots on startup so every dashboard tab has live data immediately."""
        missing_jobs: list[JobSpec] = []
        for job in self.jobs:
            if not (job.bootstrap_eligible and job.readonly):
                continue
            enabled, _ = job.is_enabled()
            if not enabled:
                continue
            sentinel = self._bootstrap_sentinel_for(job.name)
            if sentinel is None or not sentinel.is_file():
                missing_jobs.append(job)
        if not missing_jobs:
            return
        names = ", ".join(j.name for j in missing_jobs)
        print(f"[hl-worker] seeding missing read-only snapshots: {names}", flush=True)
        for job in missing_jobs:
            if self._stop_event.is_set():
                break
            self.run_job(job)

    def collect_due_jobs(self, now_utc: datetime, *, max_catchup_minutes: int = 60) -> list[tuple[JobSpec, str]]:
        """Return all ``(job, slot_key)`` pairs due between the last check and ``now_utc`` inclusive."""
        cur_min = now_utc.replace(second=0, microsecond=0)
        if self._last_checked_minute is None:
            minutes_to_scan = [cur_min]
        else:
            start_min = max(self._last_checked_minute + timedelta(minutes=1), cur_min - timedelta(minutes=max_catchup_minutes))
            minutes_to_scan = []
            cursor = start_min
            while cursor <= cur_min:
                minutes_to_scan.append(cursor)
                cursor += timedelta(minutes=1)
        self._last_checked_minute = cur_min

        due: list[tuple[JobSpec, str]] = []
        seen_jobs_this_poll: set[str] = set()
        for slot_dt in minutes_to_scan:
            slot_key = slot_dt.strftime("%Y-%m-%d %H:%M")
            for job in self.jobs:
                if job.name in seen_jobs_this_poll:
                    continue
                if job.should_run_at(slot_dt) and self._last_slot_run.get(job.name) != slot_key:
                    self._last_slot_run[job.name] = slot_key
                    seen_jobs_this_poll.add(job.name)
                    due.append((job, slot_key))
        return due

    def tick_minute(self, now_utc: datetime) -> list[str]:
        """Synchronously execute any jobs due at or since the last checked minute (deterministic for tests)."""
        ran: list[str] = []
        for job, _slot_key in self.collect_due_jobs(now_utc):
            if self._stop_event.is_set():
                break
            self.run_job(job)
            ran.append(job.name)
        self.publish_status(now_utc)
        return ran

    def run_forever(self, *, bootstrap: bool = True, poll_seconds: float = 15.0) -> None:
        def _handle_stop(signum: int, _frame: Any) -> None:
            print(f"[hl-worker] received signal {signum}; forwarding SIGTERM to children...", flush=True)
            self._stop_event.set()

        signal.signal(signal.SIGTERM, _handle_stop)
        signal.signal(signal.SIGINT, _handle_stop)

        self.publish_status()
        bootstrap_thread: threading.Thread | None = None
        if bootstrap:
            bootstrap_thread = threading.Thread(
                target=self.run_bootstrap_readonly,
                name="hl-bootstrap",
                daemon=True,
            )
            bootstrap_thread.start()

        print(
            f"[hl-worker] scheduler active (data_dir={self.data_dir}, jobs={len(self.jobs)}, workers={self.max_workers})",
            flush=True,
        )
        futures: set[Future[int]] = set()
        with ThreadPoolExecutor(max_workers=self.max_workers, thread_name_prefix="hl-job") as pool:
            try:
                while not self._stop_event.is_set():
                    now_utc = datetime.now(tz=timezone.utc)
                    for job, _slot_key in self.collect_due_jobs(now_utc):
                        if self._stop_event.is_set():
                            break
                        fut = pool.submit(self.run_job, job)
                        futures.add(fut)
                    futures = {f for f in futures if not f.done()}
                    self.publish_status(now_utc)
                    self._stop_event.wait(timeout=poll_seconds)
            finally:
                self.terminate_active_children(grace_sec=15.0)
                if bootstrap_thread is not None and bootstrap_thread.is_alive():
                    bootstrap_thread.join(timeout=5.0)
                for fut in futures:
                    try:
                        fut.result(timeout=5.0)
                    except Exception:
                        pass
                self.publish_status()


app = typer.Typer(add_completion=False, help="Hyperliquid Quant DeFi container worker scheduler.")


@app.command()
def main(
    data_dir: Path | None = typer.Option(None, help="Override data directory (default: $HL_DATA_DIR or ./data)"),
    list_jobs: bool = typer.Option(False, "--list", help="List configured scheduled jobs and exit"),
    run_once: str | None = typer.Option(None, "--run-once", help="Execute one named job and exit"),
    force: bool = typer.Option(False, "--force", help="Allow running a disabled opt-in job with --run-once"),
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
        enabled, reason = match.is_enabled()
        if not enabled and not force:
            typer.echo(f"Refusing to run '{run_once}': {reason} (pass --force to override opt-in gate)", err=True)
            raise typer.Exit(3)
        rc = sched.run_job(match, force=force)
        raise typer.Exit(rc)
    if bootstrap_only:
        sched.run_bootstrap_readonly()
        raise typer.Exit(0)

    bootstrap = (not no_bootstrap) and _env_flag("HL_WORKER_BOOTSTRAP_READONLY", True)
    sched.run_forever(bootstrap=bootstrap)


if __name__ == "__main__":
    app()
