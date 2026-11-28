"""Avellaneda–Stoikov (2008) Market Making engine for Hyperliquid Testnet.

Implements a scale-invariant (bps-space) Avellaneda–Stoikov / GLFT quoting model with:
* Microstructure fair price (top-of-book microprice + top-5 Order Book Imbalance tilt);
* Online EWMA realized-volatility estimation (per-second variance scaled to horizon tau);
* Reservation price inventory skew + asymmetric quote sizing + hard inventory caps;
* Maker-fee floor + post-only BBO clamping (pure ``ALO``, zero taker crossing);
* Requote deadband to preserve Hyperliquid queue priority and limit cancel/replace churn;
* Adverse-selection (post-fill markout) widening & volatility/drawdown kill-switches;
* Both offline L2 replay simulator and live Hyperliquid Testnet quoting loop (hard-pinned
  to ``https://api.hyperliquid-testnet.xyz`` with guaranteed ``finally:`` order cleanup).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx
import numpy as np
import pandas as pd

from hl_quant.hl_microstructure import TESTNET_WS_URL
from hl_quant.hl_testnet_executor import (
    TESTNET_API_URL,
    TESTNET_INFO_URL,
    AssetSpec,
    DryRunAdapter,
    ExchangeAdapter,
    PlannedOrder,
    extract_hl_order_statuses,
    fetch_testnet_meta,
    round_hl_price,
    round_hl_size,
)


# --------------------------------------------------------------------------- Data models


@dataclass(frozen=True, slots=True)
class ASParams:
    """Scale-invariant Avellaneda–Stoikov parameters (prices/vol in basis points)."""

    gamma: float = 0.03  # Inventory risk aversion (1/bp)
    kappa: float = 0.35  # Arrival intensity decay (1/bp); small gamma -> spread ~ 2/kappa = 5.7 bps
    horizon_sec: float = 30.0  # Effective inventory holding horizon tau (seconds)
    default_sigma_bps: float = 10.0  # Cold-start horizon volatility before EWMA warms up
    vol_half_life_sec: float = 30.0  # EWMA half-life for realized volatility
    min_sigma_bps: float = 4.0  # Floor on effective horizon volatility
    max_sigma_bps: float = 40.0  # Cap on volatility plugged into spread formula
    vol_halt_sigma_bps: float = 65.0  # Hard circuit breaker: halt quoting above this horizon vol
    obi_tilt_bps: float = 2.0  # Microstructure fair-price tilt from top-5 OBI in [-1, 1]
    maker_fee_bps: float = 1.5  # Hyperliquid Tier-0 perp maker fee
    min_edge_bps: float = 1.0  # Minimum required gross edge per side above maker fee
    min_half_spread_bps: float = 2.5  # Absolute floor on half-spread (bps)
    max_half_spread_bps: float = 60.0  # Ceiling on half-spread (bps)
    clamp_to_bbo: bool = True  # Enforce bid <= best_bid and ask >= best_ask for strict ALO
    base_order_usd: float = 30.0  # Base quote size per side in USD
    max_inventory_usd: float = 100.0  # Hard per-coin net inventory cap in USD
    min_order_usd: float = 15.0  # Minimum notional per order (HL min ~$10)
    requote_threshold_bps: float = 1.0  # Deadband to preserve queue priority
    max_book_spread_bps: float = 25.0  # Halt quoting if L2 book spread exceeds this
    max_session_drawdown_usd: float = 10.0  # Hard session PnL stop-loss
    adverse_markout_widen_bps: float = -8.0  # Widen spreads 1.5x if rolling markout falls below this
    adverse_markout_halt_bps: float = -20.0  # Halt quoting if rolling markout collapses below this
    lead_beta: float = 0.60  # Cross-exchange CEX lead-lag weight into fair price
    lead_pull_bps: float = 2.0  # Pull exposed side immediately if CEX jumps >= this many bps vs basis
    max_lead_shift_bps: float = 15.0  # Cap on lead-lag fair-price shift
    portfolio_beta_coupling: float = 0.25  # Cross-asset inventory beta spill in multi-coin MM
    funding_overlay_apr_threshold: float = 25.0  # Trigger :59->:05 quote skew when |funding APR| >= 25%
    funding_overlay_skew_bps: float = 2.5  # Bearish fair-price tilt (bps) across :59->:05 settlement window
    vpin_window_sec: float = 15.0  # Rolling trade-flow toxicity window (seconds)
    vpin_min_volume_usd: float = 2_500.0  # Minimum notional flow before VPIN widens spreads
    vpin_widen_threshold: float = 0.75  # |V_buy - V_sell| / (V_buy + V_sell) trigger for 1.35x toxic spread + explicit cancel
    api_budget_free_buffer_reqs: int = 40  # Initial free requests before adaptive rate-limit throttle engages

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class L2BookSnapshot:
    coin: str
    observed_ms: int
    best_bid: float
    best_ask: float
    bid_sz1: float
    ask_sz1: float
    imbalance5: float
    mid_px: float
    microprice: float
    book_spread_bps: float


@dataclass(frozen=True, slots=True)
class MMQuoteDecision:
    coin: str
    mid_px: float
    fair_px: float
    reservation_px: float
    sigma_tau_bps: float
    q_norm: float
    skew_bps: float
    half_spread_bps: float
    bid_px: float | None
    bid_sz: float
    ask_px: float | None
    ask_sz: float
    bid_reduce_only: bool = False
    ask_reduce_only: bool = False
    halt_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class MMSessionSummary:
    mode: str
    coin: str
    started_ms: int
    ended_ms: int
    ticks_observed: int
    quotes_placed: int
    quotes_cancelled: int
    requotes_skipped_deadband: int
    fills_count: int
    buy_fills: int
    sell_fills: int
    volume_usd: float
    maker_share_pct: float
    start_inventory_sz: float
    end_inventory_sz: float
    max_abs_inventory_usd: float
    avg_half_spread_bps: float
    avg_sigma_tau_bps: float
    realized_spread_pnl_usd: float
    inventory_mtm_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    vw_markout_5s_bps: float | None
    vw_markout_30s_bps: float | None
    halt_events: list[str]
    final_cleanup_cancelled: int
    flattened_exit_sz: float = 0.0
    bbo_ticks_observed: int = 0
    quotes_modified: int = 0
    lead_pulls: int = 0
    dead_man_refreshes: int = 0
    urgent_cancels: int = 0
    api_requests_per_usd_vol: float = 0.0
    vpin_widen_events: int = 0
    rate_limit_throttles: int = 0
    mempool_priority_eligible: bool = True
    # True when shutdown could not confirm every MM order was cancelled, so the exchange-side
    # Dead Man's Switch was intentionally left armed to sweep the leftovers.
    dms_left_armed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class ArrivalCalibrationResult:
    """Empirical Avellaneda–Stoikov arrival-intensity fit ``lambda(delta) = A * exp(-kappa * delta)``."""

    coin: str
    duration_sec: float
    l2_ticks: int
    trades_count: int
    empirical_sigma_1s_bps: float
    empirical_sigma_tau_bps: float
    horizon_sec: float
    median_book_spread_bps: float
    kappa_per_bp: float
    arrival_a_per_sec: float
    r2_fit: float
    implied_min_half_spread_bps: float
    depth_curve: list[dict[str, float]]
    # 1/kappa alone (adverse-selection term) — the old ``implied_min_half_spread_bps``.
    adverse_term_bps: float = 0.0
    maker_fee_bps: float = 0.0
    n_windows: int = 0
    fit_points: int = 0
    observed_at_ms: int = 0
    # False when the sample is too short / too thin for the kappa fit to be trusted.
    calibration_ok: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- Microstructure & EWMA vol


def parse_l2_snapshot(coin: str, book: dict[str, Any], *, now_ms: int | None = None) -> L2BookSnapshot | None:
    """Parse Hyperliquid REST ``l2Book`` JSON into an ``L2BookSnapshot`` with microprice & OBI5."""
    levels = book.get("levels") if isinstance(book, dict) else None
    if not (isinstance(levels, list) and len(levels) == 2 and levels[0] and levels[1]):
        return None
    bids, asks = levels[0][:5], levels[1][:5]
    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    if not (math.isfinite(best_bid) and math.isfinite(best_ask) and best_bid > 0 and best_ask >= best_bid):
        return None
    bid_sz1 = max(0.0, float(bids[0]["sz"]))
    ask_sz1 = max(0.0, float(asks[0]["sz"]))
    mid = 0.5 * (best_bid + best_ask)
    tot1 = bid_sz1 + ask_sz1
    # Standard Stoikov microprice: weighted toward the thinner side
    microprice = (best_bid * ask_sz1 + best_ask * bid_sz1) / tot1 if tot1 > 0 else mid
    bid_usd5 = sum(float(lv["px"]) * float(lv["sz"]) for lv in bids)
    ask_usd5 = sum(float(lv["px"]) * float(lv["sz"]) for lv in asks)
    tot5 = bid_usd5 + ask_usd5
    imb5 = (bid_usd5 - ask_usd5) / tot5 if tot5 > 0 else 0.0
    book_ms = book.get("time")
    obs_ms = int(book_ms) if isinstance(book_ms, (int, float)) and book_ms > 0 else int(now_ms or time.time() * 1000)
    spread_bps = (best_ask - best_bid) / mid * 1e4
    return L2BookSnapshot(
        coin=coin,
        observed_ms=obs_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_sz1=bid_sz1,
        ask_sz1=ask_sz1,
        imbalance5=imb5,
        mid_px=mid,
        microprice=microprice,
        book_spread_bps=spread_bps,
    )


def fetch_testnet_l2_snapshot(client: httpx.Client, coin: str) -> L2BookSnapshot | None:
    resp = client.post(TESTNET_INFO_URL, json={"type": "l2Book", "coin": coin})
    resp.raise_for_status()
    return parse_l2_snapshot(coin, resp.json() or {}, now_ms=int(time.time() * 1000))


def parse_bbo_snapshot(
    coin: str,
    data: dict[str, Any],
    *,
    prev_imbalance5: float = 0.0,
    now_ms: int | None = None,
) -> L2BookSnapshot | None:
    """Parse Hyperliquid sub-second ``bbo`` WS payload into an ``L2BookSnapshot``."""
    bbo = data.get("bbo") if isinstance(data, dict) else None
    if not (isinstance(bbo, list) and len(bbo) == 2 and isinstance(bbo[0], dict) and isinstance(bbo[1], dict)):
        return None
    best_bid = float(bbo[0]["px"])
    best_ask = float(bbo[1]["px"])
    if not (math.isfinite(best_bid) and math.isfinite(best_ask) and best_bid > 0 and best_ask >= best_bid):
        return None
    bid_sz1 = max(0.0, float(bbo[0].get("sz", 0.0)))
    ask_sz1 = max(0.0, float(bbo[1].get("sz", 0.0)))
    mid = 0.5 * (best_bid + best_ask)
    tot1 = bid_sz1 + ask_sz1
    microprice = (best_bid * ask_sz1 + best_ask * bid_sz1) / tot1 if tot1 > 0 else mid
    book_ms = data.get("time")
    obs_ms = int(book_ms) if isinstance(book_ms, (int, float)) and book_ms > 0 else int(now_ms or time.time() * 1000)
    return L2BookSnapshot(
        coin=coin,
        observed_ms=obs_ms,
        best_bid=best_bid,
        best_ask=best_ask,
        bid_sz1=bid_sz1,
        ask_sz1=ask_sz1,
        imbalance5=float(prev_imbalance5),
        mid_px=mid,
        microprice=microprice,
        book_spread_bps=(best_ask - best_bid) / mid * 1e4,
    )


class EWMAVolatilityEstimator:
    """Online time-aware EWMA realized volatility estimator (returns horizon sigma in bps)."""

    def __init__(self, *, horizon_sec: float = 60.0, half_life_sec: float = 30.0, default_sigma_bps: float = 10.0) -> None:
        self.horizon_sec = max(1.0, float(horizon_sec))
        self.half_life_sec = max(1.0, float(half_life_sec))
        self.default_sigma_bps = max(0.5, float(default_sigma_bps))
        self._last_ms: int | None = None
        self._last_mid: float | None = None
        # Initialize per-second variance from default horizon sigma
        self._var_per_sec: float = (self.default_sigma_bps**2) / self.horizon_sec
        self._obs_count: int = 0

    def update(self, ts_ms: int, mid_px: float) -> float:
        if not math.isfinite(mid_px) or mid_px <= 0:
            return self.sigma_tau_bps
        t_cur = int(ts_ms)
        if self._last_ms is not None and t_cur <= self._last_ms:
            return self.sigma_tau_bps
        if self._last_ms is not None and self._last_mid is not None and self._last_mid > 0:
            dt_sec = (t_cur - self._last_ms) / 1000.0
            if mid_px == self._last_mid and dt_sec < 1.0:
                # High-frequency depth/size ticks with unchanged mid must not decay variance
                # or chop the elapsed time before the next true mid-price change.
                return self.sigma_tau_bps
            ret_bps = math.log(mid_px / self._last_mid) * 1e4
            # Winsorize extreme single-tick outliers at 200 bps / sqrt(s)
            inst_var = min((ret_bps**2) / max(dt_sec, 0.05), 40_000.0)
            alpha = 1.0 - math.exp(-math.log(2.0) * dt_sec / self.half_life_sec)
            alpha = min(max(alpha, 0.0), 0.50)
            self._var_per_sec = (1.0 - alpha) * self._var_per_sec + alpha * inst_var
            self._obs_count += 1
        self._last_ms = t_cur
        self._last_mid = float(mid_px)
        return self.sigma_tau_bps

    @property
    def sigma_tau_bps(self) -> float:
        est = math.sqrt(max(self._var_per_sec, 1e-8) * self.horizon_sec)
        # Smooth cold-start blend for the first 5 ticks
        w = min(1.0, self._obs_count / 5.0)
        return w * est + (1.0 - w) * self.default_sigma_bps


class LeadLagEstimator:
    """Tracks cross-exchange CEX (Binance Futures) lead-lag impulse net of slow structural basis."""

    def __init__(self, *, basis_half_life_sec: float = 45.0, max_cex_age_ms: int = 5_000) -> None:
        self.basis_half_life_sec = max(5.0, float(basis_half_life_sec))
        self.max_cex_age_ms = int(max_cex_age_ms)
        self._cex_mid: float = 0.0
        self._cex_ms: int = 0
        self._basis_bps: float | None = None
        self._last_update_ms: int | None = None

    def update_cex(self, ts_ms: int, cex_mid: float) -> None:
        if math.isfinite(cex_mid) and cex_mid > 0:
            self._cex_mid = float(cex_mid)
            self._cex_ms = int(ts_ms)

    def impulse_bps(self, ts_ms: int, hl_mid: float) -> float:
        """Return instantaneous CEX-minus-HL move (bps) net of slow EWMA cross-venue basis."""
        if self._cex_mid <= 0 or hl_mid <= 0 or (ts_ms - self._cex_ms) > self.max_cex_age_ms:
            return 0.0
        raw_bps = (self._cex_mid - hl_mid) / hl_mid * 1e4
        # Ignore insane stale/mismatched symbols (> 5%)
        if abs(raw_bps) > 500.0:
            return 0.0
        if self._basis_bps is None:
            self._basis_bps = raw_bps
            self._last_update_ms = int(ts_ms)
            return 0.0
        impulse = raw_bps - self._basis_bps
        dt_sec = max(0.0, (ts_ms - (self._last_update_ms if self._last_update_ms is not None else ts_ms)) / 1000.0)
        alpha = 1.0 - math.exp(-math.log(2.0) * dt_sec / self.basis_half_life_sec)
        self._basis_bps = (1.0 - alpha) * self._basis_bps + alpha * raw_bps
        self._last_update_ms = int(ts_ms)
        return impulse


class OrderFlowToxicityEstimator:
    """Rolling trade-flow imbalance (VPIN proxy) over public ``trades`` stream.

    Detects one-sided aggressive sweeps before fills accumulate adverse markout:
    ``vpin = |V_buy - V_sell| / (V_buy + V_sell)`` in ``[0, 1]`` once rolling notional
    reaches ``min_volume_usd``.
    """

    def __init__(self, *, window_sec: float = 15.0, min_volume_usd: float = 2_500.0) -> None:
        self.window_ms = max(1_000, int(round(window_sec * 1000.0)))
        self.min_volume_usd = max(1.0, float(min_volume_usd))
        self._events: list[tuple[int, float, float]] = []  # (ts_ms, buy_usd, sell_usd)

    def _prune(self, now_ms: int) -> None:
        cutoff = now_ms - self.window_ms
        if self._events and self._events[0][0] < cutoff:
            self._events = [e for e in self._events if e[0] >= cutoff]

    def update(self, ts_ms: int, side: str, notional_usd: float) -> None:
        if not math.isfinite(notional_usd) or notional_usd <= 0:
            return
        s = str(side).upper()
        if s in {"B", "BUY"}:
            self._events.append((int(ts_ms), float(notional_usd), 0.0))
        elif s in {"A", "S", "SELL"}:
            self._events.append((int(ts_ms), 0.0, float(notional_usd)))
        self._prune(int(ts_ms))

    def vpin(self, now_ms: int) -> float:
        self._prune(int(now_ms))
        buy_v = sum(e[1] for e in self._events)
        sell_v = sum(e[2] for e in self._events)
        tot = buy_v + sell_v
        if tot < self.min_volume_usd:
            return 0.0
        return abs(buy_v - sell_v) / tot


def compute_adaptive_action_interval_ms(
    base_interval_ms: int,
    api_reqs: int,
    volume_usd: float,
    *,
    free_buffer_reqs: int = 40,
    is_growth_mode: bool = False,
) -> int:
    """Dynamically slow calm requotes as address API usage approaches HyperCore's rate-limit budget.

    Standard HyperCore markets grant ``1 request per 1 USDC`` traded; HIP-3 **Growth Mode** applies
    a ``-90%`` fee/volume scale so every ``$1`` traded contributes only ``0.1`` request quota
    (effective ``1 req / 10 USDC``).
    """
    base = max(50, int(base_interval_ms))
    if api_reqs <= free_buffer_reqs:
        return base
    vol_weight = 0.1 if is_growth_mode else 1.0
    effective_vol = max(float(volume_usd) * vol_weight, 1.0)
    ratio = (api_reqs - free_buffer_reqs) / effective_vol
    if ratio >= 1.2:
        return base * 8
    if ratio >= 0.9:
        return base * 4
    if ratio >= 0.6:
        return base * 2
    return base


# --------------------------------------------------------------------------- Avellaneda–Stoikov core


def compute_funding_settlement_overlay_bps(
    ts_ms: int,
    funding_apr_pct: float,
    *,
    apr_threshold: float = 25.0,
    skew_bps: float = 2.5,
) -> float:
    """Return fair-price tilt (bps) during Hyperliquid's ``:59 -> :05`` hourly funding window.

    Based on the 17-day / 17,874-event OOS study (``hl_funding_settlement_study.py``):
    extreme negative funding (``APR <= -25%``) exhibits a statistically significant ``-2.3..-3.8 bps``
    post-``:00`` harvester unwind drop, and overheated positive funding (``APR >= +25%``) leans
    bearish across settlement. Shifting MM quotes downward during ``:59 -> :05`` captures this
    execution-timing edge with zero additional fees.
    """
    if skew_bps <= 0 or not math.isfinite(funding_apr_pct):
        return 0.0
    sec_in_hour = (int(ts_ms) // 1000) % 3600
    in_window = (sec_in_hour >= 3540) or (sec_in_hour < 300)  # :59:00 .. :04:59 UTC
    if not in_window:
        return 0.0
    if funding_apr_pct <= -abs(apr_threshold) or funding_apr_pct >= abs(apr_threshold):
        return -abs(skew_bps)
    return 0.0


def fetch_open_orders_checked(http: httpx.Client, account_address: str) -> list[dict[str, Any]] | None:
    """``openOrders`` for ``account_address``, or ``None`` when the response is not a valid list.

    Callers must skip order-tracking reconciliation on ``None``: treating an error payload (dict /
    null / HTTP error) as "no live orders" would drop every tracked quote and re-place duplicates.
    """
    try:
        resp = http.post(TESTNET_INFO_URL, json={"type": "openOrders", "user": account_address})
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    return [o for o in payload if isinstance(o, dict)]


def fetch_user_fills_checked(http: httpx.Client, account_address: str) -> list[dict[str, Any]] | None:
    """``userFills`` for ``account_address``, or ``None`` when the response is not a valid HTTP 200 list."""
    try:
        resp = http.post(TESTNET_INFO_URL, json={"type": "userFills", "user": account_address})
        resp.raise_for_status()
        payload = resp.json()
    except Exception:
        return None
    if not isinstance(payload, list):
        return None
    return [fl for fl in payload if isinstance(fl, dict)]


def fetch_clearinghouse_state_checked(
    http: httpx.Client,
    account_address: str,
    *,
    retries: int = 3,
    backoff_sec: float = 0.05,
) -> dict[str, Any] | None:
    """Fetch verified ``clearinghouseState`` (requiring HTTP 200 and ``assetPositions`` list)."""
    attempts = max(1, int(retries))
    for attempt in range(attempts):
        try:
            resp = http.post(TESTNET_INFO_URL, json={"type": "clearinghouseState", "user": account_address})
            if resp.status_code == 200:
                payload = resp.json()
                if isinstance(payload, dict) and isinstance(payload.get("assetPositions"), list):
                    return payload
        except Exception:
            pass
        if attempt + 1 < attempts and backoff_sec > 0:
            time.sleep(backoff_sec)
    return None


def _is_ambiguous_order_status(status: str) -> bool:
    """True when an order response did not carry a definite acceptance OID or explicit exchange error."""
    s = str(status or "").strip()
    return (
        not s
        or s in {"unknown", "invalid_response", "err"}
        or s.startswith("ambiguous_exception")
    )


def _adopt_matching_open_orders_for_cleanup(
    coin: str,
    ambiguous: list[dict[str, Any]],
    oo: list[dict[str, Any]],
    all_mm_oids: set[int],
) -> None:
    """Ensure any untracked resting order from an ambiguous submission is added to ``all_mm_oids``."""
    if not ambiguous or not oo:
        return
    for o in oo:
        if not isinstance(o, dict) or str(o.get("coin") or "") != coin or o.get("oid") is None:
            continue
        try:
            oid = int(o["oid"])
        except (TypeError, ValueError):
            continue
        if oid in all_mm_oids:
            continue
        raw_side = str(o.get("side") or "").upper()
        o_side = "buy" if raw_side in {"B", "BUY"} else ("sell" if raw_side in {"A", "S", "SELL"} else "")
        try:
            o_px = float(o.get("limitPx", o.get("px", o.get("limit_px", 0.0))) or 0.0)
            o_sz = float(o.get("sz", o.get("origSz", 0.0)) or 0.0)
        except (TypeError, ValueError):
            continue
        o_ts = o.get("timestamp")
        for sub in ambiguous:
            if o_side != sub["side"]:
                continue
            if isinstance(o_ts, (int, float)) and o_ts > 0 and int(o_ts) < int(sub["ts_ms"]) - 5_000:
                continue
            if abs(o_px - sub["px"]) <= max(1e-8, 1e-5 * sub["px"]) and 0 < o_sz <= sub["sz"] * (1.0 + 1e-4):
                all_mm_oids.add(oid)
                break


def _reconcile_ambiguous_submissions(
    coin: str,
    ambiguous: list[dict[str, Any]],
    *,
    oo: list[dict[str, Any]],
    uf: list[dict[str, Any]],
    active_orders: dict[str, dict[str, Any]],
    all_mm_oids: set[int],
    started_ms: int,
) -> int:
    """Reconcile timed-out/ambiguous ALO placements against verified ``openOrders`` and ``userFills``."""
    if not ambiguous:
        return 0
    adopted = 0
    for sub in ambiguous:
        side_k = str(sub["side"])
        sub_px = float(sub["px"])
        sub_sz = float(sub["sz"])
        sub_ts = int(sub["ts_ms"])
        matched_oid: int | None = None
        for o in oo:
            if not isinstance(o, dict) or str(o.get("coin") or "") != coin or o.get("oid") is None:
                continue
            try:
                oid = int(o["oid"])
            except (TypeError, ValueError):
                continue
            if oid in all_mm_oids:
                continue
            raw_side = str(o.get("side") or "").upper()
            o_side = "buy" if raw_side in {"B", "BUY"} else ("sell" if raw_side in {"A", "S", "SELL"} else "")
            if o_side != side_k:
                continue
            o_ts = o.get("timestamp")
            if isinstance(o_ts, (int, float)) and o_ts > 0 and int(o_ts) < sub_ts - 5_000:
                continue
            try:
                o_px = float(o.get("limitPx", o.get("px", o.get("limit_px", 0.0))) or 0.0)
                o_sz = float(o.get("sz", o.get("origSz", 0.0)) or 0.0)
                o_orig = float(o.get("origSz", o_sz) or o_sz)
            except (TypeError, ValueError):
                continue
            if (
                abs(o_px - sub_px) <= max(1e-8, 1e-5 * sub_px)
                and 0 < o_sz <= sub_sz * (1.0 + 1e-4)
                and o_orig <= sub_sz * (1.0 + 1e-4)
            ):
                matched_oid = oid
                all_mm_oids.add(oid)
                active_orders[side_k] = {
                    "oid": oid,
                    "px": o_px,
                    "sz": o_sz,
                    "reduce_only": bool(o.get("reduceOnly", sub["reduce_only"])),
                }
                adopted += 1
                break
        if matched_oid is not None:
            continue
        min_fill_ms = max(started_ms - 5_000, sub_ts - 2_000)
        for fl in uf:
            if not isinstance(fl, dict) or str(fl.get("coin") or "") != coin or fl.get("oid") is None:
                continue
            try:
                f_oid = int(fl["oid"])
                f_time = int(fl.get("time", 0) or 0)
                f_px = float(fl.get("px", 0.0) or 0.0)
                f_sz = abs(float(fl.get("sz", 0.0) or 0.0))
            except (TypeError, ValueError):
                continue
            if f_oid in all_mm_oids or f_time < min_fill_ms or f_px <= 0 or f_sz <= 0:
                continue
            f_side = "buy" if str(fl.get("side") or "").upper() in {"B", "BUY"} else "sell"
            if f_side != side_k or f_sz > sub_sz * (1.0 + 1e-4):
                continue
            px_ok = (f_px <= sub_px * (1.0 + 1e-5)) if side_k == "buy" else (f_px >= sub_px * (1.0 - 1e-5))
            if px_ok:
                all_mm_oids.add(f_oid)
                adopted += 1
                break
    ambiguous.clear()
    return adopted


MAX_BOOK_STALENESS_FOR_DMS_SEC = 10.0


def _hl_price_tick(px: float, sz_decimals: int) -> float:
    """Minimum Hyperliquid perp price tick at ``px`` for ``sz_decimals``."""
    if not math.isfinite(px) or px <= 0:
        return 1e-6
    if px >= 100_000:
        return 1.0
    max_dec = max(0, 6 - int(sz_decimals))
    magnitude = math.floor(math.log10(px))
    sig_dec = max(0, 4 - magnitude)
    decimals = min(max_dec, sig_dec)
    return float(10.0 ** (-decimals))


def reduce_only_is_valid(side: str, sz: float, account_position_sz: float) -> bool:
    """True when a ``side`` order of ``sz`` only reduces the signed *account* position."""
    if sz <= 0:
        return False
    if side == "sell":
        return account_position_sz >= sz - 1e-12
    return account_position_sz <= -sz + 1e-12


def compute_as_quotes(
    book: L2BookSnapshot,
    spec: AssetSpec,
    *,
    inventory_sz: float,
    sigma_tau_bps: float,
    params: ASParams = ASParams(),
    toxicity_mult: float = 1.0,
    lead_impulse_bps: float = 0.0,
    portfolio_other_inv_usd: float = 0.0,
    funding_overlay_bps: float = 0.0,
    account_position_sz: float | None = None,
) -> MMQuoteDecision:
    """Compute Avellaneda–Stoikov reservation price, optimal spread, and post-only ALO quotes.

    Supports external CEX lead-lag fair-price shifting & toxic-side pulling, cross-asset
    portfolio beta inventory coupling, and hourly ``:59->:05`` funding-unwind overlay.

    ``inventory_sz`` is the MM *session* inventory (drives skew and caps). The exchange applies
    ``reduceOnly`` to the *account* position, which on a shared address also holds other engines'
    inventory; pass ``account_position_sz`` so the unwind side is only flagged reduce-only when it
    actually reduces the account position (otherwise Hyperliquid rejects it and the MM sticks).
    """
    inv_usd = inventory_sz * book.mid_px
    eff_inv_usd = inv_usd + params.portfolio_beta_coupling * portfolio_other_inv_usd
    q_norm = eff_inv_usd / max(params.max_inventory_usd, 1.0)
    lead_shift = max(-params.max_lead_shift_bps, min(params.max_lead_shift_bps, params.lead_beta * lead_impulse_bps))
    fair_px = book.microprice * (1.0 + (params.obi_tilt_bps * book.imbalance5 + lead_shift + funding_overlay_bps) * 1e-4)

    # Pre-quote circuit breakers
    if book.book_spread_bps > params.max_book_spread_bps:
        return MMQuoteDecision(
            coin=book.coin,
            mid_px=book.mid_px,
            fair_px=fair_px,
            reservation_px=fair_px,
            sigma_tau_bps=sigma_tau_bps,
            q_norm=q_norm,
            skew_bps=0.0,
            half_spread_bps=0.0,
            bid_px=None,
            bid_sz=0.0,
            ask_px=None,
            ask_sz=0.0,
            halt_reason=f"wide_book_spread:{book.book_spread_bps:.1f}bps>{params.max_book_spread_bps:.1f}bps",
        )
    if sigma_tau_bps > params.vol_halt_sigma_bps:
        return MMQuoteDecision(
            coin=book.coin,
            mid_px=book.mid_px,
            fair_px=fair_px,
            reservation_px=fair_px,
            sigma_tau_bps=sigma_tau_bps,
            q_norm=q_norm,
            skew_bps=0.0,
            half_spread_bps=0.0,
            bid_px=None,
            bid_sz=0.0,
            ask_px=None,
            ask_sz=0.0,
            halt_reason=f"high_volatility:{sigma_tau_bps:.1f}bps>{params.vol_halt_sigma_bps:.1f}bps",
        )

    eff_sigma = min(max(sigma_tau_bps, params.min_sigma_bps), params.max_sigma_bps)
    gamma = max(params.gamma, 0.0)
    kappa = max(params.kappa, 1e-4)
    q_clamped = max(-1.5, min(1.5, q_norm))

    # 1. Reservation price offset (bps): delta_r = -q * gamma * sigma^2
    skew_bps = -q_clamped * gamma * (eff_sigma**2)
    # Cap skew so quotes never collapse past sanity bounds
    skew_bps = max(-params.max_half_spread_bps, min(params.max_half_spread_bps, skew_bps))
    reservation_px = fair_px * (1.0 + skew_bps * 1e-4)

    # 2. Optimal total spread (bps): delta = gamma * sigma^2 + (2 / gamma) * ln(1 + gamma / kappa)
    if gamma < 1e-9:
        adverse_term = 2.0 / kappa
    else:
        adverse_term = (2.0 / gamma) * math.log(1.0 + gamma / kappa)
    total_spread_bps = (gamma * (eff_sigma**2) + adverse_term) * max(1.0, toxicity_mult)

    fee_floor = max(params.min_half_spread_bps, params.maker_fee_bps + params.min_edge_bps)
    half_spread_bps = min(max(0.5 * total_spread_bps, fee_floor), params.max_half_spread_bps)

    raw_bid = reservation_px * (1.0 - half_spread_bps * 1e-4)
    raw_ask = reservation_px * (1.0 + half_spread_bps * 1e-4)
    if params.clamp_to_bbo:
        raw_bid = min(raw_bid, book.best_bid)
        raw_ask = max(raw_ask, book.best_ask)

    bid_px: float | None = round_hl_price(raw_bid, spec.sz_decimals, side="buy")
    ask_px: float | None = round_hl_price(raw_ask, spec.sz_decimals, side="sell")
    # Guarantee strict post-only non-crossing (bid < best_ask, ask > best_bid, bid < ask) even on 0-tick locked books
    if bid_px >= ask_px or (params.clamp_to_bbo and (bid_px >= book.best_ask or ask_px <= book.best_bid)):
        if book.best_bid < book.best_ask:
            bid_px = min(bid_px, round_hl_price(book.best_bid, spec.sz_decimals, side="buy"))
            ask_px = max(ask_px, round_hl_price(book.best_ask, spec.sz_decimals, side="sell"))
        if bid_px >= ask_px or bid_px >= book.best_ask:
            b_tick = _hl_price_tick(book.best_bid, spec.sz_decimals)
            bid_px = round_hl_price(
                min(book.best_ask - b_tick, book.best_bid * (1.0 - fee_floor * 1e-4)),
                spec.sz_decimals,
                side="buy",
            )
        if ask_px <= bid_px or ask_px <= book.best_bid:
            a_tick = _hl_price_tick(book.best_ask, spec.sz_decimals)
            ask_px = round_hl_price(
                max(book.best_bid + a_tick, book.best_ask * (1.0 + fee_floor * 1e-4)),
                spec.sz_decimals,
                side="sell",
            )

    # 3. Inventory-aware asymmetric quote sizing & hard inventory caps
    bid_reduce_only = False
    ask_reduce_only = False

    if inv_usd >= params.max_inventory_usd:
        # At or above max long cap -> turn off bids completely, unwind aggressively on ask
        bid_px = None
        bid_usd = 0.0
        ask_usd = min(params.base_order_usd * 1.5, abs(inv_usd))
        ask_reduce_only = True
    elif inv_usd <= -params.max_inventory_usd:
        # At or below max short cap -> turn off asks completely, unwind aggressively on bid
        ask_px = None
        ask_usd = 0.0
        bid_usd = min(params.base_order_usd * 1.5, abs(inv_usd))
        bid_reduce_only = True
    else:
        if q_norm >= 0:
            bid_usd = params.base_order_usd * max(0.0, 1.0 - 0.85 * q_norm)
            ask_usd = params.base_order_usd * (1.0 + 0.5 * q_norm)
        else:
            bid_usd = params.base_order_usd * (1.0 + 0.5 * abs(q_norm))
            ask_usd = params.base_order_usd * max(0.0, 1.0 - 0.85 * abs(q_norm))

    bid_sz = round_hl_size(bid_usd / bid_px, spec.sz_decimals) if (bid_px and bid_usd >= params.min_order_usd) else 0.0
    if bid_px is None or bid_sz <= 0 or bid_sz * bid_px < params.min_order_usd:
        bid_px = None
        bid_sz = 0.0

    ask_sz = round_hl_size(ask_usd / ask_px, spec.sz_decimals) if (ask_px and ask_usd >= params.min_order_usd) else 0.0
    if ask_px is None or ask_sz <= 0 or ask_sz * ask_px < params.min_order_usd:
        ask_px = None
        ask_sz = 0.0

    if account_position_sz is not None:
        bid_reduce_only = bid_reduce_only and reduce_only_is_valid("buy", bid_sz, account_position_sz)
        ask_reduce_only = ask_reduce_only and reduce_only_is_valid("sell", ask_sz, account_position_sz)

    # 4. Lead-Lag toxic-side instant pull (protect stale quotes when Binance jumps sharply)
    if params.lead_pull_bps > 0:
        if lead_impulse_bps >= params.lead_pull_bps and not ask_reduce_only:
            ask_px = None
            ask_sz = 0.0
        elif lead_impulse_bps <= -params.lead_pull_bps and not bid_reduce_only:
            bid_px = None
            bid_sz = 0.0

    return MMQuoteDecision(
        coin=book.coin,
        mid_px=book.mid_px,
        fair_px=fair_px,
        reservation_px=reservation_px,
        sigma_tau_bps=sigma_tau_bps,
        q_norm=q_norm,
        skew_bps=skew_bps,
        half_spread_bps=half_spread_bps,
        bid_px=bid_px,
        bid_sz=bid_sz,
        ask_px=ask_px,
        ask_sz=ask_sz,
        bid_reduce_only=bid_reduce_only,
        ask_reduce_only=ask_reduce_only,
    )


def should_requote_side(
    desired_px: float | None,
    desired_sz: float,
    resting: tuple[float, float] | tuple[float, float, bool] | None,
    *,
    mid_px: float,
    threshold_bps: float,
    desired_reduce_only: bool | None = None,
) -> bool:
    """Return True if a resting quote must be cancelled/replaced (respecting queue-preserving deadband)."""
    if desired_px is None or desired_sz <= 0:
        return resting is not None
    if resting is None:
        return True
    rest_px, rest_sz = float(resting[0]), float(resting[1])
    if desired_reduce_only is not None and len(resting) >= 3 and bool(resting[2]) != bool(desired_reduce_only):
        return True
    if mid_px <= 0:
        return True
    px_diff_bps = abs(desired_px - rest_px) / mid_px * 1e4
    if px_diff_bps >= threshold_bps:
        return True
    sz_diff_rel = abs(desired_sz - rest_sz) / max(desired_sz, rest_sz, 1e-12)
    return sz_diff_rel >= 0.25


# --------------------------------------------------------------------------- Average-cost PnL ledger


class _InventoryLedger:
    """Exact average-cost inventory & realized-vs-MTM PnL tracker."""

    def __init__(self, start_sz: float = 0.0, start_px: float = 0.0, maker_fee_bps: float = 1.5) -> None:
        self.start_sz = float(start_sz)
        self.net_sz = 0.0  # MM session delta inventory (isolated from pre-existing holdings)
        self.avg_entry_px = float(start_px)
        self.realized_pnl_usd = 0.0
        self.fees_usd = 0.0
        self.maker_fee_bps = float(maker_fee_bps)
        self.volume_usd = 0.0
        self.buy_fills = 0
        self.sell_fills = 0
        self.max_abs_inv_usd = 0.0

    def on_fill(self, side: str, px: float, sz: float, *, fee_usd: float | None = None) -> None:
        if sz <= 0 or px <= 0:
            return
        notional = px * sz
        self.volume_usd += notional
        fee = float(fee_usd) if fee_usd is not None else notional * self.maker_fee_bps * 1e-4
        self.fees_usd += fee
        signed = sz if side == "buy" else -sz
        if side == "buy":
            self.buy_fills += 1
        else:
            self.sell_fills += 1

        prev = self.net_sz
        if abs(prev) < 1e-12 or (prev > 0 and signed > 0) or (prev < 0 and signed < 0):
            # Increasing or opening inventory
            new_sz = prev + signed
            self.avg_entry_px = (abs(prev) * self.avg_entry_px + sz * px) / abs(new_sz)
            self.net_sz = new_sz
        else:
            # Reducing or flipping inventory
            close_sz = min(abs(prev), sz)
            direction = 1.0 if prev > 0 else -1.0
            self.realized_pnl_usd += direction * (px - self.avg_entry_px) * close_sz
            new_sz = prev + signed
            if abs(new_sz) < 1e-12:
                self.net_sz = 0.0
                self.avg_entry_px = 0.0
            elif (prev > 0 and new_sz < 0) or (prev < 0 and new_sz > 0):
                self.net_sz = new_sz
                self.avg_entry_px = px
            else:
                self.net_sz = new_sz
        self.max_abs_inv_usd = max(self.max_abs_inv_usd, abs(self.net_sz) * px)

    def unrealized_pnl_usd(self, mid_px: float) -> float:
        if abs(self.net_sz) < 1e-12 or mid_px <= 0:
            return 0.0
        return self.net_sz * (mid_px - self.avg_entry_px)

    def net_pnl_usd(self, mid_px: float) -> float:
        return self.realized_pnl_usd + self.unrealized_pnl_usd(mid_px) - self.fees_usd


def _append_monotonic_mid(mids_series: list[tuple[int, float]], ts_ms: int, mid_px: float) -> None:
    """Keep ``mids_series`` strictly non-decreasing in timestamp for ``np.searchsorted`` markout lookups."""
    if not (math.isfinite(mid_px) and mid_px > 0):
        return
    t = int(ts_ms)
    if mids_series:
        last_t = mids_series[-1][0]
        if t < last_t:
            return
        if t == last_t:
            mids_series[-1] = (t, float(mid_px))
            return
    mids_series.append((t, float(mid_px)))


def _compute_vw_markout(fills: list[dict[str, Any]], mids_series: list[tuple[int, float]], horizon_ms: int) -> float | None:
    """Compute volume-weighted signed post-fill markout (bps) at ``fill_ms + horizon_ms``."""
    if not fills or not mids_series:
        return None
    ts_arr = np.array([t for t, _ in mids_series], dtype=np.int64)
    mid_arr = np.array([m for _, m in mids_series], dtype=np.float64)
    if len(ts_arr) > 1 and np.any(ts_arr[1:] < ts_arr[:-1]):
        order = np.argsort(ts_arr, kind="mergesort")
        ts_arr = ts_arr[order]
        mid_arr = mid_arr[order]
    num = 0.0
    den = 0.0
    for fl in fills:
        t0 = int(fl["ts_ms"])
        target_t = t0 + horizon_ms
        idx = int(np.searchsorted(ts_arr, target_t, side="left"))
        if idx >= len(ts_arr):
            continue
        future_mid = float(mid_arr[idx])
        px = float(fl["px"])
        notional = float(fl["notional_usd"])
        if px <= 0 or future_mid <= 0 or notional <= 0:
            continue
        sgn = 1.0 if fl["side"] == "buy" else -1.0
        mk_bps = sgn * (future_mid - px) / px * 1e4
        num += mk_bps * notional
        den += notional
    return round(num / den, 2) if den > 0 else None


# --------------------------------------------------------------------------- Offline L2 Replay Simulator


def simulate_avellaneda_stoikov_on_l2(
    l2_df: pd.DataFrame,
    spec: AssetSpec,
    *,
    trades_df: pd.DataFrame | None = None,
    params: ASParams = ASParams(),
) -> tuple[MMSessionSummary, pd.DataFrame, pd.DataFrame]:
    """Replay Avellaneda–Stoikov MM over an L2 book DataFrame (touch + optional trade-through fills)."""
    if l2_df.empty:
        raise ValueError("l2_df is empty")
    ts_col = "recv_ms" if "recv_ms" in l2_df.columns else "book_time_ms"
    df = l2_df.sort_values(ts_col).reset_index(drop=True)
    vol_est = EWMAVolatilityEstimator(
        horizon_sec=params.horizon_sec,
        half_life_sec=params.vol_half_life_sec,
        default_sigma_bps=params.default_sigma_bps,
    )
    ledger = _InventoryLedger(maker_fee_bps=params.maker_fee_bps)

    resting_bid: tuple[float, float, bool] | None = None
    resting_ask: tuple[float, float, bool] | None = None
    quotes_placed = 0
    quotes_cancelled = 0
    requotes_skipped = 0
    halt_events: list[str] = []
    fills: list[dict[str, Any]] = []
    quote_rows: list[dict[str, Any]] = []
    mids_series: list[tuple[int, float]] = []

    trades_by_idx: dict[int, list[tuple[str, float, float]]] = {}
    if trades_df is not None and not trades_df.empty:
        t_ts = trades_df["trade_time_ms" if "trade_time_ms" in trades_df.columns else "recv_ms"].to_numpy(dtype=np.int64)
        l2_ts = df[ts_col].to_numpy(dtype=np.int64)
        bins = np.searchsorted(l2_ts, t_ts, side="left")
        for b_idx, row in zip(bins, trades_df.itertuples(index=False), strict=False):
            if 0 <= int(b_idx) < len(df):
                trades_by_idx.setdefault(int(b_idx), []).append((str(row.side), float(row.px), float(row.sz)))

    half_spreads: list[float] = []
    sigmas: list[float] = []
    last_mid = float(df["mid_px"].iloc[0]) if "mid_px" in df.columns else 0.5 * (float(df["best_bid"].iloc[0]) + float(df["best_ask"].iloc[0]))

    for idx, row in enumerate(df.itertuples(index=False)):
        ts_ms = int(getattr(row, ts_col))
        bb = float(row.best_bid)
        ba = float(row.best_ask)
        mid = 0.5 * (bb + ba)
        last_mid = mid
        _append_monotonic_mid(mids_series, ts_ms, mid)
        bsz1 = float(getattr(row, "bid_sz1", 1.0))
        asz1 = float(getattr(row, "ask_sz1", 1.0))
        imb5 = float(getattr(row, "imbalance5", 0.0))
        tot1 = bsz1 + asz1
        micro = (bb * asz1 + ba * bsz1) / tot1 if tot1 > 0 else mid

        # 1. Check fills on resting quotes against current book touch + intervening trades
        tick_trades = trades_by_idx.get(idx, [])
        if resting_bid is not None:
            b_px, b_sz, _ = resting_bid
            trade_hit = any(side == "sell" and t_px <= b_px for side, t_px, _ in tick_trades)
            if ba <= b_px or trade_hit:
                ledger.on_fill("buy", b_px, b_sz)
                fills.append({"ts_ms": ts_ms, "side": "buy", "px": b_px, "sz": b_sz, "notional_usd": b_px * b_sz})
                resting_bid = None

        if resting_ask is not None:
            a_px, a_sz, _ = resting_ask
            trade_hit = any(side == "buy" and t_px >= a_px for side, t_px, _ in tick_trades)
            if bb >= a_px or trade_hit:
                ledger.on_fill("sell", a_px, a_sz)
                fills.append({"ts_ms": ts_ms, "side": "sell", "px": a_px, "sz": a_sz, "notional_usd": a_px * a_sz})
                resting_ask = None

        # 2. Check session drawdown kill-switch
        cur_net = ledger.net_pnl_usd(mid)
        if cur_net <= -abs(params.max_session_drawdown_usd):
            if resting_bid is not None:
                quotes_cancelled += 1
                resting_bid = None
            if resting_ask is not None:
                quotes_cancelled += 1
                resting_ask = None
            halt_events.append(f"drawdown_stop:{cur_net:.2f}$")
            break

        # 3. Update EWMA vol & toxicity multiplier
        sigma_tau = vol_est.update(ts_ms, mid)
        tox_mult = 1.0
        if len(fills) >= 3:
            mk5 = _compute_vw_markout(fills[-10:], mids_series, 5_000)
            if mk5 is not None:
                if mk5 <= params.adverse_markout_halt_bps:
                    if resting_bid is not None:
                        quotes_cancelled += 1
                        resting_bid = None
                    if resting_ask is not None:
                        quotes_cancelled += 1
                        resting_ask = None
                    halt_events.append(f"adverse_markout_halt:{mk5:.1f}bps")
                    continue
                if mk5 <= params.adverse_markout_widen_bps:
                    tox_mult = 1.5

        book = L2BookSnapshot(
            coin=spec.coin,
            observed_ms=ts_ms,
            best_bid=bb,
            best_ask=ba,
            bid_sz1=bsz1,
            ask_sz1=asz1,
            imbalance5=imb5,
            mid_px=mid,
            microprice=micro,
            book_spread_bps=(ba - bb) / mid * 1e4,
        )
        dec = compute_as_quotes(
            book,
            spec,
            inventory_sz=ledger.net_sz,
            sigma_tau_bps=sigma_tau,
            params=params,
            toxicity_mult=tox_mult,
        )
        if dec.halt_reason:
            halt_events.append(dec.halt_reason)
            if resting_bid is not None:
                quotes_cancelled += 1
                resting_bid = None
            if resting_ask is not None:
                quotes_cancelled += 1
                resting_ask = None
            continue

        half_spreads.append(dec.half_spread_bps)
        sigmas.append(dec.sigma_tau_bps)

        # 4. Queue-preserving requote deadband per side (including reduce_only transitions)
        if should_requote_side(
            dec.bid_px,
            dec.bid_sz,
            resting_bid,
            mid_px=mid,
            threshold_bps=params.requote_threshold_bps,
            desired_reduce_only=dec.bid_reduce_only,
        ):
            if resting_bid is not None:
                quotes_cancelled += 1
            if dec.bid_px is not None and dec.bid_sz > 0:
                resting_bid = (dec.bid_px, dec.bid_sz, dec.bid_reduce_only)
                quotes_placed += 1
            else:
                resting_bid = None
        else:
            requotes_skipped += 1

        if should_requote_side(
            dec.ask_px,
            dec.ask_sz,
            resting_ask,
            mid_px=mid,
            threshold_bps=params.requote_threshold_bps,
            desired_reduce_only=dec.ask_reduce_only,
        ):
            if resting_ask is not None:
                quotes_cancelled += 1
            if dec.ask_px is not None and dec.ask_sz > 0:
                resting_ask = (dec.ask_px, dec.ask_sz, dec.ask_reduce_only)
                quotes_placed += 1
            else:
                resting_ask = None
        else:
            requotes_skipped += 1

        quote_rows.append(
            {
                "ts_ms": ts_ms,
                "mid_px": mid,
                "fair_px": dec.fair_px,
                "reservation_px": dec.reservation_px,
                "sigma_tau_bps": dec.sigma_tau_bps,
                "q_norm": dec.q_norm,
                "skew_bps": dec.skew_bps,
                "half_spread_bps": dec.half_spread_bps,
                "bid_px": dec.bid_px,
                "ask_px": dec.ask_px,
                "inventory_sz": ledger.net_sz,
                "net_pnl_usd": ledger.net_pnl_usd(mid),
            }
        )

    cleanup_cnt = int(resting_bid is not None) + int(resting_ask is not None)
    summary = MMSessionSummary(
        mode="l2_replay_sim",
        coin=spec.coin,
        started_ms=int(df[ts_col].iloc[0]),
        ended_ms=int(df[ts_col].iloc[-1]),
        ticks_observed=len(df),
        quotes_placed=quotes_placed,
        quotes_cancelled=quotes_cancelled,
        requotes_skipped_deadband=requotes_skipped,
        fills_count=len(fills),
        buy_fills=ledger.buy_fills,
        sell_fills=ledger.sell_fills,
        volume_usd=round(ledger.volume_usd, 2),
        maker_share_pct=100.0 if fills else 0.0,
        start_inventory_sz=0.0,
        end_inventory_sz=round(ledger.net_sz, spec.sz_decimals),
        max_abs_inventory_usd=round(ledger.max_abs_inv_usd, 2),
        avg_half_spread_bps=round(float(np.mean(half_spreads)), 2) if half_spreads else 0.0,
        avg_sigma_tau_bps=round(float(np.mean(sigmas)), 2) if sigmas else 0.0,
        realized_spread_pnl_usd=round(ledger.realized_pnl_usd, 4),
        inventory_mtm_pnl_usd=round(ledger.unrealized_pnl_usd(last_mid), 4),
        fees_usd=round(-ledger.fees_usd, 4),
        net_pnl_usd=round(ledger.net_pnl_usd(last_mid), 4),
        vw_markout_5s_bps=_compute_vw_markout(fills, mids_series, 5_000),
        vw_markout_30s_bps=_compute_vw_markout(fills, mids_series, 30_000),
        halt_events=halt_events[:20],
        final_cleanup_cancelled=cleanup_cnt,
    )
    return summary, pd.DataFrame(fills), pd.DataFrame(quote_rows)


# --------------------------------------------------------------------------- Live Hyperliquid Testnet MM loop


def _fill_dedup_key(
    fl: dict[str, Any],
    coin: str,
    f_oid: int,
    f_side: str,
    f_sz: float,
    f_time: int,
) -> tuple[Any, ...]:
    """Build a collision-free fill deduplication key prioritizing Hyperliquid's unique ``tid``."""
    tid = fl.get("tid")
    if tid is not None:
        try:
            return ("tid", int(tid))
        except (TypeError, ValueError):
            return ("tid", str(tid))
    return ("fallback", coin, f_oid, f_side, f_sz, f_time, str(fl.get("hash") or ""))


