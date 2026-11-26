"""Hyperliquid Testnet ALO (post-only) execution engine with reconciliation and kill-switch.

Safety contract:
* Hard-pinned to ``https://api.hyperliquid-testnet.xyz`` (never targets mainnet).
* Defaults to dry-run; live Testnet order submission requires explicit ``execute=True``
  plus ``HL_TESTNET_PRIVATE_KEY`` and ``HL_TESTNET_ACCOUNT_ADDRESS``.
* Enforces Hyperliquid's exact perp price/size rounding (5 significant figures,
  ``max_decimals = 6 - szDecimals``), child-order slicing, position reconciliation,
  and pre-trade kill-switch gates.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import time
from typing import Any, Protocol

import httpx

TESTNET_API_URL = "https://api.hyperliquid-testnet.xyz"
TESTNET_INFO_URL = f"{TESTNET_API_URL}/info"


@dataclass(frozen=True, slots=True)
class AssetSpec:
    coin: str
    sz_decimals: int
    max_leverage: int
    only_isolated: bool = False
    margin_mode: str = "normal"


@dataclass(frozen=True, slots=True)
class RiskLimits:
    max_gross_leverage: float = 1.5
    max_single_coin_weight: float = 0.25
    max_drawdown_pct: float = 5.0
    min_free_margin_pct: float = 20.0
    max_stale_feed_sec: float = 30.0
    max_child_usd: float = 1_000.0
    min_order_usd: float = 15.0  # HL minimum notional is ~$10
    allow_only_isolated: bool = False
    default_isolated_leverage: int = 2
    default_cross_leverage: int = 3
    avoid_pre_settlement_neg_funding: bool = True
    neg_funding_apr_threshold: float = -25.0
    pre_settlement_window_min: int = 5


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity_usd: float
    free_margin_usd: float
    positions_sz: dict[str, float]  # signed base-asset size (+long, -short)
    open_orders: list[dict[str, Any]]
    observed_at_ms: int
    position_leverage: dict[str, tuple[str, int]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PlannedOrder:
    coin: str
    side: str  # "buy" or "sell"
    limit_px: float
    sz: float
    notional_usd: float
    tif: str = "Alo"  # Post-only Add-Liquidity-Only
    reduce_only: bool = False
    child_index: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ExchangeAdapter(Protocol):
    def place_alo_order(self, order: PlannedOrder) -> dict[str, Any]: ...
    def place_ioc_order(self, order: PlannedOrder) -> dict[str, Any]: ...
    def cancel_all_open_orders(self, open_orders: list[dict[str, Any]]) -> list[dict[str, Any]]: ...
    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> dict[str, Any]: ...


# --------------------------------------------------------------------------- Hyperliquid precision rules


def round_hl_price(px: float, sz_decimals: int, *, side: str = "buy") -> float:
    """Round price to Hyperliquid perp precision: <=5 significant figures and <= 6 - szDecimals decimals.

    For post-only safety, buys round down (never cross ask) and sells round up (never cross bid).
    """
    if not math.isfinite(px) or px <= 0:
        raise ValueError(f"invalid price {px}")
    max_dec = max(0, 6 - int(sz_decimals))
    if px >= 100_000:
        # Integer prices are always allowed regardless of sig figs
        return float(math.floor(px) if side == "buy" else math.ceil(px))
    magnitude = math.floor(math.log10(px))
    sig_dec = max(0, 4 - magnitude)  # 5 significant digits total
    decimals = min(max_dec, sig_dec)
    factor = 10.0**decimals
    if side == "buy":
        return math.floor(px * factor + 1e-12) / factor
    return math.ceil(px * factor - 1e-12) / factor


def round_hl_size(sz: float, sz_decimals: int) -> float:
    """Round base-asset size down to ``sz_decimals`` decimal places."""
    if not math.isfinite(sz) or sz <= 0:
        return 0.0
    factor = 10.0 ** int(sz_decimals)
    return math.floor(sz * factor + 1e-12) / factor


# --------------------------------------------------------------------------- Testnet public readers


def fetch_testnet_meta(client: httpx.Client) -> dict[str, AssetSpec]:
    resp = client.post(TESTNET_INFO_URL, json={"type": "meta"})
    resp.raise_for_status()
    universe = (resp.json() or {}).get("universe") or []
    out: dict[str, AssetSpec] = {}
    for item in universe:
        if item.get("isDelisted"):
            continue
        name = str(item.get("name") or "").strip()
        if name:
            margin_mode = str(item.get("marginMode") or ("onlyIsolated" if item.get("onlyIsolated") else "normal"))
            only_isolated = bool(item.get("onlyIsolated", False) or margin_mode in {"strictIsolated", "noCross", "onlyIsolated"})
            out[name] = AssetSpec(
                coin=name,
                sz_decimals=int(item.get("szDecimals", 2)),
                max_leverage=int(item.get("maxLeverage", 10)),
                only_isolated=only_isolated,
                margin_mode=margin_mode,
            )
    return out


def fetch_testnet_bbo(client: httpx.Client, coins: list[str]) -> dict[str, tuple[float, float, int]]:
    """Return ``{coin: (best_bid, best_ask, observed_ms)}`` from Hyperliquid Testnet ``l2Book``."""
    out: dict[str, tuple[float, float, int]] = {}
    for coin in sorted(set(coins)):
        resp = client.post(TESTNET_INFO_URL, json={"type": "l2Book", "coin": coin})
        resp.raise_for_status()
        book = resp.json()
        levels = book.get("levels") if isinstance(book, dict) else None
        if not (isinstance(levels, list) and len(levels) == 2 and levels[0] and levels[1]):
            continue
        bid = float(levels[0][0]["px"])
        ask = float(levels[1][0]["px"])
        # Exchange-side book time, so the stale-feed gate measures real quote age.
        book_ms = book.get("time")
        observed_ms = int(book_ms) if isinstance(book_ms, (int, float)) and book_ms > 0 else int(time.time() * 1000)
        if bid > 0 and ask >= bid:
            out[coin] = (bid, ask, observed_ms)
    return out


def fetch_testnet_account(client: httpx.Client, account_address: str) -> AccountSnapshot:
    """Read live Hyperliquid Testnet clearinghouse state & open orders (read-only)."""
    st_resp = client.post(TESTNET_INFO_URL, json={"type": "clearinghouseState", "user": account_address})
    st_resp.raise_for_status()
    st = st_resp.json() or {}
    margin = st.get("marginSummary") or {}
    perps_eq = float(margin.get("accountValue", 0.0))
    used_margin = float(margin.get("totalMarginUsed", 0.0))
    withdrawable = float(st.get("withdrawable", 0.0))
    equity = perps_eq
    free_margin = max(0.0, perps_eq - used_margin)

    positions_sz: dict[str, float] = {}
    position_leverage: dict[str, tuple[str, int]] = {}
    for entry in st.get("assetPositions") or []:
        pos = entry.get("position") or {}
        coin = str(pos.get("coin") or "").strip()
        szi = float(pos.get("szi", 0.0))
        if coin and abs(szi) > 1e-12:
            positions_sz[coin] = szi
            lev = pos.get("leverage") if isinstance(pos.get("leverage"), dict) else {}
            if lev:
                position_leverage[coin] = (str(lev.get("type", "cross")), int(float(lev.get("value", 1))))

    # Hyperliquid Unified Account keeps settled + live equity inside ``spot.USDC.total``
    # (verified 1:1 against ``portfolio.accountValueHistory``) with locked perp margin
    # and open-order holds inside ``spot.USDC.hold``; avoid double-counting perps_eq.
    abstraction = ""
    try:
        ua_resp = client.post(TESTNET_INFO_URL, json={"type": "userAbstraction", "user": account_address})
        if ua_resp.status_code == 200 and isinstance(ua_resp.json(), str):
            abstraction = str(ua_resp.json())
    except Exception:
        abstraction = ""

    spot_resp = client.post(TESTNET_INFO_URL, json={"type": "spotClearinghouseState", "user": account_address})
    if spot_resp.status_code == 200:
        for bal in (spot_resp.json() or {}).get("balances") or []:
            if str(bal.get("coin")) == "USDC":
                spot_total = float(bal.get("total", 0.0))
                spot_hold = float(bal.get("hold", 0.0))
                if spot_total > 0:
                    is_unified = abstraction in {"unifiedAccount", "portfolioMargin"} or (
                        withdrawable <= 0.0 and (spot_hold > 0.0 or perps_eq <= 0.0)
                    )
                    if is_unified:
                        equity = spot_total
                        free_margin = max(0.0, spot_total - spot_hold)
                    else:
                        # Standard Account: separate Perps and Spot wallets
                        equity = perps_eq + spot_total
                        free_margin = max(0.0, perps_eq - used_margin) + max(0.0, spot_total - spot_hold)
                break

    oo_resp = client.post(TESTNET_INFO_URL, json={"type": "openOrders", "user": account_address})
    oo_resp.raise_for_status()
    open_orders = oo_resp.json() or []
    return AccountSnapshot(
        equity_usd=equity,
        free_margin_usd=free_margin,
        positions_sz=positions_sz,
        open_orders=open_orders if isinstance(open_orders, list) else [],
        observed_at_ms=int(time.time() * 1000),
        position_leverage=position_leverage,
    )


# --------------------------------------------------------------------------- Kill-switch & Order planner


def check_kill_switch(
    account: AccountSnapshot,
    target_weights: dict[str, float],
    bbo: dict[str, tuple[float, float, int]],
    limits: RiskLimits,
    *,
    high_water_mark_usd: float,
    now_ms: int | None = None,
) -> list[str]:
    """Return list of risk violations; non-empty list halts new entries and cancels open orders."""
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    reasons: list[str] = []

    if account.equity_usd <= 0:
        reasons.append("zero_or_negative_equity")
        return reasons

    if high_water_mark_usd > 0:
        dd_pct = (1.0 - account.equity_usd / high_water_mark_usd) * 100.0
        if dd_pct >= limits.max_drawdown_pct:
            reasons.append(f"drawdown_limit:{dd_pct:.2f}%>={limits.max_drawdown_pct:.2f}%")

    free_pct = (account.free_margin_usd / account.equity_usd) * 100.0
    if free_pct < limits.min_free_margin_pct:
        reasons.append(f"low_free_margin:{free_pct:.1f}%<{limits.min_free_margin_pct:.1f}%")

    gross = sum(abs(w) for w in target_weights.values())
    if gross > limits.max_gross_leverage + 1e-9:
        reasons.append(f"gross_leverage:{gross:.2f}x>{limits.max_gross_leverage:.2f}x")

    for coin, w in target_weights.items():
        if abs(w) > limits.max_single_coin_weight + 1e-9:
            reasons.append(f"coin_cap:{coin}={abs(w):.2f}>{limits.max_single_coin_weight:.2f}")

    for coin, w in target_weights.items():
        if coin not in bbo:
            if abs(w) > 1e-9:
                reasons.append(f"missing_quote:{coin}")
            continue
        age_sec = (now - bbo[coin][2]) / 1000.0
        if age_sec > limits.max_stale_feed_sec:
            reasons.append(f"stale_feed:{coin}={age_sec:.1f}s>{limits.max_stale_feed_sec:.1f}s")

    return reasons


def fetch_testnet_funding_apr(client: httpx.Client) -> dict[str, float]:
    """Read current annualized funding APR (%) per coin from Hyperliquid Testnet ``metaAndAssetCtxs``."""
    try:
        resp = client.post(TESTNET_INFO_URL, json={"type": "metaAndAssetCtxs"})
        if resp.status_code != 200:
            return {}
        payload = resp.json()
        if not (isinstance(payload, list) and len(payload) == 2):
            return {}
        meta, ctxs = payload
        out: dict[str, float] = {}
        for u, ctx in zip(meta.get("universe") or [], ctxs or [], strict=False):
            coin = str(u.get("name") or "").strip()
            if coin:
                out[coin] = float(ctx.get("funding", 0.0) or 0.0) * 24.0 * 365.0 * 100.0
        return out
    except Exception:
        return {}


def is_pre_settlement_neg_funding_trap(ts_ms: int, funding_apr_pct: float, limits: RiskLimits) -> bool:
    """Return True during T-5m..T0 UTC when opening a new long on extreme negative funding (``APR <= -25%``).

    17-day OOS study (17,874 settlements) showed ``naive_harvest_5m`` loses -5.85 bps net due to
    simultaneous harvester unwind immediately after ``:00``.
    """
    if not limits.avoid_pre_settlement_neg_funding or not math.isfinite(funding_apr_pct):
        return False
    sec_in_hour = (int(ts_ms) // 1000) % 3600
    cutoff_sec = max(0, (60 - int(limits.pre_settlement_window_min)) * 60)
    return sec_in_hour >= cutoff_sec and funding_apr_pct <= limits.neg_funding_apr_threshold


def plan_rebalance_orders(
    account: AccountSnapshot,
    target_weights: dict[str, float],
    meta: dict[str, AssetSpec],
    bbo: dict[str, tuple[float, float, int]],
    limits: RiskLimits,
    *,
    funding_apr_by_coin: dict[str, float] | None = None,
    now_ms: int | None = None,
) -> list[PlannedOrder]:
    """Reconcile live Testnet positions vs target weights and emit sliced post-only ``ALO`` child orders."""
    all_coins = sorted(set(account.positions_sz.keys()) | set(target_weights.keys()))
    planned: list[PlannedOrder] = []
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    fund_map = funding_apr_by_coin or {}

    for coin in all_coins:
        if coin not in meta or coin not in bbo:
            continue
        spec = meta[coin]
        bid, ask, _ = bbo[coin]
        mid = 0.5 * (bid + ask)
        if mid <= 0:
            continue

        cur_sz = float(account.positions_sz.get(coin, 0.0))
        target_w = float(target_weights.get(coin, 0.0))
        if spec.only_isolated and not limits.allow_only_isolated:
            # Skip opening isolated-only markets by default; cleanly unwind if already held.
            target_w = 0.0
        target_sz = (account.equity_usd * target_w) / mid
        delta_sz = target_sz - cur_sz
        delta_usd = abs(delta_sz) * mid
        min_notional = 10.5 if (target_w == 0.0 and abs(cur_sz) > 1e-12) else limits.min_order_usd
        if delta_usd < min_notional:
            continue

        side = "buy" if delta_sz > 0 else "sell"
        reduce_only = (cur_sz > 0 and side == "sell" and target_sz >= 0) or (cur_sz < 0 and side == "buy" and target_sz <= 0)
        if side == "buy" and not reduce_only and is_pre_settlement_neg_funding_trap(ts_ms, fund_map.get(coin, 0.0), limits):
            # Defer naive pre-:00 long entry on extreme negative funding until after :00 unwind
            continue
        raw_px = bid if side == "buy" else ask
        limit_px = round_hl_price(raw_px, spec.sz_decimals, side=side)

        # Slice into child ALO orders <= max_child_usd
        n_slices = max(1, int(math.ceil(delta_usd / limits.max_child_usd)))
        slice_sz = round_hl_size(abs(delta_sz) / n_slices, spec.sz_decimals)
        if slice_sz <= 0 or slice_sz * limit_px < min_notional:
            # Try single un-sliced order if slicing fell below HL min notional
            slice_sz = round_hl_size(abs(delta_sz), spec.sz_decimals)
            n_slices = 1
        if slice_sz <= 0 or slice_sz * limit_px < min_notional:
            continue

        for idx in range(n_slices):
            planned.append(
                PlannedOrder(
                    coin=coin,
                    side=side,
                    limit_px=limit_px,
                    sz=slice_sz,
                    notional_usd=round(slice_sz * limit_px, 2),
                    tif="Alo",
                    reduce_only=reduce_only,
                    child_index=idx,
                )
            )
    return planned


# --------------------------------------------------------------------------- Adapters & Orchestrator


class DryRunAdapter:
    """Zero-network dry-run adapter that records every planned ALO order, cancel, and leverage update."""

    def __init__(self) -> None:
        self.placed: list[dict[str, Any]] = []
        self.modified: list[dict[str, Any]] = []
        self.cancelled: list[dict[str, Any]] = []
        self.leverage_updates: list[dict[str, Any]] = []
        self.scheduled_cancels: list[int | None] = []
        self._next_oid: int = 100_000

    def place_alo_order(self, order: PlannedOrder) -> dict[str, Any]:
        self._next_oid += 1
        rec = {
            "mode": "dry_run",
            "status": "simulated_alo",
            "oid": self._next_oid,
            "response": {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": self._next_oid}}]}}},
            **order.to_dict(),
        }
        self.placed.append(rec)
        return rec

    def place_bulk_alo_orders(self, orders: list[PlannedOrder]) -> list[dict[str, Any]]:
        return [self.place_alo_order(o) for o in orders]

    def modify_bulk_alo_orders(self, modifications: list[tuple[int, PlannedOrder]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for oid, order in modifications:
            self._next_oid += 1
            rec = {
                "mode": "dry_run",
                "status": "simulated_modify",
                "prev_oid": int(oid),
                "oid": self._next_oid,
                "response": {"status": "ok", "response": {"data": {"statuses": [{"resting": {"oid": self._next_oid}}]}}},
                **order.to_dict(),
            }
            self.modified.append(rec)
            out.append(rec)
        return out

    def place_ioc_order(self, order: PlannedOrder) -> dict[str, Any]:
        rec = {"mode": "dry_run", "status": "simulated_ioc", **{**order.to_dict(), "tif": "Ioc"}}
        self.placed.append(rec)
        return rec

    def cancel_all_open_orders(self, open_orders: list[dict[str, Any]], *, fast: bool = False) -> list[dict[str, Any]]:
        out = [
            {"mode": "dry_run", "status": "simulated_cancel", "oid": o.get("oid"), "coin": o.get("coin"), "fast": bool(fast)}
            for o in open_orders
        ]
        self.cancelled.extend(out)
        return out

    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> dict[str, Any]:
        rec = {"mode": "dry_run", "status": "ok", "coin": coin, "leverage": int(leverage), "is_cross": is_cross}
        self.leverage_updates.append(rec)
        return rec

    def schedule_cancel(self, trigger_time_ms: int | None) -> dict[str, Any]:
        self.scheduled_cancels.append(trigger_time_ms)
        return {"mode": "dry_run", "status": "ok", "time": trigger_time_ms}


class HyperliquidTestnetSDKAdapter:
    """Live Hyperliquid Testnet adapter via ``hyperliquid-python-sdk`` (hard-pinned to Testnet)."""

    def __init__(self, private_key: str, account_address: str) -> None:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as exc:
            raise RuntimeError("Install hyperliquid-python-sdk to submit live Testnet orders") from exc
        wallet = Account.from_key(private_key)
        self._exchange = Exchange(wallet, base_url=TESTNET_API_URL, account_address=account_address)

    def place_alo_order(self, order: PlannedOrder) -> dict[str, Any]:
        resp = self._exchange.order(
            name=order.coin,
            is_buy=(order.side == "buy"),
            sz=order.sz,
            limit_px=order.limit_px,
            order_type={"limit": {"tif": "Alo"}},
            reduce_only=order.reduce_only,
        )
        return {"mode": "testnet_live", "order": order.to_dict(), "response": resp}

    def place_ioc_order(self, order: PlannedOrder) -> dict[str, Any]:
        resp = self._exchange.order(
            name=order.coin,
            is_buy=(order.side == "buy"),
            sz=order.sz,
            limit_px=order.limit_px,
            order_type={"limit": {"tif": "Ioc"}},
            reduce_only=order.reduce_only,
        )
        return {"mode": "testnet_live", "order": {**order.to_dict(), "tif": "Ioc"}, "response": resp}

    def cancel_all_open_orders(self, open_orders: list[dict[str, Any]], *, fast: bool = False) -> list[dict[str, Any]]:
        reqs = [
            {"coin": str(o.get("coin") or ""), "oid": int(o["oid"])}
            for o in open_orders
            if o.get("coin") and o.get("oid") is not None
        ]
        if not reqs:
            return []
        if fast and hasattr(self._exchange, "_post_action"):
            try:
                from hyperliquid.utils.signing import get_timestamp_ms, sign_l1_action

                ts_ms = get_timestamp_ms()
                cancel_action = {
                    "type": "cancel",
                    "cancels": [
                        {"a": self._exchange.info.name_to_asset(r["coin"]), "o": r["oid"], "f": True}
                        for r in reqs
                    ],
                }
                sig = sign_l1_action(
                    self._exchange.wallet,
                    cancel_action,
                    self._exchange.vault_address,
                    ts_ms,
                    self._exchange.expires_after,
                    False,  # hard-pinned to Testnet
                )
                resp = self._exchange._post_action(cancel_action, sig, ts_ms)
                if isinstance(resp, dict) and resp.get("status") == "ok":
                    return [{"mode": "testnet_live", "fast": True, "bulk_count": len(reqs), "requests": reqs, "response": resp}]
            except Exception:
                pass
        if len(reqs) == 1:
            resp = self._exchange.cancel(reqs[0]["coin"], reqs[0]["oid"])
            return [{"mode": "testnet_live", "fast": False, "coin": reqs[0]["coin"], "oid": reqs[0]["oid"], "response": resp}]
        resp = self._exchange.bulk_cancel(reqs)
        return [{"mode": "testnet_live", "fast": False, "bulk_count": len(reqs), "requests": reqs, "response": resp}]

    def place_bulk_alo_orders(self, orders: list[PlannedOrder]) -> list[dict[str, Any]]:
        if not orders:
            return []
        # HyperCore mempool validators prioritize pure ALO batches; mixing GTC/IOC strips priority.
        if any(str(o.tif) != "Alo" for o in orders):
            raise ValueError("place_bulk_alo_orders requires a pure ALO batch (mixing GTC/IOC forfeits HyperCore mempool priority)")
        if len(orders) == 1:
            return [self.place_alo_order(orders[0])]
        reqs = [
            {
                "coin": o.coin,
                "is_buy": o.side == "buy",
                "sz": o.sz,
                "limit_px": o.limit_px,
                "order_type": {"limit": {"tif": "Alo"}},
                "reduce_only": o.reduce_only,
            }
            for o in orders
        ]
        resp = self._exchange.bulk_orders(reqs)
        statuses = (((resp or {}).get("response") or {}).get("data") or {}).get("statuses") or []
        out: list[dict[str, Any]] = []
        for idx, o in enumerate(orders):
            st = [statuses[idx]] if idx < len(statuses) else []
            out.append(
                {
                    "mode": "testnet_live",
                    "order": o.to_dict(),
                    "response": {"status": (resp or {}).get("status", "ok"), "response": {"data": {"statuses": st}}},
                }
            )
        return out

    def modify_bulk_alo_orders(self, modifications: list[tuple[int, PlannedOrder]]) -> list[dict[str, Any]]:
        if not modifications:
            return []
        reqs = [
            {
                "oid": int(oid),
                "order": {
                    "coin": o.coin,
                    "is_buy": o.side == "buy",
                    "sz": o.sz,
                    "limit_px": o.limit_px,
                    "order_type": {"limit": {"tif": "Alo"}},
                    "reduce_only": o.reduce_only,
                },
            }
            for oid, o in modifications
        ]
        resp = self._exchange.bulk_modify_orders_new(reqs)
        statuses = (((resp or {}).get("response") or {}).get("data") or {}).get("statuses") or []
        out: list[dict[str, Any]] = []
        for idx, (oid, o) in enumerate(modifications):
            st = [statuses[idx]] if idx < len(statuses) else []
            out.append(
                {
                    "mode": "testnet_live",
                    "prev_oid": int(oid),
                    "order": o.to_dict(),
                    "response": {"status": (resp or {}).get("status", "ok"), "response": {"data": {"statuses": st}}},
                }
            )
        return out

    def set_leverage(self, coin: str, leverage: int, *, is_cross: bool) -> dict[str, Any]:
        resp = self._exchange.update_leverage(leverage=int(leverage), name=coin, is_cross=is_cross)
        return {"mode": "testnet_live", "coin": coin, "leverage": int(leverage), "is_cross": is_cross, "response": resp}

    def schedule_cancel(self, trigger_time_ms: int | None) -> dict[str, Any]:
        """Set or disarm Hyperliquid native exchange-side Dead Man's Switch."""
        resp = self._exchange.schedule_cancel(trigger_time_ms)
        return {"mode": "testnet_live", "time": trigger_time_ms, "response": resp}


