"""Native Single-Venue HyperCore Spot + Perp Funding Arb + 10% HLP Reserve Engine.

Inspired by Neutral Trade's flagship *Hyperliquid Funding Arb* strategy (zero bridges,
zero CEX counterparty risk, ~1.3x–1.5x low leverage, 10% USDC reserve in HLP Vault),
upgraded with HyperCore L2 depth VWAP pricing, proxy-hedge basis guardrails (e.g. XAUT0/PAXG),
optional Pendle Boros fixed-rate lock discovery, automatic margin-drift rebalancing,
bounded NAV sanity checks, and live Risk & Exposure telemetry (30D avg/max leverage,
venue concentration, and market-cap tier breakdown).

100% read-only against Mainnet public APIs; zero live orders placed.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx
import pandas as pd

from hl_quant import cross_dex_scanner as cds
from hl_quant import hl_boros_fixed_funding as boros
from hl_quant import hl_hyperevm_defi as evm
from hl_quant import persistence_clock as pc
from hl_quant.persistence_clock import advance_streak, streak_count, update_streak_map

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HOURS_PER_YEAR = 8760.0
# A NAV-guard clamp is confirmed (accepted) by a tick at least this much later.
NAV_GUARD_CONFIRM_MS = 3_600_000

# Canonical HyperCore spot token -> HyperCore perpetual symbol aliases.
# Unit bridge tokens (UBTC, UETH, USOL, ...) represent 1:1 native assets on HyperCore.
SPOT_TO_PERP_ALIASES: dict[str, str] = {
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
    "UZEC": "ZEC",
    "UPUMP": "PUMP",
    "UFART": "FARTCOIN",
    "UXPL": "XPL",
    "UBONK": "kBONK",
    "UENA": "ENA",
    "USUI": "SUI",
    "UAAVE": "AAVE",
    "ULINK": "LINK",
    "FXRP": "XRP",
    "ONEAR": "NEAR",
    "HXMR": "XMR",
    "XMR2": "XMR",
    "XAUT0": "PAXG",
    "PAXG": "PAXG",
}

# Cross-issuer proxy hedges carry issuer/peg spread risk (treated with stricter basis limits & half size).
PROXY_HEDGE_PAIRS: set[tuple[str, str]] = {
    ("XAUT0", "PAXG"),
    ("XMR2", "XMR"),
}

# Market-cap / asset-class classification for Risk & Exposure telemetry.
LARGE_CAP_COINS: set[str] = {"BTC", "ETH", "SOL", "XRP", "HYPE", "BNB", "ADA", "DOGE", "AVAX", "LINK", "SUI"}
MID_CAP_COINS: set[str] = {"ZEC", "XMR", "NEAR", "AAVE", "ENA", "TAO", "UNI", "LTC", "ARB", "OP", "APT"}
GOLD_RWA_COINS: set[str] = {"PAXG", "XAUT", "XAUT0"}


def classify_asset_tier(perp_coin: str) -> str:
    """Classify perp underlying into Neutral-style market-cap / asset-class risk tiers."""
    c = perp_coin.upper()
    if c in GOLD_RWA_COINS:
        return "Gold RWA"
    if c in LARGE_CAP_COINS:
        return "Crypto - Large Cap"
    if c in MID_CAP_COINS:
        return "Crypto - Mid Cap"
    return "Crypto - Small/Meme"


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    tmp.replace(path)


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


@dataclass(frozen=True, slots=True)
class SpotPerpOpportunity:
    base_symbol: str  # HyperCore perp symbol (e.g. "BTC", "HYPE", "PAXG")
    spot_pair: str  # HyperCore spot l2Book coin key (e.g. "@142", "@107", "PURR/USDC")
    spot_token: str  # Spot token symbol (e.g. "UBTC", "HYPE", "XAUT0")
    quote_token: str
    asset_tier: str
    is_proxy_hedge: bool
    only_isolated: bool
    max_leverage: int
    spot_mid_px: float
    perp_mark_px: float
    perp_oracle_px: float
    mark_basis_bps: float  # (perp_mark - spot_mid) / spot_mid * 1e4
    funding_rate_h: float
    funding_apr_pct: float
    spot_day_vol_usd: float
    perp_day_vol_usd: float
    perp_oi_usd: float
    entry_l2_slip_bps: float | None
    exec_basis_bps: float | None  # positive = sell perp above spot ask (favorable entry!)
    l2_full_fill_ok: bool
    one_way_fee_bps: float  # spot_fee + perp_fee for entering one unit of hedged notional
    capital_efficiency_pct: float  # L / (L + 1) * 100
    net_deposit_apr_pct: float  # funding_apr * cap_eff
    boros_fixed_apr_pct: float | None
    payback_hours: float | None
    eligible: bool
    reject_reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class SpotPerpScanSnapshot:
    observed_at_ms: int
    observed_at_utc: str
    hlp_apr_pct: float
    hlp_tvl_usd: float
    clip_usd: float
    target_perp_leverage: float
    use_maker_fees: bool
    opportunities: list[SpotPerpOpportunity]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "observed_at_utc": self.observed_at_utc,
            "hlp_apr_pct": self.hlp_apr_pct,
            "hlp_tvl_usd": self.hlp_tvl_usd,
            "clip_usd": self.clip_usd,
            "target_perp_leverage": self.target_perp_leverage,
            "use_maker_fees": self.use_maker_fees,
            "opportunities": [o.to_dict() for o in self.opportunities],
        }


@dataclass(frozen=True, slots=True)
class SpotPerpPaperConfig:
    starting_capital_usd: float = 100_000.0
    hlp_reserve_share: float = 0.10  # 10% of equity deployed to HLP Vault (Neutral Trade benchmark)
    max_hlp_reserve_share: float = 0.15  # Hard risk cap on HLP reserve
    min_cash_buffer_share: float = 0.05  # Keep >= 5% free USDC for margin top-ups
    max_slots: int = 6
    slot_equity_usd: float = 14_000.0  # Total capital per full slot (split into spot + perp margin)
    proxy_slot_scale: float = 0.5  # Half-size clip on cross-issuer proxies (e.g. XAUT0 -> PAXG)
    isolated_slot_scale: float = 0.5  # Half-size clip on onlyIsolated perp markets
    target_perp_leverage: float = 2.0  # cap_eff = 2/3 -> portfolio gross leverage ~1.2x-1.4x
    max_perp_leverage: float = 3.5  # Rebalance deadband trigger if rally pushes perp lev > 3.5x
    min_spot_vol_usd: float = 200_000.0
    min_perp_vol_usd: float = 1_000_000.0
    entry_apr_pct: float = 8.0  # Enter when positive funding >= 8% APR
    exit_apr_pct: float = 2.0  # Hysteresis exit if funding stays below 2% APR
    hard_flip_apr_pct: float = -15.0  # Immediate exit only on deep negative funding flip (mild <-5% uses streak confirm)
    exit_confirm_ticks: int = 2  # Consecutive low-APR ticks before soft exit
    min_clean_streak: int = 2  # Consecutive eligible ticks before entry
    # Wall-clock spacing between streak increments: manual re-runs / worker restarts inside the
    # 4h cron window must not advance entry or exit gates (prevents minute-scale fee churn).
    min_streak_spacing_hours: float = 3.5
    min_hold_hours_before_soft_exit: float = 8.0  # soft funding-decay exits wait >= 8h after entry
    # Opportunity-cost hurdle: a slot must out-earn parking the same capital in HLP
    # (net_deposit_apr >= hlp_apr * hurdle_mult); otherwise skip the entry and its fees.
    require_hlp_hurdle: bool = True
    hlp_hurdle_mult: float = 1.0
    max_l2_slip_bps: float = 10.0
    min_exec_basis_bps: float = -12.0
    max_proxy_basis_bps: float = 120.0
    max_payback_hours: float = 168.0  # Max 7 days round-trip payback
    use_maker_fees: bool = True  # Realistic ALO execution (spot 4.0 + perp 1.5 = 5.5 bps one-way)
    spot_maker_fee_bps: float = 4.0
    perp_maker_fee_bps: float = 1.5
    spot_taker_fee_bps: float = 7.0
    perp_taker_fee_bps: float = 4.5
    allow_proxy_hedges: bool = True
    allow_only_isolated: bool = False
    max_missing_ticks: int = 3
    max_tick_nav_jump_pct: float = 5.0  # Bounded NAV sanity guard (Neutral Trade circuit breaker)
    default_hlp_apr_pct: float = 12.0  # Fallback only if HLP API is momentarily unreachable

    @property
    def one_way_fee_bps(self) -> float:
        if self.use_maker_fees:
            return self.spot_maker_fee_bps + self.perp_maker_fee_bps
        return self.spot_taker_fee_bps + self.perp_taker_fee_bps

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- Live Readers & Scanner


def fetch_hlp_vault_summary(
    client: httpx.Client,
    *,
    vault_address: str = evm.HLP_VAULT_ADDRESS,
    info_url: str = HL_INFO_URL,
) -> tuple[float, float]:
    """Fetch live HLP parent vault ``(apr_pct, tvl_usd)`` with a single lightweight POST."""
    r = client.post(info_url, json={"type": "vaultDetails", "vaultAddress": vault_address})
    r.raise_for_status()
    vd = r.json() or {}
    if not isinstance(vd, dict):
        return 0.0, 0.0
    apr_pct = round(_f(vd.get("apr")) * 100.0, 2)
    tvl_usd = 0.0
    for p_name, p_data in vd.get("portfolio") or []:
        if p_name == "allTime":
            hist = (p_data or {}).get("accountValueHistory") or []
            if hist:
                tvl_usd = round(_f(hist[-1][1]), 2)
            break
    return apr_pct, tvl_usd


def fetch_boros_hl_fixed_rates(client: httpx.Client) -> dict[str, float]:
    """Optional lookup of live Pendle Boros fixed-bid APRs (%) by underlying for HL perps."""
    out: dict[str, float] = {}
    try:
        _, mkts = boros.fetch_boros_active_markets(client)
        for m in mkts:
            if not (
                m.is_hyperliquid
                and not m.is_spread_market
                and m.days_to_maturity >= 5.0
                and m.is_actionable
            ):
                continue
            if m.best_bid_apr_pct is None:
                continue
            u = m.underlying.upper()
            if u not in out or m.best_bid_apr_pct > out[u]:
                out[u] = round(float(m.best_bid_apr_pct), 2)
    except Exception:
        return {}
    return out


def fetch_matched_spot_perp_markets(
    client: httpx.Client,
    *,
    info_url: str = HL_INFO_URL,
    min_spot_vol_usd: float = 10_000.0,
    include_bases: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Fetch HyperCore USDC spot pairs matched cleanly via ``ctx['coin']`` to HyperCore perps.

    Note: ``spotMetaAndAssetCtxs`` universe and ctx lists are NOT index-aligned (ctxs include
    delisted/historical pairs), so matching MUST join on ``ctx['coin'] == u['name']``.
    Currently held ``include_bases`` bypass the ``min_spot_vol_usd`` and ``[0.90, 1.10]`` parity
    filters so depegged or illiquid open positions are accurately marked to market and closed.
    """
    held_bases = set(include_bases or set())
    held_upper = {b.upper() for b in held_bases}
    p_resp = client.post(info_url, json={"type": "metaAndAssetCtxs"})
    p_resp.raise_for_status()
    p_meta, p_ctxs = p_resp.json()
    perps: dict[str, dict[str, Any]] = {}
    for u, ctx in zip(p_meta.get("universe") or [], p_ctxs or [], strict=False):
        if u.get("isDelisted"):
            continue
        coin = str(u.get("name") or "").strip()
        mark = _f(ctx.get("markPx"))
        if not coin or mark <= 0:
            continue
        perps[coin] = {
            "coin": coin,
            "mark_px": mark,
            "oracle_px": _f(ctx.get("oraclePx"), mark),
            "funding_rate_h": _f(ctx.get("funding")),
            "day_vol_usd": _f(ctx.get("dayNtlVlm")),
            "oi_usd": _f(ctx.get("openInterest")) * mark,
            "only_isolated": bool(u.get("onlyIsolated", False))
            or str(u.get("marginMode") or "") in {"strictIsolated", "noCross", "onlyIsolated"},
            "max_leverage": int(u.get("maxLeverage") or 10),
        }

    s_resp = client.post(info_url, json={"type": "spotMetaAndAssetCtxs"})
    s_resp.raise_for_status()
    s_meta, s_ctxs = s_resp.json()
    tokens_by_idx = {int(t["index"]): t for t in (s_meta.get("tokens") or [])}
    univ_by_name = {str(u.get("name")): u for u in (s_meta.get("universe") or [])}

    # Keep the highest-volume USDC spot pair per underlying perp (prefer exact canonical bridge over proxy).
    best_by_perp: dict[str, dict[str, Any]] = {}
    for ctx in s_ctxs or []:
        pair_name = str(ctx.get("coin") or "")
        u = univ_by_name.get(pair_name)
        if not u:
            continue
        toks = u.get("tokens") or []
        if len(toks) < 2:
            continue
        t0 = tokens_by_idx.get(int(toks[0]), {})
        t1 = tokens_by_idx.get(int(toks[1]), {})
        quote = str(t1.get("name") or "").upper()
        if quote != "USDC":
            continue
        spot_tok = str(t0.get("name") or "").strip()
        spot_mid = _f(ctx.get("midPx"), _f(ctx.get("markPx")))
        spot_vol = _f(ctx.get("dayNtlVlm"))
        if not spot_tok or spot_mid <= 0:
            continue

        perp_cand = SPOT_TO_PERP_ALIASES.get(spot_tok, spot_tok)
        if perp_cand not in perps and spot_tok.startswith("U") and spot_tok[1:] in perps:
            perp_cand = spot_tok[1:]
        if perp_cand not in perps:
            continue

        is_held = perp_cand in held_bases or perp_cand.upper() in held_upper
        if not is_held and spot_vol < min_spot_vol_usd:
            continue

        p = perps[perp_cand]
        ratio = spot_mid / p["mark_px"]
        if not is_held and not (0.90 <= ratio <= 1.10):
            continue

        is_proxy = (spot_tok, perp_cand) in PROXY_HEDGE_PAIRS
        row = {
            "base_symbol": perp_cand,
            "spot_pair": pair_name,
            "spot_token": spot_tok,
            "quote_token": quote,
            "is_proxy_hedge": is_proxy,
            "spot_mid_px": spot_mid,
            "spot_day_vol_usd": spot_vol,
            "perp_mark_px": p["mark_px"],
            "perp_oracle_px": p["oracle_px"],
            "funding_rate_h": p["funding_rate_h"],
            "perp_day_vol_usd": p["day_vol_usd"],
            "perp_oi_usd": p["oi_usd"],
            "only_isolated": p["only_isolated"],
            "max_leverage": p["max_leverage"],
        }
        prev = best_by_perp.get(perp_cand)
        if prev is None or (prev["is_proxy_hedge"] and not is_proxy) or (
            prev["is_proxy_hedge"] == is_proxy and spot_vol > prev["spot_day_vol_usd"]
        ):
            best_by_perp[perp_cand] = row

    rows = list(best_by_perp.values())
    rows.sort(key=lambda r: (-r["spot_day_vol_usd"], r["base_symbol"]))
    return rows


