from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

from conftest import T0, event_fields, fp, spec
from quant_data_kit import AssetClass, MarginMode, MarkPriceEvent, StatusEvent, TradeEvent

from quant_execution import (
    Fill,
    LiquidityRole,
    OrderIntent,
    OrderType,
    Side,
    TimeInForce,
)
from quant_execution.broker import DeterministicBroker
from quant_execution.ledger import ExactAccountLedger
from quant_execution.rules import RuleBookRiskGate

STOCK = "equity:sse:600000"
FUTURE = "future:cffex:IF2603"
SPOT = "crypto:test:BTCUSDT"
PERP = "crypto:test:BTCUSDT-PERP"


def specs():
    return {
        STOCK: spec(
            STOCK,
            asset_class=AssetClass.EQUITY,
            product_type="a_share",
            settlement_currency="CNY",
            metadata={
                "lot_size": "100",
                "commission_rate": "0.0003",
                "stamp_duty_rate": "0.001",
                "daily_upper_limit": "12",
                "daily_lower_limit": "8",
            },
        ),
        FUTURE: spec(
            FUTURE,
            asset_class=AssetClass.FUTURE,
            product_type="index_future",
            settlement_currency="CNY",
            multiplier="300",
            margin_mode=MarginMode.CROSS,
            metadata={
                "initial_margin_rate": "0.1",
                "maintenance_margin_rate": "0.08",
                "fee_rate": "0.00002",
                "close_today_fee_rate": "0.0002",
            },
        ),
        SPOT: spec(
            SPOT,
            asset_class=AssetClass.CRYPTO,
            product_type="spot",
            settlement_currency="USDT",
            base_currency="BTC",
            quote_currency="USDT",
            quantity_step="0.001",
            metadata={
                "min_quantity": "0.01",
                "maker_fee_rate": "0.0002",
                "taker_fee_rate": "0.0005",
            },
        ),
        PERP: spec(
            PERP,
            asset_class=AssetClass.CRYPTO,
            product_type="linear_perpetual",
            settlement_currency="USDT",
            base_currency="BTC",
            quote_currency="USDT",
            quantity_step="0.001",
            margin_mode=MarginMode.CROSS,
            metadata={
                "min_quantity": "0.001",
                "initial_margin_rate": "0.1",
                "maintenance_margin_rate": "0.05",
                "maker_fee_rate": "0.0002",
                "taker_fee_rate": "0.0005",
                "close_today_fee_rate": "0.0005",
            },
        ),
    }


def intent(
    instrument_id: str,
    side: Side,
    quantity: str,
    *,
    key: str,
    price: str = "10",
    reduce_only: bool = False,
) -> OrderIntent:
    scale = 3 if "." in quantity else 0
    return OrderIntent(
        idempotency_key=key,
        account_id="account",
        strategy_id="strategy",
        instrument_id=instrument_id,
        side=side,
        quantity=fp(quantity, scale),
        order_type=OrderType.LIMIT,
        time_in_force=TimeInForce.GTC,
        created_at=T0,
        limit_price=fp(price),
        reduce_only=reduce_only,
    )


def state_event(instrument_id: str, price: str, *, trading_day=date(2026, 1, 2)):
    return TradeEvent(
        **event_fields(
            f"trade:{instrument_id}:{trading_day}", instrument_id, trading_day=trading_day
        ),
        price=fp(price),
        quantity=fp("100"),
    )


def test_a_share_lot_t1_limit_status_and_stamp_duty() -> None:
    registry = specs()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
        fx_to_base={"USDT": fp("7")},
    )
    gate = RuleBookRiskGate(instruments=registry, ledger=ledger)
    event = state_event(STOCK, "10")
    gate.observe(event)
    ledger.observe_market(event)
    assert (
        gate.check(intent(STOCK, Side.BUY, "50", key="odd"), ledger.snapshot()).code
        == "A_SHARE_LOT"
    )
    buy_fill = Fill(
        fill_id="stock-fill",
        order_id="stock-order",
        account_id="account",
        strategy_id="strategy",
        instrument_id=STOCK,
        side=Side.BUY,
        quantity=fp("100"),
        price=fp("10"),
        event_time=T0,
        liquidity_role=LiquidityRole.TAKER,
    )
    ledger.apply_with_trading_day(buy_fill, trading_day=event.trading_day)
    sell = intent(STOCK, Side.SELL, "100", key="sell")
    assert gate.check(sell, ledger.snapshot()).code == "A_SHARE_T_PLUS_ONE"
    next_event = state_event(STOCK, "10", trading_day=date(2026, 1, 3))
    gate.observe(next_event)
    assert gate.check(sell, ledger.snapshot()).accepted
    broker = DeterministicBroker()
    order = broker.submit(sell)
    fee = gate.fee_for(
        Fill(
            fill_id="sell-fill",
            order_id=order.order_id,
            account_id="account",
            strategy_id="strategy",
            instrument_id=STOCK,
            side=Side.SELL,
            quantity=fp("100"),
            price=fp("10"),
            event_time=T0 + timedelta(days=1),
            liquidity_role=LiquidityRole.TAKER,
        ),
        order,
    )
    assert fee is not None and fee.amount.to_decimal() == Decimal("1.3")
    halted = StatusEvent(
        **event_fields("halt", STOCK, seconds=1), status="suspended", reason="fixture"
    )
    gate.observe(halted)
    assert (
        gate.check(intent(STOCK, Side.BUY, "100", key="halted"), ledger.snapshot()).code
        == "MARKET_NOT_TRADABLE"
    )


