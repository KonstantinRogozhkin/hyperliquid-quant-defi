"""Train-only universe discovery, liquidity filtering, and coin selection for HL funding arb.

Research only. Everything that decides which coins enter the portfolio or which
parameter set is used reads strictly from ``hour_ms < split_hour_ms`` so there
is zero look-ahead into the test period.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Iterable, Literal

import numpy as np
import pandas as pd

from hl_quant import cex_perp_public as cex
from hl_quant import funding_arb as fa
from hl_quant import hyperliquid as hl

DAY_MS = 24 * fa.HOUR_MS
RankMetric = Literal["best_sharpe", "mean_sharpe", "mean_apr"]


def cex_symbol(coin: str) -> str:
    """HL ``kPEPE``-style thousand-unit coins trade as ``1000PEPEUSDT`` on CEXs."""
    if coin.startswith("k") and coin[1:].isupper():
        return f"1000{coin[1:]}USDT"
    return f"{coin}USDT"


@dataclass(frozen=True, slots=True)
class CoinCandidate:
    coin: str
    cex_symbol: str
    hl_day_notional_usd: float
    hl_oi_usd: float


@dataclass(frozen=True, slots=True)
class LiquidityStats:
    coin: str
    train_hours: int
    train_coverage: float
    hl_median_daily_vol_usd: float
    cex_median_daily_vol_usd: float
    min_venue_median_daily_vol_usd: float
    eligible: bool
    reject_reason: str


@dataclass(frozen=True, slots=True)
class UniverseEvaluation:
    split_hour_ms: int
    eligible_coins: list[str]
    selected_coins: list[str]
    liquidity: pd.DataFrame
    train_ranking: pd.DataFrame
    selected_eval: fa.Evaluation
    all_eligible_eval: fa.Evaluation | None

    def to_dict(self) -> dict:
        return {
            "split_hour_ms": self.split_hour_ms,
            "eligible_count": len(self.eligible_coins),
            "eligible_coins": self.eligible_coins,
            "selected_coins": self.selected_coins,
            "selected_eval": self.selected_eval.to_dict(),
            "all_eligible_eval": self.all_eligible_eval.to_dict() if self.all_eligible_eval is not None else None,
            "liquidity": self.liquidity.to_dict(orient="records"),
            "train_ranking": self.train_ranking.to_dict(orient="records"),
        }


# --------------------------------------------------------------------------- discovery


def _f(value: object) -> float:
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0
    return out if math.isfinite(out) else 0.0


def _binance_perp_symbols(client: cex.PublicGetClient) -> set[str]:
    payload = client.get("/fapi/v1/exchangeInfo", {})
    symbols = payload.get("symbols") if isinstance(payload, dict) else None
    if not isinstance(symbols, list):
        raise cex.CexDataError("binance exchangeInfo missing symbols list")
    return {
        str(item["symbol"])
        for item in symbols
        if isinstance(item, dict)
        and item.get("status") == "TRADING"
        and item.get("contractType") == "PERPETUAL"
        and item.get("quoteAsset") == "USDT"
    }


def _bybit_perp_symbols(client: cex.PublicGetClient) -> set[str]:
    found: set[str] = set()
    cursor = ""
    for _ in range(20):
        params: dict[str, object] = {"category": "linear", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        payload = client.get("/v5/market/instruments-info", params)
        result = payload.get("result") if isinstance(payload, dict) else None
        rows = result.get("list") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            raise cex.CexDataError("bybit instruments-info missing result.list")
        for item in rows:
            if (
                isinstance(item, dict)
                and item.get("status") == "Trading"
                and item.get("contractType") == "LinearPerpetual"
                and item.get("quoteCoin") == "USDT"
            ):
                found.add(str(item["symbol"]))
        cursor = str(result.get("nextPageCursor") or "")
        if not cursor:
            break
    return found


def discover_common_coins(
    hl_client: hl.HyperliquidInfoClient,
    cex_client: cex.PublicGetClient,
    venue: str,
    *,
    min_prefilter_vol_usd: float = 1_000_000.0,
    max_coins: int | None = None,
) -> list[CoinCandidate]:
    """List active Hyperliquid perps that also trade as USDT linear perps on ``venue``.

    Note: ``min_prefilter_vol_usd`` uses current HL 24h notional volume solely as a
    lightweight download pre-filter; the real backtest liquidity gate runs strictly
    on historical train-period bars via :func:`train_liquidity_stats`.
    """
    if venue not in cex.VENUES:
        raise cex.CexDataError(f"unknown venue {venue!r}")
    if venue not in ("binance", "bybit"):
        raise cex.CexDataError(f"discover_common_coins does not support venue {venue!r} yet")

    meta_ctx = hl_client.info({"type": "metaAndAssetCtxs"})
    if not (isinstance(meta_ctx, list) and len(meta_ctx) == 2 and isinstance(meta_ctx[0], dict) and "universe" in meta_ctx[0]):
        raise hl.HyperliquidError("metaAndAssetCtxs shape changed")
    universe, ctxs = meta_ctx[0]["universe"], meta_ctx[1]
    if len(universe) != len(ctxs):
        raise hl.HyperliquidError("metaAndAssetCtxs universe/context length mismatch")

    cex_symbols = _binance_perp_symbols(cex_client) if venue == "binance" else _bybit_perp_symbols(cex_client)

    candidates: list[CoinCandidate] = []
    for asset, ctx in zip(universe, ctxs):
        if asset.get("isDelisted"):
            continue
        coin = str(asset.get("name") or "").strip()
        if not coin:
            continue
        sym = cex_symbol(coin)
        if sym not in cex_symbols:
            continue
        day_vol = _f(ctx.get("dayNtlVlm"))
        if day_vol < min_prefilter_vol_usd:
            continue
        oi_usd = _f(ctx.get("openInterest")) * _f(ctx.get("markPx"))
        candidates.append(
            CoinCandidate(
                coin=coin,
                cex_symbol=sym,
                hl_day_notional_usd=day_vol,
                hl_oi_usd=oi_usd,
            )
        )
    candidates.sort(key=lambda c: (-c.hl_day_notional_usd, c.coin))
    if max_coins is not None and max_coins > 0:
        candidates = candidates[:max_coins]
    return candidates


# --------------------------------------------------------------------------- train-only liquidity


def _median_daily_notional(train: pd.DataFrame, col: str, *, min_hours_per_day: int = 12) -> float:
    if col not in train.columns or train.empty:
        return 0.0
    series = train[col].dropna()
    if series.empty:
        return 0.0
    days = series.index.to_numpy(dtype="int64") // DAY_MS
    grouped = series.groupby(days).agg(["count", "sum"])
    valid_days = grouped[grouped["count"] >= min_hours_per_day]["sum"]
    if valid_days.empty:
        valid_days = grouped["sum"]
    return float(valid_days.median()) if not valid_days.empty else 0.0


def train_liquidity_stats(
    coin: str,
    panel: pd.DataFrame,
    *,
    split_hour_ms: int,
    expected_train_hours: int,
    min_train_daily_vol_usd: float = 5_000_000.0,
    min_train_coverage: float = 0.80,
) -> LiquidityStats:
    """Compute train-only coverage and median daily notional volume (zero test leakage)."""
    train = panel[panel.index < split_hour_ms]
    valid_px = train["hl_close"].notna() & train["cex_close"].notna()
    train_hours = int(valid_px.sum())
    coverage = float(train_hours / expected_train_hours) if expected_train_hours > 0 else 0.0
    hl_med = _median_daily_notional(train, "hl_notional_usd")
    cex_med = _median_daily_notional(train, "cex_notional_usd")
    min_med = min(hl_med, cex_med)

    reasons: list[str] = []
    if coverage < min_train_coverage:
        reasons.append(f"coverage<{min_train_coverage:.2f}")
    if min_med < min_train_daily_vol_usd:
        reasons.append(f"min_daily_vol<{min_train_daily_vol_usd:.0f}")
    return LiquidityStats(
        coin=coin,
        train_hours=train_hours,
        train_coverage=coverage,
        hl_median_daily_vol_usd=hl_med,
        cex_median_daily_vol_usd=cex_med,
        min_venue_median_daily_vol_usd=min_med,
        eligible=not reasons,
        reject_reason=";".join(reasons),
    )


# --------------------------------------------------------------------------- train-only ranking & eval


def rank_coins_on_train(
    panels: dict[str, pd.DataFrame],
    *,
    split_hour_ms: int,
    costs: fa.Costs,
    grid: Iterable[fa.Params] = fa.DEFAULT_GRID,
    rank_by: RankMetric = "best_sharpe",
    min_train_tim: float = 0.05,
) -> pd.DataFrame:
    """Score each coin strictly on ``hour_ms < split_hour_ms``."""
    grid = tuple(grid)
    rows: list[dict[str, object]] = []
    for coin, panel in sorted(panels.items()):
        train_panel = panel[panel.index < split_hour_ms]
        if len(train_panel) < max(p.lookback_h for p in grid) + 2:
            continue
        grid_metrics = [fa.metrics(fa.simulate(train_panel, p, costs)) for p in grid]
        active = [m for m in grid_metrics if m["time_in_market"] >= min_train_tim]
        if active:
            best = max(active, key=lambda m: (m["sharpe"], m["apr"]))
        else:
            best = max(grid_metrics, key=lambda m: (m["sharpe"], m["apr"]))
        mean_sharpe = float(np.mean([m["sharpe"] for m in grid_metrics]))
        mean_apr = float(np.mean([m["apr"] for m in grid_metrics]))
        positive_grid = int(sum(1 for m in grid_metrics if m["apr"] > 0))
        score = {
            "best_sharpe": float(best["sharpe"]),
            "mean_sharpe": mean_sharpe,
            "mean_apr": mean_apr,
        }[rank_by]
        rows.append(
            {
                "coin": coin,
                "rank_score": score,
                "best_train_sharpe": float(best["sharpe"]),
                "best_train_apr": float(best["apr"]),
                "best_train_tim": float(best["time_in_market"]),
                "mean_train_sharpe": mean_sharpe,
                "mean_train_apr": mean_apr,
                "positive_train_grid": positive_grid,
                "grid_size": len(grid),
            }
        )
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    return df.sort_values(
        by=["rank_score", "best_train_apr", "coin"],
        ascending=[False, False, True],
    ).reset_index(drop=True)


def evaluate_universe(
    panels: dict[str, pd.DataFrame],
    *,
    costs: fa.Costs = fa.Costs(hl_fee_bps=1.5, cex_fee_bps=2.0, slippage_bps_per_leg=1.0),
    grid: Iterable[fa.Params] = fa.DEFAULT_GRID,
    train_fraction: float = 0.6,
    top_k: int = 10,
    min_train_daily_vol_usd: float = 5_000_000.0,
    min_train_coverage: float = 0.80,
    min_train_tim: float = 0.05,
    require_positive_train_score: bool = True,
    rank_by: RankMetric = "best_sharpe",
    window_start_ms: int | None = None,
    window_end_ms: int | None = None,
) -> UniverseEvaluation:
    """Filter universe by train liquidity, select top-K coins on train, evaluate untouched on test."""
    if not panels:
        raise ValueError("panels must not be empty")
    if not 0.1 <= train_fraction <= 0.9:
        raise ValueError("train_fraction must be within [0.1, 0.9]")
    if top_k < 1:
        raise ValueError("top_k must be >= 1")

    grid = tuple(grid)
    start_ms = int(window_start_ms) if window_start_ms is not None else min(int(p.index.min()) for p in panels.values())
    end_ms = int(window_end_ms) if window_end_ms is not None else max(int(p.index.max()) for p in panels.values())
    total_hours = int((end_ms - start_ms) // fa.HOUR_MS) + 1
    expected_train_hours = int(total_hours * train_fraction)
    split_hour_ms = start_ms + expected_train_hours * fa.HOUR_MS

    liq_records = [
        train_liquidity_stats(
            coin,
            panel,
            split_hour_ms=split_hour_ms,
            expected_train_hours=expected_train_hours,
            min_train_daily_vol_usd=min_train_daily_vol_usd,
            min_train_coverage=min_train_coverage,
        )
        for coin, panel in sorted(panels.items())
    ]
    liq_df = pd.DataFrame([asdict(r) for r in liq_records])
    eligible_coins = [r.coin for r in liq_records if r.eligible]
    if not eligible_coins:
        raise ValueError("no coins passed train liquidity and coverage filters")

    eligible_panels = {c: panels[c] for c in eligible_coins}
    ranking = rank_coins_on_train(
        eligible_panels,
        split_hour_ms=split_hour_ms,
        costs=costs,
        grid=grid,
        rank_by=rank_by,
        min_train_tim=min_train_tim,
    )
    if ranking.empty:
        raise ValueError("no eligible coins had enough train history for ranking")

    candidates = ranking[ranking["rank_score"] > 0] if require_positive_train_score else ranking
    if candidates.empty:
        # Fall back to top_k by rank_score so the report still records an honest result.
        candidates = ranking
    selected_coins = candidates["coin"].head(top_k).tolist()
    selected_panels = {c: eligible_panels[c] for c in selected_coins}

    selected_eval = fa.evaluate(
        selected_panels,
        grid=grid,
        costs=costs,
        train_fraction=train_fraction,
        split_hour_ms=split_hour_ms,
    )
    all_eligible_eval: fa.Evaluation | None = None
    try:
        all_eligible_eval = fa.evaluate(
            eligible_panels,
            grid=grid,
            costs=costs,
            train_fraction=train_fraction,
            split_hour_ms=split_hour_ms,
        )
    except ValueError:
        all_eligible_eval = None

    return UniverseEvaluation(
        split_hour_ms=split_hour_ms,
        eligible_coins=eligible_coins,
        selected_coins=selected_coins,
        liquidity=liq_df,
        train_ranking=ranking,
        selected_eval=selected_eval,
        all_eligible_eval=all_eligible_eval,
    )
