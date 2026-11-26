"""Fail-closed Cross-USDC mainnet micro-execution evidence workflow.

The module deliberately separates live order code from research NAV code.  A
caller must freeze a contract, pass a fresh preflight, and explicitly provide
the matching one-time run id before any order endpoint is reachable.  Private
responses remain in memory; artifacts contain aggregate, non-identifying
evidence only.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from hashlib import sha256
import hmac
import json
from pathlib import Path
import time
from typing import Any, Callable, Iterable, Mapping
from urllib.parse import urlencode
from uuid import uuid4

import httpx
import yaml


BINANCE = "https://api.binance.com"
BYBIT = "https://api.bybit.com"
HYPERLIQUID = "https://api.hyperliquid.xyz"
ASSETS = ("BTC", "ETH")
FORBIDDEN_ARTIFACT_KEYS = {
    "address",
    "api_key",
    "api_secret",
    "balance",
    "clientorderid",
    "cloid",
    "execprice",
    "execqty",
    "fee",
    "orderid",
    "price",
    "quantity",
    "size",
    "tradeid",
    "wallet",
}


class MicroExecutionError(RuntimeError):
    """A safe, non-secret error suitable for an evidence verdict."""


class PreflightBlocked(MicroExecutionError):
    """Raised when the immutable safety gates reject a batch."""


class SafeUnwindRequired(MicroExecutionError):
    """Raised when an entry or exit needs the batch's unwind procedure."""


def _decimal(value: Any, *, field: str = "value") -> Decimal:
    try:
        result = Decimal(str(value))
    except Exception as error:  # pragma: no cover - Decimal error detail is unsafe
        raise MicroExecutionError(f"invalid_{field}") from error
    if not result.is_finite():
        raise MicroExecutionError(f"invalid_{field}")
    return result


def _text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def round_to_step(value: Decimal | str | float, step: Decimal | str | float, *, up: bool = False) -> Decimal:
    """Round a positive amount to a venue step without ever increasing risk by accident."""

    amount, increment = _decimal(value), _decimal(step, field="step")
    if amount <= 0 or increment <= 0:
        raise MicroExecutionError("non_positive_rounding_input")
    mode = ROUND_UP if up else ROUND_DOWN
    return (amount / increment).to_integral_value(rounding=mode) * increment


@dataclass(frozen=True)
class MicroExecutionContract:
    strategy_id: str
    run_id: str
    created_at_utc: str
    assets: tuple[str, ...]
    binance_spot_usdc_notional: Decimal
    bybit_perp_short_usdc_notional: Decimal
    hyperliquid_perp_short_usdc_notional: Decimal
    max_batch_gross_usdc: Decimal
    max_adverse_execution_bps: Decimal
    entry_deadline_seconds: int
    hold_seconds: int
    max_delta_mismatch_fraction: Decimal
    minimum_liquidation_distance_fraction: Decimal
    minimum_free_margin_to_required_im: Decimal
    required_perp_leverage: Decimal
    max_snapshot_sync_span_ms: int
    spot_quantity_mode: str
    minimum_actual_spot_notional_fraction: Decimal
    max_bybit_short_weight_deviation_fraction: Decimal
    credential_prefix: str
    execution_mode: str

    @property
    def total_perp_notional(self) -> Decimal:
        return self.bybit_perp_short_usdc_notional + self.hyperliquid_perp_short_usdc_notional

    @property
    def per_asset_gross(self) -> Decimal:
        return self.binance_spot_usdc_notional + self.total_perp_notional

    def validate(self) -> None:
        if tuple(self.assets) != ASSETS:
            raise MicroExecutionError("contract_assets_must_be_btc_eth")
        if self.binance_spot_usdc_notional != self.total_perp_notional:
            raise MicroExecutionError("contract_delta_notional_mismatch")
        if self.per_asset_gross * len(self.assets) > self.max_batch_gross_usdc:
            raise MicroExecutionError("contract_exceeds_max_batch_gross")
        if self.max_adverse_execution_bps != Decimal("5"):
            raise MicroExecutionError("contract_must_fix_five_bps_protection")
        if self.entry_deadline_seconds != 10 or self.hold_seconds != 60:
            raise MicroExecutionError("contract_timing_must_remain_frozen")
        if self.max_delta_mismatch_fraction != Decimal("0.001"):
            raise MicroExecutionError("contract_delta_tolerance_must_be_0_1_percent")
        if self.spot_quantity_mode not in {"fixed_target", "match_rounded_perps"}:
            raise MicroExecutionError("contract_spot_quantity_mode_invalid")
        if not Decimal("0.80") <= self.minimum_actual_spot_notional_fraction <= Decimal("1"):
            raise MicroExecutionError("contract_actual_spot_notional_floor_invalid")
        if not Decimal("0") <= self.max_bybit_short_weight_deviation_fraction <= Decimal("0.05"):
            raise MicroExecutionError("contract_short_weight_tolerance_invalid")
        if self.execution_mode != "mainnet_only":
            raise MicroExecutionError("contract_must_be_mainnet_only")


def build_contract(config_path: Path, *, run_id: str | None = None) -> MicroExecutionContract:
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    contract = MicroExecutionContract(
        strategy_id=str(raw["strategy_id"]),
        run_id=run_id or f"cross-usdc-micro-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}-{uuid4().hex[:10]}",
        created_at_utc=datetime.now(timezone.utc).isoformat(),
        assets=tuple(str(asset).upper() for asset in raw["assets"]),
        binance_spot_usdc_notional=_decimal(raw["binance_spot_usdc_notional"]),
        bybit_perp_short_usdc_notional=_decimal(raw["bybit_perp_short_usdc_notional"]),
        hyperliquid_perp_short_usdc_notional=_decimal(raw["hyperliquid_perp_short_usdc_notional"]),
        max_batch_gross_usdc=_decimal(raw["max_batch_gross_usdc"]),
        max_adverse_execution_bps=_decimal(raw["max_adverse_execution_bps"]),
        entry_deadline_seconds=int(raw["entry_deadline_seconds"]),
        hold_seconds=int(raw["hold_seconds"]),
        max_delta_mismatch_fraction=_decimal(raw["max_delta_mismatch_fraction"]),
        minimum_liquidation_distance_fraction=_decimal(raw["minimum_liquidation_distance_fraction"]),
        minimum_free_margin_to_required_im=_decimal(raw["minimum_free_margin_to_required_im"]),
        required_perp_leverage=_decimal(raw["required_perp_leverage"]),
        max_snapshot_sync_span_ms=int(raw["max_snapshot_sync_span_ms"]),
        spot_quantity_mode=str(raw["spot_quantity_mode"]),
        minimum_actual_spot_notional_fraction=_decimal(raw["minimum_actual_spot_notional_fraction"]),
        max_bybit_short_weight_deviation_fraction=_decimal(raw["max_bybit_short_weight_deviation_fraction"]),
        credential_prefix=str(raw["credential_prefix"]),
        execution_mode=str(raw["execution_mode"]),
    )
    contract.validate()
    return contract


def contract_from_payload(payload: Mapping[str, Any]) -> MicroExecutionContract:
    """Restore a previously frozen contract without regenerating its creation time."""

    contract = MicroExecutionContract(
        strategy_id=str(payload["strategy_id"]),
        run_id=str(payload["run_id"]),
        created_at_utc=str(payload["created_at_utc"]),
        assets=tuple(str(asset).upper() for asset in payload["assets"]),
        binance_spot_usdc_notional=_decimal(payload["binance_spot_usdc_notional"]),
        bybit_perp_short_usdc_notional=_decimal(payload["bybit_perp_short_usdc_notional"]),
        hyperliquid_perp_short_usdc_notional=_decimal(payload["hyperliquid_perp_short_usdc_notional"]),
        max_batch_gross_usdc=_decimal(payload["max_batch_gross_usdc"]),
        max_adverse_execution_bps=_decimal(payload["max_adverse_execution_bps"]),
        entry_deadline_seconds=int(payload["entry_deadline_seconds"]),
        hold_seconds=int(payload["hold_seconds"]),
        max_delta_mismatch_fraction=_decimal(payload["max_delta_mismatch_fraction"]),
        minimum_liquidation_distance_fraction=_decimal(payload["minimum_liquidation_distance_fraction"]),
        minimum_free_margin_to_required_im=_decimal(payload["minimum_free_margin_to_required_im"]),
        required_perp_leverage=_decimal(payload["required_perp_leverage"]),
        max_snapshot_sync_span_ms=int(payload["max_snapshot_sync_span_ms"]),
        spot_quantity_mode=str(payload["spot_quantity_mode"]),
        minimum_actual_spot_notional_fraction=_decimal(payload["minimum_actual_spot_notional_fraction"]),
        max_bybit_short_weight_deviation_fraction=_decimal(payload["max_bybit_short_weight_deviation_fraction"]),
        credential_prefix=str(payload["credential_prefix"]),
        execution_mode=str(payload["execution_mode"]),
    )
    contract.validate()
    if payload.get("contract_sha256") != contract_payload(contract)["contract_sha256"]:
        raise MicroExecutionError("frozen_contract_hash_invalid")
    return contract


