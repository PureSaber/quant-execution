"""Single-currency research derivative account using ExactAccountLedger.

Integer contracts, positive-price fills, explicit cash/physical option expiry,
daily futures variation margin and conservative sum-of-contract margin. No live
broker, SPAN, automatic American optimal exercise, or unverified currency FX.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from zoneinfo import ZoneInfo

from quant_data_kit import (
    AssetClass,
    CorporateActionEvent,
    FixedPoint,
    InstrumentSpec,
    MarkPriceEvent,
    StatusEvent,
)
from quant_data_kit.derivatives.models import Contract, decimal, utc

from .contracts import Fee, Fill, Side
from .ledger import ExactAccountLedger


def fp(value, scale=8):
    value = decimal(value)
    rounded = value.quantize(Decimal(1).scaleb(-scale))
    if value != rounded:
        raise ValueError(f"value exceeds supported {scale}-decimal precision")
    return FixedPoint.from_decimal(rounded, scale)


def specification(c: Contract):
    metadata = {
        "initial_margin_rate": "0",
        "maintenance_margin_rate": "0",
        "rules_source": c.rules_source,
    }
    if c.initial_margin is not None:
        metadata.update(
            initial_margin_per_contract=str(c.initial_margin),
            maintenance_margin_per_contract=str(c.maintenance_margin),
        )
    return InstrumentSpec(
        instrument_id=c.instrument_id,
        asset_class=AssetClass.FUTURE if c.kind == "future" else AssetClass.OPTION,
        product_type="research_" + c.kind,
        venue=c.venue,
        native_symbol=c.symbol,
        settlement_currency=c.currency,
        quote_currency=c.currency,
        price_tick=fp(c.tick),
        quantity_step=fp(1, 0),
        contract_multiplier=fp(c.multiplier),
        calendar_id=c.timezone,
        effective_from=c.listed_at,
        available_at=c.known_at,
        underlying_id=c.underlying or None,
        expiry_date=c.expiry.date(),
        metadata=metadata,
    )


class DerivativeAccount:
    def __init__(self, contracts, initial_cash, opened_at, *, fee_per_contract=0, slippage_ticks=0):
        contracts = tuple(contracts)
        self.contracts = {c.instrument_id: c for c in contracts}
        if (
            not contracts
            or len(contracts) != len(self.contracts)
            or len({c.currency for c in contracts}) != 1
        ):
            raise ValueError(
                "distinct contracts in one currency required; split cross-currency studies"
            )
        self.currency = contracts[0].currency
        self.fee = decimal(fee_per_contract)
        self.slippage = decimal(slippage_ticks)
        if (
            self.fee < 0
            or self.slippage < 0
            or self.slippage != int(self.slippage)
            or decimal(initial_cash) <= 0
        ):
            raise ValueError("positive cash, nonnegative fee and integer slippage ticks required")
        specs = {c.instrument_id: specification(c) for c in contracts}
        for c in contracts:
            if c.kind != "option" or c.settlement != "physical":
                continue
            if c.underlying_kind == "future":
                underlying = self.contracts.get(c.underlying)
                if (
                    underlying is None
                    or underlying.kind != "future"
                    or underlying.multiplier != c.multiplier
                ):
                    raise ValueError(
                        "physical futures option requires linked future with same multiplier"
                    )
            elif c.underlying not in specs:
                specs[c.underlying] = InstrumentSpec(
                    instrument_id=c.underlying,
                    asset_class=AssetClass.EQUITY,
                    product_type="delivery_underlying",
                    venue=c.venue,
                    native_symbol=c.underlying,
                    settlement_currency=c.currency,
                    quote_currency=c.currency,
                    price_tick=fp(c.tick),
                    quantity_step=fp(1, 0),
                    contract_multiplier=fp(1, 0),
                    calendar_id=c.timezone,
                    effective_from=c.listed_at,
                    available_at=c.known_at,
                    metadata={"research_delivery": "true"},
                )
        self.ledger = ExactAccountLedger(
            account_id="derivatives-research",
            base_currency=self.currency,
            instruments=specs,
            initial_cash={self.currency: fp(initial_cash)},
            opened_at=utc(opened_at),
        )
        self.fills = []
        self.lifecycle = []
        self._events = {}

    def quantity(self, instrument_id):
        return decimal(self.ledger.snapshot().positions.get(instrument_id, fp(0, 0)).to_decimal())

    def _time(self, at):
        stamp = utc(at)
        if stamp < self.ledger.snapshot().event_time:
            raise ValueError("event cannot precede current account time")
        return stamp

    def _fields(self, instrument_id, at, event_id):
        c = self.contracts.get(instrument_id)
        day = at.astimezone(ZoneInfo(c.timezone if c else "UTC")).date()
        return {
            "event_id": event_id,
            "instrument_id": instrument_id,
            "event_time": at,
            "received_at": at,
            "available_at": at,
            "source": "derivative-research",
            "trading_day": day,
            "session_id": f"research:{day}",
            "sequence": 0,
        }

    def _duplicate(self, event_id, fingerprint):
        if not event_id or not isinstance(event_id, str):
            raise ValueError("event ID required")
        if event_id in self._events:
            if self._events[event_id] != fingerprint:
                raise ValueError("event ID reused with changed content")
            return True
        return False

    def mark(self, instrument_id, price, at, event_id):
        at, price = utc(at), decimal(price)
        fingerprint = ("mark", instrument_id, price, at)
        if self._duplicate(event_id, fingerprint):
            return
        self._time(at)
        self.ledger.mark(
            MarkPriceEvent(**self._fields(instrument_id, at, event_id), price=fp(price))
        )
        self._events[event_id] = fingerprint

    def trade_batch(self, legs, at, event_id):
        """Execute a declared simultaneous basket atomically; no leg-order margin artifact."""
        at = utc(at)
        legs = tuple(
            (instrument_id, decimal(quantity), decimal(price))
            for instrument_id, quantity, price in legs
        )
        fingerprint = ("basket", legs, at)
        if self._duplicate(event_id, fingerprint):
            return
        self._time(at)
        if not legs or len({leg[0] for leg in legs}) != len(legs):
            raise ValueError("basket needs distinct instruments")
        state = self.ledger.capture_state()
        records = []
        try:
            for i, (instrument_id, quantity, price) in enumerate(legs):
                c = self.contracts[instrument_id]
                if not max(c.listed_at, c.known_at) <= at <= c.last_trade_at:
                    raise ValueError("trade outside contract tradability/knowledge interval")
                if quantity == 0 or quantity != int(quantity) or price <= 0:
                    raise ValueError("nonzero integer contracts and positive prices required")
                final_quantity = self.quantity(instrument_id) + quantity
                if c.initial_margin is None and (c.kind == "future" or final_quantity < 0):
                    raise ValueError(
                        "explicit margin assumptions required for futures or short options"
                    )
                side = Side.BUY if quantity > 0 else Side.SELL
                rounding = ROUND_CEILING if quantity > 0 else ROUND_FLOOR
                execution = (
                    (price / c.tick).to_integral_value(rounding=rounding)
                    + (self.slippage if quantity > 0 else -self.slippage)
                ) * c.tick
                trade_id = f"{event_id}:{i}"
                self._fill(instrument_id, quantity, execution, at, trade_id)
                fee = self.fee * abs(quantity)
                self.ledger.apply(
                    Fee(
                        fee_id=trade_id + ":fee",
                        fill_id=trade_id,
                        account_id=self.ledger.account_id,
                        amount=fp(fee),
                        currency=self.currency,
                        event_time=at,
                        fee_type="commission",
                    )
                )
                records.append(
                    {
                        "trade_id": trade_id,
                        "instrument_id": instrument_id,
                        "at": at.isoformat(),
                        "quantity": str(quantity),
                        "price": str(execution),
                        "fee": str(fee),
                        "side": side.value,
                    }
                )
            snapshot = self.ledger.snapshot()
            if (
                snapshot.nav.units < snapshot.initial_margin.units
                or self.ledger.cash_balance(self.currency) < 0
            ):
                raise ValueError("insufficient cash or declared initial margin")
        except Exception:
            self.ledger.restore_state(state)
            raise
        self.fills.extend(records)
        self._events[event_id] = fingerprint

    def _fill(self, instrument_id, quantity, price, at, trade_id):
        self.ledger.apply(
            Fill(
                fill_id=trade_id,
                order_id=trade_id,
                account_id=self.ledger.account_id,
                strategy_id="research",
                instrument_id=instrument_id,
                side=Side.BUY if quantity > 0 else Side.SELL,
                quantity=fp(abs(quantity), 0),
                price=fp(price),
                event_time=at,
            )
        )

    def settle_future(self, instrument_id, price, at, event_id):
        at, price = utc(at), decimal(price)
        fingerprint = ("settle", instrument_id, price, at)
        if self._duplicate(event_id, fingerprint):
            return
        if self.contracts[instrument_id].kind != "future":
            raise ValueError("daily settlement requires a future")
        self._time(at)
        state = self.ledger.capture_state()
        try:
            self.ledger.mark(
                MarkPriceEvent(
                    **self._fields(instrument_id, at, event_id + ":mark"), price=fp(price)
                )
            )
            event = StatusEvent(
                **self._fields(instrument_id, at, event_id), status="daily_settlement"
            )
            settlement = self.ledger.settlement_from_market(event)
            if settlement is not None:
                self.ledger.apply(settlement)
        except Exception:
            self.ledger.restore_state(state)
            raise
        self._events[event_id] = fingerprint

    def exercise(self, instrument_id, underlying_price, at, event_id, *, early=False):
        """Explicit full-position exercise/assignment; zero intrinsic expires worthless.

        Physical spot delivery can expose funding/short-stock requirements, reported
        by snapshot as a breach. No automatic loan, stock borrow or liquidation.
        """
        at, underlying_price = utc(at), decimal(underlying_price)
        fingerprint = ("exercise", instrument_id, underlying_price, at, early)
        if self._duplicate(event_id, fingerprint):
            return
        self._time(at)
        c = self.contracts[instrument_id]
        if c.kind != "option" or underlying_price <= 0:
            raise ValueError("option and positive observed underlying required")
        if early:
            if c.exercise_style != "american" or not max(c.listed_at, c.known_at) <= at < c.expiry:
                raise ValueError("early exercise only inside American option life")
        elif at < c.expiry:
            raise ValueError("expiry cannot occur before contract expiry")
        quantity = self.quantity(instrument_id)
        sign = Decimal(1 if c.option_right == "call" else -1)
        intrinsic = max(Decimal(0), sign * (underlying_price - c.strike))
        payout = intrinsic * c.multiplier if c.settlement == "cash" else Decimal(0)
        state = self.ledger.capture_state()
        try:
            self.ledger.apply(
                CorporateActionEvent(
                    **self._fields(instrument_id, at, event_id),
                    action_type="terminal_cash",
                    effective_date=at.date(),
                    ratio=fp(0, 0),
                    cash_amount=fp(payout),
                    currency=c.currency,
                )
            )
            if c.settlement == "physical" and intrinsic > 0 and quantity != 0:
                units = quantity * sign * (c.multiplier if c.underlying_kind == "spot" else 1)
                self._fill(c.underlying, units, c.strike, at, event_id + ":delivery")
                self.ledger.mark(
                    MarkPriceEvent(
                        **self._fields(c.underlying, at, event_id + ":mark"),
                        price=fp(underlying_price),
                    )
                )
        except Exception:
            self.ledger.restore_state(state)
            raise
        self.lifecycle.append(
            {
                "event_id": event_id,
                "instrument_id": instrument_id,
                "at": at.isoformat(),
                "kind": "early_exercise" if early else "expiry",
                "settlement": c.settlement,
                "quantity": str(quantity),
                "intrinsic": str(intrinsic),
                "cash": str(payout * quantity),
            }
        )
        self._events[event_id] = fingerprint

    def snapshot(self):
        snap = self.ledger.snapshot()
        cash = self.ledger.cash_balance(self.currency)
        delivered_short = any(
            i not in self.contracts and q.units < 0 for i, q in snap.positions.items()
        )
        return {
            "at": snap.event_time.isoformat(),
            "currency": self.currency,
            "nav": str(snap.nav.to_decimal()),
            "cash": str(cash),
            "initial_margin": str(snap.initial_margin.to_decimal()),
            "maintenance_margin": str(snap.maintenance_margin.to_decimal()),
            "margin_breach": bool(snap.liquidation_required or cash < 0 or delivered_short),
            "positions": {i: str(q.to_decimal()) for i, q in snap.positions.items()},
            "margin_model": "sum of declared per-contract margin; no offsets or stock-borrow model",
        }

    def validate_balance(self):
        for transaction in self.ledger.transactions:
            totals = {}
            for posting in transaction.postings:
                totals[posting.currency] = totals.get(posting.currency, Decimal(0)) + decimal(
                    posting.amount.to_decimal()
                )
            if any(totals.values()):
                raise AssertionError("unbalanced double-entry transaction")
