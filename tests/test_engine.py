from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import (
    AggressorSide,
    BarEvent,
    BookLevel,
    BookSnapshotEvent,
    FundingRateEvent,
    MarketEvent,
    StatusEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError
from test_rules import FUTURE, PERP, SPOT, STOCK, specs

from quant_execution import OrderIntent, OrderType, Side, TimeInForce
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import DeterministicRunEngine, ReplayError
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import BarMatchingModel, L2MatchingModel
from quant_execution.rules import RuleBookRiskGate


@dataclass
class Signal:
    instrument_id: str
    side: Side
    quantity: object
    price: object
    reduce_only: bool = False
    tif: TimeInForce = TimeInForce.GTC


class FixtureStrategy:
    def __init__(self, signals: dict[str, list[Signal]], *, fail_on: str | None = None) -> None:
        self.signals = signals
        self.fail_on = fail_on
        self.calls = 0

    def reset(self) -> None:
        self.calls = 0

    def on_event(self, context, event: MarketEvent):
        self.calls += 1
        if event.event_id == self.fail_on:
            raise RuntimeError("intentional strategy failure")
        return [
            OrderIntent(
                idempotency_key=f"{context.run_id}:{event.event_id}:{index}",
                account_id=context.account_id,
                strategy_id=context.strategy_id,
                instrument_id=signal.instrument_id,
                side=signal.side,
                quantity=signal.quantity,
                order_type=OrderType.LIMIT,
                time_in_force=signal.tif,
                created_at=event.available_at,
                limit_price=signal.price,
                reduce_only=signal.reduce_only,
            )
            for index, signal in enumerate(self.signals.get(event.event_id, []))
        ]


def bar(
    event_id: str,
    instrument_id: str,
    seconds: int,
    price: str,
    *,
    volume: str = "1000",
) -> BarEvent:
    return BarEvent(
        **event_fields(event_id, instrument_id, seconds=seconds),
        bar_start=T0 + timedelta(seconds=seconds - 60),
        bar_end=T0 + timedelta(seconds=seconds),
        open_price=fp(price),
        high_price=fp(str(float(price) + 1)),
        low_price=fp(str(float(price) - 1)),
        close_price=fp(price),
        volume=fp(volume, 3 if "." in volume else 0),
        is_complete=True,
    )


def engine_for(
    *,
    run_id: str,
    registry,
    initial_cash,
    base_currency: str,
    strategy: FixtureStrategy,
) -> DeterministicRunEngine:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency=base_currency,
        instruments=registry,
        initial_cash=initial_cash,
    )
    return DeterministicRunEngine(
        run_id=run_id,
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=BarMatchingModel(registry, participation_rate="1"),
        ledger=ledger,
    )