def contract_payload(contract: MicroExecutionContract) -> dict[str, Any]:
    payload = asdict(contract)
    for key, value in list(payload.items()):
        if isinstance(value, Decimal):
            payload[key] = _text(value)
    payload["assets"] = list(contract.assets)
    payload["contract_sha256"] = sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


def write_frozen_contract(path: Path, contract: MicroExecutionContract) -> dict[str, Any]:
    """Write once; changing a run id's contract is a hard failure."""

    payload = contract_payload(contract)
    if path.exists():
        existing = json.loads(path.read_text(encoding="utf-8"))
        if existing.get("contract_sha256") != payload["contract_sha256"]:
            raise MicroExecutionError("immutable_contract_conflict")
        return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def plan_entry_quantities(
    contract: MicroExecutionContract,
    *,
    binance_price: Decimal,
    bybit_price: Decimal,
    hyperliquid_price: Decimal,
    binance_step: Decimal,
    bybit_step: Decimal,
    hyperliquid_step: Decimal,
) -> dict[str, Decimal]:
    """Build one lot-aware entry plan shared by preflight and live execution.

    The v2 mode treats the configured notionals as risk ceilings.  Perp legs
    are rounded down first, then the spot quantity matches their combined base
    quantity.  This preserves the delta gate without increasing a coarse venue
    lot merely to hit a headline notional.
    """

    bybit_quantity = round_to_step(
        contract.bybit_perp_short_usdc_notional / bybit_price,
        bybit_step,
    )
    hyperliquid_quantity = round_to_step(
        contract.hyperliquid_perp_short_usdc_notional / hyperliquid_price,
        hyperliquid_step,
    )
    if contract.spot_quantity_mode == "match_rounded_perps":
        binance_quantity = round_to_step(
            bybit_quantity + hyperliquid_quantity,
            binance_step,
        )
    else:
        binance_quantity = round_to_step(
            contract.binance_spot_usdc_notional / binance_price,
            binance_step,
        )

    binance_notional = binance_quantity * binance_price
    bybit_notional = bybit_quantity * bybit_price
    hyperliquid_notional = hyperliquid_quantity * hyperliquid_price
    short_notional = bybit_notional + hyperliquid_notional
    target_bybit_weight = (
        contract.bybit_perp_short_usdc_notional / contract.total_perp_notional
    )
    return {
        "binance": binance_quantity,
        "bybit": bybit_quantity,
        "hyperliquid": hyperliquid_quantity,
        "delta_fraction": (
            abs(binance_quantity - bybit_quantity - hyperliquid_quantity)
            / binance_quantity
            if binance_quantity
            else Decimal("1")
        ),
        "spot_target_coverage_fraction": (
            binance_notional / contract.binance_spot_usdc_notional
        ),
        "bybit_short_weight_fraction": (
            bybit_notional / short_notional if short_notional else Decimal("0")
        ),
        "bybit_short_weight_deviation_fraction": (
            abs(bybit_notional / short_notional - target_bybit_weight)
            if short_notional
            else Decimal("1")
        ),
        "actual_gross_usdc": binance_notional + short_notional,
    }


def confirmation_is_valid(contract: MicroExecutionContract, confirmation: str | None) -> bool:
    return bool(confirmation) and hmac.compare_digest(contract.run_id, confirmation)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fraction(numerator: Any, denominator: Any) -> float | None:
    top, bottom = _float(numerator), _float(denominator)
    if top is None or bottom in (None, 0.0):
        return None
    return abs(top) / abs(bottom)


def _liq_distance(liq: Any, mark: Any) -> float | None:
    liquidation, current = _float(liq), _float(mark)
    if liquidation in (None, 0.0) or current in (None, 0.0):
        return None
    return abs(liquidation - current) / abs(current)


def _has_forbidden_key(value: Any) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            normalized = str(key).replace("_", "").lower()
            if normalized in FORBIDDEN_ARTIFACT_KEYS:
                return True
            if _has_forbidden_key(nested):
                return True
    elif isinstance(value, (list, tuple)):
        return any(_has_forbidden_key(item) for item in value)
    return False


def assert_sanitized(payload: Mapping[str, Any]) -> None:
    if _has_forbidden_key(payload):
        raise MicroExecutionError("sanitized_artifact_contains_forbidden_field")


def _hash_source(label: str, payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":")).encode("utf-8")
    return f"{label}:{sha256(raw).hexdigest()}"


