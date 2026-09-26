from dataclasses import replace

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import CorporateActionEvent
from quant_data_kit.exceptions import ValidationError
from test_ledger import STOCK, mark, stock_spec

from quant_execution.ledger import ExactAccountLedger


@pytest.mark.parametrize("ratio", [None, "2"])
def test_unheld_action_has_no_cash_transaction_but_retains_event_identity(ratio):
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: stock_spec()},
        initial_cash={"CNY": fp("10000")},
    )
    ledger.mark(mark(STOCK, "10", 1))
    event = CorporateActionEvent(
        **event_fields("unheld-action", STOCK, seconds=2),
        action_type="split_and_dividend" if ratio else "cash_dividend",
        effective_date=T0.date(),
        cash_amount=fp("1"),
        currency="CNY",
        ratio=fp(ratio) if ratio else None,
    )
    count = len(ledger.transactions)
    after = ledger.apply(event)
    assert after.cash_balances["CNY"].to_decimal() == 10000
    assert after.nav.to_decimal() == 10000
    assert len(ledger.transactions) == count
    assert ledger.apply(event) == after
    with pytest.raises(ValidationError, match="reused"):
        ledger.apply(replace(event, cash_amount=fp("2")))