def _unwrap_sdk_response(resp: dict[str, Any]) -> dict[str, Any] | None:
    """Accept both ``HyperliquidTestnetSDKAdapter`` wrapper dicts and direct SDK order dicts."""
    inner = resp.get("response")
    if isinstance(inner, dict) and ("status" in inner or "response" in inner):
        return inner
    if isinstance(inner, dict) and "data" in inner:
        return resp
    return None


def _extract_order_oid(resp: dict[str, Any]) -> tuple[int | None, str]:
    if not isinstance(resp, dict):
        return None, "invalid_response"
    sdk_resp = _unwrap_sdk_response(resp)
    if sdk_resp is None:
        if resp.get("mode") == "dry_run":
            return None, "simulated_alo"
        return None, str(resp.get("status", "unknown"))
    statuses, err_msg = extract_hl_order_statuses(sdk_resp)
    if err_msg is not None:
        return None, f"error:{err_msg}"
    for st in statuses:
        if not isinstance(st, dict):
            continue
        if "resting" in st and isinstance(st["resting"], dict) and "oid" in st["resting"]:
            return int(st["resting"]["oid"]), "resting"
        if "filled" in st and isinstance(st["filled"], dict) and "oid" in st["filled"]:
            return int(st["filled"]["oid"]), "filled"
        if "error" in st:
            return None, f"error:{st['error']}"
    if resp.get("mode") == "dry_run":
        return None, "simulated_alo"
    return None, str(resp.get("status", "unknown"))


