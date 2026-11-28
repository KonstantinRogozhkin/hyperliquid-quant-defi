"""Read-only HIP-3 Cross-Builder Paper Tracker with Split-Margin & Oracle-Divergence Guardrails.

Zero orders placed. Tracks live dual-leg HIP-3 builder-perp arbitrage (`xyz` × `io` × `para`)
on HyperCore L1 with realistic execution accounting:
- **Zero-Bridge, Builder-Specific Fees:** uses live per-builder `est_taker_fee_bps` (or maker)
  derived from `growthMode` and `deployerFeeScale` (e.g. `1.8 bps` entry / `3.6 bps` RT taker
  when both legs are in Growth Mode).
- **Split-Margin Accounting:** each HIP-3 builder DEX clears and margins independently (no
  cross-DEX netting), so each pair locks `2 * leg_notional / per_leg_leverage` margin.
- **Oracle-Divergence & Persistence Guards:** refuses entries with `oracle_risk_warning` or
  `oracle_divergence_bps > max_oracle_div_bps`, requires `clean_streak >= min_clean_streak`
  (unchanged legs, no gaps), and emergency-exits if deployer oracles decouple beyond
  `exit_oracle_div_bps`.
- **Isolated-Market Sizing Discipline:** halves notional (`isolated_slot_scale = 0.5`) on
  `onlyIsolated` builder legs (or skips them entirely when `allow_only_isolated=False`).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
from pathlib import Path
import time
from typing import Any

import httpx
import pandas as pd

from hl_quant import cross_dex_scanner as cds
from hl_quant import hl_hip3_builder_arb as hip3
from hl_quant import persistence_clock as pc
from hl_quant.persistence_clock import streak_count, update_streak_map


NAV_GUARD_CONFIRM_MS = 3_600_000


@dataclass(frozen=True, slots=True)
class Hip3PaperConfig:
    starting_capital_usd: float = 50_000.0
    max_slots: int = 5
    slot_notional_usd: float = 5_000.0  # per leg on Cross markets -> $10k gross pair
    isolated_slot_scale: float = 0.5  # half clip ($2.5k/leg) on onlyIsolated builder legs
    per_leg_leverage: float = 2.0  # independent HIP-3 DEX margin: $2.5k/DEX = $5k total margin per $5k/leg pair
    max_margin_utilization: float = 0.80  # keep >= 20% free equity buffer
    min_leg_vol_usd: float = 200_000.0
    entry_apr_pct: float = 20.0  # matches frontier_persistence clean streak bar
    exit_apr_pct: float = 6.0  # hysteresis exit when directional carry decays below 6%
    min_clean_streak: int = 2  # consecutive clean same-direction ticks required to open
    max_oracle_div_bps: float = 15.0  # reject pairs with builder oracle divergence > 15 bps
    exit_oracle_div_bps: float = 35.0  # emergency exit if oracles diverge > 35 bps while held
    max_l2_slip_bps: float = 6.0  # reject thin builder books with > 6 bps L2 VWAP slip
    min_exec_basis_bps: float = -5.0  # refuse entries starting deep underwater on L2 basis
    max_book_mark_divergence_bps: float = 10.0  # reject stale/async L2 book vs mark basis divergence
    max_payback_hours: float = 24.0  # reject slow-payback pairs (> 1 day)
    use_taker_fees: bool = True  # conservative atomic two-leg crossing assumption
    allow_only_isolated: bool = True  # track both Cross and half-sized Isolated pairs in paper
    max_missing_ticks: int = 3  # tolerate transient missing market before force-closing
    min_streak_spacing_hours: float = 3.5  # local clean streak advances at most once per 4h window
    max_tick_nav_jump_pct: float = 8.0  # Bounded NAV sanity guard

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_or_init_state(state_path: Path, cfg: Hip3PaperConfig) -> dict[str, Any]:
    if state_path.is_file():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st.setdefault("clean_streaks", {})
        st.setdefault("positions", {})
        st.setdefault("closed_pairs_count", 0)
        st.setdefault("payback_achieved_count", 0)
        st.setdefault("nav_guard_triggers", 0)
        st.setdefault("nav_guard_triggered", False)
        st.setdefault("nav_guard_reason", "")
        st.setdefault("nav_guard_pending_ms", None)
        st.setdefault("nav_guard_pending_ret_pct", None)
        st.setdefault("last_clamped_ms", 0)
        return st
    return {
        "starting_capital_usd": cfg.starting_capital_usd,
        "equity_usd": cfg.starting_capital_usd,
        "return_pct": 0.0,
        "margin_locked_usd": 0.0,
        "margin_utilization_pct": 0.0,
        "net_delta_usd": 0.0,
        "cum_funding_usd": 0.0,
        "cum_basis_mtm_usd": 0.0,
        "cum_costs_usd": 0.0,
        "last_tick_ms": 0,
        "last_clamped_ms": 0,
        "ny_session": "",
        "positions": {},  # base_symbol -> position dict
        "clean_streaks": {},  # "BASE:short_sym->long_sym" -> consecutive clean ticks
        "closed_pairs_count": 0,
        "payback_achieved_count": 0,
        "nav_guard_triggers": 0,
        "nav_guard_triggered": False,
        "nav_guard_reason": "",
        "nav_guard_pending_ms": None,
        "nav_guard_pending_ret_pct": None,
    }


def _append_parquet(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    new_df = pd.DataFrame(rows)
    if path.is_file():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    tmp = path.with_suffix(".tmp.parquet")
    combined.to_parquet(tmp, index=False)
    tmp.replace(path)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


def _load_external_clean_streaks(frontier_summary_path: Path | None) -> dict[str, int]:
    """Read verified zero-flip active clean streaks from ``hl_frontier_history/latest_summary.json``."""
    if frontier_summary_path is None or not frontier_summary_path.is_file():
        return {}
    try:
        raw = json.loads(frontier_summary_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, int] = {}
    for row in raw.get("hip3_persistence") or []:
        if not isinstance(row, dict):
            continue
        if row.get("active_in_latest_tick") is False:
            continue
        if int(row.get("direction_flips", 0) or 0) > 0:
            continue
        base = str(row.get("base_symbol") or "").upper()
        short_sym = str(row.get("short_symbol") or "")
        long_sym = str(row.get("long_symbol") or "")
        streak = int(row.get("clean_streak", 0) or 0)
        if base and short_sym and long_sym and streak > 0:
            out[f"{base}:{short_sym}->{long_sym}"] = streak
    return out


def _leg_fee_bps(mkt: hip3.Hip3BuilderMarket, *, use_taker: bool) -> float:
    return float(mkt.est_taker_fee_bps if use_taker else mkt.est_maker_fee_bps)


def _pair_gross_notional_usd(pos: dict[str, Any]) -> float:
    s_n = pos.get("short_notional_usd")
    l_n = pos.get("long_notional_usd")
    if s_n is not None and l_n is not None:
        return max(0.0, float(s_n)) + max(0.0, float(l_n))
    return 2.0 * max(0.0, float(pos.get("leg_notional_usd", 0.0)))


def settled_funding_by_position(
    client: Any,
    positions: dict[str, dict[str, Any]],
    symbols_of: Any,
    prev_ms: int,
    ts_ms: int,
    *,
    cap_hours: float | None = None,
    info_url: str = hip3.HL_INFO_URL,
) -> dict[Any, float]:
    """Realized ``fundingHistory`` rate sums per ``(start, symbol)`` and ``symbol``.

    ``symbols_of(pos)`` yields the HL symbols to fetch for a position. Symbols missing from the
    result (no history / unsupported) fall back to the snapshot rate in the caller.
    """
    if client is None or prev_ms <= 0 or not positions:
        return {}
    from hl_quant.hl_spot_perp_arb import fetch_settled_funding_sum

    by_start: dict[int, set[str]] = {}
    for pos in positions.values():
        start = pc.position_accrual_start_ms(pos, prev_ms)
        if cap_hours is not None and cap_hours > 0:
            # Same window as the snapshot-rate fallback used for the other leg (dt_cap_h), so equal
            # legs keep cancelling after a long gap.
            start = max(start, ts_ms - int(cap_hours * 3_600_000))
        if start > 0 and (ts_ms // 3_600_000) - (start // 3_600_000) > 0:
            by_start.setdefault(start, set()).update(str(s) for s in symbols_of(pos) if s)
    out: dict[Any, float] = {}
    for start, syms in by_start.items():
        try:
            fetched = fetch_settled_funding_sum(client, sorted(syms), start + 1, ts_ms, info_url=info_url)
            for sym, val in fetched.items():
                out[sym] = val
                out[(start, sym)] = val
        except Exception:  # noqa: BLE001 - fall back to snapshot-rate accrual
            continue
    return out


def run_hip3_paper_tick(
    client: httpx.Client | None = None,
    *,
    state_dir: Path,
    cfg: Hip3PaperConfig = Hip3PaperConfig(),
    now_ms: int | None = None,
    markets: list[hip3.Hip3BuilderMarket] | None = None,
    book_fetcher: Any = None,
    frontier_summary_path: Path | None = None,
    info_url: str = hip3.HL_INFO_URL,
) -> dict[str, Any]:
    """Run one idempotent HIP-3 cross-builder L2 paper tick and update the split-margin ledger."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    ny_session = hip3.classify_ny_tradfi_session(ts_ms)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = _load_or_init_state(state_path, cfg)

    prev_ms = int(state.get("last_tick_ms", 0))
    last_clamped_ms = int(state.get("last_clamped_ms", 0) or 0)
    same_prev_hour = prev_ms > 0 and (ts_ms // 3_600_000) <= (prev_ms // 3_600_000)
    same_clamped_hour = (
        bool(state.get("nav_guard_pending_ms"))
        and last_clamped_ms > 0
        and (ts_ms // 3_600_000) <= (last_clamped_ms // 3_600_000)
    )
    if same_prev_hour or same_clamped_hour:
        pos_snap = state.get("positions") or {}
        gross_snap = round(sum(_pair_gross_notional_usd(p) for p in pos_snap.values()), 2)
        eq_snap = max(float(state["equity_usd"]), 1.0)
        return {
            "status": "duplicate_hour_skipped",
            "tick": {
                "ts_ms": ts_ms,
                "ny_session": ny_session,
                "dt_hours": 0.0,
                "equity_usd": state["equity_usd"],
                "liquidation_equity_usd": state.get("liquidation_equity_usd", state["equity_usd"]),
                "active_pairs": len(pos_snap),
                "gross_notional_usd": gross_snap,
                "gross_leverage": round(gross_snap / eq_snap, 3),
                "margin_locked_usd": state.get("margin_locked_usd", 0.0),
                "margin_utilization_pct": state.get("margin_utilization_pct", 0.0),
                "net_delta_usd": state.get("net_delta_usd", 0.0),
                "tick_funding_usd": 0.0,
                "tick_basis_mtm_usd": 0.0,
                "tick_costs_usd": 0.0,
                "tick_net_pnl_usd": 0.0,
                "opened_count": 0,
                "closed_count": 0,
                "nav_guard_triggered": bool(state.get("nav_guard_triggered", False)),
            },
            "state": state,
            "trades": [],
            "gated_stats": {},
            "markets_scanned": 0,
            "opps_evaluated": 0,
        }

    own_client = client is None and (markets is None or book_fetcher is None)
    http_client = client or (httpx.Client(timeout=20.0) if own_client else None)
    try:
        if markets is None:
            assert http_client is not None
            _, live_markets = hip3.fetch_hip3_builders_and_markets(http_client, info_url=info_url, min_day_vol_usd=0.0)
        else:
            live_markets = list(markets)

        def _default_book(sym: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
            assert http_client is not None
            return cds.fetch_dex_l2_book(http_client, "hyperliquid", sym, info_url=info_url)

        get_book = book_fetcher or _default_book
        by_symbol: dict[str, hip3.Hip3BuilderMarket] = {m.full_symbol: m for m in live_markets}

        dt_cap_h = 168.0 if state.get("nav_guard_pending_ms") else 24.0
        dt_hours = max(0.0, min(dt_cap_h, (ts_ms - prev_ms) / 3_600_000.0)) if prev_ms > 0 else 0.0

        positions: dict[str, dict[str, Any]] = state.get("positions") or {}
        positions_backup = {k: dict(v) for k, v in positions.items()}
        tick_funding_usd = 0.0
        tick_basis_mtm_usd = 0.0
        tick_costs_usd = 0.0
        trade_rows: list[dict[str, Any]] = []

        # 1. Mark-to-market & accrue hourly funding on open dual-builder pairs; realized settlements
        # per leg (``fundingHistory``) when live, snapshot rate x hours as fallback.
        settled = (
            settled_funding_by_position(
                http_client,
                positions,
                lambda p: [p.get("short_symbol"), p.get("long_symbol")],
                prev_ms,
                ts_ms,
                cap_hours=dt_cap_h,
                info_url=info_url,
            )
            if markets is None
            else {}
        )
        for _base, pos in list(positions.items()):
            s_mkt = by_symbol.get(str(pos["short_symbol"]))
            l_mkt = by_symbol.get(str(pos["long_symbol"]))
            if not (s_mkt and l_mkt):
                continue
            notional = float(pos["leg_notional_usd"])
            prev_s = float(pos["last_short_px"])
            prev_l = float(pos["last_long_px"])
            entry_s = float(pos.get("entry_short_px") or prev_s)
            entry_l = float(pos.get("entry_long_px") or prev_l)
            short_qty = float(pos.get("short_qty") or (notional / entry_s if entry_s > 0 else 0.0))
            long_qty = float(pos.get("long_qty") or (notional / entry_l if entry_l > 0 else 0.0))
            # Short leg (-short_qty) profits when price drops; long leg (+long_qty) profits when price rises.
            basis_pnl = -short_qty * (s_mkt.mark_px - prev_s) + long_qty * (l_mkt.mark_px - prev_l)
            s_notional = short_qty * s_mkt.mark_px
            l_notional = long_qty * l_mkt.mark_px
            # Per-position clock: a tick skipped for a missing quote keeps its funding interval.
            pos_prev_ms = pc.position_accrual_start_ms(pos, prev_ms)
            eff_start = max(pos_prev_ms, ts_ms - int(dt_cap_h * 3_600_000)) if dt_cap_h > 0 else pos_prev_ms
            pos_settled_hours = pc.settled_hours_between(pos_prev_ms, ts_ms, cap_hours=dt_cap_h) if prev_ms > 0 else 0.0
            pos_dt_hours = max(0.0, min(dt_cap_h, (ts_ms - pos_prev_ms) / 3_600_000.0)) if pos_prev_ms > 0 else dt_hours
            pos["last_accrual_ms"] = ts_ms
            # Short receives positive funding (+s_mkt.funding_rate_h); long pays positive funding (-l_mkt.funding_rate_h).
            s_sym, l_sym = str(pos["short_symbol"]), str(pos["long_symbol"])
            s_rate_sum = float(settled.get((eff_start, s_sym), settled.get(s_sym, s_mkt.funding_rate_h * pos_settled_hours)))
            l_rate_sum = float(settled.get((eff_start, l_sym), settled.get(l_sym, l_mkt.funding_rate_h * pos_settled_hours)))
            fund_pnl = s_notional * s_rate_sum - l_notional * l_rate_sum

            live_spread_apr = round(s_mkt.funding_apr_pct - l_mkt.funding_apr_pct, 2)
            live_mark_basis = round((s_mkt.mark_px - l_mkt.mark_px) / l_mkt.mark_px * 1e4, 2) if l_mkt.mark_px > 0 else 0.0
            live_ora_div = (
                round(abs(s_mkt.oracle_px - l_mkt.oracle_px) / l_mkt.oracle_px * 1e4, 2)
                if l_mkt.oracle_px > 0
                else 0.0
            )
            lev_p = max(float(pos.get("per_leg_leverage") or cfg.per_leg_leverage), 1.0)
            init_leg_m = notional / lev_p
            short_upnl = -short_qty * (s_mkt.mark_px - entry_s)
            long_upnl = long_qty * (l_mkt.mark_px - entry_l)
            s_mmr = 0.5 / max(1.0, float(s_mkt.max_leverage or 10))
            l_mmr = 0.5 / max(1.0, float(l_mkt.max_leverage or 10))
            leg_liquidated = (
                (init_leg_m + short_upnl) <= s_notional * s_mmr
                or (init_leg_m + long_upnl) <= l_notional * l_mmr
            )
            # Do NOT count unrealized profit (> 0) on the winning leg as locked margin!
            dyn_margin = max(0.0, max(init_leg_m + min(0.0, short_upnl), s_notional / lev_p)) + max(
                0.0, max(init_leg_m + min(0.0, long_upnl), l_notional / lev_p)
            )

            pos["short_qty"] = round(short_qty, 8)
            pos["long_qty"] = round(long_qty, 8)
            pos["short_notional_usd"] = round(s_notional, 2)
            pos["long_notional_usd"] = round(l_notional, 2)
            pos["net_delta_usd"] = round(l_notional - s_notional, 2)
            pos["margin_locked_usd"] = round(dyn_margin, 2)
            pos["split_margin_leg_liquidated"] = bool(leg_liquidated)
            pos["last_short_px"] = s_mkt.mark_px
            pos["last_long_px"] = l_mkt.mark_px
            pos["hours_held"] = round(float(pos.get("hours_held", 0.0)) + pos_dt_hours, 2)
            pos["cum_funding_usd"] = round(float(pos.get("cum_funding_usd", 0.0)) + fund_pnl, 4)
            pos["cum_basis_mtm_usd"] = round(float(pos.get("cum_basis_mtm_usd", 0.0)) + basis_pnl, 4)
            pos["live_spread_apr_pct"] = live_spread_apr
            pos["live_mark_basis_bps"] = live_mark_basis
            pos["live_oracle_div_bps"] = live_ora_div
            pos["both_growth_mode"] = bool(s_mkt.growth_mode and l_mkt.growth_mode)
            pos["net_pnl_usd"] = round(
                pos["cum_funding_usd"] + pos["cum_basis_mtm_usd"] - float(pos.get("cum_cost_usd", 0.0)),
                4,
            )
            pos["payback_achieved"] = bool(pos["cum_funding_usd"] >= float(pos.get("entry_cost_usd", 0.0)))

            tick_funding_usd += fund_pnl
            tick_basis_mtm_usd += basis_pnl

        # 1b. Bounded NAV Sanity Guard (clamp anomalous single-tick MTM jump until >= 1h confirmation)
        prev_equity = float(state.get("equity_usd", cfg.starting_capital_usd))
        raw_tick_pnl = tick_funding_usd + tick_basis_mtm_usd
        raw_ret_pct = (raw_tick_pnl / prev_equity * 100.0) if prev_equity > 0 else 0.0
        pending_raw = state.get("nav_guard_pending_ms")
        nav_guard_pending: int | None = int(pending_raw) if pending_raw else None
        pending_ret_raw = state.get("nav_guard_pending_ret_pct")
        nav_guard_pending_ret: float | None = float(pending_ret_raw) if pending_ret_raw is not None else None
        jump_exceeded = (
            prev_ms > 0
            and prev_equity > 0
            and cfg.max_tick_nav_jump_pct > 0.0
            and abs(raw_ret_pct) > cfg.max_tick_nav_jump_pct
        )
        nav_guard_triggered = False
        nav_guard_reason = ""
        if jump_exceeded and nav_guard_pending is not None and ts_ms - nav_guard_pending >= NAV_GUARD_CONFIRM_MS:
            confirmed = nav_guard_pending_ret is None or (
                (raw_ret_pct * nav_guard_pending_ret) > 0.0
                and abs(raw_ret_pct - nav_guard_pending_ret)
                <= max(cfg.max_tick_nav_jump_pct, 0.5 * abs(nav_guard_pending_ret))
            )
            if confirmed:
                nav_guard_reason = (
                    f"Tick NAV jump {raw_ret_pct:+.2f}% persisted since the clamped tick; "
                    "accepted as genuine"
                )
                nav_guard_pending = None
                nav_guard_pending_ret = None
                jump_exceeded = False
            else:
                nav_guard_pending = ts_ms
                nav_guard_pending_ret = round(raw_ret_pct, 6)
        elif not jump_exceeded:
            nav_guard_pending = None
            nav_guard_pending_ret = None
        if jump_exceeded:
            if nav_guard_pending is None:
                nav_guard_pending = ts_ms
            if nav_guard_pending_ret is None:
                nav_guard_pending_ret = round(raw_ret_pct, 6)
            nav_guard_triggered = True
            nav_guard_reason = (
                f"Tick NAV jump {raw_ret_pct:+.2f}% exceeded "
                f"+/-{cfg.max_tick_nav_jump_pct:.1f}% guard; clamping MTM & freezing rebalance"
            )
            state["nav_guard_triggers"] = int(state.get("nav_guard_triggers", 0)) + 1
            positions = positions_backup
            tick_funding_usd = 0.0
            tick_basis_mtm_usd = 0.0

        # 2. Evaluate exits: split-margin leg liquidation, carry decay, oracle divergence spike, or delisted/missing legs
        hip3_online = bool(by_symbol)
        closed_this_tick: set[str] = set()
        for base in (list(positions.keys()) if not nav_guard_triggered else []):
            pos = positions[base]
            s_mkt = by_symbol.get(str(pos["short_symbol"]))
            l_mkt = by_symbol.get(str(pos["long_symbol"]))
            exit_reason = ""
            if bool(pos.get("split_margin_leg_liquidated", False)):
                exit_reason = "split_margin_leg_liquidation"
            elif not (s_mkt and l_mkt):
                if not hip3_online:
                    continue
                # Wall-clock ticks, not invocations (see cross_dex_paper).
                pos["missing_streak"] = pc.advance_streak(pos.get("missing_streak"), qualifies=True, now_ms=ts_ms)
                miss = streak_count(pos["missing_streak"])
                pos["missing_quote_ticks"] = miss
                if miss >= cfg.max_missing_ticks:
                    exit_reason = "delisted_or_missing"
            else:
                pos["missing_quote_ticks"] = 0
                pos.pop("missing_streak", None)
                live_apr = float(pos.get("live_spread_apr_pct", 0.0))
                live_ora = float(pos.get("live_oracle_div_bps", 0.0))
                if live_ora > cfg.exit_oracle_div_bps:
                    exit_reason = "oracle_divergence"
                elif live_apr < cfg.exit_apr_pct:
                    exit_reason = "apr_decay"

            if exit_reason:
                notional = float(pos["leg_notional_usd"])
                s_qty = float(pos.get("short_qty") or 0.0)
                l_qty = float(pos.get("long_qty") or 0.0)
                exit_notional = (
                    0.5 * (s_qty * s_mkt.mark_px + l_qty * l_mkt.mark_px)
                    if (s_mkt and l_mkt and s_qty > 0 and l_qty > 0)
                    else notional
                )
                l2_slip = 2.0
                if s_mkt and l_mkt:
                    sb, sa = get_book(s_mkt.full_symbol)
                    lb, la = get_book(l_mkt.full_symbol)
                    # Closing reverses legs: buy back short_symbol (ask) & sell long_symbol (bid) -> short_hl=False
                    _, sl, full_ok = cds.compute_executable_cross_dex_entry_bps(
                        sb, sa, lb, la, short_hl=False, clip_usd=exit_notional
                    )
                    l2_slip = sl if (sl is not None and full_ok) else max(float(sl or 0.0), 2.0)
                    exit_fee_bps = _leg_fee_bps(s_mkt, use_taker=cfg.use_taker_fees) + _leg_fee_bps(
                        l_mkt, use_taker=cfg.use_taker_fees
                    )
                else:
                    exit_fee_bps = float(pos.get("entry_fee_bps", 1.8))
                exit_cost_bps = exit_fee_bps + l2_slip
                exit_cost_usd = exit_notional * exit_cost_bps * 1e-4
                tick_costs_usd += exit_cost_usd
                pos["cum_cost_usd"] = round(float(pos.get("cum_cost_usd", 0.0)) + exit_cost_usd, 4)
                final_net = round(pos["cum_funding_usd"] + pos["cum_basis_mtm_usd"] - pos["cum_cost_usd"], 4)
                state["closed_pairs_count"] = int(state.get("closed_pairs_count", 0)) + 1
                if (pos["cum_funding_usd"] + max(0.0, pos["cum_basis_mtm_usd"])) >= pos["cum_cost_usd"]:
                    state["payback_achieved_count"] = int(state.get("payback_achieved_count", 0)) + 1
                trade_rows.append(
                    {
                        "ts_ms": ts_ms,
                        "action": "CLOSE",
                        "exit_reason": exit_reason,
                        "base_symbol": base,
                        "short_symbol": pos["short_symbol"],
                        "long_symbol": pos["long_symbol"],
                        "any_only_isolated": bool(pos.get("any_only_isolated", False)),
                        "both_growth_mode": bool(pos.get("both_growth_mode", False)),
                        "leg_notional_usd": notional,
                        "margin_locked_usd": float(pos.get("margin_locked_usd", 0.0)),
                        "spread_apr_pct": float(pos.get("live_spread_apr_pct", 0.0)),
                        "oracle_div_bps": float(pos.get("live_oracle_div_bps", 0.0)),
                        "l2_slip_bps": round(l2_slip, 2),
                        "cost_usd": round(exit_cost_usd, 4),
                        "hours_held": float(pos.get("hours_held", 0.0)),
                        "final_net_pnl_usd": final_net,
                    }
                )
                positions.pop(base, None)
                closed_this_tick.add(base)

        # 3. Scan cross-builder candidates and update local clean streaks
        opps = hip3.scan_hip3_cross_builder_arb(
            live_markets,
            min_leg_vol_usd=cfg.min_leg_vol_usd,
            min_spread_apr_pct=cfg.entry_apr_pct,
            book_fetcher=None,  # L2 books fetched below only for streak-qualified candidates
        )
        clean_keys = [
            f"{opp.base_symbol}:{opp.short_symbol}->{opp.long_symbol}"
            for opp in opps
            if (
                opp.spread_apr_pct >= cfg.entry_apr_pct
                and not opp.oracle_risk_warning
                and opp.oracle_divergence_bps <= cfg.max_oracle_div_bps
            )
        ]
        if nav_guard_triggered:
            new_streaks = dict(state.get("clean_streaks") or {})
        else:
            new_streaks = update_streak_map(
                state.get("clean_streaks") or {},
                clean_keys,
                now_ms=ts_ms,
                min_spacing_hours=cfg.min_streak_spacing_hours,
            )
            state["clean_streaks"] = new_streaks
        external_streaks = _load_external_clean_streaks(frontier_summary_path)

        # 4. Open qualified new pairs within slot & split-margin limits
        opened_symbols: list[str] = []
        gated_stats = {
            "skipped_oracle": 0,
            "skipped_isolated": 0,
            "skipped_streak": 0,
            "skipped_l2_slip": 0,
            "skipped_basis": 0,
            "skipped_basis_source": 0,
            "skipped_payback": 0,
            "skipped_margin": 0,
        }
        est_equity = float(state["equity_usd"]) + tick_funding_usd + tick_basis_mtm_usd - tick_costs_usd
        used_margin = sum(float(p.get("margin_locked_usd", 0.0)) for p in positions.values())
        max_margin_usd = max(0.0, est_equity * cfg.max_margin_utilization)

        for opp in (opps if not nav_guard_triggered else []):
            if len(positions) >= cfg.max_slots:
                break
            if opp.base_symbol in positions or opp.base_symbol in closed_this_tick:
                continue
            if opp.oracle_risk_warning or opp.oracle_divergence_bps > cfg.max_oracle_div_bps:
                gated_stats["skipped_oracle"] += 1
                continue
            if opp.any_only_isolated and not cfg.allow_only_isolated:
                gated_stats["skipped_isolated"] += 1
                continue

            key = f"{opp.base_symbol}:{opp.short_symbol}->{opp.long_symbol}"
            local_streak = streak_count(new_streaks.get(key))
            # The frontier tracker counts streaks at a looser oracle cap (25 bps) with no wall-clock
            # spacing. It may only bootstrap a cold ledger (first tick ever); once this ledger has its
            # own history, a pair dirty on earlier ticks must not open on its first clean tick just
            # because the external count says so (SKHX at 16-25 bps -> 11 bps).
            if prev_ms <= 0 and local_streak >= 1:
                eff_streak = max(local_streak, int(external_streaks.get(key, 0)))
            else:
                eff_streak = local_streak
            if eff_streak < cfg.min_clean_streak:
                gated_stats["skipped_streak"] += 1
                continue

            leg_notional = round(
                cfg.slot_notional_usd * (cfg.isolated_slot_scale if opp.any_only_isolated else 1.0), 2
            )
            pair_margin = round((2.0 * leg_notional) / max(cfg.per_leg_leverage, 1.0), 2)
            if used_margin + pair_margin > max_margin_usd + 1e-6:
                gated_stats["skipped_margin"] += 1
                continue

            s_mkt = by_symbol.get(opp.short_symbol)
            l_mkt = by_symbol.get(opp.long_symbol)
            if not (s_mkt and l_mkt):
                continue

            sb, sa = get_book(opp.short_symbol)
            lb, la = get_book(opp.long_symbol)
            exec_basis, l2_slip, full_ok = cds.compute_executable_cross_dex_entry_bps(
                sb, sa, lb, la, short_hl=True, clip_usd=leg_notional
            )
            if not full_ok or l2_slip is None or exec_basis is None or l2_slip > cfg.max_l2_slip_bps:
                gated_stats["skipped_l2_slip"] += 1
                continue
            book_mid_basis = cds.compute_book_mid_basis_bps(sb, sa, lb, la, short_hl=True)
            if book_mid_basis is None or abs(book_mid_basis - opp.mark_basis_bps) > cfg.max_book_mark_divergence_bps:
                gated_stats["skipped_basis_source"] += 1
                continue
            # Positions are marked on mark_px: an L2 basis better than the mark basis never realizes,
            # and the part of the mark basis explained by builder oracle divergence is structural
            # (it does not converge to zero), so it is never credited as an edge.
            mark_credit = cds.credited_entry_basis_bps(exec_basis, opp.mark_basis_bps)
            if mark_credit < cfg.min_exec_basis_bps:
                gated_stats["skipped_basis"] += 1
                continue
            credited_basis = (
                max(0.0, mark_credit - abs(float(opp.oracle_divergence_bps))) if mark_credit > 0 else mark_credit
            )

            entry_fee_bps = _leg_fee_bps(s_mkt, use_taker=cfg.use_taker_fees) + _leg_fee_bps(
                l_mkt, use_taker=cfg.use_taker_fees
            )
            rt_fee_bps = 2.0 * entry_fee_bps
            # Conservative 50% credit on positive entry basis, capped at one-way entry fee.
            eff_exec_basis = (0.5 * credited_basis) if credited_basis > 0 else credited_basis
            basis_credit = min(max(float(eff_exec_basis), -50.0), entry_fee_bps)
            # exec_basis ALREADY subtracts 1x entry L2 slip from mid basis; add 1x l2_slip for exit.
            est_rt_cost_bps = max(1.0, rt_fee_bps + 1.0 * l2_slip - basis_credit)
            hourly_bps = opp.spread_apr_pct / (hip3.HOURS_PER_YEAR * 100.0) * 1e4
            be_hours = (est_rt_cost_bps / hourly_bps) if hourly_bps > 1e-9 else float("inf")
            if be_hours > cfg.max_payback_hours:
                gated_stats["skipped_payback"] += 1
                continue

            entry_cost_bps = entry_fee_bps + l2_slip
            entry_cost_usd = leg_notional * entry_cost_bps * 1e-4
            tick_costs_usd += entry_cost_usd
            used_margin += pair_margin

            positions[opp.base_symbol] = {
                "base_symbol": opp.base_symbol,
                "short_symbol": opp.short_symbol,
                "long_symbol": opp.long_symbol,
                "short_dex": opp.short_dex,
                "long_dex": opp.long_dex,
                "both_growth_mode": bool(opp.both_growth_mode),
                "any_only_isolated": bool(opp.any_only_isolated),
                "margin_mode": "isolated" if opp.any_only_isolated else "cross",
                "leg_notional_usd": leg_notional,
                "short_qty": round(leg_notional / s_mkt.mark_px, 8) if s_mkt.mark_px > 0 else 0.0,
                "long_qty": round(leg_notional / l_mkt.mark_px, 8) if l_mkt.mark_px > 0 else 0.0,
                "short_notional_usd": leg_notional,
                "long_notional_usd": leg_notional,
                "net_delta_usd": 0.0,
                "margin_locked_usd": pair_margin,
                "per_leg_leverage": cfg.per_leg_leverage,
                "opened_ms": ts_ms,
                "last_accrual_ms": ts_ms,
                "hours_held": 0.0,
                "entry_short_px": s_mkt.mark_px,
                "entry_long_px": l_mkt.mark_px,
                "last_short_px": s_mkt.mark_px,
                "last_long_px": l_mkt.mark_px,
                "entry_spread_apr_pct": opp.spread_apr_pct,
                "live_spread_apr_pct": opp.spread_apr_pct,
                # Return on the 2 * N / L margin locked across both DEXs: spread * L / 2.
                "net_apr_split_margin_pct": round(opp.spread_apr_pct * cfg.per_leg_leverage / 2.0, 2),
                "entry_oracle_div_bps": opp.oracle_divergence_bps,
                "live_oracle_div_bps": opp.oracle_divergence_bps,
                "entry_fee_bps": round(entry_fee_bps, 2),
                "entry_l2_slip_bps": round(l2_slip, 2),
                "entry_exec_basis_bps": round(exec_basis, 2),
                "entry_credited_basis_bps": round(credited_basis, 2),
                "live_mark_basis_bps": opp.mark_basis_bps,
                "entry_payback_hours": round(be_hours, 1),
                "entry_cost_usd": round(entry_cost_usd, 4),
                "cum_cost_usd": round(entry_cost_usd, 4),
                "cum_funding_usd": 0.0,
                "cum_basis_mtm_usd": 0.0,
                "net_pnl_usd": round(-entry_cost_usd, 4),
                "payback_achieved": False,
                "clean_streak": eff_streak,
                "missing_quote_ticks": 0,
            }
            opened_symbols.append(opp.base_symbol)
            trade_rows.append(
                {
                    "ts_ms": ts_ms,
                    "action": "OPEN",
                    "exit_reason": "",
                    "base_symbol": opp.base_symbol,
                    "short_symbol": opp.short_symbol,
                    "long_symbol": opp.long_symbol,
                    "any_only_isolated": bool(opp.any_only_isolated),
                    "both_growth_mode": bool(opp.both_growth_mode),
                    "leg_notional_usd": leg_notional,
                    "margin_locked_usd": pair_margin,
                    "spread_apr_pct": opp.spread_apr_pct,
                    "oracle_div_bps": opp.oracle_divergence_bps,
                    "l2_slip_bps": round(l2_slip, 2),
                    "cost_usd": round(entry_cost_usd, 4),
                    "hours_held": 0.0,
                    "final_net_pnl_usd": round(-entry_cost_usd, 4),
                }
            )

        # 5. Update portfolio equity, exit-cost liquidation NAV, split-margin utilization, and persist atomically
        for p in positions.values():
            s_q = float(p.get("short_qty") or 0.0)
            l_q = float(p.get("long_qty") or 0.0)
            s_px = float(p.get("last_short_px") or 0.0)
            l_px = float(p.get("last_long_px") or 0.0)
            cur_leg_ntl = (
                0.5 * (s_q * s_px + l_q * l_px)
                if (s_q > 0 and l_q > 0 and s_px > 0 and l_px > 0)
                else float(p.get("leg_notional_usd", 0.0))
            )
            est_exit_bps = float(p.get("entry_fee_bps", 1.8)) + float(p.get("entry_l2_slip_bps", 2.0))
            est_exit_usd = round(cur_leg_ntl * est_exit_bps * 1e-4, 4)
            p["est_exit_cost_usd"] = est_exit_usd
            p["liquidation_net_pnl_usd"] = round(float(p.get("net_pnl_usd", 0.0)) - est_exit_usd, 4)

        tick_net_pnl = tick_funding_usd + tick_basis_mtm_usd - tick_costs_usd
        new_equity = round(float(state["equity_usd"]) + tick_net_pnl, 4)
        est_exit_costs_usd = round(sum(float(p.get("est_exit_cost_usd", 0.0)) for p in positions.values()), 4)
        liquidation_equity_usd = round(new_equity - est_exit_costs_usd, 4)
        start_cap = float(state["starting_capital_usd"])
        margin_locked = round(sum(float(p.get("margin_locked_usd", 0.0)) for p in positions.values()), 2)
        margin_util = round((margin_locked / new_equity) * 100.0, 2) if new_equity > 0 else 0.0
        net_delta_total = round(sum(float(p.get("net_delta_usd", 0.0)) for p in positions.values()), 2)

        gross_notional_usd = round(sum(_pair_gross_notional_usd(p) for p in positions.values()), 2)
        gross_leverage = round(gross_notional_usd / max(new_equity, 1.0), 3)

        state["equity_usd"] = new_equity
        state["est_exit_costs_usd"] = est_exit_costs_usd
        state["liquidation_equity_usd"] = liquidation_equity_usd
        state["return_pct"] = round(((new_equity / start_cap) - 1.0) * 100.0, 4) if start_cap > 0 else 0.0
        state["gross_notional_usd"] = gross_notional_usd
        state["gross_leverage"] = gross_leverage
        state["margin_locked_usd"] = margin_locked
        state["margin_utilization_pct"] = margin_util
        state["net_delta_usd"] = net_delta_total
        state["cum_funding_usd"] = round(float(state["cum_funding_usd"]) + tick_funding_usd, 4)
        state["cum_basis_mtm_usd"] = round(float(state["cum_basis_mtm_usd"]) + tick_basis_mtm_usd, 4)
        state["cum_costs_usd"] = round(float(state["cum_costs_usd"]) + tick_costs_usd, 4)
        state["last_tick_ms"] = prev_ms if nav_guard_triggered else ts_ms
        state["last_clamped_ms"] = ts_ms if nav_guard_triggered else 0
        state["nav_guard_triggered"] = nav_guard_triggered
        state["nav_guard_reason"] = nav_guard_reason
        state["nav_guard_pending_ms"] = nav_guard_pending
        state["nav_guard_pending_ret_pct"] = nav_guard_pending_ret
        state["observed_at_utc"] = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        state["ny_session"] = ny_session
        state["positions"] = positions

        _write_json_atomic(state_path, state)

        tick_row = {
            "ts_ms": ts_ms,
            "ny_session": ny_session,
            "dt_hours": round(dt_hours, 3),
            "equity_usd": new_equity,
            "liquidation_equity_usd": liquidation_equity_usd,
            "active_pairs": len(positions),
            "gross_notional_usd": gross_notional_usd,
            "gross_leverage": gross_leverage,
            "margin_locked_usd": margin_locked,
            "margin_utilization_pct": margin_util,
            "net_delta_usd": net_delta_total,
            "tick_funding_usd": round(tick_funding_usd, 4),
            "tick_basis_mtm_usd": round(tick_basis_mtm_usd, 4),
            "tick_costs_usd": round(tick_costs_usd, 4),
            "tick_net_pnl_usd": round(tick_net_pnl, 4),
            "opened_count": len(opened_symbols),
            "closed_count": sum(1 for t in trade_rows if t["action"] == "CLOSE"),
            "nav_guard_triggered": nav_guard_triggered,
        }
        _append_parquet(state_dir / "ticks.parquet", [tick_row])
        _append_parquet(state_dir / "trades.parquet", trade_rows)

        return {
            "status": "ok",
            "tick": tick_row,
            "state": state,
            "trades": trade_rows,
            "gated_stats": gated_stats,
            "markets_scanned": len(live_markets),
            "opps_evaluated": len(opps),
        }
    finally:
        if own_client and http_client is not None:
            http_client.close()


# --------------------------------------------------------------------------- Reporting


def format_hip3_paper_markdown(result: dict[str, Any]) -> str:
    st = result["state"]
    tk = result["tick"]
    gs = result.get("gated_stats") or {}
    ts = st.get("observed_at_utc") or datetime.fromtimestamp(tk["ts_ms"] / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M UTC"
    )
    lines = [
        f"# HIP-3 Cross-Builder Split-Margin Paper Tracker ({ts})",
        "",
        f"- **Баланс Paper:** **${st['equity_usd']:,.2f}** (`{st['return_pct']:+.3f}%` от ${st['starting_capital_usd']:,.0f}) · сессия NY `{st.get('ny_session', '')}`",
        f"- **Split-маржа (независимые DEX):** залочено `${st['margin_locked_usd']:,.0f}` (`{st['margin_utilization_pct']:.1f}%`) · активных пар: **{tk['active_pairs']}** (gross `${tk['gross_notional_usd']:,.0f}`)",
        f"- **Накопленный PnL:** Funding `{st['cum_funding_usd']:+.2f}$` + Basis MTM `{st['cum_basis_mtm_usd']:+.2f}$` − L2/Fees `{st['cum_costs_usd']:.2f}$`",
        f"- **Фильтры на тике:** oracle `{gs.get('skipped_oracle', 0)}`, streak `{gs.get('skipped_streak', 0)}`, L2 slip `{gs.get('skipped_l2_slip', 0)}`, payback `{gs.get('skipped_payback', 0)}` · закрыто пар `{st['closed_pairs_count']}`, окупивших вход `{st['payback_achieved_count']}`",
        "",
        "## Активные HIP-3 пары (Zero-Bridge Split-Margin)",
        "",
        "| Тикер | Ноги | Режим | Номинал/ногу | Live APR | Oracle Div | L2 Slip | Часов | Funding | Basis MTM | Net PnL |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in sorted(st["positions"].values(), key=lambda x: -float(x["live_spread_apr_pct"])):
        mode_tag = ("⚡GM" if p.get("both_growth_mode") else "Std") + (
            " · Iso" if p.get("any_only_isolated") else " · Cross"
        )
        lines.append(
            f"| **{p['base_symbol']}** | `S {p['short_symbol']} / L {p['long_symbol']}` | {mode_tag} | "
            f"${p['leg_notional_usd']:,.0f} | **{p['live_spread_apr_pct']:+.1f}%** | "
            f"{p['live_oracle_div_bps']:.1f} б.п. | {p['entry_l2_slip_bps']:.1f} б.п. | {p['hours_held']:.1f}ч | "
            f"{p['cum_funding_usd']:+.2f}$ | {p['cum_basis_mtm_usd']:+.2f}$ | **{p['net_pnl_usd']:+.2f}$** |"
        )
    if not st["positions"]:
        lines.append("| — | Нет активных пар (ожидание clean_streak ≥ 2) | — | — | — | — | — | — | — | — | — |")
    return "\n".join(lines) + "\n"


def format_hip3_paper_telegram_rich_html(result: dict[str, Any]) -> str:
    st = result["state"]
    tk = result["tick"]
    dt = datetime.fromtimestamp(tk["ts_ms"] / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    ret_pct = float(st.get("return_pct", 0.0))
    dot = "🟢" if ret_pct >= 0 else "🔴"

    parts = [
        f"<h1>🏛️ HIP-3 Split-Margin Paper · {html.escape(date_str)}</h1>",
        (
            f"<p>{dot} <b>Депозит ({html.escape(time_str)}):</b> ${st['equity_usd']:,.2f} ({ret_pct:+.3f}%) · "
            f"<b>Маржа:</b> ${st['margin_locked_usd']:,.0f} ({st['margin_utilization_pct']:.1f}%)<br>"
            f"<b>Пар:</b> {tk['active_pairs']} · Funding {st['cum_funding_usd']:+.2f}$ · "
            f"Basis {st['cum_basis_mtm_usd']:+.2f}$ · L2/Fees −{st['cum_costs_usd']:.2f}$</p>"
        ),
    ]
    if st["positions"]:
        rows = [
            "<tr>"
            '<th align="left">Тикер / Ноги</th>'
            '<th align="left">Режим</th>'
            '<th align="right">Live APR</th>'
            '<th align="right">Oracle</th>'
            '<th align="right">Net PnL</th>'
            "</tr>"
        ]
        for p in sorted(st["positions"].values(), key=lambda x: -float(x["live_spread_apr_pct"])):
            mode = ("⚡GM/" if p.get("both_growth_mode") else "Std/") + (
                "Iso" if p.get("any_only_isolated") else "Cross"
            )
            p_dot = "🟢" if float(p["net_pnl_usd"]) >= 0 else "🔴"
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(str(p["base_symbol"]))}</b> ({html.escape(str(p["short_dex"]))}/{html.escape(str(p["long_dex"]))})</td>'
                f'<td align="left">{html.escape(mode)}</td>'
                f'<td align="right"><b>{p["live_spread_apr_pct"]:+.1f}%</b></td>'
                f'<td align="right">{p["live_oracle_div_bps"]:.1f} б.п.</td>'
                f'<td align="right">{p_dot} {p["net_pnl_usd"]:+.2f}$</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    parts.append("<footer>Read-only HIP-3 L2 Paper: Zero-Bridge Growth Mode · Split-Margin · Oracle Div ≤ 15 б.п.</footer>")
    return "".join(parts)
