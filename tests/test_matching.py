from __future__ import annotations

import pickle
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

import quant_execution.matching as matching_module
from quant_execution import OrderIntent, OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.matching import BarMatchingModel, L2MatchingModel, TradeBBOModel

ASSET = "crypto:test:BTCUSDT"


def order(
    broker: DeterministicBroker,
    key: str,
    *,
    instrument_id: str = ASSET,
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
            instrument_id=instrument_id,
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


def test_l2_partial_taker_remainder_enters_shared_maker_queue_without_mutating_book() -> None:
    broker = DeterministicBroker()
    first = order(
        broker,
        "partial-taker-first",
        quantity="2",
        price="99",
        created_seconds=0,
    )
    second = order(
        broker,
        "partial-taker-second",
        quantity="1",
        price="99",
        created_seconds=1,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("partial-taker-book", ASSET, seconds=2, sequence=80),
        bids=(BookLevel(fp("98"), fp("5")),),
        asks=(BookLevel(fp("99"), fp("1")),),
    )

    taker_fills = model.match(snapshot, broker.open_orders)
    assert [(fill.order_id, fill.quantity, fill.liquidity_role.value) for fill in taker_fills] == [
        (first.order_id, fp("1"), "taker")
    ]
    assert model._books[ASSET]["asks"] == {fp("99").units: fp("1")}
    broker.apply_fill(taker_fills[0])

    first_trade = TradeEvent(
        **event_fields("partial-taker-trade-1", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    first_maker = model.match(first_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in first_maker] == [(first.order_id, fp("1"))]
    broker.apply_fill(first_maker[0])

    second_trade = TradeEvent(
        **event_fields("partial-taker-trade-2", ASSET, seconds=4),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    second_maker = model.match(second_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in second_maker] == [(second.order_id, fp("1"))]


def test_l2_untriggered_stop_limit_ignores_deep_book_and_never_reserves_queue() -> None:
    broker = DeterministicBroker()
    waiting = order(
        broker,
        "deep-book-stop-limit",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="99",
        stop="105",
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("deep-book", ASSET, seconds=1, sequence=90),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("1")), BookLevel(fp("106"), fp("1"))),
    )
    assert model.match(snapshot, broker.open_orders) == ()
    assert waiting.order_id not in model._queue_keys
    trade = TradeEvent(
        **event_fields("untriggered-queue-trade", ASSET, seconds=2),
        price=fp("99"),
        quantity=fp("6"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(trade, broker.open_orders) == ()
    assert waiting.order_id not in model._queue_keys


@pytest.mark.parametrize(
    ("side", "stop", "limit", "trigger_price", "book_side", "book_price"),
    [
        (Side.BUY, "105", "106", "106", BookSide.ASK, "100"),
        (Side.SELL, "95", "94", "94", BookSide.BID, "100"),
    ],
)
def test_l2_trade_triggered_stop_limit_starts_on_next_event_and_fills_partially(
    side: Side,
    stop: str,
    limit: str,
    trigger_price: str,
    book_side: BookSide,
    book_price: str,
) -> None:
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"trade-trigger-{side.value}",
        side=side,
        quantity="2",
        order_type=OrderType.STOP_LIMIT,
        price=limit,
        stop=stop,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"trade-trigger-book-{side.value}", ASSET, seconds=1, sequence=100),
        bids=(BookLevel(fp("100" if side is Side.SELL else "99"), fp("1")),),
        asks=(BookLevel(fp("101" if side is Side.SELL else "100"), fp("1")),),
    )
    assert model.match(snapshot, broker.open_orders) == ()

    trigger = TradeEvent(
        **event_fields(f"trade-trigger-{side.value}", ASSET, seconds=2),
        price=fp(trigger_price),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    assert model.match(trigger, broker.open_orders) == ()

    first_visible = BookDeltaEvent(
        **event_fields(f"active-delta-1-{side.value}", ASSET, seconds=3, sequence=101),
        side=book_side,
        action=BookAction.UPSERT,
        price=fp(book_price),
        quantity=fp("1"),
        previous_sequence=100,
    )
    first_fill = model.match(first_visible, broker.open_orders)
    assert [(fill.order_id, fill.quantity, fill.liquidity_role.value) for fill in first_fill] == [
        (waiting.order_id, fp("1"), "taker")
    ]
    broker.apply_fill(first_fill[0])

    second_visible = BookDeltaEvent(
        **event_fields(f"active-delta-2-{side.value}", ASSET, seconds=4, sequence=102),
        side=book_side,
        action=BookAction.UPSERT,
        price=fp(book_price),
        quantity=fp("2"),
        previous_sequence=101,
    )
    second_fill = model.match(second_visible, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in second_fill] == [(waiting.order_id, fp("1"))]


@pytest.mark.parametrize(
    ("side", "order_type", "stop", "limit", "trigger_price", "book_price"),
    [
        (Side.BUY, OrderType.STOP, "105", None, "106", "100"),
        (Side.BUY, OrderType.STOP_LIMIT, "105", "106", "106", "100"),
        (Side.SELL, OrderType.STOP, "95", None, "94", "100"),
        (Side.SELL, OrderType.STOP_LIMIT, "95", "94", "94", "100"),
    ],
)
def test_l2_trade_triggered_stop_takes_saved_book_on_next_trade(
    side: Side,
    order_type: OrderType,
    stop: str,
    limit: str | None,
    trigger_price: str,
    book_price: str,
) -> None:
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"trade-to-trade-{order_type.value}-{side.value}",
        side=side,
        quantity="1",
        order_type=order_type,
        price=limit,
        stop=stop,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"trade-to-trade-book-{side.value}", ASSET, seconds=1, sequence=105),
        bids=(BookLevel(fp("99" if side is Side.BUY else "100"), fp("2")),),
        asks=(BookLevel(fp("100" if side is Side.BUY else "101"), fp("2")),),
    )
    assert model.match(snapshot, broker.open_orders) == ()
    authoritative_book = {
        "bids": dict(model._books[ASSET]["bids"]),
        "asks": dict(model._books[ASSET]["asks"]),
    }

    trigger = TradeEvent(
        **event_fields(f"trade-to-trade-trigger-{side.value}", ASSET, seconds=2),
        price=fp(trigger_price),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
    )
    assert model.match(trigger, broker.open_orders) == ()

    next_trade = TradeEvent(
        **event_fields(f"trade-to-trade-next-{side.value}", ASSET, seconds=3),
        price=fp(book_price),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    fills = model.match(next_trade, broker.open_orders)
    assert [
        (fill.order_id, fill.quantity, fill.price, fill.liquidity_role.value) for fill in fills
    ] == [(waiting.order_id, fp("1"), fp(book_price), "taker")]
    assert model._books[ASSET]["bids"] == authoritative_book["bids"]
    assert model._books[ASSET]["asks"] == authoritative_book["asks"]


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_l2_orders_submitted_after_snapshot_take_saved_book_on_next_trade(side: Side) -> None:
    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    bid_price = "99" if side is Side.BUY else "100"
    ask_price = "100" if side is Side.BUY else "101"
    snapshot = BookSnapshotEvent(
        **event_fields(f"post-snapshot-book-{side.value}", ASSET, seconds=1, sequence=106),
        bids=(BookLevel(fp(bid_price), fp("2")),),
        asks=(BookLevel(fp(ask_price), fp("2")),),
    )
    assert model.match(snapshot, ()) == ()
    market = order(
        broker,
        f"post-snapshot-market-{side.value}",
        side=side,
        quantity="1",
        order_type=OrderType.MARKET,
        tif=TimeInForce.IOC,
        price=None,
        created_seconds=2,
    )
    marketable_limit = order(
        broker,
        f"post-snapshot-limit-{side.value}",
        side=side,
        quantity="1",
        price="101" if side is Side.BUY else "99",
        created_seconds=2,
    )

    trade = TradeEvent(
        **event_fields(f"post-snapshot-trade-{side.value}", ASSET, seconds=3),
        price=fp("100"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity, fill.liquidity_role.value) for fill in fills] == [
        (market.order_id, fp("1"), "taker"),
        (marketable_limit.order_id, fp("1"), "taker"),
    ]
    assert model._books[ASSET]["bids"] == {fp(bid_price).units: fp("2")}
    assert model._books[ASSET]["asks"] == {fp(ask_price).units: fp("2")}


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_l2_saved_book_trade_respects_multilevel_fok_and_ioc(side: Side) -> None:
    prices = ("100", "101") if side is Side.BUY else ("100", "99")
    limit = prices[-1]

    def snapshot(event_id: str) -> BookSnapshotEvent:
        levels = tuple(BookLevel(fp(price), fp("1")) for price in prices)
        return BookSnapshotEvent(
            **event_fields(event_id, ASSET, seconds=1, sequence=107),
            bids=levels if side is Side.SELL else (BookLevel(fp("98"), fp("1")),),
            asks=levels if side is Side.BUY else (BookLevel(fp("102"), fp("1")),),
        )

    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    assert model.match(snapshot(f"saved-book-fok-{side.value}"), ()) == ()
    fok = order(
        broker,
        f"saved-book-fok-order-{side.value}",
        side=side,
        quantity="3",
        price=limit,
        tif=TimeInForce.FOK,
        created_seconds=2,
    )
    trade = TradeEvent(
        **event_fields(f"saved-book-fok-trade-{side.value}", ASSET, seconds=3),
        price=fp(limit),
        quantity=fp("5"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    assert model.match(trade, broker.open_orders) == ()
    assert fok.order_id not in model._queue_keys

    broker = DeterministicBroker()
    model.reset()
    assert model.match(snapshot(f"saved-book-ioc-{side.value}"), ()) == ()
    ioc = order(
        broker,
        f"saved-book-ioc-order-{side.value}",
        side=side,
        quantity="3",
        price=limit,
        tif=TimeInForce.IOC,
        created_seconds=2,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity, fill.price) for fill in fills] == [
        (ioc.order_id, fp("1"), fp(prices[0])),
        (ioc.order_id, fp("1"), fp(prices[1])),
    ]
    assert sum((fill.quantity.to_decimal() for fill in fills), Decimal(0)) == Decimal(2)
    assert ioc.order_id not in model._queue_keys


def test_l2_saved_book_taker_and_same_trade_maker_share_order_remaining() -> None:
    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("same-trade-taker-maker-book", ASSET, seconds=1, sequence=108),
        bids=(BookLevel(fp("99"), fp("1")),),
        asks=(BookLevel(fp("100"), fp("1")),),
    )
    assert model.match(snapshot, ()) == ()
    marketable = order(
        broker,
        "same-trade-taker-maker-order",
        quantity="2",
        price="101",
        created_seconds=2,
    )
    trade = TradeEvent(
        **event_fields("same-trade-taker-maker-trade", ASSET, seconds=3),
        price=fp("101"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.quantity, fill.price, fill.liquidity_role.value) for fill in fills] == [
        (fp("1"), fp("100"), "taker"),
        (fp("1"), fp("101"), "maker"),
    ]
    assert sum((fill.quantity.to_decimal() for fill in fills), Decimal(0)) == Decimal(2)
    assert all(fill.order_id == marketable.order_id for fill in fills)
    assert model._books[ASSET]["asks"] == {fp("100").units: fp("1")}


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
def test_l2_stop_uses_top_of_book_not_deep_levels_on_snapshot_and_delta(side: Side) -> None:
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"top-only-stop-{side.value}",
        side=side,
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="105" if side is Side.BUY else "95",
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"top-only-book-{side.value}", ASSET, seconds=1, sequence=110),
        bids=(
            BookLevel(fp("100"), fp("1")),
            BookLevel(fp("94"), fp("1")),
        ),
        asks=(
            BookLevel(fp("101"), fp("1")),
            BookLevel(fp("106"), fp("1")),
        ),
    )
    assert model.match(snapshot, broker.open_orders) == ()

    reveal_trigger = BookDeltaEvent(
        **event_fields(f"top-only-delta-{side.value}", ASSET, seconds=2, sequence=111),
        side=BookSide.ASK if side is Side.BUY else BookSide.BID,
        action=BookAction.DELETE,
        price=fp("101" if side is Side.BUY else "100"),
        quantity=fp("0"),
        previous_sequence=110,
    )
    fills = model.match(reveal_trigger, broker.open_orders)
    assert [(fill.order_id, fill.price) for fill in fills] == [
        (waiting.order_id, fp("106" if side is Side.BUY else "94"))
    ]


