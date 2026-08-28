from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import (
    AggressorSide,
    BookLevel,
    BookSnapshotEvent,
    CorporateActionEvent,
    MarkPriceEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
)
from quant_data_kit.exceptions import ValidationError
from test_engine import FixtureStrategy, Signal, bar, engine_for
from test_engine_edges import BadStrategy
from test_matching import ASSET, instrument, order
from test_rules import FUTURE, PERP, SPOT, STOCK, intent, specs, state_event

from quant_execution import (
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderStatus,
    OrderType,
    Settlement,
    Side,
    TimeInForce,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.engine import ReplayError
from quant_execution.ledger import ExactAccountLedger
from quant_execution.matching import L2MatchingModel, TradeBBOModel
from quant_execution.rules import RuleBookRiskGate


def _fill(
    fill_id: str,
    order_id: str,
    instrument_id: str,
    side: Side,
    quantity: str,
    price: str,
    *,
    seconds: int = 1,
) -> Fill:
    scale = 3 if "." in quantity else 0
    return Fill(
        fill_id=fill_id,
        order_id=order_id,
        account_id="account",
        strategy_id="strategy",
        instrument_id=instrument_id,
        side=side,
        quantity=fp(quantity, scale),
        price=fp(price),
        event_time=T0 + timedelta(seconds=seconds),
        liquidity_role=LiquidityRole.TAKER,
    )


def test_broker_fill_idempotency_and_conflict_are_fail_closed() -> None:
    broker = DeterministicBroker()
    submitted = broker.submit(intent(STOCK, Side.BUY, "100", key="broker-fill"))
    fill = _fill("same-fill", submitted.order_id, STOCK, Side.BUY, "100", "10")
    first = broker.apply_fill(fill)
    second = broker.apply_fill(fill)
    assert second == first
    assert broker.orders[0].filled_quantity == fp("100", 0)
    assert len(broker.order_events) == 2
    with pytest.raises(ValidationError, match="different fill content"):
        broker.apply_fill(replace(fill, price=fp("11")))
    assert broker.orders[0].filled_quantity == fp("100", 0)


def test_l2_passive_trade_is_price_level_causal_and_scale_exact() -> None:
    broker = DeterministicBroker()
    passive = order(broker, "queue-99", quantity="1", price="99")
    model = L2MatchingModel({ASSET: instrument()})
    snapshot = BookSnapshotEvent(
        **event_fields("book", ASSET, seconds=1, sequence=1),
        bids=(BookLevel(fp("99"), fp("1")), BookLevel(fp("98"), fp("2"))),
        asks=(BookLevel(fp("100"), fp("1")),),
    )
    assert model.match(snapshot, [passive]) == ()
    wrong_level = TradeEvent(
        **event_fields("trade-98", ASSET, seconds=2),
        price=fp("98"),
        quantity=fp("10"),
        aggressor_side=AggressorSide.SELL,
    )
    assert model.match(wrong_level, [passive]) == ()
    wrong_scale = replace(wrong_level, event_id="trade-scale", price=fp("99", 3))
    with pytest.raises(ValidationError, match="price scale"):
        model.match(wrong_scale, [passive])
    right_level = replace(wrong_level, event_id="trade-99", price=fp("99"))
    matched = model.match(right_level, [passive])
    assert matched and matched[0].price == fp("99")


def test_strategy_callback_cannot_backdate_or_future_date_intent() -> None:
    event = bar("causal", STOCK, 60, "10")
    valid = OrderIntent(
        idempotency_key="causal",
        account_id="account",
        strategy_id="strategy",
        instrument_id=STOCK,
        side=Side.BUY,
        quantity=fp("100", 0),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=event.available_at,
        limit_price=fp("10"),
    )
    for created_at in (
        event.available_at - timedelta(seconds=1),
        event.available_at + timedelta(seconds=1),
    ):
        engine = engine_for(
            run_id="causal",
            registry={STOCK: specs()[STOCK]},
            initial_cash={"CNY": fp("10000")},
            base_currency="CNY",
            strategy=BadStrategy([replace(valid, created_at=created_at)]),
        )
        with pytest.raises(ReplayError, match="must equal"):
            engine.replay([event], 1)
        assert engine.broker.orders == ()


def test_spot_and_reduce_only_reservations_conserve_position_and_release_exactly() -> None:
    registry = specs()
    stock_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: registry[STOCK]},
        initial_cash={"CNY": fp("100000")},
    )
    stock_gate = RuleBookRiskGate(instruments={STOCK: registry[STOCK]}, ledger=stock_ledger)
    stock_market = state_event(STOCK, "10", trading_day=date(2026, 1, 3))
    stock_gate.observe(stock_market)
    stock_ledger.observe_market(stock_market, create_snapshot=False)
    stock_ledger.apply_with_trading_day(
        _fill("stock-prior", "external", STOCK, Side.BUY, "1000", "10"),
        trading_day=date(2026, 1, 2),
        create_snapshot=False,
    )
    stock_sell_one = intent(STOCK, Side.SELL, "600", key="stock-sell-1", price="10")
    stock_sell_two = intent(STOCK, Side.SELL, "500", key="stock-sell-2", price="10")
    assert stock_gate.check(stock_sell_one, stock_ledger.snapshot()).accepted
    stock_gate.reserve(stock_sell_one)
    assert (
        stock_gate.check(stock_sell_two, stock_ledger.snapshot()).code
        == "INSUFFICIENT_AVAILABLE_POSITION"
    )

    spot_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={SPOT: registry[SPOT]},
        initial_cash={"USDT": fp("1000")},
    )
    spot_gate = RuleBookRiskGate(instruments={SPOT: registry[SPOT]}, ledger=spot_ledger)
    market = state_event(SPOT, "100", trading_day=date(2026, 1, 3))
    spot_gate.observe(market)
    spot_ledger.observe_market(market, create_snapshot=False)
    spot_ledger.apply_with_trading_day(
        _fill("spot-open", "external", SPOT, Side.BUY, "1.000", "100"),
        trading_day=date(2026, 1, 2),
        create_snapshot=False,
    )
    first = intent(SPOT, Side.SELL, "0.600", key="spot-sell-1", price="100")
    second = intent(SPOT, Side.SELL, "0.600", key="spot-sell-2", price="100")
    assert spot_gate.check(first, spot_ledger.snapshot()).accepted
    spot_gate.reserve(first)
    assert spot_gate.check(second, spot_ledger.snapshot()).code == "INSUFFICIENT_AVAILABLE_POSITION"
    broker = DeterministicBroker()
    first_order = broker.submit(first)
    partial = _fill(
        "spot-partial", first_order.order_id, SPOT, Side.SELL, "0.200", "100", seconds=2
    )
    spot_ledger.apply_with_trading_day(partial, trading_day=date(2026, 1, 3), create_snapshot=False)
    broker.apply_fill(partial)
    spot_gate.release_fill(partial, first_order)
    assert spot_gate.check_open_order_current(
        broker.get_order(first_order.order_id), event_time=T0 + timedelta(seconds=2)
    ).accepted
    assert spot_gate.check(
        replace(second, quantity=fp("0.400", 3)), spot_ledger.snapshot()
    ).accepted
    assert spot_gate.check(second, spot_ledger.snapshot()).code == "INSUFFICIENT_AVAILABLE_POSITION"
    spot_gate.release_order(first_order)
    assert spot_gate.check(second, spot_ledger.snapshot()).accepted

    perp_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: registry[PERP]},
        initial_cash={"USDT": fp("1000")},
    )
    perp_gate = RuleBookRiskGate(instruments={PERP: registry[PERP]}, ledger=perp_ledger)
    perp_market = state_event(PERP, "100")
    perp_gate.observe(perp_market)
    perp_ledger.observe_market(perp_market, create_snapshot=False)
    perp_ledger.apply_with_trading_day(
        _fill("perp-open", "external", PERP, Side.BUY, "1.000", "100"),
        trading_day=perp_market.trading_day,
        create_snapshot=False,
    )
    reduce_one = intent(PERP, Side.SELL, "0.600", key="reduce-1", price="100", reduce_only=True)
    reduce_two = intent(PERP, Side.SELL, "0.600", key="reduce-2", price="100", reduce_only=True)
    assert perp_gate.check(reduce_one, perp_ledger.snapshot()).accepted
    perp_gate.reserve(reduce_one)
    assert (
        perp_gate.check(reduce_two, perp_ledger.snapshot()).code
        == "INSUFFICIENT_AVAILABLE_POSITION"
    )


