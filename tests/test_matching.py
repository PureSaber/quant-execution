from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp, spec
from quant_data_kit import (
    AggressorSide,
    AssetClass,
    BarEvent,
    BookAction,
    BookDeltaEvent,
    BookLevel,
    BookSide,
    BookSnapshotEvent,
    QuoteEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution import OrderIntent, OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.matching import BarMatchingModel, L2MatchingModel, TradeBBOModel

ASSET = "crypto:test:BTCUSDT"


def order(
    broker: DeterministicBroker,
    key: str,
    *,
    side: Side = Side.BUY,
    quantity: str = "5",
    order_type: OrderType = OrderType.LIMIT,
    tif: TimeInForce = TimeInForce.GTC,
    price: str | None = "100",
    stop: str | None = None,
    created_seconds: int = 0,
):
    return broker.submit(
        OrderIntent(
            idempotency_key=key,
            account_id="account",
            strategy_id="strategy",
            instrument_id=ASSET,
            side=side,
            quantity=fp(quantity),
            order_type=order_type,
            time_in_force=tif,
            created_at=T0 + timedelta(seconds=created_seconds),
            limit_price=fp(price) if price is not None else None,
            stop_price=fp(stop) if stop is not None else None,
        )
    )


def instrument():
    return spec(
        ASSET,
        asset_class=AssetClass.CRYPTO,
        product_type="spot",
        settlement_currency="USDT",
        base_currency="BTC",
        quote_currency="USDT",
    )


def bar(event_id: str, seconds: int, *, low: str = "95", high: str = "105") -> BarEvent:
    return BarEvent(
        **event_fields(event_id, ASSET, seconds=seconds),
        bar_start=T0 + timedelta(seconds=seconds - 60),
        bar_end=T0 + timedelta(seconds=seconds),
        open_price=fp("101"),
        high_price=fp(high),
        low_price=fp(low),
        close_price=fp("100"),
        volume=fp("20"),
        is_complete=True,
    )


def test_bar_matching_participation_partial_fok_slippage_and_stop_limit_ambiguity() -> None:
    broker = DeterministicBroker()
    limit = order(broker, "limit", quantity="5")
    fok = order(broker, "fok", quantity="5", tif=TimeInForce.FOK)
    market = order(
        broker,
        "market",
        quantity="1",
        order_type=OrderType.MARKET,
        tif=TimeInForce.IOC,
        price=None,
    )
    model = BarMatchingModel(
        {ASSET: instrument()}, participation_rate=Decimal("0.2"), slippage_ticks=2
    )
    fills = model.match(bar("bar-1", 60), [limit, fok, market])
    assert [(item.order_id, item.quantity.to_decimal()) for item in fills] == [
        (limit.order_id, Decimal("4.00"))
    ]
    broker = DeterministicBroker()
    stop_limit = order(
        broker,
        "stop-limit",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="99",
        stop="103",
    )
    model.reset()
    assert model.match(bar("trigger", 60), [stop_limit]) == ()
    later = model.match(bar("later", 120, high="102"), [stop_limit])
    assert later[0].price == fp("99")


def test_trade_bbo_respects_latency_spread_volume_and_liquidity_role() -> None:
    broker = DeterministicBroker()
    taker = order(broker, "taker", quantity="3", price="102")
    model = TradeBBOModel({ASSET: instrument()}, latency=timedelta(seconds=2))
    quote = QuoteEvent(
        **event_fields("q1", ASSET, seconds=1),
        bid_price=fp("99"),
        bid_quantity=fp("8"),
        ask_price=fp("101"),
        ask_quantity=fp("2"),
    )
    assert model.match(quote, [taker]) == ()
    quote2 = QuoteEvent(
        **event_fields("q2", ASSET, seconds=2),
        bid_price=fp("99"),
        bid_quantity=fp("8"),
        ask_price=fp("101"),
        ask_quantity=fp("2"),
    )
    fill = model.match(quote2, [taker])[0]
    assert fill.price == quote2.ask_price
    assert fill.quantity == fp("2")
    broker = DeterministicBroker()
    maker = order(broker, "maker", quantity="2", price="99")
    trade = TradeEvent(
        **event_fields("t1", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    maker_fill = model.match(trade, [maker])[0]
    assert maker_fill.liquidity_role.value == "maker"
    assert maker_fill.quantity == fp("1")


def test_l2_consumes_levels_and_models_queue_ahead_deterministically() -> None:
    broker = DeterministicBroker()
    market = order(
        broker,
        "market",
        quantity="4",
        order_type=OrderType.MARKET,
        tif=TimeInForce.IOC,
        price=None,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("s1", ASSET, seconds=1, sequence=10),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("2")), BookLevel(fp("101"), fp("3"))),
    )
    fills = model.match(snapshot, [market])
    assert [(item.price.to_decimal(), item.quantity.to_decimal()) for item in fills] == [
        (Decimal("100.00"), Decimal("2.00")),
        (Decimal("101.00"), Decimal("2.00")),
    ]
    broker = DeterministicBroker()
    passive = order(broker, "passive", quantity="2", price="99")
    model.reset()
    assert model.match(snapshot, [passive]) == ()
    delta = BookDeltaEvent(
        **event_fields("d1", ASSET, seconds=2, sequence=11),
        side=BookSide.BID,
        action=BookAction.UPSERT,
        price=fp("99"),
        quantity=fp("3"),
        previous_sequence=10,
    )
    assert model.match(delta, [passive]) == ()
    trade = TradeEvent(
        **event_fields("trade", ASSET, seconds=3, sequence=12),
        price=fp("99"),
        quantity=fp("4"),
        aggressor_side=AggressorSide.SELL,
    )
    queue_fill = model.match(trade, [passive])[0]
    assert queue_fill.quantity == fp("1")


def test_l2_fails_closed_on_sequence_gap() -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("s1", ASSET, seconds=1, sequence=10),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("2")),),
    )
    model.match(snapshot, [])
    bad = BookDeltaEvent(
        **event_fields("bad", ASSET, seconds=2, sequence=12),
        side=BookSide.ASK,
        action=BookAction.DELETE,
        price=fp("100"),
        quantity=fp("0"),
        previous_sequence=11,
    )
    with pytest.raises(ValidationError, match="sequence gap"):
        model.match(bad, [])


