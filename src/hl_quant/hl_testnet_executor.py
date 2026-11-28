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
import threading
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
    # Do not OPEN exposure where the thin Testnet book mid deviates from the oracle by more than
    # this (reduce-only orders are still allowed): joining a dislocated book buys the dislocation.
    max_entry_oracle_dev_pct: float = 10.0


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
    perps_equity_usd: float | None = None
    is_unified: bool = True


def effective_perp_equity_usd(account: AccountSnapshot) -> float:
    """Collateral backing Hyperliquid Perps (`perps_equity_usd` on Standard Accounts, `equity_usd` on Unified)."""
    if not account.is_unified and account.perps_equity_usd is not None:
        return float(account.perps_equity_usd)
    return float(account.equity_usd)


def account_gross_notional_usd(
    account: AccountSnapshot,
    reference_px: dict[str, float] | None = None,
    bbo: dict[str, tuple[float, float, int]] | None = None,
) -> float:
    """Total absolute USD notional across all non-zero open Perp positions."""
    refs = reference_px or {}
    quotes = bbo or {}
    total = 0.0
    for coin, raw_sz in account.positions_sz.items():
        sz = float(raw_sz or 0.0)
        if abs(sz) <= 1e-12:
            continue
        ntl = abs(float(account.position_notional_usd.get(coin, 0.0) or 0.0))
        if ntl <= 0.0:
            if coin in quotes:
                ntl = abs(sz) * 0.5 * (quotes[coin][0] + quotes[coin][1])
            elif coin in refs and math.isfinite(float(refs[coin])) and float(refs[coin]) > 0:
                ntl = abs(sz) * float(refs[coin])
        total += ntl
    return total


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


def _post_testnet_info(
    client: httpx.Client,
    payload: dict[str, Any],
    *,
    max_attempts: int = 3,
    retry_delay_s: float = 0.05,
) -> httpx.Response:
    """POST to Hyperliquid Testnet ``/info`` with bounded retry on HTTP 429 / 5xx / transport errors."""
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = client.post(TESTNET_INFO_URL, json=payload)
            status_code = int(getattr(resp, "status_code", 200))
            if status_code == 429 or status_code >= 500:
                if attempt < max_attempts:
                    time.sleep(retry_delay_s * attempt)
                    continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPError as exc:
            if (
                isinstance(exc, httpx.HTTPStatusError)
                and 400 <= exc.response.status_code < 500
                and exc.response.status_code != 429
            ):
                raise
            last_exc = exc
            if attempt < max_attempts:
                time.sleep(retry_delay_s * attempt)
    assert last_exc is not None
    raise last_exc


def fetch_testnet_meta(client: httpx.Client) -> dict[str, AssetSpec]:
    resp = _post_testnet_info(client, {"type": "meta"})
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


def strip_own_orders_from_l2_levels(
    levels: Any,
    open_orders: list[dict[str, Any]] | None,
    coin: str,
) -> list[list[dict[str, Any]]]:
    """Subtract our own resting orders on ``coin`` from Hyperliquid ``l2Book`` ``[bids, asks]``.

    Prevents a resting orphan-unwind ALO order we placed on an empty side from making a 1-sided
    book appear 2-sided on the next cycle (which would cancel the order and oscillate).
    """
    if not (isinstance(levels, list) and len(levels) == 2):
        return [[], []]
    raw_bids = [lv for lv in (levels[0] or []) if isinstance(lv, dict)]
    raw_asks = [lv for lv in (levels[1] or []) if isinstance(lv, dict)]
    if not open_orders:
        return [raw_bids, raw_asks]

    own_by_side: tuple[list[tuple[float, float]], list[tuple[float, float]]] = ([], [])
    for oo in open_orders:
        if not isinstance(oo, dict) or str(oo.get("coin") or "").strip() != coin:
            continue
        side_raw = str(oo.get("side") or "").strip().upper()
        if side_raw in {"B", "BUY"}:
            s_idx = 0
        elif side_raw in {"A", "S", "SELL"}:
            s_idx = 1
        else:
            continue
        raw_px = oo.get("limitPx", oo.get("px", oo.get("limit_px")))
        raw_sz = oo.get("sz", oo.get("origSz"))
        try:
            px_val = float(raw_px)  # type: ignore[arg-type]
            sz_val = float(raw_sz)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if math.isfinite(px_val) and px_val > 0 and math.isfinite(sz_val) and sz_val > 0:
            own_by_side[s_idx].append((px_val, sz_val))

    if not own_by_side[0] and not own_by_side[1]:
        return [raw_bids, raw_asks]

    cleaned: list[list[dict[str, Any]]] = [[], []]
    for s_idx, side_levels in enumerate((raw_bids, raw_asks)):
        own_list = own_by_side[s_idx]
        if not own_list:
            cleaned[s_idx] = side_levels
            continue
        for lv in side_levels:
            try:
                lv_px = float(lv.get("px"))  # type: ignore[arg-type]
                lv_sz = float(lv.get("sz", 0.0) or 0.0)
            except (TypeError, ValueError):
                continue
            if not (math.isfinite(lv_px) and lv_px > 0 and math.isfinite(lv_sz) and lv_sz > 0):
                continue
            lv_n = safe_int(lv.get("n")) if "n" in lv else None
            tol = max(1e-9, 1e-6 * lv_px)
            matched_sz = 0.0
            matched_n = 0
            for oo_px, oo_sz in own_list:
                if abs(oo_px - lv_px) <= tol:
                    matched_sz += oo_sz
                    matched_n += 1
            if matched_n == 0:
                cleaned[s_idx].append(lv)
                continue
            rem_sz = lv_sz - matched_sz
            rem_n = (lv_n - matched_n) if lv_n is not None else None
            if rem_sz <= max(1e-9, 1e-6 * lv_sz) or (rem_n is not None and rem_n <= 0):
                continue
            upd = dict(lv)
            upd["sz"] = f"{rem_sz:.10g}"
            if rem_n is not None:
                upd["n"] = rem_n
            cleaned[s_idx].append(upd)
    return cleaned


