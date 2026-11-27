"""Read-only Cross-DEX perpetual arbitrage scanner v2 (5 Perp DEXs + L2 Depth VWAP).

Zero auth, zero orders. Normalizes live public perpetual snapshots & L2 orderbooks from:
* **Hyperliquid** (``metaAndAssetCtxs`` + ``l2Book`` — 1h funding)
* **Lighter** (``mainnet.zklighter.elliot.ai`` — 8h-normalized funding + ``orderBookOrders``)
* **Extended** (``api.starknet.extended.exchange`` — 1h funding + ``/orderbook``)
* **dYdX v4** (``indexer.dydx.trade`` — 1h funding + ``/orderbooks/perpetualMarket``)
* **Paradex** (``api.prod.paradex.trade`` — interval-normalized funding + ``/orderbook``)

Computes both headline funding spreads and **true L2 depth-weighted VWAP executable spreads**
for ``$10k`` and ``$50k`` clip sizes plus bridge/rebalancing drag.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

import httpx
import pandas as pd

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
DYDX_MARKETS_URL = "https://indexer.dydx.trade/v4/perpetualMarkets"
DYDX_BOOK_URL = "https://indexer.dydx.trade/v4/orderbooks/perpetualMarket"
PARADEX_SUMMARY_URL = "https://api.prod.paradex.trade/v1/markets/summary"
PARADEX_BOOK_URL = "https://api.prod.paradex.trade/v1/orderbook"
LIGHTER_BASE_URL = "https://mainnet.zklighter.elliot.ai/api/v1"
EXTENDED_MARKETS_URL = "https://api.starknet.extended.exchange/api/v1/info/markets"
HOURS_PER_YEAR = 8760.0


@dataclass(frozen=True, slots=True)
class DexMarketQuote:
    dex: str
    coin: str
    raw_symbol: str
    mark_px: float
    funding_rate_h: float
    funding_apr_pct: float
    day_vol_usd: float
    oi_usd: float
    spread_bps: float | None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(val: object, default: float = 0.0) -> float:
    try:
        x = float(val)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


K_PREFIX_BASES: frozenset[str] = frozenset({"PEPE", "BONK", "SHIB", "FLOKI", "DOGS", "NEIRO", "LUNC"})

DEFAULT_VENUE_RT_FEE_BPS: dict[str, float] = {
    "lighter": 9.0,  # 2 * (4.5 HL + 0.0 Lighter)
    "extended": 13.0,  # 2 * (4.5 HL + 2.0 Extended)
    "paradex": 15.0,  # 2 * (4.5 HL + 3.0 Paradex)
    "dydx": 15.0,  # 2 * (4.5 HL + 3.0 dYdX)
}


def default_round_trip_fee_bps(other_dex: str, *, bridge_drag_bps: float = 0.0) -> float:
    """Return conservative two-leg round-trip fee (plus optional bridge drag) for ``other_dex``."""
    base = DEFAULT_VENUE_RT_FEE_BPS.get(str(other_dex or "").strip().lower(), 13.0)
    return base + float(bridge_drag_bps)


def _normalize_coin(raw_base: str) -> str:
    raw = raw_base.strip()
    if not raw:
        return ""
    if len(raw) > 1 and raw[0] == "k" and raw[1:] == raw[1:].upper() and any(ch.isalpha() for ch in raw[1:]):
        return "k" + raw[1:].upper()
    s = raw.upper()
    if s.startswith("1000") and len(s) > 4:
        return "k" + s[4:]
    if s.startswith("K") and s[1:] in K_PREFIX_BASES:
        return "k" + s[1:]
    return s


# --------------------------------------------------------------------------- 5-DEX Public Readers


def fetch_hyperliquid_quotes(client: httpx.Client) -> dict[str, DexMarketQuote]:
    resp = client.post(HL_INFO_URL, json={"type": "metaAndAssetCtxs"})
    resp.raise_for_status()
    payload = resp.json()
    universe, ctxs = payload[0]["universe"], payload[1]
    out: dict[str, DexMarketQuote] = {}
    for asset, ctx in zip(universe, ctxs, strict=False):
        if asset.get("isDelisted"):
            continue
        raw_coin = str(asset.get("name") or "").strip()
        if not raw_coin:
            continue
        coin = _normalize_coin(raw_coin)
        mark = _f(ctx.get("markPx"))
        if mark <= 0:
            continue
        rate_h = _f(ctx.get("funding"))
        oi_usd = _f(ctx.get("openInterest")) * mark
        vol_usd = _f(ctx.get("dayNtlVlm"))
        imp = ctx.get("impactPxs")
        sp_bps = None
        if isinstance(imp, list) and len(imp) == 2:
            b, a = _f(imp[0]), _f(imp[1])
            if b > 0 and a >= b:
                sp_bps = (a - b) / (0.5 * (a + b)) * 1e4
        out[coin] = DexMarketQuote(
            dex="hyperliquid",
            coin=coin,
            raw_symbol=raw_coin,
            mark_px=mark,
            funding_rate_h=rate_h,
            funding_apr_pct=rate_h * HOURS_PER_YEAR * 100.0,
            day_vol_usd=vol_usd,
            oi_usd=oi_usd,
            spread_bps=sp_bps,
        )
    return out


def fetch_dydx_quotes(client: httpx.Client) -> dict[str, DexMarketQuote]:
    resp = client.get(DYDX_MARKETS_URL)
    resp.raise_for_status()
    markets = (resp.json() or {}).get("markets") or {}
    out: dict[str, DexMarketQuote] = {}
    for ticker, item in markets.items():
        if not isinstance(item, dict) or item.get("status") != "ACTIVE":
            continue
        if not str(ticker).endswith("-USD"):
            continue
        coin = _normalize_coin(str(ticker).split("-")[0])
        px = _f(item.get("oraclePrice"))
        if px <= 0:
            continue
        rate_h = _f(item.get("nextFundingRate"))
        vol_usd = _f(item.get("volume24H"))
        oi_usd = _f(item.get("openInterest")) * px
        out[coin] = DexMarketQuote(
            dex="dydx",
            coin=coin,
            raw_symbol=str(ticker),
            mark_px=px,
            funding_rate_h=rate_h,
            funding_apr_pct=rate_h * HOURS_PER_YEAR * 100.0,
            day_vol_usd=vol_usd,
            oi_usd=oi_usd,
            spread_bps=None,
        )
    return out


def fetch_paradex_quotes(client: httpx.Client) -> dict[str, DexMarketQuote]:
    resp = client.get(PARADEX_SUMMARY_URL, params={"market": "ALL"})
    resp.raise_for_status()
    rows = (resp.json() or {}).get("results") or []
    out: dict[str, DexMarketQuote] = {}
    for item in rows:
        if not isinstance(item, dict):
            continue
        sym = str(item.get("symbol") or "")
        if not sym.endswith("-USD-PERP"):
            continue
        coin = _normalize_coin(sym.split("-")[0])
        px = _f(item.get("mark_price"))
        if px <= 0:
            continue
        interval_min = _f(item.get("funding_interval_minutes"), default=480.0)
        interval_h = max(1.0, interval_min / 60.0)
        rate_h = _f(item.get("funding_rate")) / interval_h
        vol_usd = _f(item.get("volume_24h"))
        oi_usd = _f(item.get("open_interest")) * px
        bid, ask = _f(item.get("bid")), _f(item.get("ask"))
        sp_bps = (ask - bid) / (0.5 * (ask + bid)) * 1e4 if bid > 0 and ask >= bid else None
        out[coin] = DexMarketQuote(
            dex="paradex",
            coin=coin,
            raw_symbol=sym,
            mark_px=px,
            funding_rate_h=rate_h,
            funding_apr_pct=rate_h * HOURS_PER_YEAR * 100.0,
            day_vol_usd=vol_usd,
            oi_usd=oi_usd,
            spread_bps=sp_bps,
        )
    return out


def fetch_lighter_quotes(client: httpx.Client) -> dict[str, DexMarketQuote]:
    """Fetch Lighter perp markets & 8h-normalized funding rates (divided by 8 -> 1h rate)."""
    ob_r = client.get(f"{LIGHTER_BASE_URL}/orderBookDetails")
    ob_r.raise_for_status()
    fr_r = client.get(f"{LIGHTER_BASE_URL}/funding-rates")
    fr_r.raise_for_status()

    fund_by_mid: dict[int, float] = {}
    for item in (fr_r.json() or {}).get("funding_rates") or []:
        if str(item.get("exchange")) == "lighter" and item.get("market_id") is not None:
            # Verified against HL/Binance in the same endpoint: rates are 8-hour funding fractions.
            fund_by_mid[int(item["market_id"])] = _f(item.get("rate")) / 8.0

    out: dict[str, DexMarketQuote] = {}
    for item in (ob_r.json() or {}).get("order_book_details") or []:
        if item.get("market_type") != "perp" or item.get("status") != "active":
            continue
        sym = str(item.get("symbol") or "").strip()
        mid = item.get("market_id")
        if not sym or mid is None or int(mid) not in fund_by_mid:
            continue
        px = _f(item.get("last_trade_price"))
        if px <= 0:
            continue
        coin = _normalize_coin(sym)
        rate_h = fund_by_mid[int(mid)]
        vol_usd = _f(item.get("daily_quote_token_volume"))
        oi_usd = _f(item.get("open_interest")) * px
        out[coin] = DexMarketQuote(
            dex="lighter",
            coin=coin,
            raw_symbol=str(int(mid)),  # market_id needed by orderBookOrders
            mark_px=px,
            funding_rate_h=rate_h,
            funding_apr_pct=rate_h * HOURS_PER_YEAR * 100.0,
            day_vol_usd=vol_usd,
            oi_usd=oi_usd,
            spread_bps=None,
        )
    return out


def fetch_extended_quotes(client: httpx.Client) -> dict[str, DexMarketQuote]:
    """Fetch Extended (Starknet) perp markets & 1h funding rates."""
    resp = client.get(EXTENDED_MARKETS_URL)
    resp.raise_for_status()
    rows = (resp.json() or {}).get("data") or []
    out: dict[str, DexMarketQuote] = {}
    for item in rows:
        if not isinstance(item, dict) or item.get("type") != "PERPETUAL":
            continue
        name = str(item.get("name") or "")
        if not name.endswith("-USD"):
            continue
        st = item.get("marketStats") or {}
        px = _f(st.get("markPrice"), _f(st.get("lastPrice")))
        if px <= 0:
            continue
        coin = _normalize_coin(name.split("-")[0])
        rate_h = _f(st.get("fundingRate"))
        vol_usd = _f(st.get("dailyVolume"))
        oi_usd = _f(st.get("openInterest"))
        bid, ask = _f(st.get("bidPrice")), _f(st.get("askPrice"))
        sp_bps = (ask - bid) / (0.5 * (ask + bid)) * 1e4 if bid > 0 and ask >= bid else None
        out[coin] = DexMarketQuote(
            dex="extended",
            coin=coin,
            raw_symbol=name,
            mark_px=px,
            funding_rate_h=rate_h,
            funding_apr_pct=rate_h * HOURS_PER_YEAR * 100.0,
            day_vol_usd=vol_usd,
            oi_usd=oi_usd,
            spread_bps=sp_bps,
        )
    return out


# --------------------------------------------------------------------------- L2 Depth VWAP Engine


def compute_book_vwap(levels: list[tuple[float, float]], clip_usd: float) -> tuple[float | None, float]:
    """Walk sorted ``(px, base_sz)`` levels and return ``(vwap_px, filled_usd)`` for ``clip_usd``."""
    if not levels or clip_usd <= 0:
        return None, 0.0
    rem = float(clip_usd)
    tot_usd = 0.0
    tot_base = 0.0
    for px, sz in levels:
        if px <= 0 or sz <= 0:
            continue
        lvl_usd = px * sz
        take_usd = min(rem, lvl_usd)
        tot_usd += take_usd
        tot_base += take_usd / px
        rem -= take_usd
        if rem <= 1e-6:
            break
    if tot_base <= 0:
        return None, 0.0
    return (tot_usd / tot_base), round(tot_usd, 2)


def fetch_dex_l2_book(
    client: httpx.Client,
    dex: str,
    raw_symbol: str,
) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
    """Fetch normalized ``(bids_desc, asks_asc)`` L2 orderbook levels ``[(px, base_sz), ...]``."""
    try:
        if dex == "hyperliquid":
            r = client.post(HL_INFO_URL, json={"type": "l2Book", "coin": raw_symbol})
            r.raise_for_status()
            lv = (r.json() or {}).get("levels") or [[], []]
            bids = sorted([(_f(x["px"]), _f(x["sz"])) for x in lv[0]], key=lambda t: -t[0])
            asks = sorted([(_f(x["px"]), _f(x["sz"])) for x in lv[1]], key=lambda t: t[0])
            return bids, asks
        if dex == "lighter":
            r = client.get(f"{LIGHTER_BASE_URL}/orderBookOrders", params={"market_id": raw_symbol, "limit": 50})
            r.raise_for_status()
            body = r.json() or {}
            bids = sorted([(_f(x["price"]), _f(x["remaining_base_amount"])) for x in (body.get("bids") or [])], key=lambda t: -t[0])
            asks = sorted([(_f(x["price"]), _f(x["remaining_base_amount"])) for x in (body.get("asks") or [])], key=lambda t: t[0])
            return bids, asks
        if dex == "extended":
            r = client.get(f"{EXTENDED_MARKETS_URL}/{raw_symbol}/orderbook")
            r.raise_for_status()
            data = (r.json() or {}).get("data") or {}
            bids = sorted([(_f(x["price"]), _f(x["qty"])) for x in (data.get("bid") or [])], key=lambda t: -t[0])
            asks = sorted([(_f(x["price"]), _f(x["qty"])) for x in (data.get("ask") or [])], key=lambda t: t[0])
            return bids, asks
        if dex == "dydx":
            r = client.get(f"{DYDX_BOOK_URL}/{raw_symbol}")
            r.raise_for_status()
            body = r.json() or {}
            bids = sorted([(_f(x["price"]), _f(x["size"])) for x in (body.get("bids") or [])], key=lambda t: -t[0])
            asks = sorted([(_f(x["price"]), _f(x["size"])) for x in (body.get("asks") or [])], key=lambda t: t[0])
            return bids, asks
        if dex == "paradex":
            r = client.get(f"{PARADEX_BOOK_URL}/{raw_symbol}")
            r.raise_for_status()
            body = r.json() or {}
            bids = sorted([(_f(x[0]), _f(x[1])) for x in (body.get("bids") or []) if len(x) >= 2], key=lambda t: -t[0])
            asks = sorted([(_f(x[0]), _f(x[1])) for x in (body.get("asks") or []) if len(x) >= 2], key=lambda t: t[0])
            return bids, asks
    except Exception:
        return [], []
    return [], []


def compute_executable_cross_dex_entry_bps(
    hl_bids: list[tuple[float, float]],
    hl_asks: list[tuple[float, float]],
    other_bids: list[tuple[float, float]],
    other_asks: list[tuple[float, float]],
    *,
    short_hl: bool,
    clip_usd: float,
) -> tuple[float | None, float | None, bool]:
    """Return ``(exec_basis_bps, total_l2_slip_bps, full_fill_ok)`` for entering ``clip_usd`` on both DEXs."""
    if not (hl_bids and hl_asks and other_bids and other_asks):
        return None, None, False
    hl_mid = 0.5 * (hl_bids[0][0] + hl_asks[0][0])
    oth_mid = 0.5 * (other_bids[0][0] + other_asks[0][0])
    if hl_mid <= 0 or oth_mid <= 0:
        return None, None, False

    if short_hl:
        # Sell HL (hit HL bids), Buy Other (lift Other asks)
        hl_exec, hl_filled = compute_book_vwap(hl_bids, clip_usd)
        oth_exec, oth_filled = compute_book_vwap(other_asks, clip_usd)
        if hl_exec is None or oth_exec is None:
            return None, None, False
        exec_basis = (hl_exec - oth_exec) / oth_exec * 1e4
        slip_hl = (hl_mid - hl_exec) / hl_mid * 1e4
        slip_oth = (oth_exec - oth_mid) / oth_mid * 1e4
    else:
        # Buy HL (lift HL asks), Sell Other (hit Other bids)
        hl_exec, hl_filled = compute_book_vwap(hl_asks, clip_usd)
        oth_exec, oth_filled = compute_book_vwap(other_bids, clip_usd)
        if hl_exec is None or oth_exec is None:
            return None, None, False
        exec_basis = (oth_exec - hl_exec) / hl_exec * 1e4
        slip_hl = (hl_exec - hl_mid) / hl_mid * 1e4
        slip_oth = (oth_mid - oth_exec) / oth_mid * 1e4

    full_ok = hl_filled >= clip_usd * 0.98 and oth_filled >= clip_usd * 0.98
    return round(exec_basis, 2), round(max(0.0, slip_hl) + max(0.0, slip_oth), 2), full_ok


def compute_book_mid_basis_bps(
    hl_bids: list[tuple[float, float]],
    hl_asks: list[tuple[float, float]],
    other_bids: list[tuple[float, float]],
    other_asks: list[tuple[float, float]],
    *,
    short_hl: bool,
) -> float | None:
    """Directional top-of-book mid basis (same sign convention as ``favorable_basis_bps``).

    An executable VWAP basis can never beat this mid basis; if it beats the *mark* basis by a
    wide margin the two price sources (mark/oracle vs. L2 book) disagree and the L2 edge is not
    something the mark-to-market ledger will ever realize.
    """
    if not (hl_bids and hl_asks and other_bids and other_asks):
        return None
    hl_mid = 0.5 * (hl_bids[0][0] + hl_asks[0][0])
    oth_mid = 0.5 * (other_bids[0][0] + other_asks[0][0])
    if hl_mid <= 0 or oth_mid <= 0:
        return None
    if short_hl:
        return round((hl_mid - oth_mid) / oth_mid * 1e4, 2)
    return round((oth_mid - hl_mid) / hl_mid * 1e4, 2)


def credited_entry_basis_bps(exec_basis_bps: float | None, mark_basis_bps: float | None) -> float:
    """Basis edge that may be credited against costs: never more than the mark basis.

    Positions are marked on ``mark_px``; a favorable L2 book basis that is not also present in the
    mark basis (stale/async book, venue mark offset) cannot converge into realized PnL.
    """
    if exec_basis_bps is None:
        return 0.0 if mark_basis_bps is None else min(0.0, float(mark_basis_bps))
    if mark_basis_bps is None:
        return float(exec_basis_bps)
    return float(min(exec_basis_bps, mark_basis_bps))


def scan_cross_dex_opportunities(
    hl_quotes: dict[str, DexMarketQuote],
    other_quotes: dict[str, DexMarketQuote],
    *,
    min_day_vol_usd: float = 500_000.0,
    max_price_log_diff: float = 0.05,
    round_trip_fee_bps: float = 7.0,
    bridge_rebalance_drag_bps: float = 2.0,
) -> pd.DataFrame:
    """Compare Hyperliquid against another Perp DEX and rank cross-DEX funding opportunities."""
    rows: list[dict[str, Any]] = []
    common = sorted(set(hl_quotes.keys()) & set(other_quotes.keys()))
    total_cost_bps = round_trip_fee_bps + bridge_rebalance_drag_bps
    for coin in common:
        h = hl_quotes[coin]
        o = other_quotes[coin]
        if min(h.day_vol_usd, o.day_vol_usd) < min_day_vol_usd:
            continue
        if abs(math.log(h.mark_px / o.mark_px)) > max_price_log_diff:
            continue
        diff_apr = h.funding_apr_pct - o.funding_apr_pct
        abs_apr = abs(diff_apr)
        if diff_apr >= 0:
            direction = f"Short HL / Long {o.dex}"
            favorable_basis_bps = (h.mark_px - o.mark_px) / o.mark_px * 1e4
        else:
            direction = f"Long HL / Short {o.dex}"
            favorable_basis_bps = (o.mark_px - h.mark_px) / h.mark_px * 1e4

        hourly_spread_bps = abs(h.funding_rate_h - o.funding_rate_h) * 1e4
        breakeven_h = (total_cost_bps / hourly_spread_bps) if hourly_spread_bps > 1e-9 else float("inf")
        rows.append(
            {
                "coin": coin,
                "other_dex": o.dex,
                "other_raw_symbol": o.raw_symbol,
                "direction": direction,
                "hl_apr_pct": round(h.funding_apr_pct, 2),
                "other_apr_pct": round(o.funding_apr_pct, 2),
                "spread_apr_pct": round(abs_apr, 2),
                "favorable_basis_bps": round(favorable_basis_bps, 2),
                "breakeven_hours": round(breakeven_h, 1) if math.isfinite(breakeven_h) else float("inf"),
                "hl_vol_m": round(h.day_vol_usd / 1e6, 2),
                "other_vol_m": round(o.day_vol_usd / 1e6, 2),
                "hl_oi_m": round(h.oi_usd / 1e6, 2),
                "other_oi_m": round(o.oi_usd / 1e6, 2),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(by=["spread_apr_pct", "favorable_basis_bps"], ascending=[False, False]).reset_index(drop=True)


def enrich_opportunities_with_l2_depth(
    client: httpx.Client,
    df: pd.DataFrame,
    *,
    top_n: int = 12,
    round_trip_fee_bps: float | None = None,
    bridge_drag_bps: float = 1.5,
) -> pd.DataFrame:
    """Fetch live L2 books on both venues for top-N rows and compute $10k/$50k executable entry & slippage."""
    if df.empty:
        return df
    sub = df.head(top_n).copy()
    hl_cache: dict[str, tuple[list[tuple[float, float]], list[tuple[float, float]]]] = {}
    exec_10k: list[float | None] = []
    slip_10k: list[float | None] = []
    slip_50k: list[float | None] = []
    depth_ok_50k: list[bool] = []
    be_10k_h: list[float] = []
    book_mid: list[float | None] = []
    credited: list[float] = []
    rt_cost_list: list[float] = []

    for r in sub.itertuples(index=False):
        coin = str(r.coin)
        hl_sym = "k" + coin[1:] if coin.startswith("k") else coin
        if coin not in hl_cache:
            hl_cache[coin] = fetch_dex_l2_book(client, "hyperliquid", hl_sym)
        hb, ha = hl_cache[coin]
        ob, oa = fetch_dex_l2_book(client, str(r.other_dex), str(r.other_raw_symbol))
        short_hl = str(r.direction).startswith("Short HL")
        eb10, sl10, _ = compute_executable_cross_dex_entry_bps(hb, ha, ob, oa, short_hl=short_hl, clip_usd=10_000.0)
        _, sl50, ok50 = compute_executable_cross_dex_entry_bps(hb, ha, ob, oa, short_hl=short_hl, clip_usd=50_000.0)
        exec_10k.append(eb10)
        slip_10k.append(sl10)
        slip_50k.append(sl50)
        depth_ok_50k.append(ok50)
        hourly_bps = float(r.spread_apr_pct) / (HOURS_PER_YEAR * 100.0) * 1e4
        mid_b = compute_book_mid_basis_bps(hb, ha, ob, oa, short_hl=short_hl)
        book_mid.append(mid_b)
        mark_b = float(r.favorable_basis_bps) if hasattr(r, "favorable_basis_bps") else None
        credit = credited_entry_basis_bps(eb10, mark_b)
        credited.append(round(credit, 2))
        row_rt_bps = (
            float(round_trip_fee_bps)
            if round_trip_fee_bps is not None
            else default_round_trip_fee_bps(str(r.other_dex), bridge_drag_bps=bridge_drag_bps)
        )
        rt_cost_list.append(round(row_rt_bps, 2))
        # Entry slippage is already inside the VWAP exec basis; sl10 is charged once more as the
        # symmetric exit-side slippage estimate.
        eff_cost = row_rt_bps + (sl10 if sl10 is not None else 0.0) - credit
        be_10k_h.append(round(max(0.0, eff_cost) / hourly_bps, 1) if hourly_bps > 1e-9 else float("inf"))

    sub["round_trip_fee_bps"] = rt_cost_list
    sub["exec_basis_10k_bps"] = exec_10k
    sub["l2_slip_10k_bps"] = slip_10k
    sub["l2_slip_50k_bps"] = slip_50k
    sub["depth_50k_ok"] = depth_ok_50k
    sub["book_mid_basis_bps"] = book_mid
    sub["credited_basis_10k_bps"] = credited
    sub["depth_be_10k_h"] = be_10k_h
    return sub