def test_l2_same_price_trade_consumes_shared_queue_once_in_price_time_order() -> None:
    broker = DeterministicBroker()
    first = order(broker, "same-price-first", quantity="1", price="99")
    second = order(broker, "same-price-second", quantity="1", price="99")
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("same-price-book", ASSET, seconds=1, sequence=20),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, [first, second]) == ()
    trade = TradeEvent(
        **event_fields("same-price-trade", ASSET, seconds=2, sequence=21),
        price=fp("99"),
        quantity=fp("7"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(trade, [first, second])
    expected = sorted((first, second), key=lambda item: (item.intent.created_at, item.order_id))
    assert [(item.order_id, item.quantity) for item in fills] == [
        (expected[0].order_id, fp("1")),
        (expected[1].order_id, fp("1")),
    ]


@pytest.mark.parametrize("terminal_action", ["cancel", "expire"])
def test_l2_terminal_predecessor_releases_later_same_price_queue(
    terminal_action: str,
) -> None:
    broker = DeterministicBroker()
    first = order(
        broker,
        f"{terminal_action}-first",
        quantity="1",
        price="99",
        created_seconds=0,
    )
    second = order(
        broker,
        f"{terminal_action}-second",
        quantity="1",
        price="99",
        created_seconds=1,
    )
    model = L2MatchingModel({ASSET: instrument()})
    book = BookSnapshotEvent(
        **event_fields(f"{terminal_action}-book", ASSET, seconds=2, sequence=30),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(book, broker.open_orders) == ()
    if terminal_action == "cancel":
        broker.cancel(
            first.order_id,
            idempotency_key="cancel-first",
            created_at=T0 + timedelta(seconds=3),
        )
    else:
        broker.expire(
            first.order_id,
            event_time=T0 + timedelta(seconds=3),
            reason="fixture expiry",
        )
    trade = TradeEvent(
        **event_fields(f"{terminal_action}-trade", ASSET, seconds=4),
        price=fp("99"),
        quantity=fp("6"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in fills] == [(second.order_id, fp("1"))]


def test_l2_rejected_predecessor_never_reserves_queue() -> None:
    broker = DeterministicBroker()
    rejected_intent = OrderIntent(
        idempotency_key="rejected-first",
        account_id="account",
        strategy_id="strategy",
        instrument_id=ASSET,
        side=Side.BUY,
        quantity=fp("1"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp("99"),
    )
    broker.reject(rejected_intent, code="FIXTURE_REJECTION")
    second = order(
        broker,
        "accepted-second",
        quantity="1",
        price="99",
        created_seconds=1,
    )
    model = L2MatchingModel({ASSET: instrument()})
    book = BookSnapshotEvent(
        **event_fields("rejected-book", ASSET, seconds=2, sequence=40),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(book, broker.open_orders) == ()
    trade = TradeEvent(
        **event_fields("rejected-trade", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("6"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in fills] == [(second.order_id, fp("1"))]


def test_l2_partial_then_complete_fill_preserves_later_queue_position() -> None:
    broker = DeterministicBroker()
    first = order(
        broker,
        "partial-first",
        quantity="2",
        price="99",
        created_seconds=0,
    )
    second = order(
        broker,
        "partial-second",
        quantity="1",
        price="99",
        created_seconds=1,
    )
    model = L2MatchingModel({ASSET: instrument()})
    book = BookSnapshotEvent(
        **event_fields("partial-book", ASSET, seconds=2, sequence=50),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(book, broker.open_orders) == ()

    first_trade = TradeEvent(
        **event_fields("partial-trade-1", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("6"),
        aggressor_side=AggressorSide.SELL,
    )
    first_fills = model.match(first_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in first_fills] == [(first.order_id, fp("1"))]
    broker.apply_fill(first_fills[0])

    second_trade = TradeEvent(
        **event_fields("partial-trade-2", ASSET, seconds=4),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    second_fills = model.match(second_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in second_fills] == [(first.order_id, fp("1"))]
    broker.apply_fill(second_fills[0])

    third_trade = TradeEvent(
        **event_fields("partial-trade-3", ASSET, seconds=5),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    third_fills = model.match(third_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in third_fills] == [(second.order_id, fp("1"))]


def test_l2_books_and_queues_are_isolated_across_instruments() -> None:
    other = "crypto:test:ETHUSDT"
    other_spec = spec(
        other,
        asset_class=AssetClass.CRYPTO,
        product_type="spot",
        settlement_currency="USDT",
        base_currency="ETH",
        quote_currency="USDT",
    )
    registry = {ASSET: instrument(), other: other_spec}

    broker = DeterministicBroker()
    passive = order(broker, "snapshot-isolation", quantity="1", price="99")
    model = L2MatchingModel(registry)
    asset_book = BookSnapshotEvent(
        **event_fields("asset-book", ASSET, seconds=1, sequence=60),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    other_book = BookSnapshotEvent(
        **event_fields("other-book", other, seconds=2, sequence=70),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(asset_book, broker.open_orders) == ()
    assert model.match(other_book, broker.open_orders) == ()
    asset_trade = TradeEvent(
        **event_fields("asset-after-other-book", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("6"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(asset_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in fills] == [(passive.order_id, fp("1"))]

    broker = DeterministicBroker()
    passive = order(broker, "delta-isolation", quantity="1", price="99")
    model.reset()
    assert model.match(other_book, ()) == ()
    assert model.match(asset_book, broker.open_orders) == ()
    other_delta = BookDeltaEvent(
        **event_fields("other-delta", other, seconds=3, sequence=71),
        side=BookSide.BID,
        action=BookAction.UPSERT,
        price=fp("99"),
        quantity=fp("4"),
        previous_sequence=70,
    )
    assert model.match(other_delta, broker.open_orders) == ()
    insufficient_trade = TradeEvent(
        **event_fields("asset-after-other-delta", ASSET, seconds=4),
        price=fp("99"),
        quantity=fp("5"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(insufficient_trade, broker.open_orders) == ()
