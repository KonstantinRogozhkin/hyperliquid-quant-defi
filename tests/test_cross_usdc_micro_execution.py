from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from hl_quant.cross_usdc_micro_execution import (
    BinanceClient,
    BybitClient,
    MicroExecutionError,
    MicroExecutionRunner,
    PreflightBlocked,
    _aggregate_fills,
    _binance_free_balance,
    _bybit_available_usdc,
    _hyperliquid_available_usdc,
    _hyperliquid_ioc_filled_quantity,
    _validate_hyperliquid_ioc_response,
    assert_sanitized,
    build_contract,
    confirmation_is_valid,
    contract_from_payload,
    contract_payload,
    hyperliquid_protected_limit,
    plan_entry_quantities,
    protected_limit,
    round_to_step,
    sanitized_bundle,
)


ROOT = Path(__file__).resolve().parents[1]


def contract():
    return build_contract(ROOT / "config/cross_usdc_micro_execution.yml", run_id="micro-test-run")


def test_contract_is_exact_and_rejects_excess_gross() -> None:
    frozen = contract()
    assert frozen.strategy_id == "cross_usdc_micro_execution_v2"
    assert frozen.spot_quantity_mode == "match_rounded_perps"
    assert frozen.per_asset_gross == Decimal("200")
    assert frozen.per_asset_gross * len(frozen.assets) == Decimal("400")
    with pytest.raises(MicroExecutionError, match="exceeds_max_batch_gross"):
        replace(frozen, max_batch_gross_usdc=Decimal("399")).validate()


def test_one_time_confirmation_must_byte_match_run_id() -> None:
    frozen = contract()
    assert confirmation_is_valid(frozen, "micro-test-run")
    assert not confirmation_is_valid(frozen, "MICRO-TEST-RUN")
    assert not confirmation_is_valid(frozen, None)


def test_frozen_contract_restores_the_original_hash() -> None:
    frozen = contract()
    restored = contract_from_payload(contract_payload(frozen))
    assert contract_payload(restored)["contract_sha256"] == contract_payload(frozen)["contract_sha256"]


def test_lot_rounding_and_five_bps_protection() -> None:
    assert round_to_step("1.239", "0.01") == Decimal("1.23")
    assert round_to_step("1.231", "0.01", up=True) == Decimal("1.24")
    assert protected_limit(Decimal("100"), side="buy", adverse_bps=Decimal("5"), tick=Decimal("0.01")) == Decimal("100.05")
    assert protected_limit(Decimal("100"), side="sell", adverse_bps=Decimal("5"), tick=Decimal("0.01")) == Decimal("99.95")
    assert protected_limit(Decimal("100.00005"), side="buy", adverse_bps=Decimal("5"), tick=Decimal("0.01")) <= Decimal("100.00005") * Decimal("1.0005")
    assert protected_limit(Decimal("100.00005"), side="sell", adverse_bps=Decimal("5"), tick=Decimal("0.01")) >= Decimal("100.00005") * Decimal("0.9995")


@pytest.mark.parametrize(
    ("best_price", "side", "sz_decimals"),
    [
        (Decimal("118123.4"), "buy", 5),
        (Decimal("118123.4"), "sell", 5),
        (Decimal("3812.34"), "buy", 4),
        (Decimal("3812.34"), "sell", 4),
    ],
)
def test_hyperliquid_limit_is_valid_and_never_weakens_protection(
    best_price: Decimal,
    side: str,
    sz_decimals: int,
) -> None:
    adverse_bps = Decimal("5")
    raw = protected_limit(best_price, side=side, adverse_bps=adverse_bps)
    rounded = hyperliquid_protected_limit(
        best_price,
        side=side,
        adverse_bps=adverse_bps,
        sz_decimals=sz_decimals,
    )
    normalized = rounded.normalize()
    decimal_places = max(0, -normalized.as_tuple().exponent)
    is_integer = rounded == rounded.to_integral_value()
    assert is_integer or len(normalized.as_tuple().digits) <= 5
    assert decimal_places <= 6 - sz_decimals
    assert rounded <= raw if side == "buy" else rounded >= raw


def test_hyperliquid_ioc_response_rejects_http_200_exchange_errors() -> None:
    filled = {
        "status": "ok",
        "response": {
            "type": "order",
            "data": {"statuses": [{"filled": {"totalSz": "0.001"}}]},
        },
    }
    _validate_hyperliquid_ioc_response(filled)
    assert _hyperliquid_ioc_filled_quantity(filled) == Decimal("0.001")
    with pytest.raises(MicroExecutionError, match="hyperliquid_tick_rejected"):
        _validate_hyperliquid_ioc_response(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {"error": "Price must be divisible by tick size."}
                        ]
                    },
                },
            }
        )
    with pytest.raises(MicroExecutionError, match="hyperliquid_ioc_cancelled"):
        _validate_hyperliquid_ioc_response(
            {
                "status": "ok",
                "response": {
                    "type": "order",
                    "data": {
                        "statuses": [
                            {
                                "error": (
                                    "Order could not immediately match against any "
                                    "resting orders."
                                )
                            }
                        ]
                    },
                },
            }
        )


