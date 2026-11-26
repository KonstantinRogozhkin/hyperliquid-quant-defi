"""Shared immutable run publication for public alternative-market collectors.

Every run lands as one immutable directory::

    <output_root>/<dataset>/<run_id>/
        raw/page-0000.json ...   canonical JSON provider responses
        rows.parquet             normalized single-table rows (or <table_name>.parquet)
        manifest.json            provider, endpoint, params, status, sha256 of every file

Directories are built under ``.staging-*`` and published with an atomic rename.
Historical runs get a deterministic ``run_id`` derived from their provider,
endpoint and parameters so reruns can be reused without hitting the network.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
from typing import Any

import pandas as pd

SCHEMA_VERSION = 1

STATUS_COMPLETE = "complete"
STATUS_PARTIAL_HISTORY = "partial_history"
STATUS_EMPTY = "empty"


class MarketDataError(RuntimeError):
    """Base error for public alternative-market collectors."""


class ManifestCorruptError(MarketDataError):
    """A published run directory exists but its manifest does not verify."""


@dataclass(slots=True)
class Collected:
    """In-memory result of one collection, before publication."""

    dataset: str
    params: dict[str, Any]
    pages: list[Any]
    rows: pd.DataFrame
    status: str
    observed_at_ms: int
    notes: dict[str, Any] = field(default_factory=dict)
    provider: str = "hyperliquid"
    endpoint: str | None = None
    tables: dict[str, pd.DataFrame] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PublishResult:
    run_dir: Path
    run_id: str
    status: str
    row_count: int
    reused: bool


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def file_sha256(path: Path) -> str:
    digest = sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_id_for_params(
    dataset: str,
    params: dict[str, Any],
    *,
    base_url: str | None = None,
    observed_at_ms: int | None = None,
    provider: str | None = None,
) -> str:
    """Deterministic run id for historical windows; timestamp-based for snapshots."""
    if dataset == "snapshot":
        if observed_at_ms is None:
            raise ValueError("snapshot run_id requires observed_at_ms")
        stamp = datetime.fromtimestamp(observed_at_ms / 1000, tz=timezone.utc)
        return stamp.strftime("%Y%m%dT%H%M%S%fZ")
    identity: dict[str, Any] = {"params": params}
    if base_url:
        identity["endpoint"] = base_url
    if provider:
        identity["provider"] = provider
    digest = sha256(canonical_json(identity)).hexdigest()[:16]
    label = "_".join(str(params[k]) for k in ("coin", "symbol", "interval") if k in params)
    return f"{label}_{params['start_ms']}_{params['end_ms']}_{digest}"


def run_id_for(
    collected: Collected,
    *,
    base_url: str | None = None,
    provider: str | None = None,
) -> str:
    return run_id_for_params(
        collected.dataset,
        collected.params,
        base_url=base_url if base_url is not None else collected.endpoint,
        observed_at_ms=collected.observed_at_ms,
        provider=provider if provider is not None else collected.provider,
    )


def try_reuse(
    output_root: Path,
    dataset: str,
    params: dict[str, Any],
    *,
    base_url: str,
    provider: str = "hyperliquid",
) -> PublishResult | None:
    """Return a verified existing run for identical (provider, endpoint, params), or None."""
    if dataset == "snapshot":
        return None
    run_id = run_id_for_params(dataset, params, base_url=base_url, provider=provider)
    final_dir = Path(output_root) / dataset / run_id
    if not final_dir.exists():
        return None
    manifest = verify_run(final_dir)
    if manifest.get("params") != params:
        raise ManifestCorruptError(f"{final_dir} holds different params")
    if manifest.get("endpoint") != base_url:
        raise ManifestCorruptError(f"{final_dir} holds different endpoint")
    if manifest.get("provider") != provider:
        raise ManifestCorruptError(f"{final_dir} holds different provider")
    return PublishResult(final_dir, run_id, manifest["status"], int(manifest["row_count"]), reused=True)


def publish(
    collected: Collected,
    output_root: Path,
    *,
    base_url: str,
    provider: str | None = None,
) -> PublishResult:
    """Stage and atomically publish one run; reuse a verified identical run."""
    effective_provider = provider if provider is not None else collected.provider
    dataset_dir = Path(output_root) / collected.dataset
    run_id = run_id_for(collected, base_url=base_url, provider=effective_provider)
    final_dir = dataset_dir / run_id
    if final_dir.exists():
        manifest = verify_run(final_dir)
        if manifest.get("params") != collected.params:
            raise ManifestCorruptError(f"{final_dir} holds different params")
        if manifest.get("endpoint") != base_url:
            raise ManifestCorruptError(f"{final_dir} holds different endpoint")
        if manifest.get("provider") != effective_provider:
            raise ManifestCorruptError(f"{final_dir} holds different provider")
        return PublishResult(final_dir, run_id, manifest["status"], int(manifest["row_count"]), reused=True)

    dataset_dir.mkdir(parents=True, exist_ok=True)
    staging = dataset_dir / f".staging-{run_id}-{os.getpid()}"
    if staging.exists():
        shutil.rmtree(staging)
    try:
        raw_dir = staging / "raw"
        raw_dir.mkdir(parents=True)
        files: dict[str, str] = {}
        for index, page in enumerate(collected.pages):
            name = f"raw/page-{index:04d}.json"
            data = canonical_json(page)
            (staging / name).write_bytes(data)
            files[name] = sha256(data).hexdigest()

        if collected.tables:
            row_count = 0
            for table_name, frame in sorted(collected.tables.items()):
                parquet_name = f"{table_name}.parquet"
                table_path = staging / parquet_name
                frame.to_parquet(table_path, index=False)
                files[parquet_name] = file_sha256(table_path)
                row_count += int(len(frame))
        else:
            rows_path = staging / "rows.parquet"
            collected.rows.to_parquet(rows_path, index=False)
            files["rows.parquet"] = file_sha256(rows_path)
            row_count = int(len(collected.rows))

        manifest = {
            "schema_version": SCHEMA_VERSION,
            "provider": effective_provider,
            "endpoint": base_url,
            "dataset": collected.dataset,
            "run_id": run_id,
            "params": collected.params,
            "status": collected.status,
            "row_count": row_count,
            "observed_at": datetime.fromtimestamp(collected.observed_at_ms / 1000, tz=timezone.utc).isoformat(),
            "observed_at_ms": collected.observed_at_ms,
            "notes": collected.notes,
            "research_only": True,
            "files": files,
        }
        (staging / "manifest.json").write_bytes(json.dumps(manifest, indent=2, sort_keys=True).encode())
        try:
            os.replace(staging, final_dir)
        except OSError:
            if final_dir.exists() and collected.dataset != "snapshot":
                shutil.rmtree(staging, ignore_errors=True)
                reused = try_reuse(
                    output_root,
                    collected.dataset,
                    collected.params,
                    base_url=base_url,
                    provider=effective_provider,
                )
                if reused is not None:
                    return reused
            raise
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return PublishResult(final_dir, run_id, collected.status, row_count, reused=False)


def verify_run(run_dir: Path) -> dict[str, Any]:
    """Return the manifest of a published run or raise ``ManifestCorruptError``."""
    manifest_path = Path(run_dir) / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text())
    except (OSError, ValueError) as exc:
        raise ManifestCorruptError(f"{run_dir}: unreadable manifest") from exc
    if not isinstance(manifest, dict):
        raise ManifestCorruptError(f"{run_dir}: manifest root must be an object")
    files = manifest.get("files")
    if not isinstance(files, dict) or not any(name.endswith(".parquet") for name in files):
        raise ManifestCorruptError(f"{run_dir}: manifest lists no parquet files")
    for name, digest in files.items():
        path = Path(run_dir) / name
        if not path.is_file() or file_sha256(path) != digest:
            raise ManifestCorruptError(f"{run_dir}: {name} missing or hash mismatch")
    return manifest
