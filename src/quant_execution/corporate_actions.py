"""Atomic, exact corporate-action application to the existing ledger.

No synthetic fills, no automatic rights election, and no inferred FX or cost
allocation. Complex conversions currently require same-currency cash equities.
Fractional quantities must be represented by the declared instrument step;
cash-in-lieu is a separate evidenced event, never a guessed rounding price.
"""

from decimal import Decimal

from quant_data_kit import CorporateActionEvent
from quant_data_kit.exceptions import ValidationError
from quant_data_kit.financial.actions import ActionTerms
from quant_data_kit.financial.common import number, utc

from ._fixed import decimal, fixed
from .contracts import LedgerEventType, LedgerTransaction


def apply_action(ledger, action: ActionTerms, *, at):
    ledger._require_mutable()
    stamp = utc(at)
    if stamp < utc(action.effective_at) or stamp < utc(action.available_at):
        raise ValidationError("corporate action is not effective and known yet")
    fingerprint = action.fingerprint()
    key = f"financial-action:{action.event_id}"
    if key in ledger._event_fingerprints:
        if ledger._event_fingerprints[key] != fingerprint:
            raise ValidationError("corporate action ID reused with changed terms")
        return ledger.snapshot()
    if stamp < ledger.snapshot().event_time:
        raise ValidationError("corporate action cannot reverse account time")
    if ledger._artifact_sink is not None:
        raise ValidationError("complex action batch requires nonstreaming atomic replay")
    spec = ledger._spec(action.instrument_id)
    if ledger._is_derivative(spec) or spec.settlement_currency != action.currency:
        raise ValidationError("action requires matching cash-asset currency")
    state = ledger.capture_state()
    try:
        if action.kind == "cash_in_lieu":
            _cash_in_lieu(ledger, action, stamp)
        elif action.kind in {"split", "dividend_entitlement", "dividend_payment", "terminal_cash"}:
            kind = {
                "dividend_entitlement": "cash_dividend_entitlement",
                "dividend_payment": "cash_dividend_payment",
            }.get(action.kind, action.kind)
            cash = (
                None
                if action.kind == "split"
                else fixed(number(action.cash_per_unit), ledger.money_scale)
            )
            ratio = fixed(number(action.ratio), 12) if action.kind == "split" else None
            if action.kind == "terminal_cash":
                ratio = fixed(Decimal(0), 0)
            ledger.apply(
                CorporateActionEvent(
                    event_id=key,
                    instrument_id=action.instrument_id,
                    action_type=kind,
                    event_time=stamp,
                    received_at=stamp,
                    available_at=stamp,
                    source=action.source + "#" + action.evidence_id,
                    trading_day=stamp.date(),
                    session_id="financial-actions",
                    sequence=0,
                    effective_date=(
                        utc(action.entitlement_date + "T00:00:00Z").date()
                        if action.entitlement_date
                        else stamp.date()
                    ),
                    ratio=ratio,
                    cash_amount=cash,
                    currency=action.currency if cash is not None else None,
                )
            )
        else:
            _convert(ledger, action, stamp)
        ledger._event_fingerprints[key] = fingerprint
        return ledger.snapshot(stamp)
    except Exception:
        ledger.restore_state(state)
        raise


def _cash_in_lieu(ledger, action, stamp):
    spec = ledger._spec(action.instrument_id)
    old = ledger._positions.get(action.instrument_id, Decimal(0))
    retired = number(action.retired_quantity)
    if retired > old or retired % decimal(spec.quantity_step):
        raise ValidationError("cash-in-lieu quantity exceeds holding or violates declared step")
    cost = decimal(
        fixed(
            ledger._position_cost(action.instrument_id, derivative=False) * retired / old,
            ledger.money_scale,
        )
    )
    cash = decimal(fixed(retired * number(action.cash_per_unit), ledger.money_scale))
    currency = action.currency
    postings = [
        ledger._posting("assets:cash", currency, cash),
        ledger._posting(
            "assets:position_cost", currency, -cost, instrument_id=action.instrument_id
        ),
        ledger._posting(
            "income:realized_pnl", currency, cost - cash, instrument_id=action.instrument_id
        ),
    ]
    for account, delta in (("assets:position", -retired), ("memo:position_counter", retired)):
        postings.append(
            ledger._posting(
                account,
                currency,
                Decimal(0),
                instrument_id=action.instrument_id,
                quantity_delta=delta,
                quantity_scale=spec.quantity_step.scale,
            )
        )
    tx = ledger._make_transaction(
        event_type=LedgerEventType.CORPORATE_ACTION,
        reference_id=action.event_id,
        idempotency_key=f"financial-action:{action.event_id}",
        event_time=stamp,
        postings=tuple(postings),
    )
    LedgerTransaction.__post_init__(tx)
    ledger._post(tx)
    remaining, lots = retired, []
    for acquired, quantity in ledger._position_lots.get(action.instrument_id, []):
        take = min(quantity, remaining)
        remaining -= take
        if quantity > take:
            lots.append((acquired, quantity - take))
    if remaining:
        raise ValidationError("cash-in-lieu quantity is not backed by holding lots")
    ledger._position_lots[action.instrument_id] = lots
    if retired == old:
        ledger._marks.pop(action.instrument_id, None)
    ledger._event_time = stamp


