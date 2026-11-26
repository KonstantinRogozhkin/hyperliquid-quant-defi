"""HyperEVM (Chain 999) on-chain DeFi & HyperCore cross-layer intelligence scanner (v2).

100% read-only analytics combining:
1. **HyperEVM JSON-RPC** (``https://rpc.hyperliquid.xyz/evm``): block height, gas price,
   and direct ABI ``eth_call`` reads of live Uniswap-V3-style concentrated-liquidity pools
   on **HyperSwap V3** and **Project X (PRJX V3)** (``slot0``, ``liquidity``, ``token0``,
   ``token1``, ``decimals``) with zero external Web3 dependencies.
2. **True Peg-Adjusted HyperCore ↔ HyperEVM Arbitrage + V3 Depth Slippage**:
   - Reads live on-chain ``USDC/USDT0 1bp`` stable pools so USDT0-quoted pools are normalized
     into true USDC terms (eliminating false stablecoin-peg arb signals!).
   - Computes exact concentrated-liquidity active-tick virtual depth (USD) and average price
     impact (bps) for ``$5k``, ``$25k``, and ``$100k`` clip sizes from ``L`` and ``sqrtPriceX96``.
3. **HyperEVM Native ``L1Read`` Precompiles** (``0x000...0806..0809``):
   - Reads HyperCore ``markPx``, ``oraclePx``, and ``l1BlockNumber`` directly from inside
     HyperEVM via ``eth_call`` and verifies on-chain vs REST consensus.
4. **HyperCore Spot ↔ Perp Cash-and-Carry Basis** & **HLP / Liquidator Vault Intelligence**
   & **Extreme-Funding Dislocation Detector**.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import html
import math
from typing import Any

import httpx

HYPEREVM_RPC_URL = "https://rpc.hyperliquid.xyz/evm"
HYPERCORE_INFO_URL = "https://api.hyperliquid.xyz/info"
HLP_VAULT_ADDRESS = "0xdfc24b077bc1425ad1dea75bcb6f8158e10df303"

# Standard EVM function selectors
SEL_SLOT0 = "0x3850c7bd"
SEL_LIQUIDITY = "0x1a686502"
SEL_TOKEN0 = "0x0dfe1681"
SEL_TOKEN1 = "0xd21220a7"
SEL_DECIMALS = "0x313ce567"

# HyperEVM native L1Read precompile addresses (Chain 999)
PRECOMPILE_MARK_PX = "0x0000000000000000000000000000000000000806"
PRECOMPILE_ORACLE_PX = "0x0000000000000000000000000000000000000807"
PRECOMPILE_SPOT_PX = "0x0000000000000000000000000000000000000808"
PRECOMPILE_L1_BLOCK = "0x0000000000000000000000000000000000000809"

# Verified HyperEVM (Chain 999) canonical tokens
HYPEREVM_TOKENS: dict[str, tuple[str, int]] = {
    "WHYPE": ("0x5555555555555555555555555555555555555555", 18),
    "USDC": ("0xb88339cb7199b77e23db6e890353e22632ba630f", 6),
    "USDT0": ("0xb8ce59fc3717ada4c02eadf9682a9e934f625ebb", 6),
    "UBTC": ("0x9fdbda0a5e284c32744d2f17ee5c74b284993463", 8),
    "UETH": ("0xbe6727b535545c67d5caa73dea54865b92cf7907", 18),
    "USOL": ("0x068f321fa8fb9f0d135f290ef6a3e2813e1c8a29", 9),
}

# Verified live USDC/USDT0 1bp stable pools on HyperEVM for exact peg normalization
STABLE_PEG_POOLS: tuple[tuple[str, str, float], ...] = (
    ("PRJX V3 USDC/USDT0 1bp", "0x94291bea4c3ac9dbe81615083bb9a028722eebec", 1.0),
    ("HyperSwap V3 USDC/USDT0 1bp", "0x55443b2a8ee28dc35172d9e7d8982b4282415356", 1.0),
)

# Canonical HyperCore spot token -> perp coin mapping (Unit-bridged U-prefix assets)
SPOT_TO_PERP_ALIAS: dict[str, str] = {
    "HYPE": "HYPE",
    "UBTC": "BTC",
    "UETH": "ETH",
    "USOL": "SOL",
    "UPUMP": "PUMP",
    "UZEC": "ZEC",
    "UFART": "FARTCOIN",
    "PURR": "PURR",
    "HXMR": "XMR",
}


@dataclass(frozen=True, slots=True)
class PoolSpec:
    dex: str
    label: str
    pool_address: str
    base_symbol: str  # e.g. "WHYPE", "UBTC", "UETH"
    quote_symbol: str  # e.g. "USDC", "USDT0", "WHYPE"
    core_base_perp: str  # e.g. "HYPE", "BTC", "ETH"
    core_quote_perp: str | None  # None when quote is USD stablecoin; "HYPE" when quoted in WHYPE
    fee_bps: float  # pool swap fee in bps (e.g. 5.0 for 0.05%, 30.0 for 0.30%)


DEFAULT_HYPEREVM_POOLS: tuple[PoolSpec, ...] = (
    PoolSpec("PRJX V3", "WHYPE/USDC 5bp", "0x6c9a33e3b592c0d65b3ba59355d5be0d38259285", "WHYPE", "USDC", "HYPE", None, 5.0),
    PoolSpec("PRJX V3", "WHYPE/USDT0 5bp", "0xbd19e19e4b70eb7f248695a42208bc1edbbfb57d", "WHYPE", "USDT0", "HYPE", None, 5.0),
    PoolSpec("HyperSwap V3", "WHYPE/USDT0 5bp", "0x337b56d87a6185cd46af3ac2cdf03cbc37070c30", "WHYPE", "USDT0", "HYPE", None, 5.0),
    PoolSpec("HyperSwap V3", "WHYPE/USDC 30bp", "0xe712d505572b3f84c1b4deb99e1beab9dd0e23c9", "WHYPE", "USDC", "HYPE", None, 30.0),
    PoolSpec("PRJX V3", "UBTC/USDT0 5bp", "0xfd0cb41a4a28b5ca13affe65cd5924f461b3c850", "UBTC", "USDT0", "BTC", None, 5.0),
    PoolSpec("PRJX V3", "UBTC/WHYPE 30bp", "0x0d6ecb912b6ee160e95bc198b618acc1bcb92525", "UBTC", "WHYPE", "BTC", "HYPE", 30.0),
    PoolSpec("HyperSwap V3", "UBTC/WHYPE 30bp", "0x3a36b04bcc1d5e2e303981ef643d2668e00b43e7", "UBTC", "WHYPE", "BTC", "HYPE", 30.0),
    PoolSpec("PRJX V3", "UETH/WHYPE 30bp", "0xaf80230eb13222db743c21762f65a046bb5f5437", "UETH", "WHYPE", "ETH", "HYPE", 30.0),
    PoolSpec("HyperSwap V3", "UETH/WHYPE 30bp", "0x719d7f4388cb0efb6a48f3c3266e443edce6588a", "UETH", "WHYPE", "ETH", "HYPE", 30.0),
    PoolSpec("PRJX V3", "UETH/USDT0 30bp", "0xaeae69783e3121196a45f3930fa141f462a4df2f", "UETH", "USDT0", "ETH", None, 30.0),
)


@dataclass(frozen=True, slots=True)
class HyperEVMChainHealth:
    chain_id: int
    block_number: int
    gas_price_gwei: float
    rpc_latency_ms: float
    l1_block_number: int = 0
    usdt0_per_usdc: float = 1.0
    usdt0_peg_discount_bps: float = 0.0
    stable_peg_source: str = ""


@dataclass(frozen=True, slots=True)
class L1ReadOracleRow:
    coin: str
    asset_index: int
    l1read_mark_px: float
    l1read_oracle_px: float
    rest_mark_px: float
    drift_bps: float


@dataclass(frozen=True, slots=True)
class EvmPoolArbOpportunity:
    dex: str
    label: str
    pool_address: str
    evm_price: float
    core_ref_price: float
    gross_spread_bps: float  # raw (evm - core) / core * 1e4
    peg_adjusted_spread_bps: float  # after normalizing USDT0 -> USDC via on-chain 1bp pool
    pool_fee_bps: float
    core_fee_bps: float
    stable_leg_fee_bps: float
    net_arb_bps: float  # |peg_adjusted_spread_bps| - pool_fee - core_fee - stable_leg_fee
    direction: str
    active_liquidity_raw: int
    virtual_depth_usd: float
    slippage_5k_bps: float
    slippage_25k_bps: float
    slippage_100k_bps: float
    net_arb_25k_bps: float  # net_arb_bps - slippage_25k_bps
    executable: bool


@dataclass(frozen=True, slots=True)
class SpotPerpBasisRow:
    spot_token: str
    perp_coin: str
    spot_mid: float
    perp_mark: float
    basis_bps: float
    funding_hourly_bps: float
    funding_apr_pct: float
    spot_day_vol_usd: float
    perp_oi_usd: float
    strategy_hint: str


@dataclass(frozen=True, slots=True)
class VaultIntelRow:
    name: str
    address: str
    tvl_usd: float
    apr_pct: float
    open_positions_count: int
    total_notional_usd: float
    margin_utilization_pct: float


@dataclass(frozen=True, slots=True)
class FundingExtremumRow:
    coin: str
    mark_px: float
    funding_hourly_bps: float
    funding_apr_pct: float
    open_interest_usd: float
    day_vol_usd: float
    premium_bps: float


@dataclass(frozen=True, slots=True)
class HyperEVMDefiSnapshot:
    observed_at_ms: int
    chain: HyperEVMChainHealth
    l1_read_oracles: list[L1ReadOracleRow]
    evm_arb: list[EvmPoolArbOpportunity]
    spot_perp_basis: list[SpotPerpBasisRow]
    vaults: list[VaultIntelRow]
    extreme_funding: list[FundingExtremumRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at_ms": self.observed_at_ms,
            "chain": asdict(self.chain),
            "l1_read_oracles": [asdict(x) for x in self.l1_read_oracles],
            "evm_arb": [asdict(x) for x in self.evm_arb],
            "spot_perp_basis": [asdict(x) for x in self.spot_perp_basis],
            "vaults": [asdict(x) for x in self.vaults],
            "extreme_funding": [asdict(x) for x in self.extreme_funding],
        }


# --------------------------------------------------------------------------- Pure EVM ABI & V3 Depth math


def decode_v3_price_from_sqrt_x96(
    sqrt_price_x96: int,
    *,
    dec0: int,
    dec1: int,
    invert: bool = False,
) -> float:
    """Decode human-readable spot price from Uniswap V3 ``sqrtPriceX96``."""
    if sqrt_price_x96 <= 0:
        return 0.0
    ratio = float(sqrt_price_x96) / float(1 << 96)
    p_1_per_0 = (ratio * ratio) * (10.0 ** (int(dec0) - int(dec1)))
    if p_1_per_0 <= 0 or not math.isfinite(p_1_per_0):
        return 0.0
    return (1.0 / p_1_per_0) if invert else p_1_per_0


def compute_v3_virtual_depth_and_slippage(
    liquidity_raw: int,
    sqrt_price_x96: int,
    *,
    dec0: int,
    token0_usd: float,
    clips_usd: tuple[float, ...] = (5_000.0, 25_000.0, 100_000.0),
) -> tuple[float, dict[float, float]]:
    """Compute concentrated-liquidity active-tick virtual depth (USD) and exact average slippage (bps).

    In Uniswap V3, virtual token0 reserve inside the active tick is ``x_virt = L / sqrtP``
    (in raw units), so one-side virtual USD depth is ``V_virt = (L / sqrtP) / 10^dec0 * p0_usd``.
    Average execution price impact for a clip ``dV`` inside the tick is ``dV / V_virt * 1e4`` bps.
    """
    if liquidity_raw <= 0 or sqrt_price_x96 <= 0 or token0_usd <= 0:
        return 0.0, {c: 9999.0 for c in clips_usd}
    sqrt_p = float(sqrt_price_x96) / float(1 << 96)
    if sqrt_p <= 0:
        return 0.0, {c: 9999.0 for c in clips_usd}
    x_virt_human = (float(liquidity_raw) / sqrt_p) / (10.0 ** int(dec0))
    v_depth_usd = x_virt_human * token0_usd
    if v_depth_usd <= 0 or not math.isfinite(v_depth_usd):
        return 0.0, {c: 9999.0 for c in clips_usd}
    impacts = {c: round(min((c / v_depth_usd) * 1e4, 9999.0), 2) for c in clips_usd}
    return round(v_depth_usd, 2), impacts


def rpc_call(client: httpx.Client, method: str, params: list[Any], *, rpc_url: str = HYPEREVM_RPC_URL) -> Any:
    resp = client.post(rpc_url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    resp.raise_for_status()
    payload = resp.json() or {}
    if "error" in payload and payload["error"]:
        raise RuntimeError(f"HyperEVM RPC error ({method}): {payload['error']}")
    return payload.get("result")


def eth_call(client: httpx.Client, to: str, data: str, *, rpc_url: str = HYPEREVM_RPC_URL) -> str:
    res = rpc_call(client, "eth_call", [{"to": to, "data": data}, "latest"], rpc_url=rpc_url)
    return str(res or "0x")


def fetch_stable_peg_usdt0_per_usdc(
    client: httpx.Client,
    *,
    rpc_url: str = HYPEREVM_RPC_URL,
) -> tuple[float, float, str]:
    """Read live on-chain ``USDT0 per 1 USDC`` rate from HyperEVM 1bp stable pools."""
    usdc_addr = HYPEREVM_TOKENS["USDC"][0].lower()
    for name, pool_addr, fee_bps in STABLE_PEG_POOLS:
        try:
            s0 = eth_call(client, pool_addr, SEL_SLOT0, rpc_url=rpc_url)
            liq_h = eth_call(client, pool_addr, SEL_LIQUIDITY, rpc_url=rpc_url)
            t0_h = eth_call(client, pool_addr, SEL_TOKEN0, rpc_url=rpc_url)
            if not s0 or len(s0) < 66 or int(liq_h or "0x0", 16) <= 0:
                continue
            sq = int(s0[2:66], 16)
            t0 = ("0x" + t0_h[-40:]).lower()
            invert = t0 != usdc_addr
            px = decode_v3_price_from_sqrt_x96(sq, dec0=6, dec1=6, invert=invert)
            if 0.95 <= px <= 1.05:
                return px, fee_bps, name
        except Exception:
            continue
    return 1.0, 1.0, "fallback_1.0"


def fetch_hyperevm_chain_health(client: httpx.Client, *, rpc_url: str = HYPEREVM_RPC_URL) -> HyperEVMChainHealth:
    t0 = datetime.now(tz=timezone.utc).timestamp()
    chain_hex = rpc_call(client, "eth_chainId", [], rpc_url=rpc_url)
    block_hex = rpc_call(client, "eth_blockNumber", [], rpc_url=rpc_url)
    gas_hex = rpc_call(client, "eth_gasPrice", [], rpc_url=rpc_url)
    dt_ms = (datetime.now(tz=timezone.utc).timestamp() - t0) * 1000.0

    l1_block = 0
    try:
        l1_h = eth_call(client, PRECOMPILE_L1_BLOCK, "0x", rpc_url=rpc_url)
        if l1_h and l1_h != "0x":
            l1_block = int(l1_h, 16)
    except Exception:
        l1_block = 0

    usdt0_per_usdc, _, peg_src = fetch_stable_peg_usdt0_per_usdc(client, rpc_url=rpc_url)
    peg_disc_bps = (usdt0_per_usdc - 1.0) * 1e4

    return HyperEVMChainHealth(
        chain_id=int(str(chain_hex), 16),
        block_number=int(str(block_hex), 16),
        gas_price_gwei=round(int(str(gas_hex), 16) / 1e9, 4),
        rpc_latency_ms=round(dt_ms, 1),
        l1_block_number=l1_block,
        usdt0_per_usdc=round(usdt0_per_usdc, 6),
        usdt0_peg_discount_bps=round(peg_disc_bps, 2),
        stable_peg_source=peg_src,
    )


def fetch_hyperevm_l1_read_precompiles(
    client: httpx.Client,
    perps: dict[str, dict[str, Any]],
    *,
    coins: tuple[str, ...] = ("BTC", "ETH", "SOL", "HYPE"),
    rpc_url: str = HYPEREVM_RPC_URL,
) -> list[L1ReadOracleRow]:
    """Read HyperCore ``markPx`` (0x0806) & ``oraclePx`` (0x0807) directly via HyperEVM L1Read precompiles."""
    rows: list[L1ReadOracleRow] = []
    for coin in coins:
        p = perps.get(coin)
        if not p or "asset_index" not in p:
            continue
        idx = int(p["asset_index"])
        sz_dec = int(p.get("sz_decimals", 2))
        scale = 10.0 ** max(0, 6 - sz_dec)
        arg = "0x" + hex(idx)[2:].rjust(64, "0")
        try:
            mk_h = eth_call(client, PRECOMPILE_MARK_PX, arg, rpc_url=rpc_url)
            or_h = eth_call(client, PRECOMPILE_ORACLE_PX, arg, rpc_url=rpc_url)
            mk_raw = int(mk_h, 16) if mk_h and mk_h != "0x" else 0
            or_raw = int(or_h, 16) if or_h and or_h != "0x" else 0
            if mk_raw <= 0:
                continue
            l1_mark = mk_raw / scale
            l1_oracle = or_raw / scale if or_raw > 0 else l1_mark
            rest_mark = float(p["mark_px"])
            drift = (l1_mark - rest_mark) / rest_mark * 1e4 if rest_mark > 0 else 0.0
            rows.append(
                L1ReadOracleRow(
                    coin=coin,
                    asset_index=idx,
                    l1read_mark_px=round(l1_mark, 5),
                    l1read_oracle_px=round(l1_oracle, 5),
                    rest_mark_px=round(rest_mark, 5),
                    drift_bps=round(drift, 2),
                )
            )
        except Exception:
            continue
    return rows


def read_v3_pool_state(
    client: httpx.Client,
    pool: PoolSpec,
    *,
    rpc_url: str = HYPEREVM_RPC_URL,
) -> dict[str, Any] | None:
    """Read live on-chain price, liquidity, sqrtPriceX96, and token ordering from a HyperEVM V3 pool."""
    slot0_hex = eth_call(client, pool.pool_address, SEL_SLOT0, rpc_url=rpc_url)
    if not slot0_hex or len(slot0_hex) < 66:
        return None
    sqrt_x96 = int(slot0_hex[2:66], 16)
    if sqrt_x96 <= 0:
        return None

    liq_hex = eth_call(client, pool.pool_address, SEL_LIQUIDITY, rpc_url=rpc_url)
    liq = int(liq_hex, 16) if liq_hex and liq_hex != "0x" else 0

    t0_hex = eth_call(client, pool.pool_address, SEL_TOKEN0, rpc_url=rpc_url)
    t1_hex = eth_call(client, pool.pool_address, SEL_TOKEN1, rpc_url=rpc_url)
    t0_addr = ("0x" + t0_hex[-40:]).lower() if len(t0_hex) >= 42 else ""
    t1_addr = ("0x" + t1_hex[-40:]).lower() if len(t1_hex) >= 42 else ""

    base_addr, base_dec = HYPEREVM_TOKENS[pool.base_symbol]
    quote_addr, quote_dec = HYPEREVM_TOKENS[pool.quote_symbol]
    if t0_addr == base_addr.lower() and t1_addr == quote_addr.lower():
        price = decode_v3_price_from_sqrt_x96(sqrt_x96, dec0=base_dec, dec1=quote_dec, invert=False)
        t0_is_base, dec0 = True, base_dec
    elif t0_addr == quote_addr.lower() and t1_addr == base_addr.lower():
        price = decode_v3_price_from_sqrt_x96(sqrt_x96, dec0=quote_dec, dec1=base_dec, invert=True)
        t0_is_base, dec0 = False, quote_dec
    else:
        d0_hex = eth_call(client, t0_addr, SEL_DECIMALS, rpc_url=rpc_url)
        d1_hex = eth_call(client, t1_addr, SEL_DECIMALS, rpc_url=rpc_url)
        dec0 = int(d0_hex, 16) if d0_hex and d0_hex != "0x" else 18
        dec1 = int(d1_hex, 16) if d1_hex and d1_hex != "0x" else 18
        t0_is_base = t0_addr == base_addr.lower()
        price = decode_v3_price_from_sqrt_x96(sqrt_x96, dec0=dec0, dec1=dec1, invert=not t0_is_base)

    if price <= 0:
        return None
    return {"price": price, "liquidity": liq, "sqrt_x96": sqrt_x96, "t0_is_base": t0_is_base, "dec0": dec0}


def read_v3_pool_price(client: httpx.Client, pool: PoolSpec, *, rpc_url: str = HYPEREVM_RPC_URL) -> tuple[float, int] | None:
    """Backwards-compatible wrapper returning ``(base_in_quote_price, active_liquidity)``."""
    st = read_v3_pool_state(client, pool, rpc_url=rpc_url)
    return (float(st["price"]), int(st["liquidity"])) if st else None


# --------------------------------------------------------------------------- HyperCore + Cross-Layer Scans


def _f(v: object, default: float = 0.0) -> float:
    try:
        x = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return x if math.isfinite(x) else default


def fetch_hypercore_markets(client: httpx.Client, *, info_url: str = HYPERCORE_INFO_URL) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Fetch HyperCore perpetuals (``metaAndAssetCtxs``) and USDC spot markets (``spotMetaAndAssetCtxs``)."""
    perp_raw = client.post(info_url, json={"type": "metaAndAssetCtxs"})
    perp_raw.raise_for_status()
    p_meta, p_ctxs = perp_raw.json()
    perps: dict[str, dict[str, Any]] = {}
    for idx, (u, ctx) in enumerate(zip(p_meta.get("universe") or [], p_ctxs or [], strict=False)):
        coin = str(u.get("name") or "").strip()
        if not coin:
            continue
        mark = _f(ctx.get("markPx"))
        mid = _f(ctx.get("midPx"), mark)
        oracle = _f(ctx.get("oraclePx"), mark)
        oi = _f(ctx.get("openInterest")) * mark
        perps[coin] = {
            "coin": coin,
            "asset_index": idx,
            "sz_decimals": int(u.get("szDecimals", 2)),
            "mark_px": mark,
            "mid_px": mid,
            "oracle_px": oracle,
            "funding_hourly": _f(ctx.get("funding")),
            "premium": _f(ctx.get("premium")),
            "oi_usd": oi,
            "day_vol_usd": _f(ctx.get("dayNtlVlm")),
        }

    spot_raw = client.post(info_url, json={"type": "spotMetaAndAssetCtxs"})
    spot_raw.raise_for_status()
    s_meta, s_ctxs = spot_raw.json()
    tok_by_idx = {int(t["index"]): t for t in (s_meta.get("tokens") or [])}
    ctx_by_coin = {str(c.get("coin")): c for c in (s_ctxs or [])}

    spots: dict[str, dict[str, Any]] = {}
    for u in s_meta.get("universe") or []:
        ctx = ctx_by_coin.get(str(u.get("name")))
        toks = u.get("tokens") or []
        if not ctx or len(toks) < 2:
            continue
        t0 = tok_by_idx.get(int(toks[0]), {})
        t1 = tok_by_idx.get(int(toks[1]), {})
        if str(t1.get("name")) != "USDC":
            continue
        sym = str(t0.get("name") or "").strip()
        mid = _f(ctx.get("midPx"), _f(ctx.get("markPx")))
        vol = _f(ctx.get("dayNtlVlm"))
        if sym and mid > 0 and (sym not in spots or vol > spots[sym]["day_vol_usd"]):
            spots[sym] = {"token": sym, "pair": str(u.get("name")), "mid_px": mid, "day_vol_usd": vol}
    return perps, spots