def fetch_testnet_book_tops(
    client: httpx.Client,
    coins: list[str],
    *,
    open_orders: list[dict[str, Any]] | None = None,
) -> dict[str, BookTop]:
    """Return ``{coin: (best_bid | None, best_ask | None, observed_ms)}`` including one-sided books.

    A well-formed but empty/one-sided book (routine on thin Testnet alts) keeps a positive
    ``observed_ms``; a malformed response is marked ``observed_ms == 0`` so callers can tell a
    genuine feed failure apart from an illiquid market. When ``open_orders`` is supplied, our own
    resting liquidity is stripped first so a self-quote never fakes a two-sided book.
    """
    out: dict[str, BookTop] = {}
    for coin in sorted(set(coins)):
        resp = _post_testnet_info(client, {"type": "l2Book", "coin": coin})
        book = resp.json()
        levels = book.get("levels") if isinstance(book, dict) else None
        if not (isinstance(levels, list) and len(levels) == 2):
            out[coin] = (None, None, 0)
            continue
        eff_levels = strip_own_orders_from_l2_levels(levels, open_orders, coin) if open_orders else levels
        # Exchange-side book time, so the stale-feed gate measures real quote age.
        book_ms = book.get("time")
        observed_ms = int(book_ms) if isinstance(book_ms, (int, float)) and book_ms > 0 else int(time.time() * 1000)
        out[coin] = (_level_px(eff_levels[0]), _level_px(eff_levels[1]), observed_ms)
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
    st_resp = _post_testnet_info(client, {"type": "clearinghouseState", "user": account_address})
    st = st_resp.json() or {}
    margin = st.get("marginSummary") or {}
    perps_eq = float(margin.get("accountValue", 0.0))
    used_margin = float(margin.get("totalMarginUsed", 0.0))
    withdrawable = float(st.get("withdrawable", 0.0))
    equity = perps_eq
    perps_equity = perps_eq
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
        ua_resp = _post_testnet_info(client, {"type": "userAbstraction", "user": account_address})
        ua_val = ua_resp.json()
        if isinstance(ua_val, str):
            abstraction = ua_val
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code not in {400, 404, 422}:
            raise
    except ValueError:
        abstraction = ""

    spot_resp = _post_testnet_info(client, {"type": "spotClearinghouseState", "user": account_address})
    spot_payload = spot_resp.json()
    spot_balances = (spot_payload.get("balances") or []) if isinstance(spot_payload, dict) else []
    is_unified_acct = abstraction in {"unifiedAccount", "portfolioMargin"} if abstraction else False
    for bal in spot_balances:
        if str(bal.get("coin")) == "USDC":
            spot_total = float(bal.get("total", 0.0))
            spot_hold = float(bal.get("hold", 0.0))
            if spot_total > 0:
                if abstraction:
                    is_unified = abstraction in {"unifiedAccount", "portfolioMargin"}
                else:
                    is_unified = withdrawable <= 0.0 and (spot_hold > 0.0 or perps_eq <= 0.0)
                is_unified_acct = is_unified
                if is_unified:
                    equity = spot_total
                    perps_equity = spot_total
                    free_margin = max(0.0, spot_total - spot_hold)
                else:
                    # Standard Account: separate Perps and Spot wallets; Spot USDC cannot
                    # margin Perps without an explicit usdClassTransfer.
                    equity = perps_eq + spot_total
                    perps_equity = perps_eq
                    free_margin = max(0.0, perps_eq - used_margin)
            break

    open_orders: list[dict[str, Any]] = []
    try:
        foo_resp = _post_testnet_info(client, {"type": "frontendOpenOrders", "user": account_address})
        foo_val = foo_resp.json()
        if isinstance(foo_val, list) and foo_val:
            open_orders = [o for o in foo_val if isinstance(o, dict)]
    except Exception:
        open_orders = []
    if not open_orders:
        oo_resp = _post_testnet_info(client, {"type": "openOrders", "user": account_address})
        oo_val = oo_resp.json()
        if isinstance(oo_val, list):
            open_orders = [o for o in oo_val if isinstance(o, dict)]
    return AccountSnapshot(
        equity_usd=equity,
        free_margin_usd=free_margin,
        positions_sz=positions_sz,
        open_orders=open_orders,
        observed_at_ms=int(time.time() * 1000),
        position_leverage=position_leverage,
        position_notional_usd=position_notional_usd,
        position_entry_px=position_entry_px,
        position_unrealized_pnl_usd=position_unrealized_pnl_usd,
        perps_equity_usd=perps_equity,
        is_unified=is_unified_acct,
    )


# --------------------------------------------------------------------------- Kill-switch & Order planner


def filter_unquoted_targets(
    target_weights: dict[str, float],
    bbo: dict[str, tuple[float, float, int]],
    *,
    max_missing_fraction: float = 0.35,
    book_tops: dict[str, BookTop] | None = None,
) -> tuple[dict[str, float], list[str]]:
    """Drop thinly-quoted Testnet coins lacking a two-sided BBO unless missing share exceeds safety threshold.

    Fail-closed guarantee: if all targets are missing, if fewer than 2 quoted coins remain
    when >= 2 non-zero targets were requested, or if the share of coins whose book could NOT be
    read exceeds ``max_missing_fraction``, returns the original ``target_weights`` unchanged so
    ``check_kill_switch`` halts the cycle on systemic feed outages.

    When ``book_tops`` is given, a coin whose ``l2Book`` was read successfully but is empty or
    one-sided (``observed_ms > 0``) is an illiquid market, not a feed outage: it is always skipped
    and does not count toward the outage fraction.
    """
    nonzero = {c: float(w) for c, w in target_weights.items() if abs(float(w)) > 1e-9}
    if not nonzero:
        return dict(target_weights), []
    missing = sorted(c for c in nonzero if c not in bbo)
    if not missing:
        return dict(target_weights), []
    quoted_count = len(nonzero) - len(missing)
    if book_tops is None:
        missing_frac = len(missing) / len(nonzero)
        if quoted_count <= 0 or (len(nonzero) >= 2 and quoted_count < 2) or missing_frac > max_missing_fraction + 1e-9:
            return dict(target_weights), []
    else:
        outage = [c for c in missing if c not in book_tops or int(book_tops[c][2] or 0) <= 0]
        missing_frac = len(outage) / len(nonzero)
        if missing_frac > max_missing_fraction + 1e-9 or (quoted_count <= 0 and len(outage) > 0):
            return dict(target_weights), []
        if quoted_count <= 0 or (len(nonzero) >= 2 and quoted_count < 2):
            # Books were read cleanly (`observed_ms > 0`) yet <2 coins have two-sided Testnet BBO:
            # skip opening a 1-legged directional book without tripping a feed-outage kill-switch.
            return {}, sorted(nonzero.keys())
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

    perp_eq = effective_perp_equity_usd(account)
    if not (math.isfinite(account.equity_usd) and math.isfinite(account.free_margin_usd) and math.isfinite(perp_eq)):
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

    free_pct = (account.free_margin_usd / perp_eq) * 100.0 if perp_eq > 0 else 0.0
    if free_pct < limits.min_free_margin_pct:
        reasons.append(f"low_free_margin:{free_pct:.1f}%<{limits.min_free_margin_pct:.1f}%")

    gross = 0.0
    for c in set(target_weights.keys()) | set(account.positions_sz.keys()):
        tw = abs(float(target_weights.get(c, 0.0) or 0.0))
        sz = float(account.positions_sz.get(c, 0.0) or 0.0)
        if abs(sz) > 1e-12:
            ntl_abs = abs(float(account.position_notional_usd.get(c, 0.0) or 0.0))
            held_w = (ntl_abs / perp_eq) if perp_eq > 0 else (999.0 if ntl_abs > 0 else 0.0)
        else:
            held_w = 0.0
        if c not in bbo or c in retained:
            # Unquoted orphans and MM-retained coins cannot be rebalanced by the BBO planner;
            # count whichever is larger between held notional and any target weight without double-counting.
            gross += max(tw, held_w)
        else:
            gross += tw
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
        resp = _post_testnet_info(client, {"type": "metaAndAssetCtxs"})
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


