"""Exact journal for declared stock/vanilla-option research lifecycles.

Offline event replay only. Signed stock positions require dated borrow locates;
margin is collateral segregation, not an expense. Existing public journal types
are reused so each transaction balances per currency without schema changes.
"""

import copy
import hashlib
import json
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from quant_data_kit import FixedPoint
from quant_data_kit.derivatives.models import decimal, utc

from .contracts import LedgerEventType, LedgerTransaction, Posting
from .schemas import execution_payload


def amount(value):
    return FixedPoint.from_decimal(decimal(value), 8, rounding=ROUND_HALF_EVEN)


class ResearchLifecycle:
    sends_live_orders = False

    def __init__(self, currency, instruments):
        if not isinstance(currency, str) or len(currency) != 3 or not currency.isupper():
            raise ValueError("three-letter currency required")
        self.currency = currency
        self.instruments = copy.deepcopy(instruments)
        if not instruments:
            raise ValueError("instrument declarations required")
        for spec in self.instruments.values():
            required = {"kind", "currency", "multiplier", "known_at"}
            extra = {"underlying", "right", "strike", "expiry", "style", "settlement"}
            if not required <= set(spec) or set(spec) - required - extra:
                raise ValueError("invalid instrument specification")
            if spec["currency"] != currency or spec["kind"] not in {"stock", "option"}:
                raise ValueError("single currency stock/option portfolio required")
            if decimal(spec["multiplier"]) <= 0:
                raise ValueError("positive multiplier required")
            utc(spec["known_at"])
            if spec["kind"] == "stock":
                if decimal(spec["multiplier"]) != 1:
                    raise ValueError("stock multiplier must be one")
            else:
                if not extra <= set(spec) or spec["right"] not in {"call", "put"}:
                    raise ValueError("complete option contract required")
                if spec["style"] not in {"american", "european"} or spec["settlement"] not in {
                    "cash",
                    "physical",
                }:
                    raise ValueError("invalid option style/settlement")
                if decimal(spec["strike"]) <= 0:
                    raise ValueError("positive strike required")
                utc(spec["expiry"])
                underlying = instruments.get(spec["underlying"])
                if underlying is None or underlying["kind"] != "stock":
                    raise ValueError("declared underlying stock required")
        self.cash = Decimal(0)
        self.collateral = Decimal(0)
        self.required_margin = Decimal(0)
        self.positions = dict.fromkeys(instruments, Decimal(0))
        self.costs = dict.fromkeys(instruments, Decimal(0))
        self.marks = {}
        self.locates = {}
        self.dividends = {}
        self.pending = {}
        self.closed_options = set()
        self.accrued_to = None
        self.at = None
        self.ids = {}
        self.transactions = []
        self.margin_calls = []

    def _post(self, event, postings, typ=LedgerEventType.SETTLEMENT, quantity_deltas=None):
        postings = tuple(
            Posting(
                ledger_account=a,
                currency=self.currency,
                amount=amount(v),
                instrument_id=i,
                quantity_delta=amount(quantity_deltas[i])
                if quantity_deltas and a == "assets:position_cost" and i in quantity_deltas
                else None,
            )
            for a, v, i in postings
        )
        transaction = LedgerTransaction(
            transaction_id=event["id"] + f":{len(self.transactions)}",
            idempotency_key=event["id"] + f":{len(self.transactions)}",
            event_time=utc(event["at"]),
            event_type=typ,
            reference_id=event["id"],
            postings=postings,
        )
        self.transactions.append(transaction)

    def _trade(self, event, instrument, quantity, price, fee=0, *, settlement=False):
        spec = self.instruments[instrument]
        at = utc(event["at"])
        q, p, fee = decimal(quantity), decimal(price), decimal(fee)
        if any(amount(v).to_decimal() != v for v in (q, p)):
            raise ValueError("price and quantity support at most eight decimal places")
        if q == 0 or p < 0 or fee < 0 or spec["known_at"] and utc(spec["known_at"]) > at:
            raise ValueError("nonzero trade and available rules/nonnegative price/cost required")
        if spec["kind"] == "option" and (
            q != int(q) or (not settlement and at >= utc(spec["expiry"]))
        ):
            raise ValueError("integer unexpired option contracts required")
        if instrument in self.closed_options and not settlement:
            raise ValueError("option already settled")
        old, old_cost = self.positions[instrument], self.costs[instrument]
        new = old + q
        if spec["kind"] == "stock" and new < 0:
            locate = self.locates.get(instrument)
            if (
                locate is None
                or not utc(locate["available_at"]) <= at <= utc(locate["expires_at"])
                or -new > decimal(locate["quantity"])
            ):
                raise ValueError("short stock exceeds available unexpired borrow locate")
        multiplier = decimal(spec["multiplier"])
        closed = min(abs(old), abs(q)) if old * q < 0 else Decimal(0)
        removed_cost = amount(old_cost * closed / abs(old)).to_decimal() if closed else Decimal(0)
        opened = abs(q) - closed
        added_cost = amount(opened * (1 if q > 0 else -1) * p * multiplier).to_decimal()
        cost_delta = added_cost - removed_cost
        cash_delta = amount(-q * p * multiplier).to_decimal()
        fee = amount(fee).to_decimal()
        self._post(
            event,
            [
                ("assets:cash", cash_delta - fee, None),
                ("assets:position_cost", cost_delta, instrument),
                ("income:realized_pnl", -(cash_delta + cost_delta), instrument),
                ("expenses:commission", fee, instrument),
            ],
            LedgerEventType.FILL,
            quantity_deltas={instrument: q},
        )
        self.cash += cash_delta - fee
        self.positions[instrument] = new
        self.costs[instrument] += cost_delta

    def apply(self, event):
        fingerprint = hashlib.sha256(
            json.dumps(event, sort_keys=True, allow_nan=False).encode()
        ).hexdigest()
        if not event.get("id") or not isinstance(event["id"], str):
            raise ValueError("event id required")
        if event["id"] in self.ids:
            if self.ids[event["id"]] != fingerprint:
                raise ValueError("idempotency conflict")
            return self.snapshot()
        at = utc(event["at"])
        if self.at is not None and at < self.at:
            raise ValueError("events must be chronological")
        if utc(event["available_at"]) > at:
            raise ValueError("event not available at processing time")
        before = copy.deepcopy(self.__dict__)
        try:
            with localcontext() as context:
                context.prec = 60
                self._apply(event)
                self.at = at
                self.ids[event["id"]] = fingerprint
                return self.snapshot()
        except Exception:
            self.__dict__.clear()
            self.__dict__.update(before)
            raise

    def _apply(self, e):
        kind, at = e["type"], utc(e["at"])
        if kind == "deposit":
            value = amount(e["amount"]).to_decimal()
            if value <= 0:
                raise ValueError("positive capital deposit required")
            self._post(e, [("assets:cash", value, None), ("equity:capital", -value, None)])
            self.cash += value
        elif kind == "locate":
            instrument = e["instrument"]
            if (
                self.instruments[instrument]["kind"] != "stock"
                or decimal(e["quantity"]) < 0
                or utc(e["expires_at"]) < at
            ):
                raise ValueError("valid stock locate required")
            if decimal(e["quantity"]) < max(0, -self.positions[instrument]):
                raise ValueError("locate recall requires covering outstanding borrow first")
            self.locates[instrument] = dict(e)
        elif kind == "fill":
            self._trade(e, e["instrument"], e["quantity"], e["price"], e.get("fee", 0))
        elif kind == "mark":
            for instrument, p in e["prices"].items():
                if instrument not in self.instruments or decimal(p) < 0:
                    raise ValueError("nonnegative declared instrument marks required")
                self.marks[instrument] = (amount(p).to_decimal(), at)
        elif kind == "accrue":
            start = utc(e["start"])
            if start >= at or (self.accrued_to is not None and start != self.accrued_to):
                raise ValueError("contiguous nonoverlapping funding intervals required")
            # Caller supplies previous interval's balance via immutable state: any
            # intervening cash/position change requires a boundary accrual event.
            if self.at and start < self.at:
                raise ValueError("accrue before applying cash/position changes inside interval")
            days = Decimal(str((at - start).total_seconds())) / Decimal(86400)
            debit, credit = decimal(e["debit_rate"]), decimal(e["credit_rate"])
            if abs(debit) > 1 or abs(credit) > 1:
                raise ValueError("annual funding rate outside [-1,1]")
            interest = amount(
                self.cash * (debit if self.cash < 0 else credit) * days / 365
            ).to_decimal()
            borrowing = Decimal(0)
            for instrument, q in self.positions.items():
                if q < 0 and self.instruments[instrument]["kind"] == "stock":
                    if instrument not in e["borrow_rates"] or instrument not in self.marks:
                        raise ValueError("borrow rate and mark required for each short stock")
                    r = decimal(e["borrow_rates"][instrument])
                    if not 0 <= r <= 5 or utc(self.locates[instrument]["expires_at"]) < at:
                        raise ValueError("borrow rate/locate expiry invalid during accrual")
                    borrowing += amount(
                        -q * self.marks[instrument][0] * r * days / 365
                    ).to_decimal()
            self._post(
                e,
                [
                    ("assets:cash", interest - borrowing, None),
                    ("income:interest", -interest, None),
                    ("expenses:borrow", borrowing, None),
                ],
                LedgerEventType.FUNDING,
            )
            self.cash += interest - borrowing
            self.accrued_to = at
        elif kind == "dividend_ex":
            instrument, div_id = e["instrument"], e["dividend_id"]
            if self.instruments[instrument]["kind"] != "stock" or div_id in self.dividends:
                raise ValueError("distinct stock dividend required")
            value = amount(self.positions[instrument] * decimal(e["amount_per_share"])).to_decimal()
            if decimal(e["amount_per_share"]) < 0 or utc(e["pay_at"]) < at:
                raise ValueError("nonnegative dividend and future payment required")
            self.dividends[div_id] = (value, utc(e["pay_at"]))
            self.pending[div_id] = value
            self._post(
                e,
                [
                    ("assets:dividend_receivable", value, instrument),
                    ("income:dividend", -value, instrument),
                ],
                LedgerEventType.CORPORATE_ACTION,
            )
        elif kind == "dividend_pay":
            div_id = e["dividend_id"]
            if div_id not in self.pending or at < self.dividends[div_id][1]:
                raise ValueError("unpaid dividend at/after payment date required")
            value = self.pending.pop(div_id)
            self._post(
                e,
                [("assets:cash", value, None), ("assets:dividend_receivable", -value, None)],
                LedgerEventType.CORPORATE_ACTION,
            )
            self.cash += value
        elif kind == "split":
            instrument = e["instrument"]
            ratio = decimal(e["ratio"])
            if self.instruments[instrument]["kind"] != "stock" or ratio <= 0:
                raise ValueError("positive stock split ratio required")
            affected = {i for i, s in self.instruments.items() if s.get("underlying") == instrument}
            if set(e["option_adjustments"]) != affected:
                raise ValueError("explicit exchange adjustments required for every related option")
            old_quantity = self.positions[instrument]
            new_quantity = old_quantity * ratio
            if amount(new_quantity).to_decimal() != new_quantity:
                raise ValueError("fractional share precision requires cash-in-lieu handling")
            self.positions[instrument] = new_quantity
            if instrument in self.marks:
                p, observed = self.marks[instrument]
                self.marks[instrument] = (amount(p / ratio).to_decimal(), observed)
            if instrument in self.locates:
                self.locates[instrument]["quantity"] = str(
                    decimal(self.locates[instrument]["quantity"]) * ratio
                )
            for i in affected:
                spec, adjustment = self.instruments[i], e["option_adjustments"][i]
                multiplier, strike = (
                    decimal(adjustment["multiplier"]),
                    decimal(adjustment["strike"]),
                )
                if multiplier != decimal(spec["multiplier"]) * ratio or strike * ratio != decimal(
                    spec["strike"]
                ):
                    raise ValueError("only value-preserving pure splits supported by this event")
                spec.update(multiplier=str(multiplier), strike=str(strike), known_at=e["at"])
                if i in self.marks:
                    p, observed = self.marks[i]
                    self.marks[i] = (amount(p / ratio).to_decimal(), observed)
            self._post(
                e,
                [("assets:position_cost", 0, instrument), ("equity:split", 0, instrument)],
                LedgerEventType.CORPORATE_ACTION,
                {instrument: new_quantity - old_quantity},
            )
        elif kind == "margin":
            requirement = amount(e["requirement"]).to_decimal()
            if requirement < 0 or not e.get("model"):
                raise ValueError("nonnegative margin and declared model required")
            change = requirement - self.collateral
            transfer = min(change, max(self.cash, Decimal(0))) if change > 0 else change
            self._post(e, [("assets:cash", -transfer, None), ("assets:collateral", transfer, None)])
            self.cash -= transfer
            self.collateral += transfer
            self.required_margin = requirement
            if requirement > self.collateral:
                self.margin_calls.append(
                    {
                        "at": at.isoformat(),
                        "shortfall": str(requirement - self.collateral),
                        "model": e["model"],
                        "status": "requires_funding_or_liquidation",
                    }
                )
        elif kind == "exercise":
            instrument = e["instrument"]
            spec = self.instruments[instrument]
            if spec["kind"] != "option" or instrument in self.closed_options:
                raise ValueError("open option required for exercise/expiry")
            if at < utc(spec["expiry"]) and spec["style"] != "american":
                raise ValueError("European option cannot exercise before expiry")
            if utc(e["fixing_at"]) > at or utc(e["fixing_available_at"]) > at:
                raise ValueError("settlement fixing not available")
            if at >= utc(spec["expiry"]) and utc(e["fixing_at"]) != utc(spec["expiry"]):
                raise ValueError("exact expiry fixing required")
            spot, strike = decimal(e["fixing"]), decimal(spec["strike"])
            if spot <= 0:
                raise ValueError("positive settlement fixing required")
            held = self.positions[instrument]
            quantity = decimal(e.get("contracts", abs(held)))
            if quantity < 0 or quantity != int(quantity) or quantity > abs(held):
                raise ValueError("exercise quantity exceeds position")
            signed = quantity * (1 if held > 0 else -1)
            right_sign = 1 if spec["right"] == "call" else -1
            intrinsic = max(right_sign * (spot - strike), Decimal(0))
            if signed:
                self._trade(e, instrument, -signed, intrinsic, settlement=True)
                if spec["settlement"] == "physical" and intrinsic > 0:
                    # Closing option for intrinsic then acquiring/delivering shares
                    # at fixing is exactly equivalent to paying/receiving strike.
                    shares = signed * decimal(spec["multiplier"]) * right_sign
                    self._trade(e, spec["underlying"], shares, spot, settlement=True)
            if at >= utc(spec["expiry"]):
                if self.positions[instrument]:
                    raise ValueError("expiry must settle entire outstanding position")
                self.closed_options.add(instrument)
            if at >= utc(spec["expiry"]) or not self.positions[instrument]:
                self.marks[instrument] = (Decimal(0), at)
            else:
                # Intrinsic is the exercise payoff, not the fair value of the
                # remaining American contracts. Require a new observed mark.
                self.marks.pop(instrument, None)
            self.marks[spec["underlying"]] = (spot, utc(e["fixing_at"]))
        else:
            raise ValueError(f"unsupported lifecycle event: {kind}")

    def snapshot(self, *, max_mark_age_seconds=None):
        nav = self.cash + self.collateral + sum(self.pending.values())
        unpriced = []
        for instrument, q in self.positions.items():
            if not q:
                continue
            mark = self.marks.get(instrument)
            if mark is None or (
                max_mark_age_seconds is not None
                and self.at is not None
                and (self.at - mark[1]).total_seconds() > max_mark_age_seconds
            ):
                unpriced.append(instrument)
            else:
                nav += q * mark[0] * decimal(self.instruments[instrument]["multiplier"])
        return {
            "currency": self.currency,
            "at": self.at.isoformat() if self.at else None,
            "cash": str(self.cash),
            "collateral": str(self.collateral),
            "required_margin": str(self.required_margin),
            "margin_shortfall": str(max(0, self.required_margin - self.collateral)),
            "positions": {k: str(v) for k, v in self.positions.items()},
            "nav": str(amount(nav).to_decimal()) if not unpriced else None,
            "unpriced": unpriced,
            "dividend_receivables": str(sum(self.pending.values())),
            "transactions": len(self.transactions),
        }

    def journal(self):
        return [execution_payload(t) for t in self.transactions]