def _extract_ioc_fill(
    resp: Any,
    *,
    fallback_sz: float,
    fallback_px: float,
    execute_live: bool,
) -> tuple[float, float]:
    """Extract ``(filled_sz, avg_px)`` from an IOC flatten response (honoring dry-run & live SDK shapes)."""
    if not execute_live:
        return fallback_sz, fallback_px
    if not isinstance(resp, dict):
        return 0.0, fallback_px
    sdk_resp = _unwrap_sdk_response(resp)
    if sdk_resp is None:
        if resp.get("mode") == "dry_run":
            return fallback_sz, fallback_px
        return 0.0, fallback_px
    statuses, err_msg = extract_hl_order_statuses(sdk_resp)
    if err_msg is not None:
        return 0.0, fallback_px
    filled_sz = 0.0
    notional = 0.0
    for st in statuses:
        if isinstance(st, dict) and isinstance(st.get("filled"), dict):
            fl = st["filled"]
            sz = abs(float(fl.get("totalSz") or fl.get("sz") or 0.0))
            px = float(fl.get("avgPx") or fl.get("px") or fallback_px)
            filled_sz += sz
            notional += sz * px
    avg_px = (notional / filled_sz) if filled_sz > 0 else fallback_px
    return filled_sz, avg_px


def run_live_mm_session(
    coin: str,
    adapter: ExchangeAdapter,
    *,
    account_address: str | None = None,
    execute_live: bool = False,
    duration_sec: float = 45.0,
    poll_interval_sec: float = 2.0,
    enable_dead_man_switch: bool = False,
    dead_man_timeout_ms: int = 15_000,
    flatten_on_exit: bool = False,
    params: ASParams = ASParams(),
    audit_log_path: Path | None = None,
    client: httpx.Client | None = None,
) -> tuple[MMSessionSummary, pd.DataFrame, pd.DataFrame]:
    """Run an Avellaneda–Stoikov MM session against live Hyperliquid Testnet L2 books.

    In ``execute_live=True`` mode, submits real two-sided ``ALO`` quotes on Testnet for ``coin``
    (refusing ``onlyIsolated`` coins), monitors real fills & markouts, and guarantees canceling
    every open MM quote inside ``finally:`` upon exit or error.
    """
    owns_client = client is None
    http = client or httpx.Client(timeout=15.0)
    started_ms = int(time.time() * 1000)

    active_orders: dict[str, dict[str, Any]] = {}  # {"buy": {oid, px, sz}, "sell": ...}
    all_mm_oids: set[int] = set()
    seen_fill_keys: set[tuple[Any, ...]] = set()
    cleanup_cancelled = 0
    dead_man_refreshes = 0
    dead_man_armed = False
    flattened_exit_sz = 0.0
    dms_left_armed = False
    spec: AssetSpec | None = None
    ledger: _InventoryLedger | None = None
    last_mid = 0.0
    fills: list[dict[str, Any]] = []

    def _refresh_rest_dms(ts_ms: int) -> None:
        nonlocal dead_man_refreshes, dead_man_armed
        if not (enable_dead_man_switch and hasattr(adapter, "schedule_cancel")):
            return
        try:
            resp = adapter.schedule_cancel(ts_ms + max(6_000, dead_man_timeout_ms))
            inner = resp.get("response") if isinstance(resp, dict) else None
            if (
                (isinstance(resp, dict) and resp.get("status") == "ok")
                or (isinstance(inner, dict) and inner.get("status") == "ok")
            ):
                dead_man_refreshes += 1
                dead_man_armed = True
        except Exception:
            return

    ticks_observed = 0
    quotes_placed = 0
    quotes_cancelled = 0
    requotes_skipped = 0
    halt_events: list[str] = []
    quote_rows: list[dict[str, Any]] = []
    mids_series: list[tuple[int, float]] = []
    half_spreads: list[float] = []
    sigmas: list[float] = []
    ambiguous_subs: list[dict[str, Any]] = []
    live_init_ok = True

    def _ingest_rest_fills(uf_rows: list[dict[str, Any]]) -> None:
        assert ledger is not None
        for fl in uf_rows:
            if not isinstance(fl, dict) or str(fl.get("coin")) != coin:
                continue
            try:
                f_oid = int(fl.get("oid", -1))
                f_time = int(fl.get("time", 0))
            except (TypeError, ValueError):
                continue
            if f_oid not in all_mm_oids or f_time < started_ms - 5_000:
                continue
            f_sz = abs(float(fl.get("sz", 0.0)))
            f_px = float(fl.get("px", 0.0))
            f_side = "buy" if str(fl.get("side")) == "B" else "sell"
            key = _fill_dedup_key(fl, coin, f_oid, f_side, f_sz, f_time)
            if key in seen_fill_keys:
                continue
            seen_fill_keys.add(key)
            ledger.on_fill(f_side, f_px, f_sz, fee_usd=float(fl.get("fee", 0.0)))
            fills.append(
                {
                    "ts_ms": f_time,
                    "oid": f_oid,
                    "side": f_side,
                    "px": f_px,
                    "sz": f_sz,
                    "notional_usd": f_px * f_sz,
                    "crossed": bool(fl.get("crossed", False)),
                }
            )

    try:
        meta = fetch_testnet_meta(http)
        if coin not in meta:
            raise ValueError(f"coin {coin!r} not listed on Hyperliquid Testnet")
        spec = meta[coin]
        if spec.only_isolated:
            raise ValueError(f"refusing to market-make onlyIsolated asset {coin!r} ({spec.margin_mode})")

        # Read starting position on this coin so MM tracks only its own delta inventory.
        # Fail closed if live account state cannot be verified so we never quote against a fictitious 0 baseline.
        start_inv_sz = 0.0
        if execute_live:
            ch = fetch_clearinghouse_state_checked(http, account_address) if account_address else None
            if ch is None:
                halt_events.append("init_clearinghouse_unavailable")
                live_init_ok = False
            else:
                for entry in ch.get("assetPositions") or []:
                    p = (entry.get("position") or {}) if isinstance(entry, dict) else {}
                    if str(p.get("coin")) == coin:
                        start_inv_sz = float(p.get("szi", 0.0))
                        break
                # Ensure safe cross leverage (<=3x) before quoting
                try:
                    adapter.set_leverage(coin, max(1, min(3, spec.max_leverage)), is_cross=True)
                except Exception:
                    pass

        vol_est = EWMAVolatilityEstimator(
            horizon_sec=params.horizon_sec,
            half_life_sec=params.vol_half_life_sec,
            default_sigma_bps=params.default_sigma_bps,
        )
        ledger = _InventoryLedger(start_sz=start_inv_sz, maker_fee_bps=params.maker_fee_bps)

        if live_init_ok and enable_dead_man_switch:
            _refresh_rest_dms(started_ms)
        dms_refresh_sec = max(3.0, (dead_man_timeout_ms / 1000.0) * 0.4)
        last_dms_mono = time.monotonic()
        last_book_ok_mono = time.monotonic()

        deadline = time.monotonic() + max(1.0, duration_sec)
        while live_init_ok:
            loop_start = time.monotonic()
            now_ms = int(time.time() * 1000)
            # Stale market data must not keep quotes alive: only extend the exchange-side Dead Man's
            # Switch while books keep arriving, so a frozen feed lets it fire and pull the quotes.
            if (
                enable_dead_man_switch
                and (loop_start - last_dms_mono) >= dms_refresh_sec
                and (loop_start - last_book_ok_mono) <= MAX_BOOK_STALENESS_FOR_DMS_SEC
            ):
                _refresh_rest_dms(now_ms)
                last_dms_mono = loop_start
            try:
                book = fetch_testnet_l2_snapshot(http, coin)
            except Exception:
                book = None
            if book is not None:
                last_book_ok_mono = time.monotonic()
            if book is None:
                if active_orders:
                    to_cancel_none = [
                        {"coin": coin, "oid": o["oid"]} for o in active_orders.values() if o.get("oid") is not None
                    ]
                    if to_cancel_none:
                        try:
                            c_resps = adapter.cancel_all_open_orders(to_cancel_none)
                        except Exception as exc:
                            c_resps = [{"status": "err", "response": str(exc)}]
                        if _cancel_response_ok(c_resps):
                            quotes_cancelled += len(to_cancel_none)
                            active_orders.clear()
                        elif _cancel_response_confirmed_gone(c_resps):
                            active_orders.clear()
                    else:
                        active_orders.clear()
                    halt_events.append("empty_or_invalid_book")
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(poll_interval_sec, max(0.0, deadline - time.monotonic())))
                continue

            ticks_observed += 1
            last_mid = book.mid_px
            _append_monotonic_mid(mids_series, now_ms, book.mid_px)

            # 1. Reconcile fills & ambiguous placements BEFORE popping vanished open orders or quoting
            fill_sync_halt: str | None = None
            if execute_live and account_address:
                uf = fetch_user_fills_checked(http, account_address)
                oo = fetch_open_orders_checked(http, account_address)
                if ambiguous_subs and oo is not None and uf is not None:
                    quotes_placed += _reconcile_ambiguous_submissions(
                        coin,
                        ambiguous_subs,
                        oo=oo,
                        uf=uf,
                        active_orders=active_orders,
                        all_mm_oids=all_mm_oids,
                        started_ms=started_ms,
                    )
                if uf is None:
                    fill_sync_halt = "user_fills_unavailable"
                else:
                    _ingest_rest_fills(uf)
                    if oo is not None:
                        live_oids = {int(o["oid"]) for o in oo if o.get("oid") is not None}
                        for side_k in list(active_orders.keys()):
                            oid = active_orders[side_k].get("oid")
                            if oid is not None and oid not in live_oids:
                                active_orders.pop(side_k, None)
                if ambiguous_subs:
                    fill_sync_halt = fill_sync_halt or "ambiguous_placement_unreconciled"
            else:
                # Dry-run paper touch fills
                for side_k in list(active_orders.keys()):
                    ord_info = active_orders[side_k]
                    if (side_k == "buy" and book.best_ask <= ord_info["px"]) or (
                        side_k == "sell" and book.best_bid >= ord_info["px"]
                    ):
                        ledger.on_fill(side_k, ord_info["px"], ord_info["sz"])
                        fills.append(
                            {
                                "ts_ms": now_ms,
                                "oid": ord_info.get("oid"),
                                "side": side_k,
                                "px": ord_info["px"],
                                "sz": ord_info["sz"],
                                "notional_usd": ord_info["px"] * ord_info["sz"],
                                "crossed": False,
                            }
                        )
                        active_orders.pop(side_k, None)

            # 2. Check drawdown circuit breaker
            cur_net = ledger.net_pnl_usd(book.mid_px)
            if cur_net <= -abs(params.max_session_drawdown_usd):
                halt_events.append(f"drawdown_stop:{cur_net:.2f}$")
                break

            # 3. Compute Avellaneda–Stoikov quotes (with adverse-markout widen & halt circuit breaker)
            sigma_tau = vol_est.update(book.observed_ms, book.mid_px)
            adverse_halt: str | None = None
            tox_mult = 1.0
            if len(fills) >= 3:
                mk5 = _compute_vw_markout(fills[-10:], mids_series, 5_000)
                if mk5 is not None:
                    if mk5 <= params.adverse_markout_halt_bps:
                        adverse_halt = f"adverse_markout_halt:{mk5:.1f}bps"
                    elif mk5 <= params.adverse_markout_widen_bps:
                        tox_mult = 1.5
            dec = compute_as_quotes(
                book,
                spec,
                inventory_sz=ledger.net_sz,
                sigma_tau_bps=sigma_tau,
                params=params,
                toxicity_mult=tox_mult,
                account_position_sz=ledger.start_sz + ledger.net_sz,
            )
            eff_halt = fill_sync_halt or adverse_halt or dec.halt_reason
            if eff_halt:
                halt_events.append(eff_halt)
                to_cancel = [{"coin": coin, "oid": o["oid"]} for o in active_orders.values() if o.get("oid") is not None]
                if to_cancel:
                    try:
                        c_resps = adapter.cancel_all_open_orders(to_cancel)
                    except Exception as exc:
                        c_resps = [{"status": "err", "response": str(exc)}]
                    if _cancel_response_ok(c_resps):
                        quotes_cancelled += len(to_cancel)
                        active_orders.clear()
                    elif _cancel_response_confirmed_gone(c_resps):
                        active_orders.clear()
                else:
                    active_orders.clear()
            else:
                half_spreads.append(dec.half_spread_bps)
                sigmas.append(dec.sigma_tau_bps)
                desired = {
                    "buy": (dec.bid_px, dec.bid_sz, dec.bid_reduce_only),
                    "sell": (dec.ask_px, dec.ask_sz, dec.ask_reduce_only),
                }
                for side_k, (d_px, d_sz, d_red) in desired.items():
                    cur_ord = active_orders.get(side_k)
                    cur_tuple = (
                        (cur_ord["px"], cur_ord["sz"], bool(cur_ord.get("reduce_only", False))) if cur_ord else None
                    )
                    if not should_requote_side(
                        d_px,
                        d_sz,
                        cur_tuple,
                        mid_px=book.mid_px,
                        threshold_bps=params.requote_threshold_bps,
                        desired_reduce_only=d_red,
                    ):
                        requotes_skipped += 1
                        continue
                    # Cancel existing side order if present before replacing
                    if cur_ord is not None:
                        if cur_ord.get("oid") is not None:
                            try:
                                c_resps = adapter.cancel_all_open_orders([{"coin": coin, "oid": cur_ord["oid"]}])
                            except Exception as exc:
                                c_resps = [{"status": "err", "response": str(exc)}]
                            if _cancel_response_ok(c_resps):
                                quotes_cancelled += 1
                                active_orders.pop(side_k, None)
                            elif _cancel_response_confirmed_gone(c_resps):
                                active_orders.pop(side_k, None)
                                continue
                            else:
                                continue
                        else:
                            quotes_cancelled += 1
                            active_orders.pop(side_k, None)
                    # Place new passive ALO quote
                    if d_px is not None and d_sz > 0:
                        order = PlannedOrder(
                            coin=coin,
                            side=side_k,
                            limit_px=d_px,
                            sz=d_sz,
                            notional_usd=round(d_px * d_sz, 2),
                            tif="Alo",
                            reduce_only=d_red,
                        )
                        try:
                            resp = adapter.place_alo_order(order)
                        except Exception as exc:
                            resp = {
                                "status": f"ambiguous_exception:{type(exc).__name__}",
                                "response": f"{type(exc).__name__}: {exc}",
                            }
                        oid, status = _extract_order_oid(resp)
                        is_ambig = execute_live and oid is None and _is_ambiguous_order_status(status)
                        if not _is_ambiguous_order_status(status) and not status.startswith("err"):
                            quotes_placed += 1
                        if oid is not None:
                            all_mm_oids.add(oid)
                        if status in {"resting", "simulated_alo"}:
                            active_orders[side_k] = {"oid": oid, "px": d_px, "sz": d_sz, "reduce_only": bool(d_red)}
                        elif is_ambig:
                            ambiguous_subs.append(
                                {
                                    "side": side_k,
                                    "px": float(d_px),
                                    "sz": float(d_sz),
                                    "reduce_only": bool(d_red),
                                    "ts_ms": now_ms,
                                }
                            )
                            halt_events.append(f"ambiguous_placement:{side_k}")
                        if audit_log_path is not None:
                            is_dry = isinstance(adapter, DryRunAdapter) or (
                                isinstance(resp, dict) and resp.get("mode") == "dry_run"
                            )
                            audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                            with audit_log_path.open("a", encoding="utf-8") as fh:
                                fh.write(
                                    json.dumps(
                                        {
                                            "ts_ms": now_ms,
                                            "mode": "dry_run" if is_dry else "rest_live",
                                            "coin": coin,
                                            "status": status,
                                            "oid": oid,
                                            "order": order.to_dict(),
                                        },
                                        default=str,
                                    )
                                    + "\n"
                                )

            quote_rows.append(
                {
                    "ts_ms": now_ms,
                    "mid_px": book.mid_px,
                    "fair_px": dec.fair_px,
                    "reservation_px": dec.reservation_px,
                    "sigma_tau_bps": dec.sigma_tau_bps,
                    "q_norm": dec.q_norm,
                    "skew_bps": dec.skew_bps,
                    "half_spread_bps": dec.half_spread_bps,
                    "bid_px": dec.bid_px,
                    "ask_px": dec.ask_px,
                    "inventory_sz": ledger.net_sz,
                    "net_pnl_usd": ledger.net_pnl_usd(book.mid_px),
                }
            )

            if time.monotonic() >= deadline:
                break
            elapsed = time.monotonic() - loop_start
            sleep_for = min(max(0.0, poll_interval_sec - elapsed), max(0.0, deadline - time.monotonic()))
            if sleep_for > 0:
                time.sleep(sleep_for)
    finally:
        # Guaranteed cleanup: cancel every MM order created in this session so nothing is left orphaned.
        cancel_list: list[dict[str, Any]] = []
        if execute_live and account_address:
            oo_final = fetch_open_orders_checked(http, account_address)
            if oo_final is not None:
                _adopt_matching_open_orders_for_cleanup(coin, ambiguous_subs, oo_final, all_mm_oids)
                for o in oo_final:
                    if isinstance(o, dict) and str(o.get("coin")) == coin and o.get("oid") is not None:
                        if int(o["oid"]) in all_mm_oids:
                            cancel_list.append({"coin": coin, "oid": int(o["oid"])})
            else:
                cancel_list = [{"coin": coin, "oid": o["oid"]} for o in active_orders.values() if o.get("oid") is not None]
        else:
            cancel_list = [{"coin": coin, "oid": o.get("oid")} for o in active_orders.values()]
        if cancel_list:
            try:
                adapter.cancel_all_open_orders(cancel_list)
                cleanup_cancelled = len(cancel_list)
            except Exception:
                pass

        mm_orders_cleared = True
        if execute_live and account_address:
            mm_orders_cleared = False
            for _attempt in range(2):
                oo_check = fetch_open_orders_checked(http, account_address)
                if oo_check is None:
                    break
                _adopt_matching_open_orders_for_cleanup(coin, ambiguous_subs, oo_check, all_mm_oids)
                leftovers = [
                    {"coin": coin, "oid": int(o["oid"])}
                    for o in oo_check
                    if isinstance(o, dict)
                    and str(o.get("coin")) == coin
                    and o.get("oid") is not None
                    and int(o["oid"]) in all_mm_oids
                ]
                if not leftovers:
                    mm_orders_cleared = True
                    break
                try:
                    adapter.cancel_all_open_orders(leftovers)
                except Exception:
                    pass

        final_fills_synced = True
        if execute_live and account_address and ledger is not None and (all_mm_oids or ambiguous_subs):
            uf_fin = fetch_user_fills_checked(http, account_address)
            oo_fin_chk = fetch_open_orders_checked(http, account_address) if ambiguous_subs else []
            if uf_fin is None or oo_fin_chk is None:
                final_fills_synced = False
            else:
                if ambiguous_subs:
                    quotes_placed += _reconcile_ambiguous_submissions(
                        coin,
                        ambiguous_subs,
                        oo=oo_fin_chk,
                        uf=uf_fin,
                        active_orders=active_orders,
                        all_mm_oids=all_mm_oids,
                        started_ms=started_ms,
                    )
                _ingest_rest_fills(uf_fin)

        if (
            flatten_on_exit
            and live_init_ok
            and final_fills_synced
            and spec is not None
            and ledger is not None
            and last_mid > 0
            and abs(ledger.net_sz) > 1e-12
        ):
            unwind_sz = round_hl_size(abs(ledger.net_sz), spec.sz_decimals)
            unwind_side = "sell" if ledger.net_sz > 0 else "buy"
            acct_pos = ledger.start_sz + ledger.net_sz
            flat_reduce_only = reduce_only_is_valid(unwind_side, unwind_sz, acct_pos)
            closes_full_acct = flat_reduce_only and abs(unwind_sz - abs(acct_pos)) <= max(
                1e-9, 0.5 * (10.0 ** -spec.sz_decimals)
            )
            min_flat_usd = 0.50 if closes_full_acct else 10.5
            if unwind_sz > 0 and unwind_sz * last_mid >= min_flat_usd and hasattr(adapter, "place_ioc_order"):
                raw_px = last_mid * (0.9975 if unwind_side == "sell" else 1.0025)
                limit_px = round_hl_price(raw_px, spec.sz_decimals, side=unwind_side)
                flat_ord = PlannedOrder(
                    coin=coin,
                    side=unwind_side,
                    limit_px=limit_px,
                    sz=unwind_sz,
                    notional_usd=round(unwind_sz * last_mid, 2),
                    tif="Ioc",
                    reduce_only=flat_reduce_only,
                )
                try:
                    flat_resp = adapter.place_ioc_order(flat_ord)
                    filled_sz, filled_px = _extract_ioc_fill(
                        flat_resp, fallback_sz=unwind_sz, fallback_px=last_mid, execute_live=execute_live
                    )
                    if filled_sz > 0:
                        flattened_exit_sz = filled_sz
                        ledger.on_fill(unwind_side, filled_px, filled_sz, fee_usd=filled_sz * filled_px * 4.5e-4)
                        fills.append(
                            {
                                "ts_ms": int(time.time() * 1000),
                                "oid": None,
                                "side": unwind_side,
                                "px": filled_px,
                                "sz": filled_sz,
                                "notional_usd": filled_sz * filled_px,
                                "crossed": True,
                            }
                        )
                    if audit_log_path is not None:
                        is_dry_flat = (
                            (not execute_live)
                            or isinstance(adapter, DryRunAdapter)
                            or (isinstance(flat_resp, dict) and flat_resp.get("mode") == "dry_run")
                        )
                        audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                        with audit_log_path.open("a", encoding="utf-8") as fh:
                            fh.write(
                                json.dumps(
                                    {
                                        "ts_ms": int(time.time() * 1000),
                                        "mode": "dry_run" if is_dry_flat else "rest_flatten_ioc",
                                        "action": "rest_flatten_ioc",
                                        "coin": coin,
                                        "oid": _extract_order_oid(flat_resp)[0],
                                        "status": "flatten",
                                        "filled_sz": filled_sz,
                                        "order": flat_ord.to_dict(),
                                    },
                                    default=str,
                                )
                                + "\n"
                            )
                except Exception:
                    pass

        if dead_man_armed and not mm_orders_cleared:
            dms_left_armed = True
        if dead_man_armed and mm_orders_cleared and hasattr(adapter, "schedule_cancel"):
            try:
                adapter.schedule_cancel(None)
            except Exception:
                pass
        if owns_client:
            http.close()

    ended_ms = int(time.time() * 1000)
    maker_vol = sum(f["notional_usd"] for f in fills if not f.get("crossed", False))
    tot_vol = sum(f["notional_usd"] for f in fills)
    assert spec is not None and ledger is not None
    summary = MMSessionSummary(
        mode="testnet_live_mm" if execute_live else "testnet_dry_run_mm",
        coin=coin,
        started_ms=started_ms,
        ended_ms=ended_ms,
        ticks_observed=ticks_observed,
        quotes_placed=quotes_placed,
        quotes_cancelled=quotes_cancelled,
        requotes_skipped_deadband=requotes_skipped,
        fills_count=len(fills),
        buy_fills=ledger.buy_fills,
        sell_fills=ledger.sell_fills,
        volume_usd=round(ledger.volume_usd, 2),
        maker_share_pct=round(maker_vol / tot_vol * 100.0, 1) if tot_vol > 0 else (100.0 if not fills else 0.0),
        start_inventory_sz=round(start_inv_sz, spec.sz_decimals),
        end_inventory_sz=round(ledger.net_sz, spec.sz_decimals),
        max_abs_inventory_usd=round(ledger.max_abs_inv_usd, 2),
        avg_half_spread_bps=round(float(np.mean(half_spreads)), 2) if half_spreads else 0.0,
        avg_sigma_tau_bps=round(float(np.mean(sigmas)), 2) if sigmas else 0.0,
        realized_spread_pnl_usd=round(ledger.realized_pnl_usd, 4),
        inventory_mtm_pnl_usd=round(ledger.unrealized_pnl_usd(last_mid), 4),
        fees_usd=round(-ledger.fees_usd, 4),
        net_pnl_usd=round(ledger.net_pnl_usd(last_mid), 4),
        vw_markout_5s_bps=_compute_vw_markout(fills, mids_series, 5_000),
        vw_markout_30s_bps=_compute_vw_markout(fills, mids_series, 30_000),
        halt_events=halt_events[:20],
        final_cleanup_cancelled=cleanup_cancelled,
        flattened_exit_sz=round(flattened_exit_sz, spec.sz_decimals),
        dead_man_refreshes=dead_man_refreshes,
        dms_left_armed=dms_left_armed,
    )
    return summary, pd.DataFrame(fills), pd.DataFrame(quote_rows)


