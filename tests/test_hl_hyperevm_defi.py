from __future__ import annotations

import json
import math

import httpx
import pytest

from hl_quant import hl_hyperevm_defi as defi


def test_decode_uniswap_v3_sqrt_price_x96_and_depth_slippage():
    # token0=WHYPE (18 dec), token1=USDT0 (6 dec), price = 60.0 USDT0 per WHYPE
    sqrt_x96 = int(math.sqrt(60.0 * 1e-12) * (1 << 96))
    px = defi.decode_v3_price_from_sqrt_x96(sqrt_x96, dec0=18, dec1=6, invert=False)
    assert px == pytest.approx(60.0, rel=1e-6)

    # Inverted: token0=USDT0 (6 dec), token1=WHYPE (18 dec)
    sqrt_inv = int(math.sqrt((1.0 / 60.0) * 1e12) * (1 << 96))
    px_inv = defi.decode_v3_price_from_sqrt_x96(sqrt_inv, dec0=6, dec1=18, invert=True)
    assert px_inv == pytest.approx(60.0, rel=1e-6)

    # V3 virtual depth check: choose L so x_virt_human * 60 = $10,000,000 -> $25k clip == 25.0 bps!
    sqrt_p = float(sqrt_x96) / float(1 << 96)
    target_x_human = 10_000_000.0 / 60.0
    liq_raw = int(target_x_human * (10**18) * sqrt_p)
    v_depth, slips = defi.compute_v3_virtual_depth_and_slippage(liq_raw, sqrt_x96, dec0=18, token0_usd=60.0)
    assert v_depth == pytest.approx(10_000_000.0, rel=1e-3)
    assert slips[5_000.0] == pytest.approx(5.0, abs=0.1)
    assert slips[25_000.0] == pytest.approx(25.0, abs=0.2)
    assert slips[100_000.0] == pytest.approx(100.0, abs=0.5)


