"""Read-only FastAPI backend serving live Hyperliquid Quant DeFi telemetry and desk state.

Reads atomic JSON/Parquet snapshots produced by ``hl_quant.scheduler`` (mounted read-only at
``HL_DATA_ROOT`` in Docker) and exposes clean JSON endpoints for the React/Vite dashboard SPA
plus immutable ``/build-info.json`` and ``/healthz`` probes for Dokploy CI/CD verification.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
import pandas as pd

from hl_quant.hl_portfolio_dashboard import collect_dashboard_payload
from hl_quant.metrics import PROMETHEUS_CONTENT_TYPE, render_prometheus_metrics

ARTIFACT_FILES: dict[str, str] = {
    "frontier_history": "hl_frontier_history/latest_summary.json",
    "hip3": "hl_hip3_arb/snapshot.json",
    "hip3_paper": "hl_hip3_paper/state.json",
    "spot_perp_paper": "hl_spot_perp_paper/state.json",
    "autopilot": "hl_autopilot/state.json",
    "boros": "hl_boros/snapshot.json",
    "yield_stack": "hl_hyperevm_yield_stack/snapshot.json",
    "onchain_events": "hl_onchain_events/latest_snapshot.json",
    "liq_funding_alpha": "hl_liq_funding_alpha/snapshot.json",
    "metamodel_paper": "hl_metamodel/state.json",
    "cross_dex_paper": "hl_cross_dex_paper/state.json",
    "cross_dex_scan": "hl_cross_dex_v2/opportunities.parquet",
    "hyperevm_defi": "hl_hyperevm_defi/snapshot.json",
    "mm_calibration": "hl_mm_calibration/calibration.json",
    "settlement_study": "hl_funding_settlement_study/stats.parquet",
    "testnet_attribution": "hl_testnet_attribution/attribution.json",
    "testnet_audit": "hl_testnet_audit.jsonl",
    "worker_status": "hl_worker_status.json",
}


def sanitize_json(value: Any) -> Any:
    """Recursively convert NaN/Inf, NaT, NumPy scalars, Timestamps, and Paths into strict JSON values."""
    if value is None or value is pd.NaT or isinstance(value, (bool, str, int)):
        return None if value is pd.NaT else value
    if isinstance(value, float):
        return None if (math.isnan(value) or math.isinf(value)) else value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, datetime):
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): sanitize_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [sanitize_json(v) for v in value]
    # Handle pandas/numpy scalars (np.float64, np.int64, np.bool_, pd.Timestamp, pd.NA)
    if hasattr(value, "isoformat") and callable(value.isoformat):
        try:
            if pd.isna(value):
                return None
            return value.isoformat()
        except Exception:
            pass
    if hasattr(value, "item") and callable(value.item):
        try:
            return sanitize_json(value.item())
        except Exception:
            pass
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    return str(value)


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _read_parquet_tail(path: Path, limit: int = 100) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        df = pd.read_parquet(path)
        if limit > 0 and len(df) > limit:
            df = df.tail(limit)
        return df.to_dict(orient="records")
    except Exception:
        return []


def _read_metamodel_positions(data_root: Path) -> list[dict[str, Any]]:
    state = _read_json(data_root / "hl_metamodel" / "state.json")
    raw_pos = (state or {}).get("positions")
    if not isinstance(raw_pos, dict):
        return []
    rows: list[dict[str, Any]] = []
    for coin, info in raw_pos.items():
        if not isinstance(info, dict):
            continue
        rows.append(
            {
                "coin": str(coin),
                "hl_weight": float(info.get("hl_weight", 0.0) or 0.0),
                "cex_weight": float(info.get("cex_weight", 0.0) or 0.0),
                "cex_venue": str(info.get("cex_venue") or ""),
                "last_hl_px": info.get("last_hl_px"),
                "l2_spread_bps": info.get("l2_spread_bps"),
                "cum_pnl_usd": float(info.get("cum_pnl_usd", 0.0) or 0.0),
            }
        )
    rows.sort(key=lambda r: (-abs(r["hl_weight"]), r["coin"]))
    return rows


def _read_testnet_audit_tail(data_root: Path, limit: int = 15) -> list[dict[str, Any]]:
    path = data_root / "hl_testnet_audit.jsonl"
    if not path.is_file():
        return []
    records: list[dict[str, Any]] = []
    try:
        lines = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return []
    for line in reversed(lines[-limit:]):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict):
            continue
        ts_ms = int(rec.get("ts_ms") or 0)
        obs = (
            datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            if ts_ms > 0
            else None
        )
        records.append(
            {
                "ts_ms": ts_ms,
                "observed_at_utc": obs,
                "status": str(rec.get("status") or "unknown"),
                "violations": list(rec.get("violations") or []),
                "cancelled_count": int(rec.get("cancelled_count", 0) or 0),
                "placed_count": int(rec.get("placed_count", 0) or 0),
                "total_notional_usd": float(rec.get("total_notional_usd", 0.0) or 0.0),
                "high_water_mark_usd": float(rec.get("high_water_mark_usd", 0.0) or 0.0),
                "blocked_isolated_coins": list(rec.get("blocked_isolated_coins") or []),
                "skipped_unquoted_coins": list(rec.get("skipped_unquoted_coins") or []),
            }
        )
    return records


def collect_artifact_freshness(data_root: Path, now_utc: datetime | None = None) -> dict[str, dict[str, Any]]:
    """Return existence, UTC mtime, and age in minutes for each expected artifact."""
    now = now_utc or datetime.now(tz=timezone.utc)
    out: dict[str, dict[str, Any]] = {}
    for key, rel in ARTIFACT_FILES.items():
        target = data_root / rel
        if target.is_file():
            mtime = datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc)
            age_min = round(max((now - mtime).total_seconds() / 60.0, 0.0), 1)
            out[key] = {
                "exists": True,
                "path": rel,
                "updated_at_utc": mtime.strftime("%Y-%m-%d %H:%M UTC"),
                "age_minutes": age_min,
            }
        else:
            out[key] = {
                "exists": False,
                "path": rel,
                "updated_at_utc": None,
                "age_minutes": None,
            }
    return out


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"


def create_app(
    *,
    data_root: Path | None = None,
    build_sha: str | None = None,
    built_at: str | None = None,
) -> FastAPI:
    """Create the configured read-only FastAPI application."""
    resolved_data_root = Path(
        data_root if data_root is not None else os.environ.get("HL_DATA_ROOT", "data/paper")
    )
    resolved_sha = (build_sha if build_sha is not None else os.environ.get("HL_BUILD_SHA", "dev")).strip() or "dev"
    resolved_built_at = (built_at if built_at is not None else os.environ.get("HL_BUILT_AT", "")).strip()

    app = FastAPI(
        title="Hyperliquid Quant DeFi API",
        version="0.1.0",
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
    )

    cors_raw = os.environ.get("HL_CORS_ORIGINS", "").strip()
    if cors_raw:
        origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
        if origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=False,
                allow_methods=["GET"],
                allow_headers=["*"],
            )

    build_payload = {
        "service": "hl-backend",
        "sha": resolved_sha,
        "built_at": resolved_built_at,
    }

    @app.get("/healthz")
    @app.get("/api/v1/healthz")
    def healthz(response: Response) -> dict[str, Any]:
        _no_store(response)
        return {
            "status": "ok",
            "service": "hl-backend",
            "sha": resolved_sha,
            "data_root_exists": resolved_data_root.is_dir(),
        }

    @app.get("/build-info.json")
    @app.get("/api/v1/build-info.json")
    def build_info(response: Response) -> dict[str, Any]:
        _no_store(response)
        return dict(build_payload)

    def _build_overview_payload() -> dict[str, Any]:
        payload = collect_dashboard_payload(resolved_data_root)
        payload["build_info"] = dict(build_payload)
        payload["worker_status"] = _read_json(resolved_data_root / "hl_worker_status.json")
        payload["artifact_freshness"] = collect_artifact_freshness(resolved_data_root)
        payload["paper_positions"] = _read_metamodel_positions(resolved_data_root)
        payload["testnet_audit_recent"] = _read_testnet_audit_tail(resolved_data_root, limit=15)
        return payload

    @app.get("/metrics")
    @app.get("/api/v1/metrics")
    def prometheus_metrics() -> PlainTextResponse:
        body = render_prometheus_metrics(_build_overview_payload())
        return PlainTextResponse(
            content=body,
            media_type=PROMETHEUS_CONTENT_TYPE,
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/api/v1/overview")
    def overview(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(_build_overview_payload())

    @app.get("/api/v1/metamodel/positions")
    def metamodel_positions(response: Response) -> dict[str, Any]:
        _no_store(response)
        positions = _read_metamodel_positions(resolved_data_root)
        return sanitize_json({"count": len(positions), "positions": positions})

    @app.get("/api/v1/hip3/persistence")
    def hip3_persistence(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(
            {
                "frontier_history": _read_json(resolved_data_root / "hl_frontier_history" / "latest_summary.json"),
                "hip3": _read_json(resolved_data_root / "hl_hip3_arb" / "snapshot.json"),
                "hip3_paper": _read_json(resolved_data_root / "hl_hip3_paper" / "state.json"),
                "hip3_paper_ticks": _read_parquet_tail(
                    resolved_data_root / "hl_hip3_paper" / "ticks.parquet", limit=100
                ),
                "boros": _read_json(resolved_data_root / "hl_boros" / "snapshot.json"),
                "yield_stack": _read_json(resolved_data_root / "hl_hyperevm_yield_stack" / "snapshot.json"),
                "history_tail": _read_parquet_tail(
                    resolved_data_root / "hl_frontier_history" / "hip3_history.parquet", limit=100
                ),
            }
        )

    @app.get("/api/v1/spot-perp")
    def spot_perp(response: Response) -> dict[str, Any]:
        _no_store(response)
        scan = _read_json(resolved_data_root / "hl_spot_perp_paper" / "snapshot.json") or _read_json(
            resolved_data_root / "hl_spot_perp_arb" / "snapshot.json"
        )
        return sanitize_json(
            {
                "state": _read_json(resolved_data_root / "hl_spot_perp_paper" / "state.json"),
                "scan": scan,
                "ticks": _read_parquet_tail(resolved_data_root / "hl_spot_perp_paper" / "ticks.parquet", limit=200),
                "trades": _read_parquet_tail(resolved_data_root / "hl_spot_perp_paper" / "trades.parquet", limit=100),
            }
        )

    @app.get("/api/v1/autopilot")
    def autopilot(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(
            {
                "state": _read_json(resolved_data_root / "hl_autopilot" / "state.json"),
                "ticks": _read_parquet_tail(resolved_data_root / "hl_autopilot" / "ticks.parquet", limit=200),
                "rebalances": _read_parquet_tail(resolved_data_root / "hl_autopilot" / "rebalances.parquet", limit=100),
            }
        )

    @app.get("/api/v1/events/latest")
    def events_latest(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(
            {
                "onchain_events": _read_json(resolved_data_root / "hl_onchain_events" / "latest_snapshot.json"),
                "hyperevm": _read_json(resolved_data_root / "hl_hyperevm_defi" / "snapshot.json"),
            }
        )

    @app.get("/api/v1/attribution")
    def attribution(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(
            {
                "attribution": _read_json(resolved_data_root / "hl_testnet_attribution" / "attribution.json"),
                "testnet_audit_recent": _read_testnet_audit_tail(resolved_data_root, limit=15),
                "mm_session": _read_json(resolved_data_root / "hl_mm_testnet" / "summary.json"),
                "mm_calibration": _read_json(resolved_data_root / "hl_mm_calibration" / "calibration.json"),
            }
        )

    @app.get("/api/v1/scheduler/status")
    def scheduler_status(response: Response) -> dict[str, Any]:
        _no_store(response)
        return sanitize_json(
            {
                "build_info": dict(build_payload),
                "worker_status": _read_json(resolved_data_root / "hl_worker_status.json"),
                "artifact_freshness": collect_artifact_freshness(resolved_data_root),
            }
        )

    return app


app = create_app()
