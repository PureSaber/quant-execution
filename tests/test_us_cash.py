from dataclasses import replace
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from quant_data_kit import AssetClass, CorporateActionEvent, MarkPriceEvent
from quant_data_kit.exceptions import ValidationError

from quant_execution.contracts import OrderIntent, OrderType, Side, TimeInForce
from quant_execution.rules import MarketState, RuleBookRiskGate, USCashEquityRule
from quant_execution.us_cash import USCashAccount, fp, instrument, settled_cash


def account(at="2024-05-24T14:00:00Z", cash=1000, **kwargs):
    stamp = pd.Timestamp(at).to_pydatetime()
    spec = instrument("US:A", "AAPL", stamp)
    return USCashAccount({"US:A": spec}, cash, stamp, **kwargs)


def test_same_day_resale_allowed_but_proceeds_not_reusable():
    a = account(commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00:00Z"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 10, 100, at, "b")
    a.trade("US:A", -10, 100, at, "s")
    assert a.quantity("US:A") == 0
    assert a.buying_power(at) == 0
    with pytest.raises(ValueError, match="settled"):
        a.trade("US:A", 1, 100, at, "blocked")
    assert settled_cash(a.ledger, pd.Timestamp("2024-05-28T14:00Z")) == 0
    assert settled_cash(a.ledger, pd.Timestamp("2024-05-29T14:00Z")) == 1000
    a.validate_balance()


def test_post_transition_t1_settlement_and_us_rule_dispatch():
    a = account(at="2024-05-28T14:00Z", commission_bps=0, slippage_bps=0)
    at = "2024-05-28T14:00Z"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 10, 100, at, "b")
    a.trade("US:A", -10, 100, at, "s")
    assert a.buying_power("2024-05-29T14:00Z") == 1000
    assert isinstance(RuleBookRiskGate._rule(a.ledger.instruments["US:A"]), USCashEquityRule)


def test_dividend_entitlement_survives_sale_and_is_not_early_cash():
    a = account(commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 10, 100, at, "b")
    a.action("US:A", "2024-05-28T13:30Z", "ex", cash=1)
    assert a.ledger.dividend_receivable_balance("USD") == 10
    assert a.buying_power("2024-05-28T13:30Z") == 0
    a.trade("US:A", -10, 100, "2024-05-28T13:30Z", "s")
    a.action("US:A", "2024-05-30T13:30Z", "pay", cash=1, payment=True, ex_date=date(2024, 5, 28))
    assert a.ledger.dividend_receivable_balance("USD") == 0
    assert a.ledger.cash_balance("USD") == 1010
    a.validate_balance()


