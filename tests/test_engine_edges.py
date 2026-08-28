from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import (
    AggressorSide,
    BookLevel,
    BookSnapshotEvent,
    CorporateActionEvent,
    FixedPoint,
    FundingRateEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError
from test_engine import FixtureStrategy, Signal, bar, engine_for
from test_rules import PERP, SPOT, STOCK, specs

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
from quant_execution.matching import BarMatchingModel, L2MatchingModel
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
            account_id="other" if self.mode == "account" else "account",
            strategy_id="other" if self.mode == "strategy" else "strategy",
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
    for mode, message in (
        ("unknown", "unknown order"),
        ("duplicate", "duplicate fill_id"),
        ("account", "fill account differs"),
        ("strategy", "fill strategy differs"),
    ):
        engine = manual_engine(strategy, InvalidFillModel(mode))
        with pytest.raises(ReplayError, match=message):
            engine.replay(events, 1)
        assert engine.broker.orders == ()
        assert len(engine.ledger.transactions) == 1


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


class MarketOrderStrategy:
    def reset(self):
        pass

    def on_event(self, context, event):
        if event.event_id != "risk-signal":
            return ()
        return (
            OrderIntent(
                idempotency_key="risk-fill-market",
                account_id=context.account_id,
                strategy_id=context.strategy_id,
                instrument_id=SPOT,
                side=Side.BUY,
                quantity=fp("1.000", 3),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                created_at=event.available_at,
            ),
        )


def test_fill_risk_rejection_restores_l2_overlay_and_queue_state() -> None:
    registry = {SPOT: specs()[SPOT]}
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100.05")},
    )
    matcher = L2MatchingModel(registry)
    engine = DeterministicRunEngine(
        run_id="risk-fill-overlay",
        account_id="account",
        strategy_id="strategy",
        strategy=MarketOrderStrategy(),
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=matcher,
        ledger=ledger,
    )
    book = BookSnapshotEvent(
        **event_fields("risk-book", SPOT, seconds=1, sequence=50),
        bids=(BookLevel(fp("99"), fp("1.000", 3)),),
        asks=(BookLevel(fp("200"), fp("1.000", 3)),),
    )
    signal = TradeEvent(
        **event_fields("risk-signal", SPOT, seconds=2),
        price=fp("100"),
        quantity=fp("1.000", 3),
        aggressor_side=AggressorSide.SELL,
    )
    attempt = TradeEvent(
        **event_fields("risk-attempt", SPOT, seconds=3),
        price=fp("100"),
        quantity=fp("1.000", 3),
        aggressor_side=AggressorSide.SELL,
    )

    result = engine.replay((book, signal, attempt), seed=7)

    assert result.fill_count == 0
    assert engine.artifacts is not None
    assert len(engine.artifacts.risk_events) == 1
    assert "INSUFFICIENT_CASH_AT_FILL" in engine.artifacts.risk_events[0]
    asks = matcher._liquidity_books[SPOT]["asks"]
    assert isinstance(asks, dict)
    assert asks[fp("200").units] == fp("1.000", 3)
    assert matcher._queue_keys == {}
    assert matcher._queue_ahead == {}
    assert matcher._queue_remaining == {}


class TransactionalBatchMatcher:
    def __init__(
        self,
        prices_by_index: dict[int, tuple[str, ...]],
        *,
        capacity: int | None = None,
    ) -> None:
        self.prices_by_index = prices_by_index
        self.capacity = capacity
        self.reset()

    def reset(self):
        self.match_attempts = 0
        self.committed_order_keys: tuple[str, ...] = ()

    def capture_state(self):
        return self.match_attempts, self.committed_order_keys

    def restore_state(self, state):
        self.match_attempts, self.committed_order_keys = state

    def match(self, event, open_orders):
        if event.event_id != "match" or not open_orders:
            return ()
        ordered = tuple(
            sorted(
                open_orders,
                key=lambda order: int(order.intent.idempotency_key.rsplit(":", 1)[1]),
            )
        )
        selected = ordered[: self.capacity] if self.capacity else ordered
        self.match_attempts += 1
        self.committed_order_keys = tuple(order.intent.idempotency_key for order in selected)
        fills = []
        for order in selected:
            index = int(order.intent.idempotency_key.rsplit(":", 1)[1])
            prices = self.prices_by_index[index]
            quantity = (
                order.intent.quantity
                if len(prices) == 1
                else FixedPoint(
                    order.intent.quantity.units // len(prices), order.intent.quantity.scale
                )
            )
            for level, price in enumerate(prices):
                fills.append(
                    Fill(
                        fill_id=f"txn-fill-{order.intent.idempotency_key}-{level}",
                        order_id=order.order_id,
                        account_id=order.intent.account_id,
                        strategy_id=order.intent.strategy_id,
                        instrument_id=order.intent.instrument_id,
                        side=order.intent.side,
                        quantity=quantity,
                        price=fp(price),
                        event_time=event.available_at,
                        liquidity_role=LiquidityRole.TAKER,
                    )
                )
        return tuple(fills)


