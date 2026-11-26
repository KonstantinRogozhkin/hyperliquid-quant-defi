"""Post-trade PnL attribution, maker fill-rate, markout & liquidation-risk audit for Hyperliquid.

Read-only analytics over ``userFills``, ``userFunding``, ``clearinghouseState``,
``spotClearinghouseState``, ``openOrders``, live ``l2Book`` mids, and the local
``hl_testnet_audit.jsonl`` order log. Never places or cancels orders.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
from typing import Any

import httpx
import pandas as pd

TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"


@dataclass(frozen=True, slots=True)
class CoinAttribution:
    coin: str
    position_sz: float
    position_usd: float
    entry_px: float
    current_mid: float
    leverage_mode: str
    liq_px: float | None
    liq_distance_pct: float | None
    fills_count: int
    fill_volume_usd: float
    maker_share_pct: float
    realized_pnl_usd: float
    unrealized_pnl_usd: float
    funding_pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    vw_markout_bps: float | None
    adl_rank_score: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class PostTradeAttribution:
    account_address: str
    observed_at_ms: int
    equity_usd: float
    perps_equity_usd: float
    spot_usdc_usd: float
    open_positions_count: int
    open_orders_count: int
    total_fills: int
    total_volume_usd: float
    maker_volume_share_pct: float
    effective_fee_bps: float
    audit_orders_placed: int
    audit_orders_filled: int
    audit_orders_resting: int
    real_alo_fill_rate_pct: float | None
    realized_price_pnl_usd: float
    unrealized_price_pnl_usd: float
    funding_pnl_usd: float
    fees_usd: float
    net_attributed_pnl_usd: float
    vw_markout_bps: float | None
    min_liq_distance_pct: float | None
    liq_warnings: list[str]
    per_coin: list[CoinAttribution]
    paper_comparison: dict[str, Any] | None
    adl_warnings: list[str] = field(default_factory=list)
    api_reqs_per_usd_vol: float | None = None
    api_quota_health: str = "OK"

    def to_dict(self) -> dict[str, Any]:
        return {
            **{k: v for k, v in asdict(self).items() if k != "per_coin"},
            "per_coin": [c.to_dict() for c in self.per_coin],
        }


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def fetch_post_trade_snapshot(
    client: httpx.Client,
    account_address: str,
    *,
    info_url: str = TESTNET_INFO_URL,
    start_time_ms: int = 0,
) -> dict[str, Any]:
    """Fetch all read-only post-trade endpoints from Hyperliquid for ``account_address``."""
    fills_r = client.post(info_url, json={"type": "userFills", "user": account_address})
    fills_r.raise_for_status()
    fills = fills_r.json()
    funding_r = client.post(info_url, json={"type": "userFunding", "user": account_address, "startTime": start_time_ms})
    funding_r.raise_for_status()
    funding = funding_r.json()
    ch_r = client.post(info_url, json={"type": "clearinghouseState", "user": account_address})
    ch_r.raise_for_status()
    ch = ch_r.json()
    spot_r = client.post(info_url, json={"type": "spotClearinghouseState", "user": account_address})
    spot_r.raise_for_status()
    spot = spot_r.json()
    oo_r = client.post(info_url, json={"type": "openOrders", "user": account_address})
    oo_r.raise_for_status()
    open_orders = oo_r.json()
    mids_r = client.post(info_url, json={"type": "allMids"})
    mids_r.raise_for_status()
    mids_raw = mids_r.json()
    mids = {str(k): _f(v) for k, v in (mids_raw or {}).items()} if isinstance(mids_raw, dict) else {}

    user_abstraction = ""
    try:
        ua_r = client.post(info_url, json={"type": "userAbstraction", "user": account_address})
        if ua_r.status_code == 200 and isinstance(ua_r.json(), str):
            user_abstraction = str(ua_r.json())
    except Exception:
        user_abstraction = ""

    return {
        "account_address": account_address,
        "observed_at_ms": int(datetime.now(tz=timezone.utc).timestamp() * 1000),
        "user_abstraction": user_abstraction,
        "fills": fills if isinstance(fills, list) else [],
        "funding": funding if isinstance(funding, list) else [],
        "clearinghouse": ch if isinstance(ch, dict) else {},
        "spot": spot if isinstance(spot, dict) else {},
        "open_orders": open_orders if isinstance(open_orders, list) else [],
        "all_mids": mids,
    }


def extract_audit_oids(audit_log_path: Path | None) -> set[int]:
    """Extract all resting/filled order IDs submitted by our executor from ``hl_testnet_audit.jsonl``."""
    oids: set[int] = set()
    if audit_log_path is None or not audit_log_path.is_file():
        return oids
    for line in audit_log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        for ord_rec in rec.get("orders") or []:
            resp = ord_rec.get("response") or {}
            statuses = (((resp.get("response") or {}).get("data") or {}).get("statuses") or [])
            for st in statuses:
                if not isinstance(st, dict):
                    continue
                for key in ("resting", "filled"):
                    if key in st and isinstance(st[key], dict) and "oid" in st[key]:
                        oids.add(int(st[key]["oid"]))
    return oids


def load_paper_comparison(paper_dir: Path | None, testnet_net_pnl_usd: float, testnet_equity_usd: float) -> dict[str, Any] | None:
    """Compare live Testnet cumulative return against the parallel Paper L2 ledger."""
    if paper_dir is None:
        return None
    ticks_path = paper_dir / "ticks.parquet"
    state_path = paper_dir / "state.json"
    if not (ticks_path.is_file() and state_path.is_file()):
        return None
    ticks = pd.read_parquet(ticks_path)
    state = json.loads(state_path.read_text())
    if ticks.empty:
        return None
    start_cap = float(state.get("starting_capital_usd", 100_000.0))
    paper_eq = float(ticks["equity_usd"].iloc[-1])
    paper_ret_pct = (paper_eq / start_cap - 1.0) * 100.0 if start_cap > 0 else 0.0
    testnet_start = max(1.0, testnet_equity_usd - testnet_net_pnl_usd)
    testnet_ret_pct = (testnet_net_pnl_usd / testnet_start) * 100.0
    return {
        "paper_ticks": int(len(ticks)),
        "paper_equity_usd": round(paper_eq, 2),
        "paper_return_pct": round(paper_ret_pct, 4),
        "testnet_return_pct": round(testnet_ret_pct, 4),
        "tracking_diff_pct": round(testnet_ret_pct - paper_ret_pct, 4),
    }


def compute_post_trade_attribution(
    snapshot: dict[str, Any],
    *,
    audit_log_path: Path | None = None,
    paper_dir: Path | None = None,
    liq_warn_threshold_pct: float = 15.0,
    adl_warn_threshold: float = 2.0,
    is_growth_mode: bool = False,
) -> PostTradeAttribution:
    """Compute complete post-trade attribution, real ALO fill rate, markout & liquidation audit."""
    fills: list[dict[str, Any]] = snapshot.get("fills") or []
    funding: list[dict[str, Any]] = snapshot.get("funding") or []
    ch: dict[str, Any] = snapshot.get("clearinghouse") or {}
    spot: dict[str, Any] = snapshot.get("spot") or {}
    open_orders: list[dict[str, Any]] = snapshot.get("open_orders") or []
    mids: dict[str, float] = snapshot.get("all_mids") or {}

    perps_eq = _f((ch.get("marginSummary") or {}).get("accountValue"))
    withdrawable = _f(ch.get("withdrawable"))
    spot_usdc = 0.0
    spot_hold = 0.0
    for bal in spot.get("balances") or []:
        if str(bal.get("coin")) == "USDC":
            spot_usdc = _f(bal.get("total"))
            spot_hold = _f(bal.get("hold"))
            break

    # Map open positions
    pos_map: dict[str, dict[str, Any]] = {}
    for entry in ch.get("assetPositions") or []:
        p = entry.get("position") or {}
        coin = str(p.get("coin") or "").strip()
        if coin and abs(_f(p.get("szi"))) > 1e-12:
            pos_map[coin] = p

    abstraction = str(snapshot.get("user_abstraction") or "")
    is_unified = abstraction in {"unifiedAccount", "portfolioMargin"} or (
        spot_usdc > 0 and withdrawable <= 0.0 and (spot_hold > 0.0 or perps_eq <= 0.0)
    )
    if is_unified and spot_usdc > 0:
        # Verified against HL ``portfolio`` API: Unified Account ``spot.USDC.total``
        # already reflects locked perp margin and live unrealized PnL.
        total_eq = spot_usdc
    else:
        total_eq = perps_eq + spot_usdc

    # Aggregate fills by coin
    fill_stats: dict[str, dict[str, float]] = {}
    filled_oids: set[int] = set()
    tot_vol = 0.0
    maker_vol = 0.0
    tot_fees = 0.0
    tot_realized = 0.0
    tot_mk_num = 0.0
    tot_mk_den = 0.0

    for fl in fills:
        coin = str(fl.get("coin") or "").strip()
        if not coin:
            continue
        px = _f(fl.get("px"))
        sz = abs(_f(fl.get("sz")))
        notional = px * sz
        fee = _f(fl.get("fee"))
        closed_pnl = _f(fl.get("closedPnl"))
        crossed = bool(fl.get("crossed", False))
        if "oid" in fl and fl["oid"] is not None:
            filled_oids.add(int(fl["oid"]))

        st = fill_stats.setdefault(
            coin,
            {"count": 0.0, "vol": 0.0, "maker_vol": 0.0, "fees": 0.0, "realized": 0.0, "mk_num": 0.0, "mk_den": 0.0},
        )
        st["count"] += 1.0
        st["vol"] += notional
        if not crossed:
            st["maker_vol"] += notional
            maker_vol += notional
        st["fees"] += fee
        st["realized"] += closed_pnl

        tot_vol += notional
        tot_fees += fee
        tot_realized += closed_pnl

        mid = mids.get(coin, 0.0)
        if mid > 0 and px > 0 and notional > 0:
            side = str(fl.get("side", ""))
            sgn = 1.0 if side == "B" else -1.0
            mk_bps = sgn * (mid - px) / px * 1e4
            st["mk_num"] += mk_bps * notional
            st["mk_den"] += notional
            tot_mk_num += mk_bps * notional
            tot_mk_den += notional

    # Aggregate funding payments by coin
    fund_by_coin: dict[str, float] = {}
    tot_funding = 0.0
    for item in funding:
        delta = item.get("delta") if isinstance(item.get("delta"), dict) else item
        coin = str(delta.get("coin") or "").strip()
        usdc = _f(delta.get("usdc"))
        if coin:
            fund_by_coin[coin] = fund_by_coin.get(coin, 0.0) + usdc
        tot_funding += usdc

    # Audit ALO order fill rate against local executor audit trail
    audit_oids = extract_audit_oids(audit_log_path)
    resting_oids = {int(o["oid"]) for o in open_orders if isinstance(o, dict) and o.get("oid") is not None}
    audit_filled = len(audit_oids & filled_oids)
    audit_resting = len(audit_oids & resting_oids)
    real_alo_fill_rate = (audit_filled / len(audit_oids) * 100.0) if audit_oids else None

    # Build per-coin rows & liquidation audit
    all_coins = sorted(set(pos_map.keys()) | set(fill_stats.keys()) | set(fund_by_coin.keys()))
    per_coin: list[CoinAttribution] = []
    tot_unrealized = 0.0
    liq_warnings: list[str] = []
    adl_warnings: list[str] = []
    liq_dists: list[float] = []

    for coin in all_coins:
        p = pos_map.get(coin, {})
        szi = _f(p.get("szi"))
        entry_px = _f(p.get("entryPx"))
        pos_usd = abs(_f(p.get("positionValue")))
        unreal = _f(p.get("unrealizedPnl"))
        tot_unrealized += unreal
        lev_info = p.get("leverage") if isinstance(p.get("leverage"), dict) else {}
        lev_mode = f"{lev_info.get('type', 'none')}:{lev_info.get('value', '')}" if lev_info else "flat"
        liq_raw = p.get("liquidationPx")
        liq_px = _f(liq_raw) if liq_raw is not None and _f(liq_raw) > 0 else None
        cur_mid = mids.get(coin, entry_px)

        liq_dist: float | None = None
        if liq_px is not None and cur_mid > 0 and abs(szi) > 1e-12:
            liq_dist = abs(cur_mid - liq_px) / cur_mid * 100.0
            liq_dists.append(liq_dist)
            if liq_dist < liq_warn_threshold_pct:
                liq_warnings.append(f"{coin} ({lev_mode}): до ликв. {liq_dist:.1f}% (mid {cur_mid:g}, liq {liq_px:g})")

        # Hyperliquid ADL Queue Priority Monitor:
        # ADL Rank ≈ (Mark/Entry for long, Entry/Mark for short) × (Notional / Denominator)
        # Strictly profitable positions (unreal > 0) can be auto-deleveraged.
        # Denominator scope: isolated positions use their isolated `marginUsed` (not whole account!);
        # cross positions use the DEX's accountValue (`total_eq`).
        adl_rank: float | None = None
        if abs(szi) > 1e-12 and unreal > 0.0 and entry_px > 0.0 and cur_mid > 0.0:
            lev_type = str(lev_info.get("type") or "cross").lower()
            lev_val = max(1.0, _f(lev_info.get("value"), 1.0))
            if lev_type == "isolated":
                iso_margin = _f(p.get("marginUsed"))
                denom = iso_margin if iso_margin > 0.0 else (pos_usd / lev_val)
            else:
                denom = total_eq
            if denom > 0.0:
                pnl_ratio = (cur_mid / entry_px) if szi > 0 else (entry_px / cur_mid)
                eff_lev = pos_usd / denom
                adl_rank = round(max(1.0, pnl_ratio) * eff_lev, 3)
                upnl_pct = unreal / max(pos_usd, 1.0) * 100.0
                if adl_rank >= adl_warn_threshold or (szi < 0 and upnl_pct >= 10.0 and eff_lev >= 1.0):
                    side_lbl = "SHORT" if szi < 0 else "LONG"
                    scope_lbl = "isolated margin" if lev_type == "isolated" else "cross equity"
                    adl_warnings.append(
                        f"{coin} {side_lbl} ({scope_lbl}): ADL Rank {adl_rank:.2f} (uPnL +{upnl_pct:.1f}%, eff.lev {eff_lev:.1f}x) — "
                        "ротируйте/кристаллизуйте PnL для сброса очереди автоделевериджа"
                    )

        fs = fill_stats.get(coin, {})
        c_vol = float(fs.get("vol", 0.0))
        c_maker_pct = (float(fs.get("maker_vol", 0.0)) / c_vol * 100.0) if c_vol > 0 else 100.0
        c_real = float(fs.get("realized", 0.0))
        c_fees = -float(fs.get("fees", 0.0))
        c_fund = float(fund_by_coin.get(coin, 0.0))
        c_net = c_real + unreal + c_fund + c_fees
        c_mk = (float(fs["mk_num"]) / float(fs["mk_den"])) if fs.get("mk_den", 0.0) > 0 else None

        per_coin.append(
            CoinAttribution(
                coin=coin,
                position_sz=szi,
                position_usd=round(pos_usd, 2),
                entry_px=entry_px,
                current_mid=cur_mid,
                leverage_mode=lev_mode,
                liq_px=liq_px,
                liq_distance_pct=round(liq_dist, 2) if liq_dist is not None else None,
                fills_count=int(fs.get("count", 0)),
                fill_volume_usd=round(c_vol, 2),
                maker_share_pct=round(c_maker_pct, 1),
                realized_pnl_usd=round(c_real, 4),
                unrealized_pnl_usd=round(unreal, 4),
                funding_pnl_usd=round(c_fund, 4),
                fees_usd=round(c_fees, 4),
                net_pnl_usd=round(c_net, 4),
                vw_markout_bps=round(c_mk, 2) if c_mk is not None else None,
                adl_rank_score=adl_rank,
            )
        )

    per_coin.sort(key=lambda c: (-c.position_usd, -c.fill_volume_usd, c.coin))
    net_attr = tot_realized + tot_unrealized + tot_funding - tot_fees
    paper_cmp = load_paper_comparison(paper_dir, net_attr, total_eq)

    # Address API Rate-Limit Health:
    # Standard HyperCore grants 1 request per 1 USDC traded (+10k initial buffer).
    # HIP-3 Growth Mode applies -90% volume weighting (0.1 req per 1 USDC = 1 req / 10 USDC).
    # Safe fill-rate threshold scales inversely with average order size (1% at $100, 0.05% at $2,000).
    api_reqs_per_usd: float | None = None
    api_quota_health = "OK"
    if len(audit_oids) > 0:
        vol_weight = 0.1 if is_growth_mode else 1.0
        eff_vol = max(tot_vol * vol_weight, 1.0)
        api_reqs_per_usd = round(len(audit_oids) / eff_vol, 4)
        avg_order_usd = (tot_vol / max(len(fills), 1)) if tot_vol > 0 else 100.0
        safe_fill_pct = min(100.0, (100.0 / max(avg_order_usd * vol_weight, 1.0)))
        low_fill_risk = (
            real_alo_fill_rate is not None
            and real_alo_fill_rate < safe_fill_pct
            and len(audit_oids) >= 20
        )
        if api_reqs_per_usd > 1.0 or low_fill_risk:
            gm_tag = " [Growth 0.1x]" if is_growth_mode else ""
            api_quota_health = f"THROTTLE_RISK (>1 req/eff.${gm_tag} or fill<{safe_fill_pct:.2f}%)"
        elif api_reqs_per_usd > 0.5:
            api_quota_health = "WATCH (>0.5 req/eff.$)"

    return PostTradeAttribution(
        account_address=str(snapshot.get("account_address", "")),
        observed_at_ms=int(snapshot.get("observed_at_ms", 0)),
        equity_usd=round(total_eq, 4),
        perps_equity_usd=round(perps_eq, 4),
        spot_usdc_usd=round(spot_usdc, 4),
        open_positions_count=len(pos_map),
        open_orders_count=len(open_orders),
        total_fills=len(fills),
        total_volume_usd=round(tot_vol, 2),
        maker_volume_share_pct=round((maker_vol / tot_vol * 100.0) if tot_vol > 0 else 0.0, 2),
        effective_fee_bps=round((tot_fees / tot_vol * 1e4) if tot_vol > 0 else 0.0, 3),
        audit_orders_placed=len(audit_oids),
        audit_orders_filled=audit_filled,
        audit_orders_resting=audit_resting,
        real_alo_fill_rate_pct=round(real_alo_fill_rate, 1) if real_alo_fill_rate is not None else None,
        realized_price_pnl_usd=round(tot_realized, 4),
        unrealized_price_pnl_usd=round(tot_unrealized, 4),
        funding_pnl_usd=round(tot_funding, 4),
        fees_usd=round(-tot_fees, 4),
        net_attributed_pnl_usd=round(net_attr, 4),
        vw_markout_bps=round(tot_mk_num / tot_mk_den, 2) if tot_mk_den > 0 else None,
        min_liq_distance_pct=round(min(liq_dists), 2) if liq_dists else None,
        liq_warnings=liq_warnings,
        per_coin=per_coin,
        paper_comparison=paper_cmp,
        adl_warnings=adl_warnings,
        api_reqs_per_usd_vol=api_reqs_per_usd,
        api_quota_health=api_quota_health,
    )


def format_attribution_markdown(attr: PostTradeAttribution) -> str:
    ts = datetime.fromtimestamp(attr.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    fill_rate_txt = f"{attr.real_alo_fill_rate_pct:.1f}% ({attr.audit_orders_filled}/{attr.audit_orders_placed})" if attr.real_alo_fill_rate_pct is not None else "—"
    mk_txt = f"{attr.vw_markout_bps:+.2f} б.п." if attr.vw_markout_bps is not None else "—"
    min_liq_txt = f"{attr.min_liq_distance_pct:.1f}%" if attr.min_liq_distance_pct is not None else "нет (cross/flat)"

    lines = [
        f"# Пост-трейд атрибуция Hyperliquid Testnet ({ts})",
        "",
        f"- Адрес: `{attr.account_address}` · Баланс: **${attr.equity_usd:,.2f}** (Spot USDC ${attr.spot_usdc_usd:,.2f} · маржа Perps ${attr.perps_equity_usd:,.2f})",
        f"- **Атрибуция PnL:** всего **{attr.net_attributed_pnl_usd:+,.2f}$** = Realized {attr.realized_price_pnl_usd:+,.2f}$ + Unrealized {attr.unrealized_price_pnl_usd:+,.2f}$ + Funding {attr.funding_pnl_usd:+,.2f}$ + Fees {attr.fees_usd:+,.2f}$",
        f"- **Качество исполнения:** сделок {attr.total_fills}, оборот ${attr.total_volume_usd:,.2f}, **Maker доля {attr.maker_volume_share_pct:.1f}%**, факт. комиссия **{attr.effective_fee_bps:.2f} б.п.**, реальный **ALO Fill Rate {fill_rate_txt}**, VW Markout **{mk_txt}**",
        f"- **Риск маржи & API Rate-Limit:** открыто {attr.open_positions_count} поз. / {attr.open_orders_count} ордеров · мин. дистанция до ликвидации: **{min_liq_txt}** · API quota: **`{attr.api_quota_health}`** ({f'{attr.api_reqs_per_usd_vol:.3f} req/$' if attr.api_reqs_per_usd_vol is not None else '—'})",
    ]
    if attr.paper_comparison:
        pc = attr.paper_comparison
        lines.append(
            f"- **Сверка Testnet vs Paper L2:** Testnet `{pc['testnet_return_pct']:+.3f}%` vs Paper `{pc['paper_return_pct']:+.3f}%` "
            f"(расхождение `{pc['tracking_diff_pct']:+.3f}%`, Paper тиков: {pc['paper_ticks']})"
        )
    if attr.liq_warnings:
        lines += ["", "> ⚠️ **Предупреждения по ликвидации (< 15%):**", *[f"> - {w}" for w in attr.liq_warnings]]
    if attr.adl_warnings:
        lines += ["", "> 🚨 **Предупреждения по ADL-очереди (автоделеверидж):**", *[f"> - {w}" for w in attr.adl_warnings]]

    lines += [
        "",
        "## Разрез по монетам",
        "",
        "| Монета | Позиция ($) | Режим | Филлов | Maker % | Net PnL | Markout | До ликв. | ADL Rank |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|",
    ]
    for c in attr.per_coin:
        side = "Лонг" if c.position_sz > 0 else ("Шорт" if c.position_sz < 0 else "Закрыта")
        pos_txt = f"{side} ${c.position_usd:,.1f}" if c.position_usd > 0 else "Закрыта"
        mk = f"{c.vw_markout_bps:+.1f} б.п." if c.vw_markout_bps is not None else "—"
        lq = f"{c.liq_distance_pct:.1f}%" if c.liq_distance_pct is not None else "—"
        adl_t = f"{c.adl_rank_score:.2f}" if c.adl_rank_score is not None else "—"
        lines.append(
            f"| **{c.coin}** | {pos_txt} | `{c.leverage_mode}` | {c.fills_count} | {c.maker_share_pct:.0f}% | "
            f"{c.net_pnl_usd:+,.2f}$ | {mk} | {lq} | {adl_t} |"
        )
    return "\n".join(lines) + "\n"


def format_attribution_telegram_rich_html(attr: PostTradeAttribution, *, max_rows: int = 12) -> str:
    """Plain-Russian Telegram Rich HTML table (``sendRichMessage``) for post-trade attribution."""
    dt = datetime.fromtimestamp(attr.observed_at_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    pnl_dot = "🟢" if attr.net_attributed_pnl_usd >= 0 else "🔴"
    fill_txt = f"{attr.real_alo_fill_rate_pct:.0f}% ({attr.audit_orders_filled}/{attr.audit_orders_placed})" if attr.real_alo_fill_rate_pct is not None else "—"
    mk_txt = f"{attr.vw_markout_bps:+.1f} б.п." if attr.vw_markout_bps is not None else "—"

    parts = [
        f"<h1>🧾 Атрибуция сделок Testnet · {html.escape(date_str)}</h1>",
        (
            f"<p>{pnl_dot} <b>Итого PnL:</b> {attr.net_attributed_pnl_usd:+,.2f}$ "
            f"(цена {attr.realized_price_pnl_usd + attr.unrealized_price_pnl_usd:+,.2f}$, "
            f"фандинг {attr.funding_pnl_usd:+,.2f}$, комиссии {attr.fees_usd:+,.2f}$)<br>"
            f"<b>Исполнение ({html.escape(time_str)}):</b> Maker <b>{attr.maker_volume_share_pct:.0f}%</b> · "
            f"комиссия {attr.effective_fee_bps:.2f} б.п. · ALO Fill {html.escape(fill_txt)} · Markout {html.escape(mk_txt)} · "
            f"API: <b>{html.escape(attr.api_quota_health)}</b></p>"
        ),
    ]
    if attr.liq_warnings:
        parts.append(f"<p>⚠️ <b>Риск ликвидации:</b> {html.escape('; '.join(attr.liq_warnings))}</p>")
    if attr.adl_warnings:
        parts.append(f"<p>🚨 <b>Риск ADL (автоделеверидж):</b> {html.escape('; '.join(attr.adl_warnings))}</p>")

    if attr.per_coin:
        rows = [
            "<tr>"
            '<th align="left">Монета</th>'
            '<th align="left">Позиция</th>'
            '<th align="right">PnL</th>'
            '<th align="right">Markout</th>'
            '<th align="right">До ликв.</th>'
            "</tr>"
        ]
        for c in attr.per_coin[:max_rows]:
            if c.position_sz > 0:
                pos_txt = f"🟢 Лонг ${c.position_usd:,.0f}"
            elif c.position_sz < 0:
                pos_txt = f"🔴 Шорт ${c.position_usd:,.0f}"
            else:
                pos_txt = "⚪ Закрыта"
            c_dot = "🟢" if c.net_pnl_usd > 0.01 else ("🔴" if c.net_pnl_usd < -0.01 else "⚪")
            mk = f"{c.vw_markout_bps:+.1f}" if c.vw_markout_bps is not None else "—"
            lq = f"{c.liq_distance_pct:.1f}%" if c.liq_distance_pct is not None else "—"
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(c.coin)}</b></td>'
                f'<td align="left">{html.escape(pos_txt)}</td>'
                f'<td align="right">{c_dot} {c.net_pnl_usd:+,.2f}$</td>'
                f'<td align="right">{html.escape(mk)}</td>'
                f'<td align="right">{html.escape(lq)}</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    parts.append("<footer>Пост-трейд аудит Hyperliquid Testnet (userFills + userFunding + L2 markout)</footer>")
    return "".join(parts)
