"""Regression tests for CLI --help hygiene, shell script safety, and atomic artifact writes."""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
from typer.testing import CliRunner

from hl_quant import cross_usdc_micro_execution as cme

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_CLI_SCRIPTS = sorted((REPO_ROOT / "scripts" / "analytics").glob("*.py")) + sorted(
    (REPO_ROOT / "scripts" / "ops").glob("*.py")
)
ZSH_OPS_SCRIPTS = sorted((REPO_ROOT / "scripts" / "ops").glob("*.sh"))


def _load_script_module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deploy_dokploy_cli_help_and_arg_validation(monkeypatch, capsys) -> None:
    deploy_mod = _load_script_module(REPO_ROOT / "scripts" / "ops" / "deploy_dokploy.py")
    for var in ("DOKPLOY_API_URL", "DOKPLOY_API_KEY", "DOKPLOY_COMPOSE_ID"):
        monkeypatch.delenv(var, raising=False)

    assert deploy_mod.main(["deploy_dokploy.py", "--help"]) == 0
    out_help = capsys.readouterr()
    assert "Usage: deploy_dokploy.py <40-char-git-sha>" in out_help.out
    assert "DOKPLOY_API_URL" in out_help.out

    assert deploy_mod.main(["deploy_dokploy.py", "-h"]) == 0
    _ = capsys.readouterr()

    assert deploy_mod.main(["deploy_dokploy.py"]) == 2
    assert "Usage:" in capsys.readouterr().err

    assert deploy_mod.main(["deploy_dokploy.py", "not-a-valid-sha"]) == 2
    assert "40-char lowercase hex git SHA" in capsys.readouterr().err

    monkeypatch.setenv("DOKPLOY_DEPLOY_TIMEOUT_S", "not-an-int")
    assert deploy_mod.main(["deploy_dokploy.py", "a" * 40]) == 2
    assert "DOKPLOY_DEPLOY_TIMEOUT_S" in capsys.readouterr().err


@pytest.mark.parametrize("script_path", PY_CLI_SCRIPTS, ids=lambda p: f"{p.parent.name}/{p.name}")
def test_all_python_cli_scripts_support_help_without_secrets(script_path: Path) -> None:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("DOKPLOY_", "HL_TESTNET_", "TELEGRAM_"))
    }
    proc = subprocess.run(
        [sys.executable, str(script_path), "--help"],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert proc.returncode == 0, f"{script_path} --help failed ({proc.returncode}): {proc.stderr}"
    assert proc.stdout.strip(), f"{script_path} --help produced empty stdout"


def test_colab_shard_bootstraps_hl_quant_path_without_editable_install() -> None:
    src = (REPO_ROOT / "scripts" / "analytics" / "run_hl_colab_shard.py").read_text(encoding="utf-8")
    assert '(candidate / "hl_quant").exists()' in src
    assert "overnight" not in src


@pytest.mark.skipif(shutil.which("zsh") is None, reason="zsh is not installed on this runner")
@pytest.mark.parametrize("script_path", ZSH_OPS_SCRIPTS, ids=lambda p: p.name)
def test_all_zsh_ops_scripts_syntax_realpath_and_help(script_path: Path) -> None:
    content = script_path.read_text(encoding="utf-8")
    assert "${0:A}" in content, f"{script_path.name} must resolve symlinks via ${{0:A}}"

    syntax = subprocess.run(
        ["zsh", "-n", str(script_path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert syntax.returncode == 0, f"zsh -n {script_path.name} failed: {syntax.stderr}"

    help_proc = subprocess.run(
        ["zsh", str(script_path), "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert help_proc.returncode == 0, f"zsh {script_path.name} --help failed: {help_proc.stderr}"
    assert "Usage:" in help_proc.stdout


def _sample_contract(run_id: str = "run-atomic-01") -> cme.MicroExecutionContract:
    return cme.MicroExecutionContract(
        strategy_id="cross-usdc-micro-v1",
        run_id=run_id,
        created_at_utc="2026-11-28T00:00:00+00:00",
        assets=("BTC", "ETH"),
        binance_spot_usdc_notional=Decimal("50"),
        bybit_perp_short_usdc_notional=Decimal("25"),
        hyperliquid_perp_short_usdc_notional=Decimal("25"),
        max_batch_gross_usdc=Decimal("100"),
        max_adverse_execution_bps=Decimal("5"),
        entry_deadline_seconds=10,
        hold_seconds=60,
        max_delta_mismatch_fraction=Decimal("0.001"),
        minimum_liquidation_distance_fraction=Decimal("0.20"),
        minimum_free_margin_to_required_im=Decimal("2.0"),
        required_perp_leverage=Decimal("3"),
        max_snapshot_sync_span_ms=500,
        spot_quantity_mode="match_rounded_perps",
        minimum_actual_spot_notional_fraction=Decimal("0.90"),
        max_bybit_short_weight_deviation_fraction=Decimal("0.02"),
        credential_prefix="TEST",
        execution_mode="testnet_only",
    )


def test_write_frozen_contract_atomic_and_conflict_detection(tmp_path: Path) -> None:
    contract = _sample_contract("run-atomic-01")
    out = tmp_path / "contracts" / "frozen.json"
    written = cme.write_frozen_contract(out, contract)
    assert out.exists()
    assert not list(out.parent.glob(".*.tmp"))
    # Idempotent re-write of identical contract succeeds cleanly
    reused = cme.write_frozen_contract(out, contract)
    assert reused["contract_sha256"] == written["contract_sha256"]

    # Mutated contract for same path raises MicroExecutionError
    conflicting = replace(contract, hold_seconds=120)
    with pytest.raises(cme.MicroExecutionError, match="immutable_contract_conflict"):
        cme.write_frozen_contract(out, conflicting)


def test_build_hl_portfolio_dashboard_writes_atomically(tmp_path: Path) -> None:
    mod = _load_script_module(REPO_ROOT / "scripts" / "analytics" / "build_hl_portfolio_dashboard.py")
    data_root = tmp_path / "paper"
    data_root.mkdir(parents=True)
    out_html = tmp_path / "reports" / "hl_desk_dashboard.html"
    runner = CliRunner()
    res = runner.invoke(mod.app, ["--data-root", str(data_root), "--output-html", str(out_html)])
    assert res.exit_code == 0, res.output
    assert out_html.exists()
    assert (data_root / "hl_desk_dashboard.html").exists()
    assert not list(out_html.parent.glob(".*.tmp"))
    assert not list(data_root.glob(".*.tmp"))
