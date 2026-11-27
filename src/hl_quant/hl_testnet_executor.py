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
import os
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
    max_unquoted_coin_fraction: float = 0.35
    # Kill-switch de-risking: on account-risk violations (drawdown, margin, gross, coin cap) keep
    # submitting reduce-only orders instead of freezing the book; on a drawdown breach flatten all.
    derisk_on_kill_switch: bool = True
    flatten_on_drawdown: bool = True
    # Close positions on coins without a two-sided Testnet book with reduce-only ALO orders priced
    # off the oracle (or the surviving book side) instead of leaving them unmanaged forever.
    unwind_unquoted_orphans: bool = True


# Kill-switch reasons caused by untrustworthy inputs: no BBO-based rebalancing on them (fail-closed).
DATA_INTEGRITY_VIOLATIONS = ("non_finite", "zero_or_negative_equity", "stale_feed", "missing_quote")
# Subset that makes the account snapshot itself untrustworthy: nothing at all is submitted. A pure
# ``missing_quote`` feed gap still allows reduce-only oracle-priced unwinds of orphan positions.
HARD_DATA_VIOLATIONS = ("non_finite", "zero_or_negative_equity", "stale_feed")
# Kill-switch reasons describing account risk: block new exposure but keep reducing it.
ACCOUNT_RISK_VIOLATIONS = ("drawdown_limit", "low_free_margin", "gross_leverage", "coin_cap")


def safe_int(raw: Any) -> int | None:
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def classify_violations(violations: list[str]) -> tuple[bool, bool, bool]:
    """Return ``(data_integrity, account_risk, drawdown)`` flags for kill-switch reasons."""
    data_integrity = any(v.startswith(DATA_INTEGRITY_VIOLATIONS) for v in violations)
    account_risk = any(v.startswith(ACCOUNT_RISK_VIOLATIONS) for v in violations)
    drawdown = any(v.startswith("drawdown_limit") for v in violations)
    return data_integrity, account_risk, drawdown


@dataclass(frozen=True, slots=True)
class AccountSnapshot:
    equity_usd: float
    free_margin_usd: float
    positions_sz: dict[str, float]  # signed base-asset size (+long, -short)
    open_orders: list[dict[str, Any]]
    observed_at_ms: int
    position_leverage: dict[str, tuple[str, int]] = field(default_factory=dict)
    # Signed USD mark value per held coin (+long, -short) from clearinghouseState, so unquoted
    # orphan positions are still counted in gross leverage and dollar-neutrality checks.
    position_notional_usd: dict[str, float] = field(default_factory=dict)
    # Per-coin average entry price and exchange-reported (mark-based) unrealized PnL, used by the
    # read-only risk probe to re-price positions at the oracle on thin Testnet books.
    position_entry_px: dict[str, float] = field(default_factory=dict)
    position_unrealized_pnl_usd: dict[str, float] = field(default_factory=dict)


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


BookTop = tuple[float | None, float | None, int]


def _level_px(side: Any) -> float | None:
    if not (isinstance(side, list) and side and isinstance(side[0], dict)):
        return None
    try:
        px = float(side[0].get("px"))
    except (TypeError, ValueError):
        return None
    return px if math.isfinite(px) and px > 0 else None


def fetch_testnet_book_tops(client: httpx.Client, coins: list[str]) -> dict[str, BookTop]:
    """Return ``{coin: (best_bid | None, best_ask | None, observed_ms)}`` including one-sided books."""
    out: dict[str, BookTop] = {}
    for coin in sorted(set(coins)):
        resp = client.post(TESTNET_INFO_URL, json={"type": "l2Book", "coin": coin})
        resp.raise_for_status()
        book = resp.json()
        levels = book.get("levels") if isinstance(book, dict) else None
        if not (isinstance(levels, list) and len(levels) == 2):
            out[coin] = (None, None, int(time.time() * 1000))
            continue
        # Exchange-side book time, so the stale-feed gate measures real quote age.
        book_ms = book.get("time")
        observed_ms = int(book_ms) if isinstance(book_ms, (int, float)) and book_ms > 0 else int(time.time() * 1000)
        out[coin] = (_level_px(levels[0]), _level_px(levels[1]), observed_ms)
    return out


def two_sided_bbo(tops: dict[str, BookTop]) -> dict[str, tuple[float, float, int]]:
    """Keep only coins with a valid two-sided book: ``{coin: (bid, ask, observed_ms)}``."""
    out: dict[str, tuple[float, float, int]] = {}
    for coin, (bid, ask, ms) in tops.items():
        if bid is not None and ask is not None and ask >= bid > 0:
            out[coin] = (bid, ask, ms)
    return out


