"""Read-only collectors for public CEX perpetual funding and 1h klines (Binance, Bybit).

Same publication contract as :mod:`hl_quant.hyperliquid`
(raw pages + ``rows.parquet`` + hash manifest, atomic, idempotent). No keys,
no account endpoints. Rows use one schema across venues so the funding-arb
panel can join them:

* funding: ``venue, symbol, time_ms, time, funding_rate, available_at``
* klines:  ``venue, symbol, interval, open_time_ms, close_time_ms, open, high,
  low, close, volume, open_time, available_at``
"""

from __future__ import annotations

from dataclasses import dataclass, field
import time
from typing import Any, Callable

import httpx
import pandas as pd

from hl_quant.hyperliquid import (
    RETRYABLE_STATUS,
    STATUS_COMPLETE,
    STATUS_EMPTY,
    STATUS_PARTIAL_HISTORY,
    Collected,
    HyperliquidError,
    current_time_ms,
)

BINANCE_FAPI = "https://fapi.binance.com"
BYBIT_API = "https://api.bybit.com"
OKX_API = "https://www.okx.com"
HOUR_MS = 3_600_000
VENUES = ("binance", "bybit", "okx")

BINANCE_FUNDING_LIMIT = 1000
BINANCE_KLINE_LIMIT = 1500
BYBIT_FUNDING_LIMIT = 200
BYBIT_KLINE_LIMIT = 1000
OKX_FUNDING_LIMIT = 100
OKX_KLINE_LIMIT = 100


class CexDataError(HyperliquidError):
    """Terminal public CEX collector failure."""


@dataclass(slots=True)
class PublicGetClient:
    """Retrying GET client for public market-data endpoints."""

    base_url: str
    timeout_s: float = 20.0
    max_retries: int = 4
    backoff_s: float = 1.0
    transport: httpx.BaseTransport | None = None
    sleep: Callable[[float], None] = time.sleep
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    def __enter__(self) -> "PublicGetClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def get(self, path: str, params: dict[str, Any]) -> Any:
        if self._client is None:
            self._client = httpx.Client(base_url=self.base_url, timeout=self.timeout_s, transport=self.transport)
        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.get(path, params=params)
            except httpx.TransportError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code == 200:
                    try:
                        return response.json()
                    except ValueError as exc:
                        raise CexDataError(f"non-JSON response from {path}") from exc
                if response.status_code not in RETRYABLE_STATUS:
                    raise CexDataError(f"{path} rejected with HTTP {response.status_code}: {response.text[:200]}")
                last_error = f"HTTP {response.status_code}"
            if attempt < self.max_retries:
                self.sleep(self.backoff_s * (2**attempt))
        raise CexDataError(f"{path} failed after {self.max_retries + 1} attempts: {last_error}")


def default_client(venue: str) -> PublicGetClient:
    if venue == "binance":
        return PublicGetClient(BINANCE_FAPI)
    if venue == "bybit":
        return PublicGetClient(BYBIT_API)
    if venue == "okx":
        return PublicGetClient(OKX_API)
    raise CexDataError(f"unknown venue {venue!r}")


# --------------------------------------------------------------------------- funding


def collect_funding(
    client: PublicGetClient,
    venue: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    now_ms: int | None = None,
    max_pages: int = 10_000,
) -> Collected:
    observed = now_ms if now_ms is not None else current_time_ms()
    _check_window(start_ms, end_ms, observed)
    if venue == "binance":
        pages, events = _binance_funding(client, symbol, start_ms, end_ms, max_pages)
    elif venue == "bybit":
        pages, events = _bybit_funding(client, symbol, start_ms, end_ms, max_pages)
    elif venue == "okx":
        pages, events = _okx_funding(client, symbol, start_ms, end_ms, max_pages)
    else:
        raise CexDataError(f"unknown venue {venue!r}")
    rows = pd.DataFrame(
        [{"venue": venue, "symbol": symbol, "time_ms": t, "funding_rate": r} for t, r in sorted(events.items())],
        columns=["venue", "symbol", "time_ms", "funding_rate"],
    )
    rows["time"] = pd.to_datetime(rows["time_ms"].astype("int64"), unit="ms", utc=True)
    rows["available_at"] = rows["time"]
    if rows.empty:
        status = STATUS_EMPTY
        truncated_start = False
    else:
        oldest_ms = int(rows["time_ms"].min())
        # Funding settles every 1h-8h; a gap > 24h after requested start_ms indicates provider history truncation (e.g. OKX).
        truncated_start = oldest_ms > start_ms + 24 * HOUR_MS
        status = STATUS_PARTIAL_HISTORY if truncated_start else STATUS_COMPLETE
    return Collected(
        dataset=f"{venue}_funding",
        params={"coin": symbol, "start_ms": start_ms, "end_ms": end_ms},
        pages=pages,
        rows=rows,
        status=status,
        observed_at_ms=observed,
        notes={"page_count": len(pages), "truncated_start": truncated_start},
        provider=venue,
    )


