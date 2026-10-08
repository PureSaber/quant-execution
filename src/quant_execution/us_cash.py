"""USD cash research account backed by the shared exact double-entry ledger.

Trade-date cash and settled buying power are distinct. Sale proceeds remain
unavailable until standard settlement. Purchases use settled funds only; thus
same-day resale does not inherit the domestic T+1 holding restriction.
"""

from __future__ import annotations

from datetime import datetime
from decimal import ROUND_DOWN, Decimal

from quant_data_kit import (
    AssetClass,
    CorporateActionEvent,
    FixedPoint,
    InstrumentSpec,
    MarkPriceEvent,
)
from quant_data_kit.us_research.calendar import settlement_session
from quant_data_kit.us_research.prices import utc

from .contracts import Fee, Fill, LedgerEventType, Side
from .ledger import ExactAccountLedger


def money(value) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("finite decimal required")
    return result


def fp(value, scale: int = 8) -> FixedPoint:
    return FixedPoint.from_decimal(money(value).quantize(Decimal(1).scaleb(-scale)), scale)


def settled_cash(ledger: ExactAccountLedger, at: datetime) -> Decimal:
    if utc(at) < ledger.snapshot().event_time:
        raise ValueError("cash query cannot precede current account state")
    day = str(utc(at).tz_convert("America/New_York").date())
    pending = Decimal(0)
    for transaction in ledger.transactions:
        if transaction.event_type is not LedgerEventType.FILL:
            continue
        trade_day = str(utc(transaction.event_time).tz_convert("America/New_York").date())
        if settlement_session(trade_day) > day:
            pending += sum(
                (
                    money(p.amount.to_decimal())
                    for p in transaction.postings
                    if p.ledger_account == "assets:cash"
                    and p.currency == "USD"
                    and p.amount.units > 0
                ),
                Decimal(0),
            )
    return ledger.cash_balance("USD") - pending


def instrument(
    instrument_id: str, symbol: str, at: datetime, *, etf: bool = False
) -> InstrumentSpec:
    return InstrumentSpec(
        instrument_id=instrument_id,
        asset_class=AssetClass.ETF if etf else AssetClass.EQUITY,
        product_type="us_etf" if etf else "us_equity",
        venue="US-CONSOLIDATED",
        native_symbol=symbol,
        settlement_currency="USD",
        quote_currency="USD",
        price_tick=fp("0.00000001"),
        quantity_step=fp("0.000001", 6),
        contract_multiplier=fp(1, 0),
        calendar_id="XNYS",
        effective_from=at,
        available_at=at,
        metadata={"market": "US", "commission_rate": "0"},
    )