def _emit_sliced_alo_leg(
    coin: str,
    side: str,
    abs_delta_sz: float,
    limit_px: float,
    sz_decimals: int,
    *,
    reduce_only: bool,
    is_full_close: bool,
    max_child_usd: float,
    min_order_usd: float,
    ref_px: float | None = None,
    start_child_index: int = 0,
) -> list[PlannedOrder]:
    """Slice one pure-ALO leg into child orders, assigning exact remainder on full-close final slice."""
    if not (math.isfinite(abs_delta_sz) and abs_delta_sz > 0 and math.isfinite(limit_px) and limit_px > 0):
        return []
    eff_min_usd = 0.0 if is_full_close else float(min_order_usd)
    sizing_px = float(ref_px) if (ref_px is not None and math.isfinite(ref_px) and ref_px > 0) else limit_px
    total_sz = round_hl_size(abs_delta_sz, sz_decimals)
    if total_sz <= 0:
        return []
    delta_usd = abs_delta_sz * sizing_px
    if delta_usd < eff_min_usd:
        return []

    n_slices = max(1, int(math.ceil(delta_usd / max(float(max_child_usd), 1.0))))
    base_slice_sz = round_hl_size(total_sz / n_slices, sz_decimals)
    if base_slice_sz <= 0 or (not is_full_close and base_slice_sz * limit_px < eff_min_usd):
        base_slice_sz = total_sz
        n_slices = 1
    if base_slice_sz <= 0 or (not is_full_close and base_slice_sz * limit_px < eff_min_usd):
        return []

    out: list[PlannedOrder] = []
    if is_full_close and n_slices > 1:
        factor = 10 ** max(0, int(sz_decimals))
        total_lots = int(round(total_sz * factor))
        q_lots, r_lots = divmod(total_lots, n_slices)
        sum_prev_sz = 0.0
        for idx in range(n_slices):
            if idx < n_slices - 1:
                lots_i = q_lots + (1 if idx >= (n_slices - r_lots) and r_lots > 0 else 0)
                sz_i = lots_i / factor
                sum_prev_sz += sz_i
            else:
                sz_i = round_hl_size(max(0.0, total_sz - sum_prev_sz) + 1e-12, sz_decimals)
            if sz_i <= 0:
                continue
            out.append(
                PlannedOrder(
                    coin=coin,
                    side=side,
                    limit_px=limit_px,
                    sz=sz_i,
                    notional_usd=round(sz_i * limit_px, 2),
                    tif="Alo",
                    reduce_only=True,
                    child_index=start_child_index + len(out),
                )
            )
        return out

    for idx in range(n_slices):
        out.append(
            PlannedOrder(
                coin=coin,
                side=side,
                limit_px=limit_px,
                sz=base_slice_sz,
                notional_usd=round(base_slice_sz * limit_px, 2),
                tif="Alo",
                reduce_only=reduce_only,
                child_index=start_child_index + idx,
            )
        )
    return out


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

    ``exclude_coins`` are coins owned by another engine on the shared address (e.g. MM inventory);
    their positions are left untouched even if they appear in ``target_weights``.
    Sign-reversing rebalances (``cur_sz * target_sz < 0``) are split into a ``reduce_only=True``
    close leg (``cur_sz -> 0``) plus a ``reduce_only=False`` open leg (``0 -> target_sz``) so entry
    gates never strand an existing opposite-sign position.
    """
    excluded = set(exclude_coins or ())
    all_coins = sorted((set(account.positions_sz.keys()) | set(target_weights.keys())) - excluded)
    planned: list[PlannedOrder] = []
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    fund_map = funding_apr_by_coin or {}
    perp_eq = max(0.0, effective_perp_equity_usd(account))

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
        target_sz = (perp_eq * target_w) / mid

        # Split sign-flip rebalances into (1) full reduce-only close to 0 + (2) fresh entry from 0.
        if abs(cur_sz) > 1e-12 and abs(target_sz) > 1e-12 and (cur_sz > 0) != (target_sz > 0):
            close_side = "sell" if cur_sz > 0 else "buy"
            close_raw_px = bid if close_side == "buy" else ask
            close_px = round_hl_price(close_raw_px, spec.sz_decimals, side=close_side)
            close_orders = _emit_sliced_alo_leg(
                coin,
                close_side,
                abs(cur_sz),
                close_px,
                spec.sz_decimals,
                reduce_only=True,
                is_full_close=True,
                max_child_usd=limits.max_child_usd,
                min_order_usd=limits.min_order_usd,
                ref_px=mid,
                start_child_index=0,
            )
            planned.extend(close_orders)

            open_side = "buy" if target_sz > 0 else "sell"
            if open_side == "buy" and is_pre_settlement_neg_funding_trap(ts_ms, fund_map.get(coin, 0.0), limits):
                continue
            open_raw_px = bid if open_side == "buy" else ask
            open_px = round_hl_price(open_raw_px, spec.sz_decimals, side=open_side)
            planned.extend(
                _emit_sliced_alo_leg(
                    coin,
                    open_side,
                    abs(target_sz),
                    open_px,
                    spec.sz_decimals,
                    reduce_only=False,
                    is_full_close=False,
                    max_child_usd=limits.max_child_usd,
                    min_order_usd=limits.min_order_usd,
                    ref_px=mid,
                    start_child_index=len(close_orders),
                )
            )
            continue

        delta_sz = target_sz - cur_sz
        if abs(delta_sz) <= 1e-12:
            continue
        side = "buy" if delta_sz > 0 else "sell"
        reduce_only = (cur_sz > 0 and side == "sell" and target_sz >= -1e-12) or (
            cur_sz < 0 and side == "buy" and target_sz <= 1e-12
        )
        is_full_close = bool(reduce_only and abs(target_sz) <= 1e-12 and abs(cur_sz) > 1e-12)
        if side == "buy" and not reduce_only and is_pre_settlement_neg_funding_trap(ts_ms, fund_map.get(coin, 0.0), limits):
            # Defer naive pre-:00 long entry on extreme negative funding until after :00 unwind
            continue
        raw_px = bid if side == "buy" else ask
        limit_px = round_hl_price(raw_px, spec.sz_decimals, side=side)
        planned.extend(
            _emit_sliced_alo_leg(
                coin,
                side,
                abs(delta_sz),
                limit_px,
                spec.sz_decimals,
                reduce_only=reduce_only,
                is_full_close=is_full_close,
                max_child_usd=limits.max_child_usd,
                min_order_usd=limits.min_order_usd,
                ref_px=mid,
                start_child_index=0,
            )
        )
    return planned


ORPHAN_MIN_CLOSE_USD = 0.0


def _orphan_close_px(
    side: str,
    bid: float | None,
    ask: float | None,
    ref_px: float | None,
    sz_decimals: int,
    *,
    max_oracle_slip_pct: float = 2.0,
) -> float | None:
    """Passive (post-only) closing price for a position whose book is one-sided or empty.

    Requires a finite positive ``ref_px`` (oracle) and bounds adverse slippage within
    ``max_oracle_slip_pct`` of the oracle so a rogue Testnet quote on a thin book can never drag
    an unwind order far away from fair value.
    """
    if ref_px is None or not math.isfinite(ref_px) or ref_px <= 0:
        return None
    max_adverse_frac = max(0.0, float(max_oracle_slip_pct) / 100.0)
    tick_frac = 1e-4
    if side == "sell":
        floor_px = ref_px * (1.0 - max_adverse_frac)
        preferred = ref_px
        if ask is not None and math.isfinite(ask) and ask >= floor_px:
            preferred = min(preferred, ask)
        px = round_hl_price(preferred, sz_decimals, side="sell")
        if bid is not None and math.isfinite(bid) and px <= bid:
            px = round_hl_price(bid * (1.0 + tick_frac), sz_decimals, side="sell")
            if px <= bid:
                return None
        if not math.isfinite(px) or px <= 0 or (px < floor_px and (bid is None or px <= bid)):
            return None
        return px
    ceil_px = ref_px * (1.0 + max_adverse_frac)
    preferred = ref_px
    if bid is not None and math.isfinite(bid) and bid <= ceil_px:
        preferred = max(preferred, bid)
    px = round_hl_price(preferred, sz_decimals, side="buy")
    if ask is not None and math.isfinite(ask) and px >= ask:
        px = round_hl_price(ask * (1.0 - tick_frac), sz_decimals, side="buy")
        if px >= ask:
            return None
    if not math.isfinite(px) or px <= 0 or (px > ceil_px and (ask is None or px >= ask)):
        return None
    return px


def plan_orphan_unwind_orders(
    account: AccountSnapshot,
    meta: dict[str, AssetSpec],
    bbo: dict[str, tuple[float, float, int]],
    book_tops: dict[str, BookTop] | None,
    reference_px: dict[str, float] | None,
    *,
    limits: RiskLimits = RiskLimits(),
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
    perp_eq = max(0.0, effective_perp_equity_usd(account))
    planned: list[PlannedOrder] = []
    for coin in sorted(account.positions_sz):
        cur_sz = float(account.positions_sz.get(coin, 0.0) or 0.0)
        if abs(cur_sz) <= 1e-12 or coin in bbo or coin in excluded or coin not in meta:
            continue
        spec = meta[coin]
        bid, ask, _ = tops.get(coin, (None, None, 0))
        ref = refs.get(coin)
        ref_px = float(ref) if ref is not None and math.isfinite(float(ref)) and float(ref) > 0 else None
        if ref_px is None:
            continue  # require a valid oracle anchor before pricing an orphan unwind
        desired_w = float(targets.get(coin, 0.0) or 0.0)
        desired_sz = (perp_eq * desired_w / ref_px) if abs(desired_w) > 1e-12 else 0.0
        if desired_sz * cur_sz > 0:
            excess = abs(cur_sz) - abs(desired_sz)
            if excess <= 0:
                continue
        else:
            excess = abs(cur_sz)
        is_full_close = abs(desired_sz) <= 1e-12 or excess >= abs(cur_sz) - 1e-12
        side = "sell" if cur_sz > 0 else "buy"
        limit_px = _orphan_close_px(side, bid, ask, ref_px, spec.sz_decimals)
        if limit_px is None:
            continue
        planned.extend(
            _emit_sliced_alo_leg(
                coin,
                side,
                excess,
                limit_px,
                spec.sz_decimals,
                reduce_only=True,
                is_full_close=is_full_close,
                max_child_usd=limits.max_child_usd,
                min_order_usd=limits.min_order_usd,
                start_child_index=0,
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
    limits: RiskLimits = RiskLimits(),
    exclude_coins: set[str] | None = None,
    target_weights: dict[str, float] | None = None,
    assume_unwinds_fill: bool = False,
) -> dict[str, float]:
    """Signed USD notional each unquoted position carries into gross-leverage & neutrality budgets.

    Passive ``Alo`` unwind orders placed on an unquoted/empty book (``coin not in bbo``) rest
    passively and do NOT fill immediately, so ``keep_frac = 1.0`` until ``clearinghouseState``
    confirms the position has actually decreased (preventing ``gross_leverage`` breaches when
    sizing new quoted entries alongside unquoted orphans).
    """
    planned: dict[str, float] = {}
    if assume_unwinds_fill:
        for o in plan_orphan_unwind_orders(
            account,
            meta,
            bbo,
            book_tops,
            reference_px,
            limits=limits,
            exclude_coins=exclude_coins,
            target_weights=target_weights,
        ):
            planned[o.coin] = planned.get(o.coin, 0.0) + o.sz
    out: dict[str, float] = {}
    for coin, cur_sz in account.positions_sz.items():
        cur = float(cur_sz or 0.0)
        if abs(cur) <= 1e-12 or coin in bbo:
            continue
        ntl = float(account.position_notional_usd.get(coin, 0.0) or 0.0)
        keep_frac = max(0.0, 1.0 - planned.get(coin, 0.0) / abs(cur)) if assume_unwinds_fill else 1.0
        out[coin] = ntl * keep_frac
    return out


def load_logged_order_coins(audit_log_path: Path | None) -> set[str]:
    """Collect coin symbols recorded in a live MM JSONL audit log (skipping dry-run records)."""
    coins: set[str] = set()
    if audit_log_path is None or not audit_log_path.is_file():
        return coins
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
        c = str(rec.get("coin") or "").strip()
        if c:
            coins.add(c)
    return coins


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


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """PID- and thread-safe atomic JSON write with guaranteed cleanup on error."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def save_high_water_mark(path: Path, value: float, *, net_external_flow_usd: float | None = None) -> None:
    payload: dict[str, Any] = {}
    if path.is_file():
        try:
            prev = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(prev, dict):
                for k in ("pre_reanchor_hwm_usd", "reanchored_at_ms", "reanchored_streak_ms"):
                    if k in prev:
                        payload[k] = prev[k]
        except (OSError, ValueError, TypeError):
            pass
    payload["high_water_mark_usd"] = float(value)
    if net_external_flow_usd is not None and math.isfinite(net_external_flow_usd):
        payload["net_external_flow_usd"] = float(net_external_flow_usd)
    _atomic_write_json(path, payload)