def test_futures_margin_reduce_only_night_day_and_close_today_fee() -> None:
    registry = specs()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments=registry,
        initial_cash={"CNY": fp("100000")},
        fx_to_base={"USDT": fp("7")},
    )
    gate = RuleBookRiskGate(instruments=registry, ledger=ledger)
    night_day = date(2026, 1, 3)
    event = state_event(FUTURE, "4000", trading_day=night_day)
    gate.observe(event)
    ledger.observe_market(event)
    too_large = intent(FUTURE, Side.BUY, "10", key="margin", price="4000")
    assert gate.check(too_large, ledger.snapshot()).code == "INSUFFICIENT_MARGIN"
    reduce = intent(FUTURE, Side.SELL, "1", key="reduce", price="4000", reduce_only=True)
    assert gate.check(reduce, ledger.snapshot()).code == "REDUCE_ONLY_VIOLATION"
    opened = Fill(
        fill_id="future-open",
        order_id="future-order",
        account_id="account",
        strategy_id="strategy",
        instrument_id=FUTURE,
        side=Side.BUY,
        quantity=fp("1"),
        price=fp("4000"),
        event_time=T0,
        liquidity_role=LiquidityRole.TAKER,
    )
    ledger.apply_with_trading_day(opened, trading_day=night_day)
    assert gate.check(reduce, ledger.snapshot()).accepted
    broker = DeterministicBroker()
    reduce_order = broker.submit(reduce)
    fee = gate.fee_for(
        Fill(
            fill_id="future-close",
            order_id=reduce_order.order_id,
            account_id="account",
            strategy_id="strategy",
            instrument_id=FUTURE,
            side=Side.SELL,
            quantity=fp("1"),
            price=fp("4000"),
            event_time=T0,
            liquidity_role=LiquidityRole.TAKER,
        ),
        reduce_order,
    )
    assert fee is not None and fee.amount.to_decimal() == Decimal(240)


def test_crypto_spot_and_perpetual_balance_step_reduce_only_and_liquidation() -> None:
    registry = specs()
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="USDT",
        instruments=registry,
        initial_cash={"USDT": fp("100")},
        fx_to_base={"CNY": fp("0.14", 2)},
    )
    gate = RuleBookRiskGate(instruments=registry, ledger=ledger)
    spot_event = state_event(SPOT, "100")
    gate.observe(spot_event)
    ledger.observe_market(spot_event)
    assert (
        gate.check(intent(SPOT, Side.BUY, "0.001", key="min", price="100"), ledger.snapshot()).code
        == "MIN_QUANTITY"
    )
    assert (
        gate.check(intent(SPOT, Side.BUY, "2.000", key="cash", price="100"), ledger.snapshot()).code
        == "INSUFFICIENT_CASH"
    )
    perp_event = state_event(PERP, "100")
    gate.observe(perp_event)
    ledger.observe_market(perp_event)
    reduce = intent(PERP, Side.SELL, "1.000", key="perp-reduce", price="100", reduce_only=True)
    assert gate.check(reduce, ledger.snapshot()).code == "REDUCE_ONLY_VIOLATION"
    ledger.apply(
        Fill(
            fill_id="leveraged",
            order_id="leveraged-order",
            account_id="account",
            strategy_id="strategy",
            instrument_id=PERP,
            side=Side.BUY,
            quantity=fp("10.000", 3),
            price=fp("100"),
            event_time=T0,
        )
    )
    ledger.mark(
        MarkPriceEvent(
            **event_fields("crash", PERP, seconds=2),
            price=fp("1"),
        )
    )
    decision = gate.runtime_check(ledger.snapshot())
    assert not decision.accepted and decision.code == "LIQUIDATION_REQUIRED"
