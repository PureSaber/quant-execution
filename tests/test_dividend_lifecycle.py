from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import (
    ROUND_DOWN,
    ROUND_HALF_EVEN,
    ROUND_HALF_UP,
    ROUND_UP,
    Decimal,
    Inexact,
    Rounded,
    localcontext,
)
from fractions import Fraction
from types import SimpleNamespace

import pytest
from conftest import fp, spec
from quant_data_kit import AssetClass, FixedPoint
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial import (
    CashDeduction,
    CurrencyReference,
    DividendEntitlement,
    DividendLifecycle,
    DividendPayment,
    DividendPaymentElection,
    EvidenceTiming,
    IssuerFxConversion,
    PaymentPolicy,
    PhaseEvidence,
    PitFxRate,
    PublishedAmount,
    RoundingPolicy,
)

import quant_execution.dividends as dividends_module
from quant_execution.contracts import LedgerEventType, LedgerTransaction, Posting
from quant_execution.dividends import (
    DividendEntitlementBasis,
    DividendExecutionMode,
    DividendExecutionPhase,
    DividendExecutionRequest,
    DividendValuationRecord,
    FxValuationMode,
    PitFxObservationRecord,
)
from quant_execution.ledger import ExactAccountLedger

UTC = timezone.utc
OPENED = datetime(2026, 1, 1, tzinfo=UTC)
EX_AT = datetime(2026, 1, 2, tzinfo=UTC)
STOCK = "HK:DIVIDEND"


def stamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def evidence(
    event_id: str,
    *,
    effective_at: datetime,
    available_at: datetime,
    captured_at: datetime | None = None,
) -> PhaseEvidence:
    captured = captured_at or available_at
    return PhaseEvidence(
        event_id=event_id,
        source="issuer-feed",
        evidence_id=f"evidence:{event_id}",
        timing=EvidenceTiming(
            effective_at=stamp(effective_at),
            available_at=stamp(available_at),
            captured_at=stamp(captured),
            source_published_at=stamp(available_at),
        ),
    )


def amount(
    value: str,
    source_units: str = "1",
    *,
    approximate: bool = False,
) -> PublishedAmount:
    return PublishedAmount(
        amount_text=value,
        source_unit_text=source_units,
        source_unit_name="share",
        published_decimal_places=len(value.partition(".")[2]),
        approximate=approximate,
    )


def currency(code: str) -> CurrencyReference:
    return CurrencyReference(
        source_label=code,
        calculation_currency=code,
        normalization_rule="identity",
    )


def entitlement(
    *,
    event_id: str = "entitlement",
    declared: str = "HKD",
    approved: str = "1.00",
    source_units: str = "1",
    available_at: datetime = EX_AT - timedelta(hours=1),
    captured_at: datetime = EX_AT + timedelta(days=1),
    options: tuple[str, ...] | None = None,
) -> DividendEntitlement:
    payment_options = options or (declared,)
    return DividendEntitlement(
        evidence=evidence(
            event_id,
            effective_at=EX_AT,
            available_at=available_at,
            captured_at=captured_at,
        ),
        approved_amount=amount(approved, source_units),
        declared_currency=currency(declared),
        record_date="2026-01-03",
        scheduled_payment_date="2026-01-10",
        payment_currencies=tuple(currency(item) for item in payment_options),
        default_payment_currency=payment_options[0],
    )


def election(
    payment_currency: str,
    *,
    policy_id: str = "policy-1",
    at: datetime = EX_AT + timedelta(days=1),
) -> DividendPaymentElection:
    return DividendPaymentElection(
        evidence=evidence("election", effective_at=at, available_at=at),
        account_id="account",
        account_policy_id=policy_id,
        payment_currency=payment_currency,
        selection_kind="explicit",
    )


def certified_policy(
    *,
    policy_id: str = "policy-1",
    decimal_places: int = 2,
    scope: str = "aggregate_account",
    at: datetime = EX_AT + timedelta(days=1),
) -> PaymentPolicy:
    return PaymentPolicy(
        policy_id=policy_id,
        account_id="account",
        certification_status="certified",
        holder_tax_profile_id="holder-tax-1",
        withholding_rule_id="withholding-rule-1",
        rounding=RoundingPolicy(
            scope=scope,
            decimal_places=decimal_places,
            mode="ROUND_HALF_UP",
            evidence_id="rounding-evidence",
        ),
        evidence=evidence("policy", effective_at=at, available_at=at),
    )


def issuer_conversion(
    *,
    source: str = "CNY",
    target: str = "HKD",
    rate: str = "0.9",
    published: str = "0.90",
    source_units: str = "1",
    approximate: bool = False,
    at: datetime = EX_AT + timedelta(days=1),
) -> IssuerFxConversion:
    return IssuerFxConversion(
        evidence=evidence("conversion", effective_at=at, available_at=at),
        from_currency=source,
        to_currency=target,
        rate_text=rate,
        rate_convention="quote_per_base",
        fixing_at=stamp(at),
        fixing_date=None,
        fixing_timezone=None,
        published_payment_amount=amount(
            published,
            source_units,
            approximate=approximate,
        ),
    )


def payment(
    *,
    gross: str,
    net: str,
    payment_currency: str = "HKD",
    withholding: str = "0",
    deductions: tuple[CashDeduction, ...] = (),
    rounding: str = "0",
    policy_id: str = "policy-1",
    at: datetime = EX_AT + timedelta(days=2),
) -> DividendPayment:
    return DividendPayment(
        evidence=evidence("payment", effective_at=at, available_at=at),
        account_id="account",
        payment_currency=payment_currency,
        policy_id=policy_id,
        gross_cash_text=gross,
        withholding_cash_text=withholding,
        deductions=deductions,
        rounding_adjustment_text=rounding,
        net_cash_text=net,
    )


def lifecycle(
    dividend_id: str = "dividend-ordinary",
    *,
    terms: DividendEntitlement | None = None,
    choice: DividendPaymentElection | None = None,
    conversion: IssuerFxConversion | None = None,
    policy: PaymentPolicy | None = None,
    paid: DividendPayment | None = None,
) -> DividendLifecycle:
    return DividendLifecycle(
        dividend_id=dividend_id,
        instrument_id=STOCK,
        entitlement=terms or entitlement(),
        election=choice,
        conversion=conversion,
        payment_policy=policy,
        payment=paid,
    )


def basis(
    dividend_id: str = "dividend-ordinary",
    *,
    quantity: FixedPoint | None = None,
    available_at: datetime = EX_AT - timedelta(hours=2),
    captured_at: datetime = EX_AT + timedelta(days=2),
    certification_ref: str | None = None,
) -> DividendEntitlementBasis:
    entitled_quantity = quantity if quantity is not None else FixedPoint(3, 0)
    return DividendEntitlementBasis(
        account_id="account",
        dividend_id=dividend_id,
        instrument_id=STOCK,
        ex_at=EX_AT,
        entitled_quantity=entitled_quantity,
        available_at=available_at,
        captured_at=captured_at,
        evidence_id=f"basis:{dividend_id}",
        evidence_source="custodian-statement",
        certification_ref=certification_ref,
    )


