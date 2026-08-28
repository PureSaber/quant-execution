"""Legal order lifecycle transitions."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from quant_data_kit import FixedPoint, ensure_utc_datetime
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import Order, OrderEvent, OrderStatus

ALLOWED_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset({OrderStatus.ACCEPTED, OrderStatus.REJECTED}),
    OrderStatus.ACCEPTED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
}


def transition_order(
    order: Order,
    to_status: OrderStatus,
    *,
    event_id: str,
    event_time: datetime,
    fill_quantity: FixedPoint | None = None,
    reason: str = "",
) -> tuple[Order, OrderEvent]:
    """Apply one legal transition and return the new immutable order plus event."""
    if not isinstance(order, Order):
        raise ValidationError("order must be an Order")
    if not isinstance(to_status, OrderStatus):
        raise ValidationError("to_status must be an OrderStatus")
    if to_status not in ALLOWED_TRANSITIONS[order.status]:
        raise ValidationError(f"Illegal order transition: {order.status.value}->{to_status.value}")
    event_time = ensure_utc_datetime(event_time, field="event_time")
    if event_time < order.intent.created_at:
        raise ValidationError("order event_time cannot precede intent created_at")
    is_fill = to_status in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
    if is_fill != (fill_quantity is not None):
        raise ValidationError("fill_quantity is required exactly for fill transitions")
    if to_status in {OrderStatus.REJECTED, OrderStatus.CANCELLED, OrderStatus.EXPIRED}:
        if not isinstance(reason, str) or not reason.strip():
            raise ValidationError(f"{to_status.value} transition requires a reason")
    elif not isinstance(reason, str):
        raise ValidationError("reason must be a string")

    filled = order.filled_quantity
    if fill_quantity is not None:
        if not fill_quantity.is_positive():
            raise ValidationError("fill_quantity must be positive")
        if fill_quantity.scale != order.intent.quantity.scale:
            raise ValidationError("fill_quantity must use the order quantity scale")
        filled = FixedPoint(filled.units + fill_quantity.units, filled.scale)
        if filled.units > order.intent.quantity.units:
            raise ValidationError("fill transition would overfill the order")
        if to_status is OrderStatus.PARTIALLY_FILLED and filled.units >= order.intent.quantity.units:
            raise ValidationError("partial fill must leave an open quantity")
        if to_status is OrderStatus.FILLED and filled.units != order.intent.quantity.units:
            raise ValidationError("filled transition must complete the order quantity")

    next_version = order.version + 1
    updated = replace(
        order,
        status=to_status,
        filled_quantity=filled,
        version=next_version,
    )
    event = OrderEvent(
        event_id=event_id,
        order_id=order.order_id,
        event_time=event_time,
        sequence=next_version,
        from_status=order.status,
        to_status=to_status,
        reason=reason,
    )
    return updated, event
