"""Causal multi-week/multi-month empirical study of Hyperliquid hourly ``:00`` funding settlements.

100% read-only research over public Hyperliquid Mainnet ``fundingHistory`` (paginated) and
``candleSnapshot`` bars:
* ``5m`` interval: ~5,000 bars = **17.4 days** (~416 hourly settlements per coin).
* ``15m`` interval: ~5,000 bars = **52.1 days** (~1,250 hourly settlements per coin) across 100+ coins
  (~100,000+ settlement events), specifically designed to test whether ``APR >= +25%`` holds out-of-sample
  across regimes and without single-coin outliers (**Ex-Top1-Coin audit**).

Zero lookahead:
* Signal for settlement hour ``T0`` uses strictly the *prior* settled funding rate ``f(T0 - 1h)``
  (already public 1 hour before ``T0``) plus optional pre-settlement return observed *before* ``T0``
  (strictly restricted to post-``:00`` strategies).
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import math
import time
from typing import Any

import httpx
import numpy as np
import pandas as pd

HYPERCORE_INFO_URL = "https://api.hyperliquid.xyz/info"
HOUR_MS = 3_600_000
FIVE_MIN_MS = 300_000
FIFTEEN_MIN_MS = 900_000


@dataclass(frozen=True, slots=True)
class StrategyBucketStats:
    strategy: str
    bucket: str
    split: str  # "dev" or "holdout"
    events: int
    mean_gross_bps: float
    std_bps: float
    t_stat: float
    win_rate_pct: float
    net_maker_bps: float  # mean_gross_bps - maker_rt_bps (default 3.0 = 2x 1.5 bps maker)
    net_taker_bps: float  # mean_gross_bps - 9.0 (2x 4.5 bps taker)
    sharpe_event: float  # per-event mean / std (net maker)
    top1_coin: str = ""
    ex_top1_gross_bps: float = 0.0
    ex_top1_t_stat: float = 0.0
    # Settlements at the same :00 timestamp across correlated coins are not independent draws:
    # t-stat on per-timestamp cross-sectional means (cluster-robust by settlement hour).
    t_stat_clustered: float = 0.0
    clusters: int = 0
    maker_rt_bps: float = 3.0
    # Net after a stressed passive-execution cost (live alt fills measured ~5 bps one-way).
    net_maker_stress_bps: float = 0.0
    stress_rt_bps: float = 10.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _index_funding_by_hour(funding_history: list[dict[str, Any]]) -> dict[int, float]:
    fund_by_hour: dict[int, float] = {}
    for item in funding_history:
        t = int(item.get("time", 0))
        rate = _f(item.get("fundingRate"))
        if t > 0:
            h_ts = int(round(t / HOUR_MS) * HOUR_MS)
            fund_by_hour[h_ts] = rate
    return fund_by_hour


def build_coin_settlement_events(
    coin: str,
    candles_5m: list[dict[str, Any]],
    funding_history: list[dict[str, Any]],
    *,
    day_vol_usd: float = 0.0,
    oi_usd: float = 0.0,
) -> pd.DataFrame:
    """Align 5-minute OHLC bars with causal prior-hour funding ``f(T0 - 1h)`` and settled ``f(T0)``."""
    if not candles_5m or not funding_history:
        return pd.DataFrame()

    bars: dict[int, tuple[float, float]] = {}
    for c in candles_5m:
        t = int(c.get("t", 0))
        o, cl = _f(c.get("o")), _f(c.get("c"))
        if t > 0 and o > 0 and cl > 0:
            bars[t] = (o, cl)

    fund_by_hour = _index_funding_by_hour(funding_history)
    rows: list[dict[str, Any]] = []
    for t0 in sorted(bars.keys()):
        if t0 % HOUR_MS != 0:
            continue
        prev_rate = fund_by_hour.get(t0 - HOUR_MS)
        curr_rate = fund_by_hour.get(t0)
        if prev_rate is None or abs(prev_rate) < 1e-9:
            continue

        b_m15 = bars.get(t0 - 3 * FIVE_MIN_MS)
        b_m5 = bars.get(t0 - FIVE_MIN_MS)
        b_0 = bars.get(t0)
        b_p10 = bars.get(t0 + 2 * FIVE_MIN_MS)
        if not (b_m15 and b_m5 and b_0 and b_p10):
            continue

        p_m15, p_m5, p_0, p_p5, p_p15 = b_m15[0], b_m5[0], b_0[0], b_0[1], b_p10[1]
        if not all(math.isfinite(px) and px > 0 for px in (p_m15, p_m5, p_0, p_p5, p_p15)):
            continue
        recv_sgn = -1.0 if prev_rate > 0 else 1.0
        fade_sgn = -recv_sgn

        pre_5m_recv = recv_sgn * (p_0 - p_m5) / p_m5 * 1e4
        pre_15m_recv = recv_sgn * (p_0 - p_m15) / p_m15 * 1e4
        post_5m_fade = fade_sgn * (p_p5 - p_0) / p_0 * 1e4
        post_15m_fade = fade_sgn * (p_p15 - p_0) / p_0 * 1e4

        actual_fund_bps = (curr_rate if curr_rate is not None else prev_rate) * 1e4
        payout_bps = (-recv_sgn) * actual_fund_bps
        naive_harvest_5m = recv_sgn * (p_p5 - p_m5) / p_m5 * 1e4 + payout_bps

        # Reject only broken-feed >90% corrupt glitches; preserve genuine 5-20% :00 squeeze cascades.
        if max(abs(pre_5m_recv), abs(pre_15m_recv), abs(post_5m_fade), abs(post_15m_fade)) > 9000.0:
            continue

        rows.append(
            {
                "coin": coin,
                "t0_ms": t0,
                "prev_funding_bps": prev_rate * 1e4,
                "prev_apr_pct": prev_rate * 24.0 * 365.0 * 100.0,
                "curr_funding_bps": actual_fund_bps,
                "day_vol_usd": day_vol_usd,
                "oi_usd": oi_usd,
                "pre_crowding_5m": pre_5m_recv,
                "pre_crowding_15m": pre_15m_recv,
                "post_unwind_fade_5m": post_5m_fade,
                "post_unwind_fade_15m": post_15m_fade,
                "naive_harvest_5m": naive_harvest_5m,
            }
        )
    return pd.DataFrame(rows)


def build_coin_settlement_events_15m(
    coin: str,
    candles_15m: list[dict[str, Any]],
    funding_history: list[dict[str, Any]],
    *,
    day_vol_usd: float = 0.0,
    oi_usd: float = 0.0,
) -> pd.DataFrame:
    """Align 15-minute OHLC bars (~52.1 days) with causal prior-hour funding ``f(T0 - 1h)``."""
    if not candles_15m or not funding_history:
        return pd.DataFrame()

    bars: dict[int, tuple[float, float]] = {}
    for c in candles_15m:
        t = int(c.get("t", 0))
        o, cl = _f(c.get("o")), _f(c.get("c"))
        if t > 0 and o > 0 and cl > 0:
            bars[t] = (o, cl)

    fund_by_hour = _index_funding_by_hour(funding_history)
    rows: list[dict[str, Any]] = []
    for t0 in sorted(bars.keys()):
        if t0 % HOUR_MS != 0:
            continue
        prev_rate = fund_by_hour.get(t0 - HOUR_MS)
        curr_rate = fund_by_hour.get(t0)
        if prev_rate is None or abs(prev_rate) < 1e-9:
            continue

        b_m15 = bars.get(t0 - FIFTEEN_MIN_MS)
        b_0 = bars.get(t0)  # [T0, T0+15m)
        b_p15 = bars.get(t0 + FIFTEEN_MIN_MS)  # [T0+15m, T0+30m)
        if not (b_m15 and b_0 and b_p15):
            continue

        p_m15, p_0, p_p15, p_p30 = b_m15[0], b_0[0], b_0[1], b_p15[1]
        if not all(math.isfinite(px) and px > 0 for px in (p_m15, p_0, p_p15, p_p30)):
            continue
        recv_sgn = -1.0 if prev_rate > 0 else 1.0
        fade_sgn = -recv_sgn

        pre_15m_recv = recv_sgn * (p_0 - p_m15) / p_m15 * 1e4
        post_15m_fade = fade_sgn * (p_p15 - p_0) / p_0 * 1e4
        post_30m_fade = fade_sgn * (p_p30 - p_0) / p_0 * 1e4

        actual_fund_bps = (curr_rate if curr_rate is not None else prev_rate) * 1e4
        payout_bps = (-recv_sgn) * actual_fund_bps
        naive_harvest_15m = recv_sgn * (p_p15 - p_m15) / p_m15 * 1e4 + payout_bps

        # Reject only broken-feed >90% corrupt glitches; preserve genuine 5-20% :00 squeeze cascades.
        if max(abs(pre_15m_recv), abs(post_15m_fade), abs(post_30m_fade)) > 9000.0:
            continue

        rows.append(
            {
                "coin": coin,
                "t0_ms": t0,
                "prev_funding_bps": prev_rate * 1e4,
                "prev_apr_pct": prev_rate * 24.0 * 365.0 * 100.0,
                "curr_funding_bps": actual_fund_bps,
                "day_vol_usd": day_vol_usd,
                "oi_usd": oi_usd,
                "pre_crowding_15m": pre_15m_recv,
                "post_unwind_fade_15m": post_15m_fade,
                "post_unwind_fade_30m": post_30m_fade,
                "naive_harvest_15m": naive_harvest_15m,
            }
        )
    return pd.DataFrame(rows)


def _clustered_t_stat(sub: pd.DataFrame, strat: str) -> tuple[float, int]:
    """t-stat of the mean using one observation per settlement timestamp (cross-sectional mean)."""
    if "t0_ms" not in sub.columns or sub.empty:
        return 0.0, 0
    g = sub.groupby("t0_ms")[strat].mean().to_numpy(dtype=np.float64)
    n = len(g)
    if n < 2:
        return 0.0, n
    sd = float(np.std(g, ddof=1))
    return ((float(np.mean(g)) / (sd / math.sqrt(n))) if sd > 1e-9 else 0.0), n


MIN_COLLECTION_RATIO = 0.8


def evaluate_settlement_strategies(
    events_df: pd.DataFrame,
    *,
    dev_fraction: float = 0.65,
    maker_rt_bps: float = 3.0,
    taker_rt_bps: float = 9.0,
    stress_rt_bps: float = 10.0,
) -> pd.DataFrame:
    """Evaluate event strategies across Dev / Holdout splits, funding buckets, and Ex-Top1-Coin."""
    if events_df.empty:
        return pd.DataFrame()
    df = events_df.sort_values("t0_ms").reset_index(drop=True)
    unique_hours = np.sort(df["t0_ms"].unique())
    if len(unique_hours) < 10:
        cutoff_ms = int(unique_hours[len(unique_hours) // 2])
    else:
        cutoff_ms = int(unique_hours[int(len(unique_hours) * dev_fraction)])

    df = df.assign(split=np.where(df["t0_ms"] < cutoff_ms, "dev", "holdout"))

    is_5m = "post_unwind_fade_5m" in df.columns
    if is_5m:
        all_strats: tuple[str, ...] = (
            "pre_crowding_5m",
            "pre_crowding_15m",
            "post_unwind_fade_5m",
            "post_unwind_fade_15m",
            "naive_harvest_5m",
        )
        post_only_strats: tuple[str, ...] = ("post_unwind_fade_5m", "post_unwind_fade_15m")
        pre_col = "pre_crowding_5m"
        pre_label = "APR<=-50% + pre5m>0"
    else:
        all_strats = (
            "pre_crowding_15m",
            "post_unwind_fade_15m",
            "post_unwind_fade_30m",
            "naive_harvest_15m",
        )
        post_only_strats = ("post_unwind_fade_15m", "post_unwind_fade_30m")
        pre_col = "pre_crowding_15m"
        pre_label = "APR<=-50% + pre15m>0"

    buckets: list[tuple[str, pd.Series, tuple[str, ...]]] = [
        ("|APR|>=25% (all)", df["prev_apr_pct"].abs() >= 25.0, all_strats),
        ("|APR|>=50% (all)", df["prev_apr_pct"].abs() >= 50.0, all_strats),
        ("APR<=-25% (neg)", df["prev_apr_pct"] <= -25.0, all_strats),
        ("APR>=+25% (pos)", df["prev_apr_pct"] >= 25.0, all_strats),
        ("APR>=+50% (pos)", df["prev_apr_pct"] >= 50.0, all_strats),
        (pre_label, (df["prev_apr_pct"] <= -50.0) & (df[pre_col] > 0.0), post_only_strats),
    ]

    out_rows: list[dict[str, Any]] = []
    for b_name, mask, allowed_strats in buckets:
        sub_b = df[mask]
        if sub_b.empty:
            continue
        for split in ("dev", "holdout"):
            sub = sub_b[sub_b["split"] == split]
            if len(sub) < 5:
                continue
            for strat in allowed_strats:
                vals = sub[strat].to_numpy(dtype=np.float64)
                n = len(vals)
                mu = float(np.mean(vals))
                sd = float(np.std(vals, ddof=1)) if n > 1 else 0.0
                t_stat = (mu / (sd / math.sqrt(n))) if sd > 1e-9 else 0.0
                win_pct = float(np.mean(vals > 0.0) * 100.0)
                net_m = mu - maker_rt_bps
                net_t = mu - taker_rt_bps
                sh_ev = (net_m / sd) if sd > 1e-9 else 0.0
                t_cl, n_cl = _clustered_t_stat(sub, strat)

                # Ex-Top1-Coin robustness audit (protects against single-coin outliers like INIT)
                top1_coin = ""
                ex_mu = mu
                ex_t = t_stat
                if "coin" in sub.columns and sub["coin"].nunique() >= 2:
                    coin_contrib = sub.groupby("coin")[strat].sum()
                    top1_coin = str(coin_contrib.abs().idxmax())
                    ex_vals = sub[sub["coin"] != top1_coin][strat].to_numpy(dtype=np.float64)
                    if len(ex_vals) >= 2:
                        ex_mu = float(np.mean(ex_vals))
                        ex_sd = float(np.std(ex_vals, ddof=1))
                        ex_t = (ex_mu / (ex_sd / math.sqrt(len(ex_vals)))) if ex_sd > 1e-9 else 0.0

                out_rows.append(
                    StrategyBucketStats(
                        strategy=strat,
                        bucket=b_name,
                        split=split,
                        events=n,
                        mean_gross_bps=round(mu, 3),
                        std_bps=round(sd, 3),
                        t_stat=round(t_stat, 2),
                        win_rate_pct=round(win_pct, 1),
                        net_maker_bps=round(net_m, 3),
                        net_taker_bps=round(net_t, 3),
                        sharpe_event=round(sh_ev, 3),
                        top1_coin=top1_coin,
                        ex_top1_gross_bps=round(ex_mu, 3),
                        ex_top1_t_stat=round(ex_t, 2),
                        t_stat_clustered=round(t_cl, 2),
                        clusters=n_cl,
                        maker_rt_bps=float(maker_rt_bps),
                        net_maker_stress_bps=round(mu - stress_rt_bps, 3),
                        stress_rt_bps=float(stress_rt_bps),
                    ).to_dict()
                )
    return pd.DataFrame(out_rows)


# --------------------------------------------------------------------------- Live Mainnet Collector


async def fetch_settlement_dataset(
    *,
    bar_interval: str = "5m",
    max_coins: int = 45,
    min_oi_usd: float = 2_000_000.0,
    min_vol_usd: float = 500_000.0,
    lookback_days: int | None = None,
    info_url: str = HYPERCORE_INFO_URL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Concurrently collect 5m (~17.4d) or 15m (~52.1d) candles + paginated ``fundingHistory``."""
    iv = bar_interval if bar_interval in ("5m", "15m") else "5m"
    days = lookback_days if lookback_days is not None else (55 if iv == "15m" else 18)
    now_ms = int(time.time() * 1000)
    start_ms = now_ms - days * 24 * HOUR_MS

    async with httpx.AsyncClient(timeout=25.0) as client:
        r = await client.post(info_url, json={"type": "metaAndAssetCtxs"})
        r.raise_for_status()
        p_meta, p_ctxs = r.json()
        candidates: list[dict[str, Any]] = []
        for u, ctx in zip(p_meta.get("universe") or [], p_ctxs or [], strict=False):
            if u.get("isDelisted"):
                continue
            coin = str(u.get("name") or "").strip()
            mark = _f(ctx.get("markPx"))
            oi = _f(ctx.get("openInterest")) * mark
            vol = _f(ctx.get("dayNtlVlm"))
            fund = _f(ctx.get("funding"))
            if coin and mark > 0 and oi >= min_oi_usd and vol >= min_vol_usd:
                candidates.append({"coin": coin, "oi_usd": oi, "vol_usd": vol, "abs_fund": abs(fund)})

        # Rank strictly by liquidity (OI, then 24h volume) — never pre-select by current
        # end-of-window funding magnitude, which introduces lookahead selection bias.
        selected = sorted(candidates, key=lambda x: (-x["oi_usd"], -x["vol_usd"], x["coin"]))[:max_coins]

        # Hyperliquid IP budget is ~1,200 weight/min (weight 20 per candleSnapshot/fundingHistory
        # call). Pace requests gently and coordinate backoff across workers on HTTP 429 so all
        # requested coins collect cleanly instead of silently dropping half the universe.
        sem = asyncio.Semaphore(2)
        rate_lock = asyncio.Lock()
        next_allowed_at = 0.0
        min_request_interval_s = 0.25

        async def _wait_pacing_slot() -> None:
            nonlocal next_allowed_at
            async with rate_lock:
                now_mono = time.monotonic()
                wait_s = max(0.0, next_allowed_at - now_mono)
                next_allowed_at = max(now_mono, next_allowed_at) + min_request_interval_s
            if wait_s > 0:
                await asyncio.sleep(wait_s)

        async def _push_cooldown(delay_s: float) -> None:
            nonlocal next_allowed_at
            async with rate_lock:
                next_allowed_at = max(next_allowed_at, time.monotonic() + delay_s)

        async def _post_with_retry(payload: dict[str, Any], *, max_attempts: int = 6) -> Any:
            for attempt in range(max_attempts):
                await _wait_pacing_slot()
                try:
                    r_p = await client.post(info_url, json=payload)
                except httpx.HTTPError:
                    delay = min(15.0, 1.5 * (2**attempt))
                    await _push_cooldown(delay)
                    await asyncio.sleep(delay)
                    continue
                if r_p.status_code == 200:
                    return r_p.json() or []
                if r_p.status_code in (429, 500, 502, 503, 504):
                    retry_after = _f(r_p.headers.get("Retry-After"), 0.0)
                    delay = max(retry_after, min(20.0, 1.5 * (2**attempt)))
                    await _push_cooldown(delay)
                    await asyncio.sleep(delay)
                    continue
                return []
            return []

        async def _fetch_paginated_funding(coin: str, c_start: int) -> list[dict[str, Any]]:
            all_rows: list[dict[str, Any]] = []
            cursor = c_start
            for _ in range(5):  # up to 2,500 hours (~104 days)
                page = await _post_with_retry(
                    {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": now_ms}
                )
                if not page:
                    break
                all_rows.extend(page)
                if len(page) < 480:
                    break
                last_t = int(page[-1].get("time", 0))
                if last_t <= cursor:
                    break
                cursor = last_t + 1
            return all_rows

        async def _collect_one(item: dict[str, Any]) -> tuple[str, pd.DataFrame]:
            coin = item["coin"]
            async with sem:
                try:
                    candles = await _post_with_retry(
                        {"type": "candleSnapshot", "req": {"coin": coin, "interval": iv, "startTime": start_ms, "endTime": now_ms}}
                    )
                    if not candles:
                        return coin, pd.DataFrame()
                    c_start = int(candles[0]["t"]) - 2 * HOUR_MS
                    f_hist = await _fetch_paginated_funding(coin, c_start)
                    if not f_hist:
                        return coin, pd.DataFrame()
                    if iv == "15m":
                        return coin, build_coin_settlement_events_15m(
                            coin, candles, f_hist, day_vol_usd=item["vol_usd"], oi_usd=item["oi_usd"]
                        )
                    return coin, build_coin_settlement_events(
                        coin, candles, f_hist, day_vol_usd=item["vol_usd"], oi_usd=item["oi_usd"]
                    )
                except Exception:
                    return coin, pd.DataFrame()

        results = await asyncio.gather(*[_collect_one(it) for it in selected])
        valid = [df for _coin, df in results if not df.empty]
        coins_failed = [coin for coin, df in results if df.empty]
        combined = pd.concat(valid, ignore_index=True) if valid else pd.DataFrame()

    span_days = (
        round((int(combined["t0_ms"].max()) - int(combined["t0_ms"].min())) / (24 * HOUR_MS), 2)
        if not combined.empty
        else 0.0
    )
    meta = {
        "observed_at_ms": now_ms,
        "bar_interval": iv,
        "coins_requested": len(selected),
        "coins_collected": int(combined["coin"].nunique()) if not combined.empty else 0,
        "coins_failed": coins_failed,
        "total_settlement_events": int(len(combined)),
        "span_days": span_days,
    }
    collected = int(meta["coins_collected"])
    ratio = (collected / len(selected)) if selected else 0.0
    meta["collection_ratio"] = round(ratio, 4)
    # A run that silently lost most of its universe (HTTP 429 etc.) must not publish as healthy.
    meta["degraded"] = bool(ratio < MIN_COLLECTION_RATIO)
    return combined, meta


async def fetch_17d_settlement_dataset(
    *,
    max_coins: int = 45,
    min_oi_usd: float = 2_000_000.0,
    min_vol_usd: float = 500_000.0,
    lookback_days: int = 18,
    info_url: str = HYPERCORE_INFO_URL,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Backward-compatible wrapper for 17-day 5m collection."""
    return await fetch_settlement_dataset(
        bar_interval="5m",
        max_coins=max_coins,
        min_oi_usd=min_oi_usd,
        min_vol_usd=min_vol_usd,
        lookback_days=lookback_days,
        info_url=info_url,
    )


# --------------------------------------------------------------------------- Reporting


def format_settlement_study_markdown(stats_df: pd.DataFrame, meta: dict[str, Any]) -> str:
    maker_rt = (
        float(stats_df["maker_rt_bps"].iloc[0]) if (not stats_df.empty and "maker_rt_bps" in stats_df.columns) else 3.0
    )
    stress_rt = (
        float(stats_df["stress_rt_bps"].iloc[0]) if (not stats_df.empty and "stress_rt_bps" in stats_df.columns) else 10.0
    )
    ts = datetime.fromtimestamp(meta["observed_at_ms"] / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    iv = meta.get("bar_interval", "5m")
    span = meta.get("span_days", 0)
    lines = [
        f"# Каузальное {span:.0f}-дневное исследование часовых расчётов фандинга `:00` на Hyperliquid ({ts})",
        "",
        f"- **Выборка:** `{meta['coins_collected']}` монет, **{meta['total_settlement_events']:,}** часовых расчётов `:00` за **{span} дней** (свечи `{iv}` + полный пагинированный `fundingHistory`).",
        "- **Строгая каузальность:** сигнал для часа `T0` использует **только** прошлую ставку `f(T0 - 1h)`; разделение по времени: **Dev (первые 65%)** vs **Holdout (последние 35%)** + аудит **Ex-Top1-Coin**.",
        f"- **Комиссии:** Maker round-trip `{maker_rt:.1f} б.п.`, Taker round-trip `9.0 б.п.` (`2 × 4.5`); стресс-сценарий исполнения `{stress_rt:.1f} б.п.` RT. "
        "Вердикт требует `t >= 2` и по наивному, и по кластеризованному (по часу `:00`) t-stat.",
        "",
    ]
    if meta.get("degraded"):
        lines.insert(
            3,
            f"- ⚠️ **DEGRADED:** собрано `{meta.get('coins_collected')}` из `{meta.get('coins_requested')}` монет "
            f"(`{float(meta.get('collection_ratio', 0.0)) * 100:.0f}%`); выводы ниже не репрезентативны.",
        )
    if stats_df.empty:
        lines.append("Недостаточно экстремальных событий фандинга в выбранном окне.")
        return "\n".join(lines) + "\n"

    dev = stats_df[stats_df["split"] == "dev"].set_index(["bucket", "strategy"])
    hld = stats_df[stats_df["split"] == "holdout"].set_index(["bucket", "strategy"])
    common_idx = [k for k in dev.index if k in hld.index]

    lines += [
        "## Сводка Dev (65%) vs Holdout (35%) + Ex-Top1-Coin",
        "",
        "| Бакет | Стратегия | Dev N | Dev Gross (t) | Holdout N | Holdout Gross (t) | Holdout Net Maker | Ex-Top1 Gross (t) | Вердикт |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    winners: list[tuple[str, str, float, float, int, float]] = []
    for b_name, strat in common_idx:
        d = dev.loc[(b_name, strat)]
        h = hld.loc[(b_name, strat)]
        ex_g = float(h.get("ex_top1_gross_bps", h["mean_gross_bps"]))
        ex_t = float(h.get("ex_top1_t_stat", h["t_stat"]))
        top_c = str(h.get("top1_coin", ""))
        h_t_cl = float(h.get("t_stat_clustered", h["t_stat"]))
        go = (
            (float(d["net_maker_bps"]) > 0.0)
            and (float(h["net_maker_bps"]) > 0.0)
            and (min(float(h["t_stat"]), h_t_cl) >= 2.0)
            and ((ex_g - maker_rt) > 0.0)
        )
        verdict = (
            "🟢 **GO (Maker)**"
            if go
            else ("🟡 Gross/Overlay" if float(d["mean_gross_bps"]) > 1.0 and float(h["mean_gross_bps"]) > 1.0 and float(h["t_stat"]) >= 2.0 else "🔴 NO-GO")
        )
        if go:
            winners.append((b_name, strat, float(h["net_maker_bps"]), float(h["t_stat"]), int(h["events"]), ex_g - maker_rt))
        ex_txt = f"{ex_g:+.2f} ({ex_t:+.1f}) ex-{top_c}" if top_c else f"{ex_g:+.2f}"
        lines.append(
            f"| `{b_name}` | `{strat}` | {int(d['events'])} | {d['mean_gross_bps']:+.2f} ({d['t_stat']:+.1f}) | "
            f"{int(h['events'])} | {h['mean_gross_bps']:+.2f} ({h['t_stat']:+.1f}) | **{h['net_maker_bps']:+.2f} б.п.** | {ex_txt} | {verdict} |"
        )

    lines.append("")
    lines.append("## Честный вывод исследования")
    lines.append("")
    if winners:
        winners.sort(key=lambda x: -x[2])
        top = winners[0]
        lines.append(
            f"- ✅ **Подтверждён устойчивый OOS + Ex-Top1-Coin эффект:** `{top[1]}` в бакете `{top[0]}` дал на Holdout "
            f"**`{top[2]:+.2f} б.п.` чистыми (Maker)** (`t = {top[3]:+.2f}`, `N = {top[4]}`, Ex-Top1 Net `{top[5]:+.2f} б.п.`)."
        )
    else:
        gross_winners = [
            (b, s, float(hld.loc[(b, s), "mean_gross_bps"]), float(hld.loc[(b, s), "t_stat"]), int(hld.loc[(b, s), "events"]))
            for b, s in common_idx
            if float(dev.loc[(b, s), "mean_gross_bps"]) > 1.0
            and float(dev.loc[(b, s), "t_stat"]) >= 2.0
            and float(hld.loc[(b, s), "mean_gross_bps"]) > 1.0
            and float(hld.loc[(b, s), "t_stat"]) >= 2.0
        ]
        if gross_winners:
            gw = sorted(gross_winners, key=lambda x: -x[2])[0]
            lines.append(
                f"- 🟡 **Подтверждён статистически значимый Gross-эффект (execution/MM overlay, но не standalone Tier-0):** "
                f"`{gw[1]}` в бакете `{gw[0]}` положителен на Dev и Holdout (`{gw[2]:+.2f} б.п.` gross, `t = {gw[3]:+.2f}`, `N = {gw[4]}`), "
                "но комиссия Tier-0 Maker (`3.0 б.п.` RT) съедает чистый остаток. Применение: бесплатный **Execution/MM Overlay**."
            )
        else:
            lines.append(
                "- ⚠️ **Честная фальсификация:** ни одна стратегия не прошла одновременно Dev + Holdout + Ex-Top1-Coin после комиссий `3.0 б.п.` (`t >= 2.0`). "
                "В standalone-продакшн **НЕ** допускается."
            )
    return "\n".join(lines) + "\n"


def format_settlement_study_telegram_rich_html(stats_df: pd.DataFrame, meta: dict[str, Any]) -> str:
    dev = stats_df[stats_df["split"] == "dev"].set_index(["bucket", "strategy"]) if not stats_df.empty else pd.DataFrame()
    hld = stats_df[stats_df["split"] == "holdout"].set_index(["bucket", "strategy"]) if not stats_df.empty else pd.DataFrame()
    rows = [
        '<tr><th align="left">Бакет / Стратегия</th><th align="right">Dev Gross</th><th align="right">Holdout Gross (t)</th><th align="right">Net Maker</th></tr>'
    ]
    if not hld.empty:
        top_h = hld.sort_values("mean_gross_bps", ascending=False).head(6)
        for (b_name, strat), h in top_h.iterrows():
            d_g = f"{dev.loc[(b_name, strat), 'mean_gross_bps']:+.2f}" if (b_name, strat) in dev.index else "—"
            dot = "🟢" if (float(h["net_maker_bps"]) > 0 and float(h["t_stat"]) >= 2.0) else "⚪"
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(str(strat))}</b> ({html.escape(str(b_name))})</td>'
                f'<td align="right">{d_g} б.п.</td>'
                f'<td align="right">{h["mean_gross_bps"]:+.2f} ({h["t_stat"]:+.1f})</td>'
                f'<td align="right">{dot} {h["net_maker_bps"]:+.2f} б.п.</td>'
                "</tr>"
            )
    iv = meta.get("bar_interval", "5m")
    deg_note = (
        f"<p>⚠️ <b>DEGRADED ({meta.get('coins_collected')}/{meta.get('coins_requested')} монет):</b> "
        "неполный сбор из-за лимитов API; предыдущий stats.parquet сохранён.</p>"
        if meta.get("degraded")
        else ""
    )
    return (
        f"<h1>🔬 OOS-аудит фандинга :00 ({meta['span_days']:.0f}д · {html.escape(str(iv))}, Hyperliquid)</h1>"
        f"{deg_note}"
        f"<p><b>Выборка:</b> {meta['coins_collected']} монет · <b>{meta['total_settlement_events']:,}</b> расчётов (Dev 65% vs Holdout 35%, Ex-Top1-Coin).</p>"
        f'<table bordered striped>{"".join(rows)}</table>'
        f"<footer>Hyperliquid Mainnet {html.escape(str(iv))} candles + paginated fundingHistory</footer>"
    )