# --------------------------------------------------------------------------- Calibration & Grid Sweep


def calibrate_as_arrival_intensity(
    l2_df: pd.DataFrame,
    trades_df: pd.DataFrame | None = None,
    *,
    coin: str = "ETH",
    horizon_sec: float = 30.0,
    window_sec: float = 5.0,
    deltas_bps: tuple[float, ...] = (0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0, 16.0),
    step_sec: float | None = None,
    maker_fee_bps: float = 1.5,
    min_windows_ok: int = 50,
    min_r2_ok: float = 0.5,
) -> ArrivalCalibrationResult:
    """Calibrate Avellaneda–Stoikov ``(kappa, A)`` and 1s volatility from recorded L2 + trades.

    Uses Poisson hazard inversion over stepped quoting windows ``[t_j, t_j + window_sec]``:
    ``lambda(delta) = -ln(1 - p_fill(delta)) / window_sec``, then fits
    ``ln(lambda(delta)) = ln(A) - kappa * delta`` via weighted least squares.
    """
    if l2_df.empty:
        raise ValueError("l2_df is empty")
    ts_col = "recv_ms" if "recv_ms" in l2_df.columns else "book_time_ms"
    df = l2_df.sort_values(ts_col).reset_index(drop=True)
    ts = df[ts_col].to_numpy(dtype=np.int64)
    bids = df["best_bid"].to_numpy(dtype=np.float64)
    asks = df["best_ask"].to_numpy(dtype=np.float64)
    mids = 0.5 * (bids + asks)
    spreads_bps = (asks - bids) / np.maximum(mids, 1e-12) * 1e4
    duration_sec = max(1.0, (int(ts[-1]) - int(ts[0])) / 1000.0)

    # 1. Resampled 1-second mid-price realized volatility (avoids tick bid-ask bounce bias)
    sec_df = pd.DataFrame({"sec": ts // 1000, "mid": mids}).groupby("sec", as_index=True)["mid"].last()
    if len(sec_df) >= 3:
        full_idx = np.arange(int(sec_df.index[0]), int(sec_df.index[-1]) + 1)
        sec_aligned = sec_df.reindex(full_idx).ffill(limit=5).dropna()
        rets_bps = np.diff(np.log(sec_aligned.to_numpy(dtype=np.float64))) * 1e4
        rets_bps = np.clip(rets_bps, -100.0, 100.0)
        sigma_1s = float(np.std(rets_bps, ddof=1)) if len(rets_bps) >= 2 else 1.5
    else:
        sigma_1s = 1.5
    sigma_1s = max(0.1, sigma_1s)
    sigma_tau = sigma_1s * math.sqrt(max(1.0, horizon_sec))

    # 2. Prepare trades arrays if available
    tr_ts = np.array([], dtype=np.int64)
    tr_px = np.array([], dtype=np.float64)
    tr_is_buy = np.array([], dtype=bool)
    if trades_df is not None and not trades_df.empty:
        t_col = "trade_time_ms" if "trade_time_ms" in trades_df.columns else "recv_ms"
        tdf = trades_df.sort_values(t_col)
        tr_ts = tdf[t_col].to_numpy(dtype=np.int64)
        tr_px = tdf["px"].to_numpy(dtype=np.float64)
        tr_is_buy = tdf["side"].astype(str).isin({"buy", "B"}).to_numpy()

    # 3. Quoting windows. Default step = window length (non-overlapping): overlapping windows
    #    share fills, violate the independent-trial assumption of the hazard inversion and
    #    inflate the apparent sample size / R^2.
    win_ms = int(max(1.0, window_sec) * 1000)
    step_ms = int(max(1.0, step_sec if step_sec is not None else window_sec) * 1000)
    t_start = int(ts[0])
    t_end = int(ts[-1]) - win_ms
    epochs = list(range(t_start, max(t_start + 1, t_end + 1), step_ms))
    if not epochs:
        epochs = [t_start]

    # Precompute window extrema per epoch for fast O(N log N) sweep over all deltas
    bid_excursions_bps: list[float] = []
    ask_excursions_bps: list[float] = []
    for t0 in epochs:
        i0 = int(np.searchsorted(ts, t0, side="left"))
        i1 = int(np.searchsorted(ts, t0 + win_ms, side="right"))
        if i0 >= len(ts):
            continue
        m0 = float(mids[i0])
        if m0 <= 0:
            continue
        lo_px = float(np.min(asks[i0:max(i0 + 1, i1)]))
        hi_px = float(np.max(bids[i0:max(i0 + 1, i1)]))
        if len(tr_ts) > 0:
            j0 = int(np.searchsorted(tr_ts, t0, side="left"))
            j1 = int(np.searchsorted(tr_ts, t0 + win_ms, side="right"))
            if j1 > j0:
                sells = tr_px[j0:j1][~tr_is_buy[j0:j1]]
                buys = tr_px[j0:j1][tr_is_buy[j0:j1]]
                if len(sells) > 0:
                    lo_px = min(lo_px, float(np.min(sells)))
                if len(buys) > 0:
                    hi_px = max(hi_px, float(np.max(buys)))
        bid_excursions_bps.append((m0 - lo_px) / m0 * 1e4)
        ask_excursions_bps.append((hi_px - m0) / m0 * 1e4)

    exc = np.array(bid_excursions_bps + ask_excursions_bps, dtype=np.float64)
    n_trials = max(1, len(exc))
    raw_curve: list[dict[str, float]] = []
    fit_x: list[float] = []
    fit_y: list[float] = []
    fit_w: list[float] = []

    for d in deltas_bps:
        hits = int(np.sum(exc >= d))
        p_hit = hits / n_trials
        # Haldane-Anscombe (+0.5) keeps zero-hit deltas in the fit instead of dropping them,
        # which biased kappa low (only the near-touch, high-hit deltas survived).
        p_eff = min((hits + 0.5) / (n_trials + 1.0), 1.0 - 0.5 / n_trials)
        lam = -math.log(max(1.0 - p_eff, 1e-9)) / max(1.0, window_sec)
        fit_x.append(float(d))
        fit_y.append(math.log(max(lam, 1e-9)))
        fit_w.append(float(hits) + 0.5)
        if hits == 0:
            lam = 0.0
        raw_curve.append({"delta_bps": float(d), "hits": float(hits), "fill_prob": round(p_hit, 4), "lambda_per_sec": round(lam, 6)})

    # 4. Weighted log-linear regression: ln(lambda) = ln(A) - kappa * delta
    if len(fit_x) >= 2:
        x = np.array(fit_x, dtype=np.float64)
        y = np.array(fit_y, dtype=np.float64)
        w = np.array(fit_w, dtype=np.float64)
        w_sum = float(np.sum(w))
        x_bar = float(np.sum(w * x) / w_sum)
        y_bar = float(np.sum(w * y) / w_sum)
        denom = float(np.sum(w * (x - x_bar) ** 2))
        slope = float(np.sum(w * (x - x_bar) * (y - y_bar)) / denom) if denom > 1e-12 else -0.35
        intercept = y_bar - slope * x_bar
        y_hat = intercept + slope * x
        ss_res = float(np.sum(w * (y - y_hat) ** 2))
        ss_tot = float(np.sum(w * (y - y_bar) ** 2))
        r2 = max(0.0, 1.0 - ss_res / ss_tot) if ss_tot > 1e-12 else 0.0
        kappa = min(max(-slope, 0.05), 2.5)
        a_rate = math.exp(min(max(intercept, -10.0), 5.0))
    else:
        kappa = 0.35
        a_rate = 0.25
        r2 = 0.0

    depth_curve = [
        {**row, "fitted_lambda": round(a_rate * math.exp(-kappa * row["delta_bps"]), 6)}
        for row in raw_curve
    ]

    return ArrivalCalibrationResult(
        coin=coin,
        duration_sec=round(duration_sec, 2),
        l2_ticks=len(df),
        trades_count=len(tr_ts),
        empirical_sigma_1s_bps=round(sigma_1s, 3),
        empirical_sigma_tau_bps=round(sigma_tau, 2),
        horizon_sec=float(horizon_sec),
        median_book_spread_bps=round(float(np.median(spreads_bps)), 3),
        kappa_per_bp=round(kappa, 4),
        arrival_a_per_sec=round(a_rate, 4),
        r2_fit=round(r2, 4),
        # Minimum profitable half-spread: adverse-selection term 1/kappa plus the maker fee paid
        # on each fill (the inventory term 0.5*gamma*sigma^2*tau is added by the quoting engine).
        implied_min_half_spread_bps=round(1.0 / kappa + max(0.0, maker_fee_bps), 2),
        depth_curve=depth_curve,
        adverse_term_bps=round(1.0 / kappa, 2),
        maker_fee_bps=float(maker_fee_bps),
        n_windows=len(bid_excursions_bps),
        fit_points=int(sum(1 for row in raw_curve if row["hits"] > 0)),
        observed_at_ms=int(ts[-1]),
        calibration_ok=bool(
            len(bid_excursions_bps) >= min_windows_ok
            and r2 >= min_r2_ok
            and sum(1 for row in raw_curve if row["hits"] > 0) >= 4
        ),
    )


def sweep_as_parameters_on_l2(
    l2_df: pd.DataFrame,
    spec: AssetSpec,
    *,
    trades_df: pd.DataFrame | None = None,
    gammas: tuple[float, ...] = (0.01, 0.03, 0.08, 0.15),
    kappas: tuple[float, ...] = (0.20, 0.35, 0.60),
    base_params: ASParams = ASParams(),
) -> pd.DataFrame:
    """Grid-sweep ``(gamma, kappa)`` on recorded L2 + trades to compare spread capture & markout."""
    rows: list[dict[str, Any]] = []
    for g in gammas:
        for k in sorted({round(float(kv), 4) for kv in kappas if kv > 0}):
            p = ASParams(**{**base_params.to_dict(), "gamma": float(g), "kappa": float(k)})
            summ, _, _ = simulate_avellaneda_stoikov_on_l2(l2_df, spec, trades_df=trades_df, params=p)
            rows.append(
                {
                    "gamma": float(g),
                    "kappa": float(k),
                    "fills": summ.fills_count,
                    "buy_fills": summ.buy_fills,
                    "sell_fills": summ.sell_fills,
                    "volume_usd": summ.volume_usd,
                    "avg_half_spread_bps": summ.avg_half_spread_bps,
                    "max_abs_inv_usd": summ.max_abs_inventory_usd,
                    "spread_pnl_usd": summ.realized_spread_pnl_usd,
                    "mtm_pnl_usd": summ.inventory_mtm_pnl_usd,
                    "fees_usd": summ.fees_usd,
                    "net_pnl_usd": summ.net_pnl_usd,
                    "markout_5s_bps": summ.vw_markout_5s_bps,
                    "markout_30s_bps": summ.vw_markout_30s_bps,
                }
            )
    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["net_pnl_usd", "fills"], ascending=[False, False]).reset_index(drop=True)
    return out


# --------------------------------------------------------------------------- Live WebSocket MM Loop (v2: Lead-Lag + batchModify + scheduleCancel + Multi-Coin)

BINANCE_FUTURES_STREAM_URL = "wss://fstream.binance.com/stream"


def binance_futures_symbol(coin: str) -> str:
    """Map Hyperliquid perp symbol to Binance USD-M Futures symbol (e.g. ``kPEPE`` -> ``1000PEPEUSDT``)."""
    if len(coin) > 1 and coin.startswith("k") and coin[1].isupper():
        return f"1000{coin[1:].upper()}USDT"
    return f"{coin.upper()}USDT"


async def _binance_lead_reader(
    coins: list[str],
    estimators: dict[str, LeadLagEstimator],
    stop_event: asyncio.Event,
) -> None:
    """Background Binance Futures ``bookTicker`` WS reader with auto-reconnect and ``k*`` mapping."""
    import websockets

    sym_to_coin = {binance_futures_symbol(c): c for c in coins if c}
    if not sym_to_coin:
        return
    streams = "/".join(f"{sym.lower()}@bookTicker" for sym in sym_to_coin)
    url = f"{BINANCE_FUTURES_STREAM_URL}?streams={streams}"
    failures = 0
    while not stop_event.is_set():
        try:
            async with websockets.connect(url, open_timeout=6, ping_interval=20) as ws:
                while not stop_event.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1.0)
                    except asyncio.TimeoutError:
                        continue
                    failures = 0
                    msg = json.loads(raw)
                    data = msg.get("data") if isinstance(msg, dict) and "data" in msg else msg
                    if not isinstance(data, dict):
                        continue
                    coin = sym_to_coin.get(str(data.get("s", "")).upper())
                    if coin is None:
                        continue
                    bid = float(data.get("b", 0.0) or 0.0)
                    ask = float(data.get("a", 0.0) or 0.0)
                    if bid > 0 and ask >= bid:
                        estimators[coin].update_cex(int(time.time() * 1000), 0.5 * (bid + ask))
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set():
                return
            failures += 1
            backoff_s = min(5.0, 0.5 * (2 ** min(failures - 1, 4)))
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=backoff_s)
            except asyncio.TimeoutError:
                continue


