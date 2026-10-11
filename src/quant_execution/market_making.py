"""Inventory-aware cash-equity/ETF quoting on the shared offline replay engine."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR

from quant_data_kit import (
    AssetClass,
    BookDeltaEvent,
    BookSnapshotEvent,
    FixedPoint,
    QuoteEvent,
    StatusEvent,
)
from quant_data_kit.l2_replay import L2BookReconstructor
from quant_data_kit.market_events_v2 import market_event_payload

from ._fixed import decimal, fixed
from .broker import DeterministicBroker
from .contracts import OrderIntent, OrderType, Side, TimeInForce
from .engine import DeterministicRunEngine
from .ledger import ExactAccountLedger
from .matching import L2MatchingModel, TradeBBOModel
from .rules import RuleBookRiskGate


@dataclass(frozen=True)
class QuotePolicy:
    order_quantity: FixedPoint
    max_inventory: FixedPoint
    target_inventory: FixedPoint
    half_spread_ticks: int = 1
    inventory_skew_ticks: int = 4
    max_quote_age_ms: int = 1000
    refresh_ms: int = 100
    cancel_latency_ms: int = 10

    def __post_init__(self):
        for name in ("order_quantity", "max_inventory"):
            if not isinstance(getattr(self, name), FixedPoint) or getattr(self, name).units <= 0:
                raise ValueError(f"positive FixedPoint {name} required")
        if not isinstance(self.target_inventory, FixedPoint):
            raise TypeError("FixedPoint target_inventory required")
        if not 0 <= decimal(self.target_inventory) <= decimal(self.max_inventory):
            raise ValueError("cash inventory target must lie within [0,max_inventory]")
        for name in (
            "half_spread_ticks",
            "inventory_skew_ticks",
            "max_quote_age_ms",
            "refresh_ms",
            "cancel_latency_ms",
        ):
            low = 1 if name in {"half_spread_ticks", "max_quote_age_ms", "refresh_ms"} else 0
            if type(getattr(self, name)) is not int or getattr(self, name) < low:
                raise ValueError(f"invalid integer {name}")


def quote_plan(spec, policy, *, bid, ask, inventory):
    """Passive tick-aligned quotes, long-only with independent worst-case sizes."""
    tick, step = decimal(spec.price_tick), decimal(spec.quantity_step)
    bid, ask, qty = decimal(bid), decimal(ask), decimal(inventory)
    if bid <= 0 or ask <= bid or qty < 0 or qty > decimal(policy.max_inventory):
        raise ValueError("invalid BBO or inventory outside policy bounds")
    for amount in (policy.order_quantity, policy.max_inventory, policy.target_inventory):
        if decimal(amount) % step:
            raise ValueError("inventory policy must align to instrument quantity step")
    center = (bid + ask) / 2 - (
        (qty - decimal(policy.target_inventory))
        / decimal(policy.max_inventory)
        * policy.inventory_skew_ticks
        * tick
    )
    result = []
    for side, price, capacity, rounding in (
        (
            Side.BUY,
            min(bid, center - policy.half_spread_ticks * tick),
            decimal(policy.max_inventory) - qty,
            ROUND_FLOOR,
        ),
        (Side.SELL, max(ask, center + policy.half_spread_ticks * tick), qty, ROUND_CEILING),
    ):
        price = (price / tick).to_integral_value(rounding=rounding) * tick
        size = (min(decimal(policy.order_quantity), capacity) / step).to_integral_value(
            rounding=ROUND_FLOOR
        ) * step
        if size > 0 and price > 0:
            result.append(
                (side, fixed(price, spec.price_tick.scale), fixed(size, spec.quantity_step.scale))
            )
    return result


class InventoryMarketMaker:
    """Callbacks cancel/requote after the current market event has been matched.

    Cancellation latency is rounded UP to the next observed callback; old quotes
    remain executable through that callback's matching phase. No time travel.
    """

    sends_live_orders = False

    def __init__(self, spec, policy, broker, gate, ledger):
        if spec.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}:
            raise ValueError("this policy supports cash equities/ETFs only")
        self.spec, self.policy = spec, policy
        self.broker, self.gate, self.ledger = broker, gate, ledger
        self.reset()

    def reset(self):
        self._state = {
            "book": L2BookReconstructor(),
            "bbo": None,
            "last_submit": None,
            "cancel_due": None,
            "decisions": [],
        }

    def capture_state(self):
        return deepcopy(self._state)

    def restore_state(self, state):
        self._state = deepcopy(state)

    @property
    def decisions(self):
        return deepcopy(self._state["decisions"])

    def on_event(self, context, event):
        if event.instrument_id != self.spec.instrument_id:
            return ()
        at, state, policy = event.available_at, self._state, self.policy
        if isinstance(event, (BookSnapshotEvent, BookDeltaEvent)):
            book = state["book"].apply(market_event_payload(event))
            if book.bids and book.asks:
                bid, ask = book.bids[0], book.asks[0]
                state["bbo"] = (
                    FixedPoint(bid.price_units, bid.price_scale),
                    FixedPoint(ask.price_units, ask.price_scale),
                    event.event_time,
                )
            else:
                state["bbo"] = None
        elif isinstance(event, QuoteEvent):
            state["bbo"] = (event.bid_price, event.ask_price, event.event_time)
        elif isinstance(event, StatusEvent):
            # A fresh book/quote is needed after status changes, including reopens.
            state["bbo"] = None
        live = [
            order
            for order in self.broker.open_orders
            if (
                order.intent.strategy_id == context.strategy_id
                and order.intent.instrument_id == event.instrument_id
            )
        ]
        if state["cancel_due"] is not None:
            if at < state["cancel_due"]:
                return ()
            for order in live:
                self.broker.cancel(
                    order.order_id,
                    idempotency_key=f"mm-cancel:{order.order_id}:{event.event_id}",
                    created_at=at,
                )
                self.gate.release_order(order)
            live = []
            state["cancel_due"] = None
        quote = state["bbo"]
        fresh = quote and timedelta(0) <= at - quote[2] <= timedelta(
            milliseconds=policy.max_quote_age_ms
        )
        inventory = self.ledger.snapshot(at).positions.get(event.instrument_id, FixedPoint(0, 0))
        plan = (
            quote_plan(self.spec, policy, bid=quote[0], ask=quote[1], inventory=inventory)
            if fresh
            else []
        )
        desired = {(s, decimal(p), decimal(q)) for s, p, q in plan}
        outstanding = {
            (
                o.intent.side,
                decimal(o.intent.limit_price),
                decimal(o.intent.quantity) - decimal(o.filled_quantity),
            )
            for o in live
        }
        if live and desired != outstanding:
            state["cancel_due"] = at + timedelta(milliseconds=policy.cancel_latency_ms)
            state["decisions"].append(
                {
                    "at": at.isoformat(),
                    "action": "cancel_requested",
                    "effective_no_earlier_than": state["cancel_due"].isoformat(),
                }
            )
            # Even zero-latency cancels are applied at the next callback.
            return ()
        if (
            live
            or not plan
            or (
                state["last_submit"] is not None
                and at - state["last_submit"] < timedelta(milliseconds=policy.refresh_ms)
            )
        ):
            return ()
        state["last_submit"] = at
        state["decisions"].append(
            {"at": at.isoformat(), "action": "quote", "inventory": str(decimal(inventory))}
        )
        return tuple(
            OrderIntent(
                idempotency_key=f"mm:{event.event_id}:{side.value}",
                account_id=context.account_id,
                strategy_id=context.strategy_id,
                instrument_id=event.instrument_id,
                side=side,
                quantity=qty,
                limit_price=price,
                order_type=OrderType.LIMIT,
                time_in_force=TimeInForce.DAY,
                created_at=at,
            )
            for side, price, qty in plan
        )


def replay_market_making(events, spec, policy, *, initial_cash, matching="l2", latency_ms=1):
    """Return a replay engine and quote audit. Cash/fees/positions use shared ledgers."""
    if matching not in {"l2", "bbo"} or type(latency_ms) is not int or latency_ms < 1:
        raise ValueError("matching must be l2/bbo; positive integer order latency required")
    if not isinstance(initial_cash, FixedPoint) or initial_cash.units <= 0:
        raise ValueError("positive FixedPoint cash required")
    events = tuple(events)
    if not events or any(e.instrument_id != spec.instrument_id for e in events):
        raise ValueError("nonempty single-instrument stream required")
    if matching == "l2" and not any(isinstance(e, BookSnapshotEvent) for e in events):
        raise ValueError("L2 replay needs snapshots; use bbo explicitly for reduced fidelity")
    if matching == "bbo" and any(
        isinstance(e, (BookSnapshotEvent, BookDeltaEvent)) for e in events
    ):
        raise ValueError("BBO replay cannot silently ignore supplied L2 liquidity")
    ledger = ExactAccountLedger(
        account_id="mm-research",
        base_currency=spec.settlement_currency,
        instruments={spec.instrument_id: spec},
        initial_cash={spec.settlement_currency: initial_cash},
    )
    broker = DeterministicBroker()
    gate = RuleBookRiskGate(instruments={spec.instrument_id: spec}, ledger=ledger)
    strategy = InventoryMarketMaker(spec, policy, broker, gate, ledger)
    model = L2MatchingModel if matching == "l2" else TradeBBOModel
    engine = DeterministicRunEngine(
        run_id="inventory-market-making",
        account_id=ledger.account_id,
        strategy_id="inventory-mm",
        strategy=strategy,
        broker=broker,
        risk_gate=gate,
        matching_model=model(
            {spec.instrument_id: spec}, latency=timedelta(milliseconds=latency_ms)
        ),
        ledger=ledger,
    )
    engine.replay(events, seed=0)
    return engine, strategy.decisions