class BinanceClient:
    """Minimal Binance Spot client; it never writes except through ``create_ioc``."""

    def __init__(self, api_key: str, api_secret: str, *, client: httpx.Client | None = None):
        self.api_key, self.api_secret = api_key, api_secret
        self.client = client or httpx.Client(timeout=10.0)

    def _signed(self, method: str, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        values = {key: str(value) for key, value in (params or {}).items() if value is not None}
        values["timestamp"] = str(int(time.time() * 1000))
        values["recvWindow"] = "5000"
        query = urlencode(values)
        values["signature"] = hmac.new(self.api_secret.encode(), query.encode(), sha256).hexdigest()
        response = self.client.request(method, f"{BINANCE}{path}", params=values, headers={"X-MBX-APIKEY": self.api_key})
        payload = response.json()
        if response.status_code >= 400:
            code = payload.get("code") if isinstance(payload, Mapping) else None
            safe_code = {
                -1013: "binance_symbol_filter_rejected",
                -1022: "binance_signature_rejected",
                -2010: "binance_order_engine_rejected",
                -2015: "binance_key_ip_or_permission_rejected",
            }.get(code, "binance_private_request_rejected")
            raise MicroExecutionError(safe_code)
        if isinstance(payload, dict) and payload.get("code") not in (None, 0):
            raise MicroExecutionError("binance_private_request_rejected")
        return payload

    def account(self) -> dict[str, Any]:
        return self._signed("GET", "/api/v3/account")

    def instrument(self, symbol: str) -> dict[str, Any]:
        response = self.client.get(f"{BINANCE}/api/v3/exchangeInfo", params={"symbol": symbol})
        response.raise_for_status()
        rows = response.json().get("symbols") or []
        if len(rows) != 1:
            raise MicroExecutionError("binance_instrument_unavailable")
        return dict(rows[0])

    def book(self, symbol: str) -> dict[str, Any]:
        response = self.client.get(f"{BINANCE}/api/v3/depth", params={"symbol": symbol, "limit": 5})
        response.raise_for_status()
        return response.json()

    def create_ioc(self, *, symbol: str, side: str, quantity: Decimal, price: Decimal, client_id: str) -> dict[str, Any]:
        return self._signed(
            "POST",
            "/api/v3/order",
            {
                "symbol": symbol,
                "side": side,
                "type": "LIMIT",
                "timeInForce": "IOC",
                "quantity": _text(quantity),
                "price": _text(price),
                "newClientOrderId": client_id,
            },
        )

    def fills(self, *, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        payload = self._signed("GET", "/api/v3/myTrades", {"symbol": symbol, "startTime": start_ms, "endTime": end_ms})
        return payload if isinstance(payload, list) else []


class BybitClient:
    """Bybit V5 signed client for the isolated linear micro-test subaccount."""

    def __init__(self, api_key: str, api_secret: str, *, client: httpx.Client | None = None):
        self.api_key, self.api_secret = api_key, api_secret
        self.client = client or httpx.Client(timeout=10.0)

    def _private(self, method: str, path: str, *, params: Mapping[str, Any] | None = None, body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        recv_window = "5000"
        query = urlencode(sorted((str(key), str(value)) for key, value in (params or {}).items() if value is not None))
        body_text = json.dumps(dict(body or {}), separators=(",", ":")) if body is not None else ""
        signing_text = f"{timestamp}{self.api_key}{recv_window}{query if method == 'GET' else body_text}"
        signature = hmac.new(self.api_secret.encode(), signing_text.encode(), sha256).hexdigest()
        response = self.client.request(
            method,
            f"{BYBIT}{path}",
            params=query if method == "GET" else None,
            content=body_text if method != "GET" else None,
            headers={
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": signature,
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": recv_window,
                "Content-Type": "application/json",
            },
        )
        if response.status_code >= 400:
            raise MicroExecutionError("bybit_private_auth_or_ip_check_failed")
        payload = response.json()
        if int(payload.get("retCode", -1)) != 0:
            raise MicroExecutionError("bybit_private_request_rejected")
        return payload

    def query_api(self) -> dict[str, Any]:
        return self._private("GET", "/v5/user/query-api")

    def wallet(self) -> dict[str, Any]:
        return self._private("GET", "/v5/account/wallet-balance", params={"accountType": "UNIFIED", "coin": "USDC"})

    def account_info(self) -> dict[str, Any]:
        return self._private("GET", "/v5/account/info")

    def fee_rate(self, symbol: str) -> dict[str, Any]:
        return self._private("GET", "/v5/account/fee-rate", params={"category": "linear", "symbol": symbol})

    def positions(self, symbol: str) -> list[dict[str, Any]]:
        payload = self._private("GET", "/v5/position/list", params={"category": "linear", "symbol": symbol})
        return list((payload.get("result") or {}).get("list") or [])

    def instrument(self, symbol: str) -> dict[str, Any]:
        response = self.client.get(f"{BYBIT}/v5/market/instruments-info", params={"category": "linear", "symbol": symbol})
        response.raise_for_status()
        if int(response.json().get("retCode", -1)) != 0:
            raise MicroExecutionError("bybit_instrument_unavailable")
        rows = response.json().get("result", {}).get("list") or []
        if len(rows) != 1:
            raise MicroExecutionError("bybit_instrument_unavailable")
        return dict(rows[0])

    def book(self, symbol: str) -> dict[str, Any]:
        response = self.client.get(f"{BYBIT}/v5/market/orderbook", params={"category": "linear", "symbol": symbol, "limit": 5})
        response.raise_for_status()
        payload = response.json()
        if int(payload.get("retCode", -1)) != 0:
            raise MicroExecutionError("bybit_book_unavailable")
        return payload.get("result") or {}

    def create_ioc(self, *, symbol: str, side: str, quantity: Decimal, price: Decimal, client_id: str, reduce_only: bool) -> dict[str, Any]:
        return self._private(
            "POST",
            "/v5/order/create",
            body={
                "category": "linear",
                "symbol": symbol,
                "side": side,
                "orderType": "Limit",
                "qty": _text(quantity),
                "price": _text(price),
                "timeInForce": "IOC",
                "reduceOnly": reduce_only,
                "orderLinkId": client_id,
            },
        )

    def fills(self, *, symbol: str, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        payload = self._private("GET", "/v5/execution/list", params={"category": "linear", "symbol": symbol, "startTime": start_ms, "endTime": end_ms, "limit": 100})
        return list((payload.get("result") or {}).get("list") or [])


class HyperliquidClient:
    """Official Hyperliquid SDK bridge.  The private key stays process-local."""

    def __init__(self, account_address: str, api_wallet_private_key: str):
        try:
            from eth_account import Account
            from hyperliquid.exchange import Exchange
            from hyperliquid.info import Info
            from hyperliquid.utils import constants
        except ImportError as error:  # pragma: no cover - protected by extra dependency
            raise MicroExecutionError("hyperliquid_sdk_missing") from error
        self.account_address = account_address
        self._info = Info(constants.MAINNET_API_URL, skip_ws=True, timeout=10.0)
        self._wallet = Account.from_key(api_wallet_private_key)
        self._exchange = Exchange(self._wallet, base_url=constants.MAINNET_API_URL, account_address=account_address, timeout=10.0)

    @property
    def api_wallet_address(self) -> str:
        return str(self._wallet.address)

    def state(self) -> dict[str, Any]:
        return dict(self._info.user_state(self.account_address) or {})

    def spot_state(self) -> dict[str, Any]:
        return dict(self._info.spot_user_state(self.account_address) or {})

    def account_mode(self) -> str:
        return str(self._info.query_user_abstraction_state(self.account_address) or "")

    def extra_agents(self) -> Any:
        return self._info.extra_agents(self.account_address)

    def meta(self) -> dict[str, Any]:
        return dict(self._info.meta() or {})

    def mark_prices(self) -> dict[str, Any]:
        meta, contexts = self._info.meta_and_asset_ctxs()
        return {
            str(row.get("name")): (contexts[index] or {}).get("markPx")
            for index, row in enumerate((meta or {}).get("universe") or [])
        }

    def book(self, asset: str) -> dict[str, Any]:
        return dict(self._info.l2_snapshot(asset) or {})

    def fills(self, *, start_ms: int, end_ms: int) -> list[dict[str, Any]]:
        return list(self._info.user_fills_by_time(self.account_address, start_ms, end_ms) or [])

    def user_fees(self) -> dict[str, Any]:
        return dict(self._info.user_fees(self.account_address) or {})

    def create_ioc(self, *, asset: str, is_buy: bool, quantity: Decimal, price: Decimal, client_id: str, reduce_only: bool) -> Any:
        # A cloid is not supplied: it could be linked to an order identity and must never leave memory.
        self._exchange.expires_after = int((time.time() + 10.0) * 1000)
        response = self._exchange.order(
            asset,
            is_buy,
            float(quantity),
            float(price),
            {"limit": {"tif": "Ioc"}},
            reduce_only=reduce_only,
        )
        _validate_hyperliquid_ioc_response(response)
        return response


def _binance_constraints(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    filters = {str(item.get("filterType")): item for item in row.get("filters") or []}
    lot = filters.get("LOT_SIZE") or {}
    notional = filters.get("MIN_NOTIONAL") or filters.get("NOTIONAL") or {}
    return _decimal(lot.get("stepSize"), field="binance_step"), _decimal(lot.get("minQty"), field="binance_min_qty"), _decimal(notional.get("minNotional", notional.get("notional", "0")), field="binance_min_notional")


def _binance_price_tick(row: Mapping[str, Any]) -> Decimal:
    filters = {str(item.get("filterType")): item for item in row.get("filters") or []}
    return _decimal((filters.get("PRICE_FILTER") or {}).get("tickSize"), field="binance_tick")


def _bybit_constraints(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    lot = dict(row.get("lotSizeFilter") or {})
    price = dict(row.get("priceFilter") or {})
    return (
        _decimal(lot.get("qtyStep"), field="bybit_step"),
        _decimal(lot.get("minOrderQty"), field="bybit_min_qty"),
        _decimal(lot.get("minNotionalValue", "0"), field="bybit_min_notional"),
        _decimal(price.get("tickSize"), field="bybit_tick"),
    )


def _hyperliquid_constraints(meta: Mapping[str, Any], asset: str) -> tuple[Decimal, Decimal, int]:
    universe = list(meta.get("universe") or [])
    row = next((item for item in universe if item.get("name") == asset), None)
    if row is None:
        raise MicroExecutionError("hyperliquid_instrument_unavailable")
    decimals = int(row.get("szDecimals"))
    return Decimal(1).scaleb(-decimals), Decimal("10"), decimals


def _validate_hyperliquid_ioc_response(response: Any) -> None:
    """Reject HTTP-200 exchange errors without persisting the raw response."""

    if not isinstance(response, Mapping) or response.get("status") != "ok":
        raise MicroExecutionError("hyperliquid_order_request_rejected")
    order_response = response.get("response")
    if not isinstance(order_response, Mapping) or order_response.get("type") != "order":
        raise MicroExecutionError("hyperliquid_order_response_invalid")
    data = order_response.get("data")
    statuses = data.get("statuses") if isinstance(data, Mapping) else None
    if not isinstance(statuses, list) or len(statuses) != 1:
        raise MicroExecutionError("hyperliquid_order_status_missing")
    status = statuses[0]
    if isinstance(status, Mapping) and isinstance(status.get("filled"), Mapping):
        return
    error = str(status.get("error") or "").lower() if isinstance(status, Mapping) else ""
    if "tick size" in error or "invalid price" in error:
        code = "hyperliquid_tick_rejected"
    elif "could not immediately match" in error:
        code = "hyperliquid_ioc_cancelled"
    elif "minimum value" in error:
        code = "hyperliquid_min_notional_rejected"
    elif "insufficient margin" in error:
        code = "hyperliquid_margin_rejected"
    else:
        code = "hyperliquid_order_rejected"
    raise MicroExecutionError(code)


def _hyperliquid_ioc_filled_quantity(response: Any) -> Decimal:
    """Return the exchange-confirmed fill without waiting for userFills indexing."""

    _validate_hyperliquid_ioc_response(response)
    status = response["response"]["data"]["statuses"][0]
    return _decimal(status["filled"].get("totalSz"), field="hyperliquid_ioc_filled_quantity")


def _best_price(book: Mapping[str, Any], *, venue: str, side: str) -> Decimal:
    if venue == "binance":
        rows = book.get("asks") if side == "buy" else book.get("bids")
        value = rows[0][0] if rows else None
    elif venue == "bybit":
        rows = book.get("a") if side == "buy" else book.get("b")
        value = rows[0][0] if rows else None
    else:
        levels = book.get("levels") or []
        rows = levels[1] if side == "buy" else levels[0]
        value = rows[0].get("px") if rows else None
    price = _decimal(value, field=f"{venue}_best_price")
    if price <= 0:
        raise MicroExecutionError("invalid_best_price")
    return price


def protected_limit(best_price: Decimal, *, side: str, adverse_bps: Decimal, tick: Decimal | None = None) -> Decimal:
    multiplier = Decimal("1") + (adverse_bps / Decimal("10000")) if side == "buy" else Decimal("1") - (adverse_bps / Decimal("10000"))
    value = best_price * multiplier
    if value <= 0:
        raise MicroExecutionError("non_positive_protected_limit")
    return round_to_step(value, tick, up=side == "sell") if tick else value


def hyperliquid_protected_limit(
    best_price: Decimal,
    *,
    side: str,
    adverse_bps: Decimal,
    sz_decimals: int,
) -> Decimal:
    """Apply Hyperliquid price precision without exceeding the adverse limit."""

    if side not in {"buy", "sell"} or not 0 <= sz_decimals <= 6:
        raise MicroExecutionError("invalid_hyperliquid_price_contract")
    raw = protected_limit(best_price, side=side, adverse_bps=adverse_bps)
    decimal_tick = Decimal(1).scaleb(-(6 - sz_decimals))
    significant_tick = Decimal(1).scaleb(raw.adjusted() - 4)
    # Hyperliquid explicitly allows integer prices regardless of significant figures.
    tick = max(decimal_tick, min(Decimal("1"), significant_tick))
    # A buy rounds down and a sell rounds up so venue precision never weakens 5 bps protection.
    rounded = round_to_step(raw, tick, up=side == "sell")
    if rounded <= 0:
        raise MicroExecutionError("non_positive_hyperliquid_limit")
    return rounded


def _safe_error_code(error: Exception) -> str:
    return str(error) if isinstance(error, MicroExecutionError) else type(error).__name__


def _zero_position(rows: Iterable[Mapping[str, Any]], *, size_key: str) -> bool:
    return all(abs(_float(row.get(size_key)) or 0.0) == 0.0 for row in rows)


def _bybit_available_usdc(wallet: Mapping[str, Any]) -> Decimal | None:
    accounts = list((wallet.get("result") or {}).get("list") or [])
    if not accounts:
        return None
    account = accounts[0]
    total_available = account.get("totalAvailableBalance")
    if total_available not in (None, ""):
        return _decimal(total_available, field="bybit_available")
    coin_rows = list(account.get("coin") or [])
    row = next((item for item in coin_rows if item.get("coin") == "USDC"), None)
    return _decimal(row.get("availableToWithdraw"), field="bybit_available") if row and row.get("availableToWithdraw") not in (None, "") else None


def _binance_asset_balance(account: Mapping[str, Any], asset: str) -> Decimal:
    rows = list(account.get("balances") or [])
    row = next((item for item in rows if item.get("asset") == asset), None)
    if row is None:
        return Decimal("0")
    return _decimal(row.get("free", "0"), field="binance_spot_balance") + _decimal(row.get("locked", "0"), field="binance_spot_locked")


def _binance_free_balance(account: Mapping[str, Any], asset: str) -> Decimal:
    rows = list(account.get("balances") or [])
    row = next((item for item in rows if item.get("asset") == asset), None)
    return _decimal(row.get("free", "0"), field="binance_spot_free") if row else Decimal("0")


def _hyperliquid_available_usdc(
    state: Mapping[str, Any],
    *,
    spot_state: Mapping[str, Any] | None = None,
    account_mode: str | None = None,
) -> Decimal | None:
    if account_mode == "unifiedAccount":
        balances = list((spot_state or {}).get("balances") or [])
        row = next((item for item in balances if item.get("coin") == "USDC"), None)
        if row is None:
            return Decimal("0")
        total = _decimal(row.get("total", "0"), field="hyperliquid_spot_total")
        hold = _decimal(row.get("hold", "0"), field="hyperliquid_spot_hold")
        return max(total - hold, Decimal("0"))
    value = state.get("withdrawable")
    return _decimal(value, field="hyperliquid_available") if value not in (None, "") else None


def _bybit_position_evidence(rows: list[Mapping[str, Any]]) -> dict[str, Any]:
    row = next((item for item in rows if abs(_float(item.get("size")) or 0.0) > 0.0), None)
    if row is None:
        return {"has_target_position": False, "initial_margin_fraction": None, "maintenance_margin_fraction": None, "margin_usage_fraction": None, "liquidation_distance_fraction": None}
    initial_margin = _fraction(row.get("positionIM"), row.get("positionValue"))
    return {
        "has_target_position": True,
        "initial_margin_fraction": initial_margin,
        "maintenance_margin_fraction": _fraction(row.get("positionMM"), row.get("positionValue")),
        "margin_usage_fraction": initial_margin,
        "liquidation_distance_fraction": _liq_distance(row.get("liqPrice"), row.get("markPrice")),
    }


def _hyperliquid_position_evidence(state: Mapping[str, Any], asset: str, *, mark_price: Any = None) -> dict[str, Any]:
    positions = list(state.get("assetPositions") or [])
    row = next((item.get("position") or {} for item in positions if (item.get("position") or {}).get("coin") == asset and abs(_float((item.get("position") or {}).get("szi")) or 0.0) > 0.0), None)
    if row is None:
        return {"has_target_position": False, "initial_margin_fraction": None, "maintenance_margin_fraction": None, "margin_usage_fraction": None, "liquidation_distance_fraction": None}
    value = _float(row.get("positionValue"))
    margin = _float(row.get("marginUsed"))
    liquidation = _liq_distance(row.get("liquidationPx"), mark_price)
    return {
        "has_target_position": True,
        "initial_margin_fraction": abs(margin / value) if margin is not None and value not in (None, 0.0) else None,
        "maintenance_margin_fraction": None,
        "margin_usage_fraction": abs(margin / value) if margin is not None and value not in (None, 0.0) else None,
        "liquidation_distance_fraction": liquidation,
    }


def _aggregate_fills(rows: Iterable[Mapping[str, Any]], *, venue: str, side: str, symbol: str) -> tuple[Decimal, dict[str, Any]]:
    def normalized_side(row: Mapping[str, Any]) -> str:
        raw = str(row.get("side") or "").lower()
        if raw in {"buy", "b"} or (venue == "binance" and bool(row.get("isBuyer"))):
            return "buy"
        if raw in {"sell", "a"} or (venue == "binance" and row.get("isBuyer") is False):
            return "sell"
        return "unknown"

    matching = []
    for row in rows:
        row_symbol = str(row.get("symbol") or row.get("coin") or "")
        row_side = normalized_side(row)
        if row_symbol != symbol or row_side != side.lower():
            continue
        matching.append(row)
    quantity_key = "qty" if venue == "binance" else "execQty" if venue == "bybit" else "sz"
    price_key = "price" if venue == "binance" else "execPrice" if venue == "bybit" else "px"
    total_qty = sum((_decimal(row.get(quantity_key), field="fill_quantity") for row in matching), Decimal("0"))
    notional = sum((_decimal(row.get(quantity_key), field="fill_quantity") * _decimal(row.get(price_key), field="fill_price") for row in matching), Decimal("0"))
    maker_count = sum(bool(row.get("isMaker") if venue != "hyperliquid" else row.get("crossed") is False) for row in matching)
    fee_key = "commission" if venue == "binance" else "execFee" if venue == "bybit" else "fee"
    fee_units_verified = True
    fee_total = Decimal("0")
    for row in matching:
        fee = abs(_decimal(row.get(fee_key, "0"), field="fill_fee"))
        if venue != "binance":
            fee_total += fee
            continue
        commission_asset = str(row.get("commissionAsset") or "USDC")
        if commission_asset == "USDC":
            fee_total += fee
        elif commission_asset == symbol.removesuffix("USDC"):
            fee_total += fee * _decimal(row.get(price_key), field="fill_price")
        else:
            fee_units_verified = False
    fee_bps = float(fee_total / notional * Decimal("10000")) if notional > 0 and fee_units_verified else None
    return total_qty, {"fill_count": len(matching), "maker_count": maker_count, "taker_count": len(matching) - maker_count, "fee_bps": fee_bps}


def _shortfall_bps(rows: Iterable[Mapping[str, Any]], *, venue: str, symbol: str, side: str, protected_price: Decimal) -> float | None:
    quantity, summary = _aggregate_fills(rows, venue=venue, side=side, symbol=symbol)
    if quantity <= 0 or summary["fill_count"] == 0:
        return None
    quantity_key = "qty" if venue == "binance" else "execQty" if venue == "bybit" else "sz"
    price_key = "price" if venue == "binance" else "execPrice" if venue == "bybit" else "px"
    def is_side(row: Mapping[str, Any]) -> bool:
        raw = str(row.get("side") or "").lower()
        normalized = "buy" if raw in {"buy", "b"} or (venue == "binance" and bool(row.get("isBuyer"))) else "sell" if raw in {"sell", "a"} or (venue == "binance" and row.get("isBuyer") is False) else "unknown"
        return normalized == side.lower()

    matching = [row for row in rows if str(row.get("symbol") or row.get("coin") or "") == symbol and is_side(row)]
    avg = sum((_decimal(row.get(quantity_key)) * _decimal(row.get(price_key)) for row in matching), Decimal("0")) / quantity
    return float((avg - protected_price) / protected_price * Decimal("10000")) * (1 if side == "buy" else -1)


def _run_parallel(calls: Mapping[str, Callable[[], Any]]) -> tuple[dict[str, Any], int]:
    started = int(time.time() * 1000)
    results: dict[str, Any] = {}
    with ThreadPoolExecutor(max_workers=len(calls)) as executor:
        futures = {executor.submit(call): name for name, call in calls.items()}
        for future in as_completed(futures):
            results[futures[future]] = future.result()
    return results, int(time.time() * 1000) - started


class MicroExecutionRunner:
    """Stateful runner that intentionally makes the execution path awkward to call."""

    def __init__(self, contract: MicroExecutionContract, *, binance: BinanceClient, bybit: BybitClient, hyperliquid: HyperliquidClient):
        contract.validate()
        self.contract, self.binance, self.bybit, self.hyperliquid = contract, binance, bybit, hyperliquid

    def _market_snapshot(self, asset: str) -> tuple[dict[str, Any], int]:
        symbol, perp = f"{asset}USDC", f"{asset}PERP"
        responses, span_ms = _run_parallel({"binance": lambda: self.binance.book(symbol), "bybit": lambda: self.bybit.book(perp), "hyperliquid": lambda: self.hyperliquid.book(asset)})
        return responses, span_ms

    def preflight(self) -> dict[str, Any]:
        """Read-only verification.  It never invokes an order, cancel, or transfer endpoint."""

        try:
            responses, private_span_ms = _run_parallel({
                "binance_account": self.binance.account,
                "bybit_api": self.bybit.query_api,
                "bybit_wallet": self.bybit.wallet,
                "bybit_account_info": self.bybit.account_info,
                "hyperliquid_state": self.hyperliquid.state,
                "hyperliquid_spot_state": self.hyperliquid.spot_state,
                "hyperliquid_account_mode": self.hyperliquid.account_mode,
                "hyperliquid_agents": self.hyperliquid.extra_agents,
                "hyperliquid_fees": self.hyperliquid.user_fees,
            })
            binance_account = responses["binance_account"]
            binance_available_usdc = _binance_free_balance(binance_account, "USDC")
            bybit_permissions = responses["bybit_api"].get("result", {}).get("permissions") or []
            expected_agent = self.hyperliquid.api_wallet_address.lower()
            agent_authorized = expected_agent in json.dumps(responses["hyperliquid_agents"], sort_keys=True).lower()
            hyperliquid_account_mode = str(responses["hyperliquid_account_mode"])
            checks: list[dict[str, Any]] = [
                {"name": "private_auth_and_ip_access", "passed": private_span_ms <= self.contract.max_snapshot_sync_span_ms * 2},
                {"name": "binance_trade_permission", "passed": bool(binance_account.get("canTrade"))},
                {"name": "bybit_contract_trade_permission", "passed": "ContractTrade" in bybit_permissions},
                {"name": "bybit_regular_margin", "passed": str((responses["bybit_account_info"].get("result") or {}).get("marginMode") or "") == "REGULAR_MARGIN"},
                {"name": "hyperliquid_api_wallet_authorized", "passed": agent_authorized},
                {"name": "hyperliquid_account_mode_supported", "passed": hyperliquid_account_mode in {"standard", "unifiedAccount"}},
            ]
            bybit_available = _bybit_available_usdc(responses["bybit_wallet"])
            hyper_available = _hyperliquid_available_usdc(
                responses["hyperliquid_state"],
                spot_state=responses["hyperliquid_spot_state"],
                account_mode=hyperliquid_account_mode,
            )
            needed_bybit = self.contract.bybit_perp_short_usdc_notional / self.contract.required_perp_leverage
            needed_hyper = self.contract.hyperliquid_perp_short_usdc_notional / self.contract.required_perp_leverage
            bybit_ratio = float(bybit_available / needed_bybit) if bybit_available is not None and needed_bybit > 0 else None
            hyper_ratio = float(hyper_available / needed_hyper) if hyper_available is not None and needed_hyper > 0 else None
            checks.extend([
                {"name": "bybit_free_margin", "passed": bybit_ratio is not None and bybit_ratio >= float(self.contract.minimum_free_margin_to_required_im)},
                {"name": "hyperliquid_free_margin", "passed": hyper_ratio is not None and hyper_ratio >= float(self.contract.minimum_free_margin_to_required_im)},
            ])
            asset_evidence: dict[str, Any] = {}
            for asset in self.contract.assets:
                symbol, perp = f"{asset}USDC", f"{asset}PERP"
                data, span_ms = _run_parallel({
                    "binance_instrument": lambda symbol=symbol: self.binance.instrument(symbol),
                    "bybit_instrument": lambda perp=perp: self.bybit.instrument(perp),
                    "hyperliquid_meta": self.hyperliquid.meta,
                    "hyperliquid_marks": self.hyperliquid.mark_prices,
                    "bybit_positions": lambda perp=perp: self.bybit.positions(perp),
                    "bybit_fee": lambda perp=perp: self.bybit.fee_rate(perp),
                    "market": lambda asset=asset: self._market_snapshot(asset),
                })
                market, market_span = data["market"]
                binance_step, binance_min_qty, binance_min_notional = _binance_constraints(data["binance_instrument"])
                binance_tick = _binance_price_tick(data["binance_instrument"])
                bybit_step, bybit_min_qty, bybit_min_notional, _ = _bybit_constraints(data["bybit_instrument"])
                hyper_step, hyper_min_notional, hyper_sz_decimals = _hyperliquid_constraints(
                    data["hyperliquid_meta"],
                    asset,
                )
                binance_price = _best_price(market["binance"], venue="binance", side="buy")
                bybit_price = _best_price(market["bybit"], venue="bybit", side="sell")
                hyper_price = _best_price(market["hyperliquid"], venue="hyperliquid", side="sell")
                hyperliquid_protected_limit(
                    hyper_price,
                    side="sell",
                    adverse_bps=self.contract.max_adverse_execution_bps,
                    sz_decimals=hyper_sz_decimals,
                )
                protected_limit(
                    binance_price,
                    side="buy",
                    adverse_bps=self.contract.max_adverse_execution_bps,
                    tick=binance_tick,
                )
                quantities = plan_entry_quantities(
                    self.contract,
                    binance_price=binance_price,
                    bybit_price=bybit_price,
                    hyperliquid_price=hyper_price,
                    binance_step=binance_step,
                    bybit_step=bybit_step,
                    hyperliquid_step=hyper_step,
                )
                valid_sizes = quantities["binance"] >= binance_min_qty and quantities["bybit"] >= bybit_min_qty
                valid_notionals = quantities["binance"] * binance_price >= binance_min_notional and quantities["bybit"] * bybit_price >= bybit_min_notional and quantities["hyperliquid"] * hyper_price >= hyper_min_notional
                delta_ratio = quantities["delta_fraction"]
                spot_coverage_passed = quantities["spot_target_coverage_fraction"] >= self.contract.minimum_actual_spot_notional_fraction
                short_weight_passed = quantities["bybit_short_weight_deviation_fraction"] <= self.contract.max_bybit_short_weight_deviation_fraction
                gross_passed = quantities["actual_gross_usdc"] <= self.contract.per_asset_gross
                binance_required_usdc = quantities["binance"] * binance_price
                binance_balance_ratio = (
                    binance_available_usdc / binance_required_usdc
                    if binance_required_usdc > 0
                    else Decimal("0")
                )
                binance_balance_passed = binance_balance_ratio >= 1
                bybit_positions = data["bybit_positions"]
                hyper_state = self.hyperliquid.state()
                binance_asset_balance = _binance_asset_balance(binance_account, asset)
                binance_dust_only = (
                    binance_asset_balance > 0
                    and (
                        binance_asset_balance < binance_step
                        or binance_asset_balance * binance_price < binance_min_notional
                    )
                )
                zero_positions = _zero_position(bybit_positions, size_key="size") and not _hyperliquid_position_evidence(hyper_state, asset, mark_price=data["hyperliquid_marks"].get(asset))["has_target_position"] and (binance_asset_balance == 0 or binance_dust_only)
                passed = bool(
                    valid_sizes
                    and valid_notionals
                    and spot_coverage_passed
                    and short_weight_passed
                    and gross_passed
                    and binance_balance_passed
                    and zero_positions
                    and delta_ratio <= self.contract.max_delta_mismatch_fraction
                    and market_span <= self.contract.max_snapshot_sync_span_ms
                )
                bybit_fee_rows = list((data["bybit_fee"].get("result") or {}).get("list") or [])
                bybit_taker_rate = _float((bybit_fee_rows[0] if bybit_fee_rows else {}).get("takerFeeRate"))
                asset_evidence[asset] = {
                    "passed": passed,
                    "timestamp_sync_passed": market_span <= self.contract.max_snapshot_sync_span_ms,
                    "snapshot_span_ms": market_span,
                    "lot_rounding_delta_fraction": float(delta_ratio),
                    "minimum_size_passed": bool(valid_sizes and valid_notionals),
                    "spot_target_coverage_fraction": float(quantities["spot_target_coverage_fraction"]),
                    "spot_target_coverage_passed": spot_coverage_passed,
                    "bybit_short_weight_fraction": float(quantities["bybit_short_weight_fraction"]),
                    "short_weight_tolerance_passed": short_weight_passed,
                    "actual_gross_within_contract": gross_passed,
                    "binance_spot_available_to_required_ratio": float(binance_balance_ratio),
                    "binance_spot_balance_passed": binance_balance_passed,
                    "target_positions_zero": zero_positions,
                    "binance_spot_dust_only": binance_dust_only,
                    "liquidation_distance_pre_entry_not_applicable": True,
                    "bybit_taker_fee_bps": bybit_taker_rate * 10_000.0 if bybit_taker_rate is not None else None,
                }
            checks.append({"name": "all_assets_preflight", "passed": all(item["passed"] for item in asset_evidence.values())})
            verdict = "passed" if all(item["passed"] for item in checks) else "blocked"
            return {
                "status": verdict,
                "run_id": self.contract.run_id,
                "checks": checks,
                "assets": asset_evidence,
                "margin": {
                    "bybit_free_margin_to_required_im_ratio": bybit_ratio,
                    "hyperliquid_free_margin_to_required_im_ratio": hyper_ratio,
                    "minimum_required_ratio": float(self.contract.minimum_free_margin_to_required_im),
                },
                "fees": {
                    "binance_account_schedule_present": bool(binance_account.get("makerCommission") is not None and binance_account.get("takerCommission") is not None),
                    "hyperliquid_user_fee_schedule_present": bool(responses["hyperliquid_fees"]),
                },
                "source_hashes": sorted([_hash_source("binance_account", binance_account), _hash_source("bybit_account", responses["bybit_api"]), _hash_source("hyperliquid_state", responses["hyperliquid_state"])]),
            }
        except Exception as error:
            return {"status": "blocked", "run_id": self.contract.run_id, "checks": [{"name": "read_only_preflight", "passed": False}], "safe_error": type(error).__name__}

    def _account_snapshot(self, asset: str) -> dict[str, Any]:
        perp = f"{asset}PERP"
        snapshots, _ = _run_parallel({"bybit": lambda: self.bybit.positions(perp), "hyperliquid": self.hyperliquid.state, "hyperliquid_marks": self.hyperliquid.mark_prices})
        bybit_rows, hyper_state = snapshots["bybit"], snapshots["hyperliquid"]
        bybit = _bybit_position_evidence(bybit_rows)
        hyper = _hyperliquid_position_evidence(hyper_state, asset, mark_price=snapshots["hyperliquid_marks"].get(asset))
        liq_passed = all(
            value is None or value >= float(self.contract.minimum_liquidation_distance_fraction)
            for value in (bybit["liquidation_distance_fraction"], hyper["liquidation_distance_fraction"])
        )
        return {"bybit": bybit, "hyperliquid": hyper, "liquidation_distance_passed": liq_passed}

    def _wait_fills(
        self,
        asset: str,
        *,
        start_ms: int,
        end_ms: int,
        expected_venues: Iterable[str] = (),
    ) -> dict[str, list[dict[str, Any]]]:
        symbol, perp = f"{asset}USDC", f"{asset}PERP"
        deadline = time.monotonic() + 2.0
        while True:
            query_end_ms = max(end_ms, int(time.time() * 1000))
            rows, _ = _run_parallel({"binance": lambda: self.binance.fills(symbol=symbol, start_ms=start_ms, end_ms=query_end_ms), "bybit": lambda: self.bybit.fills(symbol=perp, start_ms=start_ms, end_ms=query_end_ms), "hyperliquid": lambda: self.hyperliquid.fills(start_ms=start_ms, end_ms=query_end_ms)})
            if all(rows.get(venue) for venue in expected_venues) or time.monotonic() >= deadline:
                return rows
            time.sleep(0.25)

    def _safe_unwind(self, asset: str, *, filled: Mapping[str, Decimal], books: Mapping[str, Mapping[str, Any]], client_tag: str) -> bool:
        """Flatten observed state, then verify it is flat before reporting success.

        We do not trust an earlier order response: any transport timeout can hide
        a fill.  The initial preflight requires zero target spot/perp inventory,
        so current target balances/positions are safe to treat as this batch.
        """

        try:
            symbol, perp = f"{asset}USDC", f"{asset}PERP"
            fresh_books, span_ms = self._market_snapshot(asset)
            if span_ms <= self.contract.max_snapshot_sync_span_ms:
                books = fresh_books
            state, _ = _run_parallel({
                "binance": self.binance.account,
                "bybit": lambda: self.bybit.positions(perp),
                "hyperliquid": self.hyperliquid.state,
            })
            binance_qty = _binance_asset_balance(state["binance"], asset)
            bybit_row = next((row for row in state["bybit"] if abs(_float(row.get("size")) or 0.0) > 0.0), None)
            bybit_qty = _decimal(bybit_row.get("size"), field="bybit_position_size") if bybit_row else Decimal("0")
            bybit_side = str(bybit_row.get("side") or "").lower() if bybit_row else ""
            hyper_positions = list(state["hyperliquid"].get("assetPositions") or [])
            hyper_row = next((row.get("position") or {} for row in hyper_positions if (row.get("position") or {}).get("coin") == asset and abs(_float((row.get("position") or {}).get("szi")) or 0.0) > 0.0), None)
            hyper_szi = _decimal(hyper_row.get("szi"), field="hyperliquid_position_size") if hyper_row else Decimal("0")
            bybit_step, _, _, bybit_tick = _bybit_constraints(self.bybit.instrument(perp))
            hyper_step, _, hyper_sz_decimals = _hyperliquid_constraints(
                self.hyperliquid.meta(),
                asset,
            )
            calls: dict[str, Callable[[], Any]] = {}
            if binance_qty > 0:
                binance_instrument = self.binance.instrument(symbol)
                binance_step, binance_min_qty, binance_min_notional = _binance_constraints(binance_instrument)
                binance_tick = _binance_price_tick(binance_instrument)
                binance_sell_qty = round_to_step(binance_qty, binance_step)
                binance_bid = _best_price(books["binance"], venue="binance", side="sell")
                if binance_sell_qty >= binance_min_qty and binance_sell_qty * binance_bid >= binance_min_notional:
                    calls["binance"] = lambda: self.binance.create_ioc(symbol=symbol, side="SELL", quantity=binance_sell_qty, price=protected_limit(binance_bid, side="sell", adverse_bps=self.contract.max_adverse_execution_bps, tick=binance_tick), client_id=f"{client_tag}-bu")
            if bybit_qty > 0:
                side = "Buy" if bybit_side == "sell" else "Sell"
                calls["bybit"] = lambda side=side: self.bybit.create_ioc(symbol=perp, side=side, quantity=round_to_step(bybit_qty, bybit_step), price=protected_limit(_best_price(books["bybit"], venue="bybit", side=side.lower()), side=side.lower(), adverse_bps=self.contract.max_adverse_execution_bps, tick=bybit_tick), client_id=f"{client_tag}-yu", reduce_only=True)
            if hyper_szi != 0:
                is_buy = hyper_szi < 0
                calls["hyperliquid"] = lambda is_buy=is_buy: self.hyperliquid.create_ioc(
                    asset=asset,
                    is_buy=is_buy,
                    quantity=round_to_step(abs(hyper_szi), hyper_step),
                    price=hyperliquid_protected_limit(
                        _best_price(
                            books["hyperliquid"],
                            venue="hyperliquid",
                            side="buy" if is_buy else "sell",
                        ),
                        side="buy" if is_buy else "sell",
                        adverse_bps=self.contract.max_adverse_execution_bps,
                        sz_decimals=hyper_sz_decimals,
                    ),
                    client_id=f"{client_tag}-hu",
                    reduce_only=True,
                )
            if calls:
                _run_parallel(calls)
            time.sleep(0.3)
            verified, _ = _run_parallel({
                "binance": self.binance.account,
                "bybit": lambda: self.bybit.positions(perp),
                "hyperliquid": self.hyperliquid.state,
            })
            bybit_flat = _zero_position(verified["bybit"], size_key="size")
            hyper_flat = not _hyperliquid_position_evidence(verified["hyperliquid"], asset)["has_target_position"]
            binance_remaining = _binance_asset_balance(verified["binance"], asset)
            binance_flat = binance_remaining == 0
            if not binance_flat:
                binance_instrument = self.binance.instrument(symbol)
                binance_step, _, binance_min_notional = _binance_constraints(binance_instrument)
                binance_bid = _best_price(self.binance.book(symbol), venue="binance", side="sell")
                binance_flat = binance_remaining < binance_step or binance_remaining * binance_bid < binance_min_notional
            return binance_flat and bybit_flat and hyper_flat
        except Exception:
            return False

    def execute_asset(self, asset: str, *, confirmation: str) -> dict[str, Any]:
        if not confirmation_is_valid(self.contract, confirmation):
            raise PreflightBlocked("one_time_confirmation_missing_or_mismatched")
        if asset not in self.contract.assets:
            raise MicroExecutionError("asset_not_in_contract")
        symbol, perp = f"{asset}USDC", f"{asset}PERP"
        books, span_ms = self._market_snapshot(asset)
        if span_ms > self.contract.max_snapshot_sync_span_ms:
            raise PreflightBlocked("stale_or_unsynchronized_l2")
        b_instrument = self.binance.instrument(symbol)
        b_step, b_min, b_min_notional = _binance_constraints(b_instrument)
        b_tick = _binance_price_tick(b_instrument)
        y_step, y_min, y_min_notional, y_tick = _bybit_constraints(self.bybit.instrument(perp))
        h_step, h_min_notional, h_sz_decimals = _hyperliquid_constraints(
            self.hyperliquid.meta(),
            asset,
        )
        b_price = _best_price(books["binance"], venue="binance", side="buy")
        y_price = _best_price(books["bybit"], venue="bybit", side="sell")
        h_price = _best_price(books["hyperliquid"], venue="hyperliquid", side="sell")
        plan = plan_entry_quantities(
            self.contract,
            binance_price=b_price,
            bybit_price=y_price,
            hyperliquid_price=h_price,
            binance_step=b_step,
            bybit_step=y_step,
            hyperliquid_step=h_step,
        )
        b_qty, y_qty, h_qty = plan["binance"], plan["bybit"], plan["hyperliquid"]
        if b_qty < b_min or y_qty < y_min or b_qty * b_price < b_min_notional or y_qty * y_price < y_min_notional or h_qty * h_price < h_min_notional:
            raise PreflightBlocked("minimum_notional_or_lot_size_failed")
        if plan["spot_target_coverage_fraction"] < self.contract.minimum_actual_spot_notional_fraction:
            raise PreflightBlocked("actual_spot_notional_below_floor")
        if plan["bybit_short_weight_deviation_fraction"] > self.contract.max_bybit_short_weight_deviation_fraction:
            raise PreflightBlocked("rounded_short_weight_mismatch")
        if plan["actual_gross_usdc"] > self.contract.per_asset_gross:
            raise PreflightBlocked("actual_gross_exceeds_contract")
        if plan["delta_fraction"] > self.contract.max_delta_mismatch_fraction:
            raise PreflightBlocked("rounded_delta_mismatch")
        start_ms = int(time.time() * 1000)
        tag = f"micro-{self.contract.run_id[-10:]}-{asset.lower()}"
        protected = {
            "binance": protected_limit(
                b_price,
                side="buy",
                adverse_bps=self.contract.max_adverse_execution_bps,
                tick=b_tick,
            ),
            "bybit": protected_limit(
                y_price,
                side="sell",
                adverse_bps=self.contract.max_adverse_execution_bps,
                tick=y_tick,
            ),
            "hyperliquid": hyperliquid_protected_limit(
                h_price,
                side="sell",
                adverse_bps=self.contract.max_adverse_execution_bps,
                sz_decimals=h_sz_decimals,
            ),
        }
        try:
            perp_orders, _ = _run_parallel({"bybit": lambda: self.bybit.create_ioc(symbol=perp, side="Sell", quantity=y_qty, price=protected["bybit"], client_id=f"{tag}-yi", reduce_only=False), "hyperliquid": lambda: self.hyperliquid.create_ioc(asset=asset, is_buy=False, quantity=h_qty, price=protected["hyperliquid"], client_id=f"{tag}-hi", reduce_only=False)})
            perp_fills = self._wait_fills(asset, start_ms=start_ms, end_ms=int(time.time() * 1000), expected_venues=("bybit", "hyperliquid"))
            y_filled, _ = _aggregate_fills(perp_fills["bybit"], venue="bybit", side="sell", symbol=perp)
            h_indexed, _ = _aggregate_fills(perp_fills["hyperliquid"], venue="hyperliquid", side="sell", symbol=asset)
            h_filled = h_indexed or _hyperliquid_ioc_filled_quantity(perp_orders["hyperliquid"])
            filled = {"binance": Decimal("0"), "bybit": y_filled, "hyperliquid": h_filled}
            if y_filled != y_qty or h_filled != h_qty:
                unwound = self._safe_unwind(asset, filled=filled, books=books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": int(time.time() * 1000) - start_ms, "delta_mismatch_fraction": None, "entry": {"fills": {"bybit": _aggregate_fills(perp_fills["bybit"], venue="bybit", side="sell", symbol=perp)[1], "hyperliquid": _aggregate_fills(perp_fills["hyperliquid"], venue="hyperliquid", side="sell", symbol=asset)[1]}}, "safe_unwind_attempted": True}
            spot_qty = (
                round_to_step(y_filled + h_filled, b_step)
                if self.contract.spot_quantity_mode == "match_rounded_perps"
                else b_qty
            )
            prospective_delta = abs(spot_qty - y_filled - h_filled) / spot_qty
            if prospective_delta > self.contract.max_delta_mismatch_fraction:
                unwound = self._safe_unwind(asset, filled=filled, books=books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": int(time.time() * 1000) - start_ms, "delta_mismatch_fraction": None, "safe_unwind_attempted": True}
            self.binance.create_ioc(symbol=symbol, side="BUY", quantity=spot_qty, price=protected["binance"], client_id=f"{tag}-bi")
            all_fills = self._wait_fills(asset, start_ms=start_ms, end_ms=int(time.time() * 1000), expected_venues=("binance", "bybit", "hyperliquid"))
            b_filled, b_summary = _aggregate_fills(all_fills["binance"], venue="binance", side="buy", symbol=symbol)
            if b_filled != spot_qty or int(time.time() * 1000) - start_ms > self.contract.entry_deadline_seconds * 1000:
                filled["binance"] = b_filled
                unwound = self._safe_unwind(asset, filled=filled, books=books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": int(time.time() * 1000) - start_ms, "delta_mismatch_fraction": None, "entry": {"fills": {"binance": b_summary}}, "safe_unwind_attempted": True}
            binance_base_fee = sum(
                (
                    _decimal(row.get("commission", "0"), field="binance_base_fee")
                    for row in all_fills["binance"]
                    if str(row.get("symbol") or "") == symbol
                    and bool(row.get("isBuyer"))
                    and str(row.get("commissionAsset") or "") == asset
                ),
                Decimal("0"),
            )
            binance_position_qty = max(Decimal("0"), b_filled - binance_base_fee)
            delta_mismatch = abs(binance_position_qty - y_filled - h_filled) / binance_position_qty
            entry_snapshot = self._account_snapshot(asset)
            if delta_mismatch > self.contract.max_delta_mismatch_fraction or not entry_snapshot["liquidation_distance_passed"]:
                unwound = self._safe_unwind(asset, filled={"binance": b_filled, "bybit": y_filled, "hyperliquid": h_filled}, books=books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": int(time.time() * 1000) - start_ms, "delta_mismatch_fraction": float(delta_mismatch), "entry_snapshot": entry_snapshot, "safe_unwind_attempted": True}
            time.sleep(self.contract.hold_seconds)
            exit_books, exit_span = self._market_snapshot(asset)
            if exit_span > self.contract.max_snapshot_sync_span_ms:
                unwound = self._safe_unwind(asset, filled={"binance": b_filled, "bybit": y_filled, "hyperliquid": h_filled}, books=books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": int(time.time() * 1000) - start_ms, "delta_mismatch_fraction": float(delta_mismatch), "entry_snapshot": entry_snapshot, "safe_unwind_attempted": True}
            # Close all three legs promptly; the spot order first avoids holding an accidental net long.
            exit_start = int(time.time() * 1000)
            binance_exit_available = _binance_free_balance(self.binance.account(), asset)
            binance_exit_qty = round_to_step(min(binance_position_qty, binance_exit_available), b_step)
            binance_exit_bid = _best_price(exit_books["binance"], venue="binance", side="sell")
            if binance_exit_qty < b_min or binance_exit_qty * binance_exit_bid < b_min_notional:
                unwound = self._safe_unwind(asset, filled={"binance": binance_position_qty, "bybit": y_filled, "hyperliquid": h_filled}, books=exit_books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": exit_start - start_ms, "delta_mismatch_fraction": float(delta_mismatch), "entry_snapshot": entry_snapshot, "safe_unwind_attempted": True}
            _run_parallel({
                "binance": lambda: self.binance.create_ioc(symbol=symbol, side="SELL", quantity=binance_exit_qty, price=protected_limit(binance_exit_bid, side="sell", adverse_bps=self.contract.max_adverse_execution_bps, tick=b_tick), client_id=f"{tag}-bo"),
                "bybit": lambda: self.bybit.create_ioc(symbol=perp, side="Buy", quantity=y_filled, price=protected_limit(_best_price(exit_books["bybit"], venue="bybit", side="buy"), side="buy", adverse_bps=self.contract.max_adverse_execution_bps, tick=y_tick), client_id=f"{tag}-yo", reduce_only=True),
                "hyperliquid": lambda: self.hyperliquid.create_ioc(
                    asset=asset,
                    is_buy=True,
                    quantity=h_filled,
                    price=hyperliquid_protected_limit(
                        _best_price(exit_books["hyperliquid"], venue="hyperliquid", side="buy"),
                        side="buy",
                        adverse_bps=self.contract.max_adverse_execution_bps,
                        sz_decimals=h_sz_decimals,
                    ),
                    client_id=f"{tag}-ho",
                    reduce_only=True,
                ),
            })
            exit_fills = self._wait_fills(asset, start_ms=exit_start, end_ms=int(time.time() * 1000), expected_venues=("binance", "bybit", "hyperliquid"))
            exit_qty = {"binance": _aggregate_fills(exit_fills["binance"], venue="binance", side="sell", symbol=symbol)[0], "bybit": _aggregate_fills(exit_fills["bybit"], venue="bybit", side="buy", symbol=perp)[0], "hyperliquid": _aggregate_fills(exit_fills["hyperliquid"], venue="hyperliquid", side="buy", symbol=asset)[0]}
            exit_ok = exit_qty == {"binance": binance_exit_qty, "bybit": y_filled, "hyperliquid": h_filled}
            exit_snapshot = self._account_snapshot(asset)
            binance_remaining = _binance_asset_balance(self.binance.account(), asset)
            binance_post_exit_bid = _best_price(self.binance.book(symbol), venue="binance", side="sell")
            binance_flat = binance_remaining < b_step or binance_remaining * binance_post_exit_bid < b_min_notional
            exit_snapshot["binance"] = {"has_target_position": not binance_flat, "dust_only": binance_remaining > 0 and binance_flat}
            if not exit_ok or not binance_flat or exit_snapshot["bybit"]["has_target_position"] or exit_snapshot["hyperliquid"]["has_target_position"]:
                unwound = self._safe_unwind(asset, filled={"binance": max(Decimal("0"), binance_position_qty - exit_qty["binance"]), "bybit": max(Decimal("0"), y_filled - exit_qty["bybit"]), "hyperliquid": max(Decimal("0"), h_filled - exit_qty["hyperliquid"])}, books=exit_books, client_tag=tag)
                return {"asset": asset, "status": "failed_safe_unwind" if unwound else "failed_manual_intervention", "entry_span_ms": exit_start - start_ms, "exit_span_ms": int(time.time() * 1000) - exit_start, "delta_mismatch_fraction": float(delta_mismatch), "entry_snapshot": entry_snapshot, "exit_snapshot": exit_snapshot, "safe_unwind_attempted": True}
            return {"asset": asset, "status": "completed", "entry_span_ms": exit_start - start_ms - self.contract.hold_seconds * 1000, "exit_span_ms": int(time.time() * 1000) - exit_start, "delta_mismatch_fraction": float(delta_mismatch), "entry_snapshot": entry_snapshot, "exit_snapshot": exit_snapshot, "entry": {"fills": {"binance": b_summary, "bybit": _aggregate_fills(all_fills["bybit"], venue="bybit", side="sell", symbol=perp)[1], "hyperliquid": _aggregate_fills(all_fills["hyperliquid"], venue="hyperliquid", side="sell", symbol=asset)[1]}}, "exit": {"fills": {"binance": _aggregate_fills(exit_fills["binance"], venue="binance", side="sell", symbol=symbol)[1], "bybit": _aggregate_fills(exit_fills["bybit"], venue="bybit", side="buy", symbol=perp)[1], "hyperliquid": _aggregate_fills(exit_fills["hyperliquid"], venue="hyperliquid", side="buy", symbol=asset)[1]}}, "implementation_shortfall_bps": {"binance_entry": _shortfall_bps(all_fills["binance"], venue="binance", symbol=symbol, side="buy", protected_price=protected["binance"]), "bybit_entry": _shortfall_bps(all_fills["bybit"], venue="bybit", symbol=perp, side="sell", protected_price=protected["bybit"]), "hyperliquid_entry": _shortfall_bps(all_fills["hyperliquid"], venue="hyperliquid", symbol=asset, side="sell", protected_price=protected["hyperliquid"]), }, "safe_unwind_attempted": False}
        except Exception as error:
            # If we cannot prove state, attempt all currently known legs and force stop before the next asset.
            unwound = self._safe_unwind(asset, filled={"binance": Decimal("0"), "bybit": Decimal("0"), "hyperliquid": Decimal("0")}, books=books, client_tag=tag)
            return {
                "asset": asset,
                "status": "failed_safe_unwind" if unwound else "failed_manual_intervention",
                "entry_span_ms": int(time.time() * 1000) - start_ms,
                "delta_mismatch_fraction": None,
                "safe_error": _safe_error_code(error),
                "safe_unwind_attempted": True,
            }


def sanitized_bundle(contract: MicroExecutionContract, *, preflight: Mapping[str, Any], asset_results: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    rows = list(asset_results)
    completed = [row for row in rows if row.get("status") == "completed"]
    status = "micro_execution_evidence_collected" if len(completed) == len(contract.assets) else "micro_execution_evidence_incomplete"
    bundle = {
        "schema_version": 1,
        "status": status,
        "run_id": contract.run_id,
        "contract_sha256": contract_payload(contract)["contract_sha256"],
        "preflight_passed": preflight.get("status") == "passed",
        "asset_cycles": rows,
        "acceptance": {
            "completed_btc_eth_cycles": len(completed) == len(contract.assets),
            "all_entry_delta_within_0_1_percent": all((row.get("delta_mismatch_fraction") or 0.0) <= float(contract.max_delta_mismatch_fraction) for row in completed),
            "production_gate_remains_closed": True,
            "forward_oos_required_days": 90,
            "intraday_paired_cycles_required": 100,
        },
    }
    assert_sanitized(bundle)
    return bundle
