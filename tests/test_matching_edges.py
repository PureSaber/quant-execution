from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import event_fields, fp
from quant_data_kit import (
    AggressorSide,
    BookAction,
    BookDeltaEvent,
    BookLevel,
    BookSide,
    BookSnapshotEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError
from test_matching import ASSET, bar, instrument, order

from quant_execution import OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.matching import (
    BarMatchingModel,
    L2MatchingModel,
    TradeBBOModel,
    _BaseMatchingModel,
    _quantity_from_available,
)


def quote(event_id: str, seconds: int = 1) -> QuoteEvent:
    return QuoteEvent(
        **event_fields(event_id, ASSET, seconds=seconds),
        bid_price=fp("99"),
        bid_quantity=fp("2"),
        ask_price=fp("101"),
        ask_quantity=fp("2"),
    )


def snapshot(event_id: str = "snapshot", sequence: int = 10) -> BookSnapshotEvent:
    return BookSnapshotEvent(
        **event_fields(event_id, ASSET, seconds=1, sequence=sequence),
        bids=(BookLevel(fp("99"), fp("5")), BookLevel(fp("98"), fp("2"))),
        asks=(BookLevel(fp("100"), fp("2")), BookLevel(fp("101"), fp("3"))),
    )


def test_matching_configuration_and_nonmatching_events_fail_or_return_empty() -> None:
    with pytest.raises(ValidationError, match="latency"):
        BarMatchingModel({ASSET: instrument()}, latency=timedelta(seconds=-1))
    with pytest.raises(ValidationError, match="participation"):
        BarMatchingModel({ASSET: instrument()}, participation_rate="0")
    with pytest.raises(ValidationError, match="slippage"):
        BarMatchingModel({ASSET: instrument()}, slippage_ticks=True)
    model = BarMatchingModel({ASSET: instrument()})
    assert model.match(replace(bar("incomplete", 60), is_complete=False), []) == ()
    status = StatusEvent(**event_fields("status", ASSET, seconds=1), status="open", reason="")
    assert model.match(status, []) == ()
    broker = DeterministicBroker()
    missing = order(broker, "missing-spec")
    with pytest.raises(ValidationError, match="missing InstrumentSpec"):
        BarMatchingModel({}).match(bar("bar", 60), [missing])


def test_bar_sell_stop_and_open_triggered_stop_limit_rules() -> None:
    broker = DeterministicBroker()
    sell_market = order(
        broker,
        "sell-market",
        side=Side.SELL,
        quantity="1",
        order_type=OrderType.MARKET,
        price=None,
    )
    sell_limit = order(broker, "sell-limit", side=Side.SELL, quantity="1", price="104")
    sell_stop = order(
        broker,
        "sell-stop",
        side=Side.SELL,
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="99",
    )
    untouched = order(
        broker,
        "untouched",
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="110",
    )
    opened = order(
        broker,
        "opened-stop-limit",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="102",
        stop="100",
    )
    fills = BarMatchingModel({ASSET: instrument()}, participation_rate="1", slippage_ticks=1).match(
        bar("all", 60), [sell_market, sell_limit, sell_stop, untouched, opened]
    )
    prices = {item.order_id: item.price.to_decimal() for item in fills}
    assert prices[sell_market.order_id].to_eng_string() == "100.99"
    assert prices[sell_limit.order_id].to_eng_string() == "104.00"
    assert prices[sell_stop.order_id].to_eng_string() == "98.99"
    assert untouched.order_id not in prices
    assert prices[opened.order_id].to_eng_string() == "102.00"


def test_bbo_covers_sell_fok_stop_and_visible_price_guards() -> None:
    broker = DeterministicBroker()
    sell = order(broker, "sell", side=Side.SELL, quantity="1", price="98")
    fok = order(broker, "fok", quantity="3", price="102", tif=TimeInForce.FOK)
    stop = order(
        broker,
        "stop",
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="100",
    )
    model = TradeBBOModel({ASSET: instrument()})
    fills = model.match(quote("quote"), [sell, fok, stop])
    assert {item.order_id for item in fills} == {sell.order_id, stop.order_id}
    assert all(item.liquidity_role.value == "taker" for item in fills)
    broker = DeterministicBroker()
    buy = order(broker, "cross-buy", price="102")
    model.reset()
    model.match(quote("stored"), [])
    crossed_ask = TradeEvent(
        **event_fields("crossed-ask", ASSET, seconds=2),
        price=fp("102"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    with pytest.raises(ValidationError, match="visible ask"):
        model.match(crossed_ask, [buy])
    broker = DeterministicBroker()
    sell = order(broker, "cross-sell", side=Side.SELL, price="98")
    crossed_bid = TradeEvent(
        **event_fields("crossed-bid", ASSET, seconds=3),
        price=fp("98"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY,
    )
    with pytest.raises(ValidationError, match="visible bid"):
        model.match(crossed_bid, [sell])


def test_bbo_passive_filters_market_wrong_aggressor_price_and_fok() -> None:
    broker = DeterministicBroker()
    market = order(broker, "market", order_type=OrderType.MARKET, price=None, quantity="1")
    wrong_aggressor = order(broker, "wrong-aggressor", quantity="1", price="99")
    wrong_price = order(broker, "wrong-price", quantity="1", price="98")
    fok = order(broker, "passive-fok", quantity="2", price="99", tif=TimeInForce.FOK)
    model = TradeBBOModel({ASSET: instrument()})
    trade = TradeEvent(
        **event_fields("trade", ASSET, seconds=2),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY,
    )
    assert model.match(trade, [market, wrong_aggressor, wrong_price, fok]) == ()
    assert (
        model.match(StatusEvent(**event_fields("status", ASSET), status="open", reason=""), [])
        == ()
    )


def test_l2_sell_fok_stop_sequence_delete_and_price_specific_queue() -> None:
    model = L2MatchingModel({ASSET: instrument()})
    before = BookDeltaEvent(
        **event_fields("before", ASSET, seconds=1, sequence=2),
        side=BookSide.BID,
        action=BookAction.DELETE,
        price=fp("99"),
        quantity=fp("0"),
        previous_sequence=1,
    )
    with pytest.raises(ValidationError, match="BookSnapshot"):
        model.match(before, [])
    broker = DeterministicBroker()
    sell = order(
        broker,
        "sell-market",
        side=Side.SELL,
        quantity="6",
        order_type=OrderType.MARKET,
        price=None,
    )
    fills = model.match(snapshot(), [sell])
    assert [item.price.to_decimal() for item in fills] == [
        fp("99").to_decimal(),
        fp("98").to_decimal(),
    ]
    broker = DeterministicBroker()
    fok = order(
        broker,
        "l2-fok",
        quantity="6",
        price="101",
        tif=TimeInForce.FOK,
    )
    model.reset()
    assert model.match(snapshot("s2"), [fok]) == ()
    absent = BookDeltaEvent(
        **event_fields("absent", ASSET, seconds=2, sequence=11),
        side=BookSide.ASK,
        action=BookAction.DELETE,
        price=fp("102"),
        quantity=fp("0"),
        previous_sequence=10,
    )
    with pytest.raises(ValidationError, match="absent"):
        model.match(absent, [])


def test_l2_queue_only_moves_for_same_side_and_price_and_fok_stays_atomic() -> None:
    broker = DeterministicBroker()
    passive = order(broker, "queue", quantity="2", price="99")
    model = L2MatchingModel({ASSET: instrument()})
    model.match(snapshot(), [passive])
    unrelated = BookDeltaEvent(
        **event_fields("ask-down", ASSET, seconds=2, sequence=11),
        side=BookSide.ASK,
        action=BookAction.UPSERT,
        price=fp("100"),
        quantity=fp("1"),
        previous_sequence=10,
    )
    model.match(unrelated, [passive])
    trade = TradeEvent(
        **event_fields("small", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("4"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(trade, [passive]) == ()
    broker = DeterministicBroker()
    fok = order(
        broker,
        "queue-fok",
        quantity="5",
        price="97",
        tif=TimeInForce.FOK,
    )
    model.reset()
    model.match(snapshot("fresh"), [fok])
    print_event = TradeEvent(
        **event_fields("print", ASSET, seconds=3),
        price=fp("97"),
        quantity=fp("2"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(print_event, [fok]) == ()


def test_l2_uses_price_then_time_priority_for_visible_liquidity() -> None:
    broker = DeterministicBroker()
    older_worse = order(
        broker,
        "older-worse",
        quantity="2",
        price="100",
        created_seconds=0,
    )
    newer_better = order(
        broker,
        "newer-better",
        quantity="2",
        price="101",
        created_seconds=1,
    )
    one_level = BookSnapshotEvent(
        **event_fields("price-time", ASSET, seconds=2, sequence=1),
        bids=(BookLevel(fp("99"), fp("5")),),
        asks=(BookLevel(fp("100"), fp("2")),),
    )
    fills = L2MatchingModel({ASSET: instrument()}).match(one_level, [older_worse, newer_better])
    assert [fill.order_id for fill in fills] == [newer_better.order_id]


def test_internal_quantity_floor_and_base_reset_are_deterministic() -> None:
    broker = DeterministicBroker()
    resting = order(broker, "rounding", quantity="1")
    assert _quantity_from_available(resting, fp("0.001", 3).to_decimal()).units == 0
    base = _BaseMatchingModel({ASSET: instrument()})
    base.reset()
    assert not base.eligible(
        resting,
        StatusEvent(
            **event_fields("other", "another-asset", seconds=1),
            status="open",
            reason="",
        ),
    )


def test_bbo_nonmarketable_tiny_stop_limit_and_passive_filter_branches() -> None:
    broker = DeterministicBroker()
    nonmarketable = order(broker, "not-marketable", quantity="1", price="100")
    tiny = order(broker, "tiny", quantity="1", price="102")
    stop_waiting = order(
        broker,
        "stop-waiting",
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="110",
    )
    stop_limit = order(
        broker,
        "stop-limit-bbo",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="102",
        stop="100",
    )
    model = TradeBBOModel({ASSET: instrument()})
    tiny_quote = QuoteEvent(
        **event_fields("tiny-quote", ASSET, seconds=1),
        bid_price=fp("99"),
        bid_quantity=fp("0.001", 3),
        ask_price=fp("101"),
        ask_quantity=fp("0.001", 3),
    )
    fills = model.match(tiny_quote, [nonmarketable, tiny, stop_waiting, stop_limit])
    assert fills == ()
    full_quote = quote("full", 2)
    fill = model.match(full_quote, [stop_limit])[0]
    assert fill.order_id == stop_limit.order_id

    broker = DeterministicBroker()
    market = order(broker, "passive-market", order_type=OrderType.MARKET, price=None)
    sell_wrong = order(broker, "sell-wrong", side=Side.SELL, price="100")
    buy_price = order(broker, "buy-price", price="98")
    sell_price = order(broker, "sell-price", side=Side.SELL, price="100")
    stop_passive = order(
        broker,
        "stop-passive",
        order_type=OrderType.STOP,
        price=None,
        stop="99",
    )
    stop_limit_wait = order(
        broker,
        "stop-limit-wait",
        order_type=OrderType.STOP_LIMIT,
        price="99",
        stop="105",
    )
    sell_trade = TradeEvent(
        **event_fields("sell-print", ASSET, seconds=3),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    assert (
        model.match(
            sell_trade,
            [market, sell_wrong, buy_price, sell_price, stop_passive, stop_limit_wait],
        )
        == ()
    )


def test_l2_stop_orders_tiny_levels_and_queue_filters() -> None:
    broker = DeterministicBroker()
    stop_waiting = order(
        broker,
        "l2-stop-wait",
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="110",
    )
    stop_triggered = order(
        broker,
        "l2-stop",
        quantity="1",
        order_type=OrderType.STOP,
        price=None,
        stop="100",
    )
    stop_limit = order(
        broker,
        "l2-stop-limit",
        quantity="1",
        order_type=OrderType.STOP_LIMIT,
        price="101",
        stop="100",
    )
    model = L2MatchingModel({ASSET: instrument()})
    fills = model.match(snapshot(), [stop_waiting, stop_triggered, stop_limit])
    assert stop_waiting.order_id not in {item.order_id for item in fills}
    assert {stop_triggered.order_id, stop_limit.order_id}.issubset(
        {item.order_id for item in fills}
    )
    assert (
        model.match(StatusEvent(**event_fields("l2-status", ASSET), status="open", reason=""), [])
        == ()
    )

    broker = DeterministicBroker()
    tiny = order(broker, "l2-tiny", quantity="1", price="101")
    tiny_book = BookSnapshotEvent(
        **event_fields("tiny-book", ASSET, seconds=1, sequence=1),
        bids=(BookLevel(fp("99"), fp("1")),),
        asks=(BookLevel(fp("100"), fp("0.001", 3)),),
    )
    model.reset()
    assert model.match(tiny_book, [tiny]) == ()

    broker = DeterministicBroker()
    buy = order(broker, "queued-buy", quantity="1", price="99")
    sell = order(broker, "queued-sell", side=Side.SELL, quantity="1", price="101")
    absent = order(broker, "not-queued", quantity="1", price="97")
    model.reset()
    model.match(snapshot("queues"), [buy, sell])
    wrong = TradeEvent(
        **event_fields("wrong-aggressor", ASSET, seconds=2),
        price=fp("99"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.BUY,
    )
    assert model.match(wrong, [buy, sell, absent]) == ()
    wrong_prices = TradeEvent(
        **event_fields("wrong-prices", ASSET, seconds=3),
        price=fp("102"),
        quantity=fp("1"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(wrong_prices, [buy, sell, absent]) == ()
