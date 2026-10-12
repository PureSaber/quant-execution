from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from conftest import T0, spec
from quant_data_kit import AssetClass, MarginMode

from quant_execution.continuous import SharedCapitalAccount


def account(allocations=None):
    specs = [
        spec(i, asset_class=AssetClass.ETF, product_type="cash_etf", settlement_currency=c)
        for i, c in [("A", "USD"), ("B", "USD"), ("E", "EUR")]
    ]
    specs.append(
        spec(
            "F",
            asset_class=AssetClass.FUTURE,
            product_type="linear_future",
            settlement_currency="USD",
            margin_mode=MarginMode.CROSS,
            metadata={"initial_margin_rate": "0.2", "maintenance_margin_rate": "0.1"},
        )
    )
    return SharedCapitalAccount(
        specs=specs,
        owners={"A": "alpha", "B": "beta", "E": "beta", "F": "beta"},
        allocations=allocations or {"alpha": {"USD": "600"}, "beta": {"USD": "400"}},
        base_currency="USD",
        opened_at=T0.isoformat(),
        fx_rates={"EUR": "1.2"},
    )


def event(n, kind, **kwargs):
    at = (T0 + timedelta(seconds=n)).isoformat()
    return dict(event_id=f"e{n}", at=at, available_at=at, sequence=n, kind=kind, **kwargs)


def fill(n, sleeve="alpha", instrument="A", quantity="5", price="100", fee="1", delay=0):
    return event(
        n,
        "fill",
        sleeve=sleeve,
        instrument_id=instrument,
        quantity=quantity,
        price=price,
        fee=fee,
        settles_at=(T0 + timedelta(seconds=n + delay)).isoformat(),
    )


def test_shared_capital_competes_and_transfers_are_neutral():
    a = account()
    a.apply(fill(1))  # 600 - 500 - 1 = 99, not 1000 of independent buying power.
    original = a.report()
    with pytest.raises(ValueError, match="insufficient"):
        a.apply(fill(2, "beta", "B"))
    assert a.report() == original
    a.apply(event(3, "transfer", source="alpha", target="beta", currency="USD", amount="99"))
    assert a.ledger.cash_balance("USD") == Decimal(499)
    with pytest.raises(ValueError, match="insufficient"):
        a.apply(fill(4, "beta", "B"))  # 501 cannot be funded by combined remaining 499.
    a.apply(fill(5, "beta", "B", quantity="4"))
    report = a.report()
    assert Decimal(report["net_pnl"]) == -2
    assert a.ledger.cash_balance("USD") == 98
    assert report["reconciliation"]["cash_residual"]["USD"] == "0E-8"


def test_settlement_hold_units_and_cash_prevent_reinvestment_until_maturity():
    a = account()
    a.apply(fill(1, delay=3))
    with pytest.raises(ValueError, match="unsettled units"):
        a.apply(fill(2, quantity="-5", price="110"))
    a.apply(fill(4, quantity="-5", price="110", delay=4))
    # Cash is 648, but only 98 settled. Pending proceeds cannot fund another sleeve.
    assert a.ledger.cash_balance("USD") == 1048
    with pytest.raises(ValueError, match="unfunded"):
        a.apply(event(5, "transfer", source="alpha", target="beta", currency="USD", amount="100"))
    a.apply(event(8, "settle"))
    a.apply(event(9, "transfer", source="alpha", target="beta", currency="USD", amount="600"))
    a.apply(fill(10, "beta", "B", quantity="9"))
    assert Decimal(a.report()["net_pnl"]) == 47


def test_fx_futures_and_independent_decimal_conservation():
    a = account()
    events = [
        fill(1),
        event(2, "mark", instrument_id="A", price="110"),
        event(
            3,
            "fx_convert",
            sleeve="beta",
            source_currency="USD",
            target_currency="EUR",
            source_amount="120",
            target_amount="100",
        ),
        fill(4, "beta", "E", quantity="2", price="20", fee="1"),
        event(5, "fx_mark", currency="EUR", rate="1.3"),
        fill(6, "beta", "F", quantity="2", price="100", fee="2"),
        event(7, "mark", instrument_id="F", price="105"),
        event(8, "variation_margin", sleeve="beta", instrument_id="F", price="105", amount="10"),
        event(9, "external_cash", sleeve="alpha", currency="USD", amount="50"),
        event(10, "charge", sleeve="beta", instrument_id="F", amount="3"),
    ]
    result = a.replay(events)
    # Independent scalar cash bookkeeping: USD=1000-501-120-2+10+50-3=434,
    # EUR=100-40-1=59, spot A=550; E=40 EUR; futures reset after +10 settlement.
    expected = Decimal(434) + Decimal(59 + 40) * Decimal("1.3") + 550
    assert a.ledger.cash_balance("USD") == 434
    assert a.ledger.cash_balance("EUR") == 59
    assert Decimal(result["final_account"]["nav"]["units"]) / 10**8 == expected
    assert Decimal(result["net_pnl"]) == expected - 1000 - 50 == Decimal("62.7")
    for tx in result["ledger"]:
        for currency in ("USD", "EUR"):
            assert (
                sum(p["amount"]["units"] for p in tx["postings"] if p["currency"] == currency) == 0
            )
    resumed = account()
    resumed.replay(events[:5])
    checkpoint = resumed.checkpoint()
    restored = account()
    restored.restore(checkpoint)
    assert restored.replay(events[5:]) == result
    assert account().replay(list(reversed(events))) == result


