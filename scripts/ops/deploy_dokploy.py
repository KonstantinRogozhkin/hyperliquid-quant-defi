#!/usr/bin/env python3
"""Deploy an immutable tested SHA release to a Dokploy Docker Compose stack and verify live probes.

All infrastructure coordinates and credentials come from environment variables so this script
stays safe and portable in a public repository:

Required env:
  DOKPLOY_API_URL        Base URL ending in ``/api`` (e.g. ``https://dokploy.example.com/api``)
  DOKPLOY_API_KEY        Dokploy API token (sent via ``x-api-key`` header)
  DOKPLOY_COMPOSE_ID     Target Dokploy Compose ID

Optional env:
  DOKPLOY_PUBLIC_URL     Public HTTPS origin (e.g. ``https://hl.example.com``) for post-deploy checks
  DOKPLOY_SHA_VAR        Env var name in Dokploy Compose (default: ``DEPLOY_SHA``)
  DOKPLOY_ALLOW_AUTO_DEPLOY  Set ``true`` to skip the ``autoDeploy == false`` safety check
  DOKPLOY_DEPLOY_TIMEOUT_S   Max seconds to wait for Dokploy deployment completion (default: 600)
  DOKPLOY_VERIFY_TIMEOUT_S   Max seconds to poll live ``/build-info.json`` endpoints (default: 180)
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

SHA_PATTERN = re.compile(r"^[0-9a-f]{40}\Z")


def _require_env(name: str) -> str:
    val = os.environ.get(name, "").strip()
    if not val:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return val


def upsert_env_var(env_text: str, key: str, value: str) -> str:
    """Replace ``KEY=...`` in Dokploy dotenv text or append it cleanly without touching other keys."""
    pattern = re.compile(rf"(?m)^{re.escape(key)}=.*$")
    line = f"{key}={value}"
    if pattern.search(env_text):
        return pattern.sub(line, env_text, count=1)
    if env_text and not env_text.endswith("\n"):
        return f"{env_text}\n{line}\n"
    return f"{env_text}{line}\n"


def extract_env_var(env_text: str, key: str) -> str | None:
    match = re.search(rf"(?m)^\s*{re.escape(key)}\s*=\s*(.*)$", env_text or "")
    if not match:
        return None
    raw = match.group(1).strip()
    if not raw:
        return ""
    if raw[0] in ('"', "'"):
        quote = raw[0]
        end_idx = raw.find(quote, 1)
        if end_idx != -1:
            return raw[1:end_idx].strip()
        raw = raw[1:]
    raw = raw.split("#", 1)[0].strip()
    return raw.strip("\"'").strip()


class DokployClient:
    def __init__(self, api_url: str, api_key: str, *, timeout_s: int = 20) -> None:
        self.api_url = api_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s

    def request(self, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        req = Request(
            f"{self.api_url}/{endpoint.lstrip('/')}",
            data=body,
            headers={
                "x-api-key": self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "hyperliquid-quant-defi-deploy/1.0",
            },
            method="GET" if body is None else "POST",
        )
        try:
            with urlopen(req, timeout=self.timeout_s) as resp:
                raw = resp.read()
            return json.loads(raw) if raw else {}
        except HTTPError as exc:
            # Never echo request payload: compose env contains deployment secrets.
            endpoint_name = endpoint.split("?", 1)[0]
            snippet = ""
            try:
                err_body = exc.read()
                if err_body:
                    snippet = err_body.decode("utf-8", errors="replace").strip().replace("\n", " ")[:200]
            except Exception:
                snippet = ""
            detail = f"HTTP {exc.code}" + (f": {snippet}" if snippet else "")
            raise RuntimeError(f"Dokploy API call '{endpoint_name}' failed ({detail})") from exc
        except (URLError, ValueError, OSError) as exc:
            # Never echo request payload: compose env contains deployment secrets.
            endpoint_name = endpoint.split("?", 1)[0]
            raise RuntimeError(f"Dokploy API call '{endpoint_name}' failed: {type(exc).__name__}") from exc

    def get_compose(self, compose_id: str) -> dict[str, Any]:
        row = self.request(f"compose.one?{urlencode({'composeId': compose_id})}")
        if not isinstance(row, dict) or row.get("composeId") != compose_id:
            raise RuntimeError("Dokploy compose.one returned an unexpected compose record")
        return row

    def update_compose_env(self, compose_id: str, env_text: str) -> None:
        self.request("compose.update", {"composeId": compose_id, "env": env_text})

    def list_deployments(self, compose_id: str) -> list[dict[str, Any]]:
        rows = self.request(f"deployment.allByCompose?{urlencode({'composeId': compose_id})}")
        return rows if isinstance(rows, list) else []

    def trigger_deploy(self, compose_id: str, title: str = "Automated Dokploy deploy") -> None:
        self.request("compose.deploy", {"composeId": compose_id, "title": title})


def fetch_json(url: str, *, timeout_s: int = 10) -> dict[str, Any]:
    req = Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "hyperliquid-quant-defi-verify/1.0",
        },
    )
    with urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected JSON object from {url}")
    return data


def wait_for_latest_deployment(
    client: DokployClient,
    *,
    compose_id: str,
    before_ids: set[str],
    deploy_timeout_s: int,
    poll_interval_s: float,
) -> str | None:
    deadline = time.monotonic() + deploy_timeout_s
    tracked_id: str | None = None
    while time.monotonic() < deadline:
        try:
            rows = client.list_deployments(compose_id)
        except RuntimeError as exc:
            print(f"[deploy] transient poll error, retrying: {exc}", flush=True)
            time.sleep(poll_interval_s)
            continue

        if tracked_id is None:
            fresh = [item for item in rows if str(item.get("deploymentId")) not in before_ids]
            if len(fresh) > 1:
                raise RuntimeError("Multiple concurrent Dokploy deployments detected; refusing to guess")
            if fresh:
                tracked_id = str(fresh[0].get("deploymentId"))
                print(f"[deploy] tracking Dokploy deployment {tracked_id}", flush=True)

        current = next((item for item in rows if tracked_id and str(item.get("deploymentId")) == tracked_id), None)
        if current is not None:
            status = str(current.get("status") or "").lower()
            if status == "done":
                print(f"[deploy] Dokploy deployment {tracked_id} finished cleanly", flush=True)
                return tracked_id
            if status in {"error", "failed", "cancelled"}:
                raise RuntimeError(f"Dokploy deployment {tracked_id} ended with status='{status}'")
        time.sleep(poll_interval_s)
    raise RuntimeError(f"Dokploy deployment timed out after {deploy_timeout_s}s")


def verify_live_probes(
    public_url: str,
    *,
    expected_sha: str,
    verify_timeout_s: int = 180,
    poll_interval_s: float = 10.0,
) -> dict[str, Any]:
    if not public_url:
        return {}
    base = public_url.rstrip("/")
    checks = {
        "frontend_build": f"{base}/build-info.json",
        "backend_build": f"{base}/api/v1/build-info.json",
        "backend_health": f"{base}/api/v1/healthz",
        "scheduler_status": f"{base}/api/v1/scheduler/status",
    }
    verify_deadline = time.monotonic() + verify_timeout_s
    last_err = ""
    while time.monotonic() < verify_deadline:
        try:
            fe = fetch_json(checks["frontend_build"])
            be = fetch_json(checks["backend_build"])
            hz = fetch_json(checks["backend_health"])
            sc = fetch_json(checks["scheduler_status"])
            sc_build = sc.get("build_info")
            sc_sha = sc_build.get("sha") if isinstance(sc_build, dict) else None
            ws = sc.get("worker_status")
            ws_valid = ws is None or isinstance(ws, dict)
            if (
                fe.get("sha") == expected_sha
                and be.get("sha") == expected_sha
                and hz.get("status") == "ok"
                and sc_sha == expected_sha
                and ws_valid
            ):
                verified = {"frontend": fe, "backend": be, "health": hz, "scheduler": sc}
                print(f"[deploy] live verification passed for sha={expected_sha[:12]}", flush=True)
                return verified
            last_err = (
                f"mismatch fe={fe.get('sha')} be={be.get('sha')} "
                f"health={hz.get('status')} sched_sha={sc_sha} ws_valid={ws_valid}"
            )
        except Exception as exc:
            last_err = str(exc)
        time.sleep(min(poll_interval_s, 5.0))
    raise RuntimeError(f"Post-deploy verification failed after {verify_timeout_s}s ({last_err})")


def deploy_sha_to_dokploy(
    client: DokployClient,
    *,
    compose_id: str,
    sha: str,
    sha_var: str = "DEPLOY_SHA",
    public_url: str = "",
    allow_auto_deploy: bool = False,
    deploy_timeout_s: int = 600,
    verify_timeout_s: int = 180,
    poll_interval_s: float = 10.0,
) -> dict[str, Any]:
    """Pin ``DEPLOY_SHA``, trigger a Dokploy compose deploy, wait for completion, and verify live probes."""
    if not SHA_PATTERN.match(sha):
        raise ValueError(f"Expected full 40-char lowercase Git SHA, got: {sha!r}")

    row = client.get_compose(compose_id)
    if not allow_auto_deploy and row.get("autoDeploy") is True:
        raise RuntimeError(
            "Dokploy compose has autoDeploy enabled; disable Git auto-deploy so CI controls tested SHA releases "
            "(or set DOKPLOY_ALLOW_AUTO_DEPLOY=true)."
        )

    old_env = row.get("env") or ""
    previous_sha = extract_env_var(old_env, sha_var)
    new_env = upsert_env_var(old_env, sha_var, sha)
    client.update_compose_env(compose_id, new_env)

    refreshed = client.get_compose(compose_id)
    if extract_env_var(refreshed.get("env") or "", sha_var) != sha:
        raise RuntimeError(f"Dokploy did not persist {sha_var}={sha}")

    deploy_triggered = False
    try:
        before_ids = {str(item.get("deploymentId")) for item in client.list_deployments(compose_id)}
        client.trigger_deploy(compose_id, title=f"CI tested release {sha[:12]}")
        deploy_triggered = True
        tracked_id = wait_for_latest_deployment(
            client,
            compose_id=compose_id,
            before_ids=before_ids,
            deploy_timeout_s=deploy_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        verified = verify_live_probes(
            public_url,
            expected_sha=sha,
            verify_timeout_s=verify_timeout_s,
            poll_interval_s=poll_interval_s,
        )
        return {
            "compose_id": compose_id,
            "deployment_id": tracked_id,
            "previous_sha": previous_sha,
            "deployed_sha": sha,
            "verified": verified,
        }
    except Exception:
        if previous_sha != sha:
            try:
                client.update_compose_env(compose_id, old_env)
                print(f"[deploy] rolled back {sha_var} to previous={previous_sha!r} after failure", flush=True)
                if deploy_triggered and previous_sha:
                    rb_before = {str(item.get("deploymentId")) for item in client.list_deployments(compose_id)}
                    client.trigger_deploy(compose_id, title=f"Rollback to {previous_sha[:12]}")
                    wait_for_latest_deployment(
                        client,
                        compose_id=compose_id,
                        before_ids=rb_before,
                        deploy_timeout_s=deploy_timeout_s,
                        poll_interval_s=poll_interval_s,
                    )
                    print(f"[deploy] redeployed previous {sha_var}={previous_sha[:12]} cleanly", flush=True)
            except Exception as rb_exc:
                print(f"[deploy] warning: failed to roll back {sha_var}: {rb_exc}", flush=True)
        raise


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"Usage: {argv[0]} <40-char-git-sha>", file=sys.stderr)
        return 2
    sha = argv[1].strip()
    client = DokployClient(
        api_url=_require_env("DOKPLOY_API_URL"),
        api_key=_require_env("DOKPLOY_API_KEY"),
    )
    result = deploy_sha_to_dokploy(
        client,
        compose_id=_require_env("DOKPLOY_COMPOSE_ID"),
        sha=sha,
        sha_var=os.environ.get("DOKPLOY_SHA_VAR", "DEPLOY_SHA").strip() or "DEPLOY_SHA",
        public_url=os.environ.get("DOKPLOY_PUBLIC_URL", "").strip(),
        allow_auto_deploy=os.environ.get("DOKPLOY_ALLOW_AUTO_DEPLOY", "false").strip().lower() in {"1", "true", "yes"},
        deploy_timeout_s=int(os.environ.get("DOKPLOY_DEPLOY_TIMEOUT_S", "600")),
        verify_timeout_s=int(os.environ.get("DOKPLOY_VERIFY_TIMEOUT_S", "180")),
    )
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