def fetch_testnet_bbo(client: httpx.Client, coins: list[str]) -> dict[str, tuple[float, float, int]]:
    """Return ``{coin: (best_bid, best_ask, observed_ms)}`` from Hyperliquid Testnet ``l2Book``."""
    return two_sided_bbo(fetch_testnet_book_tops(client, coins))


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
    position_notional_usd: dict[str, float] = {}
    position_entry_px: dict[str, float] = {}
    position_unrealized_pnl_usd: dict[str, float] = {}
    for entry in st.get("assetPositions") or []:
        pos = entry.get("position") or {}
        coin = str(pos.get("coin") or "").strip()
        szi = float(pos.get("szi", 0.0))
        if coin and abs(szi) > 1e-12:
            positions_sz[coin] = szi
            pos_val = abs(float(pos.get("positionValue", 0.0) or 0.0))
            if pos_val <= 0.0:
                pos_val = abs(szi) * abs(float(pos.get("entryPx", 0.0) or 0.0))
            if pos_val > 0.0:
                position_notional_usd[coin] = math.copysign(pos_val, szi)
            entry_px = float(pos.get("entryPx", 0.0) or 0.0)
            if math.isfinite(entry_px) and entry_px > 0.0:
                position_entry_px[coin] = entry_px
            upnl = float(pos.get("unrealizedPnl", 0.0) or 0.0)
            if math.isfinite(upnl):
                position_unrealized_pnl_usd[coin] = upnl
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
                    if abstraction:
                        is_unified = abstraction in {"unifiedAccount", "portfolioMargin"}
                    else:
                        is_unified = withdrawable <= 0.0 and (spot_hold > 0.0 or perps_eq <= 0.0)
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
        position_notional_usd=position_notional_usd,
        position_entry_px=position_entry_px,
        position_unrealized_pnl_usd=position_unrealized_pnl_usd,
    )


# --------------------------------------------------------------------------- Kill-switch & Order planner


def filter_unquoted_targets(
    target_weights: dict[str, float],
    bbo: dict[str, tuple[float, float, int]],
    *,
    max_missing_fraction: float = 0.35,
) -> tuple[dict[str, float], list[str]]:
    """Drop thinly-quoted Testnet coins lacking a two-sided BBO unless missing share exceeds safety threshold.

    Fail-closed guarantee: if all targets are missing, if fewer than 2 quoted coins remain
    when >= 2 non-zero targets were requested, or if the unquoted fraction exceeds
    ``max_missing_fraction``, returns the original ``target_weights`` unchanged so
    ``check_kill_switch`` halts the cycle on systemic feed outages.
    """
    nonzero = {c: float(w) for c, w in target_weights.items() if abs(float(w)) > 1e-9}
    if not nonzero:
        return dict(target_weights), []
    missing = sorted(c for c in nonzero if c not in bbo)
    if not missing:
        return dict(target_weights), []
    quoted_count = len(nonzero) - len(missing)
    missing_frac = len(missing) / len(nonzero)
    if quoted_count <= 0 or (len(nonzero) >= 2 and quoted_count < 2) or missing_frac > max_missing_fraction + 1e-9:
        return dict(target_weights), []
    missing_set = set(missing)
    filtered = {c: float(w) for c, w in target_weights.items() if c not in missing_set}
    return filtered, missing


def drawdown_pct(equity_usd: float, high_water_mark_usd: float) -> float:
    """Percent drawdown of ``equity_usd`` below the high-water mark (0 when at/above HWM or HWM unset)."""
    if not (math.isfinite(equity_usd) and math.isfinite(high_water_mark_usd)) or high_water_mark_usd <= 0:
        return 0.0
    return max(0.0, (1.0 - equity_usd / high_water_mark_usd) * 100.0)