_MM_CANCEL_GONE_MARKERS = ("already canceled", "already cancelled", "filled", "never placed")


def _cancel_response_ok(cancel_resps: Any) -> bool:
    """Return True when an explicit cancel confirmed the order was removed without error.

    Returns False when the cancel reported an exchange error (e.g. order already filled/cancelled
    or rate-limited), so failed ``batchModify`` fallback never re-places on stale inventory or
    stacks a duplicate quote on top of an uncancelled resting order.
    """
    if isinstance(cancel_resps, int):
        return cancel_resps > 0
    if not isinstance(cancel_resps, list) or not cancel_resps:
        return False
    for item in cancel_resps:
        if not isinstance(item, dict):
            continue
        if item.get("status") == "err" or item.get("error"):
            return False
        if item.get("mode") == "dry_run":
            continue
        sdk_resp = _unwrap_sdk_response(item)
        if sdk_resp is not None:
            statuses, err_msg = extract_hl_order_statuses(sdk_resp)
            if err_msg is not None:
                return False
            for st in statuses:
                if isinstance(st, dict) and st.get("error"):
                    return False
                if isinstance(st, str) and "error" in st.lower():
                    return False
    return True


def _cancel_response_confirmed_gone(cancel_resps: Any) -> bool:
    """Return True when every cancel status indicates the order was already filled/cancelled/gone.

    Distinguishes an order that is verifiably no longer resting on the book (safe to drop from
    ``active_orders``, unsafe to immediately re-quote before fill ingestion) from an HTTP 429 or
    transport timeout where ``prev_oid`` may STILL rest live on the book (must keep ``prev_oid`` in
    ``active_orders`` so subsequent ticks never stack duplicate orders).
    """
    if not isinstance(cancel_resps, list) or not cancel_resps:
        return False
    saw_gone = False
    for item in cancel_resps:
        if not isinstance(item, dict) or item.get("status") == "err" or item.get("error"):
            return False
        sdk_resp = _unwrap_sdk_response(item)
        if sdk_resp is None:
            return False
        statuses, err_msg = extract_hl_order_statuses(sdk_resp)
        if err_msg is not None or not statuses:
            return False
        for st in statuses:
            if st == "success":
                continue
            if isinstance(st, dict) and st.get("error"):
                err_l = str(st.get("error")).lower()
                if any(m in err_l for m in _MM_CANCEL_GONE_MARKERS):
                    saw_gone = True
                    continue
            return False
    return saw_gone