def test_margin_call_keeps_loss_blocks_increase_allows_reduction_and_deposit():
    a = account()
    a.apply(fill(1, "beta", "F", quantity="10", fee="0"))
    a.apply(event(2, "mark", instrument_id="F", price="60"))
    assert a.report()["history"][-1]["margin_call"]
    assert Decimal(a.report()["net_pnl"]) == -400
    before = a.report()
    with pytest.raises(ValueError, match="insufficient"):
        a.apply(fill(3, "beta", "F", quantity="1", price="60", fee="0"))
    assert a.report() == before
    a.apply(fill(4, "beta", "F", quantity="-5", price="60", fee="0"))
    a.apply(event(5, "external_cash", sleeve="beta", currency="USD", amount="100"))
    assert not a.report()["history"][-1]["margin_call"]


def test_idempotency_ordering_availability_and_checkpoint_identity():
    a = account()
    e = fill(1)
    a.apply(e)
    before = a.report()
    a.apply(e)
    assert a.report() == before
    with pytest.raises(ValueError, match="changed content"):
        a.apply({**e, "fee": "2"})
    with pytest.raises(ValueError, match="strictly increasing"):
        a.apply({**e, "event_id": "different"})
    with pytest.raises(ValueError, match="availability"):
        a.apply({**fill(2), "available_at": (T0 + timedelta(seconds=3)).isoformat()})
    checkpoint = deepcopy(a.checkpoint())
    checkpoint["events"][0]["price"] = "99"
    with pytest.raises(ValueError, match="integrity"):
        account().restore(checkpoint)
    with pytest.raises(ValueError, match="configuration"):
        account({"alpha": {"USD": "601"}, "beta": {"USD": "399"}}).restore(a.checkpoint())
    with pytest.raises(ValueError, match="another sleeve"):
        a.apply(fill(3, "beta", "A", quantity="1"))


def test_initial_shortfall_is_not_maintenance_call_and_partial_transfer_helps():
    a = account({"alpha": {"USD": "750"}, "beta": {"USD": "250"}})
    a.apply(fill(1, "beta", "F", quantity="10", fee="0"))
    a.apply(event(2, "mark", instrument_id="F", price="90"))
    assert a.report()["history"][-1]["initial_margin_shortfall"]
    assert not a.report()["history"][-1]["margin_call"]
    assert a.liquidity("beta")["USD"] == -30
    a.apply(event(3, "transfer", source="alpha", target="beta", currency="USD", amount="10"))
    assert a.liquidity("beta")["USD"] == -20
    assert a.ledger.cash_balance("USD") == 1000
    assert Decimal(a.report()["net_pnl"]) == -100


def test_closed_events_exact_cash_precision_and_atomic_restore():
    from quant_data_kit.exceptions import ValidationError

    from quant_execution.continuous import digest

    a = account()
    for invalid in [
        {**event(1, "settle"), "ignored": "bad"},
        event(1, "external_cash", sleeve="alpha", currency="USD", amount="0.000000001"),
    ]:
        original = a.report()
        with pytest.raises((ValueError, ValidationError)):
            a.apply(invalid)
        assert a.report() == original
    checkpoint = a.checkpoint()
    checkpoint["events"] = [fill(1), fill(2, quantity="100")]
    checkpoint["sha256"] = digest({k: v for k, v in checkpoint.items() if k != "sha256"})
    original = a.report()
    with pytest.raises(ValueError):
        a.restore(checkpoint)
    assert a.report() == original


def test_native_futures_loss_is_reserved_before_fx_reporting_rounding():
    instrument = spec(
        "F",
        asset_class=AssetClass.FUTURE,
        product_type="linear_future",
        settlement_currency="EUR",
        price_tick="0.00000001",
        margin_mode=MarginMode.CROSS,
        metadata={"initial_margin_rate": "0.2", "maintenance_margin_rate": "0.1"},
    )
    a = SharedCapitalAccount(
        specs=[instrument],
        owners={"F": "beta"},
        allocations={"beta": {"EUR": "100"}},
        base_currency="USD",
        opened_at=T0.isoformat(),
        fx_rates={"EUR": "0.1"},
    )
    a.apply(fill(1, "beta", "F", quantity="1", price="100", fee="0"))
    a.apply(event(2, "mark", instrument_id="F", price="99.99999999"))
    # Native loss is 1e-8 EUR, even though its 1e-9 USD report rounds to zero.
    expected = Decimal(100) - Decimal("99.99999999") * Decimal("0.2") - Decimal("0.00000001")
    assert a.liquidity("beta")["EUR"] == expected == Decimal("79.999999992")


@pytest.mark.parametrize(
    "changes",
    [
        {"quantity": "0"},
        {"quantity": "0.5"},
        {"price": "0"},
        {"price": "1.001"},
        {"fee": "-1"},
        {"price": True},
        {"quantity": "NaN"},
        {"settles_at": T0.isoformat()},
        {"sequence": True},
    ],
)
def test_bad_fill_is_atomic(changes):
    a = account()
    original = a.report()
    with pytest.raises((ValueError, TypeError)):
        a.apply({**fill(1), **changes})
    assert a.report() == original
