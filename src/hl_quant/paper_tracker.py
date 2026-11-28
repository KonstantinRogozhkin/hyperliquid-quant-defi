"""Read-only live Paper / Shadow Tracker for the Hyperliquid 6-family metamodel.

Zero orders placed. On every tick:
* Evaluates frozen Dev-trained metamodel sleeve weights on trailing 1h bars & funding.
* Prices turnover against live Hyperliquid L2 orderbooks (real top-of-book bid/ask
  spread and top-5 depth) instead of assuming frictionless 1h closes.
* Accrues mark-to-market price/basis + settled funding PnL since the previous tick
  and persists an append-only ledger (``state.json``, ``ticks.parquet``, ``trades.parquet``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from hl_quant import alpha_families as af
from hl_quant import hyperliquid as hl


@dataclass(frozen=True, slots=True)
class L2Quote:
    coin: str
    best_bid: float
    best_ask: float
    mid_px: float
    spread_bps: float
    top5_bid_usd: float
    top5_ask_usd: float


@dataclass(frozen=True, slots=True)
class MetamodelSleeve:
    spec: af.HypothesisSpec
    weight: float


def default_frozen_metamodel() -> list[MetamodelSleeve]:
    """Frozen Dev-only ``shrinkage_sharpe`` metamodel after the ``best_cex`` venue-switch cost fix.

    Reproduce: ``run_hl_colab_shard.py`` over the frozen 2026-11-25 bundle (70 coins, 180 days,
    48 hypotheses) then ``build_hl_metamodel.py`` with the default 80% Dev-fold gate. With venue
    switches priced, ``carry_best_cex_L72_in10`` fails the Dev Sharpe gate (0.10 < 0.5) and drops
    out. See ``docs/reports/2026-11-25/hl_colab_multifamily_metamodel.md``.
    """
    reg = {s.name: s for s in af.default_hypothesis_registry()}
    weights = {
        "carry_best_cex_L168_in5": 0.400,
        "xs_rev_L24_r12": 0.379,
        "xs_mom_L72_r24": 0.221,
    }
    return [MetamodelSleeve(spec=reg[name], weight=w) for name, w in weights.items()]


def apply_alo_fill_rate_throttle(
    sleeves: list[MetamodelSleeve],
    observed_alo_fill_rate_pct: float | None,
    *,
    target_fill_pct: float = 85.0,
    halt_fill_pct: float = 70.0,
) -> list[MetamodelSleeve]:
    """Scale down high-turnover ``hl_xs_price_reversal_mom`` sleeves when live ALO fill rate degrades.

    Operationalizes the Monte Carlo stress finding: ``xs_rev`` requires >=85% passive ALO fill rate
    to stay profitable Ex-INIT, and breaks below ~70%. Between ``target_fill_pct`` and ``halt_fill_pct``,
    turnover-sensitive sleeves scale linearly toward zero and remaining weights re-normalize.
    """
    if observed_alo_fill_rate_pct is None or not math.isfinite(observed_alo_fill_rate_pct):
        return sleeves
    if observed_alo_fill_rate_pct >= target_fill_pct:
        return sleeves
    span = max(1.0, target_fill_pct - halt_fill_pct)
    rev_scale = max(0.0, min(1.0, (observed_alo_fill_rate_pct - halt_fill_pct) / span))
    adjusted: list[MetamodelSleeve] = []
    for sl in sleeves:
        w = sl.weight * rev_scale if sl.spec.family == "hl_xs_price_reversal_mom" else sl.weight
        if w > 1e-6:
            adjusted.append(MetamodelSleeve(spec=sl.spec, weight=w))
    tot = sum(s.weight for s in adjusted)
    if tot <= 1e-9:
        return sleeves
    return [MetamodelSleeve(spec=s.spec, weight=round(s.weight / tot, 6)) for s in adjusted]


def fetch_l2_quotes(client: hl.HyperliquidInfoClient, coins: list[str]) -> dict[str, L2Quote]:
    """Fetch live Hyperliquid L2 top-5 quotes for each coin (read-only)."""
    quotes: dict[str, L2Quote] = {}
    for coin in sorted(set(coins)):
        try:
            book = client.info({"type": "l2Book", "coin": coin})
        except Exception:  # noqa: BLE001 - one bad book must not abort the whole tick
            continue
        levels = book.get("levels") if isinstance(book, dict) else None
        if not (isinstance(levels, list) and len(levels) == 2 and levels[0] and levels[1]):
            continue
        bids, asks = levels[0][:5], levels[1][:5]
        best_bid = float(bids[0]["px"])
        best_ask = float(asks[0]["px"])
        mid = 0.5 * (best_bid + best_ask)
        if mid <= 0:
            continue
        spread_bps = (best_ask - best_bid) / mid * 1e4
        bid_usd = sum(float(lv["px"]) * float(lv["sz"]) for lv in bids)
        ask_usd = sum(float(lv["px"]) * float(lv["sz"]) for lv in asks)
        quotes[coin] = L2Quote(
            coin=coin,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_px=mid,
            spread_bps=spread_bps,
            top5_bid_usd=bid_usd,
            top5_ask_usd=ask_usd,
        )
    return quotes


def _extend_market_for_live_decision(market: af.WideMarket) -> af.WideMarket:
    """Append the post-bar decision timestamp ``last_hour + 1h`` for causal ``.shift(1)`` builders.

    ``market.hours[-1]`` is the start of the most recent *completed* 1h bar ``[h_{T-1}, h_T)``
    (e.g. ``23:00 UTC`` when evaluated at ``00:05 UTC``). Because every weight builder in
    ``alpha_families`` shifts inputs by ``.shift(1)`` so ``w_df.loc[h_t]`` uses bars closed up
    through ``h_{t-1}``, evaluating at ``h_T = market.hours[-1] + HOUR_MS`` (``00:00 UTC``)
    incorporates the just-closed bar with zero look-ahead and hits canonical UTC boundaries
    (``00:00`` / ``12:00 UTC``) right on schedule.
    """
    if len(market.hours) == 0:
        return market
    eval_h = int(market.hours[-1]) + 3_600_000
    ext_hours = np.append(market.hours, np.int64(eval_h))

    def _append_row(df: pd.DataFrame, fill_val: float) -> pd.DataFrame:
        extra = pd.DataFrame(fill_val, index=[eval_h], columns=df.columns, dtype=float)
        return pd.concat([df, extra], axis=0)

    return af.WideMarket(
        hours=ext_hours,
        coins=list(market.coins),
        hl_close=_append_row(market.hl_close, np.nan),
        hl_ret_next=_append_row(market.hl_ret_next, 0.0),
        hl_fund_next=_append_row(market.hl_fund_next, 0.0),
        hl_rate_h=_append_row(market.hl_rate_h, np.nan),
        hl_notional=_append_row(market.hl_notional, np.nan),
        bybit_close=_append_row(market.bybit_close, np.nan),
        bybit_ret_next=_append_row(market.bybit_ret_next, 0.0),
        bybit_fund_next=_append_row(market.bybit_fund_next, 0.0),
        bybit_rate_h=_append_row(market.bybit_rate_h, np.nan),
        bybit_notional=_append_row(market.bybit_notional, np.nan),
        binance_close=_append_row(market.binance_close, np.nan),
        binance_ret_next=_append_row(market.binance_ret_next, 0.0),
        binance_fund_next=_append_row(market.binance_fund_next, 0.0),
        binance_rate_h=_append_row(market.binance_rate_h, np.nan),
        binance_notional=_append_row(market.binance_notional, np.nan),
        okx_close=_append_row(market.okx_close, np.nan),
        okx_ret_next=_append_row(market.okx_ret_next, 0.0),
        okx_fund_next=_append_row(market.okx_fund_next, 0.0),
        okx_rate_h=_append_row(market.okx_rate_h, np.nan),
        okx_notional=_append_row(market.okx_notional, np.nan),
    )


def compute_latest_target_positions(
    market: af.WideMarket,
    sleeves: list[MetamodelSleeve],
) -> dict[str, dict[str, Any]]:
    """Compute net target HL and CEX hedge weights after the latest closed bar ``market.hours[-1]``.

    Sign convention in output:
    * ``hl_weight``: signed HL perp notional weight (+long HL, -short HL) as fraction of equity.
    * ``cex_legs``: ``{venue: signed CEX perp notional weight}`` (+long CEX, -short CEX). Sleeves
      may hedge the same coin on different venues, so each venue is a separate leg.
    * ``cex_weight``: sum of ``cex_legs``; ``cex_venue``: the venue with the largest leg (display only).
    """
    eval_market = _extend_market_for_live_decision(market)
    net_hl: dict[str, float] = {c: 0.0 for c in market.coins}
    net_cex: dict[str, dict[str, float]] = {c: {} for c in market.coins}

    def add_cex(coin: str, venue: str, w: float) -> None:
        net_cex[coin][venue] = net_cex[coin].get(venue, 0.0) + w

    for sl in sleeves:
        spec = sl.spec
        if spec.family == "funding_carry_arb":
            w_df, vc_df = af.build_funding_carry_weights(eval_market, spec.params)
            last_w = w_df.iloc[-1]
            last_vc = vc_df.iloc[-1] if vc_df is not None else None
            for coin, raw_w in last_w.items():
                if not math.isfinite(raw_w) or abs(raw_w) < 1e-9:
                    continue
                # In funding_carry_arb, raw_w > 0 means short HL / long CEX, capital = 2x notional -> scale 0.5
                leg_w = float(raw_w) * sl.weight * 0.5
                net_hl[coin] -= leg_w
                venue = "bybit"
                if last_vc is not None:
                    v_val = last_vc.get(coin, "bybit")
                    if isinstance(v_val, str) and v_val in ("bybit", "binance", "okx"):
                        venue = v_val
                    elif not isinstance(v_val, str) and bool(v_val):
                        venue = "binance"
                add_cex(coin, venue, leg_w)
        elif spec.family == "basis_mean_reversion":
            w_df = af.build_basis_mr_weights(eval_market, spec.params)
            last_w = w_df.iloc[-1]
            for coin, raw_w in last_w.items():
                if not math.isfinite(raw_w) or abs(raw_w) < 1e-9:
                    continue
                leg_w = float(raw_w) * sl.weight * 0.5
                net_hl[coin] -= leg_w
                add_cex(coin, "bybit", leg_w)
        elif spec.family == "hl_xs_funding_contrarian":
            w_df = af.build_xs_funding_contrarian_weights(eval_market, spec.params)
            for coin, raw_w in w_df.iloc[-1].items():
                if math.isfinite(raw_w) and abs(raw_w) >= 1e-9:
                    net_hl[coin] += float(raw_w) * sl.weight
        elif spec.family == "hl_xs_price_reversal_mom":
            w_df = af.build_xs_price_weights(eval_market, spec.params)
            for coin, raw_w in w_df.iloc[-1].items():
                if math.isfinite(raw_w) and abs(raw_w) >= 1e-9:
                    net_hl[coin] += float(raw_w) * sl.weight
        elif spec.family == "hl_xs_velocity_div":
            w_df = af.build_xs_velocity_divergence_weights(eval_market, spec.params)
            for coin, raw_w in w_df.iloc[-1].items():
                if math.isfinite(raw_w) and abs(raw_w) >= 1e-9:
                    net_hl[coin] += float(raw_w) * sl.weight
        else:
            raise ValueError(f"unsupported sleeve family for paper tracker: {spec.family!r}")

    targets: dict[str, dict[str, Any]] = {}
    for coin in market.coins:
        hw = net_hl[coin]
        legs = {v: round(w, 6) for v, w in sorted(net_cex[coin].items()) if abs(w) >= 1e-5}
        if abs(hw) >= 1e-5 or legs:
            targets[coin] = {
                "hl_weight": round(hw, 6),
                "cex_legs": legs,
                "cex_weight": round(sum(legs.values()), 6),
                "cex_venue": max(legs, key=lambda v: abs(legs[v])) if legs else "",
            }
    return targets


def _cex_legs(pos: dict[str, Any]) -> dict[str, float]:
    """CEX legs of a stored position; states written before per-venue legs keep one leg."""
    legs = pos.get("cex_legs")
    if isinstance(legs, dict):
        return {str(v): float(w) for v, w in legs.items()}
    cw = float(pos.get("cex_weight", 0.0))
    return {str(pos.get("cex_venue") or "bybit"): cw} if abs(cw) > 1e-9 else {}


def _last_cex_prices(pos: dict[str, Any]) -> dict[str, float]:
    prices = pos.get("last_cex_px_by_venue")
    if isinstance(prices, dict):
        return {str(v): float(px) for v, px in prices.items()}
    px = float(pos.get("last_cex_px", 0.0) or 0.0)
    return {str(pos.get("cex_venue") or "bybit"): px} if px > 0 else {}


def _last_known(df: pd.DataFrame, coin: str, tick_hour_ms: int) -> float:
    """Latest finite value of ``df[coin]`` at or before ``tick_hour_ms`` (NaN when none)."""
    if coin not in df.columns:
        return float("nan")
    series = df[coin].loc[:tick_hour_ms].dropna()
    return float(series.iloc[-1]) if not series.empty else float("nan")


def _append_parquet(path: Path, new_df: pd.DataFrame, *, dedup_subset: list[str] | None = None) -> None:
    if new_df.empty:
        return
    if path.exists():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    if dedup_subset and all(col in combined.columns for col in dedup_subset):
        combined = combined.drop_duplicates(subset=dedup_subset, keep="last").reset_index(drop=True)
    tmp = path.with_suffix(".tmp.parquet")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)


def _cex_frames(market: af.WideMarket, venue: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    if venue == "binance":
        return market.binance_close, market.binance_fund_next
    if venue == "okx":
        return market.okx_close, market.okx_fund_next
    return market.bybit_close, market.bybit_fund_next


def funding_interval_hours(hours: np.ndarray, prev_hour_ms: int, tick_hour_ms: int) -> np.ndarray:
    """Grid hours whose ``*_fund_next`` settlements were earned by positions held since the last tick.

    Both arguments are the OPEN of the last closed hourly bar at each tick (a 20:05 run has
    ``tick_hour_ms`` = 19:00), and ``*_fund_next[h]`` is the settlement paid at ``h + 1h``. Positions
    held from the previous tick to now therefore earned indices ``(prev_hour_ms, tick_hour_ms]``.
    """
    return hours[(hours > int(prev_hour_ms)) & (hours <= int(tick_hour_ms))]


def run_paper_tick(
    market: af.WideMarket,
    quotes: dict[str, L2Quote],
    *,
    state_dir: Path,
    sleeves: list[MetamodelSleeve] | None = None,
    starting_capital_usd: float = 100_000.0,
    hl_fee_bps: float = 1.5,
    cex_fee_bps: float = 2.0,
    l2_spread_fraction: float = 0.5,
    hl_spread_fraction: float | None = None,
    cex_mid_prices: dict[str, dict[str, float]] | None = None,
    rebalance_deadband_weight: float = 0.0,
    max_tick_nav_jump_pct: float = 8.0,
    max_held_missing_ticks: int = 3,
) -> dict[str, Any]:
    """Execute one idempotent paper-trading tick and persist updated ledger."""
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    ticks_path = state_dir / "ticks.parquet"
    trades_path = state_dir / "trades.parquet"

    active_sleeves = sleeves if sleeves is not None else default_frozen_metamodel()
    tick_hour_ms = int(market.hours[-1])

    if state_path.exists():
        state = json.loads(state_path.read_text())
    else:
        state = {
            "starting_capital_usd": starting_capital_usd,
            "equity_usd": starting_capital_usd,
            "last_tick_hour_ms": None,
            "positions": {},
            "tick_count": 0,
            "nav_guard_triggers": 0,
            "nav_guard_triggered": False,
            "cum_funding_usd": 0.0,
            "cum_price_pnl_usd": 0.0,
            "cum_costs_usd": 0.0,
        }

    prev_hour_ms = state.get("last_tick_hour_ms")
    if prev_hour_ms is not None and int(prev_hour_ms) >= tick_hour_ms:
        return {"status": "duplicate_hour_skipped", "tick_hour_ms": tick_hour_ms, "equity_usd": state["equity_usd"]}

    equity = float(state["equity_usd"])
    prev_pos: dict[str, dict[str, Any]] = state.get("positions", {})
    nav_guard_triggers = int(state.get("nav_guard_triggers", 0) or 0)

    def _cur_cex_px(venue: str, coin_name: str) -> float:
        if cex_mid_prices:
            v_map = cex_mid_prices.get(venue.lower()) or cex_mid_prices.get(venue) or {}
            live_px = v_map.get(coin_name)
            if live_px is not None and math.isfinite(float(live_px)) and float(live_px) > 0:
                return float(live_px)
        return _last_known(_cex_frames(market, venue)[0], coin_name, tick_hour_ms)

    # A held coin or held CEX leg missing a valid synchronous price/funding feed cannot be marked
    # to market safely; skip the tick without mutating state.json rather than mixing asynchronous
    # prices or dropping a leg as NaN.
    missing_held_set: set[str] = {c for c in prev_pos if c not in market.coins}
    if prev_hour_ms is not None and prev_pos:
        for coin, p in prev_pos.items():
            if coin in missing_held_set:
                continue
            cur_hl_px = quotes[coin].mid_px if coin in quotes else _last_known(market.hl_close, coin, tick_hour_ms)
            if not (math.isfinite(cur_hl_px) and cur_hl_px > 0):
                missing_held_set.add(coin)
                continue
            active_cex_legs = {v: cw for v, cw in _cex_legs(p).items() if abs(cw) > 1e-9}
            if active_cex_legs and cex_mid_prices is not None and coin not in quotes:
                missing_held_set.add(coin)
                continue
            for venue in active_cex_legs:
                cex_close_df, cex_fund_df = _cex_frames(market, venue)
                if cex_mid_prices is not None:
                    v_map = cex_mid_prices.get(venue.lower()) or cex_mid_prices.get(venue) or {}
                    live_c_px = v_map.get(coin)
                    if live_c_px is None or not (math.isfinite(float(live_c_px)) and float(live_c_px) > 0):
                        missing_held_set.add(coin)
                        break
                cur_cex_px = _cur_cex_px(venue, coin)
                if not (math.isfinite(cur_cex_px) and cur_cex_px > 0):
                    missing_held_set.add(coin)
                    break
                if (
                    coin not in cex_fund_df.columns
                    or cex_fund_df[coin].isna().all()
                    or coin not in cex_close_df.columns
                    or cex_close_df[coin].loc[:tick_hour_ms].dropna().empty
                ):
                    missing_held_set.add(coin)
                    break

    forced_stale_exits: list[str] = []
    if missing_held_set:
        # Count distinct tick hours the ledger has been blocked. A dead feed for one held coin (e.g.
        # Bybit delisting) must not freeze the whole metamodel forever: after
        # ``max_held_missing_ticks`` blocked hours the stale coins are written off at their last
        # mark (no MTM for the unobservable interval) and the tick proceeds for everything else.
        # The counter lives in a sidecar file so a blocked tick never mutates state.json.
        blocked_path = state_dir / "held_missing.json"
        try:
            blocked = json.loads(blocked_path.read_text()) if blocked_path.is_file() else {}
        except (OSError, ValueError):
            blocked = {}
        # Per-coin blocked tick hours: a coin that recovered drops out, and a coin that just lost its
        # feed starts from 1 — another coin's earlier outage never shortens its grace period.
        prior = blocked.get("coins") if isinstance(blocked.get("coins"), dict) else {}
        per_coin: dict[str, list[int]] = {}
        for c in sorted(missing_held_set):
            hrs = [int(h) for h in (prior.get(c) or []) if isinstance(h, (int, float))]
            if tick_hour_ms not in hrs:
                hrs.append(int(tick_hour_ms))
            per_coin[c] = hrs[-50:]
        still_waiting = [c for c, hrs in per_coin.items() if len(hrs) < max_held_missing_ticks]
        if max_held_missing_ticks <= 0 or still_waiting:
            tmp_blocked = blocked_path.with_suffix(".tmp.json")
            tmp_blocked.write_text(json.dumps({"coins": per_coin}))
            tmp_blocked.replace(blocked_path)
            return {
                "status": "held_coins_missing",
                "tick_hour_ms": tick_hour_ms,
                "equity_usd": state["equity_usd"],
                "missing_coins": sorted(missing_held_set),
                "blocked_ticks": {c: len(h) for c, h in per_coin.items()},
            }
        forced_stale_exits = sorted(missing_held_set)
        prev_pos = {c: p for c, p in prev_pos.items() if c not in missing_held_set}
    (state_dir / "held_missing.json").unlink(missing_ok=True)

    all_raw_targets = compute_latest_target_positions(market, active_sleeves)

    def _live_feed_ok(coin_name: str) -> bool:
        if (
            coin_name not in market.coins
            or coin_name not in quotes
            or coin_name not in market.hl_close.columns
            or market.hl_close[coin_name].loc[:tick_hour_ms].dropna().empty
        ):
            return False
        cand = all_raw_targets.get(coin_name)
        req_legs = _cex_legs(cand) if isinstance(cand, dict) else {}
        if req_legs:
            for venue in req_legs:
                cex_close_df, cex_fund_df = _cex_frames(market, venue)
                if cex_mid_prices is not None:
                    v_map = cex_mid_prices.get(venue.lower()) or cex_mid_prices.get(venue) or {}
                    px = v_map.get(coin_name)
                    if px is None or not (math.isfinite(float(px)) and float(px) > 0):
                        return False
                if (
                    coin_name not in cex_fund_df.columns
                    or cex_fund_df[coin_name].isna().all()
                    or coin_name not in cex_close_df.columns
                    or cex_close_df[coin_name].loc[:tick_hour_ms].dropna().empty
                ):
                    return False
            return True
        if cand is not None or cex_mid_prices is None:
            return True
        for v_map in cex_mid_prices.values():
            px = (v_map or {}).get(coin_name)
            if px is not None and math.isfinite(float(px)) and float(px) > 0:
                return True
        return False

    # Written-off coins stay out of the target book until their live feeds recover, otherwise the
    # rebalance would reopen them from historical-close fallbacks right away (new entry cost, reset
    # PnL) and the ledger would block on them again.
    stale_excluded = {c for c in (state.get("stale_excluded_coins") or []) if not _live_feed_ok(str(c))}
    stale_excluded |= set(forced_stale_exits)

    # 1. Mark-to-market PnL and funding accrual over (prev_hour_ms, tick_hour_ms]
    price_pnl_usd = 0.0
    fund_pnl_usd = 0.0
    coin_tick_pnl: dict[str, float] = {}
    if prev_hour_ms is not None and prev_pos:
        # ``tick_hour_ms`` is the OPEN of the last closed hourly bar: a tick running at 20:05 has
        # tick_hour_ms = 19:00, and the previous one (16:05) had 15:00. Positions set at 16:05 were
        # held through the settlements at 17:00, 18:00, 19:00 and 20:00 (the wrapper fetches funding
        # up to the current ``:00``). The settlement at h + 1h is stored in *_fund_next at index h,
        # so the held settlements live at indices (prev_hour_ms, tick_hour_ms]. Using
        # [prev, tick) instead credited the 16:00 payment (before entry) and dropped 20:00 — for
        # 8-hourly CEX funding that moved a whole settlement to the wrong portfolio.
        interval_hours = funding_interval_hours(market.hours, int(prev_hour_ms), tick_hour_ms)
        for coin, p in prev_pos.items():
            hw = float(p.get("hl_weight", 0.0))
            prev_hl_px = float(p.get("last_hl_px", 0.0))
            prev_cex_px = _last_cex_prices(p)
            c_px_pnl = 0.0
            c_fn_pnl = 0.0

            cur_hl_px = quotes[coin].mid_px if coin in quotes else _last_known(market.hl_close, coin, tick_hour_ms)
            if prev_hl_px > 0 and math.isfinite(cur_hl_px):
                c_px_pnl += equity * hw * (cur_hl_px / prev_hl_px - 1.0)
            if len(interval_hours) > 0:
                hl_f_sum = float(market.hl_fund_next.loc[interval_hours, coin].sum())
                c_fn_pnl += equity * (-hw) * hl_f_sum

            for venue, cw in _cex_legs(p).items():
                _, cex_fund_df = _cex_frames(market, venue)
                cur_cex_px = _cur_cex_px(venue, coin)
                prev_px = prev_cex_px.get(venue, 0.0)
                if prev_px > 0 and math.isfinite(cur_cex_px):
                    c_px_pnl += equity * cw * (cur_cex_px / prev_px - 1.0)
                if len(interval_hours) > 0 and coin in cex_fund_df.columns:
                    cex_f_sum = float(cex_fund_df.loc[interval_hours, coin].sum())
                    c_fn_pnl += equity * (-cw) * cex_f_sum

            price_pnl_usd += c_px_pnl
            fund_pnl_usd += c_fn_pnl
            coin_tick_pnl[coin] = c_px_pnl + c_fn_pnl

    # 1b. Bounded NAV Sanity Guard: clamp an anomalous single-tick MTM jump and freeze positions,
    # but only until the next tick hour confirms it. A clamped tick does not advance
    # ``last_tick_hour_ms``, so the confirming tick re-measures price AND funding over the whole
    # interval — nothing is dropped, and a genuine move cannot freeze the ledger forever.
    nav_guard_triggered = False
    nav_guard_reason = ""
    pending_raw = state.get("nav_guard_pending_tick_hour_ms")
    nav_guard_pending: int | None = int(pending_raw) if pending_raw is not None else None
    raw_mtm_pnl = price_pnl_usd + fund_pnl_usd
    jump_exceeded = (
        prev_hour_ms is not None
        and equity > 0
        and max_tick_nav_jump_pct > 0.0
        and abs(raw_mtm_pnl) / equity * 100.0 > max_tick_nav_jump_pct
    )
    if jump_exceeded and nav_guard_pending is not None and tick_hour_ms > nav_guard_pending:
        nav_guard_reason = (
            f"Tick MTM jump {raw_mtm_pnl / equity * 100.0:+.2f}% persisted since the clamped tick; "
            "accepted as genuine"
        )
        nav_guard_pending = None
    elif not jump_exceeded:
        nav_guard_pending = None
    if jump_exceeded and nav_guard_reason == "":
        nav_guard_pending = nav_guard_pending if nav_guard_pending is not None else tick_hour_ms
        nav_guard_triggered = True
        nav_guard_triggers += 1
        nav_guard_reason = (
            f"Tick MTM jump {raw_mtm_pnl / equity * 100.0:+.2f}% exceeded "
            f"+/-{max_tick_nav_jump_pct:.1f}% guard; clamping MTM & freezing rebalance"
        )
        price_pnl_usd = 0.0
        fund_pnl_usd = 0.0
        cost_pnl_usd = 0.0
        total_turnover = 0.0
        trade_rows: list[dict[str, Any]] = []
        new_positions: dict[str, dict[str, Any]] = {c: dict(p) for c, p in prev_pos.items()}
        new_equity = equity
    else:
        # 2. Compute new target weights (with optional micro-turnover deadband) and L2-priced costs
        raw_targets = {c: t for c, t in all_raw_targets.items() if c not in stale_excluded}
        targets: dict[str, dict[str, Any]] = {}
        for coin, t in raw_targets.items():
            if rebalance_deadband_weight > 0.0 and coin in prev_pos:
                old_p = prev_pos[coin]
                old_hw = float(old_p.get("hl_weight", 0.0))
                old_legs = _cex_legs(old_p)
                cand_hw = float(t.get("hl_weight", 0.0))
                cand_legs = dict(t.get("cex_legs", {}))
                same_hl_dir = old_hw != 0.0 and cand_hw != 0.0 and (old_hw * cand_hw > 0.0)
                same_venues = (not old_legs and not cand_legs) or (
                    bool(old_legs)
                    and set(old_legs) == set(cand_legs)
                    and all(old_legs[v] * cand_legs[v] > 0.0 for v in old_legs)
                )
                if same_hl_dir and same_venues:
                    dhw_cand = abs(cand_hw - old_hw)
                    dcw_cand = sum(abs(cand_legs[v] - old_legs[v]) for v in old_legs)
                    if dhw_cand < rebalance_deadband_weight and dcw_cand < rebalance_deadband_weight:
                        targets[coin] = {
                            "hl_weight": old_hw,
                            "cex_legs": dict(old_legs),
                            "cex_weight": round(sum(old_legs.values()), 6),
                            "cex_venue": old_p.get("cex_venue") or t.get("cex_venue") or "",
                        }
                        continue
            targets[coin] = t

        all_coins = sorted(set(prev_pos.keys()) | set(targets.keys()))
        cost_pnl_usd = 0.0
        total_turnover = 0.0
        coin_trade_cost: dict[str, float] = {}
        trade_rows = []

        for coin in all_coins:
            old_hw = float(prev_pos.get(coin, {}).get("hl_weight", 0.0))
            old_legs = _cex_legs(prev_pos.get(coin, {}))
            new_hw = float(targets.get(coin, {}).get("hl_weight", 0.0))
            new_legs = dict(targets.get(coin, {}).get("cex_legs", {}))
            old_cw = sum(old_legs.values())
            new_cw = sum(new_legs.values())
            dhw = abs(new_hw - old_hw)
            # Turnover per venue: moving a hedge from one CEX to another closes one leg and opens another.
            dcw = sum(abs(new_legs.get(v, 0.0) - old_legs.get(v, 0.0)) for v in set(old_legs) | set(new_legs))
            if dhw < 1e-6 and dcw < 1e-6:
                continue

            q = quotes.get(coin)
            l2_sp_bps = q.spread_bps if q is not None else 2.0
            # hl_spread_fraction=0 when hl_fee_bps is already an all-in measured execution cost.
            hl_frac = l2_spread_fraction if hl_spread_fraction is None else hl_spread_fraction
            hl_eff_bps = hl_fee_bps + hl_frac * l2_sp_bps
            cex_eff_bps = cex_fee_bps + l2_spread_fraction * l2_sp_bps
            trade_cost_usd = -equity * (dhw * hl_eff_bps + dcw * cex_eff_bps) / 1e4
            cost_pnl_usd += trade_cost_usd
            coin_trade_cost[coin] = trade_cost_usd
            total_turnover += dhw + dcw
            trade_rows.append(
                {
                    "tick_hour_ms": tick_hour_ms,
                    "coin": coin,
                    "old_hl_w": old_hw,
                    "new_hl_w": new_hw,
                    "old_cex_w": old_cw,
                    "new_cex_w": new_cw,
                    "l2_spread_bps": round(l2_sp_bps, 3),
                    "l2_top5_bid_usd": round(q.top5_bid_usd, 1) if q is not None else np.nan,
                    "l2_top5_ask_usd": round(q.top5_ask_usd, 1) if q is not None else np.nan,
                    "trade_cost_usd": round(trade_cost_usd, 4),
                }
            )

        net_pnl_usd = price_pnl_usd + fund_pnl_usd + cost_pnl_usd
        new_equity = equity + net_pnl_usd

        # Snapshot current prices & cumulative open-position PnL into new positions state
        new_positions = {}
        for coin, t in targets.items():
            cur_hl_px = quotes[coin].mid_px if coin in quotes else _last_known(market.hl_close, coin, tick_hour_ms)
            # Each CEX leg is marked with its own venue's price; never substitute the HL price,
            # which differs by basis (and by contract scale for 1000x-style symbols).
            cex_px_by_venue: dict[str, float] = {}
            for venue in t["cex_legs"]:
                px = _cur_cex_px(venue, coin)
                if math.isfinite(px) and px > 0:
                    cex_px_by_venue[venue] = px
            prev_cum = float(prev_pos.get(coin, {}).get("cum_pnl_usd", 0.0)) if coin in prev_pos else 0.0
            cum_pnl = prev_cum + coin_tick_pnl.get(coin, 0.0) + coin_trade_cost.get(coin, 0.0)
            new_positions[coin] = {
                **t,
                "last_hl_px": cur_hl_px,
                "last_cex_px_by_venue": cex_px_by_venue,
                "last_cex_px": cex_px_by_venue.get(t["cex_venue"]) if t["cex_venue"] else None,
                "l2_spread_bps": round(quotes[coin].spread_bps, 3) if coin in quotes else None,
                "cum_pnl_usd": round(cum_pnl, 4),
            }

    net_pnl_usd = price_pnl_usd + fund_pnl_usd + cost_pnl_usd
    gross_hl = sum(abs(float(p.get("hl_weight", 0.0))) for p in new_positions.values())
    gross_cex = sum(sum(abs(float(w)) for w in _cex_legs(p).values()) for p in new_positions.values())
    net_beta_hl = sum(float(p.get("hl_weight", 0.0)) + float(p.get("cex_weight", 0.0)) for p in new_positions.values())
    median_l2_bps = float(np.median([q.spread_bps for q in quotes.values()])) if quotes else np.nan

    tick_row = {
        "tick_hour_ms": tick_hour_ms,
        "equity_usd": new_equity,
        "net_pnl_usd": net_pnl_usd,
        "price_pnl_usd": price_pnl_usd,
        "fund_pnl_usd": fund_pnl_usd,
        "cost_pnl_usd": cost_pnl_usd,
        "turnover": total_turnover,
        "gross_hl": gross_hl,
        "gross_cex": gross_cex,
        "net_exposure": net_beta_hl,
        "active_coins": len(new_positions),
        "median_l2_spread_bps": median_l2_bps,
        "nav_guard_triggered": nav_guard_triggered,
    }

    # Seed cumulative totals from prior state (or back-fill from prior non-clamped ticks when upgrading)
    if (
        "cum_funding_usd" in state
        and "cum_price_pnl_usd" in state
        and "cum_costs_usd" in state
    ):
        prev_cum_fund = float(state.get("cum_funding_usd") or 0.0)
        prev_cum_px = float(state.get("cum_price_pnl_usd") or 0.0)
        prev_cum_costs = float(state.get("cum_costs_usd") or 0.0)
    elif ticks_path.exists():
        old_t = pd.read_parquet(ticks_path)
        if "tick_hour_ms" in old_t.columns:
            old_t = old_t[old_t["tick_hour_ms"] < tick_hour_ms]
        prev_cum_fund = float(old_t["fund_pnl_usd"].sum()) if "fund_pnl_usd" in old_t.columns else 0.0
        prev_cum_px = float(old_t["price_pnl_usd"].sum()) if "price_pnl_usd" in old_t.columns else 0.0
        prev_cum_costs = float((-old_t["cost_pnl_usd"]).clip(lower=0.0).sum()) if "cost_pnl_usd" in old_t.columns else 0.0
    else:
        prev_cum_fund = 0.0
        prev_cum_px = 0.0
        prev_cum_costs = 0.0

    cum_funding_usd = round(prev_cum_fund + fund_pnl_usd, 4)
    cum_price_pnl_usd = round(prev_cum_px + price_pnl_usd, 4)
    cum_costs_usd = round(prev_cum_costs + abs(cost_pnl_usd), 4)

    _append_parquet(ticks_path, pd.DataFrame([tick_row]), dedup_subset=["tick_hour_ms"])
    if trade_rows:
        _append_parquet(trades_path, pd.DataFrame(trade_rows), dedup_subset=["tick_hour_ms", "coin"])

    state_out = {
        "starting_capital_usd": float(state["starting_capital_usd"]),
        "equity_usd": new_equity,
        "cum_funding_usd": cum_funding_usd,
        "cum_price_pnl_usd": cum_price_pnl_usd,
        "cum_costs_usd": cum_costs_usd,
        # A clamped tick keeps the previous clock so the confirming tick covers the full interval.
        "last_tick_hour_ms": prev_hour_ms if nav_guard_triggered else tick_hour_ms,
        "tick_count": int(state.get("tick_count", 0)) + 1,
        "nav_guard_triggers": nav_guard_triggers,
        "nav_guard_triggered": nav_guard_triggered,
        "nav_guard_reason": nav_guard_reason,
        "nav_guard_pending_tick_hour_ms": nav_guard_pending,
        "last_forced_stale_exits": forced_stale_exits,
        "stale_excluded_coins": sorted(stale_excluded),
        "positions": new_positions,
        "sleeves": [{"name": s.spec.name, "weight": s.weight} for s in active_sleeves],
    }
    tmp_state = state_path.with_suffix(".tmp.json")
    tmp_state.write_text(json.dumps(state_out, indent=2))
    tmp_state.replace(state_path)

    return {"status": "ok", "forced_stale_exits": forced_stale_exits, **tick_row, "trades_count": len(trade_rows), "quotes_count": len(quotes)}


def load_telegram_credentials(env_paths: tuple[Path, ...] = ()) -> tuple[str, str, str | None]:
    """Read Telegram bot token, chat_id, and optional thread_id from env or fallback ``.env`` files.

    Accepts both legacy ``TELEGRAM_SIGNAL_*`` and standard ``TELEGRAM_*`` variable names.
    """
    vals = {
        "token": (
            os.getenv("TELEGRAM_SIGNAL_BOT_TOKEN", "").strip()
            or os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        ),
        "chat_id": (
            os.getenv("TELEGRAM_SIGNAL_CHAT_ID", "").strip()
            or os.getenv("TELEGRAM_CHAT_ID", "").strip()
        ),
        "thread_id": (
            os.getenv("TELEGRAM_SIGNAL_THREAD_ID", "").strip()
            or os.getenv("TELEGRAM_THREAD_ID", "").strip()
        ),
    }
    key_map = {
        "TELEGRAM_SIGNAL_BOT_TOKEN": "token",
        "TELEGRAM_BOT_TOKEN": "token",
        "TELEGRAM_SIGNAL_CHAT_ID": "chat_id",
        "TELEGRAM_CHAT_ID": "chat_id",
        "TELEGRAM_SIGNAL_THREAD_ID": "thread_id",
        "TELEGRAM_THREAD_ID": "thread_id",
    }
    if not (vals["token"] and vals["chat_id"]):
        for path in env_paths:
            if not path.is_file():
                continue
            for raw_line in path.read_text().splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                slot = key_map.get(key.strip())
                if slot and not vals[slot]:
                    vals[slot] = val.strip().strip('"').strip("'")
            if vals["token"] and vals["chat_id"]:
                break
    return vals["token"], vals["chat_id"], vals["thread_id"] or None


def format_telegram_summary(result: dict[str, Any], state: dict[str, Any]) -> str:
    """Format a Telegram rich HTML message with a monospace table for one HL paper tick."""
    ts = datetime.fromtimestamp(int(result["tick_hour_ms"]) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    start_cap = float(state.get("starting_capital_usd", 100_000.0))
    eq = float(result["equity_usd"])
    cum_pct = (eq / start_cap - 1.0) * 100.0 if start_cap > 0 else 0.0
    net_pnl = float(result["net_pnl_usd"])
    px_pnl = float(result["price_pnl_usd"])
    fn_pnl = float(result["fund_pnl_usd"])
    co_pnl = float(result["cost_pnl_usd"])
    med_sp = float(result.get("median_l2_spread_bps", float("nan")))
    sp_txt = f"{med_sp:.2f} bp" if math.isfinite(med_sp) else "n/a"
    pnl_icon = "🟢" if net_pnl >= 0 else "🔴"

    kpi_rows = [
        f"{'Equity':<10} ${eq:>10,.2f} ({cum_pct:+.2f}%)",
        f"{'Tick PnL':<10} ${net_pnl:>+10,.2f}",
        f"{'  Price':<10} ${px_pnl:>+10,.2f}",
        f"{'  Funding':<10} ${fn_pnl:>+10,.2f}",
        f"{'  L2+Fee':<10} ${co_pnl:>+10,.2f}",
        f"{'Gross HL':<10} {result['gross_hl']*100:>9.1f}% | CEX {result['gross_cex']*100:.1f}%",
        f"{'Active':<10} {int(result['active_coins']):>3d} coins | Trades {int(result['trades_count'])} | L2 {sp_txt}",
    ]

    lines = [
        "📊 <b>Hyperliquid Metamodel · Paper L2</b>",
        f"🕒 <code>{html.escape(ts)}</code> · Tick <b>#{state.get('tick_count', 1)}</b> · {pnl_icon} <b>{net_pnl:+,.2f}$</b>",
        "<blockquote><pre>" + html.escape("\n".join(kpi_rows)) + "</pre></blockquote>",
    ]

    positions: dict[str, dict[str, Any]] = state.get("positions", {})
    if positions:
        top_pos = sorted(
            positions.items(),
            key=lambda kv: -(abs(kv[1].get("hl_weight", 0.0)) + abs(kv[1].get("cex_weight", 0.0))),
        )[:10]
        header = f"{'Coin':<7} {'HL%':>6} {'CEX%':>6} {'Leg':<3} {'L2bp':>4}"
        sep = "-" * len(header)
        table_lines = [header, sep]
        for coin, p in top_pos:
            hw = float(p.get("hl_weight", 0.0)) * 100.0
            cw = float(p.get("cex_weight", 0.0)) * 100.0
            leg = str(p.get("cex_venue", ""))[:3].upper() if abs(cw) >= 0.05 else "HL"
            sp = p.get("l2_spread_bps")
            sp_cell = f"{float(sp):>4.1f}" if sp is not None and math.isfinite(float(sp)) else "   -"
            cex_cell = f"{cw:>+6.1f}" if abs(cw) >= 0.05 else "     -"
            table_lines.append(f"{coin[:7]:<7} {hw:>+6.1f} {cex_cell} {leg:<3} {sp_cell}")
        lines.append("<b>Top Positions</b>")
        lines.append("<pre>" + html.escape("\n".join(table_lines)) + "</pre>")

    lines.append("<i>Paper/shadow L2 mode · zero live orders.</i>")
    return "\n".join(lines)


def _format_price(px: float) -> str:
    if not math.isfinite(px) or px <= 0:
        return "—"
    if px >= 1000:
        return f"${px:,.0f}"
    if px >= 10:
        return f"${px:,.2f}"
    if px >= 0.1:
        return f"${px:.4f}"
    return f"${px:.5f}"


def _window_pnl(ticks_df: pd.DataFrame | None, current_hour_ms: int, hours_back: int) -> float | None:
    if ticks_df is None or ticks_df.empty or "tick_hour_ms" not in ticks_df.columns:
        return None
    cutoff = current_hour_ms - hours_back * 3_600_000
    sub = ticks_df[ticks_df["tick_hour_ms"] > cutoff]
    if sub.empty:
        return None
    return float(sub["net_pnl_usd"].sum())


def format_telegram_rich_html(
    result: dict[str, Any],
    state: dict[str, Any],
    *,
    ticks_df: pd.DataFrame | None = None,
    max_rows: int = 12,
) -> str:
    """Build a plain-Russian native Telegram Rich HTML table (``sendRichMessage``)."""
    tick_ms = int(result["tick_hour_ms"])
    dt = datetime.fromtimestamp(tick_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    start_cap = float(state.get("starting_capital_usd", 100_000.0))
    eq = float(result["equity_usd"])
    cum_pct = (eq / start_cap - 1.0) * 100.0 if start_cap > 0 else 0.0
    net_pnl = float(result["net_pnl_usd"])
    px_pnl = float(result["price_pnl_usd"])
    fn_pnl = float(result["fund_pnl_usd"])
    co_pnl = float(result["cost_pnl_usd"])
    total_deployed = eq * (float(result["gross_hl"]) + float(result["gross_cex"]))
    pnl_dot = "🟢" if net_pnl > 0 else ("🔴" if net_pnl < 0 else "⚪")

    pnl_24h = _window_pnl(ticks_df, tick_ms, 24)
    pnl_7d = _window_pnl(ticks_df, tick_ms, 168)
    win_parts = []
    if pnl_24h is not None:
        win_parts.append(f"<b>24ч:</b> {pnl_24h:+,.1f}$")
    if pnl_7d is not None:
        win_parts.append(f"<b>7д:</b> {pnl_7d:+,.1f}$")
    win_txt = (" · " + " · ".join(win_parts)) if win_parts else ""

    positions: dict[str, dict[str, Any]] = state.get("positions", {})
    top_pos = sorted(
        positions.items(),
        key=lambda kv: -(abs(kv[1].get("hl_weight", 0.0)) + abs(kv[1].get("cex_weight", 0.0))),
    )[:max_rows]

    parts = [
        f"<h1>📈 Hyperliquid · {html.escape(date_str)}</h1>",
        (
            f"<p><b>Бумажный счёт:</b> ${eq:,.0f} ({cum_pct:+.2f}%){win_txt}<br>"
            f"{pnl_dot} <b>За шаг:</b> {net_pnl:+,.1f}$ "
            f"(цена {px_pnl:+,.1f}$, фандинг {fn_pnl:+,.1f}$, комиссии {co_pnl:+,.1f}$)<br>"
            f"<b>В работе:</b> {int(result['active_coins'])} монет на ${total_deployed:,.0f} "
            f"({html.escape(time_str)}, тик #{int(state.get('tick_count', 1))})</p>"
        ),
    ]

    if top_pos:
        rows_html = [
            "<tr>"
            '<th align="left">Монета</th>'
            '<th align="left">Что открыто</th>'
            '<th align="right">Сумма</th>'
            '<th align="right">PnL</th>'
            '<th align="right">Цена</th>'
            "</tr>"
        ]
        for coin, p in top_pos:
            hw = float(p.get("hl_weight", 0.0))
            cw = float(p.get("cex_weight", 0.0))
            usd_size = eq * (abs(hw) + abs(cw))
            venue_raw = str(p.get("cex_venue", "bybit")).lower()
            venue = "OKX" if venue_raw == "okx" else venue_raw.capitalize()
            if abs(cw) >= 0.0005:
                if hw < 0 and cw > 0:
                    action = f"⚖️ Арб: Шорт HL + Лонг {venue}"
                elif hw > 0 and cw < 0:
                    action = f"⚖️ Арб: Лонг HL + Шорт {venue}"
                else:
                    action = f"⚖️ Арб: HL + {venue}"
            elif hw > 0:
                action = "🟢 Лонг на HL"
            else:
                action = "🔴 Шорт на HL"
            c_pnl = float(p.get("cum_pnl_usd", 0.0))
            c_dot = "🟢" if c_pnl > 0.05 else ("🔴" if c_pnl < -0.05 else "⚪")
            px = float(p.get("last_hl_px", 0.0))
            rows_html.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(coin)}</b></td>'
                f'<td align="left">{html.escape(action)}</td>'
                f'<td align="right">${usd_size:,.0f}</td>'
                f'<td align="right">{c_dot} {c_pnl:+,.1f}$</td>'
                f'<td align="right">{html.escape(_format_price(px))}</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows_html) + "</table>")

    parts.append("<footer>Бумажный тест по реальному стакану (HL + Bybit/Binance/OKX) · реальные деньги не используются</footer>")
    return "".join(parts)


def send_telegram_rich_message(
    rich_html: str,
    *,
    bot_token: str,
    chat_id: str,
    thread_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Send a native Telegram Rich Message (Bot API 10.1+ ``sendRichMessage``)."""
    if not bot_token or not chat_id:
        raise ValueError("bot_token and chat_id are required")
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "rich_message": {"html": rich_html},
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    with httpx.Client(timeout=20.0, transport=transport) as client:
        resp = client.post(f"https://api.telegram.org/bot{bot_token}/sendRichMessage", json=payload)
        _raise_for_status_redacted(resp)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body}")
        return {"ok": True, "message_id": body.get("result", {}).get("message_id")}


def _raise_for_status_redacted(resp: httpx.Response) -> None:
    """Like ``raise_for_status`` but never leaks the bot token embedded in the request URL."""
    if resp.is_error:
        raise RuntimeError(f"Telegram API HTTP {resp.status_code}: {resp.text[:300]}")


def send_telegram_message(
    text: str,
    *,
    bot_token: str,
    chat_id: str,
    thread_id: str | None = None,
    transport: httpx.BaseTransport | None = None,
) -> dict[str, Any]:
    """Send an HTML message via Telegram Bot API."""
    if not bot_token or not chat_id:
        raise ValueError("bot_token and chat_id are required")
    payload: dict[str, Any] = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    with httpx.Client(timeout=15.0, transport=transport) as client:
        resp = client.post(f"https://api.telegram.org/bot{bot_token}/sendMessage", json=payload)
        _raise_for_status_redacted(resp)
        body = resp.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram API error: {body}")
        return {"ok": True, "message_id": body.get("result", {}).get("message_id")}
