"""Borrow-gated, premium-funded research baskets on one exact shared ledger.

Cash equities use the existing signed delivery accounting explicitly. Short sale
proceeds and a declared collateral add-on are reserved; settlement is evidenced
per basket, never assumed to grant broker buying power. No live orders.
"""

from copy import deepcopy
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal

from quant_data_kit import AssetClass, FixedPoint, MarkPriceEvent
from quant_data_kit.derivatives.models import decimal, utc
from quant_data_kit.exceptions import ValidationError

from .contracts import Fee, Fill, Funding, Side
from .ledger import ExactAccountLedger
from .schemas import execution_payload


def fp(value):
    return FixedPoint.from_decimal(decimal(value), 8, rounding=ROUND_HALF_EVEN)


def ledger_rows(report):
    """Exact decimal postings for independently checkable CSV artifacts."""
    return [
        {
            "transaction_id": tx["transaction_id"],
            "event_time": tx["event_time"],
            "event_type": tx["event_type"],
            "currency": p["currency"],
            "ledger_account": p["ledger_account"],
            "instrument_id": p["instrument_id"],
            "amount": str(FixedPoint(**p["amount"]).to_decimal()),
        }
        for tx in report["ledger"]
        for p in tx["postings"]
    ]


class ResearchBasketAccount:
    sends_live_orders = False

    def __init__(self, specs, initial_cash, opened_at, *, short_collateral_addon="0.5"):
        self.specs = {
            s.instrument_id: replace(s, metadata={**s.metadata, "research_delivery": "true"})
            if s.asset_class in {AssetClass.EQUITY, AssetClass.ETF}
            else s
            for s in specs
        }
        currencies = {s.settlement_currency for s in self.specs.values()}
        if not self.specs or len(currencies) != 1 or decimal(initial_cash) <= 0:
            raise ValueError("one currency, nonempty specs and positive cash required")
        self.currency = next(iter(currencies))
        self.addon = decimal(short_collateral_addon)
        if not 0 <= self.addon <= 5:
            raise ValueError("invalid short collateral add-on")
        if any(
            s.asset_class not in {AssetClass.EQUITY, AssetClass.ETF, AssetClass.OPTION}
            for s in self.specs.values()
        ):
            raise ValueError("premium-funded spot equities/ETFs and options required")
        self.ledger = ExactAccountLedger(
            account_id="research-basket",
            base_currency=self.currency,
            instruments=self.specs,
            initial_cash={self.currency: fp(initial_cash)},
            opened_at=utc(opened_at),
        )
        self.pending, self.fills, self.events = [], [], {}

    def quantity(self, identity):
        return self.ledger.snapshot().positions.get(identity, fp(0)).to_decimal()

    def mark(self, identity, price, at, event_id):
        at = utc(at)
        self.ledger.mark(
            MarkPriceEvent(
                event_id=event_id,
                instrument_id=identity,
                event_time=at,
                received_at=at,
                available_at=at,
                source="declared-research",
                trading_day=at.date(),
                session_id=at.date().isoformat(),
                sequence=0,
                price=fp(price),
            )
        )

    def _collateral(self, marks):
        return sum(
            (
                abs(self.quantity(i)) * decimal(marks[i]) * (1 + self.addon)
                for i, s in self.specs.items()
                if self.quantity(i) < 0 and s.asset_class in {AssetClass.EQUITY, AssetClass.ETF}
            ),
            Decimal(0),
        )

    def execute(self, legs, *, at, event_id, marks, borrow=None, settles_at=None, fee_per_unit=0):
        at = utc(at)
        settles = utc(settles_at) if settles_at else at
        if settles < at or decimal(fee_per_unit) < 0:
            raise ValueError("settlement cannot precede execution; nonnegative fees required")
        legs = tuple((i, decimal(q), decimal(p)) for i, q, p in legs)
        fingerprint = repr((legs, at, sorted(marks.items()), borrow, settles, str(fee_per_unit)))
        if event_id in self.events:
            if self.events[event_id] != fingerprint:
                raise ValueError("basket ID reused with changed content")
            return
        if not event_id or not legs or len({i for i, _, _ in legs}) != len(legs):
            raise ValueError("nonempty unique basket and ID required")
        checkpoint = self.ledger.capture_state()
        previous = deepcopy((self.pending, self.fills))
        try:
            # Refresh valuation before calculating collateral and trading. No quote
            # from outside this observed basket is inferred by the account.
            for i in self.specs:
                if self.quantity(i) or any(i == leg[0] for leg in legs):
                    self.mark(i, marks[i], at, f"{event_id}:mark:{i}")
            for n, (identity, quantity, price) in enumerate(legs):
                spec = self.specs[identity]
                if quantity == 0 or price <= 0 or quantity % spec.quantity_step.to_decimal():
                    raise ValueError("positive price, nonzero step-aligned quantity required")
                if price % spec.price_tick.to_decimal():
                    raise ValueError("execution price is not tick-aligned")
                if not max(spec.available_at, spec.effective_from) <= at:
                    raise ValueError("instrument terms unavailable at execution")
                old, final = self.quantity(identity), self.quantity(identity) + quantity
                if final < 0 and spec.asset_class in {AssetClass.EQUITY, AssetClass.ETF}:
                    evidence = (borrow or {}).get(identity)
                    if not evidence or not (
                        utc(evidence["available_at"]) <= at < utc(evidence["expires_at"])
                    ):
                        raise ValueError("valid borrow evidence required for short inventory")
                    if decimal(evidence["units"]) < abs(final) or not evidence.get("source"):
                        raise ValueError("insufficient evidenced borrow units")
                if (
                    final < 0
                    and spec.asset_class is AssetClass.OPTION
                    and "initial_margin_per_contract" not in spec.metadata
                ):
                    raise ValueError("short option requires declared margin")
                key = f"{event_id}:{n}"
                self.ledger.apply(
                    Fill(
                        fill_id=key,
                        order_id=key,
                        account_id=self.ledger.account_id,
                        strategy_id="research-basket",
                        instrument_id=identity,
                        side=Side.BUY if quantity > 0 else Side.SELL,
                        quantity=fp(abs(quantity)),
                        price=fp(price),
                        event_time=at,
                    )
                )
                fee = fp(abs(quantity) * decimal(fee_per_unit))
                self.ledger.apply(
                    Fee(
                        fee_id=key + ":fee",
                        fill_id=key,
                        account_id=self.ledger.account_id,
                        amount=fee,
                        currency=self.currency,
                        event_time=at,
                        fee_type="commission",
                    )
                )
                # Only disposal of pre-owned positive units releases sale proceeds.
                long_sale = min(max(old, Decimal(0)), max(-quantity, Decimal(0)))
                if long_sale and settles > at:
                    self.pending.append(
                        (settles, long_sale * price * spec.contract_multiplier.to_decimal())
                    )
                self.fills.append(
                    {
                        "at": at.isoformat(),
                        "instrument_id": identity,
                        "quantity": str(quantity),
                        "price": str(price),
                        "fee": str(fee.to_decimal()),
                        "settles_at": settles.isoformat(),
                        "event_id": key,
                    }
                )
            snap = self.ledger.snapshot(at)
            unsettled = sum((amount for date, amount in self.pending if date > at), Decimal(0))
            collateral = self._collateral(marks)
            required = collateral + snap.initial_margin.to_decimal() + unsettled
            if (
                self.ledger.cash_balance(self.currency) < required
                or snap.nav.to_decimal() < snap.initial_margin.to_decimal()
            ):
                raise ValueError(
                    "insufficient cash after margin, short collateral and unsettled proceeds"
                )
            self.validate()
        except Exception as exc:
            self.ledger.restore_state(checkpoint)
            self.pending, self.fills = previous
            if isinstance(exc, ValidationError):
                raise ValueError(str(exc)) from exc  # noqa: TRY004 - normalize ledger validation
            raise
        self.events[event_id] = fingerprint

    def charge(self, amount, at, event_id, kind="funding_and_borrow"):
        amount = fp(amount)
        if amount.units < 0:
            raise ValueError("nonnegative costs required")
        self.ledger.apply(
            Funding(
                funding_id=kind + ":" + event_id,
                instrument_id=next(iter(self.specs)),
                account_id=self.ledger.account_id,
                amount=fp(-amount.to_decimal()),
                currency=self.currency,
                event_time=utc(at),
            )
        )

    def check_risk(self, marks, at, borrow=None):
        """Validate carried inventory even when there is no new order."""
        at = utc(at)
        for identity, spec in self.specs.items():
            if self.quantity(identity) >= 0 or spec.asset_class is AssetClass.OPTION:
                continue
            evidence = (borrow or {}).get(identity)
            if not evidence or not utc(evidence["available_at"]) <= at < utc(
                evidence["expires_at"]
            ):
                raise ValueError("carried short requires unexpired borrow evidence")
            if decimal(evidence["units"]) < abs(self.quantity(identity)) or not evidence.get(
                "source"
            ):
                raise ValueError("carried short exceeds evidenced borrow")
        snap = self.ledger.snapshot(at)
        pending = sum((value for date, value in self.pending if date > at), Decimal(0))
        required = self._collateral(marks) + snap.initial_margin.to_decimal() + pending
        if (
            self.ledger.cash_balance(self.currency) < required
            or snap.nav.to_decimal() < snap.initial_margin.to_decimal()
        ):
            raise ValueError("insufficient carried collateral or margin")
        self.validate()

    def validate(self):
        for tx in self.ledger.transactions:
            totals = {}
            for posting in tx.postings:
                totals[posting.currency] = (
                    totals.get(posting.currency, Decimal(0)) + posting.amount.to_decimal()
                )
            if any(totals.values()):
                raise AssertionError("unbalanced research transaction")

    def report(self):
        self.validate()
        return {
            "final_account": execution_payload(self.ledger.snapshot()),
            "fills": self.fills,
            "ledger": [execution_payload(tx) for tx in self.ledger.transactions],
            "ledger_balanced": True,
            "sends_live_orders": False,
        }
