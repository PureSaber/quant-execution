from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, event_fields, fp
from quant_data_kit import CorporateActionEvent
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.share_entitlements import EntitlementUnresolved, ShareEntitlement
from test_ledger import STOCK, fill, stock_spec

from quant_execution import Side
from quant_execution.ledger import ExactAccountLedger


def setup():
    ledger = ExactAccountLedger(
        account_id="account",
        base_currency="CNY",
        instruments={STOCK: stock_spec()},
        initial_cash={"CNY": fp("10000")},
    )
    ledger.apply(fill("buy", STOCK, Side.BUY, "100", "10", seconds=0))
    terms = ShareEntitlement(
        entitlement_id="distribution",
        account_id="account",
        record_date=T0.date(),
        eligible_quantity=fp("100"),
        ordinary_additional_ratio=fp("0.400595", 6),
        known_at=T0,
        credit_at=T0 + timedelta(days=1),
        sellable_date=(T0 + timedelta(days=3)).date(),
        evidence_source="test:official-announcement",
        evidence_sha256="a" * 64,
        fractional_rule="largest_fraction",
    )
    event = CorporateActionEvent(
        **event_fields("distribution:account", STOCK, seconds=86400),
        action_type="share_credit",
        effective_date=T0.date(),
        share_entitlement=terms,
    )
    return ledger, event


def test_unresolved_cannot_change_cash_or_holdings_then_confirmed_credit_is_idempotent():
    ledger, event = setup()
    before = ledger.capture_state()
    with pytest.raises(EntitlementUnresolved) as error:
        ledger.apply(event)
    assert error.value.diagnostics["publishable"] is False
    assert ledger.capture_state() == before
    terms = replace(
        event.share_entitlement, evidence_status="confirmed", confirmed_quantity=fp("40")
    )
    confirmed = replace(event, share_entitlement=terms)
    result = ledger.apply(confirmed)
    assert result.positions[STOCK].to_decimal() == 140
    assert result.cash_balances["CNY"].to_decimal() == Decimal(9000)
    assert ledger.acquired_today(STOCK, (T0 + timedelta(days=1)).date()) == 40
    assert ledger.acquired_today(STOCK, (T0 + timedelta(days=2)).date()) == 40
    assert ledger.acquired_today(STOCK, (T0 + timedelta(days=3)).date()) == 0
    assert ledger.apply(confirmed) == result
    with pytest.raises(ValidationError):
        ledger.apply(
            replace(confirmed, share_entitlement=replace(terms, confirmed_quantity=fp("41")))
        )
    assert ledger.snapshot() == result
    # Restoring the captured native state removes every effect; a deterministic
    # replay can apply a separately evidenced revision without a second ledger.
    ledger.restore_state(before)
    assert ledger.apply(confirmed) == result
    for transaction in ledger.transactions:
        totals = {}
        for posting in transaction.postings:
            totals[posting.currency] = (
                totals.get(posting.currency, Decimal(0)) + posting.amount.to_decimal()
            )
        assert all(value == 0 for value in totals.values())


@pytest.mark.parametrize("status, quantity", [("sensitivity_floor", 40), ("sensitivity_ceil", 41)])
def test_separately_identified_sensitivity_quantities(status, quantity):
    ledger, event = setup()
    branch = replace(
        event, share_entitlement=replace(event.share_entitlement, evidence_status=status)
    )
    assert ledger.apply(branch).positions[STOCK].to_decimal() == 100 + quantity


def test_wrong_account_and_future_evidence_are_rejected():
    _, event = setup()
    with pytest.raises(ValidationError, match="event ID"):
        replace(event, share_entitlement=replace(event.share_entitlement, account_id="different"))
    with pytest.raises(ValidationError, match="not available"):
        replace(event, available_at=T0, received_at=T0, event_time=T0)
    with pytest.raises(ValidationError, match="allocation rule"):
        replace(
            event.share_entitlement, fractional_rule="unknown", evidence_status="sensitivity_floor"
        )
