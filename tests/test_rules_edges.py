from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from conftest import T0, event_fields, fp, spec
from quant_data_kit import AssetClass, BarEvent, QuoteEvent, StatusEvent
from quant_data_kit.exceptions import ValidationError
from test_rules import FUTURE, PERP, SPOT, STOCK, intent, specs, state_event

from quant_execution import (
    AccountSnapshot,
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.ledger import ExactAccountLedger
from quant_execution.rules import MarketState, RuleBookRiskGate, _AssetRule, _metadata_decimal


def make_gate(registry=None, cash="100000"):
    registry = registry or specs()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp(cash), "USDT": fp(cash)},
        fx_to_base={"USDT": fp("7")},
    )
    return RuleBookRiskGate(instruments=registry, ledger=ledger), ledger


def market_intent(instrument_id: str, *, key: str, side: Side = Side.BUY) -> OrderIntent:
    return OrderIntent(
        idempotency_key=key,
        account_id="account",
        strategy_id="strategy",
        instrument_id=instrument_id,
        side=side,
        quantity=fp("100") if instrument_id == STOCK else fp("1.000", 3),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.IOC,
        created_at=T0,
    )


def test_metadata_and_generic_rule_fail_closed_branches() -> None:
    stock = specs()[STOCK]
    missing = replace(stock, metadata={})
    with pytest.raises(ValidationError, match="required"):
        _metadata_decimal(missing, "lot_size", required=True)
    with pytest.raises(ValidationError, match="decimal"):
        _metadata_decimal(replace(stock, metadata={"x": "bad"}), "x")
    with pytest.raises(ValidationError, match="finite"):
        _metadata_decimal(replace(stock, metadata={"x": "NaN"}), "x")
    gate, ledger = make_gate({STOCK: stock})
    status = StatusEvent(**event_fields("open-no-price", STOCK), status="open", reason="")
    gate.observe(status)
    decision = gate.check(market_intent(STOCK, key="no-price"), ledger.snapshot())
    assert decision.code == "NO_REFERENCE_PRICE"
    gate.observe(state_event(STOCK, "10"))
    assert (
        gate.check(intent(STOCK, Side.BUY, "100", key="upper", price="13"), ledger.snapshot()).code
        == "PRICE_ABOVE_LIMIT"
    )
    assert (
        gate.check(intent(STOCK, Side.BUY, "100", key="lower", price="7"), ledger.snapshot()).code
        == "PRICE_BELOW_LIMIT"
    )
    direct_spot = RuleBookRiskGate._rule(specs()[SPOT])
    no_reference = MarketState(status, None, "open")
    assert (
        direct_spot.check(
            market_intent(SPOT, key="spot-no-reference"),
            ledger.snapshot(),
            no_reference,
            specs()[SPOT],
            ledger,
        ).code
        == "NO_REFERENCE_PRICE"
    )


