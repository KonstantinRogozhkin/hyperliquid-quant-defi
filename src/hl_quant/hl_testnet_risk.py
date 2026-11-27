"""Read-only live risk snapshot of the Hyperliquid Testnet execution account.

The ALO executor and the post-trade attribution job each run every 4 hours, so an intra-cycle
drawdown, unmanaged ("orphan") positions on one-sided Testnet books, or a large mark-vs-oracle
dislocation stayed invisible to ``/api/v1/overview``, ``/api/v1/metrics`` and alerting between runs.
This module builds a compact snapshot from public ``info`` endpoints only (no signing, no orders)
that a 15-minute worker job writes to ``hl_testnet_risk/latest.json``.

Thin Testnet books routinely push the exchange mark price far away from the (mainnet-sourced)
oracle, so unrealized PnL is reported on both bases: ``mark`` (what the exchange uses for margin)
and ``oracle`` (closer to the strategy's economic exposure).
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import time
from typing import Any

import httpx

from hl_quant import hl_testnet_executor as ex


@dataclass(frozen=True, slots=True)
class AssetCtx:
    mark_px: float
    oracle_px: float
    funding_apr_pct: float


def _pos_float(raw: Any) -> float | None:
    try:
        x = float(raw)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) and x > 0 else None


def parse_asset_ctxs(payload: Any) -> dict[str, AssetCtx]:
    """Parse a ``metaAndAssetCtxs`` response into ``{coin: AssetCtx}`` (coins without prices are skipped)."""
    if not (isinstance(payload, list) and len(payload) == 2 and isinstance(payload[0], dict)):
        return {}
    meta, ctxs = payload
    out: dict[str, AssetCtx] = {}
    for u, ctx in zip(meta.get("universe") or [], ctxs or [], strict=False):
        if not (isinstance(u, dict) and isinstance(ctx, dict)):
            continue
        coin = str(u.get("name") or "").strip()
        mark = _pos_float(ctx.get("markPx"))
        oracle = _pos_float(ctx.get("oraclePx"))
        if not coin or mark is None or oracle is None:
            continue
        try:
            funding = float(ctx.get("funding", 0.0) or 0.0)
        except (TypeError, ValueError):
            funding = 0.0
        out[coin] = AssetCtx(
            mark_px=mark,
            oracle_px=oracle,
            funding_apr_pct=funding * 24.0 * 365.0 * 100.0 if math.isfinite(funding) else 0.0,
        )
    return out


def fetch_testnet_asset_ctxs(client: httpx.Client) -> dict[str, AssetCtx]:
    resp = ex._post_testnet_info(client, {"type": "metaAndAssetCtxs"})
    return parse_asset_ctxs(resp.json())


def fetch_book_sides(client: httpx.Client, coins: list[str]) -> dict[str, str]:
    """Return ``{coin: "two_sided" | "bid_only" | "ask_only" | "empty" | "unknown"}`` from Testnet ``l2Book``."""
    out: dict[str, str] = {}
    for coin in sorted(set(coins)):
        try:
            resp = ex._post_testnet_info(client, {"type": "l2Book", "coin": coin})
            levels = (resp.json() or {}).get("levels")
        except (httpx.HTTPError, ValueError, AttributeError):
            out[coin] = "unknown"
            continue
        if not (isinstance(levels, list) and len(levels) == 2):
            out[coin] = "unknown"
            continue
        has_bid, has_ask = bool(levels[0]), bool(levels[1])
        out[coin] = (
            "two_sided" if has_bid and has_ask else "bid_only" if has_bid else "ask_only" if has_ask else "empty"
        )
    return out


def oracle_adjusted_equity_usd(account: ex.AccountSnapshot, asset_ctxs: dict[str, AssetCtx]) -> float:
    """Cap account equity at oracle-repriced equity so thin-book mark spikes cannot inflate HWM."""
    equity = float(account.equity_usd)
    if not asset_ctxs or not math.isfinite(equity):
        return equity
    upnl_mark = 0.0
    upnl_oracle = 0.0
    for coin, raw_szi in account.positions_sz.items():
        szi = float(raw_szi or 0.0)
        if abs(szi) <= 1e-12:
            continue
        u_mark = float(account.position_unrealized_pnl_usd.get(coin, 0.0) or 0.0)
        entry = account.position_entry_px.get(coin)
        ctx = asset_ctxs.get(coin)
        oracle_px = ctx.oracle_px if ctx else None
        u_oracle = szi * (oracle_px - entry) if (oracle_px and entry) else u_mark
        upnl_mark += u_mark
        upnl_oracle += u_oracle
    return min(equity, equity - max(0.0, upnl_mark - upnl_oracle))


def build_risk_snapshot(
    account: ex.AccountSnapshot,
    *,
    high_water_mark_usd: float,
    asset_ctxs: dict[str, AssetCtx],
    book_sides: dict[str, str],
    max_drawdown_pct: float = ex.RiskLimits().max_drawdown_pct,
    now_ms: int | None = None,
) -> dict[str, Any]:
    """Pure function: combine an account snapshot, HWM, asset contexts and book sides into a risk record.

    A position is an orphan when its Testnet book is not two-sided: the ALO planner cannot price,
    rebalance or close it, matching ``execute_rebalance_cycle``'s ``coin not in bbo`` rule.
    """
    equity = float(account.equity_usd)
    hwm = max(float(high_water_mark_usd or 0.0), equity if math.isfinite(equity) else 0.0)
    dd = ex.drawdown_pct(equity, hwm)

    per_coin: list[dict[str, Any]] = []
    gross = 0.0
    upnl_mark = 0.0
    upnl_oracle = 0.0
    orphan_notional = 0.0
    orphan_upnl = 0.0
    orphans: dict[str, dict[str, Any]] = {}
    unknown_books = 0
    max_dev_pct = 0.0
    max_dev_coin = ""
    for coin in sorted(account.positions_sz):
        szi = float(account.positions_sz[coin])
        if abs(szi) <= 1e-12:
            continue
        notional = float(account.position_notional_usd.get(coin, 0.0) or 0.0)
        entry = account.position_entry_px.get(coin)
        u_mark = float(account.position_unrealized_pnl_usd.get(coin, 0.0) or 0.0)
        ctx = asset_ctxs.get(coin)
        mark_px = ctx.mark_px if ctx else (abs(notional / szi) if notional else None)
        oracle_px = ctx.oracle_px if ctx else None
        # Re-price at the oracle when both entry and oracle are known; otherwise fall back to mark.
        u_oracle = szi * (oracle_px - entry) if (oracle_px and entry) else u_mark
        dev_pct = ((mark_px / oracle_px) - 1.0) * 100.0 if (mark_px and oracle_px) else 0.0
        book = book_sides.get(coin, "unknown")
        if book == "unknown":
            unknown_books += 1
        is_orphan = book in {"empty", "bid_only", "ask_only"}

        gross += abs(notional)
        upnl_mark += u_mark
        upnl_oracle += u_oracle
        if abs(dev_pct) > abs(max_dev_pct):
            max_dev_pct, max_dev_coin = dev_pct, coin
        if is_orphan:
            orphan_notional += abs(notional)
            orphan_upnl += u_mark
            orphans[coin] = {
                "szi": szi,
                "notional_usd": round(notional, 4),
                "unrealized_pnl_usd": round(u_mark, 4),
                "book": book,
            }
        per_coin.append(
            {
                "coin": coin,
                "szi": szi,
                "notional_usd": round(notional, 4),
                "entry_px": entry,
                "mark_px": mark_px,
                "oracle_px": oracle_px,
                "mark_oracle_dev_pct": round(dev_pct, 4),
                "unrealized_pnl_mark_usd": round(u_mark, 4),
                "unrealized_pnl_oracle_usd": round(u_oracle, 4),
                "book": book,
                "orphan": is_orphan,
            }
        )

    eq_pos = equity if math.isfinite(equity) and equity > 0 else 0.0
    gap = upnl_mark - upnl_oracle
    return {
        "observed_at_ms": int(now_ms if now_ms is not None else time.time() * 1000),
        "equity_usd": round(equity, 6),
        "free_margin_usd": round(float(account.free_margin_usd), 6),
        "high_water_mark_usd": round(hwm, 6),
        "drawdown_pct": round(dd, 4),
        "kill_switch_drawdown_pct": float(max_drawdown_pct),
        "drawdown_headroom_pct": round(max(0.0, float(max_drawdown_pct) - dd), 4),
        "open_positions": len(per_coin),
        "gross_notional_usd": round(gross, 4),
        "gross_leverage": round(gross / eq_pos, 6) if eq_pos else None,
        "unrealized_pnl_mark_usd": round(upnl_mark, 4),
        "unrealized_pnl_oracle_usd": round(upnl_oracle, 4),
        "mark_oracle_pnl_gap_usd": round(gap, 4),
        "mark_oracle_pnl_gap_pct": round(abs(gap) / eq_pos * 100.0, 4) if eq_pos else None,
        "max_mark_oracle_dev_pct": round(max_dev_pct, 4),
        "max_mark_oracle_dev_coin": max_dev_coin,
        "orphan_positions": orphans,
        "orphan_notional_usd": round(orphan_notional, 4),
        "orphan_unrealized_pnl_usd": round(orphan_upnl, 4),
        "unknown_books": unknown_books,
        "open_orders": len(account.open_orders),
        "per_coin": per_coin,
    }
