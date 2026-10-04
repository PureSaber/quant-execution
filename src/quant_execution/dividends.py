"""Evidence-bound dividend lifecycle execution for exact account ledgers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import ROUND_DOWN, ROUND_HALF_EVEN, ROUND_HALF_UP, ROUND_UP, Decimal
from enum import Enum
from fractions import Fraction
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from quant_data_kit import FixedPoint, ensure_utc_datetime
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial import (
    DividendLifecycle,
    PaymentPolicy,
    PitFxRate,
    RoundingPolicy,
    select_pit_fx,
)

from quant_execution._fixed import (
    decimal,
    decimal_fraction,
    fraction_decimal_exact,
    sum_decimal_exact,
)
from quant_execution.contracts import LedgerEventType, LedgerTransaction, Posting

DIVIDEND_RECORD_SCHEMA_ID = "puresaber.execution.dividend-record/1"
DIVIDEND_JOURNAL_SCHEMA_ID = "puresaber.ledger-journal/2"
UTC = timezone.utc


class _FrozenMapping(Mapping[str, object]):
    __slots__ = ("_values",)

    def __init__(self, values: Mapping[str, object]) -> None:
        self._values = MappingProxyType({str(key): _freeze(value) for key, value in values.items()})

    def __getitem__(self, key: str) -> object:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __deepcopy__(self, memo: dict[int, object]):
        memo[id(self)] = self
        return self

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Mapping) and _thaw(self) == _thaw(other)

    def __repr__(self) -> str:
        return repr(self._values)


class _FrozenSequence(Sequence[object]):
    __slots__ = ("_values",)

    def __init__(self, values: Sequence[object]) -> None:
        self._values = tuple(_freeze(value) for value in values)

    def __getitem__(self, index):
        return self._values[index]

    def __len__(self) -> int:
        return len(self._values)

    def __deepcopy__(self, memo: dict[int, object]):
        memo[id(self)] = self
        return self

    def __eq__(self, other: object) -> bool:
        return (
            isinstance(other, Sequence)
            and not isinstance(other, (str, bytes, bytearray))
            and _thaw(self) == _thaw(other)
        )

    def __repr__(self) -> str:
        return repr(self._values)


def _freeze(value: object) -> object:
    if isinstance(value, _FrozenMapping | _FrozenSequence):
        return value
    if isinstance(value, Mapping):
        return _FrozenMapping(value)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return _FrozenSequence(value)
    return value


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_thaw(item) for item in value]
    return value


class DividendExecutionMode(str, Enum):
    SCENARIO_ONLY = "scenario_only"
    PRODUCTION_CERTIFIED = "production_certified"


class FxValuationMode(str, Enum):
    LEGACY = "legacy"
    EVIDENCED_PIT = "evidenced_pit"


class DividendExecutionPhase(str, Enum):
    ENTITLEMENT = "entitlement"
    ISSUER_CONVERSION = "issuer_conversion"
    PAYMENT = "payment"


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be a non-empty string")
    return value.strip()


def _utc(value: datetime, field: str) -> datetime:
    return ensure_utc_datetime(value, field=field)


def _timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise ValidationError(f"{field} must be an ISO-8601 timestamp") from exc
    return _utc(parsed, field)


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256(value: object) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _fixed_payload(value: FixedPoint) -> dict[str, int]:
    return {"units": value.units, "scale": value.scale}


def _fixed_from_payload(value: object, field: str) -> FixedPoint:
    if not isinstance(value, Mapping) or set(value) != {"units", "scale"}:
        raise ValidationError(f"{field} must be a fixed-point object")
    units = value["units"]
    scale = value["scale"]
    if isinstance(units, bool) or not isinstance(units, int):
        raise ValidationError(f"{field}.units must be an integer")
    if isinstance(scale, bool) or not isinstance(scale, int):
        raise ValidationError(f"{field}.scale must be an integer")
    return FixedPoint(units, scale)


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendEntitlementBasis:
    account_id: str
    dividend_id: str
    instrument_id: str
    ex_at: datetime
    entitled_quantity: FixedPoint
    available_at: datetime
    captured_at: datetime
    evidence_id: str
    evidence_source: str
    certification_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "account_id", _text(self.account_id, "account_id"))
        object.__setattr__(self, "dividend_id", _text(self.dividend_id, "dividend_id"))
        object.__setattr__(self, "instrument_id", _text(self.instrument_id, "instrument_id"))
        object.__setattr__(self, "ex_at", _utc(self.ex_at, "ex_at"))
        object.__setattr__(self, "available_at", _utc(self.available_at, "available_at"))
        object.__setattr__(self, "captured_at", _utc(self.captured_at, "captured_at"))
        if not isinstance(self.entitled_quantity, FixedPoint) or self.entitled_quantity.units < 0:
            raise ValidationError("entitled_quantity must be a nonnegative FixedPoint")
        if self.available_at > self.captured_at:
            raise ValidationError("BASIS_CAPTURE_PRECEDES_AVAILABILITY")
        object.__setattr__(self, "evidence_id", _text(self.evidence_id, "evidence_id"))
        object.__setattr__(self, "evidence_source", _text(self.evidence_source, "evidence_source"))
        if self.certification_ref is not None:
            object.__setattr__(
                self,
                "certification_ref",
                _text(self.certification_ref, "certification_ref"),
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "dividend_id": self.dividend_id,
            "instrument_id": self.instrument_id,
            "ex_at": _timestamp(self.ex_at),
            "entitled_quantity": _fixed_payload(self.entitled_quantity),
            "available_at": _timestamp(self.available_at),
            "captured_at": _timestamp(self.captured_at),
            "evidence_id": self.evidence_id,
            "evidence_source": self.evidence_source,
            "certification_ref": self.certification_ref,
        }

    @classmethod
    def from_dict(cls, value: object) -> DividendEntitlementBasis:
        if not isinstance(value, Mapping):
            raise ValidationError("entitlement_basis must be an object")
        return cls(
            account_id=value["account_id"],
            dividend_id=value["dividend_id"],
            instrument_id=value["instrument_id"],
            ex_at=_parse_timestamp(value["ex_at"], "ex_at"),
            entitled_quantity=_fixed_from_payload(value["entitled_quantity"], "entitled_quantity"),
            available_at=_parse_timestamp(value["available_at"], "available_at"),
            captured_at=_parse_timestamp(value["captured_at"], "captured_at"),
            evidence_id=value["evidence_id"],
            evidence_source=value["evidence_source"],
            certification_ref=value.get("certification_ref"),
        )


@runtime_checkable
class EntitlementEvidenceVerifier(Protocol):
    """Trusted production boundary for entitlement, policy, and payment evidence."""

    def verify_entitlement_basis(
        self,
        *,
        basis: DividendEntitlementBasis,
        lifecycle: DividendLifecycle,
    ) -> bool: ...

    def verify_payment_policy(
        self,
        *,
        policy: PaymentPolicy,
        lifecycle: DividendLifecycle,
    ) -> bool: ...

    def verify_dividend_payment(
        self,
        *,
        payment: object,
        lifecycle: DividendLifecycle,
    ) -> bool: ...


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendExecutionRequest:
    lifecycle: DividendLifecycle
    phase: DividendExecutionPhase
    cutoff: datetime
    entitlement_basis: DividendEntitlementBasis | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.lifecycle, DividendLifecycle):
            raise ValidationError("lifecycle must be a DividendLifecycle")
        if not isinstance(self.phase, DividendExecutionPhase):
            raise ValidationError("phase must be a DividendExecutionPhase")
        object.__setattr__(self, "cutoff", _utc(self.cutoff, "cutoff"))
        if self.entitlement_basis is not None and not isinstance(
            self.entitlement_basis, DividendEntitlementBasis
        ):
            raise ValidationError("entitlement_basis has an invalid type")


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendLifecycleState:
    account_id: str
    dividend_id: str
    instrument_id: str
    execution_mode: DividendExecutionMode
    fx_valuation_mode: FxValuationMode
    entitlement_basis: DividendEntitlementBasis
    ledger_quantity_at_ex: FixedPoint
    lifecycle_snapshot: Mapping[str, object]
    declared_currency: str
    declared_gross: FixedPoint
    receivable_currency: str
    receivable_gross: FixedPoint
    payment_policy_id: str | None
    phases: tuple[str, ...]
    phase_fingerprints: tuple[tuple[str, str], ...]
    transaction_ids: tuple[str, ...]
    paid: bool
    state_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle_snapshot", _freeze(self.lifecycle_snapshot))

    def to_dict(self, *, include_hash: bool = True) -> dict[str, object]:
        payload: dict[str, object] = {
            "account_id": self.account_id,
            "dividend_id": self.dividend_id,
            "instrument_id": self.instrument_id,
            "execution_mode": self.execution_mode.value,
            "fx_valuation_mode": self.fx_valuation_mode.value,
            "entitlement_basis": self.entitlement_basis.to_dict(),
            "ledger_quantity_at_ex": _fixed_payload(self.ledger_quantity_at_ex),
            "lifecycle_snapshot": _thaw(self.lifecycle_snapshot),
            "declared_currency": self.declared_currency,
            "declared_gross": _fixed_payload(self.declared_gross),
            "receivable_currency": self.receivable_currency,
            "receivable_gross": _fixed_payload(self.receivable_gross),
            "payment_policy_id": self.payment_policy_id,
            "phases": list(self.phases),
            "phase_fingerprints": [list(item) for item in self.phase_fingerprints],
            "transaction_ids": list(self.transaction_ids),
            "paid": self.paid,
        }
        if include_hash:
            payload["state_sha256"] = self.state_sha256
        return payload

    @classmethod
    def from_dict(cls, value: object) -> DividendLifecycleState:
        if not isinstance(value, Mapping):
            raise ValidationError("dividend lifecycle state must be an object")
        return cls(
            account_id=value["account_id"],
            dividend_id=value["dividend_id"],
            instrument_id=value["instrument_id"],
            execution_mode=DividendExecutionMode(value["execution_mode"]),
            fx_valuation_mode=FxValuationMode(value["fx_valuation_mode"]),
            entitlement_basis=DividendEntitlementBasis.from_dict(value["entitlement_basis"]),
            ledger_quantity_at_ex=_fixed_from_payload(
                value["ledger_quantity_at_ex"], "ledger_quantity_at_ex"
            ),
            lifecycle_snapshot=dict(value["lifecycle_snapshot"]),
            declared_currency=value["declared_currency"],
            declared_gross=_fixed_from_payload(value["declared_gross"], "declared_gross"),
            receivable_currency=value["receivable_currency"],
            receivable_gross=_fixed_from_payload(value["receivable_gross"], "receivable_gross"),
            payment_policy_id=value["payment_policy_id"],
            phases=tuple(value["phases"]),
            phase_fingerprints=tuple(tuple(item) for item in value["phase_fingerprints"]),
            transaction_ids=tuple(value["transaction_ids"]),
            paid=value["paid"],
            state_sha256=value["state_sha256"],
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendExecutionRecord:
    account_id: str
    dividend_id: str
    instrument_id: str
    phase: DividendExecutionPhase
    phase_event_id: str
    cutoff: datetime
    economic_effective_at: datetime
    available_at: datetime
    applied_at: datetime
    lifecycle_snapshot: Mapping[str, object]
    lifecycle_snapshot_sha256: str
    phase_fingerprint: str
    parent_state_sha256: str
    resulting_state_sha256: str
    execution_mode: DividendExecutionMode
    fx_valuation_mode: FxValuationMode
    entitlement_basis: DividendEntitlementBasis | None
    issuer_conversion_audit: Mapping[str, object] | None
    transaction_ids: tuple[str, ...]
    operation_sequence: int
    transaction_count_before: int
    transaction_count_after: int
    schema: str = DIVIDEND_RECORD_SCHEMA_ID
    record_kind: str = "phase_application"

    def __post_init__(self) -> None:
        object.__setattr__(self, "lifecycle_snapshot", _freeze(self.lifecycle_snapshot))
        object.__setattr__(
            self,
            "issuer_conversion_audit",
            (
                _freeze(self.issuer_conversion_audit)
                if self.issuer_conversion_audit is not None
                else None
            ),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "record_kind": self.record_kind,
            "account_id": self.account_id,
            "dividend_id": self.dividend_id,
            "instrument_id": self.instrument_id,
            "phase": self.phase.value,
            "phase_event_id": self.phase_event_id,
            "cutoff": _timestamp(self.cutoff),
            "economic_effective_at": _timestamp(self.economic_effective_at),
            "available_at": _timestamp(self.available_at),
            "applied_at": _timestamp(self.applied_at),
            "lifecycle_snapshot": _thaw(self.lifecycle_snapshot),
            "lifecycle_snapshot_sha256": self.lifecycle_snapshot_sha256,
            "phase_fingerprint": self.phase_fingerprint,
            "parent_state_sha256": self.parent_state_sha256,
            "resulting_state_sha256": self.resulting_state_sha256,
            "execution_mode": self.execution_mode.value,
            "fx_valuation_mode": self.fx_valuation_mode.value,
            "entitlement_basis": (
                self.entitlement_basis.to_dict() if self.entitlement_basis is not None else None
            ),
            "issuer_conversion_audit": (
                _thaw(self.issuer_conversion_audit)
                if self.issuer_conversion_audit is not None
                else None
            ),
            "transaction_ids": list(self.transaction_ids),
            "operation_sequence": self.operation_sequence,
            "transaction_count_before": self.transaction_count_before,
            "transaction_count_after": self.transaction_count_after,
        }

    @classmethod
    def from_dict(cls, value: object) -> DividendExecutionRecord:
        if not isinstance(value, Mapping):
            raise ValidationError("dividend execution record must be an object")
        if value.get("schema") != DIVIDEND_RECORD_SCHEMA_ID:
            raise ValidationError("unsupported dividend record schema")
        if value.get("record_kind") != "phase_application":
            raise ValidationError("record is not a dividend phase application")
        basis = value["entitlement_basis"]
        return cls(
            account_id=value["account_id"],
            dividend_id=value["dividend_id"],
            instrument_id=value["instrument_id"],
            phase=DividendExecutionPhase(value["phase"]),
            phase_event_id=value["phase_event_id"],
            cutoff=_parse_timestamp(value["cutoff"], "cutoff"),
            economic_effective_at=_parse_timestamp(
                value["economic_effective_at"], "economic_effective_at"
            ),
            available_at=_parse_timestamp(value["available_at"], "available_at"),
            applied_at=_parse_timestamp(value["applied_at"], "applied_at"),
            lifecycle_snapshot=dict(value["lifecycle_snapshot"]),
            lifecycle_snapshot_sha256=value["lifecycle_snapshot_sha256"],
            phase_fingerprint=value["phase_fingerprint"],
            parent_state_sha256=value["parent_state_sha256"],
            resulting_state_sha256=value["resulting_state_sha256"],
            execution_mode=DividendExecutionMode(value["execution_mode"]),
            fx_valuation_mode=FxValuationMode(value["fx_valuation_mode"]),
            entitlement_basis=(
                DividendEntitlementBasis.from_dict(basis) if basis is not None else None
            ),
            issuer_conversion_audit=(
                dict(value["issuer_conversion_audit"])
                if value["issuer_conversion_audit"] is not None
                else None
            ),
            transaction_ids=tuple(value["transaction_ids"]),
            operation_sequence=value["operation_sequence"],
            transaction_count_before=value["transaction_count_before"],
            transaction_count_after=value["transaction_count_after"],
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class PitFxObservationRecord:
    observation_sequence: int
    rate_payload: Mapping[str, object]
    rate_fingerprint: str
    operation_sequence: int
    transaction_count: int
    schema: str = DIVIDEND_RECORD_SCHEMA_ID
    record_kind: str = "pit_fx_observation"

    def __post_init__(self) -> None:
        object.__setattr__(self, "rate_payload", _freeze(self.rate_payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "record_kind": self.record_kind,
            "observation_sequence": self.observation_sequence,
            "rate_payload": _thaw(self.rate_payload),
            "rate_fingerprint": self.rate_fingerprint,
            "operation_sequence": self.operation_sequence,
            "transaction_count": self.transaction_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> PitFxObservationRecord:
        if not isinstance(value, Mapping):
            raise ValidationError("PIT FX observation record must be an object")
        return cls(
            observation_sequence=value["observation_sequence"],
            rate_payload=dict(value["rate_payload"]),
            rate_fingerprint=value["rate_fingerprint"],
            operation_sequence=value["operation_sequence"],
            transaction_count=value["transaction_count"],
        )


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendExposureItem:
    dividend_id: str
    instrument_id: str
    currency: str
    gross_receivable: FixedPoint
    base_value: FixedPoint
    tax_status: str

    def to_dict(self) -> dict[str, object]:
        return {
            "dividend_id": self.dividend_id,
            "instrument_id": self.instrument_id,
            "currency": self.currency,
            "gross_receivable": _fixed_payload(self.gross_receivable),
            "base_value": _fixed_payload(self.base_value),
            "tax_status": self.tax_status,
        }


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendExposureSnapshot:
    account_id: str
    as_of: datetime
    execution_mode: DividendExecutionMode
    fx_valuation_mode: FxValuationMode
    items: tuple[DividendExposureItem, ...]
    gross_nav: FixedPoint
    tax_status: str
    estimated_net_cash: FixedPoint | None

    def to_dict(self) -> dict[str, object]:
        return {
            "account_id": self.account_id,
            "as_of": _timestamp(self.as_of),
            "execution_mode": self.execution_mode.value,
            "fx_valuation_mode": self.fx_valuation_mode.value,
            "items": [item.to_dict() for item in self.items],
            "gross_nav": _fixed_payload(self.gross_nav),
            "tax_status": self.tax_status,
            "estimated_net_cash": (
                _fixed_payload(self.estimated_net_cash)
                if self.estimated_net_cash is not None
                else None
            ),
        }


@dataclass(frozen=True, kw_only=True, slots=True)
class DividendValuationRecord:
    valuation_idempotency_key: str
    as_of: datetime
    execution_mode: DividendExecutionMode
    fx_valuation_mode: FxValuationMode
    selected_rates: tuple[Mapping[str, object], ...]
    result_payload: Mapping[str, object]
    result_sha256: str
    operation_sequence: int
    transaction_count: int
    schema: str = DIVIDEND_RECORD_SCHEMA_ID
    record_kind: str = "valuation"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "selected_rates",
            tuple(_freeze(item) for item in self.selected_rates),
        )
        object.__setattr__(self, "result_payload", _freeze(self.result_payload))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": self.schema,
            "record_kind": self.record_kind,
            "valuation_idempotency_key": self.valuation_idempotency_key,
            "as_of": _timestamp(self.as_of),
            "execution_mode": self.execution_mode.value,
            "fx_valuation_mode": self.fx_valuation_mode.value,
            "selected_rates": [_thaw(item) for item in self.selected_rates],
            "result_payload": _thaw(self.result_payload),
            "result_sha256": self.result_sha256,
            "operation_sequence": self.operation_sequence,
            "transaction_count": self.transaction_count,
        }

    @classmethod
    def from_dict(cls, value: object) -> DividendValuationRecord:
        if not isinstance(value, Mapping):
            raise ValidationError("dividend valuation record must be an object")
        return cls(
            valuation_idempotency_key=value["valuation_idempotency_key"],
            as_of=_parse_timestamp(value["as_of"], "as_of"),
            execution_mode=DividendExecutionMode(value["execution_mode"]),
            fx_valuation_mode=FxValuationMode(value["fx_valuation_mode"]),
            selected_rates=tuple(dict(item) for item in value["selected_rates"]),
            result_payload=dict(value["result_payload"]),
            result_sha256=value["result_sha256"],
            operation_sequence=value["operation_sequence"],
            transaction_count=value["transaction_count"],
        )


DividendRecord = DividendExecutionRecord | PitFxObservationRecord | DividendValuationRecord


def dividend_record_bytes(record: DividendRecord) -> bytes:
    return canonical_bytes(record.to_dict())


def dividend_record_from_dict(value: object) -> DividendRecord:
    if not isinstance(value, Mapping):
        raise ValidationError("dividend record must be an object")
    kind = value.get("record_kind")
    if kind == "phase_application":
        return DividendExecutionRecord.from_dict(value)
    if kind == "pit_fx_observation":
        return PitFxObservationRecord.from_dict(value)
    if kind == "valuation":
        return DividendValuationRecord.from_dict(value)
    raise ValidationError("unsupported dividend record kind")


def _fraction_text(value: str, field: str) -> Fraction:
    try:
        parsed = Decimal(value)
    except Exception as exc:
        raise ValidationError(f"{field} must be decimal text") from exc
    if not parsed.is_finite():
        raise ValidationError(f"{field} must be finite")
    return decimal_fraction(parsed)


def _fixed_exact(value: Fraction, scale: int, code: str) -> FixedPoint:
    scaled = value * 10**scale
    if scaled.denominator != 1:
        raise ValidationError(code)
    try:
        return FixedPoint(scaled.numerator, scale)
    except (OverflowError, ValueError, ValidationError) as exc:
        raise ValidationError("FIXED_POINT_OVERFLOW") from exc


def _round_fraction(value: Fraction, decimal_places: int, mode: str) -> Fraction:
    scaled = value * 10**decimal_places
    sign = -1 if scaled < 0 else 1
    absolute = abs(scaled)
    quotient, remainder = divmod(absolute.numerator, absolute.denominator)
    if remainder:
        if mode == ROUND_DOWN:
            increment = False
        elif mode == ROUND_UP:
            increment = True
        elif mode == ROUND_HALF_UP:
            increment = remainder * 2 >= absolute.denominator
        elif mode == ROUND_HALF_EVEN:
            doubled = remainder * 2
            increment = doubled > absolute.denominator or (
                doubled == absolute.denominator and quotient % 2 == 1
            )
        else:
            raise ValidationError("unsupported rounding mode")
        if increment:
            quotient += 1
    return Fraction(sign * quotient, 10**decimal_places)


def _certified_rounding_policy(
    policy: PaymentPolicy | None,
    *,
    account_id: str,
) -> RoundingPolicy | None:
    if policy is None:
        return None
    if policy.account_id != account_id:
        raise ValidationError("PAYMENT_POLICY_ACCOUNT_MISMATCH")
    if policy.certification_status != "certified":
        return None
    return policy.rounding


def _account_money(
    value: Fraction,
    *,
    money_scale: int,
    policy: RoundingPolicy | None,
) -> FixedPoint:
    if policy is not None:
        if policy.scope != "aggregate_account":
            raise ValidationError("UNSUPPORTED_ROUNDING_SCOPE")
        if policy.decimal_places > money_scale:
            raise ValidationError("LEDGER_SCALE_TOO_COARSE")
        value = _round_fraction(value, policy.decimal_places, policy.mode)
    return _fixed_exact(value, money_scale, "ROUNDING_POLICY_REQUIRED")


def _published_account_amount(
    lifecycle: DividendLifecycle, quantity: FixedPoint | Fraction
) -> Fraction:
    amount = lifecycle.entitlement.approved_amount
    exact_quantity = quantity if isinstance(quantity, Fraction) else decimal_fraction(quantity)
    return (
        _fraction_text(amount.amount_text, "approved amount")
        / _fraction_text(amount.source_unit_text, "approved source unit")
        * exact_quantity
    )


def _conversion_account_amount(
    lifecycle: DividendLifecycle, quantity: FixedPoint | Fraction
) -> Fraction:
    conversion = lifecycle.conversion
    if conversion is None:
        raise ValidationError("ISSUER_CONVERSION_REQUIRED")
    amount = conversion.published_payment_amount
    exact_quantity = quantity if isinstance(quantity, Fraction) else decimal_fraction(quantity)
    return (
        _fraction_text(amount.amount_text, "published payment amount")
        / _fraction_text(amount.source_unit_text, "published payment source unit")
        * exact_quantity
    )


def _fraction_payload(value: Fraction) -> dict[str, str]:
    return {
        "numerator": str(value.numerator),
        "denominator": str(value.denominator),
    }


def _issuer_conversion_audit(
    lifecycle: DividendLifecycle,
    account_quantity: Fraction,
) -> dict[str, object]:
    conversion = lifecycle.conversion
    if conversion is None:
        raise ValidationError("ISSUER_CONVERSION_REQUIRED")
    declared = lifecycle.entitlement.approved_amount
    published = conversion.published_payment_amount
    if declared.source_unit_name != published.source_unit_name:
        raise ValidationError("ISSUER_CONVERSION_SOURCE_UNIT_MISMATCH")
    declared_per_unit = _fraction_text(declared.amount_text, "approved amount") / _fraction_text(
        declared.source_unit_text, "approved source unit"
    )
    rate = _fraction_text(conversion.rate_text, "issuer FX rate")
    rate_implied = declared_per_unit * rate
    published_per_unit = _fraction_text(
        published.amount_text, "published payment amount"
    ) / _fraction_text(published.source_unit_text, "published payment source unit")
    rate_implied_account = rate_implied * account_quantity
    published_account = published_per_unit * account_quantity
    return {
        "relationship_status": "unverified_no_rounding_contract",
        "normalized_source_unit_name": declared.source_unit_name,
        "declared_amount": declared.to_dict(),
        "rate_text": conversion.rate_text,
        "published_payment_amount": published.to_dict(),
        "declared_per_unit": _fraction_payload(declared_per_unit),
        "rate_implied_payment_per_unit": _fraction_payload(rate_implied),
        "published_payment_per_unit": _fraction_payload(published_per_unit),
        "published_minus_rate_implied": _fraction_payload(published_per_unit - rate_implied),
        "account_quantity": _fraction_payload(account_quantity),
        "rate_implied_account_payment": _fraction_payload(rate_implied_account),
        "published_account_payment": _fraction_payload(published_account),
        "account_published_minus_rate_implied": _fraction_payload(
            published_account - rate_implied_account
        ),
    }


def _require_trusted_payment_policy(ledger, lifecycle: DividendLifecycle) -> None:
    if (
        ledger.dividend_execution_mode is not DividendExecutionMode.PRODUCTION_CERTIFIED
        or lifecycle.payment_policy is None
    ):
        return
    verify = getattr(ledger._entitlement_evidence_verifier, "verify_payment_policy", None)
    if not callable(verify) or not verify(
        policy=lifecycle.payment_policy,
        lifecycle=lifecycle,
    ):
        raise ValidationError("PAYMENT_POLICY_EVIDENCE_NOT_CERTIFIED")


def _require_trusted_actual_payment(ledger, lifecycle: DividendLifecycle) -> None:
    if ledger.dividend_execution_mode is not DividendExecutionMode.PRODUCTION_CERTIFIED:
        return
    verify = getattr(ledger._entitlement_evidence_verifier, "verify_dividend_payment", None)
    if not callable(verify) or not verify(
        payment=lifecycle.payment,
        lifecycle=lifecycle,
    ):
        raise ValidationError("DIVIDEND_PAYMENT_EVIDENCE_NOT_CERTIFIED")


def _contract_quantity(ledger, basis: DividendEntitlementBasis) -> Fraction:
    spec = ledger.instruments[basis.instrument_id]
    return decimal_fraction(basis.entitled_quantity) * decimal_fraction(spec.contract_multiplier)


def _fact_available_at(fact) -> datetime:
    return _parse_timestamp(fact.evidence.timing.available_at, "available_at")


def _fact_effective_at(fact) -> datetime:
    return _parse_timestamp(fact.evidence.timing.effective_at, "effective_at")


def _validate_cutoff_snapshot(lifecycle: DividendLifecycle, cutoff: datetime) -> None:
    facts = [
        lifecycle.proposal,
        lifecycle.entitlement,
        lifecycle.election,
        lifecycle.conversion,
        lifecycle.payment,
    ]
    if lifecycle.payment_policy is not None:
        facts.append(lifecycle.payment_policy)
    for fact in facts:
        if fact is None:
            continue
        evidence = getattr(fact, "evidence", None)
        if evidence is not None and _fact_available_at(fact) > cutoff:
            raise ValidationError("FUTURE_DIVIDEND_FACT_IN_REQUEST")


def _lifecycle_prefix(
    lifecycle: DividendLifecycle, phase: DividendExecutionPhase
) -> dict[str, object]:
    payload = lifecycle.to_dict()
    if phase is DividendExecutionPhase.ENTITLEMENT:
        payload["election"] = None
        payload["conversion"] = None
        payload["payment"] = None
    elif phase is DividendExecutionPhase.ISSUER_CONVERSION:
        payload["payment"] = None
    return payload


def _phase_event(lifecycle: DividendLifecycle, phase: DividendExecutionPhase):
    if phase is DividendExecutionPhase.ENTITLEMENT:
        return lifecycle.entitlement
    if phase is DividendExecutionPhase.ISSUER_CONVERSION:
        if lifecycle.election is None:
            raise ValidationError("PAYMENT_ELECTION_REQUIRED")
        return lifecycle.conversion or lifecycle.election
    if lifecycle.payment is None:
        raise ValidationError("DIVIDEND_PAYMENT_REQUIRED")
    return lifecycle.payment


def _record_key(account_id: str, dividend_id: str, phase: DividendExecutionPhase) -> str:
    return f"dividend:{account_id}:{dividend_id}:{phase.value}"


def _phase_hash(
    *,
    ledger,
    lifecycle: DividendLifecycle,
    phase: DividendExecutionPhase,
    prefix: Mapping[str, object],
    basis: DividendEntitlementBasis | None,
    parent_hash: str,
    ledger_quantity: FixedPoint | None,
) -> str:
    payload: dict[str, object] = {
        "schema": DIVIDEND_RECORD_SCHEMA_ID,
        "execution_mode": ledger.dividend_execution_mode.value,
        "fx_valuation_mode": ledger.fx_valuation_mode.value,
        "account_id": ledger.account_id,
        "dividend_id": lifecycle.dividend_id,
        "instrument_id": lifecycle.instrument_id,
        "phase": phase.value,
        "parent_state_sha256": parent_hash,
        "lifecycle_snapshot": dict(prefix),
    }
    if basis is not None:
        payload["entitlement_basis"] = basis.to_dict()
    if ledger_quantity is not None:
        payload["ledger_quantity_at_ex"] = _fixed_payload(ledger_quantity)
    return _sha256(payload)


def _state_with_hash(**values) -> DividendLifecycleState:
    state = DividendLifecycleState(state_sha256="", **values)
    return replace(state, state_sha256=_sha256(state.to_dict(include_hash=False)))


def _transaction(
    *,
    ledger,
    lifecycle: DividendLifecycle,
    phase: DividendExecutionPhase,
    event_time: datetime,
    postings: tuple[Posting, ...],
) -> LedgerTransaction:
    reference = f"dividend:{lifecycle.dividend_id}:{phase.value}"
    identity = _sha256(
        {
            "account_id": ledger.account_id,
            "reference": reference,
            "event_time": _timestamp(event_time),
        }
    )[:24]
    return LedgerTransaction(
        transaction_id=f"tx-dividend-{identity}",
        idempotency_key=_record_key(ledger.account_id, lifecycle.dividend_id, phase),
        event_time=event_time,
        event_type=LedgerEventType.CORPORATE_ACTION,
        reference_id=reference,
        postings=postings,
    )


def _posting(
    account: str,
    currency: str,
    amount: FixedPoint,
    *,
    dividend_id: str,
    bind_dividend: bool = True,
) -> Posting:
    return Posting(
        ledger_account=account,
        currency=currency,
        amount=amount,
        instrument_id=f"dividend:{dividend_id}" if bind_dividend else None,
    )


def _negate(value: FixedPoint) -> FixedPoint:
    return FixedPoint(-value.units, value.scale)


def apply_dividend_lifecycle(ledger, request: DividendExecutionRequest) -> DividendExecutionRecord:
    """Apply one lifecycle phase as a complete in-memory transaction."""

    if not isinstance(request, DividendExecutionRequest):
        raise ValidationError("request must be a DividendExecutionRequest")
    ledger._require_mutable()
    checkpoint = ledger.capture_state()
    try:
        return _apply_dividend_lifecycle(ledger, request)
    except Exception:
        ledger._restore_captured_state(checkpoint)
        raise


def _apply_dividend_lifecycle(ledger, request: DividendExecutionRequest) -> DividendExecutionRecord:
    lifecycle = request.lifecycle
    phase = request.phase
    basis = request.entitlement_basis
    if ledger.dividend_execution_mode is None:
        raise ValidationError("DIVIDEND_EXECUTION_MODE_REQUIRED")
    if ledger.fx_valuation_mode is not FxValuationMode.EVIDENCED_PIT:
        raise ValidationError("EVIDENCED_PIT_REQUIRED")
    if lifecycle.instrument_id not in ledger.instruments:
        raise ValidationError("UNKNOWN_DIVIDEND_INSTRUMENT")
    if (
        phase is DividendExecutionPhase.ENTITLEMENT
        and basis is not None
        and (
            _fact_available_at(lifecycle.entitlement) > basis.ex_at
            or basis.available_at > basis.ex_at
        )
    ):
        raise ValidationError("LATE_ENTITLEMENT_UNSUPPORTED")
    _validate_cutoff_snapshot(lifecycle, request.cutoff)

    key = _record_key(ledger.account_id, lifecycle.dividend_id, phase)
    prefix = _lifecycle_prefix(lifecycle, phase)
    prior_record = ledger._dividend_execution_by_key.get(key)
    if prior_record is not None:
        prior_state = ledger._dividend_lifecycle_states.get(
            (ledger.account_id, lifecycle.dividend_id)
        )
        expected_basis = (
            basis
            if phase is DividendExecutionPhase.ENTITLEMENT
            else (prior_state.entitlement_basis if prior_state is not None else None)
        )
        same = (
            _thaw(prior_record.lifecycle_snapshot) == prefix
            and prior_record.entitlement_basis == expected_basis
        )
        if same:
            return prior_record
        raise ValidationError("DIVIDEND_PHASE_ID_REUSED")

    state_key = (ledger.account_id, lifecycle.dividend_id)
    prior_state = ledger._dividend_lifecycle_states.get(state_key)
    parent_hash = prior_state.state_sha256 if prior_state is not None else "0" * 64
    event = _phase_event(lifecycle, phase)
    phase_facts = [event]
    if phase is DividendExecutionPhase.ISSUER_CONVERSION:
        phase_facts = [lifecycle.election]
        if lifecycle.conversion is not None:
            phase_facts.append(lifecycle.conversion)
        if lifecycle.payment_policy is not None:
            phase_facts.append(lifecycle.payment_policy)
    economic_at = max(_fact_effective_at(item) for item in phase_facts)
    available_at = max(_fact_available_at(item) for item in phase_facts)
    transaction_before = len(ledger._transactions)
    postings: tuple[Posting, ...] = ()
    issuer_conversion_audit: Mapping[str, object] | None = None

    if phase is DividendExecutionPhase.ENTITLEMENT:
        if prior_state is not None:
            raise ValidationError("DIVIDEND_PHASE_ORDER_INVALID")
        if basis is None:
            raise ValidationError("ENTITLEMENT_BASIS_REQUIRED")
        if (
            basis.account_id != ledger.account_id
            or basis.dividend_id != lifecycle.dividend_id
            or basis.instrument_id != lifecycle.instrument_id
        ):
            raise ValidationError("ENTITLEMENT_BASIS_IDENTITY_MISMATCH")
        if basis.available_at > basis.ex_at or available_at > basis.ex_at:
            raise ValidationError("LATE_ENTITLEMENT_UNSUPPORTED")
        if request.cutoff < basis.ex_at:
            raise ValidationError("ENTITLEMENT_CUTOFF_PRECEDES_EX_AT")
        if ledger._event_time > basis.ex_at:
            raise ValidationError("LEDGER_PASSED_EX_AT")
        if ledger.dividend_execution_mode is DividendExecutionMode.PRODUCTION_CERTIFIED:
            verifier = ledger._entitlement_evidence_verifier
            if (
                verifier is None
                or basis.certification_ref is None
                or not verifier.verify_entitlement_basis(basis=basis, lifecycle=lifecycle)
            ):
                raise ValidationError("ENTITLEMENT_EVIDENCE_NOT_CERTIFIED")
        ledger_quantity_decimal = ledger._positions.get(lifecycle.instrument_id, Decimal(0))
        if ledger._is_derivative(ledger.instruments[lifecycle.instrument_id]):
            raise ValidationError("UNSUPPORTED_DERIVATIVE_DIVIDEND")
        if ledger_quantity_decimal < 0:
            raise ValidationError("NEGATIVE_DIVIDEND_POSITION_UNSUPPORTED")
        if ledger_quantity_decimal != decimal(basis.entitled_quantity):
            raise ValidationError("ENTITLEMENT_BASIS_MISMATCH")
        ledger_quantity = basis.entitled_quantity
        _require_trusted_payment_policy(ledger, lifecycle)
        policy = _certified_rounding_policy(lifecycle.payment_policy, account_id=ledger.account_id)
        gross_fraction = _published_account_amount(lifecycle, _contract_quantity(ledger, basis))
        declared_gross = _account_money(
            gross_fraction, money_scale=ledger.money_scale, policy=policy
        )
        declared_currency = lifecycle.entitlement.declared_currency.calculation_currency
        applied_at = basis.ex_at
        if declared_gross.units:
            postings = (
                _posting(
                    "assets:dividend_receivable",
                    declared_currency,
                    declared_gross,
                    dividend_id=lifecycle.dividend_id,
                ),
                _posting(
                    "income:dividend",
                    declared_currency,
                    _negate(declared_gross),
                    dividend_id=lifecycle.dividend_id,
                ),
            )
        phase_fingerprint = _phase_hash(
            ledger=ledger,
            lifecycle=lifecycle,
            phase=phase,
            prefix=prefix,
            basis=basis,
            parent_hash=parent_hash,
            ledger_quantity=ledger_quantity,
        )
        next_state = _state_with_hash(
            account_id=ledger.account_id,
            dividend_id=lifecycle.dividend_id,
            instrument_id=lifecycle.instrument_id,
            execution_mode=ledger.dividend_execution_mode,
            fx_valuation_mode=ledger.fx_valuation_mode,
            entitlement_basis=basis,
            ledger_quantity_at_ex=ledger_quantity,
            lifecycle_snapshot=prefix,
            declared_currency=declared_currency,
            declared_gross=declared_gross,
            receivable_currency=declared_currency,
            receivable_gross=declared_gross,
            payment_policy_id=(
                lifecycle.payment_policy.policy_id if lifecycle.payment_policy is not None else None
            ),
            phases=(phase.value,),
            phase_fingerprints=((phase.value, phase_fingerprint),),
            transaction_ids=(),
            paid=False,
        )
    else:
        if prior_state is None:
            raise ValidationError("DIVIDEND_PHASE_ORDER_INVALID")
        if prior_state.instrument_id != lifecycle.instrument_id:
            raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
        prior_entitlement = _thaw(prior_state.lifecycle_snapshot["entitlement"])
        if prior_entitlement != lifecycle.entitlement.to_dict():
            raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
        prior_proposal = _thaw(prior_state.lifecycle_snapshot.get("proposal"))
        current_proposal = lifecycle.proposal.to_dict() if lifecycle.proposal is not None else None
        if prior_proposal is not None and current_proposal != prior_proposal:
            raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
        prior_policy = _thaw(prior_state.lifecycle_snapshot.get("payment_policy"))
        current_policy = (
            lifecycle.payment_policy.to_dict() if lifecycle.payment_policy is not None else None
        )
        if prior_policy is not None and current_policy != prior_policy:
            raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
        applied_at = max(economic_at, available_at)
        if applied_at < ledger._event_time:
            raise ValidationError("LEDGER_EVENT_TIME_BACKWARDS")

        if phase is DividendExecutionPhase.ISSUER_CONVERSION:
            if prior_state.phases != (DividendExecutionPhase.ENTITLEMENT.value,):
                raise ValidationError("DIVIDEND_PHASE_ORDER_INVALID")
            election = lifecycle.election
            if election is None or election.account_id != ledger.account_id:
                raise ValidationError("PAYMENT_ELECTION_ACCOUNT_MISMATCH")
            declared = prior_state.declared_currency
            selected = election.payment_currency
            _require_trusted_payment_policy(ledger, lifecycle)
            policy = _certified_rounding_policy(
                lifecycle.payment_policy, account_id=ledger.account_id
            )
            if selected == declared:
                if lifecycle.conversion is not None:
                    raise ValidationError("SAME_CURRENCY_CONVERSION_FORBIDDEN")
                payment_gross = prior_state.declared_gross
            else:
                conversion = lifecycle.conversion
                if conversion is None:
                    raise ValidationError("ISSUER_CONVERSION_REQUIRED")
                if conversion.from_currency != declared or conversion.to_currency != selected:
                    raise ValidationError("ISSUER_CONVERSION_CURRENCY_MISMATCH")
                account_quantity = _contract_quantity(ledger, prior_state.entitlement_basis)
                exact_payment = _conversion_account_amount(lifecycle, account_quantity)
                issuer_conversion_audit = _issuer_conversion_audit(
                    lifecycle,
                    account_quantity,
                )
                payment_gross = _account_money(
                    exact_payment, money_scale=ledger.money_scale, policy=policy
                )
                if prior_state.declared_gross.units or payment_gross.units:
                    postings = (
                        _posting(
                            "assets:dividend_receivable",
                            declared,
                            _negate(prior_state.declared_gross),
                            dividend_id=lifecycle.dividend_id,
                        ),
                        _posting(
                            "clearing:dividend_issuer_fx",
                            declared,
                            prior_state.declared_gross,
                            dividend_id=lifecycle.dividend_id,
                        ),
                        _posting(
                            "assets:dividend_receivable",
                            selected,
                            payment_gross,
                            dividend_id=lifecycle.dividend_id,
                        ),
                        _posting(
                            "clearing:dividend_issuer_fx",
                            selected,
                            _negate(payment_gross),
                            dividend_id=lifecycle.dividend_id,
                        ),
                    )
            phase_fingerprint = _phase_hash(
                ledger=ledger,
                lifecycle=lifecycle,
                phase=phase,
                prefix=prefix,
                basis=None,
                parent_hash=parent_hash,
                ledger_quantity=None,
            )
            next_state = _state_with_hash(
                account_id=prior_state.account_id,
                dividend_id=prior_state.dividend_id,
                instrument_id=prior_state.instrument_id,
                execution_mode=prior_state.execution_mode,
                fx_valuation_mode=prior_state.fx_valuation_mode,
                entitlement_basis=prior_state.entitlement_basis,
                ledger_quantity_at_ex=prior_state.ledger_quantity_at_ex,
                lifecycle_snapshot=prefix,
                declared_currency=prior_state.declared_currency,
                declared_gross=prior_state.declared_gross,
                receivable_currency=selected,
                receivable_gross=payment_gross,
                payment_policy_id=(
                    lifecycle.payment_policy.policy_id
                    if lifecycle.payment_policy is not None
                    else prior_state.payment_policy_id
                ),
                phases=(*prior_state.phases, phase.value),
                phase_fingerprints=(
                    *prior_state.phase_fingerprints,
                    (phase.value, phase_fingerprint),
                ),
                transaction_ids=prior_state.transaction_ids,
                paid=False,
            )
        else:
            if prior_state.phases != (
                DividendExecutionPhase.ENTITLEMENT.value,
                DividendExecutionPhase.ISSUER_CONVERSION.value,
            ):
                raise ValidationError("DIVIDEND_PHASE_ORDER_INVALID")
            election = lifecycle.election
            policy = lifecycle.payment_policy
            payment = lifecycle.payment
            if election is None or payment is None or policy is None:
                raise ValidationError("UNVERIFIED_PAYMENT_POLICY")
            _require_trusted_payment_policy(ledger, lifecycle)
            _require_trusted_actual_payment(ledger, lifecycle)
            if election.to_dict() != _thaw(prior_state.lifecycle_snapshot.get("election")):
                raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
            prior_conversion = _thaw(prior_state.lifecycle_snapshot.get("conversion"))
            current_conversion = (
                lifecycle.conversion.to_dict() if lifecycle.conversion is not None else None
            )
            if current_conversion != prior_conversion:
                raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
            if lifecycle.conversion is not None:
                issuer_conversion_audit = _issuer_conversion_audit(
                    lifecycle,
                    _contract_quantity(ledger, prior_state.entitlement_basis),
                )
            if (
                policy.certification_status != "certified"
                or policy.account_id != ledger.account_id
                or payment.account_id != ledger.account_id
            ):
                raise ValidationError("UNVERIFIED_PAYMENT_POLICY")
            if prior_policy is not None and policy.to_dict() != prior_policy:
                raise ValidationError("LIFECYCLE_PREFIX_CHANGED")
            if payment.payment_currency != prior_state.receivable_currency:
                raise ValidationError("PAYMENT_CURRENCY_MISMATCH")
            gross = _fixed_exact(
                _fraction_text(payment.gross_cash_text, "gross cash"),
                ledger.money_scale,
                "PAYMENT_AMOUNT_NOT_EXACT",
            )
            if gross != prior_state.receivable_gross:
                raise ValidationError("PAYMENT_GROSS_MISMATCH")
            net = _fixed_exact(
                _fraction_text(payment.net_cash_text, "net cash"),
                ledger.money_scale,
                "PAYMENT_AMOUNT_NOT_EXACT",
            )
            withholding = _fixed_exact(
                _fraction_text(payment.withholding_cash_text, "withholding cash"),
                ledger.money_scale,
                "PAYMENT_AMOUNT_NOT_EXACT",
            )
            rounding = _fixed_exact(
                _fraction_text(payment.rounding_adjustment_text, "rounding adjustment"),
                ledger.money_scale,
                "PAYMENT_AMOUNT_NOT_EXACT",
            )
            payment_postings = [
                _posting(
                    "assets:cash",
                    payment.payment_currency,
                    net,
                    dividend_id=lifecycle.dividend_id,
                    bind_dividend=False,
                ),
                _posting(
                    "expenses:dividend_withholding",
                    payment.payment_currency,
                    withholding,
                    dividend_id=lifecycle.dividend_id,
                ),
            ]
            payment_postings.extend(
                _posting(
                    f"expenses:dividend_deduction:{deduction.deduction_id}",
                    payment.payment_currency,
                    _fixed_exact(
                        _fraction_text(deduction.amount_text, "deduction amount"),
                        ledger.money_scale,
                        "PAYMENT_AMOUNT_NOT_EXACT",
                    ),
                    dividend_id=lifecycle.dividend_id,
                )
                for deduction in payment.deductions
            )
            payment_postings.extend(
                (
                    _posting(
                        "expenses:dividend_rounding",
                        payment.payment_currency,
                        rounding,
                        dividend_id=lifecycle.dividend_id,
                    ),
                    _posting(
                        "assets:dividend_receivable",
                        payment.payment_currency,
                        _negate(gross),
                        dividend_id=lifecycle.dividend_id,
                    ),
                )
            )
            if gross.units:
                postings = tuple(payment_postings)
            phase_fingerprint = _phase_hash(
                ledger=ledger,
                lifecycle=lifecycle,
                phase=phase,
                prefix=prefix,
                basis=None,
                parent_hash=parent_hash,
                ledger_quantity=None,
            )
            next_state = _state_with_hash(
                account_id=prior_state.account_id,
                dividend_id=prior_state.dividend_id,
                instrument_id=prior_state.instrument_id,
                execution_mode=prior_state.execution_mode,
                fx_valuation_mode=prior_state.fx_valuation_mode,
                entitlement_basis=prior_state.entitlement_basis,
                ledger_quantity_at_ex=prior_state.ledger_quantity_at_ex,
                lifecycle_snapshot=prefix,
                declared_currency=prior_state.declared_currency,
                declared_gross=prior_state.declared_gross,
                receivable_currency=prior_state.receivable_currency,
                receivable_gross=FixedPoint(0, ledger.money_scale),
                payment_policy_id=policy.policy_id,
                phases=(*prior_state.phases, phase.value),
                phase_fingerprints=(
                    *prior_state.phase_fingerprints,
                    (phase.value, phase_fingerprint),
                ),
                transaction_ids=prior_state.transaction_ids,
                paid=True,
            )

    transaction_ids: tuple[str, ...] = ()
    if postings:
        transaction = _transaction(
            ledger=ledger,
            lifecycle=lifecycle,
            phase=phase,
            event_time=applied_at,
            postings=postings,
        )
        ledger._post(transaction)
        transaction_ids = (transaction.transaction_id,)
    ledger._event_time = applied_at
    if transaction_ids:
        next_state = replace(
            next_state,
            transaction_ids=(*next_state.transaction_ids, *transaction_ids),
            state_sha256="",
        )
        next_state = replace(
            next_state, state_sha256=_sha256(next_state.to_dict(include_hash=False))
        )
    operation_sequence = ledger._next_dividend_replay_sequence()
    record = DividendExecutionRecord(
        account_id=ledger.account_id,
        dividend_id=lifecycle.dividend_id,
        instrument_id=lifecycle.instrument_id,
        phase=phase,
        phase_event_id=event.evidence.event_id,
        cutoff=request.cutoff,
        economic_effective_at=economic_at,
        available_at=available_at,
        applied_at=applied_at,
        lifecycle_snapshot=prefix,
        lifecycle_snapshot_sha256=_sha256(prefix),
        phase_fingerprint=phase_fingerprint,
        parent_state_sha256=parent_hash,
        resulting_state_sha256=next_state.state_sha256,
        execution_mode=ledger.dividend_execution_mode,
        fx_valuation_mode=ledger.fx_valuation_mode,
        entitlement_basis=next_state.entitlement_basis,
        issuer_conversion_audit=issuer_conversion_audit,
        transaction_ids=transaction_ids,
        operation_sequence=operation_sequence,
        transaction_count_before=transaction_before,
        transaction_count_after=len(ledger._transactions),
    )
    ledger._dividend_lifecycle_states[state_key] = next_state
    ledger._dividend_execution_records.append(record)
    ledger._dividend_execution_by_key[key] = record
    ledger._dividend_phase_fingerprints[key] = phase_fingerprint
    ledger._dividend_operation_log.append(record)
    return record


def observe_pit_fx(ledger, rate: PitFxRate) -> PitFxObservationRecord:
    if ledger.fx_valuation_mode is not FxValuationMode.EVIDENCED_PIT:
        raise ValidationError("EVIDENCED_PIT_REQUIRED")
    if not isinstance(rate, PitFxRate):
        raise ValidationError("rate must be a PitFxRate")
    ledger._require_mutable()
    fingerprint = rate.fingerprint()
    prior = ledger._dividend_pit_fx_by_event_id.get(rate.event_id)
    if prior is not None:
        if prior.fingerprint() != fingerprint:
            raise ValidationError("PIT_FX_EVENT_ID_CONFLICT")
        return next(
            item
            for item in ledger._dividend_pit_fx_records
            if item.rate_payload["event_id"] == rate.event_id
        )
    sequence = len(ledger._dividend_pit_fx_observations)
    operation_sequence = ledger._next_dividend_replay_sequence()
    record = PitFxObservationRecord(
        observation_sequence=sequence,
        rate_payload=rate.to_dict(),
        rate_fingerprint=fingerprint,
        operation_sequence=operation_sequence,
        transaction_count=len(ledger._transactions),
    )
    ledger._dividend_pit_fx_observations.append(rate)
    ledger._dividend_pit_fx_by_event_id[rate.event_id] = rate
    ledger._dividend_pit_fx_records.append(record)
    ledger._dividend_operation_log.append(record)
    return record


def select_valuation_rate(ledger, currency: str, as_of: datetime):
    try:
        return select_pit_fx(
            ledger._dividend_pit_fx_observations,
            base_currency=currency,
            quote_currency=ledger.base_currency,
            cutoff=_timestamp(as_of),
        )
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"PIT_FX_UNAVAILABLE: {exc}") from exc


def convert_for_valuation(
    ledger, amount: Decimal | FixedPoint, currency: str, as_of: datetime
) -> Decimal:
    if ledger.fx_valuation_mode is FxValuationMode.LEGACY:
        return ledger._to_base(amount, currency, as_of)
    value = decimal(amount) if isinstance(amount, FixedPoint) else amount
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValidationError("valuation amount must be a finite Decimal or FixedPoint")
    if value == 0:
        return Decimal(0)
    selected = select_valuation_rate(ledger, currency, as_of)
    try:
        return fraction_decimal_exact(decimal_fraction(value) * decimal_fraction(selected.rate))
    except ValidationError as exc:
        raise ValidationError("PIT_VALUATION_RESULT_NOT_EXACT") from exc


def dividend_exposure(ledger, *, as_of: datetime) -> DividendExposureSnapshot:
    at = _utc(as_of, "as_of")
    if at < ledger._event_time:
        raise ValidationError("HISTORICAL_LEDGER_STATE_UNAVAILABLE")
    items: list[DividendExposureItem] = []
    base_values: list[Decimal] = []
    unknown = False
    for state in sorted(
        ledger._dividend_lifecycle_states.values(), key=lambda item: item.dividend_id
    ):
        if state.receivable_gross.units == 0:
            continue
        value = convert_for_valuation(ledger, state.receivable_gross, state.receivable_currency, at)
        base_value = _fixed_exact(
            decimal_fraction(value), ledger.money_scale, "PIT_VALUATION_PRECISION_REQUIRED"
        )
        policy_payload = state.lifecycle_snapshot.get("payment_policy")
        certified = bool(
            isinstance(policy_payload, Mapping)
            and policy_payload.get("certification_status") == "certified"
        )
        unknown = unknown or not certified
        items.append(
            DividendExposureItem(
                dividend_id=state.dividend_id,
                instrument_id=state.instrument_id,
                currency=state.receivable_currency,
                gross_receivable=state.receivable_gross,
                base_value=base_value,
                tax_status="certified" if certified else "unknown",
            )
        )
        base_values.append(value)
    gross = _fixed_exact(
        decimal_fraction(sum_decimal_exact(base_values)),
        ledger.money_scale,
        "PIT_VALUATION_PRECISION_REQUIRED",
    )
    return DividendExposureSnapshot(
        account_id=ledger.account_id,
        as_of=at,
        execution_mode=ledger.dividend_execution_mode,
        fx_valuation_mode=ledger.fx_valuation_mode,
        items=tuple(items),
        gross_nav=gross,
        tax_status="unknown" if unknown else "certified",
        estimated_net_cash=None,
    )


def _selected_rate_payloads(ledger, as_of: datetime) -> tuple[Mapping[str, object], ...]:
    currencies = {
        currency
        for (account, currency, _), amount in ledger._accounts.items()
        if amount != 0 and account in {"assets:cash", "assets:dividend_receivable"}
    }
    currencies.update(
        ledger.instruments[instrument_id].settlement_currency
        for instrument_id, quantity in ledger._positions.items()
        if quantity != 0
    )
    payloads = []
    for currency in sorted(currencies):
        selected = select_valuation_rate(ledger, currency, as_of)
        if selected.identity:
            payloads.append(
                {
                    "base_currency": selected.base_currency,
                    "quote_currency": selected.quote_currency,
                    "rate_text": "1",
                    "identity": True,
                    "event_id": None,
                    "observed_at": None,
                    "available_at": None,
                    "captured_at": None,
                    "source": "identity",
                    "evidence_id": "identity",
                }
            )
        else:
            full = ledger._dividend_pit_fx_by_event_id[selected.event_id].to_dict()
            payloads.append({**full, "identity": False})
    return tuple(payloads)


def record_dividend_valuation(ledger, *, as_of: datetime) -> DividendValuationRecord:
    if ledger.fx_valuation_mode is not FxValuationMode.EVIDENCED_PIT:
        raise ValidationError("EVIDENCED_PIT_REQUIRED")
    ledger._require_mutable()
    at = _utc(as_of, "as_of")
    exposure = dividend_exposure(ledger, as_of=at)
    account = ledger.snapshot(at)
    result_payload = {
        "account_snapshot": {
            "account_id": account.account_id,
            "event_time": _timestamp(account.event_time),
            "base_currency": account.base_currency,
            "cash_balances": {
                key: _fixed_payload(value) for key, value in account.cash_balances.items()
            },
            "positions": {key: _fixed_payload(value) for key, value in account.positions.items()},
            "nav": _fixed_payload(account.nav),
            "cost_basis": {key: _fixed_payload(value) for key, value in account.cost_basis.items()},
            "realized_pnl": {
                key: _fixed_payload(value) for key, value in account.realized_pnl.items()
            },
            "unrealized_pnl": {
                key: _fixed_payload(value) for key, value in account.unrealized_pnl.items()
            },
            "initial_margin": _fixed_payload(account.initial_margin),
            "maintenance_margin": _fixed_payload(account.maintenance_margin),
            "liquidation_required": account.liquidation_required,
        },
        "dividend_exposure": exposure.to_dict(),
    }
    selected = _selected_rate_payloads(ledger, at)
    key = f"dividend-valuation:{ledger.account_id}:{_timestamp(at)}"
    result_sha = _sha256(result_payload)
    prior = ledger._recorded_dividend_valuations.get(key)
    if prior is not None:
        if (
            prior.result_sha256 == result_sha
            and prior.selected_rates == selected
            and _thaw(prior.result_payload) == result_payload
        ):
            return prior
        raise ValidationError("VALUATION_IDEMPOTENCY_CONFLICT")
    record = DividendValuationRecord(
        valuation_idempotency_key=key,
        as_of=at,
        execution_mode=ledger.dividend_execution_mode,
        fx_valuation_mode=ledger.fx_valuation_mode,
        selected_rates=selected,
        result_payload=result_payload,
        result_sha256=result_sha,
        operation_sequence=ledger._next_dividend_replay_sequence(),
        transaction_count=len(ledger._transactions),
    )
    ledger._recorded_dividend_valuations[key] = record
    ledger._dividend_valuation_records.append(record)
    ledger._dividend_operation_log.append(record)
    return record


__all__ = [
    "DIVIDEND_JOURNAL_SCHEMA_ID",
    "DIVIDEND_RECORD_SCHEMA_ID",
    "DividendEntitlementBasis",
    "DividendExecutionMode",
    "DividendExecutionPhase",
    "DividendExecutionRecord",
    "DividendExecutionRequest",
    "DividendExposureItem",
    "DividendExposureSnapshot",
    "DividendLifecycleState",
    "DividendValuationRecord",
    "EntitlementEvidenceVerifier",
    "FxValuationMode",
    "PitFxObservationRecord",
    "apply_dividend_lifecycle",
    "canonical_bytes",
    "convert_for_valuation",
    "dividend_exposure",
    "dividend_record_bytes",
    "dividend_record_from_dict",
    "observe_pit_fx",
    "record_dividend_valuation",
    "select_valuation_rate",
]
