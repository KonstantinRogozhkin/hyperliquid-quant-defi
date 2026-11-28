"""Hyperliquid-native structural alpha: On-Chain Whale Liquidation Heatmap & Hourly Funding Microstructure.

100% read-only public analytics exploiting two unique Hyperliquid L1 mechanics unavailable on CEXs:
1. **Transparent L1 Whale Liquidation Heatmap**:
   Unlike Binance/Bybit, every wallet's open positions, leverage, and exact ``liquidationPx`` are
   public on Hyperliquid. Scans active top leaderboard whales concurrently, aggregates notional
   liquidation walls into distance buckets (``0-2%``, ``2-5%``, ``5-10%`` above/below mark), and
   identifies directional liquidation cascade / squeeze magnets.
2. **Hourly ``:00`` Funding Settlement Microstructure (``T-5m -> T+1m / T+5m``)**:
   Hyperliquid settles funding every 1 hour (vs 8h on CEXs). Pulls live 1-minute ``candleSnapshot``
   bars across recent hourly settlements on extreme-funding markets (``|APR| >= 30%``) to measure
   pre-funding crowding drift, immediate post-settlement snapback, and net funding-capture edge.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
import os
from pathlib import Path
import time
from typing import Any

import httpx
import numpy as np

HYPERCORE_INFO_URL = "https://api.hyperliquid.xyz/info"
HYPERCORE_LEADERBOARD_URL = "https://stats-data.hyperliquid.xyz/Mainnet/leaderboard"


# Machine-facing enums (JSON/API). Human-readable Russian labels are applied only in renderers.
CASCADE_DOWNSIDE = "downside_cascade"
CASCADE_UPSIDE = "upside_squeeze"
CASCADE_BALANCED = "balanced"
_CASCADE_LABELS_RU = {
    CASCADE_DOWNSIDE: "Downside Cascade Magnet (Лонг-ликвидации)",
    CASCADE_UPSIDE: "Upside Squeeze Magnet (Шорт-ликвидации)",
    CASCADE_BALANCED: "Сбалансировано",
}
_SIDE_LABELS_RU = {"short": "Шорт", "long": "Лонг"}
# Distance weights for the 0-2 / 2-5 / 5-10% liquidation bands in cascade_bias.
BIAS_W_0_2 = 1.0
BIAS_W_2_5 = 0.6
BIAS_W_5_10 = 0.3


def cascade_bias_label(bias: object) -> str:
    return _CASCADE_LABELS_RU.get(str(bias), str(bias))


def side_label(side: object) -> str:
    return _SIDE_LABELS_RU.get(str(side), str(side))


@dataclass(frozen=True, slots=True)
class WhaleLiqPosition:
    wallet: str
    coin: str
    side: str  # "long" or "short"
    notional_usd: float
    entry_px: float
    mark_px: float
    liq_px: float
    dist_pct: float  # signed distance: negative = below mark (long liq), positive = above mark (short liq)
    leverage_mode: str


@dataclass(frozen=True, slots=True)
class CoinLiquidationCluster:
    coin: str
    mark_px: float
    whales_count: int
    long_liq_0_2_usd: float
    long_liq_2_5_usd: float
    long_liq_5_10_usd: float
    short_liq_0_2_usd: float
    short_liq_2_5_usd: float
    short_liq_5_10_usd: float
    total_0_10_usd: float
    nearest_long_liq_px: float | None
    nearest_long_dist_pct: float | None
    nearest_short_liq_px: float | None
    nearest_short_dist_pct: float | None
    cascade_bias: str


@dataclass(frozen=True, slots=True)
class HourlyFundingMicroRow:
    coin: str
    mark_px: float
    open_interest_usd: float
    funding_hourly_bps: float
    funding_apr_pct: float
    settlements_measured: int
    avg_pre_5m_bps: float  # signed in funding-receiver direction: T-5m -> T0
    avg_post_1m_bps: float  # signed in funding-receiver direction: T0 -> T+1m
    avg_post_5m_bps: float  # signed in funding-receiver direction: T0 -> T+5m
    # Hold T-5m -> T+1m / T+5m in the receiver direction of EACH settlement's own rate:
    # mean(|f_t| + pre_t + post_t) - 3.0 (2x maker). The pre-settlement drift is paid to get in.
    net_capture_1m_bps: float
    net_capture_5m_bps: float
    receiver_side: str  # machine enum for the CURRENT rate: "short" (funding > 0) or "long" (< 0)
    # Settlements priced with their historical rate (``fundingHistory``) vs the current-rate fallback.
    settlements_with_history: int = 0


@dataclass(frozen=True, slots=True)
class LiquidationAndFundingAlphaSnapshot:
    observed_at_ms: int
    whales_scanned: int
    whales_with_positions: int
    liq_positions_count: int
    clusters: list[CoinLiquidationCluster]
    funding_micro: list[HourlyFundingMicroRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "whales_scanned": self.whales_scanned,
            "whales_with_positions": self.whales_with_positions,
            "liq_positions_count": self.liq_positions_count,
            "clusters": [asdict(c) for c in self.clusters],
            "funding_micro": [asdict(f) for f in self.funding_micro],
        }


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


# --------------------------------------------------------------------------- Whale Liquidation Heatmap


def select_active_whale_wallets(
    leaderboard_rows: list[dict[str, Any]],
    *,
    top_n: int = 50,
    min_equity_usd: float = 150_000.0,
    max_directional_equity_usd: float = 35_000_000.0,
) -> list[str]:
    """Select active whales biased toward leveraged directional traders (who create real liq walls).

    Pure top-volume ranking over-indexes on delta-neutral HFT market makers (low leverage, no
    near-mark ``liquidationPx``). Blend ~75% high-ROI directional whales ($150k-$35M equity)
    with ~25% largest active high-volume accounts ($500k+ equity) without duplicates.
    """
    directional: list[tuple[float, float, str]] = []
    high_vol: list[tuple[float, float, str]] = []
    for r in leaderboard_rows:
        addr = str(r.get("ethAddress") or "").strip()
        eq = _f(r.get("accountValue"))
        if not addr or eq < min_equity_usd:
            continue
        day_vol = 0.0
        week_vol = 0.0
        day_roi = 0.0
        week_roi = 0.0
        for win_name, win_data in r.get("windowPerformances") or []:
            if not isinstance(win_data, dict):
                continue
            if win_name == "day":
                day_vol = _f(win_data.get("vlm"))
                day_roi = abs(_f(win_data.get("roi")))
            elif win_name == "week":
                week_vol = _f(win_data.get("vlm"))
                week_roi = abs(_f(win_data.get("roi")))
        act_vol = max(day_vol, week_vol)
        if act_vol <= 0:
            continue
        if day_vol >= 200_000.0 and eq <= max_directional_equity_usd:
            roi_score = (day_roi * 3.0 + week_roi) * math.sqrt(eq)
            if roi_score > 0:
                directional.append((roi_score, eq, addr))
        if eq >= 500_000.0:
            high_vol.append((act_vol, eq, addr))

    directional.sort(key=lambda x: (-x[0], -x[1]))
    high_vol.sort(key=lambda x: (-x[0], -x[1]))

    dir_quota = max(1, int(round(top_n * 0.75)))
    selected: list[str] = []
    seen: set[str] = set()
    for _, _, addr in directional:
        if len(selected) >= dir_quota:
            break
        if addr not in seen:
            seen.add(addr)
            selected.append(addr)
    for _, _, addr in high_vol:
        if len(selected) >= top_n:
            break
        if addr not in seen:
            seen.add(addr)
            selected.append(addr)
    # Fill any remainder from directional if high_vol had fewer entries
    for _, _, addr in directional:
        if len(selected) >= top_n:
            break
        if addr not in seen:
            seen.add(addr)
            selected.append(addr)
    return selected[:top_n]


def extract_whale_liquidation_positions(
    wallet: str,
    ch_state: dict[str, Any],
    mark_prices: dict[str, float],
    *,
    max_dist_pct: float = 25.0,
    min_notional_usd: float = 25_000.0,
) -> list[WhaleLiqPosition]:
    """Extract active positions with live ``liquidationPx`` within ``max_dist_pct`` of mark price."""
    out: list[WhaleLiqPosition] = []
    for entry in ch_state.get("assetPositions") or []:
        p = entry.get("position") or {}
        coin = str(p.get("coin") or "").strip()
        szi = _f(p.get("szi"))
        liq_px = _f(p.get("liquidationPx"))
        mark = mark_prices.get(coin, _f(p.get("entryPx")))
        notional = abs(_f(p.get("positionValue"), abs(szi) * mark))
        if not coin or abs(szi) <= 1e-12 or liq_px <= 0 or mark <= 0 or notional < min_notional_usd:
            continue
        dist_pct = (liq_px - mark) / mark * 100.0
        if abs(dist_pct) > max_dist_pct:
            continue
        side = "long" if szi > 0 else "short"
        # Sanity: long liq must be below mark, short liq above mark
        if (side == "long" and liq_px >= mark) or (side == "short" and liq_px <= mark):
            continue
        lev = p.get("leverage") if isinstance(p.get("leverage"), dict) else {}
        lev_mode = f"{lev.get('type', 'cross')}:{lev.get('value', '')}"
        out.append(
            WhaleLiqPosition(
                wallet=wallet,
                coin=coin,
                side=side,
                notional_usd=notional,
                entry_px=_f(p.get("entryPx")),
                mark_px=mark,
                liq_px=liq_px,
                dist_pct=round(dist_pct, 2),
                leverage_mode=lev_mode,
            )
        )
    return out


def aggregate_liquidation_clusters(positions: list[WhaleLiqPosition], mark_prices: dict[str, float]) -> list[CoinLiquidationCluster]:
    """Aggregate whale liquidation walls per coin into 0-2%, 2-5%, 5-10% distance buckets."""
    by_coin: dict[str, list[WhaleLiqPosition]] = {}
    for pos in positions:
        by_coin.setdefault(pos.coin, []).append(pos)

    clusters: list[CoinLiquidationCluster] = []
    for coin, plist in by_coin.items():
        mark = mark_prices.get(coin, plist[0].mark_px)
        l02 = l25 = l510 = s02 = s25 = s510 = 0.0
        long_dists: list[tuple[float, float]] = []
        short_dists: list[tuple[float, float]] = []
        wallets = set()
        for pos in plist:
            wallets.add(pos.wallet)
            ad = abs(pos.dist_pct)
            if pos.side == "long":
                long_dists.append((ad, pos.liq_px))
                if ad <= 2.0:
                    l02 += pos.notional_usd
                elif ad <= 5.0:
                    l25 += pos.notional_usd
                elif ad <= 10.0:
                    l510 += pos.notional_usd
            else:
                short_dists.append((ad, pos.liq_px))
                if ad <= 2.0:
                    s02 += pos.notional_usd
                elif ad <= 5.0:
                    s25 += pos.notional_usd
                elif ad <= 10.0:
                    s510 += pos.notional_usd

        tot_0_10 = l02 + l25 + l510 + s02 + s25 + s510
        if tot_0_10 <= 0:
            continue
        # Distance-weighted over the SAME 0-10% band that the H2 alert fires on, so a wall that is
        # 100% long liquidations at 5-10% can no longer be reported as "balanced".
        long_near = l02 * BIAS_W_0_2 + l25 * BIAS_W_2_5 + l510 * BIAS_W_5_10
        short_near = s02 * BIAS_W_0_2 + s25 * BIAS_W_2_5 + s510 * BIAS_W_5_10
        if long_near > short_near * 1.5 and long_near >= 250_000:
            bias = CASCADE_DOWNSIDE
        elif short_near > long_near * 1.5 and short_near >= 250_000:
            bias = CASCADE_UPSIDE
        else:
            bias = CASCADE_BALANCED

        long_dists.sort()
        short_dists.sort()
        clusters.append(
            CoinLiquidationCluster(
                coin=coin,
                mark_px=mark,
                whales_count=len(wallets),
                long_liq_0_2_usd=round(l02, 0),
                long_liq_2_5_usd=round(l25, 0),
                long_liq_5_10_usd=round(l510, 0),
                short_liq_0_2_usd=round(s02, 0),
                short_liq_2_5_usd=round(s25, 0),
                short_liq_5_10_usd=round(s510, 0),
                total_0_10_usd=round(tot_0_10, 0),
                nearest_long_liq_px=round(long_dists[0][1], 5) if long_dists else None,
                nearest_long_dist_pct=round(long_dists[0][0], 2) if long_dists else None,
                nearest_short_liq_px=round(short_dists[0][1], 5) if short_dists else None,
                nearest_short_dist_pct=round(short_dists[0][0], 2) if short_dists else None,
                cascade_bias=bias,
            )
        )
    clusters.sort(key=lambda c: -c.total_0_10_usd)
    return clusters


# --------------------------------------------------------------------------- Hourly :00 Funding Microstructure


def parse_funding_history(rows: Any) -> dict[int, float]:
    """``fundingHistory`` rows -> ``{settlement_hour_ms: rate}`` (times rounded to the nearest hour)."""
    out: dict[int, float] = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        t_ms = int(_f(row.get("time")))
        rate = _f(row.get("fundingRate"), float("nan"))
        if t_ms <= 0 or not math.isfinite(rate):
            continue
        out[int(round(t_ms / 3_600_000)) * 3_600_000] = rate
    return out


def compute_hourly_settlement_microstructure(
    candles_1m: list[dict[str, Any]],
    *,
    coin: str,
    mark_px: float,
    open_interest_usd: float,
    funding_hourly: float,
    round_trip_maker_bps: float = 3.0,
    funding_history: dict[int, float] | None = None,
) -> HourlyFundingMicroRow | None:
    """Measure T-5m -> T0 drift and T0 -> T+1m / T+5m snapback around hourly ``:00`` funding settlements.

    Each settlement is evaluated with ITS OWN rate from ``funding_history`` (``{settle_hour_ms:
    rate}``; falls back to the current ``funding_hourly`` when missing): returns are signed in that
    settlement's funding-receiver direction. Collecting the payment requires holding through T0, so
    the capture of one settlement is ``|f_t| + pre_t + post_t`` (entry at T-5m, exit at T+1m/T+5m)
    minus ``round_trip_maker_bps``. The previous formula ignored the pre-settlement drift and applied
    the current rate/side to every past settlement (look-ahead).
    """
    if not candles_1m:
        return None
    by_min: dict[int, tuple[float, float]] = {}  # minute_ts_ms -> (open, close)
    for c in candles_1m:
        t_ms = int(c.get("t", 0))
        o = _f(c.get("o"))
        cl = _f(c.get("c"))
        if t_ms > 0 and o > 0 and cl > 0:
            by_min[t_ms] = (o, cl)
    if len(by_min) < 20:
        return None

    # Current-rate receiver (display only); each settlement below uses its own historical sign.
    receiver_side = "short" if funding_hourly > 0 else "long"
    fund_bps = funding_hourly * 1e4
    fund_apr = funding_hourly * 24.0 * 365.0 * 100.0

    pre_5m: list[float] = []
    post_1m: list[float] = []
    post_5m: list[float] = []
    cap_1m: list[float] = []
    cap_5m: list[float] = []
    with_history = 0
    hist = funding_history or {}

    hour_ms = 3_600_000
    min_ms = 60_000
    for ts in sorted(by_min.keys()):
        if ts % hour_ms != 0:
            continue
        p0_open = by_min[ts][0]
        p_pre = by_min.get(ts - 5 * min_ms)
        p_post1 = by_min.get(ts)  # close of :00 candle == T+1m
        p_post5 = by_min.get(ts + 4 * min_ms)  # close of :04 candle == T+5m
        if p_pre and p_post1 and p_post5:
            rate_t = hist.get(ts)
            if rate_t is not None and math.isfinite(rate_t):
                with_history += 1
            else:
                rate_t = funding_hourly
            sgn_t = -1.0 if rate_t > 0 else 1.0
            pre = sgn_t * (p0_open - p_pre[0]) / p_pre[0] * 1e4
            p1 = sgn_t * (p_post1[1] - p0_open) / p0_open * 1e4
            p5 = sgn_t * (p_post5[1] - p0_open) / p0_open * 1e4
            pre_5m.append(pre)
            post_1m.append(p1)
            post_5m.append(p5)
            cap_1m.append(abs(rate_t) * 1e4 + pre + p1)
            cap_5m.append(abs(rate_t) * 1e4 + pre + p5)

    if not post_1m:
        return None
    avg_pre = float(np.mean(pre_5m))
    avg_p1 = float(np.mean(post_1m))
    avg_p5 = float(np.mean(post_5m))
    return HourlyFundingMicroRow(
        coin=coin,
        mark_px=mark_px,
        open_interest_usd=round(open_interest_usd, 0),
        funding_hourly_bps=round(fund_bps, 3),
        funding_apr_pct=round(fund_apr, 1),
        settlements_measured=len(post_1m),
        avg_pre_5m_bps=round(avg_pre, 2),
        avg_post_1m_bps=round(avg_p1, 2),
        avg_post_5m_bps=round(avg_p5, 2),
        net_capture_1m_bps=round(float(np.mean(cap_1m)) - round_trip_maker_bps, 2),
        net_capture_5m_bps=round(float(np.mean(cap_5m)) - round_trip_maker_bps, 2),
        receiver_side=receiver_side,
        settlements_with_history=with_history,
    )


# --------------------------------------------------------------------------- Live Async Orchestrator


DEFAULT_TOP_WHALES = 60


async def run_liquidation_and_funding_alpha_scan(
    *,
    top_whales: int = DEFAULT_TOP_WHALES,
    top_funding_coins: int = 8,
    min_oi_usd: float = 2_000_000.0,
    lookback_hours: int = 6,
    info_url: str = HYPERCORE_INFO_URL,
    leaderboard_url: str = HYPERCORE_LEADERBOARD_URL,
    whale_cache_path: Path | None = None,
    whale_cache_ttl_sec: int = 6 * 3600,
) -> LiquidationAndFundingAlphaSnapshot:
    """Run live read-only Liquidation Heatmap + Hourly Funding Microstructure scan.

    When ``whale_cache_path`` is provided and fresher than ``whale_cache_ttl_sec``, reuses the
    cached active whale address list instead of re-downloading the ~38 MB leaderboard JSON.
    """
    now_ms = int(time.time() * 1000)
    cached_wallets: list[str] | None = None
    if whale_cache_path is not None and whale_cache_path.is_file():
        try:
            cache_obj = json.loads(whale_cache_path.read_text(encoding="utf-8"))
            cached_at = int(cache_obj.get("cached_at_ms", 0))
            w_list = cache_obj.get("wallets") or []
            if (
                isinstance(w_list, list)
                and len(w_list) >= top_whales
                and (now_ms - cached_at) <= whale_cache_ttl_sec * 1000
            ):
                cached_wallets = [str(w) for w in w_list[:top_whales]]
        except Exception:
            cached_wallets = None

    async with httpx.AsyncClient(timeout=25.0) as client:
        async def _req_with_retry(
            method: str,
            url: str,
            *,
            json_body: dict[str, Any] | None = None,
            max_attempts: int = 3,
        ) -> httpx.Response | None:
            last_resp: httpx.Response | None = None
            for attempt in range(max_attempts):
                try:
                    resp = (
                        await client.post(url, json=json_body)
                        if method == "POST"
                        else await client.get(url)
                    )
                    last_resp = resp
                    if resp.status_code not in (429, 500, 502, 503, 504):
                        return resp
                except httpx.HTTPError:
                    pass
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.8 * (attempt + 1))
            return last_resp

        # 1. Fetch perp mark prices + funding + OI (and leaderboard only on cache miss)
        if cached_wallets is not None:
            perp_resp = await _req_with_retry("POST", info_url, json_body={"type": "metaAndAssetCtxs"})
            if perp_resp is None:
                raise RuntimeError("Failed to fetch metaAndAssetCtxs after retries")
            perp_resp.raise_for_status()
            wallets = cached_wallets
        else:
            perp_task = _req_with_retry("POST", info_url, json_body={"type": "metaAndAssetCtxs"})
            lb_task = _req_with_retry("GET", leaderboard_url)
            perp_resp, lb_resp = await asyncio.gather(perp_task, lb_task)
            if perp_resp is None or lb_resp is None:
                raise RuntimeError("Failed to fetch Hyperliquid meta/leaderboard after retries")
            perp_resp.raise_for_status()
            lb_resp.raise_for_status()
            lb_rows = (lb_resp.json() or {}).get("leaderboardRows") or []
            wallets = select_active_whale_wallets(lb_rows, top_n=top_whales)
            if whale_cache_path is not None and wallets:
                whale_cache_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_c = whale_cache_path.with_name(
                    f".{whale_cache_path.name}.{os.getpid()}.{time.time_ns()}.tmp"
                )
                tmp_c.write_text(
                    json.dumps({"cached_at_ms": now_ms, "wallets": wallets}, indent=2),
                    encoding="utf-8",
                )
                tmp_c.replace(whale_cache_path)

        p_meta, p_ctxs = perp_resp.json()
        mark_prices: dict[str, float] = {}
        perp_info: list[dict[str, Any]] = []
        for u, ctx in zip(p_meta.get("universe") or [], p_ctxs or [], strict=False):
            coin = str(u.get("name") or "").strip()
            mark = _f(ctx.get("markPx"))
            if not coin or mark <= 0:
                continue
            mark_prices[coin] = mark
            oi_usd = _f(ctx.get("openInterest")) * mark
            perp_info.append({"coin": coin, "mark_px": mark, "oi_usd": oi_usd, "funding": _f(ctx.get("funding"))})

        # 2. Fetch whale clearinghouseStates concurrently with rate-friendly semaphore + 429 retry
        sem = asyncio.Semaphore(6)

        async def _fetch_wallet(w: str) -> tuple[str, dict[str, Any]]:
            async with sem:
                try:
                    r = await _req_with_retry("POST", info_url, json_body={"type": "clearinghouseState", "user": w})
                    if r is not None and r.status_code == 200:
                        return w, r.json() or {}
                except Exception:
                    pass
                return w, {}

        whale_states = await asyncio.gather(*[_fetch_wallet(w) for w in wallets])

        all_liq_pos: list[WhaleLiqPosition] = []
        whales_with_pos = 0
        for w, st in whale_states:
            if st.get("assetPositions"):
                whales_with_pos += 1
            all_liq_pos.extend(extract_whale_liquidation_positions(w, st, mark_prices))
        clusters = aggregate_liquidation_clusters(all_liq_pos, mark_prices)

        # 3. Fetch 1m candles over last `lookback_hours` for top extreme-funding markets
        liquid_perps = [p for p in perp_info if p["oi_usd"] >= min_oi_usd and abs(p["funding"]) > 1e-6]
        liquid_perps.sort(key=lambda p: -abs(p["funding"]))
        ext_targets = liquid_perps[:top_funding_coins]
        start_ms = now_ms - (lookback_hours + 1) * 3_600_000

        async def _fetch_candles(p: dict[str, Any]) -> HourlyFundingMicroRow | None:
            async with sem:
                try:
                    r = await _req_with_retry(
                        "POST",
                        info_url,
                        json_body={"type": "candleSnapshot", "req": {"coin": p["coin"], "interval": "1m", "startTime": start_ms, "endTime": now_ms}},
                    )
                    if r is None or r.status_code != 200:
                        return None
                    fh = await _req_with_retry(
                        "POST",
                        info_url,
                        json_body={"type": "fundingHistory", "coin": p["coin"], "startTime": start_ms, "endTime": now_ms},
                    )
                    history = parse_funding_history(fh.json() if fh is not None and fh.status_code == 200 else [])
                    return compute_hourly_settlement_microstructure(
                        r.json() or [],
                        coin=p["coin"],
                        mark_px=p["mark_px"],
                        open_interest_usd=p["oi_usd"],
                        funding_hourly=p["funding"],
                        funding_history=history,
                    )
                except Exception:
                    return None

        micro_raw = await asyncio.gather(*[_fetch_candles(p) for p in ext_targets])
        funding_micro = sorted(
            [m for m in micro_raw if m is not None],
            key=lambda m: -m.net_capture_5m_bps,
        )

    return LiquidationAndFundingAlphaSnapshot(
        observed_at_ms=now_ms,
        whales_scanned=len(wallets),
        whales_with_positions=whales_with_pos,
        liq_positions_count=len(all_liq_pos),
        clusters=clusters,
        funding_micro=funding_micro,
    )


# --------------------------------------------------------------------------- Reporting


def format_liq_funding_markdown(snap: LiquidationAndFundingAlphaSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# Hyperliquid On-Chain Liquidation Heatmap & Hourly Funding Microstructure ({ts})",
        "",
        f"- Просканировано активных топ-китов: **{snap.whales_scanned}** (с открытыми позициями: **{snap.whales_with_positions}**, позиций с `liqPx ≤ 25%`: **{snap.liq_positions_count}**)",
        "",
        "## 1. Карта ликвидаций китов (0–10% от текущей цены Mark)",
        "",
        "| Монета | Mark | Китов | Лонг ликв. 0–2% / 2–5% | Шорт ликв. 0–2% / 2–5% | Всего 0–10% | Ближ. стенка | Магнит каскада |",
        "|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for c in snap.clusters[:12]:
        near_parts = []
        if c.nearest_long_dist_pct is not None:
            near_parts.append(f"L −{c.nearest_long_dist_pct:.1f}%")
        if c.nearest_short_dist_pct is not None:
            near_parts.append(f"S +{c.nearest_short_dist_pct:.1f}%")
        near_txt = " / ".join(near_parts) if near_parts else "—"
        lines.append(
            f"| **{c.coin}** | {c.mark_px:,.5g} | {c.whales_count} | "
            f"${c.long_liq_0_2_usd/1e6:,.2f}M / ${c.long_liq_2_5_usd/1e6:,.2f}M | "
            f"${c.short_liq_0_2_usd/1e6:,.2f}M / ${c.short_liq_2_5_usd/1e6:,.2f}M | "
            f"**${c.total_0_10_usd/1e6:,.2f}M** | {near_txt} | {cascade_bias_label(c.cascade_bias)} |"
        )

    lines += [
        "",
        "## 2. Часовая микроструктура расчёта фандинга (`:00` UTC · T−5m → T+1m / T+5m)",
        "",
        "| Монета | Сторона | Фандинг / ч | APR | Окон | Pre 5m | Post +1m | Post +5m | Net +1m (после 3 б.п.) | Net +5m |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for f in snap.funding_micro:
        dot = "🟢" if f.net_capture_5m_bps > 0 else "⚪"
        lines.append(
            f"| **{f.coin}** | {side_label(f.receiver_side)} | {f.funding_hourly_bps:+.3f} б.п. | **{f.funding_apr_pct:+.1f}%** | "
            f"{f.settlements_measured} | {f.avg_pre_5m_bps:+.2f} | {f.avg_post_1m_bps:+.2f} | {f.avg_post_5m_bps:+.2f} | "
            f"{f.net_capture_1m_bps:+.2f} б.п. | {dot} **{f.net_capture_5m_bps:+.2f} б.п.** |"
        )
    return "\n".join(lines) + "\n"


def format_liq_funding_telegram_rich_html(snap: LiquidationAndFundingAlphaSnapshot) -> str:
    dt = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    parts = [
        f"<h1>🧲 Карта ликвидаций и фандинг :00 · {html.escape(date_str)}</h1>",
        f"<p><b>Hyperliquid L1 ({html.escape(time_str)}):</b> просканировано <b>{snap.whales_scanned}</b> активных топ-китов, найдено <b>{snap.liq_positions_count}</b> зон ликвидации (≤25%).</p>",
    ]
    if snap.clusters:
        rows = [
            "<tr>"
            '<th align="left">Монета</th>'
            '<th align="right">Лонг 0–5%</th>'
            '<th align="right">Шорт 0–5%</th>'
            '<th align="left">Магнит</th>'
            "</tr>"
        ]
        for c in snap.clusters[:6]:
            l05 = (c.long_liq_0_2_usd + c.long_liq_2_5_usd) / 1e6
            s05 = (c.short_liq_0_2_usd + c.short_liq_2_5_usd) / 1e6
            tag = "🔴 Вниз" if c.cascade_bias == CASCADE_DOWNSIDE else ("🟢 Вверх" if c.cascade_bias == CASCADE_UPSIDE else "⚪ Баланс")
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(c.coin)}</b></td>'
                f'<td align="right">${l05:,.2f}M</td>'
                f'<td align="right">${s05:,.2f}M</td>'
                f'<td align="left">{tag}</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    if snap.funding_micro:
        f_rows = [
            "<tr>"
            '<th align="left">Фандинг :00</th>'
            '<th align="right">APR</th>'
            '<th align="right">Post +5m</th>'
            '<th align="right">Net +5m</th>'
            "</tr>"
        ]
        for f in snap.funding_micro[:5]:
            dot = "🟢" if f.net_capture_5m_bps > 0 else "⚪"
            f_rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(f.coin)}</b> ({html.escape(side_label(f.receiver_side))})</td>'
                f'<td align="right">{f.funding_apr_pct:+.0f}%</td>'
                f'<td align="right">{f.avg_post_5m_bps:+.1f} б.п.</td>'
                f'<td align="right">{dot} {f.net_capture_5m_bps:+.1f} б.п.</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(f_rows) + "</table>")

    parts.append("<footer>Hyperliquid L1 прозрачные позиции китов + 1m свечи расчёта фандинга :00</footer>")
    return "".join(parts)