def ledger(
    *,
    mode: DividendExecutionMode = DividendExecutionMode.SCENARIO_ONLY,
    verifier=None,
    money_scale: int = 8,
) -> ExactAccountLedger:
    account = ExactAccountLedger(
        account_id="account",
        base_currency="HKD",
        instruments={
            STOCK: spec(
                STOCK,
                asset_class=AssetClass.EQUITY,
                product_type="cash_equity",
                settlement_currency="HKD",
            )
        },
        initial_cash={"HKD": fp("100")},
        money_scale=money_scale,
        opened_at=OPENED,
        dividend_execution_mode=mode,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
        entitlement_evidence_verifier=verifier,
    )
    account.book_opening_position(
        instrument_id=STOCK,
        quantity=FixedPoint(3, 0),
        average_cost=fp("10"),
        acquired_on=date(2025, 12, 31),
    )
    return account


def request(
    value: DividendLifecycle,
    phase: DividendExecutionPhase,
    *,
    cutoff: datetime,
    evidence_basis: DividendEntitlementBasis | None = None,
) -> DividendExecutionRequest:
    return DividendExecutionRequest(
        lifecycle=value,
        phase=phase,
        cutoff=cutoff,
        entitlement_basis=evidence_basis,
    )


def apply_entitlement(
    account: ExactAccountLedger,
    value: DividendLifecycle | None = None,
    evidence_basis: DividendEntitlementBasis | None = None,
):
    value = value or lifecycle()
    return account.apply_dividend_lifecycle(
        request(
            value,
            DividendExecutionPhase.ENTITLEMENT,
            cutoff=EX_AT,
            evidence_basis=evidence_basis or basis(value.dividend_id),
        )
    )