class StatefulRejectingGate(RuleBookRiskGate):
    def reset(self):
        super().reset()
        self.committed_fill_checks: tuple[str, ...] = ()

    def capture_state(self):
        return super().capture_state(), self.committed_fill_checks

    def restore_state(self, state):
        base, self.committed_fill_checks = state
        super().restore_state(base)

    def check_fill(self, fill, order):
        self.committed_fill_checks += (order.intent.idempotency_key,)
        return super().check_fill(fill, order)


def transactional_engine(
    *,
    run_id: str,
    strategy: FixtureStrategy,
    matcher,
    cash: str = "250",
    gate_type=RuleBookRiskGate,
    broker_type=DeterministicBroker,
    ledger_type=ExactAccountLedger,
):
    registry = {SPOT: specs()[SPOT]}
    ledger = ledger_type(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp(cash)},
    )
    broker = broker_type()
    gate = gate_type(instruments=registry, ledger=ledger)
    return DeterministicRunEngine(
        run_id=run_id,
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=broker,
        risk_gate=gate,
        matching_model=matcher,
        ledger=ledger,
    )


def spot_signal(quantity: str = "1.000") -> Signal:
    return Signal(SPOT, Side.BUY, fp(quantity, 3), fp("100"))


def test_rejected_first_order_replays_released_liquidity_to_later_legal_order() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("200",), 1: ("100",)}, capacity=1)
    engine = transactional_engine(run_id="released", strategy=strategy, matcher=matcher)
    events = [
        bar("signal", SPOT, 60, "100", volume="10.000"),
        bar("match", SPOT, 120, "100", volume="10.000"),
    ]

    result = engine.replay(events, 1)

    assert result.fill_count == 1
    assert engine.artifacts is not None
    high, legal = sorted(engine.artifacts.orders, key=lambda order: order.intent.idempotency_key)
    assert high.status is OrderStatus.EXPIRED
    assert legal.status is OrderStatus.FILLED
    assert engine.artifacts.fills[0].order_id == legal.order_id
    assert matcher.match_attempts == 1
    assert matcher.committed_order_keys == (legal.intent.idempotency_key,)
    assert len(engine.artifacts.risk_events) == 1


def test_mixed_fill_batch_rolls_back_before_replay_without_duplicate_facts() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("100",), 1: ("200",)})
    engine = transactional_engine(run_id="mixed", strategy=strategy, matcher=matcher)

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        2,
    )

    assert result.fill_count == 1
    assert engine.artifacts is not None
    accepted = next(
        order for order in engine.artifacts.orders if order.status is OrderStatus.FILLED
    )
    rejected = next(
        order for order in engine.artifacts.orders if order.status is OrderStatus.EXPIRED
    )
    assert engine.artifacts.fills[0].order_id == accepted.order_id
    assert len(engine.artifacts.fees) == 1
    assert len({fill.fill_id for fill in engine.artifacts.fills}) == 1
    assert sum(event.order_id == accepted.order_id for event in engine.artifacts.order_events) == 2
    assert sum(event.order_id == rejected.order_id for event in engine.artifacts.order_events) == 2
    references = [transaction.reference_id for transaction in engine.artifacts.ledger_transactions]
    assert references.count(engine.artifacts.fills[0].fill_id) == 1
    assert references.count(engine.artifacts.fees[0].fee_id) == 1
    assert len(engine.artifacts.risk_events) == 1