def test_cross_currency_derivative_margin_is_compared_in_base_currency() -> None:
    future = specs()[FUTURE]
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments={FUTURE: future},
        initial_cash={"USD": fp("2000")},
        fx_to_base={"CNY": fp("0.01")},
    )
    gate = RuleBookRiskGate(instruments={FUTURE: future}, ledger=ledger)
    market = state_event(FUTURE, "4000")
    gate.observe(market)
    ledger.observe_market(market, create_snapshot=False)
    opening = intent(FUTURE, Side.BUY, "1", key="usd-cny", price="4000")
    assert gate.check(opening, ledger.snapshot()).accepted
    gate.reserve(opening)


def test_daily_mark_and_split_keep_nav_continuous_without_double_count() -> None:
    registry = specs()
    perp_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments={PERP: registry[PERP]},
        initial_cash={"USDT": fp("1000")},
    )
    perp_ledger.mark(
        MarkPriceEvent(**event_fields("mark-100", PERP), price=fp("100")),
        create_snapshot=False,
    )
    perp_ledger.apply_with_trading_day(
        _fill("perp-settle-open", "external", PERP, Side.BUY, "1.000", "100"),
        trading_day=T0.date(),
        create_snapshot=False,
    )
    perp_ledger.mark(
        MarkPriceEvent(**event_fields("mark-110", PERP, seconds=2), price=fp("110")),
        create_snapshot=False,
    )
    before = perp_ledger.snapshot().nav
    after = perp_ledger.apply(
        Settlement(
            settlement_id="daily",
            account_id="account",
            instrument_id=PERP,
            amount=fp("10"),
            currency="USDT",
            event_time=T0 + timedelta(seconds=3),
            settlement_type="daily_mark",
            settlement_price=fp("110"),
        )
    )
    assert after is not None and after.nav == before
    assert after.unrealized_pnl[PERP].units == 0

    stock_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: registry[STOCK]},
        initial_cash={"CNY": fp("10000")},
    )
    stock_ledger.mark(
        MarkPriceEvent(**event_fields("stock-mark", STOCK), price=fp("10")),
        create_snapshot=False,
    )
    stock_ledger.apply_with_trading_day(
        _fill("stock-open", "external", STOCK, Side.BUY, "100", "10"),
        trading_day=T0.date(),
        create_snapshot=False,
    )
    before_split = stock_ledger.snapshot().nav
    stock_ledger.apply(
        CorporateActionEvent(
            **event_fields("split", STOCK, seconds=2),
            action_type="split",
            effective_date=T0.date(),
            ratio=fp("2"),
        ),
        create_snapshot=False,
    )
    assert stock_ledger.snapshot().nav == before_split