def check_kill_switch(
    account: AccountSnapshot,
    target_weights: dict[str, float],
    bbo: dict[str, tuple[float, float, int]],
    limits: RiskLimits,
    *,
    high_water_mark_usd: float,
    now_ms: int | None = None,
    retained_coins: set[str] | None = None,
) -> list[str]:
    """Return list of risk violations; non-empty list halts new entries and cancels open orders.

    ``retained_coins`` are held non-target coins the planner will *not* close (another engine's
    inventory), so their notional counts toward gross leverage even when quoted.
    """
    retained = set(retained_coins or ())
    now = now_ms if now_ms is not None else int(time.time() * 1000)
    reasons: list[str] = []

    if not (math.isfinite(account.equity_usd) and math.isfinite(account.free_margin_usd)):
        reasons.append("non_finite_account_snapshot")
        return reasons
    if any(not math.isfinite(float(w)) for w in target_weights.values()):
        reasons.append("non_finite_target_weight")
        return reasons

    if account.equity_usd <= 0:
        reasons.append("zero_or_negative_equity")
        return reasons

    if high_water_mark_usd > 0:
        dd_pct = drawdown_pct(account.equity_usd, high_water_mark_usd)
        if dd_pct >= limits.max_drawdown_pct:
            reasons.append(f"drawdown_limit:{dd_pct:.2f}%>={limits.max_drawdown_pct:.2f}%")

    free_pct = (account.free_margin_usd / account.equity_usd) * 100.0
    if free_pct < limits.min_free_margin_pct:
        reasons.append(f"low_free_margin:{free_pct:.1f}%<{limits.min_free_margin_pct:.1f}%")

    orphan_gross_w = 0.0
    for c, sz in account.positions_sz.items():
        if abs(sz) <= 1e-12 or c in target_weights:
            continue
        if c in bbo and c not in retained:
            # Quoted non-target positions will be closed by plan_rebalance_orders this cycle.
            continue
        ntl = abs(float(account.position_notional_usd.get(c, 0.0) or 0.0))
        orphan_gross_w += ntl / account.equity_usd

    gross = sum(abs(w) for w in target_weights.values()) + orphan_gross_w
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
    exclude_coins: set[str] | None = None,
) -> list[PlannedOrder]:
    """Reconcile live Testnet positions vs target weights and emit sliced post-only ``ALO`` child orders.

    ``exclude_coins`` are non-target coins owned by another engine on the shared address (e.g. MM
    inventory); their positions are left untouched instead of being closed to zero.
    """
    excluded = set(exclude_coins or ()) - set(target_weights.keys())
    all_coins = sorted((set(account.positions_sz.keys()) - excluded) | set(target_weights.keys()))
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
        side = "buy" if delta_sz > 0 else "sell"
        reduce_only = (cur_sz > 0 and side == "sell" and target_sz >= 0) or (cur_sz < 0 and side == "buy" and target_sz <= 0)
        # Hyperliquid enforces ~$10 min notional only on exposure-increasing orders; full reduce-only
        # unwinds (`target_w == 0`) are permitted on sub-$10 dust (e.g. $7.37 JUP / $2.45 BTC).
        min_notional = 0.50 if (reduce_only and abs(target_w) <= 1e-12 and abs(cur_sz) > 1e-12) else limits.min_order_usd
        if delta_usd < min_notional:
            continue

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


ORPHAN_MIN_CLOSE_USD = 0.50


def _orphan_close_px(side: str, bid: float | None, ask: float | None, ref_px: float | None, sz_decimals: int) -> float | None:
    """Passive (post-only) closing price for a position whose book is one-sided or empty.

    Sells never rest at/below the best bid and buys never at/above the best ask, so the ALO order
    cannot cross. Within that bound the order prices at the oracle reference, improving (not
    crossing) the surviving opposite quote when the oracle is beyond it.
    """
    tick_frac = 1e-4
    if side == "sell":
        preferred = ref_px
        if ask is not None:
            preferred = min(preferred, ask) if preferred is not None else ask
        if preferred is None:
            preferred = bid * (1.0 + tick_frac) if bid is not None else None
        if preferred is None:
            return None
        px = round_hl_price(preferred, sz_decimals, side="sell")
        if bid is not None and px <= bid:
            px = round_hl_price(bid * (1.0 + tick_frac), sz_decimals, side="sell")
            if px <= bid:
                return None
        return px
    preferred = ref_px
    if bid is not None:
        preferred = max(preferred, bid) if preferred is not None else bid
    if preferred is None:
        preferred = ask * (1.0 - tick_frac) if ask is not None else None
    if preferred is None or preferred <= 0:
        return None
    px = round_hl_price(preferred, sz_decimals, side="buy")
    if ask is not None and px >= ask:
        px = round_hl_price(ask * (1.0 - tick_frac), sz_decimals, side="buy")
        if px >= ask:
            return None
    return px


