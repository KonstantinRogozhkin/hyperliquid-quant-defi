from __future__ import annotations

import json
import math
from pathlib import Path

from fastapi.testclient import TestClient
import pandas as pd

from hl_quant.api.app import create_app, sanitize_json


def test_sanitize_json_strips_nan_and_inf():
    raw = {
        "a": float("nan"),
        "b": float("inf"),
        "c": float("-inf"),
        "d": 12.5,
        "nested": [{"x": float("nan"), "y": "ok"}],
    }
    clean = sanitize_json(raw)
    assert clean == {"a": None, "b": None, "c": None, "d": 12.5, "nested": [{"x": None, "y": "ok"}]}


def test_api_health_and_build_info(tmp_path: Path):
    app = create_app(data_root=tmp_path, build_sha="abc123def456", built_at="2026-11-26T20:00:00Z")
    client = TestClient(app)

    r_health = client.get("/healthz")
    assert r_health.status_code == 200
    assert r_health.headers["cache-control"] == "no-store"
    assert r_health.json() == {
        "status": "ok",
        "service": "hl-backend",
        "sha": "abc123def456",
        "data_root_exists": True,
    }

    for path in ("/build-info.json", "/api/v1/build-info.json"):
        r_info = client.get(path)
        assert r_info.status_code == 200
        assert r_info.headers["cache-control"] == "no-store"
        assert r_info.json()["sha"] == "abc123def456"
        assert r_info.json()["service"] == "hl-backend"


def test_api_endpoints_handle_empty_and_populated_data_root(tmp_path: Path):
    # 1. Empty data root must return clean 200 payloads without crashing
    app = create_app(data_root=tmp_path, build_sha="deadbeef")
    client = TestClient(app)
    r_empty = client.get("/api/v1/overview")
    assert r_empty.status_code == 200
    body_empty = r_empty.json()
    assert body_empty["build_info"]["sha"] == "deadbeef"
    assert body_empty["artifact_freshness"]["frontier_history"]["exists"] is False

    # 2. Populate synthetic frontier, events, attribution, and parquet with NaN
    (tmp_path / "hl_frontier_history").mkdir(parents=True)
    (tmp_path / "hl_frontier_history" / "latest_summary.json").write_text(
        json.dumps(
            {
                "observed_at_utc": "2026-11-26 22:42 UTC",
                "ny_session": "US_AFTERHOURS",
                "hlp_apr_pct": 15.7,
                "total_logged": {"hip3_rows": 2, "boros_rows": 1, "yield_rows": 1},
                "hip3_persistence": [
                    {
                        "base_symbol": "AAPL",
                        "short_symbol": "xyz:AAPL",
                        "long_symbol": "io:AAPL",
                        "clean_streak": 2,
                        "direction_flips": 0,
                        "latest_apr_pct": 29.6,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    pd.DataFrame([{"ts_ms": 1, "base_symbol": "AAPL", "spread_apr_pct": math.nan}]).to_parquet(
        tmp_path / "hl_frontier_history" / "hip3_history.parquet", index=False
    )

    (tmp_path / "hl_onchain_events").mkdir(parents=True)
    (tmp_path / "hl_onchain_events" / "latest_snapshot.json").write_text(
        json.dumps({"observed_at_utc": "2026-11-26 22:45 UTC", "alerts_count": 1, "any_alert": True}),
        encoding="utf-8",
    )

    (tmp_path / "hl_testnet_attribution").mkdir(parents=True)
    (tmp_path / "hl_testnet_attribution" / "attribution.json").write_text(
        json.dumps({"equity_usd": 995.5, "total_fills": 14, "maker_volume_share_pct": 92.3}),
        encoding="utf-8",
    )

    (tmp_path / "hl_worker_status.json").write_text(
        json.dumps({"service": "hl-worker", "heartbeat_utc": "2026-11-26 22:45:00 UTC", "jobs": []}),
        encoding="utf-8",
    )

    r_hip3 = client.get("/api/v1/hip3/persistence")
    assert r_hip3.status_code == 200
    hip3_body = r_hip3.json()
    assert hip3_body["frontier_history"]["hip3_persistence"][0]["base_symbol"] == "AAPL"
    assert hip3_body["history_tail"][0]["spread_apr_pct"] is None

    r_events = client.get("/api/v1/events/latest")
    assert r_events.status_code == 200
    assert r_events.json()["onchain_events"]["alerts_count"] == 1

    r_attr = client.get("/api/v1/attribution")
    assert r_attr.status_code == 200
    assert r_attr.json()["attribution"]["total_fills"] == 14

    r_sched = client.get("/api/v1/scheduler/status")
    assert r_sched.status_code == 200
    sched_body = r_sched.json()
    assert sched_body["worker_status"]["service"] == "hl-worker"
    assert sched_body["artifact_freshness"]["frontier_history"]["exists"] is True
