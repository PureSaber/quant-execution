from dataclasses import replace
from datetime import date, timedelta
from decimal import Decimal

import pytest
from conftest import T0, fp, spec
from quant_data_kit import AssetClass
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import Side
from quant_execution.hong_kong import HKDailyExecution, HKFeeSchedule
from quant_execution.rules import RuleBookRiskGate


def schedule():
    return HKFeeSchedule(
        valid_from=date(2025, 6, 30),
        valid_to=date(2026, 12, 31),
        commission_rate=Decimal("0.0003"),
        minimum_commission=Decimal(3),
        platform_fee=Decimal(0),
        stamp_rate=Decimal("0.001"),
        sfc_rate=Decimal("0.000027"),
        afrc_rate=Decimal("0.0000015"),
        trading_rate=Decimal("0.0000565"),
        settlement_rate=Decimal("0.000042"),
        settlement_minimum=Decimal(0),
        settlement_maximum=None,
        slippage_rate=Decimal("0.0005"),
        source="HKEX test scenario",
    )


def instrument():
    return replace(
        spec(
            "00700",
            asset_class=AssetClass.EQUITY,
            product_type="hk-equity",
            settlement_currency="HKD",
            base_currency="HKD",
            quote_currency="HKD",
            metadata={"lot_size": "100", "stamp_exempt": "false"},
        ),
        venue="XHKG",
    )


def account():
    return HKDailyExecution(
        {"00700": instrument()},
        initial_cash=fp(100000),
        opened_at=T0,
        fees=schedule(),
        settlement_days=[
            date(2026, 1, 2),
            date(2026, 1, 5),
            date(2026, 1, 6),
            date(2026, 1, 7),
            date(2026, 1, 8),
            date(2026, 1, 9),
        ],
    )


def order(account, key, side, quantity=100, at=T0):
    return account.execute(
        order_id=key, symbol="00700", quantity=quantity, side=side, price=fp(100), at=at
    )


def test_both_sides_have_rounded_stamp_and_component_fees():
    charges = schedule().charge(fp("10000.01"), date(2026, 1, 2), stamp_exempt=False)
    assert charges["stamp"] == fp(11)
    assert charges["sfc"] == fp("0.27")
    assert charges["afrc"] == fp("0.02")
    assert charges["trading"] == fp("0.57")
    assert charges["settlement"] == fp("0.42")
    assert schedule().charge(fp(10000), date(2026, 1, 2), stamp_exempt=True)["stamp"] == fp(0)
    with pytest.raises(ValueError, match="covering"):
        schedule().charge(fp(10000), date(2024, 1, 1), stamp_exempt=False)


def test_same_day_sale_allowed_but_cash_locks_until_t_plus_two_close():
    broker = account()
    _, buy_fee = order(broker, "buy", Side.BUY)
    available = broker.available_cash()
    _, sell_fee = order(broker, "sell", Side.SELL, at=T0 + timedelta(seconds=1))
    assert buy_fee["stamp"] == sell_fee["stamp"] == fp(10)
    assert broker.available_cash() == available
    broker.settle_end_of_day(date(2026, 1, 5))
    assert broker.available_cash() == available
    broker.settle_end_of_day(date(2026, 1, 6))
    assert broker.available_cash() == broker.ledger.cash_balance("HKD")
    for transaction in broker.ledger.transactions:
        assert sum((p.amount.to_decimal() for p in transaction.postings), Decimal(0)) == 0


def test_idempotent_orders_and_invalid_reuse():
    broker = account()
    order(broker, "x", Side.BUY)
    before = broker.ledger.journal_sha256
    order(broker, "x", Side.BUY)
    assert broker.ledger.journal_sha256 == before
    with pytest.raises(ValueError, match="reused"):
        order(broker, "x", Side.BUY, quantity=200)


def test_lots_shorting_and_all_in_fees_are_enforced():
    broker = account()
    for side, quantity, match in [
        (Side.BUY, 101, "board lot"),
        (Side.SELL, 100, "Short selling"),
        (Side.BUY, 1000, "Insufficient settled"),
    ]:
        with pytest.raises(ValueError, match=match):
            order(broker, "bad", side, quantity)
    assert broker.affordable_quantity("00700", fp(100), Decimal(100000), date(2026, 1, 2)) == 900
    assert broker.ledger.cash_balance("HKD") == Decimal(100000)


def test_hk_never_silently_enters_a_share_intraday_rules():
    with pytest.raises(ValidationError, match="HKDailyExecution"):
        RuleBookRiskGate._rule(instrument())


def test_calendar_and_unavailable_rules_fail_before_a_fill():
    broker = account()
    broker.settlement_days = [date(2026, 1, 2)]
    with pytest.raises(ValueError, match=r"T\+2"):
        order(broker, "x", Side.BUY)
    assert not broker.executed


def test_settlement_clock_cannot_reopen_a_closed_session():
    broker = account()
    broker.settle_end_of_day(T0.date())
    with pytest.raises(ValueError, match="already settled"):
        order(broker, "late", Side.BUY)
    with pytest.raises(ValueError, match="backwards"):
        broker.settle_end_of_day(T0.date() - timedelta(days=1))


def test_non_cash_assets_are_rejected():
    future = replace(instrument(), asset_class=AssetClass.FUTURE)
    with pytest.raises(ValueError, match="only accepts"):
        HKDailyExecution(
            {"00700": future},
            initial_cash=fp(100000),
            opened_at=T0,
            fees=schedule(),
            settlement_days=[T0.date()],
        )
