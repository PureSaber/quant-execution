from copy import deepcopy
from decimal import Decimal

import pytest

from quant_execution.research_lifecycle import ResearchLifecycle

AT = "2026-01-02T12:00:00Z"
EXPIRY = "2026-02-02T12:00:00Z"


def account(*, settlement="cash", style="american", right="call"):
    specs = {
        "S": {"kind": "stock", "currency": "USD", "multiplier": "1", "known_at": AT},
        "O": {
            "kind": "option",
            "currency": "USD",
            "multiplier": "100",
            "known_at": AT,
            "underlying": "S",
            "right": right,
            "strike": "100",
            "expiry": EXPIRY,
            "style": style,
            "settlement": settlement,
        },
    }
    return ResearchLifecycle("USD", specs)


def event(a, key, kind, **fields):
    at = fields.pop("at", AT)
    e = dict(id=key, type=kind, at=at, available_at=at, **fields)
    a.apply(e)
    return e


def balanced(a):
    balances = {}
    quantities = {}
    for t in a.journal():
        total = Decimal(0)
        for p in t["postings"]:
            value = Decimal(p["amount"]["units"]) / (10 ** p["amount"]["scale"])
            total += value
            balances[p["ledger_account"]] = balances.get(p["ledger_account"], Decimal(0)) + value
            q = p.get("quantity_delta")
            if q is not None:
                quantities[p["instrument_id"]] = quantities.get(
                    p["instrument_id"], Decimal(0)
                ) + Decimal(q["units"]) / (10 ** q["scale"])
        assert total == 0
    assert balances.get("assets:cash", 0) == a.cash
    assert balances.get("assets:collateral", 0) == a.collateral
    assert balances.get("assets:position_cost", 0) == sum(a.costs.values())
    for i, q in quantities.items():
        assert q == a.positions[i]


def test_short_reversal_realized_pnl_and_exact_journal():
    a = account()
    event(a, "deposit", "deposit", amount="10000")
    event(a, "locate", "locate", instrument="S", quantity="100", expires_at=EXPIRY)
    event(a, "sell", "fill", instrument="S", quantity="-10", price="100", fee="1")
    event(a, "reverse", "fill", instrument="S", quantity="15", price="90", fee="1")
    event(a, "mark", "mark", prices={"S": "90"})
    assert a.cash == Decimal(9648)
    assert a.costs["S"] == 450
    assert a.snapshot()["nav"] == "10098.00000000"
    balanced(a)


@pytest.mark.parametrize(
    "settlement,expected_cash,expected_stock", [("cash", "10750", "0"), ("physical", "-250", "100")]
)
def test_expiry_cash_or_physical_matches_economic_nav(settlement, expected_cash, expected_stock):
    a = account(settlement=settlement)
    event(a, "d", "deposit", amount="10000")
    event(a, "buy", "fill", instrument="O", quantity="1", price="2.5")
    e = event(
        a,
        "expiry",
        "exercise",
        instrument="O",
        fixing="110",
        fixing_at=EXPIRY,
        fixing_available_at=EXPIRY,
        at=EXPIRY,
    )
    assert a.cash == Decimal(expected_cash)
    assert a.positions["S"] == Decimal(expected_stock)
    assert a.positions["O"] == 0
    assert Decimal(a.snapshot()["nav"]) == 10750
    prior = a.journal()
    a.apply(e)
    assert a.journal() == prior
    with pytest.raises(ValueError, match="idempotency"):
        a.apply(dict(e, fixing="111"))
    balanced(a)


