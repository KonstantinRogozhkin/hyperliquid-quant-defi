#!/usr/bin/env python3
"""Read-only 15-minute live risk probe for the Hyperliquid Testnet execution account.

Reads ``clearinghouseState`` / ``spotClearinghouseState`` / ``openOrders`` / ``metaAndAssetCtxs``
and per-position ``l2Book`` from the public Testnet ``info`` API (never signs or places orders),
compares equity with the executor's persisted high-water mark (read-only; the executor owns it),
and atomically writes ``hl_testnet_risk/latest.json`` for ``/api/v1/overview`` and ``/metrics``.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
import threading
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


@app.command()
def main(
    hwm_state: Path = typer.Option(ROOT / "data/paper/hl_testnet_hwm.json", help="Executor-owned HWM state"),
    metamodel_state: Path = typer.Option(ROOT / "data/paper/hl_metamodel/state.json", help="Metamodel target weights (read-only)"),
    output_json: Path = typer.Option(ROOT / "data/paper/hl_testnet_risk/latest.json", help="Atomic risk snapshot output"),
    audit_log: Path | None = typer.Option(None, help="Optional executor audit log (defaults to hwm_state.parent / hl_testnet_audit.jsonl)"),
    mm_audit_log: Path | None = typer.Option(None, help="Optional MM audit log (defaults to hwm_state.parent / hl_mm_testnet/mm_audit.jsonl)"),
) -> None:
    _load_dotenv(ROOT / ".env")
    account_addr = os.getenv("HL_TESTNET_ACCOUNT_ADDRESS", "").strip()
    if not account_addr:
        raise typer.BadParameter("HL_TESTNET_ACCOUNT_ADDRESS is required for the Testnet risk probe")

    cur_net_flow: float | None = None
    with httpx.Client(timeout=15.0) as client:
        account = ex.fetch_testnet_account(client, account_addr)
        asset_ctxs = risk.fetch_testnet_asset_ctxs(client)
        book_sides = risk.fetch_book_sides(client, list(account.positions_sz), open_orders=account.open_orders)
        try:
            meta = ex.fetch_testnet_meta(client)
        except Exception:
            meta = {}
        try:
            ledger = pta.fetch_all_user_ledger_updates(client, account_addr)
            cur_net_flow = pta.net_external_usdc_flow_usd(ledger, account_addr)
            if cur_net_flow is None:
                cur_net_flow = 0.0
        except Exception:
            cur_net_flow = None

    eff_mm_audit = mm_audit_log if mm_audit_log is not None else (hwm_state.parent / "hl_mm_testnet" / "mm_audit.jsonl")
    mm_order_oids = ex.load_logged_order_oids(eff_mm_audit)
    mm_recent_min_ts = int(account.observed_at_ms or time.time() * 1000) - 6 * 3_600_000
    raw_mm_coins = os.getenv("HL_MM_COINS", "")
    mm_coin_set = (
        {c.strip() for c in raw_mm_coins.split(",") if c.strip()}
        | ex.load_logged_order_coins(eff_mm_audit, min_ts_ms=mm_recent_min_ts)
        | {str(o.get("coin") or "") for o in account.open_orders if ex.safe_int(o.get("oid")) in mm_order_oids}
    )
    mm_coin_set.discard("")

    eff_metamodel_state = (
        metamodel_state if metamodel_state.is_file() else (hwm_state.parent / "hl_metamodel" / "state.json")
    )
    target_weights: dict[str, float] | None = None
    if eff_metamodel_state.is_file():
        try:
            mm_obj = json.loads(eff_metamodel_state.read_text(encoding="utf-8"))
            if isinstance(mm_obj, dict) and isinstance(mm_obj.get("positions"), dict):
                raw_targets = {
                    str(c): float(p.get("hl_weight", 0.0) or 0.0)
                    for c, p in mm_obj["positions"].items()
                    if isinstance(p, dict) and abs(float(p.get("hl_weight", 0.0) or 0.0)) >= 1e-4
                }
                if meta:
                    raw_targets = {c: w for c, w in raw_targets.items() if c in meta and not meta[c].only_isolated}
                if mm_coin_set:
                    raw_targets = {c: w for c, w in raw_targets.items() if c not in mm_coin_set}
                limits = ex.RiskLimits()
                perp_eq = ex.effective_perp_equity_usd(account)
                mm_gross_w = (
                    sum(
                        abs(float(account.position_notional_usd.get(c, 0.0) or 0.0)) / perp_eq
                        for c in mm_coin_set
                        if abs(float(account.positions_sz.get(c, 0.0) or 0.0)) > 1e-12
                    )
                    if perp_eq > 0
                    else 0.0
                )
                base_max_gross = max(0.0, min(0.90, limits.max_gross_leverage) - mm_gross_w)
                target_weights = ex.neutralize_target_weights(
                    raw_targets,
                    max_gross=base_max_gross,
                    max_single_coin_weight=limits.max_single_coin_weight,
                )
        except Exception:
            target_weights = None

    oracle_eq = risk.oracle_adjusted_equity_usd(account, asset_ctxs)
    stored_hwm = ex.load_high_water_mark(hwm_state)
    raw_prev_net_flow = ex.load_hwm_net_flow(hwm_state)
    prev_last_eq = ex.load_hwm_last_equity(hwm_state)
    if stored_hwm <= 0.0 and not hwm_state.is_file():
        if math.isfinite(oracle_eq) and oracle_eq > 0.0:
            ex.save_high_water_mark(
                hwm_state, oracle_eq, net_external_flow_usd=cur_net_flow, last_equity_usd=oracle_eq
            )
            stored_hwm = oracle_eq
    else:
        prev_net_flow = raw_prev_net_flow
        if prev_net_flow is None and cur_net_flow is not None and stored_hwm > 0:
            prev_net_flow = cur_net_flow
        stored_hwm = ex.flow_adjusted_high_water_mark(
            stored_hwm,
            prev_net_flow,
            cur_net_flow,
            current_equity_usd=oracle_eq,
            pre_flow_equity_usd=prev_last_eq,
        )
        eff_audit_log = audit_log if audit_log is not None else (hwm_state.parent / "hl_testnet_audit.jsonl")
        gross_ntl = ex.account_gross_notional_usd(
            account, reference_px={c: ctx.oracle_px for c, ctx in asset_ctxs.items()}
        )
        persist_ok = ex.hwm_persist_allowed(cur_net_flow, raw_prev_net_flow)
        stored_hwm, did_reanchor = ex.reanchor_hwm_after_flatten(
            hwm_state,
            eff_audit_log,
            stored_hwm=stored_hwm,
            oracle_equity_usd=oracle_eq,
            gross_notional_usd=gross_ntl,
            now_ms=account.observed_at_ms,
            net_external_flow_usd=cur_net_flow,
            persist=persist_ok,
        )
        if (
            not did_reanchor
            and cur_net_flow is not None
            and raw_prev_net_flow is not None
            and persist_ok
            and stored_hwm > 0.0
        ):
            flow_changed = abs(cur_net_flow - raw_prev_net_flow) > 1e-6
            eq_changed = (
                math.isfinite(oracle_eq)
                and oracle_eq > 0.0
                and (prev_last_eq is None or abs(oracle_eq - prev_last_eq) > 1e-6)
            )
            if flow_changed or eq_changed:
                ex.save_high_water_mark(
                    hwm_state,
                    stored_hwm,
                    net_external_flow_usd=cur_net_flow,
                    last_equity_usd=oracle_eq if (math.isfinite(oracle_eq) and oracle_eq > 0.0) else prev_last_eq,
                )

    snapshot = risk.build_risk_snapshot(
        account,
        high_water_mark_usd=stored_hwm,
        asset_ctxs=asset_ctxs,
        book_sides=book_sides,
        target_weights=target_weights,
        foreign_coins=mm_coin_set,
    )
    if hwm_state.is_file():
        try:
            hwm_raw = json.loads(hwm_state.read_text(encoding="utf-8"))
            if isinstance(hwm_raw, dict) and hwm_raw.get("reanchored_at_ms") is not None:
                snapshot["hwm_reanchored_at_ms"] = int(hwm_raw["reanchored_at_ms"])
        except Exception:
            pass
    output_json.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_json.with_name(f".{output_json.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(snapshot, indent=2, default=str), encoding="utf-8")
        tmp.replace(output_json)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)
    typer.echo(json.dumps({k: v for k, v in snapshot.items() if k != "per_coin"}, indent=2, default=str))


if __name__ == "__main__":
    app()