def test_later_rejection_expires_whole_multilevel_order_without_partial_commit() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("100", "2000", "100")})
    engine = transactional_engine(
        run_id="multilevel-atomic", strategy=strategy, matcher=matcher, cash="150"
    )

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        3,
    )

    assert result.fill_count == 0
    assert engine.artifacts is not None
    assert engine.artifacts.orders[0].status is OrderStatus.EXPIRED
    assert engine.artifacts.fills == ()
    assert engine.artifacts.fees == ()
    assert engine.ledger.snapshot().positions == {}
    assert len(engine.artifacts.ledger_transactions) == 1
    assert len(engine.artifacts.risk_events) == 1


def test_all_accepted_multi_fill_batch_commits_once() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("100",), 1: ("101",)})
    engine = transactional_engine(
        run_id="all-accepted", strategy=strategy, matcher=matcher, cash="500"
    )

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        4,
    )

    assert result.fill_count == 2
    assert engine.artifacts is not None
    assert len(engine.artifacts.fees) == 2
    assert len({fill.fill_id for fill in engine.artifacts.fills}) == 2
    assert all(order.status is OrderStatus.FILLED for order in engine.artifacts.orders)


def test_all_rejected_orders_are_expired_once_and_retry_terminates() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("200",), 1: ("200",)})
    engine = transactional_engine(run_id="all-rejected", strategy=strategy, matcher=matcher)

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        4,
    )

    assert result.fill_count == 0
    assert engine.artifacts is not None
    assert all(order.status is OrderStatus.EXPIRED for order in engine.artifacts.orders)
    assert len(engine.artifacts.risk_events) == 2
    assert len(set(engine.artifacts.risk_events)) == 2
    assert matcher.match_attempts == 0


class CaptureCountingBroker(DeterministicBroker):
    def __init__(self):
        super().__init__()
        self.capture_calls = 0

    def capture_state(self):
        self.capture_calls += 1
        return super().capture_state()


class CaptureCountingLedger(ExactAccountLedger):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.capture_calls = 0

    def capture_state(self):
        self.capture_calls += 1
        return super().capture_state()


class CaptureCountingL2Matcher(L2MatchingModel):
    def __init__(self, instruments):
        super().__init__(instruments)
        self.capture_calls = 0

    def capture_state(self):
        self.capture_calls += 1
        return super().capture_state()


class FillWithoutOpenOrderMatcher:
    def __init__(self):
        self.capture_calls = 0
        self.match_calls = 0
        self.restore_calls = 0
        self.state: tuple[str, ...] = ()

    def reset(self):
        self.state = ()

    def capture_state(self):
        self.capture_calls += 1
        return self.state

    def restore_state(self, state):
        self.restore_calls += 1
        self.state = state

    def match(self, event, open_orders):
        self.match_calls += 1
        assert open_orders == ()
        self.state += (event.event_id,)
        return (
            Fill(
                fill_id="fill-without-open-order",
                order_id="missing-order",
                account_id="account",
                strategy_id="strategy",
                instrument_id=STOCK,
                side=Side.BUY,
                quantity=fp("100"),
                price=fp("10"),
                event_time=event.available_at,
                liquidity_role=LiquidityRole.TAKER,
            ),
        )


class CaptureTrackingBatchMatcher(TransactionalBatchMatcher):
    def __init__(self, prices_by_index):
        super().__init__(prices_by_index)
        self.capture_calls = 0
        self.restore_calls = 0

    def capture_state(self):
        self.capture_calls += 1
        return super().capture_state()

    def restore_state(self, state):
        self.restore_calls += 1
        super().restore_state(state)


class NoFeeGate(RuleBookRiskGate):
    def fee_for(self, fill, order):
        del fill, order


