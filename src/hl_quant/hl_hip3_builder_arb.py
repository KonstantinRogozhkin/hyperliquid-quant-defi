"""Hyperliquid HIP-3 Builder-Perps Scanner & Cross-Builder L2 Arbitrage Engine.

100% read-only analytics over public Hyperliquid Info API (`perpDexs`, `metaAndAssetCtxs`,
`l2Book`) for permissionless HIP-3 builder perpetual DEXs (`xyz`, `io`, `para`, `mkts`, etc.):

1. **Zero-Bridge Cross-Builder Arbitrage** (`xyz` vs `io` vs `para`):
   - Matches identical underlying equities/commodities/indices across builders on HyperCore L1
     (e.g. `xyz:NVDA` vs `io:NVDA`, `xyz:NBIS` vs `io:NBIS`, `xyz:SKHX` vs `io:SKHX`).
   - Fetches real L2 orderbooks on BOTH builder DEXs and computes exact VWAP slippage and
     executable basis at `$5k` and `$25k` clips (no cross-chain bridge drag!).
   - Flags `onlyIsolated` margin constraints explicitly.
2. **24/7 TradFi Equities & Commodities Basis / Funding Monitor (`xyz:*`)**:
   - Tracks mark-vs-oracle basis (bps), hourly funding APR, open interest, and 24h volume on
     liquid Trade.xyz markets (`xyz:NVDA`, `xyz:SP500`, `xyz:XYZ100`, `xyz:GOLD`, `xyz:SOXL`).
   - Classifies the live New York TradFi session (`NY_REGULAR`, `US_OVERNIGHT`, `US_WEEKEND`)
     to highlight weekend/after-hours oracle-freeze funding & basis dislocations.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import math
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from hl_quant import cross_dex_scanner as cds

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HOURS_PER_YEAR = 8760.0
NY_TZ = ZoneInfo("America/New_York")


@dataclass(frozen=True, slots=True)
class Hip3BuilderMarket:
    dex: str
    dex_full_name: str
    full_symbol: str  # e.g. "xyz:NVDA"
    base_symbol: str  # e.g. "NVDA"
    mark_px: float
    oracle_px: float
    basis_vs_oracle_bps: float
    funding_rate_h: float
    funding_apr_pct: float
    day_vol_usd: float
    oi_usd: float
    only_isolated: bool
    max_leverage: int
    growth_mode: bool = False
    deployer_fee_scale: float = 1.0
    est_taker_fee_bps: float = 4.5
    est_maker_fee_bps: float = 1.5
    last_growth_change_time: str | None = None
    growth_mode_lock_days_left: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Hip3CrossBuilderOpp:
    base_symbol: str
    short_symbol: str  # builder leg with higher funding (receive funding by shorting)
    long_symbol: str  # builder leg with lower funding
    short_dex: str
    long_dex: str
    short_apr_pct: float
    long_apr_pct: float
    spread_apr_pct: float
    mark_basis_bps: float  # (short_mark - long_mark) / long_mark * 1e4 (positive = favorable)
    short_vol_usd: float
    long_vol_usd: float
    short_oi_usd: float
    long_oi_usd: float
    any_only_isolated: bool
    both_growth_mode: bool = False
    oracle_divergence_bps: float = 0.0  # |short_oracle - long_oracle| / long_oracle * 1e4
    oracle_risk_warning: bool = False  # True when deployer oracles diverge > 15 bps
    est_rt_maker_fee_bps: float = 6.0
    est_rt_taker_fee_bps: float = 18.0
    exec_basis_5k_bps: float | None = None
    l2_slip_5k_bps: float | None = None
    l2_slip_25k_bps: float | None = None
    depth_5k_ok: bool = False
    depth_25k_ok: bool = False
    payback_hours_5k: float | None = None
    payback_hours_5k_taker: float | None = None
    independent_dex_margin: bool = True  # HIP-3 DEXs clear & margin independently (no cross-DEX netting)
    net_apr_split_margin_pct: float | None = None  # Effective APR on total equity when margin is split 50/50 across 2 DEXs
    min_growth_lock_days_left: float | None = None  # Min remaining days of 30d Growth Mode cooldown

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class Hip3ScannerSnapshot:
    observed_at_ms: int
    ny_session: str  # NY_REGULAR, US_PREMARKET, US_AFTERHOURS, US_OVERNIGHT, US_WEEKEND
    builders_discovered: int
    active_markets_count: int
    total_hip3_day_vol_usd: float
    cross_builder_opps: list[Hip3CrossBuilderOpp]
    top_tradfi_markets: list[Hip3BuilderMarket]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "ny_session": self.ny_session,
            "builders_discovered": self.builders_discovered,
            "active_markets_count": self.active_markets_count,
            "total_hip3_day_vol_usd": self.total_hip3_day_vol_usd,
            "cross_builder_opps": [o.to_dict() for o in self.cross_builder_opps],
            "top_tradfi_markets": [m.to_dict() for m in self.top_tradfi_markets],
        }


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def compute_growth_mode_lock_days_left(
    change_time_iso: str | None,
    *,
    now_ms: int | None = None,
    cooldown_days: float = 30.0,
) -> float | None:
    """Compute remaining days in HIP-3's 30-day Growth Mode / fee-scale cooldown window."""
    if not change_time_iso:
        return None
    try:
        raw = str(change_time_iso).strip().replace("Z", "+00:00")
        # Truncate nanoseconds to microseconds for Python fromisoformat
        if "." in raw:
            head, tail = raw.split(".", 1)
            tz_part = ""
            for sep in ("+", "-"):
                if sep in tail:
                    frac, tz_part = tail.split(sep, 1)
                    tz_part = sep + tz_part
                    break
            else:
                frac = tail
            raw = f"{head}.{frac[:6]}{tz_part}"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now_dt = datetime.fromtimestamp((now_ms if now_ms is not None else time.time() * 1000) / 1000.0, tz=timezone.utc)
        elapsed_days = (now_dt - dt).total_seconds() / 86400.0
        return round(max(0.0, cooldown_days - elapsed_days), 1)
    except Exception:
        return None


