"""Deterministic bar, Trade/BBO and L2 matching models for simulation only."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from copy import copy, deepcopy
from datetime import datetime, timedelta
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
from quant_execution._json import flat_sequence_bytes
from quant_execution.broker import remaining_quantity
from quant_execution.contracts import Fill, LiquidityRole, Order, OrderType, Side, TimeInForce


def _fill_id(model: str, event_id: str, order_id: str, index: int, price: FixedPoint) -> str:
    raw = flat_sequence_bytes((model, event_id, order_id, index, price.units, price.scale))
    return f"fill-{hashlib.sha256(raw).hexdigest()[:24]}"


def _sort_orders(orders: Sequence[Order]) -> Sequence[Order]:
    if len(orders) < 2:
        return orders
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
    remaining_units = order.intent.quantity.units - order.filled_quantity.units
    if remaining_units == order.intent.quantity.units:
        remaining_fp = order.intent.quantity
    else:
        remaining_fp = FixedPoint(remaining_units, order.intent.quantity.scale)
    remaining = decimal(remaining_fp)
    if available >= remaining:
        return remaining_fp
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

    @staticmethod
    def checkpoint_required(open_orders: Sequence[Order]) -> bool:
        return any(order.intent.order_type is OrderType.STOP_LIMIT for order in open_orders)

    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]:
        if not isinstance(market_event, BarEvent) or not market_event.is_complete:
            return ()
        if not open_orders:
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
        # Vendor facts remain authoritative; only the overlay records simulated taker use.
        self._books: dict[str, dict[str, dict[int, FixedPoint] | int]] = {}
        self._liquidity_books: dict[str, dict[str, dict[int, FixedPoint] | int]] = {}
        self._queue_ahead: dict[str, Decimal] = {}
        self._queue_keys: dict[str, tuple[str, Side, int, int]] = {}
        self._queue_priority: dict[str, tuple[datetime, datetime, str]] = {}
        self._queue_remaining: dict[str, Decimal] = {}
        self._triggered_stops: set[str] = set()
        self._stop_triggered_at: dict[str, datetime] = {}

    def match(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> Sequence[Fill]:
        staged = self._stage_match(market_event.instrument_id)
        fills = staged._match_staged(market_event, open_orders)
        self._commit_match(staged)
        return fills

    def _stage_match(self, instrument_id: str) -> L2MatchingModel:
        """Create an isolated, shallow transaction state for one L2 event.

        Orders, instruments and FixedPoint values are immutable. Only the current
        instrument's nested book sides and the mutable queue/stop containers need
        copying. The live model is therefore untouched until ``_commit_match`` and
        any validation failure naturally discards this staged object.
        """

        staged = copy(self)
        staged._books = self._stage_book_registry(self._books, instrument_id)
        staged._liquidity_books = self._stage_book_registry(
            self._liquidity_books,
            instrument_id,
        )
        staged._queue_ahead = dict(self._queue_ahead)
        staged._queue_keys = dict(self._queue_keys)
        staged._queue_priority = dict(self._queue_priority)
        staged._queue_remaining = dict(self._queue_remaining)
        staged._triggered_stops = set(self._triggered_stops)
        staged._stop_triggered_at = dict(self._stop_triggered_at)
        return staged

    @staticmethod
    def _stage_book_registry(
        registry: dict[str, dict[str, dict[int, FixedPoint] | int]],
        instrument_id: str,
    ) -> dict[str, dict[str, dict[int, FixedPoint] | int]]:
        staged_registry = dict(registry)
        book = registry.get(instrument_id)
        if book is None:
            return staged_registry
        staged_book = dict(book)
        bids = book["bids"]
        asks = book["asks"]
        assert isinstance(bids, dict) and isinstance(asks, dict)
        staged_book["bids"] = dict(bids)
        staged_book["asks"] = dict(asks)
        staged_registry[instrument_id] = staged_book
        return staged_registry

    def _commit_match(self, staged: L2MatchingModel) -> None:
        self._books = staged._books
        self._liquidity_books = staged._liquidity_books
        self._queue_ahead = staged._queue_ahead
        self._queue_keys = staged._queue_keys
        self._queue_priority = staged._queue_priority
        self._queue_remaining = staged._queue_remaining
        self._triggered_stops = staged._triggered_stops
        self._stop_triggered_at = staged._stop_triggered_at

    def _match_staged(
        self,
        market_event: MarketEvent,
        open_orders: Sequence[Order],
    ) -> Sequence[Fill]:
        if isinstance(market_event, BookSnapshotEvent):
            staged_book = self._stage_snapshot(market_event)
            self._apply_snapshot(market_event, staged_book)
            self._reconcile_queues(market_event, open_orders)
            self._trigger_stops_from_book(market_event, open_orders)
            return self._consume_book(market_event, open_orders)
        if isinstance(market_event, BookDeltaEvent):
            staged_delta = self._stage_delta(market_event)
            self._apply_delta(market_event, staged_delta)
            self._reconcile_queues(market_event, open_orders)
            self._trigger_stops_from_book(market_event, open_orders)
            return self._consume_book(market_event, open_orders)
        if isinstance(market_event, TradeEvent):
            self._validate_trade(market_event)
            self._reconcile_queues(market_event, open_orders)
            taker_fills = self._consume_saved_book(market_event, open_orders)
            self._trigger_stops_from_trade(market_event, open_orders)
            maker_fills = self._consume_queues(market_event, open_orders)
            return (*taker_fills, *maker_fills)
        self._reconcile_queues(market_event, open_orders)
        return ()

    def _reconcile_queues(self, market_event: MarketEvent, open_orders: Sequence[Order]) -> None:
        active = {order.order_id: order for order in open_orders}
        removed = set(self._queue_keys).difference(active)
        for order_id in sorted(removed, key=self._queue_priority.__getitem__):
            key = self._queue_keys[order_id]
            priority = self._queue_priority[order_id]
            released = self._queue_remaining[order_id]
            if released:
                for later_id, later_key in self._queue_keys.items():
                    if (
                        later_id not in removed
                        and later_key == key
                        and self._queue_priority[later_id] > priority
                    ):
                        self._queue_ahead[later_id] = max(
                            Decimal(0), self._queue_ahead[later_id] - released
                        )
            self._drop_queue(order_id)

        for order_id, order in active.items():
            if order_id in self._queue_remaining:
                self._reduce_queue_remaining(
                    order_id,
                    decimal(remaining_quantity(order)),
                    release_later=True,
                )

        for order in self._price_time_orders(open_orders):
            if order.order_id in self._queue_keys or not self.eligible(order, market_event):
                continue
            book = self._liquidity_books.get(order.intent.instrument_id)
            if book is not None:
                self._queue_passive_order(order, book)

    def _drop_queue(self, order_id: str) -> None:
        self._queue_ahead.pop(order_id, None)
        self._queue_keys.pop(order_id, None)
        self._queue_priority.pop(order_id, None)
        self._queue_remaining.pop(order_id, None)

    def _clear_instrument_queues(self, instrument_id: str) -> None:
        for order_id, key in tuple(self._queue_keys.items()):
            if key[0] == instrument_id:
                self._drop_queue(order_id)

    def _reduce_queue_remaining(
        self,
        order_id: str,
        remaining: Decimal,
        *,
        release_later: bool,
    ) -> None:
        previous = self._queue_remaining[order_id]
        current = min(previous, remaining)
        released = previous - current
        self._queue_remaining[order_id] = current
        if not release_later or released <= 0:
            return
        key = self._queue_keys[order_id]
        priority = self._queue_priority[order_id]
        for later_id, later_key in self._queue_keys.items():
            if later_key == key and self._queue_priority[later_id] > priority:
                self._queue_ahead[later_id] = max(
                    Decimal(0), self._queue_ahead[later_id] - released
                )

    def _price_time_orders(self, orders: Sequence[Order]) -> list[Order]:
        """Order active L2 instructions by price and venue-entry time."""

        def priority(order: Order) -> tuple[object, ...]:
            intent = order.intent
            side_rank = 0 if intent.side is Side.BUY else 1
            if intent.order_type in {OrderType.MARKET, OrderType.STOP}:
                price_rank = (0, Decimal(0))
            else:
                limit = decimal(intent.limit_price)
                price_rank = (1, -limit if intent.side is Side.BUY else limit)
            active_at = self._stop_triggered_at.get(order.order_id, intent.created_at)
            return side_rank, *price_rank, active_at, intent.created_at, order.order_id

        return sorted(orders, key=priority)

    def _queue_passive_order(
        self,
        order: Order,
        book: dict[str, dict[int, FixedPoint] | int],
        *,
        remaining: Decimal | None = None,
    ) -> None:
        if order.intent.order_type not in {OrderType.LIMIT, OrderType.STOP_LIMIT}:
            return
        if order.intent.time_in_force in {TimeInForce.IOC, TimeInForce.FOK}:
            return
        if (
            order.intent.order_type is OrderType.STOP_LIMIT
            and order.order_id not in self._triggered_stops
        ):
            return
        queue_remaining = decimal(remaining_quantity(order)) if remaining is None else remaining
        if queue_remaining <= 0:
            return
        scale = int(book["price_scale"])
        bids = book["bids"]
        asks = book["asks"]
        assert isinstance(bids, dict) and isinstance(asks, dict)
        opposite = asks if order.intent.side is Side.BUY else bids
        if any(self._book_marketable(order, price, scale) for price in opposite):
            return
        price_units = _exact_units(
            order.intent.limit_price,
            scale,
            field="resting order limit_price",
        )
        same = bids if order.intent.side is Side.BUY else asks
        market_ahead = decimal(same[price_units]) if price_units in same else Decimal(0)
        key = (order.intent.instrument_id, order.intent.side, price_units, scale)
        effective_time = self._stop_triggered_at.get(order.order_id, order.intent.created_at)
        priority = (effective_time, order.intent.created_at, order.order_id)
        earlier_own = sum(
            (
                remaining
                for order_id, remaining in self._queue_remaining.items()
                if self._queue_keys[order_id] == key and self._queue_priority[order_id] < priority
            ),
            Decimal(0),
        )
        self._queue_ahead[order.order_id] = market_ahead + earlier_own
        self._queue_keys[order.order_id] = key
        self._queue_priority[order.order_id] = priority
        self._queue_remaining[order.order_id] = queue_remaining

    def _trigger_stops_from_book(
        self,
        event: BookSnapshotEvent | BookDeltaEvent,
        orders: Sequence[Order],
    ) -> None:
        book = self._books[event.instrument_id]
        bids = book["bids"]
        asks = book["asks"]
        assert isinstance(bids, dict) and isinstance(asks, dict)
        scale = int(book["price_scale"])
        best_bid = Decimal(max(bids)).scaleb(-scale) if bids else None
        best_ask = Decimal(min(asks)).scaleb(-scale) if asks else None
        for order in _sort_orders(orders):
            if not self.eligible(order, event) or order.order_id in self._triggered_stops:
                continue
            if order.intent.order_type not in {OrderType.STOP, OrderType.STOP_LIMIT}:
                continue
            visible = best_ask if order.intent.side is Side.BUY else best_bid
            if visible is not None and self._stop_reached(order, visible):
                self._activate_stop(order, event.available_at)

    def _trigger_stops_from_trade(
        self,
        event: TradeEvent,
        orders: Sequence[Order],
    ) -> None:
        visible = decimal(event.price)
        for order in _sort_orders(orders):
            if not self.eligible(order, event) or order.order_id in self._triggered_stops:
                continue
            if order.intent.order_type not in {OrderType.STOP, OrderType.STOP_LIMIT}:
                continue
            if self._stop_reached(order, visible):
                self._activate_stop(order, event.available_at)

    @staticmethod
    def _stop_reached(order: Order, visible_price: Decimal) -> bool:
        stop = decimal(order.intent.stop_price)
        return visible_price >= stop if order.intent.side is Side.BUY else visible_price <= stop

    def _activate_stop(self, order: Order, event_time: datetime) -> None:
        self._triggered_stops.add(order.order_id)
        self._stop_triggered_at[order.order_id] = event_time

    def _stage_snapshot(self, event: BookSnapshotEvent) -> dict[str, dict[int, FixedPoint] | int]:
        if not event.bids or not event.asks:
            raise ValidationError("L2 snapshot must contain bids and asks")
        price_scales = {level.price.scale for level in (*event.bids, *event.asks)}
        if len(price_scales) != 1:
            raise ValidationError("L2 snapshot price levels must use one price scale")
        return {
            "bids": {level.price.units: level.quantity for level in event.bids},
            "asks": {level.price.units: level.quantity for level in event.asks},
            "price_scale": event.bids[0].price.scale,
            "sequence": int(event.sequence),
        }

    def _apply_snapshot(
        self,
        event: BookSnapshotEvent,
        book: dict[str, dict[int, FixedPoint] | int],
    ) -> None:
        self._books[event.instrument_id] = book
        bids = book["bids"]
        asks = book["asks"]
        assert isinstance(bids, dict) and isinstance(asks, dict)
        self._liquidity_books[event.instrument_id] = {
            "bids": dict(bids),
            "asks": dict(asks),
            "price_scale": int(book["price_scale"]),
            "sequence": int(book["sequence"]),
        }
        self._clear_instrument_queues(event.instrument_id)

    def _stage_delta(
        self, event: BookDeltaEvent
    ) -> tuple[dict[int, FixedPoint], dict[int, FixedPoint], FixedPoint | None, Decimal]:
        try:
            book = self._books[event.instrument_id]
            liquidity_book = self._liquidity_books[event.instrument_id]
        except KeyError as exc:
            raise ValidationError("L2 matching requires a BookSnapshot before deltas") from exc
        if int(book["sequence"]) != event.previous_sequence:
            raise ValidationError("L2 matching sequence gap")
        if event.price.scale != int(book["price_scale"]):
            raise ValidationError("L2 delta price scale differs from snapshot price scale")
        side_name = "bids" if event.side is BookSide.BID else "asks"
        side = book[side_name]
        liquidity_side = liquidity_book[side_name]
        assert isinstance(side, dict) and isinstance(liquidity_side, dict)
        if event.action is BookAction.DELETE and event.price.units not in side:
            raise ValidationError("L2 delete references an absent price level")
        previous = side.get(event.price.units)
        previous_liquidity = liquidity_side.get(event.price.units)
        previous_quantity = decimal(previous) if isinstance(previous, FixedPoint) else Decimal(0)
        previous_liquidity_quantity = (
            decimal(previous_liquidity)
            if isinstance(previous_liquidity, FixedPoint)
            else Decimal(0)
        )
        consumed_gap = max(Decimal(0), previous_quantity - previous_liquidity_quantity)
        if event.action is BookAction.DELETE:
            current_quantity = Decimal(0)
        else:
            current_quantity = decimal(event.quantity)
        current_liquidity = max(Decimal(0), current_quantity - consumed_gap)
        staged_liquidity: FixedPoint | None = None
        if current_liquidity != 0:
            quantity_scale = max(
                event.quantity.scale,
                previous.scale if isinstance(previous, FixedPoint) else 0,
                previous_liquidity.scale if isinstance(previous_liquidity, FixedPoint) else 0,
            )
            staged_liquidity = fixed(
                current_liquidity,
                quantity_scale,
                rounding=None,
            )
        decrease = max(Decimal(0), previous_quantity - current_quantity)
        return side, liquidity_side, staged_liquidity, decrease

    def _apply_delta(
        self,
        event: BookDeltaEvent,
        staged: tuple[
            dict[int, FixedPoint],
            dict[int, FixedPoint],
            FixedPoint | None,
            Decimal,
        ],
    ) -> None:
        side, liquidity_side, staged_liquidity, decrease = staged
        book = self._books[event.instrument_id]
        liquidity_book = self._liquidity_books[event.instrument_id]
        if event.action is BookAction.DELETE:
            del side[event.price.units]
        else:
            side[event.price.units] = event.quantity
        if staged_liquidity is None:
            liquidity_side.pop(event.price.units, None)
        else:
            liquidity_side[event.price.units] = staged_liquidity
        if decrease:
            expected_side = Side.BUY if event.side is BookSide.BID else Side.SELL
            self._advance_queues(
                event.instrument_id,
                expected_side,
                event.price.units,
                event.price.scale,
                decrease,
            )
        book["sequence"] = int(event.sequence)
        liquidity_book["sequence"] = int(event.sequence)

    def _validate_trade(self, event: TradeEvent) -> None:
        book = self._books.get(event.instrument_id)
        if book is not None and event.price.scale != int(book["price_scale"]):
            raise ValidationError("L2 trade price scale differs from snapshot price scale")

    def _advance_queues(
        self,
        instrument_id: str,
        side: Side,
        price_units: int,
        price_scale: int,
        quantity: Decimal,
    ) -> None:
        key = (instrument_id, side, price_units, price_scale)
        for order_id in tuple(self._queue_ahead):
            if self._queue_keys.get(order_id) == key:
                self._queue_ahead[order_id] = max(
                    Decimal(0), self._queue_ahead[order_id] - quantity
                )

    def _consume_book(
        self, event: BookSnapshotEvent | BookDeltaEvent, orders: Sequence[Order]
    ) -> tuple[Fill, ...]:
        return self._consume_visible_book(event, orders, skip_queued=False)

    def _consume_saved_book(self, event: TradeEvent, orders: Sequence[Order]) -> tuple[Fill, ...]:
        """Execute pre-existing takers against the last causally visible book.

        A Trade does not update the authoritative L2 book. Orders that were already
        resting in a simulated maker queue therefore remain maker candidates, while
        active orders that could not enter that queue get one taker attempt against a
        persistent consumable view of the latest book. This runs before stops are
        triggered by the current Trade, so a newly triggered stop cannot fill on its
        trigger event.
        """

        if event.instrument_id not in self._liquidity_books:
            return ()
        return self._consume_visible_book(event, orders, skip_queued=True)

    def _consume_visible_book(
        self,
        event: BookSnapshotEvent | BookDeltaEvent | TradeEvent,
        orders: Sequence[Order],
        *,
        skip_queued: bool,
    ) -> tuple[Fill, ...]:
        book = self._liquidity_books[event.instrument_id]
        bids = dict(book["bids"])
        asks = dict(book["asks"])
        scale = int(book["price_scale"])
        local_book: dict[str, dict[int, FixedPoint] | int] = {
            "bids": bids,
            "asks": asks,
            "price_scale": scale,
            "sequence": int(book["sequence"]),
        }
        fills: list[Fill] = []
        for order in self._price_time_orders(orders):
            if not self.eligible(order, event):
                continue
            if skip_queued and order.order_id in self._queue_keys:
                continue
            if (
                order.intent.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}
                and order.order_id not in self._triggered_stops
            ):
                continue
            opposite = asks if order.intent.side is Side.BUY else bids
            prices = (
                sorted(opposite)
                if order.intent.side is Side.BUY
                else sorted(opposite, reverse=True)
            )
            executable = [price for price in prices if self._book_marketable(order, price, scale)]
            remaining = decimal(remaining_quantity(order))
            plan, planned = self._build_execution_plan(
                order,
                opposite,
                executable,
                remaining,
            )
            if order.intent.time_in_force is TimeInForce.FOK and planned != remaining:
                continue
            filled = Decimal(0)
            for price_units, quantity, remaining_level in plan:
                price = FixedPoint(price_units, scale)
                fills.append(
                    self._fill(
                        "l2-taker", event, order, quantity, price, LiquidityRole.TAKER, len(fills)
                    )
                )
                filled += decimal(quantity)
                resting_side = Side.SELL if order.intent.side is Side.BUY else Side.BUY
                self._advance_queues(
                    event.instrument_id,
                    resting_side,
                    price_units,
                    scale,
                    decimal(quantity),
                )
                if remaining_level is None:
                    del opposite[price_units]
                else:
                    opposite[price_units] = remaining_level
            remaining_after = remaining - filled
            if order.order_id in self._queue_remaining:
                self._reduce_queue_remaining(
                    order.order_id,
                    remaining_after,
                    release_later=True,
                )
            if order.order_id not in self._queue_keys:
                self._queue_passive_order(
                    order,
                    local_book,
                    remaining=remaining_after,
                )
        book["bids"] = bids
        book["asks"] = asks
        return tuple(fills)

    @staticmethod
    def _build_execution_plan(
        order: Order,
        opposite: Mapping[int, FixedPoint],
        executable: Sequence[int],
        remaining: Decimal,
    ) -> tuple[tuple[tuple[int, FixedPoint, FixedPoint | None], ...], Decimal]:
        """Stage the exact per-level FixedPoint fills before mutating matching state."""

        plan: list[tuple[int, FixedPoint, FixedPoint | None]] = []
        planned = Decimal(0)
        for price_units in executable:
            still_needed = remaining - planned
            if still_needed <= 0:
                break
            level = opposite[price_units]
            amount = min(still_needed, decimal(level))
            quantity = fixed(amount, order.intent.quantity.scale, rounding=ROUND_DOWN)
            if quantity.units <= 0:
                continue
            level_after = decimal(level) - decimal(quantity)
            remaining_level = (
                None
                if level_after == 0
                else fixed(
                    level_after,
                    max(level.scale, quantity.scale),
                    rounding=None,
                )
            )
            plan.append((price_units, quantity, remaining_level))
            planned += decimal(quantity)
        return tuple(plan), planned

    def _book_marketable(self, order: Order, price_units: int, scale: int) -> bool:
        price = Decimal(price_units).scaleb(-scale)
        intent = order.intent
        if intent.order_type is OrderType.MARKET:
            return True
        if intent.order_type in {OrderType.STOP, OrderType.STOP_LIMIT}:
            if order.order_id not in self._triggered_stops:
                return False
            if intent.order_type is OrderType.STOP:
                return True
        limit = decimal(intent.limit_price)
        return price <= limit if intent.side is Side.BUY else price >= limit

    def _consume_queues(self, trade: TradeEvent, orders: Sequence[Order]) -> tuple[Fill, ...]:
        # Every resting order stores its absolute queue position at this price level:
        # visible market volume plus earlier simulated orders. A trade consumes the
        # level once. Applying the same shrinking ``available`` value to every order
        # would charge the first order's queue-ahead again to later orders.
        traded_at_level = decimal(trade.quantity)
        fills: list[Fill] = []
        for order in self._price_time_orders(orders):
            if traded_at_level <= 0 or not self.eligible(order, trade):
                continue
            if order.order_id not in self._queue_ahead:
                continue
            if order.intent.side is Side.BUY and trade.aggressor_side is not AggressorSide.SELL:
                continue
            if order.intent.side is Side.SELL and trade.aggressor_side is not AggressorSide.BUY:
                continue
            queue_instrument, queue_side, queue_units, queue_scale = self._queue_keys[
                order.order_id
            ]
            if (
                queue_instrument != trade.instrument_id
                or queue_side is not order.intent.side
                or trade.price.scale != queue_scale
                or trade.price.units != queue_units
            ):
                continue
            ahead = self._queue_ahead[order.order_id]
            executable = max(Decimal(0), traded_at_level - ahead)
            self._queue_ahead[order.order_id] = max(Decimal(0), ahead - traded_at_level)
            if executable <= 0:
                continue
            remaining = min(
                decimal(remaining_quantity(order)), self._queue_remaining[order.order_id]
            )
            if order.intent.time_in_force is TimeInForce.FOK and remaining > executable:
                continue
            quantity = _quantity_from_available(order, min(remaining, executable))
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
            self._queue_remaining[order.order_id] = remaining - decimal(quantity)
        return tuple(fills)
