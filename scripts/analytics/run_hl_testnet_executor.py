#!/usr/bin/env python3
"""Hyperliquid Testnet ALO execution runner (dry-run by default, hard-pinned to Testnet).

Reads HL target weights from ``data/paper/hl_metamodel/state.json`` (or ``--targets``),
fetches live Hyperliquid Testnet metadata + L2 top-of-book, runs kill-switch checks,
and plans (or executes on Testnet if ``--execute-testnet`` + Testnet keys are set)
sliced post-only ``ALO`` child orders.
"""

from __future__ import annotations

import json
import math
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

from hl_quant import hl_post_trade_attribution as pta  # noqa: E402
from hl_quant import hl_testnet_executor as ex  # noqa: E402
from hl_quant import hl_testnet_risk as risk  # noqa: E402
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
    audit_log: Path | None = typer.Option(
        None, help="Append-only execution audit log (default: data/paper/hl_testnet_audit.jsonl in live mode)"
    ),
    hwm_state: Path = typer.Option(
        ROOT / "data/paper/hl_testnet_hwm.json", help="Persisted equity high-water mark for the drawdown kill-switch"
    ),
    mm_audit_log: Path = typer.Option(
        ROOT / "data/paper/hl_mm_testnet/mm_audit.jsonl",
        help="MM order log; its order ids (and their coins) are excluded from executor cancels/closes",
    ),
    mm_coins: str | None = typer.Option(
        None, help="Comma-separated coins owned by the MM on this address (default: env HL_MM_COINS)"
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
        mm_order_oids = ex.load_logged_order_oids(mm_audit_log)
        raw_mm_coins = mm_coins if mm_coins is not None else os.getenv("HL_MM_COINS", "")
        foreign_coin_set = {c.strip() for c in raw_mm_coins.split(",") if c.strip()} | {
            str(o.get("coin") or "")
            for o in account.open_orders
            if ex.safe_int(o.get("oid")) in mm_order_oids
        }
        foreign_coin_set.discard("")
        if foreign_coin_set:
            valid_targets = {c: w for c, w in valid_targets.items() if c not in foreign_coin_set}
        coins_to_quote = sorted(set(valid_targets.keys()) | set(account.positions_sz.keys()))
        book_tops = ex.fetch_testnet_book_tops(client, coins_to_quote)
        bbo = ex.two_sided_bbo(book_tops)
        try:
            # Oracle prices let the executor close positions whose Testnet book is one-sided/empty
            # and cap HWM ratcheting against thin-book mark spikes.
            asset_ctxs = risk.fetch_testnet_asset_ctxs(client)
            reference_px = {c: ctx.oracle_px for c, ctx in asset_ctxs.items()}
        except (httpx.HTTPError, ValueError):
            asset_ctxs = {}
            reference_px = {}
        skipped_unquoted: list[str] = []
        mm_gross_w = (
            sum(
                abs(float(account.position_notional_usd.get(c, 0.0) or 0.0)) / account.equity_usd
                for c in foreign_coin_set
                if abs(float(account.positions_sz.get(c, 0.0) or 0.0)) > 1e-12
            )
            if account.equity_usd > 0
            else 0.0
        )
        base_max_gross = max(0.0, min(0.90, limits.max_gross_leverage) - mm_gross_w)
        # Neutralize/cap strategy intent across all valid coins first so orphan_target_weights is
        # dollar-centered and respects single-coin caps instead of raw un-centered hl_weights.
        if targets is None and neutralize_net:
            intended_targets = ex.neutralize_target_weights(
                valid_targets,
                max_gross=base_max_gross,
                max_single_coin_weight=limits.max_single_coin_weight,
            )
        else:
            intended_targets = {
                c: math.copysign(min(abs(w), limits.max_single_coin_weight), w)
                if limits.max_single_coin_weight > 0
                else w
                for c, w in valid_targets.items()
            }
        if targets is None:
            valid_targets, skipped_unquoted = ex.filter_unquoted_targets(
                valid_targets,
                bbo,
                max_missing_fraction=limits.max_unquoted_coin_fraction,
                book_tops=book_tops,
            )
            if neutralize_net:
                orphan_net_w = 0.0
                orphan_gross_w = 0.0
                # Offset only the orphan exposure that survives this cycle's planned unwinds.
                residual_ntl = (
                    ex.orphan_residual_notional_usd(
                        account,
                        meta,
                        bbo,
                        book_tops,
                        reference_px,
                        limits=limits,
                        exclude_coins=foreign_coin_set,
                        target_weights=intended_targets,
                    )
                    if limits.unwind_unquoted_orphans
                    else {}
                )
                if account.equity_usd > 0:
                    for c, sz in account.positions_sz.items():
                        if abs(sz) <= 1e-12 or c in valid_targets:
                            continue
                        ntl = float(account.position_notional_usd.get(c, 0.0) or 0.0)
                        if c not in bbo:
                            kept = residual_ntl.get(c, ntl)
                            orphan_net_w += kept / account.equity_usd
                            orphan_gross_w += abs(kept) / account.equity_usd
                        elif c in foreign_coin_set:
                            # MM inventory stays on the book: reserve its gross, leave its net to the MM.
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
    # Cap HWM ratcheting at oracle-adjusted equity so thin-book mark spikes cannot inflate HWM,
    # and never mutate persisted HWM or production audit log during dry-run executions.
    stored_hwm = ex.load_high_water_mark(hwm_state) if account_addr else 0.0
    # Net-of-flows HWM: shift the stored peak by external deposits/withdrawals since it was saved,
    # so withdrawing capital is not read as a drawdown (and a deposit is not read as profit).
    cur_net_flow: float | None = None
    if account_addr:
        try:
            with httpx.Client(timeout=15.0) as ledger_client:
                ledger = pta.fetch_all_user_ledger_updates(ledger_client, account_addr)
            cur_net_flow = pta.net_external_usdc_flow_usd(ledger, account_addr)
            if cur_net_flow is None:
                cur_net_flow = 0.0  # ledger readable but no external flows yet
        except Exception:  # noqa: BLE001 - ledger outage: keep the stored HWM unchanged
            cur_net_flow = None
        prev_net_flow = ex.load_hwm_net_flow(hwm_state)
        if prev_net_flow is None and cur_net_flow is not None and stored_hwm > 0:
            prev_net_flow = cur_net_flow  # first run with flow tracking: baseline, no adjustment
        stored_hwm = ex.flow_adjusted_high_water_mark(stored_hwm, prev_net_flow, cur_net_flow)
    oracle_eq = risk.oracle_adjusted_equity_usd(account, asset_ctxs) if asset_ctxs else account.equity_usd
    ratchet_eq = oracle_eq if (math.isfinite(oracle_eq) and oracle_eq > 0.0) else 0.0
    hwm = max(stored_hwm, ratchet_eq)
    eff_audit_log = audit_log if audit_log is not None else ((ROOT / "data/paper/hl_testnet_audit.jsonl") if execute_testnet else None)
    result = ex.execute_rebalance_cycle(
        account,
        valid_targets,
        meta,
        bbo,
        adapter,
        limits=limits,
        high_water_mark_usd=hwm,
        audit_log_path=eff_audit_log,
        funding_apr_by_coin=funding_apr_by_coin,
        skipped_unquoted_coins=skipped_unquoted,
        book_tops=book_tops,
        reference_px=reference_px,
        foreign_order_oids=mm_order_oids,
        foreign_coins=foreign_coin_set,
        orphan_target_weights=intended_targets,
    )
    if execute_testnet and account_addr and math.isfinite(account.equity_usd) and account.equity_usd > 0.0 and hwm > 0.0:
        if ex.hwm_persist_allowed(cur_net_flow, ex.load_hwm_net_flow(hwm_state)):
            ex.save_high_water_mark(hwm_state, hwm, net_external_flow_usd=cur_net_flow)
    tg_resp = None
    if telegram:
        token, chat_id, thread_id = pt.load_telegram_credentials(
            (ROOT / ".env",)
        )
        if token and chat_id:
            try:
                rich_html = ex.format_testnet_telegram_rich_html(account, result, bbo, hwm_usd=hwm)
                tg_resp = pt.send_telegram_rich_message(rich_html, bot_token=token, chat_id=chat_id, thread_id=thread_id)
            except Exception as exc:
                typer.echo(f"telegram_warn={exc}", err=True)
    typer.echo(
        json.dumps(
            {
                "mode": "testnet_live" if execute_testnet else "testnet_dry_run",
                "testnet_url": ex.TESTNET_API_URL,
                "equity_usd": account.equity_usd,
                "valid_target_coins": len(valid_targets),
                "filtered_isolated_coins": filtered_isolated,
                "quoted_coins": len(bbo),
                **{k: v for k, v in result.items() if k not in {"orders", "mode"}},
                "telegram": tg_resp,
                "sample_orders": result["orders"][:5],
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    app()
