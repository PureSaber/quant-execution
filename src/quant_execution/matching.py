"""Deterministic bar, Trade/BBO and L2 matching models for simulation only."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import timedelta
from decimal import ROUND_DOWN, Decimal

from quant_data_kit import (
    AggressorSide,
    BarEvent,
    BookAction,
    BookDeltaEvent,
    BookSide,
    BookSnapshotEvent,
    FixedPoint,
    InstrumentSpec,
    MarketEvent,
    QuoteEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution._fixed import decimal, fixed
from quant_execution.broker import remaining_quantity
from quant_execution.contracts import Fill, LiquidityRole, Order, OrderType, Side, TimeInForce


def _fill_id(model: str, event_id: str, order_id: str, index: int, price: FixedPoint) -> str:
    raw = json.dumps(
        [model, event_id, order_id, index, price.units, price.scale],
        separators=(",", ":"),
    ).encode()
    return f"fill-{hashlib.sha256(raw).hexdigest()[:24]}"


def _sort_orders(orders: Sequence[Order]) -> list[Order]:
    return sorted(orders, key=lambda item: (item.intent.created_at, item.order_id))


def _price_time_orders(orders: Sequence[Order]) -> list[Order]:
    """Apply deterministic venue-style price priority, then acceptance time."""

    def priority(order: Order) -> tuple[object, ...]:
        intent = order.intent
        side_rank = 0 if intent.side is Side.BUY else 1
        if intent.order_type in {OrderType.MARKET, OrderType.STOP}:
            price_rank = (0, Decimal(0))
        else:
            limit = decimal(intent.limit_price)
            price_rank = (1, -limit if intent.side is Side.BUY else limit)
        return side_rank, *price_rank, intent.created_at, order.order_id

    return sorted(orders, key=priority)


def _quantity_from_available(order: Order, available: Decimal) -> FixedPoint:
    remaining = decimal(remaining_quantity(order))
    amount = min(remaining, available)
    if amount <= 0:
        return FixedPoint(0, order.intent.quantity.scale)
    return fixed(amount, order.intent.quantity.scale, rounding=ROUND_DOWN)


def _exact_units(value: FixedPoint, scale: int, *, field: str) -> int:
    scaled = decimal(value).scaleb(scale)
    if scaled != scaled.to_integral_value():
        raise ValidationError(f"{field} cannot be represented at L2 book price scale {scale}")
    return int(scaled)


class _BaseMatchingModel:
    sends_live_orders = False

    def __init__(
        self,
        instruments: Mapping[str, InstrumentSpec],
        *,
        latency: timedelta = timedelta(0),
    ) -> None:
        if latency < timedelta(0):
            raise ValidationError("latency cannot be negative")
        self.instruments = dict(instruments)
        self.latency = latency

    def reset(self) -> None:
        pass

    def capture_state(self) -> dict[str, object]:
        return deepcopy(
            {name: value for name, value in self.__dict__.items() if name.startswith("_")}
        )

    def restore_state(self, state: dict[str, object]) -> None:
        for name in tuple(self.__dict__):
            if name.startswith("_"):
                del self.__dict__[name]
        self.__dict__.update(deepcopy(state))

    def eligible(self, order: Order, event: MarketEvent) -> bool:
        return (
            order.intent.instrument_id == event.instrument_id
            and event.available_at >= order.intent.created_at + self.latency
        )

    def _price_tick(self, instrument_id: str) -> Decimal:
        try:
            return decimal(self.instruments[instrument_id].price_tick)
        except KeyError as exc:
            raise ValidationError(f"missing InstrumentSpec for {instrument_id}") from exc

    @staticmethod
    def _fill(
        model: str,
        event: MarketEvent,
        order: Order,
        quantity: FixedPoint,
        price: FixedPoint,
        role: LiquidityRole,
        index: int,
    ) -> Fill:
        return Fill(
            fill_id=_fill_id(model, event.event_id, order.order_id, index, price),
            order_id=order.order_id,
            account_id=order.intent.account_id,
            strategy_id=order.intent.strategy_id,
            instrument_id=order.intent.instrument_id,
            side=order.intent.side,
            quantity=quantity,
            price=price,
            event_time=event.available_at,
            liquidity_role=role,
            venue_trade_id=getattr(event, "event_id", None),
        )


class BarMatchingModel(_BaseMatchingModel):
    """Conservative OHLC matcher.

    Limit fills use the limit price, market/stop fills receive adverse tick slippage,
    and an intrabar stop-limit trigger is not allowed to fill until a later bar unless
    the bar opens through the stop while the opening price is already limit-executable.
    """

    def __init__(
        self,
        instruments: Mapping[str, InstrumentSpec],
        *,
        participation_rate: Decimal | str = Decimal("0.1"),
        slippage_ticks: int = 0,
        latency: timedelta = timedelta(0),
    ) -> None:
        super().__init__(instruments, latency=latency)
        self.participation_rate = Decimal(str(participation_rate))
        if not Decimal(0) < self.participation_rate <= Decimal(1):
            raise ValidationError("participation_rate must be in (0, 1]")
        if isinstance(slippage_ticks, bool) or slippage_ticks < 0:
            raise ValidationError("slippage_ticks must be a non-negative integer")
        self.slippage_ticks = slippage_ticks
        self._activated_stop_limits: set[str] = set()

    def reset(self) -> None:
        self._activated_stop_limits.clear()

    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]:
        if not isinstance(market_event, BarEvent) or not market_event.is_complete:
            return ()
        capacity = decimal(market_event.volume) * self.participation_rate
        fills: list[Fill] = []
        for order in _sort_orders(open_orders):
            if capacity <= 0 or not self.eligible(order, market_event):
                continue
            price = self._execution_price(order, market_event)
            if price is None:
                continue
            remaining = decimal(remaining_quantity(order))
            if order.intent.time_in_force is TimeInForce.FOK and remaining > capacity:
                continue
            quantity = _quantity_from_available(order, capacity)
            if quantity.units <= 0:
                continue
            fills.append(
                self._fill(
                    "bar", market_event, order, quantity, price, LiquidityRole.TAKER, len(fills)
                )
            )
            capacity -= decimal(quantity)
        return tuple(fills)

    def _execution_price(self, order: Order, bar: BarEvent) -> FixedPoint | None:
        intent = order.intent
        tick = self._price_tick(intent.instrument_id) * self.slippage_ticks
        open_price = decimal(bar.open_price)
        high = decimal(bar.high_price)
        low = decimal(bar.low_price)
        direction = Decimal(1) if intent.side is Side.BUY else Decimal(-1)
        if intent.order_type is OrderType.MARKET:
            return fixed(open_price + direction * tick, bar.open_price.scale)
        if intent.order_type is OrderType.LIMIT:
            limit = decimal(intent.limit_price)
            touched = low <= limit if intent.side is Side.BUY else high >= limit
            return intent.limit_price if touched else None
        stop = decimal(intent.stop_price)
        triggered = high >= stop if intent.side is Side.BUY else low <= stop
        if intent.order_type is OrderType.STOP:
            if not triggered:
                return None
            adverse = max(open_price, stop) if intent.side is Side.BUY else min(open_price, stop)
            return fixed(adverse + direction * tick, bar.open_price.scale)
        if order.order_id in self._activated_stop_limits:
            limit = decimal(intent.limit_price)
            touched = low <= limit if intent.side is Side.BUY else high >= limit
            return intent.limit_price if touched else None
        if not triggered:
            return None
        self._activated_stop_limits.add(order.order_id)
        limit = decimal(intent.limit_price)
        open_triggered = open_price >= stop if intent.side is Side.BUY else open_price <= stop
        open_executable = open_price <= limit if intent.side is Side.BUY else open_price >= limit
        return intent.limit_price if open_triggered and open_executable else None


class TradeBBOModel(_BaseMatchingModel):
    """Top-of-book taker and printed-trade maker simulation."""

    def __init__(
        self,
        instruments: Mapping[str, InstrumentSpec],
        *,
        latency: timedelta = timedelta(0),
    ) -> None:
        super().__init__(instruments, latency=latency)
        self._quotes: dict[str, QuoteEvent] = {}
        self._triggered_stops: set[str] = set()

    def reset(self) -> None:
        self._quotes.clear()
        self._triggered_stops.clear()

    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]:
        if isinstance(market_event, QuoteEvent):
            self._quotes[market_event.instrument_id] = market_event
            return self._match_quote(market_event, open_orders)
        if isinstance(market_event, TradeEvent):
            return self._match_trade(market_event, open_orders)
        return ()

    def _match_quote(self, quote: QuoteEvent, orders: Sequence[Order]) -> tuple[Fill, ...]:
        bid_available = decimal(quote.bid_quantity)
        ask_available = decimal(quote.ask_quantity)
        fills: list[Fill] = []
        for order in _price_time_orders(orders):
            if not self.eligible(order, quote):
                continue
            price = quote.ask_price if order.intent.side is Side.BUY else quote.bid_price
            available = ask_available if order.intent.side is Side.BUY else bid_available
            if not self._marketable(order, decimal(price)):
                continue
            remaining = decimal(remaining_quantity(order))
            if order.intent.time_in_force is TimeInForce.FOK and remaining > available:
                continue
            quantity = _quantity_from_available(order, available)
            if quantity.units <= 0:
                continue
            fills.append(
                self._fill("bbo", quote, order, quantity, price, LiquidityRole.TAKER, len(fills))
            )
            if order.intent.side is Side.BUY:
                ask_available -= decimal(quantity)
            else:
                bid_available -= decimal(quantity)
        return tuple(fills)

    def _match_trade(self, trade: TradeEvent, orders: Sequence[Order]) -> tuple[Fill, ...]:
        available = decimal(trade.quantity)
        fills: list[Fill] = []
        quote = self._quotes.get(trade.instrument_id)
        for order in _price_time_orders(orders):
            if available <= 0 or not self.eligible(order, trade):
                continue
            if order.intent.order_type is OrderType.MARKET:
                continue
            if order.intent.side is Side.BUY and trade.aggressor_side is not AggressorSide.SELL:
                continue
            if order.intent.side is Side.SELL and trade.aggressor_side is not AggressorSide.BUY:
                continue
            if not self._passive_executable(order, decimal(trade.price)):
                continue
            if quote is not None:
                if order.intent.side is Side.BUY and decimal(trade.price) > decimal(
                    quote.ask_price
                ):
                    raise ValidationError("trade fill would cross the visible ask")
                if order.intent.side is Side.SELL and decimal(trade.price) < decimal(
                    quote.bid_price
                ):
                    raise ValidationError("trade fill would cross the visible bid")
            remaining = decimal(remaining_quantity(order))
            if order.intent.time_in_force is TimeInForce.FOK and remaining > available:
                continue
            quantity = _quantity_from_available(order, available)
            if quantity.units <= 0:
                continue
            fills.append(
                self._fill(
                    "trade", trade, order, quantity, trade.price, LiquidityRole.MAKER, len(fills)
                )
            )
            available -= decimal(quantity)
        return tuple(fills)

    def _marketable(self, order: Order, visible_price: Decimal) -> bool:
        intent = order.intent
        if intent.order_type is OrderType.MARKET:
            return True
        if intent.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            stop = decimal(intent.stop_price)
            triggered = visible_price >= stop if intent.side is Side.BUY else visible_price <= stop
            if triggered:
                self._triggered_stops.add(order.order_id)
            if order.order_id not in self._triggered_stops:
                return False
            if intent.order_type is OrderType.STOP:
                return True
        limit = decimal(intent.limit_price)
        return visible_price <= limit if intent.side is Side.BUY else visible_price >= limit

    def _passive_executable(self, order: Order, trade_price: Decimal) -> bool:
        intent = order.intent
        if intent.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            stop = decimal(intent.stop_price)
            triggered = trade_price >= stop if intent.side is Side.BUY else trade_price <= stop
            if triggered:
                self._triggered_stops.add(order.order_id)
            if order.order_id not in self._triggered_stops:
                return False
            if intent.order_type is OrderType.STOP:
                return False
        limit = decimal(intent.limit_price)
        return trade_price <= limit if intent.side is Side.BUY else trade_price >= limit


class L2MatchingModel(_BaseMatchingModel):
    """Historical L2 matcher with deterministic price-time and conservative queues.

    Queue-ahead is an explicit research approximation. It is not a claim about a
    venue's nanosecond matching priority or hidden liquidity.
    """

    def __init__(
        self,
        instruments: Mapping[str, InstrumentSpec],
        *,
        latency: timedelta = timedelta(0),
    ) -> None:
        super().__init__(instruments, latency=latency)
        self.reset()

    def reset(self) -> None:
        self._books: dict[str, dict[str, dict[int, FixedPoint] | int]] = {}
        self._queue_ahead: dict[str, Decimal] = {}
        self._queue_keys: dict[str, tuple[Side, int, int]] = {}
        self._triggered_stops: set[str] = set()

    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]:
        active_ids = {order.order_id for order in open_orders}
        self._queue_ahead = {
            order_id: quantity
            for order_id, quantity in self._queue_ahead.items()
            if order_id in active_ids
        }
        self._queue_keys = {
            order_id: key for order_id, key in self._queue_keys.items() if order_id in active_ids
        }
        if isinstance(market_event, BookSnapshotEvent):
            self._apply_snapshot(market_event)
            return self._consume_book(market_event, open_orders)
        if isinstance(market_event, BookDeltaEvent):
            self._apply_delta(market_event)
            return self._consume_book(market_event, open_orders)
        if isinstance(market_event, TradeEvent):
            return self._consume_queues(market_event, open_orders)
        return ()

    def _apply_snapshot(self, event: BookSnapshotEvent) -> None:
        price_scales = {level.price.scale for level in (*event.bids, *event.asks)}
        if len(price_scales) != 1:
            raise ValidationError("L2 snapshot price levels must use one price scale")
        self._books[event.instrument_id] = {
            "bids": {level.price.units: level.quantity for level in event.bids},
            "asks": {level.price.units: level.quantity for level in event.asks},
            "price_scale": event.bids[0].price.scale,
            "sequence": int(event.sequence),
        }
        self._queue_ahead.clear()
        self._queue_keys.clear()

    def _apply_delta(self, event: BookDeltaEvent) -> None:
        try:
            book = self._books[event.instrument_id]
        except KeyError as exc:
            raise ValidationError("L2 matching requires a BookSnapshot before deltas") from exc
        if int(book["sequence"]) != event.previous_sequence:
            raise ValidationError("L2 matching sequence gap")
        if event.price.scale != int(book["price_scale"]):
            raise ValidationError("L2 delta price scale differs from snapshot price scale")
        side_name = "bids" if event.side is BookSide.BID else "asks"
        side = book[side_name]
        assert isinstance(side, dict)
        previous = side.get(event.price.units)
        previous_quantity = decimal(previous) if isinstance(previous, FixedPoint) else Decimal(0)
        if event.action is BookAction.DELETE:
            if event.price.units not in side:
                raise ValidationError("L2 delete references an absent price level")
            del side[event.price.units]
            current_quantity = Decimal(0)
        else:
            side[event.price.units] = event.quantity
            current_quantity = decimal(event.quantity)
        decrease = max(Decimal(0), previous_quantity - current_quantity)
        if decrease:
            expected_side = Side.BUY if event.side is BookSide.BID else Side.SELL
            for order_id in tuple(self._queue_ahead):
                if self._queue_keys.get(order_id) != (
                    expected_side,
                    event.price.units,
                    event.price.scale,
                ):
                    continue
                self._queue_ahead[order_id] = max(
                    Decimal(0), self._queue_ahead[order_id] - decrease
                )
        book["sequence"] = int(event.sequence)

    def _consume_book(
        self, event: BookSnapshotEvent | BookDeltaEvent, orders: Sequence[Order]
    ) -> tuple[Fill, ...]:
        book = self._books[event.instrument_id]
        bids = dict(book["bids"])
        asks = dict(book["asks"])
        scale = int(book["price_scale"])
        fills: list[Fill] = []
        prior_at_price: dict[tuple[Side, int, int], Decimal] = {}
        for order in _price_time_orders(orders):
            if not self.eligible(order, event):
                continue
            opposite = asks if order.intent.side is Side.BUY else bids
            prices = (
                sorted(opposite)
                if order.intent.side is Side.BUY
                else sorted(opposite, reverse=True)
            )
            executable = [price for price in prices if self._book_marketable(order, price, scale)]
            total_visible = sum((decimal(opposite[price]) for price in executable), Decimal(0))
            remaining = decimal(remaining_quantity(order))
            if (
                executable
                and order.intent.time_in_force is TimeInForce.FOK
                and total_visible < remaining
            ):
                continue
            for price_units in executable:
                quantity = _quantity_from_available(order, decimal(opposite[price_units]))
                already = sum(
                    decimal(fill.quantity) for fill in fills if fill.order_id == order.order_id
                )
                still_needed = remaining - already
                if still_needed <= 0:
                    break
                if decimal(quantity) > still_needed:
                    quantity = fixed(still_needed, order.intent.quantity.scale, rounding=ROUND_DOWN)
                if quantity.units <= 0:
                    continue
                price = FixedPoint(price_units, scale)
                fills.append(
                    self._fill(
                        "l2-taker", event, order, quantity, price, LiquidityRole.TAKER, len(fills)
                    )
                )
                opposite[price_units] = fixed(
                    decimal(opposite[price_units]) - decimal(quantity),
                    opposite[price_units].scale,
                )
                if opposite[price_units].units == 0:
                    del opposite[price_units]
            if not executable and order.intent.order_type in {
                OrderType.LIMIT,
                OrderType.STOP_LIMIT,
            }:
                price_units = _exact_units(
                    order.intent.limit_price,
                    scale,
                    field="resting order limit_price",
                )
                same = bids if order.intent.side is Side.BUY else asks
                market_ahead = decimal(same[price_units]) if price_units in same else Decimal(0)
                key = (order.intent.side, price_units, scale)
                self._queue_ahead.setdefault(
                    order.order_id, market_ahead + prior_at_price.get(key, Decimal(0))
                )
                self._queue_keys.setdefault(order.order_id, key)
                prior_at_price[key] = prior_at_price.get(key, Decimal(0)) + remaining
        return tuple(fills)

    def _book_marketable(self, order: Order, price_units: int, scale: int) -> bool:
        price = Decimal(price_units).scaleb(-scale)
        intent = order.intent
        if intent.order_type is OrderType.MARKET:
            return True
        if intent.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            stop = decimal(intent.stop_price)
            triggered = price >= stop if intent.side is Side.BUY else price <= stop
            if triggered:
                self._triggered_stops.add(order.order_id)
            if order.order_id not in self._triggered_stops:
                return False
            if intent.order_type is OrderType.STOP:
                return True
        limit = decimal(intent.limit_price)
        return price <= limit if intent.side is Side.BUY else price >= limit

    def _consume_queues(self, trade: TradeEvent, orders: Sequence[Order]) -> tuple[Fill, ...]:
        book = self._books.get(trade.instrument_id)
        if book is not None and trade.price.scale != int(book["price_scale"]):
            raise ValidationError("L2 trade price scale differs from snapshot price scale")
        available = decimal(trade.quantity)
        fills: list[Fill] = []
        for order in _price_time_orders(orders):
            if available <= 0 or not self.eligible(order, trade):
                continue
            if order.order_id not in self._queue_ahead:
                continue
            if order.intent.side is Side.BUY and trade.aggressor_side is not AggressorSide.SELL:
                continue
            if order.intent.side is Side.SELL and trade.aggressor_side is not AggressorSide.BUY:
                continue
            queue_side, queue_units, queue_scale = self._queue_keys[order.order_id]
            if (
                queue_side is not order.intent.side
                or trade.price.scale != queue_scale
                or trade.price.units != queue_units
            ):
                continue
            ahead = self._queue_ahead[order.order_id]
            consumed_ahead = min(ahead, available)
            ahead -= consumed_ahead
            available -= consumed_ahead
            self._queue_ahead[order.order_id] = ahead
            if available <= 0 or ahead > 0:
                continue
            remaining = decimal(remaining_quantity(order))
            if order.intent.time_in_force is TimeInForce.FOK and remaining > available:
                continue
            quantity = _quantity_from_available(order, available)
            if quantity.units <= 0:
                continue
            fills.append(
                self._fill(
                    "l2-maker",
                    trade,
                    order,
                    quantity,
                    trade.price,
                    LiquidityRole.MAKER,
                    len(fills),
                )
            )
            available -= decimal(quantity)
        return tuple(fills)