def _convert(ledger, action, stamp):
    parent, target = ledger._spec(action.instrument_id), ledger._spec(action.target_id)
    if (
        ledger._is_derivative(target)
        or target.settlement_currency != action.currency
        or decimal(parent.contract_multiplier) != 1
        or decimal(target.contract_multiplier) != 1
    ):
        raise ValidationError("conversion requires same-currency unit-multiplier cash assets")
    old = ledger._positions.get(action.instrument_id, Decimal(0))
    if old < 0:
        raise ValidationError("short corporate-action conversion is not supported")
    exercise = action.kind == "rights_exercise"
    distribution = action.kind in {"spin_off", "rights_distribution"}
    eligible = number(action.election_quantity) if exercise else old
    if eligible > old:
        raise ValidationError("election exceeds held rights")
    added = eligible * number(action.ratio)
    removed = Decimal(0) if distribution else eligible
    if added % decimal(target.quantity_step) or removed % decimal(parent.quantity_step):
        raise ValidationError(
            "fractional entitlement requires explicit step or cash-in-lieu evidence"
        )
    cost = ledger._position_cost(action.instrument_id, derivative=False)
    moved = (
        cost
        * (eligible / old if old else Decimal(0))
        * (Decimal(1) if exercise else number(action.cost_fraction))
    )
    moved = decimal(fixed(moved, ledger.money_scale))
    removed_cost = (
        moved
        if distribution
        else decimal(fixed(cost * (eligible / old if old else Decimal(0)), ledger.money_scale))
    )
    cash = eligible * number(action.cash_per_unit) * (-1 if exercise else 1)
    cash = decimal(fixed(cash, ledger.money_scale))
    if ledger.cash_balance(action.currency) + cash < 0:
        raise ValidationError("insufficient cash for explicitly elected rights subscription")
    target_cost = moved - cash if exercise else moved
    postings = [
        ledger._posting("assets:cash", action.currency, cash),
        ledger._posting(
            "assets:position_cost",
            action.currency,
            -removed_cost,
            instrument_id=action.instrument_id,
        ),
        ledger._posting(
            "assets:position_cost", action.currency, target_cost, instrument_id=action.target_id
        ),
        ledger._posting(
            "income:realized_pnl",
            action.currency,
            removed_cost - target_cost - cash,
            instrument_id=action.instrument_id,
        ),
    ]
    for instrument, delta, step in (
        (action.instrument_id, -removed, parent.quantity_step),
        (action.target_id, added, target.quantity_step),
    ):
        for account, sign in (("assets:position", 1), ("memo:position_counter", -1)):
            postings.append(
                ledger._posting(
                    account,
                    action.currency,
                    Decimal(0),
                    instrument_id=instrument,
                    quantity_delta=delta * sign,
                    quantity_scale=step.scale,
                )
            )
    tx = ledger._make_transaction(
        event_type=LedgerEventType.CORPORATE_ACTION,
        reference_id=action.event_id,
        idempotency_key=f"financial-action:{action.event_id}",
        event_time=stamp,
        postings=tuple(postings),
    )
    # _make_transaction is a trusted fast path; independently verify exact balance here.
    LedgerTransaction.__post_init__(tx)
    if any(
        p.amount.units or (p.quantity_delta is not None and p.quantity_delta.units)
        for p in postings
    ):
        ledger._post(tx)
    if removed:
        residual, consumed = [], []
        remaining = removed
        for acquired, quantity in ledger._position_lots.get(action.instrument_id, []):
            taken = min(remaining, quantity)
            remaining -= taken
            if taken:
                consumed.append(
                    (stamp.date() if exercise else acquired, taken * number(action.ratio))
                )
            if quantity > taken:
                residual.append((acquired, quantity - taken))
        if remaining:
            raise ValidationError("position lots do not cover conversion")
        ledger._position_lots[action.instrument_id] = residual
    else:
        consumed = [(stamp.date(), added)] if added else []
    ledger._position_lots.setdefault(action.target_id, []).extend(consumed)
    ledger._marks[action.target_id] = (number(action.target_mark), stamp, action.event_id)
    if distribution:
        ledger._marks[action.instrument_id] = (number(action.parent_mark), stamp, action.event_id)
    elif not ledger._positions.get(action.instrument_id):
        ledger._marks.pop(action.instrument_id, None)
    ledger._event_time = stamp
