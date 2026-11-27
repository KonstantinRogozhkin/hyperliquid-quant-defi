#!/usr/bin/env python3
"""Safely cut over local macOS ``hyperliquid-quant-defi`` crons to the Dokploy ``worker`` container.

Workflow:
1. Pre-flight verifies the live Dokploy deployment via HTTPS:
   - ``/api/v1/healthz`` reports ``status == "ok"``
   - ``/build-info.json`` and ``/api/v1/build-info.json`` match (and optionally match ``--expected-sha``)
   - ``/api/v1/scheduler/status`` reports a live ``hl-worker`` heartbeat fresher than ``--max-heartbeat-age-sec``
     and none of the required core jobs are missing, disabled, or in ``failed`` state.
2. Reads the local user's ``crontab -l`` and transforms ONLY ``hyperliquid-quant-defi`` entries
   (either commenting them out in place with a dated marker or removing the managed blocks).
   Unrelated cron entries are preserved byte-for-byte.
3. Runs in dry-run mode by default (printing a summary and unified diff). Pass ``--apply`` to save
   a timestamped backup under ``data/crontab_backups/`` and install the updated crontab via stdin.
   Pass ``--restore <backup_file> --apply`` to roll back immediately.
"""

from __future__ import annotations

import argparse
import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BACKUP_DIR = ROOT / "data" / "crontab_backups"
DEFAULT_REQUIRED_JOBS = (
    "market_snapshot",
    "metamodel_paper",
    "frontier_persistence",
    "onchain_events",
    "spot_perp_paper",
    "autopilot_allocator",
)

BEGIN_RE = re.compile(r"^#\s*BEGIN\s+(?:hyperliquid-quant-defi|Spy2Money\s+BackTester(?:\s+hl|\s+hyperliquid)?)\s+(?P<label>.+?)\s*$")
END_RE = re.compile(r"^#\s*END\s+(?:hyperliquid-quant-defi|Spy2Money\s+BackTester(?:\s+hl|\s+hyperliquid)?)\s+(?P<label>.+?)\s*$")
STRAY_HL_LINE_RE = re.compile(r"hyperliquid-quant-defi/scripts/ops/")
TESTNET_MARKER_RE = re.compile(r"(testnet\s+executor|run_hl_testnet_executor_tick\.sh)", re.IGNORECASE)


@dataclass(frozen=True)
class RemoteCheckResult:
    public_url: str
    frontend_sha: str
    backend_sha: str
    worker_sha: str
    heartbeat_utc: str
    heartbeat_age_sec: float
    testnet_execution_enabled: bool
    telegram_enabled: bool
    jobs_summary: dict[str, str]
    warnings: list[str]


@dataclass(frozen=True)
class CutoverPlan:
    original_text: str
    updated_text: str
    mode: str
    changed_count: int
    affected_labels: list[str]
    preserved_testnet_local: bool
    # True when an actual testnet-executor schedule (managed block or stray command) was disabled.
    disabled_testnet_executor: bool = False


