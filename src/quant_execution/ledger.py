"""Exact multi-currency double-entry account ledger."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import ROUND_HALF_EVEN, Decimal

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
    TradeEvent,
    market_event_payload,
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
    Posting,
    Settlement,
    Side,
)
from quant_execution.schemas import execution_payload

UTC = timezone.utc
_OPENED_AT = datetime(1970, 1, 1, tzinfo=UTC)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=True, allow_nan=False, separators=(",", ":")
    ).encode()


def _identifier(prefix: str, *parts: object) -> str:
    return f"{prefix}-{hashlib.sha256(_canonical(parts)).hexdigest()[:24]}"


def _meta_decimal(spec: InstrumentSpec, key: str, default: str = "0") -> Decimal:
    raw = spec.metadata.get(key, default)
    try:
        value = Decimal(raw)
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
    ) -> None:
        if not account_id.strip() or not base_currency.strip():
            raise ValidationError("account_id and base_currency are required")
        if not 0 <= money_scale <= 18:
            raise ValidationError("money_scale must be in [0, 18]")
        self.account_id = account_id
        self.base_currency = base_currency
        self.instruments = dict(instruments)
        self.money_scale = money_scale
        self._initial_cash = dict(initial_cash or {})
        self._initial_fx = dict(fx_to_base or {})
        self.reset()

    def reset(self) -> None:
        self._transactions: list[LedgerTransaction] = []
        self._transaction_keys: set[str] = set()
        self._event_fingerprints: dict[str, str] = {}
        self._fills: dict[str, Fill] = {}
        self._accounts: dict[tuple[str, str, str | None], Decimal] = {}
        self._positions: dict[str, Decimal] = {}
        self._marks: dict[str, tuple[Decimal, datetime, str]] = {}
        self._mark_fingerprints: dict[str, str] = {}
        self._fill_trading_days: dict[str, object] = {}
        self._fx: dict[str, tuple[Decimal, datetime]] = {
            self.base_currency: (Decimal(1), _OPENED_AT)
        }
        self._event_time = _OPENED_AT
        for currency, rate in self._initial_fx.items():
            self.set_fx_rate(currency, rate, event_time=_OPENED_AT)
        for currency, amount in sorted(self._initial_cash.items()):
            value = decimal(amount)
            transaction = self._make_transaction(
                event_type=LedgerEventType.FX_CONVERSION,
                reference_id=f"opening:{currency}",
                idempotency_key=f"opening:{self.account_id}:{currency}",
                event_time=_OPENED_AT,
                postings=(
                    self._posting("assets:cash", currency, value),
                    self._posting("equity:opening", currency, -value),
                ),
            )
            self._post(transaction)

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
        }
        return hashlib.sha256(_canonical(payload)).hexdigest()

    def set_fx_rate(self, currency: str, rate: FixedPoint, *, event_time: datetime) -> None:
        value = decimal(rate)
        if value <= 0:
            raise ValidationError("FX rate must be positive")
        prior = self._fx.get(currency)
        if prior is not None and event_time < prior[1]:
            raise ValidationError("FX snapshot time moved backwards")
        self._fx[currency] = (value, event_time)

    def convert_to_base(
        self, amount: Decimal | FixedPoint, currency: str, *, event_time: datetime
    ) -> Decimal:
        return self._to_base(amount, currency, event_time)

    def mark(
        self, event: MarkPriceEvent, *, create_snapshot: bool = True
    ) -> AccountSnapshot | None:
        if event.instrument_id not in self.instruments:
            raise ValidationError(f"missing InstrumentSpec for {event.instrument_id}")
        fingerprint = hashlib.sha256(_canonical(market_event_payload(event))).hexdigest()
        prior = self._mark_fingerprints.get(event.event_id)
        if prior is not None:
            if prior != fingerprint:
                raise ValidationError("mark event_id reused with different content")
            return self.snapshot(self._event_time) if create_snapshot else None
        current = self._marks.get(event.instrument_id)
        if current is not None and event.available_at < current[1]:
            raise ValidationError("mark price time moved backwards")
        self._marks[event.instrument_id] = (
            decimal(event.price),
            event.available_at,
            event.event_id,
        )
        self._mark_fingerprints[event.event_id] = fingerprint
        self._event_time = max(self._event_time, event.available_at)
        return self.snapshot(event.available_at) if create_snapshot else None

    def observe_market(
        self, event: MarketEvent, *, create_snapshot: bool = True
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
        synthetic = MarkPriceEvent(
            event_id=f"mark:{event.event_id}",
            instrument_id=event.instrument_id,
            event_time=event.event_time,
            received_at=event.received_at,
            available_at=event.available_at,
            source=event.source,
            trading_day=event.trading_day,
            session_id=event.session_id,
            sequence=event.sequence,
            price=price,
        )
        return self.mark(synthetic, create_snapshot=create_snapshot)

    def apply(self, event: LedgerEvent, *, create_snapshot: bool = True) -> AccountSnapshot | None:
        self._validate_event(event)
        reference_id, payload = self._event_identity(event)
        fingerprint = hashlib.sha256(_canonical(payload)).hexdigest()
        prior = self._event_fingerprints.get(reference_id)
        if prior is not None:
            if prior != fingerprint:
                raise ValidationError("ledger event id reused with different content")
            return self.snapshot(self._event_time) if create_snapshot else None
        transaction = self._translate(event)
        self._post(transaction)
        if isinstance(event, Fill):
            self._fills[event.fill_id] = event
        self._event_fingerprints[reference_id] = fingerprint
        self._event_time = max(self._event_time, transaction.event_time)
        return self.snapshot(transaction.event_time) if create_snapshot else None

    def apply_with_trading_day(
        self,
        event: LedgerEvent,
        *,
        trading_day: object,
        create_snapshot: bool = True,
    ) -> AccountSnapshot | None:
        if not isinstance(trading_day, date) or isinstance(trading_day, datetime):
            raise ValidationError("trading_day must be a date")
        snapshot = self.apply(event, create_snapshot=create_snapshot)
        if isinstance(event, Fill):
            prior = self._fill_trading_days.get(event.fill_id)
            if prior is not None and prior != trading_day:
                raise ValidationError("fill trading_day changed across idempotent application")
            self._fill_trading_days[event.fill_id] = trading_day
        return snapshot

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

    def snapshot(self, event_time: datetime | None = None) -> AccountSnapshot:
        at = event_time or self._event_time
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
        total = Decimal(0)
        current = self._positions.get(instrument_id, Decimal(0))
        for transaction in self._transactions:
            if transaction.event_type is not LedgerEventType.FILL:
                continue
            event_trading_day = self._fill_trading_days.get(
                transaction.reference_id, transaction.event_time.date()
            )
            if event_trading_day != trading_day:
                continue
            for posting in transaction.postings:
                if (
                    posting.ledger_account == "assets:position"
                    and posting.instrument_id == instrument_id
                    and posting.quantity_delta is not None
                    and (
                        (current > 0 and decimal(posting.quantity_delta) > 0)
                        or (current < 0 and decimal(posting.quantity_delta) < 0)
                    )
                ):
                    total += abs(decimal(posting.quantity_delta))
        return total

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
            return self._cash_income_transaction(
                event_type=LedgerEventType.SETTLEMENT,
                reference_id=event.settlement_id,
                event_time=event.event_time,
                currency=event.currency,
                cash_delta=decimal(event.amount),
                counterpart=f"income:settlement:{event.settlement_type}",
                instrument_id=event.instrument_id,
            )
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
        return Posting(
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
        for posting in transaction.postings:
            key = (posting.ledger_account, posting.currency, posting.instrument_id)
            self._accounts[key] = self._accounts.get(key, Decimal(0)) + decimal(posting.amount)
            if (
                posting.ledger_account == "assets:position"
                and posting.instrument_id is not None
                and posting.quantity_delta is not None
            ):
                self._positions[posting.instrument_id] = self._positions.get(
                    posting.instrument_id, Decimal(0)
                ) + decimal(posting.quantity_delta)
        self._transactions.append(transaction)
        self._transaction_keys.add(transaction.idempotency_key)

    def _event_identity(self, event: LedgerEvent) -> tuple[str, dict[str, object]]:
        if isinstance(event, CorporateActionEvent):
            return event.event_id, market_event_payload(event)
        identity_field = {
            Fill: "fill_id",
            Fee: "fee_id",
            Funding: "funding_id",
            Settlement: "settlement_id",
        }.get(type(event))
        if identity_field is None:
            raise ValidationError("ledger event has no stable identity")
        payload = execution_payload(event)
        return f"{identity_field}:{payload[identity_field]}", payload

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
            raise ValidationError("FX snapshot was not available at valuation time")
        return value * rate