def _binance_funding(client, symbol, start_ms, end_ms, max_pages):
    pages: list[Any] = []
    events: dict[int, float] = {}
    cursor = start_ms
    while True:
        _page_guard(pages, max_pages, symbol)
        page = client.get(
            "/fapi/v1/fundingRate",
            {"symbol": symbol, "startTime": cursor, "endTime": end_ms, "limit": BINANCE_FUNDING_LIMIT},
        )
        if not isinstance(page, list):
            raise CexDataError(f"binance fundingRate returned {type(page).__name__}")
        pages.append(page)
        for item in page:
            if item.get("symbol") != symbol:
                raise CexDataError(f"binance fundingRate returned {item.get('symbol')!r} for {symbol!r}")
            t = int(item["fundingTime"])
            if start_ms <= t <= end_ms:
                events[t] = float(item["fundingRate"])
        if len(page) < BINANCE_FUNDING_LIMIT:
            break
        last = max(int(item["fundingTime"]) for item in page)
        if last >= end_ms or last < cursor:
            break
        cursor = last + 1
    return pages, events


def _bybit_funding(client, symbol, start_ms, end_ms, max_pages):
    # Bybit returns newest first; page backwards by moving endTime.
    pages: list[Any] = []
    events: dict[int, float] = {}
    cursor_end = end_ms
    while True:
        _page_guard(pages, max_pages, symbol)
        body = client.get(
            "/v5/market/funding/history",
            {"category": "linear", "symbol": symbol, "startTime": start_ms, "endTime": cursor_end, "limit": BYBIT_FUNDING_LIMIT},
        )
        page = _bybit_list(body, "funding/history")
        pages.append(body)
        for item in page:
            if item.get("symbol") != symbol:
                raise CexDataError(f"bybit funding returned {item.get('symbol')!r} for {symbol!r}")
            t = int(item["fundingRateTimestamp"])
            if start_ms <= t <= end_ms:
                events[t] = float(item["fundingRate"])
        if len(page) < BYBIT_FUNDING_LIMIT:
            break
        oldest = min(int(item["fundingRateTimestamp"]) for item in page)
        if oldest <= start_ms or oldest > cursor_end:
            break
        cursor_end = oldest - 1
    return pages, events


# --------------------------------------------------------------------------- klines


def collect_klines_1h(
    client: PublicGetClient,
    venue: str,
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    now_ms: int | None = None,
    max_pages: int = 10_000,
    price_scale: float | None = None,
    hl_coin: str | None = None,
) -> Collected:
    """Closed 1h perp klines; the still-open bar is dropped."""
    observed = now_ms if now_ms is not None else current_time_ms()
    _check_window(start_ms, end_ms, observed)
    if venue == "binance":
        pages, bars = _binance_klines(client, symbol, start_ms, end_ms, max_pages)
    elif venue == "bybit":
        pages, bars = _bybit_klines(client, symbol, start_ms, end_ms, max_pages)
    elif venue == "okx":
        pages, bars = _okx_klines(client, symbol, start_ms, end_ms, max_pages, price_scale=price_scale, hl_coin=hl_coin)
    else:
        raise CexDataError(f"unknown venue {venue!r}")
    closed = [b for t, b in sorted(bars.items()) if start_ms <= t <= end_ms and b["close_time_ms"] < observed]
    rows = pd.DataFrame(
        [{"venue": venue, "symbol": symbol, "interval": "1h", **b} for b in closed],
        columns=["venue", "symbol", "interval", "open_time_ms", "close_time_ms", "open", "high", "low", "close", "volume"],
    )
    rows["open_time"] = pd.to_datetime(rows["open_time_ms"].astype("int64"), unit="ms", utc=True)
    rows["available_at"] = pd.to_datetime((rows["close_time_ms"] + 1).astype("int64"), unit="ms", utc=True)
    first_open = int(rows["open_time_ms"].iloc[0]) if not rows.empty else None
    if rows.empty:
        status = STATUS_EMPTY
    elif first_open - start_ms >= HOUR_MS:
        status = STATUS_PARTIAL_HISTORY
    else:
        status = STATUS_COMPLETE
    return Collected(
        dataset=f"{venue}_klines",
        params={"coin": symbol, "interval": "1h", "start_ms": start_ms, "end_ms": end_ms},
        pages=pages,
        rows=rows,
        status=status,
        observed_at_ms=observed,
        notes={"page_count": len(pages), "first_open_ms": first_open},
        provider=venue,
    )