def classify_ny_tradfi_session(ts_ms: int) -> str:
    """Classify timestamp into US equity market session (America/New_York)."""
    dt = datetime.fromtimestamp(ts_ms / 1000.0, tz=NY_TZ)
    if dt.weekday() >= 5:
        return "US_WEEKEND"
    hm = dt.hour * 60 + dt.minute
    if 9 * 60 + 30 <= hm < 16 * 60:
        return "NY_REGULAR"
    if 4 * 60 <= hm < 9 * 60 + 30:
        return "US_PREMARKET"
    if 16 * 60 <= hm < 20 * 60:
        return "US_AFTERHOURS"
    return "US_OVERNIGHT"


def fetch_hip3_builders_and_markets(
    client: httpx.Client,
    *,
    info_url: str = HL_INFO_URL,
    min_day_vol_usd: float = 0.0,
) -> tuple[int, list[Hip3BuilderMarket]]:
    """Discover all HIP-3 builder DEXs via ``perpDexs`` and collect active market contexts."""
    r = client.post(info_url, json={"type": "perpDexs"})
    r.raise_for_status()
    raw_dexs = r.json() or []
    builders: list[tuple[str, str]] = []
    for item in raw_dexs:
        if isinstance(item, dict) and item.get("name"):
            builders.append((str(item["name"]), str(item.get("fullName") or item["name"])))

    markets: list[Hip3BuilderMarket] = []
    for dex_name, full_name in builders:
        try:
            rm = client.post(info_url, json={"type": "metaAndAssetCtxs", "dex": dex_name})
            if rm.status_code != 200:
                continue
            payload = rm.json()
            if not isinstance(payload, list) or len(payload) < 2:
                continue
            meta, ctxs = payload[0], payload[1]
            for u, x in zip((meta or {}).get("universe") or [], ctxs or [], strict=False):
                if u.get("isDelisted"):
                    continue
                full_sym = str(u.get("name") or "").strip()
                if not full_sym:
                    continue
                base = full_sym.split(":", 1)[-1].upper()
                mark = _f(x.get("markPx"))
                ora = _f(x.get("oraclePx"), mark)
                if mark <= 0:
                    continue
                vol = _f(x.get("dayNtlVlm"))
                if vol < min_day_vol_usd:
                    continue
                oi = _f(x.get("openInterest")) * mark
                fund_h = _f(x.get("funding"))
                basis_bps = ((mark - ora) / ora * 1e4) if ora > 0 else 0.0
                only_iso = bool(u.get("onlyIsolated")) or str(u.get("marginMode") or "") in ("strictIsolated", "noCross")
                is_growth = str(u.get("growthMode") or "").lower() == "enabled"
                fee_scale = max(0.0, _f(u.get("deployerFeeScale"), 1.0))
                last_gm_ts = u.get("lastGrowthModeChangeTime") or u.get("lastFeeScaleChangeTime")
                gm_lock_left = compute_growth_mode_lock_days_left(str(last_gm_ts)) if (is_growth and last_gm_ts) else None
                # HIP-3 Growth Mode reduces base fees by >= 90%: Tier-0 taker ~0.45-0.90 bps, maker ~0.15-0.30 bps
                taker_bps = round((0.45 * (1.0 + fee_scale)) if is_growth else (4.5 * max(1.0, fee_scale)), 2)
                maker_bps = round((0.15 * (1.0 + fee_scale)) if is_growth else (1.5 * max(1.0, fee_scale)), 2)
                markets.append(
                    Hip3BuilderMarket(
                        dex=dex_name,
                        dex_full_name=full_name,
                        full_symbol=full_sym,
                        base_symbol=base,
                        mark_px=mark,
                        oracle_px=ora,
                        basis_vs_oracle_bps=round(basis_bps, 2),
                        funding_rate_h=fund_h,
                        funding_apr_pct=round(fund_h * HOURS_PER_YEAR * 100.0, 2),
                        day_vol_usd=round(vol, 2),
                        oi_usd=round(oi, 2),
                        only_isolated=only_iso,
                        max_leverage=int(u.get("maxLeverage") or 0),
                        growth_mode=is_growth,
                        deployer_fee_scale=fee_scale,
                        est_taker_fee_bps=taker_bps,
                        est_maker_fee_bps=maker_bps,
                        last_growth_change_time=str(last_gm_ts) if last_gm_ts else None,
                        growth_mode_lock_days_left=gm_lock_left,
                    )
                )
        except Exception:
            continue
    return len(builders), markets