@pytest.mark.parametrize(
    ("prices", "steps"),
    [
        (
            (Decimal("63767"), Decimal("63760"), Decimal("63755")),
            (Decimal("0.00001"), Decimal("0.001"), Decimal("0.00001")),
        ),
        (
            (Decimal("1919"), Decimal("1918"), Decimal("1917")),
            (Decimal("0.0001"), Decimal("0.01"), Decimal("0.001")),
        ),
    ],
)
def test_lot_aware_plan_matches_rounded_perps_without_weakening_gates(
    prices: tuple[Decimal, Decimal, Decimal],
    steps: tuple[Decimal, Decimal, Decimal],
) -> None:
    frozen = contract()
    plan = plan_entry_quantities(
        frozen,
        binance_price=prices[0],
        bybit_price=prices[1],
        hyperliquid_price=prices[2],
        binance_step=steps[0],
        bybit_step=steps[1],
        hyperliquid_step=steps[2],
    )
    assert plan["delta_fraction"] <= frozen.max_delta_mismatch_fraction
    assert plan["spot_target_coverage_fraction"] >= frozen.minimum_actual_spot_notional_fraction
    assert plan["bybit_short_weight_deviation_fraction"] <= frozen.max_bybit_short_weight_deviation_fraction
    assert plan["actual_gross_usdc"] <= frozen.per_asset_gross


def test_private_signatures_are_attached_without_persisting_secret() -> None:
    observed: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        observed.update({key.lower(): value for key, value in request.headers.items()})
        return httpx.Response(200, json={"retCode": 0, "result": {"permissions": ["ContractTrade"]}})

    client = BybitClient("public-key", "private-secret", client=httpx.Client(transport=httpx.MockTransport(responder)))
    assert client.query_api()["retCode"] == 0
    assert observed["x-bapi-api-key"] == "public-key"
    assert len(observed["x-bapi-sign"]) == 64
    assert "private-secret" not in observed["x-bapi-sign"]


def test_binance_signed_get_attaches_signature() -> None:
    observed: dict[str, str] = {}

    def responder(request: httpx.Request) -> httpx.Response:
        observed["query"] = request.url.query.decode()
        observed["api_key"] = request.headers["X-MBX-APIKEY"]
        return httpx.Response(200, json={"canTrade": True, "permissions": ["SPOT"]})

    client = BinanceClient("public-key", "private-secret", client=httpx.Client(transport=httpx.MockTransport(responder)))
    assert client.account()["canTrade"] is True
    assert "signature=" in observed["query"]
    assert observed["api_key"] == "public-key"


def test_binance_free_balance_excludes_locked_collateral() -> None:
    account = {"balances": [{"asset": "USDC", "free": "1.25", "locked": "99"}]}
    assert _binance_free_balance(account, "USDC") == Decimal("1.25")


def test_binance_base_asset_fee_is_converted_to_quote_bps() -> None:
    quantity, summary = _aggregate_fills(
        [
            {
                "symbol": "ETHUSDC",
                "isBuyer": True,
                "qty": "1",
                "price": "2000",
                "commission": "0.001",
                "commissionAsset": "ETH",
            }
        ],
        venue="binance",
        side="buy",
        symbol="ETHUSDC",
    )
    assert quantity == Decimal("1")
    assert summary["fee_bps"] == pytest.approx(10.0)


def test_bybit_unified_margin_uses_account_available_balance() -> None:
    wallet = {
        "result": {
            "list": [
                {
                    "totalAvailableBalance": "140.25",
                    "coin": [{"coin": "USDC", "availableToWithdraw": ""}],
                }
            ]
        }
    }
    assert _bybit_available_usdc(wallet) == Decimal("140.25")


def test_hyperliquid_unified_margin_uses_unheld_spot_usdc() -> None:
    state = {"withdrawable": "0"}
    spot_state = {
        "balances": [
            {"coin": "USDC", "total": "89.8", "hold": "0.3"},
            {"coin": "HYPE", "total": "1", "hold": "0"},
        ]
    }
    assert _hyperliquid_available_usdc(
        state,
        spot_state=spot_state,
        account_mode="unifiedAccount",
    ) == Decimal("89.5")
    assert _hyperliquid_available_usdc(state, account_mode="standard") == Decimal("0")


def test_execution_refuses_to_reach_network_without_confirmation() -> None:
    runner = MicroExecutionRunner(contract(), binance=object(), bybit=object(), hyperliquid=object())
    with pytest.raises(PreflightBlocked, match="one_time_confirmation"):
        runner.execute_asset("BTC", confirmation=None)