def same_currency_prefix(account: ExactAccountLedger) -> DividendLifecycle:
    initial = lifecycle()
    apply_entitlement(account, initial)
    value = lifecycle(
        choice=election("HKD"),
        policy=certified_policy(),
    )
    account.apply_dividend_lifecycle(
        request(
            value,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )
    return value


def pit_rate(
    event_id: str,
    *,
    base: str,
    rate: str,
    observed_at: datetime,
    available_at: datetime | None = None,
) -> PitFxRate:
    available = available_at or observed_at
    return PitFxRate(
        event_id=event_id,
        base_currency=base,
        quote_currency="HKD",
        rate_text=rate,
        rate_convention="quote_per_base",
        observed_at=stamp(observed_at),
        available_at=stamp(available),
        captured_at=stamp(max(available, EX_AT + timedelta(days=10))),
        source="fx-source",
        evidence_id=f"evidence:{event_id}",
    )


def test_entitlement_aggregates_fraction_before_exact_fixed_point_conversion() -> None:
    account = ledger()
    value = lifecycle(terms=entitlement(approved="0.10", source_units="3"))

    record = apply_entitlement(account, value)

    state = account._dividend_lifecycle_states[("account", value.dividend_id)]
    assert state.declared_gross == FixedPoint(10_000_000, 8)
    assert account.dividend_receivable_balance("HKD") == Decimal("0.10000000")
    assert record.transaction_ids
    assert account.snapshot(EX_AT).nav.to_decimal() == Decimal("130.10000000")


def test_nested_record_views_cannot_mutate_ledger_state_or_journal() -> None:
    account = ledger()
    record = apply_entitlement(account)
    state = account._dividend_lifecycle_states[("account", "dividend-ordinary")]
    journal_before = account.journal_sha256

    record_payload = record.to_dict()
    record_payload["lifecycle_snapshot"]["entitlement"]["approved_amount"]["amount_text"] = "999.00"
    state_payload = state.to_dict()
    state_payload["lifecycle_snapshot"]["entitlement"]["approved_amount"]["amount_text"] = "888.00"

    with pytest.raises(TypeError):
        record.lifecycle_snapshot["entitlement"]["approved_amount"]["amount_text"] = "777.00"
    with pytest.raises(TypeError):
        record.lifecycle_snapshot["entitlement"]["payment_currencies"][0] = {}

    assert (
        record.to_dict()["lifecycle_snapshot"]["entitlement"]["approved_amount"]["amount_text"]
        == "1.00"
    )
    assert (
        state.to_dict()["lifecycle_snapshot"]["entitlement"]["approved_amount"]["amount_text"]
        == "1.00"
    )
    assert account.dividend_receivable_balance("HKD") == Decimal("3.00000000")
    assert account.journal_sha256 == journal_before
    assert deepcopy(record) == record


def test_public_record_constructors_take_recursive_ownership_of_inputs() -> None:
    rate_payload = {"event_id": "rate", "nested": {"source": "before"}, "items": [1]}
    rate_record = PitFxObservationRecord(
        observation_sequence=0,
        rate_payload=rate_payload,
        rate_fingerprint="0" * 64,
        operation_sequence=0,
        transaction_count=0,
    )
    result_payload = {"nested": {"value": "before"}, "items": [{"value": 1}]}
    selected_rate = {"nested": {"value": "before"}}
    valuation_record = DividendValuationRecord(
        valuation_idempotency_key="valuation",
        as_of=EX_AT,
        execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
        selected_rates=(selected_rate,),
        result_payload=result_payload,
        result_sha256="1" * 64,
        operation_sequence=1,
        transaction_count=0,
    )

    rate_payload["nested"]["source"] = "after"
    rate_payload["items"].append(2)
    result_payload["nested"]["value"] = "after"
    result_payload["items"][0]["value"] = 2
    selected_rate["nested"]["value"] = "after"

    assert rate_record.to_dict()["rate_payload"] == {
        "event_id": "rate",
        "nested": {"source": "before"},
        "items": [1],
    }
    assert valuation_record.to_dict()["result_payload"] == {
        "nested": {"value": "before"},
        "items": [{"value": 1}],
    }
    assert valuation_record.to_dict()["selected_rates"] == [{"nested": {"value": "before"}}]
    with pytest.raises(TypeError):
        rate_record.rate_payload["nested"]["source"] = "blocked"
    with pytest.raises(TypeError):
        valuation_record.result_payload["items"][0]["value"] = 3


def test_dividend_contract_parsers_and_recursive_values_fail_closed() -> None:
    frozen = dividends_module._freeze({"items": [{"value": 1}]})
    assert dividends_module._freeze(frozen) is frozen
    assert len(frozen) == 1
    assert repr(frozen)
    sequence = frozen["items"]
    assert len(sequence) == 1
    assert sequence == [{"value": 1}]
    assert sequence != "not-a-sequence"
    assert repr(sequence)
    assert deepcopy(sequence) is sequence

    with pytest.raises(ValidationError, match="non-empty string"):
        dividends_module._text(" ", "value")
    with pytest.raises(ValidationError, match="ISO-8601 timestamp"):
        dividends_module._parse_timestamp(None, "value")
    for value, message in (
        (None, "fixed-point object"),
        ({"units": True, "scale": 1}, "units must be an integer"),
        ({"units": 1, "scale": True}, "scale must be an integer"),
    ):
        with pytest.raises(ValidationError, match=message):
            dividends_module._fixed_from_payload(value, "value")

    with pytest.raises(ValidationError, match="nonnegative FixedPoint"):
        replace(basis(), entitled_quantity="invalid")
    with pytest.raises(ValidationError, match="nonnegative FixedPoint"):
        replace(basis(), entitled_quantity=FixedPoint(-1, 0))
    with pytest.raises(ValidationError, match="entitlement_basis must be an object"):
        DividendEntitlementBasis.from_dict(None)

    with pytest.raises(ValidationError, match="lifecycle must be"):
        DividendExecutionRequest(
            lifecycle="invalid",
            phase=DividendExecutionPhase.ENTITLEMENT,
            cutoff=EX_AT,
        )
    with pytest.raises(ValidationError, match="phase must be"):
        DividendExecutionRequest(
            lifecycle=lifecycle(),
            phase="entitlement",
            cutoff=EX_AT,
        )
    with pytest.raises(ValidationError, match="entitlement_basis has an invalid type"):
        DividendExecutionRequest(
            lifecycle=lifecycle(),
            phase=DividendExecutionPhase.ENTITLEMENT,
            cutoff=EX_AT,
            entitlement_basis="invalid",
        )

    account = ledger()
    phase_record = apply_entitlement(account)
    state = account._dividend_lifecycle_states[("account", "dividend-ordinary")]
    for constructor, message in (
        (dividends_module.DividendLifecycleState.from_dict, "lifecycle state"),
        (dividends_module.DividendExecutionRecord.from_dict, "execution record"),
        (dividends_module.PitFxObservationRecord.from_dict, "observation record"),
        (dividends_module.DividendValuationRecord.from_dict, "valuation record"),
    ):
        with pytest.raises(ValidationError, match=message):
            constructor(None)
    assert dividends_module.DividendLifecycleState.from_dict(state.to_dict()) == state
    record_payload = phase_record.to_dict()
    with pytest.raises(ValidationError, match="unsupported dividend record schema"):
        dividends_module.DividendExecutionRecord.from_dict(
            {**record_payload, "schema": "unsupported"}
        )
    with pytest.raises(ValidationError, match="not a dividend phase application"):
        dividends_module.DividendExecutionRecord.from_dict(
            {**record_payload, "record_kind": "valuation"}
        )
    with pytest.raises(ValidationError, match="dividend record must be an object"):
        dividends_module.dividend_record_from_dict(None)
    with pytest.raises(ValidationError, match="unsupported dividend record kind"):
        dividends_module.dividend_record_from_dict({"record_kind": "unknown"})


def test_exact_dividend_numeric_helpers_cover_rounding_and_overflow_boundaries() -> None:
    with pytest.raises(ValidationError, match="decimal text"):
        dividends_module._fraction_text(None, "value")
    with pytest.raises(ValidationError, match="must be finite"):
        dividends_module._fraction_text("NaN", "value")
    with pytest.raises(ValidationError, match="NOT_EXACT"):
        dividends_module._fixed_exact(Fraction(1, 3), 2, "NOT_EXACT")
    with pytest.raises(ValidationError, match="FIXED_POINT_OVERFLOW"):
        dividends_module._fixed_exact(Fraction(2**63), 0, "NOT_EXACT")

    assert dividends_module._round_fraction(Fraction(1, 4), 1, ROUND_DOWN) == Fraction(1, 5)
    assert dividends_module._round_fraction(Fraction(-1, 4), 1, ROUND_UP) == Fraction(-3, 10)
    assert dividends_module._round_fraction(Fraction(1, 4), 1, ROUND_HALF_UP) == Fraction(3, 10)
    assert dividends_module._round_fraction(Fraction(1, 4), 1, ROUND_HALF_EVEN) == Fraction(1, 5)
    assert dividends_module._round_fraction(Fraction(3, 4), 1, ROUND_HALF_EVEN) == Fraction(4, 5)
    assert dividends_module._round_fraction(Fraction(1, 2), 1, "unused") == Fraction(1, 2)
    with pytest.raises(ValidationError, match="unsupported rounding mode"):
        dividends_module._round_fraction(Fraction(1, 4), 1, "unsupported")

    assert dividends_module._certified_rounding_policy(None, account_id="account") is None
    with pytest.raises(ValidationError, match="PAYMENT_POLICY_ACCOUNT_MISMATCH"):
        dividends_module._certified_rounding_policy(
            SimpleNamespace(account_id="other", certification_status="certified"),
            account_id="account",
        )
    assert (
        dividends_module._certified_rounding_policy(
            SimpleNamespace(account_id="account", certification_status="unverified"),
            account_id="account",
        )
        is None
    )
    with pytest.raises(ValidationError, match="ISSUER_CONVERSION_REQUIRED"):
        dividends_module._conversion_account_amount(lifecycle(), Fraction(1))
    with pytest.raises(ValidationError, match="ISSUER_CONVERSION_REQUIRED"):
        dividends_module._issuer_conversion_audit(lifecycle(), Fraction(1))


def test_dividend_phase_entry_guards_reject_invalid_mode_identity_order_and_cutoff() -> None:
    with pytest.raises(ValidationError, match="request must be"):
        ledger().apply_dividend_lifecycle("invalid")

    no_mode = ledger()
    no_mode._dividend_execution_mode = None
    with pytest.raises(ValidationError, match="DIVIDEND_EXECUTION_MODE_REQUIRED"):
        apply_entitlement(no_mode)

    legacy_fx = ledger()
    legacy_fx._fx_valuation_mode = FxValuationMode.LEGACY
    with pytest.raises(ValidationError, match="EVIDENCED_PIT_REQUIRED"):
        apply_entitlement(legacy_fx)

    with pytest.raises(ValidationError, match="UNKNOWN_DIVIDEND_INSTRUMENT"):
        apply_entitlement(
            ledger(),
            replace(lifecycle(), instrument_id="UNKNOWN"),
        )

    missing_basis = ledger()
    with pytest.raises(ValidationError, match="ENTITLEMENT_BASIS_REQUIRED"):
        missing_basis.apply_dividend_lifecycle(
            request(
                lifecycle(),
                DividendExecutionPhase.ENTITLEMENT,
                cutoff=EX_AT,
            )
        )

    identity_mismatch = ledger()
    with pytest.raises(ValidationError, match="ENTITLEMENT_BASIS_IDENTITY_MISMATCH"):
        apply_entitlement(
            identity_mismatch,
            evidence_basis=replace(basis(), account_id="other"),
        )

    cutoff_mismatch = ledger()
    early_entitlement = replace(
        entitlement(),
        evidence=evidence(
            "entitlement-early",
            effective_at=EX_AT - timedelta(hours=1),
            available_at=EX_AT - timedelta(hours=1),
        ),
    )
    with pytest.raises(ValidationError, match="ENTITLEMENT_CUTOFF_PRECEDES_EX_AT"):
        cutoff_mismatch.apply_dividend_lifecycle(
            request(
                lifecycle(terms=early_entitlement),
                DividendExecutionPhase.ENTITLEMENT,
                cutoff=EX_AT - timedelta(minutes=30),
                evidence_basis=basis(),
            )
        )

    no_parent = ledger()
    with pytest.raises(ValidationError, match="DIVIDEND_PHASE_ORDER_INVALID"):
        no_parent.apply_dividend_lifecycle(
            request(
                lifecycle(choice=election("HKD"), policy=certified_policy()),
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=EX_AT + timedelta(days=1),
            )
        )

    skipped_conversion = ledger()
    apply_entitlement(skipped_conversion)
    with pytest.raises(ValidationError, match="DIVIDEND_PHASE_ORDER_INVALID"):
        skipped_conversion.apply_dividend_lifecycle(
            request(
                lifecycle(
                    choice=election("HKD"),
                    policy=certified_policy(),
                    paid=payment(gross="3", net="3"),
                ),
                DividendExecutionPhase.PAYMENT,
                cutoff=EX_AT + timedelta(days=2),
            )
        )


def test_dividend_observation_and_valuation_type_guards_fail_closed() -> None:
    legacy = SimpleNamespace(fx_valuation_mode=FxValuationMode.LEGACY)
    with pytest.raises(ValidationError, match="EVIDENCED_PIT_REQUIRED"):
        dividends_module.observe_pit_fx(legacy, None)
    with pytest.raises(ValidationError, match="rate must be a PitFxRate"):
        ledger().observe_pit_fx("invalid")
    with pytest.raises(ValidationError, match="finite Decimal or FixedPoint"):
        dividends_module.convert_for_valuation(
            SimpleNamespace(fx_valuation_mode=FxValuationMode.EVIDENCED_PIT),
            "invalid",
            "HKD",
            EX_AT,
        )
    with pytest.raises(ValidationError, match="finite Decimal or FixedPoint"):
        dividends_module.convert_for_valuation(
            SimpleNamespace(fx_valuation_mode=FxValuationMode.EVIDENCED_PIT),
            Decimal("NaN"),
            "HKD",
            EX_AT,
        )
    with pytest.raises(ValidationError, match="EVIDENCED_PIT_REQUIRED"):
        dividends_module.record_dividend_valuation(legacy, as_of=EX_AT)

    zero_account = ledger()
    apply_entitlement(
        zero_account,
        lifecycle(terms=entitlement(approved="0")),
    )
    assert zero_account.dividend_exposure(as_of=EX_AT).items == ()


def test_aggregate_rounding_happens_once_after_account_quantity_is_known() -> None:
    account = ledger()
    value = lifecycle(
        terms=entitlement(approved="0.05", source_units="2"),
        policy=certified_policy(at=EX_AT - timedelta(minutes=1)),
    )

    apply_entitlement(account, value)

    state = account._dividend_lifecycle_states[("account", value.dividend_id)]
    assert state.declared_gross.to_decimal() == Decimal("0.08000000")
    assert state.declared_gross.to_decimal() != Decimal("0.09")


def test_stable_identity_allows_ordinary_and_special_same_day_and_is_idempotent() -> None:
    account = ledger()
    ordinary = lifecycle("ordinary")
    special = lifecycle("special", terms=entitlement(event_id="special-entitlement"))

    first = apply_entitlement(account, ordinary, basis("ordinary"))
    second = apply_entitlement(account, special, basis("special"))

    assert apply_entitlement(account, ordinary, basis("ordinary")) == first
    assert first.phase_fingerprint != second.phase_fingerprint
    assert account.dividend_receivable_balance("HKD") == Decimal("6.00000000")
    assert len(account._dividend_lifecycle_states) == 2


def test_same_phase_identity_conflict_rolls_back_everything() -> None:
    account = ledger()
    value = lifecycle()
    apply_entitlement(account, value)
    before = account.capture_state()
    changed = lifecycle(terms=entitlement(approved="2.00"))

    with pytest.raises(ValidationError, match="DIVIDEND_PHASE_ID_REUSED"):
        apply_entitlement(account, changed)

    assert account.capture_state() == before


def test_future_optional_fact_is_not_silently_trimmed_from_cutoff_snapshot() -> None:
    account = ledger()
    value = lifecycle(choice=election("HKD"))
    before = account.capture_state()

    with pytest.raises(ValidationError, match="FUTURE_DIVIDEND_FACT_IN_REQUEST"):
        apply_entitlement(account, value)

    assert account.capture_state() == before


@pytest.mark.parametrize(
    ("terms", "evidence_basis", "message"),
    [
        (
            entitlement(
                available_at=EX_AT + timedelta(seconds=1), captured_at=EX_AT + timedelta(days=1)
            ),
            basis(),
            "LATE_ENTITLEMENT_UNSUPPORTED",
        ),
        (
            entitlement(),
            basis(available_at=EX_AT + timedelta(seconds=1), captured_at=EX_AT + timedelta(days=1)),
            "LATE_ENTITLEMENT_UNSUPPORTED",
        ),
        (entitlement(), basis(quantity=FixedPoint(2, 0)), "ENTITLEMENT_BASIS_MISMATCH"),
    ],
)
def test_entitlement_timing_and_real_position_rejections_are_atomic(
    terms: DividendEntitlement,
    evidence_basis: DividendEntitlementBasis,
    message: str,
) -> None:
    account = ledger()
    before = account.capture_state()

    with pytest.raises(ValidationError, match=message):
        apply_entitlement(account, lifecycle(terms=terms), evidence_basis)

    assert account.capture_state() == before


def test_ledger_clock_cannot_move_back_to_ex_date() -> None:
    account = ledger()
    account._event_time = EX_AT + timedelta(seconds=1)
    before = account.capture_state()

    with pytest.raises(ValidationError, match="LEDGER_PASSED_EX_AT"):
        apply_entitlement(account)

    assert account.capture_state() == before


def test_production_mode_requires_independent_verifier_and_accepts_archived_capture() -> None:
    class Verifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return basis.certification_ref == "certified:historical" and bool(lifecycle.dividend_id)

    account = ledger(mode=DividendExecutionMode.PRODUCTION_CERTIFIED, verifier=Verifier())
    accepted = basis(certification_ref="certified:historical")
    assert accepted.available_at <= accepted.ex_at < accepted.captured_at
    assert apply_entitlement(account, evidence_basis=accepted)

    rejected = ledger(mode=DividendExecutionMode.PRODUCTION_CERTIFIED, verifier=Verifier())
    before = rejected.capture_state()
    with pytest.raises(ValidationError, match="ENTITLEMENT_EVIDENCE_NOT_CERTIFIED"):
        apply_entitlement(rejected, evidence_basis=basis(certification_ref="request-self-claim"))
    assert rejected.capture_state() == before


def test_production_policy_is_verified_when_first_used() -> None:
    class EntitlementOnlyVerifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return True

    account = ledger(
        mode=DividendExecutionMode.PRODUCTION_CERTIFIED,
        verifier=EntitlementOnlyVerifier(),
    )
    apply_entitlement(account, evidence_basis=basis(certification_ref="basis-certified"))
    before = account.capture_state()
    value = lifecycle(choice=election("HKD"), policy=certified_policy())

    with pytest.raises(ValidationError, match="PAYMENT_POLICY_EVIDENCE_NOT_CERTIFIED"):
        account.apply_dividend_lifecycle(
            request(
                value,
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=EX_AT + timedelta(days=1),
            )
        )

    assert account.capture_state() == before


def test_production_actual_payment_requires_separate_trusted_verification() -> None:
    class PolicyVerifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return True

        def verify_payment_policy(self, *, policy, lifecycle) -> bool:
            return policy.policy_id == "policy-1"

        def verify_dividend_payment(self, *, payment, lifecycle) -> bool:
            return False

    account = ledger(
        mode=DividendExecutionMode.PRODUCTION_CERTIFIED,
        verifier=PolicyVerifier(),
    )
    apply_entitlement(account, evidence_basis=basis(certification_ref="basis-certified"))
    prefix = lifecycle(choice=election("HKD"), policy=certified_policy())
    account.apply_dividend_lifecycle(
        request(
            prefix,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )
    before = account.capture_state()

    with pytest.raises(ValidationError, match="DIVIDEND_PAYMENT_EVIDENCE_NOT_CERTIFIED"):
        account.apply_dividend_lifecycle(
            request(
                replace(prefix, payment=payment(gross="3", net="3")),
                DividendExecutionPhase.PAYMENT,
                cutoff=EX_AT + timedelta(days=2),
            )
        )

    assert account.capture_state() == before


@pytest.mark.parametrize(
    "mode",
    [DividendExecutionMode.SCENARIO_ONLY, DividendExecutionMode.PRODUCTION_CERTIFIED],
)
def test_future_effective_policy_cannot_round_entitlement_before_ex(
    mode: DividendExecutionMode,
) -> None:
    class Verifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return True

        def verify_payment_policy(self, *, policy, lifecycle) -> bool:
            return True

    verifier = Verifier() if mode is DividendExecutionMode.PRODUCTION_CERTIFIED else None
    account = ledger(mode=mode, verifier=verifier)
    future_policy = replace(
        certified_policy(decimal_places=0),
        evidence=evidence(
            "future-policy",
            effective_at=EX_AT + timedelta(days=5),
            available_at=EX_AT - timedelta(hours=1),
            captured_at=EX_AT + timedelta(days=1),
        ),
    )
    value = lifecycle(
        terms=entitlement(approved="1.01"),
        policy=future_policy,
    )
    before = account.capture_state()

    with pytest.raises(ValidationError, match="DIVIDEND_FACT_NOT_EFFECTIVE"):
        account.apply_dividend_lifecycle(
            request(
                value,
                DividendExecutionPhase.ENTITLEMENT,
                cutoff=EX_AT + timedelta(days=10),
                evidence_basis=basis(certification_ref="basis-certified"),
            )
        )

    assert account.capture_state() == before
    assert account.dividend_receivable_balance("HKD") == 0


@pytest.mark.parametrize(
    "mode",
    [DividendExecutionMode.SCENARIO_ONLY, DividendExecutionMode.PRODUCTION_CERTIFIED],
)
def test_policy_learned_after_ex_cannot_rewrite_entitlement_receivable(
    mode: DividendExecutionMode,
) -> None:
    class Verifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return True

        def verify_payment_policy(self, *, policy, lifecycle) -> bool:
            return True

    verifier = Verifier() if mode is DividendExecutionMode.PRODUCTION_CERTIFIED else None
    account = ledger(mode=mode, verifier=verifier)
    learned_late_policy = replace(
        certified_policy(decimal_places=0),
        evidence=evidence(
            "late-policy",
            effective_at=EX_AT - timedelta(hours=1),
            available_at=EX_AT + timedelta(days=1),
            captured_at=EX_AT + timedelta(days=2),
        ),
    )
    value = lifecycle(
        terms=entitlement(approved="1.01"),
        policy=learned_late_policy,
    )
    before = account.capture_state()

    with pytest.raises(ValidationError, match="LATE_ENTITLEMENT_UNSUPPORTED"):
        account.apply_dividend_lifecycle(
            request(
                value,
                DividendExecutionPhase.ENTITLEMENT,
                cutoff=EX_AT + timedelta(days=10),
                evidence_basis=basis(certification_ref="basis-certified"),
            )
        )

    assert account.capture_state() == before
    assert account.dividend_receivable_balance("HKD") == 0


@pytest.mark.parametrize(
    "mode",
    [DividendExecutionMode.SCENARIO_ONLY, DividendExecutionMode.PRODUCTION_CERTIFIED],
)
def test_future_effective_election_cannot_advance_ledger_beyond_cutoff(
    mode: DividendExecutionMode,
) -> None:
    class Verifier:
        def verify_entitlement_basis(self, *, basis, lifecycle) -> bool:
            return True

        def verify_payment_policy(self, *, policy, lifecycle) -> bool:
            return True

    verifier = Verifier() if mode is DividendExecutionMode.PRODUCTION_CERTIFIED else None
    account = ledger(mode=mode, verifier=verifier)
    initial = lifecycle()
    apply_entitlement(
        account,
        initial,
        basis(certification_ref="basis-certified"),
    )
    cutoff = EX_AT + timedelta(days=1)
    future_election = replace(
        election("HKD"),
        evidence=evidence(
            "future-election",
            effective_at=EX_AT + timedelta(days=5),
            available_at=cutoff,
            captured_at=cutoff,
        ),
    )
    value = lifecycle(
        terms=initial.entitlement,
        choice=future_election,
        policy=certified_policy(at=cutoff),
    )
    before = account.capture_state()

    with pytest.raises(ValidationError, match="DIVIDEND_FACT_NOT_EFFECTIVE_AT_CUTOFF"):
        account.apply_dividend_lifecycle(
            request(
                value,
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=cutoff,
            )
        )

    assert account.capture_state() == before


def test_basis_capture_cannot_precede_availability() -> None:
    with pytest.raises(ValidationError, match="BASIS_CAPTURE_PRECEDES_AVAILABILITY"):
        basis(
            available_at=EX_AT - timedelta(hours=1),
            captured_at=EX_AT - timedelta(hours=2),
        )


def test_same_currency_election_has_no_fake_conversion_transaction() -> None:
    account = ledger()
    apply_entitlement(account)
    before = len(account.transactions)
    value = lifecycle(choice=election("HKD"), policy=certified_policy())

    record = account.apply_dividend_lifecycle(
        request(
            value,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )

    assert record.transaction_ids == ()
    assert len(account.transactions) == before


def test_issuer_conversion_uses_published_amount_and_balances_each_currency() -> None:
    account = ledger()
    initial = lifecycle(
        terms=entitlement(declared="CNY", options=("CNY", "HKD")),
    )
    apply_entitlement(account, initial)
    value = lifecycle(
        terms=initial.entitlement,
        choice=election("HKD"),
        conversion=issuer_conversion(),
        policy=certified_policy(),
    )

    record = account.apply_dividend_lifecycle(
        request(
            value,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )

    state = account._dividend_lifecycle_states[("account", value.dividend_id)]
    assert state.declared_gross.to_decimal() == Decimal("3.00000000")
    assert state.receivable_gross.to_decimal() == Decimal("2.70000000")
    assert record.issuer_conversion_audit["relationship_status"] == (
        "unverified_no_rounding_contract"
    )
    assert record.issuer_conversion_audit["account_published_minus_rate_implied"] == {
        "numerator": "0",
        "denominator": "1",
    }
    assert account.dividend_receivable_balance("CNY") == 0
    assert account.dividend_receivable_balance("HKD") == Decimal("2.70000000")
    transaction = account.transactions[-1]
    for code in ("CNY", "HKD"):
        assert (
            sum(
                posting.amount.units for posting in transaction.postings if posting.currency == code
            )
            == 0
        )


def test_issuer_conversion_preserves_unverified_rate_relationship_audit() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="CNY", options=("CNY", "HKD")))
    apply_entitlement(account, initial)
    inconsistent = lifecycle(
        terms=initial.entitlement,
        choice=election("HKD"),
        conversion=issuer_conversion(rate="0.8", published="0.90"),
        policy=certified_policy(),
    )

    record = account.apply_dividend_lifecycle(
        request(
            inconsistent,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )

    assert record.issuer_conversion_audit == {
        "relationship_status": "unverified_no_rounding_contract",
        "normalized_source_unit_name": "share",
        "declared_amount": initial.entitlement.approved_amount.to_dict(),
        "rate_text": "0.8",
        "published_payment_amount": inconsistent.conversion.published_payment_amount.to_dict(),
        "declared_per_unit": {"numerator": "1", "denominator": "1"},
        "rate_implied_payment_per_unit": {"numerator": "4", "denominator": "5"},
        "published_payment_per_unit": {"numerator": "9", "denominator": "10"},
        "published_minus_rate_implied": {"numerator": "1", "denominator": "10"},
        "account_quantity": {"numerator": "3", "denominator": "1"},
        "rate_implied_account_payment": {"numerator": "12", "denominator": "5"},
        "published_account_payment": {"numerator": "27", "denominator": "10"},
        "account_published_minus_rate_implied": {
            "numerator": "3",
            "denominator": "10",
        },
    }


def test_issuer_conversion_rejects_incomparable_source_units_atomically() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="CNY", options=("CNY", "HKD")))
    apply_entitlement(account, initial)
    conversion = issuer_conversion()
    conversion = replace(
        conversion,
        published_payment_amount=replace(
            conversion.published_payment_amount,
            source_unit_name="depositary receipt",
        ),
    )
    value = lifecycle(
        terms=initial.entitlement,
        choice=election("HKD"),
        conversion=conversion,
        policy=certified_policy(),
    )
    before = account.capture_state()

    with pytest.raises(ValidationError, match="ISSUER_CONVERSION_SOURCE_UNIT_MISMATCH"):
        account.apply_dividend_lifecycle(
            request(
                value,
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=EX_AT + timedelta(days=1),
            )
        )

    assert account.capture_state() == before


@pytest.mark.parametrize(
    ("approved", "approved_units", "rate", "published", "published_units", "approximate"),
    [
        ("0.10", "1", "7.776781", "0.777678", "1", True),
        ("1.858", "10", "1.1014865663", "2.04656204", "10", False),
    ],
)
def test_official_issuer_conversion_terms_do_not_invent_rounding_contract(
    approved: str,
    approved_units: str,
    rate: str,
    published: str,
    published_units: str,
    approximate: bool,
) -> None:
    account = ledger()
    initial = lifecycle(
        terms=entitlement(
            declared="CNY",
            approved=approved,
            source_units=approved_units,
            options=("CNY", "HKD"),
        )
    )
    apply_entitlement(account, initial)
    value = lifecycle(
        terms=initial.entitlement,
        choice=election("HKD"),
        conversion=issuer_conversion(
            rate=rate,
            published=published,
            source_units=published_units,
            approximate=approximate,
        ),
        policy=certified_policy(decimal_places=8),
    )

    record = account.apply_dividend_lifecycle(
        request(
            value,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )

    audit = record.issuer_conversion_audit
    assert audit is not None
    assert audit["relationship_status"] == "unverified_no_rounding_contract"
    assert audit["rate_text"] == rate
    assert audit["published_payment_amount"]["approximate"] is approximate
    assert audit["published_minus_rate_implied"] != {"numerator": "0", "denominator": "1"}


def test_prefix_change_is_rejected_without_state_change() -> None:
    account = ledger()
    apply_entitlement(account)
    before = account.capture_state()
    changed = lifecycle(
        terms=entitlement(approved="2.00"),
        choice=election("HKD"),
        policy=certified_policy(),
    )

    with pytest.raises(ValidationError, match="LIFECYCLE_PREFIX_CHANGED"):
        account.apply_dividend_lifecycle(
            request(
                changed,
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=EX_AT + timedelta(days=1),
            )
        )

    assert account.capture_state() == before


def test_unknown_tax_keeps_gross_exposure_but_payment_is_rejected() -> None:
    account = ledger()
    apply_entitlement(account)
    exposure = account.dividend_exposure(as_of=EX_AT)
    assert exposure.tax_status == "unknown"
    assert exposure.estimated_net_cash is None
    before = account.capture_state()
    with pytest.raises(ValueError, match="certified payment policy"):
        lifecycle(
            choice=election("HKD"),
            paid=payment(gross="3", net="3"),
        )

    assert account.capture_state() == before


def test_certified_zero_tax_payment_uses_original_entitlement_after_position_changes() -> None:
    account = ledger()
    prefix = same_currency_prefix(account)
    close = LedgerTransaction(
        transaction_id="tx:close-position",
        idempotency_key="close-position",
        event_time=EX_AT + timedelta(days=1, hours=1),
        event_type=LedgerEventType.SETTLEMENT,
        reference_id="close-position",
        postings=(
            Posting(
                ledger_account="assets:position",
                currency="HKD",
                amount=FixedPoint(0, 0),
                instrument_id=STOCK,
                quantity_delta=FixedPoint(-3, 0),
            ),
            Posting(
                ledger_account="memo:position_counter",
                currency="HKD",
                amount=FixedPoint(0, 0),
                instrument_id=STOCK,
                quantity_delta=FixedPoint(3, 0),
            ),
        ),
    )
    account._post(close)
    account._event_time = close.event_time
    paid = replace(prefix, payment=payment(gross="3", net="3"))

    account.apply_dividend_lifecycle(
        request(
            paid,
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )

    assert account._positions[STOCK] == 0
    assert account.cash_balance("HKD") == Decimal("103.00000000")
    assert account.dividend_receivable_balance("HKD") == 0


def test_payment_posts_withholding_deductions_and_signed_rounding_exactly() -> None:
    account = ledger()
    prefix = same_currency_prefix(account)
    fee_one = CashDeduction("fee-one", "0.10", "fee-evidence-1")
    fee_two = CashDeduction("fee-two", "0.20", "fee-evidence-2")
    paid = replace(
        prefix,
        payment=payment(
            gross="3.00",
            net="2.55",
            withholding="0.20",
            deductions=(fee_one, fee_two),
            rounding="-0.05",
        ),
    )

    account.apply_dividend_lifecycle(
        request(
            paid,
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )

    assert account.cash_balance("HKD") == Decimal("102.55000000")
    assert account._accounts[
        ("expenses:dividend_withholding", "HKD", "dividend:dividend-ordinary")
    ] == Decimal("0.20000000")
    assert account._accounts[
        ("expenses:dividend_rounding", "HKD", "dividend:dividend-ordinary")
    ] == Decimal("-0.05000000")


@pytest.mark.parametrize(
    ("policy", "money_scale", "message"),
    [
        (None, 2, "ROUNDING_POLICY_REQUIRED"),
        (
            certified_policy(scope="per_source_unit", at=EX_AT - timedelta(minutes=1)),
            8,
            "UNSUPPORTED_ROUNDING_SCOPE",
        ),
        (
            certified_policy(decimal_places=9, at=EX_AT - timedelta(minutes=1)),
            8,
            "LEDGER_SCALE_TOO_COARSE",
        ),
    ],
)
def test_rounding_rejections_are_explicit_and_atomic(
    policy, money_scale: int, message: str
) -> None:
    account = ledger(money_scale=money_scale)
    terms = entitlement(approved="0.10", source_units="3")
    evidence_basis = basis(quantity=FixedPoint(2, 0))
    account._positions[STOCK] = Decimal(2)
    value = lifecycle(terms=terms, policy=policy)
    before = account.capture_state()

    with pytest.raises(ValidationError, match=message):
        apply_entitlement(account, value, evidence_basis)

    assert account.capture_state() == before


def test_pit_fx_missing_future_ambiguous_and_unique_selection() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="USD", options=("USD",)))
    apply_entitlement(account, initial)
    with pytest.raises(ValidationError, match="PIT_FX_UNAVAILABLE"):
        account.snapshot(EX_AT)

    account.observe_pit_fx(
        pit_rate(
            "future",
            base="USD",
            rate="7.8",
            observed_at=EX_AT + timedelta(hours=1),
        )
    )
    with pytest.raises(ValidationError, match="PIT_FX_UNAVAILABLE"):
        account.snapshot(EX_AT)

    observed = EX_AT - timedelta(minutes=1)
    account.observe_pit_fx(pit_rate("a", base="USD", rate="7.8", observed_at=observed))
    account.observe_pit_fx(pit_rate("b", base="USD", rate="7.9", observed_at=observed))
    with pytest.raises(ValidationError, match="ambiguous"):
        account.snapshot(EX_AT)

    unique = ledger()
    apply_entitlement(unique, initial)
    unique.observe_pit_fx(pit_rate("only", base="USD", rate="7.8", observed_at=observed))
    assert unique.snapshot(EX_AT).nav.to_decimal() == Decimal("153.40000000")


def test_evidenced_pit_rejects_legacy_fx_mutation() -> None:
    account = ledger()
    with pytest.raises(ValidationError, match="EVIDENCED_PIT_REJECTS_LEGACY_FX"):
        account.set_fx_rate("USD", FixedPoint(78, 1), event_time=EX_AT)


def test_pit_fx_event_id_conflict_is_rejected_without_changing_observation_order() -> None:
    account = ledger()
    observed = EX_AT - timedelta(minutes=1)
    account.observe_pit_fx(pit_rate("same", base="USD", rate="7.8", observed_at=observed))
    before = account.capture_state()

    with pytest.raises(ValidationError, match="PIT_FX_EVENT_ID_CONFLICT"):
        account.observe_pit_fx(pit_rate("same", base="USD", rate="7.9", observed_at=observed))

    assert account.capture_state() == before


def test_historical_fx_availability_does_not_create_historical_ledger_state() -> None:
    account = ledger()
    prefix = same_currency_prefix(account)
    account.apply_dividend_lifecycle(
        request(
            replace(prefix, payment=payment(gross="3", net="3")),
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )

    with pytest.raises(ValidationError, match="HISTORICAL_LEDGER_STATE_UNAVAILABLE"):
        account.snapshot(EX_AT + timedelta(days=1))


def test_usd_payment_keeps_evidenced_pit_across_cash_nav_risk_and_liquidation() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="USD", options=("USD",)))
    apply_entitlement(account, initial)
    observed = EX_AT - timedelta(minutes=1)
    account.observe_pit_fx(pit_rate("usd-hkd", base="USD", rate="7.8", observed_at=observed))
    prefix = lifecycle(
        terms=initial.entitlement,
        choice=election("USD"),
        policy=certified_policy(),
    )
    account.apply_dividend_lifecycle(
        request(
            prefix,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )
    paid = replace(
        prefix,
        payment=payment(gross="3", net="3", payment_currency="USD"),
    )
    account.apply_dividend_lifecycle(
        request(
            paid,
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )
    at = EX_AT + timedelta(days=2)

    snapshot = account.snapshot(at)
    _, _, risk_nav, _ = account.risk_balances(at)
    portfolio = account.portfolio_risk_snapshot(at)
    account.assert_nav_residual(snapshot)

    assert account.cash_balance("USD") == Decimal("3.00000000")
    assert account.convert_to_base(Decimal(3), "USD", event_time=at) == Decimal("23.4")
    assert snapshot.nav.to_decimal() == Decimal("153.40000000")
    assert risk_nav == Decimal("153.400000000")
    assert portfolio.nav == snapshot.nav
    assert account.liquidation_required(at) is False


def test_payment_without_pit_fx_preserves_cash_but_all_valuation_paths_fail() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="USD", options=("USD",)))
    apply_entitlement(account, initial)
    prefix = lifecycle(
        terms=initial.entitlement,
        choice=election("USD"),
        policy=certified_policy(),
    )
    account.apply_dividend_lifecycle(
        request(
            prefix,
            DividendExecutionPhase.ISSUER_CONVERSION,
            cutoff=EX_AT + timedelta(days=1),
        )
    )
    account.apply_dividend_lifecycle(
        request(
            replace(
                prefix,
                payment=payment(gross="3", net="3", payment_currency="USD"),
            ),
            DividendExecutionPhase.PAYMENT,
            cutoff=EX_AT + timedelta(days=2),
        )
    )
    at = EX_AT + timedelta(days=2)
    assert account.cash_balance("USD") == Decimal("3.00000000")

    for query in (
        lambda: account.snapshot(at),
        lambda: account.risk_balances(at),
        lambda: account.portfolio_risk_snapshot(at),
        lambda: account.liquidation_required(at),
        lambda: account.convert_to_base(Decimal(3), "USD", event_time=at),
    ):
        with pytest.raises(ValidationError, match="PIT_FX_UNAVAILABLE"):
            query()


def test_payment_preserves_economic_and_knowledge_time_separately() -> None:
    account = ledger()
    prefix = same_currency_prefix(account)
    effective = EX_AT + timedelta(days=2)
    available = EX_AT + timedelta(days=5)
    late_payment = DividendPayment(
        evidence=evidence(
            "late-payment",
            effective_at=effective,
            available_at=available,
        ),
        account_id="account",
        payment_currency="HKD",
        policy_id="policy-1",
        gross_cash_text="3",
        withholding_cash_text="0",
        deductions=(),
        rounding_adjustment_text="0",
        net_cash_text="3",
    )
    record = account.apply_dividend_lifecycle(
        request(
            replace(prefix, payment=late_payment),
            DividendExecutionPhase.PAYMENT,
            cutoff=available,
        )
    )

    assert record.economic_effective_at == effective
    assert record.available_at == available
    assert record.applied_at == available
    assert account._event_time == available


def test_payment_gross_and_accepted_election_conflicts_roll_back() -> None:
    gross_account = ledger()
    gross_prefix = same_currency_prefix(gross_account)
    gross_before = gross_account.capture_state()
    with pytest.raises(ValidationError, match="PAYMENT_GROSS_MISMATCH"):
        gross_account.apply_dividend_lifecycle(
            request(
                replace(gross_prefix, payment=payment(gross="2", net="2")),
                DividendExecutionPhase.PAYMENT,
                cutoff=EX_AT + timedelta(days=2),
            )
        )
    assert gross_account.capture_state() == gross_before

    policy_account = ledger()
    same_currency_prefix(policy_account)
    policy_before = policy_account.capture_state()
    changed = lifecycle(
        choice=election("HKD", policy_id="policy-2"),
        policy=certified_policy(policy_id="policy-2"),
        paid=payment(gross="3", net="3", policy_id="policy-2"),
    )
    with pytest.raises(ValidationError, match="LIFECYCLE_PREFIX_CHANGED"):
        policy_account.apply_dividend_lifecycle(
            request(
                changed,
                DividendExecutionPhase.PAYMENT,
                cutoff=EX_AT + timedelta(days=2),
            )
        )
    assert policy_account.capture_state() == policy_before


def test_fixed_point_overflow_is_explicit_and_atomic() -> None:
    account = ledger()
    huge = lifecycle(terms=entitlement(approved="1000000000000.00"))
    before = account.capture_state()

    with pytest.raises(ValidationError, match="FIXED_POINT_OVERFLOW"):
        apply_entitlement(account, huge)

    assert account.capture_state() == before


def test_read_only_queries_do_not_change_records_state_or_journal() -> None:
    account = ledger()
    apply_entitlement(account)
    before_state = account.capture_state()
    before_hash = account.journal_sha256

    account.snapshot(EX_AT)
    account.dividend_exposure(as_of=EX_AT)
    account.risk_balances(EX_AT)

    assert account.capture_state() == before_state
    assert account.journal_sha256 == before_hash


def test_explicit_valuation_is_idempotent_and_conflicts_after_new_fx_fact() -> None:
    account = ledger()
    initial = lifecycle(terms=entitlement(declared="USD", options=("USD",)))
    apply_entitlement(account, initial)
    observed = EX_AT - timedelta(minutes=1)
    account.observe_pit_fx(pit_rate("first", base="USD", rate="7.8", observed_at=observed))

    first = account.record_dividend_valuation(as_of=EX_AT)
    assert account.record_dividend_valuation(as_of=EX_AT) == first
    account.observe_pit_fx(pit_rate("newer", base="USD", rate="7.9", observed_at=EX_AT))
    with pytest.raises(ValidationError, match="VALUATION_IDEMPOTENCY_CONFLICT"):
        account.record_dividend_valuation(as_of=EX_AT)


@pytest.mark.parametrize("precision", [2, 6, 28, 80])
def test_dividend_lifecycle_is_independent_of_decimal_context(precision: int) -> None:
    with localcontext() as context:
        context.prec = precision
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        account = ledger()
        value = lifecycle(terms=entitlement(approved="0.10", source_units="3"))
        apply_entitlement(account, value)
        prefix = lifecycle(
            terms=value.entitlement,
            choice=election("HKD"),
            policy=certified_policy(),
        )
        account.apply_dividend_lifecycle(
            request(
                prefix,
                DividendExecutionPhase.ISSUER_CONVERSION,
                cutoff=EX_AT + timedelta(days=1),
            )
        )
        account.apply_dividend_lifecycle(
            request(
                replace(prefix, payment=payment(gross="0.10", net="0.10")),
                DividendExecutionPhase.PAYMENT,
                cutoff=EX_AT + timedelta(days=2),
            )
        )
        snapshot = account.snapshot(EX_AT + timedelta(days=2))
        risk = account.portfolio_risk_snapshot(EX_AT + timedelta(days=2))

    assert account.dividend_receivable_balance("HKD") == 0
    assert account.cash_balance("HKD") == Decimal("100.10000000")
    assert snapshot.nav.to_decimal() == Decimal("130.10000000")
    assert risk.nav == snapshot.nav


def test_phase_failure_after_post_restores_complete_state(monkeypatch) -> None:
    account = ledger()
    before = account.capture_state()
    original = account._post

    def fail_after_post(transaction, *, local_rollback=True):
        original(transaction, local_rollback=local_rollback)
        raise RuntimeError("injected phase failure")

    monkeypatch.setattr(account, "_post", fail_after_post)
    with pytest.raises(RuntimeError, match="injected phase failure"):
        apply_entitlement(account)

    assert account.capture_state() == before


def test_zero_entitlement_keeps_phase_identity_without_zero_transaction() -> None:
    account = ledger()
    account._positions[STOCK] = Decimal(0)
    before = len(account.transactions)
    zero_basis = basis(quantity=FixedPoint(0, 0))

    record = apply_entitlement(account, evidence_basis=zero_basis)

    assert record.transaction_ids == ()
    assert len(account.transactions) == before
    assert record.resulting_state_sha256
    assert account.journal_sha256


def test_negative_and_derivative_positions_are_explicitly_unsupported() -> None:
    negative = ledger()
    negative._positions[STOCK] = Decimal(-3)
    with pytest.raises(ValidationError, match="NEGATIVE_DIVIDEND_POSITION_UNSUPPORTED"):
        apply_entitlement(negative)

    derivative_spec = spec(
        STOCK,
        asset_class=AssetClass.FUTURE,
        product_type="future",
        settlement_currency="HKD",
    )
    derivative = ExactAccountLedger(
        account_id="account",
        base_currency="HKD",
        instruments={STOCK: derivative_spec},
        opened_at=OPENED,
        dividend_execution_mode=DividendExecutionMode.SCENARIO_ONLY,
        fx_valuation_mode=FxValuationMode.EVIDENCED_PIT,
    )
    derivative._positions[STOCK] = Decimal(3)
    with pytest.raises(ValidationError, match="UNSUPPORTED_DERIVATIVE_DIVIDEND"):
        apply_entitlement(derivative)