def scan_hip3_cross_builder_arb(
    markets: list[Hip3BuilderMarket],
    *,
    min_leg_vol_usd: float = 200_000.0,
    min_spread_apr_pct: float = 10.0,
    round_trip_fee_bps: float = 6.0,  # 2 legs × 2 sides × 1.5 bps Tier-0 maker (zero bridge!)
    book_fetcher: Any = None,
    enrich_top_n: int = 12,
) -> list[Hip3CrossBuilderOpp]:
    """Identify same-chain HIP-3 cross-builder funding arbitrage pairs and verify L2 VWAP depth."""
    by_base: dict[str, list[Hip3BuilderMarket]] = {}
    for m in markets:
        if m.day_vol_usd >= min_leg_vol_usd and m.mark_px > 0:
            by_base.setdefault(m.base_symbol, []).append(m)

    prelim: list[Hip3CrossBuilderOpp] = []
    for base, legs in by_base.items():
        if len(legs) < 2:
            continue
        legs_sorted = sorted(legs, key=lambda x: -x.funding_apr_pct)
        hi, lo = legs_sorted[0], legs_sorted[-1]
        if hi.dex == lo.dex:
            continue
        # Guard symbol collision: mark prices must agree within 10%
        if not (0.90 <= hi.mark_px / lo.mark_px <= 1.10):
            continue
        spread = hi.funding_apr_pct - lo.funding_apr_pct
        if spread < min_spread_apr_pct:
            continue
        mark_basis = (hi.mark_px - lo.mark_px) / lo.mark_px * 1e4
        ora_div_bps = (abs(hi.oracle_px - lo.oracle_px) / lo.oracle_px * 1e4) if lo.oracle_px > 0 else 0.0
        rt_maker = round(2.0 * (hi.est_maker_fee_bps + lo.est_maker_fee_bps), 2)
        rt_taker = round(2.0 * (hi.est_taker_fee_bps + lo.est_taker_fee_bps), 2)
        locks = [d for d in (hi.growth_mode_lock_days_left, lo.growth_mode_lock_days_left) if d is not None]
        min_lock = min(locks) if locks else None
        # Each HIP-3 DEX clears & margins independently (no cross-DEX netting). At 2x per-leg
        # margin (50% collateral per leg => 1.0x total equity per $1 notional on each leg -> 50% cap eff.):
        net_apr_split = round(spread * 0.5, 2)
        prelim.append(
            Hip3CrossBuilderOpp(
                base_symbol=base,
                short_symbol=hi.full_symbol,
                long_symbol=lo.full_symbol,
                short_dex=hi.dex,
                long_dex=lo.dex,
                short_apr_pct=hi.funding_apr_pct,
                long_apr_pct=lo.funding_apr_pct,
                spread_apr_pct=round(spread, 2),
                mark_basis_bps=round(mark_basis, 2),
                short_vol_usd=hi.day_vol_usd,
                long_vol_usd=lo.day_vol_usd,
                short_oi_usd=hi.oi_usd,
                long_oi_usd=lo.oi_usd,
                any_only_isolated=bool(hi.only_isolated or lo.only_isolated),
                both_growth_mode=bool(hi.growth_mode and lo.growth_mode),
                oracle_divergence_bps=round(ora_div_bps, 2),
                oracle_risk_warning=ora_div_bps > 15.0,
                est_rt_maker_fee_bps=rt_maker,
                est_rt_taker_fee_bps=rt_taker,
                independent_dex_margin=True,
                net_apr_split_margin_pct=net_apr_split,
                min_growth_lock_days_left=min_lock,
            )
        )

    prelim.sort(key=lambda o: (-o.spread_apr_pct, -min(o.short_vol_usd, o.long_vol_usd)))
    if book_fetcher is None:
        return prelim

    enriched: list[Hip3CrossBuilderOpp] = []
    for idx, opp in enumerate(prelim):
        if idx >= enrich_top_n:
            enriched.append(opp)
            continue
        sb, sa = book_fetcher(opp.short_symbol)
        lb, la = book_fetcher(opp.long_symbol)
        eb5, sl5, ok5 = cds.compute_executable_cross_dex_entry_bps(sb, sa, lb, la, short_hl=True, clip_usd=5_000.0)
        _, sl25, ok25 = cds.compute_executable_cross_dex_entry_bps(sb, sa, lb, la, short_hl=True, clip_usd=25_000.0)
        be_h: float | None = None
        be_h_taker: float | None = None
        if sl5 is not None and eb5 is not None:
            # Always respect per-builder deployerFeeScale in est_rt_maker_fee_bps (not only in Growth Mode).
            # eb5 already prices entry VWAP slippage, so adding 1x sl5 accounts for estimated exit slippage without double-counting.
            rt_m = opp.est_rt_maker_fee_bps if opp.est_rt_maker_fee_bps > 0 else round_trip_fee_bps
            net_cost_m = max(0.0, rt_m + sl5 - eb5)
            net_cost_t = max(0.0, opp.est_rt_taker_fee_bps + sl5 - eb5)
            hourly_bps = opp.spread_apr_pct / (HOURS_PER_YEAR * 100.0) * 1e4
            if hourly_bps > 1e-9:
                be_h = round(net_cost_m / hourly_bps, 1)
                be_h_taker = round(net_cost_t / hourly_bps, 1)
        enriched.append(
            Hip3CrossBuilderOpp(
                base_symbol=opp.base_symbol,
                short_symbol=opp.short_symbol,
                long_symbol=opp.long_symbol,
                short_dex=opp.short_dex,
                long_dex=opp.long_dex,
                short_apr_pct=opp.short_apr_pct,
                long_apr_pct=opp.long_apr_pct,
                spread_apr_pct=opp.spread_apr_pct,
                mark_basis_bps=opp.mark_basis_bps,
                short_vol_usd=opp.short_vol_usd,
                long_vol_usd=opp.long_vol_usd,
                short_oi_usd=opp.short_oi_usd,
                long_oi_usd=opp.long_oi_usd,
                any_only_isolated=opp.any_only_isolated,
                both_growth_mode=opp.both_growth_mode,
                oracle_divergence_bps=opp.oracle_divergence_bps,
                oracle_risk_warning=opp.oracle_risk_warning,
                est_rt_maker_fee_bps=opp.est_rt_maker_fee_bps,
                est_rt_taker_fee_bps=opp.est_rt_taker_fee_bps,
                exec_basis_5k_bps=eb5,
                l2_slip_5k_bps=sl5,
                l2_slip_25k_bps=sl25,
                depth_5k_ok=ok5,
                depth_25k_ok=ok25,
                payback_hours_5k=be_h,
                payback_hours_5k_taker=be_h_taker,
                independent_dex_margin=opp.independent_dex_margin,
                net_apr_split_margin_pct=opp.net_apr_split_margin_pct,
                min_growth_lock_days_left=opp.min_growth_lock_days_left,
            )
        )
    return enriched