def test_stale_l2_blocks_before_any_order() -> None:
    class StaleRunner(MicroExecutionRunner):
        def _market_snapshot(self, asset: str):
            return {}, 5_001

    runner = StaleRunner(contract(), binance=object(), bybit=object(), hyperliquid=object())
    with pytest.raises(PreflightBlocked, match="stale_or_unsynchronized_l2"):
        runner.execute_asset("BTC", confirmation="micro-test-run")


def test_safe_unwind_rechecks_positions_after_reduce_only_order() -> None:
    class Binance:
        def account(self):
            return {"balances": []}

        def book(self, symbol):
            return {"bids": [["100", "1"]], "asks": [["101", "1"]]}

    class Bybit:
        def __init__(self) -> None:
            self.flat = False
            self.calls: list[dict] = []

        def positions(self, symbol):
            return [] if self.flat else [{"size": "0.1", "side": "Sell"}]

        def instrument(self, symbol):
            return {"lotSizeFilter": {"qtyStep": "0.1", "minOrderQty": "0.1", "minNotionalValue": "5"}, "priceFilter": {"tickSize": "0.1"}}

        def book(self, symbol):
            return {"b": [["100", "1"]], "a": [["101", "1"]]}

        def create_ioc(self, **kwargs):
            self.calls.append(kwargs)
            self.flat = True
            return {}

    class Hyperliquid:
        def state(self):
            return {"assetPositions": []}

        def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 1}]}

        def book(self, asset):
            return {"levels": [[{"px": "100", "sz": "1"}], [{"px": "101", "sz": "1"}]]}

    bybit = Bybit()
    runner = MicroExecutionRunner(contract(), binance=Binance(), bybit=bybit, hyperliquid=Hyperliquid())
    books, _ = runner._market_snapshot("BTC")
    assert runner._safe_unwind("BTC", filled={}, books=books, client_tag="test")
    assert bybit.calls[0]["side"] == "Buy"
    assert bybit.calls[0]["reduce_only"] is True


def test_safe_unwind_sells_tradable_binance_balance_and_accepts_only_dust() -> None:
    class Binance:
        balance = Decimal("0.10005")

        def account(self):
            return {"balances": [{"asset": "BTC", "free": str(self.balance), "locked": "0"}]}

        def instrument(self, symbol):
            return {
                "filters": [
                    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                    {"filterType": "MIN_NOTIONAL", "minNotional": "1"},
                    {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                ]
            }

        def book(self, symbol):
            return {"bids": [["100", "1"]], "asks": [["101", "1"]]}

        def create_ioc(self, **kwargs):
            assert kwargs["quantity"] == Decimal("0.10")
            self.balance -= kwargs["quantity"]
            return {}

    class Bybit:
        def positions(self, symbol):
            return []

        def instrument(self, symbol):
            return {
                "lotSizeFilter": {
                    "qtyStep": "0.1",
                    "minOrderQty": "0.1",
                    "minNotionalValue": "1",
                },
                "priceFilter": {"tickSize": "0.1"},
            }

        def book(self, symbol):
            return {"b": [["100", "1"]], "a": [["101", "1"]]}

    class Hyperliquid:
        def state(self):
            return {"assetPositions": []}

        def meta(self):
            return {"universe": [{"name": "BTC", "szDecimals": 1}]}

        def book(self, asset):
            return {"levels": [[{"px": "100", "sz": "1"}], [{"px": "101", "sz": "1"}]]}

    runner = MicroExecutionRunner(contract(), binance=Binance(), bybit=Bybit(), hyperliquid=Hyperliquid())
    books, _ = runner._market_snapshot("BTC")
    assert runner._safe_unwind("BTC", filled={}, books=books, client_tag="test")


def test_sanitized_bundle_retains_only_aggregate_evidence() -> None:
    bundle = sanitized_bundle(
        contract(),
        preflight={"status": "passed"},
        asset_results=[
            {"asset": "BTC", "status": "completed", "delta_mismatch_fraction": 0.0002},
            {"asset": "ETH", "status": "completed", "delta_mismatch_fraction": 0.0001},
        ],
    )
    assert bundle["status"] == "micro_execution_evidence_collected"
    assert bundle["acceptance"]["production_gate_remains_closed"] is True
    assert_sanitized(bundle)
    with pytest.raises(MicroExecutionError, match="forbidden"):
        assert_sanitized({"orderId": "forbidden"})


def test_partial_fill_or_exit_failure_never_counts_as_evidence() -> None:
    bundle = sanitized_bundle(
        contract(),
        preflight={"status": "passed"},
        asset_results=[{"asset": "BTC", "status": "failed_safe_unwind"}],
    )
    assert bundle["status"] == "micro_execution_evidence_incomplete"
    assert bundle["acceptance"]["completed_btc_eth_cycles"] is False
