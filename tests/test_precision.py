from __future__ import annotations

from decimal import Decimal, Inexact, Rounded, localcontext

import pytest
from conftest import T0, fp, spec
from quant_data_kit import AssetClass, FixedPoint
from quant_data_kit.exceptions import ValidationError

from quant_execution._fixed import decimal, fixed
from quant_execution.artifacts import ledger_transaction_bytes
from quant_execution.contracts import LedgerEventType, LedgerTransaction, Posting
from quant_execution.ledger import ExactAccountLedger

STOCK = "HK:TEST"


def ledger(*, cash: str = "100") -> ExactAccountLedger:
    instrument = spec(
        STOCK,
        asset_class=AssetClass.EQUITY,
        product_type="cash_equity",
        settlement_currency="HKD",
    )
    return ExactAccountLedger(
        account_id="precision-account",
        base_currency="HKD",
        instruments={STOCK: instrument},
        initial_cash={"HKD": fp(cash)},
    )


def cash_transaction(reference: str, amount: FixedPoint) -> LedgerTransaction:
    return LedgerTransaction(
        transaction_id=f"tx:{reference}",
        idempotency_key=f"precision:{reference}",
        event_time=T0,
        event_type=LedgerEventType.FEE,
        reference_id=reference,
        postings=(
            Posting(ledger_account="assets:cash", currency="HKD", amount=amount),
            Posting(
                ledger_account="equity:precision-counter",
                currency="HKD",
                amount=FixedPoint(-amount.units, amount.scale),
            ),
        ),
    )


def position_transaction(reference: str, quantity: FixedPoint) -> LedgerTransaction:
    zero = FixedPoint(0, 0)
    return LedgerTransaction(
        transaction_id=f"tx:{reference}",
        idempotency_key=f"precision:{reference}",
        event_time=T0,
        event_type=LedgerEventType.CORPORATE_ACTION,
        reference_id=reference,
        postings=(
            Posting(
                ledger_account="assets:position",
                currency="HKD",
                amount=zero,
                instrument_id=STOCK,
                quantity_delta=quantity,
            ),
            Posting(
                ledger_account="memo:position_counter",
                currency="HKD",
                amount=zero,
                instrument_id=STOCK,
                quantity_delta=FixedPoint(-quantity.units, quantity.scale),
            ),
        ),
    )


@pytest.mark.parametrize("local_rollback", [False, True])
def test_post_adds_small_cash_exactly_under_low_precision(local_rollback: bool) -> None:
    account = ledger()
    transaction = cash_transaction("cent", FixedPoint(1, 2))

    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        account._post(transaction, local_rollback=local_rollback)

    assert account.cash_balance("HKD") == Decimal("100.01")
    assert ledger_transaction_bytes(account.transactions[-1]) == ledger_transaction_bytes(
        transaction
    )


def test_fixed_conversion_is_exact_and_call_order_cannot_pollute_results() -> None:
    value = Decimal("100.01")
    results = []
    for precision in (2, 28, 80, 2):
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            results.append(fixed(value, 2, rounding=None))

    assert results == [FixedPoint(10001, 2)] * 4
    assert decimal(results[0]) == value
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        with pytest.raises(ValidationError, match="not exact"):
            fixed(Decimal("100.001"), 2, rounding=None)
    assert fixed(Decimal("1.005"), 2).units == 100
    assert fixed(Decimal("1.015"), 2).units == 102
    with pytest.raises(ValidationError, match="FixedPoint"):
        decimal(Decimal(1))  # type: ignore[arg-type]


def test_post_accumulates_signed_mixed_scales_and_unit_quantities_exactly() -> None:
    account = ledger()
    operations = (
        cash_transaction("plus-cent", FixedPoint(1, 2)),
        cash_transaction("minus-mills", FixedPoint(-2, 3)),
        position_transaction("one-unit", FixedPoint(1, 0)),
        position_transaction("one-hundredth", FixedPoint(1, 2)),
        position_transaction("minus-one-thousandth", FixedPoint(-1, 3)),
    )

    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        for transaction in operations:
            account._post(transaction)

    assert account.cash_balance("HKD") == Decimal("100.008")
    assert account._positions[STOCK] == Decimal("1.009")


def test_precision_and_traps_do_not_change_post_bytes_balances_or_journal() -> None:
    observations = []
    for precision in (2, 6, 28, 80):
        account = ledger()
        transaction = cash_transaction("stable", FixedPoint(1, 2))
        with localcontext() as context:
            context.prec = precision
            context.traps[Inexact] = True
            context.traps[Rounded] = True
            account._post(transaction)
        observations.append(
            (
                account.cash_balance("HKD"),
                ledger_transaction_bytes(account.transactions[-1]),
                account.journal_sha256,
            )
        )

    assert observations == [observations[0]] * len(observations)


def test_failed_stream_append_restores_successful_prior_post_and_all_new_state() -> None:
    class FailingSink:
        def append(self, stream: str, payload: bytes) -> None:
            assert stream == "ledger_transactions"
            assert payload
            raise RuntimeError("injected artifact append failure")

    account = ledger()
    account._post(cash_transaction("committed", FixedPoint(1, 2)))
    before = account.capture_state()
    before_hash = account.journal_sha256
    failing = LedgerTransaction(
        transaction_id="tx:failing",
        idempotency_key="precision:failing",
        event_time=T0,
        event_type=LedgerEventType.CORPORATE_ACTION,
        reference_id="failing",
        postings=(
            Posting(
                ledger_account="assets:cash",
                currency="HKD",
                amount=FixedPoint(1, 3),
            ),
            Posting(
                ledger_account="equity:precision-counter",
                currency="HKD",
                amount=FixedPoint(-1, 3),
            ),
            Posting(
                ledger_account="assets:position",
                currency="HKD",
                amount=FixedPoint(0, 0),
                instrument_id=STOCK,
                quantity_delta=FixedPoint(1, 3),
            ),
        ),
    )
    account._artifact_sink = FailingSink()

    with (
        localcontext() as context,
        pytest.raises(RuntimeError, match="injected artifact append failure"),
    ):
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        account._post(failing)

    account._artifact_sink = None
    assert account.capture_state() == before
    assert account.journal_sha256 == before_hash
    assert account.cash_balance("HKD") == Decimal("100.01")
    assert STOCK not in account._positions
