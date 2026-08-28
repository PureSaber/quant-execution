"""Deterministic in-memory broker and legal order lifecycle."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import date, datetime

from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import (
    Fill,
    Order,
    OrderEvent,
    OrderIntent,
    OrderStatus,
    TimeInForce,
)
from quant_execution.schemas import execution_payload
from quant_execution.state_machine import transition_order


def _digest(prefix: str, *parts: object) -> str:
    payload = json.dumps(parts, ensure_ascii=True, separators=(",", ":"), default=str)
    return f"{prefix}-{hashlib.sha256(payload.encode()).hexdigest()[:24]}"


class DeterministicBroker:
    """Research-only broker with idempotent submit/cancel and immutable facts."""

    sends_live_orders = False

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._orders: dict[str, Order] = {}
        self._open_order_ids: set[str] = set()
        self._submit_keys: dict[str, tuple[str, str]] = {}
        self._cancel_keys: dict[str, tuple[str, OrderEvent]] = {}
        self._fill_keys: dict[str, tuple[Fill, OrderEvent]] = {}
        self._events: list[OrderEvent] = []
        self._accepted_day: dict[str, date] = {}

    def capture_state(self) -> dict[str, object]:
        return deepcopy(
            {
                "orders": self._orders,
                "open_order_ids": self._open_order_ids,
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
        self._open_order_ids = restored["open_order_ids"]
        self._submit_keys = restored["submit_keys"]
        self._cancel_keys = restored["cancel_keys"]
        self._fill_keys = restored["fill_keys"]
        self._events = restored["events"]
        self._accepted_day = restored["accepted_day"]

    @property
    def orders(self) -> tuple[Order, ...]:
        return tuple(sorted(self._orders.values(), key=self._sort_key))

    @property
    def order_events(self) -> tuple[OrderEvent, ...]:
        return tuple(self._events)

    @property
    def open_orders(self) -> tuple[Order, ...]:
        return tuple(
            sorted(
                (self._orders[order_id] for order_id in self._open_order_ids),
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
        payload = json.dumps(
            execution_payload(intent), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(payload).hexdigest()

    def submit(self, order_intent: OrderIntent) -> Order:
        if not isinstance(order_intent, OrderIntent):
            raise ValidationError("order_intent must be an OrderIntent")
        semantic_hash = self._intent_hash(order_intent)
        prior = self._submit_keys.get(order_intent.idempotency_key)
        if prior is not None:
            order_id, prior_hash = prior
            if prior_hash != semantic_hash:
                raise ValidationError("submit idempotency key reused with different intent")
            return self._orders[order_id]
        order_id = _digest("ord", order_intent.idempotency_key, semantic_hash)
        order = Order(order_id=order_id, intent=order_intent)
        accepted, event = transition_order(
            order,
            OrderStatus.ACCEPTED,
            event_id=_digest("oev", order_id, 1, "accepted"),
            event_time=order_intent.created_at,
        )
        self._orders[order_id] = accepted
        self._open_order_ids.add(order_id)
        self._submit_keys[order_intent.idempotency_key] = (order_id, semantic_hash)
        self._events.append(event)
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
            return self._orders[order_id]
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
        self._submit_keys[order_intent.idempotency_key] = (order_id, semantic_hash)
        self._events.append(event)
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
        self._cancel_keys[idempotency_key] = (order_id, event)
        self._events.append(event)
        return event

    def apply_fill(self, fill: Fill) -> OrderEvent:
        prior = self._fill_keys.get(fill.fill_id)
        if prior is not None:
            prior_fill, prior_event = prior
            if prior_fill != fill:
                raise ValidationError("fill_id reused with different fill content")
            return prior_event
        order = self._require_order(fill.order_id)
        if order.status not in {OrderStatus.ACCEPTED, OrderStatus.PARTIALLY_FILLED}:
            raise ValidationError("fill references an order that is not open")
        if fill.instrument_id != order.intent.instrument_id:
            raise ValidationError("fill instrument differs from order intent")
        if fill.side is not order.intent.side:
            raise ValidationError("fill side differs from order intent")
        if fill.quantity.scale != order.intent.quantity.scale:
            raise ValidationError("fill quantity scale differs from order intent")
        remaining = order.intent.quantity.units - order.filled_quantity.units
        if fill.quantity.units > remaining:
            raise ValidationError("fill would violate order quantity conservation")
        target = (
            OrderStatus.FILLED if fill.quantity.units == remaining else OrderStatus.PARTIALLY_FILLED
        )
        updated, event = transition_order(
            order,
            target,
            event_id=_digest("oev", order.order_id, order.version + 1, target.value, fill.fill_id),
            event_time=fill.event_time,
            fill_quantity=fill.quantity,
        )
        self._orders[order.order_id] = updated
        if target is OrderStatus.FILLED:
            self._open_order_ids.remove(order.order_id)
        self._events.append(event)
        self._fill_keys[fill.fill_id] = (fill, event)
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
        self._events.append(event)
        return event

    def note_trading_day(self, order_id: str, trading_day: date) -> None:
        self._accepted_day.setdefault(order_id, trading_day)

    def expire_day_orders(self, trading_day: date, event_time: datetime) -> tuple[OrderEvent, ...]:
        expired: list[OrderEvent] = []
        for order in self.open_orders:
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
            return self._orders[order_id]
        except KeyError as exc:
            raise ValidationError(f"unknown order_id: {order_id}") from exc


def remaining_quantity(order: Order) -> FixedPoint:
    return FixedPoint(
        order.intent.quantity.units - order.filled_quantity.units,
        order.intent.quantity.scale,
    )