def _bar(t, o, h, low, c, v):
    return {
        "open_time_ms": int(t),
        "close_time_ms": int(t) + HOUR_MS - 1,
        "open": float(o),
        "high": float(h),
        "low": float(low),
        "close": float(c),
        "volume": float(v),
    }


def _binance_klines(client, symbol, start_ms, end_ms, max_pages):
    pages: list[Any] = []
    bars: dict[int, dict[str, Any]] = {}
    cursor = start_ms
    while cursor <= end_ms:
        _page_guard(pages, max_pages, symbol)
        page = client.get(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": "1h", "startTime": cursor, "endTime": end_ms, "limit": BINANCE_KLINE_LIMIT},
        )
        if not isinstance(page, list):
            raise CexDataError(f"binance klines returned {type(page).__name__}")
        pages.append(page)
        if not page:
            break
        for k in page:
            bars[int(k[0])] = _bar(k[0], k[1], k[2], k[3], k[4], k[5])
        last = max(int(k[0]) for k in page)
        if len(page) < BINANCE_KLINE_LIMIT or last < cursor:
            break
        cursor = last + HOUR_MS
    return pages, bars


def _bybit_klines(client, symbol, start_ms, end_ms, max_pages):
    pages: list[Any] = []
    bars: dict[int, dict[str, Any]] = {}
    cursor_end = end_ms
    while cursor_end >= start_ms:
        _page_guard(pages, max_pages, symbol)
        body = client.get(
            "/v5/market/kline",
            {"category": "linear", "symbol": symbol, "interval": "60", "start": start_ms, "end": cursor_end, "limit": BYBIT_KLINE_LIMIT},
        )
        page = _bybit_list(body, "kline")
        pages.append(body)
        if not page:
            break
        for k in page:
            bars[int(k[0])] = _bar(k[0], k[1], k[2], k[3], k[4], k[5])
        oldest = min(int(k[0]) for k in page)
        if len(page) < BYBIT_KLINE_LIMIT or oldest <= start_ms or oldest > cursor_end:
            break
        cursor_end = oldest - 1
    return pages, bars


# --------------------------------------------------------------------------- helpers


def _bybit_list(body: Any, what: str) -> list[Any]:
    if not isinstance(body, dict) or body.get("retCode") != 0:
        code = body.get("retCode") if isinstance(body, dict) else None
        msg = body.get("retMsg") if isinstance(body, dict) else None
        raise CexDataError(f"bybit {what} retCode={code} retMsg={msg}")
    page = (body.get("result") or {}).get("list")
    if not isinstance(page, list):
        raise CexDataError(f"bybit {what} result.list missing")
    return page


def _okx_data(body: Any, what: str) -> list[Any]:
    if not isinstance(body, dict) or str(body.get("code")) != "0":
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("msg") if isinstance(body, dict) else None
        raise CexDataError(f"okx {what} code={code} msg={msg}")
    page = body.get("data")
    if not isinstance(page, list):
        raise CexDataError(f"okx {what} data missing")
    return page


def _okx_funding(client, symbol, start_ms, end_ms, max_pages):
    pages: list[Any] = []
    events: dict[int, float] = {}
    after_cursor: int | None = end_ms + 1
    while True:
        _page_guard(pages, max_pages, symbol)
        params: dict[str, Any] = {"instId": symbol, "limit": OKX_FUNDING_LIMIT}
        if after_cursor is not None:
            params["after"] = str(after_cursor)
        body = client.get("/api/v5/public/funding-rate-history", params)
        page = _okx_data(body, "funding-rate-history")
        pages.append(body)
        if not page:
            break
        oldest = None
        for item in page:
            t = int(item["fundingTime"])
            if start_ms <= t <= end_ms:
                events[t] = float(item["fundingRate"])
            oldest = t if oldest is None else min(oldest, t)
        if oldest is None or oldest <= start_ms or len(page) < OKX_FUNDING_LIMIT:
            break
        if after_cursor is not None and oldest >= after_cursor:
            break  # cursor did not advance; avoid looping until max_pages
        after_cursor = oldest
    return pages, events