def test_futures_close_today_fee_uses_exact_fifo_bucket_split() -> None:
    future = specs()[FUTURE]
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={FUTURE: future},
        initial_cash={"CNY": fp("1000000")},
    )
    gate = RuleBookRiskGate(instruments={FUTURE: future}, ledger=ledger)
    day = date(2026, 1, 3)
    market = state_event(FUTURE, "4000", trading_day=day)
    gate.observe(market)
    ledger.observe_market(market, create_snapshot=False)
    ledger.apply_with_trading_day(
        _fill("prior", "external-1", FUTURE, Side.BUY, "1", "4000"),
        trading_day=date(2026, 1, 2),
        create_snapshot=False,
    )
    ledger.apply_with_trading_day(
        _fill("today", "external-2", FUTURE, Side.BUY, "1", "4000"),
        trading_day=day,
        create_snapshot=False,
    )
    close_intent = intent(FUTURE, Side.SELL, "2", key="mixed-close", price="4000", reduce_only=True)
    close_order = DeterministicBroker().submit(close_intent)
    close_fill = _fill("mixed-fill", close_order.order_id, FUTURE, Side.SELL, "2", "4000")
    ledger.apply_with_trading_day(close_fill, trading_day=day, create_snapshot=False)
    fee = gate.fee_for(close_fill, close_order)
    assert fee is not None and fee.amount.to_decimal() == Decimal(264)
    assert "prior=1" in fee.fee_type and "today=1" in fee.fee_type


def test_fx_is_utc_conflict_safe_versioned_and_changes_journal_hash() -> None:
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USD",
        instruments={},
        initial_cash={"CNY": fp("100")},
        fx_to_base={"CNY": fp("0.14")},
    )
    initial_hash = ledger.journal_sha256
    with pytest.raises(ValidationError, match="timezone-aware"):
        ledger.set_fx_rate("CNY", fp("0.15"), event_time=T0.replace(tzinfo=None))
    ledger.set_fx_rate("CNY", fp("0.15"), event_time=T0)
    changed_hash = ledger.journal_sha256
    assert changed_hash != initial_hash
    ledger.set_fx_rate("CNY", fp("0.15"), event_time=T0)
    assert ledger.journal_sha256 == changed_hash
    with pytest.raises(ValidationError, match="conflicts"):
        ledger.set_fx_rate("CNY", fp("0.16"), event_time=T0)
    assert ledger.journal_sha256 == changed_hash


def test_open_order_is_expired_before_matching_after_market_suspension() -> None:
    strategy = FixtureStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100", 0), fp("9"))]})
    engine = engine_for(
        run_id="suspension",
        registry={STOCK: specs()[STOCK]},
        initial_cash={"CNY": fp("10000")},
        base_currency="CNY",
        strategy=strategy,
    )
    halted = StatusEvent(
        **event_fields("halt", STOCK, seconds=120), status="suspended", reason="fixture"
    )
    engine.replay(
        [bar("signal", STOCK, 60, "10"), halted, bar("would-fill", STOCK, 180, "9")],
        1,
    )
    assert engine.broker.orders[0].status is OrderStatus.EXPIRED
    assert engine.artifacts is not None and engine.artifacts.fills == ()