def _parse_iso_utc(raw: str) -> datetime:
    text = raw.strip()
    if text.endswith(" UTC"):
        text = text[:-4] + "+00:00"
    elif text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def fetch_json(url: str, *, timeout_s: int = 10) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "hyperliquid-quant-defi-cutover/1.0",
        },
    )
    try:
        with urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read())
    except (HTTPError, URLError, ValueError, OSError) as exc:
        raise RuntimeError(f"Failed to fetch {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return payload


def verify_dokploy_stack(
    public_url: str,
    *,
    expected_sha: str = "",
    allow_sha_mismatch: bool = False,
    max_heartbeat_age_sec: float = 300.0,
    required_jobs: tuple[str, ...] = DEFAULT_REQUIRED_JOBS,
    expect_testnet_worker: bool = False,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    now_utc: datetime | None = None,
) -> RemoteCheckResult:
    """Verify Dokploy frontend, backend, and worker health before touching local crons."""
    base = public_url.strip().rstrip("/")
    if not base.startswith(("http://", "https://")):
        raise ValueError(f"--public-url must start with http:// or https://, got {public_url!r}")

    now = now_utc or datetime.now(timezone.utc)
    healthz = fetcher(f"{base}/api/v1/healthz")
    if healthz.get("status") != "ok":
        raise RuntimeError(f"Backend /api/v1/healthz not ok: {healthz}")

    fe_build = fetcher(f"{base}/build-info.json")
    be_build = fetcher(f"{base}/api/v1/build-info.json")
    fe_sha = str(fe_build.get("sha") or "").strip()
    be_sha = str(be_build.get("sha") or "").strip()
    if not fe_sha or not be_sha:
        raise RuntimeError(f"Missing build SHA in build-info probes (fe={fe_sha!r}, be={be_sha!r})")
    if not allow_sha_mismatch and fe_sha != be_sha:
        raise RuntimeError(f"Frontend/backend SHA mismatch: fe={fe_sha} vs be={be_sha}")
    if expected_sha and be_sha != expected_sha.strip():
        raise RuntimeError(f"Deployed backend SHA {be_sha} != expected {expected_sha.strip()}")

    sched = fetcher(f"{base}/api/v1/scheduler/status")
    worker = sched.get("worker_status")
    if not isinstance(worker, dict):
        raise RuntimeError("Dokploy /api/v1/scheduler/status returned no worker_status heartbeat yet")

    heartbeat_raw = str(worker.get("heartbeat_utc") or "").strip()
    if not heartbeat_raw:
        raise RuntimeError("worker_status is missing heartbeat_utc")
    heartbeat_dt = _parse_iso_utc(heartbeat_raw)
    heartbeat_age = (now - heartbeat_dt).total_seconds()
    if heartbeat_age > max_heartbeat_age_sec:
        raise RuntimeError(
            f"Dokploy worker heartbeat is stale: {heartbeat_age:.1f}s old (> {max_heartbeat_age_sec:.0f}s limit)"
        )

    jobs_raw = worker.get("jobs")
    if not isinstance(jobs_raw, list) or not jobs_raw:
        raise RuntimeError("worker_status.jobs is empty")
    jobs_by_name = {str(j.get("name")): j for j in jobs_raw if isinstance(j, dict) and j.get("name")}
    jobs_summary: dict[str, str] = {}
    for name, info in jobs_by_name.items():
        jobs_summary[name] = str(info.get("state") or "unknown")

    for req_job in required_jobs:
        info = jobs_by_name.get(req_job)
        if info is None:
            raise RuntimeError(f"Required worker job '{req_job}' missing from Dokploy worker_status")
        if not info.get("enabled", False):
            raise RuntimeError(f"Required worker job '{req_job}' is disabled ({info.get('status_reason')})")
        if str(info.get("state") or "").lower() == "failed":
            raise RuntimeError(
                f"Required worker job '{req_job}' is in failed state (exit={info.get('last_exit_code')})"
            )

    testnet_enabled = bool(worker.get("testnet_execution_enabled"))
    telegram_enabled = bool(worker.get("telegram_enabled"))
    worker_sha = str(worker.get("build_sha") or "").strip()
    warnings: list[str] = []

    if worker_sha and not allow_sha_mismatch and worker_sha != be_sha:
        warnings.append(f"worker build_sha ({worker_sha[:12]}) differs from backend ({be_sha[:12]})")
    if expect_testnet_worker and not testnet_enabled:
        raise RuntimeError(
            "Dokploy worker has testnet_execution_enabled=false while --expect-testnet-worker was requested"
        )
    if not testnet_enabled:
        warnings.append(
            "Dokploy worker has HL_ENABLE_TESTNET_EXECUTION=false (read-only). "
            "Use --keep-local-testnet if you want macOS to keep running testnet_executor until you enable it in Dokploy."
        )

    return RemoteCheckResult(
        public_url=base,
        frontend_sha=fe_sha,
        backend_sha=be_sha,
        worker_sha=worker_sha,
        heartbeat_utc=heartbeat_raw,
        heartbeat_age_sec=heartbeat_age,
        testnet_execution_enabled=testnet_enabled,
        telegram_enabled=telegram_enabled,
        jobs_summary=jobs_summary,
        warnings=warnings,
    )


def transform_crontab(
    crontab_text: str,
    *,
    mode: str = "comment",
    keep_local_testnet: bool = False,
    date_tag: str | None = None,
) -> CutoverPlan:
    """Disable or remove ``hyperliquid-quant-defi`` cron entries while preserving all other jobs."""
    if mode not in {"comment", "remove"}:
        raise ValueError(f"Unsupported mode: {mode!r} (expected 'comment' or 'remove')")

    tag = date_tag or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    disable_prefix = f"# Disabled {tag} (cutover to Dokploy worker): "

    lines = crontab_text.splitlines(keepends=True)
    out: list[str] = []
    in_hl_block = False
    current_label = ""
    skip_current_block = False
    changed_count = 0
    affected_labels: list[str] = []
    preserved_testnet_local = False
    disabled_testnet_executor = False

    def _record_label(label: str) -> None:
        if label not in affected_labels:
            affected_labels.append(label)

    for line in lines:
        stripped = line.strip()
        begin_match = BEGIN_RE.match(stripped)
        if begin_match:
            in_hl_block = True
            current_label = begin_match.group("label").strip()
            skip_current_block = keep_local_testnet and bool(TESTNET_MARKER_RE.search(current_label))
            if skip_current_block:
                preserved_testnet_local = True
                out.append(line)
            elif mode == "comment":
                out.append(line)
            else:
                changed_count += 1
                _record_label(current_label)
                if TESTNET_MARKER_RE.search(current_label):
                    disabled_testnet_executor = True
            continue

        if in_hl_block:
            end_match = END_RE.match(stripped)
            if end_match:
                if skip_current_block or mode == "comment":
                    out.append(line)
                in_hl_block = False
                current_label = ""
                skip_current_block = False
                continue

            if skip_current_block:
                out.append(line)
                continue

            if mode == "remove":
                if stripped and not stripped.startswith("#") and TESTNET_MARKER_RE.search(stripped):
                    disabled_testnet_executor = True
                continue

            # mode == "comment": comment out active non-empty, non-comment lines idempotently
            if stripped and not stripped.startswith("#"):
                out.append(f"{disable_prefix}{line}")
                changed_count += 1
                _record_label(current_label or "hyperliquid-quant-defi")
                if TESTNET_MARKER_RE.search(current_label) or TESTNET_MARKER_RE.search(stripped):
                    disabled_testnet_executor = True
            else:
                out.append(line)
            continue

        # Outside a managed BEGIN/END block: catch stray uncommented lines pointing to this repo
        if stripped and not stripped.startswith("#") and STRAY_HL_LINE_RE.search(stripped):
            if keep_local_testnet and TESTNET_MARKER_RE.search(stripped):
                preserved_testnet_local = True
                out.append(line)
                continue
            changed_count += 1
            _record_label("stray hyperliquid-quant-defi cron")
            if TESTNET_MARKER_RE.search(stripped):
                disabled_testnet_executor = True
            if mode == "comment":
                out.append(f"{disable_prefix}{line}")
            continue

        out.append(line)

    updated = "".join(out)
    if updated and not updated.endswith("\n"):
        updated += "\n"

    return CutoverPlan(
        original_text=crontab_text,
        updated_text=updated,
        mode=mode,
        changed_count=changed_count,
        affected_labels=affected_labels,
        preserved_testnet_local=preserved_testnet_local,
        disabled_testnet_executor=disabled_testnet_executor,
    )


def read_local_crontab() -> str:
    proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=False)
    if proc.returncode == 0:
        return proc.stdout
    stderr_lower = proc.stderr.lower()
    if "no crontab" in stderr_lower:
        return ""
    raise RuntimeError(f"crontab -l failed (exit {proc.returncode}): {proc.stderr.strip()}")