def compute_spot_perp_basis(perps: dict[str, dict[str, Any]], spots: dict[str, dict[str, Any]]) -> list[SpotPerpBasisRow]:
    """Match HyperCore spot markets to their perpetuals and rank Cash-and-Carry opportunities."""
    out: list[SpotPerpBasisRow] = []
    for sp_sym, sp in spots.items():
        perp_coin = SPOT_TO_PERP_ALIAS.get(sp_sym, sp_sym)
        if perp_coin not in perps:
            continue
        p = perps[perp_coin]
        spot_mid = float(sp["mid_px"])
        perp_mark = float(p["mark_px"])
        if spot_mid <= 0 or perp_mark <= 0:
            continue
        if not (0.5 <= perp_mark / spot_mid <= 2.0):
            continue
        basis_bps = (perp_mark - spot_mid) / spot_mid * 1e4
        fund_h_bps = float(p["funding_hourly"]) * 1e4
        fund_apr = float(p["funding_hourly"]) * 24.0 * 365.0 * 100.0
        if fund_apr >= 12.0:
            hint = "Cash&Carry (Лонг Spot + Шорт Perp)"
        elif fund_apr <= -12.0:
            hint = "Reverse Carry (Лонг Perp при наличии займа Spot)"
        else:
            hint = "Нейтрально"
        out.append(
            SpotPerpBasisRow(
                spot_token=sp_sym,
                perp_coin=perp_coin,
                spot_mid=spot_mid,
                perp_mark=perp_mark,
                basis_bps=round(basis_bps, 2),
                funding_hourly_bps=round(fund_h_bps, 3),
                funding_apr_pct=round(fund_apr, 2),
                spot_day_vol_usd=round(float(sp["day_vol_usd"]), 2),
                perp_oi_usd=round(float(p["oi_usd"]), 2),
                strategy_hint=hint,
            )
        )
    out.sort(key=lambda r: (-r.spot_day_vol_usd, -abs(r.funding_apr_pct)))
    return out


