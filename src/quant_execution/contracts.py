"""Frozen execution, fill and double-entry ledger contracts."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
import re
from types import MappingProxyType
from typing import Mapping, TypeAlias

from quant_data_kit import CorporateActionEvent, FixedPoint, ensure_utc_datetime
from quant_data_kit.exceptions import ValidationError


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    STOP_LIMIT = "stop_limit"


class TimeInForce(str, Enum):
    DAY = "day"
    GTC = "gtc"
    IOC = "ioc"
    FOK = "fok"


class OrderStatus(str, Enum):
    CREATED = "created"
    ACCEPTED = "accepted"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    EXPIRED = "expired"


class LiquidityRole(str, Enum):
    MAKER = "maker"
    TAKER = "taker"
    UNKNOWN = "unknown"


class LedgerEventType(str, Enum):
    FILL = "fill"
    FEE = "fee"
    FUNDING = "funding"
    SETTLEMENT = "settlement"
    CORPORATE_ACTION = "corporate_action"
    FX_CONVERSION = "fx_conversion"


_CURRENCY_PATTERN = re.compile(r"^[A-Z0-9]{3,12}$")


def _required_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive(value: FixedPoint, field_name: str) -> None:
    if not isinstance(value, FixedPoint) or not value.is_positive():
        raise ValidationError(f"{field_name} must be a positive FixedPoint")


def _currency(value: str, field_name: str = "currency") -> str:
    value = _required_text(value, field_name)
    if not _CURRENCY_PATTERN.fullmatch(value):
        raise ValidationError(f"{field_name} must be an uppercase currency code")
    return value


def _immutable_fixed_point_map(
    values: Mapping[str, FixedPoint], field_name: str
) -> Mapping[str, FixedPoint]:
    if not isinstance(values, Mapping):
        raise ValidationError(f"{field_name} must be a mapping")
    result: dict[str, FixedPoint] = {}
    for key, value in values.items():
        result[_required_text(key, f"{field_name} key")] = value
        if not isinstance(value, FixedPoint):
            raise ValidationError(f"{field_name}[{key!r}] must be a FixedPoint")
    return MappingProxyType(result)


@dataclass(frozen=True, kw_only=True)
class OrderIntent:
    idempotency_key: str
    account_id: str
    strategy_id: str
    instrument_id: str
    side: Side
    quantity: FixedPoint
    order_type: OrderType
    time_in_force: TimeInForce
    created_at: datetime
    limit_price: FixedPoint | None = None
    stop_price: FixedPoint | None = None
    reduce_only: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "idempotency_key",
            "account_id",
            "strategy_id",
            "instrument_id",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if not isinstance(self.side, Side):
            raise ValidationError("side must be a Side")
        if not isinstance(self.order_type, OrderType):
            raise ValidationError("order_type must be an OrderType")
        if not isinstance(self.time_in_force, TimeInForce):
            raise ValidationError("time_in_force must be a TimeInForce")
        if not isinstance(self.reduce_only, bool):
            raise ValidationError("reduce_only must be boolean")
        _positive(self.quantity, "quantity")
        object.__setattr__(
            self, "created_at", ensure_utc_datetime(self.created_at, field="created_at")
        )
        if self.limit_price is not None:
            _positive(self.limit_price, "limit_price")
        if self.stop_price is not None:
            _positive(self.stop_price, "stop_price")
        requires_limit = self.order_type in {OrderType.LIMIT, OrderType.STOP_LIMIT}
        requires_stop = self.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
        if (self.limit_price is not None) != requires_limit:
            raise ValidationError(f"{self.order_type.value} limit_price requirement violated")
        if (self.stop_price is not None) != requires_stop:
            raise ValidationError(f"{self.order_type.value} stop_price requirement violated")


@dataclass(frozen=True, kw_only=True)
class Order:
    order_id: str
    intent: OrderIntent
    status: OrderStatus = OrderStatus.CREATED
    filled_quantity: FixedPoint | None = None
    version: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "order_id", _required_text(self.order_id, "order_id"))
        if not isinstance(self.intent, OrderIntent):
            raise ValidationError("intent must be an OrderIntent")
        if not isinstance(self.status, OrderStatus):
            raise ValidationError("status must be an OrderStatus")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValidationError("version must be a non-negative integer")
        filled = self.filled_quantity or FixedPoint(0, self.intent.quantity.scale)
        if filled.scale != self.intent.quantity.scale:
            raise ValidationError("filled_quantity must use the order quantity scale")
        if filled.units < 0 or filled.units > self.intent.quantity.units:
            raise ValidationError("filled_quantity must be within the order quantity")
        if self.status is OrderStatus.FILLED and filled.units != self.intent.quantity.units:
            raise ValidationError("filled order must have the complete quantity")
        if self.status is OrderStatus.PARTIALLY_FILLED and not (
            0 < filled.units < self.intent.quantity.units
        ):
            raise ValidationError("partially filled order must have an intermediate quantity")
        object.__setattr__(self, "filled_quantity", filled)


@dataclass(frozen=True, kw_only=True)
class OrderEvent:
    event_id: str
    order_id: str
    event_time: datetime
    sequence: int
    from_status: OrderStatus
    to_status: OrderStatus
    reason: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _required_text(self.event_id, "event_id"))
        object.__setattr__(self, "order_id", _required_text(self.order_id, "order_id"))
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise ValidationError("sequence must be a positive integer")
        if not isinstance(self.from_status, OrderStatus) or not isinstance(
            self.to_status, OrderStatus
        ):
            raise ValidationError("order event statuses must be OrderStatus values")
        if not isinstance(self.reason, str):
            raise ValidationError("reason must be a string")


@dataclass(frozen=True, kw_only=True)
class Fill:
    fill_id: str
    order_id: str
    account_id: str
    strategy_id: str
    instrument_id: str
    side: Side
    quantity: FixedPoint
    price: FixedPoint
    event_time: datetime
    liquidity_role: LiquidityRole = LiquidityRole.UNKNOWN
    venue_trade_id: str | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "fill_id",
            "order_id",
            "account_id",
            "strategy_id",
            "instrument_id",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if not isinstance(self.side, Side):
            raise ValidationError("side must be a Side")
        if not isinstance(self.liquidity_role, LiquidityRole):
            raise ValidationError("liquidity_role must be a LiquidityRole")
        _positive(self.quantity, "quantity")
        _positive(self.price, "price")
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )
        if self.venue_trade_id is not None:
            object.__setattr__(
                self,
                "venue_trade_id",
                _required_text(self.venue_trade_id, "venue_trade_id"),
            )


@dataclass(frozen=True, kw_only=True)
class Fee:
    fee_id: str
    fill_id: str
    account_id: str
    amount: FixedPoint
    currency: str
    event_time: datetime
    fee_type: str

    def __post_init__(self) -> None:
        for field_name in ("fee_id", "fill_id", "account_id", "fee_type"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(self, "currency", _currency(self.currency))
        if not isinstance(self.amount, FixedPoint):
            raise ValidationError("amount must be a FixedPoint")
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )


@dataclass(frozen=True, kw_only=True)
class Funding:
    funding_id: str
    account_id: str
    instrument_id: str
    amount: FixedPoint
    currency: str
    event_time: datetime

    def __post_init__(self) -> None:
        for field_name in ("funding_id", "account_id", "instrument_id"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if not isinstance(self.amount, FixedPoint):
            raise ValidationError("amount must be a FixedPoint")
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )


@dataclass(frozen=True, kw_only=True)
class Settlement:
    settlement_id: str
    account_id: str
    instrument_id: str
    amount: FixedPoint
    currency: str
    event_time: datetime
    settlement_type: str

    def __post_init__(self) -> None:
        for field_name in (
            "settlement_id",
            "account_id",
            "instrument_id",
            "settlement_type",
        ):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        if not isinstance(self.amount, FixedPoint):
            raise ValidationError("amount must be a FixedPoint")
        object.__setattr__(self, "currency", _currency(self.currency))
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )


@dataclass(frozen=True, kw_only=True)
class Posting:
    ledger_account: str
    currency: str
    amount: FixedPoint
    instrument_id: str | None = None
    quantity_delta: FixedPoint | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "ledger_account", _required_text(self.ledger_account, "ledger_account")
        )
        object.__setattr__(self, "currency", _currency(self.currency))
        if not isinstance(self.amount, FixedPoint):
            raise ValidationError("amount must be a FixedPoint")
        if self.instrument_id is not None:
            object.__setattr__(
                self,
                "instrument_id",
                _required_text(self.instrument_id, "instrument_id"),
            )
        if self.quantity_delta is not None and not isinstance(
            self.quantity_delta, FixedPoint
        ):
            raise ValidationError("quantity_delta must be a FixedPoint or null")


@dataclass(frozen=True, kw_only=True)
class LedgerTransaction:
    transaction_id: str
    idempotency_key: str
    event_time: datetime
    event_type: LedgerEventType
    reference_id: str
    postings: tuple[Posting, ...]

    def __post_init__(self) -> None:
        for field_name in ("transaction_id", "idempotency_key", "reference_id"):
            object.__setattr__(
                self, field_name, _required_text(getattr(self, field_name), field_name)
            )
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )
        if not isinstance(self.event_type, LedgerEventType):
            raise ValidationError("event_type must be a LedgerEventType")
        if len(self.postings) < 2 or any(
            not isinstance(posting, Posting) for posting in self.postings
        ):
            raise ValidationError("ledger transaction requires at least two postings")
        balances: dict[str, Decimal] = {}
        for posting in self.postings:
            balances[posting.currency] = balances.get(
                posting.currency, Decimal(0)
            ) + posting.amount.to_decimal()
        unbalanced = {currency: total for currency, total in balances.items() if total != 0}
        if unbalanced:
            raise ValidationError(f"ledger transaction is unbalanced: {unbalanced}")


@dataclass(frozen=True, kw_only=True)
class AccountSnapshot:
    account_id: str
    event_time: datetime
    base_currency: str
    cash_balances: Mapping[str, FixedPoint] = field(default_factory=dict)
    positions: Mapping[str, FixedPoint] = field(default_factory=dict)
    nav: FixedPoint = FixedPoint(0, 2)

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _required_text(self.account_id, "account_id"))
        object.__setattr__(
            self, "base_currency", _currency(self.base_currency, "base_currency")
        )
        object.__setattr__(
            self, "event_time", ensure_utc_datetime(self.event_time, field="event_time")
        )
        if not isinstance(self.nav, FixedPoint):
            raise ValidationError("nav must be a FixedPoint")
        object.__setattr__(
            self,
            "cash_balances",
            _immutable_fixed_point_map(self.cash_balances, "cash_balances"),
        )
        for currency in self.cash_balances:
            _currency(currency, "cash_balances key")
        object.__setattr__(
            self, "positions", _immutable_fixed_point_map(self.positions, "positions")
        )


@dataclass(frozen=True, kw_only=True)
class RiskDecision:
    accepted: bool
    code: str
    message: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.accepted, bool):
            raise ValidationError("accepted must be boolean")
        object.__setattr__(self, "code", _required_text(self.code, "code"))
        if not isinstance(self.message, str):
            raise ValidationError("message must be a string")


@dataclass(frozen=True, kw_only=True)
class RunResult:
    run_id: str
    seed: int
    event_count: int
    order_count: int
    fill_count: int
    event_sha256: str
    fill_sha256: str
    ledger_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "run_id", _required_text(self.run_id, "run_id"))
        for field_name in ("seed", "event_count", "order_count", "fill_count"):
            value = getattr(self, field_name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValidationError(f"{field_name} must be a non-negative integer")
        for field_name in ("event_sha256", "fill_sha256", "ledger_sha256"):
            value = _required_text(getattr(self, field_name), field_name)
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValidationError(f"{field_name} must be a lowercase SHA-256")


LedgerEvent: TypeAlias = Fill | Fee | Funding | Settlement | CorporateActionEvent