def plan_orphan_unwind_orders(
    account: AccountSnapshot,
    meta: dict[str, AssetSpec],
    bbo: dict[str, tuple[float, float, int]],
    book_tops: dict[str, BookTop] | None,
    reference_px: dict[str, float] | None,
    *,
    exclude_coins: set[str] | None = None,
    target_weights: dict[str, float] | None = None,
) -> list[PlannedOrder]:
    """Reduce-only ALO orders unwinding positions on coins that lack a two-sided Testnet book.

    The regular planner skips such coins, so without this they would never be reduced (orphans
    keep accruing funding and mark risk). ``target_weights`` holds the strategy's desired weight
    for such coins (if any): a holding of the same sign and within the desired size (valued at the
    oracle) is kept; only the excess is unwound, and opposite-sign / untargeted holdings are closed.
    """
    tops = book_tops or {}
    refs = reference_px or {}
    targets = target_weights or {}
    excluded = set(exclude_coins or ())
    planned: list[PlannedOrder] = []
    for coin in sorted(account.positions_sz):
        cur_sz = float(account.positions_sz.get(coin, 0.0) or 0.0)
        if abs(cur_sz) <= 1e-12 or coin in bbo or coin in excluded or coin not in meta:
            continue
        spec = meta[coin]
        bid, ask, _ = tops.get(coin, (None, None, 0))
        ref = refs.get(coin)
        ref_px = float(ref) if ref is not None and math.isfinite(float(ref)) and float(ref) > 0 else None
        desired_w = float(targets.get(coin, 0.0) or 0.0)
        if abs(desired_w) > 1e-12 and ref_px is None:
            continue  # cannot size the excess of a still-targeted coin without a reference price
        desired_sz = (account.equity_usd * desired_w / ref_px) if (ref_px and abs(desired_w) > 1e-12) else 0.0
        if desired_sz * cur_sz > 0:
            excess = abs(cur_sz) - abs(desired_sz)
            if excess <= 0:
                continue
        else:
            excess = abs(cur_sz)
        side = "sell" if cur_sz > 0 else "buy"
        limit_px = _orphan_close_px(side, bid, ask, ref_px, spec.sz_decimals)
        if limit_px is None:
            continue
        sz = round_hl_size(excess, spec.sz_decimals)
        if sz <= 0 or sz * limit_px < ORPHAN_MIN_CLOSE_USD:
            continue
        planned.append(
            PlannedOrder(
                coin=coin,
                side=side,
                limit_px=limit_px,
                sz=sz,
                notional_usd=round(sz * limit_px, 2),
                tif="Alo",
                reduce_only=True,
            )
        )
    return planned


def orphan_residual_notional_usd(
    account: AccountSnapshot,
    meta: dict[str, AssetSpec],
    bbo: dict[str, tuple[float, float, int]],
    book_tops: dict[str, BookTop] | None,
    reference_px: dict[str, float] | None,
    *,
    exclude_coins: set[str] | None = None,
    target_weights: dict[str, float] | None = None,
) -> dict[str, float]:
    """Signed USD notional each unquoted position is intended to keep after this cycle's unwinds.

    Mirrors :func:`plan_orphan_unwind_orders`, so dollar-neutralization of the quoted book offsets
    the *residual* orphan exposure instead of exposure that is being closed in the same cycle.
    """
    planned = {
        o.coin: o.sz
        for o in plan_orphan_unwind_orders(
            account, meta, bbo, book_tops, reference_px, exclude_coins=exclude_coins, target_weights=target_weights
        )
    }
    out: dict[str, float] = {}
    for coin, cur_sz in account.positions_sz.items():
        cur = float(cur_sz or 0.0)
        if abs(cur) <= 1e-12 or coin in bbo:
            continue
        ntl = float(account.position_notional_usd.get(coin, 0.0) or 0.0)
        keep_frac = max(0.0, 1.0 - planned.get(coin, 0.0) / abs(cur))
        out[coin] = ntl * keep_frac
    return out


