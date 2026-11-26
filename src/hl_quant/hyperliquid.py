"""Read-only collector for the public Hyperliquid ``POST /info`` market-data API.

Research evidence only: no signing, no private account endpoints, no orders.
Every run lands as one immutable directory via :mod:`.publication`::

    <output_root>/<dataset>/<run_id>/
        raw/page-0000.json ...   provider responses (public user addresses stripped)
        rows.parquet             normalized rows with ``available_at`` (history)
        <table_name>.parquet     typed per-table snapshots (``snapshot`` dataset)
        manifest.json            provider, endpoint, params, status, sha256 of every file

Historical runs get a deterministic ``run_id`` from their provider, endpoint and
parameters, so a rerun with the same window can be reused without hitting the
network; snapshots are keyed by observation time and are never presented as history.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import random
import time
from typing import Any, Callable, Iterable

import httpx
import pandas as pd

from hl_quant.publication import (
    SCHEMA_VERSION,
    STATUS_COMPLETE,
    STATUS_EMPTY,
    STATUS_PARTIAL_HISTORY,
    Collected,
    ManifestCorruptError,
    MarketDataError,
    PublishResult,
    publish,
    run_id_for,
    run_id_for_params,
    try_reuse,
    verify_run,
)

__all__ = [
    "CANDLE_INTERVALS_MS",
    "CANDLE_PAGE_LIMIT",
    "Collected",
    "FUNDING_PAGE_LIMIT",
    "FUNDING_STEP_MS",
    "HyperliquidError",
    "HyperliquidInfoClient",
    "MAINNET_INFO_URL",
    "ManifestCorruptError",
    "MarketDataError",
    "PROVIDER",
    "PublishResult",
    "RETRYABLE_STATUS",
    "SCHEMA_VERSION",
    "STATUS_COMPLETE",
    "STATUS_EMPTY",
    "STATUS_PARTIAL_HISTORY",
    "TESTNET_INFO_URL",
    "collect_candles",
    "collect_funding",
    "collect_snapshot",
    "current_time_ms",
    "publish",
    "run_id_for",
    "run_id_for_params",
    "try_reuse",
    "verify_run",
]

PROVIDER = "hyperliquid"
MAINNET_INFO_URL = "https://api.hyperliquid.xyz/info"
TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
FUNDING_PAGE_LIMIT = 500
FUNDING_STEP_MS = 3_600_000
CANDLE_PAGE_LIMIT = 5000
CANDLE_INTERVALS_MS = {
    "1m": 60_000,
    "3m": 180_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "8h": 28_800_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
    "3d": 259_200_000,
    "1w": 604_800_000,
}
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRY_AFTER_S = 60.0
TRADE_SIDE_MAP = {"B": "buy", "A": "sell"}


class HyperliquidError(MarketDataError):
    """Terminal collector failure (bad request, exhausted retries, contract mismatch)."""


# --------------------------------------------------------------------------- client


@dataclass(slots=True)
class HyperliquidInfoClient:
    """Minimal retrying client for ``POST /info``. Holds no credentials."""

    base_url: str = MAINNET_INFO_URL
    timeout_s: float = 20.0
    max_retries: int = 4
    backoff_s: float = 1.0
    jitter_ratio: float = 0.25
    request_delay_s: float = 0.0
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    rng: Callable[[], float] = random.random
    _client: httpx.Client | None = field(default=None, init=False, repr=False)
    _request_count: int = field(default=0, init=False, repr=False)

    def __enter__(self) -> "HyperliquidInfoClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(timeout=self.timeout_s, transport=self.transport)
        return self._client

    def info(self, body: dict[str, Any]) -> Any:
        if self._request_count > 0 and self.request_delay_s > 0:
            self.sleep(self.request_delay_s)
        self._request_count += 1

        last_error: str = ""
        for attempt in range(self.max_retries + 1):
            retry_after_s: float | None = None
            try:
                response = self._http().post(self.base_url, json=body)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise HyperliquidError(f"non-JSON response for {body.get('type')}") from exc
                if response.status_code not in RETRYABLE_STATUS:
                    raise HyperliquidError(
                        f"{body.get('type')} rejected with HTTP {response.status_code}: {response.text[:200]}"
                    )
                last_error = f"HTTP {response.status_code}"
                retry_after_s = _parse_retry_after(response.headers.get("Retry-After"))
            if attempt < self.max_retries:
                base_wait = self.backoff_s * (2**attempt)
                jitter = base_wait * self.jitter_ratio * self.rng() if self.jitter_ratio > 0 else 0.0
                wait_s = max(base_wait + jitter, retry_after_s or 0.0)
                self.sleep(wait_s)
        raise HyperliquidError(f"{body.get('type')} failed after {self.max_retries + 1} attempts: {last_error}")


def current_time_ms() -> int:
    return int(time.time() * 1000)


def _ms_to_utc(series: pd.Series) -> pd.Series:
    return pd.to_datetime(series.astype("int64"), unit="ms", utc=True)


# --------------------------------------------------------------------------- funding


def collect_funding(
    client: HyperliquidInfoClient,
    coin: str,
    start_ms: int,
    end_ms: int,
    *,
    now_ms: int | None = None,
    max_pages: int = 10_000,
) -> Collected:
    """Funding history for one perp over ``[start_ms, end_ms]`` (paged by 500 rows)."""
    observed = now_ms if now_ms is not None else current_time_ms()
    _check_window(start_ms, end_ms, observed)
    pages: list[Any] = []
    records: dict[int, dict[str, Any]] = {}
    cursor = start_ms
    while True:
        if len(pages) >= max_pages:
            raise HyperliquidError(f"funding pagination exceeded {max_pages} pages for {coin}")
        page = client.info({"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms})
        if not isinstance(page, list):
            raise HyperliquidError(f"fundingHistory returned {type(page).__name__}, expected list")
        pages.append(page)
        for item in page:
            _require_keys(item, {"coin", "fundingRate", "premium", "time"}, "fundingHistory")
            if item["coin"] != coin:
                raise HyperliquidError(f"fundingHistory returned coin {item['coin']!r} for {coin!r}")
            ts = int(item["time"])
            if start_ms <= ts <= end_ms:
                records[ts] = item
        if len(page) < FUNDING_PAGE_LIMIT:
            break
        last = max(int(item["time"]) for item in page)
        if last >= end_ms or last < cursor:
            break
        cursor = last + 1

    rows = pd.DataFrame(
        [
            {
                "coin": coin,
                "time_ms": ts,
                "funding_rate": float(item["fundingRate"]),
                "premium": float(item["premium"]),
            }
            for ts, item in sorted(records.items())
        ],
        columns=["coin", "time_ms", "funding_rate", "premium"],
    )
    rows["time"] = _ms_to_utc(rows["time_ms"])
    rows["available_at"] = rows["time"]
    first_time = int(rows["time_ms"].iloc[0]) if not rows.empty else None
    if rows.empty:
        status = STATUS_EMPTY
    elif first_time - start_ms >= FUNDING_STEP_MS:
        status = STATUS_PARTIAL_HISTORY
    else:
        status = STATUS_COMPLETE
    return Collected(
        dataset="funding",
        params={"coin": coin, "start_ms": start_ms, "end_ms": end_ms},
        pages=pages,
        rows=rows,
        status=status,
        observed_at_ms=observed,
        notes={"page_count": len(pages), "first_time_ms": first_time},
        provider=PROVIDER,
        endpoint=client.base_url,
    )


# --------------------------------------------------------------------------- candles


def collect_candles(
    client: HyperliquidInfoClient,
    coin: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    *,
    now_ms: int | None = None,
    max_pages: int = 10_000,
) -> Collected:
    """Closed OHLCV candles; unfinished candles (close after observation) are dropped.

    Hyperliquid serves only the most recent ~5000 candles per interval, so a
    window older than that comes back short: the run is marked
    ``partial_history`` with the first available open time, never padded.
    """
    if interval not in CANDLE_INTERVALS_MS:
        raise HyperliquidError(f"unknown candle interval {interval!r}")
    observed = now_ms if now_ms is not None else current_time_ms()
    _check_window(start_ms, end_ms, observed)
    step = CANDLE_INTERVALS_MS[interval]
    pages: list[Any] = []
    records: dict[int, dict[str, Any]] = {}
    cursor = start_ms
    while cursor <= end_ms:
        if len(pages) >= max_pages:
            raise HyperliquidError(f"candle pagination exceeded {max_pages} pages for {coin}")
        page = client.info(
            {
                "type": "candleSnapshot",
                "req": {"coin": coin, "interval": interval, "startTime": cursor, "endTime": end_ms},
            }
        )
        if not isinstance(page, list):
            raise HyperliquidError(f"candleSnapshot returned {type(page).__name__}, expected list")
        pages.append(page)
        if not page:
            break
        for item in page:
            _require_keys(item, {"t", "T", "s", "i", "o", "h", "l", "c", "v", "n"}, "candleSnapshot")
            if item["s"] != coin or item["i"] != interval:
                raise HyperliquidError(f"candleSnapshot returned {item['s']}/{item['i']} for {coin}/{interval}")
            records[int(item["t"])] = item
        last_open = max(int(item["t"]) for item in page)
        if len(page) < CANDLE_PAGE_LIMIT or last_open < cursor:
            break
        cursor = last_open + step

    closed = [
        item
        for t, item in sorted(records.items())
        if start_ms <= t <= end_ms and int(item["T"]) < observed
    ]
    dropped_open = sum(1 for item in records.values() if int(item["T"]) >= observed)
    rows = pd.DataFrame(
        [
            {
                "coin": coin,
                "interval": interval,
                "open_time_ms": int(item["t"]),
                "close_time_ms": int(item["T"]),
                "open": float(item["o"]),
                "high": float(item["h"]),
                "low": float(item["l"]),
                "close": float(item["c"]),
                "volume": float(item["v"]),
                "trade_count": int(item["n"]),
            }
            for item in closed
        ],
        columns=[
            "coin", "interval", "open_time_ms", "close_time_ms",
            "open", "high", "low", "close", "volume", "trade_count",
        ],
    )
    rows["open_time"] = _ms_to_utc(rows["open_time_ms"])
    # A candle is knowable only once it has closed (T is the last millisecond).
    rows["available_at"] = _ms_to_utc(rows["close_time_ms"] + 1)
    first_open = int(rows["open_time_ms"].iloc[0]) if not rows.empty else None
    if rows.empty:
        status = STATUS_EMPTY
    elif first_open - start_ms >= step:
        status = STATUS_PARTIAL_HISTORY
    else:
        status = STATUS_COMPLETE
    return Collected(
        dataset="candles",
        params={"coin": coin, "interval": interval, "start_ms": start_ms, "end_ms": end_ms},
        pages=pages,
        rows=rows,
        status=status,
        observed_at_ms=observed,
        notes={
            "page_count": len(pages),
            "first_open_ms": first_open,
            "dropped_unclosed": dropped_open,
        },
        provider=PROVIDER,
        endpoint=client.base_url,
    )


# --------------------------------------------------------------------------- snapshot


_SNAPSHOT_COLUMNS: dict[str, list[str]] = {
    "asset_ctx": [
        "coin",
        "observed_at_ms",
        "mark_px",
        "mid_px",
        "oracle_px",
        "funding",
        "premium",
        "open_interest",
        "day_notional_volume",
        "prev_day_px",
        "impact_bid_px",
        "impact_ask_px",
        "max_leverage",
        "is_delisted",
    ],
    "predicted_funding": [
        "coin",
        "venue",
        "observed_at_ms",
        "funding_rate",
        "next_funding_time_ms",
        "funding_interval_hours",
    ],
    "l2": [
        "coin",
        "observed_at_ms",
        "book_time_ms",
        "side",
        "level",
        "px",
        "sz",
        "order_count",
    ],
    "trades": [
        "coin",
        "observed_at_ms",
        "time_ms",
        "side",
        "px",
        "sz",
        "tid",
        "hash",
    ],
}


def collect_snapshot(
    client: HyperliquidInfoClient,
    coins: Iterable[str],
    *,
    include_trades: bool = True,
    now_ms: Callable[[], int] = current_time_ms,
) -> Collected:
    """Point-in-time snapshot: asset contexts, cross-venue predicted funding, L2 books, recent trades.

    Each family is published as its own typed parquet file under the run directory
    (``asset_ctx.parquet``, ``predicted_funding.parquet``, ``l2.parquet``, ``trades.parquet``)
    so integer identifiers such as 64-bit ``tid`` never widen to float64.
    """
    coin_list = sorted({c.strip() for c in coins if c.strip()})
    if not coin_list:
        raise HyperliquidError("snapshot needs at least one coin")
    started = now_ms()
    pages: list[Any] = []
    tables: dict[str, list[dict[str, Any]]] = {name: [] for name in _SNAPSHOT_COLUMNS}

    meta_ctx = client.info({"type": "metaAndAssetCtxs"})
    received = now_ms()
    pages.append({"type": "metaAndAssetCtxs", "received_ms": received, "body": meta_ctx})
    if not (isinstance(meta_ctx, list) and len(meta_ctx) == 2 and isinstance(meta_ctx[0], dict) and "universe" in meta_ctx[0]):
        raise HyperliquidError("metaAndAssetCtxs shape changed")
    universe, ctxs = meta_ctx[0]["universe"], meta_ctx[1]
    if len(universe) != len(ctxs):
        raise HyperliquidError("metaAndAssetCtxs universe/context length mismatch")
    wanted = set(coin_list)
    for asset, ctx in zip(universe, ctxs):
        if asset.get("name") not in wanted:
            continue
        impact_bid, impact_ask = _impact_pxs(ctx.get("impactPxs"))
        tables["asset_ctx"].append(
            {
                "coin": asset["name"],
                "observed_at_ms": received,
                "mark_px": _f(ctx.get("markPx")),
                "mid_px": _f(ctx.get("midPx")),
                "oracle_px": _f(ctx.get("oraclePx")),
                "funding": _f(ctx.get("funding")),
                "premium": _f(ctx.get("premium")),
                "open_interest": _f(ctx.get("openInterest")),
                "day_notional_volume": _f(ctx.get("dayNtlVlm")),
                "prev_day_px": _f(ctx.get("prevDayPx")),
                "impact_bid_px": impact_bid,
                "impact_ask_px": impact_ask,
                "max_leverage": asset.get("maxLeverage"),
                "is_delisted": bool(asset.get("isDelisted", False)),
            }
        )
    missing = wanted - {row["coin"] for row in tables["asset_ctx"]}
    if missing:
        raise HyperliquidError(f"coins not in Hyperliquid perp universe: {sorted(missing)}")

    predicted = client.info({"type": "predictedFundings"})
    received = now_ms()
    pages.append({"type": "predictedFundings", "received_ms": received, "body": predicted})
    if not isinstance(predicted, list):
        raise HyperliquidError("predictedFundings shape changed")
    for entry in predicted:
        if not isinstance(entry, (list, tuple)) or len(entry) != 2:
            raise HyperliquidError("predictedFundings entry shape changed")
        coin, venues = entry
        if coin not in wanted:
            continue
        for venue_entry in venues or []:
            if not isinstance(venue_entry, (list, tuple)) or len(venue_entry) != 2:
                raise HyperliquidError("predictedFundings venue shape changed")
            venue, detail = venue_entry
            if not detail:
                continue
            tables["predicted_funding"].append(
                {
                    "coin": coin,
                    "venue": venue,
                    "observed_at_ms": received,
                    "funding_rate": _f(detail.get("fundingRate")),
                    "next_funding_time_ms": detail.get("nextFundingTime"),
                    "funding_interval_hours": detail.get("fundingIntervalHours"),
                }
            )

    for coin in coin_list:
        book = client.info({"type": "l2Book", "coin": coin})
        received = now_ms()
        pages.append({"type": "l2Book", "coin": coin, "received_ms": received, "body": book})
        _require_keys(book, {"coin", "time", "levels"}, "l2Book")
        if book["coin"] != coin:
            raise HyperliquidError(f"l2Book returned coin {book['coin']!r} for {coin!r}")
        if not isinstance(book["levels"], list) or len(book["levels"]) != 2:
            raise HyperliquidError(f"l2Book levels shape changed for {coin!r}")
        for side_name, levels in zip(("bid", "ask"), book["levels"]):
            for depth, level in enumerate(levels):
                _require_keys(level, {"px", "sz", "n"}, "l2Book.level")
                tables["l2"].append(
                    {
                        "coin": coin,
                        "observed_at_ms": received,
                        "book_time_ms": int(book["time"]),
                        "side": side_name,
                        "level": depth,
                        "px": float(level["px"]),
                        "sz": float(level["sz"]),
                        "order_count": int(level["n"]),
                    }
                )
        if include_trades:
            trades = client.info({"type": "recentTrades", "coin": coin})
            received = now_ms()
            if not isinstance(trades, list):
                raise HyperliquidError("recentTrades shape changed")
            # Public, but participant addresses are not needed for market research.
            clean = [{k: v for k, v in t.items() if k != "users"} for t in trades]
            pages.append({"type": "recentTrades", "coin": coin, "received_ms": received, "body": clean})
            for t in clean:
                _require_keys(t, {"time", "side", "px", "sz", "tid"}, "recentTrades")
                side = TRADE_SIDE_MAP.get(t["side"])
                if side is None:
                    raise HyperliquidError(f"recentTrades unexpected side {t['side']!r} for {coin!r}")
                tables["trades"].append(
                    {
                        "coin": coin,
                        "observed_at_ms": received,
                        "time_ms": int(t["time"]),
                        "side": side,
                        "px": float(t["px"]),
                        "sz": float(t["sz"]),
                        "tid": int(t["tid"]),
                        "hash": t.get("hash"),
                    }
                )

    table_frames: dict[str, pd.DataFrame] = {}
    for kind, cols in _SNAPSHOT_COLUMNS.items():
        if kind == "trades" and not include_trades:
            continue
        frame = pd.DataFrame(tables[kind], columns=cols)
        frame["available_at"] = _ms_to_utc(frame["observed_at_ms"])
        table_frames[kind] = frame

    return Collected(
        dataset="snapshot",
        params={"coins": coin_list, "include_trades": include_trades},
        pages=pages,
        rows=pd.DataFrame(),
        status=STATUS_COMPLETE,
        observed_at_ms=started,
        notes={"record_counts": {k: len(v) for k, v in tables.items()}},
        provider=PROVIDER,
        endpoint=client.base_url,
        tables=table_frames,
    )


# --------------------------------------------------------------------------- helpers


def _check_window(start_ms: int, end_ms: int, observed_ms: int) -> None:
    if start_ms < 0 or end_ms < start_ms:
        raise HyperliquidError(f"invalid window {start_ms}..{end_ms}")
    if end_ms > observed_ms:
        raise HyperliquidError("window end is in the future; history must end at or before now")


def _require_keys(item: Any, keys: set[str], what: str) -> None:
    if not isinstance(item, dict) or not keys <= item.keys():
        raise HyperliquidError(f"{what} row missing fields: {sorted(keys - set(item or {}))}")


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _impact_pxs(value: Any) -> tuple[float | None, float | None]:
    if value is None:
        return None, None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise HyperliquidError(f"unexpected impactPxs shape: {value!r}")
    return _f(value[0]), _f(value[1])


def _parse_retry_after(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if seconds <= 0:
        return None
    return min(seconds, MAX_RETRY_AFTER_S)