def write_local_crontab(text: str, *, timeout_s: float = 15.0) -> None:
    """Install crontab via a short ``/tmp`` path to avoid macOS BSD 99-char and stdin hangs."""
    tmp_dir = "/tmp" if os.path.isdir("/tmp") else None
    fd, tmp_path = tempfile.mkstemp(dir=tmp_dir, prefix="hlcron_", suffix=".txt")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        try:
            proc = subprocess.run(
                ["crontab", tmp_path],
                capture_output=True,
                text=True,
                timeout=timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"crontab install timed out after {timeout_s:.0f}s — on macOS this usually means "
                "a TCC permission prompt is waiting or the calling app is not authorized to edit crontab. "
                "Re-run this command directly from your interactive Terminal."
            ) from exc
        if proc.returncode != 0:
            raise RuntimeError(f"crontab install failed (exit {proc.returncode}): {proc.stderr.strip()}")
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def save_crontab_backup(text: str, backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path:
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = backup_dir / f"crontab_{ts}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Verify Dokploy hl-worker health and safely disable/remove local hyperliquid-quant-defi crons."
    )
    parser.add_argument(
        "--public-url",
        default=os.environ.get("DOKPLOY_PUBLIC_URL", "").strip(),
        help="Live Dokploy dashboard URL (e.g. https://hl.example.com). Defaults to $DOKPLOY_PUBLIC_URL.",
    )
    parser.add_argument(
        "--expected-sha",
        default="",
        help="Optional 40-char Git SHA that Dokploy backend/frontend must serve.",
    )
    parser.add_argument(
        "--allow-sha-mismatch",
        action="store_true",
        help="Allow FE/BE/worker SHA mismatch during pre-flight verification.",
    )
    parser.add_argument(
        "--max-heartbeat-age-sec",
        type=float,
        default=300.0,
        help="Maximum allowed age in seconds for worker_status.heartbeat_utc (default: 300).",
    )
    parser.add_argument(
        "--require-jobs",
        default=",".join(DEFAULT_REQUIRED_JOBS),
        help=f"Comma-separated worker jobs that must be enabled and not failed (default: {','.join(DEFAULT_REQUIRED_JOBS)}).",
    )
    parser.add_argument(
        "--expect-testnet-worker",
        action="store_true",
        help="Fail pre-flight if Dokploy worker does not have HL_ENABLE_TESTNET_EXECUTION=true.",
    )
    parser.add_argument(
        "--skip-remote-check",
        action="store_true",
        help="Skip live Dokploy HTTPS verification (emergency/offline use only).",
    )
    parser.add_argument(
        "--mode",
        choices=("comment", "remove"),
        default="comment",
        help="How to disable local hyperliquid-quant-defi crons (default: comment).",
    )
    parser.add_argument(
        "--keep-local-testnet",
        action="store_true",
        help="Keep the local testnet executor cron active while disabling the other 6 schedules.",
    )
    parser.add_argument(
        "--force-dual-executor",
        action="store_true",
        help=(
            "DANGEROUS: allow --keep-local-testnet even when the Dokploy worker also executes on Testnet "
            "(or when that cannot be verified). Both would rebalance the same account at :10 */4."
        ),
    )
    parser.add_argument(
        "--allow-no-testnet-executor",
        action="store_true",
        help="Allow disabling the local testnet executor while the Dokploy worker is read-only (executor stops).",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        default=DEFAULT_BACKUP_DIR,
        help=f"Directory where timestamped crontab backups are written (default: {DEFAULT_BACKUP_DIR}).",
    )
    parser.add_argument(
        "--restore",
        type=Path,
        default=None,
        help="Restore a previous crontab backup file (combine with --apply to install).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually write the updated crontab (without --apply, runs as a read-only dry-run).",
    )
    return parser