def test_gate_identity_pit_lifecycle_steps_status_and_configuration() -> None:
    registry = specs()
    gate, ledger = make_gate(registry)
    unknown = intent(STOCK, Side.BUY, "100", key="unknown")
    object.__setattr__(unknown, "instrument_id", "unknown")
    assert gate.check(unknown, ledger.snapshot()).code == "UNKNOWN_INSTRUMENT"
    no_state = intent(STOCK, Side.BUY, "100", key="no-state")
    assert gate.check(no_state, ledger.snapshot()).code == "NO_MARKET_STATE"
    gate.observe(state_event(STOCK, "10"))
    wrong_account = AccountSnapshot(account_id="other", event_time=T0, base_currency="CNY")
    assert gate.check(no_state, wrong_account).code == "ACCOUNT_MISMATCH"

    future_available = replace(registry[STOCK], available_at=T0 + timedelta(seconds=1))
    pit_gate, pit_ledger = make_gate({STOCK: future_available})
    pit_gate.observe(state_event(STOCK, "10"))
    assert pit_gate.check(no_state, pit_ledger.snapshot()).code == "PIT_INSTRUMENT"
    inactive = replace(registry[STOCK], effective_from=T0 + timedelta(seconds=1))
    inactive_gate, inactive_ledger = make_gate({STOCK: inactive})
    inactive_gate.observe(state_event(STOCK, "10"))
    assert inactive_gate.check(no_state, inactive_ledger.snapshot()).code == "INSTRUMENT_INACTIVE"

    limit_up = StatusEvent(**event_fields("up", STOCK), status="limit_up", reason="")
    gate.observe(limit_up)
    assert gate.check(no_state, ledger.snapshot()).code == "LIMIT_UP"
    limit_down = StatusEvent(**event_fields("down", STOCK), status="limit_down", reason="")
    gate.observe(limit_down)
    sell = intent(STOCK, Side.SELL, "100", key="down")
    assert gate.check(sell, ledger.snapshot()).code == "LIMIT_DOWN"

    gate.observe(state_event(SPOT, "100"))
    bad_step = intent(SPOT, Side.BUY, "0.001", key="step", price="100")
    object.__setattr__(bad_step, "quantity", fp("0.0015", 4))
    assert gate.check(bad_step, ledger.snapshot()).code == "QUANTITY_STEP"
    bad_tick = intent(SPOT, Side.BUY, "0.010", key="tick", price="100")
    object.__setattr__(bad_tick, "limit_price", fp("100.005", 3))
    assert gate.check(bad_tick, ledger.snapshot()).code == "PRICE_TICK"
    misconfigured = replace(registry[STOCK], metadata={})
    config_gate, config_ledger = make_gate({STOCK: misconfigured})
    config_gate.observe(state_event(STOCK, "10"))
    assert config_gate.check(no_state, config_ledger.snapshot()).code == "RULE_CONFIGURATION"


def test_spot_sell_reduce_only_success_and_perpetual_minimum() -> None:
    registry = specs()
    gate, ledger = make_gate(registry)
    spot_event = state_event(SPOT, "100")
    gate.observe(spot_event)
    ledger.observe_market(spot_event)
    sell = intent(SPOT, Side.SELL, "0.010", key="sell", price="100")
    assert gate.check(sell, ledger.snapshot()).code == "INSUFFICIENT_POSITION"
    reduce_only = intent(SPOT, Side.SELL, "0.010", key="spot-reduce", price="100", reduce_only=True)
    assert gate.check(reduce_only, ledger.snapshot()).code == "SPOT_REDUCE_ONLY"
    ledger.apply(
        Fill(
            fill_id="spot-position",
            order_id="spot-order",
            account_id="account",
            strategy_id="strategy",
            instrument_id=SPOT,
            side=Side.BUY,
            quantity=fp("1.000", 3),
            price=fp("100"),
            event_time=T0,
        )
    )
    assert gate.check(sell, ledger.snapshot()).accepted
    gate.observe(state_event(PERP, "100"))
    too_small = intent(PERP, Side.BUY, "0.001", key="perp-min", price="100")
    object.__setattr__(too_small, "quantity", fp("0.0005", 4))
    assert gate.check(too_small, ledger.snapshot()).code == "QUANTITY_STEP"
    direct_rule = RuleBookRiskGate._rule(registry[PERP])
    state = gate._states[PERP]
    assert (
        direct_rule.check(too_small, ledger.snapshot(), state, registry[PERP], ledger).code
        == "MIN_QUANTITY"
    )