def load_logged_order_oids(audit_log_path: Path | None) -> set[int]:
    """Collect every order id recorded in a JSONL audit log (MM ``mm_audit.jsonl`` or executor log).

    Handles both executor records (``orders[].response`` / ``orders[].oid``) and flat MM records
    (top-level ``oid``); dry-run records are skipped because their ids are simulated.
    """
    oids: set[int] = set()
    if audit_log_path is None or not audit_log_path.is_file():
        return oids

    def _add(raw: Any) -> None:
        try:
            oid = int(raw)
        except (TypeError, ValueError):
            return
        if oid > 0:
            oids.add(oid)

    for line in audit_log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get("mode") == "dry_run":
            continue
        if rec.get("oid") is not None:
            _add(rec.get("oid"))
        for ord_rec in rec.get("orders") or []:
            if not isinstance(ord_rec, dict) or ord_rec.get("mode") == "dry_run":
                continue
            if ord_rec.get("oid") is not None:
                _add(ord_rec.get("oid"))
            statuses, err_msg = extract_hl_order_statuses(ord_rec.get("response") or {})
            if err_msg is not None:
                continue
            for st in statuses:
                if not isinstance(st, dict):
                    continue
                for key in ("resting", "filled"):
                    if isinstance(st.get(key), dict):
                        _add(st[key].get("oid"))
    return oids


def leverage_update_ok(upd: Any) -> bool:
    """True when a ``set_leverage`` adapter result reports success (top-level or SDK ``response``)."""
    if not isinstance(upd, dict) or upd.get("error"):
        return False
    resp = upd.get("response")
    return upd.get("status") == "ok" or (isinstance(resp, dict) and resp.get("status") == "ok")


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


def extract_hl_order_statuses(resp: Any) -> tuple[list[Any], str | None]:
    """Safely extract ``response.data.statuses`` from any Hyperliquid order/modify response.

    Hyperliquid returns ``{"status": "err", "response": "<error message>"}`` on rate-limits
    or wallet/action errors where ``response`` is a string rather than a dict.
    """
    if not isinstance(resp, dict):
        return [], f"invalid_response:{type(resp).__name__}"
    status = str(resp.get("status") or "")
    inner = resp.get("response")
    if status == "err" or isinstance(inner, str):
        err_msg = inner if isinstance(inner, str) else str(inner or "unknown_error")
        return [], err_msg
    if not isinstance(inner, dict):
        return [], None
    data = inner.get("data")
    if not isinstance(data, dict):
        return [], None
    statuses = data.get("statuses")
    return (statuses if isinstance(statuses, list) else []), None


