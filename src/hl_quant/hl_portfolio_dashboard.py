"""Self-contained interactive HTML Desk Dashboard & Interview Portfolio Showcase.

Aggregates live Hyperliquid Testnet, Paper L2, Avellaneda–Stoikov MM v2, Empirical
Kappa Calibration, 5-DEX Cross-DEX v2, HL × Lighter Dual-Leg Paper, HyperEVM Chain 999
(L1Read + V3 depth + HLP), Whale Liquidation Heatmap, and 17-day `:00` Funding OOS Study
into one zero-dependency dark-mode interactive HTML page with inline SVG visualizations.
"""

from __future__ import annotations

from datetime import datetime, timezone
import html
import json
import math
from pathlib import Path
from typing import Any

import pandas as pd

from hl_quant import hl_tear_sheet as ts
from hl_quant.hl_liquidation_and_funding_alpha import cascade_bias_label


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


_PARQUET_CACHE: dict[str, tuple[int, int, int | None, list[dict[str, Any]]]] = {}


def _read_parquet_records(path: Path, limit: int | None = None, *, head: bool = False) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        st = path.stat()
        cache_key = f"{path.resolve()}::{head}"
        cached = _PARQUET_CACHE.get(cache_key)
        if cached is not None and cached[0] == st.st_mtime_ns and cached[1] == st.st_size and cached[2] == limit:
            return list(cached[3])
        df = pd.read_parquet(path)
        if limit is not None:
            df = df.head(limit) if head else df.tail(limit)
        records = df.to_dict(orient="records")
        if len(_PARQUET_CACHE) > 64:
            _PARQUET_CACHE.clear()
        _PARQUET_CACHE[cache_key] = (st.st_mtime_ns, st.st_size, limit, records)
        return list(records)
    except Exception:
        return []


def collect_dashboard_payload(data_root: Path) -> dict[str, Any]:
    """Collect all live JSON/Parquet outputs under ``data_root`` for dashboard rendering."""
    base = ts.collect_tear_sheet_data(data_root)
    cross_paper_state = _read_json(data_root / "hl_cross_dex_paper" / "state.json")
    paper_ticks = _read_parquet_records(data_root / "hl_metamodel" / "ticks.parquet", limit=720)
    mm_quotes = _read_parquet_records(data_root / "hl_mm_testnet" / "quotes.parquet", limit=200)
    # Prefer the expanded 52-day (60k-event) :00 funding study when present, else fallback to 17-day
    study_dir = (
        data_root / "hl_funding_settlement_study_52d"
        if (data_root / "hl_funding_settlement_study_52d" / "stats.parquet").is_file()
        else data_root / "hl_funding_settlement_study"
    )
    settlement_stats = _read_parquet_records(study_dir / "stats.parquet")
    settlement_meta = _read_json(study_dir / "meta.json")
    settlement_last_attempt = _read_json(study_dir / "last_attempt.json")
    onchain_events = _read_json(data_root / "hl_onchain_events" / "latest_snapshot.json")
    cross_dex_all = _read_parquet_records(data_root / "hl_cross_dex_v2" / "opportunities.parquet", limit=15, head=True)
    hip3_snap = _read_json(data_root / "hl_hip3_arb" / "snapshot.json")
    hip3_paper = _read_json(data_root / "hl_hip3_paper" / "state.json")
    hip3_paper_ticks = _read_parquet_records(data_root / "hl_hip3_paper" / "ticks.parquet", limit=720)
    spot_perp_paper = _read_json(data_root / "hl_spot_perp_paper" / "state.json")
    spot_perp_paper_ticks = _read_parquet_records(data_root / "hl_spot_perp_paper" / "ticks.parquet", limit=720)
    spot_perp_scan = _read_json(data_root / "hl_spot_perp_paper" / "snapshot.json") or _read_json(
        data_root / "hl_spot_perp_arb" / "snapshot.json"
    )
    autopilot = _read_json(data_root / "hl_autopilot" / "state.json")
    autopilot_ticks = _read_parquet_records(data_root / "hl_autopilot" / "ticks.parquet", limit=720)
    boros_snap = _read_json(data_root / "hl_boros" / "snapshot.json")
    yield_snap = _read_json(data_root / "hl_hyperevm_yield_stack" / "snapshot.json")
    frontier_hist = _read_json(data_root / "hl_frontier_history" / "latest_summary.json")

    return {
        **base,
        "generated_at_utc": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "cross_dex_paper": cross_paper_state,
        "paper_ticks": paper_ticks,
        "mm_quotes": mm_quotes,
        "settlement_stats": settlement_stats,
        "settlement_meta": settlement_meta,
        "settlement_last_attempt": settlement_last_attempt,
        "onchain_events": onchain_events,
        "cross_dex_all": cross_dex_all,
        "hip3": hip3_snap,
        "hip3_paper": hip3_paper,
        "hip3_paper_ticks": hip3_paper_ticks,
        "spot_perp_paper": spot_perp_paper,
        "spot_perp_paper_ticks": spot_perp_paper_ticks,
        "spot_perp_scan": spot_perp_scan,
        "autopilot": autopilot,
        "autopilot_ticks": autopilot_ticks,
        "boros": boros_snap,
        "yield_stack": yield_snap,
        "frontier_history": frontier_hist,
    }


def _svg_sparkline(values: list[float], *, width: int = 340, height: int = 86, stroke: str = "#10b981") -> str:
    if len(values) < 2:
        return '<div class="muted">Недостаточно точек для графика</div>'
    lo, hi = min(values), max(values)
    span = max(hi - lo, 1e-9)
    pad = 8
    pts = []
    for i, v in enumerate(values):
        x = pad + (width - 2 * pad) * i / (len(values) - 1)
        y = height - pad - (height - 2 * pad) * (v - lo) / span
        pts.append(f"{x:.1f},{y:.1f}")
    poly = " ".join(pts)
    area = f"{pad:.1f},{height - pad:.1f} {poly} {width - pad:.1f},{height - pad:.1f}"
    return (
        f'<svg viewBox="0 0 {width} {height}" class="spark" role="img" aria-label="Equity curve">'
        f'<polygon points="{area}" fill="{stroke}" fill-opacity="0.14"/>'
        f'<polyline points="{poly}" fill="none" stroke="{stroke}" stroke-width="2.2" stroke-linecap="round"/>'
        "</svg>"
    )