class MarketBuyStrategy:
    def on_event(self, context, event):
        if event.event_id != "signal":
            return ()
        return (
            OrderIntent(
                idempotency_key="market-buy",
                account_id=context.account_id,
                strategy_id=context.strategy_id,
                instrument_id=SPOT,
                side=Side.BUY,
                quantity=fp("10.000", 3),
                order_type=OrderType.MARKET,
                time_in_force=TimeInForce.GTC,
                created_at=event.available_at,
            ),
        )


def test_actual_spot_fill_price_and_fee_cannot_create_negative_cash() -> None:
    registry = {SPOT: specs()[SPOT]}
    engine = engine_for(
        run_id="bar-gap",
        registry=registry,
        initial_cash={"USDT": fp("1500")},
        base_currency="USDT",
        strategy=MarketBuyStrategy(),
    )
    engine.replay(
        [
            bar("signal", SPOT, 60, "100", volume="100.000"),
            bar("gap", SPOT, 120, "200", volume="100.000"),
        ],
        1,
    )
    assert engine.artifacts is not None and engine.artifacts.fills == ()
    assert engine.ledger.cash_balance("USDT") == Decimal(1500)

    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100.04")},
    )
    gate = RuleBookRiskGate(instruments=registry, ledger=ledger)
    quote = replace(
        state_event(SPOT, "100"),
        event_id="quote-state",
    )
    gate.observe(quote)
    ledger.observe_market(quote, create_snapshot=False)
    buy = intent(SPOT, Side.BUY, "1.000", key="bbo-buy", price="100")
    order_record = DeterministicBroker().submit(buy)
    expensive = _fill("bbo-expensive", order_record.order_id, SPOT, Side.BUY, "1.000", "100")
    assert gate.check_fill(expensive, order_record).code == "INSUFFICIENT_CASH_AT_FILL"

    bbo_ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("1800")},
    )
    from quant_execution.engine import DeterministicRunEngine

    bbo_engine = DeterministicRunEngine(
        run_id="bbo-gap",
        account_id="account",
        strategy_id="strategy",
        strategy=MarketBuyStrategy(),
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=bbo_ledger),
        matching_model=TradeBBOModel(registry),
        ledger=bbo_ledger,
    )
    signal = replace(state_event(SPOT, "100"), event_id="signal")
    wide_quote = QuoteEvent(
        **event_fields("wide-quote", SPOT, seconds=1),
        bid_price=fp("100"),
        bid_quantity=fp("100"),
        ask_price=fp("200"),
        ask_quantity=fp("100"),
    )
    bbo_engine.replay([signal, wide_quote], 1)
    assert bbo_engine.artifacts is not None and bbo_engine.artifacts.fills == ()
    assert bbo_ledger.cash_balance("USDT") == Decimal(1800)


class TwoFillThenFailModel:
    def match(self, event, open_orders):
        if not open_orders:
            return ()
        order_record = open_orders[0]
        good = _fill(
            "atomic-good",
            order_record.order_id,
            STOCK,
            Side.BUY,
            "50",
            "10",
            seconds=120,
        )
        bad = replace(good, fill_id="atomic-bad", order_id="missing")
        return good, bad


class CountingStrategy(FixtureStrategy):
    pass


def test_replay_failure_atomically_rolls_back_all_visible_component_state() -> None:
    strategy = CountingStrategy({"signal": [Signal(STOCK, Side.BUY, fp("100", 0), fp("10"))]})
    registry = {STOCK: specs()[STOCK]}
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("10000")},
    )
    from quant_execution.engine import DeterministicRunEngine

    engine = DeterministicRunEngine(
        run_id="atomic",
        account_id="account",
        strategy_id="strategy",
        strategy=strategy,
        broker=DeterministicBroker(),
        risk_gate=RuleBookRiskGate(instruments=registry, ledger=ledger),
        matching_model=TwoFillThenFailModel(),
        ledger=ledger,
    )
    before_hash = ledger.journal_sha256
    with pytest.raises(ReplayError, match="unknown order"):
        engine.replay([bar("signal", STOCK, 60, "10"), bar("match", STOCK, 120, "10")], 1)
    assert engine.broker.orders == () and engine.broker.order_events == ()
    assert ledger.journal_sha256 == before_hash and ledger.snapshot().positions == {}
    assert strategy.calls == 0 and engine.artifacts is None
