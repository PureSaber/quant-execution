"""Exact multi-currency double-entry account ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache
from types import MappingProxyType

from quant_data_kit import (
    AssetClass,
    BarEvent,
    BookSnapshotEvent,
    CorporateActionEvent,
    FixedPoint,
    FundingRateEvent,
    InstrumentSpec,
    MarketEvent,
    MarkPriceEvent,
    QuoteEvent,
    StatusEvent,
    TradeEvent,
    ensure_utc_datetime,
    market_event_payload,
)
from quant_data_kit.exceptions import ValidationError

from quant_execution._fixed import (
    add_decimal_exact,
    decimal,
    decimal_fraction,
    fixed,
    fraction_decimal_exact,
    multiply_decimal_exact,
    sum_decimal_exact,
)
from quant_execution._json import fixed_token, flat_sequence_bytes, string_token, utc_token
from quant_execution.artifacts import (
    fee_bytes,
    fill_bytes,
    ledger_transaction_bytes,
    settlement_bytes,
)
from quant_execution.contracts import (
    AccountSnapshot,
    Fee,
    Fill,
    Funding,
    LedgerEvent,
    LedgerEventType,
    LedgerTransaction,
    PortfolioRiskSnapshot,
    PositionRiskSnapshot,
    Posting,
    Settlement,
    Side,
    _currency,
)
from quant_execution.dividends import (
    DIVIDEND_JOURNAL_SCHEMA_ID,
    DividendExecutionMode,
    DividendExecutionRequest,
    DividendExposureSnapshot,
    DividendValuationRecord,
    EntitlementEvidenceVerifier,
    FxValuationMode,
    PitFxObservationRecord,
    canonical_bytes,
)
from quant_execution.dividends import (
    apply_dividend_lifecycle as apply_dividend_lifecycle_request,
)
from quant_execution.dividends import (
    convert_for_valuation as convert_dividend_value,
)
from quant_execution.dividends import (
    dividend_exposure as build_dividend_exposure,
)
from quant_execution.dividends import (
    observe_pit_fx as observe_dividend_pit_fx,
)
from quant_execution.dividends import (
    record_dividend_valuation as build_dividend_valuation,
)
from quant_execution.schemas import execution_payload

UTC = timezone.utc
_OPENED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_MISSING = object()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()


def _identifier(prefix: str, *parts: object) -> str:
    return f"{prefix}-{hashlib.sha256(flat_sequence_bytes(parts)).hexdigest()[:24]}"


@lru_cache(maxsize=256)
def _parse_metadata_decimal(raw: str) -> Decimal:
    return Decimal(raw)


def _meta_decimal(spec: InstrumentSpec, key: str, default: str = "0") -> Decimal:
    raw = spec.metadata.get(key, default)
    try:
        value = _parse_metadata_decimal(raw)
    except Exception as exc:
        raise ValidationError(f"InstrumentSpec metadata {key!r} must be decimal") from exc
    if not value.is_finite():
        raise ValidationError(f"InstrumentSpec metadata {key!r} must be finite")
    return value


class ExactAccountLedger:
    """Journal-first account state; every mutation is replayable and idempotent."""

    sends_live_orders = False

    def __init__(
        self,
        *,
        account_id: str,
        base_currency: str,
        instruments: Mapping[str, InstrumentSpec],
        initial_cash: Mapping[str, FixedPoint] | None = None,
        fx_to_base: Mapping[str, FixedPoint] | None = None,
        money_scale: int = 8,
        opened_at: datetime | None = None,
        dividend_execution_mode: DividendExecutionMode | None = None,
        fx_valuation_mode: FxValuationMode = FxValuationMode.LEGACY,
        entitlement_evidence_verifier: EntitlementEvidenceVerifier | None = None,
    ) -> None:
        if not account_id.strip() or not base_currency.strip():
            raise ValidationError("account_id and base_currency are required")
        if not 0 <= money_scale <= 18:
            raise ValidationError("money_scale must be in [0, 18]")
        if dividend_execution_mode is not None and not isinstance(
            dividend_execution_mode, DividendExecutionMode
        ):
            raise ValidationError("dividend_execution_mode has an invalid type")
        if not isinstance(fx_valuation_mode, FxValuationMode):
            raise ValidationError("fx_valuation_mode has an invalid type")
        if (
            dividend_execution_mode is not None
            and fx_valuation_mode is not FxValuationMode.EVIDENCED_PIT
        ):
            raise ValidationError("new dividend lifecycle execution requires EVIDENCED_PIT")
        if fx_valuation_mode is FxValuationMode.EVIDENCED_PIT and dividend_execution_mode is None:
            raise ValidationError("EVIDENCED_PIT requires an explicit dividend execution mode")
        if (
            dividend_execution_mode is DividendExecutionMode.PRODUCTION_CERTIFIED
            and entitlement_evidence_verifier is None
        ):
            raise ValidationError("production dividend execution requires a trusted verifier")
        if fx_valuation_mode is FxValuationMode.EVIDENCED_PIT and fx_to_base:
            raise ValidationError("EVIDENCED_PIT does not accept legacy initial FX snapshots")
        self._account_id = account_id
        self._base_currency = _currency(base_currency, "base_currency")
        self._instruments = MappingProxyType(dict(instruments))
        self._derivative_instruments = frozenset(
            instrument_id
            for instrument_id, spec in self._instruments.items()
            if self._is_derivative(spec)
        )
        self._money_scale = money_scale
        self._dividend_execution_mode = dividend_execution_mode
        self._fx_valuation_mode = fx_valuation_mode
        self._entitlement_evidence_verifier = entitlement_evidence_verifier
        self._initial_cash = dict(initial_cash or {})
        self._initial_fx = dict(fx_to_base or {})
        self._default_opened_at = (
            ensure_utc_datetime(opened_at, field="opened_at")
            if opened_at is not None
            else _OPENED_AT
        )
        self.reset()

    def reset(self, *, opened_at: datetime | None = None) -> None:
        opening_time = (
            ensure_utc_datetime(opened_at, field="opened_at")
            if opened_at is not None
            else self._default_opened_at
        )
        self._transactions: list[LedgerTransaction] = []
        self._transaction_count = 0
        self._artifact_sink = None
        self._artifact_stream_closed = False
        self._finalized_journal_sha256: str | None = None
        self._transaction_keys: set[str] = set()
        self._event_fingerprints: dict[str, LedgerEvent | bytes] = {}
        self._fills: dict[str, Fill] = {}
        self._accounts: dict[tuple[str, str, str | None], Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._marks: dict[str, tuple[Decimal, datetime, str]] = {}
        self._mark_fingerprints: dict[str, MarketEvent] = {}
        self._fill_trading_days: dict[str, date] = {}
        self._position_lots: dict[str, list[tuple[date, Decimal]]] = {}
        self._fill_close_allocations: dict[str, tuple[Decimal, Decimal]] = {}
        self._dividend_entitlements: dict[tuple[str, str, date], tuple[Decimal, Decimal]] = {}
        self._dividend_lifecycle_states = {}
        self._dividend_execution_records = []
        self._dividend_execution_by_key = {}
        self._dividend_phase_fingerprints = {}
        self._dividend_pit_fx_observations = []
        self._dividend_pit_fx_by_event_id = {}
        self._dividend_pit_fx_records = []
        self._recorded_dividend_valuations = {}
        self._dividend_valuation_records = []
        self._dividend_operation_log = []
        self._dividend_replay_facts: list[dict[str, object]] = []
        self._dividend_replay_sequence = 0
        self._posting_cache: dict[
            tuple[str, str, Decimal, str | None, Decimal | None, int], Posting
        ] = {}
        self._fx: dict[str, tuple[Decimal, datetime]] = {
            self.base_currency: (Decimal(1), opening_time)
        }
        self._fx_history: list[tuple[str, Decimal, datetime]] = [
            (self.base_currency, Decimal(1), opening_time)
        ]
        self._event_time = opening_time
        for currency in sorted(self._initial_fx):
            self.set_fx_rate(currency, self._initial_fx[currency], event_time=opening_time)
        for currency, amount in sorted(self._initial_cash.items()):
            value = decimal(amount)
            transaction = self._make_transaction(
                event_type=LedgerEventType.FX_CONVERSION,
                reference_id=f"opening:{currency}",
                idempotency_key=f"opening:{self.account_id}:{currency}",
                event_time=opening_time,
                postings=(
                    self._posting("assets:cash", currency, value),
                    self._posting("equity:opening", currency, value.copy_negate()),
                ),
            )
            self._post(transaction)

    def book_external_cash(
        self, *, transfer_id: str, amount: FixedPoint, currency: str, event_time: datetime
    ) -> AccountSnapshot:
        """Import a confirmed deposit/withdrawal as equity, never trading income."""
        self._require_mutable()
        event_time = ensure_utc_datetime(event_time, field="event_time")
        currency = _currency(currency)
        if currency != self.base_currency:
            raise ValidationError("statement cash transfers currently require the base currency")
        if not transfer_id.strip() or not isinstance(amount, FixedPoint):
            raise ValidationError("transfer ID and fixed-point amount are required")
        if amount.scale > self.money_scale:
            raise ValidationError("transfer precision exceeds ledger money scale")
        transaction = self._make_transaction(
            event_type=LedgerEventType.SETTLEMENT,
            reference_id=f"external:{transfer_id}",
            idempotency_key=f"external:{transfer_id}",
            event_time=event_time,
            postings=(
                self._posting("assets:cash", currency, decimal(amount)),
                self._posting("equity:external_flows", currency, -decimal(amount)),
            ),
        )
        if self._import_seen(transaction):
            return self.snapshot(self._event_time)
        resulting_cash = add_decimal_exact(self.cash_balance(currency), decimal(amount))
        if event_time < self._event_time or resulting_cash < 0:
            raise ValidationError("external cash would reverse time or overdraw account")
        transaction_before = len(self._transactions)
        self._post(transaction)
        self._event_time = event_time
        self._record_dividend_replay_fact(
            kind="external_cash",
            payload={
                "transfer_id": transfer_id,
                "amount": {"units": amount.units, "scale": amount.scale},
                "currency": currency,
                "event_time": event_time.isoformat().replace("+00:00", "Z"),
            },
            transaction_count_before=transaction_before,
            transaction_count_after=len(self._transactions),
        )
        return self.snapshot(event_time)

    def book_opening_position(
        self,
        *,
        instrument_id: str,
        quantity: FixedPoint,
        average_cost: FixedPoint,
        acquired_on: date,
    ) -> AccountSnapshot:
        """Import a statement opening balance without inventing historical fills."""
        self._require_mutable()
        spec = self._spec(instrument_id)
        if self._is_derivative(spec) or decimal(quantity) <= 0 or decimal(average_cost) <= 0:
            raise ValidationError("opening position requires positive cash-asset quantity and cost")
        if decimal(quantity) % decimal(spec.quantity_step) or acquired_on > self._event_time.date():
            raise ValidationError("invalid opening quantity step or acquisition date")
        cost = self._product_for_valuation(quantity, average_cost, spec.contract_multiplier)
        at = self._default_opened_at
        transaction = self._make_transaction(
            event_type=LedgerEventType.SETTLEMENT,
            reference_id=f"opening-position:{instrument_id}",
            idempotency_key=f"opening-position:{instrument_id}",
            event_time=at,
            postings=(
                self._posting(
                    "assets:position_cost",
                    spec.settlement_currency,
                    cost,
                    instrument_id=instrument_id,
                ),
                self._posting("equity:opening", spec.settlement_currency, cost.copy_negate()),
                self._posting(
                    "assets:position",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=instrument_id,
                    quantity_delta=decimal(quantity),
                    quantity_scale=quantity.scale,
                ),
                self._posting(
                    "memo:position_counter",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=instrument_id,
                    quantity_delta=decimal(quantity).copy_negate(),
                    quantity_scale=quantity.scale,
                ),
            ),
        )
        if self._import_seen(transaction):
            if self._position_lots.get(instrument_id) != [(acquired_on, decimal(quantity))]:
                raise ValidationError("opening acquisition date changed")
            return self.snapshot(self._event_time)
        if self._event_time != at or self._positions.get(instrument_id, 0):
            raise ValidationError("positions may only be imported before account activity")
        transaction_before = len(self._transactions)
        self._post(transaction)
        self._position_lots[instrument_id] = [(acquired_on, decimal(quantity))]
        self._marks[instrument_id] = (decimal(average_cost), at, transaction.reference_id)
        self._record_dividend_replay_fact(
            kind="opening_position",
            payload={
                "instrument_id": instrument_id,
                "quantity": {"units": quantity.units, "scale": quantity.scale},
                "average_cost": {"units": average_cost.units, "scale": average_cost.scale},
                "acquired_on": acquired_on.isoformat(),
            },
            transaction_count_before=transaction_before,
            transaction_count_after=len(self._transactions),
        )
        return self.snapshot(at)

    def _import_seen(self, transaction: LedgerTransaction) -> bool:
        if self._artifact_sink is not None:
            raise ValidationError("manual imports are unavailable during artifact streaming")
        if transaction.idempotency_key not in self._transaction_keys:
            return False
        previous = next(
            t for t in self._transactions if t.idempotency_key == transaction.idempotency_key
        )
        if previous != transaction:
            raise ValidationError("import ID reused with different content")
        return True

    def start_artifact_stream(self, sink: object) -> None:
        """Move journal retention to a bounded artifact sink after reset."""

        self._require_mutable()
        if self._artifact_sink is not None:
            raise ValidationError("ledger artifact stream is already active")
        if not callable(getattr(sink, "append", None)):
            raise ValidationError("artifact sink must provide append(stream, payload)")
        self._artifact_sink = sink
        for transaction in self._transactions:
            sink.append("ledger_transactions", ledger_transaction_bytes(transaction))
        self._transaction_count = len(self._transactions)
        self._transactions.clear()

    def finish_artifact_stream(self, *, journal_sha256: str | None = None) -> str | None:
        if self._artifact_sink is None:
            return self._finalized_journal_sha256
        if journal_sha256 is None:
            calculate = getattr(self._artifact_sink, "ledger_sha256", None)
            if not callable(calculate):
                raise ValidationError("artifact sink cannot finalize the ledger journal hash")
            journal_sha256 = calculate(fx_history=self._fx_history, marks=self._marks)
        if (
            not isinstance(journal_sha256, str)
            or len(journal_sha256) != 64
            or any(character not in "0123456789abcdef" for character in journal_sha256)
        ):
            raise ValidationError("finalized ledger journal hash must be lowercase SHA-256")
        self._finalized_journal_sha256 = journal_sha256
        self._artifact_sink = None
        self._artifact_stream_closed = True
        return journal_sha256

    def abort_artifact_stream(self) -> None:
        if self._artifact_sink is not None:
            self._artifact_sink = None
            self._artifact_stream_closed = True

    def _require_mutable(self) -> None:
        if self._artifact_stream_closed:
            raise ValidationError("ledger artifact stream is closed; reset is required")

    def capture_state(self) -> dict[str, object]:
        return deepcopy(
            {
                "transactions": self._transactions,
                "transaction_count": self._transaction_count,
                "artifact_stream_closed": self._artifact_stream_closed,
                "finalized_journal_sha256": self._finalized_journal_sha256,
                "transaction_keys": self._transaction_keys,
                "event_fingerprints": self._event_fingerprints,
                "fills": self._fills,
                "accounts": self._accounts,
                "positions": self._positions,
                "marks": self._marks,
                "mark_fingerprints": self._mark_fingerprints,
                "fill_trading_days": self._fill_trading_days,
                "position_lots": self._position_lots,
                "fill_close_allocations": self._fill_close_allocations,
                "dividend_entitlements": self._dividend_entitlements,
                "dividend_lifecycle_states": self._dividend_lifecycle_states,
                "dividend_execution_records": self._dividend_execution_records,
                "dividend_execution_by_key": self._dividend_execution_by_key,
                "dividend_phase_fingerprints": self._dividend_phase_fingerprints,
                "dividend_pit_fx_observations": self._dividend_pit_fx_observations,
                "dividend_pit_fx_by_event_id": self._dividend_pit_fx_by_event_id,
                "dividend_pit_fx_records": self._dividend_pit_fx_records,
                "recorded_dividend_valuations": self._recorded_dividend_valuations,
                "dividend_valuation_records": self._dividend_valuation_records,
                "dividend_operation_log": self._dividend_operation_log,
                "dividend_replay_facts": self._dividend_replay_facts,
                "dividend_replay_sequence": self._dividend_replay_sequence,
                "dividend_execution_mode": self._dividend_execution_mode,
                "fx_valuation_mode": self._fx_valuation_mode,
                "posting_cache": self._posting_cache,
                "fx": self._fx,
                "fx_history": self._fx_history,
                "event_time": self._event_time,
            }
        )

    def restore_state(self, state: dict[str, object]) -> None:
        """Restore a mutable checkpoint; sealed lifecycle recovery is engine-internal."""

        self._require_mutable()
        self._restore_captured_state(state)

    def _restore_captured_state(self, state: dict[str, object]) -> None:
        restored = deepcopy(state)
        self._transactions = restored["transactions"]
        self._transaction_count = restored["transaction_count"]
        self._artifact_stream_closed = restored["artifact_stream_closed"]
        self._finalized_journal_sha256 = restored["finalized_journal_sha256"]
        self._transaction_keys = restored["transaction_keys"]
        self._event_fingerprints = restored["event_fingerprints"]
        self._fills = restored["fills"]
        self._accounts = restored["accounts"]
        self._positions = restored["positions"]
        self._marks = restored["marks"]
        self._mark_fingerprints = restored["mark_fingerprints"]
        self._fill_trading_days = restored["fill_trading_days"]
        self._position_lots = restored["position_lots"]
        self._fill_close_allocations = restored["fill_close_allocations"]
        self._dividend_entitlements = restored["dividend_entitlements"]
        self._dividend_lifecycle_states = restored["dividend_lifecycle_states"]
        self._dividend_execution_records = restored["dividend_execution_records"]
        self._dividend_execution_by_key = restored["dividend_execution_by_key"]
        self._dividend_phase_fingerprints = restored["dividend_phase_fingerprints"]
        self._dividend_pit_fx_observations = restored["dividend_pit_fx_observations"]
        self._dividend_pit_fx_by_event_id = restored["dividend_pit_fx_by_event_id"]
        self._dividend_pit_fx_records = restored["dividend_pit_fx_records"]
        self._recorded_dividend_valuations = restored["recorded_dividend_valuations"]
        self._dividend_valuation_records = restored["dividend_valuation_records"]
        self._dividend_operation_log = restored["dividend_operation_log"]
        self._dividend_replay_facts = restored["dividend_replay_facts"]
        self._dividend_replay_sequence = restored["dividend_replay_sequence"]
        self._dividend_execution_mode = restored["dividend_execution_mode"]
        self._fx_valuation_mode = restored["fx_valuation_mode"]
        self._posting_cache = restored["posting_cache"]
        self._fx = restored["fx"]
        self._fx_history = restored["fx_history"]
        self._event_time = restored["event_time"]

    def capture_dividend_export_state(self) -> dict[str, object]:
        """Seal replay state and immutable construction inputs in one captured value."""

        return {
            "state": self.capture_state(),
            "account_id": self.account_id,
            "base_currency": self.base_currency,
            "instruments": dict(self.instruments),
            "initial_cash": dict(self._initial_cash),
            "money_scale": self.money_scale,
            "opened_at": self._default_opened_at,
            "dividend_execution_mode": self.dividend_execution_mode,
            "fx_valuation_mode": self.fx_valuation_mode,
            "entitlement_evidence_verifier": self._entitlement_evidence_verifier,
        }

    def _next_dividend_replay_sequence(self) -> int:
        sequence = self._dividend_replay_sequence
        self._dividend_replay_sequence += 1
        return sequence

    def _record_dividend_replay_fact(
        self,
        *,
        kind: str,
        payload: Mapping[str, object],
        transaction_count_before: int,
        transaction_count_after: int,
    ) -> None:
        if self.dividend_execution_mode is None:
            return
        self._dividend_replay_facts.append(
            {
                "operation_sequence": self._next_dividend_replay_sequence(),
                "kind": kind,
                "payload": deepcopy(dict(payload)),
                "transaction_count_before": transaction_count_before,
                "transaction_count_after": transaction_count_after,
            }
        )

    @staticmethod
    def _mark_replay_payload(
        event: MarketEvent,
        *,
        event_id: str,
        price: FixedPoint,
    ) -> dict[str, object]:
        return {
            "event_type": "mark_price",
            "event_id": event_id,
            "instrument_id": event.instrument_id,
            "event_time": event.event_time.isoformat().replace("+00:00", "Z"),
            "received_at": event.received_at.isoformat().replace("+00:00", "Z"),
            "available_at": event.available_at.isoformat().replace("+00:00", "Z"),
            "source": event.source,
            "trading_day": event.trading_day.isoformat(),
            "session_id": event.session_id,
            "sequence": event.sequence,
            "price": {"units": price.units, "scale": price.scale},
        }

    def _record_ledger_event_replay_fact(
        self,
        event: LedgerEvent,
        *,
        trading_day: date | None,
        transaction_count_before: int,
    ) -> None:
        event_kind = {
            Fill: "fill",
            Fee: "fee",
            Funding: "funding",
            Settlement: "settlement",
            CorporateActionEvent: "corporate_action",
        }[type(event)]
        payload = (
            market_event_payload(event)
            if isinstance(event, CorporateActionEvent)
            else execution_payload(event)
        )
        self._record_dividend_replay_fact(
            kind="ledger_event",
            payload={
                "event_kind": event_kind,
                "event": payload,
                "trading_day": trading_day.isoformat() if trading_day is not None else None,
            },
            transaction_count_before=transaction_count_before,
            transaction_count_after=len(self._transactions),
        )

    @property
    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return tuple(self._transactions)

    @property
    def account_id(self) -> str:
        return self._account_id

    @property
    def base_currency(self) -> str:
        return self._base_currency

    @property
    def money_scale(self) -> int:
        return self._money_scale

    @property
    def dividend_execution_mode(self) -> DividendExecutionMode | None:
        return self._dividend_execution_mode

    @property
    def fx_valuation_mode(self) -> FxValuationMode:
        return self._fx_valuation_mode

    @property
    def instruments(self) -> Mapping[str, InstrumentSpec]:
        return self._instruments

    @property
    def transaction_count(self) -> int:
        return (
            self._transaction_count
            if self._artifact_sink is not None or self._artifact_stream_closed
            else len(self._transactions)
        )

    @property
    def journal_sha256(self) -> str:
        if self._finalized_journal_sha256 is not None:
            return self._finalized_journal_sha256
        if self._artifact_sink is not None:
            raise ValidationError("ledger journal hash is unavailable until artifact finalization")
        if self._artifact_stream_closed:
            raise ValidationError(
                "ledger journal hash is unavailable after artifact abort; reset required"
            )
        if self._dividend_operation_log:
            payload = {
                "schema": DIVIDEND_JOURNAL_SCHEMA_ID,
                "execution_mode": self.dividend_execution_mode.value,
                "fx_valuation_mode": self.fx_valuation_mode.value,
                "pit_fx_observations": [item.to_dict() for item in self._dividend_pit_fx_records],
                "dividend_states": [
                    state.to_dict() for _, state in sorted(self._dividend_lifecycle_states.items())
                ],
                "dividend_records": [item.to_dict() for item in self._dividend_operation_log],
                "marks": [
                    {
                        "event_id": event_id,
                        "event_time": event_time.isoformat(),
                        "instrument_id": instrument_id,
                        "price": str(price),
                    }
                    for instrument_id, (price, event_time, event_id) in sorted(self._marks.items())
                ],
                "transactions": [
                    json.loads(self._transaction_bytes(transaction))
                    for transaction in self._transactions
                ],
            }
            return hashlib.sha256(canonical_bytes(payload)).hexdigest()
        digest = hashlib.sha256()
        digest.update(b'{"fx_snapshots":[')
        for index, (currency, rate, event_time) in enumerate(self._fx_history):
            if index:
                digest.update(b",")
            digest.update(
                (
                    "{"
                    f'"currency":{string_token(currency)},'
                    f'"event_time":{utc_token(event_time, zulu=False)},'
                    f'"rate":{string_token(str(rate))},'
                    f'"version":{index + 1}'
                    "}"
                ).encode()
            )
        digest.update(b'],"marks":[')
        for index, (instrument_id, (price, event_time, event_id)) in enumerate(
            sorted(self._marks.items())
        ):
            if index:
                digest.update(b",")
            digest.update(
                (
                    "{"
                    f'"event_id":{string_token(event_id)},'
                    f'"event_time":{utc_token(event_time, zulu=False)},'
                    f'"instrument_id":{string_token(instrument_id)},'
                    f'"price":{string_token(str(price))}'
                    "}"
                ).encode()
            )
        digest.update(b'],"transactions":[')
        for index, transaction in enumerate(self._transactions):
            if index:
                digest.update(b",")
            digest.update(self._transaction_bytes(transaction))
        digest.update(b"]}")
        return digest.hexdigest()

    @staticmethod
    def _transaction_bytes(transaction: LedgerTransaction) -> bytes:
        postings: list[str] = []
        for posting in transaction.postings:
            instrument_id = (
                "null" if posting.instrument_id is None else string_token(posting.instrument_id)
            )
            postings.append(
                "{"
                f'"amount":{fixed_token(posting.amount)},'
                f'"currency":{string_token(posting.currency)},'
                f'"instrument_id":{instrument_id},'
                f'"ledger_account":{string_token(posting.ledger_account)},'
                f'"quantity_delta":{fixed_token(posting.quantity_delta)}'
                "}"
            )
        return (
            "{"
            f'"event_time":{utc_token(transaction.event_time)},'
            f'"event_type":{string_token(transaction.event_type.value)},'
            f'"idempotency_key":{string_token(transaction.idempotency_key)},'
            f'"postings":[{",".join(postings)}],'
            f'"reference_id":{string_token(transaction.reference_id)},'
            f'"transaction_id":{string_token(transaction.transaction_id)}'
            "}"
        ).encode()

    def set_fx_rate(self, currency: str, rate: FixedPoint, *, event_time: datetime) -> None:
        self._require_mutable()
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            raise ValidationError("EVIDENCED_PIT_REJECTS_LEGACY_FX")
        currency = _currency(currency)
        event_time = ensure_utc_datetime(event_time, field="event_time")
        value = decimal(rate)
        if value <= 0:
            raise ValidationError("FX rate must be positive")
        if currency == self.base_currency:
            if value != 1:
                raise ValidationError("base currency FX rate must remain exactly one")
            # The base unit is an invariant rather than a versioned market quote.
            return
        prior = self._fx.get(currency)
        if prior is not None:
            if event_time < prior[1]:
                raise ValidationError("FX snapshot time moved backwards")
            if event_time == prior[1]:
                if value != prior[0]:
                    raise ValidationError("FX snapshot conflicts at the same event_time")
                return
        self._fx[currency] = (value, event_time)
        self._fx_history.append((currency, value, event_time))

    def convert_to_base(
        self, amount: Decimal | FixedPoint, currency: str, *, event_time: datetime
    ) -> Decimal:
        return self._convert_for_valuation(
            amount,
            _currency(currency),
            ensure_utc_datetime(event_time, field="event_time"),
        )

    def apply_dividend_lifecycle(self, request: DividendExecutionRequest):
        return apply_dividend_lifecycle_request(self, request)

    def observe_pit_fx(self, rate) -> PitFxObservationRecord:
        return observe_dividend_pit_fx(self, rate)

    def dividend_exposure(self, *, as_of: datetime) -> DividendExposureSnapshot:
        return build_dividend_exposure(self, as_of=as_of)

    def record_dividend_valuation(self, *, as_of: datetime) -> DividendValuationRecord:
        return build_dividend_valuation(self, as_of=as_of)

    def cash_balance(self, currency: str) -> Decimal:
        return self._accounts.get(("assets:cash", currency, None), Decimal(0))

    def dividend_receivable_balance(
        self,
        currency: str,
        *,
        instrument_id: str | None = None,
    ) -> Decimal:
        """Return declared cash dividends that have not reached their payment date."""

        currency = _currency(currency)
        dividend_keys = {
            f"dividend:{state.dividend_id}"
            for state in self._dividend_lifecycle_states.values()
            if state.instrument_id == instrument_id
        }
        values = (
            amount
            for (account, entry_currency, receivable_key), amount in self._accounts.items()
            if account == "assets:dividend_receivable"
            and entry_currency == currency
            and (
                instrument_id is None
                or (
                    receivable_key is not None
                    and (
                        receivable_key.startswith(f"{instrument_id}@")
                        or receivable_key in dividend_keys
                    )
                )
            )
        )
        return (
            sum_decimal_exact(values)
            if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT
            else sum(values, Decimal(0))
        )

    def _dividend_receivable_value(self, event_time: datetime) -> Decimal:
        return self._sum_for_valuation(
            self._convert_for_valuation(amount, currency, event_time)
            for (account, currency, _), amount in self._accounts.items()
            if account == "assets:dividend_receivable"
        )

    @property
    def has_open_derivative_position(self) -> bool:
        return any(
            quantity != 0 and instrument_id in self._derivative_instruments
            for instrument_id, quantity in self._positions.items()
        )

    def risk_balances(
        self, event_time: datetime
    ) -> tuple[dict[str, Decimal], Mapping[str, Decimal], Decimal, Decimal]:
        """Return exact decimal balances needed by the hot pre-trade risk path."""
        event_time = ensure_utc_datetime(event_time, field="event_time")
        self._require_valuation_time(event_time)
        cash = {
            currency: amount
            for (account, currency, instrument_id), amount in self._accounts.items()
            if account == "assets:cash" and instrument_id is None
        }
        nav = self._sum_for_valuation(
            self._convert_for_valuation(amount, currency, event_time)
            for currency, amount in cash.items()
        )
        nav = add_decimal_exact(nav, self._dividend_receivable_value(event_time))
        initial_margin = Decimal(0)
        for instrument_id, quantity in self._positions.items():
            spec = self._spec(instrument_id)
            average = self._average_cost(instrument_id)
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                nav = add_decimal_exact(
                    nav,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            self._difference_for_valuation(mark, average), quantity, multiplier
                        ),
                        spec.settlement_currency,
                        event_time,
                    ),
                )
                initial_margin = add_decimal_exact(
                    initial_margin,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            self._product_for_valuation(mark, quantity, multiplier).copy_abs(),
                            _meta_decimal(spec, "initial_margin_rate"),
                        ),
                        spec.settlement_currency,
                        event_time,
                    ),
                )
            else:
                nav = add_decimal_exact(
                    nav,
                    self._convert_for_valuation(
                        self._product_for_valuation(mark, quantity, multiplier),
                        spec.settlement_currency,
                        event_time,
                    ),
                )
        return cash, MappingProxyType(self._positions), nav, initial_margin

    def portfolio_risk_snapshot(self, event_time: datetime) -> PortfolioRiskSnapshot:
        """Build an exact, read-only base-currency exposure view at one PIT timestamp."""
        at = ensure_utc_datetime(event_time, field="event_time")
        self._require_valuation_time(at)
        account = self.snapshot(at)
        cash_value = self._sum_for_valuation(
            self._convert_for_valuation(decimal(amount), currency, at)
            for currency, amount in account.cash_balances.items()
        )
        positions: list[PositionRiskSnapshot] = []
        gross_exposure = Decimal(0)
        net_exposure = Decimal(0)
        initial_margin = Decimal(0)
        maintenance_margin = Decimal(0)
        for instrument_id, quantity_fp in sorted(account.positions.items()):
            quantity = decimal(quantity_fp)
            if quantity == 0:
                continue
            spec = self._spec(instrument_id)
            mark = self._mark_price(instrument_id)
            multiplier = decimal(spec.contract_multiplier)
            local_notional = self._product_for_valuation(mark, quantity, multiplier)
            base_notional = self._convert_for_valuation(
                local_notional, spec.settlement_currency, at
            )
            position_initial = Decimal(0)
            position_maintenance = Decimal(0)
            if self._is_derivative(spec):
                for key in ("initial_margin_rate", "maintenance_margin_rate"):
                    if key not in spec.metadata:
                        raise ValidationError(
                            f"InstrumentSpec metadata {key!r} is required for risk snapshot"
                        )
                absolute_notional = abs(local_notional)
                position_initial = self._convert_for_valuation(
                    self._product_for_valuation(
                        absolute_notional, _meta_decimal(spec, "initial_margin_rate")
                    ),
                    spec.settlement_currency,
                    at,
                )
                position_maintenance = self._convert_for_valuation(
                    self._product_for_valuation(
                        absolute_notional, _meta_decimal(spec, "maintenance_margin_rate")
                    ),
                    spec.settlement_currency,
                    at,
                )
            gross_exposure = add_decimal_exact(gross_exposure, base_notional.copy_abs())
            net_exposure = add_decimal_exact(net_exposure, base_notional)
            initial_margin = add_decimal_exact(initial_margin, position_initial)
            maintenance_margin = add_decimal_exact(maintenance_margin, position_maintenance)
            positions.append(
                PositionRiskSnapshot(
                    instrument_id=instrument_id,
                    asset_class=spec.asset_class,
                    venue=spec.venue,
                    settlement_currency=spec.settlement_currency,
                    quantity=quantity_fp,
                    mark_price=self._fixed_for_valuation(mark, spec.price_tick.scale),
                    base_notional=self._fixed_for_valuation(base_notional, self.money_scale),
                    initial_margin=self._fixed_for_valuation(position_initial, self.money_scale),
                    maintenance_margin=self._fixed_for_valuation(
                        position_maintenance, self.money_scale
                    ),
                )
            )
        return PortfolioRiskSnapshot(
            account_id=account.account_id,
            event_time=at,
            base_currency=account.base_currency,
            nav=account.nav,
            cash_value=self._fixed_for_valuation(cash_value, self.money_scale),
            gross_exposure=self._fixed_for_valuation(gross_exposure, self.money_scale),
            net_exposure=self._fixed_for_valuation(net_exposure, self.money_scale),
            initial_margin=self._fixed_for_valuation(initial_margin, self.money_scale),
            maintenance_margin=self._fixed_for_valuation(maintenance_margin, self.money_scale),
            positions=tuple(positions),
        )

    def mark(
        self, event: MarkPriceEvent, *, create_snapshot: bool = True
    ) -> AccountSnapshot | None:
        self._require_mutable()
        if event.instrument_id not in self.instruments:
            raise ValidationError(f"missing InstrumentSpec for {event.instrument_id}")
        prior = self._mark_fingerprints.get(event.event_id)
        if prior is not None:
            if prior != event:
                raise ValidationError("mark event_id reused with different content")
            return self.snapshot(self._event_time) if create_snapshot else None
        current = self._marks.get(event.instrument_id)
        if current is not None and event.available_at < current[1]:
            raise ValidationError("mark price time moved backwards")
        if event.available_at < self._event_time:
            raise ValidationError("ledger event time moved backwards")
        prior_mark = self._marks.get(event.instrument_id)
        prior_time = self._event_time
        transaction_before = len(self._transactions)
        try:
            self._marks[event.instrument_id] = (
                decimal(event.price),
                event.available_at,
                event.event_id,
            )
            self._mark_fingerprints[event.event_id] = event
            self._event_time = max(self._event_time, event.available_at)
            result = self.snapshot(event.available_at) if create_snapshot else None
            self._record_dividend_replay_fact(
                kind="mark",
                payload=self._mark_replay_payload(
                    event,
                    event_id=event.event_id,
                    price=event.price,
                ),
                transaction_count_before=transaction_before,
                transaction_count_after=len(self._transactions),
            )
            return result
        except Exception:
            if prior_mark is None:
                self._marks.pop(event.instrument_id, None)
            else:
                self._marks[event.instrument_id] = prior_mark
            self._mark_fingerprints.pop(event.event_id, None)
            self._event_time = prior_time
            raise

    def observe_market(
        self,
        event: MarketEvent,
        *,
        create_snapshot: bool = True,
        trusted_unique: bool = False,
    ) -> AccountSnapshot | None:
        self._require_mutable()
        if isinstance(event, MarkPriceEvent):
            return self.mark(event, create_snapshot=create_snapshot)
        price: FixedPoint | None = None
        if isinstance(event, TradeEvent):
            price = event.price
        elif isinstance(event, BarEvent):
            price = event.close_price
        elif isinstance(event, QuoteEvent):
            midpoint = (decimal(event.bid_price) + decimal(event.ask_price)) / 2
            price = fixed(midpoint, event.bid_price.scale)
        elif isinstance(event, BookSnapshotEvent):
            midpoint = (decimal(event.bids[0].price) + decimal(event.asks[0].price)) / 2
            price = fixed(midpoint, event.bids[0].price.scale)
        if price is None:
            return self.snapshot(event.available_at) if create_snapshot else None
        synthetic_id = f"mark:{event.event_id}"
        if not trusted_unique:
            prior = self._mark_fingerprints.get(synthetic_id)
            if prior is not None:
                if prior != event:
                    raise ValidationError("mark event_id reused with different content")
                return self.snapshot(self._event_time) if create_snapshot else None
        current = self._marks.get(event.instrument_id)
        if current is not None and event.available_at < current[1]:
            raise ValidationError("mark price time moved backwards")
        if event.available_at < self._event_time:
            raise ValidationError("ledger event time moved backwards")
        prior_mark = self._marks.get(event.instrument_id)
        prior_time = self._event_time
        transaction_before = len(self._transactions)
        try:
            self._marks[event.instrument_id] = (decimal(price), event.available_at, synthetic_id)
            if not trusted_unique:
                self._mark_fingerprints[synthetic_id] = event
            self._event_time = max(self._event_time, event.available_at)
            result = self.snapshot(event.available_at) if create_snapshot else None
            self._record_dividend_replay_fact(
                kind="mark",
                payload=self._mark_replay_payload(
                    event,
                    event_id=synthetic_id,
                    price=price,
                ),
                transaction_count_before=transaction_before,
                transaction_count_after=len(self._transactions),
            )
            return result
        except Exception:
            if prior_mark is None:
                self._marks.pop(event.instrument_id, None)
            else:
                self._marks[event.instrument_id] = prior_mark
            if not trusted_unique:
                self._mark_fingerprints.pop(synthetic_id, None)
            self._event_time = prior_time
            raise

    def liquidation_required(self, event_time: datetime | None = None) -> bool:
        """Evaluate the maintenance boundary without materializing reporting maps."""
        at = (
            ensure_utc_datetime(event_time, field="event_time")
            if event_time is not None
            else self._event_time
        )
        self._require_valuation_time(at)
        has_derivative = any(
            quantity and instrument_id in self._derivative_instruments
            for instrument_id, quantity in self._positions.items()
        )
        if not has_derivative and self.fx_valuation_mode is FxValuationMode.LEGACY:
            return False
        nav = self._sum_for_valuation(
            self._convert_for_valuation(amount, currency, at)
            for (account, currency, instrument_id), amount in self._accounts.items()
            if account == "assets:cash" and instrument_id is None
        )
        nav = add_decimal_exact(nav, self._dividend_receivable_value(at))
        maintenance_margin = Decimal(0)
        for instrument_id, quantity in self._positions.items():
            spec = self._spec(instrument_id)
            average = self._average_cost(instrument_id)
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                nav = add_decimal_exact(
                    nav,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            self._difference_for_valuation(mark, average),
                            quantity,
                            multiplier,
                        ),
                        spec.settlement_currency,
                        at,
                    ),
                )
                maintenance_margin = add_decimal_exact(
                    maintenance_margin,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            self._product_for_valuation(mark, quantity, multiplier).copy_abs(),
                            _meta_decimal(spec, "maintenance_margin_rate"),
                        ),
                        spec.settlement_currency,
                        at,
                    ),
                )
            else:
                nav = add_decimal_exact(
                    nav,
                    self._convert_for_valuation(
                        self._product_for_valuation(mark, quantity, multiplier),
                        spec.settlement_currency,
                        at,
                    ),
                )
        rounded_nav = self._fixed_for_valuation(nav, self.money_scale)
        rounded_maintenance = self._fixed_for_valuation(maintenance_margin, self.money_scale)
        return rounded_maintenance.units > 0 and rounded_nav.units <= rounded_maintenance.units

    def apply_corporate_action(self, action, *, at):
        """Apply evidenced cross-market terms atomically without synthetic trades."""
        from quant_execution.corporate_actions import apply_action

        return apply_action(self, action, at=at)

    def apply(self, event: LedgerEvent, *, create_snapshot: bool = True) -> AccountSnapshot | None:
        trading_day = event.event_time.date() if isinstance(event, Fill) else None
        reference_id = self._event_identity(event)
        is_new = reference_id not in self._event_fingerprints
        transaction_before = len(self._transactions)
        result = self._apply(
            event,
            trading_day=trading_day,
            create_snapshot=create_snapshot,
        )
        if is_new:
            self._record_ledger_event_replay_fact(
                event,
                trading_day=trading_day,
                transaction_count_before=transaction_before,
            )
        return result

    def _apply(
        self,
        event: LedgerEvent,
        *,
        trading_day: date | None,
        create_snapshot: bool,
        local_rollback: bool = True,
        trusted_unique: bool = False,
    ) -> AccountSnapshot | None:
        self._require_mutable()
        self._validate_event(event)
        reference_id = self._event_identity(event)
        prior = None if trusted_unique else self._event_fingerprints.get(reference_id)
        if prior is not None:
            if isinstance(prior, bytes):
                if prior != self._event_fingerprint(event):
                    raise ValidationError("ledger event id reused with different content")
            elif prior != event:
                raise ValidationError("ledger event id reused with different content")
            if isinstance(event, Fill) and self._fill_trading_days[event.fill_id] != trading_day:
                raise ValidationError("fill trading_day changed across idempotent application")
            return self.snapshot(self._event_time) if create_snapshot else None
        event_time = (
            event.available_at if isinstance(event, CorporateActionEvent) else event.event_time
        )
        if event_time < self._event_time:
            raise ValidationError("ledger event time moved backwards")
        settlement_price: Decimal | None = None
        lot_update: tuple[list[tuple[date, Decimal]], Decimal, Decimal] | None = None
        if isinstance(event, Fill):
            if trading_day is None:
                raise ValidationError("fill application requires a trading_day")
            lot_update = self._prepare_lot_update(event, trading_day)
        elif isinstance(event, Settlement) and event.settlement_type == "daily_mark":
            settlement_price = self._settlement_price(event)
        elif isinstance(event, CorporateActionEvent):
            self._validate_corporate_action(event)
        posting_cache_size = len(self._posting_cache)
        transaction: LedgerTransaction | None = None
        undo: dict[str, object] | None = None
        try:
            transaction = self._translate(event)
            if local_rollback:
                undo = self._capture_apply_undo(event, transaction, reference_id)
            # Corporate-action announcements are market events even when this
            # account holds no eligible shares. Preserve event identity, time and
            # split state, but do not invent a zero-effect accounting transaction.
            no_effect_action = isinstance(event, CorporateActionEvent) and not any(
                posting.amount.units
                or (posting.quantity_delta is not None and posting.quantity_delta.units)
                for posting in transaction.postings
            )
            if not no_effect_action:
                if local_rollback:
                    self._post(transaction)
                else:
                    self._post(transaction, local_rollback=False)
            if isinstance(event, Fill):
                self._fills[event.fill_id] = event
                self._fill_trading_days[event.fill_id] = trading_day
                assert lot_update is not None
                lots, prior_close, today_close = lot_update
                self._position_lots[event.instrument_id] = lots
                self._fill_close_allocations[event.fill_id] = (prior_close, today_close)
            elif isinstance(event, Settlement) and settlement_price is not None:
                self._marks[event.instrument_id] = (
                    settlement_price,
                    event.event_time,
                    event.settlement_id,
                )
            elif isinstance(event, CorporateActionEvent):
                self._apply_dividend_state(event)
                if event.ratio is not None:
                    self._apply_split_state(event)
            if not trusted_unique:
                self._event_fingerprints[reference_id] = (
                    self._event_fingerprint(event) if self._artifact_sink is not None else event
                )
            self._event_time = transaction.event_time
            if isinstance(event, Fee) and self._artifact_sink is not None:
                fill = self._fills.pop(event.fill_id, None)
                if fill is not None:
                    spec = self._spec(fill.instrument_id)
                    if spec.asset_class is not AssetClass.FUTURE:
                        self._fill_close_allocations.pop(event.fill_id, None)
            return self.snapshot(transaction.event_time) if create_snapshot else None
        except Exception:
            if transaction is not None and undo is not None:
                self._rollback_apply(event, transaction, reference_id, undo)
            while len(self._posting_cache) > posting_cache_size:
                self._posting_cache.popitem()
            raise

    def _capture_apply_undo(
        self,
        event: LedgerEvent,
        transaction: LedgerTransaction,
        reference_id: str,
    ) -> dict[str, object]:
        account_keys = {
            (posting.ledger_account, posting.currency, posting.instrument_id)
            for posting in transaction.postings
        }
        position_ids = {
            posting.instrument_id
            for posting in transaction.postings
            if posting.ledger_account == "assets:position"
            and posting.instrument_id is not None
            and posting.quantity_delta is not None
        }
        instrument_id = getattr(event, "instrument_id", None)
        entitlement_key = (
            self._dividend_entitlement_key(event)
            if isinstance(event, CorporateActionEvent)
            and event.action_type in {"cash_dividend_entitlement", "cash_dividend_payment"}
            else None
        )
        return {
            "transaction_count": len(self._transactions),
            "transaction_key": transaction.idempotency_key in self._transaction_keys,
            "accounts": {key: self._accounts.get(key, _MISSING) for key in account_keys},
            "positions": {
                instrument: self._positions.get(instrument, _MISSING) for instrument in position_ids
            },
            "event_fingerprint": self._event_fingerprints.get(reference_id, _MISSING),
            "fill": (
                self._fills.get(event.fill_id, _MISSING) if isinstance(event, Fill) else _MISSING
            ),
            "fill_trading_day": (
                self._fill_trading_days.get(event.fill_id, _MISSING)
                if isinstance(event, Fill)
                else _MISSING
            ),
            "fill_close_allocation": (
                self._fill_close_allocations.get(event.fill_id, _MISSING)
                if isinstance(event, Fill)
                else _MISSING
            ),
            "position_lots": (
                self._position_lots.get(instrument_id, _MISSING)
                if instrument_id is not None
                else _MISSING
            ),
            "mark": (
                self._marks.get(instrument_id, _MISSING) if instrument_id is not None else _MISSING
            ),
            "dividend_entitlement_key": entitlement_key,
            "dividend_entitlement": (
                self._dividend_entitlements.get(entitlement_key, _MISSING)
                if entitlement_key is not None
                else _MISSING
            ),
            "event_time": self._event_time,
        }

    def _rollback_apply(
        self,
        event: LedgerEvent,
        transaction: LedgerTransaction,
        reference_id: str,
        undo: dict[str, object],
    ) -> None:
        transaction_count = int(undo["transaction_count"])
        del self._transactions[transaction_count:]
        if not undo["transaction_key"]:
            self._transaction_keys.discard(transaction.idempotency_key)
        self._restore_values(self._accounts, undo["accounts"])
        self._restore_values(self._positions, undo["positions"])
        self._restore_value(
            self._event_fingerprints,
            reference_id,
            undo["event_fingerprint"],
        )
        instrument_id = getattr(event, "instrument_id", None)
        if isinstance(event, Fill):
            self._restore_value(self._fills, event.fill_id, undo["fill"])
            self._restore_value(
                self._fill_trading_days,
                event.fill_id,
                undo["fill_trading_day"],
            )
            self._restore_value(
                self._fill_close_allocations,
                event.fill_id,
                undo["fill_close_allocation"],
            )
        if instrument_id is not None:
            self._restore_value(
                self._position_lots,
                instrument_id,
                undo["position_lots"],
            )
            self._restore_value(self._marks, instrument_id, undo["mark"])
        entitlement_key = undo["dividend_entitlement_key"]
        if entitlement_key is not None:
            self._restore_value(
                self._dividend_entitlements,
                entitlement_key,
                undo["dividend_entitlement"],
            )
        self._event_time = undo["event_time"]

    @staticmethod
    def _restore_values(mapping: dict, values: object) -> None:
        assert isinstance(values, dict)
        for key, value in values.items():
            ExactAccountLedger._restore_value(mapping, key, value)

    @staticmethod
    def _restore_value(mapping: dict, key: object, value: object) -> None:
        if value is _MISSING:
            mapping.pop(key, None)
        else:
            mapping[key] = value

    def apply_with_trading_day(
        self,
        event: LedgerEvent,
        *,
        trading_day: object,
        create_snapshot: bool = True,
    ) -> AccountSnapshot | None:
        if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
            raise ValidationError("trading_day must be a date")
        applied_trading_day = trading_day if isinstance(event, Fill) else None
        reference_id = self._event_identity(event)
        is_new = reference_id not in self._event_fingerprints
        transaction_before = len(self._transactions)
        result = self._apply(
            event,
            trading_day=applied_trading_day,
            create_snapshot=create_snapshot,
        )
        if is_new:
            self._record_ledger_event_replay_fact(
                event,
                trading_day=applied_trading_day,
                transaction_count_before=transaction_before,
            )
        return result

    def _apply_replay_event(
        self,
        event: LedgerEvent,
        *,
        trading_day: date | None = None,
    ) -> None:
        """Apply one validated fact under the engine's whole-replay rollback boundary."""

        if isinstance(event, Fill):
            if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
                raise ValidationError("fill replay application requires a trading_day date")
        elif trading_day is not None:
            raise ValidationError("trading_day is only valid for fill replay application")
        transaction_before = len(self._transactions)
        self._apply(
            event,
            trading_day=trading_day,
            create_snapshot=False,
            local_rollback=False,
            trusted_unique=True,
        )
        self._record_ledger_event_replay_fact(
            event,
            trading_day=trading_day,
            transaction_count_before=transaction_before,
        )

    def _validate_event(self, event: LedgerEvent) -> None:
        if isinstance(event, CorporateActionEvent):
            return
        if event.account_id != self.account_id:
            raise ValidationError("ledger event account differs from ledger account")
        if isinstance(event, Fee):
            fill = self._fills.get(event.fill_id)
            if fill is None:
                raise ValidationError("fee references a fill not yet applied to the ledger")
            spec = self._spec(fill.instrument_id)
            if event.currency != spec.settlement_currency:
                raise ValidationError("fee currency differs from instrument settlement currency")
            if not self._is_derivative(spec) and decimal(event.amount) > 0:
                cash = self._accounts.get(("assets:cash", event.currency, None), Decimal(0))
                if cash < decimal(event.amount):
                    raise ValidationError("cash asset fee would create negative cash")
        elif isinstance(event, (Funding, Settlement)):
            spec = self._spec(event.instrument_id)
            if event.currency != spec.settlement_currency:
                raise ValidationError(
                    "ledger event currency differs from instrument settlement currency"
                )

    def funding_from_market(self, event: FundingRateEvent) -> Funding | None:
        spec = self._spec(event.instrument_id)
        position = self._positions.get(event.instrument_id, Decimal(0))
        if position == 0:
            return None
        mark = self._mark_price(event.instrument_id)
        multiplier = decimal(spec.contract_multiplier)
        amount = -(position * mark * multiplier * Decimal(str(event.rate)))
        return Funding(
            funding_id=_identifier("funding", event.event_id, self.account_id),
            account_id=self.account_id,
            instrument_id=event.instrument_id,
            amount=fixed(amount, self.money_scale),
            currency=spec.settlement_currency,
            event_time=event.available_at,
        )

    def settlement_from_market(self, event: StatusEvent) -> Settlement | None:
        """Translate an explicit daily-settlement status into an exact ledger event."""
        if event.status.lower() != "daily_settlement":
            return None
        spec = self._spec(event.instrument_id)
        if not self._is_derivative(spec):
            raise ValidationError("daily_settlement status requires a derivative instrument")
        quantity = self._positions.get(event.instrument_id, Decimal(0))
        if quantity == 0:
            return None
        settlement_price = self._mark_price(event.instrument_id)
        amount = (
            (settlement_price - self._average_cost(event.instrument_id))
            * quantity
            * decimal(spec.contract_multiplier)
        )
        return Settlement(
            settlement_id=_identifier("settlement", event.event_id, self.account_id),
            account_id=self.account_id,
            instrument_id=event.instrument_id,
            amount=fixed(amount, self.money_scale),
            currency=spec.settlement_currency,
            event_time=event.available_at,
            settlement_type="daily_mark",
            settlement_price=fixed(settlement_price, spec.price_tick.scale),
        )

    def snapshot(self, event_time: datetime | None = None) -> AccountSnapshot:
        at = (
            ensure_utc_datetime(event_time, field="event_time")
            if event_time is not None
            else self._event_time
        )
        self._require_valuation_time(at)
        cash: dict[str, FixedPoint] = {}
        for (account, currency, instrument_id), amount in self._accounts.items():
            if account == "assets:cash" and instrument_id is None:
                cash[currency] = fixed(
                    add_decimal_exact(
                        decimal(cash.get(currency, FixedPoint(0, self.money_scale))),
                        amount,
                    ),
                    self.money_scale,
                    rounding=(
                        None
                        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT
                        else ROUND_HALF_EVEN
                    ),
                )
        positions: dict[str, FixedPoint] = {}
        costs: dict[str, FixedPoint] = {}
        realized: dict[str, FixedPoint] = {}
        unrealized: dict[str, FixedPoint] = {}
        nav = self._sum_for_valuation(
            self._convert_for_valuation(value, currency, at) for currency, value in cash.items()
        )
        nav = add_decimal_exact(nav, self._dividend_receivable_value(at))
        initial_margin = Decimal(0)
        maintenance_margin = Decimal(0)
        for instrument_id, quantity in sorted(self._positions.items()):
            spec = self._spec(instrument_id)
            positions[instrument_id] = self._fixed_for_valuation(quantity, spec.quantity_step.scale)
            average = self._average_cost(instrument_id)
            costs[instrument_id] = self._fixed_for_valuation(average, spec.price_tick.scale)
            realized_value = self._realized(instrument_id)
            realized[instrument_id] = self._fixed_for_valuation(
                self._convert_for_valuation(realized_value, spec.settlement_currency, at),
                self.money_scale,
            )
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            pnl = self._product_for_valuation(
                self._difference_for_valuation(mark, average), quantity, multiplier
            )
            unrealized[instrument_id] = self._fixed_for_valuation(
                self._convert_for_valuation(pnl, spec.settlement_currency, at),
                self.money_scale,
            )
            if self._is_derivative(spec):
                nav = add_decimal_exact(
                    nav, self._convert_for_valuation(pnl, spec.settlement_currency, at)
                )
                notional = self._product_for_valuation(mark, quantity, multiplier).copy_abs()
                initial_margin = add_decimal_exact(
                    initial_margin,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            notional, _meta_decimal(spec, "initial_margin_rate")
                        ),
                        spec.settlement_currency,
                        at,
                    ),
                )
                maintenance_margin = add_decimal_exact(
                    maintenance_margin,
                    self._convert_for_valuation(
                        self._product_for_valuation(
                            notional, _meta_decimal(spec, "maintenance_margin_rate")
                        ),
                        spec.settlement_currency,
                        at,
                    ),
                )
            else:
                nav = add_decimal_exact(
                    nav,
                    self._convert_for_valuation(
                        self._product_for_valuation(mark, quantity, multiplier),
                        spec.settlement_currency,
                        at,
                    ),
                )
        nav_value = self._fixed_for_valuation(nav, self.money_scale)
        maintenance = self._fixed_for_valuation(maintenance_margin, self.money_scale)
        snapshot = AccountSnapshot(
            account_id=self.account_id,
            event_time=at,
            base_currency=self.base_currency,
            cash_balances=cash,
            positions=positions,
            nav=nav_value,
            cost_basis=costs,
            realized_pnl=realized,
            unrealized_pnl=unrealized,
            initial_margin=self._fixed_for_valuation(initial_margin, self.money_scale),
            maintenance_margin=maintenance,
            liquidation_required=maintenance.units > 0 and nav_value.units <= maintenance.units,
        )
        self.assert_nav_residual(snapshot)
        return snapshot

    def assert_nav_residual(self, snapshot: AccountSnapshot) -> None:
        expected = Decimal(0)
        at = snapshot.event_time
        self._require_valuation_time(at)
        for currency, balance in snapshot.cash_balances.items():
            expected = add_decimal_exact(
                expected, self._convert_for_valuation(decimal(balance), currency, at)
            )
        expected = add_decimal_exact(expected, self._dividend_receivable_value(at))
        for instrument_id, quantity_fp in snapshot.positions.items():
            spec = self._spec(instrument_id)
            quantity = decimal(quantity_fp)
            mark = self._mark_price(instrument_id, fallback=self._average_cost(instrument_id))
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                component = self._product_for_valuation(
                    self._difference_for_valuation(mark, self._average_cost(instrument_id)),
                    quantity,
                    multiplier,
                )
            else:
                component = self._product_for_valuation(mark, quantity, multiplier)
            expected = add_decimal_exact(
                expected, self._convert_for_valuation(component, spec.settlement_currency, at)
            )
        residual = self._difference_for_valuation(decimal(snapshot.nav), expected).copy_abs()
        tolerance = max(
            self._product_for_valuation(decimal(snapshot.nav).copy_abs(), Decimal("1e-8")),
            Decimal("0.01"),
        )
        if residual > tolerance:
            raise ValidationError(f"NAV residual {residual} exceeds tolerance {tolerance}")

    def acquired_today(self, instrument_id: str, trading_day: object) -> Decimal:
        return sum(
            (
                abs(quantity)
                for day, quantity in self._position_lots.get(instrument_id, ())
                if day == trading_day
            ),
            Decimal(0),
        )

    def close_allocation(self, fill_id: str) -> tuple[Decimal, Decimal]:
        """Return deterministic `(prior_day, today)` close quantities for a fill."""
        try:
            return self._fill_close_allocations[fill_id]
        except KeyError as exc:
            raise ValidationError(f"missing close allocation for fill_id: {fill_id}") from exc

    def _prepare_lot_update(
        self, fill: Fill, trading_day: date
    ) -> tuple[list[tuple[date, Decimal]], Decimal, Decimal]:
        signed = decimal(fill.quantity) if fill.side is Side.BUY else -decimal(fill.quantity)
        lots = list(self._position_lots.get(fill.instrument_id, ()))
        remaining = abs(signed)
        prior_close = Decimal(0)
        today_close = Decimal(0)
        if lots and lots[0][1] * signed < 0:
            updated: list[tuple[date, Decimal]] = []
            for lot_day, lot_quantity in sorted(lots, key=lambda item: item[0]):
                if remaining == 0:
                    updated.append((lot_day, lot_quantity))
                    continue
                close_quantity = min(abs(lot_quantity), remaining)
                if lot_day == trading_day:
                    today_close += close_quantity
                else:
                    prior_close += close_quantity
                residual = abs(lot_quantity) - close_quantity
                if residual:
                    updated.append((lot_day, residual if lot_quantity > 0 else -residual))
                remaining -= close_quantity
            lots = updated
        if remaining:
            opening = remaining if signed > 0 else -remaining
            for index, (lot_day, lot_quantity) in enumerate(lots):
                if lot_day == trading_day and lot_quantity * opening > 0:
                    lots[index] = (lot_day, lot_quantity + opening)
                    break
            else:
                lots.append((trading_day, opening))
        return lots, prior_close, today_close

    def _apply_split_state(self, event: CorporateActionEvent) -> None:
        if event.action_type == "terminal_cash":
            self._position_lots[event.instrument_id] = []
            self._marks.pop(event.instrument_id, None)
            return
        ratio = decimal(event.ratio)
        self._position_lots[event.instrument_id] = [
            (day, quantity * ratio)
            for day, quantity in self._position_lots.get(event.instrument_id, ())
        ]
        mark = self._marks.get(event.instrument_id)
        if mark is not None:
            self._marks[event.instrument_id] = (
                mark[0] / ratio,
                event.available_at,
                event.event_id,
            )

    @staticmethod
    def _dividend_entitlement_key(event: CorporateActionEvent) -> tuple[str, str, date]:
        assert event.currency is not None
        return (event.instrument_id, str(event.currency), event.effective_date)

    @staticmethod
    def _dividend_receivable_instrument(key: tuple[str, str, date]) -> str:
        return f"{key[0]}@{key[2].isoformat()}"

    def _apply_dividend_state(self, event: CorporateActionEvent) -> None:
        if event.action_type not in {"cash_dividend_entitlement", "cash_dividend_payment"}:
            return
        key = self._dividend_entitlement_key(event)
        if event.action_type == "cash_dividend_payment":
            del self._dividend_entitlements[key]
            return
        assert event.cash_amount is not None
        receivable_key = self._dividend_receivable_instrument(key)
        total = self._accounts.get(
            ("assets:dividend_receivable", str(event.currency), receivable_key),
            Decimal(0),
        )
        self._dividend_entitlements[key] = (decimal(event.cash_amount), total)

    def _validate_corporate_action(self, event: CorporateActionEvent) -> None:
        spec = self._spec(event.instrument_id)
        if event.action_type == "terminal_cash":
            if (
                spec.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
                or event.ratio is None
                or event.ratio.units != 0
                or event.cash_amount is None
                or event.cash_amount.units < 0
                or event.currency != spec.settlement_currency
            ):
                raise ValidationError(
                    "terminal_cash requires a zero share ratio and nonnegative settlement cash"
                )
            return
        if event.action_type in {"cash_dividend_entitlement", "cash_dividend_payment"}:
            if event.cash_amount is None or event.currency is None:
                raise ValidationError(f"{event.action_type} requires cash_amount and currency")
            if decimal(event.cash_amount) < 0:
                raise ValidationError("cash dividend amount must be non-negative")
            key = self._dividend_entitlement_key(event)
            declaration = self._dividend_entitlements.get(key)
            if event.action_type == "cash_dividend_entitlement" and declaration is not None:
                raise ValidationError("cash dividend entitlement is already registered")
            if event.action_type == "cash_dividend_payment" and event.ratio is not None:
                raise ValidationError("cash dividend payment cannot carry a share ratio")
            if event.action_type == "cash_dividend_payment":
                if declaration is None:
                    raise ValidationError("cash dividend payment has no registered entitlement")
                declared_amount, declared_total = declaration
                if decimal(event.cash_amount) != declared_amount:
                    raise ValidationError(
                        "cash dividend payment amount does not match the registered entitlement"
                    )
                receivable_key = self._dividend_receivable_instrument(key)
                ledger_total = self._accounts.get(
                    ("assets:dividend_receivable", str(event.currency), receivable_key),
                    Decimal(0),
                )
                if ledger_total != declared_total:
                    raise ValidationError(
                        "cash dividend receivable does not match the registered entitlement"
                    )
        if event.ratio is None:
            return
        ratio = decimal(event.ratio)
        if ratio <= 0:
            raise ValidationError("corporate action ratio must be positive")
        step = decimal(spec.quantity_step)
        quantity = self._positions.get(event.instrument_id, Decimal(0))
        new_quantity = quantity * ratio
        if new_quantity % step:
            raise ValidationError(
                "corporate action quantity is not aligned to instrument quantity_step"
            )

    def _translate(self, event: LedgerEvent) -> LedgerTransaction:
        if isinstance(event, Fill):
            return self._fill_transaction(event)
        if isinstance(event, Fee):
            return self._cash_income_transaction(
                event_type=LedgerEventType.FEE,
                reference_id=event.fee_id,
                event_time=event.event_time,
                currency=event.currency,
                cash_delta=-decimal(event.amount),
                counterpart="expenses:fees",
                instrument_id=None,
            )
        if isinstance(event, Funding):
            return self._cash_income_transaction(
                event_type=LedgerEventType.FUNDING,
                reference_id=event.funding_id,
                event_time=event.event_time,
                currency=event.currency,
                cash_delta=decimal(event.amount),
                counterpart="income:funding",
                instrument_id=event.instrument_id,
            )
        if isinstance(event, Settlement):
            return self._settlement_transaction(event)
        if isinstance(event, CorporateActionEvent):
            return self._corporate_action_transaction(event)
        raise ValidationError(f"unsupported ledger event: {type(event).__name__}")

    def _fill_transaction(self, fill_event: Fill) -> LedgerTransaction:
        if fill_event.account_id != self.account_id:
            raise ValidationError("fill account differs from ledger account")
        spec = self._spec(fill_event.instrument_id)
        quantity = decimal(fill_event.quantity)
        signed_quantity = quantity if fill_event.side is Side.BUY else -quantity
        old_quantity = self._positions.get(fill_event.instrument_id, Decimal(0))
        multiplier = decimal(spec.contract_multiplier)
        price = decimal(fill_event.price)
        notional = quantity * price * multiplier
        close_quantity = (
            min(abs(old_quantity), quantity)
            if old_quantity and old_quantity * signed_quantity < 0
            else Decimal(0)
        )
        derivative = fill_event.instrument_id in self._derivative_instruments
        average = (
            self._average_cost(fill_event.instrument_id)
            if close_quantity or (not derivative and fill_event.side is Side.SELL)
            else Decimal(0)
        )
        realized = Decimal(0)
        if close_quantity:
            realized = (
                (price - average) * close_quantity * multiplier
                if old_quantity > 0
                else (average - price) * close_quantity * multiplier
            )
        postings: list[Posting] = []
        if derivative:
            if realized:
                postings.extend(
                    [
                        self._posting("assets:cash", spec.settlement_currency, realized),
                        self._posting(
                            "income:realized_pnl",
                            spec.settlement_currency,
                            -realized,
                            instrument_id=fill_event.instrument_id,
                        ),
                    ]
                )
            old_cost = self._position_cost(fill_event.instrument_id, derivative=True)
            new_quantity = old_quantity + signed_quantity
            if old_quantity == 0 or old_quantity * signed_quantity > 0:
                new_cost = old_cost + signed_quantity * price * multiplier
            elif new_quantity == 0:
                new_cost = Decimal(0)
            elif old_quantity * new_quantity > 0:
                new_cost = (average * abs(new_quantity) * multiplier) * (
                    Decimal(1) if new_quantity > 0 else Decimal(-1)
                )
            else:
                new_cost = new_quantity * price * multiplier
            cost_delta = new_cost - old_cost
            postings.extend(
                [
                    self._posting(
                        "memo:position_cost",
                        spec.settlement_currency,
                        cost_delta,
                        instrument_id=fill_event.instrument_id,
                    ),
                    self._posting(
                        "memo:position_cost_counter",
                        spec.settlement_currency,
                        -cost_delta,
                        instrument_id=fill_event.instrument_id,
                    ),
                ]
            )
        else:
            if old_quantity + signed_quantity < 0:
                raise ValidationError("cash asset fill would create a short position")
            if fill_event.side is Side.BUY:
                cash = self._accounts.get(
                    ("assets:cash", spec.settlement_currency, None), Decimal(0)
                )
                if cash < notional:
                    raise ValidationError("cash asset fill would create negative cash")
                postings.extend(
                    [
                        self._posting("assets:cash", spec.settlement_currency, -notional),
                        self._posting(
                            "assets:position_cost",
                            spec.settlement_currency,
                            notional,
                            instrument_id=fill_event.instrument_id,
                        ),
                    ]
                )
            else:
                # Quantize cash and removed book cost first. Independently rounding
                # their difference can leave a one-unit imbalance for fractional
                # equity fills. P&L is the exact residual of the posted amounts.
                notional = decimal(fixed(notional, self.money_scale))
                cost_removed = decimal(fixed(average * quantity * multiplier, self.money_scale))
                postings.extend(
                    [
                        self._posting("assets:cash", spec.settlement_currency, notional),
                        self._posting(
                            "assets:position_cost",
                            spec.settlement_currency,
                            -cost_removed,
                            instrument_id=fill_event.instrument_id,
                        ),
                        self._posting(
                            "income:realized_pnl",
                            spec.settlement_currency,
                            -(notional - cost_removed),
                            instrument_id=fill_event.instrument_id,
                        ),
                    ]
                )
        postings.extend(
            [
                self._posting(
                    "assets:position",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=fill_event.instrument_id,
                    quantity_delta=signed_quantity,
                    quantity_scale=fill_event.quantity.scale,
                ),
                self._posting(
                    "memo:position_counter",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=fill_event.instrument_id,
                    quantity_delta=-signed_quantity,
                    quantity_scale=fill_event.quantity.scale,
                ),
            ]
        )
        return self._make_transaction(
            event_type=LedgerEventType.FILL,
            reference_id=fill_event.fill_id,
            idempotency_key=f"fill:{fill_event.fill_id}",
            event_time=fill_event.event_time,
            postings=tuple(postings),
        )

    def _cash_income_transaction(
        self,
        *,
        event_type: LedgerEventType,
        reference_id: str,
        event_time: datetime,
        currency: str,
        cash_delta: Decimal,
        counterpart: str,
        instrument_id: str | None,
    ) -> LedgerTransaction:
        return self._make_transaction(
            event_type=event_type,
            reference_id=reference_id,
            idempotency_key=f"{event_type.value}:{reference_id}",
            event_time=event_time,
            postings=(
                self._posting("assets:cash", currency, cash_delta),
                self._posting(counterpart, currency, -cash_delta, instrument_id=instrument_id),
            ),
        )

    def _settlement_price(self, event: Settlement) -> Decimal:
        if event.settlement_price is not None:
            return decimal(event.settlement_price)
        return self._mark_price(event.instrument_id)

    def _settlement_transaction(self, event: Settlement) -> LedgerTransaction:
        if event.settlement_type != "daily_mark":
            return self._cash_income_transaction(
                event_type=LedgerEventType.SETTLEMENT,
                reference_id=event.settlement_id,
                event_time=event.event_time,
                currency=event.currency,
                cash_delta=decimal(event.amount),
                counterpart=f"income:settlement:{event.settlement_type}",
                instrument_id=event.instrument_id,
            )
        spec = self._spec(event.instrument_id)
        if not self._is_derivative(spec):
            raise ValidationError("daily_mark settlement requires a derivative instrument")
        quantity = self._positions.get(event.instrument_id, Decimal(0))
        multiplier = decimal(spec.contract_multiplier)
        settlement_price = self._settlement_price(event)
        average = self._average_cost(event.instrument_id)
        expected = (settlement_price - average) * quantity * multiplier
        rounded_expected = fixed(expected, self.money_scale).to_decimal()
        if decimal(event.amount) != rounded_expected:
            raise ValidationError(
                "daily_mark amount differs from mark-to-market PnL at settlement_price"
            )
        old_cost = self._position_cost(event.instrument_id, derivative=True)
        new_cost = quantity * settlement_price * multiplier
        cost_delta = new_cost - old_cost
        amount = decimal(event.amount)
        return self._make_transaction(
            event_type=LedgerEventType.SETTLEMENT,
            reference_id=event.settlement_id,
            idempotency_key=f"settlement:{event.settlement_id}",
            event_time=event.event_time,
            postings=(
                self._posting("assets:cash", event.currency, amount),
                self._posting(
                    "income:settlement:daily_mark",
                    event.currency,
                    -amount,
                    instrument_id=event.instrument_id,
                ),
                self._posting(
                    "memo:position_cost",
                    event.currency,
                    cost_delta,
                    instrument_id=event.instrument_id,
                ),
                self._posting(
                    "memo:position_cost_counter",
                    event.currency,
                    -cost_delta,
                    instrument_id=event.instrument_id,
                ),
            ),
        )

    def _corporate_action_transaction(self, event: CorporateActionEvent) -> LedgerTransaction:
        spec = self._spec(event.instrument_id)
        quantity = self._positions.get(event.instrument_id, Decimal(0))
        if event.action_type == "terminal_cash":
            cash = decimal(fixed(quantity * decimal(event.cash_amount), self.money_scale))
            cost = self._position_cost(event.instrument_id, derivative=False)
            postings = (
                self._posting("assets:cash", spec.settlement_currency, cash),
                self._posting(
                    "assets:position_cost",
                    spec.settlement_currency,
                    -cost,
                    instrument_id=event.instrument_id,
                ),
                self._posting(
                    "income:realized_pnl",
                    spec.settlement_currency,
                    cost - cash,
                    instrument_id=event.instrument_id,
                ),
                self._posting(
                    "assets:position",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=event.instrument_id,
                    quantity_delta=-quantity,
                    quantity_scale=spec.quantity_step.scale,
                ),
                self._posting(
                    "memo:position_counter",
                    spec.settlement_currency,
                    Decimal(0),
                    instrument_id=event.instrument_id,
                    quantity_delta=quantity,
                    quantity_scale=spec.quantity_step.scale,
                ),
            )
            return self._make_transaction(
                event_type=LedgerEventType.CORPORATE_ACTION,
                reference_id=event.event_id,
                idempotency_key=f"corporate_action:{event.event_id}",
                event_time=event.available_at,
                postings=postings,
            )
        postings: list[Posting] = []
        if event.cash_amount is not None:
            currency = str(event.currency)
            receivable_key = f"{event.instrument_id}@{event.effective_date.isoformat()}"
            if event.action_type == "cash_dividend_payment":
                entitlement_key = self._dividend_entitlement_key(event)
                cash_delta = self._dividend_entitlements[entitlement_key][1]
                postings.extend(
                    [
                        self._posting("assets:cash", currency, cash_delta),
                        self._posting(
                            "assets:dividend_receivable",
                            currency,
                            -cash_delta,
                            instrument_id=receivable_key,
                        ),
                    ]
                )
            else:
                cash_delta = (
                    quantity * decimal(event.cash_amount) * decimal(spec.contract_multiplier)
                )
                asset_account = (
                    "assets:dividend_receivable"
                    if event.action_type == "cash_dividend_entitlement"
                    else "assets:cash"
                )
                postings.extend(
                    [
                        self._posting(
                            asset_account,
                            currency,
                            cash_delta,
                            instrument_id=(
                                receivable_key
                                if event.action_type == "cash_dividend_entitlement"
                                else None
                            ),
                        ),
                        self._posting(
                            "income:corporate_action",
                            currency,
                            -cash_delta,
                            instrument_id=event.instrument_id,
                        ),
                    ]
                )
        if event.ratio is not None:
            quantity_delta = quantity * (decimal(event.ratio) - Decimal(1))
            postings.extend(
                [
                    self._posting(
                        "assets:position",
                        spec.settlement_currency,
                        Decimal(0),
                        instrument_id=event.instrument_id,
                        quantity_delta=quantity_delta,
                        quantity_scale=spec.quantity_step.scale,
                    ),
                    self._posting(
                        "memo:position_counter",
                        spec.settlement_currency,
                        Decimal(0),
                        instrument_id=event.instrument_id,
                        quantity_delta=-quantity_delta,
                        quantity_scale=spec.quantity_step.scale,
                    ),
                ]
            )
        return self._make_transaction(
            event_type=LedgerEventType.CORPORATE_ACTION,
            reference_id=event.event_id,
            idempotency_key=f"corporate_action:{event.event_id}",
            event_time=event.available_at,
            postings=tuple(postings),
        )

    def _posting(
        self,
        account: str,
        currency: str,
        amount: Decimal,
        *,
        instrument_id: str | None = None,
        quantity_delta: Decimal | None = None,
        quantity_scale: int = 8,
    ) -> Posting:
        key = (
            account,
            currency,
            amount,
            instrument_id,
            quantity_delta,
            quantity_scale,
        )
        prior = self._posting_cache.get(key)
        if prior is not None:
            return prior
        posting = object.__new__(Posting)
        object.__setattr__(posting, "ledger_account", account)
        object.__setattr__(posting, "currency", currency)
        object.__setattr__(
            posting,
            "amount",
            fixed(amount, self.money_scale, rounding=ROUND_HALF_EVEN),
        )
        object.__setattr__(posting, "instrument_id", instrument_id)
        object.__setattr__(
            posting,
            "quantity_delta",
            (
                fixed(quantity_delta, quantity_scale, rounding=ROUND_HALF_EVEN)
                if quantity_delta is not None
                else None
            ),
        )
        self._posting_cache[key] = posting
        return posting

    def _make_transaction(
        self,
        *,
        event_type: LedgerEventType,
        reference_id: str,
        idempotency_key: str,
        event_time: datetime,
        postings: tuple[Posting, ...],
    ) -> LedgerTransaction:
        transaction = object.__new__(LedgerTransaction)
        object.__setattr__(
            transaction,
            "transaction_id",
            _identifier("tx", self.account_id, event_type.value, reference_id),
        )
        object.__setattr__(transaction, "idempotency_key", idempotency_key)
        object.__setattr__(transaction, "event_time", event_time)
        object.__setattr__(transaction, "event_type", event_type)
        object.__setattr__(transaction, "reference_id", reference_id)
        object.__setattr__(transaction, "postings", postings)
        return transaction

    def _post(self, transaction: LedgerTransaction, *, local_rollback: bool = True) -> None:
        streaming = self._artifact_sink is not None
        if not streaming and transaction.idempotency_key in self._transaction_keys:
            raise ValidationError("duplicate ledger transaction idempotency key")
        if not local_rollback:
            for posting in transaction.postings:
                key = (posting.ledger_account, posting.currency, posting.instrument_id)
                self._accounts[key] = add_decimal_exact(
                    self._accounts.get(key, Decimal(0)), decimal(posting.amount)
                )
                if (
                    posting.ledger_account == "assets:position"
                    and posting.instrument_id is not None
                    and posting.quantity_delta is not None
                ):
                    instrument_id = posting.instrument_id
                    self._positions[instrument_id] = add_decimal_exact(
                        self._positions.get(instrument_id, Decimal(0)),
                        decimal(posting.quantity_delta),
                    )
            if self._artifact_sink is None:
                self._transactions.append(transaction)
            else:
                self._artifact_sink.append(
                    "ledger_transactions", ledger_transaction_bytes(transaction)
                )
                self._transaction_count += 1
            if not streaming:
                self._transaction_keys.add(transaction.idempotency_key)
            return
        missing = object()
        prior_accounts: dict[tuple[str, str, str | None], Decimal | object] = {}
        prior_positions: dict[str, Decimal | object] = {}
        try:
            for posting in transaction.postings:
                key = (posting.ledger_account, posting.currency, posting.instrument_id)
                prior_accounts.setdefault(key, self._accounts.get(key, missing))
                self._accounts[key] = add_decimal_exact(
                    self._accounts.get(key, Decimal(0)), decimal(posting.amount)
                )
                if (
                    posting.ledger_account == "assets:position"
                    and posting.instrument_id is not None
                    and posting.quantity_delta is not None
                ):
                    instrument_id = posting.instrument_id
                    prior_positions.setdefault(
                        instrument_id, self._positions.get(instrument_id, missing)
                    )
                    self._positions[instrument_id] = add_decimal_exact(
                        self._positions.get(instrument_id, Decimal(0)),
                        decimal(posting.quantity_delta),
                    )
            if self._artifact_sink is None:
                self._transactions.append(transaction)
            else:
                self._artifact_sink.append(
                    "ledger_transactions", ledger_transaction_bytes(transaction)
                )
                self._transaction_count += 1
            if not streaming:
                self._transaction_keys.add(transaction.idempotency_key)
        except Exception:
            for key, value in prior_accounts.items():
                if value is missing:
                    self._accounts.pop(key, None)
                else:
                    self._accounts[key] = value
            for instrument_id, value in prior_positions.items():
                if value is missing:
                    self._positions.pop(instrument_id, None)
                else:
                    self._positions[instrument_id] = value
            if self._transactions and self._transactions[-1] is transaction:
                self._transactions.pop()
            self._transaction_keys.discard(transaction.idempotency_key)
            raise

    def _event_identity(self, event: LedgerEvent) -> str:
        if isinstance(event, CorporateActionEvent):
            return event.event_id
        identity_field = {
            Fill: "fill_id",
            Fee: "fee_id",
            Funding: "funding_id",
            Settlement: "settlement_id",
        }.get(type(event))
        if identity_field is None:
            raise ValidationError("ledger event has no stable identity")
        return f"{identity_field}:{getattr(event, identity_field)}"

    @staticmethod
    def _event_fingerprint(event: LedgerEvent) -> bytes:
        if isinstance(event, Fill):
            payload = fill_bytes(event)
        elif isinstance(event, Fee):
            payload = fee_bytes(event)
        elif isinstance(event, Settlement):
            payload = settlement_bytes(event)
        elif isinstance(event, CorporateActionEvent):
            payload = _canonical(market_event_payload(event))
        else:
            payload = _canonical(execution_payload(event))
        return hashlib.sha256(payload).digest()

    def _spec(self, instrument_id: str) -> InstrumentSpec:
        try:
            return self.instruments[instrument_id]
        except KeyError as exc:
            raise ValidationError(f"missing InstrumentSpec for {instrument_id}") from exc

    @staticmethod
    def _is_derivative(spec: InstrumentSpec) -> bool:
        product = spec.product_type.lower()
        return spec.asset_class is AssetClass.FUTURE or "perpetual" in product or "perp" in product

    def _position_cost(self, instrument_id: str, *, derivative: bool) -> Decimal:
        account = "memo:position_cost" if derivative else "assets:position_cost"
        spec = self._spec(instrument_id)
        return self._accounts.get((account, spec.settlement_currency, instrument_id), Decimal(0))

    def _average_cost(self, instrument_id: str) -> Decimal:
        quantity = self._positions.get(instrument_id, Decimal(0))
        if quantity == 0:
            return Decimal(0)
        spec = self._spec(instrument_id)
        cost = self._position_cost(instrument_id, derivative=self._is_derivative(spec))
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            return fraction_decimal_exact(
                decimal_fraction(cost.copy_abs())
                / decimal_fraction(quantity.copy_abs())
                / decimal_fraction(spec.contract_multiplier)
            )
        return abs(cost) / (abs(quantity) * decimal(spec.contract_multiplier))

    def _realized(self, instrument_id: str) -> Decimal:
        spec = self._spec(instrument_id)
        credit = self._accounts.get(
            ("income:realized_pnl", spec.settlement_currency, instrument_id), Decimal(0)
        )
        return (
            credit.copy_negate()
            if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT
            else -credit
        )

    def _mark_price(self, instrument_id: str, *, fallback: Decimal | None = None) -> Decimal:
        mark = self._marks.get(instrument_id)
        if mark is not None:
            return mark[0]
        if fallback is not None:
            return fallback
        raise ValidationError(f"missing mark price for {instrument_id}")

    def _to_base(self, amount: Decimal | FixedPoint, currency: str, at: datetime) -> Decimal:
        value = decimal(amount) if isinstance(amount, FixedPoint) else amount
        if value == 0:
            return Decimal(0)
        try:
            rate, available_at = self._fx[currency]
        except KeyError as exc:
            raise ValidationError(
                f"missing FX snapshot for {currency}->{self.base_currency}"
            ) from exc
        if available_at > at:
            historical = next(
                (
                    (historical_rate, historical_time)
                    for historical_currency, historical_rate, historical_time in reversed(
                        self._fx_history
                    )
                    if historical_currency == currency and historical_time <= at
                ),
                None,
            )
            if historical is None:
                raise ValidationError("FX snapshot was not available at valuation time")
            rate, available_at = historical
        return value * rate

    def _convert_for_valuation(
        self,
        amount: Decimal | FixedPoint,
        currency: str,
        at: datetime,
    ) -> Decimal:
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            self._require_valuation_time(at)
            return convert_dividend_value(self, amount, currency, at)
        return self._to_base(amount, currency, at)

    def _require_valuation_time(self, at: datetime) -> None:
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT and at < self._event_time:
            raise ValidationError("HISTORICAL_LEDGER_STATE_UNAVAILABLE")

    def _sum_for_valuation(self, values) -> Decimal:
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            return sum_decimal_exact(values)
        return sum(values, Decimal(0))

    def _product_for_valuation(self, *values: Decimal | FixedPoint | int) -> Decimal:
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            return multiply_decimal_exact(*values)
        result = Decimal(1)
        for value in values:
            result *= decimal(value) if isinstance(value, FixedPoint) else value
        return result

    def _difference_for_valuation(self, left: Decimal, right: Decimal) -> Decimal:
        if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT:
            return add_decimal_exact(left, right.copy_negate())
        return left - right

    def _fixed_for_valuation(self, value: Decimal, scale: int) -> FixedPoint:
        return fixed(
            value,
            scale,
            rounding=(
                None if self.fx_valuation_mode is FxValuationMode.EVIDENCED_PIT else ROUND_HALF_EVEN
            ),
        )