def load_hwm_net_flow(path: Path) -> float | None:
    """Cumulative external USDC flow recorded alongside the high-water mark (``None`` if never stored)."""
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text())
        value = raw.get("net_external_flow_usd") if isinstance(raw, dict) else None
        return float(value) if value is not None and math.isfinite(float(value)) else None
    except (OSError, ValueError, TypeError):
        return None


def hwm_persist_allowed(cur_net_flow_usd: float | None, stored_net_flow_usd: float | None) -> bool:
    """Whether a freshly ratcheted HWM may be saved this cycle.

    While flow tracking is active (a baseline is stored) but the ledger is unreadable, a peak
    ratcheted now may already include a deposit the baseline has not seen; saving it would add the
    same deposit again on recovery (false drawdown -> kill-switch). Legacy files keep saving.
    """
    return not (cur_net_flow_usd is None and stored_net_flow_usd is not None)


def flow_adjusted_high_water_mark(
    stored_hwm: float,
    prev_net_flow_usd: float | None,
    cur_net_flow_usd: float | None,
    *,
    current_equity_usd: float | None = None,
) -> float:
    """Adjust the HWM for external capital moved since it was stored.

    When ``current_equity_usd`` is supplied and positive:
    * Deposits (``delta >= 0``) add dollar-for-dollar (``stored_hwm + delta``) so fresh capital is
      never treated as trading profit.
    * Withdrawals (``delta < 0``, ``W = -delta``) scale multiplicatively by
      ``current_equity_usd / (current_equity_usd + W)`` so percentage drawdown ``(H - E) / H``
      stays strictly invariant across arbitrarily large capital withdrawals.

    When ``current_equity_usd`` is omitted, falls back to ``max(0.0, stored_hwm + delta)``.
    """
    if stored_hwm <= 0 or prev_net_flow_usd is None or cur_net_flow_usd is None:
        return stored_hwm
    delta = cur_net_flow_usd - prev_net_flow_usd
    if not math.isfinite(delta):
        return stored_hwm
    if abs(delta) <= 1e-12:
        return stored_hwm
    if current_equity_usd is not None and math.isfinite(current_equity_usd) and current_equity_usd > 0:
        if delta >= 0.0:
            return max(0.0, stored_hwm + delta)
        withdrawal = -delta
        pre_withdrawal_eq = current_equity_usd + withdrawal
        if pre_withdrawal_eq > 0:
            return max(0.0, stored_hwm * (current_equity_usd / pre_withdrawal_eq))
    return max(0.0, stored_hwm + delta)