async def _sync_coin_quotes_atomic(
    coin: str,
    dec: MMQuoteDecision,
    active_orders: dict[str, dict[str, Any]],
    all_mm_oids: set[int],
    adapter: Any,
    *,
    mid_px: float,
    params: ASParams,
    recv_ms: int,
    audit_log_path: Path | None,
    prefer_explicit_cancel: bool = False,
    execute_live: bool = False,
    ambiguous_out: list[dict[str, Any]] | None = None,
) -> tuple[int, int, int, int]:
    """Synchronize bid/ask for one coin using HyperCore priority-aware cancel/modify/place.

    HyperCore sorts explicit ``cancel`` ahead of incoming taker IOCs inside a block,
    whereas ``modify`` (``batchModify``) does NOT receive cancel priority. Therefore:
    - Routine calm requotes (``prefer_explicit_cancel=False``) use atomic ``modify_bulk_alo_orders``
      to conserve API rate-limit budget (1 request per 1 USDC traded).
    - Urgent toxic-flow / Binance Lead-Lag defensive moves (``prefer_explicit_cancel=True``)
      bypass ``modify`` and send explicit ``cancel`` first so stale quotes beat incoming snipers,
      then place fresh ``Alo`` quotes.

    Returns ``(placed_cnt, modified_cnt, cancelled_cnt, skipped_cnt)``.
    """
    desired = {
        "buy": (dec.bid_px, dec.bid_sz, dec.bid_reduce_only),
        "sell": (dec.ask_px, dec.ask_sz, dec.ask_reduce_only),
    }
    to_cancel: list[dict[str, Any]] = []
    cancel_sides: list[str] = []
    to_modify: list[tuple[str, int, PlannedOrder]] = []
    to_place: list[tuple[str, PlannedOrder]] = []
    deferred_after_cancel: list[tuple[str, PlannedOrder]] = []
    skipped = 0

    for side_k, (d_px, d_sz, d_red) in desired.items():
        cur_ord = active_orders.get(side_k)
        cur_tuple = (
            (cur_ord["px"], cur_ord["sz"], bool(cur_ord.get("reduce_only", False))) if cur_ord else None
        )
        if not should_requote_side(
            d_px,
            d_sz,
            cur_tuple,
            mid_px=mid_px,
            threshold_bps=params.requote_threshold_bps,
            desired_reduce_only=d_red,
        ):
            skipped += 1
            continue
        if d_px is None or d_sz <= 0:
            if cur_ord is not None:
                if cur_ord.get("oid") is not None:
                    to_cancel.append({"coin": coin, "oid": cur_ord["oid"]})
                    cancel_sides.append(side_k)
                else:
                    active_orders.pop(side_k, None)
            continue
        order = PlannedOrder(
            coin=coin,
            side=side_k,
            limit_px=d_px,
            sz=d_sz,
            notional_usd=round(d_px * d_sz, 2),
            tif="Alo",
            reduce_only=d_red,
        )
        can_use_modify = (
            not prefer_explicit_cancel
            and cur_ord is not None
            and cur_ord.get("oid") is not None
            and hasattr(adapter, "modify_bulk_alo_orders")
        )
        if can_use_modify:
            to_modify.append((side_k, int(cur_ord["oid"]), order))
        else:
            if cur_ord is not None and cur_ord.get("oid") is not None:
                to_cancel.append({"coin": coin, "oid": cur_ord["oid"]})
                cancel_sides.append(side_k)
                deferred_after_cancel.append((side_k, order))
            else:
                if cur_ord is not None:
                    active_orders.pop(side_k, None)
                to_place.append((side_k, order))

    cancelled = 0
    if to_cancel:
        try:
            if prefer_explicit_cancel:
                try:
                    cancel_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, to_cancel, fast=True)
                except TypeError:
                    cancel_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, to_cancel)
            else:
                cancel_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, to_cancel)
        except Exception as exc:
            cancel_resps = [{"status": "err", "response": str(exc)}]
        if _cancel_response_ok(cancel_resps):
            for side_k in cancel_sides:
                active_orders.pop(side_k, None)
            cancelled += len(to_cancel)
            to_place.extend(deferred_after_cancel)
        elif _cancel_response_confirmed_gone(cancel_resps):
            for side_k in cancel_sides:
                active_orders.pop(side_k, None)

    modified = 0
    if to_modify:
        try:
            mod_resps = await asyncio.to_thread(adapter.modify_bulk_alo_orders, [(oid, o) for _, oid, o in to_modify])
            if not isinstance(mod_resps, list):
                mod_resps = [{"status": "invalid_response", "response": "invalid_modify_response"} for _ in to_modify]
        except Exception as exc:
            mod_resps = [
                {"status": f"ambiguous_exception:{type(exc).__name__}", "response": f"{type(exc).__name__}: {exc}"}
                for _ in to_modify
            ]
        if len(mod_resps) < len(to_modify):
            mod_resps = list(mod_resps) + [
                {"status": "unknown", "response": "missing_modify_status"} for _ in range(len(to_modify) - len(mod_resps))
            ]
        for (side_k, prev_oid, order), resp in zip(to_modify, mod_resps, strict=False):
            new_oid, status = _extract_order_oid(resp)
            if status in {"resting", "simulated_alo"} and new_oid is not None:
                modified += 1
                all_mm_oids.add(new_oid)
                active_orders[side_k] = {
                    "oid": new_oid,
                    "px": order.limit_px,
                    "sz": order.sz,
                    "reduce_only": bool(order.reduce_only),
                }
            else:
                # Modify rejected/failed: prev_oid may STILL rest live on the book (e.g. ALO cross
                # rejection, HTTP 429, or transport timeout) OR may have already filled/cancelled.
                # Never pop active_orders[side_k] before cancel confirms removal; otherwise a failed
                # cancel leaves prev_oid resting invisibly and the next tick stacks a duplicate order!
                if new_oid is not None:
                    all_mm_oids.add(new_oid)
                cancel_req = [{"coin": coin, "oid": prev_oid}]
                try:
                    cancel_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, cancel_req, fast=True)
                except TypeError:
                    try:
                        cancel_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, cancel_req)
                    except Exception as exc:
                        cancel_resps = [{"status": "err", "response": f"{type(exc).__name__}: {exc}"}]
                except Exception as exc:
                    cancel_resps = [{"status": "err", "response": f"{type(exc).__name__}: {exc}"}]
                if _cancel_response_ok(cancel_resps):
                    active_orders.pop(side_k, None)
                    cancelled += 1
                    to_place.append((side_k, order))
                elif _cancel_response_confirmed_gone(cancel_resps):
                    # Order already filled/cancelled (or replaced by a timed-out modify): drop prev_oid and reconcile before re-quoting.
                    active_orders.pop(side_k, None)
                    if execute_live and new_oid is None and _is_ambiguous_order_status(status) and ambiguous_out is not None:
                        ambiguous_out.append(
                            {
                                "side": side_k,
                                "px": float(order.limit_px),
                                "sz": float(order.sz),
                                "reduce_only": bool(order.reduce_only),
                                "ts_ms": recv_ms,
                            }
                        )
            if audit_log_path is not None:
                is_dry = isinstance(adapter, DryRunAdapter) or (
                    isinstance(resp, dict) and resp.get("mode") == "dry_run"
                )
                audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                with audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts_ms": recv_ms,
                                "mode": "dry_run" if is_dry else "ws_modify",
                                "action": "ws_modify",
                                "coin": coin,
                                "prev_oid": prev_oid,
                                "oid": new_oid,
                                "status": status,
                                "order": order.to_dict(),
                            },
                            default=str,
                        )
                        + "\n"
                    )

    placed = 0
    if to_place:
        if hasattr(adapter, "place_bulk_alo_orders"):
            try:
                pl_resps = await asyncio.to_thread(adapter.place_bulk_alo_orders, [o for _, o in to_place])
                if not isinstance(pl_resps, list):
                    pl_resps = [{"status": "invalid_response", "response": "invalid_bulk_response"} for _ in to_place]
            except Exception as exc:
                pl_resps = [
                    {"status": f"ambiguous_exception:{type(exc).__name__}", "response": f"{type(exc).__name__}: {exc}"}
                    for _ in to_place
                ]
        else:
            pl_resps = []
            for _, o in to_place:
                try:
                    pl_resps.append(await asyncio.to_thread(adapter.place_alo_order, o))
                except Exception as exc:
                    pl_resps.append(
                        {"status": f"ambiguous_exception:{type(exc).__name__}", "response": f"{type(exc).__name__}: {exc}"}
                    )
        if len(pl_resps) < len(to_place):
            pl_resps = list(pl_resps) + [
                {"status": "unknown", "response": "missing_bulk_status"} for _ in range(len(to_place) - len(pl_resps))
            ]
        for (side_k, order), resp in zip(to_place, pl_resps, strict=False):
            oid, status = _extract_order_oid(resp)
            is_ambig = execute_live and oid is None and _is_ambiguous_order_status(status)
            if not _is_ambiguous_order_status(status) and not status.startswith("err"):
                placed += 1
            if oid is not None:
                all_mm_oids.add(oid)
            if status in {"resting", "simulated_alo"}:
                active_orders[side_k] = {
                    "oid": oid,
                    "px": order.limit_px,
                    "sz": order.sz,
                    "reduce_only": bool(order.reduce_only),
                }
            elif is_ambig and ambiguous_out is not None:
                ambiguous_out.append(
                    {
                        "side": side_k,
                        "px": float(order.limit_px),
                        "sz": float(order.sz),
                        "reduce_only": bool(order.reduce_only),
                        "ts_ms": recv_ms,
                    }
                )
            if audit_log_path is not None:
                is_dry = isinstance(adapter, DryRunAdapter) or (
                    isinstance(resp, dict) and resp.get("mode") == "dry_run"
                )
                audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                with audit_log_path.open("a", encoding="utf-8") as fh:
                    fh.write(
                        json.dumps(
                            {
                                "ts_ms": recv_ms,
                                "mode": "dry_run" if is_dry else "ws_bulk",
                                "action": "ws_bulk",
                                "coin": coin,
                                "oid": oid,
                                "status": status,
                                "order": order.to_dict(),
                            },
                            default=str,
                        )
                        + "\n"
                    )

    return placed, modified, cancelled, skipped


async def run_live_mm_ws_session(
    coin: str,
    adapter: ExchangeAdapter,
    *,
    account_address: str | None = None,
    execute_live: bool = False,
    duration_sec: float = 35.0,
    min_action_interval_ms: int = 400,
    flatten_on_exit: bool = False,
    enable_lead_lag: bool = True,
    enable_dead_man_switch: bool = True,
    dead_man_timeout_ms: int = 15_000,
    params: ASParams = ASParams(),
    ws_url: str = TESTNET_WS_URL,
    audit_log_path: Path | None = None,
    client: httpx.Client | None = None,
    ws_messages: list[dict[str, Any]] | None = None,
) -> tuple[MMSessionSummary, pd.DataFrame, pd.DataFrame]:
    """Sub-second WebSocket Avellaneda–Stoikov MM loop (``bbo + l2Book + trades + userFills + orderUpdates``).

    Includes Binance Futures Lead-Lag fair price + toxic-side pull, atomic ``bulk_orders`` /
    ``bulk_modify_orders_new``, native Hyperliquid ``scheduleCancel`` Dead Man's Switch, and
    guaranteed ``finally:`` cancel-all + optional ``flatten_on_exit``.
    """
    res = await run_multi_coin_mm_ws_session(
        [coin],
        adapter,
        account_address=account_address,
        execute_live=execute_live,
        duration_sec=duration_sec,
        min_action_interval_ms=min_action_interval_ms,
        flatten_on_exit=flatten_on_exit,
        enable_lead_lag=enable_lead_lag,
        enable_dead_man_switch=enable_dead_man_switch,
        dead_man_timeout_ms=dead_man_timeout_ms,
        params=params,
        ws_url=ws_url,
        audit_log_path=audit_log_path,
        client=client,
        ws_messages=ws_messages,
    )
    return res["summaries"][coin], res["fills_df"], res["quotes_df"]