def scan_spot_perp_opportunities(
    client: httpx.Client | None = None,
    *,
    cfg: SpotPerpPaperConfig = SpotPerpPaperConfig(),
    clip_usd: float | None = None,
    fetch_l2: bool = True,
    fetch_boros: bool = False,
    markets: list[dict[str, Any]] | None = None,
    book_fetcher: Any = None,
    hlp_summary: tuple[float, float] | None = None,
    include_bases: set[str] | None = None,
    info_url: str = HL_INFO_URL,
    now_ms: int | None = None,
) -> SpotPerpScanSnapshot:
    """Scan all matched HyperCore Spot+Perp pairs with L2 VWAP slippage, fees, and payback."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    obs_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lev = max(1.0, float(cfg.target_perp_leverage))
    cap_eff = lev / (lev + 1.0)
    target_clip = float(clip_usd if clip_usd is not None else cfg.slot_equity_usd * cap_eff)
    held_bases = set(include_bases or set())

    own_client = client is None and (markets is None or (fetch_l2 and book_fetcher is None) or hlp_summary is None)
    http_client = client or (httpx.Client(timeout=20.0) if own_client else None)
    try:
        if markets is None:
            assert http_client is not None
            raw_markets = fetch_matched_spot_perp_markets(
                http_client,
                info_url=info_url,
                min_spot_vol_usd=10_000.0,
                include_bases=held_bases,
            )
        else:
            raw_markets = list(markets)

        if hlp_summary is not None:
            hlp_apr, hlp_tvl = hlp_summary
        else:
            try:
                assert http_client is not None
                hlp_apr, hlp_tvl = fetch_hlp_vault_summary(http_client, info_url=info_url)
            except Exception:
                hlp_apr, hlp_tvl = cfg.default_hlp_apr_pct, 0.0

        boros_rates: dict[str, float] = {}
        if fetch_boros and http_client is not None:
            boros_rates = fetch_boros_hl_fixed_rates(http_client)

        def _default_book(coin_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
            assert http_client is not None
            return cds.fetch_dex_l2_book(http_client, "hyperliquid", coin_key)

        get_book = book_fetcher or _default_book
        one_way_fee = cfg.one_way_fee_bps
        rt_fee_bps = 2.0 * one_way_fee

        opps: list[SpotPerpOpportunity] = []
        for m in raw_markets:
            base = str(m["base_symbol"])
            spot_pair = str(m["spot_pair"])
            spot_tok = str(m["spot_token"])
            is_proxy = bool(m.get("is_proxy_hedge", False))
            only_iso = bool(m.get("only_isolated", False))
            spot_mid = float(m["spot_mid_px"])
            perp_mark = float(m["perp_mark_px"])
            perp_ora = float(m.get("perp_oracle_px", perp_mark))
            mark_basis_bps = round((perp_mark - spot_mid) / spot_mid * 1e4, 2) if spot_mid > 0 else 0.0
            fund_h = float(m["funding_rate_h"])
            fund_apr = round(fund_h * HOURS_PER_YEAR * 100.0, 2)
            spot_vol = float(m["spot_day_vol_usd"])
            perp_vol = float(m["perp_day_vol_usd"])
            perp_oi = float(m.get("perp_oi_usd", 0.0))

            entry_slip: float | None = None
            exec_basis: float | None = None
            full_ok = False
            if fetch_l2 and (base in held_bases or spot_vol >= min(50_000.0, cfg.min_spot_vol_usd)):
                sb, sa = get_book(spot_pair)
                pb, pa = get_book(base)
                # Enter Cash-and-Carry: short perp (hit perp bids) + buy spot (lift spot asks) -> short_hl=True
                exec_basis, entry_slip, full_ok = cds.compute_executable_cross_dex_entry_bps(
                    pb, pa, sb, sa, short_hl=True, clip_usd=target_clip
                )

            # Effective round-trip hurdle (bps) = RT fee + 2x entry L2 slip (conservative exit) - favorable entry basis
            # Cap basis credit at min(exec_basis, mark_basis_bps) and one_way_fee + entry_slip so async L2 spikes never fake payback.
            eff_exec_basis = cds.credited_entry_basis_bps(exec_basis, mark_basis_bps) if exec_basis is not None else None
            eff_slip = float(entry_slip) if entry_slip is not None else 4.0
            basis_credit = min(max(float(eff_exec_basis or 0.0), -50.0), one_way_fee + eff_slip)
            if is_proxy:
                # Proxy hedge (e.g. XAUT0 spot vs PAXG perp): the cross-issuer basis is structural
                # and does not converge, so a favorable entry basis is never credited to payback.
                basis_credit = min(basis_credit, 0.0)
            net_hurdle_bps = max(rt_fee_bps + 2.0 * eff_slip - basis_credit, 1.0)
            hourly_bps = fund_h * 1e4
            payback_h = round(net_hurdle_bps / hourly_bps, 1) if hourly_bps > 1e-6 else None

            reasons: list[str] = []
            if spot_vol < cfg.min_spot_vol_usd:
                reasons.append(f"spot_vol<${cfg.min_spot_vol_usd/1e3:.0f}k")
            if perp_vol < cfg.min_perp_vol_usd:
                reasons.append(f"perp_vol<${cfg.min_perp_vol_usd/1e6:.1f}M")
            if only_iso and not cfg.allow_only_isolated:
                reasons.append("onlyIsolated")
            if is_proxy and not cfg.allow_proxy_hedges:
                reasons.append("proxy_disabled")
            if is_proxy and abs(mark_basis_bps) > cfg.max_proxy_basis_bps:
                reasons.append(f"proxy_basis>{cfg.max_proxy_basis_bps:.0f}bps")
            if fund_apr < cfg.entry_apr_pct:
                reasons.append(f"apr<{cfg.entry_apr_pct:.1f}%")
            if fetch_l2:
                if entry_slip is None or not full_ok:
                    reasons.append("thin_l2_book")
                elif entry_slip > cfg.max_l2_slip_bps:
                    reasons.append(f"l2_slip>{cfg.max_l2_slip_bps:.1f}bps")
                if eff_exec_basis is not None and eff_exec_basis < cfg.min_exec_basis_bps:
                    reasons.append(f"neg_basis<{cfg.min_exec_basis_bps:.0f}bps")
            if payback_h is None or payback_h > cfg.max_payback_hours:
                reasons.append(f"payback>{cfg.max_payback_hours:.0f}h")

            net_dep_apr = round(fund_apr * cap_eff, 2)
            opps.append(
                SpotPerpOpportunity(
                    base_symbol=base,
                    spot_pair=spot_pair,
                    spot_token=spot_tok,
                    quote_token=str(m.get("quote_token", "USDC")),
                    asset_tier=classify_asset_tier(base),
                    is_proxy_hedge=is_proxy,
                    only_isolated=only_iso,
                    max_leverage=int(m.get("max_leverage", 10)),
                    spot_mid_px=round(spot_mid, 6),
                    perp_mark_px=round(perp_mark, 6),
                    perp_oracle_px=round(perp_ora, 6),
                    mark_basis_bps=mark_basis_bps,
                    funding_rate_h=fund_h,
                    funding_apr_pct=fund_apr,
                    spot_day_vol_usd=round(spot_vol, 2),
                    perp_day_vol_usd=round(perp_vol, 2),
                    perp_oi_usd=round(perp_oi, 2),
                    entry_l2_slip_bps=entry_slip,
                    exec_basis_bps=exec_basis,
                    l2_full_fill_ok=full_ok,
                    one_way_fee_bps=round(one_way_fee, 2),
                    capital_efficiency_pct=round(cap_eff * 100.0, 1),
                    net_deposit_apr_pct=net_dep_apr,
                    boros_fixed_apr_pct=boros_rates.get(base.upper()),
                    payback_hours=payback_h,
                    eligible=(len(reasons) == 0),
                    reject_reason=", ".join(reasons),
                )
            )

        opps.sort(
            key=lambda o: (
                not o.eligible,
                o.is_proxy_hedge,
                -o.net_deposit_apr_pct,
                -(o.exec_basis_bps if o.exec_basis_bps is not None else -999.0),
                -o.spot_day_vol_usd,
            )
        )
        return SpotPerpScanSnapshot(
            observed_at_ms=ts_ms,
            observed_at_utc=obs_utc,
            hlp_apr_pct=hlp_apr,
            hlp_tvl_usd=hlp_tvl,
            clip_usd=round(target_clip, 2),
            target_perp_leverage=lev,
            use_maker_fees=cfg.use_maker_fees,
            opportunities=opps,
        )
    finally:
        if own_client and http_client is not None:
            http_client.close()


# --------------------------------------------------------------------------- 4h Paper Ledger Engine


def _load_or_init_state(state_path: Path, cfg: SpotPerpPaperConfig) -> dict[str, Any]:
    if state_path.is_file():
        st = json.loads(state_path.read_text(encoding="utf-8"))
        st.setdefault("positions", {})
        st.setdefault("clean_streaks", {})
        st.setdefault("low_apr_streaks", {})
        st.setdefault("closed_positions_count", 0)
        st.setdefault("rebalances_count", 0)
        st.setdefault("nav_guard_triggers", 0)
        return st
    hlp_init = round(cfg.starting_capital_usd * min(cfg.hlp_reserve_share, cfg.max_hlp_reserve_share), 2)
    return {
        "starting_capital_usd": cfg.starting_capital_usd,
        "equity_usd": cfg.starting_capital_usd,
        "share_price": 1.0,
        "return_pct": 0.0,
        "hlp_reserve_usd": hlp_init,
        "hlp_apr_pct": cfg.default_hlp_apr_pct,
        "free_cash_usd": round(cfg.starting_capital_usd - hlp_init, 2),
        "cum_funding_usd": 0.0,
        "cum_hlp_yield_usd": 0.0,
        "cum_basis_mtm_usd": 0.0,
        "cum_costs_usd": 0.0,
        "last_tick_ms": 0,
        "positions": {},
        "clean_streaks": {},
        "low_apr_streaks": {},
        "closed_positions_count": 0,
        "rebalances_count": 0,
        "nav_guard_triggers": 0,
    }


def _PerpMMR(max_leverage: object) -> float:
    """Hyperliquid maintenance margin fraction: ``1 / (2 * max_leverage)`` (e.g. 40x -> 1.25%, 3x -> 16.67%)."""
    try:
        lev = float(max_leverage)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        lev = 10.0
    if not math.isfinite(lev) or lev < 1.0:
        lev = 10.0
    return 0.5 / lev


def fetch_settled_funding_sum(
    client: httpx.Client,
    coins: list[str] | set[str],
    start_ms: int,
    end_ms: int,
    *,
    info_url: str = HL_INFO_URL,
) -> dict[str, float]:
    """Sum realized hourly ``fundingHistory`` rates over ``[start_ms, end_ms]`` per perp coin."""
    out: dict[str, float] = {}
    if end_ms < start_ms:
        return out
    for coin in sorted({str(c) for c in coins if c}):
        try:
            cursor = int(start_ms)
            total_rate = 0.0
            valid = 0
            for _ in range(10):
                resp = client.post(
                    info_url,
                    json={"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": int(end_ms)},
                )
                if resp.status_code != 200:
                    break
                rows = resp.json()
                if not isinstance(rows, list) or not rows:
                    break
                max_t = cursor
                for item in rows:
                    if not isinstance(item, dict) or "fundingRate" not in item:
                        continue
                    t = int(_f(item.get("time"), -1))
                    if cursor <= t <= end_ms:
                        total_rate += _f(item.get("fundingRate"), 0.0)
                        valid += 1
                    if t > max_t:
                        max_t = t
                if len(rows) < 500 or max_t <= cursor or max_t >= end_ms:
                    break
                cursor = max_t + 1
            if valid > 0:
                out[coin] = total_rate
        except Exception:
            continue
    return out


def _compute_30d_leverage_stats(ticks_path: Path, current_leverage: float, now_ms: int) -> tuple[float, float]:
    """Compute Neutral-style ``(leverage_30d_avg, leverage_30d_max)`` from persisted tick history."""
    vals = [current_leverage]
    if ticks_path.is_file():
        try:
            df = pd.read_parquet(ticks_path)
            if not df.empty and "gross_leverage" in df.columns and "tick_ms" in df.columns:
                cutoff = now_ms - 30 * 24 * 3_600_000
                recent = df[df["tick_ms"] >= cutoff]["gross_leverage"].dropna().astype(float).tolist()
                vals = recent + [current_leverage]
        except Exception:
            pass
    avg_lev = sum(vals) / len(vals)
    max_lev = max(vals)
    return round(avg_lev, 3), round(max_lev, 3)


def run_spot_perp_paper_tick(
    client: httpx.Client | None = None,
    *,
    state_dir: Path,
    cfg: SpotPerpPaperConfig = SpotPerpPaperConfig(),
    now_ms: int | None = None,
    markets: list[dict[str, Any]] | None = None,
    book_fetcher: Any = None,
    hlp_summary: tuple[float, float] | None = None,
    info_url: str = HL_INFO_URL,
) -> dict[str, Any]:
    """Run one idempotent single-venue HyperCore Spot+Perp + 10% HLP paper tick."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    obs_utc = datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    ticks_path = state_dir / "ticks.parquet"
    trades_path = state_dir / "trades.parquet"
    state = _load_or_init_state(state_path, cfg)

    own_client = client is None and (markets is None or book_fetcher is None or hlp_summary is None)
    http_client = client or (httpx.Client(timeout=20.0) if own_client else None)
    try:
        held_bases = set((state.get("positions") or {}).keys())
        scan = scan_spot_perp_opportunities(
            http_client,
            cfg=cfg,
            fetch_l2=True,
            fetch_boros=False,
            markets=markets,
            book_fetcher=book_fetcher,
            hlp_summary=hlp_summary,
            include_bases=held_bases,
            info_url=info_url,
            now_ms=ts_ms,
        )

        def _default_book(coin_key: str) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
            assert http_client is not None
            return cds.fetch_dex_l2_book(http_client, "hyperliquid", coin_key)

        get_book = book_fetcher or _default_book
        by_base: dict[str, SpotPerpOpportunity] = {o.base_symbol: o for o in scan.opportunities}

        prev_ms = int(state.get("last_tick_ms", 0))
        dt_cap_h = 168.0 if state.get("nav_guard_pending_ms") else 24.0
        dt_hours = max(0.0, min(dt_cap_h, (ts_ms - prev_ms) / 3_600_000.0)) if prev_ms > 0 else 0.0
        prev_equity = float(state.get("equity_usd", cfg.starting_capital_usd))

        positions: dict[str, dict[str, Any]] = state.get("positions") or {}
        low_apr_streaks: dict[str, Any] = state.get("low_apr_streaks") or {}
        trade_rows: list[dict[str, Any]] = []

        tick_funding_usd = 0.0
        tick_basis_mtm_usd = 0.0
        tick_costs_usd = 0.0
        nav_guard_triggered = False
        nav_guard_reason = ""

        # 1. Accrue HLP 10% reserve yield
        hlp_apr = scan.hlp_apr_pct if scan.hlp_apr_pct != 0.0 else float(state.get("hlp_apr_pct", cfg.default_hlp_apr_pct))
        hlp_usd_prev = float(state.get("hlp_reserve_usd", 0.0))
        positions_backup = {k: dict(v) for k, v in positions.items()}
        hlp_usd = hlp_usd_prev
        tick_hlp_usd = hlp_usd * (hlp_apr / 100.0 / HOURS_PER_YEAR) * dt_hours if dt_hours > 0 else 0.0
        hlp_usd = round(hlp_usd + tick_hlp_usd, 4)

        # 2. Mark-to-market open positions (delta-neutral spot + short perp) & accrue settled hourly funding
        # Per-position accrual clock: a position skipped on an earlier tick (missing quote) keeps its
        # own ``last_accrual_ms``, so its settled-funding window starts where it last accrued.
        pos_start_ms = {base: pc.position_accrual_start_ms(pos, prev_ms) for base, pos in positions.items()}
        settled_rates: dict[str, float] = {}
        if prev_ms > 0 and positions and markets is None and http_client is not None:
            by_start: dict[int, list[str]] = {}
            for base, start in pos_start_ms.items():
                if start > 0 and (ts_ms // 3_600_000) - (start // 3_600_000) > 0:
                    by_start.setdefault(start, []).append(base)
            for start, bases in by_start.items():
                settled_rates.update(
                    fetch_settled_funding_sum(http_client, bases, start + 1, ts_ms, info_url=info_url)
                )

        for base, pos in list(positions.items()):
            opp = by_base.get(base)
            if opp is None:
                continue
            qty = float(pos["qty"])
            prev_spot = float(pos["last_spot_px"])
            prev_perp = float(pos["last_perp_px"])
            cur_spot = opp.spot_mid_px
            cur_perp = opp.perp_mark_px

            # Spot leg is long (+qty); Perp leg is short (-qty)
            spot_pnl = qty * (cur_spot - prev_spot)
            perp_pnl = qty * (prev_perp - cur_perp)
            basis_pnl = spot_pnl + perp_pnl
            perp_notional = qty * cur_perp
            spot_notional = qty * cur_spot
            p_start = pos_start_ms.get(base, prev_ms)
            # Same cap as the global clock (168h while a NAV-guard confirmation is pending).
            pos_dt_hours = max(0.0, min(dt_cap_h, (ts_ms - p_start) / 3_600_000.0)) if p_start > 0 else dt_hours
            pos_settlements = (ts_ms // 3_600_000) - (p_start // 3_600_000) if p_start > 0 else 0
            pos["last_accrual_ms"] = ts_ms
            if prev_ms > 0 and markets is None and pos_settlements == 0:
                # Sub-hour re-run with zero `:00` settlements crossed accrues zero settled funding.
                fund_pnl = 0.0
            elif base in settled_rates:
                fund_pnl = perp_notional * settled_rates[base]
            else:
                fund_pnl = perp_notional * opp.funding_rate_h * pos_dt_hours

            # Update perp margin with unrealized perp PnL + funding accrued on perp account
            perp_margin = float(pos["perp_margin_usd"]) + perp_pnl + fund_pnl
            max_lev = int(pos.get("max_leverage") or opp.max_leverage or 10)
            pos["max_leverage"] = max_lev
            mmr = _PerpMMR(max_lev)

            pos["last_spot_px"] = cur_spot
            pos["last_perp_px"] = cur_perp
            pos["spot_notional_usd"] = round(spot_notional, 2)
            pos["perp_notional_usd"] = round(perp_notional, 2)
            pos["perp_margin_usd"] = round(perp_margin, 2)
            pos["effective_perp_leverage"] = round(perp_notional / max(perp_margin, 1.0), 3)
            pos["perp_liq_distance_pct"] = round(
                max(0.0, (perp_margin / max(perp_notional, 1.0) - mmr) * 100.0), 2
            )
            pos["hours_held"] = round(float(pos.get("hours_held", 0.0)) + pos_dt_hours, 2)
            pos["live_funding_apr_pct"] = opp.funding_apr_pct
            pos["live_mark_basis_bps"] = opp.mark_basis_bps
            pos["cum_funding_usd"] = round(float(pos.get("cum_funding_usd", 0.0)) + fund_pnl, 4)
            pos["cum_basis_mtm_usd"] = round(float(pos.get("cum_basis_mtm_usd", 0.0)) + basis_pnl, 4)
            pos["net_pnl_usd"] = round(
                pos["cum_funding_usd"] + pos["cum_basis_mtm_usd"] - float(pos.get("cum_cost_usd", 0.0)),
                4,
            )
            pos["payback_achieved"] = bool(pos["cum_funding_usd"] >= float(pos.get("entry_cost_usd", 0.0)))

            tick_funding_usd += fund_pnl
            tick_basis_mtm_usd += basis_pnl

        # 2b. Bounded NAV Sanity Guard (Neutral Trade circuit breaker). A jump is clamped once; if
        # it persists on a tick >= NAV_GUARD_CONFIRM_MS later it is accepted as genuine. A clamped
        # tick keeps ``last_tick_ms`` so the confirming tick re-accrues funding/HLP/basis over the
        # whole interval (nothing is lost and the ledger cannot freeze forever).
        raw_tick_pnl = tick_funding_usd + tick_basis_mtm_usd + tick_hlp_usd
        pending_raw = state.get("nav_guard_pending_ms")
        nav_guard_pending: int | None = int(pending_raw) if pending_raw else None
        jump_exceeded = prev_equity > 0 and abs(raw_tick_pnl) / prev_equity * 100.0 > cfg.max_tick_nav_jump_pct
        if jump_exceeded and nav_guard_pending is not None and ts_ms - nav_guard_pending >= NAV_GUARD_CONFIRM_MS:
            nav_guard_reason = (
                f"Tick NAV jump {raw_tick_pnl / prev_equity * 100.0:+.2f}% persisted since the clamped tick; "
                "accepted as genuine"
            )
            nav_guard_pending = None
            jump_exceeded = False
        elif not jump_exceeded:
            nav_guard_pending = None
        if jump_exceeded:
            nav_guard_pending = nav_guard_pending if nav_guard_pending is not None else ts_ms
            nav_guard_triggered = True
            nav_guard_reason = (
                f"Tick NAV jump {raw_tick_pnl / prev_equity * 100.0:+.2f}% exceeded "
                f"+/-{cfg.max_tick_nav_jump_pct:.1f}% guard; pausing entries & clamping MTM"
            )
            state["nav_guard_triggers"] = int(state.get("nav_guard_triggers", 0)) + 1
            # Revert anomalous tick MTM to protect equity ledger integrity
            positions = positions_backup
            hlp_usd = hlp_usd_prev
            tick_funding_usd = 0.0
            tick_basis_mtm_usd = 0.0
            tick_hlp_usd = 0.0

        # 3. Auto-rebalance perp margin if a sharp rally erodes short-perp margin beyond max_perp_leverage
        free_cash = float(state.get("free_cash_usd", 0.0))
        if not nav_guard_triggered:
            for base, pos in list(positions.items()):
                eff_lev = float(pos.get("effective_perp_leverage", 0.0))
                if eff_lev > cfg.max_perp_leverage:
                    perp_ntl = float(pos["perp_notional_usd"])
                    target_margin = perp_ntl / max(cfg.target_perp_leverage, 1.0)
                    shortfall = max(0.0, target_margin - float(pos["perp_margin_usd"]))
                    topup = min(shortfall, max(0.0, free_cash))
                    if topup > 1.0:
                        pos["perp_margin_usd"] = round(float(pos["perp_margin_usd"]) + topup, 2)
                        free_cash = round(free_cash - topup, 2)
                        pos["effective_perp_leverage"] = round(perp_ntl / max(pos["perp_margin_usd"], 1.0), 3)
                        mmr_top = _PerpMMR(pos.get("max_leverage", 10))
                        pos["perp_liq_distance_pct"] = round(
                            max(0.0, (pos["perp_margin_usd"] / max(perp_ntl, 1.0) - mmr_top) * 100.0), 2
                        )
                        state["rebalances_count"] = int(state.get("rebalances_count", 0)) + 1
                        trade_rows.append(
                            {
                                "tick_ms": ts_ms,
                                "observed_at_utc": obs_utc,
                                "action": "MARGIN_TOPUP",
                                "base_symbol": base,
                                "spot_pair": pos["spot_pair"],
                                "notional_usd": round(topup, 2),
                                "funding_apr_pct": pos.get("live_funding_apr_pct", 0.0),
                                "cost_usd": 0.0,
                                "reason": f"perp_lev>{cfg.max_perp_leverage:.1f}x",
                            }
                        )

        # 4. Evaluate exits: short-perp liquidation, funding flip/decay, proxy basis dislocation, or missing market
        closed_this_tick: set[str] = set()
        if not nav_guard_triggered:
            _write_json_atomic(state_dir / "snapshot.json", scan.to_dict())
            market_online = bool(by_base)
            for base in list(positions.keys()):
                pos = positions[base]
                opp = by_base.get(base)
                exit_reason = ""
                mmr_chk = _PerpMMR(pos.get("max_leverage", 10))
                if (
                    float(pos.get("perp_margin_usd", 0.0)) <= float(pos.get("perp_notional_usd", 0.0)) * mmr_chk
                    or float(pos.get("perp_liq_distance_pct", 100.0)) <= 0.0
                ):
                    exit_reason = "perp_margin_liquidation"
                elif opp is None:
                    if not market_online:
                        continue
                    miss = int(pos.get("missing_quote_ticks", 0)) + 1
                    pos["missing_quote_ticks"] = miss
                    if miss >= cfg.max_missing_ticks:
                        exit_reason = "missing_market"
                else:
                    pos["missing_quote_ticks"] = 0
                    if opp.funding_apr_pct <= cfg.hard_flip_apr_pct:
                        exit_reason = f"hard_funding_flip({opp.funding_apr_pct:+.1f}%)"
                    elif opp.is_proxy_hedge and abs(opp.mark_basis_bps) > cfg.max_proxy_basis_bps * 1.5:
                        exit_reason = f"proxy_basis_break({opp.mark_basis_bps:+.1f}bps)"
                    elif opp.funding_apr_pct < cfg.exit_apr_pct:
                        low_apr_streaks[base] = advance_streak(
                            low_apr_streaks.get(base),
                            qualifies=True,
                            now_ms=ts_ms,
                            min_spacing_hours=cfg.min_streak_spacing_hours,
                        )
                        held_h = float(pos.get("hours_held", 0.0))
                        if (
                            streak_count(low_apr_streaks[base]) >= cfg.exit_confirm_ticks
                            and held_h >= cfg.min_hold_hours_before_soft_exit
                        ):
                            exit_reason = f"funding_decay({opp.funding_apr_pct:+.1f}%)"
                    elif (
                        cfg.require_hlp_hurdle
                        and bool(pos.get("payback_achieved"))
                        and float(pos.get("hours_held", 0.0)) >= max(24.0, cfg.min_hold_hours_before_soft_exit)
                        and opp.net_deposit_apr_pct < hlp_apr * 0.65
                    ):
                        low_apr_streaks[base] = advance_streak(
                            low_apr_streaks.get(base),
                            qualifies=True,
                            now_ms=ts_ms,
                            min_spacing_hours=cfg.min_streak_spacing_hours,
                        )
                        if streak_count(low_apr_streaks[base]) >= cfg.exit_confirm_ticks:
                            exit_reason = f"hlp_opportunity_cost({opp.net_deposit_apr_pct:+.1f}%<HLP {hlp_apr:+.1f}%)"
                    else:
                        low_apr_streaks[base] = advance_streak(None, qualifies=False, now_ms=ts_ms)

                if exit_reason:
                    notional = float(pos["perp_notional_usd"])
                    exit_slip = 3.0
                    if opp is not None:
                        sb, sa = get_book(opp.spot_pair)
                        pb, pa = get_book(base)
                        # Closing: buy back perp (ask) + sell spot (bid) -> short_hl=False
                        _, sl, _ = cds.compute_executable_cross_dex_entry_bps(
                            pb, pa, sb, sa, short_hl=False, clip_usd=notional
                        )
                        if sl is not None:
                            exit_slip = sl
                    exit_cost = notional * (cfg.one_way_fee_bps + exit_slip) / 1e4
                    tick_costs_usd += exit_cost
                    # Release spot value + perp margin back to free cash (minus exit cost)
                    released_usd = float(pos["spot_notional_usd"]) + float(pos["perp_margin_usd"]) - exit_cost
                    free_cash = round(free_cash + released_usd, 2)
                    state["closed_positions_count"] = int(state.get("closed_positions_count", 0)) + 1
                    trade_rows.append(
                        {
                            "tick_ms": ts_ms,
                            "observed_at_utc": obs_utc,
                            "action": "CLOSE",
                            "base_symbol": base,
                            "spot_pair": pos["spot_pair"],
                            "notional_usd": round(notional, 2),
                            "funding_apr_pct": opp.funding_apr_pct if opp else 0.0,
                            "cost_usd": round(exit_cost, 4),
                            "reason": exit_reason,
                        }
                    )
                    del positions[base]
                    low_apr_streaks.pop(base, None)
                    closed_this_tick.add(base)

        # 5. Update clean persistence streaks & evaluate new entries
        if nav_guard_triggered:
            clean_streaks = dict(state.get("clean_streaks") or {})
        else:
            eligible_bases = {o.base_symbol for o in scan.opportunities if o.eligible}
            clean_streaks = update_streak_map(
                state.get("clean_streaks") or {},
                eligible_bases,
                now_ms=ts_ms,
                min_spacing_hours=cfg.min_streak_spacing_hours,
                keep_non_qualifying=True,
            )
        hurdle_apr = hlp_apr * cfg.hlp_hurdle_mult if cfg.require_hlp_hurdle else float("-inf")

        # Recompute total equity before sizing new entries
        deployed_eq = sum(float(p["spot_notional_usd"]) + float(p["perp_margin_usd"]) for p in positions.values())
        cur_equity = round(deployed_eq + hlp_usd + free_cash, 2)
        min_free_cash = cur_equity * cfg.min_cash_buffer_share

        if not nav_guard_triggered:
            # Top up / rebalance HLP reserve toward 10% target if drifted by > 2% (deadband)
            target_hlp = cur_equity * min(cfg.hlp_reserve_share, cfg.max_hlp_reserve_share)
            if abs(hlp_usd - target_hlp) / max(cur_equity, 1.0) > 0.02:
                delta_hlp = target_hlp - hlp_usd
                if delta_hlp > 0 and free_cash - delta_hlp >= min_free_cash:
                    hlp_usd = round(hlp_usd + delta_hlp, 2)
                    free_cash = round(free_cash - delta_hlp, 2)
                elif delta_hlp < 0:
                    hlp_usd = round(hlp_usd + delta_hlp, 2)
                    free_cash = round(free_cash - delta_hlp, 2)

            lev = max(1.0, float(cfg.target_perp_leverage))
            cap_eff = lev / (lev + 1.0)

            for opp in scan.opportunities:
                if len(positions) >= cfg.max_slots:
                    break
                if not opp.eligible or opp.base_symbol in positions or opp.base_symbol in closed_this_tick:
                    continue
                if streak_count(clean_streaks.get(opp.base_symbol)) < cfg.min_clean_streak:
                    continue
                if opp.net_deposit_apr_pct < hurdle_apr:
                    continue

                slot_eq = cfg.slot_equity_usd
                if opp.is_proxy_hedge:
                    slot_eq *= cfg.proxy_slot_scale
                if opp.only_isolated:
                    slot_eq *= cfg.isolated_slot_scale

                available_to_deploy = max(0.0, free_cash - min_free_cash)
                if available_to_deploy < slot_eq * 0.5:
                    break
                alloc_eq = min(slot_eq, available_to_deploy)

                notional = alloc_eq * cap_eff
                perp_margin = alloc_eq - notional
                entry_slip = float(opp.entry_l2_slip_bps) if opp.entry_l2_slip_bps is not None else 3.0
                entry_cost = notional * (cfg.one_way_fee_bps + entry_slip) / 1e4
                perp_margin_after_fee = max(perp_margin - entry_cost, 1.0)
                qty = notional / opp.spot_mid_px

                tick_costs_usd += entry_cost
                free_cash = round(free_cash - alloc_eq, 2)
                positions[opp.base_symbol] = {
                    "base_symbol": opp.base_symbol,
                    "spot_pair": opp.spot_pair,
                    "spot_token": opp.spot_token,
                    "asset_tier": opp.asset_tier,
                    "is_proxy_hedge": opp.is_proxy_hedge,
                    "only_isolated": opp.only_isolated,
                    "max_leverage": opp.max_leverage,
                    "qty": round(qty, 8),
                    "entry_spot_px": opp.spot_mid_px,
                    "entry_perp_px": opp.perp_mark_px,
                    "last_spot_px": opp.spot_mid_px,
                    "last_perp_px": opp.perp_mark_px,
                    "spot_notional_usd": round(notional, 2),
                    "perp_notional_usd": round(qty * opp.perp_mark_px, 2),
                    "perp_margin_usd": round(perp_margin_after_fee, 2),
                    "effective_perp_leverage": round((qty * opp.perp_mark_px) / perp_margin_after_fee, 3),
                    "perp_liq_distance_pct": round(
                        max(0.0, (perp_margin_after_fee / max(qty * opp.perp_mark_px, 1.0) - _PerpMMR(opp.max_leverage)) * 100.0),
                        2,
                    ),
                    "entry_funding_apr_pct": opp.funding_apr_pct,
                    "live_funding_apr_pct": opp.funding_apr_pct,
                    "entry_exec_basis_bps": opp.exec_basis_bps,
                    "live_mark_basis_bps": opp.mark_basis_bps,
                    "entry_l2_slip_bps": entry_slip,
                    "entry_cost_usd": round(entry_cost, 4),
                    "cum_cost_usd": round(entry_cost, 4),
                    "cum_funding_usd": 0.0,
                    "cum_basis_mtm_usd": 0.0,
                    "net_pnl_usd": round(-entry_cost, 4),
                    "hours_held": 0.0,
                    "payback_achieved": False,
                    "opened_at_utc": obs_utc,
                }
                trade_rows.append(
                    {
                        "tick_ms": ts_ms,
                        "observed_at_utc": obs_utc,
                        "action": "OPEN",
                        "base_symbol": opp.base_symbol,
                        "spot_pair": opp.spot_pair,
                        "notional_usd": round(notional, 2),
                        "funding_apr_pct": opp.funding_apr_pct,
                        "cost_usd": round(entry_cost, 4),
                        "reason": f"streak={streak_count(clean_streaks[opp.base_symbol])},basis={opp.exec_basis_bps}bps",
                    }
                )

        # 6. Compute final equity, exit-cost liquidation NAV & Neutral-style Live Risk & Exposure telemetry
        for p in positions.values():
            est_slip = float(p.get("entry_l2_slip_bps", 3.0))
            est_exit_usd = round(float(p["spot_notional_usd"]) * (cfg.one_way_fee_bps + est_slip) / 1e4, 4)
            p["est_exit_cost_usd"] = est_exit_usd
            p["liquidation_net_pnl_usd"] = round(float(p.get("net_pnl_usd", 0.0)) - est_exit_usd, 4)

        spot_total_usd = sum(float(p["spot_notional_usd"]) for p in positions.values())
        perp_total_usd = sum(float(p["perp_notional_usd"]) for p in positions.values())
        perp_margin_total_usd = sum(float(p["perp_margin_usd"]) for p in positions.values())
        final_equity = round(spot_total_usd + perp_margin_total_usd + hlp_usd + free_cash, 2)
        est_exit_costs_usd = round(sum(float(p.get("est_exit_cost_usd", 0.0)) for p in positions.values()), 2)
        liquidation_equity_usd = round(final_equity - est_exit_costs_usd, 2)
        start_cap = float(state.get("starting_capital_usd", cfg.starting_capital_usd))
        ret_pct = round((final_equity / start_cap - 1.0) * 100.0, 4) if start_cap > 0 else 0.0
        share_price = round(final_equity / start_cap, 6) if start_cap > 0 else 1.0

        gross_notional_usd = spot_total_usd + perp_total_usd + hlp_usd
        gross_leverage = round(gross_notional_usd / max(final_equity, 1.0), 3)
        lev_30d_avg, lev_30d_max = _compute_30d_leverage_stats(ticks_path, gross_leverage, ts_ms)
        net_delta_usd = round(spot_total_usd - perp_total_usd, 2)
        net_delta_pct = round(net_delta_usd / max(final_equity, 1.0) * 100.0, 4)
        min_liq_dist = (
            min(float(p.get("perp_liq_distance_pct", 100.0)) for p in positions.values()) if positions else 100.0
        )

        # Weighted live expected APR across deployed portfolio (spot+perp funding + HLP reserve)
        blended_apr_num = (
            sum(float(p["perp_notional_usd"]) * float(p.get("live_funding_apr_pct", 0.0)) for p in positions.values())
            + hlp_usd * hlp_apr
        )
        blended_portfolio_apr_pct = round(blended_apr_num / max(final_equity, 1.0), 2)

        tier_breakdown: dict[str, float] = {}
        for p in positions.values():
            tier = str(p.get("asset_tier") or "Crypto - Small/Meme")
            tier_breakdown[tier] = round(
                tier_breakdown.get(tier, 0.0) + float(p["spot_notional_usd"]) + float(p["perp_margin_usd"]), 2
            )
        if hlp_usd > 0:
            tier_breakdown["HLP Vault Reserve"] = round(hlp_usd, 2)
        if free_cash > 0:
            tier_breakdown["Free USDC Buffer"] = round(free_cash, 2)

        venue_breakdown = {
            "HyperCore Spot": round(spot_total_usd, 2),
            "HyperCore Perp Margin": round(perp_margin_total_usd, 2),
            "HyperCore HLP Vault": round(hlp_usd, 2),
            "Free USDC Reserve": round(free_cash, 2),
        }

        state.update(
            {
                "equity_usd": final_equity,
                "est_exit_costs_usd": est_exit_costs_usd,
                "liquidation_equity_usd": liquidation_equity_usd,
                "share_price": share_price,
                "return_pct": ret_pct,
                "hlp_reserve_usd": round(hlp_usd, 2),
                "hlp_reserve_share_pct": round(hlp_usd / max(final_equity, 1.0) * 100.0, 2),
                "hlp_apr_pct": hlp_apr,
                "hlp_tvl_usd": scan.hlp_tvl_usd,
                "free_cash_usd": round(free_cash, 2),
                "blended_portfolio_apr_pct": blended_portfolio_apr_pct,
                "cum_funding_usd": round(float(state.get("cum_funding_usd", 0.0)) + tick_funding_usd, 4),
                "cum_hlp_yield_usd": round(float(state.get("cum_hlp_yield_usd", 0.0)) + tick_hlp_usd, 4),
                "cum_basis_mtm_usd": round(float(state.get("cum_basis_mtm_usd", 0.0)) + tick_basis_mtm_usd, 4),
                "cum_costs_usd": round(float(state.get("cum_costs_usd", 0.0)) + tick_costs_usd, 4),
                "last_tick_ms": prev_ms if nav_guard_triggered else ts_ms,
                "nav_guard_pending_ms": nav_guard_pending,
                "observed_at_utc": obs_utc,
                "positions": positions,
                "clean_streaks": clean_streaks,
                "low_apr_streaks": low_apr_streaks,
                "risk_telemetry": {
                    "gross_leverage": gross_leverage,
                    "leverage_30d_avg": lev_30d_avg,
                    "leverage_30d_max": lev_30d_max,
                    "active_assets": len(positions),
                    "venues_count": 1,
                    "net_delta_usd": net_delta_usd,
                    "net_delta_pct": net_delta_pct,
                    "min_perp_liq_distance_pct": round(min_liq_dist, 2),
                    "venue_breakdown_usd": venue_breakdown,
                    "asset_tier_breakdown_usd": tier_breakdown,
                    "nav_guard_triggered": nav_guard_triggered,
                    "nav_guard_reason": nav_guard_reason,
                },
            }
        )

        _write_json_atomic(state_path, state)
        _append_parquet(
            ticks_path,
            [
                {
                    "tick_ms": ts_ms,
                    "observed_at_utc": obs_utc,
                    "equity_usd": final_equity,
                    "share_price": share_price,
                    "return_pct": ret_pct,
                    "gross_leverage": gross_leverage,
                    "net_delta_pct": net_delta_pct,
                    "active_assets": len(positions),
                    "hlp_reserve_usd": round(hlp_usd, 2),
                    "blended_portfolio_apr_pct": blended_portfolio_apr_pct,
                    "tick_funding_usd": round(tick_funding_usd, 4),
                    "tick_hlp_usd": round(tick_hlp_usd, 4),
                    "tick_basis_mtm_usd": round(tick_basis_mtm_usd, 4),
                    "tick_costs_usd": round(tick_costs_usd, 4),
                    "nav_guard_triggered": nav_guard_triggered,
                }
            ],
        )
        _append_parquet(trades_path, trade_rows)

        return {
            "state": state,
            "scan": scan.to_dict(),
            "trades_this_tick": trade_rows,
        }
    finally:
        if own_client and http_client is not None:
            http_client.close()


# --------------------------------------------------------------------------- Formatting


def format_spot_perp_scan_markdown(snap: SpotPerpScanSnapshot) -> str:
    elig = [o for o in snap.opportunities if o.eligible]
    lines = [
        f"# HyperCore Native Spot + Perp Funding Arb Scan ({snap.observed_at_utc})",
        "",
        f"- **Matched Pairs:** `{len(snap.opportunities)}` (`{len(elig)}` eligible) · "
        f"**Target Clip:** `${snap.clip_usd:,.0f}` · **Perp Lev:** `{snap.target_perp_leverage:.1f}x` · "
        f"**Fee Mode:** `{'Maker ALO' if snap.use_maker_fees else 'Taker'}`",
        f"- **HLP Reserve Benchmark (10% sleeve):** **`{snap.hlp_apr_pct:+.2f}%` APR** (TVL `${snap.hlp_tvl_usd/1e6:,.2f}M`)",
        "",
        "| Pair (Spot -> Perp) | Tier | Spot Vol | Perp APR | Net Dep APR | Exec Basis | L2 Slip | Payback | Boros Fix | Status |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for o in snap.opportunities[:20]:
        proxy_tag = " [proxy]" if o.is_proxy_hedge else ""
        basis_s = f"{o.exec_basis_bps:+.1f} bps" if o.exec_basis_bps is not None else f"{o.mark_basis_bps:+.1f} (m)"
        slip_s = f"{o.entry_l2_slip_bps:.1f} bps" if o.entry_l2_slip_bps is not None else "—"
        pb_s = f"{o.payback_hours:.0f}h" if o.payback_hours is not None else "—"
        bor_s = f"{o.boros_fixed_apr_pct:+.2f}%" if o.boros_fixed_apr_pct is not None else "—"
        st_s = "✅ ELIGIBLE" if o.eligible else f"⏸ {o.reject_reason}"
        lines.append(
            f"| **{o.spot_token} -> {o.base_symbol}** (`{o.spot_pair}`){proxy_tag} | {o.asset_tier} | "
            f"${o.spot_day_vol_usd/1e6:.2f}M | {o.funding_apr_pct:+.2f}% | **{o.net_deposit_apr_pct:+.2f}%** | "
            f"{basis_s} | {slip_s} | {pb_s} | {bor_s} | {st_s} |"
        )
    return "\n".join(lines) + "\n"


def format_spot_perp_paper_markdown(result: dict[str, Any]) -> str:
    st = result["state"]
    rt = st.get("risk_telemetry") or {}
    positions = list((st.get("positions") or {}).values())
    lines = [
        f"# HyperCore Spot + Perp + 10% HLP Paper Ledger ({st.get('observed_at_utc', '')})",
        "",
        f"- **Equity:** `${st['equity_usd']:,.2f}` (`{st['return_pct']:+.4f}%` · Share `${st['share_price']:.4f}`)",
        f"- **Blended Portfolio APR:** **`{st.get('blended_portfolio_apr_pct', 0.0):+.2f}%`** · "
        f"**HLP Reserve:** `${st['hlp_reserve_usd']:,.2f}` (`{st.get('hlp_reserve_share_pct', 0.0):.1f}%` @ `{st['hlp_apr_pct']:+.2f}%`) · "
        f"**Free Cash:** `${st['free_cash_usd']:,.2f}`",
        f"- **PnL Attribution:** Funding `${st['cum_funding_usd']:+.2f}` · HLP `${st['cum_hlp_yield_usd']:+.2f}` · "
        f"Basis MTM `${st['cum_basis_mtm_usd']:+.2f}` · Costs `-${st['cum_costs_usd']:.2f}`",
        f"- **Risk Telemetry:** Leverage `{rt.get('gross_leverage', 0):.2f}x` "
        f"(30D avg `{rt.get('leverage_30d_avg', 0):.2f}x` / max `{rt.get('leverage_30d_max', 0):.2f}x`) · "
        f"Net Delta `{rt.get('net_delta_pct', 0):+.3f}%` · Min Perp Liq Dist `{rt.get('min_perp_liq_distance_pct', 100):.1f}%`",
        "",
    ]
    if positions:
        lines += [
            "| Asset | Spot Pair | Tier | Notional | Perp Lev | Live APR | Funding PnL | Net PnL | Hours |",
            "|---|---|---|---:|---:|---:|---:|---:|---:|",
        ]
        for p in sorted(positions, key=lambda x: -float(x.get("perp_notional_usd", 0.0))):
            lines.append(
                f"| **{p['base_symbol']}** ({p['spot_token']}) | `{p['spot_pair']}` | {p['asset_tier']} | "
                f"${p['perp_notional_usd']:,.0f} | {p['effective_perp_leverage']:.2f}x | "
                f"{p['live_funding_apr_pct']:+.2f}% | ${p['cum_funding_usd']:+.2f} | "
                f"${p['net_pnl_usd']:+.2f} | {p['hours_held']:.1f}h |"
            )
    else:
        lines.append("_No open Spot+Perp slots yet (waiting for clean persistence streak or eligible funding)._")
    return "\n".join(lines) + "\n"


def format_spot_perp_paper_telegram_rich_html(result: dict[str, Any]) -> str:
    st = result["state"]
    rt = st.get("risk_telemetry") or {}
    pos_cnt = len(st.get("positions") or {})
    return (
        f"<h1>⚡ HyperCore Spot+Perp + 10% HLP · {html.escape(str(st.get('observed_at_utc', '')))}</h1>"
        f"<p>Equity: <b>${st['equity_usd']:,.2f}</b> ({st['return_pct']:+.3f}%) · "
        f"Blended APR: <b>{st.get('blended_portfolio_apr_pct', 0.0):+.2f}%</b> · "
        f"Slots: <b>{pos_cnt}</b> · HLP: <b>${st['hlp_reserve_usd']:,.0f}</b> ({st['hlp_apr_pct']:+.1f}%)</p>"
        f"<p>Leverage: <b>{rt.get('gross_leverage', 0):.2f}x</b> (30D max {rt.get('leverage_30d_max', 0):.2f}x) · "
        f"Delta: {rt.get('net_delta_pct', 0):+.3f}% · Funding ${st['cum_funding_usd']:+.2f} · HLP ${st['cum_hlp_yield_usd']:+.2f}</p>"
    )