def test_no_open_orders_deliver_l2_event_without_matcher_checkpoint() -> None:
    registry = {SPOT: specs()[SPOT]}
    matcher = CaptureCountingL2Matcher(registry)
    engine = transactional_engine(
        run_id="no-open-orders-l2",
        strategy=FixtureStrategy({}),
        matcher=matcher,
    )
    snapshot = BookSnapshotEvent(
        **event_fields("no-open-orders-book", SPOT, seconds=60, sequence=1),
        bids=(BookLevel(fp("99"), fp("1.000", 3)),),
        asks=(BookLevel(fp("100"), fp("1.000", 3)),),
    )

    result = engine.replay((snapshot,), 5)

    assert result.fill_count == 0
    assert matcher.capture_calls == 1  # Whole-run fail-closed checkpoint only.
    assert matcher._books[SPOT]["sequence"] == 1
    assert matcher._liquidity_books[SPOT]["asks"] == {fp("100").units: fp("1.000", 3)}


def test_fill_without_open_orders_fails_closed_without_matcher_checkpoint() -> None:
    matcher = FillWithoutOpenOrderMatcher()
    engine = manual_engine(FixtureStrategy({}), matcher)

    with pytest.raises(ReplayError, match="not open"):
        engine.replay((bar("illegal-fill", STOCK, 60, "10"),), 5)

    assert matcher.capture_calls == 1
    assert matcher.match_calls == 1
    assert matcher.restore_calls == 1
    assert matcher.state == ()
    assert engine.broker.orders == ()
    assert len(engine.ledger.transactions) == 1


def test_open_order_fill_rejection_captures_and_restores_matcher() -> None:
    matcher = CaptureTrackingBatchMatcher({0: ("200",)})
    engine = transactional_engine(
        run_id="capture-on-fill-rejection",
        strategy=FixtureStrategy({"signal": [spot_signal()]}),
        matcher=matcher,
        cash="150",
    )

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        5,
    )

    assert result.fill_count == 0
    assert matcher.capture_calls == 2  # Whole-run checkpoint plus the ordered match attempt.
    assert matcher.restore_calls == 1
    assert matcher.match_attempts == 0
    assert engine.broker.orders[0].status is OrderStatus.EXPIRED


@pytest.mark.parametrize("with_fill", [False, True])
def test_no_fill_and_single_accepted_fill_avoid_component_history_checkpoints(
    with_fill: bool,
) -> None:
    strategy = FixtureStrategy({"signal": [spot_signal()]}) if with_fill else FixtureStrategy({})
    matcher = TransactionalBatchMatcher({0: ("100",)})
    engine = transactional_engine(
        run_id=f"fast-{with_fill}",
        strategy=strategy,
        matcher=matcher,
        broker_type=CaptureCountingBroker,
        ledger_type=CaptureCountingLedger,
    )
    events = [bar("signal", SPOT, 60, "100", volume="10.000")]
    if with_fill:
        events.append(bar("match", SPOT, 120, "100", volume="10.000"))

    result = engine.replay(events, 5)

    assert result.fill_count == int(with_fill)
    assert engine.broker.capture_calls == 1
    assert engine.ledger.capture_calls == 1


def test_single_accepted_fill_without_fee_commits_on_fast_path() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal()]})
    engine = transactional_engine(
        run_id="fast-no-fee",
        strategy=strategy,
        matcher=TransactionalBatchMatcher({0: ("100",)}),
        gate_type=NoFeeGate,
    )

    result = engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="10.000"),
            bar("match", SPOT, 120, "100", volume="10.000"),
        ],
        5,
    )

    assert result.fill_count == 1
    assert engine.artifacts is not None and engine.artifacts.fees == ()


def test_stateful_matcher_and_risk_gate_restore_deterministically() -> None:
    strategy = FixtureStrategy({"signal": [spot_signal(), spot_signal()]})
    matcher = TransactionalBatchMatcher({0: ("100",), 1: ("200",)})
    engine = transactional_engine(
        run_id="stateful",
        strategy=strategy,
        matcher=matcher,
        gate_type=StatefulRejectingGate,
    )
    events = [
        bar("signal", SPOT, 60, "100", volume="10.000"),
        bar("match", SPOT, 120, "100", volume="10.000"),
    ]
    outcomes = []
    for _ in range(3):
        result = engine.replay(events, 6)
        assert engine.artifacts is not None
        outcomes.append(
            (
                result.event_sha256,
                result.fill_sha256,
                result.ledger_sha256,
                engine.artifacts.risk_events,
                matcher.match_attempts,
                engine.risk_gate.committed_fill_checks,
            )
        )
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert len(engine.risk_gate.committed_fill_checks) == 1
