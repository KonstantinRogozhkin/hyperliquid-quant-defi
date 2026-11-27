"""HyperEVM Composable Delta-Neutral Yield Stack Monitor & Risk Calculator.

100% read-only calculator combining:
1. **DeFiLlama Live HyperEVM Pools** (`kinetiq-khype`, `sthype`, `hyperlend-pooled`, `felix-cdp`,
   `morpho-blue FEUSDCV2`, `project-x WHYPE-USDC`, `liminal-basis`, `hyperliquid-hlp`);
2. **Live HyperCore `HYPE` Perp Funding** (`metaAndAssetCtxs`);
3. **Live Pendle Boros `Hyperliquid HYPE` Fixed-Rate Maturity Bid** (`hl_boros_fixed_funding.py`).

Models realistic capital allocation (spot/LST leg + perp margin collateral buffer), liquidation
move distance on the short perp leg, optional looping LTV/borrow drag, and benchmarks every
delta-neutral stack directly against passive **HLP** (`~$152.5M` TVL).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import math
import time
from typing import Any

import httpx

from hl_quant import hl_boros_fixed_funding as boros
from hl_quant import hl_hyperevm_defi as evm

DEFILLAMA_POOLS_URL = "https://yields.llama.fi/pools"


@dataclass(frozen=True, slots=True)
class HyperEvmYieldInputs:
    observed_at_ms: int
    hype_mark_px: float
    hype_funding_apr_pct: float
    boros_hype_market_name: str
    boros_hype_fixed_bid_apr_pct: float | None
    boros_hype_days_to_maturity: float
    khype_apy_pct: float
    khype_tvl_usd: float
    sthype_apy_pct: float
    sthype_tvl_usd: float
    morpho_feusd_apy_pct: float
    morpho_feusd_tvl_usd: float
    hyperlend_khype_tvl_usd: float
    felix_whype_tvl_usd: float
    liminal_xhype_apy_pct: float
    hlp_apr_pct: float
    hlp_tvl_usd: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class YieldStackStrategyRow:
    name: str
    rate_type: str  # "Fixed (Boros)" | "Floating" | "Benchmark"
    gross_stacked_apr_pct: float  # on hedged notional
    capital_efficiency_pct: float  # % of total deposit deployed as hedged notional (e.g. 66.7% at 2x perp margin)
    net_deposit_apr_pct: float  # true net APR on total $ deposit after margin split & borrow cost
    spread_vs_hlp_pct: float
    perp_leverage: float
    liq_buffer_pct: float  # approximate % upward HYPE rally before short perp margin hits liquidation
    key_risks: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class HyperEvmYieldSnapshot:
    inputs: HyperEvmYieldInputs
    strategies: list[YieldStackStrategyRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": self.inputs.to_dict(),
            "strategies": [s.to_dict() for s in self.strategies],
        }


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def build_hyperevm_yield_strategies(
    inp: HyperEvmYieldInputs,
    *,
    perp_leverage: float = 2.0,
    loop_ltv: float = 0.50,
    assumed_stable_borrow_apr_pct: float = 3.5,
) -> list[YieldStackStrategyRow]:
    """Compute realistic capital-adjusted net APRs and liquidation buffers for 5 HyperEVM stacks."""
    # Split-capital delta-neutral: 1 unit spot LST + (1 / perp_leverage) unit USDC perp margin
    # -> hedged notional as share of total deposit = 1 / (1 + 1/L) = L / (L + 1)
    lev = max(1.0, float(perp_leverage))
    cap_eff = lev / (lev + 1.0)
    # Approximate upward move on short HYPE before maintenance margin (~10% buffer) is breached
    liq_buf_pct = round((1.0 / lev - 0.08) * 100.0, 1)

    rows: list[YieldStackStrategyRow] = []

    # 1. Fixed-Rate Locked Delta-Neutral: kHYPE + Short HYPE Perp + Short Boros HYPE Fixed
    if inp.boros_hype_fixed_bid_apr_pct is not None:
        gross_fix = inp.khype_apy_pct + inp.boros_hype_fixed_bid_apr_pct
        net_fix = gross_fix * cap_eff
        rows.append(
            YieldStackStrategyRow(
                name=f"kHYPE + Short HYPE + Boros Fix ({inp.boros_hype_days_to_maturity:.0f}d)",
                rate_type="Fixed (Boros)",
                gross_stacked_apr_pct=round(gross_fix, 2),
                capital_efficiency_pct=round(cap_eff * 100.0, 1),
                net_deposit_apr_pct=round(net_fix, 2),
                spread_vs_hlp_pct=round(net_fix - inp.hlp_apr_pct, 2),
                perp_leverage=lev,
                liq_buffer_pct=liq_buf_pct,
                key_risks="kHYPE депег, контракт Boros/Kinetiq, доливка маржи при ралли HYPE",
            )
        )

    # 2. Floating Delta-Neutral: kHYPE + Short HYPE Perp
    gross_flt = inp.khype_apy_pct + inp.hype_funding_apr_pct
    net_flt = gross_flt * cap_eff
    rows.append(
        YieldStackStrategyRow(
            name="kHYPE + Short HYPE Perp (Floating)",
            rate_type="Floating",
            gross_stacked_apr_pct=round(gross_flt, 2),
            capital_efficiency_pct=round(cap_eff * 100.0, 1),
            net_deposit_apr_pct=round(net_flt, 2),
            spread_vs_hlp_pct=round(net_flt - inp.hlp_apr_pct, 2),
            perp_leverage=lev,
            liq_buffer_pct=liq_buf_pct,
            key_risks="Падение/отрицательный фандинг HYPE, kHYPE депег",
        )
    )

    # 3. Composable Loop: kHYPE collateral on HyperLend/Felix (LTV 50%) -> borrow feUSD/USDC -> perp margin + Morpho yield
    # Here 100% of deposit buys kHYPE; borrowing 50% LTV funds the 2x short HYPE perp margin!
    # Net = kHYPE_APY + HYPE_funding_or_Boros - LTV * borrow_apr + idle_stable_carry
    ref_fund = inp.boros_hype_fixed_bid_apr_pct if inp.boros_hype_fixed_bid_apr_pct is not None else inp.hype_funding_apr_pct
    borrow_drag = loop_ltv * assumed_stable_borrow_apr_pct
    # Excess borrowed stables (if any) earn Morpho FEUSDCV2 APY; at LTV=50% and 2x perp, 100% of borrow funds perp margin
    gross_loop = inp.khype_apy_pct + ref_fund
    net_loop = gross_loop - borrow_drag
    rows.append(
        YieldStackStrategyRow(
            name=f"Composable Loop: kHYPE -> Felix/HyperLend ({int(loop_ltv*100)}% LTV) -> Short HYPE",
            rate_type="Fixed (Boros)" if inp.boros_hype_fixed_bid_apr_pct is not None else "Floating",
            gross_stacked_apr_pct=round(gross_loop, 2),
            capital_efficiency_pct=100.0,
            net_deposit_apr_pct=round(net_loop, 2),
            spread_vs_hlp_pct=round(net_loop - inp.hlp_apr_pct, 2),
            perp_leverage=round(1.0 / max(loop_ltv, 0.1), 1),
            # Short-perp buffer must use the loop's own leverage (1 / LTV), not the caller's perp_leverage.
            liq_buffer_pct=round(
                max(0.0, min((max(loop_ltv, 0.1) - 0.08) * 100.0, (1.0 - loop_ltv / 0.78) * 100.0)), 1
            ),
            key_risks="Двусторонний риск ликвидации (обвал HYPE на CDP + ралли HYPE на перпе), рост borrow rate",
        )
    )

    # 4. Alternative LST: stHYPE + Short HYPE (+ Boros Fixed if available)
    gross_st = inp.sthype_apy_pct + ref_fund
    net_st = gross_st * cap_eff
    rows.append(
        YieldStackStrategyRow(
            name="stHYPE + Short HYPE Perp" + (" + Boros Fix" if inp.boros_hype_fixed_bid_apr_pct is not None else ""),
            rate_type="Fixed (Boros)" if inp.boros_hype_fixed_bid_apr_pct is not None else "Floating",
            gross_stacked_apr_pct=round(gross_st, 2),
            capital_efficiency_pct=round(cap_eff * 100.0, 1),
            net_deposit_apr_pct=round(net_st, 2),
            spread_vs_hlp_pct=round(net_st - inp.hlp_apr_pct, 2),
            perp_leverage=lev,
            liq_buffer_pct=liq_buf_pct,
            key_risks="stHYPE ликвидность/депег, доливка маржи при ралли HYPE",
        )
    )

    # 5. Benchmarks: Passive HLP Vault & Liminal xHYPE Basis Vault
    rows.append(
        YieldStackStrategyRow(
            name="Benchmark: HLP Vault (Hyperliquidity Provider)",
            rate_type="Benchmark",
            gross_stacked_apr_pct=round(inp.hlp_apr_pct, 2),
            capital_efficiency_pct=100.0,
            net_deposit_apr_pct=round(inp.hlp_apr_pct, 2),
            spread_vs_hlp_pct=0.0,
            perp_leverage=1.0,
            liq_buffer_pct=100.0,
            key_risks="Токсичный поток / инвентарь ликвидаций (кейс JELLYJELLY), 4д lockup",
        )
    )
    if inp.liminal_xhype_apy_pct > 0:
        rows.append(
            YieldStackStrategyRow(
                name="Benchmark: Liminal Basis (xHYPE)",
                rate_type="Benchmark",
                gross_stacked_apr_pct=round(inp.liminal_xhype_apy_pct, 2),
                capital_efficiency_pct=100.0,
                net_deposit_apr_pct=round(inp.liminal_xhype_apy_pct, 2),
                spread_vs_hlp_pct=round(inp.liminal_xhype_apy_pct - inp.hlp_apr_pct, 2),
                perp_leverage=1.0,
                liq_buffer_pct=100.0,
                key_risks="Смарт-контракт протокола, плавающий фандинг HYPE",
            )
        )

    rows.sort(key=lambda r: (r.rate_type == "Benchmark", -r.net_deposit_apr_pct))
    return rows


def fetch_hyperevm_yield_inputs(
    client: httpx.Client,
    *,
    defillama_url: str = DEFILLAMA_POOLS_URL,
    info_url: str = evm.HYPERCORE_INFO_URL,
) -> HyperEvmYieldInputs:
    """Fetch live HyperEVM LST/lending yields, HYPE perp funding, and Boros fixed HYPE rate."""
    now_ms = int(time.time() * 1000)
    perps, _ = evm.fetch_hypercore_markets(client, info_url=info_url)
    hype = perps.get("HYPE") or {}
    hype_px = _f(hype.get("mark_px"))
    hype_apr = _f(hype.get("funding_hourly")) * 8760.0 * 100.0

    # Live Pendle Boros HYPE fixed market (longest active >= 7 days with highest OI)
    boros_name = "—"
    boros_bid: float | None = None
    boros_days = 0.0
    try:
        _, b_mkts = boros.fetch_boros_active_markets(client)
        hype_mkts = [m for m in b_mkts if m.is_hyperliquid and not m.is_spread_market and m.underlying == "HYPE" and m.days_to_maturity >= 5.0]
        if hype_mkts:
            best = max(hype_mkts, key=lambda m: m.notional_oi)
            boros_name = best.name
            boros_bid = best.best_bid_apr_pct
            boros_days = best.days_to_maturity
    except Exception:
        pass

    # Live DeFiLlama Hyperliquid L1 / HyperEVM pools
    khype_apy = khype_tvl = sthype_apy = sthype_tvl = 0.0
    morpho_feusd_apy = morpho_feusd_tvl = hyperlend_khype_tvl = felix_whype_tvl = 0.0
    liminal_apy = hlp_apr = hlp_tvl = 0.0
    rl = client.get(defillama_url, timeout=25.0)
    if rl.status_code != 200:
        raise RuntimeError(f"DeFiLlama pools fetch failed with HTTP {rl.status_code}; refusing to emit zero yields")
    dl_payload = rl.json()
    dl_pools = (dl_payload or {}).get("data") if isinstance(dl_payload, dict) else None
    if not isinstance(dl_pools, list) or not dl_pools:
        raise RuntimeError("DeFiLlama pools payload is empty or malformed; refusing to emit zero yields")
    for p in dl_pools:
        proj = str(p.get("project") or "").lower()
        sym = str(p.get("symbol") or "").upper()
        chain = str(p.get("chain") or "").lower()
        tvl = _f(p.get("tvlUsd"))
        apy = _f(p.get("apy"))
        if "hyperliquid" not in chain and "hyperevm" not in chain:
            continue
        if proj == "kinetiq-khype" and sym == "KHYPE":
            khype_apy, khype_tvl = apy, tvl
        elif proj == "sthype" and sym == "STHYPE":
            sthype_apy, sthype_tvl = apy, tvl
        elif proj == "morpho-blue" and "FEUSD" in sym and tvl > morpho_feusd_tvl:
            morpho_feusd_apy, morpho_feusd_tvl = apy, tvl
        elif proj == "hyperlend-pooled" and sym == "KHYPE":
            hyperlend_khype_tvl = max(hyperlend_khype_tvl, tvl)
        elif proj == "felix-cdp" and sym == "WHYPE":
            felix_whype_tvl = max(felix_whype_tvl, tvl)
        elif proj == "liminal-basis" and "HYPE" in sym:
            liminal_apy = max(liminal_apy, apy)
        elif proj == "hyperliquid-hlp":
            hlp_apr, hlp_tvl = apy, tvl

    return HyperEvmYieldInputs(
        observed_at_ms=now_ms,
        hype_mark_px=round(hype_px, 4),
        hype_funding_apr_pct=round(hype_apr, 2),
        boros_hype_market_name=boros_name,
        boros_hype_fixed_bid_apr_pct=boros_bid,
        boros_hype_days_to_maturity=round(boros_days, 1),
        khype_apy_pct=round(khype_apy, 2),
        khype_tvl_usd=round(khype_tvl, 2),
        sthype_apy_pct=round(sthype_apy, 2),
        sthype_tvl_usd=round(sthype_tvl, 2),
        morpho_feusd_apy_pct=round(morpho_feusd_apy, 2),
        morpho_feusd_tvl_usd=round(morpho_feusd_tvl, 2),
        hyperlend_khype_tvl_usd=round(hyperlend_khype_tvl, 2),
        felix_whype_tvl_usd=round(felix_whype_tvl, 2),
        liminal_xhype_apy_pct=round(liminal_apy, 2),
        hlp_apr_pct=round(hlp_apr, 2),
        hlp_tvl_usd=round(hlp_tvl, 2),
    )


def run_hyperevm_yield_stack_scan(
    client: httpx.Client | None = None,
    *,
    perp_leverage: float = 2.0,
    loop_ltv: float = 0.50,
    assumed_stable_borrow_apr_pct: float = 3.5,
) -> HyperEvmYieldSnapshot:
    own = client is None
    c = client or httpx.Client(timeout=25.0)
    try:
        inp = fetch_hyperevm_yield_inputs(c)
    finally:
        if own:
            c.close()
    strats = build_hyperevm_yield_strategies(
        inp,
        perp_leverage=perp_leverage,
        loop_ltv=loop_ltv,
        assumed_stable_borrow_apr_pct=assumed_stable_borrow_apr_pct,
    )
    return HyperEvmYieldSnapshot(inputs=inp, strategies=strats)


# --------------------------------------------------------------------------- Reporting


def format_yield_stack_markdown(snap: HyperEvmYieldSnapshot) -> str:
    i = snap.inputs
    ts = datetime.fromtimestamp(i.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    bfix = f"`{i.boros_hype_fixed_bid_apr_pct:+.2f}%` ({i.boros_hype_market_name}, {i.boros_hype_days_to_maturity:.1f}д)" if i.boros_hype_fixed_bid_apr_pct is not None else "—"
    lines = [
        f"# HyperEVM Composable Delta-Neutral Yield Stack · kHYPE / Felix / HyperLend / Boros ({ts})",
        "",
        f"- **Живые ставки:** `HYPE` Mark `${i.hype_mark_px:.2f}` · HYPE Perp Funding **`{i.hype_funding_apr_pct:+.2f}% APR`** · Pendle Boros Fix: {bfix}",
        f"- **TVL экосистемы HyperEVM:** `kHYPE` `${i.khype_tvl_usd/1e6:,.1f}M` (`{i.khype_apy_pct:.2f}%`), `stHYPE` `${i.sthype_tvl_usd/1e6:,.1f}M` (`{i.sthype_apy_pct:.2f}%`), `HyperLend kHYPE` `${i.hyperlend_khype_tvl_usd/1e6:,.1f}M`, `Felix WHYPE` `${i.felix_whype_tvl_usd/1e6:,.1f}M`, `Morpho FEUSDCV2` `${i.morpho_feusd_tvl_usd/1e6:,.1f}M` (`{i.morpho_feusd_apy_pct:.2f}%`), **HLP** `${i.hlp_tvl_usd/1e6:,.1f}M` (**`{i.hlp_apr_pct:.2f}%`**).",
        "",
        "## Сравнение дельта-нейтральных связок (с учётом расщепления маржи и буфера ликвидации)",
        "",
        "| Стратегия | Тип ставки | Gross APR (на ногу) | Эфф. капитала | **Чистый APR на депозит** | vs HLP | Плечо | Буфер ликв. | Главные риски |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for s in snap.strategies:
        lines.append(
            f"| **{s.name}** | `{s.rate_type}` | {s.gross_stacked_apr_pct:+.2f}% | {s.capital_efficiency_pct:.1f}% | "
            f"**{s.net_deposit_apr_pct:+.2f}%** | {s.spread_vs_hlp_pct:+.2f}% | {s.perp_leverage:.1f}x | "
            f"{s.liq_buffer_pct:.0f}% | {s.key_risks} |"
        )
    return "\n".join(lines) + "\n"


def format_yield_stack_telegram_rich_html(snap: HyperEvmYieldSnapshot) -> str:
    i = snap.inputs
    ts = datetime.fromtimestamp(i.observed_at_ms / 1000, tz=timezone.utc).strftime("%d.%m %H:%M UTC")
    rows = [
        '<tr><th align="left">Связка HyperEVM</th><th align="right">Net APR</th><th align="right">Эфф. кап.</th><th align="right">Буфер</th></tr>'
    ]
    for s in snap.strategies:
        rows.append(
            "<tr>"
            f'<td align="left"><b>{html.escape(s.name)}</b></td>'
            f'<td align="right"><b>{s.net_deposit_apr_pct:+.2f}%</b></td>'
            f'<td align="right">{s.capital_efficiency_pct:.0f}%</td>'
            f'<td align="right">{s.liq_buffer_pct:.0f}%</td>'
            "</tr>"
        )
    return (
        f"<h1>⛓️ HyperEVM Delta-Neutral Yield Stack · {html.escape(ts)}</h1>"
        f"<p><b>HYPE Funding:</b> {i.hype_funding_apr_pct:+.2f}% · <b>kHYPE:</b> {i.khype_apy_pct:.2f}% · "
        f"<b>Boros Fix:</b> {i.boros_hype_fixed_bid_apr_pct or 0:+.2f}% · <b>HLP:</b> {i.hlp_apr_pct:.2f}%</p>"
        f'<table bordered striped>{"".join(rows)}</table>'
        "<footer>100% read-only калькулятор kHYPE / stHYPE + Short HYPE + Pendle Boros + Felix/HyperLend</footer>"
    )