def main(
    argv: list[str] | None = None,
    *,
    crontab_reader: Callable[[], str] = read_local_crontab,
    crontab_writer: Callable[[str], None] = write_local_crontab,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
) -> int:
    args = build_parser().parse_args(argv)

    if args.restore is not None:
        backup_path: Path = args.restore
        if not backup_path.is_file():
            print(f"[cutover] Backup file not found: {backup_path}", file=sys.stderr)
            return 2
        restore_text = backup_path.read_text(encoding="utf-8")
        current_text = crontab_reader()
        diff = "".join(
            difflib.unified_diff(
                current_text.splitlines(keepends=True),
                restore_text.splitlines(keepends=True),
                fromfile="crontab(current)",
                tofile=str(backup_path),
            )
        )
        print(diff or "[cutover] Current crontab already matches backup.")
        if not args.apply:
            print("[cutover] DRY-RUN: pass --apply to restore this backup.")
            return 0
        pre_restore_backup = save_crontab_backup(current_text, args.backup_dir)
        crontab_writer(restore_text)
        print(f"[cutover] Restored crontab from {backup_path} (previous snapshot saved to {pre_restore_backup}).")
        return 0

    remote_testnet_enabled: bool | None = None
    if not args.skip_remote_check:
        if not args.public_url:
            print(
                "[cutover] Missing --public-url (or DOKPLOY_PUBLIC_URL env). "
                "Provide the live Dokploy URL to verify worker health, or pass --skip-remote-check.",
                file=sys.stderr,
            )
            return 2
        req_jobs = tuple(j.strip() for j in args.require_jobs.split(",") if j.strip())
        try:
            check = verify_dokploy_stack(
                args.public_url,
                expected_sha=args.expected_sha,
                allow_sha_mismatch=args.allow_sha_mismatch,
                max_heartbeat_age_sec=args.max_heartbeat_age_sec,
                required_jobs=req_jobs,
                expect_testnet_worker=args.expect_testnet_worker,
                fetcher=fetcher,
            )
        except Exception as exc:
            print(f"[cutover] Remote pre-flight FAILED: {exc}", file=sys.stderr)
            return 1
        print(
            f"[cutover] Dokploy pre-flight OK: url={check.public_url} sha={check.backend_sha[:12]} "
            f"heartbeat_age={check.heartbeat_age_sec:.1f}s testnet_worker={check.testnet_execution_enabled} "
            f"telegram={check.telegram_enabled}"
        )
        for warn in check.warnings:
            print(f"[cutover] WARNING: {warn}")
        remote_testnet_enabled = check.testnet_execution_enabled

    if args.keep_local_testnet and not args.force_dual_executor:
        if remote_testnet_enabled is None:
            print(
                "[cutover] REFUSED: --keep-local-testnet with --skip-remote-check cannot verify that the Dokploy "
                "worker is not also executing on Testnet. Pass --force-dual-executor to override.",
                file=sys.stderr,
            )
            return 2
        if remote_testnet_enabled:
            print(
                "[cutover] REFUSED: --keep-local-testnet while the Dokploy worker has "
                "HL_ENABLE_TESTNET_EXECUTION=true would run two executors on the same Testnet account "
                "(double cancels/orders, HWM races). Drop --keep-local-testnet, disable execution in Dokploy, "
                "or pass --force-dual-executor.",
                file=sys.stderr,
            )
            return 2

    current_text = crontab_reader()
    plan = transform_crontab(
        current_text,
        mode=args.mode,
        keep_local_testnet=args.keep_local_testnet,
    )

    if plan.changed_count == 0:
        print("[cutover] No active local hyperliquid-quant-defi cron entries to change.")
        return 0

    if plan.disabled_testnet_executor and remote_testnet_enabled is False and not args.allow_no_testnet_executor:
        print(
            "[cutover] REFUSED: this would disable the local testnet executor while the Dokploy worker is "
            "read-only, leaving NO executor (and no alert for it). Pass --keep-local-testnet, enable "
            "HL_ENABLE_TESTNET_EXECUTION in Dokploy, or --allow-no-testnet-executor.",
            file=sys.stderr,
        )
        return 2

    diff = "".join(
        difflib.unified_diff(
            plan.original_text.splitlines(keepends=True),
            plan.updated_text.splitlines(keepends=True),
            fromfile="crontab(before)",
            tofile="crontab(after)",
        )
    )
    print(f"[cutover] Mode={plan.mode} | affected={plan.changed_count} | blocks={', '.join(plan.affected_labels)}")
    if plan.preserved_testnet_local:
        print("[cutover] Preserved local testnet_executor cron (--keep-local-testnet).")
    print(diff)

    if not args.apply:
        print("[cutover] DRY-RUN complete (crontab untouched). Re-run with --apply to cut over.")
        return 0

    backup_path = save_crontab_backup(plan.original_text, args.backup_dir)
    crontab_writer(plan.updated_text)
    print(f"[cutover] Cutover APPLIED. Backup saved to {backup_path}")
    print(f"[cutover] Rollback command: python3 scripts/ops/cutover_local_crons_to_dokploy.py --restore {backup_path} --apply")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
