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
    match = re.search(rf"(?m)^{re.escape(key)}=['\"]?([^'\"\r\n]+)['\"]?$", env_text or "")
    return match.group(1).strip() if match else None


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
        except (HTTPError, URLError, ValueError, OSError) as exc:
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

    def trigger_deploy(self, compose_id: str, title: str) -> None:
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

    before_ids = {str(item.get("deploymentId")) for item in client.list_deployments(compose_id)}
    client.trigger_deploy(compose_id, title=f"CI tested release {sha[:12]}")

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
                break
            if status in {"error", "failed", "cancelled"}:
                raise RuntimeError(f"Dokploy deployment {tracked_id} ended with status='{status}'")
        time.sleep(poll_interval_s)
    else:
        raise RuntimeError(f"Dokploy deployment timed out after {deploy_timeout_s}s")

    verified: dict[str, Any] = {}
    if public_url:
        base = public_url.rstrip("/")
        checks = {
            "frontend_build": f"{base}/build-info.json",
            "backend_build": f"{base}/api/v1/build-info.json",
            "backend_health": f"{base}/api/v1/healthz",
        }
        verify_deadline = time.monotonic() + verify_timeout_s
        last_err = ""
        while time.monotonic() < verify_deadline:
            try:
                fe = fetch_json(checks["frontend_build"])
                be = fetch_json(checks["backend_build"])
                hz = fetch_json(checks["backend_health"])
                if fe.get("sha") == sha and be.get("sha") == sha and hz.get("status") == "ok":
                    verified = {"frontend": fe, "backend": be, "health": hz}
                    print(f"[deploy] live verification passed for sha={sha[:12]}", flush=True)
                    break
                last_err = f"mismatch fe={fe.get('sha')} be={be.get('sha')} health={hz.get('status')}"
            except Exception as exc:
                last_err = str(exc)
            time.sleep(min(poll_interval_s, 5.0))
        else:
            raise RuntimeError(f"Post-deploy verification failed after {verify_timeout_s}s ({last_err})")

    return {
        "compose_id": compose_id,
        "deployment_id": tracked_id,
        "previous_sha": previous_sha,
        "deployed_sha": sha,
        "verified": verified,
    }


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
