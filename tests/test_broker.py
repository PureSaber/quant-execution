from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import T0, fp
from quant_data_kit.exceptions import ValidationError

from quant_execution import OrderIntent, OrderStatus, OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.contracts import Fill, LiquidityRole


def intent(key: str = "signal-1") -> OrderIntent:
    return OrderIntent(
        idempotency_key=key,
        account_id="account",
        strategy_id="strategy",
        instrument_id="asset",
        side=Side.BUY,
        quantity=fp("10"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("100"),
    )


def fill(order_id: str, fill_id: str, quantity: str, seconds: int) -> Fill:
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        account_id="account",
        strategy_id="strategy",
        instrument_id="asset",
        side=Side.BUY,
        quantity=fp(quantity),
        price=fp("100"),
        event_time=T0 + timedelta(seconds=seconds),
        liquidity_role=LiquidityRole.MAKER,
    )


def test_submit_fill_cancel_and_idempotency_cover_complete_lifecycle() -> None:
    broker = DeterministicBroker()
    order = broker.submit(intent())
    assert order.status is OrderStatus.ACCEPTED
    assert broker.submit(intent()) == order
    broker.apply_fill(fill(order.order_id, "fill-1", "4", 1))
    assert broker.open_orders[0].status is OrderStatus.PARTIALLY_FILLED
    event = broker.cancel(
        order.order_id, idempotency_key="cancel-1", created_at=T0 + timedelta(seconds=2)
    )
    assert event.to_status is OrderStatus.CANCELLED
    assert (
        broker.cancel(
            order.order_id, idempotency_key="cancel-1", created_at=T0 + timedelta(seconds=3)
        )
        == event
    )
    assert [item.to_status for item in broker.order_events] == [
        OrderStatus.ACCEPTED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.CANCELLED,
    ]


def test_broker_rejects_idempotency_conflicts_overfill_and_terminal_mutation() -> None:
    broker = DeterministicBroker()
    order = broker.submit(intent())
    changed = intent()
    object.__setattr__(changed, "quantity", fp("9"))
    with pytest.raises(ValidationError, match="different intent"):
        broker.submit(changed)
    with pytest.raises(ValidationError, match="conservation"):
        broker.apply_fill(fill(order.order_id, "too-much", "11", 1))
    broker.apply_fill(fill(order.order_id, "complete", "10", 2))
    with pytest.raises(ValidationError, match="terminal"):
        broker.cancel(
            order.order_id,
            idempotency_key="late-cancel",
            created_at=T0 + timedelta(seconds=3),
        )


def test_rejection_and_day_expiry_have_explicit_reasons() -> None:
    broker = DeterministicBroker()
    rejected = broker.reject(intent("rejected"), code="NO_CASH", message="balance=0")
    assert rejected.status is OrderStatus.REJECTED
    day = intent("day")
    object.__setattr__(day, "time_in_force", TimeInForce.DAY)
    order = broker.submit(day)
    broker.note_trading_day(order.order_id, T0.date())
    events = broker.expire_day_orders((T0 + timedelta(days=1)).date(), T0 + timedelta(days=1))
    assert events[0].reason == "DAY session expired"