class USCashAccount:
    """Long-only, fractional research units, settled-cash funded, no broker API."""

    def __init__(
        self,
        instruments: dict[str, InstrumentSpec],
        initial_cash,
        opened_at: datetime,
        *,
        commission_bps=1,
        slippage_bps=2,
    ):
        self.commission = money(commission_bps) / 10000
        self.slippage = money(slippage_bps) / 10000
        if not 0 <= self.commission < 1 or not 0 <= self.slippage < 1 or money(initial_cash) <= 0:
            raise ValueError("invalid cash or costs")
        if any(
            spec.settlement_currency != "USD"
            or spec.quote_currency != "USD"
            or spec.asset_class not in {AssetClass.EQUITY, AssetClass.ETF}
            or spec.product_type not in {"us_equity", "us_etf"}
            or spec.contract_multiplier.to_decimal() != 1
            for spec in instruments.values()
        ):
            raise ValueError("explicit USD US equity instruments required")
        self.ledger = ExactAccountLedger(
            account_id="us-research",
            base_currency="USD",
            instruments=instruments,
            initial_cash={"USD": fp(initial_cash)},
            opened_at=opened_at,
        )
        self.fills: list[dict] = []
        self._trades: dict[str, tuple] = {}

    def quantity(self, instrument_id: str) -> Decimal:
        value = self.ledger.snapshot().positions.get(instrument_id)
        return Decimal(0) if value is None else money(value.to_decimal())

    def buying_power(self, at) -> Decimal:
        return max(Decimal(0), settled_cash(self.ledger, utc(at)))

    @staticmethod
    def _fields(event_id, instrument_id, at):
        stamp = utc(at)
        day = stamp.tz_convert("America/New_York").date()
        return {
            "event_id": event_id,
            "instrument_id": instrument_id,
            "event_time": stamp,
            "received_at": stamp,
            "available_at": stamp,
            "source": "us-research-model",
            "trading_day": day,
            "session_id": f"XNYS:{day}",
            "sequence": 0,
        }

    def mark(self, instrument_id, price, at, event_id):
        self._check_time(at)
        self.ledger.mark(
            MarkPriceEvent(**self._fields(event_id, instrument_id, at), price=fp(price))
        )

    def action(
        self, instrument_id, at, event_id, *, ratio=None, cash=None, payment=False, ex_date=None
    ):
        self._check_time(at)
        fields = self._fields(event_id, instrument_id, at)
        effective = fields["trading_day"] if ex_date is None else ex_date
        self.ledger.apply(
            CorporateActionEvent(
                **fields,
                action_type=("cash_dividend_payment" if payment else "cash_dividend_entitlement")
                if cash is not None
                else "split",
                effective_date=effective,
                ratio=fp(ratio, 6) if ratio is not None else None,
                cash_amount=fp(cash) if cash is not None else None,
                currency="USD" if cash is not None else None,
            )
        )

    def _check_time(self, at):
        if utc(at) < self.ledger.snapshot().event_time:
            raise ValueError("account event cannot precede current account state")

    def trade(self, instrument_id, quantity, reference_price, at, trade_id) -> dict:
        quantity, price = money(quantity), money(reference_price)
        stamp = utc(at)
        fingerprint = (instrument_id, quantity, price, stamp)
        if trade_id in self._trades:
            if self._trades[trade_id] != fingerprint:
                raise ValueError("trade id reused with changed content")
            return next(row for row in self.fills if row["trade_id"] == trade_id)
        self._check_time(stamp)
        if quantity == 0 or price <= 0:
            raise ValueError("nonzero quantity and positive price required")
        side = Side.BUY if quantity > 0 else Side.SELL
        absolute = abs(quantity).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)
        if absolute == 0 or absolute != abs(quantity):
            raise ValueError("quantity exceeds supported 6-decimal precision")
        execution_price = fp(price * (1 + self.slippage if side is Side.BUY else 1 - self.slippage))
        notional = money(execution_price.to_decimal()) * absolute
        fee = fp(notional * self.commission)
        if side is Side.SELL and absolute > self.quantity(instrument_id):
            raise ValueError("cash account cannot sell short")
        if side is Side.BUY and notional + money(fee.to_decimal()) > self.buying_power(stamp):
            raise ValueError("insufficient settled USD buying power")
        fill = Fill(
            fill_id=trade_id,
            order_id=trade_id,
            account_id="us-research",
            strategy_id="daily",
            instrument_id=instrument_id,
            side=side,
            quantity=fp(absolute, 6),
            price=execution_price,
            event_time=stamp,
        )
        # Both events are prevalidated; rollback keeps the pair atomic on any ledger rejection.
        state = self.ledger.capture_state()
        try:
            self.ledger.apply(fill)
            self.ledger.apply(
                Fee(
                    fee_id=f"{trade_id}:fee",
                    fill_id=trade_id,
                    account_id="us-research",
                    amount=fee,
                    currency="USD",
                    event_time=stamp,
                    fee_type="commission",
                )
            )
        except Exception:
            self.ledger.restore_state(state)
            raise
        record = {
            "trade_id": trade_id,
            "instrument_id": instrument_id,
            "at": stamp.isoformat(),
            "side": side.value,
            "quantity": str(absolute),
            "price": str(execution_price.to_decimal()),
            "fee": str(fee.to_decimal()),
            "reference_price": str(price),
        }
        self._trades[trade_id] = fingerprint
        self.fills.append(record)
        return record

    def validate_balance(self):
        for transaction in self.ledger.transactions:
            totals: dict[str, Decimal] = {}
            for posting in transaction.postings:
                totals[posting.currency] = totals.get(posting.currency, Decimal(0)) + money(
                    posting.amount.to_decimal()
                )
            if any(value != 0 for value in totals.values()):
                raise AssertionError(
                    f"unbalanced QExec transaction {transaction.transaction_id}: {totals}; {transaction.postings}"
                )
