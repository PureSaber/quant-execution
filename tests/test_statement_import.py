from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, fp
from quant_data_kit.exceptions import ValidationError
from test_ledger import STOCK, fill, stock_spec

from quant_execution import ExactAccountLedger, Side


def test_opening_balance_and_external_flows_are_not_fabricated_fills_or_income():
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: stock_spec()},
        initial_cash={"CNY": fp("1000")},
        opened_at=T0,
    )
    opened = ledger.book_opening_position(
        instrument_id=STOCK,
        quantity=fp("100", 0),
        average_cost=fp("10"),
        acquired_on=T0.date() - timedelta(days=2),
    )
    assert opened.nav.to_decimal() == Decimal(2000)
    assert (
        ledger.book_opening_position(
            instrument_id=STOCK,
            quantity=fp("100", 0),
            average_cost=fp("10"),
            acquired_on=T0.date() - timedelta(days=2),
        )
        == opened
    )
    funded = ledger.book_external_cash(
        transfer_id="deposit",
        amount=fp("500"),
        currency="CNY",
        event_time=T0 + timedelta(seconds=1),
    )
    assert funded.nav.to_decimal() == Decimal(2500)
    assert (
        ledger.book_external_cash(
            transfer_id="deposit",
            amount=fp("500"),
            currency="CNY",
            event_time=T0 + timedelta(seconds=1),
        )
        == funded
    )
    with pytest.raises(ValidationError):
        ledger.book_external_cash(
            transfer_id="deposit",
            amount=fp("600"),
            currency="CNY",
            event_time=T0 + timedelta(seconds=1),
        )
    with pytest.raises(ValidationError):
        ledger.book_external_cash(
            transfer_id="withdrawal",
            amount=fp("-9000"),
            currency="CNY",
            event_time=T0 + timedelta(seconds=2),
        )
    final = ledger.apply(fill("sell", STOCK, Side.SELL, "100", "11", seconds=3))
    assert final.cash_balances["CNY"].to_decimal() == Decimal(2600)
    assert final.realized_pnl[STOCK].to_decimal() == Decimal(100)
    postings = [p for t in ledger.transactions for p in t.postings]
    assert any(p.ledger_account == "equity:external_flows" for p in postings)
    assert not any("income:settlement" in p.ledger_account for p in postings)
    assert all(sum(p.amount.to_decimal() for p in t.postings) == 0 for t in ledger.transactions)
