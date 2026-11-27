#!/usr/bin/env python3
"""Hyperliquid Testnet ALO execution runner (dry-run by default, hard-pinned to Testnet).

Reads HL target weights from ``data/paper/hl_metamodel/state.json`` (or ``--targets``),
fetches live Hyperliquid Testnet metadata + L2 top-of-book, runs kill-switch checks,
and plans (or executes on Testnet if ``--execute-testnet`` + Testnet keys are set)
sliced post-only ``ALO`` child orders.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time

import httpx
import typer

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from hl_quant import hl_testnet_executor as ex  # noqa: E402
from hl_quant import paper_tracker as pt  # noqa: E402

app = typer.Typer(add_completion=False, help=__doc__)


def _load_dotenv(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        if k and not os.environ.get(k):
            os.environ[k] = v.strip().strip('"').strip("'")


def _load_targets(state_json: Path | None, targets_str: str | None) -> dict[str, float]:
    if targets_str:
        out: dict[str, float] = {}
        for part in targets_str.split(","):
            if "=" not in part:
                continue
            coin, w = part.split("=", 1)
            out[coin.strip()] = float(w.strip())
        return out
    if state_json is not None and state_json.is_file():
        st = json.loads(state_json.read_text())
        return {
            coin: float(p.get("hl_weight", 0.0))
            for coin, p in (st.get("positions") or {}).items()
            if abs(float(p.get("hl_weight", 0.0))) >= 1e-4
        }
    raise typer.BadParameter(f"no target weights: {state_json} not found and --targets not given")


@app.command()
def main(
    state_json: Path = typer.Option(ROOT / "data/paper/hl_metamodel/state.json", help="Metamodel paper state.json"),
    targets: str | None = typer.Option(None, help="Optional override, e.g. BTC=0.10,ETH=-0.10"),
    execute_testnet: bool = typer.Option(False, help="Submit live orders to Hyperliquid TESTNET (requires env keys)"),
    simulated_equity_usd: float = typer.Option(10_000.0, help="Equity used in dry-run when no Testnet account is set"),
    max_child_usd: float = typer.Option(1_000.0, help="Max USD per sliced ALO child order"),
    allow_only_isolated: bool = typer.Option(False, help="Allow opening exchange onlyIsolated/strictIsolated coins"),
    default_isolated_leverage: int = typer.Option(2, help="Safe leverage cap for isolated-only coins when enabled"),
    default_cross_leverage: int = typer.Option(3, help="Default cross-margin leverage applied before new entries"),
    neutralize_net: bool = typer.Option(True, help="Re-center HL weights to dollar-neutral for single-venue Testnet"),
    telegram: bool = typer.Option(False, help="Send Rich HTML summary to Telegram"),
    audit_log: Path = typer.Option(ROOT / "data/paper/hl_testnet_audit.jsonl", help="Append-only execution audit log"),
    hwm_state: Path = typer.Option(
        ROOT / "data/paper/hl_testnet_hwm.json", help="Persisted equity high-water mark for the drawdown kill-switch"
    ),
) -> None:
    _load_dotenv(ROOT / ".env")
    target_weights = _load_targets(state_json, targets)
    account_addr = os.getenv("HL_TESTNET_ACCOUNT_ADDRESS", "").strip()
    priv_key = os.getenv("HL_TESTNET_PRIVATE_KEY", "").strip()

    limits = ex.RiskLimits(
        max_child_usd=max_child_usd,
        allow_only_isolated=allow_only_isolated,
        default_isolated_leverage=default_isolated_leverage,
        default_cross_leverage=default_cross_leverage,
    )
    with httpx.Client(timeout=15.0) as client:
        meta = ex.fetch_testnet_meta(client)
        # Keep only coins listed on Hyperliquid Testnet (and skip isolated-only coins unless opted in)
        filtered_isolated = sorted(c for c in target_weights if c in meta and meta[c].only_isolated and not allow_only_isolated)
        valid_targets = {
            c: w
            for c, w in target_weights.items()
            if c in meta and (allow_only_isolated or not meta[c].only_isolated)
        }
        if account_addr:
            account = ex.fetch_testnet_account(client, account_addr)
        else:
            account = ex.AccountSnapshot(
                equity_usd=simulated_equity_usd,
                free_margin_usd=simulated_equity_usd,
                positions_sz={},
                open_orders=[],
                observed_at_ms=int(time.time() * 1000),
            )
        coins_to_quote = sorted(set(valid_targets.keys()) | set(account.positions_sz.keys()))
        bbo = ex.fetch_testnet_bbo(client, coins_to_quote)
        skipped_unquoted: list[str] = []
        if targets is None:
            valid_targets, skipped_unquoted = ex.filter_unquoted_targets(
                valid_targets,
                bbo,
                max_missing_fraction=limits.max_unquoted_coin_fraction,
            )
            if neutralize_net:
                orphan_net_w = 0.0
                orphan_gross_w = 0.0
                if account.equity_usd > 0:
                    for c, sz in account.positions_sz.items():
                        if abs(sz) > 1e-12 and c not in bbo:
                            ntl = float(account.position_notional_usd.get(c, 0.0) or 0.0)
                            orphan_net_w += ntl / account.equity_usd
                            orphan_gross_w += abs(ntl) / account.equity_usd
                rem_gross = max(0.0, min(0.90, limits.max_gross_leverage) - orphan_gross_w)
                valid_targets = ex.neutralize_target_weights(
                    valid_targets,
                    max_gross=rem_gross,
                    max_single_coin_weight=limits.max_single_coin_weight,
                    fixed_net_weight=orphan_net_w,
                )
        funding_apr_by_coin = ex.fetch_testnet_funding_apr(client)

    adapter: ex.ExchangeAdapter
    if execute_testnet:
        if not (priv_key and account_addr):
            raise typer.BadParameter("HL_TESTNET_PRIVATE_KEY and HL_TESTNET_ACCOUNT_ADDRESS are required for --execute-testnet")
        adapter = ex.HyperliquidTestnetSDKAdapter(priv_key, account_addr)
    else:
        adapter = ex.DryRunAdapter()
    # The drawdown gate needs the peak equity of the real account across runs; the
    # simulated dry-run account has constant equity and keeps no high-water mark.
    stored_hwm = ex.load_high_water_mark(hwm_state) if account_addr else 0.0
    hwm = max(stored_hwm, account.equity_usd)
    result = ex.execute_rebalance_cycle(
        account,
        valid_targets,
        meta,
        bbo,
        adapter,
        limits=limits,
        high_water_mark_usd=hwm,
        audit_log_path=audit_log,
        funding_apr_by_coin=funding_apr_by_coin,
        skipped_unquoted_coins=skipped_unquoted,
    )
    if account_addr:
        ex.save_high_water_mark(hwm_state, hwm)
    tg_resp = None
    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            rich_html = ex.format_testnet_telegram_rich_html(account, result, bbo, hwm_usd=hwm)
            tg_resp = pt.send_telegram_rich_message(rich_html, bot_token=token, chat_id=chat_id, thread_id=thread_id)
    typer.echo(
        json.dumps(
            {
                "mode": "testnet_live" if execute_testnet else "testnet_dry_run",
                "testnet_url": ex.TESTNET_API_URL,
                "equity_usd": account.equity_usd,
                "valid_target_coins": len(valid_targets),
                "filtered_isolated_coins": filtered_isolated,
                "quoted_coins": len(bbo),
                **{k: v for k, v in result.items() if k != "orders"},
                "telegram": tg_resp,
                "sample_orders": result["orders"][:5],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    app()