def scenario_a_share():
    registry = {STOCK: specs()[STOCK]}
    strategy = FixtureStrategy({"a-signal": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    engine = engine_for(
        run_id="golden-a-share",
        registry=registry,
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=strategy,
    )
    return engine, [bar("a-fill", STOCK, 120, "10"), bar("a-signal", STOCK, 60, "10")]


def scenario_future():
    registry = {FUTURE: specs()[FUTURE]}
    strategy = FixtureStrategy({"f-signal": [Signal(FUTURE, Side.BUY, fp("1"), fp("4000"))]})
    engine = engine_for(
        run_id="golden-future",
        registry=registry,
        initial_cash={"CNY": fp("1000000")},
        base_currency="CNY",
        strategy=strategy,
    )
    return engine, [
        bar("f-fill", FUTURE, 120, "4000"),
        bar("f-signal", FUTURE, 60, "4000"),
    ]


def scenario_crypto():
    all_specs = specs()
    registry = {SPOT: all_specs[SPOT], PERP: all_specs[PERP]}
    strategy = FixtureStrategy(
        {
            "s-signal": [Signal(SPOT, Side.BUY, fp("1.000", 3), fp("100"))],
            "p-signal": [Signal(PERP, Side.BUY, fp("2.000", 3), fp("100"))],
        }
    )
    engine = engine_for(
        run_id="golden-crypto",
        registry=registry,
        initial_cash={"USDT": fp("100000")},
        base_currency="USDT",
        strategy=strategy,
    )
    events = [
        bar("s-signal", SPOT, 60, "100", volume="10.000"),
        bar("s-fill", SPOT, 120, "100", volume="10.000"),
        bar("p-signal", PERP, 180, "100", volume="10.000"),
        bar("p-fill", PERP, 240, "100", volume="10.000"),
        FundingRateEvent(
            **event_fields("p-funding", PERP, seconds=300),
            rate=0.001,
            interval_start=T0,
            interval_end=T0 + timedelta(hours=8),
        ),
    ]
    return engine, events


def result_payload(engine: DeterministicRunEngine, events) -> dict[str, object]:
    result = engine.replay(events, 42)
    assert engine.artifacts is not None
    return {
        "event_count": result.event_count,
        "order_count": result.order_count,
        "fill_count": result.fill_count,
        "order_sha256": result.order_sha256,
        "fill_sha256": result.fill_sha256,
        "ledger_sha256": result.ledger_sha256,
        "result_sha256": result.result_sha256,
        "nav": engine.ledger.snapshot().nav.to_decimal().to_eng_string(),
    }


def test_same_engine_three_consecutive_replays_are_bitwise_deterministic() -> None:
    engine, events = scenario_crypto()
    results = [result_payload(engine, events) for _ in range(3)]
    assert results[0] == results[1] == results[2]


def test_engine_queues_snapshot_callback_order_before_next_trade_without_lookahead() -> None:
    registry = {SPOT: specs()[SPOT]}
    observed_open_orders: list[tuple[str, int]] = []

    class RecordingL2Model(L2MatchingModel):
        def match(self, market_event, open_orders):
            observed_open_orders.append((market_event.event_id, len(open_orders)))
            return super().match(market_event, open_orders)

    strategy = FixtureStrategy({"l2-book": [Signal(SPOT, Side.BUY, fp("1.000", 3), fp("99"))]})
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100000")},
    )
    engine = DeterministicRunEngine(
        run_id="l2-causal-ordering",
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=RecordingL2Model(registry),
        ledger=ledger,
    )
    book = BookSnapshotEvent(
        **event_fields("l2-book", SPOT, seconds=60, sequence=1),
        bids=(BookLevel(fp("99"), fp("5.000", 3)),),
        asks=(BookLevel(fp("100"), fp("5.000", 3)),),
    )
    trade = TradeEvent(
        **event_fields("l2-trade", SPOT, seconds=120, sequence=2),
        price=fp("99"),
        quantity=fp("6.000", 3),
        aggressor_side=AggressorSide.SELL,
    )
    result = engine.replay([book, trade], 7)
    assert observed_open_orders == [("l2-book", 0), ("l2-trade", 1)]
    assert result.fill_count == 1
    assert engine.artifacts is not None
    assert engine.artifacts.fills[0].quantity == fp("1.000", 3)
    assert engine.artifacts.fills[0].event_time == trade.available_at


def test_three_cross_asset_golden_replays() -> None:
    expected = json.loads(
        (Path(__file__).parent / "golden" / "m3" / "replays.json").read_text(encoding="utf-8")
    )
    actual = {}
    for name, factory in {
        "a_share": scenario_a_share,
        "future": scenario_future,
        "crypto_spot_perpetual": scenario_crypto,
    }.items():
        engine, events = factory()
        actual[name] = result_payload(engine, events)
    assert actual == expected


def test_engine_uses_run_start_for_opening_and_emits_daily_futures_settlement() -> None:
    registry = {FUTURE: specs()[FUTURE]}
    strategy = FixtureStrategy({"settle-signal": [Signal(FUTURE, Side.BUY, fp("1"), fp("4000"))]})
    engine = engine_for(
        run_id="daily-futures-settlement",
        registry=registry,
        initial_cash={"CNY": fp("1000000")},
        base_currency="CNY",
        strategy=strategy,
    )
    events = [
        bar("settle-signal", FUTURE, 60, "4000"),
        bar("settle-fill", FUTURE, 120, "4000"),
        bar("settle-mark", FUTURE, 180, "4010"),
        StatusEvent(
            **event_fields("settle-close", FUTURE, seconds=240),
            status="daily_settlement",
            reason="fixture trading-day close",
        ),
    ]

    engine.replay(events, 19)

    assert engine.artifacts is not None
    assert len(engine.artifacts.settlements) == 1
    settlement = engine.artifacts.settlements[0]
    assert settlement.settlement_type == "daily_mark"
    assert settlement.event_time == events[-1].available_at
    assert settlement.amount.to_decimal() == 3000
    assert (
        min(item.event_time for item in engine.artifacts.ledger_transactions)
        == events[0].available_at
    )
    assert any(
        item.event_type.value == "settlement" for item in engine.artifacts.ledger_transactions
    )


def test_engine_fail_closed_on_strategy_error_duplicate_event_and_future_intent() -> None:
    registry = {STOCK: specs()[STOCK]}
    failing = FixtureStrategy({}, fail_on="boom")
    engine = engine_for(
        run_id="fail",
        registry=registry,
        initial_cash={"CNY": fp("1000")},
        base_currency="CNY",
        strategy=failing,
    )
    event = bar("boom", STOCK, 60, "10")
    with pytest.raises(ReplayError, match="failed closed"):
        engine.replay([event], 1)
    assert not engine.broker.sends_live_orders
    with pytest.raises(ValidationError, match="duplicate"):
        engine.replay([event, event], 1)


def test_rejected_intent_creates_reasoned_order_event_without_position_mutation() -> None:
    registry = {STOCK: specs()[STOCK]}
    strategy = FixtureStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    engine = engine_for(
        run_id="reject",
        registry=registry,
        initial_cash={"CNY": fp("10")},
        base_currency="CNY",
        strategy=strategy,
    )
    result = engine.replay([bar("signal", STOCK, 60, "10")], 7)
    assert result.order_count == 1 and result.fill_count == 0
    assert engine.artifacts is not None
    assert engine.artifacts.order_events[-1].reason.startswith("INSUFFICIENT_CASH")
    assert engine.ledger.snapshot().positions == {}
