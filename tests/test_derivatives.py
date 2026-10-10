from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from quant_data_kit.derivatives.demo import demo_records

from quant_execution.derivatives import DerivativeAccount


def test_futures_variation_margin_and_atomic_idempotence():
    contracts, quotes = demo_records()
    at = quotes[0].at
    account = DerivativeAccount(contracts, 1000, at, fee_per_contract=1)
    account.trade_batch([("DEMO:H", 2, 100)], at, "open")
    account.trade_batch([("DEMO:H", 2, 100)], at, "open")
    assert len(account.fills) == 1
    assert account.snapshot()["initial_margin"] == "200.00000000"
    with pytest.raises(ValueError, match="changed content"):
        account.trade_batch([("DEMO:H", 3, 100)], at, "open")
    next_day = at + timedelta(days=1)
    account.settle_future("DEMO:H", 103, next_day, "settle")
    assert Decimal(account.snapshot()["cash"]) == 1058
    assert Decimal(account.snapshot()["nav"]) == 1058
    account.settle_future("DEMO:H", 103, next_day, "settle")
    account.trade_batch([("DEMO:H", -2, 104)], next_day, "close")
    assert Decimal(account.snapshot()["cash"]) == 1076
    assert account.quantity("DEMO:H") == 0
    account.validate_balance()
    before = account.snapshot()
    with pytest.raises(ValueError, match="insufficient"):
        account.trade_batch([("DEMO:H", 100, 100), ("DEMO:M", -100, 101)], next_day, "bad")
    assert account.snapshot() == before


@pytest.mark.parametrize(
    "quantity,underlying,expected", [(2, 110, 1260), (-2, 110, 740), (2, 90, 960)]
)
def test_option_premium_multiplier_and_expiry(quantity, underlying, expected):
    contracts, quotes = demo_records("option")
    c = contracts[0]  # strike 95, multiplier 10
    account = DerivativeAccount([c], 1000, quotes[0].at)
    account.trade_batch([(c.instrument_id, quantity, 2)], quotes[0].at, "open")
    assert Decimal(account.snapshot()["nav"]) == 1000
    assert Decimal(account.snapshot()["initial_margin"]) == (600 if quantity < 0 else 0)
    account.exercise(c.instrument_id, underlying, c.expiry, "expiry")
    account.exercise(c.instrument_id, underlying, c.expiry, "expiry")
    assert Decimal(account.snapshot()["cash"]) == expected
    assert account.quantity(c.instrument_id) == 0
    account.validate_balance()


def test_physical_delivery_and_early_exercise():
    contracts, quotes = demo_records("option")
    c = replace(contracts[0], settlement="physical", exercise_style="american")
    account = DerivativeAccount([c], 10000, quotes[0].at)
    account.trade_batch([(c.instrument_id, 1, 2)], quotes[0].at, "open")
    at = quotes[0].at + timedelta(days=1)
    account.exercise(c.instrument_id, 110, at, "early", early=True)
    assert account.quantity(c.underlying) == 10
    assert Decimal(account.snapshot()["cash"]) == 9030
    assert Decimal(account.snapshot()["nav"]) == 10130
    account.validate_balance()
    with pytest.raises(ValueError, match="current account time"):
        account.mark(c.underlying, 111, quotes[0].at, "old")


def test_margin_unknown_negative_price_and_wrong_exercise_fail_closed():
    contracts, quotes = demo_records("option")
    c = replace(contracts[0], initial_margin=None, maintenance_margin=None)
    account = DerivativeAccount([c], 1000, quotes[0].at)
    for legs, error in [
        ([(c.instrument_id, -1, 2)], "margin"),
        ([(c.instrument_id, 1, -2)], "positive"),
        ([(c.instrument_id, 0.5, 2)], "integer"),
    ]:
        with pytest.raises(ValueError, match=error):
            account.trade_batch(legs, quotes[0].at, "bad")
    with pytest.raises(ValueError, match="American"):
        account.exercise(c.instrument_id, 100, quotes[0].at, "early", early=True)
    assert not account.fills