def _svg_calibration_curve(cal: dict[str, Any], *, width: int = 380, height: int = 160) -> str:
    curve = cal.get("depth_curve") or []
    if not curve:
        return '<div class="muted">Нет данных калибровки</div>'
    xs = [float(r["delta_bps"]) for r in curve]
    ys_emp = [float(r["lambda_per_sec"]) for r in curve]
    ys_fit = [float(r["fitted_lambda"]) for r in curve]
    max_x = max(xs) or 16.0
    max_y = max(max(ys_emp), max(ys_fit), 1e-4)
    pl, pr, pt_p, pb = 38, 14, 14, 24

    def sx(x: float) -> float:
        return pl + (width - pl - pr) * (x / max_x)

    def sy(y: float) -> float:
        return height - pb - (height - pt_p - pb) * (y / max_y)

    fit_pts = " ".join(f"{sx(x):.1f},{sy(y):.1f}" for x, y in zip(xs, ys_fit, strict=False))
    dots = "".join(
        f'<circle cx="{sx(x):.1f}" cy="{sy(y):.1f}" r="3.6" fill="#38bdf8" stroke="#0b1220" stroke-width="1"/>'
        for x, y in zip(xs, ys_emp, strict=False)
    )
    return (
        f'<svg viewBox="0 0 {width} {height}" class="chart-svg">'
        f'<line x1="{pl}" y1="{height - pb}" x2="{width - pr}" y2="{height - pb}" stroke="#334155" stroke-width="1"/>'
        f'<line x1="{pl}" y1="{pt_p}" x2="{pl}" y2="{height - pb}" stroke="#334155" stroke-width="1"/>'
        f'<polyline points="{fit_pts}" fill="none" stroke="#10b981" stroke-width="2.2"/>'
        f"{dots}"
        f'<text x="{pl}" y="{pt_p + 8}" fill="#94a3b8" font-size="10">λ(δ) 1/s</text>'
        f'<text x="{width - 62}" y="{height - 6}" fill="#94a3b8" font-size="10">δ (б.п.)</text>'
        "</svg>"
    )


def _nf(v: Any, default: float = 0.0) -> float:
    if v is None:
        return default
    try:
        out = float(v)
        return out if math.isfinite(out) else default
    except (TypeError, ValueError):
        return default


def _ni(v: Any, default: int = 0) -> int:
    if v is None:
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _e(val: object) -> str:
    return html.escape(str(val))


