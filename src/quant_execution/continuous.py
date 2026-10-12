"""Finite, offline shared-capital research on the existing exact QExec ledger.

One economic account, with disjoint attribution books whose opening allocations
sum to its cash. Attribution books are never additional buying power. Instruments
have a fixed sleeve owner; there are no cross-sleeve position/margin offsets.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext

from quant_data_kit import AssetClass, FixedPoint, MarkPriceEvent
from quant_data_kit.artifacts import canonical_json
from quant_data_kit.domain_v2 import dataclass_payload
from quant_data_kit.temporal_v2 import ensure_utc_datetime, parse_timestamp_exact

from .contracts import Fee, Fill, Funding, LedgerEventType, Settlement, Side
from .ledger import ExactAccountLedger
from .schemas import execution_payload

ZERO = Decimal(0)
EVENT_FIELDS = {
    "mark": {"instrument_id", "price"},
    "fx_mark": {"currency", "rate"},
    "settle": set(),
    "transfer": {"source", "target", "currency", "amount"},
    "external_cash": {"sleeve", "currency", "amount"},
    "fx_convert": {
        "sleeve",
        "source_currency",
        "target_currency",
        "source_amount",
        "target_amount",
    },
    "fill": {"sleeve", "instrument_id", "quantity", "price", "fee", "settles_at"},
    "charge": {"sleeve", "instrument_id", "amount"},
    "variation_margin": {"sleeve", "instrument_id", "price", "amount"},
}
UNSUPPORTED = [
    "live_execution",
    "market_validity",
    "short_cash_assets",
    "options",
    "inverse_derivatives",
    "cross_sleeve_netting",
    "automatic_liquidation",
    "inferred_corporate_actions",
    "broker_specific_margin",
    "historical_state_queries",
]


def number(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise TypeError("exact decimal string or integer required")
    result = Decimal(value)
    if not result.is_finite():
        raise ValueError("finite amount required")
    return result


def fp(value):
    return FixedPoint.from_decimal(number(value), 8)


def utc(value):
    return ensure_utc_datetime(parse_timestamp_exact(value, field="at"), field="at")


def digest(value):
    return hashlib.sha256(canonical_json(value)).hexdigest()


class _CashLedger(ExactAccountLedger):
    """Closed research cash journal; no changes to existing public ledger methods."""

    def native_future_pnl(self, instrument, mark):
        # Collateral is reserved in its native currency. Never divide a rounded
        # base-currency reporting PnL by FX: that can erase a small native loss.
        quantity = self._positions.get(instrument, ZERO)
        spec = self.instruments[instrument]
        return quantity * mark * spec.contract_multiplier.to_decimal() - self._position_cost(
            instrument, derivative=True
        )

    def cash_journal(self, identity, changes, at, category):
        changes = {currency: fp(amount).to_decimal() for currency, amount in changes.items()}
        counterpart = {
            "external": "equity:external_flows",
            "transfer": "clearing:sleeve_transfer",
            "fx": "clearing:fx_conversion",
        }[category]
        postings = tuple(
            posting
            for currency, amount in sorted(changes.items())
            for posting in (
                self._posting("assets:cash", currency, number(amount)),
                self._posting(counterpart, currency, -number(amount)),
            )
        )
        tx = self._make_transaction(
            event_type=LedgerEventType.FX_CONVERSION,
            reference_id=identity,
            idempotency_key=identity,
            event_time=at,
            postings=postings,
        )
        self._post(tx)
        self._event_time = at


class SharedCapitalAccount:
    """Apply declared executions, never synthesize fills from returns.

    All amounts are fixed to eight decimal places; excess precision is rejected.
    Sale proceeds and purchased units remain reserved until their explicit UTC
    settlement instant. At every event, matured holds are released first. Fees are
    paid on execution. Futures use explicit linear notional margin rates, with
    negative unrealized PnL reserved and positive PnL unavailable until settlement.
    """

    sends_live_orders = False

    def __init__(self, *, specs, owners, allocations, base_currency, opened_at, fx_rates=None):
        self.opened_at = utc(opened_at)
        self.base_currency = base_currency
        self.specs = {}
        for spec in specs:
            if spec.instrument_id in self.specs:
                raise ValueError("duplicate instrument")
            if spec.asset_class not in {AssetClass.EQUITY, AssetClass.ETF, AssetClass.FUTURE}:
                raise ValueError("only long cash equities/ETFs and linear futures supported")
            if spec.inverse or spec.quote_currency not in {None, spec.settlement_currency}:
                raise ValueError("inverse or quanto instruments unsupported")
            if max(spec.available_at, spec.effective_from) > self.opened_at:
                raise ValueError("instrument unavailable at account opening")
            if spec.asset_class is AssetClass.FUTURE:
                initial = number(spec.metadata.get("initial_margin_rate", "-1"))
                maintenance = number(spec.metadata.get("maintenance_margin_rate", "-1"))
                if not 0 < maintenance <= initial <= 1:
                    raise ValueError("explicit positive maintenance/initial margin rates required")
                if "initial_margin_per_contract" in spec.metadata:
                    raise ValueError("only notional-rate futures margin supported")
            else:
                spec = replace(spec, metadata={**spec.metadata, "research_delivery": "true"})
            self.specs[spec.instrument_id] = spec
        if not self.specs or set(owners) != set(self.specs) or not allocations:
            raise ValueError("nonempty instruments, exact owners and allocations required")
        self.owners = dict(owners)
        self.allocations = deepcopy(allocations)
        if any(not isinstance(s, str) or not s.strip() or s == "total" for s in allocations):
            raise ValueError("nonempty sleeve IDs required")
        if not set(owners.values()) <= set(allocations):
            raise ValueError("unknown instrument sleeve")
        currencies = {base_currency} | {s.settlement_currency for s in self.specs.values()}
        currencies |= {c for cash in allocations.values() for c in cash}
        self.currencies = sorted(currencies)
        rates = {c: number(v) for c, v in (fx_rates or {}).items()}
        if rates.get(base_currency, Decimal(1)) != 1:
            raise ValueError("base FX must equal one")
        rates[base_currency] = Decimal(1)
        if set(rates) != currencies or any(v <= 0 for v in rates.values()):
            raise ValueError("explicit opening FX for every currency required")
        self.rates = rates
        self.configuration_sha256 = digest(
            {
                "instruments": [dataclass_payload(s) for _, s in sorted(self.specs.items())],
                "owners": self.owners,
                "allocations": self.allocations,
                "base_currency": base_currency,
                "opened_at": self.opened_at.isoformat(),
                "fx_rates": {c: str(v) for c, v in rates.items()},
            }
        )
        kwargs = {
            "base_currency": base_currency,
            "instruments": self.specs,
            "opened_at": self.opened_at,
            "fx_to_base": {c: fp(v) for c, v in rates.items()},
        }
        totals = {c: ZERO for c in currencies}
        self.sleeves = {}
        for sleeve, cash in sorted(allocations.items()):
            values = {c: number(cash.get(c, "0")) for c in currencies}
            if any(v < 0 for v in values.values()):
                raise ValueError("negative opening allocation")
            for c, v in values.items():
                totals[c] += v
            self.sleeves[sleeve] = _CashLedger(
                account_id="attribution:" + sleeve,
                initial_cash={c: fp(v) for c, v in values.items()},
                **kwargs,
            )
        self.ledger = _CashLedger(
            account_id="shared-research",
            initial_cash={c: fp(v) for c, v in totals.items()},
            **kwargs,
        )
        self.pending, self.history, self.events = [], [], []
        self.seen, self.marks = {}, {}
        self.last_key = (self.opened_at, -1)
        self.initial_nav = self.ledger.snapshot().nav.to_decimal()
        if self.initial_nav <= 0:
            raise ValueError("positive opening NAV required")
        self.external_flows = ZERO
        self.wealth = Decimal(1)
        self.return_defined = True

    def _quantity(self, sleeve, instrument):
        return (
            self.sleeves[sleeve]
            .snapshot(self.last_key[0])
            .positions.get(instrument, fp(0))
            .to_decimal()
        )

    def _books(self, sleeve):
        return (self.ledger, self.sleeves[sleeve])

    def liquidity(self, sleeve=None, *, maintenance=False):
        book = self.ledger if sleeve is None else self.sleeves[sleeve]
        snap = book.snapshot(self.last_key[0])
        free = {c: book.cash_balance(c) for c in self.currencies}
        for item in self.pending:
            if sleeve is None or item["sleeve"] == sleeve:
                free[item["currency"]] -= item["cash"]
        for instrument, quantity in snap.positions.items():
            spec = self.specs[instrument]
            if spec.asset_class is AssetClass.FUTURE and quantity.units:
                price = self.marks[instrument]
                notional = abs(
                    quantity.to_decimal() * price * spec.contract_multiplier.to_decimal()
                )
                # FixedPoint rounding happens only in the underlying ledger; liquidity
                # keeps the exact conservative reserve, never rounds buying power up.
                free[spec.settlement_currency] -= notional * number(
                    spec.metadata[
                        "maintenance_margin_rate" if maintenance else "initial_margin_rate"
                    ]
                )
                pnl = book.native_future_pnl(instrument, price)
                free[spec.settlement_currency] += min(ZERO, pnl)
        return free

    def _mark(self, instrument, price, at, identity):
        if instrument not in self.specs or number(price) <= 0:
            raise ValueError("known instrument and positive mark required")
        spec = self.specs[instrument]
        if number(price) % spec.price_tick.to_decimal():
            raise ValueError("mark not tick aligned")
        for book in (self.ledger, *self.sleeves.values()):
            book.mark(
                MarkPriceEvent(
                    event_id=identity,
                    instrument_id=instrument,
                    price=fp(price),
                    event_time=at,
                    received_at=at,
                    available_at=at,
                    source="declared-research",
                    trading_day=at.date(),
                    session_id=at.date().isoformat(),
                    sequence=0,
                )
            )
        self.marks[instrument] = number(price)

    def _dispatch(self, e, at):
        kind, identity = e["kind"], e["event_id"]
        sleeve = e.get("sleeve")
        flow, risk_reducing = ZERO, False
        if kind == "mark":
            self._mark(e["instrument_id"], e["price"], at, identity)
        elif kind == "fx_mark":
            currency, rate = e["currency"], number(e["rate"])
            if currency not in self.currencies or rate <= 0:
                raise ValueError("known currency and positive FX required")
            for book in (self.ledger, *self.sleeves.values()):
                book.set_fx_rate(currency, fp(rate), event_time=at)
            self.rates[currency] = rate
        elif kind == "settle":
            pass  # Maturity release is explicit in the event history.
        elif kind == "transfer":
            source, target, c = e["source"], e["target"], e["currency"]
            amount = number(e["amount"])
            if source == target or amount <= 0 or amount > self.liquidity(source)[c]:
                raise ValueError("invalid or unfunded internal transfer")
            self.sleeves[source].cash_journal(identity, {c: -amount}, at, "transfer")
            self.sleeves[target].cash_journal(identity, {c: amount}, at, "transfer")
        elif kind == "external_cash":
            c, amount = e["currency"], number(e["amount"])
            if c not in self.currencies or amount == 0:
                raise ValueError("known currency and nonzero flow required")
            for book in self._books(sleeve):
                book.cash_journal(identity, {c: amount}, at, "external")
            flow = amount * self.rates[c]
        elif kind == "fx_convert":
            source, target = e["source_currency"], e["target_currency"]
            sold, bought = number(e["source_amount"]), number(e["target_amount"])
            if source == target or source not in self.currencies or target not in self.currencies:
                raise ValueError("distinct known FX currencies required")
            if sold <= 0 or bought <= 0 or sold > self.liquidity(sleeve)[source]:
                raise ValueError("positive funded FX amounts required")
            for book in self._books(sleeve):
                book.cash_journal(identity, {source: -sold, target: bought}, at, "fx")
        elif kind in {"fill", "charge", "variation_margin"}:
            instrument = e["instrument_id"]
            if self.owners[instrument] != sleeve:
                raise ValueError("instrument belongs to another sleeve")
            spec = self.specs[instrument]
            if (spec.effective_to is not None and at >= spec.effective_to) or (
                spec.superseded_at is not None and at >= spec.superseded_at
            ):
                raise ValueError("instrument terms expired or superseded")
            c = spec.settlement_currency
            if kind == "fill":
                quantity, price, fee = number(e["quantity"]), number(e["price"]), number(e["fee"])
                settles = utc(e["settles_at"])
                if settles < at or fee < 0 or quantity == 0 or price <= 0:
                    raise ValueError("invalid fill, fee or settlement time")
                if (
                    quantity % spec.quantity_step.to_decimal()
                    or price % spec.price_tick.to_decimal()
                ):
                    raise ValueError("fill not step/tick aligned")
                old = self._quantity(sleeve, instrument)
                final = old + quantity
                if spec.asset_class is not AssetClass.FUTURE:
                    held = sum(
                        (p["units"] for p in self.pending if p["instrument"] == instrument), ZERO
                    )
                    if final < 0 or (quantity < 0 and -quantity > old - held):
                        raise ValueError("short cash inventory or sale of unsettled units")
                    if (
                        quantity > 0
                        and quantity * price * spec.contract_multiplier.to_decimal() + fee
                        > self.liquidity(sleeve)[c]
                    ):
                        raise ValueError("insufficient settled cash or margin")
                risk_reducing = abs(final) < abs(old) and old * final >= 0
                self._mark(instrument, price, at, identity + ":execution-mark")
                for book in self._books(sleeve):
                    book.apply(
                        Fill(
                            fill_id=identity,
                            order_id=e.get("order_id", identity),
                            account_id=book.account_id,
                            strategy_id=sleeve,
                            instrument_id=instrument,
                            side=Side.BUY if quantity > 0 else Side.SELL,
                            quantity=fp(abs(quantity)),
                            price=fp(price),
                            event_time=at,
                        )
                    )
                    book.apply(
                        Fee(
                            fee_id=identity + ":fee",
                            fill_id=identity,
                            account_id=book.account_id,
                            amount=fp(fee),
                            currency=c,
                            event_time=at,
                            fee_type="explicit_research_fee",
                        )
                    )
                if spec.asset_class is not AssetClass.FUTURE and settles > at:
                    self.pending.append(
                        {
                            "event_id": identity,
                            "sleeve": sleeve,
                            "currency": c,
                            "instrument": instrument,
                            "at": settles,
                            "units": max(ZERO, quantity),
                            "cash": max(ZERO, -quantity)
                            * price
                            * spec.contract_multiplier.to_decimal(),
                        }
                    )
            elif kind == "charge":
                amount = number(e["amount"])
                if amount < 0:
                    raise ValueError("nonnegative charge required")
                for book in self._books(sleeve):
                    book.apply(
                        Funding(
                            funding_id=identity,
                            instrument_id=instrument,
                            account_id=book.account_id,
                            amount=fp(-amount),
                            currency=c,
                            event_time=at,
                        )
                    )
            else:
                if spec.asset_class is not AssetClass.FUTURE:
                    raise ValueError("variation margin requires future")
                self._mark(instrument, e["price"], at, identity + ":settlement-mark")
                for book in self._books(sleeve):
                    book.apply(
                        Settlement(
                            settlement_id=identity,
                            account_id=book.account_id,
                            instrument_id=instrument,
                            amount=fp(e["amount"]),
                            currency=c,
                            event_time=at,
                            settlement_type="daily_mark",
                            settlement_price=fp(e["price"]),
                        )
                    )
        else:
            raise ValueError("unsupported event kind")
        return flow, risk_reducing

    def apply(self, event):
        """Atomic event admission; rejected attempts never alter the account."""
        with localcontext() as context:
            context.prec = 64
            return self._apply(event)

    def _apply(self, event):
        e = json.loads(canonical_json(event))
        kind = e.get("kind")
        required = {"event_id", "at", "sequence", "available_at", "kind"} | EVENT_FIELDS.get(
            kind, set()
        )
        optional = {"order_id"} if kind == "fill" else set()
        if kind not in EVENT_FIELDS or not required <= set(e) or set(e) - required - optional:
            raise ValueError("unsupported event kind or fields")
        identity, fingerprint = e["event_id"], digest(e)
        if not isinstance(identity, str) or not identity.strip():
            raise ValueError("nonempty event ID required")
        if identity in self.seen:
            if self.seen[identity] != fingerprint:
                raise ValueError("event ID reused with changed content")
            return
        at = utc(e["at"])
        sequence = e["sequence"]
        if type(sequence) is not int or sequence < 0 or (at, sequence) <= self.last_key:
            raise ValueError("strictly increasing UTC/sequence required across chunks")
        if "available_at" not in e or utc(e["available_at"]) > at:
            raise ValueError("explicit availability no later than event required")
        states = [book.capture_state() for book in (self.ledger, *self.sleeves.values())]
        previous = deepcopy((self.pending, self.marks, self.rates, self.last_key))
        before = self.ledger.snapshot(self.last_key[0]).nav.to_decimal()
        try:
            self.last_key = (at, sequence)
            released = [p["event_id"] for p in self.pending if p["at"] <= at]
            self.pending = [p for p in self.pending if p["at"] > at]
            old_free = {s: self.liquidity(s) for s in (None, *self.sleeves)}
            flow, reducing = self._dispatch(e, at)
            new_free = {s: self.liquidity(s) for s in old_free}
            # Marks, actual charges and settlement are economic facts, not orders:
            # preserve their losses and expose the resulting call without liquidation.
            if e["kind"] in {"fill", "transfer", "fx_convert", "external_cash"}:
                for s, free in new_free.items():
                    for c, amount in free.items():
                        if amount < 0 and not (
                            (
                                reducing
                                or e["kind"] == "transfer"
                                or (e["kind"] == "external_cash" and flow > 0)
                            )
                            and amount >= min(ZERO, old_free[s][c])
                        ):
                            raise ValueError("insufficient settled cash or margin")
            reconciliation = self.reconcile()
            after = self.ledger.snapshot(at).nav.to_decimal()
        except Exception:
            for book, state in zip((self.ledger, *self.sleeves.values()), states):
                book.restore_state(state)
            self.pending, self.marks, self.rates, self.last_key = previous
            raise
        event_return = (after - flow) / before - 1 if before > 0 else None
        self.external_flows += flow
        if event_return is None:
            self.return_defined = False
        elif self.return_defined:
            self.wealth *= 1 + event_return
        self.history.append(
            {
                "event_id": identity,
                "at": at.isoformat(),
                "sequence": sequence,
                "nav": str(after),
                "external_flow": str(flow),
                "net_return": None if event_return is None else str(event_return),
                "released_settlements": released,
                "free_cash": {
                    "total" if s is None else s: {c: str(v) for c, v in f.items()}
                    for s, f in new_free.items()
                },
                "initial_margin_shortfall": any(
                    v < 0 for f in new_free.values() for v in f.values()
                ),
                "margin_call": any(
                    v < 0 for s in new_free for v in self.liquidity(s, maintenance=True).values()
                ),
                "reconciliation": reconciliation,
            }
        )
        self.events.append(e)
        self.seen[identity] = fingerprint

    def replay(self, events):
        ordered = sorted(events, key=lambda e: (utc(e["at"]), e["sequence"]))
        for event in ordered:
            self.apply(event)
        return self.report()

    def reconcile(self):
        at = self.last_key[0]
        total = self.ledger.snapshot(at)
        sleeves = [book.snapshot(at) for book in self.sleeves.values()]
        cash_residual = {
            c: self.ledger.cash_balance(c)
            - sum((book.cash_balance(c) for book in self.sleeves.values()), ZERO)
            for c in self.currencies
        }
        position_residual = {
            i: total.positions.get(i, fp(0)).to_decimal()
            - sum((s.positions.get(i, fp(0)).to_decimal() for s in sleeves), ZERO)
            for i in self.specs
        }
        nav_residual = total.nav.to_decimal() - sum((s.nav.to_decimal() for s in sleeves), ZERO)
        # Per-book base valuation rounds to 1e-8. Allow at most one quantum per
        # attribution book, while native-currency cash and units remain exact.
        if (
            any(cash_residual.values())
            or any(position_residual.values())
            or abs(nav_residual) > Decimal("1e-8") * len(sleeves)
        ):
            raise AssertionError("shared-capital attribution failed")
        for book in (self.ledger, *self.sleeves.values()):
            for tx in book.transactions:
                totals = {}
                for p in tx.postings:
                    totals[p.currency] = totals.get(p.currency, ZERO) + p.amount.to_decimal()
                if any(totals.values()):
                    raise AssertionError("unbalanced ledger transaction")
        return {
            "cash_residual": {c: str(v) for c, v in cash_residual.items()},
            "position_residual": {i: str(v) for i, v in position_residual.items()},
            "nav_residual": str(nav_residual),
            "ledger_balanced": True,
        }

    def report(self):
        at = self.last_key[0]
        final = self.ledger.snapshot(at)
        return {
            "schema": "quant.shared-capital/v1",
            "evidence_kind": "declared_research_execution",
            "sample_kind": "unclassified",
            "configuration_sha256": self.configuration_sha256,
            "sends_live_orders": False,
            "final_account": execution_payload(final),
            "sleeves": {
                s: execution_payload(book.snapshot(at)) for s, book in self.sleeves.items()
            },
            "ledger": [execution_payload(tx) for tx in self.ledger.transactions],
            "attribution_ledgers": {
                s: [execution_payload(tx) for tx in book.transactions]
                for s, book in self.sleeves.items()
            },
            "history": deepcopy(self.history),
            "events": deepcopy(self.events),
            "pending": [
                {**p, "at": p["at"].isoformat(), "cash": str(p["cash"]), "units": str(p["units"])}
                for p in self.pending
            ],
            "initial_nav": str(self.initial_nav),
            "external_flows": str(self.external_flows),
            "net_pnl": str(final.nav.to_decimal() - self.initial_nav - self.external_flows),
            "cumulative_return": str(self.wealth - 1) if self.return_defined else None,
            "reconciliation": self.reconcile(),
            "unsupported_scopes": UNSUPPORTED,
        }

    def checkpoint(self):
        """Portable replay checkpoint; hash-bound journal, not mutable private state."""
        payload = {
            "schema": "quant.capital-checkpoint/v1",
            "events": deepcopy(self.events),
            "configuration_sha256": self.configuration_sha256,
        }
        return {**payload, "sha256": digest(payload)}

    def restore(self, checkpoint):
        if self.events:
            raise ValueError("restore requires a fresh identically configured account")
        payload = {k: v for k, v in checkpoint.items() if k != "sha256"}
        if (
            payload.get("schema") != "quant.capital-checkpoint/v1"
            or digest(payload) != checkpoint["sha256"]
        ):
            raise ValueError("checkpoint integrity failed")
        if payload["configuration_sha256"] != self.configuration_sha256:
            raise ValueError("checkpoint configuration differs")
        states = [book.capture_state() for book in (self.ledger, *self.sleeves.values())]
        fields = (
            "pending",
            "history",
            "events",
            "seen",
            "marks",
            "rates",
            "last_key",
            "external_flows",
            "wealth",
            "return_defined",
        )
        previous = {name: deepcopy(getattr(self, name)) for name in fields}
        try:
            self.replay(payload["events"])
        except Exception:
            for book, state in zip((self.ledger, *self.sleeves.values()), states):
                book.restore_state(state)
            for name, value in previous.items():
                setattr(self, name, value)
            raise