def _is_drawdown_flatten_record(rec: dict[str, Any]) -> bool:
    if rec.get("status") != "kill_switch_triggered" or rec.get("derisk_mode") != "flatten":
        return False
    violations = rec.get("violations")
    if not isinstance(violations, list) or not violations:
        return False
    return all(isinstance(v, str) and v.startswith("drawdown_limit:") for v in violations)


def reanchor_hwm_after_flatten(
    hwm_path: Path,
    audit_log_path: Path,
    *,
    stored_hwm: float,
    oracle_equity_usd: float,
    gross_notional_usd: float,
    now_ms: int,
    net_external_flow_usd: float | None = None,
    cooldown_hours: float = 4.0,
    persist: bool = True,
) -> tuple[float, bool]:
    """Re-anchor HWM to ``oracle_equity_usd`` after a completed drawdown flatten + cooldown.

    Once ``flatten`` has closed positions to flat (``gross_notional_usd <= max(5, 1% equity)``)
    and an unbroken ``drawdown_limit`` flatten streak has cooled down for ``>= cooldown_hours``,
    re-anchors the HWM to ``oracle_equity_usd`` at most once per ``streak_start_ms`` so the
    executor is not permanently wedged in ``kill_switch_triggered`` forever.
    """
    if (
        stored_hwm <= 0
        or not math.isfinite(oracle_equity_usd)
        or oracle_equity_usd <= 0
        or not math.isfinite(gross_notional_usd)
        or gross_notional_usd > max(5.0, 0.01 * oracle_equity_usd)
        or audit_log_path is None
        or not audit_log_path.is_file()
    ):
        return stored_hwm, False

    live_records: list[dict[str, Any]] = []
    try:
        lines = audit_log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return stored_hwm, False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if isinstance(rec, dict) and rec.get("mode") != "dry_run":
            live_records.append(rec)

    if not live_records or not _is_drawdown_flatten_record(live_records[-1]):
        return stored_hwm, False

    streak_start_ms: int | None = None
    for idx in range(len(live_records) - 1, -1, -1):
        rec = live_records[idx]
        if not _is_drawdown_flatten_record(rec):
            break
        ts_val = safe_int(rec.get("ts_ms"))
        if ts_val is not None and ts_val > 0:
            streak_start_ms = ts_val if streak_start_ms is None else min(streak_start_ms, ts_val)

    if streak_start_ms is None or (int(now_ms) - streak_start_ms) < float(cooldown_hours) * 3_600_000.0:
        return stored_hwm, False

    if hwm_path.is_file():
        try:
            existing = json.loads(hwm_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict) and safe_int(existing.get("reanchored_streak_ms")) == streak_start_ms:
                return stored_hwm, False
        except (OSError, ValueError, TypeError):
            pass

    new_hwm = float(oracle_equity_usd)
    if persist:
        payload: dict[str, Any] = {
            "high_water_mark_usd": new_hwm,
            "pre_reanchor_hwm_usd": float(stored_hwm),
            "reanchored_at_ms": int(now_ms),
            "reanchored_streak_ms": int(streak_start_ms),
        }
        if net_external_flow_usd is not None and math.isfinite(net_external_flow_usd):
            payload["net_external_flow_usd"] = float(net_external_flow_usd)
        _atomic_write_json(hwm_path, payload)
    return new_hwm, True