def render_portfolio_dashboard_html(d: dict[str, Any]) -> str:
    """Render complete self-contained interactive dark-mode HTML portfolio dashboard."""
    attr = d.get("attribution") or {}
    paper = d.get("paper") or {}
    evm = d.get("hyperevm") or {}
    liq = d.get("liq_funding") or {}
    lat = d.get("latency_bench") or {}
    cd_paper = d.get("cross_dex_paper") or {}
    mm_cal = d.get("mm_calibration") or {}
    mm_raw = d.get("mm_session") or {}
    mm_single = mm_raw.get("summary")
    mm_sums: dict[str, Any] = mm_raw.get("summaries") or (
        {mm_single["coin"]: mm_single} if isinstance(mm_single, dict) and "coin" in mm_single else {}
    )

    # Top KPI computations
    tn_eq = float(attr.get("equity_usd") or 0.0)
    tn_fill = attr.get("real_alo_fill_rate_pct")
    tn_fill_txt = f"{tn_fill:.1f}%" if tn_fill is not None else "—"
    tn_liq_warns = len(attr.get("liq_warnings") or [])
    tn_adl_warns = len(attr.get("adl_warnings") or [])
    tn_api_health = str(attr.get("api_quota_health") or "OK")
    tn_api_ratio = attr.get("api_reqs_per_usd_vol")
    tn_api_txt = f"{tn_api_health} ({float(tn_api_ratio):.3f} req/$)" if tn_api_ratio is not None else tn_api_health

    mm_vol = sum(_nf(s.get("volume_usd"), 0.0) for s in mm_sums.values())
    mm_pnl = sum(_nf(s.get("net_pnl_usd"), 0.0) for s in mm_sums.values())

    cd_positions = list((cd_paper.get("positions") or {}).values())
    cd_avg_apr = (
        sum(_nf(p.get("live_spread_apr_pct"), 0.0) for p in cd_positions) / len(cd_positions)
        if cd_positions
        else 0.0
    )

    evm_chain = evm.get("chain") or {}
    hlp_vault = (evm.get("vaults") or [{}])[0]
    s_meta = d.get("settlement_meta") or {}

    # Tab 1: Testnet Per-Coin Attribution rows + Paper Sparkline
    per_coin_rows = ""
    for c in attr.get("per_coin") or []:
        sz = _nf(c.get("position_sz"), 0.0)
        side_badge = '<span class="pill green">Лонг</span>' if sz > 0 else ('<span class="pill red">Шорт</span>' if sz < 0 else '<span class="pill gray">Закрыта</span>')
        pnl = _nf(c.get("net_pnl_usd"), 0.0)
        pnl_cls = "pos" if pnl >= 0 else "neg"
        mk = f"{float(c['vw_markout_bps']):+.1f} б.п." if c.get("vw_markout_bps") is not None else "—"
        lq = f"{float(c['liq_distance_pct']):.1f}%" if c.get("liq_distance_pct") is not None else "—"
        adl = f"{float(c['adl_rank_score']):.2f}" if c.get("adl_rank_score") is not None else "—"
        per_coin_rows += (
            "<tr>"
            f"<td><b>{_e(c.get('coin'))}</b></td>"
            f"<td>{side_badge} ${_nf(c.get('position_usd'), 0):,.1f}</td>"
            f"<td><code>{_e(c.get('leverage_mode'))}</code></td>"
            f"<td class='num'>{_ni(c.get('fills_count'), 0)}</td>"
            f"<td class='num'>{_nf(c.get('maker_share_pct'), 0):.0f}%</td>"
            f"<td class='num {pnl_cls}'>{pnl:+.2f}$</td>"
            f"<td class='num'>{_e(mk)}</td>"
            f"<td class='num'>{_e(lq)}</td>"
            f"<td class='num'>{_e(adl)}</td>"
            "</tr>"
        )
    if not per_coin_rows:
        per_coin_rows = '<tr><td colspan="9" class="muted">Нет данных атрибуции Testnet</td></tr>'

    paper_eq_vals = [
        float(t["equity_usd"])
        for t in (d.get("paper_ticks") or [])
        if isinstance(t, dict) and t.get("equity_usd") is not None and math.isfinite(float(t["equity_usd"]))
    ]
    paper_spark = _svg_sparkline(paper_eq_vals, stroke="#38bdf8")

    # Tab 2: MM v2 summaries + calibration SVGs
    mm_rows = ""
    for c_name, s in mm_sums.items():
        mk5 = f"{float(s['vw_markout_5s_bps']):+.2f} б.п." if s.get("vw_markout_5s_bps") is not None else "—"
        pnl_v = _nf(s.get("net_pnl_usd"), 0.0)
        mm_rows += (
            "<tr>"
            f"<td><b>{_e(c_name)}</b></td>"
            f"<td class='num'>{_ni(s.get('ticks_observed'), 0)} ({_ni(s.get('bbo_ticks_observed'), 0)} bbo)</td>"
            f"<td class='num'>{_ni(s.get('quotes_modified'), 0)} / {_ni(s.get('lead_pulls'), 0)}</td>"
            f"<td class='num'>{_nf(s.get('avg_half_spread_bps'), 0):.2f} б.п.</td>"
            f"<td class='num'>{_ni(s.get('fills_count'), 0)} ({_ni(s.get('buy_fills'), 0)}B/{_ni(s.get('sell_fills'), 0)}S)</td>"
            f"<td class='num'>${_nf(s.get('volume_usd'), 0):,.2f} · {_nf(s.get('maker_share_pct'), 0):.0f}%</td>"
            f"<td class='num {'pos' if pnl_v >= 0 else 'neg'}'>{pnl_v:+.4f}$</td>"
            f"<td class='num pos'>{_e(mk5)}</td>"
            "</tr>"
        )
    if not mm_rows:
        mm_rows = '<tr><td colspan="8" class="muted">Нет сохранённых сессий MM</td></tr>'

    cal_cards = ""
    for c_name, cal in mm_cal.items():
        if not isinstance(cal, dict):
            continue
        cal_cards += (
            '<div class="card">'
            f'<h4>Калибровка λ(δ)=Ae<sup>−κδ</sup> · {_e(c_name)}</h4>'
            f'<div class="meta-line">κ = <b>{_nf(cal.get("kappa_per_bp"), 0):.3f} 1/б.п.</b> · '
            f'A = {_nf(cal.get("arrival_a_per_sec"), 0):.3f} 1/с · '
            f'R² = <b>{_nf(cal.get("r2_fit"), 0):.2f}</b> · '
            f'σ<sub>30s</sub> = {_nf(cal.get("empirical_sigma_tau_bps"), 0):.1f} б.п.</div>'
            f'{_svg_calibration_curve(cal)}'
            "</div>"
        )

    # Tab 3: HL x Lighter x Extended Paper + 5-DEX v2 Opportunities
    cdp_rows = ""
    for p in sorted(cd_positions, key=lambda x: -_nf(x.get("live_spread_apr_pct"), 0.0)):
        net_p = _nf(p.get("net_pnl_usd"), 0.0)
        venue = str(p.get("other_dex") or "lighter")
        mode_lbl = "H1-Combo" if _nf(p.get("entry_exec_basis_bps"), 0.0) >= 8.0 else "Carry"
        pill_cls = "green" if mode_lbl == "H1-Combo" else "gray"
        cdp_rows += (
            "<tr>"
            f"<td><b>{_e(p.get('coin'))}</b></td>"
            f"<td><code>{_e(venue)}</code> <span class='pill {pill_cls}'>{mode_lbl}</span></td>"
            f"<td>{_e(p.get('direction'))}</td>"
            f"<td class='num'>${_nf(p.get('leg_notional_usd'), 0):,.0f}</td>"
            f"<td class='num pos'><b>{_nf(p.get('live_spread_apr_pct'), 0):+.1f}%</b></td>"
            f"<td class='num'>{_nf(p.get('entry_l2_slip_bps'), 0):.1f} б.п.</td>"
            f"<td class='num'>{_nf(p.get('entry_breakeven_hours'), 0):.0f}ч</td>"
            f"<td class='num'>{_nf(p.get('hours_held'), 0):.1f}ч</td>"
            f"<td class='num {'pos' if net_p >= 0 else 'neg'}'>{net_p:+.2f}$</td>"
            "</tr>"
        )
    if not cdp_rows:
        cdp_rows = '<tr><td colspan="9" class="muted">Нет открытых пар HL × Lighter × Extended</td></tr>'

    cd5_rows = ""
    for r in d.get("cross_dex_all") or []:
        ok50 = '<span class="pill green">$50k OK</span>' if r.get("depth_50k_ok") else '<span class="pill gray">Тонкий</span>'
        s10 = f"{float(r['l2_slip_10k_bps']):.1f}" if pd.notna(r.get("l2_slip_10k_bps")) else "—"
        s50 = f"{float(r['l2_slip_50k_bps']):.1f}" if pd.notna(r.get("l2_slip_50k_bps")) else "—"
        eb = f"{float(r['exec_basis_10k_bps']):+.1f} б.п." if pd.notna(r.get("exec_basis_10k_bps")) else "—"
        cd5_rows += (
            "<tr>"
            f"<td><b>{_e(r.get('coin'))}</b></td>"
            f"<td>{_e(r.get('direction'))}</td>"
            f"<td class='num pos'><b>{_nf(r.get('spread_apr_pct'), 0):.1f}%</b></td>"
            f"<td class='num'>{_e(eb)}</td>"
            f"<td class='num'>{_e(s10)} / {_e(s50)} б.п.</td>"
            f"<td>{ok50}</td>"
            f"<td class='num'>${_nf(r.get('hl_vol_m'), 0):,.1f}M / ${_nf(r.get('other_vol_m'), 0):,.1f}M</td>"
            "</tr>"
        )

    # Tab 4: HyperEVM L1Read, V3 Pools, HLP Vaults
    l1_rows = ""
    for o in evm.get("l1_read_oracles") or []:
        l1_rows += (
            "<tr>"
            f"<td><b>{_e(o.get('coin'))}</b></td>"
            f"<td class='num'>{_ni(o.get('asset_index'), 0)}</td>"
            f"<td class='num'>{_nf(o.get('l1read_mark_px'), 0):,.5g}</td>"
            f"<td class='num'>{_nf(o.get('l1read_oracle_px'), 0):,.5g}</td>"
            f"<td class='num'>{_nf(o.get('rest_mark_px'), 0):,.5g}</td>"
            f"<td class='num pos'>{_nf(o.get('drift_bps'), 0):+.2f} б.п.</td>"
            "</tr>"
        )
    evm_rows = ""
    for a in evm.get("evm_arb") or []:
        badge = '<span class="pill green">ARB</span>' if a.get("executable") else '<span class="pill gray">No-Arb</span>'
        evm_rows += (
            "<tr>"
            f"<td>{_e(a.get('dex'))}</td>"
            f"<td><b>{_e(a.get('label'))}</b></td>"
            f"<td class='num'>{_nf(a.get('gross_spread_bps'), 0):+.1f} б.п.</td>"
            f"<td class='num'>{_nf(a.get('peg_adjusted_spread_bps'), 0):+.1f} б.п.</td>"
            f"<td class='num'>{badge} {_nf(a.get('net_arb_bps'), 0):+.1f} б.п.</td>"
            f"<td class='num'>${_nf(a.get('virtual_depth_usd'), 0)/1e6:,.2f}M</td>"
            f"<td class='num'>{_nf(a.get('slippage_25k_bps'), 0):.1f} б.п.</td>"
            "</tr>"
        )
    vault_rows = ""
    for v in evm.get("vaults") or []:
        apr = _nf(v.get("apr_pct"), 0.0)
        vault_rows += (
            "<tr>"
            f"<td><b>{_e(v.get('name'))}</b></td>"
            f"<td class='num'>${_nf(v.get('tvl_usd'), 0)/1e6:,.2f}M</td>"
            f"<td class='num {'pos' if apr >= 0 else 'neg'}'><b>{apr:+.2f}%</b></td>"
            f"<td class='num'>{_ni(v.get('open_positions_count'), 0)}</td>"
            f"<td class='num'>${_nf(v.get('total_notional_usd'), 0)/1e6:,.2f}M</td>"
            f"<td class='num'>{_nf(v.get('margin_utilization_pct'), 0):.1f}%</td>"
            "</tr>"
        )

    # Tab 5: Whale Liquidations + H6 BookMid/Mark/Oracle Stress + HLP MM PnL Inversion + :00 OOS
    oc = d.get("onchain_events") or {}
    mm_pnl_box = oc.get("hlp_mm_pnl") or {}
    mm_regime = str(mm_pnl_box.get("mm_pnl_regime") or "CALM")
    mm_regime_pill = (
        '<span class="pill green">MECHANICAL CASCADE</span>'
        if "MECHANICAL" in mm_regime
        else ('<span class="pill red">TOXIC FLOW</span>' if "TOXIC" in mm_regime else '<span class="pill gray">CALM</span>')
    )
    h6_rows = ""
    for r in (oc.get("h6_events") or [])[:8]:
        al_pill = '<span class="pill red">ALERT</span>' if r.get("alert") else '<span class="pill gray">Watch</span>'
        h6_rows += (
            "<tr>"
            f"<td><b>{_e(r.get('coin'))}</b></td>"
            f"<td class='num'>{_nf(r.get('mid_vs_mark_bps'), 0):+.2f} б.п.</td>"
            f"<td class='num'><b>{_nf(r.get('mark_vs_oracle_bps'), 0):+.2f} б.п.</b></td>"
            f"<td><code>{_e(r.get('stress_type'))}</code></td>"
            f"<td class='num'>${_nf(r.get('day_vol_usd'), 0)/1e6:,.1f}M</td>"
            f"<td>{al_pill}</td>"
            "</tr>"
        )
    if not h6_rows:
        h6_rows = '<tr><td colspan="6" class="muted">Нет стресс-дислокаций BookMid/Mark/Oracle (&ge; 10 б.п.)</td></tr>'

    liq_rows = ""
    h2_live = oc.get("h2_events") or []
    if h2_live:
        for r in h2_live[:10]:
            wall = f"{float(r['nearest_wall_pct']):.2f}%" if r.get("nearest_wall_pct") is not None and pd.notna(r.get("nearest_wall_pct")) else "—"
            liq_rows += (
                "<tr>"
                f"<td><b>{_e(r.get('coin'))}</b></td>"
                f"<td class='num'>{_nf(r.get('mark_px'), 0):,.5g}</td>"
                f"<td class='num'>{_ni(r.get('whales_count'), 0)}</td>"
                f"<td class='num'><b>${_nf(r.get('total_0_10_usd'), 0)/1e6:,.2f}M</b></td>"
                f"<td class='num'>{_e(wall)}</td>"
                f"<td>{_e(cascade_bias_label(r.get('cascade_bias')))}</td>"
                "</tr>"
            )
    else:
        for c in liq.get("clusters") or []:
            liq_rows += (
                "<tr>"
                f"<td><b>{_e(c.get('coin'))}</b></td>"
                f"<td class='num'>{_nf(c.get('mark_px'), 0):,.5g}</td>"
                f"<td class='num'>{_ni(c.get('whales_count'), 0)}</td>"
                f"<td class='num'>${_nf(c.get('total_0_10_usd'), 0)/1e6:,.2f}M</td>"
                f"<td class='num'>L −{_e(c.get('nearest_long_dist_pct', '—'))}% / S +{_e(c.get('nearest_short_dist_pct', '—'))}%</td>"
                f"<td>{_e(cascade_bias_label(c.get('cascade_bias')))}</td>"
                "</tr>"
            )
    if not liq_rows:
        liq_rows = '<tr><td colspan="6" class="muted">Нет кластеров в 0–10%</td></tr>'

    s_rows = ""
    st_list = d.get("settlement_stats") or []
    if st_list:
        sdf = pd.DataFrame(st_list)
        dev_m = sdf[sdf["split"] == "dev"].set_index(["bucket", "strategy"])
        dev_m = dev_m[~dev_m.index.duplicated(keep="last")]
        hld_m = sdf[sdf["split"] == "holdout"].set_index(["bucket", "strategy"])
        hld_m = hld_m[~hld_m.index.duplicated(keep="last")]
        for key in dev_m.index:
            if key not in hld_m.index:
                continue
            b_n, st_n = key
            dv, hv = dev_m.loc[key], hld_m.loc[key]
            net_m = float(hv["net_maker_bps"])
            t_v = float(hv["t_stat"])
            if float(dv["net_maker_bps"]) > 0 and net_m > 0 and t_v >= 2.0:
                v_pill = '<span class="pill green">GO</span>'
            elif float(dv["mean_gross_bps"]) > 1.0 and float(hv["mean_gross_bps"]) > 1.0 and t_v >= 2.0:
                v_pill = '<span class="pill amber">MM Overlay</span>'
            else:
                v_pill = '<span class="pill red">NO-GO</span>'
            s_rows += (
                "<tr>"
                f"<td><code>{_e(b_n)}</code></td>"
                f"<td><code>{_e(st_n)}</code></td>"
                f"<td class='num'>{int(dv['events'])} · {float(dv['mean_gross_bps']):+.2f} ({float(dv['t_stat']):+.1f})</td>"
                f"<td class='num'>{int(hv['events'])} · {float(hv['mean_gross_bps']):+.2f} ({t_v:+.1f})</td>"
                f"<td class='num {'pos' if net_m >= 0 else 'neg'}'><b>{net_m:+.2f} б.п.</b></td>"
                f"<td>{v_pill}</td>"
                "</tr>"
            )

    # Tab 6: HIP-3 Builder-Perps, Pendle Boros Fixed Funding & HyperEVM Composable Yield Stack
    hip3 = d.get("hip3") or {}
    boros = d.get("boros") or {}
    ys = d.get("yield_stack") or {}
    ys_inp = ys.get("inputs") or {}

    hip3_rows = ""
    for o in (hip3.get("cross_builder_opps") or [])[:10]:
        s5 = f"{float(o['l2_slip_5k_bps']):.1f}" if o.get("l2_slip_5k_bps") is not None else "—"
        s25 = f"{float(o['l2_slip_25k_bps']):.1f}" if o.get("l2_slip_25k_bps") is not None else "—"
        eb5 = f"{float(o['exec_basis_5k_bps']):+.1f} б.п." if o.get("exec_basis_5k_bps") is not None else "—"
        be_m = f"{float(o['payback_hours_5k']):.0f}ч" if o.get("payback_hours_5k") is not None else "—"
        be_t = f"{float(o['payback_hours_5k_taker']):.0f}ч" if o.get("payback_hours_5k_taker") is not None else "—"
        gm = '<span class="pill green">⚡ GM</span>' if o.get("both_growth_mode") else '<span class="pill gray">Std</span>'
        odiv = float(o.get("oracle_divergence_bps", 0.0) or 0.0)
        odiv_html = (
            f'<span class="pill amber">⚠️ {odiv:.1f} б.п.</span>'
            if o.get("oracle_risk_warning")
            else f"{odiv:.1f} б.п."
        )
        iso = '<span class="pill amber">Iso</span>' if o.get("any_only_isolated") else '<span class="pill green">Cross</span>'
        hip3_rows += (
            "<tr>"
            f"<td><b>{_e(o.get('base_symbol'))}</b></td>"
            f"<td><code>S {_e(o.get('short_symbol'))}</code> / <code>L {_e(o.get('long_symbol'))}</code></td>"
            f"<td class='num pos'><b>+{_nf(o.get('spread_apr_pct'), 0):.1f}%</b></td>"
            f"<td>{gm}</td>"
            f"<td class='num'>{odiv_html}</td>"
            f"<td class='num'>{_e(eb5)}</td>"
            f"<td class='num'>{_e(s5)} / {_e(s25)} б.п.</td>"
            f"<td class='num'>{_e(be_m)} / {_e(be_t)}</td>"
            f"<td>{iso}</td>"
            "</tr>"
        )
    if not hip3_rows:
        hip3_rows = '<tr><td colspan="9" class="muted">Нет сохранённого среза HIP-3</td></tr>'

    boros_rows = ""
    for m in (boros.get("markets") or [])[:10]:
        spr = ' <span class="pill amber">SPREAD</span>' if m.get("is_spread_market") else ""
        boros_rows += (
            "<tr>"
            f"<td><code>{_e(m.get('market_id'))}</code></td>"
            f"<td><b>{_e(m.get('name'))}</b>{spr}</td>"
            f"<td class='num'>{float(m.get('days_to_maturity') or 0):.1f}д</td>"
            f"<td class='num pos'><b>{float(m.get('best_bid_apr_pct') or 0):+.2f}%</b> / {float(m.get('best_ask_apr_pct') or 0):+.2f}%</td>"
            f"<td class='num'>{float(m.get('floating_apr_pct') or 0):+.2f}%</td>"
            f"<td class='num'>{float(m.get('fixed_minus_float_bid_pct') or 0):+.2f}%</td>"
            f"<td>{_e(m.get('recommended_action'))}</td>"
            "</tr>"
        )
    if not boros_rows:
        boros_rows = '<tr><td colspan="7" class="muted">Нет сохранённого среза Pendle Boros</td></tr>'

    ys_rows = ""
    for s in ys.get("strategies") or []:
        rt = str(s.get("rate_type", ""))
        rt_pill = '<span class="pill green">Fixed</span>' if "Fixed" in rt else ('<span class="pill gray">Bench</span>' if rt == "Benchmark" else '<span class="pill amber">Float</span>')
        ys_rows += (
            "<tr>"
            f"<td><b>{_e(s.get('name'))}</b></td>"
            f"<td>{rt_pill}</td>"
            f"<td class='num'>{_nf(s.get('gross_stacked_apr_pct'), 0):+.2f}%</td>"
            f"<td class='num'>{_nf(s.get('capital_efficiency_pct'), 0):.0f}%</td>"
            f"<td class='num pos'><b>{_nf(s.get('net_deposit_apr_pct'), 0):+.2f}%</b></td>"
            f"<td class='num'>{_nf(s.get('liq_buffer_pct'), 0):.0f}%</td>"
            f"<td class='muted'>{_e(s.get('key_risks'))}</td>"
            "</tr>"
        )
    if not ys_rows:
        ys_rows = '<tr><td colspan="7" class="muted">Нет сохранённого расчёта HyperEVM Yield Stack</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>Hyperliquid Quantitative & DeFi Desk Dashboard — hyperliquid-quant-defi</title>