def test_short_assignment_requires_borrow_and_failure_rolls_back():
    a = account(settlement="physical")
    event(a, "sell", "fill", instrument="O", quantity="-1", price="3")
    before = deepcopy(a.__dict__)
    with pytest.raises(ValueError, match="borrow locate"):
        event(
            a,
            "expiry",
            "exercise",
            instrument="O",
            fixing="110",
            fixing_at=EXPIRY,
            fixing_available_at=EXPIRY,
            at=EXPIRY,
        )
    assert a.__dict__ == before
    event(a, "locate", "locate", instrument="S", quantity="100", expires_at=EXPIRY)
    event(
        a,
        "expire",
        "exercise",
        instrument="O",
        fixing="110",
        fixing_at=EXPIRY,
        fixing_available_at=EXPIRY,
        at=EXPIRY,
    )
    assert a.positions["S"] == -100
    assert Decimal(a.snapshot()["nav"]) == -700
    balanced(a)


def test_finance_dividend_ex_date_entitlement_and_margin_segregation():
    a = account()
    event(a, "d", "deposit", amount="10000")
    event(a, "loc", "locate", instrument="S", quantity="100", expires_at=EXPIRY)
    event(a, "s", "fill", instrument="S", quantity="-10", price="100")
    event(a, "m", "mark", prices={"S": "100"})
    event(a, "margin", "margin", requirement="2000", model="declared stress")
    assert Decimal(a.snapshot()["nav"]) == 10000
    end = "2026-01-03T12:00:00Z"
    event(
        a,
        "fund",
        "accrue",
        start=AT,
        at=end,
        credit_rate="0.0365",
        debit_rate="0.073",
        borrow_rates={"S": "0.365"},
    )
    assert a.cash == Decimal("8999.90")  # 0.90 interest - 1 borrow
    event(
        a,
        "ex",
        "dividend_ex",
        at=end,
        instrument="S",
        dividend_id="div",
        amount_per_share="2",
        pay_at=end,
    )
    event(a, "cover", "fill", at=end, instrument="S", quantity="10", price="98")
    event(a, "pay", "dividend_pay", at=end, dividend_id="div")
    event(a, "release", "margin", at=end, requirement="0", model="flat")
    assert Decimal(a.snapshot()["nav"]) == Decimal("9999.9")
    balanced(a)


def test_split_preserves_nav_and_changes_deliverable():
    a = account()
    event(a, "d", "deposit", amount="20000")
    event(a, "s", "fill", instrument="S", quantity="10", price="100")
    event(a, "o", "fill", instrument="O", quantity="1", price="4")
    event(a, "m", "mark", prices={"S": "100", "O": "4"})
    before = a.snapshot()["nav"]
    event(
        a,
        "split",
        "split",
        instrument="S",
        ratio="2",
        option_adjustments={"O": {"multiplier": "200", "strike": "50"}},
    )
    assert a.positions["S"] == 20
    assert a.snapshot()["nav"] == before
    balanced(a)


def test_margin_shortfall_stale_marks_and_future_events_reject():
    a = account()
    event(a, "d", "deposit", amount="100")
    event(a, "m", "margin", requirement="200", model="stress")
    assert a.snapshot()["margin_shortfall"] == "100.00000000"
    assert len(a.margin_calls) == 1
    before = deepcopy(a.__dict__)
    with pytest.raises(ValueError, match="not available"):
        a.apply({"id": "x", "type": "deposit", "amount": 100, "at": AT, "available_at": EXPIRY})
    assert a.__dict__ == before
    with pytest.raises(ValueError, match="borrow"):
        event(a, "short", "fill", instrument="S", quantity=-1, price=100)
    balanced(a)


def test_european_early_and_wrong_expiry_fixing_rejected():
    a = account(style="european")
    event(a, "b", "fill", instrument="O", quantity=1, price=2)
    with pytest.raises(ValueError, match="before expiry"):
        event(
            a, "early", "exercise", instrument="O", fixing=110, fixing_at=AT, fixing_available_at=AT
        )
    with pytest.raises(ValueError, match="exact expiry"):
        event(
            a,
            "late",
            "exercise",
            at=EXPIRY,
            instrument="O",
            fixing=110,
            fixing_at=AT,
            fixing_available_at=AT,
        )
