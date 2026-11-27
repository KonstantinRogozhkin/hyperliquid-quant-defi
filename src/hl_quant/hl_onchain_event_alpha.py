"""Read-only On-Chain Event Alpha Scanner & Forward-Outcome Logger for Hyperliquid (H2+H4+H5+H6).

Monitors and persists four structural Hyperliquid-native hypotheses in append-only Parquet ledgers:
- **H2 — L1 Whale Liquidation Wall Proximity**: flags coins where active leaderboard whales hold
  concentrated liquidation walls near mark price (`< 2.5%` wall or `>= $2M` in `0-5%` or `>= $5M` weighted).
- **H4 — HyperEVM <-> HyperCore Impulse Dislocations**: logs peg-adjusted V3 pool spreads (`WHYPE/USDC`,
  `UBTC/USDT0`, `UETH/USDT0`, etc.) and `L1Read` precompile drift, alerting when `|peg_adj| >= 10 bps`
  or `net_arb_bps > 0`.
- **H5 — HLP Inventory Skew**: reads live open positions of HLP child strategies (`Strategy A/B`,
  `Liquidator`), aggregates net USD inventory skew per coin, and flags extreme concentrations.
- **H6 — BookMid vs Mark vs Oracle Stress Dislocations**: detects acute book vacuums, oracle
  decoupling, and crowded-funding basis across liquid HyperCore perps.

Every tick automatically back-fills realized `+15m / +1h / +4h` mark-price returns onto prior
H2, H5, and H6 events (`grade_forward_outcomes`) so the 15-minute cron builds an unbiased live OOS dataset.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
import time
from typing import Any

import httpx
import numpy as np
import pandas as pd

from hl_quant import hl_hyperevm_defi as evm
from hl_quant import hl_liquidation_and_funding_alpha as liq


@dataclass(frozen=True, slots=True)
class OnChainEventConfig:
    top_whales: int = 60
    # H2 thresholds
    h2_min_cluster_log_usd: float = 200_000.0
    h2_alert_wall_dist_pct: float = 2.5
    h2_alert_0_5_usd: float = 2_000_000.0
    # Distance-weighted (0-2% x1.0, 2-5% x0.6, 5-10% x0.3 — the cascade_bias weights) notional of
    # the dominant liquidation side. Raw 0-10% notional ignored distance: a lone wall 9% away fired.
    h2_alert_weighted_usd: float = 5_000_000.0
    # H4 thresholds
    h4_min_depth_usd: float = 250_000.0
    h4_alert_peg_spread_bps: float = 10.0
    h4_alert_l1read_drift_bps: float = 15.0
    # H5 thresholds (HLP Strategy A+B net exposure stays < $15k/coin in calm markets)
    h5_min_log_usd: float = 2_000.0
    h5_alert_skew_usd: float = 100_000.0
    h5_alert_tvl_share_pct: float = 0.25
    h5_top_n: int = 12
    # H6 thresholds (BookMid vs Mark vs Oracle Stress Dislocation)
    h6_min_vol_usd: float = 5_000_000.0
    h6_min_log_bps: float = 10.0
    h6_alert_mid_mark_bps: float = 50.0
    h6_alert_mark_oracle_bps: float = 50.0
    h6_crowded_funding_apr_pct: float = 150.0  # Extreme funding where Mark-Oracle discount is structural basis
    h6_crowded_basis_alert_bps: float = 200.0  # Higher alert bar when dislocation is crowded funding basis
    h6_top_n: int = 12
    # Operational hygiene: 6h whale leaderboard cache & 4h per-key alert deduplication cooldown
    whale_cache_ttl_sec: int = 6 * 3600
    alert_cooldown_sec: int = 4 * 3600
    alert_escalation_mult: float = 1.5

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def _write_parquet_atomic(path: Path, df: pd.DataFrame) -> None:
    tmp = path.with_suffix(".tmp.parquet")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def _append_parquet(path: Path, rows: list[dict[str, Any]]) -> pd.DataFrame:
    if not rows:
        return pd.read_parquet(path) if path.is_file() else pd.DataFrame()
    new_df = pd.DataFrame(rows)
    if path.is_file():
        old = pd.read_parquet(path)
        combined = pd.concat([old, new_df], ignore_index=True)
    else:
        combined = new_df
    _write_parquet_atomic(path, combined)
    return combined


# --------------------------------------------------------------------------- H2 / H4 / H5 Pure Extractors


def extract_h2_liq_events(
    clusters: list[liq.CoinLiquidationCluster],
    *,
    ts_ms: int,
    cfg: OnChainEventConfig,
) -> list[dict[str, Any]]:
    """Extract H2 whale liquidation proximity events from aggregated L1 clusters."""
    rows: list[dict[str, Any]] = []
    for c in clusters:
        long_0_5 = float(c.long_liq_0_2_usd) + float(c.long_liq_2_5_usd)
        short_0_5 = float(c.short_liq_0_2_usd) + float(c.short_liq_2_5_usd)
        tot_0_5 = long_0_5 + short_0_5
        if c.total_0_10_usd < cfg.h2_min_cluster_log_usd and tot_0_5 < cfg.h2_min_cluster_log_usd:
            continue
        near_l = c.nearest_long_dist_pct
        near_s = c.nearest_short_dist_pct
        min_wall = min(
            [d for d in (near_l, near_s) if d is not None],
            default=None,
        )
        long_w = (
            float(c.long_liq_0_2_usd) * liq.BIAS_W_0_2
            + float(c.long_liq_2_5_usd) * liq.BIAS_W_2_5
            + float(c.long_liq_5_10_usd) * liq.BIAS_W_5_10
        )
        short_w = (
            float(c.short_liq_0_2_usd) * liq.BIAS_W_0_2
            + float(c.short_liq_2_5_usd) * liq.BIAS_W_2_5
            + float(c.short_liq_5_10_usd) * liq.BIAS_W_5_10
        )
        weighted_near = max(long_w, short_w)
        # A "near wall" only counts if there is real notional inside 0-5% (a dust position 1% away
        # with total_0_5_usd == 0 must not raise an alert); far walls only alert when large enough
        # after distance weighting (a $6M wall 9% away weighs $1.8M and stays silent).
        is_alert = bool(
            (
                min_wall is not None
                and min_wall <= cfg.h2_alert_wall_dist_pct
                and tot_0_5 >= cfg.h2_min_cluster_log_usd
            )
            or (tot_0_5 >= cfg.h2_alert_0_5_usd)
            or (weighted_near >= cfg.h2_alert_weighted_usd)
        )
        rows.append(
            {
                "ts_ms": ts_ms,
                "hypothesis": "H2_LIQ_WALL",
                "coin": c.coin,
                "mark_px": c.mark_px,
                "whales_count": c.whales_count,
                "total_0_5_usd": round(tot_0_5, 2),
                "total_0_10_usd": round(c.total_0_10_usd, 2),
                "long_0_5_usd": round(long_0_5, 2),
                "short_0_5_usd": round(short_0_5, 2),
                "weighted_near_usd": round(weighted_near, 2),
                "nearest_wall_pct": round(min_wall, 3) if min_wall is not None else np.nan,
                "cascade_bias": c.cascade_bias,
                "alert": is_alert,
                "ret_15m_bps": np.nan,
                "ret_1h_bps": np.nan,
                "ret_4h_bps": np.nan,
            }
        )
    rows.sort(key=lambda r: (-int(r["alert"]), -float(r["total_0_10_usd"])))
    return rows


def extract_h4_evm_dislocations(
    evm_snap: evm.HyperEVMDefiSnapshot,
    *,
    ts_ms: int,
    cfg: OnChainEventConfig,
) -> list[dict[str, Any]]:
    """Extract H4 HyperEVM <-> HyperCore peg-adjusted dislocation rows."""
    max_drift = max((abs(o.drift_bps) for o in evm_snap.l1_read_oracles), default=0.0)
    rows: list[dict[str, Any]] = []
    for a in evm_snap.evm_arb:
        deep_enough = float(a.virtual_depth_usd) >= cfg.h4_min_depth_usd
        meaningful_disloc = deep_enough and (
            abs(a.peg_adjusted_spread_bps) >= max(cfg.h4_alert_peg_spread_bps, float(a.pool_fee_bps) + 8.0)
        )
        is_alert = bool(
            (a.executable and deep_enough)
            or meaningful_disloc
        )
        rows.append(
            {
                "ts_ms": ts_ms,
                "hypothesis": "H4_EVM_DISLOC",
                "dex": a.dex,
                "pool": a.label,
                "direction": a.direction,
                "evm_price": a.evm_price,
                "core_ref_price": a.core_ref_price,
                "gross_spread_bps": a.gross_spread_bps,
                "peg_adjusted_spread_bps": a.peg_adjusted_spread_bps,
                "net_arb_bps": a.net_arb_bps,
                "net_arb_25k_bps": a.net_arb_25k_bps,
                "virtual_depth_usd": a.virtual_depth_usd,
                "slippage_25k_bps": a.slippage_25k_bps,
                "usdt0_peg_discount_bps": evm_snap.chain.usdt0_peg_discount_bps,
                "l1read_max_drift_bps": round(max_drift, 3),
                "executable": bool(a.executable),
                "alert": is_alert,
            }
        )
    rows.sort(key=lambda r: (-int(r["alert"]), -abs(float(r["peg_adjusted_spread_bps"]))))
    return rows


def extract_h5_hlp_inventory_skew(
    child_ch_states: list[dict[str, Any]],
    mark_prices: dict[str, float],
    *,
    hlp_tvl_usd: float,
    ts_ms: int,
    cfg: OnChainEventConfig,
) -> list[dict[str, Any]]:
    """Aggregate signed USD exposure across all HLP child vaults to detect extreme H5 skew."""
    net_by_coin: dict[str, float] = {}
    gross_by_coin: dict[str, float] = {}
    vault_count: dict[str, int] = {}

    for ch in child_ch_states:
        for item in ch.get("assetPositions") or []:
            pos = item.get("position") or {}
            coin = str(pos.get("coin") or "").strip()
            szi = _f(pos.get("szi"))
            if not coin or abs(szi) <= 1e-12:
                continue
            mark = mark_prices.get(coin, 0.0)
            if mark <= 0:
                val = _f(pos.get("positionValue"))
                signed_usd = math.copysign(abs(val), szi)
            else:
                signed_usd = szi * mark
            net_by_coin[coin] = net_by_coin.get(coin, 0.0) + signed_usd
            gross_by_coin[coin] = gross_by_coin.get(coin, 0.0) + abs(signed_usd)
            vault_count[coin] = vault_count.get(coin, 0) + 1

    tvl = max(hlp_tvl_usd, 1.0)
    rows: list[dict[str, Any]] = []
    for coin, net_usd in net_by_coin.items():
        abs_net = abs(net_usd)
        if abs_net < cfg.h5_min_log_usd:
            continue
        share_pct = abs_net / tvl * 100.0
        hlp_side = "HLP_LONG" if net_usd > 0 else "HLP_SHORT"
        is_alert = bool(abs_net >= cfg.h5_alert_skew_usd or share_pct >= cfg.h5_alert_tvl_share_pct)
        rows.append(
            {
                "ts_ms": ts_ms,
                "hypothesis": "H5_HLP_SKEW",
                "coin": coin,
                "mark_px": mark_prices.get(coin, 0.0),
                "hlp_net_usd": round(net_usd, 2),
                "hlp_abs_usd": round(abs_net, 2),
                "hlp_gross_usd": round(gross_by_coin.get(coin, abs_net), 2),
                "hlp_tvl_share_pct": round(share_pct, 3),
                "hlp_side": hlp_side,
                "vaults_holding": vault_count.get(coin, 1),
                "alert": is_alert,
                "ret_15m_bps": np.nan,
                "ret_1h_bps": np.nan,
                "ret_4h_bps": np.nan,
            }
        )
    rows.sort(key=lambda r: -float(r["hlp_abs_usd"]))
    return rows[: cfg.h5_top_n]


def extract_h6_book_mark_oracle_dislocations(
    perps_raw: dict[str, dict[str, Any]],
    *,
    ts_ms: int,
    cfg: OnChainEventConfig,
) -> list[dict[str, Any]]:
    """Extract H6 ``BookMid vs Mark vs Oracle`` stress dislocations across liquid HyperCore perps.

    Separates three distinct microstructure regimes:
    - ``CRASH_BOOK_STRESS``: ``|mid_vs_mark_bps| >= 50`` (acute liquidation book vacuum, highest alert priority).
    - ``ORACLE_DECOUPLING``: ``|mark_vs_oracle_bps| >= 50`` under normal funding (``|funding APR| < 150%``).
    - ``CROWDED_FUNDING_BASIS``: ``|mark_vs_oracle_bps| >= 50`` paired with extreme funding
      (e.g., ``INIT`` at ``-494% APR``) — structural crowded-short discount logged for study without
      spamming crash alerts unless it exceeds ``h6_crowded_basis_alert_bps`` (200 bps).
    """
    rows: list[dict[str, Any]] = []
    for coin, info in perps_raw.items():
        day_vol = _f(info.get("day_vol_usd"))
        mark_px = _f(info.get("mark_px"))
        mid_px = _f(info.get("mid_px"), mark_px)
        oracle_px = _f(info.get("oracle_px"), mark_px)
        fund_h = _f(info.get("funding_hourly"))
        fund_apr = fund_h * 24.0 * 365.0 * 100.0
        if day_vol < cfg.h6_min_vol_usd or mark_px <= 0 or oracle_px <= 0 or mid_px <= 0:
            continue
        mid_vs_mark_bps = (mid_px - mark_px) / mark_px * 1e4
        mark_vs_oracle_bps = (mark_px - oracle_px) / oracle_px * 1e4
        max_disloc = max(abs(mid_vs_mark_bps), abs(mark_vs_oracle_bps))
        if max_disloc < cfg.h6_min_log_bps:
            continue

        is_book_crash = abs(mid_vs_mark_bps) >= cfg.h6_alert_mid_mark_bps
        is_crowded_funding = abs(fund_apr) >= cfg.h6_crowded_funding_apr_pct
        if is_book_crash:
            stress_type = "CRASH_BOOK_STRESS"
            is_alert = True
        elif abs(mid_vs_mark_bps) >= abs(mark_vs_oracle_bps):
            stress_type = "BOOK_STRESS"
            is_alert = False
        elif is_crowded_funding:
            stress_type = "CROWDED_FUNDING_BASIS"
            is_alert = abs(mark_vs_oracle_bps) >= cfg.h6_crowded_basis_alert_bps
        else:
            stress_type = "ORACLE_DECOUPLING"
            is_alert = abs(mark_vs_oracle_bps) >= cfg.h6_alert_mark_oracle_bps

        rows.append(
            {
                "ts_ms": ts_ms,
                "hypothesis": "H6_MARK_ORACLE_STRESS",
                "coin": coin,
                "mid_px": mid_px,
                "mark_px": mark_px,
                "oracle_px": oracle_px,
                "mid_vs_mark_bps": round(mid_vs_mark_bps, 2),
                "mark_vs_oracle_bps": round(mark_vs_oracle_bps, 2),
                "max_disloc_bps": round(max_disloc, 2),
                "funding_apr_pct": round(fund_apr, 1),
                "day_vol_usd": round(day_vol, 2),
                "stress_type": stress_type,
                "alert": is_alert,
                "ret_15m_bps": np.nan,
                "ret_1h_bps": np.nan,
                "ret_4h_bps": np.nan,
            }
        )
    rows.sort(key=lambda r: (-int(r["alert"]), -float(r["max_disloc_bps"])))
    return rows[: cfg.h6_top_n]


def summarize_hlp_mm_pnl_regime(
    child_ch_states: list[dict[str, Any]],
    *,
    mark_prices: dict[str, float],
    prev_snapshot: dict[str, Any] | None,
    hlp_cum_pnl_usd: float | None = None,
) -> dict[str, Any]:
    """Track deposit-clean HLP PnL delta vs BTC move to distinguish mechanical cascades from toxic flow.

    Uses ``vaultDetails.portfolio.allTime.pnlHistory`` (``hlp_cum_pnl_usd``) whenever available so
    LP deposits and 4-day lockup withdrawals NEVER trigger false regime flips.
    """
    tot_acct_val = 0.0
    tot_upnl = 0.0
    for ch in child_ch_states:
        tot_acct_val += _f((ch.get("marginSummary") or {}).get("accountValue"))
        for item in ch.get("assetPositions") or []:
            pos = item.get("position") or {}
            tot_upnl += _f(pos.get("unrealizedPnl"))

    btc_mark = _f(mark_prices.get("BTC"))
    prev_mm = (prev_snapshot or {}).get("hlp_mm_pnl") if isinstance(prev_snapshot, dict) else None
    pnl_delta_usd: float | None = None
    pnl_source = "vault_pnlHistory_deposit_clean" if hlp_cum_pnl_usd is not None else "child_equity_fallback"

    if hlp_cum_pnl_usd is not None:
        if isinstance(prev_mm, dict) and prev_mm.get("hlp_cum_pnl_usd") is not None:
            pnl_delta_usd = round(float(hlp_cum_pnl_usd) - _f(prev_mm.get("hlp_cum_pnl_usd")), 2)
    elif isinstance(prev_mm, dict) and _f(prev_mm.get("hlp_child_account_value_usd")) > 0 and tot_acct_val > 0:
        pnl_delta_usd = round(tot_acct_val - _f(prev_mm.get("hlp_child_account_value_usd")), 2)

    btc_ret_bps: float | None = None
    if isinstance(prev_mm, dict) and _f(prev_mm.get("btc_mark_px")) > 0 and btc_mark > 0:
        prev_btc = _f(prev_mm.get("btc_mark_px"))
        btc_ret_bps = round((btc_mark - prev_btc) / prev_btc * 1e4, 2)

    regime = "CALM"
    if pnl_delta_usd is not None:
        if pnl_delta_usd <= -250_000.0:
            regime = "TOXIC_INFORMED_FLOW"
        elif pnl_delta_usd >= 250_000.0 and (btc_ret_bps is not None and abs(btc_ret_bps) >= 50.0):
            regime = "MECHANICAL_CASCADE_CANDIDATE"

    return {
        "hlp_cum_pnl_usd": round(float(hlp_cum_pnl_usd), 2) if hlp_cum_pnl_usd is not None else None,
        "hlp_child_account_value_usd": round(tot_acct_val, 2),
        "hlp_child_unrealized_pnl_usd": round(tot_upnl, 2),
        "hlp_pnl_delta_usd": pnl_delta_usd,
        "pnl_source": pnl_source,
        "btc_mark_px": btc_mark,
        "btc_ret_since_prev_bps": btc_ret_bps,
        "mm_pnl_regime": regime,
    }


def evaluate_alert_cooldown(
    active_alerts: dict[str, float],
    prev_state: dict[str, dict[str, float]],
    *,
    now_ms: int,
    cooldown_sec: int = 4 * 3600,
    escalation_mult: float = 1.5,
) -> tuple[list[str], list[str], dict[str, dict[str, float]]]:
    """Deduplicate recurring 15m alerts via per-key cooldown + severity escalation bypass.

    Returns ``(new_alert_keys, suppressed_alert_keys, updated_state)``.
    An alert fires immediately if unseen, past ``cooldown_sec``, or magnitude grew by
    ``>= escalation_mult`` since the last notification.
    """
    cooldown_ms = max(0, int(cooldown_sec)) * 1000
    updated: dict[str, dict[str, float]] = {}
    # Keep recent state entries (prune > 24h stale entries)
    for k, rec in (prev_state or {}).items():
        if isinstance(rec, dict) and (now_ms - int(rec.get("last_sent_ms", 0))) <= 24 * 3600 * 1000:
            updated[k] = {"last_sent_ms": int(rec.get("last_sent_ms", 0)), "magnitude": float(rec.get("magnitude", 0.0))}

    new_keys: list[str] = []
    suppressed: list[str] = []
    for key, mag in sorted(active_alerts.items()):
        prev = updated.get(key)
        abs_mag = abs(float(mag))
        if prev is None:
            fire = True
        else:
            elapsed = now_ms - int(prev["last_sent_ms"])
            prev_mag = max(abs(float(prev["magnitude"])), 1e-9)
            fire = (elapsed >= cooldown_ms) or (abs_mag >= prev_mag * escalation_mult)
        if fire:
            new_keys.append(key)
            updated[key] = {"last_sent_ms": now_ms, "magnitude": round(abs_mag, 4)}
        else:
            suppressed.append(key)
    return new_keys, suppressed, updated


def grade_forward_outcomes(
    parquet_path: Path,
    mark_prices: dict[str, float],
    *,
    now_ms: int,
) -> int:
    """Back-fill realized +15m / +1h / +4h mark-price returns onto prior logged events."""
    if not parquet_path.is_file():
        return 0
    df = pd.read_parquet(parquet_path)
    if df.empty or "coin" not in df.columns or "mark_px" not in df.columns:
        return 0

    horizons = (
        ("ret_15m_bps", 14 * 60_000, 25 * 60_000),
        ("ret_1h_bps", 55 * 60_000, 75 * 60_000),
        ("ret_4h_bps", 230 * 60_000, 260 * 60_000),
    )
    ts_series = pd.to_numeric(df["ts_ms"], errors="coerce")
    updated = 0
    for col, min_dt, max_dt in horizons:
        if col not in df.columns:
            df[col] = np.nan
        missing_mask = df[col].isna() & (ts_series >= (now_ms - max_dt)) & (ts_series <= (now_ms - min_dt))
        if not missing_mask.any():
            continue
        for idx in df.index[missing_mask]:
            coin = str(df.at[idx, "coin"])
            entry_px = _f(df.at[idx, "mark_px"])
            curr_px = _f(mark_prices.get(coin))
            if entry_px > 0 and curr_px > 0:
                df.at[idx, col] = round((curr_px - entry_px) / entry_px * 1e4, 2)
                updated += 1
    if updated > 0:
        _write_parquet_atomic(parquet_path, df)
    return updated


def _post_json_with_retry(
    client: httpx.Client,
    url: str,
    payload: dict[str, Any],
    *,
    max_attempts: int = 3,
    base_delay_s: float = 0.8,
) -> httpx.Response:
    """POST JSON to Hyperliquid with bounded retry on HTTP 429/5xx or transient transport errors."""
    last_exc: Exception | None = None
    last_resp: httpx.Response | None = None
    for attempt in range(max_attempts):
        try:
            resp = client.post(url, json=payload)
            last_resp = resp
            if resp.status_code not in (429, 500, 502, 503, 504):
                return resp
        except httpx.HTTPError as exc:
            last_exc = exc
        if attempt + 1 < max_attempts:
            time.sleep(base_delay_s * (attempt + 1))
    if last_resp is not None:
        return last_resp
    assert last_exc is not None
    raise last_exc


def fetch_hlp_vault_telemetry(
    client: httpx.Client,
    *,
    vault_address: str = evm.HLP_VAULT_ADDRESS,
    info_url: str = evm.HYPERCORE_INFO_URL,
) -> tuple[list[dict[str, Any]], float, float | None]:
    """Fetch HLP child clearinghouse states, TVL, and deposit-clean ``allTime.pnlHistory`` latest PnL."""
    r = _post_json_with_retry(client, info_url, {"type": "vaultDetails", "vaultAddress": vault_address})
    r.raise_for_status()
    vd = r.json() or {}
    tvl = 0.0
    cum_pnl: float | None = None
    for p_name, p_data in vd.get("portfolio") or []:
        if p_name == "allTime":
            hist = (p_data or {}).get("accountValueHistory") or []
            if hist:
                tvl = _f(hist[-1][1])
            phist = (p_data or {}).get("pnlHistory") or []
            if phist:
                cum_pnl = _f(phist[-1][1])
    rel = vd.get("relationship") or {}
    child_addrs = list((rel.get("data") or {}).get("childAddresses") or [])
    addrs = child_addrs if child_addrs else [vault_address]
    states: list[dict[str, Any]] = []
    for addr in addrs:
        try:
            cr = _post_json_with_retry(client, info_url, {"type": "clearinghouseState", "user": addr})
            if cr.status_code == 200 and isinstance(cr.json(), dict):
                states.append(cr.json())
        except Exception:
            continue
    return states, tvl, cum_pnl


def fetch_hlp_child_clearinghouse_states(
    client: httpx.Client,
    *,
    vault_address: str = evm.HLP_VAULT_ADDRESS,
    info_url: str = evm.HYPERCORE_INFO_URL,
) -> tuple[list[dict[str, Any]], float]:
    """Backward-compatible wrapper returning ``(child_states, tvl_usd)``."""
    states, tvl, _ = fetch_hlp_vault_telemetry(client, vault_address=vault_address, info_url=info_url)
    return states, tvl


# --------------------------------------------------------------------------- Orchestrator


def run_onchain_event_tick(
    *,
    state_dir: Path,
    cfg: OnChainEventConfig = OnChainEventConfig(),
    now_ms: int | None = None,
    liq_snap: liq.LiquidationAndFundingAlphaSnapshot | None = None,
    evm_snap: evm.HyperEVMDefiSnapshot | None = None,
    hlp_child_states: list[dict[str, Any]] | None = None,
    hlp_tvl_usd: float | None = None,
    hlp_cum_pnl_usd: float | None = None,
    mark_prices: dict[str, float] | None = None,
    perps_raw: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run one read-only H2 + H4 + H5 + H6 + MM PnL Inversion scan, grade outcomes, and persist ledgers."""
    ts_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    state_dir.mkdir(parents=True, exist_ok=True)

    prev_snapshot: dict[str, Any] | None = None
    latest_path = state_dir / "latest_snapshot.json"
    if latest_path.is_file():
        try:
            prev_snapshot = json.loads(latest_path.read_text(encoding="utf-8"))
        except Exception:
            prev_snapshot = None

    with httpx.Client(timeout=20.0) as client:
        if mark_prices is None or perps_raw is None:
            fetched_perps, _ = evm.fetch_hypercore_markets(client)
            perps = dict(perps_raw) if perps_raw is not None else fetched_perps
            marks = (
                dict(mark_prices)
                if mark_prices is not None
                else {coin: _f(info.get("mark_px")) for coin, info in fetched_perps.items() if _f(info.get("mark_px")) > 0}
            )
        else:
            perps = dict(perps_raw)
            marks = dict(mark_prices)

        if evm_snap is None:
            evm_snap = evm.run_hyperevm_defi_scan(client)

        if hlp_child_states is None:
            child_states, tvl_val, cum_pnl_val = fetch_hlp_vault_telemetry(client)
        else:
            child_states = hlp_child_states
            tvl_val = float(hlp_tvl_usd if hlp_tvl_usd is not None else 150_000_000.0)
            cum_pnl_val = hlp_cum_pnl_usd

    if liq_snap is None:
        shared_cache = (state_dir.parent / "whales_cache.json") if state_dir.parent.exists() else (state_dir / "whales_cache.json")
        liq_snap = asyncio.run(
            liq.run_liquidation_and_funding_alpha_scan(
                top_whales=cfg.top_whales,
                top_funding_coins=1,
                whale_cache_path=shared_cache,
                whale_cache_ttl_sec=cfg.whale_cache_ttl_sec,
            )
        )

    h2_path = state_dir / "h2_liq_events.parquet"
    h4_path = state_dir / "h4_evm_dislocations.parquet"
    h5_path = state_dir / "h5_hlp_skew.parquet"
    h6_path = state_dir / "h6_mark_oracle_disloc.parquet"

    # 1. Back-fill realized forward outcomes on prior ticks BEFORE appending current tick
    graded_h2 = grade_forward_outcomes(h2_path, marks, now_ms=ts_ms)
    graded_h5 = grade_forward_outcomes(h5_path, marks, now_ms=ts_ms)
    graded_h6 = grade_forward_outcomes(h6_path, marks, now_ms=ts_ms)

    # 2. Extract current tick events + HLP MM PnL Inversion regime
    h2_rows = extract_h2_liq_events(liq_snap.clusters, ts_ms=ts_ms, cfg=cfg)
    h4_rows = extract_h4_evm_dislocations(evm_snap, ts_ms=ts_ms, cfg=cfg)
    h5_rows = extract_h5_hlp_inventory_skew(child_states, marks, hlp_tvl_usd=tvl_val, ts_ms=ts_ms, cfg=cfg)
    h6_rows = extract_h6_book_mark_oracle_dislocations(perps, ts_ms=ts_ms, cfg=cfg)
    hlp_mm_pnl = summarize_hlp_mm_pnl_regime(
        child_states, mark_prices=marks, prev_snapshot=prev_snapshot, hlp_cum_pnl_usd=cum_pnl_val
    )

    h2_df = _append_parquet(h2_path, h2_rows)
    h4_df = _append_parquet(h4_path, h4_rows)
    h5_df = _append_parquet(h5_path, h5_rows)
    h6_df = _append_parquet(h6_path, h6_rows)

    alerts_h2 = [r for r in h2_rows if r["alert"]]
    alerts_h4 = [r for r in h4_rows if r["alert"]]
    alerts_h5 = [r for r in h5_rows if r["alert"]]
    alerts_h6 = [r for r in h6_rows if r["alert"]]
    mm_regime_alert = hlp_mm_pnl["mm_pnl_regime"] != "CALM"

    # 3. Alert deduplication / cooldown (prevents 15m cron from spamming persistent dislocations)
    active_alert_map: dict[str, float] = {}
    for r in alerts_h2:
        active_alert_map[f"H2:{r['coin']}"] = float(r.get("weighted_near_usd", r["total_0_10_usd"]))
    for r in alerts_h4:
        active_alert_map[f"H4:{r['pool']}"] = abs(float(r["peg_adjusted_spread_bps"]))
    l1_max_drift = max((abs(float(r.get("l1read_max_drift_bps", 0.0))) for r in h4_rows), default=0.0)
    if l1_max_drift >= cfg.h4_alert_l1read_drift_bps:
        active_alert_map["H4:L1READ_DRIFT"] = l1_max_drift
    for r in alerts_h5:
        active_alert_map[f"H5:{r['coin']}"] = float(r["hlp_abs_usd"])
    for r in alerts_h6:
        active_alert_map[f"H6:{r['coin']}"] = float(r["max_disloc_bps"])
    if mm_regime_alert:
        active_alert_map[f"MM_PNL:{hlp_mm_pnl['mm_pnl_regime']}"] = abs(float(hlp_mm_pnl.get("hlp_pnl_delta_usd") or 0.0))

    alert_state_path = state_dir / "alert_state.json"
    prev_alert_state: dict[str, dict[str, float]] = {}
    if alert_state_path.is_file():
        try:
            loaded = json.loads(alert_state_path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                prev_alert_state = loaded
        except Exception:
            prev_alert_state = {}

    new_alert_keys, suppressed_alert_keys, updated_alert_state = evaluate_alert_cooldown(
        active_alert_map,
        prev_alert_state,
        now_ms=ts_ms,
        cooldown_sec=cfg.alert_cooldown_sec,
        escalation_mult=cfg.alert_escalation_mult,
    )
    tmp_as = alert_state_path.with_suffix(".tmp.json")
    tmp_as.write_text(json.dumps(updated_alert_state, indent=2), encoding="utf-8")
    tmp_as.replace(alert_state_path)

    summary = {
        "ts_ms": ts_ms,
        "observed_at_utc": datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "hlp_tvl_usd": round(tvl_val, 2),
        "hlp_mm_pnl": hlp_mm_pnl,
        "graded_outcomes": {"h2": graded_h2, "h5": graded_h5, "h6": graded_h6},
        "total_logged": {"h2": int(len(h2_df)), "h4": int(len(h4_df)), "h5": int(len(h5_df)), "h6": int(len(h6_df))},
        "alerts_count": {"h2": len(alerts_h2), "h4": len(alerts_h4), "h5": len(alerts_h5), "h6": len(alerts_h6)},
        "any_alert": bool(active_alert_map),
        "active_alert_keys": sorted(active_alert_map.keys()),
        "new_alert_keys": new_alert_keys,
        "suppressed_alert_keys": suppressed_alert_keys,
        "should_notify": bool(new_alert_keys),
        "h2_events": h2_rows,
        "h4_events": h4_rows,
        "h5_events": h5_rows,
        "h6_events": h6_rows,
    }
    tmp_js = latest_path.with_suffix(".tmp.json")
    tmp_js.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    tmp_js.replace(latest_path)
    return summary


# --------------------------------------------------------------------------- Reporting


def format_onchain_events_markdown(res: dict[str, Any]) -> str:
    mm_pnl = res.get("hlp_mm_pnl") or {}
    h6_cnt = res.get("alerts_count", {}).get("h6", 0)
    h6_tot = res.get("total_logged", {}).get("h6", 0)
    h6_gr = res.get("graded_outcomes", {}).get("h6", 0)
    delta_txt = (
        f"${mm_pnl['hlp_pnl_delta_usd']/1e3:+,.1f}k"
        if mm_pnl.get("hlp_pnl_delta_usd") is not None
        else "первый тик"
    )
    lines = [
        f"# On-Chain Event Alpha Scanner · H2 + H4 + H5 + H6 ({res['observed_at_utc']})",
        "",
        f"- **Статус триггеров:** H2 Ликвидации `{res['alerts_count']['h2']}` · H4 HyperEVM `{res['alerts_count']['h4']}` · H5 HLP Skew `{res['alerts_count']['h5']}` · H6 Mark–Oracle `{h6_cnt}`",
        f"- **MM PnL Inversion (HLP):** режим **`{mm_pnl.get('mm_pnl_regime', 'CALM')}`** · HLP ΔPnL за тик `{delta_txt}` · Child Equity `${mm_pnl.get('hlp_child_account_value_usd', 0.0)/1e6:,.2f}M` (uPnL `${mm_pnl.get('hlp_child_unrealized_pnl_usd', 0.0)/1e3:+,.1f}k`)",
        f"- **Накоплено наблюдений:** H2 `{res['total_logged']['h2']}`, H4 `{res['total_logged']['h4']}`, H5 `{res['total_logged']['h5']}`, H6 `{h6_tot}` · размечено форвард-доходностей на тике: H2 `{res['graded_outcomes']['h2']}`, H5 `{res['graded_outcomes']['h5']}`, H6 `{h6_gr}`",
        "",
        "## H2 — Стенки ликвидаций топ-китов (0–10% от Mark)",
        "",
        "| Монета | Mark | Китов | Кластер 0–5% | Кластер 0–10% | Ближ. стенка | Магнит | Триггер |",
        "|---|---:|---:|---:|---:|---:|---|---|",
    ]
    for r in res["h2_events"]:
        wall = f"{r['nearest_wall_pct']:.2f}%" if pd.notna(r["nearest_wall_pct"]) else "—"
        al = "🚨 ALERT" if r["alert"] else "— "
        lines.append(
            f"| **{r['coin']}** | {r['mark_px']:,.5g} | {r['whales_count']} | "
            f"${r['total_0_5_usd']/1e6:,.2f}M | **${r['total_0_10_usd']/1e6:,.2f}M** | {wall} | {liq.cascade_bias_label(r['cascade_bias'])} | {al} |"
        )
    if not res["h2_events"]:
        lines.append("| — | — | 0 | $0M | $0M | — | Нет кластеров >= $0.5M | — |")

    lines += [
        "",
        "## H4 — Разрывы HyperEVM ↔ HyperCore (Peg-Adjusted V3)",
        "",
        "| DEX | Пул | Peg-Adj | Net Arb | Slip $25k | Глубина V3 | L1Read Дрейф | Триггер |",
        "|---|---|---:|---:|---:|---:|---:|---|",
    ]
    for r in res["h4_events"][:6]:
        al = "🚨 ALERT" if r["alert"] else "—"
        lines.append(
            f"| {r['dex']} | **{r['pool']}** | {r['peg_adjusted_spread_bps']:+.2f} б.п. | "
            f"**{r['net_arb_bps']:+.2f} б.п.** | {r['slippage_25k_bps']:.1f} б.п. | "
            f"${r['virtual_depth_usd']/1e6:,.2f}M | {r['l1read_max_drift_bps']:.2f} б.п. | {al} |"
        )

    lines += [
        "",
        f"## H5 — Перекос инвентаря HLP (TVL `${res['hlp_tvl_usd']/1e6:,.2f}M`, дочерние волты)",
        "",
        "| Монета | Сторона HLP | Чистый номинал HLP | Доля TVL HLP | Волтов | Триггер |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in res["h5_events"]:
        al = "🚨 ALERT" if r["alert"] else "—"
        net_v = float(r["hlp_net_usd"])
        net_txt = f"${net_v/1e6:+,.2f}M" if abs(net_v) >= 100_000.0 else f"${net_v/1e3:+,.1f}k"
        lines.append(
            f"| **{r['coin']}** | `{r['hlp_side']}` | **{net_txt}** | "
            f"{r['hlp_tvl_share_pct']:.3f}% | {r['vaults_holding']} | {al} |"
        )

    h6_events = res.get("h6_events") or []
    lines += [
        "",
        "## H6 — Стресс-дислокации BookMid ↔ Mark ↔ Oracle (алерт ≥ 50 б.п.)",
        "",
        "| Монета | BookMid vs Mark | Mark vs Oracle | Тип стресса | Объём 24ч | Триггер |",
        "|---|---:|---:|---|---:|---|",
    ]
    for r in h6_events:
        al = "🚨 ALERT" if r["alert"] else "—"
        lines.append(
            f"| **{r['coin']}** | {r['mid_vs_mark_bps']:+.2f} б.п. | {r['mark_vs_oracle_bps']:+.2f} б.п. | "
            f"`{r['stress_type']}` | ${r['day_vol_usd']/1e6:,.1f}M | {al} |"
        )
    if not h6_events:
        lines.append("| — | — | — | Спокойный стакан (< 10 б.п.) | — | — |")
    return "\n".join(lines) + "\n"


def format_onchain_events_telegram_rich_html(res: dict[str, Any]) -> str:
    rows = [
        '<tr><th align="left">Гипотеза</th><th align="left">Сигнал / Объект</th><th align="right">Метрика</th></tr>'
    ]
    for r in res["h2_events"][:3]:
        if r["alert"] or len(rows) <= 2:
            wall = f"{r['nearest_wall_pct']:.1f}%" if pd.notna(r["nearest_wall_pct"]) else "—"
            rows.append(
                "<tr>"
                '<td align="left"><b>H2 Ликв.</b></td>'
                f'<td align="left">{html.escape(str(r["coin"]))} ({html.escape(liq.cascade_bias_label(r["cascade_bias"]))})</td>'
                f'<td align="right"><b>${r["total_0_10_usd"]/1e6:,.2f}M</b> · {wall}</td>'
                "</tr>"
            )
    for r in res["h4_events"][:2]:
        if r["alert"] or r == res["h4_events"][0]:
            rows.append(
                "<tr>"
                '<td align="left"><b>H4 EVM</b></td>'
                f'<td align="left">{html.escape(str(r["pool"]))}</td>'
                f'<td align="right">Peg {r["peg_adjusted_spread_bps"]:+.1f} / Net <b>{r["net_arb_bps"]:+.1f} б.п.</b></td>'
                "</tr>"
            )
    for r in res["h5_events"][:2]:
        net_v = float(r["hlp_net_usd"])
        net_txt = f"${net_v/1e6:+,.2f}M" if abs(net_v) >= 100_000.0 else f"${net_v/1e3:+,.1f}k"
        rows.append(
            "<tr>"
            '<td align="left"><b>H5 HLP</b></td>'
            f'<td align="left">{html.escape(str(r["coin"]))} ({html.escape(str(r["hlp_side"]))})</td>'
            f'<td align="right"><b>{net_txt}</b> ({r["hlp_tvl_share_pct"]:.3f}%)</td>'
            "</tr>"
        )
    for r in (res.get("h6_events") or [])[:2]:
        rows.append(
            "<tr>"
            '<td align="left"><b>H6 Стресс</b></td>'
            f'<td align="left">{html.escape(str(r["coin"]))} ({html.escape(str(r["stress_type"]))})</td>'
            f'<td align="right">Mid–Mark {r["mid_vs_mark_bps"]:+.1f} · Mark–Orc <b>{r["mark_vs_oracle_bps"]:+.1f} б.п.</b></td>'
            "</tr>"
        )
    mm_pnl = res.get("hlp_mm_pnl") or {}
    h6_cnt = res.get("alerts_count", {}).get("h6", 0)
    h6_tot = res.get("total_logged", {}).get("h6", 0)
    return (
        f"<h1>🛰️ On-Chain Event Alpha (H2+H4+H5+H6) · {html.escape(str(res['observed_at_utc']))}</h1>"
        f"<p><b>Триггеры:</b> H2={res['alerts_count']['h2']} · H4={res['alerts_count']['h4']} · H5={res['alerts_count']['h5']} · H6={h6_cnt} · "
        f"MM PnL: <b>{html.escape(str(mm_pnl.get('mm_pnl_regime', 'CALM')))}</b><br>"
        f"Накоплено записей: H2={res['total_logged']['h2']}, H4={res['total_logged']['h4']}, H5={res['total_logged']['h5']}, H6={h6_tot}</p>"
        f'<table bordered striped>{"".join(rows)}</table>'
        "<footer>100% read-only логгер событий L1 + HyperEVM + HLP + BookMid/Mark/Oracle с авторазметкой +15m/+1h/+4h</footer>"
    )
