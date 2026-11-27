"""Hyperliquid WebSocket L2 + Trades recorder and passive ALO fill / markout analyzer.

Read-only public market data (``wss://api.hyperliquid.xyz/ws``); zero auth, zero orders.
Measures real tick-by-tick microstructure:
* bid-ask spread, top-of-book & top-5 liquidity, update frequency, OBI predictability;
* queue-aware passive ``ALO`` (post-only) limit-order fill rate;
* post-fill signed markout at +5s / +30s / +60s (adverse selection);
* net effective execution cost of passive ``ALO`` (with taker fallback on timeout) vs pure taker.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import time
from typing import Any

import numpy as np
import pandas as pd

MAINNET_WS_URL = "wss://api.hyperliquid.xyz/ws"
TESTNET_WS_URL = "wss://api.hyperliquid-testnet.xyz/ws"
SIDE_MAP = {"B": "buy", "A": "sell"}


# --------------------------------------------------------------------------- message parsers


def parse_l2_message(data: dict[str, Any], recv_ms: int) -> dict[str, Any] | None:
    """Parse one Hyperliquid ``l2Book`` WebSocket payload into a flat tick row."""
    coin = data.get("coin")
    levels = data.get("levels")
    if not coin or not (isinstance(levels, list) and len(levels) == 2 and levels[0] and levels[1]):
        return None
    bids, asks = levels[0][:5], levels[1][:5]
    best_bid = float(bids[0]["px"])
    best_ask = float(asks[0]["px"])
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    mid = 0.5 * (best_bid + best_ask)
    spread_bps = (best_ask - best_bid) / mid * 1e4
    bid_sz1 = float(bids[0]["sz"])
    ask_sz1 = float(asks[0]["sz"])
    bid_usd1 = best_bid * bid_sz1
    ask_usd1 = best_ask * ask_sz1
    bid_usd5 = sum(float(lv["px"]) * float(lv["sz"]) for lv in bids)
    ask_usd5 = sum(float(lv["px"]) * float(lv["sz"]) for lv in asks)
    tot1 = bid_sz1 + ask_sz1
    tot5 = bid_usd5 + ask_usd5
    return {
        "recv_ms": int(recv_ms),
        "book_time_ms": int(data.get("time", recv_ms)),
        "coin": str(coin),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_px": mid,
        "spread_bps": spread_bps,
        "bid_sz1": bid_sz1,
        "ask_sz1": ask_sz1,
        "bid_usd1": bid_usd1,
        "ask_usd1": ask_usd1,
        "bid_usd5": bid_usd5,
        "ask_usd5": ask_usd5,
        "imbalance1": (bid_sz1 - ask_sz1) / tot1 if tot1 > 0 else 0.0,
        "imbalance5": (bid_usd5 - ask_usd5) / tot5 if tot5 > 0 else 0.0,
    }


def parse_bbo_message(data: dict[str, Any], recv_ms: int, depth: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """Parse one ``bbo`` WebSocket payload into the same flat tick row as :func:`parse_l2_message`.

    Hyperliquid pushes ``bbo`` on every block where the top of book changes, while the public
    ``l2Book`` feed arrives only every several seconds. Top-of-book price and queue size come from
    ``bbo``; 5-level depth and imbalance are carried from the latest ``l2Book`` row (``depth``).
    """
    coin = data.get("coin")
    bbo = data.get("bbo")
    if not coin or not (isinstance(bbo, list) and len(bbo) == 2 and bbo[0] and bbo[1]):
        return None
    best_bid = float(bbo[0]["px"])
    best_ask = float(bbo[1]["px"])
    if best_bid <= 0 or best_ask <= 0 or best_ask < best_bid:
        return None
    bid_sz1 = float(bbo[0]["sz"])
    ask_sz1 = float(bbo[1]["sz"])
    mid = 0.5 * (best_bid + best_ask)
    tot1 = bid_sz1 + ask_sz1
    depth = depth or {}
    return {
        "recv_ms": int(recv_ms),
        "book_time_ms": int(data.get("time", recv_ms)),
        "coin": str(coin),
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid_px": mid,
        "spread_bps": (best_ask - best_bid) / mid * 1e4,
        "bid_sz1": bid_sz1,
        "ask_sz1": ask_sz1,
        "bid_usd1": best_bid * bid_sz1,
        "ask_usd1": best_ask * ask_sz1,
        "bid_usd5": float(depth.get("bid_usd5", float("nan"))),
        "ask_usd5": float(depth.get("ask_usd5", float("nan"))),
        "imbalance1": (bid_sz1 - ask_sz1) / tot1 if tot1 > 0 else 0.0,
        "imbalance5": float(depth.get("imbalance5", 0.0)),
        "source": "bbo",
    }


def parse_trades_message(items: list[dict[str, Any]], recv_ms: int) -> list[dict[str, Any]]:
    """Parse one Hyperliquid ``trades`` WebSocket payload into normalized trade rows."""
    out: list[dict[str, Any]] = []
    for item in items:
        coin = item.get("coin")
        if not coin:
            continue
        px = float(item["px"])
        sz = float(item["sz"])
        out.append(
            {
                "recv_ms": int(recv_ms),
                "trade_time_ms": int(item.get("time", recv_ms)),
                "coin": str(coin),
                "side": SIDE_MAP.get(str(item.get("side", "")), str(item.get("side", ""))),
                "px": px,
                "sz": sz,
                "notional_usd": px * sz,
                "tid": int(item.get("tid", 0)),
            }
        )
    return out


# --------------------------------------------------------------------------- WebSocket recorder


async def record_hl_microstructure(
    coins: list[str],
    *,
    duration_sec: float = 180.0,
    output_dir: Path,
    ws_url: str = MAINNET_WS_URL,
) -> dict[str, Any]:
    """Subscribe to public HL ``bbo``, ``l2Book`` and ``trades`` feeds for ``duration_sec`` and save parquet.

    ``l2_ticks.parquet`` holds top-of-book ticks from ``bbo`` (``source="bbo"``) enriched with the
    latest 5-level depth; the sparse ``l2Book`` snapshots themselves are kept with ``source="l2Book"``.
    """
    import websockets

    l2_rows: list[dict[str, Any]] = []
    last_depth: dict[str, dict[str, Any]] = {}
    trade_rows: list[dict[str, Any]] = []
    started = time.monotonic()
    deadline = started + duration_sec
    last_ping = started

    async with websockets.connect(ws_url, ping_interval=20, ping_timeout=20, max_size=4 * 1024 * 1024) as ws:
        for coin in coins:
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "bbo", "coin": coin}}))
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "l2Book", "coin": coin}}))
            await ws.send(json.dumps({"method": "subscribe", "subscription": {"type": "trades", "coin": coin}}))

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            now_mono = time.monotonic()
            if now_mono - last_ping >= 25.0:
                await ws.send(json.dumps({"method": "ping"}))
                last_ping = now_mono
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(2.0, remaining))
            except asyncio.TimeoutError:
                continue
            recv_ms = int(time.time() * 1000)
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            channel = msg.get("channel")
            data = msg.get("data")
            if channel == "bbo" and isinstance(data, dict):
                row = parse_bbo_message(data, recv_ms, last_depth.get(str(data.get("coin"))))
                if row is not None:
                    l2_rows.append(row)
            elif channel == "l2Book" and isinstance(data, dict):
                row = parse_l2_message(data, recv_ms)
                if row is not None:
                    last_depth[row["coin"]] = row
                    l2_rows.append({**row, "source": "l2Book"})
            elif channel == "trades" and isinstance(data, list):
                trade_rows.extend(parse_trades_message(data, recv_ms))

    output_dir.mkdir(parents=True, exist_ok=True)
    l2_df = pd.DataFrame(l2_rows)
    trades_df = pd.DataFrame(trade_rows)
    if not trades_df.empty and "tid" in trades_df.columns:
        trades_df = trades_df.drop_duplicates(subset=["coin", "tid", "trade_time_ms"]).reset_index(drop=True)
    l2_df.to_parquet(output_dir / "l2_ticks.parquet", index=False)
    trades_df.to_parquet(output_dir / "trades.parquet", index=False)
    meta = {
        "coins": coins,
        "duration_sec": round(time.monotonic() - started, 2),
        "l2_rows": len(l2_df),
        "trade_rows": len(trades_df),
        "output_dir": str(output_dir),
    }
    (output_dir / "recording_meta.json").write_text(json.dumps(meta, indent=2))
    return meta


# --------------------------------------------------------------------------- ALO fill & markout analyzer


@dataclass(frozen=True, slots=True)
class CoinMicrostructureReport:
    coin: str
    l2_updates: int
    trades_count: int
    duration_sec: float
    median_spread_bps: float
    p90_spread_bps: float
    median_top1_usd: float
    median_top5_usd: float
    obi5_corr_5s: float
    alo_trials: int
    alo_fill_rate: float
    alo_median_fill_sec: float
    markout_5s_bps: float
    markout_30s_bps: float
    markout_60s_bps: float
    maker_eff_cost_bps: float  # effective cost (negative = cost, positive = edge) including maker fee & timeout fallback
    taker_eff_cost_bps: float  # pure taker cost (-0.5*spread - taker_fee)
    alo_savings_bps: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _lookup_mid_at(times: np.ndarray, mids: np.ndarray, target_ms: int) -> float:
    idx = int(np.searchsorted(times, target_ms, side="right")) - 1
    if idx < 0 or idx >= len(mids):
        return float("nan")
    return float(mids[idx])


def simulate_passive_alo_coin(
    l2_coin: pd.DataFrame,
    trades_coin: pd.DataFrame,
    *,
    order_usd: float = 5_000.0,
    sample_interval_sec: float = 5.0,
    timeout_sec: float = 60.0,
    hl_maker_fee_bps: float = 1.5,
    hl_taker_fee_bps: float = 4.5,
) -> CoinMicrostructureReport:
    """Simulate queue-aware passive ``ALO`` limit orders and measure +5s/+30s/+60s markout."""
    l2 = l2_coin.sort_values("recv_ms").reset_index(drop=True)
    first_book_recv_ms = int(l2["recv_ms"].iloc[0]) if not l2.empty else 0
    first_book_time_ms = (
        int(l2["book_time_ms"].min()) if ("book_time_ms" in l2.columns and not l2.empty) else first_book_recv_ms
    )
    if not trades_coin.empty:
        tr = trades_coin.sort_values("recv_ms").reset_index(drop=True)
        keep_tr = tr["recv_ms"] >= first_book_recv_ms
        if "trade_time_ms" in tr.columns:
            keep_tr = keep_tr & (tr["trade_time_ms"] >= first_book_time_ms - 1_000)
        tr = tr[keep_tr].reset_index(drop=True)
    else:
        tr = pd.DataFrame()
    coin = str(l2["coin"].iloc[0])

    times = l2["recv_ms"].to_numpy(dtype="int64")
    mids = l2["mid_px"].to_numpy(dtype=float)
    bids = l2["best_bid"].to_numpy(dtype=float)
    asks = l2["best_ask"].to_numpy(dtype=float)
    bid_usd1 = l2["bid_usd1"].to_numpy(dtype=float)
    ask_usd1 = l2["ask_usd1"].to_numpy(dtype=float)
    imb5 = l2["imbalance5"].to_numpy(dtype=float)
    duration_sec = max(1.0, (times[-1] - times[0]) / 1000.0) if len(times) > 1 else 1.0

    # OBI5 predictive correlation with 5-second forward mid return
    fwd5_rets, imb_vals = [], []
    for i in range(0, len(times), max(1, len(times) // 500)):
        t_fwd = int(times[i]) + 5_000
        if t_fwd > int(times[-1]):
            break
        m_fwd = _lookup_mid_at(times, mids, t_fwd)
        if math.isfinite(m_fwd) and mids[i] > 0:
            fwd5_rets.append((m_fwd / mids[i] - 1.0) * 1e4)
            imb_vals.append(float(imb5[i]))
    if len(fwd5_rets) >= 10 and np.std(imb_vals) > 1e-9 and np.std(fwd5_rets) > 1e-9:
        obi_corr = float(np.corrcoef(imb_vals, fwd5_rets)[0, 1])
    else:
        obi_corr = 0.0

    tr_times = tr["recv_ms"].to_numpy(dtype="int64") if not tr.empty else np.empty(0, dtype="int64")
    tr_px = tr["px"].to_numpy(dtype=float) if not tr.empty else np.empty(0, dtype=float)
    tr_side = tr["side"].to_numpy(dtype=object) if not tr.empty else np.empty(0, dtype=object)
    tr_usd = tr["notional_usd"].to_numpy(dtype=float) if not tr.empty else np.empty(0, dtype=float)

    # Sample passive ALO trials across the recording (leave 60s tail for +60s markout if possible)
    max_markout_ms = 60_000
    last_start_ms = int(times[-1]) - min(int(timeout_sec * 1000), max(10_000, int(duration_sec * 1000 // 3)))
    step_ms = int(sample_interval_sec * 1000)

    fill_times_sec: list[float] = []
    mk5, mk30, mk60 = [], [], []
    trials = 0
    fills = 0

    t_cursor = int(times[0])
    while t_cursor <= last_start_ms:
        l2_idx = int(np.searchsorted(times, t_cursor, side="right")) - 1
        if l2_idx < 0:
            t_cursor += step_ms
            continue
        t_end = t_cursor + int(timeout_sec * 1000)
        tr_lo = int(np.searchsorted(tr_times, t_cursor, side="left"))
        tr_hi = int(np.searchsorted(tr_times, t_end, side="right"))

        # Test both a passive buy at best_bid and a passive sell at best_ask
        for side in ("buy", "sell"):
            trials += 1
            limit_px = float(bids[l2_idx]) if side == "buy" else float(asks[l2_idx])
            queue_ahead_usd = float(bid_usd1[l2_idx]) if side == "buy" else float(ask_usd1[l2_idx])
            needed_usd = queue_ahead_usd + order_usd
            consumed_usd = 0.0
            fill_ms: int | None = None

            for k in range(tr_lo, tr_hi):
                px = float(tr_px[k])
                if side == "buy":
                    if px < limit_px - 1e-12:
                        fill_ms = int(tr_times[k])
                        break
                    if abs(px - limit_px) <= 1e-12 and tr_side[k] == "sell":
                        consumed_usd += float(tr_usd[k])
                        if consumed_usd >= needed_usd:
                            fill_ms = int(tr_times[k])
                            break
                else:
                    if px > limit_px + 1e-12:
                        fill_ms = int(tr_times[k])
                        break
                    if abs(px - limit_px) <= 1e-12 and tr_side[k] == "buy":
                        consumed_usd += float(tr_usd[k])
                        if consumed_usd >= needed_usd:
                            fill_ms = int(tr_times[k])
                            break

            if fill_ms is not None:
                fills += 1
                fill_times_sec.append((fill_ms - t_cursor) / 1000.0)
                sgn = 1.0 if side == "buy" else -1.0
                for horizon_ms, bucket in ((5_000, mk5), (30_000, mk30), (max_markout_ms, mk60)):
                    if fill_ms + horizon_ms <= int(times[-1]):
                        m_after = _lookup_mid_at(times, mids, fill_ms + horizon_ms)
                        if math.isfinite(m_after):
                            bucket.append(sgn * (m_after - limit_px) / limit_px * 1e4)

        t_cursor += step_ms

    med_sp = float(l2["spread_bps"].median())
    p90_sp = float(l2["spread_bps"].quantile(0.90))
    med_top1 = float(np.median(np.minimum(bid_usd1, ask_usd1)))
    top5 = np.minimum(l2["bid_usd5"].to_numpy(dtype=float), l2["ask_usd5"].to_numpy(dtype=float))
    med_top5 = float(np.nanmedian(top5)) if np.isfinite(top5).any() else float("nan")
    fill_rate = float(fills / trials) if trials > 0 else 0.0
    med_fill_s = float(np.median(fill_times_sec)) if fill_times_sec else float("nan")
    m5 = float(np.mean(mk5)) if mk5 else float("nan")
    m30 = float(np.mean(mk30)) if mk30 else float("nan")
    m60 = float(np.mean(mk60)) if mk60 else float("nan")

    # Effective execution PnL vs arrival mid (negative = cost):
    # Filled ALO orders earn 30s markout minus maker fee; unfilled orders cross spread as taker after timeout.
    taker_cost = -0.5 * med_sp - hl_taker_fee_bps
    mk_ref = m30 if math.isfinite(m30) else (m5 if math.isfinite(m5) else 0.0)
    maker_eff = fill_rate * (mk_ref - hl_maker_fee_bps) + (1.0 - fill_rate) * taker_cost
    savings = maker_eff - taker_cost

    return CoinMicrostructureReport(
        coin=coin,
        l2_updates=len(l2),
        trades_count=len(tr),
        duration_sec=round(duration_sec, 1),
        median_spread_bps=med_sp,
        p90_spread_bps=p90_sp,
        median_top1_usd=med_top1,
        median_top5_usd=med_top5,
        obi5_corr_5s=obi_corr,
        alo_trials=trials,
        alo_fill_rate=fill_rate,
        alo_median_fill_sec=med_fill_s,
        markout_5s_bps=m5,
        markout_30s_bps=m30,
        markout_60s_bps=m60,
        maker_eff_cost_bps=maker_eff,
        taker_eff_cost_bps=taker_cost,
        alo_savings_bps=savings,
    )


def analyze_recording(
    recording_dir: Path,
    *,
    order_usd: float = 5_000.0,
    sample_interval_sec: float = 5.0,
    timeout_sec: float = 60.0,
) -> list[CoinMicrostructureReport]:
    """Analyze all recorded coins in ``recording_dir``."""
    l2_df = pd.read_parquet(recording_dir / "l2_ticks.parquet")
    trades_path = recording_dir / "trades.parquet"
    trades_df = pd.read_parquet(trades_path) if trades_path.exists() else pd.DataFrame()
    reports: list[CoinMicrostructureReport] = []
    if l2_df.empty or "coin" not in l2_df.columns:
        return reports
    for coin in sorted(l2_df["coin"].unique().tolist()):
        l2_c = l2_df[l2_df["coin"] == coin]
        if "source" in l2_c.columns and (l2_c["source"] == "bbo").any():
            # Top-of-book state from the per-block bbo feed; sparse l2Book snapshots would make
            # the simulated limit price and queue several seconds stale.
            l2_c = l2_c[l2_c["source"] == "bbo"]
        tr_c = trades_df[trades_df["coin"] == coin] if not trades_df.empty and "coin" in trades_df.columns else pd.DataFrame()
        if not tr_c.empty:
            # Drop WS trade replay prints received before the first book tick or stamped before recording start.
            first_recv_ms = int(l2_c["recv_ms"].min())
            keep_mask = tr_c["recv_ms"] >= first_recv_ms
            if "trade_time_ms" in tr_c.columns:
                ts_ref_col = "book_time_ms" if "book_time_ms" in l2_c.columns else "recv_ms"
                keep_mask = keep_mask & (tr_c["trade_time_ms"] >= int(l2_c[ts_ref_col].min()) - 1_000)
            tr_c = tr_c[keep_mask]
        reports.append(
            simulate_passive_alo_coin(
                l2_c,
                tr_c,
                order_usd=order_usd,
                sample_interval_sec=sample_interval_sec,
                timeout_sec=timeout_sec,
            )
        )
    return reports
