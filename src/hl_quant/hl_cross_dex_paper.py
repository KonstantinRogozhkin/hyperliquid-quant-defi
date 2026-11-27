"""Read-only Multi-Venue Cross-DEX Paper Tracker v2: Hyperliquid × Lighter × Extended.

Zero orders placed. Tracks real-time funding spread persistence, L2 VWAP execution cost,
dual-leg basis MTM, and cumulative fee-payback across Hyperliquid, Lighter, and Extended.

v2 Guardrails (derived from live Tick #2 diagnostics):
- **Persistence Gate** (`min_persistence_ticks=2`): avoids 1-tick funding spikes (like `NEAR`
  collapsing +25% -> +0.4% in 1.7h) by requiring consecutive qualifying ticks before opening.
- **Tight L2 & Basis Entry Gate** (`max_l2_slip_bps=5.0`, `min_exec_basis_bps=-3.0`): rejects
  thin books (`XMR` 11.6 bps slip / -$14.4 basis swing) and entries already underwater on L2.
- **H1 Basis + Carry Combo** (`combo_min_exec_basis_bps=+8.0`, `combo_min_apr_pct=8.0`):
  opens immediately when favorable L2 basis already pays for round-trip fees + slippage at 0h.
- **Multi-Venue** (`Lighter` + `Extended`): picks the best risk-adjusted venue per coin.
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
from hl_quant.persistence_clock import streak_count, update_streak_map


@dataclass(frozen=True, slots=True)
class CrossDexPaperConfig:
    starting_capital_usd: float = 50_000.0
    max_slots: int = 5
    slot_notional_usd: float = 5_000.0  # per leg -> 2 legs = $10k gross per pair (<= 1.0x total)
    min_day_vol_usd: float = 500_000.0
    entry_apr_pct: float = 15.0  # open persistent carry pair when spread APR >= 15%
    exit_apr_pct: float = 5.0  # hysteresis exit when directional APR decays below 5%
    max_l2_slip_bps: float = 5.0  # tight L2 depth cap (blocks thin XMR/GRAM traps)
    min_exec_basis_bps: float = -3.0  # refuse entries heavily underwater on live L2 basis
    min_persistence_ticks: int = 2  # consecutive ticks >= entry_apr_pct required for pure carry
    max_breakeven_hours: float = 96.0  # reject pairs taking > 4 days to recover entry cost
    hl_fee_bps: float = 4.5  # HL Tier-0 taker fee (cross-DEX L2 entries cross the book)
    lighter_fee_bps: float = 0.0  # Lighter standard 0% fee
    extended_fee_bps: float = 2.0  # Extended conservative maker/taker fee
    bridge_drag_bps: float = 1.5  # amortized cross-DEX margin rebalance cost per entry
    max_missing_ticks: int = 3  # tolerate transient missing quotes before assuming delisting
    enable_extended: bool = True  # scan Extended alongside Lighter
    enable_basis_carry_combo: bool = True  # H1: immediate entry on favorable basis + positive carry
    combo_min_exec_basis_bps: float = 8.0
    combo_min_apr_pct: float = 12.0  # avoid low-carry majors that churn fees if structural basis never converges
    combo_max_hold_hours: float = 48.0  # time-stop if basis never converges once carry decays below exit_apr_pct
    min_streak_spacing_hours: float = 3.5  # streaks advance at most once per ~4h cron window
    # Reject entries where the L2 book mid basis and the mark basis disagree by more than this:
    # the ledger marks on mark_px, so a book-only edge (stale/async snapshot) never realizes.
    max_book_mark_divergence_bps: float = 6.0

    def other_fee_bps(self, dex: str) -> float:
        return self.lighter_fee_bps if dex == "lighter" else self.extended_fee_bps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _load_or_init_state(state_path: Path, cfg: CrossDexPaperConfig) -> dict[str, Any]:
    if state_path.is_file():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st.setdefault("spread_streaks", {})
        return st
    return {
        "starting_capital_usd": cfg.starting_capital_usd,
        "equity_usd": cfg.starting_capital_usd,
        "cum_funding_usd": 0.0,
        "cum_basis_mtm_usd": 0.0,
        "cum_costs_usd": 0.0,
        "last_tick_ms": 0,
        "positions": {},  # coin -> position dict
        "spread_streaks": {},  # "COIN@dex:dir" -> consecutive qualifying ticks
        "closed_pairs_count": 0,
        "payback_achieved_count": 0,
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


def run_cross_dex_paper_tick(
    client: httpx.Client,
    *,
    state_dir: Path,
    cfg: CrossDexPaperConfig = CrossDexPaperConfig(),
    now_ms: int | None = None,
    hl_quotes: dict[str, cds.DexMarketQuote] | None = None,
    lighter_quotes: dict[str, cds.DexMarketQuote] | None = None,
    extended_quotes: dict[str, cds.DexMarketQuote] | None = None,
    book_fetcher: Any = None,
) -> dict[str, Any]:
    """Run one idempotent Hyperliquid × Lighter × Extended L2 paper tick and update the ledger."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    state = _load_or_init_state(state_path, cfg)

    hl_q = hl_quotes if hl_quotes is not None else cds.fetch_hyperliquid_quotes(client)
    lt_q = lighter_quotes if lighter_quotes is not None else cds.fetch_lighter_quotes(client)
    if extended_quotes is not None:
        ext_q = extended_quotes
    elif cfg.enable_extended and lighter_quotes is None:
        try:
            ext_q = cds.fetch_extended_quotes(client)
        except Exception:
            ext_q = {}
    else:
        ext_q = {}

    venue_quotes: dict[str, dict[str, cds.DexMarketQuote]] = {"lighter": lt_q}
    if ext_q:
        venue_quotes["extended"] = ext_q

    get_book = book_fetcher or (lambda dex, sym: cds.fetch_dex_l2_book(client, dex, sym))

    prev_ms = int(state.get("last_tick_ms", 0))
    dt_hours = max(0.0, min(24.0, (ts_ms - prev_ms) / 3_600_000.0)) if prev_ms > 0 else 0.0

    positions: dict[str, dict[str, Any]] = state.get("positions") or {}
    tick_funding_usd = 0.0
    tick_basis_mtm_usd = 0.0
    tick_costs_usd = 0.0
    trade_rows: list[dict[str, Any]] = []

    # 1. Mark-to-market & accrue funding on all currently open dual-leg pairs
    for coin, pos in list(positions.items()):
        other_dex = str(pos.get("other_dex") or "lighter")
        h = hl_q.get(coin)
        oth = (venue_quotes.get(other_dex) or {}).get(coin)
        if not (h and oth):
            continue
        hl_sgn = float(pos["hl_sign"])  # -1.0 = Short HL / Long Other; +1.0 = Long HL / Short Other
        oth_sgn = -hl_sgn
        notional = float(pos["leg_notional_usd"])

        prev_hl = float(pos["last_hl_px"])
        prev_oth = float(pos.get("last_other_px", pos.get("last_lt_px", oth.mark_px)))
        hl_ret = (h.mark_px - prev_hl) / prev_hl if prev_hl > 0 else 0.0
        oth_ret = (oth.mark_px - prev_oth) / prev_oth if prev_oth > 0 else 0.0
        basis_pnl = notional * (hl_sgn * hl_ret + oth_sgn * oth_ret)

        hl_fund_pnl = notional * (-hl_sgn) * h.funding_rate_h * dt_hours
        oth_fund_pnl = notional * (-oth_sgn) * oth.funding_rate_h * dt_hours
        fund_pnl = hl_fund_pnl + oth_fund_pnl

        pos["other_dex"] = other_dex
        pos["last_hl_px"] = h.mark_px
        pos["last_lt_px"] = oth.mark_px
        pos["last_other_px"] = oth.mark_px
        pos["hours_held"] = round(float(pos.get("hours_held", 0.0)) + dt_hours, 2)
        pos["cum_funding_usd"] = round(float(pos.get("cum_funding_usd", 0.0)) + fund_pnl, 4)
        pos["cum_basis_mtm_usd"] = round(float(pos.get("cum_basis_mtm_usd", 0.0)) + basis_pnl, 4)
        pos["live_spread_apr_pct"] = round((-hl_sgn) * (h.funding_apr_pct - oth.funding_apr_pct), 2)
        live_basis_bps = (-hl_sgn) * (h.mark_px - oth.mark_px) / oth.mark_px * 1e4 if oth.mark_px > 0 else 0.0
        pos["live_mark_basis_bps"] = round(live_basis_bps, 2)
        pos["net_pnl_usd"] = round(
            pos["cum_funding_usd"] + pos["cum_basis_mtm_usd"] - float(pos.get("cum_cost_usd", 0.0)),
            4,
        )
        pos["payback_achieved"] = bool(pos["cum_funding_usd"] >= float(pos.get("entry_cost_usd", 0.0)))

        tick_funding_usd += fund_pnl
        tick_basis_mtm_usd += basis_pnl

    # 2. Exit pairs whose spread decayed below exit_apr_pct (or H1 combo once basis converged & carry low)
    for coin in list(positions.keys()):
        pos = positions[coin]
        other_dex = str(pos.get("other_dex") or "lighter")
        venue_online = bool(hl_q) and bool(venue_quotes.get(other_dex))
        h = hl_q.get(coin)
        oth = (venue_quotes.get(other_dex) or {}).get(coin)
        if not h or not oth:
            if not venue_online:
                # Venue feed outage: hold position untouched rather than force-closing on synthetic slippage.
                continue
            missing_n = int(pos.get("missing_quote_ticks", 0)) + 1
            pos["missing_quote_ticks"] = missing_n
            if missing_n < cfg.max_missing_ticks:
                continue
            should_exit = True
        else:
            pos["missing_quote_ticks"] = 0
            live_dir_apr = float(pos.get("live_spread_apr_pct", 0.0))
            live_basis = float(pos.get("live_mark_basis_bps", 0.0))
            hours_held = float(pos.get("hours_held", 0.0))
            is_combo = pos.get("entry_type") == "basis_carry_combo"
            if is_combo:
                # Never hold a combo position bleeding negative funding, and cap waiting for
                # persistent cross-DEX mark-basis convergence once carry decays below exit_apr_pct.
                should_exit = (live_dir_apr < 0.0) or (
                    live_dir_apr < cfg.exit_apr_pct
                    and (live_basis <= 1.0 or hours_held >= cfg.combo_max_hold_hours)
                )
            else:
                should_exit = live_dir_apr < cfg.exit_apr_pct
        live_dir_apr = float(pos.get("live_spread_apr_pct", 0.0))
        if should_exit:
            notional = float(pos["leg_notional_usd"])
            short_hl_exit = float(pos["hl_sign"]) > 0
            l2_slip = 2.0
            if h and oth:
                hb, ha = get_book("hyperliquid", h.raw_symbol)
                ob, oa = get_book(other_dex, oth.raw_symbol)
                _, sl, _ = cds.compute_executable_cross_dex_entry_bps(
                    hb, ha, ob, oa, short_hl=short_hl_exit, clip_usd=notional
                )
                if sl is not None:
                    l2_slip = sl
            exit_cost_bps = cfg.hl_fee_bps + cfg.other_fee_bps(other_dex) + l2_slip
            exit_cost_usd = notional * exit_cost_bps * 1e-4
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
                    "coin": coin,
                    "other_dex": other_dex,
                    "entry_type": str(pos.get("entry_type", "carry_persistent")),
                    "direction": pos["direction"],
                    "leg_notional_usd": notional,
                    "spread_apr_pct": live_dir_apr,
                    "l2_slip_bps": round(l2_slip, 2),
                    "cost_usd": round(exit_cost_usd, 4),
                    "hours_held": pos["hours_held"],
                    "final_net_pnl_usd": final_net,
                }
            )
            positions.pop(coin, None)

    # 3. Scan multi-venue opportunities (Lighter + Extended) and update persistence streaks
    cand_frames: list[pd.DataFrame] = []
    for dex_name, v_q in venue_quotes.items():
        if not v_q:
            continue
        df_v = cds.scan_cross_dex_opportunities(
            hl_q,
            v_q,
            min_day_vol_usd=cfg.min_day_vol_usd,
            round_trip_fee_bps=2.0 * (cfg.hl_fee_bps + cfg.other_fee_bps(dex_name)),
            bridge_rebalance_drag_bps=cfg.bridge_drag_bps,
        )
        if not df_v.empty:
            cand_frames.append(df_v)

    all_opps = (
        pd.concat(cand_frames, ignore_index=True).sort_values("spread_apr_pct", ascending=False)
        if cand_frames
        else pd.DataFrame()
    )

    qualifying_keys: list[str] = []
    if not all_opps.empty:
        for r in all_opps.itertuples(index=False):
            if float(r.spread_apr_pct) >= cfg.entry_apr_pct:
                qualifying_keys.append(f"{r.coin}@{r.other_dex}:{r.direction}")
    new_streaks = update_streak_map(
        state.get("spread_streaks") or {},
        qualifying_keys,
        now_ms=ts_ms,
        min_spacing_hours=cfg.min_streak_spacing_hours,
    )
    state["spread_streaks"] = new_streaks

    # 4. Evaluate candidates with Persistence Gate, Tight-L2 + Basis Gate, and H1 Basis+Carry Combo
    opened_coins: list[str] = []
    gated_stats = {
        "skipped_persistence": 0,
        "skipped_l2_slip": 0,
        "skipped_basis": 0,
        "skipped_breakeven": 0,
        "skipped_basis_source": 0,
    }
    if not all_opps.empty and len(positions) < cfg.max_slots:
        # Check top 30 candidates by spread (and any with big favorable mark basis for H1 combo)
        min_scan_apr = min(cfg.entry_apr_pct, cfg.combo_min_apr_pct) if cfg.enable_basis_carry_combo else cfg.entry_apr_pct
        pool = all_opps[all_opps["spread_apr_pct"] >= min_scan_apr].head(30)
        for r in pool.itertuples(index=False):
            if len(positions) >= cfg.max_slots:
                break
            coin = str(r.coin)
            other_dex = str(r.other_dex)
            direction = str(r.direction)
            spread_apr = float(r.spread_apr_pct)
            if coin in positions:
                continue

            h = hl_q[coin]
            oth = venue_quotes[other_dex][coin]
            short_hl = direction.startswith("Short HL")
            streak_key = f"{coin}@{other_dex}:{direction}"
            streak = streak_count(new_streaks.get(streak_key))

            # Quick mark-basis pre-filter before hitting L2 endpoints if persistence not yet met
            # Same directional denominator as cds.favorable_basis_bps / compute_book_mid_basis_bps
            # (other venue when short HL, HL when long HL) so the two bases are comparable.
            mark_basis_dir = (h.mark_px - oth.mark_px) / oth.mark_px * 1e4 if short_hl else (oth.mark_px - h.mark_px) / h.mark_px * 1e4
            could_be_combo = (
                cfg.enable_basis_carry_combo
                and spread_apr >= cfg.combo_min_apr_pct
                and mark_basis_dir >= (cfg.combo_min_exec_basis_bps - 2.0)
            )
            passes_persist = spread_apr >= cfg.entry_apr_pct and streak >= cfg.min_persistence_ticks
            if not (passes_persist or could_be_combo):
                if spread_apr >= cfg.entry_apr_pct:
                    gated_stats["skipped_persistence"] += 1
                continue

            hb, ha = get_book("hyperliquid", h.raw_symbol)
            ob, oa = get_book(other_dex, oth.raw_symbol)
            exec_basis, l2_slip, full_ok = cds.compute_executable_cross_dex_entry_bps(
                hb, ha, ob, oa, short_hl=short_hl, clip_usd=cfg.slot_notional_usd
            )
            if not full_ok or l2_slip is None or exec_basis is None or l2_slip > cfg.max_l2_slip_bps:
                gated_stats["skipped_l2_slip"] += 1
                continue

            book_mid_basis = cds.compute_book_mid_basis_bps(hb, ha, ob, oa, short_hl=short_hl)
            if book_mid_basis is None or abs(book_mid_basis - mark_basis_dir) > cfg.max_book_mark_divergence_bps:
                gated_stats["skipped_basis_source"] += 1
                continue
            credited_basis = cds.credited_entry_basis_bps(exec_basis, mark_basis_dir)

            oth_fee = cfg.other_fee_bps(other_dex)
            venue_rt_fee_bps = 2.0 * (cfg.hl_fee_bps + oth_fee) + cfg.bridge_drag_bps
            combo_basis_floor = max(cfg.combo_min_exec_basis_bps, venue_rt_fee_bps)
            is_h1_combo = (
                cfg.enable_basis_carry_combo
                and spread_apr >= cfg.combo_min_apr_pct
                and credited_basis >= combo_basis_floor
            )
            if not is_h1_combo:
                if not passes_persist:
                    gated_stats["skipped_persistence"] += 1
                    continue
                if credited_basis < cfg.min_exec_basis_bps:
                    gated_stats["skipped_basis"] += 1
                    continue

            entry_cost_bps = cfg.hl_fee_bps + oth_fee + cfg.bridge_drag_bps + l2_slip
            # Credit only conservative 50% convergence for positive cross-DEX basis (persistent
            # venue oracle/mark offsets do not guarantee 100% reversion at exit); charge full adverse basis.
            basis_credit_bps = (0.5 * credited_basis) if credited_basis > 0 else credited_basis
            est_rt_cost_bps = max(0.0, venue_rt_fee_bps + l2_slip - basis_credit_bps)
            hourly_bps = spread_apr / (cds.HOURS_PER_YEAR * 100.0) * 1e4
            be_hours = (est_rt_cost_bps / hourly_bps) if hourly_bps > 1e-9 else float("inf")
            if be_hours > cfg.max_breakeven_hours:
                gated_stats["skipped_breakeven"] += 1
                continue

            entry_type = "basis_carry_combo" if is_h1_combo else "carry_persistent"
            entry_cost_usd = cfg.slot_notional_usd * entry_cost_bps * 1e-4
            tick_costs_usd += entry_cost_usd
            positions[coin] = {
                "coin": coin,
                "other_dex": other_dex,
                "entry_type": entry_type,
                "direction": direction,
                "hl_sign": -1.0 if short_hl else 1.0,
                "leg_notional_usd": cfg.slot_notional_usd,
                "opened_ms": ts_ms,
                "hours_held": 0.0,
                "entry_hl_px": h.mark_px,
                "entry_lt_px": oth.mark_px,
                "entry_other_px": oth.mark_px,
                "last_hl_px": h.mark_px,
                "last_lt_px": oth.mark_px,
                "last_other_px": oth.mark_px,
                "entry_spread_apr_pct": spread_apr,
                "live_spread_apr_pct": spread_apr,
                "entry_l2_slip_bps": round(l2_slip, 2),
                "entry_exec_basis_bps": round(exec_basis, 2),
                "entry_book_mid_basis_bps": round(book_mid_basis, 2),
                "entry_credited_basis_bps": round(credited_basis, 2),
                "live_mark_basis_bps": round(mark_basis_dir, 2),
                "entry_breakeven_hours": round(be_hours, 1),
                "entry_cost_usd": round(entry_cost_usd, 4),
                "cum_cost_usd": round(entry_cost_usd, 4),
                "cum_funding_usd": 0.0,
                "cum_basis_mtm_usd": 0.0,
                "net_pnl_usd": round(-entry_cost_usd, 4),
                "payback_achieved": False,
                "persistence_streak": streak,
            }
            opened_coins.append(f"{coin}@{other_dex}")
            trade_rows.append(
                {
                    "ts_ms": ts_ms,
                    "action": "OPEN",
                    "coin": coin,
                    "other_dex": other_dex,
                    "entry_type": entry_type,
                    "direction": direction,
                    "leg_notional_usd": cfg.slot_notional_usd,
                    "spread_apr_pct": spread_apr,
                    "l2_slip_bps": round(l2_slip, 2),
                    "cost_usd": round(entry_cost_usd, 4),
                    "hours_held": 0.0,
                    "final_net_pnl_usd": round(-entry_cost_usd, 4),
                }
            )

    # 5. Update portfolio equity & persist atomically
    tick_net_pnl = tick_funding_usd + tick_basis_mtm_usd - tick_costs_usd
    state["equity_usd"] = round(float(state["equity_usd"]) + tick_net_pnl, 4)
    state["cum_funding_usd"] = round(float(state["cum_funding_usd"]) + tick_funding_usd, 4)
    state["cum_basis_mtm_usd"] = round(float(state["cum_basis_mtm_usd"]) + tick_basis_mtm_usd, 4)
    state["cum_costs_usd"] = round(float(state["cum_costs_usd"]) + tick_costs_usd, 4)
    state["last_tick_ms"] = ts_ms
    state["positions"] = positions

    tmp_st = state_path.with_suffix(".tmp.json")
    tmp_st.write_text(json.dumps(state, indent=2), encoding="utf-8")
    tmp_st.replace(state_path)

    tick_row = {
        "ts_ms": ts_ms,
        "dt_hours": round(dt_hours, 3),
        "equity_usd": state["equity_usd"],
        "active_pairs": len(positions),
        "gross_notional_usd": round(sum(float(p["leg_notional_usd"]) * 2.0 for p in positions.values()), 2),
        "tick_funding_usd": round(tick_funding_usd, 4),
        "tick_basis_mtm_usd": round(tick_basis_mtm_usd, 4),
        "tick_costs_usd": round(tick_costs_usd, 4),
        "tick_net_pnl_usd": round(tick_net_pnl, 4),
        "opened_count": len(opened_coins),
        "closed_count": sum(1 for t in trade_rows if t["action"] == "CLOSE"),
    }
    _append_parquet(state_dir / "ticks.parquet", [tick_row])
    _append_parquet(state_dir / "trades.parquet", trade_rows)

    return {
        "status": "ok",
        "tick": tick_row,
        "state": state,
        "trades": trade_rows,
        "gated_stats": gated_stats,
        "markets_scanned": {"hyperliquid": len(hl_q), "lighter": len(lt_q), "extended": len(ext_q)},
    }


