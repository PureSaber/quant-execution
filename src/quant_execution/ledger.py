"""Exact multi-currency double-entry account ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal
from functools import lru_cache

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
)
from quant_data_kit.exceptions import ValidationError

from quant_execution._fixed import decimal, fixed
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
from quant_execution.schemas import execution_payload

UTC = timezone.utc
_OPENED_AT = datetime(1970, 1, 1, tzinfo=UTC)
_MISSING = object()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()


def _identifier(prefix: str, *parts: object) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical(parts)).hexdigest()[:24]}"


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
    ) -> None:
        if not account_id.strip() or not base_currency.strip():
            raise ValidationError("account_id and base_currency are required")
        if not 0 <= money_scale <= 18:
            raise ValidationError("money_scale must be in [0, 18]")
        self.account_id = account_id
        self.base_currency = _currency(base_currency, "base_currency")
        self.instruments = dict(instruments)
        self._derivative_instruments = frozenset(
            instrument_id
            for instrument_id, spec in self.instruments.items()
            if self._is_derivative(spec)
        )
        self.money_scale = money_scale
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
        self._transaction_keys: set[str] = set()
        self._event_fingerprints: dict[str, LedgerEvent] = {}
        self._fills: dict[str, Fill] = {}
        self._accounts: dict[tuple[str, str, str | None], Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._marks: dict[str, tuple[Decimal, datetime, str]] = {}
        self._mark_fingerprints: dict[str, MarketEvent] = {}
        self._fill_trading_days: dict[str, date] = {}
        self._position_lots: dict[str, list[tuple[date, Decimal]]] = {}
        self._fill_close_allocations: dict[str, tuple[Decimal, Decimal]] = {}
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
                    self._posting("equity:opening", currency, -value),
                ),
            )
            self._post(transaction)

    def capture_state(self) -> dict[str, object]:
        return deepcopy(
            {
                "transactions": self._transactions,
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
                "posting_cache": self._posting_cache,
                "fx": self._fx,
                "fx_history": self._fx_history,
                "event_time": self._event_time,
            }
        )

    def restore_state(self, state: dict[str, object]) -> None:
        restored = deepcopy(state)
        self._transactions = restored["transactions"]
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
        self._posting_cache = restored["posting_cache"]
        self._fx = restored["fx"]
        self._fx_history = restored["fx_history"]
        self._event_time = restored["event_time"]

    @property
    def transactions(self) -> tuple[LedgerTransaction, ...]:
        return tuple(self._transactions)

    @property
    def journal_sha256(self) -> str:
        payload = {
            "transactions": [execution_payload(item) for item in self._transactions],
            "marks": [
                {
                    "instrument_id": instrument_id,
                    "price": str(price),
                    "event_time": event_time.isoformat(),
                    "event_id": event_id,
                }
                for instrument_id, (price, event_time, event_id) in sorted(self._marks.items())
            ],
            "fx_snapshots": [
                {
                    "currency": currency,
                    "rate": str(rate),
                    "event_time": event_time.isoformat(),
                    "version": index + 1,
                }
                for index, (currency, rate, event_time) in enumerate(self._fx_history)
            ],
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def set_fx_rate(self, currency: str, rate: FixedPoint, *, event_time: datetime) -> None:
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
        return self._to_base(
            amount,
            _currency(currency),
            ensure_utc_datetime(event_time, field="event_time"),
        )

    def cash_balance(self, currency: str) -> Decimal:
        return self._accounts.get(("assets:cash", currency, None), Decimal(0))

    def risk_balances(
        self, event_time: datetime
    ) -> tuple[dict[str, Decimal], dict[str, Decimal], Decimal, Decimal]:
        """Return exact decimal balances needed by the hot pre-trade risk path."""
        event_time = ensure_utc_datetime(event_time, field="event_time")
        cash = {
            currency: amount
            for (account, currency, instrument_id), amount in self._accounts.items()
            if account == "assets:cash" and instrument_id is None
        }
        nav = sum(
            (self._to_base(amount, currency, event_time) for currency, amount in cash.items()),
            Decimal(0),
        )
        initial_margin = Decimal(0)
        for instrument_id, quantity in self._positions.items():
            spec = self._spec(instrument_id)
            average = self._average_cost(instrument_id)
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                nav += self._to_base(
                    (mark - average) * quantity * multiplier,
                    spec.settlement_currency,
                    event_time,
                )
                initial_margin += self._to_base(
                    abs(mark * quantity * multiplier) * _meta_decimal(spec, "initial_margin_rate"),
                    spec.settlement_currency,
                    event_time,
                )
            else:
                nav += self._to_base(
                    mark * quantity * multiplier,
                    spec.settlement_currency,
                    event_time,
                )
        return cash, self._positions, nav, initial_margin

    def portfolio_risk_snapshot(self, event_time: datetime) -> PortfolioRiskSnapshot:
        """Build an exact, read-only base-currency exposure view at one PIT timestamp."""
        at = ensure_utc_datetime(event_time, field="event_time")
        account = self.snapshot(at)
        cash_value = sum(
            (
                self._to_base(decimal(amount), currency, at)
                for currency, amount in account.cash_balances.items()
            ),
            Decimal(0),
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
            local_notional = mark * quantity * multiplier
            base_notional = self._to_base(local_notional, spec.settlement_currency, at)
            position_initial = Decimal(0)
            position_maintenance = Decimal(0)
            if self._is_derivative(spec):
                for key in ("initial_margin_rate", "maintenance_margin_rate"):
                    if key not in spec.metadata:
                        raise ValidationError(
                            f"InstrumentSpec metadata {key!r} is required for risk snapshot"
                        )
                absolute_notional = abs(local_notional)
                position_initial = self._to_base(
                    absolute_notional * _meta_decimal(spec, "initial_margin_rate"),
                    spec.settlement_currency,
                    at,
                )
                position_maintenance = self._to_base(
                    absolute_notional * _meta_decimal(spec, "maintenance_margin_rate"),
                    spec.settlement_currency,
                    at,
                )
            gross_exposure += abs(base_notional)
            net_exposure += base_notional
            initial_margin += position_initial
            maintenance_margin += position_maintenance
            positions.append(
                PositionRiskSnapshot(
                    instrument_id=instrument_id,
                    asset_class=spec.asset_class,
                    venue=spec.venue,
                    settlement_currency=spec.settlement_currency,
                    quantity=quantity_fp,
                    mark_price=fixed(mark, spec.price_tick.scale),
                    base_notional=fixed(base_notional, self.money_scale),
                    initial_margin=fixed(position_initial, self.money_scale),
                    maintenance_margin=fixed(position_maintenance, self.money_scale),
                )
            )
        return PortfolioRiskSnapshot(
            account_id=account.account_id,
            event_time=at,
            base_currency=account.base_currency,
            nav=account.nav,
            cash_value=fixed(cash_value, self.money_scale),
            gross_exposure=fixed(gross_exposure, self.money_scale),
            net_exposure=fixed(net_exposure, self.money_scale),
            initial_margin=fixed(initial_margin, self.money_scale),
            maintenance_margin=fixed(maintenance_margin, self.money_scale),
            positions=tuple(positions),
        )

    def mark(
        self, event: MarkPriceEvent, *, create_snapshot: bool = True
    ) -> AccountSnapshot | None:
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
        try:
            self._marks[event.instrument_id] = (
                decimal(event.price),
                event.available_at,
                event.event_id,
            )
            self._mark_fingerprints[event.event_id] = event
            self._event_time = max(self._event_time, event.available_at)
            return self.snapshot(event.available_at) if create_snapshot else None
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
        try:
            self._marks[event.instrument_id] = (decimal(price), event.available_at, synthetic_id)
            if not trusted_unique:
                self._mark_fingerprints[synthetic_id] = event
            self._event_time = max(self._event_time, event.available_at)
            return self.snapshot(event.available_at) if create_snapshot else None
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
        if not any(
            quantity and instrument_id in self._derivative_instruments
            for instrument_id, quantity in self._positions.items()
        ):
            return False
        nav = sum(
            (
                self._to_base(amount, currency, at)
                for (account, currency, instrument_id), amount in self._accounts.items()
                if account == "assets:cash" and instrument_id is None
            ),
            Decimal(0),
        )
        maintenance_margin = Decimal(0)
        for instrument_id, quantity in self._positions.items():
            spec = self._spec(instrument_id)
            average = self._average_cost(instrument_id)
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                nav += self._to_base(
                    (mark - average) * quantity * multiplier,
                    spec.settlement_currency,
                    at,
                )
                maintenance_margin += self._to_base(
                    abs(mark * quantity * multiplier)
                    * _meta_decimal(spec, "maintenance_margin_rate"),
                    spec.settlement_currency,
                    at,
                )
            else:
                nav += self._to_base(
                    mark * quantity * multiplier,
                    spec.settlement_currency,
                    at,
                )
        rounded_nav = fixed(nav, self.money_scale)
        rounded_maintenance = fixed(maintenance_margin, self.money_scale)
        return rounded_maintenance.units > 0 and rounded_nav.units <= rounded_maintenance.units

    def apply(self, event: LedgerEvent, *, create_snapshot: bool = True) -> AccountSnapshot | None:
        trading_day = event.event_time.date() if isinstance(event, Fill) else None
        return self._apply(
            event,
            trading_day=trading_day,
            create_snapshot=create_snapshot,
        )

    def _apply(
        self,
        event: LedgerEvent,
        *,
        trading_day: date | None,
        create_snapshot: bool,
    ) -> AccountSnapshot | None:
        self._validate_event(event)
        reference_id = self._event_identity(event)
        prior = self._event_fingerprints.get(reference_id)
        if prior is not None:
            if prior != event:
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
            undo = self._capture_apply_undo(event, transaction, reference_id)
            self._post(transaction)
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
            elif isinstance(event, CorporateActionEvent) and event.ratio is not None:
                self._apply_split_state(event)
            self._event_fingerprints[reference_id] = event
            self._event_time = transaction.event_time
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
        return self._apply(
            event,
            trading_day=trading_day if isinstance(event, Fill) else None,
            create_snapshot=create_snapshot,
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
        cash: dict[str, FixedPoint] = {}
        for (account, currency, instrument_id), amount in self._accounts.items():
            if account == "assets:cash" and instrument_id is None:
                cash[currency] = fixed(
                    decimal(cash.get(currency, FixedPoint(0, self.money_scale))) + amount,
                    self.money_scale,
                )
        positions: dict[str, FixedPoint] = {}
        costs: dict[str, FixedPoint] = {}
        realized: dict[str, FixedPoint] = {}
        unrealized: dict[str, FixedPoint] = {}
        nav = sum(
            (self._to_base(value, currency, at) for currency, value in cash.items()), Decimal(0)
        )
        initial_margin = Decimal(0)
        maintenance_margin = Decimal(0)
        for instrument_id, quantity in sorted(self._positions.items()):
            spec = self._spec(instrument_id)
            positions[instrument_id] = fixed(quantity, spec.quantity_step.scale)
            average = self._average_cost(instrument_id)
            costs[instrument_id] = fixed(average, spec.price_tick.scale)
            realized_value = self._realized(instrument_id)
            realized[instrument_id] = fixed(
                self._to_base(realized_value, spec.settlement_currency, at), self.money_scale
            )
            mark = self._mark_price(instrument_id, fallback=average)
            multiplier = decimal(spec.contract_multiplier)
            pnl = (mark - average) * quantity * multiplier
            unrealized[instrument_id] = fixed(
                self._to_base(pnl, spec.settlement_currency, at), self.money_scale
            )
            if self._is_derivative(spec):
                nav += self._to_base(pnl, spec.settlement_currency, at)
                notional = abs(mark * quantity * multiplier)
                initial_margin += self._to_base(
                    notional * _meta_decimal(spec, "initial_margin_rate"),
                    spec.settlement_currency,
                    at,
                )
                maintenance_margin += self._to_base(
                    notional * _meta_decimal(spec, "maintenance_margin_rate"),
                    spec.settlement_currency,
                    at,
                )
            else:
                nav += self._to_base(mark * quantity * multiplier, spec.settlement_currency, at)
        nav_value = fixed(nav, self.money_scale)
        maintenance = fixed(maintenance_margin, self.money_scale)
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
            initial_margin=fixed(initial_margin, self.money_scale),
            maintenance_margin=maintenance,
            liquidation_required=maintenance.units > 0 and nav_value.units <= maintenance.units,
        )
        self.assert_nav_residual(snapshot)
        return snapshot

    def assert_nav_residual(self, snapshot: AccountSnapshot) -> None:
        expected = Decimal(0)
        at = snapshot.event_time
        for currency, balance in snapshot.cash_balances.items():
            expected += self._to_base(decimal(balance), currency, at)
        for instrument_id, quantity_fp in snapshot.positions.items():
            spec = self._spec(instrument_id)
            quantity = decimal(quantity_fp)
            mark = self._mark_price(instrument_id, fallback=self._average_cost(instrument_id))
            multiplier = decimal(spec.contract_multiplier)
            if self._is_derivative(spec):
                component = (mark - self._average_cost(instrument_id)) * quantity * multiplier
            else:
                component = mark * quantity * multiplier
            expected += self._to_base(component, spec.settlement_currency, at)
        residual = abs(decimal(snapshot.nav) - expected)
        tolerance = max(abs(decimal(snapshot.nav)) * Decimal("1e-8"), Decimal("0.01"))
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

    def _validate_corporate_action(self, event: CorporateActionEvent) -> None:
        spec = self._spec(event.instrument_id)
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
        average = self._average_cost(fill_event.instrument_id)
        multiplier = decimal(spec.contract_multiplier)
        price = decimal(fill_event.price)
        notional = quantity * price * multiplier
        close_quantity = (
            min(abs(old_quantity), quantity)
            if old_quantity and old_quantity * signed_quantity < 0
            else Decimal(0)
        )
        if old_quantity > 0:
            realized = (price - average) * close_quantity * multiplier
        elif old_quantity < 0:
            realized = (average - price) * close_quantity * multiplier
        else:
            realized = Decimal(0)
        postings: list[Posting] = []
        if self._is_derivative(spec):
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
                cost_removed = average * quantity * multiplier
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
        postings: list[Posting] = []
        if event.cash_amount is not None:
            cash_delta = quantity * decimal(event.cash_amount) * decimal(spec.contract_multiplier)
            postings.extend(
                [
                    self._posting("assets:cash", str(event.currency), cash_delta),
                    self._posting(
                        "income:corporate_action",
                        str(event.currency),
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
        posting = Posting(
            ledger_account=account,
            currency=currency,
            amount=fixed(amount, self.money_scale, rounding=ROUND_HALF_EVEN),
            instrument_id=instrument_id,
            quantity_delta=(
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
        return LedgerTransaction(
            transaction_id=_identifier("tx", self.account_id, event_type.value, reference_id),
            idempotency_key=idempotency_key,
            event_time=event_time,
            event_type=event_type,
            reference_id=reference_id,
            postings=postings,
        )

    def _post(self, transaction: LedgerTransaction) -> None:
        if transaction.idempotency_key in self._transaction_keys:
            raise ValidationError("duplicate ledger transaction idempotency key")
        missing = object()
        prior_accounts: dict[tuple[str, str, str | None], Decimal | object] = {}
        prior_positions: dict[str, Decimal | object] = {}
        try:
            for posting in transaction.postings:
                key = (posting.ledger_account, posting.currency, posting.instrument_id)
                prior_accounts.setdefault(key, self._accounts.get(key, missing))
                self._accounts[key] = self._accounts.get(key, Decimal(0)) + decimal(posting.amount)
                if (
                    posting.ledger_account == "assets:position"
                    and posting.instrument_id is not None
                    and posting.quantity_delta is not None
                ):
                    instrument_id = posting.instrument_id
                    prior_positions.setdefault(
                        instrument_id, self._positions.get(instrument_id, missing)
                    )
                    self._positions[instrument_id] = self._positions.get(
                        instrument_id, Decimal(0)
                    ) + decimal(posting.quantity_delta)
            self._transactions.append(transaction)
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
        return abs(cost) / (abs(quantity) * decimal(spec.contract_multiplier))

    def _realized(self, instrument_id: str) -> Decimal:
        spec = self._spec(instrument_id)
        credit = self._accounts.get(
            ("income:realized_pnl", spec.settlement_currency, instrument_id), Decimal(0)
        )
        return -credit

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
