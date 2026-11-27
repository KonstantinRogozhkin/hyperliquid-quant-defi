from __future__ import annotations

import json
from pathlib import Path

import pytest

from hl_quant import hl_post_trade_attribution as pta


def test_post_trade_attribution_math_alo_fill_rate_and_liq_warning(tmp_path: Path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        json.dumps(
            {
                "orders": [
                    {"response": {"response": {"data": {"statuses": [{"resting": {"oid": 101}}]}}}},
                    {"response": {"response": {"data": {"statuses": [{"resting": {"oid": 102}}]}}}},
                ]
            }
        )
        + "\n"
    )

    snapshot = {
        "account_address": "0xTEST",
        "observed_at_ms": 1_795_708_000_000,
        "fills": [
            {"coin": "BTC", "px": "60000", "sz": "0.01", "side": "B", "oid": 101, "crossed": False, "fee": "0.09", "closedPnl": "0.0"},
        ],
        "funding": [
            {"delta": {"coin": "BTC", "usdc": "0.25"}},
        ],
        "clearinghouse": {
            "marginSummary": {"accountValue": "100.0"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "BTC",
                        "szi": "0.01",
                        "entryPx": "60000",
                        "positionValue": "606.0",
                        "unrealizedPnl": "6.0",
                        "liquidationPx": "57000",  # ~5.9% away -> triggers <15% warning
                        "leverage": {"type": "isolated", "value": 10},
                    }
                }
            ],
        },
        "spot": {"balances": [{"coin": "USDC", "total": "900.0"}]},
        "open_orders": [{"coin": "ETH", "oid": 102}],
        "all_mids": {"BTC": 60600.0, "ETH": 2000.0},
    }

    attr = pta.compute_post_trade_attribution(snapshot, audit_log_path=audit_path)
    assert attr.equity_usd == pytest.approx(1000.0)
    assert attr.maker_volume_share_pct == pytest.approx(100.0)
    assert attr.effective_fee_bps == pytest.approx(1.5, abs=0.01)
    # 1 of 2 audited ALO orders filled -> 50.0%
    assert attr.real_alo_fill_rate_pct == pytest.approx(50.0)
    assert attr.audit_orders_resting == 1
    # Net = 0 realized + 6.0 unrealized + 0.25 funding - 0.09 fee = +6.16
    assert attr.net_attributed_pnl_usd == pytest.approx(6.16)
    # Buy at 60000, current mid 60600 -> +100 bps is directional DRIFT, not a markout
    assert attr.vw_mark_drift_bps == pytest.approx(100.0, abs=0.1)
    assert attr.vw_markout_bps is None  # no post-fill series supplied -> no fake markout
    assert len(attr.liq_warnings) == 1
    assert "BTC" in attr.liq_warnings[0]

    rich = pta.format_attribution_telegram_rich_html(attr)
    assert '<table bordered striped>' in rich
    assert "Атрибуция сделок Testnet" in rich


def test_unified_account_equity_avoids_double_counting_perp_margin_and_upnl():
    # Exact structure returned by Hyperliquid Unified Account (matches portfolio.accountValueHistory)
    snapshot = {
        "account_address": "0xUNIFIED",
        "observed_at_ms": 1_795_708_354_763,
        "user_abstraction": "unifiedAccount",
        "fills": [],
        "funding": [],
        "clearinghouse": {
            "marginSummary": {"accountValue": "61.232826", "totalMarginUsed": "51.456088"},
            "withdrawable": "0.0",
            "assetPositions": [
                {
                    "position": {
                        "coin": "ETH",
                        "szi": "-0.02",
                        "entryPx": "2500",
                        "positionValue": "50.0",
                        "unrealizedPnl": "-0.257488",
                        "liquidationPx": "15000",
                        "leverage": {"type": "cross", "value": 20},
                    }
                }
            ],
        },
        "spot": {"balances": [{"coin": "USDC", "total": "998.713879", "hold": "61.351791"}]},
        "open_orders": [],
        "all_mids": {"ETH": 2512.87},
    }
    attr = pta.compute_post_trade_attribution(snapshot)
    assert attr.equity_usd == pytest.approx(998.7139, abs=1e-4)
    # Heuristic fallback without user_abstraction yields identical result
    snapshot_no_ua = {**snapshot, "user_abstraction": ""}
    attr2 = pta.compute_post_trade_attribution(snapshot_no_ua)
    assert attr2.equity_usd == pytest.approx(998.7139, abs=1e-4)


