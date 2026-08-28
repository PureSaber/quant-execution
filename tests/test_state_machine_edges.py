from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import T0, fp
from quant_data_kit.exceptions import ValidationError

from quant_execution import (
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
    transition_order,
)


def created() -> Order:
    return Order(
        order_id="order",
        intent=OrderIntent(
            idempotency_key="key",
            account_id="account",
            strategy_id="strategy",
            instrument_id="asset",
            side=Side.BUY,
            quantity=fp("10"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            created_at=T0,
        ),
    )


def test_transition_rejects_invalid_types_missing_fills_and_reasons() -> None:
    order = created()
    with pytest.raises(ValidationError, match="order must"):
        transition_order("bad", OrderStatus.ACCEPTED, event_id="e", event_time=T0)
    with pytest.raises(ValidationError, match="to_status"):
        transition_order(order, "accepted", event_id="e", event_time=T0)
    with pytest.raises(ValidationError, match="fill_quantity"):
        transition_order(
            order, OrderStatus.ACCEPTED, event_id="e", event_time=T0, fill_quantity=fp("1")
        )
    with pytest.raises(ValidationError, match="requires a reason"):
        transition_order(order, OrderStatus.REJECTED, event_id="e", event_time=T0)
    with pytest.raises(ValidationError, match="reason must"):
        transition_order(order, OrderStatus.ACCEPTED, event_id="e", event_time=T0, reason=object())


def test_transition_rejects_nonpositive_scale_and_incomplete_fill_quantities() -> None:
    order, _ = transition_order(created(), OrderStatus.ACCEPTED, event_id="accepted", event_time=T0)
    with pytest.raises(ValidationError, match="positive"):
        transition_order(
            order,
            OrderStatus.PARTIALLY_FILLED,
            event_id="zero",
            event_time=T0,
            fill_quantity=fp("0"),
        )
    with pytest.raises(ValidationError, match="scale"):
        transition_order(
            order,
            OrderStatus.PARTIALLY_FILLED,
            event_id="scale",
            event_time=T0,
            fill_quantity=fp("1.000", 3),
        )
    with pytest.raises(ValidationError, match="leave an open"):
        transition_order(
            order,
            OrderStatus.PARTIALLY_FILLED,
            event_id="not-partial",
            event_time=T0,
            fill_quantity=fp("10"),
        )
    with pytest.raises(ValidationError, match="complete"):
        transition_order(
            order,
            OrderStatus.FILLED,
            event_id="not-full",
            event_time=T0 + timedelta(seconds=1),
            fill_quantity=fp("9"),
        )