def test_observe_quote_bar_stop_price_zero_fee_and_unsupported_rule() -> None:
    registry = specs()
    zero_fee = replace(
        registry[SPOT],
        metadata={"min_quantity": "0.001", "maker_fee_rate": "0", "taker_fee_rate": "0"},
    )
    gate, ledger = make_gate({SPOT: zero_fee})
    quote = QuoteEvent(
        **event_fields("quote", SPOT),
        bid_price=fp("99"),
        bid_quantity=fp("1"),
        ask_price=fp("101"),
        ask_quantity=fp("1"),
    )
    gate.observe(quote)
    bar = BarEvent(
        **event_fields("bar", SPOT, seconds=60),
        bar_start=T0,
        bar_end=T0 + timedelta(seconds=60),
        open_price=fp("100"),
        high_price=fp("101"),
        low_price=fp("99"),
        close_price=fp("100"),
        volume=fp("1"),
        is_complete=True,
    )
    gate.observe(bar)
    stop = OrderIntent(
        idempotency_key="stop",
        account_id="account",
        strategy_id="strategy",
        instrument_id=SPOT,
        side=Side.BUY,
        quantity=fp("0.010", 3),
        order_type=OrderType.STOP,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        stop_price=fp("101"),
    )
    assert gate.check(stop, ledger.snapshot()).accepted
    broker = DeterministicBroker()
    accepted = broker.submit(stop)
    no_fee = gate.fee_for(
        Fill(
            fill_id="free",
            order_id=accepted.order_id,
            account_id="account",
            strategy_id="strategy",
            instrument_id=SPOT,
            side=Side.BUY,
            quantity=fp("0.010", 3),
            price=fp("100"),
            event_time=T0,
            liquidity_role=LiquidityRole.MAKER,
        ),
        accepted,
    )
    assert no_fee is None
    unsupported = spec(
        "bond:test",
        asset_class=AssetClass.BOND,
        product_type="bond",
        settlement_currency="CNY",
    )
    with pytest.raises(ValidationError, match="unsupported"):
        RuleBookRiskGate._rule(unsupported)
    no_reference_state = MarketState(
        StatusEvent(**event_fields("x", SPOT), status="open", reason=""), None, "open"
    )
    assert (
        _AssetRule()
        .check(
            market_intent(SPOT, key="direct"),
            ledger.snapshot(),
            no_reference_state,
            zero_fee,
            ledger,
        )
        .code
        == "NO_REFERENCE_PRICE"
    )


def test_open_order_cash_and_margin_reservations_release_exactly() -> None:
    registry = specs()
    stock_gate, stock_ledger = make_gate({STOCK: registry[STOCK]}, cash="100000")
    stock_gate.observe(state_event(STOCK, "10"))
    first = intent(STOCK, Side.BUY, "6000", key="cash-1", price="10")
    second = intent(STOCK, Side.BUY, "6000", key="cash-2", price="10")
    assert stock_gate.check(first, stock_ledger.snapshot()).accepted
    stock_gate.reserve(first)
    expected_cash = stock_gate._cash_reservations[first.idempotency_key]
    stock_gate._cash_reservations[first.idempotency_key] = (
        expected_cash[0],
        expected_cash[1] + 1,
        expected_cash[2],
    )
    with pytest.raises(ValidationError, match="different cash requirement"):
        stock_gate.reserve(first)
    stock_gate._cash_reservations[first.idempotency_key] = expected_cash
    assert stock_gate.check(second, stock_ledger.snapshot()).code == "INSUFFICIENT_AVAILABLE_CASH"
    broker = DeterministicBroker()
    first_order = broker.submit(first)
    stock_gate.release_fill(
        Fill(
            fill_id="partial",
            order_id=first_order.order_id,
            account_id="account",
            strategy_id="strategy",
            instrument_id=STOCK,
            side=Side.BUY,
            quantity=fp("3000"),
            price=fp("10"),
            event_time=T0,
        ),
        first_order,
    )
    assert stock_gate.check(second, stock_ledger.snapshot()).accepted
    stock_gate.release_order(first_order)

    future_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={FUTURE: registry[FUTURE]},
        initial_cash={"CNY": fp("200000")},
    )
    future_gate = RuleBookRiskGate(instruments={FUTURE: registry[FUTURE]}, ledger=future_ledger)
    future_gate.observe(state_event(FUTURE, "4000"))
    margin1 = intent(FUTURE, Side.BUY, "1", key="margin-1", price="4000")
    margin2 = intent(FUTURE, Side.BUY, "1", key="margin-2", price="4000")
    assert future_gate.check(margin1, future_ledger.snapshot()).accepted
    future_gate.reserve(margin1)
    expected_margin = future_gate._margin_reservations[margin1.idempotency_key]
    future_gate._margin_reservations[margin1.idempotency_key] = (
        expected_margin[0] + 1,
        expected_margin[1],
    )
    with pytest.raises(ValidationError, match="different margin requirement"):
        future_gate.reserve(margin1)
    future_gate._margin_reservations[margin1.idempotency_key] = expected_margin
    assert (
        future_gate.check(margin2, future_ledger.snapshot()).code == "INSUFFICIENT_AVAILABLE_MARGIN"
    )
    future_gate.release_order(DeterministicBroker().submit(margin1))
    assert future_gate.check(margin2, future_ledger.snapshot()).accepted