_CANCEL_GONE_MARKERS = ("already canceled", "already cancelled", "filled", "never placed")


def cancelled_orders_count(records: list[dict[str, Any]]) -> int:
    """Count individual open orders whose cancellation verifiably succeeded (or were already gone)."""
    total = 0
    for rec in records:
        if not isinstance(rec, dict) or rec.get("error"):
            continue
        reqs = rec.get("requests") if isinstance(rec.get("requests"), list) else None
        if rec.get("mode") == "dry_run":
            total += len(reqs) if reqs is not None else 1
            continue
        statuses, err_msg = extract_hl_order_statuses(rec.get("response"))
        if err_msg is not None:
            continue
        n_expected = len(reqs) if reqs is not None else 1
        for idx in range(n_expected):
            st = statuses[idx] if idx < len(statuses) else None
            if st == "success":
                total += 1
            elif isinstance(st, dict) and st.get("error"):
                if any(m in str(st.get("error")).lower() for m in _CANCEL_GONE_MARKERS):
                    total += 1
    return total


def cancel_failed_coins(records: list[dict[str, Any]], requested: list[dict[str, Any]]) -> set[str]:
    """Coins whose cancel request did not verifiably succeed.

    Parses Hyperliquid ``cancel`` / ``bulkCancel`` responses (``response.data.statuses`` aligned
    with the request order). An error saying the order is already gone (filled / cancelled) counts
    as success; any other error, a missing status, or a transport error counts as failure so the
    caller does not stack new exposure on top of an order that may still rest.
    """
    failed: set[str] = set()
    covered: set[str] = set()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        if rec.get("mode") == "dry_run":
            covered.add(str(rec.get("coin") or ""))
            continue
        reqs = rec.get("requests") if isinstance(rec.get("requests"), list) else [{"coin": rec.get("coin"), "oid": rec.get("oid")}]
        coins = [str((r or {}).get("coin") or "") for r in reqs]
        covered.update(coins)
        if rec.get("error"):
            failed.update(coins)
            continue
        statuses, err_msg = extract_hl_order_statuses(rec.get("response"))
        if err_msg is not None:
            failed.update(coins)
            continue
        for idx, coin in enumerate(coins):
            st = statuses[idx] if idx < len(statuses) else None
            if st == "success":
                continue
            if isinstance(st, dict) and st.get("error"):
                if any(m in str(st.get("error")).lower() for m in _CANCEL_GONE_MARKERS):
                    continue
            failed.add(coin)
    # Requested coins without any record (e.g. adapter returned nothing) are unverified too.
    failed.update(str(o.get("coin") or "") for o in requested if str(o.get("coin") or "") not in covered)
    failed.discard("")
    return failed


def oracle_band_blocked_coins(
    bbo: dict[str, tuple[float, float, int]], reference_px: dict[str, float] | None, limits: RiskLimits
) -> set[str]:
    """Coins whose book mid deviates from the oracle reference by more than ``max_entry_oracle_dev_pct``."""
    out: set[str] = set()
    if not reference_px or limits.max_entry_oracle_dev_pct <= 0:
        return out
    for coin, (bid, ask, _) in bbo.items():
        ref = reference_px.get(coin)
        mid = 0.5 * (bid + ask)
        if ref is None or not (math.isfinite(float(ref)) and float(ref) > 0) or mid <= 0:
            continue
        if abs(mid / float(ref) - 1.0) * 100.0 > limits.max_entry_oracle_dev_pct:
            out.add(coin)
    return out


def _accepted_notional_usd(orders: list[PlannedOrder], records: list[dict[str, Any]]) -> float:
    return round(sum(o.notional_usd for o, r in zip(orders, records, strict=False) if _order_record_succeeded(r)), 2)


def _order_record_succeeded(rec: Any) -> bool:
    """Return True when an adapter order record was accepted (resting or filled, not rejected/errored)."""
    if not isinstance(rec, dict):
        return False
    if rec.get("status") == "err" or rec.get("error"):
        return False
    resp = rec.get("response")
    if resp is None:
        return True
    statuses, err_msg = extract_hl_order_statuses(resp)
    if err_msg is not None:
        return False
    if any(isinstance(st, dict) and st.get("error") for st in statuses):
        return False
    return True