<style>
  :root {{
    --bg: #070d17; --panel: #0f172a; --panel2: #111c33; --border: #1e293b;
    --text: #e2e8f0; --muted: #94a3b8; --green: #10b981; --red: #f43f5e;
    --amber: #f59e0b; --cyan: #38bdf8;
  }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Inter", "Segoe UI", sans-serif; background: var(--bg); color: var(--text); line-height: 1.45; }}
  .wrap {{ max-width: 1320px; margin: 0 auto; padding: 24px 20px 48px; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-end; flex-wrap: wrap; gap: 12px; border-bottom: 1px solid var(--border); padding-bottom: 16px; margin-bottom: 20px; }}
  h1 {{ margin: 0; font-size: 22px; letter-spacing: -0.02em; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .badge-live {{ background: rgba(16,185,129,0.14); color: var(--green); border: 1px solid rgba(16,185,129,0.35); padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; }}
  .kpis {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }}
  .kpi {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 14px 16px; }}
  .kpi .lbl {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; }}
  .kpi .val {{ font-size: 21px; font-weight: 700; margin: 4px 0 2px; }}
  .kpi .sub {{ color: var(--muted); font-size: 12px; }}
  .tabs {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px; }}
  .tab-btn {{ background: var(--panel); color: var(--muted); border: 1px solid var(--border); border-radius: 8px; padding: 8px 14px; font-size: 13px; font-weight: 600; cursor: pointer; transition: 0.15s; }}
  .tab-btn:hover {{ color: var(--text); border-color: #334155; }}
  .tab-btn.active {{ background: rgba(16,185,129,0.16); color: var(--green); border-color: rgba(16,185,129,0.5); }}
  .panel {{ display: none; }}
  .panel.active {{ display: block; }}
  .grid-2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(380px, 1fr)); gap: 16px; margin-bottom: 16px; }}
  .card {{ background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 16px; overflow-x: auto; }}
  .card h3, .card h4 {{ margin: 0 0 10px; font-size: 15px; }}
  .guide-box {{ background: linear-gradient(135deg, rgba(16,185,129,0.08), rgba(56,189,248,0.08)); border: 1px solid rgba(56,189,248,0.28); border-radius: 12px; padding: 14px 16px; margin-bottom: 16px; }}
  .guide-title {{ font-size: 13px; font-weight: 700; color: var(--cyan); margin-bottom: 6px; }}
  .guide-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 10px; font-size: 13px; color: var(--text); }}
  .guide-item b {{ color: #f8fafc; }}
  .meta-line {{ color: var(--muted); font-size: 12px; margin-bottom: 10px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
  th, td {{ padding: 8px 10px; border-bottom: 1px solid var(--border); text-align: left; white-space: nowrap; }}
  th {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; background: var(--panel2); }}
  td.num, th.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
  tr:hover td {{ background: rgba(56,189,248,0.04); }}
  .pos {{ color: var(--green); }}
  .neg {{ color: var(--red); }}
  .muted {{ color: var(--muted); font-size: 12px; }}
  .pill {{ display: inline-block; padding: 2px 7px; border-radius: 999px; font-size: 11px; font-weight: 600; }}
  .pill.green {{ background: rgba(16,185,129,0.16); color: var(--green); }}
  .pill.red {{ background: rgba(244,63,94,0.16); color: var(--red); }}
  .pill.amber {{ background: rgba(245,158,11,0.18); color: var(--amber); }}
  .pill.gray {{ background: rgba(148,163,184,0.15); color: var(--muted); }}
  code {{ background: #1e293b; padding: 1px 5px; border-radius: 4px; font-size: 12px; }}
  .spark {{ width: 100%; height: 86px; display: block; }}
  .chart-svg {{ width: 100%; max-height: 170px; display: block; }}
  footer {{ margin-top: 28px; color: var(--muted); font-size: 12px; text-align: center; border-top: 1px solid var(--border); padding-top: 14px; }}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div>
      <h1>Hyperliquid Quantitative &amp; On-Chain DeFi Desk</h1>
      <div class="subtitle">hyperliquid-quant-defi · Alpha Research · Paper L2 · Testnet ALO · Avellaneda–Stoikov MM v2 · HIP-3 · Boros · 5-DEX · HyperEVM 999</div>
    </div>
    <span class="badge-live">● Live Snapshot · {_e(d['generated_at_utc'])}</span>
  </header>

  <section class="kpis">
    <div class="kpi">
      <div class="lbl">1. Аудит метамодели (58 гип.)</div>
      <div class="val" style="color: var(--amber);">1.5 → 5.0 б.п.</div>
      <div class="sub">Измеренный ALO fill 55% · Ex-INIT −0.37% → Obs-Only</div>
    </div>
    <div class="kpi">
      <div class="lbl">2. Тестнет-счёт (ALO ордера)</div>
      <div class="val">${tn_eq:,.2f}</div>
      <div class="sub">Исполнение ALO <b>{_e(tn_fill_txt)}</b> · Риск ликв.: <b>{tn_liq_warns}</b></div>
    </div>
    <div class="kpi">
      <div class="lbl">3. Маркет-мейкер (живой тест)</div>
      <div class="val {'pos' if mm_pnl >= 0 else 'neg'}">{mm_pnl:+.4f}$</div>
      <div class="sub">Оборот ${mm_vol:,.1f} · 100% Maker · скорость <b>{_nf(lat.get('p50_us'), 0):.1f} мкс</b></div>
    </div>
    <div class="kpi">
      <div class="lbl">4. Арбитраж HL × Lighter</div>
      <div class="val pos">{cd_avg_apr:+.1f}% APR</div>
      <div class="sub">Открыто {len(cd_positions)} пар · Баланс ${_nf(cd_paper.get('equity_usd'), 50000):,.2f}</div>
    </div>
    <div class="kpi">
      <div class="lbl">5. HyperEVM (Chain {_ni(evm_chain.get('chain_id'), 999)}) и HLP</div>
      <div class="val pos">${_nf(hlp_vault.get('tvl_usd'), 0)/1e6:,.1f}M</div>
      <div class="sub">Доходность HLP {_nf(hlp_vault.get('apr_pct'), 0):+.2f}% · Пег USDT0 {_nf(evm_chain.get('usdt0_peg_discount_bps'), 0):+.1f} б.п.</div>
    </div>
    <div class="kpi">
      <div class="lbl">6. Аудит фандинга в :00</div>
      <div class="val">{_ni(s_meta.get('total_settlement_events'), 0):,}</div>
      <div class="sub">{_ni(s_meta.get('coins_collected'), 0)} монет · {_nf(s_meta.get('span_days'), 0):.1f} дн. · Фильтр в проде</div>
    </div>
  </section>

  <nav class="tabs" role="tablist">
    <button class="tab-btn active" onclick="openTab('exec', this)">1. Testnet &amp; Paper L2</button>
    <button class="tab-btn" onclick="openTab('mm', this)">2. Avellaneda–Stoikov MM v2</button>
    <button class="tab-btn" onclick="openTab('cdex', this)">3. Cross-DEX v2 (5 DEX) &amp; HL×Lighter</button>
    <button class="tab-btn" onclick="openTab('evm', this)">4. HyperEVM Chain 999 &amp; HLP</button>
    <button class="tab-btn" onclick="openTab('alpha', this)">5. Ликвидации китов &amp; :00 OOS</button>
    <button class="tab-btn" onclick="openTab('frontier', this)">6. HIP-3, Boros &amp; Yield Stack</button>
  </nav>

  <section id="tab-exec" class="panel active">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: что показано на вкладке «Testnet &amp; Paper L2»</div>
      <div class="guide-grid">
        <div class="guide-item"><b>Что делает бот:</b> каждые 4 часа трекер наблюдает за метамоделью уже по честным измеренным издержкам <code>5.0 б.п.</code> (PR #16–#19 показали, что при реальном BBO ALO fill 55% метамодель без <code>INIT</code> в нуле, поэтому она остаётся только в paper-наблюдении).</div>
        <div class="guide-item"><b>Как читать цифры:</b> на Testnet проверяется сам механизм исполнения: <code>ALO Fill &ge; 85%</code> на малых клипах — минимум комиссий; <code>До ликв. &gt; 25%</code> и <code>Риск ликв. = 0</code> — безопасная маржа.</div>
        <div class="guide-item"><b>Статус сейчас:</b> {'🟢 Метамодель честно переведена в observation-only; Testnet-контур исполнения в норме (0 предупреждений по ликвидации, фильтр <code>onlyIsolated</code> активен).' if tn_liq_warns == 0 else f'🟠 Есть {tn_liq_warns} предупреждений по дистанции до ликвидации.'}</div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h3>Paper L2 Shadow Tracker (HL × Bybit/Binance/OKX)</h3>
        <div class="meta-line">Тиков: <b>{_ni(paper.get('ticks'), 0)}</b> · Позиций: <b>{_ni(paper.get('active_positions'), 0)}</b> · Equity: <b>${_nf(paper.get('equity_usd'), 100000):,.2f}</b> ({_nf(paper.get('return_pct'), 0):+.3f}%) · HL fee <code>5.0 б.п.</code> (PR #19) · Obs-Only</div>
        {paper_spark}
      </div>
      <div class="card">
        <h3>Пост-трейд атрибуция Hyperliquid Testnet</h3>
        <div class="meta-line">Адрес: <code>{_e(attr.get('account_address', '—'))}</code> · Сделок: <b>{_ni(attr.get('total_fills'), 0)}</b> · Оборот: <b>${_nf(attr.get('total_volume_usd'), 0):,.1f}</b> · Maker: <b>{_nf(attr.get('maker_volume_share_pct'), 0):.1f}%</b> · Fee: <b>{_nf(attr.get('effective_fee_bps'), 0):.2f} б.п.</b></div>
        <div class="meta-line">Риски: <b>{tn_liq_warns}</b> ликв. · <b>{tn_adl_warns}</b> ADL-алертов · API Quota: <code>{_e(tn_api_txt)}</code> · защита <code>onlyIsolated</code> + <code>cross:3x</code> + <code>:55–:59</code>.</div>
      </div>
    </div>
    <div class="card">
      <h3>Разрез живых позиций и сделок Testnet</h3>
      <table>
        <thead><tr><th>Монета</th><th>Позиция</th><th>Режим</th><th class="num">Филлов</th><th class="num">Maker %</th><th class="num">Net PnL</th><th class="num">Markout</th><th class="num">До ликв.</th><th class="num">ADL Rank</th></tr></thead>
        <tbody>{per_coin_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="tab-mm" class="panel">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: что делает «Маркет-мейкер Avellaneda–Stoikov v2»</div>
      <div class="guide-grid">
        <div class="guide-item"><b>Идея:</b> робот одновременно ставит заявку на покупку и продажу в стакане и зарабатывает спред, автоматически сдвигая цены при накоплении позиции.</div>
        <div class="guide-item"><b>Как читать цифры:</b> <code>Markout +5s &gt; 0</code> — нас не «переезжают» быстрые игроки; <code>LeadPull</code> — сколько раз бот успел убрать ордер по сигналу Binance; графики внизу показывают, как часто исполняются ордера в глубине стакана.</div>
        <div class="guide-item"><b>Статус сейчас:</b> 🟢 Живой тест на BTC+ETH закрыт в плюс (<b>{mm_pnl:+.4f}$</b>, 100% Maker, скорость расчёта <b>{_nf(lat.get('p50_us'), 0):.2f} мкс</b>).</div>
      </div>
    </div>
    <div class="card">
      <h3>Живая мульти-монетная сессия Avellaneda–Stoikov MM v2 (WS bbo+l2Book + Binance Lead-Lag + batchModify + scheduleCancel)</h3>
      <div class="meta-line">Бенчмарк горячего пути (<code>3 000</code> тиков): <b>p50 = {_nf(lat.get('p50_us'), 0):.2f} мкс</b> · <b>p95 = {_nf(lat.get('p95_us'), 0):.2f} мкс</b> · <b>p99 = {_nf(lat.get('p99_us'), 0):.2f} мкс</b> · <b>{_ni(lat.get('throughput_ticks_per_sec'), 0):,}</b> тик/с · <code>:59→:05</code> funding overlay встроен.</div>
      <table>
        <thead><tr><th>Монета</th><th class="num">Тики (bbo)</th><th class="num">batchMod / LeadPull</th><th class="num">Полуспред</th><th class="num">Сделки</th><th class="num">Оборот · Maker</th><th class="num">Net PnL</th><th class="num">Markout +5s</th></tr></thead>
        <tbody>{mm_rows}</tbody>
      </table>
    </div>
    <div class="grid-2">{cal_cards}</div>
  </section>

  <section id="tab-cdex" class="panel">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: что такое «Cross-DEX (5 бирж) и HL × Lighter»</div>
      <div class="guide-grid">
        <div class="guide-item"><b>Идея:</b> если на Hyperliquid фандинг отрицательный (нам платят за лонг), а на Lighter положительный (нам платят за шорт) — мы открываем встречные позиции и забираем разницу ставок без риска направления цены.</div>
        <div class="guide-item"><b>Как читать цифры:</b> важен не только <code>Спред APR</code>, но и <code>L2 Slip</code> (проскальзывание стакана) и <code>Окуп.</code> (через сколько часов спред окупит комиссии). Строки с <code>Тонкий</code> или большим slip — ловушки.</div>
        <div class="guide-item"><b>Статус сейчас:</b> 🟢 4-часовой трекер держит <b>{len(cd_positions)}</b> проверенных пар со средним спредом <b>{cd_avg_apr:+.1f}% APR</b> и проверяет их живучесть в реальном времени.</div>
      </div>
    </div>
    <div class="card">
      <h3>Двуногий Paper-трекер v2: Hyperliquid × Lighter × Extended (Persistence + Slip ≤ 5 б.п. + H1 Basis+Carry)</h3>
      <div class="meta-line">Баланс: <b>${_nf(cd_paper.get('equity_usd'), 50000):,.2f}</b> · Persistence ≥ 2 тика, L2 slip ≤ 5 б.п., Basis ≥ −3 б.п., H1-Combo (Basis ≥ +8 б.п.) · Funding {_nf(cd_paper.get('cum_funding_usd'), 0):+.2f}$ · Costs −{_nf(cd_paper.get('cum_costs_usd'), 0):.2f}$</div>
      <table>
        <thead><tr><th>Монета</th><th>Площадка / Тип</th><th>Направление</th><th class="num">Нога ($)</th><th class="num">Live APR</th><th class="num">L2 Slip</th><th class="num">Окуп.</th><th class="num">Часов</th><th class="num">Net PnL</th></tr></thead>
        <tbody>{cdp_rows}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>5-DEX Кросс-DEX сканер v2 (HL × Lighter × Extended × dYdX v4 × Paradex · L2 VWAP $10k/$50k)</h3>
      <table>
        <thead><tr><th>Монета</th><th>Направление</th><th class="num">Спред APR</th><th class="num">L2 Exec Basis $10k</th><th class="num">Slip $10k / $50k</th><th>Глубина</th><th class="num">Объём HL / DEX</th></tr></thead>
        <tbody>{cd5_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="tab-evm" class="panel">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: зачем нужны «HyperEVM (Chain 999) и HLP»</div>
      <div class="guide-grid">
        <div class="guide-item"><b>Идея:</b> проверяем разницу цен между спотовыми DEX-пулами внутри блокчейна HyperEVM и фьючерсами Hyperliquid, а также следим за главным пулом ликвидности биржи (HLP).</div>
        <div class="guide-item"><b>Как читать цифры:</b> <code>Дрейф 0.00 б.п.</code> значит, что ончейн-оракул <code>L1Read</code> абсолютно точен. <code>Peg-Adj</code> убирает ложный арбитраж из-за того, что USDT0 торгуется с дисконтом к USDC.</div>
        <div class="guide-item"><b>Статус сейчас:</b> 🟢 Ложные арбитражи отфильтрованы, HLP держит <b>${_nf(hlp_vault.get('tvl_usd'), 0)/1e6:,.1f}M</b> под <b>{_nf(hlp_vault.get('apr_pct'), 0):+.2f}% APR</b>.</div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h3>Нативные прекомпилы HyperEVM <code>L1Read</code> (0x0806 / 0x0807 / 0x0809)</h3>
        <div class="meta-line">EVM блок <code>#{_ni(evm_chain.get('block_number'), 0):,}</code> · L1 блок <code>#{_ni(evm_chain.get('l1_block_number'), 0):,}</code> · Газ <code>{_nf(evm_chain.get('gas_price_gwei'), 0):.3f} Gwei</code></div>
        <table>
          <thead><tr><th>Монета</th><th class="num">Idx</th><th class="num">L1Read Mark</th><th class="num">L1Read Oracle</th><th class="num">REST Mark</th><th class="num">Дрейф</th></tr></thead>
          <tbody>{l1_rows}</tbody>
        </table>
      </div>
      <div class="card">
        <h3>HLP и дочерние стратегии / ликвидаторы</h3>
        <table>
          <thead><tr><th>Волт</th><th class="num">TVL</th><th class="num">APR</th><th class="num">Поз.</th><th class="num">Номинал</th><th class="num">Маржа</th></tr></thead>
          <tbody>{vault_rows}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h3>Истинный (Peg-Adjusted) Арбитраж HyperCore ↔ HyperEVM + V3-глубина</h3>
      <div class="meta-line">Пег <code>{_e(evm_chain.get('stable_peg_source', '—'))}</code>: <code>1 USDC = {_nf(evm_chain.get('usdt0_per_usdc'), 1.0):.6f} USDT0</code> ({_nf(evm_chain.get('usdt0_peg_discount_bps'), 0):+.2f} б.п.)</div>
      <table>
        <thead><tr><th>DEX</th><th>Пул</th><th class="num">Сырой</th><th class="num">Peg-Adj</th><th class="num">Net (0-clip)</th><th class="num">Вирт. глубина</th><th class="num">Slip $25k</th></tr></thead>
        <tbody>{evm_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="tab-alpha" class="panel">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: что показывают «Ликвидации китов и аудит фандинга :00»</div>
      <div class="guide-grid">
        <div class="guide-item"><b>Идея:</b> на Hyperliquid видны точные цены ликвидации крупных трейдеров и фандинг платится каждый час ровно в <code>:00</code>. Мы проверили <b>{_ni(s_meta.get('total_settlement_events'), 0):,}</b> таких расчётов, чтобы отделить реальный доход от мифов.</div>
        <div class="guide-item"><b>Как читать вердикты:</b> <code>NO-GO</code> — стратегия теряет деньги после комиссий (не торгуем); <code>MM Overlay</code> — сигнал статистически реален (<code>+2.3…+3.8 б.п.</code>), поэтому мы вшили его бесплатно в сдвиг котировок маркет-мейкера и фильтр экзекутора.</div>
        <div class="guide-item"><b>Статус сейчас:</b> 🟢 Оба вывода уже работают в коде: экзекутор не лезет в ловушку в <code>:55–:59</code>, а MM сдвигает котировки в <code>:59–:05</code>.</div>
      </div>
    </div>
    <div class="grid-2">
      <div class="card">
        <h3>On-Chain Карта ликвидаций направленных китов (H2 · 0–10% от Mark)</h3>
        <div class="meta-line">Обновлено: <code>{_e(oc.get('observed_at_utc') or '—')}</code> · кэш лидерборда 6ч · гибридный отбор плечевых китов</div>
        <table>
          <thead><tr><th>Монета</th><th class="num">Mark</th><th class="num">Китов</th><th class="num">Кластер 0–10%</th><th class="num">Ближ. стенка</th><th>Магнит</th></tr></thead>
          <tbody>{liq_rows}</tbody>
        </table>
      </div>
      <div class="card">
        <h3>H6 Стресс BookMid ↔ Mark ↔ Oracle &amp; HLP MM PnL Inversion</h3>
        <div class="meta-line">Режим MM PnL: {mm_regime_pill} · HLP Child Equity: <b>${_nf(mm_pnl_box.get('hlp_child_account_value_usd'), 0)/1e6:,.2f}M</b> · Cooldown алертов: <code>4ч</code></div>
        <table>
          <thead><tr><th>Монета</th><th class="num">Mid vs Mark</th><th class="num">Mark vs Oracle</th><th>Тип</th><th class="num">Объём 24ч</th><th>Статус</th></tr></thead>
          <tbody>{h6_rows}</tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h3>Каузальное {_nf(s_meta.get('span_days'), 0):.0f}-дневное OOS-исследование расчётов фандинга :00 (Dev 65% vs Holdout 35%)</h3>
      <div class="meta-line">Монет: <b>{_ni(s_meta.get('coins_collected'), 0)}</b> · Часовых расчётов: <b>{_ni(s_meta.get('total_settlement_events'), 0):,}</b> · Нулевой lookahead (<code>f(T0-1h)</code>) · Maker RT 3.0 б.п.</div>
      <table>
        <thead><tr><th>Бакет</th><th>Стратегия</th><th class="num">Dev N · Gross (t)</th><th class="num">Holdout N · Gross (t)</th><th class="num">Holdout Net Maker</th><th>Вердикт</th></tr></thead>
        <tbody>{s_rows}</tbody>
      </table>
    </div>
  </section>

  <section id="tab-frontier" class="panel">
    <div class="guide-box">
      <div class="guide-title">💡 Простыми словами: зачем нужна вкладка «HIP-3, Boros &amp; Yield Stack» (2026)</div>
      <div class="guide-grid">
        <div class="guide-item"><b>HIP-3 (акции 24/7):</b> сторонние билдеры (<code>xyz</code>, <code>io</code>, <code>para</code>) запустили перпы на акции и сырьё (<code>NVDA</code>, <code>SP500</code>, <code>GOLD</code>). Когда фандинг на <code>xyz</code> и <code>io</code> расходится — возникает внутрибиржевой арбитраж вообще без мостов.</div>
        <div class="guide-item"><b>Pendle Boros:</b> позволяет зафиксировать плавающий фандинг Hyperliquid по твёрдой ставке (<code>Fixed Bid</code>) на 28 дней вперёд — убирая риск, что спред схлопнется завтра.</div>
        <div class="guide-item"><b>HyperEVM Yield Stack:</b> сравнивает чистый доход дельта-нейтральной связки <code>kHYPE + Short HYPE + Boros Fix</code> (с учётом расщепления маржи и буфера ликвидации) против пассивного <code>HLP</code>.</div>
      </div>
    </div>
    <div class="card">
      <h3>HIP-3 Кросс-билдер арбитраж на HyperCore L1 (нулевой риск моста · Growth Mode &amp; Oracle Guard)</h3>
      <div class="meta-line">Билдеров: <b>{_ni(hip3.get('builders_discovered'), 0)}</b> · Рынков: <b>{_ni(hip3.get('active_markets_count'), 0)}</b> · Оборот 24ч: <b>${_nf(hip3.get('total_hip3_day_vol_usd'), 0)/1e9:,.2f}B</b> · Сессия NY: <code>{_e(hip3.get('ny_session', '—'))}</code></div>
      <table>
        <thead><tr><th>Тикер</th><th>Ноги (Short / Long)</th><th class="num">Спред APR</th><th>Growth</th><th class="num">Oracle Div</th><th class="num">Exec Basis $5k</th><th class="num">Slip $5k / $25k</th><th class="num">Окуп. M/T</th><th>Режим</th></tr></thead>
        <tbody>{hip3_rows}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>Pendle Boros — Фиксация ставок фандинга Hyperliquid и Spread Markets</h3>
      <div class="meta-line">Просканировано рынков Boros: <b>{_ni(boros.get('total_markets_scanned'), 0)}</b> · Активных: <b>{_ni(boros.get('active_markets_count'), 0)}</b> · Hyperliquid / Spread: <b>{_ni(boros.get('hl_and_spread_count'), 0)}</b></div>
      <table>
        <thead><tr><th>ID</th><th>Рынок Boros</th><th class="num">Срок</th><th class="num">Fixed Bid / Ask</th><th class="num">Float</th><th class="num">Bid − Float</th><th>Идея</th></tr></thead>
        <tbody>{boros_rows}</tbody>
      </table>
    </div>
    <div class="card">
      <h3>HyperEVM Composable Delta-Neutral Yield Stack (kHYPE / stHYPE + Short HYPE + Boros vs HLP)</h3>
      <div class="meta-line">HYPE Funding: <b>{_nf(ys_inp.get('hype_funding_apr_pct'), 0):+.2f}%</b> · kHYPE: <b>{_nf(ys_inp.get('khype_apy_pct'), 0):.2f}%</b> (${_nf(ys_inp.get('khype_tvl_usd'), 0)/1e6:,.0f}M) · Boros Fix: <b>{float(ys_inp.get('boros_hype_fixed_bid_apr_pct') or 0):+.2f}%</b> · HLP: <b>{_nf(ys_inp.get('hlp_apr_pct'), 0):.2f}%</b></div>
      <table>
        <thead><tr><th>Связка</th><th>Тип</th><th class="num">Gross</th><th class="num">Эфф. кап.</th><th class="num">Net на депозит</th><th class="num">Буфер</th><th>Риски</th></tr></thead>
        <tbody>{ys_rows}</tbody>
      </table>
    </div>
  </section>

  <footer>
    hyperliquid-quant-defi · Self-contained offline HTML portfolio showcase · Все боевые ордера изолированы в Hyperliquid Testnet · 135 unit-тестов passed · 7 live crons
  </footer>
</div>
<script>
function openTab(id, btn) {{
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
  document.getElementById('tab-' + id).classList.add('active');
  btn.classList.add('active');
}}
</script>
</body>
</html>
"""