def scan_hyperevm_vs_hypercore_arb(
    client: httpx.Client,
    perps: dict[str, dict[str, Any]],
    spots: dict[str, dict[str, Any]],
    *,
    pools: tuple[PoolSpec, ...] = DEFAULT_HYPEREVM_POOLS,
    core_fee_bps: float = 4.5,
    usdt0_per_usdc: float = 1.0,
    stable_leg_fee_bps: float = 1.0,
    rpc_url: str = HYPEREVM_RPC_URL,
) -> list[EvmPoolArbOpportunity]:
    """Compare live HyperEVM V3 pool prices against HyperCore with peg normalization & V3 slippage."""

    def _core_usd(perp_coin: str) -> float:
        for sp_k, p_k in SPOT_TO_PERP_ALIAS.items():
            if p_k == perp_coin and sp_k in spots and spots[sp_k]["day_vol_usd"] > 100_000:
                return float(spots[sp_k]["mid_px"])
        return float(perps.get(perp_coin, {}).get("mid_px", 0.0))

    results: list[EvmPoolArbOpportunity] = []
    for pool in pools:
        base_usd = _core_usd(pool.core_base_perp)
        if base_usd <= 0:
            continue
        if pool.core_quote_perp is None:
            core_ref = base_usd
            quote_usd = 1.0
        else:
            quote_usd = _core_usd(pool.core_quote_perp)
            if quote_usd <= 0:
                continue
            core_ref = base_usd / quote_usd

        try:
            st = read_v3_pool_state(client, pool, rpc_url=rpc_url)
        except Exception:
            continue
        if st is None:
            continue
        evm_px = float(st["price"])
        liq = int(st["liquidity"])
        if evm_px <= 0 or not (0.5 <= evm_px / core_ref <= 2.0):
            continue

        gross_bps = (evm_px - core_ref) / core_ref * 1e4
        # Normalize USDT0-quoted pools into true USDC via live on-chain 1bp stable pool
        if pool.quote_symbol == "USDT0":
            evm_px_usdc = evm_px / max(usdt0_per_usdc, 0.9)
            peg_adj_bps = (evm_px_usdc - core_ref) / core_ref * 1e4
            extra_stable_fee = stable_leg_fee_bps
        else:
            peg_adj_bps = gross_bps
            extra_stable_fee = 0.0

        token0_usd = base_usd if st["t0_is_base"] else quote_usd
        v_depth_usd, slips = compute_v3_virtual_depth_and_slippage(
            liq,
            int(st["sqrt_x96"]),
            dec0=int(st["dec0"]),
            token0_usd=token0_usd,
        )
        net_bps = abs(peg_adj_bps) - pool.fee_bps - core_fee_bps - extra_stable_fee
        net_25k = net_bps - slips[25_000.0]
        direction = "Buy Core → Sell EVM" if peg_adj_bps > 0 else "Buy EVM → Sell Core"

        results.append(
            EvmPoolArbOpportunity(
                dex=pool.dex,
                label=pool.label,
                pool_address=pool.pool_address,
                evm_price=round(evm_px, 6),
                core_ref_price=round(core_ref, 6),
                gross_spread_bps=round(gross_bps, 2),
                peg_adjusted_spread_bps=round(peg_adj_bps, 2),
                pool_fee_bps=pool.fee_bps,
                core_fee_bps=core_fee_bps,
                stable_leg_fee_bps=extra_stable_fee,
                net_arb_bps=round(net_bps, 2),
                direction=direction,
                active_liquidity_raw=liq,
                virtual_depth_usd=v_depth_usd,
                slippage_5k_bps=slips[5_000.0],
                slippage_25k_bps=slips[25_000.0],
                slippage_100k_bps=slips[100_000.0],
                net_arb_25k_bps=round(net_25k, 2),
                executable=net_bps > 0.0 and liq > 0,
            )
        )
    results.sort(key=lambda r: -r.net_arb_bps)
    return results