def test_l2_trigger_time_places_old_stop_limit_behind_existing_resting_order() -> None:
    broker = DeterministicBroker()
    dormant = order(
        broker,
        "old-dormant-stop-limit",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="99",
        stop="105",
        created_seconds=0,
    )
    resting = order(
        broker,
        "newer-resting-limit",
        quantity="1",
        price="99",
        created_seconds=1,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("activation-priority-book", ASSET, seconds=2, sequence=120),
        bids=(BookLevel(fp("98"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("1")),),
    )
    assert model.match(snapshot, broker.open_orders) == ()
    trigger = TradeEvent(
        **event_fields("activation-priority-trigger", ASSET, seconds=3),
        price=fp("105"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY,
    )
    assert model.match(trigger, broker.open_orders) == ()

    first_trade = TradeEvent(
        **event_fields("activation-priority-trade-1", ASSET, seconds=4),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    first_fill = model.match(first_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in first_fill] == [(resting.order_id, fp("1"))]
    broker.apply_fill(first_fill[0])

    second_trade = TradeEvent(
        **event_fields("activation-priority-trade-2", ASSET, seconds=5),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    second_fill = model.match(second_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in second_fill] == [(dormant.order_id, fp("1"))]


@pytest.mark.parametrize(
    ("side", "book_side", "book_price"),
    [
        (Side.BUY, BookSide.ASK, "100"),
        (Side.SELL, BookSide.BID, "100"),
    ],
)
def test_l2_gtc_market_persistently_consumes_saved_liquidity_until_market_data_replenishes(
    side: Side,
    book_side: BookSide,
    book_price: str,
) -> None:
    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"persistent-market-book-{side.value}", ASSET, seconds=1, sequence=200),
        bids=(BookLevel(fp("100" if side is Side.SELL else "99"), fp("1")),),
        asks=(BookLevel(fp("101" if side is Side.SELL else "100"), fp("1")),),
    )
    assert model.match(snapshot, ()) == ()
    authoritative = {
        "bids": dict(model._books[ASSET]["bids"]),
        "asks": dict(model._books[ASSET]["asks"]),
    }
    market = order(
        broker,
        f"persistent-market-{side.value}",
        side=side,
        quantity="3",
        order_type=OrderType.MARKET,
        price=None,
        created_seconds=2,
    )

    first_trade = TradeEvent(
        **event_fields(f"persistent-market-first-{side.value}", ASSET, seconds=3),
        price=fp(book_price),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    first_fill = model.match(first_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in first_fill] == [(market.order_id, fp("1"))]
    broker.apply_fill(first_fill[0])

    second_trade = TradeEvent(
        **event_fields(f"persistent-market-second-{side.value}", ASSET, seconds=4),
        price=fp(book_price),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    assert model.match(second_trade, broker.open_orders) == ()
    assert model._books[ASSET]["bids"] == authoritative["bids"]
    assert model._books[ASSET]["asks"] == authoritative["asks"]

    delta = BookDeltaEvent(
        **event_fields(f"persistent-market-delta-{side.value}", ASSET, seconds=5, sequence=201),
        side=book_side,
        action=BookAction.UPSERT,
        price=fp(book_price),
        quantity=fp("2"),
        previous_sequence=200,
    )
    delta_fill = model.match(delta, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in delta_fill] == [(market.order_id, fp("1"))]
    broker.apply_fill(delta_fill[0])

    refreshed = BookSnapshotEvent(
        **event_fields(f"persistent-market-refresh-{side.value}", ASSET, seconds=6, sequence=202),
        bids=(BookLevel(fp("100" if side is Side.SELL else "99"), fp("1")),),
        asks=(BookLevel(fp("101" if side is Side.SELL else "100"), fp("1")),),
    )
    snapshot_fill = model.match(refreshed, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in snapshot_fill] == [
        (market.order_id, fp("1"))
    ]


@pytest.mark.parametrize(
    ("side", "order_type", "stop", "limit", "trigger_price", "book_side", "book_price"),
    [
        (Side.BUY, OrderType.STOP, "105", None, "106", BookSide.ASK, "100"),
        (Side.BUY, OrderType.STOP_LIMIT, "105", "106", "106", BookSide.ASK, "100"),
        (Side.SELL, OrderType.STOP, "95", None, "94", BookSide.BID, "100"),
        (Side.SELL, OrderType.STOP_LIMIT, "95", "94", "94", BookSide.BID, "100"),
    ],
)
def test_l2_triggered_stop_does_not_reuse_saved_liquidity_across_trades(
    side: Side,
    order_type: OrderType,
    stop: str,
    limit: str | None,
    trigger_price: str,
    book_side: BookSide,
    book_price: str,
) -> None:
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"persistent-stop-{order_type.value}-{side.value}",
        side=side,
        quantity="3",
        order_type=order_type,
        price=limit,
        stop=stop,
    )
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"persistent-stop-book-{side.value}", ASSET, seconds=1, sequence=210),
        bids=(BookLevel(fp("100" if side is Side.SELL else "99"), fp("1")),),
        asks=(BookLevel(fp("101" if side is Side.SELL else "100"), fp("1")),),
    )
    assert model.match(snapshot, broker.open_orders) == ()
    authoritative = {
        "bids": dict(model._books[ASSET]["bids"]),
        "asks": dict(model._books[ASSET]["asks"]),
    }
    trigger = TradeEvent(
        **event_fields(f"persistent-stop-trigger-{side.value}", ASSET, seconds=2),
        price=fp(trigger_price),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
    )
    assert model.match(trigger, broker.open_orders) == ()

    first_trade = TradeEvent(
        **event_fields(f"persistent-stop-first-{side.value}", ASSET, seconds=3),
        price=fp(book_price),
        quantity=fp("10"),
        aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
    )
    first_fill = model.match(first_trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in first_fill] == [(waiting.order_id, fp("1"))]
    broker.apply_fill(first_fill[0])

    repeated_trade = TradeEvent(
        **event_fields(f"persistent-stop-repeated-{side.value}", ASSET, seconds=4),
        price=fp(book_price),
        quantity=fp("10"),
        aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
    )
    assert model.match(repeated_trade, broker.open_orders) == ()
    assert model._books[ASSET]["bids"] == authoritative["bids"]
    assert model._books[ASSET]["asks"] == authoritative["asks"]

    delta = BookDeltaEvent(
        **event_fields(f"persistent-stop-delta-{side.value}", ASSET, seconds=5, sequence=211),
        side=book_side,
        action=BookAction.UPSERT,
        price=fp(book_price),
        quantity=fp("2"),
        previous_sequence=210,
    )
    delta_fill = model.match(delta, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in delta_fill] == [(waiting.order_id, fp("1"))]
    broker.apply_fill(delta_fill[0])

    refreshed = BookSnapshotEvent(
        **event_fields(f"persistent-stop-refresh-{side.value}", ASSET, seconds=6, sequence=212),
        bids=(BookLevel(fp("100" if side is Side.SELL else "99"), fp("1")),),
        asks=(BookLevel(fp("101" if side is Side.SELL else "100"), fp("1")),),
    )
    snapshot_fill = model.match(refreshed, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in snapshot_fill] == [
        (waiting.order_id, fp("1"))
    ]


def test_l2_delta_preserves_simulated_consumption_gap_for_level_lifecycle() -> None:
    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("overlay-delta-book", ASSET, seconds=1, sequence=220),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, ()) == ()
    order(
        broker,
        "overlay-delta-market",
        quantity="2",
        order_type=OrderType.MARKET,
        price=None,
        created_seconds=2,
    )
    trade = TradeEvent(
        **event_fields("overlay-delta-trade", ASSET, seconds=3),
        price=fp("100"),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = model.match(trade, broker.open_orders)
    assert sum((fill.quantity.to_decimal() for fill in fills), Decimal(0)) == Decimal(2)
    broker.apply_fill(fills[0])
    assert model._books[ASSET]["asks"] == {fp("100").units: fp("5")}
    assert model._liquidity_books[ASSET]["asks"] == {fp("100").units: fp("3")}

    updates = [
        (221, BookAction.UPSERT, "100", "7", {fp("100").units: fp("5")}),
        (222, BookAction.UPSERT, "100", "4", {fp("100").units: fp("2")}),
        (
            223,
            BookAction.UPSERT,
            "101",
            "1.25",
            {fp("100").units: fp("2"), fp("101").units: fp("1.25")},
        ),
        (224, BookAction.DELETE, "101", "0", {fp("100").units: fp("2")}),
        (225, BookAction.UPSERT, "100", "1", {}),
    ]
    previous_sequence = 220
    for sequence, action, price, quantity, expected in updates:
        delta = BookDeltaEvent(
            **event_fields(
                f"overlay-delta-{sequence}", ASSET, seconds=sequence - 217, sequence=sequence
            ),
            side=BookSide.ASK,
            action=action,
            price=fp(price),
            quantity=fp(quantity),
            previous_sequence=previous_sequence,
        )
        assert model.match(delta, broker.open_orders) == ()
        assert model._liquidity_books[ASSET]["asks"] == expected
        previous_sequence = sequence


@pytest.mark.parametrize("taker_side", [Side.BUY, Side.SELL])
def test_l2_taker_consumption_advances_only_the_corresponding_passive_queue(
    taker_side: Side,
) -> None:
    other = "crypto:test:ETHUSDT"
    registry = {
        ASSET: instrument(),
        other: spec(
            other,
            asset_class=AssetClass.CRYPTO,
            product_type="spot",
            settlement_currency="USDT",
            base_currency="ETH",
            quote_currency="USDT",
        ),
    }
    broker = DeterministicBroker()
    affected_side = Side.SELL if taker_side is Side.BUY else Side.BUY
    affected_price = "100" if affected_side is Side.SELL else "99"
    unaffected_price = "99" if taker_side is Side.BUY else "100"
    affected = order(
        broker,
        f"overlay-queue-affected-{taker_side.value}",
        side=affected_side,
        quantity="1",
        price=affected_price,
    )
    unaffected = order(
        broker,
        f"overlay-queue-unaffected-{taker_side.value}",
        side=taker_side,
        quantity="1",
        price=unaffected_price,
    )
    other_passive = order(
        broker,
        f"overlay-queue-other-{taker_side.value}",
        instrument_id=other,
        side=affected_side,
        quantity="1",
        price=affected_price,
    )
    model = L2MatchingModel(registry)
    snapshot = BookSnapshotEvent(
        **event_fields("overlay-queue-book", ASSET, seconds=1, sequence=230),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    other_snapshot = BookSnapshotEvent(
        **event_fields("overlay-queue-other-book", other, seconds=1, sequence=230),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, broker.open_orders) == ()
    assert model.match(other_snapshot, broker.open_orders) == ()
    assert model._queue_ahead[affected.order_id] == Decimal(5)
    assert model._queue_ahead[unaffected.order_id] == Decimal(5)
    assert model._queue_ahead[other_passive.order_id] == Decimal(5)

    taker = order(
        broker,
        f"overlay-queue-taker-{taker_side.value}",
        side=taker_side,
        quantity="2",
        order_type=OrderType.MARKET,
        price=None,
        created_seconds=2,
    )
    trade = TradeEvent(
        **event_fields("overlay-queue-trade", ASSET, seconds=3),
        price=fp("100" if taker_side is Side.BUY else "99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL if taker_side is Side.BUY else AggressorSide.BUY,
    )
    fills = model.match(trade, broker.open_orders)
    assert [(fill.order_id, fill.quantity) for fill in fills] == [(taker.order_id, fp("2"))]
    broker.apply_fill(fills[0])
    assert model._queue_ahead[affected.order_id] == Decimal(3)
    assert model._queue_ahead[unaffected.order_id] == Decimal(5)
    assert model._queue_ahead[other_passive.order_id] == Decimal(5)

    later = order(
        broker,
        f"overlay-queue-later-{taker_side.value}",
        side=affected_side,
        quantity="1",
        price=affected_price,
        created_seconds=4,
    )
    quote = QuoteEvent(
        **event_fields("overlay-queue-quote", ASSET, seconds=4),
        bid_price=fp("99"),
        bid_quantity=fp("5"),
        ask_price=fp("100"),
        ask_quantity=fp("5"),
    )
    assert model.match(quote, broker.open_orders) == ()
    assert model._queue_ahead[later.order_id] == Decimal(4)


def test_l2_liquidity_overlay_is_captured_and_restored() -> None:
    broker = DeterministicBroker()
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("overlay-checkpoint-book", ASSET, seconds=1, sequence=240),
        bids=(BookLevel(fp("99"), fp("2")),),
        asks=(BookLevel(fp("100"), fp("2")),),
    )
    assert model.match(snapshot, ()) == ()
    checkpoint = model.capture_state()
    order(
        broker,
        "overlay-checkpoint-market",
        quantity="1",
        order_type=OrderType.MARKET,
        price=None,
        created_seconds=2,
    )
    trade = TradeEvent(
        **event_fields("overlay-checkpoint-trade", ASSET, seconds=3),
        price=fp("100"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    assert len(model.match(trade, broker.open_orders)) == 1
    assert model._liquidity_books[ASSET]["asks"] == {fp("100").units: fp("1")}

    model.restore_state(checkpoint)
    assert model._liquidity_books[ASSET]["asks"] == {fp("100").units: fp("2")}
    assert len(model.match(trade, broker.open_orders)) == 1


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.STOP, OrderType.STOP_LIMIT])
@pytest.mark.parametrize(
    ("level_quantities", "expected_total"),
    [
        (("0.335", "0.335", "0.335"), Decimal(0)),
        (("0.33", "0.33", "0.34"), Decimal(1)),
        (("0.33", "0.33", "0.33"), Decimal(0)),
    ],
    ids=["per-level-rounding-short", "exact", "one-order-unit-short"],
)
def test_l2_fok_uses_an_atomic_exact_fixed_point_execution_plan(
    side: Side,
    order_type: OrderType,
    level_quantities: tuple[str, str, str],
    expected_total: Decimal,
) -> None:
    registry = {
        ASSET: spec(
            ASSET,
            asset_class=AssetClass.CRYPTO,
            product_type="spot",
            settlement_currency="USDT",
            base_currency="BTC",
            quote_currency="USDT",
            quantity_step="0.001",
        )
    }
    model = L2MatchingModel(registry)
    snapshot = BookSnapshotEvent(
        **event_fields(
            f"fok-plan-book-{side.value}-{order_type.value}-{expected_total}",
            ASSET,
            seconds=1,
            sequence=300,
        ),
        bids=tuple(
            BookLevel(fp(price), fp(quantity, 3))
            for price, quantity in zip(("100", "99", "98"), level_quantities, strict=True)
        ),
        asks=tuple(
            BookLevel(fp(price), fp(quantity, 3))
            for price, quantity in zip(("101", "102", "103"), level_quantities, strict=True)
        ),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"fok-plan-order-{side.value}-{order_type.value}-{expected_total}",
        side=side,
        quantity="1.00",
        order_type=order_type,
        tif=TimeInForce.FOK,
        price=None
        if order_type in {OrderType.MARKET, OrderType.STOP}
        else ("103" if side is Side.BUY else "98"),
        stop=None if order_type is OrderType.MARKET else ("105" if side is Side.BUY else "95"),
        created_seconds=2,
    )
    if order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
        trigger = TradeEvent(
            **event_fields(
                f"fok-plan-trigger-{side.value}-{order_type.value}-{expected_total}",
                ASSET,
                seconds=3,
            ),
            price=fp("106" if side is Side.BUY else "94"),
            quantity=fp("1"),
            aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
        )
        assert model.match(trigger, [waiting]) == ()
        match_seconds = 4
    else:
        match_seconds = 3

    before = pickle.dumps(model.capture_state(), protocol=5)
    match_event = TradeEvent(
        **event_fields(
            f"fok-plan-match-{side.value}-{order_type.value}-{expected_total}",
            ASSET,
            seconds=match_seconds,
        ),
        price=fp("101" if side is Side.BUY else "100"),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    fills = tuple(model.match(match_event, [waiting]))
    total = sum((fill.quantity.to_decimal() for fill in fills), Decimal(0))
    assert total == expected_total
    if expected_total == 0:
        assert fills == ()
        assert pickle.dumps(model.capture_state(), protocol=5) == before
    else:
        expected_prices = (
            [Decimal(101), Decimal(102), Decimal(103)]
            if side is Side.BUY
            else [Decimal(100), Decimal(99), Decimal(98)]
        )
        assert [fill.price.to_decimal() for fill in fills] == expected_prices
        assert [fill.quantity.to_decimal() for fill in fills] == [
            Decimal(value) for value in level_quantities
        ]


@pytest.mark.parametrize("side", [Side.BUY, Side.SELL])
@pytest.mark.parametrize("order_type", [OrderType.MARKET, OrderType.STOP, OrderType.STOP_LIMIT])
def test_l2_ioc_reuses_execution_plan_and_keeps_quantized_partial_fill(
    side: Side,
    order_type: OrderType,
) -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"ioc-plan-book-{side.value}-{order_type.value}", ASSET, sequence=310),
        bids=tuple(BookLevel(fp(price), fp("0.335", 3)) for price in ("100", "99", "98")),
        asks=tuple(BookLevel(fp(price), fp("0.335", 3)) for price in ("101", "102", "103")),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    waiting = order(
        broker,
        f"ioc-plan-order-{side.value}-{order_type.value}",
        side=side,
        quantity="1.00",
        order_type=order_type,
        tif=TimeInForce.IOC,
        price=None
        if order_type in {OrderType.MARKET, OrderType.STOP}
        else ("103" if side is Side.BUY else "98"),
        stop=None if order_type is OrderType.MARKET else ("105" if side is Side.BUY else "95"),
    )
    if order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
        trigger = TradeEvent(
            **event_fields(f"ioc-plan-trigger-{side.value}-{order_type.value}", ASSET, seconds=1),
            price=fp("106" if side is Side.BUY else "94"),
            quantity=fp("1"),
            aggressor_side=AggressorSide.BUY if side is Side.BUY else AggressorSide.SELL,
        )
        assert model.match(trigger, [waiting]) == ()
        match_seconds = 2
    else:
        match_seconds = 1
    match_event = TradeEvent(
        **event_fields(
            f"ioc-plan-match-{side.value}-{order_type.value}", ASSET, seconds=match_seconds
        ),
        price=fp("101" if side is Side.BUY else "100"),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL if side is Side.BUY else AggressorSide.BUY,
    )
    fills = model.match(match_event, [waiting])
    assert sum((fill.quantity.to_decimal() for fill in fills), Decimal(0)) == Decimal("0.99")


def test_l2_failed_fok_leaves_all_overlay_for_next_exact_order() -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("fok-shared-overlay-book", ASSET, seconds=1, sequence=320),
        bids=(BookLevel(fp("100"), fp("1")),),
        asks=tuple(BookLevel(fp(price), fp("0.335", 3)) for price in ("101", "102", "103")),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    rejected = order(
        broker,
        "fok-shared-overlay-rejected",
        quantity="1.00",
        order_type=OrderType.MARKET,
        tif=TimeInForce.FOK,
        price=None,
        created_seconds=2,
    )
    accepted = broker.submit(
        OrderIntent(
            idempotency_key="fok-shared-overlay-accepted",
            account_id="account",
            strategy_id="strategy",
            instrument_id=ASSET,
            side=Side.BUY,
            quantity=fp("1.005", 3),
            order_type=OrderType.MARKET,
            time_in_force=TimeInForce.FOK,
            created_at=T0 + timedelta(seconds=3),
        )
    )
    event = TradeEvent(
        **event_fields("fok-shared-overlay-match", ASSET, seconds=4),
        price=fp("101"),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL,
    )
    fills = tuple(model.match(event, [rejected, accepted]))
    assert {fill.order_id for fill in fills} == {accepted.order_id}
    assert sum((fill.quantity.to_decimal() for fill in fills), Decimal(0)) == Decimal("1.005")
    assert model._liquidity_books[ASSET]["asks"] == {}


@pytest.mark.parametrize("failure", ["sequence-gap", "price-scale", "delete-absent"])
def test_l2_invalid_delta_is_atomic_before_queue_reconciliation(failure: str) -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields(f"invalid-delta-base-{failure}", ASSET, seconds=1, sequence=330),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    passive = order(
        broker,
        f"invalid-delta-passive-{failure}",
        side=Side.SELL,
        quantity="1",
        price="100",
        created_seconds=2,
    )
    if failure == "sequence-gap":
        event = BookDeltaEvent(
            **event_fields("invalid-delta-sequence", ASSET, seconds=3, sequence=332),
            side=BookSide.ASK,
            action=BookAction.UPSERT,
            price=fp("100"),
            quantity=fp("6"),
            previous_sequence=331,
        )
        message = "sequence gap"
    elif failure == "price-scale":
        event = BookDeltaEvent(
            **event_fields("invalid-delta-scale", ASSET, seconds=3, sequence=331),
            side=BookSide.ASK,
            action=BookAction.UPSERT,
            price=fp("100", 3),
            quantity=fp("6"),
            previous_sequence=330,
        )
        message = "price scale"
    else:
        event = BookDeltaEvent(
            **event_fields("invalid-delta-absent", ASSET, seconds=3, sequence=331),
            side=BookSide.ASK,
            action=BookAction.DELETE,
            price=fp("101"),
            quantity=fp("0"),
            previous_sequence=330,
        )
        message = "absent price level"
    before = pickle.dumps(model.capture_state(), protocol=5)
    with pytest.raises(ValidationError, match=message):
        model.match(event, [passive])
    assert pickle.dumps(model.capture_state(), protocol=5) == before
    assert passive.order_id not in model._queue_keys


def test_l2_snapshot_mixed_price_scale_is_atomic_before_queue_reconciliation() -> None:
    model = L2MatchingModel({ASSET: instrument()})
    valid = BookSnapshotEvent(
        **event_fields("invalid-snapshot-base", ASSET, seconds=1, sequence=340),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(valid, ()) == ()
    invalid = BookSnapshotEvent(
        **event_fields("invalid-snapshot-mixed", ASSET, seconds=3, sequence=341),
        bids=(BookLevel(fp("99"), fp("4")),),
        asks=(BookLevel(fp("100"), fp("4")),),
    )
    object.__setattr__(invalid, "asks", (BookLevel(fp("100", 3), fp("4")),))
    broker = DeterministicBroker()
    passive = order(broker, "invalid-snapshot-passive", side=Side.SELL, price="100")
    before = pickle.dumps(model.capture_state(), protocol=5)
    with pytest.raises(ValidationError, match="one price scale"):
        model.match(invalid, [passive])
    assert pickle.dumps(model.capture_state(), protocol=5) == before
    assert passive.order_id not in model._queue_keys


def test_l2_trade_price_scale_mismatch_is_atomic_before_taker_stop_and_queue_changes() -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("invalid-trade-base", ASSET, seconds=1, sequence=350),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    passive = order(broker, "invalid-trade-passive", side=Side.SELL, price="100")
    taker = order(
        broker,
        "invalid-trade-taker",
        quantity="1",
        order_type=OrderType.MARKET,
        price=None,
    )
    dormant = order(
        broker,
        "invalid-trade-stop",
        quantity="1",
        order_type=OrderType.STOP,
        tif=TimeInForce.FOK,
        price=None,
        stop="105",
    )
    invalid = TradeEvent(
        **event_fields("invalid-trade-scale", ASSET, seconds=2),
        price=fp("106", 3),
        quantity=fp("10"),
        aggressor_side=AggressorSide.BUY,
    )
    before = pickle.dumps(model.capture_state(), protocol=5)
    with pytest.raises(ValidationError, match="price scale"):
        model.match(invalid, [passive, taker, dormant])
    assert pickle.dumps(model.capture_state(), protocol=5) == before
    assert passive.order_id not in model._queue_keys
    assert dormant.order_id not in model._triggered_stops


def test_l2_delta_fixed_point_staging_failure_precedes_all_state_commits(monkeypatch) -> None:
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("delta-stage-base", ASSET, seconds=1, sequence=360),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(snapshot, ()) == ()
    broker = DeterministicBroker()
    passive = order(broker, "delta-stage-passive", side=Side.SELL, price="100")
    event = BookDeltaEvent(
        **event_fields("delta-stage-failure", ASSET, seconds=2, sequence=361),
        side=BookSide.ASK,
        action=BookAction.UPSERT,
        price=fp("100"),
        quantity=fp("6"),
        previous_sequence=360,
    )

    def fail_fixed_point(*args, **kwargs):
        raise ValidationError("injected FixedPoint staging failure")

    monkeypatch.setattr(matching_module, "fixed", fail_fixed_point)
    before = pickle.dumps(model.capture_state(), protocol=5)
    with pytest.raises(ValidationError, match="injected FixedPoint staging failure"):
        model.match(event, [passive])
    assert pickle.dumps(model.capture_state(), protocol=5) == before
    assert passive.order_id not in model._queue_keys


@pytest.mark.parametrize("event_type", ["snapshot", "delta"])
def test_l2_new_passive_order_queues_against_the_new_overlay(event_type: str) -> None:
    model = L2MatchingModel({ASSET: instrument()})
    initial = BookSnapshotEvent(
        **event_fields(f"new-overlay-base-{event_type}", ASSET, seconds=1, sequence=370),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("5")),),
    )
    assert model.match(initial, ()) == ()
    broker = DeterministicBroker()
    passive = order(
        broker,
        f"new-overlay-passive-{event_type}",
        side=Side.BUY,
        quantity="1",
        price="99",
        created_seconds=2,
    )
    if event_type == "snapshot":
        event = BookSnapshotEvent(
            **event_fields("new-overlay-snapshot", ASSET, seconds=3, sequence=371),
            bids=(BookLevel(fp("99"), fp("2")),),
            asks=(BookLevel(fp("100"), fp("5")),),
        )
        expected = Decimal(2)
    else:
        event = BookDeltaEvent(
            **event_fields("new-overlay-delta", ASSET, seconds=3, sequence=371),
            side=BookSide.BID,
            action=BookAction.UPSERT,
            price=fp("99"),
            quantity=fp("3"),
            previous_sequence=370,
        )
        expected = Decimal(3)
    assert model.match(event, [passive]) == ()
    assert model._queue_ahead[passive.order_id] == expected