def test_adl_rank_warning_on_profitable_leveraged_short_and_api_quota_health(tmp_path: Path):
    # High-leverage profitable short (+20% gain, 2.5x effective leverage) -> ADL Rank >= 3.0 -> triggers warning
    audit_path = tmp_path / "audit_spam.jsonl"
    # 25 placed orders with only $10 volume -> > 1 req/$ -> THROTTLE_RISK
    orders = [{"response": {"response": {"data": {"statuses": [{"resting": {"oid": 500 + i}}]}}}} for i in range(25)]
    audit_path.write_text(json.dumps({"orders": orders}) + "\n")
    snapshot = {
        "account_address": "0xADL",
        "observed_at_ms": 1_795_708_000_000,
        "fills": [{"coin": "SOL", "px": "100", "sz": "0.1", "side": "A", "oid": 500, "crossed": False, "fee": "0.001", "closedPnl": "0"}],
        "funding": [],
        "clearinghouse": {
            "marginSummary": {"accountValue": "1000.0"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "SOL",
                        "szi": "-25.0",
                        "entryPx": "125.0",
                        "positionValue": "2500.0",
                        "unrealizedPnl": "625.0",  # +25% on short hedge
                        "liquidationPx": "160.0",
                        "leverage": {"type": "cross", "value": 5},
                    }
                },
                {
                    "position": {
                        "coin": "ETH",
                        "szi": "0.1",
                        "entryPx": "3000.0",
                        "positionValue": "290.0",
                        "unrealizedPnl": "-10.0",  # underwater -> no ADL risk
                        "liquidationPx": "1000.0",
                        "leverage": {"type": "cross", "value": 3},
                    }
                },
            ],
        },
        "spot": {"balances": []},
        "open_orders": [],
        "all_mids": {"SOL": 100.0, "ETH": 2900.0},
    }
    attr = pta.compute_post_trade_attribution(snapshot, audit_log_path=audit_path)
    sol = next(c for c in attr.per_coin if c.coin == "SOL")
    eth = next(c for c in attr.per_coin if c.coin == "ETH")
    assert sol.adl_rank_score is not None and sol.adl_rank_score >= 3.0
    assert eth.adl_rank_score is None
    assert len(attr.adl_warnings) == 1 and "SOL SHORT" in attr.adl_warnings[0]
    assert "THROTTLE_RISK" in attr.api_quota_health
    md = pta.format_attribution_markdown(attr)
    rich = pta.format_attribution_telegram_rich_html(attr)
    assert "ADL-очереди" in md
    assert "Риск ADL" in rich


def test_isolated_adl_uses_isolated_margin_denominator_and_growth_mode_quota(tmp_path: Path):
    # Huge $100,000 cross account, BUT a small $1,000 isolated short with $100 isolated marginUsed (10x eff.lev!)
    # Must compute ADL Rank from isolated $100 margin (~11.1), NOT from $100,000 total equity (0.01)!
    audit_path = tmp_path / "audit_gm.jsonl"
    orders = [{"response": {"response": {"data": {"statuses": [{"resting": {"oid": 700 + i}}]}}}} for i in range(25)]
    audit_path.write_text(json.dumps({"orders": orders}) + "\n")
    snapshot = {
        "account_address": "0xISO_ADL",
        "observed_at_ms": 1_795_708_000_000,
        "fills": [{"coin": "NVDA", "px": "100", "sz": "1.0", "side": "A", "oid": 700, "crossed": False, "fee": "0.01", "closedPnl": "0"}],
        "funding": [],
        "clearinghouse": {
            "marginSummary": {"accountValue": "100000.0"},
            "assetPositions": [
                {
                    "position": {
                        "coin": "NVDA",
                        "szi": "-10.0",
                        "entryPx": "110.0",
                        "positionValue": "1000.0",
                        "marginUsed": "100.0",
                        "unrealizedPnl": "100.0",
                        "liquidationPx": "118.0",
                        "leverage": {"type": "isolated", "value": 10},
                    }
                }
            ],
        },
        "spot": {"balances": []},
        "open_orders": [],
        "all_mids": {"NVDA": 100.0},
    }
    # Standard mode: $100 vol / 25 reqs = 0.25 req/$ -> OK
    attr_std = pta.compute_post_trade_attribution(snapshot, audit_log_path=audit_path, is_growth_mode=False)
    nvda = attr_std.per_coin[0]
    assert nvda.adl_rank_score == pytest.approx(11.0, abs=0.05)
    assert len(attr_std.adl_warnings) == 1 and "isolated margin" in attr_std.adl_warnings[0]
    assert attr_std.api_quota_health == "OK"

    # Growth Mode (-90% quota weight): $100 vol counts as $10 -> 25 / 10 = 2.5 req/$ -> THROTTLE_RISK!
    attr_gm = pta.compute_post_trade_attribution(snapshot, audit_log_path=audit_path, is_growth_mode=True)
    assert "THROTTLE_RISK" in attr_gm.api_quota_health and "Growth 0.1x" in attr_gm.api_quota_health
