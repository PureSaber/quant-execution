from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, fp, spec
from quant_data_kit import AssetClass
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial.actions import ActionTerms

from quant_execution.ledger import ExactAccountLedger
from quant_execution.rules import resolve_a_share_replay_status


def account(cash="1000"):
    instruments = {
        name: spec(
            name,
            asset_class=AssetClass.EQUITY,
            product_type="a_share",
            settlement_currency="CNY",
            quantity_step="0.01",
        )
        for name in ("A", "B", "RIGHT")
    }
    ledger = ExactAccountLedger(
        account_id="test",
        base_currency="CNY",
        instruments=instruments,
        initial_cash={"CNY": fp(cash)},
        opened_at=T0,
    )
    ledger.book_opening_position(
        instrument_id="A", quantity=fp(100), average_cost=fp(10), acquired_on=T0.date()
    )
    return ledger


def terms(kind, **kw):
    return ActionTerms(
        **{
            "event_id": kind,
            "instrument_id": "A",
            "kind": kind,
            "effective_at": (T0 + timedelta(days=1)).isoformat(),
            "available_at": T0.isoformat(),
            "currency": "CNY",
            "source": "synthetic",
            "evidence_id": "fixture",
            **kw,
        }
    )


def test_split_and_dividend_payment_have_distinct_cash_and_total_nav():
    ledger = account()
    before = ledger.snapshot().nav.to_decimal()
    split = terms("split", ratio="2")
    after = ledger.apply_corporate_action(split, at=split.effective_at)
    assert after.positions["A"].to_decimal() == 200 and after.nav.to_decimal() == before
    ex = terms("dividend_entitlement", cash_per_unit="1", entitlement_date="2026-01-03")
    ledger.apply_corporate_action(ex, at=ex.effective_at)
    assert ledger.cash_balance("CNY") == 1000
    assert ledger.dividend_receivable_balance("CNY") == 200
    pay = terms(
        "dividend_payment",
        cash_per_unit="1",
        entitlement_date="2026-01-03",
        effective_at=(T0 + timedelta(days=4)).isoformat(),
    )
    ledger.apply_corporate_action(pay, at=pay.effective_at)
    assert ledger.cash_balance("CNY") == 1200 and ledger.dividend_receivable_balance("CNY") == 0
    before_repeat = ledger.snapshot()
    assert ledger.apply_corporate_action(pay, at=pay.effective_at) == before_repeat


@pytest.mark.parametrize(
    "kind,ratio,allocation,parent_mark,target_mark,parent_quantity",
    [
        ("merger", ".5", "1", None, "20", 0),
        ("spin_off", ".5", ".2", "8", "4", 100),
    ],
)
def test_conversion_conserves_wealth_cost_and_idempotency(
    kind, ratio, allocation, parent_mark, target_mark, parent_quantity
):
    ledger = account()
    before = ledger.snapshot().nav.to_decimal()
    event = terms(
        kind,
        ratio=ratio,
        cost_fraction=allocation,
        target_id="B",
        target_mark=target_mark,
        parent_mark=parent_mark,
    )
    result = ledger.apply_corporate_action(event, at=event.effective_at)
    assert result.nav.to_decimal() == before
    assert ledger._positions.get("A", 0) == parent_quantity
    assert result.positions["B"].to_decimal() == 50
    assert not ledger._fills  # No invented historical trades.
    assert sum(ledger._position_cost(x, derivative=False) for x in ("A", "B")) == 1000
    assert ledger.apply_corporate_action(event, at=event.effective_at) == result
    with pytest.raises(ValidationError, match="reused"):
        ledger.apply_corporate_action(replace(event, ratio=".6"), at=event.effective_at)


def test_rights_distribution_election_cash_and_atomic_failure():
    ledger = account(cash="50")
    issue = terms(
        "rights_distribution",
        target_id="RIGHT",
        ratio=".2",
        cost_fraction="0",
        parent_mark="9.8",
        target_mark="1",
    )
    ledger.apply_corporate_action(issue, at=issue.effective_at)
    assert ledger._positions["RIGHT"] == 20
    exercise = terms(
        "rights_exercise",
        instrument_id="RIGHT",
        target_id="A",
        ratio="1",
        election_quantity="20",
        cash_per_unit="5",
        target_mark="9.8",
    )
    before = ledger.capture_state()
    with pytest.raises(ValidationError, match="insufficient cash"):
        ledger.apply_corporate_action(exercise, at=exercise.effective_at)
    assert ledger.capture_state() == before
    exercise = replace(exercise, election_quantity="10")
    ledger.apply_corporate_action(exercise, at=exercise.effective_at)
    assert ledger._positions["RIGHT"] == 10 and ledger._positions["A"] == 110
    assert ledger.cash_balance("CNY") == 0


def test_future_action_and_fractional_steps_do_not_change_account():
    ledger = account()
    event = terms("merger", target_id="B", ratio=".00001", cost_fraction="1", target_mark="10")
    before = ledger.capture_state()
    with pytest.raises(ValidationError):
        ledger.apply_corporate_action(event, at=T0)
    with pytest.raises(ValidationError, match="fractional"):
        ledger.apply_corporate_action(event, at=event.effective_at)
    assert ledger.capture_state() == before


def test_missing_status_preserved_and_price_limit_sides():
    flags = {
        "listed": True,
        "delisted": False,
        "tradable": None,
        "limit_up": False,
        "limit_down": False,
    }
    assert resolve_a_share_replay_status(**flags) == "unknown"
    assert (
        resolve_a_share_replay_status(**{**flags, "tradable": True, "limit_up": True}) == "limit_up"
    )


def test_cash_in_lieu_retires_shares_not_a_fake_dividend():
    ledger = account()
    before = ledger.snapshot().nav.to_decimal()
    event = terms("cash_in_lieu", retired_quantity=".25", cash_per_unit="10")
    ledger.apply_corporate_action(event, at=event.effective_at)
    assert ledger._positions["A"] == Decimal("99.75")
    assert ledger.cash_balance("CNY") == Decimal("1002.5")
    assert ledger.snapshot().nav.to_decimal() == before