# Immutable Hyperliquid k*-listed bases (kPEPE, kSHIB, kBONK, kFLOKI, kLUNC, kDOGS, kNEIRO).
# 1-token HL coins such as CAT or MOG are intentionally excluded and okx_symbol NEVER mutates this!
OKX_DEFAULT_HL_KILO_BASES: frozenset[str] = frozenset({"PEPE", "SHIB", "BONK", "FLOKI", "LUNC", "DOGS", "NEIRO"})
OKX_KILO_BASES: frozenset[str] = OKX_DEFAULT_HL_KILO_BASES


def is_hl_kilo_coin(coin: str) -> bool:
    """True for Hyperliquid 1,000-unit perp symbols such as ``kPEPE`` or ``kFloki``."""
    return len(coin) > 1 and coin.startswith("k") and coin[1].isupper()


def okx_price_scale(symbol: str, *, hl_coin: str | None = None) -> float:
    """Price multiplier aligning OKX 1-token swaps (``PEPE-USDT-SWAP``) to HL 1,000-token ``k*`` perps.

    When ``hl_coin`` (or a raw HL coin symbol such as ``"kPEPE"`` vs ``"CAT"``) is supplied,
    scaling is determined strictly by :func:`is_hl_kilo_coin` so 1-token HL coins (``"CAT"``,
    ``"MOG"``) are never scaled by 1,000x.
    """
    if hl_coin is not None:
        return 1_000.0 if is_hl_kilo_coin(str(hl_coin).strip()) else 1.0
    raw = str(symbol or "").strip()
    if not raw:
        return 1.0
    sym_u = raw.upper()
    if not sym_u.endswith("-USDT-SWAP"):
        return 1_000.0 if is_hl_kilo_coin(raw) else 1.0
    base = sym_u[: -len("-USDT-SWAP")]
    return 1_000.0 if base in OKX_DEFAULT_HL_KILO_BASES else 1.0


def _okx_klines(client, symbol, start_ms, end_ms, max_pages, *, price_scale: float | None = None, hl_coin: str | None = None):
    pages: list[Any] = []
    bars: dict[int, dict[str, Any]] = {}
    after_cursor: int | None = end_ms + 1
    px_scale = (
        float(price_scale)
        if (price_scale is not None and price_scale > 0.0)
        else okx_price_scale(symbol, hl_coin=hl_coin)
    )
    while True:
        _page_guard(pages, max_pages, symbol)
        params: dict[str, Any] = {"instId": symbol, "bar": "1H", "limit": OKX_KLINE_LIMIT}
        if after_cursor is not None:
            params["after"] = str(after_cursor)
        body = client.get("/api/v5/market/history-candles", params)
        page = _okx_data(body, "history-candles")
        pages.append(body)
        if not page:
            break
        oldest = None
        for k in page:
            t = int(k[0])
            # OKX candle: [ts, o, h, l, c, vol_contracts, volCcy, volCcyQuote, confirm]
            if start_ms <= t <= end_ms:
                vol_ccy = (float(k[6]) if len(k) > 6 and k[6] else float(k[5])) / px_scale
                bars[t] = _bar(
                    t,
                    float(k[1]) * px_scale,
                    float(k[2]) * px_scale,
                    float(k[3]) * px_scale,
                    float(k[4]) * px_scale,
                    vol_ccy,
                )
            oldest = t if oldest is None else min(oldest, t)
        if oldest is None or oldest <= start_ms or len(page) < OKX_KLINE_LIMIT:
            break
        if after_cursor is not None and oldest >= after_cursor:
            break  # cursor did not advance; avoid looping until max_pages
        after_cursor = oldest
    return pages, bars


def okx_symbol(coin: str) -> str:
    """Map Hyperliquid perp coin name to OKX USDT linear swap ``instId`` (pure, side-effect-free)."""
    base = coin[1:].upper() if is_hl_kilo_coin(coin) else coin.upper()
    return f"{base}-USDT-SWAP"


def _pos_float(val: Any) -> float:
    try:
        x = float(val)
    except (TypeError, ValueError):
        return 0.0
    return x if x > 0.0 and x == x and x != float("inf") else 0.0