def test_hyperevm_v2_peg_adjustment_eliminates_fake_usdt0_arb_and_reads_l1read():
    pool_usdt0 = defi.PoolSpec(
        dex="PRJX V3",
        label="WHYPE/USDT0 5bp",
        pool_address="0xbd19e19e4b70eb7f248695a42208bc1edbbfb57d",
        base_symbol="WHYPE",
        quote_symbol="USDT0",
        core_base_perp="HYPE",
        core_quote_perp=None,
        fee_bps=5.0,
    )
    # WHYPE/USDT0 raw price = 60.09 (+15 bps above Core 60.00), BUT USDT0 trades at +11 bps discount (1 USDC = 1.0011 USDT0)
    # -> peg-adjusted spread is only ~+4 bps < 10.5 bps fees -> NOT executable!
    sqrt_whype = int(math.sqrt(60.09 * 1e-12) * (1 << 96))
    slot0_whype = "0x" + hex(sqrt_whype)[2:].rjust(64, "0") + "0" * 64
    t0_whype = "0x" + defi.HYPEREVM_TOKENS["WHYPE"][0][2:].rjust(64, "0")
    t1_usdt0 = "0x" + defi.HYPEREVM_TOKENS["USDT0"][0][2:].rjust(64, "0")

    # Stable USDC/USDT0 pool: 1 USDC = 1.0011 USDT0
    sqrt_stable = int(math.sqrt(1.0011) * (1 << 96))
    slot0_stable = "0x" + hex(sqrt_stable)[2:].rjust(64, "0") + "0" * 64
    t0_usdc = "0x" + defi.HYPEREVM_TOKENS["USDC"][0][2:].rjust(64, "0")

    def rpc_handler(req: httpx.Request) -> httpx.Response:
        body = json.loads(req.content)
        m = body["method"]
        if m == "eth_chainId":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x3e7"})
        if m == "eth_blockNumber":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x100"})
        if m == "eth_gasPrice":
            return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x5f5e100"})
        if m == "eth_call":
            to = body["params"][0]["to"].lower()
            data = body["params"][0]["data"]
            if to == defi.PRECOMPILE_L1_BLOCK:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(1_235_670_000)})
            if to == defi.PRECOMPILE_MARK_PX:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(605_000)})
            if to == defi.PRECOMPILE_ORACLE_PX:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(605_100)})
            if to == defi.STABLE_PEG_POOLS[0][1].lower():
                if data == defi.SEL_SLOT0:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": slot0_stable})
                if data == defi.SEL_LIQUIDITY:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(10**14)})
                if data == defi.SEL_TOKEN0:
                    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": t0_usdc})
            if data == defi.SEL_SLOT0:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": slot0_whype})
            if data == defi.SEL_LIQUIDITY:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": hex(2 * 10**17)})
            if data == defi.SEL_TOKEN0:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": t0_whype})
            if data == defi.SEL_TOKEN1:
                return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": t1_usdt0})
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x0"})

    perps = {
        "BTC": {"coin": "BTC", "asset_index": 0, "sz_decimals": 5, "mark_px": 60500.0, "mid_px": 60500.0, "funding_hourly": 0.0, "premium": 0.0, "oi_usd": 1e9, "day_vol_usd": 1e9},
        "HYPE": {"coin": "HYPE", "asset_index": 159, "sz_decimals": 2, "mark_px": 60.03, "mid_px": 60.00, "funding_hourly": 0.00003, "premium": 0.0005, "oi_usd": 50_000_000.0, "day_vol_usd": 100_000_000.0},
    }
    spots = {"HYPE": {"token": "HYPE", "pair": "@107", "mid_px": 60.00, "day_vol_usd": 40_000_000.0}}

    with httpx.Client(transport=httpx.MockTransport(rpc_handler)) as client:
        health = defi.fetch_hyperevm_chain_health(client)
        l1 = defi.fetch_hyperevm_l1_read_precompiles(client, perps, coins=("BTC",))
        arb = defi.scan_hyperevm_vs_hypercore_arb(
            client, perps, spots, pools=(pool_usdt0,), core_fee_bps=4.5, usdt0_per_usdc=health.usdt0_per_usdc
        )

    assert health.chain_id == 999
    assert health.l1_block_number == 1_235_670_000
    assert health.usdt0_per_usdc == pytest.approx(1.0011, abs=1e-4)
    assert health.usdt0_peg_discount_bps == pytest.approx(11.0, abs=0.2)

    assert len(l1) == 1
    assert l1[0].l1read_mark_px == pytest.approx(60500.0)
    assert l1[0].l1read_oracle_px == pytest.approx(60510.0)
    assert l1[0].drift_bps == pytest.approx(0.0)

    # Raw spread is +15 bps, but peg-adjusted spread is only ~+4 bps -> NOT executable!
    assert arb[0].gross_spread_bps == pytest.approx(15.0, abs=0.2)
    assert arb[0].peg_adjusted_spread_bps == pytest.approx(4.0, abs=0.3)
    assert arb[0].executable is False
    assert arb[0].virtual_depth_usd > 0.0

    snap = defi.HyperEVMDefiSnapshot(
        observed_at_ms=1_795_710_000_000,
        chain=health,
        l1_read_oracles=l1,
        evm_arb=arb,
        spot_perp_basis=defi.compute_spot_perp_basis(perps, spots),
        vaults=[defi.VaultIntelRow("HLP", defi.HLP_VAULT_ADDRESS, 150_000_000.0, 15.6, 12, 25_000_000.0, 12.0)],
        extreme_funding=defi.extract_extreme_funding_markets(perps),
    )
    md = defi.format_hyperevm_defi_markdown(snap)
    rich = defi.format_hyperevm_defi_telegram_rich_html(snap)
    assert "L1Read" in md and "Peg-Adj" in md
    assert '<table bordered striped>' in rich and "Пег USDT0/USDC" in rich