# --------------------------------------------------------------------------- Reporting


def format_cross_dex_paper_markdown(result: dict[str, Any]) -> str:
    st = result["state"]
    tk = result["tick"]
    gs = result.get("gated_stats") or {}
    ts = datetime.fromtimestamp(tk["ts_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    start_cap = float(st["starting_capital_usd"])
    ret_pct = (float(st["equity_usd"]) / start_cap - 1.0) * 100.0 if start_cap > 0 else 0.0
    lines = [
        f"# Cross-DEX Paper Tracker v2 · Hyperliquid × Lighter × Extended ({ts})",
        "",
        f"- **Баланс Paper:** **${st['equity_usd']:,.2f}** (`{ret_pct:+.3f}%` от ${start_cap:,.0f}) · активных пар: **{tk['active_pairs']}** (gross `${tk['gross_notional_usd']:,.0f}`)",
        f"- **Накопленный PnL:** Funding `{st['cum_funding_usd']:+.2f}$` + Basis MTM `{st['cum_basis_mtm_usd']:+.2f}$` − L2/Fees `{st['cum_costs_usd']:.2f}$`",
        f"- **Фильтры v2 на тике:** отсеяно персистентностью `{gs.get('skipped_persistence', 0)}`, L2-слипом `>5 б.п.` `{gs.get('skipped_l2_slip', 0)}`, базисом `{gs.get('skipped_basis', 0)}` · активных серий спреда: `{len(st.get('spread_streaks') or {})}`",
        f"- **Статистика персистентности:** закрыто пар `{st['closed_pairs_count']}`, окупивших вход: `{st['payback_achieved_count']}`",
        "",
        "## Активные дельта-нейтральные пары (HL × Lighter × Extended)",
        "",
        "| Монета | Площадка / Тип | Направление | Live APR | L2 Slip | Exec Basis | Окуп. | Часов | Funding | Basis MTM | Net PnL |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for p in sorted(st["positions"].values(), key=lambda x: -float(x["live_spread_apr_pct"])):
        dex_tag = str(p.get("other_dex") or "lighter")
        mode_tag = "H1-Combo" if p.get("entry_type") == "basis_carry_combo" else "Carry"
        eb = p.get("entry_exec_basis_bps")
        eb_txt = f"{float(eb):+.1f} б.п." if eb is not None else "—"
        lines.append(
            f"| **{p['coin']}** | `{dex_tag}` · {mode_tag} | {p['direction']} | **{p['live_spread_apr_pct']:+.1f}%** | "
            f"{p['entry_l2_slip_bps']:.1f} б.п. | {eb_txt} | {p['entry_breakeven_hours']:.0f}ч | {p['hours_held']:.1f}ч | "
            f"{p['cum_funding_usd']:+.2f}$ | {p['cum_basis_mtm_usd']:+.2f}$ | **{p['net_pnl_usd']:+.2f}$** |"
        )
    return "\n".join(lines) + "\n"


def format_cross_dex_paper_telegram_rich_html(result: dict[str, Any]) -> str:
    st = result["state"]
    tk = result["tick"]
    dt = datetime.fromtimestamp(tk["ts_ms"] / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    start_cap = float(st["starting_capital_usd"])
    ret_pct = (float(st["equity_usd"]) / start_cap - 1.0) * 100.0 if start_cap > 0 else 0.0
    dot = "🟢" if ret_pct >= 0 else "🔴"

    parts = [
        f"<h1>🔀 Cross-DEX Paper v2 (HL × Lighter × Extended) · {html.escape(date_str)}</h1>",
        (
            f"<p>{dot} <b>Депозит Paper ({html.escape(time_str)}):</b> ${st['equity_usd']:,.2f} ({ret_pct:+.3f}%)<br>"
            f"<b>Активных пар:</b> {tk['active_pairs']} · Funding {st['cum_funding_usd']:+.2f}$ · "
            f"Basis {st['cum_basis_mtm_usd']:+.2f}$ · L2/Fees −{st['cum_costs_usd']:.2f}$</p>"
        ),
    ]
    if st["positions"]:
        rows = [
            "<tr>"
            '<th align="left">Монета</th>'
            '<th align="left">Пара / Тип</th>'
            '<th align="right">Спред APR</th>'
            '<th align="right">Slip L2</th>'
            '<th align="right">Net PnL</th>'
            "</tr>"
        ]
        for p in sorted(st["positions"].values(), key=lambda x: -float(x["live_spread_apr_pct"])):
            dex_s = "EXT" if p.get("other_dex") == "extended" else "LT"
            short_dir = f"Шорт HL / Лонг {dex_s}" if float(p["hl_sign"]) < 0 else f"Лонг HL / Шорт {dex_s}"
            if p.get("entry_type") == "basis_carry_combo":
                short_dir += " (H1)"
            p_dot = "🟢" if float(p["net_pnl_usd"]) >= 0 else "🔴"
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(p["coin"])}</b></td>'
                f'<td align="left">{html.escape(short_dir)}</td>'
                f'<td align="right"><b>{p["live_spread_apr_pct"]:+.1f}%</b></td>'
                f'<td align="right">{p["entry_l2_slip_bps"]:.1f} б.п.</td>'
                f'<td align="right">{p_dot} {p["net_pnl_usd"]:+.2f}$</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    parts.append("<footer>Read-only L2 v2: Persistence Gate + Slip ≤ 5 б.п. + H1 Basis+Carry (HL × Lighter × Extended)</footer>")
    return "".join(parts)