def fetch_hl_vault_intelligence(
    client: httpx.Client,
    *,
    vault_address: str = HLP_VAULT_ADDRESS,
    info_url: str = HYPERCORE_INFO_URL,
) -> list[VaultIntelRow]:
    """Read HLP parent vault and its child strategy / liquidator vaults (TVL, APR, live exposure)."""
    r = client.post(info_url, json={"type": "vaultDetails", "vaultAddress": vault_address})
    r.raise_for_status()
    vd = r.json() or {}
    if not isinstance(vd, dict) or not vd.get("name"):
        return []

    def _extract_tvl(detail: dict[str, Any]) -> float:
        for p_name, p_data in detail.get("portfolio") or []:
            if p_name == "allTime":
                hist = (p_data or {}).get("accountValueHistory") or []
                if hist:
                    return _f(hist[-1][1])
        return 0.0

    def _fetch_ch_stats(addr: str) -> tuple[int, float, float]:
        cr = client.post(info_url, json={"type": "clearinghouseState", "user": addr})
        if cr.status_code != 200:
            return 0, 0.0, 0.0
        ch = cr.json() or {}
        ms = ch.get("marginSummary") or {}
        eq = _f(ms.get("accountValue"))
        ntl = _f(ms.get("totalNtlPos"))
        m_used = _f(ms.get("totalMarginUsed"))
        pos_cnt = sum(1 for a in (ch.get("assetPositions") or []) if abs(_f((a.get("position") or {}).get("szi"))) > 1e-12)
        util = (m_used / eq * 100.0) if eq > 0 else 0.0
        return pos_cnt, ntl, util

    rows: list[VaultIntelRow] = []
    pos_cnt, ntl, util = _fetch_ch_stats(vault_address)
    rows.append(
        VaultIntelRow(
            name=str(vd.get("name")),
            address=vault_address,
            tvl_usd=round(_extract_tvl(vd), 2),
            apr_pct=round(_f(vd.get("apr")) * 100.0, 2),
            open_positions_count=pos_cnt,
            total_notional_usd=round(ntl, 2),
            margin_utilization_pct=round(util, 2),
        )
    )

    rel = vd.get("relationship") or {}
    children = ((rel.get("data") or {}).get("childAddresses") or []) if isinstance(rel, dict) else []
    for c_addr in children[:6]:
        try:
            cd_r = client.post(info_url, json={"type": "vaultDetails", "vaultAddress": c_addr})
            if cd_r.status_code != 200:
                continue
            cd = cd_r.json() or {}
            c_pos, c_ntl, c_util = _fetch_ch_stats(str(c_addr))
            rows.append(
                VaultIntelRow(
                    name=str(cd.get("name") or c_addr[:10]),
                    address=str(c_addr),
                    tvl_usd=round(_extract_tvl(cd), 2),
                    apr_pct=round(_f(cd.get("apr")) * 100.0, 2),
                    open_positions_count=c_pos,
                    total_notional_usd=round(c_ntl, 2),
                    margin_utilization_pct=round(c_util, 2),
                )
            )
        except Exception:
            continue
    return rows


