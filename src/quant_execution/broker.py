"""Deterministic in-memory broker and legal order lifecycle."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime

from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError

from quant_execution._json import fixed_token, flat_sequence_bytes, string_token
from quant_execution.artifacts import fill_bytes, order_bytes, order_event_bytes
from quant_execution.contracts import (
    Fill,
    Order,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from quant_execution.state_machine import transition_order


def _digest(prefix: str, *parts: object) -> str:
    return f"{prefix}-{hashlib.sha256(flat_sequence_bytes(parts)).hexdigest()[:24]}"


def _intent_bytes(intent: OrderIntent) -> bytes:
    """Serialize a validated intent exactly like sorted canonical execution_payload JSON."""

    created_at = intent.created_at.isoformat().replace("+00:00", "Z")
    return (
        "{"
        f'"account_id":{string_token(intent.account_id)},'
        f'"created_at":{string_token(created_at)},'
        f'"idempotency_key":{string_token(intent.idempotency_key)},'
        f'"instrument_id":{string_token(intent.instrument_id)},'
        f'"limit_price":{fixed_token(intent.limit_price)},'
        f'"order_type":{string_token(intent.order_type.value)},'
        f'"quantity":{fixed_token(intent.quantity)},'
        f'"reduce_only":{"true" if intent.reduce_only else "false"},'
        f'"side":{string_token(intent.side.value)},'
        f'"stop_price":{fixed_token(intent.stop_price)},'
        f'"strategy_id":{string_token(intent.strategy_id)},'
        f'"time_in_force":{string_token(intent.time_in_force.value)}'
        "}"
    ).encode()


def _fixed_from_payload(payload: dict[str, int] | None) -> FixedPoint | None:
    return None if payload is None else FixedPoint(payload["units"], payload["scale"])


def _time_from_payload(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _order_from_bytes(payload: bytes) -> Order:
    value = json.loads(payload)
    raw_intent = value["intent"]
    intent = OrderIntent(
        idempotency_key=raw_intent["idempotency_key"],
        account_id=raw_intent["account_id"],
        strategy_id=raw_intent["strategy_id"],
        instrument_id=raw_intent["instrument_id"],
        side=Side(raw_intent["side"]),
        quantity=_fixed_from_payload(raw_intent["quantity"]),
        order_type=OrderType(raw_intent["order_type"]),
        time_in_force=TimeInForce(raw_intent["time_in_force"]),
        created_at=_time_from_payload(raw_intent["created_at"]),
        limit_price=_fixed_from_payload(raw_intent["limit_price"]),
        stop_price=_fixed_from_payload(raw_intent["stop_price"]),
        reduce_only=raw_intent["reduce_only"],
    )
    return Order(
        order_id=value["order_id"],
        intent=intent,
        status=OrderStatus(value["status"]),
        filled_quantity=_fixed_from_payload(value["filled_quantity"]),
        version=value["version"],
    )


def _event_from_bytes(payload: bytes) -> OrderEvent:
    value = json.loads(payload)
    return OrderEvent(
        event_id=value["event_id"],
        order_id=value["order_id"],
        event_time=_time_from_payload(value["event_time"]),
        sequence=value["sequence"],
        from_status=OrderStatus(value["from_status"]),
        to_status=OrderStatus(value["to_status"]),
        fill_quantity=_fixed_from_payload(value["fill_quantity"]),
        reason=value["reason"],
    )


class DeterministicBroker:
    """Research-only broker with idempotent submit/cancel and immutable facts."""

    sends_live_orders = False

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._orders: dict[str, Order | bytes] = {}
        self._order_count = 0
        self._open_order_ids: set[str] = set()
        self._day_order_ids: set[str] = set()
        self._immediate_order_ids: set[str] = set()
        self._submit_keys: dict[str, tuple[str, str]] = {}
        self._cancel_keys: dict[str, tuple[str, OrderEvent]] = {}
        self._fill_keys: dict[str, tuple[bytes, bytes] | tuple[Fill, OrderEvent]] = {}
        self._events: list[OrderEvent] = []
        self._accepted_day: dict[str, date] = {}
        self._artifact_sink = None

    def start_artifact_stream(self, sink: object) -> None:
        """Route immutable history to a bounded sink while retaining live broker state."""

        if self._orders or self._events or self._artifact_sink is not None:
            raise ValidationError("broker artifact streaming must start immediately after reset")
        if not callable(getattr(sink, "append", None)):
            raise ValidationError("artifact sink must provide append(stream, payload)")
        self._artifact_sink = sink

    def finish_artifact_stream(self) -> None:
        """Persist the final state of orders that remained open at replay completion."""

        sink = self._artifact_sink
        if sink is None:
            return
        for order in self.open_orders:
            sink.append("orders", order_bytes(order))
        self._artifact_sink = None

    def abort_artifact_stream(self) -> None:
        self._artifact_sink = None

    def _record_event(self, event: OrderEvent, order: Order) -> None:
        sink = self._artifact_sink
        if sink is None:
            self._events.append(event)
            return
        sink.append("order_events", order_event_bytes(event))
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            payload = order_bytes(order)
            sink.append("orders", payload)
            self._orders[order.order_id] = payload

    def capture_state(self) -> dict[str, object]:
        return deepcopy(
            {
                "orders": self._orders,
                "order_count": self._order_count,
                "open_order_ids": self._open_order_ids,
                "day_order_ids": self._day_order_ids,
                "immediate_order_ids": self._immediate_order_ids,
                "submit_keys": self._submit_keys,
                "cancel_keys": self._cancel_keys,
                "fill_keys": self._fill_keys,
                "events": self._events,
                "accepted_day": self._accepted_day,
            }
        )

    def restore_state(self, state: dict[str, object]) -> None:
        restored = deepcopy(state)
        self._orders = restored["orders"]
        self._order_count = restored["order_count"]
        self._open_order_ids = restored["open_order_ids"]
        self._day_order_ids = restored["day_order_ids"]
        self._immediate_order_ids = restored["immediate_order_ids"]
        self._submit_keys = restored["submit_keys"]
        self._cancel_keys = restored["cancel_keys"]
        self._fill_keys = restored["fill_keys"]
        self._events = restored["events"]
        self._accepted_day = restored["accepted_day"]

    @property
    def orders(self) -> tuple[Order, ...]:
        orders = (
            _order_from_bytes(value) if isinstance(value, bytes) else value
            for value in self._orders.values()
        )
        return tuple(sorted(orders, key=self._sort_key))

    @property
    def order_count(self) -> int:
        return self._order_count

    @property
    def order_events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events)

    @property
    def open_orders(self) -> tuple[Order, ...]:
        if not self._open_order_ids:
            return ()
        if len(self._open_order_ids) == 1:
            order_id = next(iter(self._open_order_ids))
            value = self._orders[order_id]
            if isinstance(value, bytes):
                raise RuntimeError("terminal order appeared in the open-order index")
            return (value,)
        return tuple(
            sorted(
                (self._live_order(order_id) for order_id in self._open_order_ids),
                key=self._sort_key,
            )
        )

    @property
    def immediate_orders(self) -> tuple[Order, ...]:
        if not self._immediate_order_ids:
            return ()
        return tuple(
            sorted(
                (self._live_order(order_id) for order_id in self._immediate_order_ids),
                key=self._sort_key,
            )
        )

    def get_order(self, order_id: str) -> Order:
        """Return one order without sorting the complete historical order set."""
        return self._require_order(order_id)

    @staticmethod
    def _sort_key(order: Order) -> tuple[datetime, str]:
        return order.intent.created_at, order.order_id

    @staticmethod
    def _intent_hash(intent: OrderIntent) -> str:
        return hashlib.sha256(_intent_bytes(intent)).hexdigest()

    def submit(self, order_intent: OrderIntent) -> Order:
        if not isinstance(order_intent, OrderIntent):
            raise ValidationError("order_intent must be an OrderIntent")
        semantic_hash = self._intent_hash(order_intent)
        prior = self._submit_keys.get(order_intent.idempotency_key)
        if prior is not None:
            order_id, prior_hash = prior
            if prior_hash != semantic_hash:
                raise ValidationError("submit idempotency key reused with different intent")
            return self._require_order(order_id)
        order_id = _digest("ord", order_intent.idempotency_key, semantic_hash)
        filled = FixedPoint(0, order_intent.quantity.scale)
        accepted = self._order_fact(
            order_id,
            order_intent,
            status=OrderStatus.ACCEPTED,
            filled_quantity=filled,
            version=1,
        )
        event = self._event_fact(
            event_id=_digest("oev", order_id, 1, "accepted"),
            order=accepted,
            from_status=OrderStatus.CREATED,
            fill_quantity=None,
        )
        self._orders[order_id] = accepted
        self._order_count += 1
        self._open_order_ids.add(order_id)
        if order_intent.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
            self._immediate_order_ids.add(order_id)
        self._submit_keys[order_intent.idempotency_key] = (order_id, semantic_hash)
        self._record_event(event, accepted)
        return accepted

    def reject(self, order_intent: OrderIntent, *, code: str, message: str = "") -> Order:
        if not code.strip():
            raise ValidationError("rejection code is required")
        semantic_hash = self._intent_hash(order_intent)
        prior = self._submit_keys.get(order_intent.idempotency_key)
        if prior is not None:
            order_id, prior_hash = prior
            if prior_hash != semantic_hash:
                raise ValidationError("submit idempotency key reused with different intent")
            return self._require_order(order_id)
        order_id = _digest("ord", order_intent.idempotency_key, semantic_hash)
        order = Order(order_id=order_id, intent=order_intent)
        reason = code if not message else f"{code}: {message}"
        rejected, event = transition_order(
            order,
            OrderStatus.REJECTED,
            event_id=_digest("oev", order_id, 1, "rejected", reason),
            event_time=order_intent.created_at,
            reason=reason,
        )
        self._orders[order_id] = rejected
        self._order_count += 1
        self._submit_keys[order_intent.idempotency_key] = (order_id, semantic_hash)
        self._record_event(event, rejected)
        return rejected

    def cancel(
        self,
        order_id: str,
        *,
        idempotency_key: str,
        created_at: datetime,
    ) -> OrderEvent:
        if not idempotency_key.strip():
            raise ValidationError("cancel idempotency_key is required")
        prior = self._cancel_keys.get(idempotency_key)
        if prior is not None:
            prior_order_id, event = prior
            if prior_order_id != order_id:
                raise ValidationError("cancel idempotency key reused for another order")
            return event
        order = self._require_order(order_id)
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            raise ValidationError(f"cannot cancel terminal order in state {order.status.value}")
        updated, event = transition_order(
            order,
            OrderStatus.CANCELLED,
            event_id=_digest("oev", order_id, order.version + 1, "cancelled"),
            event_time=created_at,
            reason="cancel requested",
        )
        self._orders[order_id] = updated
        self._open_order_ids.remove(order_id)
        self._day_order_ids.discard(order_id)
        self._immediate_order_ids.discard(order_id)
        self._cancel_keys[idempotency_key] = (order_id, event)
        self._record_event(event, updated)
        return event

    def apply_fill(self, fill: Fill, *, trusted_unique: bool = False) -> OrderEvent:
        prior = None if trusted_unique else self._fill_keys.get(fill.fill_id)
        if prior is not None:
            if isinstance(prior[0], bytes):
                if prior[0] != hashlib.sha256(fill_bytes(fill)).digest():
                    raise ValidationError("fill_id reused with different fill content")
                return _event_from_bytes(prior[1])
            prior_fill, prior_event = prior
            if prior_fill != fill:
                raise ValidationError("fill_id reused with different fill content")
            return prior_event
        order = self._require_order(fill.order_id)
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            raise ValidationError("fill references an order that is not open")
        if fill.account_id != order.intent.account_id:
            raise ValidationError("fill account differs from order intent")
        if fill.strategy_id != order.intent.strategy_id:
            raise ValidationError("fill strategy differs from order intent")
        if fill.instrument_id != order.intent.instrument_id:
            raise ValidationError("fill instrument differs from order intent")
        if fill.side is not order.intent.side:
            raise ValidationError("fill side differs from order intent")
        if fill.quantity.scale != order.intent.quantity.scale:
            raise ValidationError("fill quantity scale differs from order intent")
        if not fill.quantity.is_positive():
            raise ValidationError("fill quantity must be positive")
        remaining = order.intent.quantity.units - order.filled_quantity.units
        if fill.quantity.units > remaining:
            raise ValidationError("fill would violate order quantity conservation")
        target = (
            OrderStatus.FILLED if fill.quantity.units == remaining else OrderStatus.PARTIALLY_FILLED
        )
        if fill.event_time < order.intent.created_at:
            raise ValidationError("order event_time cannot precede intent created_at")
        next_version = order.version + 1
        filled_quantity = FixedPoint(
            order.filled_quantity.units + fill.quantity.units,
            order.filled_quantity.scale,
        )
        updated = self._order_fact(
            order.order_id,
            order.intent,
            status=target,
            filled_quantity=filled_quantity,
            version=next_version,
        )
        event = self._event_fact(
            event_id=_digest("oev", order.order_id, next_version, target.value, fill.fill_id),
            order=updated,
            from_status=order.status,
            fill_quantity=fill.quantity,
            event_time=fill.event_time,
        )
        self._orders[order.order_id] = updated
        if target is OrderStatus.FILLED:
            self._open_order_ids.remove(order.order_id)
            self._day_order_ids.discard(order.order_id)
            self._immediate_order_ids.discard(order.order_id)
        self._record_event(event, updated)
        if not trusted_unique:
            if self._artifact_sink is None:
                self._fill_keys[fill.fill_id] = (fill, event)
            else:
                self._fill_keys[fill.fill_id] = (
                    hashlib.sha256(fill_bytes(fill)).digest(),
                    order_event_bytes(event),
                )
        return event

    def expire(self, order_id: str, *, event_time: datetime, reason: str) -> OrderEvent:
        order = self._require_order(order_id)
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            raise ValidationError("only open orders can expire")
        updated, event = transition_order(
            order,
            OrderStatus.EXPIRED,
            event_id=_digest("oev", order.order_id, order.version + 1, "expired", reason),
            event_time=event_time,
            reason=reason,
        )
        self._orders[order_id] = updated
        self._open_order_ids.remove(order_id)
        self._day_order_ids.discard(order_id)
        self._immediate_order_ids.discard(order_id)
        self._record_event(event, updated)
        return event

    def note_trading_day(self, order_id: str, trading_day: date) -> None:
        order = self._orders.get(order_id)
        if order is not None and order.intent.time_in_force is TimeInForce.DAY:
            self._accepted_day.setdefault(order_id, trading_day)
            self._day_order_ids.add(order_id)

    def expire_day_orders(self, trading_day: date, event_time: datetime) -> tuple[OrderEvent, ...]:
        if not self._day_order_ids:
            return ()
        expired: list[OrderEvent] = []
        orders = sorted(
            (self._orders[order_id] for order_id in self._day_order_ids),
            key=self._sort_key,
        )
        for order in orders:
            accepted_day = self._accepted_day.get(order.order_id)
            if (
                order.intent.time_in_force is TimeInForce.DAY
                and accepted_day is not None
                and trading_day > accepted_day
            ):
                expired.append(
                    self.expire(
                        order.order_id,
                        event_time=event_time,
                        reason="DAY session expired",
                    )
                )
        return tuple(expired)

    def _require_order(self, order_id: str) -> Order:
        try:
            value = self._orders[order_id]
        except KeyError as exc:
            raise ValidationError(f"unknown order_id: {order_id}") from exc
        return _order_from_bytes(value) if isinstance(value, bytes) else value

    def _live_order(self, order_id: str) -> Order:
        value = self._orders[order_id]
        if isinstance(value, bytes):
            raise TypeError("terminal order appeared in a live-order index")
        return value

    @staticmethod
    def _order_fact(
        order_id: str,
        intent: OrderIntent,
        *,
        status: OrderStatus,
        filled_quantity: FixedPoint,
        version: int,
    ) -> Order:
        order = object.__new__(Order)
        object.__setattr__(order, "order_id", order_id)
        object.__setattr__(order, "intent", intent)
        object.__setattr__(order, "status", status)
        object.__setattr__(order, "filled_quantity", filled_quantity)
        object.__setattr__(order, "version", version)
        return order

    @staticmethod
    def _event_fact(
        *,
        event_id: str,
        order: Order,
        from_status: OrderStatus,
        fill_quantity: FixedPoint | None,
        event_time: datetime | None = None,
    ) -> OrderEvent:
        event = object.__new__(OrderEvent)
        object.__setattr__(event, "event_id", event_id)
        object.__setattr__(event, "order_id", order.order_id)
        object.__setattr__(event, "event_time", event_time or order.intent.created_at)
        object.__setattr__(event, "sequence", order.version)
        object.__setattr__(event, "from_status", from_status)
        object.__setattr__(event, "to_status", order.status)
        object.__setattr__(event, "fill_quantity", fill_quantity)
        object.__setattr__(event, "reason", "")
        return event


def remaining_quantity(order: Order) -> FixedPoint:
    return FixedPoint(
        order.intent.quantity.units - order.filled_quantity.units,
        order.intent.quantity.scale,
    )