def test_split_preserves_nav_and_fractional_sell_balances():
    a = account(cash=100000, commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", "102.73921149", at, "m")
    a.trade("US:A", "30.123456", "102.73921149", at, "b")
    before = a.ledger.snapshot().nav
    a.action("US:A", "2024-05-28T13:30Z", "split", ratio=2)
    a.mark("US:A", "51.36960574", "2024-05-28T13:30Z", "m2")
    assert a.quantity("US:A") == Decimal("60.246912")
    assert abs(a.ledger.snapshot().nav.to_decimal() - before.to_decimal()) < Decimal("0.000001")
    a.trade("US:A", "-1.389008", "102.07636123", "2024-05-28T13:30Z", "s")
    a.validate_balance()


def test_terminal_zero_has_explicit_evidence_and_clears_position():
    a = account(commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 5, 100, at, "b")
    a.ledger.apply(
        CorporateActionEvent(
            **a._fields("delist", "US:A", "2024-05-28T13:30Z"),
            action_type="terminal_cash",
            effective_date=date(2024, 5, 28),
            ratio=fp(0, 6),
            cash_amount=fp(0),
            currency="USD",
        )
    )
    assert a.quantity("US:A") == 0
    assert a.ledger.snapshot().nav.to_decimal() == 500
    a.validate_balance()


def test_idempotency_costs_shorting_and_invalid_quantities():
    a = account(commission_bps=10, slippage_bps=20)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", 100, at, "m")
    fill = a.trade("US:A", 1, 100, at, "b")
    assert fill["price"] == "100.20000000"
    assert fill["fee"] == "0.10020000"
    count = len(a.ledger.transactions)
    assert a.trade("US:A", 1, 100, at, "b") == fill
    assert len(a.ledger.transactions) == count
    with pytest.raises(ValueError, match="reused"):
        a.trade("US:A", 2, 100, at, "b")
    for quantity in (0, -2, "0.0000001"):
        with pytest.raises(ValueError):
            a.trade("US:A", quantity, 100, at, str(quantity))
    a.validate_balance()


def test_fractional_sales_round_postings_before_pnl():
    a = account(cash=100000, commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", 100, at, "m")
    for number in range(10):
        a.trade("US:A", "13.123457", str(100 + number / 7), at, f"buy{number}")
    for number in range(30):
        a.trade("US:A", "-1.389008", str(102 + number / 13), at, f"sell{number}")
    a.validate_balance()


def test_us_risk_gate_checks_currency_settlement_shorting_and_fee():
    a = account(commission_bps=0, slippage_bps=0)
    at = pd.Timestamp("2024-05-24T14:00Z").to_pydatetime()
    spec = a.ledger.instruments["US:A"]
    event = MarkPriceEvent(**a._fields("risk-mark", "US:A", at), price=fp(100))
    state = MarketState(event=event, reference_price=fp(100), status="open")
    intent = OrderIntent(
        idempotency_key="risk",
        account_id="us-research",
        strategy_id="test",
        instrument_id="US:A",
        side=Side.BUY,
        quantity=fp(1, 6),
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.DAY,
        created_at=at,
    )
    rule = USCashEquityRule()
    snapshot = a.ledger.snapshot()
    assert rule.check(intent, snapshot, state, spec, a.ledger).accepted
    assert not rule.check(
        intent, snapshot, replace(state, reference_price=None), spec, a.ledger
    ).accepted
    assert (
        rule.check(intent, snapshot, state, replace(spec, settlement_currency="CNY"), a.ledger).code
        == "US_CURRENCY"
    )
    sell = replace(intent, side=Side.SELL)
    assert rule.check(sell, snapshot, state, spec, a.ledger).code == "US_NO_SHORT"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 10, 100, at, "b")
    assert rule.check(sell, a.ledger.snapshot(), state, spec, a.ledger).accepted
    a.trade("US:A", -10, 100, at, "s")
    assert (
        rule.check(intent, a.ledger.snapshot(), state, spec, a.ledger).code == "US_UNSETTLED_CASH"
    )
    assert rule.fee_rate(
        None, None, state, replace(spec, metadata={"commission_rate": "0.001"}), a.ledger
    ) == Decimal("0.001")


@pytest.mark.parametrize(
    "ratio,cash,currency",
    [(1, 0, "USD"), (None, 0, "USD"), (0, None, "USD"), (0, -1, "USD"), (0, 0, "CNY")],
)
def test_terminal_action_rejects_incomplete_or_invalid_evidence(ratio, cash, currency):
    a = account()
    before = a.ledger.snapshot()
    with pytest.raises(ValidationError):
        a.ledger.apply(
            CorporateActionEvent(
                **a._fields("invalid-terminal", "US:A", "2024-05-28T13:30Z"),
                action_type="terminal_cash",
                effective_date=date(2024, 5, 28),
                ratio=None if ratio is None else fp(ratio, 6),
                cash_amount=None if cash is None else fp(cash),
                currency=currency,
            )
        )
    assert a.ledger.snapshot() == before


def test_us_cash_rejects_non_cash_assets_even_if_product_label_is_us():
    at = pd.Timestamp("2024-05-24T14:00Z").to_pydatetime()
    spec = replace(instrument("US:A", "A", at), asset_class=AssetClass.FUTURE)
    with pytest.raises(ValueError, match="USD US equity"):
        USCashAccount({"US:A": spec}, 1000, at)


def test_account_events_and_cash_queries_cannot_rewind_state():
    a = account(commission_bps=0, slippage_bps=0)
    at = "2024-05-24T14:00Z"
    a.mark("US:A", 100, at, "m")
    a.trade("US:A", 1, 100, at, "b")
    a.mark("US:A", 100, "2024-05-28T14:00Z", "next")
    before = a.ledger.snapshot()
    for call in (
        lambda: a.buying_power(at),
        lambda: a.mark("US:A", 99, at, "old"),
        lambda: a.action("US:A", at, "old-split", ratio=2),
        lambda: a.trade("US:A", -1, 100, at, "old-sale"),
    ):
        with pytest.raises(ValueError, match="precede"):
            call()
        assert a.ledger.snapshot() == before
    assert a.trade("US:A", 1, 100, at, "b") == a.fills[0]