def run_hip3_builder_scan(
    client: httpx.Client | None = None,
    *,
    min_leg_vol_usd: float = 200_000.0,
    min_spread_apr_pct: float = 10.0,
    top_tradfi_n: int = 12,
    info_url: str = HL_INFO_URL,
) -> Hip3ScannerSnapshot:
    """Run full live read-only HIP-3 builder & cross-builder L2 arbitrage scan."""
    now_ms = int(time.time() * 1000)
    own = client is None
    c = client or httpx.Client(timeout=20.0)
    try:
        n_builders, markets = fetch_hip3_builders_and_markets(c, info_url=info_url, min_day_vol_usd=0.0)
        tot_vol = sum(m.day_vol_usd for m in markets)

        def _fetch_book(sym: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
            return cds.fetch_dex_l2_book(c, "hyperliquid", sym)

        opps = scan_hip3_cross_builder_arb(
            markets,
            min_leg_vol_usd=min_leg_vol_usd,
            min_spread_apr_pct=min_spread_apr_pct,
            book_fetcher=_fetch_book,
            enrich_top_n=12,
        )
        top_tradfi = sorted(
            [m for m in markets if m.day_vol_usd >= 5_000_000.0],
            key=lambda m: (-abs(m.funding_apr_pct) if abs(m.funding_apr_pct) >= 15.0 else 0.0, -m.day_vol_usd),
        )[:top_tradfi_n]
    finally:
        if own:
            c.close()

    return Hip3ScannerSnapshot(
        observed_at_ms=now_ms,
        ny_session=classify_ny_tradfi_session(now_ms),
        builders_discovered=n_builders,
        active_markets_count=len(markets),
        total_hip3_day_vol_usd=round(tot_vol, 2),
        cross_builder_opps=opps,
        top_tradfi_markets=top_tradfi,
    )


# --------------------------------------------------------------------------- Reporting


def format_hip3_arb_markdown(snap: Hip3ScannerSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Hyperliquid HIP-3 Builder-Perps & Кросс-билдер Арбитраж ({ts})",
        "",
        f"- **Охват HIP-3:** `{snap.builders_discovered}` билдер-DEX (`xyz`, `io`, `para`, `mkts` и др.), `{snap.active_markets_count}` активных рынков, суммарный оборот **${snap.total_hip3_day_vol_usd/1e9:,.2f}B / 24ч**.",
        f"- **Сессия Wall Street (NY):** `{snap.ny_session}` (вне `NY_REGULAR` оракул акций стоит/замедляется — главная зона weekend/overnight перекосов).",
        "- **Преимущество и риски кросс-билдер арба:** обе ноги на HyperCore L1 (**нулевой риск моста**); в **`Growth Mode`** базовый тейкер падает до `0.45–0.90 б.п.` (мейкер `0.15–0.30 б.п.`, но вклад объёма в L1 API-квоту снижен на `-90%` -> `1 req / 10 USDC`), а **`Oracle Div`** защищает от сбоя оракула деплоера (кейс SK Hynix `28 Jul 2026`). **Важно:** каждый HIP-3 DEX (`xyz` / `io`) имеет независимое маржирование (без кросс-неттинга) и право деплоера на `haltTrading` — держите раздельный буфер маржи на обеих площадках.",
        "",
        "## 1. Внутрибиржевой кросс-билдер арбитраж HIP-3 (L2 VWAP `$5k / $25k` · Growth Mode & Oracle Guard)",
        "",
        "| Тикер | Short / Long (HIP-3) | Спред APR | Growth | Oracle Div | Exec Basis $5k | Slip $5k / $25k | Окуп. Maker / Taker | Изол. |",
        "|---|---|---:|---|---:|---:|---:|---:|---|",
    ]
    for o in snap.cross_builder_opps[:12]:
        eb = f"{o.exec_basis_5k_bps:+.1f} б.п." if o.exec_basis_5k_bps is not None else "—"
        s5 = f"{o.l2_slip_5k_bps:.1f}" if o.l2_slip_5k_bps is not None else "—"
        s25 = f"{o.l2_slip_25k_bps:.1f}" if o.l2_slip_25k_bps is not None else "—"
        be_m = f"{o.payback_hours_5k:.0f}ч" if o.payback_hours_5k is not None else "—"
        be_t = f"{o.payback_hours_5k_taker:.0f}ч" if o.payback_hours_5k_taker is not None else "—"
        gm = "⚡ GM" if o.both_growth_mode else "Std"
        od_txt = f"⚠️ {o.oracle_divergence_bps:.1f} б.п." if o.oracle_risk_warning else f"{o.oracle_divergence_bps:.1f} б.п."
        iso = "⚠️ Iso" if o.any_only_isolated else "Cross"
        lines.append(
            f"| **{o.base_symbol}** | `S {o.short_symbol}` / `L {o.long_symbol}` | **+{o.spread_apr_pct:.1f}%** | "
            f"{gm} | {od_txt} | {eb} | {s5} / {s25} б.п. | {be_m} / {be_t} | {iso} |"
        )
    if not snap.cross_builder_opps:
        lines.append("| — | Нет кросс-билдер спредов ≥ 10% APR | — | — | — | — | — | — | — |")

    lines += [
        "",
        "## 2. Топ 24/7 TradFi-рынков HIP-3 (Базис к оракулу, Growth Mode и фандинг)",
        "",
        "| Рынок | Mark / Oracle | Базис к оракулу | Фандинг APR | Growth / Taker | Объём 24ч | OI | Режим |",
        "|---|---:|---:|---:|---|---:|---:|---|",
    ]
    for m in snap.top_tradfi_markets:
        iso = "onlyIsolated" if m.only_isolated else f"cross:{m.max_leverage}x"
        gm_txt = f"⚡ GM ({m.est_taker_fee_bps:.2f}bp)" if m.growth_mode else f"Std ({m.est_taker_fee_bps:.1f}bp)"
        lines.append(
            f"| **{m.full_symbol}** | {m.mark_px:,.4g} / {m.oracle_px:,.4g} | **{m.basis_vs_oracle_bps:+.1f} б.п.** | "
            f"**{m.funding_apr_pct:+.1f}%** | {gm_txt} | ${m.day_vol_usd/1e6:,.1f}M | ${m.oi_usd/1e6:,.1f}M | `{iso}` |"
        )
    return "\n".join(lines) + "\n"


def format_hip3_arb_telegram_rich_html(snap: Hip3ScannerSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
    rows = [
        '<tr><th align="left">Тикер</th><th align="left">Ноги HIP-3</th><th align="right">Спред APR</th><th align="right">Slip $5k</th><th align="right">Окуп.</th></tr>'
    ]
    for o in snap.cross_builder_opps[:6]:
        s5 = f"{o.l2_slip_5k_bps:.1f} б.п." if o.l2_slip_5k_bps is not None else "—"
        be = f"{o.payback_hours_5k:.0f}ч" if o.payback_hours_5k is not None else "—"
        rows.append(
            "<tr>"
            f'<td align="left"><b>{html.escape(o.base_symbol)}</b></td>'
            f'<td align="left">S {html.escape(o.short_dex)} / L {html.escape(o.long_dex)}</td>'
            f'<td align="right"><b>+{o.spread_apr_pct:.1f}%</b></td>'
            f'<td align="right">{s5}</td>'
            f'<td align="right">{be}</td>'
            "</tr>"
        )
    return (
        f"<h1>🏛️ HIP-3 Builder-Perps &amp; Кросс-билдер Арб · {html.escape(ts)}</h1>"
        f"<p><b>Билдеров:</b> {snap.builders_discovered} ({snap.active_markets_count} рынков, "
        f"${snap.total_hip3_day_vol_usd/1e9:,.2f}B/24ч) · Сессия NY: <code>{html.escape(snap.ny_session)}</code></p>"
        f'<table bordered striped>{"".join(rows)}</table>'
        "<footer>100% read-only · внутрибиржевой арбитраж HyperCore L1 без риска мостов</footer>"
    )