def _submit_alo_orders(adapter: Any, orders: list[PlannedOrder]) -> list[dict[str, Any]]:
    """Submit pure ALO batch via ``place_bulk_alo_orders`` when available, catching transport errors."""
    if not orders:
        return []
    if hasattr(adapter, "place_bulk_alo_orders"):
        try:
            resps = adapter.place_bulk_alo_orders(orders)
            if isinstance(resps, list) and len(resps) == len(orders):
                return resps
        except Exception as exc:
            err_str = f"{type(exc).__name__}: {exc}"
            return [
                {"order": o.to_dict(), "status": "err", "response": {"status": "err", "response": err_str}}
                for o in orders
            ]
    out: list[dict[str, Any]] = []
    for o in orders:
        try:
            out.append(adapter.place_alo_order(o))
        except Exception as exc:
            err_str = f"{type(exc).__name__}: {exc}"
            out.append({"order": o.to_dict(), "status": "err", "response": {"status": "err", "response": err_str}})
    return out


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
    ``foreign_coins`` (even if present in ``target_weights``) are left to their owning engine.

    Kill-switch behaviour (``summary["derisk_mode"]``):

    * hard data violations (non-finite snapshot, non-positive equity, stale feed) submit nothing;
    * a ``missing_quote`` feed gap halts BBO rebalancing but still unwinds orphan positions
      (reduce-only, oracle-priced) — ``orphan_unwind_only``;
    * account-risk violations (drawdown, free margin, gross, coin cap) cancel every open order and
      keep submitting reduce-only orders toward de-risked targets (``reduce_only``); a drawdown
      breach flattens every position instead (``flatten``, ``limits.flatten_on_drawdown``).
    """
    cycle_mode = "dry_run" if isinstance(adapter, DryRunAdapter) else "testnet_live"
    hwm = max(float(high_water_mark_usd or 0.0), 0.0) or account.equity_usd
    foreign_oids = {int(o) for o in (foreign_order_oids or set())}
    foreign_order_coins = {
        str(o.get("coin") or "")
        for o in account.open_orders
        if o.get("oid") is not None and safe_int(o.get("oid")) in foreign_oids
    }
    foreign = {c for c in (set(foreign_coins or ()) | foreign_order_coins) if c}
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
    cancelled: list[dict[str, Any]] = []
    if orders_to_cancel:
        try:
            cancelled = adapter.cancel_all_open_orders(orders_to_cancel) or []
        except Exception as exc:
            cancelled = [{"mode": cycle_mode, "error": f"{type(exc).__name__}: {exc}", "requests": orders_to_cancel}]
    # Never stack new exposure on a coin whose old orders may still rest, and never open exposure
    # into a book that is dislocated from the oracle; reduce-only orders remain allowed for both.
    cancel_failed = cancel_failed_coins(cancelled, orders_to_cancel) if orders_to_cancel else set()
    oracle_blocked = oracle_band_blocked_coins(bbo, reference_px, limits)
    entry_blocked = cancel_failed | oracle_blocked

    orphan_unwinds: list[PlannedOrder] = []
    if limits.unwind_unquoted_orphans and not hard_data:
        orphan_unwinds = plan_orphan_unwind_orders(
            account,
            meta,
            bbo,
            book_tops,
            reference_px,
            limits=limits,
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
                # 1) Clamp sign reversals to 0 so the closing leg executes without opening the new side.
                plan_targets = {
                    c: (0.0 if float(account.positions_sz.get(c, 0.0) or 0.0) * float(w) < 0 else float(w))
                    for c, w in target_weights.items()
                }
                # 2) Enforce single-coin cap so a coin_cap violation trims down to max_single_coin_weight.
                if limits.max_single_coin_weight > 0:
                    plan_targets = {
                        c: math.copysign(min(abs(w), limits.max_single_coin_weight), w)
                        for c, w in plan_targets.items()
                    }
                # 3) On gross_leverage breach, scale quoted targets so total gross <= 0.95 * max_gross_leverage.
                perp_eq_derisk = effective_perp_equity_usd(account)
                if any(v.startswith("gross_leverage") for v in violations) and perp_eq_derisk > 0:
                    fixed_gross_w = sum(
                        abs(float(account.position_notional_usd.get(c, 0.0) or 0.0)) / perp_eq_derisk
                        for c, sz in account.positions_sz.items()
                        if abs(float(sz or 0.0)) > 1e-12 and (c not in bbo or c in foreign)
                    )
                    avail_gross = max(0.0, limits.max_gross_leverage * 0.95 - fixed_gross_w)
                    quoted_gross = sum(
                        abs(w) for c, w in plan_targets.items() if c in bbo and c not in foreign
                    )
                    if quoted_gross > avail_gross and quoted_gross > 1e-9:
                        g_scale = avail_gross / quoted_gross
                        plan_targets = {c: w * g_scale for c, w in plan_targets.items()}
                # 4) On low_free_margin breach, scale targets down so held==target positions still shed margin.
                if any(v.startswith("low_free_margin") for v in violations) and perp_eq_derisk > 0:
                    free_pct = (account.free_margin_usd / perp_eq_derisk) * 100.0
                    m_scale = (
                        0.0
                        if free_pct <= 0.0
                        else min(0.5, max(0.0, 1.0 - (limits.min_free_margin_pct - free_pct) / 50.0))
                    )
                    plan_targets = {c: w * m_scale for c, w in plan_targets.items()}
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
        derisk_orders = [o for o in derisk_orders if o.reduce_only or o.coin not in entry_blocked]
        placed = _submit_alo_orders(adapter, derisk_orders)
        ok_count = sum(1 for r in placed if _order_record_succeeded(r))
        rejected_count = len(placed) - ok_count
        summary = {
            "mode": cycle_mode,
            "status": "kill_switch_triggered",
            "violations": violations,
            "derisk_mode": derisk_mode,
            "cancelled_count": cancelled_orders_count(cancelled),
            "placed_count": ok_count,
            "rejected_count": rejected_count,
            "total_notional_usd": _accepted_notional_usd(derisk_orders, placed),
            "orphan_unwind_count": len(orphan_unwinds),
            "cancel_failed_coins": sorted(cancel_failed),
            "oracle_band_blocked_coins": sorted(oracle_blocked),
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
        planned_non_reduce_coins = {
            o.coin for o in planned if not o.reduce_only and o.coin in meta
        }
        held_mismatched_coins: set[str] = set()
        for coin, cur_sz in account.positions_sz.items():
            if abs(float(cur_sz or 0.0)) <= 1e-12 or coin in foreign or coin not in meta:
                continue
            spec = meta[coin]
            if spec.only_isolated and not limits.allow_only_isolated:
                continue
            cur_mode, cur_lev = account.position_leverage.get(coin, ("", 0))
            if not cur_mode:
                continue
            is_cross = not spec.only_isolated
            expected_mode = "cross" if is_cross else "isolated"
            target_lev = max(
                1,
                min(spec.max_leverage, limits.default_cross_leverage if is_cross else limits.default_isolated_leverage),
            )
            if cur_mode != expected_mode or cur_lev != target_lev:
                held_mismatched_coins.add(coin)

        # Preserve planned order sequence first, then normalize any held managed positions with stale leverage.
        lev_candidates: list[str] = []
        seen_lev_coins: set[str] = set()
        for o in planned:
            if not o.reduce_only and o.coin in meta and o.coin not in seen_lev_coins:
                seen_lev_coins.add(o.coin)
                lev_candidates.append(o.coin)
        for coin in sorted(held_mismatched_coins):
            if coin not in seen_lev_coins:
                seen_lev_coins.add(coin)
                lev_candidates.append(coin)

        for coin in lev_candidates:
            spec = meta[coin]
            is_cross = not spec.only_isolated
            target_lev = max(1, min(spec.max_leverage, limits.default_cross_leverage if is_cross else limits.default_isolated_leverage))
            cur_mode, cur_lev = account.position_leverage.get(coin, ("", 0))
            expected_mode = "cross" if is_cross else "isolated"
            if cur_mode == expected_mode and cur_lev == target_lev:
                continue
            try:
                upd = adapter.set_leverage(coin, target_lev, is_cross=is_cross)
            except Exception as exc:
                upd = {"coin": coin, "leverage": target_lev, "is_cross": is_cross, "error": str(exc)}
            leverage_updates.append(upd)
            if not leverage_update_ok(upd) and coin in planned_non_reduce_coins:
                # Never open exposure at an unknown (exchange default / max) leverage.
                (blocked_isolated_coins if spec.only_isolated else blocked_leverage_coins).add(coin)

        blocked = blocked_isolated_coins | blocked_leverage_coins | entry_blocked
        surviving = [o for o in planned if o.reduce_only or o.coin not in blocked]

        # EXEC-10: When oracle_blocked / leverage / funding-trap drops non-reduce-only entry orders
        # on one side of a two-sided rebalance, scale down opposing non-reduce-only entry legs
        # proportionally so we never open large unhedged directional beta.
        unblocked_baseline = plan_rebalance_orders(
            account,
            target_weights,
            meta,
            bbo,
            limits,
            funding_apr_by_coin=None,
            now_ms=0,
            exclude_coins=foreign,
        )
        asymmetry_dropped_coins = blocked_isolated_coins | blocked_leverage_coins | oracle_blocked | (
            {o.coin for o in unblocked_baseline if not o.reduce_only}
            - {o.coin for o in planned if not o.reduce_only}
        )
        if asymmetry_dropped_coins:
            def _order_mid_ntl(order: PlannedOrder) -> float:
                mid_p = 0.5 * (bbo[order.coin][0] + bbo[order.coin][1]) if order.coin in bbo else order.limit_px
                return order.sz * mid_p

            intended_buy_ntl = sum(_order_mid_ntl(o) for o in unblocked_baseline if not o.reduce_only and o.side == "buy")
            intended_sell_ntl = sum(_order_mid_ntl(o) for o in unblocked_baseline if not o.reduce_only and o.side == "sell")
            if intended_buy_ntl > 0.0 and intended_sell_ntl > 0.0:
                surv_buy_ntl = sum(_order_mid_ntl(o) for o in surviving if not o.reduce_only and o.side == "buy")
                surv_sell_ntl = sum(_order_mid_ntl(o) for o in surviving if not o.reduce_only and o.side == "sell")
                r_buy = surv_buy_ntl / intended_buy_ntl
                r_sell = surv_sell_ntl / intended_sell_ntl
                scale_side: str | None = None
                side_scale = 1.0
                if r_buy + 1e-6 < r_sell and r_sell > 0:
                    scale_side = "sell"
                    side_scale = max(0.0, r_buy / r_sell)
                elif r_sell + 1e-6 < r_buy and r_buy > 0:
                    scale_side = "buy"
                    side_scale = max(0.0, r_sell / r_buy)
                if scale_side is not None and side_scale < 1.0 - 1e-6:
                    coin_totals: dict[str, tuple[float, float]] = {}
                    coin_order: list[str] = []
                    for o in surviving:
                        if o.reduce_only or o.side != scale_side:
                            continue
                        if o.coin not in coin_totals:
                            coin_order.append(o.coin)
                            coin_totals[o.coin] = (o.sz, o.limit_px)
                        else:
                            prev_sz, lpx = coin_totals[o.coin]
                            coin_totals[o.coin] = (prev_sz + o.sz, lpx)
                    resliced_by_coin: dict[str, list[PlannedOrder]] = {}
                    if side_scale > 1e-9:
                        for c_name in coin_order:
                            if c_name not in meta:
                                continue
                            tot_sz, lpx = coin_totals[c_name]
                            mid_px = 0.5 * (bbo[c_name][0] + bbo[c_name][1]) if c_name in bbo else lpx
                            resliced_by_coin[c_name] = _emit_sliced_alo_leg(
                                c_name,
                                scale_side,
                                tot_sz * side_scale,
                                lpx,
                                meta[c_name].sz_decimals,
                                reduce_only=False,
                                is_full_close=False,
                                max_child_usd=limits.max_child_usd,
                                min_order_usd=limits.min_order_usd,
                                start_child_index=0,
                                ref_px=mid_px,
                            )
                    emitted_scaled_coins: set[str] = set()
                    rebalanced_surviving: list[PlannedOrder] = []
                    for o in surviving:
                        if o.reduce_only or o.side != scale_side:
                            rebalanced_surviving.append(o)
                            continue
                        if o.coin not in emitted_scaled_coins:
                            emitted_scaled_coins.add(o.coin)
                            rebalanced_surviving.extend(resliced_by_coin.get(o.coin, []))
                    surviving = rebalanced_surviving

        executable = surviving + orphan_unwinds
        placed = _submit_alo_orders(adapter, executable)
        ok_count = sum(1 for r in placed if _order_record_succeeded(r))
        rejected_count = len(placed) - ok_count
        cycle_status = "order_rejected" if (executable and ok_count == 0 and rejected_count > 0) else "ok"
        summary = {
            "mode": cycle_mode,
            "status": cycle_status,
            "violations": [],
            "cancelled_count": cancelled_orders_count(cancelled),
            "placed_count": ok_count,
            "rejected_count": rejected_count,
            "total_notional_usd": _accepted_notional_usd(executable, placed),
            "orphan_unwind_count": len(orphan_unwinds),
            "cancel_failed_coins": sorted(cancel_failed),
            "oracle_band_blocked_coins": sorted(oracle_blocked),
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
        if len(clean) == 1:
            if max_gross < 1e-4:
                return {}
            c, w = next(iter(clean.items()))
            cap = min(max_gross, max_single_coin_weight) if max_single_coin_weight > 0 else max_gross
            if abs(w) > cap:
                w = math.copysign(cap, w)
            return {c: round(w, 6)} if abs(w) >= 1e-4 else {}
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