def load_high_water_mark(path: Path) -> float:
    """Read the persisted equity high-water mark (0.0 when none is stored yet)."""
    if not path.is_file():
        return 0.0
    try:
        value = float(json.loads(path.read_text()).get("high_water_mark_usd", 0.0))
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0
    return value if math.isfinite(value) and value > 0 else 0.0


def save_high_water_mark(path: Path, value: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps({"high_water_mark_usd": float(value)}))
    tmp.replace(path)


def execute_rebalance_cycle(
    account: AccountSnapshot,
    target_weights: dict[str, float],
    meta: dict[str, AssetSpec],
    bbo: dict[str, tuple[float, float, int]],
    adapter: ExchangeAdapter,
    *,
    limits: RiskLimits = RiskLimits(),
    high_water_mark_usd: float | None = None,
    audit_log_path: Path | None = None,
    now_ms: int | None = None,
    funding_apr_by_coin: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run one complete Testnet rebalance cycle: kill-switch -> cancel stale -> place sliced ALO orders.

    ``high_water_mark_usd`` must be the peak equity persisted across cycles; without it the
    drawdown gate only compares equity with itself and can never fire.
    """
    hwm = max(float(high_water_mark_usd or 0.0), 0.0) or account.equity_usd
    violations = check_kill_switch(account, target_weights, bbo, limits, high_water_mark_usd=hwm, now_ms=now_ms)

    cancelled = adapter.cancel_all_open_orders(account.open_orders) if account.open_orders else []
    if violations:
        summary = {
            "status": "kill_switch_triggered",
            "violations": violations,
            "cancelled_count": len(cancelled),
            "placed_count": 0,
            "high_water_mark_usd": hwm,
            "orders": [],
        }
    else:
        planned = plan_rebalance_orders(
            account,
            target_weights,
            meta,
            bbo,
            limits,
            funding_apr_by_coin=funding_apr_by_coin,
            now_ms=now_ms,
        )
        leverage_updates: list[dict[str, Any]] = []
        blocked_isolated_coins: set[str] = set()
        seen_lev_coins: set[str] = set()
        for o in planned:
            if o.reduce_only or o.coin in seen_lev_coins or o.coin not in meta:
                continue
            seen_lev_coins.add(o.coin)
            spec = meta[o.coin]
            is_cross = not spec.only_isolated
            target_lev = max(1, min(spec.max_leverage, limits.default_cross_leverage if is_cross else limits.default_isolated_leverage))
            cur_mode, cur_lev = account.position_leverage.get(o.coin, ("", 0))
            expected_mode = "cross" if is_cross else "isolated"
            if cur_mode == expected_mode and 0 < cur_lev <= target_lev:
                continue
            try:
                upd = adapter.set_leverage(o.coin, target_lev, is_cross=is_cross)
                leverage_updates.append(upd)
                resp = upd.get("response") if isinstance(upd, dict) else None
                ok = (upd.get("status") == "ok") or (isinstance(resp, dict) and resp.get("status") == "ok")
                if spec.only_isolated and not ok:
                    blocked_isolated_coins.add(o.coin)
            except Exception as exc:
                leverage_updates.append({"coin": o.coin, "leverage": target_lev, "is_cross": is_cross, "error": str(exc)})
                if spec.only_isolated:
                    blocked_isolated_coins.add(o.coin)

        executable = [o for o in planned if o.reduce_only or o.coin not in blocked_isolated_coins]
        placed = [adapter.place_alo_order(o) for o in executable]
        summary = {
            "status": "ok",
            "violations": [],
            "cancelled_count": len(cancelled),
            "placed_count": len(placed),
            "total_notional_usd": round(sum(o.notional_usd for o in executable), 2),
            "high_water_mark_usd": hwm,
            "leverage_updates": leverage_updates,
            "blocked_isolated_coins": sorted(blocked_isolated_coins),
            "orders": placed,
        }

    if audit_log_path is not None:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts_ms": now_ms or int(time.time() * 1000), **summary}
        with audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    return summary


def neutralize_target_weights(weights: dict[str, float], *, max_gross: float = 0.90) -> dict[str, float]:
    """Re-center signed weights to dollar-neutral (``sum(w) == 0``) and cap gross exposure."""
    clean = {c: float(w) for c, w in weights.items() if math.isfinite(w) and abs(w) >= 1e-4}
    if len(clean) < 2:
        return clean
    mean_w = sum(clean.values()) / len(clean)
    centered = {c: w - mean_w for c, w in clean.items()}
    gross = sum(abs(w) for w in centered.values())
    if gross > max_gross and gross > 1e-9:
        scale = max_gross / gross
        centered = {c: w * scale for c, w in centered.items()}
    return {c: round(w, 6) for c, w in centered.items() if abs(w) >= 1e-4}


def update_high_water_mark(hwm_path: Path, equity_usd: float) -> float:
    """Persist and return peak Testnet equity for cross-run drawdown kill-switch."""
    prev = equity_usd
    if hwm_path.is_file():
        try:
            prev = float(json.loads(hwm_path.read_text()).get("high_water_mark_usd", equity_usd))
        except (ValueError, OSError):
            prev = equity_usd
    hwm = max(prev, equity_usd)
    hwm_path.parent.mkdir(parents=True, exist_ok=True)
    hwm_path.write_text(json.dumps({"high_water_mark_usd": hwm, "updated_ms": int(time.time() * 1000)}, indent=2))
    return hwm


def format_testnet_telegram_rich_html(
    account: AccountSnapshot,
    result: dict[str, Any],
    bbo: dict[str, tuple[float, float, int]],
    *,
    hwm_usd: float,
) -> str:
    """Format a plain-Russian Telegram Rich HTML table for a live Hyperliquid Testnet cycle."""
    dt = datetime.now(tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    dd_pct = (1.0 - account.equity_usd / hwm_usd) * 100.0 if hwm_usd > 0 else 0.0
    status = str(result.get("status", "ok"))
    status_icon = "🟢" if status == "ok" else "🛑"

    parts = [
        f"<h1>🧪 Hyperliquid Testnet · {html.escape(date_str)}</h1>",
        (
            f"<p>{status_icon} <b>Баланс Testnet:</b> ${account.equity_usd:,.2f} "
            f"(пик ${hwm_usd:,.2f}, просадка {dd_pct:.2f}%)<br>"
            f"<b>Цикл ({html.escape(time_str)}):</b> отменено {int(result.get('cancelled_count', 0))} · "
            f"выставлено ALO: <b>{int(result.get('placed_count', 0))}</b> "
            f"(на ${float(result.get('total_notional_usd', 0.0)):,.0f})</p>"
        ),
    ]
    if result.get("violations"):
        parts.append(f"<p><b>⚠️ Kill-switch:</b> {html.escape(', '.join(result['violations']))}</p>")

    orders = result.get("orders") or []
    if orders:
        rows = [
            "<tr>"
            '<th align="left">Монета</th>'
            '<th align="left">Ордер ALO</th>'
            '<th align="right">Сумма</th>'
            '<th align="right">Цена</th>'
            '<th align="left">Статус</th>'
            "</tr>"
        ]
        for rec in orders[:12]:
            o = rec.get("order") or rec
            side_txt = "🟢 Купить" if o.get("side") == "buy" else "🔴 Продать"
            resp = rec.get("response") or {}
            st_txt = str(resp.get("status", rec.get("status", "ok")))
            if isinstance(resp, dict):
                statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses") or [])
                if statuses and isinstance(statuses[0], dict):
                    st_txt = next(iter(statuses[0].keys()))
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(str(o.get("coin", "")))}</b></td>'
                f'<td align="left">{html.escape(side_txt)}</td>'
                f'<td align="right">${float(o.get("notional_usd", 0.0)):,.0f}</td>'
                f'<td align="right">${float(o.get("limit_px", 0.0)):,.4g}</td>'
                f'<td align="left">{html.escape(st_txt)}</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    parts.append("<footer>Hyperliquid Testnet (mock USDC) · post-only ALO · реальные деньги не используются</footer>")
    return "".join(parts)