async def run_multi_coin_mm_ws_session(
    coins: list[str],
    adapter: ExchangeAdapter,
    *,
    account_address: str | None = None,
    execute_live: bool = False,
    duration_sec: float = 35.0,
    min_action_interval_ms: int = 400,
    flatten_on_exit: bool = False,
    enable_lead_lag: bool = True,
    enable_dead_man_switch: bool = True,
    dead_man_timeout_ms: int = 15_000,
    params: ASParams = ASParams(),
    ws_url: str = TESTNET_WS_URL,
    audit_log_path: Path | None = None,
    client: httpx.Client | None = None,
    ws_messages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Multi-coin beta-neutral WebSocket Avellaneda–Stoikov MM session in one async event loop.

    Cross-hedges correlated inventory across ``coins`` via ``params.portfolio_beta_coupling``,
    streams concurrent Binance Futures ``bookTicker`` lead-lag fair prices, uses atomic
    ``bulk_orders`` / ``batchModify`` + ``scheduleCancel`` heartbeat, and cleanly cancels
    (and optionally flattens) every coin in ``finally:``.
    """
    coin_list = list(dict.fromkeys(c.strip() for c in coins if c.strip()))
    if not coin_list:
        raise ValueError("coins must be non-empty")

    owns_client = client is None
    http = client or httpx.Client(timeout=15.0)
    started_ms = int(time.time() * 1000)

    specs: dict[str, AssetSpec] = {}
    active_orders: dict[str, dict[str, dict[str, Any]]] = {c: {} for c in coin_list}
    all_mm_oids: dict[str, set[int]] = {c: set() for c in coin_list}
    seen_fill_keys: set[tuple[Any, ...]] = set()
    ledgers: dict[str, _InventoryLedger] = {c: _InventoryLedger(maker_fee_bps=params.maker_fee_bps) for c in coin_list}
    vol_ests: dict[str, EWMAVolatilityEstimator] = {
        c: EWMAVolatilityEstimator(
            horizon_sec=params.horizon_sec,
            half_life_sec=params.vol_half_life_sec,
            default_sigma_bps=params.default_sigma_bps,
        )
        for c in coin_list
    }
    lead_ests: dict[str, LeadLagEstimator] = {c: LeadLagEstimator() for c in coin_list}
    flow_ests: dict[str, OrderFlowToxicityEstimator] = {
        c: OrderFlowToxicityEstimator(window_sec=params.vpin_window_sec, min_volume_usd=params.vpin_min_volume_usd)
        for c in coin_list
    }
    latest_imb5: dict[str, float] = {c: 0.0 for c in coin_list}
    last_mids: dict[str, float] = {c: 0.0 for c in coin_list}
    last_action_mono: dict[str, float] = {c: 0.0 for c in coin_list}

    stats: dict[str, dict[str, Any]] = {
        c: {
            "ticks": 0,
            "bbo_ticks": 0,
            "placed": 0,
            "modified": 0,
            "cancelled": 0,
            "skipped": 0,
            "lead_pulls": 0,
            "urgent_cancels": 0,
            "vpin_widen_events": 0,
            "rate_limit_throttles": 0,
            "cleanup_cancelled": 0,
            "dms_left_armed": False,
            "flattened_exit_sz": 0.0,
            "half_spreads": [],
            "sigmas": [],
            "mids_series": [],
            "fills": [],
            "halt_events": [],
        }
        for c in coin_list
    }
    quote_rows: list[dict[str, Any]] = []
    dead_man_refreshes = 0
    dead_man_armed = False
    lead_stop = asyncio.Event()
    lead_task: asyncio.Task[None] | None = None

    try:
        meta = fetch_testnet_meta(http)
        for c in coin_list:
            if c not in meta:
                raise ValueError(f"coin {c!r} not listed on Hyperliquid Testnet")
            if meta[c].only_isolated:
                raise ValueError(f"refusing to market-make onlyIsolated asset {c!r} ({meta[c].margin_mode})")
            specs[c] = meta[c]

        # Best-effort live funding APR lookup for :59 -> :05 hourly settlement quote-skew overlay
        funding_apr_by_coin: dict[str, float] = {}
        try:
            mc_r = http.post(TESTNET_INFO_URL, json={"type": "metaAndAssetCtxs"})
            if mc_r.status_code == 200 and isinstance(mc_r.json(), list) and len(mc_r.json()) == 2:
                mc_meta, mc_ctxs = mc_r.json()
                for u, ctx in zip(mc_meta.get("universe") or [], mc_ctxs or [], strict=False):
                    u_name = str(u.get("name") or "")
                    if u_name in specs:
                        funding_apr_by_coin[u_name] = float(ctx.get("funding", 0.0) or 0.0) * 24.0 * 365.0 * 100.0
        except Exception:
            funding_apr_by_coin = {}

        mempool_priority_ok = True
        ambiguous_by_coin: dict[str, list[dict[str, Any]]] = {c: [] for c in coin_list}
        ws_fills_unsynced = False
        ws_live_init_ok = True
        if execute_live:
            if account_address:
                # HyperCore mempool cancel/ALO prioritization is ONLY active in Standard account mode;
                # Unified Account / Portfolio Margin disables mempool priority!
                try:
                    ua_r = http.post(TESTNET_INFO_URL, json={"type": "userAbstraction", "user": account_address})
                    if ua_r.status_code == 200 and isinstance(ua_r.json(), str):
                        ua_mode = str(ua_r.json())
                        if ua_mode in {"unifiedAccount", "portfolioMargin"}:
                            mempool_priority_ok = False
                            for c in coin_list:
                                stats[c]["halt_events"].append(f"mempool_priority_disabled:{ua_mode}")
                except Exception:
                    pass
            ch = fetch_clearinghouse_state_checked(http, account_address) if account_address else None
            if ch is None:
                ws_live_init_ok = False
                for c in coin_list:
                    stats[c]["halt_events"].append("init_clearinghouse_unavailable")
            else:
                pos_by_coin = {
                    str((e.get("position") or {}).get("coin")): float((e.get("position") or {}).get("szi", 0.0))
                    for e in (ch.get("assetPositions") or [])
                    if isinstance(e, dict)
                }
                for c in coin_list:
                    ledgers[c] = _InventoryLedger(start_sz=pos_by_coin.get(c, 0.0), maker_fee_bps=params.maker_fee_bps)
                    try:
                        await asyncio.to_thread(adapter.set_leverage, c, max(1, min(3, specs[c].max_leverage)), is_cross=True)
                    except Exception:
                        pass

        async def _refresh_dead_man(now_ms: int) -> None:
            nonlocal dead_man_refreshes, dead_man_armed
            if not (enable_dead_man_switch and hasattr(adapter, "schedule_cancel")):
                return
            try:
                resp = await asyncio.to_thread(adapter.schedule_cancel, now_ms + max(6_000, dead_man_timeout_ms))
                inner = resp.get("response") if isinstance(resp, dict) else None
                if (isinstance(resp, dict) and resp.get("status") == "ok") or (isinstance(inner, dict) and inner.get("status") == "ok"):
                    dead_man_refreshes += 1
                    dead_man_armed = True
            except Exception:
                return

        if ws_live_init_ok and enable_dead_man_switch:
            await _refresh_dead_man(int(time.time() * 1000))

        if ws_live_init_ok and enable_lead_lag and ws_messages is None:
            lead_task = asyncio.create_task(_binance_lead_reader(coin_list, lead_ests, lead_stop))

        init_mono = time.monotonic()
        last_valid_book_mono: dict[str, float] = {c: init_mono for c in coin_list}

        def _ingest_live_fill(fl: dict[str, Any], *, default_ts_ms: int, check_session_window: bool = False) -> None:
            c_name = str(fl.get("coin") or "")
            if c_name not in all_mm_oids:
                return
            try:
                f_oid = int(fl.get("oid", -1))
            except (TypeError, ValueError):
                return
            f_time = int(fl.get("time", default_ts_ms) or default_ts_ms)
            if f_oid not in all_mm_oids[c_name]:
                return
            if check_session_window and f_time < started_ms - 5_000:
                return
            f_sz = abs(float(fl.get("sz", 0.0)))
            f_px = float(fl.get("px", 0.0))
            f_side = "buy" if str(fl.get("side")) == "B" else "sell"
            key = _fill_dedup_key(fl, c_name, f_oid, f_side, f_sz, f_time)
            if key in seen_fill_keys:
                return
            seen_fill_keys.add(key)
            ledgers[c_name].on_fill(f_side, f_px, f_sz, fee_usd=float(fl.get("fee", 0.0)))
            stats[c_name]["fills"].append(
                {
                    "coin": c_name,
                    "ts_ms": f_time,
                    "oid": f_oid,
                    "side": f_side,
                    "px": f_px,
                    "sz": f_sz,
                    "notional_usd": f_px * f_sz,
                    "crossed": bool(fl.get("crossed", False)),
                }
            )

        async def _handle_message(msg: dict[str, Any], recv_ms: int) -> bool:
            channel = msg.get("channel")
            data = msg.get("data")

            if channel == "cexLead" and isinstance(data, dict):
                c_name = str(data.get("coin", ""))
                if c_name in lead_ests:
                    lead_ests[c_name].update_cex(recv_ms, float(data.get("mid", 0.0)))
                return True

            if channel == "userFills" and isinstance(data, dict) and execute_live:
                nonlocal ws_fills_unsynced
                # Process both streaming and reconnect isSnapshot=True fills strictly filtered by all_mm_oids + seen_fill_keys.
                for fl in data.get("fills") or []:
                    if isinstance(fl, dict):
                        _ingest_live_fill(fl, default_ts_ms=recv_ms, check_session_window=False)
                if data.get("isSnapshot") is True:
                    ws_fills_unsynced = False
                return True

            if channel == "orderUpdates" and isinstance(data, list) and execute_live:
                for upd in data:
                    if not isinstance(upd, dict):
                        continue
                    status_str = str(upd.get("status") or "")
                    ord_obj = upd.get("order") or {}
                    c_name = str(ord_obj.get("coin") or "")
                    oid = ord_obj.get("oid")
                    if c_name in active_orders and oid is not None:
                        for side_k in list(active_orders[c_name].keys()):
                            if active_orders[c_name][side_k].get("oid") == int(oid):
                                if status_str == "open":
                                    rem_sz = ord_obj.get("sz")
                                    if rem_sz is not None:
                                        rem_val = float(rem_sz)
                                        if rem_val > 0:
                                            active_orders[c_name][side_k]["sz"] = rem_val
                                        else:
                                            active_orders[c_name].pop(side_k, None)
                                else:
                                    active_orders[c_name].pop(side_k, None)
                return True

            if channel == "trades" and isinstance(data, list):
                for tr in data:
                    if not isinstance(tr, dict):
                        continue
                    c_name = str(tr.get("coin") or "")
                    if c_name not in active_orders:
                        continue
                    t_side = str(tr.get("side", ""))
                    t_px = float(tr.get("px", 0.0))
                    t_sz = abs(float(tr.get("sz", 0.0)))
                    if t_px > 0 and t_sz > 0 and c_name in flow_ests:
                        flow_ests[c_name].update(recv_ms, t_side, t_px * t_sz)
                    if not execute_live:
                        ao = active_orders[c_name]
                        if t_side == "A" and "buy" in ao and t_px <= ao["buy"]["px"]:
                            o = ao.pop("buy")
                            ledgers[c_name].on_fill("buy", o["px"], o["sz"])
                            stats[c_name]["fills"].append({"coin": c_name, "ts_ms": recv_ms, "oid": o.get("oid"), "side": "buy", "px": o["px"], "sz": o["sz"], "notional_usd": o["px"] * o["sz"], "crossed": False})
                        elif t_side == "B" and "sell" in ao and t_px >= ao["sell"]["px"]:
                            o = ao.pop("sell")
                            ledgers[c_name].on_fill("sell", o["px"], o["sz"])
                            stats[c_name]["fills"].append({"coin": c_name, "ts_ms": recv_ms, "oid": o.get("oid"), "side": "sell", "px": o["px"], "sz": o["sz"], "notional_usd": o["px"] * o["sz"], "crossed": False})
                return True

            if channel not in {"bbo", "l2Book"} or not isinstance(data, dict):
                return True
            c_name = str(data.get("coin") or "")
            if c_name not in specs:
                return True

            if channel == "bbo":
                book = parse_bbo_snapshot(c_name, data, prev_imbalance5=latest_imb5[c_name], now_ms=recv_ms)
                if book is None:
                    return True
                stats[c_name]["bbo_ticks"] += 1
            else:
                book = parse_l2_snapshot(c_name, data, now_ms=recv_ms)
                if book is None:
                    return True
                latest_imb5[c_name] = book.imbalance5

            last_valid_book_mono[c_name] = time.monotonic()
            st = stats[c_name]
            st["ticks"] += 1
            last_mids[c_name] = book.mid_px
            _append_monotonic_mid(st["mids_series"], book.observed_ms if execute_live else recv_ms, book.mid_px)

            # Dry-run touch fills
            if not execute_live:
                ao = active_orders[c_name]
                for side_k in list(ao.keys()):
                    o = ao[side_k]
                    if (side_k == "buy" and book.best_ask <= o["px"]) or (side_k == "sell" and book.best_bid >= o["px"]):
                        ledgers[c_name].on_fill(side_k, o["px"], o["sz"])
                        st["fills"].append({"coin": c_name, "ts_ms": recv_ms, "oid": o.get("oid"), "side": side_k, "px": o["px"], "sz": o["sz"], "notional_usd": o["px"] * o["sz"], "crossed": False})
                        ao.pop(side_k, None)

            # Portfolio-level drawdown circuit breaker
            port_net_pnl = sum(ledgers[k].net_pnl_usd(last_mids[k]) for k in coin_list if last_mids[k] > 0)
            if port_net_pnl <= -abs(params.max_session_drawdown_usd):
                st["halt_events"].append(f"portfolio_drawdown_stop:{port_net_pnl:.2f}$")
                return False

            sigma_tau = vol_ests[c_name].update(book.observed_ms, book.mid_px)
            lead_imp = lead_ests[c_name].impulse_bps(recv_ms, book.mid_px) if enable_lead_lag else 0.0
            other_inv_usd = sum(ledgers[k].net_sz * last_mids[k] for k in coin_list if k != c_name and last_mids[k] > 0)

            adverse_halt: str | None = None
            tox_mult = 1.0
            if len(st["fills"]) >= 3:
                mk5 = _compute_vw_markout(st["fills"][-10:], st["mids_series"], 5_000)
                if mk5 is not None:
                    if mk5 <= params.adverse_markout_halt_bps:
                        adverse_halt = f"adverse_markout_halt:{mk5:.1f}bps"
                    elif mk5 <= params.adverse_markout_widen_bps:
                        tox_mult = 1.5
            cur_vpin = flow_ests[c_name].vpin(recv_ms)
            if cur_vpin >= params.vpin_widen_threshold:
                tox_mult = max(tox_mult, 1.35)
                st["vpin_widen_events"] += 1

            fund_ov_bps = compute_funding_settlement_overlay_bps(
                recv_ms,
                funding_apr_by_coin.get(c_name, 0.0),
                apr_threshold=params.funding_overlay_apr_threshold,
                skew_bps=params.funding_overlay_skew_bps,
            )
            dec = compute_as_quotes(
                book,
                specs[c_name],
                inventory_sz=ledgers[c_name].net_sz,
                sigma_tau_bps=sigma_tau,
                params=params,
                toxicity_mult=tox_mult,
                lead_impulse_bps=lead_imp,
                portfolio_other_inv_usd=other_inv_usd,
                funding_overlay_bps=fund_ov_bps,
                account_position_sz=ledgers[c_name].start_sz + ledgers[c_name].net_sz,
            )
            if abs(lead_imp) >= params.lead_pull_bps and params.lead_pull_bps > 0:
                st["lead_pulls"] += 1

            now_mono = time.monotonic()
            # Adaptive API rate-limit governor slows calm requotes before hitting 1 req / 1 USDC
            api_reqs_live = st["placed"] + st["modified"] + st["cancelled"]
            eff_interval_ms = compute_adaptive_action_interval_ms(
                min_action_interval_ms,
                api_reqs_live,
                ledgers[c_name].volume_usd,
                free_buffer_reqs=params.api_budget_free_buffer_reqs,
            )
            elapsed_ms = (now_mono - last_action_mono[c_name]) * 1000.0
            # Bypass rate-limit when lead-lag demands an urgent adverse-side pull
            urgent_pull = abs(lead_imp) >= params.lead_pull_bps and params.lead_pull_bps > 0
            can_act = urgent_pull or elapsed_ms >= eff_interval_ms or (ws_messages is not None)
            if not can_act and eff_interval_ms > min_action_interval_ms and elapsed_ms >= min_action_interval_ms:
                st["rate_limit_throttles"] += 1

            ws_reconcile_halt: str | None = None
            if execute_live and (ambiguous_by_coin[c_name] or ws_fills_unsynced):
                if not account_address:
                    st["halt_events"].append("ambiguous_placement_halt")
                    return False
                oo_chk = await asyncio.to_thread(fetch_open_orders_checked, http, account_address)
                uf_chk = await asyncio.to_thread(fetch_user_fills_checked, http, account_address)
                if uf_chk is not None:
                    if oo_chk is not None and ambiguous_by_coin[c_name]:
                        st["placed"] += _reconcile_ambiguous_submissions(
                            c_name,
                            ambiguous_by_coin[c_name],
                            oo=oo_chk,
                            uf=uf_chk,
                            active_orders=active_orders[c_name],
                            all_mm_oids=all_mm_oids[c_name],
                            started_ms=started_ms,
                        )
                    for fl in uf_chk:
                        _ingest_live_fill(fl, default_ts_ms=recv_ms, check_session_window=True)
                    ws_fills_unsynced = False
                if ambiguous_by_coin[c_name]:
                    ws_reconcile_halt = "ambiguous_placement_unreconciled"
                elif ws_fills_unsynced:
                    ws_reconcile_halt = "user_fills_unavailable"

            eff_halt = ws_reconcile_halt or adverse_halt or dec.halt_reason
            if eff_halt:
                st["halt_events"].append(eff_halt)
                to_c = [{"coin": c_name, "oid": o["oid"]} for o in active_orders[c_name].values() if o.get("oid") is not None]
                if to_c:
                    try:
                        c_resps = await asyncio.to_thread(adapter.cancel_all_open_orders, to_c)
                    except Exception as exc:
                        c_resps = [{"status": "err", "response": str(exc)}]
                    if _cancel_response_ok(c_resps):
                        st["cancelled"] += len(to_c)
                        active_orders[c_name].clear()
                    elif _cancel_response_confirmed_gone(c_resps):
                        active_orders[c_name].clear()
                else:
                    active_orders[c_name].clear()
                last_action_mono[c_name] = now_mono
            elif can_act:
                st["half_spreads"].append(dec.half_spread_bps)
                st["sigmas"].append(dec.sigma_tau_bps)
                # HyperCore grants in-block cancel priority ahead of taker IOCs ONLY to explicit
                # cancel (not modify). Force explicit cancel on Binance lead-lag shocks or toxic flow.
                use_explicit_cancel = bool(urgent_pull or tox_mult > 1.0)
                prev_ambig_len = len(ambiguous_by_coin[c_name])
                pl, md, cn, sk = await _sync_coin_quotes_atomic(
                    c_name,
                    dec,
                    active_orders[c_name],
                    all_mm_oids[c_name],
                    adapter,
                    mid_px=book.mid_px,
                    params=params,
                    recv_ms=recv_ms,
                    audit_log_path=audit_log_path,
                    prefer_explicit_cancel=use_explicit_cancel,
                    execute_live=execute_live,
                    ambiguous_out=ambiguous_by_coin[c_name],
                )
                for sub_new in ambiguous_by_coin[c_name][prev_ambig_len:]:
                    st["halt_events"].append(f"ambiguous_placement:{sub_new['side']}")
                st["placed"] += pl
                st["modified"] += md
                st["cancelled"] += cn
                if use_explicit_cancel and cn > 0:
                    st["urgent_cancels"] += cn
                st["skipped"] += sk
                if pl or md or cn:
                    last_action_mono[c_name] = time.monotonic()

            quote_rows.append(
                {
                    "coin": c_name,
                    "ts_ms": recv_ms,
                    "mid_px": book.mid_px,
                    "fair_px": dec.fair_px,
                    "reservation_px": dec.reservation_px,
                    "sigma_tau_bps": dec.sigma_tau_bps,
                    "lead_impulse_bps": round(lead_imp, 2),
                    "q_norm": dec.q_norm,
                    "skew_bps": dec.skew_bps,
                    "half_spread_bps": dec.half_spread_bps,
                    "bid_px": dec.bid_px,
                    "ask_px": dec.ask_px,
                    "inventory_sz": ledgers[c_name].net_sz,
                    "net_pnl_usd": ledgers[c_name].net_pnl_usd(book.mid_px),
                }
            )
            return True

        if not ws_live_init_ok:
            pass
        elif ws_messages is not None:
            for idx, msg in enumerate(ws_messages):
                keep = await _handle_message(msg, started_ms + idx * 500)
                if not keep:
                    break
        else:
            import websockets

            deadline = time.monotonic() + max(1.0, duration_sec)
            last_dms = time.monotonic()
            dms_refresh_sec = max(3.0, (dead_man_timeout_ms / 1000.0) * 0.4)
            reconnects = 0
            max_reconnects = 3
            stop_session = False

            while not stop_session and time.monotonic() < deadline:
                try:
                    async with websockets.connect(
                        ws_url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024
                    ) as ws:
                        subs: list[dict[str, Any]] = []
                        for c in coin_list:
                            subs.extend(
                                [{"type": "bbo", "coin": c}, {"type": "l2Book", "coin": c}, {"type": "trades", "coin": c}]
                            )
                        if execute_live and account_address:
                            subs.append({"type": "userFills", "user": account_address})
                            subs.append({"type": "orderUpdates", "user": account_address})
                        for sub in subs:
                            await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

                        last_ping = time.monotonic()
                        while True:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                stop_session = True
                                break
                            now_mono = time.monotonic()
                            if now_mono - last_ping >= 25.0:
                                await ws.send(json.dumps({"method": "ping"}))
                                last_ping = now_mono
                            active_dms_coins = [c for c in coin_list if active_orders[c]] or coin_list
                            books_fresh_for_dms = all(
                                (now_mono - last_valid_book_mono[c]) <= MAX_BOOK_STALENESS_FOR_DMS_SEC
                                for c in active_dms_coins
                            )
                            if (
                                enable_dead_man_switch
                                and (now_mono - last_dms) >= dms_refresh_sec
                                and books_fresh_for_dms
                            ):
                                # Extend the DMS only while every active coin has fresh valid two-sided books.
                                await _refresh_dead_man(int(time.time() * 1000))
                                last_dms = now_mono
                            try:
                                raw = await asyncio.wait_for(ws.recv(), timeout=min(1.5, remaining))
                            except asyncio.TimeoutError:
                                continue
                            recv_ms = int(time.time() * 1000)
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            if isinstance(msg, dict):
                                keep = await _handle_message(msg, recv_ms)
                                if not keep:
                                    stop_session = True
                                    break
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    reconnects += 1
                    for c in coin_list:
                        stats[c]["halt_events"].append(f"ws_reconnect:{reconnects}:{type(exc).__name__}")
                    if reconnects > max_reconnects or time.monotonic() >= deadline:
                        break
                    if execute_live and account_address:
                        oo_rc = await asyncio.to_thread(fetch_open_orders_checked, http, account_address)
                        uf_rc = await asyncio.to_thread(fetch_user_fills_checked, http, account_address)
                        if uf_rc is None and any(all_mm_oids.values()):
                            ws_fills_unsynced = True
                        elif uf_rc is not None:
                            now_rc_ms = int(time.time() * 1000)
                            if oo_rc is not None:
                                for cn in coin_list:
                                    if ambiguous_by_coin[cn]:
                                        stats[cn]["placed"] += _reconcile_ambiguous_submissions(
                                            cn,
                                            ambiguous_by_coin[cn],
                                            oo=oo_rc,
                                            uf=uf_rc,
                                            active_orders=active_orders[cn],
                                            all_mm_oids=all_mm_oids[cn],
                                            started_ms=started_ms,
                                        )
                            for fl in uf_rc:
                                _ingest_live_fill(fl, default_ts_ms=now_rc_ms, check_session_window=True)
                            ws_fills_unsynced = False
                            if oo_rc is not None:
                                live_by_coin: dict[str, set[int]] = {c: set() for c in coin_list}
                                for o in oo_rc:
                                    if isinstance(o, dict) and o.get("oid") is not None:
                                        cn = str(o.get("coin") or "")
                                        if cn in live_by_coin:
                                            live_by_coin[cn].add(int(o["oid"]))
                                for cn in coin_list:
                                    for sk in list(active_orders[cn].keys()):
                                        oid_v = active_orders[cn][sk].get("oid")
                                        if oid_v is not None and int(oid_v) not in live_by_coin[cn]:
                                            active_orders[cn].pop(sk, None)
                    backoff_s = min(0.5 * reconnects, max(0.0, deadline - time.monotonic()))
                    if backoff_s > 0:
                        await asyncio.sleep(backoff_s)

            # Final REST fill reconciliation for any fill arriving right at socket close
            if execute_live and account_address and (any(all_mm_oids.values()) or any(ambiguous_by_coin.values())):
                uf_fin = await asyncio.to_thread(fetch_user_fills_checked, http, account_address)
                oo_fin = await asyncio.to_thread(fetch_open_orders_checked, http, account_address)
                if uf_fin is None:
                    ws_fills_unsynced = True
                else:
                    now_fin_ms = int(time.time() * 1000)
                    if oo_fin is not None:
                        for cn in coin_list:
                            if ambiguous_by_coin[cn]:
                                stats[cn]["placed"] += _reconcile_ambiguous_submissions(
                                    cn,
                                    ambiguous_by_coin[cn],
                                    oo=oo_fin,
                                    uf=uf_fin,
                                    active_orders=active_orders[cn],
                                    all_mm_oids=all_mm_oids[cn],
                                    started_ms=started_ms,
                                )
                    for fl in uf_fin:
                        _ingest_live_fill(fl, default_ts_ms=now_fin_ms, check_session_window=True)
                    ws_fills_unsynced = False
    finally:
        lead_stop.set()
        if lead_task is not None:
            lead_task.cancel()
            try:
                await lead_task
            except (asyncio.CancelledError, Exception):
                pass

        # 1. Cancel all resting MM orders across all coins atomically
        cancel_all: list[dict[str, Any]] = []
        if execute_live and account_address:
            oo_final = fetch_open_orders_checked(http, account_address)
            if oo_final is not None:
                for c_name in coin_list:
                    _adopt_matching_open_orders_for_cleanup(
                        c_name, ambiguous_by_coin[c_name], oo_final, all_mm_oids[c_name]
                    )
                for o in oo_final:
                    c_name = str(o.get("coin") or "")
                    if c_name in all_mm_oids and o.get("oid") is not None and int(o["oid"]) in all_mm_oids[c_name]:
                        cancel_all.append({"coin": c_name, "oid": int(o["oid"])})
                        stats[c_name]["cleanup_cancelled"] += 1
            else:
                for c_name in coin_list:
                    for o in active_orders[c_name].values():
                        if o.get("oid") is not None:
                            cancel_all.append({"coin": c_name, "oid": int(o["oid"])})
                            stats[c_name]["cleanup_cancelled"] += 1
        else:
            for c_name in coin_list:
                for o in active_orders[c_name].values():
                    cancel_all.append({"coin": c_name, "oid": o.get("oid")})
                    stats[c_name]["cleanup_cancelled"] += 1
        if cancel_all:
            try:
                await asyncio.to_thread(adapter.cancel_all_open_orders, cancel_all)
            except Exception:
                pass

        # 1b. Verify nothing of ours is still resting before the exchange-side Dead Man's Switch is
        # disarmed; if a cancel failed (timeout / 429) or verification is impossible, keep the DMS
        # armed so Hyperliquid cancels the leftovers at its trigger time instead of never.
        mm_orders_cleared = True
        if execute_live and account_address:
            mm_orders_cleared = False
            for _attempt in range(2):
                oo_check = fetch_open_orders_checked(http, account_address)
                if oo_check is None:
                    break
                for c_name in coin_list:
                    _adopt_matching_open_orders_for_cleanup(
                        c_name, ambiguous_by_coin[c_name], oo_check, all_mm_oids[c_name]
                    )
                leftovers = [
                    {"coin": str(o.get("coin") or ""), "oid": int(o["oid"])}
                    for o in oo_check
                    if isinstance(o, dict)
                    and o.get("oid") is not None
                    and str(o.get("coin") or "") in all_mm_oids
                    and int(o["oid"]) in all_mm_oids[str(o.get("coin") or "")]
                ]
                if not leftovers:
                    mm_orders_cleared = True
                    break
                try:
                    await asyncio.to_thread(adapter.cancel_all_open_orders, leftovers)
                except Exception:
                    pass

        # 2. Optional flatten-on-exit per coin (only when initial position & fills are verified)
        if flatten_on_exit and ws_live_init_ok and not ws_fills_unsynced:
            for c_name in coin_list:
                sp = specs.get(c_name)
                mid = last_mids.get(c_name, 0.0)
                net = ledgers[c_name].net_sz
                if sp is None or mid <= 0 or abs(net) <= 1e-12:
                    continue
                unwind_sz = round_hl_size(abs(net), sp.sz_decimals)
                unwind_side = "sell" if net > 0 else "buy"
                # Returning the session delta to zero may *increase* the shared account
                # position (other engines' inventory); reduceOnly would then be rejected.
                acct_pos = ledgers[c_name].start_sz + net
                flat_reduce_only = reduce_only_is_valid(unwind_side, unwind_sz, acct_pos)
                closes_full_acct = flat_reduce_only and abs(unwind_sz - abs(acct_pos)) <= max(
                    1e-9, 0.5 * (10.0 ** -sp.sz_decimals)
                )
                min_flat_usd = 0.50 if closes_full_acct else 10.5
                if unwind_sz > 0 and unwind_sz * mid >= min_flat_usd:
                    raw_px = mid * (0.9975 if unwind_side == "sell" else 1.0025)
                    limit_px = round_hl_price(raw_px, sp.sz_decimals, side=unwind_side)
                    flat_ord = PlannedOrder(
                        coin=c_name,
                        side=unwind_side,
                        limit_px=limit_px,
                        sz=unwind_sz,
                        notional_usd=round(unwind_sz * mid, 2),
                        tif="Ioc",
                        reduce_only=flat_reduce_only,
                    )
                    try:
                        flat_resp = await asyncio.to_thread(adapter.place_ioc_order, flat_ord)
                        filled_sz, filled_px = _extract_ioc_fill(
                            flat_resp, fallback_sz=unwind_sz, fallback_px=mid, execute_live=execute_live
                        )
                        if filled_sz > 0:
                            stats[c_name]["flattened_exit_sz"] = filled_sz
                            ledgers[c_name].on_fill(
                                unwind_side, filled_px, filled_sz, fee_usd=filled_sz * filled_px * 4.5e-4
                            )
                            stats[c_name]["fills"].append(
                                {
                                    "coin": c_name,
                                    "ts_ms": int(time.time() * 1000),
                                    "oid": None,
                                    "side": unwind_side,
                                    "px": filled_px,
                                    "sz": filled_sz,
                                    "notional_usd": filled_sz * filled_px,
                                    "crossed": True,
                                }
                            )
                        if audit_log_path is not None:
                            # Log the exit IOC so attribution can tell MM flatten fills from foreign ones,
                            # while tagging dry-run simulates as mode="dry_run" so OIDs stay out of live sets.
                            is_dry_flat = (
                                (not execute_live)
                                or isinstance(adapter, DryRunAdapter)
                                or (isinstance(flat_resp, dict) and flat_resp.get("mode") == "dry_run")
                            )
                            audit_log_path.parent.mkdir(parents=True, exist_ok=True)
                            with audit_log_path.open("a", encoding="utf-8") as fh:
                                fh.write(
                                    json.dumps(
                                        {
                                            "ts_ms": int(time.time() * 1000),
                                            "mode": "dry_run" if is_dry_flat else "ws_flatten_ioc",
                                            "action": "ws_flatten_ioc",
                                            "coin": c_name,
                                            "oid": _extract_order_oid(flat_resp)[0],
                                            "status": "flatten",
                                            "filled_sz": filled_sz,
                                            "order": flat_ord.to_dict(),
                                        },
                                        default=str,
                                    )
                                    + "\n"
                                )
                    except Exception:
                        pass

        # 3. Disarm Dead Man's Switch so it never consumes a daily trigger on clean shutdown —
        # only once every MM order is confirmed gone (see 1b).
        if dead_man_armed and not mm_orders_cleared:
            for c_name in coin_list:
                stats[c_name]["dms_left_armed"] = True
        if dead_man_armed and mm_orders_cleared and hasattr(adapter, "schedule_cancel"):
            try:
                await asyncio.to_thread(adapter.schedule_cancel, None)
            except Exception:
                pass

        if owns_client:
            http.close()

    ended_ms = int(time.time() * 1000)
    summaries: dict[str, MMSessionSummary] = {}
    all_fills: list[dict[str, Any]] = []
    for c_name in coin_list:
        st = stats[c_name]
        lg = ledgers[c_name]
        sp = specs[c_name]
        mid = last_mids[c_name]
        c_fills = st["fills"]
        all_fills.extend(c_fills)
        maker_vol = sum(f["notional_usd"] for f in c_fills if not f.get("crossed", False))
        tot_vol = sum(f["notional_usd"] for f in c_fills)
        api_reqs = st["placed"] + st["modified"] + st["cancelled"] + st["cleanup_cancelled"]
        summaries[c_name] = MMSessionSummary(
            mode="testnet_ws_live_mm" if execute_live else "testnet_ws_dry_run_mm",
            coin=c_name,
            started_ms=started_ms,
            ended_ms=ended_ms,
            ticks_observed=st["ticks"],
            quotes_placed=st["placed"],
            quotes_cancelled=st["cancelled"],
            requotes_skipped_deadband=st["skipped"],
            fills_count=len(c_fills),
            buy_fills=lg.buy_fills,
            sell_fills=lg.sell_fills,
            volume_usd=round(lg.volume_usd, 2),
            maker_share_pct=round(maker_vol / tot_vol * 100.0, 1) if tot_vol > 0 else (100.0 if not c_fills else 0.0),
            start_inventory_sz=round(lg.start_sz, sp.sz_decimals),
            end_inventory_sz=round(lg.net_sz, sp.sz_decimals),
            max_abs_inventory_usd=round(lg.max_abs_inv_usd, 2),
            avg_half_spread_bps=round(float(np.mean(st["half_spreads"])), 2) if st["half_spreads"] else 0.0,
            avg_sigma_tau_bps=round(float(np.mean(st["sigmas"])), 2) if st["sigmas"] else 0.0,
            realized_spread_pnl_usd=round(lg.realized_pnl_usd, 4),
            inventory_mtm_pnl_usd=round(lg.unrealized_pnl_usd(mid), 4),
            fees_usd=round(-lg.fees_usd, 4),
            net_pnl_usd=round(lg.net_pnl_usd(mid), 4),
            vw_markout_5s_bps=_compute_vw_markout(c_fills, st["mids_series"], 5_000),
            vw_markout_30s_bps=_compute_vw_markout(c_fills, st["mids_series"], 30_000),
            halt_events=st["halt_events"][:20],
            final_cleanup_cancelled=st["cleanup_cancelled"],
            flattened_exit_sz=st["flattened_exit_sz"],
            bbo_ticks_observed=st["bbo_ticks"],
            quotes_modified=st["modified"],
            lead_pulls=st["lead_pulls"],
            dead_man_refreshes=dead_man_refreshes,
            dms_left_armed=bool(st["dms_left_armed"]),
            urgent_cancels=st["urgent_cancels"],
            api_requests_per_usd_vol=round(api_reqs / max(lg.volume_usd, 1.0), 3),
            vpin_widen_events=st["vpin_widen_events"],
            rate_limit_throttles=st["rate_limit_throttles"],
            mempool_priority_eligible=mempool_priority_ok,
        )

    return {
        "coins": coin_list,
        "summaries": summaries,
        "fills_df": pd.DataFrame(all_fills),
        "quotes_df": pd.DataFrame(quote_rows),
        "dead_man_refreshes": dead_man_refreshes,
    }


def benchmark_tick_to_quote_latency(iterations: int = 5_000) -> dict[str, float]:
    """Measure pure hot-path ``parse_bbo -> EWMA -> LeadLag -> compute_as_quotes`` latency (microseconds)."""
    spec = AssetSpec("ETH", sz_decimals=4, max_leverage=25)
    params = ASParams()
    vol = EWMAVolatilityEstimator()
    lead = LeadLagEstimator()
    latencies_ns: list[int] = []
    t0 = 1_700_000_000_000
    for i in range(max(100, iterations)):
        ts = t0 + i * 200
        bid = 2500.0 + (i % 5) * 0.1
        payload = {"coin": "ETH", "time": ts, "bbo": [{"px": str(bid), "sz": "10.0"}, {"px": str(bid + 0.2), "sz": "9.5"}]}
        lead.update_cex(ts, bid + 0.15)
        start = time.perf_counter_ns()
        bk = parse_bbo_snapshot("ETH", payload, prev_imbalance5=0.1, now_ms=ts)
        assert bk is not None
        sig = vol.update(bk.observed_ms, bk.mid_px)
        imp = lead.impulse_bps(ts, bk.mid_px)
        _ = compute_as_quotes(bk, spec, inventory_sz=0.01, sigma_tau_bps=sig, params=params, lead_impulse_bps=imp)
        latencies_ns.append(time.perf_counter_ns() - start)
    arr_us = np.array(latencies_ns, dtype=np.float64) / 1_000.0
    tot_sec = float(np.sum(latencies_ns)) / 1e9
    return {
        "iterations": float(len(latencies_ns)),
        "p50_us": round(float(np.percentile(arr_us, 50)), 2),
        "p95_us": round(float(np.percentile(arr_us, 95)), 2),
        "p99_us": round(float(np.percentile(arr_us, 99)), 2),
        "mean_us": round(float(np.mean(arr_us)), 2),
        "throughput_ticks_per_sec": round(len(latencies_ns) / max(tot_sec, 1e-9), 0),
    }


# --------------------------------------------------------------------------- Reporting


def format_mm_report_markdown(summary: MMSessionSummary, params: ASParams) -> str:
    dur_sec = max(1.0, (summary.ended_ms - summary.started_ms) / 1000.0)
    ts = datetime.fromtimestamp(summary.ended_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    mk5 = f"{summary.vw_markout_5s_bps:+.2f} б.п." if summary.vw_markout_5s_bps is not None else "—"
    mk30 = f"{summary.vw_markout_30s_bps:+.2f} б.п." if summary.vw_markout_30s_bps is not None else "—"
    lines = [
        f"# Avellaneda–Stoikov Market Making · {summary.coin} ({ts})",
        "",
        f"- Режим: `{summary.mode}` (`{TESTNET_API_URL}`) · длительность **{dur_sec:.0f}с**, тиков: **{summary.ticks_observed}** (в т.ч. быстрых `bbo`: **{summary.bbo_ticks_observed}**)",
        f"- Параметры A–S: `gamma={params.gamma}`, `kappa={params.kappa}`, `tau={params.horizon_sec:.0f}s`, `base_order=${params.base_order_usd:.0f}`, `max_inv=${params.max_inventory_usd:.0f}`",
        f"- **Котировки (ALO):** выставлено **{summary.quotes_placed}**, модифицировано атомарно (`batchModify`): **{summary.quotes_modified}**, отменено {summary.quotes_cancelled} (в т.ч. приоритетных `cancel`: **{summary.urgent_cancels}**), deadband-save: **{summary.requotes_skipped_deadband}**, lead-lag pull: **{summary.lead_pulls}**, VPIN widen: **{summary.vpin_widen_events}**, API throttles: **{summary.rate_limit_throttles}** (`{summary.api_requests_per_usd_vol:.2f} req/$vol`), DMS: **{summary.dead_man_refreshes}**, очистка: {summary.final_cleanup_cancelled}, flatten: `{summary.flattened_exit_sz:g} {summary.coin}`",
        f"- **Спред и волатильность:** ср. полуспред **{summary.avg_half_spread_bps:.2f} б.п.**, ср. EWMA σ_τ **{summary.avg_sigma_tau_bps:.2f} б.п.**",
        f"- **Сделки и инвентарь:** {summary.fills_count} ({summary.buy_fills}B / {summary.sell_fills}S), оборот **${summary.volume_usd:,.2f}**, Maker доля **{summary.maker_share_pct:.1f}%**, дельта-инвентарь `{summary.end_inventory_sz:g} {summary.coin}` (пик `${summary.max_abs_inventory_usd:,.1f}`)",
        f"- **PnL и Markout:** итог **{summary.net_pnl_usd:+,.4f}$** (спред {summary.realized_spread_pnl_usd:+,.4f}$ + MTM {summary.inventory_mtm_pnl_usd:+,.4f}$ + комиссия {summary.fees_usd:+,.4f}$) · Markout +5s **{mk5}** / +30s **{mk30}**",
    ]
    if summary.halt_events:
        lines.append(f"- **Защитные паузы:** {', '.join(summary.halt_events[:5])}")
    if summary.dms_left_armed:
        lines.append("- ⚠️ **Внимание (DMS):** не все ордера подтверждены отменёнными — Dead Man's Switch (`scheduleCancel`) оставлен активным!")
    return "\n".join(lines) + "\n"


def format_mm_telegram_rich_html(summary: MMSessionSummary, params: ASParams) -> str:
    """Plain-Russian Telegram Rich HTML table (``sendRichMessage``) for MM session summary."""
    dt = datetime.fromtimestamp(summary.ended_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    dur_sec = max(1.0, (summary.ended_ms - summary.started_ms) / 1000.0)
    dot = "🟢" if summary.net_pnl_usd >= 0 else "🔴"
    mk5 = f"{summary.vw_markout_5s_bps:+.1f} б.п." if summary.vw_markout_5s_bps is not None else "—"
    rows = [
        '<tr><th align="left">Показатель</th><th align="right">Значение</th></tr>',
        f'<tr><td align="left">Монета / режим</td><td align="right"><b>{html.escape(summary.coin)}</b> ({html.escape(summary.mode)})</td></tr>',
        f'<tr><td align="left">Длительность / тиков L2</td><td align="right">{dur_sec:.0f}с · {summary.ticks_observed} тиков</td></tr>',
        f'<tr><td align="left">Ср. полуспред / σ_τ</td><td align="right">{summary.avg_half_spread_bps:.2f} б.п. / {summary.avg_sigma_tau_bps:.1f} б.п.</td></tr>',
        f'<tr><td align="left">Ордера ALO (выст. / отм. / deadband)</td><td align="right">{summary.quotes_placed} / {summary.quotes_cancelled} / {summary.requotes_skipped_deadband}</td></tr>',
        f'<tr><td align="left">Сделки / оборот / Maker</td><td align="right">{summary.fills_count} ({summary.buy_fills}B/{summary.sell_fills}S) · ${summary.volume_usd:,.1f} · {summary.maker_share_pct:.0f}%</td></tr>',
        f'<tr><td align="left">Инвентарь (конец / пик)</td><td align="right">{summary.end_inventory_sz:g} {html.escape(summary.coin)} / ${summary.max_abs_inventory_usd:,.1f}</td></tr>',
        f'<tr><td align="left">Итог PnL / Markout +5s</td><td align="right">{dot} {summary.net_pnl_usd:+,.4f}$ · {html.escape(mk5)}</td></tr>',
    ]
    dms_warn = (
        "<p>⚠️ <b>Внимание:</b> не все ордера подтверждены отменёнными — Dead Man's Switch "
        "(<code>scheduleCancel</code>) оставлен активным для авто-снятия!</p>"
        if summary.dms_left_armed
        else ""
    )
    footer_note = (
        "DMS оставлен активным для авто-очистки"
        if summary.dms_left_armed
        else "финальная очистка ордеров выполнена"
    )
    return (
        f"<h1>📐 Маркет-мейкер A–S · {html.escape(summary.coin)} · {html.escape(date_str)}</h1>"
        f"<p><b>Параметры ({html.escape(time_str)}):</b> γ={params.gamma}, κ={params.kappa}, "
        f"ордер ${params.base_order_usd:.0f}, лимит инвентаря ${params.max_inventory_usd:.0f}</p>"
        f"{dms_warn}"
        f'<table bordered striped>{"".join(rows)}</table>'
        f"<footer>Hyperliquid Testnet · Avellaneda–Stoikov ALO ({footer_note})</footer>"
    )
