from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import CorporateActionEvent, FundingRateEvent
from quant_data_kit.exceptions import ValidationError
from test_engine import FixtureStrategy, Signal, bar, engine_for
from test_rules import PERP, STOCK, specs

from quant_execution import (
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderStatus,
    OrderType,
    RiskDecision,
    Side,
    TimeInForce,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import DeterministicRunEngine, ReplayError
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import BarMatchingModel
from quant_execution.rules import RuleBookRiskGate


def manual_engine(strategy, matching, gate_type=RuleBookRiskGate):
    registry = {STOCK: specs()[STOCK]}
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    gate = gate_type(instruments=registry, ledger=ledger)
    return DeterministicRunEngine(
        run_id="edges",
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=gate,
        matching_model=matching,
        ledger=ledger,
    )


def test_engine_constructor_seed_and_event_validation() -> None:
    registry = {STOCK: specs()[STOCK]}
    strategy = FixtureStrategy({})
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("1")},
    )
    with pytest.raises(ValidationError, match="required"):
        DeterministicRunEngine(
            run_id="",
            account_id="account",
            strategy_id="strategy",
            strategy=strategy,
            broker=DeterministicBroker(),
            risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
            matching_model=BarMatchingModel(registry),
            ledger=ledger,
        )
    with pytest.raises(ValidationError, match="differs"):
        DeterministicRunEngine(
            run_id="run",
            account_id="other",
            strategy_id="strategy",
            strategy=strategy,
            broker=DeterministicBroker(),
            risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
            matching_model=BarMatchingModel(registry),
            ledger=ledger,
        )
    engine = engine_for(
        run_id="validation",
        registry=registry,
        initial_cash={"CNY": fp("100")},
        base_currency="CNY",
        strategy=strategy,
    )
    for seed in (-1, True, 1.5):
        with pytest.raises(ValidationError, match="seed"):
            engine.replay([], seed)
    with pytest.raises(ValidationError, match="non-MarketEvent"):
        engine.replay([object()], 1)


class BadStrategy:
    def __init__(self, value):
        self.value = value

    def on_event(self, context, event):
        del context, event
        return self.value


def test_strategy_output_identity_and_future_time_are_fail_closed() -> None:
    event = bar("event", STOCK, 60, "10")
    registry = {STOCK: specs()[STOCK]}
    invalid_values = ["not-a-sequence", [object()]]
    for value in invalid_values:
        engine = engine_for(
            run_id="bad-output",
            registry=registry,
            initial_cash={"CNY": fp("1000")},
            base_currency="CNY",
            strategy=BadStrategy(value),
        )
        with pytest.raises(ReplayError):
            engine.replay([event], 1)
    wrong = OrderIntent(
        idempotency_key="wrong",
        account_id="other",
        strategy_id="strategy",
        instrument_id=STOCK,
        side=Side.BUY,
        quantity=fp("100"),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=event.available_at,
        limit_price=fp("10"),
    )
    future = replace(
        wrong, account_id="account", created_at=event.available_at + timedelta(seconds=1)
    )
    for bad_intent in (wrong, future):
        engine = engine_for(
            run_id="bad-intent",
            registry=registry,
            initial_cash={"CNY": fp("1000")},
            base_currency="CNY",
            strategy=BadStrategy([bad_intent]),
        )
        with pytest.raises(ReplayError):
            engine.replay([event], 1)


class NoLifecycleModel:
    def match(self, market_event, open_orders):
        del market_event, open_orders
        return ()


class NoResetStrategy:
    def on_event(self, context, event):
        del context, event
        return ()


def test_optional_reset_and_eligibility_methods_are_not_required() -> None:
    engine = manual_engine(NoResetStrategy(), NoLifecycleModel())
    result = engine.replay([bar("event", STOCK, 60, "10")], 1)
    assert result.event_count == 1


class InvalidFillModel:
    def __init__(self, mode: str):
        self.mode = mode

    def reset(self):
        pass

    def eligible(self, order, event):
        del order, event
        return True

    def match(self, event, open_orders):
        if not open_orders:
            return ()
        order = open_orders[0]
        fill = Fill(
            fill_id="duplicate",
            order_id="unknown" if self.mode == "unknown" else order.order_id,
            account_id="account",
            strategy_id="strategy",
            instrument_id=STOCK,
            side=Side.BUY,
            quantity=fp("50"),
            price=fp("10"),
            event_time=event.available_at,
            liquidity_role=LiquidityRole.TAKER,
        )
        return (fill, fill) if self.mode == "duplicate" else (fill,)


