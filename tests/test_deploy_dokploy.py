from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
from typing import Any

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "ops" / "deploy_dokploy.py"
SPEC = importlib.util.spec_from_file_location("deploy_dokploy", SCRIPT_PATH)
assert SPEC and SPEC.loader
mod = importlib.util.module_from_spec(SPEC)
sys.modules["deploy_dokploy"] = mod
SPEC.loader.exec_module(mod)


def test_upsert_and_extract_env_var():
    raw = "FOO=bar\nDEPLOY_SHA=0000000000000000000000000000000000000000\nSECRET=keep\n"
    sha = "a" * 40
    updated = mod.upsert_env_var(raw, "DEPLOY_SHA", sha)
    assert mod.extract_env_var(updated, "DEPLOY_SHA") == sha
    assert "SECRET=keep" in updated
    assert "FOO=bar" in updated

    appended = mod.upsert_env_var("FOO=bar", "DEPLOY_SHA", sha)
    assert mod.extract_env_var(appended, "DEPLOY_SHA") == sha


class _FakeDokployClient:
    def __init__(self, *, auto_deploy: bool = False, final_status: str = "done") -> None:
        self.auto_deploy = auto_deploy
        self.final_status = final_status
        self.env = "IMAGE_NAMESPACE=konstantinrogozhkin\nDEPLOY_SHA=" + ("1" * 40) + "\n"
        self.deployments: list[dict[str, Any]] = [{"deploymentId": "dep-old", "status": "done"}]
        self.poll_count = 0

    def get_compose(self, compose_id: str) -> dict[str, Any]:
        return {"composeId": compose_id, "autoDeploy": self.auto_deploy, "env": self.env}

    def update_compose_env(self, _compose_id: str, env_text: str) -> None:
        self.env = env_text

    def list_deployments(self, _compose_id: str) -> list[dict[str, Any]]:
        self.poll_count += 1
        if len(self.deployments) > 1 and self.poll_count >= 2:
            last = self.deployments[-1]
            if "Rollback" in str(last.get("title") or ""):
                last["status"] = "done"
            else:
                last["status"] = self.final_status
        return list(self.deployments)

    def trigger_deploy(self, _compose_id: str, title: str = "") -> None:
        dep_id = "dep-new" if len(self.deployments) == 1 else f"dep-rb-{len(self.deployments)}"
        self.deployments.append({"deploymentId": dep_id, "status": "running", "title": title})


def test_deploy_sha_to_dokploy_happy_path_and_live_verification(monkeypatch):
    from datetime import datetime, timezone

    sha = "b" * 40
    client = _FakeDokployClient()
    fresh_hb = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    def fake_fetch_json(url: str, *, timeout_s: int = 10) -> dict[str, Any]:
        if url.endswith("/api/v1/healthz"):
            return {"status": "ok", "sha": sha}
        if url.endswith("/api/v1/scheduler/status"):
            return {
                "build_info": {"sha": sha},
                "worker_status": {"service": "hl-worker", "build_sha": sha, "heartbeat_utc": fresh_hb},
            }
        return {"sha": sha}

    monkeypatch.setattr(mod, "fetch_json", fake_fetch_json)
    res = mod.deploy_sha_to_dokploy(
        client,
        compose_id="cmp-123",
        sha=sha,
        public_url="https://hl.example.com",
        poll_interval_s=0.01,
    )
    assert res["deployment_id"] == "dep-new"
    assert res["previous_sha"] == "1" * 40
    assert res["deployed_sha"] == sha
    assert res["verified"]["backend"]["sha"] == sha
    assert res["verified"]["frontend"]["sha"] == sha


def test_deploy_refuses_auto_deploy_or_bad_sha():
    with pytest.raises(ValueError):
        mod.deploy_sha_to_dokploy(_FakeDokployClient(), compose_id="cmp-1", sha="short")

    with pytest.raises(RuntimeError, match="autoDeploy"):
        mod.deploy_sha_to_dokploy(_FakeDokployClient(auto_deploy=True), compose_id="cmp-1", sha="c" * 40)


def test_deploy_raises_on_failed_dokploy_status():
    client = _FakeDokployClient(final_status="error")
    with pytest.raises(RuntimeError, match="status='error'"):
        mod.deploy_sha_to_dokploy(client, compose_id="cmp-1", sha="d" * 40, poll_interval_s=0.01)


def test_deploy_rolls_back_env_on_failed_dokploy_status():
    client = _FakeDokployClient(final_status="error")
    original_sha = "1" * 40
    with pytest.raises(RuntimeError, match="status='error'"):
        mod.deploy_sha_to_dokploy(client, compose_id="cmp-1", sha="e" * 40, poll_interval_s=0.01)
    assert mod.extract_env_var(client.env, "DEPLOY_SHA") == original_sha
