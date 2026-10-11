from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from quant_data_kit import AssetClass, CorporateActionEvent, MarkPriceEvent

from quant_execution.contracts import Fill, Side
from quant_execution.derivatives import fp
from quant_execution.ledger import ExactAccountLedger
from quant_execution.us_cash import instrument


@pytest.mark.parametrize("payout", ["0", "40", "102.125"])
def test_bond_redemption_and_default_balance_and_idempotency(payout):
    at = datetime(2025, 1, 1, tzinfo=timezone.utc)
    spec = replace(
        instrument("BOND", "BOND", at),
        asset_class=AssetClass.BOND,
        product_type="convertible",
        metadata={"research_delivery": "true"},
    )
    ledger = ExactAccountLedger(
        account_id="test",
        base_currency="USD",
        instruments={"BOND": spec},
        initial_cash={"USD": fp(1000)},
        opened_at=at,
    )
    fields = {
        "instrument_id": "BOND",
        "event_time": at,
        "received_at": at,
        "available_at": at,
        "source": "fixture",
        "trading_day": at.date(),
        "session_id": "test",
        "sequence": 0,
    }
    ledger.mark(MarkPriceEvent(**fields, event_id="mark", price=fp(100)))
    ledger.apply(
        Fill(
            fill_id="fill",
            order_id="order",
            account_id="test",
            strategy_id="test",
            instrument_id="BOND",
            side=Side.BUY,
            quantity=fp(3),
            price=fp(100),
            event_time=at,
        )
    )
    event = CorporateActionEvent(
        **fields,
        event_id="redemption",
        action_type="terminal_cash",
        effective_date=at.date(),
        ratio=fp(0),
        cash_amount=fp(payout),
        currency="USD",
    )
    ledger.apply(event)
    count = len(ledger.transactions)
    ledger.apply(event)
    assert len(ledger.transactions) == count
    assert ledger.snapshot().positions["BOND"].units == 0
    assert ledger.snapshot().nav.to_decimal() == 700 + 3 * Decimal(payout)
    for tx in ledger.transactions:
        assert sum(p.amount.to_decimal() for p in tx.postings) == 0