def extract_extreme_funding_markets(perps: dict[str, dict[str, Any]], *, min_oi_usd: float = 2_000_000.0, top_n: int = 8) -> list[FundingExtremumRow]:
    """Identify liquid HyperCore perpetuals with the strongest positive/negative funding dislocations."""
    rows: list[FundingExtremumRow] = []
    for p in perps.values():
        if float(p["oi_usd"]) < min_oi_usd:
            continue
        f_h = float(p["funding_hourly"])
        rows.append(
            FundingExtremumRow(
                coin=str(p["coin"]),
                mark_px=float(p["mark_px"]),
                funding_hourly_bps=round(f_h * 1e4, 3),
                funding_apr_pct=round(f_h * 24.0 * 365.0 * 100.0, 1),
                open_interest_usd=round(float(p["oi_usd"]), 0),
                day_vol_usd=round(float(p["day_vol_usd"]), 0),
                premium_bps=round(float(p["premium"]) * 1e4, 2),
            )
        )
    rows.sort(key=lambda r: -abs(r.funding_apr_pct))
    return rows[:top_n]


def run_hyperevm_defi_scan(
    client: httpx.Client | None = None,
    *,
    rpc_url: str = HYPEREVM_RPC_URL,
    info_url: str = HYPERCORE_INFO_URL,
) -> HyperEVMDefiSnapshot:
    owns = client is None
    http = client or httpx.Client(timeout=20.0)
    try:
        chain = fetch_hyperevm_chain_health(http, rpc_url=rpc_url)
        perps, spots = fetch_hypercore_markets(http, info_url=info_url)
        l1_oracles = fetch_hyperevm_l1_read_precompiles(http, perps, rpc_url=rpc_url)
        arb = scan_hyperevm_vs_hypercore_arb(
            http,
            perps,
            spots,
            usdt0_per_usdc=chain.usdt0_per_usdc,
            rpc_url=rpc_url,
        )
        basis = compute_spot_perp_basis(perps, spots)
        vaults = fetch_hl_vault_intelligence(http, info_url=info_url)
        ext = extract_extreme_funding_markets(perps)
        return HyperEVMDefiSnapshot(
            observed_at_ms=int(datetime.now(tz=timezone.utc).timestamp() * 1000),
            chain=chain,
            l1_read_oracles=l1_oracles,
            evm_arb=arb,
            spot_perp_basis=basis,
            vaults=vaults,
            extreme_funding=ext,
        )
    finally:
        if owns:
            http.close()