class HyperliquidTestnetSDKAdapter:
    """Live Hyperliquid Testnet adapter via ``hyperliquid-python-sdk`` (hard-pinned to Testnet)."""

    def __init__(self, private_key: str, account_address: str) -> None:
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
        except ImportError as exc:
            raise RuntimeError("Install hyperliquid-python-sdk to submit live Testnet orders") from exc
        wallet = Account.from_key(private_key)
        self._exchange = Exchange(wallet, base_url=TESTNET_API_URL, account_address=account_address, timeout=10.0)

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
        # Official Hyperliquid SDK cancel/bulk_cancel uses canonical {"a": asset, "o": oid} L1 actions
        # prioritized in the HyperCore mempool; never inject non-standard action fields that break signing.
        if len(reqs) == 1:
            resp = self._exchange.cancel(reqs[0]["coin"], reqs[0]["oid"])
            return [{"mode": "testnet_live", "fast": bool(fast), "coin": reqs[0]["coin"], "oid": reqs[0]["oid"], "response": resp}]
        resp = self._exchange.bulk_cancel(reqs)
        return [{"mode": "testnet_live", "fast": bool(fast), "bulk_count": len(reqs), "requests": reqs, "response": resp}]

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
        statuses, err_msg = extract_hl_order_statuses(resp)
        out: list[dict[str, Any]] = []
        for idx, o in enumerate(orders):
            if err_msg is not None:
                item_resp: dict[str, Any] = {"status": "err", "response": err_msg}
            else:
                st = [statuses[idx]] if idx < len(statuses) else []
                item_resp = {"status": (resp or {}).get("status", "ok"), "response": {"data": {"statuses": st}}}
            out.append({"mode": "testnet_live", "order": o.to_dict(), "response": item_resp})
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
        statuses, err_msg = extract_hl_order_statuses(resp)
        out: list[dict[str, Any]] = []
        for idx, (oid, o) in enumerate(modifications):
            if err_msg is not None:
                item_resp: dict[str, Any] = {"status": "err", "response": err_msg}
            else:
                st = [statuses[idx]] if idx < len(statuses) else []
                item_resp = {"status": (resp or {}).get("status", "ok"), "response": {"data": {"statuses": st}}}
            out.append({"mode": "testnet_live", "prev_oid": int(oid), "order": o.to_dict(), "response": item_resp})
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
    cancel_unmanaged_orders: bool = False,
    skipped_unquoted_coins: list[str] | None = None,
    book_tops: dict[str, BookTop] | None = None,
    reference_px: dict[str, float] | None = None,
    foreign_order_oids: set[int] | None = None,
    foreign_coins: set[str] | None = None,
    orphan_target_weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Run one complete Testnet rebalance cycle: kill-switch -> cancel stale -> place sliced ALO orders.

    ``high_water_mark_usd`` must be the peak equity persisted across cycles; without it the
    drawdown gate only compares equity with itself and can never fire.

    Shared-address isolation: ``foreign_order_oids`` (e.g. ids logged by the MM in
    ``mm_audit.jsonl``) are never cancelled except on an account-risk kill-switch, and positions on
    ``foreign_coins`` that are not rebalance targets are left to their owning engine.

    Kill-switch behaviour (``summary["derisk_mode"]``):

    * hard data violations (non-finite snapshot, non-positive equity, stale feed) submit nothing;
    * a ``missing_quote`` feed gap halts BBO rebalancing but still unwinds orphan positions
      (reduce-only, oracle-priced) — ``orphan_unwind_only``;
    * account-risk violations (drawdown, free margin, gross, coin cap) cancel every open order and
      keep submitting reduce-only orders toward targets (``reduce_only``); a drawdown breach
      flattens every position instead (``flatten``, ``limits.flatten_on_drawdown``).
    """
    hwm = max(float(high_water_mark_usd or 0.0), 0.0) or account.equity_usd
    foreign_oids = {int(o) for o in (foreign_order_oids or set())}
    foreign_order_coins = {
        str(o.get("coin") or "")
        for o in account.open_orders
        if o.get("oid") is not None and safe_int(o.get("oid")) in foreign_oids
    }
    foreign = (set(foreign_coins or ()) | foreign_order_coins) - set(target_weights.keys())
    skipped_unquoted = sorted(set(skipped_unquoted_coins or []))
    # Open positions on coins we cannot quote (whether dropped from targets or already target=0)
    # are unmanaged: neither rebalanced nor closable by the ALO planner. Surface all of them.
    unquoted_open_coins = set(skipped_unquoted) | {c for c in account.positions_sz if c not in bbo}
    orphan_positions = {
        c: account.positions_sz[c]
        for c in sorted(unquoted_open_coins)
        if abs(float(account.positions_sz.get(c, 0.0) or 0.0)) > 1e-12
    }
    violations = check_kill_switch(
        account, target_weights, bbo, limits, high_water_mark_usd=hwm, now_ms=now_ms, retained_coins=foreign
    )
    # Persist the account view the kill-switch evaluated, so the audit trail (and /metrics) show
    # equity, drawdown and orphan exposure even on cycles where nothing trips.
    orphan_notional_usd = sum(abs(float(account.position_notional_usd.get(c, 0.0) or 0.0)) for c in orphan_positions)
    account_fields: dict[str, Any] = {
        "equity_usd": round(account.equity_usd, 6),
        "free_margin_usd": round(account.free_margin_usd, 6),
        "drawdown_pct": round(drawdown_pct(account.equity_usd, hwm), 4),
        "orphan_notional_usd": round(orphan_notional_usd, 4),
    }

    _, account_risk, drawdown_breach = classify_violations(violations)
    hard_data = any(v.startswith(HARD_DATA_VIOLATIONS) for v in violations)
    # Reduce-only de-risking is allowed unless the account snapshot itself is untrustworthy; a
    # missing-quote feed gap only removes the affected coins from the BBO planner.
    derisk_allowed = bool(violations) and limits.derisk_on_kill_switch and not hard_data
    flatten = derisk_allowed and account_risk and drawdown_breach and limits.flatten_on_drawdown

    # Account-risk emergency (or explicit opt-in) cancels ALL open orders on the address. Otherwise
    # (normal cycle or a data-integrity halt) only orders in the rebalance scope are cancelled and
    # orders positively identified as another engine's (MM) are preserved.
    if (violations and account_risk) or cancel_unmanaged_orders:
        orders_to_cancel = list(account.open_orders)
    else:
        managed_coins = (set(target_weights.keys()) | set(account.positions_sz.keys()) | set(skipped_unquoted)) - foreign
        orders_to_cancel = [
            o
            for o in account.open_orders
            if str(o.get("coin") or "") in managed_coins and safe_int(o.get("oid")) not in foreign_oids
        ]
    cancelled = adapter.cancel_all_open_orders(orders_to_cancel) if orders_to_cancel else []

    orphan_unwinds: list[PlannedOrder] = []
    if limits.unwind_unquoted_orphans and not hard_data:
        orphan_unwinds = plan_orphan_unwind_orders(
            account,
            meta,
            bbo,
            book_tops,
            reference_px,
            exclude_coins=set() if flatten else foreign,
            # Keep strategy-intended holdings on unquoted coins, except when flattening everything.
            target_weights={} if flatten else (orphan_target_weights if orphan_target_weights is not None else target_weights),
        )

    if violations:
        derisk_mode = "none"
        derisk_orders: list[PlannedOrder] = []
        if not derisk_allowed:
            orphan_unwinds = []
        elif not account_risk and orphan_unwinds:
            # Feed gap (missing quotes) halts BBO rebalancing, but oracle-priced reduce-only
            # unwinds of orphans only shrink exposure and remain allowed.
            derisk_mode = "orphan_unwind_only"
            derisk_orders = list(orphan_unwinds)
        if derisk_allowed and account_risk:
            derisk_mode = "flatten" if flatten else "reduce_only"
            if flatten:
                plan_targets = {c: 0.0 for c in target_weights}
            else:
                # A reversal target (opposite sign to the held position) would plan one non-reduce-only
                # order through zero and be filtered out entirely; clamp it to 0 so the closing part
                # still executes without opening the new side.
                plan_targets = {
                    c: (0.0 if float(account.positions_sz.get(c, 0.0) or 0.0) * float(w) < 0 else float(w))
                    for c, w in target_weights.items()
                }
            derisk_orders = [
                o
                for o in plan_rebalance_orders(
                    account,
                    plan_targets,
                    meta,
                    bbo,
                    limits,
                    funding_apr_by_coin=funding_apr_by_coin,
                    now_ms=now_ms,
                    exclude_coins=set() if flatten else foreign,
                )
                if o.reduce_only
            ] + orphan_unwinds
        placed = [adapter.place_alo_order(o) for o in derisk_orders]
        summary = {
            "status": "kill_switch_triggered",
            "violations": violations,
            "derisk_mode": derisk_mode,
            "cancelled_count": len(cancelled),
            "placed_count": len(placed),
            "total_notional_usd": round(sum(o.notional_usd for o in derisk_orders), 2),
            "orphan_unwind_count": len(orphan_unwinds),
            "high_water_mark_usd": hwm,
            "skipped_unquoted_coins": skipped_unquoted,
            "orphan_positions": orphan_positions,
            "foreign_coins": sorted(foreign),
            **account_fields,
            "orders": placed,
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
            exclude_coins=foreign,
        )
        leverage_updates: list[dict[str, Any]] = []
        blocked_isolated_coins: set[str] = set()
        blocked_leverage_coins: set[str] = set()
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
            if cur_mode == expected_mode and cur_lev == target_lev:
                continue
            try:
                upd = adapter.set_leverage(o.coin, target_lev, is_cross=is_cross)
            except Exception as exc:
                upd = {"coin": o.coin, "leverage": target_lev, "is_cross": is_cross, "error": str(exc)}
            leverage_updates.append(upd)
            if not leverage_update_ok(upd):
                # Never open exposure at an unknown (exchange default / max) leverage.
                (blocked_isolated_coins if spec.only_isolated else blocked_leverage_coins).add(o.coin)

        blocked = blocked_isolated_coins | blocked_leverage_coins
        executable = [o for o in planned if o.reduce_only or o.coin not in blocked] + orphan_unwinds
        placed = [adapter.place_alo_order(o) for o in executable]
        summary = {
            "status": "ok",
            "violations": [],
            "cancelled_count": len(cancelled),
            "placed_count": len(placed),
            "total_notional_usd": round(sum(o.notional_usd for o in executable), 2),
            "orphan_unwind_count": len(orphan_unwinds),
            "high_water_mark_usd": hwm,
            "leverage_updates": leverage_updates,
            "blocked_isolated_coins": sorted(blocked_isolated_coins),
            "blocked_leverage_coins": sorted(blocked_leverage_coins),
            "skipped_unquoted_coins": skipped_unquoted,
            "orphan_positions": orphan_positions,
            "foreign_coins": sorted(foreign),
            **account_fields,
            "orders": placed,
        }

    if audit_log_path is not None:
        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
        record = {"ts_ms": now_ms or int(time.time() * 1000), **summary}
        with audit_log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    return summary


def neutralize_target_weights(
    weights: dict[str, float],
    *,
    max_gross: float = 0.90,
    max_single_coin_weight: float = 0.25,
    fixed_net_weight: float = 0.0,
) -> dict[str, float]:
    """Re-center signed weights so ``sum(w) + fixed_net_weight == 0`` while honoring gross & single-coin caps.

    ``fixed_net_weight`` represents signed exposure locked in unquoted orphan positions that cannot
    be traded this cycle, keeping the *total* account dollar-neutral.
    """
    clean = {c: float(w) for c, w in weights.items() if math.isfinite(w) and abs(w) >= 1e-4}
    if len(clean) < 2:
        if len(clean) == 1 and max_single_coin_weight > 0:
            c, w = next(iter(clean.items()))
            if abs(w) > max_single_coin_weight:
                return {c: round(math.copysign(max_single_coin_weight, w), 6)}
        return clean
    eff_fixed = float(fixed_net_weight) if math.isfinite(fixed_net_weight) else 0.0
    mean_w = (sum(clean.values()) + eff_fixed) / len(clean)
    centered = {c: w - mean_w for c, w in clean.items()}
    gross = sum(abs(w) for w in centered.values())
    max_abs = max((abs(w) for w in centered.values()), default=0.0)
    scale = 1.0
    if gross > max_gross and gross > 1e-9:
        scale = min(scale, max_gross / gross)
    if max_single_coin_weight > 0 and max_abs > max_single_coin_weight:
        scale = min(scale, max_single_coin_weight / max_abs)
    if scale < 1.0:
        centered = {c: w * scale for c, w in centered.items()}
        # Re-adjust mean if scaled down while offsetting a non-zero fixed_net_weight
        if abs(eff_fixed) > 1e-6 and len(centered) >= 2:
            rem_shift = (sum(centered.values()) + eff_fixed) / len(centered)
            shifted = {c: w - rem_shift for c, w in centered.items()}
            if (
                sum(abs(w) for w in shifted.values()) <= max_gross + 1e-6
                and max((abs(w) for w in shifted.values()), default=0.0) <= (max_single_coin_weight or 1e9) + 1e-6
            ):
                centered = shifted
    out = {c: round(w, 6) for c, w in centered.items() if abs(w) >= 1e-4}
    if max_single_coin_weight > 0:
        out = {
            c: (math.copysign(max_single_coin_weight, w) if abs(w) > max_single_coin_weight else w)
            for c, w in out.items()
        }
    return out


def update_high_water_mark(hwm_path: Path, equity_usd: float) -> float:
    """Persist and return peak Testnet equity for cross-run drawdown kill-switch."""
    prev = equity_usd
    if hwm_path.is_file():
        try:
            raw = json.loads(hwm_path.read_text())
            prev = float(raw.get("high_water_mark_usd", equity_usd)) if isinstance(raw, dict) else equity_usd
            if not math.isfinite(prev):
                prev = equity_usd
        except (ValueError, OSError, TypeError):
            prev = equity_usd
    hwm = max(prev, equity_usd)
    hwm_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = hwm_path.with_suffix(hwm_path.suffix + ".tmp")
    tmp.write_text(json.dumps({"high_water_mark_usd": hwm, "updated_ms": int(time.time() * 1000)}, indent=2))
    os.replace(tmp, hwm_path)
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
        derisk_txt = {
            "flatten": "закрытие всех позиций (reduce-only)",
            "reduce_only": "только сокращение позиций (reduce-only)",
            "orphan_unwind_only": "закрытие позиций без стакана по oracle (reduce-only)",
        }.get(str(result.get("derisk_mode") or ""))
        if derisk_txt:
            parts.append(f"<p><b>🛡 Снижение риска:</b> {html.escape(derisk_txt)}</p>")
    if result.get("skipped_unquoted_coins"):
        parts.append(
            f"<p><b>ℹ️ Пропущены (пустой стакан Testnet):</b> "
            f"{html.escape(', '.join(result['skipped_unquoted_coins']))}</p>"
        )

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
            st_txt = str(resp.get("status", rec.get("status", "ok"))) if isinstance(resp, dict) else str(resp)
            if isinstance(resp, dict):
                statuses, err_msg = extract_hl_order_statuses(resp)
                if err_msg is not None:
                    st_txt = f"err: {err_msg[:60]}"
                elif statuses and isinstance(statuses[0], dict):
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
