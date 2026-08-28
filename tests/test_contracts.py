from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from quant_data_kit import FixedPoint
from quant_data_kit.exceptions import ValidationError

from quant_execution import (
    AccountSnapshot,
    Fee,
    Fill,
    Funding,
    LedgerEventType,
    LedgerTransaction,
    LiquidityRole,
    Order,
    OrderIntent,
    OrderStatus,
    OrderType,
    Posting,
    Settlement,
    Side,
    TimeInForce,
    transition_order,
)

UTC = timezone.utc
T0 = datetime(2026, 1, 2, 1, 0, tzinfo=UTC)


def fp(value: str, scale: int = 2) -> FixedPoint:
    return FixedPoint.from_decimal(Decimal(value), scale)


def limit_intent() -> OrderIntent:
    return OrderIntent(
        idempotency_key="strategy-1:signal-1",
        account_id="account-1",
        strategy_id="strategy-1",
        instrument_id="crypto:binance:BTCUSDT",
        side=Side.BUY,
        quantity=fp("10"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("100"),
    )


def test_order_intent_requires_explicit_prices_and_utc() -> None:
    intent = limit_intent()
    assert intent.limit_price == fp("100")
    with pytest.raises(ValidationError, match="limit_price requirement"):
        OrderIntent(
            idempotency_key="bad",
            account_id="account-1",
            strategy_id="strategy-1",
            instrument_id="asset-1",
            side=Side.BUY,
            quantity=fp("1"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            created_at=T0,
            limit_price=fp("1"),
        )
    with pytest.raises(ValidationError, match="timezone-aware UTC"):
        OrderIntent(
            idempotency_key="bad-time",
            account_id="account-1",
            strategy_id="strategy-1",
            instrument_id="asset-1",
            side=Side.SELL,
            quantity=fp("1"),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.IOC,
            created_at=T0.replace(tzinfo=None),
        )


def test_order_state_machine_conserves_quantity_and_rejects_illegal_transitions() -> None:
    order = Order(order_id="order-1", intent=limit_intent())
    order, accepted = transition_order(
        order,
        OrderStatus.ACCEPTED,
        event_id="event-1",
        event_time=T0 + timedelta(milliseconds=1),
    )
    assert accepted.sequence == 1
    order, partial = transition_order(
        order,
        OrderStatus.PARTIALLY_FILLED,
        event_id="event-2",
        event_time=T0 + timedelta(milliseconds=2),
        fill_quantity=fp("4"),
    )
    assert partial.from_status is OrderStatus.ACCEPTED
    assert partial.fill_quantity == fp("4")
    assert order.filled_quantity == fp("4")
    order, _ = transition_order(
        order,
        OrderStatus.PARTIALLY_FILLED,
        event_id="event-3",
        event_time=T0 + timedelta(milliseconds=3),
        fill_quantity=fp("3"),
    )
    order, filled = transition_order(
        order,
        OrderStatus.FILLED,
        event_id="event-4",
        event_time=T0 + timedelta(milliseconds=4),
        fill_quantity=fp("3"),
    )
    assert filled.sequence == 4
    assert filled.fill_quantity == fp("3")
    assert order.filled_quantity == order.intent.quantity
    with pytest.raises(ValidationError, match="Illegal order transition"):
        transition_order(
            order,
            OrderStatus.CANCELLED,
            event_id="event-5",
            event_time=T0 + timedelta(milliseconds=5),
            reason="too late",
        )


def test_order_state_machine_rejects_overfill_and_causal_time_violation() -> None:
    order = Order(order_id="order-1", intent=limit_intent())
    order, _ = transition_order(
        order,
        OrderStatus.ACCEPTED,
        event_id="event-1",
        event_time=T0,
    )
    with pytest.raises(ValidationError, match="overfill"):
        transition_order(
            order,
            OrderStatus.FILLED,
            event_id="event-2",
            event_time=T0,
            fill_quantity=fp("11"),
        )
    with pytest.raises(ValidationError, match="cannot precede"):
        transition_order(
            order,
            OrderStatus.CANCELLED,
            event_id="event-3",
            event_time=T0 - timedelta(microseconds=1),
            reason="invalid clock",
        )


def test_double_entry_ledger_balances_exactly_per_currency() -> None:
    transaction = LedgerTransaction(
        transaction_id="tx-1",
        idempotency_key="fill-1:cash",
        event_time=T0,
        event_type=LedgerEventType.FILL,
        reference_id="fill-1",
        postings=(
            Posting(ledger_account="assets:cash", currency="USDT", amount=fp("-100")),
            Posting(ledger_account="assets:position", currency="USDT", amount=fp("100")),
        ),
    )
    assert sum(posting.amount.to_decimal() for posting in transaction.postings) == 0
    with pytest.raises(ValidationError, match="unbalanced"):
        LedgerTransaction(
            transaction_id="tx-2",
            idempotency_key="bad",
            event_time=T0,
            event_type=LedgerEventType.FEE,
            reference_id="fee-1",
            postings=(
                Posting(ledger_account="expenses:fee", currency="USDT", amount=fp("1")),
                Posting(ledger_account="assets:cash", currency="USDT", amount=fp("-0.99")),
            ),
        )


def test_fill_fee_funding_settlement_and_snapshot_are_typed() -> None:
    fill = Fill(
        fill_id="fill-1",
        order_id="order-1",
        account_id="account-1",
        strategy_id="strategy-1",
        instrument_id="crypto:binance:BTCUSDT",
        side=Side.BUY,
        quantity=fp("1"),
        price=fp("100"),
        event_time=T0,
        liquidity_role=LiquidityRole.MAKER,
    )
    fee = Fee(
        fee_id="fee-1",
        fill_id=fill.fill_id,
        account_id="account-1",
        amount=fp("-0.01"),
        currency="USDT",
        event_time=T0,
        fee_type="maker_rebate",
    )
    funding = Funding(
        funding_id="funding-1",
        account_id="account-1",
        instrument_id="crypto:binance:BTCUSDT-PERP",
        amount=fp("-1"),
        currency="USDT",
        event_time=T0,
    )
    settlement = Settlement(
        settlement_id="settlement-1",
        account_id="account-1",
        instrument_id="future:cffex:IF2603",
        amount=fp("20"),
        currency="CNY",
        event_time=T0,
        settlement_type="daily_mark",
    )
    snapshot = AccountSnapshot(
        account_id="account-1",
        event_time=T0,
        base_currency="USD",
        cash_balances={"USDT": fp("1000")},
        positions={fill.instrument_id: fp("1")},
        nav=fp("1000"),
    )
    assert fee.amount.units < 0
    assert funding.amount.units < 0
    assert settlement.amount.units > 0
    with pytest.raises(TypeError):
        snapshot.cash_balances["USD"] = fp("1")
