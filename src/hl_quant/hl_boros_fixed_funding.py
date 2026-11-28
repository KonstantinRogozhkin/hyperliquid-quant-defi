"""Pendle Boros Open API Scanner & Fixed-Rate Funding Arbitrage Monitor for Hyperliquid.

100% read-only client over ``https://api-boros.pendle.finance/apis/v1/markets`` (with full
``resumeToken`` cursor pagination) combined with live Hyperliquid Core/HIP-3 & Lighter funding:

- Tracks all active (``timeToMaturity > 0``) Hyperliquid single-venue markets
  (e.g. ``Hyperliquid BTC/ETH/HYPE 25 Dec 2026``, ``Hyperliquid xyz:BRENTOIL``) and
  **Cross-Venue Spread Markets** (e.g. ``Hyperliquid ETH - Gate ETHUSDT``).
- Computes executable fixed-vs-floating dislocations (``bestBid - floatingApr`` for locking
  fixed short funding, or ``floatingApr - bestAsk`` for paying fixed to receive floating),
  exact bid-ask maturity-scaled cost in bps, and 3-leg fixed-rate lock APR vs Lighter.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import math
import time
from typing import Any

import httpx

from hl_quant import cross_dex_scanner as cds

BOROS_MARKETS_URL = "https://api-boros.pendle.finance/apis/v1/markets"


@dataclass(frozen=True, slots=True)
class BorosActiveMarket:
    market_id: int
    name: str
    symbol: str
    underlying: str
    is_spread_market: bool
    is_hyperliquid: bool
    maturity_ts: int
    days_to_maturity: float
    mark_apr_pct: float
    mid_apr_pct: float
    best_bid_apr_pct: float | None
    best_ask_apr_pct: float | None
    floating_apr_pct: float
    boros_bid_ask_apr_pct: float | None
    maturity_spread_cost_bps: float | None  # (ask - bid) * (ttm / 365) * 1e4 / 100
    fixed_minus_float_bid_pct: float | None  # best_bid - floating (positive = Boros pays MORE fixed than live float!)
    float_minus_fixed_ask_pct: float | None  # floating - best_ask (positive = cheap to pay fixed & earn higher float!)
    notional_oi: float
    volume_24h: float
    recommended_action: str
    lighter_live_apr_pct: float | None = None
    locked_hl_minus_lt_apr_pct: float | None = None
    is_actionable: bool = True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class BorosScannerSnapshot:
    observed_at_ms: int
    total_markets_scanned: int
    active_markets_count: int
    hl_and_spread_count: int
    markets: list[BorosActiveMarket]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "total_markets_scanned": self.total_markets_scanned,
            "active_markets_count": self.active_markets_count,
            "hl_and_spread_count": self.hl_and_spread_count,
            "markets": [m.to_dict() for m in self.markets],
        }


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


MIN_ACTIONABLE_VOLUME_24H_USD = 10_000.0  # < $10k traded in 24h -> quotes are indications, not executable size
MIN_ACTIONABLE_OI_USD = 100_000.0  # < $100k open interest -> too thin to hedge a real position
# Perp hedge round trip (HL taker ~4.5 bps in + out) amortized over the Boros term when judging a fixed lock.
DEFAULT_PERP_ROUND_TRIP_COST_BPS = 9.0


def _opt_pct(v: object) -> float | None:
    if v is None:
        return None
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return x * 100.0 if math.isfinite(x) else None


def parse_boros_market(
    raw: dict[str, Any],
    *,
    lighter_quotes: dict[str, cds.DexMarketQuote] | None = None,
    min_volume_24h_usd: float = MIN_ACTIONABLE_VOLUME_24H_USD,
    min_oi_usd: float = MIN_ACTIONABLE_OI_USD,
    perp_round_trip_cost_bps: float = DEFAULT_PERP_ROUND_TRIP_COST_BPS,
) -> BorosActiveMarket | None:
    """Parse one raw Boros API market JSON object into a normalized active market row.

    A fixed-rate "Lock" is only recommended when the executable Boros bid beats the live floating APR
    by more than the perp-hedge round-trip cost amortized over the remaining term.
    """
    d = raw.get("data") or {}
    ttm_sec = _f(d.get("timeToMaturity"))
    if ttm_sec <= 0:
        return None
    im = raw.get("imData") or {}
    md = raw.get("metadata") or {}
    name = str(im.get("name") or "")
    sym = str(im.get("symbol") or "")
    under = str(md.get("underlyingSymbol") or "").upper()
    is_spread = bool(md.get("isSpreadMarket"))
    is_hl = "hyperliquid" in name.lower() or "hyperliquid" in str(md.get("fundingRateSymbol") or "").lower()

    days = ttm_sec / 86_400.0
    mark_opt = _opt_pct(d.get("markApr"))
    mid_opt = _opt_pct(d.get("midApr"))
    float_opt = _opt_pct(d.get("floatingApr"))
    mark_pct = mark_opt if mark_opt is not None else (mid_opt if mid_opt is not None else 0.0)
    mid_pct = mid_opt if mid_opt is not None else mark_pct
    float_pct = float_opt if float_opt is not None else mid_pct
    bid_pct = _opt_pct(d.get("bestBid"))
    ask_pct = _opt_pct(d.get("bestAsk"))

    ba_apr = max(0.0, ask_pct - bid_pct) if (bid_pct is not None and ask_pct is not None) else None
    mat_cost_bps = (ba_apr * (days / 365.0) * 100.0) if ba_apr is not None else None  # 1% APR over 1 year = 100 bps
    fixed_minus_float = (bid_pct - float_pct) if bid_pct is not None else None
    float_minus_fixed = (float_pct - ask_pct) if ask_pct is not None else None

    if bid_pct is None and ask_pct is None:
        action = "No executable quotes (Monitor)"
    elif is_spread:
        if fixed_minus_float is not None and fixed_minus_float >= 2.0:
            action = "Short Boros Spread (Lock Fixed > Float)"
        elif float_minus_fixed is not None and float_minus_fixed >= 2.0:
            action = "Long Boros Spread (Pay Cheap Fixed)"
        else:
            action = "Watch Spread"
    else:
        hedge_cost_apr_pct = (perp_round_trip_cost_bps / 100.0) * (365.0 / days) if days > 0 else math.inf
        lock_beats_float = fixed_minus_float is not None and (fixed_minus_float - hedge_cost_apr_pct) > 0.0
        if bid_pct is not None and bid_pct >= 6.0 and days >= 7.0 and lock_beats_float:
            action = f"Short Perp + Short Boros (Lock {bid_pct:+.2f}%)"
        elif ask_pct is not None and ask_pct <= -10.0 and days >= 7.0:
            action = f"Long Perp + Long Boros (Lock {-ask_pct:+.2f}%)"
        elif float_minus_fixed is not None and float_minus_fixed >= 3.0:
            action = "Long Boros YU (Float > Ask)"
        else:
            action = "Monitor"

    # Never recommend trading a market nobody trades: quoted APRs on a zero-volume / dust-OI book
    # are stale indications, not executable prices.
    oi_usd = _f(d.get("notionalOI"))
    vol_usd = _f(d.get("volume24h"))
    has_liq_fields = "volume24h" in d or "notionalOI" in d  # absent fields are unknown, not zero
    illiquid = has_liq_fields and (vol_usd < min_volume_24h_usd or oi_usd < min_oi_usd)
    is_actionable = (not illiquid) and (bid_pct is not None or ask_pct is not None)
    if illiquid:
        action = f"Illiquid (vol24h ${vol_usd:,.0f}, OI ${oi_usd:,.0f}) - no action"

    lt_apr: float | None = None
    locked_vs_lt: float | None = None
    if not is_spread and is_hl and lighter_quotes and under in lighter_quotes:
        lt_apr = round(lighter_quotes[under].funding_apr_pct, 2)
        # If Short HL Perp + Long Lighter + Short Boros HL Fixed -> receive Boros bid fixed, pay Lighter float
        if bid_pct is not None:
            locked_vs_lt = round(bid_pct - lt_apr, 2)

    return BorosActiveMarket(
        market_id=int(raw.get("marketId") or 0),
        name=name,
        symbol=sym,
        underlying=under,
        is_spread_market=is_spread,
        is_hyperliquid=is_hl,
        maturity_ts=int(im.get("maturity") or 0),
        days_to_maturity=round(days, 1),
        mark_apr_pct=round(mark_pct, 2),
        mid_apr_pct=round(mid_pct, 2),
        best_bid_apr_pct=round(bid_pct, 2) if bid_pct is not None else None,
        best_ask_apr_pct=round(ask_pct, 2) if ask_pct is not None else None,
        floating_apr_pct=round(float_pct, 2),
        boros_bid_ask_apr_pct=round(ba_apr, 2) if ba_apr is not None else None,
        maturity_spread_cost_bps=round(mat_cost_bps, 2) if mat_cost_bps is not None else None,
        fixed_minus_float_bid_pct=round(fixed_minus_float, 2) if fixed_minus_float is not None else None,
        float_minus_fixed_ask_pct=round(float_minus_fixed, 2) if float_minus_fixed is not None else None,
        notional_oi=round(oi_usd, 2),
        volume_24h=round(vol_usd, 2),
        recommended_action=action,
        lighter_live_apr_pct=lt_apr,
        locked_hl_minus_lt_apr_pct=locked_vs_lt,
        is_actionable=is_actionable,
    )


def fetch_boros_active_markets(
    client: httpx.Client,
    *,
    markets_url: str = BOROS_MARKETS_URL,
    lighter_quotes: dict[str, cds.DexMarketQuote] | None = None,
    max_pages: int = 15,
    min_volume_24h_usd: float = MIN_ACTIONABLE_VOLUME_24H_USD,
    min_oi_usd: float = MIN_ACTIONABLE_OI_USD,
    perp_round_trip_cost_bps: float = DEFAULT_PERP_ROUND_TRIP_COST_BPS,
) -> tuple[int, list[BorosActiveMarket]]:
    """Paginate through Boros Open API via ``resumeToken`` and return all active markets."""
    token: str | None = None
    total_seen = 0
    active: list[BorosActiveMarket] = []
    for _ in range(max_pages):
        params: dict[str, Any] = {"limit": 100}
        if token:
            params["resumeToken"] = token
        r = client.get(markets_url, params=params)
        r.raise_for_status()
        body = r.json() or {}
        res = body.get("results") or []
        total_seen += len(res)
        for item in res:
            parsed = parse_boros_market(
                item,
                lighter_quotes=lighter_quotes,
                min_volume_24h_usd=min_volume_24h_usd,
                min_oi_usd=min_oi_usd,
                perp_round_trip_cost_bps=perp_round_trip_cost_bps,
            )
            if parsed is not None:
                active.append(parsed)
        token = body.get("resumeToken")
        if not token or not res:
            break
    return total_seen, active


def run_boros_fixed_funding_scan(
    client: httpx.Client | None = None,
    *,
    include_lighter_comparison: bool = True,
    markets_url: str = BOROS_MARKETS_URL,
    min_volume_24h_usd: float = MIN_ACTIONABLE_VOLUME_24H_USD,
    min_oi_usd: float = MIN_ACTIONABLE_OI_USD,
    perp_round_trip_cost_bps: float = DEFAULT_PERP_ROUND_TRIP_COST_BPS,
) -> BorosScannerSnapshot:
    """Scan all active Pendle Boros Hyperliquid & Spread markets."""
    now_ms = int(time.time() * 1000)
    own = client is None
    c = client or httpx.Client(timeout=20.0)
    try:
        lt_q: dict[str, cds.DexMarketQuote] = {}
        if include_lighter_comparison:
            try:
                lt_q = cds.fetch_lighter_quotes(c)
            except Exception:
                lt_q = {}
        total_seen, all_active = fetch_boros_active_markets(
            c,
            markets_url=markets_url,
            lighter_quotes=lt_q,
            min_volume_24h_usd=min_volume_24h_usd,
            min_oi_usd=min_oi_usd,
            perp_round_trip_cost_bps=perp_round_trip_cost_bps,
        )
    finally:
        if own:
            c.close()

    hl_and_spread = [m for m in all_active if m.is_hyperliquid or m.is_spread_market]
    hl_and_spread.sort(key=lambda m: (-int(m.days_to_maturity >= 5.0), -m.notional_oi))
    return BorosScannerSnapshot(
        observed_at_ms=now_ms,
        total_markets_scanned=total_seen,
        active_markets_count=len(all_active),
        hl_and_spread_count=len(hl_and_spread),
        markets=hl_and_spread,
    )


# --------------------------------------------------------------------------- Reporting


def format_boros_markdown(snap: BorosScannerSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Pendle Boros — Фиксация ставок фандинга Hyperliquid и Spread Markets ({ts})",
        "",
        f"- **Охват Boros Open API:** просканировано `{snap.total_markets_scanned}` рынков → активных (`TTM > 0`): **`{snap.active_markets_count}`**, из них Hyperliquid / Spread: **`{snap.hl_and_spread_count}`**.",
        "- **Зачем нужен Boros:** шорт `Yield Units` по `bestBid` превращает плавающий фандинг перпа Hyperliquid в **фиксированный APR до экспирации**, полностью снимая риск схлопывания ставки.",
        "",
        "## Активные рынки Hyperliquid и Spread Markets на Pendle Boros",
        "",
        "| ID | Рынок | До эксп. | Fixed Bid / Ask | Live Float | Disloc (Bid−Float) | Цена спреда | OI | Действие / Идея |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for m in snap.markets:
        spr_tag = " `[SPREAD]`" if m.is_spread_market else ""
        bid_s = f"**{m.best_bid_apr_pct:+.2f}%**" if m.best_bid_apr_pct is not None else "—"
        ask_s = f"{m.best_ask_apr_pct:+.2f}%" if m.best_ask_apr_pct is not None else "—"
        fmf_s = f"{m.fixed_minus_float_bid_pct:+.2f}%" if m.fixed_minus_float_bid_pct is not None else "—"
        cost_s = f"{m.maturity_spread_cost_bps:.1f} б.п." if m.maturity_spread_cost_bps is not None else "—"
        lines.append(
            f"| `{m.market_id}` | **{m.name}**{spr_tag} | {m.days_to_maturity:.1f}д | "
            f"{bid_s} / {ask_s} | {m.floating_apr_pct:+.2f}% | "
            f"{fmf_s} | {cost_s} | "
            f"{m.notional_oi:,.0f} | {m.recommended_action} |"
        )
    return "\n".join(lines) + "\n"


def format_boros_telegram_rich_html(snap: BorosScannerSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
    rows = [
        '<tr><th align="left">Рынок Boros</th><th align="right">Срок</th><th align="right">Fixed Bid</th><th align="right">Float</th><th align="right">Разрыв</th></tr>'
    ]
    for m in snap.markets[:6]:
        bid_h = f"<b>{m.best_bid_apr_pct:+.2f}%</b>" if m.best_bid_apr_pct is not None else "—"
        fmf_h = f"{m.fixed_minus_float_bid_pct:+.2f}%" if m.fixed_minus_float_bid_pct is not None else "—"
        rows.append(
            "<tr>"
            f'<td align="left"><b>{html.escape(m.name)}</b></td>'
            f'<td align="right">{m.days_to_maturity:.1f}д</td>'
            f'<td align="right">{bid_h}</td>'
            f'<td align="right">{m.floating_apr_pct:+.2f}%</td>'
            f'<td align="right">{fmf_h}</td>'
            "</tr>"
        )
    return (
        f"<h1>🔒 Pendle Boros Fixed Funding (Hyperliquid) · {html.escape(ts)}</h1>"
        f"<p><b>Активных рынков HL / Spread:</b> {snap.hl_and_spread_count} (всего активных {snap.active_markets_count})</p>"
        f'<table bordered striped>{"".join(rows)}</table>'
        "<footer>100% read-only · фиксация ставок фандинга Hyperliquid и Spread Markets до экспирации</footer>"
    )