# --------------------------------------------------------------------------- Reporting


def format_hyperevm_defi_markdown(snap: HyperEVMDefiSnapshot) -> str:
    ts = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        f"# HyperEVM (Chain {snap.chain.chain_id}) & HyperCore On-Chain DeFi Скан v2 ({ts})",
        "",
        f"- **HyperEVM RPC:** блок `#{snap.chain.block_number:,}` · HyperCore L1 блок `#{snap.chain.l1_block_number:,}` · газ `{snap.chain.gas_price_gwei:.4f} Gwei` · задержка `{snap.chain.rpc_latency_ms:.0f} мс`",
        f"- **Ончейн-пег стейблов (`{snap.chain.stable_peg_source}`):** `1 USDC = {snap.chain.usdt0_per_usdc:.6f} USDT0` (дисконт USDT0: `{snap.chain.usdt0_peg_discount_bps:+.2f} б.п.`)",
    ]

    if snap.l1_read_oracles:
        lines += [
            "",
            "## 0. Нативные прекомпилы HyperEVM `L1Read` (`0x0806` markPx / `0x0807` oraclePx)",
            "",
            "| Монета | Asset Idx | `L1Read` Mark | `L1Read` Oracle | REST Mark | Дрейф |",
            "|---|---:|---:|---:|---:|---:|",
        ]
        for o in snap.l1_read_oracles:
            lines.append(
                f"| **{o.coin}** | {o.asset_index} | {o.l1read_mark_px:,.5g} | {o.l1read_oracle_px:,.5g} | "
                f"{o.rest_mark_px:,.5g} | {o.drift_bps:+.2f} б.п. |"
            )

    lines += [
        "",
        "## 1. Истинный (Peg-Adjusted) Арбитраж HyperCore ↔ HyperEVM + V3-глубина",
        "",
        "| DEX | Пул | Сырой спред | Peg-Adj спред | Комиссии | Net (0-clip) | Вирт. глубина | Slip $25k | Net ($25k) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for a in snap.evm_arb:
        flag = "🟢" if a.executable else "⚪"
        tot_fee = a.pool_fee_bps + a.core_fee_bps + a.stable_leg_fee_bps
        lines.append(
            f"| {a.dex} | **{a.label}** | {a.gross_spread_bps:+.2f} | {a.peg_adjusted_spread_bps:+.2f} | "
            f"{tot_fee:.1f} б.п. | {flag} **{a.net_arb_bps:+.2f} б.п.** | ${a.virtual_depth_usd/1e6:,.2f}M | "
            f"{a.slippage_25k_bps:.1f} б.п. | {a.net_arb_25k_bps:+.2f} б.п. |"
        )

    lines += [
        "",
        "## 2. HyperCore Spot ↔ Perp Cash-and-Carry базис",
        "",
        "| Spot | Perp | Spot Mid | Perp Mark | Базис (б.п.) | Фандинг APR | Объём Spot 24ч | OI Perp | Сигнал |",
        "|---|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    for b in snap.spot_perp_basis[:10]:
        lines.append(
            f"| **{b.spot_token}** | {b.perp_coin} | {b.spot_mid:,.5g} | {b.perp_mark:,.5g} | {b.basis_bps:+.2f} | "
            f"**{b.funding_apr_pct:+.1f}%** | ${b.spot_day_vol_usd/1e6:,.2f}M | ${b.perp_oi_usd/1e6:,.1f}M | {b.strategy_hint} |"
        )

    lines += [
        "",
        "## 3. HLP и дочерние стратегии / ликвидатор-волты",
        "",
        "| Волт | TVL ($) | APR | Позиций | Номинал ($) | Загрузка маржи |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for v in snap.vaults:
        lines.append(
            f"| **{v.name}** | ${v.tvl_usd/1e6:,.2f}M | **{v.apr_pct:+.2f}%** | {v.open_positions_count} | "
            f"${v.total_notional_usd/1e6:,.2f}M | {v.margin_utilization_pct:.1f}% |"
        )

    lines += [
        "",
        "## 4. Экстремальный фандинг и перекосы OI на HyperCore (OI ≥ $2M)",
        "",
        "| Монета | Цена | Фандинг / ч | Фандинг APR | Премия (б.п.) | OI ($) | Объём 24ч ($) |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for e in snap.extreme_funding:
        lines.append(
            f"| **{e.coin}** | {e.mark_px:,.5g} | {e.funding_hourly_bps:+.3f} б.п. | **{e.funding_apr_pct:+.1f}%** | "
            f"{e.premium_bps:+.1f} | ${e.open_interest_usd/1e6:,.1f}M | ${e.day_vol_usd/1e6:,.1f}M |"
        )
    return "\n".join(lines) + "\n"


def format_hyperevm_defi_telegram_rich_html(snap: HyperEVMDefiSnapshot) -> str:
    """Plain-Russian Telegram Rich HTML summary (``sendRichMessage``) for HyperEVM v2 + HyperCore DeFi."""
    dt = datetime.fromtimestamp(snap.observed_at_ms / 1000, tz=timezone.utc)
    date_str = dt.strftime("%d.%m.%Y")
    time_str = dt.strftime("%H:%M UTC")
    hlp = snap.vaults[0] if snap.vaults else None
    hlp_txt = f"HLP TVL <b>${hlp.tvl_usd/1e6:,.1f}M</b> · APR <b>{hlp.apr_pct:+.1f}%</b>" if hlp else "HLP: —"
    exec_cnt = sum(1 for a in snap.evm_arb if a.executable)

    parts = [
        f"<h1>⛓️ HyperEVM v2 &amp; HyperCore DeFi · {html.escape(date_str)}</h1>",
        (
            f"<p><b>Сеть ({html.escape(time_str)}):</b> Chain {snap.chain.chain_id} · EVM #{snap.chain.block_number:,} · "
            f"L1 #{snap.chain.l1_block_number:,} · газ {snap.chain.gas_price_gwei:.3f} Gwei<br>"
            f"<b>Пег USDT0/USDC:</b> {snap.chain.usdt0_per_usdc:.5f} ({snap.chain.usdt0_peg_discount_bps:+.1f} б.п.) · "
            f"{hlp_txt} · чистых арб-сигналов: <b>{exec_cnt}</b></p>"
        ),
    ]

    if snap.evm_arb:
        rows = [
            "<tr>"
            '<th align="left">Пул HyperEVM</th>'
            '<th align="right">Peg-Adj</th>'
            '<th align="right">Чистый</th>'
            '<th align="right">Slip $25k</th>'
            "</tr>"
        ]
        for a in snap.evm_arb[:6]:
            dot = "🟢" if a.executable else "⚪"
            rows.append(
                "<tr>"
                f'<td align="left"><b>{html.escape(a.label)}</b> ({html.escape(a.dex)})</td>'
                f'<td align="right">{a.peg_adjusted_spread_bps:+.1f} б.п.</td>'
                f'<td align="right">{dot} {a.net_arb_bps:+.1f} б.п.</td>'
                f'<td align="right">{a.slippage_25k_bps:.1f} б.п.</td>'
                "</tr>"
            )
        parts.append('<table bordered striped>' + "".join(rows) + "</table>")

    parts.append("<footer>Read-only аудит: HyperEVM V3 (peg-adj + depth) + L1Read precompiles + HyperCore + HLP</footer>")
    return "".join(parts)