def fetch_live_mid_prices(
    client: Any,
    venue: str,
    symbols: list[str] | set[str] | tuple[str, ...] | dict[str, str] | None = None,
    *,
    hl_coins: list[str] | set[str] | tuple[str, ...] | None = None,
) -> dict[str, float]:
    """Fetch synchronous live BBO mid prices (``symbol -> mid_px``) for a CEX linear perp venue.

    Used by ``run_hl_metamodel_paper_tick`` so CEX hedge legs are marked to market at the same
    wall-clock instant as Hyperliquid's live L2 quotes rather than a stale ``:00`` candle close.
    When ``symbols`` is a ``{hl_coin: inst_id}`` dict or ``hl_coins`` is provided on OKX, scaling
    is keyed directly off :func:`is_hl_kilo_coin` per HL coin.
    """
    v = venue.lower()
    scale_by_sym: dict[str, float] = {}
    if isinstance(symbols, dict):
        want: set[str] | None = {str(val) for val in symbols.values()}
        if v == "okx":
            for hc, inst in symbols.items():
                scale_by_sym[str(inst)] = 1_000.0 if is_hl_kilo_coin(str(hc)) else 1.0
    elif symbols is not None:
        if v == "okx":
            want = set()
            for raw_s in symbols:
                s_str = str(raw_s)
                if s_str.upper().endswith("-USDT-SWAP"):
                    want.add(s_str)
                else:
                    inst = okx_symbol(s_str)
                    want.add(inst)
                    scale_by_sym[inst] = 1_000.0 if is_hl_kilo_coin(s_str) else 1.0
        else:
            want = {str(s) for s in symbols}
    else:
        want = None
    if v == "okx" and hl_coins is not None:
        for hc in hl_coins:
            hc_str = str(hc)
            scale_by_sym[okx_symbol(hc_str)] = 1_000.0 if is_hl_kilo_coin(hc_str) else 1.0

    out: dict[str, float] = {}
    if v == "binance":
        body = client.get("/fapi/v1/ticker/bookTicker", {})
        rows = body if isinstance(body, list) else ([body] if isinstance(body, dict) else [])
        for item in rows:
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "")
            if not sym or (want is not None and sym not in want):
                continue
            bid = _pos_float(item.get("bidPrice"))
            ask = _pos_float(item.get("askPrice"))
            if ask >= bid > 0.0:
                out[sym] = 0.5 * (bid + ask)
    elif v == "bybit":
        body = client.get("/v5/market/tickers", {"category": "linear"})
        for item in _bybit_list(body, "tickers"):
            if not isinstance(item, dict):
                continue
            sym = str(item.get("symbol") or "")
            if not sym or (want is not None and sym not in want):
                continue
            bid = _pos_float(item.get("bid1Price"))
            ask = _pos_float(item.get("ask1Price"))
            if bid > 0.0 and ask > 0.0:
                mid = 0.5 * (bid + ask) if ask >= bid else 0.0
            else:
                mid = _pos_float(item.get("markPrice")) or _pos_float(item.get("lastPrice"))
            if mid > 0.0:
                out[sym] = mid
    elif v == "okx":
        body = client.get("/api/v5/market/tickers", {"instType": "SWAP"})
        for item in _okx_data(body, "tickers"):
            if not isinstance(item, dict):
                continue
            sym = str(item.get("instId") or "")
            if not sym or (want is not None and sym not in want):
                continue
            px_scale = scale_by_sym.get(sym, okx_price_scale(sym))
            bid = _pos_float(item.get("bidPx"))
            ask = _pos_float(item.get("askPx"))
            if bid > 0.0 and ask > 0.0:
                mid = (0.5 * (bid + ask) * px_scale) if ask >= bid else 0.0
            else:
                mid = _pos_float(item.get("last")) * px_scale
            if mid > 0.0:
                out[sym] = mid
    else:
        raise ValueError(f"unsupported venue: {venue}")
    return out


def _page_guard(pages: list[Any], max_pages: int, symbol: str) -> None:
    if len(pages) >= max_pages:
        raise CexDataError(f"pagination exceeded {max_pages} pages for {symbol}")


def _check_window(start_ms: int, end_ms: int, observed_ms: int) -> None:
    if start_ms < 0 or end_ms < start_ms:
        raise CexDataError(f"invalid window {start_ms}..{end_ms}")
    if end_ms > observed_ms:
        raise CexDataError("window end is in the future; history must end at or before now")
