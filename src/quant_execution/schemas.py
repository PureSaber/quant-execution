"""Frozen JSON and Arrow schemas for execution facts."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any

import pyarrow as pa
from jsonschema import Draft202012Validator, FormatChecker
from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import (
    AccountSnapshot,
    Fee,
    Fill,
    Funding,
    LedgerTransaction,
    Order,
    OrderEvent,
    OrderIntent,
    Posting,
    RunResult,
    Settlement,
)

SCHEMA_VERSION = "1.0.0"

ORDER_INTENT_SCHEMA_ID = "puresaber.execution.order-intent"
ORDER_SCHEMA_ID = "puresaber.execution.order"
ORDER_EVENT_SCHEMA_ID = "puresaber.execution.order-event"
FILL_SCHEMA_ID = "puresaber.execution.fill"
FEE_SCHEMA_ID = "puresaber.execution.fee"
FUNDING_SCHEMA_ID = "puresaber.execution.funding"
SETTLEMENT_SCHEMA_ID = "puresaber.execution.settlement"
LEDGER_TRANSACTION_SCHEMA_ID = "puresaber.execution.ledger-transaction"
ACCOUNT_SNAPSHOT_SCHEMA_ID = "puresaber.execution.account-snapshot"
RUN_RESULT_SCHEMA_ID = "puresaber.execution.run-result"

_UTC = pa.timestamp("ns", tz="UTC")
_FIXED_POINT = pa.struct(
    [
        pa.field("units", pa.int64(), nullable=False),
        pa.field("scale", pa.int16(), nullable=False),
    ]
)
_ORDER_INTENT = pa.struct(
    [
        pa.field("idempotency_key", pa.string(), nullable=False),
        pa.field("account_id", pa.string(), nullable=False),
        pa.field("strategy_id", pa.string(), nullable=False),
        pa.field("instrument_id", pa.string(), nullable=False),
        pa.field("side", pa.string(), nullable=False),
        pa.field("quantity", _FIXED_POINT, nullable=False),
        pa.field("order_type", pa.string(), nullable=False),
        pa.field("time_in_force", pa.string(), nullable=False),
        pa.field("created_at", _UTC, nullable=False),
        pa.field("limit_price", _FIXED_POINT, nullable=True),
        pa.field("stop_price", _FIXED_POINT, nullable=True),
        pa.field("reduce_only", pa.bool_(), nullable=False),
    ]
)
_POSTING = pa.struct(
    [
        pa.field("ledger_account", pa.string(), nullable=False),
        pa.field("currency", pa.string(), nullable=False),
        pa.field("amount", _FIXED_POINT, nullable=False),
        pa.field("instrument_id", pa.string(), nullable=True),
        pa.field("quantity_delta", _FIXED_POINT, nullable=True),
    ]
)
_POSTING_LIST = pa.list_(pa.field("item", _POSTING, nullable=False))
_FIXED_POINT_MAP = pa.map_(
    pa.field("key", pa.string(), nullable=False),
    pa.field("value", _FIXED_POINT, nullable=False),
)

_ARROW_SCHEMAS: dict[str, pa.Schema] = {
    ORDER_INTENT_SCHEMA_ID: pa.schema(list(_ORDER_INTENT)),
    ORDER_SCHEMA_ID: pa.schema(
        [
            pa.field("order_id", pa.string(), nullable=False),
            pa.field("intent", _ORDER_INTENT, nullable=False),
            pa.field("status", pa.string(), nullable=False),
            pa.field("filled_quantity", _FIXED_POINT, nullable=False),
            pa.field("version", pa.int64(), nullable=False),
        ]
    ),
    ORDER_EVENT_SCHEMA_ID: pa.schema(
        [
            pa.field("event_id", pa.string(), nullable=False),
            pa.field("order_id", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("sequence", pa.int64(), nullable=False),
            pa.field("from_status", pa.string(), nullable=False),
            pa.field("to_status", pa.string(), nullable=False),
            pa.field("fill_quantity", _FIXED_POINT, nullable=True),
            pa.field("reason", pa.string(), nullable=False),
        ]
    ),
    FILL_SCHEMA_ID: pa.schema(
        [
            pa.field("fill_id", pa.string(), nullable=False),
            pa.field("order_id", pa.string(), nullable=False),
            pa.field("account_id", pa.string(), nullable=False),
            pa.field("strategy_id", pa.string(), nullable=False),
            pa.field("instrument_id", pa.string(), nullable=False),
            pa.field("side", pa.string(), nullable=False),
            pa.field("quantity", _FIXED_POINT, nullable=False),
            pa.field("price", _FIXED_POINT, nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("liquidity_role", pa.string(), nullable=False),
            pa.field("venue_trade_id", pa.string(), nullable=True),
        ]
    ),
    FEE_SCHEMA_ID: pa.schema(
        [
            pa.field("fee_id", pa.string(), nullable=False),
            pa.field("fill_id", pa.string(), nullable=False),
            pa.field("account_id", pa.string(), nullable=False),
            pa.field("amount", _FIXED_POINT, nullable=False),
            pa.field("currency", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("fee_type", pa.string(), nullable=False),
        ]
    ),
    FUNDING_SCHEMA_ID: pa.schema(
        [
            pa.field("funding_id", pa.string(), nullable=False),
            pa.field("account_id", pa.string(), nullable=False),
            pa.field("instrument_id", pa.string(), nullable=False),
            pa.field("amount", _FIXED_POINT, nullable=False),
            pa.field("currency", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
        ]
    ),
    SETTLEMENT_SCHEMA_ID: pa.schema(
        [
            pa.field("settlement_id", pa.string(), nullable=False),
            pa.field("account_id", pa.string(), nullable=False),
            pa.field("instrument_id", pa.string(), nullable=False),
            pa.field("amount", _FIXED_POINT, nullable=False),
            pa.field("currency", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("settlement_type", pa.string(), nullable=False),
        ]
    ),
    LEDGER_TRANSACTION_SCHEMA_ID: pa.schema(
        [
            pa.field("transaction_id", pa.string(), nullable=False),
            pa.field("idempotency_key", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("event_type", pa.string(), nullable=False),
            pa.field("reference_id", pa.string(), nullable=False),
            pa.field("postings", _POSTING_LIST, nullable=False),
        ]
    ),
    ACCOUNT_SNAPSHOT_SCHEMA_ID: pa.schema(
        [
            pa.field("account_id", pa.string(), nullable=False),
            pa.field("event_time", _UTC, nullable=False),
            pa.field("base_currency", pa.string(), nullable=False),
            pa.field("cash_balances", _FIXED_POINT_MAP, nullable=False),
            pa.field("positions", _FIXED_POINT_MAP, nullable=False),
            pa.field("nav", _FIXED_POINT, nullable=False),
            pa.field("cost_basis", _FIXED_POINT_MAP, nullable=False),
            pa.field("realized_pnl", _FIXED_POINT_MAP, nullable=False),
            pa.field("unrealized_pnl", _FIXED_POINT_MAP, nullable=False),
            pa.field("initial_margin", _FIXED_POINT, nullable=False),
            pa.field("maintenance_margin", _FIXED_POINT, nullable=False),
            pa.field("liquidation_required", pa.bool_(), nullable=False),
        ]
    ),
    RUN_RESULT_SCHEMA_ID: pa.schema(
        [
            pa.field("run_id", pa.string(), nullable=False),
            pa.field("seed", pa.int64(), nullable=False),
            pa.field("event_count", pa.int64(), nullable=False),
            pa.field("order_count", pa.int64(), nullable=False),
            pa.field("fill_count", pa.int64(), nullable=False),
            pa.field("order_sha256", pa.string(), nullable=False),
            pa.field("fill_sha256", pa.string(), nullable=False),
            pa.field("ledger_sha256", pa.string(), nullable=False),
            pa.field("result_sha256", pa.string(), nullable=False),
        ]
    ),
}

_FIXED_POINT_JSON = {
    "type": "object",
    "additionalProperties": False,
    "required": ["units", "scale"],
    "properties": {
        "units": {"type": "integer", "minimum": -(2**63), "maximum": 2**63 - 1},
        "scale": {"type": "integer", "minimum": 0, "maximum": 18},
    },
}
_NULLABLE_FIXED_POINT_JSON = {"oneOf": [_FIXED_POINT_JSON, {"type": "null"}]}
_UTC_JSON = {"type": "string", "format": "date-time", "pattern": "Z$"}
_TEXT = {"type": "string", "minLength": 1}
_CURRENCY = {"type": "string", "pattern": "^[A-Z0-9]{3,12}$"}
_SHA256 = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_FIXED_POINT_MAP_JSON = {
    "type": "object",
    "additionalProperties": _FIXED_POINT_JSON,
}


def _object_schema(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": properties,
    }


_ORDER_INTENT_PROPERTIES = {
    "idempotency_key": _TEXT,
    "account_id": _TEXT,
    "strategy_id": _TEXT,
    "instrument_id": _TEXT,
    "side": {"enum": ["buy", "sell"]},
    "quantity": _FIXED_POINT_JSON,
    "order_type": {"enum": ["market", "limit", "stop", "stop_limit"]},
    "time_in_force": {"enum": ["day", "gtc", "ioc", "fok"]},
    "created_at": _UTC_JSON,
    "limit_price": _NULLABLE_FIXED_POINT_JSON,
    "stop_price": _NULLABLE_FIXED_POINT_JSON,
    "reduce_only": {"type": "boolean"},
}
_ORDER_INTENT_JSON = _object_schema(
    _ORDER_INTENT_PROPERTIES, list(_ARROW_SCHEMAS[ORDER_INTENT_SCHEMA_ID].names)
)
_ORDER_INTENT_JSON["allOf"] = [
    {
        "oneOf": [
            {
                "properties": {
                    "order_type": {"const": "market"},
                    "limit_price": {"type": "null"},
                    "stop_price": {"type": "null"},
                }
            },
            {
                "properties": {
                    "order_type": {"const": "limit"},
                    "limit_price": _FIXED_POINT_JSON,
                    "stop_price": {"type": "null"},
                }
            },
            {
                "properties": {
                    "order_type": {"const": "stop"},
                    "limit_price": {"type": "null"},
                    "stop_price": _FIXED_POINT_JSON,
                }
            },
            {
                "properties": {
                    "order_type": {"const": "stop_limit"},
                    "limit_price": _FIXED_POINT_JSON,
                    "stop_price": _FIXED_POINT_JSON,
                }
            },
        ]
    }
]
_ORDER_STATUSES = [
    "created",
    "accepted",
    "partially_filled",
    "filled",
    "cancelled",
    "rejected",
    "expired",
]
_POSTING_JSON = _object_schema(
    {
        "ledger_account": _TEXT,
        "currency": _CURRENCY,
        "amount": _FIXED_POINT_JSON,
        "instrument_id": {"type": ["string", "null"]},
        "quantity_delta": _NULLABLE_FIXED_POINT_JSON,
    },
    list(_POSTING.names),
)

_JSON_SCHEMAS: dict[str, dict[str, Any]] = {
    ORDER_INTENT_SCHEMA_ID: _ORDER_INTENT_JSON,
    ORDER_SCHEMA_ID: _object_schema(
        {
            "order_id": _TEXT,
            "intent": _ORDER_INTENT_JSON,
            "status": {"enum": _ORDER_STATUSES},
            "filled_quantity": _FIXED_POINT_JSON,
            "version": {"type": "integer", "minimum": 0},
        },
        list(_ARROW_SCHEMAS[ORDER_SCHEMA_ID].names),
    ),
    ORDER_EVENT_SCHEMA_ID: _object_schema(
        {
            "event_id": _TEXT,
            "order_id": _TEXT,
            "event_time": _UTC_JSON,
            "sequence": {"type": "integer", "minimum": 1},
            "from_status": {"enum": _ORDER_STATUSES},
            "to_status": {"enum": _ORDER_STATUSES},
            "fill_quantity": _NULLABLE_FIXED_POINT_JSON,
            "reason": {"type": "string"},
        },
        list(_ARROW_SCHEMAS[ORDER_EVENT_SCHEMA_ID].names),
    ),
    FILL_SCHEMA_ID: _object_schema(
        {
            "fill_id": _TEXT,
            "order_id": _TEXT,
            "account_id": _TEXT,
            "strategy_id": _TEXT,
            "instrument_id": _TEXT,
            "side": {"enum": ["buy", "sell"]},
            "quantity": _FIXED_POINT_JSON,
            "price": _FIXED_POINT_JSON,
            "event_time": _UTC_JSON,
            "liquidity_role": {"enum": ["maker", "taker", "unknown"]},
            "venue_trade_id": {"type": ["string", "null"]},
        },
        list(_ARROW_SCHEMAS[FILL_SCHEMA_ID].names),
    ),
    FEE_SCHEMA_ID: _object_schema(
        {
            "fee_id": _TEXT,
            "fill_id": _TEXT,
            "account_id": _TEXT,
            "amount": _FIXED_POINT_JSON,
            "currency": _CURRENCY,
            "event_time": _UTC_JSON,
            "fee_type": _TEXT,
        },
        list(_ARROW_SCHEMAS[FEE_SCHEMA_ID].names),
    ),
    FUNDING_SCHEMA_ID: _object_schema(
        {
            "funding_id": _TEXT,
            "account_id": _TEXT,
            "instrument_id": _TEXT,
            "amount": _FIXED_POINT_JSON,
            "currency": _CURRENCY,
            "event_time": _UTC_JSON,
        },
        list(_ARROW_SCHEMAS[FUNDING_SCHEMA_ID].names),
    ),
    SETTLEMENT_SCHEMA_ID: _object_schema(
        {
            "settlement_id": _TEXT,
            "account_id": _TEXT,
            "instrument_id": _TEXT,
            "amount": _FIXED_POINT_JSON,
            "currency": _CURRENCY,
            "event_time": _UTC_JSON,
            "settlement_type": _TEXT,
        },
        list(_ARROW_SCHEMAS[SETTLEMENT_SCHEMA_ID].names),
    ),
    LEDGER_TRANSACTION_SCHEMA_ID: _object_schema(
        {
            "transaction_id": _TEXT,
            "idempotency_key": _TEXT,
            "event_time": _UTC_JSON,
            "event_type": {
                "enum": [
                    "fill",
                    "fee",
                    "funding",
                    "settlement",
                    "corporate_action",
                    "fx_conversion",
                ]
            },
            "reference_id": _TEXT,
            "postings": {"type": "array", "minItems": 2, "items": _POSTING_JSON},
        },
        list(_ARROW_SCHEMAS[LEDGER_TRANSACTION_SCHEMA_ID].names),
    ),
    ACCOUNT_SNAPSHOT_SCHEMA_ID: _object_schema(
        {
            "account_id": _TEXT,
            "event_time": _UTC_JSON,
            "base_currency": _CURRENCY,
            "cash_balances": _FIXED_POINT_MAP_JSON,
            "positions": _FIXED_POINT_MAP_JSON,
            "nav": _FIXED_POINT_JSON,
            "cost_basis": _FIXED_POINT_MAP_JSON,
            "realized_pnl": _FIXED_POINT_MAP_JSON,
            "unrealized_pnl": _FIXED_POINT_MAP_JSON,
            "initial_margin": _FIXED_POINT_JSON,
            "maintenance_margin": _FIXED_POINT_JSON,
            "liquidation_required": {"type": "boolean"},
        },
        list(_ARROW_SCHEMAS[ACCOUNT_SNAPSHOT_SCHEMA_ID].names),
    ),
    RUN_RESULT_SCHEMA_ID: _object_schema(
        {
            "run_id": _TEXT,
            "seed": {"type": "integer", "minimum": 0},
            "event_count": {"type": "integer", "minimum": 0},
            "order_count": {"type": "integer", "minimum": 0},
            "fill_count": {"type": "integer", "minimum": 0},
            "order_sha256": _SHA256,
            "fill_sha256": _SHA256,
            "ledger_sha256": _SHA256,
            "result_sha256": _SHA256,
        },
        list(_ARROW_SCHEMAS[RUN_RESULT_SCHEMA_ID].names),
    ),
}

_JSON_SCHEMAS[ORDER_EVENT_SCHEMA_ID]["allOf"] = [
    {
        "oneOf": [
            {
                "properties": {
                    "from_status": {"const": "created"},
                    "to_status": {"enum": ["accepted", "rejected"]},
                }
            },
            {
                "properties": {
                    "from_status": {"const": "accepted"},
                    "to_status": {"enum": ["partially_filled", "filled", "cancelled", "expired"]},
                }
            },
            {
                "properties": {
                    "from_status": {"const": "partially_filled"},
                    "to_status": {"enum": ["partially_filled", "filled", "cancelled", "expired"]},
                }
            },
        ]
    },
    {
        "oneOf": [
            {
                "properties": {
                    "to_status": {"enum": ["partially_filled", "filled"]},
                    "fill_quantity": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["units", "scale"],
                        "properties": {
                            "units": {"type": "integer", "minimum": 1},
                            "scale": {"type": "integer", "minimum": 0, "maximum": 18},
                        },
                    },
                }
            },
            {
                "properties": {
                    "to_status": {"enum": ["accepted", "cancelled", "rejected", "expired"]},
                    "fill_quantity": {"type": "null"},
                }
            },
        ]
    },
]


def _time(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _fixed(value: FixedPoint | None) -> dict[str, int] | None:
    return None if value is None else {"units": value.units, "scale": value.scale}


def _intent_payload(intent: OrderIntent) -> dict[str, Any]:
    return {
        "idempotency_key": intent.idempotency_key,
        "account_id": intent.account_id,
        "strategy_id": intent.strategy_id,
        "instrument_id": intent.instrument_id,
        "side": intent.side.value,
        "quantity": _fixed(intent.quantity),
        "order_type": intent.order_type.value,
        "time_in_force": intent.time_in_force.value,
        "created_at": _time(intent.created_at),
        "limit_price": _fixed(intent.limit_price),
        "stop_price": _fixed(intent.stop_price),
        "reduce_only": intent.reduce_only,
    }


def _posting_payload(posting: Posting) -> dict[str, Any]:
    return {
        "ledger_account": posting.ledger_account,
        "currency": posting.currency,
        "amount": _fixed(posting.amount),
        "instrument_id": posting.instrument_id,
        "quantity_delta": _fixed(posting.quantity_delta),
    }


def _fixed_map(values: Mapping[str, FixedPoint]) -> dict[str, dict[str, int]]:
    return {key: _fixed(values[key]) for key in sorted(values)}


def execution_payload(value: object) -> dict[str, Any]:
    if isinstance(value, OrderIntent):
        return _intent_payload(value)
    if isinstance(value, Order):
        return {
            "order_id": value.order_id,
            "intent": _intent_payload(value.intent),
            "status": value.status.value,
            "filled_quantity": _fixed(value.filled_quantity),
            "version": value.version,
        }
    if isinstance(value, OrderEvent):
        return {
            "event_id": value.event_id,
            "order_id": value.order_id,
            "event_time": _time(value.event_time),
            "sequence": value.sequence,
            "from_status": value.from_status.value,
            "to_status": value.to_status.value,
            "fill_quantity": _fixed(value.fill_quantity),
            "reason": value.reason,
        }
    if isinstance(value, Fill):
        return {
            "fill_id": value.fill_id,
            "order_id": value.order_id,
            "account_id": value.account_id,
            "strategy_id": value.strategy_id,
            "instrument_id": value.instrument_id,
            "side": value.side.value,
            "quantity": _fixed(value.quantity),
            "price": _fixed(value.price),
            "event_time": _time(value.event_time),
            "liquidity_role": value.liquidity_role.value,
            "venue_trade_id": value.venue_trade_id,
        }
    if isinstance(value, Fee):
        return {
            "fee_id": value.fee_id,
            "fill_id": value.fill_id,
            "account_id": value.account_id,
            "amount": _fixed(value.amount),
            "currency": value.currency,
            "event_time": _time(value.event_time),
            "fee_type": value.fee_type,
        }
    if isinstance(value, Funding):
        return {
            "funding_id": value.funding_id,
            "account_id": value.account_id,
            "instrument_id": value.instrument_id,
            "amount": _fixed(value.amount),
            "currency": value.currency,
            "event_time": _time(value.event_time),
        }
    if isinstance(value, Settlement):
        return {
            "settlement_id": value.settlement_id,
            "account_id": value.account_id,
            "instrument_id": value.instrument_id,
            "amount": _fixed(value.amount),
            "currency": value.currency,
            "event_time": _time(value.event_time),
            "settlement_type": value.settlement_type,
        }
    if isinstance(value, LedgerTransaction):
        return {
            "transaction_id": value.transaction_id,
            "idempotency_key": value.idempotency_key,
            "event_time": _time(value.event_time),
            "event_type": value.event_type.value,
            "reference_id": value.reference_id,
            "postings": [_posting_payload(posting) for posting in value.postings],
        }
    if isinstance(value, AccountSnapshot):
        return {
            "account_id": value.account_id,
            "event_time": _time(value.event_time),
            "base_currency": value.base_currency,
            "cash_balances": _fixed_map(value.cash_balances),
            "positions": _fixed_map(value.positions),
            "nav": _fixed(value.nav),
            "cost_basis": _fixed_map(value.cost_basis),
            "realized_pnl": _fixed_map(value.realized_pnl),
            "unrealized_pnl": _fixed_map(value.unrealized_pnl),
            "initial_margin": _fixed(value.initial_margin),
            "maintenance_margin": _fixed(value.maintenance_margin),
            "liquidation_required": value.liquidation_required,
        }
    if isinstance(value, RunResult):
        return {
            "run_id": value.run_id,
            "seed": value.seed,
            "event_count": value.event_count,
            "order_count": value.order_count,
            "fill_count": value.fill_count,
            "order_sha256": value.order_sha256,
            "fill_sha256": value.fill_sha256,
            "ledger_sha256": value.ledger_sha256,
            "result_sha256": value.result_sha256,
        }
    raise TypeError(f"Unsupported execution contract: {type(value).__name__}")


def get_arrow_schema(schema_id: str, version: str = SCHEMA_VERSION) -> pa.Schema:
    if version != SCHEMA_VERSION:
        raise ValidationError(f"Unsupported execution schema: {schema_id}@{version}")
    try:
        return _ARROW_SCHEMAS[schema_id]
    except KeyError as exc:
        raise ValidationError(f"Unknown execution schema ID: {schema_id}") from exc


def get_json_schema(schema_id: str, version: str = SCHEMA_VERSION) -> dict[str, Any]:
    if version != SCHEMA_VERSION:
        raise ValidationError(f"Unsupported execution schema: {schema_id}@{version}")
    try:
        return deepcopy(_JSON_SCHEMAS[schema_id])
    except KeyError as exc:
        raise ValidationError(f"Unknown execution schema ID: {schema_id}") from exc


def validate_json_record(
    schema_id: str, payload: dict[str, Any], version: str = SCHEMA_VERSION
) -> None:
    validator = Draft202012Validator(
        get_json_schema(schema_id, version), format_checker=FormatChecker()
    )
    errors = sorted(validator.iter_errors(payload), key=lambda error: list(error.path))
    if errors:
        detail = "; ".join(error.message for error in errors)
        raise ValidationError(f"JSON schema validation failed for {schema_id}: {detail}")
    if schema_id == LEDGER_TRANSACTION_SCHEMA_ID:
        balances: dict[str, Decimal] = {}
        for posting in payload["postings"]:
            fixed = posting["amount"]
            amount = Decimal(fixed["units"]).scaleb(-fixed["scale"])
            currency = posting["currency"]
            balances[currency] = balances.get(currency, Decimal(0)) + amount
        unbalanced = {currency: total for currency, total in balances.items() if total != 0}
        if unbalanced:
            raise ValidationError(f"Ledger JSON record is unbalanced: {unbalanced}")


def validate_arrow_table(schema_id: str, table: pa.Table, version: str = SCHEMA_VERSION) -> None:
    expected = get_arrow_schema(schema_id, version)
    if table.schema != expected:
        raise ValidationError(
            f"Arrow schema mismatch for {schema_id}: expected={expected}, actual={table.schema}"
        )