def test_unknown_and_duplicate_fills_fail_closed() -> None:
    strategy = FixtureStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    events = [bar("signal", STOCK, 60, "10"), bar("match", STOCK, 120, "10")]
    for mode, message in (("unknown", "unknown order"), ("duplicate", "duplicate fill_id")):
        engine = manual_engine(strategy, InvalidFillModel(mode))
        with pytest.raises(ReplayError, match=message):
            engine.replay(events, 1)


def test_ioc_fok_expiry_and_latency_noneligibility() -> None:
    registry = {STOCK: specs()[STOCK]}
    for tif in (TimeInForce.IOC, TimeInForce.FOK):
        strategy = FixtureStrategy(
            {"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"), tif=tif)]}
        )
        engine = engine_for(
            run_id=f"expire-{tif.value}",
            registry=registry,
            initial_cash={"CNY": fp("100000")},
            base_currency="CNY",
            strategy=strategy,
        )
        engine.replay([bar("signal", STOCK, 60, "10"), bar("next", STOCK, 120, "10")], 1)
        assert engine.broker.orders[0].status is OrderStatus.EXPIRED
    strategy = FixtureStrategy(
        {"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"), tif=TimeInForce.IOC)]}
    )
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    delayed = DeterministicRunEngine(
        run_id="latency",
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=BarMatchingModel(registry, latency=timedelta(hours=1)),
        ledger=ledger,
    )
    delayed.replay([bar("signal", STOCK, 60, "10"), bar("too-soon", STOCK, 120, "10")], 1)
    assert delayed.broker.orders[0].status is OrderStatus.ACCEPTED


class LiquidatingGate(RuleBookRiskGate):
    def reset(self):
        super().reset()
        self.count = 0

    def observe(self, event):
        super().observe(event)
        self.count += 1

    def runtime_check(self, snapshot):
        if self.count >= 2:
            return RiskDecision(False, "LIQUIDATION_REQUIRED", "fixture boundary")
        return super().runtime_check(snapshot)


def test_runtime_liquidation_expires_open_orders_and_records_risk() -> None:
    registry = {STOCK: specs()[STOCK]}
    strategy = FixtureStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("8.5"))]})
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
    )
    engine = DeterministicRunEngine(
        run_id="liquidation",
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=LiquidatingGate(instruments=registry, ledger=ledger),
        matching_model=BarMatchingModel(registry),
        ledger=ledger,
    )
    engine.replay([bar("signal", STOCK, 60, "10"), bar("boundary", STOCK, 120, "10")], 1)
    assert engine.broker.orders[0].status is OrderStatus.EXPIRED
    assert (
        engine.artifacts is not None and "LIQUIDATION_REQUIRED" in engine.artifacts.risk_events[0]
    )


def test_engine_applies_corporate_action_and_no_position_funding_without_fee() -> None:
    registry = {STOCK: specs()[STOCK]}
    strategy = FixtureStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100"), fp("10"))]})
    engine = engine_for(
        run_id="corporate",
        registry=registry,
        initial_cash={"CNY": fp("100000")},
        base_currency="CNY",
        strategy=strategy,
    )
    action = CorporateActionEvent(
        **event_fields("action", STOCK, seconds=180),
        action_type="split",
        effective_date=T0.date(),
        ratio=fp("2"),
    )
    engine.replay([bar("signal", STOCK, 60, "10"), bar("fill", STOCK, 120, "10"), action], 1)
    assert engine.ledger.snapshot().positions[STOCK].to_decimal() == fp("200").to_decimal()

    perp_registry = {PERP: specs()[PERP]}
    no_position = engine_for(
        run_id="no-funding",
        registry=perp_registry,
        initial_cash={"USDT": fp("1000")},
        base_currency="USDT",
        strategy=FixtureStrategy({}),
    )
    funding = FundingRateEvent(
        **event_fields("funding", PERP, seconds=60),
        rate=0.001,
        interval_start=T0,
        interval_end=T0 + timedelta(hours=8),
    )
    no_position.replay([funding], 1)
    assert len(no_position.ledger.transactions) == 1
